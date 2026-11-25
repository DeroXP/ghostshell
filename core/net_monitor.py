"""Network-burst monitor — per-process bandwidth tracking + spike detection.

The premise: during a competitive game, a background process suddenly
hammers the network (Steam download wakes up, OneDrive syncs, npm install
fires, an update kicks in) — ping spikes, packets drop, the user has no
idea what caused it.  Task Manager doesn't show per-process bytes/sec.

This module fills that gap with a 1 Hz sampler that runs WHILE SOMETHING IS
READING IT.  Its only reader is the Network page, which asks every two
seconds while it is on screen.  The sampler starts with the first question
and stops by itself once nobody has asked for IDLE_STOP_S seconds.  It used to
run from the first second of every start until the app quit, whether or not
the page was ever opened: on the owner's PC that was a quarter of a processor
core, all day (docs/performance.md).  What it does while it runs:

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

A burst needs BURST_CONSECUTIVE seconds of samples, so one that is already
going on when the page is opened shows a few seconds later.

Public API:
    start() / stop() / is_running()      (get_live / get_history start it)
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

from config import OWN_PROCESS_NAMES
from core import native
from core.utils import get_logger

log = get_logger(__name__)

# ────────────────────────────────────────────────────────────────────
# Tuning
# ────────────────────────────────────────────────────────────────────

SAMPLE_INTERVAL = 1.0      # seconds between samples
HISTORY_LEN = 60           # ring buffer size (60s)
# With no reader for this long the sampler stops.  The page asks every two
# seconds while it is on screen; this is long enough to look at another page
# and come back to an unbroken graph.
IDLE_STOP_S = 30.0

# A single non-game process exceeding this for BURST_CONSECUTIVE seconds
# in a row triggers a burst alert.  5 MB/s is a comfortable threshold —
# most legit chat/web traffic is well below; OneDrive/Steam/Drive sync
# spikes far above.
BURST_BYTES_PER_SEC = 5 * 1024 * 1024
BURST_CONSECUTIVE   = 3

# Process names that are NEVER flagged (safe-list — these are legitimate
# during gameplay or are Vispora itself / system services that the
# user can't reasonably pause).
NEVER_FLAG = {
    *OWN_PROCESS_NAMES, "python.exe", "pythonw.exe",
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
_run_lock = threading.Lock()   # starting and stopping the sampler, one at a time
_sample_lock = threading.Lock()   # one sampling pass at a time (held by the sampler for each pass)
_last_wanted = 0.0             # when a reader last asked (time.monotonic()); 0: never

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

def _process_io_rows(psutil):
    """(pid, lower-case name, other-I/O bytes so far) of every process.

    From the native helper when it is there: one system call returns the
    counters of all processes at once.  Otherwise from psutil, which opens
    each process in turn; on the owner's PC that one loop, once a second for
    as long as the app was open, was a quarter of a processor core and nine
    tenths of all the processor time the app ever used."""
    rows = native.process_io()
    if rows is not None:
        for r in rows:
            yield r["pid"], r["name"].lower(), r["other_bytes"]
        return
    for p in psutil.process_iter(["pid", "name", "io_counters"]):
        try:
            pid = p.info["pid"]
            name = (p.info.get("name") or "").lower()
            io = p.info.get("io_counters")
            if not io:
                continue
            other = int(io.other_bytes)
        except Exception:
            continue
        yield pid, name, other


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

    for pid, name, cur_other in _process_io_rows(psutil):
        seen_pids.add(pid)
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


def _nobody_is_reading() -> bool:
    return time.monotonic() - _last_wanted > IDLE_STOP_S


def _forget_readings() -> None:
    """Drop what the last run measured.  A run that starts later must not
    work out a rate from counters read minutes ago, nor show the old figures
    as if they were live."""
    global _last_nic_rx, _last_nic_tx, _last_sample_t
    with _sample_lock:          # (a sampler that was just told to stop may still be finishing a pass)
        _per_proc.clear()
        _last_nic_rx = _last_nic_tx = 0
        _last_sample_t = 0.0
        with _lock:
            _latest.update({"total_rx_per_sec": 0, "total_tx_per_sec": 0, "top": [], "burst_alert": None})


def _loop(me: threading.Event):
    """Sample once a second until told to stop (`me`), or until nobody has
    read the result for IDLE_STOP_S seconds."""
    log.info("net_monitor: sampler started")
    why = "stopped"
    while not me.is_set():
        with _sample_lock:
            if me.is_set():
                break
            try:
                _sample_once()
            except Exception as e:
                log.warning(f"net_monitor sample failed: {e}")
        if me.wait(SAMPLE_INTERVAL):
            break
        with _run_lock:
            if _nobody_is_reading() and not me.is_set():
                # Decided with the lock held: a reader that asks now either
                # got in first (and we go on) or starts a new sampler.
                me.set()
                why = f"nobody has read it for {IDLE_STOP_S:.0f} s"
    log.info(f"net_monitor: sampler exiting ({why})")


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def start() -> dict:
    """Start the sampler if it is not running.  It keeps going for as long as
    get_live() or get_history() are called, and stops IDLE_STOP_S seconds
    after the last call."""
    global _thread, _stop, _last_wanted
    with _run_lock:
        _last_wanted = time.monotonic()
        if _thread and _thread.is_alive() and not _stop.is_set():
            return {"ok": True, "msg": "already running"}
        _forget_readings()
        _stop = threading.Event()
        _thread = threading.Thread(target=_loop, args=(_stop,), daemon=True, name="net-monitor")
        _thread.start()
    return {"ok": True}


def stop() -> dict:
    with _run_lock:
        _stop.set()
    return {"ok": True}


def is_running() -> bool:
    return bool(_thread and _thread.is_alive() and not _stop.is_set())


def _wanted() -> None:
    """A reader is asking: note it, and start the sampler if it is not
    running.  Never raises."""
    global _last_wanted
    try:
        _last_wanted = time.monotonic()
        if not is_running():
            start()
    except Exception as e:
        log.debug(f"net_monitor could not be started for a reader: {e}")


def get_live() -> dict:
    """Latest computed snapshot.  Cheap to call repeatedly from the UI.
    Calling it is what keeps the sampler running (see the top of this file):
    the first call after a pause starts it, and answers zeroes until the
    second sample is in, a second later."""
    _wanted()
    with _lock:
        # Shallow copy to avoid the UI seeing a partial mid-update state
        snap = dict(_latest)
        snap["history_len"] = len(_history)
        snap["running"] = is_running()
    return snap


def get_history(seconds: int = 60) -> list[dict]:
    _wanted()
    with _lock:
        rows = list(_history)
    cutoff = time.time() - seconds
    return [r for r in rows if r["ts"] >= cutoff]


def get_top_processes(limit: int = 15) -> list[dict]:
    snap = get_live()
    return (snap.get("top") or [])[:limit]


def pause_process(pid: int) -> dict:
    """Suspend a process via the background pauser, which records it so the
    pauser's "Resume now" (and next-launch recovery) can thaw it again."""
    try:
        from core import background_pauser
        ok = background_pauser.pause_pid(int(pid), reason="network burst")
        if ok:
            log.info(f"net_monitor: paused PID {pid}")
            return {"ok": True, "pid": int(pid)}
        return {"ok": False, "err": f"NtSuspendProcess failed for PID {pid}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def resume_process(pid: int) -> dict:
    try:
        from core import background_pauser
        ok = background_pauser.resume_pid(int(pid))
        if ok:
            return {"ok": True, "pid": int(pid)}
        return {"ok": False, "err": f"NtResumeProcess failed for PID {pid}"}
    except Exception as e:
        return {"ok": False, "err": str(e)}
