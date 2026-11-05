import sandbox as m

# The crosshair feature module (core/crosshair.py): its settings file, presets,
# images, game rules, hotkey checks, when the overlay may start, the
# fullscreen check and fix on the fake registry, and the PresentMode reading
# it relies on in core/frame_telemetry.py.  The overlay runs against the
# sandbox's fake Win32 adapter, so no real window is ever opened.

import base64
import io
import json
import os
import re
import struct
import threading
import time
import zlib

from PIL import Image

import config
from core import clipper
from core import crosshair as X
from core import crosshair_overlay as xo
from core import crosshair_render as xr
from core import frame_telemetry as FT
from core import game_profiles, snapshots

LAYERS_CU = r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
LAYERS_LM = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
GCS = r"HKCU\System\GameConfigStore"
CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
CS2_COPY = r"E:\Games\cs2\cs2.exe"
OTHER = r"C:\Games\Other\other.exe"
DISPLAY1 = "\\\\.\\DISPLAY1"
# What core.crosshair found when it was imported: the sandbox's wire() branch
# must already have put a fake adapter into the overlay.
WIRED_AT_IMPORT = type(xo._win)


def test_the_overlay_core_crosshair_drives_is_the_sandboxed_fake():
    assert WIRED_AT_IMPORT is m.FakeOverlayWin
    assert X.overlay is xo


def _fresh():
    m.fresh()
    X.stop()
    fake = m.FakeOverlayWin()
    xo._win = fake
    xo.on_hotkey = None
    xo._caps = None
    xo._cfg = dict(xo._DEFAULT_CFG)
    with xo._lock:
        xo._rt.update(hidden=False, test_until=0.0, exclusive=frozenset())
        del xo._last_games[:]
    X._ui_ready = False
    X._enabled = None
    X._start_error = None
    X._thumbs.clear()
    X._images.clear()
    FT._state["present_mode"] = None
    return fake


def _live(fn):
    """Run fn(fake) on a fresh machine; the overlay is always stopped after."""
    def run():
        fake = _fresh()
        try:
            fn(fake)
        finally:
            X.stop()
            X._ui_ready = False
    run.__name__ = fn.__name__
    return run


def _saved() -> dict:
    with open(X.SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write_settings(d) -> None:
    os.makedirs(os.path.dirname(X.SETTINGS_PATH), exist_ok=True)
    with open(X.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f)


def _on_and_running(fake):
    assert X.set_settings({"enabled": True})["ok"]
    X.allow_start()
    assert m.wait(xo.is_running), "the overlay never started"


def _png(path, size=(16, 16), colour=(255, 0, 0, 255), mode="RGBA", extra=b""):
    Image.new(mode, size, colour if mode == "RGBA" else 1).save(path, "PNG")
    if extra:
        with open(path, "ab") as f:
            f.write(extra)
    return path


def _header_only_png(path, w, h):
    """A PNG whose header claims w x h pixels and that holds next to nothing."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)
    return path


def _decode(uri) -> Image.Image:
    assert uri.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGBA")


def _image_files() -> list:
    try:
        return sorted(os.listdir(X.IMAGES_DIR))
    except FileNotFoundError:
        return []


# ── settings ──────────────────────────────────────────────────────────
def test_defaults_without_a_settings_file():
    _fresh()
    assert not os.path.exists(X.SETTINGS_PATH)
    assert X.is_enabled() is False
    s = X.get_state()["settings"]
    assert s == {
        "version": 1, "enabled": False, "active_preset": "builtin:classic", "user_presets": [],
        "show_mode": "games", "anchor": "game", "monitor": "auto", "offset_x": 0, "offset_y": 0,
        "hide_on_aim": "off", "aim_button": "mouse2", "hide_with_cursor": False,
        "hotkeys": {"toggle": "Ctrl+Alt+X", "next": "", "prev": ""}, "exclude_from_capture": False,
        "games": {}, "notice_seen": False, "legacy_notice_seen": False,
    }
    assert not os.path.exists(X.SETTINGS_PATH)          # reading never writes
    assert os.path.basename(X.SETTINGS_PATH) == "crosshair_settings.json"
    assert X.SETTINGS_PATH.startswith(config.APPDATA_DIR)


def test_settings_round_trip_through_the_file():
    _fresh()
    r = X.set_settings({"show_mode": "list", "anchor": "monitor", "monitor": DISPLAY1, "offset_x": -12,
                        "offset_y": 7, "hide_on_aim": "toggle", "aim_button": "mouse4", "hide_with_cursor": True,
                        "hotkeys": {"next": "ctrl+shift+n"}, "exclude_from_capture": True, "notice_seen": True})
    assert r["ok"], r
    s = r["settings"]
    assert s["hotkeys"] == {"toggle": "Ctrl+Alt+X", "next": "Ctrl+Shift+N", "prev": ""}
    assert (s["show_mode"], s["anchor"], s["monitor"], s["offset_x"], s["offset_y"]) == \
        ("list", "monitor", DISPLAY1, -12, 7)
    assert (s["hide_on_aim"], s["aim_button"], s["hide_with_cursor"], s["exclude_from_capture"]) == \
        ("toggle", "mouse4", True, True)
    assert _saved() == s
    assert X._load() == s
    # The overlay got the same settings.
    assert xo._cfg["offset_x"] == -12 and xo._cfg["anchor"] == "monitor" and xo._cfg["hide_on_aim"] == "toggle"


def test_a_hand_edited_file_is_clamped_and_cleaned():
    _fresh()
    pid = "user:0123abcd"
    _write_settings({
        "version": 9, "enabled": "yes", "active_preset": "user:ffffffff",
        "user_presets": [{"id": pid, "name": "  Mine\n\tnow  ", "design": {"color": "#f00", "length": 999}},
                         {"id": pid, "name": "dup"}, {"id": "user:XYZ", "name": "bad id"}, "junk",
                         {"id": "user:00000001", "name": "", "design": None}],
        "show_mode": "sometimes", "anchor": 3, "monitor": "x" * 80, "offset_x": 9999, "offset_y": "-12.4",
        "hide_on_aim": "hold", "aim_button": "mouse9", "hide_with_cursor": 1,
        "hotkeys": {"toggle": "Q", "next": "ctrl+alt+n", "prev": None, "other": "Ctrl+1"},
        "games": {"CS2.EXE": {"mode": "hide", "preset": pid, "offset_x": 600, "offset_y": "a"},
                  "bad|name.exe": {"mode": "show"}, "notepad": {"mode": "weird", "preset": "builtin:nope"}},
        "notice_seen": True, "extra": 1,
    })
    s = X._load()
    assert s["version"] == 1 and s["enabled"] is False and "extra" not in s
    assert [p["id"] for p in s["user_presets"]] == [pid, "user:00000001"]
    assert s["user_presets"][0]["name"] == "Mine now" and s["user_presets"][1]["name"] == "My crosshair"
    assert s["user_presets"][0]["design"]["color"] == "#FF0000" and s["user_presets"][0]["design"]["length"] == 120
    assert s["user_presets"][1]["design"] == xr.DEFAULT_DESIGN
    assert s["active_preset"] == "builtin:classic"                  # unknown preset id
    assert (s["show_mode"], s["anchor"], s["monitor"], s["offset_x"], s["offset_y"]) == \
        ("games", "game", "auto", 500, -12)
    assert (s["hide_on_aim"], s["aim_button"], s["hide_with_cursor"]) == ("hold", "mouse2", False)
    assert s["hotkeys"] == {"toggle": "Ctrl+Alt+X", "next": "Ctrl+Alt+N", "prev": ""}   # "Q" alone is invalid
    assert s["games"] == {"cs2.exe": {"mode": "hide", "preset": pid, "offset_x": 500, "offset_y": None},
                          "notepad.exe": {"mode": "default", "preset": None, "offset_x": None, "offset_y": None}}
    assert s["notice_seen"] is True


def test_limits_of_100_presets_and_200_game_rules_on_load():
    _fresh()
    _write_settings({"user_presets": [{"id": "user:%08x" % i, "name": f"P{i}"} for i in range(150)],
                     "games": {f"game{i}.exe": {"mode": "show"} for i in range(250)}})
    s = X._load()
    assert len(s["user_presets"]) == 100 and len(s["games"]) == 200


def _set_aside_files() -> list:
    return sorted(n for n in os.listdir(os.path.dirname(X.SETTINGS_PATH))
                  if n.startswith("crosshair_settings.json.corrupt-"))


def test_a_damaged_settings_file_is_set_aside_not_overwritten():
    _fresh()
    os.makedirs(os.path.dirname(X.SETTINGS_PATH), exist_ok=True)
    with open(X.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write('{"user_presets": [')
    s = X._load()
    assert s["user_presets"] == [] and not os.path.exists(X.SETTINGS_PATH)
    assert len(_set_aside_files()) == 1


def test_a_file_that_is_not_a_json_object_is_set_aside_too():
    _fresh()
    for text in ("[]", "null", '"x"', "[" * 100000):
        _write_settings({})
        with open(X.SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        n = len(_set_aside_files())
        assert X._load()["user_presets"] == [], text[:10]
        assert not os.path.exists(X.SETTINGS_PATH) and len(_set_aside_files()) == n + 1, text[:10]


def test_a_settings_file_saved_with_a_bom_is_still_read():
    _fresh()
    pid = "user:0123abcd"
    os.makedirs(os.path.dirname(X.SETTINGS_PATH), exist_ok=True)
    with open(X.SETTINGS_PATH, "w", encoding="utf-8-sig") as f:      # what a hand edit in some editors saves
        json.dump({"enabled": True, "user_presets": [{"id": pid, "name": "Mine"}]}, f)
    s = X._load()
    assert s["enabled"] is True and [p["id"] for p in s["user_presets"]] == [pid]
    assert os.path.exists(X.SETTINGS_PATH) and _set_aside_files() == []


class _held_by_another_program:
    """Opening the settings file fails the way it does on Windows while a virus
    scan or a backup holds it: every time, or only the first `times` times."""

    def __init__(self, times=None):
        self.left, self.opens = times, 0

    def __enter__(self):
        real, target = open, os.path.normcase(os.path.abspath(X.SETTINGS_PATH))

        def fake_open(path, *a, **k):
            if os.path.normcase(os.path.abspath(str(path))) == target:
                self.opens += 1
                if self.left is None or self.left > 0:
                    if self.left is not None:
                        self.left -= 1
                    raise PermissionError(13, "The process cannot access the file because it is being used "
                                              "by another process", str(path))
            return real(path, *a, **k)

        X.open = fake_open                      # shadows the builtin inside core.crosshair only
        self.retry, X._READ_RETRY_S = X._READ_RETRY_S, 0.001
        return self

    def __exit__(self, *exc):
        del X.open
        X._READ_RETRY_S = self.retry
        return False


def _raw_settings() -> bytes:
    with open(X.SETTINGS_PATH, "rb") as f:
        return f.read()


def test_a_settings_file_held_for_a_moment_is_read_again():
    _fresh()
    assert X.set_settings({"enabled": True, "offset_x": 7})["ok"]
    X._enabled = None
    with _held_by_another_program(times=X._READ_TRIES - 1) as held:
        st = X.get_status()
    assert held.opens == X._READ_TRIES
    assert st["ok"] and st["enabled"] is True and X.is_enabled() is True


def test_an_unreadable_settings_file_is_never_replaced_by_the_defaults():
    _fresh()
    iid = X.add_image_from_path(_png(os.path.join(m.ROOT, "mine.png")))["image_id"]
    pid = X.presets("save", name="Mine", design={"kind": "image", "image": {"id": iid}}, activate=True)["id"]
    assert X.set_game_rule("cs2.exe", {"mode": "show", "preset": pid})["ok"]
    assert X.set_settings({"enabled": True})["ok"] and X.is_enabled() is True
    three_days = time.time() - 3 * 24 * 3600
    os.utime(os.path.join(X.IMAGES_DIR, iid + ".png"), (three_days, three_days))
    before = _raw_settings()
    with _held_by_another_program() as held:
        changes = (lambda: X.activate("builtin:dot"), lambda: X.cycle(1), lambda: X.set_settings({"offset_x": 3}),
                   lambda: X.set_enabled(False), lambda: X.presets("save", name="New", design={}),
                   lambda: X.presets("delete", id=pid), lambda: X.set_game_rule("cs2.exe", None),
                   lambda: X.dismiss_legacy_notice())
        for change in changes:
            r = change()
            assert r == {"ok": False, "err": X._ERR_UNREADABLE_CHANGE}, r
        # Readers say so for that poll, and the tray keeps the last good value.
        assert X.get_status() == {"ok": False, "err": X._ERR_UNREADABLE}
        assert X.get_state() == {"ok": False, "err": X._ERR_UNREADABLE}
        assert X.is_enabled() is True
        with xo._lock:
            xo._last_games[:] = ["discord.exe", "cs2.exe"]
        assert X.default_game() == "cs2.exe"                            # known games still count
        assert X.set_hidden(True)["ok"] and X.set_hidden(False)["ok"]  # not a setting: still done
        assert held.opens >= len(changes) * X._READ_TRIES
    assert _raw_settings() == before and _set_aside_files() == []
    assert _image_files() == [iid + ".png"]
    st = X.get_status()
    assert st["ok"] and st["enabled"] is True and st["active_preset"] == pid


def test_a_damaged_file_that_cannot_be_moved_aside_is_not_overwritten():
    _fresh()
    os.makedirs(os.path.dirname(X.SETTINGS_PATH), exist_ok=True)
    with open(X.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write('{"user_presets": [')
    real = X._set_aside
    X._set_aside = lambda: PermissionError(13, "Access is denied")
    retry, X._READ_RETRY_S = X._READ_RETRY_S, 0.001
    try:
        assert X.presets("save", name="New", design={}) == {"ok": False, "err": X._ERR_UNREADABLE_CHANGE}
        assert X.get_status()["ok"] is False and X.is_enabled() is False
    finally:
        X._set_aside = real
        X._READ_RETRY_S = retry
    assert _raw_settings() == b'{"user_presets": ['
    assert X.get_status()["ok"] and _set_aside_files() != []          # moved aside once it could be


def test_a_status_read_cannot_put_an_old_enabled_back_after_a_save():
    _fresh()
    assert X.set_settings({"offset_x": 1})["ok"] and X.is_enabled() is False
    reading, release = threading.Event(), threading.Event()
    real = X._normalize

    def slow_normalize(raw):
        if threading.current_thread().name == "status-poll":
            reading.set()
            release.wait(5)
        return real(raw)

    X._normalize = slow_normalize
    try:
        poll = threading.Thread(target=X.get_status, name="status-poll", daemon=True)
        poll.start()
        assert reading.wait(5)                          # the poll has read the old file (enabled off)
        save = threading.Thread(target=X.set_enabled, args=(True,), name="tray-toggle", daemon=True)
        save.start()
        time.sleep(0.2)
        assert save.is_alive(), "the save waits for the read in flight"
        release.set()
        poll.join(5)
        save.join(5)
        assert not poll.is_alive() and not save.is_alive()
    finally:
        release.set()
        X._normalize = real
    assert _saved()["enabled"] is True and X.is_enabled() is True


def test_set_settings_takes_only_its_own_keys():
    _fresh()
    r = X.set_settings({"active_preset": "builtin:dot", "user_presets": [{"id": "user:00000000", "name": "x"}],
                        "games": {"cs2.exe": {"mode": "hide"}}, "version": 7, "offset_x": 3, "bogus": True})
    assert r["ok"]
    s = _saved()
    assert s["offset_x"] == 3
    assert s["active_preset"] == "builtin:classic" and s["user_presets"] == [] and s["games"] == {}
    assert s["version"] == 1 and "bogus" not in s
    assert X.set_settings("enabled")["ok"] is False                 # not a JSON object
    assert X.set_settings(offset_y=-4)["settings"]["offset_y"] == -4


def test_set_settings_refuses_bad_values_and_saves_nothing():
    _fresh()
    X.set_settings({"offset_x": 5})
    before = _saved()
    for partial in ({"show_mode": "sometimes"}, {"anchor": "desk"}, {"hide_on_aim": True}, {"aim_button": "mouse1"},
                    {"enabled": "true"}, {"hide_with_cursor": 1}, {"offset_x": "left"}, {"offset_y": None},
                    {"monitor": 5}, {"monitor": ""}, {"hotkeys": "Ctrl+Alt+X"}, {"offset_x": 1, "show_mode": "x"}):
        r = X.set_settings(partial)
        assert r["ok"] is False and r["err"].endswith("."), (partial, r)
        assert "\u2014" not in r["err"]
    assert _saved() == before
    assert X.set_settings({"offset_x": 9000})["settings"]["offset_x"] == 500        # numbers are clamped


# ── hotkeys ───────────────────────────────────────────────────────────
def test_hotkeys_are_checked_and_stored_in_canonical_form():
    _fresh()
    r = X.set_settings({"hotkeys": {"toggle": "alt + ctrl + x"}})
    assert r["ok"] and r["settings"]["hotkeys"]["toggle"] == "Ctrl+Alt+X"
    r = X.set_settings({"hotkeys": {"next": "N"}})
    assert r["ok"] is False and r["err"] == "Next crosshair: N needs Ctrl, Alt, Shift or Win with it."
    r = X.set_settings({"hotkeys": {"prev": "Ctrl+Hyper"}})
    assert r["ok"] is False and r["err"].startswith("Previous crosshair: ")
    r = X.set_settings({"hotkeys": {"next": "Num5", "prev": "Ctrl+Shift+F13"}})
    assert r["ok"] and r["settings"]["hotkeys"] == {"toggle": "Ctrl+Alt+X", "next": "Num5", "prev": "Ctrl+Shift+F13"}
    r = X.set_settings({"hotkeys": {"next": "", "prev": None, "unknown": "Ctrl+1"}})
    assert r["ok"] and r["settings"]["hotkeys"] == {"toggle": "Ctrl+Alt+X", "next": "", "prev": ""}
    assert xo._cfg["hotkeys"] == r["settings"]["hotkeys"]


def test_two_crosshair_hotkeys_cannot_be_the_same():
    _fresh()
    r = X.set_settings({"hotkeys": {"next": "ctrl+alt+x"}})
    assert r["ok"] is False and r["err"] == "The Next crosshair and Show or hide hotkeys can't be the same."
    r = X.set_settings({"hotkeys": {"next": "Ctrl+Shift+N", "prev": "Shift+Ctrl+N"}})
    assert r["ok"] is False and "can't be the same" in r["err"]
    assert not os.path.exists(X.SETTINGS_PATH)                     # nothing was saved
    # Moving one off a key and another onto it in the same change is fine.
    assert X.set_settings({"hotkeys": {"next": "Ctrl+Shift+N"}})["ok"]
    r = X.set_settings({"hotkeys": {"next": "Ctrl+Alt+X", "toggle": "Ctrl+Shift+T"}})
    assert r["ok"], r


def test_a_hotkey_on_the_aim_button_is_refused_both_ways():
    _fresh()
    assert X.set_settings({"aim_button": "mouse4"})["ok"]
    for hk in ("Mouse4", "Ctrl+Mouse4"):
        r = X.set_settings({"hotkeys": {"next": hk}})
        assert r["ok"] is False and r["err"] == "Mouse4 is the aim button. Pick another key for Next crosshair."
    assert X.set_settings({"hotkeys": {"next": "Mouse5"}})["ok"]
    r = X.set_settings({"aim_button": "mouse5"})
    assert r["ok"] is False and "Mouse5 is the aim button" in r["err"]
    assert _saved()["aim_button"] == "mouse4"
    assert X.set_settings({"aim_button": "mouse2"})["ok"]          # the right button is never a hotkey


def test_a_hotkey_the_clipper_already_uses_is_refused():
    _fresh()
    clipper._save_settings({**clipper._DEFAULTS, "save_hotkey": "Ctrl+Alt+F10", "screenshot_hotkey": "Alt+Shift+S"})
    r = X.set_settings({"hotkeys": {"toggle": "alt+ctrl+F10"}})
    assert r["ok"] is False and r["err"] == "Ctrl+Alt+F10 is already the hotkey for saving a clip. Pick other keys."
    r = X.set_settings({"hotkeys": {"prev": "Shift+Alt+S"}})
    assert r["ok"] is False and "screenshots" in r["err"]
    assert X.set_settings({"hotkeys": {"prev": "Ctrl+F10"}})["ok"]          # other modifiers: another key
    # A clash saved earlier (the clipper changed later) doesn't block other changes.
    clipper._save_settings({**clipper._DEFAULTS, "save_hotkey": "Ctrl+F10"})
    assert X.set_settings({"offset_x": 2})["ok"]
    assert X.get_state()["clipper_hotkeys"] == ["Ctrl+F10", "F9"]


# ── presets ───────────────────────────────────────────────────────────
def test_presets_save_update_rename_duplicate_and_delete():
    _fresh()
    r = X.presets("save", name="  My \t red ", design={"color": "#f00", "nonsense": 1}, activate=True)
    assert r["ok"], r
    pid = r["id"]
    assert re.fullmatch(r"user:[0-9a-f]{8}", pid)
    assert r["settings"]["active_preset"] == pid
    assert [p["id"] for p in r["presets"]] == list(xr.BUILTIN_IDS) + [pid]
    mine = r["presets"][-1]
    assert mine["name"] == "My red" and mine["builtin"] is False and mine["design"]["color"] == "#FF0000"
    assert "nonsense" not in mine["design"] and _decode(mine["thumb"]).size[0] % 2 == 1
    assert all(p["builtin"] and p["thumb"] for p in r["presets"][:12])

    r = X.presets("update", id=pid, design={"color": "#0f0"})
    assert r["ok"] and X._user(r["settings"], pid)["design"]["color"] == "#00FF00"
    assert xo._cfg["designs"][pid]["color"] == "#00FF00"                # the overlay has it at once
    r = X.presets("update", id="builtin:classic", design={"color": "#0f0"})
    assert r["ok"] is False and "Save as new" in r["err"]

    for bad in ("", "   ", "x" * 41, None):
        assert X.presets("rename", id=pid, name=bad)["ok"] is False
    assert X._user(X.presets("rename", id=pid, name="Green")["settings"], pid)["name"] == "Green"

    r = X.presets("duplicate", id="builtin:classic")
    assert r["ok"] and X._user(r["settings"], r["id"])["name"] == "Classic copy"
    assert X._user(r["settings"], r["id"])["design"] == xr.builtin_preset("builtin:classic")["design"]
    r = X.presets("duplicate", id=pid)
    assert r["ok"] and X._user(r["settings"], r["id"])["name"] == "Green copy"
    assert r["settings"]["active_preset"] == pid                       # duplicating doesn't switch

    assert X.set_game_rule("cs2.exe", {"mode": "show", "preset": pid})["ok"]
    r = X.presets("delete", id=pid)
    assert r["ok"] and X._user(r["settings"], pid) is None
    assert r["settings"]["active_preset"] == "builtin:classic"         # was in effect
    assert r["settings"]["games"]["cs2.exe"] == {"mode": "show", "preset": None, "offset_x": None, "offset_y": None}

    assert X.presets("delete", id="builtin:dot")["ok"] is False
    assert X.presets("delete", id=pid)["err"] == "That crosshair doesn't exist anymore."
    assert X.presets("explode", id=pid)["ok"] is False
    assert X.presets("save", name="No design")["ok"]                    # a missing design is the default


def test_no_more_than_100_presets():
    _fresh()
    _write_settings({"user_presets": [{"id": "user:%08x" % i, "name": f"P{i}"} for i in range(100)]})
    assert X.presets("save", name="One more", design={})["err"] == "You have 100 crosshairs saved. Delete one first."
    assert X.presets("duplicate", id="builtin:dot")["ok"] is False
    assert len(_saved()["user_presets"]) == 100


def test_thumbnails_are_cached_by_design_and_kept_small():
    _fresh()
    big = X.presets("save", name="Big", design={"shape": "circle", "ring_radius": 120, "ring_thickness": 24})
    assert big["ok"]
    assert max(_decode(big["presets"][-1]["thumb"]).size) <= 128
    calls = []
    real = xr.render
    xr.render = lambda *a, **k: calls.append(a) or real(*a, **k)
    try:
        X.get_state()
        assert calls == []                                              # all cached already
        X.presets("save", name="New", design={"length": 33})
        assert len(calls) == 1
    finally:
        xr.render = real


# ── activate and cycle ────────────────────────────────────────────────
def test_activate_and_cycle_change_the_active_preset_with_no_game_in_front():
    _fresh()
    r = X.activate("builtin:ring")
    assert r["ok"] and r["settings"]["active_preset"] == "builtin:ring" and r["preset_in_effect"] == "builtin:ring"
    assert xo._cfg["active_preset"] == "builtin:ring" and "builtin:ring" in xo._cfg["designs"]
    assert X.activate(id="builtin:dot")["settings"]["active_preset"] == "builtin:dot"
    assert X.activate("user:00000000")["ok"] is False
    assert X.cycle(1)["settings"]["active_preset"] == "builtin:cross-dot"
    assert X.cycle(-1)["settings"]["active_preset"] == "builtin:dot"
    X.activate("builtin:diagonal")
    assert X.cycle(1)["settings"]["active_preset"] == "builtin:classic"           # wraps, no user presets
    assert X.cycle(-1)["settings"]["active_preset"] == "builtin:diagonal"
    a = X.presets("save", name="A", design={})["id"]
    b = X.presets("save", name="B", design={})["id"]
    assert X.cycle(1)["settings"]["active_preset"] == a                           # built-ins, then mine
    assert X.cycle(1)["settings"]["active_preset"] == b
    assert X.cycle(1)["settings"]["active_preset"] == "builtin:classic"
    assert X.cycle(-1)["settings"]["active_preset"] == b


@_live
def test_activate_and_cycle_change_the_rule_preset_of_the_game_in_front(fake):
    _on_and_running(fake)
    assert xo.on_hotkey is X._on_hotkey
    assert X.set_game_rule("cs2.exe", {"mode": "show", "preset": "builtin:dot"})["ok"]
    assert X.set_game_rule("valorant.exe", {"mode": "show"})["ok"]                # a rule without a preset
    fake.add_window("cs2.exe")
    assert m.wait(lambda: X.get_status()["status"]["game_exe"] == "cs2.exe")
    st = X.get_status()
    assert st["preset_in_effect"] == "builtin:dot" and st["active_preset"] == "builtin:classic"
    r = X.activate("builtin:ring")
    assert r["settings"]["games"]["cs2.exe"]["preset"] == "builtin:ring"
    assert r["settings"]["active_preset"] == "builtin:classic" and r["preset_in_effect"] == "builtin:ring"
    assert m.wait(lambda: xo.get_status()["preset"] == "builtin:ring")
    X._on_hotkey("next")                                                          # what the overlay calls
    assert _saved()["games"]["cs2.exe"]["preset"] == "builtin:ring-dot"
    X._on_hotkey("prev")
    X._on_hotkey("prev")
    assert _saved()["games"]["cs2.exe"]["preset"] == "builtin:t-shape"
    assert _saved()["active_preset"] == "builtin:classic"
    # A game whose rule has no preset of its own: the active preset changes.
    fake.add_window("valorant.exe", pid=5151)
    assert m.wait(lambda: X.get_status()["status"]["game_exe"] == "valorant.exe")
    assert X.cycle(1)["settings"]["active_preset"] == "builtin:dot"
    assert _saved()["games"]["cs2.exe"]["preset"] == "builtin:t-shape"


# ── game rules ────────────────────────────────────────────────────────
def test_game_rules_are_checked_merged_and_removed():
    _fresh()
    r = X.set_game_rule("CS2.EXE", {"mode": "hide"})
    assert r["ok"] and r["settings"]["games"] == {"cs2.exe": {"mode": "hide", "preset": None,
                                                              "offset_x": None, "offset_y": None}}
    assert X.set_game_rule(r"C:\Games\Foo\Foo.exe", {"mode": "show"})["settings"]["games"]["foo.exe"]["mode"] == "show"
    assert "valorant.exe" in X.set_game_rule("valorant", {"mode": "show"})["settings"]["games"]
    r = X.set_game_rule("cs2.exe", {"offset_x": "12", "offset_y": -9999, "preset": "builtin:box"})
    assert r["settings"]["games"]["cs2.exe"] == {"mode": "hide", "preset": "builtin:box",
                                                 "offset_x": 12, "offset_y": -500}
    assert X.set_game_rule("cs2.exe", {"preset": "", "offset_x": None})["settings"]["games"]["cs2.exe"] == \
        {"mode": "hide", "preset": None, "offset_x": None, "offset_y": -500}
    for exe, rule in (("bad|name.exe", {"mode": "show"}), ("", {"mode": "show"}), (None, {}), ("what?.exe", {}),
                      ("cs2.exe", {"mode": "sometimes"}), ("cs2.exe", {"preset": "user:deadbeef"}),
                      ("cs2.exe", {"offset_x": "left"}), ("cs2.exe", "show")):
        r = X.set_game_rule(exe, rule)
        assert r["ok"] is False and r["err"].endswith("."), (exe, rule, r)
    assert X.set_game_rule("cs2.exe", None)["ok"]
    assert "cs2.exe" not in _saved()["games"]
    assert X.set_game_rule("never.exe", None)["ok"]                   # removing a missing rule is fine
    # The overlay gets the rules and the designs they use.
    X.set_game_rule("valorant.exe", {"mode": "show", "preset": "builtin:chevron"})
    assert xo._cfg["game_rules"]["valorant.exe"]["preset"] == "builtin:chevron"
    assert set(xo._cfg["designs"]) == {"builtin:classic", "builtin:chevron"}


def test_no_more_than_200_game_rules():
    _fresh()
    _write_settings({"games": {f"game{i}.exe": {"mode": "show"} for i in range(200)}})
    assert X.set_game_rule("one-more.exe", {"mode": "show"})["err"] == \
        "You can have up to 200 game rules. Remove one first."
    assert X.set_game_rule("game7.exe", {"mode": "hide"})["ok"]


def test_the_runtime_config_handed_to_the_overlay():
    _fresh()
    path = _png(os.path.join(m.ROOT, "pic.png"), size=(9, 7))
    iid = X.add_image_from_path(path)["image_id"]
    pid = X.presets("save", name="Pic", design={"kind": "image", "image": {"id": iid}})["id"]
    X.set_game_rule("cs2.exe", {"mode": "show", "preset": pid})
    X.set_settings({"offset_x": 4, "hotkeys": {"next": "Ctrl+Alt+N"}})
    cfg = xo._cfg
    assert set(cfg) == set(xo._DEFAULT_CFG)
    assert cfg["enabled"] is False and cfg["active_preset"] == "builtin:classic"
    assert set(cfg["designs"]) == {"builtin:classic", pid}
    assert isinstance(cfg["images"][iid], Image.Image) and cfg["images"][iid].size == (9, 7)
    assert cfg["render"] is xr.render and cfg["offset_x"] == 4
    assert cfg["hotkeys"] == {"toggle": "Ctrl+Alt+X", "next": "Ctrl+Alt+N", "prev": ""}
    assert cfg["blocked_exes"] == frozenset({"escapefromtarkov.exe", "sotgame.exe"})
    assert cfg["own_exes"] == frozenset(config.OWN_PROCESS_NAMES)
    assert cfg["is_known_game"]("cs2.exe") and not cfg["is_known_game"]("notepad.exe")
    # An unchanged image is the same object every time, so the overlay doesn't redraw for nothing.
    X.set_settings({"offset_x": 5})
    assert xo._cfg["images"][iid] is cfg["images"][iid]


# ── images ────────────────────────────────────────────────────────────
def test_a_png_is_copied_in_as_rgba_under_a_content_id():
    _fresh()
    path = os.path.join(m.ROOT, "pal.png")
    Image.new("P", (20, 10), 3).save(path, "PNG")
    r = X.add_image_from_path(path)
    assert r["ok"] and (r["width"], r["height"]) == (20, 10)
    assert re.fullmatch(r"[0-9a-f]{32}", r["image_id"])
    assert _image_files() == [r["image_id"] + ".png"]
    assert os.path.dirname(X.IMAGES_DIR) == config.APPDATA_DIR and os.path.basename(X.IMAGES_DIR) == "crosshairs"
    with Image.open(os.path.join(X.IMAGES_DIR, r["image_id"] + ".png")) as im:
        assert im.mode == "RGBA" and im.size == (20, 10)
    assert X.add_image_from_path(path)["image_id"] == r["image_id"]      # the same picture again
    img = X._load_image(r["image_id"])
    assert img.mode == "RGBA" and X._load_image(r["image_id"]) is img    # cached while unchanged


def test_images_that_are_not_used():
    _fresh()
    d = m.ROOT
    jpg = os.path.join(d, "photo.png")
    Image.new("RGB", (8, 8)).save(jpg, "JPEG")
    text = os.path.join(d, "notes.png")
    with open(text, "w") as f:
        f.write("hello")
    cases = {
        jpg: "That file isn't a PNG image.",
        text: "That file isn't a PNG image.",
        _png(os.path.join(d, "huge.png"), size=(4, 4), extra=b"\0" * (1024 * 1024)): "That image file is bigger than 1 MB.",
        _png(os.path.join(d, "wide.png"), size=(513, 10)): X._IMAGE_TOO_BIG,
        _header_only_png(os.path.join(d, "bomb.png"), 30000, 30000): X._IMAGE_TOO_BIG,
        os.path.join(d, "missing.png"): "That file couldn't be found.",
    }
    for path, err in cases.items():
        r = X.add_image_from_path(path)
        assert r == {"ok": False, "err": err}, (path, r)
    assert X.add_image_from_path(None)["ok"] is False and X.add_image_from_path("  ")["ok"] is False
    broken = os.path.join(d, "broken.png")
    with open(broken, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
    assert X.add_image_from_path(broken) == {"ok": False, "err": "That PNG image couldn't be read."}
    assert _image_files() == []


def test_pillows_decompression_bomb_guard_is_caught():
    _fresh()
    path = _header_only_png(os.path.join(m.ROOT, "bomb2.png"), 30000, 30000)
    assert Image.MAX_IMAGE_PIXELS == 1024 * 1024 * 1024 // 4 // 3                 # left at Pillow's default
    side = X.MAX_IMAGE_SIDE
    X.MAX_IMAGE_SIDE = 100000                   # past the header check: Pillow's own guard must catch it
    try:
        assert X.add_image_from_path(path) == {"ok": False, "err": X._IMAGE_TOO_BIG}
    finally:
        X.MAX_IMAGE_SIDE = side
    assert _image_files() == []


def test_only_well_formed_image_ids_are_read():
    _fresh()
    iid = X.add_image_from_path(_png(os.path.join(m.ROOT, "ok.png")))["image_id"]
    with open(os.path.join(m.ROOT, "secret.png"), "wb") as f:
        f.write(b"x")
    for bad in ("../secret", "..\\..\\secret", iid.upper(), iid[:31], iid + "0", "", None, 5):
        assert X._load_image(bad) is None, bad
    assert X._load_image(iid) is not None
    # A design with a bad id is normalized to no image, and saving an image design needs a saved image.
    assert X.presets("save", name="X", design={"kind": "image", "image": {"id": "../x"}})["err"] == "Pick an image first."
    assert X.presets("save", name="X", design={"kind": "image", "image": {"id": "a" * 32}})["err"] == \
        "That image is missing. Pick it again."


def test_images_no_preset_uses_are_deleted():
    _fresh()
    iid = X.add_image_from_path(_png(os.path.join(m.ROOT, "a.png")))["image_id"]
    a = X.presets("save", name="A", design={"kind": "image", "image": {"id": iid}})["id"]
    b = X.presets("duplicate", id=a)["id"]
    assert X.presets("delete", id=a)["ok"]
    assert _image_files() == [iid + ".png"]                                  # B still uses it
    assert X.presets("update", id=b, design={"kind": "shape"})["ok"]
    assert _image_files() == []
    assert X._load_image(iid) is None
    # One picked but never saved in a preset is left alone, however old: only a
    # change to the presets that used an image deletes it.
    fresh_id = X.add_image_from_path(_png(os.path.join(m.ROOT, "b.png"), colour=(0, 0, 255, 255)))["image_id"]
    old_id = X.add_image_from_path(_png(os.path.join(m.ROOT, "c.png"), colour=(0, 255, 0, 255)))["image_id"]
    two_days = time.time() - 2 * 24 * 3600
    os.utime(os.path.join(X.IMAGES_DIR, old_id + ".png"), (two_days, two_days))
    X.presets("save", name="C", design={})
    assert _image_files() == sorted([fresh_id + ".png", old_id + ".png"])


def test_images_a_damaged_settings_file_names_are_kept():
    _fresh()
    iid = X.add_image_from_path(_png(os.path.join(m.ROOT, "mine.png")))["image_id"]
    assert X.presets("save", name="Mine", design={"kind": "image", "image": {"id": iid}})["ok"]
    three_days = time.time() - 3 * 24 * 3600
    os.utime(os.path.join(X.IMAGES_DIR, iid + ".png"), (three_days, three_days))
    with open(X.SETTINGS_PATH, "a", encoding="utf-8") as f:
        f.write("}")                                                  # a stray character: damaged
    assert X.get_state()["settings"]["user_presets"] == []           # the poll sets it aside
    corrupt = _set_aside_files()
    assert len(corrupt) == 1
    assert X.presets("save", name="New one", design={"shape": "dot"})["ok"]
    assert _image_files() == [iid + ".png"], "the set-aside file still points at it"
    # The same picture picked again gets the same id: a preset using it and then
    # deleted doesn't take the image the set-aside file needs.
    assert X.add_image_from_path(os.path.join(m.ROOT, "mine.png"))["image_id"] == iid
    again = X.presets("save", name="Again", design={"kind": "image", "image": {"id": iid}})["id"]
    assert X.presets("delete", id=again)["ok"]
    assert _image_files() == [iid + ".png"]
    # A set-aside file older than 30 days no longer holds its images.
    folder = os.path.dirname(X.SETTINGS_PATH)
    old = "crosshair_settings.json.corrupt-%d" % int((time.time() - 31 * 24 * 3600) * 1000)
    os.replace(os.path.join(folder, corrupt[0]), os.path.join(folder, old))
    again = X.presets("save", name="Again", design={"kind": "image", "image": {"id": iid}})["id"]
    assert X.presets("delete", id=again)["ok"]
    assert _image_files() == []
    assert _set_aside_files() == [old]                                # the file itself is never deleted


def test_no_image_is_deleted_while_a_set_aside_file_cannot_be_read():
    _fresh()
    iid = X.add_image_from_path(_png(os.path.join(m.ROOT, "mine.png")))["image_id"]
    pid = X.presets("save", name="Mine", design={"kind": "image", "image": {"id": iid}})["id"]
    unreadable = "crosshair_settings.json.corrupt-%d" % int(time.time() * 1000)     # a folder: can't be opened
    os.makedirs(os.path.join(os.path.dirname(X.SETTINGS_PATH), unreadable))
    assert X.presets("delete", id=pid)["ok"]
    assert _image_files() == [iid + ".png"]


# ── preview ───────────────────────────────────────────────────────────
def test_preview_draws_at_the_monitor_height_on_the_chosen_background():
    _fresh()
    r = X.preview(xr.DEFAULT_DESIGN, "none")
    assert r["ok"] and r["target_height"] == 1080 and r["warnings"] == []
    img = _decode(r["png"])
    assert img.size == (r["width"], r["height"]) and r["width"] % 2 == 1 and r["height"] % 2 == 1
    assert img.getpixel((0, 0))[3] == 0
    assert img.getpixel((r["width"] // 2 + 4, r["height"] // 2))[:3] == (0, 255, 255)   # an arm, cyan
    small = r["width"]
    xo._win.modes[DISPLAY1] = (3840, 2160)
    r = X.preview(xr.DEFAULT_DESIGN, "none")
    assert r["target_height"] == 2160 and r["width"] > small
    assert X.preview(xr.DEFAULT_DESIGN, "none", 1080)["width"] == small
    assert X.preview(xr.DEFAULT_DESIGN, "none", "junk")["target_height"] == 2160
    assert _decode(X.preview({}, "dark", 1080)["png"]).getpixel((0, 0)) == (22, 24, 29, 255)
    assert _decode(X.preview({}, "light", 1080)["png"]).getpixel((0, 0)) == (236, 238, 241, 255)
    assert _decode(X.preview({}, "purple", 1080)["png"]).getpixel((0, 0))[3] == 0
    r = X.preview({"kind": "image", "image": {"id": "b" * 32}}, "none", 1080)
    assert r["ok"] and r["warnings"] == [X._MISSING_IMAGE_WARNING]
    r = X.preview("not a design")
    assert r["ok"] and r["design"] == xr.DEFAULT_DESIGN


# ── starting, stopping, test show, hide ───────────────────────────────
@_live
def test_the_overlay_waits_for_the_app_before_it_starts(fake):
    r = X.set_settings({"enabled": True})
    assert r["ok"] and r["status"]["reason"] == "waiting_for_app"
    assert X.is_enabled() is True
    assert not xo.is_running() and fake.calls_to("CreateWindowExW") == []
    st = X.get_status()
    assert st["ok"] and st["enabled"] is True and st["status"]["reason"] == "waiting_for_app"
    assert X.test_show(5)["ok"] is False
    X.allow_start()
    assert m.wait(xo.is_running)
    assert m.wait(lambda: X._watch_thread is not None and X._watch_thread.is_alive())
    assert X._watch_thread.name == "crosshair-fs-watch"
    assert X.get_status()["status"]["running"] is True
    assert xo._cfg["enabled"] is True
    watch = X._watch_thread
    r = X.set_enabled(False)
    assert r["ok"] and r["status"]["reason"] == "disabled"
    assert not xo.is_running() and not watch.is_alive() and X._watch_thread is None
    assert X.is_enabled() is False and _saved()["enabled"] is False
    assert X.set_enabled(True)["ok"] and m.wait(xo.is_running)     # the app is ready now: starts at once
    assert X.stop() == {"ok": True}
    assert not xo.is_running() and _saved()["enabled"] is True     # quitting doesn't turn it off


@_live
def test_allow_start_with_the_crosshair_off_opens_nothing(fake):
    X.allow_start()
    time.sleep(0.2)
    assert not xo.is_running() and fake.calls_to("CreateWindowExW") == []
    assert X.get_status()["status"]["reason"] == "disabled"


@_live
def test_a_start_that_fails_is_reported(fake):
    fake.window_ok = False
    X.allow_start()
    r = X.set_settings({"enabled": True})
    assert r["ok"]
    assert m.wait(lambda: X.get_status()["status"]["reason"] == "error")
    assert X.get_status()["status"]["last_error"]
    X.set_enabled(False)
    assert X.get_status()["status"]["reason"] == "disabled"


@_live
def test_test_show_and_hide(fake):
    assert X.test_show(5) == {"ok": False, "err": "Turn the crosshair on first."}
    _on_and_running(fake)
    t0 = time.monotonic()
    r = X.test_show(5)
    assert r["ok"] and 4.5 < xo._rt["test_until"] - t0 < 5.5
    X.test_show(99)
    assert xo._rt["test_until"] - time.monotonic() <= 10.01
    X.test_show("soon")
    assert 4.5 < xo._rt["test_until"] - time.monotonic() <= 5.01
    assert X.set_hidden(True)["ok"] and xo._rt["hidden"] is True
    assert m.wait(lambda: X.get_status()["status"]["reason"] == "hidden_by_hotkey")
    assert X.set_hidden(False)["ok"] and xo._rt["hidden"] is False
    assert X.set_hidden("yes")["ok"] is False


@_live
def test_the_watch_thread_reports_a_game_in_exclusive_fullscreen(fake):
    every = X._WATCH_EVERY_S
    X._WATCH_EVERY_S = 0.05
    try:
        FT._state["present_mode"] = {"pid": 1, "name": "cs2.exe", "mode": "Hardware: Legacy Flip", "ts": time.time()}
        _on_and_running(fake)
        assert m.wait(lambda: xo._rt["exclusive"] == frozenset({"cs2.exe"}))
        FT._state["present_mode"] = {"pid": 1, "name": "cs2.exe", "mode": "Hardware: Independent Flip",
                                     "ts": time.time()}
        assert m.wait(lambda: xo._rt["exclusive"] == frozenset())
        FT._state["present_mode"] = {"pid": 1, "name": "cs2.exe", "mode": "Hardware: Legacy Copy to front buffer",
                                     "ts": time.time()}
        assert m.wait(lambda: xo._rt["exclusive"] == frozenset({"cs2.exe"}))
        FT._state["present_mode"] = {"pid": 1, "name": "cs2.exe", "mode": "Hardware: Legacy Flip",
                                     "ts": time.time() - 30}                         # stale
        assert m.wait(lambda: xo._rt["exclusive"] == frozenset())
        FT._state["present_mode"] = {"pid": 1, "name": "cs2.exe", "mode": "Hardware: Legacy Flip", "ts": time.time()}
        assert m.wait(lambda: xo._rt["exclusive"] == frozenset({"cs2.exe"}))
        X.set_enabled(False)
        assert xo._rt["exclusive"] == frozenset()
        assert not any(t.name == "crosshair-fs-watch" and t.is_alive() for t in threading.enumerate())
    finally:
        X._WATCH_EVERY_S = every


# ── state ─────────────────────────────────────────────────────────────
def test_get_state_has_everything_the_page_needs():
    _fresh()
    os.makedirs(game_profiles.PROFILES_DIR, exist_ok=True)
    with open(os.path.join(game_profiles.PROFILES_DIR, "custom_exes.json"), "w", encoding="utf-8") as f:
        json.dump([{"exe": "mygame.exe", "name": "My Game"}, "junk"], f)
    X.set_game_rule("zz-ruled.exe", {"mode": "show"})
    with xo._lock:
        xo._last_games[:] = ["somegame.exe", "cs2.exe"]
    st = X.get_state()
    assert st["ok"]
    for key in ("settings", "presets", "status", "enabled", "monitors", "capabilities", "blocked", "games_hint",
                "last_game", "clipper_hotkeys", "legacy_notice", "preset_in_effect", "active_preset"):
        assert key in st, key
    assert st["monitors"] == [{"id": DISPLAY1, "name": "Display 1", "width": 1920, "height": 1080, "primary": True,
                               "left": 0, "top": 0, "label": "Display 1 (1920x1080, main)"}]
    assert st["capabilities"] == {"capture_exclusion": True, "high_res_timer": True}
    assert st["blocked"] == [
        {"exe": "escapefromtarkov.exe", "name": "Escape from Tarkov",
         "reason": "Reported not to start while overlay apps are running."},
        {"exe": "sotgame.exe", "name": "Sea of Thieves",
         "reason": "Players have reported being flagged for crosshair overlays."}]
    assert all(b["reason"].endswith(".") and "known" not in b["reason"].lower() for b in st["blocked"])
    hint = [g["exe"] for g in st["games_hint"]]
    assert hint[:4] == ["mygame.exe", "zz-ruled.exe", "somegame.exe", "cs2.exe"]
    assert st["games_hint"][0]["name"] == "My Game"
    assert len(hint) == len(set(hint)) and set(game_profiles.KNOWN_GAME_EXES) <= set(hint)
    assert st["last_game"] == "cs2.exe"                  # somegame.exe was in front later, but isn't a game
    assert st["clipper_hotkeys"] == ["F10", "F9"]
    assert st["status"]["reason"] == "disabled" and st["preset_in_effect"] == "builtin:classic"
    assert len(st["presets"]) == 12


class _game_mode_running:
    """Game Mode tracking a game for the duration of a with block."""

    KEYS = ("gaming_mode", "active_pid", "active_exe", "active_game")

    def __init__(self, exe, name):
        self.exe, self.name = exe, name

    def __enter__(self):
        self.saved = {k: game_profiles._engine.get(k) for k in self.KEYS}
        game_profiles._engine.update(gaming_mode=True, active_pid=4242, active_exe=self.exe, active_game=self.name)

    def __exit__(self, *exc):
        game_profiles._engine.update(self.saved)
        return False


def _seen_in_front(*exes):
    with xo._lock:
        xo._last_games[:] = list(exes)


def test_the_default_game_is_a_game_not_the_last_app_in_front():
    _fresh()
    assert X.default_game() == "" and X.get_state()["last_game"] == ""
    _seen_in_front("discord.exe", "chrome.exe")
    assert X.default_game() == "" and X.get_state()["last_game"] == ""
    _seen_in_front("discord.exe", "cs2.exe", "r5apex.exe")
    assert X.default_game() == "cs2.exe" and X.get_state()["last_game"] == "cs2.exe"
    assert X.set_game_rule("mygame.exe", {"mode": "hide"})["ok"]
    _seen_in_front("chrome.exe", "mygame.exe", "cs2.exe")
    assert X.default_game() == "mygame.exe", "a game with a rule counts"
    _seen_in_front("steamwebhelper.exe", "sotgame.exe", "cs2.exe")
    assert X.default_game() == "sotgame.exe", "so does a blocked game"
    with _game_mode_running("r5apex.exe", "Apex Legends"):
        assert X.default_game() == "r5apex.exe" and X.get_state()["last_game"] == "r5apex.exe"
    _seen_in_front()


@_live
def test_the_default_game_is_the_game_in_front_first(fake):
    _on_and_running(fake)
    fake.add_window("cs2.exe")
    assert m.wait(lambda: X.get_status()["status"]["game_exe"] == "cs2.exe")
    with _game_mode_running("r5apex.exe", "Apex Legends"):
        assert X.default_game() == "cs2.exe" and X.get_state()["last_game"] == "cs2.exe"


def test_the_legacy_notice_shows_until_dismissed():
    _fresh()
    assert X.get_state()["legacy_notice"] is False
    old = os.path.join(config.APPDATA_DIR, "gamepad_profiles.json")
    with open(old, "w", encoding="utf-8") as f:
        f.write("{}")
    assert X.get_state()["legacy_notice"] is True
    assert X.dismiss_legacy_notice() == {"ok": True}
    assert X.get_state()["legacy_notice"] is False and _saved()["legacy_notice_seen"] is True
    assert os.path.exists(old)                                       # the old profiles are left alone


def test_codes_go_through_the_codes_module():
    _fresh()
    out = X.export_code({"color": "#ff00ff", "length": 9})
    assert out["ok"] and out["code"].startswith("VXH1-")
    back = X.import_code(out["code"])
    assert back["ok"] and back["design"]["color"] == "#FF00FF" and back["design"]["length"] == 9
    assert X.import_code(None)["ok"] is False and X.import_code("x" * 10000)["ok"] is False


# ── fullscreen ────────────────────────────────────────────────────────
def _seed_layers():
    m.rset(LAYERS_CU, CS2, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE HIGHDPIAWARE")
    m.rset(LAYERS_LM, CS2, "disabledxmaximizedwindowedmode")
    m.rset(LAYERS_CU, CS2_COPY, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    m.rset(LAYERS_LM, CS2_COPY, "HIGHDPIAWARE DISABLEDXMAXIMIZEDWINDOWEDMODE RUNASADMIN")
    m.rset(LAYERS_CU, OTHER, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    m.rset(LAYERS_CU, r"C:\Games\cs2.exe.bak", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    m.rset(LAYERS_CU, r"C:\Old\CS2.EXE", "~ RUNASADMIN")


def test_fullscreen_check_reads_the_per_game_flags():
    _fresh()
    _seed_layers()
    r = X.fullscreen_check("CS2.exe")
    assert r["ok"] and r["exe"] == "cs2.exe" and r["verdict"] == "may_be_exclusive"
    assert sorted((f["hive"], f["path"]) for f in r["fso_flags"]) == \
        [("HKCU", CS2), ("HKCU", CS2_COPY), ("HKLM", CS2), ("HKLM", CS2_COPY)]
    assert r["global_optouts"] == [] and r["present_mode"] is None
    assert "If this game uses DirectX 12, this setting has no effect. Try borderless instead." in r["advice"]
    assert any("borderless" in a for a in r["advice"]) and r["can_undo"] is False and r["fixed_exes"] == []
    r = X.fullscreen_check(r"D:\Some\Path\nothing.exe")
    assert r["verdict"] == "ok" and r["fso_flags"] == [] and r["advice"] == [X._ADVICE_NOTHING]
    r = X.fullscreen_check("")
    assert r["ok"] and r["verdict"] == "unknown" and r["exe"] == "" and X._ADVICE_PICK in r["advice"]
    assert X.fullscreen_check("bad|name.exe")["ok"] is False
    assert all("\u2014" not in a for a in X.fullscreen_check("cs2.exe")["advice"])


def test_fullscreen_check_reads_the_global_opt_outs():
    _fresh()
    m.rset(GCS, "GameDVR_FSEBehaviorMode", 2)
    m.rset(GCS, "GameDVR_HonorUserFSEBehaviorMode", 1)
    m.rset(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    r = X.fullscreen_check("nothing.exe")
    assert r["verdict"] == "may_be_exclusive" and X._ADVICE_GLOBAL in r["advice"]
    assert [o["name"] for o in r["global_optouts"]] == ["GameDVR_FSEBehaviorMode", "GameDVR_DXGIHonorFSEWindowsCompatible"]
    assert "GameDVR_HonorUserFSEBehaviorMode is 1" in r["global_optouts"][0]["meaning"]
    assert [o["effective"] for o in r["global_optouts"]] == [True, True]
    assert X._ADVICE_DX12 not in r["advice"]                      # that one is about per-game flags
    assert X._ADVICE_GLOBAL_IGNORED not in r["advice"]
    m.rset(GCS, "GameDVR_FSEBehaviorMode", 0)
    m.rset(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible", 0)
    assert X.fullscreen_check("nothing.exe")["verdict"] == "ok"


def test_fse_behavior_mode_without_honor_user_is_listed_but_not_counted():
    _fresh()
    m.rset(GCS, "GameDVR_FSEBehaviorMode", 2)
    for honor in (0, None):                                       # 0, then not there at all
        if honor is not None:
            m.rset(GCS, "GameDVR_HonorUserFSEBehaviorMode", honor)
        r = X.fullscreen_check("nothing.exe")
        assert r["verdict"] == "ok", honor
        assert r["advice"] == [X._ADVICE_NOTHING, X._ADVICE_GLOBAL_IGNORED], honor
        assert X._ADVICE_GLOBAL not in r["advice"]
        row, = r["global_optouts"]
        assert row["name"] == "GameDVR_FSEBehaviorMode" and row["effective"] is False and row["honor_user"] == honor
        assert "probably changes nothing" in row["meaning"] and chr(0x2014) not in row["meaning"]
        m.rdel(GCS, "GameDVR_HonorUserFSEBehaviorMode")
    # Per-game flags still count, and the global value is explained, not blamed.
    _seed_layers()
    r = X.fullscreen_check("cs2.exe")
    assert r["verdict"] == "may_be_exclusive" and X._ADVICE_GLOBAL not in r["advice"]
    assert X._ADVICE_FLAGS in r["advice"] and X._ADVICE_GLOBAL_IGNORED in r["advice"]
    assert r["advice"][-1] == X._ADVICE_BORDERLESS
    # With the DXGI opt-out on as well, that one is counted.
    m.rset(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    r = X.fullscreen_check("nothing.exe")
    assert r["verdict"] == "may_be_exclusive"
    assert X._ADVICE_GLOBAL in r["advice"] and X._ADVICE_GLOBAL_IGNORED in r["advice"]


def test_fullscreen_check_trusts_a_fresh_present_mode_for_this_game():
    _fresh()
    _seed_layers()
    now = time.time()
    FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": "Hardware: Legacy Flip", "ts": now - 2}
    r = X.fullscreen_check("cs2.exe")
    assert r["verdict"] == "exclusive" and r["present_mode"]["mode"] == "Hardware: Legacy Flip"
    assert 1.5 <= r["present_mode"]["age_s"] <= 3 and r["advice"][0] == X._ADVICE_EXCLUSIVE
    FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": "Composed: Flip", "ts": now}
    assert X.fullscreen_check("cs2.exe")["verdict"] == "ok"
    FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": "Composed: Flip", "ts": now - 11}
    r = X.fullscreen_check("cs2.exe")
    assert r["present_mode"] is None and r["verdict"] == "may_be_exclusive"
    FT._state["present_mode"] = {"pid": 7, "name": "valorant.exe", "mode": "Hardware: Legacy Flip", "ts": now}
    assert X.fullscreen_check("cs2.exe")["present_mode"] is None
    for mode in ("Hardware: Independent Flip", "Hardware Composed: Independent Flip", "Composed: Composition Atlas"):
        FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": mode, "ts": now}
        r = X.fullscreen_check("cs2.exe")
        assert r["verdict"] == "ok" and r["present_mode"]["mode"] == mode and r["advice"] == [X._ADVICE_SEEN_OK]


def test_a_present_mode_presentmon_could_not_tell_is_no_reading():
    _fresh()
    now = time.time()
    for mode in ("Other", "Unknown", "Hardware: Le", "  "):
        FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": mode, "ts": now}
        r = X.fullscreen_check("cs2.exe")
        assert r["present_mode"] is None and r["verdict"] == "ok" and r["advice"] == [X._ADVICE_NOTHING], mode
    _seed_layers()
    FT._state["present_mode"] = {"pid": 7, "name": "cs2.exe", "mode": "Other", "ts": now}
    r = X.fullscreen_check("cs2.exe")
    assert r["present_mode"] is None and r["verdict"] == "may_be_exclusive" and X._ADVICE_SEEN_OK not in r["advice"]


def test_fullscreen_fix_removes_only_the_one_flag_and_undo_puts_it_all_back():
    _fresh()
    _seed_layers()
    before = m.flat()
    r = X.fullscreen_fix("cs2.exe")
    assert r["ok"] and len(r["changed"]) == 4 and r["failed"] == []
    assert r["advice"] == "Restart the game for this to take effect."
    assert m.rget(LAYERS_CU, CS2) == ("REG_SZ", "~ HIGHDPIAWARE")                  # "~" stays first
    assert m.rget(LAYERS_LM, CS2) is None                                           # nothing left: deleted
    assert m.rget(LAYERS_CU, CS2_COPY) is None                                      # only "~" left: deleted
    assert m.rget(LAYERS_LM, CS2_COPY) == ("REG_SZ", "HIGHDPIAWARE RUNASADMIN")
    assert m.rget(LAYERS_CU, OTHER) == ("REG_SZ", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")     # other games untouched
    assert m.rget(LAYERS_CU, r"C:\Games\cs2.exe.bak") == ("REG_SZ", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    assert m.rget(LAYERS_CU, r"C:\Old\CS2.EXE") == ("REG_SZ", "~ RUNASADMIN")
    r = X.fullscreen_check("cs2.exe")
    assert r["verdict"] == "ok" and r["fso_flags"] == [] and r["can_undo"] is True and r["fixed_exes"] == ["cs2.exe"]
    again = X.fullscreen_fix("cs2.exe")
    assert again["ok"] and again["changed"] == []
    u = X.fullscreen_undo()
    assert u["ok"] and u["restored"] == 4
    assert m.flat() == before
    assert X.fullscreen_check("cs2.exe")["can_undo"] is False
    assert X.fullscreen_fix("")["ok"] is False


def test_a_refused_write_is_reported_and_the_rest_still_undoes():
    _fresh()
    _seed_layers()
    before = m.flat()
    m.REG_FAIL.add((LAYERS_LM.lower(), CS2.lower()))
    r = X.fullscreen_fix("cs2.exe")
    assert r["ok"] is False and r["err"] == "Some of this game's settings couldn't be changed."
    assert [f["path"] for f in r["failed"]] == [CS2] and len(r["changed"]) == 3
    assert m.rget(LAYERS_LM, CS2) == ("REG_SZ", "disabledxmaximizedwindowedmode")
    assert m.rget(LAYERS_CU, CS2) == ("REG_SZ", "~ HIGHDPIAWARE")
    m.REG_FAIL.clear()
    assert X.fullscreen_undo()["ok"]
    assert m.flat() == before


def test_the_fix_changes_only_the_programs_the_page_listed():
    """The page sends the paths it showed: a copy of the game that turned up
    after the check is left alone."""
    _fresh()
    _seed_layers()
    before = m.flat()
    r = X.fullscreen_fix("cs2.exe", [CS2.upper()])
    assert r["ok"] and sorted(f["hive"] for f in r["changed"]) == ["HKCU", "HKLM"]
    assert all(f["path"] == CS2 for f in r["changed"])
    assert m.rget(LAYERS_CU, CS2) == ("REG_SZ", "~ HIGHDPIAWARE")
    assert m.rget(LAYERS_CU, CS2_COPY) == ("REG_SZ", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")       # not listed
    assert m.rget(LAYERS_LM, CS2_COPY) == ("REG_SZ", "HIGHDPIAWARE DISABLEDXMAXIMIZEDWINDOWEDMODE RUNASADMIN")
    assert X.fullscreen_undo()["ok"] and m.flat() == before
    for no_filter in (None, [], [""], "C:\\x\\cs2.exe"):
        assert len(X.fullscreen_fix("cs2.exe", no_filter)["changed"]) == 4, no_filter
        assert X.fullscreen_undo()["ok"] and m.flat() == before


def test_the_global_fix_and_its_undo():
    _fresh()
    assert X.fullscreen_fix_global() == {"ok": True, "changed": [], "failed": []}
    m.rset(GCS, "GameDVR_FSEBehaviorMode", 2)
    m.rset(GCS, "GameDVR_HonorUserFSEBehaviorMode", 1)
    m.rset(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    before = m.flat()
    r = X.fullscreen_fix_global()
    assert r["ok"] and r["changed"] == ["GameDVR_FSEBehaviorMode", "GameDVR_DXGIHonorFSEWindowsCompatible"]
    assert m.rdata(GCS, "GameDVR_FSEBehaviorMode") == 0 and m.rdata(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible") == 0
    assert m.rdata(GCS, "GameDVR_HonorUserFSEBehaviorMode") == 1
    assert X.fullscreen_check("x.exe")["global_optouts"] == [] and X.fullscreen_check("x.exe")["can_undo"] is True
    assert X.fullscreen_undo()["ok"]
    assert m.flat() == before


def test_snapshots_cover_the_registry_the_fullscreen_fix_changes():
    keys = {k.lower() for _, k in snapshots._GS_REGISTRY_SCOPE}
    for key in (LAYERS_CU, LAYERS_LM, GCS):
        assert key.lower() in keys, key


# ── PresentMode in frame telemetry ────────────────────────────────────
class _Proc:
    def __init__(self, text):
        self.stdout = io.StringIO(text)


_PM2_HEADER = "Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing," \
              "PresentMode,CPUStartTime,FrameTime,CPUBusy\n"


def _row(name="cs2.exe", pid=4242, mode="Hardware: Legacy Flip", ft="6.9"):
    return f"{name},{pid},0x1,DXGI,0,0,0,{mode},1.0,{ft},1.0\n"


def test_the_pm2_stream_reader_records_the_present_mode():
    FT._state["present_mode"] = None
    FT._samples.clear()
    FT._pm_stdout_reader(_Proc("\ufeff" + _PM2_HEADER + _row(ft="0")), threading.Event())
    pm = FT.get_present_mode()
    assert pm["mode"] == "Hardware: Legacy Flip" and pm["pid"] == 4242 and pm["name"] == "cs2.exe"
    assert time.time() - pm["ts"] < 5
    assert len(FT._samples) == 0                     # the frame-time filter still drops that row
    first = FT.get_present_mode()
    FT._pm_stdout_reader(_Proc(_PM2_HEADER + _row()), threading.Event())
    assert FT.get_present_mode() == first           # same game, same mode, within a second: not rewritten
    assert len(FT._samples) == 1
    FT._pm_stdout_reader(_Proc(_PM2_HEADER + _row() + _row(mode="Composed: Flip")), threading.Event())
    assert FT.get_present_mode()["mode"] == "Composed: Flip"
    FT._pm_stdout_reader(_Proc(_PM2_HEADER + _row(pid=99, mode="Composed: Flip")), threading.Event())
    assert FT.get_present_mode()["pid"] == 99
    FT._state["present_mode"] = dict(FT._state["present_mode"], ts=time.time() - 5)
    FT._pm_stdout_reader(_Proc(_PM2_HEADER + _row(pid=99, mode="Composed: Flip")), threading.Event())
    assert time.time() - FT.get_present_mode()["ts"] < 1           # refreshed once a second
    got = FT.get_present_mode()
    got["mode"] = "changed by a caller"
    assert FT.get_present_mode()["mode"] == "Composed: Flip"         # callers get a copy


def test_no_present_mode_column_changes_nothing():
    FT._state["present_mode"] = None
    FT._samples.clear()
    FT._pm_stdout_reader(_Proc("Application,ProcessID,FrameTime\ncs2.exe,1,7.0\n"), threading.Event())
    assert FT.get_present_mode() is None and len(FT._samples) == 1


def test_the_pm1_csv_reader_records_the_present_mode_too():
    FT._state["present_mode"] = None
    FT._samples.clear()
    FT._state["pm_csv_header"] = None
    FT._state["pm_csv_pos"] = 0
    path = os.path.join(m.ROOT, "pm.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,"
                "WasBatched,DwmNotified,Dropped,TimeInSeconds,msInPresentAPI,msBetweenPresents\n")
        f.write("game.exe,77,0x1,DXGI,0,0,0,Hardware: Independent Flip,0,0,0,1.0,0.1,6.9\n")
        f.write("game.exe,77,0x1,DXGI,0,0,0,Hardware: Legacy Flip,0,0,0,1.1,0.1,0\n")
    assert FT._parse_presentmon_csv({"csv_path": path}) == 1
    pm = FT.get_present_mode()
    assert (pm["name"], pm["pid"], pm["mode"]) == ("game.exe", 77, "Hardware: Legacy Flip")
    FT._state["pm_csv_header"] = None
    FT._state["pm_csv_pos"] = 0
