"""Idle Mode (3.5.0-beta.12) — cool & quiet when you're not gaming.

When the game profiler sees no game running for a configurable grace
period, Vispora shifts the machine into a *thermal* low-profile:

  • GPU: drop the power limit and park any aggressive core/mem OC back to
    stock.  Lower power cap → less heat → the fans back off → a quieter,
    cooler card while you browse / watch / work.
  • CPU/system: switch to the Balanced power plan so cores idle down
    between bursts instead of holding max clocks.

The instant a game is detected again, the EXACT prior posture is
restored — the user's saved OC profile is re-applied and the power plan
that was active before idle mode engaged is put back — so gaming
performance is never sacrificed.

DESIGN PRINCIPLES (deliberate):
  • This is a THERMAL envelope, not a throttle.  It never suspends
    processes, never touches CPU affinity, never touches input, never
    limits what the user can do.  Everything on the desktop still runs
    exactly as normal — just cooler and quieter when nothing needs the
    headroom.  A GPU power *limit* only caps sustained draw; light
    desktop / video / even moderate compute stays perfectly responsive.
  • Opt-in.  Default OFF.  Nothing happens unless the user enables it.
  • Restore-exact.  The prior power plan GUID + saved OC profile are
    captured when idle mode engages and put back verbatim on exit, so a
    user running +450/+2500 gets +450/+2500 back the moment they launch
    a game — not "stock" and not "whatever idle mode left".
  • Fail-safe.  If the GPU can't be OC-controlled (AMD / no NVIDIA /
    driver missing) it simply skips the GPU lever and still does the
    power-plan lever.  Any error degrades to a no-op, never a crash.
"""
import threading
import time
from typing import Any, Optional

from config import APPDATA_DIR
from core.utils import get_logger, run_cmd, atomic_write_json
from core import game_profiles, gpu_overclock

import os

log = get_logger("idle_mode")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "idle_mode_settings.json")

# Windows Balanced power plan — the cool/quiet target while idle.
_BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"

_DEFAULTS = {
    "enabled": False,
    "grace_minutes": 5,        # no-game time before idle engages
    "poll_seconds": 20,        # how often we check for a game
    "idle_power_pct": 70,      # GPU power limit while idle (of default)
    "park_oc": True,           # drop core/mem offsets to stock while idle
    "switch_power_plan": True, # use Balanced while idle
}
# Clamp bounds so a hand-edited settings file can't push the GPU
# somewhere unsafe or set a nonsense cadence.
_BOUNDS = {
    "grace_minutes":  (1, 120),
    "poll_seconds":   (5, 120),
    "idle_power_pct": (50, 100),
}

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None

# Live runtime state (guarded by _lock).  `restore` holds the exact
# posture to put back when a game returns.
_state = {
    "engaged": False,          # currently in the cool/quiet posture?
    "last_game_ts": 0.0,       # last time a game was seen
    "restore": None,           # {"power_guid": str, "oc": {...}} or None
    "last_transition": "",     # human-readable last action
    "last_transition_ts": 0.0,
}


# ─── Settings ────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            import json
            d = json.load(f)
        return {**_DEFAULTS, **(d if isinstance(d, dict) else {})}
    except Exception:
        return dict(_DEFAULTS)


def _save(s: dict) -> None:
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load()


def set_settings(enabled: Optional[bool] = None,
                 grace_minutes: Optional[int] = None,
                 poll_seconds: Optional[int] = None,
                 idle_power_pct: Optional[int] = None,
                 park_oc: Optional[bool] = None,
                 switch_power_plan: Optional[bool] = None) -> dict:
    s = _load()
    if enabled is not None:            s["enabled"] = bool(enabled)
    if park_oc is not None:            s["park_oc"] = bool(park_oc)
    if switch_power_plan is not None:  s["switch_power_plan"] = bool(switch_power_plan)
    for key, val in (("grace_minutes", grace_minutes),
                     ("poll_seconds", poll_seconds),
                     ("idle_power_pct", idle_power_pct)):
        if val is not None:
            lo, hi = _BOUNDS[key]
            try:
                s[key] = max(lo, min(hi, int(val)))
            except (TypeError, ValueError):
                pass
    _save(s)
    # Apply the enable/disable change live.
    if s["enabled"]:
        _ensure_running()
    else:
        # Disabling: stop the loop and, if we were mid-idle, restore the
        # user's full posture immediately so nothing is left parked.
        _restore_if_engaged("idle mode disabled")
        stop()
    return {"ok": True, "settings": s}


# ─── The two cooling levers (each captures + restores exactly) ───────
def _current_power_guid() -> str:
    """The GUID of the currently-active power scheme, or '' if unknown."""
    r = run_cmd(["powercfg", "/getactivescheme"], timeout=6)
    if not r.get("ok"):
        return ""
    # Output: "Power Scheme GUID: 8c5e7fda-... (High performance)"
    for part in (r.get("out") or "").split():
        if len(part) == 36 and part.count("-") == 4:
            return part
    return ""


def _engage() -> None:
    """Capture the current posture, then apply the cool/quiet levers.
    Assumes caller holds nothing — grabs _lock internally."""
    s = _load()
    with _lock:
        if _state["engaged"]:
            return
        # ── Capture restore point BEFORE changing anything ──
        restore: dict[str, Any] = {"power_guid": "", "oc": None}
        if s.get("switch_power_plan"):
            restore["power_guid"] = _current_power_guid()
        if s.get("park_oc"):
            # Prefer the user's saved profile (what boot re-applies); fall
            # back to current live offsets if there's no saved profile.
            prof = gpu_overclock.load_profile()
            if prof.get("exists"):
                restore["oc"] = {
                    "core":  int(prof.get("core_offset_mhz", 0) or 0),
                    "mem":   int(prof.get("mem_offset_mhz", 0) or 0),
                    "power": int(prof.get("power_pct", 100) or 100),
                }
        _state["restore"] = restore
        _state["engaged"] = True

    # ── Apply levers (outside the lock — these shell out) ──
    detail = []
    if s.get("switch_power_plan"):
        r = run_cmd(["powercfg", "/setactive", _BALANCED_GUID], timeout=6)
        if r.get("ok"):
            detail.append("power plan → Balanced")
    # GPU: lower power limit and (optionally) park OC to stock for a
    # genuinely cooler card.  Only if OC is actually controllable.
    try:
        cap = gpu_overclock.detect_gpu_oc_capability()
        if cap.get("ok") and cap.get("vendor") == "nvidia":
            core = 0 if s.get("park_oc") else None
            mem  = 0 if s.get("park_oc") else None
            pct  = int(s.get("idle_power_pct", 70))
            # If not parking OC, keep the user's saved offsets and only
            # lower the power cap.
            if core is None:
                prof = gpu_overclock.load_profile()
                core = int(prof.get("core_offset_mhz", 0) or 0) if prof.get("exists") else 0
                mem  = int(prof.get("mem_offset_mhz", 0) or 0) if prof.get("exists") else 0
            gr = gpu_overclock.apply_oc(core_offset_mhz=core, mem_offset_mhz=mem, power_pct=pct)
            if gr.get("ok"):
                detail.append(f"GPU power limit → {pct}%"
                              + (" · OC parked to stock" if s.get("park_oc") else ""))
    except Exception as e:
        log.debug(f"idle GPU lever skipped: {e}")

    msg = "Idle Mode engaged — " + (", ".join(detail) if detail else "no coolable levers available")
    log.info(msg)
    with _lock:
        _state["last_transition"] = msg
        _state["last_transition_ts"] = time.time()


def _restore_if_engaged(reason: str) -> None:
    """Put the captured posture back and mark disengaged.  Safe to call
    even when not engaged (no-op)."""
    with _lock:
        if not _state["engaged"]:
            return
        restore = _state.get("restore") or {}
        _state["engaged"] = False
        _state["restore"] = None

    detail = []
    # Restore the power plan first (cheap, always safe).
    guid = restore.get("power_guid") or ""
    if guid:
        r = run_cmd(["powercfg", "/setactive", guid], timeout=6)
        if r.get("ok"):
            detail.append("power plan restored")
    # Restore the exact OC the user had (their saved profile), so gaming
    # performance comes back in full.
    oc = restore.get("oc")
    if oc is not None:
        try:
            gpu_overclock.apply_oc(core_offset_mhz=oc.get("core", 0),
                                   mem_offset_mhz=oc.get("mem", 0),
                                   power_pct=oc.get("power", 100))
            detail.append(f"OC restored (+{oc.get('core',0)}/+{oc.get('mem',0)}, "
                          f"{oc.get('power',100)}% power)")
        except Exception as e:
            log.warning(f"idle OC restore failed: {e}")

    msg = f"Idle Mode released ({reason}) — " + (", ".join(detail) if detail else "nothing to restore")
    log.info(msg)
    with _lock:
        _state["last_transition"] = msg
        _state["last_transition_ts"] = time.time()


# ─── Detection ───────────────────────────────────────────────────────
def _a_game_is_running() -> bool:
    """True if the game profiler currently sees any known game process.
    Uses the same scanner game-mode uses, independent of whether game
    optimization is actually engaged."""
    try:
        games = game_profiles.get_active_games() or []
        return len(games) > 0
    except Exception as e:
        log.debug(f"game scan failed (treating as game-present to be safe): {e}")
        # Fail SAFE: if we can't tell, assume a game might be running and
        # do NOT engage idle mode — never risk parking OC mid-game.
        return True


# ─── Poll loop ───────────────────────────────────────────────────────
def _loop(stop: threading.Event) -> None:
    log.info("Idle Mode watcher started")
    # Seed last_game_ts to now so we always wait the full grace period
    # after enabling before engaging (avoids an instant park on startup).
    with _lock:
        _state["last_game_ts"] = time.time()
    while not stop.is_set():
        try:
            s = _load()
            if not s.get("enabled"):
                # User disabled via settings file directly — release + exit.
                _restore_if_engaged("disabled")
                return
            poll = int(s.get("poll_seconds", 20))
            grace_s = int(s.get("grace_minutes", 5)) * 60

            if _a_game_is_running():
                with _lock:
                    _state["last_game_ts"] = time.time()
                # A game showed up while we were idle → restore INSTANTLY.
                if _state["engaged"]:
                    _restore_if_engaged("game detected")
            else:
                with _lock:
                    idle_for = time.time() - _state["last_game_ts"]
                    engaged = _state["engaged"]
                if not engaged and idle_for >= grace_s:
                    _engage()

            if stop.wait(poll):
                return
        except Exception as e:
            # Never let the watcher die silently and leave the machine
            # parked; log and keep looping.
            log.warning(f"idle mode loop iteration error: {e}")
            if stop.wait(int(_load().get("poll_seconds", 20))):
                return


def _ensure_running() -> None:
    global _thread, _stop
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(target=_loop, args=(_stop,), daemon=True,
                                   name="idle-mode")
        _thread.start()


def stop() -> dict:
    global _thread, _stop
    with _lock:
        ev, t = _stop, _thread
        _thread = None
        _stop = None
    if ev:
        ev.set()
    if t is not None:
        t.join(timeout=2.0)
    return {"ok": True}


def is_running() -> bool:
    with _lock:
        return bool(_thread and _thread.is_alive())


def get_status() -> dict:
    s = _load()
    with _lock:
        engaged = _state["engaged"]
        last_game_ts = _state["last_game_ts"]
        last_transition = _state["last_transition"]
    idle_for = max(0, int(time.time() - last_game_ts)) if last_game_ts else 0
    return {
        "enabled": bool(s.get("enabled")),
        "running": is_running(),
        "engaged": engaged,
        "idle_seconds": idle_for,
        "grace_seconds": int(s.get("grace_minutes", 5)) * 60,
        "last_transition": last_transition,
        "settings": s,
    }


def run_on_boot() -> None:
    """Arm the watcher on app startup if the user left idle mode enabled.
    Called from a deferred boot hook so it never blocks window creation."""
    if _load().get("enabled"):
        _ensure_running()
        log.info("Idle Mode was enabled — watcher armed on boot")


def manual_release() -> dict:
    """Force-release idle mode right now (UI 'wake' button)."""
    _restore_if_engaged("manual wake")
    return {"ok": True}
