"""Adaptive Tuning's engine (core/adaptive_tuning.py): the session around a
game, the files, the card and the Windows event log.

Nothing here touches a real clock.  The graphics card is a stand-in behind
the real core.gpu_overclock (so its write counter, its drift watchdog's target
and its timeline are the real ones), the event log is the sandbox's, time only
moves when a test moves it, and the watch thread does not run: a test calls
poll() where the thread would wake.  Two tests at the end of the file run the
real thread.

The rules themselves (what an incident means) are tested in
tests/test_adaptive_policy.py; this file tests that the engine asks them the
right question at the right moment and does what they answer, and the
invariants I1 to I7 in the module's docstring.
"""
import sandbox as m

import ast
import json
import os
import random
import re
import threading
import time
import types

GO = m.app("core.gpu_overclock")
AT = m.app("core.adaptive_tuning")
GP = m.app("core.game_profiles")
from core import adaptive_policy as P, capabilities as CAP, gpu_events as GE, notifier as NOTE      # noqa: E402
from core import temp_ceiling as TC, health_audit as HA, session_report as SR, crash_recovery as CR  # noqa: E402
from core import tournament_mode as TM, integrity_scan as IS                                         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXE, NAME, PID = "cod.exe", "Call of Duty", 4242
T0 = 1_794_571_200.0                    # 13 Nov 2026 12:00 UTC, as in the UI fixtures
FULL, L75, L50, NONE = (450, 2225), (338, 1669), (225, 1113), (0, 0)
MINE = "adaptive_tuning"
EM_DASH = chr(0x2014)
BANNED = re.compile(r"(?i:offset|tdr|blacklist|baseline|yielded|stock|strike)|\bAT\b")
LIMITS = {"core_max_mhz": 2900, "mem_max_mhz": 14000, "power_default_w": 300, "power_max_w": 350,
          "power_min_w": 150}
NVIDIA = {"has_nvidia": True, "gpu_name": "NVIDIA GeForce RTX 5070 Ti", "gpu_vendor": "NVIDIA"}

_REAL_GO = {n: getattr(GO, n) for n in (
    "detect_gpu_oc_capability", "detect_oc_tools_running", "verify_oc_applied", "_nvapi", "time", "apply_oc",
    "write_offsets", "reset_oc", "load_profile", "save_profile", "delete_profile", "set_profile_apply_on_startup",
    "_oc_watchdog_thread", "is_tuning_active", "wait_startup_apply", "get_current_offsets", "tuning_windows",
    "held_at", "check_driver_events", "wait_external_writes", "read_live_state", "emergency_reset_oc",
    "tuning_windows_since")}
_REAL_AT = {n: getattr(AT, n) for n in ("_now", "_pause", "_defer", "_defer_later", "_tracked_game", "_watching",
                                        "_write", "_exe_running", "_read_json", "atomic_write_json", "POLL_S",
                                        "_uptime_s", "_eased_beside")}
_REAL_GP = {n: getattr(GP, n) for n in ("monitor_starting", "_get_foreground_pid", "running_game_candidates")}
_REAL_TC = {"get_status": TC.get_status, "time": TC.time}
_REAL_P = {"decide": P.decide}
_REAL_CAP = CAP.detect
_REAL_TOAST = NOTE.toast
_REAL_OPEN = open


class Card:
    """The graphics card as the app sees it through its clock interface."""

    def __init__(self, current=(0, 0)):
        self.current = tuple(current)
        self.available = self.read_ok = True
        self.drop = 0               # this many of the next writes are accepted by nothing (the driver is recovering)
        self.refuse = False         # the driver says no to every write
        self.intruder = None        # another program puts this back after every write
        self.writes, self.reads = [], 0
        self.on_write = None

    def is_available(self):
        return {"ok": self.available, "err": "" if self.available else "nvapi64.dll not found"}

    def get_current_offsets(self):
        self.reads += 1
        if not self.read_ok:
            return {"ok": False, "err": "the driver did not answer"}
        return {"ok": True, "core_offset_mhz": self.current[0], "mem_offset_mhz": self.current[1]}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        if self.on_write:
            self.on_write(core_offset_mhz, mem_offset_mhz)
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        if self.refuse:
            return {"ok": False, "err": "driver said no"}
        if self.drop > 0:
            self.drop -= 1
        elif self.intruder is not None:
            self.current = tuple(self.intruder)
        else:
            self.current = (core_offset_mhz, mem_offset_mhz)
        return {"ok": True, "struct_version_used": 3, "err": ""}

    def force_reset_all(self):
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

    def sleep(self, seconds):
        self.now += seconds


clock = Clock()
toasts: list = []           # (title, text, force)
pauses: list = []           # every wait between tries
later: list = []            # (seconds, fn) handed to _defer_later
deferred: list = []         # what _defer was given, when a test wants to run it itself
calls: list = []            # every clock write asked of gpu_overclock: {"fn", "owner", "core", "mem", "power", ...}
decided: list = []          # every incident handed to the rules: (exe, first_ts, last_ts, action)
write_stack: list = []      # the _write call in progress
TRACKED = [None]            # what game mode says it is tracking


def _record(fn_name):
    real = _REAL_GO[fn_name]

    def recorded(*a, **k):
        if fn_name == "apply_oc":
            core = k.get("core_offset_mhz", a[0] if a else 0)
            mem = k.get("mem_offset_mhz", a[1] if len(a) > 1 else 0)
            power = k.get("power_pct", a[2] if len(a) > 2 else 100)
        elif fn_name == "write_offsets":
            core, mem, power = (a[0] if a else k.get("core", 0)), (a[1] if len(a) > 1 else k.get("mem", 0)), None
        else:
            core = mem = 0
            power = 100
        s = AT._sess
        calls.append({"fn": fn_name, "owner": k.get("owner"), "core": core, "mem": mem, "power": power,
                      "in_write": write_stack[-1] if write_stack else None,
                      "session": s, "stopped": bool(s and s["stop"].is_set()), "held": s["held"] if s else None,
                      "profile": _REAL_GO["load_profile"](), "level": s["level"] if s else None,
                      "lal": s["lal"] if s else None, "enabled": enabled_now(),
                      "paused": MINE in GO._oc_watchdog_pause_sources, "ts": clock.now})
        return real(*a, **k)
    return recorded


def _recording_write(why, s=None, values=None, light=False, cap=None, skip_if_held=False):
    write_stack.append({"why": why, "values": values, "session": s})
    try:
        return _REAL_AT["_write"](why, s, values=values, light=light, cap=cap, skip_if_held=skip_if_held)
    finally:
        write_stack.pop()


def _recording_decide(game, incident, ctx):
    d = _REAL_P["decide"](game, incident, ctx)
    decided.append((game.get("exe"), incident.get("first_ts"), incident.get("last_ts"), d.get("action"), dict(ctx)))
    return d


def enabled_now():
    try:
        with _REAL_OPEN(AT.settings_path(), encoding="utf-8") as f:
            return json.load(f).get("enabled") is True
    except (OSError, ValueError):
        return False


def restore():
    """Everything this file replaces, back as it was (and no session left open)."""
    AT._defer = lambda fn: None
    for name in ("_now", "_pause", "_exe_running", "_read_json", "atomic_write_json", "_write"):
        setattr(AT, name, _REAL_AT[name])
    if AT._sess is not None:
        AT._sess["stop"].set()
        with AT._state_lock:
            AT._sess = None
    for name, value in _REAL_GO.items():
        setattr(GO, name, value)
    for name, value in _REAL_AT.items():
        setattr(AT, name, value)
    for name, value in _REAL_GP.items():
        setattr(GP, name, value)
    TC.get_status, TC.time = _REAL_TC["get_status"], _REAL_TC["time"]
    P.decide = _REAL_P["decide"]
    CAP.detect = _REAL_CAP
    NOTE.toast = _REAL_TOAST
    GO._deactivate_oc_watchdog()
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    for sess in (GO._stability_session, GO._auto_oc_session, GO._benchmark_oc_session, GO._benchmark_state):
        sess["active"] = False
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
    AT._WATCH_THREAD = True
    TRACKED[0] = None
    for box in (toasts, pauses, later, deferred, calls, decided, write_stack):
        del box[:]


def rig(profile=(450, 2225, 100), apply_on_startup=True, enabled=True, sensitivity="balanced", notify=True,
        held=None, card=None):
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
    GO._nvapi = card if card is not None else Card()
    GO.mark_startup_apply_done()
    GO.tuning_windows_since = lambda: T0 - 400 * 86400.0        # the app has kept a record of its tests for a year
    GP._get_foreground_pid = lambda: 0
    for name in ("apply_oc", "write_offsets", "reset_oc"):
        setattr(GO, name, _record(name))
    CAP.detect = lambda force=False: dict(NVIDIA)
    NOTE.toast = lambda title, text, force=False: toasts.append((title, text, force)) or {"ok": True}
    P.decide = _recording_decide
    AT._now = clock.time
    GE._now = clock.time
    AT._pause = pauses.append
    AT._defer = lambda fn: fn()
    AT._defer_later = lambda seconds, fn: later.append((seconds, fn))
    AT._tracked_game = lambda: TRACKED[0]
    AT._write = _recording_write
    AT._uptime_s = lambda: 30 * 86400.0                         # Windows has been up for a month
    AT._WATCH_THREAD = False
    if profile is not None:
        save_profile(*profile, apply_on_startup=apply_on_startup)
    if held is not None:
        _REAL_GO["apply_oc"](core_offset_mhz=held[0], mem_offset_mhz=held[1],
                             power_pct=held[2] if len(held) > 2 else 100)
        del GO._nvapi.writes[:]
    with AT._state_lock:
        AT._save_settings(dict(AT._default_settings(), enabled=enabled, sensitivity=sensitivity, notify=notify))
    return GO._nvapi


def save_profile(core, mem, power=100, apply_on_startup=True):
    """The user's saved overclock (the GPU page writes it; Adaptive Tuning never does)."""
    r = _REAL_GO["save_profile"]({"core_offset_mhz": core, "mem_offset_mhz": mem, "power_pct": power,
                                  "apply_on_startup": apply_on_startup})
    assert r["ok"], r


def profile_bytes():
    try:
        with _REAL_OPEN(GO.OC_PROFILE_PATH, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def start(exe=EXE, name=NAME, pid=PID, proc=True):
    if proc:
        m.proc(pid, exe=exe)
    return AT.on_game_start(exe, name, pid)


def poll(seconds=20.0):
    """What the watch thread does when it wakes, `seconds` later."""
    clock.now += seconds
    s = AT._sess
    assert s is not None, "no session to poll"
    AT._poll(s["id"])


def error(ago=1.0, **more):
    """The graphics driver logs an error (a record in the System log), one
    second ago unless said otherwise.  (Never further back than the last
    poll: Windows writes the record when it happens.)"""
    return m.EVENTS.add("driver_error", clock.now - ago, **more)


def close(exe=EXE, pid=PID, ago=0.0):
    """The game closes and game mode reports it."""
    m.exit_proc(pid)
    return AT.on_game_exit(exe, exit_ts=clock.now - ago)


def windows(*procs):
    """These processes, (pid, exe), have a visible main window: what game
    mode's scan finds, and with it Adaptive Tuning's look for another game.
    (Without this nothing has a window.)"""
    rows = [{"Id": pid, "ProcessName": exe[:-4] if exe.lower().endswith(".exe") else exe, "WS": 900}
            for pid, exe in procs]
    m.PS_HOOKS[:] = [h for h in m.PS_HOOKS if not getattr(h, "is_windows", False)]

    def hook(command):
        return m.R(json.dumps(rows)) if "MainWindowHandle" in command else None
    hook.is_windows = True
    m.PS_HOOKS.append(hook)


def sess():
    return AT._sess


def state():
    s = AT.get_state()
    assert s["ok"] is True, s
    return s


def game(exe=EXE):
    with AT._state_lock:
        return AT._load_state()["games"].get(exe)


def put_game(exe=EXE, name=NAME, **over):
    """A game Adaptive Tuning already has a record of."""
    with AT._state_lock:
        st = AT._load_state()
        g = P.new_game(exe, name, clock.now - 86400)
        g.update(over)
        st["games"][exe] = g
        AT._save_state(st)
    return g


def texts():
    """What happened, oldest first."""
    with AT._state_lock:
        return [h["text"] for h in AT._load_state()["history"]]


def session_file(path=None):
    """The session file as it is on disk, or None.  (While the real watch
    thread saves it, Windows can refuse an open that lands inside the
    replace: that is "not readable this instant", so it is tried again.)"""
    for attempt in range(50):
        try:
            with _REAL_OPEN(path or AT.session_path(), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            if attempt == 49:
                raise
            time.sleep(0.01)


def mine(since=0):
    """The clock writes Adaptive Tuning asked for, as (function, core, mem, power)."""
    return [(c["fn"], c["core"], c["mem"], c["power"]) for c in calls[since:] if c["owner"] == MINE]


def watchdog_paused():
    return MINE in GO._oc_watchdog_pause_sources


def user_applies(core, mem, power=100, note=True):
    """The user presses Apply on the GPU page (the route notes it first)."""
    if note:
        AT.note_manual_change()
    return GO.apply_oc(core_offset_mhz=core, mem_offset_mhz=mem, power_pct=power)


def trip(cooldown=60.0):
    """A moment later the temperature ceiling takes the overclock off a card
    that is too hot."""
    clock.now += 1.0
    TC._state.update(last_trip_at=clock.now, in_cooldown_until=time.time() + cooldown)
    GO.reset_oc()


def cool():
    TC._state["in_cooldown_until"] = None


def run_deferred():
    while deferred:
        deferred.pop(0)()


# ═════════════════════════════════════════════════════════════════════
# on_game_start: every reason not to take a game on
# ═════════════════════════════════════════════════════════════════════
def untouched(card):
    return (card.writes == [] and mine() == [] and session_file() is None and AT._sess is None
            and not watchdog_paused())


def test_with_the_switch_off_nothing_is_touched():
    """I5.  Not even a record of the game is made."""
    card = rig(enabled=False, held=(450, 2225))
    r = start()
    assert r == {"engaged": False, "reason": "off"} and untouched(card)
    assert game() is None and state()["games"] == [] and state()["game"] is None
    assert GO.get_applied_oc()["core"] == 450, "the overclock the user applied stays"


def test_a_card_that_is_not_supported_is_never_written():
    card = rig()
    CAP.detect = lambda force=False: {"has_nvidia": False, "gpu_name": "AMD Radeon RX 7800 XT", "gpu_vendor": "AMD"}
    assert start() == {"engaged": False, "reason": "unsupported"} and untouched(card) and game() is None
    assert state()["supported"] == {
        "ok": False, "reason": "Adaptive Tuning needs an NVIDIA graphics card. This PC has AMD Radeon RX 7800 XT."}
    assert state()["health"]["available"] is False

    card = rig()
    card.available = False                                      # NVIDIA, but the clock interface is not there
    assert start()["reason"] == "unsupported" and untouched(card)
    assert state()["supported"] == {"ok": False, "reason": "Vispora can't change the clocks on this graphics card."}

    rig()
    CAP.detect = lambda force=False: {"has_nvidia": False, "gpu_name": "Unknown"}
    assert AT.supported()["reason"] == ("Adaptive Tuning needs an NVIDIA graphics card. "
                                        "This PC has a graphics card Vispora does not recognise.")


def test_a_no_is_asked_again_after_a_minute_and_a_yes_is_kept():
    card = rig()
    card.available = False
    asked = []
    real = CAP.detect
    CAP.detect = lambda force=False: asked.append(1) or real()
    assert AT.supported()["ok"] is False and AT.supported()["ok"] is False and len(asked) == 1
    clock.now += 59
    assert AT.supported()["ok"] is False and len(asked) == 1
    card.available = True                                       # (a driver was installed meanwhile)
    clock.now += 2
    assert AT.supported() == {"ok": True, "reason": ""} and len(asked) == 2
    card.available = False
    clock.now += 3600
    assert AT.supported()["ok"] is True and len(asked) == 2, "a yes is kept for the life of the process"


def test_without_a_saved_overclock_nothing_is_touched():
    for profile in (None, (0, 0, 100)):
        card = rig(profile=profile)
        assert start() == {"engaged": False, "reason": "no_overclock"} and untouched(card) and game() is None
        assert state()["limit"]["core"] == 0 and state()["limit"]["has_profile"] is (profile is not None)


def test_a_game_whose_own_switch_is_off_is_left_alone():
    card = rig(held=(450, 2225))
    put_game(enabled=False, level=2)
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start() == {"engaged": False, "reason": "switched_off"} and untouched(card)
    assert state()["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "switched_off"}
    assert GO.get_applied_oc()["core"] == 450, "left alone: not eased either"
    AT.on_game_exit(EXE)
    assert state()["game"] is None, "the note about the game goes with the game"


def test_a_game_that_starts_during_a_test_is_not_taken_on_and_is_picked_up_when_it_ends():
    card = rig()
    GO.is_tuning_active = lambda: True
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start() == {"engaged": False, "reason": "test_running"} and untouched(card) and game() is None
    st = state()
    assert st["tuner_running"] is True
    assert st["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "test_running"}
    GO.is_tuning_active = lambda: False
    assert AT.try_engage_running_game()["engaged"] is True, "what gpu_overclock calls when the test ends"
    assert card.current == FULL and state()["game"]["looked_after"] is True


def test_a_card_that_cannot_be_read_is_not_taken_on_unless_the_app_holds_the_overclock():
    card = rig()
    AT.supported()                                              # (asked while the card still answers)
    card.read_ok = False
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start() == {"engaged": False, "reason": "card_unreadable"}
    assert card.writes == [] and session_file() is None and game() is None
    assert state()["game"]["reason"] == "card_unreadable"

    card = rig(held=(450, 2225))                                # the app knows what it put there
    AT.supported()
    card.read_ok = False
    r = start()
    assert r["engaged"] is True and sess()["pre"] == [450, 2225, 100]

    card = rig()                                                # two reads that disagree are not a reading
    answers = [(0, 0), (120, 0)]
    real = card.get_current_offsets
    card.get_current_offsets = lambda: dict(real(), core_offset_mhz=answers.pop(0)[0]) if answers else real()
    AT._supported_cache.update(ok=True)
    assert start()["reason"] == "card_unreadable"


def test_it_waits_for_the_apps_own_start_before_it_records_what_was_on_the_card():
    """A game already running when the app starts is found while the saved
    overclock may still be on its way to the card: taken then, "before" was no
    overclock, and that is what the exit handed back."""
    card = rig()
    order = []
    GO._startup_apply_done.clear()

    def wait(timeout=0.0):
        order.append(("wait", timeout))
        _REAL_GO["apply_oc"](core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)     # the start-up apply lands
        return True
    GO.wait_startup_apply = wait
    card.on_write = lambda c, mm: order.append(("write", c, mm))
    assert start()["engaged"] is True
    assert order[0] == ("wait", 15.0) and sess()["pre"] == [450, 2225, 100], order

    close()                                                     # waited for once per run, not at every game start
    del order[:]
    start()
    assert ("wait", 15.0) not in order and sess() is not None

    rig(enabled=False)                                          # nothing waits when the game is not taken on
    GO.wait_startup_apply = lambda timeout=0.0: order.append("waited") or True
    del order[:]
    start()
    assert order == []


# ═════════════════════════════════════════════════════════════════════
# on_game_start: taking a game on
# ═════════════════════════════════════════════════════════════════════
def test_a_game_is_taken_on_at_its_level_of_the_saved_overclock():
    card = rig(apply_on_startup=False)
    seen = {}

    def on_write(core, mem):
        seen["file"] = session_file()
        seen["paused"] = watchdog_paused()
    card.on_write = on_write
    seq = GO.external_write_seq()
    r = start()
    assert r == {"engaged": True, "adopted": False, "exe": EXE, "name": NAME, "level": 0, "level_name": "Full",
                 "core": 450, "mem": 2225, "applied_ok": True, "held": None}
    assert card.writes == [(450, 2225)] and mine() == [("apply_oc", 450, 2225, 100)]
    assert GO.external_write_seq() == seq, "its own write is not counted as somebody else's"
    # on disk BEFORE the card was written: what was there, and what is about to be
    assert seen["file"]["pre"] == [0, 0, 100] and seen["file"]["writes"] == [[450, 2225]]
    assert seen["file"]["exe"] == EXE and seen["file"]["version"] == 2 and seen["paused"] is True
    f = session_file()
    assert {"version", "exe", "name", "pid", "started", "seen", "judged_until", "pre", "writes", "level",
            "held"} <= set(f)
    assert f["pid"] == PID and f["started"] == T0 and f["held"] is None and f["level"] == 0
    assert watchdog_paused() and AT.session_active() is True
    g = game()
    assert g["name"] == NAME and g["level"] == 0 and g["enabled"] is True and set(g) == set(P.new_game("x", "x", 0))
    st = state()
    assert st["session"] == {"exe": EXE, "name": NAME, "level": 0, "level_name": "Full", "percent": 100,
                             "core": 450, "mem": 2225, "since": T0, "applied_ok": True, "held": None}
    assert st["game"] == {"exe": EXE, "name": NAME, "looked_after": True, "reason": ""}


def test_each_level_puts_its_share_on_the_card_and_never_more():
    for level, want, power in ((0, FULL, 110), (1, L75, 110), (2, L50, 110), (3, NONE, None)):
        card = rig(profile=(450, 2225, 110), held=(100, 500, 110))
        put_game(level=level)
        assert start()["engaged"] and card.current == want, (level, card.current)
        assert mine() == [("apply_oc", want[0], want[1], power)], mine()
        assert state()["session"]["percent"] == (100, 75, 50, 0)[level]
        # the profile's power limit at levels 0 to 2; left as found at "no overclock"
        assert GO.get_applied_oc()["power"] == 110
    card = rig(profile=(450, 2225, 110), held=(100, 500, 105))  # a session that never set the power limit
    put_game(level=3)
    start()
    close()
    assert mine() == [("apply_oc", 0, 0, None), ("apply_oc", 100, 500, None)], "...does not set it at the end either"
    assert card.current == (100, 500) and GO.get_applied_oc()["power"] == 105
    card = rig(profile=(-50, 2225, 100))                        # a negative part is applied unchanged
    put_game(level=2)
    start()
    assert card.current == (-50, 1113)
    card = rig(profile=(450, 2225, 100))                        # eased against a smaller overclock: not lifted by a raise
    put_game(level=1, limit_at_level=[400, 2000])
    start()
    assert card.current == (300, 1500) and state()["games"][0]["limit_changed"] is True


def test_a_write_the_driver_refuses_is_shown_and_the_session_still_ends_properly():
    card = rig(held=(100, 500))
    card.refuse = True
    r = start()
    assert r["engaged"] is True and r["applied_ok"] is False and state()["session"]["applied_ok"] is False
    card.refuse = False
    poll()                                                      # the next look puts it on
    assert card.current == FULL
    poll()
    assert state()["session"]["applied_ok"] is True
    close()
    assert card.current == (100, 500) and session_file() is None


def test_a_write_that_is_still_under_way_is_not_shown_as_refused():
    """The first write takes seconds on a real PC (it starts processes).  The
    page can ask in the middle of it, and "the graphics card did not accept
    the overclock" must not be what it reads before the card has answered."""
    card = rig(held=(100, 500))
    seen = []
    card.on_write = lambda core, mem: seen.append(AT.get_state()["session"])
    r = start()
    assert r["engaged"] is True and len(seen) == 1
    assert seen[0]["exe"] == EXE and seen[0]["applied_ok"] is True and (seen[0]["core"], seen[0]["mem"]) == FULL
    assert state()["session"]["applied_ok"] is True
    close()
    assert card.current == (100, 500) and session_file() is None


def test_a_session_file_that_cannot_be_saved_means_no_write():
    """The value a session is about to write is noted first; if that cannot
    be done, the card is not written."""
    card = rig()
    AT.atomic_write_json = lambda path, data: False if path == AT.session_path() else _REAL_AT["atomic_write_json"](
        path, data)
    r = start()
    assert r["engaged"] is False and r["reason"] == "error" and card.writes == [] and AT._sess is None
    assert game() is None, "and no record was made"


# ═════════════════════════════════════════════════════════════════════
# A session that is already open
# ═════════════════════════════════════════════════════════════════════
def test_a_game_found_again_keeps_its_session_and_what_was_on_the_card_before_it():
    """Game mode put normal mode back mid-game and detected the game again:
    the second start used to take the session's own clocks for "what was on
    the card before", and the exit then handed those back."""
    card = rig(held=(100, 500))
    put_game(level=1)
    assert start()["engaged"] and card.current == L75
    first = sess()
    clock.now += 30
    r = AT.on_game_start(EXE, NAME, 5151)                       # found again, nothing closed in between
    assert r["engaged"] is True and r["adopted"] is True
    assert sess() is first and first["pid"] == 5151 and first["pre"] == [100, 500, 100]
    assert not first["stop"].is_set() and card.writes == [L75], "nothing was written again"
    assert session_file()["pid"] == 5151
    m.proc(5151, exe=EXE)
    close(pid=5151)
    assert card.current == (100, 500), "the card is handed back as it was found"


def test_another_game_starting_releases_the_open_session_first():
    rig(held=(100, 500), sensitivity="strict")
    assert start()["engaged"]
    poll()
    error()                                                     # seen, not judged yet
    poll()
    assert sess()["pending"]
    before = len(mine())
    r = start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    assert r["engaged"] is True and sess()["exe"] == "cs2.exe"
    # the first game's session: card handed back, nothing judged as an exit
    assert mine()[before:] == [("apply_oc", 100, 500, 100), ("apply_oc", 450, 2225, 100)]
    assert sess()["pre"] == [100, 500, 100], "the new session found what the first one had found"
    g = game()
    assert g["level"] == 0 and g["sessions"] == 1, "strict would have eased it, had this been judged as an exit"
    assert decided[0][4]["ended_game"] is False and len(decided) == 1
    # Only the first game was running when the driver logged it (that is looked at when the error is read,
    # not when it is judged): it is that game's, and at a release it is a warning at most.
    assert decided[0][4]["other_game_running"] is False and texts() == [P.W_SURVIVED(NAME)], texts()
    assert session_file()["exe"] == "cs2.exe"


# ═════════════════════════════════════════════════════════════════════
# The watch thread's poll
# ═════════════════════════════════════════════════════════════════════
def test_a_poll_keeps_the_heartbeat_and_counts_play_time():
    card = rig()
    start()
    poll(20)
    f = session_file()
    assert f["seen"] == T0 + 20 and sess()["play_s"] == 20 and f["play_s"] == 20
    poll(20)
    assert sess()["play_s"] == 40 and session_file()["judged_until"] == T0 + 40
    poll(3 * 3600)                                              # the PC slept
    assert sess()["play_s"] == 100, "a gap longer than a minute counts as one minute"
    assert card.writes == [FULL], "and nothing is written while the card holds the session's values"
    assert game()["minutes"] == 0.0, "play time reaches the record when the session ends"
    close()
    g = game()
    assert round(g["minutes"] * 60) == 100 and g["minutes_at_level"] == g["minutes"] and g["sessions"] == 1
    assert g["last_played"] == clock.now


def test_a_poll_looks_at_things_in_a_fixed_order():
    """Is the game still there; has someone else taken the card (the
    temperature ceiling first, then anybody else's write, then the saved
    overclock); the card itself; and only then the driver's errors."""
    card = rig()
    start()
    seen = []
    real = {"running": AT._exe_running, "ceiling": TC.get_status, "seq": GO.external_write_seq,
            "profile": GO.load_profile, "card": GO.get_current_offsets, "events": GE.query}
    AT._exe_running = lambda exe: seen.append("running") or real["running"](exe)
    TC.get_status = lambda: seen.append("ceiling") or real["ceiling"]()
    GO.external_write_seq = lambda: seen.append("seq") or real["seq"]()
    GO.load_profile = lambda: seen.append("profile") or real["profile"]()
    GO.get_current_offsets = lambda: seen.append("card") or real["card"]()
    GE.query = lambda *a, **k: seen.append("events") or real["events"](*a, **k)
    try:
        poll()
    finally:
        TC.get_status, GO.external_write_seq, GE.query = real["ceiling"], real["seq"], real["events"]
    first = [seen.index(x) for x in ("running", "ceiling", "seq", "profile", "card", "events")]
    assert first == sorted(first), seen
    assert seen.count("events") == 1 and seen.count("card") == 1 and seen[0] == "running"
    assert session_file()["seen"] == clock.now and card.writes == [FULL]
    # a held session stops after the take-over checks: no card read, no event read
    user_applies(300, 1500)
    poll()
    del seen[:]
    AT._exe_running = lambda exe: seen.append("running") or real["running"](exe)
    GO.get_current_offsets = lambda: seen.append("card") or real["card"]()
    GE.query = lambda *a, **k: seen.append("events") or real["events"](*a, **k)
    try:
        poll()
    finally:
        GE.query = real["events"]
    assert seen == ["running"], seen


def test_a_game_whose_exit_is_never_reported_closes_its_own_session():
    """Game mode only reports an exit it saw itself.  After Force normal mode,
    with the monitor stopped, or when the session was picked up by hand,
    nobody would ever end it: the card stayed on the session's values with
    the drift watchdog paused, and every test on the GPU page was refused."""
    card = rig(held=(100, 500))
    start()
    poll()
    error()
    poll()                                                      # T0+40: the error is seen
    m.exit_proc(PID)                                            # the game dies; nobody says so
    poll()                                                      # T0+60: first miss
    first_miss = clock.now
    poll()
    assert sess() is not None and card.current == FULL, "two misses are not an exit (a game that restarts itself)"
    assert sess()["play_s"] == 40, "no play time while the game is gone"
    poll()                                                      # third miss
    assert AT._sess is None and card.current == (100, 500) and session_file() is None
    assert not watchdog_paused() and AT.session_active() is False
    assert texts() == [P.W_ENDED(NAME)], "judged as a game exit, at the time of the first miss"
    assert decided[0][4]["ended_game"] is True
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["ended"] == first_miss
    assert AT.on_game_exit(EXE, exit_ts=clock.now) == {"handled": True, "already": True}, "a late report is harmless"
    assert texts() == [P.W_ENDED(NAME)] and game()["sessions"] == 1


def test_a_process_list_that_cannot_be_taken_is_not_an_exit():
    card = rig()
    start()
    AT._exe_running = lambda exe: None
    for _ in range(5):
        poll()
    assert sess() is not None and card.current == FULL and sess()["play_s"] == 100

    rig()                                                       # a game that comes back within two polls
    start()
    m.exit_proc(PID)
    poll()
    poll()
    m.proc(4545, exe=EXE)
    poll()
    poll()
    poll()
    assert sess() is not None and sess()["misses"] == 0


def test_a_thread_whose_session_has_ended_writes_nothing():
    """I3.  A poll under way when the session ended used to write its value
    after the card had been put back, and it stayed there."""
    card = rig(held=(100, 500))
    start()
    old = sess()
    close()
    card.current = (7, 7)                                       # anything the old session would want to correct
    n = len(card.writes)
    clock.now += 20
    AT._poll(old["id"])
    assert card.writes[n:] == [] and old["stop"].is_set()
    assert AT._write("re-assert", old, light=True)["refused"] == "no open session"
    # ...also when the next session is already open: the old id is not the open one
    start()
    new = sess()
    card.current = (7, 7)
    AT._poll(old["id"])
    assert card.current == (7, 7) and new["reasserts"] == 0


def test_the_drivers_dropped_clocks_are_put_back_the_light_way():
    card = rig()
    start()
    poll()
    m.clear_logs()
    card.current = (0, 0)                                       # the driver reset them
    poll()
    assert card.current == FULL and mine()[-1] == ("write_offsets", 450, 2225, None)
    assert not m.ran("nvidia-smi") and not m.PSC, "no process is started for a re-assert"
    assert sess()["reasserts"] == 1
    poll()
    assert sess()["reasserts"] == 0 and state()["session"]["applied_ok"] is True
    card.current = (452, 2222)                                  # within 5 MHz: nothing to do
    n = len(card.writes)
    poll()
    assert len(card.writes) == n


# ── someone else takes the card ───────────────────────────────────────
def test_the_user_changing_the_overclock_is_left_alone_until_the_game_closes():
    card = rig(held=(100, 500))
    start()
    poll()
    user_applies(300, 1500)                                     # the GPU page's Apply, mid-game
    n = len(card.writes)
    poll()
    s = sess()
    assert s["held"] == "manual" and card.current == (300, 1500) and card.writes[n:] == []
    assert not watchdog_paused(), "the user's own overclock is watched again at once"
    assert session_file()["held"] == "manual" and state()["session"]["held"] == "manual"
    assert texts() == [P.HELD_MANUAL(NAME)]
    error()
    poll()
    poll(200)
    assert texts() == [P.HELD_MANUAL(NAME)] and decided == [], "once, and nothing is counted while it is held"
    assert AT.step_game(EXE, "slower")["ok"] and card.current == (300, 1500), "a step changes the record only"
    close()
    assert card.current == (300, 1500) and card.writes[n:] == [], "nothing is put back: the user's value stays"
    assert session_file() is None and not watchdog_paused() and GO.get_applied_oc()["core"] == 300
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == "Overclock: left alone after you changed it"


def test_a_poll_between_the_note_and_the_users_write_does_not_write():
    """The route notes the change BEFORE it writes.  Without that order a poll
    in between saw "the card no longer holds my values" and wrote the
    session's back over the user's."""
    card = rig()
    start()
    AT.note_manual_change()
    card.current = (0, 0)                                       # (anything a poll would otherwise correct)
    poll()
    assert sess()["held"] == "manual" and card.current == (0, 0) and card.writes == [FULL]

    card = rig()                                                # the write alone (a reset, the tray) is noticed too
    start()
    GO.reset_oc()
    poll()
    assert sess()["held"] == "manual" and card.current == (0, 0)
    close()
    assert card.current == (0, 0) and card.writes.count(FULL) == 1

    card = rig()                                                # a write that lands while a re-assert is on its way
    start()
    s = sess()
    card.current = (0, 0)
    real = GO.get_current_offsets

    def read_then_user_writes():
        r = real()
        if not s["held"] and len(calls) == 1:
            _REAL_GO["apply_oc"](core_offset_mhz=200, mem_offset_mhz=900, power_pct=100)
        return r
    GO.get_current_offsets = read_then_user_writes
    poll()
    assert s["held"] == "manual" and card.current == (200, 900), "the session's write was not made over the user's"

    card = rig()                                                # ...and one that lands after the session's last
    start()                                                     # check, a moment before its own write
    s = sess()
    card.current = (0, 0)
    asked = []
    real_cooldown = AT._cooldown_active

    def cooldown_then_user_resets():
        asked.append(1)
        if len(asked) == 2:                                     # (the check inside the write itself)
            _REAL_GO["apply_oc"](core_offset_mhz=200, mem_offset_mhz=900, power_pct=100)
        return real_cooldown()
    AT._cooldown_active = cooldown_then_user_resets
    try:
        poll()
    finally:
        AT._cooldown_active = real_cooldown
    assert len(asked) == 2 and card.current == (200, 900) and s["held"] == "manual"
    assert calls[-1]["fn"] == "write_offsets" and calls[-1]["owner"] == MINE, "asked for, and refused by the count"


def test_nothing_is_written_over_the_users_change_before_the_next_poll_either():
    """Between the user's change and the next poll (up to 20 s) the session
    does not know yet that it is held.  A step, or an ease decided at that
    moment, must not write over what the user has just set."""
    card = rig()
    start()
    user_applies(300, 1500)
    n = len(card.writes)
    assert AT.step_game(EXE, "slower")["ok"] is True
    assert card.current == (300, 1500) and card.writes[n:] == [] and game()["level"] == 1
    assert AT._write("re-assert", sess(), light=True)["refused"] == "taken over"
    AT._follow_record(EXE)
    assert card.current == (300, 1500)
    poll()
    assert sess()["held"] == "manual"


def test_with_the_switch_off_a_session_that_is_somehow_still_open_writes_nothing():
    """I5.  Switching off ends the session; the write itself checks the switch
    as well, so turning it off stops every write whatever else went wrong."""
    card = rig()
    start()
    with AT._state_lock:
        AT._save_settings(dict(AT._default_settings(), enabled=False))     # (behind the engine's back)
    card.current = (0, 0)
    n = len(card.writes)
    poll()
    poll()
    assert card.current == (0, 0) and card.writes[n:] == [] and sess()["reasserts"] == 0
    assert AT._write("re-assert", sess(), light=True)["refused"] == "off"
    close()
    assert mine()[-1] == ("apply_oc", 0, 0, 100) and session_file() is None, "the hand-back still happens"


def test_only_a_session_that_is_still_looked_after_judges_driver_errors():
    """Asked by the GPU page's emergency-reset route (app.py)."""
    rig(held=(100, 500))
    assert AT.judging_driver_errors() is False
    start()
    assert AT.judging_driver_errors() is True
    user_applies(300, 1500)
    assert AT.judging_driver_errors() is True, "not known to the session yet"
    poll()
    assert sess()["held"] == "manual" and AT.judging_driver_errors() is False
    close()
    assert AT.judging_driver_errors() is False
    start()
    s = sess()
    assert AT.judging_driver_errors() is True
    AT.end_active_session("monitor stopped")
    assert AT.judging_driver_errors() is False and s["stop"].is_set()


def test_a_note_without_a_session_does_nothing_and_never_reaches_the_next_one():
    card = rig()
    AT.note_manual_change()                                     # Benchmark Tune applies through the same route
    AT.note_manual_change()
    start()
    poll()
    assert sess()["held"] is None and card.current == FULL
    AT.note_manual_change()
    close()
    start()
    poll()
    assert sess()["held"] is None, "the note went with the session it was made in"


def test_a_card_that_got_too_hot_stays_without_overclock_until_the_game_closes():
    card = rig(held=(450, 2225))
    start()
    poll()
    trip()                                                      # the ceiling resets the card: also "someone else wrote"
    n = len(card.writes)
    poll()
    assert sess()["held"] == "hot", "the ceiling's reset must not read as the user's"
    assert texts() == [P.HELD_HOT(NAME)] and not watchdog_paused()
    cool()                                                      # the cooldown is over; the session stays held
    poll()
    poll()
    assert card.current == (0, 0) and card.writes[n:] == [], "the overclock is not put back on a card that was too hot"
    close()
    assert card.current == (450, 2225), "handed back once the game is closed and the card has cooled"
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == "Overclock: off after the card got too hot"

    card = rig(held=(450, 2225))                                # closed while the cooldown is still running,
    start()                                                     # before a poll has seen the trip
    trip()
    n = len(card.writes)
    close()
    assert card.current == (0, 0) and card.writes[n:] == [] and session_file() is None and not watchdog_paused()
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == "Overclock: off after the card got too hot"

    card = rig(held=(450, 2225))                                # after the ceiling, the user sets something themselves
    start()
    trip()
    poll()
    cool()
    user_applies(200, 900)
    poll()
    assert sess()["held"] == "manual"
    close()
    assert card.current == (200, 900)


def test_a_game_that_starts_while_the_ceiling_cools_down_stays_without_overclock():
    """The ceiling took the overclock off a card that was too hot, less than
    its cooldown ago.  The start used to be shown as "the graphics card did
    not accept the overclock", and the overclock was put on later, mid-game.
    Now the session is left alone for heat, and says so."""
    card = rig()
    TC._state.update(last_trip_at=T0 - 30, in_cooldown_until=time.time() + 60)      # tripped before the game
    r = start()
    assert r["engaged"] is True and r["held"] == "hot" and r["applied_ok"] is True and card.writes == []
    assert state()["session"]["held"] == "hot" and state()["session"]["applied_ok"] is True
    assert texts() == [AT.HOT_AT_START(NAME)] and not watchdog_paused()
    poll()
    cool()
    for _ in range(3):
        poll()
    assert card.writes == [] and sess()["held"] == "hot", "not put on later either: it stays off until the game closes"
    # a step up in that time changes the record only; the page never says "at full overclock" over other clocks
    put_game(level=1)
    assert AT.step_game(EXE, "full", confirm=True)["ok"] is True and card.writes == []
    assert state()["session"]["held"] == "hot"
    close()
    assert card.writes == [] and session_file() is None
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == "Overclock: off after the card got too hot"

    for held, other_tool in (((100, 500), None), (None, Card(current=(100, 500)))):
        card = rig(held=held, card=other_tool, apply_on_startup=False)     # an overclock was put back on the card
        TC._state.update(last_trip_at=T0 - 30, in_cooldown_until=time.time() + 60)     # after that trip: it is not
        r = start()                                                         # "with no overclock", so it is left
        assert r["engaged"] is True and r["held"] == "manual" and card.writes == []    # alone as somebody's own
        assert texts() == [AT.OWN_AFTER_HOT(NAME)] and not watchdog_paused()
        cool()
        poll()
        close()
        assert card.writes == [] and card.current == (100, 500) and session_file() is None
        assert GO.get_applied_oc()["active"] is (held is not None)
        assert last_line() == "Overclock: left alone after you changed it"

    card = rig()                                                # the ceiling only told the user ("Notify only"):
    TC._state.update(last_trip_at=T0 - 30, in_cooldown_until=time.time() + 60, last_trip_action="notify")
    r = start()                                                 # nothing was taken off, so nothing is held back
    assert r["held"] is None and r["applied_ok"] is True and card.current == FULL


def test_another_program_that_keeps_changing_the_clocks_is_left_to_it():
    card = rig(held=(100, 500))
    start()
    card.intruder = (50, 0)                                     # it puts its own values back after every write
    card.current = (50, 0)
    for _ in range(3):
        poll()
    assert card.writes[1:] == [FULL] * 3 and sess()["held"] is None
    assert state()["session"]["applied_ok"] is False
    n = len(card.writes)
    poll()
    assert sess()["held"] == "other_program" and card.writes[n:] == [], "after three tries it stops"
    assert state()["session"]["applied_ok"] is True, "the page now says why it is left alone, not that a write failed"
    assert texts() == [P.HELD_OTHER(NAME)]
    assert watchdog_paused(), "its own drift watchdog must not go on writing the session's values either"
    for _ in range(3):
        poll()
    assert card.writes[n:] == []
    card.intruder = None
    close()
    assert card.current == (100, 500) and GO.get_applied_oc()["core"] == 100 and not watchdog_paused()
    assert session_file() is None
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] is None


# ── the saved overclock changes mid-game ──────────────────────────────
def test_a_lowered_profile_is_followed_at_once_and_a_raised_one_is_not():
    card = rig()
    put_game(level=1)
    start()
    assert card.current == L75
    save_profile(300, 1500)                                     # saved lower (no route in between: the file itself)
    poll()
    assert card.current == (225, 1125) and mine()[-1] == ("write_offsets", 225, 1125, None)
    assert sess()["held"] is None and sess()["reasserts"] == 0
    save_profile(600, 3000)                                     # saved higher
    n = len(card.writes)
    poll()
    poll()
    assert card.current == (225, 1125) and card.writes[n:] == [], "nothing goes up by itself mid-game"
    card.current = (0, 0)                                       # the driver drops the clocks: put back as they were
    poll()
    assert card.current == (225, 1125), "a re-assert is not a way up either"
    close()
    card2 = rig(profile=(600, 3000, 100))
    put_game(level=1, limit_at_level=[450, 2225])
    start()
    assert card2.current == L75, "and the next start still does not lift a game that was eased against less"


def test_a_deleted_profile_is_never_written_back():
    card = rig(held=(450, 2225))
    start()
    poll()
    AT.note_manual_change()
    GO.delete_profile()                                         # the GPU page's Delete: profile gone, card reset
    n = len(card.writes)
    poll()
    poll()
    assert sess()["held"] == "manual" and card.current == (0, 0) and card.writes[n:] == []
    close()
    assert card.current == (0, 0) and card.writes[n:] == [], "not at the exit either"
    assert profile_bytes() is None

    card = rig()                                                # the file removed behind the app's back
    start()
    os.remove(GO.OC_PROFILE_PATH)
    card.current = (0, 0)
    poll()
    assert sess()["held"] == "manual" and card.current == (0, 0)
    assert AT._write("re-assert", sess(), light=True)["refused"] == "held"

    card = rig()                                                # a profile that cannot be read: nothing this time
    start()
    GO.load_profile = lambda: {"exists": False, "err": "locked"}
    card.current = (0, 0)
    poll()
    assert sess()["held"] is None and card.current == (0, 0)
    GO.load_profile = _REAL_GO["load_profile"]
    poll()
    assert card.current == FULL


def test_a_profile_raised_mid_game_is_not_what_a_game_is_eased_against():
    """The session remembers the lowest the saved overclock was while it ran.
    A raise lifts nothing mid-game: not the clocks, and not the limit an ease
    is recorded against (the record said "75% of 600 / 3000" while the card
    ran 450 / 2225)."""
    card = rig(sensitivity="strict")
    start()
    poll()
    save_profile(600, 3000)
    poll()
    error()
    for _ in range(7):
        poll()
    g = game()
    assert g["level"] == 1 and g["limit_at_level"] == [450, 2225] and card.current == L75
    save_profile(300, 1500)                                     # lowered: followed at once
    poll()
    assert card.current == (225, 1125) and sess()["lim"] == [300, 1500] and session_file()["lim"] == [300, 1500]
    save_profile(450, 2225)                                     # and raised back: the lowest it has seen stands
    n = len(card.writes)
    poll()
    poll()
    assert card.writes[n:] == []
    assert AT.step_game(EXE, "full", confirm=True)["ok"] and card.current == (300, 1500), "also for the user's step"
    assert state()["session"]["core"] == 300 and state()["limit"]["core"] == 450
    close()
    start(pid=5151)                                             # the next session starts from what is saved now
    assert card.current == FULL

    card = rig()                                                # seen at every look, also while nothing is written
    put_game(level=3)                                           # (a game at no overclock)
    start()
    save_profile(300, 1500)
    poll()
    save_profile(450, 2225)
    poll()
    assert card.writes == [NONE] and sess()["lim"] == [300, 1500]
    assert AT.step_game(EXE, "faster")["ok"] and card.current == (150, 750)


def test_an_eased_game_running_beside_the_tracked_one_holds_the_card_down():
    """Game mode tracks the first game-list process it found (a controller
    tool's window, a launcher) until that exits.  A game eased to 75% that is
    started beside it used to run on the full overclock the whole time."""
    card = rig(held=(450, 2225))
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=1)   # eased on record
    start()                                                     # another program is tracked, at full overclock
    assert card.current == FULL
    m.proc(5151, exe="cs2.exe")                                 # ...and then the eased game is started beside it
    before = len(mine())
    poll()
    assert card.current == L75 and mine()[before:] == [("write_offsets", 338, 1669, None)]
    assert sess()["level"] == 1 and state()["session"]["level_name"] == "75%", "the page says what the card holds"
    assert game()["level"] == 0 and game("cs2.exe")["level"] == 1 and sess()["stepped"] is False, "no record changes"
    assert texts() == [AT.RUNS_BESIDE(NAME, "Counter-Strike 2", 1)]
    poll()
    assert len(texts()) == 1 and sess()["held"] is None, "said once"
    m.exit_proc(5151)
    n = len(card.writes)
    for _ in range(3):
        poll()
    assert card.current == L75 and card.writes[n:] == [], "nothing goes up by itself when it closes"
    close()
    assert card.current == FULL, "handed back as it was found"

    card = rig(held=(100, 500))                                 # found while the eased game is running already:
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=2, limit_at_level=[400, 2000])
    m.proc(5151, exe="cs2.exe")                                 # the higher pair is never written at all
    start()
    assert card.writes == [(200, 1000)] and sess()["level"] == 2 and game()["level"] == 0
    assert AT.step_game(EXE, "slower")["ok"] and card.current == (200, 1000), "a step to 75% is not a way up"
    assert card.writes == [(200, 1000)] and game()["level"] == 1, "and nothing is written for it: the record changes"
    assert AT.step_game(EXE, "slower")["ok"] and AT.step_game(EXE, "slower")["ok"] and card.current == NONE

    for other in ({"level": 1, "enabled": False}, {"level": 0}):    # its own switch is off, or it is not eased
        card = rig(held=(100, 500))
        put_game(exe="cs2.exe", name="Counter-Strike 2", **other)
        m.proc(5151, exe="cs2.exe")
        start()
        poll()
        assert card.current == FULL and texts() == []
    card = rig(held=(100, 500))                                 # ...or it is not running
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=2)
    start()
    poll()
    assert card.current == FULL

    card = rig(held=(100, 500))                                 # a session that is being left alone writes nothing,
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=2)   # so it takes no level from the game beside it and
    start()                                                     # says nothing about one
    user_applies(300, 1500)
    poll()
    m.proc(5151, exe="cs2.exe")
    poll()
    assert AT.step_game(EXE, "slower")["ok"] and sess()["level"] == 1 and card.current == (300, 1500)
    assert texts() == [P.HELD_MANUAL(NAME), P.USER_SET(NAME, 1)]
    card = rig()
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=2)
    m.proc(5151, exe="cs2.exe")
    TC._state.update(last_trip_at=T0 - 30, in_cooldown_until=time.time() + 60)
    assert start()["held"] == "hot" and texts() == [AT.HOT_AT_START(NAME)] and sess()["level"] == 0


# ── the temperature ceiling, as it really trips ───────────────────────
RESET_TRIP = dict(TC.DEFAULT_SETTINGS, action="reset")
NOTIFY_TRIP = dict(TC.DEFAULT_SETTINGS, action="notify")


def test_a_ceiling_that_only_notifies_takes_nothing_off_and_the_session_carries_on():
    """"Notify only": the trip was read as "the overclock was taken off".
    The page said so, the session stopped judging, and a game that closed
    inside the cooldown (nine times in ten on a card that sits at its ceiling)
    was not handed back."""
    for held, level, on_card in ((None, 0, FULL), ((450, 2225), 2, L50)):
        card = rig(apply_on_startup=held is not None, held=held)
        TC.time = clock
        put_game(level=level)
        start()
        poll()
        TC._do_trip(86, NOTIFY_TRIP)
        st = TC.get_status()
        assert st["last_trip_action"] == "notify" and st["cooldown_remaining_s"] == 60.0 and card.current == on_card
        n = len(card.writes)
        poll()
        assert card.writes[n:] == [], "nothing to write: the card still holds the session's values"
        assert sess()["held"] is None and state()["session"]["held"] is None and watchdog_paused()
        card.current = (0, 0)                                   # dropped clocks are still put back
        poll()
        assert card.current == on_card
        error()
        for _ in range(7):
            poll()
        assert [d[3] for d in decided] == ["warning"], "and a driver error is still judged"
        TC._do_trip(86, NOTIFY_TRIP)                            # it trips again about every minute
        assert TC.get_status()["cooldown_remaining_s"] is not None
        close()                                                 # inside the cooldown
        want = (0, 0) if held is None else held[:2]
        assert card.current == want and session_file() is None and not watchdog_paused(), (held, card.current)
        assert "too hot" not in (last_line() or "") and not any("too hot" in t for t in texts())

    card = rig(held=(450, 2225))                                # the default action takes it off: held for heat
    TC.time = clock
    start()
    poll()
    TC._do_trip(86, RESET_TRIP)
    assert card.current == (0, 0) and GO.external_write_counts() == (2, 1)
    poll()
    assert sess()["held"] == "hot" and texts() == [P.HELD_HOT(NAME)]


def test_the_ceilings_own_resets_are_never_taken_for_the_user():
    """A card that stays at its ceiling with no overclock trips again when the
    cooldown ends.  The second reset moved the write count, and the session
    turned from "too hot" into "you changed the overclock yourself": untrue,
    and the overclock was not put back after the game."""
    card = rig(held=(450, 2225))
    TC.time = clock
    start()
    poll()
    TC._do_trip(86, RESET_TRIP)
    poll()
    assert sess()["held"] == "hot"
    clock.now += 45
    TC._do_trip(84, RESET_TRIP)                                 # still hot with no overclock on it
    poll()
    poll()
    assert sess()["held"] == "hot" and state()["session"]["held"] == "hot" and texts() == [P.HELD_HOT(NAME)]
    clock.now += 61
    close()
    assert card.current == FULL and last_line() == "Overclock: off after the card got too hot"

    # the watch thread wakes, or the game closes, while the reset is still under way
    for what in ("poll", "exit"):
        card = rig(held=(450, 2225))
        TC.time = clock
        start()
        poll()
        real_reset, seen = card.force_reset_all, {}

        def during(real_reset=real_reset, seen=seen, what=what):
            seen["counts"] = GO.external_write_counts()
            if what == "poll":
                poll(0)
            else:
                seen["exit"] = close()
            return real_reset()
        card.force_reset_all = during
        n = len(mine())
        TC._do_trip(86, RESET_TRIP)
        assert seen["counts"] == (2, 1), "counted before the card is touched, and as the ceiling's"
        if what == "poll":
            assert sess()["held"] == "hot" and texts() == [P.HELD_HOT(NAME)]
        else:
            assert seen["exit"]["left"] == "hot" and mine()[n:] == [], "nothing is put back on a card that is too hot"
            assert last_line() == "Overclock: off after the card got too hot" and card.current == (0, 0)

    card = rig(held=(450, 2225))                                # the count alone says it (the trip not on record yet)
    start()
    poll()
    GO.emergency_reset_oc(reason="temp_ceiling:86c")
    poll()
    assert sess()["held"] == "hot"
    card = rig(held=(450, 2225))                                # ...also at an exit that no poll came before
    start()
    poll()
    GO.emergency_reset_oc(reason="temp_ceiling:86c")
    assert close()["left"] != "manual" and last_line() != "Overclock: left alone after you changed it"

    card = rig(held=(450, 2225))                                # a session held for heat, carried on after Vispora
    TC.time = clock                                             # was restarted, stays held for heat
    start()
    poll()
    TC._do_trip(86, RESET_TRIP)
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["adopted"] is True and r["held"] == "hot"
    poll()
    poll()
    assert sess()["held"] == "hot"
    clock.now += 61
    close()
    assert card.current == FULL, "handed back once the game is closed and the card has cooled"

    card = rig(held=(450, 2225))                                # the user's own change after a trip is still theirs
    TC.time = clock
    start()
    TC._do_trip(86, RESET_TRIP)
    poll()
    clock.now += 61
    TC._do_trip(84, RESET_TRIP)
    user_applies(200, 900)
    poll()
    assert sess()["held"] == "manual"
    close()
    assert card.current == (200, 900)


def other_program_takes_the_card(card):
    start()
    card.intruder = (50, 0)
    card.current = (50, 0)
    for _ in range(4):
        poll()
    assert sess()["held"] == "other_program" and watchdog_paused()
    card.intruder = None


def test_what_the_user_does_after_another_program_took_the_card_stays():
    """A session left alone for "another program" ignored what the user did
    afterwards: the exit wrote the old overclock over their Reset, their Apply
    or a deleted profile, and the drift watchdog then held it there."""
    def resets():
        AT.note_manual_change()
        GO.reset_oc()
        return (0, 0), False

    def applies():
        user_applies(200, 900)
        return (200, 900), True

    def deletes():
        AT.note_manual_change()
        GO.delete_profile()
        return (0, 0), False

    def emergency():
        GO.emergency_reset_oc(reason="webgl_context_lost")      # (the page reports a driver reset: no note)
        return (0, 0), False

    for change in (resets, applies, deletes, emergency):
        for end in ("exit", "release"):
            card = rig(held=(450, 2225))
            put_game(level=1)
            other_program_takes_the_card(card)
            want, held_after = change()
            n = len(mine())
            for _ in range(3):
                poll()
            assert sess()["held"] == "manual" and state()["session"]["held"] == "manual", change.__name__
            assert watchdog_paused(), "its target may still be the session's: not resumed before the session ends"
            if end == "exit":
                assert close()["left"] == "manual"
            else:
                assert AT.end_active_session("app quit")["left"] == "manual"
            assert card.current == want and mine()[n:] == [], (change.__name__, end, card.current)
            assert GO.get_applied_oc()["active"] is held_after and not watchdog_paused()
            assert session_file() is None and last_line() == "Overclock: left alone after you changed it"

    card = rig(held=(100, 500))                                 # nobody changed anything, and the other program
    other_program_takes_the_card(card)                          # is still there when the game closes: asked for
    card.intruder = (50, 0)                                     # once, and that is all that can be done
    n = len(mine())
    r = close()
    assert r["handled"] is True and mine()[n:] == [("apply_oc", 100, 500, 100)] and pauses == []
    assert session_file() is None and texts() == [P.HELD_OTHER(NAME)] and not watchdog_paused()

    card = rig(held=(450, 2225))                                # ...also when no poll has seen it yet
    put_game(level=1)
    other_program_takes_the_card(card)
    AT.note_manual_change()
    GO.reset_oc()
    n = len(mine())
    close()
    assert card.current == (0, 0) and mine()[n:] == []
    assert last_line() == "Overclock: left alone after you changed it"

    card = rig(held=(450, 2225))                                # a ceiling reset after that hold is not the user:
    put_game(level=1)                                           # the card is still handed back once
    other_program_takes_the_card(card)
    GO.emergency_reset_oc(reason="temp_ceiling:86c")
    poll()
    assert sess()["held"] == "other_program"

    card = rig(held=(100, 500))                                 # carried on after Vispora was restarted, with no
    other_program_takes_the_card(card)                          # change by the user: still handed back, once
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["adopted"] is True and r["held"] == "other_program"
    for _ in range(3):
        poll()
    assert sess()["held"] == "other_program"
    n = len(mine())
    close()
    assert mine()[n:] == [("apply_oc", 100, 500, 100)] and card.current == (100, 500) and session_file() is None


def test_an_apply_or_a_reset_still_under_way_when_the_game_is_found_is_waited_for():
    """Apply and Reset count themselves at once and write the card seconds
    later (a process scan, nvidia-smi calls).  A session that started in
    between took the moved count as its starting point and the old values as
    "before": the user's change was written over, and the old values handed
    back after the game, with nothing said."""
    for press in ("apply", "reset"):
        card = rig(held=(450, 2225))
        gate, inside = threading.Event(), threading.Event()
        out = {}

        def slow(real, *a, **k):
            if threading.current_thread() is user:              # (only the user's own call is slow here)
                inside.set()
                gate.wait(5)
            return real(*a, **k)
        if press == "apply":
            real_scan = GO.detect_oc_tools_running
            GO.detect_oc_tools_running = lambda: slow(real_scan)
            user = threading.Thread(target=lambda: out.update(
                user=_REAL_GO["apply_oc"](core_offset_mhz=200, mem_offset_mhz=900, power_pct=100)))
            want = (200, 900)
        else:
            real_cap = GO.detect_gpu_oc_capability
            GO.detect_gpu_oc_capability = lambda: slow(real_cap)
            user = threading.Thread(target=lambda: out.update(user=_REAL_GO["reset_oc"]()))
            want = (0, 0)
        m.proc(PID, exe=EXE)
        monitor = threading.Thread(target=lambda: out.update(start=AT.on_game_start(EXE, NAME, PID)))
        user.start()
        try:
            assert inside.wait(5)
            assert GO.external_write_seq() == 2 and card.current == FULL, "counted, and not on the card yet"
            monitor.start()
            time.sleep(0.15)
            assert "start" not in out and AT._sess is None, "the start did not wait for the write that was under way"
        finally:                                                # (no thread is left behind, whatever happened)
            gate.set()
            user.join(5)
            if monitor.ident is not None:
                monitor.join(5)
        assert out["user"]["ok"] is True and out["start"]["engaged"] is True and out["start"]["held"] is None, out
        s = sess()
        assert s["pre"][:2] == list(want) and s["seq"] == 2, "what the user set is what was on the card before"
        assert card.current == FULL, "the game still gets its share of the saved overclock"
        poll()
        assert s["held"] is None and texts() == []
        close()
        assert card.current == want and GO.get_applied_oc()["active"] is (press == "apply"), (press, card.current)

    card = rig(held=(450, 2225))                                # a write that begins while the start is at work is
    real_read = GO.get_applied_oc                               # a take-over from the very first moment

    def user_presses_reset_meanwhile():
        GO.get_applied_oc = real_read
        _REAL_GO["reset_oc"]()
        return real_read()
    GO.get_applied_oc = user_presses_reset_meanwhile
    try:
        r = start()
    finally:
        GO.get_applied_oc = real_read
    assert r["engaged"] is True and r["held"] == "manual" and card.current == (0, 0) and mine() == []
    close()
    assert card.current == (0, 0)

    card = rig()                                                # a wait that fails or runs out changes nothing
    GO.wait_external_writes = lambda timeout: (_ for _ in ()).throw(RuntimeError("boom"))
    assert start()["engaged"] is True and card.current == FULL


# ═════════════════════════════════════════════════════════════════════
# Graphics driver errors during play
# ═════════════════════════════════════════════════════════════════════
def test_an_error_the_game_survives_is_a_warning_and_the_second_one_eases_it():
    card = rig()
    start()
    poll()
    error()                                                     # T0+19
    poll()                                                      # T0+40: seen
    assert sess()["pending"] and texts() == [] and decided == []
    assert session_file()["judged_until"] < T0 + 19, "a run that dies now finds it again at the next start"
    for _ in range(5):
        poll()                                                  # T0+140: 121 s after the error
    assert texts() == [P.W_SURVIVED(NAME)] and game()["level"] == 0 and card.current == FULL
    assert decided[0][3] == "warning" and decided[0][4]["ended_game"] is False
    assert game()["last_problem"] == {"ts": T0 + 19, "kind": "warning", "text": P.W_SURVIVED(NAME)}
    assert toasts == [], "nothing was changed, so nothing is announced"
    for _ in range(20):
        poll()                                                  # well past the five minutes that chain errors
    error()                                                     # 9 minutes after the first: the same spell of the driver
    for _ in range(7):
        poll()
    assert game()["level"] == 0 and card.current == FULL and texts()[-1] == P.W_SAME_SPELL(NAME)
    for _ in range(180):
        poll()                                                  # more than an hour of play after the first one
    m.clear_logs()
    error()
    before = len(mine())
    noted = []
    card.on_write = lambda core, mem: noted.append(session_file()["writes"])
    for _ in range(7):
        poll()
    card.on_write = None
    g = game()
    assert g["level"] == 1 and card.current == L75, card.current
    assert noted == [[[450, 2225], [338, 1669]]], "on disk before the card is written: a run that dies here knows"
    assert mine()[before:] == [("write_offsets", 338, 1669, None)], "lowered at once, the light way"
    assert not m.ran("nvidia-smi")
    assert texts()[-1] == P.EASED(NAME, 0, 1) and toasts == [("Adaptive Tuning", P.T_EASED(NAME, 1), True)]
    assert g["changes"][-1]["by"] == "driver" and g["limit_at_level"] == [450, 2225] and g["incidents"] == []
    assert sess()["level"] == 1 and state()["session"]["level_name"] == "75%"
    assert session_file()["writes"] == [[450, 2225], [338, 1669]] and session_file()["level"] == 1
    # at most one step per session
    for _ in range(40):
        poll()                                                  # (past the ten minutes after its own change)
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and texts()[-1] == P.W_ALREADY(NAME) and card.current == L75
    close()
    assert card.current == (0, 0)
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == (
            "Overclock eased to 75% of your overclock after graphics driver errors")


def test_an_error_is_counted_once():
    """I7.  The look reaches two seconds back, so the same record comes back
    at the next poll; errors less than five minutes apart are one incident,
    also when the first of them has already been judged; and an error from
    before the session is not the session's."""
    rig()
    m.EVENTS.add("driver_error", T0 - 1.0)                      # a second before the game was taken on
    start()
    assert sess() is not None
    error(ago=-0.5)                                             # T0+0.5
    clock.now += 1.0
    AT._poll(sess()["id"])                                      # T0+1
    clock.now += 1.0
    AT._poll(sess()["id"])                                      # T0+2: its look goes back to T0-1
    assert len(sess()["pending"]) == 1 and sess()["pending"][0]["count"] == 1
    error(ago=-100)                                             # T0+102: the same burst
    for _ in range(12):
        poll()                                                  # T0+242: 140 s after its last error
    assert [d[1:4] for d in decided] == [(T0 + 0.5, T0 + 102, "warning")]
    error()                                                     # T0+241: 139 s after an error already judged
    for _ in range(10):
        poll()
    assert len(decided) == 1 and len(game()["incidents"]) == 1, "the same incident, not a second one"
    assert sess()["pending"] == []


def test_an_event_log_that_cannot_be_read_counts_nothing_and_loses_nothing():
    rig()
    start()
    poll()
    m.EVENTS.fail = True
    error()
    poll()
    poll()
    assert decided == [] and sess()["judged_until"] == T0 + 20, "nothing is marked as looked at"
    m.EVENTS.fail = False
    poll()
    assert len(sess()["pending"]) == 1
    for _ in range(6):
        poll()
    assert texts() == [P.W_SURVIVED(NAME)]
    assert m.EVENTS.open_handles == 0 and m.EVENTS.bad == []


def test_strict_eases_at_the_first_error_and_only_tell_me_changes_nothing():
    card = rig(sensitivity="strict")
    start()
    poll()
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and card.current == L75 and texts() == [P.EASED_STRICT(NAME, 0, 1)]

    card = rig(sensitivity="watch")
    put_game(incidents=[{"ts": T0 - 3600, "play_minutes": 0.0, "level": 0, "ended_game": True}])
    start()
    poll()
    error()
    n = len(card.writes)
    for _ in range(7):
        poll()
    assert game()["level"] == 0 and card.writes[n:] == [] and texts() == [P.WATCH(NAME, 1)]
    assert toasts == [("Adaptive Tuning", P.WATCH(NAME, 1), True)]


def test_an_error_with_another_game_running_is_pinned_on_neither():
    rig(sensitivity="strict")
    start()
    m.proc(5151, exe="cs2.exe")                                 # a second game the game list knows,
    windows((PID, EXE), (5151, "cs2.exe"))                      # with a window of its own
    poll()
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 0 and texts() == [P.W_OTHER(NAME)] and game()["incidents"] == []
    assert decided[0][4]["other_game_running"] is True

    rig(sensitivity="strict")                                   # a second program of the same game does not count
    start()
    m.proc(5152, exe="cod23-cod.exe", ppid=PID)                 # started by the game
    m.proc(5153, exe="notepad.exe")
    m.proc(5154, exe="modernwarfare.exe")                       # another exe under the same name
    windows((PID, EXE), (5152, "cod23-cod.exe"), (5153, "notepad.exe"), (5154, "modernwarfare.exe"))
    assert GP.display_name_for("modernwarfare.exe") == NAME
    poll()
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)]

    rig(sensitivity="strict")                                   # what counts is who was running as it was logged,
    start()                                                     # not two minutes later when it is judged
    m.proc(5151, exe="cs2.exe")
    windows((PID, EXE), (5151, "cs2.exe"))
    poll()
    error()
    poll()                                                      # read while both were running
    m.exit_proc(5151)
    windows((PID, EXE))                                         # the other game is closed before it is judged
    for _ in range(6):
        poll()
    assert texts() == [P.W_OTHER(NAME)] and game()["level"] == 0

    rig(sensitivity="strict")                                   # and a game started after it does not make it "both"
    start()
    poll()
    error()
    poll()
    m.proc(5151, exe="cs2.exe")
    windows((PID, EXE), (5151, "cs2.exe"))
    for _ in range(6):
        poll()
    assert texts() == [P.EASED_STRICT(NAME, 0, 1)] and decided[0][4]["other_game_running"] is False


def test_errors_right_after_an_ease_are_not_counted():
    """A driver that has just been reset goes on logging errors for minutes;
    they say nothing about the new level."""
    rig(sensitivity="strict")
    start()
    poll()
    error()
    for _ in range(7):
        poll()                                                  # eased at T0+140
    assert game()["level"] == 1
    for _ in range(18):
        poll()                                                  # T0+500: six minutes later, a new incident
    error()
    for _ in range(7):
        poll()
    assert decided[-1][3] == "ignore" and len(texts()) == 1 and game()["incidents"] == []


# ═════════════════════════════════════════════════════════════════════
# The game closes
# ═════════════════════════════════════════════════════════════════════
def test_an_exit_hands_the_card_back_at_once_and_judges_afterwards():
    card = rig(held=(100, 500))
    start()
    poll()
    poll()
    clock.now += 5
    error(ago=0)                                                # 35 s before the exit is reported, as on the owner's PC
    clock.now += 35
    AT._defer = deferred.append
    r = close()
    assert r == {"handled": True, "restored": True, "left": None}
    # the first half is done: the card, the watchdog, the file
    assert card.current == (100, 500) and mine()[-1] == ("apply_oc", 100, 500, 100)
    assert AT._sess is None and session_file() is None and not watchdog_paused()
    assert GO.get_applied_oc() == {"core": 100, "mem": 500, "power": 100, "active": True}
    # the second half has not run yet: nothing judged, nothing counted
    assert texts() == [] and game()["sessions"] == 0 and len(deferred) == 1
    run_deferred()
    assert texts() == [P.W_ENDED(NAME)] and decided[0][4]["ended_game"] is True
    g = game()
    assert g["sessions"] == 1 and round(g["minutes"] * 60) == 40 and g["incidents"][0]["ended_game"] is True
    assert g["incidents"][0]["ts"] == clock.now - 35
    with AT._state_lock:
        last = AT._load_state()["last_sessions"][EXE]
    assert last == {"ended": clock.now, "line": "Overclock: full, a graphics driver error was noted"}
    assert AT.report_line(EXE, clock.now + 3) == "Overclock: full, a graphics driver error was noted"
    assert AT.report_line(EXE, clock.now + 3600) is None and AT.report_line("other.exe", clock.now) is None


def test_an_error_long_before_the_exit_is_one_the_game_survived():
    rig()
    start()
    poll()
    error()
    poll()
    clock.now += 110                                            # 131 s after the error, before the next poll
    close()
    assert texts() == [P.W_SURVIVED(NAME)] and decided[0][4]["ended_game"] is False

    rig()                                                       # an error logged after the game was gone is not its own
    start()
    poll()
    m.exit_proc(PID)
    exit_ts = clock.now + 1
    clock.now += 10
    error(ago=2)
    AT.on_game_exit(EXE, exit_ts=exit_ts)
    assert decided == [] and texts() == []


def test_a_game_relaunched_seconds_after_a_driver_crash_ends_at_the_right_level():
    """On the owner's PC the game was detected again 3 to 4 s after normal
    mode came back.  The new session used to be refused (another one was
    still open) or closed by the old one's exit; either way the game that had
    just crashed played on at full overclock."""
    card = rig(held=(450, 2225), sensitivity="strict")
    start()
    poll()
    poll()
    clock.now += 4
    error(ago=0)
    clock.now += 35                                             # the exit is reported 35 s after the error
    AT._defer = deferred.append
    assert close()["handled"] is True
    clock.now += 4
    r = start(pid=5151)                                         # the relaunch, before the exit has been judged
    assert r["engaged"] is True and r["adopted"] is False and r["level"] == 0 and card.current == FULL
    new = sess()
    before = len(mine())
    run_deferred()                                              # now the old session's exit is judged
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)]
    assert sess() is new and new["level"] == 1 and card.current == L75, "the new session is lowered at once"
    assert mine()[before:] == [("write_offsets", 338, 1669, None)]
    assert toasts == [("Adaptive Tuning", P.T_EASED(NAME, 1), True)]
    assert new["pre"] == [450, 2225, 100], "and it still knows what to hand back"
    # the storm goes on in the new session: nothing more is changed there
    poll()
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and len(texts()) == 1, "an error minutes after the ease is not counted"
    close(pid=5151)
    run_deferred()
    assert card.current == FULL
    with AT._state_lock:
        assert AT._load_state()["last_sessions"][EXE]["line"] == "Overclock: 75%, no problems"


def test_a_start_waits_for_an_exit_that_is_still_under_way():
    card = rig(held=(100, 500))
    start()
    gate, in_handback = threading.Event(), threading.Event()

    def slow(core, mem):
        if (core, mem) == (100, 500):
            in_handback.set()
            gate.wait(5)
    card.on_write = slow
    m.exit_proc(PID)
    out = {}
    t1 = threading.Thread(target=lambda: out.update(exit=AT.on_game_exit(EXE, exit_ts=clock.now)))
    t1.start()
    assert in_handback.wait(5)
    m.proc(5151, exe=EXE)
    t2 = threading.Thread(target=lambda: out.update(start=AT.on_game_start(EXE, NAME, 5151)))
    t2.start()
    time.sleep(0.15)
    assert "start" not in out, "the start did not wait for the exit"
    began = time.time()
    assert AT.get_state()["ok"] is True and AT.session_active() is True and AT.judging_driver_errors() is False
    AT.note_manual_change()
    assert time.time() - began < 1.0 and not gate.is_set(), "reads do not wait for the card"
    gate.set()
    t1.join(5)
    t2.join(5)
    assert out["exit"]["handled"] is True and out["start"]["engaged"] is True and out["start"]["adopted"] is False
    assert sess()["pre"] == [100, 500, 100] and card.current == FULL


def test_a_stale_exit_leaves_a_newer_session_alone():
    card = rig(held=(100, 500))
    start()
    old_exit = clock.now - 5                                    # an exit from before this session started
    poll()
    n = len(card.writes)
    assert AT.on_game_exit(EXE, exit_ts=old_exit) == {"handled": False, "reason": "a newer session is running"}
    assert AT.on_game_exit("other.exe", exit_ts=clock.now) == {"handled": False, "reason": "no session for this game"}
    assert sess() is not None and not sess()["stop"].is_set() and card.writes[n:] == [] and card.current == FULL
    assert session_file() is not None and watchdog_paused()
    assert AT.on_game_exit(EXE, exit_ts=clock.now)["handled"] is True and AT._sess is None


def test_an_exit_reported_after_the_relaunch_was_found_judges_without_closing():
    """The relaunch was found while its old session was still open (so that
    session carried on); the old run's exit arrives afterwards.  It must not
    close the game that is running now, and it is still "the game closed"."""
    card = rig(held=(100, 500), sensitivity="strict")
    start()
    poll()
    error()
    exit_ts = clock.now + 10
    clock.now += 14
    AT.on_game_start(EXE, NAME, 5151)                           # found again first
    m.proc(5151, exe=EXE)
    r = AT.on_game_exit(EXE, exit_ts=exit_ts)
    assert r == {"handled": True, "kept_open": True}
    assert sess() is not None and not sess()["stop"].is_set()
    assert game()["level"] == 1 and card.current == L75 and decided[0][4]["ended_game"] is True
    close(pid=5151)
    assert card.current == (100, 500)

    card = rig(held=(100, 500), sensitivity="strict")           # a session that is being left alone counts nothing,
    start()                                                     # also not at such an exit
    user_applies(300, 1500)
    poll()
    assert sess()["held"] == "manual"
    error()
    exit_ts = clock.now + 10
    clock.now += 14
    AT.on_game_start(EXE, NAME, 5151)
    m.proc(5151, exe=EXE)
    assert AT.on_game_exit(EXE, exit_ts=exit_ts) == {"handled": True, "kept_open": True}
    assert decided == [] and game()["level"] == 0 and card.current == (300, 1500)


def test_a_session_is_closed_once():
    """The app quitting while the exit was being handled: both closed the
    session, and its play time and its hand-back were done twice."""
    card = rig(held=(100, 500))
    start()
    poll()
    hold = threading.Event()
    card.on_write = lambda c, mm: hold.wait(0.3)
    m.exit_proc(PID)
    threads = [threading.Thread(target=AT.on_game_exit, args=(EXE,), kwargs={"exit_ts": clock.now}),
               threading.Thread(target=AT.end_active_session, args=("user quit",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert game()["sessions"] == 1 and round(game()["minutes"] * 60) == 20
    assert card.writes.count((100, 500)) == 1 and AT._sess is None


def test_the_card_is_handed_back_even_when_the_record_cannot_be_read():
    card = rig(held=(100, 500))
    start()
    poll()

    def locked(path):
        if path == AT.state_path():
            raise AT.StateError("locked")
        return _REAL_AT["_read_json"](path)
    AT._read_json = locked
    r = close()
    assert r["handled"] is True and card.current == (100, 500) and not watchdog_paused() and AT._sess is None
    AT._read_json = _REAL_AT["_read_json"]
    assert session_file() is None and game()["sessions"] == 0, "the play time is lost, the record is not"


def test_a_crash_without_a_driver_error_is_noted_and_changes_nothing():
    rig()
    start()
    poll()
    m.EVENTS.add("app_crash", clock.now - 3, exe=EXE, module="cod.exe", pid=PID)
    m.EVENTS.add("app_crash", clock.now - 3, exe="notepad.exe", module="x.dll", pid=77)
    clock.now += 12
    close()
    g = game()
    assert texts() == [P.PLAIN_CRASH(NAME)] and g["level"] == 0 and len(g["plain_crashes"]) == 1
    assert g["last_problem"]["kind"] == "plain_crash" and decided == [] and toasts == []

    rig()                                                       # matched by process id when the name is another
    start()
    poll()
    m.EVENTS.add("app_crash", clock.now - 3, exe="launcher.exe", pid=PID)
    close()
    assert texts() == [P.PLAIN_CRASH(NAME)]

    rig()                                                       # with a driver error it is that, not a plain crash
    start()
    poll()
    error()
    m.EVENTS.add("app_crash", clock.now - 1, exe=EXE, pid=PID)
    clock.now += 12
    close()
    assert texts() == [P.W_ENDED(NAME)] and game()["plain_crashes"] == []

    rig()                                                       # somebody else's crash, or an old one of this game
    start()
    m.EVENTS.add("app_crash", clock.now - 600, exe=EXE, pid=PID)
    poll()
    m.EVENTS.add("app_crash", clock.now - 3, exe="notepad.exe", pid=77)
    close()
    assert texts() == []

    rig()                                                       # the Application log unreadable: nothing is made up
    start()
    poll()
    m.EVENTS.add("app_crash", clock.now - 3, exe=EXE, pid=PID)
    m.EVENTS.fail = True
    close()
    assert texts() == [] and game()["sessions"] == 1
    m.EVENTS.fail = False


# ── ends that are not the game closing ────────────────────────────────
def test_a_release_hands_back_and_judges_nothing_as_a_crash():
    """The app quitting or updating, game mode put back by hand, the monitor
    stopped, the tracker moving on: the game may still be running.  A driver
    error from the last two minutes used to become "the game closed after
    it", which eases a game at once in the strict setting."""
    card = rig(held=(100, 500), sensitivity="strict")
    start()
    poll()
    error()
    poll()                                                      # seen 1 s ago, not judged
    r = AT.end_active_session("user quit")
    assert r["ended"] is True and r["restored"] is True
    assert card.current == (100, 500) and session_file() is None and not watchdog_paused() and AT._sess is None
    g = game()
    assert g["level"] == 0 and g["sessions"] == 1 and round(g["minutes"] * 60) == 40
    assert texts() == [P.W_SURVIVED(NAME)], "kept as a warning at most"
    assert len(g["incidents"]) == 1 and g["incidents"][0]["ended_game"] is False and toasts == []
    assert AT.end_active_session("again") == {"ended": False, "reason": "no active session"}

    card = rig(held=(100, 500))                                 # errors it had not seen yet are not read at a release
    start()
    poll()
    error()
    AT.end_active_session("monitor stopped")
    assert texts() == [] and decided == []


def test_switching_off_forgetting_and_resetting_release_the_open_session():
    for act in (lambda: AT.set_settings(enabled=False), lambda: AT.set_game_enabled(EXE, False),
                lambda: AT.forget_game(EXE), lambda: AT.reset_all()):
        card = rig(held=(100, 500), sensitivity="strict")
        start()
        poll()
        error()
        poll()
        r = act()
        assert r["ok"] is True and r["state"]["session"] is None, r
        assert card.current == (100, 500) and AT._sess is None and session_file() is None and not watchdog_paused()
        assert game() is None or game()["level"] == 0, "strict would have eased it, had this been an exit"
        assert not any("Eased" in t for t in texts()) and toasts == []
        n = len(card.writes)
        clock.now += 20
        assert card.writes[n:] == []


# ── letting go of a session whose game is still running on less ───────
def eased_and_running(level=2, **rig_kw):
    """A game that was eased, running on less than was on the card before it
    (the saved overclock, applied at start-up)."""
    card = rig(held=(450, 2225), **rig_kw)
    put_game(level=level)
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert start()["engaged"] and card.current == (FULL, L75, L50, NONE)[level]
    poll()
    return card


def like_the_fixture(answer, name):
    """The same keys as the state the page was built against."""
    with _REAL_OPEN(os.path.join(HERE, "fixtures", "at2_states.json"), encoding="utf-8") as f:
        fixture = json.load(f)[name]
    assert set(answer) == set(fixture), sorted(set(answer) ^ set(fixture))
    for key in ("session", "health"):
        if isinstance(fixture[key], dict):
            assert isinstance(answer[key], dict) and set(answer[key]) == set(fixture[key]), (name, key, answer[key])
        else:
            assert answer[key] is None, (name, key, answer[key])
    if isinstance(fixture["game"], dict):                       # (the page does not look at it otherwise)
        assert answer["game"] == dict(fixture["game"], exe=answer["game"]["exe"], name=answer["game"]["name"])
    return True


def last_line(exe=EXE):
    with AT._state_lock:
        return (AT._load_state()["last_sessions"].get(exe) or {}).get("line")


def test_a_release_never_raises_the_clocks_under_a_game_that_is_still_running():
    """Force normal mode, Stop Monitoring, "Refresh PC now", quitting Vispora:
    each let go of the session, and that handed the card back at once.  For a
    game eased to 50% because the driver keeps failing in it on more, "back
    to normal" put the full overclock under it while it was being played."""
    card = eased_and_running()
    n = len(card.writes)
    r = AT.end_active_session("user-tray")
    assert r == {"ended": False, "released": True, "exe": EXE, "left": "released",
                 "reason": "the game is still running"}
    s = sess()
    assert s is not None and s["held"] == "released" and not s["stop"].is_set()
    assert card.current == L50 and card.writes[n:] == [] and watchdog_paused()
    assert session_file()["held"] == "released" and AT.session_active() is True, "tests on the GPU page stay refused"
    assert AT.judging_driver_errors() is False and texts() == [P.HELD_RELEASED(NAME)]
    st = state()
    assert st["session"]["held"] == "released" and st["session"]["applied_ok"] is True
    assert like_the_fixture(st, "held_released")
    # nothing more is judged or written while it waits for the game to close
    error()
    card.current = (220, 1100)
    for _ in range(8):
        poll()
    assert decided == [] and card.writes[n:] == [] and len(texts()) == 1
    assert AT.end_active_session("monitor stopped")["released"] is True and len(texts()) == 1, "said once"
    assert AT.step_game(EXE, "full", confirm=True)["ok"] and card.writes[n:] == [], "a step changes the record only"
    # the game closes; game mode has let go of it, so nobody says so: the watch thread sees it
    m.exit_proc(PID)
    for _ in range(3):
        poll()
    assert AT._sess is None and card.current == FULL and mine()[-1] == ("apply_oc", 450, 2225, 100)
    assert session_file() is None and not watchdog_paused() and GO.get_applied_oc()["core"] == 450
    assert last_line() == "Overclock: kept until the game closed"

    card = eased_and_running(level=1)                           # game mode still watches it and reports the exit
    AT.end_active_session("shutdown cleanup")
    assert sess()["held"] == "released" and card.current == L75
    assert close()["handled"] is True and card.current == FULL and session_file() is None

    card = eased_and_running(sensitivity="strict")              # an error it had seen but not judged: a warning at
    error()                                                     # most, as at any release
    poll()
    assert sess()["pending"]
    AT.end_active_session("manual")
    assert texts() == [P.W_SURVIVED(NAME), P.HELD_RELEASED(NAME)] and game()["level"] == 2 and card.current == L50

    card = eased_and_running()                                  # the game is gone already: handed back at once
    m.exit_proc(PID)
    r = AT.end_active_session("monitor stopped")
    assert r["ended"] is True and card.current == FULL and AT._sess is None

    card = eased_and_running()                                  # whether it runs cannot be told: not raised
    AT._exe_running = lambda exe: None
    assert AT.end_active_session("manual")["released"] is True and card.current == L50

    card = eased_and_running()                                  # a card that cannot be read holds the session's
    card.read_ok = False                                        # values as far as anybody knows: not raised
    n = len(card.writes)
    assert AT.end_active_session("manual")["released"] is True and card.writes[n:] == []


def test_a_game_that_was_let_go_of_is_looked_after_again_when_it_is_found_again():
    card = eased_and_running()
    AT.end_active_session("monitor stopped")
    first = sess()
    n = len(card.writes)
    error()                                                     # while nobody was looking: never counted
    clock.now += 40
    r = AT.on_game_start(EXE, NAME, PID)                        # the monitor was started again and found it
    assert r["engaged"] is True and r["adopted"] is True and r["held"] is None
    assert sess() is first and first["held"] is None and first["released_from"] is None and watchdog_paused()
    assert card.writes[n:] == [] and session_file()["held"] is None, "its values were on the card all along"
    for _ in range(8):
        poll()
    assert decided == [] and card.current == L50
    close()
    assert card.current == FULL and last_line() == "Overclock: 50%, no problems"

    card = eased_and_running()                                  # the same when the event log cannot be asked, as it
    AT.end_active_session("monitor stopped")                    # is found again, how far it had got by then
    error()
    clock.now += 40
    m.EVENTS.fail = True
    assert AT.on_game_start(EXE, NAME, PID)["adopted"] is True
    m.EVENTS.fail = False
    for _ in range(8):
        poll()
    assert decided == [] and sess()["pending"] == [], "what was logged while nobody looked is still not counted"

    card = eased_and_running()                                  # its level was changed meanwhile: put on when it is
    AT.end_active_session("monitor stopped")                    # looked after again
    assert AT.step_game(EXE, "slower")["ok"] and card.current == L50
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["held"] is None and r["level"] == 3 and card.current == NONE

    card = eased_and_running()                                  # the user set something themselves meanwhile
    AT.end_active_session("monitor stopped")
    user_applies(300, 1500)
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["held"] == "manual" and card.current == (300, 1500)
    close()
    assert card.current == (300, 1500) and last_line() == "Overclock: left alone after you changed it"

    card = eased_and_running()                                  # ...also when it is not found again: their value
    AT.end_active_session("manual")                             # stays, during the game and after it
    user_applies(300, 1500)
    n = len(card.writes)
    poll()
    assert sess()["held"] == "manual" and state()["session"]["held"] == "manual"
    m.exit_proc(PID)
    for _ in range(3):
        poll()
    assert AT._sess is None and card.current == (300, 1500) and card.writes[n:] == [] and not watchdog_paused()


def test_switching_off_does_not_raise_the_clocks_under_the_game_either():
    card = eased_and_running(level=1)
    n = len(card.writes)
    r = AT.set_settings(enabled=False)
    assert r["ok"] is True and r["state"]["enabled"] is False and r["state"]["session"]["held"] == "released"
    assert like_the_fixture(r["state"], "off_released")
    assert card.current == L75 and card.writes[n:] == [] and texts() == [AT.STOPPED_KEPT(NAME)]
    card.current = (0, 0)
    poll()
    assert card.writes[n:] == [], "and with the switch off nothing is written, whatever happens"
    card.current = L75
    r = AT.set_settings(enabled=True)                           # on again: looked after again
    assert r["state"]["session"]["held"] is None and card.writes[n:] == [] and sess()["held"] is None
    close()
    assert card.current == FULL

    card = eased_and_running(level=1)                           # the game's own switch
    n = len(card.writes)
    r = AT.set_game_enabled(EXE, False)
    assert r["state"]["session"]["held"] == "released" and r["state"]["games"][0]["enabled"] is False
    assert card.current == L75 and card.writes[n:] == [] and texts() == [AT.STOPPED_KEPT(NAME)]
    assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "switched_off"} and sess()["held"] == "released"
    r = AT.set_game_enabled(EXE, True)
    assert r["state"]["session"]["held"] is None and card.writes[n:] == []
    m.exit_proc(PID)
    for _ in range(3):
        poll()
    assert card.current == FULL and AT._sess is None


def test_a_game_forgotten_while_it_runs_is_not_looked_after_for_the_rest_of_that_run():
    """It used to be put back on the full overclock at once (the card showed
    the idle sentence while it ran unlooked after)."""
    for forget in (lambda: AT.forget_game(EXE), AT.reset_all):
        card = eased_and_running(level=1)
        put_game(exe="cs2.exe", name="Counter-Strike 2")
        n = len(card.writes)
        r = forget()
        st = r["state"]
        assert r["ok"] is True and EXE not in [g["exe"] for g in st["games"]]
        assert st["session"]["held"] == "released" and card.current == L75 and card.writes[n:] == []
        assert st["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "forgotten"}
        assert like_the_fixture(st, "forgotten_released")
        # found again in the same run of the game: still not looked after, and no record is made
        assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "forgotten"}
        assert AT.try_engage_running_game()["reason"] == "forgotten" and game() is None and card.writes[n:] == []
        m.exit_proc(PID)
        for _ in range(3):
            poll()
        assert AT._sess is None and card.current == FULL and state()["game"] is None
        m.proc(PID + 1, exe=EXE)                                # its next start is a first start
        r = AT.on_game_start(EXE, NAME, PID + 1)
        assert r["engaged"] is True and r["level"] == 0 and card.current == FULL

    card = rig(held=(100, 500))                                 # handing back only lowers: done at once, and the
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}      # page still says why it is not looked after
    start()
    r = AT.forget_game(EXE)
    assert r["state"]["session"] is None and card.current == (100, 500) and session_file() is None
    assert r["state"]["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "forgotten"}
    assert like_the_fixture(r["state"], "forgotten")
    n = len(card.writes)
    assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "forgotten"} and card.writes[n:] == []
    assert AT.on_game_exit(EXE)["handled"] is False and state()["game"] is None
    assert start()["engaged"] is True and sess()["level"] == 0, "the next start is a first start"

    card = rig(held=(100, 500))                                 # the same run under another process (a relaunch
    start()                                                     # inside game mode's grace): a first start too
    AT.forget_game(EXE)
    m.exit_proc(PID)
    m.proc(5151, exe=EXE)
    assert AT.on_game_start(EXE, NAME, 5151)["engaged"] is True

    rig()                                                       # a game that is not running: nothing to say
    put_game(level=1)
    TRACKED[0] = None
    assert AT.forget_game(EXE)["state"]["game"] is None and read_json(AT.state_path())["forgotten"] == {}

    card = eased_and_running(level=1)                           # Vispora is restarted while that run of the game
    AT.forget_game(EXE)                                         # goes on: it is still not looked after, and still
    assert read_json(AT.state_path())["forgotten"] == {EXE: {"pid": PID, "name": NAME, "ts": clock.now}}
    die()                                                       # gets no more overclock before it closes
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    n = len(card.writes)
    assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "forgotten"}
    assert card.writes[n:] == [] and card.current == L75 and game() is None and AT._sess is None
    assert state()["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "forgotten"}
    m.exit_proc(PID)
    assert AT.on_game_exit(EXE)["handled"] is False             # it closes: the card is put back, the note is gone
    assert card.current == FULL and session_file() is None and read_json(AT.state_path())["forgotten"] == {}

    card = eased_and_running(level=1)                           # Windows was restarted since: whatever runs under
    AT.forget_game(EXE)                                         # that name now is another run
    die()
    clock.now += 3600
    AT._uptime_s = lambda: 600.0
    card.current = (0, 0)
    AT.recover_interrupted_session()
    assert AT.on_game_start(EXE, NAME, PID)["engaged"] is True and read_json(AT.state_path())["forgotten"] == {}


def test_another_game_is_not_taken_on_while_a_game_that_was_let_go_of_keeps_the_card():
    card = eased_and_running()
    AT.end_active_session("manual")                             # Force normal mode: that game is held off now
    n = len(card.writes)
    m.proc(5151, exe="cs2.exe")
    TRACKED[0] = {"exe": "cs2.exe", "display": "Counter-Strike 2", "pid": 5151}
    r = AT.on_game_start("cs2.exe", "Counter-Strike 2", 5151)   # at full overclock it would raise the card under it
    assert r == {"engaged": False, "reason": "another_game"} and card.writes[n:] == []
    assert sess()["exe"] == EXE and sess()["held"] == "released" and game("cs2.exe") is None
    m.exit_proc(PID)                                            # the first game closes
    for _ in range(3):
        poll()
    assert sess() is not None and sess()["exe"] == "cs2.exe", "the card is handed back and the other game picked up"
    assert sess()["pre"] == [450, 2225, 100] and card.current == FULL
    close(exe="cs2.exe", pid=5151)
    assert card.current == FULL and session_file() is None


def test_a_session_that_was_let_go_of_survives_the_app_quitting():
    """Vispora quits mid-game: the eased game keeps its clocks, the session
    file stays, and the next start finishes it."""
    card = eased_and_running()
    AT.end_active_session("app quit")
    n = len(card.writes)
    die()
    clock.now += 600
    m.exit_proc(PID)                                            # the game was closed before Vispora started again
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and card.current == FULL and session_file() is None
    assert decided == [] and game()["sessions"] == 1

    card = eased_and_running()                                  # still running at the next start: kept, and looked
    AT.end_active_session("app quit")                           # after again when the monitor finds it
    die()
    clock.now += 600
    n = len(card.writes)
    assert AT.recover_interrupted_session()["kept"] is True and card.writes[n:] == []
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["engaged"] is True and r["adopted"] is True and r["held"] is None and card.current == L50
    assert L50 == card.writes[-1] and FULL not in card.writes[n:]
    close()
    assert card.current == FULL


# ── the hand-back ─────────────────────────────────────────────────────
def test_the_hand_back_is_read_back_and_tried_again():
    """Right after a driver crash the first write often does not land.  It
    used to be written once and the session file deleted whatever happened."""
    card = rig(held=(100, 500))
    start()
    poll()
    card.drop = 2
    r = close()
    assert r["restored"] is True and card.current == (100, 500) and pauses == [5.0, 5.0]
    assert mine()[-3:] == [("apply_oc", 100, 500, 100)] * 3 and session_file() is None and texts() == []


def test_a_hand_back_that_cannot_be_confirmed_keeps_the_session_file():
    card = rig(held=(100, 500))
    put_game(level=1)
    start()
    poll()
    card.drop = 99
    r = close()
    assert r["handled"] is True and r["restored"] is False and card.current == L75
    assert mine()[-3:] == [("apply_oc", 100, 500, 100)] * 3 and pauses == [5.0, 5.0]
    f = session_file()
    assert f is not None and f["pre"] == [100, 500, 100] and f["writes"] == [[338, 1669]] and f["judged"] is True
    assert texts() == [AT.NOT_PUT_BACK(NAME)] and not watchdog_paused() and AT._sess is None
    assert game()["sessions"] == 1
    # the next start of the app finishes it, and counts nothing twice
    card.drop = 0
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and card.current == (100, 500) and session_file() is None
    assert game()["sessions"] == 1 and texts()[-1] == P.HANDED_BACK(NAME)

    card = rig(held=(100, 500))                                 # ...or the game's next launch does
    put_game(level=1)
    start()
    card.drop = 99
    close()
    card.drop = 0
    assert session_file() is not None
    n = len(mine())
    r = start(pid=5151)
    # that session had ended: what it owed is put back first, and the new launch is a session of its own
    assert r["engaged"] is True and r["adopted"] is False and sess()["pre"] == [100, 500, 100]
    assert mine()[n:] == [("apply_oc", 100, 500, 100), ("apply_oc", 338, 1669, 100)] and sess()["started"] == clock.now
    close(pid=5151)
    assert card.current == (100, 500) and session_file() is None

    card = rig(held=(450, 2225))                                # what is owed is more than the game runs on:
    put_game(level=1)                                           # not put back under the game that has just started
    start()
    card.drop = 99
    close()
    card.drop = 0
    n = len(card.writes)
    r = start(pid=5151)
    assert r["engaged"] is True and sess()["pre"] == [450, 2225, 100] and FULL not in card.writes[n:]
    assert card.current == L75
    close(pid=5151)
    assert card.current == FULL and session_file() is None, "handed back when that run closed"


def test_nothing_is_written_at_the_end_when_the_card_is_as_it_was_found():
    card = rig(held=(450, 2225))                                # the overclock is applied at start-up anyway
    start()
    poll()
    n = len(card.writes)
    close()
    assert card.writes[n:] == [] and card.current == FULL and session_file() is None and not watchdog_paused()
    assert GO.get_applied_oc() == {"core": 450, "mem": 2225, "power": 100, "active": True}


def test_nothing_is_written_at_the_start_when_the_app_already_holds_exactly_that():
    """With the overclock applied at start-up, every game start ran the full
    apply again (a process scan and several nvidia-smi calls: seconds on a
    real PC) to put on what was there already."""
    card = rig(profile=(450, 2225, 110), held=(450, 2225, 110))
    m.clear_logs()
    seq = GO.external_write_seq()
    r = start()
    assert r["engaged"] is True and r["applied_ok"] is True and card.writes == [] and mine() == []
    assert not m.ran("nvidia-smi") and GO.external_write_seq() == seq, "no process is started either"
    assert session_file()["writes"] == [[450, 2225]] and watchdog_paused(), "it is the session's value all the same"
    card.current = (0, 0)                                       # the driver drops the clocks: put back
    poll()
    assert card.current == FULL
    close()
    assert card.current == FULL and GO.get_applied_oc() == {"core": 450, "mem": 2225, "power": 110, "active": True}

    for held, now_on_card, readable in (((450, 2225, 100), FULL, True),     # another power limit
                                        ((450, 2225, 110), (0, 0), True),   # the card does not read it back
                                        ((450, 2225, 110), FULL, False),    # ...or cannot be read
                                        ((100, 500, 110), (100, 500), True)):
        card = rig(profile=(450, 2225, 110), held=held)
        card.current, card.read_ok = now_on_card, readable
        AT._supported_cache.update(ok=True)
        assert start()["engaged"] and mine() == [("apply_oc", 450, 2225, 110)], (held, now_on_card, readable)
    card = rig(held=(450, 2225))                                # an eased game is written, of course
    put_game(level=1)
    start()
    assert mine() == [("apply_oc", 338, 1669, 100)]


def test_a_change_the_user_makes_in_the_last_seconds_before_the_game_closes_stays():
    """The watch thread looks every 20 s.  A Reset or an Apply on the GPU page
    less than that before the game closes has not been seen by any poll: the
    exit itself must notice it, or it writes the old values over it."""
    def resets():
        AT.note_manual_change()
        GO.reset_oc()
        return (0, 0)

    def applies(note):
        user_applies(200, 900, note=note)
        return (200, 900)

    for change in (resets, lambda: applies(True), lambda: applies(False)):
        card = rig(held=(450, 2225))
        put_game(level=1)
        start()
        poll()
        want = change()
        n = len(mine())
        r = close()                                             # no poll in between
        assert r["handled"] is True and r["left"] == "manual" and card.current == want and mine()[n:] == []
        assert (GO.get_applied_oc()["core"], GO.get_applied_oc()["mem"]) == want and not watchdog_paused()
        assert session_file() is None and last_line() == "Overclock: left alone after you changed it"


def test_the_power_limit_is_handed_back_as_it_was_found_too():
    rig(profile=(450, 2225, 110), held=(100, 500, 90))         # the user runs 90%; the saved overclock says 110%
    start()
    assert mine() == [("apply_oc", 450, 2225, 110)] and sess()["pre"] == [100, 500, 90]
    close()
    assert mine()[-1] == ("apply_oc", 100, 500, 90) and GO.get_applied_oc()["power"] == 90

    rig(profile=(450, 2225, 110), held=(450, 2225, 90))         # the same clocks, another power limit: the game
    start()                                                     # sets 110%, and the exit must put 90% back
    assert mine() == [("apply_oc", 450, 2225, 110)]
    close()
    assert mine()[-1] == ("apply_oc", 450, 2225, 90) and GO.get_applied_oc()["power"] == 90

    for held, back in (((100, 500, 90), (100, 500)), ((450, 2225, 90), FULL)):     # ...also at the next start
        rig(profile=(450, 2225, 110), held=held)                                    # after a run that died
        start()
        die()
        m.exit_proc(PID)
        r = AT.recover_interrupted_session()
        assert r["recovered"] is True and mine()[-1] == ("apply_oc", back[0], back[1], 90), (held, mine())


def test_what_the_app_was_not_holding_before_a_game_is_not_held_after_it():
    """An overclock another tool put on the card (or an earlier run of the
    app): before the game Vispora enforced nothing.  After one game its drift
    watchdog wrote that value back within seconds whenever the user changed
    it or took it off in the other tool."""
    for level, before in ((0, (100, 500)), (0, FULL), (3, (100, 500)), (1, (500, 2400))):
        card = rig(apply_on_startup=False, card=Card(current=before))
        put_game(level=level)
        assert GO.get_applied_oc()["active"] is False
        start()
        assert sess()["pre"] == [before[0], before[1], 100] and sess()["pre_held"] is False
        assert session_file()["pre_held"] is False
        poll()
        assert GO.get_applied_oc()["active"] is (level < 3), "during the game it holds the session's values"
        close()
        assert card.current == before and session_file() is None and not watchdog_paused(), (level, before)
        assert GO.get_applied_oc() == {"core": 0, "mem": 0, "power": 100, "active": False}, (level, before)

    card = rig(held=(100, 500))                                 # what the app did hold, it holds again
    start()
    assert sess()["pre_held"] is True
    close()
    assert GO.get_applied_oc() == {"core": 100, "mem": 500, "power": 100, "active": True}

    card = rig(apply_on_startup=False, card=Card(current=(100, 500)))      # ...also across a run that died
    put_game(level=1)
    start()
    die()
    m.exit_proc(PID)
    assert AT.recover_interrupted_session()["recovered"] is True and card.current == (100, 500)
    assert GO.get_applied_oc()["active"] is False

    # the power limit that was set outside the app is read, not taken for the default, and put back
    card = rig(apply_on_startup=False, card=Card(current=(100, 500)))
    GO.read_live_state = lambda: {"ok": True, "power_limit_w": 345.0}      # 115% of this card's 300 W
    start()
    assert sess()["pre"] == [100, 500, 115] and mine() == [("apply_oc", 450, 2225, 100)]
    close()
    assert mine()[-1] == ("apply_oc", 100, 500, 115) and GO.get_applied_oc()["active"] is False
    for answer in ({"ok": False}, {"ok": True, "power_limit_w": 0.0}, None):        # it cannot be read: the default
        card = rig(apply_on_startup=False)
        GO.read_live_state = lambda: answer
        start()
        assert sess()["pre"] == [0, 0, 100]


# ═════════════════════════════════════════════════════════════════════
# A run that died mid-game
# ═════════════════════════════════════════════════════════════════════
def die():
    """Vispora is killed (or the PC stops): no session end runs."""
    s = AT._sess
    s["stop"].set()
    with AT._state_lock:
        AT._sess = None
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    GO._deactivate_oc_watchdog()                                # a new process holds nothing
    AT._last_closed = None
    AT._burst_tail.clear()                                      # (what was judged last is only in the record now)
    AT._failed_reads[0] = 0
    return s


def test_nothing_to_recover_without_a_session_file():
    card = rig()
    assert AT.recover_interrupted_session() == {"recovered": False, "reason": "no interrupted session"}
    start()
    close()
    assert AT.recover_interrupted_session()["recovered"] is False and card.writes.count((0, 0)) == 1


def test_a_game_still_running_at_the_next_start_keeps_its_session():
    """Vispora restarted while the game ran (13 of 399 sessions on the owner's
    PC).  Handing back here put the full overclock under a game that had been
    eased, until the monitor found it again."""
    card = rig(held=(450, 2225))
    put_game(level=2)
    start()
    poll()
    die()
    clock.now += 30
    n = len(card.writes)
    r = AT.recover_interrupted_session()
    assert r["recovered"] is False and r["kept"] is True and card.writes[n:] == [] and session_file() is not None
    AT._exe_running = lambda exe: None                          # cannot tell: the same
    assert AT.recover_interrupted_session()["kept"] is True
    AT._exe_running = _REAL_AT["_exe_running"]
    error(ago=10)                                               # while nobody was watching: not counted
    clock.now += 30
    r = AT.on_game_start(EXE, NAME, PID)                        # game mode finds it again
    assert r["engaged"] is True and r["adopted"] is True and r["level"] == 2
    assert sess()["pre"] == [450, 2225, 100] and sess()["started"] == T0 and card.current == L50
    poll()
    for _ in range(7):
        poll()
    assert decided == []
    close()
    assert card.current == FULL and round(game()["minutes"] * 60) == 180, "and its play time carried on"


def test_the_card_is_put_back_at_the_next_start():
    card = rig(held=(100, 500))
    put_game(level=1)
    start()
    poll()
    die()
    m.exit_proc(PID)
    clock.now += 300
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and r["to"] == (100, 500) and card.current == (100, 500)
    assert mine()[-1] == ("apply_oc", 100, 500, 100) and session_file() is None
    assert texts() == [P.HANDED_BACK(NAME)] and round(game()["minutes"] * 60) == 20 and game()["sessions"] == 1
    assert AT.recover_interrupted_session()["recovered"] is False, "once"


def test_after_a_restart_of_the_pc_the_card_is_left_as_it_is():
    """A power cut: the card came back with no overclock.  Putting the old
    values back then would apply an overclock at start-up nobody asked for."""
    for level, now_on_card in ((1, (0, 0)), (3, (0, 0)), (1, (200, 900))):
        card = rig(held=(100, 500))
        put_game(level=level)
        start()
        die()
        m.exit_proc(PID)
        card.current = now_on_card                              # no overclock after a restart, or set since
        n = len(card.writes)
        r = AT.recover_interrupted_session()
        assert r["recovered"] is False and card.writes[n:] == [] and card.current == now_on_card, (level, r)
        assert session_file() is None and texts() == []

    card = rig(held=(100, 500))                                 # a card that cannot be read: the file is kept
    start()
    die()
    m.exit_proc(PID)
    card.read_ok = False
    assert AT.recover_interrupted_session()["recovered"] is False and session_file() is not None
    card.read_ok = True
    assert AT.recover_interrupted_session()["recovered"] is True and session_file() is None


def test_a_leftover_that_cannot_be_settled_is_taken_over_by_the_next_session():
    """The file of a session that was never confirmed back is all that knows
    what the card held before it.  Another game starting must not lose it."""
    card = rig(held=(100, 500))
    put_game(level=1)
    start()
    die()
    m.exit_proc(PID)
    card.drop = 3                                               # the driver takes none of the three tries
    r = start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    assert r["engaged"] is True and sess()["pre"] == [100, 500, 100], "not whatever the card holds now"
    assert session_file()["exe"] == "cs2.exe" and card.current == FULL
    assert sess()["writes"] == [[338, 1669], [450, 2225]], "what the old session wrote is this one's to undo too"
    close(exe="cs2.exe", pid=5151)
    assert card.current == (100, 500) and session_file() is None

    card = rig(held=(100, 500))                                 # another program has the card by now: the old
    put_game(level=1)                                           # session's values are not on it any more, so they
    start()                                                     # are not this session's to undo
    die()
    m.exit_proc(PID)
    card.intruder = (50, 0)
    r = start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    assert r["engaged"] is True and sess()["writes"] == [[450, 2225]] and sess()["pre"] == [100, 500, 100]
    assert session_file()["exe"] == "cs2.exe"

    card = rig(held=(100, 500))                                 # a card that cannot be asked right now: what it
    put_game(level=1)                                           # holds is not known, so nothing is taken on and
    start()                                                     # nothing is lost
    die()
    m.exit_proc(PID)
    card.read_ok = False
    n = len(card.writes)
    r = start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    assert r == {"engaged": False, "reason": "card_unreadable"} and card.writes[n:] == [] and AT._sess is None
    assert session_file()["exe"] == EXE and session_file()["pre"] == [100, 500, 100]
    assert [sec for sec, _ in later] == [30.0], "and it is looked at again later"
    card.read_ok = True
    r = start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    assert r["engaged"] is True and sess()["pre"] == [100, 500, 100] and card.current == FULL
    close(exe="cs2.exe", pid=5151)
    assert card.current == (100, 500) and session_file() is None


def test_a_session_the_user_had_taken_over_is_left_alone_at_the_next_start():
    card = rig(held=(100, 500))
    start()
    user_applies(300, 1500)
    poll()
    die()
    m.exit_proc(PID)
    n = len(card.writes)
    assert AT.recover_interrupted_session()["recovered"] is False
    assert card.writes[n:] == [] and card.current == (300, 1500) and session_file() is None


def test_a_session_carried_on_after_the_user_took_over_still_says_so():
    """The page shows "the card did not accept the overclock" before any other
    line.  A session that is left alone asks nothing of the card, so it must
    not report a refused write, also when it was picked up from the file of a
    run that died and has never written anything itself."""
    card = rig(held=(100, 500))
    start()
    user_applies(300, 1500)
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    n = len(card.writes)
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["engaged"] is True and r["adopted"] is True and r["held"] == "manual" and card.writes[n:] == []
    shown = state()["session"]
    assert shown["held"] == "manual" and shown["applied_ok"] is True
    poll()
    close()
    assert card.writes[n:] == [] and card.current == (300, 1500) and session_file() is None


def test_the_hand_back_at_the_next_start_is_read_back_and_tried_again_too():
    card = rig(held=(100, 500))
    put_game(level=1)
    start()
    poll()
    die()
    m.exit_proc(PID)
    card.drop = 1                                               # the driver does not take the first write
    n = len(mine())
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and card.current == (100, 500) and pauses == [5.0]
    assert mine()[n:] == [("apply_oc", 100, 500, 100)] * 2 and session_file() is None
    assert texts() == [P.HANDED_BACK(NAME)]

    card = rig(held=(100, 500))                                 # it takes none of the three
    put_game(level=1)
    start()
    poll()
    die()
    m.exit_proc(PID)
    card.drop = 99
    n = len(mine())
    r = AT.recover_interrupted_session()
    assert r["recovered"] is False and r["settled"] is False and card.current == L75
    assert mine()[n:] == [("apply_oc", 100, 500, 100)] * 3 and pauses == [5.0, 5.0]
    f = session_file()
    assert f is not None and f["judged"] is True and f["play_s"] == 0.0 and f["pre"] == [100, 500, 100]
    assert texts() == [], "it does not say the overclock was put back"
    assert game()["sessions"] == 1
    card.drop = 0                                               # the start after that finishes it, and counts
    r = AT.recover_interrupted_session()                        # nothing twice
    assert r["recovered"] is True and card.current == (100, 500) and session_file() is None
    assert texts() == [P.HANDED_BACK(NAME)] and game()["sessions"] == 1

    card = rig(profile=(450, 2225, 110), held=(450, 2225))     # the clocks are as they were found, the power limit
    start()                                                     # is not: that is put back as well
    assert mine() == [("apply_oc", 450, 2225, 110)]
    die()
    m.exit_proc(PID)
    n = len(mine())
    assert AT.recover_interrupted_session()["recovered"] is True and mine()[n:] == [("apply_oc", 450, 2225, 100)]

    card = rig(held=(100, 500))                                 # a session the user had taken over, with the card
    start()                                                     # back on a value that session wrote: left alone
    user_applies(300, 1500)
    poll()
    assert sess()["held"] == "manual"
    die()
    m.exit_proc(PID)
    card.current = FULL
    n = len(card.writes)
    assert AT.recover_interrupted_session()["recovered"] is False and card.writes[n:] == [] and session_file() is None


def test_a_game_that_is_found_but_not_looked_after_still_gets_its_card_put_back():
    """Vispora died mid-game, the game still runs, and before game mode finds
    it again the switch (or the game's own) was turned off.  What the dead
    run left on the card is put back when the game is found."""
    for switch_off in (lambda: AT.set_settings(enabled=False), lambda: AT.set_game_enabled(EXE, False)):
        card = rig(held=(100, 500))
        start()
        poll()
        die()
        clock.now += 30
        assert AT.recover_interrupted_session()["kept"] is True
        assert switch_off()["ok"] is True
        r = AT.on_game_start(EXE, NAME, PID)
        assert r["engaged"] is False and r["reason"] in ("off", "switched_off")
        assert card.current == (100, 500) and session_file() is None and AT._sess is None
        assert mine()[-1] == ("apply_oc", 100, 500, 100)

    card = rig(held=(450, 2225))                                # ...unless that would raise the clocks under the
    put_game(level=2)                                           # game: then it waits until the game has closed
    start()
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    AT.set_settings(enabled=False)
    n = len(card.writes)
    assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "off"}
    assert card.writes[n:] == [] and card.current == L50 and session_file() is not None
    assert [sec for sec, _ in later] == [30.0]
    m.exit_proc(PID)
    assert AT.on_game_exit(EXE)["handled"] is False             # game mode reports the exit: settled right then
    assert card.current == FULL and session_file() is None


def test_a_session_kept_at_the_start_is_looked_at_again_until_the_game_is_found_or_gone():
    """With the monitor not started by itself, or a game that closes before
    the monitor's first look, nobody ever carried the kept session on: the
    card stayed on its values, and the saved overclock was not applied at
    start-up in that run."""
    card = rig(held=(450, 2225))
    put_game(level=2)
    start()
    poll()
    die()
    clock.now += 30
    r = AT.recover_interrupted_session()
    assert r["kept"] is True and [sec for sec, _ in later] == [30.0]
    assert AT.recover_interrupted_session()["kept"] is True and len(later) == 1, "one look at a time"
    n = len(card.writes)
    m.EVENTS.add("driver_error", T0 + 25)                       # (around its last heartbeat)
    clock.now += 30
    later.pop(0)[1]()                                           # still running, not found yet: nothing, and again
    assert card.writes[n:] == [] and session_file() is not None and [sec for sec, _ in later] == [30.0]
    assert decided == [] and session_file()["judged"] is False, "a game that is still running is not judged as closed"
    AT._exe_running = lambda exe: None                          # (cannot tell: the same)
    later.pop(0)[1]()
    assert card.writes[n:] == [] and [sec for sec, _ in later] == [30.0]
    AT._exe_running = _REAL_AT["_exe_running"]
    m.exit_proc(PID)                                            # the game closes; the monitor never looked
    clock.now += 30
    later.pop(0)[1]()
    assert card.current == FULL and session_file() is None and later == []
    assert texts()[0] == P.HANDED_BACK(NAME) and game()["sessions"] == 1 and round(game()["minutes"] * 60) == 20
    assert [d[3] for d in decided] == ["warning"] and decided[0][4]["ended_game"] is True, "judged then, once"

    card = rig(held=(450, 2225))                                # the monitor finds the game first: the look ends
    put_game(level=2)
    start()
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    assert AT.on_game_start(EXE, NAME, PID)["adopted"] is True
    n = len(card.writes)
    later.pop(0)[1]()
    assert later == [] and sess() is not None and card.writes[n:] == [] and session_file() is not None


def test_a_session_from_before_windows_was_restarted_is_never_carried_on():
    """The PC stopped mid-game (a driver bugcheck, a power cut) and the user
    is back in the game before Vispora starts, or another program runs under
    the same name.  "Still running" was decided by the name alone: the old
    session was kept and carried on, the crash that stopped the PC was never
    judged, and its old "before" was handed back in a new Windows session."""
    def stopped_pc():
        card = interrupted()                                    # (last heartbeat at T0+60)
        m.proc(5151, exe=EXE)                                   # a process of that name is running at the start
        m.EVENTS.add("driver_error", T0 + 70)
        clock.now = T0 + 3600
        card.current = (0, 0)                                   # (as after any restart)
        return card

    card = stopped_pc()                                         # control: Windows has been up all along
    assert AT.recover_interrupted_session()["kept"] is True and decided == []

    card = stopped_pc()
    AT._uptime_s = lambda: 600.0                                # Windows was started ten minutes ago
    n = len(card.writes)
    r = AT.recover_interrupted_session()
    assert r.get("kept") is None and r["judged"] == "driver_error" and session_file() is None
    assert game()["level"] == 1 and card.writes[n:] == [] and later == []
    r = AT.on_game_start(EXE, NAME, 5151)
    assert r["engaged"] is True and r["adopted"] is False and sess()["pre"] == [0, 0, 100]

    card = stopped_pc()                                         # the game is found before the start-up check ran
    AT._uptime_s = lambda: 600.0
    r = AT.on_game_start(EXE, NAME, 5151)
    assert r["engaged"] is True and r["adopted"] is False, "not carried on with what was on the card a boot ago"
    assert sess()["pre"] == [0, 0, 100] and sess()["started"] == clock.now and sess()["level"] == 1
    assert [d[3] for d in decided] == ["ease"] and decided[0][4]["ended_game"] is True

    card = stopped_pc()                                         # the heartbeat is from this run of Windows
    AT._uptime_s = lambda: 3600.0 + 10 * 60                     # (started ten minutes before the session)
    assert AT.recover_interrupted_session()["kept"] is True
    card = stopped_pc()                                         # how long Windows is up cannot be told: as before
    AT._uptime_s = lambda: None
    assert AT.recover_interrupted_session()["kept"] is True
    assert AT._windows_restarted_since("x") is False


def test_a_leftover_is_only_carried_on_while_the_card_still_holds_what_it_wrote():
    """A session file left on disk was carried on with its old "before"
    whatever had happened to the card since.  After the user had saved and
    applied a lower overclock, the exit wrote the old, higher one back over
    it: above the saved profile, and held there by the drift watchdog."""
    # A: the hand-back could not be confirmed; the user lowers the overclock; the game is started again
    card = rig(held=(450, 2225))
    put_game(level=2)
    start()
    card.drop = 99
    close()
    card.drop = 0
    assert session_file() is not None and card.current == L50
    save_profile(300, 1500)
    assert _REAL_GO["apply_oc"](core_offset_mhz=300, mem_offset_mhz=1500, power_pct=100)["ok"]
    clock.now += 3600
    r = start(pid=5151)
    assert r["engaged"] is True and r["adopted"] is False and sess()["pre"] == [300, 1500, 100]
    assert card.current == (150, 750) and sess()["started"] == clock.now
    close(pid=5151)
    assert card.current == (300, 1500) and FULL not in card.writes[-3:], "what the user applied, not the old overclock"
    assert GO.get_applied_oc()["core"] == 300 and session_file() is None

    # B: Vispora died mid-game and was started again; the user lowers the overclock before the game is found
    card = rig(held=(450, 2225))
    put_game(level=1)
    start()
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    save_profile(300, 1500)
    assert _REAL_GO["apply_oc"](core_offset_mhz=300, mem_offset_mhz=1500, power_pct=100)["ok"]
    n = len(card.writes)
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["engaged"] is True and r["adopted"] is True and r["held"] == "manual"
    assert texts() == [AT.CHANGED_MEANWHILE(NAME)] and not watchdog_paused()
    poll()
    close()
    assert card.current == (300, 1500) and card.writes[n:] == [], "nothing is written over it, then or at the exit"
    assert GO.get_applied_oc()["core"] == 300 and session_file() is None

    # the dead run's clocks are no longer on the card for another reason (the driver dropped them): left alone too
    card = rig(held=(450, 2225))
    put_game(level=1)
    start()
    poll()
    die()
    clock.now += 30
    AT.recover_interrupted_session()
    card.current = (0, 0)
    n = len(card.writes)
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["held"] == "manual" and card.writes[n:] == []

    # the card cannot be asked: what the leftover says is not taken on trust, and it is not lost either
    card = rig(held=(450, 2225))
    put_game(level=1)
    start()
    poll()
    die()
    clock.now += 30
    AT.recover_interrupted_session()
    before = session_file()
    card.read_ok = False
    n = len(card.writes)
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert AT.on_game_start(EXE, NAME, PID) == {"engaged": False, "reason": "card_unreadable"}
    assert card.writes[n:] == [] and session_file() == before and [sec for sec, _ in later] == [30.0]
    assert state()["game"]["reason"] == "card_unreadable"
    card.read_ok = True
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["adopted"] is True and r["held"] is None and sess()["pre"] == [450, 2225, 100] and card.current == L75

    # it had written nothing (the run died before its first write): nothing is owed, a session of its own
    card = rig(held=(450, 2225))
    put_game(level=1)
    start()
    die()
    rec = session_file()
    rec["writes"] = []
    write_json(AT.session_path(), rec)
    clock.now += 30
    r = AT.on_game_start(EXE, NAME, PID)
    assert r["engaged"] is True and r["adopted"] is False and sess()["started"] == clock.now


def test_a_session_file_that_cannot_be_removed_is_never_taken_for_a_run_that_died():
    """Another program held the file open for a moment (a scanner, a backup
    tool) when a normal exit removed it.  The file stayed as the last
    heartbeat left it, and the next start judged the same driver errors a
    second time (a game eased on one crash) and wrote the old "before"."""
    def refuse(times):
        real, refused = os.remove, []

        def remove(path, *a, **k):
            if os.path.normcase(str(path)) == os.path.normcase(AT.session_path()) and len(refused) < times:
                refused.append(path)
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return real(path, *a, **k)
        os.remove = remove
        return real, refused

    card = rig()                                                # refused once: tried again a moment later
    start()
    poll()
    real, refused = refuse(1)
    try:
        close()
    finally:
        os.remove = real
    assert len(refused) == 1 and session_file() is None and pauses == [0.05]

    card = rig()                                                # held longer: made into a record of nothing
    start()
    poll()
    error()                                                     # one driver crash closes the game
    clock.now += 12
    real, refused = refuse(10)
    try:
        close()
    finally:
        os.remove = real
    assert len(refused) == 10 and session_file() == {"version": 2, "closed": True}
    assert texts() == [P.W_ENDED(NAME)] and game()["level"] == 0 and game()["sessions"] == 1
    user_applies(450, 2225)                                     # (the user applies the overclock by hand afterwards)
    n = len(card.writes)
    clock.now += 3600
    assert AT.recover_interrupted_session() == {"recovered": False, "reason": "no interrupted session"}
    assert len(decided) == 1 and game()["sessions"] == 1 and card.writes[n:] == [], "nothing twice, nothing taken off"
    assert not os.path.exists(AT.session_path()), "and it is removed as soon as that is possible"

    card = rig()                                                # the same game started again in the same run:
    start()                                                     # a session of its own
    for _ in range(3):
        poll()
    real, refused = refuse(10)
    try:
        close()
    finally:
        os.remove = real
    clock.now += 600
    r = start(pid=5151)
    assert r["adopted"] is False and sess()["started"] == clock.now and sess()["play_s"] == 0.0
    close(pid=5151)
    assert game()["sessions"] == 2 and round(game()["minutes"] * 60) == 60 and session_file() is None

    card = rig()                                                # the file cannot even be written: logged, no more
    start()
    real, refused = refuse(99)
    AT.atomic_write_json = lambda path, data: False if path == AT.session_path() else _REAL_AT["atomic_write_json"](
        path, data)
    try:
        assert close()["handled"] is True and card.current == (0, 0) and AT._sess is None
    finally:
        os.remove = real


def test_carrying_a_kept_session_on_does_not_lose_its_file_when_a_save_fails():
    """The game's record could not be saved at that moment (the file held by
    another program, a full disk): the error path deleted the session file,
    the only thing that knew what the card was owed."""
    for held, level, on_card in (((450, 2225), 1, L75), (None, 0, FULL)):
        card = rig(held=held, apply_on_startup=held is not None)
        put_game(level=level)
        start()
        poll()
        die()
        clock.now += 30
        assert AT.recover_interrupted_session()["kept"] is True
        before, n = session_file(), len(card.writes)
        AT.atomic_write_json = lambda path, data: (False if path == AT.state_path()
                                                   else _REAL_AT["atomic_write_json"](path, data))
        r = AT.on_game_start(EXE, NAME, PID)
        AT.atomic_write_json = _REAL_AT["atomic_write_json"]
        assert r["engaged"] is False and r["reason"] == "error" and AT._sess is None
        assert session_file() == before and card.writes[n:] == [] and card.current == on_card
        assert [sec for sec, _ in later] == [30.0], "and it is still looked at again"
        m.exit_proc(PID)
        later.pop(0)[1]()
        assert card.current == (held or (0, 0)) and session_file() is None

    card = rig(held=(450, 2225))                                # ...or the session file itself, a moment later
    put_game(level=1)
    start()
    poll()
    die()
    clock.now += 30
    AT.recover_interrupted_session()
    before = session_file()
    AT.atomic_write_json = lambda path, data: (False if path == AT.session_path()
                                               else _REAL_AT["atomic_write_json"](path, data))
    r = AT.on_game_start(EXE, NAME, PID)
    AT.atomic_write_json = _REAL_AT["atomic_write_json"]
    assert r["reason"] == "error" and session_file() == before and AT._sess is None


def test_a_session_file_from_3_6_8_is_still_handed_back():
    """The old tuner's session file (3.6.8, 3.7.0) keeps its old name.  It is
    handed back once, by the same rules, and then removed."""
    old = {"exe": EXE, "started": T0 - 500, "pre": {"core": 100, "mem": 500}, "applied": {"core": 175, "mem": 800}}
    card = rig()
    write_json(AT.v1_session_path(), old)
    card.current = (175, 800)
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and card.current == (100, 500)
    assert not os.path.exists(AT.v1_session_path()) and session_file() is None
    assert mine() == [("apply_oc", 100, 500, None)], "the old tuner never touched the power limit"
    assert texts() == [P.HANDED_BACK(NAME)], "by the game's name, not its exe"
    assert AT.recover_interrupted_session() == {"recovered": False, "reason": "no interrupted session"}, "once"

    card = rig()                                                # it had written nothing
    write_json(AT.v1_session_path(), dict(old, applied=None))
    assert AT.recover_interrupted_session()["recovered"] is False and card.writes == []
    assert not os.path.exists(AT.v1_session_path())

    card = rig()                                                # that game is still running, and it had been taken
    write_json(AT.v1_session_path(), dict(old, pre={"core": 450, "mem": 2225}))     # BELOW what was there before:
    card.current = (175, 800)                                   # the clocks are not raised under it
    m.proc(PID, exe=EXE)
    r = AT.recover_interrupted_session()
    assert r["recovered"] is False and r["kept"] is True and card.writes == [] and len(later) == 1
    assert read_json(AT.v1_session_path()) == dict(old, pre={"core": 450, "mem": 2225}), "left exactly as it was"
    later.pop()[1]()                                            # 30 s later: still running
    assert card.writes == [] and len(later) == 1
    m.exit_proc(PID)
    later.pop()[1]()                                            # closed: put back now
    assert card.current == FULL and not os.path.exists(AT.v1_session_path()) and later == []

    card = rig()                                                # the game is found before it closes: its session
    write_json(AT.v1_session_path(), dict(old, pre={"core": 450, "mem": 2225}))     # takes over what is owed
    card.current = (175, 800)
    m.proc(PID, exe=EXE)
    assert AT.recover_interrupted_session()["kept"] is True
    put_game(level=3)
    r = start()
    assert r["engaged"] is True and sess()["pre"] == [450, 2225, 100] and card.current == NONE
    assert not os.path.exists(AT.v1_session_path()) and session_file()["exe"] == EXE
    close()
    assert card.current == FULL and session_file() is None

    card = rig(held=(100, 500))                                 # a build before this one wrote today's shape under
    put_game(level=1)                                           # the old name: still read from there
    start()
    die()
    m.exit_proc(PID)
    os.replace(AT.session_path(), AT.v1_session_path())
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and card.current == (100, 500) and not os.path.exists(AT.v1_session_path())


def interrupted(sensitivity="strict", **game_over):
    """A session that was polled for a minute and then died with its game."""
    card = rig(held=(100, 500), sensitivity=sensitivity)
    if game_over:
        put_game(**game_over)
    start()
    for _ in range(3):
        poll()                                                  # last heartbeat at T0+60
    die()
    m.exit_proc(PID)
    return card


def test_driver_errors_around_the_last_heartbeat_are_judged_once_at_the_next_start():
    interrupted()
    m.EVENTS.add("driver_error", T0 + 70)                       # after the last poll: the PC froze with the session
    m.EVENTS.add("driver_error", T0 + 100)
    clock.now = T0 + 3600
    r = AT.recover_interrupted_session()
    assert r["judged"] == "driver_error" and game()["level"] == 1
    assert [d[1:4] for d in decided] == [(T0 + 70, T0 + 100, "ease")] and decided[0][4]["ended_game"] is True
    assert texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]
    assert AT.recover_interrupted_session()["recovered"] is False and len(decided) == 1

    interrupted()                                               # more than a minute after it: not this session's
    m.EVENTS.add("driver_error", T0 + 121)
    clock.now = T0 + 3600
    assert AT.recover_interrupted_session().get("judged") is None and decided == [] and game()["level"] == 0

    interrupted()                                               # an incident the session had seen but not judged
    with _REAL_OPEN(AT.session_path(), encoding="utf-8") as f:
        rec = json.load(f)
    rec["judged_until"] = T0 + 45                               # (the mark stays before a waiting incident)
    with _REAL_OPEN(AT.session_path(), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    m.EVENTS.add("driver_error", T0 + 30)                       # judged while the session was alive
    m.EVENTS.add("driver_error", T0 + 50)                       # seen, still waiting when the run died
    clock.now = T0 + 3600
    AT.recover_interrupted_session()
    assert [d[1:3] for d in decided] == [(T0 + 50, T0 + 50)], "what was already judged is not judged again"


def test_a_vispora_started_again_days_later_counts_nothing():
    """Vispora was killed; the PC ran on.  Two days later it logs a driver
    error, crashes and restarts, and only then is Vispora started again."""
    card = interrupted()
    card.current = (0, 0)
    day = 86400.0
    m.EVENTS.add("boot", T0 - 5 * day)
    m.EVENTS.add("app_crash", T0 + day, exe="notepad.exe")              # (Windows was alive and logging)
    m.EVENTS.add_xml("System", SERVICE_RECORD % GE._iso(T0 + 3600, False))
    m.EVENTS.add("driver_error", T0 + 2 * day)
    m.EVENTS.add("gpu_bugcheck", T0 + 2 * day + 600)
    m.EVENTS.add("boot", T0 + 2 * day + 610)
    clock.now = T0 + 2 * day + 900
    r = AT.recover_interrupted_session()
    assert r.get("judged") is None and decided == [] and game()["level"] == 0 and texts() == [] and later == []


SERVICE_RECORD = ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
                  "<Provider Name='Service Control Manager'/><EventID>7036</EventID><Level>4</Level>"
                  "<TimeCreated SystemTime='%s'/><EventRecordID>0</EventRecordID><Channel>System</Channel>"
                  "</System><EventData><Data>x</Data></EventData></Event>")


def test_a_pc_that_stopped_with_the_session_is_judged_from_the_next_start_of_windows():
    # Windows blames the graphics driver, and went quiet together with the session
    interrupted()
    m.EVENTS.add("gpu_bugcheck", T0 + 900)                      # written at the next start, 14 minutes later
    m.EVENTS.add("boot", T0 + 910)
    clock.now = T0 + 1000
    r = AT.recover_interrupted_session()
    assert r["judged"] == "gpu_bugcheck" and game()["level"] == 1
    assert decided[0][1] == T0 + 60 and decided[0][4]["ended_game"] is True
    assert texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]

    # the same records, but Windows went on logging long after the session's last heartbeat
    interrupted()
    m.EVENTS.add_xml("System", SERVICE_RECORD % GE._iso(T0 + 500, False))
    m.EVENTS.add("gpu_bugcheck", T0 + 900)
    m.EVENTS.add("boot", T0 + 910)
    clock.now = T0 + 1000
    assert AT.recover_interrupted_session().get("judged") is None and decided == []
    assert texts() == [P.HANDED_BACK(NAME)]

    # a plain unexpected shutdown: noted, nothing changed, one more look two minutes later
    interrupted()
    m.EVENTS.add("unexpected_shutdown", T0 + 900)
    m.EVENTS.add("boot", T0 + 910)
    clock.now = T0 + 1000
    r = AT.recover_interrupted_session()
    assert r["judged"] == "unexpected_shutdown" and game()["level"] == 0 and decided == []
    assert texts() == [P.HANDED_BACK(NAME), P.PC_STOPPED(NAME)] and [s for s, _ in later] == [120.0]
    later[0][1]()                                               # nothing new was written meanwhile
    assert decided == [] and len(texts()) == 2 and len(later) == 1
    m.EVENTS.add("gpu_bugcheck", T0 + 1030, provider="Microsoft-Windows-WER-SystemErrorReporting")
    clock.now = T0 + 1120
    later[0][1]()                                               # Windows wrote its crash record late
    assert game()["level"] == 1 and texts()[-1] == P.EASED_STRICT(NAME, 0, 1)

    # no restart of Windows at all (only Vispora died): nothing
    interrupted()
    clock.now = T0 + 1000
    assert AT.recover_interrupted_session().get("judged") is None and texts() == [P.HANDED_BACK(NAME)]

    # a later crash of the PC is not the first start after the session
    interrupted()
    m.EVENTS.add("boot", T0 + 200)                              # a clean restart first
    m.EVENTS.add("gpu_bugcheck", T0 + 5000)
    m.EVENTS.add("boot", T0 + 5010)
    clock.now = T0 + 6000
    assert AT.recover_interrupted_session().get("judged") is None and decided == []


def test_what_stopped_the_pc_is_read_from_the_system_log_alone():
    """Only the System log's records are used for it.  The Application log
    was asked as well, so when that one did not answer (it is the slow one
    right after a crash of the PC) the judgement was put off, although
    nothing in it was needed."""
    interrupted()
    m.EVENTS.add("gpu_bugcheck", T0 + 900)                      # written at the next start, 14 minutes later
    m.EVENTS.add("boot", T0 + 910)
    clock.now = T0 + 1000
    real, asked = m.EVENTS.EvtQuery, []

    def system_only(channel, xpath, flags):
        asked.append(channel)
        return (0, 5) if channel == "Application" else real(channel, xpath, flags)     # (5: access denied)

    m.EVENTS.EvtQuery = system_only
    try:
        r = AT.recover_interrupted_session()
    finally:
        del m.EVENTS.EvtQuery                                   # (the fake's own method again)
    assert r.get("judged") == "gpu_bugcheck" and not r.get("unread"), r
    assert game()["level"] == 1 and texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]
    assert asked and set(asked) == {"System"}, asked
    with AT._state_lock:
        assert AT._load_state()["unread"] == [], "nothing is left waiting for a later look"


def test_recovery_with_an_unreadable_event_log_counts_nothing():
    card = interrupted()
    m.EVENTS.add("driver_error", T0 + 70)
    m.EVENTS.fail = True
    clock.now = T0 + 3600
    r = AT.recover_interrupted_session()
    m.EVENTS.fail = False
    assert r["recovered"] is True and card.current == (100, 500) and decided == [] and session_file() is None


# ═════════════════════════════════════════════════════════════════════
# Files: the upgrade, and files that cannot be read
# ═════════════════════════════════════════════════════════════════════
def write_json(path, data):
    with _REAL_OPEN(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_json(path):
    with _REAL_OPEN(path, encoding="utf-8") as f:
        return json.load(f)


V1_STATE = {"cod.exe": {"enabled": True, "mode": "competitive", "current_core": 525, "current_mem": 2700,
                        "baseline_core": 450, "baseline_mem": 2225, "session_count": 88}}


V1_SETTINGS = {"enabled_globally": True, "default_per_game_enabled": True}


def raw(path):
    with _REAL_OPEN(path, "rb") as f:
        return f.read()


def test_the_upgrade_starts_switched_off_for_everyone_with_a_note():
    """The old switch did nothing until each game was opted in.  Carried over,
    it would have put the saved overclock on in every game for people who
    never asked for that.

    This version keeps its own files.  The old tuner's are only read: someone
    who goes back to 3.7.0 finds its settings and its games as it left them
    (they used to be written over and moved aside, and 3.7.0 then failed on
    every load of its game list)."""
    card = rig()
    os.remove(AT.settings_path())                               # this version has never run on this PC
    write_json(AT.v1_settings_path(), V1_SETTINGS)
    write_json(AT.v1_state_path(), V1_STATE)
    before = raw(AT.v1_settings_path()), raw(AT.v1_state_path())
    st = state()
    assert st["enabled"] is False and st["upgrade_note"] is True and st["old_switch_was_on"] is True
    assert st["games"] == [] and st["history"] == []
    assert read_json(AT.settings_path()) == {
        "version": 2, "enabled": False, "notify": True, "sensitivity": "balanced", "upgrade_note": True,
        "old_switch_was_on": True, "told_in_games": False}
    assert start() == {"engaged": False, "reason": "off"} and card.writes == []
    assert AT.dismiss_note()["state"]["upgrade_note"] is False
    assert AT.set_settings(enabled=True)["state"]["enabled"] is True
    st = state()
    assert st["upgrade_note"] is False and st["old_switch_was_on"] is True
    start()                                                     # a whole game later, with a session file and a record
    poll()
    assert os.path.basename(AT.session_path()) == "adaptive_tuning_v2_session.json" and session_file() is not None
    assert not os.path.exists(AT.v1_session_path()), "the open session is not where 3.7.0 would delete it unread"
    close()
    assert read_json(AT.state_path())["version"] == 2 and game()["sessions"] == 1
    assert (raw(AT.v1_settings_path()), raw(AT.v1_state_path())) == before, "exactly as 3.7.0 left them"
    assert not os.path.exists(AT.state_v1_path()), "and nothing is moved aside"
    folder = os.path.dirname(AT.state_path())
    assert sorted(n for n in os.listdir(folder) if n.startswith("adaptive_tuning")) == [
        "adaptive_tuning_settings.json", "adaptive_tuning_state.json", "adaptive_tuning_v2_games.json",
        "adaptive_tuning_v2_settings.json"]

    rig()                                                       # the old switch was off; only the games file is there
    os.remove(AT.settings_path())
    write_json(AT.v1_state_path(), V1_STATE)
    st = state()
    assert st["enabled"] is False and st["upgrade_note"] is True and st["old_switch_was_on"] is False
    assert game() is None and read_json(AT.v1_state_path()) == V1_STATE

    rig()                                                       # a build before this one had moved the old record aside
    os.remove(AT.settings_path())
    write_json(AT.state_v1_path(), V1_STATE)
    assert state()["upgrade_note"] is True and read_json(AT.state_v1_path()) == V1_STATE

    rig()                                                       # ...or had written today's shape under the old names:
    os.remove(AT.settings_path())                               # that is not the old tuner's, so there is no note
    write_json(AT.v1_settings_path(), dict(AT._default_settings(), enabled=True))
    write_json(AT.v1_state_path(), {"version": 2, "games": {}, "history": [], "last_sessions": {}})
    st = state()
    assert st["enabled"] is False and st["upgrade_note"] is False

    rig()                                                       # once this version has its settings, an old file
    write_json(AT.v1_settings_path(), V1_SETTINGS)              # turning up later changes nothing
    st = state()
    assert st["enabled"] is True and st["upgrade_note"] is False

    rig()                                                       # a new install: off, and no note
    os.remove(AT.settings_path())
    st = state()
    assert st["enabled"] is False and st["upgrade_note"] is False and not os.path.exists(AT.settings_path())

    rig()                                                       # something else in this version's own files is set
    write_json(AT.settings_path(), V1_SETTINGS)                 # aside, never taken for the old tuner's
    write_json(AT.state_path(), V1_STATE)
    st = state()
    assert st["enabled"] is False and st["upgrade_note"] is False and st["games"] == []
    assert read_json(os.path.join(folder, "adaptive_tuning_v2_settings.damaged.json")) == V1_SETTINGS
    assert read_json(os.path.join(folder, "adaptive_tuning_v2_games.old.json")) == V1_STATE
    assert [h["text"] for h in st["history"]] == [AT.DAMAGED_SETTINGS]


def test_a_file_that_cannot_be_read_is_never_taken_for_a_missing_one():
    """Taken for missing, every game would start again at full overclock and
    the next save would write over the real record."""
    card = rig()
    put_game(level=2)
    before = _REAL_OPEN(AT.state_path(), "rb").read()
    opened = []

    def locked(path, *a, **k):
        if os.path.normcase(str(path)) == os.path.normcase(AT.state_path()) and (not a or "r" in a[0]):
            opened.append(path)
            raise PermissionError(13, "The process cannot access the file")
        return _REAL_OPEN(path, *a, **k)
    AT.open = locked
    try:
        assert AT.get_state() == {"ok": False, "err": AT.ERR_STATE}
        assert len(opened) == 3 and pauses == [0.05, 0.05], "tried briefly, then given up"
        r = start()
        assert r["engaged"] is False and r["reason"] == "error" and card.writes == [] and session_file() is None
        assert AT.step_game(EXE, "full") == {"ok": False, "err": AT.ERR_SAVE}
        assert AT.reset_all() == {"ok": False, "err": AT.ERR_SAVE}
        assert AT.report_line(EXE, clock.now) is None
    finally:
        del AT.open
    assert _REAL_OPEN(AT.state_path(), "rb").read() == before, "and it was not written over"
    assert game()["level"] == 2


def test_a_damaged_file_is_set_aside_and_that_is_recorded():
    rig()
    put_game(level=2)
    with _REAL_OPEN(AT.state_path(), "w", encoding="utf-8") as f:
        f.write('{"version": 2, "games": {"cod.exe": {"lev')
    st = state()
    assert st["games"] == [] and [h["text"] for h in st["history"]] == [AT.DAMAGED_STATE]
    aside = os.path.join(os.path.dirname(AT.state_path()), "adaptive_tuning_v2_games.damaged.json")
    assert _REAL_OPEN(aside, encoding="utf-8").read().startswith('{"version": 2, "games"')
    assert toasts == [("Adaptive Tuning", AT.DAMAGED_STATE, True)]
    assert len(state()["history"]) == 1, "once"

    rig()                                                       # the settings: off until the user says otherwise
    with _REAL_OPEN(AT.settings_path(), "w", encoding="utf-8") as f:
        f.write("[1, 2")
    st = state()
    assert st["enabled"] is False and [h["text"] for h in st["history"]] == [AT.DAMAGED_SETTINGS]
    assert read_json(AT.settings_path())["enabled"] is False

    card = rig()                                                # a session file nothing can be put back from
    with _REAL_OPEN(AT.session_path(), "w", encoding="utf-8") as f:
        f.write("{")
    assert AT.recover_interrupted_session()["recovered"] is False and card.writes == []
    assert not os.path.exists(AT.session_path()) and len(texts()) == 1
    assert start()["engaged"] is True


def test_a_save_that_fails_is_an_error_and_not_a_silent_success():
    rig()
    put_game(level=1)
    AT.atomic_write_json = lambda path, data: False
    for r in (AT.set_settings(notify=False), AT.set_game_enabled(EXE, False), AT.step_game(EXE, "full", confirm=True),
              AT.forget_game(EXE), AT.reset_all(), AT.dismiss_note()):
        assert r == {"ok": False, "err": AT.ERR_SAVE}, r
    AT.atomic_write_json = _REAL_AT["atomic_write_json"]
    assert game()["level"] == 1 and game()["enabled"] is True and state()["notify"] is True


# ═════════════════════════════════════════════════════════════════════
# What the user asks for
# ═════════════════════════════════════════════════════════════════════
def test_settings_are_checked_and_saved():
    rig(enabled=False)
    for bad in ({"enabled": "yes"}, {"notify": 1}, {"sensitivity": "wild"}, {"sensitivity": 3}):
        assert AT.set_settings(**bad) == {"ok": False, "err": AT.ERR_SETTING}, bad
    r = AT.set_settings(notify=False, sensitivity="strict")
    assert r["ok"] and r["state"]["notify"] is False and r["state"]["sensitivity"] == "strict"
    assert r["state"]["enabled"] is False and set(r) == {"ok", "state"}
    assert AT.set_settings()["ok"] is True
    assert AT.get_settings() == {"ok": True, "enabled": False, "notify": False, "sensitivity": "strict",
                                 "upgrade_note": False, "old_switch_was_on": False}
    assert AT.set_game_enabled(EXE, True) == {"ok": False, "err": AT.ERR_NO_GAME}
    assert AT.set_game_enabled(EXE, "on") == {"ok": False, "err": AT.ERR_SETTING}
    assert AT.forget_game(EXE) == {"ok": False, "err": AT.ERR_NO_GAME}
    assert AT.step_game(EXE, "slower") == {"ok": False, "err": AT.ERR_NO_GAME}
    put_game()
    assert AT.step_game(EXE, "sideways") == {"ok": False, "err": AT.ERR_STEP}
    assert AT.step_game(EXE, "faster") == {"ok": False, "err": f"{NAME} already runs at full overclock."}


def test_switching_on_takes_on_the_game_that_is_running():
    card = rig(enabled=False, held=(100, 500))
    m.proc(PID, exe=EXE)
    assert AT.try_engage_running_game() == {"engaged": False, "reason": "no_game"}, "no game is tracked"
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    assert AT.try_engage_running_game()["reason"] == "off"
    r = AT.set_settings(enabled=True)
    assert r["state"]["session"]["exe"] == EXE and card.current == FULL and sess()["pid"] == PID
    assert AT.try_engage_running_game()["adopted"] is True and card.writes == [FULL], "already open: nothing new"
    # a game's own switch
    r = AT.set_game_enabled(EXE, False)
    assert r["state"]["session"] is None and card.current == (100, 500)
    assert r["state"]["game"] == {"exe": EXE, "name": NAME, "looked_after": False, "reason": "switched_off"}
    assert r["state"]["games"][0]["enabled"] is False
    r = AT.set_game_enabled(EXE, True)
    assert r["state"]["session"]["exe"] == EXE and card.current == FULL


def test_a_step_is_recorded_and_applied_at_once_to_the_game_being_played():
    card = rig(profile=(450, 2225, 110), held=(100, 500))
    start()
    poll()
    before = len(mine())
    r = AT.step_game(EXE, "slower")
    assert r["ok"] and r["state"]["games"][0]["level"] == 1 and card.current == L75
    assert mine()[before:] == [("apply_oc", 338, 1669, 110)]
    assert texts() == [P.USER_SET(NAME, 1)] and game()["changes"][-1]["by"] == "user"
    assert sess()["level"] == 1 and sess()["stepped"] is False and toasts == []
    AT.step_game(EXE, "slower")
    AT.step_game(EXE, "slower")
    assert card.current == NONE and mine()[-1] == ("apply_oc", 0, 0, None)
    assert AT.step_game(EXE, "slower") == {"ok": False, "err": f"{NAME} already runs with no overclock."}
    r = AT.step_game(EXE, "faster")
    assert r["ok"] and card.current == L50 and mine()[-1] == ("apply_oc", 225, 1113, 110)
    r = AT.step_game(EXE, "full")
    assert r["ok"] and card.current == FULL and game()["limit_at_level"] is None
    poll()
    assert sess()["held"] is None and sess()["reasserts"] == 0, "its own steps are not somebody else's writes"
    close()
    assert card.current == (100, 500)
    # with no game running it only changes the record
    n = len(card.writes)
    assert AT.step_game(EXE, "slower")["ok"] and card.writes[n:] == [] and game()["level"] == 1


def test_going_back_up_to_a_level_that_failed_lately_needs_a_yes():
    rig()
    put_game(level=1, changes=[{"ts": T0 - 5 * 86400, "from_level": 0, "to_level": 1, "by": "driver",
                                "hours_at_from_level": 30.0, "text": "x"}])
    r = AT.step_game(EXE, "faster")
    assert r == {"ok": False, "need_confirm": True,
                 "text": f"{NAME} had graphics driver errors at full overclock in the last 30 days. Try it again?"}
    assert game()["level"] == 1 and AT.step_game(EXE, "full")["need_confirm"] is True
    assert AT.step_game(EXE, "slower")["ok"] is True, "going down is always allowed"
    assert AT.step_game(EXE, "faster")["ok"] is True, "level 1 did not fail"
    assert AT.step_game(EXE, "faster", confirm=True)["ok"] is True and game()["level"] == 0

    rig()                                                       # 31 days ago: no question
    put_game(level=1, changes=[{"ts": T0 - 31 * 86400, "from_level": 0, "to_level": 1, "by": "driver",
                                "hours_at_from_level": 30.0, "text": "x"}])
    assert AT.step_game(EXE, "faster")["ok"] is True
    rig()                                                       # the user's own step down is not a failure
    put_game(level=1, changes=[{"ts": T0 - 86400, "from_level": 0, "to_level": 1, "by": "user",
                                "hours_at_from_level": 30.0, "text": "x"}])
    assert AT.step_game(EXE, "full")["ok"] is True


def test_forgetting_a_game_and_forgetting_everything():
    rig()
    put_game(level=2)
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=1)
    AT._add_history(EXE, NAME, "something happened")
    r = AT.forget_game(EXE)
    assert [g["exe"] for g in r["state"]["games"]] == ["cs2.exe"]
    assert r["state"]["history"][0]["name"] == NAME, "what happened still names the game"
    r = AT.reset_all()
    assert r["state"]["games"] == [] and r["state"]["history"] == [] and r["state"]["enabled"] is True
    assert start()["level"] == 0, "a forgotten game starts again at full overclock"


def test_the_first_switch_on_says_what_it_now_does_when_the_overclock_is_not_applied_at_start_up():
    rig(enabled=False, apply_on_startup=False, notify=False)
    AT.set_settings(enabled=True)
    assert toasts == [("Adaptive Tuning", AT.FIRST_ON_TOAST, True)], "said once, whatever the notify switch says"
    AT.set_settings(enabled=False)
    AT.set_settings(enabled=True)
    assert len(toasts) == 1

    rig(enabled=False, apply_on_startup=True)                   # nothing new for these users
    AT.set_settings(enabled=True)
    assert toasts == []
    rig(enabled=False, profile=None)                            # no overclock yet: said when there is one to say it about
    AT.set_settings(enabled=True)
    AT.set_settings(enabled=False)
    assert toasts == []
    save_profile(450, 2225, apply_on_startup=False)
    AT.set_settings(enabled=True)
    assert [t[1] for t in toasts] == [AT.FIRST_ON_TOAST]


def test_a_change_is_announced_only_when_the_user_wants_to_be_told():
    for notify in (True, False):
        rig(sensitivity="strict", notify=notify)
        start()
        poll()
        error()
        clock.now += 12
        close()
        assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)]
        assert toasts == ([("Adaptive Tuning", P.T_EASED(NAME, 1), True)] if notify else [])


# ═════════════════════════════════════════════════════════════════════
# What the page is given
# ═════════════════════════════════════════════════════════════════════
def _shape(value):
    """The key sets of a get_state() answer, all the way down."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        shapes = [_shape(v) for v in value if isinstance(v, dict)]
        return shapes[0] if shapes else []
    return None


def _same_keys(mine_, theirs, where):
    if isinstance(theirs, dict) and isinstance(mine_, dict):
        assert set(mine_) == set(theirs), f"{where}: {sorted(set(mine_) ^ set(theirs))}"
        for k in theirs:
            if isinstance(theirs[k], dict) and isinstance(mine_[k], dict):
                _same_keys(mine_[k], theirs[k], f"{where}.{k}")


def test_get_state_has_exactly_the_keys_the_page_was_built_against():
    with _REAL_OPEN(os.path.join(HERE, "fixtures", "at2_states.json"), encoding="utf-8") as f:
        fixtures = json.load(f)
    assert fixtures["load_failed"] == {"ok": False}
    wanted = {name: _shape(s) for name, s in fixtures.items() if name != "load_failed"}

    answers = {}
    rig(enabled=False, profile=None)
    answers["no_overclock"] = state()
    rig(enabled=False)
    CAP.detect = lambda force=False: {"has_nvidia": False, "gpu_name": "AMD Radeon RX 7800 XT"}
    answers["not_supported"] = state()
    rig()
    m.EVENTS.add("boot", T0 - 40 * 86400)
    put_game(exe="cs2.exe", name="Counter-Strike 2")
    put_game(level=1, last_problem={"ts": T0 - 86400, "kind": "ease", "text": P.EASED(NAME, 0, 1)})
    AT._add_history(EXE, NAME, P.EASED(NAME, 0, 1), T0 - 86400)
    answers["on_idle"] = state()
    TRACKED[0] = {"exe": EXE, "display": NAME, "pid": PID}
    start()
    answers["in_game_75"] = state()
    user_applies(100, 500)
    poll()
    answers["held_manual"] = state()
    close()
    AT.set_game_enabled("cs2.exe", False)
    TRACKED[0] = {"exe": "cs2.exe", "display": "Counter-Strike 2", "pid": 5151}
    start(exe="cs2.exe", name="Counter-Strike 2", pid=5151)
    answers["game_switched_off"] = state()

    top = set(wanted["on_idle"])
    for name, shape in wanted.items():
        assert set(shape) == top, f"the fixture {name} has other top-level keys"
    for name, answer in answers.items():
        assert set(answer) == top, (name, sorted(set(answer) ^ top))
        _same_keys(_shape(answer), wanted[name], name)
        json.dumps(answer)
    full = answers["in_game_75"]                                # the parts that are null or empty in some states
    assert set(full["session"]) == set(fixtures["in_game_75"]["session"])
    assert set(full["game"]) == set(fixtures["in_game_75"]["game"])
    assert set(full["games"][0]) == set(fixtures["in_game_75"]["games"][0]) and len(full["games"][0]) == 24
    assert set(full["history"][0]) == set(fixtures["in_game_75"]["history"][0]) == {"ts", "exe", "name", "text"}
    problem = next(g["last_problem"] for g in full["games"] if g["last_problem"])
    assert set(problem) == {"ts", "kind", "text"}
    assert set(full["health"]) == set(fixtures["on_idle"]["health"]) and len(top) == 16
    assert full["monitor_starting"] is False and full["health"]["log_readable"] is True
    assert all(isinstance(g["warning_open"], bool) and isinstance(g["eased_by_driver"], bool) for g in full["games"])
    assert answers["held_manual"]["session"]["held"] == "manual"
    assert answers["game_switched_off"]["game"]["reason"] == "switched_off"
    assert answers["on_idle"]["health"]["available"] is True and answers["on_idle"]["health"]["covered_days"] == 30


def test_get_state_lists_games_and_history_newest_first():
    rig()
    put_game(exe="a.exe", name="A", last_played=T0 - 300)
    put_game(exe="b.exe", name="B", last_played=T0 - 100)
    put_game(exe="c.exe", name="C", last_played=T0 - 200)
    for i in range(230):
        AT._add_history("a.exe", "A", f"line {i}", T0 + i)
    st = state()
    assert [g["exe"] for g in st["games"]] == ["b.exe", "c.exe", "a.exe"]
    assert len(st["history"]) == 50 and st["history"][0]["text"] == "line 229" and st["history"][-1]["text"] == "line 180"
    assert len(read_json(AT.state_path())["history"]) == 200
    assert st["limit"] == {"has_profile": True, "core": 450, "mem": 2225, "power": 100, "apply_on_startup": True}
    assert st["watching"] is False and st["tuner_running"] is False and st["session"] is None and st["game"] is None
    assert st["monitor_starting"] is False
    GP.monitor_starting = lambda: True                          # the app's start-up task has the monitor on its way
    assert state()["monitor_starting"] is True and like_the_fixture(state(), "monitor_starting")
    GP.monitor_starting = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert state()["monitor_starting"] is False, "never a reason for the page to fail"


def test_the_health_line_counts_bursts_and_leaves_out_tests():
    rig()
    day = 86400.0
    m.EVENTS.add("boot", T0 - 60 * day)                         # the System log reaches back 60 days
    for ts in (T0 - 50 * day,                                   # older than 30 days: only in the 90 day count
               T0 - 20 * day, T0 - 20 * day + 100,              # one burst of two
               T0 - 10 * day,                                   # during a test on the GPU page
               T0 - 3 * day):
        m.EVENTS.add("driver_error", ts)
    GO.tuning_windows = lambda grace_s=None: [(T0 - 10 * day - 300, T0 - 10 * day + 300)]
    held = {T0 - 50 * day: (0, 0), T0 - 20 * day: (450, 2225), T0 - 3 * day: (0, 0)}
    # (what the app held at the time of each burst, and for the half hour before it)
    GO.held_at = lambda ts: next((v for at, v in held.items() if at - 1800 <= ts <= at), None)
    h = state()["health"]
    assert h == {"available": True, "errors_30d": 2, "events_30d": 3, "last_error_ts": T0 - 3 * day,
                 "at_stock_90d": 2, "covered_days": 30, "log_readable": True}
    m.EVENTS.add("driver_error", clock.now - 5)
    assert state()["health"]["errors_30d"] == 2, "asked again at most every 30 s"
    clock.now += 31
    AT._health_reset()
    assert state()["health"]["errors_30d"] == 3

    rig()                                                       # what Vispora held is not known for any of them
    m.EVENTS.add("boot", T0 - 60 * day)
    m.EVENTS.add("driver_error", T0 - 3 * day)
    assert state()["health"]["at_stock_90d"] is None
    rig()                                                       # a log that only goes back hours says nothing
    m.EVENTS.add("driver_error", T0 - 3600)
    assert state()["health"] == AT._no_health()
    rig()
    m.EVENTS.add("boot", T0 - 60 * day)
    m.EVENTS.fail = True
    assert state()["health"]["available"] is False and state()["ok"] is True
    assert state()["health"]["log_readable"] is False, "the page says that the log cannot be read"
    m.EVENTS.fail = False


def test_errors_with_no_overclock_keep_a_game_at_half():
    """The driver on the owner's PC logs the same error with no overclock on
    the card about as often as with it: a game is then not taken below 50%."""
    card = rig(sensitivity="strict")
    m.EVENTS.add("boot", T0 - 60 * 86400)
    m.EVENTS.add("driver_error", T0 - 9 * 86400)
    GO.held_at = lambda ts: (0, 0)
    put_game(level=2)
    start()
    poll()
    error()
    for _ in range(7):
        poll()
    assert decided[0][4]["errors_with_no_overclock"] is True
    assert game()["level"] == 2 and texts() == [P.H_FLOOR(NAME)] and card.current == L50


# ═════════════════════════════════════════════════════════════════════
# I2: one place writes the clocks
# ═════════════════════════════════════════════════════════════════════
def test_only_write_touches_the_clocks_and_nothing_touches_the_saved_profile():
    path = os.path.join(m.REPO, "core", "adaptive_tuning.py")
    with _REAL_OPEN(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)
    writers = {"apply_oc", "write_offsets", "reset_oc", "emergency_reset_oc", "set_offsets", "force_reset_all"}
    never = {"save_profile", "delete_profile", "set_profile_apply_on_startup", "OC_PROFILE_PATH", "_nvapi",
             "nvapi_oc", "_reset_to_stock_hard", "_activate_oc_watchdog", "_deactivate_oc_watchdog"}
    found = []

    def walk(node, inside):
        for child in ast.iter_child_nodes(node):
            where = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else inside
            name = child.attr if isinstance(child, ast.Attribute) else child.id if isinstance(child, ast.Name) else None
            if name in writers or name in never:
                found.append((name, where))
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    if alias.name.split(".")[-1] in never | writers | {"nvapi_oc"}:
                        found.append((alias.name, where))
            walk(child, where)
    walk(tree, "<module>")
    assert sorted(found) == [("apply_oc", "_write"), ("apply_oc", "_write"), ("write_offsets", "_write")], found
    assert EM_DASH not in source


def test_no_banned_word_in_anything_a_user_can_read():
    said = [AT.FIRST_ON_TOAST, AT.NO_CLOCKS, AT.NO_NVIDIA.format(name=AT.UNKNOWN_CARD), AT.DAMAGED_STATE,
            AT.DAMAGED_SETTINGS, AT.ERR_NO_GAME, AT.ERR_STEP, AT.ERR_SETTING, AT.ERR_SAVE, AT.ERR_STATE,
            AT.NOT_PUT_BACK("Game"), AT.CONFIRM_STEP_UP("Game", 0), AT.CONFIRM_STEP_UP("Game", 2), HA._AT_DETAIL,
            AT.HOT_AT_START("Game"), AT.OWN_AFTER_HOT("Game"), AT.STOPPED_KEPT("Game"), AT.CHANGED_MEANWHILE("Game"),
            AT.RUNS_BESIDE("Game", "Other Game", 1), AT.RUNS_BESIDE("Game", "Other Game", 3)]
    for step_down, stepped in (({"core": 425, "mem": 2125}, True), ({"core": 425, "mem": 2125}, False), (None, False)):
        for core, mem in ((450, 2225), (0, 0)):
            said.extend(CR.crash_toast("Game", core, mem, step_down, stepped))
    for state_ in ({"ok": True, "supported": {"ok": True}, "limit": {"has_profile": False}},
                   {"ok": True, "supported": {"ok": True}, "limit": {"has_profile": True, "core": 450, "mem": 0},
                    "enabled": False, "health": {"errors_30d": 3}},
                   {"ok": True, "supported": {"ok": True}, "limit": {"has_profile": True, "core": 450, "mem": 0},
                    "enabled": True, "games": [{"level": 1}, {"level": 0}, {"level": 3}]}):
        row = HA._adaptive_tuning_row(state_)
        said.extend([row["name"], row["current"], row["detail"]])
    for text in said:
        assert isinstance(text, str) and text.strip(), text
        assert not BANNED.search(text) and EM_DASH not in text, text


# ═════════════════════════════════════════════════════════════════════
# The modules around it
# ═════════════════════════════════════════════════════════════════════
def test_the_dashboard_row_is_built_from_get_state():
    rig(enabled=False)
    row = HA._adaptive_tuning_row()
    assert row["id"] == "vispora.adaptive" and row["severity"] == "ok" and row["current"] == "Off"
    assert row["fix"] == {"kind": "navigate", "page": "profiles"} and row["expected"] is None
    m.EVENTS.add("boot", T0 - 60 * 86400)
    m.EVENTS.add("driver_error", T0 - 86400)
    AT._health_reset()
    row = HA._adaptive_tuning_row()
    assert row["severity"] == "warn" and row["current"] == (
        "Off. The graphics driver logged errors 1 time in the last 30 days.")
    m.EVENTS.add("driver_error", T0 - 2 * 86400)
    AT._health_reset()
    assert HA._adaptive_tuning_row()["current"] == "Off. The graphics driver logged errors 2 times in the last 30 days."
    AT.set_settings(enabled=True)
    assert HA._adaptive_tuning_row()["current"] == "On" and HA._adaptive_tuning_row()["severity"] == "ok"
    by_driver = [{"ts": T0 - 86400, "from_level": 0, "to_level": 1, "by": "driver", "hours_at_from_level": 30.0,
                  "text": "x"}]
    put_game(level=1, changes=by_driver)
    assert HA._adaptive_tuning_row()["current"] == "On, 1 game eased"
    put_game(exe="cs2.exe", name="Counter-Strike 2", level=3, changes=[dict(by_driver[0], from_level=2, to_level=3)])
    put_game(exe="a.exe", name="A")
    put_game(exe="b.exe", name="B", level=1)                    # set lower by the user: not "eased"
    assert HA._adaptive_tuning_row()["current"] == "On, 2 games eased"
    assert [c for c in HA._check_vispora_state() if c["id"] == "vispora.adaptive"] == [HA._adaptive_tuning_row()]

    rig(profile=None)
    row = HA._adaptive_tuning_row()
    assert row["current"] == "No overclock saved yet" and row["severity"] == "ok"
    rig()
    CAP.detect = lambda force=False: {"has_nvidia": False, "gpu_name": "Intel Arc A770"}
    assert HA._adaptive_tuning_row() is None
    assert [c for c in HA._check_vispora_state() if c["id"] == "vispora.adaptive"] == []
    assert HA._adaptive_tuning_row({"ok": False, "err": "x"}) is None


def test_the_after_game_report_carries_the_overclock_line_once_the_exit_is_judged():
    """The report is written when game mode ends, before Adaptive Tuning has
    judged how the game closed: a line written then always said "no problems"
    for the very session that was eased."""
    rig(sensitivity="strict")
    report = {"game": NAME, "exe": EXE, "ended": round(time.time()), "minutes": 42.0, "p_busy": 30.0,
              "e_busy": 5.0, "game_share": 20.0, "other_share": 3.0, "eco_on": False, "eco_programs": 0,
              "eco_processes": 0}
    os.makedirs(os.path.dirname(SR._FILE), exist_ok=True)
    write_json(SR._FILE, [report])
    assert not any("verclock" in ln for ln in SR.latest()["lines"])
    clock.now = float(report["ended"]) - 60
    start()
    poll()
    error()
    clock.now = float(report["ended"]) - 2
    close()
    lines = SR.latest()["lines"]
    assert lines[-1] == "Overclock eased to 75% of your overclock after graphics driver errors", lines
    report["exe"] = "other.exe"
    write_json(SR._FILE, [report])
    assert not any("verclock" in ln for ln in SR.latest()["lines"])


def test_crash_recovery_uses_the_same_two_minutes_and_knows_nothing_of_the_old_tuner():
    assert CR.CRASH_LOOKBACK_S == 120
    with _REAL_OPEN(os.path.join(m.REPO, "core", "crash_recovery.py"), encoding="utf-8") as f:
        src = f.read()
    assert "adaptive_tuning" not in src.replace("Adaptive Tuning", "") and "cross_game" not in src
    rig()
    CR.add_to_blacklist(EXE, 500, 2400, "tdr")                  # (used to call into the old tuner)
    assert CR.get_blacklist(EXE)[0]["core"] == 500
    asked = []
    GO.check_driver_events = lambda seconds_back=300: asked.append(seconds_back) or {"ok": True, "tdr_count": 0,
                                                                                    "crash_count": 0, "events": []}
    try:
        CR.detect_crash(exit_ts=time.time() - 12)
    finally:
        GO.check_driver_events = _REAL_GO["check_driver_events"]
    assert 135 <= asked[0] <= 140, "120 s before an exit that was reported 12 s late, plus the monitor's slack"
    assert callable(GO.check_driver_events) and GO.check_driver_events(5)["ok"] is True, "still there for the next test"


def test_the_modules_that_went_with_the_old_tuner():
    assert not os.path.exists(os.path.join(m.REPO, "core", "gameplay_state.py"))
    for name in ("adaptive_tuning.py", "game_profiles.py", "tournament_mode.py", "health_audit.py"):
        with _REAL_OPEN(os.path.join(m.REPO, "core", name), encoding="utf-8") as f:
            src = f.read()
        assert "gameplay_state" not in src and "perf_monitor" not in src, name
    for old in ("get_game_state", "set_game_mode", "set_game_target", "pause", "resume", "apply_best_stable",
                "add_cross_game_blacklist", "_tuning_loop", "MODE_PRESETS", "AUTO_TARGET"):
        assert not hasattr(AT, old), old
    for kept in ("on_game_start", "on_game_exit", "end_active_session", "recover_interrupted_session",
                 "clear_runtime_if_orphaned", "session_active", "try_engage_running_game", "note_manual_change",
                 "report_line", "get_state", "set_settings", "set_game_enabled", "step_game", "forget_game",
                 "reset_all", "dismiss_note", "supported"):
        assert callable(getattr(AT, kept)), kept
    for name in ("adaptive_tuning_settings.json", "adaptive_tuning_state.json", "adaptive_tuning_session.json",
                 "adaptive_tuning_state.v1.json", "gpu_oc_timeline.json", "gpu_tuning_windows.json",
                 "adaptive_tuning_v2_settings.json", "adaptive_tuning_v2_games.json",
                 "adaptive_tuning_v2_session.json"):
        assert name in IS._NEVER_TOUCH_NAMES, name
    assert {os.path.basename(p) for p in (AT.settings_path(), AT.state_path(), AT.session_path(),
                                          AT.v1_settings_path(), AT.v1_state_path(), AT.v1_session_path(),
                                          AT.state_v1_path(), GO.OC_TIMELINE_PATH, GO.TUNING_WINDOWS_PATH)
            } <= IS._NEVER_TOUCH_NAMES
    # this version's files are its own: the old tuner's keep their names and are never written here
    assert len({AT.settings_path(), AT.state_path(), AT.session_path(), AT.v1_settings_path(), AT.v1_state_path(),
                AT.v1_session_path()}) == 6


def test_tournament_mode_has_no_tuner_step_and_puts_up_with_an_old_saved_state():
    rig()
    with _REAL_OPEN(os.path.join(m.REPO, "core", "tournament_mode.py"), encoding="utf-8") as f:
        src = f.read()
    assert "import adaptive_tuning" not in src and "adaptive_tuning." not in src
    assert "set_game_mode" not in src and "get_game_state" not in src
    write_json(TM.STATE_PATH, {"enabled": True, "started_at": time.time(), "expires_at": 0, "duration_min": 0,
                               "baseline": {"at_mode_changes": [{"exe": EXE, "prev_mode": "balanced"}],
                                            "pauser_was_enabled": True},
                               "last_actions": []})
    r = TM.disable("test")
    assert r["ok"] is True and not any("AT" in a.split() for a in r.get("actions", [])), r
    assert TM.get_status()["enabled"] is False


def test_an_orphaned_session_is_closed_and_a_live_one_is_not():
    card = rig(held=(100, 500))
    assert AT.clear_runtime_if_orphaned() == {"orphan": False}
    start()
    assert AT.clear_runtime_if_orphaned() == {"orphan": False, "active_exe": EXE}
    AT._exe_running = lambda exe: None                          # the process list cannot be taken: not "gone"
    assert AT.clear_runtime_if_orphaned() == {"orphan": False, "active_exe": EXE} and sess() is not None
    AT._exe_running = _REAL_AT["_exe_running"]
    m.exit_proc(PID)
    assert AT.clear_runtime_if_orphaned() == {"orphan": True, "cleared_exe": EXE}
    assert AT._sess is None and card.current == (100, 500) and game()["sessions"] == 1


# ═════════════════════════════════════════════════════════════════════
# Whose error it is, and whether it was counted before
# ═════════════════════════════════════════════════════════════════════
def new_process():
    """The app is started again: what the last run only kept in memory is gone."""
    AT._burst_tail.clear()
    AT._failed_reads[0] = 0
    AT._health_cache.update(at=None, value=None)
    AT._last_game = AT._last_closed = None


def crash_loop(gap_s, crashes, crash_record=False, **rig_kw):
    """A game that closes on a graphics driver error `crashes` times, the
    errors `gap_s` apart; it is started again four seconds after each exit."""
    assert (gap_s - 10) % 20 == 0
    card = rig(held=(450, 2225), **rig_kw)
    for n in range(crashes):
        pid = PID + n
        assert start(pid=pid)["engaged"] is True
        for _ in range(1 if n == 0 else (gap_s - 10) // 20):
            poll()
        error(ago=0)
        if crash_record:
            m.EVENTS.add("app_crash", clock.now, exe=EXE, pid=pid)
        clock.now += 6
        close(pid=pid)
        clock.now += 4
    return card


def test_a_game_that_keeps_closing_on_driver_errors_minutes_apart_is_eased_at_the_second_one():
    """Errors less than five minutes after one that was judged went on from
    it for ever: a game that crashed every three minutes was counted once,
    never eased, and each later crash was written up as "No graphics driver
    error was logged"."""
    for gap in (150, 210, 290, 310):                            # (310 s was eased before, too: the control)
        card = crash_loop(gap, 2)
        assert texts() == [P.W_ENDED(NAME), P.EASED(NAME, 0, 1)], (gap, texts())
        assert game()["level"] == 1 and len(decided) == 2 and decided[1][4]["ended_game"] is True
        assert last_line() == "Overclock eased to 75% of your overclock after graphics driver errors"
        assert card.current == FULL, "handed back as found"
        assert start(pid=5151)["level"] == 1 and card.current == L75, "and the next launch runs on less"

    crash_loop(210, 6, crash_record=True)                       # Windows logs a crash record of the game each time
    assert texts()[:2] == [P.W_ENDED(NAME), P.EASED(NAME, 0, 1)] and game()["level"] >= 1
    assert P.PLAIN_CRASH(NAME) not in texts() and game()["plain_crashes"] == [], texts()
    assert state()["games"][0]["plain_crash_hint"] is False

    crash_loop(210, 4, crash_record=True, sensitivity="strict")
    assert texts() == [P.EASED_STRICT(NAME, 0, 1), P.EASED_STRICT(NAME, 1, 2)], texts()
    assert [d[3] for d in decided] == ["ease", "ignore", "ignore", "ease"], "ten minutes after a step count nothing"
    assert game()["plain_crashes"] == []


def test_errors_that_go_on_for_more_than_ten_minutes_are_not_one_incident_for_ever():
    rig()
    start()
    poll()
    error()                                                     # T0+19
    for _ in range(8):
        poll()                                                  # T0+180: judged, it kept running
    assert texts() == [P.W_SURVIVED(NAME)]
    for _ in range(4):
        poll()
    error()                                                     # T0+259: four minutes after the first
    for _ in range(12):
        poll()
    error()                                                     # T0+499: eight minutes after the first
    for _ in range(12):
        poll()
    assert len(decided) == 1 and sess()["pending"] == [] and sess()["absorbed"]["count"] == 2
    assert read_json(AT.state_path())["tails"][EXE] == {"first": T0 + 19, "last": T0 + 499, "judged": T0 + 19}
    error()                                                     # T0+739: twelve minutes after the first
    poll()
    assert len(sess()["pending"]) == 1, "no longer the incident that was judged"
    for _ in range(7):
        poll()
    assert texts() == [P.W_SURVIVED(NAME), P.W_SAME_SPELL(NAME)] and game()["level"] == 0
    assert len(game()["incidents"]) == 2 and sess()["absorbed"] is None


def test_a_reset_storm_of_many_records_is_taken_in_one_go():
    rig()
    start()
    poll()
    error()
    for _ in range(8):
        poll()                                                  # judged: it kept running
    saves = []
    real = AT.atomic_write_json
    AT.atomic_write_json = lambda path, data: saves.append(path) or real(path, data)
    for n in range(60):
        m.EVENTS.add("driver_error", clock.now + n * 0.25)      # sixty more records in fifteen seconds
    poll()
    AT.atomic_write_json = real
    assert sess()["absorbed"]["count"] == 60 and sess()["pending"] == [] and len(decided) == 1
    assert saves.count(AT.state_path()) == 1, "the record is saved once for all of them, not sixty times"
    assert read_json(AT.state_path())["tails"][EXE]["last"] == clock.now - 20 + 59 * 0.25


def test_a_game_that_closes_on_errors_going_on_from_a_judged_incident_is_not_a_crash_with_no_driver_error():
    rig()
    start()
    poll()
    error()
    for _ in range(9):
        poll()                                                  # T0+200: judged, it kept running
    error()                                                     # three minutes after the first
    m.EVENTS.add("app_crash", clock.now, exe=EXE, pid=PID)
    clock.now += 5
    close()
    g = game()
    assert texts() == [P.W_SURVIVED(NAME)] and g["plain_crashes"] == [] and len(decided) == 1, texts()
    assert g["last_problem"]["kind"] == "warning"
    assert last_line() == "Overclock: full, a graphics driver error was noted"


def test_a_relaunch_found_before_the_exit_was_reported_counts_the_next_crash_too():
    """The game closes on a driver error and is back within game mode's grace
    each time: the session carries on, and game mode reports each exit after
    the relaunch.  The second crash is a second incident, not what went on
    from the first."""
    card = rig(held=(450, 2225))
    start()
    first = sess()
    pid = PID
    for n, new_pid in enumerate((5151, 6161)):
        for _ in range(1 if n == 0 else 10):
            poll()
        error()
        exit_ts = clock.now + 5                                 # it closes six seconds after the error ...
        clock.now += 9
        m.exit_proc(pid)
        m.proc(new_pid, exe=EXE)
        assert AT.on_game_start(EXE, NAME, new_pid)["adopted"] is True      # ... game mode finds it again ...
        assert AT.on_game_exit(EXE, exit_ts=exit_ts) == {"handled": True, "kept_open": True}   # ... and says so
        pid = new_pid
        if n == 0:
            assert texts() == [P.W_ENDED(NAME)] and card.current == FULL
    assert texts() == [P.W_ENDED(NAME), P.EASED(NAME, 0, 1)] and game()["level"] == 1
    assert sess() is first and first["level"] == 1 and card.current == L75, "lowered under the game that runs now"
    assert len(first["launches"]) == 3 and decided[1][4]["ended_game"] is True
    # found again under the same process (game mode was put back): not another run of the game
    assert AT.on_game_start(EXE, NAME, 6161)["adopted"] is True and len(first["launches"]) == 3


def test_what_was_judged_of_an_incident_is_remembered_across_a_restart_of_the_app():
    """It was only kept in memory: after a restart the same burst was counted
    a second time, and one burst eased the game ("twice within 10 hours")."""
    rig()                                                       # (a) the game closed on it; the app is restarted
    start()
    poll()
    error(ago=0)                                                # T0+20
    clock.now += 6
    close()
    assert texts() == [P.W_ENDED(NAME)]
    assert read_json(AT.state_path())["tails"] == {EXE: {"first": T0 + 20, "last": T0 + 20, "judged": T0 + 20}}
    new_process()
    clock.now += 4
    start(pid=5151)
    for _ in range(4):
        poll()
    clock.now += 10
    error(ago=0)                                                # T0+120: 100 s after the first, and the game runs on
    for _ in range(8):
        poll()
    assert texts() == [P.W_ENDED(NAME)] and len(decided) == 1 and game()["level"] == 0

    rig(held=(100, 500))                                        # (b) judged while it ran; then the app dies
    start()
    poll()
    error()                                                     # T0+19
    for _ in range(7):
        poll()                                                  # T0+160: judged at T0+140
    assert texts() == [P.W_SURVIVED(NAME)]
    die()
    m.EVENTS.add("driver_error", T0 + 193)                      # goes on from it, around the last heartbeat
    m.exit_proc(PID)
    clock.now = T0 + 3600
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and r.get("judged") is None and len(decided) == 1 and len(game()["incidents"]) == 1
    assert texts() == [P.W_SURVIVED(NAME), P.HANDED_BACK(NAME)]

    rig(held=(450, 2225))                                       # (c) the app dies, the game runs on
    start()
    poll()
    error()
    for _ in range(7):
        poll()
    die()
    clock.now += 10
    assert AT.recover_interrupted_session()["kept"] is True
    assert AT.on_game_start(EXE, NAME, PID)["adopted"] is True
    clock.now += 20
    error(ago=0)                                                # T0+190: 171 s after the first
    for _ in range(8):
        poll()
    assert texts() == [P.W_SURVIVED(NAME)] and len(decided) == 1 and sess()["absorbed"]["later_launch"] is False

    rig()                                                       # five minutes after it, it is a new incident as ever
    start()
    poll()
    error(ago=0)
    clock.now += 6
    close()
    new_process()
    clock.now += 4
    start(pid=5151)
    for _ in range(15):
        poll()
    error(ago=0)                                                # T0+330
    for _ in range(7):
        poll()
    assert [d[3] for d in decided] == ["warning", "ease"] and game()["level"] == 1

    assert AT.forget_game(EXE)["ok"] is True                    # forgetting a game forgets this too
    assert read_json(AT.state_path())["tails"] == {} and EXE not in AT._burst_tail
    put_game()
    with AT._state_lock:                                        # a damaged entry is dropped, not a reason to fail
        st = AT._load_state()
        st["tails"] = {EXE: {"first": "x", "last": 5}, "b.exe": {"first": 9.0, "last": 3.0}, "c.exe": 7}
        AT._save_state(st)
        assert AT._load_state()["tails"] == {}


def test_another_game_is_one_being_played_not_any_program_the_game_list_knows():
    """It was any process on the game list or with a record: a Java program,
    a game's client left open in the tray or a controller tool in the
    background, and no driver error was ever counted."""
    def other():
        return AT._other_game_running(EXE, NAME)

    rig()
    m.proc(PID, exe=EXE)
    assert other() is False, "nothing else is running"
    for pid, exe in ((7001, "javaw.exe"), (7002, "leagueclient.exe"), (7003, "vrisingserver.exe")):
        m.proc(pid, exe=exe)
        assert GP._game_match(exe) == "known"
    windows((PID, EXE))
    assert other() is False, "a process that merely exists is not a game being played"
    windows((PID, EXE), (7001, "javaw.exe"))
    assert other() is True, "Minecraft in a window is"
    for pid, exe in ((7004, "fortnitelauncher.exe"), (7005, "minecraftlauncher.exe"), (7006, "launcher.exe")):
        m.proc(pid, exe=exe)
        windows((PID, EXE), (pid, exe))
        assert GP._game_match(exe) == "known" and other() is False, f"{exe} is a launcher, whatever the list says"

    m.proc(7007, exe="gamepad tester.exe")                      # a game only by the word "game" in its name
    windows((PID, EXE), (7007, "Gamepad Tester.exe"))
    assert GP._game_match("gamepad tester.exe") == "hint" and other() is False
    put_game(exe="gamepad tester.exe", name="Gamepad Tester", minutes=29.0)
    assert other() is False, "a record alone does not make it a game"
    put_game(exe="gamepad tester.exe", name="Gamepad Tester", minutes=30.0)
    assert other() is True, "half an hour of play on record does"
    m.proc(7008, exe="gamesir connect.exe")                     # game mode no longer takes this one for a game at all
    windows((PID, EXE), (7008, "GameSir Connect.exe"))
    put_game(exe="gamesir connect.exe", name="Gamesir Connect", minutes=300.0)
    assert GP._is_game("gamesir connect.exe") is False and other() is False, "whatever it has on record"

    m.proc(7010, exe="cs2.exe", ppid=PID)                       # started by the game
    windows((PID, EXE), (7010, "cs2.exe"))
    assert other() is False
    m.proc(7011, exe="crashpad.exe", ppid=PID)
    m.proc(7012, exe="eldenring.exe", ppid=7011)                # ... two steps down
    windows((PID, EXE), (7012, "eldenring.exe"))
    assert other() is False
    m.proc(7020, exe="modernwarfare.exe")                       # another program of the same game
    windows((PID, EXE), (7020, "modernwarfare.exe"))
    assert other() is False
    m.proc(7021, exe="r5apex.exe")
    windows((PID, EXE), (7021, "r5apex.exe"))
    assert other() is True, "a real second game still is"
    for failed in (m.F("timed out"), m.R("not json")):
        m.PS_HOOKS[:] = [lambda command, failed=failed: failed if "MainWindowHandle" in command else None]
        assert other() is False, "when the look fails nothing can be said"

    rig()                                                       # the game's own launcher, two steps above it
    m.proc(8001, exe="r5apex.exe")
    m.proc(8002, exe="bootstrap.exe", ppid=8001)
    m.proc(PID, exe=EXE, ppid=8002)
    windows((PID, EXE), (8001, "r5apex.exe"))
    assert other() is False

    card = rig(sensitivity="strict")                            # and so the error is the game's
    start()
    m.proc(7001, exe="javaw.exe")
    m.proc(7002, exe="leagueclient.exe")
    windows((PID, EXE))
    poll()
    error()
    for _ in range(7):
        poll()
    assert texts() == [P.EASED_STRICT(NAME, 0, 1)] and decided[0][4]["other_game_running"] is False
    assert card.current == L75


def test_a_game_that_closed_on_a_driver_error_as_the_tracker_moved_on_is_judged_as_closed():
    """Game mode now reports the old game's exit when its tracker moves to
    another window, then starts the new one.  Whether that other program
    counts as "another game" does not depend on which of the two comes first:
    a record made a moment ago for a controller tool does not make it one."""
    for other_exe, other_name, expect in (("gamepad tester.exe", "Gamepad Tester", P.W_ENDED(NAME)),
                                          ("cs2.exe", "Counter-Strike 2", P.W_OTHER(NAME))):
        for judged_first in (False, True):
            card = rig(held=(100, 500))
            start()
            poll()
            m.proc(7001, exe=other_exe)
            windows((PID, EXE), (7001, other_exe))              # its window is opened during play
            error(ago=0)
            clock.now += 6
            m.exit_proc(PID)
            windows((7001, other_exe))
            if not judged_first:
                AT._defer = deferred.append                     # (as in the app: the exit is judged on another thread)
            assert AT.on_game_exit(EXE, exit_ts=clock.now)["handled"] is True
            assert card.current == (100, 500), "handed back before the next game is taken on"
            assert AT.on_game_start(other_exe, other_name, 7001)["engaged"] is True
            run_deferred()
            assert texts() == [expect], (other_exe, judged_first, texts())
            assert len(game()["incidents"]) == (1 if expect == P.W_ENDED(NAME) else 0)
            assert sess()["exe"] == other_exe and sess()["pre"] == [100, 500, 100]


def test_the_report_line_says_so_when_something_was_noted_and_nothing_was_changed():
    """It said "Overclock: full, no problems" for a session that closed on a
    graphics driver error."""
    def play(error_at_poll=None, crash=False, polls=9):
        start()
        for n in range(polls):
            poll()
            if n == error_at_poll:
                error()
        if crash:
            m.EVENTS.add("app_crash", clock.now, exe=EXE, pid=PID)
        clock.now += 5
        close()
        return last_line()

    rig()
    assert play() == "Overclock: full, no problems"
    rig()
    assert play(error_at_poll=0) == "Overclock: full, a graphics driver error was noted"     # it kept running
    assert texts() == [P.W_SURVIVED(NAME)]
    rig()
    assert play(error_at_poll=8) == "Overclock: full, a graphics driver error was noted"     # it closed on it
    assert texts() == [P.W_ENDED(NAME)]
    rig(sensitivity="watch")
    put_game(incidents=[{"ts": T0 - 3600, "play_minutes": 0.0, "level": 0, "ended_game": True}])
    assert play(error_at_poll=0) == "Overclock: full, a graphics driver error was noted"
    assert texts() == [P.WATCH(NAME, 1)]
    rig(sensitivity="strict")
    m.proc(5151, exe="cs2.exe")
    windows((PID, EXE), (5151, "cs2.exe"))
    assert play(error_at_poll=0) == "Overclock: full, a graphics driver error was noted"
    assert texts() == [P.W_OTHER(NAME)]
    rig()
    assert play(crash=True) == "Overclock: full, the game closed unexpectedly"
    assert texts() == [P.PLAIN_CRASH(NAME)]
    rig(sensitivity="strict")                                   # eased yesterday, an error again: left where it is
    put_game(level=1, limit_at_level=[450, 2225], minutes=3000.0, minutes_at_level=10.0,
             changes=[{"ts": T0 - 86400, "from_level": 0, "to_level": 1, "by": "driver", "hours_at_from_level": 30.0,
                       "text": "x"}])
    assert play(error_at_poll=0) == "Overclock: 75%, a graphics driver error was noted"
    assert texts() == [P.H_NOT_HELPING(NAME)]
    rig(sensitivity="strict")
    put_game(level=3)
    assert play(error_at_poll=0) == "No overclock for this game"
    rig(sensitivity="strict")                                   # the eased line stays what it was
    assert play(error_at_poll=0) == "Overclock eased to 75% of your overclock after graphics driver errors"


def test_an_error_that_was_waiting_when_the_user_changed_the_level_says_nothing_about_the_new_level():
    """It was judged against the level the user had just set: "easing does
    not seem to help" a minute after the user's own step down, or a step up
    undone by an error from before it."""
    fifty_hours = {"minutes": 3000.0, "minutes_at_level": 3000.0}
    card = rig(sensitivity="strict")
    put_game(**fifty_hours)
    start()
    poll()
    error()
    poll()                                                      # T0+40: seen, waiting
    clock.now += 20
    assert AT.step_game(EXE, "slower")["ok"] is True and sess()["pending"], "41 s after the error"
    for _ in range(6):
        poll()
    g = game()
    assert len(decided) == 1 and decided[0][3] == "ignore" and decided[0][4]["since_last_change_s"] == -41.0
    assert g["level"] == 1 and g["incidents"] == [] and g["not_helping"] is False and card.current == L75
    assert texts() == [P.USER_SET(NAME, 1)] and toasts == [] and sess()["pending"] == []
    error()                                                     # what goes on from it is not counted either
    for _ in range(7):
        poll()
    assert len(decided) == 1 and texts() == [P.USER_SET(NAME, 1)]

    card = rig()                                                # a step up is not undone by an error from before it
    put_game(level=1, limit_at_level=[450, 2225], minutes=1500.0, minutes_at_level=300.0,
             changes=[{"ts": T0 - 40 * 86400, "from_level": 0, "to_level": 1, "by": "driver",
                       "hours_at_from_level": 20.0, "text": "x"}])
    start()
    poll()
    error()
    poll()
    clock.now += 20
    assert AT.step_game(EXE, "full")["ok"] is True and card.current == FULL
    for _ in range(6):
        poll()
    g = game()
    assert decided[-1][3] == "ignore" and g["level"] == 0 and card.current == FULL and toasts == []
    assert [c["by"] for c in g["changes"]] == ["driver", "user"] and P.failures_at(g, 0) == 1

    rig(sensitivity="strict")                                   # the same when the game closes before it is judged
    put_game(**fifty_hours)
    start()
    poll()
    error()
    poll()
    clock.now += 10
    AT.step_game(EXE, "slower")
    clock.now += 30
    close()
    assert decided[-1][3] == "ignore" and game()["not_helping"] is False and game()["incidents"] == []

    rig(sensitivity="strict")                                   # an error that begins after the step is the new level's
    put_game(**fifty_hours)
    start()
    poll()
    AT.step_game(EXE, "slower")
    poll()
    error()
    for _ in range(7):
        poll()
    assert decided[-1][3] == "hold" and decided[-1][4]["since_last_change_s"] is None
    assert texts()[-1] == P.H_NOT_HELPING(NAME)

    card = rig(sensitivity="strict")                            # the ten minutes after the driver's own step still hold
    start()                                                     # when the user steps back up in between
    poll()
    error()
    for _ in range(7):
        poll()                                                  # eased at T0+140
    assert game()["level"] == 1
    assert AT.step_game(EXE, "full", confirm=True)["ok"] is True
    for _ in range(18):
        poll()
    error()                                                     # T0+519: six minutes after the ease
    for _ in range(7):
        poll()
    assert decided[-1][3] == "ignore" and game()["level"] == 0


def test_what_the_driver_logs_as_the_pc_wakes_from_sleep_is_not_the_games():
    """The gap was "a PC that slept, not play", and its errors were counted
    all the same: two nights with the game left open eased it."""
    rig(sensitivity="strict")
    start()
    for _ in range(3):
        poll()
    clock.now += 8 * 3600                                       # the PC sleeps with the game open ...
    error()                                                     # ... and the driver logs an error as it wakes
    poll()
    woke = clock.now
    assert sess()["count_from"] == woke + AT.SLEEP_QUIET_S and sess()["play_s"] == 120
    for _ in range(3):
        poll()
    error()                                                     # and one more a minute later
    for _ in range(8):
        poll()
    assert decided == [] and texts() == [] and game()["level"] == 0 and sess()["pending"] == []
    for _ in range(5):
        poll()
    assert clock.now > woke + AT.SLEEP_QUIET_S
    error()                                                     # five minutes on, an error is the game's again
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)]

    rig()                                                       # an error that was waiting when the PC went to sleep
    start()
    poll()
    error()
    poll()
    assert sess()["pending"]
    clock.now += 3600
    poll()
    assert texts() == [P.W_SURVIVED(NAME)] and len(game()["incidents"]) == 1


def test_the_errors_of_a_test_on_the_gpu_page_are_never_the_games():
    """A test crashed the driver and ended; the game was taken on a second
    later and the test's last errors were counted for it (strict: eased, with
    a toast), while the health line left the same errors out."""
    card = rig(sensitivity="strict")
    m.EVENTS.add("boot", T0 - 60 * 86400)
    ended = T0 - 20
    GO.tuning_windows = lambda grace_s=None: [(ended - 200, ended + (120.0 if grace_s is None else grace_s))]
    start()
    poll()
    clock.now += 10
    error()                                                     # 49 s after the test: inside its two minutes
    for _ in range(8):
        poll()
    assert decided == [] and texts() == [] and game()["level"] == 0 and card.current == FULL and toasts == []
    assert sess()["pending"] == [] and state()["health"]["errors_30d"] == 0
    error()                                                     # minutes later: the game's
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)]

    interrupted()                                               # also for a run that died
    m.EVENTS.add("driver_error", T0 + 70)
    GO.tuning_windows = lambda grace_s=None: [(T0 + 65, T0 + 75 + (120.0 if grace_s is None else grace_s))]
    clock.now = T0 + 3600
    assert AT.recover_interrupted_session().get("judged") is None and decided == [] and game()["level"] == 0


def test_errors_just_after_the_overclock_came_off_are_not_errors_with_no_overclock():
    """A burst eight minutes after the game closed (and the card was handed
    back to none) counted as "with no overclock on the card": the page said
    the overclock may not be the cause, and every game stayed at 50%."""
    rig(apply_on_startup=False)                                 # the overclock is only on in games
    m.EVENTS.add("boot", T0 - 60 * 86400)
    start()
    for _ in range(30):
        poll()
    error(ago=0)
    clock.now += 6
    close()                                                     # the game closes on it; the card goes back to none
    off = clock.now
    assert GO.held_at(off + 1) == (0, 0) and GO.held_at(off - 60) == FULL
    clock.now += 8 * 60
    error(ago=0)                                                # the episode goes on eight minutes later
    clock.now += 60
    AT._health_reset()
    h = state()["health"]
    assert h["errors_30d"] == 2 and h["at_stock_90d"] == 0, h
    clock.now = off + 29 * 60
    error(ago=0)                                                # 29 minutes after it came off: still not
    clock.now += 60
    AT._health_reset()
    assert state()["health"]["at_stock_90d"] == 0
    clock.now = off + 70 * 60
    error(ago=0)                                                # no overclock for more than half an hour: now it is
    clock.now += 60
    AT._health_reset()
    h = state()["health"]
    assert h["errors_30d"] == 4 and h["at_stock_90d"] == 1, h

    rig()                                                       # ... nor the half hour after a test on the GPU page
    m.EVENTS.add("boot", T0 - 60 * 86400)
    GO.held_at = lambda ts: (0, 0)
    GO.tuning_windows = lambda grace_s=None: [(T0 - 3600, T0 - 3000 + (120.0 if grace_s is None else grace_s))]
    m.EVENTS.add("driver_error", T0 - 2400)                     # ten minutes after the test ended
    h = state()["health"]
    assert h["errors_30d"] == 1 and h["at_stock_90d"] == 0, h
    m.EVENTS.add("driver_error", T0 - 600)                      # forty minutes after it
    AT._health_reset()
    assert state()["health"]["at_stock_90d"] == 1
    rig()                                                       # a game ran on the overclock for a few minutes
    m.EVENTS.add("boot", T0 - 60 * 86400)                       # inside that half hour
    burst = T0 - 3 * 86400
    m.EVENTS.add("driver_error", burst)
    GO.held_at = lambda ts: FULL if burst - 900 <= ts <= burst - 400 else (0, 0)
    assert state()["health"]["at_stock_90d"] == 0
    rig()                                                       # a record that does not reach back that far
    m.EVENTS.add("boot", T0 - 60 * 86400)
    m.EVENTS.add("driver_error", T0 - 3 * 86400)
    GO.held_at = lambda ts: (0, 0) if ts >= T0 - 3 * 86400 - 600 else None
    assert state()["health"]["at_stock_90d"] is None


def test_errors_from_before_the_app_kept_a_record_of_its_tests_are_not_in_the_health_line():
    """3.7.0 kept no record of when a test ran on the GPU page.  For a month
    after the update the driver crashes its tests had caused were counted as
    the user's own, on the card and on the dashboard."""
    rig(enabled=False)
    GO.tuning_windows_since = _REAL_GO["tuning_windows_since"]  # (the rig says "for a year"; here it begins now)
    day = 86400.0
    m.EVENTS.add("boot", T0 - 60 * day)
    for back in (20, 12, 5):
        m.EVENTS.add("driver_error", T0 - back * day)
    assert not os.path.exists(GO.TUNING_WINDOWS_PATH)
    assert AT.recover_interrupted_session()["recovered"] is False       # the app's start: the record begins
    assert read_json(GO.TUNING_WINDOWS_PATH)["since"] == T0
    assert state()["health"] == AT._no_health(), "nothing is said on the first day"
    assert HA._adaptive_tuning_row()["current"] == "Off" and HA._adaptive_tuning_row()["severity"] == "ok"
    clock.now += 3 * day
    m.EVENTS.add("driver_error", clock.now - 3600)              # a real one, after the update
    AT._health_reset()
    h = state()["health"]
    assert h["available"] is True and h["errors_30d"] == 1 and h["events_30d"] == 1 and h["covered_days"] == 3, h
    assert h["last_error_ts"] == clock.now - 3600
    GO.tuning_windows_since = lambda: None                      # the record cannot be opened just now
    AT._health_reset()
    assert state()["health"] == AT._no_health()


# ═════════════════════════════════════════════════════════════════════
# A clock that is set back
# ═════════════════════════════════════════════════════════════════════
def test_an_error_waiting_when_the_clock_is_set_back_is_still_judged_and_new_ones_are_still_seen():
    """A PC whose clock ran an hour fast and is put right mid-game.  The
    waiting error was never judged (its two minutes never seemed to pass) and
    was dropped at the exit as "logged after the game was gone"; no new error
    was seen until the clock had caught up."""
    card = rig(sensitivity="strict")
    start()
    poll()
    error()
    poll()                                                      # T0+40: seen, waiting
    clock.now -= 3600
    poll()                                                      # the first look on the new clock
    s = sess()
    assert s["set_back"] is None and s["count_from"] == T0 - 3600 and s["started"] == T0
    assert [(i["first_ts"], i["last_ts"]) for i in s["pending"]] == [(T0 + 19 - 3600, T0 + 19 - 3600)]
    assert session_file()["judged_until"] < T0 + 19 - 3600
    for _ in range(5):
        poll()
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)] and card.current == L75
    with AT._state_lock:
        assert AT._load_state()["history"][0]["ts"] == T0 + 19 - 3600

    rig()                                                       # what is logged after it, on the new clock
    start()
    poll()
    error()
    poll()
    clock.now -= 3600
    poll()
    error()                                                     # the same burst goes on
    poll()
    assert sess()["pending"][0]["count"] == 2
    for _ in range(7):
        poll()
    assert [d[1:4] for d in decided] == [(T0 + 19 - 3600, T0 + 59 - 3600, "warning")] and sess()["pending"] == []

    rig(sensitivity="strict")                                   # the game closes right after the clock was put back
    start()
    poll()
    error()
    poll()
    clock.now -= 3600
    poll()
    clock.now += 5
    # (its start is a time of the old clock, so the exit game mode reports is taken for an old one, as before;
    # the watch thread sees the game gone and closes the session)
    assert close() == {"handled": False, "reason": "a newer session is running"}
    for _ in range(3):
        poll()
    assert AT._sess is None and texts() == [P.EASED_STRICT(NAME, 0, 1)] and decided[0][4]["ended_game"] is True

    rig(sensitivity="strict")                                   # an error logged on the old clock just before it
    start()                                                     # was put back, and not read yet
    poll()
    poll()
    clock.now += 5
    error(ago=0)                                                # T0+45
    clock.now -= 3600
    poll(15)
    assert [i["first_ts"] for i in sess()["pending"]] == [T0 + 45 - 3600]
    for _ in range(7):
        poll()
    assert game()["level"] == 1 and decided[0][1] == T0 + 45 - 3600

    rig(sensitivity="strict")                                   # the look after the set-back fails, and the game
    start()                                                     # closes before the next one (more than an hour in,
    for _ in range(190):                                        # so that its exit is not taken for an old one)
        poll()
    error()
    poll()
    clock.now -= 3600
    m.EVENTS.fail = True
    poll()                                                      # the set-back is noticed; the log does not answer
    m.EVENTS.fail = False
    assert sess()["set_back"] == {"upto": T0 + 3840, "by": 3600.0} and sess()["pending"][0]["first_ts"] == T0 + 3799
    clock.now += 5
    assert close()["handled"] is True
    assert texts() == [P.EASED_STRICT(NAME, 0, 1)] and decided[0][4]["ended_game"] is True
    assert decided[0][1] == T0 + 199

    rig(sensitivity="strict")                                   # set back by more than the session has lasted,
    start()                                                     # with nothing waiting
    for _ in range(5):
        poll()
    clock.now -= 5 * 3600
    poll()
    assert sess()["count_from"] == T0 - 5 * 3600
    error()
    for _ in range(7):
        poll()
    assert game()["level"] == 1, "every later error was before the session's start, and never counted"


def test_a_record_from_before_the_session_is_never_its_own_whatever_time_it_carries():
    """The clock is set back two hours between two sessions.  When it passes
    the old error's time again, that record was inside the new session's
    window and was counted a second time: one error, "twice within 10 hours
    of play", eased."""
    for restarted in (False, True):
        rig()
        start()
        poll()
        error(ago=0.4)
        error(ago=0)                                            # one incident, two records 0.4 s apart
        was = clock.now
        clock.now += 6
        close()
        assert texts() == [P.W_ENDED(NAME)] and len(decided) == 1
        clock.now -= 2 * 3600
        if restarted:
            new_process()
            with AT._state_lock:                                # (even if nothing at all were remembered of it)
                st = AT._load_state()
                st["tails"].clear()
                AT._save_state(st)
        start(pid=5151)
        s = sess()
        assert s["rec_mark"] is not None and s["count_from"] < was - 3600
        while clock.now < was + 400:
            poll()                                              # the new session runs past the old error's time
        assert len(decided) == 1 and s["pending"] == [] and game()["level"] == 0 and texts() == [P.W_ENDED(NAME)]
        newest = {"channel": "System", "record_id": s["rec_mark"], "ts": clock.now, "kind": "driver_error"}
        assert AT._is_new(s, newest, clock.now) is False
        assert AT._is_new(s, dict(newest, record_id=s["rec_mark"] + 1), clock.now) is True
        error()                                                 # an error of its own still counts
        for _ in range(7):
            poll()
        assert game()["level"] == 1 and texts()[-1] == P.EASED(NAME, 0, 1)

    rig()                                                       # an event log that cannot be read at the start:
    m.EVENTS.fail = True                                        # no mark, and everything else as it was
    assert start()["engaged"] is True and sess()["rec_mark"] is None
    m.EVENTS.fail = False
    poll()
    error()
    for _ in range(7):
        poll()
    assert texts() == [P.W_SURVIVED(NAME)]
    rig(enabled=False)                                          # with the switch off the log is not even asked
    n = len(m.EVENTS.queries)
    assert start()["reason"] == "off" and len(m.EVENTS.queries) == n


# ═════════════════════════════════════════════════════════════════════
# An event log that is slow, or cannot be read
# ═════════════════════════════════════════════════════════════════════
def test_a_look_at_the_event_log_that_fails_when_the_game_closes_is_made_again():
    """It was made once.  Seconds after a driver reset the log is often slow:
    the error that closed the game was then never looked at again."""
    card = rig(held=(100, 500), sensitivity="strict")
    start()
    poll()
    error()
    clock.now += 5
    m.EVENTS.fail = True
    assert close()["handled"] is True
    assert card.current == (100, 500) and session_file() is None, "the card is not kept waiting for the log"
    assert texts() == [] and game()["sessions"] == 1 and [sec for sec, _ in later] == [30.0]
    assert last_line() is None, "nothing is said about a session that could not be looked at yet"
    m.EVENTS.fail = False
    clock.now += 30
    later.pop(0)[1]()
    assert game()["level"] == 1 and texts() == [P.EASED_STRICT(NAME, 0, 1)] and decided[0][4]["ended_game"] is True
    assert last_line() == "Overclock eased to 75% of your overclock after graphics driver errors" and later == []
    assert game()["sessions"] == 1 and round(game()["minutes"] * 60) == 20, "its play time is not added twice"

    card = rig(held=(450, 2225), sensitivity="strict")          # the game is running again by then: lowered at once
    start()
    poll()
    error()
    clock.now += 5
    m.EVENTS.fail = True
    close()
    m.EVENTS.fail = False
    clock.now += 4
    assert start(pid=5151)["level"] == 0 and card.current == FULL
    clock.now += 26
    later.pop(0)[1]()
    assert game()["level"] == 1 and sess()["level"] == 1 and card.current == L75
    assert decided[0][1] < sess()["count_from"], "and it is the old session's error, not the new one's"

    rig()                                                       # a crash with no driver error is only said once the
    start()                                                     # log could be read
    poll()
    m.EVENTS.add("app_crash", clock.now - 3, exe=EXE, pid=PID)
    m.EVENTS.fail = True
    close()
    m.EVENTS.fail = False
    assert texts() == [] and last_line() is None
    later.pop(0)[1]()
    assert texts() == [P.PLAIN_CRASH(NAME)] and last_line() == "Overclock: full, the game closed unexpectedly"

    rig(sensitivity="strict")                                   # only the driver's errors could not be read: the
    start()                                                     # game's own crash record is no proof of "no
    poll()                                                      # graphics driver error" yet
    error()
    m.EVENTS.add("app_crash", clock.now, exe=EXE, pid=PID)
    clock.now += 5
    real = GE.query
    GE.query = lambda *a, **k: ({"ok": False, "events": [], "err": "busy"} if k.get("kinds") == "driver_error"
                                else real(*a, **k))
    try:
        close()
    finally:
        GE.query = real
    assert texts() == [] and game()["plain_crashes"] == []
    later.pop(0)[1]()
    assert texts() == [P.EASED_STRICT(NAME, 0, 1)] and game()["plain_crashes"] == []

    rig(sensitivity="strict")                                   # a log that stays unreadable: three more looks, then
    start()                                                     # it is given up
    poll()
    error()
    clock.now += 5
    m.EVENTS.fail = True
    close()
    waits = []
    while later:
        seconds, fn = later.pop(0)
        waits.append(seconds)
        clock.now += seconds
        fn()
    m.EVENTS.fail = False
    assert waits == [30.0, 120.0, 300.0] and texts() == [] and decided == [] and last_line() is None

    card = rig(held=(100, 500), sensitivity="strict")           # the exit of a run the session carried on over
    start()
    poll()
    error()
    exit_ts = clock.now + 5
    clock.now += 9
    m.exit_proc(PID)
    m.proc(5151, exe=EXE)
    AT.on_game_start(EXE, NAME, 5151)
    m.EVENTS.fail = True
    assert AT.on_game_exit(EXE, exit_ts=exit_ts) == {"handled": True, "kept_open": True}
    m.EVENTS.fail = False
    assert decided == [] and [sec for sec, _ in later] == [30.0] and sess()["judged_until"] == T0 + 20
    clock.now += 30
    later.pop(0)[1]()
    assert game()["level"] == 1 and decided[0][4]["ended_game"] is True and card.current == L75 and later == []


def test_a_run_that_died_is_judged_later_when_the_event_log_cannot_be_read_at_the_start():
    """At the next start after a crash the log is read once, at login.  When
    that failed the session file was removed and nothing could look again."""
    card = interrupted()
    m.EVENTS.add("driver_error", T0 + 70)
    clock.now = T0 + 3600
    m.EVENTS.fail = True
    r = AT.recover_interrupted_session()
    m.EVENTS.fail = False
    assert r["recovered"] is True and r["unread"] is True and r["judged"] is None
    assert card.current == (100, 500) and session_file() is None and decided == [], "the card does not wait"
    kept = read_json(AT.state_path())["unread"]
    assert len(kept) == 1 and kept[0]["exe"] == EXE and kept[0]["tries"] == 1 and kept[0]["seen"] == T0 + 60
    assert [sec for sec, _ in later] == [120.0] and round(game()["minutes"] * 60) == 60
    clock.now += 120
    later.pop(0)[1]()
    assert game()["level"] == 1 and texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]
    assert decided[0][4]["ended_game"] is True and read_json(AT.state_path())["unread"] == [] and later == []
    assert round(game()["minutes"] * 60) == 60, "its play time is not added twice"
    assert AT.recover_interrupted_session()["recovered"] is False and len(decided) == 1

    interrupted()                                               # the app is closed before that look
    m.EVENTS.add("driver_error", T0 + 70)
    clock.now = T0 + 3600
    m.EVENTS.fail = True
    AT.recover_interrupted_session()
    m.EVENTS.fail = False
    del later[:]
    new_process()
    clock.now += 86400
    assert AT.recover_interrupted_session() == {"recovered": False, "reason": "no interrupted session"}
    assert game()["level"] == 1 and len(decided) == 1 and read_json(AT.state_path())["unread"] == []

    interrupted()                                               # a log that stays unreadable: looked at a few more
    m.EVENTS.add("driver_error", T0 + 70)                       # times, further and further apart, then given up
    clock.now = T0 + 3600
    m.EVENTS.fail = True
    AT.recover_interrupted_session()
    waits = []
    while later:
        seconds, fn = later.pop(0)
        waits.append(seconds)
        clock.now += seconds
        fn()
    m.EVENTS.fail = False
    assert waits == [120.0, 300.0, 900.0, 900.0, 900.0] and decided == [] and game()["level"] == 0
    assert read_json(AT.state_path())["unread"] == []

    interrupted()                                               # forgetting the game forgets what waited for it
    m.EVENTS.add("driver_error", T0 + 70)
    clock.now = T0 + 3600
    m.EVENTS.fail = True
    AT.recover_interrupted_session()
    m.EVENTS.fail = False
    assert AT.forget_game(EXE)["ok"] is True and read_json(AT.state_path())["unread"] == []
    later.pop(0)[1]()
    assert decided == [] and game() is None


def lock_free_during(fn):
    """Run fn() and say, for every read of the event log it makes, whether
    another thread could have taken the session lock at that moment."""
    real, free = GE.query, []

    def watched(*a, **k):
        got = []

        def try_it():
            got.append(AT._session_lock.acquire(False))
            if got[0]:
                AT._session_lock.release()
        t = threading.Thread(target=try_it)
        t.start()
        t.join(5)
        free.append(got == [True])
        return real(*a, **k)
    GE.query = watched
    try:
        fn()
    finally:
        GE.query = real
    return free


def look_held_up():
    """The watch thread's next look, on a thread of its own, held up in its
    read of the event log once the log has answered.  Returns the call that
    lets it go on (and waits for it to finish)."""
    real, gate, reading = GE.query, threading.Event(), threading.Event()

    def slow(*a, **k):
        answer = real(*a, **k)
        reading.set()
        gate.wait(10)
        return answer
    GE.query = slow
    t = threading.Thread(target=AT._poll, args=(sess()["id"],))
    t.start()
    try:
        assert reading.wait(5), "the look never read the event log"
    finally:
        GE.query = real

    def let_go():
        gate.set()
        t.join(5)
        assert not t.is_alive()
    return let_go


def test_a_look_that_comes_back_after_the_session_ended_or_was_let_go_of_judges_nothing():
    """The read is made with no lock held, so the session can end, or be let
    go of, while it is under way.  What it brings back is then not this
    look's to judge."""
    card = rig(held=(100, 500))
    start()
    poll()
    error()                                                     # T0+19
    for _ in range(5):
        poll()                                                  # T0+120: seen, still waiting
    clock.now += 20                                             # its two minutes are over as the next look begins
    let_go = look_held_up()
    m.exit_proc(PID)
    AT._defer = deferred.append
    assert AT.on_game_exit(EXE, exit_ts=T0 + 130)["handled"] is True    # the game had closed 111 s after the error
    assert card.current == (100, 500), "the exit did not wait for the look"
    let_go()
    assert decided == [] and texts() == [], "the look found the session closed and judged nothing"
    AT._defer = lambda fn: fn()
    run_deferred()
    assert texts() == [P.W_ENDED(NAME)] and decided[0][4]["ended_game"] is True, "the exit judged it, as closed"
    assert session_file() is None

    card = eased_and_running()                                  # let go of while its game runs on less: left alone
    error()
    clock.now += 20
    let_go = look_held_up()                                     # (it has read the error)
    assert AT.end_active_session("monitor stopped")["released"] is True
    let_go()
    assert sess()["held"] == "released" and sess()["pending"] == [] and decided == []
    for _ in range(8):
        poll()
    assert decided == [] and texts() == [P.HELD_RELEASED(NAME)] and card.current == L50


def test_the_apps_start_puts_the_card_back_and_judges_a_run_that_died_afterwards():
    """The look at the event log was part of the app's start, under the
    session lock: at login after a crash, when the log can take many seconds
    to answer, the whole start (and any game being found) waited for it."""
    card = interrupted()
    m.EVENTS.add("driver_error", T0 + 70)
    clock.now = T0 + 3600
    AT._defer = deferred.append                                 # as in the app: on a thread of its own, afterwards
    reads = len(m.EVENTS.queries)
    r = AT.recover_interrupted_session()
    assert r["recovered"] is True and r["settled"] is True and card.current == (100, 500) and session_file() is None
    assert "judged" not in r and decided == [] and len(deferred) == 1, "what happened is not read during the start"
    assert len(m.EVENTS.queries) == reads and round(game()["minutes"] * 60) == 60
    free = lock_free_during(run_deferred)
    assert game()["level"] == 1 and texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]
    assert decided[0][4]["ended_game"] is True and decided[0][4]["play_minutes_now"] == 1.0
    assert free and all(free), free
    assert AT.recover_interrupted_session()["recovered"] is False and len(decided) == 1

    card = interrupted()                                        # the game is found again before that look is done:
    m.EVENTS.add("driver_error", T0 + 70)                       # it is taken on, and lowered as soon as it is judged
    clock.now = T0 + 3600
    AT._defer = deferred.append
    AT.recover_interrupted_session()
    AT._defer = lambda fn: fn()
    assert start(pid=5151)["level"] == 0 and card.current == FULL
    run_deferred()
    assert game()["level"] == 1 and sess()["level"] == 1 and card.current == L75
    assert sess()["pre"] == [100, 500, 100]

    card = rig(held=(450, 2225), sensitivity="strict")          # the look every 30 s at a session that was kept
    start()
    poll()
    die()
    clock.now += 30
    assert AT.recover_interrupted_session()["kept"] is True
    m.EVENTS.add("driver_error", T0 + 25)
    m.exit_proc(PID)
    clock.now += 30
    free = lock_free_during(later.pop(0)[1])
    assert session_file() is None and texts() == [P.HANDED_BACK(NAME), P.EASED_STRICT(NAME, 0, 1)]
    assert free and all(free), free


def test_an_event_log_that_stays_unreadable_during_play_is_said_on_the_page():
    """Every look failed quietly: the card said the game was looked after,
    nothing was ever eased, and nothing told the user."""
    card = rig()
    m.EVENTS.add("boot", T0 - 40 * 86400)
    start()
    assert state()["health"]["log_readable"] is True and state()["health"]["available"] is True
    m.EVENTS.fail = True
    poll()
    poll()
    assert state()["health"]["log_readable"] is True, "two looks that fail may be a hiccup"
    m.EVENTS.fail = False
    poll()                                                      # it reads again: the count starts over
    looked = clock.now
    m.EVENTS.fail = True
    poll()
    poll()
    assert AT._failed_reads[0] == 2 and state()["health"]["log_readable"] is True
    poll()
    assert AT._failed_reads[0] == 3 and state()["health"]["log_readable"] is False
    assert state()["session"]["level"] == 0 and card.current == FULL, "nothing else changes"
    assert sess()["judged_until"] == looked, "and nothing was marked as looked at"
    close()
    assert like_the_fixture(state(), "log_unreadable") and state()["health"]["log_readable"] is False
    m.EVENTS.fail = False
    start(pid=5151)
    assert state()["health"]["log_readable"] is True
    poll()
    assert AT._failed_reads[0] == 0


def test_the_event_log_is_read_with_no_lock_held():
    """The watch thread read it under the session lock: a read that takes
    seconds kept the hand-back at a game's exit, the next game start and the
    app quitting waiting."""
    card = rig(held=(100, 500))
    m.EVENTS.add("boot", T0 - 40 * 86400)
    start()
    poll()
    real = GE.query

    def play():
        error()
        for _ in range(8):
            poll()                                              # seen, then judged
        assert texts() == [P.W_SURVIVED(NAME)]
    free = lock_free_during(play)
    assert len(free) >= 8 and all(free), free

    def relaunch_then_exit():
        exit_ts = clock.now + 5
        clock.now += 9
        m.exit_proc(PID)
        m.proc(5151, exe=EXE)
        AT.on_game_start(EXE, NAME, 5151)
        AT.on_game_exit(EXE, exit_ts=exit_ts)                   # the exit of the run before: judged off the lock too
        close(pid=5151)
    free = lock_free_during(relaunch_then_exit)
    assert len(free) >= 2 and all(free), free

    card = rig(held=(100, 500))                                 # a read that does not come back
    start()
    poll()
    gate, reading = threading.Event(), threading.Event()

    def stuck(*a, **k):
        reading.set()
        gate.wait(10)
        return real(*a, **k)
    GE.query = stuck
    try:
        clock.now += 20
        t = threading.Thread(target=AT._poll, args=(sess()["id"],))
        t.start()
        assert reading.wait(5)
        GE.query = real
        began = time.time()
        assert AT.get_state()["ok"] is True
        assert close()["handled"] is True and card.current == (100, 500) and AT._sess is None
        assert time.time() - began < 2.0, "the exit waited for the read"
    finally:
        GE.query = real
        gate.set()
    t.join(5)
    assert not t.is_alive() and session_file() is None and card.current == (100, 500)


# ═════════════════════════════════════════════════════════════════════
# I1 to I7 over a long random run
# ═════════════════════════════════════════════════════════════════════
GAMES = (("cod.exe", "Call of Duty", 4242), ("cs2.exe", "Counter-Strike 2", 5151), ("eldenring.exe", "Elden Ring", 6161))
# What happens in a run, and how often: mostly play (polls, errors, exits),
# now and then the user or something else getting in the way.
ACTIONS = (["start"] * 8 + ["poll"] * 12 + ["long_play"] * 8 + ["error"] * 10 + ["exit"] * 5 + ["relaunch"] * 3
           + ["release", "vanish", "step", "step", "save", "apply", "reset", "intruder", "trip", "switch",
              "game_switch", "delete"])


def _check_invariants(card, world):
    saved = world["profile"]
    # I1: the saved overclock is what the test (the user) last saved
    now_saved = _REAL_GO["load_profile"]()
    if saved is None:
        assert not now_saved.get("exists"), "I1: a deleted profile is back"
    else:
        assert (now_saved["core_offset_mhz"], now_saved["mem_offset_mhz"], now_saved["power_pct"]) == saved, "I1"
    assert world["profile_writes"] == world["profile_writes_by_user"], "I1: Adaptive Tuning wrote the profile"

    for c in calls[world["checked"]:]:
        if c["owner"] != MINE:
            continue
        w = c["in_write"]
        assert w is not None, f"I2: a clock write outside _write: {c}"
        if w["why"] == "handback":
            # I2: exactly what was recorded before the session; I3: only at a session's end
            assert (c["core"], c["mem"]) == tuple(w["values"][:2]), f"I2: {c}"
            assert c["session"] is not None and c["stopped"], f"I3: a hand-back with the session still running: {c}"
            assert [c["core"], c["mem"]] == c["session"]["pre"][:2], f"I2: not what was recorded before it: {c}"
            assert c["power"] in (None, c["session"]["pre"][2]), f"I2: {c}"
            continue
        s = c["session"]
        assert s is not None and w["session"] is s and not c["stopped"], f"I3: a write without an open session: {c}"
        assert c["held"] is None, f"a write by a held session: {c}"
        assert c["enabled"], f"I5: a session write with the switch off: {c}"
        prof = c["profile"]
        assert prof.get("exists"), f"I5: a session write with no saved overclock: {c}"
        want = P.offsets_for([prof["core_offset_mhz"], prof["mem_offset_mhz"]], c["level"], c["lal"])
        for got, share, whole in zip((c["core"], c["mem"]), want, (prof["core_offset_mhz"], prof["mem_offset_mhz"])):
            assert got <= share, f"I2: above the level's share: {c}"
            assert got <= max(whole, 0) or got == whole, f"I2: above the saved overclock: {c}"
    world["checked"] = len(calls)

    # I4: levels only go down by themselves, one step, on the record; only the user's step goes up
    with AT._state_lock:
        games = AT._load_state()["games"]
    for exe, g in games.items():
        old = world["levels"].get(exe, 0)
        new = g["level"]
        if new != old:
            last = g["changes"][-1]
            assert last["to_level"] == new and last["text"], f"I4: a change that is not on record: {exe}"
            if world["user_step"] == exe:
                assert last["by"] == "user"
            else:
                assert last["by"] == "driver" and new == old + 1, f"I4: {exe} went from {old} to {new} by itself"
                world["eases"].setdefault((exe, world["session_no"].get(exe, 0)), 0)
                world["eases"][(exe, world["session_no"].get(exe, 0))] += 1
        world["levels"][exe] = new
    world["levels"] = {exe: lv for exe, lv in world["levels"].items() if exe in games}

    # I5: nothing is open that should not be
    s = AT._sess
    if s is not None:
        # (with the switch off only a session that was let go of while its game still runs is open)
        assert (enabled_now() or s["held"] == "released") and not s["stop"].is_set()
        assert session_file() is not None and session_file()["exe"] == s["exe"]
        assert [list(w) for w in s["writes"]] == session_file()["writes"]
    else:
        # I6: after the end the watchdog runs again and the file is gone
        assert not watchdog_paused(), "I6: the drift watchdog is still paused"
        assert session_file() is None, "I6: the session file is still there"

    # I7: no error is judged twice, and only within a session of that game
    spans = sorted((d[1], d[2], d[0]) for d in decided)
    for (a1, b1, _), (a2, b2, _) in zip(spans, spans[1:]):
        assert b1 < a2, f"I7: an error was judged twice: {spans}"
    spans = list(world["sessions"])
    if world["open_since"] is not None:
        spans.append((world["open_since"][0], world["open_since"][1], clock.now))
    for d in decided[world["decided"]:]:
        assert any(lo <= d[1] <= hi for (exe, lo, hi) in spans if exe == d[0]), f"I7: {d}"
    world["decided"] = len(decided)


trace: list = []            # (step, action, seconds since T0, open session, held, incidents judged so far)


def _random_run(seed, steps):
    del trace[:]
    rnd = random.Random(seed)
    card = rig(held=None, sensitivity=rnd.choice(["balanced", "strict", "balanced"]))
    world = {"profile": (450, 2225, 100), "profile_writes": 0, "profile_writes_by_user": 0, "checked": 0,
             "levels": {}, "user_step": None, "eases": {}, "session_no": {}, "sessions": [], "decided": 0,
             "running": None, "open_since": None, "released": 0}

    def counting(name):
        real = _REAL_GO[name]

        def wrapped(*a, **k):
            world["profile_writes"] += 1
            return real(*a, **k)
        return wrapped
    for name in ("save_profile", "delete_profile", "set_profile_apply_on_startup"):
        setattr(GO, name, counting(name))

    def user_saves(core, mem, power=100):
        world["profile_writes_by_user"] += 1
        AT.note_manual_change()
        assert GO.save_profile({"core_offset_mhz": core, "mem_offset_mhz": mem, "power_pct": power})["ok"]
        world["profile"] = (core, mem, power)

    def session_opened():
        s = AT._sess
        if s is not None and world["open_since"] is None:
            world["open_since"] = (s["exe"], s["count_from"], s["id"])
            world["session_no"][s["exe"]] = world["session_no"].get(s["exe"], 0) + 1

    def session_closed():
        if world["open_since"] is not None and (AT._sess is None or AT._sess["id"] != world["open_since"][2]):
            exe, since, _ = world["open_since"]
            world["sessions"].append((exe, since, clock.now))
            world["open_since"] = None

    for step in range(steps):
        world["user_step"] = None
        was_open = AT._sess
        was_taken = bool(was_open and (was_open["held"] or AT._taken_over(was_open)
                                       or AT._tripped_since(was_open["started"])))
        action = rnd.choice(ACTIONS)
        running = world["running"]
        on_card = card.current
        if action == "start" and running is None:
            exe, name, pid = rnd.choice(GAMES)
            m.proc(pid, exe=exe)
            TRACKED[0] = {"exe": exe, "display": name, "pid": pid}
            world["running"] = (exe, name, pid)
            AT.on_game_start(exe, name, pid)
        elif action == "poll" and AT._sess is not None:
            poll(rnd.choice([20, 20, 20, 45, 400]))
        elif action == "long_play" and AT._sess is not None:
            for _ in range(rnd.randint(5, 40)):
                if AT._sess is None:
                    break
                poll(20)
        elif action == "error":
            clock.now += 1
            error(ago=0.5)
        elif action == "exit" and running is not None:
            m.exit_proc(running[2])
            clock.now += rnd.choice([1, 12, 12, 200])
            AT.on_game_exit(running[0], exit_ts=clock.now)
            world["running"], TRACKED[0] = None, None
        elif action == "relaunch" and running is not None and AT._sess is not None:
            m.exit_proc(running[2])
            clock.now += 12
            AT._defer = deferred.append
            AT.on_game_exit(running[0], exit_ts=clock.now)
            session_closed()
            clock.now += 4
            m.proc(running[2], exe=running[0])
            AT.on_game_start(*running)
            session_opened()
            AT._defer = lambda fn: fn()
            run_deferred()
        elif action == "release" and AT._sess is not None:
            AT.end_active_session(rnd.choice(["manual", "user-tray", "monitor stopped", "user quit"]))
        elif action == "step" and world["levels"]:
            exe = rnd.choice(sorted(world["levels"]))
            world["user_step"] = exe
            AT.step_game(exe, rnd.choice(["slower", "slower", "faster", "full"]), confirm=True)
        elif action == "save":
            user_saves(rnd.choice([300, 450, 600, -50]), rnd.choice([1500, 2225, 3000]), rnd.choice([100, 110]))
        elif action == "delete" and world["profile"] is not None and rnd.random() < 0.3:
            world["profile_writes_by_user"] += 1
            AT.note_manual_change()
            GO.delete_profile()
            world["profile"] = None
        elif action == "apply":
            user_applies(rnd.choice([0, 100, 500]), rnd.choice([0, 500, 2500]), note=rnd.random() < 0.7)
        elif action == "reset":
            GO.reset_oc()
        elif action == "intruder" and AT._sess is not None:
            card.current = (rnd.choice([0, 50, 475]), rnd.choice([0, 100, 2300]))
        elif action == "trip" and rnd.random() < 0.4:
            trip(cooldown=rnd.choice([0.0, 60.0]))
        elif action == "switch":
            AT.set_settings(enabled=not enabled_now())
        elif action == "game_switch" and world["levels"]:
            exe = rnd.choice(sorted(world["levels"]))
            g = game(exe)
            AT.set_game_enabled(exe, not g["enabled"])
        elif action == "vanish" and running is not None:
            m.exit_proc(running[2])                             # the game is gone and nobody reports it
            world["running"], TRACKED[0] = None, None
        if action in ("release", "switch", "game_switch") and was_open is not None and running is not None:
            # letting go of a session never raises the clocks under a game that is still running
            if AT._sess is None or AT._sess is was_open:
                assert card.current[0] <= on_card[0] + 5 and card.current[1] <= on_card[1] + 5, (
                    f"(seed {seed}, step {step}, {action}): {on_card} -> {card.current} under a running game")
                world["released"] += AT._sess is was_open and was_open["held"] == "released"
        if world["profile"] is None and rnd.random() < 0.5:
            user_saves(450, 2225)
        if not enabled_now() and rnd.random() < 0.5:
            AT.set_settings(enabled=True)
        if rnd.random() < 0.2:
            cool()
        session_opened()
        closed_now = was_open is not None and AT._sess is not was_open
        session_closed()
        session_opened()
        trace.append((step, action, round(clock.now - T0, 1), AT._sess["exe"] if AT._sess else None,
                      AT._sess["held"] if AT._sess else None, len(decided)))
        _check_invariants(card, world)
        if closed_now and not was_taken and was_open["writes"] and not card.intruder:
            hb = was_open.get("handback") or {}
            if hb.get("verified"):
                # I6: the card reads back what the session found
                pre = (was_open["pre"][0], was_open["pre"][1])
                if AT._sess is None and action in ("exit", "release", "switch", "game_switch", "poll", "long_play"):
                    assert AT._same(card.current, pre), f"I6 (seed {seed}, step {step}, {action}): {card.current} {pre}"
    # I4: at most one step down by the driver per session
    assert all(n == 1 for n in world["eases"].values()), world["eases"]
    AT.end_active_session("the end")
    _check_invariants(card, world)
    return world


def test_the_invariants_hold_over_long_random_runs():
    eased = released = 0
    for seed in (1, 2, 3, 5, 8, 13, 21, 34):
        world = _random_run(seed, 400)
        eased += len(world["eases"])
        released += world["released"]
        if os.environ.get("AT_RANDOM_STATS"):
            print(f"seed {seed}: {len(world['sessions'])} sessions, {len(decided)} incidents judged, "
                  f"{len(world['eases'])} eases, {len(calls)} clock writes, {world['released']} kept at a release",
                  flush=True)
        assert len(world["sessions"]) > 10, "the run did not exercise anything"
    assert eased > 0, "no run ever eased a game: the test would not notice a broken ease"
    assert released > 0, "no run ever let go of a session whose game was running on less than was there before"
    restore()


# ═════════════════════════════════════════════════════════════════════
# The real watch thread
# ═════════════════════════════════════════════════════════════════════
def test_the_watch_thread_polls_and_stops_with_its_session():
    card = rig(held=(100, 500))
    AT._WATCH_THREAD = True
    AT.POLL_S = 0.03
    AT._now = time.time                                         # (the thread paces itself in real time)
    GE._now = time.time
    try:
        start()
        s = sess()
        t = s["thread"]
        assert t.is_alive() and t.daemon and t.name == f"adaptive-tuning-{EXE}"
        card.current = (0, 0)                                   # the driver drops the clocks: the thread notices
        assert m.wait(lambda: card.current == FULL), "the thread never looked"
        assert m.wait(lambda: s["play_s"] > 0 and (session_file() or {}).get("seen", 0) > s["started"])
        # one look that goes wrong does not end the watch: the next one carries on
        real_running, raised = AT._exe_running, []

        def breaks_once(exe):
            if not raised:
                raised.append(exe)
                raise RuntimeError("the process list blew up")
            return real_running(exe)
        AT._exe_running = breaks_once
        assert m.wait(lambda: bool(raised))
        card.current = (0, 0)
        assert m.wait(lambda: card.current == FULL) and t.is_alive(), "the thread died on one error"
        clock.now = time.time()                                 # (game mode reports the exit at the real time)
        assert close()["handled"] is True, "the exit was not taken"
        t.join(5)
        assert not t.is_alive(), "the thread did not end with its session"
        card.current = (9, 9)
        time.sleep(0.15)
        assert card.current == (9, 9), "I3: written after the session had ended"
    finally:
        AT.POLL_S = _REAL_AT["POLL_S"]
        restore()


def test_the_watch_thread_sees_a_driver_error_and_judges_it():
    """The look at the event log is the watch thread's own, made after it has
    let go of the session lock: the whole way, with the real thread."""
    card = rig(held=(100, 500))
    AT._WATCH_THREAD = True
    AT.POLL_S, AT.PENDING_S = 0.03, 0.2
    AT._now = time.time
    GE._now = time.time
    AT._defer = _REAL_AT["_defer"]
    try:
        start()
        s = sess()
        assert m.wait(lambda: s["play_s"] > 0)
        m.EVENTS.add("driver_error", time.time())
        assert m.wait(lambda: texts() == [P.W_SURVIVED(NAME)]), "the thread never judged it"
        assert decided[0][4]["ended_game"] is False and s["thread"].is_alive() and card.current == FULL
        assert m.wait(lambda: s["pending"] == [] and (session_file() or {}).get("judged_until", 0) > decided[0][2])
        m.EVENTS.add("driver_error", time.time())               # the game closes on the next one
        time.sleep(0.01)
        clock.now = time.time()
        assert close()["handled"] is True and card.current == (100, 500)
        assert m.wait(lambda: (game() or {}).get("sessions") == 1)
        s["thread"].join(5)
        assert not s["thread"].is_alive() and AT._sess is None and session_file() is None
        assert len(decided) == 1, "what went on from the judged error is not a second one"
    finally:
        AT.POLL_S, AT.PENDING_S = _REAL_AT["POLL_S"], 120.0
        restore()


def test_the_watch_thread_closes_a_session_nobody_ends():
    card = rig(held=(100, 500))
    AT._WATCH_THREAD = True
    AT.POLL_S = 0.03
    AT._now = time.time
    GE._now = time.time
    AT._defer = _REAL_AT["_defer"]                              # the second half on its own thread, as in the app
    try:
        start()
        t = sess()["thread"]
        m.exit_proc(PID)
        assert m.wait(lambda: AT._sess is None and card.current == (100, 500))
        assert m.wait(lambda: (game() or {}).get("sessions") == 1)
        t.join(5)
        assert not t.is_alive() and session_file() is None and not watchdog_paused()
    finally:
        AT.POLL_S = _REAL_AT["POLL_S"]
        restore()


def test_zz_nothing_is_left_behind():
    """The last test: no session, no thread, the event log's handles closed."""
    restore()
    m.fresh()
    assert AT._sess is None and AT.session_active() is False
    assert not [t for t in threading.enumerate() if t.name.startswith("adaptive-tuning-") and t.is_alive()]
    assert m.EVENTS.open_handles == 0
