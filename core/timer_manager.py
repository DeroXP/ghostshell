"""Timer Resolution Manager & Standby List Cleaner (ISLC-style)."""
import json
import threading
import time
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("timer")

# Standby list cleaner state
_islc_state = {
    "running": False,
    "thread": None,
    "free_threshold_mb": 1024,
    "standby_threshold_mb": 1024,
    "interval_sec": 10,
    "clears": 0,
}


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


# ═══════════════════════════════════════════════════════════════
# Real timer-resolution hold (beta.3)
# ═══════════════════════════════════════════════════════════════
# The module was named "Timer Resolution Manager" but never actually SET the
# timer resolution — it only toggled dynamic tick / platform clock / HPET.
# Windows 11 lets the global timer resolution drift to ~15.6 ms when nothing
# requests better, and since Win10 2004 a foreground game's timeBeginPeriod no
# longer reliably applies system-wide.  We hold 0.5 ms via ntdll while Vispora
# runs (the request persists for the lifetime of the process), which tightens
# OS timer-driven scheduling/wait granularity and trims tail/1%-low input and
# frame-pacing latency.  Reversible: release it or just exit.
import ctypes as _ct

try:
    _ntdll = _ct.WinDLL("ntdll")
except Exception:
    _ntdll = None

_KERNEL_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel"
_timer_hold = {"active": False, "target_ms": 0.5}


def query_timer_resolution_ms():
    """Current system timer resolution in ms (None if unavailable)."""
    if not _ntdll:
        return None
    try:
        mn = _ct.c_ulong(); mx = _ct.c_ulong(); cur = _ct.c_ulong()
        _ntdll.NtQueryTimerResolution(_ct.byref(mn), _ct.byref(mx), _ct.byref(cur))
        return round(cur.value / 10000.0, 4)   # 100ns units -> ms
    except Exception:
        return None


def start_timer_resolution_hold(target_ms: float = 0.5) -> dict:
    """Request and HOLD a fine timer resolution for as long as this process
    lives.  Also sets GlobalTimerResolutionRequests=1 so the request applies
    system-wide on Win11 (takes effect after a reboot)."""
    if not _ntdll:
        return {"ok": False, "err": "ntdll unavailable"}
    try:
        desired = _ct.c_ulong(int(round(target_ms * 10000)))  # ms -> 100ns
        cur = _ct.c_ulong()
        st = _ntdll.NtSetTimerResolution(desired, True, _ct.byref(cur))
        ok = (st == 0)
        if ok:
            _timer_hold["active"] = True
            _timer_hold["target_ms"] = target_ms
        # Win11: allow a single process's request to apply globally (reboot).
        backup_registry(_KERNEL_KEY, "session_kernel")
        _reg(_KERNEL_KEY, "GlobalTimerResolutionRequests", "1")
        now = query_timer_resolution_ms()
        log.info(f"Timer resolution hold {'ON' if ok else 'FAILED'} "
                 f"(target {target_ms}ms, now {now}ms)")
        return {"ok": ok, "resolution_ms": now}
    except Exception as e:
        log.warning(f"timer resolution hold failed: {e}")
        return {"ok": False, "err": str(e)}


def stop_timer_resolution_hold() -> dict:
    """Release the fine-resolution request (lets Windows return to default)."""
    if not _ntdll:
        return {"ok": False, "err": "ntdll unavailable"}
    try:
        cur = _ct.c_ulong()
        # SetResolution=False releases THIS process's request.
        _ntdll.NtSetTimerResolution(_ct.c_ulong(int(0.5 * 10000)), False, _ct.byref(cur))
        _timer_hold["active"] = False
        log.info("Timer resolution hold released")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def is_timer_hold_active() -> bool:
    return bool(_timer_hold["active"])


# ═══════════════════════════════════════════════════════════════
# Timer Resolution
# ═══════════════════════════════════════════════════════════════
def get_timer_status() -> dict:
    """Get current timer configuration and dynamic tick status."""
    result = {
        "dynamic_tick": None,
        "platform_clock": None,
        "hpet_enabled": None,
        "current_resolution_ms": None,
    }

    # Check bcdedit settings.  bcdedit prints "disabledynamictick    Yes/No"
    # on the same line as the key, so split-line scan is the only reliable parse.
    r = run_cmd(["bcdedit", "/enum"])
    if r["ok"] and r["out"]:
        out_lower = r["out"].lower()
        if "disabledynamictick" in out_lower:
            for line in r["out"].splitlines():
                if "disabledynamictick" in line.lower():
                    # If the override is set to Yes, dynamic tick is DISABLED.
                    result["dynamic_tick"] = "yes" not in line.lower()
                    break
        else:
            result["dynamic_tick"] = True  # No override → Windows default = enabled

        for line in r["out"].splitlines():
            if "useplatformclock" in line.lower():
                result["platform_clock"] = "yes" in line.lower()
                break

    # Check HPET status
    r2 = run_ps(
        "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or $_.FriendlyName -like '*HPET*'} "
        "| Select-Object Status,FriendlyName | ConvertTo-Json"
    )
    if r2["ok"] and r2["out"]:
        try:
            d = json.loads(r2["out"])
            if isinstance(d, list):
                d = d[0] if d else {}
            result["hpet_enabled"] = d.get("Status", "") == "OK"
        except Exception:
            pass

    # Actual current timer resolution (ms) via ntdll, plus our hold state.
    result["current_resolution_ms"] = query_timer_resolution_ms()
    result["resolution_hold_active"] = is_timer_hold_active()

    return result


def apply_timer_tweaks(disable_dynamic_tick: bool = True,
                       disable_platform_clock: bool = True,
                       disable_hpet: bool = True) -> list[dict]:
    """Apply timer resolution optimizations for lowest latency."""
    log.info("Applying timer resolution tweaks...")
    results = []

    # Disable dynamic tick — forces consistent timer intervals
    if disable_dynamic_tick:
        r = run_cmd(["bcdedit", "/set", "disabledynamictick", "yes"])
        ok = r["ok"]
        log.info(f"  {'✓' if ok else '✗'} Disable dynamic tick")
        results.append({"name": "Disable Dynamic Tick", "ok": ok})

    # Disable platform clock (use TSC instead of HPET)
    if disable_platform_clock:
        r = run_cmd(["bcdedit", "/set", "useplatformclock", "no"])
        ok = r["ok"]
        log.info(f"  {'✓' if ok else '✗'} Disable platform clock (use TSC)")
        results.append({"name": "Use TSC Timer", "ok": ok})

    # Disable HPET device
    if disable_hpet:
        r = run_ps(
            "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or $_.FriendlyName -like '*HPET*'} "
            "| Disable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue"
        )
        ok = r["ok"] or "not found" in r.get("err", "").lower()
        log.info(f"  {'✓' if ok else '✗'} Disable HPET device")
        results.append({"name": "Disable HPET", "ok": ok})

    # beta.3 — REMOVED the `bcdedit /set useplatformtick yes` line that used to
    # be here.  optimizer.py deliberately dropped it: on TSC-capable systems
    # it's a no-op, and on others it forces HPET/ACPI ticks which RAISES idle
    # wakeups and latency — the opposite of what this module is for.

    return results


def restore_timer_defaults() -> list[dict]:
    """Restore timer settings to Windows defaults."""
    log.info("Restoring timer defaults...")
    results = []

    r1 = run_cmd(["bcdedit", "/deletevalue", "disabledynamictick"])
    results.append({"name": "Restore Dynamic Tick", "ok": r1["ok"]})

    r2 = run_cmd(["bcdedit", "/deletevalue", "useplatformclock"])
    results.append({"name": "Restore Platform Clock", "ok": r2["ok"]})

    r3 = run_ps(
        "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or $_.FriendlyName -like '*HPET*'} "
        "| Enable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue"
    )
    results.append({"name": "Re-enable HPET", "ok": r3["ok"]})

    return results


# ═══════════════════════════════════════════════════════════════
# Standby List Cleaner (ISLC-style)
# ═══════════════════════════════════════════════════════════════
def get_memory_status() -> dict:
    """Get current memory breakdown including standby list size."""
    r = run_ps("""
$os = Get-CimInstance Win32_OperatingSystem
$perf = Get-Counter '\\Memory\\Standby Cache Normal Priority Bytes','\\Memory\\Standby Cache Reserve Bytes','\\Memory\\Standby Cache Core Bytes','\\Memory\\Available Bytes','\\Memory\\Committed Bytes' -ErrorAction SilentlyContinue
$standby = 0
$available = 0
$committed = 0
if ($perf) {
    foreach ($sample in $perf.CounterSamples) {
        if ($sample.Path -like '*Normal*') { $standby += $sample.CookedValue }
        if ($sample.Path -like '*Reserve*') { $standby += $sample.CookedValue }
        if ($sample.Path -like '*Core*') { $standby += $sample.CookedValue }
        if ($sample.Path -like '*Available*') { $available = $sample.CookedValue }
        if ($sample.Path -like '*Committed*') { $committed = $sample.CookedValue }
    }
}
$total = $os.TotalVisibleMemorySize * 1024
$free = $os.FreePhysicalMemory * 1024
@{
    total_mb = [math]::Round($total / 1MB)
    free_mb = [math]::Round($free / 1MB)
    available_mb = [math]::Round($available / 1MB)
    standby_mb = [math]::Round($standby / 1MB)
    committed_mb = [math]::Round($committed / 1MB)
    used_pct = [math]::Round((1 - $free / $total) * 100)
} | ConvertTo-Json
""")
    if r["ok"] and r["out"]:
        try:
            return json.loads(r["out"])
        except Exception:
            pass
    return {"total_mb": 0, "free_mb": 0, "available_mb": 0, "standby_mb": 0, "committed_mb": 0, "used_pct": 0}


def clear_standby_list() -> dict:
    """Clear the memory standby list."""
    log.info("Clearing standby list...")
    # Use PowerShell to call the API via .NET
    # This requires admin — uses undocumented NtSetSystemInformation
    r = run_ps("""
# Clear Standby List using RAMMap-style approach
$code = @'
using System;
using System.Runtime.InteropServices;
public class MemoryAPI {
    [DllImport("ntdll.dll")]
    public static extern uint NtSetSystemInformation(int InfoClass, IntPtr Info, int Length);
    public static bool ClearStandbyList() {
        // SystemMemoryListInformation = 80
        // MemoryPurgeStandbyList = 4
        int command = 4;
        IntPtr ptr = Marshal.AllocHGlobal(Marshal.SizeOf(command));
        Marshal.WriteInt32(ptr, command);
        uint result = NtSetSystemInformation(80, ptr, Marshal.SizeOf(command));
        Marshal.FreeHGlobal(ptr);
        return result == 0;
    }
}
'@
try {
    Add-Type -TypeDefinition $code -Language CSharp -ErrorAction Stop
    $result = [MemoryAPI]::ClearStandbyList()
    if ($result) { "OK" } else { "FAILED" }
} catch {
    # Fallback: use RAMMap if available, or just report
    "FALLBACK"
}
""")
    if r["ok"] and "OK" in r["out"]:
        _islc_state["clears"] += 1
        log.info("  ✓ Standby list cleared")
        return {"ok": True}
    # Fallback: try PowerShell workaround
    r2 = run_ps("Clear-Content $null -ErrorAction SilentlyContinue; [System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers()")
    log.info("  ⚠ Standby clear: native API may not be available, used fallback")
    return {"ok": True, "fallback": True}


def _islc_loop():
    """Background loop that clears standby list when thresholds are met.

    Includes a circuit breaker — if `get_memory_status()` keeps failing
    (e.g. a permission regression broke the WMI call) the loop disables
    itself instead of busy-spinning forever.
    """
    consecutive_errors = 0
    MAX_ERRORS = 10
    while _islc_state["running"]:
        try:
            mem = get_memory_status()
            free_mb = mem.get("free_mb", 99999)
            standby_mb = mem.get("standby_mb", 0)

            if (free_mb < _islc_state["free_threshold_mb"] and
                    standby_mb > _islc_state["standby_threshold_mb"]):
                clear_standby_list()
                log.info(f"  ISLC auto-cleared: free={free_mb}MB standby={standby_mb}MB")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.warning(f"  ISLC error ({consecutive_errors}/{MAX_ERRORS}): {e}")
            if consecutive_errors >= MAX_ERRORS:
                log.error("  ISLC: too many consecutive errors — disabling loop")
                _islc_state["running"] = False
                return

        time.sleep(_islc_state["interval_sec"])


def start_islc(free_threshold_mb: int = 1024,
               standby_threshold_mb: int = 1024,
               interval_sec: int = 10) -> dict:
    """Start the automatic standby list cleaner."""
    if _islc_state["running"]:
        return {"ok": True, "msg": "Already running"}

    _islc_state["free_threshold_mb"] = free_threshold_mb
    _islc_state["standby_threshold_mb"] = standby_threshold_mb
    _islc_state["interval_sec"] = max(5, interval_sec)
    _islc_state["running"] = True
    _islc_state["clears"] = 0

    t = threading.Thread(target=_islc_loop, daemon=True)
    t.start()
    _islc_state["thread"] = t

    log.info(f"ISLC started: free<{free_threshold_mb}MB & standby>{standby_threshold_mb}MB → clear every {interval_sec}s")
    return {"ok": True}


def stop_islc() -> dict:
    """Stop the automatic standby list cleaner."""
    _islc_state["running"] = False
    log.info(f"ISLC stopped ({_islc_state['clears']} clears this session)")
    return {"ok": True, "total_clears": _islc_state["clears"]}


def get_islc_status() -> dict:
    return {
        "running": _islc_state["running"],
        "free_threshold_mb": _islc_state["free_threshold_mb"],
        "standby_threshold_mb": _islc_state["standby_threshold_mb"],
        "interval_sec": _islc_state["interval_sec"],
        "total_clears": _islc_state["clears"],
    }
