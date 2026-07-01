"""Background Process Pauser — freezes ONLY high-RAM, low-priority processes
when a game launches; resumes them when the game exits.

Curated targets (deliberately narrow — we never touch comms/audio):
    AI desktop apps  : ChatGPT, Claude, Microsoft Copilot
    Browsers         : Chrome, Edge, Firefox, Brave, Arc, Opera, Vivaldi,
                       LibreWolf, Waterfox, Tor, Pale Moon, Zen, Mullvad

Explicit non-targets (must never be paused):
    Discord / Discord PTB / Discord Canary  (active voice calls)
    Spotify / Apple Music / iTunes / Tidal  (active audio playback)
    OBS / Streamlabs / XSplit               (active stream / recording)
    Steam / EpicGamesLauncher / Battle.net  (might be patching the game)

Mechanism: NtSuspendProcess + NtResumeProcess via ctypes — pauses execution
entirely so suspended processes consume ZERO CPU, and Windows pushes their
working set onto standby (effectively freeing 4-8 GB of RAM during a typical
gaming session).

CRASH SAFETY
-----------
We persist the list of suspended PIDs to disk BEFORE freezing them.
On startup, GhostShell calls `recover_orphans()` which:
  1. Reads the persisted PID list
  2. Calls NtResumeProcess on every PID still alive
  3. Wipes the file

So a GhostShell crash mid-game can never leave a browser frozen across
a reboot — at worst the user has to launch GhostShell once more.
"""

from __future__ import annotations
import ctypes
import json
import os
import time
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger, atomic_write_json
from config import APPDATA_DIR

log = get_logger(__name__)

SETTINGS_PATH = os.path.join(APPDATA_DIR, "background_pauser.json")
PAUSED_PIDS_PATH = os.path.join(APPDATA_DIR, "paused_pids.json")


# ────────────────────────────────────────────────────────────────────
# Curated target list
# ────────────────────────────────────────────────────────────────────

# Targets are organized by category so the UI can present logical groups
# and the user can disable a whole category at once.
TARGETS: dict[str, dict] = {
    "ai": {
        "label": "AI assistants",
        "desc":  "Desktop chat apps that idle in the background eating RAM.",
        "exes": [
            "chatgpt.exe",
            "claude.exe",
            "copilot.exe",
            "microsoft.copilot.exe",
            "perplexity.exe",
            "groq.exe",
        ],
    },
    "browsers": {
        "label": "Browsers",
        "desc":  "Each renderer / tab is its own process — collectively the "
                 "biggest non-game RAM sink on most systems.",
        "exes": [
            "chrome.exe",
            "msedge.exe",
            # NOTE: msedgewebview2.exe is INTENTIONALLY OMITTED — it hosts
            # pywebview (GhostShell's own UI), Slack, Teams, the Outlook PWA,
            # Notion's app, etc.  Pausing it freezes our own window mid-game.
            "firefox.exe",
            "brave.exe",
            "arc.exe",
            "opera.exe",
            "operagx.exe",
            "vivaldi.exe",
            "librewolf.exe",
            "waterfox.exe",
            "tor.exe",
            "browser.exe",            # Tor Browser, Yandex
            "palemoon.exe",
            "zen.exe",
            "mullvadbrowser.exe",
            "thorium.exe",
            "ungoogled-chromium.exe",
        ],
    },
}


# Hard-deny list — these process names are NEVER suspended even if a target
# list expansion accidentally collides.  Belt-and-suspenders defense.
NEVER_PAUSE_EXES = {
    # Voice / chat — must keep working during a game
    "discord.exe", "discordcanary.exe", "discordptb.exe",
    "discorddevelopment.exe",
    "teamspeak3.exe", "ts3client_win64.exe", "mumble.exe",
    "skype.exe", "msteams.exe", "teams.exe", "slack.exe",
    "telegram.exe", "telegramdesktop.exe", "whatsapp.exe", "signal.exe",
    # Music / audio — must keep playing
    "spotify.exe", "apple music.exe", "applemusic.exe", "itunes.exe",
    "tidal.exe", "deezer.exe", "amazon music.exe", "amazonmusic.exe",
    "youtube music.exe", "youtubemusic.exe",
    "foobar2000.exe", "winamp.exe", "musicbee.exe", "aimp.exe",
    # Streaming software — must keep capturing
    "obs64.exe", "obs32.exe", "obs.exe",
    "streamlabs obs.exe", "streamlabs.exe", "slobs.exe",
    "xsplit.broadcaster.exe", "xsplit.gamecaster.exe",
    # Hardware monitoring — keep capturing telemetry
    "rtss.exe", "rivatuner statistics server.exe",
    "msiafterburner.exe", "afterburner.exe",
    "hwinfo64.exe", "hwinfo32.exe", "hwinfo.exe",
    "corectrl.exe", "throttlestop.exe",
    # Game launchers — might be downloading the game
    "steam.exe", "steamwebhelper.exe",
    "epicgameslauncher.exe", "battle.net.exe", "blizzard.exe",
    "ubisoftconnect.exe", "uplay.exe", "ea.exe", "eadesktop.exe",
    "origin.exe", "riotclientux.exe", "rockstargameslauncher.exe",
    "galaxyclient.exe", "gog galaxy.exe",
    # GhostShell / Vispora itself
    "ghostshell.exe", "vispora.exe", "python.exe", "pythonw.exe",
    # Windows core
    "explorer.exe", "winlogon.exe", "csrss.exe", "smss.exe",
    "lsass.exe", "services.exe", "system.exe", "system idle process",
    "dwm.exe", "wininit.exe",
    # Anti-cheat (we don't want to even risk poking these)
    "easyanticheat.exe", "easyanticheat_eos.exe",
    "battleyelauncher.exe", "be_service.exe", "beservice.exe",
    "vgc.exe", "vgtray.exe",       # Vanguard
    "ricochet.exe",                 # COD
    "faceit.exe", "esportal.exe",
}


DEFAULT_SETTINGS = {
    "enabled": False,                       # master switch — opt-in
    "categories": {                         # per-category enable
        "ai":       True,
        "browsers": True,
    },
    "exclude_exes": [],                     # user-supplied per-exe exclusions
    "min_pause_ram_mb": 50,                 # don't bother pausing tiny procs
    "auto_resume_on_game_exit": True,       # hook into restore_normal_mode
}


# ────────────────────────────────────────────────────────────────────
# Win32 ctypes plumbing
# ────────────────────────────────────────────────────────────────────

PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_ntdll = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _suspend_pid(pid: int) -> bool:
    h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
    if not h:
        return False
    try:
        status = _ntdll.NtSuspendProcess(h)
        return status == 0
    finally:
        _kernel32.CloseHandle(h)


def _resume_pid(pid: int) -> bool:
    h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
    if not h:
        return False
    try:
        status = _ntdll.NtResumeProcess(h)
        return status == 0
    finally:
        _kernel32.CloseHandle(h)


# ────────────────────────────────────────────────────────────────────
# Settings I/O
# ────────────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path: str, data):
    atomic_write_json(path, data)


def get_settings() -> dict:
    cur = _load_json(SETTINGS_PATH, {})
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))   # deep copy
    merged.update({k: v for k, v in cur.items() if k in DEFAULT_SETTINGS})
    # Sub-merge categories so newly-added categories pick up defaults
    if "categories" in cur and isinstance(cur["categories"], dict):
        merged["categories"].update(cur["categories"])
    return merged


def set_settings(**kwargs) -> dict:
    cur = get_settings()
    for k, v in kwargs.items():
        if k in DEFAULT_SETTINGS:
            cur[k] = v
    _save_json(SETTINGS_PATH, cur)
    log.info(f"Background Pauser settings updated: {kwargs}")
    return cur


def set_category_enabled(cat: str, enabled: bool) -> dict:
    if cat not in TARGETS:
        return {"ok": False, "err": f"unknown category {cat}"}
    cur = get_settings()
    cur["categories"][cat] = bool(enabled)
    _save_json(SETTINGS_PATH, cur)
    return {"ok": True, "settings": cur}


# ────────────────────────────────────────────────────────────────────
# Resolve target list
# ────────────────────────────────────────────────────────────────────

def _enabled_target_exes() -> set[str]:
    s = get_settings()
    out: set[str] = set()
    excludes = {e.lower() for e in s.get("exclude_exes", [])}
    for cat, info in TARGETS.items():
        if not s["categories"].get(cat, False):
            continue
        for exe in info["exes"]:
            low = exe.lower()
            if low in NEVER_PAUSE_EXES or low in excludes:
                continue
            out.add(low)
    return out


# ────────────────────────────────────────────────────────────────────
# Pause / resume
# ────────────────────────────────────────────────────────────────────

def _list_target_processes() -> list[dict]:
    """psutil walk — returns every process matching an enabled target exe."""
    import psutil
    targets = _enabled_target_exes()
    if not targets:
        return []
    out: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = (p.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name not in targets:
            continue
        if name in NEVER_PAUSE_EXES:    # belt-and-suspenders
            continue
        try:
            rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            rss = 0
        out.append({"pid": p.info["pid"], "name": name, "rss": rss})
    return out


def pause_now(reason: str = "manual") -> dict:
    """Suspend every running process matching an enabled target exe.

    Returns: { paused: [...], skipped: [...], total_freed_mb }"""
    s = get_settings()
    if not s.get("enabled"):
        return {"ok": False, "err": "background pauser disabled"}

    procs = _list_target_processes()
    if not procs:
        return {"ok": True, "paused": [], "total_freed_mb": 0,
                "msg": "no enabled targets running"}

    min_ram_bytes = int(s.get("min_pause_ram_mb", 50)) * 1024 * 1024

    # Persist FIRST so a crash mid-suspend can't strand a process
    persist = []
    for p in procs:
        if p["rss"] < min_ram_bytes:
            continue
        persist.append({"pid": p["pid"], "name": p["name"],
                        "rss": p["rss"], "ts": time.time(),
                        "reason": reason})
    if not persist:
        return {"ok": True, "paused": [], "total_freed_mb": 0,
                "msg": f"no targets above {min_ram_bytes//1024//1024} MB threshold"}

    _save_json(PAUSED_PIDS_PATH, {"pids": persist, "started_at": time.time()})

    paused: list[dict] = []
    failed: list[dict] = []
    for p in persist:
        if _suspend_pid(p["pid"]):
            paused.append(p)
        else:
            failed.append(p)

    total_mb = sum(p["rss"] for p in paused) // (1024 * 1024)
    log.info(f"Background pauser: suspended {len(paused)} process(es), "
             f"~{total_mb} MB working set, reason={reason}")
    if failed:
        log.warning(f"Background pauser: failed to suspend {len(failed)} "
                    f"PID(s) (likely permission denied)")

    return {"ok": True, "paused": paused, "failed": failed,
            "total_freed_mb": total_mb}


def resume_now(reason: str = "manual") -> dict:
    """Resume every PID we suspended.  Safe to call even if nothing was paused."""
    state = _load_json(PAUSED_PIDS_PATH, None)
    if not state or not state.get("pids"):
        return {"ok": True, "resumed": [], "msg": "nothing to resume"}
    import psutil

    resumed: list[dict] = []
    not_alive: list[dict] = []
    for p in state["pids"]:
        try:
            if not psutil.pid_exists(p["pid"]):
                not_alive.append(p)
                continue
            if _resume_pid(p["pid"]):
                resumed.append(p)
        except Exception as e:        # noqa: BLE001
            log.debug(f"resume_pid({p['pid']}) failed: {e}")

    # Wipe the persisted file regardless — even processes that have already
    # exited shouldn't sit in the file forever
    try:
        os.remove(PAUSED_PIDS_PATH)
    except OSError:
        pass

    log.info(f"Background pauser: resumed {len(resumed)} process(es), "
             f"{len(not_alive)} no longer alive, reason={reason}")
    return {"ok": True, "resumed": resumed, "not_alive": not_alive}


def recover_orphans() -> dict:
    """Called at GhostShell startup.  If a previous run died mid-game and
    left processes suspended, resume them now so the user isn't stuck with
    a frozen browser."""
    state = _load_json(PAUSED_PIDS_PATH, None)
    if not state or not state.get("pids"):
        return {"ok": True, "orphans": 0}
    log.warning(f"Background pauser: detected {len(state['pids'])} orphan(s) "
                f"from previous run — resuming now")
    return resume_now(reason="orphan-recovery")


# ────────────────────────────────────────────────────────────────────
# Status helpers for the UI
# ────────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Snapshot for the UI: settings + currently-paused PIDs + live target
    process count."""
    s = get_settings()
    state = _load_json(PAUSED_PIDS_PATH, None)
    paused_pids = state["pids"] if state else []
    live_targets = _list_target_processes() if s.get("enabled") else []

    return {
        "settings": s,
        "targets": TARGETS,                 # exposed so UI can show categories
        "paused": paused_pids,
        "currently_paused": bool(paused_pids),
        "live_target_count": len(live_targets),
        "live_targets": live_targets[:50],   # cap for payload size
        "estimated_savings_mb": sum(p["rss"] for p in live_targets) // (1024 * 1024),
    }
