import sandbox as m
# Game mode and Adaptive Tuning under awkward timing: a session replaced
# before it was closed, a game relaunched before its exit was handled, a
# tuning loop still mid-poll when its session ends, a temperature-ceiling
# reset, a mode changed mid-game, a game that restarts into a new process, a
# pid that belongs to another process by the time the game closes, and the
# start-up check meeting values the optimizer set on purpose.  Nothing here
# touches a real clock: the NVIDIA interface is a stand-in and apply_oc()
# only records.

import os
import threading
import time

from core import reg_originals as RO, process_tuning as PT

GP = m.app("core.game_profiles")
OPT = m.app("core.optimizer")
ST = m.app("core.scheduler_tweaks")
from core import adaptive_tuning as AT, gpu_overclock as GO, perf_monitor as PM          # noqa: E402
from core import frame_telemetry as FT, gameplay_state as GS, temp_ceiling as TC        # noqa: E402
from core import background_pauser as BPZ                                                # noqa: E402

PRIO_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
HIGH, NORMAL, ABOVE = PT.HIGH_PRIORITY_CLASS, PT.NORMAL_PRIORITY_CLASS, PT.ABOVE_NORMAL_PRIORITY_CLASS
EXE = "testgame.exe"

# ── everything around Adaptive Tuning that is not under test ──────────
applied = {"core": 100, "mem": 500, "power": 100, "active": True}     # what the app holds on the card
oc_calls: list = []
tracking: list = []
targets: list = []
info = [{"exe": EXE, "display": "Test Game", "pid": 4242, "oc_core": 0, "oc_mem": 0}]
score = [{"score": 90, "verdict": "healthy", "last_util": 95}]
score_delay = [0.005]                       # one poll of the loop takes at least this
apply_delay = [0.0]


def _apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100):
    time.sleep(apply_delay[0])
    oc_calls.append((int(core_offset_mhz), int(mem_offset_mhz)))
    applied.update(core=int(core_offset_mhz), mem=int(mem_offset_mhz))
    return {"ok": True}


def _score(seconds=30):
    time.sleep(score_delay[0])
    return dict(score[0])


GO.apply_oc = _apply_oc
GO.get_applied_oc = lambda: dict(applied)
GO.load_profile = lambda: {"exists": True, "core_offset_mhz": 100, "mem_offset_mhz": 500}
GO.detect_gpu_oc_capability = lambda: {"ok": True, "vendor": "nvidia", "limits": {"power_max_w": 300}}
PM.start = lambda consumer=None: {"ok": True}
PM.stop = lambda consumer=None, force=False: {"ok": True}
PM.get_status = lambda: {"last": {"temp": 60, "power": 100.0}}
PM.compute_stability_score = _score
FT.start_tracking = lambda pid=None, exe_name=None: tracking.append(("start", pid)) or {"ok": True}
FT.stop_tracking = lambda: tracking.append(("stop",)) or {"ok": True}
FT.get_recent_stats = lambda seconds=30: {"available": False}
GS.set_target = lambda exe, pid: targets.append((exe, pid))
GS.observe = lambda exe, **k: {"state": "gameplay", "confidence": 1.0, "reasons": [], "baseline": {}}
GP.get_active_game_info = lambda: dict(info[0]) if info[0] else None
GP._notifier = None
BPZ.get_settings = lambda: {"enabled": False, "auto_resume_on_game_exit": True}
BPZ.resume_now = lambda reason="": {"resumed": []}
AT._target_process_alive = lambda exe: True
REAL_LOOP = AT._tuning_loop


def idle_loop(exe, name, stop_event):
    stop_event.wait(30)


def at_fresh(loop=idle_loop):
    """A clean machine, AT switched on for every game, nothing engaged."""
    if AT._runtime.get("active_exe"):
        AT.end_active_session("test reset")
    m.fresh()
    AT._save_json(AT.SETTINGS_PATH, {"enabled_globally": True, "default_per_game_enabled": True})
    applied.update(core=100, mem=500, power=100, active=True)
    del oc_calls[:], tracking[:], targets[:]
    info[0] = {"exe": EXE, "display": "Test Game", "pid": 4242, "oc_core": 0, "oc_mem": 0}
    score[0] = {"score": 90, "verdict": "healthy", "last_util": 95}
    score_delay[0] = 0.005
    apply_delay[0] = 0.0
    AT._tuning_loop = loop
    AT._runtime.update(active_exe=None, loop_stop=None, loop_thread=None, pre_engage_oc=None, target_pid=None)


def climb_to(core, mem):
    """What a tuning loop does on a step-up: the card, the runtime, the record."""
    _apply_oc(core, mem)
    AT._runtime.update(current_core=core, current_mem=mem)
    st = AT.get_game_state(EXE)
    st.update(current_core=core, current_mem=mem)
    AT._save_game_state(EXE, st)


# ═════════════════════════════════════════════════════════════════════
# Adaptive Tuning: session starts and ends
# ═════════════════════════════════════════════════════════════════════
def test_a_session_still_open_keeps_what_was_on_the_card_before_it():
    """Game mode put normal mode back mid-game (the tray's Force normal) and
    detected the game again at its next tick: AT was engaged a second time
    without the first session being closed, and took its own climbed offset
    for "what was on the card before AT" - which the game's exit then put
    back, for the stock watchdog to keep on the desktop."""
    at_fresh()
    assert AT.on_game_start(EXE, "Test Game")["engaged"]
    first = AT._runtime["loop_stop"]
    assert AT._runtime["pre_engage_oc"]["core"] == 100
    climb_to(150, 700)
    assert AT.on_game_start(EXE, "Test Game")["engaged"]        # detected again, nothing closed in between
    assert first.is_set(), "the first session's loop is told to stop"
    assert AT._runtime["pre_engage_oc"]["core"] == 100 and AT._runtime["pre_engage_oc"]["mem"] == 500
    climb_to(150, 700)
    del oc_calls[:]
    AT.on_game_exit(EXE, "Test Game")
    assert oc_calls == [(100, 500)], oc_calls
    assert (applied["core"], applied["mem"]) == (100, 500), "the card is handed back as AT found it"


def test_a_late_exit_leaves_the_relaunched_game_running():
    """A game relaunched right after a crash: its new session started before
    the crash check behind the old session's exit was done, and that exit
    then ended the new session instead."""
    at_fresh()
    assert AT.on_game_start(EXE, "Test Game")["engaged"]
    exited = time.time()
    time.sleep(0.02)
    assert AT.on_game_start(EXE, "Test Game")["engaged"]        # the relaunch
    new = AT._runtime["loop_stop"]
    r = AT.on_game_exit(EXE, "Test Game", was_crash=True, exited_at=exited)
    assert r["updated"] is False
    assert AT._runtime["active_exe"] == EXE and AT._runtime["loop_stop"] is new and not new.is_set()
    r = AT.on_game_exit(EXE, "Test Game", exited_at=time.time())     # its own exit later
    assert r["updated"] is True and new.is_set() and AT._runtime["active_exe"] is None


def test_a_session_is_closed_once():
    """The app quitting while the post-exit chain was closing the session:
    both closed it, and its minutes and stable sessions were counted twice."""
    at_fresh()
    assert AT.on_game_start(EXE, "Test Game")["engaged"]
    climb_to(150, 700)
    before = AT.get_game_state(EXE)["session_count"]
    del oc_calls[:]
    apply_delay[0] = 0.2                            # handing the card back takes a while
    threads = [threading.Thread(target=AT.on_game_exit, args=(EXE, "Test Game")),
               threading.Thread(target=AT.end_active_session, args=("user quit",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert AT.get_game_state(EXE)["session_count"] == before + 1
    assert oc_calls.count((100, 500)) == 1


def test_a_loop_whose_session_has_ended_writes_nothing():
    """on_game_exit put the card back while a tuning loop was mid-poll: the
    loop's step (or drift re-assert) landed afterwards and stayed."""
    at_fresh()
    ev = threading.Event()
    result = []
    with AT._session_lock:                          # on_game_exit under way
        t = threading.Thread(target=lambda: result.append(AT._loop_apply(ev, EXE, 200, 900, "step-up")))
        t.start()
        time.sleep(0.05)
        ev.set()                                    # ...it stops the loop, then hands the card back
    t.join(5)
    assert result == [False] and (200, 900) not in oc_calls
    assert AT._loop_apply(threading.Event(), EXE, 200, 900, "step-up") is True and oc_calls[-1] == (200, 900)


class FakeNvapi:
    def __init__(self, core=0, mem=0):
        self.now, self.writes = (core, mem), []

    def is_available(self):
        return {"ok": True}

    def get_current_offsets(self):
        return {"ok": True, "core_offset_mhz": self.now[0], "mem_offset_mhz": self.now[1]}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        return {"ok": True}


def test_the_drift_check_leaves_a_temperature_ceiling_reset_alone():
    """The ceiling reset the card to stock and cannot trip again during its
    cooldown; AT's drift check put the overclock straight back."""
    at_fresh()
    real = GO._nvapi
    GO._nvapi = nv = FakeNvapi(0, 0)                # stock: the ceiling has just reset it
    try:
        TC._state["in_cooldown_until"] = time.time() + 60
        r = AT._at_check_offset_drift(EXE, 150, 700)
        assert r["drifted"] and not r["reapplied"] and nv.writes == []
        TC._state["in_cooldown_until"] = None       # cooldown over: AT may carry on
        r = AT._at_check_offset_drift(EXE, 150, 700)
        assert r["reapplied"] and nv.writes == [(150, 700)]
    finally:
        GO._nvapi = real
        TC._state["in_cooldown_until"] = None


# ═════════════════════════════════════════════════════════════════════
# Adaptive Tuning: the loop itself, at full speed
# ═════════════════════════════════════════════════════════════════════
FAST = {"warmup_seconds": 0, "score_poll_interval_s": 0, "consecutive_good_required": 1}


class FastLoop:
    """The real tuning loop with no warm-up, no start-up guard and no sleep
    between polls."""

    def __enter__(self):
        self.saved = ({k: dict(v) for k, v in AT.MODE_PRESETS.items()}, AT.STARTUP_GUARD_SEC, AT.OPENING_PHASE_SEC)
        for preset in AT.MODE_PRESETS.values():
            preset.update(FAST)
        AT.STARTUP_GUARD_SEC = AT.OPENING_PHASE_SEC = 0
        at_fresh(loop=REAL_LOOP)
        return self

    def __exit__(self, *exc):
        t = AT._runtime.get("loop_thread")
        AT.end_active_session("test over")
        if t:
            t.join(5)
        presets, AT.STARTUP_GUARD_SEC, AT.OPENING_PHASE_SEC = self.saved
        for k, v in presets.items():
            AT.MODE_PRESETS[k].clear()
            AT.MODE_PRESETS[k].update(v)
        AT._tuning_loop = idle_loop


def steps_up():
    return [c for c in oc_calls if c[0] > 100 or c[1] > 500]


def test_a_mode_changed_mid_game_applies_at_the_next_poll():
    """set_game_mode() says the running loop picks the new mode up at its
    next poll, and tournament mode relies on that; the loop read the preset
    once, so it went on stepping at the old mode's thresholds."""
    with FastLoop():
        for preset in AT.MODE_PRESETS.values():
            preset.update(max_core_offset=800, max_mem_offset=3000)     # (room to climb all test long)
        AT.MODE_PRESETS["competitive"]["min_score_to_step_up"] = 95       # 90 is not enough here
        score_delay[0] = 0.04
        assert AT.on_game_start(EXE, "Test Game")["engaged"]
        assert m.wait(lambda: len(steps_up()) >= 2), "balanced: 90 is a healthy score, it steps up"
        AT.set_game_mode(EXE, "competitive")
        time.sleep(0.1)                             # (a poll already past the check may still finish)
        n = len(steps_up())
        time.sleep(0.5)
        assert len(steps_up()) == n, "no step-up below the competitive threshold"
        assert AT._runtime["loop_thread"].is_alive()


def test_telemetry_follows_the_game_to_its_new_process():
    """Some games restart into a new process at launch (an anti-cheat
    bootstrap); game mode follows them.  AT's classifier stayed on the old
    PID - the game never counted as in the foreground, so no decision was
    made all session - and PresentMon watched a process that was gone."""
    with FastLoop():
        score[0] = {"score": None, "verdict": "idle", "last_util": 0}
        assert AT.on_game_start(EXE, "Test Game")["engaged"]
        assert targets[-1] == (EXE, 4242) and tracking[-1] == ("start", 4242)
        info[0] = dict(info[0], pid=4545)          # game mode now follows PID 4545
        assert m.wait(lambda: targets[-1] == (EXE, 4545))
        assert m.wait(lambda: tracking[-2:] == [("stop",), ("start", 4545)]), tracking
        n = len(tracking)
        time.sleep(0.2)
        assert len(tracking) == n, "once, not at every poll"


def learned(core, mem):
    """An offset an earlier session climbed to, above the 100/500 baseline."""
    st = AT.get_game_state(EXE)
    st.update(current_core=core, current_mem=mem)
    AT._save_game_state(EXE, st)


def test_no_jump_to_the_learned_offset_while_at_is_paused():
    """A recoil capture or latency measurement pauses AT so the clocks hold
    still; the jump after the start-up guard changed them anyway."""
    with FastLoop():
        AT.STARTUP_GUARD_SEC = 0.3
        score[0] = {"score": None, "verdict": "idle", "last_util": 0}
        learned(150, 700)
        assert AT.on_game_start(EXE, "Test Game")["climb_target_core"] == 150
        AT.pause("capture")                         # starts during the guard
        try:
            assert m.wait(lambda: (AT._runtime.get("last_reason") or "").startswith("paused"))
            assert (150, 700) not in oc_calls and oc_calls[-1] == (100, 500)
        finally:
            AT.resume("capture")


def test_no_jump_to_the_learned_offset_once_at_is_switched_off():
    with FastLoop():
        AT.STARTUP_GUARD_SEC = 0.3
        learned(150, 700)
        assert AT.on_game_start(EXE, "Test Game")["climb_target_core"] == 150
        AT.set_settings(enabled_globally=False)     # switched off during the guard
        assert m.wait(lambda: AT._runtime["active_exe"] is None), "the session ends"
        assert (150, 700) not in oc_calls, "the clocks were changed after AT had been switched off"


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


def test_the_cross_game_blacklist_keeps_the_newest_entries():
    m.fresh()
    AT._save_cross_game_blacklist([{"core": i, "mem": 0, "ts": i, "source_exe": "", "reason": ""}
                                   for i in range(500)])
    AT.add_cross_game_blacklist(900, 3000, source_exe=EXE)
    assert AT._is_cross_game_blacklisted(900, 3000), "the newest crash used to be the one dropped"
    assert len(AT.get_cross_game_blacklist()) == 500 and not AT._is_cross_game_blacklisted(0, 0)


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


def gm_fresh():
    GP.stop_monitoring()
    time.sleep(0.12)                                # let a running loop notice and leave
    if AT._runtime.get("active_exe"):
        AT.end_active_session("test reset")
    m.fresh(plan=m.BALANCED)
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
    AT.set_game_target("game.exe", "max_fps")
    m.PLAN["active"] = m.BALANCED                   # Idle Mode engaged: Balanced, the user's plan on record
    IM._state.update(engaged=True, restore={"power_guid": m.USERPLAN, "oc": None})
    assert launch()
    assert IM._state["engaged"] is False
    assert GP._load_target_restore()["power_guid"] == m.USERPLAN
    m.exit_proc(4242)
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert m.PLAN["active"] == m.USERPLAN, "Balanced was put back as the user's plan"
    stop()
