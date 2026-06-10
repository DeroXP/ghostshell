"""Screenshot capture — fast multi-monitor PNGs via mss.

Three target modes:
  • foreground  — capture whatever window is in front right now
                  (game OR not — we don't filter; if the user pressed
                  the hotkey while looking at Discord that's what they
                  meant to capture).
  • game        — capture the foreground game window specifically.
                  Returns an error if no known game is foregrounded.
  • display     — capture the entire monitor that contains the
                  foreground window.

Output: PNG to %USERPROFILE%/Pictures/GhostShell/Screenshots/
        named `<game_or_app>_<YYYY-MM-DD>_<HH-MM-SS>.png`

Why this lives in its own module rather than recoil_capture's:
recoil_capture is a stateful capture session for the anti-recoil
trace.  Screenshots are one-shot + opaque.  Keeping them separate
avoids accidentally interfering with a live recoil-capture session.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from datetime import datetime
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger(__name__)


SCREENSHOTS_DIR = os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"),
    "Pictures", "GhostShell", "Screenshots",
)

# Lazy mss import (it loads native libs that take ~25 ms)
_mss = None
_mss_lock = threading.Lock()


def _ensure_mss():
    global _mss
    if _mss is not None:
        return True
    try:
        import mss as _mss_mod
        _mss = _mss_mod
        return True
    except Exception as e:
        log.warning(f"mss unavailable: {e}")
        return False


def _foreground_window_info() -> dict:
    """Return foreground window's exe + bounding rect.  Empty dict if
    nothing meaningful is foregrounded (desktop / fullscreen-exclusive
    game we can't reach).

    Used to:
      • Pick a sensible filename prefix (the exe name)
      • Crop to just the window's client rect (foreground mode)
      • Find which monitor it's on (display mode)
    """
    try:
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd: return {}

        # Bounding rect — use the full window rect rather than the client
        # rect for a "true window-with-titlebar" screenshot.  Most games
        # in windowed mode hide the titlebar anyway so it doesn't matter.
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return {}

        # Owning process exe
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        if pid.value:
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf = (ctypes.c_wchar * 260)()
                    size = wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        exe = os.path.basename(buf.value)
                finally:
                    kernel32.CloseHandle(h)

        return {
            "hwnd":  int(hwnd),
            "left":   int(rect.left),
            "top":    int(rect.top),
            "right":  int(rect.right),
            "bottom": int(rect.bottom),
            "exe":    exe,
            "pid":    int(pid.value) if pid.value else 0,
        }
    except Exception as e:
        log.debug(f"_foreground_window_info failed: {e}")
        return {}


def _sanitize(name: str) -> str:
    """Strip filesystem-unfriendly chars from a name for use in filenames."""
    bad = '<>:"/\\|?*'
    out = "".join(c if c not in bad else "_" for c in (name or "")).strip()
    return out or "unknown"


def _output_path(prefix: str) -> str:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe = _sanitize(prefix).removesuffix(".exe")
    return os.path.join(SCREENSHOTS_DIR, f"{safe}_{ts}.png")


def take_screenshot(target: str = "foreground") -> dict:
    """Capture and save a PNG.  Returns {ok, path, target, exe, size}."""
    if not _ensure_mss():
        return {"ok": False, "err": "mss library unavailable"}

    target = (target or "foreground").lower()
    fg = _foreground_window_info()

    # ── Decide capture region + filename prefix ───────────────────
    region = None
    prefix = "screenshot"

    if target == "game":
        # Require a known game to be in foreground
        try:
            from core import game_profiles
            game_info = game_profiles.get_foreground_game()
        except Exception:
            game_info = None
        if not game_info:
            return {"ok": False, "err": "no known game is in the foreground"}
        if not fg:
            return {"ok": False, "err": "couldn't read foreground window rect"}
        if fg["right"] - fg["left"] < 100 or fg["bottom"] - fg["top"] < 100:
            return {"ok": False, "err": "game window too small to capture"}
        region = {"left": fg["left"], "top": fg["top"],
                  "width": fg["right"] - fg["left"],
                  "height": fg["bottom"] - fg["top"]}
        prefix = game_info["exe"]

    elif target == "foreground":
        if not fg:
            return {"ok": False, "err": "couldn't read foreground window"}
        region = {"left": fg["left"], "top": fg["top"],
                  "width":  fg["right"] - fg["left"],
                  "height": fg["bottom"] - fg["top"]}
        prefix = fg.get("exe") or "foreground"

    elif target == "display":
        # Full primary monitor.  mss's monitor[1] is the primary; [0] is
        # the virtual screen (all monitors stitched).
        region = None  # signal "grab monitor"
        prefix = fg.get("exe") or "display"

    else:
        return {"ok": False, "err": f"unknown target '{target}'"}

    # ── Capture ──────────────────────────────────────────────────
    out_path = _output_path(prefix)
    try:
        with _mss.mss() as sct:
            if region:
                grab = sct.grab(region)
            else:
                grab = sct.grab(sct.monitors[1])
            # mss.tools.to_png is the fastest path — no PIL roundtrip.
            from mss import tools as _mss_tools
            _mss_tools.to_png(grab.rgb, grab.size, output=out_path)
    except Exception as e:
        return {"ok": False, "err": f"capture failed: {e}"}

    try: size = os.path.getsize(out_path)
    except Exception: size = 0
    log.info(f"screenshot saved: {out_path} ({size} B, target={target})")
    return {
        "ok":     True,
        "path":   out_path,
        "target": target,
        "exe":    fg.get("exe", ""),
        "size":   size,
        "width":  (region or sct.monitors[1])["width"]  if region else None,
        "height": (region or sct.monitors[1])["height"] if region else None,
    }


def list_recent(limit: int = 30) -> list:
    """Return the most-recent screenshots (newest first).  Each:
    {path, name, size, mtime}."""
    if not os.path.isdir(SCREENSHOTS_DIR):
        return []
    items = []
    try:
        for fn in os.listdir(SCREENSHOTS_DIR):
            full = os.path.join(SCREENSHOTS_DIR, fn)
            if not os.path.isfile(full): continue
            if not fn.lower().endswith((".png", ".jpg", ".jpeg")): continue
            try:
                st = os.stat(full)
            except OSError: continue
            items.append({
                "path":  full,
                "name":  fn,
                "size":  st.st_size,
                "mtime": st.st_mtime,
            })
    except Exception as e:
        log.debug(f"list_recent failed: {e}")
        return []
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def delete_screenshot(path: str) -> dict:
    """Delete by absolute path.  Guards against deleting anything
    outside SCREENSHOTS_DIR (so a malicious API caller can't unlink
    arbitrary files)."""
    if not path: return {"ok": False, "err": "no path"}
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(SCREENSHOTS_DIR)
    if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
        return {"ok": False, "err": "path outside screenshots dir"}
    try:
        os.remove(abs_path)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def get_screenshots_dir() -> str:
    return SCREENSHOTS_DIR
