"""app.py - the crosshair routes, how the overlay is allowed to start, the
tray item and the quit path.  The routes are thin wrappers over
core/crosshair.py, so these tests check the wiring: every route answers,
bodies that aren't JSON objects are refused with a 400, the API guard still
applies, and nothing here ever reaches a real window (the sandbox's fake
Win32 adapter is in place before app.py is imported)."""
import sandbox as m

import importlib.util
import inspect
import os
import sys
import threading
import types
import unittest

from PIL import Image

import config

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

from core import crosshair as X
from core import crosshair_overlay as xo
from core import frame_telemetry as FT
from core import game_profiles

HOST = {"Host": f"127.0.0.1:{config.APP_PORT}"}
LAYERS_CU = r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
GCS = r"HKCU\System\GameConfigStore"
CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
NOT_AN_OBJECT = "Expected a JSON object."

GET_ROUTES = ("/api/crosshair/state", "/api/crosshair/status", "/api/crosshair/fullscreen")
POST_ROUTES = ("/api/crosshair/settings", "/api/crosshair/preview", "/api/crosshair/presets",
               "/api/crosshair/activate", "/api/crosshair/import", "/api/crosshair/export",
               "/api/crosshair/image/browse", "/api/crosshair/games", "/api/crosshair/hidden",
               "/api/crosshair/test", "/api/crosshair/fullscreen/fix", "/api/crosshair/fullscreen/fix-global",
               "/api/crosshair/fullscreen/undo", "/api/crosshair/legacy-notice/dismiss")
_MISSING = object()


def client():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A.app.test_client()


def auth(**extra):
    return {**HOST, A._API_TOKEN_HEADER: A._API_TOKEN, **extra}


def _fresh():
    """A clean machine, crosshair settings and overlay; returns the fake Win32 adapter."""
    c = client()
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
    return c, fake


def _post(c, route, body):
    r = c.post(route, headers=auth(), json=body)
    assert r.status_code == 200, (route, r.status_code, r.get_data(as_text=True))
    return r.get_json()


def _get(c, route):
    r = c.get(route, headers=auth())
    assert r.status_code == 200, (route, r.status_code, r.get_data(as_text=True))
    return r.get_json()


class _swap_module:
    """Put a stand-in module into sys.modules for the duration of a with block."""

    def __init__(self, name, module):
        self.name, self.module = name, module

    def __enter__(self):
        self.saved = sys.modules.get(self.name, _MISSING)
        sys.modules[self.name] = self.module
        return self.module

    def __exit__(self, *exc):
        if self.saved is _MISSING:
            sys.modules.pop(self.name, None)
        else:
            sys.modules[self.name] = self.saved
        return False


# ── the sandbox and the route table ────────────────────────────────────
def test_the_app_drives_the_sandboxed_overlay():
    client()
    assert A.crosshair is X and X.overlay is xo
    assert isinstance(xo._win, m.FakeOverlayWin), "a route test must never be able to open a real window"
    assert A.config.APPDATA_DIR.startswith(m.ROOT)


def test_every_crosshair_route_is_registered_with_its_method():
    client()
    found = {}
    for rule in A.app.url_map.iter_rules():
        if rule.rule.startswith("/api/crosshair/"):
            found[rule.rule] = sorted(rule.methods - {"HEAD", "OPTIONS"})
    want = {**{r: ["GET"] for r in GET_ROUTES}, **{r: ["POST"] for r in POST_ROUTES}}
    assert found == want, (found, want)


def test_the_api_guard_still_covers_the_crosshair_routes():
    c, _ = _fresh()
    for route in GET_ROUTES:
        assert c.get(route, headers=HOST).status_code == 403, route
        assert c.get(route, headers=auth(Origin="https://evil.example")).status_code == 403, route
        assert c.get(route, headers={"Host": "evil.example", A._API_TOKEN_HEADER: A._API_TOKEN}).status_code == 403
    for route in POST_ROUTES:
        r = c.post(route, headers=HOST, json={})
        assert r.status_code == 403 and r.get_json()["err"] == "missing or wrong session token", route
        assert c.post(route, headers=auth(**{"Sec-Fetch-Site": "cross-site"}), json={}).status_code == 403, route
    assert not os.path.exists(X.SETTINGS_PATH), "a refused call changed nothing"


def test_post_bodies_that_are_not_json_objects_get_a_400_and_change_nothing():
    c, fake = _fresh()
    bad = [dict(json=[]), dict(json=["enabled", True]), dict(json="enabled"), dict(json=5), dict(json=True),
           dict(data="null", content_type="application/json"), dict(data="{not json", content_type="application/json"),
           dict(data="", content_type="application/json"), dict()]
    for route in POST_ROUTES:
        for kw in bad:
            r = c.post(route, headers=auth(), **kw)
            assert r.status_code == 400, (route, kw, r.status_code)
            assert r.get_json() == {"ok": False, "err": NOT_AN_OBJECT}, (route, kw, r.get_json())
    assert not os.path.exists(X.SETTINGS_PATH) and not m.WRITES and not m.DELETES
    assert not fake.calls_to("CreateWindowExW")


def test_the_wrong_method_is_answered_as_json():
    c, _ = _fresh()
    for route in POST_ROUTES:
        r = c.get(route, headers=auth())
        assert r.status_code == 405 and r.get_json()["ok"] is False, route
    for route in GET_ROUTES:
        r = c.post(route, headers=auth(), json={})
        assert r.status_code == 405 and r.get_json()["ok"] is False, route


# ── state, status, settings and the start gate ────────────────────────
def test_state_and_status_load_on_a_fresh_machine():
    c, _ = _fresh()
    st = _get(c, "/api/crosshair/state")
    assert st["ok"] is True and st["enabled"] is False and st["settings"]["enabled"] is False
    assert [p["id"] for p in st["presets"]][:12] == list(A.crosshair.xr.BUILTIN_IDS)
    assert st["presets"][0]["thumb"].startswith("data:image/png;base64,")
    assert st["status"]["reason"] == "disabled" and st["legacy_notice"] is False
    assert st["monitors"] and st["monitors"][0]["label"].startswith("Display 1")
    s = _get(c, "/api/crosshair/status")
    assert s["ok"] is True and s["enabled"] is False and s["status"]["reason"] == "disabled"
    assert s["active_preset"] == s["preset_in_effect"] == "builtin:classic"


def test_settings_save_and_bad_values_are_explained():
    c, _ = _fresh()
    r = _post(c, "/api/crosshair/settings", {"show_mode": "always", "offset_x": 12, "no_such_key": 1})
    assert r["ok"] is True and r["settings"]["show_mode"] == "always" and r["settings"]["offset_x"] == 12
    assert "no_such_key" not in r["settings"]
    r = _post(c, "/api/crosshair/settings", {"hotkeys": {"toggle": "Ctrl+Nonsense"}})
    assert r["ok"] is False and r["err"].endswith("."), r
    assert _get(c, "/api/crosshair/state")["settings"]["hotkeys"]["toggle"] == "Ctrl+Alt+X"


def test_turning_it_on_waits_for_the_window_then_starts_on_the_fake():
    c, fake = _fresh()
    try:
        r = _post(c, "/api/crosshair/settings", {"enabled": True})
        assert r["ok"] is True and r["settings"]["enabled"] is True
        assert r["status"]["reason"] == "waiting_for_app"
        assert not fake.calls_to("CreateWindowExW"), "no window before pywebview made the process DPI aware"
        r = _post(c, "/api/crosshair/test", {"seconds": 3})
        assert r["ok"] is False and r["err"].endswith("."), "the test show can't start it early either"

        A._crosshair_allow_start()                  # what the window's before_show event calls
        assert m.wait(xo.is_running), "the overlay never started"
        made = fake.calls_to("CreateWindowExW")
        assert len(made) == 1 and made[0][1][2] == "", made     # empty title: never "Vispora"
        assert _get(c, "/api/crosshair/status")["status"]["running"] is True

        r = _post(c, "/api/crosshair/test", {"seconds": 3})
        assert r["ok"] is True and "status" in r
        r = _post(c, "/api/crosshair/hidden", {"hidden": "yes"})
        assert r["ok"] is False, "only a real true or false"
        r = _post(c, "/api/crosshair/hidden", {"hidden": True})
        assert r["ok"] is True and "status" in r
        assert _post(c, "/api/crosshair/hidden", {"hidden": False})["ok"] is True

        r = _post(c, "/api/crosshair/settings", {"enabled": False})
        assert r["ok"] is True and r["status"]["reason"] == "disabled"
        assert m.wait(lambda: not xo.is_running())
        assert fake.calls_to("DestroyWindow")
    finally:
        X.stop()
        X._ui_ready = False


def test_quitting_closes_the_crosshair_but_keeps_it_on_for_next_time():
    c, fake = _fresh()
    try:
        assert _post(c, "/api/crosshair/settings", {"enabled": True})["ok"]
        A._crosshair_allow_start()
        assert m.wait(xo.is_running)
        A._restore_system_before_exit("user quit")
        assert not xo.is_running() and fake.calls_to("DestroyWindow")
        assert X._watch_thread is None, "the fullscreen watch stopped too"
        assert _get(c, "/api/crosshair/state")["settings"]["enabled"] is True
    finally:
        X.stop()
        X._ui_ready = False


# ── designer, presets, codes, images ──────────────────────────────────
def test_preview_draws_the_design_it_is_given():
    c, _ = _fresh()
    design = {"kind": "shape", "shape": "dot", "thickness": 5, "color": "#ff0000"}
    r = _post(c, "/api/crosshair/preview", {"design": design, "background": "dark", "target_height": 1080})
    assert r["ok"] is True and r["png"].startswith("data:image/png;base64,")
    assert r["width"] % 2 == 1 and r["height"] % 2 == 1 and r["target_height"] == 1080
    assert r["design"]["shape"] == "dot" and r["design"]["color"] == "#FF0000"
    r = _post(c, "/api/crosshair/preview", {})
    assert r["ok"] is True and r["design"]["shape"] == "cross", "no design: the default one"


def test_presets_save_activate_rename_duplicate_and_delete():
    c, _ = _fresh()
    design = {"shape": "circle", "ring_radius": 10}
    r = _post(c, "/api/crosshair/presets", {"action": "save", "name": "Mine", "design": design, "activate": True})
    assert r["ok"] is True and r["id"].startswith("user:") and r["settings"]["active_preset"] == r["id"]
    pid = r["id"]
    assert [p["name"] for p in r["presets"] if p["id"] == pid] == ["Mine"]

    r = _post(c, "/api/crosshair/activate", {"id": "builtin:dot"})
    assert r["ok"] is True and r["settings"]["active_preset"] == "builtin:dot" and "status" in r
    assert _post(c, "/api/crosshair/activate", {"id": "builtin:nope"})["ok"] is False
    assert _post(c, "/api/crosshair/activate", {})["ok"] is False

    r = _post(c, "/api/crosshair/presets", {"action": "rename", "id": pid, "name": "Ring 10"})
    assert r["ok"] is True and [p["name"] for p in r["presets"] if p["id"] == pid] == ["Ring 10"]
    r = _post(c, "/api/crosshair/presets", {"action": "update", "id": pid, "design": {"shape": "box"}})
    assert r["ok"] is True and [p["design"]["shape"] for p in r["presets"] if p["id"] == pid] == ["box"]
    r = _post(c, "/api/crosshair/presets", {"action": "duplicate", "id": "builtin:classic"})
    assert r["ok"] is True and [p["name"] for p in r["presets"] if p["id"] == r["id"]] == ["Classic copy"]
    r = _post(c, "/api/crosshair/presets", {"action": "rename", "id": "builtin:classic", "name": "x"})
    assert r["ok"] is False, "built-ins can't be renamed"
    r = _post(c, "/api/crosshair/presets", {"action": "delete", "id": pid})
    assert r["ok"] is True and pid not in [p["id"] for p in r["presets"]]
    r = _post(c, "/api/crosshair/presets", {"action": "explode", "id": "builtin:dot"})
    assert r["ok"] is False and r["err"].endswith(".")
    assert _post(c, "/api/crosshair/presets", {})["ok"] is False


def test_codes_export_and_import():
    c, _ = _fresh()
    r = _post(c, "/api/crosshair/export", {"design": {"shape": "chevron", "color": "#56b4e9"}})
    assert r["ok"] is True and r["code"].startswith("VXH1-")
    back = _post(c, "/api/crosshair/import", {"code": r["code"]})
    assert back["ok"] is True and back["source"] == "vispora" and back["design"]["shape"] == "chevron"
    assert back["design"]["color"] == "#56B4E9"
    val = _post(c, "/api/crosshair/import", {"code": "0;P;c;5;h;0;0l;4;0o;2;0a;1;0f;0;1b;0"})
    assert val["ok"] is True and val["source"] == "valorant" and val["approximate"] is True
    for junk in ({"code": "not a code"}, {"code": 42}, {}):
        r = _post(c, "/api/crosshair/import", junk)
        assert r["ok"] is False and r["err"].endswith("."), junk


class _DialogWindow:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def create_file_dialog(self, kind, **kw):
        self.calls.append((kind, kw))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _fake_webview(windows, file_dialog=True):
    mod = types.ModuleType("webview")
    mod.windows = windows
    if file_dialog:
        mod.FileDialog = types.SimpleNamespace(OPEN=10)
    else:
        mod.OPEN_DIALOG = 10                        # pywebview before FileDialog existed
    return mod


def test_the_image_picker_route():
    c, _ = _fresh()
    pick = os.path.join(m.ROOT, "my crosshair.png")
    Image.new("RGBA", (21, 15), (0, 255, 0, 255)).save(pick, "PNG")
    not_png = os.path.join(m.ROOT, "notes.png")
    with open(not_png, "wb") as f:
        f.write(b"just text")

    with _swap_module("webview", _fake_webview([])):
        r = _post(c, "/api/crosshair/image/browse", {})
        assert r["ok"] is False and r["err"].endswith("."), "no window (browser mode)"

    win = _DialogWindow(None)
    with _swap_module("webview", _fake_webview([win])):
        assert _post(c, "/api/crosshair/image/browse", {}) == {"ok": True, "cancelled": True}
        assert win.calls == [(10, {"allow_multiple": False, "file_types": ("PNG images (*.png)",)})], win.calls

    win = _DialogWindow((pick,))
    with _swap_module("webview", _fake_webview([win], file_dialog=False)):
        r = _post(c, "/api/crosshair/image/browse", {})
        assert r["ok"] is True and len(r["image_id"]) == 32 and (r["width"], r["height"]) == (21, 15), r
        assert win.calls[0][0] == 10
    assert os.path.exists(os.path.join(X.IMAGES_DIR, r["image_id"] + ".png"))

    with _swap_module("webview", _fake_webview([_DialogWindow(not_png)])):
        r = _post(c, "/api/crosshair/image/browse", {})
        assert r["ok"] is False and r["err"].endswith("."), "a plain path string works, and a fake PNG is refused"

    with _swap_module("webview", _fake_webview([_DialogWindow(RuntimeError("COM went away"))])):
        r = _post(c, "/api/crosshair/image/browse", {})
        assert r["ok"] is False and "file picker" in r["err"]


# ── games, fullscreen, legacy notice ──────────────────────────────────
def test_game_rules_are_added_changed_and_removed():
    c, _ = _fresh()
    r = _post(c, "/api/crosshair/games", {"exe": "CS2.exe", "rule": {"mode": "show", "preset": "builtin:dot"}})
    assert r["ok"] is True and r["settings"]["games"]["cs2.exe"]["mode"] == "show"
    assert r["settings"]["games"]["cs2.exe"]["preset"] == "builtin:dot"
    r = _post(c, "/api/crosshair/games", {"exe": "cs2.exe", "rule": {"offset_x": 4}})
    assert r["ok"] is True and r["settings"]["games"]["cs2.exe"]["offset_x"] == 4
    assert r["settings"]["games"]["cs2.exe"]["mode"] == "show", "merged onto the rule"
    r = _post(c, "/api/crosshair/games", {"exe": "cs2.exe", "rule": {"mode": "loud"}})
    assert r["ok"] is False and r["err"].endswith(".")
    r = _post(c, "/api/crosshair/games", {"exe": "cs2.exe", "rule": None})
    assert r["ok"] is True and "cs2.exe" not in r["settings"]["games"]
    for exe in ("bad*name.exe", "", 5, None):
        r = _post(c, "/api/crosshair/games", {"exe": exe, "rule": {"mode": "show"}})
        assert r["ok"] is False and r["err"].endswith("."), exe


def test_fullscreen_check_fix_and_undo():
    c, _ = _fresh()
    m.rset(LAYERS_CU, CS2, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE HIGHDPIAWARE")
    r = _get(c, "/api/crosshair/fullscreen?exe=cs2.exe")
    assert r["ok"] is True and r["exe"] == "cs2.exe" and r["verdict"] == "may_be_exclusive"
    assert [f["hive"] for f in r["fso_flags"]] == ["HKCU"] and r["can_undo"] is False

    r = _post(c, "/api/crosshair/fullscreen/fix", {"exe": "cs2.exe"})
    assert r["ok"] is True and len(r["changed"]) == 1
    assert m.rget(LAYERS_CU, CS2) == ("REG_SZ", "~ HIGHDPIAWARE")
    r = _get(c, "/api/crosshair/fullscreen?exe=cs2.exe")
    assert r["fso_flags"] == [] and r["can_undo"] is True and r["verdict"] == "ok"
    assert _post(c, "/api/crosshair/fullscreen/fix", {})["ok"] is False, "no exe"
    # The paths the page listed limit the fix; anything but a list of strings is no limit.
    other = r"E:\Games\cs2\cs2.exe"
    m.rset(LAYERS_CU, CS2, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    m.rset(LAYERS_CU, other, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    r = _post(c, "/api/crosshair/fullscreen/fix", {"exe": "cs2.exe", "paths": [CS2]})
    assert r["ok"] is True and [f["path"] for f in r["changed"]] == [CS2]
    assert m.rget(LAYERS_CU, other) == ("REG_SZ", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE")
    r = _post(c, "/api/crosshair/fullscreen/fix", {"exe": "cs2.exe", "paths": [other, 5]})
    assert r["ok"] is True and [f["path"] for f in r["changed"]] == [other], "a bad list is no limit"

    m.rset(GCS, "GameDVR_FSEBehaviorMode", 2)
    m.rset(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    r = _post(c, "/api/crosshair/fullscreen/fix-global", {})
    assert r["ok"] is True and sorted(r["changed"]) == ["GameDVR_DXGIHonorFSEWindowsCompatible",
                                                        "GameDVR_FSEBehaviorMode"]
    assert m.rdata(GCS, "GameDVR_FSEBehaviorMode") == 0

    r = _post(c, "/api/crosshair/fullscreen/undo", {})
    assert r["ok"] is True and r["restored"] >= 3, r
    assert m.rget(LAYERS_CU, CS2) == ("REG_SZ", "~ DISABLEDXMAXIMIZEDWINDOWEDMODE HIGHDPIAWARE")
    assert m.rdata(GCS, "GameDVR_FSEBehaviorMode") == 2 and m.rdata(GCS, "GameDVR_DXGIHonorFSEWindowsCompatible") == 1
    assert _get(c, "/api/crosshair/fullscreen?exe=cs2.exe")["can_undo"] is False


def _seen_in_front(*exes):
    with xo._lock:
        xo._last_games[:] = list(exes)


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


def test_the_fullscreen_check_picks_the_game_itself_when_none_is_named():
    c, _ = _fresh()
    assert callable(getattr(X, "default_game", None)), \
        "core/crosshair.default_game() is missing: the route only has its fallback"
    try:
        r = _get(c, "/api/crosshair/fullscreen")
        assert r["ok"] is True and r["exe"] == "" and r["verdict"] == "unknown", "nothing known: nothing guessed"

        _seen_in_front("chrome.exe", "discord.exe")
        r = _get(c, "/api/crosshair/fullscreen")
        assert r["exe"] == "" and r["verdict"] == "unknown", "a browser or Discord last in front is not a game"
        assert _get(c, "/api/crosshair/state")["last_game"] == "", "and the page doesn't select one either"

        _seen_in_front("discord.exe", "valorant-win64-shipping.exe", "r5apex.exe")
        assert _get(c, "/api/crosshair/fullscreen?exe=")["exe"] == "valorant-win64-shipping.exe", \
            "the newest game the crosshair saw in front, not the chat app after it"
        assert _get(c, "/api/crosshair/state")["last_game"] == "valorant-win64-shipping.exe", \
            "the page selects the same game the route checks"

        assert _post(c, "/api/crosshair/games", {"exe": "mygame.exe", "rule": {"mode": "show"}})["ok"]
        _seen_in_front("chrome.exe", "mygame.exe", "cs2.exe")
        assert _get(c, "/api/crosshair/fullscreen")["exe"] == "mygame.exe", "a game with a rule counts"
        _seen_in_front("steamwebhelper.exe", "escapefromtarkov.exe", "cs2.exe")
        assert _get(c, "/api/crosshair/fullscreen")["exe"] == "escapefromtarkov.exe", "so does a blocked game"

        _seen_in_front("discord.exe", "r5apex.exe")
        with _game_mode_running("cs2.exe", "CS2"):
            assert _get(c, "/api/crosshair/fullscreen")["exe"] == "cs2.exe", "the game Game Mode is running for"
            assert _get(c, "/api/crosshair/fullscreen?exe=r5apex.exe")["exe"] == "r5apex.exe", "a named game wins"
            assert _get(c, "/api/crosshair/fullscreen?exe=discord.exe")["exe"] == "discord.exe", \
                "the user may still check any program by name"
    finally:
        _seen_in_front()


def test_the_fullscreen_default_still_works_without_default_game():
    """The route guards its call to crosshair.default_game(): if it is missing
    or raises, the route falls back to the same order with known games only."""
    c, _ = _fresh()
    saved = X.__dict__.get("default_game", _MISSING)
    try:
        def broken():
            raise RuntimeError("settings unreadable")

        for stand_in in (_MISSING, broken):
            if stand_in is _MISSING:
                X.__dict__.pop("default_game", None)
            else:
                X.default_game = stand_in
            _seen_in_front()
            r = _get(c, "/api/crosshair/fullscreen")
            assert r["ok"] is True and r["exe"] == "" and r["verdict"] == "unknown", stand_in
            _seen_in_front("discord.exe", "cs2.exe", "r5apex.exe")
            assert _get(c, "/api/crosshair/fullscreen")["exe"] == "cs2.exe", ("the newest known game", stand_in)
            _seen_in_front("chrome.exe")
            assert _get(c, "/api/crosshair/fullscreen")["exe"] == "", ("never a browser", stand_in)
            with _game_mode_running("r5apex.exe", "Apex Legends"):
                assert _get(c, "/api/crosshair/fullscreen")["exe"] == "r5apex.exe", stand_in

        X.default_game = lambda: None                # an answer that isn't a name: no game
        _seen_in_front("cs2.exe")
        assert _get(c, "/api/crosshair/fullscreen")["exe"] == ""
    finally:
        if saved is _MISSING:
            X.__dict__.pop("default_game", None)
        else:
            X.default_game = saved
        _seen_in_front()


def test_the_legacy_notice_is_dismissed():
    c, _ = _fresh()
    with open(os.path.join(config.APPDATA_DIR, "gamepad_profiles.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    assert _get(c, "/api/crosshair/state")["legacy_notice"] is True
    assert _post(c, "/api/crosshair/legacy-notice/dismiss", {}) == {"ok": True}
    assert _get(c, "/api/crosshair/state")["legacy_notice"] is False
    assert os.path.exists(os.path.join(config.APPDATA_DIR, "gamepad_profiles.json")), "old profiles are kept"


# ── when the overlay may start: pywebview's before_show ────────────────
def _pywebview_file(*parts):
    spec = importlib.util.find_spec("webview")
    if spec is None or not spec.submodule_search_locations:
        raise unittest.SkipTest("pywebview is not installed with this Python")
    path = os.path.join(list(spec.submodule_search_locations)[0], *parts)
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_installed_pywebview_sets_before_show_after_dpi_awareness_even_for_hidden_windows():
    """The reason app.py ties allow_start() to events.before_show: in
    winforms.create_window the main thread calls SetProcessDPIAware() before
    it starts the GUI thread, and the GUI thread sets before_show before it
    shows (or, for an autostart window, hides) the form."""
    src = _pywebview_file("platforms", "winforms.py")
    body = src[src.index("\ndef create_window(window):"):]
    body = body[:body.index("\ndef ", 1)]
    split = body.index("app = WinForms.Application")
    create, master = body[:split], body[split:]     # the GUI thread's create(), then what the main thread does
    assert create.index("window.events.before_show.set()") < create.index("if window.hidden:"), create
    assert master.index("SetProcessDPIAware()") < master.index("thread.Start()"), master
    window_src = _pywebview_file("window.py")
    assert "self.events.before_show = Event(self, True)" in window_src, \
        "before_show must run its handlers before returning, i.e. before Show()"


def test_the_before_show_handler_only_allows_the_start():
    c, fake = _fresh()
    try:
        from webview.event import Event
    except ImportError:
        raise unittest.SkipTest("pywebview is not installed with this Python")
    window = types.SimpleNamespace(events=types.SimpleNamespace())
    window.events.before_show = Event(window, True)
    window.events.shown = Event(window)
    assert A._gate_crosshair_start(window) == "before_show"
    assert len(window.events.before_show) == 1 and len(window.events.shown) == 0
    try:
        assert _post(c, "/api/crosshair/settings", {"enabled": True})["status"]["reason"] == "waiting_for_app"
        assert window.events.before_show.set() is False, "the handler never vetoes the window"
        assert X._ui_ready is True
        assert m.wait(xo.is_running) and len(fake.calls_to("CreateWindowExW")) == 1
    finally:
        X.stop()
        X._ui_ready = False


def test_without_before_show_the_gate_uses_shown_and_a_late_fallback():
    client()
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon, self.started = delay, fn, False, False
            timers.append(self)

        def start(self):
            self.started = True

    added = []

    class Shown:                                    # an older pywebview: no before_show
        def __iadd__(self, fn):
            added.append(fn)
            return self

    window = types.SimpleNamespace(events=types.SimpleNamespace(shown=Shown()))
    saved = A.threading.Timer
    A.threading.Timer = FakeTimer
    try:
        assert A._gate_crosshair_start(window) == "shown"
        assert A._gate_crosshair_start(object()) == "none"
    finally:
        A.threading.Timer = saved
    assert added == [A._crosshair_allow_start]
    assert [(t.fn, t.daemon, t.started) for t in timers] == [(A._crosshair_allow_start, True, True)] * 2
    assert all(t.delay >= 10 for t in timers)


def test_main_ties_the_start_to_the_window_and_allows_it_at_once_in_browser_mode():
    client()
    src = inspect.getsource(A.main)
    created = src.index("webview.create_window(")
    gated = src.index("_gate_crosshair_start(_webview_window)")
    started = src.index("webview.start(debug=False")
    assert created < gated < started, "the handler must be added before pywebview starts"
    fallback = src[src.index("except ImportError:"):]
    assert "_crosshair_allow_start()" in fallback, "browser mode never sets DPI awareness: start at once"
    assert src.count("allow_start") == 1, "nothing else may let the overlay open before the window"


# ── the tray item ─────────────────────────────────────────────────────
class _FakeMenuItem:
    def __init__(self, text, action, checked=None, visible=True, enabled=True, default=False):
        self.text, self.action, self.checked = text, action, checked


class _FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class _FakeIcon:
    made = []

    def __init__(self, name, icon=None, title=None, menu=None):
        self.menu, self.updated_from = menu, []
        _FakeIcon.made.append(self)

    def run(self):                                  # the real one runs the tray's message loop
        return None

    def update_menu(self):                          # the tray watcher calls it too, every 3 s
        self.updated_from.append(threading.current_thread().name)

    def stop(self):
        pass


class _record_toasts:
    """Catch the tray's toasts (core.notifier.toast) for a with block."""

    def __enter__(self):
        from core import notifier
        self.notifier, self.saved, self.shown = notifier, notifier.toast, []
        notifier.toast = lambda title, message, force=False: self.shown.append((title, message)) or {"ok": True}
        return self.shown

    def __exit__(self, *exc):
        self.notifier.toast = self.saved
        return False


def _tray_crosshair_item():
    """Build the tray with a stand-in pystray; returns (icon, the Crosshair item)."""
    fake_pystray = types.ModuleType("pystray")
    fake_pystray.Icon, fake_pystray.Menu, fake_pystray.MenuItem = _FakeIcon, _FakeMenu, _FakeMenuItem
    with _swap_module("pystray", fake_pystray):
        A._create_tray_icon(lambda: None, lambda: None)
    icon = _FakeIcon.made[-1]
    items = [i for i in icon.menu.items if isinstance(i, _FakeMenuItem) and i.text == "Crosshair"]
    assert len(items) == 1, [getattr(i, "text", i) for i in icon.menu.items]
    return icon, items[0]


def _click(icon, item, n):
    """Click the tray item; n is how many clicks have finished afterwards."""
    item.action(icon, item)
    assert m.wait(lambda: icon.updated_from.count("tray-crosshair") == n), "the menu is refreshed after"


def test_the_tray_has_a_crosshair_check_item_that_toggles_it():
    _fresh()
    asked = []
    saved_get_state = X.get_state
    X.get_state = lambda: asked.append("get_state") or {"ok": False}
    try:
        with _record_toasts() as toasts:
            icon, item = _tray_crosshair_item()
            assert callable(item.checked) and item.checked(item) is False

            _click(icon, item, 1)
            assert X.is_enabled() is True and item.checked(item) is True
            assert X._load()["enabled"] is True and not xo.is_running(), "saved; it starts once the app allows it"
            _click(icon, item, 2)
            assert X.is_enabled() is False and item.checked(item) is False
        assert toasts == [], "waiting for the app window is not a failure"
        assert asked == [], "the menu is rebuilt every 3 s: it must not build the whole page state"
    finally:
        X.get_state = saved_get_state
        X.stop()


def test_the_tray_says_why_when_the_crosshair_window_does_not_open():
    _, fake = _fresh()
    fake.window_ok = False                          # CreateWindowExW fails
    X._ui_ready = True                              # the app window is up: turning it on starts the overlay
    try:
        with _record_toasts() as toasts:
            icon, item = _tray_crosshair_item()
            _click(icon, item, 1)
            st = X.get_status()["status"]
            assert st["reason"] == "error" and st["last_error"], st
            assert X.is_enabled() is True and item.checked(item) is True, "kept on, like the page's switch"
            assert toasts == [("Crosshair didn't start", st["last_error"])], toasts
            assert toasts[0][1].endswith(".") and chr(0x2014) not in toasts[0][1]

            _click(icon, item, 2)
            assert X.is_enabled() is False and len(toasts) == 1, "turning it off again says nothing"

            fake.window_ok = True
            _click(icon, item, 3)
            assert m.wait(xo.is_running) and len(toasts) == 1, "a start that works says nothing"
            _click(icon, item, 4)
            assert m.wait(lambda: not xo.is_running())
    finally:
        X.stop()
        X._ui_ready = False


def test_the_tray_says_why_when_the_setting_is_not_saved():
    _fresh()
    saved = X.set_enabled
    X.set_enabled = lambda on: {"ok": False, "err": "Couldn't save the crosshair settings."}
    try:
        with _record_toasts() as toasts:
            icon, item = _tray_crosshair_item()
            _click(icon, item, 1)
            X.set_enabled = lambda on: {"ok": False}
            _click(icon, item, 2)
        assert toasts == [("Crosshair didn't start", "Couldn't save the crosshair settings."),
                          ("Crosshair didn't start", "Couldn't change the crosshair setting.")], toasts
    finally:
        X.set_enabled = saved
        X.stop()


def test_crosshair_routes_never_start_a_thread_that_outlives_a_stop():
    c, _ = _fresh()
    try:
        _post(c, "/api/crosshair/settings", {"enabled": True})
        A._crosshair_allow_start()
        assert m.wait(xo.is_running)
    finally:
        X.stop()
        X._ui_ready = False
    names = {t.name for t in threading.enumerate()}
    assert "crosshair-overlay" not in names and "crosshair-fs-watch" not in names, names
