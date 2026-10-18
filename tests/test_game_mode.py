"""Game mode (core/game_profiles.py, core/process_tuning.py): what is applied
when a game starts, on which processes and which power plan, that it stays
applied while the game runs, that the status says what really is in effect,
and that all of it is put back when the game closes - or at the next start,
after a crash.

The monitor loop runs for real, on a fake machine: processes are entries in
the fake kernel32, and a "game" is whatever _scan_game_candidates returns.
"""
import sandbox as m

import os
import re
import threading
import time

from core import reg_originals as RO, process_tuning as PT

GP = m.app("core.game_profiles")
OPT = m.app("core.optimizer")
CL = m.app("core.competitive_latency")
from core import gpu_overclock as GO, adaptive_tuning as AT, perf_monitor as PM   # noqa: E402
from core import crash_recovery as CR, background_pauser as BPZ                   # noqa: E402

PRIO_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
PROFILE_KEY = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
STOCK = {"idle": [0, 0], "park_e": [100, 10], "park_p": [0, 5]}     # like the plan on a Core Ultra 285K
HIGH, NORMAL = PT.HIGH_PRIORITY_CLASS, PT.NORMAL_PRIORITY_CLASS

# ── everything around the engine that is not under test ───────────────
TARGET = ["auto"]
cl_calls: list = []
oc_calls: list = []
refreshes: list = []
candidates: list = []
REAL_STREAMERS = GP._find_running_streaming_exes
REAL_REFRESH = GP._post_game_refresh
REAL_EXTRAS = GP._apply_session_extras
GP._notifier = None
GP._post_game_refresh = lambda name=None, pids=None: refreshes.append((name, set(pids or ())))
GP._find_running_streaming_exes = lambda: []
GP._scan_game_candidates = lambda: [c for c in candidates
                                    if c["pid"] in m.ALIVE and c["pid"] != GP._engine.get("active_pid")]
GO.read_live_state = lambda: {"ok": False}
GO.get_stock_baseline = lambda: {}
GO.load_profile = lambda: {"exists": False}
GO.apply_oc = lambda **k: oc_calls.append(k) or {"ok": True}
AT.on_game_start = lambda exe, name: {"engaged": False}
AT.on_game_exit = lambda *a, **k: None
AT.get_game_state = lambda exe: {"target": TARGET[0]}
PM.start = lambda consumer=None: None
PM.stop = lambda consumer=None: None
CR.handle_game_exit = lambda **k: {"crashed": False}
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


def fresh(target="auto", plan=m.BALANCED):
    GP.stop_monitoring()
    time.sleep(0.12)                                # let a running loop notice and leave
    m.fresh(plan=plan)
    os.makedirs(GP.PROFILES_DIR, exist_ok=True)
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x26)       # this user's own values
    m.rset(PROFILE_KEY, "SystemResponsiveness", 20)
    m.rset(PROFILE_KEY, "NetworkThrottlingIndex", 10)
    del candidates[:], cl_calls[:], oc_calls[:], refreshes[:]
    TARGET[0] = target
    GP._engine.update(gaming_mode=False, active_game=None, active_exe=None, active_pid=None, changes=[],
                      proc_prior=None, game_procs={}, checks=[], exit_ticks=0, streamer_prior={})
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
    assert launch(ignore_priority=True, ignore_affinity=True, affinity=0xFF)
    assert check("proc:priority")["ok"] is False and check("proc:affinity")["ok"] is False
    assert "Process priority -> High" not in GP._engine["changes"]
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
    assert check("proc:priority")["text"] == "Process priority: the game keeps setting its own — no longer forced"
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
def test_max_fps_tweaks_land_on_the_plan_the_game_runs_on():
    fresh(target="max_fps")
    assert launch()
    clone = m.PLAN["active"]
    assert clone not in (m.BALANCED, m.HIGH_PERF), "switched to the Ultimate Performance copy"
    assert plan_vals(clone)["idle"][0] == 1 and plan_vals(clone)["park_p"][0] == 100
    assert plan_vals(m.BALANCED) == STOCK, "they used to go to the plan it had just left"
    assert oc_calls and oc_calls[0]["power_pct"] == 100
    assert quit_game()
    assert m.PLAN["active"] == m.BALANCED, "back on the user's plan"
    assert plan_vals(clone) == STOCK, "the performance plan is left as it was found, not with idle off for good"
    assert plan_vals(m.BALANCED) == STOCK and RO.summary()["power"] == 0


def test_low_latency_target_engages_competitive_latency_for_the_session():
    fresh(target="low_latency")
    assert launch()
    assert cl_calls == ["apply"] and plan_vals(m.BALANCED)["idle"][0] == 1
    assert quit_game()
    assert cl_calls == ["apply", "reset"] and plan_vals(m.BALANCED) == STOCK


def test_per_game_posture_survives_an_app_restart():
    fresh(target="max_fps")
    assert launch()
    assert os.path.exists(GP._TARGET_RESTORE_FILE), "on disk before anything changes"
    saved = GP._load_target_restore()
    assert saved["target"] == "max_fps" and saved["power_guid"] == m.BALANCED
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
    assert oc_calls == [], "the card is at its own defaults after a restart: no overclock is re-applied"


# ═════════════════════════════════════════════════════════════════════
# A process Windows will not let us change (anti-cheat)
# ═════════════════════════════════════════════════════════════════════
def test_protected_game_is_left_alone_and_the_ui_says_so():
    fresh()
    assert launch(protected=True, affinity=0xFF)
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["priority"] == NORMAL and p["affinity"] == 0xFF
    assert "Process priority: unchanged (the game's process is protected)" in changes
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
    m.PS_PRIORITY_OK[0] = True                      # says OK, changes nothing
    assert launch(protected=True)
    assert "Process priority: unchanged (the game's process is protected)" in GP._engine["changes"]
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
    m.POWER_LIMITS[m.PARK_E] = 0
    assert launch()
    c = check("power:parking")
    assert c["ok"] is False and "P-cores" in c["text"] and "E-cores could not be changed" in c["text"]
    assert "capped" in c["text"] and "Reset All" in c["text"]
    assert m.cell(m.BALANCED, m.PARK_P)[0] == 100, "the P-cores still are"
    assert not RO.has_power(m.PROC_SUB, m.PARK_E, scope="gamemode"), "nothing recorded that cannot be restored"
    assert quit_game()
    assert RO.summary()["power"] == 0 and plan_vals(m.BALANCED) == STOCK


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


def test_the_refresh_gets_the_games_processes():
    fresh()
    m.proc(4343, exe="game.exe")
    assert launch()
    m.exit_proc(4343)
    assert quit_game()
    assert refreshes == [("Game", {4242, 4343})]


# ═════════════════════════════════════════════════════════════════════
# The "every CPU core" switch, Windows 10, unrestricted games
# ═════════════════════════════════════════════════════════════════════
def test_settings_default_and_persist():
    fresh()
    assert GP.get_settings() == {"auto_enable_on_boot": True, "all_cores": True}
    GP.set_settings(all_cores=False)
    assert GP.get_settings() == {"auto_enable_on_boot": True, "all_cores": False}
    GP.set_settings(auto_enable_on_boot=False)
    assert GP.get_settings() == {"auto_enable_on_boot": False, "all_cores": False}


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
