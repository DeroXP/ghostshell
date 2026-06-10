"""System-audio capture bridge — Windows WASAPI loopback → FFmpeg stdin.

Background
──────────
On Windows the only way to capture "whatever's playing through the
user's speakers right now" via FFmpeg's built-in dshow input is to
install a third-party loopback driver (VB-CABLE, Voicemeeter,
virtual-audio-capturer, or enable Stereo Mix on hardware that exposes
it).  This is a terrible first-run experience — users install
GhostShell, flip on system audio capture, hit Save, and the saved
clip has no audio with a vague "install VB-CABLE" message.

The proper solution is **WASAPI loopback** — a native Windows audio
API that lets us open ANY render endpoint (speakers, headphones, USB
DAC, HDMI audio) in capture mode and receive the mixed stream the OS
is sending to that device.  No drivers needed.  Works on every
Windows 10+ machine out of the box.

FFmpeg doesn't directly support WASAPI loopback as an input.  But we
can bridge it: a small Python thread opens WASAPI loopback via
PyAudioWPatch (a maintained PortAudio fork with WASAPI loopback
support), reads raw int16 PCM samples, and writes them to FFmpeg's
stdin.  FFmpeg consumes the stdin pipe via `-f s16le -i pipe:0`.

Architecture
────────────
  ┌─ Default Win playback endpoint ─┐
  │ (Speakers / Headphones / HDMI)  │
  └────────────┬────────────────────┘
               │ WASAPI loopback (shared mode)
  ┌────────────▼────────────────────┐
  │ LoopbackBridge thread           │
  │  · PortAudio / PyAudioWPatch    │
  │  · reads int16 frames @ 512     │
  │  · writes raw bytes to stdin    │
  └────────────┬────────────────────┘
               │ os pipe
  ┌────────────▼────────────────────┐
  │ FFmpeg (-f s16le -i pipe:0)     │
  │  amix with mic / encode / seg   │
  └─────────────────────────────────┘

Notes
─────
• Format is always paInt16 (16-bit signed little-endian PCM) — what
  FFmpeg's `s16le` reader expects, and what every loopback endpoint
  can deliver after the WASAPI mixer.
• Sample rate + channel count come from the endpoint's
  defaultSampleRate (usually 48000 or 96000) and maxInputChannels
  (usually 2).  We pass both to FFmpeg via `-ar` and `-ac` so it
  doesn't have to guess.
• If the user changes their default playback device mid-recording,
  the existing bridge keeps capturing from the original device until
  the buffer is restarted.  We surface the picked device in get_status
  so the UI can show "Recording from Headphones".
• Bridge is robust against FFmpeg dying — write() to a broken pipe
  triggers BrokenPipeError, we log it once and exit cleanly.
"""
from __future__ import annotations

import threading
import time
from typing import Optional, BinaryIO

from core.utils import get_logger

log = get_logger(__name__)


# Lazy import of pyaudiowpatch — only loaded when the user actually
# enables system audio capture, so the dep cost is paid on demand.
_pa_module = None
_pa_import_err: str = ""


def _ensure_pyaudio():
    """Import pyaudiowpatch on first use.  Returns the module or None
    plus an error string if the import failed (typically: bundled
    runtime missing the .pyd, or user is on an unsupported Python)."""
    global _pa_module, _pa_import_err
    if _pa_module is not None:
        return _pa_module
    if _pa_import_err:
        return None
    try:
        import pyaudiowpatch as pa   # noqa
        _pa_module = pa
        return pa
    except Exception as e:
        _pa_import_err = f"{type(e).__name__}: {e}"
        log.warning(f"PyAudioWPatch import failed: {_pa_import_err}")
        return None


def import_error() -> str:
    """Public hook for the UI/status — empty if the runtime is healthy."""
    _ensure_pyaudio()
    return _pa_import_err


def probe_default_loopback() -> dict:
    """Find the loopback device for the current Windows default output
    endpoint.  Returns {ok, name, channels, rate, index, err}.

    Used by get_status() to surface "Recording from <speaker name>" in
    the UI without actually opening a capture stream.  Cheap call —
    just enumerates devices.
    """
    pa = _ensure_pyaudio()
    if pa is None:
        return {"ok": False, "name": "", "channels": 0, "rate": 0,
                "index": -1, "err": _pa_import_err or "pyaudiowpatch not available"}
    p = None
    try:
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        except OSError:
            return {"ok": False, "name": "", "channels": 0, "rate": 0,
                    "index": -1, "err": "WASAPI host API not available on this Windows build"}
        default_out_idx = wasapi.get("defaultOutputDevice", -1)
        if default_out_idx is None or default_out_idx < 0:
            return {"ok": False, "name": "", "channels": 0, "rate": 0,
                    "index": -1, "err": "No default playback device set in Windows"}
        dev = p.get_device_info_by_index(default_out_idx)
        # If this device IS already a loopback endpoint we're done;
        # otherwise find its loopback analogue (which PortAudio appends
        # at the end of the device list with "[Loopback]" suffix).
        if not dev.get("isLoopbackDevice"):
            picked = None
            try:
                for lb in p.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        picked = lb
                        break
            except Exception as e:
                return {"ok": False, "name": dev["name"], "channels": 0,
                        "rate": 0, "index": -1,
                        "err": f"loopback enumeration failed: {e}"}
            if not picked:
                return {"ok": False, "name": dev["name"], "channels": 0,
                        "rate": 0, "index": -1,
                        "err": f"no loopback analogue for {dev['name']!r} "
                               f"— device may not support shared-mode loopback"}
            dev = picked
        return {
            "ok":       True,
            "name":     dev["name"],
            "channels": int(dev.get("maxInputChannels") or 2),
            "rate":     int(dev.get("defaultSampleRate") or 48000),
            "index":    int(dev["index"]),
            "err":      "",
        }
    except Exception as e:
        return {"ok": False, "name": "", "channels": 0, "rate": 0,
                "index": -1, "err": f"{type(e).__name__}: {e}"}
    finally:
        if p is not None:
            try: p.terminate()
            except Exception: pass


class LoopbackBridge:
    """Background thread that pumps WASAPI loopback samples into an
    arbitrary writable stream (FFmpeg's stdin in our case).

    Usage:
        b = LoopbackBridge()
        info = b.start(target_stdin=proc.stdin)
        # ... clipper records ...
        b.stop()
    """

    # Frames per read — at 48 kHz this is ~10.7 ms per chunk, plenty
    # fast for the recording use case and small enough that a broken
    # pipe gets caught within a couple of chunks of FFmpeg dying.
    _FRAMES_PER_READ = 512

    def __init__(self):
        self._stop  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_err  = ""
        self.device_name = ""
        self.channels = 0
        self.rate     = 0
        self.bytes_written = 0
        self.started_at = 0.0

    def start(self, target_stdin: BinaryIO) -> dict:
        """Open the default WASAPI loopback and start the pump thread.

        Returns {ok, name, channels, rate, err}.  If ok=False, no
        thread was started and the caller can fall back to no audio.
        """
        if self._thread and self._thread.is_alive():
            return {"ok": True, "name": self.device_name,
                    "channels": self.channels, "rate": self.rate,
                    "err": "already running"}
        info = probe_default_loopback()
        if not info["ok"]:
            self.last_err = info["err"]
            return {"ok": False, "name": info["name"],
                    "channels": 0, "rate": 0, "err": info["err"]}
        self.device_name = info["name"]
        self.channels    = info["channels"]
        self.rate        = info["rate"]
        self._stop.clear()
        self.bytes_written = 0
        self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, args=(target_stdin, info["index"]),
            daemon=True, name="wasapi-loopback-bridge",
        )
        self._thread.start()
        log.info(f"sys-audio bridge: capturing {self.device_name!r} "
                 f"@ {self.rate} Hz x{self.channels} ch")
        return {"ok": True, "name": self.device_name,
                "channels": self.channels, "rate": self.rate, "err": ""}

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.5)
        self._thread = None

    def _run(self, target_stdin: BinaryIO, device_index: int) -> None:
        pa = _ensure_pyaudio()
        if pa is None:
            self.last_err = _pa_import_err or "pyaudiowpatch not available"
            return
        # v3.3.0-beta.9: bump this thread's priority so PortAudio's
        # blocking read can't get scheduled away under load (fullscreen
        # game pinning a CPU, browser tab going wild, etc.).  Any
        # scheduling jitter > 5ms here drops samples → A/V gets out of
        # sync → playback looks/sounds wrong.  THREAD_PRIORITY_HIGHEST
        # (= +2 above normal) is enough — TIME_CRITICAL would let us
        # starve the OS audio service which would be worse.
        try:
            import ctypes as _ct
            _k32 = _ct.windll.kernel32
            _h = _k32.GetCurrentThread()
            if not _k32.SetThreadPriority(_h, 2):    # THREAD_PRIORITY_HIGHEST
                log.debug(f"sys-audio bridge: SetThreadPriority failed "
                          f"(GetLastError={_k32.GetLastError()})")
        except Exception as e:
            log.debug(f"sys-audio bridge: priority bump failed: {e}")
        p = None
        stream = None
        try:
            p = pa.PyAudio()
            stream = p.open(
                format=pa.paInt16,
                channels=self.channels,
                rate=self.rate,
                frames_per_buffer=self._FRAMES_PER_READ,
                input=True,
                input_device_index=device_index,
            )
            while not self._stop.is_set():
                try:
                    data = stream.read(self._FRAMES_PER_READ,
                                        exception_on_overflow=False)
                except Exception as e:
                    # Device removed mid-capture (USB unplug, Bluetooth
                    # disconnect, default device change with the old one
                    # vanishing).  Log + bail.
                    self.last_err = f"WASAPI read failed: {e}"
                    log.warning(f"sys-audio bridge: {self.last_err}")
                    return
                if not data:
                    continue
                try:
                    target_stdin.write(data)
                    self.bytes_written += len(data)
                except (BrokenPipeError, OSError) as e:
                    # FFmpeg exited / closed stdin.  Normal during a
                    # buffer restart; not an error worth shouting about.
                    log.debug(f"sys-audio bridge: stdin closed ({e})")
                    return
        except Exception as e:
            self.last_err = f"{type(e).__name__}: {e}"
            log.warning(f"sys-audio bridge: opener crashed: {self.last_err}")
        finally:
            if stream is not None:
                try: stream.stop_stream()
                except Exception: pass
                try: stream.close()
                except Exception: pass
            if p is not None:
                try: p.terminate()
                except Exception: pass

    def get_status(self) -> dict:
        running = bool(self._thread and self._thread.is_alive())
        uptime = (time.time() - self.started_at) if running and self.started_at else 0
        return {
            "running":       running,
            "device_name":   self.device_name,
            "channels":      self.channels,
            "rate":          self.rate,
            "bytes_written": self.bytes_written,
            "uptime_s":      round(uptime, 1),
            "last_err":      self.last_err,
        }
