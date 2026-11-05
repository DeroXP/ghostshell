"""Crosshair overlay: the feature module, and the only crosshair module app.py
calls.

It keeps the settings (crosshair_settings.json), the user's presets and
images, the per-game rules, and the fullscreen check and fix, and hands
core/crosshair_overlay.py a ready-made runtime config. The overlay owns the
window; nothing here touches it. Every function returns a dict with "ok" and
never raises to its caller.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import ntpath
import os
import re
import secrets
import struct
import threading
import time
import winreg

from PIL import Image

import config
from core import crosshair_codes as codes
from core import crosshair_overlay as overlay
from core import crosshair_render as xr
from core import frame_telemetry, game_profiles, reg_originals
from core.utils import atomic_write_json, get_logger

log = get_logger("crosshair")

SETTINGS_PATH = os.path.join(config.APPDATA_DIR, "crosshair_settings.json")
IMAGES_DIR = os.path.join(config.APPDATA_DIR, "crosshairs")
LEGACY_PROFILES = "gamepad_profiles.json"       # the removed Gamepad Mapper's profiles

DEFAULT_PRESET = "builtin:classic"
MAX_USER_PRESETS = 100
MAX_GAMES = 200
MAX_NAME = 40
OFFSET_LIMIT = 500
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_SIDE = 512
SHOW_MODES = ("games", "always", "list")
ANCHORS = ("game", "monitor")
HIDE_ON_AIM = ("off", "hold", "toggle")
AIM_BUTTONS = ("mouse2", "mouse3", "mouse4", "mouse5")
RULE_MODES = ("default", "show", "hide")
BACKGROUNDS = {"dark": (22, 24, 29), "light": (236, 238, 241)}

# Second-hand reports, not checked by Vispora: the crosshair is hidden while one of
# these is in front (unless a "show" rule says otherwise), but its window still exists.
BLOCKED_EXES = {
    "escapefromtarkov.exe": "Reported not to start while overlay apps are running.",
    "sotgame.exe": "Players have reported being flagged for crosshair overlays.",
}
BLOCKED_NAMES = {"escapefromtarkov.exe": "Escape from Tarkov", "sotgame.exe": "Sea of Thieves"}

# Fullscreen optimizations: where Windows keeps the per-program and global opt-outs.
LAYERS_KEYS = (("HKCU", r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"),
               ("HKLM", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"))
GAME_CONFIG_STORE = r"HKCU\System\GameConfigStore"
FSO_TOKEN = "DISABLEDXMAXIMIZEDWINDOWEDMODE"
UNDO_SCOPE = "crosshair"
PRESENT_MODE_FRESH_S = 10.0

_DEFAULT_HOTKEYS = {"toggle": "Ctrl+Alt+X", "next": "", "prev": ""}
_DEFAULTS = {
    "version": 1, "enabled": False, "active_preset": DEFAULT_PRESET, "user_presets": [],
    "show_mode": "games", "anchor": "game", "monitor": "auto", "offset_x": 0, "offset_y": 0,
    "hide_on_aim": "off", "aim_button": "mouse2", "hide_with_cursor": False,
    "hotkeys": dict(_DEFAULT_HOTKEYS), "exclude_from_capture": False, "games": {},
    "notice_seen": False, "legacy_notice_seen": False,
}
_BOOL_KEYS = ("enabled", "hide_with_cursor", "exclude_from_capture", "notice_seen", "legacy_notice_seen")
_ENUM_KEYS = {"show_mode": SHOW_MODES, "anchor": ANCHORS, "hide_on_aim": HIDE_ON_AIM, "aim_button": AIM_BUTTONS}
_ENUM_ERRORS = {
    "show_mode": "Pick when the crosshair shows: games, always or your list.",
    "anchor": "Pick where to centre the crosshair: the game window or the screen.",
    "hide_on_aim": "Pick Off, While held or Toggle for hiding while aiming.",
    "aim_button": "Pick the right, middle, back or forward mouse button for aiming.",
}
_SETTABLE = _BOOL_KEYS + tuple(_ENUM_KEYS) + ("monitor", "offset_x", "offset_y", "hotkeys")
_AIM_TOKENS = {"mouse3": "Mouse3", "mouse4": "Mouse4", "mouse5": "Mouse5"}     # mouse2 can't be a hotkey

_USER_ID_RE = re.compile(r"user:[0-9a-f]{8}")
_IMAGE_ID_RE = re.compile(r"[0-9a-f]{32}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MONITOR_RE = re.compile(r"[\x20-\x7e]{1,64}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_THUMB_MAX = 128                    # px; a thumbnail is a picker, not the real size
_SET_ASIDE_KEEP_S = 30 * 24 * 3600  # images a damaged, set-aside settings file names are kept this long
_READ_TRIES = 5                     # reading a settings file another program holds for a moment
_READ_RETRY_S = 0.05
_WATCH_EVERY_S = 2.0

_IMAGE_TOO_BIG = "That image is too big. Use one up to 512 x 512 pixels."
_MISSING_IMAGE_WARNING = "The image for this crosshair is missing, so the default crosshair is shown."
_ERR_UNREADABLE = "Couldn't read the crosshair settings. Try again in a moment."
_ERR_UNREADABLE_CHANGE = "Couldn't read the crosshair settings, so nothing was changed. Try again in a moment."

_lock = threading.RLock()           # every load-modify-save of the settings (hotkeys cycle on other threads)
_life = threading.Lock()            # starting and stopping the overlay and the watch thread
_ui_ready = False                   # app.py says the process is DPI aware and the window exists
_enabled = None                     # cached "enabled", for the tray menu
_start_error = None
_watch_thread = None
_watch_stop = None
_thumbs: dict = {}                  # design hash -> PNG data URI
_images: dict = {}                  # image id -> (mtime_ns, size, RGBA image)


class _BadImage(Exception):
    """A picked image Vispora won't use; the message is shown to the user."""


class _SettingsUnreadable(Exception):
    """The settings file is there but can't be read (or set aside) right now.
    Never treated as "no file": the defaults would be saved over it."""


def _err(msg: str) -> dict:
    return {"ok": False, "err": msg}


# ── values ────────────────────────────────────────────────────────────
def _int_in(v, lo: int, hi: int):
    """v as a whole number clamped to lo..hi, or None when v isn't a number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v.strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return max(lo, min(hi, int(round(f))))


def _clean_name(v) -> str:
    if not isinstance(v, str):
        return ""
    return " ".join(_CONTROL_RE.sub(" ", v).split())


def _valid_name(v):
    """(name, None) or (None, error)."""
    name = _clean_name(v)
    if not name:
        return None, "Give the crosshair a name."
    if len(name) > MAX_NAME:
        return None, f"Keep the name to {MAX_NAME} characters or fewer."
    return name, None


def _norm_exe(v) -> str:
    """A lowercase exe name ("cs2.exe") from a name or a path, or "" if it isn't one."""
    if not isinstance(v, str):
        return ""
    exe = ntpath.basename(v.strip().strip('"').strip()).strip().lower()
    if exe and not exe.endswith(".exe"):
        exe += ".exe"
    if len(exe) > 128 or not game_profiles._SAFE_EXE.match(exe):
        return ""
    return exe


def _canon_hotkey(v):
    """(canonical text, None), ("", None) for "not set", or (None, error)."""
    if v is None:
        return "", None
    spec, err = overlay.parse_hotkey(v)
    if err:
        return None, err
    return (spec["text"] if spec else ""), None


# ── settings ──────────────────────────────────────────────────────────
def _norm_rule(r, ids) -> dict:
    r = r if isinstance(r, dict) else {}
    mode = r.get("mode") if r.get("mode") in RULE_MODES else "default"
    preset = r.get("preset")
    out = {"mode": mode, "preset": preset if isinstance(preset, str) and preset in ids else None,
           "offset_x": None, "offset_y": None}
    for key in ("offset_x", "offset_y"):
        if r.get(key) is not None:
            out[key] = _int_in(r.get(key), -OFFSET_LIMIT, OFFSET_LIMIT)
    return out


def _normalize(raw) -> dict:
    """Complete, valid settings from whatever the file holds: defaults for what's
    missing, clamped numbers, known choices only, at most 100 presets and 200
    game rules."""
    d = raw if isinstance(raw, dict) else {}
    s = copy.deepcopy(_DEFAULTS)
    for key in _BOOL_KEYS:
        if isinstance(d.get(key), bool):
            s[key] = d[key]
    for key, allowed in _ENUM_KEYS.items():
        if d.get(key) in allowed:
            s[key] = d[key]
    mon = d.get("monitor")
    if isinstance(mon, str) and _MONITOR_RE.fullmatch(mon):
        s["monitor"] = mon
    for key in ("offset_x", "offset_y"):
        v = _int_in(d.get(key), -OFFSET_LIMIT, OFFSET_LIMIT)
        if v is not None:
            s[key] = v
    hk = d.get("hotkeys") if isinstance(d.get("hotkeys"), dict) else {}
    for name in overlay.HOTKEY_NAMES:
        if name in hk:
            text, err = _canon_hotkey(hk.get(name))
            if err is None:
                s["hotkeys"][name] = text
    presets, seen = [], set()
    for p in d.get("user_presets") if isinstance(d.get("user_presets"), list) else []:
        if len(presets) >= MAX_USER_PRESETS:
            break
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not _USER_ID_RE.fullmatch(pid) or pid in seen:
            continue
        seen.add(pid)
        name = _clean_name(p.get("name"))[:MAX_NAME].strip() or "My crosshair"
        presets.append({"id": pid, "name": name, "design": xr.normalize_design(p.get("design"))})
    s["user_presets"] = presets
    ids = set(xr.BUILTIN_IDS) | seen
    if d.get("active_preset") in ids:
        s["active_preset"] = d["active_preset"]
    games = d.get("games") if isinstance(d.get("games"), dict) else {}
    for exe, rule in games.items():
        if len(s["games"]) >= MAX_GAMES:
            break
        name = _norm_exe(exe)
        if name and name not in s["games"]:
            s["games"][name] = _norm_rule(rule, ids)
    return s


def _set_aside():
    """Move a damaged settings file aside. None when done (or it's gone), else the error."""
    stamp = int(time.time() * 1000)
    while os.path.exists(f"{SETTINGS_PATH}.corrupt-{stamp}"):     # never over an earlier one
        stamp += 1
    bad = f"{SETTINGS_PATH}.corrupt-{stamp}"
    try:
        os.replace(SETTINGS_PATH, bad)
    except FileNotFoundError:
        return None
    except OSError as e:
        return e
    log.warning(f"crosshair settings were damaged; moved to {bad}")
    return None


def _load() -> dict:
    """The settings, complete and valid. No file: the defaults. A damaged file
    is moved aside rather than overwritten by the next save, so the user's
    presets can still be recovered by hand, and gives the defaults too. A file
    that is there but can't be read (a virus scan or a backup holding it) is
    tried again for a moment, then raises _SettingsUnreadable: defaults then
    would be saved over it by the next change. Under _lock, so a reader can't
    put an old "enabled" back into the cache after a save."""
    global _enabled
    with _lock:
        raw, err, damaged = None, None, False
        for attempt in range(_READ_TRIES):
            if attempt:
                time.sleep(_READ_RETRY_S)
            raw, err, damaged = None, None, False
            try:
                # utf-8-sig: a file saved by hand with a BOM is still read
                with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                break
            except (ValueError, RecursionError):
                raw = None
            except OSError as e:
                err = e
                continue
            if isinstance(raw, dict):
                break
            raw, damaged = None, True                    # damaged, or not a JSON object
            err = _set_aside()
            if err is None:
                break
        if err is not None:
            what = "are damaged and couldn't be moved aside" if damaged else "couldn't be read"
            log.warning(f"crosshair settings {what}: {err}")
            raise _SettingsUnreadable(str(err))
        s = _normalize(raw)
        _enabled = s["enabled"]
        return s


def _save(s: dict) -> bool:
    global _enabled
    ok = atomic_write_json(SETTINGS_PATH, s)
    if ok:
        _enabled = s["enabled"]
    return ok


# ── presets ───────────────────────────────────────────────────────────
def _user(s: dict, pid):
    return next((p for p in s["user_presets"] if p["id"] == pid), None)


def _ids(s: dict) -> list:
    """Every preset id in display order: built-ins, then the user's."""
    return list(xr.BUILTIN_IDS) + [p["id"] for p in s["user_presets"]]


def _design_of(s: dict, pid):
    b = xr.builtin_preset(pid)
    if b is not None:
        return b["design"]
    u = _user(s, pid)
    return copy.deepcopy(u["design"]) if u else None


def _preset_in_effect(s: dict, exe: str) -> str:
    rule = s["games"].get(exe) if exe else None
    if rule and rule.get("preset") in _ids(s):
        return rule["preset"]
    return s["active_preset"]


def _game_in_front() -> str:
    """The game the overlay sees in front ("" when it isn't running or none is)."""
    try:
        if overlay.is_running():
            return overlay.get_status().get("game_exe") or ""
    except Exception:
        log.debug("crosshair: no overlay status", exc_info=True)
    return ""


def _set_in_effect(s: dict, pid: str, exe: str) -> None:
    """Make pid the preset in effect: the rule's preset when the game in front
    has a rule with one, else the active preset."""
    rule = s["games"].get(exe) if exe else None
    if rule is not None and rule.get("preset"):
        rule["preset"] = pid
    else:
        s["active_preset"] = pid


def _new_user_id(s: dict) -> str:
    taken = set(_ids(s))
    while True:
        pid = "user:" + secrets.token_hex(4)
        if pid not in taken:
            return pid


def _thumb(design: dict) -> str:
    key = xr.design_hash(design)
    uri = _thumbs.get(key)
    if uri is not None:
        return uri
    try:
        img = xr.render(design, xr.BASE_HEIGHT, _load_image)
        missing = img.info.get("warning") == "image missing"
        if max(img.size) > _THUMB_MAX:
            f = _THUMB_MAX / max(img.size)
            img = img.resize((max(1, round(img.size[0] * f)), max(1, round(img.size[1] * f))),
                             Image.Resampling.LANCZOS)
        uri = xr.to_png_data_uri(img)
    except Exception:
        log.warning("crosshair: thumbnail not drawn", exc_info=True)
        return ""
    if not missing:                     # the image may come back (same id, same picture)
        if len(_thumbs) >= 512:
            _thumbs.clear()
        _thumbs[key] = uri
    return uri


def _presets_list(s: dict) -> list:
    out = [{"id": p["id"], "name": p["name"], "builtin": True, "design": copy.deepcopy(p["design"]),
            "thumb": _thumb(p["design"])} for p in xr.BUILTIN_PRESETS]
    out += [{"id": p["id"], "name": p["name"], "builtin": False, "design": copy.deepcopy(p["design"]),
             "thumb": _thumb(p["design"])} for p in s["user_presets"]]
    return out


# ── images ────────────────────────────────────────────────────────────
def _image_path(iid: str) -> str:
    return os.path.join(IMAGES_DIR, iid + ".png")


def _open_png(data: bytes) -> Image.Image:
    """The first frame of a PNG as RGBA, at most 512 x 512. Raises _BadImage."""
    if not data.startswith(_PNG_SIGNATURE):
        raise _BadImage("That file isn't a PNG image.")
    # The size is in the header: refuse a huge one before Pillow even parses it.
    if len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        if w > MAX_IMAGE_SIDE or h > MAX_IMAGE_SIDE:
            raise _BadImage(_IMAGE_TOO_BIG)
    try:
        img = Image.open(io.BytesIO(data), formats=("PNG",))     # MAX_IMAGE_PIXELS stays at Pillow's default
        if img.size[0] > MAX_IMAGE_SIDE or img.size[1] > MAX_IMAGE_SIDE:
            raise _BadImage(_IMAGE_TOO_BIG)
        if min(img.size) < 1:
            raise _BadImage("That PNG image is empty.")
        img.seek(0)                                              # an animated PNG: its first frame
        img.load()
        return img.convert("RGBA")
    except _BadImage:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _BadImage(_IMAGE_TOO_BIG)
    except Exception:
        raise _BadImage("That PNG image couldn't be read.")


def _load_image(iid):
    """A saved crosshair image (RGBA) by id, or None. The id is checked first."""
    if not isinstance(iid, str) or not _IMAGE_ID_RE.fullmatch(iid):
        return None
    path = _image_path(iid)
    try:
        st = os.stat(path)
    except OSError:
        _images.pop(iid, None)
        return None
    cached = _images.get(iid)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    if st.st_size > 4 * MAX_IMAGE_BYTES:      # ours are re-encoded inputs of at most 1 MB
        return None
    try:
        with open(path, "rb") as f:
            img = _open_png(f.read())
    except (OSError, _BadImage):
        log.warning(f"crosshair image {iid} couldn't be read")
        return None
    _images[iid] = (st.st_mtime_ns, st.st_size, img)
    return img


def _image_problem(design: dict):
    """An error when an image design points at an image that isn't saved."""
    if design["kind"] == "image":
        if not design["image"]["id"]:
            return "Pick an image first."
        if not os.path.isfile(_image_path(design["image"]["id"])):
            return "That image is missing. Pick it again."
    return None


def _referenced_images(s: dict) -> set:
    return {p["design"]["image"]["id"] for p in s["user_presets"] if p["design"]["image"]["id"]}


def _ids_in_set_aside():
    """Image ids named by the settings files set aside as damaged in the last
    30 days (read as text: they can't be parsed), or None when one of them
    couldn't be read."""
    folder = os.path.dirname(SETTINGS_PATH)
    prefix = os.path.basename(SETTINGS_PATH) + ".corrupt-"
    try:
        names = [n for n in os.listdir(folder) if n.startswith(prefix)]
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    out, now = set(), time.time()
    for name in names:
        path = os.path.join(folder, name)
        try:
            stamp = name[len(prefix):]
            when = int(stamp) / 1000 if re.fullmatch(r"[0-9]{1,16}", stamp) else os.path.getmtime(path)
            if now - when > _SET_ASIDE_KEEP_S:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                out.update(_IMAGE_ID_RE.findall(f.read(4 * 1024 * 1024)))
        except OSError:
            return None
    return out


def _gc_images(before: set, s: dict) -> None:
    """Delete the images the presets used before this change and no longer
    use. Nothing else: an image never saved in a preset is left alone, and so
    is one a damaged settings file set aside lately still names (the user may
    put that file back by hand)."""
    drop = set(before) - _referenced_images(s)
    if drop:
        kept = _ids_in_set_aside()
        drop = set() if kept is None else drop - kept
    for iid in drop:
        if not _IMAGE_ID_RE.fullmatch(iid or ""):
            continue
        try:
            os.remove(_image_path(iid))
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning(f"crosshair image {iid} not deleted: {e}")
            continue
        _images.pop(iid, None)


# ── the overlay's runtime config ──────────────────────────────────────
def _is_known_game(exe: str) -> bool:
    return exe in game_profiles.KNOWN_GAME_EXES          # custom games included, and added live


def _runtime_cfg(s: dict) -> dict:
    designs = {}
    wanted = [s["active_preset"]] + [r["preset"] for r in s["games"].values() if r.get("preset")]
    for pid in wanted:
        if pid not in designs:
            d = _design_of(s, pid)
            if d is not None:
                designs[pid] = d
    images = {}
    for d in designs.values():
        iid = d["image"]["id"]
        if d["kind"] == "image" and iid and iid not in images:
            img = _load_image(iid)                       # the same object while the file is unchanged
            if img is not None:
                images[iid] = img
    return {
        "enabled": s["enabled"], "show_mode": s["show_mode"], "active_preset": s["active_preset"],
        "designs": designs, "images": images, "render": xr.render,
        "game_rules": {exe: dict(rule) for exe, rule in s["games"].items()},
        "blocked_exes": frozenset(BLOCKED_EXES), "own_exes": frozenset(config.OWN_PROCESS_NAMES),
        "is_known_game": _is_known_game,
        "anchor": s["anchor"], "monitor": s["monitor"], "offset_x": s["offset_x"], "offset_y": s["offset_y"],
        "hide_on_aim": s["hide_on_aim"], "aim_button": s["aim_button"], "hide_with_cursor": s["hide_with_cursor"],
        "hotkeys": dict(s["hotkeys"]), "exclude_from_capture": s["exclude_from_capture"],
    }


def _push(s: dict) -> None:
    try:
        overlay.configure(_runtime_cfg(s))
    except Exception:
        log.exception("crosshair: settings not handed to the overlay")


# ── starting and stopping ─────────────────────────────────────────────
def _on_hotkey(name: str) -> None:
    """The overlay's "next" / "prev" hotkeys (on a short thread of their own)."""
    if name == "next":
        r = cycle(1)
    elif name == "prev":
        r = cycle(-1)
    else:
        return
    if not r.get("ok"):
        log.warning(f"crosshair: {name} hotkey: {r.get('err')}")


def _start_overlay() -> dict:
    global _start_error
    with _life:
        with _lock:
            try:
                s = _load()
            except _SettingsUnreadable:
                _start_error = _ERR_UNREADABLE
                return _err(_ERR_UNREADABLE)
            if not s["enabled"]:
                return _err("Turn the crosshair on first.")
            if not _ui_ready:
                return {"ok": True, "running": False, "waiting": True}
            overlay.on_hotkey = _on_hotkey
            _push(s)                                     # configure before start: start uses the last cfg
        r = overlay.start()
        if r.get("ok"):
            _start_error = None
            _start_watch()
        else:
            _start_error = r.get("err") or "The crosshair window didn't open."
            log.warning(f"crosshair: overlay didn't start: {_start_error}")
        return r


def _stop_overlay() -> dict:
    with _life:
        _stop_watch()
        r = overlay.stop()
        try:
            overlay.set_exclusive_exes(frozenset())
        except Exception:
            pass
    return r


def _start_in_background() -> None:
    try:
        _start_overlay()
    except Exception:
        log.exception("crosshair: start failed")


def _exclusive_now() -> frozenset:
    """The exe PresentMon saw in true exclusive fullscreen in the last 10 s."""
    pm = frame_telemetry.get_present_mode()
    if not pm:
        return frozenset()
    try:
        age = time.time() - float(pm.get("ts") or 0)
    except (TypeError, ValueError):
        return frozenset()
    name = str(pm.get("name") or "").strip().lower()
    if name and 0 <= age < PRESENT_MODE_FRESH_S and str(pm.get("mode") or "").startswith("Hardware: Legacy"):
        return frozenset({name})
    return frozenset()


def _watch_loop(stop_ev: threading.Event) -> None:
    while True:
        try:
            overlay.set_exclusive_exes(_exclusive_now())
        except Exception:
            log.debug("crosshair: fullscreen watch failed", exc_info=True)
        if stop_ev.wait(_WATCH_EVERY_S) or not overlay.is_running():
            return


def _start_watch() -> None:
    global _watch_thread, _watch_stop
    if _watch_thread is not None and _watch_thread.is_alive():
        return
    ev = threading.Event()
    t = threading.Thread(target=_watch_loop, args=(ev,), name="crosshair-fs-watch", daemon=True)
    _watch_stop, _watch_thread = ev, t
    t.start()


def _stop_watch() -> None:
    global _watch_thread, _watch_stop
    ev, t = _watch_stop, _watch_thread
    _watch_stop = _watch_thread = None
    if ev is not None:
        ev.set()
    if t is not None and t is not threading.current_thread():
        t.join(timeout=1.0)


def _status_dict(s: dict) -> dict:
    """The overlay's status, with the reason filled in while it isn't running."""
    st = overlay.get_status()
    if not st.get("running"):
        if not s["enabled"]:
            st["reason"] = "disabled"
        elif not _ui_ready:
            st["reason"] = "waiting_for_app"
        elif _start_error:
            st["reason"], st["last_error"] = "error", _start_error
        else:
            st["reason"] = "not_running"
    return st


def allow_start() -> dict:
    """app.py: the process is DPI aware now. Starts the overlay (on a worker
    thread) when the crosshair is on."""
    global _ui_ready
    _ui_ready = True
    try:
        if is_enabled():
            threading.Thread(target=_start_in_background, name="crosshair-start", daemon=True).start()
    except Exception:
        log.exception("crosshair: allow_start failed")
    return {"ok": True}


def stop() -> dict:
    """Close the overlay and the watch thread (the exit path)."""
    try:
        _stop_overlay()
    except Exception:
        log.exception("crosshair: stop failed")
    return {"ok": True}


def is_enabled() -> bool:
    """Cheap: the cached setting (the tray asks every few seconds). A read
    that fails leaves the last good value in place."""
    try:
        v = _enabled
        if v is None:
            v = _load()["enabled"]
        return bool(v)
    except Exception:
        return False


def set_enabled(on) -> dict:
    return set_settings({"enabled": bool(on)})


# ── settings API ──────────────────────────────────────────────────────
def _clipper_combos() -> list:
    """[((mods, vk), text, what)] for the clipper's own hotkeys."""
    try:
        from core import capture_hotkeys, clipper
        cs = clipper.get_settings()
        out = []
        for key, what in (("save_hotkey", "saving a clip"), ("screenshot_hotkey", "screenshots")):
            text = cs.get(key)
            if isinstance(text, str) and text.strip():
                mods, vk = capture_hotkeys._parse_hotkey(text)
                if vk:
                    out.append(((mods & 0xF, vk), text.strip(), what))
        return out
    except Exception:
        log.debug("crosshair: clipper hotkeys not read", exc_info=True)
        return []


def _clipper_hotkeys() -> list:
    try:
        from core import clipper
        cs = clipper.get_settings()
        return [str(cs.get("save_hotkey") or ""), str(cs.get("screenshot_hotkey") or "")]
    except Exception:
        return []


def _hotkey_conflict(hotkeys: dict, check: set, aim_check: set, aim_button: str):
    """An error for the first clash: two crosshair hotkeys alike, one on the aim
    button, or one the clipper already uses. Only the names in `check` (and,
    for the aim button, `aim_check`) are looked at, so a clash saved earlier
    doesn't block unrelated changes."""
    labels = overlay.HOTKEY_LABELS
    specs = {n: overlay.parse_hotkey(hotkeys.get(n) or "")[0] for n in overlay.HOTKEY_NAMES}
    aim_key = _AIM_TOKENS.get(aim_button)
    clip = None
    for name in overlay.HOTKEY_NAMES:
        spec = specs[name]
        if spec is None:
            continue
        if aim_key and spec["key"] == aim_key and (name in check or name in aim_check):
            return f"{aim_key} is the aim button. Pick another key for {labels[name]}."
        if name not in check:
            continue
        combo = (spec["mods"], spec["vk"])
        for other in overlay.HOTKEY_NAMES:
            o = specs[other]
            if other != name and o is not None and (o["mods"], o["vk"]) == combo:
                return f"The {labels[name]} and {labels[other]} hotkeys can't be the same."
        if clip is None:
            clip = _clipper_combos()
        for ccombo, ctext, what in clip:
            if ccombo == combo:
                return f"{spec['text']} is already the hotkey for {what}. Pick other keys."
    return None


def _apply_partial(s: dict, partial: dict):
    """(new settings, None) or (None, error)."""
    new = copy.deepcopy(s)
    changed_hotkeys, aim_changed = set(), False
    for key in _SETTABLE:
        if key not in partial:
            continue
        v = partial[key]
        if key in _BOOL_KEYS:
            if not isinstance(v, bool):
                return None, "That switch must be on or off."
            new[key] = v
        elif key in _ENUM_KEYS:
            if v not in _ENUM_KEYS[key]:
                return None, _ENUM_ERRORS[key]
            aim_changed = aim_changed or (key == "aim_button" and v != s[key])
            new[key] = v
        elif key == "monitor":
            if not isinstance(v, str) or not _MONITOR_RE.fullmatch(v):
                return None, "Pick a monitor from the list."
            new[key] = v
        elif key in ("offset_x", "offset_y"):
            n = _int_in(v, -OFFSET_LIMIT, OFFSET_LIMIT)
            if n is None:
                return None, "Offsets must be whole numbers."
            new[key] = n
        elif key == "hotkeys":
            if not isinstance(v, dict):
                return None, "Send hotkeys as names with their keys."
            for name, val in v.items():
                if name not in overlay.HOTKEY_NAMES:
                    continue
                text, err = _canon_hotkey(val)
                if err:
                    return None, f"{overlay.HOTKEY_LABELS[name]}: {err}"
                if text != s["hotkeys"].get(name):
                    changed_hotkeys.add(name)
                new["hotkeys"][name] = text
    if changed_hotkeys or aim_changed:
        err = _hotkey_conflict(new["hotkeys"], changed_hotkeys,
                               set(overlay.HOTKEY_NAMES) if aim_changed else set(), new["aim_button"])
        if err:
            return None, err
    return new, None


def set_settings(partial=None, **kw) -> dict:
    """Change any of enabled, show_mode, anchor, monitor, offset_x, offset_y,
    hide_on_aim, aim_button, hide_with_cursor, hotkeys (merged per key),
    exclude_from_capture, notice_seen, legacy_notice_seen. Other keys are
    ignored. {ok, settings, status} or {ok: False, err}."""
    global _start_error
    if partial is None:
        partial = {}
    if not isinstance(partial, dict):
        return _err("Expected a JSON object.")
    partial = {**partial, **kw}
    try:
        with _lock:
            s = _load()
            new, err = _apply_partial(s, partial)
            if err:
                return _err(err)
            if new != s and not _save(new):
                return _err("Couldn't save the crosshair settings.")
            _push(new)
        if new["enabled"]:
            if _ui_ready and not overlay.is_running() and (not s["enabled"] or "enabled" in partial):
                _start_overlay()
        else:
            _start_error = None
            if s["enabled"] or overlay.is_running():
                _stop_overlay()
        return {"ok": True, "settings": new, "status": _status_dict(new)}
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception("crosshair: settings not changed")
        return _err("Couldn't change the crosshair settings.")


def _monitor_label(mon: dict) -> str:
    extra = ", main" if mon.get("primary") else ""
    return f"{mon.get('name') or 'Display'} ({mon.get('width')}x{mon.get('height')}{extra})"


def _custom_games() -> list:
    p = os.path.join(game_profiles.PROFILES_DIR, "custom_exes.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def _nice(exe: str) -> str:
    try:
        return game_profiles._nice_name(exe) or exe
    except Exception:
        return exe


def _games_hint(s: dict, st: dict) -> list:
    """Exes for the "add a game" list: custom games, ruled games, the active
    game, the ones seen in front lately, then every other known game."""
    out, seen = [], set()

    def add(exe, name=None):
        exe = exe.strip().lower() if isinstance(exe, str) else ""
        if exe and exe not in seen:
            seen.add(exe)
            out.append({"exe": exe, "name": name if isinstance(name, str) and name.strip() else _nice(exe)})

    for c in _custom_games():
        add(c.get("exe"), c.get("name"))
    for exe in s["games"]:
        add(exe)
    try:
        active = game_profiles.get_active_game_info()
    except Exception:
        active = None
    if active:
        add(active.get("exe"), active.get("display"))
    for exe in st.get("last_games") or []:
        add(exe)
    for exe in sorted(game_profiles.KNOWN_GAME_EXES):
        add(exe)
    return out


def _default_game(rules: dict, st: dict) -> str:
    """The game the fullscreen check looks at first: the game in front, else
    the one Game Mode is running for, else the newest exe seen in front that
    the overlay counts as a game (a known one, one with a rule, or a blocked
    one). Never just the app in front last: that is often a browser or chat."""
    exe = st.get("game_exe") or ""
    if isinstance(exe, str) and exe:
        return exe.lower()
    try:
        info = game_profiles.get_active_game_info()
    except Exception:
        info = None
    if info:
        exe = _norm_exe(info.get("exe"))
        if exe:
            return exe
    for exe in st.get("last_games") or []:
        exe = exe.lower() if isinstance(exe, str) else ""
        if exe and (_is_known_game(exe) or exe in rules or exe in BLOCKED_EXES):
            return exe
    return ""


def default_game() -> str:
    """The game the fullscreen check defaults to, "" when there is none (see
    _default_game). Never raises."""
    try:
        try:
            rules = _load()["games"]
        except _SettingsUnreadable:
            rules = {}                                   # known and blocked games still count
        return _default_game(rules, overlay.get_status())
    except Exception:
        log.debug("crosshair: no default game", exc_info=True)
        return ""


def get_state() -> dict:
    """Everything the Crosshair page needs on load."""
    try:
        s = _load()
        st = _status_dict(s)
        monitors = [dict(mon, label=_monitor_label(mon)) for mon in overlay.list_monitors()]
        legacy = os.path.exists(os.path.join(config.APPDATA_DIR, LEGACY_PROFILES))
        return {
            "ok": True, "settings": s, "presets": _presets_list(s), "status": st, "enabled": s["enabled"],
            "active_preset": s["active_preset"], "preset_in_effect": _preset_in_effect(s, st.get("game_exe") or ""),
            "monitors": monitors, "capabilities": overlay.capabilities(),
            "blocked": [{"exe": exe, "name": BLOCKED_NAMES.get(exe) or _nice(exe), "reason": why}
                        for exe, why in BLOCKED_EXES.items()],
            "games_hint": _games_hint(s, st), "last_game": _default_game(s["games"], st),
            "clipper_hotkeys": _clipper_hotkeys(), "legacy_notice": legacy and not s["legacy_notice_seen"],
        }
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE)
    except Exception:
        log.exception("crosshair: state not read")
        return _err("Couldn't read the crosshair settings.")


def get_status() -> dict:
    """Polled every second by the page: {ok, status, enabled, active_preset, preset_in_effect}.
    {ok: False, err} for a poll that couldn't read the settings file."""
    try:
        s = _load()
        st = _status_dict(s)
        return {"ok": True, "status": st, "enabled": s["enabled"], "active_preset": s["active_preset"],
                "preset_in_effect": _preset_in_effect(s, st.get("game_exe") or "")}
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE)
    except Exception:
        log.exception("crosshair: status not read")
        return _err("Couldn't read the crosshair status.")


def _preview_height() -> int:
    """The height the overlay draws for: its monitor while showing, else the main one."""
    try:
        mon = overlay.get_status().get("monitor")
        if mon and mon.get("height"):
            return int(mon["height"])
        for mon in overlay.list_monitors():
            if mon.get("primary") and mon.get("height"):
                return int(mon["height"])
    except Exception:
        log.debug("crosshair: no monitor height for the preview", exc_info=True)
    return xr.BASE_HEIGHT


def preview(design=None, background="none", target_height=None) -> dict:
    """The design drawn as the overlay would draw it, as a PNG data URI:
    {ok, png, width, height, design, warnings, target_height}."""
    try:
        d = xr.normalize_design(design)
        h = _int_in(target_height, 240, 8640) if target_height is not None else None
        if h is None:
            h = _preview_height()
        img = xr.render(d, h, _load_image)
        notes = [_MISSING_IMAGE_WARNING] if img.info.get("warning") == "image missing" else []
        if background in BACKGROUNDS:
            canvas = Image.new("RGBA", img.size, BACKGROUNDS[background] + (255,))
            canvas.alpha_composite(img)
            img = canvas
        return {"ok": True, "png": xr.to_png_data_uri(img), "width": img.size[0], "height": img.size[1],
                "design": d, "warnings": notes, "target_height": h}
    except Exception:
        log.exception("crosshair: preview failed")
        return _err("Couldn't draw this crosshair.")


def _result(s: dict, **extra) -> dict:
    st = _status_dict(s)
    out = {"ok": True, "settings": s, "status": st,
           "preset_in_effect": _preset_in_effect(s, st.get("game_exe") or "")}
    out.update(extra)
    return out


def _presets(action, kw: dict) -> dict:
    new_id = None
    with _lock:
        s = _load()
        before = _referenced_images(s)
        if action == "save":
            name, err = _valid_name(kw.get("name"))
            if err:
                return _err(err)
            if len(s["user_presets"]) >= MAX_USER_PRESETS:
                return _err(f"You have {MAX_USER_PRESETS} crosshairs saved. Delete one first.")
            design = xr.normalize_design(kw.get("design"))
            err = _image_problem(design)
            if err:
                return _err(err)
            new_id = _new_user_id(s)
            s["user_presets"].append({"id": new_id, "name": name, "design": design})
            if kw.get("activate"):
                _set_in_effect(s, new_id, _game_in_front())
        elif action == "duplicate":
            pid = kw.get("id")
            b = xr.builtin_preset(pid)
            src = b if b is not None else _user(s, pid)
            if src is None:
                return _err("That crosshair doesn't exist anymore.")
            if len(s["user_presets"]) >= MAX_USER_PRESETS:
                return _err(f"You have {MAX_USER_PRESETS} crosshairs saved. Delete one first.")
            new_id = _new_user_id(s)
            name = (src["name"][:MAX_NAME - 5].rstrip() + " copy")
            s["user_presets"].append({"id": new_id, "name": name, "design": copy.deepcopy(src["design"])})
        else:
            pid = kw.get("id")
            p = _user(s, pid)
            if p is None:
                if pid in xr.BUILTIN_IDS:
                    return _err("Built-in crosshairs can't be changed. Use Save as new.")
                return _err("That crosshair doesn't exist anymore.")
            if action == "update":
                design = xr.normalize_design(kw.get("design"))
                err = _image_problem(design)
                if err:
                    return _err(err)
                p["design"] = design
            elif action == "rename":
                name, err = _valid_name(kw.get("name"))
                if err:
                    return _err(err)
                p["name"] = name
            else:                                        # delete
                s["user_presets"].remove(p)
                if s["active_preset"] == pid:
                    s["active_preset"] = DEFAULT_PRESET
                for rule in s["games"].values():
                    if rule.get("preset") == pid:
                        rule["preset"] = None
        if not _save(s):
            return _err("Couldn't save the crosshair settings.")
        _push(s)
        _gc_images(before, s)
    out = {"ok": True, "presets": _presets_list(s), "settings": s}
    if new_id:
        out["id"] = new_id
    return out


def presets(action=None, **kw) -> dict:
    """action "save" {name, design, activate}, "update" {id, design},
    "rename" {id, name}, "duplicate" {id}, "delete" {id}.
    {ok, presets, settings, id?} or {ok: False, err}."""
    if action not in ("save", "update", "rename", "duplicate", "delete"):
        return _err("Vispora doesn't know that preset action.")
    try:
        return _presets(action, kw)
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception(f"crosshair: preset {action} failed")
        return _err("Something went wrong with that crosshair. Try again.")


def activate(preset_id=None, **kw) -> dict:
    """Make preset_id (or id=...) the preset in effect. {ok, settings, status, preset_in_effect}."""
    if preset_id is None:
        preset_id = kw.get("id")
    try:
        with _lock:
            s = _load()
            if preset_id not in _ids(s):
                return _err("That crosshair doesn't exist anymore.")
            _set_in_effect(s, preset_id, _game_in_front())
            if not _save(s):
                return _err("Couldn't save the crosshair settings.")
            _push(s)
        return _result(s)
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception("crosshair: activate failed")
        return _err("Couldn't switch the crosshair.")


def cycle(step=1) -> dict:
    """The next (step 1) or previous (step -1) preset, built-ins then the user's, wrapping."""
    try:
        step = int(step) or 1
    except (TypeError, ValueError):
        step = 1
    try:
        with _lock:
            s = _load()
            ids, exe = _ids(s), _game_in_front()
            cur = _preset_in_effect(s, exe)
            i = ids.index(cur) if cur in ids else 0
            _set_in_effect(s, ids[(i + step) % len(ids)], exe)
            if not _save(s):
                return _err("Couldn't save the crosshair settings.")
            _push(s)
        return _result(s)
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception("crosshair: cycle failed")
        return _err("Couldn't switch the crosshair.")


def import_code(text) -> dict:
    try:
        return codes.import_code(text)
    except Exception:
        log.exception("crosshair: import failed")
        return _err("That code couldn't be read.")


def export_code(design) -> dict:
    try:
        return codes.export_code(design)
    except Exception:
        log.exception("crosshair: export failed")
        return _err("This crosshair couldn't be turned into a code.")


def _write_bytes(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def add_image_from_path(path) -> dict:
    """Copy a PNG (at most 1 MB and 512 x 512) into the crosshair images folder,
    re-encoded as RGBA. {ok, image_id, width, height} or {ok: False, err}."""
    if not isinstance(path, str) or not path.strip():
        return _err("Pick a PNG image.")
    path = path.strip()
    try:
        if not os.path.isfile(path):
            return _err("That file couldn't be found.")
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            return _err("That image file is bigger than 1 MB.")
        with open(path, "rb") as f:
            data = f.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return _err("That file couldn't be opened.")
    if len(data) > MAX_IMAGE_BYTES:
        return _err("That image file is bigger than 1 MB.")
    try:
        img = _open_png(data)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png = buf.getvalue()
        iid = hashlib.sha256(png).hexdigest()[:32]       # the same picture always gets the same id
        os.makedirs(IMAGES_DIR, exist_ok=True)
        _write_bytes(_image_path(iid), png)
        _images.pop(iid, None)
    except _BadImage as e:
        return _err(str(e))
    except Exception:
        log.exception("crosshair: image not added")
        return _err("That image couldn't be saved.")
    return {"ok": True, "image_id": iid, "width": img.size[0], "height": img.size[1]}


def set_game_rule(exe, rule=None) -> dict:
    """Add, change (merged onto the current rule) or, with rule None, remove a
    game's rule {mode, preset, offset_x, offset_y}. {ok, settings}."""
    name = _norm_exe(exe)
    if not name:
        return _err("That isn't a program name Vispora can use. It should end in .exe.")
    if rule is not None and not isinstance(rule, dict):
        return _err("Expected the rule as a JSON object.")
    try:
        with _lock:
            s = _load()
            if rule is None:
                s["games"].pop(name, None)
            else:
                cur = dict(s["games"].get(name) or {"mode": "default", "preset": None,
                                                     "offset_x": None, "offset_y": None})
                for key in ("mode", "preset", "offset_x", "offset_y"):
                    if key in rule:
                        cur[key] = rule[key]
                if cur["mode"] not in RULE_MODES:
                    return _err("Pick Show, Hide or Default for this game.")
                if cur["preset"] in ("", None):
                    cur["preset"] = None
                elif cur["preset"] not in _ids(s):
                    return _err("That crosshair doesn't exist anymore.")
                for key in ("offset_x", "offset_y"):
                    if cur[key] is not None:
                        n = _int_in(cur[key], -OFFSET_LIMIT, OFFSET_LIMIT)
                        if n is None:
                            return _err("Offsets must be whole numbers.")
                        cur[key] = n
                if name not in s["games"] and len(s["games"]) >= MAX_GAMES:
                    return _err(f"You can have up to {MAX_GAMES} game rules. Remove one first.")
                s["games"][name] = cur
            if not _save(s):
                return _err("Couldn't save the crosshair settings.")
            _push(s)
        return {"ok": True, "settings": s}
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception("crosshair: game rule not saved")
        return _err("Couldn't save that game rule.")


def test_show(seconds=5) -> dict:
    """Show the crosshair for 1 to 10 s, even over Vispora or with no game in front."""
    secs = _int_in(seconds, 1, 10) or 5
    try:
        s = _load()
        if not s["enabled"]:
            return _err("Turn the crosshair on first.")
        if not overlay.is_running():
            if not _ui_ready:
                return _err("Vispora is still starting. Try again in a moment.")
            r = _start_overlay()
            if not r.get("ok") or not overlay.is_running():
                return _err(r.get("err") or "The crosshair isn't running.")
        overlay.test_show(secs)
        return {"ok": True, "status": _status_dict(s)}
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE)
    except Exception:
        log.exception("crosshair: test show failed")
        return _err("Couldn't show the crosshair.")


def set_hidden(hidden) -> dict:
    """Hide or show again for now (what the toggle hotkey does). Not saved."""
    if not isinstance(hidden, bool):
        return _err("Expected true or false.")
    try:
        overlay.set_hidden(hidden)
        try:
            st = _status_dict(_load())
        except _SettingsUnreadable:
            st = overlay.get_status()                    # done all the same: it isn't a setting
        return {"ok": True, "status": st}
    except Exception:
        log.exception("crosshair: hide failed")
        return _err("Couldn't change that.")


def dismiss_legacy_notice() -> dict:
    try:
        with _lock:
            s = _load()
            s["legacy_notice_seen"] = True
            if not _save(s):
                return _err("Couldn't save the crosshair settings.")
        return {"ok": True}
    except _SettingsUnreadable:
        return _err(_ERR_UNREADABLE_CHANGE)
    except Exception:
        log.exception("crosshair: notice not dismissed")
        return _err("Couldn't save that.")


# ── fullscreen ────────────────────────────────────────────────────────
_HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

_ADVICE_BORDERLESS = ("Set the game to borderless (or windowed) in its display settings: "
                      "the crosshair always shows there.")
_ADVICE_DX12 = "If this game uses DirectX 12, this setting has no effect. Try borderless instead."
_ADVICE_FLAGS = "Fullscreen optimizations are turned off for this game. Turn them back on below, then restart the game."
_ADVICE_GLOBAL = "A tweak turned fullscreen optimizations off for all games. You can restore Windows' default below."
_ADVICE_GLOBAL_IGNORED = ("GameDVR_FSEBehaviorMode is set to 2, but Windows only follows it when "
                          "GameDVR_HonorUserFSEBehaviorMode is 1, so it probably changes nothing.")
_ADVICE_EXCLUSIVE = "This game is in exclusive fullscreen right now, so the crosshair can't draw over it."
_ADVICE_SEEN_OK = "This game draws through the desktop right now, so the crosshair can show over it."
_ADVICE_NOTHING = ("Nothing on this PC turns fullscreen optimizations off for this game. Some games still use "
                   "exclusive fullscreen on their own: if the crosshair doesn't show, set the game to borderless.")
_ADVICE_PICK = "Pick a game to check."


def _split(key_path: str):
    hive, _, sub = key_path.partition("\\")
    return _HIVES[hive.upper()], sub


def _reg_set(key_path: str, name: str, vtype: str, data) -> bool:
    try:
        hive, sub = _split(key_path)
        with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, getattr(winreg, vtype), data)
        return True
    except OSError as e:
        log.warning(f"crosshair: couldn't write {key_path}\\{name}: {e}")
        return False


def _reg_delete(key_path: str, name: str) -> bool:
    try:
        hive, sub = _split(key_path)
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log.warning(f"crosshair: couldn't delete {key_path}\\{name}: {e}")
        return False


def _dword(key_path: str, name: str):
    cur = reg_originals.read_value(key_path, name)
    if cur and cur.get("type") in ("REG_DWORD", "REG_QWORD"):
        try:
            return int(cur["data"])
        except (TypeError, ValueError):
            return None
    return None


def _has_token(data: str) -> bool:
    return any(t.upper() == FSO_TOKEN for t in data.split())


def _layers_values(exe: str) -> list:
    """[(hive, key, value name, data, type)] for the Layers values naming exe
    (the value name is the program's full path)."""
    out = []
    for hive, key in LAYERS_KEYS:
        for name in reg_originals.value_names(key) or []:
            if not name or ntpath.basename(name).lower() != exe:
                continue
            cur = reg_originals.read_value(key, name)
            if cur and cur.get("type") in ("REG_SZ", "REG_EXPAND_SZ"):
                out.append((hive, key, name, str(cur.get("data") or ""), cur["type"]))
    return out


def _global_optouts() -> list:
    """The GameConfigStore values that turn fullscreen optimizations off for
    every game. "effective" is False for GameDVR_FSEBehaviorMode 2 without
    GameDVR_HonorUserFSEBehaviorMode 1: Windows probably ignores it then, so
    it is listed but not counted against the game."""
    out = []
    fse = _dword(GAME_CONFIG_STORE, "GameDVR_FSEBehaviorMode")
    if fse == 2:
        honor = _dword(GAME_CONFIG_STORE, "GameDVR_HonorUserFSEBehaviorMode")
        if honor == 1:
            meaning = ("Fullscreen optimizations are off for all games "
                       "(GameDVR_HonorUserFSEBehaviorMode is 1, so Windows follows it).")
        else:
            meaning = ("Set to turn fullscreen optimizations off for all games, but Windows only follows it "
                       "when GameDVR_HonorUserFSEBehaviorMode is 1. Here it isn't, so it probably changes nothing.")
        out.append({"name": "GameDVR_FSEBehaviorMode", "value": 2, "meaning": meaning, "honor_user": honor,
                    "effective": honor == 1})
    if _dword(GAME_CONFIG_STORE, "GameDVR_DXGIHonorFSEWindowsCompatible") == 1:
        out.append({"name": "GameDVR_DXGIHonorFSEWindowsCompatible", "value": 1,
                    "meaning": "A tweak tool set this to turn fullscreen optimizations off for all games.",
                    "effective": True})
    return out


def _undo_records():
    """(can_undo, exes whose Layers value a fix changed)."""
    recs = reg_originals.registry_records(UNDO_SCOPE)
    layer_keys = {k.lower() for _, k in LAYERS_KEYS}
    fixed = sorted({ntpath.basename(v).lower() for k, v in recs if k.lower() in layer_keys})
    return bool(recs), fixed


# PresentMon's names for how a game's frames reach the screen. Anything else
# ("Other" is its word for "couldn't tell") is no reading at all.
_PRESENT_MODES = frozenset({
    "Hardware: Legacy Flip", "Hardware: Legacy Copy to front buffer",
    "Hardware: Independent Flip", "Hardware Composed: Independent Flip",
    "Composed: Flip", "Composed: Copy with GPU GDI", "Composed: Copy with CPU GDI",
    "Composed: Composition Atlas",
})


def _present_mode_for(exe: str):
    """{mode, age_s} when PresentMon saw this game's present mode in the last
    10 s and it is one Vispora knows, else None."""
    pm = frame_telemetry.get_present_mode()
    if not pm or not exe or str(pm.get("name") or "").lower() != exe:
        return None
    try:
        age = time.time() - float(pm.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    mode = str(pm.get("mode") or "").strip()
    if 0 <= age < PRESENT_MODE_FRESH_S and mode in _PRESENT_MODES:
        return {"mode": mode, "age_s": round(age, 1)}
    return None


def fullscreen_check(exe=None) -> dict:
    """Can the crosshair show over this game in fullscreen? Reads the per-game
    and global fullscreen-optimization opt-outs and, when PresentMon is
    running for this game, its present mode."""
    try:
        name = _norm_exe(exe) if exe else ""
        if exe and not name:
            return _err("That isn't a program name Vispora can check.")
        flags = []
        if name:
            flags = [{"hive": hive, "path": vname, "data": data}
                     for hive, _, vname, data, _ in _layers_values(name) if _has_token(data)]
        optouts = _global_optouts()
        active = [o for o in optouts if o.get("effective", True)]
        present = _present_mode_for(name)
        if not name:
            verdict = "unknown"
        elif present and present["mode"].startswith("Hardware: Legacy"):
            verdict = "exclusive"
        elif present:
            verdict = "ok"
        elif flags or active:
            verdict = "may_be_exclusive"
        else:
            verdict = "ok"
        advice = []
        if verdict == "unknown":
            advice.append(_ADVICE_PICK)
        elif verdict == "exclusive":
            advice.append(_ADVICE_EXCLUSIVE)
        elif verdict == "ok":
            advice.append(_ADVICE_SEEN_OK if present else _ADVICE_NOTHING)
        if verdict != "ok":
            if flags:
                advice += [_ADVICE_FLAGS, _ADVICE_DX12]
            if active:
                advice.append(_ADVICE_GLOBAL)
        if len(active) < len(optouts):
            advice.append(_ADVICE_GLOBAL_IGNORED)
        if verdict != "ok":
            advice.append(_ADVICE_BORDERLESS)
        can_undo, fixed = _undo_records()
        return {"ok": True, "exe": name, "fso_flags": flags, "global_optouts": optouts, "present_mode": present,
                "verdict": verdict, "advice": advice, "can_undo": can_undo, "fixed_exes": fixed}
    except Exception:
        log.exception("crosshair: fullscreen check failed")
        return _err("Couldn't check this game.")


def fullscreen_fix(exe, paths=None) -> dict:
    """Turn fullscreen optimizations back on for one game: remove
    DISABLEDXMAXIMIZEDWINDOWEDMODE from its Layers values (HKCU and HKLM),
    keeping every other flag in order; a value left with only "~" or nothing
    is deleted. Each change is recorded first, so fullscreen_undo can put it back.
    paths (program paths, any case) limits it to the programs the user was shown."""
    name = _norm_exe(exe)
    if not name:
        return _err("That isn't a program name Vispora can change.")
    only = None
    if isinstance(paths, (list, tuple)) and paths:
        only = {p.lower() for p in paths if isinstance(p, str) and p} or None
    changed, failed = [], []
    try:
        with _lock:
            for hive, key, vname, data, vtype in _layers_values(name):
                if not _has_token(data) or (only is not None and vname.lower() not in only):
                    continue
                rest = [t for t in data.split() if t.upper() != FSO_TOKEN]
                after = " ".join(rest) if rest and rest != ["~"] else None
                if after is None:
                    reg_originals.remember(key, vname, "", reg_originals.ABSENT, scope=UNDO_SCOPE)
                else:
                    reg_originals.remember(key, vname, after, vtype, scope=UNDO_SCOPE)
                item = {"hive": hive, "path": vname, "before": data, "after": after}
                # Never change what couldn't be recorded: it could not be undone.
                if not reg_originals.has_original(key, vname, scope=UNDO_SCOPE):
                    failed.append(item)
                    continue
                ok = _reg_delete(key, vname) if after is None else _reg_set(key, vname, vtype, after)
                (changed if ok else failed).append(item)
    except Exception:
        log.exception("crosshair: fullscreen fix failed")
        return _err("Couldn't change this game's settings.")
    if changed:
        log.info(f"crosshair: fullscreen optimizations back on for {name} ({len(changed)} value(s))")
    out = {"ok": not failed, "exe": name, "changed": changed, "failed": failed,
           "advice": "Restart the game for this to take effect." if changed else
                     "Fullscreen optimizations are already on for this game."}
    if failed:
        out["err"] = ("Windows didn't let Vispora change this game's setting." if not changed else
                      "Some of this game's settings couldn't be changed.")
    return out


def fullscreen_fix_global() -> dict:
    """Restore Windows' default for every game: GameDVR_FSEBehaviorMode 2 -> 0 and
    GameDVR_DXGIHonorFSEWindowsCompatible 1 -> 0, each recorded first."""
    changed, failed = [], []
    try:
        with _lock:
            for vname, bad in (("GameDVR_FSEBehaviorMode", 2), ("GameDVR_DXGIHonorFSEWindowsCompatible", 1)):
                if _dword(GAME_CONFIG_STORE, vname) != bad:
                    continue
                reg_originals.remember(GAME_CONFIG_STORE, vname, "0", "REG_DWORD", scope=UNDO_SCOPE)
                if not reg_originals.has_original(GAME_CONFIG_STORE, vname, scope=UNDO_SCOPE):
                    failed.append(vname)
                    continue
                (changed if _reg_set(GAME_CONFIG_STORE, vname, "REG_DWORD", 0) else failed).append(vname)
    except Exception:
        log.exception("crosshair: global fullscreen fix failed")
        return _err("Couldn't change Windows' fullscreen settings.")
    out = {"ok": not failed, "changed": changed, "failed": failed}
    if failed:
        out["err"] = "Windows didn't let Vispora change every setting."
    return out


def fullscreen_undo() -> dict:
    """Put back everything the fullscreen fixes changed."""
    try:
        with _lock:
            r = reg_originals.restore_all(UNDO_SCOPE)
    except Exception:
        log.exception("crosshair: fullscreen undo failed")
        return _err("Couldn't undo the fullscreen fixes.")
    out = {"ok": not r.get("failed"), "restored": r.get("restored", 0), "failed": r.get("failed", 0)}
    if r.get("failed"):
        out["err"] = "Some settings couldn't be put back. Try again."
    return out
