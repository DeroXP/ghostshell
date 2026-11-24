"""Adaptive Tuning's rules, with nothing attached to them.

Every function here works on the plain dicts and numbers its caller passes in.
There are no files, no clocks, no time calls, no card and no event log in this
module; core/adaptive_tuning.py (the engine) owns all of those and asks this
module three kinds of question:

  * what to put on the card     offsets_for(limit, level, limit_at_level)
  * what a driver error means   merge_bursts(events), decide(game, incident, ctx),
                                apply(game, decision, now)
  * what the user is told       row(game, limit) and the text functions (W_ENDED,
                                EASED, T_EASED, ...), which return the exact
                                sentences of the spec

A game runs at its saved overclock (the "limit") scaled by its level:
0 Full, 1 "75%", 2 "50%", 3 "No overclock".  A level only ever goes down by
itself, one step at a time, on repeated graphics driver errors; only the user
moves a game up (step()).

Play time.  The rules compare play minutes, not wall-clock time.  The engine
passes ctx["play_minutes_now"] = game["minutes"] plus whatever the open session
has played that is not in the record yet, and it always adds play time to
"minutes" and "minutes_at_level" together (add_play() does exactly that).  When
a level changes while such unrecorded time exists, "minutes_at_level" restarts
at minus that time, so that it reads exactly zero at the moment of the change
once the session's time is added.  row() never shows a negative number.

Whenever a rule left a choice open, this module takes the one that changes the
user's overclock less.
"""
from __future__ import annotations

# ── levels ────────────────────────────────────────────────────────────
LEVELS = (1.0, 0.75, 0.5, 0.0)
LEVEL_NAMES = ("Full", "75%", "50%", "No overclock")
LEVEL_PERCENT = (100, 75, 50, 0)                      # LEVELS as whole numbers: the scaling is done in integers
LEVEL_PHRASES = ("full overclock", "75% of your overclock", "50% of your overclock", "no overclock")
MAX_LEVEL = len(LEVELS) - 1

TOAST_TITLE = "Adaptive Tuning"
SENSITIVITIES = ("balanced", "strict", "watch")

# ── the numbers in the rules ──────────────────────────────────────────
BURST_GAP_S = 300.0            # events less than 5 minutes apart are one incident
RESET_STORM_S = 600.0          # errors this soon after the session's own level change are ignored
PAIR_WINDOW_MIN = 600.0        # "within 10 hours of play"
SPELL_MIN = 60.0               # one spell of the driver: an error the game survived pairs only with one this much later
STEADY_MIN = 1200.0            # "steady for 20 hours at this level"
PLAIN_CRASH_HINT_COUNT = 3
MAX_INCIDENTS = 20
MAX_CHANGES = 30
MAX_PLAIN_CRASHES = 10

_INF = float("inf")


# ── small helpers ─────────────────────────────────────────────────────
def _num(value, default=0.0):
    """A finite float, or `default` for anything that is not one."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if x != x or x in (_INF, -_INF):
        return default
    return x


def _int(value) -> int:
    """A whole number, rounded down, so a clock value is never read as higher than it is."""
    x = _num(value, 0.0)
    whole = int(x)
    return whole - 1 if x < whole else whole


def _level(value) -> int:
    """A level index inside 0..3.  Anything unreadable is Full: the overclock the user saved by hand."""
    if isinstance(value, bool):
        return 0
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_LEVEL, level))


def _name(game) -> str:
    if isinstance(game, dict):
        return str(game.get("name") or game.get("exe") or "This game")
    return "This game"


def _list(game, key) -> list:
    """The record's list under `key`, holding only its dict entries (a damaged record must not raise)."""
    value = game.get(key) if isinstance(game, dict) else None
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _append(game, key, entry, keep) -> None:
    items = _list(game, key)
    items.append(entry)
    game[key] = items[-keep:]


def _pair(limit) -> tuple:
    """(core, mem) from a saved profile dict, get_state's limit dict, or a [core, mem, ...] list."""
    if isinstance(limit, dict):
        core = limit.get("core_offset_mhz", limit.get("core", 0))
        mem = limit.get("mem_offset_mhz", limit.get("mem", 0))
        return (_int(core), _int(mem))
    if isinstance(limit, (list, tuple)) and len(limit) >= 2:
        return (_int(limit[0]), _int(limit[1]))
    return (0, 0)


def _has_limit(limit) -> bool:
    """Was a saved overclock actually passed in (not None, not "no profile", not 0 / 0)?"""
    if isinstance(limit, dict):
        if limit.get("exists", True) is False or limit.get("has_profile", True) is False:
            return False
    elif not (isinstance(limit, (list, tuple)) and len(limit) >= 2):
        return False
    return _pair(limit) != (0, 0)


def _old_pair(limit_at_level):
    """The record's limit_at_level as (core, mem), or None when it holds nothing usable."""
    if isinstance(limit_at_level, (list, tuple)) and len(limit_at_level) >= 2:
        return _pair(limit_at_level)
    return None


def _scale(value: int, level: int) -> int:
    """A positive clock value at a level, rounded half up.  Zero and negative values pass through unchanged."""
    if value <= 0:
        return value
    return (value * LEVEL_PERCENT[level] * 2 + 100) // 200


# ── levels in words ───────────────────────────────────────────────────
def level_name(level) -> str:
    return LEVEL_NAMES[_level(level)]


def level_percent(level) -> int:
    return LEVEL_PERCENT[_level(level)]


def level_phrase(level) -> str:
    return LEVEL_PHRASES[_level(level)]


# ── what goes on the card ─────────────────────────────────────────────
def limit_in_force(limit, level, limit_at_level=None) -> tuple:
    """The (core, mem) a game's level is taken from.

    That is the saved overclock, except for a game below Full whose record
    remembers the overclock it was eased against: there each positive part is
    the smaller of the two, so raising the saved overclock never lifts an eased
    game by itself.  A part saved as zero or negative is used as saved."""
    now = _pair(limit)
    old = _old_pair(limit_at_level)
    if old is None or _level(level) == 0:
        return now
    return tuple(v if v <= 0 else min(v, o) for v, o in zip(now, old))


def offsets_for(limit, level, limit_at_level=None) -> tuple:
    """(core, mem) in MHz for a game at `level` of the saved overclock `limit`.

    Positive parts are scaled (75% of 2225 is 1669, 50% is 1113) and are never
    above the saved value; negative parts are applied unchanged at every level.
    With `limit_at_level` and a level below Full, each positive part is the
    smaller of the two scalings."""
    level = _level(level)
    return tuple(_scale(v, level) for v in limit_in_force(limit, level, limit_at_level))


# ── the game record ───────────────────────────────────────────────────
def new_game(exe, name, now) -> dict:
    return {
        "exe": exe,
        "name": name or exe,
        "enabled": True,
        "level": 0,
        "sessions": 0,
        "minutes": 0.0,
        "minutes_at_level": 0.0,
        "last_played": now,
        "incidents": [],                    # {"ts", "play_minutes", "level", "ended_game"}, counted ones only
        "changes": [],                      # {"ts", "from_level", "to_level", "by", "hours_at_from_level", "text"}
        "plain_crashes": [],                # {"ts", "play_minutes"}
        "not_helping": False,
        "problem_with_no_overclock": False,
        "limit_at_level": None,
        "last_problem": None,               # {"ts", "kind", "text"}
    }


def add_play(game, minutes, now=None, sessions=0) -> None:
    """Add play time to the record: the same amount to "minutes" and "minutes_at_level".

    The engine calls this when a session ends (sessions=1) or as it goes; it is
    the only way play time should reach a record (see the module docstring)."""
    add = max(0.0, _num(minutes))
    game["minutes"] = _num(game.get("minutes")) + add
    game["minutes_at_level"] = _num(game.get("minutes_at_level")) + add
    if sessions:
        game["sessions"] = _int(game.get("sessions")) + (1 if sessions is True else max(0, _int(sessions)))
    if now is not None:
        game["last_played"] = now


def _session_part(game, play_minutes_now) -> float:
    """Play minutes of the open session that are not in the record yet."""
    if play_minutes_now is None:
        return 0.0
    return max(0.0, _num(play_minutes_now) - _num(game.get("minutes")))


def minutes_at_level_now(game, play_minutes_now=None) -> float:
    """Play minutes at the current level, counting the open session up to `play_minutes_now`."""
    return max(0.0, _num(game.get("minutes_at_level")) + _session_part(game, play_minutes_now))


def failures_at(game, level, since=None) -> int:
    """How many times the graphics driver took this game down from `level` (optionally since a time).

    Only changes made on driver evidence count; a step down the user asked for is not a failure."""
    level = _level(level)
    count = 0
    for change in _list(game, "changes"):
        if change.get("by") != "driver" or _level(change.get("from_level")) != level:
            continue
        if _level(change.get("to_level")) <= level:
            continue
        if since is not None and _num(change.get("ts"), -_INF) < _num(since, -_INF):
            continue
        count += 1
    return count


# ── driver errors into incidents ──────────────────────────────────────
def merge_bursts(events) -> list:
    """Events into incidents [{"first_ts", "last_ts", "count"}], oldest first.

    An event less than 5 minutes after the previous one extends the same
    incident.  Items may be events ({"ts", ...}), bare times, or incidents made
    earlier (so pending incidents can be merged with new events).  The same
    log record (channel and record id) given twice is counted once."""
    spans = []
    seen = set()
    for item in events or ():
        count = 1
        if isinstance(item, dict):
            if "first_ts" in item or "last_ts" in item:
                first = _num(item.get("first_ts", item.get("last_ts")), None)
                last = _num(item.get("last_ts", item.get("first_ts")), None)
                count = max(1, _int(item.get("count", 1)))
            else:
                first = last = _num(item.get("ts"), None)
                if item.get("record_id") is not None:
                    key = (item.get("channel"), item.get("record_id"))
                    if key in seen:
                        continue
                    seen.add(key)
        else:
            first = last = _num(item, None)
        if first is None or last is None:
            continue
        if last < first:
            first, last = last, first
        spans.append((first, last, count))
    spans.sort()
    out = []
    for first, last, count in spans:
        if out and first - out[-1]["last_ts"] < BURST_GAP_S:
            out[-1]["last_ts"] = max(out[-1]["last_ts"], last)
            out[-1]["count"] += count
        else:
            out.append({"first_ts": first, "last_ts": last, "count": count})
    return out


def _incident_ts(incident):
    if isinstance(incident, dict):
        for key in ("first_ts", "ts", "last_ts"):
            ts = _num(incident.get(key), None)
            if ts is not None:
                return ts
        return None
    return _num(incident, None)


# ── the decision ──────────────────────────────────────────────────────
def _same_spell(inc, gap) -> bool:
    """An earlier incident the game survived, less than SPELL_MIN play minutes before this one: the same spell of
    the driver, so it is no second piece of evidence.  One that closed the game pairs at any distance."""
    return 0.0 <= gap < SPELL_MIN and inc.get("ended_game") is not True


def _paired(game, level, now_min) -> bool:
    """A counted incident at this level within 10 hours of play before this one, and not of the same spell."""
    for inc in _list(game, "incidents"):
        if _level(inc.get("level")) != level:
            continue
        gap = now_min - _num(inc.get("play_minutes"), _INF)
        if 0.0 <= gap <= PAIR_WINDOW_MIN and not _same_spell(inc, gap):
            return True
    return False


def _would_pair(game, level, now_min) -> bool:
    """Would an error at `now_min` pair with a counted incident at this level (rule 4)?  As _paired(), except that
    a record that lags the incident (the session that logged it is still open) counts as "right after it"."""
    for inc in _list(game, "incidents"):
        if _level(inc.get("level")) != level:
            continue
        mark = _num(inc.get("play_minutes"), None)
        if mark is None:
            continue
        gap = max(0.0, now_min - mark)
        if gap <= PAIR_WINDOW_MIN and not _same_spell(inc, gap):
            return True
    return False


def _in_a_spell(game, level, now_min) -> bool:
    """A counted incident at this level that this one is part of the same spell with (see _same_spell)."""
    for inc in _list(game, "incidents"):
        if _level(inc.get("level")) != level:
            continue
        if _same_spell(inc, now_min - _num(inc.get("play_minutes"), _INF)):
            return True
    return False


def _retried(game, level, at_level_min) -> bool:
    """The user moved this game up to this level within the last 10 hours of play (no free warning).

    Only while the game is still at the level the user moved it to: after any
    later change the new level gets its first warning again."""
    changes = _list(game, "changes")
    if not changes:
        return False
    last = changes[-1]
    if last.get("by") != "user" or _level(last.get("to_level")) != level:
        return False
    return _level(last.get("from_level")) > level and at_level_min <= PAIR_WINDOW_MIN


def _eased_in_vain(game, level, at_level_min) -> bool:
    """The game came to this level one step down from the level above and has played here for less than half the
    hours it played up there before that change, those hours counted at most PAIR_WINDOW_MIN (10 h).

    So the hold lasts at most the first 5 hours of play at the eased level, whatever the game's history: a long
    record at the level above says nothing about whether easing helps."""
    if level == 0:
        return False
    changes = _list(game, "changes")
    if not changes:
        return False
    last = changes[-1]
    if _level(last.get("to_level")) != level or _level(last.get("from_level")) != level - 1:
        return False
    before = _num(last.get("hours_at_from_level"), None)
    if before is None:
        return False
    before = min(max(before, 0.0), PAIR_WINDOW_MIN / 60.0)
    return round(at_level_min / 60.0, 4) < round(before / 2.0, 4)


def decide(game, incident, ctx) -> dict:
    """What one graphics driver incident means for this game.  Changes nothing: apply() does.

    ctx: "sensitivity" ("balanced" | "strict" | "watch"), "ended_game",
    "play_minutes_now", "stepped_this_session", "since_last_change_s",
    "other_game_running", "errors_with_no_overclock"; and "limit", the saved
    overclock as read at this moment (needed so an ease can remember what it
    was eased against).

    Returns {"action", "to_level", "history", "toast", "problem"} plus what
    apply() needs ("counted", "flag", "incident", "from_level", "play_minutes",
    "hours_at_from_level", "limit", "would_be_level", "rule").
    "to_level" is the level the game is at once the decision is applied, so it
    differs from the current level only when "action" is "ease".  An "ignore"
    has an empty "history" and no "problem": add no line for it."""
    game = game if isinstance(game, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    name = _name(game)
    level = _level(game.get("level"))
    sensitivity = ctx.get("sensitivity")
    if sensitivity not in SENSITIVITIES:
        sensitivity = "balanced"
    ended = bool(ctx.get("ended_game"))
    now_min = _num(ctx.get("play_minutes_now"), _num(game.get("minutes")))
    at_level_min = minutes_at_level_now(game, now_min)

    def result(rule, action, text="", toast=None, counted=False, flag=None, to_level=level, **more):
        out = {
            "action": action,
            "to_level": to_level,
            "history": text,
            "toast": toast,
            "problem": {"kind": action, "text": text} if text else None,
            "rule": rule,
            "counted": counted,
            "flag": flag,
            "from_level": level,
            "would_be_level": level,
            "play_minutes": now_min,
            "incident": {"ts": _incident_ts(incident), "play_minutes": now_min, "level": level,
                         "ended_game": ended},
        }
        out.update(more)
        return out

    # 1. A reset storm right after this session's own level change: not evidence about the new level.
    since = _num(ctx.get("since_last_change_s"), None)
    if since is not None and since < RESET_STORM_S:
        return result(1, "ignore")

    # 2. Two games at once: the error cannot be pinned on this one.
    if ctx.get("other_game_running"):
        return result(2, "warning", W_OTHER(name))

    # 3. At most one step per session.  It still counts, so it can pair with a later one at the new level.
    if ctx.get("stepped_this_session"):
        return result(3, "warning", W_ALREADY(name), counted=True)

    # 4. Does it qualify?  "watch" uses the recommended (balanced) test: it reports what that would have done.
    #    An error less than SPELL_MIN after one the game survived is the same spell: counted, never a pair with it.
    paired = _paired(game, level, now_min)
    retried = _retried(game, level, at_level_min)
    if not (sensitivity == "strict" or paired or retried):
        if _in_a_spell(game, level, now_min):
            return result(4, "warning", W_SAME_SPELL(name), counted=True)
        return result(4, "warning", W_ENDED(name) if ended else W_SURVIVED(name), counted=True)

    # 5. Nothing left to ease.
    if level >= MAX_LEVEL:
        return result(5, "hold", H_NONE(name), counted=True, flag="problem_with_no_overclock")

    # 6. Easing did not make it steadier.
    if _eased_in_vain(game, level, at_level_min):
        return result(6, "hold", H_NOT_HELPING(name), counted=True, flag="not_helping")

    # 7. Never below 50% by itself while the driver also logs errors with no overclock on the card.
    if level == 2 and ctx.get("errors_with_no_overclock"):
        return result(7, "hold", H_FLOOR(name), counted=True)

    # 8. Only tell.
    if sensitivity == "watch":
        text = WATCH(name, level + 1)
        return result(8, "would_ease", text, toast=text, counted=True, would_be_level=level + 1)

    # 9. One step down.
    if sensitivity == "balanced" and paired:
        text = EASED(name, level, level + 1)
    else:
        text = EASED_STRICT(name, level, level + 1)
    limit = ctx.get("limit")
    return result(9, "ease", text, toast=T_EASED(name, level + 1), to_level=level + 1, would_be_level=level + 1,
                  hours_at_from_level=round(at_level_min / 60.0, 4),
                  limit=list(_pair(limit)) if _has_limit(limit) else None)


def apply(game, decision, now, limit=None) -> None:
    """Carry a decision into the record.  `limit` (the saved overclock) may be passed here instead of in ctx.

    A stale "ease" (the level moved since decide(), or it is not exactly one
    step down) changes nothing.  Take the level to write from game["level"]
    afterwards."""
    if not isinstance(decision, dict):
        return
    action = decision.get("action")
    if action not in ("warning", "ease", "hold", "would_ease"):
        return
    level = _level(game.get("level"))
    incident = decision.get("incident") if isinstance(decision.get("incident"), dict) else {}
    ts = incident.get("ts")
    if ts is None:
        ts = now
    text = decision.get("history") or ""

    if action == "ease":
        to_level = decision.get("to_level")
        if isinstance(to_level, bool) or not isinstance(to_level, int):
            return
        if level >= MAX_LEVEL or to_level != level + 1 or _level(decision.get("from_level", level)) != level:
            return
        now_min = decision.get("play_minutes")
        hours = _num(decision.get("hours_at_from_level"), None)
        if hours is None:
            hours = round(minutes_at_level_now(game, now_min) / 60.0, 4)
        use = limit if limit is not None else decision.get("limit")
        if _has_limit(use):
            game["limit_at_level"] = list(limit_in_force(use, level, game.get("limit_at_level")))
        _append(game, "changes", {"ts": now, "from_level": level, "to_level": to_level, "by": "driver",
                                  "hours_at_from_level": hours, "text": text}, MAX_CHANGES)
        game["level"] = to_level
        game["minutes_at_level"] = 0.0 - _session_part(game, now_min)
        game["incidents"] = []
    else:
        if decision.get("counted"):
            _append(game, "incidents", {
                "ts": ts,
                "play_minutes": _num(incident.get("play_minutes"), _num(game.get("minutes"))),
                "level": _level(incident.get("level", level)),
                "ended_game": bool(incident.get("ended_game")),
            }, MAX_INCIDENTS)
        if decision.get("flag") in ("not_helping", "problem_with_no_overclock"):
            game[decision["flag"]] = True

    problem = decision.get("problem")
    if isinstance(problem, dict) and problem.get("text"):
        game["last_problem"] = {"ts": ts, "kind": problem.get("kind") or action, "text": problem["text"]}


# ── what the user asks for ────────────────────────────────────────────
def step(game, to, now, limit, play_minutes_now=None) -> dict:
    """A level change the user asked for: "faster" (one level up), "full", or "slower" (one level down).

    Returns {"ok", "err", "history"} (plus "from_level" and "to_level" when ok).
    Pass `play_minutes_now` (as in ctx) when a session is open for this game.
    Going up or to Full anchors the game to the saved overclock as it is now;
    going down keeps the smaller of that and what the game was eased against,
    so "slower" can never put more on the card than the game had.  The user's
    change becomes the game's last problem line (kind "user"), so the row never
    shows an older sentence that the change made untrue."""
    name = _name(game)
    level = _level(game.get("level"))
    if to == "faster":
        new = level - 1
    elif to == "full":
        new = 0
    elif to == "slower":
        new = level + 1
    else:
        return {"ok": False, "err": "That step is not possible.", "history": ""}
    if to in ("faster", "full") and level == 0:
        return {"ok": False, "err": f"{name} already runs at full overclock.", "history": ""}
    if to == "slower" and level >= MAX_LEVEL:
        return {"ok": False, "err": f"{name} already runs with no overclock.", "history": ""}

    text = USER_SET(name, new)
    hours = round(minutes_at_level_now(game, play_minutes_now) / 60.0, 4)
    old = game.get("limit_at_level")
    if new == 0:
        game["limit_at_level"] = None
    elif not _has_limit(limit):
        game["limit_at_level"] = old if (new > level and _old_pair(old) is not None) else None
    elif new > level:
        game["limit_at_level"] = list(limit_in_force(limit, level, old))
    else:
        game["limit_at_level"] = list(_pair(limit))
    _append(game, "changes", {"ts": now, "from_level": level, "to_level": new, "by": "user",
                              "hours_at_from_level": hours, "text": text}, MAX_CHANGES)
    game["level"] = new
    game["minutes_at_level"] = 0.0 - _session_part(game, play_minutes_now)
    game["incidents"] = []
    game["not_helping"] = False
    game["problem_with_no_overclock"] = False
    game["last_problem"] = {"ts": now, "kind": "user", "text": text}
    return {"ok": True, "err": "", "history": text, "from_level": level, "to_level": new}


# ── crashes without a driver error ────────────────────────────────────
def add_plain_crash(game, now, play_minutes) -> None:
    """The game closed unexpectedly and no graphics driver error was logged.  Never changes a level."""
    _append(game, "plain_crashes", {"ts": now, "play_minutes": _num(play_minutes, _num(game.get("minutes")))},
            MAX_PLAIN_CRASHES)
    game["last_problem"] = {"ts": now, "kind": "plain_crash", "text": PLAIN_CRASH(_name(game))}


def plain_crash_hint(game) -> bool:
    """The last three plain crashes fell within 10 hours of play of each other.

    Only crashes since the game came to its current level count (after a step
    the question starts again), and the hint goes away once the game has
    played 10 hours without another one."""
    changes = _list(game, "changes")
    since = _num(changes[-1].get("ts"), None) if changes else None
    marks = []
    for crash in _list(game, "plain_crashes"):
        if since is not None and _num(crash.get("ts"), -_INF) <= since:
            continue
        mark = _num(crash.get("play_minutes"), None)
        if mark is not None:
            marks.append(mark)
    if len(marks) < PLAIN_CRASH_HINT_COUNT:
        return False
    marks.sort()
    first, last = marks[-PLAIN_CRASH_HINT_COUNT], marks[-1]
    return last - first <= PAIR_WINDOW_MIN and _num(game.get("minutes")) - last <= PAIR_WINDOW_MIN


# ── hints for the row ─────────────────────────────────────────────────
def best_left_here(game) -> bool:
    """The level above failed twice or more."""
    level = _level(game.get("level"))
    return level > 0 and failures_at(game, level - 1) >= 2


def suggest_step_up(game) -> bool:
    """Below Full, 20 hours of play at this level since the last counted incident, and the level above
    failed fewer than two times."""
    level = _level(game.get("level"))
    if level == 0 or _num(game.get("minutes_at_level")) < STEADY_MIN:
        return False
    minutes = _num(game.get("minutes"))
    for inc in _list(game, "incidents"):
        if minutes - _num(inc.get("play_minutes"), _INF) < STEADY_MIN:
            return False
    return failures_at(game, level - 1) < 2


def eased_by_driver(game) -> bool:
    """Below Full, and the latest change of level was made on graphics driver evidence (not by the user)."""
    level = _level(game.get("level"))
    changes = _list(game, "changes")
    if level == 0 or not changes:
        return False
    last = changes[-1]
    return last.get("by") == "driver" and _level(last.get("to_level")) == level


def warning_open(game, play_minutes_now=None, errors_with_no_overclock=None) -> bool:
    """Is "one more within 10 hours of play and it will be eased" true for this game right now?

    True only when all of these hold:
      * the game's last problem line is a warning about a counted incident at
        its current level (the same ts), with no level change after it;
      * that incident lies within PAIR_WINDOW_MIN play minutes of the game's
        play time now (`play_minutes_now` counts the open session, as in ctx);
      * an error right now would pair with a counted incident (rule 4): not
        in the first SPELL_MIN play minutes after an error the game survived,
        when one more would only be the same spell of the driver;
      * the next qualifying error would step the game down (or, in watch mode,
        be reported as a step down): not at no overclock (rule 5), not while
        easing is being held as not helping (rule 6, judged now), and not at
        50% unless the caller says the driver logs no errors with no overclock
        on the card (rule 7; None means not known, so no promise).
    Whether Adaptive Tuning and the game's own switch are on is the page's part."""
    if not isinstance(game, dict):
        return False
    level = _level(game.get("level"))
    if level >= MAX_LEVEL:
        return False
    if level == 2 and errors_with_no_overclock is not False:
        return False
    problem = game.get("last_problem")
    if not isinstance(problem, dict) or problem.get("kind") != "warning":
        return False
    problem_ts = _num(problem.get("ts"), None)
    if problem_ts is None:
        return False
    if _eased_in_vain(game, level, minutes_at_level_now(game, play_minutes_now)):
        return False
    changes = _list(game, "changes")
    changed_at = _num(changes[-1].get("ts"), None) if changes else None
    if changed_at is not None and problem_ts < changed_at:
        return False
    now_min = _num(play_minutes_now, _num(game.get("minutes")))
    for inc in _list(game, "incidents"):
        if _level(inc.get("level")) != level or _num(inc.get("ts"), None) != problem_ts:
            continue
        mark = _num(inc.get("play_minutes"), None)
        if mark is not None and now_min - mark <= PAIR_WINDOW_MIN:
            return _would_pair(game, level, now_min)
    return False


def row(game, limit, play_minutes_now=None, errors_with_no_overclock=None) -> dict:
    """The per-game part of get_state().

    `play_minutes_now` (the game's minutes plus its open session, as in ctx)
    and `errors_with_no_overclock` (as in ctx) are only used for
    "warning_open"; see warning_open() for what leaving them out means."""
    level = _level(game.get("level"))
    core, mem = offsets_for(limit, level, game.get("limit_at_level"))
    old = _old_pair(game.get("limit_at_level"))
    problem = game.get("last_problem")
    return {
        "exe": game.get("exe"),
        "name": _name(game),
        "enabled": bool(game.get("enabled", True)),
        "level": level,
        "level_name": LEVEL_NAMES[level],
        "percent": LEVEL_PERCENT[level],
        "core": core,
        "mem": mem,
        "sessions": _int(game.get("sessions")),
        "hours": round(max(0.0, _num(game.get("minutes"))) / 60.0, 1),
        "hours_at_level": round(max(0.0, _num(game.get("minutes_at_level"))) / 60.0, 1),
        "last_played": game.get("last_played"),
        "last_problem": dict(problem) if isinstance(problem, dict) else None,
        "not_helping": bool(game.get("not_helping")),
        "problem_with_no_overclock": bool(game.get("problem_with_no_overclock")),
        "limit_changed": level > 0 and old is not None and old != _pair(limit),
        "can_step_up": level > 0,
        "next_up_name": "" if level == 0 else LEVEL_NAMES[level - 1],
        "can_step_down": level < MAX_LEVEL,
        "suggest_step_up": suggest_step_up(game),
        "best_left_here": best_left_here(game),
        "plain_crash_hint": plain_crash_hint(game),
        "warning_open": warning_open(game, play_minutes_now, errors_with_no_overclock),
        "eased_by_driver": eased_by_driver(game),
    }


# ── texts ─────────────────────────────────────────────────────────────
# Each returns exactly the sentence in the spec.  `from_level` and `to_level` are level numbers.
# A warning carries no promise about the next error: the page adds that sentence only while row()["warning_open"].
def W_SURVIVED(name) -> str:
    return f"{name} had a graphics driver error but kept running. Nothing was changed."


def W_ENDED(name) -> str:
    return f"{name} closed after a graphics driver error. Nothing was changed."


def W_SAME_SPELL(name) -> str:
    return f"{name} had more graphics driver errors in the same spell. Nothing was changed."


def W_OTHER(name) -> str:
    return (f"The graphics driver logged an error while {name} and another game were both running, "
            "so nothing was changed.")


def W_ALREADY(name) -> str:
    return (f"{name} had another graphics driver error. It was already eased this session, "
            "so nothing more was changed.")


def EASED(name, from_level, to_level) -> str:
    return (f"{name} had graphics driver errors twice within 10 hours of play. "
            f"Eased from {level_phrase(from_level)} to {level_phrase(to_level)}.")


def EASED_STRICT(name, from_level, to_level) -> str:
    return f"{name} had a graphics driver error. Eased from {level_phrase(from_level)} to {level_phrase(to_level)}."


def T_EASED(name, to_level) -> str:
    """The toast (title TOAST_TITLE) for a level that went down."""
    how = "with no overclock" if _level(to_level) >= MAX_LEVEL else f"at {level_phrase(to_level)}"
    return f"{name} now runs {how} after graphics driver errors. You can undo this in Vispora under Profiles."


def H_NOT_HELPING(name) -> str:
    return (f"{name} had graphics driver errors again soon after it was eased, so easing does not seem to help. "
            "It was left where it is. You can ease it one more step yourself.")


def H_NONE(name) -> str:
    return (f"{name} had graphics driver errors with no overclock on the card, "
            "so the overclock is probably not the cause.")


def H_FLOOR(name) -> str:
    return (f"{name} had graphics driver errors again. The graphics driver also logs errors with no overclock "
            "on the card, so it stays at 50% of your overclock.")


def WATCH(name, to_level) -> str:
    return (f"{name}: Adaptive Tuning would have eased it to {level_phrase(to_level)} after graphics driver errors. "
            "It is set to only tell you, so nothing was changed.")


def PLAIN_CRASH(name) -> str:
    return f"{name} closed unexpectedly. No graphics driver error was logged, so the overclock was left alone."


def PC_STOPPED(name) -> str:
    return f"Your PC stopped while {name} was running. No graphics driver error was logged, so nothing was changed."


def HANDED_BACK(name) -> str:
    return f"Vispora stopped while {name} was running. The overclock was put back at the next start."


def USER_SET(name, to_level) -> str:
    return f"You set {name} to {level_phrase(to_level)}."


def HELD_HOT(name) -> str:
    return (f"The graphics card got too hot while {name} was running, "
            "so the overclock stayed off until the game closed.")


def HELD_MANUAL(name) -> str:
    return (f"You changed the overclock while {name} was running, "
            "so Adaptive Tuning left it alone until the game closed.")


def HELD_OTHER(name) -> str:
    return (f"Another program kept changing the overclock while {name} was running, "
            "so Adaptive Tuning left it alone until the game closed.")


def HELD_RELEASED(name) -> str:
    return (f"Game mode was turned off while {name} was running, "
            "so the overclock was kept where it was until the game closed.")


def LIMIT_CHANGED(name) -> str:
    return f"Your saved overclock changed since {name} was eased."


def REPORT_LINE(level, eased_to=None, held=None, trouble=None):
    """The after-game report line.

    `eased_to`: the level this session was eased to, if it was.  `held`: None,
    "manual", "hot" or "released".  The spec has no line for "other_program",
    so that returns None (the history line HELD_OTHER carries the message).
    `trouble`: None, "driver" (the session added a graphics driver error line)
    or "crash" (the game closed unexpectedly); any other value returns None
    rather than claim there were no problems.
    Precedence: eased_to, held, no overclock, trouble, then "no problems"."""
    if eased_to is not None:
        return f"Overclock eased to {level_phrase(eased_to)} after graphics driver errors"
    if held == "manual":
        return "Overclock: left alone after you changed it"
    if held == "hot":
        return "Overclock: off after the card got too hot"
    if held == "released":
        return "Overclock: kept until the game closed"
    if held:
        return None
    level = _level(level)
    if level >= MAX_LEVEL:
        return "No overclock for this game"
    part = ("full", "75%", "50%")[level]
    if trouble == "driver":
        return f"Overclock: {part}, a graphics driver error was noted"
    if trouble == "crash":
        return f"Overclock: {part}, the game closed unexpectedly"
    if trouble:
        return None
    return f"Overclock: {part}, no problems"
