"""app.py - the Adaptive Tuning routes, the note every change on the GPU page
leaves first, and the order of the app's own start and quit.

Three kinds of test:

  * the wiring alone, with stand-ins for the engine's calls: every route is
    there with its method, passes on exactly what it was sent, refuses a body
    that is not what it expects with a 400 before the engine is asked anything,
    and the routes of the old tuner are gone;
  * the routes against the real engine (core/adaptive_tuning.py) on a stand-in
    graphics card, set up the way tests/test_adaptive_tuning.py does it: the
    answers have the keys the page was built against, and a change made on the
    GPU page during a game is noted BEFORE the card or the profile is written,
    so the game's session leaves the card alone from then on;
  * the start and the quit: a session a dead run left behind is settled before
    anything else can touch the card, then ONE task applies the saved
    overclock, says that this is over and only then starts the game monitor.

Nothing here touches a real clock, registry or process.  The sandbox is in
place before app.py is imported, the card is a stand-in behind the real
core.gpu_overclock, time only moves when a test moves it, and the watch thread
does not run: a test calls poll() where the thread would wake.
"""
import sandbox as m

import ast
import contextlib
import functools
import inspect
import json
import os
import re
import threading
import types
import unittest

import config

GO = m.app("core.gpu_overclock")
AT = m.app("core.adaptive_tuning")
GP = m.app("core.game_profiles")
from core import adaptive_policy as P, capabilities as CAP, gpu_events as GE, notifier as NOTE    # noqa: E402
from core import crash_recovery as CR, temp_ceiling as TC, utils as U                              # noqa: E402

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = {"Host": f"127.0.0.1:{config.APP_PORT}"}
EXE, NAME, PID = "cod.exe", "Call of Duty", 4242
T0 = 1_794_571_200.0                    # 13 Nov 2026 12:00 UTC, as in the UI fixtures
FULL, L75, L50, NONE = (450, 2225), (338, 1669), (225, 1113), (0, 0)
MINE = "adaptive_tuning"
EM_DASH = chr(0x2014)
BANNED = re.compile(r"(?i:offset|tdr|blacklist|baseline|yielded|stock|strike)|\bAT\b")
LIMITS = {"core_max_mhz": 2900, "mem_max_mhz": 14000, "power_default_w": 300, "power_max_w": 350,
          "power_min_w": 150}
NVIDIA = {"has_nvidia": True, "gpu_name": "NVIDIA GeForce RTX 5070 Ti", "gpu_vendor": "NVIDIA"}

NOT_AN_OBJECT = "Expected a JSON object."
BAD_REQUEST = "Adaptive Tuning did not understand that request."
FAILED = "Adaptive Tuning could not do that. Try again."

# The seven routes of spec section 7, and a body each POST accepts.
ROUTES = {
    "/api/adaptive/state": "GET",
    "/api/adaptive/settings": "POST",
    "/api/adaptive/game": "POST",
    "/api/adaptive/game/step": "POST",
    "/api/adaptive/game/forget": "POST",
    "/api/adaptive/reset": "POST",
    "/api/adaptive/note/dismiss": "POST",
}
VALID = {
    "/api/adaptive/settings": {"enabled": True},
    "/api/adaptive/game": {"exe": EXE, "enabled": False},
    "/api/adaptive/game/step": {"exe": EXE, "to": "slower"},
    "/api/adaptive/game/forget": {"exe": EXE},
    "/api/adaptive/reset": {},
    "/api/adaptive/note/dismiss": {},
}
ENGINE_CALLS = ("get_state", "set_settings", "set_game_enabled", "step_game", "forget_game", "reset_all",
                "dismiss_note")
# The 23 routes of the old tuner.  Two of the paths live on with a new meaning.
OLD_ADAPTIVE = ("status", "settings", "games", "game-toggle", "reset", "reseed", "history", "game-mode",
                "game-target", "apply-best", "clear-blacklist", "pause", "resume", "cross-blacklist",
                "clear-cross-blacklist", "diagnostics", "gameplay-state", "gameplay-baselines",
                "gameplay-recalibrate", "gameplay-reset", "menu-signature/add", "menu-signature/remove",
                "game-knobs")
OLD_OTHER = ("/api/library/enable_at", "/api/library/enable_at_bulk", "/api/frame-telemetry/status",
             "/api/frame-telemetry/start", "/api/frame-telemetry/stop", "/api/frame-telemetry/install",
             "/api/frame-telemetry/install/progress")
_UNSET = object()


def client():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A.app.test_client()


def auth(**extra):
    return {**HOST, A._API_TOKEN_HEADER: A._API_TOKEN, **extra}


def get(c, route):
    r = c.get(route, headers=auth())
    return r.status_code, r.get_json()


def post(c, route, body=_UNSET, raw=None):
    """POST a JSON body, or `raw` bytes as they are; no body at all when neither is given."""
    if raw is not None:
        r = c.post(route, headers=auth(), data=raw)
    elif body is _UNSET:
        r = c.post(route, headers=auth())
    else:
        r = c.post(route, headers=auth(), json=body)
    return r.status_code, r.get_json()


def delete(c, route):
    r = c.delete(route, headers=auth())
    return r.status_code, r.get_json()


@contextlib.contextmanager
def swapped(obj, **attrs):
    """Replace attributes of a module for a with block, and put them back."""
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def fake_engine(answer=None):
    """Stand-ins for the engine's calls.  Yields the list of calls made, as
    (name, args, kwargs).  `answer`: what every call returns (an exception is
    raised instead); left out, {"ok": True, "state": {"ok": True, "from": name}}."""
    made = []

    def stand_in(name):
        def call(*a, **k):
            made.append((name, a, k))
            if isinstance(answer, Exception):
                raise answer
            return answer if answer is not None else {"ok": True, "state": {"ok": True, "from": name}}
        return call

    with swapped(AT, **{name: stand_in(name) for name in ENGINE_CALLS}):
        yield made


def source():
    with open(A.__file__, encoding="utf-8") as f:
        return f.read()


def rules():
    return {(r.rule, method) for r in A.app.url_map.iter_rules() for method in r.methods
            if method in ("GET", "POST", "PUT", "DELETE")}


# ═════════════════════════════════════════════════════════════════════
# The route table
# ═════════════════════════════════════════════════════════════════════
def test_the_app_and_this_file_share_the_sandboxed_modules():
    client()
    assert A.adaptive_tuning is AT and A.gpu_overclock is GO and A.game_profiles is GP
    assert A.config.APPDATA_DIR.startswith(m.ROOT)
    assert GE._api is m.EVENTS, "a route test must never read the real event log"


def test_the_seven_routes_are_there_with_their_methods():
    client()
    found = {(rule, method) for rule, method in rules() if rule.startswith("/api/adaptive")}
    assert found == set(ROUTES.items()), sorted(found ^ set(ROUTES.items()))


def test_the_old_tuners_routes_are_gone():
    c = client()
    assert len(OLD_ADAPTIVE) == 23
    gone = ["/api/adaptive/" + name for name in OLD_ADAPTIVE if name not in ("settings", "reset")] + list(OLD_OTHER)
    with fake_engine() as made:
        for path in gone:
            for send in (c.get, c.post):
                r = send(path, headers=auth())
                assert r.status_code == 404 and r.get_json()["ok"] is False, (path, r.status_code)
        r = c.get("/api/adaptive/settings", headers=auth())     # the old GET of the settings
        assert r.status_code == 405, r.status_code
        assert c.get("/api/adaptive/reset", headers=auth()).status_code == 405
    assert made == []
    for rule, _method in rules():
        assert not any(word in rule for word in ("gameplay", "menu-signature", "enable_at", "frame-telemetry",
                                                 "blacklist/cross", "cross-blacklist", "game-knobs")), rule
    kept = {rule for rule, _method in rules()}
    assert {"/api/library", "/api/library/scan", "/api/profiles/add-game"} <= kept, "Installed Games still works"


def test_the_app_asks_the_engine_only_for_what_it_has():
    client()
    src = source()
    tree = ast.parse(src)
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "adaptive_tuning"}
    assert used == set(ENGINE_CALLS) | {"note_manual_change", "end_active_session", "recover_interrupted_session",
                                        "clear_runtime_if_orphaned", "judging_driver_errors"}, sorted(used)
    for name in used:
        assert callable(getattr(AT, name, None)), f"core/adaptive_tuning.py has no {name}()"
    assert "gameplay_state" not in src, "that module went with the old tuner"
    assert re.findall(r"frame_telemetry\.(\w+)", src) == ["stop_tracking"], "only the quit path still stops it"


def test_the_api_guard_covers_the_new_routes():
    c = client()
    with fake_engine() as made:
        for path, method in ROUTES.items():
            send = c.get if method == "GET" else c.post
            assert send(path, headers=HOST).status_code == 403, path                        # no token
            assert send(path, headers=auth(Origin="https://evil.example")).status_code == 403, path
            assert send(path, headers={"Host": "evil.example", A._API_TOKEN_HEADER: A._API_TOKEN}).status_code == 403
    assert made == [], "refused before anything was asked"


# ═════════════════════════════════════════════════════════════════════
# The wiring: what each route passes on, and what it sends back
# ═════════════════════════════════════════════════════════════════════
def test_state_is_the_engines_answer_as_it_is():
    c = client()
    answer = {"ok": True, "enabled": False, "session": None, "games": [], "history": []}
    with fake_engine(answer) as made:
        assert get(c, "/api/adaptive/state") == (200, answer)
    assert made == [("get_state", (), {})]
    failed = {"ok": False, "err": AT.ERR_STATE}
    with fake_engine(failed):
        assert get(c, "/api/adaptive/state") == (200, failed), "the page draws its own 'could not load' from this"


def test_each_route_passes_on_exactly_what_it_was_sent():
    """The bodies are the ones the page sends (tests/test_at2_ui.py pins them)."""
    c = client()
    nothing = {"enabled": None, "notify": None, "sensitivity": None}
    cases = [
        ("/api/adaptive/settings", {"enabled": True}, ("set_settings", (), {**nothing, "enabled": True})),
        ("/api/adaptive/settings", {"enabled": False}, ("set_settings", (), {**nothing, "enabled": False})),
        ("/api/adaptive/settings", {"notify": False}, ("set_settings", (), {**nothing, "notify": False})),
        ("/api/adaptive/settings", {"sensitivity": "strict"}, ("set_settings", (), {**nothing, "sensitivity": "strict"})),
        ("/api/adaptive/settings", {"enabled": True, "notify": True, "sensitivity": "watch"},
         ("set_settings", (), {"enabled": True, "notify": True, "sensitivity": "watch"})),
        ("/api/adaptive/settings", {"sensitivity": "fast"},                 # a value is the engine's to refuse
         ("set_settings", (), {**nothing, "sensitivity": "fast"})),
        ("/api/adaptive/game", {"exe": "cs2.exe", "enabled": False}, ("set_game_enabled", ("cs2.exe", False), {})),
        ("/api/adaptive/game", {"exe": "cs2.exe", "enabled": True}, ("set_game_enabled", ("cs2.exe", True), {})),
        ("/api/adaptive/game/step", {"exe": EXE, "to": "faster"}, ("step_game", (EXE, "faster"), {"confirm": False})),
        ("/api/adaptive/game/step", {"exe": EXE, "to": "faster", "confirm": True},
         ("step_game", (EXE, "faster"), {"confirm": True})),
        ("/api/adaptive/game/step", {"exe": EXE, "to": "full", "confirm": False},
         ("step_game", (EXE, "full"), {"confirm": False})),
        ("/api/adaptive/game/step", {"exe": "r5apex.exe", "to": "slower"},
         ("step_game", ("r5apex.exe", "slower"), {"confirm": False})),
        ("/api/adaptive/game/step", {"exe": EXE, "to": "up"}, ("step_game", (EXE, "up"), {"confirm": False})),
        ("/api/adaptive/game/forget", {"exe": EXE}, ("forget_game", (EXE,), {})),
        ("/api/adaptive/reset", {}, ("reset_all", (), {})),
        ("/api/adaptive/note/dismiss", {}, ("dismiss_note", (), {})),
    ]
    for path, body, want in cases:
        with fake_engine() as made:
            status, answer = post(c, path, body)
        assert status == 200 and answer == {"ok": True, "state": {"ok": True, "from": want[0]}}, (path, body, answer)
        assert made == [want], (path, body, made)
    assert {path for path, _b, _w in cases} == set(VALID), "every POST route is in the table"


def test_a_no_from_the_engine_comes_back_whole_and_with_200():
    """The page reads "ok", "err", "need_confirm" and "text" from the body; a
    5xx would also open its error report."""
    c = client()
    question = {"ok": False, "need_confirm": True,
                "text": "Call of Duty had graphics driver errors at full overclock in the last 30 days. Try it again?"}
    with fake_engine(question):
        assert post(c, "/api/adaptive/game/step", {"exe": EXE, "to": "faster"}) == (200, question)
    no = {"ok": False, "err": AT.ERR_NO_GAME}
    with fake_engine(no):
        for path, body in VALID.items():
            assert post(c, path, body) == (200, no), path


def test_a_body_that_is_not_a_json_object_is_refused_before_the_engine_is_asked():
    c = client()
    not_objects = (b"hello", b"[]", b'["cod.exe"]', b'[{"exe": "cod.exe"}]', b'"cod.exe"', b"5", b"null", b"true",
                   b"{", b"{'exe': 'cod.exe'}", b"exe=cod.exe&enabled=true")
    refusal = (400, {"ok": False, "err": NOT_AN_OBJECT})
    with fake_engine() as made:
        for path in VALID:
            for raw in not_objects:
                assert post(c, path, raw=raw) == refusal, (path, raw)
            assert post(c, path) == refusal, (path, "no body at all")
    assert made == []


def test_a_field_that_is_missing_unknown_or_of_the_wrong_type_is_refused():
    """Nothing is carried out in part, and nothing is guessed: "yes" is not
    true, null is not "leave it alone", and a body made for an older version
    of a route is not taken for something else."""
    c = client()
    wrong = {
        "/api/adaptive/settings": [
            {}, {"enabled": "yes"}, {"enabled": "true"}, {"enabled": 1}, {"enabled": 0}, {"enabled": None},
            {"notify": "no"}, {"notify": 1}, {"notify": None}, {"sensitivity": 2}, {"sensitivity": None},
            {"sensitivity": ""}, {"sensitivity": "  "}, {"sensitivity": ["strict"]}, {"sensitivity": True},
            {"enabled": True, "aggressive": True}, {"enabled": True, "exe": EXE}, {"enabled": True, "notify": "x"},
            {"enabled_globally": True}, {"dry_run": False}, {"mode": "competitive"},
        ],
        "/api/adaptive/game": [
            {}, {"exe": EXE}, {"enabled": True}, {"exe": "", "enabled": True}, {"exe": "   ", "enabled": True},
            {"exe": 5, "enabled": True}, {"exe": None, "enabled": True}, {"exe": [EXE], "enabled": True},
            {"exe": EXE, "enabled": "true"}, {"exe": EXE, "enabled": 1}, {"exe": EXE, "enabled": None},
            {"exe": EXE, "enabled": True, "mode": "competitive"},
        ],
        "/api/adaptive/game/step": [
            {}, {"exe": EXE}, {"to": "full"}, {"exe": "", "to": "full"}, {"exe": EXE, "to": ""},
            {"exe": EXE, "to": 1}, {"exe": EXE, "to": None}, {"exe": 7, "to": "full"},
            {"exe": EXE, "to": "full", "confirm": "yes"}, {"exe": EXE, "to": "full", "confirm": 1},
            {"exe": EXE, "to": "full", "confirm": None}, {"exe": EXE, "to": "full", "level": 0},
        ],
        "/api/adaptive/game/forget": [
            {}, {"exe": None}, {"exe": ""}, {"exe": 3}, {"exe": EXE, "all": True}, {"name": NAME},
        ],
        "/api/adaptive/reset": [{"exe": EXE}, {"all": True}, {"exe": None}],
        "/api/adaptive/note/dismiss": [{"note": "upgrade"}],
    }
    assert set(wrong) == set(VALID)
    with fake_engine() as made:
        for path, bodies in wrong.items():
            for body in bodies:
                assert post(c, path, body) == (400, {"ok": False, "err": BAD_REQUEST}), (path, body)
    assert made == [], made


def test_the_old_per_game_reset_can_never_wipe_everything():
    """Up to 3.7.0 POST /api/adaptive/reset with {exe} reset one game.  The
    path now forgets everything, so that body must not get through."""
    c = client()
    with fake_engine() as made:
        assert post(c, "/api/adaptive/reset", {"exe": EXE})[0] == 400
        assert made == []
        assert post(c, "/api/adaptive/reset", {})[0] == 200
    assert made == [("reset_all", (), {})]


def test_a_call_that_raises_is_answered_in_plain_words():
    c = client()
    U.clear_log_buffer()
    with fake_engine(RuntimeError("boom: 'NoneType' object is not subscriptable")) as made:
        for path, method in ROUTES.items():
            answer = get(c, path) if method == "GET" else post(c, path, VALID[path])
            assert answer == (500, {"ok": False, "err": FAILED}), (path, answer)
    assert len(made) == len(ROUTES)
    logged = [e["msg"] for e in U.get_log_buffer() if "raised: boom" in e["msg"]]
    assert len(logged) == len(ROUTES) and "Traceback" in logged[0], "the log has what the page does not show"


def test_what_the_routes_say_themselves_is_plain():
    client()
    assert (A._ADAPTIVE_NOT_AN_OBJECT, A._ADAPTIVE_BAD_REQUEST, A._ADAPTIVE_FAILED) == (NOT_AN_OBJECT, BAD_REQUEST,
                                                                                         FAILED)
    tree = ast.parse(source())
    mine = ("_note_manual_overclock_change", "_adaptive_body", "_adaptive_answer",
            "_boot_saved_overclock_then_game_monitor")
    texts_ = [NOT_AN_OBJECT, BAD_REQUEST, FAILED]
    seen = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and (fn.name in mine or fn.name.startswith("api_adaptive_")):
            seen.add(fn.name)
            texts_ += [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert seen == set(mine) | {"api_adaptive_" + n for n in ("state", "settings", "game", "game_step", "game_forget",
                                                              "reset", "note_dismiss")}, sorted(seen)
    for text in texts_:
        assert EM_DASH not in text and not BANNED.search(text), text


# ═════════════════════════════════════════════════════════════════════
# The real engine behind the routes, on a stand-in card
# ═════════════════════════════════════════════════════════════════════
class Card:
    """The graphics card as the app sees it through its clock interface."""

    def __init__(self, current=(0, 0)):
        self.current = tuple(current)
        self.writes = []
        self.on_write = None

    def is_available(self):
        return {"ok": True, "err": ""}

    def get_current_offsets(self):
        return {"ok": True, "core_offset_mhz": self.current[0], "mem_offset_mhz": self.current[1]}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        if self.on_write:
            self.on_write((core_offset_mhz, mem_offset_mhz))
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        self.current = (core_offset_mhz, mem_offset_mhz)
        return {"ok": True, "struct_version_used": 3, "err": ""}

    def force_reset_all(self):
        if self.on_write:
            self.on_write(("reset",))
        self.writes.append(("reset",))
        self.current = (0, 0)
        return {"ok": True, "steps": [{"name": "Graphics P-state freqDelta -> 0", "ok": True, "err": ""}]}


class Clock:
    """Time for the engine, the event log reader and gpu_overclock: it only
    moves when a test moves it."""

    def __init__(self):
        self.now = T0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


clock = Clock()
toasts: list = []
TRACKED = [None]            # what game mode says it is tracking

_REAL_GO = {n: getattr(GO, n) for n in (
    "detect_gpu_oc_capability", "detect_oc_tools_running", "verify_oc_applied", "_nvapi", "time",
    "_oc_watchdog_thread", "tuning_windows_since")}
_REAL_AT = {n: getattr(AT, n) for n in ("_now", "_pause", "_defer", "_defer_later", "_tracked_game",
                                        "_exe_running", "_WATCH_THREAD", "_uptime_s")}
_REAL_CAP = CAP.detect
_REAL_TOAST = NOTE.toast


def restore():
    """Everything rig() replaces, back as it was, and no session left open."""
    AT._defer = lambda fn: None
    if AT._sess is not None:
        AT._sess["stop"].set()
        with AT._state_lock:
            AT._sess = None
    for name, value in _REAL_GO.items():
        setattr(GO, name, value)
    for name, value in _REAL_AT.items():
        setattr(AT, name, value)
    CAP.detect = _REAL_CAP
    NOTE.toast = _REAL_TOAST
    GO._deactivate_oc_watchdog()
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    for run in (GO._stability_session, GO._auto_oc_session, GO._benchmark_oc_session, GO._benchmark_state):
        run["active"] = False
    GO.clear_crash_state()
    GO._tuning_window.update(open=False, start=0.0, last=0.0, saved=0.0)
    GO._timeline_pending.clear()
    GO._startup_apply_done.clear()
    del GO._tuning_windows_unsaved[:]
    with GO._external_write_lock:
        GO._external_write_seq = 0
        GO._ceiling_reset_seq = 0
    TC._state.update(in_cooldown_until=None, last_trip_at=None, last_trip_action=None, above_since=None)
    AT._last_game = AT._last_closed = None
    AT._recheck_scheduled[0] = False
    AT._burst_tail.clear()
    AT._failed_reads[0] = 0
    AT._supported_cache.update(ok=None, reason="", at=0.0)
    AT._health_cache.update(at=None, value=None)
    AT._startup_waited[0] = False
    GP._engine.update(monitoring=False, gaming_mode=False)
    TRACKED[0] = None
    del toasts[:]


def rig(profile=(450, 2225, 100), apply_on_startup=True, enabled=True, held=None, startup_done=True):
    """A clean machine with an NVIDIA card the app can write (the stand-in), a
    saved overclock, and Adaptive Tuning switched on.  `held`: what the app
    itself holds on the card when the test starts (None: nothing)."""
    restore()
    m.fresh()
    clock.now = T0
    m.CMD_HOOKS.append(lambda a: m.R() if a[0] == "nvidia-smi" else None)
    GO.time = clock
    GO.detect_gpu_oc_capability = lambda: {"ok": True, "vendor": "nvidia", "limits": dict(LIMITS)}
    GO.detect_oc_tools_running = lambda: {"conflict_likely": False, "tools": []}
    GO.verify_oc_applied = lambda *a, **k: {"verified": True, "warnings": []}
    GO._oc_watchdog_thread = types.SimpleNamespace(is_alive=lambda: True)    # no thread: nothing re-asserts by itself
    GO._nvapi = Card()
    if startup_done:
        GO.mark_startup_apply_done()
    GO.tuning_windows_since = lambda: T0 - 400 * 86400.0        # the app has kept a record of its tests for a year
    CAP.detect = lambda force=False: dict(NVIDIA)
    NOTE.toast = lambda title, text, force=False: toasts.append((title, text, force)) or {"ok": True}
    AT._now = clock.time
    GE._now = clock.time
    AT._pause = lambda seconds: None
    AT._defer = lambda fn: fn()
    AT._defer_later = lambda seconds, fn: None
    AT._tracked_game = lambda: TRACKED[0]
    AT._uptime_s = lambda: 30 * 86400.0                         # Windows has been up for a month
    AT._WATCH_THREAD = False
    if profile is not None:
        r = GO.save_profile({"core_offset_mhz": profile[0], "mem_offset_mhz": profile[1], "power_pct": profile[2],
                             "apply_on_startup": apply_on_startup})
        assert r["ok"], r
    if held is not None:
        GO.apply_oc(core_offset_mhz=held[0], mem_offset_mhz=held[1], power_pct=held[2] if len(held) > 2 else 100)
        del GO._nvapi.writes[:]
    with AT._state_lock:
        AT._save_settings(dict(AT._default_settings(), enabled=enabled))
    return GO._nvapi


def engine_test(fn):
    """A test that runs the real engine: skipped without Flask, and whatever
    it replaced is put back afterwards."""
    @functools.wraps(fn)
    def run():
        c = client()
        try:
            fn(c)
        finally:
            restore()
    return run


def start(exe=EXE, name=NAME, pid=PID):
    m.proc(pid, exe=exe)
    return AT.on_game_start(exe, name, pid)


def poll(seconds=20.0):
    """What the watch thread does when it wakes, `seconds` later."""
    clock.now += seconds
    s = AT._sess
    assert s is not None, "no session to poll"
    AT._poll(s["id"])


def close(exe=EXE, pid=PID):
    """The game closes and game mode reports it."""
    m.exit_proc(pid)
    return AT.on_game_exit(exe, exit_ts=clock.now)


def die():
    """Vispora is killed mid-game: no session end runs, and a new process holds nothing."""
    s = AT._sess
    s["stop"].set()
    with AT._state_lock:
        AT._sess = None
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    GO._deactivate_oc_watchdog()
    GO._startup_apply_done.clear()
    AT._startup_waited[0] = False
    AT._last_closed = None
    return s


def put_game(exe=EXE, name=NAME, **over):
    """A game Adaptive Tuning already has a record of."""
    with AT._state_lock:
        st = AT._load_state()
        g = P.new_game(exe, name, clock.now - 86400)
        g.update(over)
        st["games"][exe] = g
        AT._save_state(st)
    return g


def game(exe=EXE):
    with AT._state_lock:
        return AT._load_state()["games"].get(exe)


def texts():
    with AT._state_lock:
        return [h["text"] for h in AT._load_state()["history"]]


def session_file():
    try:
        with open(AT.session_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def saved_profile():
    p = GO.load_profile()
    return (p["core_offset_mhz"], p["mem_offset_mhz"], p["power_pct"], p["apply_on_startup"]) if p.get("exists") else None


def state(c):
    status, answer = get(c, "/api/adaptive/state")
    assert status == 200 and answer["ok"] is True, (status, answer)
    return answer


def ok(c, route, body):
    status, answer = post(c, route, body)
    assert status == 200 and answer["ok"] is True and answer["state"]["ok"] is True, (route, body, status, answer)
    return answer["state"]


@engine_test
def test_the_state_route_has_exactly_the_keys_the_page_was_built_against(c):
    with open(os.path.join(HERE, "fixtures", "at2_states.json"), encoding="utf-8") as f:
        fixtures = json.load(f)
    rig()
    put_game(level=1, last_problem={"ts": T0 - 86400, "kind": "ease", "text": P.EASED(NAME, 0, 1)})
    AT._add_history(EXE, NAME, P.EASED(NAME, 0, 1), T0 - 86400)
    idle = state(c)
    # The fix round added "monitor_starting" (top level), "log_readable" (health)
    # and "warning_open" / "eased_by_driver" (each game's row).
    assert "monitor_starting" in idle and "log_readable" in idle["health"], sorted(idle)
    assert set(idle) == set(fixtures["on_idle"]) and len(idle) == 16
    assert idle["session"] is None and idle["game"] is None and idle["enabled"] is True
    assert idle["monitor_starting"] is False and isinstance(idle["health"]["log_readable"], bool)
    for part in ("supported", "limit", "health"):
        assert set(idle[part]) == set(fixtures["on_idle"][part]), part
    assert idle["limit"] == {"has_profile": True, "core": 450, "mem": 2225, "power": 100, "apply_on_startup": True}
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start()["engaged"] is True
    playing = state(c)
    want = fixtures["in_game_75"]
    assert set(playing) == set(want)
    for part in ("session", "game"):
        assert set(playing[part]) == set(want[part]), part
    assert {"warning_open", "eased_by_driver"} <= set(playing["games"][0]), sorted(playing["games"][0])
    assert set(playing["games"][0]) == set(want["games"][0]) and len(playing["games"][0]) == 24
    assert set(playing["history"][0]) == set(want["history"][0])
    assert (playing["session"]["level"], playing["session"]["core"], playing["session"]["mem"]) == (1, *L75)
    assert playing["game"] == {"exe": EXE, "name": NAME, "looked_after": True, "reason": ""}


def test_the_installed_games_route_carries_display_names():
    c = client()
    m.fresh()
    LS = m.app("core.library_scanner")
    assert LS.atomic_write_json(LS.CACHE_FILE, {"games": [
        {"title": "Call of Duty- Modern Warfare 3", "exe": "cod23-cod.exe", "launcher": "Xbox", "known": True},
        {"title": "Rocket League®", "exe": "launcher.exe", "launcher": "Epic", "known": True}],
        "scanned_at": T0, "total": 2, "known": 2})
    status, answer = get(c, "/api/library")
    assert status == 200 and [(g["title"], g["display_name"]) for g in answer["games"]] == [
        ("Call of Duty- Modern Warfare 3", "Call of Duty: Modern Warfare 3"),
        ("Rocket League®", "Rocket League")], answer


def test_the_dashboard_row_says_what_get_state_says():
    """The dashboard's one pointer to the card.  It said "Off" with a green
    tick after the update had switched the old one off, counted games the
    user set lower or switched off as "eased", and warned about errors the
    card itself says the overclock may not be the cause of."""
    from core import health_audit as HA
    base = {"ok": True, "supported": {"ok": True}, "limit": {"has_profile": True, "core": 450, "mem": 2225}}

    def row(**state):
        r = HA._adaptive_tuning_row(dict(base, **state))
        return r["severity"], r["current"]

    rows = []
    rows.append(row(enabled=False, upgrade_note=True, old_switch_was_on=True, health={"errors_30d": 3}))
    assert rows[-1] == ("warn", "Off since the update. It now covers every game: turn it on again under Profiles.")
    rows.append(row(enabled=False, upgrade_note=True, old_switch_was_on=False, health={"errors_30d": 0}))
    assert rows[-1] == ("ok", "Off"), "the old one was off too: nothing to point at"
    rows.append(row(enabled=False, upgrade_note=False, old_switch_was_on=True, health={}))
    assert rows[-1] == ("ok", "Off"), "the note was read"
    assert row(enabled=True, upgrade_note=True, old_switch_was_on=True, games=[])[0] == "ok", "on again"

    games = [{"enabled": True, "level": 1, "eased_by_driver": True},
             {"enabled": True, "level": 1, "eased_by_driver": False},       # the user set it lower
             {"enabled": False, "level": 2, "eased_by_driver": True},       # switched off: not looked after
             {"enabled": True, "level": 0, "eased_by_driver": False},
             {"enabled": True, "level": 3}]                                 # (a row from before the key)
    rows.append(row(enabled=True, games=games))
    assert rows[-1] == ("ok", "On, 1 game eased"), rows[-1]
    rows.append(row(enabled=True, games=games + [dict(games[0])]))
    assert rows[-1] == ("ok", "On, 2 games eased")
    assert row(enabled=True, games=games[1:]) == ("ok", "On")

    rows.append(row(enabled=False, health={"errors_30d": 2, "at_stock_90d": 1}))
    assert rows[-1] == ("ok", "Off. The graphics driver logged errors 2 times in the last 30 days. "
                              "It also did that with no overclock on the card."), rows[-1]
    rows.append(row(enabled=False, health={"errors_30d": 1, "at_stock_90d": 0}))
    assert rows[-1] == ("warn", "Off. The graphics driver logged errors 1 time in the last 30 days.")
    assert row(enabled=False, health={"errors_30d": 1, "at_stock_90d": None})[0] == "warn", "not known: as before"
    # the count only covers the days since the app kept a record of its tests: the sentence says how many
    assert row(enabled=False, health={"errors_30d": 2, "at_stock_90d": 0, "covered_days": 3}) == (
        "warn", "Off. The graphics driver logged errors 2 times in the last 3 days.")
    assert row(enabled=False, health={"errors_30d": 1, "at_stock_90d": 1, "covered_days": 1}) == (
        "ok", "Off. The graphics driver logged errors 1 time in the last day. "
              "It also did that with no overclock on the card.")
    assert row(enabled=False, health={"errors_30d": 1, "covered_days": 30})[1].endswith("in the last 30 days.")
    assert row(enabled=False, health={"errors_30d": 1, "covered_days": "x"})[1].endswith("in the last 30 days.")
    assert row(enabled=False, health={"errors_30d": "x", "at_stock_90d": "y"}) == ("ok", "Off")
    for _severity, text in rows:
        assert EM_DASH not in text and not BANNED.search(text), text


@engine_test
def test_the_settings_route_saves_what_it_is_sent_and_nothing_else(c):
    rig()
    after = ok(c, "/api/adaptive/settings", {"notify": False})
    assert (after["enabled"], after["notify"], after["sensitivity"]) == (True, False, "balanced")
    after = ok(c, "/api/adaptive/settings", {"sensitivity": "strict"})
    assert (after["enabled"], after["notify"], after["sensitivity"]) == (True, False, "strict")
    after = ok(c, "/api/adaptive/settings", {"enabled": False})
    assert (after["enabled"], after["notify"], after["sensitivity"]) == (False, False, "strict")
    assert post(c, "/api/adaptive/settings", {"sensitivity": "fast"}) == (200, {"ok": False, "err": AT.ERR_SETTING})
    assert post(c, "/api/adaptive/settings", {"enabled": "on"})[0] == 400
    now = state(c)
    assert (now["enabled"], now["notify"], now["sensitivity"]) == (False, False, "strict"), "a refusal changed nothing"
    with open(AT.settings_path(), encoding="utf-8") as f:
        on_disk = json.load(f)
    assert (on_disk["enabled"], on_disk["notify"], on_disk["sensitivity"]) == (False, False, "strict")


@engine_test
def test_the_main_switch_takes_on_the_running_game_and_hands_the_card_back(c):
    card = rig(enabled=False, held=(100, 500))
    m.proc(PID, exe=EXE)
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert state(c)["session"] is None and card.writes == []
    on = ok(c, "/api/adaptive/settings", {"enabled": True})
    assert on["enabled"] is True and on["session"]["exe"] == EXE and on["session"]["level"] == 0
    assert card.current == FULL and AT.session_active() is True
    off = ok(c, "/api/adaptive/settings", {"enabled": False})
    assert off["enabled"] is False and off["session"] is None
    assert card.current == (100, 500) and session_file() is None, "what was on the card before the game is back"
    n = len(card.writes)
    assert ok(c, "/api/adaptive/settings", {"notify": False})["session"] is None and card.writes[n:] == []


@engine_test
def test_a_games_own_switch_through_the_route(c):
    card = rig(held=(100, 500))
    assert post(c, "/api/adaptive/game", {"exe": EXE, "enabled": False}) == (200, {"ok": False, "err": AT.ERR_NO_GAME})
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start()["engaged"] is True and card.current == FULL
    off = ok(c, "/api/adaptive/game", {"exe": EXE, "enabled": False})
    assert off["session"] is None and off["games"][0]["enabled"] is False
    assert off["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "switched_off"}
    assert card.current == (100, 500), "the card was handed back"
    on = ok(c, "/api/adaptive/game", {"exe": EXE, "enabled": True})
    assert on["games"][0]["enabled"] is True and on["session"]["exe"] == EXE and card.current == FULL


@engine_test
def test_steps_through_the_route_and_the_question_before_a_level_that_failed(c):
    card = rig()
    put_game(level=1, changes=[{"ts": T0 - 5 * 86400, "from_level": 0, "to_level": 1, "by": "driver",
                                "hours_at_from_level": 30.0, "text": "x"}])
    assert start()["level"] == 1 and card.current == L75
    status, answer = post(c, "/api/adaptive/game/step", {"exe": EXE, "to": "faster"})
    assert status == 200 and answer["ok"] is False and answer["need_confirm"] is True
    assert answer["text"] == f"{NAME} had graphics driver errors at full overclock in the last 30 days. Try it again?"
    assert set(answer) == {"ok", "need_confirm", "text"}
    assert game()["level"] == 1 and card.current == L75, "nothing moved without a yes"
    status, answer = post(c, "/api/adaptive/game/step", {"exe": EXE, "to": "faster", "confirm": False})
    assert answer.get("need_confirm") is True and card.current == L75, "false is not a yes"

    down = ok(c, "/api/adaptive/game/step", {"exe": EXE, "to": "slower"})       # down is always allowed
    assert down["games"][0]["level"] == 2 and down["session"]["level"] == 2 and card.current == L50
    up = ok(c, "/api/adaptive/game/step", {"exe": EXE, "to": "faster"})         # 75% is not where it failed
    assert up["games"][0]["level"] == 1 and card.current == L75
    full = ok(c, "/api/adaptive/game/step", {"exe": EXE, "to": "full", "confirm": True})
    assert full["games"][0]["level"] == 0 and full["session"]["level"] == 0 and card.current == FULL
    assert max(w for w in card.writes if w != ("reset",)) == FULL, "never above what was saved"

    assert post(c, "/api/adaptive/game/step", {"exe": EXE, "to": "up"}) == (200, {"ok": False, "err": AT.ERR_STEP})
    assert post(c, "/api/adaptive/game/step", {"exe": "nope.exe", "to": "slower"}) == \
        (200, {"ok": False, "err": AT.ERR_NO_GAME})
    assert saved_profile() == (450, 2225, 100, True), "no route of Adaptive Tuning writes the saved overclock"


@engine_test
def test_forget_reset_and_the_note_through_the_routes(c):
    card = rig(held=(100, 500))
    with AT._state_lock:
        AT._save_settings(dict(AT._default_settings(), enabled=True, upgrade_note=True, old_switch_was_on=True))
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=1)
    put_game(level=2)
    AT._add_history(EXE, NAME, "something happened")
    assert start()["level"] == 2 and card.current == L50

    before = state(c)
    assert before["upgrade_note"] is True and before["old_switch_was_on"] is True
    after = ok(c, "/api/adaptive/note/dismiss", {})
    assert after["upgrade_note"] is False and after["old_switch_was_on"] is True and after["enabled"] is True

    after = ok(c, "/api/adaptive/game/forget", {"exe": EXE})
    assert [g["exe"] for g in after["games"]] == ["cs2.exe"] and after["session"] is None
    assert card.current == (100, 500), "its open session was closed and the card handed back"
    assert post(c, "/api/adaptive/game/forget", {"exe": EXE}) == (200, {"ok": False, "err": AT.ERR_NO_GAME})

    after = ok(c, "/api/adaptive/reset", {})
    assert after["games"] == [] and after["history"] == [] and after["enabled"] is True, "the settings stay"
    assert saved_profile() == (450, 2225, 100, True)


# ═════════════════════════════════════════════════════════════════════
# A change on the GPU page is noted BEFORE it is made
# ═════════════════════════════════════════════════════════════════════
# (method, route, body, the gpu_overclock call it ends in, what that call is given)
# Saving a profile is not among them: it writes no clock (see
# test_saving_a_profile_is_not_a_manual_change_and_leaves_no_note).
PROFILE_BODY = {"core_offset_mhz": 500, "mem_offset_mhz": 2400, "power_pct": 110, "apply_on_startup": False}
GPU_CHANGES = (
    ("POST", "/api/gpu/oc/apply", {"core_offset_mhz": 300, "mem_offset_mhz": 1000, "power_pct": 110},
     ("apply_oc", (), {"core_offset_mhz": 300, "mem_offset_mhz": 1000, "power_pct": 110})),
    ("POST", "/api/gpu/oc/reset", {}, ("reset_oc", (), {})),
    ("POST", "/api/gpu/oc/emergency-reset", {"reason": "webgl_context_lost"},
     ("emergency_reset_oc", (), {"reason": "webgl_context_lost"})),
    ("DELETE", "/api/gpu/oc/profile", None, ("delete_profile", (), {})),
)
GPU_CALLS = ("apply_oc", "reset_oc", "emergency_reset_oc", "save_profile", "delete_profile", "write_offsets",
             "set_profile_apply_on_startup", "start_stability_probe", "run_probe_tick", "end_stability_probe",
             "auto_oc_start", "auto_oc_next", "auto_oc_cancel", "benchmark_oc_start", "benchmark_oc_next",
             "benchmark_oc_cancel", "benchmark_start", "benchmark_record_result", "benchmark_cancel",
             "recalibrate_stock_baseline", "verify_oc_applied")
NOTE_CALL = ("note", (), {})


@contextlib.contextmanager
def fake_gpu_page(note_raises=None):
    """Stand-ins for everything the GPU page's routes call in gpu_overclock,
    and for the note.  Yields one list with all of it in the order it happened."""
    order = []

    def stand_in(name):
        def call(*a, **k):
            order.append((name, a, k))
            return {"ok": True, "by": name}
        return call

    def note():
        order.append(NOTE_CALL)
        if note_raises is not None:
            raise note_raises

    with swapped(GO, **{name: stand_in(name) for name in GPU_CALLS}), swapped(AT, note_manual_change=note):
        yield order


def send(c, method, route, body):
    if method == "DELETE":
        return delete(c, route)
    return post(c, route, body)


def test_every_route_that_writes_the_clocks_notes_it_first():
    c = client()
    for method, route, body, want in GPU_CHANGES:
        with fake_gpu_page() as order:
            status, answer = send(c, method, route, body)
        assert status == 200 and answer == {"ok": True, "by": want[0]}, (route, status, answer)
        assert order == [NOTE_CALL, want], (route, order)


def test_saving_a_profile_is_not_a_manual_change_and_leaves_no_note():
    """A save writes no clock.  The note made a game's session read "you
    changed the overclock" and leave the card alone: it was never handed
    back after the game, and a lower saved overclock was never followed."""
    c = client()
    with fake_gpu_page() as order:
        status, answer = post(c, "/api/gpu/oc/profile", PROFILE_BODY)
    assert status == 200 and answer == {"ok": True, "by": "save_profile"}, (status, answer)
    assert order == [("save_profile", (PROFILE_BODY,), {})], order
    with fake_gpu_page(note_raises=RuntimeError("the engine is broken")) as order:
        assert post(c, "/api/gpu/oc/profile", PROFILE_BODY) == (200, {"ok": True, "by": "save_profile"})
    assert order == [("save_profile", (PROFILE_BODY,), {})], order


def test_the_apply_and_emergency_reset_routes_read_their_bodies_as_before():
    c = client()
    apply_cases = (
        ({"core_offset_mhz": 120, "mem_offset_mhz": 800}, {"core_offset_mhz": 120, "mem_offset_mhz": 800, "power_pct": 100}),
        ({"core_offset_mhz": 120, "mem_offset_mhz": 800, "power_pct": None},        # a tune's step: leave the power limit
         {"core_offset_mhz": 120, "mem_offset_mhz": 800, "power_pct": None}),
        ({}, {"core_offset_mhz": 0, "mem_offset_mhz": 0, "power_pct": 100}),
        ({"core_offset_mhz": "150", "mem_offset_mhz": 900.0, "power_pct": "105"},
         {"core_offset_mhz": 150, "mem_offset_mhz": 900, "power_pct": 105}),
    )
    for body, kwargs in apply_cases:
        with fake_gpu_page() as order:
            assert post(c, "/api/gpu/oc/apply", body)[0] == 200
        assert order == [NOTE_CALL, ("apply_oc", (), kwargs)], (body, order)
    for body, raw in ((_UNSET, None), ({}, None), (None, b"[1, 2]"), (None, b"not json"), (None, b'"x"')):
        with fake_gpu_page() as order:                          # the emergency reset runs whatever the body is
            status, _answer = post(c, "/api/gpu/oc/emergency-reset", body, raw=raw)
        assert status == 200 and order == [NOTE_CALL, ("emergency_reset_oc", (), {"reason": "user_triggered"})], (raw, order)


def test_a_lost_context_the_page_reports_is_only_stepped_over_for_a_game_session():
    """The page's stress canvas outlives its test and reports every later
    reset of the graphics driver.  The reset it asks for is carried out as it
    always was, except while Adaptive Tuning looks after a game (which counts
    the error itself) and no test is running."""
    c = client()
    route, lost = "/api/gpu/oc/emergency-reset", {"reason": "webgl_context_lost"}
    carried_out = [NOTE_CALL, ("emergency_reset_oc", (), lost)]

    def boom():
        raise RuntimeError("the engine is broken")

    with fake_gpu_page() as order:                                      # no game: as before
        assert post(c, route, lost)[0] == 200
    assert order == carried_out, order
    with fake_gpu_page() as order, swapped(AT, judging_driver_errors=lambda: True):
        status, answer = post(c, route, lost)
    assert (status, answer) == (200, {"ok": True, "skipped": "adaptive_session", "reason": "webgl_context_lost",
                                      "steps": []})
    assert order == [], "no note either: nothing was changed, so the session carries on"
    with fake_gpu_page() as order, swapped(AT, judging_driver_errors=lambda: True), \
            swapped(GO, is_tuning_active=lambda: True):                 # a test is running: its crash, its reset
        assert post(c, route, lost)[0] == 200
    assert order == carried_out, order
    with fake_gpu_page() as order, swapped(AT, judging_driver_errors=boom):         # in doubt, the reset
        assert post(c, route, lost)[0] == 200
    assert order == carried_out, order
    with fake_gpu_page() as order, swapped(AT, judging_driver_errors=lambda: True), swapped(GO, is_tuning_active=boom):
        assert post(c, route, lost)[0] == 200
    assert order == carried_out, order
    for body in ({"reason": "user_triggered"}, {}, {"reason": "temp_ceiling"}):     # only the page's own report
        with fake_gpu_page() as order, swapped(AT, judging_driver_errors=lambda: True):
            assert post(c, route, body)[0] == 200
        assert order == [NOTE_CALL, ("emergency_reset_oc", (), {"reason": body.get("reason", "user_triggered")})], order


def test_a_request_that_cannot_be_carried_out_leaves_no_note():
    """A note without a change would make a game's session leave the card
    alone for nothing, and say "you changed the overclock" when nobody did."""
    c = client()
    cases = [
        ("/api/gpu/oc/apply", b"[300, 1000]", 400),
        ("/api/gpu/oc/apply", b'{"core_offset_mhz": "fast"}', 500),
        ("/api/gpu/oc/apply", b'{"core_offset_mhz": 100, "power_pct": "max"}', 500),
        ("/api/gpu/oc/apply", b"not json", 500),
        ("/api/gpu/oc/profile", b'["core_offset_mhz"]', 400),
        ("/api/gpu/oc/profile", b"not json", 500)]
    # Up to 3.7.0 these were read as {}: 0 / 0 went to the card, or was saved
    # over the user's overclock.
    for empty in (b"null", b"[]", b"0", b"false", b'""'):
        cases += [("/api/gpu/oc/apply", empty, 400), ("/api/gpu/oc/profile", empty, 400)]
    for route, raw, status_wanted in cases:
        with fake_gpu_page() as order:
            status, answer = post(c, route, raw=raw)
        assert status == status_wanted and answer["ok"] is False, (route, raw, status, answer)
        if status == 400:
            assert answer == {"ok": False, "err": NOT_AN_OBJECT}, (route, raw, answer)
        assert order == [], (route, raw, order)


def test_the_routes_that_change_neither_the_clocks_nor_the_limit_leave_no_note():
    """The tests on the GPU page refuse by themselves while a game is looked
    after, and the apply-on-startup switch changes when the saved overclock is
    applied, not what it is: a note there would end a session for nothing."""
    c = client()
    quiet = (
        ("/api/gpu/oc/profile/reapply", {"enabled": True}, "set_profile_apply_on_startup"),
        ("/api/gpu/oc/probe/start", {}, "start_stability_probe"),
        ("/api/gpu/oc/probe/tick", {}, "run_probe_tick"),
        ("/api/gpu/oc/probe/end", {}, "end_stability_probe"),
        ("/api/gpu/oc/auto/start", {}, "auto_oc_start"),
        ("/api/gpu/oc/auto/next", {}, "auto_oc_next"),
        ("/api/gpu/oc/auto/cancel", {}, "auto_oc_cancel"),
        ("/api/gpu/oc/benchmark-tune/start", {}, "benchmark_oc_start"),
        ("/api/gpu/oc/benchmark-tune/next", {}, "benchmark_oc_next"),
        ("/api/gpu/oc/benchmark-tune/cancel", {}, "benchmark_oc_cancel"),
        ("/api/gpu/oc/benchmark/start", {}, "benchmark_start"),
        ("/api/gpu/oc/benchmark/record", {}, "benchmark_record_result"),
        ("/api/gpu/oc/benchmark/cancel", {}, "benchmark_cancel"),
        ("/api/gpu/oc/baseline", {}, "recalibrate_stock_baseline"),
    )
    for route, body, fn in quiet:
        with fake_gpu_page() as order:
            status, _answer = post(c, route, body)
        assert status == 200 and [name for name, _a, _k in order] == [fn], (route, status, order)
    crash_test = []
    with fake_gpu_page() as order, swapped(CR, handle_game_exit=lambda **k: crash_test.append(k) or {"crashed": False}):
        assert post(c, "/api/gpu/crash-recovery/test", {})[0] == 200
    assert order == [] and len(crash_test) == 1 and crash_test[0]["dry_run"] is True, crash_test


def test_a_note_that_fails_never_stands_in_the_way_of_the_users_change():
    c = client()
    for method, route, body, want in GPU_CHANGES:
        with fake_gpu_page(note_raises=RuntimeError("the engine is broken")) as order:
            status, answer = send(c, method, route, body)
        assert status == 200 and answer == {"ok": True, "by": want[0]}, (route, status, answer)
        assert order == [NOTE_CALL, want], (route, order)


def test_no_function_in_the_app_can_write_the_clocks_without_the_note():
    """Read from the source, so a route added later cannot forget it.  (A
    profile save writes no clock: it must NOT note, see above.)"""
    client()
    writers = {"apply_oc", "write_offsets", "reset_oc", "emergency_reset_oc", "delete_profile"}
    found = {}
    savers = {}
    for fn in ast.walk(ast.parse(source())):
        if not isinstance(fn, ast.FunctionDef):
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        if any(isinstance(n.func, ast.Attribute) and n.func.attr == "save_profile" for n in calls):
            savers[fn.name] = sum(1 for n in calls
                                  if isinstance(n.func, ast.Name) and n.func.id == "_note_manual_overclock_change")
        writes = [n for n in calls if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "gpu_overclock" and n.func.attr in writers]
        if not writes:
            continue
        notes = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_note_manual_overclock_change"]
        assert len(notes) == 1 and notes[0].lineno < min(w.lineno for w in writes), \
            f"{fn.name} writes without noting it first"
        between = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
                   and notes[0].lineno < n.lineno < min(w.lineno for w in writes)]
        assert between == [], f"{fn.name} can return between the note and the write"
        found[fn.name] = sorted(w.func.attr for w in writes)
    assert found == {"api_gpu_oc_apply": ["apply_oc"], "api_gpu_oc_reset": ["reset_oc"],
                     "api_gpu_oc_emergency_reset": ["emergency_reset_oc"],
                     "api_gpu_oc_profile_delete": ["delete_profile"]}, found
    assert savers == {"api_gpu_oc_profile_save": 0}, savers


def _watch_the_writers(seen):
    """Wrap the five writers so each records, as it is entered, whether the
    open session already carries the note.  Returns the swap to use in a with."""
    def watched(name):
        real = getattr(GO, name)

        def call(*a, **k):
            s = AT._sess
            seen.append((name, bool(s and s["manual_note"]), s["held"] if s else "no session"))
            return real(*a, **k)
        return call
    return swapped(GO, **{name: watched(name) for name in ("apply_oc", "reset_oc", "emergency_reset_oc",
                                                           "save_profile", "delete_profile")})


@engine_test
def test_during_a_game_the_session_hears_of_the_change_before_the_card_does(c):
    """The whole path, with the real engine: the route, the note, the write,
    then the watch thread's next looks.  Without the note coming first, a look
    in the middle of the write put the session's values back over the user's."""
    after_change = {"apply_oc": (300, 1000), "reset_oc": NONE, "emergency_reset_oc": NONE, "delete_profile": NONE}
    user_writes = {"apply_oc": [(300, 1000)], "reset_oc": [("reset",)], "emergency_reset_oc": [("reset",)],
                   "delete_profile": [("reset",)]}
    for method, route, body, want in GPU_CHANGES:
        fn = want[0]
        if fn == "emergency_reset_oc":
            body = {"reason": "user_triggered"}     # (the page's own report of a lost context has its test below)
        card = rig(held=(100, 500))
        assert start()["engaged"] is True and card.current == FULL
        poll()
        n = len(card.writes)
        seen, at_card = [], []
        card.on_write = lambda values: at_card.append(bool(AT._sess and AT._sess["manual_note"]))
        with _watch_the_writers(seen):
            status, answer = send(c, method, route, body)
        card.on_write = None
        assert status == 200 and answer["ok"] is True, (route, status, answer)
        assert seen[0] == (fn, True, None), (route, seen)
        assert all(at_card), f"{route}: the card was written before the session had the note"
        assert card.current == after_change[fn] and card.writes[n:] == user_writes[fn], (route, card.writes[n:])

        poll()
        poll()
        assert AT._sess["held"] == "manual", route
        assert card.writes[n:] == user_writes[fn], f"{route}: the session wrote over the user's change"
        assert state(c)["session"]["held"] == "manual"
        assert texts() == [P.HELD_MANUAL(NAME)], route
        close()
        assert card.current == after_change[fn] and card.writes[n:] == user_writes[fn], \
            f"{route}: nothing is put back after the user took over"
        assert session_file() is None and AT.session_active() is False


@engine_test
def test_a_driver_reset_the_page_reports_during_a_game_is_counted_not_taken_for_the_user(c):
    """With the real engine.  After any test in this run of the app the GPU
    page reported a driver reset during a game as a lost context: the
    overclock came off, the session read "you changed the overclock" and the
    error was never counted, so the game was never eased."""
    card = rig(held=(450, 2225))
    assert start()["engaged"] is True and card.current == FULL
    poll()
    n = len(card.writes)
    m.EVENTS.add("driver_error", clock.now - 1)                 # the driver resets; the page's canvas notices
    status, answer = post(c, "/api/gpu/oc/emergency-reset", {"reason": "webgl_context_lost"})
    assert status == 200 and answer["ok"] is True and answer["skipped"] == "adaptive_session", answer
    assert card.writes[n:] == [] and card.current == FULL and AT._sess["manual_note"] is False
    assert GO.external_write_seq() == AT._sess["seq"]
    for _ in range(8):
        poll()
    assert AT._sess["held"] is None and texts() == [P.W_SURVIVED(NAME)], "counted, as an error the game survived"
    assert len(game()["incidents"]) == 1 and card.writes[n:] == []

    # The user's own reset is carried out, and the session then leaves the card alone ...
    status, answer = post(c, "/api/gpu/oc/emergency-reset", {"reason": "user_triggered"})
    assert status == 200 and "skipped" not in answer and card.current == NONE
    poll()
    assert AT._sess["held"] == "manual"
    # ... and a session that is left alone judges nothing, so the page's report is carried out again.
    n = len(card.writes)
    status, answer = post(c, "/api/gpu/oc/emergency-reset", {"reason": "webgl_context_lost"})
    assert status == 200 and "skipped" not in answer and card.writes[n:] == [("reset",)]
    close()
    assert card.current == NONE and session_file() is None

    # No game looked after (Adaptive Tuning off): exactly as before.
    card = rig(held=(450, 2225), enabled=False)
    assert start() == {"engaged": False, "reason": "off"}
    status, answer = post(c, "/api/gpu/oc/emergency-reset", {"reason": "webgl_context_lost"})
    assert status == 200 and "skipped" not in answer and card.current == NONE and card.writes == [("reset",)]


def save(c, core, mem, power=100, apply_on_startup=True):
    status, answer = post(c, "/api/gpu/oc/profile", {"core_offset_mhz": core, "mem_offset_mhz": mem,
                                                     "power_pct": power, "apply_on_startup": apply_on_startup})
    assert status == 200 and answer["ok"] is True, (status, answer)
    assert saved_profile() == (core, mem, power, apply_on_startup)


@engine_test
def test_a_raised_profile_saved_during_a_game_is_not_followed(c):
    """Saving a profile writes no clock and is no manual change: the session
    carries on, lifts nothing mid-game, and hands the card back at the exit."""
    card = rig()
    put_game(level=1)
    assert start()["level"] == 1 and card.current == L75
    n = len(card.writes)
    save(c, 600, 3000)
    assert GO.external_write_seq() == AT._sess["seq"] and AT._sess["manual_note"] is False
    for _ in range(3):
        poll()
    assert AT._sess["held"] is None and card.current == L75 and card.writes[n:] == []
    assert game()["level"] == 1
    close()
    assert card.current == NONE, "handed back as the session found it"
    assert P.HELD_MANUAL(NAME) not in texts() and session_file() is None


@engine_test
def test_a_lowered_profile_saved_during_a_game_is_followed_and_the_card_handed_back(c):
    card = rig(held=(450, 2225))
    assert start()["engaged"] is True and card.current == FULL
    save(c, 300, 1500)
    poll()
    assert AT._sess["held"] is None and card.current == (300, 1500), "followed at once"
    poll()
    assert card.current == (300, 1500) and AT._sess["held"] is None
    close()
    assert card.current == FULL, "what was on the card before the game (a save applies nothing)"
    assert P.HELD_MANUAL(NAME) not in texts() and session_file() is None


@engine_test
def test_saving_the_same_profile_on_an_eased_game_still_hands_the_card_back(c):
    """It used to leave the eased clocks on the desktop after the game, with
    the drift watchdog holding them."""
    card = rig(held=(450, 2225))
    put_game(level=2)
    assert start()["level"] == 2 and card.current == L50
    n = len(card.writes)
    save(c, 450, 2225)
    for _ in range(3):
        poll()
    assert AT._sess["held"] is None and card.current == L50 and card.writes[n:] == []
    close()
    assert card.current == FULL and texts() == [] and session_file() is None
    wd = GO.get_oc_watchdog_status()
    assert not (wd["active"] and (wd["target_core_offset"], wd["target_mem_offset"]) == L50), \
        "the drift watchdog does not hold the eased clocks"


@engine_test
def test_saving_a_profile_mid_game_keeps_the_overclock_only_in_games(c):
    """Apply at start-up off: "It comes off when the game closes"."""
    card = rig(apply_on_startup=False)
    assert start()["engaged"] is True and card.current == FULL
    save(c, 450, 2225, apply_on_startup=False)
    for _ in range(2):
        poll()
    assert AT._sess["held"] is None
    close()
    assert card.current == NONE and session_file() is None


@engine_test
def test_a_save_then_an_apply_mid_game_leaves_the_users_value(c):
    card = rig(held=(450, 2225))
    assert start()["engaged"] is True and card.current == FULL
    save(c, 300, 1500)
    poll()
    assert post(c, "/api/gpu/oc/apply", {"core_offset_mhz": 300, "mem_offset_mhz": 1500, "power_pct": 100})[0] == 200
    poll()
    assert AT._sess["held"] == "manual", "the Apply is the user's change"
    close()
    assert card.current == (300, 1500) and texts() == [P.HELD_MANUAL(NAME)]


@engine_test
def test_the_crash_recovery_test_button_writes_no_clock_and_nothing_to_the_list(c):
    """With one axis of the saved overclock at 0 and a driver error in the
    last two minutes, the page's made-up +1 / +1 used to be "above the saved
    overclock": 0 / 0 went to the card, and a running game's session was held
    as changed by the user and left at 0 / 0 after the game."""
    card = rig(profile=(0, 1200, 100), held=(0, 1200))
    assert start()["engaged"] is True and card.current == (0, 1200)
    poll()
    n = len(card.writes)
    driver_error = {"ok": True, "tdr_count": 1, "crash_count": 0,
                    "events": [{"ts": clock.now - 30, "id": 153, "provider": "nvlddmkm"}]}
    with swapped(GO, check_driver_events=lambda seconds_back=300: driver_error):
        status, answer = post(c, "/api/gpu/crash-recovery/test",
                              {"exe": "__test__.exe", "display_name": "Vispora Test", "core": 1, "mem": 1})
    assert status == 200 and answer["crashed"] is True and answer["dry_run"] is True, answer
    assert answer["blacklisted"] is False and answer["step_down"] == {"core": 0, "mem": 0}, "what it would have done"
    assert card.writes[n:] == [] and card.current == (0, 1200), "no clock written"
    assert CR.get_blacklist() == {}, "nothing written to the per-game list"
    assert CR.get_history()[-1]["exe"] == "__test__.exe" and CR.get_history()[-1]["test"] is True
    assert toasts[-1] == ("Vispora Test closed after a graphics driver error", CR.TEST_TOAST_TEXT, True), toasts
    poll()
    assert AT._sess["held"] is None and AT._sess["manual_note"] is False
    close()
    assert card.current == (0, 1200) and texts() == []
    for text in (CR.TEST_TOAST_TEXT,):
        assert EM_DASH not in text and not BANNED.search(text), text


@engine_test
def test_the_crash_flag_a_test_set_is_not_taken_for_a_game_crash(c):
    """A failed tune step sets the in-app crash flag.  With the event log's
    two minutes also used for it, a game closed normally up to two minutes
    later was announced as "closed after a graphics driver error"."""
    rig()
    no_events = {"ok": True, "tdr_count": 0, "crash_count": 0, "events": []}
    windows = []                                        # what gpu_overclock has on record (grace included)
    with swapped(GO, check_driver_events=lambda seconds_back=300: no_events,
                 tuning_windows=lambda grace_s=None: list(windows)), swapped(CR, time=clock):
        try:
            windows.append((clock.now, clock.now + 5 + 120))    # a stability test ran ...
            clock.now += 5
            GO._crash_state.update(crashed_ts=clock.now, kind="tdr")      # ... its probe saw the driver fail
            clock.now += 15                             # the game is closed normally 15 s later
            r = CR.detect_crash(exit_ts=clock.now - 5)
            assert r["crashed"] is False and r["kind"] == "normal", ("a flag a test set", r)

            del windows[:]                              # with no test window on record ...
            r = CR.detect_crash(exit_ts=clock.now - 5)
            assert r["kind"] == "inapp_signal", ("... a fresh flag still counts", r)
            with swapped(GO, is_tuning_active=lambda: True):
                assert CR.detect_crash(exit_ts=clock.now - 5)["crashed"] is False, "not while a test runs"

            def unreadable(grace_s=None):
                raise OSError("the record could not be read")

            with swapped(GO, tuning_windows=unreadable):
                assert CR.detect_crash(exit_ts=clock.now - 5)["crashed"] is False, "in doubt, not a crash"
            clock.now += 85                             # 100 s after the flag: its own short window is over
            r = CR.detect_crash(exit_ts=clock.now - 5)
            assert r["crashed"] is False, ("the event log's two minutes are not the flag's", r)
        finally:
            GO.clear_crash_state()


@engine_test
def test_the_driver_errors_of_a_test_are_not_taken_for_a_game_crash(c):
    """Crash recovery reads the real event log now (it never could on a real
    PC), so it also sees the errors a test on the GPU page causes on purpose.
    A game Adaptive Tuning does not look after, closed in the two minutes
    after a failed tune step, was announced as "closed after a graphics driver
    error", and with more on the card than is saved the clocks were lowered."""
    card = rig(enabled=False, held=(450, 2225))
    windows = []                                        # what gpu_overclock has on record (grace included)

    def exit_now():
        n = len(card.writes)
        r = CR.handle_game_exit("cs2.exe", "Counter-Strike 2", 500, 2400, exit_ts=clock.now - 5)
        return r, card.writes[n:]

    with swapped(GO, tuning_windows=lambda grace_s=None: list(windows)), swapped(CR, time=clock, _append_history=(
            lambda entry: None)):
        clock.now += 600
        windows.append((clock.now - 90, clock.now - 30 + 120))      # a tune ran until 30 s ago ...
        m.EVENTS.add("driver_error", clock.now - 40)                # ... and crashed the driver on its last step
        assert GO.check_driver_events(seconds_back=120)["tdr_count"] == 1, "the test's own probe still sees it"
        r, wrote = exit_now()
        assert r["crashed"] is False and r["kind"] == "normal" and wrote == [], (r, wrote)
        assert CR.get_blacklist() == {} and toasts == [], "nothing listed, nothing said"
        assert CR.detect_crash(exit_ts=clock.now - 5)["events"] == []

        def unreadable(grace_s=None):
            raise OSError("the record could not be read")

        with swapped(GO, tuning_windows=unreadable):
            assert CR.detect_crash(exit_ts=clock.now - 5)["crashed"] is False, "in doubt, not a crash"
        with swapped(GE, query=lambda *a, **k: {"ok": False, "events": [], "err": "busy"}):
            # (check_driver_events is asked first and cannot read the log either)
            assert CR.detect_crash(exit_ts=clock.now - 5)["crashed"] is False

        m.EVENTS.add("driver_error", clock.now - 200)               # before the test began: not the test's
        r = CR.detect_crash(exit_ts=clock.now - 100, lookback_s=120)
        assert r["crashed"] is True and r["kind"] == "tdr" and r["tdr_count"] == 1, r

        del windows[:]                                              # no test near: as before, and one read only
        asked = []
        real = GE.query
        with swapped(GE, query=lambda *a, **k: asked.append(a) or real(*a, **k)):
            r, wrote = exit_now()
        assert len(asked) == 1, "with no test on record the event log is read once, by check_driver_events"
        assert r["crashed"] is True and r["blacklisted"] is True and wrote == [(475, 2300)], (r, wrote)
        assert toasts and toasts[-1][0] == "Counter-Strike 2 closed after a graphics driver error"
    CR.clear_blacklist()


@engine_test
def test_a_deleted_profile_is_never_written_back_through_the_routes(c):
    card = rig(held=(450, 2225))
    assert start()["engaged"] is True
    n = len(card.writes)
    assert delete(c, "/api/gpu/oc/profile") == (200, {"ok": True})
    assert saved_profile() is None and card.current == NONE
    for _ in range(4):
        poll()
    assert card.current == NONE and card.writes[n:] == [("reset",)]
    now = state(c)
    assert now["limit"]["has_profile"] is False and now["session"]["held"] == "manual"
    close()
    assert card.current == NONE and card.writes[n:] == [("reset",)] and saved_profile() is None


@engine_test
def test_a_change_with_no_game_open_never_reaches_the_next_session(c):
    """Benchmark Tune applies every step through the same route, with no game
    running; the note must not make the next game start as taken over."""
    card = rig()
    for _ in range(3):
        assert post(c, "/api/gpu/oc/apply", {"core_offset_mhz": 200, "mem_offset_mhz": 900, "power_pct": None})[0] == 200
    assert post(c, "/api/gpu/oc/reset", {})[0] == 200
    assert AT.session_active() is False and texts() == []
    assert start()["engaged"] is True and card.current == FULL
    for _ in range(3):
        poll()
    assert AT._sess["held"] is None and AT._sess["manual_note"] is False and card.current == FULL
    close()
    assert card.current == NONE and texts() == []


@engine_test
def test_the_apply_on_startup_switch_does_not_end_a_games_session(c):
    card = rig(held=(450, 2225))
    put_game(level=2)
    assert start()["level"] == 2 and card.current == L50
    n = len(card.writes)
    status, answer = post(c, "/api/gpu/oc/profile/reapply", {"enabled": False})
    assert status == 200 and answer["ok"] is True and saved_profile() == (450, 2225, 100, False)
    for _ in range(3):
        poll()
    assert AT._sess["held"] is None and card.current == L50 and card.writes[n:] == [] and texts() == []
    close()
    assert card.current == FULL, "the card is handed back as the session found it"


@engine_test
def test_a_test_on_the_gpu_page_is_refused_through_its_route_while_a_game_is_looked_after(c):
    """The page reads "refused" and then sends nothing more; the cancels it
    used to send must not take the game's overclock away either."""
    card = rig()
    assert start()["engaged"] is True and card.current == FULL
    n = len(card.writes)
    refusal = {"ok": False, "err": GO.SESSION_REFUSAL, "refused": "adaptive_session"}
    for route in ("/api/gpu/oc/auto/start", "/api/gpu/oc/benchmark-tune/start", "/api/gpu/oc/probe/start",
                  "/api/gpu/oc/benchmark/start"):
        assert post(c, route, {}) == (200, refusal), route
    for route in ("/api/gpu/oc/auto/cancel", "/api/gpu/oc/benchmark-tune/cancel", "/api/gpu/oc/benchmark/cancel"):
        status, answer = post(c, route, {})
        assert status == 200 and answer.get("skipped") == "adaptive_session", (route, answer)
    poll()
    assert card.current == FULL and card.writes[n:] == [] and AT._sess["held"] is None


# ═════════════════════════════════════════════════════════════════════
# The start: recover first; then the saved overclock, the flag, the monitor
# ═════════════════════════════════════════════════════════════════════
def test_main_settles_an_interrupted_session_before_anything_else_can_touch_the_card():
    client()
    src = inspect.getsource(A.main)
    assert src.count("adaptive_tuning.recover_interrupted_session()") == 1
    at = src.index("adaptive_tuning.recover_interrupted_session()")
    before = src[:at]
    for word in ("game_profiles", "gpu_overclock", "idle_mode", "temp_ceiling", "crash_recovery", "tournament_mode",
                 "boot_prep", "_delayed(", "start_flask", "_boot_saved_overclock_then_game_monitor",
                 "webview.", "threading.Thread("):
        assert word not in before, f"{word} comes before the recovery"
    for later in ("game_profiles.check_and_restore_on_boot()", "adaptive_tuning.clear_runtime_if_orphaned()",
                  "_delayed(0, lambda: _boot_saved_overclock_then_game_monitor(", "idle_mode.run_on_boot()",
                  "temp_ceiling.start_watchdog()", "threading.Thread(target=start_flask"):
        assert src.index(later) > at, later
    # Not in a thread and not deferred: main() does not go on until it is done.
    tree = ast.parse("def main():\n" + src.split("\n", 1)[1])
    main = tree.body[0]
    top = [s for s in main.body if any(isinstance(n, ast.Attribute) and n.attr == "recover_interrupted_session"
                                       for n in ast.walk(s))]
    assert len(top) == 1 and isinstance(top[0], ast.Try), "a statement of main() itself, guarded"
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in top[0].handlers)


def test_main_starts_the_saved_overclock_and_the_game_monitor_as_one_task_with_the_old_delays():
    client()
    src = inspect.getsource(A.main)
    assert re.search(r"saved_oc_delay\s*=\s*15 if from_autostart else 0\b", src)
    assert re.search(r"boot_prep_delay\s*=\s*60 if from_autostart else 0\b", src)
    one = re.findall(r"_delayed\(0, lambda: _boot_saved_overclock_then_game_monitor\(\s*"
                     r"saved_oc_delay, boot_prep_delay, session_kept=adaptive_session_kept\)\)", src)
    assert len(one) == 1 and src.count("_boot_saved_overclock_then_game_monitor(") == 1
    for name in ("apply_saved_profile_on_startup", "mark_startup_apply_done", "auto_enable_on_boot_if_set",
                 "start_monitoring", "gpu_overclock."):
        assert name not in src, f"main() itself must not call {name}"
    assert "adaptive_session_kept = bool(isinstance(recovered, dict) and recovered.get(\"kept\"))" in src

    # In the whole app, each of the three steps is called in exactly one place.
    where = {}
    for fn in ast.parse(source()).body:
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr in ("apply_saved_profile_on_startup",
                                                               "mark_startup_apply_done",
                                                               "auto_enable_on_boot_if_set"):
                    where.setdefault(n.attr, []).append(fn.name)
    assert where == {name: ["_boot_saved_overclock_then_game_monitor"]
                     for name in ("apply_saved_profile_on_startup", "mark_startup_apply_done",
                                  "auto_enable_on_boot_if_set")}, where


@contextlib.contextmanager
def fake_boot(apply=None, monitor=None, mark=None):
    """Stand-ins for the three steps of the start-up task.  Yields the order
    they ran in; each entry is (step, the clock, was the flag already set)."""
    order = []
    real_mark = GO.mark_startup_apply_done

    def stamp(step):
        order.append((step, clock.now, GO.wait_startup_apply(0)))

    def do_apply():
        stamp("apply")
        return apply() if apply else {"applied": True}

    def do_mark():
        stamp("mark")
        if mark:
            mark()
        real_mark()

    def do_monitor():
        stamp("monitor")
        return monitor() if monitor else {"ok": True, "started": True}

    GO._startup_apply_done.clear()
    clock.now = T0
    with swapped(GO, apply_saved_profile_on_startup=do_apply, mark_startup_apply_done=do_mark), \
            swapped(GP, auto_enable_on_boot_if_set=do_monitor), swapped(A, time=clock):
        try:
            yield order
        finally:
            GO._startup_apply_done.clear()


def test_the_start_up_task_applies_marks_and_only_then_starts_the_monitor():
    client()
    with fake_boot() as order:
        A._boot_saved_overclock_then_game_monitor()
    assert order == [("apply", T0, False), ("mark", T0, False), ("monitor", T0, True)], order


def test_the_start_up_task_keeps_the_delays_of_an_autostart():
    """15 s for the overclock and 60 s for the monitor, both counted from the
    start, so a slow apply does not push the monitor back."""
    client()

    def slow_apply():
        clock.now += 5.0                            # the apply itself takes seconds
        return {"applied": True}

    with fake_boot(apply=slow_apply) as order:
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert order == [("apply", T0 + 15, False), ("mark", T0 + 20, False), ("monitor", T0 + 60, True)], order

    def very_slow_apply():
        clock.now += 90.0
        return {"applied": False}

    with fake_boot(apply=very_slow_apply) as order:
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert order == [("apply", T0 + 15, False), ("mark", T0 + 105, False), ("monitor", T0 + 105, True)], \
        "late, but still after the apply and the flag"


def test_a_step_of_the_start_up_task_that_fails_does_not_stop_the_next():
    client()

    def boom():
        raise OSError("nvidia-smi went away")

    with fake_boot(apply=boom) as order:
        A._boot_saved_overclock_then_game_monitor()
        assert GO.wait_startup_apply(0) is True, "a game start must not wait 15 s for an apply that failed"
    assert [step for step, _t, _f in order] == ["apply", "mark", "monitor"]

    with fake_boot(mark=boom) as order:
        A._boot_saved_overclock_then_game_monitor()
    assert [step for step, _t, _f in order] == ["apply", "mark", "monitor"]

    with fake_boot(monitor=boom) as order:
        A._boot_saved_overclock_then_game_monitor()             # must not raise
        assert GO.wait_startup_apply(0) is True
    assert [step for step, _t, _f in order] == ["apply", "mark", "monitor"]

    with fake_boot(apply=lambda: None) as order:                # an answer that is not a dict
        A._boot_saved_overclock_then_game_monitor()
    assert [step for step, _t, _f in order] == ["apply", "mark", "monitor"]


def test_with_a_session_kept_for_a_running_game_the_saved_overclock_is_not_put_over_it():
    client()
    with fake_boot() as order:
        A._boot_saved_overclock_then_game_monitor(15, 60, session_kept=True)
    assert order == [("mark", T0 + 15, False), ("monitor", T0 + 60, True)], order


def test_the_start_up_task_says_the_monitor_is_on_its_way_until_it_has_started():
    """For the first minute after an autostart the card said "On, but Vispora
    isn't watching for games. Press Start Monitoring above.", although the
    monitor was about to start by itself."""
    client()
    seen = []

    def apply():
        seen.append(("apply", GP.monitor_starting()))
        return {"applied": True}

    def monitor():
        seen.append(("monitor", GP.monitor_starting()))
        return {"ok": True, "started": True}

    assert GP.monitor_starting() is False
    with fake_boot(apply=apply, monitor=monitor):
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert seen == [("apply", True), ("monitor", True)], seen
    assert GP.monitor_starting() is False, "cleared once the monitor is started"

    def boom():
        raise OSError("the monitor did not start")

    del seen[:]
    with fake_boot(apply=apply, monitor=boom):
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert seen == [("apply", True)] and GP.monitor_starting() is False, "cleared when the task gives up too"

    assert GP.set_settings(auto_enable_on_boot=False)["ok"]
    try:
        del seen[:]
        with fake_boot(apply=apply, monitor=monitor) as order:
            A._boot_saved_overclock_then_game_monitor(15, 60)
        assert seen == [("apply", False), ("monitor", False)], "the monitor is off for the start: not on its way"
        assert order[-1] == ("monitor", T0 + 60, True), "and nothing is looked for"
    finally:
        GP.set_settings(auto_enable_on_boot=True)


def test_the_monitor_starts_right_after_the_apply_when_a_game_is_already_running():
    """After an autostart the saved overclock went on the card at 15 s and the
    monitor only came at 60 s: a game Adaptive Tuning had eased ran on the
    full overclock for 45 seconds."""
    client()
    asked = []
    game = [{"exe": EXE, "pid": PID, "name": NAME}]
    with fake_boot() as order, swapped(GP, running_game_candidates=lambda: asked.append(clock.now) or game):
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert order == [("apply", T0 + 15, False), ("mark", T0 + 15, False), ("monitor", T0 + 15, True)], order
    assert asked == [T0 + 15], "looked for right after the apply"

    del asked[:]                                   # a game started during the wait: found at the next look
    with fake_boot() as order, swapped(GP, running_game_candidates=lambda: asked.append(clock.now)
                                       or (game if clock.now >= T0 + 30 else [])):
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert asked == [T0 + 15, T0 + 30] and order[-1] == ("monitor", T0 + 30, True), (asked, order)

    del asked[:]                                   # no game, or a scan that fails: the old delay
    for answer in ([], None):
        with fake_boot() as order, swapped(GP, running_game_candidates=lambda: asked.append(clock.now) or answer):
            A._boot_saved_overclock_then_game_monitor(15, 60)
        assert order[-1] == ("monitor", T0 + 60, True), (answer, order)
    assert asked == [T0 + 15, T0 + 30, T0 + 45] * 2, asked

    del asked[:]                                   # a manual start waits for nothing and looks for nothing
    with fake_boot() as order, swapped(GP, running_game_candidates=lambda: asked.append(clock.now) or game):
        A._boot_saved_overclock_then_game_monitor()
    assert asked == [] and order == [("apply", T0, False), ("mark", T0, False), ("monitor", T0, True)]

    def broken():
        raise RuntimeError("PowerShell went away")

    with fake_boot() as order, swapped(GP, running_game_candidates=broken):
        A._boot_saved_overclock_then_game_monitor(15, 60)
    assert order[-1] == ("monitor", T0 + 60, True), "a scan that raises waits the delay out"


@engine_test
def test_the_start_up_task_with_the_real_modules(c):
    """The saved overclock is on the card, and gpu_overclock says so, before
    the monitor can find a game."""
    card = rig(startup_done=False)
    at_monitor = []

    def monitor():
        at_monitor.append((card.current, GO.wait_startup_apply(0), GO.get_applied_oc()["active"]))
        return {"ok": True}

    with swapped(GP, start_monitoring=monitor):
        assert GO.wait_startup_apply(0) is False
        A._boot_saved_overclock_then_game_monitor()
    assert at_monitor == [(FULL, True, True)] and card.writes == [FULL]

    card = rig(apply_on_startup=False, startup_done=False)      # not applied at start-up: skipped, and still marked
    del at_monitor[:]
    with swapped(GP, start_monitoring=monitor):
        A._boot_saved_overclock_then_game_monitor()
    assert at_monitor == [(NONE, True, False)] and card.writes == []

    card = rig(profile=None, startup_done=False)                # no saved overclock at all
    del at_monitor[:]
    with swapped(GP, start_monitoring=monitor):
        A._boot_saved_overclock_then_game_monitor()
    assert at_monitor == [(NONE, True, False)] and card.writes == []

    card = rig(startup_done=False)                              # the monitor is switched off for the start
    assert GP.set_settings(auto_enable_on_boot=False)["ok"]
    del at_monitor[:]
    with swapped(GP, start_monitoring=monitor):
        A._boot_saved_overclock_then_game_monitor()
    assert at_monitor == [] and card.current == FULL and GO.wait_startup_apply(0) is True


@engine_test
def test_a_game_found_right_after_the_start_hands_back_the_saved_overclock_not_nothing(c):
    """What the order is for.  With the two steps on two threads, a game that
    was already running could be taken on before the saved overclock had
    reached the card: "no overclock" was then what the session handed back."""
    card = rig(startup_done=False)
    put_game(level=1)
    found = []

    def monitor():                                              # the monitor's first look finds the game
        found.append(start())
        return {"ok": True}

    with swapped(GP, start_monitoring=monitor):
        A._boot_saved_overclock_then_game_monitor()
    assert found[0]["engaged"] is True and found[0]["level"] == 1
    assert AT._sess["pre"] == [450, 2225, 100] and card.current == L75
    assert card.writes == [FULL, L75], "the saved overclock first, the game's own level last"
    poll()
    assert AT._sess["held"] is None, "the app's own start is not taken for the user changing the overclock"
    close()
    assert card.current == FULL


@engine_test
def test_vispora_restarted_mid_game_carries_the_session_on_without_raising_the_clocks(c):
    """The sequence of main(): recover, then the start-up task with what the
    recovery answered.  The game had been eased; the saved overclock must not
    land on it on the way."""
    card = rig(held=(450, 2225))
    put_game(level=2)
    assert start()["level"] == 2 and card.current == L50
    poll()
    die()                                                       # the process is gone; the game is not
    clock.now += 30
    n = len(card.writes)

    recovered = AT.recover_interrupted_session()
    kept = bool(isinstance(recovered, dict) and recovered.get("kept"))      # as main() reads it
    assert kept is True and card.writes[n:] == []
    found = []
    with swapped(GP, start_monitoring=lambda: found.append(AT.on_game_start(EXE, NAME, PID)) or {"ok": True}):
        A._boot_saved_overclock_then_game_monitor(session_kept=kept)
    assert found[0]["engaged"] is True and found[0]["adopted"] is True and found[0]["level"] == 2
    assert FULL not in card.writes[n:] and all(w == L50 for w in card.writes[n:]), card.writes[n:]
    assert card.current == L50 and AT._sess["pre"] == [450, 2225, 100] and GO.wait_startup_apply(0) is True
    close()
    assert card.current == FULL, "the saved overclock comes back when the game closes"


# ═════════════════════════════════════════════════════════════════════
# The quit
# ═════════════════════════════════════════════════════════════════════
def test_quitting_ends_the_session_even_when_the_step_before_it_fails():
    client()
    m.fresh()
    ended = []

    def tuning_check():
        raise OSError("the tuning state could not be read")

    with swapped(GO, is_tuning_active=tuning_check), \
            swapped(AT, end_active_session=lambda reason="": ended.append(reason) or {"ended": False}):
        A._restore_system_before_exit("user quit")
    assert ended == ["user quit"], ended


def test_quit_and_the_updater_go_through_the_same_restore():
    client()
    src = source()
    assert "updater.set_pre_exit_hook(_restore_system_before_exit)" in src
    quit_path = inspect.getsource(A._restore_system_before_exit)
    assert quit_path.count("adaptive_tuning.end_active_session(reason)") == 1
    assert quit_path.index("game_profiles.stop_monitoring()") < quit_path.index("adaptive_tuning.end_active_session(")
    assert quit_path.index("adaptive_tuning.end_active_session(") < quit_path.index("idle_mode.manual_release()")


@engine_test
def test_quitting_mid_game_hands_the_card_back(c):
    card = rig(held=(100, 500))
    put_game(level=1)
    assert start()["level"] == 1 and card.current == L75
    A._restore_system_before_exit("user quit")
    assert card.current == (100, 500) and session_file() is None and AT.session_active() is False
    assert game()["level"] == 1 and texts() == [], "a quit judges nothing"
    assert MINE not in GO._oc_watchdog_pause_sources, "the drift watchdog runs again"


@engine_test
def test_quitting_mid_game_on_an_eased_game_leaves_its_clocks_until_the_game_has_closed(c):
    """Quitting handed the card back at once: the full saved overclock went
    under a game that had been eased because it fails on it.  Now the eased
    clocks stay, and the next start puts the card back once the game is gone."""
    card = rig(held=(450, 2225))
    put_game(level=1, limit_at_level=[450, 2225])
    assert start()["level"] == 1 and card.current == L75
    poll()
    n = len(card.writes)
    A._restore_system_before_exit("user quit")
    assert card.current == L75 and card.writes[n:] == [], "nothing is written under the running game"
    assert AT._sess is not None and AT._sess["held"] == "released" and session_file()["held"] == "released"
    assert MINE in GO._oc_watchdog_pause_sources, "the drift watchdog must not put the full overclock back either"
    assert texts() == [P.HELD_RELEASED(NAME)] and game()["level"] == 1
    die()                                                       # the process is gone ...
    m.exit_proc(PID)                                            # ... the game closes later ...
    clock.now += 3600
    recovered = AT.recover_interrupted_session()                # ... and Vispora is started again
    assert recovered.get("recovered") is True and not recovered.get("kept"), recovered
    assert card.current == FULL and card.writes[n:] == [FULL] and session_file() is None
    assert texts() == [P.HELD_RELEASED(NAME), P.HANDED_BACK(NAME)] and game()["level"] == 1, "nothing is judged"
    with swapped(GP, start_monitoring=lambda: {"ok": True}):
        A._boot_saved_overclock_then_game_monitor(session_kept=False)
    assert card.current == FULL and MINE not in GO._oc_watchdog_pause_sources

    # Started again while the game still runs: the session is carried on and looked after again.
    card = rig(held=(450, 2225))
    put_game(level=1, limit_at_level=[450, 2225])
    assert start()["level"] == 1
    poll()
    A._restore_system_before_exit("user quit")
    die()
    clock.now += 30
    n = len(card.writes)
    recovered = AT.recover_interrupted_session()
    kept = bool(isinstance(recovered, dict) and recovered.get("kept"))      # as main() reads it
    assert kept is True and card.writes[n:] == [] and card.current == L75
    found = []
    with swapped(GP, start_monitoring=lambda: found.append(AT.on_game_start(EXE, NAME, PID)) or {"ok": True}):
        A._boot_saved_overclock_then_game_monitor(session_kept=kept)
    assert found[0]["engaged"] is True and found[0]["adopted"] is True and found[0]["level"] == 1, found
    assert found[0]["held"] is None, "found again: looked after again"
    assert all(w == L75 for w in card.writes[n:]) and card.current == L75, card.writes[n:]
    poll()
    assert AT._sess["held"] is None and AT._sess["pre"] == [450, 2225, 100]
    close()
    assert card.current == FULL and session_file() is None, "the saved overclock comes back when the game closes"


def test_zz_nothing_is_left_behind():
    """The last test: no session, no watch thread, the event log's handles closed."""
    restore()
    m.fresh()
    assert AT._sess is None and AT.session_active() is False
    assert not [t for t in threading.enumerate() if t.name.startswith("adaptive-tuning-") and t.is_alive()]
    assert m.EVENTS.open_handles == 0 and m.EVENTS.bad == []
