"""Low-level keyboard + mouse hooks for the gamepad mapper.

Uses Win32 SetWindowsHookEx with WH_KEYBOARD_LL and WH_MOUSE_LL — the
same API every input remapper on Windows uses (AutoHotkey, reWASD,
JoyToKey, X-Mouse Button Control etc.).  Each hook runs on a dedicated
thread with its own message pump.

Events are pushed onto a thread-safe queue that the gamepad-mapper's
main polling loop drains every tick.  This keeps the hook callbacks
short (Windows requires <300 ms or it auto-disables them) and keeps
all the actual remap logic in one place.

Public API:
    start_keyboard_hook()  / stop_keyboard_hook()
    start_mouse_hook()     / stop_mouse_hook()
    drain_events()         -> list of pending {source, trigger, pressed,
                                                 mx, my, dx, dy} dicts
    get_status()           -> {keyboard_running, mouse_running,
                                kb_events_total, mouse_events_total}

A consumer should call drain_events() at >= 60 Hz so the queue doesn't
back up.  The mapper's 60-1000 Hz polling loop satisfies this trivially.
"""

from __future__ import annotations
import ctypes
import threading
import queue
import time
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# Win32 constants
# ────────────────────────────────────────────────────────────────────

WH_KEYBOARD_LL  = 13
WH_MOUSE_LL     = 14
HC_ACTION       = 0

WM_KEYDOWN      = 0x0100
WM_KEYUP        = 0x0101
WM_SYSKEYDOWN   = 0x0104
WM_SYSKEYUP     = 0x0105

WM_LBUTTONDOWN  = 0x0201
WM_LBUTTONUP    = 0x0202
WM_RBUTTONDOWN  = 0x0204
WM_RBUTTONUP    = 0x0205
WM_MBUTTONDOWN  = 0x0207
WM_MBUTTONUP    = 0x0208
WM_XBUTTONDOWN  = 0x020B
WM_XBUTTONUP    = 0x020C
WM_MOUSEMOVE    = 0x0200
WM_MOUSEWHEEL   = 0x020A

XBUTTON1        = 0x0001
XBUTTON2        = 0x0002


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wintypes.DWORD),
        ("scanCode",    wintypes.DWORD),
        ("flags",       wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          POINT),
        ("mouseData",   wintypes.DWORD),
        ("flags",       wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_user32   = ctypes.WinDLL("user32",   use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LowLevelKbdProc   = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM)
LowLevelMouseProc = LowLevelKbdProc

_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, LowLevelKbdProc, wintypes.HINSTANCE, wintypes.DWORD,
]
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.CallNextHookEx.restype = ctypes.c_long
_user32.GetMessageW.argtypes = [
    ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_uint,
]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# ────────────────────────────────────────────────────────────────────
# Event queue — drained by the mapper polling loop
# ────────────────────────────────────────────────────────────────────

_event_q: queue.Queue = queue.Queue(maxsize=4096)
_stats = {"kb_events_total": 0, "mouse_events_total": 0}

# We keep refs to the bound callbacks so Python doesn't GC them while
# Windows is using them — that would crash the process.
_kb_cb_ref:    Optional[LowLevelKbdProc]   = None
_mouse_cb_ref: Optional[LowLevelMouseProc] = None
_kb_hook:    Optional[wintypes.HHOOK] = None
_mouse_hook: Optional[wintypes.HHOOK] = None
_kb_thread:    Optional[threading.Thread] = None
_mouse_thread: Optional[threading.Thread] = None
_kb_thread_id    = 0
_mouse_thread_id = 0
_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────
# Friendly key-name → VK code (and reverse) for the UI bind picker
# ────────────────────────────────────────────────────────────────────

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift",
    0x11: "Ctrl",     0x12: "Alt", 0x13: "Pause", 0x14: "CapsLock",
    0x1B: "Esc",      0x20: "Space",
    0x21: "PgUp",     0x22: "PgDn", 0x23: "End",  0x24: "Home",
    0x25: "Left",     0x26: "Up",   0x27: "Right",0x28: "Down",
    0x2C: "PrtSc",    0x2D: "Insert",0x2E: "Delete",
    0x5B: "LWin",     0x5C: "RWin",
}
for _v, _n in [(c, chr(c)) for c in range(0x30, 0x3A)] + \
              [(c, chr(c)) for c in range(0x41, 0x5B)]:
    VK_NAMES.setdefault(_v, _n)
for _i in range(1, 25):
    VK_NAMES.setdefault(0x6F + _i if _i > 12 else 0x6F + _i, f"F{_i}")
for _i, _v in enumerate([0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77,
                         0x78, 0x79, 0x7A, 0x7B], 1):
    VK_NAMES[_v] = f"F{_i}"

VK_BY_NAME = {v.lower(): k for k, v in VK_NAMES.items()}


# Mouse button → friendly name (used as the trigger string for input mappings)
MOUSE_TRIGGERS = {
    "M_LEFT":    WM_LBUTTONDOWN,
    "M_RIGHT":   WM_RBUTTONDOWN,
    "M_MIDDLE":  WM_MBUTTONDOWN,
    "M_X1":      "x1",     # WM_XBUTTONDOWN with high-word == XBUTTON1
    "M_X2":      "x2",     # WM_XBUTTONDOWN with high-word == XBUTTON2
}


# ────────────────────────────────────────────────────────────────────
# Hook callbacks
# ────────────────────────────────────────────────────────────────────

def _kb_callback(nCode, wParam, lParam):
    if nCode == HC_ACTION:
        try:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
            pressed = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if pressed or up:
                trigger = VK_NAMES.get(int(kb.vkCode), f"VK_0x{kb.vkCode:02X}")
                try:
                    _event_q.put_nowait({
                        "source":  "keyboard",
                        "trigger": trigger,
                        "vk":      int(kb.vkCode),
                        "pressed": bool(pressed),
                    })
                    _stats["kb_events_total"] += 1
                except queue.Full:
                    pass    # drop event rather than block the hook
        except Exception:
            pass            # never let a hook callback raise to Windows
    return _user32.CallNextHookEx(None, nCode, wParam, lParam)


def _mouse_callback(nCode, wParam, lParam):
    if nCode == HC_ACTION:
        try:
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT))[0]
            ev = None
            if wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                ev = {"source": "mouse", "trigger": "M_LEFT",
                      "pressed": wParam == WM_LBUTTONDOWN}
            elif wParam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
                ev = {"source": "mouse", "trigger": "M_RIGHT",
                      "pressed": wParam == WM_RBUTTONDOWN}
            elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                ev = {"source": "mouse", "trigger": "M_MIDDLE",
                      "pressed": wParam == WM_MBUTTONDOWN}
            elif wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                xbtn = (ms.mouseData >> 16) & 0xFFFF
                trigger = "M_X1" if xbtn == XBUTTON1 else "M_X2"
                ev = {"source": "mouse", "trigger": trigger,
                      "pressed": wParam == WM_XBUTTONDOWN}
            elif wParam == WM_MOUSEMOVE:
                ev = {"source": "mouse", "trigger": "M_MOVE",
                      "pressed": True,
                      "mx": int(ms.pt.x), "my": int(ms.pt.y)}
            elif wParam == WM_MOUSEWHEEL:
                delta = ctypes.c_int16((ms.mouseData >> 16) & 0xFFFF).value
                ev = {"source": "mouse",
                      "trigger": "M_WHEEL_UP" if delta > 0 else "M_WHEEL_DOWN",
                      "pressed": True}
            if ev is not None:
                try:
                    _event_q.put_nowait(ev)
                    _stats["mouse_events_total"] += 1
                except queue.Full:
                    pass
        except Exception:
            pass
    return _user32.CallNextHookEx(None, nCode, wParam, lParam)


# ────────────────────────────────────────────────────────────────────
# Hook lifetime — each runs on its own thread with a message pump
# ────────────────────────────────────────────────────────────────────

WM_QUIT = 0x0012


def _kb_thread_fn():
    global _kb_hook, _kb_cb_ref, _kb_thread_id
    _kb_thread_id = _kernel32.GetCurrentThreadId()
    _kb_cb_ref = LowLevelKbdProc(_kb_callback)
    _kb_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, _kb_cb_ref, None, 0)
    if not _kb_hook:
        log.warning(f"SetWindowsHookEx KB failed: {ctypes.get_last_error()}")
        return
    log.info("keyboard hook installed")
    msg = ctypes.c_buffer(48)
    while _user32.GetMessageW(msg, None, 0, 0):
        pass     # we only need the pump; we don't process messages
    _user32.UnhookWindowsHookEx(_kb_hook)
    _kb_hook = None
    log.info("keyboard hook removed")


def _mouse_thread_fn():
    global _mouse_hook, _mouse_cb_ref, _mouse_thread_id
    _mouse_thread_id = _kernel32.GetCurrentThreadId()
    _mouse_cb_ref = LowLevelMouseProc(_mouse_callback)
    _mouse_hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_cb_ref, None, 0)
    if not _mouse_hook:
        log.warning(f"SetWindowsHookEx mouse failed: {ctypes.get_last_error()}")
        return
    log.info("mouse hook installed")
    msg = ctypes.c_buffer(48)
    while _user32.GetMessageW(msg, None, 0, 0):
        pass
    _user32.UnhookWindowsHookEx(_mouse_hook)
    _mouse_hook = None
    log.info("mouse hook removed")


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def start_keyboard_hook() -> dict:
    global _kb_thread
    with _lock:
        if _kb_thread and _kb_thread.is_alive():
            return {"ok": True, "msg": "already running"}
        _kb_thread = threading.Thread(target=_kb_thread_fn, daemon=True,
                                      name="input-hook-kb")
        _kb_thread.start()
    return {"ok": True}


def _stop_hook_thread(thread: Optional[threading.Thread], tid: int, name: str) -> bool:
    """Ask `tid`'s message pump to quit and wait for the thread to actually
    exit before we let the caller believe the hook is gone.

    PostThreadMessageW can fail if the target thread hasn't created a
    message queue yet (a very real race if stop is called right after
    start, before the thread reaches GetMessageW) — a stale thread left
    running would keep its native hook installed while our module-level
    _kb_cb_ref/_kb_hook (or the mouse equivalents) get overwritten by a
    subsequent start(), risking Windows calling back into a callback
    object Python has since garbage-collected. Retry briefly, then wait
    for the thread to actually finish."""
    if not tid:
        return True
    for attempt in range(5):
        if _user32.PostThreadMessageW(tid, WM_QUIT, 0, 0):
            break
        if attempt == 4:
            log.warning(f"{name}: PostThreadMessageW failed after retries "
                        f"(tid={tid}); hook thread may not stop cleanly")
            return False
        time.sleep(0.05)
    if thread is not None:
        thread.join(timeout=1.0)
        if thread.is_alive():
            log.warning(f"{name}: hook thread did not exit within 1s after WM_QUIT")
            return False
    return True


def stop_keyboard_hook() -> dict:
    global _kb_thread, _kb_thread_id
    with _lock:
        # Held for the whole stop (including the join below) — releasing
        # early would let a concurrent start_keyboard_hook() slip in while
        # the old thread is still exiting and recreate the same race this
        # is meant to close.
        ok = _stop_hook_thread(_kb_thread, _kb_thread_id, "keyboard hook")
        _kb_thread = None
        _kb_thread_id = 0
    return {"ok": ok}


def start_mouse_hook() -> dict:
    global _mouse_thread
    with _lock:
        if _mouse_thread and _mouse_thread.is_alive():
            return {"ok": True, "msg": "already running"}
        _mouse_thread = threading.Thread(target=_mouse_thread_fn, daemon=True,
                                         name="input-hook-mouse")
        _mouse_thread.start()
    return {"ok": True}


def stop_mouse_hook() -> dict:
    global _mouse_thread, _mouse_thread_id
    with _lock:
        ok = _stop_hook_thread(_mouse_thread, _mouse_thread_id, "mouse hook")
        _mouse_thread = None
        _mouse_thread_id = 0
    return {"ok": ok}


def drain_events() -> list[dict]:
    """Drain every pending hook event.  Should be called from the
    mapper's polling loop on every tick."""
    out: list[dict] = []
    while True:
        try:
            out.append(_event_q.get_nowait())
        except queue.Empty:
            break
    return out


def get_status() -> dict:
    return {
        "keyboard_running":   bool(_kb_thread    and _kb_thread.is_alive()),
        "mouse_running":      bool(_mouse_thread and _mouse_thread.is_alive()),
        "kb_events_total":    _stats["kb_events_total"],
        "mouse_events_total": _stats["mouse_events_total"],
        "queue_size":         _event_q.qsize(),
    }
