"""The tray icon behind the shell (shell_rs/), held against today's.

Behind the shell the tray icon is the shell's.  Today's tray (app.py,
_create_tray_icon) is the measure: the same three pictures, the same menu
in the same order with the same words, the same ticks, and each entry doing
what it does today, with the same message afterwards.  No release starts
the app behind the shell yet.

What is checked here:

  * started as it always was, the app has none of the tray's calls;
  * behind the shell they need the run's secret, like every other call;
  * what the app says the tray shows is what today's tray looks at, and the
    shell makes of it the menu, the picture and the tooltip today's tray
    shows (today's tray is built here for real, with a stand-in for the
    library that puts it on the screen; the shell's side is asked of the
    shell's trial tool where that has been built);
  * each call does what the entry of the same name in today's menu does;
  * the pictures the shell carries are the ones the app draws;
  * today's tray is as it was;
  * behind the shell the entry that starts the app at sign-in names the
    shell, and started as it always was it names what it named.  No task is
    made and nothing is written to the registry: the app's door to the
    outside is watched, and nothing goes through it.

The shell's own side is in shell_rs/core (cargo test); the real tray icon,
with its real menu, is tried by shell_rs/app/try_it.py.
"""
import sandbox as m

import ast
import contextlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import types
import unittest

import config
import test_shell_rs as S                           # (its helpers: where the trial tool is, the files of the two crates)

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
    from core import autostart, cleaner, crosshair, game_profiles, notifier, power
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

CALLS = {"game-mode": "POST", "force-normal": "POST", "crosshair": "POST", "refresh-pc": "POST", "clean-ram": "POST",
         "state": "GET"}
LOOKS = ("idle", "armed", "playing")
_MISSING = object()


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def host():
    return {"Host": f"127.0.0.1:{config.APP_PORT}"}


def auth():
    return {**host(), A._API_TOKEN_HEADER: A._API_TOKEN}


@contextlib.contextmanager
def behind_the_shell():
    """As _serve_for_the_shell leaves the app, as far as the tray's calls look."""
    saved = A._behind_shell
    A._behind_shell = {"port": config.APP_PORT, "shell_pid": 1, "shell_started": 1, "shell_exe": None}
    try:
        yield A.app.test_client()
    finally:
        A._behind_shell = saved


@contextlib.contextmanager
def changed(target, **values):
    """Give `target` (a module or a dict) other values for a while."""
    is_dict = isinstance(target, dict)
    saved = {name: (target.get(name, _MISSING) if is_dict else getattr(target, name, _MISSING)) for name in values}
    for name, value in values.items():
        if is_dict:
            target[name] = value
        else:
            setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if is_dict:
                if value is _MISSING:
                    target.pop(name, None)
                else:
                    target[name] = value
            elif value is _MISSING:
                delattr(target, name)
            else:
                setattr(target, name, value)


@contextlib.contextmanager
def in_state(monitoring=False, game=None, pid=None, crosshair_on=False):
    with changed(game_profiles._engine, monitoring=monitoring, active_game=game, active_pid=pid), \
            changed(crosshair, is_enabled=lambda: crosshair_on):
        yield


# ── today's tray, built for real ──────────────────────────────────────
class _Item:
    def __init__(self, text, action, checked=None, visible=True, enabled=True, default=False):
        self.text, self.action, self.checked = text, action, checked
        self.visible, self.enabled, self.default = visible, enabled, default

    @staticmethod
    def _now(value):
        return bool(value(None)) if callable(value) else bool(value)

    def shown(self):
        return self._now(self.visible)

    def ticked(self):
        return None if self.checked is None else self._now(self.checked)


class _Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class _Icon:
    made = []

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name, self.icon, self.title, self.menu, self.updated_from = name, icon, title, menu, []
        _Icon.made.append(self)

    def run(self):                                  # the real one runs the tray's message loop
        return None

    def update_menu(self):
        self.updated_from.append(threading.current_thread().name)

    def stop(self):
        pass


def todays_tray():
    """Build today's tray, with a stand-in for the library that puts it on
    the screen, and wait until its watcher has looked at the machine once."""
    stand_in = types.ModuleType("pystray")
    stand_in.Icon, stand_in.Menu, stand_in.MenuItem = _Icon, _Menu, _Item
    saved = sys.modules.get("pystray", _MISSING)
    sys.modules["pystray"] = stand_in
    try:
        A._create_tray_icon(lambda: None, lambda: None)
    finally:
        if saved is _MISSING:
            sys.modules.pop("pystray", None)
        else:
            sys.modules["pystray"] = saved
    icon = _Icon.made[-1]
    assert m.wait(lambda: "tray-watcher" in icon.updated_from), "today's tray never looked at the machine"
    return icon


def as_today(icon):
    """What today's tray shows, as plain values."""
    pictures = {A._render_tray_image(look).tobytes(): look for look in LOOKS}
    menu = []
    for item in icon.menu.items:
        if item is _Menu.SEPARATOR:
            menu.append(None)
        elif item.shown():
            menu.append((item.text, item.ticked(), _Item._now(item.enabled)))
    return {"picture": pictures[icon.icon.tobytes()], "tooltip": icon.title, "menu": menu}


def the_shells(state):
    """What the shell makes of what the app says: asked of the shell's trial
    tool.  None on a PC where that has not been built."""
    if not S.TRY:
        return None
    out = m.REAL_POPEN([S.TRY, "--tray-look", json.dumps(state) if state is not None else "none"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    said, complaint = out.communicate(timeout=30)
    assert out.returncode == 0, complaint
    look = json.loads(said.decode("utf-8"))
    menu = [None if e["kind"] == "line" else (e["text"], e["ticked"] if e["kind"] == "tick" else None, e["enabled"])
            for e in look["menu"]]
    return {"picture": look["icon"], "tooltip": look["tooltip"], "menu": menu, "ids": [e["id"] for e in look["menu"]]}


# ═════════════════════════════════════════════════════════════════════
# Who may call
# ═════════════════════════════════════════════════════════════════════
def test_started_as_it_always_was_the_app_has_none_of_the_trays_calls():
    a = app()
    assert a._behind_shell is None
    c = a.app.test_client()
    nowhere = c.get("/api/no-such-call-at-all", headers=auth())
    assert nowhere.status_code == 404
    for call, method in CALLS.items():
        answer = c.open(f"/api/tray/{call}", method=method, headers=auth(), json={})
        assert answer.status_code == 404, (call, answer.status_code)
        assert answer.get_data() == nowhere.get_data().replace(b"/api/no-such-call-at-all", f"/api/tray/{call}".encode()), \
            f"{call}: it answers as a path that does not exist"
    # (the app as it was: nothing changed on the fake machine, nothing started)
    assert m.CMDS == [] or all("tray" not in str(cmd).lower() for cmd in m.CMDS)
    src = inspect.getsource(a._for_the_shells_tray)
    assert "if _behind_shell is None:\n            abort(404)" in src
    for call in CALLS:
        rule = next(r for r in a.app.url_map.iter_rules() if r.rule == f"/api/tray/{call}")
        handler = a.app.view_functions[rule.endpoint]
        assert handler.__code__.co_name == "only_behind_the_shell" and handler.__wrapped__, \
            f"{call} is answered for an ordinary start"


def test_behind_the_shell_they_need_the_secret_like_every_other_call():
    app()
    with behind_the_shell() as c, in_state():
        for call, method in CALLS.items():
            path = f"/api/tray/{call}"
            assert c.open(path, method=method, headers=host(), json={}).status_code == 403, call
            wrong = {**host(), A._API_TOKEN_HEADER: "x" * 43}
            assert c.open(path, method=method, headers=wrong, json={}).status_code == 403, call
            cross = {**auth(), "Origin": "http://evil.example"}
            assert c.open(path, method=method, headers=cross, json={}).status_code == 403, call
        # What changes something is not done for a GET.
        for call, method in CALLS.items():
            if method == "POST":
                assert c.get(f"/api/tray/{call}", headers=auth()).status_code == 405, call
        said = c.get("/api/tray/state", headers=auth())
        assert said.status_code == 200 and said.get_json() == {
            "ok": True, "monitoring": False, "playing": False, "active_game": None, "crosshair": False}


def test_the_shell_and_the_app_agree_on_the_trays_calls():
    """Every call the shell's tray makes is one the app has, with that
    method; and the app has no call for the tray that the shell does not make."""
    a = app()
    core, tray = S.rust("src", "tray.rs"), S.app_crate("src", "tray.rs")
    asked = dict(re.findall(r'^        (\w+) => Some\("(/api/tray/[\w-]+)"\),$', core, re.M))
    names = dict(re.findall(r'^pub const (\w+): &str = "([\w-]+)";$', core, re.M))
    state = re.search(r'^pub const STATE_CALL: &str = "(/api/tray/[\w-]+)";$', core, re.M).group(1)
    made = {path: "POST" for path in asked.values()} | {state: "GET"}
    there = {r.rule: sorted(r.methods - {"HEAD", "OPTIONS"}) for r in a.app.url_map.iter_rules() if r.rule.startswith("/api/tray/")}
    assert {path: [method] for path, method in made.items()} == there
    assert there == {f"/api/tray/{call}": [method] for call, method in CALLS.items()}
    # An entry's call is named after the entry.
    assert {names[const]: path for const, path in asked.items()} == {call: f"/api/tray/{call}" for call in CALLS if call != "state"}
    # The shell makes them itself, with the secret, and only of a backend that is up.
    assert 'hands.shell.ask_backend("POST", call)' in tray and 'shell.ask_backend("GET", tray::STATE_CALL)' in tray
    # The two entries that are the shell's own: the window, and the quit every quit is.
    assert 'tray::OPEN => self.open("the tray\'s menu"),' in tray
    assert 'tray::QUIT => self.shell.quit("Quit in the tray\'s menu"),' in tray
    # The left button brings the window back and does not open the menu.
    assert ".show_menu_on_left_click(false)" in tray
    assert "TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. }" in tray
    assert "TrayIconEvent::DoubleClick { button: MouseButton::Left, .. }" in tray
    # After an entry was chosen the menu is made again whether or not
    # anything changed: Windows ticks an entry by itself when it is clicked,
    # and where the backend did not do as asked the tick would be wrong.
    assert "if asked_for || look != shown {" in tray and "hands.again();" in tray
    # Made once the window is there; without one the app still runs.
    main = S.app_crate("src", "main.rs")
    run = main[main.index("fn run("):]
    made_at = run.index("if let Err(why) = tray::make(&app, shell.clone(), pane.clone(), trial.is_some()) {")
    assert run.index(".map_err(no_window)?;", run.index("WebviewWindowBuilder::new(")) < made_at < run.index("shell.start();")
    assert 'shell.say(&format!("no tray icon: {why}"));' in run


# ═════════════════════════════════════════════════════════════════════
# What the tray shows
# ═════════════════════════════════════════════════════════════════════
STATES = {
    "Game Mode is off": dict(),
    "Game Mode is on": dict(monitoring=True),
    "a game is running": dict(monitoring=True, game="Counter-Strike 2", pid=4242),
    "a game is running, the crosshair is on": dict(monitoring=True, game="Counter-Strike 2", pid=4242, crosshair_on=True),
    "a game the app has no name for": dict(monitoring=True, pid=4242),
    "a game with a very long name": dict(monitoring=True, pid=7, game="A Game With " + "A Very " * 9 + "Long Name"),
    "a game runs although Game Mode was switched off": dict(game="Counter-Strike 2", pid=4242),
    "only the crosshair is on": dict(crosshair_on=True),
}


def test_what_the_app_says_the_tray_shows_is_what_todays_tray_looks_at():
    app()
    for name, state in STATES.items():
        with behind_the_shell() as c, in_state(**state):
            said = c.get("/api/tray/state", headers=auth()).get_json()
            status = game_profiles.get_engine_status()
            assert said == {"ok": True, "monitoring": bool(status["monitoring"]), "playing": bool(status["active_pid"]),
                            "active_game": status["active_game"] or None, "crosshair": crosshair.is_enabled()}, name
            assert said["monitoring"] is state.get("monitoring", False) and said["crosshair"] is state.get("crosshair_on", False)
    # It is asked every three seconds, as today's tray asks: it does not
    # build what the page shows, and it starts no program.
    src = inspect.getsource(A.api_tray_state)
    assert "get_engine_status()" in src and "crosshair.is_enabled()" in src and "get_state" not in src
    with behind_the_shell() as c, in_state():
        del m.CMDS[:]
        del m.PSC[:]
        for _ in range(3):
            assert c.get("/api/tray/state", headers=auth()).status_code == 200
        assert m.CMDS == [] and m.PSC == [], "asking what the tray shows started a program"
    watcher = inspect.getsource(A._create_tray_icon)
    assert "time.sleep(3)" in watcher and "pub const LOOK_EVERY: Duration = Duration::from_secs(3);" in S.rust("src", "tray.rs")
    # A backend that is up is asked every three seconds whatever it answers:
    # the short wait is for a backend that is not up, when nothing is sent.
    # (With the wait taken from the answer, a state call that fails was
    # asked four times a second for as long as the app ran.)
    tray = S.app_crate("src", "tray.rs")
    assert "let pause = tray::look_again_in(shell.phase() == Phase::Ready);" in tray
    assert "state.is_some()" not in tray and "LOOK_SOON" not in tray, "the wait depends on the answer again"
    # What goes wrong with the icon is said once, not at every round.
    assert "if let Some(line) = complaints.failed(&why.to_string()) {" in tray and "if let Some(line) = complaints.worked() {" in tray
    assert "could not be brought up to date" not in tray, "the words are the core's, and are said through Complaints"


def test_the_shell_shows_the_menu_the_picture_and_the_tooltip_todays_tray_shows():
    app()
    if not S.TRY:
        return
    for name, state in STATES.items():
        with behind_the_shell() as c, in_state(**state):
            said = c.get("/api/tray/state", headers=auth()).get_json()
            today, shell = as_today(todays_tray()), the_shells(said)
        assert shell["menu"] == today["menu"], f"{name}:\n  today's {today['menu']}\n  the shell's {shell['menu']}"
        assert shell["picture"] == today["picture"], (name, shell["picture"], today["picture"])
        if state.get("monitoring") and not state.get("pid"):
            # The one place where the two differ, and on purpose.  Today's
            # tray writes its tooltip only when the game changes, so "Game
            # Mode is on" shows in it only after a game has ended: until
            # then it still says what it said before.  The shell's says what
            # is so.  The words are today's (the next test sees today's tray
            # say them).
            assert (today["tooltip"], shell["tooltip"]) == ("Vispora", "Vispora: game mode armed"), name
        else:
            assert shell["tooltip"] == today["tooltip"], (name, shell["tooltip"], today["tooltip"])
    # The first entry is the one a click on the icon stands for, today and
    # in the shell; "Force Normal Mode" is there only while a game runs.
    with in_state():
        icon = todays_tray()
    first = icon.menu.items[0]
    assert (first.text, first.default) == ("Open Vispora", True)
    assert [e for e in the_shells(None)["ids"] if e != "line"][0] == "open"
    # Before the app has said anything (it is still starting), the shell
    # shows the menu of "Game Mode is off", and only its own two entries
    # can be clicked.
    early, off = the_shells(None), the_shells({"ok": True})
    assert [e and e[0] for e in early["menu"]] == [e and e[0] for e in off["menu"]]
    assert [e[0] for e in early["menu"] if e and e[2]] == ["Open Vispora", "Quit"]


def test_todays_tray_says_game_mode_is_on_in_the_same_words_once_a_game_has_ended():
    """(It takes one turn of today's tray watcher, which is three seconds.)"""
    app()
    with in_state(monitoring=True, game="Counter-Strike 2", pid=4242):
        icon = todays_tray()
        assert icon.title == "Vispora: playing Counter-Strike 2"
        seen = len(icon.updated_from)
        game_profiles._engine.update(active_game=None, active_pid=None)         # the game ends
        assert m.wait(lambda: len(icon.updated_from) > seen, 8), "today's tray did not look again"
        assert icon.title == "Vispora: game mode armed"
    if S.TRY:
        assert the_shells({"ok": True, "monitoring": True})["tooltip"] == icon.title


def test_the_trials_menu_is_todays_menu():
    """shell_rs/app/try_it.py reads the tray's menu as Windows has it and
    compares it with a list written there.  That list is today's menu."""
    app()
    source = S.app_crate("try_it.py")
    written = re.search(r"^TODAYS_MENU_WHEN_OFF = (\[.*?\])$", source, re.M | re.S).group(1)
    with in_state():
        today = as_today(todays_tray())
    assert ast.literal_eval(written) == [entry and entry[0] for entry in today["menu"]]


# ═════════════════════════════════════════════════════════════════════
# What an entry does
# ═════════════════════════════════════════════════════════════════════
class Watched:
    """The app's doors for the tray's entries, watched: what was asked of
    the machine, and what the person was told."""

    def __init__(self, ram=None, crosshair_answer=None):
        self.asked, self.told = [], []
        self.ram = ram or {"ok": True, "freed_mb": 512}
        self.crosshair_on, self.crosshair_answer = False, crosshair_answer

    def _monitor(self, on):
        self.asked.append("start watching for games" if on else "stop watching for games")
        game_profiles._engine["monitoring"] = on
        return {"ok": True}

    def _set_crosshair(self, on):
        self.asked.append(f"crosshair {'on' if on else 'off'}")
        answer = self.crosshair_answer(on) if self.crosshair_answer else {"ok": True, "status": {"reason": "running"}}
        if answer.get("ok"):
            self.crosshair_on = bool(on)
        return answer

    @contextlib.contextmanager
    def watching(self):
        with changed(game_profiles, start_monitoring=lambda: self._monitor(True), stop_monitoring=lambda: self._monitor(False),
                     restore_normal_mode=lambda reason: self.asked.append(f"normal mode ({reason})")), \
                changed(power, cleanup_only=lambda: self.asked.append("refresh") or {"ok": True}), \
                changed(cleaner, clean_ram=lambda: self.asked.append("clean ram") or self.ram), \
                changed(crosshair, is_enabled=lambda: self.crosshair_on, set_enabled=self._set_crosshair), \
                changed(notifier, toast=lambda title, message, force=False: self.told.append((title, message)) or {"ok": True}), \
                changed(game_profiles._engine, monitoring=False, active_game=None, active_pid=None):
            yield self

    def settled(self, asked, told):
        assert m.wait(lambda: len(self.asked) >= asked and len(self.told) >= told), (self.asked, self.told)
        return list(self.asked), list(self.told)


ENTRIES = {
    # the words in today's menu: (the shell's call, how often it is chosen, what is asked of the machine, messages)
    "Game Mode": ("game-mode", 2, 2, 2),
    "Force Normal Mode": ("force-normal", 1, 1, 1),
    "Refresh PC now": ("refresh-pc", 1, 1, 1),
    "Clean RAM now": ("clean-ram", 1, 1, 1),
    "Crosshair": ("crosshair", 2, 2, 0),
}


def chosen_today(words, times, asked, told, **how):
    with Watched(**how).watching() as doors:
        icon = todays_tray()
        item = next(i for i in icon.menu.items if i is not _Menu.SEPARATOR and i.text == words)
        for n in range(times):
            item.action(icon, item)
            doors.settled(asked * (n + 1) // times, told * (n + 1) // times)
        return doors.settled(asked, told)


def chosen_in_the_shell(call, times, asked, told, **how):
    with Watched(**how).watching() as doors, behind_the_shell() as c:
        for n in range(times):
            answer = c.post(f"/api/tray/{call}", headers=auth(), json={})
            assert answer.status_code == 200 and answer.get_json()["ok"] is True, (call, answer.get_data())
            doors.settled(asked * (n + 1) // times, told * (n + 1) // times)
        return doors.settled(asked, told)


def test_each_call_does_what_the_entry_of_todays_menu_does():
    app()
    expected = {
        "Game Mode": (["start watching for games", "stop watching for games"],
                      [("Game Mode", "Now watching for games."), ("Game Mode", "Monitoring stopped.")]),
        "Force Normal Mode": (["normal mode (user-tray)"], [("Normal Mode", "All gaming tweaks reverted.")]),
        "Refresh PC now": (["refresh"], [("PC Refreshed", "Standby cleared, DNS flushed, MMCSS reset.")]),
        "Clean RAM now": (["clean ram"], [("RAM Cleaned", "Freed 512 MB")]),
        "Crosshair": (["crosshair on", "crosshair off"], []),
    }
    for words, (call, times, asked, told) in ENTRIES.items():
        today = chosen_today(words, times, asked, told)
        shell = chosen_in_the_shell(call, times, asked, told)
        assert today == shell == expected[words], f"{words}:\n  today {today}\n  behind the shell {shell}"
    # Every entry of today's menu is covered: the two that are the shell's
    # own need no call, and the status line does nothing.
    with in_state(game="x", pid=1):
        texts = [i.text for i in todays_tray().menu.items if i is not _Menu.SEPARATOR]
    assert set(texts) - set(ENTRIES) == {"Open Vispora", "Quit", "▶  x"}, texts
    if S.TRY:
        ids = the_shells({"ok": True, "playing": True, "active_game": "x"})["ids"]
        calls = [i for i in ids if i not in ("open", "status", "quit", "line")]
        assert calls == ["game-mode", "force-normal", "crosshair", "refresh-pc", "clean-ram"]
        assert sorted(calls) == sorted(call for call, *_ in ENTRIES.values())


def test_what_goes_wrong_is_said_as_todays_tray_says_it():
    app()
    # The RAM could not be cleaned.
    failed = {"ram": {"ok": False}}
    today, shell = chosen_today("Clean RAM now", 1, 1, 1, **failed), chosen_in_the_shell("clean-ram", 1, 1, 1, **failed)
    assert today == shell == (["clean ram"], [("RAM Cleaned", "RAM clean failed")])
    # The crosshair is saved as on, and its window did not open.
    no_window = {"crosshair_answer": lambda on: {"ok": True, "status": {"reason": "error", "last_error": "No display to draw on."}}}
    today, shell = chosen_today("Crosshair", 1, 1, 1, **no_window), chosen_in_the_shell("crosshair", 1, 1, 1, **no_window)
    assert today == shell == (["crosshair on"], [("Crosshair didn't start", "No display to draw on.")])
    # The setting could not be changed at all, with and without a reason.
    for answer, words in (({"ok": False, "err": "The settings file is read-only."}, "The settings file is read-only."),
                          ({"ok": False}, "Couldn't change the crosshair setting.")):
        refused = {"crosshair_answer": lambda on, answer=answer: answer}
        today, shell = chosen_today("Crosshair", 1, 1, 1, **refused), chosen_in_the_shell("crosshair", 1, 1, 1, **refused)
        assert today == shell == (["crosshair on"], [("Crosshair didn't start", words)])
    # A door that throws is noted in the log, and the call still answers.
    def broken():
        raise OSError("the machine said no")
    with Watched().watching() as doors, changed(power, cleanup_only=broken), behind_the_shell() as c:
        assert c.post("/api/tray/refresh-pc", headers=auth(), json={}).get_json() == {"ok": True}
        assert not m.wait(lambda: doors.told, 0.3), "a message for something that did not happen"


def test_the_calls_come_back_at_once_where_todays_entries_do():
    """Today's entries that take time do their work on a thread of their
    own, so that the menu does not hang; so do the calls, so that the tray
    does not wait."""
    a = app()
    for call in ("force-normal", "crosshair", "refresh-pc", "clean-ram"):
        src = inspect.getsource(a.app.view_functions[next(r.endpoint for r in a.app.url_map.iter_rules() if r.rule == f"/api/tray/{call}")])
        assert "threading.Thread(target=_bg, daemon=True" in src, call
    gate = threading.Event()
    with Watched().watching() as doors, changed(power, cleanup_only=lambda: gate.wait(10) and doors.asked.append("refresh")), \
            behind_the_shell() as c:
        assert c.post("/api/tray/refresh-pc", headers=auth(), json={}).get_json() == {"ok": True}
        assert doors.asked == [] and doors.told == [], "it answered before the work was done"
        gate.set()
        assert doors.settled(1, 1) == (["refresh"], [("PC Refreshed", "Standby cleared, DNS flushed, MMCSS reset.")])


# ═════════════════════════════════════════════════════════════════════
# The pictures, and today's tray as it was
# ═════════════════════════════════════════════════════════════════════
def test_the_pictures_the_shell_carries_are_the_ones_the_app_draws():
    app()
    for look in LOOKS:
        with open(os.path.join(S.CORE, "pictures", f"tray-{look}.rgba"), "rb") as f:
            carried = f.read()
        assert carried == A._render_tray_image(look).tobytes(), \
            f"the app draws its {look} tray icon differently now: run shell_rs/core/pictures/make.py again"
        if S.TRY:
            out = m.REAL_POPEN([S.TRY, "--tray-picture", look], stdout=subprocess.PIPE).communicate(timeout=30)[0]
            assert bytes.fromhex(out.decode().strip()) == carried, look
    tray = S.rust("src", "tray.rs")
    for look in LOOKS:
        assert f'include_bytes!("../pictures/tray-{look}.rgba")' in tray, look
    assert "Image::new(tray::picture(look.icon), side, side)" in S.app_crate("src", "tray.rs")


def test_todays_tray_is_as_it_was():
    """The calls are new; the tray the app has when it is started as it
    always was is not touched by them."""
    a = app()
    tray = inspect.getsource(a._create_tray_icon)
    assert "/api/tray" not in tray and "_behind_shell" not in tray and "request" not in tray
    main = inspect.getsource(a.main)
    assert "threading.Thread(target=_create_tray_icon, args=(show_window, quit_app), daemon=True)" in main
    # Behind the shell the app has no tray of its own: main() has left
    # before it comes to it (tests/test_backend_mode.py holds that).
    assert main.index("if shell is not None:") < main.index("_create_tray_icon")
    with in_state():
        today = as_today(todays_tray())
    assert today == {"picture": "idle", "tooltip": "Vispora", "menu": [
        ("Open Vispora", None, True), None, ("○  Game Mode: off", None, False), ("Game Mode", False, True),
        ("Crosshair", False, True), None, ("Refresh PC now", None, True), ("Clean RAM now", None, True), None,
        ("Quit", None, True)]}


# ═════════════════════════════════════════════════════════════════════
# Starting at sign-in
# ═════════════════════════════════════════════════════════════════════
def command_in(xml):
    return re.search(r"<Command>(.*?)</Command>\s*<Arguments>(.*?)</Arguments>", xml, re.S).groups()


def test_started_as_it_always_was_the_entry_names_what_it_named():
    app()
    assert autostart._app_exe is None
    assert autostart._current_exe_path() is None, "from source there is no exe to name"
    with changed(sys, frozen=True):
        own = os.path.abspath(sys.executable)
        assert autostart._current_exe_path() == own
        assert command_in(autostart._build_task_xml(autostart._current_exe_path(), 30)) == (own, "--from-autostart")
    # Nothing but the shell's hand-over sets another exe.
    source = S.repo("app.py")
    assert source.count("use_app_exe(") == 2, "for the entry at sign-in, and for the updater (tests/test_one_file.py)"
    serve = inspect.getsource(A._serve_for_the_shell)
    assert 'autostart.use_app_exe(handoff.get("shell_exe"))' in serve
    assert "use_app_exe" not in inspect.getsource(A.main)


def test_behind_the_shell_the_entry_names_the_shell():
    """Behind the shell this process is the app's backend, and what a
    person starts is the shell.  The shell says where it is, and that is
    what the entry names.  (What decides the entry is tried; no entry is
    made: schtasks and reg are the fake machine's, and the one call that
    would make the task is caught before it gets even there.)"""
    app()
    shell = os.path.join(m.ROOT, "Program Files", "Vispora & Co", "Vispora.exe")
    os.makedirs(os.path.dirname(shell), exist_ok=True)
    with open(shell, "wb"):
        pass
    made = []

    def door(cmd_list, timeout=20):
        if cmd_list[:2] == ["schtasks", "/Create"]:
            with open(cmd_list[cmd_list.index("/XML") + 1], "rb") as f:
                made.append((cmd_list, f.read()))
            return {"ok": True, "out": "SUCCESS", "err": "", "rc": 0}
        return real_door(cmd_list, timeout)

    real_door = autostart._run
    try:
        autostart.use_app_exe(shell)
        assert autostart._current_exe_path() == shell
        with changed(sys, frozen=True):
            assert autostart._current_exe_path() == shell, "the backend's own exe is not what a person starts"
        command, arguments = command_in(autostart._build_task_xml(autostart._current_exe_path(), 45))
        assert (command, arguments) == (shell.replace("&", "&amp;"), "--from-autostart")
        assert autostart.get_status()["exe_path"] == shell
        # Switched on behind the shell: the task that would be made.
        del m.CMDS[:]
        with changed(autostart, _run=door):
            answer = autostart.enable(delay_seconds=45)
        assert answer["ok"] is True and answer["exe_path"] == shell, answer
        assert len(made) == 1 and made[0][0][:2] == ["schtasks", "/Create"] and autostart.TASK_NAME in made[0][0]
        xml = made[0][1]
        assert xml[:2] == b"\xff\xfe"
        assert command_in(xml[2:].decode("utf-16-le")) == (shell.replace("&", "&amp;"), "--from-autostart")
        assert "<Delay>PT45S</Delay>" in xml[2:].decode("utf-16-le")
        assert not [c for c in m.CMDS if "/create" in str(c).lower()], "a task was asked of the (fake) machine"
        # A shell that does not say where it is: as before.
        autostart.use_app_exe(None)
        assert autostart._current_exe_path() is None
    finally:
        autostart.use_app_exe(None)
        autostart._save_settings({"enabled": False, "delay_seconds": autostart.STARTUP_DELAY_SECONDS_DEFAULT})


def test_the_shell_says_where_it_is_and_the_backend_takes_its_word():
    """The two together, for real, where the trial tool has been built: the
    tool stands in for the shell and starts the app on the fake machine as
    its backend.  The backend then answers the tray's calls, and names the
    tool's exe as the one to start at sign-in."""
    app()
    if not S.TRY:
        return
    t = S.Trial()
    try:
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        said = t.ask(do="own", method="GET", path="/api/tray/state")["answer"]
        assert said == {"ok": True, "monitoring": False, "playing": False, "active_game": None, "crosshair": False}, said
        assert t.ask(do="own", method="POST", path="/api/tray/game-mode")["answer"] == {"ok": True}
        assert t.ask(do="own", method="GET", path="/api/tray/state")["answer"]["monitoring"] is True
        assert t.ask(do="own", method="POST", path="/api/tray/game-mode")["answer"] == {"ok": True}
        assert t.ask(do="own", method="GET", path="/api/tray/state")["answer"]["monitoring"] is False
        # A page's call for the same thing is passed on like any other.
        through = t.call("GET", "/api/tray/state")
        assert through["status"] == 200 and json.loads(through["body"])["ok"] is True, through
        # Where the shell is.
        status = json.loads(t.call("GET", "/api/autostart")["body"])
        assert os.path.normcase(status["exe_path"]) == os.path.normcase(S.TRY), status
    finally:
        t.close()
