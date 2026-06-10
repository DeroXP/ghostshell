"""OBS Studio backend — alternative to the FFmpeg/ddagrab clipper path.

Status (v3.3.0-beta.15)
───────────────────────
This is the FOUNDATION layer.  This release only detects whether OBS
Studio is installed on the user's machine and surfaces that to the
Capture page; the actual WebSocket connection + scene setup + replay
buffer wiring lands in beta.16 / beta.17 / beta.18.

Architecture
────────────
GhostShell does NOT bundle OBS Studio.  OBS is GPLv2 and redistributing
it would require us to comply with the license; instead we detect the
user's existing OBS install (or prompt them to download it from
obsproject.com) and drive it externally via the OBS WebSocket plugin
(built into OBS 28+).

When the user enables the OBS backend toggle:
  1. We connect to OBS' WebSocket server (default port 4455) using the
     obsws-python client.
  2. We programmatically create a scene collection + profile + scene +
     sources matching GhostShell's clipper settings (display capture
     for the chosen monitor, Audio Output Capture for system audio
     via OBS' built-in WGC + WASAPI loopback, Audio Input Capture
     for the mic).
  3. We enable + start the Replay Buffer with the user's configured
     duration.
  4. The save-clip hotkey fires SaveReplayBuffer over the WebSocket.
  5. We move the resulting file from OBS' output directory into our
     own Videos/GhostShell/Clips folder.

OBS handles the actual capture via Windows.Graphics.Capture (WGC),
which is materially better than FFmpeg's DXGI Desktop Duplication for
games — handles fullscreen exclusive, lower drop rate, hardware-
accelerated NVENC path with zero CPU overhead.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from core.utils import get_logger

log = get_logger(__name__)


# obsws_python logs its own tracebacks on connection failure (even
# when the exception is caught by us).  Silence it — our connect()
# already surfaces a friendly error.  Done at module-import time so
# the rate-limited auto-reconnect thread doesn't spam ghostshell.log
# with ConnectionRefusedError stack traces every 30 s.
try:
    import logging as _logging
    _logging.getLogger("obsws_python").setLevel(_logging.CRITICAL)
    _logging.getLogger("websocket").setLevel(_logging.CRITICAL)
except Exception:
    pass


# ─── Install detection ──────────────────────────────────────────────

# Standard install locations.  OBS Studio installer defaults to
# `%ProgramFiles%\obs-studio\bin\64bit\obs64.exe` on 64-bit Windows;
# winget installs land in the same place.  Portable builds extract
# anywhere; we don't try to find those (user can configure via the
# `obs_exe_override` setting).
_OBS_PROBE_PATHS = [
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    # Some users keep OBS on a secondary drive
    r"D:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"E:\Program Files\obs-studio\bin\64bit\obs64.exe",
]

# Minimum OBS version with the modern (v5) WebSocket built-in.  Older
# versions need the obs-websocket plugin installed separately and use
# v4 protocol which obsws-python won't speak.
_OBS_MIN_VERSION = (28, 0, 0)


def _parse_version(s: str) -> tuple:
    """Best-effort tuple-ify of '32.0.4' → (32, 0, 4).  Trailing
    non-numeric parts (e.g. '32.0.4-rc1') are dropped."""
    out = []
    for part in (s or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit(): digits += ch
            else: break
        if digits:
            try: out.append(int(digits))
            except Exception: break
        else: break
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _read_exe_version(path: str) -> str:
    """Pull the FileVersion attribute off the obs64.exe so the UI can
    say 'OBS 32.0.4 detected' instead of a bare path.  Uses Win32
    GetFileVersionInfo via ctypes — keeps zero new deps."""
    try:
        import ctypes
        from ctypes import wintypes
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(path, None)
        if size == 0: return ""
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        # Pull the root \\ VS_FIXEDFILEINFO struct
        verPtr = ctypes.c_void_p()
        verLen = ctypes.c_uint()
        if not version.VerQueryValueW(buf, "\\",
                                        ctypes.byref(verPtr),
                                        ctypes.byref(verLen)):
            return ""
        # VS_FIXEDFILEINFO has dwFileVersionMS (high 32) + dwFileVersionLS (low 32)
        # Layout: dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS, ...
        # Each is a DWORD.  We want offset 8 (MS) and 12 (LS).
        if verLen.value < 16: return ""
        data = (ctypes.c_uint32 * 4).from_address(verPtr.value)
        ms = data[2]; ls = data[3]
        major  = (ms >> 16) & 0xFFFF
        minor  = ms & 0xFFFF
        patch  = (ls >> 16) & 0xFFFF
        return f"{major}.{minor}.{patch}"
    except Exception as e:
        log.debug(f"version probe failed for {path}: {e}")
        return ""


# ── Cache: OBS detection isn't free (file existence checks + version
# probe) and the answer rarely changes inside one app session.  60 s
# cache.  Hitting the "Detect OBS" button in the UI calls
# detect_obs(force_refresh=True) to bypass.
_DETECT_CACHE_TTL = 60.0
_detect_ts = 0.0
_detect_data: Optional[dict] = None
_detect_lock = threading.Lock()


def detect_obs(force_refresh: bool = False) -> dict:
    """Find an OBS Studio installation on this machine.

    Returns:
        {
            ok:               bool      true if an OBS exe was found
            path:             str       full path to obs64.exe, or ""
            version:          str       "32.0.4" or "" if unknown
            version_ok:       bool      true if >= 28.0.0 (WebSocket v5)
            websocket_ready:  bool      placeholder; populated by the
                                          next iteration after we
                                          actually try to connect
            err:              str       human-readable hint if not found
        }
    """
    global _detect_ts, _detect_data
    now = time.time()
    if not force_refresh:
        with _detect_lock:
            if _detect_data is not None and (now - _detect_ts) < _DETECT_CACHE_TTL:
                return dict(_detect_data)

    found_path = ""
    # Check the manual override first
    try:
        from config import APPDATA_DIR
        import json as _json
        ov_path = os.path.join(APPDATA_DIR, "obs_settings.json")
        if os.path.exists(ov_path):
            with open(ov_path, "r", encoding="utf-8") as f:
                ov = _json.load(f) or {}
            cand = (ov.get("obs_exe_override") or "").strip()
            if cand and os.path.isfile(cand):
                found_path = cand
    except Exception: pass

    if not found_path:
        for p in _OBS_PROBE_PATHS:
            if os.path.isfile(p):
                found_path = p
                break

    if not found_path:
        result = {
            "ok":              False,
            "path":            "",
            "version":         "",
            "version_ok":      False,
            "websocket_ready": False,
            "err":             "OBS Studio not found in standard install paths. "
                                "Install from obsproject.com (we need v28 or newer for "
                                "the built-in WebSocket plugin).",
        }
    else:
        version = _read_exe_version(found_path)
        vtuple = _parse_version(version) if version else (0, 0, 0)
        version_ok = vtuple >= _OBS_MIN_VERSION
        err = ""
        if not version_ok and version:
            err = (f"OBS {version} is too old — need {_OBS_MIN_VERSION[0]}.0+ "
                    f"for the built-in WebSocket plugin. "
                    f"Update from obsproject.com.")
        result = {
            "ok":              True,
            "path":            found_path,
            "version":         version,
            "version_ok":      version_ok,
            "websocket_ready": False,   # populated in beta.16
            "err":             err,
        }

    with _detect_lock:
        _detect_ts   = time.time()
        _detect_data = dict(result)
    return result


# ─── Settings persistence (separate from clipper_settings.json so
#     OBS toggling doesn't conflict with clipper config) ─────────────

def _settings_path() -> str:
    from config import APPDATA_DIR
    return os.path.join(APPDATA_DIR, "obs_settings.json")


_DEFAULT_SETTINGS = {
    "enabled":          False,    # OBS backend toggle in the UI
    "obs_exe_override": "",       # manual path if auto-detect fails
    "websocket_host":   "localhost",
    "websocket_port":   4455,
    "websocket_password": "",     # OBS sets one by default in newer builds
    "auto_launch_obs":   True,    # spawn obs64.exe on buffer-start if not running
    "scene_collection": "GhostShell",
    "profile":          "GhostShell",
}


def get_settings() -> dict:
    try:
        import json as _json
        p = _settings_path()
        if not os.path.isfile(p):
            return dict(_DEFAULT_SETTINGS)
        with open(p, "r", encoding="utf-8") as f:
            data = _json.load(f) or {}
        return {**_DEFAULT_SETTINGS, **data}
    except Exception as e:
        log.debug(f"obs settings load failed: {e}")
        return dict(_DEFAULT_SETTINGS)


def set_settings(**kwargs) -> dict:
    s = get_settings()
    for k, v in kwargs.items():
        if k in _DEFAULT_SETTINGS:
            s[k] = v
    try:
        import json as _json
        from config import APPDATA_DIR
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            _json.dump(s, f, indent=2)
    except Exception as e:
        log.warning(f"obs settings save failed: {e}")
    return {"ok": True, "settings": s}


# ─── WebSocket connection (v3.3.0-beta.16+) ─────────────────────────
#
# Use obsws-python's ReqClient for one-shot requests (GetVersion,
# SetCurrentProgramScene, SaveReplayBuffer, etc).  EventClient comes
# later when we need to subscribe to ReplayBufferSaved.
#
# The ReqClient blocks for up to `timeout` seconds when establishing
# the connection — usually instant for localhost but can hang if OBS
# is mid-launch.  We wrap every call in try/except so a flaky OBS
# never wedges a Flask handler thread.

_client = None
_client_lock = threading.Lock()
_conn_state = {
    "connected":   False,
    "connect_err": "",
    "obs_version": "",
    "ws_version":  "",
    "platform":    "",
    "platform_desc": "",
    "rpc_version": 0,
    "last_check":  0.0,
}


def _err_msg(e: Exception) -> str:
    """Friendly error string from obsws_python exceptions or stdlib."""
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if "connection refused" in low or "actively refused" in low:
        return ("Connection refused — OBS WebSocket server isn't listening.  "
                "In OBS go to Tools → WebSocket Server Settings and tick "
                "'Enable WebSocket server'.")
    if "timed out" in low or "timeout" in low:
        return ("Connection timed out — OBS is unreachable at the configured "
                "host/port.  Is OBS running?")
    if "authentication" in low or "auth" in low or "password" in low:
        return ("Authentication failed — the configured WebSocket password "
                "doesn't match OBS's.  Open OBS → Tools → WebSocket Server "
                "Settings → 'Show Connect Info' to copy the current password.")
    return s


def connect(force_reconnect: bool = False) -> dict:
    """Try to open a ReqClient to OBS using the persisted settings.

    Returns the populated _conn_state dict.  On failure, `connected`
    is False and `connect_err` carries a human-readable hint.

    Multiple GhostShell threads may call this concurrently (Flask
    request handlers); serialize with _client_lock so we don't end up
    with two half-open clients fighting over the socket.
    """
    global _client
    with _client_lock:
        if _client is not None and not force_reconnect:
            # Already connected — just refresh the cached version info
            try:
                _refresh_version_unlocked()
                return dict(_conn_state)
            except Exception:
                # Cached client is stale; drop it and reconnect below.
                _safe_close_unlocked()

        s = get_settings()
        try:
            import obsws_python as obs
        except Exception as e:
            _conn_state["connected"]   = False
            _conn_state["connect_err"] = f"obsws_python not bundled: {e}"
            log.warning(_conn_state["connect_err"])
            return dict(_conn_state)

        host    = (s.get("websocket_host") or "localhost").strip()
        port    = int(s.get("websocket_port") or 4455)
        passwd  = (s.get("websocket_password") or "").strip()
        timeout = 4.0
        log.info(f"OBS connect attempt: {host}:{port} (auth={'yes' if passwd else 'no'})")

        try:
            _client = obs.ReqClient(host=host, port=port, password=passwd,
                                      timeout=timeout)
            _refresh_version_unlocked()
            _conn_state["connected"]   = True
            _conn_state["connect_err"] = ""
            log.info(f"OBS connected: v{_conn_state['obs_version']} "
                     f"ws v{_conn_state['ws_version']} on {_conn_state['platform']}")
        except Exception as e:
            _client = None
            _conn_state["connected"]   = False
            _conn_state["connect_err"] = _err_msg(e)
            log.warning(f"OBS connect failed: {_conn_state['connect_err']}")

        _conn_state["last_check"] = time.time()
        return dict(_conn_state)


def _refresh_version_unlocked() -> None:
    """Call GetVersion and stash the result into _conn_state.  Called
    holding _client_lock."""
    if _client is None:
        return
    r = _client.get_version()
    # obsws_python returns a namespace object whose attributes are
    # named after the JSON response fields.  The OBS WebSocket v5
    # response has: obsVersion, obsWebSocketVersion, rpcVersion,
    # availableRequests, supportedImageFormats, platform, platformDescription.
    _conn_state["obs_version"]    = getattr(r, "obs_version", "") or getattr(r, "obsVersion", "")
    _conn_state["ws_version"]     = getattr(r, "obs_web_socket_version", "") or getattr(r, "obsWebSocketVersion", "")
    _conn_state["rpc_version"]    = int(getattr(r, "rpc_version", 0) or getattr(r, "rpcVersion", 0))
    _conn_state["platform"]       = getattr(r, "platform", "") or ""
    _conn_state["platform_desc"]  = getattr(r, "platform_description", "") or getattr(r, "platformDescription", "")


def _safe_close_unlocked() -> None:
    """Close the cached client without raising.  Called holding
    _client_lock."""
    global _client
    if _client is None:
        return
    try:
        # obsws-python's ReqClient inherits a `disconnect()` method
        # from the base client.  Older builds may not have it; fall
        # back to deleting the reference and letting GC handle the
        # underlying websocket.
        d = getattr(_client, "disconnect", None)
        if callable(d):
            d()
    except Exception as e:
        log.debug(f"OBS client.disconnect raised: {e}")
    finally:
        _client = None


def disconnect() -> dict:
    """Tear down the active connection.  Idempotent."""
    with _client_lock:
        _safe_close_unlocked()
        _conn_state["connected"]   = False
        _conn_state["connect_err"] = ""
    log.info("OBS disconnected")
    return dict(_conn_state)


def is_connected() -> bool:
    return bool(_conn_state.get("connected"))


def launch_obs() -> dict:
    """Spawn OBS Studio detached, minimized.  Returns immediately —
    the caller should poll detect_obs() / connect() to know when OBS
    is ready to accept WebSocket connections (typically 2-5 seconds
    after launch).
    """
    det = detect_obs()
    if not det["ok"]:
        return {"ok": False, "err": "OBS not installed"}
    exe = det["path"]
    try:
        import subprocess
        # --minimize-to-tray puts OBS in the tray instead of stealing
        # focus; --disable-shutdown-check skips the "do you want to
        # stop streaming" prompt when GhostShell shuts down later;
        # --collection ... + --profile ... select the GhostShell-
        # configured scene/profile (beta.17 will create these).
        s = get_settings()
        argv = [
            exe,
            "--minimize-to-tray",
            "--disable-shutdown-check",
            "--collection", s.get("scene_collection") or "GhostShell",
            "--profile",    s.get("profile") or "GhostShell",
        ]
        # OBS expects to be launched from its install dir (DLLs look
        # for relative paths).  cwd to the bin/64bit folder.
        cwd = os.path.dirname(exe)
        # DETACHED_PROCESS | CREATE_NO_WINDOW so OBS lives independently.
        subprocess.Popen(
            argv, cwd=cwd, close_fds=True,
            creationflags=0x00000008 | 0x08000000,   # DETACHED | NO_WINDOW
        )
        log.info(f"OBS launched: {exe}")
        return {"ok": True, "path": exe}
    except Exception as e:
        log.exception("OBS launch failed")
        return {"ok": False, "err": str(e)}


# ─── Auto-reconnect (v3.3.0-beta.17+) ───────────────────────────────
#
# get_status() is called by the Capture-page poll every ~6 seconds.
# If the user enabled the OBS toggle and we're NOT connected (either
# because GhostShell just booted or OBS closed mid-session), we
# attempt a quiet reconnect — but rate-limited so a closed OBS doesn't
# burn 4 s of timeout on every poll.

_last_auto_reconnect = 0.0
_AUTO_RECONNECT_INTERVAL = 30.0


def _maybe_auto_reconnect() -> None:
    """Quietly try to reconnect if the user wants OBS but we're down.
    Throttled to once per 30 s.  Safe to call from get_status() — does
    nothing if toggle is off, already connected, or rate-limited."""
    global _last_auto_reconnect
    s = get_settings()
    if not s.get("enabled"):
        return
    if _conn_state["connected"]:
        return
    now = time.time()
    if (now - _last_auto_reconnect) < _AUTO_RECONNECT_INTERVAL:
        return
    _last_auto_reconnect = now
    # The actual connect() handles all error paths; we just kick it.
    # Run in a background thread so the status poll doesn't block on
    # a 4-second timeout when OBS is unreachable.
    threading.Thread(
        target=connect, args=(False,),
        daemon=True, name="obs-auto-reconnect",
    ).start()


def boot_autoconnect() -> None:
    """Call on app startup.  Fires a connect attempt immediately if
    the user had the OBS backend enabled, so the UI shows the live
    connection state from the moment the Capture page first opens."""
    s = get_settings()
    if not s.get("enabled"):
        return
    global _last_auto_reconnect
    _last_auto_reconnect = time.time()
    threading.Thread(
        target=connect, args=(False,),
        daemon=True, name="obs-boot-autoconnect",
    ).start()


# ─── Scene + replay buffer (v3.3.0-beta.18+) ────────────────────────
#
# GhostShell owns a single OBS scene + a small set of sources, named
# with a "GhostShell " prefix so they don't collide with whatever the
# user has configured for streaming or their own recording workflow.
# Scene setup is idempotent — re-running it is a no-op if everything's
# already in place.

GS_SCENE_NAME      = "GhostShell"
GS_INPUT_DISPLAY   = "GhostShell Display"
GS_INPUT_SYS_AUDIO = "GhostShell System Audio"
GS_INPUT_MIC       = "GhostShell Mic"


def _scene_state_unlocked():
    """Returns (existing_scene_names, existing_input_names) for the
    GhostShell scene.  Called holding _client_lock."""
    sl = _client.get_scene_list()
    scenes = [s["sceneName"] for s in sl.scenes]
    items = set()
    if GS_SCENE_NAME in scenes:
        try:
            il = _client.get_scene_item_list(GS_SCENE_NAME)
            items = {it["sourceName"] for it in il.scene_items}
        except Exception:
            pass
    return scenes, items


def setup_scene(monitor_idx: int = 0,
                 system_audio: bool = True,
                 mic_enabled: bool = False) -> dict:
    """Create the GhostShell scene + sources if missing.  Idempotent.

    Returns {ok, err, scene, sources_created: [str]}.  `sources_created`
    only lists sources that were newly created on this call so the UI
    can show "scene refreshed" vs "added Display Capture" feedback.
    """
    with _client_lock:
        if _client is None or not _conn_state["connected"]:
            return {"ok": False, "err": "Not connected to OBS",
                    "scene": "", "sources_created": []}
        created = []
        try:
            scenes, existing_inputs = _scene_state_unlocked()
            if GS_SCENE_NAME not in scenes:
                _client.create_scene(GS_SCENE_NAME)
                log.info(f"OBS: created scene '{GS_SCENE_NAME}'")
                created.append("scene")

            # Make it the current program scene so OBS records it.
            _client.set_current_program_scene(GS_SCENE_NAME)

            # Display Capture (monitor_capture).  method=2 = WGC on
            # Win10+ which handles fullscreen-exclusive games far
            # better than method=0 (DXGI Desktop Duplication, the
            # same API GhostShell's FFmpeg path was struggling with).
            if GS_INPUT_DISPLAY not in existing_inputs:
                _client.create_input(
                    GS_SCENE_NAME, GS_INPUT_DISPLAY, "monitor_capture",
                    {
                        "monitor":        int(monitor_idx),
                        "method":         2,      # WGC
                        "capture_cursor": True,
                    },
                    True,
                )
                log.info(f"OBS: created Display Capture (monitor {monitor_idx})")
                created.append("display")
            else:
                # Update existing input's monitor index in case user
                # changed capture target setting.
                try:
                    _client.set_input_settings(
                        GS_INPUT_DISPLAY,
                        {"monitor": int(monitor_idx), "method": 2,
                          "capture_cursor": True},
                        True,
                    )
                except Exception as e:
                    log.debug(f"OBS: failed to update monitor idx: {e}")

            # System audio via WASAPI Output Capture (loopback on the
            # default playback device).  This is OBS's native loopback
            # support, no third-party drivers needed.
            if system_audio and GS_INPUT_SYS_AUDIO not in existing_inputs:
                _client.create_input(
                    GS_SCENE_NAME, GS_INPUT_SYS_AUDIO,
                    "wasapi_output_capture",
                    {"device_id": "default"},
                    True,
                )
                log.info("OBS: created System Audio capture (default device)")
                created.append("system_audio")

            # Mic via WASAPI Input Capture.  "default" matches whatever
            # the user has set as Windows' default recording device.
            if mic_enabled and GS_INPUT_MIC not in existing_inputs:
                _client.create_input(
                    GS_SCENE_NAME, GS_INPUT_MIC,
                    "wasapi_input_capture",
                    {"device_id": "default"},
                    True,
                )
                log.info("OBS: created Mic capture (default device)")
                created.append("mic")

            return {"ok": True, "scene": GS_SCENE_NAME,
                    "sources_created": created, "err": ""}
        except Exception as e:
            log.exception("OBS setup_scene failed")
            return {"ok": False, "err": _err_msg(e),
                    "scene": "", "sources_created": created}


def _set_param_both_modes_unlocked(category_pairs, name, value) -> None:
    """Set the same profile parameter in both SimpleOutput and AdvOut
    categories — we don't know which output mode the user has selected,
    and OBS silently ignores unknown sections."""
    for cat in category_pairs:
        try:
            _client.set_profile_parameter(cat, name, str(value))
        except Exception as e:
            log.debug(f"OBS set_profile_parameter({cat},{name}) failed: {e}")


def ensure_replay_buffer(buffer_seconds: int = 60) -> dict:
    """Configure replay buffer duration + start it if not already
    running.  Idempotent.  Returns {ok, running, buffer_seconds, err}."""
    with _client_lock:
        if _client is None or not _conn_state["connected"]:
            return {"ok": False, "err": "Not connected to OBS",
                    "running": False, "buffer_seconds": 0}
        try:
            buffer_seconds = max(10, min(600, int(buffer_seconds)))
            # Cover both output modes so toggling Simple/Advanced in
            # OBS Settings doesn't break us.
            _set_param_both_modes_unlocked(
                ["SimpleOutput", "AdvOut"], "RecRB", "true",
            )
            _set_param_both_modes_unlocked(
                ["SimpleOutput", "AdvOut"], "RecRBTime", buffer_seconds,
            )
            # Also push a generous size cap so OBS doesn't truncate
            # high-bitrate captures: 0 = uncapped.
            _set_param_both_modes_unlocked(
                ["SimpleOutput", "AdvOut"], "RecRBSize", 0,
            )
            # Check status; start if not running.
            status = _client.get_replay_buffer_status()
            running = bool(getattr(status, "output_active", False))
            if not running:
                _client.start_replay_buffer()
                log.info(f"OBS: started replay buffer ({buffer_seconds}s)")
                running = True
            return {"ok": True, "running": running,
                    "buffer_seconds": buffer_seconds, "err": ""}
        except Exception as e:
            log.exception("OBS ensure_replay_buffer failed")
            return {"ok": False, "running": False,
                    "buffer_seconds": 0, "err": _err_msg(e)}


def stop_replay_buffer_safe() -> dict:
    """Stop the OBS replay buffer if running.  Idempotent + tolerant —
    failures are logged but don't propagate (called during GhostShell
    shutdown / buffer-toggle-off)."""
    with _client_lock:
        if _client is None or not _conn_state["connected"]:
            return {"ok": True, "was_running": False}
        try:
            status = _client.get_replay_buffer_status()
            if getattr(status, "output_active", False):
                _client.stop_replay_buffer()
                log.info("OBS: stopped replay buffer")
                return {"ok": True, "was_running": True}
            return {"ok": True, "was_running": False}
        except Exception as e:
            log.debug(f"OBS stop_replay_buffer_safe error: {e}")
            return {"ok": False, "err": str(e)}


def save_replay(target_dir: Optional[str] = None,
                 prefix: Optional[str] = None,
                 timeout: float = 10.0) -> dict:
    """Trigger SaveReplayBuffer in OBS, wait for the file to land,
    optionally move it into `target_dir` renamed to `prefix`.

    Returns {ok, path, err}.  `path` is the final file location after
    the optional move.
    """
    if not _conn_state["connected"]:
        return {"ok": False, "err": "OBS not connected", "path": ""}
    try:
        # Capture the previous saved-replay path so we can detect when
        # OBS finishes writing the new one (the value updates only
        # when SaveReplayBuffer completes).
        with _client_lock:
            status = _client.get_replay_buffer_status()
            if not getattr(status, "output_active", False):
                return {"ok": False, "path": "",
                        "err": "OBS replay buffer isn't running — "
                                "turn the GhostShell buffer toggle on first."}
            try:
                prev = _client.get_last_replay_buffer_replay()
                prev_path = getattr(prev, "saved_replay_path", "") or ""
            except Exception:
                prev_path = ""
            _client.save_replay_buffer()

        # Poll OBS until the saved_replay_path changes AND the file
        # exists on disk (OBS reports the path slightly before the
        # mp4 moov atom is written; the os.path.isfile + size check
        # protects against trying to play a half-written file).
        deadline = time.time() + max(2.0, float(timeout))
        new_path = ""
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                with _client_lock:
                    r = _client.get_last_replay_buffer_replay()
                p = getattr(r, "saved_replay_path", "") or ""
                if (p and p != prev_path
                        and os.path.isfile(p)
                        and os.path.getsize(p) > 64 * 1024):
                    new_path = p
                    break
            except Exception as e:
                log.debug(f"OBS save poll: {e}")

        if not new_path:
            return {"ok": False, "path": "",
                    "err": f"OBS didn't return a finalized clip within {timeout:.0f}s.  "
                            f"Check OBS' recording log."}

        # Optional move to GhostShell's clips dir
        if target_dir:
            try:
                import shutil as _shutil
                os.makedirs(target_dir, exist_ok=True)
                base = os.path.basename(new_path)
                ext  = os.path.splitext(base)[1] or ".mp4"
                stem = (prefix or os.path.splitext(base)[0])
                final = os.path.join(target_dir, stem + ext)
                # Avoid clobber on rapid double-save (same second)
                if os.path.exists(final):
                    final = os.path.join(target_dir,
                                          f"{stem}_{int(time.time())}{ext}")
                _shutil.move(new_path, final)
                log.info(f"OBS replay moved: {new_path} → {final}")
                new_path = final
            except Exception as e:
                log.warning(f"OBS clip move to {target_dir!r} failed: {e}")

        log.info(f"OBS save_replay → {new_path}")
        return {"ok": True, "path": new_path, "err": ""}
    except Exception as e:
        log.exception("OBS save_replay failed")
        return {"ok": False, "path": "", "err": _err_msg(e)}


def get_recording_status() -> dict:
    """Live status of the OBS-side recording pipeline.  Returns
    {scene_ready, buffer_running, buffer_size_mb, err}.  Cheap (one
    RPC call per check) so safe for the Capture-page status poll."""
    out = {"scene_ready":    False,
           "buffer_running": False,
           "buffer_size_mb": 0,
           "err":            ""}
    with _client_lock:
        if _client is None or not _conn_state["connected"]:
            out["err"] = "Not connected"
            return out
        try:
            scenes, items = _scene_state_unlocked()
            out["scene_ready"] = (GS_SCENE_NAME in scenes
                                    and GS_INPUT_DISPLAY in items)
            status = _client.get_replay_buffer_status()
            out["buffer_running"] = bool(getattr(status, "output_active", False))
            # Newer OBS reports duration / sizes via separate calls.
            # Skip the size lookup for now — not worth a third RPC
            # on every poll.
            return out
        except Exception as e:
            out["err"] = _err_msg(e)
            return out


def is_ready_for_recording() -> bool:
    """True when the OBS backend should handle save-clip requests
    instead of GhostShell's FFmpeg path.  Used by clipper.save_clip()
    to route between backends."""
    s = get_settings()
    return bool(s.get("enabled") and _conn_state["connected"])


# ─── Status for the Capture page UI ─────────────────────────────────

def get_status() -> dict:
    """Combined status: detection + user settings + connection state.
    This is what the /api/obs/status endpoint returns and the UI
    renders."""
    # Fire a (rate-limited, async) reconnect attempt if the toggle is
    # on but we're not connected.  Doesn't block this call.
    _maybe_auto_reconnect()
    det = detect_obs()
    s   = get_settings()
    rec = get_recording_status() if _conn_state["connected"] else {
        "scene_ready": False, "buffer_running": False,
        "buffer_size_mb": 0, "err": "",
    }
    return {
        "detected":       det,
        "settings":       s,
        "connection":     dict(_conn_state),
        "recording":      rec,
        # The backend is *fully active* (clipper save-clip is routed
        # here) when the install is compatible, the toggle is on, the
        # WebSocket is live, AND the OBS scene + replay buffer have
        # been set up.  beta.18 wires save_clip to dispatch on this.
        "ready":          bool(det["ok"] and det["version_ok"]
                                and s["enabled"] and _conn_state["connected"]
                                and rec["scene_ready"] and rec["buffer_running"]),
        "install_url":    "https://obsproject.com/download",
    }
