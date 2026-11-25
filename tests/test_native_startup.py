"""The start-up readings through the native helper (step 2 of the move to
Rust), and the two start-up fixes that came with it.

Each reading is fed the same fake machine twice, once the old way (PowerShell,
nvidia-smi, reg, powercfg, sc, tasklist) and once through the helper, and the
answers are compared.  With the helper answering, nothing is started.  Without
it, or when it gives no answer, the old way runs exactly as before.

The helper's two actions (they free memory) are checked for the same things,
and that they are only ever asked for by the start-up preparation.

The real library is not loaded here: m.NATIVE and m.NATIVE_ACTS answer for it.
"""
import sandbox as m

import inspect
import json
import re
import subprocess
import threading
import time
import types

from core import reg_originals as RO, system_info as SI, utils as U

BP = m.app("core.boot_prep")
GP = m.app("core.game_profiles")
OPT = m.app("core.optimizer")
from core import debloater as DB, dependencies as DEP, driver_updater as DU, net_monitor as NM     # noqa: E402
import app as A                                                                                    # noqa: E402

GIB = 1024 ** 3
MIB = 1048576


# ═════════════════════════════════════════════════════════════════════
# One fake PC, readable through PowerShell and through the helper
# ═════════════════════════════════════════════════════════════════════
def desktop() -> dict:
    """WMI's view of a desktop with an NVIDIA card, class by class."""
    return {
        "Win32_OperatingSystem": [{"Caption": "Microsoft Windows 11 Home", "Version": "10.0.26300",
                                   "BuildNumber": "26300", "OSArchitecture": "64-bit",
                                   "TotalVisibleMemorySize": 66_900_000, "FreePhysicalMemory": 40_140_000}],
        "Win32_Processor": [{"Name": "Intel(R) Core(TM) Ultra 9 285K  ", "NumberOfCores": 24,
                             "NumberOfLogicalProcessors": 24, "MaxClockSpeed": 3700, "CurrentClockSpeed": 3650}],
        "Win32_PhysicalMemory": [{"Capacity": 32 * GIB, "Speed": 6000, "ConfiguredClockSpeed": 5600},
                                 {"Capacity": 32 * GIB, "Speed": 6000, "ConfiguredClockSpeed": 5600}],
        "Win32_LogicalDisk": [{"DeviceID": "C:", "Size": 1999 * GIB, "FreeSpace": 512 * GIB},
                              {"DeviceID": "D:", "Size": 3725 * GIB, "FreeSpace": 3000 * GIB}],
        "Win32_VideoController": [{"Name": "NVIDIA GeForce RTX 5070 Ti", "DriverVersion": "32.0.15.2140",
                                   "AdapterRAM": 4293918720, "PNPDeviceID": r"PCI\\VEN_10DE&DEV_2C05\\4&1",
                                   "Status": "OK"}],
        "MSFT_DNSClientServerAddress": [{"InterfaceAlias": "Loopback Pseudo-Interface 1", "ServerAddresses": []},
                                        {"InterfaceAlias": "Ethernet", "ServerAddresses": ["1.1.1.1", "1.0.0.1"]},
                                        {"InterfaceAlias": "Wi-Fi", "ServerAddresses": ["192.168.1.1"]}],
        "MSFT_PhysicalDisk": [{"MediaType": 4, "BusType": 17}, {"MediaType": 3, "BusType": 11}],
    }


NVIDIA = {"name": "NVIDIA GeForce RTX 5070 Ti", "driver_version": "621.40", "mem_total_bytes": 17094934528}
_MEDIA = {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}
_BUS = {11: "SATA", 17: "NVMe", 7: "USB", 8: "RAID"}


def _pick(row: dict, names: list) -> dict:
    return {n: row.get(n) for n in names}


def install(pc: dict, helper: bool, nvidia=NVIDIA) -> None:
    """Put `pc` behind PowerShell and nvidia-smi (always: they are the
    fallback), and behind the helper too when `helper`."""
    def powershell(command: str):
        fields = [f.strip() for f in command.split("Select-Object", 1)[1].split("|")[0].split(",")] \
            if "Select-Object" in command else []
        if "Get-PhysicalDisk" in command:
            rows = [{"MediaType": _MEDIA.get(d["MediaType"], ""), "BusType": _BUS.get(d["BusType"], "")}
                    for d in pc["MSFT_PhysicalDisk"]]
        elif "Get-DnsClientServerAddress" in command:
            rows = [_pick(r, fields) for r in pc["MSFT_DNSClientServerAddress"] if r["ServerAddresses"]]
        else:
            found = re.search(r"Get-CimInstance (Win32_\w+)", command)
            if not found or found.group(1) not in pc:
                return None
            rows = [_pick(r, fields) for r in pc[found.group(1)]]
            if "Where-Object" in command:                   # the driver updater's Radeon / AMD filter
                rows = [r for r in rows if "radeon" in r["Name"].lower() or "amd" in r["Name"].lower()]
        if not rows:
            return m.R("")
        return m.R(json.dumps(rows[0] if len(rows) == 1 else rows))     # (one row prints as a bare object)

    def nvidia_smi(a: list):
        if a[0] != "nvidia-smi" or nvidia is None:
            return None
        query = next(x for x in a if x.startswith("--query-gpu=")).split("=", 1)[1].split(",")
        text = {"name": nvidia["name"], "driver_version": nvidia["driver_version"] or "[N/A]",
                "memory.total": str(round(nvidia["mem_total_bytes"] / MIB))}
        return m.R(", ".join(text[q] for q in query))

    m.PS_HOOKS.append(powershell)
    m.CMD_HOOKS.append(nvidia_smi)
    if helper:
        def wmi(namespace="", query=""):
            found = re.match(r"SELECT (.+?) FROM (\w+)", query)
            return [_pick(r, found.group(1).split(",")) for r in pc.get(found.group(2), [])]
        m.NATIVE["wmi"] = wmi
        if nvidia is not None:
            m.NATIVE["gpu_identity"] = dict(nvidia)


def both_ways(pc: dict, read, nvidia=NVIDIA):
    """read() with PowerShell answering, then with the helper answering.
    Returns (old answer, new answer, what the helper was asked)."""
    m.fresh()
    install(pc, helper=False, nvidia=nvidia)
    old = read()
    assert m.PSC or m.CMDS, "the old way started something"
    m.fresh()
    install(pc, helper=True, nvidia=nvidia)
    new = read()
    assert m.PSC == [] and m.CMDS == [], f"with the helper nothing is started: {m.PSC} {m.CMDS}"
    return old, new, list(m.NATIVE_CALLS)


# ═════════════════════════════════════════════════════════════════════
# The first screen
# ═════════════════════════════════════════════════════════════════════
def test_the_first_screen_is_the_same_either_way_and_starts_nothing():
    old, new, asked = both_ways(desktop(), SI.get_system_info)
    assert new == old, (new, old)
    assert new == {
        "os": {"name": "Microsoft Windows 11 Home", "version": "10.0.26300", "build": "26300", "arch": "64-bit"},
        "cpu": {"name": "Intel(R) Core(TM) Ultra 9 285K", "cores": 24, "threads": 24, "max_clock": 3700,
                "cur_clock": 3650},
        "gpu": {"name": "NVIDIA GeForce RTX 5070 Ti", "driver": "621.40", "vram_gb": 15.9, "status": "OK"},
        "ram": {"total_gb": 64.0, "speed_mhz": 5600, "sticks": 2, "used_pct": 40},
        "storage": {"type": "nvme", "drives": [
            {"letter": "C:", "total_gb": 1999.0, "free_gb": 512.0, "used_pct": 74},
            {"letter": "D:", "total_gb": 3725.0, "free_gb": 3000.0, "used_pct": 19}]},
        "network": {"adapter": "Ethernet", "dns": ["1.1.1.1", "1.0.0.1"]},
    }
    # Seven queries and the card's name, where seven PowerShells and an nvidia-smi were started.
    assert [q for q, _ in asked].count("wmi") == 7 and [q for q, _ in asked].count("gpu_identity") == 1
    assert {a["namespace"] for q, a in asked if q == "wmi"} == {"root\\cimv2", "root\\StandardCimv2",
                                                               "root\\Microsoft\\Windows\\Storage"}


def test_the_old_way_starts_seven_powershells_and_one_nvidia_smi():
    m.fresh()
    install(desktop(), helper=False)
    SI.get_system_info()
    assert len(m.PSC) == 7 and len(m.ran("nvidia-smi")) == 1


def test_a_helper_that_cannot_answer_one_query_leaves_that_one_to_powershell():
    m.fresh()
    pc = desktop()
    install(pc, helper=True)
    answer = m.NATIVE["wmi"]
    m.NATIVE["wmi"] = lambda namespace="", query="": [] if "Win32_Processor" in query else answer(namespace, query)
    info = SI.get_system_info()
    assert info["cpu"]["name"] == "Intel(R) Core(TM) Ultra 9 285K" and info["cpu"]["cores"] == 24
    assert len(m.PSC) == 1 and "Win32_Processor" in m.PSC[0], "only the query that got no rows"
    # WMI not answering at all: everything the old way, and the same screen.
    m.fresh()
    install(pc, helper=False)
    expected = SI.get_system_info()
    m.fresh()
    install(pc, helper=True)
    m.NATIVE["wmi"] = None
    del m.NATIVE["gpu_identity"]
    assert SI.get_system_info() == expected and len(m.PSC) == 7


def test_the_card_comes_from_nvidia_smi_when_the_driver_leaves_something_out():
    # Memory in whole MiB, rounded, as nvidia-smi prints memory.total.
    odd = dict(NVIDIA, mem_total_bytes=12 * GIB - 600_000)          # 12287.43 MiB
    old, new, _ = both_ways(desktop(), SI._get_gpu_info, nvidia=odd)
    assert new == old and new["vram_gb"] == 12.0
    for missing in ("driver_version", "mem_total_bytes"):
        m.fresh()
        install(desktop(), helper=True)
        m.NATIVE["gpu_identity"] = dict(NVIDIA, **{missing: None})
        assert SI._get_gpu_info()["name"] == "NVIDIA GeForce RTX 5070 Ti"
        assert len(m.ran("nvidia-smi")) == 1, f"without {missing} the helper's answer is not used"


def test_a_pc_without_an_nvidia_card_reads_its_adapter_from_wmi_either_way():
    laptop = desktop()
    laptop["Win32_VideoController"] = [
        {"Name": "Microsoft Basic Display Adapter", "DriverVersion": "10.0", "AdapterRAM": 0,
         "PNPDeviceID": "ROOT\\BASICDISPLAY\\0000", "Status": "OK"},
        {"Name": "AMD Radeon RX 7800 XT", "DriverVersion": "31.0.24033.1003", "AdapterRAM": 4293918720,
         "PNPDeviceID": "PCI\\VEN_1002&DEV_747E", "Status": "OK"}]
    for read in (SI._get_gpu_info, SI._detect_gpus, DU._detect_amd, DU.detect_gpu):
        m.fresh()
        install(laptop, helper=False, nvidia=None)
        old = read()
        m.fresh()
        install(laptop, helper=True, nvidia=None)
        new = read()
        assert new == old, (read.__name__, new, old)
        assert m.PSC == [], read.__name__
    assert SI._get_gpu_info() == {"name": "AMD Radeon RX 7800 XT", "driver": "31.0.24033.1003", "vram_gb": 4.0,
                                  "status": "OK"}
    assert DU._detect_amd() == {"vendor": "amd", "model": "AMD Radeon RX 7800 XT",
                                "current_version": "31.0.24033.1003"}
    assert SI._detect_gpus() == [{"name": "AMD Radeon RX 7800 XT", "vendor": "AMD", "vram_gb": 4.0,
                                  "driver": "31.0.24033.1003"}]


def test_every_adapter_is_listed_the_same_either_way():
    pc = desktop()
    pc["Win32_VideoController"] = [
        {"Name": "Intel(R) UHD Graphics 770", "DriverVersion": "31.0.101.5186", "AdapterRAM": 1073741824,
         "PNPDeviceID": "PCI\\VEN_8086&DEV_A780", "Status": "OK"},
        {"Name": "Parsec Virtual Display Adapter", "DriverVersion": "0.45", "AdapterRAM": 0,
         "PNPDeviceID": "ROOT\\DISPLAY\\0001", "Status": "OK"},
        {"Name": "NVIDIA GeForce RTX 5070 Ti", "DriverVersion": "32.0.15.2140", "AdapterRAM": 4293918720,
         "PNPDeviceID": "PCI\\VEN_10DE&DEV_2C05", "Status": "OK"}]
    old, new, _ = both_ways(pc, SI._detect_gpus)
    assert new == old and [g["vendor"] for g in new] == ["Intel", "NVIDIA"]
    # WMI missing the NVIDIA card (it happens): the card itself is asked.
    pc["Win32_VideoController"] = pc["Win32_VideoController"][:1]
    old, new, _ = both_ways(pc, SI._detect_gpus)
    assert new == old and new[-1] == {"name": "NVIDIA GeForce RTX 5070 Ti", "vendor": "NVIDIA", "vram_gb": 15.9,
                                      "driver": ""}


def test_the_drive_type_is_decided_by_the_same_disk_either_way():
    for disks, expected in (
            ([{"MediaType": 4, "BusType": 17}, {"MediaType": 3, "BusType": 11}], "nvme"),
            ([{"MediaType": 4, "BusType": 11}, {"MediaType": 4, "BusType": 17}], "ssd"),     # the first one decides
            ([{"MediaType": 3, "BusType": 11}, {"MediaType": 4, "BusType": 11}], "ssd"),
            ([{"MediaType": 3, "BusType": 11}], "hdd"),
            ([{"MediaType": 0, "BusType": 7}], "hdd"),
            ([{"MediaType": 5, "BusType": 8}], "hdd")):
        pc = desktop()
        pc["MSFT_PhysicalDisk"] = disks
        old, new, _ = both_ways(pc, U.detect_drive_type)
        assert new == old == expected, (disks, old, new)
    # No disk listed at all: the helper has nothing to say, PowerShell is asked, and prints nothing.
    pc = desktop()
    pc["MSFT_PhysicalDisk"] = []
    m.fresh()
    install(pc, helper=True)
    assert U.detect_drive_type() == "unknown" and len(m.PSC) == 1


def test_the_dns_servers_are_the_first_adapter_that_has_any_either_way():
    for rows, expected in (
            (desktop()["MSFT_DNSClientServerAddress"], {"adapter": "Ethernet", "dns": ["1.1.1.1", "1.0.0.1"]}),
            ([{"InterfaceAlias": "Wi-Fi", "ServerAddresses": ["192.168.1.1"]}],
             {"adapter": "Wi-Fi", "dns": ["192.168.1.1"]}),
            ([{"InterfaceAlias": "Ethernet", "ServerAddresses": []}], {"adapter": "", "dns": []})):
        pc = desktop()
        pc["MSFT_DNSClientServerAddress"] = rows
        m.fresh()
        install(pc, helper=False)
        old = SI._get_network_info()
        m.fresh()
        install(pc, helper=True)
        assert SI._get_network_info() == old == expected, rows


def test_the_driver_updater_reads_the_card_and_driver_either_way():
    ran = []

    def nvidia_smi(line):
        """subprocess, as the driver updater sees it: nvidia-smi printing `line`."""
        return types.SimpleNamespace(
            run=lambda a, **k: ran.append(a) or types.SimpleNamespace(returncode=0, stdout=line), CREATE_NO_WINDOW=0)
    card = {"vendor": "nvidia", "model": "NVIDIA GeForce RTX 5070 Ti", "current_version": "621.40"}
    try:
        m.fresh()
        DU.subprocess = nvidia_smi("NVIDIA GeForce RTX 5070 Ti, 621.40\n")
        assert DU._detect_nvidia() == card and len(ran) == 1 and ran[0][0] == "nvidia-smi"
        m.fresh()
        m.NATIVE["gpu_identity"] = dict(NVIDIA)
        assert DU._detect_nvidia() == card and len(ran) == 1, "the helper answered: nvidia-smi was not run"
        assert m.NATIVE_CALLS == [("gpu_identity", {})]
        # A driver that does not say its version: nvidia-smi is asked, as before.
        m.NATIVE["gpu_identity"] = dict(NVIDIA, driver_version=None)
        assert DU._detect_nvidia() == card and len(ran) == 2
        # During a driver install the helper has let go of NVIDIA's library
        # and answers nothing: nvidia-smi again, and it sees the new driver.
        m.fresh()
        DU.subprocess = nvidia_smi("NVIDIA GeForce RTX 5070 Ti, 622.10\n")
        assert DU._detect_nvidia() == dict(card, current_version="622.10") and len(ran) == 3
    finally:
        DU.subprocess = subprocess


# ═════════════════════════════════════════════════════════════════════
# The start-up preparation: two actions, and eight writes that are reads now
# ═════════════════════════════════════════════════════════════════════
USER = {(BP._GAMES_TASK, "GPU Priority"): 2, (BP._GAMES_TASK, "Priority"): 2,
        (BP._GAMES_TASK, "Scheduling Category"): "Medium", (BP._GAMES_TASK, "SFIO Priority"): "Normal",
        (r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation"): 2,
        (r"HKCU\Control Panel\Desktop", "ForegroundLockTimeout"): 200000,
        (BP._SYSTEM_PROFILE, "NetworkThrottlingIndex"): 10, (BP._SYSTEM_PROFILE, "SystemResponsiveness"): 20}


def fresh_prep():
    m.wait(lambda: not BP._state["running"])
    m.fresh()
    BP._state.update(running=False, done=False, steps=[], started_at=0, finished_at=0)
    BP.is_admin = lambda: True          # the app runs elevated
    for (key, name), data in USER.items():
        m.rset(key, name, data)


def run_prep(force=False):
    BP.prep_for_gaming_async(force=force)
    assert m.wait(lambda: BP._state["done"] and not BP._state["running"])
    return {s["name"]: s["ok"] for s in BP._state["steps"]}


def test_the_two_memory_steps_go_through_the_helper_and_start_no_powershell():
    fresh_prep()
    m.NATIVE_ACTS.update(trim_working_sets={"processes": 310, "trimmed": 262, "debug_privilege": True},
                         purge_standby_list={"purged": True})
    steps = run_prep()
    assert steps["Trim working sets"] is True and steps["Clear standby list"] is True
    assert m.NATIVE_ACTED == [("trim_working_sets", {}), ("purge_standby_list", {})], "each once, for every process"
    assert m.PSC == [], "no PowerShell, and no C# compiled inside it"


def test_without_the_helper_the_two_memory_steps_run_powershell_as_before():
    fresh_prep()
    m.PS_HOOKS.append(lambda c: m.R("OK") if "Add-Type" in c else None)
    steps = run_prep()
    assert steps["Trim working sets"] is True and steps["Clear standby list"] is True
    assert len(m.PSC) == 2
    assert "SetProcessWorkingSetSize" in m.PSC[0] and "NtSetSystemInformation(80" in m.PSC[1]


def test_a_trim_that_could_not_reach_the_services_is_done_the_old_way_as_well():
    """Elevated, the helper switches the debug privilege on for its thread,
    as PowerShell has it on.  Should that ever fail, it has trimmed fewer
    processes than the old way: PowerShell runs too, so nothing is lost."""
    fresh_prep()
    m.NATIVE_ACTS.update(trim_working_sets={"processes": 310, "trimmed": 40, "debug_privilege": False},
                         purge_standby_list={"purged": True})
    m.PS_HOOKS.append(lambda c: m.R("OK") if "Add-Type" in c else None)
    steps = run_prep()
    assert steps["Trim working sets"] is True
    assert len(m.PSC) == 1 and "SetProcessWorkingSetSize" in m.PSC[0]
    # Not elevated (a source checkout run by hand) there is no such privilege
    # to switch on, for PowerShell either: the helper's trim is all there is.
    fresh_prep()
    BP.is_admin = lambda: False
    m.NATIVE_ACTS.update(trim_working_sets={"processes": 310, "trimmed": 40, "debug_privilege": False},
                         purge_standby_list={"purged": True})
    assert run_prep()["Trim working sets"] is True and m.PSC == []


def test_a_step_the_helper_did_not_do_is_tried_the_old_way():
    fresh_prep()
    m.NATIVE_ACTS["trim_working_sets"] = {"processes": 300, "trimmed": 250, "debug_privilege": True}
    m.NATIVE_ACTS["purge_standby_list"] = None          # refused, or the helper gave no answer
    m.PS_HOOKS.append(lambda c: m.R("FAIL") if "Add-Type" in c else None)
    steps = run_prep()
    assert steps["Trim working sets"] is True
    assert steps["Clear standby list"] is False and len(m.PSC) == 1 and "NtSetSystemInformation" in m.PSC[0]


def test_only_the_start_up_preparation_asks_for_an_action():
    import glob
    import os
    askers = []
    for path in glob.glob(os.path.join(m.REPO, "core", "*.py")) + [os.path.join(m.REPO, "app.py")]:
        source = open(path, encoding="utf-8").read()
        if re.search(r"native\.(trim_working_sets|purge_standby_list|act)\(", source):
            askers.append(os.path.basename(path))
    assert askers == ["boot_prep.py"], askers


def test_the_registry_values_are_written_once_and_only_read_after_that():
    fresh_prep()
    run_prep()
    first = [(key, name) for key, name, _t, _d in m.WRITES]
    assert sorted(first) == sorted(USER), "the first start writes all eight"
    assert all(RO.has_original(key, name) for key, name in USER)
    records = RO.summary()
    # Every later start: the values are there, so no reg.exe is started.
    m.clear_logs()
    BP._state.update(done=False)
    steps = run_prep()
    assert m.ran("reg") == [] and m.WRITES == []
    assert all(steps[s] for s in ("MMCSS gaming profile", "Foreground boost", "Network throttle off"))
    assert RO.summary() == records, "and nothing new is recorded"
    # A value somebody changed back is written again, and only that one.
    m.rset(BP._SYSTEM_PROFILE, "SystemResponsiveness", 20)
    m.clear_logs()
    BP._state.update(done=False)
    run_prep()
    assert [(k, n, d) for k, n, _t, d in m.WRITES] == [(BP._SYSTEM_PROFILE, "SystemResponsiveness", "10")]
    assert m.rdata(BP._SYSTEM_PROFILE, "SystemResponsiveness") == 10


def test_a_write_that_fails_is_still_reported():
    fresh_prep()
    m.REG_FAIL.add((BP._SYSTEM_PROFILE.lower(), "systemresponsiveness"))
    steps = run_prep()
    assert steps["Network throttle off"] is False and steps["MMCSS gaming profile"] is True


# ═════════════════════════════════════════════════════════════════════
# Before the window: the Game Bar handlers, game mode's baseline
# ═════════════════════════════════════════════════════════════════════
def test_the_game_bar_handlers_are_written_once_and_only_read_after_that():
    m.fresh()
    assert DB.block_gamebar_protocols() == [{"scheme": s, "ok": True} for s in DB.GAMEBAR_PROTOCOLS]
    assert len(m.ran("reg", "add")) == 9
    for scheme in DB.GAMEBAR_PROTOCOLS:
        base = rf"HKCU\Software\Classes\{scheme}"
        assert m.rdata(base, "") == f"URL:{scheme}" and m.rdata(base, "URL Protocol") == ""
        assert m.rdata(rf"{base}\shell\open\command", "") == "cmd.exe /c exit"
    m.clear_logs()
    assert DB.block_gamebar_protocols() == [{"scheme": s, "ok": True} for s in DB.GAMEBAR_PROTOCOLS]
    assert m.CMDS == [], "every start after the first: read, found in place, nothing started"
    # One handler somebody else took over: that scheme is written again, the others are left.
    m.rset(r"HKCU\Software\Classes\ms-gamebar\shell\open\command", "", r"C:\Other\handler.exe %1")
    m.clear_logs()
    DB.block_gamebar_protocols()
    assert len(m.ran("reg", "add")) == 3 and all("ms-gamebar" in c[2] and "services" not in c[2] for c in m.CMDS)
    assert m.rdata(r"HKCU\Software\Classes\ms-gamebar\shell\open\command", "") == "cmd.exe /c exit"


def _baseline_the_old_way() -> dict:
    """game_profiles._capture_baseline's registry part as it was: reg query,
    and the digits after the last "0x" in what it printed."""
    out = {}
    for name, key, value in (
            ("priority_sep", r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation"),
            ("sys_responsiveness", GP_PROFILE, "SystemResponsiveness"),
            ("net_throttle", GP_PROFILE, "NetworkThrottlingIndex")):
        r = m.run_cmd(["reg", "query", key, "/v", value])
        if r["ok"] and "0x" in r["out"]:
            out[name] = r["out"].split("0x")[-1].strip().split()[0]
    return out


GP_PROFILE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
GP_PRIO = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"


def test_game_modes_baseline_reads_the_same_values_without_starting_reg():
    for prio, responsiveness, throttle in ((38, 20, 10), (2, 10, 4294967295), (0x28, 0, 0), (None, 20, None),
                                           (None, None, None)):
        m.fresh()
        for key, name, data in ((GP_PRIO, "Win32PrioritySeparation", prio),
                                (GP_PROFILE, "SystemResponsiveness", responsiveness),
                                (GP_PROFILE, "NetworkThrottlingIndex", throttle)):
            if data is not None:
                m.rset(key, name, data)
        old = _baseline_the_old_way()
        m.clear_logs()
        new = GP._capture_baseline()
        assert {k: new[k] for k in old} == old and set(new) - set(old) == {"cpu_idle_disabled", "idle_probe"}
        assert m.ran("reg") == [], "read directly"
    # A value that is not a number is left out, as before (reg query prints no "0x" for it).
    m.fresh()
    m.rset(GP_PRIO, "Win32PrioritySeparation", "38", "REG_SZ")
    assert "priority_sep" not in GP._capture_baseline() and _baseline_the_old_way() == {}


def test_a_stale_gaming_value_is_still_found_at_start_up():
    m.fresh()
    restored = []
    real, GP.restore_normal_mode = GP.restore_normal_mode, lambda reason="": restored.append(reason)
    try:
        m.rset(GP_PRIO, "Win32PrioritySeparation", 0x28)
        GP._verify_normal_state({"priority_sep": "26"})
        assert restored == ["stale gaming tweak detected"]
        m.rset(GP_PRIO, "Win32PrioritySeparation", 0x26)
        GP._verify_normal_state({"priority_sep": "26"})
        assert len(restored) == 1 and m.ran("reg") == []
    finally:
        GP.restore_normal_mode = real


# ═════════════════════════════════════════════════════════════════════
# Power plans
# ═════════════════════════════════════════════════════════════════════
def _power_readings() -> dict:
    return {
        "active": RO.active_scheme(),
        "optimizer": OPT.active_power_plan(),
        "game mode": GP._active_power_guid(),
        "idle (hidden), by alias": RO.query_power("SCHEME_CURRENT", "SUB_PROCESSOR", "IdleDisable"),
        "idle, by GUID on a named plan": RO.query_power(m.BALANCED, m.PROC_SUB, m.IDLE),
        "processor maximum": RO.query_power(m.USERPLAN, m.PROC_SUB, m.PROC_MAX),
        "parking": RO.query_power("SCHEME_CURRENT", m.PROC_SUB, m.PARK_E),
        "a plan that is not there": RO.query_power("12345678-1234-1234-1234-123456789abc", m.PROC_SUB, m.IDLE),
        "limit": GP._power_limit(m.USERPLAN, m.PROC_MAX),
        "limit an old tweak lowered": GP._power_limit("SCHEME_CURRENT", m.PARK_E),
        "idle disabled": GP._cpu_idle_disabled(),
        "user plan gone": RO.scheme_gone(m.USERPLAN),
        "made-up plan gone": RO.scheme_gone("12345678-1234-1234-1234-123456789abc"),
    }


def _power_machine():
    m.fresh()
    m.cell(m.USERPLAN, m.IDLE)[:] = [1, 0]
    m.cell(m.USERPLAN, m.PROC_MAX)[:] = [99, 80]
    m.cell(m.BALANCED, m.IDLE)[:] = [0, 0]
    m.rset(rf"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\{m.PROC_SUB}\{m.PARK_E}", "ValueMax", 0)


def test_power_plan_readings_are_the_same_either_way_and_start_no_powercfg():
    _power_machine()
    old = _power_readings()
    assert len(m.ran("powercfg")) >= 12
    _power_machine()
    m.native_machine()
    new = _power_readings()
    assert new == old, (new, old)
    assert old["idle (hidden), by alias"] == (1, 0) and old["processor maximum"] == (99, 80)
    assert old["limit"] == 100 and old["limit an old tweak lowered"] == 0 and old["idle disabled"] is True
    assert old["a plan that is not there"] is None and old["made-up plan gone"] is True and not old["user plan gone"]
    # One command is left: the plan that does not exist.  The helper has no
    # answer for it, powercfg is asked, and says so itself.
    assert [c[1:] for c in m.ran("powercfg")] == [["/qh", "12345678-1234-1234-1234-123456789abc", m.PROC_SUB, m.IDLE]]


def test_a_power_setting_named_the_way_only_powercfg_knows_goes_to_powercfg():
    _power_machine()
    m.native_machine()
    m.clear_logs()
    assert RO.query_power("SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMAX") is not None
    assert len(m.ran("powercfg", "/qh")) == 1 and not [q for q, _ in m.NATIVE_CALLS if q == "power_setting"]


def test_a_power_record_is_made_and_put_back_the_same_either_way():
    """remember_power and restore_all read through the helper; what they
    record and what they write back must not depend on it."""
    outcomes = []
    for helper in (False, True):
        _power_machine()
        if helper:
            m.native_machine()
        assert RO.remember_power("SUB_PROCESSOR", "IdleDisable", scope="competitive") is True
        m.run_cmd(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "IdleDisable", "0"])
        record = dict(RO._load()["power"])
        m.clear_logs()
        done = RO.restore_all("competitive")
        outcomes.append((record, done, m.cell(m.USERPLAN, m.IDLE), list(m.PTOUCH)))
        if helper:
            assert m.ran("powercfg", "/qh") == [] and m.ran("powercfg", "/getactivescheme") == []
    assert outcomes[0] == outcomes[1], outcomes
    assert outcomes[0][1] == {"restored": 1, "failed": 0} and outcomes[0][2] == [1, 0]


# ═════════════════════════════════════════════════════════════════════
# The dependency check
# ═════════════════════════════════════════════════════════════════════
class _Tools:
    """subprocess, as core.dependencies sees it: sc, tasklist, PowerShell and
    ffmpeg answering from the fake machine, every call listed."""
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, lhm_running=False, lhm_namespace=False, ffmpeg=True):
        self.ran, self.lhm_running, self.lhm_namespace, self.ffmpeg = [], lhm_running, lhm_namespace, ffmpeg

    def run(self, a, **k):
        self.ran.append(a[0])
        out, code = "", 0
        if a[0] == "sc":
            r = m.run_cmd(a)
            out, code = r["out"] + r["err"], 0 if r["ok"] else 1060
        elif a[0] == "tasklist":
            out = '"LibreHardwareMonitor.exe","4321","Console","1","48,112 K"' if self.lhm_running \
                else "INFO: No tasks are running which match the specified criteria."
        elif a[0] == "powershell":
            out = "Name\n----\nSensors\n" if self.lhm_namespace else ""
        elif a[0] == "ffmpeg":
            if not self.ffmpeg:
                raise FileNotFoundError(2, "The system cannot find the file specified")
            out = "ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright (c) 2000-2024\nbuilt with gcc"
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")


def _with_tools(tools):
    DEP.subprocess = tools
    DEP._ffmpeg_on_path = None
    return tools


def test_a_service_is_found_the_same_either_way():
    try:
        for helper in (False, True):
            m.fresh()
            tools = _with_tools(_Tools())
            if helper:
                m.native_machine()
            assert DEP._service_query("Spooler") == {"installed": True, "running": True, "name": "Spooler"}
            assert DEP._service_query("XboxGipSvc") == {"installed": True, "running": False, "name": "XboxGipSvc"}
            assert DEP._service_query("PresentMonService") == {"installed": False, "running": False,
                                                               "name": "PresentMonService"}
            assert DEP._detect_pm2() == {"installed": False, "running": False}
            m.svc_set("PresentMonSharedService", "auto")
            m.RUNNING.add("presentmonsharedservice")
            assert DEP._detect_pm2() == {"installed": True, "running": True, "name": "PresentMonSharedService"}
            assert (tools.ran == []) if helper else (tools.ran.count("sc") == 7), tools.ran
    finally:
        DEP.subprocess = subprocess


def test_libre_hardware_monitor_is_looked_for_the_same_either_way():
    try:
        for running, namespace in ((True, True), (True, False), (False, False)):
            m.fresh()
            tools = _with_tools(_Tools(lhm_running=running, lhm_namespace=namespace))
            old = (DEP._lhm_process_running(), DEP._lhm_wmi_namespace_present())
            assert tools.ran == ["tasklist", "powershell"]
            m.fresh()
            tools = _with_tools(_Tools())
            m.NATIVE["process_io"] = [{"pid": 4, "name": "System", "other_bytes": 0}] + (
                [{"pid": 4321, "name": "LibreHardwareMonitor.exe", "other_bytes": 9}] if running else [])
            m.NATIVE["wmi"] = lambda namespace="", query="": ([{"Name": "Sensors"}] if namespace_there else [])
            namespace_there = namespace
            assert (DEP._lhm_process_running(), DEP._lhm_wmi_namespace_present()) == old == (running, namespace)
            assert tools.ran == [], "no tasklist and no PowerShell"
            assert m.NATIVE_CALLS[-1] == ("wmi", {"namespace": "root\\LibreHardwareMonitor",
                                                 "query": "SELECT Name FROM __NAMESPACE"})
    finally:
        DEP.subprocess = subprocess


def test_ffmpeg_found_on_the_path_is_asked_once_a_run():
    try:
        m.fresh()
        tools = _with_tools(_Tools(ffmpeg=True))
        first = DEP._detect_ffmpeg()
        assert first == {"installed": True, "path": "ffmpeg",
                         "version_line": "ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright (c) 2000-2024"}
        for _ in range(4):
            again = DEP._detect_ffmpeg()
            assert again == first and again is not first
        assert tools.ran == ["ffmpeg"], "five questions at a start, one program"
        # Not found is not remembered: an install is seen at the next look.
        tools = _with_tools(_Tools(ffmpeg=False))
        assert DEP._detect_ffmpeg()["installed"] is False and DEP._detect_ffmpeg()["installed"] is False
        assert tools.ran == ["ffmpeg", "ffmpeg"]
        tools.ffmpeg = True
        assert DEP._detect_ffmpeg()["installed"] is True
    finally:
        DEP.subprocess = subprocess
        DEP._ffmpeg_on_path = None


# ═════════════════════════════════════════════════════════════════════
# The network monitor: only while something reads it
# ═════════════════════════════════════════════════════════════════════
class _Sampler:
    """The monitor with a counting stand-in for a sampling pass and a short
    patience, put back afterwards."""

    def __enter__(self):
        self.saved = (NM._sample_once, NM.SAMPLE_INTERVAL, NM.IDLE_STOP_S)
        self.passes = []
        NM.stop()
        m.wait(lambda: not (NM._thread and NM._thread.is_alive()), 3)
        NM._sample_once = lambda: self.passes.append(threading.current_thread().name)
        NM.SAMPLE_INTERVAL, NM.IDLE_STOP_S = 0.02, 0.25
        NM._last_wanted = 0.0
        return self

    def __exit__(self, *exc):
        NM.stop()
        m.wait(lambda: not (NM._thread and NM._thread.is_alive()), 3)
        NM._sample_once, NM.SAMPLE_INTERVAL, NM.IDLE_STOP_S = self.saved
        return False


def test_the_app_does_not_start_the_network_sampler_at_start_up():
    src = inspect.getsource(A.main)
    assert "net_monitor.start()" not in src
    assert not NM.is_running(), "importing the app started nothing"


def test_reading_the_figures_starts_the_sampler_and_it_stops_when_nobody_reads():
    with _Sampler() as s:
        assert not NM.is_running() and s.passes == []
        live = NM.get_live()
        assert live["running"] is True and NM.is_running()
        assert m.wait(lambda: len(s.passes) >= 3, 3), "it samples while it is being read"
        # Read on: it keeps going well past its patience.
        end = time.time() + 0.7
        while time.time() < end:
            NM.get_history(60)
            time.sleep(0.03)
        assert NM.is_running()
        # Nobody reads: it stops by itself.
        assert m.wait(lambda: not NM.is_running(), 3), "still sampling with nobody reading"
        thread = NM._thread
        assert m.wait(lambda: not thread.is_alive(), 3)
        n = len(s.passes)
        time.sleep(0.15)
        assert len(s.passes) == n, "and it stays stopped"
        # The page is opened again: a new sampler.
        NM.get_live()
        assert NM.is_running() and m.wait(lambda: len(s.passes) > n, 3)


def test_a_new_run_does_not_work_from_the_last_runs_readings():
    with _Sampler():
        NM._per_proc[77] = {"pid": 77, "name": "old.exe", "last_other_bytes": 5, "bytes_per_sec": 9_000_000,
                            "consecutive_high": 9}
        NM._last_nic_rx, NM._last_nic_tx, NM._last_sample_t = 123, 456, 1000.0
        NM._latest.update(total_rx_per_sec=7, top=[{"pid": 77}], burst_alert={"pid": 77})
        live = NM.get_live()
        assert NM._per_proc == {} and (NM._last_nic_rx, NM._last_nic_tx, NM._last_sample_t) == (0, 0, 0.0)
        assert live["top"] == [] and live["burst_alert"] is None and live["total_rx_per_sec"] == 0


def test_only_one_sampler_runs_however_many_readers_ask_at_once():
    with _Sampler() as s:
        readers = [threading.Thread(target=NM.get_live) for _ in range(12)]
        for t in readers:
            t.start()
        for t in readers:
            t.join()
        assert m.wait(lambda: len(s.passes) >= 5, 3)
        assert len({name for name in s.passes}) == 1
        assert sum(1 for t in threading.enumerate() if t.name == "net-monitor" and t.is_alive()) == 1
        # stop() and a reader straight after: the old one ends, one new one runs.
        NM.stop()
        assert not NM.is_running()
        NM.get_live()
        assert NM.is_running()
        assert m.wait(lambda: sum(1 for t in threading.enumerate()
                                  if t.name == "net-monitor" and t.is_alive()) == 1, 3)


def test_the_network_routes_are_what_keeps_it_running():
    with _Sampler():
        client = A.app.test_client()
        headers = {"Host": f"127.0.0.1:{A.config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}
        assert not NM.is_running()
        live = client.get("/api/network/live", headers=headers).get_json()
        assert live["running"] is True and NM.is_running()
        NM.stop()
        assert client.get("/api/network/history?seconds=60", headers=headers).status_code == 200
        assert NM.is_running(), "the graph's own request starts it too"
        NM.stop()
        assert client.get("/api/network/top", headers=headers).status_code == 200 and NM.is_running()


# ═════════════════════════════════════════════════════════════════════
# Efficiency mode: after the start-up work, not before it
# ═════════════════════════════════════════════════════════════════════
class _Efficiency:
    def __enter__(self):
        self.saved = (A._set_efficiency_mode, A._startup_work_done)
        self.entered = []
        A._set_efficiency_mode = lambda: self.entered.append(time.monotonic())
        GP._engine["gaming_mode"] = False
        return self

    def __exit__(self, *exc):
        A._set_efficiency_mode, A._startup_work_done = self.saved
        GP._engine["gaming_mode"] = False
        return False


def test_main_enters_efficiency_mode_at_once_only_after_a_start_at_sign_in():
    src = inspect.getsource(A.main)
    assert src.count("_set_efficiency_mode()") == 1
    assert re.search(r"if from_autostart:\n\s+_set_efficiency_mode\(\)\n", src)
    assert re.search(r"if not from_autostart:\n\s+threading\.Thread\(target=_efficiency_mode_after_startup", src)
    # ...and that thread starts after the recovery steps that must run alone.
    assert src.index("target=_efficiency_mode_after_startup") > src.index("adaptive_tuning.clear_runtime_if_orphaned()")


def test_efficiency_mode_waits_for_the_start_up_work():
    with _Efficiency() as e:
        done_at = time.monotonic() + 0.3
        A._startup_work_done = lambda: time.monotonic() >= done_at
        started = time.monotonic()
        assert A._efficiency_mode_after_startup(cap_s=10, poll_s=0.01) == "efficiency"
        assert len(e.entered) == 1 and 0.3 <= e.entered[0] - started < 2.0


def test_efficiency_mode_does_not_wait_for_ever():
    with _Efficiency() as e:
        A._startup_work_done = lambda: False
        started = time.monotonic()
        assert A._efficiency_mode_after_startup(cap_s=0.25, poll_s=0.01) == "efficiency"
        assert len(e.entered) == 1 and 0.25 <= e.entered[0] - started < 2.0
        assert A._STARTUP_EFFICIENCY_CAP_S == 60.0


def test_a_game_found_during_start_up_keeps_the_app_responsive():
    with _Efficiency() as e:
        A._startup_work_done = lambda: True
        GP._engine["gaming_mode"] = True
        assert A._efficiency_mode_after_startup(cap_s=1, poll_s=0.01) == "left to game mode"
        assert e.entered == [], "game mode enters efficiency mode itself when the game closes"
        GP._engine["gaming_mode"] = False
        assert A._efficiency_mode_after_startup(cap_s=1, poll_s=0.01) == "efficiency" and len(e.entered) == 1


def test_a_game_cannot_start_between_the_check_and_entering_efficiency_mode():
    calls = []
    GP._engine["gaming_mode"] = False
    assert GP.unless_in_a_game(lambda: calls.append(GP._session_lock._is_owned())) is True
    assert calls == [True], "called with the session lock held"
    GP._engine["gaming_mode"] = True
    try:
        assert GP.unless_in_a_game(lambda: calls.append("in a game")) is False and len(calls) == 1
    finally:
        GP._engine["gaming_mode"] = False


def test_start_up_is_over_when_its_three_parts_are():
    saved = dict(BP._state)
    try:
        A._first_screen_served.clear()
        DEP._boot_check_done.clear()
        BP._state.update(done=False)
        assert A._startup_work_done() is False
        BP._state.update(done=True)
        DEP._boot_check_done.set()
        assert A._startup_work_done() is False, "the first screen has not asked for its data yet"
        m.fresh()
        install(desktop(), helper=True)
        client = A.app.test_client()
        headers = {"Host": f"127.0.0.1:{A.config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}
        assert client.get("/api/dashboard", headers=headers).get_json()["cpu"]["cores"] == 24
        assert A._first_screen_served.is_set() and A._startup_work_done() is True
        DEP._boot_check_done.clear()
        assert A._startup_work_done() is False
    finally:
        BP._state.update(saved)
        DEP._boot_check_done.clear()
        A._first_screen_served.clear()
