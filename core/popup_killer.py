"""Popup watchdog — defensive layer that auto-closes the 'Get an app to
open this link' dialog if it ever appears.

Primary fix for the ms-gamebar:* popup is the no-op URL handlers in
debloater.block_gamebar_protocols() — that prevents Windows from
opening the picker at all.  But:

  • If a third-party app (Razer Synapse, Steam overlay, etc.) opens an
    ms-gamebar:// link via ShellExecuteEx() with a different elevation
    context, our HKCU override might be skipped.
  • Some system processes do the same.
  • Even if the no-op handler IS used, on rare races a brief picker
    flash may appear.

This watchdog runs in a low-overhead thread (1-second poll) and
sends WM_CLOSE to any 'Open With' picker dialog that mentions one of
the protocols we know are broken (ms-gamebar, ms-gamebarservices,
ms-gamingoverlay).

It uses ctypes/user32 — no extra deps.
"""

from __future__ import annotations
import ctypes
import threading
import time
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# Win32 plumbing
# ────────────────────────────────────────────────────────────────────

WM_CLOSE = 0x0010

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
    ctypes.c_bool, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL


# Phrases that identify the picker dialog we want to close.  The dialog
# is "Open With" / "How do you want to open this <X>?"  Title varies by
# Windows build, but the protocol token always appears somewhere.
TARGET_TOKENS = (
    "ms-gamebar",
    "ms-gamebarservices",
    "ms-gamingoverlay",
)


def _get_window_text(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    _user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value or ""


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def _enumerate_windows() -> list[tuple[int, str, str]]:
    """Return [(hwnd, class_name, window_text), ...] for every visible
    top-level window."""
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_window_text(hwnd)
            cls = _get_class_name(hwnd)
            if title or cls:
                out.append((hwnd, cls, title))
        except Exception:
            pass
        return True

    _user32.EnumWindows(_cb, 0)
    return out


# Window classes the "Get an app to open this link" picker is hosted in.
_PICKER_CLASSES = {"ApplicationFrameWindow", "#32770", "Shell_Flyout",
                   "Windows.UI.Core.CoreWindow"}


def _owner_exe(hwnd: int) -> str:
    """Lower-cased image name of the process that owns `hwnd`, or ''."""
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not k32.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))
    except Exception:
        return ""


def _is_picker_window(hwnd: int, class_name: str) -> bool:
    """A title match alone is not enough to close a window: a browser tab
    about this very error, a text file named after it or an Explorer folder
    all contain the same words — and used to be sent WM_CLOSE once a second.
    It also has to BE the picker: hosted in one of its window classes, or
    owned by OpenWith.exe."""
    return class_name in _PICKER_CLASSES or _owner_exe(hwnd) == "openwith.exe"


def _close_window(hwnd: int) -> bool:
    """PostMessage WM_CLOSE — never blocks, never throws if hwnd died."""
    try:
        return bool(_user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────
# Watchdog state
# ────────────────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_kill_count = 0
_last_kill_t = 0.0
_left_alone: set = set()        # hwnds whose title matched but which are not the picker


def _matches_target(window_text: str, class_name: str) -> bool:
    """Detect the 'Get an app to open this <protocol> link' picker."""
    text = (window_text or "").lower()
    if not text:
        return False
    # The picker class on Win11 is 'ApplicationFrameWindow' for the modern
    # one or '#32770' for the legacy dialog.  We rely on the title text
    # rather than the class so we don't have to track every Windows version.
    for token in TARGET_TOKENS:
        if token in text:
            return True
    # Sometimes the title is a translated "Get an app" header that doesn't
    # include the protocol name — match the English phrase as a fallback.
    if "get an app" in text and "link" in text:
        return True
    return False


def _watchdog_loop():
    global _kill_count, _last_kill_t
    log.info("popup-killer watchdog started")
    while not _stop.is_set():
        try:
            for hwnd, cls, title in _enumerate_windows():
                if not _matches_target(title, cls):
                    continue
                if not _is_picker_window(hwnd, cls):
                    # Logged once per window, so a picker hosted in a class
                    # this list does not know yet can be diagnosed from the log.
                    if hwnd not in _left_alone:
                        if len(_left_alone) > 500:
                            _left_alone.clear()
                        _left_alone.add(hwnd)
                        log.debug(f"popup-killer: title matched but not the picker — left alone "
                                  f"(cls={cls}, owner={_owner_exe(hwnd) or '?'})")
                    continue
                log.info(f"popup-killer: closing '{title}' (cls={cls}, hwnd={hwnd})")
                if _close_window(hwnd):
                    _kill_count += 1
                    _last_kill_t = time.time()
        except Exception as e:                      # noqa: BLE001
            log.debug(f"popup-killer tick failed: {e}")
        if _stop.wait(1.0):                # 1 Hz — picker isn't time-critical
            break
    log.info("popup-killer watchdog exiting")


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

def start() -> dict:
    global _thread
    if _thread and _thread.is_alive():
        return {"ok": True, "msg": "already running"}
    _stop.clear()
    _thread = threading.Thread(target=_watchdog_loop, daemon=True,
                               name="popup-killer")
    _thread.start()
    return {"ok": True}


def stop() -> dict:
    _stop.set()
    return {"ok": True}


def get_status() -> dict:
    return {
        "running":     bool(_thread and _thread.is_alive()),
        "kill_count":  _kill_count,
        "last_kill":   _last_kill_t,
    }
