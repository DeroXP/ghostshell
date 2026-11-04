import sandbox as m
# Robustness of the system-tweak modules: the Background Pauser and Tournament
# Mode (processes suspended, never stranded or mistaken for others), DNS and
# network reads that must work on a non-English Windows, and results that say
# what really happened.

import json
import os
import re
import subprocess
import sys
import threading
import time
import types

# ── psutil, as the Background Pauser sees it: PROCS[pid] = (name, created) ──
PROCS: dict = {}


class NoSuchProcess(Exception):
    pass


class _Proc:
    def __init__(self, pid):
        if pid not in PROCS:
            raise NoSuchProcess(pid)
        self.pid = pid

    def name(self):
        return PROCS[self.pid][0]

    def create_time(self):
        return PROCS[self.pid][1]


def _process_iter(attrs=None):
    for pid, (name, _) in list(PROCS.items()):
        yield types.SimpleNamespace(info={"pid": pid, "name": name,
                                          "memory_info": types.SimpleNamespace(rss=500 * 2 ** 20)})


_psutil = types.ModuleType("psutil")
_psutil.NoSuchProcess = NoSuchProcess
_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
_psutil.Process = _Proc
_psutil.pid_exists = lambda pid: pid in PROCS
_psutil.process_iter = _process_iter
sys.modules["psutil"] = _psutil

from core import background_pauser as BPZ, net_monitor as NM          # noqa: E402
from core import reg_originals as RO                                     # noqa: E402

SUSPENDED: dict = {}        # pid -> suspend count
RESUMED: list = []
DENY: set = set()           # pids the suspend is refused for (access denied)


def _fake_suspend(pid):
    if pid in DENY:
        return False
    SUSPENDED[pid] = SUSPENDED.get(pid, 0) + 1
    return True


def _fake_resume(pid):
    if pid not in PROCS:
        return False
    if SUSPENDED.get(pid):
        SUSPENDED[pid] -= 1
    RESUMED.append(pid)
    return True


BPZ._suspend_pid = _fake_suspend
BPZ._resume_pid = _fake_resume

TMODE = m.app("core.tournament_mode")
DNS = m.app("core.dns_manager")
NT = m.app("core.network_tweaks")
IA = m.app("core.interrupt_affinity")
DB = m.app("core.debloater")
PR = m.app("core.privacy")
HA = m.app("core.health_audit")
m.app("core.timer_manager")
CL = m.app("core.competitive_latency")
LD = m.app("core.latency_doctor")
SM = m.app("core.startup_manager")
IM = m.app("core.idle_mode")


def fresh():
    m.fresh()
    PROCS.clear()
    SUSPENDED.clear()
    del RESUMED[:]
    DENY.clear()


# ═════════════════════════════════════════════════════════════════════
# Background Pauser
# ═════════════════════════════════════════════════════════════════════
def test_a_process_that_could_not_be_suspended_is_not_left_on_record():
    fresh()
    PROCS.update({101: ("chrome.exe", 1000.0), 102: ("msedge.exe", 1000.0)})
    DENY.add(102)
    BPZ.set_settings(enabled=True)
    r = BPZ.pause_now()
    assert [p["pid"] for p in r["paused"]] == [101] and [p["pid"] for p in r["failed"]] == [102]
    assert [p["pid"] for p in BPZ._recorded_pids()] == [101], "102 stayed on record as paused"
    BPZ.resume_now()
    assert RESUMED == [101] and BPZ._recorded_pids() == []


def test_resume_leaves_alone_a_pid_that_now_belongs_to_another_process():
    """After a crash and a restart every PID on record is someone else's."""
    fresh()
    paused_at = time.time() - 3600
    BPZ._write_recorded_pids([{"pid": 201, "name": "chrome.exe", "rss": 0, "ts": paused_at, "reason": "x"},
                              {"pid": 202, "name": "chrome.exe", "rss": 0, "ts": paused_at, "reason": "x"},
                              {"pid": 203, "name": "chrome.exe", "rss": 0, "ts": paused_at, "reason": "x"}])
    PROCS.update({201: ("svchost.exe", paused_at - 100),        # another program
                  202: ("chrome.exe", paused_at + 600),         # a chrome started after the pause
                  203: ("chrome.exe", paused_at - 100)})        # ours
    r = BPZ.recover_orphans()
    assert RESUMED == [203]
    assert sorted(p["pid"] for p in r["not_alive"]) == [201, 202]
    assert BPZ._recorded_pids() == []


def test_two_pauses_at_once_suspend_each_process_once():
    """A game starting while Tournament Mode switches on: both found the
    record empty, and each process was suspended twice - one resume then
    left it frozen."""
    fresh()
    PROCS.update({301: ("chrome.exe", 1000.0), 302: ("firefox.exe", 1000.0)})
    BPZ.set_settings(enabled=True)
    real_load = BPZ._load_json

    def slow_load(path, default):
        if path == BPZ.PAUSED_PIDS_PATH:
            time.sleep(0.1)
        return real_load(path, default)

    BPZ._load_json = slow_load
    try:
        threads = [threading.Thread(target=BPZ.pause_now, args=(f"run {i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        BPZ._load_json = real_load
    assert SUSPENDED == {301: 1, 302: 1}
    assert sorted(p["pid"] for p in BPZ._recorded_pids()) == [301, 302]
    BPZ.resume_now()
    assert SUSPENDED == {301: 0, 302: 0}, "nothing left frozen"


def test_a_process_resumed_from_the_network_monitor_can_be_paused_again():
    fresh()
    PROCS[401] = ("updater.exe", 1000.0)
    assert NM.pause_process(401)["ok"] and SUSPENDED[401] == 1
    assert [(p["pid"], p["name"]) for p in BPZ._recorded_pids()] == [(401, "updater.exe")]
    assert NM.resume_process(401)["ok"] and SUSPENDED[401] == 0
    assert BPZ._recorded_pids() == [], "a resumed process stayed on record as paused"
    assert NM.pause_process(401)["ok"]
    assert SUSPENDED[401] == 1, "the second pause took it for still suspended and did nothing"


# ═════════════════════════════════════════════════════════════════════
# Tournament Mode
# ═════════════════════════════════════════════════════════════════════
def _tournament_machine(running):
    def ps(c):
        mm = re.search(r"(Get|Stop|Start)-Service -Name '(\w+)'", c)
        if mm:
            verb, svc = mm.group(1), mm.group(2).lower()
            if verb == "Get":
                time.sleep(0.05)
                return m.R("Running" if svc in running else "Stopped")
            (running.discard if verb == "Stop" else running.add)(svc)
            return m.R("OK")
        if "Get-Process -Name 'onedrive'" in c:
            return m.R("0")
        return None
    m.PS_HOOKS.append(ps)


def test_a_second_click_on_enable_does_not_replace_the_users_baseline():
    fresh()
    running = {"wuauserv", "wsearch", "widgetservice"}
    _tournament_machine(running)
    toasts = r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings"
    saved = (BPZ.get_settings, BPZ.set_settings, BPZ.pause_now, BPZ.resume_now)
    BPZ.get_settings = lambda: {"enabled": True}
    BPZ.set_settings = lambda **k: None
    BPZ.pause_now = lambda reason="": {"ok": True, "paused": []}
    BPZ.resume_now = lambda reason="": {"ok": True, "resumed": []}
    try:
        out = []
        threads = [threading.Thread(target=lambda: out.append(TMODE.enable(duration_min=0))) for _ in range(2)]
        for t in threads:
            t.start()
            time.sleep(0.02)
        for t in threads:
            t.join()
        assert sorted(r["ok"] for r in out) == [False, True], "both runs went through"
        base = TMODE._load_state()["baseline"]
        assert base["power_plan_guid"] == m.USERPLAN and base["toasts_enabled"] is True
        assert base["wuauserv_running"] and base["wsearch_running"] and base["widgetservice_running"]
        assert TMODE.disable()["ok"]
    finally:
        BPZ.get_settings, BPZ.set_settings, BPZ.pause_now, BPZ.resume_now = saved
    assert m.PLAN["active"] == m.USERPLAN, "the user's power plan is back"
    assert running == {"wuauserv", "wsearch", "widgetservice"} and m.rdata(toasts, "NOC_GLOBAL_SETTING_TOASTS_ENABLED") == 1


def test_onedrive_is_started_as_the_user_not_elevated():
    fresh()
    exe = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "OneDrive", "OneDrive.exe")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    open(exe, "w").close()
    started = []
    real = subprocess.Popen
    subprocess.Popen = lambda args, **k: started.append(args)
    try:
        assert TMODE._launch_onedrive()
    finally:
        subprocess.Popen = real
    assert started == [["explorer.exe", exe]], "an elevated OneDrive refuses to run"


# ═════════════════════════════════════════════════════════════════════
# DNS and network
# ═════════════════════════════════════════════════════════════════════
# "Wireless network" on a Russian Windows.  Kept out of the assert lines: the
# test runner prints a failing line in the console code page.
_WIFI = {"Name": "Беспроводная сеть",
         "InterfaceIndex": 17, "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201",
         "MacAddress": "AA-BB", "MediaType": "Native 802.11", "PhysicalMediaType": "Native 802.11",
         "LinkSpeed": "866.7 Mbps", "Status": "Up"}

_PING_DE = ("Ping wird ausgeführt für {ip} mit 32 Bytes Daten:\n"
            "Antwort von {ip}: Bytes=32 Zeit=12ms TTL=57\n\n"
            "Ping-Statistik für {ip}:\n"
            "    Pakete: Gesendet = 4, Empfangen = 3, Verloren = 1\n"
            "    (25% Verlust),\n"
            "Ca. Zeitangaben in Millisek.:\n"
            "    Minimum = 11ms, Maximum = 14ms, Mittelwert = 12ms")
_PING_FR = ("Statistiques Ping pour {ip}:\n"
            "    Paquets : envoyés = 10, reçus = 9, perdus = 1 (perte 10%),\n"
            "Durée approximative des boucles en millisecondes :\n"
            "    Minimum = 21ms, Maximum = 30ms, Moyenne = 24ms")


def _ping(template):
    m.CMD_HOOKS.append(lambda a: m.R(template.format(ip=a[-1])) if a[0] == "ping" else None)


def test_dns_goes_to_the_adapter_by_index_and_a_failed_secondary_is_reported():
    fresh()
    m.PS_HOOKS.append(lambda c: m.R(json.dumps([_WIFI])) if c.startswith("Get-NetAdapter") else None)
    m.CMD_HOOKS.append(lambda a: m.F("The object already exists.") if a[1:5] == ["interface", "ip", "add", "dns"]
                       else None)
    r = DNS.apply_dns("cloudflare")
    assert r == {"ok": False, "adapters": [{"adapter": _WIFI["Name"], "ok": False}]}
    assert m.ran("netsh", "set", "dns", "name=17", "static", "1.1.1.1")
    assert not [c for c in m.CMDS if c[0] == "netsh" and any(_WIFI["Name"] in x for x in c)]
    assert DNS.reset_dns_to_dhcp()["ok"] and m.ran("netsh", "set", "dns", "name=17", "dhcp")


def test_dns_adapter_reads_ask_powershell_for_ascii_json():
    """PowerShell writes in the console code page, which is not the one its
    output is read with: an "ü" made the whole adapter list fail to read."""
    fresh()
    DNS.get_all_adapters()
    DNS.get_current_dns()
    assert len([c for c in m.PSC if c.endswith(DNS._ASCII_JSON)]) == 3


def test_dns_latency_is_read_from_a_german_ping():
    fresh()
    _ping(_PING_DE)
    rows = DNS.test_dns_latency(["1.1.1.1", "8.8.8.8"])
    assert [r["avg_ms"] for r in rows] == [12, 12], "every server showed as timed out"


def test_ping_test_is_read_from_a_french_ping():
    fresh()
    _ping(_PING_FR)
    (row,) = NT.ping_test([("Cloudflare", "1.1.1.1")])
    assert (row["min_ms"], row["max_ms"], row["avg_ms"], row["loss_pct"]) == (21, 30, 24, 10)


def test_ping_test_on_a_german_ping_with_the_loss_on_its_own_line():
    fresh()
    _ping(_PING_DE)
    (row,) = NT.ping_test([("Cloudflare", "1.1.1.1")])
    assert (row["min_ms"], row["max_ms"], row["avg_ms"], row["loss_pct"]) == (11, 14, 12, 25)


def test_prefer_ipv4_reports_a_refused_write():
    fresh()
    key = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters"
    m.REG_FAIL.add((key.lower(), "disabledcomponents"))
    assert NT.disable_ipv6()["ok"] is False
    m.REG_FAIL.clear()
    assert NT.disable_ipv6()["ok"] is True and m.rdata(key, "DisabledComponents") == 32


def test_lso_scripts_no_unsafe_name_and_says_when_nothing_matched():
    """Its name pattern matches no common driver's setting, so on most PCs
    nothing is changed: that is "skipped", not done."""
    fresh()
    m.PS_HOOKS.append(lambda c: m.R(json.dumps([{"Name": 'Ethernet "2"; calc'}, {"Name": "Ethernet"},
                                                {"Name": "LAN"}, {"Name": "Dock"}]))
                      if "Select-Object Name | ConvertTo-Json" in c else None)
    # Per adapter: how many matching settings it has (and set); Dock's driver refuses the value.
    answers = {'"Ethernet"': m.R("0"), '"LAN"': m.R("2"), '"Dock"': m.F("Set-NetAdapterAdvancedProperty : refused")}
    m.PS_HOOKS.append(lambda c: next((r for n, r in answers.items() if f"-Name {n}" in c), None)
                      if "Large Send Offload" in c else None)
    rows = NT.disable_lso()
    assert [(r["adapter"], r["ok"]) for r in rows] == [('Ethernet "2"; calc', False), ("Ethernet", None),
                                                       ("LAN", True), ("Dock", False)]
    assert "skipped" in rows[1]["name"]
    assert not m.ps_ran("calc") and len(m.ps_ran("Large Send Offload")) == 3


def test_other_adapter_tweaks_with_nothing_to_change_say_skipped():
    fresh()
    wired = {"names": ""}           # the first power pass prints the wired adapters it handled
    found = {"count": "0"}          # how many of the named settings the adapters have
    m.PS_HOOKS.append(lambda c: m.R(wired["names"]) if "Set-NetAdapterPowerManagement" in c else None)
    m.PS_HOOKS.append(lambda c: m.R(found["count"]) if "Get-NetAdapterAdvancedProperty" in c else None)
    assert [r["ok"] for r in NT.optimize_adapter_power()] == [None], "a PC with no wired adapter"
    wired["names"] = '"Ethernet"'
    rows = NT.optimize_adapter_power()
    assert [r["ok"] for r in rows] == [True, None] and "skipped" in rows[1]["name"]
    found["count"] = "2"
    assert [r["ok"] for r in NT.optimize_adapter_power()] == [True]
    found["count"] = "0"
    assert NT.apply_rss_rsc_adapter_tweaks()[0]["ok"] is None
    found["count"] = "5"
    assert NT.apply_rss_rsc_adapter_tweaks()[0]["ok"] is True


def _dns_machine():
    """Two adapters (Ethernet, Wi-Fi) on the fake machine's interfaces, and a
    netsh that writes NameServer the way Windows does.  Returns the adapters,
    for a test to change their status."""
    nics = [{"Name": "Ethernet", "InterfaceIndex": 11, "InterfaceGuid": m.IFACES[0].upper(), "MediaType": "802.3",
             "PhysicalMediaType": "802.3", "InterfaceDescription": "Intel(R) Ethernet I225-V", "Status": "Up"},
            {"Name": "Wi-Fi", "InterfaceIndex": 12, "InterfaceGuid": m.IFACES[1].upper(),
             "MediaType": "Native 802.11", "PhysicalMediaType": "Native 802.11",
             "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201", "Status": "Up"}]
    by_index = {n["InterfaceIndex"]: n["InterfaceGuid"] for n in nics}

    def adapters(c):
        if not c.startswith("Get-NetAdapter"):
            return None
        up = "Status -eq 'Up'" in c
        return m.R(json.dumps([n for n in nics if not up or n["Status"] == "Up"]))

    def netsh(a):
        if a[:3] != ["netsh", "interface", "ip"] or a[4] != "dns":
            return None
        key = rf"{m.IFACES_KEY}\{by_index[int(a[5].split('=')[1])]}"
        servers = [s for s in (m.rdata(key, "NameServer") or "").split(",") if s]
        if a[3] == "set":
            servers = [] if a[6] == "dhcp" else [a[7]]
        else:
            servers.insert(int(a[7].split("=")[1]) - 1, a[6])
        m.rset(key, "NameServer", ",".join(servers))
        return m.R("Ok.")

    m.PS_HOOKS.append(adapters)
    m.CMD_HOOKS.append(netsh)
    return nics


def test_dns_reset_puts_back_what_each_adapter_had_before_the_first_preset():
    fresh()
    _dns_machine()
    eth, wlan = (rf"{m.IFACES_KEY}\{g}" for g in m.IFACES)
    m.rset(eth, "NameServer", "192.168.1.2,192.168.1.3")       # the user's own resolver
    m.rset(wlan, "NameServer", "")                              # from DHCP
    assert DNS.apply_dns("cloudflare")["ok"] and m.rdata(eth, "NameServer") == "1.1.1.1,1.0.0.1"
    assert DNS.apply_dns("google")["ok"] and m.rdata(eth, "NameServer") == "8.8.8.8,8.8.4.4"
    assert DNS.reset_dns_to_dhcp()["ok"]
    assert m.rdata(eth, "NameServer") == "192.168.1.2,192.168.1.3", "the second preset replaced the record"
    assert m.rdata(wlan, "NameServer") == "" and RO.state_records("dns", "dns") == {}
    m.rset(eth, "NameServer", "9.9.9.9")                       # set by a build that kept no record
    DNS.reset_dns_to_dhcp()
    assert m.rdata(eth, "NameServer") == ""


def test_dns_reset_reaches_a_changed_adapter_that_is_not_connected_now():
    fresh()
    nics = _dns_machine()
    wlan = rf"{m.IFACES_KEY}\{m.IFACES[1]}"
    m.rset(wlan, "NameServer", "10.0.0.53")
    DNS.apply_dns("quad9")
    nics[1]["Status"] = "Disconnected"
    assert DNS.reset_dns_to_dhcp()["ok"]
    assert m.rdata(wlan, "NameServer") == "10.0.0.53"


def test_a_damaged_dns_record_cannot_point_dns_anywhere():
    fresh()
    _dns_machine()
    eth = rf"{m.IFACES_KEY}\{m.IFACES[0]}"
    m.rset(eth, "NameServer", "1.1.1.1")
    RO.remember_state("dns", m.IFACES[0], {"servers": ["6.6.6.6 validate=no", "x"]}, "dns")
    DNS.reset_dns_to_dhcp()
    assert m.rdata(eth, "NameServer") == ""


# ═════════════════════════════════════════════════════════════════════
# Interrupts, services, health checks, startup apps, idle mode
# ═════════════════════════════════════════════════════════════════════
def test_irq_affinity_reset_reports_a_refused_delete():
    fresh()
    iid = r"PCI\VEN_8086&DEV_15F3\NIC0"
    key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{iid}\Device Parameters\Interrupt Management\Affinity Policy"
    assert IA.reset_device_affinity(iid)["ok"], "nothing was set: nothing to reset"
    m.rset(key, "DevicePolicy", 4)
    m.rset(key, "AssignmentSetOverride", 4)
    m.REG_FAIL.add((key.lower(), "assignmentsetoverride"))
    assert IA.reset_device_affinity(iid)["ok"] is False
    assert m.rget(key, "DevicePolicy") is None and m.rget(key, "AssignmentSetOverride") is not None


def test_auto_interrupt_tuning_enables_msi_only_where_the_driver_declares_it():
    fresh()
    enum = r"HKLM\SYSTEM\CurrentControlSet\Enum" + "\\"
    msi = r"\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
    devs = {r"PCI\VEN_10DE&DEV_2C05\GPU0": ("Display", 0),            # the vendor opted it out
            r"PCI\VEN_8086&DEV_15F3\NIC0": ("Net", None),             # key, no value: declared
            r"PCI\VEN_8086&DEV_7AD0\AUD0": ("Media", "no key"),       # the driver does not declare MSI
            r"PCI\VEN_8086&DEV_7AE0\USB0": ("USB", 1),                # already on
            r"USB\VID_046D&PID_C539\5&1A2B&0&1": ("USB", None)}       # a device behind a controller
    for iid, (cls, val) in devs.items():
        if val != "no key":
            m.make_key(enum + iid + msi)
            if val is not None:
                m.rset(enum + iid + msi, "MSISupported", val)
    listing = [{"name": iid.split("\\")[-1], "instance_id": iid, "class": cls,
                "msi_supported": val if isinstance(val, int) else None, "msi_enabled": val == 1}
               for iid, (cls, val) in devs.items()]
    m.PS_HOOKS.append(lambda c: m.R(json.dumps(listing))
                      if "MessageSignaledInterruptProperties" in c and "Get-PnpDevice -Class Display" in c else None)
    rows = IA.auto_optimize_interrupts()
    assert [r["name"] for r in rows if r["name"].startswith("MSI")] == ["MSI: NIC0"]
    assert m.rdata(enum + r"PCI\VEN_8086&DEV_15F3\NIC0" + msi, "MSISupported") == 1
    assert m.rdata(enum + r"PCI\VEN_10DE&DEV_2C05\GPU0" + msi, "MSISupported") == 0
    assert not m.key_exists(enum + r"PCI\VEN_8086&DEV_7AD0\AUD0" + msi)
    assert m.rget(enum + r"USB\VID_046D&PID_C539\5&1A2B&0&1" + msi, "MSISupported") is None


def test_the_startup_repairs_run_once_so_the_users_own_choice_stands():
    """They undo damage from old builds, and ran at every launch: the
    microphone one switched device-wide mic access back on each time."""
    fresh()
    mic = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    m.rset(mic, "Value", "Deny")                                # what an old build left
    m.REG_FAIL.add((mic.lower(), "value"))
    PR.restore_microphone_access()
    assert m.rdata(mic, "Value") == "Deny"
    m.REG_FAIL.clear()
    PR.restore_microphone_access()                              # it failed: tried again at the next launch
    assert m.rdata(mic, "Value") == "Allow"
    m.rset(mic, "Value", "Deny")                                # the user turns the microphone off
    assert PR.restore_microphone_access()["skipped"] and m.rdata(mic, "Value") == "Deny"

    m.svc_set("DiagTrack", "disabled")
    m.SC_FAIL.add("diagtrack")
    PR.restore_store_updates()
    assert m.svc_get("DiagTrack") == "disabled"
    m.SC_FAIL.clear()
    PR.restore_store_updates()
    assert m.svc_get("DiagTrack") == "demand"
    m.svc_set("DiagTrack", "disabled")                          # the user's own doing, later
    assert PR.restore_store_updates()["skipped"] and m.svc_get("DiagTrack") == "disabled"


def test_debloated_delayed_start_service_comes_back_delayed():
    fresh()
    assert m.svc_get("WSearch") == "delayed-auto"
    DB.disable_service("WSearch")
    DB.restore_services()
    assert m.svc_get("WSearch") == "delayed-auto" and "wsearch" in m.RUNNING


def test_a_service_restore_that_fails_stays_on_record_for_the_next_try():
    fresh()
    DB.disable_service("DiagTrack")
    DB.disable_service("lfsvc")
    DB._save_service_backup({**DB._load_service_backup(), "GoneSvc": 3})     # uninstalled since
    m.SC_FAIL.add("diagtrack")
    res = DB.restore_services()
    assert (res["restored"], res["total"]) == (1, 3) and m.svc_get("lfsvc") == "demand"
    assert DB._load_service_backup() == {"DiagTrack": 2}, "its original start type was forgotten"
    m.SC_FAIL.clear()
    assert DB.restore_services()["restored"] == 1 and m.svc_get("DiagTrack") == "auto"
    assert DB._load_service_backup() == {}


def test_power_plan_check_reads_a_german_windows():
    fresh()
    shown = {"plan": ""}
    m.PS_HOOKS.append(lambda c: m.R(f"GUID des Energieschemas: {shown['plan']}") if "getactivescheme" in c else None)
    for plan, severity in (("381b4222-f694-41f0-9685-ff5bb260df2e  (Ausbalanciert)", "warn"),
                           ("a1841308-3541-4fab-bc81-f71556f20b4a  (Energiesparmodus)", "crit"),
                           ("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (Höchstleistung)", "ok")):
        shown["plan"] = plan
        assert HA._check_power_plan()[0]["severity"] == severity, plan


def test_latency_doctor_reads_the_minimum_cpu_state_on_a_german_windows():
    fresh()
    qh = ("GUID des Energieschemas: {s}  (Ausbalanciert)\n"
          "  GUID der Untergruppe: 54533251-82be-4824-96c1-47b60b740d00  (Prozessorenergieverwaltung)\n"
          "    GUID der Energieeinstellung: 893dee8e-2bef-41e0-89c6-b55d0929964c  (Minimaler Leistungszustand)\n"
          "      Minimaler möglicher Wert: 0x00000000\n"
          "      Maximaler möglicher Wert: 0x00000064\n"
          "      Mögliche Einstellungsinkremente: 0x00000001\n"
          "    Index der aktuellen Wechselstromeinstellung: 0x00000005\n"
          "    Index der aktuellen Gleichstromeinstellung: 0x0000000a\n")
    m.CMD_HOOKS.append(lambda a: m.R(qh.format(s=m.PLAN["active"]))
                       if a[:2] == ["powercfg", "/qh"] and a[-1].lower() == m.PROC_MIN else None)
    checks = {c["id"]: c for c in LD.scan()["checks"]}
    assert "cpu_min_state" in checks, "the check never appeared on a non-English Windows"
    assert (checks["cpu_min_state"]["current"], checks["cpu_min_state"]["status"]) == ("5%", "warn")


def test_a_negative_startup_delay_does_not_stop_the_apps_after_it():
    fresh()
    paths = []
    for n in ("first.exe", "second.exe"):
        paths.append(os.path.join(m.ROOT, n))
        open(paths[-1], "w").close()
    SM._save({**SM._DEFAULTS, "stagger_s": 0, "skip_if_running": False,
              "apps": [{"id": "a1", "name": "first", "path": paths[0], "delay_s": -5, "enabled": True},
                       {"id": "a2", "name": "second", "path": paths[1], "delay_s": 0, "enabled": True}]})
    launched = []
    real = SM.subprocess
    SM.subprocess = types.SimpleNamespace(Popen=lambda args, **k: launched.append(args))
    try:
        res = SM.launch_all(manual=True)
    finally:
        SM.subprocess = real
    assert [r["reason"] for r in res["results"]] == ["launched", "launched"]
    assert launched == [["explorer.exe", paths[0]], ["explorer.exe", paths[1]]]


def test_a_wake_during_idle_engage_leaves_the_card_restored():
    """The release used to run in the middle of the engage: it restored the
    card, then the engage's own power cap landed, with idle mode off."""
    fresh()
    calls = []
    capping = threading.Event()

    def apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100):
        if power_pct != 100:
            capping.set()
            time.sleep(0.3)                     # the idle cap lands late
        calls.append((core_offset_mhz, mem_offset_mhz, power_pct))
        return {"ok": True}

    real = IM.gpu_overclock
    IM.gpu_overclock = types.SimpleNamespace(
        get_applied_oc=lambda: {"core": 150, "mem": 500, "power": 100},
        detect_gpu_oc_capability=lambda: {"ok": True, "vendor": "nvidia"},
        apply_oc=apply_oc, is_tuning_active=lambda: False)
    IM._state.update(engaged=False, restore=None)
    IM._save({**IM._DEFAULTS, "switch_power_plan": False, "park_oc": True, "idle_power_pct": 70})
    try:
        t = threading.Thread(target=IM._engage)
        t.start()
        assert capping.wait(5)
        IM.manual_release()
        t.join()
    finally:
        IM.gpu_overclock = real
    assert calls == [(0, 0, 70), (150, 500, 100)], calls
    assert IM._state["engaged"] is False
