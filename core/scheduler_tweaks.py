"""Advanced CPU Scheduler & Kernel tweaks for gaming."""
import json
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("scheduler")


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


def get_scheduler_status() -> dict:
    """Get current scheduler configuration."""
    result = {}

    # Win32PrioritySeparation
    r = run_cmd(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "/v", "Win32PrioritySeparation"])
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            if "Win32PrioritySeparation" in line and "0x" in line:
                val = line.split("0x")[-1].strip().split()[0]
                result["priority_sep"] = f"0x{val}"

    # MMCSS Games profile
    games_key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
    r2 = run_cmd(["reg", "query", games_key])
    if r2["ok"] and r2["out"]:
        result["mmcss_games"] = {}
        for line in r2["out"].splitlines():
            line = line.strip()
            for field in ["GPU Priority", "Priority", "Scheduling Category", "SFIO Priority", "Background Only"]:
                if field in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        result["mmcss_games"][field] = parts[-1]

    # SystemResponsiveness
    r3 = run_cmd(["reg", "query",
                   r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
                   "/v", "SystemResponsiveness"])
    if r3["ok"] and "0x" in r3["out"]:
        val = r3["out"].split("0x")[-1].strip().split()[0]
        result["system_responsiveness"] = int(val, 16)

    # CPU idle status
    r4 = run_ps("powercfg /query SCHEME_CURRENT SUB_PROCESSOR IdleDisable 2>$null | Select-String 'Current AC'")
    if r4["ok"] and r4["out"]:
        result["idle_disabled"] = "0x00000001" in r4["out"]
    else:
        result["idle_disabled"] = False

    # Check for Intel hybrid CPU
    r5 = run_ps("Get-CimInstance Win32_Processor | Select-Object Name | ConvertTo-Json")
    if r5["ok"] and r5["out"]:
        try:
            d = json.loads(r5["out"])
            if isinstance(d, list):
                d = d[0]
            name = d.get("Name", "").lower()
            result["hybrid_cpu"] = any(x in name for x in ["12th gen", "13th gen", "14th gen", "core ultra", "raptor", "alder"])
        except Exception:
            result["hybrid_cpu"] = False
    else:
        result["hybrid_cpu"] = False

    return result


def apply_mmcss_gaming_profile() -> list[dict]:
    """Apply optimal MMCSS Games task profile."""
    log.info("Applying MMCSS Gaming profile...")
    results = []
    games_key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
    backup_registry(games_key, "mmcss_games")

    tweaks = [
        ("Affinity", "0", "REG_DWORD"),
        ("Background Only", "False", "REG_SZ"),
        ("Background Priority", "1", "REG_DWORD"),  # low background priority
        ("Clock Rate", "10000", "REG_DWORD"),
        ("GPU Priority", "8", "REG_DWORD"),  # max GPU priority
        ("Priority", "6", "REG_DWORD"),  # high thread priority
        ("Scheduling Category", "High", "REG_SZ"),
        ("SFIO Priority", "High", "REG_SZ"),
        ("SFIO Rate", "4", "REG_DWORD"),  # high I/O priority
    ]

    for name, data, vtype in tweaks:
        ok = _reg(games_key, name, data, vtype)
        log.info(f"  {'✓' if ok else '✗'} Games\\{name} = {data}")
        results.append({"name": f"MMCSS Games\\{name}", "ok": ok})

    # Also configure DisplayPostProcessing task
    dpp_key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\DisplayPostProcessing"
    dpp_tweaks = [
        ("Affinity", "0", "REG_DWORD"),
        ("Background Only", "True", "REG_SZ"),
        ("Clock Rate", "10000", "REG_DWORD"),
        ("GPU Priority", "8", "REG_DWORD"),
        ("Priority", "8", "REG_DWORD"),
        ("Scheduling Category", "High", "REG_SZ"),
        ("SFIO Priority", "High", "REG_SZ"),
    ]

    for name, data, vtype in dpp_tweaks:
        ok = _reg(dpp_key, name, data, vtype)
        results.append({"name": f"MMCSS DPP\\{name}", "ok": ok})

    log.info("  ✓ MMCSS profiles configured")
    return results


def apply_priority_separation(mode: str = "competitive") -> dict:
    """
    Set Win32PrioritySeparation.
    competitive = 0x28 (Short, Fixed — lowest input lag)
    balanced = 0x26 (Short, Variable — good balance)
    default = 0x02 (Long, Variable — Windows default)
    """
    log.info(f"Setting Win32PrioritySeparation: {mode}")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "priority_sep")

    values = {
        "competitive": "40",    # 0x28 hex = 40 decimal — Short, Fixed
        "balanced": "38",       # 0x26 hex = 38 decimal — Short, Variable
        "default": "2",         # 0x02 — Long, Variable (Windows default)
    }
    val = values.get(mode, "40")
    ok = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
              "Win32PrioritySeparation", val)
    log.info(f"  {'✓' if ok else '✗'} Win32PrioritySeparation = {val} ({mode})")
    return {"ok": ok, "mode": mode, "value": val}


def toggle_cpu_idle(disable: bool = True) -> dict:
    """Disable/enable CPU idle states during gaming for max clock speeds."""
    log.info(f"{'Disabling' if disable else 'Enabling'} CPU idle...")
    val = "1" if disable else "0"
    r = run_cmd(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "IdleDisable", val])
    run_cmd(["powercfg", "/setactive", "SCHEME_CURRENT"])
    log.info(f"  {'✓' if r['ok'] else '✗'} CPU idle {'disabled' if disable else 'enabled'}")
    return {"ok": r["ok"], "idle_disabled": disable}


def apply_dwm_mpo_fix() -> dict:
    """Apply DWM Multi-Plane Overlay fix for Windows 11 24H2+."""
    log.info("Applying DWM MPO fix...")
    ok = _reg(r"HKCU\Software\Microsoft\Windows\DWM", "OverlayMinFPS", "0")
    log.info(f"  {'✓' if ok else '✗'} DWM OverlayMinFPS = 0")
    return {"ok": ok, "name": "DWM MPO Fix"}


def apply_thread_dpc_lock() -> dict:
    """Lock DPCs to reduce contention (ThreadDpcEnable = 0)."""
    log.info("Locking thread DPCs...")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel", "thread_dpc")
    ok = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel",
              "ThreadDpcEnable", "0")
    log.info(f"  {'✓' if ok else '✗'} ThreadDpcEnable = 0")
    return {"ok": ok}


def disable_spectre_meltdown() -> dict:
    """Disable Spectre/Meltdown mitigations (security tradeoff for ~5-10% perf)."""
    log.info("Disabling Spectre/Meltdown mitigations...")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "spectre")

    # FeatureSettingsOverride = 3, FeatureSettingsOverrideMask = 3 disables all mitigations
    ok1 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
               "FeatureSettingsOverride", "3")
    ok2 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
               "FeatureSettingsOverrideMask", "3")
    ok = ok1 and ok2
    log.info(f"  {'✓' if ok else '✗'} Spectre/Meltdown mitigations disabled")
    return {"ok": ok, "reboot_required": True}


def enable_spectre_meltdown() -> dict:
    """Re-enable Spectre/Meltdown mitigations."""
    log.info("Re-enabling Spectre/Meltdown mitigations...")
    ok1 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
               "FeatureSettingsOverride", "0")
    ok2 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management",
               "FeatureSettingsOverrideMask", "3")
    return {"ok": ok1 and ok2, "reboot_required": True}


def run_full_scheduler_optimize() -> dict:
    """Apply all scheduler optimizations."""
    log.info("═══ FULL SCHEDULER OPTIMIZATION ═══")
    results = {}
    results["mmcss"] = apply_mmcss_gaming_profile()
    results["priority_sep"] = apply_priority_separation("competitive")
    results["cpu_idle"] = toggle_cpu_idle(True)
    results["dwm_mpo"] = apply_dwm_mpo_fix()
    results["thread_dpc"] = apply_thread_dpc_lock()

    # System responsiveness = 10 (audio-safe foreground priority)
    # beta.6: was 0.  0 starves Audiosrv (voice crackle); aligned with the
    # app-wide gaming value 10 that optimizer/network_tweaks use and that
    # auto_recover enforces (it reverts any 0 it detects).
    ok = _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
              "SystemResponsiveness", "10")
    results["sys_responsiveness"] = {"ok": ok}

    # Network throttling disabled
    ok2 = _reg(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
               "NetworkThrottlingIndex", "4294967295")
    results["net_throttle"] = {"ok": ok2}

    log.info("═══ SCHEDULER OPTIMIZATION COMPLETE ═══")
    return results
