"""Ask the built helper each question on this PC and hold the answer against
what the old way says: PowerShell, nvidia-smi, ping, psutil.

    python native/check_parity.py

Everything here only reads the machine.  It loads the DLL directly and imports
nothing of the app, so a live install's data folder and log are not touched.
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

print(f"\n{sum(results)} of {len(results)} checks agree")
sys.exit(0 if all(results) else 1)
