"""The tabs with their own undo: GPU (core/nvidia_clean.py), Network
(core/network_tweaks.py), Debloater tasks and services (core/debloater.py),
and the Privacy tab's start-up repair and hosts-file block (core/privacy.py)."""
import sandbox as m

import config
from core import reg_originals as RO

NC = m.app("core.nvidia_clean")
NT = m.app("core.network_tweaks")
DB = m.app("core.debloater")
PR = m.app("core.privacy")
OPT = m.app("core.optimizer")

NV_KEY = m.DISPLAY_CLASS + r"\0001"
MSI = (r"HKLM\SYSTEM\CurrentControlSet\Enum\PCI\VEN_10DE&DEV_2C05\GPU0\Device Parameters\Interrupt Management"
       r"\MessageSignaledInterruptProperties")
TCPIP = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
PROFILE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"


def gpu_machine(vendor):
    m.fresh()
    NC.detect_gpu = lambda: {"vendor": vendor, "name": f"{vendor} card", "driver": "1", "vram_mb": 16000}
    names = {"nvidia": "NVIDIA GeForce RTX 5070 Ti", "amd": "AMD Radeon RX 9070", "intel": "Intel(R) Arc(TM) B580"}
    m.PNP["Display"] = [{"InstanceId": r"PCI\VEN_10DE&DEV_2C05\GPU0", "FriendlyName": names[vendor]}]
    # \0000 is a virtual adapter on this PC; the real card is \0001.
    m.PS_HOOKS.append(lambda c: m.R("0000|Meta Virtual Monitor\n0001|" + names[vendor])
                      if "DriverDesc" in c else None)
    for t in NC._NV_TASKS + NC._AMD_TASKS + NC._INTEL_TASKS:
        m.task_add(t)
    m.make_key(m.DISPLAY_CLASS + r"\0000")
    m.make_key(NV_KEY)


# ═════════════════════════════════════════════════════════════════════
# GPU tab
# ═════════════════════════════════════════════════════════════════════
def test_nvidia_tweaks_apply_to_the_real_adapter_and_undo_exactly():
    gpu_machine("nvidia")
    m.rset(NV_KEY, "PowerMizerEnable", 0)           # this user's own driver settings
    m.rset(NV_KEY, "PerfLevelSrc", 0x3322)
    m.task_on(NC._NV_TASKS[0])
    m.TASKS[m._task_key(NC._NV_TASKS[1])]["enabled"] = False       # the user disabled this one
    before, system = m.flat(), m.snap_system()
    tasks_before = {t: m.task_on(t) for t in NC._NV_TASKS}

    rows = NC.apply_nvidia_tweaks()
    assert all(r["ok"] for r in rows), rows
    assert m.rdata(NV_KEY, "PerfLevelSrc") == 0x2222 and m.rdata(NV_KEY, "PowerMizerEnable") == 1
    assert m.rget(m.DISPLAY_CLASS + r"\0000", "PerfLevelSrc") is None, "not written to the wrong adapter"
    assert m.svc_get("NvTelemetryContainer") == "disabled"
    assert not any(m.task_on(t) for t in NC._NV_TASKS)
    assert m.rdata(MSI, "MSISupported") == 1
    assert RO.has_original(NV_KEY, "PerfLevelSrc", scope="gpu") and not RO.has_original(NV_KEY, "PerfLevelSrc")

    res = NC.undo_gpu_tweaks()
    assert res["ok"] and not res.get("nothing")
    assert "Restart the PC" in res["results"][-1]["name"]
    assert m.flat() == before, "every value back, and the ones the tweak created gone again"
    assert m.snap_system() == system, "the telemetry service has its start type back"
    assert {t: m.task_on(t) for t in NC._NV_TASKS} == tasks_before, "only tasks that were enabled are re-enabled"
    assert (RO.summary()["registry"], RO.summary()["state"]) == (0, 0)


def test_gpu_undo_with_nothing_on_record_says_so():
    gpu_machine("nvidia")
    res = NC.undo_gpu_tweaks()
    assert res["ok"] and res["nothing"] is True and "Nothing on record" in res["results"][0]["name"]


def test_vendor_tweaks_are_skipped_on_another_vendors_card():
    gpu_machine("amd")
    before = m.flat()
    assert NC.apply_nvidia_tweaks() == [{"name": "NVIDIA Detection", "ok": False}]
    assert NC.apply_intel_tweaks() == [{"name": "Intel Detection", "ok": False}]
    assert m.flat() == before and RO.summary()["state"] == 0


def test_amd_tweaks_undo_exactly():
    gpu_machine("amd")
    m.rset(m.DISPLAY_CLASS + r"\0000", "EnableUlps", 1)
    before, tasks = m.flat(), dict(m.snap_system()["tasks"])
    out = NC.apply_all_gpu_tweaks()
    assert out["gpu"]["vendor"] == "amd" and all(r["ok"] for r in out["tweaks"])
    assert m.rdata(m.DISPLAY_CLASS + r"\0000", "EnableUlps") == 0
    assert m.rdata(r"HKLM\SOFTWARE\AMD\CN", "UEPNotificationStatus") == 0
    assert m.task_on(r"\AMD\RadeonSoftwareStartupTask") is False
    assert NC.undo_gpu_tweaks()["ok"]
    assert m.flat() == before and m.snap_system()["tasks"] == tasks


def test_intel_tweaks_undo_exactly():
    gpu_machine("intel")
    before = m.flat()
    NC.apply_all_gpu_tweaks()
    assert m.rdata(MSI, "MSISupported") == 1 and m.task_on(NC._INTEL_TASKS[0]) is False
    NC.undo_gpu_tweaks()
    assert m.flat() == before and m.task_on(NC._INTEL_TASKS[0]) is True


def test_gpu_tweaks_are_not_undone_by_reset_all():
    gpu_machine("nvidia")
    m.rset(NV_KEY, "PowerMizerLevel", 0)
    NC.apply_nvidia_tweaks()
    OPT.reset_all_optimizations()
    assert m.rdata(NV_KEY, "PowerMizerLevel") == 1, "this tab has its own undo"
    assert RO.has_original(NV_KEY, "PowerMizerLevel", scope="gpu")
    NC.undo_gpu_tweaks()
    assert m.rdata(NV_KEY, "PowerMizerLevel") == 0


def test_reset_all_undoes_an_older_builds_power_mode_on_the_nvidia_adapters_own_key():
    r"""No record: a build that kept none.  The fixed defaults used to go to
    \0000 whatever adapter that was."""
    gpu_machine("nvidia")
    virtual = m.DISPLAY_CLASS + r"\0000"
    for key in (virtual, NV_KEY):
        m.rset(key, "PerfLevelSrc", 0x2222)
        m.rset(key, "PowerMizerLevel", 1)
        m.rset(key, "PowerMizerLevelAC", 1)
    before = m.flat()
    OPT.reset_all_optimizations()
    assert (m.rdata(NV_KEY, "PerfLevelSrc"), m.rdata(NV_KEY, "PowerMizerLevel"),
            m.rdata(NV_KEY, "PowerMizerLevelAC")) == (0x3322, 0, 0)
    assert {k: v for k, v in m.flat().items() if "0000" in k[1]} == {k: v for k, v in before.items() if "0000" in k[1]}, \
        "another adapter's key is not written to"

    gpu_machine("amd")                              # no NVIDIA card at all
    m.rset(virtual, "PowerMizerLevel", 1)
    m.rset(NV_KEY, "PowerMizerLevel", 1)
    before = m.flat()
    OPT.reset_all_optimizations()
    assert m.flat() == before


# ═════════════════════════════════════════════════════════════════════
# Network tab
# ═════════════════════════════════════════════════════════════════════
def net_machine():
    m.fresh()
    m.rset(TCPIP, "TcpTimedWaitDelay", 120)         # this user's own values
    m.rset(PROFILE, "SystemResponsiveness", 20)
    m.rset(rf"{m.IFACES_KEY}\{m.IFACES[0]}", "TcpAckFrequency", 2)


def test_network_optimize_then_undo_is_exact():
    net_machine()
    before, system = m.flat(), m.snap_system()
    out = NT.run_full_network_optimize()
    assert set(out) == {"nagle", "tcp", "throttle", "lso", "adapter", "netsh", "netbios_teredo", "rss_rsc",
                        "game_port_qos"}
    for guid in m.IFACES:
        assert m.rdata(rf"{m.IFACES_KEY}\{guid}", "TCPNoDelay") == 1
        assert m.rdata(rf"HKLM\SYSTEM\CurrentControlSet\Services\NetBT\Parameters\Interfaces\Tcpip_{guid}",
                       "NetbiosOptions") == 2
    assert m.rdata(TCPIP, "TcpTimedWaitDelay") == 30 and m.rdata(PROFILE, "NetworkThrottlingIndex") == 0xFFFFFFFF
    assert m.TCP["rsc"] == "disabled" and m.STACK == {"teredo": "disabled", "6to4": "disabled",
                                                       "isatap": "disabled", "heuristics": "disabled"}
    assert len(m.ps_ran("New-NetQosPolicy")) == len(NT._GAME_PORT_RULES)
    assert RO.has_original(TCPIP, "TcpTimedWaitDelay", scope="network")

    m.clear_logs()
    res = NT.undo_network_tweaks()
    assert res["ok"] and res["nothing"] is False and res["reboot_recommended"] is True
    assert m.flat() == before and m.snap_system() == system
    removed = m.ps_ran("-NetQosPolicy -Name")
    assert removed and f'"{config.APP_NAME}_Steam_UDP"' in removed[0]
    assert f'"{config.LEGACY_APP_NAME}_Steam_UDP"' in removed[0], "rules an older build made go too"
    assert not m.ran("netsh", "winsock") and not [c for c in m.CMDS if c[0] == "netsh" and "reset" in c], \
        "the gentle undo: nothing is reset wholesale"
    assert (RO.summary()["registry"], RO.summary()["state"]) == (0, 0)


def test_network_undo_with_nothing_on_record_says_so():
    net_machine()
    res = NT.undo_network_tweaks()
    assert res["ok"] and res["nothing"] is True and res["reboot_recommended"] is False
    assert "Nothing on record" in res["results"][0]["name"]


def test_restore_networking_is_a_recovery_and_records_nothing():
    net_machine()
    m.rset(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters", "DisabledComponents", 0xFF)
    for svc in ("WlanSvc", "WwanSvc", "dot3svc", "netprofm", "NlaSvc", "nsi", "NetSetupSvc"):
        m.svc_set(svc, "disabled")
    res = NT.restore_networking()
    assert res["ok"] and all(r["ok"] for r in res["results"])
    assert m.rdata(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters", "DisabledComponents") == 0
    assert m.svc_get("WlanSvc") == "auto" and "wlansvc" in m.RUNNING
    assert m.ran("netsh", "winsock", "reset")
    assert RO.summary() == {"registry": 0, "power": 0, "state": 0, "notes": {}}, "a recovery is not a tweak"


def test_prefer_ipv4_is_recorded_and_keeps_ipv6_bound():
    net_machine()
    key = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters"
    NT.disable_ipv6()
    assert m.rdata(key, "DisabledComponents") == 32, "0x20 = prefer IPv4; 0x11 disabled IPv6 outright"
    assert m.ps_ran("Enable-NetAdapterBinding") and not m.ps_ran("Disable-NetAdapterBinding")
    NT.undo_network_tweaks()
    assert m.rget(key, "DisabledComponents") is None


def test_network_undo_still_works_after_reset_all():
    net_machine()
    NT.run_full_network_optimize()
    OPT.reset_all_optimizations()
    NT.undo_network_tweaks()
    assert m.rdata(TCPIP, "TcpTimedWaitDelay") == 120 and m.TCP["rsc"] == "enabled"
    assert m.STACK["teredo"] == "client"


def test_per_app_priority_rejects_names_that_could_run_code():
    net_machine()
    for bad in ('x.exe"; calc; "', "a$(calc).exe", "noext", "", "a`b.exe"):
        assert NT.set_per_app_net_priority(bad)["ok"] is False
    assert not m.ps_ran("New-NetQosPolicy")
    r = NT.set_per_app_net_priority("My Game.exe")
    assert r["ok"] and r["rule"] == f"{config.APP_NAME}_Prio_My_Game"
    assert m.ps_ran("New-NetQosPolicy", "My Game.exe")
    assert config.APP_NAME in NT.qos_policy_filter() and config.LEGACY_APP_NAME in NT.qos_policy_filter("Prio")


# ═════════════════════════════════════════════════════════════════════
# Debloater
# ═════════════════════════════════════════════════════════════════════
def test_debloater_tasks_are_recorded_and_restored():
    m.fresh()
    for t in config.SCHEDULED_TASKS_TO_DISABLE:
        if m.task_on(t) is None and t != m.MISSING_TASK:
            m.task_add(t)
    before = dict(m.snap_system()["tasks"])
    rows = DB.disable_all_scheduled_tasks()
    assert len(rows) == len(config.SCHEDULED_TASKS_TO_DISABLE)
    assert [r["task"] for r in rows if not r["ok"]] == [m.MISSING_TASK], "a task this Windows lacks is reported"
    assert not any(m.task_on(t) for t in config.SCHEDULED_TASKS_TO_DISABLE)
    assert RO.has_state("task", scope="debloat") and not RO.has_state("task")
    res = DB.restore_scheduled_tasks()
    enabled_before = sum(1 for t in config.SCHEDULED_TASKS_TO_DISABLE if before.get(t))
    assert res == {"ok": True, "restored": enabled_before, "failed": 0,
                   "results": [{"name": f"{enabled_before} scheduled task(s) re-enabled", "ok": True}]}
    assert m.snap_system()["tasks"] == before, "the task the user had disabled is still disabled"


def test_debloater_services_go_back_to_their_own_start_type():
    m.fresh()
    DB.disable_service("DiagTrack")                 # automatic
    DB.disable_service("WSearch")                   # automatic (delayed start)
    DB.disable_service("lfsvc")                     # manual
    DB.disable_service("DiagTrack")                 # again: must not record "disabled" as the original
    assert m.svc_get("DiagTrack") == "disabled" and m.svc_get("lfsvc") == "disabled"
    res = DB.restore_services()
    assert res["ok"] and res["restored"] == res["total"]
    assert m.svc_get("DiagTrack") == "auto" and m.svc_get("lfsvc") == "demand"
    assert m.svc_get("WSearch") in ("auto", "delayed-auto")
    assert DB._load_service_backup() == {}


def test_reset_all_restores_debloated_services_and_tasks():
    m.fresh()
    DB.disable_service("SysMain")
    DB.disable_scheduled_task(m.TELEMETRY_TASKS[0])
    res = OPT.reset_all_optimizations()
    assert m.svc_get("SysMain") == "auto" and m.task_on(m.TELEMETRY_TASKS[0]) is True
    assert res["debloat_services"] and all(r["ok"] for r in res["debloat_services"])


def test_a_service_the_user_disabled_themselves_is_not_switched_back_on():
    m.fresh()
    m.svc_set("DiagTrack", "disabled")              # the user's own doing: there is no record
    m.svc_set("SysMain", "disabled")
    DB.disable_service("DiagTrack")                 # the debloater finds it disabled: nothing to record
    DB.disable_service("lfsvc")                     # this one it does disable
    res = DB.restore_services()
    assert m.svc_get("lfsvc") == "demand" and (res["restored"], res["total"]) == (1, 1)
    assert m.svc_get("DiagTrack") == "disabled" and m.svc_get("SysMain") == "disabled", \
        "both were set to Manual: 'disabled, no record' was taken for a lost record"
    assert res["unrecorded"] == ["DiagTrack", "SysMain"]

    m.clear_logs()
    out = OPT.reset_all_optimizations()             # ...which is what every Reset All did, too
    assert m.svc_get("DiagTrack") == "disabled" and m.svc_get("SysMain") == "disabled"
    assert not m.ran("sc", "config")
    assert "DiagTrack, SysMain" in out["debloat_services"][-1]["name"] and out["debloat_services"][-1]["ok"]


# ═════════════════════════════════════════════════════════════════════
# Privacy
# ═════════════════════════════════════════════════════════════════════
def test_startup_repair_leaves_a_stock_machine_alone():
    """This runs at every launch.  It used to set DiagTrack to manual and
    InstallService to automatic unconditionally - each time the app started."""
    m.fresh()
    PR.restore_store_updates()
    assert not m.ran("sc", "config")
    assert (m.svc_get("DiagTrack"), m.svc_get("AppXSvc"), m.svc_get("InstallService")) == ("auto", "demand", "demand")


def test_startup_repair_only_fixes_services_that_are_disabled():
    m.fresh()
    for svc in ("DiagTrack", "AppXSvc", "InstallService"):
        m.svc_set(svc, "disabled")
    policy = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy"
    m.rset(policy, "LetAppsRunInBackground", 2)
    out = PR.restore_store_updates()
    assert out["ok"]
    assert (m.svc_get("DiagTrack"), m.svc_get("AppXSvc"), m.svc_get("InstallService")) == ("demand", "demand", "auto")
    assert m.rget(policy, "LetAppsRunInBackground") is None


def test_hosts_block_keeps_what_is_above_and_below_it():
    m.fresh()
    mine = "127.0.0.1 localhost\n10.0.0.5 nas.home\n"
    with open(PR.HOSTS_PATH, "w", encoding="utf-8") as f:
        f.write(mine)
    assert PR.HOSTS_PATH.startswith(m.ROOT), "never the real hosts file"
    assert PR.apply_hosts_blocking() == {"ok": True, "count": len(config.TELEMETRY_HOSTS)}
    with open(PR.HOSTS_PATH, "a", encoding="utf-8") as f:
        f.write("10.0.0.6 printer.home\n")          # added below the block afterwards
    assert PR.get_hosts_status() == {"active": True, "blocked_count": len(config.TELEMETRY_HOSTS)}
    PR.apply_hosts_blocking()                       # again: the old block is replaced, not the file truncated
    text = open(PR.HOSTS_PATH, encoding="utf-8").read()
    assert text.count(PR.HOSTS_MARKER) == 2 and "printer.home" in text and "nas.home" in text
    assert PR.remove_hosts_blocking() == {"ok": True}
    text = open(PR.HOSTS_PATH, encoding="utf-8").read()
    assert "0.0.0.0" not in text and "nas.home" in text and "printer.home" in text
    assert PR.get_hosts_status()["active"] is False


def test_hosts_block_from_before_the_rename_is_recognised_and_removed():
    m.fresh()
    with open(PR.HOSTS_PATH, "w", encoding="utf-8") as f:
        f.write(f"127.0.0.1 localhost\n{PR._LEGACY_HOSTS_MARKER}\n0.0.0.0 vortex.data.microsoft.com\n"
                f"{PR._LEGACY_HOSTS_MARKER}\n10.0.0.5 nas.home\n")
    assert PR.get_hosts_status()["active"] is True
    PR.remove_hosts_blocking()
    text = open(PR.HOSTS_PATH, encoding="utf-8").read()
    assert config.LEGACY_APP_NAME not in text and "0.0.0.0" not in text and "nas.home" in text


def test_strip_hosts_block_with_a_missing_closing_marker():
    text, found = PR._strip_hosts_block(f"a\n{PR.HOSTS_MARKER}\n0.0.0.0 x\n")
    assert found and text.strip() == "a"
    assert PR._strip_hosts_block("just\nlines\n") == ("just\nlines\n", False)
