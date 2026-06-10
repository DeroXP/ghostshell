"""Real-time hardware monitoring & DPC latency measurement.

3.3.2 — three live-dash sensors got robustness work:

  * CPU temp — the old chain (MSAcpi_ThermalZoneTemperature then a
    single LibreHardwareMonitor query) silently returned None on
    modern desktops without LHM installed, leaving the tile blank.
    New approach tries LHM, OHM, ACPI in sequence and logs each
    miss at INFO level so it's clear from ghostshell.log which
    source paid off (or whether none did).

  * RAM standby — Get-Counter was returning 0 on this user's machine
    even though the standby cache is several GB.  The PowerShell
    perflib path is touchy.  We now read it directly via
    NtQuerySystemInformation(SystemMemoryListInformation = 80)
    through ctypes — same syscall Process Explorer / Task Manager
    use, no performance-counter dependency.

  * DPC latency — the old code multiplied `% DPC Time` by 100 and
    called the result microseconds.  It isn't — that's just a
    scaled percentage.  Real μs DPC latency requires ETW which we
    don't run.  Renamed the field to DPC Load and surfaced the
    actual percentage from the counter; a healthy idle system
    legitimately sits near 0% (target <0.5%).
"""
import ctypes
import json
import time
import threading
from collections import deque
from ctypes import wintypes
from core.utils import run_ps, run_cmd, get_logger

log = get_logger("monitor")

# Rolling data buffers (60 seconds @ 1 sample/sec)
_metrics = {
    "cpu_temp": deque(maxlen=60),
    "cpu_load": deque(maxlen=60),
    "cpu_clock": deque(maxlen=60),
    "gpu_temp": deque(maxlen=60),
    "gpu_load": deque(maxlen=60),
    "gpu_clock": deque(maxlen=60),
    "gpu_vram_pct": deque(maxlen=60),
    "ram_pct": deque(maxlen=60),
    "ram_standby_mb": deque(maxlen=60),
    "disk_read_bps": deque(maxlen=60),
    "disk_write_bps": deque(maxlen=60),
    "net_ping_ms": deque(maxlen=60),
    "dpc_latency_us": deque(maxlen=60),   # legacy name — now stores DPC %
    "dpc_pct": deque(maxlen=60),
    "isr_pct": deque(maxlen=60),
}

# Module-level diagnostic — set once when we figure out which CPU temp
# source works on this machine, so we don't spam the log every poll.
_cpu_temp_source: str = ""
_cpu_temp_hint_shown: bool = False

_monitor_state = {
    "running": False,
    "thread": None,
    "latest": {},
    "alerts": [],
    "ping_target": "1.1.1.1",
}

# Alert thresholds
THRESHOLDS = {
    "cpu_temp": 90,
    "gpu_temp": 85,
    "ram_pct": 90,
    "gpu_vram_pct": 95,
}


def _sample_cpu():
    """Sample CPU metrics."""
    data = {"temp": None, "load": None, "clock": None}

    # CPU load: try NT API first (works even with corrupt perflib),
    # fall back to Get-Counter when NT API returns None on the very
    # first sample (no delta baseline yet).
    nt_load = _read_cpu_load_via_ntapi()
    if nt_load is not None:
        data["load"] = nt_load
    else:
        r = run_ps(
            "(Get-Counter '\\Processor(_Total)\\% Processor Time' -ErrorAction SilentlyContinue).CounterSamples[0].CookedValue",
            timeout=5
        )
        if r["ok"] and r["out"]:
            try:
                data["load"] = round(float(r["out"].strip()), 1)
            except ValueError:
                pass

    # CPU clock speed
    r2 = run_ps(
        "(Get-CimInstance Win32_Processor).CurrentClockSpeed",
        timeout=5
    )
    if r2["ok"] and r2["out"]:
        try:
            data["clock"] = int(r2["out"].strip())
        except ValueError:
            pass

    # CPU temperature — try in order, log which one wins.
    # 1) LibreHardwareMonitor WMI (gold standard, requires LHM installed)
    # 2) OpenHardwareMonitor WMI (older but still common)
    # 3) MSAcpi_ThermalZoneTemperature (ACPI; only works on some laptops)
    # 4) Win32_PerfFormattedData_Counters_ThermalZoneInformation (same
    #    ACPI source surfaced through perfmon; sometimes works when the
    #    direct WMI class doesn't)
    t = _read_cpu_temp_via_lhm()
    if t is not None: data["temp"], data["temp_source"] = t, "LibreHardwareMonitor"
    if data["temp"] is None:
        t = _read_cpu_temp_via_ohm()
        if t is not None: data["temp"], data["temp_source"] = t, "OpenHardwareMonitor"
    if data["temp"] is None:
        t = _read_cpu_temp_via_msacpi()
        if t is not None: data["temp"], data["temp_source"] = t, "MSAcpi_ThermalZone"
    if data["temp"] is None:
        t = _read_cpu_temp_via_perf_thermal()
        if t is not None: data["temp"], data["temp_source"] = t, "PerfFormatted ThermalZone"

    # Surface the source we landed on (or didn't) exactly once so
    # ghostshell.log records which read path is paying off without
    # noise on every poll.
    global _cpu_temp_source
    if data["temp"] is not None and data.get("temp_source") and data["temp_source"] != _cpu_temp_source:
        log.info(f"CPU temp source: {data['temp_source']} ({data['temp']}°C)")
        _cpu_temp_source = data["temp_source"]
    elif data["temp"] is None and _cpu_temp_source != "<none>":
        # 3.3.3: LibreHardwareMonitor is now auto-installed by the
        # dependency manager + auto-launched on boot, so this warning
        # should be rare.  When it does fire, the auto-install probably
        # hit an error; check ghostshell.log for "lhm-winget" install
        # output to see why.
        log.warning(
            "CPU temp unavailable — neither LibreHardwareMonitor (auto-installed) "
            "nor any other WMI temp provider returned a sane reading.  "
            "Check %APPDATA%/GhostShell/install_logs/ for 'lhm-winget' output, "
            "or open Settings > Dependencies and click 'Install' on the LHM row."
        )
        _cpu_temp_source = "<none>"

    return data


# ─── CPU temp readers ───────────────────────────────────────────────
def _sane_temp(v) -> bool:
    """A real CPU temp is 10-110 °C.  Reject 0, negative, and the
    classic ACPI 27.85 °C placeholder (which is 301.15 K → exactly
    27.85 when the ThermalZone has no real sensor backing it)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    if not (10.0 < f < 120.0):
        return False
    # MSAcpi returns 273.15 K (= 0 C) or 301.15 K (= 28 C) on systems
    # that wire the class but have no real sensor.  28.0 is the most
    # common false-positive.
    if abs(f - 27.85) < 0.5 or abs(f - 28.0) < 0.5:
        return False
    return True


def _read_cpu_temp_via_lhm():
    """Try LibreHardwareMonitor's WMI provider.  Picks the Package
    sensor by name (matches Intel + AMD)."""
    r = run_ps(
        "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue "
        "| Where-Object {$_.SensorType -eq 'Temperature' -and ($_.Name -like '*Package*' -or $_.Name -like '*CCD1*' -or $_.Name -like '*Tctl*')} "
        "| Select-Object -First 1 Value | ConvertTo-Json -Compress",
        timeout=4)
    if not (r.get("ok") and r.get("out")):
        return None
    try:
        d = json.loads(r["out"])
        v = d.get("Value") if isinstance(d, dict) else None
        if _sane_temp(v):
            return round(float(v), 1)
    except Exception as e:
        log.debug(f"LHM CPU temp parse failed: {e}")
    return None


def _read_cpu_temp_via_ohm():
    """OpenHardwareMonitor — older WMI namespace, same API shape."""
    r = run_ps(
        "Get-CimInstance -Namespace root/OpenHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue "
        "| Where-Object {$_.SensorType -eq 'Temperature' -and ($_.Name -like '*Package*' -or $_.Name -like '*CCD1*')} "
        "| Select-Object -First 1 Value | ConvertTo-Json -Compress",
        timeout=4)
    if not (r.get("ok") and r.get("out")):
        return None
    try:
        d = json.loads(r["out"])
        v = d.get("Value") if isinstance(d, dict) else None
        if _sane_temp(v):
            return round(float(v), 1)
    except Exception as e:
        log.debug(f"OHM CPU temp parse failed: {e}")
    return None


def _read_cpu_temp_via_msacpi():
    """ACPI ThermalZone via WMI.  Reading is tenths of Kelvin."""
    r = run_ps(
        "Get-CimInstance MSAcpi_ThermalZoneTemperature -Namespace root/WMI -ErrorAction SilentlyContinue "
        "| Select-Object -First 1 CurrentTemperature | ConvertTo-Json -Compress",
        timeout=4)
    if not (r.get("ok") and r.get("out")):
        return None
    try:
        d = json.loads(r["out"])
        k = d.get("CurrentTemperature", 0) if isinstance(d, dict) else 0
        if k > 0:
            c = (float(k) / 10.0) - 273.15
            if _sane_temp(c):
                return round(c, 1)
    except Exception as e:
        log.debug(f"MSAcpi CPU temp parse failed: {e}")
    return None


def _read_cpu_temp_via_perf_thermal():
    """Win32_PerfFormattedData_Counters_ThermalZoneInformation.HighPrecisionTemperature
    — ACPI thermal data through the perfmon surface.  Sometimes
    populated when the direct MSAcpi class isn't."""
    r = run_ps(
        "Get-CimInstance -ClassName Win32_PerfFormattedData_Counters_ThermalZoneInformation -ErrorAction SilentlyContinue "
        "| Sort-Object HighPrecisionTemperature -Descending "
        "| Select-Object -First 1 HighPrecisionTemperature | ConvertTo-Json -Compress",
        timeout=4)
    if not (r.get("ok") and r.get("out")):
        return None
    try:
        d = json.loads(r["out"])
        k = d.get("HighPrecisionTemperature", 0) if isinstance(d, dict) else 0
        if k > 0:
            c = (float(k) / 10.0) - 273.15
            if _sane_temp(c):
                return round(c, 1)
    except Exception as e:
        log.debug(f"PerfThermal CPU temp parse failed: {e}")
    return None


def _sample_gpu():
    """Sample GPU metrics via nvidia-smi."""
    data = {"temp": None, "load": None, "clock": None, "vram_pct": None}

    r = run_cmd([
        "nvidia-smi",
        "--query-gpu=temperature.gpu,utilization.gpu,clocks.current.graphics,memory.used,memory.total",
        "--format=csv,noheader,nounits"
    ], timeout=5)

    if r["ok"] and r["out"]:
        parts = r["out"].strip().split(",")
        if len(parts) >= 5:
            try:
                data["temp"] = int(parts[0].strip())
                data["load"] = int(parts[1].strip())
                data["clock"] = int(parts[2].strip())
                used = int(parts[3].strip())
                total = int(parts[4].strip())
                data["vram_pct"] = round(used / total * 100, 1) if total > 0 else 0
            except ValueError:
                pass

    # Fallback: WMI
    # 3.4.0 — LHM GPU fallback broadened for AMD + Intel.  nvidia-smi
    # only covers NVIDIA; on an AMD/Intel dGPU or any iGPU the temp +
    # load come from LibreHardwareMonitor's WMI namespace instead.
    # We match a wider set of sensor names: NVIDIA "GPU Core",
    # AMD "GPU Core"/"GPU Hot Spot", Intel iGPU "GPU Core"/"GPU
    # Temperature".  Pull load too (SensorType=Load, Name~"GPU Core")
    # so the live tile isn't blank on non-NVIDIA hardware.
    if data["temp"] is None or data["load"] is None:
        lhm = _read_gpu_via_lhm()
        if data["temp"] is None and lhm.get("temp") is not None:
            data["temp"] = lhm["temp"]
        if data["load"] is None and lhm.get("load") is not None:
            data["load"] = lhm["load"]
        if data["clock"] is None and lhm.get("clock") is not None:
            data["clock"] = lhm["clock"]

    return data


def _read_gpu_via_lhm() -> dict:
    """Pull GPU temp / load / clock from LibreHardwareMonitor's WMI
    namespace.  Works for any vendor LHM can see (NVIDIA, AMD, Intel,
    iGPU).  Returns {temp, load, clock} with None for anything missing.

    One PowerShell round-trip grabs all GPU sensors at once, then we
    pick the best-matching one per metric in Python — cheaper than
    three separate queries."""
    out = {"temp": None, "load": None, "clock": None}
    r = run_ps(
        "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor "
        "-ErrorAction SilentlyContinue "
        "| Where-Object { $_.Identifier -like '*gpu*' } "
        "| Select-Object Name,SensorType,Value,Identifier | ConvertTo-Json -Compress",
        timeout=5)
    if not (r.get("ok") and r.get("out")):
        return out
    try:
        rows = json.loads(r["out"])
        if isinstance(rows, dict):
            rows = [rows]
    except Exception:
        return out

    def _pick(sensor_type, name_prefs):
        """Return the Value of the first sensor matching sensor_type
        whose name contains any preference (in priority order)."""
        cands = [x for x in rows
                 if (x.get("SensorType") == sensor_type) and x.get("Value") is not None]
        for pref in name_prefs:
            for x in cands:
                if pref in (x.get("Name", "") or "").lower():
                    return x.get("Value")
        # No name match — fall back to the first sensor of this type
        return cands[0].get("Value") if cands else None

    t = _pick("Temperature", ["gpu core", "core", "hot spot", "gpu"])
    l = _pick("Load",        ["gpu core", "gpu", "core", "d3d"])
    c = _pick("Clock",       ["gpu core", "core", "gpu"])
    try:
        if t is not None: out["temp"]  = round(float(t), 1)
        if l is not None: out["load"]  = round(float(l), 1)
        if c is not None: out["clock"] = int(float(c))
    except (TypeError, ValueError):
        pass
    return out


def _sample_ram():
    """Sample RAM usage + standby cache size.

    3.3.2: standby is now read directly via NtQuerySystemInformation
    (SystemMemoryListInformation = class 80) which is the same syscall
    Task Manager + Process Explorer use.  Avoids Get-Counter's perflib
    dependency that was silently returning 0 on the user's machine."""
    used_pct = None
    # Used % via GlobalMemoryStatusEx — cheap, no PowerShell roundtrip.
    try:
        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength",                wintypes.DWORD),
                ("dwMemoryLoad",            wintypes.DWORD),
                ("ullTotalPhys",            ctypes.c_ulonglong),
                ("ullAvailPhys",            ctypes.c_ulonglong),
                ("ullTotalPageFile",        ctypes.c_ulonglong),
                ("ullAvailPageFile",        ctypes.c_ulonglong),
                ("ullTotalVirtual",         ctypes.c_ulonglong),
                ("ullAvailVirtual",         ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual",ctypes.c_ulonglong),
            ]
        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            used_pct = round(float(ms.dwMemoryLoad), 1)
    except Exception as e:
        log.debug(f"GlobalMemoryStatusEx failed: {e}")

    standby_mb = _read_standby_mb_via_ntapi()
    # Fallback to the legacy Get-Counter path only if the NT API didn't
    # work (e.g. ntdll missing — vanishingly rare).
    if standby_mb is None:
        standby_mb = _read_standby_mb_via_getcounter()

    return {
        "used_pct":   used_pct if used_pct is not None else 0,
        "standby_mb": standby_mb if standby_mb is not None else 0,
    }


def _read_standby_mb_via_ntapi() -> int | None:
    """Read SYSTEM_MEMORY_LIST_INFORMATION via NtQuerySystemInformation.
    Standby = sum of PageCountByPriority[0..7] × 4 KB per page.

    Returns the standby cache size in MB, or None on any failure."""
    try:
        class _MEMINFO(ctypes.Structure):
            _fields_ = [
                ("ZeroPageCount",            ctypes.c_size_t),
                ("FreePageCount",            ctypes.c_size_t),
                ("ModifiedPageCount",        ctypes.c_size_t),
                ("ModifiedNoWritePageCount", ctypes.c_size_t),
                ("BadPageCount",             ctypes.c_size_t),
                ("PageCountByPriority",      ctypes.c_size_t * 8),
                ("RepurposedPagesByPriority",ctypes.c_size_t * 8),
                ("ModifiedPageCountPageFile",ctypes.c_size_t),
            ]
        SYSTEM_MEMORY_LIST_INFORMATION = 80
        info = _MEMINFO()
        ret = wintypes.ULONG(0)
        nt = ctypes.windll.ntdll
        status = nt.NtQuerySystemInformation(
            SYSTEM_MEMORY_LIST_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(ret),
        )
        if status != 0:
            # 0xC0000004 = STATUS_INFO_LENGTH_MISMATCH (struct shape changed)
            # 0xC0000003 = STATUS_INVALID_INFO_CLASS  (class not supported)
            log.debug(f"NtQuerySystemInformation(SystemMemoryListInformation) "
                      f"returned {status & 0xFFFFFFFF:#010x}")
            return None
        standby_pages = sum(info.PageCountByPriority[i] for i in range(8))
        # Page size: query SystemInfo for accuracy (typically 4096).
        try:
            class _SYSINFO(ctypes.Structure):
                _fields_ = [
                    ("wProcessorArchitecture",       wintypes.WORD),
                    ("wReserved",                    wintypes.WORD),
                    ("dwPageSize",                   wintypes.DWORD),
                    ("lpMinimumApplicationAddress",  ctypes.c_void_p),
                    ("lpMaximumApplicationAddress",  ctypes.c_void_p),
                    ("dwActiveProcessorMask",        ctypes.c_void_p),
                    ("dwNumberOfProcessors",         wintypes.DWORD),
                    ("dwProcessorType",              wintypes.DWORD),
                    ("dwAllocationGranularity",      wintypes.DWORD),
                    ("wProcessorLevel",              wintypes.WORD),
                    ("wProcessorRevision",           wintypes.WORD),
                ]
            si = _SYSINFO()
            ctypes.windll.kernel32.GetSystemInfo(ctypes.byref(si))
            page_size = int(si.dwPageSize) or 4096
        except Exception:
            page_size = 4096
        return int(round((standby_pages * page_size) / (1024 * 1024)))
    except Exception as e:
        log.debug(f"_read_standby_mb_via_ntapi raised: {e}")
        return None


def _read_standby_mb_via_getcounter() -> int | None:
    """Legacy Get-Counter path — kept as a fallback for the (rare) case
    where NtQuerySystemInformation can't be reached."""
    r = run_ps(
        "$standby = 0; "
        "try { "
            "$perf = Get-Counter "
              "'\\Memory\\Standby Cache Core Bytes',"
              "'\\Memory\\Standby Cache Normal Priority Bytes',"
              "'\\Memory\\Standby Cache Reserve Bytes' -ErrorAction Stop;"
            "foreach ($s in $perf.CounterSamples) { $standby += [int64]$s.CookedValue }"
        "} catch { }"
        "[math]::Round($standby / 1MB)", timeout=5)
    if r.get("ok") and r.get("out"):
        try:
            n = int(r["out"].strip())
            return n if n > 0 else None
        except (ValueError, AttributeError):
            return None
    return None


# ─── NT API: CPU load + DPC rate (perflib-independent) ─────────────
# These work even when Windows Performance Counters are corrupt
# (lodctr /R territory).  Same syscalls Task Manager uses.

class _SYSTEM_PROCESSOR_PERF_INFO(ctypes.Structure):
    _fields_ = [
        ("IdleTime",   ctypes.c_int64),
        ("KernelTime", ctypes.c_int64),
        ("UserTime",   ctypes.c_int64),
        ("Reserved1",  ctypes.c_int64 * 2),
        ("Reserved2",  ctypes.c_ulong),
    ]


class _SYSTEM_INTERRUPT_INFO(ctypes.Structure):
    _fields_ = [
        ("ContextSwitches", ctypes.c_ulong),
        ("DpcCount",        ctypes.c_ulong),
        ("DpcRate",         ctypes.c_ulong),
        ("TimeIncrement",   ctypes.c_ulong),
        ("DpcBypassCount",  ctypes.c_ulong),
        ("ApcBypassCount",  ctypes.c_ulong),
    ]


_SystemProcessorPerformanceInformation = 8
_SystemInterruptInformation             = 23

# Cached prev sample for CPU% delta calc
_cpu_prev_sample: dict = {"ts": 0.0, "idle": 0, "kernel": 0, "user": 0}


def _num_processors() -> int:
    try:
        import os as _os
        return _os.cpu_count() or 1
    except Exception:
        return 1


def _read_cpu_load_via_ntapi():
    """Sample SystemProcessorPerformanceInformation across all CPUs and
    return aggregate CPU % since the previous call.  Returns None on
    the very first call (no baseline) or on syscall failure."""
    global _cpu_prev_sample
    n = _num_processors()
    arr = (_SYSTEM_PROCESSOR_PERF_INFO * n)()
    ret = wintypes.ULONG(0)
    try:
        status = ctypes.windll.ntdll.NtQuerySystemInformation(
            _SystemProcessorPerformanceInformation,
            ctypes.byref(arr),
            ctypes.sizeof(arr),
            ctypes.byref(ret),
        )
    except Exception as e:
        log.debug(f"NtQuery(ProcessorPerf) raised: {e}")
        return None
    if status != 0:
        log.debug(f"NtQuery(ProcessorPerf) returned {status & 0xFFFFFFFF:#010x}")
        return None
    idle = sum(arr[i].IdleTime for i in range(n))
    kernel = sum(arr[i].KernelTime for i in range(n))
    user = sum(arr[i].UserTime for i in range(n))
    now = time.time()
    prev = _cpu_prev_sample
    # First call — store baseline, return None.
    if prev["ts"] <= 0:
        _cpu_prev_sample = {"ts": now, "idle": idle, "kernel": kernel, "user": user}
        return None
    d_idle   = idle   - prev["idle"]
    d_kernel = kernel - prev["kernel"]
    d_user   = user   - prev["user"]
    _cpu_prev_sample = {"ts": now, "idle": idle, "kernel": kernel, "user": user}
    # KernelTime INCLUDES IdleTime per NT semantics.  Total = kernel + user.
    total = d_kernel + d_user
    busy  = total - d_idle
    if total <= 0:
        return None
    return round((busy / total) * 100.0, 1)


def _read_dpc_rate_via_ntapi():
    """Sum DpcRate across all processors via SystemInterruptInformation.
    Returns DPCs/sec, or None on failure.

    Heuristic ranges (from LatencyMon / Process Explorer experience):
      < 5,000  /s : healthy idle
      5k-20k   /s : busy but normal
      > 20,000 /s : something is hammering DPCs (driver issue)
    """
    n = _num_processors()
    arr = (_SYSTEM_INTERRUPT_INFO * n)()
    ret = wintypes.ULONG(0)
    try:
        status = ctypes.windll.ntdll.NtQuerySystemInformation(
            _SystemInterruptInformation,
            ctypes.byref(arr),
            ctypes.sizeof(arr),
            ctypes.byref(ret),
        )
    except Exception as e:
        log.debug(f"NtQuery(Interrupt) raised: {e}")
        return None
    if status != 0:
        log.debug(f"NtQuery(Interrupt) returned {status & 0xFFFFFFFF:#010x}")
        return None
    return int(sum(arr[i].DpcRate for i in range(n)))


def _sample_dpc():
    """Sample CPU time spent in DPCs + interrupts (as percentages).

    3.3.2: the old code multiplied % DPC time by 100 and called the
    result microseconds.  It isn't — actual μs DPC latency requires
    an ETW session which we don't run.  Returning the raw counter
    percentage is honest: a healthy system sits near 0% (target
    <0.5%) and any spike to 1%+ is something the user can investigate
    with LatencyMon.  Returns dict {dpc_pct, isr_pct, ok}; ok=False
    means the perfmon counter couldn't be read at all (different
    from a real 0% reading)."""
    r = run_ps(
        "try { "
            "$perf = Get-Counter "
              "'\\Processor(_Total)\\% DPC Time',"
              "'\\Processor(_Total)\\% Interrupt Time' -ErrorAction Stop;"
            "$dpc = $perf.CounterSamples | Where-Object {$_.Path -like '*DPC*'} | Select-Object -First 1;"
            "$isr = $perf.CounterSamples | Where-Object {$_.Path -like '*Interrupt*'} | Select-Object -First 1;"
            "$out = @{ dpc_pct = [math]::Round($dpc.CookedValue, 3); "
                    "  isr_pct = [math]::Round($isr.CookedValue, 3); ok = $true };"
            "$out | ConvertTo-Json -Compress "
        "} catch { '{\"ok\":false}' }",
        timeout=5)
    perfmon_ok = False
    dpc_pct = None
    isr_pct = None
    if r.get("ok") and r.get("out"):
        try:
            d = json.loads(r["out"])
            if isinstance(d, dict) and d.get("ok"):
                dpc_pct = float(d.get("dpc_pct", 0.0))
                isr_pct = float(d.get("isr_pct", 0.0))
                perfmon_ok = True
        except Exception as e:
            log.debug(f"DPC perfmon parse failed: {e}")
    # Fallback path: even when perflib is corrupt (lodctr /R territory)
    # the kernel interrupt-info syscall still works.  Returns DPCs/sec,
    # which is more meaningful to users than the % anyway — LatencyMon
    # uses the same metric.
    dpc_rate = _read_dpc_rate_via_ntapi()
    return {
        "dpc_pct":   dpc_pct,
        "isr_pct":   isr_pct,
        "dpc_rate":  dpc_rate,
        "ok":        perfmon_ok or (dpc_rate is not None),
        "perfmon_ok": perfmon_ok,
    }


def _sample_ping(target: str = "1.1.1.1"):
    """Quick single ping."""
    r = run_cmd(["ping", "-n", "1", "-w", "500", target], timeout=3)
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            if "time=" in line.lower() or "time<" in line.lower():
                try:
                    if "time<" in line.lower():
                        return 1
                    val = line.lower().split("time=")[1].split("ms")[0].strip()
                    return int(val)
                except (IndexError, ValueError):
                    pass
    return None


def _monitor_loop():
    """Main monitoring loop — samples every 1 second."""
    while _monitor_state["running"]:
        try:
            ts = time.time()

            cpu = _sample_cpu()
            gpu = _sample_gpu()
            ram = _sample_ram()
            dpc = _sample_dpc()
            ping = _sample_ping(_monitor_state["ping_target"])

            # Store in rolling buffers
            if cpu["temp"] is not None:
                _metrics["cpu_temp"].append(cpu["temp"])
            if cpu["load"] is not None:
                _metrics["cpu_load"].append(cpu["load"])
            if cpu["clock"] is not None:
                _metrics["cpu_clock"].append(cpu["clock"])
            if gpu["temp"] is not None:
                _metrics["gpu_temp"].append(gpu["temp"])
            if gpu["load"] is not None:
                _metrics["gpu_load"].append(gpu["load"])
            if gpu["clock"] is not None:
                _metrics["gpu_clock"].append(gpu["clock"])
            if gpu["vram_pct"] is not None:
                _metrics["gpu_vram_pct"].append(gpu["vram_pct"])
            _metrics["ram_pct"].append(ram.get("used_pct", 0))
            _metrics["ram_standby_mb"].append(ram.get("standby_mb", 0))
            # New DPC shape: store percentages.  Keep dpc_latency_us in
            # the history dict so any external graph code that grabs
            # by that key doesn't crash; it just trails None now.
            dpc_pct = dpc.get("dpc_pct") if isinstance(dpc, dict) else None
            isr_pct = dpc.get("isr_pct") if isinstance(dpc, dict) else None
            if dpc_pct is not None: _metrics["dpc_pct"].append(dpc_pct)
            if isr_pct is not None: _metrics["isr_pct"].append(isr_pct)
            if ping is not None:
                _metrics["net_ping_ms"].append(ping)

            # Update latest snapshot
            _monitor_state["latest"] = {
                "ts": ts,
                "cpu_temp": cpu["temp"],
                "cpu_temp_source": cpu.get("temp_source") or "",
                "cpu_load": cpu["load"],
                "cpu_clock": cpu["clock"],
                "gpu_temp": gpu["temp"],
                "gpu_load": gpu["load"],
                "gpu_clock": gpu["clock"],
                "gpu_vram_pct": gpu["vram_pct"],
                "ram_pct": ram.get("used_pct"),
                "ram_standby_mb": ram.get("standby_mb"),
                # New DPC fields.  When the user's perflib is healthy
                # we publish dpc_pct + isr_pct + dpc_rate.  When it's
                # corrupt (lodctr /R territory) only dpc_rate is
                # populated via the NT API fallback.  UI prefers
                # dpc_rate as the primary display since it's the more
                # useful metric and the more available one.
                "dpc_pct":         dpc_pct,
                "isr_pct":         isr_pct,
                "dpc_rate":        dpc.get("dpc_rate") if isinstance(dpc, dict) else None,
                "dpc_ok":          bool(isinstance(dpc, dict) and dpc.get("ok")),
                "dpc_perfmon_ok":  bool(isinstance(dpc, dict) and dpc.get("perfmon_ok")),
                "dpc_latency_us":  None,
                "net_ping_ms":     ping,
            }

            # Check alerts
            alerts = []
            if cpu["temp"] and cpu["temp"] > THRESHOLDS["cpu_temp"]:
                alerts.append(f"CPU temp: {cpu['temp']}°C")
            if gpu["temp"] and gpu["temp"] > THRESHOLDS["gpu_temp"]:
                alerts.append(f"GPU temp: {gpu['temp']}°C")
            if ram.get("used_pct", 0) > THRESHOLDS["ram_pct"]:
                alerts.append(f"RAM: {ram['used_pct']}%")
            if gpu["vram_pct"] and gpu["vram_pct"] > THRESHOLDS["gpu_vram_pct"]:
                alerts.append(f"VRAM: {gpu['vram_pct']}%")
            _monitor_state["alerts"] = alerts

            # Sleep remainder of 1 second
            elapsed = time.time() - ts
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

        except Exception as e:
            log.warning(f"hw_monitor sample failed: {e}")
            time.sleep(1)


def start_monitor(ping_target: str = "1.1.1.1") -> dict:
    """Start the hardware monitor."""
    if _monitor_state["running"]:
        return {"ok": True, "msg": "Already running"}
    _monitor_state["running"] = True
    _monitor_state["ping_target"] = ping_target
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    _monitor_state["thread"] = t
    log.info("Hardware monitor started")
    return {"ok": True}


def stop_monitor() -> dict:
    _monitor_state["running"] = False
    log.info("Hardware monitor stopped")
    return {"ok": True}


def get_monitor_snapshot() -> dict:
    """Get latest readings + rolling history for sparklines."""
    return {
        "running": _monitor_state["running"],
        "latest": _monitor_state["latest"],
        "alerts": _monitor_state["alerts"],
        "history": {k: list(v) for k, v in _metrics.items()},
    }


def get_top_processes(count: int = 10) -> list[dict]:
    """Get top resource-consuming processes."""
    r = run_ps(f"""
Get-Process | Sort-Object CPU -Descending | Select-Object -First {count} Id,ProcessName,
    @{{N='CPU_s';E={{[math]::Round($_.CPU,1)}}}},
    @{{N='RAM_MB';E={{[math]::Round($_.WorkingSet64/1MB,1)}}}} |
ConvertTo-Json
""", timeout=5)
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            return data
        except Exception:
            pass
    return []


def run_benchmark() -> dict:
    """Run a quick system benchmark for before/after comparison."""
    log.info("Running system benchmark...")
    results = {}

    # CPU single-thread: prime sieve timing
    r = run_ps("""
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$count = 0
for ($i = 2; $i -lt 100000; $i++) {
    $isPrime = $true
    for ($j = 2; $j -le [Math]::Sqrt($i); $j++) {
        if ($i % $j -eq 0) { $isPrime = $false; break }
    }
    if ($isPrime) { $count++ }
}
$sw.Stop()
@{ primes = $count; elapsed_ms = $sw.ElapsedMilliseconds } | ConvertTo-Json
""", timeout=30)
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            results["cpu_single"] = {
                "primes_found": d.get("primes", 0),
                "elapsed_ms": d.get("elapsed_ms", 0),
                "score": round(100000 / max(d.get("elapsed_ms", 1), 1) * 1000, 1),
            }
        except Exception:
            pass

    # Memory bandwidth: large array copy
    r2 = run_ps("""
$size = 100MB
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$src = New-Object byte[] $size
$dst = New-Object byte[] $size
[Array]::Copy($src, $dst, $size)
$sw.Stop()
@{ size_mb = 100; elapsed_ms = $sw.ElapsedMilliseconds } | ConvertTo-Json
""", timeout=15)
    if r2["ok"] and r2["out"]:
        try:
            d = json.loads(r2["out"])
            ms = max(d.get("elapsed_ms", 1), 1)
            results["memory"] = {
                "bandwidth_mbps": round(100 / (ms / 1000), 1),
                "elapsed_ms": ms,
            }
        except Exception:
            pass

    # Disk sequential write/read (small test)
    r3 = run_ps("""
$testFile = "$env:TEMP\\ghostshell_bench.tmp"
$size = 50MB
$data = New-Object byte[] $size
$sw = [System.Diagnostics.Stopwatch]::StartNew()
[IO.File]::WriteAllBytes($testFile, $data)
$writeMs = $sw.ElapsedMilliseconds
$sw.Restart()
$null = [IO.File]::ReadAllBytes($testFile)
$readMs = $sw.ElapsedMilliseconds
$sw.Stop()
Remove-Item $testFile -Force -ErrorAction SilentlyContinue
@{ write_ms = $writeMs; read_ms = $readMs; size_mb = 50 } | ConvertTo-Json
""", timeout=15)
    if r3["ok"] and r3["out"]:
        try:
            d = json.loads(r3["out"])
            wms = max(d.get("write_ms", 1), 1)
            rms = max(d.get("read_ms", 1), 1)
            results["disk"] = {
                "write_mbps": round(50 / (wms / 1000), 1),
                "read_mbps": round(50 / (rms / 1000), 1),
            }
        except Exception:
            pass

    # Network latency
    ping = _sample_ping("1.1.1.1")
    results["network"] = {"ping_ms": ping}

    # Composite score (arbitrary weighting)
    cpu_score = results.get("cpu_single", {}).get("score", 0)
    mem_bw = results.get("memory", {}).get("bandwidth_mbps", 0)
    disk_r = results.get("disk", {}).get("read_mbps", 0)
    composite = round(cpu_score * 0.4 + mem_bw * 0.1 + disk_r * 0.1 + (1000 / max(ping or 50, 1)) * 0.4, 1)
    results["composite_score"] = composite

    log.info(f"Benchmark complete: composite score = {composite}")
    return results
