"""What starting one program costs on this PC: time on the clock and processor
time, for the commands the app runs most.

    python tools/measure/process_cost.py [repeats]       (default 7)

Every command here only READS (the process list, counters, sensors, one
ping).  Nothing is changed.

The last line says whether this script itself was running in efficiency mode.
While the app has game mode on, it puts background programs there, this
script included, and everything measures slower than on an idle PC.
"""
import ctypes
import json
import statistics
import subprocess
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
k32.GetProcessTimes.restype = wintypes.BOOL
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
k32.GetPriorityClass.restype = wintypes.DWORD
k32.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]


def _seconds(ft) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def run(args, timeout=30):
    """(seconds on the clock, processor seconds the program used, exit code, bytes printed)."""
    t0 = time.perf_counter()
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    wall = time.perf_counter() - t0
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    cpu = None
    if k32.GetProcessTimes(int(p._handle), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel),
                           ctypes.byref(user)):
        cpu = _seconds(kernel) + _seconds(user)
    return wall, cpu, p.returncode, len(out or b"")


class _Throttle(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def own_state() -> dict:
    handle = k32.GetCurrentProcess()
    state = _Throttle()
    state.Version = 1
    ok = k32.GetProcessInformation(handle, 4, ctypes.byref(state), ctypes.sizeof(state))
    return {"priority_class": hex(k32.GetPriorityClass(handle)),
            "efficiency_mode": bool(ok and (state.ControlMask & 1) and (state.StateMask & 1))}


PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
UTF8 = "try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}\n"
CASES = {
    "powershell, an empty command (the start alone)": PS + [UTF8 + "1"],
    "game monitor: processes with a window": PS + [
        UTF8 + "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } "
        "| Select-Object Id,ProcessName,@{N='WS';E={[int]($_.WorkingSet64/1MB)}} | ConvertTo-Json -Compress"],
    "hardware monitor: processor clock": PS + [UTF8 + "(Get-CimInstance Win32_Processor).CurrentClockSpeed"],
    "hardware monitor: processor temperature (LibreHardwareMonitor)": PS + [
        UTF8 + "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue "
        "| Where-Object {$_.SensorType -eq 'Temperature' -and ($_.Name -like '*Package*' -or $_.Name -like '*CCD1*' "
        "-or $_.Name -like '*Tctl*')} | Select-Object -First 1 Value | ConvertTo-Json -Compress"],
    "hardware monitor: DPC and interrupt time": PS + [
        UTF8 + r"try { $perf = Get-Counter '\Processor(_Total)\% DPC Time','\Processor(_Total)\% Interrupt Time' "
        "-ErrorAction Stop; $perf.CounterSamples | Select-Object Path,CookedValue | ConvertTo-Json -Compress } "
        "catch { '{\"ok\":false}' }"],
    "nvidia-smi, one query": [
        "nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,clocks.current.graphics,memory.used,memory.total",
        "--format=csv,noheader,nounits"],
    "ping, one echo": ["ping", "-n", "1", "-w", "500", "1.1.1.1"],
    "reg query, one value": ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion", "/v", "ProductName"],
    "powercfg, the active plan": ["powercfg", "/getactivescheme"],
    "tasklist with a filter": ["tasklist", "/FI", "IMAGENAME eq LibreHardwareMonitor.exe"],
    "cmd /c exit (a bare start)": ["cmd", "/c", "exit"],
}

repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 7
results = {}
for name, args in CASES.items():
    walls, cpus, code, size = [], [], None, 0
    for _ in range(repeats):
        try:
            wall, cpu, code, size = run(args)
        except FileNotFoundError:
            code = "not installed"
            break
        walls.append(wall)
        cpus.append(cpu or 0.0)
    results[name] = ({"ms": round(1000 * statistics.median(walls)), "slowest_ms": round(1000 * max(walls)),
                      "cpu_ms": round(1000 * statistics.median(cpus)), "exit": code, "bytes_printed": size}
                     if walls else {"exit": code})
results["this script"] = own_state()
print(json.dumps(results, indent=1))
