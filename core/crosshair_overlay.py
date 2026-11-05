"""Crosshair overlay: draws the crosshair in its own see-through, click-through
window above the game. The window never takes focus and never touches the game
(no injection, no hooks into the game, no keyboard or mouse hooks).

One thread owns the window and runs its message loop. Other threads only store
work and wake it. Every Win32 call goes through the module-level adapter `_win`
(class Win32, private WinDLLs), so tests can swap in a fake.
"""
from __future__ import annotations

import copy
import ctypes
import logging
import math
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes as W

from core.utils import get_logger

log = get_logger(__name__)

# ── window, message and API constants ─────────────────────────────────
CLASS_NAME = "VisporaCrosshairOverlay"
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE

SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
SWP_SHOWWINDOW, SWP_NOOWNERZORDER, SWP_NOSENDCHANGING = 0x0040, 0x0200, 0x0400
TOPMOST_FLAGS = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING
MOVE_FLAGS = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING
HWND_TOPMOST = -1
GW_HWNDPREV = 3

WM_DESTROY, WM_CLOSE, WM_QUIT = 0x0002, 0x0010, 0x0012
WM_MOUSEACTIVATE, WM_DISPLAYCHANGE, WM_NCHITTEST = 0x0021, 0x007E, 0x0084
WM_USER, WM_WTSSESSION_CHANGE, WM_DPICHANGED = 0x0400, 0x02B1, 0x02E0
MA_NOACTIVATE, HTTRANSPARENT = 3, -1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
NOTIFY_FOR_THIS_SESSION = 0
PM_NOREMOVE, PM_REMOVE = 0, 1

WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0x0, 0x102, 0xFFFFFFFF
QS_ALLINPUT, MWMO_INPUTAVAILABLE = 0x04FF, 0x0004
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS = 0x00000002, 0x001F0003

BI_RGB = DIB_RGB_COLORS = 0
AC_SRC_OVER, AC_SRC_ALPHA, ULW_ALPHA = 0x00, 0x01, 0x00000002

MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
THREAD_PRIORITY_HIGHEST = 2
THREAD_POWER_THROTTLING = 3                 # THREAD_INFORMATION_CLASS ThreadPowerThrottling
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0x0000
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x00, 0x11
CAPTURE_EXCLUSION_MIN_BUILD = 19041         # Windows 10 2004
CURSOR_SHOWING, CURSOR_SUPPRESSED = 0x1, 0x2
SM_SWAPBUTTON = 23
GR_GDIOBJECTS = 0
ERROR_CLASS_ALREADY_EXISTS = 1410

VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2 = 0x01, 0x02, 0x04, 0x05, 0x06
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
VK_OEM_1, VK_OEM_PLUS, VK_OEM_COMMA, VK_OEM_MINUS, VK_OEM_PERIOD, VK_OEM_2 = 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF
VK_OEM_3, VK_OEM_4, VK_OEM_5, VK_OEM_6, VK_OEM_7 = 0xC0, 0xDB, 0xDC, 0xDD, 0xDE
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008

# ── timing ────────────────────────────────────────────────────────────
MIN_WAIT_MS = 5            # the loop never waits less than this, so it cannot spin
TICK_AIM_MS = 10           # hide-while-aiming needs a quick reaction
TICK_HOTKEY_MS = 33
TICK_IDLE_MS = 100
FOREGROUND_EVERY_S = 0.1
TOPMOST_EVERY_S = 0.5
START_TIMEOUT_S = 3.0
MAX_SIDE = 1024            # the renderer caps at 511; anything bigger is a bug, not a crosshair

# Shell windows are never "a game seen in front".
_SHELL_EXES = frozenset({
    "explorer.exe", "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "lockapp.exe", "textinputhost.exe", "applicationframehost.exe",
})

_DEFAULT_CFG = {
    "enabled": False, "show_mode": "games", "active_preset": "", "designs": {}, "images": {},
    "render": None, "game_rules": {}, "blocked_exes": frozenset(), "own_exes": frozenset(),
    "is_known_game": None, "anchor": "game", "monitor": "auto", "offset_x": 0, "offset_y": 0,
    "hide_on_aim": "off", "aim_button": "mouse2", "hide_with_cursor": False,
    "hotkeys": {"toggle": "", "next": "", "prev": ""}, "exclude_from_capture": False,
}


# ═════════════════════════════════════════════════════════════════════
# Hotkey grammar (shared with core/crosshair.py and the UI)
# ═════════════════════════════════════════════════════════════════════
HOTKEY_NAMES = ("toggle", "next", "prev")
HOTKEY_LABELS = {"toggle": "Show or hide", "next": "Next crosshair", "prev": "Previous crosshair"}
MODIFIERS = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))
_MOD_LOOKUP = {name.lower(): (name, bit) for name, bit in MODIFIERS}


def _hotkey_keys() -> dict:
    # A token names a Windows virtual key, not a physical key position. The UI
    # builds tokens from KeyboardEvent.keyCode, which is the virtual key after
    # the keyboard layout (the same code GetAsyncKeyState sees), so on AZERTY
    # "A" is the key labelled A. The punctuation tokens are the VK_OEM_* codes,
    # named after what those keys type on a US layout.
    # No Pause: it sends press and release together, so polling never sees it.
    keys = {c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
    keys.update({f"F{n}": 0x6F + n for n in range(1, 25)})
    keys.update({f"Num{n}": 0x60 + n for n in range(10)})
    keys.update({
        "NumAdd": 0x6B, "NumSub": 0x6D, "NumMul": 0x6A, "NumDiv": 0x6F, "NumDot": 0x6E,
        "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
        "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
        "Space": 0x20, "Tab": 0x09, "Enter": 0x0D, "ScrollLock": 0x91, "PrintScreen": 0x2C,
        "Minus": VK_OEM_MINUS, "Equals": VK_OEM_PLUS, "Comma": VK_OEM_COMMA, "Period": VK_OEM_PERIOD,
        "Slash": VK_OEM_2, "Semicolon": VK_OEM_1, "Quote": VK_OEM_7, "LeftBracket": VK_OEM_4,
        "RightBracket": VK_OEM_6, "Backslash": VK_OEM_5, "Backquote": VK_OEM_3,
        "Mouse3": VK_MBUTTON, "Mouse4": VK_XBUTTON1, "Mouse5": VK_XBUTTON2,
    })
    return keys


HOTKEY_KEYS = _hotkey_keys()                       # canonical main token -> virtual-key code
_KEY_LOOKUP = {k.lower(): k for k in HOTKEY_KEYS}
# Keys that work on their own: nothing a game or chat box needs is in here.
BARE_KEYS = frozenset([f"F{n}" for n in range(13, 25)] + [f"Num{n}" for n in range(10)] + [
    "ScrollLock", "Insert", "Home", "End", "PageUp", "PageDown",
    "NumAdd", "NumSub", "NumMul", "NumDiv", "NumDot", "Mouse3", "Mouse4", "Mouse5"])
# With Num Lock on, Shift turns these into Home, End, the arrows and so on (and
# Windows lets go of Shift first), so Shift plus one of them can never be seen.
_NO_SHIFT_KEYS = frozenset([f"Num{n}" for n in range(10)] + ["NumDot"])


def parse_hotkey(s) -> tuple:
    """"ctrl+alt+x" -> ({"text": "Ctrl+Alt+X", "key": "X", "vk": 0x58, "mods": MOD_CONTROL | MOD_ALT,
    "modifiers": ("Ctrl", "Alt")}, None). An empty string means "not set": (None, None).
    Anything invalid: (None, a plain sentence)."""
    if s is None:
        return None, None
    if not isinstance(s, str):
        return None, "A hotkey must be written like Ctrl+Alt+X."
    text = s.strip()
    if not text:
        return None, None
    if len(text) > 64:
        return None, "That hotkey is too long."
    if not text.isascii():
        return None, "Use plain keys, like Ctrl+Alt+X."
    mods, key = 0, None
    for part in (p.strip() for p in text.split("+")):
        if not part:
            return None, "That hotkey has an empty part. Write it like Ctrl+Alt+X."
        low = part.lower()
        if low in _MOD_LOOKUP:
            name, bit = _MOD_LOOKUP[low]
            if mods & bit:
                return None, f"{name} is in that hotkey twice."
            mods |= bit
            continue
        canon = _KEY_LOOKUP.get(low)
        if canon is None:
            return None, f"{part} isn't a key Vispora can use."
        if key is not None:
            return None, "Use one key plus Ctrl, Alt, Shift or Win, like Ctrl+Alt+X."
        key = canon
    if key is None:
        return None, "Add a key after the modifiers, like Ctrl+Alt+X."
    if not mods and key not in BARE_KEYS:
        return None, f"{key} needs Ctrl, Alt, Shift or Win with it."
    if mods & MOD_SHIFT and key in _NO_SHIFT_KEYS:
        return None, f"Shift doesn't work with {key}. Use Ctrl, Alt or Win instead."
    names = tuple(n for n, bit in MODIFIERS if mods & bit)
    return {"text": "+".join(names + (key,)), "key": key, "vk": HOTKEY_KEYS[key],
            "mods": mods, "modifiers": names}, None


def format_hotkey(spec) -> str:
    """The canonical text of a parsed hotkey ("" for None). A string is parsed first."""
    if isinstance(spec, str):
        spec = parse_hotkey(spec)[0]
    if not isinstance(spec, dict) or spec.get("key") not in HOTKEY_KEYS:
        return ""
    mods = int(spec.get("mods") or 0)
    return "+".join(tuple(n for n, bit in MODIFIERS if mods & bit) + (spec["key"],))


def current_modifiers(down) -> int | None:
    """MOD_* bits held right now, or None while AltGr (left Ctrl plus right Alt,
    which Windows reports for AltGr) is down: it never counts as Ctrl+Alt."""
    lctrl, ralt = down(VK_LCONTROL), down(VK_RMENU)
    if lctrl and ralt:
        return None
    mods = 0
    if lctrl or down(VK_RCONTROL):
        mods |= MOD_CONTROL
    if ralt or down(VK_LMENU):
        mods |= MOD_ALT
    if down(VK_LSHIFT) or down(VK_RSHIFT):
        mods |= MOD_SHIFT
    if down(VK_LWIN) or down(VK_RWIN):
        mods |= MOD_WIN
    return mods


class HotkeyPoller:
    """Edge detection for the polled hotkeys: a binding fires once, when its
    main key goes down while exactly its modifiers are held. Holding the key
    (auto-repeat) never fires again."""

    def __init__(self):
        self.bindings = {}      # name -> parsed spec
        self.prev = {}          # name -> main key down at the last poll (None: not polled yet)

    def set_bindings(self, hotkeys) -> dict:
        """Parse {"toggle": str, "next": str, "prev": str}. Returns the per-binding
        status {name: {"ok": bool, "err": str | None}}."""
        hotkeys = hotkeys if isinstance(hotkeys, dict) else {}
        bindings, status, seen = {}, {}, {}
        for name in HOTKEY_NAMES:
            spec, err = parse_hotkey(hotkeys.get(name) or "")
            if spec is not None:
                combo = (spec["mods"], spec["vk"])
                if combo in seen:
                    spec, err = None, f"Same as the {HOTKEY_LABELS[seen[combo]]} hotkey."
                else:
                    seen[combo] = name
                    bindings[name] = spec
            status[name] = {"ok": err is None, "err": err}
        self.prev = {n: self.prev.get(n) for n in bindings if self.bindings.get(n) == bindings[n]}
        self.bindings = bindings
        return status

    def poll(self, down, eligible: bool) -> list:
        """down(vk) -> bool. Returns the names that fired on this poll."""
        fired, mods = [], -1
        for name, spec in self.bindings.items():
            is_down = bool(down(spec["vk"]))
            was = self.prev.get(name)
            self.prev[name] = is_down
            if is_down and was is False and eligible:
                if mods == -1:
                    mods = current_modifiers(down)
                if mods is not None and mods == spec["mods"]:
                    fired.append(name)
        return fired


class AimState:
    """Hide while aiming. "hold": hidden while the button is down. "toggle": each
    press flips; reset() puts it back to "not aiming"."""

    def __init__(self):
        self.aiming = False
        self.prev = None

    def reset(self) -> None:
        self.aiming = False
        self.prev = None        # the next poll only records the button, so a held button doesn't flip

    def update(self, mode: str, is_down: bool) -> bool:
        if mode == "hold":
            self.aiming = bool(is_down)
        elif mode == "toggle":
            if is_down and self.prev is False:
                self.aiming = not self.aiming
        else:
            self.aiming = False
        self.prev = bool(is_down)
        return self.aiming


def aim_vk(button: str, swapped: bool) -> int:
    """The virtual key to poll. GetAsyncKeyState reads physical buttons, so with
    the buttons swapped in Windows the physical right button is VK_LBUTTON."""
    if button == "mouse3":
        return VK_MBUTTON
    if button == "mouse4":
        return VK_XBUTTON1
    if button == "mouse5":
        return VK_XBUTTON2
    return VK_LBUTTON if swapped else VK_RBUTTON


def cursor_visible(ok, flags, hcursor) -> bool:
    """GetCursorInfo's answer: the pointer is on screen (a game hides it in play)."""
    try:
        flags = int(flags or 0)
    except (TypeError, ValueError):
        return False
    return bool(ok) and bool(flags & CURSOR_SHOWING) and not (flags & CURSOR_SUPPRESSED) and bool(hcursor)


# ═════════════════════════════════════════════════════════════════════
# Pure decisions: visibility, position, design
# ═════════════════════════════════════════════════════════════════════
def _known(inp: dict, exe: str) -> bool:
    if not exe:
        return False
    if "known_game" in inp:
        return bool(inp["known_game"])
    fn = inp.get("is_known_game")
    try:
        return bool(fn(exe)) if callable(fn) else False
    except Exception:
        return False


def decide(inp: dict) -> tuple:
    """(visible, reason) for the window in front; the first matching rule wins.
    inp keys: enabled, locked, hidden, exe, own (our own process is in front),
    own_exes, show_mode, testing, game_rules, blocked_exes, known_game (or the
    is_known_game callable), exclusive_exes, aiming, hide_with_cursor, cursor_visible."""
    if not inp.get("enabled"):
        return False, "disabled"
    if inp.get("locked"):
        return False, "locked"
    if inp.get("hidden"):
        return False, "hidden_by_hotkey"
    exe = str(inp.get("exe") or "").lower()
    testing = bool(inp.get("testing"))
    mode = inp.get("show_mode")
    if mode not in ("games", "always", "list"):
        mode = "games"
    own = bool(inp.get("own")) or (bool(exe) and exe in (inp.get("own_exes") or ()))
    if own and mode != "always" and not testing:
        return False, "vispora_in_front"
    rule = (inp.get("game_rules") or {}).get(exe) if exe else None
    rule_mode = rule.get("mode") if isinstance(rule, dict) else None
    if rule_mode == "hide":
        return False, "rule_hidden"
    if exe and exe in (inp.get("blocked_exes") or ()) and rule_mode != "show":
        return False, "blocked_game"
    if not testing:
        if mode == "games" and rule_mode != "show" and not _known(inp, exe):
            return False, "no_game"
        if mode == "list" and rule_mode != "show":
            return False, "not_in_list"
    if exe and exe in (inp.get("exclusive_exes") or ()):
        return False, "exclusive_fullscreen"
    if inp.get("aiming"):
        return False, "aiming"
    if inp.get("hide_with_cursor") and inp.get("cursor_visible"):
        return False, "cursor_visible"
    return True, ("testing" if testing else "shown")


def place(inp: dict) -> dict | None:
    """The target monitor and the aim point on screen (physical pixels).
    inp: anchor ("game" | "monitor"), monitor ("auto" | szDevice), monitors
    (list_monitors() rows), fg_monitor (szDevice holding the window in front),
    game_center ((x, y) client-area centre of the game in front, or None).
    Returns {"monitor": row, "centre": (x, y)}, or None with no monitors."""
    monitors = [m for m in (inp.get("monitors") or [])
                if isinstance(m, dict) and m.get("width") and m.get("height")]
    if not monitors:
        return None
    by_id = {m.get("id"): m for m in monitors}
    primary = next((m for m in monitors if m.get("primary")), monitors[0])
    fg_mon = by_id.get(inp.get("fg_monitor"))
    centre = inp.get("game_center")
    if inp.get("anchor", "game") != "monitor" and centre is not None:
        return {"monitor": fg_mon or primary, "centre": (int(centre[0]), int(centre[1]))}
    pref = inp.get("monitor") or "auto"
    mon = by_id.get(pref) if pref != "auto" else None
    if mon is None:                         # "auto", or a display that is gone
        mon = fg_mon or primary
    return {"monitor": mon, "centre": (int(mon.get("left", 0)) + int(mon["width"]) // 2,
                                       int(mon.get("top", 0)) + int(mon["height"]) // 2)}


def window_origin(centre, size, offset) -> tuple:
    """Top-left of the window that puts the image's centre pixel (w//2, h//2) on
    centre + offset."""
    return (int(centre[0]) - int(size[0]) // 2 + int(offset[0]),
            int(centre[1]) - int(size[1]) // 2 + int(offset[1]))


def _as_int(v, default: int) -> int:
    try:
        return max(-10000, min(10000, int(v)))
    except (TypeError, ValueError):
        return default


def effective_offset(cfg: dict, exe: str) -> tuple:
    """The global offset; a game rule's offset_x / offset_y replace it when set."""
    ox, oy = _as_int(cfg.get("offset_x"), 0), _as_int(cfg.get("offset_y"), 0)
    rule = (cfg.get("game_rules") or {}).get(exe) if exe else None
    if isinstance(rule, dict):
        if rule.get("offset_x") is not None:
            ox = _as_int(rule["offset_x"], ox)
        if rule.get("offset_y") is not None:
            oy = _as_int(rule["offset_y"], oy)
    return ox, oy


def pick_design(cfg: dict, exe: str) -> tuple:
    """(preset id, design) for the game in front: its rule's preset when that
    preset is in cfg["designs"], else the active preset. The design is None when
    the active preset isn't in designs either."""
    designs = cfg.get("designs") or {}
    rule = (cfg.get("game_rules") or {}).get(exe) if exe else None
    pid = rule.get("preset") if isinstance(rule, dict) else None
    if pid and pid in designs:
        return pid, designs[pid]
    active = cfg.get("active_preset") or ""
    return active, designs.get(active)


def _premultiplied_bgra(img) -> bytes:
    """Top-down rows of B, G, R, A with colour already multiplied by alpha: what
    UpdateLayeredWindow with AC_SRC_ALPHA expects."""
    if img.mode not in ("RGBA", "RGBa"):
        img = img.convert("RGBA")
    return img.convert("RGBa").tobytes("raw", "BGRa")


def _err_text(e: BaseException, fallback: str) -> str:
    return str(e).strip() or fallback


# ═════════════════════════════════════════════════════════════════════
# Module state (one overlay at a time)
# ═════════════════════════════════════════════════════════════════════
_lock = threading.RLock()        # guards the state below; never held across window calls
_life = threading.Lock()         # serialises start() and stop()
_eng = None                      # the running _Overlay
_thread = None
_cfg = dict(_DEFAULT_CFG)
_cfg_seq = 0
_rt = {"hidden": False, "test_until": 0.0, "exclusive": frozenset()}     # runtime, not persisted
_last_games: list = []
_status = None
_caps = None
_class_owner = None              # the adapter the window class was registered with
on_hotkey = None                 # callable(name) for "next" / "prev"; runs on its own short thread
_log_q: queue.SimpleQueue = queue.SimpleQueue()
_log_thread = None


def _tlog(level: int, msg: str, *args, exc_info: bool = False) -> None:
    """Log without touching the disk on the calling thread (the overlay thread does
    no disk I/O): a daemon thread writes the record."""
    global _log_thread
    _log_q.put((level, msg, args, sys.exc_info() if exc_info else None))
    with _lock:
        if _log_thread is None or not _log_thread.is_alive():
            _log_thread = threading.Thread(target=_log_writer, name="crosshair-log", daemon=True)
            _log_thread.start()


def _log_writer() -> None:
    while True:
        level, msg, args, info = _log_q.get()
        try:
            log.log(level, msg, *args, exc_info=info)
        except Exception:
            pass


def _key_down(vk: int) -> bool:
    try:
        return bool(int(_win.GetAsyncKeyState(vk)) & 0x8000)
    except Exception:
        return False


def _fire_hotkey(name: str) -> None:
    cb = on_hotkey
    if cb is None:
        return

    def run():
        try:
            cb(name)
        except Exception:
            log.exception("crosshair: hotkey %s handler failed", name)

    threading.Thread(target=run, name="crosshair-hotkey", daemon=True).start()


def _signal() -> None:
    """Wake the overlay thread. Under the lock, so the handle can't be closed under us."""
    with _lock:
        eng = _eng
        handle = eng.wake if eng is not None else 0
        if handle:
            try:
                _win.SetEvent(handle)
            except Exception:
                pass


def _wndproc(hwnd, msg, wparam, lparam):
    """The window procedure. Always returns an int; never lets an exception out."""
    try:
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_NCHITTEST:
            return HTTRANSPARENT
        if msg == WM_CLOSE:
            return 0                    # only stop() destroys this window
        eng = _eng
        if eng is not None and hwnd and eng.hwnd and int(hwnd) == eng.hwnd:
            r = eng.on_message(int(msg), int(wparam or 0), int(lparam or 0))
            if r is not None:
                return int(r)
        if msg == WM_DPICHANGED:
            return 0                    # sized in physical pixels: ignore the suggested rect
        return int(_win.DefWindowProcW(hwnd, msg, wparam, lparam) or 0)
    except BaseException:
        try:
            _tlog(logging.ERROR, "crosshair: window procedure failed (msg 0x%04X)", int(msg or 0), exc_info=True)
        except BaseException:
            pass
        return 0


def _on_foreground_event(hook, event, hwnd, id_object, id_child, thread_id, time_ms):
    """EVENT_SYSTEM_FOREGROUND (out of context, delivered by our own message loop)."""
    try:
        eng = _eng
        if eng is not None:
            eng.fg_dirty = True
    except BaseException:
        pass
    return 0


def _ensure_class() -> None:
    """Register the window class once per process (per adapter) and keep it."""
    global _class_owner
    with _lock:
        if _class_owner is _win:
            return
    atom = _win.RegisterClassExW(CLASS_NAME, _wndproc)
    if not atom:
        raise OSError(f"Couldn't register the crosshair window (error {_win.GetLastError()}).")
    with _lock:
        _class_owner = _win


# ═════════════════════════════════════════════════════════════════════
# The overlay thread
# ═════════════════════════════════════════════════════════════════════
class _Overlay:
    """Everything here runs on the overlay thread, except the fields start(),
    stop() and _signal() touch under _lock (quit_requested, wake)."""

    def __init__(self):
        self.ready = threading.Event()
        self.quit_requested = False
        self.quit = False
        self.destroyed = False
        self.hwnd = 0
        self.wake = 0
        self.timer = 0
        self.timer_kind = "normal"
        self.hook = 0
        self.wts = False
        self.cfg = dict(_DEFAULT_CFG)
        self.cfg_seq = -1
        self.hotkeys = HotkeyPoller()
        self.hk_status = self.hotkeys.set_bindings({})
        self.aim = AimState()
        self.locked = False
        self.fg_hwnd = 0
        self.fg_exe = ""
        self.fg_own = False
        self.fg_monitor = None
        self.game_center = None
        self.fg_dirty = True
        self.next_fg = 0.0
        self.monitors = None
        self.eligible = False
        self.cursor_visible = False
        self.shown = False
        self.pos = None
        self.size = None
        self.render_src = None          # (design, target height, image object) the window shows
        self.failed_src = None          # the same for a render that raised: never retried as is
        self.draw_retry_at = 0.0        # Windows refused the drawing: try again after this
        self.draw_backoff = 0.0
        self.target = None
        self.preset_id = ""
        self.game_exe = ""
        self.capture_want = None
        self.capture_excluded = False
        self.last_topmost = 0.0
        self.topmost_fg = None
        self.period_ms = TICK_IDLE_MS
        self.last_tick = 0.0
        self.next_tick = 0.0
        self.early = 0
        self.wait_failures = 0
        self.reason = "not_running"
        self.since = time.time()
        self.last_error = None
        self.published = None

    # ---- life cycle ----
    def run(self) -> None:
        try:
            self._setup()
        except Exception as e:
            self.last_error = _err_text(e, "The crosshair window didn't open.")
            _tlog(logging.WARNING, "crosshair: overlay didn't start: %s", self.last_error)
            self._teardown()
            self.ready.set()
            return
        self.ready.set()
        try:
            self._loop()
        except Exception as e:
            self.last_error = _err_text(e, "The crosshair stopped unexpectedly.")
            _tlog(logging.ERROR, "crosshair: overlay loop failed", exc_info=True)
        finally:
            self._teardown()

    def _setup(self) -> None:
        w = _win
        # A window's DPI awareness is fixed by its thread's context at creation.
        if not w.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            w.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)
        if not w.SetThreadPriority(THREAD_PRIORITY_HIGHEST):
            _tlog(logging.DEBUG, "crosshair: thread priority not raised")
        try:
            w.disable_power_throttling()     # best effort: no EcoQoS for this thread
        except Exception:
            pass
        w.ensure_message_queue()
        wake = w.CreateEventW()
        if not wake:
            raise OSError(f"Couldn't create the crosshair's wake event (error {w.GetLastError()}).")
        with _lock:
            self.wake = int(wake)
        self.timer = int(w.CreateWaitableTimerExW(True) or 0)
        self.timer_kind = "high_res" if self.timer else "normal"
        if not self.timer:
            self.timer = int(w.CreateWaitableTimerExW(False) or 0)
        _ensure_class()
        hwnd = w.CreateWindowExW(OVERLAY_EX_STYLE, CLASS_NAME, "", WS_POPUP, 0, 0, 1, 1)
        if not hwnd:
            raise OSError(f"Couldn't create the crosshair window (error {w.GetLastError()}).")
        self.hwnd = int(hwnd)
        self._drain()                        # the configured settings, capture exclusion included
        self._apply_capture()
        self.wts = bool(w.WTSRegisterSessionNotification(self.hwnd))
        self.hook = int(w.SetWinEventHook(_on_foreground_event) or 0)
        self._set_period(self.period_ms, force=True)

    def _teardown(self) -> None:
        global _eng, _status
        w = _win
        if self.hook:
            try:
                w.UnhookWinEvent(self.hook)
            except Exception:
                _tlog(logging.ERROR, "crosshair: unhook failed", exc_info=True)
            self.hook = 0
        if self.hwnd:
            try:
                if self.wts:
                    w.WTSUnRegisterSessionNotification(self.hwnd)
            except Exception:
                _tlog(logging.ERROR, "crosshair: session notification not removed", exc_info=True)
            self.wts = False
            try:
                if self.shown:
                    w.ShowWindow(self.hwnd, SW_HIDE)
                w.DestroyWindow(self.hwnd)
            except Exception:
                _tlog(logging.ERROR, "crosshair: window not destroyed", exc_info=True)
            self.hwnd = 0
        self.shown = False
        if self.timer:
            self._drop_timer()
        with _lock:
            wake, self.wake = self.wake, 0
        if wake:
            try:
                w.CloseHandle(wake)
            except Exception:
                pass
        self.reason, self.since = "not_running", time.time()
        st = self._status_dict(running=False)
        with _lock:
            _status = st
            if _eng is self:
                _eng = None

    # ---- the loop ----
    def _loop(self) -> None:
        w = _win
        self._step(time.monotonic(), True)
        while not self.quit and not self.destroyed:
            handles = [self.wake] + ([self.timer] if self.timer else [])
            n = len(handles)
            r = w.MsgWaitForMultipleObjectsEx(handles, self._wait_ms(time.monotonic()))
            now = time.monotonic()
            tick = False
            if r == WAIT_OBJECT_0 + n:                      # messages (and the foreground hook)
                if not w.pump_messages():
                    break                                   # WM_QUIT
            elif r == WAIT_TIMEOUT:
                tick = True
            elif n > 1 and r == WAIT_OBJECT_0 + 1:          # the timer
                tick = now - self.last_tick >= MIN_WAIT_MS / 1000.0 * 0.8 or self._early_timer()
            elif r != WAIT_OBJECT_0:                        # WAIT_FAILED or something stranger
                self._wait_failed(r)
                now = time.monotonic()
            if r in (WAIT_OBJECT_0, WAIT_TIMEOUT) or 0 < r <= n:
                self.wait_failures = 0
            if not tick and now >= self.next_tick:
                tick = True
            self._step(now, tick)
        if self.destroyed and not self.quit:
            self.last_error = "The crosshair window was closed."

    def _wait_ms(self, now: float) -> int:
        ms = max(MIN_WAIT_MS, int(math.ceil((self.next_tick - now) * 1000.0)))
        if self.timer:
            ms += self.period_ms        # the timer ends the wait; the timeout is only a backstop
        return ms

    def _early_timer(self) -> bool:
        """The timer fired far too soon. A few in a row: stop trusting it."""
        self.early += 1
        if self.early >= 5 and self.timer:
            _tlog(logging.WARNING, "crosshair: the wait timer keeps firing early, using plain waits")
            self._drop_timer()
        return False

    def _wait_failed(self, r) -> None:
        self.wait_failures += 1
        if self.wait_failures == 1:
            _tlog(logging.WARNING, "crosshair: wait failed (%s, error %s)", r, _win.GetLastError())
        if self.wait_failures > 200:
            raise OSError("The crosshair's message loop keeps failing.")
        time.sleep(0.05)

    def _set_period(self, ms: int, force: bool = False) -> None:
        if ms == self.period_ms and not force:
            return
        self.period_ms = ms
        self.next_tick = min(self.next_tick or float("inf"), time.monotonic() + ms / 1000.0)
        if self.timer and not _win.SetWaitableTimer(self.timer, ms):
            _tlog(logging.WARNING, "crosshair: timer not set (error %s)", _win.GetLastError())
            self._drop_timer()

    def _drop_timer(self) -> None:
        timer, self.timer = self.timer, 0
        self.timer_kind = "normal"
        try:
            _win.CancelWaitableTimer(timer)
            _win.CloseHandle(timer)
        except Exception:
            pass

    def _step(self, now: float, tick: bool) -> None:
        self._drain()
        if self.quit:
            return
        if tick:
            self.last_tick, self.next_tick, self.early = now, now + self.period_ms / 1000.0, 0
        if self.fg_dirty or now >= self.next_fg:
            self.fg_dirty, self.next_fg = False, now + FOREGROUND_EVERY_S
            self._read_foreground()
        inp = self._inputs(now)
        eligible = decide(dict(inp, hidden=False, aiming=False, cursor_visible=False,
                               testing=False))[1] == "shown"
        if eligible and not self.eligible:
            self.aim.reset()
            self.hotkeys.prev = {}            # the first poll only records: a key already down never fires
        self.eligible = eligible
        if tick:
            self._poll_input(eligible)        # aim and hotkeys: edges on the tick cadence
        # The pointer is read on every step: a step that makes the crosshair eligible
        # (foreground change, unlock, turned on) must not show it over a menu for a tick.
        self.cursor_visible = (eligible and bool(self.cfg.get("hide_with_cursor"))
                               and cursor_visible(*_win.GetCursorInfo()))
        inp = self._inputs(now)
        self._evaluate(now, inp)
        aim_on = self.cfg.get("hide_on_aim") in ("hold", "toggle")
        # Hotkeys only act while eligible, so anywhere else a slow tick is enough.
        self._set_period(TICK_AIM_MS if aim_on and eligible
                         else TICK_HOTKEY_MS if eligible and self.hotkeys.bindings else TICK_IDLE_MS)

    # ---- work from other threads ----
    def _drain(self) -> None:
        with _lock:
            quit_now = self.quit_requested
            cfg = _cfg if _cfg_seq != self.cfg_seq else None
            seq = _cfg_seq
        if quit_now:
            self.quit = True
            return
        if cfg is not None:
            self.cfg_seq = seq
            self._apply_cfg(cfg)

    def _apply_cfg(self, cfg: dict) -> None:
        self.cfg = cfg
        self.hk_status = self.hotkeys.set_bindings(cfg.get("hotkeys") or {})
        self.failed_src = None          # new settings: worth one more try
        self.draw_retry_at = self.draw_backoff = 0.0
        self.fg_dirty = True            # the anchor may have changed
        self._apply_capture()

    def _apply_capture(self) -> None:
        if not self.hwnd:
            return
        want = bool(self.cfg.get("exclude_from_capture"))
        if want == self.capture_want:
            return
        first = self.capture_want is None
        self.capture_want = want
        try:
            build = int(_win.windows_build())
        except Exception:
            build = 0
        if build < CAPTURE_EXCLUSION_MIN_BUILD:
            self.capture_excluded = False     # never WDA_MONITOR: that is a black box in every clip
            return
        if first and not want:
            return                            # a new window already shows in captures
        ok = bool(_win.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE if want else WDA_NONE))
        self.capture_excluded = ok and want
        if not ok:
            _tlog(logging.WARNING, "crosshair: display affinity not set (error %s)", _win.GetLastError())

    # ---- window messages (overlay thread, from pump_messages) ----
    def on_message(self, msg: int, wp: int, lp: int):
        if msg == WM_DISPLAYCHANGE:           # resolution, colour depth, monitor plugged in or out
            self.monitors = None
            self._redraw_now()
            self.fg_dirty = True
            return 0
        if msg == WM_DPICHANGED:
            self.fg_dirty = True
            return 0
        if msg == WM_WTSSESSION_CHANGE:
            if wp == WTS_SESSION_LOCK:
                self.locked = True
            elif wp == WTS_SESSION_UNLOCK:
                self.locked = False
                self._redraw_now()
            self.fg_dirty = True
            return 0
        if msg == WM_DESTROY:
            self.destroyed = True
            return 0
        return None

    def _redraw_now(self) -> None:
        """The desktop changed or came back: draw again on the next step, and
        give a failed render or drawing one more try at once."""
        self.render_src = None
        self.failed_src = None
        self.draw_retry_at = 0.0

    # ---- inputs ----
    def _is_known(self, exe: str) -> bool:
        # Asked every time, never cached: custom games are added and removed
        # while the overlay runs, and the answer is a set lookup.
        if not exe:
            return False
        fn = self.cfg.get("is_known_game")
        try:
            return bool(fn(exe)) if callable(fn) else False
        except Exception:
            return False

    def _read_foreground(self) -> None:
        w = _win
        hwnd = int(w.GetForegroundWindow() or 0)
        if hwnd != self.fg_hwnd:
            pid = int(w.GetWindowThreadProcessId(hwnd) or 0) if hwnd else 0
            path = (w.process_image(pid) or "") if pid else ""
            self.fg_hwnd = hwnd
            self.fg_exe = os.path.basename(path.replace("/", "\\")).lower()
            self.fg_own = bool(pid) and pid == os.getpid()
            self.aim.reset()                  # toggle aim never carries over to another window
            self._remember(self.fg_exe)
        self.fg_monitor = w.monitor_device(hwnd) if hwnd else None
        centre = None
        if hwnd and self.cfg.get("anchor", "game") != "monitor" and not w.IsIconic(hwnd):
            centre = w.client_center(hwnd)
        self.game_center = centre

    def _remember(self, exe: str) -> None:
        if (not exe or self.fg_own or exe in _SHELL_EXES
                or exe in (self.cfg.get("own_exes") or ())):
            return
        with _lock:
            if exe in _last_games:
                _last_games.remove(exe)
            _last_games.insert(0, exe)
            del _last_games[10:]

    def _game_exe(self) -> str:
        exe, cfg = self.fg_exe, self.cfg
        if not exe or self.fg_own or exe in (cfg.get("own_exes") or ()):
            return ""
        if self._is_known(exe) or exe in (cfg.get("game_rules") or {}) or exe in (cfg.get("blocked_exes") or ()):
            return exe
        return ""

    def _inputs(self, now: float) -> dict:
        cfg = self.cfg
        with _lock:
            hidden, test_until, exclusive = bool(_rt["hidden"]), _rt["test_until"], _rt["exclusive"]
        exe = self.fg_exe
        return {
            "enabled": bool(cfg.get("enabled")), "locked": self.locked, "hidden": hidden,
            "exe": exe, "own": self.fg_own, "own_exes": cfg.get("own_exes") or frozenset(),
            "show_mode": cfg.get("show_mode") or "games", "testing": now < test_until,
            "game_rules": cfg.get("game_rules") or {}, "blocked_exes": cfg.get("blocked_exes") or frozenset(),
            "known_game": self._is_known(exe), "exclusive_exes": exclusive,
            "aiming": self.aim.aiming, "hide_with_cursor": bool(cfg.get("hide_with_cursor")),
            "cursor_visible": self.cursor_visible,
        }

    def _poll_input(self, eligible: bool) -> None:
        cfg = self.cfg
        mode = cfg.get("hide_on_aim")
        if mode in ("hold", "toggle") and eligible:
            swapped = bool(_win.GetSystemMetrics(SM_SWAPBUTTON))
            self.aim.update(mode, _key_down(aim_vk(cfg.get("aim_button"), swapped)))
        else:
            self.aim.aiming = False
        for name in self.hotkeys.poll(_key_down, eligible):
            if name == "toggle":
                with _lock:
                    _rt["hidden"] = not _rt["hidden"]
            else:
                _fire_hotkey(name)

    # ---- what the window shows ----
    def _evaluate(self, now: float, inp: dict) -> None:
        global _status
        visible, reason = decide(inp)
        self.game_exe = self._game_exe()
        self.preset_id = pick_design(self.cfg, self.fg_exe)[0]
        if visible:
            err = self._present(now)
            if err:
                if err != self.last_error:
                    _tlog(logging.WARNING, "crosshair: %s", err)
                self.last_error, reason = err, "error"
                self._hide()
        else:
            self._hide()
        if reason != self.reason:
            self.reason, self.since = reason, time.time()
        target = self.target if self.shown else None
        sig = (self.reason, self.shown, self.pos, self.size, self.preset_id, self.game_exe, self.capture_excluded,
               self.timer_kind, self.last_error, target["monitor"].get("id") if target else None, self.cfg_seq,
               self.since, self.fg_exe)
        if sig != self.published:             # the 10 ms aim tick needn't rebuild an unchanged status
            self.published = sig
            st = self._status_dict(running=True)
            with _lock:
                _status = st

    def _present(self, now: float) -> str | None:
        cfg, w = self.cfg, _win
        if not self.monitors:
            self.monitors = list_monitors()
        target = place({"anchor": cfg.get("anchor", "game"), "monitor": cfg.get("monitor", "auto"),
                        "monitors": self.monitors, "fg_monitor": self.fg_monitor,
                        "game_center": self.game_center if self.game_exe else None})
        if target is None:
            self.monitors = None
            return "No monitor found."
        self.target = target
        height = int(target["monitor"]["height"])
        design = pick_design(cfg, self.fg_exe)[1]
        if not isinstance(design, dict):
            return "No crosshair design to draw."
        images = cfg.get("images") or {}
        img_obj = images.get((design.get("image") or {}).get("id")) if design.get("kind") == "image" else None
        src = self.render_src
        fresh = src is None or not (src[0] == design and src[1] == height and src[2] is img_obj)
        if fresh:
            failed = self.failed_src
            if failed is not None and failed[0] == design and failed[1] == height and failed[2] is img_obj:
                return self.last_error or "Couldn't draw this crosshair."
            if self.draw_retry_at and now < self.draw_retry_at:
                return self.last_error or "Couldn't draw the crosshair."
            render = cfg.get("render")
            if not callable(render):
                return "Nothing to draw the crosshair with."
            try:
                img = render(design, height, images.get)
                size = (int(img.size[0]), int(img.size[1]))
                if not (1 <= size[0] <= MAX_SIDE and 1 <= size[1] <= MAX_SIDE):
                    raise ValueError(f"The crosshair image is {size[0]}x{size[1]}, too big to draw.")
                data = _premultiplied_bgra(img)
            except Exception as e:
                self.failed_src = (copy.deepcopy(design), height, img_obj)
                _tlog(logging.ERROR, "crosshair: render failed", exc_info=True)
                return f"Couldn't draw this crosshair: {_err_text(e, type(e).__name__)}"
            x, y = window_origin(target["centre"], size, effective_offset(cfg, self.fg_exe))
            err = self._draw(data, size, x, y)
            if err:
                # Not cached like a render error: a display switch or a driver reset
                # can make Windows refuse once, so try again after 1, 2, 4, then every 5 s.
                self.draw_backoff = min(5.0, self.draw_backoff * 2 or 1.0)
                self.draw_retry_at = now + self.draw_backoff
                return err
            self.render_src, self.failed_src = (copy.deepcopy(design), height, img_obj), None
            self.draw_retry_at = self.draw_backoff = 0.0
            self.size, self.pos = size, (x, y)
        else:
            x, y = window_origin(target["centre"], self.size, effective_offset(cfg, self.fg_exe))
            if (x, y) != self.pos:
                w.SetWindowPos(self.hwnd, 0, x, y, 0, 0, MOVE_FLAGS)
                self.pos = (x, y)
        self.last_error = None
        if not self.shown:
            w.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.shown = True
            self._raise(now)
        elif self.topmost_fg != self.fg_hwnd or now - self.last_topmost >= TOPMOST_EVERY_S:
            with _lock:
                exclusive = self.fg_exe in _rt["exclusive"]
            if exclusive:
                pass                          # no z-order churn over an exclusive-fullscreen game
            elif self.topmost_fg != self.fg_hwnd or w.GetWindow(self.hwnd, GW_HWNDPREV):
                self._raise(now)
            else:
                self.last_topmost = now       # nothing above us
        return None

    def _draw(self, data: bytes, size: tuple, x: int, y: int) -> str | None:
        """Copy the pixels into a 32-bit top-down DIB and hand it to the window.
        GDI objects go back in order: old bitmap selected back, DIB, then DC."""
        w = _win
        width, height = size
        dc = w.CreateCompatibleDC()
        if not dc:
            return f"Couldn't draw the crosshair (error {w.GetLastError()})."
        hbmp = old = 0
        try:
            hbmp, bits = w.CreateDIBSection(dc, width, height)
            if not hbmp or not bits:
                return f"Couldn't draw the crosshair (error {w.GetLastError()})."
            w.write_bits(bits, data)
            old = w.SelectObject(dc, hbmp)
            if not w.UpdateLayeredWindow(self.hwnd, x, y, width, height, dc):
                return f"Couldn't draw the crosshair (error {w.GetLastError()})."
            return None
        finally:
            if old:
                w.SelectObject(dc, old)
            if hbmp:
                w.DeleteObject(hbmp)
            w.DeleteDC(dc)

    def _hide(self) -> None:
        if self.shown:
            _win.ShowWindow(self.hwnd, SW_HIDE)
            self.shown = False

    def _raise(self, now: float) -> None:
        # Never SWP_SHOWWINDOW: it would undo a deliberate hide.
        _win.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, TOPMOST_FLAGS)
        self.last_topmost, self.topmost_fg = now, self.fg_hwnd

    def _status_dict(self, running: bool) -> dict:
        target = self.target if running and self.shown else None
        mon = target["monitor"] if target else None
        with _lock:
            games = list(_last_games)
        return {
            "running": running, "visible": bool(running and self.shown), "reason": self.reason,
            "game_exe": self.game_exe if running else "", "last_games": games,
            "monitor": ({"id": mon.get("id"), "name": mon.get("name"), "width": mon.get("width"),
                         "height": mon.get("height")} if mon else None),
            "position": {"x": self.pos[0], "y": self.pos[1]} if target and self.pos else None,
            "image": {"w": self.size[0], "h": self.size[1]} if target and self.size else None,
            "preset": self.preset_id or (self.cfg.get("active_preset") or ""),
            "capture_excluded": bool(running and self.capture_excluded),
            "hotkeys": copy.deepcopy(self.hk_status), "timer": self.timer_kind,
            "last_error": self.last_error, "since": self.since,
        }


# ═════════════════════════════════════════════════════════════════════
# Public API (any thread)
# ═════════════════════════════════════════════════════════════════════
def start() -> dict:
    """Open the overlay window on its own thread. {ok, running} or {ok: False, err}."""
    global _eng, _thread
    with _life:
        th = _thread
        if th is not None and th.is_alive():
            with _lock:
                eng = _eng
                closing = eng is None or eng.quit_requested
            if not closing:
                return {"ok": True, "running": True}
            th.join(2.0)
            if th.is_alive():
                return {"ok": False, "err": "The crosshair is still closing. Try again in a moment."}
        eng = _Overlay()
        th = threading.Thread(target=eng.run, name="crosshair-overlay", daemon=True)
        with _lock:
            _eng = eng
        _thread = th
        th.start()
        if not eng.ready.wait(START_TIMEOUT_S) and not eng.hwnd:
            # Reported as a failure, so make it one: a window that opens late
            # closes itself before it is ever shown, and the next start begins afresh.
            err = "The crosshair window took too long to open."
            with _lock:
                eng.quit_requested = True
                eng.last_error = err
            _signal()
            return {"ok": False, "err": err}
    if eng.hwnd and th.is_alive():
        return {"ok": True, "running": True}
    return {"ok": False, "err": eng.last_error or "The crosshair window didn't open."}


def stop(timeout: float = 2.0) -> dict:
    """Close the window (on its own thread) and wait for the thread to end."""
    with _life:
        th = _thread
        with _lock:
            if _eng is not None:
                _eng.quit_requested = True
        if th is None or not th.is_alive():
            return {"ok": True, "running": False}
        if th is threading.current_thread():
            return {"ok": True, "running": True, "stopping": True}
        _signal()
        try:
            th.join(max(0.0, float(timeout)))
        except (TypeError, ValueError):
            th.join(2.0)
        if th.is_alive():
            return {"ok": False, "err": "The crosshair didn't close in time."}
    return {"ok": True, "running": False}


def is_running() -> bool:
    th = _thread
    with _lock:
        eng = _eng
        return bool(th is not None and th.is_alive() and eng is not None
                    and eng.hwnd and not eng.quit_requested)


def configure(cfg: dict) -> None:
    """Hand the overlay its runtime settings (built by core/crosshair.py)."""
    global _cfg, _cfg_seq
    if not isinstance(cfg, dict):
        return
    with _lock:
        if cfg.get("enabled") and not _cfg.get("enabled"):
            _rt["hidden"] = False          # turning the crosshair on always shows it again
        _cfg = {**_DEFAULT_CFG, **cfg}
        _cfg_seq += 1
    _signal()


def set_exclusive_exes(exes) -> None:
    """Exes PresentMon saw in true exclusive fullscreen (the crosshair can't show there)."""
    try:
        new = frozenset(str(e).lower() for e in (exes or ()))
    except TypeError:
        new = frozenset()
    with _lock:
        if new == _rt["exclusive"]:
            return
        _rt["exclusive"] = new
    _signal()


def set_hidden(hidden: bool) -> None:
    with _lock:
        _rt["hidden"] = bool(hidden)
    _signal()


def test_show(seconds: float) -> None:
    """Show for a few seconds even with Vispora in front or no game running."""
    try:
        seconds = max(0.0, min(60.0, float(seconds)))
    except (TypeError, ValueError):
        seconds = 0.0
    with _lock:
        _rt["test_until"] = time.monotonic() + seconds if seconds else 0.0
    _signal()


def get_status() -> dict:
    with _lock:
        st = _status
    if st is None or not is_running():
        st = _idle_status(st)
    return copy.deepcopy(st)


def _idle_status(prev) -> dict:
    prev = prev or {}
    with _lock:
        cfg, games = _cfg, list(_last_games)
    return {
        "running": False, "visible": False, "reason": "not_running", "game_exe": "",
        "last_games": games, "monitor": None, "position": None, "image": None,
        "preset": cfg.get("active_preset") or "", "capture_excluded": False,
        "hotkeys": HotkeyPoller().set_bindings(cfg.get("hotkeys") or {}),
        "timer": prev.get("timer", "normal"), "last_error": prev.get("last_error"),
        "since": prev.get("since", time.time()),
    }


def list_monitors() -> list:
    """[{id: szDevice, name: "Display 1", width, height, primary, left, top}].
    width and height are the physical mode (EnumDisplaySettingsW), which no DPI
    setting changes, so this is safe from any thread."""
    try:
        raw = _win.EnumDisplayMonitors() or []
    except Exception:
        _tlog(logging.ERROR, "crosshair: monitors not listed", exc_info=True)
        return []
    out = []
    for i, m in enumerate(raw):
        try:
            dev = str(m.get("id") or "")
            mode = _win.EnumDisplaySettingsW(dev) if dev else None
            width, height = mode if mode else (int(m["right"]) - int(m["left"]), int(m["bottom"]) - int(m["top"]))
            num = re.search(r"(\d+)\s*$", dev)
            out.append({"id": dev, "name": f"Display {num.group(1) if num else i + 1}",
                        "width": int(width), "height": int(height), "primary": bool(m.get("primary")),
                        "left": int(m.get("left", 0)), "top": int(m.get("top", 0))})
        except Exception:
            _tlog(logging.ERROR, "crosshair: monitor %r skipped", m, exc_info=True)
    return out


def capabilities() -> dict:
    """{"capture_exclusion": Windows 10 2004 or later, "high_res_timer": bool}."""
    global _caps
    if _caps is None:
        high_res = False
        try:
            h = _win.CreateWaitableTimerExW(True)
            if h:
                high_res = True
                _win.CloseHandle(h)
        except Exception:
            pass
        try:
            build = int(_win.windows_build())
        except Exception:
            build = 0
        _caps = {"capture_exclusion": build >= CAPTURE_EXCLUSION_MIN_BUILD, "high_res_timer": high_res}
    return dict(_caps)


# ═════════════════════════════════════════════════════════════════════
# The real Win32 adapter
# ═════════════════════════════════════════════════════════════════════
LRESULT = W.LPARAM                      # LONG_PTR (wintypes has no LRESULT)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND, W.LONG, W.LONG, W.DWORD, W.DWORD)
MONITORENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", W.HWND), ("message", W.UINT), ("wParam", W.WPARAM), ("lParam", W.LPARAM),
                ("time", W.DWORD), ("pt", W.POINT), ("lPrivate", W.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", W.BYTE), ("rgbGreen", W.BYTE), ("rgbRed", W.BYTE), ("rgbReserved", W.BYTE)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", W.BYTE), ("BlendFlags", W.BYTE), ("SourceConstantAlpha", W.BYTE),
                ("AlphaFormat", W.BYTE)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD),
                ("szDevice", W.WCHAR * 32)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", W.HANDLE), ("ptScreenPos", W.POINT)]


class DEVMODEW(ctypes.Structure):       # the display variant of the union
    _fields_ = [("dmDeviceName", W.WCHAR * 32), ("dmSpecVersion", W.WORD), ("dmDriverVersion", W.WORD),
                ("dmSize", W.WORD), ("dmDriverExtra", W.WORD), ("dmFields", W.DWORD),
                ("dmPositionX", W.LONG), ("dmPositionY", W.LONG), ("dmDisplayOrientation", W.DWORD),
                ("dmDisplayFixedOutput", W.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", W.WCHAR * 32), ("dmLogPixels", W.WORD),
                ("dmBitsPerPel", W.DWORD), ("dmPelsWidth", W.DWORD), ("dmPelsHeight", W.DWORD),
                ("dmDisplayFlags", W.DWORD), ("dmDisplayFrequency", W.DWORD), ("dmICMMethod", W.DWORD),
                ("dmICMIntent", W.DWORD), ("dmMediaType", W.DWORD), ("dmDitherType", W.DWORD),
                ("dmReserved1", W.DWORD), ("dmReserved2", W.DWORD), ("dmPanningWidth", W.DWORD),
                ("dmPanningHeight", W.DWORD)]


class THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", W.ULONG), ("ControlMask", W.ULONG), ("StateMask", W.ULONG)]


_P = ctypes.POINTER
_SIGS = (
    ("user32", "RegisterClassExW", W.ATOM, (_P(WNDCLASSEXW),)),
    ("user32", "CreateWindowExW", W.HWND, (W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)),
    ("user32", "DestroyWindow", W.BOOL, (W.HWND,)),
    ("user32", "DefWindowProcW", LRESULT, (W.HWND, W.UINT, W.WPARAM, W.LPARAM)),
    ("user32", "ShowWindow", W.BOOL, (W.HWND, ctypes.c_int)),
    ("user32", "SetWindowPos", W.BOOL, (W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, W.UINT)),
    ("user32", "GetWindow", W.HWND, (W.HWND, W.UINT)),
    ("user32", "IsWindow", W.BOOL, (W.HWND,)),
    ("user32", "UpdateLayeredWindow", W.BOOL, (W.HWND, W.HDC, _P(W.POINT), _P(W.SIZE), W.HDC, _P(W.POINT),
                                               W.DWORD, _P(BLENDFUNCTION), W.DWORD)),
    ("user32", "PeekMessageW", W.BOOL, (_P(MSG), W.HWND, W.UINT, W.UINT, W.UINT)),
    ("user32", "TranslateMessage", W.BOOL, (_P(MSG),)),
    ("user32", "DispatchMessageW", LRESULT, (_P(MSG),)),
    ("user32", "MsgWaitForMultipleObjectsEx", W.DWORD, (W.DWORD, _P(W.HANDLE), W.DWORD, W.DWORD, W.DWORD)),
    ("user32", "GetAsyncKeyState", W.SHORT, (ctypes.c_int,)),
    ("user32", "GetSystemMetrics", ctypes.c_int, (ctypes.c_int,)),
    ("user32", "GetForegroundWindow", W.HWND, ()),
    ("user32", "GetWindowThreadProcessId", W.DWORD, (W.HWND, _P(W.DWORD))),
    ("user32", "IsIconic", W.BOOL, (W.HWND,)),
    ("user32", "GetClientRect", W.BOOL, (W.HWND, _P(W.RECT))),
    ("user32", "ClientToScreen", W.BOOL, (W.HWND, _P(W.POINT))),
    ("user32", "MonitorFromWindow", W.HMONITOR, (W.HWND, W.DWORD)),
    ("user32", "GetMonitorInfoW", W.BOOL, (W.HMONITOR, _P(MONITORINFOEXW))),
    ("user32", "EnumDisplayMonitors", W.BOOL, (W.HDC, _P(W.RECT), MONITORENUMPROC, W.LPARAM)),
    ("user32", "EnumDisplaySettingsW", W.BOOL, (W.LPCWSTR, W.DWORD, _P(DEVMODEW))),
    ("user32", "GetCursorInfo", W.BOOL, (_P(CURSORINFO),)),
    ("user32", "SetThreadDpiAwarenessContext", W.HANDLE, (W.HANDLE,)),
    ("user32", "SetWindowDisplayAffinity", W.BOOL, (W.HWND, W.DWORD)),
    ("user32", "SetWinEventHook", W.HANDLE, (W.DWORD, W.DWORD, W.HMODULE, WINEVENTPROC, W.DWORD, W.DWORD,
                                             W.DWORD)),
    ("user32", "UnhookWinEvent", W.BOOL, (W.HANDLE,)),
    ("user32", "GetGuiResources", W.DWORD, (W.HANDLE, W.DWORD)),
    ("gdi32", "CreateCompatibleDC", W.HDC, (W.HDC,)),
    ("gdi32", "DeleteDC", W.BOOL, (W.HDC,)),
    ("gdi32", "CreateDIBSection", W.HBITMAP, (W.HDC, _P(BITMAPINFO), W.UINT, _P(ctypes.c_void_p), W.HANDLE,
                                              W.DWORD)),
    ("gdi32", "SelectObject", W.HGDIOBJ, (W.HDC, W.HGDIOBJ)),
    ("gdi32", "DeleteObject", W.BOOL, (W.HGDIOBJ,)),
    ("kernel32", "GetModuleHandleW", W.HMODULE, (W.LPCWSTR,)),
    ("kernel32", "GetCurrentProcess", W.HANDLE, ()),
    ("kernel32", "GetCurrentThread", W.HANDLE, ()),
    ("kernel32", "SetThreadPriority", W.BOOL, (W.HANDLE, ctypes.c_int)),
    ("kernel32", "SetThreadInformation", W.BOOL, (W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD)),
    ("kernel32", "CreateEventW", W.HANDLE, (W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR)),
    ("kernel32", "SetEvent", W.BOOL, (W.HANDLE,)),
    ("kernel32", "CreateWaitableTimerExW", W.HANDLE, (W.LPVOID, W.LPCWSTR, W.DWORD, W.DWORD)),
    ("kernel32", "SetWaitableTimer", W.BOOL, (W.HANDLE, _P(ctypes.c_longlong), W.LONG, W.LPVOID, W.LPVOID,
                                              W.BOOL)),
    ("kernel32", "CancelWaitableTimer", W.BOOL, (W.HANDLE,)),
    ("kernel32", "CloseHandle", W.BOOL, (W.HANDLE,)),
    ("kernel32", "OpenProcess", W.HANDLE, (W.DWORD, W.BOOL, W.DWORD)),
    ("kernel32", "QueryFullProcessImageNameW", W.BOOL, (W.HANDLE, W.DWORD, W.LPWSTR, _P(W.DWORD))),
    ("wtsapi32", "WTSRegisterSessionNotification", W.BOOL, (W.HWND, W.DWORD)),
    ("wtsapi32", "WTSUnRegisterSessionNotification", W.BOOL, (W.HWND,)),
)

# Callback thunks Windows may still call: kept for the life of the process.
_KEEP_ALIVE: list = []


class _Api:
    pass


def _bind_api() -> _Api:
    api, dlls = _Api(), {}
    for dll_name, fn_name, restype, argtypes in _SIGS:
        dll = dlls.get(dll_name)
        if dll is None:
            # Private instances: argtypes set here never clash with other modules' bindings.
            dll = dlls[dll_name] = ctypes.WinDLL(dll_name, use_last_error=True)
        fn = getattr(dll, fn_name, None)           # None when this Windows lacks it
        if fn is not None:
            fn.restype, fn.argtypes = restype, list(argtypes)
        setattr(api, fn_name, fn)
    return api


class Win32:
    """The overlay's only door to Windows. Python-friendly signatures, handles
    as ints. The DLLs are bound on first use, so importing touches none."""

    def __init__(self):
        self._a = None
        self._bind_lock = threading.Lock()

    def api(self) -> _Api:
        a = self._a
        if a is None:
            with self._bind_lock:
                if self._a is None:
                    self._a = _bind_api()
                a = self._a
        return a

    # ---- process, thread, kernel objects ----
    def GetLastError(self) -> int:
        return ctypes.get_last_error()

    def windows_build(self) -> int:
        v = sys.getwindowsversion()
        return max(int(v.build), int(getattr(v, "platform_version", (0, 0, 0))[2]))

    def SetThreadDpiAwarenessContext(self, ctx: int) -> bool:
        fn = self.api().SetThreadDpiAwarenessContext
        return bool(fn and fn(ctx))

    def SetThreadPriority(self, priority: int) -> bool:
        a = self.api()
        return bool(a.SetThreadPriority(a.GetCurrentThread(), priority))

    def disable_power_throttling(self) -> bool:
        a = self.api()
        if not a.SetThreadInformation:
            return False
        st = THREAD_POWER_THROTTLING_STATE(1, THREAD_POWER_THROTTLING_EXECUTION_SPEED, 0)
        return bool(a.SetThreadInformation(a.GetCurrentThread(), THREAD_POWER_THROTTLING,
                                           ctypes.byref(st), ctypes.sizeof(st)))

    def ensure_message_queue(self) -> None:
        msg = MSG()
        self.api().PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)

    def CreateEventW(self) -> int:
        return int(self.api().CreateEventW(None, False, False, None) or 0)     # auto-reset

    def SetEvent(self, handle: int) -> bool:
        return bool(self.api().SetEvent(handle))

    def CreateWaitableTimerExW(self, high_res: bool) -> int:
        flags = CREATE_WAITABLE_TIMER_HIGH_RESOLUTION if high_res else 0
        return int(self.api().CreateWaitableTimerExW(None, None, flags, TIMER_ALL_ACCESS) or 0)

    def SetWaitableTimer(self, handle: int, period_ms: int) -> bool:
        due = ctypes.c_longlong(-int(period_ms) * 10_000)          # relative, 100 ns units
        return bool(self.api().SetWaitableTimer(handle, ctypes.byref(due), int(period_ms), None, None, False))

    def CancelWaitableTimer(self, handle: int) -> bool:
        return bool(self.api().CancelWaitableTimer(handle))

    def CloseHandle(self, handle: int) -> bool:
        return bool(self.api().CloseHandle(handle))

    def MsgWaitForMultipleObjectsEx(self, handles: list, timeout_ms: int) -> int:
        arr = (W.HANDLE * len(handles))(*handles)
        return int(self.api().MsgWaitForMultipleObjectsEx(len(handles), arr, int(timeout_ms),
                                                          QS_ALLINPUT, MWMO_INPUTAVAILABLE))

    def pump_messages(self) -> bool:
        """Dispatch what is queued. False when WM_QUIT came up."""
        a, msg = self.api(), MSG()
        p = ctypes.byref(msg)
        for _ in range(1000):
            if not a.PeekMessageW(p, None, 0, 0, PM_REMOVE):
                break
            if msg.message == WM_QUIT:
                return False
            a.TranslateMessage(p)
            a.DispatchMessageW(p)
        return True

    # ---- the window ----
    def RegisterClassExW(self, class_name: str, wndproc) -> int:
        a = self.api()
        thunk = WNDPROC(wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0                    # no CS_OWNDC / CS_CLASSDC with a layered window
        wc.lpfnWndProc = thunk
        wc.hInstance = a.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        atom = a.RegisterClassExW(ctypes.byref(wc))
        if atom:
            _KEEP_ALIVE.append(thunk)
            return int(atom)
        if ctypes.get_last_error() == ERROR_CLASS_ALREADY_EXISTS:
            return 1                    # registered earlier in this process; its thunk is still kept
        return 0

    def CreateWindowExW(self, ex_style, class_name, title, style, x, y, width, height) -> int:
        a = self.api()
        return int(a.CreateWindowExW(ex_style, class_name, title, style, x, y, width, height,
                                     None, None, a.GetModuleHandleW(None), None) or 0)

    def DestroyWindow(self, hwnd: int) -> bool:
        return bool(self.api().DestroyWindow(hwnd))

    def DefWindowProcW(self, hwnd, msg, wparam, lparam) -> int:
        return int(self.api().DefWindowProcW(hwnd, msg, wparam, lparam) or 0)

    def ShowWindow(self, hwnd: int, cmd: int) -> bool:
        return bool(self.api().ShowWindow(hwnd, cmd))

    def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags) -> bool:
        return bool(self.api().SetWindowPos(hwnd, insert_after or None, x, y, cx, cy, flags))

    def GetWindow(self, hwnd: int, cmd: int) -> int:
        return int(self.api().GetWindow(hwnd, cmd) or 0)

    def IsWindow(self, hwnd: int) -> bool:
        return bool(self.api().IsWindow(hwnd))

    def SetWindowDisplayAffinity(self, hwnd: int, affinity: int) -> bool:
        return bool(self.api().SetWindowDisplayAffinity(hwnd, affinity))

    def WTSRegisterSessionNotification(self, hwnd: int) -> bool:
        fn = self.api().WTSRegisterSessionNotification
        return bool(fn and fn(hwnd, NOTIFY_FOR_THIS_SESSION))

    def WTSUnRegisterSessionNotification(self, hwnd: int) -> bool:
        fn = self.api().WTSUnRegisterSessionNotification
        return bool(fn and fn(hwnd))

    def SetWinEventHook(self, callback) -> int:
        """EVENT_SYSTEM_FOREGROUND, out of context, delivered through this thread's loop."""
        thunk = WINEVENTPROC(callback)
        hook = self.api().SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, thunk,
                                          0, 0, WINEVENT_OUTOFCONTEXT)
        if hook:
            _KEEP_ALIVE.append(thunk)
        return int(hook or 0)

    def UnhookWinEvent(self, hook: int) -> bool:
        return bool(self.api().UnhookWinEvent(hook))

    # ---- drawing ----
    def CreateCompatibleDC(self) -> int:
        return int(self.api().CreateCompatibleDC(None) or 0)

    def CreateDIBSection(self, hdc: int, width: int, height: int) -> tuple:
        """A 32-bit top-down DIB: (bitmap handle, pointer to its pixels)."""
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = int(width), -int(height)       # negative: top-down rows
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, BI_RGB
        bits = ctypes.c_void_p()
        hbmp = self.api().CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        return int(hbmp or 0), int(bits.value or 0)

    def write_bits(self, bits: int, data: bytes) -> None:
        ctypes.memmove(bits, data, len(data))                       # 32 bpp rows have no padding

    def SelectObject(self, hdc: int, obj: int) -> int:
        return int(self.api().SelectObject(hdc, obj) or 0)

    def DeleteObject(self, obj: int) -> bool:
        return bool(self.api().DeleteObject(obj))

    def DeleteDC(self, hdc: int) -> bool:
        return bool(self.api().DeleteDC(hdc))

    def UpdateLayeredWindow(self, hwnd: int, x: int, y: int, width: int, height: int, hdc_src: int) -> bool:
        dst, size, src = W.POINT(x, y), W.SIZE(width, height), W.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        return bool(self.api().UpdateLayeredWindow(hwnd, None, ctypes.byref(dst), ctypes.byref(size), hdc_src,
                                                   ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA))

    def gdi_objects(self) -> int:
        a = self.api()
        return int(a.GetGuiResources(a.GetCurrentProcess(), GR_GDIOBJECTS))

    # ---- the desktop ----
    def GetForegroundWindow(self) -> int:
        return int(self.api().GetForegroundWindow() or 0)

    def GetWindowThreadProcessId(self, hwnd: int) -> int:
        pid = W.DWORD(0)
        self.api().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def process_image(self, pid: int) -> str:
        """Full path of a process's exe, or ""."""
        a = self.api()
        h = a.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = W.DWORD(len(buf))
            return buf.value if a.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        finally:
            a.CloseHandle(h)

    def IsIconic(self, hwnd: int) -> bool:
        return bool(self.api().IsIconic(hwnd))

    def client_center(self, hwnd: int):
        """Screen position of the client area's centre pixel (w//2, h//2), or None."""
        a, rc = self.api(), W.RECT()
        if not a.GetClientRect(hwnd, ctypes.byref(rc)) or rc.right <= 0 or rc.bottom <= 0:
            return None
        pt = W.POINT(rc.right // 2, rc.bottom // 2)
        return (int(pt.x), int(pt.y)) if a.ClientToScreen(hwnd, ctypes.byref(pt)) else None

    def monitor_device(self, hwnd: int):
        """szDevice of the monitor holding the window, or None."""
        a = self.api()
        hmon = a.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        return mi.szDevice if a.GetMonitorInfoW(hmon, ctypes.byref(mi)) else None

    def EnumDisplayMonitors(self) -> list:
        a, found = self.api(), []

        def each(hmon, hdc, rect, lparam):
            try:
                mi = MONITORINFOEXW()
                mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if a.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    r = mi.rcMonitor
                    found.append({"id": mi.szDevice, "left": r.left, "top": r.top, "right": r.right,
                                  "bottom": r.bottom, "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY)})
            except Exception:
                pass
            return 1

        thunk = MONITORENUMPROC(each)
        a.EnumDisplayMonitors(None, None, thunk, 0)
        return found

    def EnumDisplaySettingsW(self, device: str):
        """The current mode's (width, height) in physical pixels, or None."""
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if self.api().EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            if dm.dmPelsWidth and dm.dmPelsHeight:
                return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
        return None

    def GetCursorInfo(self) -> tuple:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        ok = bool(self.api().GetCursorInfo(ctypes.byref(ci)))
        return ok, int(ci.flags), int(ci.hCursor or 0)

    def GetAsyncKeyState(self, vk: int) -> int:
        return int(self.api().GetAsyncKeyState(vk))

    def GetSystemMetrics(self, index: int) -> int:
        return int(self.api().GetSystemMetrics(index))


_win = Win32()
