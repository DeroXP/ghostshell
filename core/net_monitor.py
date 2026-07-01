"""Network-burst monitor — per-process bandwidth tracking + spike detection.

The premise: during a competitive game, a background process suddenly
hammers the network (Steam download wakes up, OneDrive syncs, npm install
fires, an update kicks in) — ping spikes, packets drop, the user has no
idea what caused it.  Task Manager doesn't show per-process bytes/sec.

This module fills that gap by running a 1 Hz sampler in the background:

  • Per-process delta-bytes from psutil's `Process.io_counters().other_bytes`,
    which on Windows includes network + IPC.  For real network hogs
    (browsers downloading, Steam syncing, OneDrive sync) this dominates
    so cleanly that "other_bytes/sec" is effectively per-process network.
  • Total NIC throughput from psutil's `net_io_counters()` (interface-level).
  • 60-sample ring buffer so the UI can show a sparkline.

Burst detection: when a game is active (per `game_profiles`), any OTHER
process exceeding `BURST_BYTES_PER_SEC` for `BURST_CONSECUTIVE` consecutive
seconds is flagged.  The UI surfaces the top offender with a one-click
pause action that reuses the v3.1 `background_pauser` ctypes suspend.

Public API:
    start() / stop() / is_running()
    get_live() -> { total_rx_per_sec, total_tx_per_sec, top: [...],
                    burst_alert: {...} | None, last_sampled_at }
    get_history(seconds=60) -> [{ts, total_rx, total_tx}, ...]
    get_top_processes(limit=10) -> [...]
"""

from __future__ import annotations
import threading
import time
from collections import deque
from typing import Optional

from core.utils import get_logger

log = get_logger(__name__)

# ────────────────────────────────────────────────────────────────────
# Tuning
# ────────────────────────────────────────────────────────────────────

SAMPLE_INTERVAL = 1.0      # seconds between samples
HISTORY_LEN = 60           # ring buffer size (60s)

# A single non-game process exceeding this for BURST_CONSECUTIVE seconds
# in a row triggers a burst alert.  5 MB/s is a comfortable threshold —
# most legit chat/web traffic is well below; OneDrive/Steam/Drive sync
# spikes far above.
BURST_BYTES_PER_SEC = 5 * 1024 * 1024
BURST_CONSECUTIVE   = 3

# Process names that are NEVER flagged (safe-list — these are legitimate
# during gameplay or are GhostShell itself / system services that the
# user can't reasonably pause).
NEVER_FLAG = {
    "ghostshell.exe", "vispora.exe", "python.exe", "pythonw.exe",
    "system", "system idle process",
    "svchost.exe",                    # too generic, false-positives
    "discord.exe", "discordcanary.exe", "discordptb.exe",
    "spotify.exe", "tidal.exe", "apple music.exe",
}


# ────────────────────────────────────────────────────────────────────
# Module state
# ────────────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()

# Ring buffer of total-throughput samples (for sparkline)
_history: deque = deque(maxlen=HISTORY_LEN)

# Per-process running state.  Indexed by PID.  Each row:
#   { name, pid, last_other_bytes, bytes_per_sec, consecutive_high }
_per_proc: dict[int, dict] = {}

# Last NIC counters for delta math
_last_nic_rx = 0
_last_nic_tx = 0
_last_sample_t = 0.0

# Latest computed snapshot (cached for cheap /api/network/live calls)
_latest: dict = {
    "total_rx_per_sec": 0,
    "total_tx_per_sec": 0,
    "top": [],
    "burst_alert": None,
    "last_sampled_at": 0,
}


# ────────────────────────────────────────────────────────────────────
# Sampling loop
# ────────────────────────────────────────────────────────────────────

def _sample_once():
    """One sampling pass.  Updates _per_proc, _history, _latest."""
    global _last_nic_rx, _last_nic_tx, _last_sample_t

    import psutil

    now = time.time()
    dt = max(now - _last_sample_t, 0.001) if _last_sample_t else SAMPLE_INTERVAL

    # ── NIC totals ──────────────────────────────────────────────────
    try:
        nic = psutil.net_io_counters()
        rx_total = int(nic.bytes_recv)
        tx_total = int(nic.bytes_sent)
    except Exception:
        rx_total = tx_total = 0

    rx_per_sec = max(0, int((rx_total - _last_nic_rx) / dt)) if _last_nic_rx else 0
    tx_per_sec = max(0, int((tx_total - _last_nic_tx) / dt)) if _last_nic_tx else 0
    _last_nic_rx = rx_total
    _last_nic_tx = tx_total

    # ── Per-process delta ──────────────────────────────────────────
    seen_pids: set[int] = set()
    burst_candidates: list[dict] = []
    active_game_exe = _get_active_game_exe()

    for p in psutil.process_iter(["pid", "name", "io_counters"]):
        try:
            pid = p.info["pid"]
            name = (p.info.get("name") or "").lower()
            io = p.info.get("io_counters")
            if not io:
                continue
        except Exception:
            continue
        seen_pids.add(pid)
        cur_other = int(io.other_bytes)
        prev = _per_proc.get(pid)
        if prev is None:
            _per_proc[pid] = {
                "pid": pid, "name": name,
                "last_other_bytes": cur_other,
                "bytes_per_sec": 0,
                "consecutive_high": 0,
            }
            continue
        bps = max(0, int((cur_other - prev["last_other_bytes"]) / dt))
        prev["last_other_bytes"] = cur_other
        prev["bytes_per_sec"] = bps
        prev["name"] = name           # refresh in case psutil cached stale

        # Burst tracking — only for non-game, non-safelisted processes
        is_game = active_game_exe and name == active_game_exe
        if (not is_game) and (name not in NEVER_FLAG) and bps >= BURST_BYTES_PER_SEC:
            prev["consecutive_high"] += 1
            if prev["consecutive_high"] >= BURST_CONSECUTIVE:
                burst_candidates.append({
                    "pid": pid, "name": name,
                    "bytes_per_sec": bps,
                    "consecutive_high": prev["consecutive_high"],
                })
        else:
            prev["consecutive_high"] = 0

    # GC processes that exited
    for stale_pid in list(_per_proc.keys()):
        if stale_pid not in seen_pids:
            del _per_proc[stale_pid]

    # ── Pick the loudest burst (if any) ────────────────────────────
    burst_alert = None
    if burst_candidates:
        burst_candidates.sort(key=lambda r: -r["bytes_per_sec"])
        top = burst_candidates[0]
        burst_alert = {
            "pid":           top["pid"],
            "name":          top["name"],
            "bytes_per_sec": top["bytes_per_sec"],
            "mb_per_sec":    round(top["bytes_per_sec"] / (1024 * 1024), 2),
            "duration_sec":  top["consecutive_high"],
            "active_game":   active_game_exe,
        }

    # ── Compute top-N for the live UI ──────────────────────────────
    top_list = [
        {
            "pid": v["pid"],
            "name": v["name"],
            "bytes_per_sec": v["bytes_per_sec"],
            "mb_per_sec":    round(v["bytes_per_sec"] / (1024 * 1024), 3),
            "is_game":       (active_game_exe == v["name"]) if active_game_exe else False,
        }
        for v in _per_proc.values()
        if v["bytes_per_sec"] > 0
    ]
    top_list.sort(key=lambda r: -r["bytes_per_sec"])
    top_list = top_list[:15]

    # ── Update state under lock ────────────────────────────────────
    with _lock:
        _history.append({
            "ts": now,
            "rx": rx_per_sec,
            "tx": tx_per_sec,
        })
        _latest.update({
            "total_rx_per_sec": rx_per_sec,
            "total_tx_per_sec": tx_per_sec,
            "top":              top_list,
            "burst_alert":      burst_alert,
            "last_sampled_at":  now,
            "active_game":      active_game_exe,
        })

    _last_sample_t = now


def _get_active_game_exe() -> Optional[str]:
    """Returns the lowercase exe name of the game that game_profiles says
    is currently running, or None.  Never raises."""
    try:
        from core import game_profiles
        info = game_profiles.get_active_game_info()
        if info and info.get("exe"):
            return info["exe"].lower()
    except Exception:
        pass
    return None


def _loop():
    log.info("net_monitor: sampler thread started")
    while not _stop.is_set():
        try:
            _sample_once()
        except Exception as e:
            log.warning(f"net_monitor sample failed: {e}")
        if _stop.wait(SAMPLE_INTERVAL):
            break
    log.info("net_monitor: sampler thread exiting")


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def start() -> dict:
    global _thread
    if _thread and _thread.is_alive():
        return {"ok": True, "msg": "already running"}
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="net-monitor")
    _thread.start()
    return {"ok": True}


def stop() -> dict:
    _stop.set()
    return {"ok": True}


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def get_live() -> dict:
    """Latest computed snapshot.  Cheap to call repeatedly from the UI."""
    with _lock:
        # Shallow copy to avoid the UI seeing a partial mid-update state
        snap = dict(_latest)
        snap["history_len"] = len(_history)
        snap["running"] = is_running()
    return snap


def get_history(seconds: int = 60) -> list[dict]:
    with _lock:
        rows = list(_history)
    cutoff = time.time() - seconds
    return [r for r in rows if r["ts"] >= cutoff]


def get_top_processes(limit: int = 15) -> list[dict]:
    snap = get_live()
    return (snap.get("top") or [])[:limit]


def pause_process(pid: int) -> dict:
    """Suspend a process via the existing background_pauser ctypes
    helper.  Returns immediately — no rollback timer; the user uses
    /api/pauser/resume_now or the explicit resume button to undo."""
    try:
        from core import background_pauser
        ok = background_pauser._suspend_pid(int(pid))
        if ok:
            log.info(f"net_monitor: paused PID {pid}")
            return {"ok": True, "pid": int(pid)}
        return {"ok": False, "err": f"NtSuspendProcess failed for PID {pid}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def resume_process(pid: int) -> dict:
    try:
        from core import background_pauser
        ok = background_pauser._resume_pid(int(pid))
        if ok:
            return {"ok": True, "pid": int(pid)}
        return {"ok": False, "err": f"NtResumeProcess failed for PID {pid}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}
