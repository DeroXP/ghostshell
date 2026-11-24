import sandbox as m
# Game mode under awkward timing, on its own and together with Adaptive
# Tuning: game mode put back by hand in the middle of a game, the monitor
# stopped mid-game, a game relaunched seconds after it closed, the tracker
# moving to a second game, a game that restarts into a new process, a pid that
# belongs to another process by the time the game closes, and the start-up
# check meeting values the optimizer set on purpose.
#
# Adaptive Tuning is the real engine here (its own tests, with a card that
# behaves like one, are in tests/test_adaptive_tuning.py).  Nothing touches a
# real clock: gpu_overclock's apply, light write and read-back are stand-ins
# that only move the numbers in `card` and `held`.

import os
import threading
import time

from core import reg_originals as RO, process_tuning as PT

GP = m.app("core.game_profiles")
OPT = m.app("core.optimizer")
ST = m.app("core.scheduler_tweaks")
from core import adaptive_tuning as AT, gpu_overclock as GO, capabilities as CAP           # noqa: E402
from core import adaptive_policy as P                                                    # noqa: E402
from core import crash_recovery as CR, notifier as NOTE                                    # noqa: E402
from core import background_pauser as BPZ                                                # noqa: E402

PRIO_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
HIGH, NORMAL, ABOVE = PT.HIGH_PRIORITY_CLASS, PT.NORMAL_PRIORITY_CLASS, PT.ABOVE_NORMAL_PRIORITY_CLASS
FULL, BEFORE = (450, 2225), (100, 500)

# ── the graphics card, as far as Adaptive Tuning asks about it ────────────
held = {"core": 100, "mem": 500, "power": 100, "active": True}     # what the app holds on the card
card = [BEFORE]                                                     # what the card reads back
oc_calls: list = []         # every clock write: (how, core, mem, who)
cr_calls: list = []         # what crash_recovery is told at a game exit
toasts: list = []


def _apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100, owner=None):
    core, mem = int(core_offset_mhz), int(mem_offset_mhz)
    oc_calls.append(("apply", core, mem, owner))
    power = held["power"] if power_pct is None else int(power_pct)
    held.update(core=core, mem=mem, power=power, active=bool(core or mem or power != 100))
    card[0] = (core, mem)
    return {"ok": True}


def _write_offsets(core=0, mem=0, owner=None, if_seq=None, **more):
    oc_calls.append(("light", int(core), int(mem), owner))
    held.update(core=int(core), mem=int(mem), active=bool(core or mem or held["power"] != 100))
    card[0] = (int(core), int(mem))
    return {"ok": True, "err": ""}


GO.apply_oc = _apply_oc
GO.write_offsets = _write_offsets
GO.get_applied_oc = lambda: dict(held)
GO.get_current_offsets = lambda: {"ok": True, "core_offset_mhz": card[0][0], "mem_offset_mhz": card[0][1]}
GO.load_profile = lambda: {"exists": True, "core_offset_mhz": 450, "mem_offset_mhz": 2225, "power_pct": 100,
                           "apply_on_startup": True}
GO.detect_gpu_oc_capability = lambda: {"ok": True, "vendor": "nvidia", "limits": {"power_max_w": 300}}
GO.is_tuning_active = lambda: False
GO.wait_startup_apply = lambda timeout=0.0: True
CAP.detect = lambda force=False: {"has_nvidia": True, "gpu_name": "NVIDIA GeForce RTX 5070 Ti"}
CR.handle_game_exit = lambda **k: cr_calls.append(k) or {"crashed": False}
NOTE.toast = lambda title, text, force=False: toasts.append((title, text)) or {"ok": True}
GP._notifier = None
BPZ.get_settings = lambda: {"enabled": False, "auto_resume_on_game_exit": True}
BPZ.resume_now = lambda reason="": {"resumed": []}
AT._WATCH_THREAD = False        # (its polls are tested in test_adaptive_tuning.py; no thread outlives a test here)
AT._uptime_s = lambda: 30 * 86400.0     # Windows has been up for a month (not this PC's own uptime)
AT._defer = lambda fn: fn()     # the second half of an exit runs at once


def at_switch(on: bool) -> None:
    """Adaptive Tuning's own switch (it is off after m.fresh(), as on a new install)."""
    with AT._state_lock:
        AT._save_settings(dict(AT._default_settings(), enabled=on))


def card_reset() -> None:
    held.update(core=100, mem=500, power=100, active=True)
    card[0] = BEFORE
    del oc_calls[:], cr_calls[:], toasts[:]
    AT._last_game = AT._last_closed = None
    AT._recheck_scheduled[0] = False
    AT._failed_reads[0] = 0
    AT._supported_cache.update(ok=None)
    AT._health_cache.update(at=None, value=None)
    tail = getattr(AT, "_burst_tail", None)         # (what was judged of a burst: one test's error is not the next's)
    if hasattr(tail, "clear"):
        tail.clear()


def mine() -> list:
    return [c[:3] for c in oc_calls if c[3] == "adaptive_tuning"]


# ═════════════════════════════════════════════════════════════════════
# Game mode
# ═════════════════════════════════════════════════════════════════════
def test_a_streaming_apps_pid_taken_by_another_process_is_left_alone():
    m.fresh()
    m.proc(600, exe="Discord.exe", priority=HIGH)     # a copy the user had raised themselves
    m.proc(601, exe="Discord.exe")
    GP._engine["streamer_prior"] = {}
    GP._raise_streamers([{"exe": "discord.exe", "pid": 600}, {"exe": "discord.exe", "pid": 601}])
    assert m.K.procs[600]["priority"] == ABOVE and m.K.procs[601]["priority"] == ABOVE
    m.exit_proc(600)
    m.proc(600, exe="notepad.exe", created=999_999)  # the pid is another program's by the time the game closes
    GP.restore_normal_mode("manual")
    assert m.K.procs[600]["priority"] == NORMAL, "the other program was raised to High"
    assert m.K.procs[601]["priority"] == NORMAL, "the streaming app itself is still put back"


def test_values_the_optimizer_set_survive_the_start_up_check():
    """Hard tweaks (0x28) and idle off, then the restart Windows asks for:
    the start-up check took both for game-mode leftovers and put the old
    baseline back."""
    m.fresh()
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x26)
    GP.check_and_restore_on_boot()                  # first start: the baseline
    OPT._reg(PRIO_KEY, "Win32PrioritySeparation", "40")
    ST.toggle_cpu_idle(True)
    RO._cache = None                                # (the app restarts)
    GP.check_and_restore_on_boot()
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 40
    assert m.cell(m.USERPLAN, m.IDLE)[0] == 1
    # A leftover of game mode's own is still put back.
    m.fresh()
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x26)
    GP.check_and_restore_on_boot()
    RO.remember(PRIO_KEY, "Win32PrioritySeparation", "40", scope="gamemode")
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 40)
    GP.check_and_restore_on_boot()
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 0x26


def test_stopping_the_monitor_during_a_restore_does_not_restore_twice():
    """The second restore found no records and wrote the old baseline over
    what the first had just put back."""
    m.fresh()
    calls = []
    real = GP._restore_normal_mode
    GP._restore_normal_mode = lambda reason: calls.append(reason)
    try:
        GP._engine.update(gaming_mode=True, monitoring=False)
        gen = GP._engine.get("monitor_gen", 0)
        with GP._session_lock:                      # the tray's restore is under way
            t = threading.Thread(target=GP._monitor_loop, args=(gen,), daemon=True)
            t.start()
            time.sleep(0.1)
            GP._engine["gaming_mode"] = False       # ...and done
        t.join(5)
        assert calls == []
    finally:
        GP._restore_normal_mode = real
        GP._engine["gaming_mode"] = False


# ═════════════════════════════════════════════════════════════════════
# Game mode with the monitor running (as in test_game_mode.py: a "game"
# is whatever the stand-in scanner returns, and a tick takes 20 ms)
# ═════════════════════════════════════════════════════════════════════
IM = m.app("core.idle_mode")
candidates: list = []
ECO, UNSET = (1, 1), (0, 0)


class _FastClock:
    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, seconds):
        time.sleep(0.02)


GP.time = _FastClock()
GP._post_game_refresh = lambda name=None, pids=None: None
GP._find_running_streaming_exes = lambda: []
GP._scan_game_candidates = lambda: [c for c in candidates
                                    if c["pid"] in m.ALIVE and c["pid"] != GP._engine.get("active_pid")]
GO.read_live_state = lambda: {"ok": False}
GO.get_stock_baseline = lambda: {}


def gm_fresh(adaptive=False):
    """A clean machine with the monitor stopped.  `adaptive`: Adaptive Tuning
    switched on (the app holds +100 / +500 on the card, the saved overclock is
    +450 / +2225)."""
    GP.stop_monitoring()
    time.sleep(0.12)                                # let a running loop notice and leave
    AT.end_active_session("test reset")
    m.fresh(plan=m.BALANCED)
    card_reset()
    at_switch(adaptive)
    os.makedirs(GP.PROFILES_DIR, exist_ok=True)
    del candidates[:]
    GP._engine.update(gaming_mode=False, active_game=None, active_exe=None, active_pid=None, changes=[],
                      proc_prior=None, game_procs={}, checks=[], exit_ticks=0, streamer_prior={},
                      eco_on=False, eco_seen={}, eco_procs={}, eco_failed=0, held_off={})
    GP._target_restore = {}
    IM._state.update(engaged=False, restore=None)


def launch(pid=4242, exe="game.exe", **process):
    m.proc(pid, exe=exe, **process)
    candidates.append({"pid": pid, "exe": exe, "name": "Game", "ws_mb": 900, "foreground": True, "score": 1900})
    GP.start_monitoring()
    return m.wait(lambda: GP._engine["gaming_mode"] and GP._engine["active_pid"] == pid
                  and any("CPU idle" in c for c in GP._engine["changes"]))


def stop():
    GP.stop_monitoring()
    m.wait(lambda: not GP._engine["gaming_mode"])


def thr(pid):
    return m.K.procs[pid].get("throttle", UNSET)


def test_forcing_normal_mode_mid_game_is_not_undone_at_the_next_tick():
    """The tray's Force normal mode lasted until the next tick (3 s), which
    found the same game and applied everything again."""
    gm_fresh()
    assert launch()
    GP.restore_normal_mode("user-tray")
    assert not GP._engine["gaming_mode"] and m.K.procs[4242]["priority"] == NORMAL
    time.sleep(0.3)                                 # a dozen ticks
    assert not GP._engine["gaming_mode"], "the same game was taken up again"
    assert m.K.procs[4242]["priority"] == NORMAL
    m.exit_proc(4242)                               # it closes...
    assert m.wait(lambda: not GP._engine["held_off"]), "...and is no longer held"
    assert launch(pid=4545), "its next launch gets game mode as usual"
    stop()


def test_a_held_game_is_not_taken_up_when_another_game_closes():
    gm_fresh()
    assert launch()
    GP.restore_normal_mode("manual")                # forced while game A runs
    assert launch(pid=5151, exe="other.exe")        # game B, started afterwards: game mode as usual
    m.exit_proc(5151)                               # B closes, A still runs
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    time.sleep(0.2)
    assert not GP._engine["gaming_mode"] and m.K.procs[4242]["priority"] == NORMAL, "switched to A"
    stop()


def test_forcing_normal_mode_with_nothing_applied_writes_nothing():
    """With no game running, Force normal mode wrote the baseline saved at
    some earlier game over whatever the user or the optimizer had set since."""
    gm_fresh()
    GP._save_baseline({"priority_sep": "2", "sys_responsiveness": "14", "net_throttle": "a",
                       "cpu_idle_disabled": False, "idle_probe": "qh"})
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x28)          # set since
    m.cell(m.BALANCED, m.IDLE)[0] = 1
    m.clear_logs()
    GP.restore_normal_mode("user-tray")
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 0x28 and not m.ran("reg", "add")
    assert m.cell(m.BALANCED, m.IDLE)[0] == 1
    GP._set_gaming_flag()                           # a session cut short (a crash): still put back
    GP.restore_normal_mode("manual")
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 2 and m.cell(m.BALANCED, m.IDLE)[0] == 0


def test_another_game_running_alongside_stays_off_the_efficiency_cores():
    gm_fresh()
    m.proc(5151, exe="cs2.exe")                     # a game the detector knows
    m.proc(5152, exe="Something-Win64-Shipping.exe")    # one it recognises by its name
    m.proc(5153, exe="notepad.exe")
    assert launch()
    assert thr(5151) == UNSET and thr(5152) == UNSET, "taken for background programs"
    assert thr(5153) == ECO
    stop()


def test_idle_mode_is_released_before_game_mode_records_the_plan():
    """Idle Mode noticed the game only at its next poll; by then a max_fps
    game had saved Idle Mode's Balanced plan as the one to put back, and the
    user's own plan was lost."""
    gm_fresh()
    m.PLAN["active"] = m.USERPLAN
    GP.AUTO_TARGET["game.exe"] = "max_fps"          # (game mode's own table: it no longer asks the tuner's state)
    try:
        m.PLAN["active"] = m.BALANCED               # Idle Mode engaged: Balanced, the user's plan on record
        IM._state.update(engaged=True, restore={"power_guid": m.USERPLAN, "oc": None})
        assert launch()
        assert IM._state["engaged"] is False
        assert GP._load_target_restore()["power_guid"] == m.USERPLAN
        m.exit_proc(4242)
        assert m.wait(lambda: not GP._engine["gaming_mode"])
        assert m.PLAN["active"] == m.USERPLAN, "Balanced was put back as the user's plan"
        stop()
    finally:
        GP.AUTO_TARGET.pop("game.exe", None)


# ═════════════════════════════════════════════════════════════════════
# Game mode and Adaptive Tuning together (the real engine, the monitor
# running)
# ═════════════════════════════════════════════════════════════════════
def looked_after(exe="game.exe"):
    s = AT._sess
    return s is not None and s["exe"] == exe and card[0] == FULL


def test_a_game_is_looked_after_from_its_start_to_its_exit():
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after), (AT._sess, card)
    assert AT._sess["pre"] == [100, 500, 100] and AT._sess["pid"] == 4242 and AT._sess["name"] == "Game"
    assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == FULL, "what the session runs the game at"
    assert os.path.exists(AT.session_path())
    m.exit_proc(4242)
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE), "the card is handed back as it was found"
    assert mine() == [("apply", 450, 2225), ("apply", 100, 500)]
    assert not os.path.exists(AT.session_path())
    time.sleep(0.15)
    assert cr_calls == [], "Adaptive Tuning handled the exit: crash_recovery is not asked as well"
    assert m.wait(lambda: AT.get_state()["games"][0]["sessions"] == 1)
    stop()


def test_switched_off_it_writes_nothing_and_crash_recovery_keeps_its_job():
    gm_fresh(adaptive=False)
    assert launch()
    time.sleep(0.1)
    assert AT._sess is None and oc_calls == [] and AT.get_state()["games"] == []
    m.exit_proc(4242)
    assert m.wait(lambda: cr_calls)
    assert (cr_calls[0]["active_core"], cr_calls[0]["active_mem"]) == BEFORE and oc_calls == []
    stop()


def test_the_switch_turned_on_mid_game_takes_on_the_game_that_game_mode_tracks():
    """The engine's own links to game mode, with nothing standing in for
    them: which game is tracked, whether the monitor runs, and whether a
    session is open (which the tests on the GPU page ask through
    gpu_overclock).  Each of them could be bent without a test noticing."""
    gm_fresh(adaptive=False)
    assert launch()
    time.sleep(0.1)
    assert AT._sess is None and oc_calls == []
    state = AT.set_settings(enabled=True)["state"]      # the switch on the card, while the game runs
    assert AT._sess is not None and AT._sess["exe"] == "game.exe" and AT._sess["pid"] == 4242, AT._sess
    assert card[0] == FULL and mine() == [("apply", 450, 2225)]
    assert state["watching"] is True and state["session"]["exe"] == "game.exe"
    assert state["game"] == {"exe": "game.exe", "name": "Game", "looked_after": True, "reason": ""}
    # a test on the GPU page is refused while the game is looked after, and while its session is left alone
    refusal = {"ok": False, "refused": "adaptive_session", "err": GO.SESSION_REFUSAL}
    assert GO.start_stability_probe() == refusal
    AT._sess["held"] = "manual"
    assert AT.session_active() is True and GO.start_stability_probe() == refusal
    AT._sess["held"] = None
    # the game's own switch off: said about the game that is running, by its real name
    state = AT.set_game_enabled("game.exe", False)["state"]
    assert AT._sess is None and card[0] == BEFORE, "handing back only lowers here, so it is done at once"
    assert state["game"] == {"exe": "game.exe", "name": "Game", "looked_after": False, "reason": "switched_off"}
    assert state["session"] is None and state["watching"] is True
    state = AT.set_game_enabled("game.exe", True)["state"]
    assert AT._sess is not None and card[0] == FULL and state["game"]["looked_after"] is True
    stop()
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    assert AT.get_state()["watching"] is False


def test_forcing_normal_mode_mid_game_ends_the_session_and_hands_the_card_back():
    """Game mode only reports an exit for a game it is still tracking.  After
    Force normal mode the game is let go of, so its session was never ended:
    the card stayed on the session's values, the drift watchdog stayed paused
    and every test on the GPU page was refused until Vispora restarted."""
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    GP.restore_normal_mode("user-tray")
    assert AT._sess is None and card[0] == BEFORE and not AT.session_active()
    assert mine()[-1] == ("apply", 100, 500) and "adaptive_tuning" not in GO._oc_watchdog_pause_sources
    assert AT.get_state()["history"] == [], "a release is not judged as a crash"
    time.sleep(0.3)                                 # a dozen ticks: the same game is not taken up again
    assert AT._sess is None and card[0] == BEFORE
    m.exit_proc(4242)
    assert m.wait(lambda: not GP._engine["held_off"])
    time.sleep(0.1)
    assert len(mine()) == 2 and cr_calls == []
    assert launch(pid=4545), "its next launch is looked after as usual"
    assert m.wait(looked_after)
    stop()
    assert AT._sess is None and card[0] == BEFORE


L50 = (225, 1113)


def eased_and_running():
    """game.exe, which the graphics driver eased to 50% some time ago, is
    being played.  The app held the full saved overclock before it (applied at
    start-up), so handing the card back would raise the clocks."""
    gm_fresh(adaptive=True)
    held.update(core=450, mem=2225, power=100, active=True)
    card[0] = FULL
    with AT._state_lock:
        st = AT._load_state()
        game = P.new_game("game.exe", "Game", time.time() - 86400)
        game.update(level=2, limit_at_level=[450, 2225], minutes=900.0, sessions=20)
        st["games"]["game.exe"] = game
        AT._save_state(st)
    assert launch()
    assert m.wait(lambda: AT._sess is not None and card[0] == L50), (AT._sess, card)
    assert AT._sess["pre"] == [450, 2225, 100] and mine() == [("apply", 225, 1113)]
    return AT._sess


def the_game_closes(s, pid=4242):
    """What the watch thread does once the game is gone (no thread runs here)."""
    m.exit_proc(pid)
    for _ in range(AT.MISSES_TO_CLOSE):
        AT._poll(s["id"])


def test_forcing_normal_mode_on_an_eased_game_never_raises_the_card():
    """Force normal mode handed the card back at once: the full saved
    overclock went under a game that had been eased because it fails on it,
    while that game was still being played."""
    s = eased_and_running()
    GP.restore_normal_mode("user-tray")
    assert AT._sess is s and s["held"] == "released", "the session is kept until the game closes"
    assert card[0] == L50 and mine() == [("apply", 225, 1113)], "nothing was written"
    assert "adaptive_tuning" in GO._oc_watchdog_pause_sources, "nor may the drift watchdog put the old value back"
    assert [h["text"] for h in AT.get_state()["history"]] == [P.HELD_RELEASED("Game")]
    assert AT.get_state()["session"]["held"] == "released" and AT.session_active()
    time.sleep(0.3)                                 # a dozen ticks of the monitor: still nothing
    assert card[0] == L50 and len(mine()) == 1
    the_game_closes(s)
    assert AT._sess is None and card[0] == FULL, "handed back once the game has closed"
    assert mine() == [("apply", 225, 1113), ("apply", 450, 2225)]
    assert "adaptive_tuning" not in GO._oc_watchdog_pause_sources and not os.path.exists(AT.session_path())
    state = AT.get_state()
    assert state["games"][0]["level"] == 2 and len(state["history"]) == 1, "a release judges nothing"
    assert AT.report_line("game.exe", time.time()) == "Overclock: kept until the game closed"
    assert cr_calls == []
    stop()


def test_stopping_the_monitor_on_an_eased_game_never_raises_the_card_and_it_is_found_again():
    s = eased_and_running()
    stop()
    assert AT._sess is s and s["held"] == "released" and card[0] == L50 and len(mine()) == 1
    assert AT.get_state()["watching"] is False
    GP._engine["held_off"] = {}
    GP.start_monitoring()                           # Start Monitoring again while the game still runs
    assert m.wait(lambda: GP._engine["gaming_mode"] and AT._sess is s and s["held"] is None), (s["held"],)
    assert card[0] == L50 and len(mine()) == 1, "looked after again, and nothing had to be written"
    m.exit_proc(4242)
    assert m.wait(lambda: AT._sess is None and card[0] == FULL)
    assert mine()[-1] == ("apply", 450, 2225) and cr_calls == []
    stop()


def test_stopping_the_monitor_mid_game_ends_the_session():
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    stop()
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    assert not os.path.exists(AT.session_path()) and cr_calls == []


def test_a_game_relaunched_right_after_it_closed_is_looked_after_again():
    """The game was detected again 3 to 4 s after normal mode came back (after
    2 of the 3 real driver crashes on the owner's PC).  The new session was
    refused, or was closed by the old one's exit."""
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    m.exit_proc(4242)
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert launch(pid=4545)                         # at the very next tick
    assert m.wait(lambda: looked_after() and AT._sess["pid"] == 4545)
    assert AT._sess["pre"] == [100, 500, 100], "not the first session's own clocks"
    time.sleep(0.2)
    assert looked_after(), "the first session's exit did not close the new one"
    m.exit_proc(4545)
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    assert m.wait(lambda: AT.get_state()["games"][0]["sessions"] == 2)
    stop()


def test_the_session_follows_the_tracker_to_the_other_game():
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    m.proc(5151, exe="other.exe")
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 900, "foreground": False,
                       "score": 900})
    m.exit_proc(4242)                               # the first game closes, the second is still running
    assert m.wait(lambda: looked_after("other.exe")), AT._sess
    assert AT._sess["pre"] == [100, 500, 100] and AT._sess["pid"] == 5151
    assert mine() == [("apply", 450, 2225), ("apply", 100, 500), ("apply", 450, 2225)]
    assert AT.get_state()["history"] == []
    assert [g["sessions"] for g in AT.get_state()["games"] if g["exe"] == "game.exe"] == [1], \
        "the first game's session ended as an exit"
    m.exit_proc(5151)
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    stop()


def test_a_game_that_closes_on_a_driver_error_while_another_game_has_a_window_is_judged():
    """The tracker moved to the other game and released the first one's
    session, which judges nothing: the driver error that closed the game was
    never looked at (no line, no count), however often it happened."""
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    m.proc(5151, exe="other.exe")
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 900, "foreground": False,
                       "score": 900})
    m.EVENTS.add("driver_error", time.time())       # the driver fails ...
    m.exit_proc(4242)                               # ... and the game closes on it
    assert m.wait(lambda: looked_after("other.exe")), AT._sess
    history = [h["text"] for h in AT.get_state()["history"]]
    assert history == [P.W_ENDED("Game")], history
    assert mine() == [("apply", 450, 2225), ("apply", 100, 500), ("apply", 450, 2225)], \
        "handed back before the other game's session read the card"
    m.exit_proc(5151)
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    stop()


def test_a_game_relaunched_inside_the_grace_is_judged_as_closed_and_carried_on():
    """It closed on a driver error and was started again before game mode's
    grace ran out: game mode stayed on and told Adaptive Tuning nothing, so
    the error was later judged as one the game "kept running" through."""
    gm_fresh(adaptive=True)
    assert launch()
    assert m.wait(looked_after)
    first = AT._sess["id"]
    fast = GP.time

    class _SlowerClock(_FastClock):
        def sleep(self, seconds):
            time.sleep(0.2)                         # (the grace is 4 ticks: long enough to come back in)

    GP.time = _SlowerClock()
    try:
        m.EVENTS.add("driver_error", time.time())
        m.exit_proc(4242)
        assert m.wait(lambda: GP._engine["exit_ticks"] >= 1)
        m.proc(4545, exe="game.exe")                # back within the grace
        assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    finally:
        GP.time = fast
    assert m.wait(lambda: AT.get_state()["history"]), "judged now, not two minutes later"
    assert [h["text"] for h in AT.get_state()["history"]] == [P.W_ENDED("Game")]
    assert AT._sess is not None and AT._sess["id"] == first and AT._sess["pid"] == 4545, "the session carried on"
    assert card[0] == FULL and mine() == [("apply", 450, 2225)], "nothing handed back under the running game"
    assert GP._engine["gaming_mode"]
    m.exit_proc(4545)
    assert m.wait(lambda: AT._sess is None and card[0] == BEFORE)
    stop()
