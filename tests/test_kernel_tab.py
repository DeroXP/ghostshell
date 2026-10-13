"""The Kernel tab: VBS / Memory Integrity toggle (core/vbs_toggle.py), timer
tweaks (core/timer_manager.py) and scheduler tweaks (core/scheduler_tweaks.py)."""
import sandbox as m

from core import reg_originals as RO

V = m.app("core.vbs_toggle")
TM = m.app("core.timer_manager")
ST = m.app("core.scheduler_tweaks")
OPT = m.app("core.optimizer")

DG = r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard"
HVCI = DG + r"\Scenarios\HypervisorEnforcedCodeIntegrity"
CG = DG + r"\Scenarios\CredentialGuard"
VBS = "EnableVirtualizationBasedSecurity"
GAMES = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
PRIO = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"


def fresh(vbs=1, hvci=1, cg=1, locked=None, vmp=True):
    m.fresh()
    TM._timer_hold["active"] = False
    m.rset(DG, VBS, vbs)
    m.rset(HVCI, "Enabled", hvci)
    m.rset(CG, "Enabled", cg)
    if locked is not None:
        m.rset(HVCI, "Locked", locked)
    m.FEATURES["VirtualMachinePlatform"] = vmp


def state():
    return (m.rdata(DG, VBS), m.rdata(HVCI, "Enabled"), m.rdata(CG, "Enabled"), m.rdata(HVCI, "Locked"),
            m.FEATURES["VirtualMachinePlatform"])


# ── VBS / Memory Integrity ────────────────────────────────────────────
def test_disable_and_enable_vbs_round_trip():
    fresh()
    assert V.disable_vbs()["ok"]
    assert state() == (0, 0, 0, None, False)
    assert V.enable_vbs() == {"ok": True, "reboot_required": True}
    assert state() == (1, 1, 1, None, True), "the VM platform comes back too: WSL2 and Docker need it"
    assert RO.summary() == {"registry": 0, "power": 0, "state": 0, "notes": {}}


def test_enable_vbs_does_not_switch_on_what_was_off_before():
    fresh(cg=0, vmp=False)
    V.disable_vbs()
    V.enable_vbs()
    assert state() == (1, 1, 0, None, False), "Credential Guard and the VM platform were off: they stay off"
    assert not m.ps_ran("Enable-WindowsOptionalFeature")


def test_enable_vbs_with_nothing_on_record():
    fresh(vbs=0, hvci=0, cg=0)                      # disabled by a build that kept no record
    assert V.enable_vbs()["ok"]
    assert state()[:2] == (1, 1), "what the user asked for is on, record or no record"


def test_disable_and_enable_memory_integrity_round_trip():
    fresh(locked=1)
    V.disable_hvci()
    assert (m.rdata(HVCI, "Enabled"), m.rdata(HVCI, "Locked")) == (0, 0)
    V.enable_hvci()
    assert (m.rdata(HVCI, "Enabled"), m.rdata(HVCI, "Locked")) == (1, 1), "the Locked flag goes back to what it was"
    assert m.rdata(DG, VBS) == 1, "VBS itself was never touched"


def test_enable_memory_integrity_when_only_the_lock_flag_is_on_record():
    fresh(hvci=0, locked=1)                         # Enabled was already 0: only Locked gets recorded
    V.disable_hvci()
    assert RO.has_original(HVCI, "Locked", scope="vbs") and not RO.has_original(HVCI, "Enabled", scope="vbs")
    assert V.enable_hvci()["ok"]
    assert m.rdata(HVCI, "Enabled") == 1, "this used to return without enabling anything"


def test_vbs_records_are_not_touched_by_reset_all():
    fresh()
    V.disable_vbs()
    OPT.reset_all_optimizations()
    assert state()[:3] == (0, 0, 0), "its own switch, its own undo"
    V.enable_vbs()
    assert state()[:3] == (1, 1, 1)


def test_vbs_status_reads_the_registry_state():
    fresh()
    s = V.get_vbs_status()
    assert s["hvci_enabled"] is True and s["vbs_enabled"] is True and s["vm_platform_enabled"] is True
    assert s["has_mbec"] is True and s["estimated_impact"] == "5-10% FPS"
    V.disable_vbs()
    s = V.get_vbs_status()
    assert s["hvci_enabled"] is False and s["vbs_enabled"] is False and s["vm_platform_enabled"] is False


# ── timer tweaks ──────────────────────────────────────────────────────
def test_timer_tweaks_restore_the_users_boot_settings():
    fresh()
    rows = TM.apply_timer_tweaks()
    assert all(r["ok"] for r in rows)
    assert m.bcd_get("disabledynamictick") == "Yes" and m.bcd_get("useplatformclock") == "No"
    assert m.ps_ran("Disable-PnpDevice")
    rows = TM.restore_timer_defaults()
    assert all(r["ok"] for r in rows)
    assert m.bcd_get("disabledynamictick") is None, "was not set before: removed again"
    assert m.bcd_get("useplatformclock") == "Yes", "the user's own setting, not deleted"
    assert m.ps_ran("Enable-PnpDevice")


def test_timer_restore_without_records_deletes_the_overrides():
    fresh()
    m.BCD.update({"disabledynamictick": "Yes", "useplatformclock": "No"})       # left by an older build
    TM.restore_timer_defaults()
    assert m.bcd_get("disabledynamictick") is None and m.bcd_get("useplatformclock") is None


def test_timer_tweak_options_are_independent():
    fresh()
    TM.apply_timer_tweaks(disable_dynamic_tick=False, disable_platform_clock=True, disable_hpet=False)
    assert m.bcd_get("disabledynamictick") is None and m.bcd_get("useplatformclock") == "No"
    assert not m.ps_ran("Disable-PnpDevice")


def test_timer_tweaks_are_also_undone_by_reset_all():
    fresh()
    TM.apply_timer_tweaks()
    OPT.reset_all_optimizations()
    assert m.bcd_get("disabledynamictick") is None and m.bcd_get("useplatformclock") == "Yes"


def test_timer_status():
    fresh()
    s = TM.get_timer_status()
    assert s["dynamic_tick"] is True and s["platform_clock"] is True and s["current_resolution_ms"] == 15.625
    TM.apply_timer_tweaks()
    s = TM.get_timer_status()
    assert s["dynamic_tick"] is False and s["platform_clock"] is False


def test_timer_resolution_hold():
    fresh()
    assert TM.start_timer_resolution_hold(0.5) == {"ok": True, "resolution_ms": 0.5}
    assert TM.is_timer_hold_active() and m.TIMER == [(5000, True)]
    assert TM.stop_timer_resolution_hold() == {"ok": True}
    assert not TM.is_timer_hold_active() and m.TIMER[-1] == (5000, False)


# ── scheduler tweaks ──────────────────────────────────────────────────
def test_scheduler_status_sees_the_hidden_idle_setting():
    fresh()
    m.rset(PRIO, "Win32PrioritySeparation", 0x26)
    m.rset(GAMES, "GPU Priority", 8)
    m.rset(GAMES, "Scheduling Category", "High")
    s = ST.get_scheduler_status()
    assert s["priority_sep"] == "0x26" and s["idle_disabled"] is False and s["hybrid_cpu"] is True
    assert s["mmcss_games"]["Scheduling Category"] == "High"
    m.cell(m.USERPLAN, m.IDLE)[0] = 1
    assert ST.get_scheduler_status()["idle_disabled"] is True, "`powercfg /query` never lists it: this always said enabled"


def test_cpu_idle_toggle_is_undone_by_reset_all():
    fresh()
    assert ST.toggle_cpu_idle(True) == {"ok": True, "idle_disabled": True}
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0] and RO.has_power("SUB_PROCESSOR", "IdleDisable")
    assert ST.toggle_cpu_idle(False)["idle_disabled"] is False and m.cell(m.USERPLAN, m.IDLE) == [0, 0]
    ST.toggle_cpu_idle(True)
    RO.restore_all()
    assert m.cell(m.USERPLAN, m.IDLE) == [0, 0]


def test_full_scheduler_optimize_is_recorded_and_undone_by_reset_all():
    fresh()
    m.rset(PRIO, "Win32PrioritySeparation", 0x26)
    m.rset(GAMES, "Priority", 2)
    m.rset(GAMES, "Scheduling Category", "Medium")
    kernel = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel"
    dwm = r"HKCU\Software\Microsoft\Windows\DWM"
    ST.run_full_scheduler_optimize()
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 40 and m.rdata(GAMES, "Priority") == 6
    assert m.rdata(kernel, "ThreadDpcEnable") == 0 and m.rdata(dwm, "OverlayMinFPS") == 0
    assert m.cell(m.USERPLAN, m.IDLE)[0] == 1
    OPT.reset_all_optimizations()
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 0x26 and m.rdata(GAMES, "Priority") == 2
    assert m.rdata(GAMES, "Scheduling Category") == "Medium"
    assert m.rget(kernel, "ThreadDpcEnable") is None and m.rget(dwm, "OverlayMinFPS") is None
    assert m.rget(GAMES, "SFIO Rate") is None, "created by the tweak, gone again"
    assert m.cell(m.USERPLAN, m.IDLE) == [0, 0]


def test_priority_separation_modes():
    fresh()
    assert ST.apply_priority_separation("balanced")["value"] == "38"
    assert ST.apply_priority_separation("default")["value"] == "2"
    assert ST.apply_priority_separation("nonsense")["value"] == "40"
    assert m.rdata(PRIO, "Win32PrioritySeparation") == 40
