"""The Adaptive Tuning card on the Profiles page, and what this change did to the
GPU page and the Installed Games card.

tests/fixtures/at2_states.json holds one complete answer of GET /api/adaptive/state
per state of the card (a plain object: state name -> answer; "load_failed" is
{"ok": false}).  The fixtures are run through the card's pure text functions in
static/js/app.js and through the card's real markup from templates/index.html,
in headless Edge the way test_static.py loads the script (no Node here).  Every
sentence is compared with the agreed wording, and nothing a user can read may
use a banned word.  fetch is replaced inside the page: nothing leaves it."""
import sandbox as m

import base64
import copy
import io
import json
import os
import pathlib
import re
import subprocess
import time
import unittest

FIXTURES = os.path.join(m.REPO, "tests", "fixtures", "at2_states.json")
NOW = 1794571200            # 13 Nov 2026 12:00 UTC: "now" for every fixture
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BANNED = re.compile(r"\b(offsets?|tdrs?|blacklist\w*|baseline\w*|yielded|stock|strikes?)\b", re.I)
BANNED_AT = re.compile(r"\bAT\b")           # the abbreviation, not the word "at"
REFUSED = "Close the game first. A test can't run while a game is using the graphics card."
GPU_BUTTONS = ["at2-gpu-test-stability", "at2-gpu-test-benchmark", "auto-oc-start-btn", "bench-tune-start-btn"]

# The states section 8 of the spec lists; the file may hold more.
SPEC_STATES = ["load_failed", "not_supported", "no_overclock", "off", "off_with_errors", "off_apply_on_startup_false",
               "on_idle", "not_watching", "test_running", "in_game_full", "in_game_75", "held_manual", "held_hot",
               "held_other_program", "write_refused", "game_switched_off", "eased_game_with_suggestion", "not_helping",
               "problem_with_no_overclock", "limit_changed", "upgrade_note_old_switch_on"]

# States the fix round added (the fix plan's list).
FIX_STATES = ["warning_open", "watch_mode", "in_game_warning", "held_released", "off_released", "not_watching_released",
              "forgotten", "forgotten_released", "monitor_starting", "log_unreadable", "off_with_games"]

# get_state()'s keys (spec section 4, and the fix plan's I-engine additions), exactly.
STATE_KEYS = {"ok", "enabled", "notify", "sensitivity", "upgrade_note", "old_switch_was_on", "supported", "limit",
              "watching", "tuner_running", "monitor_starting", "game", "session", "games", "health", "history"}
SUPPORTED_KEYS = {"ok", "reason"}
LIMIT_KEYS = {"has_profile", "core", "mem", "power", "apply_on_startup"}
GAME_KEYS = {"exe", "name", "looked_after", "reason"}
SESSION_KEYS = {"exe", "name", "level", "level_name", "percent", "core", "mem", "since", "applied_ok", "held"}
ROW_KEYS = {"exe", "name", "enabled", "level", "level_name", "percent", "core", "mem", "sessions", "hours",
            "hours_at_level", "last_played", "last_problem", "not_helping", "problem_with_no_overclock", "limit_changed",
            "can_step_up", "next_up_name", "can_step_down", "suggest_step_up", "best_left_here", "plain_crash_hint",
            "warning_open", "eased_by_driver"}
PROBLEM_KEYS = {"ts", "kind", "text"}
HEALTH_KEYS = {"available", "errors_30d", "events_30d", "last_error_ts", "at_stock_90d", "covered_days", "log_readable"}
HISTORY_KEYS = {"ts", "exe", "name", "text"}

LEAD = ("Adaptive Tuning runs your games on your saved overclock and eases it off, just for that game, if the graphics "
        "driver keeps having problems there. It never raises anything above what you saved.")
LEVELS = "Each repeat problem takes that game one step down: full, 75%, 50%, then no overclock. You can undo any step."
LEVELS_WATCH = "It is set to only tell you, so nothing is eased by itself. You can ease a game one step yourself."
NOTE = ("Adaptive Tuning is new in this version. It no longer raises your overclock while you play. It keeps the overclock "
        "you saved and eases it off in a game where the graphics driver keeps having problems.")
NOTE_OLD_ON = NOTE + " It is off until you turn it on again, because it now covers every game."
OFF = "Off. Your saved overclock: +450 MHz core, +2225 MHz memory."
ON = "On. Games run at +450 MHz core, +2225 MHz memory unless one needs less."
NOT_WATCHING = "On, but Vispora isn't watching for games. Press Start Monitoring above."
RELEASED_TAIL = " Call of Duty is still running, so the overclock is left as it is until the game closes."
FORGOTTEN = "Now: Call of Duty. It isn't looked after this time, because you asked Adaptive Tuning to forget it."
NO_ERRORS = "No graphics driver errors in the last 30 days."
LOG_UNREADABLE = ("Vispora can't read the Windows event log on this PC, so it can't tell when the graphics driver has "
                  "problems. Games still get your saved overclock.")
MAIN_OFF = ("Adaptive Tuning is off, so these games run on whatever overclock is on the graphics card (your full saved "
            "overclock, when it is applied at start-up). Their levels are kept for when you turn it on again.")
RUNNING = "{0} is running now. Adaptive Tuning stops looking after it, and it won't get more overclock before it closes."
RESET_Q = ("Forget everything Adaptive Tuning learned about your games?\n\nEvery game goes back to full overclock, games "
           "you switched off are looked after again, and the list under What happened is cleared.")
FORGET_Q = "Forget {0}?\n\nWhat Adaptive Tuning learned about it is removed. It starts again at full overclock."
FORGET_OFF_Q = ("Forget {0}?\n\nWhat Adaptive Tuning learned about it is removed, its switch here included. Next time it "
                "runs, it is looked after again, at full overclock.")
SWITCH_TIP = "Off: Adaptive Tuning leaves this game alone. To run it with less overclock, use Ease one step."
NO_GAMES = "Play a game with Adaptive Tuning on and it will show up here."
LIB_LEAD = ("Lists the games installed on this PC. Vispora watches the recognised ones by itself. Add the others so game "
            "mode and Adaptive Tuning can see them.")

STATUS = {
    "load_failed": "Couldn't load Adaptive Tuning. Try again.",
    "not_supported": "Adaptive Tuning needs an NVIDIA graphics card. This PC has AMD Radeon RX 7800 XT.",
    "no_overclock": "There's no overclock to look after yet. Quick Tune (GPU page, under Auto Overclock) finds one in "
                    "about 5 to 8 minutes, or set your own there and press Save Profile.",
    "off": OFF,
    "off_with_errors": OFF,
    "off_apply_on_startup_false": OFF + " It isn't applied when Windows starts.",
    "on_idle": ON,
    "on_idle_apply_on_startup_false": ON + " Your overclock is only put on while a game runs. It comes off when the "
                                           "game closes.",
    "not_watching": NOT_WATCHING,
    "test_running": "Paused while a test runs on the GPU page.",
    "in_game_full": "Now: Call of Duty at full overclock (+450 MHz core, +2225 MHz memory).",
    "in_game_75": "Now: Call of Duty at 75% of your overclock (+338 MHz core, +1669 MHz memory).",
    "in_game_no_overclock": "Now: Marvel Rivals with no overclock.",
    "held_manual": "Now: Call of Duty. You changed the overclock yourself, so it's left alone until the game closes.",
    "held_hot": "Now: Call of Duty with no overclock. The graphics card got too hot, so it stays off until the game "
                "closes.",
    "held_other_program": "Now: Call of Duty. Another program keeps changing the overclock, so it's left alone until "
                          "the game closes.",
    "write_refused": "Now: Call of Duty. The graphics card did not accept the overclock. Close other overclocking tools "
                     "and start the game again.",
    "game_switched_off": "Now: Counter-Strike 2. Adaptive Tuning is switched off for this game.",
    "game_test_running": "Now: Counter-Strike 2. It started while a test was running, so it isn't looked after this time.",
    "game_card_unreadable": "Now: Counter-Strike 2. Vispora couldn't read the graphics card, so it isn't looked after "
                            "this time.",
    "eased_game_with_suggestion": ON,
    "not_helping": ON,
    "problem_with_no_overclock": ON,
    "limit_changed": "On. Games run at +400 MHz core, +2000 MHz memory unless one needs less.",
    "upgrade_note_old_switch_on": OFF,
    "upgrade_note": OFF,
    "warning_open": ON,
    "watch_mode": ON,
    "in_game_warning": "Now: Counter-Strike 2 at full overclock (+450 MHz core, +2225 MHz memory).",
    "held_released": "Now: Call of Duty. It isn't looked after any more this time, so the overclock is left as it is "
                     "until the game closes.",
    "off_released": OFF + RELEASED_TAIL,
    "not_watching_released": NOT_WATCHING + RELEASED_TAIL,
    "forgotten": FORGOTTEN,
    "forgotten_released": FORGOTTEN + " The overclock is left as it is until the game closes.",
    "monitor_starting": "On. Vispora starts watching for games in a moment.",
    "log_unreadable": ON,
    "off_with_games": OFF,
}
KIND = {
    "load_failed": "load_failed", "not_supported": "unsupported", "no_overclock": "no_overclock", "off": "off",
    "off_with_errors": "off", "off_apply_on_startup_false": "off", "on_idle": "on_idle",
    "on_idle_apply_on_startup_false": "on_idle", "not_watching": "not_watching", "test_running": "test_running",
    "in_game_full": "session", "in_game_75": "session", "in_game_no_overclock": "session", "held_manual": "session",
    "held_hot": "session", "held_other_program": "session", "write_refused": "session",
    "game_switched_off": "game_not_looked_after", "game_test_running": "game_not_looked_after",
    "game_card_unreadable": "game_not_looked_after", "eased_game_with_suggestion": "on_idle", "not_helping": "on_idle",
    "problem_with_no_overclock": "on_idle", "limit_changed": "on_idle", "upgrade_note_old_switch_on": "off",
    "upgrade_note": "off",
    "warning_open": "on_idle", "watch_mode": "on_idle", "in_game_warning": "session", "held_released": "session",
    "off_released": "off", "not_watching_released": "not_watching", "forgotten": "game_not_looked_after",
    "forgotten_released": "session", "monitor_starting": "not_watching", "log_unreadable": "on_idle",
    "off_with_games": "off",
}
WARN = {"load_failed", "not_watching", "held_manual", "held_hot", "held_other_program", "write_refused",
        "held_released", "not_watching_released", "forgotten_released"}
LIVE = {"in_game_full", "in_game_75", "in_game_no_overclock", "in_game_warning"}

# A warning carries no promise (D4); the page adds one only while it is true.
W_ENDED = "{0} closed after a graphics driver error. Nothing was changed."
W_SURVIVED = "{0} had a graphics driver error but kept running. Nothing was changed."
PROMISE = "One more within 10 hours of play and it will be eased."
PROMISE_WATCH = "One more within 10 hours of play and you will be told."
EASED = "{0} had graphics driver errors twice within 10 hours of play. Eased from {1} to {2}."
H_NOT_HELPING = ("{0} had graphics driver errors again soon after it was eased, so easing does not seem to help. It was "
                 "left where it is. You can ease it one more step yourself.")
H_NONE = "{0} had graphics driver errors with no overclock on the card, so the overclock is probably not the cause."
PLAIN_CRASH = "{0} closed unexpectedly. No graphics driver error was logged, so the overclock was left alone."
WATCH = ("{0}: Adaptive Tuning would have eased it to 75% of your overclock after graphics driver errors. It is set to "
         "only tell you, so nothing was changed.")
USER_SET = "You set {0} to {1}."
HINT_STEADY = "Steady for 20 hours at this level. You could try full overclock again."
HINT_LEFT = "It had problems at the level above twice, so it is best left here."
HINT_LIMIT = ("Your saved overclock changed since this was eased. It runs at 75% of the old or the new one, whichever "
              "is less.")
HINT_PLAIN = ("Closed unexpectedly 3 times with no graphics driver error. If you think the overclock is the cause, ease "
              "it one step.")


def _read(*parts):
    return io.open(os.path.join(m.REPO, *parts), encoding="utf-8-sig").read()


def _states():
    return json.loads(io.open(FIXTURES, encoding="utf-8").read())


def _utc(year, month, day):
    import calendar
    return float(calendar.timegm((year, month, day, 12, 0, 0)))


def _day(ts):
    """A day the way the card writes it, in this PC's time zone like the page: "1 Nov",
    with the year only when it is not the year of NOW."""
    t = time.localtime(ts)
    text = "%d %s" % (t.tm_mday, MONTHS[t.tm_mon - 1])
    return text if t.tm_year == time.localtime(NOW).tm_year else "%s %d" % (text, t.tm_year)


def _errors(n, days, last, tail):
    return ("The graphics driver logged errors %d %s in the last %d days (last on %s). %s"
            % (n, "time" if n == 1 else "times", days, _day(last), tail))


EASED_ON = "If it keeps happening in a game, that game is eased."
WATCH_ON = "If it keeps happening in a game, Adaptive Tuning tells you and changes nothing."
EASE_OFFER = "Adaptive Tuning can ease your overclock in the game where it keeps happening."
NOT_THE_CAUSE = "It also did that with no overclock on the card, so the overclock may not be the cause."


def _banned(text):
    """Every banned word in something a user can read (a game's own name is not the card's wording)."""
    text = str(text).replace("Counter-Strike", "")
    found = [w.group(0) for w in BANNED.finditer(text)] + [w.group(0) for w in BANNED_AT.finditer(text)]
    if "\u2014" in text:
        found.append("an em dash")
    return found


def _strings(value):
    """Every string inside a JSON-like value (dictionary keys are not text a user reads)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


# ── the fixtures themselves ───────────────────────────────────────────
def test_the_fixture_file_has_every_card_state_with_exactly_the_agreed_keys():
    states = _states()
    missing = [name for name in SPEC_STATES + FIX_STATES if name not in states]
    assert missing == [], missing
    assert set(states) == set(STATUS), "every fixture needs an expected status line: %s" % sorted(set(states) ^ set(STATUS))
    assert states["load_failed"] == {"ok": False}
    for name, s in states.items():
        if name == "load_failed":
            continue
        assert set(s) == STATE_KEYS, (name, sorted(set(s) ^ STATE_KEYS))
        assert s["ok"] is True, name
        assert s["sensitivity"] in ("balanced", "strict", "watch"), name
        for key in ("enabled", "notify", "upgrade_note", "old_switch_was_on", "watching", "tuner_running",
                    "monitor_starting"):
            assert isinstance(s[key], bool), (name, key)
        assert set(s["supported"]) == SUPPORTED_KEYS and set(s["limit"]) == LIMIT_KEYS, name
        assert set(s["health"]) == HEALTH_KEYS and isinstance(s["health"]["log_readable"], bool), name
        assert s["game"] is None or set(s["game"]) == GAME_KEYS, name
        assert s["game"] is None or s["game"]["reason"] in ("", "switched_off", "test_running", "card_unreadable",
                                                            "forgotten"), name
        assert s["session"] is None or set(s["session"]) == SESSION_KEYS, name
        assert s["session"] is None or s["session"]["held"] in (None, "manual", "hot", "other_program", "released"), name
        for g in s["games"]:
            assert set(g) == ROW_KEYS, (name, g.get("exe"), sorted(set(g) ^ ROW_KEYS))
            assert g["last_problem"] is None or set(g["last_problem"]) == PROBLEM_KEYS, (name, g["exe"])
            assert g["level_name"] == ("Full", "75%", "50%", "No overclock")[g["level"]], (name, g["exe"])
            assert type(g["warning_open"]) is bool and type(g["eased_by_driver"]) is bool, (name, g["exe"])
            # what row() promises about these two (core/adaptive_policy.py)
            assert not g["warning_open"] or (g["last_problem"]["kind"] == "warning" and g["level"] < 3), (name, g["exe"])
            assert not g["eased_by_driver"] or g["level"] > 0, (name, g["exe"])
        for h in s["history"]:
            assert set(h) == HISTORY_KEYS, name
        assert len(s["history"]) <= 50, name
        played = [g["last_played"] for g in s["games"]]
        assert played == sorted(played, reverse=True), "%s: games are newest played first" % name
        times = [h["ts"] for h in s["history"]]
        assert times == sorted(times, reverse=True), "%s: history is newest first" % name


def test_the_fixtures_follow_the_rules_of_the_feature():
    for name, s in _states().items():
        if name == "load_failed":
            continue
        limit = s["limit"]
        for held in [s["session"]] if s["session"] else []:
            assert held["core"] <= max(limit["core"], 0) and held["mem"] <= max(limit["mem"], 0), \
                "%s: a session never runs above the saved overclock" % name
        for g in s["games"]:
            assert g["core"] <= max(limit["core"], 0) and g["mem"] <= max(limit["mem"], 0), (name, g["exe"])
        if s["session"]:
            assert s["supported"]["ok"] and limit["has_profile"], name
            # D1: a session let go of while the game runs stays open after the switch or the monitor went off
            assert (s["enabled"] and s["watching"]) or s["session"]["held"] == "released", name
            assert not s["tuner_running"], "%s: a test can't run while a session is open" % name
        if s["game"] and s["game"]["reason"] == "forgotten":
            assert s["game"]["exe"] not in [g["exe"] for g in s["games"]], "%s: a forgotten game has no row" % name


def test_the_fixture_texts_use_no_banned_word():
    for name, s in _states().items():
        for text in _strings(s):
            assert _banned(text) == [], (name, text, _banned(text))


# ── the page: real markup, real script, fetch replaced ────────────────
def _edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        exe = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(exe):
            return exe
    return None


def _between(page, start, end):
    assert page.count(start) == 1, start
    a = page.index(start)
    return page[a:page.index(end, a)]


def _markup():
    """The pieces of the real page the card's code works on."""
    page = _read("templates", "index.html")
    cards = _between(page, '<div class="card" id="at2-card">', '<div class="section-divider">Background Pauser</div>')
    gpu = _between(page, '<div class="warn-box" id="at2-gpu-note"', '<div class="section-divider">Results</div>')
    return cards, gpu


def _crash_card():
    return _between(_read("templates", "index.html"), '<div class="card" id="crash-recovery-card">',
                    '<div class="card" id="oc-live-card">')


def _variant(state, **changes):
    s = copy.deepcopy(state)
    for path, value in changes.items():
        node = s
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[int(k) if isinstance(node, list) else k]
        node[int(keys[-1]) if isinstance(node, list) else keys[-1]] = value
    return s


def _cases(states):
    """Calls of the pure functions on variants of the fixtures: [label, function, arguments...]."""
    on, off, full, eased = states["on_idle"], states["off"], states["in_game_full"], states["in_game_75"]
    row = eased["games"][0]
    ms = NOW * 1000
    return [
        ["date_this_year", "at2DateText", _utc(2026, 11, 1), ms],
        ["date_last_year", "at2DateText", _utc(2025, 12, 19), ms],
        ["date_none", "at2DateText", None, ms],
        ["no_answer", "at2StatusText", None],
        ["answer_without_ok", "at2StatusText", {}],
        ["unsupported_no_reason", "at2StatusText", _variant(states["not_supported"], supported__reason="")],
        ["profile_of_zero", "at2StatusText", _variant(off, limit__core=0, limit__mem=0)],
        ["profile_of_zero_health", "at2HealthText", _variant(off, limit__core=0, limit__mem=0), ms],
        ["negative_part", "at2StatusText", _variant(off, limit__core=-50)],
        ["off_wins_over_not_watching", "at2StatusText", _variant(off, watching=False)],
        ["not_watching_wins_over_test", "at2StatusText", _variant(on, watching=False, tuner_running=True)],
        ["test_wins_over_game", "at2StatusText", _variant(states["game_test_running"], tuner_running=True)],
        ["unsupported_wins_over_everything", "at2StatusText", _variant(full, supported__ok=False, supported__reason="No.")],
        ["refused_wins_over_held", "at2StatusText", _variant(full, session__applied_ok=False, session__held="hot")],
        ["held_unknown", "at2StatusText", _variant(full, session__held="something_new")],
        ["level_50", "at2StatusText", _variant(eased, session__level=2, session__level_name="50%", session__percent=50,
                                               session__core=225, session__mem=1113)],
        ["session_without_name", "at2StatusText", _variant(full, session__name="")],
        ["game_unknown_reason", "at2StatusText", _variant(states["game_switched_off"], game__reason="")],
        ["game_looked_after_no_session", "at2StatusText", _variant(full, session=None)],
        ["health_one_day", "at2HealthText", _variant(on, health__covered_days=1), ms],
        ["health_no_days", "at2HealthText", _variant(on, health__covered_days=0), ms],
        ["health_no_date", "at2HealthText", _variant(states["off_with_errors"], health__last_error_ts=None), ms],
        ["health_unknown_cause_on", "at2HealthText", _variant(states["off_with_errors"], enabled=True), ms],
        ["health_not_supported", "at2HealthText", _variant(states["off_with_errors"], supported__ok=False), ms],
        ["chip_no_name", "at2ChipText", _variant(row, level_name="")],
        ["played_one", "at2PlayedText", {"hours": 1.2}],
        ["played_under_one", "at2PlayedText", {"hours": 0.9}],
        ["played_none", "at2PlayedText", {}],
        ["actions_full_name", "at2RowActions", _variant(row, next_up_name="Full")],
        ["actions_phrase", "at2RowActions", _variant(row, next_up_name="full overclock")],
        ["actions_no_name", "at2RowActions", _variant(row, next_up_name="")],
        ["actions_level_2_no_step_up", "at2RowActions", _variant(row, level=2, can_step_up=False)],
        ["row_no_problem_date", "at2RowText", _variant(row, last_problem__ts=0), ms],
    ] + _fix_cases(states, ms)


def _fix_cases(states, ms):
    """The fix round's cases: the promise, Adaptive Tuning off, released, forgotten, the log, the questions."""
    warn = states["warning_open"]
    cs2, cod, apex, fortnite = warn["games"][0], warn["games"][1], warn["games"][2], warn["games"][3]
    eased = states["in_game_75"]
    return [
        # the promise after a warning: only while it is true
        ["promise_balanced", "at2RowText", cs2, ms, warn],
        ["promise_strict", "at2RowText", cs2, ms, _variant(warn, sensitivity="strict")],
        ["promise_watch", "at2RowText", cs2, ms, _variant(warn, sensitivity="watch")],
        ["promise_main_off", "at2RowText", cs2, ms, _variant(warn, enabled=False)],
        ["promise_not_watching", "at2RowText", cs2, ms, _variant(warn, watching=False)],
        ["promise_monitor_starting", "at2RowText", cs2, ms, _variant(warn, watching=False, monitor_starting=True)],
        ["promise_no_overclock", "at2RowText", cs2, ms, _variant(warn, limit__has_profile=False)],
        ["promise_without_state", "at2RowText", cs2, ms],
        ["promise_switched_off", "at2RowText", fortnite, ms, warn],
        ["promise_closed_warning", "at2RowText", _variant(cs2, warning_open=False), ms, warn],
        ["promise_key_missing", "at2RowText", _variant(cs2, warning_open=None), ms, warn],
        ["promise_not_a_warning", "at2RowText", _variant(cs2, last_problem__kind="hold"), ms, warn],
        ["promise_no_overclock_level", "at2RowText", _variant(cs2, level=3), ms, warn],
        ["promise_50_with_errors_at_no_overclock", "at2RowText", apex, ms, _variant(warn, health__at_stock_90d=2)],
        ["promise_50", "at2RowText", apex, ms, warn],
        ["promise_closed_by_policy", "at2RowText", cod, ms, warn],
        ["promise_own_session", "at2RowText", cs2, ms,
         _variant(warn, session={"exe": "cs2.exe", "name": "Counter-Strike 2", "level": 0, "level_name": "Full",
                                 "percent": 100, "core": 450, "mem": 2225, "since": cs2["last_problem"]["ts"] - 60,
                                 "applied_ok": True, "held": None})],
        ["promise_session_before_it", "at2RowText", cs2, ms,
         _variant(warn, session={"exe": "cs2.exe", "name": "Counter-Strike 2", "level": 0, "level_name": "Full",
                                 "percent": 100, "core": 450, "mem": 2225, "since": cs2["last_problem"]["ts"] + 60,
                                 "applied_ok": True, "held": None})],
        # a game whose own switch is off: no step buttons, no advice that needs one
        ["switched_off_hints", "at2RowText", _variant(eased["games"][0], enabled=False, suggest_step_up=True,
                                                      plain_crash_hint=True, best_left_here=True), ms, eased],
        ["switched_off_actions", "at2RowActions", _variant(eased["games"][0], enabled=False)],
        ["steady_at_50", "at2RowText", _variant(apex, suggest_step_up=True, last_problem=None), ms, warn],
        ["limit_changed_at_no_overclock", "at2RowText", _variant(apex, level=3, limit_changed=True, last_problem=None), ms],
        # chips while Adaptive Tuning is off
        ["chip_main_off", "at2ChipText", eased["games"][0], False],
        ["chip_main_off_full", "at2ChipText", eased["games"][1], False],
        ["chip_main_off_switched_off", "at2ChipText", fortnite, False],
        ["chip_main_on", "at2ChipText", eased["games"][0], True],
        ["main_off_no_games", "at2MainOffText", states["off"]],
        ["main_off_on", "at2MainOffText", states["on_idle"]],
        ["main_off_apply_false", "at2MainOffText", _variant(states["off_with_games"], limit__apply_on_startup=False)],
        ["main_off_failed", "at2MainOffText", {"ok": False}],
        # the session line
        ["session_limit_changed", "at2StatusText", _variant(eased, games__0__limit_changed=True, session__core=300,
                                                            session__mem=1500)],
        # the session keeps to the lowest saved overclock it has seen (D2): its clocks are not this level's
        # share of what is saved now, although the saved overclock reads the same as when the game was eased
        ["session_lowest_limit", "at2StatusText", _variant(eased, session__core=300, session__mem=1500)],
        ["session_full_not_raised", "at2StatusText", _variant(states["in_game_full"], limit__core=500, limit__mem=2400,
                                                              games__0__core=500, games__0__mem=2400)],
        ["session_other_level_than_the_row", "at2StatusText", _variant(eased, session__level=2, session__percent=50,
                                                                       session__level_name="50%", session__core=225,
                                                                       session__mem=1113)],
        ["released_without_game", "at2StatusText", _variant(states["held_released"], game=None)],
        ["forgotten_other_game", "at2StatusText", _variant(states["forgotten_released"], game__exe="other.exe")],
        ["monitor_starting_off", "at2StatusText", _variant(states["monitor_starting"], enabled=False)],
        ["monitor_starting_released", "at2StatusText", _variant(states["not_watching_released"], monitor_starting=True)],
        ["monitor_starting_released_tone", "at2StatusTone", _variant(states["not_watching_released"], monitor_starting=True)],
        ["off_released_tone", "at2StatusTone", states["off_released"]],
        # the health line
        ["health_unreadable_off", "at2HealthText", _variant(states["log_unreadable"], enabled=False), ms],
        ["health_unreadable_cached", "at2HealthText", _variant(states["in_game_75"], health__log_readable=False), ms],
        ["health_unreadable_no_overclock", "at2HealthText", _variant(states["no_overclock"], enabled=True,
                                                                     health__log_readable=False), ms],
        ["health_readable_unknown", "at2HealthText", _variant(states["log_unreadable"], health__log_readable=None), ms],
        ["health_watch_not_the_cause", "at2HealthText", _variant(states["not_helping"], sensitivity="watch"), ms],
        ["levels_failed", "at2LevelsText", {"ok": False}],
        ["levels_strict", "at2LevelsText", _variant(states["on_idle"], sensitivity="strict")],
        # "What happened": the time of day for the last 7 days
        ["hist_recent", "at2HistDateText", NOW - 3600, ms],
        ["hist_six_days", "at2HistDateText", NOW - 6 * 86400, ms],
        ["hist_old", "at2HistDateText", NOW - 8 * 86400, ms],
        ["hist_later_than_now", "at2HistDateText", NOW + 3600, ms],
        ["hist_none", "at2HistDateText", None, ms],
        # the two Forget questions
        ["forget_switched_off", "at2ForgetQuestion", fortnite, warn],
        ["forget_running", "at2ForgetQuestion", eased["games"][0], eased],
        ["forget_other_than_running", "at2ForgetQuestion", eased["games"][1], eased],
        ["forget_running_not_looked_after", "at2ForgetQuestion", states["game_switched_off"]["games"][1],
         states["game_switched_off"]],
        ["forget_released", "at2ForgetQuestion", states["held_released"]["games"][0], states["held_released"]],
        ["forget_without_state", "at2ForgetQuestion", eased["games"][0]],
        ["reset_idle", "at2ResetQuestion", states["on_idle"]],
        ["reset_running", "at2ResetQuestion", eased],
        ["reset_failed", "at2ResetQuestion", {"ok": False}],
    ]


_DRIVER = r"""
(async function () {
    var out = { errors: errs, states: {}, cases: {}, flows: {}, consts: {} };
    function b64(s) { return btoa(unescape(encodeURIComponent(s))); }
    function $(id) { return document.getElementById(id); }
    function shown(id) { var el = $(id); return !!(el && el.getClientRects().length); }
    function list(nodes, fn) { return Array.prototype.map.call(nodes, fn); }
    async function settle() { for (var i = 0; i < 40; i++) await Promise.resolve(); }
    try {
        var FX = JSON.parse($('fx').textContent);
        var S = FX.states, NOW = FX.now * 1000, GPU = FX.gpu_buttons;
        document.querySelectorAll('#host details').forEach(function (d) { d.open = true; });

        // What the page asks the backend, tells the user and asks the user.
        var calls = [], toasts = [], confirms = [], answers = {}, sayYes = true;
        window.fetch = function (input, init) {
            var path = String(typeof input === 'string' ? input : ((input && input.url) || '')).split('?')[0];
            var body = null;
            try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) {}
            calls.push({ method: (init && init.method) || 'GET', path: path, body: body });
            var a = answers[path];
            if (Array.isArray(a)) a = a.length > 1 ? a.shift() : a[0];
            if (a === undefined) a = { ok: false, err: 'not stubbed: ' + path };
            function respond(v) {
                v = JSON.parse(JSON.stringify(v));
                return { ok: true, status: 200, headers: { get: function () { return 'application/json'; } },
                         json: function () { return Promise.resolve(v); } };
            }
            // a function answers later: it returns a promise of the answer
            if (typeof a === 'function') return a(body).then(respond);
            return Promise.resolve(respond(a));
        };
        showWarnToast = function (msg, opts) { toasts.push([String(msg), (opts && opts.title) || '']); return null; };
        window.confirm = function (q) { confirms.push(String(q)); return sayYes; };
        function begin(map, yes) { calls = []; toasts = []; confirms = []; answers = map || {}; sayYes = yes !== false; }
        function posts() { return calls.filter(function (c) { return c.method !== 'GET'; }).map(function (c) { return [c.path, c.body]; }); }
        function paths() { return calls.map(function (c) { return c.method + ' ' + c.path; }); }

        function draw(st) {
            _at2.state = null; _at2.loaded = false; _at2.failed = false; _at2.saving = false;
            _at2.busy = false; _at2.again = false; _at2.html = {}; _at2.inflight = 0; _at2.dropped = false;
            if (st && st.ok === true) _at2Apply(st); else { _at2.failed = true; _at2Render(true); }
        }
        function dom() {
            var sw = $('at2-toggle-enabled'), line = $('at2-status'), empty = $('at2-games').querySelector('.at2-empty');
            return {
                text: $('at2-card').innerText,
                status: line.textContent, warn: line.classList.contains('at2-warn'), live: line.classList.contains('at2-live'),
                switch_on: sw.classList.contains('on'), switch_dead: !!sw.disabled, switch_pending: sw.classList.contains('at2-pending'),
                switch_disabled: sw.classList.contains('at2-disabled'), aria: sw.getAttribute('aria-checked'),
                extra: list($('at2-status-extra').querySelectorAll('button'), function (b) { return [b.textContent, b.getAttribute('onclick')]; }),
                levels: shown('at2-levels'), levels_text: $('at2-levels').textContent, body: shown('at2-body'),
                health: shown('at2-health') ? $('at2-health').textContent : '',
                health_warn: $('at2-health').classList.contains('at2-warn'),
                note: shown('at2-note') ? $('at2-note-text').textContent : '',
                main_off: $('at2-games').classList.contains('at2-main-off'),
                main_off_line: shown('at2-main-off-line') ? $('at2-main-off-line').textContent : '',
                busy: $('at2-card').getAttribute('aria-busy'),
                empty: empty ? empty.textContent : '',
                games: list($('at2-games').querySelectorAll('.at2-game'), function (el) {
                    var s = el.querySelector('.at2-game-switch'), chip = el.querySelector('.at2-chip');
                    return { name: el.querySelector('.at2-game-name').textContent, chip: chip.textContent, chip_class: chip.className,
                             played: el.querySelector('.at2-game-played').textContent,
                             off: el.classList.contains('at2-game-off'), now: el.classList.contains('at2-game-now'),
                             switch_on: s.classList.contains('on'), aria: s.getAttribute('aria-checked'), tip: s.getAttribute('title'),
                             problem: (el.querySelector('.at2-game-problem') || {}).textContent || '',
                             hints: list(el.querySelectorAll('.at2-game-hint'), function (h) { return h.textContent; }),
                             buttons: list(el.querySelectorAll('.at2-game-actions button'), function (b) { return b.textContent; }) };
                }),
                history: list($('at2-history').querySelectorAll('.at2-hist-text'), function (r) { return r.textContent; }),
                notify_on: $('at2-toggle-notify').classList.contains('on'), sensitivity: $('at2-sensitivity').value,
            };
        }
        function gpu() {
            return { disabled: GPU.map(function (id) { return !!$(id).disabled; }),
                     titles: GPU.map(function (id) { return $(id).getAttribute('title') || ''; }),
                     note: shown('at2-gpu-note') ? $('at2-gpu-note').innerText.trim() : '' };
        }

        // 1. every fixture through the pure functions and through the card
        Object.keys(S).forEach(function (name) {
            var st = S[name];
            var mainOn = !!(st && st.enabled);
            var r = { kind: at2StatusKind(st), status: at2StatusText(st), tone: at2StatusTone(st),
                      health: at2HealthText(st, NOW), note: at2NoteText(st), levels: at2LevelsText(st),
                      main_off: at2MainOffText(st), reset: at2ResetQuestion(st),
                      rows: ((st && st.games) || []).map(function (g) {
                          return { exe: g.exe, chip: at2ChipText(g, mainOn), text: at2RowText(g, NOW, st), actions: at2RowActions(g),
                                   forget: at2ForgetQuestion(g, st) };
                      }) };
            draw(st);
            r.dom = dom();
            _at2GpuTake(st && st.ok === true ? st : null);
            r.gpu = gpu();
            out.states[name] = r;
        });
        FX.cases.forEach(function (c) { out.cases[c[0]] = window[c[1]].apply(null, c.slice(2)); });
        out.consts = { refused: AT2_TEST_REFUSED, no_games: AT2_NO_GAMES, no_history: AT2_NO_HISTORY, tip: AT2_GAME_SWITCH_TIP,
                       reset: AT2_RESET_QUESTION, confirm_fallback: AT2_CONFIRM_FALLBACK };

        // 2. the switches and buttons
        var F = out.flows, sw = $('at2-toggle-enabled'), p, during, row;
        draw(S.off); begin({ '/api/adaptive/settings': { ok: true, state: S.on_idle } });
        p = at2ToggleEnabled(sw); during = sw.classList.contains('on'); await p;
        F.switch_on = { during: during, after: dom().switch_on, status: dom().status, posts: posts(), toasts: toasts };

        draw(S.off); begin({ '/api/adaptive/settings': { ok: false, err: 'The settings could not be saved.' } });
        p = at2ToggleEnabled(sw); during = sw.classList.contains('on'); await p;
        F.switch_fail = { during: during, after: dom().switch_on, aria: dom().aria, status: dom().status, posts: posts(), toasts: toasts };

        draw(S.on_idle); begin({ '/api/adaptive/settings': { ok: true } , '/api/adaptive/state': S.off });
        await at2ToggleEnabled(sw); await settle();
        F.switch_answer_without_state = { calls: paths(), status: dom().status };

        draw(S.not_supported); begin({}); await at2ToggleEnabled(sw);
        F.switch_unsupported = { calls: paths(), on: dom().switch_on };
        draw({ ok: false }); begin({}); await at2ToggleEnabled(sw);
        F.switch_before_first_read = { calls: paths() };

        draw(S.on_idle); begin({ '/api/adaptive/game': { ok: true, state: S.game_switched_off } });
        row = document.querySelectorAll('#at2-games .at2-game-switch')[1];
        p = at2ToggleGame('cs2.exe', row); during = row.classList.contains('on'); await p;
        F.game_off = { during: during, posts: posts(), chips: dom().games.map(function (g) { return g.chip; }) };
        draw(S.on_idle); begin({ '/api/adaptive/game': { ok: false, err: 'Not saved.' } });
        row = document.querySelectorAll('#at2-games .at2-game-switch')[1];
        await at2ToggleGame('cs2.exe', row);
        F.game_off_fail = { switch_on: dom().games[1].switch_on, toasts: toasts };

        draw(S.eased_game_with_suggestion);
        begin({ '/api/adaptive/game/step': [{ ok: false, need_confirm: true, text: 'Call of Duty had a graphics driver error at full overclock on 18 Oct. Try it again?' },
                                             { ok: true, state: S.on_idle }] });
        document.querySelector('#at2-games .at2-game-actions button').click();      // "Try full overclock", through its onclick
        await settle();
        F.step_up_confirmed = { posts: posts(), confirms: confirms, toasts: toasts, chips: dom().games.map(function (g) { return g.chip; }) };
        draw(S.eased_game_with_suggestion);
        begin({ '/api/adaptive/game/step': { ok: false, need_confirm: true, text: 'Try it again?' } }, false);
        await at2Step('cod.exe', 'faster');
        F.step_up_declined = { posts: posts(), confirms: confirms, toasts: toasts, chip: dom().games[0].chip };
        draw(S.eased_game_with_suggestion); begin({ '/api/adaptive/game/step': { ok: false, need_confirm: true } });
        await at2Step('cod.exe', 'faster');
        F.step_up_question_missing = { confirms: confirms };
        draw(S.on_idle); begin({ '/api/adaptive/game/step': { ok: true, state: S.in_game_75 } });
        await at2Step('cod.exe', 'slower');
        F.step_down = { posts: posts(), chip: dom().games[0].chip, confirms: confirms };
        draw(S.limit_changed); begin({ '/api/adaptive/game/step': { ok: false, err: 'That game is not known.' } });
        await at2Step('r5apex.exe', 'full');
        F.step_fail = { posts: posts(), toasts: toasts };

        draw(S.on_idle); begin({ '/api/adaptive/game/forget': { ok: true, state: S.off } });
        await at2Forget('cod.exe');
        F.forget = { posts: posts(), confirms: confirms };
        draw(S.on_idle); begin({}, false); await at2Forget('cod.exe');
        F.forget_declined = { calls: paths(), confirms: confirms };

        draw(S.on_idle); begin({ '/api/adaptive/settings': { ok: true, state: S.on_idle } });
        await at2ToggleNotify($('at2-toggle-notify'));
        await at2SetSensitivity('strict');
        F.advanced = { posts: posts() };
        draw(S.on_idle); begin({ '/api/adaptive/settings': { ok: false, err: 'Not saved.' } });
        $('at2-sensitivity').value = 'watch';
        await at2SetSensitivity('watch');
        await at2ToggleNotify($('at2-toggle-notify'));
        F.advanced_fail = { sensitivity: $('at2-sensitivity').value, notify_on: dom().notify_on, toasts: toasts };
        draw(S.on_idle); begin({ '/api/adaptive/reset': { ok: true, state: S.off } });
        await at2ResetAll();
        F.reset = { posts: posts(), confirms: confirms };
        draw(S.on_idle); begin({}, false); await at2ResetAll();
        F.reset_declined = { calls: paths() };

        draw(S.upgrade_note_old_switch_on); begin({ '/api/adaptive/note/dismiss': { ok: true, state: S.off } });
        await at2DismissNote();
        F.note = { posts: posts(), note: dom().note };
        draw(S.upgrade_note); begin({ '/api/adaptive/note/dismiss': { ok: false, err: 'Not saved.' } });
        await at2DismissNote();
        F.note_fail = { note: dom().note, toasts: toasts };

        draw(S.in_game_75); begin({ '/api/adaptive/game/forget': { ok: true, state: S.forgotten } });
        await at2Forget('cod.exe');
        F.forget_running = { confirms: confirms, status: dom().status };
        draw(S.in_game_75); begin({ '/api/adaptive/reset': { ok: true, state: S.on_idle } });
        await at2ResetAll();
        F.reset_running = { confirms: confirms };

        // 2b. a save that is still out: the card says so, a read in the meantime is
        // not drawn, and other clicks do nothing until the answer is in
        var release, gate = new Promise(function (res) { release = res; });
        draw(S.off);
        begin({ '/api/adaptive/settings': function () { return gate.then(function () { return { ok: true, state: S.on_idle }; }); },
                '/api/adaptive/state': S.off });
        p = at2ToggleEnabled(sw);
        await settle();
        var during2 = { busy: dom().busy, saving_class: $('at2-card').classList.contains('at2-saving'),
                        select_disabled: $('at2-sensitivity').disabled, switch_on: sw.classList.contains('on') };
        await at2Load();                                      // a poll lands meanwhile: it reads the old state
        var afterPoll = { switch_on: sw.classList.contains('on'), status: dom().status };
        await at2ToggleEnabled(sw);                           // a second click
        await at2ToggleNotify($('at2-toggle-notify'));
        release(); await p; await settle();
        F.save_in_flight = { during: during2, after_poll: afterPoll, calls: paths(), toasts: toasts,
                             after: { switch_on: dom().switch_on, status: dom().status, busy: dom().busy,
                                      select_disabled: $('at2-sensitivity').disabled } };
        var release2, gate2 = new Promise(function (res) { release2 = res; });
        draw(S.off);
        begin({ '/api/adaptive/settings': function () { return gate2.then(function () { return { ok: false, err: 'Not saved.' }; }); },
                '/api/adaptive/state': S.on_idle });
        p = at2ToggleEnabled(sw);
        await settle();
        await at2Load();
        release2(); await p; await settle();
        F.save_fails_after_a_read = { calls: paths(), toasts: toasts, status: dom().status, busy: dom().busy };

        // 2c. a redraw of the list keeps the keyboard where it was
        draw(S.on_idle);
        var target = document.querySelectorAll('#at2-games .at2-game-switch')[1];
        target.focus();
        var focusedBefore = document.activeElement === target;
        _at2Apply(S.game_switched_off);
        F.focus = { before: focusedBefore, redrawn: !$('at2-games').contains(target),
                    key: document.activeElement ? document.activeElement.getAttribute('data-at2-key') : null };
        draw(S.in_game_75);
        document.querySelector('#at2-games [data-at2-key="faster|cod.exe"]').focus();
        _at2Apply(S.in_game_full);                            // "Try full overclock" is gone: the game's switch
        F.focus_gone = document.activeElement ? document.activeElement.getAttribute('data-at2-key') : null;

        // 2d. "Open the GPU page" goes to Quick Tune
        var realSwitch = switchPage, switched = [];
        switchPage = function (pg) { switched.push(pg); };
        try { at2OpenQuickTune(); } finally { switchPage = realSwitch; }
        F.open_quick_tune = { switched: switched, flash: $('oc-auto-card').classList.contains('at2-flash') };
        $('oc-auto-card').classList.remove('at2-flash');

        // 3. reading the state
        draw({ ok: false }); begin({ '/api/adaptive/state': S.in_game_75 });
        $('at2-status-extra').querySelector('button').click();                      // Retry
        await settle();
        F.retry = { calls: paths(), status: dom().status, body: dom().body };
        begin({ '/api/adaptive/state': { ok: false, err: 'boom' } });
        await at2Load();
        F.read_fails_later = { status: dom().status, switch_on: dom().switch_on, switch_dead: dom().switch_dead,
                               switch_pending: dom().switch_pending, body: dom().body };
        F.poll = { idle: (draw(S.on_idle), _at2PollDelay()), off: (draw(S.off), _at2PollDelay()),
                   session: (draw(S.in_game_full), _at2PollDelay()), game: (draw(S.game_switched_off), _at2PollDelay()),
                   failed: (draw({ ok: false }), _at2PollDelay()) };

        // 4. GPU page: a test never starts while a game session is open.  Only the buttons the
        // session turned off are turned on again: Quick Tune stays off on a card Vispora can't overclock.
        _ocState.capability = { ok: true, vendor: 'amd', name: 'AMD Radeon RX 7800 XT' };
        _at2GpuTake(null);
        $('auto-oc-start-btn').disabled = true;               // what loadOcCapability does on such a card
        _at2GpuTake(S.in_game_full); var gs1 = gpu();
        _at2GpuTake(S.on_idle); var gs2 = gpu();
        $('auto-oc-start-btn').disabled = false;
        _at2GpuTake(S.in_game_full); var gs3 = gpu();         // the capability check comes back while a session holds it
        _at2GpuTake(S.on_idle); var gs4 = gpu();
        F.gpu_unsupported = { session: gs1, after: gs2, marked_session: gs3, marked_after: gs4 };
        $('auto-oc-start-btn').disabled = false; _at2GpuTake(null);

        // Quick Tune's finished line points at Adaptive Tuning
        _at2GpuTake(S.on_idle); var tuneOn = _at2AfterTuneText();
        _at2GpuTake(S.off); var tuneOff = _at2AfterTuneText();
        _at2GpuTake(null); var tuneUnknown = _at2AfterTuneText();
        F.after_tune = { on: tuneOn, off: tuneOff, unknown: tuneUnknown };

        // Crash Recovery: its history and its test, in words
        begin({ '/api/gpu/crash-recovery/history': { history: [
            { ts: FX.now - 3600, exe: 'cod.exe', display_name: 'Call of Duty', kind: 'tdr', blacklisted: true, active_core: 450,
              active_mem: 2225, step_down: { core: 425, mem: 2125 }, tdr_count: 2, crash_count: 1 },
            { ts: FX.now - 7200, exe: 'cs2.exe', display_name: 'Counter-Strike 2', kind: 'inapp_signal', blacklisted: false,
              active_core: 0, active_mem: 0, step_down: null, tdr_count: 0, crash_count: 0 }] } });
        await loadCrashRecoveryHistory();
        F.crash_history = $('crash-recovery-card').innerText;
        $('gpu-terminal').innerHTML = '';
        begin({ '/api/gpu/crash-recovery/test': [{ crashed: false, kind: 'normal' }, { crashed: true, kind: 'tdr' }],
                '/api/gpu/crash-recovery/history': { history: [] } });
        await testCrashRecovery(); await testCrashRecovery();
        F.crash_test = { confirms: confirms, posts: posts(), terminal: $('gpu-terminal').innerText,
                         card: $('crash-recovery-card').innerText };
        $('gpu-terminal').innerHTML = '';
        begin({ '/api/gpu/crash-recovery/history': { history: [] } });
        await clearCrashRecoveryHistory(); await clearCrashRecoveryBlacklist();
        F.crash_clear = { confirms: confirms, calls: paths(), terminal: $('gpu-terminal').innerText };

        _ocState.capability = { ok: true, vendor: 'nvidia' };
        _ocState.profile = { exists: true };
        function tune() {
            return { active: _ocState.autoActive, stress: _ocState.stressActive, bench: _ocState.benchmarkActive,
                     start_btn: $('auto-oc-start-btn').style.display, cancel_btn: $('auto-oc-cancel-btn').style.display,
                     status: $('oc-auto-status').textContent, stress_card: $('oc-stress-card').style.display,
                     bench_text: $('oc-benchmark-content').textContent, terminal: $('gpu-terminal').innerText };
        }
        function clean() { $('gpu-terminal').innerHTML = ''; $('oc-auto-status').innerHTML = ''; $('oc-benchmark-content').innerHTML = ''; }
        var NO = { ok: false, err: AT2_TEST_REFUSED, refused: 'adaptive_session' };

        _at2GpuTake(S.in_game_full); begin({}); clean();
        await runStabilityTest(60); await startAutoOc(); await startBenchmarkTune(); await runBenchmarkComparison();
        F.known_session = { calls: paths(), toasts: toasts, confirms: confirms, tune: tune() };

        // The buttons did not know yet: the start call is refused by the backend.
        var refusedState = { '/api/adaptive/state': S.in_game_full, '/api/gpu/oc/live': { ok: true, core_max_mhz: 2800 },
                             '/api/gpu/oc/probe/start': NO, '/api/gpu/oc/auto/start': NO,
                             '/api/gpu/oc/benchmark-tune/start': NO, '/api/gpu/oc/benchmark/start': NO };
        _at2GpuTake(null); begin(refusedState); clean();
        await runStabilityTest(60); await settle();
        F.refused_stability = { calls: paths(), toasts: toasts, tune: tune(), gpu: gpu() };
        _at2GpuTake(null); begin(refusedState); clean();
        await startAutoOc(); await settle();
        F.refused_quick = { calls: paths(), toasts: toasts, tune: tune(), gpu: gpu() };
        _at2GpuTake(null); begin(refusedState); clean();
        await startBenchmarkTune(); await settle();
        F.refused_bench_tune = { calls: paths(), toasts: toasts, tune: tune() };
        _at2GpuTake(null); begin(refusedState); clean();
        await runBenchmarkComparison(); await settle();
        F.refused_benchmark = { calls: paths(), tune: tune(), gpu: gpu() };

        // A start that fails without being a refusal: stop, show err, and close what may have opened.
        _at2GpuTake(null); clean();
        begin({ '/api/adaptive/state': S.on_idle, '/api/gpu/oc/live': { ok: true }, '/api/gpu/oc/auto/start': { ok: false, err: 'Request timed out' },
                '/api/gpu/oc/auto/cancel': { ok: true }, '/api/gpu/oc/probe/start': { ok: false, err: 'Request timed out' },
                '/api/gpu/oc/probe/end': { ok: false, err: 'No active probe session' } });
        await startAutoOc(); await settle();
        F.failed_quick = { calls: paths(), toasts: toasts, tune: tune() };
        begin(answers); clean();
        await runStabilityTest(60); await settle();
        F.failed_stability = { calls: paths(), toasts: toasts, tune: tune() };

        // Quick Tune started, then its first stability check can't start: stop, and close the run.
        _at2GpuTake(null); clean();
        begin({ '/api/adaptive/state': S.on_idle, '/api/gpu/oc/live': { ok: true }, '/api/gpu/oc/auto/start': { ok: true },
                '/api/gpu/oc/auto/next': { ok: true, phase: 'core_jump', step: 1, max_step: 14, current_core: 75, current_mem: 0,
                                           recommended_duration_s: 1 },
                '/api/gpu/oc/probe/start': { ok: false, err: 'The check could not start.' }, '/api/gpu/oc/auto/cancel': { ok: true } });
        await startAutoOc(); await settle();
        F.quick_step_fails = { calls: paths(), toasts: toasts, tune: tune() };

        // 5. Installed Games
        _libraryCache = { scanned_at: FX.now, total: 4, known: 1, games: [
            { title: 'Counter-Strike 2', launcher: 'steam', exe: 'cs2.exe', known: true },
            { title: "Marvel's Spider-Man 2™", display_name: "Marvel's Spider-Man 2", launcher: 'steam', exe: 'Spider-Man2.exe', known: false },
            { title: 'Call of Duty: Black Ops 6', launcher: 'battlenet', exe: 'cod24-cod.exe', known: false },
            { title: 'Mystery Game', launcher: 'epic', exe: '', known: false } ] };
        _libraryMine = {}; _libraryFilter = 'all';
        function lib() {
            return { summary: $('library-summary').textContent,
                     rows: list($('library-list').querySelectorAll('.at2-lib-row'), function (r) {
                         var b = r.querySelector('button'), st = r.querySelector('.at2-lib-state');
                         return { title: r.querySelector('.at2-lib-title').textContent, sub: r.querySelector('.at2-lib-sub').textContent,
                                  button: b ? b.textContent : '', state: st ? st.textContent : '' };
                     }),
                     text: $('library-card').innerText,
                     all_active: $('lib-filter-all').classList.contains('active'), known_active: $('lib-filter-known').classList.contains('active') };
        }
        _renderLibrary();
        F.library = lib();
        begin({ '/api/profiles/add-game': { ok: true, exe: 'spider-man2.exe' }, '/api/profiles': { profiles: {} } });
        $('library-list').querySelector('button').click();                          // Add to my games, through its onclick
        await settle();
        F.library_added = { posts: posts(), lib: lib(), toasts: toasts };
        begin({ '/api/profiles/add-game': { ok: false, err: 'Not saved.' } });
        await addLibraryGame('cod24-cod.exe', 'Call of Duty: Black Ops 6', null);
        F.library_add_fail = { toasts: toasts, button: lib().rows[2].button };
        setLibraryFilter('known');
        F.library_known = lib();
        _libraryCache = { scanned_at: 0, games: [] }; _renderLibrary();
        F.library_no_scan = lib();
        begin({ '/api/library': { scanned_at: FX.now, games: [{ title: 'Call of Duty: Black Ops 6', launcher: 'battlenet', exe: 'cod24-cod.exe', known: false }] },
                '/api/profiles': { profiles: { custom: { exes: [{ exe: 'cod24-cod.exe', name: 'Call of Duty: Black Ops 6' }] } } } });
        _libraryFilter = 'all';
        await loadLibrary(); await settle();
        F.library_loaded = { calls: paths(), lib: lib() };

        // 6. the dashboard's pointer: a "navigate" fix is a button to that page
        F.health_row = _renderHealthRow({ id: 'vispora.adaptive', name: 'Adaptive Tuning', severity: 'ok', current: 'On',
                                          detail: '', fix: { kind: 'navigate', page: 'profiles' } });
        F.tournament_off = (function () { _renderTournament({ enabled: false }); return $('tournament-summary').textContent; })();
    } catch (e) {
        out.crash = String((e && e.stack) || e);
    }
    $('out').textContent = 'AT2[' + b64(JSON.stringify(out)) + ']';
})();
"""

_RUN = {}


def _run():
    """Everything the page reports, from one headless Edge run for the whole file."""
    if "skip" in _RUN:
        raise unittest.SkipTest(_RUN["skip"])
    if "broken" in _RUN:
        raise AssertionError(_RUN["broken"])
    if "out" in _RUN:
        return _RUN["out"]
    edge = _edge()
    if not edge:
        _RUN["skip"] = "Microsoft Edge not found"
        raise unittest.SkipTest(_RUN["skip"])
    states = _states()
    cards, gpu = _markup()
    gpu = _crash_card() + gpu
    data = {"states": states, "now": NOW, "cases": _cases(states), "gpu_buttons": GPU_BUTTONS}
    script = pathlib.Path(m.REPO, "static", "js", "app.js").as_uri()
    page = os.path.join(m.ROOT, "at2check.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write('<!doctype html><html><head><meta charset="utf-8"></head><body><pre id="out">PENDING</pre>'
                '<div class="nav-item" data-page="profiles"><svg></svg> Profiles <span class="nav-badge">3</span></div>'
                '<div id="tournament-status-pill"></div><div id="tournament-idle-block"></div>'
                '<div id="tournament-active-block"></div><div id="tournament-summary"></div>'
                '<div id="host">' + cards + gpu + '<div class="terminal" id="gpu-terminal"></div></div>'
                "<script>var errs=[];window.onerror=function(m,s,l,c){errs.push(String(m)+' @'+l+':'+c);return true;};"
                "window.addEventListener('unhandledrejection',function(e){errs.push('rejected: '+String(e.reason));"
                "e.preventDefault();});"
                "window.fetch=function(){return new Promise(function(){});};</script>"     # nothing leaves the page
                f'<script src="{script}" charset="utf-8"></script>'
                '<script id="fx" type="application/json">' + json.dumps(data).replace("</", "<\\/") + "</script>"
                "<script>" + _DRIVER + "</script></body></html>")
    try:
        p = m.REAL_POPEN([edge, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
                          f"--user-data-dir={os.path.join(m.ROOT, 'edge-profile')}", "--virtual-time-budget=4000",
                          "--dump-dom", pathlib.Path(page).as_uri()], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        dumped = p.communicate(timeout=120)[0].decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        # Edge is installed and did not do its job: that is a failure, not a
        # skip (a skip here leaves every sentence of the card unchecked).
        _RUN["broken"] = f"headless Edge is installed but did not run: {e}"
        raise AssertionError(_RUN["broken"])
    if 'id="out"' not in dumped:
        _RUN["broken"] = "headless Edge is installed but printed no page"
        raise AssertionError(_RUN["broken"])
    found = re.search(r"AT2\[([A-Za-z0-9+/=]+)\]", dumped)
    assert found, "the page did not finish: " + dumped[dumped.find('id="out"'):][:300]
    out = json.loads(base64.b64decode(found.group(1)).decode("utf-8"))
    assert "crash" not in out, out.get("crash")
    _RUN["out"] = out
    return out


def test_the_script_runs_every_fixture_without_an_error():
    out = _run()
    assert [e for e in out["errors"] if "SyntaxError" in e or "at2" in e.lower()] == [], out["errors"]
    assert set(out["states"]) == set(_states())


# ── the sentences ─────────────────────────────────────────────────────
def test_status_line_of_every_state():
    out = _run()["states"]
    for name, want in STATUS.items():
        assert out[name]["status"] == want, (name, out[name]["status"])
        assert out[name]["kind"] == KIND[name], (name, out[name]["kind"])
        assert out[name]["dom"]["status"] == want, "the card shows what the function says (%s)" % name
        tone = "warn" if name in WARN else ("live" if name in LIVE else "")
        assert out[name]["tone"] == tone, (name, out[name]["tone"])
        assert (out[name]["dom"]["warn"], out[name]["dom"]["live"]) == (tone == "warn", tone == "live"), name


def test_status_line_first_match_wins_and_odd_answers():
    c = _run()["cases"]
    assert c["no_answer"] == c["answer_without_ok"] == STATUS["load_failed"]
    assert c["unsupported_no_reason"] == "Adaptive Tuning isn't available on this PC."
    assert c["unsupported_wins_over_everything"] == "No."
    # a saved profile of +0 / +0 is no overclock to look after
    assert c["profile_of_zero"] == STATUS["no_overclock"] and c["profile_of_zero_health"] == ""
    assert c["negative_part"] == "Off. Your saved overclock: -50 MHz core, +2225 MHz memory."
    assert c["off_wins_over_not_watching"] == OFF
    assert c["not_watching_wins_over_test"] == STATUS["not_watching"]
    assert c["test_wins_over_game"] == STATUS["test_running"]
    assert c["refused_wins_over_held"] == STATUS["write_refused"]
    assert c["held_unknown"] == "Now: Call of Duty. The overclock is left alone until the game closes."
    assert c["level_50"] == "Now: Call of Duty at 50% of your overclock (+225 MHz core, +1113 MHz memory)."
    assert c["session_without_name"] == "Now: cod.exe at full overclock (+450 MHz core, +2225 MHz memory)."
    assert c["game_unknown_reason"] == "Now: Counter-Strike 2. It isn't looked after this time."
    assert c["game_looked_after_no_session"] == ON


def test_status_line_of_the_fix_round():
    """Released and forgotten sessions, the monitor about to start, a saved overclock changed since the ease."""
    c = _run()["cases"]
    # the clocks are not 75% of what is saved now: only the percentage is named
    assert c["session_limit_changed"] == "Now: Call of Duty at 75% (+300 MHz core, +1500 MHz memory)."
    assert c["session_lowest_limit"] == "Now: Call of Duty at 75% (+300 MHz core, +1500 MHz memory)."
    assert c["session_full_not_raised"] == "Now: Call of Duty at +450 MHz core, +2225 MHz memory.", \
        "a higher overclock saved mid-game is not what the game runs on yet: it is not called full"
    assert c["session_other_level_than_the_row"] == "Now: Call of Duty at 50% of your overclock (+225 MHz core, +1113 MHz memory)."
    assert c["released_without_game"] == STATUS["held_released"]
    assert c["forgotten_other_game"] == STATUS["held_released"], "the forgotten line is only for that game"
    assert c["monitor_starting_off"] == OFF
    assert c["monitor_starting_released"] == "On. Vispora starts watching for games in a moment." + RELEASED_TAIL
    assert c["monitor_starting_released_tone"] == "warn", "a game the overclock is left on is worth the colour"
    assert c["off_released_tone"] == ""
    assert _run()["states"]["monitor_starting"]["tone"] == "", "nothing to do for the user"


def test_dates_are_a_day_and_a_short_month_with_the_year_only_when_it_is_another_year():
    c = _run()["cases"]
    assert c["date_this_year"] == _day(_utc(2026, 11, 1)) and re.fullmatch(r"\d{1,2} [A-Z][a-z]{2}", c["date_this_year"])
    assert c["date_last_year"] == _day(_utc(2025, 12, 19)) and re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} 2025", c["date_last_year"])
    assert c["date_none"] == ""
    # "What happened" adds the time of day for the last 7 days (in the PC's own format)
    clock = r", \d{1,2}:\d{2}(\s?[AP]M)?"
    assert re.fullmatch(re.escape(_day(NOW - 3600)) + clock, c["hist_recent"]), c["hist_recent"]
    assert re.fullmatch(re.escape(_day(NOW - 6 * 86400)) + clock, c["hist_six_days"]), c["hist_six_days"]
    assert c["hist_old"] == _day(NOW - 8 * 86400) and c["hist_later_than_now"] == _day(NOW + 3600)
    assert c["hist_none"] == ""


def test_health_line():
    out = _run()["states"]
    want = {name: NO_ERRORS for name in STATUS}
    day = 86400
    want.update({
        "load_failed": "", "not_supported": "", "no_overclock": "",
        "not_watching": "",                                             # the log goes back less than a day
        "off_with_errors": _errors(3, 30, _utc(2026, 11, 1), EASE_OFFER),
        "in_game_75": _errors(2, 30, _utc(2026, 11, 8), EASED_ON),
        "in_game_no_overclock": _errors(6, 30, _utc(2026, 11, 11), EASED_ON),
        "eased_game_with_suggestion": _errors(2, 30, _utc(2026, 11, 9), EASED_ON),
        "not_helping": _errors(4, 30, _utc(2026, 11, 10), NOT_THE_CAUSE),
        "problem_with_no_overclock": _errors(1, 12, _utc(2026, 11, 12), EASED_ON),
        "warning_open": _errors(5, 30, NOW - day, EASED_ON),
        "watch_mode": _errors(3, 30, NOW - day, WATCH_ON),          # "Only tell me": nothing is eased
        "in_game_warning": _errors(2, 30, NOW - 600, EASED_ON),
        "held_released": _errors(2, 30, NOW - 6 * day, EASED_ON),
        "off_released": _errors(2, 30, NOW - 6 * day, EASE_OFFER),
        "not_watching_released": _errors(2, 30, NOW - 6 * day, EASED_ON),
        "forgotten": _errors(2, 30, NOW - 6 * day, EASED_ON),
        "forgotten_released": _errors(2, 30, NOW - 6 * day, EASED_ON),
        "log_unreadable": LOG_UNREADABLE,
        "off_with_games": _errors(4, 30, NOW - 2 * day, EASE_OFFER),
    })
    for name, text in want.items():
        assert out[name]["health"] == text, (name, out[name]["health"])
        # (the card formats dates against the real clock, so only the part before the date is compared)
        assert out[name]["dom"]["health"].split(" (last on")[0] == text.split(" (last on")[0], name
    c = _run()["cases"]
    assert c["health_one_day"] == c["health_no_days"] == "No graphics driver errors in the last day."
    assert c["health_no_date"] == "The graphics driver logged errors 3 times in the last 30 days. " + EASE_OFFER
    assert c["health_unknown_cause_on"].endswith(EASED_ON)
    assert c["health_not_supported"] == ""
    # events-3: an event log that can't be read is said, while Adaptive Tuning is on, and only then
    assert [n for n in out if out[n]["dom"]["health_warn"]] == ["log_unreadable"]
    assert c["health_unreadable_off"] == ""
    assert c["health_unreadable_cached"] == LOG_UNREADABLE, "a count from before is not shown over it"
    assert c["health_unreadable_no_overclock"] == ""
    assert c["health_readable_unknown"] == "", "only an explicit false is unreadable"
    assert c["health_watch_not_the_cause"].endswith(NOT_THE_CAUSE)


def test_the_line_under_the_lead_follows_the_setting():
    out = _run()["states"]
    for name, r in out.items():
        want = LEVELS_WATCH if name == "watch_mode" else LEVELS
        assert r["levels"] == want, name
        if KIND[name] not in ("load_failed", "unsupported"):
            assert r["dom"]["levels_text"] == want, name
    c = _run()["cases"]
    assert c["levels_failed"] == c["levels_strict"] == LEVELS


def test_upgrade_note():
    out = _run()["states"]
    for name in STATUS:
        want = NOTE_OLD_ON if name == "upgrade_note_old_switch_on" else (NOTE if name == "upgrade_note" else "")
        assert out[name]["note"] == want, name
        assert out[name]["dom"]["note"] == want, name


def _row(out, state, exe):
    return next(r for r in out[state]["rows"] if r["exe"] == exe)


def _labels(row):
    return [a["label"] for a in row["actions"]]


def test_game_rows_chip_hours_last_problem_and_hints():
    out = _run()["states"]
    cod = _row(out, "on_idle", "cod.exe")
    assert cod["chip"] == "Full" and cod["text"] == {"played": "212 hours played", "problem": "", "hints": []}
    assert _row(out, "on_idle", "cs2.exe")["text"]["played"] == "41 hours played"
    assert _row(out, "on_idle", "javaw.exe")["text"]["played"] == "Less than 1 hour played"

    eased = _row(out, "in_game_75", "cod.exe")
    assert eased["chip"] == "75%"
    assert eased["text"]["problem"] == _day(_utc(2026, 11, 8)) + ": " + EASED.format(
        "Call of Duty", "full overclock", "75% of your overclock")
    assert eased["text"]["hints"] == []

    warned = _row(out, "write_refused", "cod.exe")
    assert warned["chip"] == "Full" and warned["text"]["problem"] == _day(_utc(2026, 11, 1)) + ": " + W_ENDED.format("Call of Duty")

    off = _row(out, "game_switched_off", "cs2.exe")
    assert off["chip"] == "Not looked after"
    assert _row(out, "game_switched_off", "cod.exe")["chip"] == "Full"

    steady = _row(out, "eased_game_with_suggestion", "cod.exe")
    assert steady["chip"] == "75%" and steady["text"]["hints"] == [HINT_STEADY]
    assert steady["text"]["problem"].startswith(_day(_utc(2026, 10, 18)) + ": Call of Duty had graphics driver errors twice")
    # the warning still counts: the page adds the promise
    assert _row(out, "eased_game_with_suggestion", "cs2.exe")["text"]["problem"] == (
        _day(_utc(2026, 11, 9)) + ": " + W_SURVIVED.format("Counter-Strike 2") + " " + PROMISE)

    # "not helping" needs no extra hint: the sentence is the last problem
    stuck = _row(out, "not_helping", "cod.exe")
    assert stuck["text"] == {"played": "233 hours played", "hints": [],
                             "problem": _day(_utc(2026, 11, 10)) + ": " + H_NOT_HELPING.format("Call of Duty")}

    none = _row(out, "problem_with_no_overclock", "marvel-win64-shipping.exe")
    assert none["chip"] == "No overclock" and none["text"]["hints"] == []
    assert none["text"]["problem"] == _day(_utc(2026, 11, 12)) + ": " + H_NONE.format("Marvel Rivals")

    assert _row(out, "limit_changed", "cod.exe")["text"]["hints"] == [HINT_LIMIT]
    apex = _row(out, "limit_changed", "r5apex.exe")
    assert apex["chip"] == "50%" and apex["text"]["hints"] == [HINT_LEFT]
    mc = _row(out, "limit_changed", "javaw.exe")
    assert mc["text"]["hints"] == [HINT_PLAIN]
    assert mc["text"]["problem"] == _day(_utc(2026, 11, 5)) + ": " + PLAIN_CRASH.format("Minecraft")

    c = _run()["cases"]
    assert c["chip_no_name"] == "75%"
    assert (c["played_one"], c["played_under_one"], c["played_none"]) == (
        "1 hour played", "Less than 1 hour played", "Less than 1 hour played")
    assert c["row_no_problem_date"]["problem"] == EASED.format("Call of Duty", "full overclock", "75% of your overclock")
    assert c["steady_at_50"]["hints"] == ["Steady for 20 hours at this level. You could try 75% again."]
    assert c["limit_changed_at_no_overclock"]["hints"] == ["Your saved overclock changed since this was eased."]


def test_the_promise_after_a_warning_is_only_there_while_it_is_true():
    """ui-ux-1: "One more within 10 hours of play and it will be eased." only while the
    backend says the warning still counts (warning_open), Adaptive Tuning is on and
    watching, the game's own switch is on and its last problem is that warning; in
    "Only tell me" it says the user will be told."""
    out = _run()["states"]
    W = lambda state, exe: _row(out, state, exe)["text"]["problem"]
    d1, d2, d3, d4, d12 = (_day(NOW - n * 86400) for n in (1, 2, 3, 4, 12))
    assert W("warning_open", "cs2.exe") == d1 + ": " + W_SURVIVED.format("Counter-Strike 2") + " " + PROMISE
    assert W("warning_open", "cod.exe") == d2 + ": " + W_SURVIVED.format("Call of Duty"), "warning_open false"
    assert W("warning_open", "r5apex.exe") == d3 + ": " + W_SURVIVED.format("Apex Legends") + " " + PROMISE
    assert W("warning_open", "fortniteclient-win64-shipping.exe") == d4 + ": " + W_ENDED.format("Fortnite"), "switched off"
    assert W("warning_open", "javaw.exe") == d12 + ": " + W_ENDED.format("Minecraft"), "more than 10 hours of play ago"
    assert W("write_refused", "cod.exe") == _day(_utc(2026, 11, 1)) + ": " + W_ENDED.format("Call of Duty")
    assert W("watch_mode", "cs2.exe") == d1 + ": " + W_SURVIVED.format("Counter-Strike 2") + " " + PROMISE_WATCH
    assert W("watch_mode", "cod.exe") == d2 + ": " + WATCH.format("Call of Duty"), "not after the would-ease line"
    # a warning from the game's own open session: not while it runs (one ease per session, one spell)
    assert W("in_game_warning", "cs2.exe") == _day(NOW - 600) + ": " + W_SURVIVED.format("Counter-Strike 2")
    assert W("in_game_warning", "cod.exe") == d2 + ": " + W_ENDED.format("Call of Duty") + " " + PROMISE
    # Adaptive Tuning off: nothing forward-looking
    assert W("off_with_games", "cs2.exe") == d2 + ": " + W_SURVIVED.format("Counter-Strike 2")
    assert W("off_with_games", "r5apex.exe") == _day(NOW - 9 * 86400) + ": " + USER_SET.format(
        "Apex Legends", "75% of your overclock")
    for name, r in out.items():
        for row, dom in zip(r["rows"], r["dom"]["games"]):
            # the card shows what the function says (the date in front is the real clock's)
            assert dom["problem"].split(": ", 1)[-1] == row["text"]["problem"].split(": ", 1)[-1], (name, row["exe"])
    c = _run()["cases"]
    with_promise = d1 + ": " + W_SURVIVED.format("Counter-Strike 2") + " " + PROMISE
    without = d1 + ": " + W_SURVIVED.format("Counter-Strike 2")
    assert c["promise_balanced"]["problem"] == c["promise_strict"]["problem"] == with_promise
    assert c["promise_watch"]["problem"] == without + " " + PROMISE_WATCH
    for case in ("promise_main_off", "promise_not_watching", "promise_monitor_starting", "promise_no_overclock",
                 "promise_without_state", "promise_closed_warning", "promise_key_missing", "promise_own_session"):
        assert c[case]["problem"] == without, case
    assert c["promise_not_a_warning"]["problem"] == without
    assert c["promise_no_overclock_level"]["problem"] == without
    assert c["promise_session_before_it"]["problem"] == with_promise, "a warning from an earlier session"
    assert c["promise_switched_off"]["problem"] == d4 + ": " + W_ENDED.format("Fortnite")
    apex = d3 + ": " + W_SURVIVED.format("Apex Legends")
    assert c["promise_50"]["problem"] == apex + " " + PROMISE
    assert c["promise_50_with_errors_at_no_overclock"]["problem"] == apex, "50% is held while that is so"
    assert c["promise_closed_by_policy"]["problem"] == d2 + ": " + W_SURVIVED.format("Call of Duty")


def test_adaptive_tuning_off_says_the_levels_are_not_in_force():
    """ui-ux-2: one line above the list, grey chips "75% when on", working game switches."""
    out = _run()["states"]
    d = out["off_with_games"]["dom"]
    assert d["main_off_line"] == MAIN_OFF and out["off_with_games"]["main_off"] == MAIN_OFF
    assert [(g["chip"], g["chip_class"]) for g in d["games"]] == [
        ("50% when on", "at2-chip at2-chip-off"), ("Full when on", "at2-chip at2-chip-off"),
        ("75% when on", "at2-chip at2-chip-off"), ("Not looked after", "at2-chip at2-chip-off")]
    assert [g["switch_on"] for g in d["games"]] == [True, True, True, False]
    for name, r in out.items():
        if KIND[name] in ("load_failed", "unsupported"):
            continue
        off_with_games = not _states()[name]["enabled"] and bool(_states()[name]["games"])
        assert bool(r["dom"]["main_off_line"]) == off_with_games, name
    c = _run()["cases"]
    assert (c["chip_main_off"], c["chip_main_off_full"], c["chip_main_off_switched_off"], c["chip_main_on"]) == (
        "75% when on", "Full when on", "Not looked after", "75%")
    assert c["main_off_no_games"] == c["main_off_on"] == c["main_off_failed"] == ""
    assert c["main_off_apply_false"] == ("Adaptive Tuning is off, so these games run on whatever overclock is on the "
                                         "graphics card. Their levels are kept for when you turn it on again.")
    css = _read("static", "css", "style.css")
    assert ".at2-main-off .at2-game-switch" not in css, "the game switches work while it is off: not drawn as dead"


def test_game_row_buttons():
    out = _run()["states"]
    assert _labels(_row(out, "on_idle", "cod.exe")) == ["Ease one step", "Forget this game"]
    assert _labels(_row(out, "in_game_75", "cod.exe")) == ["Try full overclock", "Ease one step", "Forget this game"]
    # more than one step from full: "Back to full" as well
    assert _labels(_row(out, "limit_changed", "r5apex.exe")) == ["Try 75%", "Back to full", "Ease one step", "Forget this game"]
    # nothing below "No overclock"
    assert _labels(_row(out, "problem_with_no_overclock", "marvel-win64-shipping.exe")) == [
        "Try 50%", "Back to full", "Forget this game"]
    assert [a["to"] for a in _row(out, "limit_changed", "r5apex.exe")["actions"]] == ["faster", "full", "slower", "forget"]
    c = _run()["cases"]
    assert [a["label"] for a in c["actions_full_name"]][0] == "Try full overclock"
    assert [a["label"] for a in c["actions_phrase"]][0] == "Try full overclock"
    assert [a["label"] for a in c["actions_no_name"]][0] == "Try one step up"
    # the backend says it can't go up: no way up is offered, "Back to full" included
    assert [a["label"] for a in c["actions_level_2_no_step_up"]] == ["Ease one step", "Forget this game"]
    assert _row(out, "on_idle", "cod.exe")["forget"] == FORGET_Q.format("Call of Duty")
    # ui-ux-8: a game whose own switch is off: a level set for it would not be used, so only Forget
    assert _labels(_row(out, "game_switched_off", "cs2.exe")) == ["Forget this game"]
    assert _labels(_row(out, "warning_open", "fortniteclient-win64-shipping.exe")) == ["Forget this game"]
    assert [a["label"] for a in c["switched_off_actions"]] == ["Forget this game"]
    assert c["switched_off_hints"]["hints"] == [HINT_LEFT], "no advice that needs a step button"


def test_the_forget_questions_say_what_happens():
    """ui-ux-3 and D1: a running game never gets more overclock before it closes,
    switched-off games are looked after again, and What happened is cleared."""
    c = _run()["cases"]
    assert c["forget_running"] == FORGET_Q.format("Call of Duty") + "\n\n" + RUNNING.format("Call of Duty")
    assert c["forget_released"] == FORGET_Q.format("Call of Duty") + "\n\n" + RUNNING.format("Call of Duty")
    assert c["forget_other_than_running"] == FORGET_Q.format("Counter-Strike 2")
    assert c["forget_running_not_looked_after"] == FORGET_OFF_Q.format("Counter-Strike 2"), "nothing to stop"
    assert c["forget_switched_off"] == FORGET_OFF_Q.format("Fortnite")
    assert c["forget_without_state"] == FORGET_Q.format("Call of Duty")
    assert c["reset_idle"] == c["reset_failed"] == RESET_Q
    assert c["reset_running"] == RESET_Q + "\n\n" + RUNNING.format("Call of Duty")
    f = _run()["flows"]
    assert f["forget_running"]["confirms"] == [c["forget_running"]], "the card asks with the state it shows"
    assert f["forget_running"]["status"] == FORGOTTEN
    assert f["reset_running"]["confirms"] == [c["reset_running"]]


# ── the card as drawn ─────────────────────────────────────────────────
def test_the_card_before_and_without_a_usable_answer():
    out = _run()["states"]
    failed = out["load_failed"]["dom"]
    assert failed["extra"] == [["Retry", "at2Load()"]]
    assert failed["switch_pending"] and failed["switch_dead"] and not failed["switch_on"], "a failed read is not 'off'"
    assert not failed["body"] and failed["health"] == "" and failed["note"] == ""

    # not supported: only the lead and the reason
    unsupported = out["not_supported"]["dom"]
    assert unsupported["switch_disabled"] and unsupported["switch_dead"] and not unsupported["switch_on"]
    assert not unsupported["body"] and not unsupported["levels"] and unsupported["health"] == "" and unsupported["extra"] == []
    lines = [ln.strip() for ln in unsupported["text"].splitlines() if ln.strip()]
    assert lines == ["Adaptive Tuning", LEAD, "Adaptive Tuning", STATUS["not_supported"]], lines

    none = out["no_overclock"]["dom"]
    assert none["extra"] == [["Open the GPU page", "at2OpenQuickTune()"]]
    assert none["body"] and none["levels"] and none["health"] == "" and not none["switch_dead"]
    # ui-ux-4: the button opens the GPU page and points at Quick Tune
    assert _run()["flows"]["open_quick_tune"] == {"switched": ["gpu"], "flash": True}


def test_the_card_draws_the_switch_the_games_and_the_folds():
    out = _run()["states"]
    for name, r in out.items():
        d = r["dom"]
        if KIND[name] in ("load_failed", "unsupported"):
            continue
        state = _states()[name]
        assert d["body"] and d["levels"], name
        assert d["switch_on"] == state["enabled"] and d["aria"] == ("true" if state["enabled"] else "false"), name
        assert not d["switch_dead"] and not d["switch_pending"] and not d["switch_disabled"], name
        assert d["main_off"] == (not state["enabled"]), name
        assert d["busy"] is None, "%s: nothing is being saved" % name
        assert [g["name"] for g in d["games"]] == [g["name"] for g in state["games"]], name
        assert d["empty"] == ("" if state["games"] else NO_GAMES), name
        assert d["history"] == [h["text"] for h in state["history"]], name
        assert d["notify_on"] == state["notify"] and d["sensitivity"] == state["sensitivity"], name
        for g, row, src in zip(d["games"], r["rows"], state["games"]):
            assert g["chip"] == row["chip"] and g["played"] == row["text"]["played"], (name, g["name"])
            assert g["hints"] == row["text"]["hints"], (name, g["name"])
            assert g["buttons"] == [a["label"] for a in row["actions"]], (name, g["name"])
            assert g["switch_on"] == src["enabled"] and g["off"] == (not src["enabled"]), (name, g["name"])
            assert g["tip"] == SWITCH_TIP, (name, g["name"])
            assert g["now"] == bool(state["session"] and state["session"]["exe"] == src["exe"]), (name, g["name"])
            # grey while the game's switch or Adaptive Tuning itself is off: the level is not in force then
            want = ("at2-chip at2-chip-off" if not (src["enabled"] and state["enabled"])
                    else "at2-chip at2-chip-l%d" % src["level"])
            assert g["chip_class"] == want, (name, g["name"], g["chip_class"])
    assert out["off"]["dom"]["empty"] == NO_GAMES
    assert "Nothing has happened yet." in out["off"]["dom"]["text"]
    for heading in ("Your games", "What happened", "Advanced", "Tell me when it changes something", "Ease a game",
                    "Forget everything it learned"):
        assert heading.lower() in out["on_idle"]["dom"]["text"].lower(), heading


def test_nothing_the_card_shows_uses_a_banned_word():
    out = _run()
    for name, r in out["states"].items():
        for text in _strings([r["status"], r["health"], r["note"], r["rows"], r["dom"], r["gpu"]]):
            assert _banned(text) == [], (name, text, _banned(text))
    f = out["flows"]
    for text in _strings([out["cases"], out["consts"], f["library"], f["library_added"], f["library_no_scan"],
                          f["library_known"], f["crash_history"], f["crash_test"]["confirms"],
                          f["crash_test"]["terminal"], f["crash_clear"]["confirms"], f["crash_clear"]["terminal"],
                          f["after_tune"],
                          f["forget_running"], f["reset_running"], f["save_in_flight"], f["save_fails_after_a_read"]]):
        assert _banned(text) == [], (text, _banned(text))


def test_the_gpu_pages_terminal_status_and_crash_lines_use_no_banned_word():
    """K11: what the GPU page writes to its terminal, its Auto Overclock status line and its crash banner."""
    js = _read("static", "js", "app.js")
    lines = js.split("\n")
    found = []
    for i, line in enumerate(lines):
        if not any(k in line for k in ("termWrite('gpu-terminal'", "updateAutoOcStatus(", "_showCrashBanner(")):
            continue
        stmt, j = line, i + 1
        while not stmt.rstrip().endswith(";") and j < len(lines) and j - i < 8:
            stmt += "\n" + lines[j]
            j += 1
        for a, b in re.findall(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", stmt):
            if _banned(a or b):
                found.append((i + 1, a or b))
    assert found == [], found


def test_the_text_in_the_page_and_the_script_uses_no_banned_word():
    cards, gpu = _markup()
    visible = re.sub(r"<!--.*?-->", " ", cards + gpu.split('<div class="bento-grid two">')[0], flags=re.S)
    attributes = re.findall(r'(?:title|aria-label|placeholder)="([^"]*)"', visible)
    visible = re.sub(r"<svg.*?</svg>", " ", visible, flags=re.S)
    for text in [re.sub(r"<[^>]+>", " ", visible)] + attributes:
        assert _banned(text) == [], (text[:200], _banned(text))
    # every quoted text in the two sections of the script this change wrote
    js = _read("static", "js", "app.js")
    section = _between(js, "// ═══ Installed Games (Profiles page) ═══", "async function loadProfileEngineStatus()")
    code = "\n".join(line.split("//")[0] if line.lstrip().startswith("//") else line for line in section.splitlines())
    for text in re.findall(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", code):
        text = text[0] or text[1]
        assert _banned(text) == [], (text, _banned(text))
    assert "\u2014" not in section and "\u2014" not in cards


# ── the page's own text ───────────────────────────────────────────────
def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<svg.*?</svg>|<!--.*?-->", " ", html, flags=re.S))).strip()


def test_the_card_markup_says_what_was_agreed():
    page = _read("templates", "index.html")
    cards, gpu = _markup()
    card = cards.split('<div class="section-divider">Installed Games</div>')[0]
    assert f'<p class="at2-lead" id="at2-lead">{LEAD}</p>' in card
    assert f'<p class="at2-levels" id="at2-levels">{LEVELS}</p>' in card
    assert card.index(LEAD) < card.index(LEVELS) < card.index('id="at2-note"') < card.index('id="at2-toggle-enabled"')
    assert '<div class="toggle-name">Adaptive Tuning</div>' in card
    # a screen reader hears the status line change, and hears it with the switch
    assert '<div class="toggle-desc" id="at2-status" aria-live="polite">' in card
    assert re.search(r'id="at2-toggle-enabled"[^>]*aria-describedby="at2-status"', card)
    assert card.index('id="at2-main-off-line"') < card.index('id="at2-games"')
    assert re.findall(r'<option value="(\w+)">([^<]+)</option>', card) == [
        ("balanced", "At the second graphics driver error within 10 hours of play (recommended)"),
        ("strict", "At the first graphics driver error"),
        ("watch", "Only tell me, change nothing")]
    for text in ("<summary>What happened</summary>", "<summary>Advanced</summary>", "Tell me when it changes something",
                 '<label class="form-label" for="at2-sensitivity">Ease a game</label>', ">Forget everything it learned</button>",
                 ">Your games</div>"):
        assert text in card, text
    # "What happened" and "Advanced" start collapsed
    assert "<details" in card and not re.search(r"<details[^>]*\bopen\b", card)
    ids = re.findall(r'\bid="([^"]+)"', card)
    assert len(ids) > 12 and all(i.startswith("at2-") for i in ids), ids
    assert page.index('id="game-status-card"') < page.index('id="at2-card"') < page.index('id="library-card"') \
        < page.index('id="pauser-card"'), "the card stays where the old one was"
    for gone in ("adaptive-card", "adaptive-games-list", "adaptive-live", "adaptive-diagnostics", "adaptive-history",
                 "toggle-adaptive-global", "Per-Game Auto-Tuning", "Pause AT", "Wipe all learned profiles"):
        assert gone not in page, gone


def test_installed_games_card_markup():
    page = _read("templates", "index.html")
    cards, _ = _markup()
    lib = cards.split('<div class="section-divider">Installed Games</div>')[1]
    assert '<div class="card-title">Installed Games</div>' in lib and LIB_LEAD in lib
    assert "Detected Library" not in page
    assert re.findall(r"<button[^>]*>([^<]+)</button>", lib) == ["Scan now"], "both bulk buttons are gone"
    for gone in ("Enable AT", "Disable AT", "enableATForLibrary", "One click enables Adaptive Tuning"):
        assert gone not in page, gone


def test_gpu_page_and_tournament_mode_text():
    page = _read("templates", "index.html")
    js = _read("static", "js", "app.js")
    _, gpu = _markup()
    assert _text(gpu.split('<div class="bento-grid two">')[0]) == REFUSED
    assert 'id="at2-gpu-note" style="display:none"' in gpu, "hidden until a session is known"
    for button in GPU_BUTTONS:
        assert gpu.count(f'id="{button}"') == 1, button
    assert re.search(r'id="at2-gpu-test-stability" onclick="runStabilityTest\(60\)"', gpu)
    assert re.search(r'id="at2-gpu-test-benchmark" onclick="runBenchmarkComparison\(\)"', gpu)
    # Quick Tune's time is the same everywhere
    assert "fastest path to stable max (about 5 to 8 minutes)." in page
    assert "About 5 to 8 minutes in total" in js and "about 5 to 8 minutes" in STATUS["no_overclock"]
    for old in ("3–5 min", "3-5 min", "~5-8 minutes", "about 8 minutes"):
        assert old not in page and old not in js, old
    # so is Benchmark Tune's (a 90 s step per rung: about 30 minutes near a saved overclock, up to 90 without)
    assert "picks best actual performance (about 30 to 90 minutes)." in page
    assert "Takes about 30 to 90 minutes" in js
    for old in ("~5–7 min", "45-90 minutes", "280-entry", "280 entry"):
        assert old not in page and old not in js, old
    # Quick Tune and Benchmark Tune say where the new overclock is used in games
    after = _run()["flows"]["after_tune"]
    assert after == {"on": "Adaptive Tuning uses it from the next game you start.",
                     "off": "Turn on Adaptive Tuning under Profiles to have it looked after in your games.",
                     "unknown": "Turn on Adaptive Tuning under Profiles to have it looked after in your games."}
    assert js.count("_at2AfterTuneText()") == 3, "defined once, used by both tunes"
    # Tournament Mode no longer promises a tuner mode
    summary = 'One-button "before a match" macro: pauses browsers, kills OneDrive, mutes notifications, switches to High Performance.'
    assert summary + "</div>" in page and "'" + summary + "'" in js
    assert _run()["flows"]["tournament_off"] == summary
    assert "competitive AT" not in page and "competitive AT" not in js


def test_readme():
    readme = re.sub(r"\s+", " ", _read("README.md"))
    assert ("**GPU** (NVIDIA): overclocking with stability testing and auto-tune, Adaptive Tuning (runs games on your saved "
            "overclock and eases it off per game if the graphics driver keeps having problems there), a hard temperature "
            "ceiling, crash recovery, driver updates.") in readme
    assert "- **Monitoring**: live hardware sensors. " in readme
    assert "PresentMon" not in readme and "frame-time" not in readme and "per-game adaptive tuning" not in readme
    assert "\u2014" not in readme


# ── the script: old tuner gone, only the agreed routes ────────────────
def test_the_old_tuner_is_gone_from_the_script():
    js = _read("static", "js", "app.js")
    for gone in ("loadAdaptiveSettings", "toggleAdaptiveGlobal", "_refreshAdaptiveLive", "loadAdaptiveGames", "reseedAdaptiveGame",
                 "toggleAdaptiveGame", "resetAdaptiveGame", "setAdaptiveGameMode", "setAdaptiveGameTarget", "applyAdaptiveBest",
                 "clearAdaptiveBlacklist", "resetAdaptiveAll", "toggleAtKnobs", "setAtKnob", "toggleAtDryRun", "pauseAtManually",
                 "resumeAtManually", "loadCrossGameBlacklist", "clearCrossGameBlacklist", "startAdaptiveDiagnosticsPoll",
                 "_refreshAdaptiveDiagnostics", "recalibrateGameplay", "resetGameplayBaseline", "markCurrentAsMenu",
                 "removeMenuSignature", "installPresentMon", "addThisGameToAt", "loadAdaptiveHistory", "_startAdaptivePoll",
                 "_stopAdaptivePoll", "_quickEnableAt", "toggleATForLibraryGame", "enableATForLibrary", "mode_hint",
                 "/api/library/enable_at", "/api/frame-telemetry", "adaptive-"):
        assert gone not in js, gone
    used = set(re.findall(r"""['"](/api/adaptive/[\w/-]*)['"]""", js))
    assert used == {"/api/adaptive/state", "/api/adaptive/settings", "/api/adaptive/game", "/api/adaptive/game/step",
                    "/api/adaptive/game/forget", "/api/adaptive/reset", "/api/adaptive/note/dismiss"}, used
    for name in ("at2StatusText", "at2HealthText", "at2RowText", "at2ChipText"):
        assert re.search(r"\bfunction %s\(" % name, js), name


def test_the_text_functions_read_only_what_they_are_given():
    js = _read("static", "js", "app.js")
    pure = _between(js, "var AT2_TEST_REFUSED", "// ─── the card ───")
    for word in ("document", "window", "fetch", "apiGet", "apiPost", "_at2.", "localStorage"):
        assert word not in pure, word
    assert pure.count("Date.now()") == 1, "only as the default for 'now'"


# ── switches and buttons ──────────────────────────────────────────────
def test_the_main_switch_moves_at_once_and_goes_back_when_the_save_fails():
    f = _run()["flows"]
    ok = f["switch_on"]
    assert ok["during"] is True, "optimistic: it moves before the answer"
    assert ok["posts"] == [["/api/adaptive/settings", {"enabled": True}]]
    assert ok["after"] is True and ok["status"] == ON and ok["toasts"] == []
    bad = f["switch_fail"]
    assert bad["during"] is True and bad["after"] is False and bad["aria"] == "false", "put back"
    assert bad["status"] == OFF
    assert bad["toasts"] == [["The settings could not be saved.", "Adaptive Tuning"]]
    # an answer that carries no state: read it again
    assert f["switch_answer_without_state"]["calls"] == ["POST /api/adaptive/settings", "GET /api/adaptive/state"]
    assert f["switch_answer_without_state"]["status"] == OFF
    # no supported card, or nothing read yet: the switch does nothing
    assert f["switch_unsupported"] == {"calls": [], "on": False}
    assert f["switch_before_first_read"] == {"calls": []}


def test_while_a_change_is_saved_the_card_says_so_and_a_read_does_not_undo_it():
    """ui-ux-7: the save is out (the engine's lock can be busy for seconds).  A read that
    lands meanwhile is not drawn, other clicks do nothing, and the card shows it is busy."""
    f = _run()["flows"]["save_in_flight"]
    assert f["during"] == {"busy": "true", "saving_class": True, "select_disabled": True, "switch_on": True}
    assert f["after_poll"] == {"switch_on": True, "status": OFF}, "the old answer did not flip the switch back"
    assert f["calls"] == ["POST /api/adaptive/settings", "GET /api/adaptive/state"], "one save, nothing more sent"
    assert f["toasts"] == []
    assert f["after"] == {"switch_on": True, "status": ON, "busy": None, "select_disabled": False}
    # the save failed, and a read was dropped while it was out: read again
    bad = _run()["flows"]["save_fails_after_a_read"]
    assert bad["calls"] == ["POST /api/adaptive/settings", "GET /api/adaptive/state", "GET /api/adaptive/state"]
    assert bad["toasts"] == [["Not saved.", "Adaptive Tuning"]] and bad["status"] == ON and bad["busy"] is None
    css = _read("static", "css", "style.css")
    assert "#at2-card.at2-saving" in css


def test_a_redraw_keeps_the_keyboard_focus():
    f = _run()["flows"]["focus"]
    assert f == {"before": True, "redrawn": True, "key": "switch|cs2.exe"}, f
    assert _run()["flows"]["focus_gone"] == "switch|cod.exe"


def test_a_games_own_switch():
    f = _run()["flows"]
    assert f["game_off"]["during"] is False, "it moves at once"
    assert f["game_off"]["posts"] == [["/api/adaptive/game", {"exe": "cs2.exe", "enabled": False}]]
    assert f["game_off"]["chips"] == ["Full", "Not looked after"]
    assert f["game_off_fail"]["switch_on"] is True, "put back"
    assert f["game_off_fail"]["toasts"] == [["Not saved.", "Adaptive Tuning"]]


def test_row_buttons_call_the_step_and_forget_routes():
    f = _run()["flows"]
    up = f["step_up_confirmed"]
    assert up["confirms"] == ["Call of Duty had a graphics driver error at full overclock on 18 Oct. Try it again?"]
    assert up["posts"] == [["/api/adaptive/game/step", {"exe": "cod.exe", "to": "faster"}],
                           ["/api/adaptive/game/step", {"exe": "cod.exe", "to": "faster", "confirm": True}]]
    assert up["toasts"] == [] and up["chips"][0] == "Full"
    no = f["step_up_declined"]
    assert no["posts"] == [["/api/adaptive/game/step", {"exe": "cod.exe", "to": "faster"}]], "no means nothing is sent again"
    assert no["toasts"] == [] and no["chip"] == "75%"
    assert f["step_up_question_missing"]["confirms"] == [
        "This game had a graphics driver error at that level not long ago. Try it again?"]
    assert f["step_down"]["posts"] == [["/api/adaptive/game/step", {"exe": "cod.exe", "to": "slower"}]]
    assert f["step_down"]["chip"] == "75%" and f["step_down"]["confirms"] == [], "easing is never questioned"
    assert f["step_fail"]["posts"] == [["/api/adaptive/game/step", {"exe": "r5apex.exe", "to": "full"}]]
    assert f["step_fail"]["toasts"] == [["That game is not known.", "Adaptive Tuning"]]
    assert f["forget"]["posts"] == [["/api/adaptive/game/forget", {"exe": "cod.exe"}]]
    assert f["forget"]["confirms"] == [FORGET_Q.format("Call of Duty")]
    assert f["forget_declined"]["calls"] == [] and len(f["forget_declined"]["confirms"]) == 1


def test_advanced_settings_and_the_note():
    f = _run()["flows"]
    assert f["advanced"]["posts"] == [["/api/adaptive/settings", {"notify": False}],
                                      ["/api/adaptive/settings", {"sensitivity": "strict"}]]
    bad = f["advanced_fail"]
    assert bad["sensitivity"] == "balanced" and bad["notify_on"] is True, "both go back to what is saved"
    assert bad["toasts"] == [["Not saved.", "Adaptive Tuning"]] * 2
    assert f["reset"]["posts"] == [["/api/adaptive/reset", {}]]
    assert f["reset"]["confirms"] == [RESET_Q]
    assert f["reset_declined"]["calls"] == []
    assert f["note"] == {"posts": [["/api/adaptive/note/dismiss", {}]], "note": ""}
    assert f["note_fail"]["note"] == NOTE, "not saved as read: it comes back"
    assert f["note_fail"]["toasts"] == [["Not saved.", "Adaptive Tuning"]]


def test_retry_and_a_read_that_fails_later():
    f = _run()["flows"]
    assert f["retry"] == {"calls": ["GET /api/adaptive/state"], "status": STATUS["in_game_75"], "body": True}
    later = f["read_fails_later"]
    assert later["status"] == STATUS["load_failed"] and not later["body"]
    assert later["switch_on"] is True and later["switch_dead"] and not later["switch_pending"], \
        "the switch keeps what it showed, and can't be used"
    # every 5 s while a game or a session is on the card, every 30 s otherwise
    assert f["poll"] == {"idle": 30000, "off": 30000, "session": 5000, "game": 5000, "failed": 30000}


# ── GPU page ──────────────────────────────────────────────────────────
def test_gpu_test_buttons_are_off_while_a_session_is_open():
    out = _run()["states"]
    for name, r in out.items():
        open_session = name != "load_failed" and _states()[name]["session"] is not None
        assert r["gpu"]["disabled"] == [open_session] * 4, name
        assert r["gpu"]["note"] == (REFUSED if open_session else ""), name
        assert r["gpu"]["titles"] == ([REFUSED] * 4 if open_session else [""] * 4), name
    assert _run()["consts"]["refused"] == REFUSED
    f = _run()["flows"]["known_session"]
    assert f["calls"] == [], "nothing is asked of the backend"
    assert f["confirms"] == [], "and the user is not asked to start"
    assert f["toasts"] == [[REFUSED, t] for t in ("Stability test", "Quick Tune", "Benchmark Tune", "Benchmark")]
    assert not (f["tune"]["active"] or f["tune"]["stress"] or f["tune"]["bench"])


def test_the_session_check_turns_on_only_the_buttons_it_turned_off():
    """ui-ux-9: on a card Vispora can't overclock, Quick Tune stays off after the session ends."""
    g = _run()["flows"]["gpu_unsupported"]
    # Quick Tune was already off: the session leaves it, and its title, alone
    assert g["session"]["disabled"] == [True] * 4 and g["session"]["titles"] == [REFUSED, REFUSED, "", REFUSED]
    assert g["after"]["disabled"] == [False, False, True, False] and g["after"]["titles"] == [""] * 4
    # turned off by the session, then the capability check said no: it stays off when the session ends
    assert g["marked_session"]["disabled"] == [True] * 4 and g["marked_session"]["titles"] == [REFUSED] * 4
    assert g["marked_after"]["disabled"] == [False, False, True, False] and g["marked_after"]["titles"] == [""] * 4


def test_a_refused_test_start_stops_shows_why_and_never_cancels():
    """A cancel takes the overclock off the card, and a refusal means a game is
    using it: after a refused start nothing else may be sent."""
    f = _run()["flows"]
    stab = f["refused_stability"]
    assert stab["calls"] == ["GET /api/gpu/oc/live", "POST /api/gpu/oc/probe/start", "GET /api/adaptive/state"]
    assert stab["toasts"] == [[REFUSED, "Stability test"]]
    assert stab["tune"]["stress"] is False and stab["tune"]["stress_card"] == "none"
    assert REFUSED in stab["tune"]["terminal"]
    assert stab["gpu"]["disabled"] == [True] * 4, "the buttons learn about the session"

    quick = f["refused_quick"]
    assert quick["calls"] == ["POST /api/gpu/oc/auto/start", "GET /api/adaptive/state"], quick["calls"]
    assert quick["toasts"] == [[REFUSED, "Quick Tune"]]
    assert quick["tune"]["active"] is False and quick["tune"]["start_btn"] == "" and quick["tune"]["cancel_btn"] == "none"
    assert REFUSED in quick["tune"]["status"] and REFUSED in quick["tune"]["terminal"]

    tune = f["refused_bench_tune"]
    assert tune["calls"] == ["POST /api/gpu/oc/benchmark-tune/start", "GET /api/adaptive/state"], tune["calls"]
    assert tune["toasts"] == [[REFUSED, "Benchmark Tune"]]
    assert tune["tune"]["active"] is False and REFUSED in tune["tune"]["status"]

    bench = f["refused_benchmark"]
    assert bench["calls"] == ["POST /api/gpu/oc/benchmark/start", "GET /api/adaptive/state"], bench["calls"]
    assert REFUSED in bench["tune"]["bench_text"] and bench["tune"]["bench"] is False

    for flow in (stab, quick, tune, bench):
        assert not [c for c in flow["calls"] if "cancel" in c or "/next" in c or "probe/end" in c or "/apply" in c], flow["calls"]


def test_a_start_that_fails_for_another_reason_stops_and_shows_err():
    f = _run()["flows"]
    quick = f["failed_quick"]
    # not a refusal: the run may have opened after all, so it is closed (as before), but never driven
    assert quick["calls"] == ["POST /api/gpu/oc/auto/start", "POST /api/gpu/oc/auto/cancel"], quick["calls"]
    assert quick["toasts"] == [["Request timed out", "Quick Tune"]]
    assert quick["tune"]["active"] is False and "Request timed out" in quick["tune"]["status"]
    stab = f["failed_stability"]
    assert stab["calls"] == ["GET /api/gpu/oc/live", "POST /api/gpu/oc/probe/start", "POST /api/gpu/oc/probe/end"]
    assert stab["toasts"] == [["Request timed out", "Stability test"]] and stab["tune"]["stress"] is False
    step = f["quick_step_fails"]
    assert step["calls"] == ["POST /api/gpu/oc/auto/start", "POST /api/gpu/oc/auto/next", "GET /api/gpu/oc/live",
                             "POST /api/gpu/oc/probe/start", "POST /api/gpu/oc/auto/cancel"], step["calls"]
    assert step["toasts"] == [["The check could not start.", "Quick Tune"]]
    assert step["tune"]["active"] is False and step["tune"]["start_btn"] == ""


def test_the_crash_recovery_card_says_what_it_still_does():
    """It only acts for a game Adaptive Tuning was not looking after, looks
    back two minutes, and never changes the saved overclock."""
    page = _read("templates", "index.html")
    card = _text(_between(page, '<div class="card" id="crash-recovery-card">', '<div class="card" id="oc-live-card">'))
    lead = card.split("Total recorded crashes")[0].strip()
    assert lead == ("Crash Recovery Works for games Adaptive Tuning is not looking after. When one of them closes, "
                    "Vispora checks the Windows event log for a graphics driver error in the two minutes before. If "
                    "there was one while the card held more than your saved overclock, the clocks are lowered for now "
                    "and those values are remembered for that game. Your saved overclock is never changed. A "
                    "notification tells you what happened."), lead
    assert _banned(lead) == [], _banned(lead)
    for old in ("last 10 seconds", "auto-steps-down", "A toast fires"):
        assert old not in page, old
    # K11: the whole card, its buttons and what its script writes, in plain words
    assert _banned(card) == [], _banned(card)
    assert re.findall(r"<button[^>]*>([^<]+)</button>", _crash_card()) == [
        "Refresh", "Run a test check", "Clear history", "Clear remembered clocks"]
    f = _run()["flows"]
    history = f["crash_history"]
    assert "graphics driver error" in history and "clocks remembered" in history and "seen by a Vispora test" in history
    assert "Graphics driver errors: 2 · driver crashes: 1" in history
    assert _banned(history) == [] and "blacklist" not in history.lower()
    test = f["crash_test"]
    assert len(test["confirms"]) == 2 and test["confirms"][0].startswith("Test Crash Recovery?\n\n")
    assert "no clock is changed and nothing is remembered for any game" in test["confirms"][0]
    assert [p[0] for p in test["posts"]] == ["/api/gpu/crash-recovery/test"] * 2
    assert ("no graphics driver error in the last two minutes, so a game closing now would count as a normal exit"
            in test["terminal"])
    assert ("the event log has a graphics driver error from the last two minutes, so a game closing now would count "
            "as a crash" in test["terminal"])
    clear = f["crash_clear"]
    assert clear["confirms"] == [
        "Clear the crash history?\n\nThe clocks remembered for each game are kept. Clear remembered clocks removes those.",
        "Forget the clocks Crash Recovery remembered for every game?\n\nIt stops avoiding clocks that crashed a game before."]
    assert "Remembered crash clocks cleared." in clear["terminal"]
    for text in _strings([history, test["confirms"], test["terminal"], test["card"], clear["confirms"], clear["terminal"]]):
        assert _banned(text) == [], text


# ── Installed Games and the dashboard pointer ─────────────────────────
def test_installed_games_card():
    f = _run()["flows"]
    lib = f["library"]
    assert lib["summary"].startswith("4 games installed · 1 recognised · last scan ")
    assert [(r["title"], r["button"], r["state"]) for r in lib["rows"]] == [
        ("Counter-Strike 2", "", "Recognised"),                         # recognised: no button
        ("Marvel's Spider-Man 2", "Add to my games", ""),
        ("Call of Duty: Black Ops 6", "Add to my games", ""),
        ("Mystery Game", "", "No program found")]                       # nothing to add without a program
    assert lib["rows"][1]["sub"] == "steam · spider-man2.exe"
    assert lib["all_active"] and not lib["known_active"], "the games to add are shown from the start"
    assert LIB_LEAD in lib["text"] and "mode" not in lib["rows"][0]["title"].lower()

    # K10: the cleaned name ("display_name") is shown and sent, not the launcher's title with its trademark sign
    added = f["library_added"]
    assert added["posts"] == [["/api/profiles/add-game", {"exe": "spider-man2.exe", "name": "Marvel's Spider-Man 2"}]]
    assert added["toasts"] == []
    assert [(r["button"], r["state"]) for r in added["lib"]["rows"]] == [
        ("", "Recognised"), ("", "In my games"), ("Add to my games", ""), ("", "No program found")], "drawn again"
    assert added["lib"]["summary"].startswith("4 games installed · 2 recognised")

    assert f["library_add_fail"] == {"toasts": [["Not saved.", "Installed Games"]], "button": "Add to my games"}
    assert [r["title"] for r in f["library_known"]["rows"]] == ["Counter-Strike 2", "Marvel's Spider-Man 2"]
    assert f["library_known"]["known_active"] and not f["library_known"]["all_active"]
    assert f["library_no_scan"]["rows"] == [] and f["library_no_scan"]["summary"].startswith("No scan yet.")
    # a game added earlier shows as added after a fresh load, without a new scan
    loaded = f["library_loaded"]
    assert sorted(loaded["calls"]) == ["GET /api/library", "GET /api/profiles"], "the old tuner's list is not asked for"
    assert [(r["button"], r["state"]) for r in loaded["lib"]["rows"]] == [("", "In my games")]


def test_a_navigate_fix_on_the_dashboard_is_a_button_to_that_page():
    row = _run()["flows"]["health_row"]
    assert 'onclick="switchPage(&quot;profiles&quot;)"' in row
    assert ">Open Profiles</button>" in row, "named as the sidebar names it, without the badge"
