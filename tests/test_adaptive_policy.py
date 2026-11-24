"""Adaptive Tuning's rules (core/adaptive_policy.py): the clock table (1669 and
1113, negative parts, the overclock a game was eased against), every decide
rule as a table row, what apply and step do to a game record, the row hints
(warning_open, eased_by_driver), every sentence word for word and free of the
banned words, and the replays: the owner's real log (three graphics driver
errors that each closed the game, 11.8 and 16.6 hours of play apart), an
overclock that really is unstable, one with a long record at full, and the
owner's 30 September spell of the driver (one spell, not two pieces of evidence)."""
import sandbox as m

import ast
import calendar
import copy
import inspect
import itertools
import json
import os
import random
import re
import unittest
from decimal import ROUND_HALF_UP, Decimal

P = m.app("core.adaptive_policy")

NAME = "Call of Duty"
EXE = "cod.exe"
T0 = float(calendar.timegm((2026, 10, 25, 13, 10, 41)))        # the first error in the owner's log
LIMIT = {"exists": True, "core_offset_mhz": 450, "mem_offset_mhz": 2225, "power_pct": 110, "apply_on_startup": False}
INCIDENT = {"first_ts": T0 + 50.0, "last_ts": T0 + 80.0, "count": 3}

RECORD_KEYS = {"exe", "name", "enabled", "level", "sessions", "minutes", "minutes_at_level", "last_played",
               "incidents", "changes", "plain_crashes", "not_helping", "problem_with_no_overclock",
               "limit_at_level", "last_problem"}
ROW_KEYS = {"exe", "name", "enabled", "level", "level_name", "percent", "core", "mem", "sessions", "hours",
            "hours_at_level", "last_played", "last_problem", "not_helping", "problem_with_no_overclock",
            "limit_changed", "can_step_up", "next_up_name", "can_step_down", "suggest_step_up", "best_left_here",
            "plain_crash_hint", "warning_open", "eased_by_driver"}
DECISION_KEYS = {"action", "to_level", "history", "toast", "problem"}
BANNED = ("offset", "tdr", "blacklist", "baseline", "yielded", "stock", "strike")
EM_DASH = chr(0x2014)


# ── builders ──────────────────────────────────────────────────────────
def G(level=0, minutes=1000.0, at_level=None, incidents=(), changes=(), **more):
    """A game record at `level` that has played `minutes`, all of it in the record."""
    g = P.new_game(EXE, NAME, T0)
    g.update(level=level, minutes=minutes, minutes_at_level=minutes if at_level is None else at_level,
             incidents=[dict(i) for i in incidents], changes=[dict(c) for c in changes])
    g.update(more)
    return g


def C(**over):
    ctx = {"sensitivity": "balanced", "ended_game": False, "play_minutes_now": 1000.0,
           "stepped_this_session": False, "since_last_change_s": None, "other_game_running": False,
           "errors_with_no_overclock": False, "limit": LIMIT}
    ctx.update(over)
    return ctx


def chg(frm, to, by="driver", hours=1.0, ts=T0 - 9000.0):
    return {"ts": ts, "from_level": frm, "to_level": to, "by": by, "hours_at_from_level": hours, "text": "x"}


def inc(play_minutes, level=0, ended=False, ts=T0 - 5000.0):
    return {"ts": ts, "play_minutes": play_minutes, "level": level, "ended_game": ended}


def bad_words(text):
    low = text.lower()
    found = [w for w in BANNED if w in low]
    if re.search(r"\bAT\b", text):
        found.append("AT")
    if EM_DASH in text:
        found.append("em dash")
    return found


# ── levels and clocks ─────────────────────────────────────────────────
def test_levels_names_and_phrases():
    assert P.LEVELS == (1.0, 0.75, 0.5, 0.0)
    assert P.LEVEL_NAMES == ("Full", "75%", "50%", "No overclock")
    assert [P.level_phrase(i) for i in range(4)] == [
        "full overclock", "75% of your overclock", "50% of your overclock", "no overclock"]
    assert [P.level_name(i) for i in range(4)] == list(P.LEVEL_NAMES)
    assert [P.level_percent(i) for i in range(4)] == [100, 75, 50, 0] == [int(x * 100) for x in P.LEVELS]
    assert P.TOAST_TITLE == "Adaptive Tuning"
    # A level nobody can read is Full, the overclock the user saved; one past the end is the last one.
    assert P.level_phrase(None) == P.level_phrase("x") == P.level_phrase(-2) == "full overclock"
    assert P.level_phrase(9) == "no overclock"


OFFSETS = [
    # label, limit, level, limit_at_level, expected
    ("full", (450, 2225), 0, None, (450, 2225)),
    ("75%: 337.5 and 1668.75 round half up", (450, 2225), 1, None, (338, 1669)),
    ("50%: 1112.5 rounds half up", (450, 2225), 2, None, (225, 1113)),
    ("no overclock", (450, 2225), 3, None, (0, 0)),
    ("negative core at full", (-50, 2225), 0, None, (-50, 2225)),
    ("negative core at 75%", (-50, 2225), 1, None, (-50, 1669)),
    ("negative core at 50%", (-50, 2225), 2, None, (-50, 1113)),
    ("negative core with no overclock", (-50, 2225), 3, None, (-50, 0)),
    ("negative memory at 50%", (450, -200), 2, None, (225, -200)),
    ("both negative with no overclock", (-30, -40), 3, None, (-30, -40)),
    ("nothing saved", (0, 0), 1, None, (0, 0)),
    ("zero core", (0, 2225), 1, None, (0, 1669)),
    ("tiny values at 75%: 0.75 and 1.5", (1, 2), 1, None, (1, 2)),
    ("tiny values at 50%: 0.5 and 1.5", (1, 3), 2, None, (1, 2)),
    # the overclock a game was eased against
    ("raised since: 75% is not lifted", (500, 2500), 1, [450, 2225], (338, 1669)),
    ("raised since: 50% is not lifted", (500, 2500), 2, [450, 2225], (225, 1113)),
    ("raised since: no overclock stays none", (500, 2500), 3, [450, 2225], (0, 0)),
    ("at full the remembered value is not used", (500, 2500), 0, [450, 2225], (500, 2500)),
    ("lowered since: follows the lower value", (400, 2000), 1, [450, 2225], (300, 1500)),
    ("one part raised, one lowered", (500, 2000), 1, [450, 2225], (338, 1500)),
    ("a part that had no overclock when eased stays off", (200, 2225), 1, [0, 2225], (0, 1669)),
    ("a part that was negative when eased stays there", (200, 2225), 1, [-50, 2225], (-50, 1669)),
    ("a part saved negative now is applied unchanged", (-100, 2225), 1, [450, 2225], (-100, 1669)),
    ("unreadable remembered value is ignored", (450, 2225), 1, "x", (338, 1669)),
    ("short remembered value is ignored", (450, 2225), 1, [450], (338, 1669)),
    # the shapes a limit arrives in
    ("saved profile dict", LIMIT, 1, None, (338, 1669)),
    ("get_state limit dict", {"has_profile": True, "core": 450, "mem": 2225, "power": 110}, 2, None, (225, 1113)),
    ("list with power", [450, 2225, 110], 1, None, (338, 1669)),
    ("no profile", {"exists": False}, 0, None, (0, 0)),
    ("None", None, 1, None, (0, 0)),
    ("fractions are read down, never up", (450.9, 2225.9), 0, None, (450, 2225)),
    ("unreadable level is full", (450, 2225), None, None, (450, 2225)),
    ("level past the end is no overclock", (450, 2225), 7, None, (0, 0)),
]


def test_offsets_table():
    for label, limit, level, at_level, want in OFFSETS:
        got = P.offsets_for(limit, level, at_level)
        assert got == want, f"{label}: {got} != {want}"
        assert all(type(v) is int for v in got), label
    assert P.offsets_for((450, 2225), 1) == (338, 1669)            # the third argument is optional


def test_offsets_round_half_up_and_never_above_the_saved_value():
    for level, pct in enumerate((100, 75, 50, 0)):
        for v in range(0, 3001):
            want = int((Decimal(v) * pct / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            core, mem = P.offsets_for((v, v), level)
            assert core == mem == want, (level, v, core, want)
            assert 0 <= core <= v
        for v in (-1, -25, -500):
            assert P.offsets_for((v, v), level) == (v, v)
    # lower level, lower or equal clock; with a remembered value it is the smaller of the two scalings
    for v in range(0, 3001, 7):
        steps = [P.offsets_for((v, v), level)[0] for level in range(4)]
        assert steps == sorted(steps, reverse=True), (v, steps)
    for v, old, level in itertools.product((0, 1, 199, 450, 2225, 3000), (-50, 0, 1, 300, 450, 2500), (1, 2, 3)):
        got = P.offsets_for((v, v), level, [old, old])[0]
        want = min(P.offsets_for((v, v), level)[0], P.offsets_for((old, old), level)[0]) if v > 0 else v
        assert got == want, (v, old, level, got, want)
        assert got <= max(v, 0)


# ── the record ────────────────────────────────────────────────────────
def test_new_game_record():
    g = P.new_game(EXE, NAME, T0)
    assert g == {
        "exe": "cod.exe", "name": "Call of Duty", "enabled": True, "level": 0, "sessions": 0, "minutes": 0.0,
        "minutes_at_level": 0.0, "last_played": T0, "incidents": [], "changes": [], "plain_crashes": [],
        "not_helping": False, "problem_with_no_overclock": False, "limit_at_level": None, "last_problem": None}
    assert set(g) == RECORD_KEYS
    assert P.new_game(EXE, None, T0)["name"] == EXE              # no display name: the exe, never an empty name
    other = P.new_game("b.exe", "B", T0)
    assert other["incidents"] is not g["incidents"] and other["changes"] is not g["changes"]
    assert json.loads(json.dumps(g)) == g


def test_add_play_adds_the_same_time_to_both_counters():
    g = G(1, minutes=100.0, at_level=40.0)
    P.add_play(g, 30.0, T0 + 9.0, sessions=1)
    assert (g["minutes"], g["minutes_at_level"], g["sessions"], g["last_played"]) == (130.0, 70.0, 1, T0 + 9.0)
    P.add_play(g, 5.0)
    assert (g["minutes"], g["minutes_at_level"], g["sessions"], g["last_played"]) == (135.0, 75.0, 1, T0 + 9.0)
    P.add_play(g, -20.0)                                          # time never runs backwards
    P.add_play(g, "x")
    assert (g["minutes"], g["minutes_at_level"]) == (135.0, 75.0)
    P.add_play(g, 0.0, sessions=True)
    P.add_play(g, 0.0, sessions=-3)
    assert g["sessions"] == 2


# ── bursts ────────────────────────────────────────────────────────────
def test_merge_bursts():
    def one(first, last, count):
        return {"first_ts": first, "last_ts": last, "count": count}

    assert P.merge_bursts([]) == [] and P.merge_bursts(None) == []
    assert P.merge_bursts([{"ts": 100.0}]) == [one(100.0, 100.0, 1)]
    assert P.merge_bursts([{"ts": 0.0}, {"ts": 299.0}]) == [one(0.0, 299.0, 2)]
    assert P.merge_bursts([{"ts": 0.0}, {"ts": 300.0}]) == [one(0.0, 0.0, 1), one(300.0, 300.0, 1)]
    # each event is measured from the previous one, so a slow drip is still one incident
    assert P.merge_bursts([{"ts": t} for t in (0.0, 240.0, 480.0, 720.0)]) == [one(0.0, 720.0, 4)]
    # order of arrival does not matter, and bare times work
    assert P.merge_bursts([900.0, {"ts": 10.0}, 0.0]) == [one(0.0, 10.0, 2), one(900.0, 900.0, 1)]
    # the same log record twice is one event; the same record id on another channel is another event
    twice = [{"ts": 5.0, "channel": "System", "record_id": 7}, {"ts": 5.0, "channel": "System", "record_id": 7},
             {"ts": 6.0, "channel": "Application", "record_id": 7}]
    assert P.merge_bursts(twice) == [one(5.0, 6.0, 2)]
    # incidents still pending can be merged with what the next poll finds
    pending = one(0.0, 100.0, 2)
    assert P.merge_bursts([pending, {"ts": 350.0}]) == [one(0.0, 350.0, 3)]
    assert P.merge_bursts([pending, {"ts": 350.0}, {"ts": 700.0}]) == [one(0.0, 350.0, 3), one(700.0, 700.0, 1)]
    assert pending == one(0.0, 100.0, 2)                          # the input is left alone
    # things that are not events are skipped
    assert P.merge_bursts([None, {}, {"ts": "x"}, "x", {"ts": 1.0}]) == [one(1.0, 1.0, 1)]
    # the owner's 30 September storm: 09:04, 09:15 to 09:17, 09:25, 09:44 are four incidents
    storm = [{"ts": minute * 60.0} for minute in (4, 15, 16, 17, 25, 44)]
    assert [(b["first_ts"] / 60, b["last_ts"] / 60, b["count"]) for b in P.merge_bursts(storm)] == [
        (4, 4, 1), (15, 17, 3), (25, 25, 1), (44, 44, 1)]


# ── decide ────────────────────────────────────────────────────────────
N = NAME
RULES = [
    # label, game, ctx, rule, action, to_level, history, toast, counted, flag

    # 1. less than 10 minutes after this session's own level change
    ("1: 9 min 59 s after the change", G(1, changes=[chg(0, 1)]), C(sensitivity="strict", since_last_change_s=599.0),
     1, "ignore", 1, "", None, False, None),
    ("1: wins over 2 and 3", G(1, changes=[chg(0, 1)]),
     C(since_last_change_s=0.0, other_game_running=True, stepped_this_session=True),
     1, "ignore", 1, "", None, False, None),
    ("1: an error dated just before the change is part of the same storm", G(1, changes=[chg(0, 1)]),
     C(sensitivity="strict", since_last_change_s=-30.0), 1, "ignore", 1, "", None, False, None),
    ("1: exactly 10 minutes is past it", G(0), C(sensitivity="strict", since_last_change_s=600.0),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),

    # 2. another game is running too
    ("2: another game running, strict", G(0), C(sensitivity="strict", other_game_running=True),
     2, "warning", 0, P.W_OTHER(N), None, False, None),
    ("2: another game running, a pair on record", G(0, incidents=[inc(900.0)]), C(other_game_running=True),
     2, "warning", 0, P.W_OTHER(N), None, False, None),
    ("2: wins over 3", G(1, changes=[chg(0, 1)]), C(other_game_running=True, stepped_this_session=True),
     2, "warning", 1, P.W_OTHER(N), None, False, None),

    # 3. already stepped this session
    ("3: second error in the session that eased it, strict", G(1, changes=[chg(0, 1)]),
     C(sensitivity="strict", stepped_this_session=True), 3, "warning", 1, P.W_ALREADY(N), None, True, None),
    ("3: no second step even with a pair on record", G(1, changes=[chg(0, 1)], incidents=[inc(900.0, level=1)]),
     C(stepped_this_session=True), 3, "warning", 1, P.W_ALREADY(N), None, True, None),

    # 4. does it qualify?
    ("4: balanced, first error, game kept running", G(0), C(), 4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, first error, game closed", G(0), C(ended_game=True),
     4, "warning", 0, P.W_ENDED(N), None, True, None),
    ("4: balanced, a counted error exactly 10 play hours before", G(0, incidents=[inc(400.0)]), C(),
     9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, the earlier one is half a minute too old", G(0, incidents=[inc(399.5)]), C(),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, the earlier one was at another level", G(1, changes=[chg(0, 1, hours=0.1)],
                                                         incidents=[inc(900.0, level=0)]), C(),
     4, "warning", 1, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, the earlier one is dated after this one", G(0, incidents=[inc(1200.0)]), C(),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, the user moved it up 5 play hours ago: no free warning",
     G(0, at_level=300.0, changes=[chg(1, 0, by="user")]), C(),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, the user moved it up exactly 10 play hours ago",
     G(1, at_level=600.0, changes=[chg(3, 1, by="user")]), C(),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("4: balanced, the user moved it up longer ago", G(0, at_level=600.5, changes=[chg(1, 0, by="user")]), C(),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, a move up that is not the user's (damaged record) is no reason to ease",
     G(0, at_level=300.0, changes=[chg(1, 0, by="driver")]), C(),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, moved up 500 recorded minutes ago plus 101 in this session",
     G(0, at_level=500.0, changes=[chg(1, 0, by="user")]), C(play_minutes_now=1101.0),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, the user moved it DOWN recently: a warning first",
     G(1, at_level=30.0, changes=[chg(0, 1, by="user", hours=0.1)]), C(),
     4, "warning", 1, P.W_SURVIVED(N), None, True, None),
    ("4: balanced, eased by the driver recently: the new level gets a warning first",
     G(1, at_level=30.0, changes=[chg(0, 1, hours=0.1)]), C(ended_game=True),
     4, "warning", 1, P.W_ENDED(N), None, True, None),
    ("4: strict, first error", G(0), C(sensitivity="strict"),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: watch, first error is a warning like balanced", G(0), C(sensitivity="watch"),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    ("4: an unknown setting is read as balanced", G(0), C(sensitivity="loud"),
     4, "warning", 0, P.W_SURVIVED(N), None, True, None),
    # one spell of the driver: an error the game survived pairs only with one SPELL_MIN (60) play minutes later
    ("4: balanced, a survived error 59 play minutes before is the same spell", G(0, incidents=[inc(941.0)]), C(),
     4, "warning", 0, P.W_SAME_SPELL(N), None, True, None),
    ("4: balanced, a survived error 59.9 play minutes before is still the same spell",
     G(0, incidents=[inc(940.1)]), C(), 4, "warning", 0, P.W_SAME_SPELL(N), None, True, None),
    ("4: balanced, a survived error exactly 60 play minutes before pairs", G(0, incidents=[inc(940.0)]), C(),
     9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, the same spell even when this one closed the game", G(0, incidents=[inc(990.0)]),
     C(ended_game=True), 4, "warning", 0, P.W_SAME_SPELL(N), None, True, None),
    ("4: balanced, an error that closed the game pairs at any distance: crash, relaunch, error again",
     G(0, incidents=[inc(995.0, ended=True)]), C(), 9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, an error that closed the game pairs even in the same play minute",
     G(0, incidents=[inc(1000.0, ended=True)]), C(), 9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, a spell error and an older one that pairs: eased", G(0, incidents=[inc(900.0), inc(990.0)]), C(),
     9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, an unreadable 'closed the game' is read as survived", G(0, incidents=[
        dict(inc(990.0), ended_game="yes")]), C(), 4, "warning", 0, P.W_SAME_SPELL(N), None, True, None),
    ("4: balanced, a survived error at another level is no spell", G(1, changes=[chg(0, 1, hours=0.1)],
                                                                    incidents=[inc(990.0, level=0)]), C(),
     4, "warning", 1, P.W_SURVIVED(N), None, True, None),
    ("4: watch, the same spell is told without a toast", G(0, incidents=[inc(990.0)]), C(sensitivity="watch"),
     4, "warning", 0, P.W_SAME_SPELL(N), None, True, None),
    ("4: strict does not ask about spells", G(0, incidents=[inc(990.0)]), C(sensitivity="strict"),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("4: balanced, a retried level gets no free warning inside a spell either",
     G(0, at_level=300.0, changes=[chg(1, 0, by="user")], incidents=[inc(990.0)]), C(),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),

    # 5. qualified with no overclock left to ease
    ("5: strict at no overclock", G(3, changes=[chg(2, 3)]), C(sensitivity="strict"),
     5, "hold", 3, P.H_NONE(N), None, True, "problem_with_no_overclock"),
    ("5: balanced pair at no overclock", G(3, changes=[chg(2, 3)], incidents=[inc(900.0, level=3)]), C(),
     5, "hold", 3, P.H_NONE(N), None, True, "problem_with_no_overclock"),
    ("5: balanced first error at no overclock is still a warning", G(3, changes=[chg(2, 3)]), C(),
     4, "warning", 3, P.W_SURVIVED(N), None, True, None),
    ("5: wins over 6", G(3, at_level=30.0, changes=[chg(2, 3, hours=12.0)]), C(sensitivity="strict"),
     5, "hold", 3, P.H_NONE(N), None, True, "problem_with_no_overclock"),
    ("5: wins over 8", G(3, changes=[chg(2, 3)], incidents=[inc(900.0, level=3)]), C(sensitivity="watch"),
     5, "hold", 3, P.H_NONE(N), None, True, "problem_with_no_overclock"),

    # 6. fewer play hours at this level than half the hours at the one above before it was eased, those counted
    #    at most 10 (so the hold lasts at most the first 5 hours of play at the eased level)
    ("6: 4 h at 75% after 12 h at full (counted as 10, half is 5)",
     G(1, at_level=240.0, changes=[chg(0, 1, hours=12.0)]),
     C(sensitivity="strict"), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: 4 h 59 min at 75% after 12 h at full", G(1, at_level=299.0, changes=[chg(0, 1, hours=12.0)]),
     C(sensitivity="strict"), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: exactly half of the counted hours is not fewer", G(1, at_level=300.0, changes=[chg(0, 1, hours=12.0)]),
     C(sensitivity="strict"), 9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("6: a long record above counts as 10 hours: 200 h at full, 5 h at 75% is enough",
     G(1, at_level=300.0, changes=[chg(0, 1, hours=200.0)]), C(sensitivity="strict"),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("6: a short time above: half of 3 h", G(1, at_level=89.0, changes=[chg(0, 1, hours=3.0)]),
     C(sensitivity="strict"), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: a short time above: 1.5 h at 75% after 3 h at full is enough",
     G(1, at_level=90.0, changes=[chg(0, 1, hours=3.0)]), C(sensitivity="strict"),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("6: also after the user eased it by hand", G(1, at_level=240.0, changes=[chg(0, 1, by="user", hours=12.0)]),
     C(sensitivity="strict"), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: not when the last change moved it UP to this level",
     G(1, at_level=240.0, changes=[chg(2, 1, by="user", hours=12.0)]), C(sensitivity="strict"),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("6: counts the open session, still fewer", G(1, at_level=30.0, changes=[chg(0, 1, hours=2.0)]),
     C(sensitivity="strict", play_minutes_now=1020.0), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: counts the open session, enough", G(1, at_level=30.0, changes=[chg(0, 1, hours=2.0)]),
     C(sensitivity="strict", play_minutes_now=1030.0),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("6: wins over 7", G(2, at_level=240.0, changes=[chg(1, 2, hours=12.0)]),
     C(sensitivity="strict", errors_with_no_overclock=True),
     6, "hold", 2, P.H_NOT_HELPING(N), None, True, "not_helping"),
    ("6: wins over 8", G(1, at_level=240.0, changes=[chg(0, 1, hours=12.0)], incidents=[inc(900.0, level=1)]),
     C(sensitivity="watch"), 6, "hold", 1, P.H_NOT_HELPING(N), None, True, "not_helping"),

    # 7. at 50% while the driver also logs errors with no overclock on the card
    ("7: strict at 50%", G(2, changes=[chg(1, 2)]), C(sensitivity="strict", errors_with_no_overclock=True),
     7, "hold", 2, P.H_FLOOR(N), None, True, None),
    ("7: balanced pair at 50%", G(2, changes=[chg(1, 2)], incidents=[inc(900.0, level=2)]),
     C(errors_with_no_overclock=True), 7, "hold", 2, P.H_FLOOR(N), None, True, None),
    ("7: only at 50%: 75% still goes down", G(1, changes=[chg(0, 1)]),
     C(sensitivity="strict", errors_with_no_overclock=True),
     9, "ease", 2, P.EASED_STRICT(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("7: without those errors 50% goes to no overclock", G(2, changes=[chg(1, 2)]), C(sensitivity="strict"),
     9, "ease", 3, P.EASED_STRICT(N, 2, 3), P.T_EASED(N, 3), False, None),
    ("7: wins over 8", G(2, changes=[chg(1, 2)], incidents=[inc(900.0, level=2)]),
     C(sensitivity="watch", errors_with_no_overclock=True), 7, "hold", 2, P.H_FLOOR(N), None, True, None),

    # 8. only tell
    ("8: watch, pair at full", G(0, incidents=[inc(900.0)]), C(sensitivity="watch"),
     8, "would_ease", 0, P.WATCH(N, 1), P.WATCH(N, 1), True, None),
    ("8: watch, pair at 50%", G(2, changes=[chg(1, 2)], incidents=[inc(900.0, level=2)]), C(sensitivity="watch"),
     8, "would_ease", 2, P.WATCH(N, 3), P.WATCH(N, 3), True, None),

    # 9. one step down
    ("9: balanced pair at full", G(0, incidents=[inc(900.0)]), C(ended_game=True),
     9, "ease", 1, P.EASED(N, 0, 1), P.T_EASED(N, 1), False, None),
    ("9: balanced pair at 75%", G(1, changes=[chg(0, 1)], incidents=[inc(900.0, level=1)]), C(),
     9, "ease", 2, P.EASED(N, 1, 2), P.T_EASED(N, 2), False, None),
    ("9: balanced pair at 50%", G(2, changes=[chg(1, 2)], incidents=[inc(900.0, level=2)]), C(),
     9, "ease", 3, P.EASED(N, 2, 3), P.T_EASED(N, 3), False, None),
    ("9: strict words even with a pair on record", G(0, incidents=[inc(900.0)]), C(sensitivity="strict"),
     9, "ease", 1, P.EASED_STRICT(N, 0, 1), P.T_EASED(N, 1), False, None),
]


def test_decide_rules_table():
    for label, game, ctx, rule, action, to_level, history, toast, counted, flag in RULES:
        before = copy.deepcopy(game)
        d = P.decide(game, INCIDENT, ctx)
        assert game == before, f"{label}: decide changed the record"
        assert DECISION_KEYS <= set(d), label
        got = (d["rule"], d["action"], d["to_level"], d["history"], d["toast"], d["counted"], d["flag"])
        assert got == (rule, action, to_level, history, toast, counted, flag), f"{label}: {got}"
        assert d["problem"] == ({"kind": action, "text": history} if history else None), label
        assert d["incident"] == {"ts": INCIDENT["first_ts"], "play_minutes": ctx["play_minutes_now"],
                                 "level": game["level"], "ended_game": ctx["ended_game"]}, label
        # only an ease names another level, and it is exactly one step down
        assert (d["to_level"] != game["level"]) == (action == "ease"), label
        if action == "ease":
            assert d["to_level"] == game["level"] + 1 <= 3 and d["limit"] == [450, 2225], label
        json.dumps(d)
    assert {r[3] for r in RULES} == set(range(1, 10)), "every rule has a row"
    assert {r[4] for r in RULES} == {"ignore", "warning", "ease", "hold", "would_ease"}


def test_decide_with_little_to_go_on_changes_nothing():
    """Missing or unreadable context never eases: it is read as balanced, with nothing known."""
    g = G(0)
    for ctx in (None, {}, {"sensitivity": None}, {"play_minutes_now": "x", "since_last_change_s": "soon"}):
        d = P.decide(g, INCIDENT, ctx)
        assert (d["action"], d["to_level"], d["history"]) == ("warning", 0, P.W_SURVIVED(N)), ctx
    for incident in (None, {}, {"ts": T0}, T0, "x"):
        assert P.decide(g, incident, C())["action"] == "warning"
    assert P.decide({}, INCIDENT, C())["action"] == "warning"       # a record with nothing in it
    assert P.decide(None, INCIDENT, C())["action"] == "warning"


# ── apply ─────────────────────────────────────────────────────────────
def _decide_apply(game, ctx, now=T0 + 500.0, **kw):
    d = P.decide(game, INCIDENT, ctx)
    P.apply(game, d, now, **kw)
    return d


def test_apply_ignore_leaves_the_record_alone():
    g = G(1, changes=[chg(0, 1)])
    before = copy.deepcopy(g)
    d = _decide_apply(g, C(since_last_change_s=10.0))
    assert d["action"] == "ignore" and g == before


def test_apply_warning_counted_and_not_counted():
    g = G(0)
    _decide_apply(g, C(ended_game=True, play_minutes_now=1004.0))
    assert g["incidents"] == [{"ts": INCIDENT["first_ts"], "play_minutes": 1004.0, "level": 0, "ended_game": True}]
    assert g["last_problem"] == {"ts": INCIDENT["first_ts"], "kind": "warning", "text": P.W_ENDED(N)}
    assert (g["level"], g["changes"], g["limit_at_level"], g["minutes_at_level"]) == (0, [], None, 1000.0)

    g = G(0)
    _decide_apply(g, C(other_game_running=True))
    assert g["incidents"] == []                                    # not counted: it can never pair
    assert g["last_problem"] == {"ts": INCIDENT["first_ts"], "kind": "warning", "text": P.W_OTHER(N)}
    assert _decide_apply(g, C())["action"] == "warning"            # so the next one is still a first warning

    g = G(1, changes=[chg(0, 1, hours=0.1)])
    _decide_apply(g, C(stepped_this_session=True))
    assert g["incidents"] == [{"ts": INCIDENT["first_ts"], "play_minutes": 1000.0, "level": 1, "ended_game": False}]
    assert g["last_problem"]["text"] == P.W_ALREADY(N) and g["level"] == 1
    # ...and that one pairs with the next error at the new level, in a later session
    d = P.decide(g, INCIDENT, C(play_minutes_now=1200.0))
    assert (d["rule"], d["action"], d["to_level"]) == (9, "ease", 2)


def test_apply_hold_sets_the_flag_and_keeps_the_level():
    g = G(3, changes=[chg(2, 3)])
    _decide_apply(g, C(sensitivity="strict"))
    assert g["problem_with_no_overclock"] is True and g["not_helping"] is False and g["level"] == 3
    assert len(g["incidents"]) == 1 and len(g["changes"]) == 1
    assert g["last_problem"] == {"ts": INCIDENT["first_ts"], "kind": "hold", "text": P.H_NONE(N)}

    g = G(1, at_level=240.0, changes=[chg(0, 1, hours=12.0)], limit_at_level=[450, 2225])
    _decide_apply(g, C(sensitivity="strict"))
    assert g["not_helping"] is True and g["problem_with_no_overclock"] is False and g["level"] == 1
    assert g["last_problem"]["text"] == P.H_NOT_HELPING(N)
    assert (g["minutes_at_level"], g["limit_at_level"], len(g["changes"])) == (240.0, [450, 2225], 1)

    g = G(2, changes=[chg(1, 2)])
    _decide_apply(g, C(sensitivity="strict", errors_with_no_overclock=True))
    assert g["level"] == 2 and not g["not_helping"] and not g["problem_with_no_overclock"]
    assert g["last_problem"]["text"] == P.H_FLOOR(N) and len(g["incidents"]) == 1


def test_apply_would_ease_changes_no_level():
    g = G(0, incidents=[inc(900.0)])
    d = _decide_apply(g, C(sensitivity="watch"))
    assert d["toast"] == d["history"] == P.WATCH(N, 1) and d["would_be_level"] == 1
    assert (g["level"], g["changes"], g["limit_at_level"], g["minutes_at_level"]) == (0, [], None, 1000.0)
    assert len(g["incidents"]) == 2                                # it stays counted, nothing is cleared
    assert g["last_problem"] == {"ts": INCIDENT["first_ts"], "kind": "would_ease", "text": P.WATCH(N, 1)}
    assert P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (450, 2225)


def test_apply_ease():
    g = G(0, incidents=[inc(900.0)], not_helping=True)
    now = T0 + 500.0
    d = _decide_apply(g, C(ended_game=True), now)
    assert g["level"] == 1 == d["to_level"]
    assert g["changes"] == [{"ts": now, "from_level": 0, "to_level": 1, "by": "driver",
                             "hours_at_from_level": round(1000.0 / 60.0, 4), "text": P.EASED(N, 0, 1)}]
    assert g["minutes_at_level"] == 0.0 and g["minutes"] == 1000.0
    assert g["incidents"] == []
    assert g["limit_at_level"] == [450, 2225]
    assert g["last_problem"] == {"ts": INCIDENT["first_ts"], "kind": "ease", "text": P.EASED(N, 0, 1)}
    assert g["not_helping"] is True                                # only the user's own step clears the flags
    assert set(g) == RECORD_KEYS and json.loads(json.dumps(g)) == g
    assert P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (338, 1669)


def test_apply_ease_during_a_session_keeps_the_minutes_right():
    """100 minutes on record, 60 more in the open session when the level changes: the 60 belong to the old level."""
    g = G(0, minutes=100.0, incidents=[inc(90.0)])
    _decide_apply(g, C(play_minutes_now=160.0))
    assert g["level"] == 1 and g["changes"][-1]["hours_at_from_level"] == round(160.0 / 60.0, 4)
    assert P.minutes_at_level_now(g, 160.0) == 0.0
    assert P.minutes_at_level_now(g, 175.0) == 15.0               # 15 minutes later, still in that session
    assert P.row(g, LIMIT)["hours_at_level"] == 0.0               # never a negative number on screen
    P.add_play(g, 60.0 + 30.0, T0, sessions=1)                    # the session ends 30 minutes after the change
    assert (g["minutes"], g["minutes_at_level"]) == (190.0, 30.0)
    # the engine adding time as it goes gives the same answer
    g = G(0, minutes=100.0, incidents=[inc(90.0)])
    P.add_play(g, 60.0)
    _decide_apply(g, C(play_minutes_now=160.0))
    P.add_play(g, 30.0, T0, sessions=1)
    assert (g["minutes"], g["minutes_at_level"], g["changes"][-1]["hours_at_from_level"]) == (
        190.0, 30.0, round(160.0 / 60.0, 4))


def test_apply_ease_remembers_the_overclock_in_force():
    # the limit may come through apply instead of ctx
    g = G(0)
    ctx = C(sensitivity="strict")
    del ctx["limit"]
    d = P.decide(g, INCIDENT, ctx)
    assert d["limit"] is None
    P.apply(g, d, T0, limit=[450, 2225, 110])
    assert g["level"] == 1 and g["limit_at_level"] == [450, 2225]
    # with no limit from either side nothing is invented
    g = G(0)
    P.apply(g, P.decide(g, INCIDENT, ctx), T0)
    assert g["level"] == 1 and g["limit_at_level"] is None
    # eased once at +450 / +2225; the saved overclock was raised since; a second ease must not lift it
    raised = {"exists": True, "core_offset_mhz": 600, "mem_offset_mhz": 3000}
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    had = P.offsets_for(raised, 1, g["limit_at_level"])
    _decide_apply(g, C(sensitivity="strict", limit=raised))
    assert g["level"] == 2 and g["limit_at_level"] == [450, 2225]
    assert had == (338, 1669) and P.offsets_for(raised, 2, g["limit_at_level"]) == (225, 1113)
    # lowered since: the lower value is the one in force
    lowered = [400, 2000]
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    _decide_apply(g, C(sensitivity="strict", limit=lowered))
    assert g["limit_at_level"] == [400, 2000] and P.offsets_for(lowered, 2, g["limit_at_level"]) == (200, 1000)
    # "no profile" and 0 / 0 are not a limit
    for nothing in ({"exists": False}, {"has_profile": False, "core": 0, "mem": 0}, [0, 0], None, "x"):
        g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
        d = _decide_apply(g, C(sensitivity="strict", limit=nothing))
        assert d["limit"] is None and g["level"] == 2 and g["limit_at_level"] == [450, 2225], nothing


def test_apply_refuses_a_stale_or_bent_ease():
    g = G(0)
    d = P.decide(g, INCIDENT, C(sensitivity="strict"))
    assert P.step(g, "slower", T0, LIMIT)["ok"]                    # the user got there first
    before = copy.deepcopy(g)
    P.apply(g, d, T0 + 1.0)
    assert g == before, "an ease decided for another level must change nothing"

    for bend in ({"to_level": 2}, {"to_level": 3}, {"to_level": 0}, {"to_level": "1"}, {"to_level": True},
                 {"to_level": None}, {"from_level": 2}):
        g = G(0)
        d = P.decide(g, INCIDENT, C(sensitivity="strict"))
        d.update(bend)
        before = copy.deepcopy(g)
        P.apply(g, d, T0)
        assert g == before, bend
    g = G(3, changes=[chg(2, 3)])
    before = copy.deepcopy(g)
    P.apply(g, {"action": "ease", "to_level": 4, "history": "x", "toast": None, "problem": None}, T0)
    assert g == before
    for junk in (None, {}, {"action": "raise", "to_level": 0}, "ease", []):
        P.apply(g, junk, T0)
        assert g == before
    # a hand-built decision with only the five named keys still works, and is not counted
    g = G(0)
    P.apply(g, {"action": "ease", "to_level": 1, "history": "h", "toast": None,
                "problem": {"kind": "ease", "text": "h"}}, T0, limit=LIMIT)
    assert g["level"] == 1 and g["limit_at_level"] == [450, 2225] and g["last_problem"]["ts"] == T0
    g = G(0)
    P.apply(g, {"action": "warning", "to_level": 0, "history": "h", "toast": None,
                "problem": {"kind": "warning", "text": "h"}}, T0)
    assert g["incidents"] == [] and g["last_problem"] == {"ts": T0, "kind": "warning", "text": "h"}


def test_lists_are_capped():
    g = G(0)
    for i in range(30):
        _decide_apply(g, C(play_minutes_now=1000.0 + 700.0 * i))   # too far apart to pair: 30 warnings
    assert g["level"] == 0 and len(g["incidents"]) == 20
    assert g["incidents"][-1]["play_minutes"] == 1000.0 + 700.0 * 29
    g = G(0)
    for i in range(40):
        assert P.step(g, "slower" if g["level"] == 0 else "full", T0 + i, LIMIT)["ok"]
    assert len(g["changes"]) == 30 and g["changes"][-1]["ts"] == T0 + 39
    for i in range(14):
        P.add_plain_crash(g, T0 + 100 + i, 1000.0 + i)
    assert len(g["plain_crashes"]) == 10 and g["plain_crashes"][-1]["play_minutes"] == 1013.0


# ── step ──────────────────────────────────────────────────────────────
def test_step_up_down_and_full():
    g = G(2, at_level=90.0, changes=[chg(0, 1), chg(1, 2)], incidents=[inc(990.0, level=2)],
          not_helping=True, problem_with_no_overclock=True, limit_at_level=[450, 2225])
    r = P.step(g, "faster", T0 + 7.0, LIMIT)
    assert r == {"ok": True, "err": "", "history": "You set Call of Duty to 75% of your overclock.",
                 "from_level": 2, "to_level": 1}
    assert g["level"] == 1 and g["minutes_at_level"] == 0.0 and g["incidents"] == []
    assert g["not_helping"] is False and g["problem_with_no_overclock"] is False
    assert g["limit_at_level"] == [450, 2225]
    assert g["changes"][-1] == {"ts": T0 + 7.0, "from_level": 2, "to_level": 1, "by": "user",
                                "hours_at_from_level": 1.5, "text": "You set Call of Duty to 75% of your overclock."}
    assert set(g) == RECORD_KEYS

    g = G(3, changes=[chg(2, 3)], limit_at_level=[450, 2225], problem_with_no_overclock=True)
    r = P.step(g, "full", T0, LIMIT)
    assert r["ok"] and r["history"] == "You set Call of Duty to full overclock." and r["to_level"] == 0
    assert g["level"] == 0 and g["limit_at_level"] is None and g["problem_with_no_overclock"] is False
    assert P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (450, 2225)

    g = G(0)
    for want, text in ((1, "75% of your overclock"), (2, "50% of your overclock"), (3, "no overclock")):
        r = P.step(g, "slower", T0, LIMIT)
        assert r["ok"] and r["history"] == f"You set Call of Duty to {text}." and g["level"] == want
        assert g["limit_at_level"] == [450, 2225] and g["changes"][-1]["by"] == "user"
    assert {"ok", "err", "history"} <= set(r)


def test_step_that_cannot_be_done_changes_nothing():
    for level, to, err in ((0, "faster", "Call of Duty already runs at full overclock."),
                           (0, "full", "Call of Duty already runs at full overclock."),
                           (3, "slower", "Call of Duty already runs with no overclock."),
                           (1, "sideways", "That step is not possible."),
                           (1, None, "That step is not possible.")):
        g = G(level, changes=[chg(0, level)] if level else [])
        before = copy.deepcopy(g)
        assert P.step(g, to, T0, LIMIT) == {"ok": False, "err": err, "history": ""}
        assert g == before, (level, to)


def test_step_and_the_saved_overclock():
    raised = [1000, 3000]
    # "slower" is an ease: it must never put more on the card than the game had
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    had = P.offsets_for(raised, 1, g["limit_at_level"])
    assert P.step(g, "slower", T0, raised)["ok"]
    now = P.offsets_for(raised, 2, g["limit_at_level"])
    assert g["limit_at_level"] == [450, 2225] and had == (338, 1669) and now == (225, 1113)
    # "faster" is the user's own move up: it anchors to the saved overclock as it is now
    g = G(2, changes=[chg(1, 2)], limit_at_level=[450, 2225])
    assert P.step(g, "faster", T0, [600, 3000])["ok"]
    assert g["limit_at_level"] == [600, 3000] and P.offsets_for([600, 3000], 1, g["limit_at_level"]) == (450, 2250)
    # no saved overclock known: going down keeps what was remembered, going up forgets it
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    assert P.step(g, "slower", T0, None)["ok"] and g["limit_at_level"] == [450, 2225]
    assert P.step(g, "faster", T0, {"exists": False})["ok"] and g["limit_at_level"] is None
    g = G(0)
    assert P.step(g, "slower", T0, None)["ok"] and g["limit_at_level"] is None
    # a saved overclock of 0 / 0 is no overclock: nothing to remember, so a later one is not pinned to zero
    g = G(0)
    assert P.step(g, "slower", T0, [0, 0])["ok"] and g["limit_at_level"] is None
    assert P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (338, 1669)
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    assert P.step(g, "slower", T0, {"exists": True, "core_offset_mhz": 0, "mem_offset_mhz": 0})["ok"]
    assert g["limit_at_level"] == [450, 2225]
    # whatever the user does, a part is never above the saved value, and a part saved negative is used as saved
    rnd = random.Random(7)
    g = G(0)
    for _ in range(300):
        limit = [rnd.choice((-80, 0, 150, 450, 900)), rnd.choice((-200, 0, 1000, 2225, 4000))]
        P.step(g, rnd.choice(("faster", "slower", "full")), T0, limit)
        for got, saved in zip(P.offsets_for(limit, g["level"], g["limit_at_level"]), limit):
            assert (got == saved) if saved <= 0 else (got <= saved), (limit, g["level"], g["limit_at_level"])


def test_step_during_a_session_keeps_the_minutes_right():
    g = G(1, minutes=100.0, at_level=40.0, changes=[chg(0, 1)])
    r = P.step(g, "slower", T0, LIMIT, play_minutes_now=130.0)
    assert r["ok"] and g["changes"][-1]["hours_at_from_level"] == round(70.0 / 60.0, 4)
    assert P.minutes_at_level_now(g, 130.0) == 0.0
    P.add_play(g, 50.0, T0, sessions=1)                           # the session ends 20 minutes after the step
    assert (g["minutes"], g["minutes_at_level"]) == (150.0, 20.0)


def test_step_replaces_the_last_problem_line():
    """After the user's own change the row says what the user did, never an older line the change made untrue
    ("Nothing was changed" under a 75% chip, or "Eased ..." under a Full chip)."""
    g = G(0)
    _decide_apply(g, C(ended_game=True))
    assert g["last_problem"]["text"] == P.W_ENDED(N) and P.row(g, LIMIT)["warning_open"] is True
    r = P.step(g, "slower", T0 + 900.0, LIMIT)
    assert r["ok"] and g["last_problem"] == {"ts": T0 + 900.0, "kind": "user", "text": P.USER_SET(N, 1)}
    row = P.row(g, LIMIT)
    assert row["last_problem"]["text"] == "You set Call of Duty to 75% of your overclock."
    assert row["warning_open"] is False and row["eased_by_driver"] is False and g["incidents"] == []
    # after an ease, "Try full overclock"
    g = G(0, incidents=[inc(900.0)])
    _decide_apply(g, C(ended_game=True))
    assert g["level"] == 1 and g["last_problem"]["kind"] == "ease" and P.row(g, LIMIT)["eased_by_driver"] is True
    assert P.step(g, "full", T0 + 901.0, LIMIT)["ok"]
    assert g["last_problem"] == {"ts": T0 + 901.0, "kind": "user", "text": P.USER_SET(N, 0)}
    assert P.row(g, LIMIT)["eased_by_driver"] is False
    # a step that cannot be done leaves the line alone
    before = copy.deepcopy(g["last_problem"])
    assert not P.step(g, "full", T0 + 902.0, LIMIT)["ok"] and g["last_problem"] == before


def test_a_retried_level_gets_no_free_warning():
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    assert P.step(g, "faster", T0, LIMIT)["ok"] and g["level"] == 0
    P.add_play(g, 240.0)
    d = _decide_apply(g, C(play_minutes_now=g["minutes"], ended_game=True))
    assert (d["rule"], d["action"]) == (9, "ease") and g["level"] == 1
    assert d["history"] == "Call of Duty had a graphics driver error. Eased from full overclock to 75% of your overclock."
    assert P.failures_at(g, 0) == 2 and P.best_left_here(g)
    # but a game the user left alone for more than 10 hours of play is back to a first warning
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    assert P.step(g, "faster", T0, LIMIT)["ok"]
    P.add_play(g, 601.0)
    assert _decide_apply(g, C(play_minutes_now=g["minutes"]))["action"] == "warning" and g["level"] == 0


# ── hints ─────────────────────────────────────────────────────────────
def test_plain_crashes_and_their_hint():
    g = G(0, minutes=1000.0)
    P.add_plain_crash(g, T0 + 1.0, 1000.0)
    assert g["plain_crashes"] == [{"ts": T0 + 1.0, "play_minutes": 1000.0}]
    assert g["last_problem"] == {"ts": T0 + 1.0, "kind": "plain_crash", "text": P.PLAIN_CRASH(N)}
    assert (g["level"], g["incidents"], g["changes"]) == (0, [], []), "a plain crash is never evidence"
    P.add_plain_crash(g, T0 + 2.0, 1300.0)
    assert not P.plain_crash_hint(g)
    P.add_plain_crash(g, T0 + 3.0, 1600.5)                        # the first one is now more than 10 hours back
    assert not P.plain_crash_hint(g)
    P.add_plain_crash(g, T0 + 4.0, 1900.0)                        # 1300, 1600.5 and 1900: three within 10 hours
    assert P.plain_crash_hint(g) and P.row(g, LIMIT)["plain_crash_hint"] is True
    g["minutes"] = 1900.0 + 600.0
    assert P.plain_crash_hint(g)
    g["minutes"] = 1900.0 + 601.0                                 # 10 hours of play without one: the hint goes away
    assert not P.plain_crash_hint(g)
    P.add_plain_crash(g, T0 + 5.0, 2600.0)                        # 1600.5, 1900 and 2600 are too far apart
    assert not P.plain_crash_hint(g)
    # it never leads to a decision on its own
    assert P.decide(g, INCIDENT, C(play_minutes_now=2600.0))["action"] == "warning"

    # the hint is about the level the game is at: after any change it starts again
    g = G(0, minutes=1000.0)
    for i in range(3):
        P.add_plain_crash(g, T0 + i, 1000.0 + i)
    assert P.plain_crash_hint(g)
    assert P.step(g, "slower", T0 + 10.0, LIMIT)["ok"]
    assert not P.plain_crash_hint(g), "the user just eased it: do not ask again without new crashes"
    for i in range(3):
        P.add_plain_crash(g, T0 + 20.0 + i, 1010.0 + i)
    assert P.plain_crash_hint(g)


def test_suggest_step_up_and_best_left_here():
    assert not P.suggest_step_up(G(0, minutes=5000.0)) and not P.best_left_here(G(0, minutes=5000.0))
    once = [chg(0, 1)]
    assert not P.suggest_step_up(G(1, minutes=5000.0, at_level=1199.9, changes=once))
    assert P.suggest_step_up(G(1, minutes=5000.0, at_level=1200.0, changes=once))
    # a counted error restarts the 20 hours
    assert not P.suggest_step_up(G(1, minutes=5000.0, at_level=3000.0, changes=once, incidents=[inc(4500.0, 1)]))
    assert not P.suggest_step_up(G(1, minutes=5000.0, at_level=3000.0, changes=once, incidents=[inc(3800.5, 1)]))
    assert P.suggest_step_up(G(1, minutes=5000.0, at_level=3000.0, changes=once, incidents=[inc(3800.0, 1)]))
    assert not P.suggest_step_up(G(1, minutes=5000.0, at_level=3000.0, changes=once,
                                   incidents=[{"ts": T0, "level": 1}]))       # no play time on it: assume recent
    # the level above failed twice: best left here, and no suggestion
    twice = [chg(0, 1), chg(1, 0, by="user"), chg(0, 1)]
    g = G(1, minutes=5000.0, at_level=3000.0, changes=twice)
    assert P.best_left_here(g) and not P.suggest_step_up(g)
    assert not P.best_left_here(G(1, minutes=5000.0, at_level=3000.0, changes=once))
    # the user's own steps down are not failures
    own = [chg(0, 1, by="user"), chg(1, 0, by="user"), chg(0, 1, by="user")]
    g = G(1, minutes=5000.0, at_level=3000.0, changes=own)
    assert not P.best_left_here(g) and P.suggest_step_up(g) and P.failures_at(g, 0) == 0
    # failures are per level, and can be counted since a time
    g = G(2, changes=[chg(0, 1, ts=T0 - 400.0), chg(1, 2, ts=T0 - 300.0), chg(2, 1, by="user", ts=T0 - 200.0),
                      chg(1, 2, ts=T0 - 100.0)])
    assert (P.failures_at(g, 0), P.failures_at(g, 1), P.failures_at(g, 2)) == (1, 2, 0)
    assert P.failures_at(g, 1, since=T0 - 150.0) == 1 and P.failures_at(g, 1, since=T0) == 0
    assert P.best_left_here(g)


def test_warning_open_is_true_only_while_the_next_error_would_ease():
    """The page adds "One more within 10 hours of play and it will be eased." only while row()["warning_open"]."""
    def warned(game, **ctx):
        d = _decide_apply(game, C(**ctx))
        assert d["action"] == "warning" and d["counted"], d
        return game

    # a first warning, in play time: open for 10 hours of play, then not
    g = warned(G(0), ended_game=True)
    assert g["last_problem"]["text"] == P.W_ENDED(N) and P.row(g, LIMIT)["warning_open"] is True
    assert P.warning_open(g, play_minutes_now=1600.0) and not P.warning_open(g, play_minutes_now=1600.5)
    # one the game survived: for the first hour of play another error is the same spell, not a second one (D6),
    # so the promise is only made once that hour has been played
    g = warned(G(0))
    assert g["last_problem"]["text"] == P.W_SURVIVED(N) and P.row(g, LIMIT)["warning_open"] is False
    assert [P.warning_open(g, play_minutes_now=x) for x in (1000.0, 1059.9, 1060.0, 1600.0, 1600.5)] == [
        False, False, True, True, False]
    for now_min, opened in ((1059.9, False), (1060.0, True)):         # and it is so
        again = P.decide(copy.deepcopy(g), INCIDENT, C(play_minutes_now=now_min))
        assert (again["action"] == "ease") is opened and P.warning_open(g, play_minutes_now=now_min) is opened
        assert again["history"] == (P.EASED(N, 0, 1) if opened else P.W_SAME_SPELL(N))
    later = copy.deepcopy(g)
    P.add_play(later, 600.5, T0 + 99999.0, sessions=1)
    assert P.row(later, LIMIT)["warning_open"] is False, "11 hours of play later the promise is gone"
    assert P.decide(later, INCIDENT, C(play_minutes_now=later["minutes"]))["action"] == "warning"   # and it is so
    # during the session that logged it the record lags the incident: still open
    g = warned(G(0), play_minutes_now=1030.0, ended_game=True)
    assert P.row(g, LIMIT)["warning_open"] is True and P.row(g, LIMIT, play_minutes_now=1030.0)["warning_open"]
    # the same spell is a counted warning too: open once an hour of play lies between its first error and now
    g = warned(G(0, incidents=[inc(990.0)]))
    assert g["last_problem"]["text"] == P.W_SAME_SPELL(N) and P.row(g, LIMIT)["warning_open"] is False
    assert [P.warning_open(g, play_minutes_now=x) for x in (1049.9, 1050.0)] == [False, True]
    assert P.decide(copy.deepcopy(g), INCIDENT, C(play_minutes_now=1050.0))["action"] == "ease"
    # one of a spell that closed the game pairs at once
    g = warned(G(0, incidents=[inc(990.0)]), ended_game=True)
    assert g["last_problem"]["text"] == P.W_SAME_SPELL(N) and P.row(g, LIMIT)["warning_open"] is True
    # the second error after an ease in the same session pairs with the next one: open once easing may go on
    g = warned(G(1, changes=[chg(0, 1, hours=0.1)]), stepped_this_session=True)
    assert g["last_problem"]["text"] == P.W_ALREADY(N) and P.row(g, LIMIT)["warning_open"] is False
    assert P.warning_open(g, play_minutes_now=1060.0) is True
    g = warned(G(1, changes=[chg(0, 1, hours=0.1)]), stepped_this_session=True, ended_game=True)
    assert g["last_problem"]["text"] == P.W_ALREADY(N) and P.row(g, LIMIT)["warning_open"] is True

    # nothing to promise: the last line is not a counted warning about this level
    g = warned(G(0))
    P.add_plain_crash(g, T0 + 600.0, 1001.0)
    assert P.row(g, LIMIT)["warning_open"] is False, "a plain crash line gets no promise"
    g = warned(G(0))
    other = {"first_ts": T0 + 4000.0, "last_ts": T0 + 4000.0, "count": 1}
    P.apply(g, P.decide(g, other, C(other_game_running=True, play_minutes_now=1050.0)), T0 + 4100.0)
    assert g["last_problem"]["text"] == P.W_OTHER(N) and P.row(g, LIMIT)["warning_open"] is False
    g = G(0, incidents=[inc(900.0)])
    _decide_apply(g, C(sensitivity="watch"))
    assert g["last_problem"]["kind"] == "would_ease" and P.row(g, LIMIT)["warning_open"] is False
    g = warned(G(0))
    assert P.step(g, "slower", T0 + 700.0, LIMIT)["ok"] and P.row(g, LIMIT)["warning_open"] is False

    # rule 5: at no overclock the next one is never eased
    g = warned(G(3, changes=[chg(2, 3)]))
    assert P.row(g, LIMIT)["warning_open"] is False
    # rule 7: at 50% it is only promised when the driver is known to log no errors with no overclock
    g = warned(G(2, changes=[chg(1, 2, hours=0.1)]), ended_game=True)
    assert [P.row(g, LIMIT, errors_with_no_overclock=x)["warning_open"] for x in (None, True, False)] == [
        False, False, True]
    assert P.row(g, LIMIT)["warning_open"] is False, "not known: no promise"
    # rule 6: while easing is held as not helping, judged with the open session
    g = warned(G(1, at_level=60.0, changes=[chg(0, 1, hours=12.0)]))
    assert P.row(g, LIMIT)["warning_open"] is False
    assert P.warning_open(g, play_minutes_now=1239.0) is False and P.warning_open(g, play_minutes_now=1240.0) is True
    assert P.decide(g, INCIDENT, C(play_minutes_now=1239.0))["action"] == "hold"
    assert P.decide(g, INCIDENT, C(play_minutes_now=1240.0))["action"] == "ease"
    # a warning dated before the game's last change of level (judged after it) promises nothing
    g = warned(G(1, changes=[chg(0, 1, hours=0.1, ts=INCIDENT["first_ts"] + 1.0)]))
    assert P.row(g, LIMIT)["warning_open"] is False
    # damaged lines
    for bad in ({"ts": None, "kind": "warning", "text": "x"}, {"kind": "warning"}, "x", None):
        g = warned(G(0))
        g["last_problem"] = bad
        assert P.warning_open(g) is False, bad
    assert P.warning_open(None) is False and P.warning_open({}) is False


def test_eased_by_driver():
    assert P.eased_by_driver(G(0)) is False
    assert P.eased_by_driver(G(0, changes=[chg(1, 0, by="user")])) is False
    assert P.eased_by_driver(G(1, changes=[chg(0, 1)])) is True
    assert P.eased_by_driver(G(2, changes=[chg(0, 1), chg(1, 2, by="user")])) is False
    assert P.eased_by_driver(G(3, changes=[chg(1, 2, by="user"), chg(2, 3)])) is True
    assert P.eased_by_driver(G(1)) is False                         # below Full with no change on record
    assert P.eased_by_driver(G(1, changes=[chg(0, 2)])) is False    # a change that does not match the level
    g = G(0, incidents=[inc(900.0)])
    _decide_apply(g, C())
    assert g["level"] == 1 and P.row(g, LIMIT)["eased_by_driver"] is True
    assert P.step(g, "slower", T0 + 900.0, LIMIT)["ok"] and P.row(g, LIMIT)["eased_by_driver"] is False


def test_row():
    g = P.new_game(EXE, NAME, T0)
    assert P.row(g, LIMIT) == {
        "exe": "cod.exe", "name": "Call of Duty", "enabled": True, "level": 0, "level_name": "Full", "percent": 100,
        "core": 450, "mem": 2225, "sessions": 0, "hours": 0.0, "hours_at_level": 0.0, "last_played": T0,
        "last_problem": None, "not_helping": False, "problem_with_no_overclock": False, "limit_changed": False,
        "can_step_up": False, "next_up_name": "", "can_step_down": True, "suggest_step_up": False,
        "best_left_here": False, "plain_crash_hint": False, "warning_open": False, "eased_by_driver": False}
    want = {1: ("75%", 75, 338, 1669, "Full", True), 2: ("50%", 50, 225, 1113, "75%", True),
            3: ("No overclock", 0, 0, 0, "50%", False)}
    for level, (name, percent, core, mem, up, down) in want.items():
        g = G(level, minutes=1234.0, at_level=95.0, changes=[chg(level - 1, level)], limit_at_level=[450, 2225],
              sessions=12, enabled=False, last_problem={"ts": T0, "kind": "ease", "text": "t"})
        r = P.row(g, LIMIT)
        assert set(r) == ROW_KEYS
        assert (r["level"], r["level_name"], r["percent"], r["core"], r["mem"]) == (level, name, percent, core, mem)
        assert (r["can_step_up"], r["next_up_name"], r["can_step_down"]) == (True, up, down)
        assert (r["hours"], r["hours_at_level"], r["sessions"], r["enabled"]) == (20.6, 1.6, 12, False)
        assert r["limit_changed"] is False
        assert r["eased_by_driver"] is True and r["warning_open"] is False      # its last line is an ease
        assert r["last_problem"] == g["last_problem"] and r["last_problem"] is not g["last_problem"]
        json.dumps(r)
    # the saved overclock changed since the game was eased
    g = G(1, changes=[chg(0, 1)], limit_at_level=[450, 2225])
    r = P.row(g, [500, 2500])
    assert r["limit_changed"] is True and (r["core"], r["mem"]) == (338, 1669)
    assert P.row(g, [400, 2225])["limit_changed"] is True and P.row(g, None)["limit_changed"] is True
    assert (P.row(g, None)["core"], P.row(g, None)["mem"]) == (0, 0)
    assert P.row(G(0, limit_at_level=[450, 2225]), [500, 2500])["limit_changed"] is False     # full follows the limit
    assert P.row(G(1, changes=[chg(0, 1)]), [500, 2500])["limit_changed"] is False            # nothing remembered
    # flags and hints come through
    g = G(1, minutes=5000.0, at_level=1300.0, changes=[chg(0, 1)], not_helping=True, problem_with_no_overclock=True)
    r = P.row(g, LIMIT)
    assert r["not_helping"] and r["problem_with_no_overclock"] and r["suggest_step_up"] and not r["best_left_here"]
    # a damaged record still gives a full row
    r = P.row({"exe": "x.exe", "level": "9", "minutes": None, "incidents": 5, "changes": [None], "last_problem": 3},
              LIMIT)
    assert set(r) == ROW_KEYS and r["name"] == "x.exe" and r["level"] == 3 and r["last_problem"] is None
    assert r["warning_open"] is False and r["eased_by_driver"] is False
    r = P.row({"level": 1, "changes": "x", "incidents": [None, 3], "last_problem": {"kind": "warning", "ts": "x"}},
              LIMIT, play_minutes_now="soon", errors_with_no_overclock="x")
    assert r["warning_open"] is False and r["eased_by_driver"] is False


def test_row_agrees_with_the_ui_fixtures():
    """tests/fixtures/at2_states.json (the UI's examples of get_state) and row() describe a game the same way."""
    path = os.path.join(m.REPO, "tests", "fixtures", "at2_states.json")
    try:
        with open(path, encoding="utf-8") as f:
            states = json.load(f)
    except (OSError, ValueError) as e:
        raise unittest.SkipTest(f"no readable UI fixture: {e}")
    rows = 0
    for label, state in states.items():
        if not isinstance(state, dict):
            continue
        for g in state.get("games") or ():
            rows += 1
            where = f"{label} / {g.get('exe')}"
            assert set(g) == ROW_KEYS, where
            level = g["level"]
            assert (g["level_name"], g["percent"]) == (P.level_name(level), P.level_percent(level)), where
            assert g["next_up_name"] == P.row(G(level), None)["next_up_name"], where
            assert (g["can_step_up"], g["can_step_down"]) == (level > 0, level < 3), where
            if not g["limit_changed"]:
                assert (g["core"], g["mem"]) == P.offsets_for(state.get("limit"), level), where
            problem = g["last_problem"]
            if problem is not None:
                assert set(problem) == {"ts", "kind", "text"}, where
                # a game's own name may hold such a word ("Counter-Strike 2"); the sentence around it may not
                assert not bad_words(problem["text"].replace(g["name"], "")), where
                assert problem["kind"] in {"warning", "ease", "hold", "would_ease", "plain_crash", "user"}, where
            # row() only says a warning is open for a counted warning below no overclock, and only calls a game
            # eased by the driver below Full
            assert type(g["warning_open"]) is bool and type(g["eased_by_driver"]) is bool, where
            if g["warning_open"]:
                assert problem is not None and problem["kind"] == "warning" and level < 3, where
            if g["eased_by_driver"]:
                assert level > 0, where
    assert rows, "the fixture lists no games"


# ── texts ─────────────────────────────────────────────────────────────
TEXTS = [
    # a warning carries no promise: the page adds it only while row()["warning_open"]
    (P.W_SURVIVED(N), "Call of Duty had a graphics driver error but kept running. Nothing was changed."),
    (P.W_ENDED(N), "Call of Duty closed after a graphics driver error. Nothing was changed."),
    (P.W_SAME_SPELL(N), "Call of Duty had more graphics driver errors in the same spell. Nothing was changed."),
    (P.W_OTHER(N), "The graphics driver logged an error while Call of Duty and another game were both running, "
                   "so nothing was changed."),
    (P.W_ALREADY(N), "Call of Duty had another graphics driver error. It was already eased this session, "
                     "so nothing more was changed."),
    (P.EASED(N, 0, 1), "Call of Duty had graphics driver errors twice within 10 hours of play. "
                       "Eased from full overclock to 75% of your overclock."),
    (P.EASED(N, 1, 2), "Call of Duty had graphics driver errors twice within 10 hours of play. "
                       "Eased from 75% of your overclock to 50% of your overclock."),
    (P.EASED(N, 2, 3), "Call of Duty had graphics driver errors twice within 10 hours of play. "
                       "Eased from 50% of your overclock to no overclock."),
    (P.EASED_STRICT(N, 0, 1), "Call of Duty had a graphics driver error. "
                              "Eased from full overclock to 75% of your overclock."),
    (P.EASED_STRICT(N, 2, 3), "Call of Duty had a graphics driver error. "
                              "Eased from 50% of your overclock to no overclock."),
    (P.T_EASED(N, 1), "Call of Duty now runs at 75% of your overclock after graphics driver errors. "
                      "You can undo this in Vispora under Profiles."),
    (P.T_EASED(N, 2), "Call of Duty now runs at 50% of your overclock after graphics driver errors. "
                      "You can undo this in Vispora under Profiles."),
    (P.T_EASED(N, 3), "Call of Duty now runs with no overclock after graphics driver errors. "
                      "You can undo this in Vispora under Profiles."),
    (P.H_NOT_HELPING(N), "Call of Duty had graphics driver errors again soon after it was eased, so easing does "
                         "not seem to help. It was left where it is. You can ease it one more step yourself."),
    (P.H_NONE(N), "Call of Duty had graphics driver errors with no overclock on the card, "
                  "so the overclock is probably not the cause."),
    (P.H_FLOOR(N), "Call of Duty had graphics driver errors again. The graphics driver also logs errors with no "
                   "overclock on the card, so it stays at 50% of your overclock."),
    (P.WATCH(N, 1), "Call of Duty: Adaptive Tuning would have eased it to 75% of your overclock after graphics "
                    "driver errors. It is set to only tell you, so nothing was changed."),
    (P.WATCH(N, 3), "Call of Duty: Adaptive Tuning would have eased it to no overclock after graphics "
                    "driver errors. It is set to only tell you, so nothing was changed."),
    (P.PLAIN_CRASH(N), "Call of Duty closed unexpectedly. No graphics driver error was logged, "
                       "so the overclock was left alone."),
    (P.PC_STOPPED(N), "Your PC stopped while Call of Duty was running. No graphics driver error was logged, "
                      "so nothing was changed."),
    (P.HANDED_BACK(N), "Vispora stopped while Call of Duty was running. The overclock was put back at the next start."),
    (P.USER_SET(N, 0), "You set Call of Duty to full overclock."),
    (P.USER_SET(N, 1), "You set Call of Duty to 75% of your overclock."),
    (P.USER_SET(N, 2), "You set Call of Duty to 50% of your overclock."),
    (P.USER_SET(N, 3), "You set Call of Duty to no overclock."),
    (P.HELD_HOT(N), "The graphics card got too hot while Call of Duty was running, "
                    "so the overclock stayed off until the game closed."),
    (P.HELD_MANUAL(N), "You changed the overclock while Call of Duty was running, "
                       "so Adaptive Tuning left it alone until the game closed."),
    (P.HELD_OTHER(N), "Another program kept changing the overclock while Call of Duty was running, "
                      "so Adaptive Tuning left it alone until the game closed."),
    (P.HELD_RELEASED(N), "Game mode was turned off while Call of Duty was running, "
                         "so the overclock was kept where it was until the game closed."),
    (P.LIMIT_CHANGED(N), "Your saved overclock changed since Call of Duty was eased."),
    (P.REPORT_LINE(0), "Overclock: full, no problems"),
    (P.REPORT_LINE(1), "Overclock: 75%, no problems"),
    (P.REPORT_LINE(2), "Overclock: 50%, no problems"),
    (P.REPORT_LINE(3), "No overclock for this game"),
    (P.REPORT_LINE(1, eased_to=1), "Overclock eased to 75% of your overclock after graphics driver errors"),
    (P.REPORT_LINE(2, eased_to=2), "Overclock eased to 50% of your overclock after graphics driver errors"),
    (P.REPORT_LINE(0, held="manual"), "Overclock: left alone after you changed it"),
    (P.REPORT_LINE(0, held="hot"), "Overclock: off after the card got too hot"),
    (P.REPORT_LINE(0, held="released"), "Overclock: kept until the game closed"),
    (P.REPORT_LINE(1, eased_to=1, held="hot"), "Overclock eased to 75% of your overclock after graphics driver errors"),
    (P.REPORT_LINE(0, trouble="driver"), "Overclock: full, a graphics driver error was noted"),
    (P.REPORT_LINE(1, trouble="driver"), "Overclock: 75%, a graphics driver error was noted"),
    (P.REPORT_LINE(2, trouble="driver"), "Overclock: 50%, a graphics driver error was noted"),
    (P.REPORT_LINE(0, trouble="crash"), "Overclock: full, the game closed unexpectedly"),
    (P.REPORT_LINE(1, trouble="crash"), "Overclock: 75%, the game closed unexpectedly"),
    (P.REPORT_LINE(2, trouble="crash"), "Overclock: 50%, the game closed unexpectedly"),
]


def test_texts_word_for_word():
    for got, want in TEXTS:
        assert got == want, f"{got!r} != {want!r}"
    assert P.REPORT_LINE(0, held="other_program") is None          # the spec has no report line for it
    # a name with braces or a percent sign is printed as it is
    assert P.W_ENDED("100% {Orange} Juice").startswith("100% {Orange} Juice closed after")
    # no warning or hold sentence promises what happens next: the page says that only while it is true
    for text in (P.W_SURVIVED(N), P.W_ENDED(N), P.W_SAME_SPELL(N), P.W_OTHER(N), P.W_ALREADY(N),
                 P.H_NOT_HELPING(N)):
        assert "will be" not in text and "One more" not in text and "probably not the cause" not in text, text


def test_report_line_order():
    """eased_to, then held, then no overclock, then trouble, then "no problems"."""
    eased = "Overclock eased to 75% of your overclock after graphics driver errors"
    assert P.REPORT_LINE(1, eased_to=1, held="released", trouble="driver") == eased
    assert P.REPORT_LINE(0, held="hot", trouble="driver") == "Overclock: off after the card got too hot"
    assert P.REPORT_LINE(0, held="manual", trouble="crash") == "Overclock: left alone after you changed it"
    assert P.REPORT_LINE(3, held="released") == "Overclock: kept until the game closed"
    assert P.REPORT_LINE(1, held="released", trouble="driver") == "Overclock: kept until the game closed"
    assert P.REPORT_LINE(0, held="other_program", trouble="driver") is None
    assert P.REPORT_LINE(3, trouble="driver") == P.REPORT_LINE(3, trouble="crash") == "No overclock for this game"
    assert P.REPORT_LINE(0, trouble=None) == "Overclock: full, no problems"
    # a kind of trouble this module does not know never reads as "no problems"
    assert P.REPORT_LINE(0, trouble="other") is None and P.REPORT_LINE(1, trouble=True) is None
    assert P.REPORT_LINE(0, None, None) == "Overclock: full, no problems"                 # positional use still works


def _every_text():
    """Every sentence this module can hand to a user: each text function with every argument it takes, the
    decisions of the rule table, step answers, row strings, level names."""
    out = set(P.LEVEL_NAMES) | set(P.LEVEL_PHRASES) | {P.TOAST_TITLE}
    choices = {"name": [NAME], "from_level": range(4), "to_level": range(4), "level": range(4),
               "eased_to": [None, 0, 1, 2, 3], "held": [None, "manual", "hot", "released", "other_program"],
               "trouble": [None, "driver", "crash"]}
    functions = [(k, v) for k, v in vars(P).items() if k.isupper() and inspect.isfunction(v)]
    for fname, fn in functions:
        params = list(inspect.signature(fn).parameters)
        assert set(params) <= set(choices), f"{fname} takes an argument this test does not know: {params}"
        for combo in itertools.product(*(choices[p] for p in params)):
            text = fn(*combo)
            if text is not None:
                assert isinstance(text, str) and text.strip() == text and text, (fname, combo)
                out.add(text)
    for _label, game, ctx, *_rest in RULES:
        d = P.decide(game, INCIDENT, ctx)
        out.update(t for t in (d["history"], d["toast"], (d["problem"] or {}).get("text")) if t)
    for level in range(4):
        g = G(level, changes=[chg(0, level)] if level else [])
        out.update(str(v) for v in P.row(g, LIMIT).values() if isinstance(v, str))
        for to in ("faster", "slower", "full", "sideways"):
            r = P.step(copy.deepcopy(g), to, T0, LIMIT)
            out.update(t for t in (r["err"], r["history"]) if t)
    return len(functions), out


def test_no_banned_words_in_anything_a_user_can_read():
    assert bad_words("AT raised the offset above stock") == ["offset", "stock", "AT"]      # the checker works
    assert bad_words("A strike, a TDR, the baseline, a blacklist, it yielded " + EM_DASH) == [
        "tdr", "blacklist", "baseline", "yielded", "strike", "em dash"]
    assert bad_words("Eased at 75% of your overclock. At last.") == []
    count, texts = _every_text()
    assert count >= 20 and len(texts) > 60, (count, len(texts))
    for text in sorted(texts):
        assert not bad_words(text), f"{bad_words(text)} in {text!r}"
    for got, _want in TEXTS:
        assert got in texts


def test_no_em_dash_in_the_source_and_the_module_is_pure():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(m.REPO, "core", "adaptive_policy.py"), os.path.join(here, "test_adaptive_policy.py")]
    for path in paths:
        with open(path, encoding="utf-8") as f:
            assert EM_DASH not in f.read(), path
    with open(paths[0], encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            raise AssertionError(f"import {node.names[0].name}: the policy imports nothing")
        if isinstance(node, ast.ImportFrom):
            assert node.module == "__future__", f"from {node.module} import ...: the policy imports nothing"
        if isinstance(node, ast.Name):
            assert node.id not in {"open", "print", "input", "exec", "eval", "__import__", "globals", "time",
                                   "datetime", "os", "sys", "threading", "ctypes", "json"}, node.id
    # same inputs, same answer, whatever the clock says
    a = P.decide(G(0, incidents=[inc(900.0)]), INCIDENT, C())
    b = P.decide(G(0, incidents=[inc(900.0)]), INCIDENT, C())
    assert a == b


# ── replays ───────────────────────────────────────────────────────────
def play(game, sessions, sensitivity, errors_with_no_overclock=False, limit=LIMIT):
    """Drive the policy the way the engine does and return the decisions in order.

    A session is {"minutes": length, "incidents": [(minute into the session, did it close the game)]}.  An error
    that closed the game is decided when the session ends; one the game survived, 2 minutes later.  Play time
    reaches the record when the session ends."""
    decisions = []
    clock = T0
    for s in sessions:
        stepped, changed_at = False, None
        for at, ended in s.get("incidents", ()):
            decided_at = s["minutes"] if ended else min(s["minutes"], at + 2.0)
            ctx = {"sensitivity": sensitivity, "ended_game": ended,
                   "play_minutes_now": game["minutes"] + decided_at,
                   "stepped_this_session": stepped,
                   "since_last_change_s": None if changed_at is None else (at - changed_at) * 60.0,
                   "other_game_running": False, "errors_with_no_overclock": errors_with_no_overclock,
                   "limit": limit}
            ts = clock + at * 60.0
            before = game["level"]
            d = P.decide(game, {"first_ts": ts, "last_ts": ts, "count": 1}, ctx)
            P.apply(game, d, clock + decided_at * 60.0)
            assert game["level"] - before == (1 if d["action"] == "ease" else 0)
            assert not (d["action"] == "ease" and stepped), "more than one step in a session"
            if d["action"] == "ease":
                stepped, changed_at = True, decided_at
            decisions.append(d)
        P.add_play(game, s["minutes"], clock + s["minutes"] * 60.0, sessions=1)
        clock += s["minutes"] * 60.0 + 3600.0
    return decisions


# The owner's real log (critique.md, "Attribution"): three nvlddmkm 153 errors, each followed by cod.exe closing
# within 35 s (25 Oct 13:10:41, 26 Oct 18:40:54, 1 Nov 16:14:13), at the end of sessions of 4.1, 355.7 and 21.2
# minutes, with 11.8 and 16.6 hours of play between them.  11 of this PC's 19 driver errors were logged with no
# overclock on the card and no game running.
OWNER_REPLAY = [
    {"minutes": 4.1, "incidents": [(4.1 - 35 / 60.0, True)]},
    {"minutes": 150.0}, {"minutes": 120.0}, {"minutes": 82.3},
    {"minutes": 355.7, "incidents": [(355.7 - 20 / 60.0, True)]},
    {"minutes": 240.0}, {"minutes": 240.0}, {"minutes": 240.0}, {"minutes": 254.8},
    {"minutes": 21.2, "incidents": [(21.2 - 34 / 60.0, True)]},
]

# An overclock that really is unstable: a graphics driver error closes the game every 2 hours of play.
UNSTABLE = [{"minutes": 120.0, "incidents": [(119.5, True)]} for _ in range(8)]

# The same, in long sessions the game survives: errors 2 hours apart, one more inside the reset storm.
UNSTABLE_LONG = [
    {"minutes": 360.0, "incidents": [(120.0, False), (240.0, False), (245.0, False), (355.0, False)]},
    {"minutes": 240.0, "incidents": [(120.0, False)]},
]


def test_owner_replay_fixture_is_the_log():
    ends, total = [], 0.0
    for s in OWNER_REPLAY:
        total += s["minutes"]
        if s.get("incidents"):
            ends.append(total)
    assert len(ends) == 3
    assert abs((ends[1] - ends[0]) / 60.0 - 11.8) < 1e-6 and abs((ends[2] - ends[1]) / 60.0 - 16.6) < 1e-6


def test_owner_replay_balanced_stays_at_full_with_three_warnings():
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, OWNER_REPLAY, "balanced", errors_with_no_overclock=True)
    assert [d["action"] for d in ds] == ["warning", "warning", "warning"]
    assert [d["history"] for d in ds] == [P.W_ENDED(N)] * 3 and [d["toast"] for d in ds] == [None] * 3
    assert g["level"] == 0 and g["changes"] == [] and g["limit_at_level"] is None
    assert len(g["incidents"]) == 3 and all(i["ended_game"] and i["level"] == 0 for i in g["incidents"])
    assert not g["not_helping"] and not g["problem_with_no_overclock"]
    assert g["sessions"] == 10 and abs(g["minutes"] - 1708.1) < 1e-6
    r = P.row(g, LIMIT)
    assert (r["level_name"], r["core"], r["mem"], r["can_step_up"]) == ("Full", 450, 2225, False)
    assert r["last_problem"]["text"] == P.W_ENDED(N)
    # the same with the errors-with-no-overclock evidence unknown
    g2 = P.new_game(EXE, NAME, T0)
    assert [d["action"] for d in play(g2, OWNER_REPLAY, "balanced")] == ["warning"] * 3 and g2["level"] == 0


def test_owner_replay_watch_only_warns():
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, OWNER_REPLAY, "watch", errors_with_no_overclock=True)
    assert [d["action"] for d in ds] == ["warning"] * 3 and g["level"] == 0 and g["changes"] == []


def test_owner_replay_strict():
    """Strict: 75%, then 50%, one step per session.  The third error comes after 16.6 hours at 50%, which is not
    fewer than half of the 11.8 hours at 75% (counted as 10), so by the rules it is not "easing does not help": the
    game goes to no overclock, unless the driver also logs errors with no overclock on the card (true on the
    owner's PC), in which case it stays at 50%."""
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, OWNER_REPLAY, "strict")
    assert [(d["rule"], d["action"], d["to_level"]) for d in ds] == [(9, "ease", 1), (9, "ease", 2), (9, "ease", 3)]
    assert [d["history"] for d in ds] == [P.EASED_STRICT(N, 0, 1), P.EASED_STRICT(N, 1, 2), P.EASED_STRICT(N, 2, 3)]
    assert [d["toast"] for d in ds] == [P.T_EASED(N, 1), P.T_EASED(N, 2), P.T_EASED(N, 3)]
    assert [(c["from_level"], c["to_level"], c["by"]) for c in g["changes"]] == [
        (0, 1, "driver"), (1, 2, "driver"), (2, 3, "driver")]
    assert [c["hours_at_from_level"] for c in g["changes"]] == [round(4.1 / 60.0, 4), 11.8, 16.6]
    assert g["level"] == 3 and P.offsets_for(LIMIT, 3, g["limit_at_level"]) == (0, 0)
    assert abs(g["minutes_at_level"]) < 1e-6 and g["incidents"] == []

    g = P.new_game(EXE, NAME, T0)
    ds = play(g, OWNER_REPLAY, "strict", errors_with_no_overclock=True)
    assert [(d["rule"], d["action"], d["to_level"]) for d in ds] == [(9, "ease", 1), (9, "ease", 2), (7, "hold", 2)]
    assert ds[2]["history"] == P.H_FLOOR(N) and ds[2]["toast"] is None
    assert g["level"] == 2 and P.offsets_for(LIMIT, 2, g["limit_at_level"]) == (225, 1113)
    assert not g["not_helping"] and not g["problem_with_no_overclock"]
    assert P.row(g, LIMIT)["level_name"] == "50%"


def test_owner_replay_strict_with_a_quicker_third_error_stops_at_not_helping():
    """Had the third error come after 4.85 hours at 50% (fewer than half of the 11.8 at 75%, counted as 10),
    strict holds there; at 5.35 hours it goes on to no overclock."""
    sooner = OWNER_REPLAY[:5] + [{"minutes": 270.0}] + OWNER_REPLAY[-1:]
    for errors_with_no_overclock in (False, True):
        g = P.new_game(EXE, NAME, T0)
        ds = play(g, sooner, "strict", errors_with_no_overclock)
        assert [(d["rule"], d["action"]) for d in ds] == [(9, "ease"), (9, "ease"), (6, "hold")]
        assert ds[2]["history"] == P.H_NOT_HELPING(N)
        assert g["level"] == 2 and g["not_helping"] is True and len(g["changes"]) == 2
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, OWNER_REPLAY[:5] + [{"minutes": 300.0}] + OWNER_REPLAY[-1:], "strict")
    assert [(d["rule"], d["action"]) for d in ds] == [(9, "ease"), (9, "ease"), (9, "ease")] and g["level"] == 3


def test_truly_unstable_overclock_ends_lower_within_three_sessions():
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE[:3], "balanced")
    assert [(d["rule"], d["action"]) for d in ds] == [(4, "warning"), (9, "ease"), (4, "warning")]
    assert ds[1]["history"] == P.EASED(N, 0, 1) and ds[1]["toast"] == P.T_EASED(N, 1)
    assert g["level"] == 1 and P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (338, 1669)
    assert g["changes"][0]["hours_at_from_level"] == 4.0

    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE[:3], "strict")
    assert [d["action"] for d in ds] == ["ease", "ease", "ease"] and g["level"] == 3

    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE[:3], "strict", errors_with_no_overclock=True)
    assert [d["action"] for d in ds] == ["ease", "ease", "hold"] and g["level"] == 2

    g = P.new_game(EXE, NAME, T0)
    assert [d["action"] for d in play(g, UNSTABLE[:3], "watch")] == ["warning", "would_ease", "would_ease"]
    assert g["level"] == 0 and g["changes"] == []


def test_truly_unstable_overclock_all_the_way_down():
    """Balanced, eight sessions: a warning then a step, at every level, and at the end the plain statement that
    the overclock is probably not the cause."""
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE, "balanced")
    assert [(d["rule"], d["action"], d["to_level"]) for d in ds] == [
        (4, "warning", 0), (9, "ease", 1), (4, "warning", 1), (9, "ease", 2),
        (4, "warning", 2), (9, "ease", 3), (4, "warning", 3), (5, "hold", 3)]
    assert g["level"] == 3 and g["problem_with_no_overclock"] is True and ds[-1]["history"] == P.H_NONE(N)
    assert [c["hours_at_from_level"] for c in g["changes"]] == [4.0, 4.0, 4.0]
    # with errors also logged with no overclock on the card it never goes below 50% by itself
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE, "balanced", errors_with_no_overclock=True)
    assert [d["rule"] for d in ds] == [4, 9, 4, 9, 4, 7, 7, 7] and g["level"] == 2


def test_truly_unstable_overclock_in_long_sessions():
    g = P.new_game(EXE, NAME, T0)
    ds = play(g, UNSTABLE_LONG[:1], "balanced")
    # first error: warning; second, 2 hours later: eased; 3 minutes after the change: ignored; later: warning
    assert [(d["rule"], d["action"]) for d in ds] == [(4, "warning"), (9, "ease"), (1, "ignore"), (3, "warning")]
    assert g["level"] == 1 and ds[3]["history"] == P.W_ALREADY(N)
    assert g["changes"][0]["hours_at_from_level"] == round(242.0 / 60.0, 4)
    assert g["minutes"] == 360.0 and g["minutes_at_level"] == 118.0          # 360 minutes, 242 of them at full
    assert g["incidents"] == [{"ts": T0 + 355 * 60.0, "play_minutes": 357.0, "level": 1, "ended_game": False}]
    # next session: it pairs with that one, and 4 hours at 75% is not fewer than half of the 4.03 at full: eased
    ds = play(g, UNSTABLE_LONG[1:], "balanced")
    assert [(d["rule"], d["action"], d["to_level"]) for d in ds] == [(9, "ease", 2)]
    assert g["level"] == 2 and g["not_helping"] is False and ds[0]["history"] == P.EASED(N, 1, 2)


# A game with a long record at full whose overclock turns unstable (a new driver, or the saved overclock raised):
# a graphics driver error closes it every 40 to 120 play minutes.
UNSTABLE_LATE = [{"minutes": float(m), "incidents": [(m - 0.5, True)]}
                 for m in (40, 120, 75, 90, 60, 110, 45, 100, 80, 55, 115, 70)]


def _record_at_full(hours):
    g = P.new_game(EXE, NAME, T0)
    g["minutes"] = g["minutes_at_level"] = hours * 60.0
    return g


def test_a_long_record_at_full_does_not_stop_easing_at_75():
    """Rule 6 counts at most 10 hours at the level above, so 60 or 200 hours at full on record do not keep an
    unstable game at 75% for good: it reaches 50% after a few holds, as a new game does."""
    for hours in (60.0, 200.0):
        g = _record_at_full(hours)
        ds = play(g, UNSTABLE_LATE[:6], "balanced")
        assert [(d["rule"], d["action"]) for d in ds] == [
            (4, "warning"), (9, "ease"), (4, "warning"), (6, "hold"), (6, "hold"), (9, "ease")], hours
        assert g["level"] == 2 and P.offsets_for(LIMIT, 2, g["limit_at_level"]) == (225, 1113)
        assert [d["history"] for d in ds if d["rule"] == 6] == [P.H_NOT_HELPING(N)] * 2
        assert g["changes"][-1]["hours_at_from_level"] == round(335.0 / 60.0, 4)        # 75 + 90 + 60 + 110 min

        g = _record_at_full(hours)
        ds = play(g, UNSTABLE_LATE[:5], "strict")
        assert [(d["rule"], d["action"]) for d in ds] == [
            (9, "ease"), (6, "hold"), (6, "hold"), (6, "hold"), (9, "ease")], hours
        assert g["level"] == 2

        g = _record_at_full(hours)
        ds = play(g, UNSTABLE_LATE, "watch")
        assert [d["action"] for d in ds] == ["warning"] + ["would_ease"] * 11 and g["level"] == 0

    # the saved overclock raised at full after 200 hours of the old one: the hours keep counting, and it still
    # reaches 50% of the new overclock
    g = _record_at_full(200.0)
    play(g, [{"minutes": 600.0}], "balanced", limit=[200, 1000])
    ds = play(g, UNSTABLE_LATE[:6], "balanced", limit=LIMIT)
    assert [d["action"] for d in ds] == ["warning", "ease", "warning", "hold", "hold", "ease"] and g["level"] == 2
    assert g["limit_at_level"] == [450, 2225] and P.offsets_for(LIMIT, g["level"], g["limit_at_level"]) == (225, 1113)


# The owner's 30 September spell of the driver (critique.md): 8 errors in four bursts starting 0, 11 min 8 s,
# 21 min 9 s and 40 min 2 s after the first, logged with no game running.  Here replayed as if a game at full
# with 50 hours on record had been open and survived them.
SPELL_30_SEP = [(60.0 + s / 60.0, False) for s in (0, 11 * 60 + 8, 21 * 60 + 9, 40 * 60 + 2)]


def test_one_spell_of_the_driver_is_one_piece_of_evidence():
    for sensitivity in ("balanced", "watch"):
        g = _record_at_full(50.0)
        ds = play(g, [{"minutes": 180.0, "incidents": SPELL_30_SEP}], sensitivity)
        assert [(d["rule"], d["action"]) for d in ds] == [(4, "warning")] * 4, sensitivity
        assert [d["history"] for d in ds] == [P.W_SURVIVED(N)] + [P.W_SAME_SPELL(N)] * 3
        assert [d["toast"] for d in ds] == [None] * 4, "no toast for a spell, not even in watch mode"
        assert g["level"] == 0 and len(g["incidents"]) == 4, "each is counted"
        assert P.row(g, LIMIT)["warning_open"] is True
        # a later error, more than an hour of play after the first, pairs with it
        ds = play(g, [{"minutes": 30.0, "incidents": [(20.0, False)]}], sensitivity)
        assert [d["action"] for d in ds] == (["ease"] if sensitivity == "balanced" else ["would_ease"])
    # strict is untouched: the first error eases, the rest of the spell is the reset storm or "already eased"
    g = _record_at_full(50.0)
    ds = play(g, [{"minutes": 180.0, "incidents": SPELL_30_SEP}], "strict")
    assert [(d["rule"], d["action"]) for d in ds] == [(9, "ease"), (1, "ignore"), (3, "warning"), (3, "warning")]
    # the owner's 23 September spell (longest pause under 4 minutes) is one incident, as before
    assert len(P.merge_bursts([0.0, 234.0, 272.0, 289.0, 299.0])) == 1
    # an error that closed the game still pairs at once: crash, relaunch, error again 10 minutes in
    g = _record_at_full(50.0)
    ds = play(g, [{"minutes": 30.0, "incidents": [(29.5, True)]}, {"minutes": 12.0, "incidents": [(10.0, False)]}],
              "balanced")
    assert [(d["rule"], d["action"]) for d in ds] == [(4, "warning"), (9, "ease")] and g["level"] == 1


def test_nothing_but_the_user_ever_moves_a_game_up():
    """A seeded run of errors, user steps and saved-overclock edits: a decision lowers by one step at most, never
    raises a clock, and nothing ever goes above the saved value."""
    rnd = random.Random(20261113)
    for _run in range(60):
        g = P.new_game(EXE, NAME, T0)
        limit = [450, 2225]
        now = T0
        for _ in range(120):
            now += rnd.choice((30.0, 400.0, 5000.0))
            P.add_play(g, rnd.choice((0.0, 3.0, 45.0, 200.0, 700.0)), now, sessions=1)
            roll = rnd.random()
            if roll < 0.10:
                limit = [rnd.choice((-50, 0, 200, 450, 700)), rnd.choice((0, 1000, 2225, 3000))]
            elif roll < 0.25:
                P.step(g, rnd.choice(("faster", "slower", "full")), now, limit,
                       play_minutes_now=g["minutes"] + rnd.choice((0.0, 20.0)))
            elif roll < 0.30:
                P.add_plain_crash(g, now, g["minutes"])
            else:
                ctx = C(sensitivity=rnd.choice(P.SENSITIVITIES), ended_game=rnd.random() < 0.5,
                        play_minutes_now=g["minutes"] + rnd.choice((0.0, 5.0, 90.0)),
                        stepped_this_session=rnd.random() < 0.15,
                        since_last_change_s=rnd.choice((None, None, None, 30.0, 4000.0)),
                        other_game_running=rnd.random() < 0.1,
                        errors_with_no_overclock=rnd.random() < 0.4, limit=limit)
                level, had = g["level"], P.offsets_for(limit, g["level"], g["limit_at_level"])
                d = P.decide(g, {"first_ts": now, "last_ts": now, "count": 1}, ctx)
                P.apply(g, d, now)
                got = P.offsets_for(limit, g["level"], g["limit_at_level"])
                assert g["level"] - level == (1 if d["action"] == "ease" else 0), d
                assert d["to_level"] == g["level"] <= 3
                assert all(a <= b for a, b in zip(got, had)), (had, got, d["action"])
                if ctx["sensitivity"] == "watch" or d["action"] != "ease":
                    assert got == had and g["level"] == level
                if d["action"] == "ease":
                    assert d["toast"] and d["history"] and g["changes"][-1]["by"] == "driver"
            for got, saved in zip(P.offsets_for(limit, g["level"], g["limit_at_level"]), limit):
                assert (got == saved) if saved <= 0 else (got <= saved), (limit, g["level"], g["limit_at_level"])
            assert set(g) == RECORD_KEYS and 0 <= g["level"] <= 3
            assert len(g["incidents"]) <= 20 and len(g["changes"]) <= 30 and len(g["plain_crashes"]) <= 10
            assert all(i["level"] == g["level"] for i in g["incidents"])
            r = P.row(g, limit, errors_with_no_overclock=False)
            if r["warning_open"]:
                assert g["last_problem"]["kind"] == "warning" and g["level"] < 3 and g["incidents"]
                if all(i["play_minutes"] <= g["minutes"] for i in g["incidents"]):
                    # the promise the page makes is true: one more right now would step the game down
                    nxt = {"first_ts": now + 1.0, "last_ts": now + 1.0, "count": 1}
                    for sens, action in (("balanced", "ease"), ("watch", "would_ease"), ("strict", "ease")):
                        ctx = C(sensitivity=sens, play_minutes_now=g["minutes"], limit=limit)
                        assert P.decide(copy.deepcopy(g), nxt, ctx)["action"] == action, (sens, g)
            assert r["eased_by_driver"] == (g["level"] > 0 and g["changes"][-1]["by"] == "driver")
        assert json.loads(json.dumps(g)) == g
        assert set(P.row(g, limit)) == ROW_KEYS
