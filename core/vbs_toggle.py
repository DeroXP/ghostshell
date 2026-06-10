"""VBS/HVCI Memory Integrity toggle for gaming performance."""
import json
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("vbs")


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


def get_vbs_status() -> dict:
    """Get current VBS and HVCI status."""
    log.info("Checking VBS/HVCI status...")
    result = {"vbs_running": False, "hvci_running": False, "hvci_enabled": None, "vbs_enabled": None}

    # Check via WMI
    r = run_ps(
        "Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\\Microsoft\\Windows\\DeviceGuard "
        "| Select-Object VirtualizationBasedSecurityStatus,SecurityServicesRunning,SecurityServicesConfigured "
        "| ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            # VBS status: 0=not enabled, 1=enabled not running, 2=enabled and running
            vbs_status = d.get("VirtualizationBasedSecurityStatus", 0)
            result["vbs_running"] = vbs_status == 2
            result["vbs_enabled"] = vbs_status >= 1
            # Services: 1=Credential Guard, 2=HVCI
            services = d.get("SecurityServicesRunning", []) or []
            if not isinstance(services, list):
                services = [services]
            result["hvci_running"] = 2 in services
        except Exception:
            pass

    # Check registry for HVCI enabled state
    r2 = run_cmd(["reg", "query",
                   r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
                   "/v", "Enabled"])
    if r2["ok"] and r2["out"]:
        result["hvci_enabled"] = "0x1" in r2["out"]

    # Check VBS registry
    r3 = run_cmd(["reg", "query",
                   r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard",
                   "/v", "EnableVirtualizationBasedSecurity"])
    if r3["ok"] and r3["out"]:
        result["vbs_enabled"] = "0x1" in r3["out"] or "0x2" in r3["out"]

    # Check Virtual Machine Platform feature
    r4 = run_ps("Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform | Select-Object State | ConvertTo-Json")
    if r4["ok"] and r4["out"]:
        try:
            d4 = json.loads(r4["out"])
            result["vm_platform_enabled"] = d4.get("State", 0) == 2  # 2 = Enabled
        except Exception:
            result["vm_platform_enabled"] = None
    else:
        result["vm_platform_enabled"] = None

    # Detect CPU generation for impact estimate
    r5 = run_ps("Get-CimInstance Win32_Processor | Select-Object Name,Description | ConvertTo-Json")
    cpu_name = ""
    if r5["ok"] and r5["out"]:
        try:
            d5 = json.loads(r5["out"])
            if isinstance(d5, list):
                d5 = d5[0]
            cpu_name = d5.get("Name", "")
        except Exception:
            pass

    # Newer CPUs with MBEC/GMET have less performance impact
    has_mbec = False
    if cpu_name:
        name_lower = cpu_name.lower()
        # Intel 7th gen+ has MBEC, AMD Zen 2+ has GMET
        if "intel" in name_lower:
            for gen in ["7th", "8th", "9th", "10th", "11th", "12th", "13th", "14th"]:
                if gen in name_lower:
                    has_mbec = True
                    break
            if any(x in name_lower for x in ["i9-1", "i7-1", "i5-1", "i3-1", "core ultra"]):
                has_mbec = True
        elif "amd" in name_lower or "ryzen" in name_lower:
            if any(x in name_lower for x in ["3600", "3700", "3800", "3900", "3950",
                                              "5600", "5700", "5800", "5900", "5950",
                                              "7600", "7700", "7800", "7900", "7950",
                                              "9600", "9700", "9800", "9900", "9950"]):
                has_mbec = True

    result["cpu_name"] = cpu_name
    result["has_mbec"] = has_mbec
    result["estimated_impact"] = "5-10% FPS" if has_mbec else "10-25% FPS"

    log.info(f"  VBS: {'running' if result['vbs_running'] else 'off'}, "
             f"HVCI: {'running' if result['hvci_running'] else 'off'}, "
             f"Impact estimate: {result['estimated_impact']}")
    return result


def disable_hvci() -> dict:
    """Disable Memory Integrity (HVCI)."""
    log.info("Disabling Memory Integrity (HVCI)...")
    backup_registry(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "hvci_backup"
    )
    ok = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "Enabled", "0"
    )
    ok2 = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "Locked", "0"
    )
    log.info(f"  {'✓' if ok else '✗'} HVCI disabled (reboot required)")
    return {"ok": ok, "reboot_required": True}


def enable_hvci() -> dict:
    """Re-enable Memory Integrity (HVCI)."""
    log.info("Re-enabling Memory Integrity (HVCI)...")
    ok = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "Enabled", "1"
    )
    log.info(f"  {'✓' if ok else '✗'} HVCI enabled (reboot required)")
    return {"ok": ok, "reboot_required": True}


def disable_vbs() -> dict:
    """Disable Virtualization-Based Security entirely."""
    log.info("Disabling VBS...")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard", "vbs_backup")

    results = []
    # Disable VBS
    ok1 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard",
               "EnableVirtualizationBasedSecurity", "0")
    results.append({"name": "Disable VBS", "ok": ok1})

    # Disable HVCI
    ok2 = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "Enabled", "0")
    results.append({"name": "Disable HVCI", "ok": ok2})

    # Disable Credential Guard
    ok3 = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\CredentialGuard",
        "Enabled", "0")
    results.append({"name": "Disable Credential Guard", "ok": ok3})

    # Disable Virtual Machine Platform feature
    r = run_ps("Disable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -NoRestart -ErrorAction SilentlyContinue")
    results.append({"name": "Disable VM Platform", "ok": r["ok"] or "not found" in r["err"].lower()})

    log.info("  VBS fully disabled (reboot required)")
    return {"ok": all(r["ok"] for r in results), "results": results, "reboot_required": True}


def enable_vbs() -> dict:
    """Re-enable VBS."""
    log.info("Re-enabling VBS...")
    ok1 = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard",
               "EnableVirtualizationBasedSecurity", "1")
    ok2 = _reg(
        r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        "Enabled", "1")
    return {"ok": ok1 and ok2, "reboot_required": True}
