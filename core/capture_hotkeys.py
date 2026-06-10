"""Global hotkeys for clip-save + screenshot.

Uses Win32 RegisterHotKey + a GetMessage loop on a dedicated thread.
This is the same mechanism Windows itself uses for system hotkeys —
fires reliably even when our window isn't focused, which is the whole
point (you press F10 mid-game and a clip saves).

Why not the `keyboard` PyPI package?  It works but requires either
admin (which we have) or installs a low-level keyboard hook that some
anti-cheats flag.  RegisterHotKey is the OS-blessed path — no hook,
no risk.

Hotkey strings the UI sends:
    "F9"
    "F10"
    "Ctrl+F10"
    "Ctrl+Shift+S"
    "Alt+Print Screen"
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from core.utils import get_logger

log = get_logger(__name__)


# ─── Win32 constants + bindings ──────────────────────────────────────

# Modifier flags
MOD_ALT     = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT   = 0x0004
MOD_WIN     = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

# Virtual-key code mapping for keys we care about.  Letters/digits use
# their ASCII codepoints directly.
_VK_MAP = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B, "f13": 0x7C, "f14": 0x7D, "f15": 0x7E,
    "f16": 0x7F, "f17": 0x80, "f18": 0x81, "f19": 0x82, "f20": 0x83,
    "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87,
    "space":      0x20,
    "tab":        0x09,
    "enter":      0x0D,
    "return":     0x0D,
    "escape":     0x1B,
    "esc":        0x1B,
    "backspace":  0x08,
    "delete":     0x2E,
    "insert":     0x2D,
    "home":       0x24,
    "end":        0x23,
    "pageup":     0x21,
    "pagedown":   0x22,
    "print screen": 0x2C,
    "printscreen": 0x2C,
    "prtsc":      0x2C,
    "scroll lock":0x91,
    "pause":      0x13,
    "left":       0x25,
    "up":         0x26,
    "right":      0x27,
    "down":       0x28,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
    "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


def _parse_hotkey(s: str) -> tuple:
    """Parse "Ctrl+Shift+F10" → (mods, vk).  Returns (0, 0) on failure."""
    if not s: return (0, 0)
    parts = [p.strip().lower() for p in s.split("+") if p.strip()]
    mods = 0
    vk = 0
    for p in parts:
        if p in ("ctrl", "control"): mods |= MOD_CONTROL
        elif p in ("alt",):           mods |= MOD_ALT
        elif p in ("shift",):         mods |= MOD_SHIFT
        elif p in ("win", "windows"): mods |= MOD_WIN
        elif p in _VK_MAP:            vk = _VK_MAP[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
    if not vk:
        return (0, 0)
    return (mods | MOD_NOREPEAT, vk)


# ─── State ───────────────────────────────────────────────────────────

_thread: Optional[threading.Thread]   = None
_stop:   Optional[threading.Event]    = None
_lock                                  = threading.Lock()
_bindings: dict = {}    # hotkey_id -> {hotkey_str, callback}
_last_err: str   = ""

# Hotkey IDs — small integers passed to RegisterHotKey.  We map our
# named bindings (e.g. "save_clip", "screenshot") to fixed IDs so
# rebind operations are simple.
_NAMED_IDS = {
    "save_clip":  1,
    "screenshot": 2,
}


def register_binding(name: str, hotkey: str, callback: Callable) -> dict:
    """Register or replace a single named binding.  Persists across
    rebinds.  Returns {ok, hotkey, name}."""
    if name not in _NAMED_IDS:
        return {"ok": False, "err": f"unknown binding name: {name}"}
    with _lock:
        _bindings[_NAMED_IDS[name]] = {
            "name":     name,
            "hotkey":   hotkey,
            "callback": callback,
        }
    # If the loop is already running, ask it to refresh registrations
    if _thread and _thread.is_alive():
        _request_refresh()
    return {"ok": True, "hotkey": hotkey, "name": name}


def unregister_binding(name: str) -> dict:
    if name not in _NAMED_IDS:
        return {"ok": False, "err": "unknown binding"}
    with _lock:
        _bindings.pop(_NAMED_IDS[name], None)
    if _thread and _thread.is_alive():
        _request_refresh()
    return {"ok": True}


# ─── Hotkey thread ───────────────────────────────────────────────────

_refresh_event: Optional[threading.Event] = None


def _request_refresh() -> None:
    """Poke the hotkey thread to re-register everything (because Win32
    doesn't have a 'change hotkey' API — you unregister + register).
    The thread wakes via PostThreadMessage and rebuilds its registration
    set."""
    global _refresh_event
    if _refresh_event:
        _refresh_event.set()


def _hotkey_loop(stop_event: threading.Event):
    """Win32 message-pump thread.  Owns the hotkey registrations —
    Windows requires the SAME thread that calls RegisterHotKey to also
    pump the message queue that receives WM_HOTKEY events."""
    global _last_err, _refresh_event
    user32 = ctypes.windll.user32
    _refresh_event = threading.Event()

    # First registration pass
    registered = _register_all()

    msg = wintypes.MSG()
    poll_interval_ms = 100

    try:
        while not stop_event.is_set():
            # PeekMessage with no-remove + PM_NOREMOVE so we can also
            # poll the stop/refresh events without blocking forever.
            had_msg = user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1)  # PM_REMOVE
            if had_msg:
                if msg.message == WM_HOTKEY:
                    hotkey_id = int(msg.wParam)
                    _fire_callback(hotkey_id)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                # Check for refresh request — if the binding map changed,
                # re-register everything.
                if _refresh_event and _refresh_event.is_set():
                    _refresh_event.clear()
                    _unregister_all(registered)
                    registered = _register_all()
                # Brief sleep so we don't busy-spin
                time.sleep(poll_interval_ms / 1000)
    finally:
        _unregister_all(registered)


def _register_all() -> list:
    """Register every binding in _bindings with Win32.  Returns the
    list of (hotkey_id, hotkey_str) we successfully registered."""
    global _last_err
    user32 = ctypes.windll.user32
    out = []
    with _lock:
        snapshot = dict(_bindings)
    for hk_id, info in snapshot.items():
        mods, vk = _parse_hotkey(info["hotkey"])
        if not vk:
            _last_err = f"invalid hotkey: {info['hotkey']}"
            log.warning(_last_err)
            continue
        if user32.RegisterHotKey(None, hk_id, mods, vk):
            out.append((hk_id, info["hotkey"]))
            log.info(f"hotkey registered: {info['name']} = {info['hotkey']}")
        else:
            err = ctypes.get_last_error()
            _last_err = (f"RegisterHotKey({info['hotkey']}) failed: "
                          f"WinErr {err} (probably already grabbed by "
                          f"another app — pick a different key)")
            log.warning(_last_err)
    return out


def _unregister_all(registered: list) -> None:
    user32 = ctypes.windll.user32
    for hk_id, _ in registered:
        try: user32.UnregisterHotKey(None, hk_id)
        except Exception: pass


def _fire_callback(hotkey_id: int) -> None:
    """Run the user's callback for a given hotkey ID on a worker thread
    so we don't block the message pump.  Callbacks can do anything —
    save a clip, take a screenshot, etc — and we don't want a slow one
    to drop the next press."""
    with _lock:
        info = _bindings.get(hotkey_id)
    if not info:
        return
    cb = info["callback"]
    name = info.get("name", "?")
    def _runner():
        try:
            log.info(f"hotkey fired: {name}")
            cb()
        except Exception as e:
            log.warning(f"hotkey callback {name} raised: {e}")
    threading.Thread(target=_runner, daemon=True,
                     name=f"hotkey-{name}").start()


def start() -> dict:
    """Start the hotkey thread.  Idempotent.

    3.4.2 — if a previous thread is still winding down (its finally:
    block calls UnregisterHotKey on IDs 1+2), we MUST wait for it to
    finish before starting a new one.  Otherwise the dying thread
    unregisters the hotkey IDs the new thread just claimed → F9/F10
    silently stop working until the next rebind.  start() now joins any
    lingering thread first."""
    global _thread, _stop
    with _lock:
        if _thread and _thread.is_alive():
            return {"ok": True, "already_running": True}
        old = _thread
    # Signal + join any lingering (already-stopped-but-not-dead) thread
    # OUTSIDE the lock so its finally-block unregister completes before
    # we register fresh hotkeys.
    if old is not None:
        if _stop:
            _stop.set()
        try:
            old.join(timeout=1.5)
        except Exception:
            pass
    _stop = threading.Event()
    t = threading.Thread(
        target=_hotkey_loop, args=(_stop,),
        daemon=True, name="capture-hotkeys",
    )
    with _lock:
        _thread = t
    t.start()
    return {"ok": True, "started": True}


def stop() -> dict:
    """Stop the hotkey thread and wait for its cleanup (UnregisterHotKey)
    to complete before returning, so a subsequent start() doesn't race
    the dying thread's unregister against its own register."""
    global _thread, _stop
    with _lock:
        t = _thread
        ev = _stop
    if ev:
        ev.set()
    if t is not None:
        try:
            t.join(timeout=1.5)
        except Exception:
            pass
    with _lock:
        _thread = None
    return {"ok": True}


def get_status() -> dict:
    with _lock:
        bindings = [
            {"name": v["name"], "hotkey": v["hotkey"]}
            for v in _bindings.values()
        ]
    return {
        "running":   bool(_thread and _thread.is_alive()),
        "bindings":  bindings,
        "last_err":  _last_err,
    }
