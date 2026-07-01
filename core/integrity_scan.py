"""Twice-daily PC integrity scan — runs in the background while GhostShell
is minimized to the tray and no game is active.

The whole module is built around one principle: **failure mode = did
nothing**.  If any precondition is unclear, any path is suspicious, any
size is unexpected — we skip rather than delete.  This is not an
antivirus; it's a maintenance routine that surfaces actionable findings
to the user and tidies up files GhostShell itself created.

Three phases per cycle:

  Phase A — GhostShell self-clean (auto-action)
    Walks %APPDATA%/GhostShell against a hard-coded ALLOW-LIST of glob
    patterns + age thresholds.  Anything not on the allow-list is
    ignored regardless of age.  Path traversal protected: the resolved
    real path must startswith APPDATA_DIR or we refuse.

  Phase B — Windows temp + Recycle Bin (auto-action, conservative)
    Cleans %TEMP%/%TMP% files older than 7 days that we can open for
    write (skips locked / in-use).  Recycle Bin items deleted >30 days
    ago + <100 MB each get removed via Shell.Application COM.

  Phase C — System health probe (detect-only)
    Top RAM consumers, low disk space, recent service crashes, SMART
    warnings, broken autoruns.  NEVER auto-fixes; results land in
    integrity_scan_status.json for the UI to surface, and a warn toast
    fires when something looks genuinely actionable.

Gating (ALL must hold to start a scan):
  • GhostShell window not in foreground
  • No tracked game running
  • System idle ≥ 30 s
  • System CPU < 30 %

Robustness guarantees:
  • Phase A allow-list is small, explicit, and reviewed.  Any new
    pattern needs to be added intentionally.
  • Every individual file delete is its own try/except — one broken
    delete never kills the scan or any subsequent phase.
  • Hard 5-minute total time budget per cycle (Phase A+B+C combined).
  • Thread runs at BELOW_NORMAL_PRIORITY_CLASS so we don't compete
    with anything the user actually cares about.
  • Audit log at %APPDATA%/GhostShell/integrity_scan_history.json
    (ring buffer, last 100 cycles) — every deletion records
    {path, size, kind, ts}.
  • Settings toggle to disable entirely.

UI integration:
  • Status endpoint /api/integrity/status — current state + last cycle
  • History endpoint /api/integrity/history — last 100 cycles + actions
  • Manual /api/integrity/run-now for "scan now" button
  • Settings endpoints to toggle on/off + adjust interval
  • Toast fires only when something genuinely actionable lands in
    Phase C findings — routine "cleaned N files freed M MB" stays
    log-only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
import subprocess
from typing import Optional, Iterable

from config import APPDATA_DIR
from core.utils import get_logger, atomic_write_json

log = get_logger("integrity_scan")

# ─── Constants ───────────────────────────────────────────────────────

# Default cadence chosen via AskUserQuestion → "Twice daily (every 12 hours)".
# Stored as seconds; settable via /api/integrity/settings.
DEFAULT_INTERVAL_SEC = 12 * 3600

# Hard cap on how long a single scan cycle can run, total.  Phase A+B+C
# add up; if any one phase blows past its share, the cycle is cut short
# and resumes on the next tick.
MAX_CYCLE_DURATION_SEC = 5 * 60

# Idle threshold — no keyboard / mouse activity for this long before
# we'll consider starting a scan.  Matches the updater's safety gate so
# the two don't race when the user steps away from the PC.
USER_IDLE_THRESHOLD_SEC = 30

# CPU usage ceiling — abort the scan start if anything else is busy.
# Re-checked once per cycle, not per file.
CPU_CEILING_PCT = 30.0

# How often to re-evaluate the gating conditions when we're deferring.
GATE_RECHECK_SEC = 5 * 60

# ─── Self-clean allow-list ───────────────────────────────────────────
# Every entry is (glob_pattern, max_age_days, description).
# Patterns are relative to APPDATA_DIR and use forward slashes.
# Age is "modified time"; any file younger gets left alone.
# Anything NOT matched by this list is ignored entirely — no exceptions.

_SELF_CLEAN_RULES = [
    # Rotated GhostShell logs — keep the current one (no number suffix)
    # and the most recent rotated one.  Older rotated logs go after 14d.
    ("ghostshell.log.*", 14, "rotated GhostShell log"),

    # Cached old installer downloads.  Once an install has happened the
    # .exe sitting in updates/ is just a stale copy of the old version.
    # 7 days is well past any sensible re-install window.
    ("updates/GhostShell_*.exe", 7,  "old cached installer"),
    ("updates/GhostShell_*.zip", 7,  "old cached source bundle"),

    # Partial downloads that never completed.  1 day is generous — a
    # real download finishes in seconds.
    ("updates/*.part",            1,  "orphan partial download"),

    # One-shot scripts written by the legacy in-place updater.  These
    # only matter for ~30 s during an install.  Any older copy is dead.
    ("updates/update.bat",                 1,  "old update script"),
    ("updates/last_install.status",        1,  "old install status file"),

    # Updater binary's own log.  GhostShellUpdater is short-lived; its
    # log fills up across many install cycles and 30 days of history is
    # plenty for any post-mortem.
    ("updater/updater.log",       30, "old updater log"),

    # One-shot NVIDIA clean-install script generated by the driver
    # cleaner.  Lives for one click; not needed afterwards.
    ("nvidia_clean.bat",          1,  "old NVIDIA clean script"),

    # Local copies of error reports.  Once they've been submitted to
    # Railway and acknowledged, they exist only as a fallback for
    # offline-time errors.  14 days is plenty.
    ("error_reports/*.json",      14, "old local error report"),

    # Acknowledged recovery report.  '.seen' suffix means the user
    # already clicked through the apology toast.  90 days because some
    # users like to keep a record of what was reverted.
    ("recovery_report.json.seen", 90, "old acknowledged recovery report"),

    # Install logs from winget invocations.  30 days = same retention
    # window as updater log.
    ("install_logs/*.log",        30, "old install log"),

    # Frame-telemetry CSVs.  PresentMon writes one CSV per game session;
    # we keep a week's worth for any post-mortem AT analysis.
    ("frame_telemetry/*.csv",     7,  "old frame telemetry CSV"),
]

# Files we ABSOLUTELY DO NOT TOUCH no matter what.  Even if a path
# somehow ends up matching a pattern above, if it matches a name here
# we abort the delete.  Belt-and-suspenders.
_NEVER_TOUCH_NAMES = {
    "vault.db", "vault.db-journal", "vault.db-wal", "vault.db-shm",
    "ghostshell.log",                       # current log — only rotated copies are cleanable
    "install_info.json", "updater_settings.json",
    "optimizer_pro_settings.json", "autostart_settings.json",
    "background_pauser.json", "clipper_settings.json",
    "community_anon_id", "community_settings.json",
    "dependencies_state.json", "driver_updater_settings.json",
    "error_reporter_settings.json", "gameplay_baselines.json",
    "gamepad_profiles.json", "user_recoil_presets.json",
    "gpu_oc_profile.json", "gpu_stock_baseline.json",
    "obs_settings.json", "power_baseline.json",
    "stream_helper_settings.json", "temp_ceiling_settings.json",
    "adaptive_tuning_settings.json", "adaptive_tuning_state.json",
    "integrity_scan_settings.json", "integrity_scan_history.json",
    "integrity_scan_status.json",
    # Updater & active binary cache
    "GhostShellUpdater.exe", "updater_version.json",
    "PresentMon.exe",
    # Recovery / install handoff
    "recovery_report.json",
}

# Directories we never recurse INTO for Phase A.  Files inside these
# dirs are user data (snapshots, backups, profiles) and don't belong
# to GhostShell's own scratch space.
_NEVER_RECURSE_DIRS = {
    "backups", "snapshots", "profiles", "game_profiles", "drivers",
    "bin",            # holds downloaded binaries we still need
    "updater",        # one allow-listed file inside (updater.log)
}

# Hard cap on file size that auto-clean is willing to touch.  Anything
# bigger than this is "suspicious" — config files don't grow to GB.
_AUTOCLEAN_MAX_BYTES = 200 * 1024 * 1024     # 200 MB

# ─── Paths ───────────────────────────────────────────────────────────

SETTINGS_PATH = os.path.join(APPDATA_DIR, "integrity_scan_settings.json")
HISTORY_PATH  = os.path.join(APPDATA_DIR, "integrity_scan_history.json")
STATUS_PATH   = os.path.join(APPDATA_DIR, "integrity_scan_status.json")

# Ring-buffer cap for history.json on disk.  Each entry is one cycle
# summary plus actions taken; 100 cycles = 50 days at twice-daily.
_HISTORY_MAX_ENTRIES = 100

# ─── State ───────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state = {
    "running":            False,
    "current_phase":      None,
    "started_at":         0.0,
    "ended_at":           0.0,
    "next_run_at":        0.0,
    "last_cycle":         None,   # summary dict, written on success
    "last_findings":      None,   # actionable items from Phase C
}
_scan_thread: Optional[threading.Thread] = None
_scan_stop:   Optional[threading.Event]  = None

# Settings cache — loaded on import, re-loaded after any set_settings.
_DEFAULT_SETTINGS = {
    "enabled":             True,
    "interval_sec":        DEFAULT_INTERVAL_SEC,
    "clean_self":          True,
    "clean_windows_temp":  True,
    "clean_recycle_bin":   True,
    "auto_disable_startup":False,   # off by default per user's choice
    "toast_on_findings":   True,    # toast only when actionable
    "last_run_ts":         0.0,
}

_CREATE_NO_WINDOW = 0x08000000


# ─── Settings + history persistence ──────────────────────────────────

def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return {**_DEFAULT_SETTINGS, **data}
    except Exception as e:
        log.debug(f"settings load failed: {e}")
        return dict(_DEFAULT_SETTINGS)


def _save_settings(s: dict) -> None:
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load_settings()


def set_settings(**kw) -> dict:
    s = _load_settings()
    for k, v in kw.items():
        if k in _DEFAULT_SETTINGS:
            s[k] = v
    _save_settings(s)
    log.info(f"integrity-scan settings updated: {kw}")
    return s


def _trim_cycle_for_persist(cycle: dict) -> None:
    """In-place: replace each phase's full `actions` list with a small
    sample + count, so the persisted status/history JSON stays tiny
    regardless of how many files a cleanup pass touched.  Aggregate
    numbers (files/bytes_freed/errors) are preserved."""
    for phase in cycle.get("phases", []) or []:
        acts = phase.get("actions")
        if isinstance(acts, list) and len(acts) > 5:
            phase["actions_sample"] = acts[:5]
            phase["actions_total"]  = len(acts)
            phase["actions"]        = []   # drop the full list


def _append_history(entry: dict) -> None:
    """Add one cycle summary to the ring buffer.  Atomic: writes to .tmp,
    fsyncs, os.replace."""
    try:
        hist = _load_history()
        hist.append(entry)
        hist = hist[-_HISTORY_MAX_ENTRIES:]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f)
            f.flush()
            try: os.fsync(f.fileno())
            except OSError: pass
        os.replace(tmp, HISTORY_PATH)
    except Exception as e:
        log.debug(f"history append failed: {e}")


def _load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_history(limit: int = 50) -> list:
    rows = _load_history()
    return rows[-max(1, min(_HISTORY_MAX_ENTRIES, int(limit))):]


def _write_status_snapshot() -> None:
    """Mirror the in-memory state to disk so callers reading the file
    directly (or the UI on a fresh page load) see the latest."""
    with _state_lock:
        snap = dict(_state)
    atomic_write_json(STATUS_PATH, snap)


def get_status() -> dict:
    with _state_lock:
        snap = dict(_state)
    snap["settings"] = _load_settings()
    return snap


# ─── Gating ──────────────────────────────────────────────────────────

def _get_user_idle_sec() -> float:
    """Match updater.py's idle probe.  Returns 0.0 on any error so we
    default to 'user is active' and defer."""
    try:
        import ctypes
        from ctypes import wintypes
        class _LII(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD)]
        lii = _LII()
        lii.cbSize = ctypes.sizeof(_LII)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _is_ghostshell_foreground() -> bool:
    try:
        from core.gamepad_mapper import _get_foreground_exe_name
        return (_get_foreground_exe_name() or "").lower() == "ghostshell.exe"
    except Exception:
        return False


def _is_game_running() -> bool:
    try:
        from core import game_profiles
        info = game_profiles.get_active_game_info()
        return bool(info and info.get("exe"))
    except Exception:
        return False


def _system_cpu_pct() -> float:
    """One-shot CPU load read.  Uses psutil if present, otherwise WMI.
    Falls back to 0.0 (treat as idle) on any error so we don't
    over-skip — we'd rather scan during a brief CPU spike than miss a
    cycle entirely."""
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.5))
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["wmic", "cpu", "get", "loadpercentage", "/value"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        for line in (r.stdout or "").splitlines():
            if "LoadPercentage" in line:
                try:
                    return float(line.split("=", 1)[1].strip())
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    return 0.0


def _safe_to_run() -> tuple[bool, str]:
    """Returns (ok, reason).  reason is empty on success, populated
    with a short string on defer."""
    if _is_game_running():
        return False, "game in foreground"
    if _is_ghostshell_foreground():
        return False, "GhostShell window in foreground"
    idle = _get_user_idle_sec()
    if idle < USER_IDLE_THRESHOLD_SEC:
        return False, f"user active ({int(idle)}s idle)"
    cpu = _system_cpu_pct()
    if cpu > CPU_CEILING_PCT:
        return False, f"system busy ({cpu:.0f}% CPU)"
    return True, ""


# ─── Phase A: GhostShell self-clean ──────────────────────────────────

def _resolve_inside_appdata(path: str) -> Optional[str]:
    """Return the real path iff it resolves inside APPDATA_DIR.
    Defeats symlink + .. traversal — any path that bounces outside
    APPDATA_DIR comes back as None and the caller refuses to act."""
    try:
        real = os.path.realpath(path)
        appdata_real = os.path.realpath(APPDATA_DIR)
        # +os.sep so APPDATA_DIR_evil_twin doesn't false-match.
        if real == appdata_real:
            return real
        if real.startswith(appdata_real + os.sep):
            return real
        return None
    except Exception:
        return None


def _file_age_days(path: str) -> Optional[float]:
    try:
        st = os.stat(path)
        return (time.time() - st.st_mtime) / 86400.0
    except OSError:
        return None


def _phase_a_self_clean(deadline_ts: float) -> dict:
    """Walk APPDATA_DIR against the allow-list; delete matched files
    older than their rule's threshold.  Returns a summary."""
    actions: list = []
    err_count = 0
    bytes_freed = 0
    skipped: dict = {}     # reason → count, for logging

    for rel_pattern, max_age_days, kind in _SELF_CLEAN_RULES:
        if time.time() > deadline_ts:
            log.info("Phase A: deadline reached mid-rule, stopping early")
            break
        # Walk matching files
        try:
            yield_from = _iter_matches(rel_pattern)
        except Exception as e:
            log.debug(f"matcher error for {rel_pattern!r}: {e}")
            continue
        for path in yield_from:
            if time.time() > deadline_ts:
                break
            # Defensive: re-validate every single path before touching it.
            resolved = _resolve_inside_appdata(path)
            if not resolved:
                skipped["traversal_block"] = skipped.get("traversal_block", 0) + 1
                continue
            name = os.path.basename(resolved)
            if name in _NEVER_TOUCH_NAMES:
                skipped["never_touch"] = skipped.get("never_touch", 0) + 1
                continue
            # Age check
            age = _file_age_days(resolved)
            if age is None or age < max_age_days:
                skipped["too_young"] = skipped.get("too_young", 0) + 1
                continue
            # Size guard
            try:
                sz = os.path.getsize(resolved)
            except OSError:
                continue
            if sz > _AUTOCLEAN_MAX_BYTES:
                skipped["oversized"] = skipped.get("oversized", 0) + 1
                log.info(f"skipping {resolved} ({sz} B > cap)")
                continue
            # Pace ourselves — one tiny pause per file keeps CPU usage
            # invisible.  Cumulatively negligible vs the 5-min budget.
            time.sleep(0.005)
            try:
                os.remove(resolved)
            except OSError as e:
                err_count += 1
                log.debug(f"could not delete {resolved}: {e}")
                continue
            bytes_freed += sz
            actions.append({
                "phase": "A", "kind": kind, "path": resolved,
                "size": sz, "age_days": round(age, 1),
            })

    return {
        "phase":       "A",
        "actions":     actions,
        "files":       len(actions),
        "bytes_freed": bytes_freed,
        "errors":      err_count,
        "skipped":     skipped,
    }


def _iter_matches(rel_pattern: str) -> Iterable[str]:
    """Yield absolute paths matching `rel_pattern` (relative to APPDATA_DIR).
    Pattern may contain `*` and `?` like a shell glob.  Cheap walk —
    skips directories listed in _NEVER_RECURSE_DIRS at the top level.

    `updater/updater.log` style patterns (with a slash) walk into exactly
    that subdir; simple names match top-level only."""
    # Normalize separators
    rel_pattern = rel_pattern.replace("/", os.sep)
    if os.sep in rel_pattern:
        subdir, name_pat = rel_pattern.rsplit(os.sep, 1)
        base_dir = os.path.join(APPDATA_DIR, subdir)
        if not os.path.isdir(base_dir):
            return
        try:
            for entry in os.scandir(base_dir):
                if entry.is_file(follow_symlinks=False):
                    if fnmatch.fnmatch(entry.name, name_pat):
                        yield entry.path
        except OSError:
            return
    else:
        try:
            for entry in os.scandir(APPDATA_DIR):
                if entry.is_file(follow_symlinks=False):
                    if fnmatch.fnmatch(entry.name, rel_pattern):
                        yield entry.path
        except OSError:
            return


def _phase_a2_prune_driver_cache(deadline_ts: float) -> dict:
    """Prune stale NVIDIA driver installers from APPDATA/drivers.

    These are ~960 MB each — they blow past Phase A's 200 MB per-file
    safety cap by design, so the generic allow-list can't touch them.
    This dedicated step targets ONLY drivers/*.exe (a known, GhostShell-
    managed, always-large-and-safe pattern) and removes any older than
    30 days.  By then the user has either installed the driver (making
    the installer dead weight) or abandoned it (a re-check re-downloads).
    The driver_updater also prunes on download — this is the autonomous
    backstop for the 'downloaded once, never updated again' case.

    Path-validated to stay inside APPDATA/drivers; the never-touch name
    list still applies."""
    drivers_dir = os.path.join(APPDATA_DIR, "drivers")
    actions: list = []
    bytes_freed = 0
    err = 0
    skipped: dict = {}
    MAX_AGE_DAYS = 30
    if not os.path.isdir(drivers_dir):
        return {"phase": "A2-drivers", "files": 0, "bytes_freed": 0, "actions": []}
    try:
        entries = list(os.scandir(drivers_dir))
    except OSError:
        return {"phase": "A2-drivers", "files": 0, "bytes_freed": 0, "errors": 1}
    for entry in entries:
        if time.time() > deadline_ts:
            break
        if not entry.name.lower().endswith(".exe"):
            continue
        resolved = _resolve_inside_appdata(entry.path)
        if not resolved:
            skipped["traversal_block"] = skipped.get("traversal_block", 0) + 1
            continue
        if os.path.basename(resolved) in _NEVER_TOUCH_NAMES:
            skipped["never_touch"] = skipped.get("never_touch", 0) + 1
            continue
        age = _file_age_days(resolved)
        if age is None or age < MAX_AGE_DAYS:
            skipped["too_young"] = skipped.get("too_young", 0) + 1
            continue
        try:
            sz = os.path.getsize(resolved)
        except OSError:
            continue
        try:
            os.remove(resolved)
        except OSError as e:
            err += 1
            log.debug(f"driver prune could not delete {resolved}: {e}")
            continue
        bytes_freed += sz
        actions.append({"phase": "A2-drivers", "kind": "old driver installer",
                        "path": resolved, "size": sz, "age_days": round(age, 1)})
    if bytes_freed:
        log.info(f"integrity scan pruned {len(actions)} stale driver "
                 f"installer(s), freed {bytes_freed/(1024**2):.0f} MB")
    return {"phase": "A2-drivers", "files": len(actions),
            "bytes_freed": bytes_freed, "errors": err,
            "actions": actions, "skipped": skipped}


# ─── Phase B: Windows temp + Recycle Bin ─────────────────────────────

def _phase_b_temp_cleanup(deadline_ts: float, settings: dict) -> dict:
    """Clean OS temp files older than 7 days that we can open for write.
    Skips locked / in-use files (those are by definition still relevant
    to something).  Caps individual deletions at 100 MB so we never
    nuke a freshly-downloaded large installer."""
    actions: list = []
    bytes_freed = 0
    err_count = 0
    skipped: dict = {}

    if not settings.get("clean_windows_temp", True):
        return {"phase": "B-temp", "skipped_setting": True}

    temp_dirs = [
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        # %LOCALAPPDATA%/Temp is a separate path on some Windows installs
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp"),
    ]
    seen_real = set()
    candidates = []
    # Hard rule — never clean inside any path under %WINDIR% (typically
    # C:\Windows).  When GhostShell runs as admin via UAC, %TEMP% can
    # resolve to C:\Windows\Temp; cleaning that is firmly system-side
    # territory and we keep our hands off.
    win_dir = os.environ.get("WINDIR") or r"C:\Windows"
    try:
        win_dir_real = os.path.realpath(win_dir).lower()
    except OSError:
        win_dir_real = win_dir.lower()
    for d in temp_dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            real = os.path.realpath(d)
        except OSError:
            continue
        if real in seen_real:
            continue
        if real.lower().startswith(win_dir_real + os.sep) or real.lower() == win_dir_real:
            log.info(f"skipping system-owned temp dir {real}")
            skipped["system_dir_blocked"] = skipped.get("system_dir_blocked", 0) + 1
            continue
        seen_real.add(real)
        candidates.append(real)

    AGE_DAYS = 7
    SIZE_CAP = 100 * 1024 * 1024
    for tdir in candidates:
        if time.time() > deadline_ts:
            break
        for root, dirs, files in os.walk(tdir, topdown=True):
            if time.time() > deadline_ts:
                break
            # Don't recurse into the directory holding the running
            # GhostShell extraction (_MEI*) — that'd kill us.
            dirs[:] = [d for d in dirs if not d.startswith("_MEI")]
            for fname in files:
                if time.time() > deadline_ts:
                    break
                fp = os.path.join(root, fname)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                age_days = (time.time() - st.st_mtime) / 86400.0
                if age_days < AGE_DAYS:
                    skipped["too_young"] = skipped.get("too_young", 0) + 1
                    continue
                if st.st_size > SIZE_CAP:
                    skipped["oversized"] = skipped.get("oversized", 0) + 1
                    continue
                # Cooperative throttle — windows temp can have tens of
                # thousands of files; a tiny sleep keeps CPU near 0.
                time.sleep(0.002)
                try:
                    os.remove(fp)
                except OSError:
                    err_count += 1
                    skipped["locked_or_inuse"] = skipped.get("locked_or_inuse", 0) + 1
                    continue
                bytes_freed += st.st_size
                # Only log paths for the ringbuf; don't keep all (could
                # be thousands).  Cap the actions list at 200 entries.
                if len(actions) < 200:
                    actions.append({
                        "phase": "B-temp", "kind": "windows temp file",
                        "path": fp, "size": st.st_size,
                        "age_days": round(age_days, 1),
                    })
    return {
        "phase":       "B-temp",
        "actions":     actions,
        "files":       sum(1 for _ in actions) + skipped.get("oversized", 0)*0,
        # Real file count from skipped + actions accounting:
        "deleted":     len(actions) + max(0, bytes_freed and 0),
        "bytes_freed": bytes_freed,
        "errors":      err_count,
        "skipped":     skipped,
        "dirs_scanned": candidates,
    }


def _phase_b_recycle_bin(deadline_ts: float, settings: dict) -> dict:
    """Remove Recycle Bin items deleted >30 days ago, each <100 MB.

    Uses Shell.Application COM (the recommended Windows API for the
    Recycle Bin).  PowerShell handles the COM bridge so we don't pull
    in pywin32 just for this.  Failure modes — PowerShell missing or
    COM unavailable — degrade silently to a no-op."""
    if not settings.get("clean_recycle_bin", True):
        return {"phase": "B-recycle", "skipped_setting": True}
    # Conservative date threshold + size cap built into the script
    ps = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cutoff = (Get-Date).AddDays(-30)
$sizeCap = 100MB
$results = @{ removed = 0; bytes = 0; skipped_young = 0; skipped_large = 0; errors = 0 }
try {
    $shell = New-Object -ComObject Shell.Application
    $bin   = $shell.NameSpace(10)        # 0xA = Recycle Bin
    if ($bin -ne $null) {
        $items = @($bin.Items())
        foreach ($it in $items) {
            try {
                $dateDel = $it.ExtendedProperty('System.Recycle.DateDeleted')
                $size    = $it.ExtendedProperty('System.Size')
                if (-not $dateDel) { continue }
                if ($dateDel -gt $cutoff)  { $results.skipped_young += 1; continue }
                if ($size -gt $sizeCap)    { $results.skipped_large += 1; continue }
                Remove-Item -LiteralPath $it.Path -Force -Recurse -ErrorAction Stop
                $results.removed += 1
                $results.bytes   += [int64]$size
            } catch { $results.errors += 1 }
        }
    }
} catch { $results.errors += 1 }
($results | ConvertTo-Json -Compress)
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=120,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        data = json.loads(out) if out.startswith("{") else {}
    except Exception as e:
        log.debug(f"recycle-bin scan failed: {e}")
        return {"phase": "B-recycle", "errors": 1, "removed": 0, "bytes_freed": 0}
    return {
        "phase":      "B-recycle",
        "removed":    int(data.get("removed", 0) or 0),
        "bytes_freed":int(data.get("bytes",   0) or 0),
        "skipped":    {
            "too_young": int(data.get("skipped_young", 0) or 0),
            "oversized": int(data.get("skipped_large", 0) or 0),
        },
        "errors":     int(data.get("errors", 0) or 0),
    }


# ─── Phase C: System health probe (detect-only) ──────────────────────

def _phase_c_health_probe(deadline_ts: float) -> dict:
    """Read-only checks.  Result feeds the UI and the actionable-toast
    decision.  Each sub-check is independently try/except'd."""
    findings: list = []
    notes: list = []

    # ---- Disk space on the system drive ----
    try:
        import shutil
        sys_drive = os.environ.get("SystemDrive", "C:")
        if not sys_drive.endswith(os.sep):
            sys_drive_path = sys_drive + os.sep
        else:
            sys_drive_path = sys_drive
        usage = shutil.disk_usage(sys_drive_path)
        pct_free = (usage.free / usage.total) * 100 if usage.total else 100
        if pct_free < 5:
            findings.append({
                "kind":     "low_disk_space",
                "severity": "warn",
                "summary":  f"System drive {sys_drive} has only {pct_free:.1f}% free",
                "free_gb":  round(usage.free / (1024**3), 1),
                "total_gb": round(usage.total / (1024**3), 1),
            })
        notes.append(f"sys drive {sys_drive}: {pct_free:.1f}% free")
    except Exception as e:
        log.debug(f"disk-space check failed: {e}")
    if time.time() > deadline_ts:
        return {"phase": "C", "findings": findings, "notes": notes, "incomplete": True}

    # ---- Disk SMART status (WMIC) ----
    try:
        r = subprocess.run(
            ["wmic", "diskdrive", "get", "Model,Status", "/format:csv"],
            capture_output=True, text=True, timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        bad = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or "Model" in line or "," not in line:
                continue
            parts = [p.strip() for p in line.split(",")]
            # CSV columns: Node, Model, Status
            if len(parts) >= 3:
                model, status = parts[-2], parts[-1]
                if status and status.upper() not in ("OK", ""):
                    bad.append({"model": model, "status": status})
        if bad:
            findings.append({
                "kind":     "smart_warning",
                "severity": "warn",
                "summary":  f"SMART status not OK on {len(bad)} drive(s)",
                "drives":   bad,
            })
        notes.append(f"smart: {len(bad)} concerning drive(s)")
    except Exception as e:
        log.debug(f"smart check failed: {e}")
    if time.time() > deadline_ts:
        return {"phase": "C", "findings": findings, "notes": notes, "incomplete": True}

    # ---- Top 5 RAM consumers (excluding GhostShell itself) ----
    try:
        import psutil
        rows = []
        my_pid = os.getpid()
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = p.info
                if info["pid"] == my_pid:
                    continue
                if not info.get("memory_info"):
                    continue
                rss = info["memory_info"].rss
                rows.append((info.get("name") or "?", info["pid"], rss))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda r: r[2], reverse=True)
        top5 = [{"name": n, "pid": p, "rss_mb": round(s / (1024**2), 1)}
                for n, p, s in rows[:5]]
        # Flag any single non-game process using >4GB
        big_offenders = [
            t for t in top5
            if t["rss_mb"] > 4096
            and t["name"].lower() not in (
                "ghostshell.exe", "chrome.exe", "msedge.exe", "firefox.exe",
                "obs64.exe", "discord.exe", "spotify.exe",
            )
        ]
        if big_offenders:
            findings.append({
                "kind":     "ram_hog",
                "severity": "warn",
                "summary":  f"{big_offenders[0]['name']} is using "
                            f"{big_offenders[0]['rss_mb']:.0f} MB of RAM",
                "process":  big_offenders[0],
            })
        notes.append(f"top RAM: {top5[0]['name']} {top5[0]['rss_mb']:.0f} MB" if top5 else "no procs")
        # Always include the top-5 list (informational) for the UI
    except Exception as e:
        log.debug(f"ram-hog probe failed: {e}")
        top5 = []
    if time.time() > deadline_ts:
        return {"phase": "C", "findings": findings, "notes": notes,
                "top_ram": top5, "incomplete": True}

    # ---- Recent service crashes (event log) ----
    try:
        # Last 1 day, system log, errors only.  Limit to 100 records.
        ps = (
            "Get-WinEvent -LogName System -MaxEvents 200 -ErrorAction SilentlyContinue "
            "| Where-Object { $_.LevelDisplayName -eq 'Error' "
            "                 -and $_.TimeCreated -gt (Get-Date).AddHours(-24) } "
            "| Group-Object ProviderName "
            "| Sort-Object Count -Descending "
            "| Select-Object -First 5 "
            "| ForEach-Object { '{0}|{1}' -f $_.Count, $_.Name }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        groups = []
        for line in (r.stdout or "").splitlines():
            if "|" not in line: continue
            try:
                count_s, provider = line.split("|", 1)
                groups.append({"provider": provider.strip(),
                               "count": int(count_s.strip())})
            except ValueError: continue
        # Flag any provider with >10 errors in 24h as actionable
        flagged = [g for g in groups if g["count"] > 10]
        if flagged:
            findings.append({
                "kind":     "service_errors",
                "severity": "warn",
                "summary":  f"{flagged[0]['provider']} logged "
                            f"{flagged[0]['count']} errors in 24 h",
                "groups":   flagged,
            })
        notes.append(f"event log: {len(groups)} provider(s) with errors")
    except Exception as e:
        log.debug(f"event-log probe failed: {e}")
    if time.time() > deadline_ts:
        return {"phase": "C", "findings": findings, "notes": notes,
                "top_ram": top5, "incomplete": True}

    return {
        "phase":     "C",
        "findings":  findings,
        "notes":     notes,
        "top_ram":   top5,
    }


# ─── Orchestrator ────────────────────────────────────────────────────

def _set_thread_priority_below_normal() -> None:
    """Drop our own thread's priority so the scan never competes with
    foreground work the user actually cares about."""
    try:
        import ctypes
        THREAD_PRIORITY_BELOW_NORMAL = -1
        ctypes.windll.kernel32.SetThreadPriority(
            ctypes.windll.kernel32.GetCurrentThread(),
            THREAD_PRIORITY_BELOW_NORMAL,
        )
    except Exception:
        pass


def _maybe_toast_findings(findings: list, settings: dict) -> None:
    """When Phase C surfaces something actionable, write the latest
    finding into status so the JS poller can fire a toast.  We don't
    do the toast from Python — keeping all UI in the JS layer means
    the same toast helpers + dedupe apply."""
    if not findings or not settings.get("toast_on_findings", True):
        return
    # Pick the highest-severity finding to surface.  All current
    # severities are 'warn'; if we add 'critical' later this is the
    # right hook.
    pick = findings[0]
    with _state_lock:
        _state["last_findings"] = {
            "ts":       time.time(),
            "summary":  pick.get("summary", "Integrity scan found an issue"),
            "kind":     pick.get("kind", "unknown"),
            "severity": pick.get("severity", "warn"),
        }


def run_once() -> dict:
    """Execute a single scan cycle synchronously.  Used by the manual
    "Run now" endpoint as well as the periodic scheduler."""
    started = time.time()
    deadline = started + MAX_CYCLE_DURATION_SEC
    settings = _load_settings()
    if not settings.get("enabled"):
        return {"ok": False, "skipped": "disabled"}

    with _state_lock:
        if _state["running"]:
            return {"ok": False, "skipped": "already running"}
        _state.update({
            "running":       True,
            "current_phase": "starting",
            "started_at":    started,
            "ended_at":      0.0,
        })
    _write_status_snapshot()

    cycle = {"started_at": started, "phases": []}

    # ── Phase A ──
    if settings.get("clean_self", True):
        with _state_lock: _state["current_phase"] = "A: self-clean"
        try:
            a = _phase_a_self_clean(deadline)
        except Exception as e:
            a = {"phase": "A", "error": str(e)}
            log.warning(f"Phase A raised: {e}")
        cycle["phases"].append(a)

        # ── Phase A2: large driver-installer cache (>30d) ──
        if time.time() < deadline:
            with _state_lock: _state["current_phase"] = "A2: driver cache"
            try:
                a2 = _phase_a2_prune_driver_cache(deadline)
            except Exception as e:
                a2 = {"phase": "A2-drivers", "error": str(e)}
                log.warning(f"Phase A2 raised: {e}")
            cycle["phases"].append(a2)

    # ── Phase B ──
    if time.time() < deadline and (
        settings.get("clean_windows_temp", True)
        or settings.get("clean_recycle_bin", True)
    ):
        with _state_lock: _state["current_phase"] = "B-temp: Windows temp"
        try:
            b_temp = _phase_b_temp_cleanup(deadline, settings)
        except Exception as e:
            b_temp = {"phase": "B-temp", "error": str(e)}
            log.warning(f"Phase B-temp raised: {e}")
        cycle["phases"].append(b_temp)

        if time.time() < deadline:
            with _state_lock: _state["current_phase"] = "B-recycle: Recycle Bin"
            try:
                b_rb = _phase_b_recycle_bin(deadline, settings)
            except Exception as e:
                b_rb = {"phase": "B-recycle", "error": str(e)}
                log.warning(f"Phase B-recycle raised: {e}")
            cycle["phases"].append(b_rb)

    # ── Phase C ──
    if time.time() < deadline:
        with _state_lock: _state["current_phase"] = "C: health probe"
        try:
            c = _phase_c_health_probe(deadline)
        except Exception as e:
            c = {"phase": "C", "error": str(e)}
            log.warning(f"Phase C raised: {e}")
        cycle["phases"].append(c)
        _maybe_toast_findings(c.get("findings", []) or [], settings)

    ended = time.time()
    cycle["ended_at"] = ended
    cycle["duration_sec"] = round(ended - started, 1)
    # Aggregate totals for the log line
    files_deleted = sum(
        p.get("files", 0) or p.get("deleted", 0) or p.get("removed", 0)
        for p in cycle["phases"]
    )
    bytes_freed = sum(p.get("bytes_freed", 0) for p in cycle["phases"])
    findings_count = sum(len(p.get("findings", []) or []) for p in cycle["phases"])
    cycle["files_deleted"]  = files_deleted
    cycle["bytes_freed"]    = bytes_freed
    cycle["findings_count"] = findings_count
    cycle["mb_freed"]       = round(bytes_freed / (1024**2), 2)
    log.info(
        f"integrity scan complete in {cycle['duration_sec']:.1f}s — "
        f"deleted {files_deleted} files freeing {cycle['mb_freed']:.1f} MB, "
        f"{findings_count} actionable finding(s)"
    )

    # 3.4.3 — trim the per-file `actions` lists before persisting.  Phase
    # B can delete thousands of temp files; the full path list bloated
    # integrity_scan_status.json to 34 KB+ and the ring-buffer history
    # by the same per cycle.  We keep aggregate counts + a small sample
    # (first 5 paths) for context, not the whole list.
    _trim_cycle_for_persist(cycle)

    _append_history(cycle)
    settings["last_run_ts"] = ended
    _save_settings(settings)
    with _state_lock:
        _state["running"]       = False
        _state["current_phase"] = None
        _state["ended_at"]      = ended
        _state["last_cycle"]    = cycle
        _state["next_run_at"]   = ended + settings.get("interval_sec",
                                                       DEFAULT_INTERVAL_SEC)
    _write_status_snapshot()
    return {"ok": True, "cycle": cycle}


def _scheduler_loop(stop: threading.Event) -> None:
    """Long-lived thread.  Wakes at the cadence interval, checks gating,
    runs a cycle if safe, otherwise defers GATE_RECHECK_SEC."""
    _set_thread_priority_below_normal()
    log.info("integrity-scan scheduler started")
    while not stop.is_set():
        try:
            settings = _load_settings()
            if not settings.get("enabled"):
                # Re-check every 5 minutes whether the user re-enabled
                if stop.wait(GATE_RECHECK_SEC): return
                continue
            interval = float(settings.get("interval_sec", DEFAULT_INTERVAL_SEC))
            last_run = float(settings.get("last_run_ts") or 0.0)
            time_until_next = max(0.0, (last_run + interval) - time.time())
            with _state_lock:
                _state["next_run_at"] = last_run + interval
            if time_until_next > 0:
                if stop.wait(time_until_next): return
                continue
            # Time's up — check gating
            ok, reason = _safe_to_run()
            if not ok:
                log.debug(f"integrity scan deferred: {reason}")
                if stop.wait(GATE_RECHECK_SEC): return
                continue
            try:
                run_once()
            except Exception as e:
                log.warning(f"integrity scan cycle raised: {e}")
                # Mark this attempt as "ran" so we don't tight-loop on a
                # persistent crash; the next cycle still waits interval.
                settings = _load_settings()
                settings["last_run_ts"] = time.time()
                _save_settings(settings)
        except Exception as e:
            # Anything unexpected outside the run_once() call itself (a bad
            # settings read, a gating-check bug, ...) must not silently kill
            # this thread — that would permanently disable integrity scanning
            # for the rest of the session with no user-visible indication.
            log.warning(f"integrity scan scheduler iteration raised: {e}")
            if stop.wait(GATE_RECHECK_SEC): return


def start() -> dict:
    """Spin up the scheduler thread.  Idempotent."""
    global _scan_thread, _scan_stop
    if _scan_thread and _scan_thread.is_alive():
        return {"ok": True, "already_running": True}
    _scan_stop = threading.Event()
    _scan_thread = threading.Thread(
        target=_scheduler_loop, args=(_scan_stop,),
        daemon=True, name="integrity-scan-scheduler",
    )
    _scan_thread.start()
    return {"ok": True, "started": True}


def stop() -> dict:
    if _scan_stop:
        _scan_stop.set()
    return {"ok": True, "stopping": True}
