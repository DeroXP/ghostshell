"""Clipper — instant replay + manual clips via FFmpeg.

Architecture
────────────
A long-running FFmpeg subprocess captures the chosen target continuously
and writes a rolling ring of 10-second segments to a temp directory.
When the user saves a clip we concat the last N seconds from the ring
into a single mp4 in %USERPROFILE%/Videos/GhostShell/Clips/.

**It is always recording when the toggle is on.** save_clip() does NOT
start recording — it copies what's already buffered.  Setting "60s
back" means "save the last 60 seconds of what was already captured".

Capture targets (v3.3 beta.2):
  • auto-follow-game-monitor  default — follow whichever monitor the
                              foreground game is on; restart FFmpeg
                              when it moves to a different display
  • game-window-only          capture just the foreground game's
                              window rect; restart on window move/focus
  • monitor:N                 lock to a specific monitor index
  • desktop                   capture the full virtual screen (all
                              monitors stitched — useful for streamers
                              with multi-monitor setups)

Audio:
  • mic    optional, picked from dshow device list (Realtek mic, etc)
  • system optional — auto-detects a loopback capture device
                       (Voicemeeter / VB-CABLE / virtual-audio-capturer /
                       Stereo Mix in that priority).  No user dropdown —
                       if a loopback driver is installed, we use it; if
                       none is installed, the UI shows install help.
  Both mixed with `-filter_complex amix`.

DPI note (v3.3.0-beta.4):
  pywebview makes our Python process DPI-aware, so EnumDisplayMonitors
  returns *physical* pixel coordinates.  FFmpeg's gdigrab is DPI-unaware
  — it sees the *virtualized* desktop.  On a high-DPI display the two
  coordinate spaces don't match, and gdigrab errors out:
      "Capture area (-2160,0),(0,3840) extends outside window area
       (-1440,0),(3840,2560)"
  (physical 4K-portrait coords clipped against the scaled virtual
  screen).  Fix: ALL coordinate-producing Win32 calls used by the
  clipper run inside `_dpi_unaware` scope so they return values
  consistent with gdigrab.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger, atomic_write_json

log = get_logger(__name__)


# ─── Paths + defaults ───────────────────────────────────────────────

CLIPS_DIR = os.path.join(
    os.environ.get("USERPROFILE") or os.path.expanduser("~"),
    "Videos", "GhostShell", "Clips",
)
BUFFER_DIR    = os.path.join(APPDATA_DIR, "clipper_buffer")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "clipper_settings.json")

# Capture target enum (stored as a string in settings)
TARGET_AUTO_FOLLOW    = "auto-follow"
TARGET_GAME_WINDOW    = "game-window"
TARGET_DESKTOP        = "desktop"
TARGET_MONITOR_PREFIX = "monitor:"

_DEFAULTS = {
    "enabled":            False,
    "buffer_seconds":     30,        # default clip length
    "max_buffer_seconds": 60,        # rolling ring wall-clock cap (configurable 30-600)
    "fps":                30,
    "encoder":            "auto",
    "bitrate_kbps":       12000,     # 12 Mbps default — good quality vs disk
    "save_hotkey":        "F10",
    "screenshot_hotkey":  "F9",
    "output_dir":         CLIPS_DIR,
    # Capture target.  v3.3.0-beta.12: default changed from
    # TARGET_AUTO_FOLLOW to monitor:0 (primary).  The auto-follow /
    # game-window / virtual-desktop modes were removed from the UI;
    # any legacy setting like "auto-follow" gracefully falls back to
    # primary monitor in _resolve_capture_region.
    "capture_target":     "monitor:0",
    # Audio
    "mic_enabled":        False,
    "mic_device":         "",        # exact dshow name, blank = "use default mic"
    "system_audio_enabled": False,
    "system_audio_device":  "",      # blank = try to auto-detect
}

# v3.3.0-beta.8: dropped from 10 -> 2.  The segment muxer only writes
# the moov atom of a segment WHEN it starts the next one, so the
# currently-being-written segment is always missing its index and is
# corrupt-looking on disk.  save_clip MUST skip that segment.  Smaller
# segments mean less trailing footage lost at save time:
#   10s segments → lose up to 10s of "the moment you wanted"
#    2s segments → lose up to 2s, which is barely noticeable
# Trade-off is 5× more segment files; on a 60s buffer that's 30 files
# instead of 6.  Negligible disk overhead, moov+mdat headers stay tiny.
SEGMENT_SECONDS = 2
MIN_BUFFER_SEC  = 15        # below this, ring buffer doesn't have time to fill
MAX_BUFFER_SEC  = 600       # 10 min cap


# ─── State ──────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state = {
    "running":       False,
    "ffmpeg_pid":    0,
    "started_at":    0.0,
    "ffmpeg_path":   "",
    "encoder_used":  "",
    "target_used":   "",
    "target_rect":   None,      # (x, y, w, h) currently captured, or None for title-based
    "target_title":  "",
    "segment_dir":   "",
    "last_err":      "",
    "stderr_tail":   [],
    "sys_audio_active":  False,
    "sys_audio_device":  "",
    "sys_audio_format":  "",     # human-readable "48 kHz × 2 ch" or ""
    "sys_audio_err":     "",
    "capture_method":    "",     # "ddagrab" | "gdigrab" | "" (idle)
}
_proc: Optional[subprocess.Popen] = None
_watcher_thread: Optional[threading.Thread] = None
_target_follow_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
# v3.3.0-beta.6: WASAPI loopback bridge (PyAudioWPatch).  Optional —
# `None` if system audio is off or pyaudiowpatch failed to import.
_sys_audio_bridge = None  # type: Optional["sys_audio_bridge.LoopbackBridge"]


# ─── Settings persistence ───────────────────────────────────────────

def _clamp_int(v, lo, hi):
    try: v = int(v)
    except Exception: return lo
    return max(lo, min(hi, v))


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        merged = {**_DEFAULTS, **data}
        # Sanitize ranges so a corrupted file can't ask FFmpeg for a
        # nonsensical buffer length / fps.
        merged["max_buffer_seconds"] = _clamp_int(merged.get("max_buffer_seconds", 60),
                                                    MIN_BUFFER_SEC, MAX_BUFFER_SEC)
        merged["buffer_seconds"] = _clamp_int(merged.get("buffer_seconds", 30), 5,
                                                merged["max_buffer_seconds"])
        merged["fps"] = _clamp_int(merged.get("fps", 30), 15, 120)
        merged["bitrate_kbps"] = _clamp_int(merged.get("bitrate_kbps", 12000),
                                              2000, 60000)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def _save_settings(s: dict) -> None:
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load_settings()


def set_settings(**kwargs) -> dict:
    s = _load_settings()
    for k, v in kwargs.items():
        if k in _DEFAULTS:
            s[k] = v
    # re-sanitize after writes
    s["max_buffer_seconds"] = _clamp_int(s["max_buffer_seconds"],
                                          MIN_BUFFER_SEC, MAX_BUFFER_SEC)
    s["buffer_seconds"] = _clamp_int(s["buffer_seconds"], 5, s["max_buffer_seconds"])
    s["fps"]            = _clamp_int(s["fps"], 15, 120)
    s["bitrate_kbps"]   = _clamp_int(s["bitrate_kbps"], 2000, 60000)
    # v3.3.0-beta.5: a `system_audio_device` value that isn't a known
    # loopback name is almost certainly a stale leftover from the
    # pre-beta.4 dropdown (which often saved the user's mic).  Clear
    # it so auto-detect can take over cleanly.
    sad = (s.get("system_audio_device") or "").strip()
    if sad and not _is_loopback_name(sad):
        log.info(f"clipper: clearing stale system_audio_device "
                 f"{sad!r} (not a loopback) on settings save")
        s["system_audio_device"] = ""
    _save_settings(s)
    return {"ok": True, "settings": s}


# ─── FFmpeg detection + encoder selection ───────────────────────────

# Cache for the ddagrab availability probe — `ffmpeg -filters` is slow
# (200-500 ms cold) and the answer doesn't change for a given ffmpeg
# binary.  TTL not needed; just a session-level "has it" flag.
_ddagrab_avail_cache: Optional[bool] = None


def _ffmpeg_has_ddagrab(ffmpeg: str) -> bool:
    """True if this FFmpeg build supports the `ddagrab` filter
    (Windows Desktop Duplication API — GPU-accelerated screen capture).

    ddagrab is the right tool for 4K / 60fps / multi-monitor capture.
    gdigrab uses GDI BitBlt which copies pixels CPU-side and stalls
    on high-resolution displays; ddagrab gets D3D11 textures directly
    from the GPU's display engine with near-zero CPU cost.

    Requires FFmpeg 5.0+; Gyan.dev "essentials" and "full" builds and
    BtbN builds all include it.  Older / minimal builds may not."""
    global _ddagrab_avail_cache
    if _ddagrab_avail_cache is not None:
        return _ddagrab_avail_cache
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,
        )
        _ddagrab_avail_cache = "ddagrab" in (r.stdout or "")
    except Exception:
        _ddagrab_avail_cache = False
    log.info(f"clipper: ddagrab available = {_ddagrab_avail_cache}")
    return _ddagrab_avail_cache


def _find_ffmpeg() -> Optional[str]:
    try:
        from core import dependencies
        info = dependencies._detect_ffmpeg()
        if info.get("installed"):
            return info["path"]
    except Exception: pass
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    return None


def _pick_encoder(ffmpeg_path: str, preference: str = "auto") -> str:
    if preference and preference != "auto":
        return preference
    try:
        r = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except Exception: return "libx264"
    for cand in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"):
        if cand in out:
            return cand
    return "libx264"


def _encoder_args(encoder: str, bitrate_kbps: int) -> list:
    b = f"{bitrate_kbps}k"
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "ll",
                "-rc", "cbr", "-b:v", b]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed",
                "-rc", "cbr", "-b:v", b]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", b]
    return ["-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-b:v", b]


# ─── Process / thread priority shim (v3.3.0-beta.9) ────────────────

# Windows CreateProcess priority class flags.  NORMAL is the default
# for everything; ABOVE_NORMAL gives a noticeable boost without
# crowding out games; HIGH is aggressive but safe (REALTIME would let
# us starve the audio subsystem and is never used).
_NORMAL_PRIORITY_CLASS       = 0x00000020
_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
_HIGH_PRIORITY_CLASS         = 0x00000080
_CREATE_NO_WINDOW            = 0x08000000

# SetThreadPriority levels (relative to the process's priority class)
_THREAD_PRIORITY_HIGHEST       = 2
_THREAD_PRIORITY_TIME_CRITICAL = 15

# Saved priority-class of the GhostShell process before we boosted it,
# so stop_buffer can restore it cleanly.  None when no boost is active.
_prev_priority_class: Optional[int] = None


def _boost_self_priority(target_class: int = _HIGH_PRIORITY_CLASS) -> Optional[int]:
    """Bump the running GhostShell process to a higher Windows priority
    class so the clipper's ffmpeg subprocess + WASAPI bridge thread
    don't get scheduled away by a fullscreen game.

    Returns the previous priority class (so caller can restore) or
    None on failure.  Errors are non-fatal — recording works at normal
    priority too, just with more chance of frame drops under load."""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetCurrentProcess()
        k32.GetPriorityClass.restype = ctypes.c_uint
        old = k32.GetPriorityClass(h)
        if k32.SetPriorityClass(h, target_class):
            log.info(f"clipper: process priority {old:#x} → {target_class:#x}")
            return old
        else:
            log.debug(f"clipper: SetPriorityClass({target_class:#x}) returned 0 "
                      f"(GetLastError={k32.GetLastError()})")
    except Exception as e:
        log.debug(f"clipper: priority boost failed: {e}")
    return None


def _restore_self_priority(old: Optional[int]) -> None:
    """Restore process priority to whatever it was before _boost_self_priority."""
    if old is None: return
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetCurrentProcess()
        if k32.SetPriorityClass(h, old):
            log.info(f"clipper: process priority restored to {old:#x}")
    except Exception as e:
        log.debug(f"clipper: priority restore failed: {e}")


# ─── DPI awareness shim ─────────────────────────────────────────────

class _dpi_unaware:
    """Context manager: switch the calling thread to DPI-unaware
    coordinate space.  All Win32 calls inside the `with` block —
    EnumDisplayMonitors, GetSystemMetrics, GetWindowRect — will return
    the same coordinates FFmpeg's gdigrab sees, so we can hand them
    straight to `-offset_x / -offset_y / -video_size` without any
    physical→virtual scaling.

    Requires Windows 10 v1607+ (SetThreadDpiAwarenessContext).  On
    older Windows the API is missing and this becomes a no-op — fine,
    because pre-1607 systems don't have per-thread DPI virtualization
    drift either."""
    _CONTEXT_UNAWARE = ctypes.c_void_p(-1)

    def __init__(self):
        self._prev = None

    def __enter__(self):
        try:
            u = ctypes.windll.user32
            if hasattr(u, "SetThreadDpiAwarenessContext"):
                u.SetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
                u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                self._prev = u.SetThreadDpiAwarenessContext(self._CONTEXT_UNAWARE)
        except Exception:
            self._prev = None
        return self

    def __exit__(self, *_a):
        if self._prev is not None:
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(self._prev)
            except Exception: pass


# ─── Audio device enumeration ───────────────────────────────────────

# Substrings that uniquely identify a loopback capture device in the
# DirectShow audio list.  These are the only devices that can deliver
# system playback audio into FFmpeg on Windows.  Ordered by quality:
# Voicemeeter virtual outputs are cleanest, virtual-audio-capturer
# (screen-capture-recorder) is most reliable, VB-CABLE is most common,
# Stereo Mix is built-in but disabled by default on most systems.
_LOOPBACK_PRIORITY = [
    "voicemeeter vaio",
    "voicemeeter out",
    "voicemeeter aux",
    "virtual-audio-capturer",
    "virtual audio capturer",
    "cable output",          # VB-CABLE (incl. "CABLE Output (VB-Audio Virtual Cable)")
    "vb-cable",
    "vb-audio",
    "stereo mix",
    "what u hear",
    "what you hear",
    "wave out mix",
    "loopback",
]


def _is_loopback_name(name: str) -> bool:
    """True if `name` looks like a system-audio loopback capture device.
    Used both for auto-detect and for sanitizing stale overrides — a
    user's pre-v3.3.0-beta.4 `system_audio_device` value might be a
    leftover mic name from when the UI had a dropdown that defaulted
    to the first dshow audio device (often a mic).  If it doesn't
    look like a loopback, we ignore it and auto-detect instead."""
    if not name: return False
    nlow = str(name).lower()
    return any(tok in nlow for tok in _LOOPBACK_PRIORITY)


# Cache for the dshow probe — spawning ffmpeg.exe to list devices is
# slow (50-150 ms on cold cache, plus a hidden console pop on some
# AVs) and `get_status()` gets called every few seconds while the user
# is on the Capture page.  Devices rarely change at runtime, so a TTL
# cache makes this effectively free for the status poll.
_AUDIO_CACHE_TTL = 60.0
_audio_cache_ts   = 0.0
_audio_cache_data: Optional[dict] = None
_audio_cache_lock = threading.Lock()


def _invalidate_audio_cache() -> None:
    """Force a re-probe on the next list_audio_devices() call.
    Wired into Refresh buttons in the UI so plugging in a new mic /
    enabling Stereo Mix mid-session doesn't require restarting."""
    global _audio_cache_ts, _audio_cache_data
    with _audio_cache_lock:
        _audio_cache_ts = 0.0
        _audio_cache_data = None


def auto_detect_loopback() -> dict:
    """Find the best system-audio loopback device on this machine.

    Returns {found: bool, name: str, all: [str], matched_token: str}.
    `name` is the exact dshow device name to pass to `-i audio=<name>`.
    Empty `name` means no loopback driver is installed — UI should
    prompt user to install VB-CABLE / Voicemeeter / etc.

    Backed by the same 60-s cache as list_audio_devices() so repeated
    status polls don't spawn ffmpeg over and over.
    """
    info = list_audio_devices()
    if not info.get("ok"):
        return {"found": False, "name": "", "all": [],
                "matched_token": "", "err": info.get("err", "")}
    audio = info.get("audio") or []
    names = [d.get("name", "") for d in audio]
    names_lower = [(n, n.lower()) for n in names]
    loopback_candidates = []
    matched_token = ""
    chosen = ""
    for token in _LOOPBACK_PRIORITY:
        for n, nlow in names_lower:
            if token in nlow:
                if not chosen:
                    chosen = n
                    matched_token = token
                if n not in loopback_candidates:
                    loopback_candidates.append(n)
    return {
        "found":         bool(chosen),
        "name":          chosen,
        "all":           loopback_candidates,
        "matched_token": matched_token,
    }


def list_audio_devices(force_refresh: bool = False) -> dict:
    """Probe FFmpeg's dshow device list.  Returns {ok, inputs} where
    inputs is a list of {name, kind: 'audio'|'video', alternative_name}.

    Used by the UI to populate the mic / system-audio dropdowns.  We
    only care about audio devices here but parse video too (cheap).

    v3.3.0-beta.5: cached for 60 s.  Spawning ffmpeg.exe for every
    status poll on the Capture page was the main perf regression of
    beta.4 — ffmpeg cold-starts in 50-150 ms and the status poll fires
    every few seconds.  Devices rarely change at runtime so a TTL
    cache is effectively free; users can hit "Refresh devices" or
    toggle the buffer to force a re-probe."""
    global _audio_cache_ts, _audio_cache_data
    now = time.time()
    if not force_refresh:
        with _audio_cache_lock:
            if _audio_cache_data is not None and (now - _audio_cache_ts) < _AUDIO_CACHE_TTL:
                return _audio_cache_data
    ff = _find_ffmpeg()
    if not ff:
        return {"ok": False, "err": "ffmpeg not installed"}
    try:
        # FFmpeg prints device list to stderr.  Force English locale
        # so the parsing regex doesn't get confused by translations.
        r = subprocess.run(
            [ff, "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000,
        )
    except Exception as e:
        return {"ok": False, "err": str(e)}

    text = (r.stderr or "") + (r.stdout or "")
    inputs = []
    # Lines look like:
    #   [dshow @ 0x...] "Microphone (Realtek...)"
    #   [dshow @ 0x...]   Alternative name "@device_cm_{...}\..."
    # The "(audio)" / "(video)" marker is on separate header sections
    # in older builds, inline in newer ones.  We track current section.
    section = None
    last_name = None
    for line in text.splitlines():
        L = line.strip()
        if "DirectShow audio devices" in line:
            section = "audio"; continue
        if "DirectShow video devices" in line:
            section = "video"; continue
        # Inline tagging: ' "..." (audio)' or ' "..." (video)'
        m_tag = re.search(r'"([^"]+)"\s*\((audio|video)\)', line)
        if m_tag:
            name, kind = m_tag.group(1), m_tag.group(2)
            inputs.append({"name": name, "kind": kind, "alternative_name": ""})
            last_name = name
            continue
        m_quoted = re.search(r'"([^"]+)"', line)
        if m_quoted and section in ("audio", "video"):
            name = m_quoted.group(1)
            if "Alternative name" in line and inputs and last_name:
                inputs[-1]["alternative_name"] = name
            else:
                inputs.append({"name": name, "kind": section,
                                "alternative_name": ""})
                last_name = name
    result = {"ok": True,
              "inputs":     inputs,
              "audio":      [d for d in inputs if d["kind"] == "audio"],
              "video":      [d for d in inputs if d["kind"] == "video"]}
    with _audio_cache_lock:
        _audio_cache_ts   = time.time()
        _audio_cache_data = result
    return result


# ─── Monitor + window geometry ──────────────────────────────────────

# Monitor list cache.  EnumDisplayMonitors + GetMonitorInfoW for each
# display takes 2-10 ms — fast in isolation but get_status() is hit
# every few seconds and so was paying that cost forever.  Monitor
# topology only changes when the user replugs / rotates / rearranges
# displays in Windows Settings, so a 30 s cache is generous.
_MON_CACHE_TTL = 30.0
_mon_cache_ts   = 0.0
_mon_cache_data: Optional[list] = None
_mon_cache_lock = threading.Lock()


def _invalidate_monitor_cache() -> None:
    global _mon_cache_ts, _mon_cache_data
    with _mon_cache_lock:
        _mon_cache_ts = 0.0
        _mon_cache_data = None


def list_monitors(force_refresh: bool = False) -> list:
    """Enumerate all displays via Win32.  Returns
    [{index, x, y, w, h, physical_w, physical_h, primary, name}].
    Index 0 = primary.

    Coordinates (x, y, w, h) are in *DPI-virtual* space (matching what
    gdigrab sees) — see `_dpi_unaware` docstring for the why.

    physical_w / physical_h are the REAL pixel dimensions the user
    sees in Windows Display Settings (e.g. 3840x2160 for a 4K monitor
    at 150 % scaling, vs the DPI-unaware 2560x1440 that w/h report).
    Use these for UI labels; use w/h for any FFmpeg-side coords.

    v3.3.0-beta.5: cached for 30 s.  Monitor topology rarely changes
    at runtime; if the user moves displays around they can re-open
    the Capture page (which can call this with force_refresh=True
    in future) or restart GhostShell.

    Order is stable across calls within a session; if the user
    rearranges displays in Windows Settings, GhostShell needs a
    restart for the changes to flow."""
    global _mon_cache_ts, _mon_cache_data
    now = time.time()
    if not force_refresh:
        with _mon_cache_lock:
            if _mon_cache_data is not None and (now - _mon_cache_ts) < _MON_CACHE_TTL:
                return list(_mon_cache_data)
    out = []
    try:
        user32 = ctypes.windll.user32
        # MONITORINFOEX struct
        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD),
                        ("szDevice", ctypes.c_wchar * 32)]

        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(RECT), wintypes.LPARAM,
        )

        results = []
        def _cb(hmon, hdc, rect_p, lparam):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                results.append({
                    "rect":   (r.left, r.top, r.right, r.bottom),
                    "name":   mi.szDevice,
                    "primary": bool(mi.dwFlags & 1),    # MONITORINFOF_PRIMARY
                })
            return 1

        cb = MONITORENUMPROC(_cb)
        with _dpi_unaware():
            user32.EnumDisplayMonitors(0, None, cb, 0)

        # Also gather the PHYSICAL pixel dimensions of each monitor for
        # UI display — a 4K monitor at 150 % scaling reports as
        # 2560×1440 in DPI-unaware coords, which confuses users who
        # know they have a 4K display.  Match physical entries to the
        # DPI-unaware ones by device name (szDevice is the same in
        # both contexts).
        physical_by_name = {}
        try:
            phys_results = []
            def _cb_phys(hmon, hdc, rect_p, lparam):
                mi = MONITORINFOEXW()
                mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    pr = mi.rcMonitor
                    phys_results.append({
                        "name": mi.szDevice,
                        "w":    pr.right - pr.left,
                        "h":    pr.bottom - pr.top,
                    })
                return 1
            cb_phys = MONITORENUMPROC(_cb_phys)
            # Enumerate in DPI-aware mode this time → physical pixels.
            user32.EnumDisplayMonitors(0, None, cb_phys, 0)
            for pr in phys_results:
                physical_by_name[pr["name"]] = (pr["w"], pr["h"])
        except Exception as e:
            log.debug(f"physical monitor enumeration failed: {e}")

        # Primary first, then secondaries left-to-right
        results.sort(key=lambda m: (not m["primary"], m["rect"][0]))
        for i, m in enumerate(results):
            x, y, r, b = m["rect"]
            virt_w = r - x
            virt_h = b - y
            phys_w, phys_h = physical_by_name.get(m["name"], (virt_w, virt_h))
            out.append({
                "index":      i,
                "x":          x, "y": y,
                "w":          virt_w, "h": virt_h,
                "physical_w": phys_w,
                "physical_h": phys_h,
                "primary":    m["primary"],
                "name":       m["name"],
            })
    except Exception as e:
        log.debug(f"list_monitors failed: {e}")
    with _mon_cache_lock:
        _mon_cache_ts   = time.time()
        _mon_cache_data = list(out)
    return out


def _foreground_window_info() -> dict:
    """Foreground window + bounding rect.  {} if unreadable.

    Rect is in DPI-virtual coords (matching gdigrab + list_monitors)."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd: return {}
        rect = wintypes.RECT()
        with _dpi_unaware():
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return {}
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        title = ""
        if pid.value:
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if h:
                try:
                    buf = (ctypes.c_wchar * 260)()
                    size = wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        exe = os.path.basename(buf.value)
                finally:
                    kernel32.CloseHandle(h)
        # Window title
        try:
            tbuf = (ctypes.c_wchar * 512)()
            n = user32.GetWindowTextW(hwnd, tbuf, 512)
            title = tbuf.value if n else ""
        except Exception: pass
        return {
            "hwnd":   int(hwnd),
            "x":      int(rect.left),
            "y":      int(rect.top),
            "w":      int(rect.right - rect.left),
            "h":      int(rect.bottom - rect.top),
            "exe":    exe,
            "pid":    int(pid.value) if pid.value else 0,
            "title":  title,
        }
    except Exception: return {}


def _monitor_for_point(x: int, y: int) -> Optional[dict]:
    """Pick the monitor that contains (x, y).  Returns None if outside
    all monitors (rare — desktop fills the virtual screen)."""
    for m in list_monitors():
        if m["x"] <= x < m["x"] + m["w"] and m["y"] <= y < m["y"] + m["h"]:
            return m
    return None


def _resolve_capture_region(target: str) -> dict:
    """Map the configured capture_target setting into a concrete
    capture region or window-title for FFmpeg.  Returns:
        {mode, x, y, w, h, title, source, monitor_idx, is_virtual_screen}

      mode:               'monitor' | 'title' | 'desktop'
      monitor_idx:        index of the monitor in list_monitors(), or -1
                          if this is a virtual-screen / window-title /
                          fallback region
      is_virtual_screen:  True only for the all-monitors-stitched case
                          (TARGET_DESKTOP) — used to decide whether
                          ddagrab can handle it (it can't — falls back
                          to gdigrab in that one case)

    All coords are DPI-virtualized (match gdigrab) — see _dpi_unaware.
    `source` describes WHY we picked this region — surfaced in UI.
    """
    # Default desktop = virtual screen (full)
    with _dpi_unaware():
        full_x = ctypes.windll.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        full_y = ctypes.windll.user32.GetSystemMetrics(77)
        full_w = ctypes.windll.user32.GetSystemMetrics(78)
        full_h = ctypes.windll.user32.GetSystemMetrics(79)

    if target.startswith(TARGET_MONITOR_PREFIX):
        try: idx = int(target.split(":", 1)[1])
        except Exception: idx = 0
        mons = list_monitors()
        if 0 <= idx < len(mons):
            m = mons[idx]
            return {"mode": "monitor", "x": m["x"], "y": m["y"],
                    "w": m["w"], "h": m["h"], "title": "",
                    "source": f"monitor {idx}: {m['name']}",
                    "monitor_idx": idx, "is_virtual_screen": False}
        return {"mode": "monitor", "x": full_x, "y": full_y,
                "w": full_w, "h": full_h, "title": "",
                "source": f"monitor:{idx} not found — falling back to desktop",
                "monitor_idx": -1, "is_virtual_screen": True}

    if target == TARGET_DESKTOP:
        return {"mode": "monitor", "x": full_x, "y": full_y,
                "w": full_w, "h": full_h, "title": "",
                "source": "full virtual desktop (all monitors)",
                "monitor_idx": -1, "is_virtual_screen": True}

    if target == TARGET_GAME_WINDOW:
        fg = _foreground_window_info()
        if not fg or not fg.get("title"):
            # Fall back to auto-follow monitor when no game in foreground
            return _resolve_capture_region(TARGET_AUTO_FOLLOW) | {
                "source": "game-window mode but no foreground game — using monitor fallback"
            }
        return {"mode": "title", "x": 0, "y": 0, "w": 0, "h": 0,
                "title": fg["title"],
                "source": f"game window: '{fg['title']}' ({fg.get('exe','?')})",
                "monitor_idx": -1, "is_virtual_screen": False}

    # auto-follow (default)
    fg = _foreground_window_info()
    if fg:
        cx = fg["x"] + fg["w"] // 2
        cy = fg["y"] + fg["h"] // 2
        mons = list_monitors()
        for i, m in enumerate(mons):
            if m["x"] <= cx < m["x"] + m["w"] and m["y"] <= cy < m["y"] + m["h"]:
                return {"mode": "monitor", "x": m["x"], "y": m["y"],
                        "w": m["w"], "h": m["h"], "title": "",
                        "source": f"foreground monitor ({m['name']})",
                        "monitor_idx": i, "is_virtual_screen": False}
    # No foreground or off-screen — primary monitor (index 0 after sort)
    mons = list_monitors()
    if mons:
        m = mons[0]
        return {"mode": "monitor", "x": m["x"], "y": m["y"],
                "w": m["w"], "h": m["h"], "title": "",
                "source": "primary monitor (no foreground game)",
                "monitor_idx": 0, "is_virtual_screen": False}
    return {"mode": "monitor", "x": full_x, "y": full_y,
            "w": full_w, "h": full_h, "title": "",
            "source": "full desktop (couldn't enumerate monitors)",
            "monitor_idx": -1, "is_virtual_screen": True}


# ─── Foreground-watcher: restart FFmpeg when target changes ─────────

def _target_watcher(stop_event: threading.Event):
    """Run in a thread.  Polls the resolved target every 2 s; if the
    rect or title materially changed, restart FFmpeg with the new
    target.  Only does work for the auto-follow + game-window modes;
    static monitor / desktop targets never change."""
    last_rect = None
    last_title = ""
    while not stop_event.is_set():
        try:
            with _state_lock:
                if not _state["running"]:
                    return
                target_cfg = _load_settings().get("capture_target", TARGET_AUTO_FOLLOW)

            if (target_cfg not in (TARGET_AUTO_FOLLOW, TARGET_GAME_WINDOW)
                    and not target_cfg.startswith(TARGET_MONITOR_PREFIX)):
                # static target — nothing to watch
                stop_event.wait(2.0)
                continue
            if target_cfg.startswith(TARGET_MONITOR_PREFIX) or target_cfg == TARGET_DESKTOP:
                stop_event.wait(2.0)
                continue

            region = _resolve_capture_region(target_cfg)
            cur_rect  = (region["x"], region["y"], region["w"], region["h"]) \
                          if region["mode"] == "monitor" else None
            cur_title = region["title"]

            if last_rect is None and last_title == "":
                last_rect, last_title = cur_rect, cur_title
                stop_event.wait(2.0)
                continue

            changed = False
            if region["mode"] == "monitor" and cur_rect != last_rect:
                changed = True
            if region["mode"] == "title" and cur_title != last_title:
                changed = True

            if changed:
                log.info(f"capture target changed → "
                         f"restarting FFmpeg ({region['source']})")
                _restart_internal()
                last_rect, last_title = cur_rect, cur_title
        except Exception as e:
            log.debug(f"target watcher tick failed: {e}")
        stop_event.wait(2.0)


def _restart_internal():
    """Internal: stop current FFmpeg and re-spawn with the same buffer
    settings.  Used by the watcher when the capture target changes."""
    stop_buffer()
    time.sleep(0.5)
    start_buffer()


# ─── Buffer lifecycle ───────────────────────────────────────────────

def _build_ffmpeg_cmd(ffmpeg: str, settings: dict,
                       region: dict, encoder: str,
                       seg_dir: str,
                       sys_audio_fmt: Optional[dict] = None) -> tuple:
    """Compose the full FFmpeg argv.  Returns (argv, n_segments_retained).

    Reads:
      • capture region (monitor rect OR window title)
      • mic settings (dshow, optional)
      • system audio format (if sys_audio_fmt is set, FFmpeg reads
        s16le PCM from stdin; otherwise no system audio).  Caller is
        responsible for starting the WASAPI loopback bridge that pumps
        PCM into proc.stdin.
      • encoder + bitrate + fps
      • max_buffer_seconds → segment_wrap

    Input indices (matter for the audio mix):
      0:v video (gdigrab)
      1:a mic (dshow) — only if mic_enabled + mic_device set
      next:a system audio (pipe:0 s16le) — only if sys_audio_fmt given
    """
    fps         = settings["fps"]
    bitrate     = settings["bitrate_kbps"]
    max_buf     = settings["max_buffer_seconds"]
    n_segments  = max(2, (max_buf + SEGMENT_SECONDS - 1) // SEGMENT_SECONDS)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
    ]

    # ── Video input (index 0) ───────────────────────────────────
    # v3.3.0-beta.7: prefer ddagrab (DXGI Desktop Duplication) for
    # single-monitor captures.  ddagrab pulls D3D11 textures directly
    # from the GPU's display engine — near-zero CPU cost and handles
    # 4K@60fps without breaking a sweat.  gdigrab uses GDI BitBlt
    # which stalls hard on high-resolution displays and was the
    # likely culprit for choppy clips on Ryzen 5 5600X + RX 6600.
    #
    # ddagrab can't do two things:
    #   • Window-title capture (mode='title' — game-window-only)
    #   • Virtual-screen capture (multi-monitor stitched — TARGET_DESKTOP)
    # Both fall back to gdigrab.
    use_ddagrab = (
        region["mode"] == "monitor"
        and not region.get("is_virtual_screen")
        and region.get("monitor_idx", -1) >= 0
        and _ffmpeg_has_ddagrab(ffmpeg)
    )

    if use_ddagrab:
        # `output_idx` is the DXGI adapter output index.  For typical
        # single-GPU setups it aligns with our Win32-enumerated monitor
        # index; for multi-GPU / hybrid configs the indices may diverge,
        # in which case the user picks the wrong display and just
        # re-saves the setting.  Documented limitation.
        #
        # ddagrab emits D3D11 hardware frames.  ffmpeg's encoders can't
        # auto-bridge those to system pixel formats (verified empirically
        # against h264_nvenc / h264_amf / libx264 on FFmpeg 8.0.1) —
        # the auto-scaler errors out with "Impossible to convert between
        # the formats supported by the filter 'Parsed_null_0' and the
        # filter 'auto_scale_0'".  Adding `hwdownload,format=bgra` after
        # ddagrab does the GPU→CPU bridge explicitly and the encoder
        # picks up clean BGRA pixels.
        out_idx = region["monitor_idx"]
        # v3.3.0-beta.11: append `fps=N:round=up` filter to force
        # constant framerate output.  ddagrab in theory does
        # dup_frames=true by default and should emit CFR, but under
        # real load (game running + audio bridge) ffprobe reports 1.5%
        # VFR with individual frame deltas up to 38 ms (= 26 fps
        # instantaneous) when target is 60 fps.  That's the "stutters
        # every couple seconds" the user is seeing.  The fps filter
        # pads/drops frames to maintain exact target spacing.
        #
        # round=up: when in doubt, DUPLICATE the previous frame
        # instead of dropping a new one.  For screen recording we'd
        # always rather have a tiny freeze than miss an actual game
        # frame — and the file always plays back at exactly target
        # fps regardless, so the duplicates are imperceptible.
        cmd += [
            "-f", "lavfi",
            "-i", (f"ddagrab=output_idx={out_idx}:framerate={fps}"
                    f":draw_mouse=0:allow_fallback=1,"
                    f"hwdownload,format=bgra,fps=fps={fps}:round=up"),
        ]
    elif region["mode"] == "title":
        cmd += [
            "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "0",
            "-i", f"title={region['title']}",
        ]
    else:
        # Virtual-screen / fallback path
        cmd += [
            "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "0",
            "-offset_x", str(region["x"]),
            "-offset_y", str(region["y"]),
            "-video_size", f"{region['w']}x{region['h']}",
            "-i", "desktop",
        ]

    # ── Audio inputs ────────────────────────────────────────────
    # v3.3.0-beta.6: system audio comes from a WASAPI loopback bridge
    # (core/sys_audio_bridge.py) that pipes raw int16 PCM into FFmpeg's
    # stdin.  No third-party loopback driver (VB-CABLE / Voicemeeter /
    # Stereo Mix) required — WASAPI loopback is built into every
    # Windows 10+ installation.  Mic still uses dshow as before since
    # it's a real input device.
    mic_on  = bool(settings.get("mic_enabled"))
    mic_dev = (settings.get("mic_device") or "").strip()

    next_input_idx = 1
    audio_inputs = []
    if mic_on and mic_dev:
        cmd += ["-f", "dshow",
                "-rtbufsize", "100M",
                "-i", f"audio={mic_dev}"]
        audio_inputs.append(f"{next_input_idx}:a")
        next_input_idx += 1
    if sys_audio_fmt:
        cmd += [
            "-f", "s16le",
            "-ar", str(sys_audio_fmt["rate"]),
            "-ac", str(sys_audio_fmt["channels"]),
            # Modest stdin buffering — FFmpeg keeps ~64 KB queued so we
            # don't underflow during a brief disk hiccup.
            "-thread_queue_size", "1024",
            "-i", "pipe:0",
        ]
        audio_inputs.append(f"{next_input_idx}:a")
        next_input_idx += 1
        log.info(f"clipper: system audio via WASAPI loopback "
                 f"({sys_audio_fmt.get('name', '?')}, "
                 f"{sys_audio_fmt['rate']} Hz x{sys_audio_fmt['channels']} ch)")

    if len(audio_inputs) == 1:
        # Single audio source — still normalize to 48 kHz / stereo so
        # the segment muxer doesn't pick up whatever weird rate the
        # source happened to be at (mic = 44.1 kHz Realtek, system
        # audio = 96 kHz HyperX loopback, etc).
        cmd += [
            "-filter_complex",
            f"[{audio_inputs[0]}]aresample=48000,aformat=channel_layouts=stereo[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]
    elif len(audio_inputs) >= 2:
        # v3.3.0-beta.11: when mixing two streams at DIFFERENT sample
        # rates (e.g. 44.1 kHz mic + 96 kHz loopback), amix's default
        # behavior is to match the LOWEST rate, downsampling everything
        # — verified the user's saved clip came out at 44.1 kHz audio
        # even though the loopback source was 96 kHz.  The resample
        # work also competes with video muxing on the FFmpeg main
        # thread and was correlated with the dropped video frames.
        #
        # Fix: explicitly aresample BOTH inputs to a common 48 kHz
        # stereo before amix, so amix's job is trivial pure mixing.
        # Result: predictable output rate + lower CPU during encode.
        parts = []
        labels = []
        for i, ainp in enumerate(audio_inputs):
            label = f"a{i}"
            parts.append(f"[{ainp}]aresample=48000,aformat=channel_layouts=stereo[{label}]")
            labels.append(f"[{label}]")
        filter_complex = (
            ";".join(parts) + ";"
            + "".join(labels)
            + f"amix=inputs={len(audio_inputs)}:dropout_transition=0:normalize=0[aout]"
        )
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]

    # ── Video encoder ───────────────────────────────────────────
    cmd += _encoder_args(encoder, bitrate)
    # v3.3.0-beta.11: -fps_mode cfr forces constant framerate on the
    # output muxer side (belt-and-suspenders with the fps filter on
    # ddagrab).  Without this, even if our source is CFR, an audio
    # backpressure event could cause the muxer to skip a video packet.
    cmd += ["-pix_fmt", "yuv420p",
            "-g", str(fps * 2),     # keyframe every 2 s → clean splits
            "-fps_mode", "cfr",
            ]

    # ── Output: rolling segments ────────────────────────────────
    # v3.3.0-beta.13: switched from MP4 segments to MPEG-TS.  MP4
    # segments each have their own moov atom + sample table that the
    # concat demuxer has to read and re-base — any tiny rounding in
    # the per-segment duration accumulates into visible micro-freezes
    # at segment boundaries (~15 of them in a 30 s clip).  TS has no
    # moov atom: each packet carries its own PCR timestamp, all
    # segments share a single logical timeline, and concat is just
    # appending packet streams.  Far less timestamp jitter at the
    # cost of a tiny per-packet overhead (~5 %) and slightly larger
    # ring buffer files on disk.
    cmd += [
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_wrap", str(n_segments),
        "-reset_timestamps", "0",      # TS uses one continuous timeline
        "-segment_format", "mpegts",
        "-mpegts_flags", "+initial_discontinuity",
        "-y",
        os.path.join(seg_dir, "buf_%03d.ts"),
    ]
    return cmd, n_segments


def start_buffer() -> dict:
    global _proc, _watcher_thread, _target_follow_thread, _stop_event
    global _sys_audio_bridge

    # ── v3.3.0-beta.18: OBS backend path ─────────────────────────
    # When the user has the OBS toggle on AND OBS is connected, the
    # recording pipeline runs INSIDE OBS — we configure the scene +
    # replay buffer over the WebSocket instead of spawning our own
    # FFmpeg.  GhostShell's _state["running"] still flips True so
    # the rest of the UI / hotkeys behave the same.
    try:
        from core import obs_backend as _obs
        if _obs.is_ready_for_recording():
            settings = _load_settings()
            target = settings.get("capture_target", "monitor:0")
            mon_idx = 0
            if str(target).startswith("monitor:"):
                try: mon_idx = int(target.split(":", 1)[1])
                except Exception: mon_idx = 0
            r1 = _obs.setup_scene(
                monitor_idx  = mon_idx,
                system_audio = bool(settings.get("system_audio_enabled")),
                mic_enabled  = bool(settings.get("mic_enabled")),
            )
            if not r1.get("ok"):
                log.warning(f"OBS setup_scene failed: {r1.get('err')}; "
                            f"falling through to FFmpeg path")
            else:
                r2 = _obs.ensure_replay_buffer(
                    buffer_seconds = settings.get("max_buffer_seconds", 60),
                )
                if r2.get("ok") and r2.get("running"):
                    with _state_lock:
                        _state.update({
                            "running":        True,
                            "ffmpeg_pid":     0,
                            "started_at":     time.time(),
                            "ffmpeg_path":    "(obs)",
                            "encoder_used":   "obs",
                            "target_used":    target,
                            "target_rect":    None,
                            "target_title":   "",
                            "segment_dir":    "",
                            "sys_audio_active":  bool(settings.get("system_audio_enabled")),
                            "sys_audio_device":  "OBS WASAPI loopback",
                            "sys_audio_format":  "via OBS",
                            "capture_method": "obs",
                            "last_err":       "",
                        })
                    log.info(f"clipper: OBS backend handling capture "
                             f"(monitor={mon_idx}, buffer={settings.get('max_buffer_seconds')}s)")
                    return {"ok": True, "backend": "obs",
                            "buffer_seconds": r2.get("buffer_seconds")}
                else:
                    log.warning(f"OBS ensure_replay_buffer failed: {r2.get('err')}; "
                                f"falling through to FFmpeg path")
    except Exception as e:
        log.debug(f"OBS backend routing on start_buffer: {e}; using FFmpeg")
    # ── End OBS path — FFmpeg fallback below ────────────────────

    with _state_lock:
        if _state["running"]:
            return {"ok": True, "already_running": True}
        _state["last_err"]    = ""
        _state["stderr_tail"] = []
        _state["sys_audio_active"] = False
        _state["sys_audio_device"] = ""
        _state["sys_audio_format"] = ""
        _state["sys_audio_err"]    = ""

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        msg = "FFmpeg not installed — open the Dependencies panel to auto-install."
        with _state_lock: _state["last_err"] = msg
        return {"ok": False, "err": msg}

    settings = _load_settings()
    encoder  = _pick_encoder(ffmpeg, settings.get("encoder", "auto"))
    target_cfg = settings.get("capture_target", TARGET_AUTO_FOLLOW)
    region = _resolve_capture_region(target_cfg)

    seg_dir = os.path.join(BUFFER_DIR, f"session_{int(time.time())}")
    os.makedirs(seg_dir, exist_ok=True)

    # ── System audio: probe the WASAPI default-output loopback so
    # FFmpeg's argv has the right -ar / -ac.  Actual capture starts
    # after Popen so we can hand the bridge ffmpeg's stdin.
    sys_audio_fmt = None
    sys_audio_err = ""
    if settings.get("system_audio_enabled"):
        try:
            from core import sys_audio_bridge as _sab
            probe = _sab.probe_default_loopback()
            if probe["ok"]:
                sys_audio_fmt = {
                    "rate":     probe["rate"],
                    "channels": probe["channels"],
                    "name":     probe["name"],
                }
            else:
                sys_audio_err = probe.get("err", "unknown probe failure")
                log.warning(f"clipper: WASAPI loopback probe failed: {sys_audio_err}")
        except Exception as e:
            sys_audio_err = f"WASAPI bridge unavailable: {e}"
            log.warning(f"clipper: {sys_audio_err}")

    cmd, n_segs = _build_ffmpeg_cmd(ffmpeg, settings, region, encoder,
                                      seg_dir, sys_audio_fmt=sys_audio_fmt)
    # Sniff which capture engine actually got picked so we can show it
    # in the UI status.  The argv is fully formed at this point.
    capture_method = "ddagrab" if any("ddagrab=" in a for a in cmd) else "gdigrab"
    log.info(f"clipper start: target={target_cfg} ({region['source']}) "
             f"capture={capture_method} encoder={encoder} fps={settings['fps']} "
             f"buffer={settings['max_buffer_seconds']}s segments={n_segs} "
             f"sys_audio={'on' if sys_audio_fmt else 'off'}")
    log.debug(f"clipper cmd: {' '.join(cmd)}")

    # v3.3.0-beta.9: bump priority BEFORE spawning ffmpeg so the
    # subprocess inherits HIGH from us (we then also pass HIGH
    # explicitly via creationflags as belt-and-suspenders).  Also
    # ensures the WASAPI bridge thread we'll spawn shortly runs at
    # the new priority class.
    global _prev_priority_class
    _prev_priority_class = _boost_self_priority(_HIGH_PRIORITY_CLASS)

    try:
        _stop_event = threading.Event()
        _proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # CREATE_NO_WINDOW | HIGH_PRIORITY_CLASS — keeps ffmpeg
            # ahead of normal background work even when GhostShell's
            # own priority gets bounced (some AV / cleanup tools
            # reset child process priorities).
            creationflags=_CREATE_NO_WINDOW | _HIGH_PRIORITY_CLASS,
        )
    except Exception as e:
        log.error(f"clipper spawn failed: {e}")
        # Roll back priority on spawn failure so a stuck HIGH state
        # doesn't outlive the start attempt.
        _restore_self_priority(_prev_priority_class)
        _prev_priority_class = None
        return {"ok": False, "err": str(e)}

    # ── Start the WASAPI loopback bridge AFTER FFmpeg is up, so we
    # have stdin to write into.  If bridge start fails (e.g. user
    # unplugged headphones in the millisecond between probe and open)
    # we just continue without system audio.
    bridge_status = {"ok": False, "name": "", "channels": 0, "rate": 0, "err": ""}
    if sys_audio_fmt:
        try:
            from core import sys_audio_bridge as _sab
            _sys_audio_bridge = _sab.LoopbackBridge()
            bridge_status = _sys_audio_bridge.start(_proc.stdin)
            if not bridge_status["ok"]:
                log.warning(f"clipper: WASAPI bridge start failed: "
                            f"{bridge_status.get('err', '')}")
                _sys_audio_bridge = None
        except Exception as e:
            log.warning(f"clipper: WASAPI bridge spawn crashed: {e}")
            _sys_audio_bridge = None
            bridge_status = {"ok": False, "name": "", "channels": 0,
                              "rate": 0, "err": str(e)}

    with _state_lock:
        _state.update({
            "running":      True,
            "ffmpeg_pid":   _proc.pid,
            "started_at":   time.time(),
            "ffmpeg_path":  ffmpeg,
            "encoder_used": encoder,
            "target_used":  target_cfg,
            "target_rect":  (region["x"], region["y"], region["w"], region["h"]) \
                              if region["mode"] == "monitor" else None,
            "target_title": region.get("title", ""),
            "segment_dir":  seg_dir,
            "sys_audio_active":  bool(bridge_status["ok"]),
            "sys_audio_device":  bridge_status.get("name", ""),
            "sys_audio_format":  (f"{bridge_status['rate']} Hz × "
                                    f"{bridge_status['channels']} ch")
                                  if bridge_status["ok"] else "",
            "sys_audio_err":     bridge_status.get("err") or sys_audio_err,
            "capture_method":    capture_method,
        })

    _watcher_thread = threading.Thread(
        target=_watcher_loop, args=(_proc, _stop_event),
        daemon=True, name="clipper-watcher",
    )
    _watcher_thread.start()

    # v3.3.0-beta.10: The foreground-tracking watcher used to restart
    # FFmpeg every time the foreground window's monitor changed in
    # auto-follow mode, OR when the foreground game's title changed in
    # game-window mode.  In practice this fired every 8-15 seconds when
    # the user alt-tabbed between monitors / games (verified from user
    # logs).  Each restart creates a fresh seg_dir and wipes the rolling
    # buffer, so a user with `buffer=600s` configured got effectively
    # ~10-20 seconds of actual usable buffer — and save_clip would
    # concat half-finalized fragments from the new session, producing
    # the "1 frame per when it feels like it" symptom.
    #
    # New behavior: auto-follow / game-window resolve ONCE at buffer
    # start and stay locked there until the user explicitly toggles the
    # buffer off + on.  If you want to switch the monitor or game you
    # are recording, flip the buffer toggle.  The rolling buffer
    # actually accumulates for the configured duration now.
    _target_follow_thread = None

    return {"ok": True, "encoder": encoder, "seg_dir": seg_dir,
            "target": region["source"], "n_segments": n_segs,
            "sys_audio": bridge_status}


def stop_buffer() -> dict:
    global _proc, _watcher_thread, _target_follow_thread, _stop_event
    global _sys_audio_bridge, _prev_priority_class
    with _state_lock:
        if not _state["running"]:
            return {"ok": True, "was_running": False}
        proc = _proc
        used_obs = (_state.get("capture_method") == "obs")
        _state["running"] = False
        _state["sys_audio_active"] = False

    # v3.3.0-beta.18: OBS-backed buffer stops its replay buffer in OBS
    # instead of killing an FFmpeg subprocess.  proc is None when the
    # OBS path was taken at start time, so the rest of this function
    # naturally no-ops.
    if used_obs:
        try:
            from core import obs_backend as _obs
            _obs.stop_replay_buffer_safe()
        except Exception as e:
            log.debug(f"OBS stop_replay_buffer: {e}")
        log.info("clipper stopped (OBS backend)")
        return {"ok": True, "was_running": True, "backend": "obs"}

    if _stop_event:
        try: _stop_event.set()
        except Exception: pass
    # Stop the WASAPI loopback bridge FIRST so it stops writing into
    # FFmpeg's stdin and we don't trigger spurious BrokenPipeErrors
    # while ffmpeg is exiting.
    had_bridge = _sys_audio_bridge is not None
    if _sys_audio_bridge is not None:
        try: _sys_audio_bridge.stop()
        except Exception: pass
        _sys_audio_bridge = None
    if proc:
        # If the bridge was feeding stdin as PCM data, sending 'q\n'
        # would just be noise samples to ffmpeg — not a quit signal.
        # Close stdin (signals EOF to the s16le input, which causes
        # ffmpeg to flush + exit cleanly) and terminate as backup.
        # Without the bridge, stdin is open for the legacy 'q' quit
        # signal — use that.
        if had_bridge:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception: pass
            try: proc.terminate()
            except Exception: pass
        else:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
            except Exception:
                try: proc.terminate()
                except Exception: pass
        try: proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            try: proc.terminate()
            except Exception: pass
            try: proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
    _proc = None
    # Restore GhostShell's normal priority class now that the realtime
    # capture pipeline is done.  Leaving it at HIGH would slow down
    # everything else in Windows (Spotify, browsers, etc).
    _restore_self_priority(_prev_priority_class)
    _prev_priority_class = None
    log.info("clipper stopped")
    return {"ok": True, "was_running": True}


def _watcher_loop(proc: subprocess.Popen, stop_event: threading.Event):
    """Pump FFmpeg stderr into our tail buffer; flag unexpected exits.

    v3.3.0-beta.14: distinguish intentional stops (stop_event was set
    by stop_buffer) from real crashes.  Previously, every non-zero
    exit was reported as an error in the UI, including the rc=1 that
    proc.terminate() produces when WE stopped ffmpeg ourselves
    (necessary path when the WASAPI bridge is feeding PCM into stdin
    and we can't send the legacy `q` quit signal).  That produced a
    misleading red 'FFmpeg exited rc=1. Tail: Guessed Channel
    Layout: stereo' message on every Stop click."""
    tail: list = []
    try:
        while not stop_event.is_set() and proc.poll() is None:
            line = proc.stderr.readline() if proc.stderr else b""
            if not line:
                time.sleep(0.2); continue
            try: text = line.decode("utf-8", errors="replace").strip()
            except Exception: text = ""
            if text:
                tail.append(text); tail = tail[-30:]
                with _state_lock: _state["stderr_tail"] = list(tail)
        rc = proc.poll()
        # stop_event being set means stop_buffer initiated the exit.
        # Everything else (game crashed → window vanished → ddagrab
        # failed → ffmpeg died, dependency missing, audio device
        # unplugged, etc.) is a real error worth surfacing.
        intentional = stop_event.is_set()
        if rc is not None and rc != 0 and not intentional:
            with _state_lock:
                _state["last_err"] = (
                    f"FFmpeg exited rc={rc}.  Tail: "
                    + (" | ".join(tail[-3:]) or "(no stderr)")
                )
                _state["running"] = False
            log.warning(f"clipper FFmpeg exited rc={rc}")
        elif rc is not None and rc != 0:
            # Intentional stop — log at debug level only, don't surface
            # to UI.  Also clear last_err in case it was set by a
            # previous run that we then deliberately restarted.
            log.debug(f"clipper ffmpeg exited (intentional stop) rc={rc}")
            with _state_lock:
                _state["last_err"] = ""
    except Exception as e:
        log.debug(f"watcher loop ended: {e}")


# ─── Save clip ──────────────────────────────────────────────────────

def save_clip(seconds: Optional[int] = None) -> dict:
    """Concat the last N seconds of buffer into a single mp4.

    v3.3.0-beta.18: dispatches to the OBS backend's replay-buffer save
    when the user has enabled the OBS toggle AND OBS is connected AND
    its replay buffer is running.  Falls back to the FFmpeg segment-
    concat path otherwise.  The OBS path is much more reliable for
    high-framerate / fullscreen-exclusive games (Windows Graphics
    Capture vs FFmpeg's DXGI wrapper).

    **This does NOT start a recording.**  The rolling buffer is
    already capturing — save_clip just grabs the most recent segments
    that cover the requested duration."""
    # ── OBS routing ──────────────────────────────────────────────
    try:
        from core import obs_backend as _obs
        if _obs.is_ready_for_recording():
            from datetime import datetime
            s_clipper = _load_settings()
            out_dir = s_clipper.get("output_dir") or CLIPS_DIR
            try:
                fg = _foreground_window_info()
                base = (fg.get("exe") or "clip").replace(".exe", "")
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
            except Exception:
                fg, safe = {}, "clip"
            stem = f"{safe}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
            r = _obs.save_replay(target_dir=out_dir, prefix=stem)
            if r.get("ok"):
                # Match the FFmpeg-path return shape so the UI / hotkey
                # callback doesn't have to special-case backends.
                try:    size = os.path.getsize(r["path"])
                except Exception: size = 0
                log.info(f"OBS save_clip → {r['path']} ({size//(1024*1024)} MB)")
                return {
                    "ok":               True,
                    "path":             r["path"],
                    "duration_s":       int(seconds or s_clipper.get("buffer_seconds", 30)),
                    "source_segments":  0,
                    "size":             size,
                    "game":             fg.get("exe", ""),
                    "backend":          "obs",
                }
            else:
                log.warning(f"OBS save_clip failed ({r.get('err')}); "
                            f"falling back to FFmpeg segment path")
                # Fall through to FFmpeg path below
    except Exception as e:
        log.debug(f"OBS routing check raised: {e}; using FFmpeg path")
    # ── End OBS routing — original FFmpeg path follows ──────────
    s = _load_settings()
    target_dur = int(seconds if seconds else s.get("buffer_seconds", 30))
    target_dur = _clamp_int(target_dur, 5, s["max_buffer_seconds"])

    with _state_lock:
        if not _state["running"]:
            return {"ok": False,
                    "err": "Rolling buffer isn't running — turn it on first "
                           "in the Capture tab.  save_clip never starts a "
                           "new recording, it only saves what's already buffered."}
        seg_dir = _state["segment_dir"]
        ffmpeg  = _state["ffmpeg_path"]

    if not seg_dir or not os.path.isdir(seg_dir):
        return {"ok": False, "err": "buffer directory missing"}

    try:
        segs = []
        for fn in os.listdir(seg_dir):
            if fn.endswith(".ts") and fn.startswith("buf_"):
                full = os.path.join(seg_dir, fn)
                try: st = os.stat(full)
                except OSError: continue
                segs.append((st.st_mtime, st.st_size, full))
        segs.sort(reverse=True)
    except Exception as e:
        return {"ok": False, "err": f"can't list segments: {e}"}

    if not segs:
        return {"ok": False, "err": "buffer hasn't captured any segments yet — wait a few seconds"}

    # v3.3.0-beta.13: TS doesn't have the moov-atom problem the MP4
    # segments did — each TS chunk is independently parseable even
    # when partially written.  But we STILL skip the newest segment
    # because the file the muxer is currently writing may have a
    # half-written packet at the tail end, which can cause the concat
    # to insert a tiny corrupt section at exactly the most recent
    # position in the clip (visible as a half-second of garbage right
    # before the cut).  Skipping it costs ~2 s of trailing footage —
    # acceptable, and matches how OBS' replay buffer works.
    #
    # Tiny-file filter (< 32 KB) catches startup stubs from very
    # recent FFmpeg restarts.
    if segs:
        log.debug(f"save_clip: skipping in-progress segment {os.path.basename(segs[0][2])} "
                  f"(mtime={segs[0][0]:.1f}, size={segs[0][1]} bytes)")
        segs = segs[1:]
    segs = [s for s in segs if s[1] >= 32 * 1024]

    if not segs:
        return {"ok": False, "err": "no finalized segments yet — "
                                     "the buffer needs at least a few seconds "
                                     "after start.  Try again in 3-5 seconds."}

    seg_count = max(1, (target_dur // SEGMENT_SECONDS) + 1)
    # Drop the size field from tuples for compat with the rest of the function
    segs = [(mtime, path) for (mtime, _size, path) in segs]
    picked = segs[:seg_count]
    picked.reverse()

    # Filename
    fg = _foreground_window_info()
    prefix = (fg.get("exe") or "clip").replace(".exe", "")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in prefix)
    out_dir = s.get("output_dir") or CLIPS_DIR
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir,
        f"{safe}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mp4")

    concat_list = os.path.join(seg_dir, "concat_list.txt")
    try:
        with open(concat_list, "w", encoding="utf-8") as f:
            for _, path in picked:
                f.write(f"file '{path.replace(chr(92), '/')}'\n")
    except Exception as e:
        return {"ok": False, "err": f"can't write concat list: {e}"}

    try:
        # v3.3.0-beta.13: concat TS segments into final MP4.
        # `-c copy`: pure remux, video stays H.264 / audio stays AAC,
        #   no re-encode.  Saving is fast (seconds, not minutes).
        # `-bsf:a aac_adtstoasc`: AAC in TS uses ADTS framing, AAC in
        #   MP4 uses MPEG-4 ASC.  Without this filter the audio track
        #   in the output mp4 is unplayable.
        # `-movflags +faststart`: places the moov atom at the start of
        #   the file so the clip starts playing immediately in
        #   browsers / preview pickers instead of waiting for the
        #   whole file to download.
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "warning",
             "-y", "-f", "concat", "-safe", "0",
             "-i", concat_list,
             "-c", "copy",
             "-bsf:a", "aac_adtstoasc",
             "-movflags", "+faststart",
             out_path],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000,
        )
        if r.returncode != 0:
            return {"ok": False, "err": f"concat failed (rc={r.returncode}): "
                                          f"{(r.stderr or '')[:200]}"}
    except Exception as e:
        return {"ok": False, "err": f"concat exception: {e}"}
    finally:
        try: os.remove(concat_list)
        except Exception: pass

    try: size = os.path.getsize(out_path)
    except Exception: size = 0
    log.info(f"clip saved: {out_path} ({size // (1024*1024)} MB, "
             f"{len(picked)} segments, {target_dur}s)")
    return {
        "ok":              True,
        "path":            out_path,
        "duration_s":      target_dur,
        "source_segments": len(picked),
        "size":            size,
        "game":            fg.get("exe", ""),
    }


# ─── List + delete ──────────────────────────────────────────────────

def list_recent(limit: int = 30) -> list:
    out_dir = _load_settings().get("output_dir") or CLIPS_DIR
    if not os.path.isdir(out_dir): return []
    items = []
    try:
        for fn in os.listdir(out_dir):
            full = os.path.join(out_dir, fn)
            if not os.path.isfile(full): continue
            if not fn.lower().endswith((".mp4", ".mkv", ".mov")): continue
            try: st = os.stat(full)
            except OSError: continue
            items.append({"path": full, "name": fn,
                           "size": st.st_size, "mtime": st.st_mtime})
    except Exception: return []
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def delete_clip(path: str) -> dict:
    if not path: return {"ok": False, "err": "no path"}
    out_dir = _load_settings().get("output_dir") or CLIPS_DIR
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(out_dir)
    if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
        return {"ok": False, "err": "path outside clips dir"}
    try:
        os.remove(abs_path)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ─── Status ─────────────────────────────────────────────────────────

def _buffered_seconds() -> float:
    """How much wall-clock time of capture is currently in the ring
    buffer.  Used by the UI to show 'buffered: 45 / 60 s' confidence
    that the buffer is actually rolling."""
    with _state_lock:
        seg_dir = _state["segment_dir"]
        running = _state["running"]
    if not running or not seg_dir or not os.path.isdir(seg_dir):
        return 0.0
    try:
        segs = [os.path.join(seg_dir, f) for f in os.listdir(seg_dir)
                  if f.startswith("buf_") and f.endswith(".ts")]
        if not segs: return 0.0
        mtimes = sorted(os.stat(s).st_mtime for s in segs)
        # Time between oldest and newest segment + the partially-filled
        # current segment.  Roughly: (now - oldest_mtime) clamped to ring size.
        span = time.time() - mtimes[0]
        return round(min(span, _load_settings()["max_buffer_seconds"]), 1)
    except Exception:
        return 0.0


def get_status() -> dict:
    s = _load_settings()
    with _state_lock:
        st = dict(_state)
    ffmpeg_ok = bool(_find_ffmpeg())

    # ── System audio probe (v3.3.0-beta.6) ──────────────────────
    # WASAPI loopback: probe the user's current default playback
    # endpoint so the UI can say "Recording from <speaker name>".
    # If the running buffer was started against a DIFFERENT endpoint
    # (because user changed default output mid-recording), we surface
    # both: `active.device_name` is what's actually being captured
    # now, `probe.name` is what would be captured if the user
    # restarted the buffer.
    sys_audio = {
        "available":         False,
        "probe_ok":          False,
        "probe_name":        "",
        "probe_channels":    0,
        "probe_rate":        0,
        "probe_err":         "",
        "active":            bool(st.get("sys_audio_active")),
        "active_device":     st.get("sys_audio_device", ""),
        "active_format":     st.get("sys_audio_format", ""),
        "active_err":        st.get("sys_audio_err", ""),
        "import_err":        "",
    }
    try:
        from core import sys_audio_bridge as _sab
        sys_audio["import_err"] = _sab.import_error()
        sys_audio["available"]  = not bool(sys_audio["import_err"])
        if sys_audio["available"]:
            probe = _sab.probe_default_loopback()
            sys_audio["probe_ok"]       = probe["ok"]
            sys_audio["probe_name"]     = probe.get("name", "")
            sys_audio["probe_channels"] = probe.get("channels", 0)
            sys_audio["probe_rate"]     = probe.get("rate", 0)
            sys_audio["probe_err"]      = probe.get("err", "")
    except Exception as e:
        sys_audio["import_err"] = f"{type(e).__name__}: {e}"
    # Static capability flag — surfaced for the UI so a "Capture engine:
    # ddagrab (GPU)" hint can show up even before the buffer starts.
    ddagrab_avail = bool(_ddagrab_avail_cache) if ffmpeg_ok else False
    return {
        "ffmpeg_installed": ffmpeg_ok,
        "settings":         s,
        "running":          st["running"],
        "ffmpeg_pid":       st["ffmpeg_pid"],
        "encoder_used":     st["encoder_used"],
        "capture_method":   st.get("capture_method", ""),
        "ddagrab_avail":    ddagrab_avail,
        "target_used":      st["target_used"],
        "target_rect":      st["target_rect"],
        "target_title":     st["target_title"],
        "started_at":       st["started_at"],
        "segment_dir":      st["segment_dir"],
        "last_err":         st["last_err"],
        "stderr_tail":      st["stderr_tail"][-5:],
        "uptime_s":         (time.time() - st["started_at"])
                              if st["running"] and st["started_at"] else 0,
        "buffered_seconds": _buffered_seconds(),
        "monitors":         list_monitors(),
        "system_audio":     sys_audio,
        "clips_dir":        CLIPS_DIR,
    }
