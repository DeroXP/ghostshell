"""Frame telemetry — FPS / frametime / 1%-low from running games.

GhostShell needs real frame timing to score whether an OC actually
helps the game's presentation layer.  Two sources are supported, in
order of preference:

────────────────────────────────────────────────────────────────────
Source 1 — PresentMon (PRIMARY, recommended)
────────────────────────────────────────────────────────────────────
Microsoft's official frame-timing tool.  Single ~600 KB CLI (no
service install needed for v1.x).  Uses Windows ETW directly — no
third-party tool needs to be running in the background.  GhostShell
spawns PresentMon as a child process when AT engages on a game and
kills it when AT disengages.  The user never sees a console window
(spawned with CREATE_NO_WINDOW).

PresentMon writes a CSV with one row per Present() call from the
game.  We tail the CSV at 1 Hz and append frametime samples to the
ring buffer.

PresentMon is BSD-licensed and auto-downloaded on first use from
Microsoft's GitHub release page to:
    %APPDATA%/GhostShell/bin/PresentMon.exe
The download is opt-in: nothing happens until the user clicks the
"Enable frame telemetry" button in the AT diagnostic panel.

────────────────────────────────────────────────────────────────────
Source 2 — RTSS shared memory (FALLBACK)
────────────────────────────────────────────────────────────────────
RivaTuner Statistics Server (bundled with MSI Afterburner) exposes
per-PID FPS + frametime via a named file mapping.  If the user
already has Afterburner running we use it for free — but we never
require it.

────────────────────────────────────────────────────────────────────
Public API (unchanged from v3.2.0-beta.1)
────────────────────────────────────────────────────────────────────
    is_available()        → {available, source, reason, sources_tried}
    install_presentmon()  → download + install in background
    install_progress()    → {state, downloaded, total, err}
    start_tracking(pid)   → begin tracking a process (idempotent)
    stop_tracking()       → stop tracking
    get_recent_stats(sec) → {avg_fps, min_fps_1pct, frametime_*, source}
    get_live_snapshot()   → instantaneous {fps, frametime_ms, source}
    get_status()          → full status dump for diagnostics panel

All errors are caught — telemetry is advisory.  If no source is
available, AT degrades to GPU-only scoring (the historic v3.1
behaviour) and the UI shows a download button.
"""
from __future__ import annotations

import ctypes
import csv as csv_mod
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("frame_telemetry")


# ─── PresentMon paths + download URL ──────────────────────────────────

# PresentMon v1.10.0 is the last release of the v1.x branch — standalone
# CLI, no service needed.  v2.x requires installing PresentMonService
# which the user explicitly does NOT want running in the background.
PRESENTMON_VERSION   = "1.10.0"
PRESENTMON_URL       = (
    "https://github.com/GameTechDev/PresentMon/releases/download/"
    f"v{PRESENTMON_VERSION}/PresentMon-{PRESENTMON_VERSION}-x64.exe"
)
PRESENTMON_BIN_DIR   = os.path.join(APPDATA_DIR, "bin")
PRESENTMON_BIN_PATH  = os.path.join(PRESENTMON_BIN_DIR, "PresentMon.exe")
# We also look in the PyInstaller bundle path so future builds can ship
# PresentMon directly if we ever choose to (currently we don't).
_BUNDLED_PATHS = [
    PRESENTMON_BIN_PATH,
    os.path.join(getattr(sys, "_MEIPASS", ""), "PresentMon.exe") if hasattr(sys, "_MEIPASS") else "",
    os.path.join(os.path.dirname(sys.executable), "PresentMon.exe"),
    os.path.join(os.path.dirname(sys.executable), "bin", "PresentMon.exe"),
]


def _find_presentmon_path() -> Optional[str]:
    """Look in the well-known locations.  Returns the first existing
    PresentMon binary path, or None if not installed."""
    for p in _BUNDLED_PATHS:
        if p and os.path.isfile(p):
            return p
    return None


# ─── PresentMon 2.x detection (Microsoft's service-based version) ─────
# 2.x ships only via MSI installer (~120 MB).  Bundles a Windows service
# named "Intel PresentMon Service" that owns the ETW sessions; the CLI
# (PresentMon-x.x.x-x64.exe) is just a client that talks to the service.
# Crucially, 2.x enables the Microsoft-Windows-OpenGL-CodepathProvider
# and Vulkan providers — capturing presents from games PresentMon 1.x
# can't see (Minecraft Java, older Source-engine games, etc).

PM2_SERVICE_NAMES = (
    "Intel PresentMon Service",
    "InspectionMonitor",   # alt name used in some builds
    "PresentMonService",
)
PM2_INSTALL_HINTS = [
    r"C:\Program Files\Intel\PresentMon",
    r"C:\Program Files (x86)\Intel\PresentMon",
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Intel", "PresentMon"),
]
PM2_RELEASE_URL = "https://github.com/GameTechDev/PresentMon/releases/latest"
RTSS_DOWNLOAD_URL = "https://www.guru3d.com/files-details/rtss-rivatuner-statistics-server-download.html"


def _detect_pm2_service() -> dict:
    """Probe Windows services for an installed PresentMon 2.x service.
    Returns {installed, running, name} — empty/falsy when not present."""
    try:
        import subprocess as _sp
        for name in PM2_SERVICE_NAMES:
            try:
                r = _sp.run(
                    ["sc", "query", name],
                    capture_output=True, text=True,
                    creationflags=_CREATE_NO_WINDOW if _CREATE_NO_WINDOW else 0,
                    timeout=3,
                )
                out = (r.stdout or "") + (r.stderr or "")
                if r.returncode == 0 and "STATE" in out:
                    running = "RUNNING" in out
                    return {"installed": True, "running": running, "name": name}
            except Exception: continue
    except Exception:
        pass
    return {"installed": False, "running": False, "name": None}


def _find_pm2_cli() -> Optional[str]:
    """Locate the PresentMon 2.x CLI binary.

    v3.2-beta.8 — the v2.3.0 MSI lays files out in subdirectories
    under Program Files\\Intel\\PresentMon\\:
        Service\\PresentMonService.exe
        PresentMonApplication\\PresentMon.exe          (GUI app — not us)
        PresentMonConsoleApplication\\PresentMon-2.3.0-x64.exe   (CLI we want)
    The earlier code only looked in the root directory and missed the
    CLI.  Now we walk all subdirs AND consult `sc qc` for the service's
    binary path to anchor the search."""

    # ── 1. Look at the service's binPath and walk up to the install root ──
    try:
        import subprocess as _sp
        for svc_name in PM2_SERVICE_NAMES:
            try:
                r = _sp.run(
                    ["sc", "qc", svc_name],
                    capture_output=True, text=True,
                    creationflags=_CREATE_NO_WINDOW if _CREATE_NO_WINDOW else 0,
                    timeout=4,
                )
            except Exception: continue
            if r.returncode != 0 or "BINARY_PATH_NAME" not in (r.stdout or ""):
                continue
            for line in (r.stdout or "").splitlines():
                if "BINARY_PATH_NAME" in line:
                    # Format: '  BINARY_PATH_NAME   : "C:\\...\\PresentMonService.exe"'
                    after = line.split(":", 1)[-1].strip().strip('"').strip("'")
                    if after and os.path.isfile(after):
                        # walk up from <root>/Service/PresentMonService.exe
                        root = os.path.dirname(os.path.dirname(after))
                        cli = _walk_for_pm2_cli(root)
                        if cli:
                            return cli
            break

    except Exception as e:
        log.debug(f"sc qc walk failed: {e}")

    # ── 2. Fall back to the hinted hard-coded paths (recursive) ──
    for base in PM2_INSTALL_HINTS:
        if not base or not os.path.isdir(base):
            continue
        cli = _walk_for_pm2_cli(base)
        if cli:
            return cli
    return None


def _walk_for_pm2_cli(root: str, max_depth: int = 3) -> Optional[str]:
    """Recursively search `root` (up to max_depth levels) for the PM2
    CLI.  The CLI's filename is `PresentMon-<version>-x64.exe`.  We
    exclude the GUI application (`PresentMon.exe` without a version) and
    the service binary."""
    try:
        for entry in os.scandir(root):
            if entry.is_file():
                lo = entry.name.lower()
                # CLI is versioned: PresentMon-2.3.0-x64.exe
                # GUI app is unversioned: PresentMon.exe (skip it)
                # Service is PresentMonService.exe (skip it)
                if (lo.startswith("presentmon-") and lo.endswith("-x64.exe")
                    and "service" not in lo):
                    return entry.path
            elif entry.is_dir() and max_depth > 0:
                # Skip obviously irrelevant subdirs
                if entry.name.startswith("."):
                    continue
                sub = _walk_for_pm2_cli(entry.path, max_depth - 1)
                if sub:
                    return sub
    except (PermissionError, OSError) as e:
        log.debug(f"_walk_for_pm2_cli({root}): {e}")
    return None


def _start_pm2_service() -> bool:
    """Start the PresentMon 2.x service if it's installed but stopped.
    Returns True on success or if already running.  Safe to call when
    not installed (returns False)."""
    svc = _detect_pm2_service()
    if not svc.get("installed"):
        return False
    if svc.get("running"):
        return True
    try:
        import subprocess as _sp
        r = _sp.run(
            ["sc", "start", svc["name"]],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW if _CREATE_NO_WINDOW else 0,
            timeout=10,
        )
        if r.returncode != 0:
            log.debug(f"sc start failed: {r.stdout} {r.stderr}")
            return False
        # Wait briefly for service to enter RUNNING state
        for _ in range(10):
            time.sleep(0.5)
            if _detect_pm2_service().get("running"):
                return True
        return False
    except Exception as e:
        log.debug(f"start_pm2_service failed: {e}")
        return False


# ─── RTSS shared memory layout (fallback source) ──────────────────────

RTSS_SIGNATURE_RTSS = 0x53535452       # 'RTSS' little-endian
RTSS_SIGNATURE_SSPM = 0x4D505353       # 'SSPM' little-endian
RTSS_MAPPING_NAME   = "RTSSSharedMemoryV2"
FILE_MAP_READ       = 0x0004
_OFF_PROCESS_ID     = 0
_OFF_NAME           = 4
_NAME_LEN           = 260
_OFF_FRAMETIME_US   = 540
_OFF_STAT_FR_MIN    = 564     # 0.1 fps units
_OFF_STAT_FR_AVG    = 568
_OFF_STAT_FR_MAX    = 572


# ─── State ────────────────────────────────────────────────────────────

_state = {
    "running":       False,
    "thread":        None,
    "stop_event":    None,
    "tracked_pid":   None,
    "tracked_name":  None,
    "started_at":    0.0,
    "last_err":      None,
    "source":        None,    # "presentmon" / "rtss" / None
    # PresentMon subprocess + CSV state
    "pm_proc":       None,
    "pm_csv_path":   None,
    "pm_csv_pos":    0,
    "pm_csv_header": None,
    # Download progress
    "install_state": "idle",  # idle | downloading | installed | error
    "install_dl":    0,
    "install_total": 0,
    "install_err":   None,
}
_state_lock = threading.Lock()


def _is_presentmon(src) -> bool:
    """True for any PresentMon source variant ("presentmon" = 1.x, "presentmon2" = 2.x).
    All CSV-tailing / FPS-derivation paths must accept BOTH — early builds only
    matched "presentmon", so the PM2 path silently sampled nothing."""
    return bool(src) and str(src).startswith("presentmon")


_samples = deque(maxlen=600)
_samples_lock = threading.Lock()


# ─── Install / download ───────────────────────────────────────────────

def install_presentmon() -> dict:
    """Download PresentMon from Microsoft's GitHub release to
    %APPDATA%/GhostShell/bin/PresentMon.exe.  Non-blocking — kicks off
    a background thread; poll install_progress() for status."""
    with _state_lock:
        if _state["install_state"] == "downloading":
            return {"ok": False, "err": "install already in progress"}
        if _find_presentmon_path():
            _state["install_state"] = "installed"
            return {"ok": True, "already_installed": True,
                    "path": _find_presentmon_path()}
        _state["install_state"] = "downloading"
        _state["install_dl"]    = 0
        _state["install_total"] = 0
        _state["install_err"]   = None

    threading.Thread(
        target=_download_presentmon_worker,
        daemon=True,
        name="frame-telemetry-install",
    ).start()
    return {"ok": True, "started": True, "url": PRESENTMON_URL}


def install_progress() -> dict:
    """Status of the in-flight (or last) PresentMon install."""
    with _state_lock:
        return {
            "state":      _state["install_state"],
            "downloaded": _state["install_dl"],
            "total":      _state["install_total"],
            "err":        _state["install_err"],
            "path":       _find_presentmon_path(),
        }


def _download_presentmon_worker() -> None:
    """Background HTTP GET → save to bin path."""
    try:
        os.makedirs(PRESENTMON_BIN_DIR, exist_ok=True)
        import urllib.request
        log.info(f"Downloading PresentMon from {PRESENTMON_URL}")
        req = urllib.request.Request(
            PRESENTMON_URL,
            headers={"User-Agent": "GhostShell-FrameTelemetry/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            with _state_lock:
                _state["install_total"] = total
            tmp = PRESENTMON_BIN_PATH + ".part"
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with _state_lock:
                        _state["install_dl"] = downloaded
            # Sanity check — PresentMon-1.10.0-x64.exe is ~660 KB.
            # Bail if we got something obviously wrong (e.g. a GitHub
            # 404 HTML page).
            sz = os.path.getsize(tmp)
            if sz < 100 * 1024 or sz > 5 * 1024 * 1024:
                os.remove(tmp)
                raise RuntimeError(
                    f"downloaded file is {sz} bytes, expected ~600 KB. "
                    f"Microsoft may have moved the release URL."
                )
            os.replace(tmp, PRESENTMON_BIN_PATH)
        with _state_lock:
            _state["install_state"] = "installed"
        log.info(f"PresentMon installed at {PRESENTMON_BIN_PATH}")
    except Exception as e:
        with _state_lock:
            _state["install_state"] = "error"
            _state["install_err"]   = str(e)
        log.warning(f"PresentMon install failed: {e}")


# ─── PresentMon subprocess management ─────────────────────────────────

# PresentMon 1.x flags we care about:
#   -process_id <pid>             scope to one game
#   -no_top                       no live console refresh
#   -terminate_after_timed        exit after -timed seconds
#   -timed <sec>                  duration cap (we use 1h, restart if needed)
#   -output_file <path>           where to write CSV
#   -stop_existing_session        kill any prior PresentMon ETW session
#   -no_console_stats             suppress stdout stats output
#   -terminate_existing           terminate any prior PresentMon instance

PRESENTMON_TIMED_SEC = 3600          # 1 h per session before restart
_CREATE_NO_WINDOW    = 0x08000000     # subprocess flag


_stray_session_cleanup_done = False


def _cleanup_stray_pm_sessions() -> None:
    """beta.7 — best-effort: stop leaked 'ghostshell_*' PresentMon ETW sessions
    left behind by prior runs / older builds.

    beta.4/5 launched PresentMon with a UNIQUE per-launch --session_name while
    keeping --stop_existing_session (which only stops a same-named session), so
    hard-killed captures leaked OS-level ETW sessions (ghostshell_<pid>_<stamp>)
    that ACCUMULATED across a play session until ETW saturated — "N events were
    lost", empty CSVs, frame capture dead system-wide until a reboot.  beta.6's
    fixed session name stops NEW accumulation, but pre-existing leaks persist
    until stopped.  This clears them on startup so upgrading users self-heal
    without a manual reset or reboot.

    Runs once per process.  Needs elevation (the app runs elevated for GPU OC /
    power-plan control); silently no-ops if `logman` fails or access is denied.
    Safe to stop even our CURRENT fixed-name session — the caller relaunches PM2
    (with --stop_existing_session) immediately after.
    """
    global _stray_session_cleanup_done
    if _stray_session_cleanup_done:
        return
    _stray_session_cleanup_done = True
    try:
        r = subprocess.run(
            ["logman", "query", "-ets"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout:
            return
        stray = []
        for line in r.stdout.splitlines():
            tok = line.split()
            if tok and tok[0].lower().startswith("ghostshell_"):
                stray.append(tok[0])
        for name in stray:
            try:
                subprocess.run(
                    ["logman", "stop", name, "-ets"],
                    capture_output=True, timeout=10,
                    creationflags=_CREATE_NO_WINDOW,
                )
                log.info(f"frame_telemetry: cleared stray PresentMon ETW session {name}")
            except Exception as e:
                log.debug(f"could not stop stray session {name}: {e}")
        if stray:
            log.info(f"frame_telemetry: cleaned {len(stray)} stray PresentMon ETW session(s)")
    except Exception as e:
        log.debug(f"stray PM session cleanup skipped: {e}")


def _start_presentmon(pid: int, prefer_pm2: bool = True) -> Optional[dict]:
    """Spawn a PresentMon subprocess targeting the given PID.

    If PresentMon 2.x's service is installed (and we can start it), we
    spawn the 2.x CLI — it gets OpenGL + Vulkan coverage.  Otherwise we
    fall back to the bundled 1.x binary.

    Returns {"proc": Popen, "csv_path": str, "variant": "pm1"|"pm2"}
    on success, None on failure.
    """
    # beta.7 — clear any leaked ghostshell_* ETW sessions from older builds /
    # prior runs before we start capturing, so accumulated leaks can't saturate
    # ETW and kill frame capture.  Once per process, best-effort.
    _cleanup_stray_pm_sessions()
    # ── PM 2.x preferred path ─────────────────────────────────────
    if prefer_pm2:
        pm2_svc = _detect_pm2_service()
        if pm2_svc.get("installed"):
            cli = _find_pm2_cli()
            if cli:
                # Auto-start the service if it's installed but stopped
                if not pm2_svc.get("running"):
                    _start_pm2_service()
                handle = _start_presentmon2(int(pid), cli)
                if handle:
                    return handle

    # ── PM 1.x fallback (bundled, auto-downloadable) ──────────────
    exe = _find_presentmon_path()
    if not exe:
        return None
    csv_path = os.path.join(
        tempfile.gettempdir(),
        f"ghostshell_pm_{pid}_{int(time.time())}.csv",
    )
    cmd = [
        exe,
        "-process_id", str(int(pid)),
        "-no_top",
        "-no_console_stats",
        "-terminate_existing",
        "-terminate_after_timed",
        "-timed", str(PRESENTMON_TIMED_SEC),
        "-output_file", csv_path,
        "-stop_existing_session",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        log.info(f"PresentMon 1.x started: pid={pid} csv={csv_path}")
        return {"proc": proc, "csv_path": csv_path, "variant": "pm1"}
    except Exception as e:
        log.warning(f"PresentMon 1.x spawn failed: {e}")
        return None


def _start_presentmon2(pid: int, cli_path: str) -> Optional[dict]:
    """Spawn PresentMon 2.x CLI targeting the given PID.  Talks to the
    Microsoft service which owns the ETW sessions.

    PresentMon 2.x supports OpenGL + Vulkan via additional ETW providers
    the 1.x build didn't enable.  This is what gets you frame data for
    Minecraft Java and similar games.

    CLI flags for 2.x differ from 1.x:
      --process_id <pid>
      --output_file <path>
      --timed <sec>
      --terminate_after_timed
      --stop_existing_session       (clear a stale session, THEN capture)
      --no_console_stats            (suppress live stats output)

    3.5.1-beta.2: use --stop_existing_session, NOT --terminate_existing_session.
    They read almost identically but do OPPOSITE things — verified against
    PresentMon 2.3.0's own --help:
       --stop_existing_session      → stop an existing same-named session, then
                                      START a new capture (what we want).
       --terminate_existing_session → stop the existing session then EXIT.
    The old code used --terminate_existing_session, so PM2 printed
    "exits without capturing anything; ignoring all other options" and
    quit immediately — no CSV, zero frame samples, AT silently blind.
    """
    # FIXED, app-private ETW session name — reused every launch, paired with
    # --stop_existing_session below.  Two goals:
    #   1. Don't use PresentMon's DEFAULT name ("PresentMon"), so we never fight
    #      another PresentMon-based tool (CapFrameX / the PM overlay / some
    #      Afterburner setups) for the same session (that collision => error 4201,
    #      no CSV, zero frames).  A custom name sidesteps it.
    #   2. Reclaim OUR OWN session on every (re)launch.  A fixed name makes
    #      --stop_existing_session actually reclaim the previous session each
    #      time, so nothing piles up (see _cleanup_stray_pm_sessions for why a
    #      unique-per-launch name leaked sessions and saturated ETW).
    session_name = "ghostshell_frametelemetry"
    # beta.8 — STREAM via --output_stdout, do NOT write --output_file.  PM 2.x
    # holds its --output_file with an EXCLUSIVE lock while capturing, so the
    # sampler could never open it to tail (every read => "used by another
    # process" / PermissionError => 0 samples the whole session).  A stdout pipe
    # has no lock and delivers rows in real time, so we read + parse them off the
    # pipe in _pm_stdout_reader instead of tailing a file.
    cmd = [
        cli_path,
        "--process_id", str(int(pid)),
        "--output_stdout",
        "--timed", str(PRESENTMON_TIMED_SEC),
        "--terminate_after_timed",
        "--session_name", session_name,
        "--stop_existing_session",
        "--no_console_stats",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        reader_stop = threading.Event()
        reader = threading.Thread(
            target=_pm_stdout_reader, args=(proc, reader_stop),
            daemon=True, name=f"pm2-stdout-reader-{pid}",
        )
        reader.start()
        log.info(f"PresentMon 2.x started (stdout stream): pid={pid} cli={cli_path}")
        return {"proc": proc, "variant": "pm2", "stream": True,
                "reader_stop": reader_stop, "reader_thread": reader}
    except Exception as e:
        log.warning(f"PresentMon 2.x spawn failed: {e}")
        return None


def _pm_stdout_reader(proc, stop_event: threading.Event) -> None:
    """Read PresentMon 2.x CSV rows off its stdout pipe and append per-frame
    frametime samples to the shared ring buffer, in real time.

    This replaces file-tailing for PM 2.x: PM2 locks its --output_file
    exclusively while capturing, so the sampler could never read it.  Blocking
    line reads on the pipe end when PM2 exits (game closed / --timed hit) or
    when stop_event is set (retarget / stop_tracking); the sampler loop then
    restarts a fresh PM2 + reader as needed."""
    idx_pid = idx_ft = idx_name = None
    header_done = False
    added = 0
    try:
        # readline() (not `for line in proc.stdout`) — the file iterator reads
        # ahead in large chunks, which batches/delays lines from a live pipe;
        # readline returns each line as soon as PresentMon flushes it.
        while True:
            if stop_event.is_set():
                break
            line = proc.stdout.readline()
            if line == "":            # EOF — PM2 exited
                break
            line = line.strip()
            if not line or "," not in line:
                continue
            if not header_done:
                cells = [c.strip().lower().lstrip("﻿") for c in line.split(",")]

                def _col(*names, default=None):
                    for nm in names:
                        if nm in cells:
                            return cells.index(nm)
                    return default

                idx_pid  = _col("processid", default=1)
                idx_name = _col("application", default=0)
                # PM 2.3.0 = FrameTime; older = msBetweenPresents.
                idx_ft   = _col("frametime", "msbetweenpresents", default=None)
                header_done = True
                # Lock-free single-key writes (atomic under the GIL).  We must
                # NOT take _state_lock here: start_tracking()/retarget calls
                # _stop_presentmon() (which join()s this thread) WHILE holding
                # _state_lock, so grabbing it here would deadlock/stall the join.
                _state["pm_csv_header"] = line
                _state["pm_stream_rows"] = 0
                if idx_ft is None:
                    log.warning(f"PM2 stdout: no frametime column in header: {line[:140]}")
                continue
            if idx_ft is None:
                continue
            row = line.split(",")
            if len(row) <= max(idx_pid, idx_ft, idx_name):
                continue
            try:
                pid_v = int(row[idx_pid])
                ft_ms = float(row[idx_ft])
                name  = row[idx_name].lower()
            except (ValueError, IndexError):
                continue
            if ft_ms <= 0 or ft_ms > 1000:
                # Skip dropped frames / hiccups beyond 1 s — they poison the
                # variance calc more than they inform.
                continue
            with _samples_lock:
                _samples.append({
                    "ts":           time.time(),
                    "pid":          pid_v,
                    "name":         name,
                    "fps_avg":      0.0,
                    "fps_min":      0.0,
                    "fps_max":      0.0,
                    "frametime_ms": ft_ms,
                })
            added += 1
            if added <= 3 or added % 250 == 0:
                _state["pm_stream_rows"] = added   # lock-free (see header note)
    except Exception as e:
        _state["last_err"] = f"pm2 stdout reader: {e}"
        log.debug(f"PM2 stdout reader ended: {e}")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        _state["pm_stream_rows"] = added
    log.info(f"PM2 stdout reader exited ({added} rows read)")


def _stop_presentmon(handle: Optional[dict]) -> None:
    """Politely terminate the PresentMon subprocess (+ its stdout reader for the
    PM 2.x stream path).  If it doesn't exit within 3 s we hard-kill it."""
    if not handle:
        return
    # Signal the stdout reader (PM 2.x stream) to stop first.
    rs = handle.get("reader_stop")
    if rs:
        try: rs.set()
        except Exception: pass
    proc = handle.get("proc")
    if proc:
        try:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception as e:
            log.debug(f"PresentMon terminate failed: {e}")
    # Join the reader thread now that the pipe is closing (unblocks its read).
    rt = handle.get("reader_thread")
    if rt:
        try: rt.join(timeout=2)
        except Exception: pass
    # Legacy PM 1.x file cleanup (stream path has no csv_path).
    csv_path = handle.get("csv_path")
    if csv_path and os.path.exists(csv_path):
        try: os.remove(csv_path)
        except Exception: pass


def _parse_presentmon_csv(handle: dict) -> int:
    """Tail the PresentMon CSV — read new rows since the last call,
    parse `msBetweenPresents` for each, and append to the shared ring
    buffer.  Returns count of new samples added.

    PresentMon CSV columns (v1.10.0):
      Application, ProcessID, SwapChainAddress, Runtime, SyncInterval,
      PresentFlags, AllowsTearing, PresentMode, WasBatched, DwmNotified,
      Dropped, TimeInSeconds, msInPresentAPI, msBetweenPresents,
      msBetweenDisplayChange, msUntilRenderComplete, msUntilDisplayed
    """
    csv_path = handle.get("csv_path")
    if not csv_path or not os.path.exists(csv_path):
        return 0
    added = 0
    try:
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            # Parse header on first read so the column order is correct
            # regardless of which PresentMon version produced it.
            with _state_lock:
                header = _state.get("pm_csv_header")
                pos    = int(_state.get("pm_csv_pos") or 0)
            if header is None:
                f.seek(0)
                first = f.readline()
                if not first or "," not in first:
                    return 0
                header = [h.strip() for h in first.strip().split(",")]
                pos = f.tell()
                with _state_lock:
                    _state["pm_csv_header"] = header
                    _state["pm_csv_pos"]    = pos
            f.seek(pos)
            # Match columns by NAME, case-insensitively, with NO fixed-index
            # fallback.  PresentMon changed the frame-time column name across
            # versions (verified against the CLIs' actual CSV output):
            #   1.x       : msBetweenPresents  (+ TimeInSeconds, ProcessID, Application)
            #   2.3.0     : FrameTime          (+ CPUStartTime,  ProcessID, Application)
            # The old code looked ONLY for msBetweenPresents and explicitly
            # dropped "FrameTime" believing it was "a myth no PresentMon
            # emits" — but PM 2.3.0's header is literally
            #   Application,ProcessID,...,CPUStartTime,FrameTime,CPUBusy,...
            # so with PM2 (AT's preferred source) idx_ft came back None and
            # every frame was skipped → 0 samples.  Match FrameTime FIRST
            # (current 2.x) then fall back to msBetweenPresents (1.x/older).
            # PM 2.x writes a UTF-8 BOM, so the first header cell is
            # "﻿Application" — strip it so name matching still works.
            hdr_lower = [h.lower().lstrip("﻿") for h in header]

            def _col(*names, default=None):
                for nm in names:
                    if nm.lower() in hdr_lower:
                        return hdr_lower.index(nm.lower())
                return default

            idx_pid  = _col("ProcessID", default=1)
            idx_name = _col("Application", default=0)
            idx_ft   = _col("FrameTime", "msBetweenPresents", default=None)
            # (we use wall-clock time.time() per sample, so the CSV timestamp
            #  column — TimeInSeconds in 1.x / CPUStartTime in 2.x — isn't read)
            if idx_ft is None:
                log.warning(
                    "PresentMon CSV: no frame-time column (FrameTime / "
                    f"msBetweenPresents) in header {header} — skipping"
                )
                return 0

            for row_str in f:
                row = row_str.strip().split(",")
                if len(row) <= max(idx_pid, idx_ft, idx_name):
                    continue
                try:
                    pid   = int(row[idx_pid])
                    ft_ms = float(row[idx_ft])
                    name  = row[idx_name].lower()
                except (ValueError, IndexError):
                    continue
                if ft_ms <= 0 or ft_ms > 1000:
                    # Skip dropped frames / hiccups beyond 1 s — they
                    # poison the variance calc more than they inform.
                    continue
                # PresentMon emits per-frame rows, so each row is one
                # sample.  Use wall time so the recent-stats window
                # math stays consistent with RTSS sampling.
                with _samples_lock:
                    _samples.append({
                        "ts":           time.time(),
                        "pid":          pid,
                        "name":         name,
                        "fps_avg":      0.0,   # computed from frametimes
                        "fps_min":      0.0,
                        "fps_max":      0.0,
                        "frametime_ms": ft_ms,
                    })
                added += 1
            with _state_lock:
                _state["pm_csv_pos"] = f.tell()
    except Exception as e:
        log.debug(f"PresentMon CSV parse failed: {e}")
    return added


# ─── RTSS reader (kept as fallback) ───────────────────────────────────

def _open_rtss_mapping():
    try:
        kernel32 = ctypes.windll.kernel32
        OpenFileMappingW = kernel32.OpenFileMappingW
        OpenFileMappingW.restype  = wintypes.HANDLE
        OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        h = OpenFileMappingW(FILE_MAP_READ, False, RTSS_MAPPING_NAME)
        if not h:
            return None, None
        MapViewOfFile = kernel32.MapViewOfFile
        MapViewOfFile.restype  = wintypes.LPVOID
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        addr = MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not addr:
            ctypes.windll.kernel32.CloseHandle(h)
            return None, None
        return h, addr
    except Exception:
        return None, None


def _close_rtss_mapping(h, addr):
    try:
        if addr: ctypes.windll.kernel32.UnmapViewOfFile(addr)
    except Exception: pass
    try:
        if h: ctypes.windll.kernel32.CloseHandle(h)
    except Exception: pass


def _read_dword(addr_int: int, offset: int) -> int:
    return int(ctypes.c_uint32.from_address(addr_int + offset).value)


def _read_string(addr_int: int, offset: int, max_len: int) -> str:
    buf = (ctypes.c_char * max_len).from_address(addr_int + offset)
    s = bytes(buf).split(b"\x00", 1)[0]
    try: return s.decode("latin-1", errors="ignore")
    except Exception: return ""


def _scan_rtss_entries() -> list:
    h, addr = _open_rtss_mapping()
    if not h or not addr:
        return []
    try:
        base = int(addr)
        sig  = _read_dword(base, 0)
        if sig not in (RTSS_SIGNATURE_RTSS, RTSS_SIGNATURE_SSPM):
            return []
        entry_size = _read_dword(base, 8)
        arr_offset = _read_dword(base, 12)
        arr_size   = _read_dword(base, 16)
        if entry_size < 64 or entry_size > 4096:
            return []
        if arr_size < 1 or arr_size > 256:
            return []
        results = []
        for i in range(arr_size):
            ebase = base + arr_offset + (i * entry_size)
            try: pid = _read_dword(ebase, _OFF_PROCESS_ID)
            except Exception: continue
            if pid == 0: continue
            try:
                name        = _read_string(ebase, _OFF_NAME, _NAME_LEN)
                ft_us       = _read_dword(ebase, _OFF_FRAMETIME_US)
                fr_min_dec  = _read_dword(ebase, _OFF_STAT_FR_MIN)
                fr_avg_dec  = _read_dword(ebase, _OFF_STAT_FR_AVG)
                fr_max_dec  = _read_dword(ebase, _OFF_STAT_FR_MAX)
            except Exception: continue
            if ft_us == 0 and fr_avg_dec == 0: continue
            results.append({
                "pid":          pid,
                "name":         name.lower(),
                "frametime_ms": ft_us / 1000.0,
                "fps_min":      fr_min_dec / 10.0,
                "fps_avg":      fr_avg_dec / 10.0,
                "fps_max":      fr_max_dec / 10.0,
            })
        return results
    except Exception:
        return []
    finally:
        _close_rtss_mapping(h, addr)


def _rtss_mapping_exists() -> bool:
    h, addr = _open_rtss_mapping()
    ok = bool(h and addr)
    _close_rtss_mapping(h, addr)
    return ok


# ─── Public API ───────────────────────────────────────────────────────

def is_available() -> dict:
    """Probe ALL sources.  Reports the best available one + per-source
    status so the UI can show which APIs each option covers.

    v3.3.1-beta.5: RTSS dropped as a frame-telemetry source — only
    PresentMon 1.x and 2.x remain.  Reasons: RTSS's shared-memory
    layout has shifted across Afterburner releases (broke our reader
    several times), the install footprint is bigger than PresentMon
    1.x, and PresentMon 2.x already covers OpenGL/Vulkan natively
    via the Windows service.  The reader functions stay in this
    module as dead code; removing them entirely will follow in a
    cleanup pass.
    """
    pm1_path = _find_presentmon_path()
    pm2_svc  = _detect_pm2_service()
    pm2_cli  = _find_pm2_cli()

    # Build the source matrix for the UI.  Order is preference for
    # automatic selection: pm2 > pm1 > none.
    sources_tried = []
    sources_tried.append({
        "name":     "presentmon2",
        "ok":       bool(pm2_svc.get("installed") and pm2_svc.get("running") and pm2_cli),
        "label":    "PresentMon 2.x (Microsoft)",
        "coverage": "D3D9/10/11/12, DXGI, OpenGL, Vulkan",
        "service_installed": pm2_svc.get("installed", False),
        "service_running":   pm2_svc.get("running",  False),
        "cli_path":          pm2_cli,
        "service_name":      pm2_svc.get("name"),
        "reason":   (
            "" if (pm2_svc.get("installed") and pm2_svc.get("running") and pm2_cli) else
            "service not installed — get the MSI from Microsoft's GitHub release"
            if not pm2_svc.get("installed") else
            "service installed but stopped — AT will start it on engage"
            if pm2_svc.get("installed") and not pm2_svc.get("running") else
            "service running but CLI not found in standard install paths"
        ),
        "install_url": PM2_RELEASE_URL,
        "size":     "~122 MB MSI",
    })
    sources_tried.append({
        "name":     "presentmon",
        "ok":       bool(pm1_path),
        "label":    "PresentMon 1.x (bundled, auto-downloaded)",
        "coverage": "D3D9/10/11/12, DXGI (no OpenGL/Vulkan)",
        "path":     pm1_path,
        "reason":   ("" if pm1_path else "not installed — click 'Enable frame telemetry'"),
        "install_url": PRESENTMON_URL,
        "size":     "~380 KB",
    })

    # Pick the best available (v3.3.1-beta.5: RTSS removed — PM only)
    if pm2_svc.get("installed") and pm2_cli:
        return {
            "available":     True,
            "source":        "presentmon2",
            "label":         "PresentMon 2.x (Microsoft)",
            "coverage":      "D3D9/10/11/12, DXGI, OpenGL, Vulkan",
            "path":          pm2_cli,
            "service":       pm2_svc,
            "reason":        ("" if pm2_svc.get("running") else "service stopped — will auto-start"),
            "sources_tried": sources_tried,
        }
    if pm1_path:
        return {
            "available":     True,
            "source":        "presentmon",
            "label":         "PresentMon 1.x (Microsoft, bundled)",
            "coverage":      "D3D9/10/11/12, DXGI",
            "path":          pm1_path,
            "version":       PRESENTMON_VERSION,
            "reason":        "",
            "sources_tried": sources_tried,
        }
    return {
        "available":     False,
        "source":        None,
        "reason":        (
            "No frame-timing source available.  Two options: "
            "(1) Click 'Enable frame telemetry' for PresentMon 1.x — "
            "~380 KB auto-download, covers all D3D9-12 / DXGI games "
            "(no OpenGL/Vulkan).  "
            "(2) Install PresentMon 2.x (Microsoft MSI, ~122 MB) for "
            "first-party OpenGL/Vulkan support via a Windows service."
        ),
        "sources_tried":   sources_tried,
        "install_url":     PRESENTMON_URL,
    }


def start_tracking(pid: Optional[int] = None,
                   exe_name: Optional[str] = None) -> dict:
    """Begin tracking a specific game's frame stats.  Auto-selects the
    best available source (PresentMon 2 → PresentMon 1 → none).

    v3.3.1-beta.5: RTSS source removed.  If no PresentMon is
    available the tracker still starts (so the AT classifier can
    operate from GPU-util signals alone), but no frame data flows."""
    with _state_lock:
        _state["tracked_pid"]  = int(pid) if pid else None
        _state["tracked_name"] = (exe_name or "").lower() or None
        if _state["running"]:
            log.info(f"frame_telemetry retargeted: pid={pid} exe={exe_name}")
            # Restart presentmon if we were using it (different PID = new instance)
            old = _state.get("pm_proc")
            if old:
                _stop_presentmon(old)
                _state["pm_proc"]       = None
                _state["pm_csv_path"]   = None
                _state["pm_csv_pos"]    = 0
                _state["pm_csv_header"] = None

        ev = threading.Event()
        _state["running"]    = True
        _state["stop_event"] = ev
        _state["started_at"] = time.time()
        _state["last_err"]   = None

        # Decide source.  v3.3.1-beta.5: RTSS path removed; PresentMon
        # is the only frame-data source now.  When PresentMon isn't
        # available or we don't have a PID, source stays None and the
        # AT classifier falls back to GPU-util signals only.
        pm2_svc = _detect_pm2_service()
        pm2_cli = _find_pm2_cli()
        have_pm2 = bool(pm2_svc.get("installed") and pm2_cli)
        have_pm1 = bool(_find_presentmon_path())

        if (have_pm2 or have_pm1) and pid:
            handle = _start_presentmon(int(pid), prefer_pm2=have_pm2)
            if handle:
                _state["pm_proc"]     = handle
                _state["pm_csv_path"] = handle.get("csv_path")  # None for PM2 stream
                _state["pm_csv_pos"]  = 0
                _state["pm_csv_header"] = None
                _state["pm_stream_rows"] = 0
                _state["source"]      = ("presentmon2" if handle.get("variant") == "pm2"
                                          else "presentmon")
            else:
                _state["source"] = None
        else:
            _state["source"] = None

        t = threading.Thread(
            target=_sampler_loop,
            args=(ev,),
            daemon=True,
            name="frame-telemetry",
        )
        _state["thread"] = t
        t.start()
    with _samples_lock:
        _samples.clear()
    log.info(f"frame_telemetry started: pid={pid} exe={exe_name} source={_state.get('source')}")
    return {"ok": True, "started": True, "source": _state.get("source")}


def stop_tracking() -> dict:
    """Stop the sampler + PresentMon subprocess (if any).  Buffer is
    preserved so the UI can render the last session's stats even after
    the game closes."""
    with _state_lock:
        if not _state["running"]:
            return {"ok": True, "was_running": False}
        ev = _state["stop_event"]
        _state["running"] = False
        pm = _state.get("pm_proc")
        _state["pm_proc"]       = None
        _state["pm_csv_path"]   = None
        _state["pm_csv_pos"]    = 0
        _state["pm_csv_header"] = None
    try:
        if ev: ev.set()
    except Exception: pass
    _stop_presentmon(pm)
    log.info("frame_telemetry stopped")
    return {"ok": True, "was_running": True}


def get_recent_stats(seconds: int = 30) -> dict:
    """Aggregate stats over the last `seconds` of samples."""
    cutoff = time.time() - max(1, seconds)
    with _samples_lock:
        recent = [s for s in _samples if s["ts"] >= cutoff]

    src = _state.get("source")

    if not recent:
        return {
            "available":          False,
            "sample_count":       0,
            "source":             src,
            "age_sec":            None,
            "avg_fps":            None,
            "min_fps_1pct":       None,
            "max_fps":            None,
            "rtss_min_fps":       None,
            "frametime_avg_ms":   None,
            "frametime_std_ms":   None,
            "frametime_p99_ms":   None,
            "frametime_var_pct":  0.0,
        }

    fts  = sorted(s["frametime_ms"] for s in recent if s["frametime_ms"] > 0)
    if not fts:
        return {
            "available":          False,
            "sample_count":       len(recent),
            "source":             src,
            "frametime_var_pct":  0.0,
        }

    avg_ft = sum(fts) / len(fts)
    if len(fts) > 1:
        var = sum((ft - avg_ft) ** 2 for ft in fts) / len(fts)
        std_ft = var ** 0.5
    else:
        std_ft = 0.0
    p99_idx = max(0, int(len(fts) * 0.99) - 1)
    p99_ft  = fts[p99_idx] if p99_idx < len(fts) else fts[-1]
    min_fps_1pct = (1000.0 / p99_ft) if p99_ft > 0 else None

    # FPS: prefer 1000/avg_frametime for PresentMon (more accurate),
    # use RTSS's reported avg for RTSS (it's already smoothed).
    if _is_presentmon(src) or all((s.get("fps_avg") or 0) == 0 for s in recent):
        avg_fps = (1000.0 / avg_ft) if avg_ft > 0 else None
        # max FPS = inverse of fastest (smallest) frametime
        max_fps = (1000.0 / fts[0]) if fts[0] > 0 else None
        rtss_min_fps = None
    else:
        fps_avg_vals = [s["fps_avg"] for s in recent if s["fps_avg"] > 0]
        fps_max_vals = [s["fps_max"] for s in recent if s["fps_max"] > 0]
        fps_min_vals = [s["fps_min"] for s in recent if s["fps_min"] > 0]
        avg_fps = round(sum(fps_avg_vals) / len(fps_avg_vals), 1) if fps_avg_vals else None
        max_fps = round(max(fps_max_vals), 1) if fps_max_vals else None
        rtss_min_fps = round(min(fps_min_vals), 1) if fps_min_vals else None

    latest = recent[-1]
    return {
        "available":          True,
        "sample_count":       len(recent),
        "source":             src,
        "age_sec":            round(time.time() - latest["ts"], 1),
        "avg_fps":            round(avg_fps, 1) if avg_fps else None,
        "min_fps_1pct":       round(min_fps_1pct, 1) if min_fps_1pct else None,
        "max_fps":            round(max_fps, 1) if max_fps else None,
        "rtss_min_fps":       rtss_min_fps,
        "frametime_avg_ms":   round(avg_ft, 2),
        "frametime_std_ms":   round(std_ft, 2),
        "frametime_p99_ms":   round(p99_ft, 2),
        "frametime_var_pct":  round((std_ft / avg_ft * 100), 1) if avg_ft > 0 else 0.0,
    }


def get_live_snapshot() -> dict:
    with _samples_lock:
        if not _samples:
            return {"available": False, "source": _state.get("source")}
        s = _samples[-1]
    src = _state.get("source")
    # For PresentMon, derive instantaneous FPS from the latest frametime
    if _is_presentmon(src) and s["frametime_ms"] > 0:
        fps = 1000.0 / s["frametime_ms"]
    else:
        fps = s.get("fps_avg", 0.0)
    return {
        "available":    True,
        "source":       src,
        "fps_avg":      round(fps, 1) if fps else 0.0,
        "fps_min":      s.get("fps_min"),
        "fps_max":      s.get("fps_max"),
        "frametime_ms": round(s["frametime_ms"], 2),
        "age_sec":      round(time.time() - s["ts"], 1),
        "exe":          s.get("name", ""),
        "pid":          s.get("pid"),
    }


# ─── Sampler loop ─────────────────────────────────────────────────────

def _sampler_loop(stop_event: threading.Event) -> None:
    """Drives whichever source we picked.  PresentMon: tail the CSV at
    1 Hz.  RTSS: poll shared memory at 1 Hz."""
    log.info(f"frame_telemetry sampler started (source={_state.get('source')})")
    presentmon_restart_cooldown = 0.0

    while not stop_event.is_set():
        try:
            src = _state.get("source")
            if _is_presentmon(src):
                with _state_lock:
                    handle = _state.get("pm_proc")
                if handle:
                    # Detect if PresentMon died (hit its -timed cap or
                    # the game closed).  Restart with the same PID.
                    proc = handle.get("proc")
                    if proc and proc.poll() is not None and time.time() > presentmon_restart_cooldown:
                        log.info(f"PresentMon subprocess exited (rc={proc.returncode}) — restarting")
                        with _state_lock:
                            pid = _state.get("tracked_pid")
                            _state["pm_proc"]       = None
                            _state["pm_csv_path"]   = None
                            _state["pm_csv_pos"]    = 0
                            _state["pm_csv_header"] = None
                        # Clean up old CSV first
                        _stop_presentmon(handle)
                        if pid:
                            new_h = _start_presentmon(int(pid))
                            if new_h:
                                with _state_lock:
                                    _state["pm_proc"]     = new_h
                                    _state["pm_csv_path"] = new_h.get("csv_path")
                        presentmon_restart_cooldown = time.time() + 5
                    else:
                        # PM 2.x streams frames via its own stdout reader thread
                        # (_pm_stdout_reader); only the legacy PM 1.x file path
                        # needs tailing from here.
                        if not handle.get("stream"):
                            _parse_presentmon_csv(handle)
            # v3.3.1-beta.5: RTSS sampler branch removed.  When source
            # is None we just don't sample frame data; AT classifier
            # gracefully degrades to GPU-util-only signals.
        except Exception as e:
            with _state_lock:
                _state["last_err"] = str(e)
            log.debug(f"frame_telemetry sampler tick failed: {e}")
        stop_event.wait(1.0)
    log.info("frame_telemetry sampler stopped")


def get_status() -> dict:
    """Full status dump for the diagnostics panel."""
    with _state_lock:
        running     = _state["running"]
        tracked_pid = _state["tracked_pid"]
        tracked_name= _state["tracked_name"]
        started     = _state["started_at"]
        err         = _state["last_err"]
        src         = _state["source"]
        pm_proc     = _state.get("pm_proc")
        pm_csv_path = _state.get("pm_csv_path")
        pm_csv_pos  = _state.get("pm_csv_pos") or 0
        pm_stream_rows = _state.get("pm_stream_rows") or 0
    avail   = is_available()
    recent  = get_recent_stats(30)
    live    = get_live_snapshot()
    install = install_progress()

    # v3.2-beta.5 — PresentMon health.  Until this row landed, the UI
    # only showed "PresentMon connected" based on the binary existing
    # — that doesn't tell you whether the subprocess is actually running
    # OR producing frame data.  For games PresentMon 1.x can't capture
    # (Minecraft Java's OpenGL is the typical case), it'd show
    # "connected" forever while emitting zero frame samples.
    pm_health = None
    if _is_presentmon(src):
        proc      = (pm_proc or {}).get("proc") if pm_proc else None
        alive     = bool(proc and proc.poll() is None) if proc else False
        is_stream = bool((pm_proc or {}).get("stream")) if pm_proc else False
        csv_size  = 0
        try:
            if pm_csv_path and os.path.exists(pm_csv_path):
                csv_size = os.path.getsize(pm_csv_path)
        except Exception: pass
        with _samples_lock:
            buf_count = len(_samples)
        pm_health = {
            "alive":      alive,
            # PM 2.x streams over stdout (no file); PM 1.x tails a CSV.
            "mode":       "stdout-stream" if is_stream else "csv-tail",
            "csv_path":   pm_csv_path,
            "csv_size":   csv_size,
            "csv_read_pos": pm_csv_pos,
            "stream_rows":  pm_stream_rows,
            "buffer_samples": buf_count,
            "rc":         proc.returncode if (proc and not alive) else None,
            # "producing" = subprocess alive AND frames actually reaching the
            # ring buffer.  For the stream path there's no file to size-check,
            # so we rely on the buffer (fed by _pm_stdout_reader); for the
            # legacy file path we also require the CSV to be growing.
            "producing_frames": (alive and buf_count > 0
                                 and (is_stream or csv_size > 1024)),
        }
    return {
        "running":     running,
        "tracked_pid": tracked_pid,
        "tracked_name":tracked_name,
        "started_at":  started,
        "source":      src,
        "available":   avail,
        "recent":      recent,
        "live":        live,
        "install":     install,
        "pm_health":   pm_health,
        "last_err":    err,
    }
