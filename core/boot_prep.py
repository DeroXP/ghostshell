"""Quick gaming-ready prep on app start — safe, reversible, non-destructive.

Runs in a background thread so it never blocks the UI. All tweaks here are
either temporary (RAM flush) or idempotent registry settings that Windows can
undo via 'Reset All' in the Optimizer page.
"""
import os
import time
import threading
from core.utils import run_ps, run_cmd, get_logger

log = get_logger("boot_prep")

_state = {"running": False, "done": False, "started_at": 0, "finished_at": 0, "steps": []}


def get_status() -> dict:
    return dict(_state)


def _step(name: str, fn):
    """Run a single prep step and record the outcome."""
    try:
        ok = fn()
        _state["steps"].append({"name": name, "ok": bool(ok)})
        log.info(f"  ✓ {name}" if ok else f"  ~ {name}")
    except Exception as e:
        _state["steps"].append({"name": name, "ok": False, "err": str(e)})
        log.debug(f"  ✗ {name}: {e}")


def _flush_working_sets() -> bool:
    """Trim working sets of all processes — frees RAM without killing anything."""
    r = run_ps("""
try {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Diagnostics;
public class WS {
    [DllImport("kernel32.dll")] public static extern bool SetProcessWorkingSetSize(IntPtr p, int a, int b);
    public static int Go() { int c=0; foreach(Process p in Process.GetProcesses()){try{SetProcessWorkingSetSize(p.Handle,-1,-1);c++;}catch{}} return c; }
}
'@ -ErrorAction SilentlyContinue
    [WS]::Go() | Out-Null
    'OK'
} catch { 'FAIL' }
""", timeout=20)
    return r["ok"] and "OK" in r["out"]


def _clear_standby_list() -> bool:
    """Drop the standby memory list so games get fresh allocations."""
    r = run_ps("""
try {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class SB {
    [DllImport("ntdll.dll")] public static extern uint NtSetSystemInformation(int c, IntPtr i, int l);
    public static bool Go() { int v=4; IntPtr p=Marshal.AllocHGlobal(4); Marshal.WriteInt32(p,v); uint r=NtSetSystemInformation(80,p,4); Marshal.FreeHGlobal(p); return r==0; }
}
'@ -ErrorAction SilentlyContinue
    if ([SB]::Go()) { 'OK' } else { 'FAIL' }
} catch { 'FAIL' }
""", timeout=15)
    return r["ok"] and "OK" in r["out"]


def _mmcss_gaming_profile() -> bool:
    """Tell MMCSS the Games task has priority — foundational latency tweak."""
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
             "/v", "GPU Priority", "/t", "REG_DWORD", "/d", "8", "/f"])
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
             "/v", "Priority", "/t", "REG_DWORD", "/d", "6", "/f"])
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
             "/v", "Scheduling Category", "/t", "REG_SZ", "/d", "High", "/f"])
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
             "/v", "SFIO Priority", "/t", "REG_SZ", "/d", "High", "/f"])
    return True


def _foreground_boost() -> bool:
    """Boost foreground apps a little — balanced, not the aggressive 0x28 value."""
    # 0x26 = Short quantum, variable — gaming-friendly but not as aggressive as 0x28
    run_cmd(["reg", "add", r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
             "/v", "Win32PrioritySeparation", "/t", "REG_DWORD", "/d", "38", "/f"])
    run_cmd(["reg", "add", r"HKCU\Control Panel\Desktop",
             "/v", "ForegroundLockTimeout", "/t", "REG_DWORD", "/d", "0", "/f"])
    return True


def _network_throttle_off() -> bool:
    """Disable the 10-packet-per-ms network throttle — safe, widely recommended."""
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v", "NetworkThrottlingIndex", "/t", "REG_DWORD", "/d", "4294967295", "/f"])
    run_cmd(["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
             "/v", "SystemResponsiveness", "/t", "REG_DWORD", "/d", "10", "/f"])
    return True


def _flush_dns() -> bool:
    r = run_cmd(["ipconfig", "/flushdns"], timeout=8)
    return r["ok"]


def _high_performance_power() -> bool:
    """Switch to High Performance power plan (non-destructive — can be changed back anytime)."""
    # Try Ultimate Performance first
    r = run_cmd(["powercfg", "/setactive", "e9a42b02-d5df-448d-aa00-03f14749eb61"], timeout=8)
    if r["ok"]:
        return True
    # Fallback to High Performance GUID
    r = run_cmd(["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"], timeout=8)
    return r["ok"]


def prep_for_gaming_async(on_done=None):
    """Run boot prep in a background thread. Safe to call multiple times — becomes a no-op if already running/done."""
    if _state["running"] or _state["done"]:
        return

    def _runner():
        _state["running"] = True
        _state["started_at"] = time.time()
        _state["steps"] = []
        log.info("=== BOOT PREP: priming system for gaming ===")

        _step("Flush DNS cache", _flush_dns)
        _step("Trim working sets", _flush_working_sets)
        _step("Clear standby list", _clear_standby_list)
        _step("MMCSS gaming profile", _mmcss_gaming_profile)
        _step("Foreground boost", _foreground_boost)
        _step("Network throttle off", _network_throttle_off)
        _step("High performance power", _high_performance_power)

        _state["finished_at"] = time.time()
        _state["running"] = False
        _state["done"] = True
        duration = _state["finished_at"] - _state["started_at"]
        log.info(f"=== BOOT PREP: done in {duration:.1f}s ===")

        if on_done:
            try:
                on_done(duration)
            except Exception as e:
                log.debug(f"on_done callback failed: {e}")

    threading.Thread(target=_runner, daemon=True).start()
