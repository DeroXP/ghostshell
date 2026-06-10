"""Thin ctypes wrapper around xinput1_4.dll — reads the button + analog
state of XInput slots 0-3.  Used by the gamepad mapper to drive the
remap loop without spawning the JS Gamepad API roundtrip (which would
cost ~16 ms per poll and ruin remap latency).

XInput supports up to 4 simultaneously-connected Xbox-compatible
controllers.  Most modern PC controllers run in XInput mode by default
(Xbox pads natively, DualSense via DS4Windows / Steam Input, the user's
Razer Wolverine V2 in Xbox emulation mode, Logitech F310/F710, 8BitDo
in X-Input mode, etc.).
"""

from __future__ import annotations
import ctypes
from ctypes import wintypes
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# XInput constants
# ────────────────────────────────────────────────────────────────────
ERROR_SUCCESS                  = 0
ERROR_DEVICE_NOT_CONNECTED     = 0x048F

XINPUT_GAMEPAD_DPAD_UP         = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN       = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT       = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT      = 0x0008
XINPUT_GAMEPAD_START           = 0x0010
XINPUT_GAMEPAD_BACK            = 0x0020
XINPUT_GAMEPAD_LEFT_THUMB      = 0x0040
XINPUT_GAMEPAD_RIGHT_THUMB     = 0x0080
XINPUT_GAMEPAD_LEFT_SHOULDER   = 0x0100
XINPUT_GAMEPAD_RIGHT_SHOULDER  = 0x0200
XINPUT_GAMEPAD_A               = 0x1000
XINPUT_GAMEPAD_B               = 0x2000
XINPUT_GAMEPAD_X               = 0x4000
XINPUT_GAMEPAD_Y               = 0x8000

# UI-friendly button names → XInput bitmask.  Order matters for the UI
# rendering — common buttons first.
BUTTONS: list[tuple[str, int]] = [
    ("A",          XINPUT_GAMEPAD_A),
    ("B",          XINPUT_GAMEPAD_B),
    ("X",          XINPUT_GAMEPAD_X),
    ("Y",          XINPUT_GAMEPAD_Y),
    ("LB",         XINPUT_GAMEPAD_LEFT_SHOULDER),
    ("RB",         XINPUT_GAMEPAD_RIGHT_SHOULDER),
    ("BACK",       XINPUT_GAMEPAD_BACK),
    ("START",      XINPUT_GAMEPAD_START),
    ("LSTICK",     XINPUT_GAMEPAD_LEFT_THUMB),    # click L3
    ("RSTICK",     XINPUT_GAMEPAD_RIGHT_THUMB),   # click R3
    ("DPAD_UP",    XINPUT_GAMEPAD_DPAD_UP),
    ("DPAD_DOWN",  XINPUT_GAMEPAD_DPAD_DOWN),
    ("DPAD_LEFT",  XINPUT_GAMEPAD_DPAD_LEFT),
    ("DPAD_RIGHT", XINPUT_GAMEPAD_DPAD_RIGHT),
]
# Digital-button names ONLY (matches the wButtons bitmask)
BUTTON_NAMES_DIGITAL = [name for name, _ in BUTTONS]

# Triggers — analog 0-255 in XInput, but the mapper treats them as
# digital press/release events using a threshold + hysteresis.  Adding
# them here so the UI dropdown lists them alongside the digital buttons.
TRIGGER_NAMES = ["LT", "RT"]

# Combined list — the UI uses this for the "controller trigger" picker
BUTTON_NAMES = BUTTON_NAMES_DIGITAL + TRIGGER_NAMES

# Threshold + hysteresis for digital trigger detection.  XInput's
# documented deadzone is 30/255 — we add a small release-side gap to
# stop edge-of-pull bouncing.
TRIGGER_PRESS_THRESHOLD   = 30
TRIGGER_RELEASE_THRESHOLD = 20


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons",      wintypes.WORD),
        ("bLeftTrigger",  ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX",      ctypes.c_short),
        ("sThumbLY",      ctypes.c_short),
        ("sThumbRX",      ctypes.c_short),
        ("sThumbRY",      ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad",        XINPUT_GAMEPAD),
    ]


# ────────────────────────────────────────────────────────────────────
# DLL loader — try newest version first, fall back to older copies.
# ────────────────────────────────────────────────────────────────────
_dll = None
for _name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
    try:
        _dll = ctypes.WinDLL(_name)
        break
    except OSError:
        continue


def is_available() -> bool:
    return _dll is not None


def get_state(controller_index: int) -> Optional[dict]:
    """Read state of one XInput slot (0-3).  Returns None if no
    controller plugged into that slot, else a dict:

        { "packet": int,        # increments on state change
          "buttons": int,       # raw bitmask
          "pressed": [...],     # list of button names currently pressed
          "lt": int, "rt": int, # 0-255
          "lx": int, "ly": int, # -32768..32767
          "rx": int, "ry": int }
    """
    if _dll is None:
        return None
    state = XINPUT_STATE()
    rc = _dll.XInputGetState(int(controller_index), ctypes.byref(state))
    if rc == ERROR_DEVICE_NOT_CONNECTED:
        return None
    if rc != ERROR_SUCCESS:
        return None
    g = state.Gamepad
    pressed = [name for name, mask in BUTTONS if g.wButtons & mask]
    return {
        "packet":  int(state.dwPacketNumber),
        "buttons": int(g.wButtons),
        "pressed": pressed,
        "lt":      int(g.bLeftTrigger),
        "rt":      int(g.bRightTrigger),
        "lx":      int(g.sThumbLX),
        "ly":      int(g.sThumbLY),
        "rx":      int(g.sThumbRX),
        "ry":      int(g.sThumbRY),
    }


def get_connected_slots() -> list[int]:
    """Return the indices (0-3) of every currently-plugged controller."""
    return [i for i in range(4) if get_state(i) is not None]
