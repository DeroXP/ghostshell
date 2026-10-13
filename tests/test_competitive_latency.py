"""core/competitive_latency.py - the per-session low-latency mode: reset()
returns every value to what it was, not to a fixed "Windows default"."""
import sandbox as m

import json

from core import reg_originals as RO

TM = m.app("core.timer_manager")
CL = m.app("core.competitive_latency")
OPT = m.app("core.optimizer")

MOUSE = r"HKCU\Control Panel\Mouse"
STORE = r"HKCU\System\GameConfigStore"
CAPTURE = r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR"
PRIO = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
THROTTLE = r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling"
MOUCLASS = r"HKLM\SYSTEM\CurrentControlSet\Services\mouclass\Parameters"
KBDCLASS = r"HKLM\SYSTEM\CurrentControlSet\Services\kbdclass\Parameters"
DX = r"HKCU\Software\Microsoft\DirectX\UserGpuPreferences"
USB = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
PCIE = "ee12f906-d277-404b-b6da-e5fa1a576df5"


def fresh(accel=True):
    m.fresh()
    TM._timer_hold["active"] = False
    m.rset(MOUSE, "MouseSpeed", "1" if accel else "0")
    m.rset(MOUSE, "MouseThreshold1", "6" if accel else "0")
    m.rset(MOUSE, "MouseThreshold2", "10" if accel else "0")
    m.rset(PRIO, "Win32PrioritySeparation", 2)


def test_apply_sets_the_whole_profile():
    fresh()
    assert CL.apply()["ok"]
    assert [m.rdata(MOUSE, n) for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")] == ["0", "0", "0"]
    assert m.rdata(STORE, "GameDVR_Enabled") == 0 and m.rdata(CAPTURE, "AppCaptureEnabled") == 0
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 38 and m.rdata(THROTTLE, "PowerThrottlingOff") == 1
    assert m.rdata(MOUCLASS, "MouseDataQueueSize") == 20 and m.rdata(KBDCLASS, "KeyboardDataQueueSize") == 20
    assert m.rdata(DX, "DirectXUserGlobalSettings") == "SwapEffectUpgradeEnable=1;"
    assert m.cell(m.USERPLAN, m.PROC_MIN) == [100, 100] and m.cell(m.USERPLAN, m.PROC_MAX) == [100, 100]
    assert m.cell(m.USERPLAN, USB) == [0, 0] and m.cell(m.USERPLAN, PCIE) == [0, 0]
    assert m.SPI[-1] == (4, [0, 0, 0]), "mouse acceleration switched off live, not at the next sign-in"
    assert m.TIMER[-1] == (5000, True) and TM.is_timer_hold_active()
    assert json.load(open(CL.SETTINGS_PATH)) == {"enabled": True, "recorded": True}
    assert RO.summary()["power"] == 4 and RO.has_original(MOUSE, "MouseSpeed", scope="competitive")
    assert not RO.has_original(MOUSE, "MouseSpeed"), "its own scope: Reset All does not undo a running session"


def test_reset_returns_every_value_to_what_it_was():
    fresh()
    m.rset(DX, "DirectXUserGlobalSettings", "VRROptimizeEnable=0;")
    m.cell(m.USERPLAN, m.PROC_MIN)[:] = [5, 20]
    before, plan = m.flat(), m.plan_values(m.USERPLAN)
    CL.apply()
    assert m.rdata(DX, "DirectXUserGlobalSettings") == "VRROptimizeEnable=0;SwapEffectUpgradeEnable=1;"
    CL.reset()
    assert m.flat() == before, "values that did not exist are gone again; the rest are the user's"
    assert m.cell(m.USERPLAN, m.PROC_MIN) == [5, 20] and m.cell(m.USERPLAN, m.PROC_MAX) == [5, 6]
    assert m.cell(m.USERPLAN, USB) == [5, 6] and m.cell(m.USERPLAN, PCIE) == [5, 6], \
        "min/max CPU state and PCIe were never reverted at all before"
    assert all(m.cell(m.USERPLAN, s) == v for s, v in plan.items())
    assert m.SPI[-1] == (4, [6, 10, 1]), "this user's mouse acceleration is live again"
    assert m.TIMER[-1] == (5000, False) and not TM.is_timer_hold_active()
    assert RO.summary() == {"registry": 0, "power": 0, "state": 0, "notes": {}}
    assert json.load(open(CL.SETTINGS_PATH)) == {"enabled": False, "recorded": False}


def test_a_player_with_pointer_precision_off_keeps_it_off():
    """reset() runs at every game exit for low-latency targets.  It used to
    write 'Windows defaults' - switching mouse acceleration ON and Game DVR
    back on after each session."""
    fresh(accel=False)
    m.rset(STORE, "GameDVR_Enabled", 0)
    CL.apply()
    CL.reset()
    assert [m.rdata(MOUSE, n) for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")] == ["0", "0", "0"]
    assert m.rdata(STORE, "GameDVR_Enabled") == 0
    assert m.SPI[-1] == (4, [0, 0, 0])


def test_applying_twice_still_restores_the_first_values():
    fresh()
    CL.apply()
    CL.apply()
    assert json.load(open(CL.SETTINGS_PATH))["recorded"] is True
    CL.reset()
    assert m.rdata(MOUSE, "MouseSpeed") == "1" and m.rdata(PRIO, "Win32PrioritySeparation") == 2


def test_mode_left_on_by_an_older_build_falls_back_to_fixed_defaults():
    fresh(accel=False)
    CL.atomic_write_json(CL.SETTINGS_PATH, {"enabled": True})       # no "recorded": nothing was noted
    m.rset(PRIO, "Win32PrioritySeparation", 38)
    m.rset(THROTTLE, "PowerThrottlingOff", 1)
    m.rset(MOUCLASS, "MouseDataQueueSize", 20)
    CL.reset()
    assert [m.rdata(MOUSE, n) for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")] == ["1", "6", "10"]
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 2 and m.rget(THROTTLE, "PowerThrottlingOff") is None
    assert m.rget(MOUCLASS, "MouseDataQueueSize") is None and m.cell(m.USERPLAN, USB)[0] == 1
    assert RO.summary()["registry"] == 0 and RO.summary()["power"] == 0, "a reset never records"


def test_reapplying_over_an_older_builds_session_is_not_called_recorded():
    fresh()
    CL.atomic_write_json(CL.SETTINGS_PATH, {"enabled": True})
    CL.apply()                                      # finds the values already changed: nothing left to record
    assert json.load(open(CL.SETTINGS_PATH)) == {"enabled": True, "recorded": False}


def test_optimizer_run_during_a_session_keeps_its_value_at_game_exit():
    fresh()
    CL.apply()                                      # a low-latency game starts: 2 -> 38
    OPT.run_full_optimize(["cpu"])                  # the user runs the optimizer: writes 38 as well
    CL.reset()                                      # the game closes
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 38, "the optimizer's tweak stays"
    OPT.reset_all_optimizations()
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 2, "and Reset All returns the user's value, not 38"


def test_status_and_fps_cap():
    fresh()
    m.PS_HOOKS.append(lambda c: m.R("240") if "EnumDisplaySettings" in c else None)
    assert CL.status()["flip_model_enabled"] is False
    CL.apply()
    s = CL.status()
    assert s["enabled"] and s["flip_model_enabled"] and s["timer_hold_active"] and s["timer_resolution_ms"] == 0.5
    assert s["refresh_hz"] == 240 and s["recommended_fps_cap"] == 235
    assert CL.recommended_fps_cap(144) == 141 and CL.recommended_fps_cap(None) is None


def test_timer_hold_is_reasserted_at_startup_only_when_the_mode_is_on():
    fresh()
    CL.apply_on_boot()
    assert m.TIMER == []
    CL.apply()
    del m.TIMER[:]
    CL.apply_on_boot()
    assert m.TIMER == [(5000, True)]
