"""Game mode (core/game_profiles.py, core/process_tuning.py): what is applied
when a game starts, on which processes and which power plan, that it stays
applied while the game runs, that the status says what really is in effect,
and that all of it is put back when the game closes - or at the next start,
after a crash.

The monitor loop runs for real, on a fake machine: processes are entries in
the fake kernel32, and a "game" is whatever _scan_game_candidates returns.
"""
import sandbox as m

import json
import os
import re
import threading
import time

from core import reg_originals as RO, process_tuning as PT

GP = m.app("core.game_profiles")
OPT = m.app("core.optimizer")
CL = m.app("core.competitive_latency")
from core import gpu_overclock as GO, adaptive_tuning as AT                        # noqa: E402
from core import crash_recovery as CR, background_pauser as BPZ                   # noqa: E402
from core import frame_telemetry as FT, library_scanner as LS, idle_mode as IM    # noqa: E402

XH = m.app("core.crosshair")

PRIO_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
PROFILE_KEY = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
STOCK = {"idle": [0, 0], "park_e": [100, 10], "park_p": [0, 5]}     # like the plan on a Core Ultra 285K
HIGH, NORMAL = PT.HIGH_PRIORITY_CLASS, PT.NORMAL_PRIORITY_CLASS

# ── everything around the engine that is not under test ───────────────
cl_calls: list = []
oc_calls: list = []         # every overclock anybody asks gpu_overclock to apply
ft_calls: list = []         # frame telemetry started and stopped
at_starts: list = []        # what Adaptive Tuning is told at a game start: (exe, display name, pid)
at_exits: list = []         # ...at a game exit: (exe, exit_ts)
at_ends: list = []          # ...when game mode ends without the game closing: the reason
AT_START = [{"engaged": False, "reason": "off"}]     # what it answers at a start
AT_EXIT = [{"handled": False}]                       # ...and at an exit
order: list = []            # the order of the steps a test asks about
cr_calls: list = []         # what crash_recovery is told at a game exit
live_reads: list = []       # the clock read-outs the "captured overclock" used to be worked out from
refreshes: list = []
candidates: list = []
CROSSHAIR = [False]         # is the crosshair switched on
NOT_HELD = {"core": 0, "mem": 0, "power": 100, "active": False}
SAVED = {"exists": True, "core_offset_mhz": 450, "mem_offset_mhz": 2225, "power_pct": 100, "apply_on_startup": False}
APPLIED = [dict(NOT_HELD)]  # what the app holds on the card
PROFILE = [{"exists": False}]
REAL_SCAN = GP._scan_game_candidates
REAL_STREAMERS = GP._find_running_streaming_exes
REAL_REFRESH = GP._post_game_refresh
REAL_EXTRAS = GP._apply_session_extras
GP._notifier = None
GP._post_game_refresh = lambda name=None, pids=None: refreshes.append((name, set(pids or ())))
GP._find_running_streaming_exes = lambda: []
GP._scan_game_candidates = lambda: [c for c in candidates
                                    if c["pid"] in m.ALIVE and c["pid"] != GP._engine.get("active_pid")]
GO.read_live_state = lambda: live_reads.append("live") or {"ok": False}
GO.get_stock_baseline = lambda: live_reads.append("stock") or {}
GO.load_profile = lambda: dict(PROFILE[0])
GO.get_applied_oc = lambda: dict(APPLIED[0])
GO.apply_oc = lambda *a, **k: oc_calls.append(k) or {"ok": True}


def _at_start(exe, display_name=None, pid=None):
    order.append("adaptive tuning start")
    at_starts.append((exe, display_name, pid))
    return dict(AT_START[0])


def _at_exit(exe, exit_ts=None):
    order.append("adaptive tuning exit")
    at_exits.append((exe, exit_ts))
    return dict(AT_EXIT[0])


AT.on_game_start = _at_start
AT.on_game_exit = _at_exit
AT.end_active_session = lambda reason="": (order.append("adaptive tuning release") or at_ends.append(reason)
                                            or {"ended": False, "reason": "no active session"})
FT.start_tracking = lambda pid=None, exe_name=None: ft_calls.append(("start", pid, exe_name)) or {"ok": True}
FT.stop_tracking = lambda: ft_calls.append(("stop",)) or {"ok": True}
XH.is_enabled = lambda: CROSSHAIR[0]
CR.handle_game_exit = lambda **k: order.append("crash recovery") or cr_calls.append(k) or {"crashed": False}
CL.status = lambda: {"enabled": False}
CL.apply = lambda: cl_calls.append("apply") or {"ok": True}
CL.reset = lambda: cl_calls.append("reset") or {"ok": True}
BPZ.get_settings = lambda: {"enabled": False, "auto_resume_on_game_exit": True}
BPZ.resume_now = lambda reason="": {"resumed": []}


class _FastClock:
    """The monitor sleeps 3 s between ticks; here it sleeps 20 ms."""

    def __getattr__(self, name):
        return getattr(time, name)

    delay = 0.02

    def sleep(self, seconds):
        time.sleep(self.delay)


GP.time = _FastClock()


def qos_ok(command):
    """PowerShell creating the network rules: answers the way the real script does."""
    if "New-NetQosPolicy" in command:
        return m.R("\n".join(f"OK {n}" for n in re.findall(r"New-NetQosPolicy -Name '([^']+)'", command)))
    return None


def fresh(plan=m.BALANCED):
    GP.stop_monitoring()
    time.sleep(0.12)                                # let a running loop notice and leave
    m.fresh(plan=plan)
    os.makedirs(GP.PROFILES_DIR, exist_ok=True)
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x26)       # this user's own values
    m.rset(PROFILE_KEY, "SystemResponsiveness", 20)
    m.rset(PROFILE_KEY, "NetworkThrottlingIndex", 10)
    del candidates[:], cl_calls[:], oc_calls[:], refreshes[:]
    del ft_calls[:], at_starts[:], at_exits[:], at_ends[:], cr_calls[:], live_reads[:], order[:]
    AT_START[0], AT_EXIT[0] = {"engaged": False, "reason": "off"}, {"handled": False}
    CROSSHAIR[0] = False
    APPLIED[0], PROFILE[0] = dict(NOT_HELD), {"exists": False}
    GP._engine.update(gaming_mode=False, active_game=None, active_exe=None, active_pid=None, changes=[],
                      proc_prior=None, game_procs={}, checks=[], exit_ticks=0, streamer_prior={},
                      eco_on=False, eco_seen={}, eco_procs={}, eco_failed=0, frames_pid=None, held_off={})
    GP._names_cache.clear()
    m.CPU_LAYOUT.update(logical=24, p=8, e=16, hybrid=True)
    GP._target_restore = {}
    GP._find_running_streaming_exes = lambda: []
    GP._apply_session_extras = REAL_EXTRAS


def launch(pid=4242, exe="game.exe", **process):
    m.proc(pid, exe=exe, **process)
    candidates.append({"pid": pid, "exe": exe, "name": "Game", "ws_mb": 900, "foreground": True, "score": 1900})
    GP.start_monitoring()
    return m.wait(lambda: GP._engine["gaming_mode"] and any("CPU idle" in c for c in GP._engine["changes"]))


def quit_game(pid=4242):
    m.exit_proc(pid)
    return m.wait(lambda: not GP._engine["gaming_mode"])


def plan_vals(scheme):
    return {"idle": list(m.cell(scheme, m.IDLE)), "park_e": list(m.cell(scheme, m.PARK_E)),
            "park_p": list(m.cell(scheme, m.PARK_P))}


def reg_now():
    return (m.rdata(PRIO_KEY, "Win32PrioritySeparation"), m.rdata(PROFILE_KEY, "SystemResponsiveness"),
            m.rdata(PROFILE_KEY, "NetworkThrottlingIndex"))


HINT = " (Settings → Game mode can have Windows start it at High instead)"


def check(key):
    return next((c for c in GP._engine["checks"] if c["key"] == key), None)


def stop():
    GP.stop_monitoring()
    m.wait(lambda: not GP._engine["gaming_mode"])


class Meddler:
    """Something else on the PC (the game, its launcher, its anti-cheat)
    setting the game's process back, over and over."""

    def __init__(self, pid, **values):
        self.pid, self.values, self.go = pid, values, True

    def __enter__(self):
        def run():
            while self.go:
                m.K.procs[self.pid].update(self.values)
                time.sleep(0.003)
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.go = False
        self.t.join(1)


# ═════════════════════════════════════════════════════════════════════
# A game its launcher left on 8 of 24 cores
# ═════════════════════════════════════════════════════════════════════
def test_game_gets_high_priority_every_core_and_no_throttling():
    fresh()
    assert launch(affinity=0xFF), GP._engine
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["priority"] == HIGH and "Process priority -> High" in changes
    assert p["affinity"] == m.SYSTEM_MASK, "bound to ALL cores, P and E: the 8-core restriction is lifted"
    assert p.get("cpusets_cleared") is True
    assert "CPU affinity -> all 24 cores (8 P + 16 E) (was limited to 8)" in changes, changes
    assert p.get("throttle") == (5, 0), "exempt from power throttling, timer requests honoured"
    assert "Power throttling -> off for the game" in changes
    assert all(c["ok"] for c in GP._engine["checks"] if c["key"].startswith("proc:"))
    stop()


def test_no_core_is_parked_and_idle_is_off_on_the_active_plan():
    fresh()
    assert launch()
    changes = GP._engine["changes"]
    assert plan_vals(m.BALANCED) == {"idle": [1, 0], "park_e": [100, 10], "park_p": [100, 5]}
    assert "Core parking -> off (every core kept awake)" in changes and "CPU idle -> disabled" in changes
    assert RO.summary()["power"] == 2, "the two that changed are on record (the E-cores were at 100 already)"
    assert RO.has_power("SUB_PROCESSOR", "IdleDisable", "gamemode"), "...under the game-mode scope"
    assert not RO.has_power("SUB_PROCESSOR", "IdleDisable")
    assert reg_now() == (40, 10, 4294967295), "the registry half is applied as before"
    assert RO.has_original(PRIO_KEY, "Win32PrioritySeparation", scope="gamemode"), \
        "and now on record too: the exit puts back exactly what was there"
    stop()


def test_a_process_that_keeps_its_settings_is_not_touched_again():
    fresh()
    assert launch(affinity=0xFF)
    time.sleep(0.3)                                 # a dozen ticks
    assert len([c for c in m.K.calls if c[0] == "affinity"]) == 1
    assert len([c for c in m.K.calls if c[0] == "priority"]) == 1
    stop()


def test_everything_is_put_back_when_the_game_closes():
    fresh()
    assert launch(affinity=0xFF)
    assert quit_game()
    assert plan_vals(m.BALANCED) == STOCK and m.PLAN["active"] == m.BALANCED
    assert reg_now() == (0x26, 20, 10), "the user's values, not Windows defaults"
    assert RO.summary()["power"] == 0 and RO.summary()["registry"] == 0
    assert not GP._is_gaming_flag_set() and GP._engine["checks"] == []


# ═════════════════════════════════════════════════════════════════════
# What it says is what is in effect
# ═════════════════════════════════════════════════════════════════════
def test_a_setting_windows_refuses_is_reported_as_not_done():
    fresh()
    m.REG_FAIL.add((PRIO_KEY.lower(), "win32priorityseparation"))
    assert launch()
    c = check("reg:Win32PrioritySeparation")
    assert c["ok"] is False and c["text"] == "Win32PrioritySeparation: could not be set"
    assert "Win32PrioritySeparation -> 0x28 (Short/Fixed)" not in GP._engine["changes"], \
        "it used to say this whether the write worked or not"
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 0x26
    stop()


def test_a_call_that_succeeds_without_changing_anything_is_not_reported_as_done():
    fresh()
    GP.set_settings(priority_at_launch=False)             # (the wording without the launch setting)
    assert launch(ignore_priority=True, ignore_affinity=True, affinity=0xFF)
    assert check("proc:priority")["ok"] is False and check("proc:affinity")["ok"] is False
    assert "Process priority -> High" not in GP._engine["changes"]
    assert "on 0 of 1 processes" not in check("proc:priority")["text"], "said 'the others refused it' with no others"
    assert check("proc:priority")["text"] == "Process priority: Windows accepted High but the game set itself back" + HINT
    stop()


def test_status_for_the_ui():
    fresh()
    assert launch()
    s = GP.get_engine_status()
    assert s["gaming_mode"] and s["game_pids"] == [4242] and s["verified_at"] > 0
    assert {c["text"] for c in s["checks"]} == set(s["applied_changes"])
    assert all(set(c) == {"ok", "text"} for c in s["checks"])
    m.exit_proc(4242)
    assert m.wait(lambda: GP.get_engine_status()["waiting_for_restart"] or not GP._engine["gaming_mode"])
    assert m.wait(lambda: not GP._engine["gaming_mode"])


def test_the_log_lists_what_was_not_done():
    fresh()
    m.REG_FAIL.add((PRIO_KEY.lower(), "win32priorityseparation"))
    assert launch()
    from core.utils import get_log_buffer
    text = "\n".join(e["msg"] for e in get_log_buffer())
    assert "game-mode settings in effect (read back)" in text
    assert "not done: Win32PrioritySeparation: could not be set" in text
    stop()


# ═════════════════════════════════════════════════════════════════════
# Keeping it applied while the game runs
# ═════════════════════════════════════════════════════════════════════
def test_priority_the_game_changes_back_is_applied_again():
    fresh()
    assert launch()
    m.K.procs[4242]["priority"] = NORMAL            # the game (or its launcher) resets it
    assert m.wait(lambda: m.K.procs[4242]["priority"] == HIGH), "it used to stay Normal for the whole session"
    assert m.wait(lambda: "re-applied 1x" in check("proc:priority")["text"])
    assert check("proc:priority")["ok"] is True
    stop()
    assert m.K.procs[4242]["priority"] == NORMAL, "and the game's own priority is what is put back"


def test_affinity_the_game_narrows_later_is_widened_again():
    fresh()
    assert launch()
    m.K.procs[4242]["affinity"] = 0xFF              # the game pins itself to the P-cores after start-up
    assert m.wait(lambda: m.K.procs[4242]["affinity"] == m.SYSTEM_MASK)
    assert m.wait(lambda: "re-applied" in check("proc:affinity")["text"])
    GP.stop_monitoring()
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert m.K.procs[4242]["affinity"] == 0xFF, "switched off mid-game: the game's own choice comes back"


def test_power_throttling_the_game_opts_back_into_is_switched_off_again():
    fresh()
    assert launch()
    m.K.procs[4242]["throttle"] = (1, 1)            # EcoQoS back on
    assert m.wait(lambda: m.K.procs[4242]["throttle"] == (5, 0))
    stop()


def test_a_game_that_keeps_resetting_itself_is_not_fought_forever():
    fresh()
    assert launch()
    with Meddler(4242, priority=NORMAL):
        assert m.wait(lambda: check("proc:priority")["ok"] is False)
        sets = len([c for c in m.K.calls if c[0] == "priority"])
        time.sleep(0.3)
        assert len([c for c in m.K.calls if c[0] == "priority"]) == sets, "no longer forced"
    assert sets <= 1 + GP._REAPPLY_LIMIT
    assert check("proc:priority")["text"] == "Process priority: the game keeps setting its own, so it is no longer forced"
    stop()


def test_registry_value_changed_mid_game_is_put_back():
    fresh()
    assert launch()
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 2)  # something else writes it during the game
    assert m.wait(lambda: m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 40)
    assert "re-applied" in check("reg:Win32PrioritySeparation")["text"]
    assert quit_game()
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 0x26, "the user's own value, not the 2 in between"


def test_a_value_the_optimizer_takes_over_mid_game_is_left_to_it():
    fresh()
    assert launch()
    OPT._reg(PRIO_KEY, "Win32PrioritySeparation", "38")       # the user applies the optimizer during the game
    assert m.wait(lambda: (check("reg:Win32PrioritySeparation") or {}).get("ok") is None)
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 38, "not fought"
    assert quit_game()
    assert m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 38, "and still the optimizer's after the game"


def test_power_setting_changed_mid_game_is_put_back():
    fresh()
    assert launch()
    m.cell(m.BALANCED, m.PARK_P)[0] = 0
    assert m.wait(lambda: m.cell(m.BALANCED, m.PARK_P)[0] == 100)
    assert "re-applied" in check("power:parking")["text"]
    assert quit_game() and plan_vals(m.BALANCED) == STOCK


# ═════════════════════════════════════════════════════════════════════
# Following the game: several processes, restarts, false exits
# ═════════════════════════════════════════════════════════════════════
def test_every_process_of_the_game_is_tuned():
    fresh()
    m.proc(4343, exe="game.exe", affinity=0xF)      # a second process of the same exe
    assert launch(affinity=0xFF)
    assert m.K.procs[4242]["priority"] == HIGH and m.K.procs[4343]["priority"] == HIGH
    assert m.K.procs[4343]["affinity"] == m.SYSTEM_MASK
    assert "Process priority -> High (2 processes)" in GP._engine["changes"]
    assert GP.get_engine_status()["game_pids"] == [4242, 4343]
    stop()


def test_a_process_of_another_exe_is_not_tuned():
    fresh()
    m.proc(5000, exe="launcher.exe")
    m.proc(5001, exe="game.exe", image=r"D:\Other\game.exe")      # same name, different program
    assert launch()
    assert m.K.procs[5000]["priority"] == NORMAL and m.K.procs[5001]["priority"] == NORMAL
    stop()


def test_a_game_that_restarts_itself_keeps_game_mode():
    fresh()
    assert launch()
    GP.time.delay = 0.1                             # (slower ticks, so the gap below is a real one)
    try:
        m.exit_proc(4242)                           # it closes...
        assert m.wait(lambda: GP._engine["exit_ticks"] >= 2), "the monitor has seen it gone"
        m.proc(4545, exe="game.exe")                # ...and starts again (an update, an anti-cheat handshake)
        assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    finally:
        GP.time.delay = 0.02
    assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    assert m.wait(lambda: m.K.procs[4545]["priority"] == HIGH), "the new process gets the profile at once"
    assert GP._engine["gaming_mode"] and refreshes == [], "normal mode did not come back in between"
    assert reg_now() == (40, 10, 4294967295)
    assert quit_game(4545)
    assert reg_now() == (0x26, 20, 10) and refreshes and refreshes[0][1] >= {4545}


def test_a_running_game_is_not_taken_for_closed_when_powershell_hangs():
    """Under load the old PowerShell check took over five seconds now and
    then; the timeout was read as "the game has exited", normal mode came
    back mid-game, and the profile was applied again a minute later."""
    fresh()
    assert launch()
    m.PS_HOOKS.append(lambda command: m.F("Timed out"))
    time.sleep(0.5)                                 # ~25 ticks
    assert GP._engine["gaming_mode"] and GP._engine["active_pid"] == 4242 and refreshes == []
    assert reg_now() == (40, 10, 4294967295)
    m.PS_HOOKS.clear()
    stop()


def test_a_protected_game_is_still_followed_through_a_query_only_handle():
    fresh()
    assert launch(protected=True)
    time.sleep(0.3)
    assert GP._engine["gaming_mode"] and refreshes == []
    assert quit_game() and refreshes


def test_a_reused_pid_is_not_taken_for_the_game():
    fresh()
    assert launch()
    m.exit_proc(4242)
    m.K.procs[4242] = dict(priority=NORMAL, affinity=m.SYSTEM_MASK, exe="notepad.exe", created=1)   # pid reused
    m.ALIVE.add(4242)
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert m.K.procs[4242]["priority"] == NORMAL, "the other program was not touched"


def test_monitor_restarted_during_a_session_start_does_not_start_a_second_one():
    """Restarting the monitor (adding a custom game does) while a session was
    being set up used to let the new thread capture its "normal" baseline
    with the gaming values in place — and restore those after every game."""
    fresh()
    captured = []
    real_capture = GP._capture_baseline
    GP._capture_baseline = lambda: captured.append(1) or real_capture()

    def slow_extras(exe):
        time.sleep(0.3)
        REAL_EXTRAS(exe)

    GP._apply_session_extras = slow_extras
    try:
        m.proc(4242, exe="game.exe")
        candidates.append({"pid": 4242, "exe": "game.exe", "name": "Game", "ws_mb": 900, "foreground": True,
                           "score": 1900})
        GP.start_monitoring()
        assert m.wait(lambda: GP._engine["gaming_mode"])            # set at once, before the slow part
        GP.stop_monitoring()
        GP.start_monitoring()                                       # mid-start, as adding a custom game does
        assert m.wait(lambda: any("CPU idle" in c for c in GP._engine["changes"]))
        time.sleep(0.2)
        assert captured == [1], "one baseline, taken before anything was changed"
        assert len([c for c in m.K.calls if c[0] == "priority"]) == 1, "one session, not two"
        assert GP._load_baseline()["priority_sep"] == "26"
    finally:
        GP._capture_baseline = real_capture
    stop()


# ═════════════════════════════════════════════════════════════════════
# Per-game targets
# ═════════════════════════════════════════════════════════════════════
MAX_FPS_GAME, LOW_LATENCY_GAME = "spider-man2.exe", "cod.exe"       # from game mode's own table


def test_max_fps_tweaks_land_on_the_plan_the_game_runs_on():
    fresh()
    PROFILE[0] = dict(SAVED)                        # an overclock is saved; it is not on the card
    assert launch(exe=MAX_FPS_GAME)
    clone = m.PLAN["active"]
    assert clone not in (m.BALANCED, m.HIGH_PERF), "switched to the Ultimate Performance copy"
    assert plan_vals(clone)["idle"][0] == 1 and plan_vals(clone)["park_p"][0] == 100
    assert plan_vals(m.BALANCED) == STOCK, "they used to go to the plan it had just left"
    assert oc_calls == [], "the saved overclock at 100% power used to be put on the card from here"
    assert quit_game()
    assert m.PLAN["active"] == m.BALANCED, "back on the user's plan"
    assert plan_vals(clone) == STOCK, "the performance plan is left as it was found, not with idle off for good"
    assert plan_vals(m.BALANCED) == STOCK and RO.summary()["power"] == 0
    assert oc_calls == [], "and again at exit, where it stayed on for a user who does not apply it at startup"


def test_low_latency_target_engages_competitive_latency_for_the_session():
    fresh()
    assert launch(exe=LOW_LATENCY_GAME)
    assert cl_calls == ["apply"] and plan_vals(m.BALANCED)["idle"][0] == 1
    assert quit_game()
    assert cl_calls == ["apply", "reset"] and plan_vals(m.BALANCED) == STOCK
    assert oc_calls == []


def test_per_game_posture_survives_an_app_restart():
    fresh()
    PROFILE[0] = dict(SAVED)
    assert launch(exe=MAX_FPS_GAME)
    assert os.path.exists(GP._TARGET_RESTORE_FILE), "on disk before anything changes"
    saved = GP._load_target_restore()
    assert saved["target"] == "max_fps" and saved["power_guid"] == m.BALANCED
    assert "oc" not in saved, "nothing about the overclock is on record: it is not game mode's to put back"
    GP._engine["monitor_gen"] += 1                  # the app dies: no clean-up runs
    GP._engine["monitoring"] = False
    time.sleep(0.15)
    GP._engine.update(gaming_mode=False, active_pid=None, active_game=None, active_exe=None, changes=[],
                      proc_prior=None, game_procs={}, checks=[])
    GP._target_restore = {}
    RO._cache = None
    del oc_calls[:]
    GP.check_and_restore_on_boot()                  # next start
    assert m.PLAN["active"] == m.BALANCED, "the plan the game left is returned to"
    assert not os.path.exists(GP._TARGET_RESTORE_FILE)
    assert oc_calls == [], "no overclock is applied from here"


def test_the_target_is_game_modes_own_table_and_the_tuner_is_not_asked():
    assert GP.AUTO_TARGET == {
        "spider-man.exe": "max_fps", "milesmorales.exe": "max_fps", "spider-man2.exe": "max_fps",
        "marvel-spiderman.exe": "max_fps", "spiderman2.exe": "max_fps",
        "modernwarfare.exe": "low_latency", "blackopscoldwar.exe": "low_latency", "vanguard.exe": "low_latency",
        "cod22-cod.exe": "low_latency", "cod23-cod.exe": "low_latency", "cod.exe": "low_latency",
    }, "the table as the old tuner had it"
    assert GP._target_for(" COD.exe ") == "low_latency" and GP._target_for("Spider-Man2.exe") == "max_fps"
    assert GP._target_for("game.exe") == "auto" and GP._target_for(None) == "auto"
    fresh()                                         # a game with no entry in the table: no posture
    assert launch()
    assert m.PLAN["active"] == m.BALANCED and cl_calls == [] and not os.path.exists(GP._TARGET_RESTORE_FILE)
    assert quit_game()
    assert not hasattr(AT, "get_game_state"), "a target set in the old tuner ended with it: there is no state to ask"
    assert oc_calls == []


def test_a_posture_left_on_disk_by_3_7_0_is_undone_without_writing_the_clocks():
    """3.7.0's max_fps target also held the saved overclock ("oc" in its
    record) and wrote the profile back when the game closed.  The record of a
    session cut short by the update is still undone: the power plan goes back,
    the clocks are not written."""
    fresh()
    PROFILE[0] = dict(SAVED)
    old = {"target": "max_fps", "exe": "spider-man2.exe", "power_guid": m.BALANCED,
           "oc": {"core": 450, "mem": 2225, "power": 100}}
    m.PLAN["active"] = m.HIGH_PERF                  # where that session left the PC
    GP.atomic_write_json(GP._TARGET_RESTORE_FILE, old)
    GP.check_and_restore_on_boot()                  # the first start after the update
    assert m.PLAN["active"] == m.BALANCED and not os.path.exists(GP._TARGET_RESTORE_FILE)
    assert oc_calls == []
    GP._target_restore = dict(old)                  # the same record in memory: 3.7.0 wrote the profile here
    m.PLAN["active"] = m.HIGH_PERF
    GP.restore_target()
    assert m.PLAN["active"] == m.BALANCED and GP._target_restore == {} and oc_calls == []
    GP.atomic_write_json(GP._TARGET_RESTORE_FILE,   # Competitive Latency it switched on for a Call of Duty session
                         {"target": "low_latency", "exe": "cod.exe", "we_enabled_latency": True})
    GP.check_and_restore_on_boot()
    assert cl_calls == ["reset"] and not os.path.exists(GP._TARGET_RESTORE_FILE)
    GP.atomic_write_json(GP._TARGET_RESTORE_FILE, {"target": "balanced", "exe": "game.exe"})   # a user-set target
    GP.check_and_restore_on_boot()
    assert not os.path.exists(GP._TARGET_RESTORE_FILE) and cl_calls == ["reset"] and oc_calls == []


def test_game_mode_has_no_call_that_writes_the_clocks():
    """In a game the clocks are Adaptive Tuning's alone: nothing in game mode
    writes them or the saved profile, and it does not read the tuner's state."""
    with open(GP.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    calls = re.findall(r"\b(apply_oc|write_offsets|reset_oc|emergency_reset_oc|save_profile|delete_profile)\s*\(", src)
    assert calls == [], calls
    assert "get_game_state" not in src


# ═════════════════════════════════════════════════════════════════════
# A process Windows will not let us change (anti-cheat)
# ═════════════════════════════════════════════════════════════════════
def test_protected_game_is_left_alone_and_the_ui_says_so():
    fresh()
    GP.set_settings(priority_at_launch=False)             # (the wording without the launch setting)
    assert launch(protected=True, affinity=0xFF)
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["priority"] == NORMAL and p["affinity"] == 0xFF
    assert "Process priority: unchanged (the game's process is protected)" + HINT in changes
    assert "CPU affinity: unchanged (the game's process is protected)" in changes
    assert not any("Power throttling" in c for c in changes)
    assert check("proc:priority")["ok"] is False
    assert plan_vals(m.BALANCED)["park_p"][0] == 100, "the system-wide half still applies"
    assert quit_game() and plan_vals(m.BALANCED) == STOCK


def test_protected_game_whose_priority_powershell_can_set():
    fresh()

    def powershell_sets_it(command):
        if "PriorityClass='High'" in command:
            m.K.procs[4242]["priority"] = HIGH
            return m.R("OK")
        return None

    m.PS_HOOKS.append(powershell_sets_it)
    assert launch(protected=True)
    assert "Process priority -> High" in GP._engine["changes"]
    stop()


def test_powershell_saying_ok_without_doing_it_is_not_believed():
    fresh()
    GP.set_settings(priority_at_launch=False)             # (the wording without the launch setting)
    m.PS_PRIORITY_OK[0] = True                      # says OK, changes nothing
    assert launch(protected=True)
    assert "Process priority: unchanged (the game's process is protected)" + HINT in GP._engine["changes"]
    stop()


def test_a_protected_process_that_opens_up_later_is_tuned_then():
    fresh()
    assert launch(protected=True)
    m.K.procs[4242]["protected"] = False            # e.g. once the anti-cheat has finished starting
    assert m.wait(lambda: m.K.procs[4242]["priority"] == HIGH)
    assert m.wait(lambda: check("proc:priority")["ok"] is True)
    stop()


# ═════════════════════════════════════════════════════════════════════
# Power plan specifics
# ═════════════════════════════════════════════════════════════════════
def test_a_setting_windows_has_capped_is_reported_and_not_left_failing_on_record():
    """On the developer's PC an old "unpark cores" tweak had set the E-core
    parking setting's maximum to 0: Windows refused 100, and the restore of
    the record failed at every game exit, for good."""
    fresh()
    m.POWER_LIMITS[m.PARK_E] = 0                    # capped some other way than by the old tweak's ValueMax
    m.cell(m.BALANCED, m.PARK_E)[:] = [10, 10]
    assert launch()
    c = check("power:parking")
    assert c["ok"] is False and "P-cores" in c["text"] and "E-cores could not be changed" in c["text"]
    assert "Windows' limit for that setting is 0" in c["text"] and "Reset All" not in c["text"]
    assert m.cell(m.BALANCED, m.PARK_P)[0] == 100, "the P-cores still are"
    assert not RO.has_power(m.PROC_SUB, m.PARK_E, scope="gamemode"), "nothing recorded that cannot be restored"
    assert quit_game()
    assert RO.summary()["power"] == 0 and m.cell(m.BALANCED, m.PARK_E) == [10, 10]


PARK_CAP = rf"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\{m.PROC_SUB}\{m.PARK_E}"


def test_the_old_tweaks_parking_cap_is_repaired_by_game_mode():
    """The developer's PC: ValueMax 0 on the E-core setting.  Game mode used
    to report it and point at Reset All; it puts Windows' 100 back itself."""
    fresh()
    m.rset(PARK_CAP, "ValueMax", 0)
    m.cell(m.BALANCED, m.PARK_E)[:] = [10, 10]
    assert launch()
    assert m.rdata(PARK_CAP, "ValueMax") == 100
    assert check("power:parking") == {"key": "power:parking", "ok": True, "text":
                                      "Core parking -> off (every core kept awake), after repairing the limit an "
                                      "old tweak had set to 0"}
    assert m.cell(m.BALANCED, m.PARK_E)[0] == 100
    assert quit_game()
    assert m.cell(m.BALANCED, m.PARK_E) == [10, 10], "the plan's own value back"
    assert m.rdata(PARK_CAP, "ValueMax") == 100, "the repair stays: it is Windows' own default"


def test_a_capped_setting_already_at_100_is_not_reported_as_failed():
    """Also the developer's PC: the High performance plan already had the
    E-cores at 100 — and game mode still said they "could not be changed"."""
    fresh()
    m.POWER_LIMITS[m.PARK_E] = 0
    assert launch()                                 # (the plan's E-core value is 100 already)
    assert check("power:parking")["ok"] is True
    assert quit_game()


def test_a_parking_cap_somebody_set_on_purpose_is_left_alone():
    fresh()
    m.rset(PARK_CAP, "ValueMax", 50)
    m.cell(m.BALANCED, m.PARK_E)[:] = [10, 10]
    assert launch()
    assert m.rdata(PARK_CAP, "ValueMax") == 50
    assert "Windows' limit for that setting is 50" in check("power:parking")["text"]
    stop()
    fresh()
    m.rset(PARK_CAP, "ValueMax", 5)
    RO.remember(PARK_CAP, "ValueMax", "0", scope="optimizer")      # on somebody's record: theirs to put back
    m.rset(PARK_CAP, "ValueMax", 0)
    assert RO.on_record(PARK_CAP, "ValueMax")
    m.cell(m.BALANCED, m.PARK_E)[:] = [10, 10]
    assert launch()
    assert m.rdata(PARK_CAP, "ValueMax") == 0
    stop()


def test_cpu_idle_windows_refuses_is_reported():
    fresh()
    real = m.run_cmd

    def refuse_idle(args):
        if args[:2] == ["powercfg", "/setacvalueindex"] and args[4].lower() in (m.IDLE, "idledisable"):
            return m.F("Access is denied.")
        return None

    m.CMD_HOOKS.append(refuse_idle)
    m.proc(4242, exe="game.exe")
    candidates.append({"pid": 4242, "exe": "game.exe", "name": "Game", "ws_mb": 900, "foreground": True,
                       "score": 1900})
    GP.start_monitoring()
    assert m.wait(lambda: check("power:idle") is not None)
    assert check("power:idle") == {"key": "power:idle", "ok": False,
                                   "text": "CPU idle: Windows did not accept the change"}
    assert real is m.run_cmd
    stop()


# ═════════════════════════════════════════════════════════════════════
# Network priority and streaming apps
# ═════════════════════════════════════════════════════════════════════
def test_network_rules_take_one_powershell_call_and_are_checked():
    """It used to be two PowerShell calls per streaming process: with eight
    Discord processes, half a minute went by before the rest was applied."""
    fresh()
    GP._find_running_streaming_exes = REAL_STREAMERS
    for i in range(8):
        m.proc(600 + i, exe="Discord.exe")
    m.K.procs[600]["priority"] = HIGH               # one copy the user had raised themselves
    m.PS_HOOKS.append(qos_ok)
    assert launch()
    assert len(m.ps_ran("New-NetQosPolicy")) == 1
    assert check("qos:game") == {"key": "qos:game", "ok": True, "text": "DSCP 46 for game.exe"}
    assert check("qos:discord.exe")["ok"] is True
    assert check("qos:discord.exe")["text"] == "Stream priority: discord.exe (8 processes)"
    assert all(m.K.procs[600 + i]["priority"] == PT.ABOVE_NORMAL_PRIORITY_CLASS for i in range(8))
    assert quit_game()
    assert m.K.procs[600]["priority"] == HIGH, "back to its own priority, not Normal for everyone"
    assert all(m.K.procs[600 + i]["priority"] == NORMAL for i in range(1, 8))


def test_a_network_rule_that_was_not_created_is_reported():
    fresh()
    assert launch()                                 # (the fake PowerShell confirms nothing)
    assert check("qos:game")["ok"] is False and "could not be created" in check("qos:game")["text"]
    stop()


# ═════════════════════════════════════════════════════════════════════
# After the game
# ═════════════════════════════════════════════════════════════════════
def test_only_the_games_own_leftovers_are_stopped():
    fresh()
    m.proc(4242, exe="game.exe")
    m.proc(900, exe="UnrealCEFSubProcess.exe", ppid=4242)     # left behind by the game
    m.proc(901, exe="steamwebhelper.exe", ppid=1)             # Steam's own window
    m.proc(902, exe="UnrealCEFSubProcess.exe", ppid=5555)     # another program's
    m.proc(903, exe="notepad.exe", ppid=4242)                 # the game opened it, but it is not a leftover
    m.exit_proc(4242)
    assert GP._stop_game_leftovers({4242}) == 1
    assert [c for c in m.K.calls if c[0] == "terminate"] == [("terminate", 900)]
    assert GP._stop_game_leftovers(set()) == 0 and GP._stop_game_leftovers(None) == 0


def test_post_game_refresh_kills_nothing_by_name():
    fresh()
    m.proc(901, exe="steamwebhelper.exe", ppid=1)
    m.clear_logs()
    REAL_REFRESH("Game", {4242})
    assert not m.ps_ran("Stop-Process"), "Steam's and Epic's web helpers, SteamVR and rundll32 used to be killed"
    assert not [c for c in m.K.calls if c[0] == "terminate"]


def test_post_game_refresh_leaves_the_shader_caches_alone():
    """Deleting the drivers' *.lock / *.tmp cache files after every game
    corrupted the cache and gave games visual glitches."""
    fresh()
    m.clear_logs()
    REAL_REFRESH("Game", {4242})
    for folder in ("DXCache", "GLCache", "DxCache", "D3DSCache", "NV_Cache"):
        assert not m.ps_ran(folder), f"{folder} was touched"
    assert not m.ps_ran("*.lock") and not m.ps_ran("Remove-Item")


def test_the_refresh_gets_the_games_processes():
    fresh()
    m.proc(4343, exe="game.exe")
    assert launch()
    m.exit_proc(4343)
    assert quit_game()
    assert refreshes == [("Game", {4242, 4343})]


# ═════════════════════════════════════════════════════════════════════
# Background programs on the efficiency cores
# ═════════════════════════════════════════════════════════════════════
ECO, UNSET = (1, 1), (0, 0)


def thr(pid):
    return m.K.procs[pid].get("throttle", UNSET)


def test_background_programs_go_to_efficiency_mode_and_come_back():
    fresh()
    m.proc(500, exe="chrome.exe")
    m.proc(501, exe="chrome.exe")
    m.proc(502, exe="Battle.net.exe")
    assert launch()
    assert thr(500) == thr(501) == thr(502) == ECO
    assert check("eco") == {"key": "eco", "ok": True,
                            "text": "Background programs -> efficiency cores (2 programs, 3 processes)"}
    assert os.path.exists(GP._ECO_FILE), "kept on disk for after a crash"
    assert thr(4242) == (5, 0), "the game itself stays out of efficiency mode"
    assert quit_game()
    assert thr(500) == thr(501) == thr(502) == UNSET
    assert not os.path.exists(GP._ECO_FILE) and GP._engine["eco_procs"] == {}


def test_what_must_stay_responsive_is_left_alone():
    fresh()
    keep = {600: "dwm.exe", 601: "audiodg.exe", 602: "Discord.exe", 603: "EasyAntiCheat_EOS.exe",
            604: "lghub_agent.exe", 605: "RazerAppEngine.exe", 606: "steam.exe", 607: "explorer.exe",
            608: "RTSS.exe", 609: "obs64.exe", 610: m.config.APP_EXE_NAME, 611: "csrss.exe"}
    for pid, exe in keep.items():
        m.proc(pid, exe=exe)
    m.proc(620, exe="CrashHandler.exe", ppid=4242)           # started by the game
    m.proc(621, exe="crashpad.exe", ppid=620)                # and what that started
    m.proc(622, exe="MsMpEng.exe", session=0)                # a service
    m.proc(623, exe="spotify.exe")
    assert launch()
    assert {pid: thr(pid) for pid in [*keep, 620, 621, 622] if thr(pid) != UNSET} == {}
    assert thr(623) == ECO
    stop()


def test_input_audio_display_and_xbox_helpers_are_left_alone():
    """What 3.6.4 put in efficiency mode on the developer's PC that it should
    not have (input, live audio, the display and VR runtimes, the Xbox app's
    game helpers) — and some it rightly did."""
    keep = ["gameinputsvc.exe", "gameinputredistservice.exe", "rzenginemon.exe", "rzsdkservice.exe",
            "nvidia broadcast.exe", "rtkauduservice64.exe", "dax3api.exe", "dolbydax3api.exe",
            "xgamehelper.exe", "gamebarpresencewriter.exe", "nvdisplay.container.exe",
            "ovrserver_x64.exe", "ovrredir.exe", "vrserver.exe", "vrcompositor.exe", "taskmgr.exe"]
    move = ["qbittorrent.exe", "nordvpn.exe", "librehardwaremonitor.exe", "malwarebytes.exe",
            "searchhost.exe", "bootstrapper.exe", "claude.exe", "msedgewebview2.exe", "awcc.ucsubagent.exe"]
    assert [e for e in keep if not GP._eco_keep(e)] == []
    assert [e for e in move if GP._eco_keep(e)] == []
    fresh()
    for i, exe in enumerate(keep + move):
        m.proc(800 + i, exe=exe)
    assert launch()
    assert [exe for i, exe in enumerate(keep) if thr(800 + i) != UNSET] == []
    assert [exe for i, exe in enumerate(move, len(keep)) if thr(800 + i) != ECO] == []
    stop()


def test_a_program_with_its_own_setting_is_left_alone():
    fresh()
    m.proc(700, exe="voiceapp.exe", throttle=(1, 0))          # opted out of efficiency mode itself
    m.proc(701, exe="updater.exe", throttle=(1, 1))           # in it already
    assert launch()
    assert thr(700) == (1, 0) and thr(701) == (1, 1)
    assert not [c for c in m.K.calls if c[0] == "throttle" and c[1] in (700, 701)]
    assert 700 not in GP._engine["eco_procs"] and 701 not in GP._engine["eco_procs"]
    assert quit_game()
    assert thr(700) == (1, 0) and thr(701) == (1, 1), "and nothing is 'put back' on them"


def test_other_power_throttling_flags_are_kept():
    fresh()
    m.proc(710, exe="tool.exe", throttle=(4, 0))             # its own timer-resolution setting
    assert launch()
    assert thr(710) == (5, 1)
    assert quit_game()
    assert thr(710) == (4, 0), "exactly what it had"


def test_a_program_opened_mid_game_is_moved_too():
    fresh()
    assert launch()
    m.proc(720, exe="notepad.exe")
    assert m.wait(lambda: thr(720) == ECO)
    # (the status line follows a moment later, after the record is on disk)
    assert m.wait(lambda: check("eco")["text"].endswith("(1 program, 1 process)")), check("eco")
    assert quit_game() and thr(720) == UNSET


def test_a_program_that_changes_its_own_setting_is_not_fought():
    fresh()
    m.proc(730, exe="chrome.exe")
    assert launch() and thr(730) == ECO
    m.K.procs[730]["throttle"] = (1, 0)                       # Chrome brings a tab to the front
    time.sleep(0.2)
    assert thr(730) == (1, 0), "not set again"
    assert quit_game()
    assert thr(730) == (1, 0), "and its own choice is not overwritten on the way out"


def test_a_reused_pid_is_not_put_back():
    fresh()
    m.proc(740, exe="chrome.exe")
    assert launch() and thr(740) == ECO
    m.K.procs[740].update(created=999_999)                    # 740 is another process by now, in efficiency mode itself
    GP.stop_monitoring()
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    assert thr(740) == ECO


def test_switched_off_mid_game_puts_them_back_at_once():
    fresh()
    m.proc(750, exe="chrome.exe")
    assert launch() and thr(750) == ECO
    GP.set_settings(background_on_ecores=False)
    assert m.wait(lambda: thr(750) == UNSET)
    assert m.wait(lambda: check("eco") is None) and GP._engine["gaming_mode"]
    m.proc(751, exe="notepad.exe")
    time.sleep(0.2)
    assert thr(751) == UNSET, "and nothing new is moved"
    stop()


def test_switched_off_or_no_e_cores_moves_nothing():
    fresh()
    GP.set_settings(background_on_ecores=False)
    m.proc(760, exe="chrome.exe")
    assert launch()
    assert thr(760) == UNSET and check("eco") is None
    stop()
    fresh()
    m.CPU_LAYOUT.update(logical=16, p=16, e=0, hybrid=False)
    m.proc(760, exe="chrome.exe")
    assert launch()
    assert thr(760) == UNSET and check("eco") is None
    stop()


def test_a_program_windows_does_not_keep_in_efficiency_mode_is_reported():
    fresh()
    m.proc(770, exe="stubborn.exe", ignore_eco=True)
    assert launch()
    assert check("eco") == {"key": "eco", "ok": False,
                            "text": "Background programs: Windows did not keep them in efficiency mode"}
    assert 770 not in GP._engine["eco_procs"]
    stop()


def test_a_background_program_that_becomes_the_tracked_game_comes_out():
    fresh()
    m.proc(5151, exe="other.exe")
    assert launch() and thr(5151) == ECO
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 700, "foreground": False,
                       "score": 900})
    m.exit_proc(4242)
    assert m.wait(lambda: GP._engine["active_pid"] == 5151)
    assert m.wait(lambda: 5151 not in GP._engine["eco_procs"])
    assert thr(5151) == (5, 0), "tuned as the game"
    assert quit_game(5151)
    assert thr(5151) == (5, 0) or m.K.procs[5151].get("exited")


def test_crash_mid_game_leaves_records_the_next_start_puts_back():
    fresh()
    m.proc(780, exe="chrome.exe")
    assert launch() and thr(780) == ECO
    GP._engine["monitor_gen"] += 1                  # the app dies: no clean-up runs
    GP._engine["monitoring"] = False
    time.sleep(0.15)
    GP._engine.update(gaming_mode=False, game_procs={}, checks=[], eco_procs={}, eco_seen={}, eco_on=False)
    RO._cache = None
    GP.check_and_restore_on_boot()
    assert thr(780) == UNSET and not os.path.exists(GP._ECO_FILE)
    # and a records file without the gaming flag (crash after the flag was cleared)
    m.K.procs[780]["throttle"] = ECO
    GP._engine["eco_procs"] = {780: {"exe": "chrome.exe", "created": 10_780, "before": [0, 0]}}
    GP._save_eco()
    GP._engine["eco_procs"] = {}
    GP.check_and_restore_on_boot()
    assert thr(780) == UNSET and not os.path.exists(GP._ECO_FILE)


def test_to_and_from_efficiency():
    fresh()
    m.proc(790, exe="a.exe")
    r = PT.to_efficiency(790)
    assert r == {"done": True, "before": [0, 0], "created": 10_790, "skip": ""} and thr(790) == ECO
    assert PT.to_efficiency(790)["skip"] == "own setting"
    assert PT.from_efficiency(790, 10_790, [0, 0]) and thr(790) == UNSET
    m.proc(791, exe="b.exe", protected=True)
    assert PT.to_efficiency(791)["skip"] == "cannot be opened"
    assert PT.to_efficiency(99_999)["skip"] == "cannot be opened"
    assert PT.from_efficiency(99_999, 1, [0, 0]), "gone: nothing of ours left"
    m.proc(792, exe="c.exe", throttle=ECO)
    assert PT.from_efficiency(792, 1, [0, 0]) and thr(792) == ECO, "another process by now: untouched"
    assert PT.session_of(790) == 1 and PT.session_of(99_999) is None


def test_windows_background_services_are_moved_and_no_other_service_is():
    fresh()
    for pid, exe in ((900, "SearchIndexer.exe"), (901, "TiWorker.exe"), (902, "MoUsoCoreWorker.exe"),
                     (903, "spoolsv.exe"), (904, "svchost.exe"), (905, "MsMpEng.exe"), (906, "audiodg.exe")):
        m.proc(pid, exe=exe, session=0)
    m.proc(907, exe="smartscreen.exe")                       # Defender / SmartScreen: left alone everywhere
    m.proc(908, exe="SecurityHealthSystray.exe")
    assert launch()
    assert thr(900) == thr(901) == thr(902) == ECO
    assert {pid: thr(pid) for pid in range(903, 909) if thr(pid) != UNSET} == {}
    assert m.PRIV and len(m.PRIV) % 2 == 0 and m.PRIV[::2] == [True] * (len(m.PRIV) // 2) \
        and m.PRIV[1::2] == [False] * (len(m.PRIV) // 2), "the debug privilege on only while each is opened"
    assert GP._engine["eco_procs"][900]["privileged"] is True
    assert GP._engine["eco_procs"].get(4242) is None
    del m.PRIV[:]
    assert quit_game()
    assert thr(900) == thr(901) == thr(902) == UNSET
    assert m.PRIV.count(True) == 3, "and again to put them back"


def test_real_session_of_this_process():
    """Read-only, against Windows."""
    saved = PT._k32
    PT._k32 = None
    try:
        assert isinstance(PT.session_of(os.getpid()), int)
    finally:
        PT._k32 = saved


# ═════════════════════════════════════════════════════════════════════
# High priority from the game's launch (anti-cheat-protected games)
# ═════════════════════════════════════════════════════════════════════
IFEO_EXE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\game.exe"
IFEO_KEY = IFEO_EXE + r"\PerfOptions"


def test_launch_priority_switched_off_writes_nothing():
    fresh()
    GP.set_settings(priority_at_launch=False)
    assert launch(protected=True)
    assert m.rget(IFEO_KEY, "CpuPriorityClass") is None
    assert check("proc:priority")["text"].endswith(HINT), "the panel says where it can be switched on"
    stop()


def test_a_protected_game_is_started_at_high_from_its_next_launch():
    """On by default: Call of Duty on the developer's PC never got it while
    it was a switch to find first."""
    fresh()
    assert GP.get_settings()["priority_at_launch"] is True
    assert launch(protected=True)
    assert m.rdata(IFEO_KEY, "CpuPriorityClass") == 3
    want = {"key": "proc:priority", "ok": None, "text":
            "Process priority: blocked while the game runs, so Windows will start it at High from the next launch"}
    assert check("proc:priority") == want
    time.sleep(0.2)
    assert check("proc:priority") == want, "and the ticks after it say the same, not 'lowered it again'"
    assert quit_game()
    assert m.rdata(IFEO_KEY, "CpuPriorityClass") == 3, "kept: it is for the next launch"
    assert launch(protected=True, priority=HIGH)             # Windows has started it at High
    assert check("proc:priority") == {"key": "proc:priority", "ok": True,
                                      "text": "Process priority -> High (Windows starts the game at High)"}
    assert quit_game()
    GP.set_settings(priority_at_launch=False)
    assert RO.value_names(IFEO_EXE) is None, "removed, keys and all"
    assert RO.registry_records("gamepriority") == []


def test_launch_priority_the_game_lowers_again_is_reported_so():
    fresh()
    GP.set_settings(priority_at_launch=True)
    m.rset(IFEO_KEY, "CpuPriorityClass", 3)                  # set at an earlier session
    assert launch(protected=True)                            # and still Normal
    assert check("proc:priority") == {"key": "proc:priority", "ok": False, "text":
                                      "Process priority: Windows started the game at High, but the game or its "
                                      "anti-cheat lowered it again"}
    stop()


def test_launch_priority_is_not_set_for_a_game_that_takes_high_anyway():
    fresh()
    GP.set_settings(priority_at_launch=True)
    assert launch()
    assert check("proc:priority")["ok"] is True and m.rget(IFEO_KEY, "CpuPriorityClass") is None
    stop()


def test_a_launch_priority_the_user_had_is_put_back():
    fresh()
    m.rset(IFEO_EXE, "MitigationOptions", 1)                 # something else under the game's key
    m.rset(IFEO_KEY, "CpuPriorityClass", 2)
    GP.set_settings(priority_at_launch=True)
    assert launch(protected=True)
    assert m.rdata(IFEO_KEY, "CpuPriorityClass") == 3
    stop()
    GP.set_settings(priority_at_launch=False)
    assert m.rdata(IFEO_KEY, "CpuPriorityClass") == 2 and m.rdata(IFEO_EXE, "MitigationOptions") == 1


def test_an_anti_cheat_that_refuses_the_call_is_named():
    fresh()
    real = m.K.SetPriorityClass
    GP.set_settings(priority_at_launch=False)
    m.K.SetPriorityClass = lambda h, value: 0                # the handle's right stripped
    try:
        assert launch()
        assert check("proc:priority")["text"] == "Process priority: the game's anti-cheat blocks the change" + HINT
        stop()
        fresh()
        assert launch(priority=HIGH)                         # refused, but Windows started it at High
        assert check("proc:priority") == {"key": "proc:priority", "ok": True, "text": "Process priority -> High"}
        stop()
    finally:
        m.K.SetPriorityClass = real


# ═════════════════════════════════════════════════════════════════════
# The session report
# ═════════════════════════════════════════════════════════════════════
from core import session_report as SR      # noqa: E402


def play(minutes, p_busy, e_busy, game_share, eco=True):
    """One game session: `minutes` of play with the cores that busy and the
    game using that share of the whole CPU."""
    fresh()
    GP.set_settings(background_on_ecores=eco)
    m.run_cpu(0, 0, 0)
    m.proc(500, exe="chrome.exe")
    assert launch(cpu=0)
    time.sleep(0.1)                                          # a tick reads the starting point
    m.run_cpu(minutes * 60, p_busy, e_busy)
    m.K.procs[4242]["cpu"] = int(game_share * minutes * 60 * 24 * 1e7)
    time.sleep(0.1)
    assert quit_game()
    return GP.get_engine_status()["last_report"]


def test_a_session_report_with_core_load_and_the_games_share():
    r = play(10, 0.6, 0.2, 0.25)
    assert (r["minutes"], r["p_busy"], r["e_busy"], r["game_share"], r["other_share"]) == (10.0, 60.0, 20.0, 25.0, 8.3)
    assert (r["eco_on"], r["eco_programs"], r["eco_processes"]) == (True, 1, 1)
    assert r["lines"][0].startswith("Last session: Game, 10.0 min")
    assert r["lines"][1] == "P-cores 60.0% busy, E-cores 20.0% busy; the game used 25.0% of the CPU, everything else 8.3%"
    assert r["lines"][2] == "Background programs on the efficiency cores: 1 (1 processes)"


def test_the_report_compares_with_the_same_game_the_other_way_round():
    play(10, 0.7, 0.1, 0.25, eco=False)
    reports = SR._load()
    r = play(10, 0.6, 0.2, 0.25, eco=True)                   # (fresh() wiped the file: put the first one back)
    SR.atomic_write_json(SR._FILE, reports + SR._load())
    r = SR.latest()
    assert r["lines"][-1].startswith("Same game with efficiency cores off (")
    assert r["lines"][-1].endswith("P-cores 70.0% busy, E-cores 10.0% busy; the game used 25.0% of the CPU, "
                                   "everything else 5.0%")


def test_a_session_too_short_to_say_anything_leaves_no_report():
    assert play(0.5, 0.6, 0.2, 0.25) is None


def test_real_cpu_counters_core_classes_and_process_time():
    """Read-only, against Windows."""
    times, classes = m.real_cpu_times(), m.real_core_classes()
    assert times and all(0 <= b <= t for b, t in times)
    assert classes and len(classes) == len(times) and any(classes)
    saved = PT._k32
    PT._k32 = None
    try:
        assert PT.process_cpu(os.getpid()) > 0
    finally:
        PT._k32 = saved


# ═════════════════════════════════════════════════════════════════════
# Updates wait for the game
# ═════════════════════════════════════════════════════════════════════
from core import updater as UP      # noqa: E402


class _Staged:
    """A downloaded update, and an installer that only records."""

    def __enter__(self):
        self.applied = []
        self.saved = (UP._apply_exe_update_async, UP._INSTALL_AFTER_GAME_DELAY_S, dict(UP._update_state))
        UP._apply_exe_update_async = lambda path: self.applied.append(path) or {"ok": True}
        UP._INSTALL_AFTER_GAME_DELAY_S = 0
        self.path = os.path.join(m.config.APPDATA_DIR, "staged-update.exe")
        with open(self.path, "wb") as f:
            f.write(b"MZ" + b"\0" * 64)
        UP._update_state.update(pending_install_path=self.path, latest_url="https://example.invalid/v.exe",
                                download_complete=True, update_available=True, install_after_game=False)
        return self

    def __exit__(self, *exc):
        UP._apply_exe_update_async, UP._INSTALL_AFTER_GAME_DELAY_S, state = self.saved
        UP._update_state.clear()
        UP._update_state.update(state)


def test_an_update_waits_for_the_game_and_installs_when_it_closes():
    fresh()
    with _Staged() as st:
        assert launch()
        r = UP.install_update_now()
        assert r["ok"] and r["deferred"] and r["msg"] == "Game is running. The update installs as soon as it closes."
        assert st.applied == [] and UP._update_state["install_after_game"] is True
        assert UP.is_safe_to_install() == {"safe": False, "reason": "Game is running"}, "auto-install waits too"
        assert quit_game()
        assert m.wait(lambda: st.applied == [st.path])
        assert UP._update_state["install_after_game"] is False


def test_install_now_anyway_and_without_a_game():
    fresh()
    with _Staged() as st:
        assert launch()
        assert UP.install_update_now(force=True) == {"ok": True} and st.applied == [st.path]
        stop()
    fresh()
    with _Staged() as st:
        assert UP.install_update_now() == {"ok": True} and st.applied == [st.path]
        UP.on_game_closed()                                  # nothing waiting: nothing happens
        time.sleep(0.1)
        assert st.applied == [st.path]


# ═════════════════════════════════════════════════════════════════════
# The "every CPU core" switch, Windows 10, unrestricted games
# ═════════════════════════════════════════════════════════════════════
def test_settings_default_and_persist():
    fresh()
    on = {"auto_enable_on_boot": True, "all_cores": True, "background_on_ecores": True, "priority_at_launch": True}
    assert GP.get_settings() == on
    GP.set_settings(all_cores=False)
    assert GP.get_settings() == {**on, "all_cores": False}
    GP.set_settings(auto_enable_on_boot=False)
    assert GP.get_settings() == {**on, "all_cores": False, "auto_enable_on_boot": False}
    GP.set_settings(background_on_ecores=False, priority_at_launch=False)
    assert GP.get_settings() == {"auto_enable_on_boot": False, "all_cores": False, "background_on_ecores": False,
                                 "priority_at_launch": False}


def test_every_core_switched_off_leaves_affinity_alone():
    fresh()
    GP.set_settings(all_cores=False)
    assert launch(affinity=0xFF)
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["affinity"] == 0xFF and not any("affinity" in c for c in changes)
    assert "cpusets_cleared" not in p
    assert p["priority"] == HIGH and p.get("throttle") == (5, 0), "the rest still applies"
    time.sleep(0.2)
    assert p["affinity"] == 0xFF, "and the re-checks leave it alone too"
    stop()


def test_windows_10_without_the_timer_flag():
    fresh()
    m.K.win10 = True
    assert launch()
    assert m.K.procs[4242].get("throttle") == (1, 0)
    assert "Power throttling -> off for the game" in GP._engine["changes"]
    stop()


def test_game_already_on_all_cores_is_not_rewritten():
    fresh()
    assert launch()
    assert not [c for c in m.K.calls if c[0] == "affinity"]
    assert "CPU affinity -> all 24 cores (8 P + 16 E)" in GP._engine["changes"], "and no restriction is claimed"
    stop()


# ═════════════════════════════════════════════════════════════════════
# Switching off mid-game, a second game, crashes
# ═════════════════════════════════════════════════════════════════════
def test_switched_off_while_the_game_is_running():
    fresh()
    m.proc(4343, exe="game.exe", affinity=0xF)
    assert launch(affinity=0xFF)
    GP.stop_monitoring()
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    p = m.K.procs[4242]
    assert p["priority"] == NORMAL and p["affinity"] == 0xFF and p["throttle"] == (0, 0), \
        "the still-running game gets its own priority, affinity and throttling back"
    assert m.K.procs[4343]["affinity"] == 0xF and m.K.procs[4343]["priority"] == NORMAL, "every process of it"
    assert plan_vals(m.BALANCED) == STOCK and RO.summary()["power"] == 0
    assert refreshes == [], "not a game exit: no clean-up of its processes"


def test_tracker_moves_to_a_second_running_game():
    fresh()
    assert launch(affinity=0xFF)
    m.proc(5151, affinity=0xF, exe="other.exe")
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 700, "foreground": False,
                       "score": 900})
    m.exit_proc(4242)
    assert m.wait(lambda: GP._engine["active_pid"] == 5151) and GP._engine["gaming_mode"]
    assert m.wait(lambda: m.K.procs[5151]["priority"] == HIGH)
    assert m.K.procs[5151]["affinity"] == m.SYSTEM_MASK, "the new game gets every core too"
    assert quit_game(5151)


def test_off_then_on_again_leaves_one_monitor_loop():
    fresh()
    GP.start_monitoring()
    first = GP._engine["thread"]
    GP.stop_monitoring()
    GP.start_monitoring()                           # within the old loop's sleep
    second = GP._engine["thread"]
    assert second is not first
    assert m.wait(lambda: not first.is_alive()), "the old loop must leave: two used to run side by side"
    assert second.is_alive()
    stop()


def test_crash_mid_game_is_repaired_at_the_next_start():
    fresh()
    assert launch()
    GP._engine["monitor_gen"] += 1                  # the app dies: no clean-up runs
    GP._engine["monitoring"] = False
    time.sleep(0.15)
    GP._engine.update(gaming_mode=False, active_pid=None, active_game=None, active_exe=None, changes=[],
                      proc_prior=None, game_procs={}, checks=[])
    RO._cache = None
    assert plan_vals(m.BALANCED)["idle"][0] == 1 and GP._is_gaming_flag_set(), "the tweaks are still on the machine"
    GP.check_and_restore_on_boot()
    assert plan_vals(m.BALANCED) == STOCK and reg_now() == (0x26, 20, 10), "put back from the records on disk"
    assert not GP._is_gaming_flag_set()


def test_crash_with_a_poisoned_baseline_still_restores_the_real_values():
    fresh()
    assert launch()
    GP._save_baseline({"priority_sep": "28", "sys_responsiveness": "a", "net_throttle": "ffffffff"})   # wrong
    GP._engine["monitor_gen"] += 1
    GP._engine["monitoring"] = False
    time.sleep(0.15)
    GP._engine.update(gaming_mode=False, game_procs={}, checks=[])
    RO._cache = None
    GP.check_and_restore_on_boot()
    assert reg_now() == (0x26, 20, 10), "the records win over a baseline taken at the wrong moment"


def test_session_started_by_an_older_build_falls_back_to_the_baseline():
    fresh()
    GP._save_baseline({"priority_sep": "26", "sys_responsiveness": "14", "net_throttle": "a",
                       "cpu_idle_disabled": False})
    GP._set_gaming_flag()
    m.cell(m.BALANCED, m.IDLE)[0] = 1               # left by a build that kept no records
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 40)
    GP.restore_normal_mode("boot recovery")
    assert plan_vals(m.BALANCED)["idle"] == [0, 0] and m.rdata(PRIO_KEY, "Win32PrioritySeparation") == 0x26


def test_older_build_fallback_respects_a_user_who_had_idle_off():
    fresh()
    GP._save_baseline({"priority_sep": "26", "sys_responsiveness": "14", "net_throttle": "a",
                       "cpu_idle_disabled": True})
    GP._set_gaming_flag()
    m.cell(m.BALANCED, m.IDLE)[0] = 1
    GP.restore_normal_mode("boot recovery")
    assert plan_vals(m.BALANCED)["idle"][0] == 1


def test_optimizer_run_during_a_game_keeps_its_setting_when_the_game_closes():
    fresh()
    assert launch()
    RO.remember_power(m.PROC_SUB, m.IDLE)           # the optimizer sets "idle off", mid-game
    m.run_cmd(["powercfg", "/setacvalueindex", "scheme_current", m.PROC_SUB, m.IDLE, "1"])
    assert quit_game()
    assert plan_vals(m.BALANCED)["idle"][0] == 1, "closing the game leaves the optimizer's setting alone"
    assert plan_vals(m.BALANCED)["park_p"] == [0, 5]
    RO.restore_all()
    assert plan_vals(m.BALANCED) == STOCK, "and Reset All then returns the user's own value"


# ═════════════════════════════════════════════════════════════════════
# The saved "normal" state
# ═════════════════════════════════════════════════════════════════════
def test_baseline_is_not_recaptured_while_a_gaming_profile_is_applied():
    fresh()
    GP._save_baseline({"priority_sep": "26"})
    GP._set_gaming_flag()
    assert GP.recapture_baseline()["ok"] is False, "it would save the gaming values as 'normal'"
    assert GP._load_baseline() == {"priority_sep": "26"}
    GP._clear_gaming_flag()
    assert GP.recapture_baseline()["ok"] is True
    assert GP._load_baseline()["priority_sep"] == "26"


def test_reset_all_refreshes_the_baseline():
    fresh()
    GP._save_baseline({"priority_sep": "28", "sys_responsiveness": "0", "net_throttle": "ffffffff"})
    OPT.reset_all_optimizations()                   # what is there now IS normal
    base = GP._load_baseline()
    assert base["priority_sep"] == "2" and base["sys_responsiveness"] == "14" and base["net_throttle"] == "a"


def test_first_start_captures_a_baseline_and_changes_nothing():
    fresh()
    assert not os.path.exists(GP.BASELINE_FILE)
    GP.check_and_restore_on_boot()
    assert GP._load_baseline()["priority_sep"] == "26"
    assert reg_now() == (0x26, 20, 10) and plan_vals(m.BALANCED) == STOCK


# ═════════════════════════════════════════════════════════════════════
# core/process_tuning.py on its own
# ═════════════════════════════════════════════════════════════════════
def test_a_process_that_has_exited_is_not_tuned():
    fresh()
    m.proc(7, affinity=0x3)
    m.K.procs[7]["exited"] = True                   # gone, but someone still holds a handle to it
    r = PT.tune(7)
    assert r["ok"] is False and r["err"] == "process has exited"
    assert m.K.procs[7]["priority"] == NORMAL
    assert PT.release(7, {"priority": 32}) is False
    assert PT.alive(7) is False and PT.read_state(7)["ok"] is False


def test_unknown_and_protected_pids_report_failure():
    fresh()
    r = PT.tune(99999)
    assert r["ok"] is False and "error" in r["err"]
    assert PT.alive(99999) is False
    m.proc(8, protected=True)
    assert PT.tune(8)["err"] == "access denied (protected process)"
    assert PT.release(8, {"priority": 32}) is False and PT.release(8, None) is False
    assert PT.alive(8) is True and PT.read_state(8)["ok"] is True, "a query-only handle still works"


def test_tune_and_release_round_trip():
    fresh()
    m.proc(9, affinity=0xF0)
    r = PT.tune(9)
    assert r["ok"] and r["priority"] and r["throttling"]
    assert r["affinity"] == {"ok": True, "cores": 24, "was": 4, "changed": True}
    assert r["prior"] == {"priority": NORMAL, "affinity": 0xF0}
    st = PT.read_state(9)
    assert (st["priority"], st["affinity"], st["throttle_exempt"]) == (HIGH, m.SYSTEM_MASK, True)
    assert PT.release(9, r["prior"]) is True
    p = m.K.procs[9]
    assert (p["priority"], p["affinity"], p["throttle"]) == (NORMAL, 0xF0, (0, 0))


def test_tune_reports_only_what_reads_back():
    fresh()
    m.proc(11, affinity=0xF, ignore_priority=True, ignore_affinity=True)
    r = PT.tune(11)
    assert r["ok"] is True and r["priority"] is False and r["affinity"]["ok"] is False


def test_tune_without_all_cores():
    fresh()
    m.proc(10, affinity=0x3)
    r = PT.tune(10, all_cores=False)
    assert r["affinity"] is None and "affinity" not in r["prior"] and m.K.procs[10]["affinity"] == 0x3


def test_alive_tells_a_reused_pid_from_the_original():
    fresh()
    m.proc(12, created=111)
    assert PT.alive(12, created=111) is True and PT.alive(12, created=222) is False
    assert PT.identity(12) == {"image": r"C:\Games\game.exe", "created": 111}


def test_find_image_matches_path_and_name():
    fresh()
    m.proc(20, exe="game.exe")
    m.proc(21, exe="Game.exe", image=r"c:\games\GAME.exe")
    m.proc(22, exe="game.exe", image=r"D:\elsewhere\game.exe")
    m.proc(23, exe="other.exe")
    assert sorted(PT.find_image(r"C:\Games\game.exe")) == [20, 21]
    assert PT.find_image("") == []


def test_set_priority_and_terminate():
    fresh()
    m.proc(30)
    assert PT.set_priority(30, PT.ABOVE_NORMAL_PRIORITY_CLASS) == (True, NORMAL)
    assert PT.terminate(30) is True and PT.alive(30) is False
    assert PT.set_priority(31, NORMAL) == (False, None) and PT.terminate(31) is False


def test_core_description():
    assert PT.describe_cores() == "24 cores (8 P + 16 E)"
    assert PT.describe_cores({"logical": 16, "p": 16, "e": 0, "hybrid": False}) == "16 cores"
    assert PT.describe_cores({"logical": 0}) == "every core"


def test_real_cpu_layout_adds_up():
    lay = m.real_cpu_layout()                       # read-only: asks Windows about this PC's cores
    assert lay["logical"] >= 1 and lay["p"] + lay["e"] == lay["logical"]
    assert lay["hybrid"] == (lay["e"] > 0)


def test_real_process_queries_against_windows():
    """The read-only calls, against the real kernel32 (this test process)."""
    saved = PT._k32
    PT._k32 = None
    try:
        me = os.getpid()
        procs = PT.processes()
        assert procs and any(p["pid"] == me for p in procs)
        ident = PT.identity(me)
        assert ident["image"] and ident["created"]
        assert PT.alive(me, ident["created"]) is True and PT.alive(me, created=1) is False
        assert me in PT.find_image(ident["image"])
        st = PT.read_state(me)
        assert st["ok"] and st["system"] and st["affinity"]
    finally:
        PT._k32 = saved


# ═════════════════════════════════════════════════════════════════════
# What a game is called
# ═════════════════════════════════════════════════════════════════════
# custom_exes.json and library_cache.json as they are on the owner's PC (the
# scan was saved by 3.7.0, so it still carries the old mode_hint).
OWNER_CUSTOM = [{"exe": "robloxstudiobeta.exe", "name": "rbxStudio"}, {"exe": "cod.exe", "name": "Call of Duty"},
                {"exe": "megabonk.exe", "name": "Megabonk"}, {"exe": "capcut.exe", "name": "capcut"}]
OWNER_LIBRARY = [
    {"title": "Call of Duty- Modern Warfare 3", "exe": "cod23-cod.exe", "launcher": "Xbox", "known": True,
     "mode_hint": None},
    {"title": "Rocket League®", "exe": "launcher.exe", "launcher": "Epic", "known": True, "mode_hint": None},
    {"title": "Terraria", "exe": "terraria.exe", "launcher": "Steam", "known": True, "mode_hint": None},
    {"title": "Call of Duty- Black Ops 6", "exe": "cod24-cod.exe", "launcher": "Xbox", "known": False,
     "mode_hint": None},
    {"title": "Marvel's Spider-Man 2", "exe": "spider-man2.exe", "launcher": "Steam", "known": False,
     "mode_hint": "visual"},
    {"title": "Marvel’s Spider-Man Remastered", "exe": "spider-man.exe", "launcher": "Steam", "known": False,
     "mode_hint": "visual"},
]
CUSTOM_FILE = os.path.join(GP.PROFILES_DIR, "custom_exes.json")


def owner_files(custom=OWNER_CUSTOM, library=OWNER_LIBRARY):
    fresh()
    if custom is not None:
        assert GP.atomic_write_json(CUSTOM_FILE, custom)
    if library is not None:
        assert LS.atomic_write_json(LS.CACHE_FILE, {"games": library, "scanned_at": time.time(),
                                                    "total": len(library), "known": 3})


def windows(*rows):
    """PowerShell listing the processes that have a window, the way the real
    scan asks for them: (pid, process name, MB)."""
    def hook(command):
        if "MainWindowHandle" not in command:
            return None
        return m.R(json.dumps([{"Id": pid, "ProcessName": name, "WS": ws} for pid, name, ws in rows
                               if pid in m.ALIVE]))
    return hook


def test_display_names_on_the_owners_pc():
    owner_files()
    want = {
        "cod.exe": "Call of Duty",                              # the name the owner typed when adding it
        "robloxstudiobeta.exe": "rbxStudio", "capcut.exe": "capcut", "megabonk.exe": "Megabonk",
        "cod23-cod.exe": "Call of Duty: Modern Warfare 3",      # the installed-games scan ("- " read as ": ")
        "cod24-cod.exe": "Call of Duty: Black Ops 6",
        "terraria.exe": "Terraria",
        "launcher.exe": "Rocket League",                        # the scan's exe for it; no trademark sign
        "spider-man.exe": "Marvel’s Spider-Man Remastered", "spider-man2.exe": "Marvel's Spider-Man 2",
        "modernwarfare.exe": "Call of Duty",                    # the built-in names
        "rainbowsix.exe": "Rainbow Six Siege", "rainbowsix_be.exe": "Rainbow Six Siege",
        "javaw.exe": "Minecraft", "r5apex.exe": "Apex Legends", "r5apex_dx12.exe": "Apex Legends",
        "marvel-win64-shipping.exe": "Marvel Rivals", "cs2.exe": "Counter-Strike 2",
        "start_protected_game.exe": "Elden Ring", "vrchat.exe": "VRChat",
        "fortniteclient-win64-shipping.exe": "Fortnite", "fortniteclient-win64-shipping_eac_eos.exe": "Fortnite",
        "xerces_spike_dx11.exe": "Xerces Spike Dx11",           # nothing knows it: the exe, made readable
        "gamesir connect.exe": "Gamesir Connect",
    }
    assert {exe: GP.display_name_for(exe) for exe in want} == want
    # in whatever case the exe comes, with or without its ending
    assert GP.display_name_for("Cod23-Cod.exe") == "Call of Duty: Modern Warfare 3"
    assert GP.display_name_for("Marvel-Win64-Shipping.exe") == "Marvel Rivals"
    assert GP.display_name_for(" COD ") == "Call of Duty" and GP.display_name_for("JAVAW.EXE") == "Minecraft"
    # the fallback is the name game mode has always shown
    assert GP.display_name_for("EpicGamesUpdater.exe") == "Epic Games Updater"
    assert GP.display_name_for("game.exe") == "Game" and GP.display_name_for("other.exe") == "Other"
    assert GP.display_name_for("Something-Win64-Shipping.exe") == "Something"
    assert GP.display_name_for("something-win64-shipping.exe") == "Something", "lower case too"
    for odd in (None, "", "   ", 7, object()):
        assert isinstance(GP.display_name_for(odd), str), "never raises"
    assert GP.display_name_for(None) == "" and GP.display_name_for("") == ""


def test_display_names_with_nothing_on_disk():
    fresh()
    assert not os.path.exists(CUSTOM_FILE) and not os.path.exists(LS.CACHE_FILE)
    assert GP.display_name_for("cod.exe") == "Call of Duty" and GP.display_name_for("cod23-cod.exe") == "Call of Duty"
    assert GP.display_name_for("cod24-cod.exe") == "Call of Duty" and GP.display_name_for("terraria.exe") == "Terraria"


def test_a_game_added_without_a_name_does_not_hide_a_better_one():
    """add_custom_game saves a game added without a name under the name made
    up from its exe; that one is not the user's choice."""
    owner_files(custom=[{"exe": "cod24-cod.exe", "name": "Cod24-Cod"}, {"exe": "javaw.exe", "name": "Javaw"},
                        {"exe": "cs2.exe", "name": "CS2 (my settings)"}, {"exe": "nameless.exe"},
                        {"exe": "blank.exe", "name": "  \x00 "}, "not an entry", {"name": "no exe"}])
    assert GP._nice_name("cod24-cod.exe") == "Cod24-Cod" and GP._nice_name("javaw.exe") == "Javaw"
    assert GP.display_name_for("cod24-cod.exe") == "Call of Duty: Black Ops 6", "the scan's title"
    assert GP.display_name_for("javaw.exe") == "Minecraft", "the built-in name"
    assert GP.display_name_for("cs2.exe") == "CS2 (my settings)", "a name the user typed wins over everything"
    assert GP.display_name_for("nameless.exe") == "Nameless" and GP.display_name_for("blank.exe") == "Blank"


def test_a_game_added_from_the_installed_games_list_is_named_as_cleanly():
    """That list adds an unrecognised game under the title the scan found,
    so the name arrives exactly as the folder or the launcher spells it."""
    owner_files(library=None, custom=[{"exe": "cod24-cod.exe", "name": "Call of Duty- Black Ops 6"},
                                      {"exe": "rocketleague.exe", "name": "Rocket League\u00ae"},
                                      {"exe": "cod.exe", "name": "Call of Duty"}, {"exe": "x.exe", "name": "A - B"}])
    assert GP.display_name_for("cod24-cod.exe") == "Call of Duty: Black Ops 6"
    assert GP.display_name_for("rocketleague.exe") == "Rocket League"
    assert GP.display_name_for("cod.exe") == "Call of Duty" and GP.display_name_for("x.exe") == "A - B"


def test_scanned_titles_are_cleaned_before_they_are_shown():
    owner_files(custom=None, library=[
        {"title": "Rocket League�", "exe": "rocketleague.exe"},
        {"title": "  Half-Life 2- Episode One™\x00\x07 ", "exe": "hl2.exe"},
        {"title": "Tom Clancy's Rainbow Six® Siege - Deluxe", "exe": "r6.exe"},
        {"title": "Zero​Width Space\tTab\nLine", "exe": "zw.exe"},
        {"title": "�\x00", "exe": "cs2.exe"},
        {"title": 42, "exe": "dota2.exe"}, {"title": "No exe", "exe": ""}, {"title": "No exe either"}, "not a game",
        {"title": "x" * 300, "exe": "long.exe"},
    ])
    assert GP.display_name_for("rocketleague.exe") == "Rocket League"
    assert GP.display_name_for("hl2.exe") == "Half-Life 2: Episode One"
    assert GP.display_name_for("r6.exe") == "Tom Clancy's Rainbow Six Siege - Deluxe", "a real dash stays a dash"
    assert GP.display_name_for("zw.exe") == "ZeroWidth Space Tab Line"
    assert GP.display_name_for("cs2.exe") == "Counter-Strike 2", "nothing readable left: the next source"
    assert GP.display_name_for("dota2.exe") == "Dota 2"
    assert len(GP.display_name_for("long.exe")) == 80


def test_an_exe_two_installed_titles_share_names_neither():
    owner_files(custom=None, library=[
        {"title": "Rocket League", "exe": "launcher.exe"}, {"title": "Fall Guys", "exe": "Launcher.exe"},
        {"title": "Cyberpunk 2077", "exe": "cyberpunk2077.exe", "launcher": "GOG"},
        {"title": "Cyberpunk 2077", "exe": "cyberpunk2077.exe", "launcher": "Steam"}])
    assert GP.display_name_for("launcher.exe") == "Launcher"
    assert GP.display_name_for("cyberpunk2077.exe") == "Cyberpunk 2077", "the same game from two launchers"


def test_names_follow_the_files_and_a_damaged_file_is_no_problem():
    owner_files()
    reads = []
    real_cached = LS.get_cached
    LS.get_cached = lambda: reads.append(1) or real_cached()
    try:
        for _ in range(40):
            assert GP.display_name_for("terraria.exe") == "Terraria"
            assert GP.display_name_for("cod.exe") == "Call of Duty"
        assert reads == [1], "the scan is read once, not at every question (detection asks every 3 s)"
        GP.atomic_write_json(CUSTOM_FILE, [{"exe": "cod.exe", "name": "CoD HQ"}])
        assert GP.display_name_for("cod.exe") == "CoD HQ", "a changed file is read again"
        assert GP.display_name_for("capcut.exe") == "Capcut", "no longer a custom game"
        LS.atomic_write_json(LS.CACHE_FILE, {"games": [{"title": "Terraria (modded)", "exe": "terraria.exe"}]})
        assert GP.display_name_for("terraria.exe") == "Terraria (modded)"
        with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        with open(LS.CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("[1, 2")
        assert GP.display_name_for("cod.exe") == "Call of Duty" and GP.display_name_for("terraria.exe") == "Terraria"
        with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
            f.write('{"exe": "cod.exe"}')               # JSON, but not the list it should be
        with open(LS.CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        assert GP.display_name_for("cod.exe") == "Call of Duty" and GP.display_name_for("cod23-cod.exe") == "Call of Duty"
    finally:
        LS.get_cached = real_cached


def test_detection_names_games_properly():
    """The real scan, on what PowerShell lists: the owner's log has 179
    "Game detected: Cod23-Cod", 83 "Cod", 9 "Javaw" and 3 "Marvel"."""
    owner_files()
    rows = [(100, "cod", 900), (104, "cod23-cod", 2400), (108, "javaw", 1500), (112, "EpicGamesUpdater", 19),
            (116, "GameSir Connect Setup 1.14.2", 18), (120, "start_protected_game", 60), (124, "chrome", 800),
            (128, "Marvel-Win64-Shipping", 3000), (132, "ExitedGame", 500)]
    for pid, name, _ in rows[:-1]:
        m.proc(pid, exe=name + ".exe")
    m.PS_HOOKS.append(windows(*rows))
    real_fg = GP._get_foreground_pid
    GP._get_foreground_pid = lambda: 104
    try:
        found = REAL_SCAN()
    finally:
        GP._get_foreground_pid = real_fg
    assert [(c["exe"], c["name"]) for c in found] == [
        ("cod23-cod.exe", "Call of Duty: Modern Warfare 3"),        # the one in front comes first
        ("Marvel-Win64-Shipping.exe", "Marvel Rivals"), ("javaw.exe", "Minecraft"), ("cod.exe", "Call of Duty"),
        ("start_protected_game.exe", "Elden Ring")], "and neither the updater nor the installer is a game"
    assert found[0]["foreground"] is True and found[0]["pid"] == 104


class _Toasts:
    """Stands in for core.notifier."""

    def __init__(self):
        self.seen = []

    def notify_game_detected(self, name, exe):
        self.seen.append(("detected", name, exe))

    def notify_game_exited(self, name):
        self.seen.append(("closed", name))

    def toast(self, *a, **k):
        self.seen.append(("toast",) + a)
        return {"ok": True}


def test_the_hero_the_toasts_and_the_report_use_the_display_name():
    owner_files()
    m.run_cpu(0, 0, 0)
    m.proc(4242, exe="cod.exe", cpu=0)
    m.PS_HOOKS.append(windows((4242, "cod", 900)))
    toasts = _Toasts()
    stand_in, real_fg = GP._scan_game_candidates, GP._get_foreground_pid
    GP._scan_game_candidates, GP._get_foreground_pid, GP._notifier = REAL_SCAN, (lambda: 4242), toasts
    try:
        GP.start_monitoring()
        assert m.wait(lambda: GP._engine["gaming_mode"] and any("CPU idle" in c for c in GP._engine["changes"]))
        status = GP.get_engine_status()
        assert status["active_game"] == "Call of Duty" and status["active_exe"] == "cod.exe"
        assert GP.get_active_game_info()["display"] == "Call of Duty"
        assert m.wait(lambda: at_starts) and at_starts[0] == ("cod.exe", "Call of Duty", 4242), "Adaptive Tuning too"
        time.sleep(0.1)                                      # a tick reads the starting point
        m.run_cpu(600, 0.6, 0.2)
        m.K.procs[4242]["cpu"] = int(0.25 * 600 * 24 * 1e7)
        time.sleep(0.1)
        assert quit_game()
        assert m.wait(lambda: ("closed", "Call of Duty") in toasts.seen), toasts.seen
    finally:
        GP._scan_game_candidates, GP._get_foreground_pid, GP._notifier = stand_in, real_fg, None
    assert toasts.seen[0] == ("detected", "Call of Duty", "cod.exe")
    assert GP.get_engine_status()["last_report"]["lines"][0].startswith("Last session: Call of Duty, 10.0 min")
    assert cl_calls == ["apply", "reset"], "and its posture is still Call of Duty's"


def test_the_installed_games_list_carries_the_name_game_mode_shows():
    owner_files()
    cached = LS.get_cached()
    names = {g["exe"]: g["display_name"] for g in cached["games"]}
    assert names["cod23-cod.exe"] == "Call of Duty: Modern Warfare 3" and names["launcher.exe"] == "Rocket League"
    assert names["cod24-cod.exe"] == "Call of Duty: Black Ops 6" and names["terraria.exe"] == "Terraria"
    assert [g["title"] for g in cached["games"]] == [g["title"] for g in OWNER_LIBRARY], "the title stays as found"
    assert [g["display_name"] for g in LS.scan_all(force=False)["games"]] == [g["display_name"] for g in cached["games"]]
    with open(LS.CACHE_FILE, encoding="utf-8") as f:
        assert "display_name" not in f.read(), "the file on disk is not changed"
    owner_files(custom=None, library=[{"title": "�\x00", "exe": "xerces_spike_dx11.exe"},
                                      {"title": "Half-Life 2- Episode One™", "exe": "hl2.exe"},
                                      {"title": 42, "exe": ""}, "not a game"])
    games = LS.get_cached()["games"]
    assert [g["display_name"] if isinstance(g, dict) else g for g in games] == [
        "Xerces Spike Dx11", "Half-Life 2: Episode One", "", "not a game"]
    with open(LS.CACHE_FILE, "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")                        # not the dict a scan saves: handed on as it was
    assert LS.get_cached() == [1, 2, 3]


def test_the_crosshair_names_games_the_way_game_mode_does_and_asks_once_per_exe():
    """It named them with the old _nice_name ("Cod23-Cod")."""
    owner_files()
    XH._names["stamp"] = None
    asked = []
    real = GP.display_name_for
    GP.display_name_for = lambda exe: asked.append(exe) or real(exe)
    try:
        settings = XH._load()
        hint = {g["exe"]: g["name"] for g in XH._games_hint(settings, {"last_games": ["cod23-cod.exe"]})}
        assert hint["cod23-cod.exe"] == "Call of Duty: Modern Warfare 3" and hint["javaw.exe"] == "Minecraft"
        assert hint["cod.exe"] == "Call of Duty" and hint["marvel-win64-shipping.exe"] == "Marvel Rivals"
        first = len(asked)
        assert first >= len(GP.KNOWN_GAME_EXES) - 20, "every known game is named"
        assert {g["exe"]: g["name"] for g in XH._games_hint(settings, {"last_games": ["cod23-cod.exe"]})} == hint
        assert len(asked) == first, "asked once per exe, not at every read"
        GP.atomic_write_json(CUSTOM_FILE, OWNER_CUSTOM + [{"exe": "javaw.exe", "name": "My Java game"}])
        again = {g["exe"]: g["name"] for g in XH._games_hint(XH._load(), {"last_games": []})}
        assert again["javaw.exe"] == "My Java game" and len(asked) > first, "a changed file is read again"
    finally:
        GP.display_name_for = real
        XH._names["stamp"] = None


def test_the_foreground_game_is_named_the_same_way():
    owner_files()
    real = GP._foreground_exe_via_user32
    try:
        GP._foreground_exe_via_user32 = lambda: (104, "cod23-cod.exe")
        assert GP.get_foreground_game() == {"exe": "cod23-cod.exe", "pid": 104,
                                            "name": "Call of Duty: Modern Warfare 3"}
        GP._foreground_exe_via_user32 = lambda: (108, "notepad.exe")
        assert GP.get_foreground_game() is None
    finally:
        GP._foreground_exe_via_user32 = real


# ═════════════════════════════════════════════════════════════════════
# What counts as a game
# ═════════════════════════════════════════════════════════════════════
def test_an_installer_or_updater_is_not_a_game_because_of_a_hint_in_its_name():
    """On the owner's PC the bare "game" hint started game mode sessions for
    EpicGamesUpdater.exe (twice, 15 s each) and three GameSir installers, and
    nine more for GameSir Connect itself, a controller tool."""
    not_games = ["epicgamesupdater.exe", "EpicGamesUpdater.exe", "gamesir connect setup 1.14.2.exe",
                 "GameSir Connect Setup 1.14.5.exe", "gamesir connect setup 1.14.2(1).exe",
                 "CrashReportClient-Win64-Shipping.exe", "UnrealEngineLauncher.exe", "MyGame_Installer.exe",
                 "GameUpdate.exe", "gamehelper.exe", "unins000_game.exe", "vulkan_redist.exe",
                 "SomeGame-CrashHandler64.exe", "game_dxsetup.exe",
                 "gamesir connect.exe", "GameSir Connect.exe", "GameSir Nexus.exe"]
    assert [e for e in not_games if GP._game_match(e) != "hint"] == [], "each of them only matches a hint"
    assert [e for e in not_games if GP._is_game(e)] == []
    for word in GP._NOT_A_GAME:
        assert any(word in e.lower() for e in not_games), word
    assert set(GP._NOT_A_GAME) == {"setup", "install", "updater", "update", "launcher", "helper", "crashhandler",
                                   "crashreport", "unins", "redist", "dxsetup", "gamesir"}
    still_games = ["xerces_spike_dx11.exe", "Something-Win64-Shipping.exe", "start_protected_game.exe",
                   "mygame.exe", "boardgame arena.exe"]
    assert [e for e in still_games if not GP._is_game(e)] == [], "a hint alone still makes a game"
    known = ["launcher.exe", "minecraftlauncher.exe", "arma3launcher.exe", "starcitizenlauncher.exe",
             "genshinimpactlauncher.exe", "cod.exe"]
    assert [e for e in known if GP._game_match(e) != "known" or not GP._is_game(e)] == [], \
        "the known list is never second-guessed"
    GP.KNOWN_GAME_EXES.add("my game launcher setup.exe")        # what adding a custom game does
    try:
        assert GP._is_game("My Game Launcher Setup.exe") is True, "nor is a game the user added"
    finally:
        GP.KNOWN_GAME_EXES.discard("my game launcher setup.exe")
    assert GP._is_game("My Game Launcher Setup.exe") is False
    for exe in ("chrome.exe", "obs64.exe", "epicgameslauncher.exe", "notepad.exe", "gamebarpresencewriter.exe"):
        assert GP._is_game(exe) is False and GP._game_match(exe) == "", exe
    assert GP._is_game("cs2") is True and GP._is_game("") is False and GP._is_game(None) is False


def test_an_updater_named_like_a_game_stays_off_the_efficiency_cores_as_before():
    """Only detection changed: the sweep that moves background programs to
    the efficiency cores leaves alone what it left alone before."""
    fresh()
    m.proc(700, exe="EpicGamesUpdater.exe")
    m.proc(701, exe="notepad.exe")
    assert launch()
    assert thr(700) == UNSET and thr(701) == ECO
    stop()


def test_the_installed_games_scan_no_longer_carries_a_tuner_mode():
    fresh()
    assert LS._enrich({"title": "Counter-Strike 2", "exe": "cs2.exe"}) == \
        {"title": "Counter-Strike 2", "exe": "cs2.exe", "known": True}
    assert LS._enrich({"title": "Unknown", "exe": "nope.exe"}) == {"title": "Unknown", "exe": "nope.exe",
                                                                  "known": False}
    owner_files()                                   # a scan 3.7.0 saved, mode_hint and all
    cached = LS.get_cached()
    assert [g["exe"] for g in cached["games"]] == [g["exe"] for g in OWNER_LIBRARY]
    assert not any("mode_hint" in g for g in cached["games"])
    assert not any("mode_hint" in g for g in LS.scan_all(force=False)["games"]), "(answered from the saved scan)"
    assert not any("mode_hint" in g for g in LS.get_known_only())
    with open(LS.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    assert "adaptive_tuning" not in src and "AUTO_CLASSIFY" not in src, "the scanner does not import the tuner"


# ═════════════════════════════════════════════════════════════════════
# The overclock on record for the session
# ═════════════════════════════════════════════════════════════════════
def test_the_overclock_the_app_holds_is_recorded_at_game_start():
    """It came out as 0 / 0 on all 399 game starts in the owner's log: the
    clocks were subtracted under keys the stock record does not have."""
    fresh()
    APPLIED[0] = {"core": 450, "mem": 2225, "power": 100, "active": True}
    order = []
    real_release, real_read = IM.manual_release, GO.get_applied_oc
    IM.manual_release = lambda: order.append("idle un-park") or {"ok": True}
    GO.get_applied_oc = lambda: order.append("read") or dict(APPLIED[0])
    try:
        assert launch()
        assert order[:2] == ["idle un-park", "read"], "read after a parked overclock is back"
        assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (450, 2225)
        info = GP.get_active_game_info()
        assert (info["oc_core"], info["oc_mem"]) == (450, 2225)
        from core.utils import get_log_buffer
        assert any("Captured active OC at game-start: core+450 / mem+2225" in e["msg"] for e in get_log_buffer())
        assert live_reads == [], "not worked out from clock read-outs any more"
        assert quit_game()
    finally:
        IM.manual_release, GO.get_applied_oc = real_release, real_read
    assert m.wait(lambda: cr_calls), "the exit is handed on"
    assert (cr_calls[-1]["active_core"], cr_calls[-1]["active_mem"]) == (450, 2225)
    assert cr_calls[-1]["exe"] == "game.exe" and oc_calls == []
    assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (0, 0), "cleared with the session"


def test_a_saved_overclock_the_app_is_not_holding_is_not_recorded():
    fresh()
    PROFILE[0] = dict(SAVED)                        # saved, not applied (or parked, or another tool's)
    assert launch()
    assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (0, 0)
    assert quit_game()
    assert m.wait(lambda: cr_calls) and (cr_calls[-1]["active_core"], cr_calls[-1]["active_mem"]) == (0, 0)


def test_a_card_that_cannot_be_asked_does_not_stop_the_session():
    fresh()
    real_read = GO.get_applied_oc

    def broken():
        raise OSError("no driver")

    GO.get_applied_oc = broken
    try:
        assert launch()
        assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (0, 0)
        assert check("proc:priority")["ok"], "the session started all the same"
        assert quit_game()
        GO.get_applied_oc = lambda: None            # nothing to say
        assert launch(pid=4343)
        assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (0, 0)
        assert quit_game(4343)
    finally:
        GO.get_applied_oc = real_read


# ═════════════════════════════════════════════════════════════════════
# Frame telemetry for the crosshair
# ═════════════════════════════════════════════════════════════════════
# The crosshair's "is the game in exclusive fullscreen" check reads the
# present mode PresentMon reports; the old tuner was the only thing that
# ever started it.
START = ("start", 4242, "game.exe")


def test_frame_telemetry_runs_for_the_crosshair_and_stops_with_the_game():
    fresh()
    CROSSHAIR[0] = True
    assert launch()
    assert m.wait(lambda: ft_calls == [START]), ft_calls
    assert GP._engine["frames_pid"] == 4242
    time.sleep(0.3)                                 # a dozen ticks
    assert ft_calls == [START], "started once, not at every tick"
    assert quit_game()
    assert m.wait(lambda: ft_calls == [START, ("stop",)]), ft_calls     # (the last step of the exit)
    assert GP._engine["frames_pid"] is None


def test_no_frame_telemetry_with_the_crosshair_off():
    fresh()
    assert launch()
    time.sleep(0.3)
    assert quit_game()
    time.sleep(0.1)
    assert ft_calls == [], "nothing started, so nothing to stop either"


def test_the_crosshair_switched_on_or_off_mid_game_is_followed():
    fresh()
    assert launch()
    time.sleep(0.1)
    assert ft_calls == []
    CROSSHAIR[0] = True
    assert m.wait(lambda: ft_calls == [START]), ft_calls
    CROSSHAIR[0] = False
    assert m.wait(lambda: ft_calls == [START, ("stop",)]), ft_calls
    assert quit_game()
    time.sleep(0.1)
    assert ft_calls == [START, ("stop",)], "not stopped a second time"


def test_frame_telemetry_follows_a_game_that_restarts_itself():
    fresh()
    CROSSHAIR[0] = True
    assert launch()
    assert m.wait(lambda: ft_calls == [START])
    GP.time.delay = 0.1
    try:
        m.exit_proc(4242)
        assert m.wait(lambda: GP._engine["exit_ticks"] >= 2)
        m.proc(4545, exe="game.exe")
        assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    finally:
        GP.time.delay = 0.02
    assert m.wait(lambda: ft_calls == [START, ("stop",), ("start", 4545, "game.exe")]), ft_calls
    assert quit_game(4545)
    assert m.wait(lambda: ft_calls[3:] == [("stop",)]), ft_calls


def test_frame_telemetry_follows_the_tracker_to_a_second_game():
    fresh()
    CROSSHAIR[0] = True
    assert launch()
    assert m.wait(lambda: ft_calls == [START])
    m.proc(5151, exe="other.exe")
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 700, "foreground": False,
                       "score": 900})
    m.exit_proc(4242)
    assert m.wait(lambda: ft_calls == [START, ("stop",), ("start", 5151, "other.exe")]), ft_calls
    assert quit_game(5151)
    assert m.wait(lambda: ft_calls[3:] == [("stop",)]), ft_calls


def test_frame_telemetry_stops_when_normal_mode_is_forced_mid_game():
    fresh()
    CROSSHAIR[0] = True
    assert launch()
    assert m.wait(lambda: ft_calls == [START])
    GP.restore_normal_mode("manual")
    assert ft_calls == [START, ("stop",)] and not GP._engine["gaming_mode"]
    time.sleep(0.2)
    assert ft_calls == [START, ("stop",)], "the held-off game does not start it again"
    stop()


def test_frame_telemetry_that_fails_never_gets_in_the_way_of_the_session():
    fresh()
    CROSSHAIR[0] = True
    tries = []
    real_start, real_stop, real_enabled = FT.start_tracking, FT.stop_tracking, XH.is_enabled

    def broken_start(pid=None, exe_name=None):
        tries.append(pid)
        raise RuntimeError("PresentMon would not start")

    def broken_stop():
        tries.append("stop")
        raise RuntimeError("nor stop")

    FT.start_tracking, FT.stop_tracking = broken_start, broken_stop
    try:
        assert launch()
        assert m.wait(lambda: tries == [4242])
        time.sleep(0.3)                             # a dozen ticks
        assert tries == [4242], "one try for the process, not one every 3 seconds"
        assert GP._engine["gaming_mode"] and check("proc:priority")["ok"]
        assert quit_game(), "and the exit goes through"
        assert reg_now() == (0x26, 20, 10)
        assert m.wait(lambda: tries == [4242, "stop"]), tries

        def broken_question():
            raise RuntimeError("settings unreadable")

        XH.is_enabled = broken_question             # the crosshair cannot say: taken as off
        del tries[:]
        assert launch(pid=4343)
        time.sleep(0.2)
        assert tries == [] and GP._engine["gaming_mode"]
        assert quit_game(4343)
    finally:
        FT.start_tracking, FT.stop_tracking, XH.is_enabled = real_start, real_stop, real_enabled


# ═════════════════════════════════════════════════════════════════════
# Adaptive Tuning's call sites (the engine itself is a stand-in here; it
# is tested in tests/test_adaptive_tuning.py)
# ═════════════════════════════════════════════════════════════════════
def test_adaptive_tuning_is_told_right_after_the_idle_un_park_and_before_the_slow_steps():
    """It used to be the last step of a game start, after the registry, power
    and PowerShell network steps: a game it had eased ran its launch and its
    shader compile on the full overclock."""
    fresh()
    real_release, real_reg, real_net = IM.manual_release, GP._apply_registry_tweaks, GP._apply_network_priority
    IM.manual_release = lambda: order.append("idle un-park") or {"ok": True}
    GP._apply_registry_tweaks = lambda *a, **k: order.append("registry") or real_reg(*a, **k)
    GP._apply_network_priority = lambda *a, **k: order.append("network") or real_net(*a, **k)

    def engaged(exe, display_name=None, pid=None):
        order.append("adaptive tuning start")
        at_starts.append((exe, display_name, pid))
        APPLIED[0] = {"core": 338, "mem": 1669, "power": 100, "active": True}      # what its session put on the card
        return {"engaged": True, "level": 1, "level_name": "75%", "core": 338, "mem": 1669, "applied_ok": True}
    AT.on_game_start = engaged
    try:
        assert launch()
        assert at_starts == [("game.exe", "Game", 4242)], "the exe, the display name and the pid"
        steps = [s for s in order if s in ("idle un-park", "adaptive tuning start", "registry", "network")]
        assert steps[:4] == ["idle un-park", "adaptive tuning start", "registry", "network"], order
        # the overclock on record for the session is what Adaptive Tuning runs the game at
        assert (GP._engine["active_oc_core"], GP._engine["active_oc_mem"]) == (338, 1669)
        assert quit_game()
    finally:
        IM.manual_release, GP._apply_registry_tweaks, GP._apply_network_priority = real_release, real_reg, real_net
        AT.on_game_start = _at_start


def test_a_game_start_survives_adaptive_tuning_failing():
    fresh()

    def broken(*a, **k):
        raise RuntimeError("state file locked")
    AT.on_game_start = broken
    try:
        assert launch() and check("proc:priority")["ok"]
        assert quit_game()
    finally:
        AT.on_game_start = _at_start
    fresh()
    AT.on_game_start = lambda *a, **k: None         # (an answer that is not a dict)
    try:
        assert launch()
        assert quit_game()
    finally:
        AT.on_game_start = _at_start


def test_the_exit_goes_to_adaptive_tuning_first_and_crash_recovery_only_hears_of_a_game_it_did_not_handle():
    """crash_recovery used to run first and Adaptive Tuning second.  Both
    judged the same exit, and crash_recovery could lower the clocks after
    Adaptive Tuning had handed the card back."""
    fresh()
    APPLIED[0] = {"core": 450, "mem": 2225, "power": 100, "active": True}
    AT_EXIT[0] = {"handled": True, "restored": True}
    assert launch()
    before = time.time()
    assert quit_game()
    assert m.wait(lambda: at_exits)
    exe, exit_ts = at_exits[0]
    assert exe == "game.exe" and before <= exit_ts <= time.time(), "the time game mode saw the game gone"
    time.sleep(0.2)
    assert cr_calls == [] and at_ends == [], "handled: crash_recovery is not asked, and an exit is not a release"

    fresh()                                         # not handled (switched off, say): crash_recovery, afterwards
    APPLIED[0] = {"core": 450, "mem": 2225, "power": 100, "active": True}
    assert launch()
    assert quit_game()
    assert m.wait(lambda: cr_calls)
    assert [s for s in order if s in ("adaptive tuning exit", "crash recovery")] == ["adaptive tuning exit",
                                                                                     "crash recovery"]
    assert cr_calls[0]["exe"] == "game.exe" and cr_calls[0]["exit_ts"] == at_exits[0][1]

    fresh()                                         # Adaptive Tuning failing must not lose the crash check
    def broken(*a, **k):
        raise RuntimeError("boom")
    AT.on_game_exit = broken
    try:
        assert launch()
        assert quit_game()
        assert m.wait(lambda: cr_calls)
    finally:
        AT.on_game_exit = _at_exit


def test_ending_game_mode_without_the_game_closing_releases_adaptive_tunings_session():
    """Game mode only reports an exit for "game exit".  Put back by hand, from
    the tray or by stopping the monitor, the session was never ended: the card
    stayed on its values and every later game was refused."""
    for how, reason in ((lambda: GP.restore_normal_mode("manual"), "manual"),
                        (lambda: GP.restore_normal_mode("user-tray"), "user-tray"),
                        (lambda: GP.stop_monitoring(), "monitor stopped")):
        fresh()
        assert launch()
        how()
        assert m.wait(lambda: not GP._engine["gaming_mode"])
        assert m.wait(lambda: at_ends == [reason]), (reason, at_ends)
        time.sleep(0.15)
        assert at_exits == [] and cr_calls == [], "not judged as a game exit: the game is still running"
    fresh()                                         # a real exit is not a release
    assert launch()
    assert quit_game()
    assert m.wait(lambda: at_exits) and at_ends == []


def test_the_tracker_moving_to_another_game_reports_the_old_ones_exit_and_takes_on_the_new_one():
    """The tracker only moves once no process of the old game is left, so the
    old game has closed: that is an exit.  It used to be a release, which
    judges nothing: a game that closed on a graphics driver error while
    another game's window was open left no trace at all."""
    fresh()
    assert launch()
    m.proc(5151, exe="other.exe")
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 900, "foreground": False,
                       "score": 900})
    gone = time.time()
    m.exit_proc(4242)
    assert m.wait(lambda: GP._engine["active_pid"] == 5151)
    assert m.wait(lambda: len(at_starts) == 2)
    switched = time.time()
    assert at_starts[1] == ("other.exe", "Other", 5151)
    assert at_ends == [], "not a release"
    assert len(at_exits) == 1 and at_exits[0][0] == "game.exe", at_exits
    assert gone <= at_exits[0][1] <= switched, "the time the old game was first seen gone"
    assert [s for s in order if s.startswith("adaptive tuning")] == [
        "adaptive tuning start", "adaptive tuning exit", "adaptive tuning start"], \
        "the old game's exit first: the card is handed back before the new session reads it"
    time.sleep(0.15)
    assert cr_calls == [], "nothing more than before at a switch: crash_recovery was never asked there"
    assert quit_game(5151)
    assert m.wait(lambda: len(at_exits) == 2) and at_exits[1][0] == "other.exe"


def test_a_tracker_switch_still_moves_on_when_adaptive_tuning_fails():
    fresh()
    assert launch()

    def broken(*a, **k):
        raise RuntimeError("boom")

    AT.on_game_exit = broken
    try:
        m.proc(5151, exe="other.exe")
        candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 900, "foreground": False,
                           "score": 900})
        m.exit_proc(4242)
        assert m.wait(lambda: GP._engine["active_pid"] == 5151)
        assert m.wait(lambda: len(at_starts) == 2), "the new game is still taken on"
    finally:
        AT.on_game_exit = _at_exit
    assert quit_game(5151)


def test_a_game_relaunched_inside_the_grace_is_reported_as_started_again_then_as_closed():
    """Game mode waits about 12 s for a game that closes to come back.  When
    it does, under a new process, game mode stays on as before, but Adaptive
    Tuning is now told: the new process first (its session is carried on),
    then the exit at the time the old process was first seen gone, so a
    graphics driver error just before it is judged as "the game closed"
    instead of "it kept running"."""
    fresh()
    AT_START[0] = {"engaged": True, "adopted": True, "level": 0}
    assert launch()
    GP.time.delay = 0.1                             # (slower ticks, so the gap below is a real one)
    try:
        gone = time.time()
        m.exit_proc(4242)
        assert m.wait(lambda: GP._engine["exit_ticks"] >= 2), "the monitor has seen it gone"
        back = time.time()
        m.proc(4545, exe="game.exe")
        assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    finally:
        GP.time.delay = 0.02
    assert m.wait(lambda: at_exits)
    assert at_starts == [("game.exe", "Game", 4242), ("game.exe", "Game", 4545)], at_starts
    assert len(at_exits) == 1 and at_exits[0][0] == "game.exe"
    assert gone <= at_exits[0][1] <= back, "when the old process was first seen gone, not when it came back"
    assert [s for s in order if s.startswith("adaptive tuning")] == [
        "adaptive tuning start", "adaptive tuning start", "adaptive tuning exit"], order
    assert at_ends == [] and GP._engine["gaming_mode"] and refreshes == [], "game mode itself stays on as before"
    time.sleep(0.15)
    assert len(at_exits) == 1 and cr_calls == [], "reported once"
    assert quit_game(4545)
    assert m.wait(lambda: len(at_exits) == 2) and at_exits[1][0] == "game.exe"


def test_a_relaunch_adaptive_tuning_did_not_take_on_is_not_reported_as_an_exit():
    """Not taken on (switched off, say): an exit reported now could close a
    session and hand its card back under a game that is running again."""
    fresh()
    assert launch()                                 # AT_START answers "off"
    GP.time.delay = 0.1
    try:
        m.exit_proc(4242)
        assert m.wait(lambda: GP._engine["exit_ticks"] >= 2)
        m.proc(4545, exe="game.exe")
        assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    finally:
        GP.time.delay = 0.02
    assert m.wait(lambda: len(at_starts) == 2) and at_starts[1] == ("game.exe", "Game", 4545)
    time.sleep(0.15)
    assert at_exits == [] and at_ends == []
    assert quit_game(4545)


def test_a_new_process_of_the_game_with_no_gap_is_not_an_exit():
    """A pid that changes between two ticks without the game ever being seen
    gone (a game that restarts itself at once): followed as before, nothing
    is reported."""
    fresh()
    AT_START[0] = {"engaged": True, "level": 0}
    assert launch()
    m.proc(4545, exe="game.exe")                    # the new process is there before the old one goes
    m.exit_proc(4242)
    assert m.wait(lambda: GP._engine["active_pid"] == 4545)
    time.sleep(0.15)
    assert len(at_starts) == 1 and at_exits == [] and at_ends == [], (at_starts, at_exits, at_ends)
    assert quit_game(4545)


def test_running_game_candidates_lists_the_games_with_a_window_and_says_when_it_could_not_look():
    owner_files()
    rows = [(100, "cod", 900), (104, "cod23-cod", 2400), (124, "chrome", 800), (116, "GameSir Connect Setup 1.14.2", 18)]
    for pid, name, _ in rows:
        m.proc(pid, exe=name + ".exe")
    m.PS_HOOKS.append(windows(*rows))
    real_fg = GP._get_foreground_pid
    front = [100]
    GP._get_foreground_pid = lambda: front[0]
    try:
        found = GP.running_game_candidates()
        assert found == [{"exe": "cod.exe", "pid": 100, "name": "Call of Duty"},          # the one in front first
                         {"exe": "cod23-cod.exe", "pid": 104, "name": "Call of Duty: Modern Warfare 3"}], found
        # Held off after Force normal mode: game mode leaves it alone, but it is still a game being played.
        front[0] = 0
        GP._engine["held_off"] = {100: None}
        assert [c["exe"] for c in GP.running_game_candidates()] == ["cod23-cod.exe", "cod.exe"]
        assert GP._engine["held_off"] == {100: None}, "game mode's state is not touched"
    finally:
        GP._get_foreground_pid = real_fg
        GP._engine["held_off"] = {}
    m.PS_HOOKS.clear()
    m.PS_HOOKS.append(lambda command: m.R("") if "MainWindowHandle" in command else None)
    assert GP.running_game_candidates() == [], "nothing with a window"
    for failed in (m.F("Timed out"), m.R("not json"), m.R('"just a string"')):
        m.PS_HOOKS.clear()
        m.PS_HOOKS.append(lambda command, failed=failed: failed if "MainWindowHandle" in command else None)
        assert GP.running_game_candidates() is None, failed
        assert GP._scan_game_candidates is not None and REAL_SCAN() == [], "game mode's own scan: nothing, as before"
    m.PS_HOOKS.clear()
    m.PS_HOOKS.append(lambda command: m.R(json.dumps({"Id": 100, "ProcessName": "cod", "WS": "x"}))
                      if "MainWindowHandle" in command else None)
    assert GP.running_game_candidates() == [{"exe": "cod.exe", "pid": 100, "name": "Call of Duty"}], "one row"
    m.PS_HOOKS.clear()
    m.PS_HOOKS.append(lambda command: (_ for _ in ()).throw(RuntimeError("boom")))
    assert GP.running_game_candidates() is None, "never raises"
    m.PS_HOOKS.clear()


def test_running_game_candidates_takes_no_lock_of_game_modes():
    """Adaptive Tuning asks it from its own threads, and game mode calls into
    Adaptive Tuning while it holds its lock."""
    fresh()
    m.proc(100, exe="cod.exe")
    m.PS_HOOKS.append(windows((100, "cod", 900)))
    answer = []
    with GP._session_lock:
        t = threading.Thread(target=lambda: answer.append(GP.running_game_candidates()), daemon=True)
        t.start()
        t.join(3)
    assert answer and [c["exe"] for c in answer[0]] == ["cod.exe"], "answered while game mode held its lock"
    m.PS_HOOKS.clear()


def test_the_monitor_can_say_it_is_on_its_way():
    fresh()
    assert GP.monitor_starting() is False
    GP.set_monitor_starting(True)
    try:
        assert GP.monitor_starting() is True and GP.is_monitoring() is False
        GP.start_monitoring()
        assert GP.is_monitoring() is True and GP.monitor_starting() is False, "once it runs, it is not on its way"
        GP.stop_monitoring()
    finally:
        GP.set_monitor_starting(False)
    assert GP.monitor_starting() is False


def test_game_mode_no_longer_runs_the_sampler_nobody_reads():
    """The 5 Hz clock sampler was started for every session for the old
    tuner; the hardware page starts its own."""
    with open(os.path.join(m.REPO, "core", "game_profiles.py"), encoding="utf-8") as f:
        src = f.read()
    assert "perf_monitor" not in src
    for gone in ("was_crash=", "exited_at=", "starting_core", "gameplay_state"):
        assert gone not in src, gone
