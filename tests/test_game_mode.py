"""Game mode (core/game_profiles.py, core/process_tuning.py): what is applied
when a game starts, on which process and which power plan, and that all of it
is put back when the game closes - or at the next start, after a crash.

The monitor loop runs for real, on a fake machine: processes are entries in
the fake kernel32, and a "game" is whatever _scan_game_candidates returns.
"""
import sandbox as m

import os
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

# ── everything around the engine that is not under test ───────────────
TARGET = ["auto"]
cl_calls: list = []
oc_calls: list = []
candidates: list = []
GP._notifier = None
GP._post_game_refresh = lambda *a: None
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

    def sleep(self, seconds):
        time.sleep(0.02)


GP.time = _FastClock()


def fresh(target="auto", plan=m.BALANCED):
    GP.stop_monitoring()
    time.sleep(0.12)                                # let a running loop notice and leave
    m.fresh(plan=plan)
    os.makedirs(GP.PROFILES_DIR, exist_ok=True)
    m.rset(PRIO_KEY, "Win32PrioritySeparation", 0x26)       # this user's own values
    m.rset(PROFILE_KEY, "SystemResponsiveness", 20)
    m.rset(PROFILE_KEY, "NetworkThrottlingIndex", 10)
    del candidates[:], cl_calls[:], oc_calls[:]
    TARGET[0] = target
    GP._engine.update(gaming_mode=False, active_game=None, active_exe=None, active_pid=None, changes=[],
                      proc_prior=None)
    GP._target_restore = {}


def launch(pid=4242, exe="game.exe", **process):
    m.proc(pid, **process)
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


def stop():
    GP.stop_monitoring()
    m.wait(lambda: not GP._engine["gaming_mode"])


# ═════════════════════════════════════════════════════════════════════
# A game its launcher left on 8 of 24 cores
# ═════════════════════════════════════════════════════════════════════
def test_game_gets_high_priority_every_core_and_no_throttling():
    fresh()
    assert launch(affinity=0xFF), GP._engine
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["priority"] == PT.HIGH_PRIORITY_CLASS and "Process priority -> High" in changes
    assert p["affinity"] == m.SYSTEM_MASK, "bound to ALL cores, P and E: the 8-core restriction is lifted"
    assert p.get("cpusets_cleared") is True
    assert "CPU affinity -> all 24 cores (8 P + 16 E) (was limited to 8)" in changes, changes
    assert p.get("throttle") == (5, 0), "exempt from power throttling, timer requests honoured"
    assert "Power throttling -> off for the game" in changes
    stop()


def test_no_core_is_parked_and_idle_is_off_on_the_active_plan():
    fresh()
    assert launch()
    changes = GP._engine["changes"]
    assert plan_vals(m.BALANCED) == {"idle": [1, 0], "park_e": [100, 10], "park_p": [100, 5]}
    assert "Core parking -> off (every core kept awake)" in changes and "CPU idle -> disabled" in changes
    assert RO.summary()["power"] == 3, "each of those is on record..."
    assert RO.has_power("SUB_PROCESSOR", "IdleDisable", "gamemode"), "...under the game-mode scope"
    assert not RO.has_power("SUB_PROCESSOR", "IdleDisable")
    assert reg_now() == (40, 10, 4294967295), "the registry half is applied as before"
    stop()


def test_process_is_tuned_once_not_on_every_tick():
    fresh()
    assert launch(affinity=0xFF)
    time.sleep(0.3)                                 # a dozen ticks
    assert len([c for c in m.K.calls if c[0] == "affinity"]) == 1, "a game's own later choice stands"
    stop()


def test_everything_is_put_back_when_the_game_closes():
    fresh()
    assert launch(affinity=0xFF)
    assert quit_game()
    assert plan_vals(m.BALANCED) == STOCK and m.PLAN["active"] == m.BALANCED
    assert reg_now() == (0x26, 20, 10), "the user's values, not Windows defaults"
    assert RO.summary()["power"] == 0 and RO.summary()["registry"] == 0
    assert not GP._is_gaming_flag_set()


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
                      proc_prior=None)
    GP._target_restore = {}
    RO._cache = None
    del oc_calls[:]
    GP.check_and_restore_on_boot()                  # next start
    assert m.PLAN["active"] == m.BALANCED, "the plan the game left is returned to"
    assert not os.path.exists(GP._TARGET_RESTORE_FILE)
    assert oc_calls == [], "the card is at its own defaults after a restart: no overclock is re-applied"


# ═════════════════════════════════════════════════════════════════════
# A process Windows will not let us open (anti-cheat)
# ═════════════════════════════════════════════════════════════════════
def test_protected_game_is_left_alone_and_the_ui_says_so():
    fresh()
    assert launch(protected=True, affinity=0xFF)
    p, changes = m.K.procs[4242], GP._engine["changes"]
    assert p["priority"] == PT.NORMAL_PRIORITY_CLASS and p["affinity"] == 0xFF
    assert "Process priority: unchanged (the game's process is protected)" in changes
    assert "CPU affinity: unchanged (the game's process is protected)" in changes
    assert not any("Power throttling" in c for c in changes)
    assert plan_vals(m.BALANCED)["park_p"][0] == 100, "the system-wide half still applies"
    assert quit_game() and plan_vals(m.BALANCED) == STOCK


def test_protected_game_whose_priority_powershell_can_set():
    fresh()
    m.PS_PRIORITY_OK[0] = True
    assert launch(protected=True)
    assert "Process priority -> High" in GP._engine["changes"]
    stop()


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
    assert p["priority"] == PT.HIGH_PRIORITY_CLASS and p.get("throttle") == (5, 0), "the rest still applies"
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
    assert launch(affinity=0xFF)
    GP.stop_monitoring()
    assert m.wait(lambda: not GP._engine["gaming_mode"])
    p = m.K.procs[4242]
    assert p["priority"] == PT.NORMAL_PRIORITY_CLASS and p["affinity"] == 0xFF and p["throttle"] == (0, 0), \
        "the still-running game gets its own priority, affinity and throttling back"
    assert plan_vals(m.BALANCED) == STOCK and RO.summary()["power"] == 0


def test_tracker_moves_to_a_second_running_game():
    fresh()
    assert launch(affinity=0xFF)
    m.proc(5151, affinity=0xF)
    candidates.append({"pid": 5151, "exe": "other.exe", "name": "Other", "ws_mb": 700, "foreground": False,
                       "score": 900})
    m.exit_proc(4242)
    assert m.wait(lambda: GP._engine["active_pid"] == 5151) and GP._engine["gaming_mode"]
    assert m.wait(lambda: m.K.procs[5151]["priority"] == PT.HIGH_PRIORITY_CLASS)
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
                      proc_prior=None)
    RO._cache = None
    assert plan_vals(m.BALANCED)["idle"][0] == 1 and GP._is_gaming_flag_set(), "the tweaks are still on the machine"
    GP.check_and_restore_on_boot()
    assert plan_vals(m.BALANCED) == STOCK and reg_now() == (0x26, 20, 10), "put back from the records on disk"
    assert not GP._is_gaming_flag_set()


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
    OPT.reset_all_optimizations()                   # writes its defaults; what is there now IS normal
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
    assert m.K.procs[7]["priority"] == PT.NORMAL_PRIORITY_CLASS
    assert PT.release(7, {"priority": 32}) is False


def test_unknown_and_protected_pids_report_failure():
    fresh()
    r = PT.tune(99999)
    assert r["ok"] is False and "error" in r["err"]
    m.proc(8, protected=True)
    assert PT.tune(8)["err"] == "access denied (protected process)"
    assert PT.release(8, {"priority": 32}) is False and PT.release(8, None) is False


def test_tune_and_release_round_trip():
    fresh()
    m.proc(9, affinity=0xF0)
    r = PT.tune(9)
    assert r["ok"] and r["priority"] and r["throttling"]
    assert r["affinity"] == {"ok": True, "cores": 24, "was": 4, "changed": True}
    assert r["prior"] == {"priority": PT.NORMAL_PRIORITY_CLASS, "affinity": 0xF0}
    assert PT.release(9, r["prior"]) is True
    p = m.K.procs[9]
    assert (p["priority"], p["affinity"], p["throttle"]) == (PT.NORMAL_PRIORITY_CLASS, 0xF0, (0, 0))


def test_tune_without_all_cores():
    fresh()
    m.proc(10, affinity=0x3)
    r = PT.tune(10, all_cores=False)
    assert r["affinity"] is None and "affinity" not in r["prior"] and m.K.procs[10]["affinity"] == 0x3


def test_core_description():
    assert PT.describe_cores() == "24 cores (8 P + 16 E)"
    assert PT.describe_cores({"logical": 16, "p": 16, "e": 0, "hybrid": False}) == "16 cores"
    assert PT.describe_cores({"logical": 0}) == "every core"


def test_real_cpu_layout_adds_up():
    lay = m.real_cpu_layout()                       # read-only: asks Windows about this PC's cores
    assert lay["logical"] >= 1 and lay["p"] + lay["e"] == lay["logical"]
    assert lay["hybrid"] == (lay["e"] > 0)
