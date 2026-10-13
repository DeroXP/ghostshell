"""core/boot_prep.py - the tweaks applied every time the app starts: they are
recorded like any other, Reset All undoes them, and they stay off afterwards
until asked for again."""
import sandbox as m

import os

from core import reg_originals as RO

BP = m.app("core.boot_prep")
OPT = m.app("core.optimizer")

GAMES = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
PROFILE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
PRIO = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
DESKTOP = r"HKCU\Control Panel\Desktop"
USER = {(GAMES, "GPU Priority"): 2, (GAMES, "Priority"): 2, (GAMES, "Scheduling Category"): "Medium",
        (GAMES, "SFIO Priority"): "Normal", (PRIO, "Win32PrioritySeparation"): 2,
        (DESKTOP, "ForegroundLockTimeout"): 200000, (PROFILE, "NetworkThrottlingIndex"): 10,
        (PROFILE, "SystemResponsiveness"): 20}
TWEAKED = {(GAMES, "GPU Priority"): 8, (GAMES, "Priority"): 6, (GAMES, "Scheduling Category"): "High",
           (GAMES, "SFIO Priority"): "High", (PRIO, "Win32PrioritySeparation"): 38,
           (DESKTOP, "ForegroundLockTimeout"): 0, (PROFILE, "NetworkThrottlingIndex"): 4294967295,
           (PROFILE, "SystemResponsiveness"): 10}


def fresh(plan=m.USERPLAN):
    m.wait(lambda: not BP._state["running"])
    m.fresh(plan=plan)
    BP._state.update(running=False, done=False, steps=[], started_at=0, finished_at=0)
    for (key, name), data in USER.items():
        m.rset(key, name, data)


def run_prep(force=False):
    BP.prep_for_gaming_async(force=force)
    assert m.wait(lambda: BP._state["done"] and not BP._state["running"])


def now():
    return {(key, name): m.rdata(key, name) for key, name in USER}


def test_first_launch_applies_and_records_the_tweaks():
    fresh()
    run_prep()
    assert now() == TWEAKED
    assert all(RO.has_original(key, name) for key, name in USER), "these used to be written with no record at all"
    assert [s["name"] for s in BP._state["steps"]][:3] == ["Flush DNS cache", "Trim working sets", "Clear standby list"]


def test_the_users_plan_is_noted_before_switching_to_high_performance():
    fresh(plan=m.USERPLAN)
    run_prep()
    assert m.PLAN["active"] == m.HIGH_PERF
    assert OPT._read_power_baseline() == m.USERPLAN, "nothing used to: Reset All then 'returned' to our own plan"


def test_reset_all_undoes_the_startup_tweaks():
    fresh()
    run_prep()
    OPT.reset_all_optimizations()
    assert now() == USER, "back to this user's values"
    assert m.PLAN["active"] == m.USERPLAN


def test_after_reset_all_the_next_launch_leaves_the_system_alone():
    fresh()
    run_prep()
    OPT.reset_all_optimizations()
    assert BP.get_status()["persistent"] is False
    BP._state.update(done=False, steps=[])          # the app is started again
    m.clear_logs()
    run_prep()
    assert now() == USER and m.PLAN["active"] == m.USERPLAN
    assert not m.ran("reg", "add") and not m.ran("powercfg", "setactive")
    assert m.ran("ipconfig", "/flushdns"), "the harmless per-launch steps still run"
    assert len(BP._state["steps"]) == 3


def test_rerun_prep_turns_the_tweaks_back_on():
    fresh()
    BP.set_persistent_allowed(False)
    run_prep(force=True)                            # the "Re-run Prep" button
    assert BP.get_status()["persistent"] is True and now() == TWEAKED


def test_prep_does_not_drop_the_ultimate_plan_to_high_performance():
    fresh()
    clone = OPT.apply_ultimate_power_plan()["guid"]
    m.clear_logs()
    run_prep()
    assert m.PLAN["active"] == clone and not m.ran("powercfg", "setactive")


def test_two_quick_calls_run_the_prep_once():
    fresh()
    BP.prep_for_gaming_async()
    BP.prep_for_gaming_async()
    assert m.wait(lambda: BP._state["done"] and not BP._state["running"])
    assert len(m.ran("ipconfig", "/flushdns")) == 1


def test_prep_is_not_repeated_once_done():
    fresh()
    run_prep()
    m.clear_logs()
    BP.prep_for_gaming_async()                      # e.g. the window is re-opened from the tray
    assert not m.CMDS and BP._state["done"]


def test_done_callback_gets_the_duration():
    fresh()
    seen = []
    BP.prep_for_gaming_async(on_done=seen.append)
    assert m.wait(lambda: bool(seen)) and seen[0] >= 0


def test_persistent_setting_survives_a_damaged_file():
    fresh()
    with open(BP._SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("[]")
    assert BP._persistent_allowed() is True
    os.remove(BP._SETTINGS_PATH)
    assert BP._persistent_allowed() is True, "allowed by default"
