"""Quick gaming-ready prep on app start — safe, reversible, non-destructive.

Runs in a background thread so it never blocks the UI. All tweaks here are
either temporary (RAM flush) or idempotent registry settings that Windows can
undo via 'Reset All' in the Optimizer page.
"""
import json
import os
import time
import threading
from config import APPDATA_DIR
from core import reg_originals
from core.utils import run_ps, run_cmd, get_logger, atomic_write_json

log = get_logger("boot_prep")

_state = {"running": False, "done": False, "started_at": 0, "finished_at": 0, "steps": []}


_SETTINGS_PATH = os.path.join(APPDATA_DIR, "boot_prep.json")


def _persistent_allowed() -> bool:
    """May the prep change lasting system settings (registry, power plan)?
    Reset All turns this off: the prep runs at every launch, and would
    otherwise re-apply what the user has just asked to have undone."""
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return bool((json.load(f) or {}).get("persistent", True))
    except (OSError, ValueError, AttributeError):
        return True


def set_persistent_allowed(allowed: bool) -> None:
    try:
        atomic_write_json(_SETTINGS_PATH, {"persistent": bool(allowed)})
    except Exception as e:
        log.debug(f"could not save boot-prep setting: {e}")


def get_status() -> dict:
    return dict(_state, persistent=_persistent_allowed())


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


def _reg(key: str, val: str, data: str, vtype: str = "REG_DWORD") -> bool:
    # Recorded like any optimizer tweak, so Reset All can put back whatever
    # was there before.  These used to be written at every launch with no
    # record at all.
    reg_originals.remember(key, val, data, vtype)
    return run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])["ok"]


_GAMES_TASK = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
_SYSTEM_PROFILE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"


def _mmcss_gaming_profile() -> bool:
    """Tell MMCSS the Games task has priority."""
    _reg(_GAMES_TASK, "GPU Priority", "8")
    _reg(_GAMES_TASK, "Priority", "6")
    _reg(_GAMES_TASK, "Scheduling Category", "High", "REG_SZ")
    _reg(_GAMES_TASK, "SFIO Priority", "High", "REG_SZ")
    return True


def _foreground_boost() -> bool:
    """Boost foreground apps a little (0x26: short, variable quanta)."""
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", "38")
    _reg(r"HKCU\Control Panel\Desktop", "ForegroundLockTimeout", "0")
    return True


def _network_throttle_off() -> bool:
    """Disable the 10-packets-per-ms multimedia network throttle."""
    _reg(_SYSTEM_PROFILE, "NetworkThrottlingIndex", "4294967295")
    _reg(_SYSTEM_PROFILE, "SystemResponsiveness", "10")
    return True


def _flush_dns() -> bool:
    r = run_cmd(["ipconfig", "/flushdns"], timeout=8)
    return r["ok"]


def _high_performance_power() -> bool:
    """Switch to High Performance power plan (non-destructive — can be changed back anytime)."""
    from core import optimizer
    # Note the plan the user is on BEFORE leaving it.  Nothing used to: by the
    # time the optimizer recorded "the user's plan" for Reset All to return
    # to, this step had already replaced it with High Performance.
    optimizer._capture_power_baseline()
    if optimizer.active_power_plan().lower() in optimizer._own_power_plans():
        # Already on a performance plan.  Switching anyway used to drop the
        # optimizer's Ultimate Performance plan back to High Performance at
        # every launch.
        return True
    # Try Ultimate Performance first
    r = run_cmd(["powercfg", "/setactive", "e9a42b02-d5df-448d-aa00-03f14749eb61"], timeout=8)
    if r["ok"]:
        return True
    # Fallback to High Performance GUID
    r = run_cmd(["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"], timeout=8)
    return r["ok"]


def prep_for_gaming_async(on_done=None, force=False):
    """Run boot prep in a background thread. Safe to call multiple times — becomes a no-op if already running/done."""
    if _state["running"] or (_state["done"] and not force):
        return
    if force:
        set_persistent_allowed(True)        # asked for by hand ("Re-run Prep")
    _state["running"] = True                # claimed here, not in the thread: two quick calls ran it twice

    def _runner():
        _state["running"] = True
        _state["started_at"] = time.time()
        _state["steps"] = []
        try:
            log.info("=== BOOT PREP: priming system for gaming ===")
            _step("Flush DNS cache", _flush_dns)
            _step("Trim working sets", _flush_working_sets)
            _step("Clear standby list", _clear_standby_list)
            if _persistent_allowed():
                _step("MMCSS gaming profile", _mmcss_gaming_profile)
                _step("Foreground boost", _foreground_boost)
                _step("Network throttle off", _network_throttle_off)
                _step("High performance power", _high_performance_power)
            else:
                log.info("  system tweaks skipped: Reset All was used (Re-run Prep turns them back on)")
        except Exception as e:
            # Belt-and-suspenders: individual steps already catch their own
            # errors via _step(), but if anything here still slips through,
            # we must not leave "running" stuck True / "done" stuck False —
            # that would silently wedge this feature for the rest of the session.
            log.warning(f"boot prep runner raised unexpectedly: {e}")
        finally:
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
