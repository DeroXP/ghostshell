"""Stream Helper — auto-configured OBS recording for streamers.

What this is
────────────
A higher-level mode on top of `core.obs_backend` that hides the manual
Capture-page controls (pick monitor, pick mic, pick system audio,
toggle buffer, configure encoders, etc) and just DOES the right
thing for streaming gameplay:

  • Game Capture (OBS `game_capture` source) targeting whichever
    game is currently in the foreground.  Game Capture uses hook
    injection — handles fullscreen-exclusive games, high refresh
    rates, and HDR conversion far better than Display Capture / DXGI
    Desktop Duplication.  When no game is in foreground, the source
    is configured for `any_fullscreen` so it locks onto whatever
    fullscreen app appears.
  • Three independent audio sources on three OBS audio tracks so
    streamers can mix in OBS / their broadcast software:
      track 1 — game audio   (wasapi_process_output_capture of the
                                active game's process)
      track 2 — Discord audio (wasapi_process_output_capture of
                                Discord.exe)
      track 3 — microphone   (wasapi_input_capture of default mic)
  • Foreground game tracker that swaps the Game Capture window
    target + game-audio process target when the user launches a
    different game.  Uses the existing `core.game_profiles`
    detection — no second polling loop.

When the Stream Helper toggle is on the legacy Capture page
controls (monitor dropdown, mic dropdown, encoder dropdown, etc)
collapse into a single status block.  When off, the page reverts
to the v3.3 manual Capture UI.

Settings live in `%APPDATA%/GhostShell/stream_helper_settings.json`
so they don't entangle with clipper_settings or obs_settings.

This module is the foundation (v3.3.1-beta.1).  Scene setup is
implemented + idempotent; the foreground-game tracker that hot-swaps
the Game Capture target lands in beta.2.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger(__name__)


# ─── Source names (prefixed so they don't collide with anything the
#     user has in their own OBS scenes) ────────────────────────────

SH_SCENE_NAME       = "GhostShell Stream Helper"
SH_GAME_CAPTURE     = "GhostShell Game Capture"
SH_GAME_AUDIO       = "GhostShell Game Audio"
SH_DISCORD_AUDIO    = "GhostShell Discord Audio"
SH_MIC              = "GhostShell Mic"


# ─── Settings persistence ───────────────────────────────────────────

_SETTINGS_PATH = os.path.join(APPDATA_DIR, "stream_helper_settings.json")

_DEFAULTS = {
    # Master toggle.  When True, GhostShell sets up + maintains the
    # Stream Helper scene in OBS and the legacy Capture page controls
    # collapse to a status indicator.
    "enabled":             False,
    # Sub-toggles for the three audio sources.  All on by default so
    # the "just works" experience captures everything a typical
    # gameplay streamer wants on day one.
    "capture_game_audio":     True,
    "capture_discord_audio":  True,
    "capture_mic":            True,
    # Game Capture mode.  v3.3.1-beta.2: default flipped from
    # "any_fullscreen" to "window".  any_fullscreen would have OBS
    # lock onto any fullscreen app (video players, browsers in
    # fullscreen, etc.) — but the user wants Game Capture to ONLY
    # engage for games that exist in GhostShell's game profiler.
    # In "window" mode we push the foreground game's exe into the
    # window matcher; when no known game is in foreground the
    # matcher is empty and OBS captures nothing.
    "game_capture_mode":      "window",
}


def _load_settings() -> dict:
    try:
        if not os.path.isfile(_SETTINGS_PATH):
            return dict(_DEFAULTS)
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return {**_DEFAULTS, **data}
    except Exception as e:
        log.debug(f"stream_helper settings load failed: {e}")
        return dict(_DEFAULTS)


def _save_settings(s: dict) -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log.warning(f"stream_helper settings save failed: {e}")


def get_settings() -> dict:
    return _load_settings()


def set_settings(**kwargs) -> dict:
    s = _load_settings()
    for k, v in kwargs.items():
        if k in _DEFAULTS:
            s[k] = v
    _save_settings(s)
    return {"ok": True, "settings": s}


# ─── Window-matching helpers ───────────────────────────────────────
#
# OBS's window-matcher format is "<title>:<class>:<exe>".  With
# priority=2 in OBS settings, the exe match is required and the
# title/class fields are advisory.  Different sources need different
# matcher strings because Electron apps (Discord) all share the
# Chrome_WidgetWin_1 class but games have varied window classes —
# matching by exe alone is the only universal approach for games.

_DISCORD_EXE = "Discord.exe"
# Electron apps (Discord, Slack, VSCode, etc) share this class.  Used
# for Discord audio capture matcher.
_ELECTRON_CLASS = "Chrome_WidgetWin_1"


def _process_match_string(exe: str, klass: str = "") -> str:
    """Build an OBS window-matcher string '<title>:<class>:<exe>'.

    `klass` is optional; pass it for known-class apps like Discord
    to slightly tighten the match.  For games, leave it empty — game
    window classes vary across engines and DRM wrappers, exe matching
    is the only reliable approach.
    """
    return f":{klass}:{exe}"


# ─── Scene + source setup ──────────────────────────────────────────

def _get_client():
    """Borrow the obs_backend's WebSocket client.  Returns None if not
    connected — caller is responsible for surfacing that to the UI."""
    from core import obs_backend
    if not obs_backend.is_connected():
        return None
    return obs_backend._client      # noqa: deliberate cross-module poke


def _scene_input_names(client, scene_name: str) -> set:
    try:
        items = client.get_scene_item_list(scene_name)
        return {it["sourceName"] for it in items.scene_items}
    except Exception:
        return set()


def setup_scene(active_game_exe: Optional[str] = None) -> dict:
    """Create + configure the Stream Helper scene in OBS.  Idempotent:
    safe to call repeatedly (will only add missing sources, and will
    update existing source settings to match the latest config).

    `active_game_exe`: if known, the game-audio source's process
    target is set to it.  When None we leave whatever was last
    configured in place (the foreground tracker will update it).

    Returns {ok, scene, sources_created, err}.
    """
    from core import obs_backend
    client = _get_client()
    if client is None:
        return {"ok": False, "err": "Not connected to OBS",
                "scene": "", "sources_created": []}

    s = _load_settings()
    created = []
    try:
        # 1. Scene ────────────────────────────────────────────────
        sl = client.get_scene_list()
        scene_names = [sc["sceneName"] for sc in sl.scenes]
        if SH_SCENE_NAME not in scene_names:
            client.create_scene(SH_SCENE_NAME)
            log.info(f"Stream Helper: created scene '{SH_SCENE_NAME}'")
            created.append("scene")
        client.set_current_program_scene(SH_SCENE_NAME)
        existing = _scene_input_names(client, SH_SCENE_NAME)

        # 2. Game Capture source ─────────────────────────────────
        # `capture_mode` values: any_fullscreen | window | hotkey
        # `priority` for window mode: 0=title, 1=title+class, 2=exe
        #
        # v3.3.1-beta.2: gate by game_profiles.  Default mode is now
        # `window` so the matcher controls which app is captured —
        # we only fill it with the foreground game's exe if that
        # exe is in GhostShell's known-games list (caller checks
        # via game_profiles.get_foreground_game() before passing
        # active_game_exe).  When the matcher is empty OBS Game
        # Capture finds nothing to hook → no capture happens, which
        # is exactly what the user wants for non-game windows.
        gc_mode = s.get("game_capture_mode", "window")
        gc_settings = {
            "capture_mode":           gc_mode,
            "capture_cursor":         True,
            "allow_transparency":     False,
            "capture_overlays":       False,
            # Window matcher: exe-only when we have a known active
            # game, empty otherwise (no capture).
            "window":                 (_process_match_string(active_game_exe)
                                          if active_game_exe and gc_mode == "window"
                                          else ""),
            "priority":               2,    # exe match required
            # v3.3.1-beta.3: explicit defaults.  anti_cheat_hook=true
            # helps with games that use Easy Anti-Cheat / BattlEye
            # (does NOT help with Hyperion/Byfron on Roblox or
            # Vanguard on Valorant — those actively block OBS hooks
            # and require Window Capture / Display Capture as a
            # fallback).  hook_rate=1 (medium) is OBS's recommended
            # polling cadence for the injection probe.
            "anti_cheat_hook":        True,
            "hook_rate":              1,
            "sli_compatibility":      False,
            "limit_framerate":        False,
        }
        if SH_GAME_CAPTURE not in existing:
            client.create_input(
                SH_SCENE_NAME, SH_GAME_CAPTURE, "game_capture",
                gc_settings, True,
            )
            log.info(f"Stream Helper: created Game Capture ({gc_mode})")
            created.append("game_capture")
        else:
            try:
                client.set_input_settings(SH_GAME_CAPTURE, gc_settings, True)
            except Exception as e:
                log.debug(f"Stream Helper: game_capture update failed: {e}")

        # 3. Game audio (per-process WASAPI capture of the active game).
        # Exe-only matching since game window classes vary.  Only
        # creates the source when a known game is in foreground —
        # game_profiles.get_foreground_game() already filters by the
        # profiler list, so this naturally gates on it.
        if s.get("capture_game_audio") and active_game_exe:
            ga_settings = {
                "window":   _process_match_string(active_game_exe),
                "priority": 2,
            }
            if SH_GAME_AUDIO not in existing:
                try:
                    client.create_input(
                        SH_SCENE_NAME, SH_GAME_AUDIO,
                        "wasapi_process_output_capture",
                        ga_settings, True,
                    )
                    log.info(f"Stream Helper: created Game Audio "
                             f"(target {active_game_exe!r})")
                    created.append("game_audio")
                except Exception as e:
                    log.warning(f"Stream Helper: game audio create failed "
                                f"(OBS < 28?): {e}")
            else:
                try:
                    client.set_input_settings(SH_GAME_AUDIO, ga_settings, True)
                except Exception as e:
                    log.debug(f"Stream Helper: game audio update failed: {e}")

        # 4. Discord audio ────────────────────────────────────────
        # Discord is an Electron app — use the Chrome_WidgetWin_1
        # class match in addition to the exe so we don't accidentally
        # latch onto an unrelated Discord.exe if the user has multiple
        # Discord installs (Stable + Canary + PTB, etc.).
        if s.get("capture_discord_audio") and SH_DISCORD_AUDIO not in existing:
            try:
                client.create_input(
                    SH_SCENE_NAME, SH_DISCORD_AUDIO,
                    "wasapi_process_output_capture",
                    {
                        "window":   _process_match_string(_DISCORD_EXE, _ELECTRON_CLASS),
                        "priority": 2,
                    },
                    True,
                )
                log.info("Stream Helper: created Discord Audio capture")
                created.append("discord_audio")
            except Exception as e:
                log.warning(f"Stream Helper: discord audio create failed: {e}")

        # 5. Mic ──────────────────────────────────────────────────
        if s.get("capture_mic") and SH_MIC not in existing:
            try:
                client.create_input(
                    SH_SCENE_NAME, SH_MIC, "wasapi_input_capture",
                    {"device_id": "default"},
                    True,
                )
                log.info("Stream Helper: created Mic capture")
                created.append("mic")
            except Exception as e:
                log.warning(f"Stream Helper: mic create failed: {e}")

        return {"ok": True, "scene": SH_SCENE_NAME,
                "sources_created": created, "err": ""}
    except Exception as e:
        log.exception("Stream Helper setup_scene failed")
        return {"ok": False, "err": obs_backend._err_msg(e),
                "scene": SH_SCENE_NAME, "sources_created": created}


# ─── Foreground-game watcher (lands fully in beta.2; foundation here)

_watcher_thread: Optional[threading.Thread] = None
_watcher_stop: Optional[threading.Event] = None
_last_active_exe: str = ""


def update_for_game(exe: Optional[str]) -> dict:
    """Re-point Game Capture + Game Audio sources at the given game's
    process.  Called from the foreground-game watcher when the user
    launches / closes a game.  Pass None or '' to unbind (any_fullscreen
    mode keeps capturing whatever's fullscreen)."""
    global _last_active_exe
    s = _load_settings()
    if not s.get("enabled"):
        return {"ok": False, "err": "Stream Helper disabled"}
    if (exe or "") == _last_active_exe:
        return {"ok": True, "noop": True, "exe": exe or ""}
    _last_active_exe = exe or ""
    log.info(f"Stream Helper: active game → {exe or '(none)'}")
    return setup_scene(active_game_exe=exe or None)


def start_watcher() -> None:
    """Launch the foreground-game watcher thread.  Polls every 2 s
    and calls update_for_game() whenever the active game changes.
    No-op if already running.

    v3.3.1-beta.1 ships a stub that just logs; the real watcher
    integration with core.game_profiles lands in beta.2."""
    global _watcher_thread, _watcher_stop
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_stop = threading.Event()
    _watcher_thread = threading.Thread(
        target=_watcher_loop, args=(_watcher_stop,),
        daemon=True, name="stream-helper-watcher",
    )
    _watcher_thread.start()
    log.info("Stream Helper: watcher started")


def stop_watcher() -> None:
    global _watcher_thread, _watcher_stop
    if _watcher_stop:
        _watcher_stop.set()
    _watcher_thread = None
    _watcher_stop = None


def _watcher_loop(stop: threading.Event) -> None:
    """Background loop.  Pure observer — does not start/stop OBS
    recording, just keeps Game Capture / Game Audio sources pointed
    at the right process.

    v3.3.1-beta.3: switched from get_foreground_game() (which returns
    None the moment the user alt-tabs to Discord — blanking Game
    Capture mid-stream) to get_active_games() with a preference
    cascade:
      1. The known game currently in foreground, if any
      2. Otherwise the first running known game
      3. Otherwise empty (no known games running at all)
    Means a single-game streaming setup keeps capturing the game
    whether the user is alt-tabbed away or not — which is what
    every streamer expects.

    Also: exe comes from get_active_games() which preserves the
    real PE-header case (RobloxPlayerBeta.exe), not the lowercased
    version that GetWindowText returns.  OBS' Game Capture +
    wasapi_process_output_capture matchers are case-sensitive
    against the OS-reported module name.
    """
    while not stop.is_set():
        try:
            s = _load_settings()
            if not s.get("enabled"):
                stop.wait(2.0); continue
            from core import game_profiles
            active = game_profiles.get_active_games() or []
            target = None
            # Prefer the foreground game
            for g in active:
                if g.get("foreground"):
                    target = g
                    break
            # Fall back to the first running known game (so a
            # single-game setup keeps capturing across alt-tabs)
            if target is None and active:
                target = active[0]
            target_exe = (target.get("exe") if target else "") or ""
            update_for_game(target_exe.strip())
        except Exception as e:
            log.debug(f"Stream Helper watcher tick failed: {e}")
        stop.wait(2.0)


# ─── Public status surface ──────────────────────────────────────────

def get_status() -> dict:
    """Snapshot of Stream Helper state for the Capture page UI."""
    from core import obs_backend
    s = _load_settings()
    obs_status = obs_backend.get_status()
    return {
        "settings":      s,
        "obs_connected": obs_status["connection"]["connected"],
        "obs_ready":     obs_status["ready"],
        "active_game":   _last_active_exe,
        "watcher_running": bool(_watcher_thread and _watcher_thread.is_alive()),
    }
