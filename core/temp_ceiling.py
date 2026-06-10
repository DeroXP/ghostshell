"""Hard GPU temperature ceiling — emergency revert if temp exceeds limit.

A small background watchdog that polls GPU temperature every second.
When the temperature stays above the user-set ceiling for the
configured sustained duration, we automatically:
  1. Call `gpu_overclock.emergency_reset_oc()` to revert to stock
  2. Append a trip event to the on-disk log
  3. Fire a Windows toast notification

The ceiling is the safety floor underneath every other GPU feature
(manual OC, auto OC, adaptive tuning).  It runs even when no OC is
applied — if the user's GPU is overheating from a hot day or a stuck
fan, a toast notification is still worth firing.

Settings live in `%APPDATA%\\GhostShell\\temp_ceiling_settings.json`.
Trip events live in `%APPDATA%\\GhostShell\\temp_ceiling_trips.json`
(capped at last 50 entries).

Public API:
    set_settings(**kwargs)   → update + save settings
    get_settings()           → current settings
    get_status()             → live status (temp, sustained, trip count)
    get_trips(limit=20)      → recent trip events
    start_watchdog()         → begin polling (idempotent)
    stop_watchdog()          → stop polling
    is_running()             → bool
    test_trip()              → manually fire a trip (for UI testing)
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("temp_ceiling")

SETTINGS_PATH = os.path.join(APPDATA_DIR, "temp_ceiling_settings.json")
TRIP_LOG_PATH = os.path.join(APPDATA_DIR, "temp_ceiling_trips.json")

# Hardware-safety limits — clamp user input to these
MIN_CEILING_C   = 60
MAX_CEILING_C   = 95     # stock NVIDIA TjMax is typically 88–95°C
MIN_SUSTAINED_S = 1
MAX_SUSTAINED_S = 60

# Defaults — conservative.  83°C is below the 87°C stability-test abort
# point already in gpu_overclock.py, so the ceiling fires *first* during
# normal use.
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled":          True,
    "ceiling_c":        83,
    "sustained_seconds": 5,
    "poll_interval_s":  1.0,
    # "reset" → emergency_reset_oc  /  "notify" → toast only, no revert.
    "action":           "reset",
    # Cooldown before the watchdog will fire again after a trip.  Prevents
    # a thrash loop if the GPU is genuinely struggling (e.g. failing fan).
    "cooldown_seconds": 60,
}

# Live state (in memory)
_state: dict[str, Any] = {
    "running":            False,
    "thread":             None,
    "stop_event":         None,
    "current_temp_c":     None,     # last reading
    "above_since":        None,     # epoch when temp first crossed ceiling
    "tripped_count":      0,        # since this app boot
    "last_trip_at":       None,     # epoch
    "last_trip_temp_c":   None,
    "in_cooldown_until":  None,     # epoch — won't trip again until then
}
_lock = threading.Lock()


# ─── Settings persistence ────────────────────────────────────────────────
def _load() -> dict[str, Any]:
    if not os.path.exists(SETTINGS_PATH):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged
    except Exception as e:
        log.warning(f"Could not load temp_ceiling settings ({e}); using defaults")
        return dict(DEFAULT_SETTINGS)


def _save(settings: dict[str, Any]) -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save temp_ceiling settings: {e}")


def get_settings() -> dict[str, Any]:
    return _load()


def set_settings(**kwargs) -> dict[str, Any]:
    """Update one or more settings and persist them.  Returns the merged
    config back to the caller so the UI can refresh.  Unknown keys are
    silently ignored."""
    cur = _load()
    if "enabled" in kwargs:
        cur["enabled"] = bool(kwargs["enabled"])
    if "ceiling_c" in kwargs:
        try:
            v = int(kwargs["ceiling_c"])
            cur["ceiling_c"] = max(MIN_CEILING_C, min(MAX_CEILING_C, v))
        except (TypeError, ValueError):
            pass
    if "sustained_seconds" in kwargs:
        try:
            v = int(kwargs["sustained_seconds"])
            cur["sustained_seconds"] = max(MIN_SUSTAINED_S, min(MAX_SUSTAINED_S, v))
        except (TypeError, ValueError):
            pass
    if "action" in kwargs and kwargs["action"] in ("reset", "notify"):
        cur["action"] = kwargs["action"]
    if "cooldown_seconds" in kwargs:
        try:
            v = int(kwargs["cooldown_seconds"])
            cur["cooldown_seconds"] = max(10, min(600, v))
        except (TypeError, ValueError):
            pass
    _save(cur)
    log.info(f"temp_ceiling settings updated: {cur}")
    return cur


# ─── Trip log ─────────────────────────────────────────────────────────────
def _append_trip(temp_c: int, ceiling_c: int, action: str, reset_result: dict) -> None:
    """Record a trip event to disk for the UI's trip history."""
    entry = {
        "ts":           time.time(),
        "temp_c":       temp_c,
        "ceiling_c":    ceiling_c,
        "action":       action,
        "reset_ok":     bool(reset_result.get("ok")) if isinstance(reset_result, dict) else None,
        "reset_detail": reset_result if isinstance(reset_result, dict) else None,
    }
    history: list = []
    if os.path.exists(TRIP_LOG_PATH):
        try:
            with open(TRIP_LOG_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    history.append(entry)
    history = history[-50:]
    try:
        with open(TRIP_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.warning(f"Could not write trip log: {e}")


def get_trips(limit: int = 20) -> list[dict]:
    if not os.path.exists(TRIP_LOG_PATH):
        return []
    try:
        with open(TRIP_LOG_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
        return list(history[-limit:])
    except Exception:
        return []


def clear_trips() -> dict:
    try:
        if os.path.exists(TRIP_LOG_PATH):
            os.remove(TRIP_LOG_PATH)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ─── Live status ──────────────────────────────────────────────────────────
def get_status() -> dict[str, Any]:
    """Snapshot of the watchdog's live state for the UI."""
    s = _load()
    with _lock:
        st = dict(_state)
    above = None
    if st["above_since"] is not None and st["current_temp_c"] is not None:
        above = round(time.time() - st["above_since"], 1)
    cooldown = None
    if st["in_cooldown_until"] is not None and time.time() < st["in_cooldown_until"]:
        cooldown = round(st["in_cooldown_until"] - time.time(), 1)
    return {
        "settings":            s,
        "running":             st["running"],
        "current_temp_c":      st["current_temp_c"],
        "above_ceiling_for_s": above,           # None if currently below
        "tripped_count":       st["tripped_count"],
        "last_trip_at":        st["last_trip_at"],
        "last_trip_temp_c":    st["last_trip_temp_c"],
        "cooldown_remaining_s": cooldown,
    }


# ─── Watchdog loop ────────────────────────────────────────────────────────
def _do_trip(temp_c: int, settings: dict) -> None:
    """Execute the configured action when the ceiling is crossed."""
    ceiling = int(settings["ceiling_c"])
    action  = settings["action"]
    log.warning(
        f"⚠ TEMP CEILING TRIPPED — GPU at {temp_c}°C, ceiling {ceiling}°C, action={action}"
    )

    reset_result: dict = {}
    if action == "reset":
        try:
            from core import gpu_overclock
            reset_result = gpu_overclock.emergency_reset_oc(
                reason=f"temp_ceiling:{temp_c}c"
            )
        except Exception as e:
            log.error(f"emergency_reset_oc failed: {e}")
            reset_result = {"ok": False, "err": str(e)}

    # Toast — best-effort
    try:
        from core import notifier
        title = "GPU Temp Ceiling Tripped"
        msg = (f"GPU hit {temp_c}°C (ceiling {ceiling}°C). "
               + ("Overclock reverted to stock." if action == "reset"
                  else "Notify-only mode — no auto-revert."))
        notifier.toast(title, msg, force=True)
    except Exception as e:
        log.debug(f"toast failed: {e}")

    _append_trip(temp_c, ceiling, action, reset_result)

    with _lock:
        _state["tripped_count"]    += 1
        _state["last_trip_at"]      = time.time()
        _state["last_trip_temp_c"]  = temp_c
        _state["above_since"]       = None
        _state["in_cooldown_until"] = (
            time.time() + int(settings.get("cooldown_seconds", 60))
        )


def _watchdog_loop() -> None:
    log.info("temp_ceiling watchdog started")
    # Defer heavy import until thread runs so module load is cheap.
    from core import gpu_overclock

    stop_event = _state["stop_event"]
    while stop_event is not None and not stop_event.is_set():
        try:
            settings = _load()
            if not settings.get("enabled"):
                # Stay in the loop but skip the work — lets the user
                # toggle without bouncing the thread.
                stop_event.wait(2.0)
                continue

            poll_int = max(0.5, float(settings.get("poll_interval_s", 1.0)))
            ceiling  = int(settings["ceiling_c"])
            sustain  = int(settings["sustained_seconds"])

            state = gpu_overclock.read_live_state()
            temp  = state.get("temp_c") if state.get("ok") else None

            with _lock:
                _state["current_temp_c"] = temp

                in_cooldown = (
                    _state["in_cooldown_until"] is not None
                    and time.time() < _state["in_cooldown_until"]
                )

                if temp is not None and temp >= ceiling and not in_cooldown:
                    if _state["above_since"] is None:
                        _state["above_since"] = time.time()
                        log.warning(f"GPU {temp}°C ≥ ceiling {ceiling}°C — sustaining…")
                    sustained_for = time.time() - _state["above_since"]
                    if sustained_for >= sustain:
                        # Trip!  Release the lock first so _do_trip can grab it.
                        local_settings = settings
                        local_temp = temp
                        # Drop and re-acquire pattern: exit the with-block.
                        break_to_trip = True
                    else:
                        break_to_trip = False
                else:
                    if _state["above_since"] is not None and temp is not None and temp < ceiling - 2:
                        # 2°C hysteresis below ceiling before we reset the timer
                        log.info(f"GPU dropped to {temp}°C, clearing sustained timer")
                        _state["above_since"] = None
                    break_to_trip = False

            if break_to_trip:
                _do_trip(local_temp, local_settings)

        except Exception as e:
            log.warning(f"watchdog tick failed: {e}")

        try:
            stop_event.wait(poll_int if 'poll_int' in locals() else 1.0)
        except Exception:
            time.sleep(1.0)

    with _lock:
        _state["running"] = False
        _state["above_since"] = None
    log.info("temp_ceiling watchdog stopped")


def start_watchdog() -> dict:
    """Start the watchdog thread.  Idempotent — safe to call repeatedly."""
    with _lock:
        if _state["running"]:
            return {"ok": True, "msg": "Already running"}
        _state["stop_event"] = threading.Event()
        _state["running"] = True
        _state["above_since"] = None
        t = threading.Thread(
            target=_watchdog_loop,
            daemon=True,
            name="temp-ceiling-watchdog",
        )
        _state["thread"] = t
        t.start()
    return {"ok": True}


def stop_watchdog() -> dict:
    with _lock:
        if not _state["running"]:
            return {"ok": True, "msg": "Already stopped"}
        ev = _state["stop_event"]
    if ev is not None:
        ev.set()
    return {"ok": True}


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


# ─── Manual test ──────────────────────────────────────────────────────────
def test_trip() -> dict:
    """Manually fire a trip event to verify the toast + log integration.
    Does NOT call emergency_reset_oc — this is a UI test only."""
    s = _load()
    fake_temp = int(s["ceiling_c"]) + 5
    log.info(f"temp_ceiling test trip: simulating {fake_temp}°C")
    try:
        from core import notifier
        notifier.toast(
            "GPU Temp Ceiling — TEST",
            f"This is what you'll see when the GPU hits {fake_temp}°C "
            f"(ceiling: {s['ceiling_c']}°C). No actual revert was performed.",
            force=True,
        )
    except Exception as e:
        log.debug(f"test toast failed: {e}")
    _append_trip(fake_temp, int(s["ceiling_c"]), "test", {"ok": True, "test": True})
    return {"ok": True, "fake_temp": fake_temp, "ceiling_c": s["ceiling_c"]}
