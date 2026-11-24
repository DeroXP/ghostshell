"""Adaptive Tuning: games run on the saved overclock, eased per game when needed.

For the user: "Adaptive Tuning runs your games on your saved overclock and
eases it off, just for that game, if the graphics driver keeps having problems
there.  It never raises anything above what you saved."

The saved overclock profile is the limit.  A game runs at that limit scaled by
its level (0 Full, 1 75%, 2 50%, 3 No overclock).  A level only goes down by
itself, one step at a time, on repeated graphics driver errors, and only the
user moves it back up.  The rules live in core/adaptive_policy.py, which has no
files, clocks or card in it; this module is everything around them: the
session, the files, the card and the Windows event log.

What it promises (each has a test in tests/test_adaptive_tuning.py):

  I1  It never writes or deletes the saved overclock profile.  (The old tuner
      saved every exploratory step over the user's own profile and armed it to
      apply at the next start: the "beta.5" incident.  Nothing here has a way
      to touch the profile at all.)
  I2  Every clock write goes through _write() below.  During a session it only
      writes the level's share of the profile as read at that moment, so never
      above the saved value; the hand-back writes exactly what was recorded as
      on the card before the session.
  I3  Nothing is written outside an open session, except the hand-back at its
      end and at the next start after a run that died mid-game.  A watch
      thread whose session has ended writes nothing.
  I4  A level goes down by itself only on graphics driver evidence, at most one
      step per session, every change is recorded in plain words and one click
      undoes it.
  I5  With the switch off, an unsupported card or no saved overclock there are
      no session writes.
  I6  After a session the card reads back what it held before (or is left
      alone after the user took over), the drift watchdog runs again and the
      session file is gone (it is kept only when the hand-back could not be
      confirmed, so the next start finishes it).  A session that is let go of
      while its game still runs is not ended by raising the clocks under that
      game: it stays open, left alone, until the game has closed.
  I7  A driver error is counted once, and only for the session it happened in.
      (Also across a restart of the app: what was judged last is kept in the
      record.  A record Windows wrote before a session began is never that
      session's, whatever time it carries, so a clock that is set back does
      not bring an old error round again.  The errors of a test on the GPU
      page, and those logged as the PC wakes from sleep, are nobody's.)

Files, all in the app's data folder:

  adaptive_tuning_v2_settings.json  the switch, the notify switch, how soon to ease
  adaptive_tuning_v2_games.json     the games, what happened, each last session
  adaptive_tuning_v2_session.json   the open session: what was on the card
                                    before it, every value it wrote (noted
                                    BEFORE the card is written) and a
                                    heartbeat.  A run that dies mid-game leaves
                                    it behind, and the next start uses it to
                                    put the card back.

The old tuner's files (adaptive_tuning_settings.json, adaptive_tuning_state.json)
are only read, for the one-time note; its session file (adaptive_tuning_session.json,
3.6.8 and 3.7.0) is handed back once and removed.

A save that fails raises; a file that is there but cannot be read is never
taken for a missing one; a damaged one is set aside, and that is recorded.

Two locks.  The state lock is short: the files and the picture of the open
session that get_state() draws.  The session lock is held across everything
that starts or ends a session and every clock write, which can take seconds.
The session lock is always taken first.  A start waits for an exit that is
still under way, so a game relaunched seconds after a crash is never refused.

The Windows event log is read with no lock held wherever a session is open
(the watch thread's look, the end of a game): a read can take seconds, and
the hand-back, the next game start and the app quitting must not wait for
it.  What was read is then taken in under the session lock.  A run that
died is judged the same way at the app's start: the card is settled under
the lock, what happened is read and judged after it.  (Only when a game is
started while such a leftover is still there is it judged under the lock,
before the new session takes the game's level.)
"""
from __future__ import annotations

import json
import os
import threading
import time

import config
from core import adaptive_policy as policy
from core import capabilities, gpu_events, gpu_overclock, notifier, process_tuning, temp_ceiling
from core.utils import atomic_write_json, get_logger

log = get_logger("adaptive")

OWNER = gpu_overclock.ADAPTIVE_OWNER        # how gpu_overclock tells this module's writes from everyone else's
VERSION = 2

# This version's own files.  The old tuner's three files keep their names and
# are never written here, so going back to 3.7.0 finds its own data intact
# (and 3.7.0 never reads, or deletes unread, a session file of this version).
SETTINGS_FILE = "adaptive_tuning_v2_settings.json"
STATE_FILE = "adaptive_tuning_v2_games.json"
SESSION_FILE = "adaptive_tuning_v2_session.json"
V1_SETTINGS_FILE = "adaptive_tuning_settings.json"     # read only: the one-time note and the old switch
V1_STATE_FILE = "adaptive_tuning_state.json"           # read only: the one-time note
V1_SESSION_FILE = "adaptive_tuning_session.json"       # a 3.6.8 / 3.7.0 session: handed back once, then removed
STATE_V1_FILE = "adaptive_tuning_state.v1.json"        # where a build before this one moved the old record

POLL_S = 20.0                   # the watch thread wakes this often
PLAY_GAP_CAP_S = 60.0           # a longer gap between polls (a sleeping PC) is not play
SLEEP_QUIET_S = 300.0           # ... and driver errors from that gap, and for this long after it, are not counted
PENDING_S = 120.0               # a driver error waits this long to see whether the game closes
ENDED_WINDOW_S = 120.0          # "the game closed after the error": within this long
TOLERANCE_MHZ = 5               # clocks are whole MHz; this absorbs rounding
MISSES_TO_CLOSE = 3             # polls in a row without the game's process
REASSERTS_TO_HOLD = 3           # writes in a row that did not stick
STARTUP_WAIT_S = 15.0           # for the saved-overclock apply at the app's own start
HANDBACK_TRIES = 3
HANDBACK_WAIT_S = 5.0           # between tries: three tries over ten seconds
CRASH_LOOKBACK_S = 180.0        # a crash record of the game counts from this long before its exit
RECOVERY_SLACK_S = 60.0         # after the last heartbeat of a run that died
SILENT_AFTER_S = 300.0          # "Windows went quiet with the session": nothing logged from here ...
BOOT_LEAD_S = 60.0              # ... to this long before the next start of Windows
BOOT_WINDOW_S = 120.0           # records this close to a start of Windows belong to it
LATE_LOOK_S = 120.0             # a second look for a crash record Windows writes late
KEPT_RECHECK_S = 30.0           # a session kept at the start for a running game is looked at again this often
EXIT_READ_RETRIES_S = (30.0, 120.0, 300.0)      # a look at the event log that failed at a game's end: again after these
UNREAD_LOOKS_S = (120.0, 300.0, 900.0)          # ... and one that failed for a run that died: again after these,
UNREAD_TRIES = 6                # ... and at later starts of the app, this many times in all
UNREAD_KEEP = 5                 # how many such ends are kept waiting
FAILED_READS_TO_SAY = 3         # looks at the event log in a row that failed during play before the page says so
OTHER_GAME_PLAY_MIN = 30.0      # a program that is a game only by a word in its name counts as one with this much play
NO_OVERCLOCK_QUIET_S = 1800.0   # "errors with no overclock on the card": none was held for this long before them,
NO_OVERCLOCK_STEP_S = 300.0     # ... looked at this often
EXTERNAL_WAIT_S = 10.0          # a game start waits this long for an Apply or Reset that is under way
RESTART_SLACK_S = 5.0           # Windows started more than this after a session's last heartbeat: a restart
CONFIRM_DAYS = 30
HISTORY_KEEP = 200
HISTORY_SHOWN = 50
REPORT_MATCH_S = 300.0
SUPPORTED_RETRY_S = 60.0
HEALTH_CACHE_S = 30.0

SENSITIVITIES = policy.SENSITIVITIES

# Sentences of this module's own (the rest are in adaptive_policy).
FIRST_ON_TOAST = ("From now on your saved overclock is applied in every game Vispora detects, "
                  "and taken off when the game closes.")
NO_NVIDIA = "Adaptive Tuning needs an NVIDIA graphics card. This PC has {name}."
NO_CLOCKS = "Vispora can't change the clocks on this graphics card."
UNKNOWN_CARD = "a graphics card Vispora does not recognise"
DAMAGED_STATE = ("Adaptive Tuning's saved data could not be read and was set aside. "
                 "Games it had eased are back at full overclock.")
DAMAGED_SETTINGS = ("Adaptive Tuning's settings could not be read and were set aside. "
                    "It is off until you turn it on again.")
ERR_NO_GAME = "Vispora has not looked after that game yet."
ERR_STEP = "That step is not possible."
ERR_SETTING = "That setting is not possible."
ERR_SAVE = "Adaptive Tuning could not save that change. Try again."
ERR_STATE = "Adaptive Tuning's saved data could not be read. Try again."


def NOT_PUT_BACK(name) -> str:
    return (f"Vispora could not confirm that the overclock was put back when it stopped looking after {name}. "
            "It will try again the next time it starts.")


def CONFIRM_STEP_UP(name, level) -> str:
    return (f"{name} had graphics driver errors at {policy.level_phrase(level)} in the last {CONFIRM_DAYS} days. "
            "Try it again?")


def HOT_AT_START(name) -> str:
    return (f"The graphics card had just got too hot when {name} started, "
            "so the overclock stayed off until the game closed.")


def OWN_AFTER_HOT(name) -> str:
    return (f"The graphics card had just got too hot when {name} started, and an overclock had been put back "
            "on it since, so Adaptive Tuning left it alone until the game closed.")


def STOPPED_KEPT(name) -> str:
    return (f"Adaptive Tuning stopped looking after {name} while it was running, "
            "so the overclock was kept where it was until the game closed.")


def CHANGED_MEANWHILE(name) -> str:
    return (f"The overclock on the graphics card changed while Adaptive Tuning was not looking after {name}, "
            "so it was left alone until the game closed.")


def RUNS_BESIDE(name, other, level) -> str:
    return (f"{other} is running as well and is eased to {policy.level_phrase(level)}, "
            f"so {name} runs at that too until it closes.")


class StateError(Exception):
    """A file of this module could not be read or saved."""


class _Damaged(Exception):
    """A file of this module is there but does not hold what it should."""


# ── seams (the tests replace these) ───────────────────────────────────
def _now() -> float:
    return time.time()


def _pause(seconds: float) -> None:
    """Only between tries: reading a locked file, and the hand-back."""
    time.sleep(seconds)


def _defer(fn) -> None:
    """Run `fn` soon, on a thread of its own, with no lock held."""
    threading.Thread(target=_guarded, args=(fn,), daemon=True, name="adaptive-tuning-after").start()


def _defer_later(seconds: float, fn) -> None:
    t = threading.Timer(seconds, _guarded, args=(fn,))
    t.daemon = True
    t.name = "adaptive-tuning-later"
    t.start()


def _guarded(fn) -> None:
    try:
        fn()
    except Exception as e:
        log.warning(f"Adaptive Tuning: a follow-up step failed: {e}")


def _uptime_s():
    """Seconds since Windows started, or None when that cannot be told."""
    try:
        import ctypes
        tick = ctypes.windll.kernel32.GetTickCount64
        tick.restype = ctypes.c_ulonglong
        return float(tick()) / 1000.0
    except Exception as e:
        log.debug(f"Adaptive Tuning: the time since Windows started could not be read: {e}")
        return None


_WATCH_THREAD = True            # False: no thread is started (the tests run the polls by hand)

# ── state ─────────────────────────────────────────────────────────────
_state_lock = threading.RLock()
_session_lock = threading.RLock()
_sess = None                    # the open session, or None
_next_id = [0]
_last_game = None               # the last game that was not taken on, and why: {"exe", "name", "reason"}
_last_closed = None             # {"exe", "ended", "by"} of the session closed last
# exe -> the driver incident judged last for that game: {"first", "last", "judged"}.  "first" and "judged" are
# its first and last error as it was judged; "last" moves on with the errors that went on from it since (they
# chain for 5 minutes, for 10 minutes from "first" at most).  Kept in the record too (see _keep_tail).
_burst_tail: dict = {}
_failed_reads = [0]             # looks at the event log during play that failed, in a row
_unread_busy = threading.Lock()     # one look at a time at the ends that could not be judged (see _judge_unread)
_supported_cache = {"ok": None, "reason": "", "at": 0.0}
_startup_waited = [False]       # the app's own start has been waited for (once per run)
_health_cache = {"at": None, "value": None}
_recheck_scheduled = [False]    # a look at a kept session file is on its timer


def _norm(exe) -> str:
    return os.path.basename(str(exe or "")).strip().lower()


def _path(name: str) -> str:
    return os.path.join(config.APPDATA_DIR, name)


def settings_path() -> str:
    return _path(SETTINGS_FILE)


def state_path() -> str:
    return _path(STATE_FILE)


def state_v1_path() -> str:
    return _path(STATE_V1_FILE)


def session_path() -> str:
    return _path(SESSION_FILE)


def v1_settings_path() -> str:
    return _path(V1_SETTINGS_FILE)


def v1_state_path() -> str:
    return _path(V1_STATE_FILE)


def v1_session_path() -> str:
    return _path(V1_SESSION_FILE)


# ── files ─────────────────────────────────────────────────────────────
def _read_json(path: str):
    """The dict in `path`, or None when there is no such file.

    A file that is there but cannot be opened is tried three times and then
    raises StateError: it is never taken for a missing one (that would start
    every game again at full overclock and then save over the real record).
    One that opens but does not hold a JSON object raises _Damaged."""
    last = None
    for attempt in range(3):
        if attempt:
            _pause(0.05)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            break
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as e:
            last = e
    else:
        raise StateError(f"{os.path.basename(path)} could not be read: {last}")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _Damaged(path)
    return data


def _write_json(path: str, data: dict) -> None:
    if not atomic_write_json(path, data):
        raise StateError(f"{os.path.basename(path)} could not be saved")


def _set_aside(path: str, tag: str = "damaged") -> str:
    """Move a file out of the way, keeping it: name.json becomes name.<tag>.json."""
    stem, ext = os.path.splitext(path)
    aside = f"{stem}.{tag}{ext}"
    try:
        os.replace(path, aside)
    except OSError as e:
        raise StateError(f"{os.path.basename(path)} could not be set aside: {e}")
    log.warning(f"Adaptive Tuning: {os.path.basename(path)} was set aside as {os.path.basename(aside)}")
    return aside


def _default_settings() -> dict:
    return {"version": VERSION, "enabled": False, "notify": True, "sensitivity": "balanced",
            "upgrade_note": False, "old_switch_was_on": False, "told_in_games": False}


def _is_v2(data: dict) -> bool:
    v = data.get("version")
    return isinstance(v, int) and not isinstance(v, bool) and v >= VERSION


def _old_file(path: str):
    """A file of the old tuner, only ever read: its dict, or None when it is
    not there, cannot be read, or holds this version's shape (a build before
    this one wrote that under the old name)."""
    try:
        data = _read_json(path)
    except (StateError, _Damaged):
        return None
    return data if data is not None and not _is_v2(data) else None


def _old_state_on_disk() -> bool:
    """Is the old tuner's record of games here (where 3.7.0 keeps it, or
    where a build before this one moved it)?"""
    return os.path.exists(state_v1_path()) or _old_file(v1_state_path()) is not None


def _load_settings() -> dict:
    """The settings, in today's shape.  Caller holds the state lock.

    The first time this version runs on a PC where the old tuner was used
    (its settings or its record of games are there): the switch starts off
    for everyone (it used to mean something else: it did nothing until each
    game was opted in, and now it covers every game) and a one-time note
    explains.  The old files are only read, never changed."""
    path = settings_path()
    damaged = False
    try:
        data = _read_json(path)
    except _Damaged:
        _set_aside(path)
        data, damaged = None, True
    if data is not None and not _is_v2(data):               # (this file only ever holds today's shape)
        _set_aside(path)
        data, damaged = None, True
    if data is not None:
        s = _default_settings()
        s["enabled"] = data.get("enabled") is True
        s["notify"] = data.get("notify") is not False
        if data.get("sensitivity") in SENSITIVITIES:
            s["sensitivity"] = data["sensitivity"]
        for key in ("upgrade_note", "old_switch_was_on", "told_in_games"):
            s[key] = data.get(key) is True
        return s
    s = _default_settings()
    if not damaged:
        old = _old_file(v1_settings_path())
        if old is not None:                                 # the old tuner's settings
            s["upgrade_note"] = True
            s["old_switch_was_on"] = old.get("enabled_globally") is True
        elif _old_state_on_disk():                          # no old settings, but the old tuner's games
            s["upgrade_note"] = True
    if damaged or s["upgrade_note"]:
        _write_json(path, s)
    if damaged:
        _note_damage(DAMAGED_SETTINGS)
    return s


def _save_settings(s: dict) -> None:
    s["version"] = VERSION
    _write_json(settings_path(), s)


def _empty_state() -> dict:
    # "forgotten": {exe: {"pid", "name", "ts"}}: games the user forgot while they
    # were running, which are not looked after for the rest of that run.
    # "tails": {exe: {"first", "last", "judged"}}: the driver incident judged
    # last for a game (see _burst_tail), so a restart does not count it again.
    # "unread": the ends of sessions whose run died that could not be judged
    # yet, because the event log could not be read (see _keep_unread).
    return {"version": VERSION, "games": {}, "history": [], "last_sessions": {}, "forgotten": {}, "tails": {},
            "unread": []}


def _clean_tail(tail):
    """A saved {"first", "last", "judged"} as numbers, or None for anything else."""
    if not isinstance(tail, dict):
        return None
    try:
        first, last = float(tail["first"]), float(tail["last"])
        judged = float(tail.get("judged", last))
    except (KeyError, TypeError, ValueError):
        return None
    if not first <= judged <= last:                 # (also false for a value that is not a number)
        return None
    return {"first": first, "last": last, "judged": judged}


def _clean_unread(item):
    """A saved entry of "unread" in today's shape, or None for anything else."""
    if not isinstance(item, dict) or not _norm(item.get("exe")):
        return None
    try:
        lim = item.get("lim")
        return {
            "exe": _norm(item["exe"]), "name": str(item.get("name") or _norm(item["exe"])),
            "pid": int(item.get("pid") or 0), "started": float(item["started"]),
            "judged_until": float(item["judged_until"]), "seen": float(item["seen"]),
            "lim": ([int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
                     for v in lim] if isinstance(lim, list) and len(lim) == 2 else [None, None]),
            "tries": int(item.get("tries") or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _load_state() -> dict:
    """The games, the history and each game's last session.  Caller holds the state lock."""
    path = state_path()
    try:
        data = _read_json(path)
    except _Damaged:
        _set_aside(path)
        st = _empty_state()
        _history(st, "", "", DAMAGED_STATE, _now())
        _save_state(st)
        _toast(DAMAGED_STATE)
        return st
    if data is None:
        return _empty_state()
    if not _is_v2(data):
        # Not today's shape (this file never held the old tuner's record):
        # kept, out of the way, and started afresh.
        _set_aside(path, "old")
        return _empty_state()
    st = _empty_state()
    games = data.get("games")
    for exe, saved in (games.items() if isinstance(games, dict) else ()):
        key = _norm(exe)
        if not key or not isinstance(saved, dict):
            continue
        game = policy.new_game(key, saved.get("name") or key, 0.0)
        game.update(saved)
        game["exe"] = key
        st["games"][key] = game
    hist = data.get("history")
    st["history"] = [h for h in hist if isinstance(h, dict) and h.get("text")] if isinstance(hist, list) else []
    last = data.get("last_sessions")
    st["last_sessions"] = {k: v for k, v in last.items() if isinstance(v, dict)} if isinstance(last, dict) else {}
    forgot = data.get("forgotten")
    for exe, mark in (forgot.items() if isinstance(forgot, dict) else ()):
        key = _norm(exe)
        if key and isinstance(mark, dict):
            st["forgotten"][key] = {"pid": int(_num(mark.get("pid"))), "name": str(mark.get("name") or key),
                                    "ts": _num(mark.get("ts"))}
    tails = data.get("tails")
    for exe, saved in (tails.items() if isinstance(tails, dict) else ()):
        key, tail = _norm(exe), _clean_tail(saved)
        if key and tail is not None:
            st["tails"][key] = tail
    unread = data.get("unread")
    st["unread"] = [u for u in map(_clean_unread, unread if isinstance(unread, list) else ()) if u is not None]
    return st


def _save_state(st: dict) -> None:
    st["version"] = VERSION
    st["history"] = list(st.get("history") or [])[-HISTORY_KEEP:]
    _write_json(state_path(), st)


def _history(st: dict, exe: str, name: str, text: str, ts: float) -> None:
    if text:
        st.setdefault("history", []).append({"ts": ts, "exe": exe or "", "name": name or exe or "", "text": text})


def _add_history(exe: str, name: str, text: str, ts=None) -> None:
    """One line of "What happened", saved.  Never raises."""
    try:
        with _state_lock:
            st = _load_state()
            _history(st, exe, name, text, _now() if ts is None else ts)
            _save_state(st)
    except Exception as e:
        log.warning(f"Adaptive Tuning: could not record '{text}': {e}")


def _note_damage(text: str) -> None:
    _add_history("", "", text)
    _toast(text)


def _toast(text, always: bool = False) -> None:
    """A Windows notification, when "Tell me when it changes something" is on."""
    if not text:
        return
    try:
        if not always:
            with _state_lock:
                if not _load_settings()["notify"]:
                    return
        notifier.toast(policy.TOAST_TITLE, text, force=True)
    except Exception as e:
        log.debug(f"Adaptive Tuning: toast failed: {e}")


# ── the session file ──────────────────────────────────────────────────
def _session_record(s: dict) -> dict:
    return {
        "version": VERSION, "exe": s["exe"], "name": s["name"], "pid": s["pid"],
        "started": s["started"], "seen": s["seen"], "judged_until": s["judged_until"],
        "pre": list(s["pre"]), "writes": [list(w) for w in s["writes"]],
        "level": s["level"], "held": s["held"],
        # Not in the list above the card can be put back from, but needed for it:
        "power_touched": bool(s["power_touched"]), "play_s": round(float(s["play_s"]), 1),
        "judged": bool(s.get("judged")),
        # Was "pre" something this app held (and enforced), or only what the card read?
        "pre_held": bool(s.get("pre_held", True)), "released_from": s.get("released_from"),
        # The lowest the saved overclock was seen at during the session (per part; None: not seen yet).
        "lim": list(s.get("lim") or [None, None]),
    }


def _save_session(s: dict) -> None:
    """Raises when the file cannot be saved."""
    _write_json(session_path(), _session_record(s))


def _save_session_quiet(s: dict) -> bool:
    try:
        _save_session(s)
        return True
    except Exception as e:
        log.warning(f"Adaptive Tuning: the session file could not be saved: {e}")
        return False


HELD_KINDS = ("manual", "hot", "other_program", "released")
CLOSED_RECORD = {"version": VERSION, "closed": True}   # what a session file that could not be removed is made into


def _delete_session_file(path=None) -> None:
    """Remove the session file.  Another program can hold it open for a
    moment (a scanner, a backup tool), so it is tried for half a second; a file
    that still cannot be removed is made into a record of nothing (a session
    that has ended), so that no later start takes it for a run that died."""
    path = path or session_path()
    last = None
    for attempt in range(10):
        if attempt:
            _pause(0.05)
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last = e
    log.warning(f"Adaptive Tuning: the session file could not be removed: {last}")
    try:
        _write_json(path, dict(CLOSED_RECORD))
        log.warning("Adaptive Tuning: the session file was marked as ended instead")
    except Exception as e:
        log.warning(f"Adaptive Tuning: the session file could not be marked as ended either: {e}")


def _read_session_file(path=None):
    """A session file as a dict in today's shape, or None when there is none.

    A file written by 3.6.8 or 3.7.0 ({"exe", "started", "pre": {core, mem},
    "applied": {core, mem} | None}) is read too: its card must still be
    handed back.  A file marked as ended is no session.  A damaged file is set
    aside (there is nothing in it to put back from).  "path" in the answer is
    the file it came from."""
    path = path or session_path()
    try:
        data = _read_json(path)
    except _Damaged:
        _set_aside(path)
        _add_history("", "", "Adaptive Tuning's record of the last game session could not be read and was set aside.")
        return None
    if data is None:
        return None
    if data.get("closed") is True:
        _delete_session_file(path)
        return None
    try:
        if _is_v2(data):
            pre = data.get("pre") or [0, 0, 100]
            rec = {
                "version": VERSION, "old_format": False, "path": path,
                "exe": _norm(data.get("exe")), "name": str(data.get("name") or data.get("exe") or ""),
                "pid": int(data.get("pid") or 0),
                "started": float(data.get("started") or 0.0),
                "pre": [int(pre[0]), int(pre[1]), int(pre[2]) if len(pre) > 2 and pre[2] is not None else 100],
                "writes": [[int(w[0]), int(w[1])] for w in (data.get("writes") or [])],
                "level": max(0, min(policy.MAX_LEVEL, int(data.get("level") or 0))),
                "held": data.get("held") if data.get("held") in HELD_KINDS else None,
                "play_s": float(data.get("play_s") or 0.0),
                "judged": data.get("judged") is True,
                # A file from before this was recorded: as it was then (held).
                "pre_held": data.get("pre_held") is not False,
                "released_from": data.get("released_from") if data.get("released_from") in HELD_KINDS else None,
            }
            rec["seen"] = float(data.get("seen") or rec["started"])
            rec["judged_until"] = float(data.get("judged_until") or rec["started"])
            rec["power_touched"] = (data["power_touched"] is True if "power_touched" in data
                                    else rec["level"] < policy.MAX_LEVEL)
            lim = data.get("lim")
            rec["lim"] = ([int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
                           for v in lim] if isinstance(lim, list) and len(lim) == 2 else [None, None])
            return rec
        pre, applied = data.get("pre") or {}, data.get("applied")
        started = float(data.get("started") or 0.0)
        return {
            "version": 1, "old_format": True, "path": path, "exe": _norm(data.get("exe")), "name": "",
            "pid": 0, "started": started, "seen": started, "judged_until": started,
            "pre": [int(pre.get("core", 0) or 0), int(pre.get("mem", 0) or 0), 100],
            "writes": [[int(applied.get("core", 0) or 0), int(applied.get("mem", 0) or 0)]]
            if isinstance(applied, dict) else [],
            "level": 0, "held": None, "play_s": 0.0, "judged": True, "power_touched": False,
            "pre_held": True, "released_from": None, "lim": [None, None],
        }
    except (TypeError, ValueError, IndexError, AttributeError, KeyError) as e:
        log.warning(f"Adaptive Tuning: the session file is not usable ({e})")
        _set_aside(path)
        _add_history("", "", "Adaptive Tuning's record of the last game session could not be read and was set aside.")
        return None


def _read_leftover():
    """A session file with no session behind it: this version's, else one the
    old tuner (3.6.8, 3.7.0) left behind.  None when there is neither."""
    rec = _read_session_file()
    if rec is None:
        rec = _read_session_file(v1_session_path())
    return rec


# ── the card, the saved overclock, the temperature ceiling ────────────
def supported() -> dict:
    """{"ok", "reason"}: can Adaptive Tuning work on this PC?  A yes is kept for
    the life of the process, a no is asked again after a minute."""
    c = _supported_cache
    now = _now()
    if c["ok"] is True:
        return {"ok": True, "reason": ""}
    if c["ok"] is False and 0 <= now - c["at"] < SUPPORTED_RETRY_S:
        return {"ok": False, "reason": c["reason"]}
    ok, reason = False, NO_CLOCKS
    try:
        caps = capabilities.detect() or {}
        if not caps.get("has_nvidia"):
            name = str(caps.get("gpu_name") or "").strip()
            if not name or name.lower() == "unknown":
                name = UNKNOWN_CARD
            reason = NO_NVIDIA.format(name=name)
        elif (gpu_overclock.get_current_offsets() or {}).get("ok"):
            ok, reason = True, ""
    except Exception as e:
        log.debug(f"Adaptive Tuning: could not tell whether this card is supported: {e}")
    c.update(ok=ok, reason=reason, at=now)
    return {"ok": ok, "reason": reason}


def _clamp(value, low: int, high: int) -> int:
    return max(low, min(high, int(value or 0)))


def _profile() -> dict:
    """The saved overclock as it is on disk right now (it is never kept).

    {"exists", "unreadable", "core", "mem", "power", "apply_on_startup"}; core and
    mem are held to the same bounds gpu_overclock applies, so what a session
    asks for is what the card can be compared with."""
    out = {"exists": False, "unreadable": False, "core": 0, "mem": 0, "power": 100, "apply_on_startup": False}
    try:
        p = gpu_overclock.load_profile() or {}
        if not p.get("exists"):
            out["unreadable"] = bool(p.get("err"))
            return out
        out.update(
            exists=True,
            core=_clamp(p.get("core_offset_mhz"), -500, gpu_overclock.MAX_CORE_OFFSET_MHZ),
            mem=_clamp(p.get("mem_offset_mhz"), -1000, gpu_overclock.MAX_MEM_OFFSET_MHZ),
            power=_clamp(p.get("power_pct", 100), gpu_overclock.MIN_POWER_PCT, gpu_overclock.MAX_POWER_PCT),
            apply_on_startup=p.get("apply_on_startup", True) is not False,
        )
    except Exception as e:
        log.warning(f"Adaptive Tuning: the saved overclock could not be read: {e}")
        out.update(exists=False, unreadable=True, core=0, mem=0)
    return out


def _has_overclock(p: dict) -> bool:
    return bool(p["exists"]) and (p["core"], p["mem"]) != (0, 0)


def _limit(p: dict):
    return [p["core"], p["mem"]] if p["exists"] else None


def _level_values(p: dict, level: int, limit_at_level) -> tuple:
    core, mem = policy.offsets_for([p["core"], p["mem"]], level, limit_at_level)
    return (int(core), int(mem))


def _lvl(game: dict) -> int:
    try:
        return max(0, min(policy.MAX_LEVEL, int(game.get("level") or 0)))
    except (TypeError, ValueError):
        return 0


def _bounded(s, prof: dict) -> dict:
    """The saved overclock as a session may use it: each positive part no
    higher than the lowest the session has seen it at.  A lowered profile is
    followed at once; one raised mid-game lifts nothing, also not the limit
    an ease is recorded against.  (`s` None: the profile as it is.)"""
    if s is None or not prof.get("exists"):
        return prof
    lim = s.setdefault("lim", [None, None])
    out = dict(prof)
    for i, key in enumerate(("core", "mem")):
        v = int(prof[key])
        if v > 0:
            lim[i] = v if lim[i] is None else min(int(lim[i]), v)
            out[key] = min(v, int(lim[i]))
    return out


def _eased_beside(exe: str, level: int, games=None):
    """The most eased game on record that is running beside `exe` right now
    and is eased further than `level`: (its level, the limit it was eased
    against, its exe, its name), or None.

    Game mode tracks one process, the first it found, and the card holds only
    one value: a game that was eased because it fails on more must not run on
    more because another program's window was found first.  A game whose own
    switch is off is left out.  The session only ever goes down for this (the
    card is handed back at its end as usual), no record is changed, and it is
    not one of the session's own steps.  None too when the process list or
    the record cannot be taken: nothing changes then."""
    try:
        procs = process_tuning.processes()
        if not procs:
            return None
        running = {str(p.get("exe") or "").lower() for p in procs}
        if games is None:
            with _state_lock:
                games = _load_state()["games"]
        found = None
        for other, g in games.items():
            if other == exe or other not in running or not isinstance(g, dict):
                continue
            if g.get("enabled", True) is False:
                continue
            if _lvl(g) > level and (found is None or _lvl(g) > found[0]):
                found = (_lvl(g), g.get("limit_at_level"), other, str(g.get("name") or other))
        return found
    except Exception as e:
        log.debug(f"Adaptive Tuning: could not look for other eased games: {e}")
        return None


def _go_down_beside(s: dict, beside) -> bool:
    """The session takes the level of the eased game running beside it.
    True when that game was not the one it had taken its level from before
    (then it is said once under "What happened")."""
    s["level"], s["lal"] = beside[0], beside[1]
    first = s.get("floor_from") != beside[2]
    s["floor_from"] = beside[2]
    if first:
        log.info(f"Adaptive Tuning: {beside[3]} ({beside[2]}) is running beside {s['name']} and is eased to "
                 f"{policy.level_phrase(beside[0])}: the session runs at that")
    return first


def _power_pct_now() -> int:
    """The card's power limit now, in whole percent of its default, held to
    the bounds gpu_overclock applies; 100 when either cannot be read."""
    try:
        live = gpu_overclock.read_live_state() or {}
        limits = (gpu_overclock.detect_gpu_oc_capability() or {}).get("limits") or {}
        watts, default = float(live.get("power_limit_w") or 0), float(limits.get("power_default_w") or 0)
        if not live.get("ok") or watts <= 0 or default <= 0:
            return 100
        return _clamp(round(watts * 100.0 / default), gpu_overclock.MIN_POWER_PCT, gpu_overclock.MAX_POWER_PCT)
    except Exception as e:
        log.debug(f"Adaptive Tuning: the power limit could not be read: {e}")
        return 100


def _same(a, b) -> bool:
    return abs(int(a[0]) - int(b[0])) <= TOLERANCE_MHZ and abs(int(a[1]) - int(b[1])) <= TOLERANCE_MHZ


def _read_card(verified: bool = False):
    """(core, mem) as the driver reports them now, or None when it cannot be
    read.  `verified`: read twice, and both must agree."""
    try:
        r = gpu_overclock.get_current_offsets() or {}
        if not r.get("ok"):
            return None
        first = (int(r.get("core_offset_mhz") or 0), int(r.get("mem_offset_mhz") or 0))
        if not verified:
            return first
        r = gpu_overclock.get_current_offsets() or {}
        if not r.get("ok"):
            return None
        second = (int(r.get("core_offset_mhz") or 0), int(r.get("mem_offset_mhz") or 0))
        return second if _same(first, second) else None
    except Exception as e:
        log.debug(f"Adaptive Tuning: the card could not be read: {e}")
        return None


def _ceiling() -> dict:
    try:
        return temp_ceiling.get_status() or {}
    except Exception as e:
        log.debug(f"Adaptive Tuning: the temperature ceiling could not be asked: {e}")
        return {}


def _reset_trip(c: dict) -> bool:
    """Did the ceiling's last trip take the overclock off?  With its action
    set to "Notify only" it only told the user, and the card kept what it had.
    (A trip with no action on record counts as a reset.)"""
    return c.get("last_trip_action") != "notify"


def _cooldown_active() -> bool:
    """The temperature ceiling has just taken the overclock off a card that
    was too hot, and cannot trip again until its cooldown is over: nothing may
    put an overclock back meanwhile."""
    c = _ceiling()
    return c.get("cooldown_remaining_s") is not None and _reset_trip(c)


def _tripped_since(ts: float) -> bool:
    """Has the ceiling taken the overclock off since `ts`?"""
    try:
        c = _ceiling()
        trip = c.get("last_trip_at")
        return trip is not None and float(trip) > float(ts) and _reset_trip(c)
    except (TypeError, ValueError):
        return False


def _write_counts():
    """(every clock write not this module's own, how many of those were the
    temperature ceiling's resets), read together; None when not known."""
    try:
        total, ceiling = gpu_overclock.external_write_counts()
        return int(total), int(ceiling)
    except Exception as e:
        log.debug(f"Adaptive Tuning: the write counts could not be read: {e}")
        return None


def _others_count():
    """Clock writes by anyone but this module and the temperature ceiling:
    the user (or another part of the app).  None when not known."""
    counts = _write_counts()
    return None if counts is None else counts[0] - counts[1]


def _user_changed_since_hold(s: dict) -> bool:
    """Since the session was left alone ("hot", "other_program", "released"),
    has the user changed the overclock (or said they are about to)?  The
    ceiling's own resets do not count: they are not the user."""
    return bool(s["manual_note"]) or s["held_seq"] is None or _others_count() != s["held_seq"]


def _ceiling_reset_during(s: dict) -> bool:
    """Has the temperature ceiling taken the overclock off during this session
    (on record, or a reset of it is under way right now)?"""
    if _tripped_since(s["started"]):
        return True
    counts = _write_counts()
    return counts is not None and s.get("ceil_seq") is not None and counts[1] != s["ceil_seq"]


def _windows_restarted_since(ts: float) -> bool:
    """Did Windows start after `ts` (a session's last heartbeat)?  Then no
    process of that session can still be running."""
    try:
        up = _uptime_s()
        return up is not None and _now() - float(up) > float(ts) + RESTART_SLACK_S
    except (TypeError, ValueError):
        return False


def _display_name(exe: str) -> str:
    try:
        from core import game_profiles
        return str(game_profiles.display_name_for(exe) or exe)
    except Exception:
        return exe


def _exe_running(exe: str):
    """True, False, or None when the process list cannot be taken (which a
    caller must not take for "gone")."""
    try:
        procs = process_tuning.processes()
    except Exception as e:
        log.debug(f"Adaptive Tuning: the process list could not be taken: {e}")
        return None
    if procs is None:
        return None
    return any(str(p.get("exe") or "").lower() == exe for p in procs)


def _taken_over(s: dict) -> bool:
    """Has anything but this session written the clocks (or said it is about to)?"""
    try:
        return bool(s["manual_note"]) or gpu_overclock.external_write_seq() != s["seq"]
    except Exception:
        return True


# ── the one place that writes the clocks ──────────────────────────────
def _write(why: str, s=None, values=None, light: bool = False, cap=None, skip_if_held: bool = False) -> dict:
    """Every clock write of this module (I2).  Returns {"ok", "err", ...};
    "refused" says why nothing was written.  Never raises.

    why "handback": `values` is (core, mem, power) exactly as recorded before
        the session (power None: leave the power limit alone).  The only write
        that is not bound by the saved overclock, because it is not this
        module's choice of value: it is what the card held before.

    anything else is a session write, for the open session `s`:
      * refused unless `s` is the open session, not stopped, not held, nobody
        else has written the clocks since, the temperature ceiling is not in
        its cooldown, the switch is on and a saved overclock exists;
      * the value is never passed in: it is worked out here, from the saved
        overclock as read at this moment and the session's level, so it cannot
        be above what the user saved.  `cap` (the session's current values)
        keeps a write from going UP by itself when the saved overclock was
        raised mid-game;
      * the value is noted in the session file BEFORE the card is written, so
        a run that dies right here still knows what it put on the card;
      * `light`: one driver call and no process started (re-asserts, lowering
        during play); otherwise the full apply, with the profile's power limit
        at levels 0 to 2 and the power limit left alone at "no overclock";
      * `skip_if_held` (taking a game on): when this app already holds exactly
        these clocks and power limit and the card reads them back, the value
        is recorded as the session's but the card is not written again.
    """
    with _session_lock:
        try:
            if why == "handback":
                core, mem, power = int(values[0]), int(values[1]), values[2]
                r = gpu_overclock.apply_oc(core_offset_mhz=core, mem_offset_mhz=mem, power_pct=power, owner=OWNER)
                ok = bool((r or {}).get("ok"))
                log.info(f"Adaptive Tuning: card handed back to core{core:+d} / mem{mem:+d} "
                         f"({'ok' if ok else 'not accepted'})")
                return {"ok": ok, "err": "" if ok else str((r or {}).get("err") or ""), "core": core, "mem": mem}

            def refused(reason):
                log.info(f"Adaptive Tuning: no write ({why}): {reason}")
                return {"ok": False, "err": reason, "refused": reason}

            if s is None or s is not _sess or s["stop"].is_set():
                return refused("no open session")
            if s["held"]:
                return refused("held")
            if _taken_over(s):
                return refused("taken over")
            if _cooldown_active():
                return refused("cooldown")
            with _state_lock:
                enabled = _load_settings()["enabled"]
            if not enabled:
                return refused("off")
            prof = _bounded(s, _profile())
            if not prof["exists"]:
                return refused("no saved overclock")
            core, mem = _level_values(prof, s["level"], s["lal"])
            if cap is not None:
                core, mem = min(core, int(cap[0])), min(mem, int(cap[1]))
            power = None if (light or s["level"] >= policy.MAX_LEVEL) else int(prof["power"])
            pair = [core, mem]
            fresh = pair not in s["writes"]
            if fresh:
                s["writes"].append(pair)
            if power is not None:
                fresh = fresh or not s["power_touched"]
                s["power_touched"], s["power_set"] = True, power
            if fresh:
                _save_session(s)                    # BEFORE the card is written
            s["values"] = (core, mem)
            if skip_if_held:
                ha = gpu_overclock.get_applied_oc() or {}
                if (ha.get("active") and (int(ha.get("core") or 0), int(ha.get("mem") or 0)) == (core, mem)
                        and (power is None or int(ha.get("power") or 100) == power)):
                    card = _read_card(verified=True)
                    if card is not None and _same(card, (core, mem)):
                        log.info(f"Adaptive Tuning: {s['name']} at {policy.level_phrase(s['level'])}: "
                                 f"core{core:+d} / mem{mem:+d} is on the card already ({why}; not written again)")
                        return {"ok": True, "err": "", "core": core, "mem": mem, "superseded": False,
                                "skipped": True}
            if light:
                r = gpu_overclock.write_offsets(core, mem, owner=OWNER, if_seq=s["seq"])
            else:
                r = gpu_overclock.apply_oc(core_offset_mhz=core, mem_offset_mhz=mem, power_pct=power, owner=OWNER)
            r = r or {}
            ok = bool(r.get("ok"))
            log.info(f"Adaptive Tuning: {s['name']} at {policy.level_phrase(s['level'])}: core{core:+d} / mem{mem:+d} "
                     f"({why}; {'ok' if ok else 'not accepted: ' + str(r.get('err') or '')})")
            return {"ok": ok, "err": "" if ok else str(r.get("err") or ""), "core": core, "mem": mem,
                    "superseded": bool(r.get("superseded"))}
        except Exception as e:
            log.warning(f"Adaptive Tuning: write failed ({why}): {e}")
            return {"ok": False, "err": str(e), "refused": "error"}


def _resume_watchdog() -> None:
    try:
        gpu_overclock.resume_oc_watchdog(OWNER)
    except Exception as e:
        log.warning(f"Adaptive Tuning: the drift watchdog could not be resumed: {e}")


def _pause_watchdog() -> None:
    try:
        gpu_overclock.pause_oc_watchdog(OWNER)
    except Exception as e:
        log.warning(f"Adaptive Tuning: the drift watchdog could not be paused: {e}")


def _drop_hold() -> None:
    try:
        gpu_overclock.drop_hold()
    except Exception as e:
        log.warning(f"Adaptive Tuning: the drift watchdog's target could not be dropped: {e}")


def _end_kind(s: dict):
    """How the session stands for its end: "manual" (the user took over: their
    value stays), "hot", "other_program", "released" or None.  Also what the
    watch thread has not seen yet: a ceiling reset first (it counts as
    somebody else's write too, and must not read as the user), then anybody
    else's write."""
    held = s["held"]
    if held is None:
        if _ceiling_reset_during(s):
            return "hot"
        return "manual" if _taken_over(s) else None
    if held in ("hot", "other_program", "released") and _user_changed_since_hold(s):
        return "manual"                     # after it was left alone, the user set something themselves
    return held


def _would_raise(s: dict) -> bool:
    """Would handing the card back now raise its clocks (core or memory) above
    what it holds?  As _hand_back() decides: nothing is written for the
    user's change, during the ceiling's cooldown, or when the session wrote
    nothing.  A card that cannot be read is taken to hold the session's
    values (no overclock after the ceiling's reset)."""
    kind = _end_kind(s)
    if kind == "manual" or _cooldown_active() or not s["writes"]:
        return False
    want = (int(s["pre"][0]), int(s["pre"][1]))
    card = _read_card()
    if card is None:
        card = (0, 0) if kind == "hot" else tuple(s["values"])
    return want[0] > int(card[0]) + TOLERANCE_MHZ or want[1] > int(card[1]) + TOLERANCE_MHZ


def _hand_back(s: dict) -> dict:
    """Give the card back as the session found it.  Caller holds the session lock.

    Returns {"settled", "verified", "wrote", "left"}.  "settled" False means the
    card could not be confirmed back: the session file is then kept, so the
    next start (or the game's next launch) still knows what is owed.

      * the user changed the overclock during the game: their value stays;
      * the temperature ceiling is in its cooldown: it took the overclock off
        a card that was too hot, and that stands;
      * the session wrote nothing, or only what was on the card anyway, and
        the card still reads that: nothing to do;
      * otherwise what was on the card before the session is written, read
        back (5 MHz), and tried again up to three times over ten seconds: right
        after a driver crash the first write often does not land.
    When the app was not holding what was on the card before the session
    (another tool put it there, say), it is not held afterwards either: the
    drift watchdog is not left enforcing it.  The drift watchdog runs again
    afterwards, whatever happened."""
    out = {"settled": False, "verified": False, "wrote": False, "left": None}
    pre = s["pre"]
    want = (int(pre[0]), int(pre[1]))
    try:
        held = _end_kind(s)
        if held == "manual":
            out.update(settled=True, left="manual")
        elif _cooldown_active():
            out.update(settled=True, left="hot")
        elif not s["writes"]:
            out.update(settled=True, verified=True)
        else:
            power = int(pre[2]) if s["power_touched"] else None
            changed = (any(tuple(w) != want for w in s["writes"])
                       or (s["power_touched"] and s["power_set"] != power))
            card = None if changed else _read_card()
            single = held == "other_program" or (held == "released" and s.get("released_from") == "other_program")
            if card is not None and _same(card, want):
                out.update(settled=True, verified=True)
            else:
                for attempt in range(1 if single else HANDBACK_TRIES):
                    if attempt:
                        _pause(HANDBACK_WAIT_S)
                    _write("handback", values=(want[0], want[1], power))
                    out["wrote"] = True
                    card = _read_card()
                    if card is not None and _same(card, want):
                        out.update(settled=True, verified=True)
                        break
                if single:
                    out["settled"] = True   # another program has the card: what was asked for is all that can be done
            if out["settled"] and not s.get("pre_held", True):
                _drop_hold()                # (before the watchdog runs again below)
    except Exception as e:
        log.error(f"Adaptive Tuning: handing the card back failed: {e}")
    finally:
        _resume_watchdog()
    return out


def _hold(s: dict, kind: str, now: float, text=None) -> None:
    """From here to the end of the session it writes nothing and counts nothing.

    "manual": the user changed the overclock (or the saved one is gone): their
    value stays, also after the game.  "hot": the temperature ceiling took the
    overclock off.  "other_program": something outside Vispora keeps changing
    the clocks.  "released": the session was let go of while its game was
    still running, and handing the card back would have raised the clocks
    under it: its values stay on the card until the game closes.

    The drift watchdog is handed back at once for the first two (the user's
    own overclock is watched again); for the other two it stays paused until
    the game closes, because its target is still this session's values and it
    would go on writing them every five seconds.

    `text`: the line for "What happened" (a function of the game's name) when
    it is not the usual one for this kind."""
    if kind == "released":
        s["released_from"] = s["held"]
    s["held"] = kind
    s["pending"], s["other_seen"] = [], None
    s["judged_until"] = now
    # Counted without the ceiling's own resets: one of those after this is
    # not the user (a card that stays hot trips again every minute).
    s["held_seq"] = _others_count()
    if kind in ("hot", "manual"):
        _resume_watchdog()
    _save_session_quiet(s)
    if text is None:
        text = {"hot": policy.HELD_HOT, "manual": policy.HELD_MANUAL, "other_program": policy.HELD_OTHER,
                "released": policy.HELD_RELEASED}[kind]
    _add_history(s["exe"], s["name"], text(s["name"]), now)
    log.info(f"Adaptive Tuning: {s['name']} is left alone until it closes ({kind})")


# ── a session starts ──────────────────────────────────────────────────
def _new_session(exe: str, name: str, pid, now: float, pre, level: int, lal, values, counts=None,
                 pre_held: bool = True, mark=None) -> dict:
    """`counts`: (every clock write that was not this module's, the ceiling's
    resets among them) as read before "pre" was taken; read now when left out.
    `pre_held`: was "pre" something the app itself was holding?  `mark`: the
    number of the newest record in the System log just before `now` (see
    _log_mark; None: not known)."""
    _next_id[0] += 1
    _failed_reads[0] = 0
    if counts is None:
        counts = _write_counts()
    if counts is None:
        counts = (gpu_overclock.external_write_seq(), None)
    return {
        "id": _next_id[0], "exe": exe, "name": name, "pid": int(pid or 0),
        "started": now,             # when it was first engaged (kept across an adoption)
        "count_from": now,          # driver errors before this are not this session's
        "adopted_at": None,         # when on_game_start last found it already open
        "launches": [now],          # when a run of the game began within this session (a relaunch found while
                                    # it was open adds one)
        "rec_mark": mark,           # records of the System log up to this number were written before the session
        "seen": now, "judged_until": now,
        "pre": [int(pre[0]), int(pre[1]), int(pre[2])],
        "pre_held": bool(pre_held),         # False: the card held "pre" but the app was not holding it
        "writes": [], "power_touched": False, "power_set": None,
        "level": level, "lal": lal, "values": (int(values[0]), int(values[1])),
        "lim": [None, None],                # the lowest the saved overclock was seen at (see _bounded)
        "floor_from": None,                 # the eased game running beside this one whose level it took
        "held": None, "held_seq": None, "released_from": None,
        "applied_ok": True,         # False once the card has said no (not while the first write is still under way)
        # The write counts the session starts from: anybody else's write after
        # this is a take-over; a ceiling reset after this is the ceiling's.
        "seq": int(counts[0]), "ceil_seq": counts[1], "manual_note": False,
        "pending": [], "seen_records": set(),
        "other_seen": None,         # was another game running when the errors now waiting were read? (None: not looked)
        "absorbed": None,           # errors that went on from an incident judged earlier (see _absorb)
        "set_back": None,           # the wall clock was set back and the session's times are still to be moved
        "play_s": 0.0, "last_poll": now, "poll_end": now, "misses": 0, "first_miss": None, "reasserts": 0,
        "stepped": False, "eased_to": None, "judged": False,
        "trouble": None,            # "driver" / "crash": something was noted and nothing changed (the report line)
        "judged_n": 0,              # incidents judged at the session's end
        "ended": None, "closed_by": None, "handback": None,
        "stop": threading.Event(), "thread": None,
    }


def _engaged(s: dict, adopted: bool = False) -> dict:
    return {"engaged": True, "adopted": adopted, "exe": s["exe"], "name": s["name"], "level": s["level"],
            "level_name": policy.level_name(s["level"]), "core": s["values"][0], "mem": s["values"][1],
            "applied_ok": bool(s["applied_ok"]), "held": s["held"]}


def _remember(exe: str, name: str, reason: str) -> None:
    """What get_state() says about a game that is running but not looked after."""
    global _last_game
    if reason in ("switched_off", "test_running", "card_unreadable", "forgotten"):
        _last_game = {"exe": exe, "name": name, "reason": reason}
    else:
        _last_game = None


def _forget_running(st: dict, exe: str, name: str, pid, now: float) -> None:
    """The user forgot this game (or everything) while it was running: it is
    not looked after for the rest of this run of it.  (Its next start is a
    first start, at full overclock; taking it on again now would put more on
    the card under a game that is in the middle of being played.)  Noted in
    `st`, which the caller saves."""
    if exe and _exe_running(exe) is True:
        st["forgotten"][exe] = {"pid": int(pid or 0), "name": str(name or exe), "ts": now}


def _forgotten_now(exe: str, mark: dict, pid=None) -> bool:
    """Is the run of `exe` that the user forgot still going?  Not when the
    game is found under another process, when no process of it is running, or
    when Windows was started since."""
    if pid and mark.get("pid") and int(pid) != int(mark["pid"]):
        return False
    if _exe_running(exe) is False:
        return False
    return not _windows_restarted_since(mark.get("ts") or 0.0)


def _unforget(exe: str) -> None:
    """That run of a game the user had forgotten is over.  Never raises."""
    try:
        with _state_lock:
            st = _load_state()
            if st["forgotten"].pop(exe, None) is not None:
                _save_state(st)
    except Exception as e:
        log.debug(f"Adaptive Tuning: the note about {exe} could not be cleared: {e}")


def _gate(exe: str, pid=None):
    """Why this game is not taken on, or "".  Also returns the saved overclock
    and the game's record (None for a game never looked after)."""
    with _state_lock:
        settings = _load_settings()
        st = _load_state()
        game, forgot = st["games"].get(exe), st["forgotten"].get(exe)
    prof = _profile()
    if not settings["enabled"]:
        return "off", prof, game
    if not supported()["ok"]:
        return "unsupported", prof, game
    if not _has_overclock(prof):
        return "no_overclock", prof, game
    if game is not None and game.get("enabled", True) is False:
        return "switched_off", prof, game
    if forgot is not None:
        if _forgotten_now(exe, forgot, pid):
            return "forgotten", prof, game
        _unforget(exe)                      # that run of it is over: this is a first start
    if gpu_overclock.is_tuning_active():
        return "test_running", prof, game
    return "", prof, game


def on_game_start(exe, display_name=None, pid=None) -> dict:
    """Game mode found a game.  Returns {"engaged": bool, ...}; when it is not
    taken on, "reason" says why in one word: "off", "unsupported",
    "no_overclock", "switched_off", "forgotten" (the user forgot it while it
    was running), "test_running", "card_unreadable", "another_game" (a game
    that was let go of is still running and keeps the card until it closes),
    "error".  Nothing is written for a game that is not taken on, except that
    what an earlier run left on the card may be put back first.

    Waits for an exit that is still being handled (session starts and ends go
    one at a time), so a game relaunched right after a crash is taken on at
    the level that crash left it at.  Also waits, up to 15 s once per run, for
    the app's own start-up apply, and up to 10 s for an Apply or Reset on the
    GPU page that is under way.  With the switch on, the newest record of the
    Windows System log is noted first (see _log_mark): what Windows wrote
    before the session is never counted for it."""
    key = _norm(exe)
    if not key:
        return {"engaged": False, "reason": "no_game"}
    try:
        mark = _log_mark() if _mark_wanted(key) else None      # (a read of the event log: before the lock)
        with _session_lock:
            return _engage_locked(key, display_name, pid, mark)
    except Exception as e:
        log.warning(f"Adaptive Tuning: could not look after {key}: {e}")
        return {"engaged": False, "reason": "error", "err": str(e)}


def _mark_wanted(exe: str) -> bool:
    """Is a session about to be started for this game, as far as can be told
    without the session lock?  (Not with the switch off, and not for a game
    whose session is open: that one keeps the mark it has.)"""
    s = _sess
    if s is not None and s["exe"] == exe and s["held"] != "released":
        return False
    try:
        with _state_lock:
            return bool(_load_settings()["enabled"])
    except Exception:
        return False


def _log_mark():
    """The number of the newest record in the System log right now, or None
    when that cannot be told.

    Windows numbers the records of a log in the order it writes them, so "this
    record was written before the session began" does not depend on the
    clock.  A driver error from before a session is then never that
    session's, also when the clock is set back and passes that record's time
    a second time (see _is_new).  One record is read.  Never raises.
    (If the System log is cleared during a session its numbers start again,
    and the rest of that session counts nothing.)"""
    try:
        mark = gpu_events.newest_system_record_id()
        return None if mark is None else int(mark)
    except Exception as e:
        log.debug(f"Adaptive Tuning: the newest record of the System log could not be read: {e}")
    return None


def _engage_locked(exe: str, display_name, pid, mark=None) -> dict:
    global _sess, _last_closed
    now = _now()
    s = _sess
    if s is not None:
        if s["exe"] == exe and s["held"] == "released":
            again = _look_after_again(s, display_name, pid, now, mark)
            if again is not None:
                return again
        elif s["exe"] == exe:
            # Found again while its session is open (game mode was put back and
            # detected the game again, or its next launch came before the exit
            # was reported).  The card holds this session's values now, so it
            # keeps its own "before": taking a fresh one here used to hand the
            # session's own clocks back as "what was on the card before".
            if pid and s["pid"] and int(pid) != s["pid"]:
                s["launches"].append(now)   # another process: the game was started again
            if pid:
                s["pid"] = int(pid)
            if display_name:
                s["name"] = str(display_name)
            s["adopted_at"] = now
            _save_session_quiet(s)
            return _engaged(s, adopted=True)
        else:
            hb = _release_locked(s, f"another game started ({exe})", said=STOPPED_KEPT)
            if hb.get("kept_open"):
                # Its game is still running and the card stays as that session
                # left it until it closes: taking this game on now could raise
                # the clocks under the other one.  (It is picked up when the
                # other one has closed, if game mode still tracks it then.)
                log.info(f"Adaptive Tuning: {exe} is not looked after ({s['name']} is still running and keeps "
                         "its overclock until it closes)")
                return {"engaged": False, "reason": "another_game"}
    if _last_closed and _last_closed["exe"] == exe:
        _last_closed = None

    # A session file with no session behind it: a run that died mid-game (the
    # start-up check kept it because the game was still running), a hand-back
    # that could not be confirmed, or one the old tuner left behind.
    adopt = unsettled = None
    left = _read_leftover()
    if left is not None:
        restarted = _windows_restarted_since(left["seen"])
        same = left["exe"] == exe
        if same and not left["old_format"] and not left["judged"] and not restarted:
            adopt = left                    # that run died mid-game and its game is still running
        else:
            # Another game's; or one whose own end has run already (only the
            # hand-back could not be confirmed); or one from before Windows
            # was restarted, whose process is gone whatever runs under that
            # name now.  Settled first, and never by raising the clocks under
            # a game that may be running.
            alive = same or _exe_running(left["exe"]) is not False
            done = _settle_leftover(left, now, judge=restarted or not alive, alive=alive)
            if not done.get("settled"):
                unsettled = left

    reason, prof, game = _gate(exe, pid)
    name = str(display_name or (game or {}).get("name") or (adopt or {}).get("name") or exe)

    def not_taken(why: str, kept=None) -> dict:
        if kept is not None:
            _schedule_recheck()             # the file still says what the card is owed: looked at again later
        _remember(exe, name, why)
        log.info(f"Adaptive Tuning: {name} is not looked after ({why})")
        return {"engaged": False, "reason": why}

    if reason:
        if adopt is not None:
            # Not looked after this time: what that run left on the card is
            # put back now, unless that would raise the clocks under the game.
            done = _settle_leftover(adopt, now, judge=False, alive=True)
            if not done.get("settled"):
                unsettled = adopt
        return not_taken(reason, unsettled)

    # The app's own start may still be putting the saved overclock on the
    # card: what is read now could be "nothing", and that would be handed back
    # at the exit.  Waited for once; if it never comes, later starts do not
    # wait again.
    if not _startup_waited[0]:
        try:
            gpu_overclock.wait_startup_apply(STARTUP_WAIT_S)
        except Exception as e:
            log.debug(f"Adaptive Tuning: wait_startup_apply failed: {e}")
        _startup_waited[0] = True

    # An Apply or a Reset on the GPU page counts itself at once and writes the
    # card seconds later.  A session that started in between took the count as
    # its starting point and the old values as "before": the user's change was
    # written over and the old values handed back.  So: the counts first, then
    # wait for a write that is under way, and only then look at the card.  A
    # write that begins after this is seen as a take-over at the first poll.
    counts = _write_counts()
    try:
        gpu_overclock.wait_external_writes(EXTERNAL_WAIT_S)
    except Exception as e:
        log.debug(f"Adaptive Tuning: wait_external_writes failed: {e}")

    # What a leftover says was on the card before it is only taken over while
    # the card still holds something that session wrote.  A card that reads
    # anything else was changed since: handing the old "before" back at the
    # exit would write over that.
    owed = None
    changed_meanwhile = False
    if adopt is not None and adopt["held"] not in ("manual", "hot", "other_program"):
        if not adopt["writes"]:
            _settle_leftover(adopt, now, judge=False, alive=True)     # it had written nothing: nothing is owed
            adopt = None
        else:
            card = _read_card()
            if card is None:
                return not_taken("card_unreadable", adopt)
            changed_meanwhile = not any(_same(card, w) for w in adopt["writes"])
    elif unsettled is not None:
        card = _read_card()
        if card is None:
            return not_taken("card_unreadable", unsettled)
        if any(_same(card, w) for w in unsettled["writes"]):
            owed = unsettled                # still not put back: this session takes over what is owed
        else:
            _delete_session_file(unsettled["path"])     # the card holds something else by now: nothing to undo

    pre_held = True
    if adopt is not None:
        pre, pre_held = adopt["pre"], adopt["pre_held"]
    elif owed is not None:
        pre, pre_held = owed["pre"], owed["pre_held"]
    else:
        held = gpu_overclock.get_applied_oc() or {}
        if held.get("active"):
            pre = [int(held.get("core") or 0), int(held.get("mem") or 0), int(held.get("power") or 100)]
        else:
            card = _read_card(verified=True)
            if card is None:
                return not_taken("card_unreadable")
            # Not something the app was holding (another tool put it there,
            # or an earlier run of the app): handed back as found, power limit
            # included, and not held afterwards either.
            pre, pre_held = [card[0], card[1], _power_pct_now()], False

    carried = adopt if adopt is not None else owed
    if gpu_overclock.is_tuning_active():    # a test on the GPU page began while this start was waiting
        return not_taken("test_running", carried)

    with _state_lock:
        st = _load_state()
        game = st["games"].get(exe)
        if game is None:
            game = policy.new_game(exe, name, now)
            st["games"][exe] = game
        elif display_name:
            game["name"] = name
        level, lal = _lvl(game), game.get("limit_at_level")
        _recall_tail(exe, st)
        s = _new_session(exe, name, pid, now, pre, level, lal, (0, 0), counts=counts, pre_held=pre_held, mark=mark)
        if adopt is not None:
            s.update(started=adopt["started"] or now, adopted_at=now, writes=[list(w) for w in adopt["writes"]],
                     held=adopt["held"], power_touched=adopt["power_touched"], play_s=adopt["play_s"],
                     lim=list(adopt["lim"]))
            # The same run of the game as before the app was started again
            # (unless it is found under another process now).
            s["launches"] = [s["started"]] + ([now] if pid and adopt["pid"] and int(pid) != adopt["pid"] else [])
            if s["held"] == "released":
                s["held"] = adopt["released_from"]      # found again: looked after again
            if s["power_touched"]:
                s["power_set"] = int(prof["power"])
            if s["held"]:
                s["held_seq"] = _others_count()         # (what the user does from here on is a change)
        elif owed is not None:
            # What that session wrote is this one's to undo now.  (Its power
            # limit is not known any more, so the hand-back sets it again.)
            s.update(writes=[list(w) for w in owed["writes"]], power_touched=owed["power_touched"])
        hot_now = _cooldown_active()
        if not (s["held"] or changed_meanwhile or hot_now):
            beside = _eased_beside(exe, level, st["games"])
            if beside is not None and _go_down_beside(s, beside):
                _history(st, exe, name, RUNS_BESIDE(name, beside[3], beside[0]), now)
        s["values"] = _level_values(_bounded(s, prof), s["level"], s["lal"])
        # The session file is on disk before anything is written to the card.
        # A file that was already there (carried on, or owed) is only written
        # over once the game's record is saved: until then it is all that
        # says what the card is owed, and a save that fails must not lose it.
        if carried is None:
            _save_session(s)
        try:
            _save_state(st)
            if carried is not None:
                _save_session(s)
        except Exception:
            if carried is None:
                _delete_session_file()      # made by this call, and nothing was written to the card
            else:
                _schedule_recheck()
            raise
        _sess = s
    if carried is not None and carried["path"] != session_path():
        _delete_session_file(carried["path"])
    _remember(exe, name, "")
    if s["held"] is None and changed_meanwhile:
        _hold(s, "manual", now, text=CHANGED_MEANWHILE)
    if s["held"] is None and hot_now:
        # The temperature ceiling has just taken the overclock off: it stays
        # off for this game.  (The write would be refused anyway; held, the
        # page says why, and nothing is put on later behind the user's back.)
        # When an overclock is on the card again all the same, somebody put
        # it back since: that is theirs, and "with no overclock" would not be
        # true, so the session is left alone as for any change by the user.
        if adopt is not None:
            _hold(s, "hot", now)
        elif _same(pre, (0, 0)):
            _hold(s, "hot", now, text=HOT_AT_START)
        else:
            _hold(s, "manual", now, text=OWN_AFTER_HOT)
    if s["held"] in (None, "other_program"):
        # The drift watchdog would re-assert its old target over the session's
        # values: paused for as long as the session holds the card.
        _pause_watchdog()
    if s["held"] is None:
        r = _write("engage", s, skip_if_held=True)
        if r.get("superseded") or r.get("refused") == "taken over":
            _hold(s, "manual", now)         # somebody else wrote the clocks at this very moment
        else:
            s["applied_ok"] = bool(r.get("ok"))
        _save_session_quiet(s)
    s["poll_end"] = _now()                  # (the waits and the write above are not a PC that slept)
    try:
        _start_thread(s)
    except Exception as e:                  # the session still ends with the game; it is only not watched
        log.error(f"Adaptive Tuning: the watch thread could not be started: {e}")
    log.info(f"Adaptive Tuning: looking after {name} ({exe}) at {policy.level_phrase(s['level'])}: "
             f"core{s['values'][0]:+d} / mem{s['values'][1]:+d}, before it core{pre[0]:+d} / mem{pre[1]:+d}"
             + (" (session carried on)" if adopt is not None else ""))
    return _engaged(s, adopted=adopt is not None)


def _look_after_again(s: dict, display_name, pid, now: float, mark=None):
    """The game of a session that had been let go of ("released") was found
    again: the monitor was started again, or the switch was turned back on.
    It is looked after again from now; nothing from the time in between is
    counted.  Caller holds the session lock.  Returns the answer for
    on_game_start, or None when this is another run of the game whose record
    is gone (the session was closed: a fresh start follows)."""
    exe = s["exe"]
    reason, prof, game = _gate(exe, pid)
    name = str(display_name or s["name"])
    if reason:
        log.info(f"Adaptive Tuning: {name} is still not looked after ({reason})")
        return {"engaged": False, "reason": reason}
    if game is None:
        _close_locked(s, ended=now, by="release")
        _finish(s, classify=False)
        return None
    if pid and s["pid"] and int(pid) != s["pid"]:
        s["launches"].append(now)           # another process: the game was started again
    if pid:
        s["pid"] = int(pid)
    if display_name:
        s["name"] = name
    s["adopted_at"] = now
    s["count_from"] = s["judged_until"] = now
    s["pending"], s["other_seen"], s["set_back"] = [], None, None
    if mark is not None:
        s["rec_mark"] = mark
    if _end_kind(s) == "manual":
        s["held"] = "manual"                # the user set something themselves meanwhile: that stays
    else:
        s["held"], s["released_from"] = s["released_from"], None
        if s["held"]:
            s["held_seq"] = _others_count()
    s["level"], s["lal"] = _lvl(game), game.get("limit_at_level")
    if s["held"] is None:                   # (as a poll would find it)
        if _ceiling_reset_during(s) or _cooldown_active():
            _hold(s, "hot", now)
        elif _taken_over(s):
            _hold(s, "manual", now)
    beside = _eased_beside(exe, s["level"]) if s["held"] is None else None
    said = beside is not None and _go_down_beside(s, beside)
    if s["held"] in (None, "other_program"):
        _pause_watchdog()
    if s["held"] is None and _level_values(_bounded(s, prof), s["level"], s["lal"]) != tuple(s["values"]):
        r = _write("looked after again", s)             # its level was changed meanwhile
        if r.get("superseded") or r.get("refused") == "taken over":
            _hold(s, "manual", now)
        elif not r.get("refused"):
            s["applied_ok"] = bool(r.get("ok"))
            s["reasserts"] = 0
    _save_session_quiet(s)
    if said:
        _add_history(exe, s["name"], RUNS_BESIDE(s["name"], beside[3], beside[0]), now)
    _remember(exe, s["name"], "")
    log.info(f"Adaptive Tuning: looking after {s['name']} ({exe}) again")
    return _engaged(s, adopted=True)


def _start_thread(s: dict) -> None:
    if not _WATCH_THREAD:
        return
    t = threading.Thread(target=_watch, args=(s["id"], s["stop"]), daemon=True,
                         name=f"adaptive-tuning-{s['exe']}")
    s["thread"] = t
    t.start()


def try_engage_running_game() -> dict:
    """Look after the game that game mode is tracking right now, if there is
    one and nothing stands in the way.  Called when a test on the GPU page
    ends, and when the switch or a game's own switch is turned on.  Does
    nothing when no game is tracked; never raises."""
    try:
        info = _tracked_game()
        if not info or not info.get("exe"):
            return {"engaged": False, "reason": "no_game"}
        return on_game_start(info["exe"], info.get("display"), info.get("pid"))
    except Exception as e:
        log.warning(f"Adaptive Tuning: could not pick up the running game: {e}")
        return {"engaged": False, "reason": "error", "err": str(e)}


def _tracked_game():
    """{"exe", "display", "pid"} of the game that game mode is tracking, or None."""
    try:
        from core import game_profiles
        return game_profiles.get_active_game_info()
    except Exception as e:
        log.debug(f"Adaptive Tuning: game mode could not be asked: {e}")
        return None


def _watching() -> bool:
    try:
        from core import game_profiles
        return bool(game_profiles._engine.get("monitoring"))
    except Exception:
        return False


def _monitor_starting() -> bool:
    """The app's start-up task has the game monitor on its way."""
    try:
        from core import game_profiles
        return bool(game_profiles.monitor_starting())
    except Exception:
        return False


def _tracked_pid(exe: str):
    """The process id game mode tracks, when it is tracking `exe`; else None."""
    info = _tracked_game() or {}
    return info.get("pid") if _norm(info.get("exe")) == exe else None


def session_active() -> bool:
    """Is a session open (also one that is being left alone)?  Asked by the
    GPU page's tests and by Idle Mode at every poll: no lock, no file."""
    return _sess is not None


def judging_driver_errors() -> bool:
    """Is a session open that Adaptive Tuning still looks after (not one it
    is leaving alone)?  Then a reset of the graphics driver during it is
    counted by the session's own watch thread, and nothing else should take
    the overclock off on that account.  No lock, no file."""
    s = _sess
    return s is not None and not s["held"] and not s["stop"].is_set()


def note_manual_change() -> None:
    """The user is about to change the clocks on the GPU page: apply, reset,
    emergency reset or profile delete (called BEFORE that write).  Saving a
    profile is not noted: it writes no clock, and the session follows a lower
    saved overclock by itself.  The open session then leaves the card alone
    until the game closes.  The note is kept in the session itself, so without
    a session it does nothing and it cannot reach the next one."""
    s = _sess
    if s is not None:
        s["manual_note"] = True


# ── a session ends ────────────────────────────────────────────────────
def _close_locked(s: dict, ended: float, by: str) -> dict:
    """The first half of every end: stop the watch thread and hand the card
    back.  Caller holds the session lock.  The hand-back is in a `finally`:
    whatever else goes wrong, the card is not left on the session's values
    with the drift watchdog paused.  The thread is told to stop and is never
    waited for here (it may be the caller, or waiting for this lock); it
    checks its stop flag under this lock before any write."""
    global _sess, _last_closed
    hb = {"settled": False, "verified": False, "wrote": False, "left": None}
    try:
        s["stop"].set()
        s["ended"], s["closed_by"] = ended, by
    finally:
        try:
            hb = _hand_back(s)
        except Exception as e:
            log.error(f"Adaptive Tuning: handing the card back failed: {e}")
        with _state_lock:
            if _sess is s:
                _sess = None
        _last_closed = {"exe": s["exe"], "ended": ended, "by": by}
        if hb.get("settled"):
            _delete_session_file()
        else:
            # Not confirmed back: the file stays, so the next start (or this
            # game's next launch) still knows what was on the card before.
            try:                            # (judged and counted by the second half of this end, not again later)
                _write_json(session_path(), dict(_session_record(s), judged=True, play_s=0.0))
            except Exception as e:
                log.warning(f"Adaptive Tuning: the session file could not be kept: {e}")
            _add_history(s["exe"], s["name"], NOT_PUT_BACK(s["name"]), ended)
    s["handback"] = hb
    return hb


def on_game_exit(exe, exit_ts=None) -> dict:
    """Game mode saw the game close (`exit_ts`: when).  Returns {"handled": bool};
    when it is False nothing was touched and the caller's own crash check runs.

    In two halves.  At once, under the lock a start waits for: the watch
    thread is stopped and the card handed back.  Afterwards, off the lock: the
    graphics driver errors the session had not judged yet are judged (as "the
    game closed" when the exit came within two minutes of them), a crash with
    no driver error is noted, the play time is added.  If the game is already
    running again by then and its level went down, the new session is lowered
    at once."""
    global _last_game
    key = _norm(exe)
    after = carried = None
    try:
        with _session_lock:
            now = _now()
            ts = now if exit_ts is None else float(exit_ts)
            if _last_game and _last_game["exe"] == key:
                _last_game = None
            s = _sess
            if s is None or s["exe"] != key:
                _unforget(key)              # (the run of a game the user had forgotten is over)
                done = _last_closed
                if done and done["exe"] == key and done["by"] == "exit" and -60.0 <= ts - done["ended"] <= 180.0:
                    return {"handled": True, "already": True}       # the watch thread saw it go first
                if s is None and _recheck_scheduled[0]:
                    _defer(lambda: _recheck_kept(timer=False))      # a kept file may be this game's: settled now
                return {"handled": False, "reason": "no session for this game"}
            if ts < s["started"]:
                return {"handled": False, "reason": "a newer session is running"}
            if s["adopted_at"] is not None and ts < s["adopted_at"]:
                # The game was started again before this exit was reported and
                # its session carried on.  It stays open; the errors before
                # that exit are judged next, off this lock, as "the game
                # closed" (not for a session that is being left alone: it
                # counts nothing).
                if s["held"]:
                    _save_session_quiet(s)
                    return {"handled": True, "kept_open": True}
                carried = s
            else:
                hb = _close_locked(s, ended=ts, by="exit")
                after = s
        if carried is not None:
            _defer(lambda: _judge_ended(carried, ts))
            return {"handled": True, "kept_open": True}
        _defer(lambda: _finish(after, classify=True))
        return {"handled": True, "restored": bool(hb.get("verified")), "left": hb.get("left")}
    except Exception as e:
        log.warning(f"Adaptive Tuning: on_game_exit({key}) failed: {e}")
        return {"handled": after is not None or carried is not None, "err": str(e)}


def _release_locked(s: dict, reason: str, said=None) -> dict:
    """End a session for any reason that is not the game closing: the app is
    quitting or updating, game mode was put back by hand, the monitor was
    stopped, the switch (or the game's own) was turned off, the game was
    forgotten.  The card is handed back and the play time added, but nothing
    is judged as a game exit: a driver error from the last two minutes is NOT
    "the game closed after it".

    A release never raises the clocks under a game that is still running.
    When handing back would do that (the game was eased, and what was on the
    card before it is more), the session is not closed: it goes to the hold
    "released" (answer "kept_open" True).  Its values stay on the card, the
    drift watchdog stays paused, nothing more is judged or written, and the
    card is handed back when the game closes (the watch thread sees that), or
    at the next start of the app if the app quits first (the session file
    stays).  When the game is found again it is looked after again.

    `said`: the line for "What happened" when the session is kept like that
    (default: game mode was turned off)."""
    now = _now()
    if _exe_running(s["exe"]) is not False and _would_raise(s):
        if s["held"] != "released":
            for inc in list(s["pending"]):  # seen but not judged yet: a warning at most, as at any release
                s["pending"].remove(inc)
                try:
                    _decide(s, inc, False, now, may_ease=False, no_oc=_no_overclock_errors(fresh=False))
                except Exception as e:
                    log.warning(f"Adaptive Tuning: an incident of {s['name']} could not be recorded: {e}")
            _hold(s, "released", now, text=said or policy.HELD_RELEASED)
            log.info(f"Adaptive Tuning: {s['name']} is still running, so its overclock stays as it is until it "
                     f"closes ({reason})")
        return {"settled": False, "verified": False, "wrote": False, "left": "released", "kept_open": True}
    hb = _close_locked(s, ended=now, by="release")
    _finish(s, classify=False)
    log.info(f"Adaptive Tuning: stopped looking after {s['name']} ({reason})")
    return hb


def end_active_session(reason: str = "") -> dict:
    """Let go of the open session without judging it (see _release_locked).
    {"ended": True} when it was closed and the card handed back; {"ended":
    False, "released": True} when its game is still running and handing back
    would have raised the clocks under it (it stays open, left alone, until
    the game closes).  Does nothing when none is open.  Never raises."""
    global _last_game
    try:
        with _session_lock:
            _last_game = None
            s = _sess
            if s is None:
                return {"ended": False, "reason": "no active session"}
            hb = _release_locked(s, reason or "released")
            if hb.get("kept_open"):
                return {"ended": False, "released": True, "exe": s["exe"], "left": "released",
                        "reason": "the game is still running"}
            return {"ended": True, "exe": s["exe"], "restored": bool(hb.get("verified")), "left": hb.get("left")}
    except Exception as e:
        log.warning(f"Adaptive Tuning: end_active_session({reason}) failed: {e}")
        return {"ended": False, "err": str(e)}


def clear_runtime_if_orphaned() -> dict:
    """A session whose game is definitely no longer running is closed as a
    game exit (the watch thread does the same after three polls)."""
    after = None
    try:
        with _session_lock:
            s = _sess
            if s is None:
                return {"orphan": False}
            if _exe_running(s["exe"]) is not False:
                return {"orphan": False, "active_exe": s["exe"]}
            _close_locked(s, ended=_now(), by="exit")
            after = s
        _defer(lambda: _finish(after, classify=True))
        return {"orphan": True, "cleared_exe": after["exe"]}
    except Exception as e:
        log.warning(f"Adaptive Tuning: orphan check failed: {e}")
        return {"orphan": False, "err": str(e)}


def _finish(s: dict, classify: bool) -> None:
    """The second half of an end: the record.  Never raises.

    classify True (a real game exit, and no lock is held): the driver errors
    up to the exit that were not judged yet are read and judged; those the
    exit followed within two minutes count as "the game closed".  When that
    read fails it is tried again later (see _retry_exit_read).  classify
    False (a release): nothing new is read, and an error the watch thread had
    seen but not yet judged is kept as a warning at most."""
    exe, name = s["exe"], s["name"]
    unread = False
    try:
        if not s["held"]:
            if classify:
                _settle_clock(s)
                try:
                    _collect_events(s, upto=s["ended"])
                except Exception as e:
                    unread = True
                    log.warning(f"Adaptive Tuning: the event log could not be read at the end of {name} "
                                f"(it is tried again later): {e}")
                    if s.get("other_seen") is None:     # (looked at now: the later look cannot know any more)
                        s["other_seen"] = _other_game_running(exe, name)
            _judge_at_end(s, classify, unread)
    except Exception as e:
        log.warning(f"Adaptive Tuning: judging the end of {name} failed: {e}")
    try:
        with _state_lock:
            st = _load_state()
            game = st["games"].get(exe)
            # (the game has closed: a run of it the user had forgotten is over)
            over = s["closed_by"] == "exit" and st["forgotten"].pop(exe, None) is not None
            if game is not None:
                policy.add_play(game, s["play_s"] / 60.0, now=s["ended"], sessions=1)
                s["play_s"] = 0.0
                st["last_sessions"][exe] = {"ended": s["ended"], "line": _report_line_of(s, game, unread)}
            if game is not None or over:
                _save_state(st)
    except Exception as e:
        log.warning(f"Adaptive Tuning: the play time of {name} could not be saved: {e}")
    _follow_record(exe)
    if unread:
        _retry_exit_read(s, 0)


def _report_line_of(s: dict, game: dict, unread: bool = False):
    """The after-game report's line for a session that has ended.  `unread`:
    the event log could not be looked at yet, so there is no line that says
    "no problems" (the later look writes the line)."""
    # (what the hand-back found first: a session that was being left alone
    # may have been taken over by the user since)
    held = (s.get("handback") or {}).get("left") or s["held"]
    trouble = s.get("trouble") or ("not looked at yet" if unread else None)
    return policy.REPORT_LINE(_lvl(game), eased_to=s["eased_to"], held=held, trouble=trouble)


def _judge_at_end(s: dict, classify: bool, unread: bool = False) -> None:
    """Judge what a session had seen and not yet judged when it ended (see
    _finish for `classify`).  `unread`: the look at the event log failed, so
    whether the game closed with no driver error cannot be said yet."""
    name = s["name"]
    for inc in list(s["pending"]):
        s["pending"].remove(inc)
        if _num(inc["first_ts"]) > s["ended"]:
            continue                        # logged after the game was gone: not this session's
        s["judged_n"] = s.get("judged_n", 0) + 1
        try:
            if classify:
                _decide(s, inc, s["ended"] - _num(inc["last_ts"]) <= ENDED_WINDOW_S, _now())
            else:
                _decide(s, inc, False, _now(), may_ease=False, no_oc=_no_overclock_errors(fresh=False))
        except Exception as e:
            log.warning(f"Adaptive Tuning: an incident of {name} could not be recorded: {e}")
    if not classify or unread or s.get("judged_n"):
        return
    ab = s.get("absorbed")
    if ab is not None and 0.0 <= s["ended"] - ab["last_ts"] <= ENDED_WINDOW_S:
        # The game closed on errors that went on from an incident judged
        # earlier: that is not a crash "with no graphics driver error".  In a
        # later run of the game than that incident's it is the game failing
        # again, and it is judged as that.  In the same run it is the same
        # incident, which has been judged.
        if ab["later_launch"]:
            s["judged_n"] = 1
            if s.get("other_seen") is None:
                s["other_seen"] = ab["other"]
            try:
                _decide(s, {"first_ts": ab["first_ts"], "last_ts": ab["last_ts"], "count": ab["count"]}, True, _now())
            except Exception as e:
                log.warning(f"Adaptive Tuning: an incident of {name} could not be recorded: {e}")
        return
    _note_plain_crash(s)


def _retry_exit_read(s: dict, attempt: int) -> None:
    """The look at the event log at the end of a session failed (seconds
    after a driver reset the log is often slow to answer): it is made again
    later, a few times.  The session keeps where it had got to."""
    if attempt >= len(EXIT_READ_RETRIES_S):
        log.warning(f"Adaptive Tuning: the event log could not be read after {s['name']} closed; "
                    "its last minutes were not judged")
        return
    try:
        _defer_later(EXIT_READ_RETRIES_S[attempt], lambda: _finish_late(s, attempt))
    except Exception as e:
        log.warning(f"Adaptive Tuning: the later look at the end of {s['name']} could not be set up: {e}")


def _finish_late(s: dict, attempt: int) -> None:
    """One more look at how a session ended (see _retry_exit_read).  Never
    raises.  Whether another game was running is what was seen at the exit."""
    try:
        try:
            _collect_events(s, upto=s["ended"], look_around=False)
        except Exception as e:
            log.debug(f"Adaptive Tuning: {e}")
            _retry_exit_read(s, attempt + 1)
            return
        _judge_at_end(s, True)          # (a game that is running again by now is lowered at once: see _decide)
        with _state_lock:
            st = _load_state()
            game, last = st["games"].get(s["exe"]), st["last_sessions"].get(s["exe"])
            if game is not None and last is not None and last.get("ended") == s["ended"]:
                line = _report_line_of(s, game)
                if line != last.get("line"):
                    last["line"] = line
                    _save_state(st)
    except Exception as e:
        log.warning(f"Adaptive Tuning: the later look at the end of {s['name']} failed: {e}")


def _note_plain_crash(s: dict) -> None:
    """The game closed with a crash record of its own and no graphics driver
    error: remembered, shown, never a reason to change anything."""
    now = _now()
    r = gpu_events.query(s["ended"] - CRASH_LOOKBACK_S, until=max(now, s["ended"]), kinds="app_crash")
    if not r.get("ok"):
        return
    hit = False
    for e in r.get("events") or ():
        if e.get("ts", 0) < s["started"]:
            continue
        if e.get("exe") == s["exe"] or (s["pid"] and e.get("pid") == s["pid"]):
            hit = True
            break
    if not hit:
        return
    with _state_lock:
        st = _load_state()
        game = st["games"].get(s["exe"])
        if game is None:
            return
        policy.add_plain_crash(game, s["ended"], _num(game.get("minutes")) + s["play_s"] / 60.0)
        _history(st, s["exe"], game.get("name") or s["name"], policy.PLAIN_CRASH(game.get("name") or s["name"]),
                 s["ended"])
        _save_state(st)
    s["trouble"] = s.get("trouble") or "crash"


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ── graphics driver errors ────────────────────────────────────────────
def _read_events(since: float, upto: float) -> list:
    """The driver errors Windows logged from `since` to `upto`.  Raises
    StateError when the event log cannot be read.  A read can take seconds
    (15 at most): made with no lock held wherever a session is open."""
    r = gpu_events.query(since, until=upto, kinds="driver_error")
    if not r.get("ok"):
        raise StateError(f"the event log could not be read: {r.get('err')}")
    return list(r.get("events") or ())


def _is_new(s: dict, e: dict, upto: float) -> bool:
    """Is this record one the session has not taken yet, and its own?  Each
    record is taken once, by channel and record id; one from before the
    session, or from after `upto`, is not its own; and neither is one Windows
    wrote before the session began whatever time it carries (see _log_mark)."""
    ts = _num(e.get("ts"))
    if (e.get("channel"), e.get("record_id")) in s["seen_records"] or ts < s["count_from"] or ts > upto:
        return False
    mark = s.get("rec_mark")
    return not (mark is not None and e.get("channel") == "System" and _num(e.get("record_id")) <= mark)


def _test_windows(grace_s=None) -> list:
    """[(start, end)] of the tests that ran on the GPU page (a test crashes
    the driver on purpose, so its errors are nobody's), each end with
    gpu_overclock's own grace unless `grace_s` says otherwise.  [] when they
    cannot be read."""
    try:
        wins = gpu_overclock.tuning_windows() if grace_s is None else gpu_overclock.tuning_windows(grace_s=grace_s)
        return [(float(a), float(b)) for a, b in (wins or [])]
    except Exception as e:
        log.debug(f"Adaptive Tuning: the times of the tests on the GPU page could not be read: {e}")
        return []


def _in_chain(tail, ts: float) -> bool:
    """Does a driver error at `ts` go on from the incident judged last
    (`tail`)?  Errors chain while they come less than five minutes apart, for
    ten minutes from that incident's first error at most: a game that keeps
    failing every few minutes is not one endless incident."""
    return (tail is not None and tail["first"] <= ts and ts - tail["last"] < policy.BURST_GAP_S
            and ts - tail["first"] < policy.RESET_STORM_S)


def _keep_tail(exe: str, tail: dict) -> None:
    """Remember the incident judged last for a game: in memory, and quietly in
    the record, so that a restart of the app does not count what goes on from
    it a second time.  Never raises."""
    _burst_tail[exe] = tail
    try:
        with _state_lock:
            st = _load_state()
            if exe in st["games"] and st["tails"].get(exe) != tail:
                st["tails"][exe] = dict(tail)
                _save_state(st)
    except Exception as e:
        log.debug(f"Adaptive Tuning: what was judged last for {exe} could not be saved: {e}")


def _recall_tail(exe: str, st: dict) -> None:
    """Take the incident judged last for a game from the record when this run
    of the app does not know it, or knows an older one."""
    saved, mine = st["tails"].get(exe), _burst_tail.get(exe)
    if saved is not None and (mine is None or saved["last"] > mine["last"]):
        _burst_tail[exe] = dict(saved)


def _absorb(s: dict, tail: dict, ts: float, other=None) -> None:
    """A driver error that goes on from an incident already judged: not a new
    one.  It is noted on the session all the same.  If the game closes on it,
    that is not a crash "with no driver error"; and in a later run of the game
    than the judged incident's it is the game failing again (see
    _judge_at_end).  `other`: as in _take_events."""
    launches = s.get("launches") or ()
    ab = s.get("absorbed")
    if ab is None or ab["of"] != tail["first"] or any(ab["last_ts"] < b <= ts for b in launches):
        ab = {"first_ts": ts, "last_ts": ts, "count": 0, "of": tail["first"], "other": None,
              "later_launch": any(tail["judged"] < b <= ts for b in launches)}
    ab["last_ts"] = max(ab["last_ts"], ts)
    ab["count"] += 1
    if other is not None:
        ab["other"] = bool(ab["other"]) or bool(other)
    s["absorbed"] = ab
    _burst_tail[s["exe"]] = dict(tail, last=max(tail["last"], ts))      # (saved by the caller, once for all of them)


def _take_events(s: dict, events, upto: float, other=None) -> None:
    """Take driver errors that were read (see _read_events) into s["pending"].

    The look reaches two seconds back (a record can land a moment after its
    own time), so the same record comes back: each is taken once.  An error
    inside the time of a test on the GPU page is the test's, not the game's.
    An error that goes on from an incident already judged belongs to that
    incident and is not counted again, also when that was at the end of this
    game's previous session a moment ago (see _in_chain).  `other`: whether
    another game was seen running as these were read (None: nobody looked)."""
    fresh = []
    for e in events or ():
        if _is_new(s, e, upto):
            s["seen_records"].add((e.get("channel"), e.get("record_id")))
            fresh.append(e)
    if not fresh:
        return
    tests = _test_windows()
    fresh.sort(key=lambda e: _num(e.get("ts")))
    went_on = False
    for e in fresh:
        ts = _num(e.get("ts"))
        if any(a <= ts <= b for a, b in tests):
            continue
        tail = _burst_tail.get(s["exe"])
        if not s["pending"] and _in_chain(tail, ts):
            _absorb(s, tail, ts, other)
            went_on = True
            continue
        s["pending"] = policy.merge_bursts(list(s["pending"]) + [e])
        if other is not None:
            s["other_seen"] = bool(s.get("other_seen")) or bool(other)
    if went_on:
        _keep_tail(s["exe"], _burst_tail[s["exe"]])     # (one save, however many records a reset storm wrote)


def _collect_events(s: dict, upto: float, look_around: bool = True) -> None:
    """Read the driver errors since the last look into s["pending"] (see
    _take_events).  When any is new, who else is running is looked at too
    (`look_around` False: not now, the moment has passed).  Raises when the
    log cannot be read (nothing is then marked as looked at).  A read of the
    event log and a PowerShell call: for a caller that holds no lock."""
    events = _read_events(s["judged_until"] - 2.0, upto)
    other = None
    if look_around and any(_is_new(s, e, upto) for e in events):
        other = _other_game_running(s["exe"], s["name"])
    _take_events(s, events, upto, other)


def _mark_judged(s: dict, now: float) -> None:
    """Everything up to `now` has been looked at.  While an incident is still
    waiting, the mark stays just before it, so a run that dies here finds it
    again at the next start."""
    if s["pending"]:
        s["judged_until"] = min(_num(i["first_ts"]) for i in s["pending"]) - 0.001
    else:
        s["judged_until"] = now
        s["other_seen"] = None


def _clock_set_back(s: dict, by: float) -> None:
    """The wall clock was set back by about `by` seconds during the session.
    Everything the session keeps as a time of the old clock is moved with it,
    so that an error that was waiting is still judged two minutes after it
    and what Windows logs from here on, on the new clock, is still this
    session's.  Not when the session started or was found again: the
    temperature ceiling's last trip is compared with the first, and an exit
    reported for a time before it is closed by the watch thread within a
    minute, as before."""
    for inc in s["pending"]:
        inc["first_ts"], inc["last_ts"] = _num(inc["first_ts"]) - by, _num(inc["last_ts"]) - by
    s["count_from"] -= by
    s["judged_until"] -= by
    s["launches"] = [b - by for b in s.get("launches") or ()]
    ab = s.get("absorbed")
    if ab is not None:
        ab.update(first_ts=ab["first_ts"] - by, last_ts=ab["last_ts"] - by, of=ab["of"] - by)
    tail = _burst_tail.get(s["exe"])
    if tail is not None:
        _keep_tail(s["exe"], {k: v - by for k, v in tail.items()})
    log.info(f"Adaptive Tuning: the clock was set back by about {by:.0f} s while {s['name']} was running; "
             "what was waiting to be judged was moved with it")


def _settle_clock(s: dict) -> None:
    """A set-back of the clock that the last poll noticed and no look at the
    event log has dealt with yet (the game closed first): what Windows logged
    on the old clock is read, then the session's times are moved.  For a
    caller that holds no lock.  Never raises."""
    sb = s.get("set_back")
    if not sb:
        return
    s["set_back"] = None
    try:
        try:
            _take_events(s, _read_events(s["judged_until"] - 2.0, sb["upto"]), sb["upto"])
        except Exception as e:
            log.debug(f"Adaptive Tuning: {e}")
        _clock_set_back(s, sb["by"])
    except Exception as e:
        log.warning(f"Adaptive Tuning: the session's times could not be moved with the clock: {e}")


def _judge_ended(s: dict, ended: float, again: bool = True) -> None:
    """For a session that carried on over a relaunch: the errors up to the
    exit of the run before (`ended`) are judged, as "the game closed" when
    that exit followed them within two minutes.  Called with no lock held:
    the event log is read first, and what it held is taken in under the
    session lock, if this is still the open session and it has not been left
    alone meanwhile.  A read that fails is made once more half a minute
    later.  Never raises."""
    try:
        upto = max(ended, s["judged_until"])
        events = other = None
        try:
            events = _read_events(s["judged_until"] - 2.0, upto)
            if any(_is_new(s, e, upto) for e in events):
                other = _other_game_running(s["exe"], s["name"])
        except Exception as e:
            log.warning(f"Adaptive Tuning: the event log could not be read: {e}")
        no_oc = _no_overclock_errors()
        with _session_lock:
            if _sess is not s or s["stop"].is_set() or s["held"]:
                return
            now = _now()
            if events is not None:
                _take_events(s, events, upto, other)
            judged = 0
            for inc in list(s["pending"]):
                if _num(inc["first_ts"]) <= ended:
                    judged += 1
                    _decide(s, inc, ended - _num(inc["last_ts"]) <= ENDED_WINDOW_S, now, no_oc=no_oc)
                    if inc in s["pending"]:
                        s["pending"].remove(inc)
            ab = s.get("absorbed")
            if not judged and ab is not None and ab["later_launch"] and 0.0 <= ended - ab["last_ts"] <= ENDED_WINDOW_S:
                # (that run closed on errors that went on from an incident
                # judged in a run before it: the game failing again)
                if s.get("other_seen") is None:
                    s["other_seen"] = ab["other"]
                _decide(s, {"first_ts": ab["first_ts"], "last_ts": ab["last_ts"], "count": ab["count"]}, True, now,
                        no_oc=no_oc)
            if events is not None:
                _mark_judged(s, max(upto, s["judged_until"]))   # (a poll may have looked further meanwhile)
            _save_session_quiet(s)
        if events is None and again:
            _defer_later(EXIT_READ_RETRIES_S[0], lambda: _judge_ended(s, ended, again=False))
    except Exception as e:
        log.warning(f"Adaptive Tuning: judging the run of {s['name']} that closed failed: {e}")


def _since_change(game: dict, incident: dict):
    """Seconds from the change of level this incident has to be measured
    against to the incident, or None.

    That is the last time the driver's errors took this game down a level (a
    reset storm goes on for minutes, also across a relaunch, so this is read
    from the record, not the session).  It is also a change the user made at
    or after the incident began: those errors happened at the level before
    it and say nothing about the new one.  The answer is negative then, and
    the rules ignore the incident.  A change of the user's from before the
    incident is passed over."""
    try:
        first = float(incident["first_ts"])
    except (KeyError, TypeError, ValueError):
        return None
    changes = game.get("changes")
    for change in reversed(changes if isinstance(changes, list) else []):
        if not isinstance(change, dict):
            continue
        try:
            ts = float(change["ts"])
        except (KeyError, TypeError, ValueError):
            if change.get("by") == "driver":
                return None
            continue
        if change.get("by") == "driver" or ts >= first:
            return first - ts
    return None


def _windowed_games():
    """The processes with a visible main window that game mode would take as
    a game ([{"exe", "pid", "name"}], the one in front first), or None when
    that could not be looked at.  One PowerShell call, seconds under load:
    never made with the state lock held, and with the session lock only when
    nobody looked earlier."""
    try:
        from core import game_profiles
        return game_profiles.running_game_candidates()
    except Exception as e:
        log.debug(f"Adaptive Tuning: could not look for other games: {e}")
        return None


def _other_game_running(exe: str, name: str, records=None) -> bool:
    """Is another game being played beside this one?  Then a driver error
    cannot be pinned on either.

    Another game is a process with a visible window that game mode would take
    as a game, under another name.  A process that merely exists (a Java
    program, a game's client left open in the tray) is not one.  Neither is
      * a launcher, an updater, an installer or a helper by its name, also
        when the game list knows it;
      * a program that is a game only by a tell-tale word in its name, unless
        it has half an hour of play on record (a companion tool is not a game
        because its window was open once);
      * a second program of the same game (the same display name), or one
        that started this game or was started by it, however many steps back.
    False when the look failed: nothing can be said then.  `records`: the
    games on record (read here when left out and needed).  Never raises."""
    try:
        found = _windowed_games()
        if not found:
            return False
        from core import game_profiles
        parent, pids_of = {}, {}
        for p in process_tuning.processes() or ():
            parent[p.get("pid")] = p.get("ppid")
            pids_of.setdefault(str(p.get("exe") or "").lower(), set()).add(p.get("pid"))
        mine = pids_of.get(exe, set())

        def line(pid) -> list:              # a process and everything above it
            up = []
            while pid and pid not in up and len(up) < 64:
                up.append(pid)
                pid = parent.get(pid)
            return up

        above_me = {a for pid in mine for a in line(pid)}
        tried = set()
        for c in found:
            other = _norm(c.get("exe"))
            if not other or other == exe or other in tried:
                continue
            tried.add(other)
            if any(word in other for word in game_profiles._NOT_A_GAME):
                continue
            how = game_profiles._game_match(other)
            if how == "hint":
                if records is None:
                    with _state_lock:
                        records = _load_state()["games"]
                if _num((records.get(other) or {}).get("minutes")) < OTHER_GAME_PLAY_MIN:
                    continue
            elif how != "known":
                continue
            theirs = set(pids_of.get(other, ())) | ({c.get("pid")} if c.get("pid") else set())
            if any(pid in above_me or mine.intersection(line(pid)) for pid in theirs):
                continue
            other_name = str(c.get("name") or "")
            if other_name and other_name == name:
                continue
            return True
    except Exception as e:
        log.debug(f"Adaptive Tuning: could not look for other games: {e}")
    return False


def _no_overclock_errors(fresh: bool = True) -> bool:
    """Does the driver also log errors with no overclock on the card (rule 7
    keeps a game at 50% then)?  `fresh` False: from the last answer, however
    old, without a read of the event log (for a caller that holds the session
    lock); False when there is none."""
    health = _health() if fresh else (_health_cache["value"] or {})
    return bool((health.get("at_stock_90d") or 0) > 0)


def _decide(s: dict, incident: dict, ended_game: bool, now: float, may_ease: bool = True, no_oc=None):
    """One incident through the rules, into the record, and onto the card.

    `s` is the session it happened in (open or just closed).  may_ease False
    (a release): an "ease" is recorded as a counted warning instead.  `no_oc`:
    whether the driver also logs errors with no overclock on the card, when
    the caller has asked already (it is a read of the event log, so a caller
    that holds the session lock asks first or passes what is known).
    Whether another game was running is what was seen when the errors were
    read (s["other_seen"]); it is looked at here only when nobody did."""
    exe = s["exe"]
    prof = _bounded(s, _profile())          # (a profile raised mid-game is not what the game was eased against)
    limit = _limit(prof)
    if no_oc is None:
        no_oc = _no_overclock_errors()
    other = s.get("other_seen")
    if other is None:
        other = _other_game_running(exe, s["name"])
    first, last = _num(incident.get("first_ts")), _num(incident.get("last_ts"))
    with _state_lock:
        settings = _load_settings()
        st = _load_state()
        game = st["games"].get(exe)
        if game is None:
            return None                     # forgotten meanwhile: nothing to hold it against
        name = game.get("name") or s["name"]
        before = _lvl(game)
        ctx = {
            "sensitivity": settings["sensitivity"],
            "ended_game": bool(ended_game),
            "play_minutes_now": _num(game.get("minutes")) + s["play_s"] / 60.0,
            "stepped_this_session": bool(s["stepped"]),
            "since_last_change_s": _since_change(game, incident),
            "other_game_running": bool(other),
            "errors_with_no_overclock": bool(no_oc),
            "limit": limit,
        }
        d = policy.decide(game, incident, ctx)
        if d.get("action") == "ease" and not may_ease:
            text = policy.W_SURVIVED(name)
            d = dict(d, action="warning", to_level=before, would_be_level=before, history=text, toast=None,
                     problem={"kind": "warning", "text": text}, counted=True, flag=None)
        policy.apply(game, d, now, limit=limit)
        if d.get("history"):
            _history(st, exe, name, d["history"], first or now)
        # What was judged last, kept with the record (in the same save).  An
        # incident older than the one on record does not take its place.
        tail, new_tail = _burst_tail.get(exe), False
        if tail is None or last >= tail["last"]:
            tail, new_tail = {"first": first, "last": last, "judged": last}, True
        st["tails"][exe] = dict(tail)
        _save_state(st)
        after = _lvl(game)
    _burst_tail[exe] = tail
    if new_tail:
        s["absorbed"] = None                # (what had gone on from the incident before it is of no account now)
    if d.get("history"):
        s["trouble"] = "driver"             # (plain assignment: `s` may be the stand-in for a run that died)
    _health_reset()
    log.info(f"Adaptive Tuning: {name}: graphics driver error -> {d.get('action')} (rule {d.get('rule')})"
             + (f", now at {policy.level_phrase(after)}" if after != before else ""))
    if after != before:
        s["stepped"], s["eased_to"] = True, after
        _toast(d.get("toast"))
        _follow_record(exe)
    elif d.get("action") == "would_ease":
        _toast(d.get("toast"))
    return d


def _follow_record(exe: str) -> None:
    """If this game's session is open and its level on record went down,
    lower the card at once (the light way).  Used when the watch thread eases
    the game it is watching, and when the end of one session eases a game
    whose next session has already started."""
    with _session_lock:
        s = _sess
        if s is None or s["exe"] != exe:
            return
        try:
            with _state_lock:
                game = _load_state()["games"].get(exe)
        except Exception as e:
            log.warning(f"Adaptive Tuning: the record of {exe} could not be read: {e}")
            return
        if game is None:
            return
        level = _lvl(game)
        if level <= s["level"]:
            return                          # only the user moves a game up (step_game does that itself)
        s["level"], s["lal"] = level, game.get("limit_at_level")
        s["stepped"] = True                 # at most one step per session, also when the step was decided
                                            # at the end of the session before, a moment ago
        if not s["held"]:
            r = _write("ease", s, light=True, cap=s["values"])
            if r.get("superseded") or r.get("refused") == "taken over":
                _hold(s, "manual", _now())
            elif not r.get("refused"):
                s["applied_ok"] = bool(r.get("ok"))
                s["reasserts"] = 0
        _save_session_quiet(s)


# ── the watch thread ──────────────────────────────────────────────────
def _watch(sid: int, stop: threading.Event) -> None:
    """Wakes every POLL_S through its stop event (no sleeps), so a session
    that ends is noticed at once and never waited for."""
    while not stop.wait(POLL_S):
        try:
            _poll(sid)
        except Exception as e:
            log.warning(f"Adaptive Tuning: a poll failed: {e}")


def _poll(sid: int) -> None:
    """One look at the open session.  Everything that touches the card or the
    files, every write included, happens under the session lock and only
    after checking that the session with this id is still the open one and
    has not been told to stop.  The Windows event log is read after that with
    no lock held (see _look_at_the_log)."""
    after = look = None
    with _session_lock:
        s = _sess
        if s is None or s["id"] != sid or s["stop"].is_set():
            return
        try:
            look = _poll_locked(s, _now())
            if look is True:
                after, look = s, None
        finally:
            if not look:
                s["poll_end"] = _now()
                if _sess is s:
                    _save_session_quiet(s)  # the heartbeat (after the look at the event log, when there is one)
    if after is not None:
        _defer(lambda: _after_own_close(after))
    elif look:
        _look_at_the_log(s, look)


def _read_failed(why) -> None:
    """A look at the event log during play failed.  Nothing was marked as
    looked at, so nothing is lost if it reads again later; when it goes on
    failing the page says so (get_state: health.log_readable)."""
    _failed_reads[0] += 1
    if _failed_reads[0] == 1:
        log.warning(f"Adaptive Tuning: {why}")
    elif _failed_reads[0] == FAILED_READS_TO_SAY:
        log.warning(f"Adaptive Tuning: {why} (again: graphics driver errors cannot be noticed like this)")
    else:
        log.debug(f"Adaptive Tuning: {why}")


def _look_at_the_log(s: dict, look: dict) -> None:
    """Step (6) of a poll: graphics driver errors.  An incident waits two
    minutes after its last error (if the game closes in that time, the exit
    judges it); after that it is judged as one the game survived.

    The event log is read here with no lock held: a read can take seconds,
    and the game closing, the next game starting or the app quitting must not
    wait for it.  Who else is running (when an error is new) and what the
    driver does with no overclock (when something may be judged) are asked
    here too, for the same reason.  What was read is taken in under the
    session lock, and only when this is still the open session and it has not
    been left alone meanwhile.  A read that fails marks nothing as looked at."""
    now = look["now"]
    reads, failed, other, no_oc = [], None, None, None
    try:
        for since, upto in look["windows"]:
            reads.append((_read_events(since, upto), upto))
        if any(_is_new(s, e, upto) for events, upto in reads for e in events):
            other = _other_game_running(s["exe"], s["name"])
        if other is not None or s["pending"]:
            no_oc = _no_overclock_errors()
    except Exception as e:
        failed = e
    with _session_lock:
        try:
            if _sess is not s or s["stop"].is_set():
                return
            if failed is not None:
                _read_failed(failed)
                return
            _failed_reads[0] = 0
            if s["held"]:
                return
            back = look["set_back"]
            for i, (events, upto) in enumerate(reads):
                _take_events(s, events, upto, other)
                if back and i == 0:
                    # (what Windows logged on the old clock is in; now the
                    # session's times move to the new one)
                    _clock_set_back(s, back["by"])
                    s["set_back"] = None
            for inc in list(s["pending"]):
                if now - _num(inc["last_ts"]) >= PENDING_S:
                    _decide(s, inc, False, now, no_oc=no_oc)    # (if this fails the incident stays, and is tried again)
                    if inc in s["pending"]:
                        s["pending"].remove(inc)
                    if _sess is not s or s["held"]:
                        break
            _mark_judged(s, now)
        finally:
            s["poll_end"] = _now()
            if _sess is s:
                _save_session_quiet(s)      # the heartbeat


def _after_own_close(s: dict) -> None:
    """The second half of an end the watch thread found itself.  A session
    that had been let go of stood in the way of any other game: the one game
    mode tracks now is picked up."""
    _finish(s, classify=True)
    if s["held"] == "released":
        info = _tracked_game()
        if info and info.get("exe") and _norm(info.get("exe")) != s["exe"]:
            try_engage_running_game()


def _poll_locked(s: dict, now: float):
    """Everything of a poll but the look at the event log.  True when the
    session closed itself (the game is gone); a dict when the event log is to
    be looked at next (see _look_at_the_log); else False."""
    prev = s["last_poll"]
    if now < prev:
        # The wall clock was set back (a clock that ran hours fast and was
        # put right, say).  The times the session keeps are moved with it
        # once the log has been read up to where the old clock had got to.
        back = prev + POLL_S - now
        sb = s.get("set_back")
        s["set_back"] = {"upto": sb["upto"] if sb else prev + POLL_S, "by": (sb["by"] if sb else 0.0) + back}
        if s["first_miss"] is not None:
            s["first_miss"] -= back

    # (0) Is the game still there?  Game mode only reports an exit it saw
    #     itself: after "Force normal mode", with the monitor stopped, or when
    #     this session was picked up by hand, nobody would ever close it.
    alive = _exe_running(s["exe"])
    if alive is False:
        s["misses"] += 1
        if s["first_miss"] is None:
            s["first_miss"] = now
        if s["misses"] >= MISSES_TO_CLOSE:
            log.info(f"Adaptive Tuning: {s['name']} is no longer running; closing its session")
            _close_locked(s, ended=s["first_miss"], by="exit")
            return True
    else:
        s["misses"], s["first_miss"] = 0, None

    # (1) Heartbeat and play time.  A gap longer than a minute is a PC that
    #     slept, not play.  What the driver logs as the PC wakes (in that gap
    #     and in the minutes after it) says nothing about the game either:
    #     it is not counted.
    if alive is not False:
        s["play_s"] += min(max(0.0, now - prev), PLAY_GAP_CAP_S)
    if now - max(prev, _num(s.get("poll_end"))) > PLAY_GAP_CAP_S:
        s["count_from"] = max(s["count_from"], now + SLEEP_QUIET_S)
    s["last_poll"] = s["seen"] = now

    # (2) Has someone else taken the card?  The ceiling first: its reset also
    #     counts as "someone else wrote", and it must not read as the user
    #     (also not while that reset is still under way, and not when it trips
    #     a second time on a card that stays hot).
    if s["held"] is None:
        if _ceiling_reset_during(s):
            _hold(s, "hot", now)
        elif _taken_over(s):
            _hold(s, "manual", now)
    elif s["held"] in ("hot", "other_program", "released") and _user_changed_since_hold(s):
        # After it was left alone the user set something themselves: that
        # stays, also after the game.  (The drift watchdog is left as it is
        # until the session ends: its target may still be this session's.)
        s["held"] = "manual"

    # (3) Held: nothing is written and nothing is counted.
    if s["held"]:
        s["pending"], s["other_seen"], s["set_back"] = [], None, None
        s["judged_until"] = now
        return False

    prof = _profile()
    if prof["unreadable"]:
        return False                        # cannot tell what the limit is: do nothing this time
    if not prof["exists"]:
        _hold(s, "manual", now)             # the saved overclock was deleted: it is never written back
        return False
    prof = _bounded(s, prof)                # (also notes a saved overclock that was lowered)

    # An eased game running beside this one: down to its level (see _eased_beside).
    beside = _eased_beside(s["exe"], s["level"])
    if beside is not None and _go_down_beside(s, beside):
        _add_history(s["exe"], s["name"], RUNS_BESIDE(s["name"], beside[3], beside[0]), now)

    # (4) No write while the temperature ceiling is in its cooldown.
    if not _cooldown_active():
        want = _level_values(prof, s["level"], s["lal"])
        lower = (min(want[0], s["values"][0]), min(want[1], s["values"][1]))
        if lower != s["values"]:
            # The saved overclock is lower now than what the session holds: follow at once.
            r = _write("lowered profile", s, light=True, cap=s["values"])
            if r.get("superseded") or r.get("refused") == "taken over":
                _hold(s, "manual", now)
                return False
        else:
            # (5) Does the card still hold the session's values?
            card = _read_card()
            if card is not None and not _same(card, s["values"]):
                if s["reasserts"] >= REASSERTS_TO_HOLD:
                    _hold(s, "other_program", now)
                    return False
                r = _write("re-assert", s, light=True, cap=s["values"])
                if r.get("superseded") or r.get("refused") == "taken over":
                    _hold(s, "manual", now)
                    return False
                if not r.get("refused"):
                    s["reasserts"] += 1
                    if not r.get("ok") or s["reasserts"] >= 2:
                        s["applied_ok"] = False
            elif card is not None:
                s["reasserts"] = 0
                s["applied_ok"] = True

    # (6) Graphics driver errors: the event log is looked at next, with no
    #     lock held (see _look_at_the_log).  After a set-back of the clock:
    #     first what was logged up to where the old clock had got to, then
    #     the same stretch on the new one.
    look = {"now": now, "windows": [(s["judged_until"] - 2.0, now)], "set_back": None}
    sb = s.get("set_back")
    if sb:
        look["windows"] = [(s["judged_until"] - 2.0, sb["upto"]), (s["judged_until"] - sb["by"] - 2.0, now)]
        look["set_back"] = dict(sb)
    return look


# ── a run that died mid-game ──────────────────────────────────────────
def recover_interrupted_session() -> dict:
    """At the app's start, before anything else touches the card.  A session
    file still on disk means the last run ended without closing its session
    (Vispora crashed or was killed, the PC froze or lost power), or could not
    confirm its hand-back.

      * The session's game is still running (or that cannot be told): nothing
        is written and the file is kept ("kept" True in the answer).  Game
        mode will find the game and on_game_start() carries the session on,
        with what was on the card before it.  (Handing back here would put
        the full overclock under a game that had been eased.)  Until then the
        file is looked at again every 30 s: with the monitor off, or a game
        that closes before the monitor's first look, nobody else would ever
        put the card back in this run.
      * Windows was started after the session's last heartbeat: that game's
        process is gone, whatever runs under its name now, so the session is
        never kept for it.
      * Otherwise the card is put back if it still holds a value that session
        wrote, and what happened is judged once, from the Windows event log:
        driver errors between the last thing the session judged and a minute
        after its last heartbeat count as one incident that ended the game; a
        crash of the PC that Windows blames on the graphics driver counts too,
        but only when Windows went quiet together with the session; a plain
        unexpected shutdown is only noted.  A Vispora that is started again
        days later counts nothing that happened in between.

    A file the old tuner left (3.6.8, 3.7.0) is handed back once, by the same
    rules, and removed.  Nothing here raises the clocks under a game that may
    still be running: that waits until it has closed.

    The card is put back here, at once and under the lock.  What happened is
    read from the event log and judged after that, with no lock held and off
    the app's start (the log can take seconds to answer at login, and neither
    the start nor a game being found waits for it): "judged" is in the answer
    only when that was done by the time the answer is given.  When the log
    cannot be read at all just now, what happened is judged later: in a few
    minutes, and again at the next starts (see _keep_unread).  An end kept
    like that by an earlier start is looked at now, first."""
    try:
        gpu_overclock.tuning_windows_since()    # (the record of tests on the GPU page begins at the first start)
    except Exception as e:
        log.debug(f"Adaptive Tuning: the record of tests could not be opened: {e}")
    results, jobs, failed = [], [], None
    try:
        with _session_lock:
            if _sess is not None:
                return {"recovered": False, "reason": "a session is open"}
            now = _now()
            for path in (v1_session_path(), session_path()):
                rec = _read_session_file(path)
                if rec is not None:
                    results.append(_recover_one(rec, now, jobs))
            if any(r.get("kept") for r in results):
                _schedule_recheck()
    except Exception as e:
        log.warning(f"Adaptive Tuning: recovery of an interrupted session failed: {e}")
        failed = e
    try:
        _defer(lambda: _judge_after_the_start(jobs))
    except Exception as e:
        log.warning(f"Adaptive Tuning: what ended the last session could not be looked at: {e}")
    if failed is not None:
        return {"recovered": False, "reason": f"error: {failed}"}
    if not results:
        return {"recovered": False, "reason": "no interrupted session"}
    out = dict(results[-1])
    for key in ("recovered", "kept", "unread"):
        if any(r.get(key) for r in results):
            out[key] = True
    return out


def _judge_after_the_start(jobs) -> None:
    """With no lock held: the ends an earlier start could not judge (see
    _judge_unread), then what this start found.  Never raises."""
    _judge_unread()
    for job in jobs:
        job()


def _judge_leftover(s: dict, rec: dict, out: dict) -> None:
    """Judge what ended a run that died, once its card has been settled (`s`
    stands in for the session; `out` is the answer about it, which gains
    "judged", or "unread" when the event log could not be read and the end is
    kept for a later look).  For a caller that holds no lock.  Never raises."""
    try:
        got = _judge_interrupted(s, rec, _now())
        if got == "unread":
            out.update(judged=None, unread=True)
            _keep_unread(s, rec)
        else:
            out["judged"] = got
    except Exception as e:
        log.warning(f"Adaptive Tuning: judging the interrupted session failed: {e}")


def _recover_one(rec: dict, now: float, later=None) -> dict:
    running = _exe_running(rec["exe"])
    restarted = _windows_restarted_since(rec["seen"])
    if running is not False and not restarted and not rec["old_format"]:
        log.info(f"Adaptive Tuning: {rec['exe']} from the last run is still running; its session is kept")
        return {"recovered": False, "reason": "the game is still running", "kept": True, "exe": rec["exe"]}
    if restarted and running is not False:
        log.info(f"Adaptive Tuning: Windows was started after the last session of {rec['exe']}: it is not carried on")
    return _settle_leftover(rec, now, judge=True, alive=running is not False, later=later)


def _schedule_recheck() -> None:
    """A session file that was kept (its game is still running, or the card
    could not be asked) is looked at again in 30 s.  One timer at a time."""
    if _recheck_scheduled[0]:
        return
    _recheck_scheduled[0] = True
    try:
        _defer_later(KEPT_RECHECK_S, _recheck_kept)
    except Exception as e:
        _recheck_scheduled[0] = False
        log.warning(f"Adaptive Tuning: the later look at the kept session could not be set up: {e}")


def _recheck_kept(timer: bool = True) -> None:
    """A kept session file with no session behind it: while its game is still
    running it waits (the monitor may yet find the game and carry the session
    on); once the game is gone it is settled as at the app's start.  Ends when
    a session is open, the file is gone, or it was settled; a file that then
    still cannot be settled is left for the next game start or the next start
    of the app.  Never raises."""
    if timer:
        _recheck_scheduled[0] = False
    again, jobs = False, []
    try:
        with _session_lock:
            if _sess is not None:
                return                      # carried on, or what it owed was taken over
            rec = _read_leftover()
            if rec is None:
                return
            running = _exe_running(rec["exe"])
            restarted = _windows_restarted_since(rec["seen"])
            if running is not False and not restarted and not rec["old_format"] and not rec["judged"]:
                again = True
            else:
                out = _settle_leftover(rec, _now(), judge=True, alive=running is not False, later=jobs)
                again = bool(out.get("kept"))
                log.info(f"Adaptive Tuning: the session kept for {rec['exe']} was looked at again: "
                         f"{'settled' if out.get('settled') else out.get('reason')}")
    except Exception as e:
        log.warning(f"Adaptive Tuning: the later look at the kept session failed: {e}")
    for job in jobs:
        job()                               # (what happened to it: judged with no lock held)
    if again:
        _schedule_recheck()


def _settle_leftover(rec: dict, now: float, judge: bool, alive: bool = False, later=None) -> dict:
    """Finish a session file that has no session behind it.  Caller holds the
    session lock.

    `judge`: what happened to that run is judged from the event log.  With
    `later` (a list) that is not done here: a call that does it is added to
    the list, for the caller to make once it has let go of the lock (the read
    can take seconds).

    The card is only written when it still holds something that session put
    there.  A card that reads no overclock is left as it is: that is what a
    restarted PC looks like, and putting the old values on it would apply an
    overclock at start-up that nobody asked for.  Anything else on the card
    was set since and is not this session's to undo.

    `alive`: a process with that game's name is running (or that cannot be
    told).  Then the clocks are not raised here: the file is kept ("kept"
    True in the answer) and the card is put back once the game has closed."""
    exe, name = rec["exe"], rec["name"] or _display_name(rec["exe"])
    pre = rec["pre"]
    want = (int(pre[0]), int(pre[1]))
    out = {"recovered": False, "exe": exe, "to": want}
    settled = True
    try:
        if rec["held"] == "manual":
            out["reason"] = "the overclock had been changed by hand"
        elif not rec["writes"]:
            out["reason"] = "the session had written nothing"
        else:
            card = _read_card()
            if card is None:
                out["reason"] = "the card could not be read"
                settled = False
            elif _same(card, (0, 0)) or not any(_same(card, w) for w in rec["writes"]):
                out["reason"] = "the card no longer holds the session's values"
                out["card"] = card
            elif _same(card, want) and not rec["power_touched"]:
                out["reason"] = "the session left the card as it found it"
            elif alive and (want[0] > card[0] + TOLERANCE_MHZ or want[1] > card[1] + TOLERANCE_MHZ):
                out.update(reason="the game is still running", kept=True)
                settled = False
            else:
                power = int(pre[2]) if rec["power_touched"] else None
                verified = False
                for attempt in range(HANDBACK_TRIES):
                    if attempt:
                        _pause(HANDBACK_WAIT_S)
                    _write("handback", values=(want[0], want[1], power))
                    back = _read_card()
                    if back is not None and _same(back, want):
                        verified = True
                        break
                out.update(recovered=verified, **{"from": card})
                if verified:
                    if not rec["pre_held"]:
                        _drop_hold()        # the app was not holding that before the session: not afterwards either
                    _add_history(exe, name, policy.HANDED_BACK(name), now)
                    log.warning(f"Adaptive Tuning: the last run ended mid-game ({exe}) with core{card[0]:+d} / "
                                f"mem{card[1]:+d} on the card; put back core{want[0]:+d} / mem{want[1]:+d}")
                else:
                    out["reason"] = "the card could not be confirmed back"
                    settled = False
    except Exception as e:
        log.error(f"Adaptive Tuning: putting the card back after an interrupted session failed: {e}")
        out["reason"] = f"error: {e}"
        settled = False
    finally:
        _resume_watchdog()

    if not rec["judged"] and not rec["old_format"]:
        # (Who else was running when that run died cannot be known any more:
        # what runs now says nothing about it.)
        pseudo = {"exe": exe, "name": name, "pid": rec["pid"], "started": rec["started"], "count_from": rec["started"],
                  "play_s": rec["play_s"], "stepped": False, "eased_to": None, "held": rec["held"],
                  "lim": list(rec.get("lim") or [None, None]), "other_seen": False}
        unread = False
        if judge and not rec["held"] and later is None:
            try:
                out["judged"] = _judge_interrupted(pseudo, rec, now)
                if out["judged"] == "unread":
                    out["judged"], out["unread"], unread = None, True, True
            except Exception as e:
                log.warning(f"Adaptive Tuning: judging the interrupted session failed: {e}")
        try:
            with _state_lock:
                st = _load_state()
                game = st["games"].get(exe)
                if game is not None and pseudo["play_s"] > 0:
                    policy.add_play(game, pseudo["play_s"] / 60.0, now=rec["seen"], sessions=1)
                    _save_state(st)
                    pseudo["play_s"] = 0.0
        except Exception as e:
            log.warning(f"Adaptive Tuning: the play time of the interrupted session could not be saved: {e}")
        if unread:
            _keep_unread(pseudo, rec)
        if judge and not rec["held"] and later is not None:
            later.append(lambda: _judge_leftover(pseudo, rec, out))     # (its play time is in the record by now)

    out["settled"] = settled
    path = rec.get("path") or session_path()
    if settled:
        _delete_session_file(path)
    elif not rec["judged"] and not rec["old_format"]:
        try:                                # kept for the next start; never judged or counted twice
            data = _read_json(path) or {}
            data.update(judged=True, play_s=0.0)
            _write_json(path, data)
        except Exception as e:
            log.warning(f"Adaptive Tuning: the session file could not be marked: {e}")
    return out


def _judge_interrupted(s: dict, rec: dict, now: float):
    """What ended a session whose run died.  `s` stands in for the session.
    Returns "driver_error", "gpu_bugcheck", "unexpected_shutdown", None
    (nothing that counts), or "unread" when the event log could not be read:
    nothing is counted then, and the caller keeps it for a later look.

    Errors inside the time of a test on the GPU page are the test's.  Errors
    that go on from an incident the session had already judged (see
    _in_chain) are that incident, not a second one: only what is left counts,
    as one incident that ended the game."""
    exe = s["exe"]
    low, high = rec["judged_until"], rec["seen"] + RECOVERY_SLACK_S
    r = gpu_events.query(low, until=min(high, now), kinds="driver_error")
    if not r.get("ok"):
        log.warning(f"Adaptive Tuning: the event log could not be read ({r.get('err')}); nothing is counted yet")
        return "unread"
    times = sorted(_num(e.get("ts")) for e in r.get("events") or ()
                   if low < _num(e.get("ts")) <= high and _num(e.get("ts")) >= rec["started"])
    if times:
        tests = _test_windows()
        times = [t for t in times if not any(a <= t <= b for a, b in tests)]
    if times:
        try:
            with _state_lock:
                _recall_tail(exe, _load_state())
        except Exception as e:
            log.debug(f"Adaptive Tuning: what was judged last for {exe} could not be read: {e}")
        went_on = False
        while times and _in_chain(_burst_tail.get(exe), times[0]):
            tail = _burst_tail[exe]
            _burst_tail[exe] = dict(tail, last=max(tail["last"], times.pop(0)))
            went_on = True
        if went_on:
            _keep_tail(exe, _burst_tail[exe])
    if times:
        _decide(s, {"first_ts": times[0], "last_ts": times[-1], "count": len(times)}, True, now)
        return "driver_error"
    return _judge_pc_stop(s, rec, now, late=False)


def _keep_unread(s: dict, rec: dict) -> None:
    """The end of a session whose run died could not be judged, because the
    event log could not be read.  What is needed to judge it is kept in the
    record (the session file has done its work once the card is back) and
    looked at again: later in this run, and at the next starts of the app.
    Never raises."""
    try:
        item = _clean_unread({"exe": s["exe"], "name": s["name"], "pid": rec.get("pid"), "started": rec["started"],
                              "judged_until": rec["judged_until"], "seen": rec["seen"], "lim": s.get("lim"),
                              "tries": int(rec.get("tries") or 0) + 1})
        if item is None:
            return
        if item["tries"] >= UNREAD_TRIES:
            log.warning(f"Adaptive Tuning: the event log could not be read {item['tries']} times; "
                        f"the last session of {item['name']} that was cut short is not judged")
            return
        with _state_lock:
            st = _load_state()
            st["unread"] = (st["unread"] + [item])[-UNREAD_KEEP:]
            _save_state(st)
        _defer_later(UNREAD_LOOKS_S[min(item["tries"], len(UNREAD_LOOKS_S)) - 1], _judge_unread)
    except Exception as e:
        log.warning(f"Adaptive Tuning: the end of {s.get('name')} could not be kept for a later look: {e}")


def _judge_unread() -> None:
    """Look again at the ends that could not be judged when the card was put
    back (see _keep_unread).  Each is taken out of the record before it is
    judged, so that whatever happens next none is judged twice; one whose
    look fails again goes back in.  No lock is held for the reads.  Never
    raises."""
    if not _unread_busy.acquire(False):
        return
    try:
        with _state_lock:
            st = _load_state()
            waiting = st["unread"]
            if not waiting:
                return
            st["unread"] = []
            _save_state(st)
        for item in waiting:
            stand_in = {"exe": item["exe"], "name": item["name"], "pid": item["pid"], "started": item["started"],
                        "count_from": item["started"], "play_s": 0.0, "stepped": False, "eased_to": None,
                        "held": None, "lim": list(item["lim"]), "other_seen": False}
            try:
                got = _judge_interrupted(stand_in, item, _now())
            except Exception as e:
                log.warning(f"Adaptive Tuning: judging the interrupted session failed: {e}")
                continue
            if got == "unread":
                _keep_unread(stand_in, item)
            else:
                log.info(f"Adaptive Tuning: the session of {item['name']} that was cut short was judged: {got}")
    except Exception as e:
        log.warning(f"Adaptive Tuning: the later look at a session that was cut short failed: {e}")
    finally:
        _unread_busy.release()


def _judge_pc_stop(s: dict, rec: dict, now: float, late: bool):
    """Did the PC stop with the session, and does Windows blame the graphics
    driver?  The records about it are written at the NEXT start of Windows and
    carry that time, so they are tied to the session like this: they belong to
    the first start of Windows after the session's last heartbeat, and the
    System log holds nothing between five minutes after that heartbeat and
    that start (Windows went quiet together with the session)."""
    seen, name = rec["seen"], s["name"]
    # (only the System log's records are used below, so only that log is
    # read: the Application log failing to answer does not hold this up)
    r = gpu_events.query(seen, until=now, kinds=("gpu_bugcheck", "unexpected_shutdown", "boot"), channels="System")
    if not r.get("ok"):
        return None if late else "unread"
    events = [e for e in r.get("events") or () if _num(e.get("ts")) > seen]
    # Only the System log's records are written at the next start.  (A live
    # report in the Application log comes with a driver error of its own,
    # which the look above has already counted.)
    stops = [e for e in events if e.get("kind") in ("gpu_bugcheck", "unexpected_shutdown")
             and e.get("channel") == "System"]
    boots = sorted(_num(e.get("ts")) for e in events if e.get("kind") == "boot")
    if not stops:
        return None
    # A start of Windows without its own record (Fast Startup writes none) is
    # marked by the first shutdown record instead.
    first_stop = min(_num(e.get("ts")) for e in stops)
    boot = boots[0] if boots and boots[0] - BOOT_WINDOW_S <= first_stop else first_stop
    later = [b for b in boots if b > boot + BOOT_WINDOW_S]
    upper = min(boot + 900.0, (later[0] - BOOT_WINDOW_S) if later else boot + 900.0)
    mine = [e for e in stops if boot - BOOT_WINDOW_S <= _num(e.get("ts")) <= upper]
    if not mine:
        return None
    start_of_boot = min([boot] + [_num(e.get("ts")) for e in mine])
    if gpu_events.any_system_record(seen + SILENT_AFTER_S, start_of_boot - BOOT_LEAD_S):
        return None                         # Windows went on running long after the session: not its doing
    if any(e.get("kind") == "gpu_bugcheck" for e in mine):
        _decide(s, {"first_ts": seen, "last_ts": seen, "count": 1}, True, now)
        return "gpu_bugcheck"
    if late:
        return None
    _add_history(s["exe"], name, policy.PC_STOPPED(name), seen)
    # Windows can write its crash record a minute or two after this app has
    # started: one more look.
    _defer_later(LATE_LOOK_S, lambda: _judge_pc_stop(dict(s), rec, _now(), late=True))
    return "unexpected_shutdown"


# ── what the user asks for ────────────────────────────────────────────
def _answer() -> dict:
    return {"ok": True, "state": get_state()}


def set_settings(enabled=None, notify=None, sensitivity=None) -> dict:
    """Change the switch, "Tell me when it changes something", or how soon a
    game is eased.  Switching off lets go of the open session (the card is
    handed back, unless that would raise the clocks under the running game:
    see _release_locked); switching on takes on the game that is running."""
    if any(v is not None and not isinstance(v, bool) for v in (enabled, notify)):
        return {"ok": False, "err": ERR_SETTING}
    if sensitivity is not None and sensitivity not in SENSITIVITIES:
        return {"ok": False, "err": ERR_SETTING}
    tell = turned_on = False
    try:
        with _session_lock:
            with _state_lock:
                s = _load_settings()
                was = s["enabled"]
                if notify is not None:
                    s["notify"] = notify
                if sensitivity is not None:
                    s["sensitivity"] = sensitivity
                if enabled is not None:
                    s["enabled"] = enabled
                turned_on = enabled is True and not was
                if turned_on and not s["told_in_games"]:
                    prof = _profile()
                    if prof["exists"] and not prof["apply_on_startup"]:
                        s["told_in_games"] = tell = True
                _save_settings(s)
            if enabled is False and _sess is not None:
                _release_locked(_sess, "switched off", said=STOPPED_KEPT)
    except Exception as e:
        log.warning(f"Adaptive Tuning: set_settings failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    if tell:
        _toast(FIRST_ON_TOAST, always=True)
    if turned_on:
        try_engage_running_game()
    return _answer()


def get_settings() -> dict:
    """The saved settings (get_state() carries them too)."""
    try:
        with _state_lock:
            s = _load_settings()
        return {"ok": True, "enabled": s["enabled"], "notify": s["notify"], "sensitivity": s["sensitivity"],
                "upgrade_note": s["upgrade_note"], "old_switch_was_on": s["old_switch_was_on"]}
    except Exception as e:
        log.warning(f"Adaptive Tuning: the settings could not be read: {e}")
        return {"ok": False, "err": ERR_STATE}


def set_game_enabled(exe, enabled) -> dict:
    """A game's own switch.  Off: Adaptive Tuning leaves that game alone (its
    open session is let go of and the card handed back, unless that would
    raise the clocks under the running game: see _release_locked)."""
    key = _norm(exe)
    if not isinstance(enabled, bool):
        return {"ok": False, "err": ERR_SETTING}
    try:
        with _session_lock:
            with _state_lock:
                st = _load_state()
                game = st["games"].get(key)
                if game is None:
                    return {"ok": False, "err": ERR_NO_GAME}
                game["enabled"] = enabled
                _save_state(st)
                name = game.get("name") or key
            if not enabled and _sess is not None and _sess["exe"] == key:
                _release_locked(_sess, "switched off for this game", said=STOPPED_KEPT)
                _remember(key, name, "switched_off")
    except Exception as e:
        log.warning(f"Adaptive Tuning: set_game_enabled failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    if enabled:
        try_engage_running_game()
    return _answer()


def step_game(exe, to, confirm=False) -> dict:
    """Move a game one level up ("faster"), back to full ("full") or one level
    down ("slower").  Applied at once when that game is being played.  Going
    down is always allowed.  Going up to a level the graphics driver took the
    game down from in the last 30 days needs confirm=True; without it the
    answer is {"ok": False, "need_confirm": True, "text": the question}."""
    key = _norm(exe)
    if to not in ("faster", "full", "slower"):
        return {"ok": False, "err": ERR_STEP}
    try:
        with _session_lock:
            now = _now()
            s = _sess if (_sess is not None and _sess["exe"] == key) else None
            prof = _bounded(s, _profile())  # (during its session: not against a profile raised mid-game)
            with _state_lock:
                st = _load_state()
                game = st["games"].get(key)
                if game is None:
                    return {"ok": False, "err": ERR_NO_GAME}
                name = game.get("name") or key
                level = _lvl(game)
                if to in ("faster", "full") and level > 0 and confirm is not True:
                    target = level - 1 if to == "faster" else 0
                    if policy.failures_at(game, target, since=now - CONFIRM_DAYS * 86400.0) > 0:
                        return {"ok": False, "need_confirm": True, "text": CONFIRM_STEP_UP(name, target)}
                play_now = (_num(game.get("minutes")) + s["play_s"] / 60.0) if s is not None else None
                r = policy.step(game, to, now, _limit(prof), play_minutes_now=play_now)
                if not r.get("ok"):
                    return {"ok": False, "err": r.get("err") or ERR_STEP}
                _history(st, key, name, r.get("history"), now)
                _save_state(st)
                new_level, lal = _lvl(game), game.get("limit_at_level")
            if s is not None:
                was = s["level"]
                s["level"], s["lal"] = new_level, lal
                # (an eased game running beside it still counts)
                beside = _eased_beside(key, new_level) if not s["held"] else None
                if beside is not None and _go_down_beside(s, beside):
                    _add_history(key, name, RUNS_BESIDE(name, beside[3], beside[0]), now)
                same = (s["level"] == was
                        and _level_values(_bounded(s, prof), s["level"], s["lal"]) == tuple(s["values"]))
                if not s["held"] and not same:  # (same: the eased game beside it keeps the session where it was)
                    w = _write("your step", s, light=False, cap=s["values"] if s["level"] >= was else None)
                    if not w.get("refused"):
                        s["applied_ok"] = bool(w.get("ok"))
                        s["reasserts"] = 0
                _save_session_quiet(s)
    except Exception as e:
        log.warning(f"Adaptive Tuning: step_game failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    return _answer()


def forget_game(exe) -> dict:
    """Remove everything Adaptive Tuning learned about one game.  Its next
    start is a first start, at full overclock.  A game that is running right
    now is not looked after for the rest of this run of it (get_state says
    so: reason "forgotten"), and it gets no more overclock before it closes."""
    key = _norm(exe)
    try:
        with _session_lock:
            with _state_lock:
                game = _load_state()["games"].get(key)
            if game is None:
                return {"ok": False, "err": ERR_NO_GAME}
            s = _sess if (_sess is not None and _sess["exe"] == key) else None
            pid = s["pid"] if s is not None else (_tracked_pid(key) or 0)
            if s is not None:
                _release_locked(s, "forgotten", said=STOPPED_KEPT)
            with _state_lock:
                st = _load_state()
                if key not in st["games"]:
                    return {"ok": False, "err": ERR_NO_GAME}
                del st["games"][key]
                st["last_sessions"].pop(key, None)
                st["tails"].pop(key, None)
                st["unread"] = [u for u in st["unread"] if u["exe"] != key]
                _forget_running(st, key, game.get("name") or key, pid, _now())
                _save_state(st)
            _burst_tail.pop(key, None)
    except Exception as e:
        log.warning(f"Adaptive Tuning: forget_game failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    return _answer()


def reset_all() -> dict:
    """Forget everything it learned: every game and what happened.  The
    settings stay.  A game that is running right now: as in forget_game."""
    try:
        with _session_lock:
            with _state_lock:
                games = dict(_load_state()["games"])        # an unreadable file is not written over
            running = []                                    # games it knew that are being played right now
            if _sess is not None:
                running.append((_sess["exe"], _sess["name"], _sess["pid"]))
                _release_locked(_sess, "everything forgotten", said=STOPPED_KEPT)
            tracked = _tracked_game() or {}
            texe = _norm(tracked.get("exe"))
            if texe in games and all(texe != r[0] for r in running):
                running.append((texe, games[texe].get("name") or texe, tracked.get("pid")))
            with _state_lock:
                st = dict(_empty_state(), forgotten=dict(_load_state()["forgotten"]))
                for exe_, name_, pid_ in running:
                    _forget_running(st, exe_, name_, pid_, _now())
                _save_state(st)
            _burst_tail.clear()
            _health_reset()
    except Exception as e:
        log.warning(f"Adaptive Tuning: reset_all failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    return _answer()


def dismiss_note() -> dict:
    try:
        with _state_lock:
            s = _load_settings()
            s["upgrade_note"] = False
            _save_settings(s)
    except Exception as e:
        log.warning(f"Adaptive Tuning: dismiss_note failed: {e}")
        return {"ok": False, "err": ERR_SAVE}
    return _answer()


# ── what the page shows ───────────────────────────────────────────────
def _no_health() -> dict:
    return {"available": False, "errors_30d": 0, "events_30d": 0, "last_error_ts": None,
            "at_stock_90d": None, "covered_days": 0, "log_readable": True}


def _health_reset() -> None:
    _health_cache.update(at=None, value=None)
    try:
        gpu_events.reset_cache()
    except Exception:
        pass


def _with_no_overclock(ts: float, after_tests):
    """Did the driver errors that began at `ts` come with no overclock on the
    card?  True, False, or None when what the app held then is not known.

    True only when the card had held none for half an hour before them
    (looked at every five minutes) and no test on the GPU page ended in that
    time (`after_tests`: the tests' times with half an hour added).  The
    minutes after an overclock came off (the game closed and the card was
    handed back, Idle Mode parked it, a test crashed and was reset) are the
    driver still getting over what happened under it: errors then say
    nothing about a card with no overclock."""
    held = gpu_overclock.held_at(ts)
    if held is None:
        return None
    if (int(held[0]), int(held[1])) != (0, 0) or any(a <= ts <= b for a, b in after_tests):
        return False
    backs, step = [NO_OVERCLOCK_QUIET_S], NO_OVERCLOCK_STEP_S      # (the far end first: most often that settles it)
    while step < NO_OVERCLOCK_QUIET_S:
        backs.append(step)
        step += NO_OVERCLOCK_STEP_S
    for back in backs:
        before = gpu_overclock.held_at(ts - back)
        if before is None:
            return None                     # the record does not reach back that far
        if (int(before[0]), int(before[1])) != (0, 0):
            return False
    return True


def _health() -> dict:
    """How often the graphics driver logged errors lately, from the Windows
    event log.  Bursts (errors less than five minutes apart) are counted, and
    the times a test ran on the GPU page are left out: a test crashes the
    driver on purpose.  Errors from before the app kept a record of those
    tests (gpu_overclock.tuning_windows_since: 3.7.0 kept none) are left out
    too, since nobody can tell them from a test's: until that record is a day
    old nothing is said.  "at_stock_90d": how many of the last 90 days'
    bursts came with no overclock on the card (see _with_no_overclock; None
    when that is not known for any of them)."""
    c = _health_cache
    now = _now()
    if c["value"] is not None and c["at"] is not None and 0 <= now - c["at"] < HEALTH_CACHE_S:
        return dict(c["value"])
    out = _no_health()
    try:
        since = gpu_overclock.tuning_windows_since()
        windows = _test_windows()
        b = gpu_events.driver_error_bursts(days=30, exclude=windows)
        if b.get("ok") is False:
            out["log_readable"] = False     # the Windows event log did not answer: the page says so
        covered = 0 if since is None else min(int(b.get("covered_days") or 0), int(max(0.0, now - since) / 86400.0))
        if b.get("ok") and covered >= 1:
            bursts = [x for x in (b.get("bursts") or []) if _num(x.get("first_ts")) >= since]
            out.update(available=True, errors_30d=len(bursts),
                       events_30d=sum(int(x.get("count") or 0) for x in bursts), covered_days=covered,
                       last_error_ts=max(_num(x.get("last_ts")) for x in bursts) if bursts else None)
            b90 = gpu_events.driver_error_bursts(days=90, exclude=windows)
            if b90.get("ok"):
                after_tests = _test_windows(NO_OVERCLOCK_QUIET_S)
                old = [x for x in (b90.get("bursts") or []) if _num(x.get("first_ts")) >= since]
                known = none = 0
                for burst in old:
                    stock = _with_no_overclock(_num(burst.get("first_ts")), after_tests)
                    if stock is None:
                        continue
                    known += 1
                    none += 1 if stock else 0
                out["at_stock_90d"] = none if (known or not old) else None
    except Exception as e:
        log.debug(f"Adaptive Tuning: the driver's record could not be read: {e}")
        out = dict(_no_health(), log_readable=out["log_readable"])
    c.update(at=now, value=dict(out))
    return out


def get_state() -> dict:
    """Everything the Adaptive Tuning card shows.  Never raises: on failure
    {"ok": False, "err"}.  Takes only the state lock, and only briefly."""
    try:
        with _state_lock:
            settings = _load_settings()
            st = _load_state()
            s = _sess
            session = None
            forgotten = st["forgotten"]
            play_now = None                 # the open session's game: its play time with this session
            if s is not None:
                game_ = st["games"].get(s["exe"])
                if game_ is not None:
                    play_now = (s["exe"], _num(game_.get("minutes")) + s["play_s"] / 60.0)
                session = {
                    "exe": s["exe"], "name": s["name"], "level": s["level"],
                    "level_name": policy.level_name(s["level"]), "percent": policy.level_percent(s["level"]),
                    "core": int(s["values"][0]), "mem": int(s["values"][1]), "since": s["started"],
                    # A session that is left alone asks nothing of the card, so
                    # there is nothing the card refused: the page says why it is
                    # left alone.  (It shows "the card did not accept the
                    # overclock" before any other line.)
                    "applied_ok": bool(s["applied_ok"]) or bool(s["held"]), "held": s["held"],
                }
            last = dict(_last_game) if _last_game else None
        sup = supported()
        prof = _profile()
        limit = _limit(prof)
        game = None
        if session is not None:
            if session["exe"] in forgotten:     # (its session is only still open until the game closes)
                game = {"exe": session["exe"], "name": session["name"], "looked_after": False, "reason": "forgotten"}
            else:
                game = {"exe": session["exe"], "name": session["name"], "looked_after": True, "reason": ""}
        elif last is not None or forgotten:
            tracked = _tracked_game() or {}
            texe = _norm(tracked.get("exe"))
            if texe and texe in forgotten:
                game = {"exe": texe, "name": str(tracked.get("display") or forgotten[texe]["name"]),
                        "looked_after": False, "reason": "forgotten"}
            elif texe and last is not None and texe == last["exe"]:
                game = {"exe": last["exe"], "name": last["name"], "looked_after": False, "reason": last["reason"]}
        health = _health() if sup["ok"] else _no_health()
        if sup["ok"] and _failed_reads[0] >= FAILED_READS_TO_SAY:
            # (the looks during play keep failing: said now, not when the count is next taken)
            health = dict(health, log_readable=False)
        # For "one more and it will be eased" (the row's warning_open): the
        # open session counts as play, and at 50% it is only true when the
        # driver is known not to log errors with no overclock on the card.
        no_oc = (None if not health["available"] or health["at_stock_90d"] is None
                 else bool(health["at_stock_90d"] > 0))
        rows = [policy.row(g, limit, play_minutes_now=play_now[1] if play_now and play_now[0] == g.get("exe") else None,
                           errors_with_no_overclock=no_oc) for g in st["games"].values()]
        rows.sort(key=lambda r: _num(r.get("last_played")), reverse=True)
        history = sorted(st["history"], key=lambda h: _num(h.get("ts")), reverse=True)[:HISTORY_SHOWN]
        try:
            tuner = bool(gpu_overclock.is_tuning_active())
        except Exception:
            tuner = False
        return {
            "ok": True,
            "enabled": settings["enabled"], "notify": settings["notify"], "sensitivity": settings["sensitivity"],
            "upgrade_note": settings["upgrade_note"], "old_switch_was_on": settings["old_switch_was_on"],
            "supported": {"ok": bool(sup["ok"]), "reason": sup["reason"]},
            "limit": {"has_profile": bool(prof["exists"]), "core": int(prof["core"]), "mem": int(prof["mem"]),
                      "power": int(prof["power"]),
                      "apply_on_startup": bool(prof["exists"] and prof["apply_on_startup"])},
            "watching": _watching(), "monitor_starting": _monitor_starting(), "tuner_running": tuner,
            "game": game, "session": session, "games": rows,
            "health": health,
            "history": [{"ts": h.get("ts"), "exe": h.get("exe") or "", "name": h.get("name") or "",
                         "text": h.get("text") or ""} for h in history],
        }
    except Exception as e:
        log.warning(f"Adaptive Tuning: get_state failed: {e}")
        return {"ok": False, "err": ERR_STATE}


def report_line(exe, ended_ts):
    """The after-game report's line about the overclock for the session of
    `exe` that ended at about `ended_ts`, or None."""
    try:
        with _state_lock:
            rec = _load_state()["last_sessions"].get(_norm(exe))
        if not rec or not rec.get("line"):
            return None
        if abs(_num(rec.get("ended")) - float(ended_ts)) > REPORT_MATCH_S:
            return None
        return str(rec["line"])
    except Exception as e:
        log.debug(f"Adaptive Tuning: report_line failed: {e}")
        return None
