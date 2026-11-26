"""Ask the built helper each question on this PC and hold the answer against
what the old way says: PowerShell, nvidia-smi, ping, psutil.

    python native/check_parity.py

Everything here only reads the machine, with one exception: the helper's
"trim working sets" action is tried on two scratch processes this script
starts itself (300 MB of nothing each), once the old way and once through the
helper.  No other process is trimmed, and the standby list is never cleared.
It loads the DLL directly and imports nothing of the app, so a live install's
data folder and log are not touched.
Readings that move between two looks (a temperature, a clock, memory in use)
are compared with a tolerance; identities (which processes, which names, totals
and limits) must match exactly.  Exits 1 if anything does not.
"""
import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DLL = os.path.join(HERE, "target", "release", "vispora_native.dll")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MB = 1048576

if not os.path.isfile(DLL):
    print(f"not built: {DLL}\nrun: cargo build --release --manifest-path native/Cargo.toml")
    sys.exit(2)
lib = ctypes.WinDLL(DLL)
lib.vispora_native_call.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
lib.vispora_native_call.restype = ctypes.c_int32
lib.vispora_native_act.argtypes = lib.vispora_native_call.argtypes
lib.vispora_native_act.restype = ctypes.c_int32


def act(action, /, **args):
    """One of the helper's two actions, asked once."""
    request = json.dumps({**args, "do": action}).encode("utf-8")
    buf = ctypes.create_string_buffer(8192)
    n = lib.vispora_native_act(request, len(request), buf, 8192)
    answer = json.loads(buf.raw[:n].decode("utf-8")) if n > 0 else {"ok": False, "err": "no answer"}
    return answer.get("data") if answer.get("ok") else None


def ask(question, /, **args):
    request = json.dumps({**args, "q": question}).encode("utf-8")
    size = 1 << 16
    while True:
        buf = ctypes.create_string_buffer(size)
        n = lib.vispora_native_call(request, len(request), buf, size)
        if n < 0:
            size = -n + 1024
            continue
        answer = json.loads(buf.raw[:n].decode("utf-8")) if n else {"ok": False, "err": "no answer"}
        return answer.get("data") if answer.get("ok") else None


def powershell(command, timeout=30):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}\n"
                        + command], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NO_WINDOW)
    return r.stdout.strip()


def rows_of(text):
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'ok  ' if ok else 'DIFF'}  {name}" + (f"   {detail}" if detail else ""))


def note(name, detail):
    print(f"      {name}   {detail}")


# ── processes with a main window ──────────────────────────────────────
GET_PROCESS = ("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } "
               "| Select-Object Id,ProcessName,@{N='WS';E={[int]($_.WorkingSet64/1MB)}} | ConvertTo-Json -Compress")
for attempt in range(4):                # (windows open and close between the two looks: try again)
    old = {(r["Id"], r["ProcessName"]): r["WS"] for r in rows_of(powershell(GET_PROCESS))}
    new = {(r["pid"], r["name"]): round(r["ws"] / MB) for r in ask("windowed_processes")}
    mine = {k for k in set(old) ^ set(new) if k[1].lower() in ("powershell", "conhost", "python")}   # the measurers
    if set(old) - mine == set(new) - mine:
        break
    time.sleep(0.5)
same = set(old) - mine == set(new) - mine
check("windowed processes: the same processes, by id and name", same,
      f"{len(new)} of them" if same else f"only PowerShell: {sorted(set(old) - set(new))}  only helper: {sorted(set(new) - set(old))}")
drift = max((abs(old[k] - new[k]) for k in set(old) & set(new)), default=0)
check("windowed processes: working sets within 64 MB of each other", drift <= 64, f"largest difference {drift} MB")

# ── every process's I/O counters ──────────────────────────────────────
procs = ask("process_io")
check("process list: answered", isinstance(procs, list) and len(procs) > 20, f"{len(procs or [])} processes")
try:
    import psutil
except ImportError:
    psutil = None
    note("process list", "psutil is not installed for this Python: names and counters not compared")
if psutil and procs:
    before = {p.info["pid"]: p.info for p in psutil.process_iter(["pid", "name", "io_counters"])}
    procs = ask("process_io")
    after = {p.info["pid"]: p.info for p in psutil.process_iter(["pid", "name", "io_counters"])}
    names = [(r["pid"], r["name"], before[r["pid"]]["name"]) for r in procs
             if r["pid"] in before and r["pid"] in after and before[r["pid"]]["name"] == after[r["pid"]]["name"]]
    # (psutil has no name for a few protected system processes; the helper does)
    wrong = [n for n in names if n[2] and n[1].lower() != n[2].lower()]
    check("process list: the names psutil gives", not wrong, f"{len(names)} compared" if not wrong else str(wrong[:5]))
    between, compared = 0, 0
    for r in procs:
        b, a = before.get(r["pid"]), after.get(r["pid"])
        if b and a and b["io_counters"] and a["io_counters"] and b["name"] == a["name"]:
            compared += 1
            between += b["io_counters"].other_bytes <= r["other_bytes"] <= a["io_counters"].other_bytes
    check("process list: each counter lies between psutil's reading before and after", compared and between == compared,
          f"{between} of {compared}")

# ── the network cards' byte counts (psutil.net_io_counters' own calls) ─
cards = ask("network_cards")
check("network cards: answered", isinstance(cards, list) and len(cards) > 0, f"{len(cards or [])} adapters")
if psutil and cards:
    from psutil import _psplatform
    before = _psplatform.cext.net_io_counters()            # {friendly name: (sent, received, ...)}, as Windows says them
    cards = ask("network_cards")
    after = _psplatform.cext.net_io_counters()
    check("network cards: the adapters psutil lists, by name, in its order",
          list(dict.fromkeys(c["name"] for c in cards)) == list(before) == list(after), f"{[c['name'] for c in cards]} / {list(before)}")
    between = [c["name"] for c in cards if c["name"] in before and c["name"] in after
               and before[c["name"]][0] <= c["sent"] <= after[c["name"]][0] and before[c["name"]][1] <= c["recv"] <= after[c["name"]][1]]
    check("network cards: each count lies between psutil's reading before and after", len(between) == len(cards),
          f"{len(between)} of {len(cards)}")

# ── the graphics card ─────────────────────────────────────────────────
FIELDS = ("temperature.gpu,utilization.gpu,utilization.memory,clocks.current.graphics,clocks.current.memory,"
          "clocks.max.graphics,clocks.max.memory,power.draw,power.limit,fan.speed,memory.used,memory.total")
try:
    smi = subprocess.run(["nvidia-smi", f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip().splitlines()
except FileNotFoundError:
    smi = []
gpu = ask("gpu_live")
if not smi:
    check("graphics card: no nvidia-smi here, and the helper says there is no card", gpu is None)
else:
    s = dict(zip(FIELDS.split(","), (x.strip() for x in smi[0].split(","))))

    def num(key):
        try:
            return float(s[key])
        except ValueError:
            return None
    check("graphics card: answered", isinstance(gpu, dict) and gpu.get("temp_c") is not None)
    if gpu:
        exact = (("clocks.max.graphics", gpu["core_max_mhz"]), ("clocks.max.memory", gpu["mem_max_mhz"]),
                 ("power.limit", None if gpu["power_limit_mw"] is None else round(gpu["power_limit_mw"] / 1000, 2)),
                 ("memory.total", None if gpu["mem_total_bytes"] is None else round(gpu["mem_total_bytes"] / MB)))
        for key, mine_ in exact:
            check(f"graphics card: {key} is the same", num(key) == mine_, f"nvidia-smi {s[key]}, helper {mine_}")
        near = (("temperature.gpu", gpu["temp_c"], 2), ("utilization.gpu", gpu["gpu_util_pct"], 25),
                ("clocks.current.graphics", gpu["core_mhz"], 400), ("clocks.current.memory", gpu["mem_mhz"], 400),
                ("power.draw", None if gpu["power_mw"] is None else gpu["power_mw"] / 1000, 40),
                ("fan.speed", gpu["fan_pct"], 10),
                ("memory.used", None if gpu["mem_used_bytes"] is None else gpu["mem_used_bytes"] / MB, 300))
        for key, mine_, slack in near:
            theirs = num(key)
            ok = (theirs is None and mine_ is None) or (theirs is not None and mine_ is not None
                                                        and abs(theirs - mine_) <= slack)
            check(f"graphics card: {key} within {slack}", ok, f"nvidia-smi {s[key]}, helper {mine_}")

# ── the processor ─────────────────────────────────────────────────────
clock = ask("cpu_clock")
wmi_clock = powershell("(Get-CimInstance Win32_Processor).CurrentClockSpeed")
check("processor clock: what Win32_Processor reports", clock and str(clock["mhz"]) == wmi_clock.splitlines()[0].strip(),
      f"PowerShell {wmi_clock}, helper {clock and clock['mhz']}")
a = ask("cpu_times")
time.sleep(1.0)
b = ask("cpu_times")
total = b["total"] - a["total"]
check("processor times: a second passes on every processor", 0.8e7 < total / a["processors"] < 1.3e7,
      f"{total / a['processors'] / 1e7:.3f} s per processor, {a['processors']} processors")
dpc, isr = (b["dpc"] - a["dpc"]) / total * 100, (b["interrupt"] - a["interrupt"]) / total * 100
check("processor times: DPC and interrupt time are small shares of it", 0 <= dpc < 50 and 0 <= isr < 50,
      f"DPC {dpc:.3f}%, interrupts {isr:.3f}%")
counter = powershell("try { (Get-Counter '\\Processor(_Total)\\% DPC Time' -ErrorAction Stop).CounterSamples[0].CookedValue } "
                     "catch { 'unavailable' }")
note("processor times", f"Get-Counter says % DPC Time = {counter} (its own one-second sample; 'unavailable' where "
                        "the performance-counter library is damaged)")

# ── ping ──────────────────────────────────────────────────────────────
mine_ = ask("ping", target="127.0.0.1", timeout_ms=500)
check("ping: this PC answers itself", mine_ == {"ok": True, "ms": mine_.get("ms")} and mine_["ms"] <= 5, str(mine_))
check("ping: an address that does not answer", ask("ping", target="10.255.255.1", timeout_ms=300) == {"ok": False})
check("ping: a host name is refused (ping.exe keeps those)", ask("ping", target="example.com") is None)
out = subprocess.run(["ping", "-n", "1", "-w", "500", "1.1.1.1"], capture_output=True, text=True,
                     creationflags=NO_WINDOW).stdout
mine_ = ask("ping", target="1.1.1.1", timeout_ms=500)
answered = "TTL=" in out
check("ping: 1.1.1.1 answers both or neither", bool(mine_ and mine_["ok"]) == answered,
      f"ping.exe {'answered' if answered else 'did not'}, helper {mine_}")

# ── WMI ───────────────────────────────────────────────────────────────
QUERIES = (
    ("root\\cimv2", "Win32_OperatingSystem", ("Caption", "Version", "BuildNumber", "OSArchitecture")),
    ("root\\cimv2", "Win32_Processor", ("Name", "NumberOfCores", "NumberOfLogicalProcessors", "MaxClockSpeed")),
    ("root\\cimv2", "Win32_LogicalDisk", ("DeviceID", "Size", "DriveType")),
    ("root\\cimv2", "Win32_PhysicalMemory", ("Capacity", "Speed")),
)
for namespace, cls, props in QUERIES:
    old = rows_of(powershell(f"Get-CimInstance -Namespace {namespace} -ClassName {cls} "
                             f"| Select-Object {','.join(props)} | ConvertTo-Json -Compress"))
    new = ask("wmi", namespace=namespace, query=f"SELECT {', '.join(props)} FROM {cls}")
    key = lambda r: json.dumps([r.get(p) for p in props], sort_keys=True)          # noqa: E731
    same = sorted(map(key, old)) == sorted(map(key, new or []))
    check(f"WMI: {cls} ({', '.join(props)})", same,
          f"{len(old)} row(s)" if same else f"PowerShell {old[:2]}  helper {(new or [])[:2]}")
for namespace in ("root\\LibreHardwareMonitor", "root\\OpenHardwareMonitor"):
    old = rows_of(powershell(f"Get-CimInstance -Namespace {namespace} -ClassName Sensor -ErrorAction SilentlyContinue "
                             "| Where-Object {$_.SensorType -eq 'Temperature'} | Select-Object Name,Value "
                             "| ConvertTo-Json -Compress"))
    new = ask("wmi", namespace=namespace, query="SELECT Name, Value FROM Sensor WHERE SensorType = 'Temperature'")
    check(f"WMI: {namespace} temperature sensors: the same names", [r["Name"] for r in old] == [r["Name"] for r in new],
          f"{len(old)} sensor(s)" + ("" if old else " (the namespace is not there, or not for this user: both say so)"))

# ═════════════════════════════════════════════════════════════════════
# The start-up questions (step 2)
# ═════════════════════════════════════════════════════════════════════
def run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return None, ""


# ── the card's name and driver ────────────────────────────────────────
code, smi_id = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"])
card = ask("gpu_identity")
if not smi_id.strip() or code != 0:
    check("card identity: no nvidia-smi here, and the helper says there is no card", card is None)
else:
    theirs = [x.strip() for x in smi_id.strip().splitlines()[0].split(",")]
    mine_ = card and [card["name"], card["driver_version"], str(round(card["mem_total_bytes"] / MB))]
    check("card identity: name, driver version and memory as nvidia-smi prints them", theirs == mine_,
          f"nvidia-smi {theirs}, helper {mine_}")

# ── the first screen's queries, row for row and in the same order ─────
SCREEN = (
    ("root\\cimv2", "Win32_OperatingSystem", ("Caption", "Version", "BuildNumber", "OSArchitecture",
                                             "TotalVisibleMemorySize"), ""),
    ("root\\cimv2", "Win32_Processor", ("Name", "NumberOfCores", "NumberOfLogicalProcessors", "MaxClockSpeed"), ""),
    ("root\\cimv2", "Win32_PhysicalMemory", ("Capacity", "Speed", "ConfiguredClockSpeed"), ""),
    ("root\\cimv2", "Win32_LogicalDisk", ("DeviceID", "Size"), "DriveType=3"),
    ("root\\cimv2", "Win32_VideoController", ("Name", "DriverVersion", "AdapterRAM", "PNPDeviceID", "Status"), ""),
)
for namespace, cls, props, where in SCREEN:
    old = rows_of(powershell(f"Get-CimInstance {cls}" + (f' -Filter "{where}"' if where else "")
                             + f" | Select-Object {','.join(props)} | ConvertTo-Json -Compress"))
    new = ask("wmi", namespace=namespace, query=f"SELECT {','.join(props)} FROM {cls}" + (f" WHERE {where}" if where else ""))
    same = [[r.get(p) for p in props] for r in old] == [[r.get(p) for p in props] for r in (new or [])]
    check(f"first screen: {cls}, the same rows in the same order", same,
          f"{len(old)} row(s)" if same else f"PowerShell {old[:2]}  helper {(new or [])[:2]}")

old = rows_of(powershell("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object {$_.ServerAddresses} "
                         "| Select-Object InterfaceAlias,ServerAddresses | ConvertTo-Json -Compress"))
new = [r for r in (ask("wmi", namespace="root\\StandardCimv2",
                       query="SELECT InterfaceAlias,ServerAddresses FROM MSFT_DNSClientServerAddress WHERE AddressFamily=2")
                   or []) if r.get("ServerAddresses")]
same = [(r["InterfaceAlias"], list(r["ServerAddresses"])) for r in old] == \
       [(r["InterfaceAlias"], list(r["ServerAddresses"])) for r in new]
check("first screen: DNS servers per adapter, in the same order", same,
      f"{len(old)} adapter(s) with a DNS server" if same else f"PowerShell {old}  helper {new}")

old = rows_of(powershell("Get-PhysicalDisk | Select-Object MediaType,BusType | ConvertTo-Json -Compress"))
new = ask("wmi", namespace="root\\Microsoft\\Windows\\Storage", query="SELECT MediaType,BusType FROM MSFT_PhysicalDisk") or []


def drive_type(disks, nvme, ssd):
    for d in disks:
        if nvme(d):
            return "nvme"
        if ssd(d):
            return "ssd"
    return "hdd" if disks else "unknown"


old_type = drive_type(old, lambda d: "nvme" in str(d.get("BusType", "")).lower(),
                      lambda d: "ssd" in str(d.get("MediaType", "")).lower() or "solid" in str(d.get("MediaType", "")).lower())
new_type = drive_type(new, lambda d: d.get("BusType") == 17, lambda d: d.get("MediaType") == 4)
check("first screen: the drive type, decided by the same disk", old_type == new_type and len(old) == len(new),
      f"{old_type} from {len(old)} disk(s): PowerShell {[(d.get('MediaType'), d.get('BusType')) for d in old]}, "
      f"helper {[(d.get('MediaType'), d.get('BusType')) for d in new]}")

# ── is a WMI namespace there (the LibreHardwareMonitor probe) ─────────
for namespace in ("root/cimv2", "root/LibreHardwareMonitor"):
    old = powershell(f"Get-CimInstance -Namespace {namespace} -ClassName __NAMESPACE -ErrorAction SilentlyContinue "
                     "| Select-Object -First 1 Name")
    new = ask("wmi", namespace=namespace.replace("/", "\\"), query="SELECT Name FROM __NAMESPACE")
    check(f"namespace probe: {namespace} answers the same", bool(old.strip()) == bool(new),
          f"PowerShell {'lists a namespace' if old.strip() else 'prints nothing'}, helper {len(new or [])} row(s)")

# ── services ──────────────────────────────────────────────────────────
for name in ("EventLog", "Spooler", "XboxGipSvc", "PresentMonSharedService", "Intel PresentMon Service",
             "PresentMonService", "VisporaNoSuchService_7f3a"):
    code, out = run(["sc", "query", name])
    old = {"installed": code == 0 and "STATE" in out, "running": code == 0 and "STATE" in out and "RUNNING" in out}
    s = ask("service_state", name=name)
    new = s and {"installed": s["installed"], "running": s["installed"] and s["state"] == 4}
    check(f"service {name}: installed and running as sc query says", old == new, f"sc {old}, helper {s}")

# ── power plans ───────────────────────────────────────────────────────
plan = ask("power_scheme")
code, out = run(["powercfg", "/getactivescheme"])
old_active = next((p for p in out.split() if len(p) == 36 and p.count("-") == 4), "")
check("power: the active plan", plan and plan["guid"] == old_active.lower(), f"powercfg {old_active}, helper {plan and plan['guid']}")
code, out = run(["powercfg", "/list"])
listed = sorted(p.lower() for p in out.split() if len(p) == 36 and p.count("-") == 4)
check("power: the plans there are", plan and sorted(plan["schemes"]) == listed, f"powercfg {len(listed)}, helper {plan and len(plan['schemes'])}")
PROC = "54533251-82be-4824-96c1-47b60b740d00"
SETTINGS = {"idle disable (hidden)": "5d76a2ca-e8c0-402f-a133-2158492d58ad",
            "processor maximum": "bc5038f7-23e0-4960-96da-33abaf5935ec",
            "processor minimum": "893dee8e-2bef-41e0-89c6-b55d0929964c",
            "core parking minimum": "0cc5b647-c1df-4637-891a-dec35c318583",
            "core parking minimum, class 1": "0cc5b647-c1df-4637-891a-dec35c318584",
            "boost mode (hidden)": "be337238-0d82-4146-a960-4f3749d470c7",
            "a setting that does not exist": "12345678-1234-1234-1234-123456789abc"}
import re                                                       # noqa: E402

HEX = re.compile(r"0x[0-9a-fA-F]{8}")
for scheme in (plan["schemes"] if plan else []) + ["12345678-1234-1234-1234-123456789abc"]:
    wrong, compared = [], 0
    for label, setting in SETTINGS.items():
        code, out = run(["powercfg", "/qh", scheme, PROC, setting])
        found = HEX.findall(out) if code == 0 else []
        old = None if len(found) < 2 else {"ac": int(found[-2], 16), "dc": int(found[-1], 16),
                                           "max": int(found[-4], 16) if len(found) >= 5 else None}
        s = ask("power_setting", scheme=scheme, sub=PROC, setting=setting)
        new = s and {"ac": s["ac"], "dc": s["dc"], "max": s["max"]}
        compared += 1
        if old != new:
            wrong.append((label, old, new))
    check(f"power: {compared} processor settings of plan {scheme[:8]} as powercfg /qh prints them", not wrong,
          "values, limits, and the ones that do not exist" if not wrong else str(wrong))

# ═════════════════════════════════════════════════════════════════════
# The actions (step 2).  Tried on scratch processes only.
# ═════════════════════════════════════════════════════════════════════
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
k32.CloseHandle.argtypes = [ctypes.c_void_p]


class _Counters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]


k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_uint32]


def working_set_mb(pid):
    h = k32.OpenProcess(0x1000, 0, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        c = _Counters()
        c.cb = ctypes.sizeof(c)
        k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return round(c.WorkingSetSize / MB)
    finally:
        k32.CloseHandle(h)


SCRATCH = ("import time\nb = bytearray(300 * 1024 * 1024)\nfor i in range(0, len(b), 4096):\n    b[i] = 1\n"
           "print('ready', flush=True)\ntime.sleep(120)\n")
# The app's own PowerShell (core/boot_prep.py), but for one process id instead of every process.
OLD_TRIM = r"""
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Diagnostics;
public class WS {
    [DllImport("kernel32.dll")] public static extern bool SetProcessWorkingSetSize(IntPtr p, int a, int b);
    public static int Go(int pid) { int c=0; Process p = Process.GetProcessById(pid); try{SetProcessWorkingSetSize(p.Handle,-1,-1);c++;}catch{} return c; }
}
'@ -ErrorAction SilentlyContinue
[WS]::Go(PID_HERE) | Out-Null
'OK'
"""


def scratch_process():
    p = subprocess.Popen([sys.executable, "-c", SCRATCH], stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    p.stdout.readline()
    return p


one, two = scratch_process(), scratch_process()
try:
    before = (working_set_mb(one.pid), working_set_mb(two.pid))
    powershell(OLD_TRIM.replace("PID_HERE", str(one.pid)))
    done = act("trim_working_sets", pids=[two.pid])
    time.sleep(0.3)
    after = (working_set_mb(one.pid), working_set_mb(two.pid))
    check("trim: a scratch process is trimmed by the helper as it is by the old PowerShell",
          before[0] > 250 and before[1] > 250 and after[0] < 50 and after[1] < 50
          and done == {"processes": 1, "trimmed": 1, "debug_privilege": done["debug_privilege"]},
          f"old way {before[0]} MB -> {after[0]} MB, helper {before[1]} MB -> {after[1]} MB")
    check("trim: both scratch processes are still running", one.poll() is None and two.poll() is None)
finally:
    one.kill()
    two.kill()
check("trim: a question's door does not open an action", ask("trim_working_sets") is None
      and act("process_io") is None)
if ctypes.windll.shell32.IsUserAnAdmin():
    note("standby list", "not tried: with administrator rights it would really be cleared")
else:
    check("standby list: refused without administrator rights", act("purge_standby_list") is None)

print(f"\n{sum(results)} of {len(results)} checks agree")
sys.exit(0 if all(results) else 1)
