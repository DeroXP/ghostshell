"""Gamepad Mapper — full N-source × N-target input router (v3.4).

Inputs (any of):
    • Controller buttons / sticks / triggers          (XInput poll)
    • Keyboard keys                                   (WH_KEYBOARD_LL hook)
    • Mouse buttons (left/right/middle/x1/x2/wheel)   (WH_MOUSE_LL hook)
    • Mouse movement                                  (WH_MOUSE_LL → analog)

Outputs (any of):
    • Keyboard key (single)                           (SendInput KB)
    • Mouse button (single)                           (SendInput mouse)
    • Virtual controller button                       (vgamepad / ViGEmBus)
    • Virtual controller stick direction              (vgamepad / ViGEmBus)
    • Macro — sequence of any of the above            (timer thread)
    • Disabled — input is silently swallowed

Profile schema (v3.4 — new format)
----------------------------------
%APPDATA%/GhostShell/gamepad_profiles.json
    {
      "profiles": {
          "<name>": {
              "name":                 str,
              "controller_vid_pid":   "VID_xxxx&PID_xxxx" | None,
              "active_only_in_game":  bool,
              "remap_mode":           "key_emulation" | "virtual_controller",
              "poll_hz":              1000 | 2000 | 4000 | 8000,
              "hide_physical":        bool,
              "mappings": [
                  {
                      "id":     "<short-uuid>",
                      "input":  { "source": "controller"|"keyboard"|"mouse",
                                  "trigger": "A" | "Space" | "M_LEFT" | ... },
                      "output": { "target": "key" | "mouse_button"
                                            | "vbutton" | "vstick"
                                            | "macro"  | "disabled",
                                  ...target-specific fields... },
                  },
                  ...
              ]
          }
      },
      "active_profile":  "<name>" | null,
      "enabled":         bool
    }

Old schema (v3.1-v3.3) where "buttons" was a dict keyed by physical
controller button name is auto-migrated on load — see _migrate_profile().
"""

from __future__ import annotations
import ctypes
import json
import math
import os
import threading
import time
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger, atomic_write_json
from config import APPDATA_DIR
from core import xinput_ctypes as xi

log = get_logger(__name__)

PROFILES_PATH = os.path.join(APPDATA_DIR, "gamepad_profiles.json")

# v3.3 — polling rate is now per-profile (1 / 2 / 4 / 8 kHz).  The
# default is 4000 Hz (250 µs cycles) which beats most XInput driver
# internal sample rates and adds <1 ms of remap latency on top of the
# physical controller's USB transfer cycle.  8 kHz works but ~99 % of
# polls return the same packet_number (XInput's surfacing rate is the
# real ceiling, not our loop) — useful only to minimize the lag between
# OS update and ViGEmBus forward.
DEFAULT_POLL_HZ = 4000
SUPPORTED_POLL_HZ = (1000, 2000, 4000, 8000)

# Live-measured stats (read by the UI for the "actual rate" display)
_stats = {
    "actual_hz":      0.0,
    "useful_hz":      0.0,   # ticks where XInput surfaced a new packet
    "frame_p99_us":   0,
    "frame_max_us":   0,
    "ticks_total":    0,
    "current_poll_hz": DEFAULT_POLL_HZ,
}

# v3.3 — Windows scheduler granularity helper.  timeBeginPeriod(1) drops
# the system-wide sleep granularity from ~15 ms to 1 ms during the
# poll loop, making time.sleep() / Event.wait() much more predictable.
# Paired timeEndPeriod(1) on loop exit so we don't pollute the rest of
# the system after we stop.
try:
    import ctypes as _ctypes
    _winmm = _ctypes.WinDLL("winmm")
except Exception:
    _winmm = None

def _set_timer_granularity(period_ms: int):
    if _winmm is None:
        return
    try:
        _winmm.timeBeginPeriod(int(period_ms))
    except Exception:
        pass

def _release_timer_granularity(period_ms: int):
    if _winmm is None:
        return
    try:
        _winmm.timeEndPeriod(int(period_ms))
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────────
# SendInput (ctypes, no extra deps)
# ────────────────────────────────────────────────────────────────────

INPUT_KEYBOARD       = 1
KEYEVENTF_KEYUP      = 0x0002
KEYEVENTF_SCANCODE   = 0x0008
KEYEVENTF_EXTENDED   = 0x0001
MAPVK_VK_TO_VSC      = 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         wintypes.WORD),
        ("wScan",       wintypes.WORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# v3.4 — also support MOUSEINPUT in our SendInput union.
INPUT_MOUSE              = 0
MOUSEEVENTF_LEFTDOWN     = 0x0002
MOUSEEVENTF_LEFTUP       = 0x0004
MOUSEEVENTF_RIGHTDOWN    = 0x0008
MOUSEEVENTF_RIGHTUP      = 0x0010
MOUSEEVENTF_MIDDLEDOWN   = 0x0020
MOUSEEVENTF_MIDDLEUP     = 0x0040
MOUSEEVENTF_XDOWN        = 0x0080
MOUSEEVENTF_XUP          = 0x0100
MOUSEEVENTF_WHEEL        = 0x0800
MOUSEEVENTF_MOVE         = 0x0001    # relative motion (dx, dy in pixels)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          wintypes.LONG),
        ("dy",          wintypes.LONG),
        ("mouseData",   wintypes.DWORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u",    _INPUTUNION),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_user32.MapVirtualKeyW.restype = wintypes.UINT


def _send_key(vk: int, key_up: bool):
    """Send a single key event using scancode (more compatible with games
    that read raw input).  Falls back to VK if scancode is 0."""
    scan = _user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC)
    flags = 0
    if scan:
        flags |= KEYEVENTF_SCANCODE
    if key_up:
        flags |= KEYEVENTF_KEYUP
    # Extended-key flag for arrow keys etc.
    if vk in (0x21, 0x22, 0x23, 0x24,            # PgUp PgDn End Home
              0x25, 0x26, 0x27, 0x28,            # Arrows
              0x2D, 0x2E,                        # Insert Delete
              0x90,                              # NumLock
              0x6F):                             # NumpadDiv
        flags |= KEYEVENTF_EXTENDED
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(
            wVk=int(vk) if not scan else 0,
            wScan=scan,
            dwFlags=flags,
            time=0,
            dwExtraInfo=None,
        )),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


# v3.4 — mouse-button output via SendInput.
_MOUSE_DOWN_FLAG = {
    "M_LEFT":   MOUSEEVENTF_LEFTDOWN,
    "M_RIGHT":  MOUSEEVENTF_RIGHTDOWN,
    "M_MIDDLE": MOUSEEVENTF_MIDDLEDOWN,
    "M_X1":     MOUSEEVENTF_XDOWN,
    "M_X2":     MOUSEEVENTF_XDOWN,
}
_MOUSE_UP_FLAG = {
    "M_LEFT":   MOUSEEVENTF_LEFTUP,
    "M_RIGHT":  MOUSEEVENTF_RIGHTUP,
    "M_MIDDLE": MOUSEEVENTF_MIDDLEUP,
    "M_X1":     MOUSEEVENTF_XUP,
    "M_X2":     MOUSEEVENTF_XUP,
}
_MOUSE_XBUTTON_DATA = {"M_X1": 0x0001, "M_X2": 0x0002}


def _send_mouse_button(button: str, key_up: bool):
    """Click / release a mouse button.  `button` is one of
    M_LEFT / M_RIGHT / M_MIDDLE / M_X1 / M_X2."""
    flag = _MOUSE_UP_FLAG.get(button) if key_up else _MOUSE_DOWN_FLAG.get(button)
    if not flag:
        return
    data = _MOUSE_XBUTTON_DATA.get(button, 0)
    inp = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=_MOUSEINPUT(
            dx=0, dy=0, mouseData=data, dwFlags=flag, time=0, dwExtraInfo=None,
        )),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_unicode_char(c: str):
    """Type a single unicode char via SendInput's KEYEVENTF_UNICODE flag.
    Doesn't require a VK mapping — every printable BMP character works
    regardless of the user's keyboard layout.  Surrogate-pair chars
    (emoji etc.) are skipped for simplicity."""
    if not c:
        return
    code = ord(c)
    if code > 0xFFFF:
        return
    KEYEVENTF_UNICODE = 0x0004
    # Down
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(
            wVk=0, wScan=code,
            dwFlags=KEYEVENTF_UNICODE,
            time=0, dwExtraInfo=None,
        )),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    # Up
    inp.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_mouse_wheel(direction_up: bool, ticks: int = 1):
    """Scroll the mouse wheel (positive = up).  120 = one tick."""
    delta = 120 * (ticks if direction_up else -ticks)
    inp = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=_MOUSEINPUT(
            dx=0, dy=0, mouseData=delta & 0xFFFFFFFF,
            dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=None,
        )),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_mouse_move(dx: int, dy: int):
    """v3.6 — relative-motion mouse-move event (MOUSEEVENTF_MOVE).
    Used by the macro runner's `mouse_move` step kind so recorded
    mouse-move samples can be played back without absolute-position
    teleports."""
    if not dx and not dy:
        return
    inp = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=_MOUSEINPUT(
            dx=int(dx), dy=int(dy), mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None,
        )),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))



# ────────────────────────────────────────────────────────────────────
# Persisted state I/O
# ────────────────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "profiles":       {},
    "active_profile": None,
    "enabled":        False,
    # v3.5 — global hotkey to toggle the mapper on/off without alt-tabbing.
    # 0 = disabled.  Use a function key by default to avoid clashing with
    # game inputs.
    "toggle_hotkey_vk":   0,
    "toggle_hotkey_name": "",
}


# In-memory state cache — read once per polling tick instead of re-parsing
# JSON 8000 times/sec.  Invalidated on every _save_state.
_state_cache: Optional[dict] = None
_state_cache_lock = threading.Lock()


# v3.4 — schema enums.  These are the valid values for input.source,
# input.trigger, output.target, etc. — surfaced via /api/gamepad/status
# so the UI can build dropdowns without hard-coding them.

INPUT_SOURCES = ("controller", "keyboard", "mouse")

# Mouse triggers — names match what input_hooks emits
MOUSE_TRIGGERS = ("M_LEFT", "M_RIGHT", "M_MIDDLE", "M_X1", "M_X2",
                  "M_WHEEL_UP", "M_WHEEL_DOWN", "M_MOVE")

OUTPUT_TARGETS = ("key", "mouse_button", "vbutton", "vstick",
                  "vtrigger", "macro", "disabled")

# Virtual-button names the user can target as the OUTPUT of a mapping.
# Includes the standard Xbox-layout buttons (which translate cleanly to
# their DS4 equivalents in DS4 mode — A→Cross, START→Options, etc.)
# PLUS DS4-only special buttons that have no Xbox equivalent so the user
# would otherwise have no way to fire them.  In X360 mode TOUCHPAD/PS
# are silently ignored at dispatch time.
VBUTTON_NAMES = list(xi.BUTTON_NAMES_DIGITAL) + ["TOUCHPAD", "PS"]

VSTICK_DIRECTIONS = ("up", "down", "left", "right")
VSTICK_SIDES = ("left", "right")

# Virtual triggers — analog 0.0-1.0 values forwarded to ViGEmBus's
# left_trigger() / right_trigger() (which take 0-255 internally).
VTRIGGER_SIDES = ("lt", "rt")

# Macro play modes
MACRO_PLAY_MODES = ("once", "loop_while_held", "repeat_n")

# v3.5 — stick processing curves.  Applied after deadzone, before
# saturation clamping.  Each takes a normalized magnitude m in [0, 1]
# and returns a re-mapped magnitude.
#   linear: m
#   power:  m^k                  (k>1 = slower-near-center; k<1 = faster)
#   expo:   exp(k*m)/exp(k) blend (smooth slow-near-center)
#   s_curve: 3m^2 - 2m^3 (smoothstep)
STICK_CURVES = ("linear", "power", "expo", "s_curve")

# Default per-stick tuning — applied to BOTH left and right sticks unless
# overridden by profile.sticks.left / .right.
DEFAULT_STICK_TUNING = {
    "deadzone_in":   0.00,    # 0..1, fraction of full range to ignore near center
    "deadzone_out":  1.00,    # 0..1, where the stick is treated as saturated
    "curve":         "linear",
    "curve_strength": 1.5,    # exponent / steepness for power/expo
    "invert_x":      False,
    "invert_y":      False,
    "circular_gate": False,   # True = clamp magnitude to 1.0 (vs square gate)
}

# Per-binding behavior modes (toggle, turbo, chord, layer).  Stored at
# mapping.input.behavior  alongside the existing source/trigger fields.
BINDING_BEHAVIORS = ("normal", "toggle", "turbo", "chord")


def _new_id() -> str:
    """Short-but-unique id for mapping rows.  Doesn't need to be a true
    UUID — the UI just uses it as a stable React-style key."""
    import uuid as _u
    return _u.uuid4().hex[:10]


def _migrate_profile(profile: dict) -> dict:
    """Convert old-schema profiles (where `buttons` was a dict keyed by
    physical controller button name) into the new v3.4 mappings array.
    Idempotent — already-migrated profiles pass through."""
    if not isinstance(profile, dict):
        return profile
    if "mappings" in profile and isinstance(profile["mappings"], list):
        return profile     # already on the new schema

    old = profile.get("buttons")
    new_mappings: list[dict] = []
    if isinstance(old, dict):
        for btn, binding in old.items():
            if not isinstance(binding, dict):
                continue
            btype = binding.get("type")
            if btype in (None, "passthrough"):
                continue
            inp = {"source": "controller", "trigger": btn}
            if btype == "key":
                outp = {"target": "key", "vk": int(binding.get("vk", 0))}
            elif btype == "vbutton":
                outp = {"target": "vbutton",
                        "vbutton": binding.get("vbutton") or btn}
            elif btype == "macro":
                outp = {"target": "macro",
                        "steps": binding.get("steps") or []}
            elif btype == "disabled":
                outp = {"target": "disabled"}
            else:
                continue
            new_mappings.append({"id": _new_id(),
                                 "input":  inp,
                                 "output": outp})

    profile["mappings"] = new_mappings
    # Keep the legacy "buttons" key around for at most one launch so any
    # in-flight UI reads don't fail — _save_state strips it on first save.
    return _ensure_profile_v35_fields(profile)


def _ensure_profile_v35_fields(profile: dict) -> dict:
    """Idempotently add the v3.5 fields (stick tuning, layer modifier,
    auto-switch settings) to a profile if missing.  Doesn't overwrite
    existing values, so user customizations survive."""
    if not isinstance(profile, dict):
        return profile
    sticks = profile.setdefault("sticks", {})
    sticks.setdefault("swap", False)
    for side in ("left", "right"):
        cfg = sticks.setdefault(side, {})
        for k, v in DEFAULT_STICK_TUNING.items():
            cfg.setdefault(k, v)
    # v3.5 — shift-layer modifier.  When the user holds the named input
    # (e.g. controller LB) the alt_mappings array becomes active in
    # place of mappings.  Empty modifier_trigger disables the layer.
    profile.setdefault("modifier_source",  "")  # "controller" / "keyboard" / "mouse" / ""
    profile.setdefault("modifier_trigger", "")  # button/key name or ""
    profile.setdefault("alt_mappings",     [])  # second layer
    # v3.5 — auto-switch.  Empty foreground_exe means manual select only.
    profile.setdefault("foreground_exe",   "")
    # v3.5 — per-mapping behavior fields default to 'normal'.  Existing
    # bindings get the field added without their behavior changing.
    for m in profile.get("mappings", []) or []:
        if isinstance(m, dict):
            inp = m.setdefault("input", {})
            inp.setdefault("behavior",  "normal")
            inp.setdefault("turbo_hz",  10)        # used when behavior=turbo
            inp.setdefault("chord_with", "")       # secondary trigger for chord
    for m in profile.get("alt_mappings", []) or []:
        if isinstance(m, dict):
            inp = m.setdefault("input", {})
            inp.setdefault("behavior",  "normal")
            inp.setdefault("turbo_hz",  10)
            inp.setdefault("chord_with", "")
    return profile


def _load_state(use_cache: bool = True) -> dict:
    global _state_cache
    if use_cache:
        with _state_cache_lock:
            if _state_cache is not None:
                return _state_cache
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = dict(DEFAULT_STATE)
        out = dict(DEFAULT_STATE)
        out.update(data)
        if not isinstance(out.get("profiles"), dict):
            out["profiles"] = {}
        # Migrate every profile to v3.4 schema on load
        for name, prof in list(out["profiles"].items()):
            out["profiles"][name] = _migrate_profile(prof)
    except (OSError, ValueError):
        out = dict(DEFAULT_STATE)
    with _state_cache_lock:
        _state_cache = out
    return out


def _save_state(state: dict):
    global _state_cache
    if atomic_write_json(PROFILES_PATH, state):
        # Invalidate cache so next poll-loop tick re-reads
        with _state_cache_lock:
            _state_cache = state


# ────────────────────────────────────────────────────────────────────
# Mapper runtime
# ────────────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()

# Per-controller-slot tracking
_prev_buttons: dict[int, set[str]] = {0: set(), 1: set(), 2: set(), 3: set()}

# Per-slot digital trigger state (LT / RT treated as buttons via threshold
# + hysteresis — XInput exposes them as 0-255 analog values, but for
# remapping purposes the user wants them to behave like any other button).
_prev_triggers: dict[int, dict[str, bool]] = {
    0: {"LT": False, "RT": False},
    1: {"LT": False, "RT": False},
    2: {"LT": False, "RT": False},
    3: {"LT": False, "RT": False},
}

# Tracks which keys we've held down via "key" mappings, so we can
# release them cleanly when the controller button is released.
_held_keys: dict[str, set[int]] = {}     # button_name -> {vk1, vk2, ...}

# Captured-button-press buffer for the UI's "press a button to bind" flow
_capture_event = threading.Event()
_capture_value: Optional[str] = None


def _is_active_in_foreground() -> bool:
    """When the active profile has active_only_in_game=True, only emit
    keys while game_profiles is reporting an active game.  This prevents
    your controller from typing in Discord while alt-tabbed."""
    state = _load_state()
    pname = state.get("active_profile")
    if not pname:
        return False
    profile = state["profiles"].get(pname)
    if not profile:
        return False
    if not profile.get("active_only_in_game", True):
        return True   # always-on profile
    try:
        from core import game_profiles
        info = game_profiles.get_active_game_info()
        return bool(info and info.get("exe"))
    except Exception:
        return False


# Active macros, keyed by mapping ID.  Used so a trigger release can
# signal the matching macro thread to wrap up + release its holds.
_active_macros: dict[str, "threading.Event"] = {}
_active_macros_lock = threading.Lock()


def _stop_macro(mid: str) -> None:
    """Signal the macro thread for this mapping ID to wrap up and release
    everything it's still holding.  Called when the trigger that fired
    the macro is released."""
    with _active_macros_lock:
        ev = _active_macros.get(mid)
    if ev is not None:
        ev.set()


def _ramp_vstick(side: str, cid: str,
                 start_x: float, start_y: float,
                 end_x: float, end_y: float,
                 duration_ms: int,
                 stop_event: "threading.Event") -> bool:
    """Smoothly move a virtual-stick contribution from (start_x, start_y)
    to (end_x, end_y) over duration_ms milliseconds.  Updates the shared
    _vstick_contrib dict at ~60 Hz so the polling loop's snapshot picks
    up each interpolated frame.

    Returns True if the stop_event fired during the ramp (caller should
    unwind held state in that case)."""
    if duration_ms <= 0:
        _vstick_contrib[side][cid] = {"x": end_x, "y": end_y}
        return stop_event.is_set()
    t0 = time.perf_counter()
    while True:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms >= duration_ms:
            break
        t = elapsed_ms / duration_ms
        _vstick_contrib[side][cid] = {
            "x": start_x + (end_x - start_x) * t,
            "y": start_y + (end_y - start_y) * t,
        }
        # ~16 ms tick = 60 Hz interpolation rate.  Interruptible.
        if stop_event.wait(0.016):
            return True
    # Final snap to exact target — ensures we don't end at 99% due to timing
    _vstick_contrib[side][cid] = {"x": end_x, "y": end_y}
    return stop_event.is_set()


def _run_macro(steps: list[dict], origin: str,
               play_mode: str = "once",
               repeat_count: int = 1):
    """v3.4 macro runner — supports tap/down/up modes for concurrent holds.

    Each step is one of:
       { "key":     { "vk", "down_ms", "delay_after_ms", "mode" } }
       { "mouse":   { "button", "down_ms", "delay_after_ms", "mode" } }
       { "vbutton": { "vbutton", "down_ms", "delay_after_ms", "mode" } }
       { "vstick":  { "stick": "left"|"right", "x": -1..1, "y": -1..1,
                      "down_ms", "delay_after_ms", "mode" } }

    `mode` is one of:
       "tap"  (default) — press, hold for down_ms, release; then wait delay_after_ms
       "down"           — press only (stays held); wait delay_after_ms
       "up"             — release the previously-down input; wait delay_after_ms

    For concurrent inputs: use `down` on input A, then `tap`/`down` other
    inputs while A is held.  At macro end (or if a still-held step has no
    matching `up`), the runner releases everything via _release_macro_holds.

    Legacy v3.1 steps {vk, hold_ms, delay_after_ms} are still accepted —
    treated as key tap steps."""
    # Per-macro tracking of what we still hold so we can clean up on
    # exit (and so `up` steps know what to release)
    held_keys:    set[int] = set()
    held_mouse:   set[str] = set()
    held_vbtn:    set[str] = set()       # vbutton names — published via _held_outputs
    held_vsticks: set[tuple[str, str]] = set()    # (side, contrib_id)

    macro_uid = origin + "-" + str(int(time.perf_counter() * 1000))

    # Trigger-release-stops-macro: register a stop event on the mapping ID.
    # `origin` IS the mapping ID for normal-flow callers; if a previous
    # macro from the same mapping is still running, signal it first so
    # we don't accumulate concurrent runs.
    stop_event = threading.Event()
    with _active_macros_lock:
        prev_event = _active_macros.get(origin)
        if prev_event is not None:
            prev_event.set()
        _active_macros[origin] = stop_event

    def _interruptible_sleep(seconds: float) -> bool:
        """Sleep for `seconds` but wake immediately on stop_event.
        Returns True if the stop signal was received during the wait."""
        if seconds <= 0:
            return stop_event.is_set()
        return stop_event.wait(seconds)

    def _release_held():
        for vk in list(held_keys):
            try: _send_key(vk, key_up=True)
            except Exception: pass
        held_keys.clear()
        for btn in list(held_mouse):
            try: _send_mouse_button(btn, key_up=True)
            except Exception: pass
        held_mouse.clear()
        # Vbuttons + vsticks are coordinated through the polling loop's
        # snapshot rebuild — clearing the contribution dicts is enough.
        for vb in list(held_vbtn):
            _held_outputs.pop(f"{macro_uid}:vb:{vb}", None)
        held_vbtn.clear()
        for side, cid in list(held_vsticks):
            # Stick contributions get the side directly ("left"/"right");
            # vtrigger holds use the prefix "__trigger__:lt"/"__trigger__:rt"
            # so we can route them to the right contribution dict.
            if side.startswith("__trigger__:"):
                trig_side = side.split(":", 1)[1]
                _vtrigger_contrib.get(trig_side, {}).pop(cid, None)
            else:
                _vstick_contrib[side].pop(cid, None)
        held_vsticks.clear()

    def _run_one_pass() -> bool:
        """Walk every step once.  Returns True if stop_event fired
        mid-pass so the outer loop knows to bail."""
        for step_idx, step in enumerate(steps):
            if stop_event.is_set():
                return True
            # Per-step disabled flag — runner silently skips
            if step.get("disabled"):
                continue
            try:
                # Legacy v3.1 step format — auto-treated as key tap
                if "vk" in step and "key" not in step:
                    vk = int(step.get("vk", 0))
                    if vk:
                        _send_key(vk, key_up=False)
                        if _interruptible_sleep(max(0, int(step.get("hold_ms", 30))) / 1000.0):
                            _send_key(vk, key_up=True)
                            return True
                        _send_key(vk, key_up=True)
                    if _interruptible_sleep(max(0, int(step.get("delay_after_ms", 0))) / 1000.0):
                        return True
                    continue

                kind = next((k for k in ("key", "mouse", "vbutton", "vstick",
                                         "vtrigger", "text", "mouse_move")
                             if k in step), None)
                if not kind:
                    continue
                payload  = step[kind] or {}
                mode     = payload.get("mode", "tap")
                hold_ms  = max(0, int(payload.get("down_ms", 30)))
                delay_ms = max(0, int(payload.get("delay_after_ms", 0)))

                if kind == "key":
                    vk = int(payload.get("vk", 0))
                    if vk:
                        if mode == "tap":
                            _send_key(vk, key_up=False)
                            if _interruptible_sleep(hold_ms / 1000.0):
                                _send_key(vk, key_up=True); return True
                            _send_key(vk, key_up=True)
                        elif mode == "down":
                            _send_key(vk, key_up=False)
                            held_keys.add(vk)
                        elif mode == "up":
                            _send_key(vk, key_up=True)
                            held_keys.discard(vk)

                elif kind == "mouse":
                    btn = payload.get("button", "")
                    if btn:
                        if mode == "tap":
                            _send_mouse_button(btn, key_up=False)
                            if _interruptible_sleep(hold_ms / 1000.0):
                                _send_mouse_button(btn, key_up=True); return True
                            _send_mouse_button(btn, key_up=True)
                        elif mode == "down":
                            _send_mouse_button(btn, key_up=False)
                            held_mouse.add(btn)
                        elif mode == "up":
                            _send_mouse_button(btn, key_up=True)
                            held_mouse.discard(btn)

                elif kind == "vbutton":
                    vb = payload.get("vbutton", "")
                    if vb:
                        slot_id = f"{macro_uid}:vb:{vb}"
                        if mode == "tap":
                            _held_outputs[slot_id] = {"kind": "vbutton", "vbutton": vb}
                            if _interruptible_sleep(hold_ms / 1000.0):
                                _held_outputs.pop(slot_id, None); return True
                            _held_outputs.pop(slot_id, None)
                        elif mode == "down":
                            _held_outputs[slot_id] = {"kind": "vbutton", "vbutton": vb}
                            held_vbtn.add(vb)
                        elif mode == "up":
                            _held_outputs.pop(slot_id, None)
                            held_vbtn.discard(vb)

                elif kind == "vstick":
                    side = payload.get("stick", "left")
                    if side not in VSTICK_SIDES:
                        side = "left"
                    x = max(-1.0, min(1.0, float(payload.get("x", 0.0))))
                    y = max(-1.0, min(1.0, float(payload.get("y", 0.0))))
                    ramp_ms = max(0, int(payload.get("ramp_ms", 0)))
                    cid = f"{macro_uid}:vs:{step_idx}"
                    if mode == "tap":
                        if ramp_ms > 0:
                            if _ramp_vstick(side, cid, 0.0, 0.0, x, y, ramp_ms, stop_event):
                                _vstick_contrib[side].pop(cid, None); return True
                            # Hold at target for the remainder of hold_ms
                            remainder = max(0, hold_ms - ramp_ms)
                            if remainder and _interruptible_sleep(remainder / 1000.0):
                                _vstick_contrib[side].pop(cid, None); return True
                        else:
                            _vstick_contrib[side][cid] = {"x": x, "y": y}
                            if _interruptible_sleep(hold_ms / 1000.0):
                                _vstick_contrib[side].pop(cid, None); return True
                        _vstick_contrib[side].pop(cid, None)
                    elif mode == "down":
                        if ramp_ms > 0:
                            # Smoothly move to target then leave it pinned
                            if _ramp_vstick(side, cid, 0.0, 0.0, x, y, ramp_ms, stop_event):
                                _vstick_contrib[side].pop(cid, None); return True
                        else:
                            _vstick_contrib[side][cid] = {"x": x, "y": y}
                        held_vsticks.add((side, cid))
                    elif mode == "up":
                        for held_side, held_cid in list(held_vsticks):
                            if held_side == side:
                                _vstick_contrib[side].pop(held_cid, None)
                                held_vsticks.discard((held_side, held_cid))
                                break

                elif kind == "vtrigger":
                    side = payload.get("trigger", "lt")
                    if side not in VTRIGGER_SIDES:
                        side = "lt"
                    value = max(0.0, min(1.0, float(payload.get("value", 1.0))))
                    cid = f"{macro_uid}:vt:{step_idx}"
                    if mode == "tap":
                        _vtrigger_contrib[side][cid] = value
                        if _interruptible_sleep(hold_ms / 1000.0):
                            _vtrigger_contrib[side].pop(cid, None); return True
                        _vtrigger_contrib[side].pop(cid, None)
                    elif mode == "down":
                        _vtrigger_contrib[side][cid] = value
                        # Track in a held-set so cleanup releases on macro end
                        held_vsticks.add(("__trigger__:" + side, cid))
                    elif mode == "up":
                        # Release any prior trigger contribution from this macro
                        for held_side, held_cid in list(held_vsticks):
                            if held_side == "__trigger__:" + side:
                                _vtrigger_contrib[side].pop(held_cid, None)
                                held_vsticks.discard((held_side, held_cid))
                                break

                elif kind == "text":
                    text = str(payload.get("text", ""))
                    per_char = max(0, int(payload.get("delay_per_char_ms", 0)))
                    for ch in text:
                        if stop_event.is_set():
                            return True
                        _send_unicode_char(ch)
                        if per_char and _interruptible_sleep(per_char / 1000.0):
                            return True

                elif kind == "mouse_move":
                    # v3.6 — relative cursor move via SendInput.  Used by
                    # the macro recorder when mouse-move sampling is on.
                    # Skips zero-deltas so we don't generate noise via
                    # SendInput on every empty-step playback.
                    dx = int(payload.get("dx", 0))
                    dy = int(payload.get("dy", 0))
                    if dx or dy:
                        _send_mouse_move(dx, dy)

                if delay_ms and _interruptible_sleep(delay_ms / 1000.0):
                    return True
            except Exception as e:
                log.debug(f"macro step {step_idx} failed: {e}")
        return False    # finished all steps without stop

    def _runner():
        try:
            iteration = 0
            while True:
                iteration += 1
                interrupted = _run_one_pass()
                if interrupted:
                    break
                # Decide whether to repeat
                if play_mode == "once":
                    break
                if play_mode == "repeat_n" and iteration >= max(1, repeat_count):
                    break
                # play_mode == "loop_while_held" → keep going until stop_event
                if stop_event.is_set():
                    break
            # ── Trigger-held freeze ───────────────────────────────────
            # If anything was Hold ↓-ed and the user is still holding the
            # trigger, freeze here so those inputs stay pressed.
            if (held_keys or held_mouse or held_vbtn or held_vsticks) \
                    and not stop_event.is_set():
                stop_event.wait()
        finally:
            _release_held()
            with _active_macros_lock:
                if _active_macros.get(origin) is stop_event:
                    del _active_macros[origin]

    threading.Thread(target=_runner, daemon=True,
                     name=f"macro-{origin}").start()


# ────────────────────────────────────────────────────────────────────
# v3.4 — unified output dispatcher
# ────────────────────────────────────────────────────────────────────
#
# Each mapping has an output specification.  This function fires the
# correct OS-level event(s) for that output on the given press / release
# transition.  Tracks held outputs per mapping ID so a controller-button-
# held-down maps to a key-held-down etc.

# id-keyed records of what we currently have "held" via SendInput so we
# can release on the matching just_up event.  Keys are mapping IDs.
_held_outputs: dict[str, dict] = {}     # mid -> {kind, vk|button|vbutton}

# Active virtual-stick contributions per side, keyed by mapping ID.
# Multiple mappings can contribute to one stick (e.g. WASD = 4 keys);
# we sum them and clamp at the end.  Each value is {x, y} in -1..1.
_vstick_contrib: dict[str, dict] = {
    "left":  {},   # mid -> {x, y}
    "right": {},
}

# Active virtual-trigger contributions, keyed by mapping ID per side.
# Each value is a float 0.0-1.0; we take the MAX across contributors
# (rather than sum) so two mappings both pulling the trigger don't
# accidentally overflow.  Final value is converted to 0-255 for vgamepad.
_vtrigger_contrib: dict[str, dict] = {
    "lt": {},      # mid -> float 0..1
    "rt": {},
}

# v3.5 — single coarse lock guarding the three contribution structures
# above.  Multiple threads write to them (polling loop, macro runners,
# turbo threads, hook-event handlers).  Iteration without a snapshot can
# raise "dictionary changed size during iteration" — at 8 kHz with
# multi-step macros that's not theoretical.  We snapshot under this lock
# before iterating in the hot snapshot builder.
_outputs_lock = threading.Lock()


def _dispatch_output(mid: str, output: dict, pressed: bool) -> None:
    """Apply (or release) the output specified by `output` for the given
    mapping ID.  Holds resources on `pressed=True`, releases on False.

    Writes to the shared contribution dicts go through `_outputs_lock`
    so the snapshot builder (called from the polling loop at up to 8
    kHz) can iterate them safely from another thread."""
    if not isinstance(output, dict):
        return
    target = output.get("target")
    if not target or target == "disabled":
        return

    if target == "key":
        vk = int(output.get("vk", 0))
        if not vk:
            return
        if pressed:
            _send_key(vk, key_up=False)
            with _outputs_lock:
                _held_outputs[mid] = {"kind": "key", "vk": vk}
        else:
            with _outputs_lock:
                held = _held_outputs.pop(mid, None)
            if held and held.get("kind") == "key":
                _send_key(held["vk"], key_up=True)

    elif target == "mouse_button":
        btn = output.get("button") or "M_LEFT"
        # Wheel events are one-shot — fire on press only
        if btn in ("M_WHEEL_UP", "M_WHEEL_DOWN"):
            if pressed:
                _send_mouse_wheel(direction_up=(btn == "M_WHEEL_UP"))
            return
        if pressed:
            _send_mouse_button(btn, key_up=False)
            with _outputs_lock:
                _held_outputs[mid] = {"kind": "mouse", "button": btn}
        else:
            with _outputs_lock:
                held = _held_outputs.pop(mid, None)
            if held and held.get("kind") == "mouse":
                _send_mouse_button(held["button"], key_up=True)

    elif target == "vbutton":
        # Tracked for the virtual-pad rebuild step below — we add the
        # button to a "pressed" set keyed by mapping ID.
        with _outputs_lock:
            if pressed:
                _held_outputs[mid] = {"kind": "vbutton",
                                       "vbutton": output.get("vbutton", "A")}
            else:
                _held_outputs.pop(mid, None)

    elif target == "vstick":
        side = output.get("stick", "left")
        if side not in VSTICK_SIDES:
            side = "left"
        # Digital-direction press → set magnitude in that axis
        direction = output.get("direction", "up")
        magnitude = float(output.get("magnitude", 1.0))
        magnitude = max(0.0, min(1.0, magnitude))
        x = y = 0.0
        if direction == "up":    y = -magnitude
        elif direction == "down":  y =  magnitude
        elif direction == "left":  x = -magnitude
        elif direction == "right": x =  magnitude
        with _outputs_lock:
            if pressed:
                _vstick_contrib[side][mid] = {"x": x, "y": y}
            else:
                _vstick_contrib[side].pop(mid, None)

    elif target == "vtrigger":
        # Drive the LT or RT analog trigger on the virtual pad.
        # `value` is 0.0..1.0 (0 = released, 1 = fully pulled).
        # While the input is held, we publish the configured value;
        # release zeroes it out via mapping-id removal.
        side = output.get("trigger", "lt")
        if side not in VTRIGGER_SIDES:
            side = "lt"
        value = float(output.get("value", 1.0))
        value = max(0.0, min(1.0, value))
        with _outputs_lock:
            if pressed:
                _vtrigger_contrib[side][mid] = value
            else:
                _vtrigger_contrib[side].pop(mid, None)

    elif target == "macro":
        if pressed:
            steps = output.get("steps") or []
            play_mode = output.get("play_mode") or "once"
            repeat_count = max(1, int(output.get("repeat_count", 1)))
            _run_macro(steps, mid,
                       play_mode=play_mode,
                       repeat_count=repeat_count)
        else:
            # v3.4 — trigger release stops the macro and releases any
            # Hold ↓ inputs it had still alive.
            _stop_macro(mid)


def _release_all_outputs():
    """Belt-and-suspenders release on profile change / mapper stop / layer
    switch.  Drops every key / mouse button we currently have held +
    clears every virtual-pad contribution dict (sticks + triggers).
    Also cancels active turbo threads, clears toggle/chord latches, and
    drops the held-inputs tracking set so a fresh layer starts cleanly."""
    # Snapshot under lock, send releases without lock (release calls
    # SendInput which can be slow; don't hold the lock across them).
    with _outputs_lock:
        held_snapshot = list(_held_outputs.items())
        _held_outputs.clear()
        _vstick_contrib["left"].clear()
        _vstick_contrib["right"].clear()
        _vtrigger_contrib["lt"].clear()
        _vtrigger_contrib["rt"].clear()
    for mid, held in held_snapshot:
        kind = held.get("kind")
        if kind == "key":
            try: _send_key(held["vk"], key_up=True)
            except Exception: pass
        elif kind == "mouse":
            try: _send_mouse_button(held["button"], key_up=True)
            except Exception: pass
    # v3.5 — cancel turbo threads, clear toggle / chord latches, drop
    # held-input tracking (a layer switch should reset everything).
    with _behaviors_lock:
        turbo_ids = list(_turbo_threads.keys())
        _toggle_state.clear()
        _chord_state.clear()
    for mid in turbo_ids:
        try: _stop_turbo(mid)
        except Exception: pass
    with _held_inputs_lock:
        _held_inputs.clear()


# v3.5 — last virtual-pad snapshot pushed by the polling loop, exposed
# to the UI so the visualizer can show "what the game sees" alongside
# "what the physical pad reports".  Updated under no lock since reads
# are best-effort tearing-tolerant.
_last_vpad_snapshot: dict = {
    "pressed": [], "lt": 0, "rt": 0,
    "lx": 0, "ly": 0, "rx": 0, "ry": 0,
    "ts": 0.0,
}


def get_last_vpad_snapshot() -> dict:
    """Return the most-recent virtual-pad snapshot.  Empty pressed list
    + zero analog values when the mapper isn't running or no profile is
    in virtual_controller mode."""
    return dict(_last_vpad_snapshot)


# v3.6 — Auto-fire bridge for the recoil-capture flow.  The capture
# module pushes a synthetic RT contribution into the virtual pad so the
# user doesn't have to physically hold their fire trigger during the
# capture window.  Implemented as a contribution-dict entry under a
# fixed `__capture__` mapping ID so the polling loop's snapshot builder
# picks it up automatically alongside the user's normal contributions.
#
# Returns False when the virtual pad isn't running (mapper disabled or
# profile not in virtual_controller mode), so the caller can fall back
# to "user fires manually".
_CAPTURE_AUTOFIRE_MID = "__capture_autofire__"

# v3.6 — last absolute mouse cursor position seen by the polling loop's
# hook-event drain.  Used to compute per-event deltas for the macro
# recorder's mouse-move sampling.  Initialised to None so the first
# event seeded the prior position rather than reporting a giant jump
# from (0, 0).
_last_mouse_xy: "tuple[int, int] | None" = None


def is_virtual_pad_active() -> bool:
    return _virtual_pad is not None


def set_capture_autofire(value: float) -> bool:
    """Set or clear the synthetic RT contribution used by the recoil-
    capture flow.  Pass 0 to release.  Returns True if the autofire was
    applied (virtual pad is up), False otherwise — caller should surface
    a user-visible "you'll need to fire manually" hint in the latter
    case."""
    if not is_virtual_pad_active():
        return False
    v = max(0.0, min(1.0, float(value or 0.0)))
    with _outputs_lock:
        if v > 0.0:
            _vtrigger_contrib["rt"][_CAPTURE_AUTOFIRE_MID] = v
        else:
            _vtrigger_contrib["rt"].pop(_CAPTURE_AUTOFIRE_MID, None)
    return True


def calibrate_sticks_at_rest(duration_sec: float = 2.0,
                              margin: float = 0.01,
                              save_to_profile: bool = True) -> dict:
    """v3.6 — sample physical XInput stick state for `duration_sec`
    while the user keeps their hands off the sticks, measure the max
    absolute magnitude observed per stick, and (optionally) write that
    plus a small margin into the active profile as the inner deadzone.

    Why this exists: even premium controllers report tiny non-zero
    stick values at rest (sub-detection wobble — a few hundred raw
    units out of 32 767).  The physical XInput driver smooths most of
    it through the documented per-axis deadzone (7849 for left, 8689
    for right), but some games apply less filtering than that and
    pick up the residual as drift.  GhostShell's snapshot pipeline
    passes the raw values through verbatim, so any wobble bleeds
    straight to the virtual pad.

    Setting an inner deadzone equal to the observed max + a tiny
    margin kills the drift at the source without losing meaningful
    precision (a real intentional stick movement is 10-100× larger
    than wobble).

    Returns the measured max magnitudes per stick (in normalised 0..1
    range) along with the deadzone values that were saved.
    """
    duration_sec = max(0.5, min(10.0, float(duration_sec or 2.0)))
    margin       = max(0.0, min(0.5, float(margin or 0.01)))

    # Read state at ~120 Hz for the duration.  We use the connected
    # slot list rather than slot 0 hardcode so the calibration follows
    # whichever slot the user's controller landed on.
    deadline = time.time() + duration_sec
    period   = 1.0 / 120.0
    max_l    = 0.0
    max_r    = 0.0
    samples  = 0
    while time.time() < deadline:
        slots = xi.get_connected_slots() or [0]
        for slot in slots:
            s = xi.get_state(slot)
            if s is None:
                continue
            lx_n = abs(s.get("lx", 0)) / 32767.0
            ly_n = abs(s.get("ly", 0)) / 32767.0
            rx_n = abs(s.get("rx", 0)) / 32767.0
            ry_n = abs(s.get("ry", 0)) / 32767.0
            # Use max-abs-axis as the per-stick magnitude proxy.  Hypot
            # is more accurate but `_apply_stick_tuning` uses hypot too,
            # and we want the deadzone we set to actually swallow ALL
            # observed positions — using the safer max-axis bound here
            # means wobble at the corner of the deadzone box (e.g.
            # x=300 y=300, hypot=424, max-axis=300) gets included.
            stick_l_mag = max(lx_n, ly_n)
            stick_r_mag = max(rx_n, ry_n)
            if stick_l_mag > max_l: max_l = stick_l_mag
            if stick_r_mag > max_r: max_r = stick_r_mag
            samples += 1
            break    # only first connected slot per tick
        time.sleep(period)

    if samples == 0:
        return {"ok": False, "err": "no controller detected during the "
                                    "calibration window — connect a pad first"}

    dz_l = max(0.0, min(0.95, max_l + margin))
    dz_r = max(0.0, min(0.95, max_r + margin))

    written = False
    if save_to_profile:
        state = _load_state()
        pname = state.get("active_profile")
        profile = state["profiles"].get(pname) if pname else None
        if profile is not None:
            sticks = profile.setdefault("sticks", {})
            for side, val in (("left", dz_l), ("right", dz_r)):
                cfg = sticks.setdefault(side, {})
                # Overwrite ONLY the inner deadzone; leave curve / invert /
                # outer-deadzone alone since those are user preferences
                # unrelated to drift.
                cfg["deadzone_in"] = round(val, 4)
                # Normalize defaults for the rest in case the profile
                # was created before the v3.5 stick-tuning migration ran.
                cfg.setdefault("deadzone_out",  1.0)
                cfg.setdefault("curve",         "linear")
                cfg.setdefault("curve_strength",1.5)
                cfg.setdefault("invert_x",      False)
                cfg.setdefault("invert_y",      False)
                cfg.setdefault("circular_gate", False)
            _save_state(state)
            written = True
            log.info(f"calibrated sticks at rest: L={dz_l:.4f} R={dz_r:.4f} "
                     f"(saved to profile '{pname}')")
        else:
            log.info(f"calibrated sticks at rest: L={dz_l:.4f} R={dz_r:.4f} "
                     "(no active profile — not saved)")

    return {
        "ok":          True,
        "samples":     samples,
        "duration_sec":duration_sec,
        "margin":      margin,
        "max_left":    round(max_l, 4),
        "max_right":   round(max_r, 4),
        "deadzone_left":  round(dz_l, 4),
        "deadzone_right": round(dz_r, 4),
        "written":     written,
    }


def measure_latency(samples: int = 50) -> dict:
    """Measure the round-trip latency from when we write a synthetic
    vbutton hold into _held_outputs to when the polling loop's next
    snapshot reflects it on the virtual pad.

    Methodology: pick a button the user almost certainly isn't holding
    (BACK / DPAD_UP unlikely to overlap real gameplay), inject it as a
    held vbutton output keyed to a synthetic mid, busy-wait until the
    next snapshot includes it, record the delta, release it, repeat.

    Returns avg / p99 / max in microseconds.  This measures GhostShell's
    internal contribution to latency only — does NOT include ViGEmBus
    queueing or USB delivery delay (which are kernel-side and not
    measurable from userland).  Use as a guide to whether the polling
    loop is hitting its target rate."""
    if not is_running():
        return {"ok": False, "err": "mapper not running"}
    state = _load_state()
    if not state.get("enabled"):
        return {"ok": False, "err": "mapper disabled — flip the master switch on"}
    pname = state.get("active_profile")
    profile = state["profiles"].get(pname) if pname else None
    if not profile or profile.get("remap_mode") != "virtual_controller":
        return {"ok": False, "err": "active profile must be in virtual_controller mode"}

    samples = max(5, min(500, int(samples or 50)))
    probe_btn = "DPAD_UP"   # rarely held during normal play, unambiguous
    probe_mid = "__latency_probe__"

    # v3.1 — pause Adaptive Tuning during the measurement window so an
    # in-flight OC step-up/down can't shift the polling-loop's effective
    # rate mid-sweep and skew p99.  Wrapped in try/except because AT is
    # advisory — a busted import shouldn't fail the measurement.
    _at_paused_here = False
    try:
        from core import adaptive_tuning
        adaptive_tuning.pause("gamepad-latency-measure")
        _at_paused_here = True
    except Exception as _e:
        log.debug(f"adaptive_tuning.pause(gamepad-latency-measure) failed: {_e}")

    try:
        deltas_us: list[int] = []
        timeout_count = 0
        for _ in range(samples):
            # Wait for current pressed state to clear our probe flag (it
            # shouldn't be there) so we don't measure a stale reflection.
            t_inject = time.perf_counter()
            # v3 — must lock the dict writes; the polling loop iterates
            # _held_outputs under _outputs_lock and a bare write here would
            # race + crash the snapshot builder at high tick rates.
            with _outputs_lock:
                _held_outputs[probe_mid] = {"kind": "vbutton", "vbutton": probe_btn}
            # Spin until snapshot reflects the press (or 50 ms timeout)
            deadline = t_inject + 0.050
            seen = False
            while time.perf_counter() < deadline:
                snap = _last_vpad_snapshot
                if probe_btn in (snap.get("pressed") or []) and snap.get("ts", 0.0) >= t_inject:
                    seen = True
                    break
            t_observed = time.perf_counter()
            # Release (locked)
            with _outputs_lock:
                _held_outputs.pop(probe_mid, None)
            if seen:
                deltas_us.append(int((t_observed - t_inject) * 1_000_000))
            else:
                timeout_count += 1
            # Brief pause between probes so we don't deadlock the loop with
            # back-to-back press/release transitions
            time.sleep(0.002)
    finally:
        if _at_paused_here:
            try:
                from core import adaptive_tuning
                adaptive_tuning.resume("gamepad-latency-measure")
            except Exception as _e:
                log.debug(f"adaptive_tuning.resume(gamepad-latency-measure) failed: {_e}")

    if not deltas_us:
        return {"ok": False, "err": "no probes observed (loop not pushing snapshots?)",
                "timeouts": timeout_count, "samples": samples}
    deltas_us.sort()
    n = len(deltas_us)
    p50 = deltas_us[n // 2]
    p99 = deltas_us[max(0, int(n * 0.99) - 1)]
    return {
        "ok":          True,
        "samples":     samples,
        "valid":       n,
        "timeouts":    timeout_count,
        "avg_us":      sum(deltas_us) // n,
        "p50_us":      p50,
        "p99_us":      p99,
        "max_us":      deltas_us[-1],
        "min_us":      deltas_us[0],
        "note":        ("This measures GhostShell's polling-loop contribution "
                        "to latency only.  Does not include the ViGEmBus driver "
                        "report delay (~0.5 ms avg for DS4 USB FS) or the game's "
                        "input poll cycle."),
    }


# v3.5 — foreground-window auto-switching helpers
_last_foreground_exe = ""
_last_foreground_check_ts = 0.0
_FOREGROUND_CHECK_INTERVAL = 1.0
_MANUAL_OVERRIDE_TTL       = 30.0   # seconds the user's manual selection sticks before auto-switch resumes


def _get_foreground_exe_name() -> str:
    """Return the lowercase filename (without path) of the process that
    owns the foreground window, or '' if it can't be read.  Cheap — one
    user32 + kernel32 call each."""
    try:
        import ctypes
        from ctypes import wintypes
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000 — works without admin
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = (ctypes.c_wchar * 260)()
            size = wintypes.DWORD(260)
            # QueryFullProcessImageNameW
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                full_path = buf.value
                return os.path.basename(full_path).lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _maybe_auto_switch_profile(state: dict) -> None:
    """Check the foreground window and switch the active profile if a
    profile's `foreground_exe` matches.  Throttled to once per second so
    it doesn't thrash the polling loop.

    Respects a 30 sec "manual override" debounce — if the user just
    explicitly picked a profile via the UI, we don't yank it out from
    under them when they alt-tab to a different window.  Touching the
    profile dropdown re-arms the debounce."""
    global _last_foreground_exe, _last_foreground_check_ts
    now = time.time()
    if now - _last_foreground_check_ts < _FOREGROUND_CHECK_INTERVAL:
        return
    _last_foreground_check_ts = now

    # Manual-selection debounce — if user picked a profile within the
    # last MANUAL_OVERRIDE_TTL seconds, leave it alone.
    manual_ts = float(state.get("active_profile_manual_ts") or 0.0)
    if manual_ts and (now - manual_ts) < _MANUAL_OVERRIDE_TTL:
        return

    exe_name = _get_foreground_exe_name()
    if not exe_name or exe_name == _last_foreground_exe:
        _last_foreground_exe = exe_name
        return
    _last_foreground_exe = exe_name
    # Find any profile whose foreground_exe matches (case-insensitive,
    # exact filename match — e.g. "cs2.exe", "valorant.exe")
    profiles = state.get("profiles") or {}
    for name, profile in profiles.items():
        target = (profile.get("foreground_exe") or "").strip().lower()
        if target and target == exe_name:
            if state.get("active_profile") != name:
                try:
                    set_active_profile(name, manual=False)
                    log.info(f"auto-switched profile → '{name}' (foreground={exe_name})")
                except Exception as e:
                    log.debug(f"auto-switch to {name} failed: {e}")
            return


# v3.5 — global state for binding behaviors
_held_inputs: "set[tuple[str, str]]" = set()      # (source, trigger) currently held
_held_inputs_lock = threading.Lock()
_toggle_state:    "dict[str, bool]" = {}          # mid → currently-toggled-on
_chord_state:     "dict[str, bool]" = {}          # mid → chord currently active
_turbo_threads:   "dict[str, threading.Event]" = {}   # mid → stop event
# v3 — Single coarse lock guarding the three behavior dicts above.
# All three are touched from BOTH the polling-loop thread (controller
# events) AND the hook-event drain (keyboard / mouse), so without a
# lock readers + writers race.  Cheap because contention is rare.
_behaviors_lock = threading.Lock()
_modifier_held    = False
_modifier_held_lock = threading.Lock()


def _active_mappings(profile: dict) -> list:
    """Return the layer of mappings that should be live right now —
    `mappings` normally, `alt_mappings` while the modifier is held."""
    global _modifier_held
    if _modifier_held and profile.get("alt_mappings"):
        return profile.get("alt_mappings") or []
    return profile.get("mappings") or []


def _start_turbo(mid: str, output: dict, hz: int) -> None:
    """Spawn an auto-press/release thread firing `output` at `hz` cycles
    per second until _stop_turbo is called for the same `mid`.  Replaces
    any prior turbo on the same mapping."""
    _stop_turbo(mid)   # cancel any existing
    stop_evt = threading.Event()
    with _behaviors_lock:
        _turbo_threads[mid] = stop_evt
    period = 1.0 / max(1, min(60, int(hz or 10)))   # 1..60 Hz
    half = period / 2.0

    def _runner():
        try:
            while not stop_evt.is_set():
                _dispatch_output(mid, output, True)
                if stop_evt.wait(half):
                    break
                _dispatch_output(mid, output, False)
                if stop_evt.wait(half):
                    break
        finally:
            # Always release on exit so we don't leave a held key
            _dispatch_output(mid, output, False)

    threading.Thread(target=_runner, daemon=True, name=f"turbo-{mid}").start()


def _stop_turbo(mid: str) -> None:
    with _behaviors_lock:
        ev = _turbo_threads.pop(mid, None)
    if ev is not None:
        ev.set()


def _reeval_chords(profile: dict) -> None:
    """Walk the active layer and update chord-binding output state based
    on whichever inputs are currently in `_held_inputs`."""
    for m in _active_mappings(profile):
        inp = m.get("input") or {}
        if inp.get("behavior") != "chord":
            continue
        partner = inp.get("chord_with") or ""
        if not partner:
            continue
        primary = (inp.get("source") or "", inp.get("trigger") or "")
        partner_key = (primary[0], partner)
        with _held_inputs_lock:
            active = (primary in _held_inputs) and (partner_key in _held_inputs)
        mid = m.get("id") or ""
        with _behaviors_lock:
            prev = _chord_state.get(mid, False)
            if active != prev:
                _chord_state[mid] = active
                fire = True
            else:
                fire = False
        if fire:
            _dispatch_output(mid, m.get("output") or {}, active)


def _check_modifier(profile: dict, source: str, trigger: str,
                    pressed: bool) -> bool:
    """If this event is the configured shift-layer modifier, update the
    layer state and return True so caller skips normal dispatch.  Returns
    False if event isn't the modifier.

    On layer transition, re-fire mappings for every physical input the
    user is currently holding so the new layer's bindings activate
    without requiring a re-press.  Without this, switching to the alt
    layer mid-press feels broken: outputs from the old layer release,
    but the new layer's mapping for the still-held button never fires.
    """
    global _modifier_held
    mod_src  = (profile.get("modifier_source")  or "").strip()
    mod_trig = (profile.get("modifier_trigger") or "").strip()
    if not mod_trig:
        return False
    if source != mod_src or trigger != mod_trig:
        return False
    with _modifier_held_lock:
        if pressed == _modifier_held:
            return True
        _modifier_held = pressed

    # Snapshot the physical inputs that are currently held BEFORE the
    # release wipes _held_inputs, so we can re-dispatch them through the
    # new layer.
    with _held_inputs_lock:
        still_held = list(_held_inputs)

    # Release everything from the layer we just left.
    _release_all_outputs()

    # Re-establish held-input tracking + re-fire each still-held input
    # against the new layer's mappings.  Skip the modifier itself since
    # _process_input would short-circuit on it.
    with _held_inputs_lock:
        for src_held, trg_held in still_held:
            if src_held == mod_src and trg_held == mod_trig:
                continue
            _held_inputs.add((src_held, trg_held))
    for src_held, trg_held in still_held:
        if src_held == mod_src and trg_held == mod_trig:
            continue
        # Walk the active layer with this input as if it had just been
        # pressed.  Skips modifier handling (we just did that) — call
        # the inner part directly.
        _dispatch_layer_for_input(profile, src_held, trg_held, True)
    # Re-eval chord bindings against the (preserved) held-input set
    _reeval_chords(profile)
    return True


def _dispatch_layer_for_input(profile: dict, source: str, trigger: str,
                              pressed: bool) -> None:
    """Walk the active layer's mappings for a single input event and
    dispatch.  Pulled out of `_process_input` so `_check_modifier` can
    re-fire held inputs against the new layer without re-entering the
    held-inputs / modifier checks."""
    for m in _active_mappings(profile):
        inp = m.get("input") or {}
        if inp.get("source") != source: continue
        if inp.get("trigger") != trigger: continue
        mid = m.get("id") or "?"
        out = m.get("output") or {}
        behavior = (inp.get("behavior") or "normal").lower()
        if behavior == "toggle":
            if pressed:
                with _behaviors_lock:
                    cur = not _toggle_state.get(mid, False)
                    _toggle_state[mid] = cur
                _dispatch_output(mid, out, cur)
        elif behavior == "turbo":
            if pressed:
                _start_turbo(mid, out, int(inp.get("turbo_hz", 10)))
            else:
                _stop_turbo(mid)
        elif behavior == "chord":
            pass    # _reeval_chords handles
        else:
            _dispatch_output(mid, out, pressed)


def _process_input(source: str, trigger: str, pressed: bool,
                   profile: dict, payload: Optional[dict] = None) -> None:
    """Unified input dispatcher with v3.5 binding behaviors.

    `payload` is set for analog inputs (e.g. mouse-move with mx/my)
    so the analog→stick handler can read the delta.  Currently
    digital-only — analog mouse-move support is a follow-up."""
    if not profile:
        return

    # 1. Track held-input set so chord re-eval has accurate data
    with _held_inputs_lock:
        if pressed:
            _held_inputs.add((source, trigger))
        else:
            _held_inputs.discard((source, trigger))

    # 2. Check shift-layer modifier — consume the event if it is the
    # modifier, otherwise fall through to normal dispatch.
    if _check_modifier(profile, source, trigger, pressed):
        return

    # 3. Walk active layer; dispatch based on per-mapping behavior
    _dispatch_layer_for_input(profile, source, trigger, pressed)

    # 4. Re-evaluate chord bindings after the held-input set changed
    _reeval_chords(profile)


def _process_transitions(slot: int, now_pressed: set[str], profile: dict,
                         skip_vbutton: bool = False):
    """v3.4-compat shim: feeds physical-controller button transitions
    into the new _process_input dispatcher.  Also feeds the macro
    recorder if a session is active — recording happens regardless of
    master-switch / foreground state so the user can record offline."""
    prev = _prev_buttons[slot]
    just_down = now_pressed - prev
    just_up   = prev - now_pressed

    # Feed macro recorder (cheap when not recording — first-line check)
    try:
        from core import macro_recorder
        for btn in just_down:
            macro_recorder.record_event("controller", btn, True)
        for btn in just_up:
            macro_recorder.record_event("controller", btn, False)
    except Exception:
        pass

    for btn in just_down:
        _process_input("controller", btn, True, profile)
    for btn in just_up:
        _process_input("controller", btn, False, profile)
    _prev_buttons[slot] = now_pressed


def _release_all_held():
    """Belt-and-suspenders: drop every held key + mouse button + vstick
    contribution.  Called when the mapper stops or the active profile
    changes."""
    # Legacy v3.1-style held_keys (still populated by old code paths)
    for btn, vks in list(_held_keys.items()):
        for vk in vks:
            try: _send_key(vk, key_up=True)
            except Exception: pass
    _held_keys.clear()
    # v3.4 unified output bookkeeping
    _release_all_outputs()


def _apply_stick_tuning(x: float, y: float, cfg: dict) -> "tuple[float, float]":
    """Take a normalized stick reading (-1..1, where +y = UP / XInput
    convention) and return the post-tuning value.

    Two execution paths depending on `circular_gate`:

    CIRCULAR (radial / anti-recoil-friendly).  Operates on magnitude so
    full-diagonal pushes give magnitude=1, not √2.  This is what most
    FPS games actually want — aim speed should be uniform regardless
    of stick direction.
        mag = hypot(x, y)
        if mag <= dz_in:  return (0, 0)
        if mag >= dz_out: m_norm = 1
        else: m_norm = (mag - dz_in) / (dz_out - dz_in)
        m_curved = curve(m_norm)
        out = (x/mag, y/mag) * m_curved

    SQUARE (per-axis / closer to native XInput).  Each axis is processed
    independently so a stick in the corner gives full X AND full Y at
    the same time — useful for racing wheels, retro emulation, or games
    that treat axes as independent.
        for axis in (x, y):
            if |axis| <= dz_in:   axis = 0
            elif |axis| >= dz_out: axis = sign(axis)
            else: axis = sign(axis) * curve((|axis| - dz_in)/(dz_out - dz_in))

    Both paths apply axis invert at the end.  All numeric inputs are
    defensively clamped so a malformed profile can't crash the loop.
    """
    # Defensive clamping — protects against malformed profile JSON
    dz_in   = max(0.0, min(0.95, float(cfg.get("deadzone_in",  0.0))))
    dz_out  = max(dz_in + 0.05, min(1.0, float(cfg.get("deadzone_out", 1.0))))
    curve   = cfg.get("curve", "linear")
    k       = max(0.1, min(5.0, float(cfg.get("curve_strength", 1.0))))
    inv_x   = bool(cfg.get("invert_x", False))
    inv_y   = bool(cfg.get("invert_y", False))
    circ    = bool(cfg.get("circular_gate", False))

    if circ:
        mag = math.hypot(x, y)
        if mag <= dz_in:
            out_x, out_y = 0.0, 0.0
        else:
            if mag >= dz_out:
                m_norm = 1.0
            else:
                m_norm = (mag - dz_in) / (dz_out - dz_in)
            m_curved = _apply_curve(m_norm, curve, k)
            ux, uy = x / mag, y / mag
            out_x = ux * m_curved
            out_y = uy * m_curved
    else:
        # Square gate — per-axis processing
        out_x = _shape_axis(x, dz_in, dz_out, curve, k)
        out_y = _shape_axis(y, dz_in, dz_out, curve, k)

    if inv_x: out_x = -out_x
    if inv_y: out_y = -out_y
    # Final clamp — invert + curve can't actually push past ±1 here, but
    # the contribution-summing in the snapshot builder might.  Belt-and-
    # suspenders: trim to range so vgamepad doesn't get a sign-extended
    # surprise from int(out * 32767).
    out_x = max(-1.0, min(1.0, out_x))
    out_y = max(-1.0, min(1.0, out_y))
    return out_x, out_y


def _shape_axis(v: float, dz_in: float, dz_out: float,
                curve: str, k: float) -> float:
    """Per-axis shaping for the square-gate path.  Mirrors the curved
    rescale of the circular path but on |v| with the original sign
    preserved at the end."""
    a = abs(v)
    if a <= dz_in:
        return 0.0
    sign = 1.0 if v >= 0.0 else -1.0
    if a >= dz_out:
        return sign
    m_norm = (a - dz_in) / (dz_out - dz_in)
    return sign * _apply_curve(m_norm, curve, k)


def _apply_curve(m: float, curve: str, k: float) -> float:
    """Map a normalized magnitude m∈[0,1] through the response curve."""
    if curve == "power":
        # m^k.  k>1 = slower near center; k<1 = faster near center.
        return m ** k
    if curve == "expo":
        # Smooth exponential — slow near center, fast near edge.  Blended
        # so f(0)=0 and f(1)=1.  Falls back to linear if k is degenerate.
        ek_max = math.exp(k) - 1.0
        if ek_max <= 1e-9:
            return m
        return (math.exp(k * m) - 1.0) / ek_max
    if curve == "s_curve":
        # Smoothstep: 3m² − 2m³.  Slower near both ends, fast mid.
        return (3.0 * m * m) - (2.0 * m * m * m)
    return m   # linear (default)


def _build_virtual_pad_snapshot(physical: dict, profile: dict) -> dict:
    """Compose the virtual-pad state to push to ViGEmBus this tick.
    Combines:
      • physical pad's pressed buttons that have NO mapping (passthrough)
      • physical pad's pressed buttons whose mapping target is "vbutton"
        — translated to the mapped virtual button
      • all "vbutton" outputs currently held via _held_outputs (e.g. a
        keyboard key bound to virtual A)
      • physical sticks/triggers (always passthrough in v3.4)
      • virtual stick contributions from _vstick_contrib (e.g. WASD
        emulating left stick)"""
    pressed = set()
    # v3 — use the active layer (primary OR alt depending on whether
    # the modifier is held).  Otherwise the snapshot's vbutton
    # passthrough / swallow decisions diverge from what the dispatcher
    # is actually firing the moment the user holds the layer modifier.
    mappings = _active_mappings(profile)

    # Bucket controller-source mappings by trigger so we can short-circuit
    # the lookup
    ctrl_map_by_trigger: dict[str, list[dict]] = {}
    for m in mappings:
        inp = m.get("input") or {}
        if inp.get("source") == "controller":
            trig = inp.get("trigger")
            if trig:
                ctrl_map_by_trigger.setdefault(trig, []).append(m)

    for btn in physical.get("pressed", []):
        ms = ctrl_map_by_trigger.get(btn)
        if not ms:
            pressed.add(btn)        # no mapping → passthrough
            continue
        # If ANY mapping for this button is vbutton/disabled, that wins
        # over passthrough.  If it's key/macro/mouse, the physical button
        # is silently swallowed (game shouldn't see it).
        for m in ms:
            out = m.get("output") or {}
            t = out.get("target")
            if t == "vbutton":
                pressed.add(out.get("vbutton") or btn)
            # else: swallow — don't add to passthrough
            # (key, mouse, macro, disabled all swallow)

    # Snapshot the contribution dicts under the lock — separate writers
    # (macro runners, turbo threads, hook handlers) modify these freely;
    # iterating without a snapshot can throw RuntimeError at high tick
    # rates.  We're OK with the values being one-tick stale; we are NOT
    # OK with the loop crashing.
    with _outputs_lock:
        held_outputs_snap = list(_held_outputs.values())
        vstick_left_vals  = list(_vstick_contrib["left"].values())
        vstick_right_vals = list(_vstick_contrib["right"].values())
        vtrigger_lt_vals  = list(_vtrigger_contrib["lt"].values())
        vtrigger_rt_vals  = list(_vtrigger_contrib["rt"].values())

    # Add vbutton outputs being held by non-controller-source mappings
    for held in held_outputs_snap:
        if held.get("kind") == "vbutton":
            pressed.add(held.get("vbutton", "A"))

    # Sticks pipeline (v3.5):
    #   physical raw  →  per-stick tuning (deadzone, curve, invert, gate)
    #                 →  add contributions (anti-recoil etc.)
    #                 →  clamp
    #                 →  optional L↔R swap
    #
    # Contribution Y-axis sign convention: contributions use SCREEN
    # convention (+y = DOWN, matching recoil presets and the vstick
    # `direction: "down"` table) but vgamepad / XInput uses MATH
    # convention (+y = UP).  We flip Y at the contrib-boundary so a
    # preset value of +0.30 actually pulls the right stick DOWN in-game.
    sticks_cfg = profile.get("sticks") or {}
    left_cfg   = sticks_cfg.get("left")  or DEFAULT_STICK_TUNING
    right_cfg  = sticks_cfg.get("right") or DEFAULT_STICK_TUNING

    # Normalize physical readings to -1..1 floats for the tuning math
    px_l = physical.get("lx", 0) / 32767.0
    py_l = physical.get("ly", 0) / 32767.0
    px_r = physical.get("rx", 0) / 32767.0
    py_r = physical.get("ry", 0) / 32767.0

    # Apply per-stick tuning (deadzone + curve + invert + gate)
    tx_l, ty_l = _apply_stick_tuning(px_l, py_l, left_cfg)
    tx_r, ty_r = _apply_stick_tuning(px_r, py_r, right_cfg)

    # Sum digital-direction contributions on top
    if vstick_left_vals:
        cx = sum(c.get("x", 0) for c in vstick_left_vals)
        cy = sum(c.get("y", 0) for c in vstick_left_vals)
        tx_l = max(-1.0, min(1.0, tx_l + cx))
        # NOTE: -cy — flips screen → XInput Y convention
        ty_l = max(-1.0, min(1.0, ty_l + (-cy)))
    if vstick_right_vals:
        cx = sum(c.get("x", 0) for c in vstick_right_vals)
        cy = sum(c.get("y", 0) for c in vstick_right_vals)
        tx_r = max(-1.0, min(1.0, tx_r + cx))
        ty_r = max(-1.0, min(1.0, ty_r + (-cy)))

    # Optional L↔R swap (after tuning + contrib so swapped binding still
    # gets correct deadzone/curve)
    if sticks_cfg.get("swap"):
        tx_l, tx_r = tx_r, tx_l
        ty_l, ty_r = ty_r, ty_l

    lx = int(tx_l * 32767)
    ly = int(ty_l * 32767)
    rx = int(tx_r * 32767)
    ry = int(ty_r * 32767)

    # Triggers — passthrough physical, plus the MAX of any non-controller
    # contributions (e.g. a key bound to "pull RT 60%").  We use max not
    # sum so two contributors don't accidentally overflow past 1.0.
    lt_phys = physical.get("lt", 0)
    rt_phys = physical.get("rt", 0)
    if vtrigger_lt_vals:
        contrib_max = max(vtrigger_lt_vals)
        lt_val = int(max(lt_phys, contrib_max * 255))
    else:
        lt_val = lt_phys
    if vtrigger_rt_vals:
        contrib_max = max(vtrigger_rt_vals)
        rt_val = int(max(rt_phys, contrib_max * 255))
    else:
        rt_val = rt_phys

    return {
        "pressed": list(pressed),
        "lt": lt_val,
        "rt": rt_val,
        "lx": lx, "ly": ly, "rx": rx, "ry": ry,
    }


_virtual_pad = None              # active VirtualPad instance, if remap_mode=virtual_controller
_virtual_pad_for_profile = None  # which profile name the current pad belongs to
_virtual_pad_type = None         # "x360" | "ds4" — recreate when profile flips this
_hidhide_last_retry_ts = 0.0     # last time we tried to (re-)engage; throttle retries to once per N seconds
_hidhide_retry_interval = 5.0
_hidhide_last_engage_err: str = ""    # most recent engage failure detail, surfaced via status

# HidHide engagement state.  Set when we cloak the physical controller
# from games; cleared on disengage so we always restore the user's prior
# HidHide configuration.
_hidhide_engaged = False
_hidhide_prior_state: dict | None = None
_hidhide_hidden_ours: list[str] = []
_hidhide_whitelisted_app: str = ""


def _hidhide_engage_if_needed(profile: dict):
    """v3.4 — automatically engages whenever the active profile is in
    virtual_controller mode.  Without HidHide cloaking the physical
    controller, games would see BOTH the virtual and physical pads at
    once and the physical input would dominate.  We hide the physical
    from every process EXCEPT GhostShell so the game sees only the
    virtual; we still get XInput visibility for state forwarding.

    No-op if:
      • HidHide isn't installed (UI surfaces an install button)
      • The active profile isn't in virtual_controller mode
      • Already engaged"""
    global _hidhide_engaged, _hidhide_prior_state
    global _hidhide_hidden_ours, _hidhide_whitelisted_app
    if _hidhide_engaged:
        return
    if not profile or profile.get("remap_mode") != "virtual_controller":
        return
    try:
        from core import hidhide
    except Exception:
        return
    if not hidhide.is_installed().get("installed"):
        log.debug("virtual_controller mode active but HidHide not installed — "
                  "games will see both physical and virtual pads")
        return

    # Find every HID/USB instance ID for the configured controller.
    instance_ids = _resolve_physical_controller_ids(profile)
    if not instance_ids:
        log.warning("hide_physical=True but no physical controller resolved")
        return

    # Path GhostShell is running from — needed to whitelist ourselves
    import sys
    app_path = os.path.abspath(sys.executable)
    if "python" in os.path.basename(app_path).lower():
        # Dev mode — vgamepad write goes through python.exe; whitelist that
        pass

    r = hidhide.engage_for_controller(instance_ids, app_path)
    global _hidhide_last_engage_err
    if r.get("ok") or r.get("hidden_ours"):
        _hidhide_engaged = True
        _hidhide_prior_state = r.get("prior_state")
        _hidhide_hidden_ours = r.get("hidden_ours") or []
        _hidhide_whitelisted_app = app_path
        _hidhide_last_engage_err = ""
        log.info(f"HidHide engaged: hid {len(_hidhide_hidden_ours)} device(s)")
    else:
        # Capture the most-detailed error info so the UI can show why
        errors = r.get("errors") or []
        _hidhide_last_engage_err = "; ".join(errors[:3]) if errors else "engage failed (no detail)"
        log.warning(f"HidHide engage failed: {_hidhide_last_engage_err}")


def _hidhide_disengage():
    """Reverse engage — called when virtual mode disables, app exits, or
    profile changes.  Always idempotent and safe to call repeatedly."""
    global _hidhide_engaged, _hidhide_prior_state
    global _hidhide_hidden_ours, _hidhide_whitelisted_app
    if not _hidhide_engaged:
        return
    try:
        from core import hidhide
        hidhide.disengage(_hidhide_prior_state or {},
                          _hidhide_hidden_ours,
                          _hidhide_whitelisted_app)
    except Exception as e:
        log.debug(f"HidHide disengage failed: {e}")
    _hidhide_engaged = False
    _hidhide_prior_state = None
    _hidhide_hidden_ours = []
    _hidhide_whitelisted_app = ""


def _resolve_physical_controller_ids(profile: dict) -> list[str]:
    """Look up every controller-class device that should be hidden from
    games while virtual-controller mode is active.  This includes:

      • The user's actual physical controller (matched by VID/PID from
        the press-button capture, if set), every interface of it.
      • Any OTHER gamepad-shaped HID device on the system that isn't
        our own ViGEmBus virtual pad — covers Razer Synapse's XInput
        wrapper, Steam Input's virtual controller, DS4Windows residue,
        x360ce shims, etc.  Each of these sits beside our ViGEmBus pad
        in XInput's slot table and outvotes (or duplicates) our input
        unless we hide it.
      • The ViGEmBus virtual pad we just created is intentionally
        EXCLUDED — hiding it would defeat the whole point.

    We deduplicate by instance_id so the final list passed to HidHide
    is clean."""
    from core import interrupt_affinity as ia
    instance_ids: list[str] = []
    seen: set[str] = set()

    def _add(iid: str):
        if iid and iid not in seen:
            seen.add(iid)
            instance_ids.append(iid)

    # 1. VID/PID match for the user's chosen controller (catches every
    #    interface — HID class view + USB interface view).
    cv = profile.get("controller_vid_pid")
    if cv:
        cv_upper = cv.upper().replace(":", "&PID_").replace("VID_", "")
        if "&PID_" in cv_upper:
            parts = cv_upper.split("&PID_")
            vid = parts[0].lstrip("VID_")
            pid = parts[1] if len(parts) > 1 else ""
        else:
            vid, pid = cv_upper, ""
        if vid and pid:
            try:
                for m in ia.find_controllers_by_vid_pid(vid, pid):
                    _add(m.get("instance_id", ""))
            except Exception as e:
                log.debug(f"VID/PID controller lookup failed: {e}")

    # 2. Every other gamepad-shaped HID device that isn't ViGEmBus.
    #    This is what catches Razer / Steam / DS4Windows virtual layers.
    try:
        for d in ia.get_controllers_to_hide_for_vigem():
            _add(d.get("instance_id", ""))
    except Exception as e:
        log.debug(f"non-vigembus controller enumeration failed: {e}")

    # 3. Last-resort fallback: if both lookups returned nothing, fall
    #    back to the legacy "hide every detected controller" path.  Keeps
    #    behavior stable on systems where the deeper enumeration somehow
    #    fails (no PowerShell, no PnP API, etc.).
    if not instance_ids:
        try:
            for d in ia.get_input_devices_grouped()["controllers"]:
                _add(d.get("instance_id", ""))
        except Exception as e:
            log.debug(f"legacy controller enumeration failed: {e}")

    return instance_ids


def _ensure_virtual_pad(remap_mode: str, profile_name: Optional[str],
                        profile: Optional[dict]):
    """Lazy-create / tear-down the ViGEmBus virtual pad based on the
    active profile's remap_mode + virtual_pad_type.  Also engages /
    disengages HidHide based on the profile's hide_physical flag."""
    global _virtual_pad, _virtual_pad_for_profile, _virtual_pad_type
    want = (remap_mode == "virtual_controller")
    have = _virtual_pad is not None
    desired_pad_type = (profile or {}).get("virtual_pad_type", "x360")
    if desired_pad_type not in ("x360", "ds4"):
        desired_pad_type = "x360"

    profile_changed = profile_name != _virtual_pad_for_profile
    pad_type_changed = have and (_virtual_pad_type != desired_pad_type)

    if want and (not have or profile_changed or pad_type_changed):
        if have:
            try: _virtual_pad.close()
            except Exception: pass
            _hidhide_disengage()
        try:
            from core import virtual_gamepad as vg
            from core import interrupt_affinity as ia
            # Pre-creation snapshot so we can identify which gamepad-class
            # devices belong to the virtual pad we're about to create.  The
            # set-diff after creation IS our virtual pad's PnP InstanceIDs.
            try:
                pre_ids = {d.get("instance_id") for d in ia.list_gamepad_class_instance_ids()
                           if d.get("instance_id")}
            except Exception:
                pre_ids = set()
            _virtual_pad = vg.create(pad_type=desired_pad_type)
            _virtual_pad_for_profile = profile_name
            _virtual_pad_type = desired_pad_type
            if _virtual_pad:
                log.info(f"virtual {desired_pad_type} pad created for profile '{profile_name}'")
                # v3 — move the 400 ms enumeration-settle wait + HidHide
                # engagement to a background thread.  Doing it inline
                # froze the polling loop for 400 ms (1600 ticks at 4 kHz),
                # which manifested as a held-input "stick" on the virtual
                # pad during profile / pad-type switches.
                def _post_create_settle(pre_ids_snap):
                    try:
                        time.sleep(0.4)
                        try:
                            post_ids = {d.get("instance_id") for d in ia.list_gamepad_class_instance_ids()
                                        if d.get("instance_id")}
                            new_ids = post_ids - pre_ids_snap
                            new_ids |= ia.get_known_vigembus_ids()
                            ia.record_known_vigembus_ids(new_ids)
                            log.info(f"recorded {len(new_ids)} ViGEmBus instance ID(s)")
                        except Exception as e:
                            log.debug(f"ViGEmBus instance-ID recording failed: {e}")
                        if profile is not None:
                            _hidhide_engage_if_needed(profile)
                    except Exception as e:
                        log.debug(f"post-pad-create settle failed: {e}")
                threading.Thread(target=_post_create_settle,
                                 args=(pre_ids,),
                                 daemon=True,
                                 name="vpad-post-create").start()
            else:
                log.warning("virtual pad creation failed — falling back to key emulation")
        except Exception as e:
            log.warning(f"virtual pad init failed: {e}")
            _virtual_pad = None
    elif want and have and not _hidhide_engaged and profile is not None:
        # v3.4 — RETRY engagement.  Initial engage might have failed
        # silently (HidHide not yet installed, controller not yet detected,
        # CLI hiccup).  Throttled to once every 5s so we don't hammer the
        # CLI from the polling loop.  Means the user can install HidHide
        # while the pad is up and have it auto-cloak within 5s.
        global _hidhide_last_retry_ts
        now = time.time()
        if now - _hidhide_last_retry_ts >= _hidhide_retry_interval:
            _hidhide_last_retry_ts = now
            _hidhide_engage_if_needed(profile)
    elif (not want) and have:
        try: _virtual_pad.close()
        except Exception: pass
        _virtual_pad = None
        _virtual_pad_for_profile = None
        _virtual_pad_type = None
        _hidhide_disengage()
        log.info("virtual pad closed (mode is now key_emulation)")


def _busy_wait_until(deadline: float):
    """Sleep almost all the way to `deadline`, then busy-wait the last
    bit.  time.sleep() granularity on Windows is ~1 ms even after
    timeBeginPeriod, so for sub-ms tick precision we have to spin
    briefly.  We cap the total spin to avoid pegging a CPU core if
    something has gone wrong."""
    now = time.perf_counter()
    rem = deadline - now
    if rem <= 0:
        return
    # Sleep for everything except the last 0.4 ms — leaves room for jitter
    coarse = rem - 0.0004
    if coarse > 0:
        time.sleep(coarse)
    # Busy-wait the last fraction
    while time.perf_counter() < deadline:
        pass


def _polling_loop():
    # Single `global` declaration for the entire function — Python won't
    # let us redeclare it after assignments in nested blocks.
    global _capture_value
    # Bump Windows timer granularity to 1 ms so sleeps in our busy-wait
    # stub don't drift by 15 ms (the default).  Released on exit.
    _set_timer_granularity(1)
    try:
        last_stats_t = time.perf_counter()
        ticks_since_stats = 0
        useful_ticks_since_stats = 0
        frame_times_us: list[int] = []

        # Track XInput packet numbers per slot so we can count "useful"
        # ticks (where the OS actually surfaced a new state).
        last_packets: dict[int, int] = {0: -1, 1: -1, 2: -1, 3: -1}

        # Resolve poll rate (per-profile, falls back to default)
        cur_poll_hz = DEFAULT_POLL_HZ
        cur_poll_interval = 1.0 / cur_poll_hz
        with _lock:
            _stats["current_poll_hz"] = cur_poll_hz
        log.info(f"gamepad mapper loop started @ {cur_poll_hz} Hz")

        next_deadline = time.perf_counter() + cur_poll_interval

        # Connected-slot cache.  At 8 kHz the cost of polling four XInput
        # slots per tick (4 IOCTLs ≈ 100-200 µs) eats most of our 125 µs
        # tick budget — and most users have a single controller.  Refresh
        # the slot list every 500 ms instead of every tick.  Hotplug
        # response is still well under 1 sec which is fine.
        _slot_cache: list[int] = list(range(4))
        _slot_cache_ts = 0.0
        _slot_cache_ttl = 0.5

        while not _stop.is_set():
            tick_start = time.perf_counter()
            try:
                state = _load_state()
                # v3.5 — auto-switch profile based on foreground exe.
                # Throttled internally to once per second; safe to call
                # every tick.  Modifies state in-place via set_active_profile.
                try:
                    _maybe_auto_switch_profile(state)
                except Exception as e:
                    log.debug(f"auto-switch check failed: {e}")
                pname = state.get("active_profile")
                profile = state["profiles"].get(pname) if pname else None
                remap_mode = (profile or {}).get("remap_mode", "key_emulation")

                # Spin up / tear down virtual pad + HidHide as profile dictates
                _ensure_virtual_pad(
                    remap_mode if (state.get("enabled") and profile) else "key_emulation",
                    pname if state.get("enabled") else None,
                    profile if state.get("enabled") else None,
                )

                # v3.4 — foreground filter REMOVED.  Mappings always fire
                # whenever the master switch is on and a profile is active.
                # Users who want context-sensitivity can use per-game profiles.
                in_foreground_ok = bool(state.get("enabled") and profile)

                # ── Adaptive poll rate ──────────────────────────────────
                # Hammering XInput at 4 kHz+ is only worth it WHEN we're
                # actively forwarding state to a virtual controller during
                # a game.  When the user is just looking at the bind UI,
                # 60 Hz is more than enough — and avoids potential issues
                # with custom-driver controllers (Razer 8K, etc.) that
                # don't always respond well to sustained ultra-high-rate
                # XInput polling.
                if in_foreground_ok and remap_mode == "virtual_controller":
                    desired = int((profile or {}).get("poll_hz", DEFAULT_POLL_HZ))
                    if desired not in SUPPORTED_POLL_HZ:
                        desired = DEFAULT_POLL_HZ
                else:
                    # Idle / bind-UI — gentle on the controller driver
                    desired = 60
                if desired != cur_poll_hz:
                    cur_poll_hz = desired
                    cur_poll_interval = 1.0 / cur_poll_hz
                    with _lock:
                        _stats["current_poll_hz"] = cur_poll_hz
                    log.info(f"gamepad poll rate -> {cur_poll_hz} Hz")

                tick_was_useful = False

                # v3.4 — drain keyboard/mouse hook events.  Each event
                # gets fed into _process_input() which fires any matching
                # mappings + populates the capture-button buffer if the
                # UI is waiting for a bind capture.
                try:
                    from core import input_hooks
                    hook_events = input_hooks.drain_events()
                except Exception:
                    hook_events = []

                # v3.5 — global mapper-toggle hotkey check.  Intercepts
                # the configured VK BEFORE it reaches mapping dispatch so
                # the same key can't accidentally fire a mapping too.
                toggle_vk = int(state.get("toggle_hotkey_vk") or 0)
                consumed_evs: set = set()  # event indices to skip in dispatch
                if toggle_vk:
                    for i, ev in enumerate(hook_events):
                        if (ev.get("source") == "keyboard"
                                and ev.get("vk") == toggle_vk
                                and ev.get("pressed")):
                            # Toggle enabled flag and persist
                            try:
                                set_enabled(not bool(state.get("enabled")))
                                log.info("mapper toggled via hotkey "
                                         f"VK_0x{toggle_vk:02X}")
                            except Exception as e:
                                log.debug(f"hotkey toggle failed: {e}")
                            consumed_evs.add(i)

                for i, ev in enumerate(hook_events):
                    if i in consumed_evs:
                        continue
                    src     = ev.get("source", "")
                    trig    = ev.get("trigger", "")
                    pressed_now = bool(ev.get("pressed", False))
                    # Macro recorder always observes (cheap when inactive).
                    # Mouse-move events get routed to the analog delta hook
                    # so the recorder can chunk them into mouse_move steps.
                    try:
                        from core import macro_recorder
                        if src == "mouse" and trig == "M_MOVE":
                            mx = int(ev.get("mx", 0))
                            my = int(ev.get("my", 0))
                            global _last_mouse_xy
                            if _last_mouse_xy is None:
                                last_x, last_y = mx, my
                            else:
                                last_x, last_y = _last_mouse_xy
                            dx, dy = mx - last_x, my - last_y
                            macro_recorder.record_mouse_move_delta(dx, dy)
                            # v3.6 — also feed into recoil_capture's
                            # counter-pull session if one is live.
                            try:
                                from core import recoil_capture as _rc
                                _rc.record_counter_pull_delta(dx, dy)
                            except Exception:
                                pass
                            _last_mouse_xy = (mx, my)
                        else:
                            macro_recorder.record_event(
                                src, trig, pressed_now, vk=ev.get("vk"))
                    except Exception:
                        pass
                    # Capture-button shortcut for the bind UI
                    if pressed_now and not _capture_event.is_set():
                        _capture_value = f"{src}:{trig}"
                        _capture_event.set()
                    # Fire any matching mappings (only when in foreground)
                    if in_foreground_ok:
                        _process_input(src, trig, pressed_now, profile, ev)

                # Refresh connected-slot cache periodically.  Avoids polling
                # 4 slots per tick — at 8 kHz that's >100 µs of pure IOCTL
                # overhead per tick, which alone breaks the 125 µs budget.
                now = time.perf_counter()
                if now - _slot_cache_ts >= _slot_cache_ttl:
                    _slot_cache = xi.get_connected_slots() or list(range(4))
                    _slot_cache_ts = now
                # If nothing's connected, still scan one slot occasionally
                # so plug-in is detected.  Empty list means rotate slot 0.
                slots_to_poll = _slot_cache if _slot_cache else [0]
                # v3.6 — track which slot in this tick is the FIRST one
                # we got a real state for, so the analog samplers fire
                # against the user's actual controller regardless of
                # which XInput slot it landed on (Razer pads etc. don't
                # always show up on slot 0).
                _first_live_slot_this_tick = None

                for slot in slots_to_poll:
                    s = xi.get_state(slot)
                    if s is None:
                        if _prev_buttons[slot]:
                            _prev_buttons[slot].clear()
                        last_packets[slot] = -1
                        # Drop stale slot from cache so we re-probe next refresh
                        _slot_cache_ts = 0.0
                        continue
                    # "Useful tick" = OS surfaced a new state for this slot
                    pkt = s.get("packet", 0)
                    if pkt != last_packets[slot] and last_packets[slot] != -1:
                        tick_was_useful = True
                    last_packets[slot] = pkt

                    pressed = set(s["pressed"])

                    # v3.6 — feed analog samples into the macro recorder
                    # + recoil-capture counter-pull session (if live)
                    # against THIS slot if it's the first live one we've
                    # seen this tick.  Avoids double-recording from
                    # multiple controllers AND avoids the "controller
                    # landed on slot 1, hardcoded slot==0 gate never
                    # fires" bug that bit counter-pull captures.
                    if _first_live_slot_this_tick is None:
                        _first_live_slot_this_tick = slot
                        try:
                            from core import macro_recorder as _mrec
                            _mrec.record_stick_sample("left",  s.get("lx", 0), s.get("ly", 0))
                            _mrec.record_stick_sample("right", s.get("rx", 0), s.get("ry", 0))
                            _mrec.record_trigger_sample("lt",  s.get("lt", 0))
                            _mrec.record_trigger_sample("rt",  s.get("rt", 0))
                        except Exception:
                            pass
                        # Counter-pull session — internally gated on
                        # session.method/source so this is a cheap no-op
                        # the rest of the time.
                        try:
                            from core import recoil_capture as _rc
                            _rc.record_counter_pull_stick(s.get("rx", 0),
                                                         s.get("ry", 0))
                        except Exception:
                            pass

                    # v3.4 — fold analog triggers (LT/RT) into the
                    # `pressed` set with hysteresis so they show up in
                    # the mapper as digital buttons.
                    lt_val = s.get("lt", 0)
                    rt_val = s.get("rt", 0)
                    prev_trig = _prev_triggers[slot]
                    # LT
                    if prev_trig["LT"]:
                        # Currently held — only release if BELOW release threshold
                        prev_trig["LT"] = lt_val >= xi.TRIGGER_RELEASE_THRESHOLD
                    else:
                        # Not held — only press if ABOVE press threshold
                        prev_trig["LT"] = lt_val >= xi.TRIGGER_PRESS_THRESHOLD
                    if prev_trig["LT"]:
                        pressed.add("LT")
                    # RT
                    if prev_trig["RT"]:
                        prev_trig["RT"] = rt_val >= xi.TRIGGER_RELEASE_THRESHOLD
                    else:
                        prev_trig["RT"] = rt_val >= xi.TRIGGER_PRESS_THRESHOLD
                    if prev_trig["RT"]:
                        pressed.add("RT")

                    if pressed - _prev_buttons[slot] and not _capture_event.is_set():
                        new_press = next(iter(pressed - _prev_buttons[slot]))
                        # Prefix with controller: so the UI knows which
                        # source the captured trigger came from
                        _capture_value = f"controller:{new_press}"
                        _capture_event.set()

                    if not in_foreground_ok:
                        if _held_keys:
                            _release_all_held()
                        _prev_buttons[slot] = pressed
                        continue

                    if remap_mode == "virtual_controller":
                        # v3.4 — push the merged virtual-pad snapshot
                        # (physical state + virtual contributions from
                        # non-controller mappings).
                        if _virtual_pad is not None:
                            snapshot = _build_virtual_pad_snapshot(s, profile)
                            _virtual_pad.set_state(snapshot, {})
                            # v3.5 — expose for visualizer / latency probe.
                            # Stamped so UI can detect stale data.
                            global _last_vpad_snapshot
                            _last_vpad_snapshot = {
                                **snapshot,
                                "ts": time.perf_counter(),
                            }
                        _process_transitions(slot, pressed, profile)
                    else:
                        _process_transitions(slot, pressed, profile)

                if tick_was_useful:
                    useful_ticks_since_stats += 1
            except Exception as e:
                log.debug(f"gamepad mapper tick failed: {e}")

            # Stats
            tick_us = int((time.perf_counter() - tick_start) * 1_000_000)
            frame_times_us.append(tick_us)
            ticks_since_stats += 1
            if (time.perf_counter() - last_stats_t) >= 1.0:
                ft = sorted(frame_times_us)
                n = len(ft)
                p99 = ft[max(0, int(n * 0.99) - 1)] if n else 0
                mx  = ft[-1] if ft else 0
                elapsed = max(0.001, time.perf_counter() - last_stats_t)
                with _lock:
                    _stats["actual_hz"]    = ticks_since_stats / elapsed
                    _stats["useful_hz"]    = useful_ticks_since_stats / elapsed
                    _stats["frame_p99_us"] = p99
                    _stats["frame_max_us"] = mx
                    _stats["ticks_total"] += ticks_since_stats
                ticks_since_stats = 0
                useful_ticks_since_stats = 0
                frame_times_us.clear()
                last_stats_t = time.perf_counter()

            # Pace next tick
            _busy_wait_until(next_deadline)
            next_deadline += cur_poll_interval
            if next_deadline < time.perf_counter() - 0.020:
                next_deadline = time.perf_counter() + cur_poll_interval
    finally:
        _release_timer_granularity(1)
        _release_all_held()
        if _virtual_pad is not None:
            try: _virtual_pad.close()
            except Exception: pass
        _hidhide_disengage()
        log.info("gamepad mapper loop exiting")


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def start() -> dict:
    global _thread
    if _thread and _thread.is_alive():
        return {"ok": True, "msg": "already running"}
    if not xi.is_available():
        return {"ok": False, "err": "XInput DLL not available on this system"}
    _stop.clear()
    _thread = threading.Thread(target=_polling_loop, daemon=True,
                               name="gamepad-mapper")
    _thread.start()
    # v3.4 — also bring up keyboard + mouse hooks so non-controller
    # mappings work + so the bind UI can capture from any source.
    try:
        from core import input_hooks
        input_hooks.start_keyboard_hook()
        input_hooks.start_mouse_hook()
    except Exception as e:
        log.warning(f"input hooks start failed: {e}")
    return {"ok": True}


def stop() -> dict:
    _stop.set()
    try:
        from core import input_hooks
        input_hooks.stop_keyboard_hook()
        input_hooks.stop_mouse_hook()
    except Exception:
        pass
    return {"ok": True}


def is_running() -> bool:
    return bool(_thread and _thread.is_alive())


def get_status() -> dict:
    state = _load_state()
    pname = state.get("active_profile")
    profile = state["profiles"].get(pname) if pname else None
    # Cheap virtual-pad capability probe (read-only — no pad creation)
    try:
        from core import virtual_gamepad as vg_mod
        # Don't actually create — just check if vgamepad imports.
        # is_available() does a full create+close which is more expensive.
        # We'll surface the full probe via a separate route.
        import importlib
        _ = importlib.import_module("vgamepad")
        vgamepad_importable = True
    except Exception:
        vgamepad_importable = False
    # v3.4 — hook + schema enum data for the UI
    try:
        from core import input_hooks
        hook_status = input_hooks.get_status()
    except Exception:
        hook_status = {"keyboard_running": False, "mouse_running": False}

    return {
        "running":            is_running(),
        "enabled":            bool(state.get("enabled")),
        "active_profile":     pname,
        "active_profile_obj": profile,
        "profiles":           list(state.get("profiles", {}).keys()),
        "connected_slots":    xi.get_connected_slots(),
        "available_buttons":  xi.BUTTON_NAMES,
        # vbutton OUTPUT options — superset of standard Xbox names so the
        # user can target DS4-only buttons (TOUCHPAD / PS) which have no
        # Xbox equivalent on their physical controller.
        "available_vbuttons": VBUTTON_NAMES,
        "xinput_available":   xi.is_available(),
        "vgamepad_importable": vgamepad_importable,
        "supported_poll_hz":  list(SUPPORTED_POLL_HZ),
        "default_poll_hz":    DEFAULT_POLL_HZ,
        "target_hz":          _stats["current_poll_hz"],
        "actual_hz":          round(_stats["actual_hz"], 1),
        "useful_hz":          round(_stats["useful_hz"], 1),
        "frame_p99_us":       _stats["frame_p99_us"],
        "frame_max_us":       _stats["frame_max_us"],
        "ticks_total":        _stats["ticks_total"],
        "hidhide_engaged":    _hidhide_engaged,
        "hidhide_hidden":     list(_hidhide_hidden_ours),
        # v3.5 — global toggle hotkey + binding behaviors enum
        "toggle_hotkey_vk":   int(state.get("toggle_hotkey_vk") or 0),
        "toggle_hotkey_name": state.get("toggle_hotkey_name") or "",
        "binding_behaviors":  list(BINDING_BEHAVIORS),
        "stick_curves":       list(STICK_CURVES),
        # v3.4 schema metadata
        "input_sources":      list(INPUT_SOURCES),
        "mouse_triggers":     list(MOUSE_TRIGGERS),
        "output_targets":     list(OUTPUT_TARGETS),
        "vstick_directions":   list(VSTICK_DIRECTIONS),
        "vstick_sides":        list(VSTICK_SIDES),
        "vtrigger_sides":      list(VTRIGGER_SIDES),
        "macro_play_modes":    list(MACRO_PLAY_MODES),
        "pad_types":           ["x360", "ds4"],
        "current_pad_type":    _virtual_pad_type,
        "hooks":              hook_status,
    }


def get_hidhide_status() -> dict:
    """Probe for HidHide kernel driver + CLI helper.  Cheap (no IOCTL)."""
    try:
        from core import hidhide
        out = dict(hidhide.is_installed())
        out["engaged"] = bool(_hidhide_engaged)
        out["last_engage_err"] = _hidhide_last_engage_err
        out["hidden_count"] = len(_hidhide_hidden_ours)
        out["hidden_devices"] = list(_hidhide_hidden_ours)
        return out
    except Exception as e:
        return {"installed": False, "err": str(e),
                "install_url": "https://github.com/nefarius/HidHide/releases/latest"}


def get_hidhide_diagnostic() -> dict:
    """Heavier probe for the UI's troubleshooting panel.  Enumerates
    every controller-class device currently visible to Windows,
    classifies each as ViGEmBus-virtual (ours, will be left visible)
    vs. everything-else (will be cloaked from games).  Lets the user
    spot interfering virtual-controller layers (Razer Synapse XInput
    wrapper, Steam Input shim, DS4Windows residue, etc.) BEFORE they
    blame the game."""
    try:
        from core import interrupt_affinity as ia
        all_pads = ia.get_all_gamepad_class_devices()
    except Exception as e:
        return {"ok": False, "err": str(e), "devices": []}
    state = _load_state()
    profile_name = state.get("active_profile")
    profile = state["profiles"].get(profile_name) if profile_name else None
    target_ids = set()
    if profile:
        try:
            target_ids = set(_resolve_physical_controller_ids(profile))
        except Exception:
            target_ids = set()
    # Annotate each device with whether the current resolver picked it
    # for hiding.  UI shows ✓ next to the ones already in the hide list.
    def _safe_upper(v):
        if v is None:
            return ""
        try:
            return str(v).upper()
        except Exception:
            return ""
    out_list = []
    for d in all_pads:
        iid = d.get("instance_id", "")
        out_list.append({
            "name":          d.get("name") or "",
            "instance_id":   iid,
            "vid":           _safe_upper(d.get("vid")),
            "pid":           _safe_upper(d.get("pid")),
            "class":         d.get("class") or "",
            "is_vigembus":   bool(d.get("is_vigembus")),
            "will_hide":     iid in target_ids,
        })
    return {
        "ok":              True,
        "devices":         out_list,
        "engaged":         bool(_hidhide_engaged),
        "currently_hidden": list(_hidhide_hidden_ours),
    }


def force_hidhide_engage() -> dict:
    """User-triggered HidHide engagement.  Bypasses the throttle and
    re-attempts engagement immediately, returning detailed diagnostics
    if it fails.  Use case: user clicked the status badge to retry."""
    global _hidhide_last_retry_ts, _hidhide_engaged
    state = _load_state()
    pname = state.get("active_profile")
    profile = state["profiles"].get(pname) if pname else None
    if not profile:
        return {"ok": False, "err": "No active profile"}
    if profile.get("remap_mode") != "virtual_controller":
        return {"ok": False, "err": "Active profile is not in virtual_controller mode"}
    try:
        from core import hidhide
    except Exception as e:
        return {"ok": False, "err": f"HidHide module load failed: {e}"}
    inst = hidhide.is_installed()
    if not inst.get("installed"):
        return {"ok": False, "err": "HidHide not installed", "install_url": inst.get("install_url")}
    # Reset state and retry
    _hidhide_engaged = False     # force the engage logic to actually run
    _hidhide_last_retry_ts = 0.0
    _hidhide_engage_if_needed(profile)
    if _hidhide_engaged:
        return {"ok": True, "engaged": True,
                "hidden_count": len(_hidhide_hidden_ours)}
    return {"ok": False,
            "err": _hidhide_last_engage_err or "Unknown engage failure",
            "engaged": False}


_recoil_presets_cache: Optional[list] = None


def get_recoil_presets() -> dict:
    """Load + return the curated recoil-pattern presets, MERGED with any
    user-captured presets from %APPDATA%/GhostShell/user_recoil_presets.json.
    User presets are tagged `is_user: true` so the UI can render an Edit
    /Delete control on them but not on the bundled curated ones.

    NOT cached because user presets can change at runtime via the capture
    workflow.  The expansion work is small (sub-millisecond) so re-running
    on every call is fine."""
    import sys
    # When bundled by PyInstaller, _MEIPASS is the temp extract dir
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates = [
        os.path.join(base_dir, "core", "recoil_presets.json"),
        os.path.join(os.path.dirname(__file__), "recoil_presets.json"),
    ]
    raw = None
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                break
            except Exception as e:
                log.debug(f"Failed to load recoil presets from {path}: {e}")
    if raw is None:
        raw = {"presets": [], "_disclaimer": ""}

    # Merge user-captured presets from APPDATA.  Marked is_user so the
    # UI can show a 'Delete' button on them.
    bundled = list(raw.get("presets") or [])
    try:
        from core import recoil_capture
        user_presets = recoil_capture.list_user_presets()
        for up in user_presets:
            up = dict(up)
            up["is_user"] = True
            bundled.append(up)
    except Exception as e:
        log.debug(f"failed to merge user presets: {e}")

    out = []
    for p in bundled:
        # If the preset uses a template (e.g. constant_pull) instead of
        # explicit step list, expand it here.  Saves typing 30 identical
        # rows in the JSON.
        if p.get("steps_template") == "constant_pull":
            mag_y = float(p.get("magnitude_y", 0.2))
            mag_x = float(p.get("magnitude_x", 0.0))
            shots = int(p.get("shots", 30))
            duration = int(p.get("duration_ms", 100))
            steps_data = [{"y": mag_y, "x": mag_x} for _ in range(shots)]
        else:
            steps_data = p.get("steps") or []
            duration = int(p.get("duration_ms", 100))

        # Auto-hold the right trigger while the recoil pattern runs so
        # the gun actually fires.  Default ON — most users bind a recoil
        # preset to a button (RB / mouse / key) and expect it to "do the
        # firing" too.  Override per-preset by setting `auto_hold_rt: false`
        # in recoil_presets.json (e.g. for a "stick adjustment only" preset
        # where the user holds RT manually).
        auto_hold_rt = bool(p.get("auto_hold_rt", True))

        # Convert each abstract {x, y} into a concrete vstick macro step.
        macro_steps = []
        if auto_hold_rt:
            # Step 1: press RT to start firing.  `mode: "down"` keeps it
            # held until the macro ends (or the trigger is released).
            macro_steps.append({
                "vtrigger": {
                    "trigger":        "rt",
                    "value":          1.0,
                    "down_ms":        0,
                    "delay_after_ms": 0,
                    "mode":           "down",
                },
            })
        for sd in steps_data:
            macro_steps.append({
                "vstick": {
                    "stick":          "right",
                    "x":              float(sd.get("x", 0.0)),
                    "y":              float(sd.get("y", 0.0)),
                    "down_ms":        duration,
                    "delay_after_ms": 0,
                    "ramp_ms":        0,
                    "mode":           "tap",
                }
            })

        out.append({
            "id":           p.get("id"),
            "name":         p.get("name"),
            "game":         p.get("game"),
            "weapon":       p.get("weapon"),
            "rpm":          p.get("rpm"),
            "shots":        len(macro_steps),
            "description":  p.get("description") or "",
            "play_mode":    p.get("play_mode") or "once",
            "duration_ms":  duration,
            "steps":        macro_steps,
            "is_user":      bool(p.get("is_user")),
            "captured_method": p.get("captured_method") or "",
            "sensitivity_scale": p.get("sensitivity_scale"),
        })
    # Note: cache intentionally NOT set — user presets can change.
    return {"presets": out, "ok": True,
            "disclaimer": raw.get("_disclaimer", "")}


def test_virtual_pad() -> dict:
    """Spawn a button-by-button test sequence on the virtual pad so the
    user can verify which Xbox→virtual mappings actually reach the game.
    Cycles through every face button + bumpers + back/start + sticks +
    triggers + dpad with a clear log line per stage.  Especially useful
    for confirming that DS4 mode's START→OPTIONS, BACK→SHARE, etc.
    translations actually fire (open joy.cpl's controller-test tab and
    watch the numbered button indicators light up in sequence).

    Doesn't depend on profile state — uses the existing virtual pad if
    one is up, else creates a temporary X360 pad."""
    from core import virtual_gamepad

    def _runner():
        own_pad = False
        global _virtual_pad
        pad = _virtual_pad
        if pad is None:
            pad = virtual_gamepad.create()
            own_pad = True
            if pad is None:
                log.warning("test: virtual pad creation failed")
                return

        pad_type = getattr(pad, "pad_type", "x360")
        # Order = "common buttons first" so the user sees something
        # interesting in the first second, even if they bail mid-test.
        # Each stage prints what's expected on the DS4 side too, since
        # that's the most common point of confusion.
        steps = [
            ("A",      "DS4 Cross"),
            ("B",      "DS4 Circle"),
            ("X",      "DS4 Square"),
            ("Y",      "DS4 Triangle"),
            ("LB",     "DS4 L1"),
            ("RB",     "DS4 R1"),
            ("BACK",   "DS4 Share"),
            ("START",  "DS4 Options"),
            ("LSTICK", "DS4 L3"),
            ("RSTICK", "DS4 R3"),
            ("DPAD_UP",    "DS4 Dpad N"),
            ("DPAD_RIGHT", "DS4 Dpad E"),
            ("DPAD_DOWN",  "DS4 Dpad S"),
            ("DPAD_LEFT",  "DS4 Dpad W"),
            # DS4-only special buttons.  Will fire on a DS4 pad and silently
            # no-op on X360 (the special-button vbutton names aren't in the
            # X360 button map).
            ("TOUCHPAD", "DS4 Touchpad press (DS4 only)"),
            ("PS",       "DS4 PS button (DS4 only)"),
        ]
        per_step_sec = 0.6
        try:
            log.info(f"Virtual-pad test: cycling all buttons on {pad_type} pad")
            for xbox_name, ds4_label in steps:
                tag = f"{xbox_name} ({ds4_label})" if pad_type == "ds4" else xbox_name
                log.info(f"  → pressing {tag}")
                pad.set_state({"pressed": [xbox_name], "lt": 0, "rt": 0,
                               "lx": 0, "ly": 0, "rx": 0, "ry": 0}, {})
                time.sleep(per_step_sec)
            # Triggers (analog) — also need testing so user sees LT/RT
            # path works alongside the digital buttons
            log.info("  → LT pulled to max (0.6s)")
            pad.set_state({"pressed": [], "lt": 255, "rt": 0,
                           "lx": 0, "ly": 0, "rx": 0, "ry": 0}, {})
            time.sleep(per_step_sec)
            log.info("  → RT pulled to max (0.6s)")
            pad.set_state({"pressed": [], "lt": 0, "rt": 255,
                           "lx": 0, "ly": 0, "rx": 0, "ry": 0}, {})
            time.sleep(per_step_sec)
            # Sticks
            log.info("  → R stick DOWN 50% (1s)")
            pad.set_state({"pressed": [], "lt": 0, "rt": 0,
                           "lx": 0, "ly": 0,
                           "rx": 0, "ry": int(-0.5 * 32767)}, {})
            time.sleep(1.0)
            log.info("  → returning to neutral")
            pad.set_state({"pressed": [], "lt": 0, "rt": 0,
                           "lx": 0, "ly": 0, "rx": 0, "ry": 0}, {})
        finally:
            if own_pad and pad is not None:
                try: pad.close()
                except Exception: pass

    threading.Thread(target=_runner, daemon=True, name="vpad-test").start()
    return {"ok": True,
            "msg": "Test sequence started — cycles every button + sticks + triggers "
                   "(~12 sec total).  Open joy.cpl's controller test tab and watch "
                   "each numbered button light up in sequence.  Especially watch "
                   "for OPTIONS (button 10 on DS4) when 'START' is announced in the log."}


def get_install_status() -> dict:
    """Full ViGEmBus + vgamepad probe.  This actually creates a virtual
    pad, so it's heavier than get_status() — call it from the UI only
    when the user opens the install banner."""
    from core import virtual_gamepad as vg_mod
    return vg_mod.is_available()


def list_profiles() -> dict:
    return _load_state()


def save_profile(name: str, profile: dict) -> dict:
    """Insert or update a profile.  Body should already be validated by
    the caller — we still defensively coerce types here."""
    if not name:
        return {"ok": False, "err": "name required"}
    state = _load_state()
    profile = dict(profile)
    profile["name"] = name
    profile.setdefault("active_only_in_game", True)
    # v3 — drop the legacy `buttons` dict.  The v3.4 migration converts
    # any old data to `mappings` on load; re-injecting an empty buttons
    # dict on every save bloated the JSON and masked the migrated state
    # for any older code path still looking at `buttons` first.
    profile.setdefault("mappings", [])
    state["profiles"][name] = profile
    _save_state(state)
    log.info(f"gamepad profile saved: {name}")
    return {"ok": True, "profile": profile}


def delete_profile(name: str) -> dict:
    state = _load_state()
    if name in state["profiles"]:
        del state["profiles"][name]
        if state.get("active_profile") == name:
            state["active_profile"] = None
        _save_state(state)
        log.info(f"gamepad profile deleted: {name}")
    return {"ok": True}


# ────────────────────────────────────────────────────────────────────
# v3.5 — profile import / export
# ────────────────────────────────────────────────────────────────────

PROFILE_EXPORT_VERSION = 1


def export_profile(name: str) -> dict:
    """Return a JSON-serializable export blob for the named profile.
    UI can pretty-print it for copy-paste or download as a .json file."""
    state = _load_state()
    profile = state["profiles"].get(name)
    if not profile:
        return {"ok": False, "err": f"profile '{name}' not found"}
    blob = {
        "_format":  "ghostshell-gamepad-profile",
        "_version": PROFILE_EXPORT_VERSION,
        "name":     name,
        "profile":  _strip_runtime_fields(profile),
    }
    return {"ok": True, "blob": blob}


def _strip_runtime_fields(profile: dict) -> dict:
    """Remove anything we don't want to ship with an export — runtime
    flags, deprecated 'buttons' dict, etc.  Keep mappings + tuning."""
    out = {}
    for k, v in profile.items():
        if k in ("buttons",):     # legacy; we don't want to round-trip this
            continue
        out[k] = v
    return out


def import_profile(blob: dict, override_name: Optional[str] = None) -> dict:
    """Insert a profile from an export blob.  If a profile with the same
    name already exists, the new one is suffixed `(imported)`,
    `(imported 2)`, etc., unless `override_name` is provided.

    Defensively validates the payload — rejects anything that isn't a
    well-formed export blob, scrubs unexpected types, drops malformed
    mapping entries instead of letting them propagate into the runtime.
    """
    if not isinstance(blob, dict):
        return {"ok": False, "err": "import payload must be a JSON object"}
    if blob.get("_format") != "ghostshell-gamepad-profile":
        return {"ok": False, "err": "not a GhostShell gamepad profile export"}
    version = blob.get("_version", 0)
    if not isinstance(version, int) or version > PROFILE_EXPORT_VERSION:
        return {"ok": False, "err": f"export was created by a newer GhostShell "
                                    f"(v{version} > supported v{PROFILE_EXPORT_VERSION})"}
    profile_data = blob.get("profile")
    if not isinstance(profile_data, dict):
        return {"ok": False, "err": "missing or invalid 'profile' field"}
    raw_name = override_name or blob.get("name") or "imported"
    if not isinstance(raw_name, str):
        return {"ok": False, "err": "profile name must be a string"}
    name = raw_name.strip()
    if not name:
        return {"ok": False, "err": "profile name required"}
    # Bound the name length so a malicious export can't blow up the UI
    if len(name) > 64:
        name = name[:64].strip() or "imported"

    # Scrub list-typed fields — drop entries that aren't dicts so the
    # runtime can't trip on `m.get(...)` against a non-dict.
    sanitized = dict(profile_data)
    dropped = 0
    for key in ("mappings", "alt_mappings"):
        arr = sanitized.get(key)
        if isinstance(arr, list):
            cleaned = []
            for m in arr:
                if not isinstance(m, dict):
                    dropped += 1
                    continue
                # Make sure input/output are dicts so downstream .get's work
                if not isinstance(m.get("input"), dict):
                    m["input"] = {}
                if not isinstance(m.get("output"), dict):
                    m["output"] = {"target": "disabled"}
                if not m.get("id"):
                    m["id"] = _new_id()
                cleaned.append(m)
            sanitized[key] = cleaned
        elif arr is not None:
            sanitized[key] = []

    state = _load_state()
    final_name = name
    if not override_name and final_name in state["profiles"]:
        final_name = f"{name} (imported)"
        n = 2
        while final_name in state["profiles"]:
            final_name = f"{name} (imported {n})"
            n += 1

    profile = _migrate_profile(sanitized)
    profile["name"] = final_name
    state["profiles"][final_name] = profile
    _save_state(state)
    log.info(f"gamepad profile imported as '{final_name}' "
             f"(dropped {dropped} malformed entries)")
    return {"ok": True, "name": final_name, "profile": profile,
            "dropped_entries": dropped}


def set_toggle_hotkey(vk: int, name: str = "") -> dict:
    """Set (or clear) the global mapper-toggle hotkey.  Pass vk=0 to
    disable.  `name` is purely for display in the UI."""
    state = _load_state()
    vk = max(0, min(255, int(vk or 0)))
    state["toggle_hotkey_vk"]   = vk
    state["toggle_hotkey_name"] = (name or "").strip()
    _save_state(state)
    log.info(f"mapper toggle hotkey set: VK_0x{vk:02X} ('{name}')")
    return {"ok": True, "vk": vk, "name": name}


def set_active_profile(name: Optional[str], manual: bool = True) -> dict:
    """Switch the active profile.  When `manual=True` (default — UI
    initiated), stamps `active_profile_manual_ts` so the foreground-exe
    auto-switcher won't override the user's choice for the next
    _MANUAL_OVERRIDE_TTL seconds.  Auto-switch itself calls with
    `manual=False` to avoid re-arming its own debounce."""
    state = _load_state()
    if name and name not in state["profiles"]:
        return {"ok": False, "err": f"profile '{name}' not found"}
    state["active_profile"] = name
    if manual:
        state["active_profile_manual_ts"] = time.time()
    _save_state(state)
    _release_all_held()
    log.info(f"gamepad active profile → {name}" + ("" if manual else " (auto)"))
    return {"ok": True, "active_profile": name}


def set_enabled(enabled: bool) -> dict:
    state = _load_state()
    state["enabled"] = bool(enabled)
    _save_state(state)
    if not enabled:
        _release_all_held()
    log.info(f"gamepad mapper enabled = {enabled}")

    # v3.4 — toggle process priority so the mapper isn't throttled when
    # actively forwarding state.  Master switch on → bump GhostShell to
    # Normal priority + clear EcoQoS so polling stays at 4 kHz.  Master
    # switch off AND no game running → drop back to efficiency.
    try:
        import sys
        main_app = sys.modules.get("app") or sys.modules.get("__main__")
        if main_app:
            if enabled and hasattr(main_app, "_set_responsive_mode"):
                main_app._set_responsive_mode()
            elif not enabled:
                # Only re-enter efficiency if no game is currently active
                try:
                    from core import game_profiles
                    info = game_profiles.get_active_game_info()
                    game_active = bool(info and info.get("exe"))
                except Exception:
                    game_active = False
                if not game_active and hasattr(main_app, "_set_efficiency_mode"):
                    main_app._set_efficiency_mode()
    except Exception as e:
        log.debug(f"priority toggle failed: {e}")

    return {"ok": True, "enabled": bool(enabled)}


def capture_next_button(timeout_sec: float = 8.0) -> dict:
    """Block (with timeout) until the user presses any controller button
    or keyboard / mouse hook event, then return its identity.  Returns
    a `value` string of the form `<source>:<trigger>` so the UI can use
    the same payload format for any binding capture.  Also pulls out the
    raw VK code (when the source is keyboard) so callers like the
    toggle-hotkey setter can store the numeric key id directly."""
    global _capture_value
    _capture_value = None
    _capture_event.clear()
    if _capture_event.wait(timeout=timeout_sec):
        value = _capture_value or ""
        source, _, trigger = value.partition(":")
        vk = 0
        if source == "keyboard" and trigger:
            try:
                from core import input_hooks
                vk = int(input_hooks.VK_BY_NAME.get(trigger.lower(), 0))
            except Exception:
                vk = 0
        return {
            "ok":      True,
            "value":   value,
            "button":  value,        # legacy field name kept for older callers
            "source":  source,
            "trigger": trigger,
            "vk":      vk,
        }
    return {"ok": False, "err": "timeout — no button pressed"}
