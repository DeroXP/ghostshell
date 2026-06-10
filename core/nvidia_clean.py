"""GPU optimizations — supports NVIDIA, AMD, and Intel.

File name is kept for backward compatibility with existing imports; the module
now handles all three major GPU vendors and dispatches internally based on
`detect_gpu()` results.
"""
import json
import os
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("gpu")


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"


def _find_gpu_class_key(vendor_match: str) -> str:
    r"""Return the HKLM display-adapter Class\{GUID}\NNNN subkey whose
    DriverDesc matches `vendor_match` (e.g. 'NVIDIA'), falling back to \0000.

    3.4.3-beta.5 — the old code hardcoded \0000.  That is the WRONG device on
    hybrid laptops and multi-GPU systems (e.g. \0000 is the Intel iGPU, or a
    virtual adapter like 'Meta Virtual Monitor'), so `reg add` happily wrote
    every "tweak applied ✓" to the wrong adapter's key.  We now enumerate the
    class and pick the subkey that actually belongs to the target vendor —
    same robustness the AMD path already had."""
    reg_base = r"HKLM\SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
    ps_base  = r"HKLM:\SYSTEM\CurrentControlSet\Control\Class" + "\\" + _DISPLAY_CLASS_GUID
    try:
        ps = (
            "Get-ChildItem '" + ps_base + "' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.PSChildName -match '^[0-9]{4}$' } | "
            "ForEach-Object { "
            "$d=(Get-ItemProperty $_.PSPath -Name DriverDesc -ErrorAction SilentlyContinue).DriverDesc; "
            "if ($d) { ('{0}|{1}' -f $_.PSChildName, $d) } }"
        )
        r = run_ps(ps)
        if r.get("ok") and r.get("out"):
            for line in r["out"].splitlines():
                if "|" not in line:
                    continue
                idx, desc = line.split("|", 1)
                if vendor_match.lower() in desc.lower():
                    log.info(f"Resolved {vendor_match} display-class key: {idx.strip()} ({desc.strip()})")
                    return reg_base + "\\" + idx.strip()
        log.warning(f"Could not find a {vendor_match} adapter under the display class — "
                    f"falling back to \\0000 (tweaks may target the wrong device)")
    except Exception as e:
        log.debug(f"GPU class-key probe failed: {e}")
    return reg_base + r"\0000"


# ═══════════════════════════════════════════════════════════════════════════
# Detection
# ═══════════════════════════════════════════════════════════════════════════
def detect_gpu() -> dict:
    """Detect GPU model, driver version, and vendor (NVIDIA / AMD / Intel)."""
    # Try nvidia-smi first — most reliable for NVIDIA
    r = run_cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"])
    if r["ok"] and r["out"]:
        parts = r["out"].split(",")
        if len(parts) >= 3:
            return {
                "vendor": "nvidia",
                "name": parts[0].strip(),
                "driver": parts[1].strip(),
                "vram_mb": int(parts[2].strip()) if parts[2].strip().isdigit() else 0,
            }

    # WMI fallback — filter out virtual adapters
    r = run_ps(
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM,VideoProcessor | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            skip_names = ["virtual", "basic", "meta", "microsoft", "remote", "vmware", "hyper-v", "parsec"]
            real_gpu = None
            # Prefer dedicated GPUs (nvidia/amd) over integrated (intel)
            for adapter in d:
                aname = (adapter.get("Name", "") or "").lower()
                if any(s in aname for s in skip_names):
                    continue
                if "nvidia" in aname or "geforce" in aname or "radeon" in aname or "amd" in aname:
                    real_gpu = adapter
                    break
            # Fall back to first non-virtual adapter
            if real_gpu is None:
                for adapter in d:
                    aname = (adapter.get("Name", "") or "").lower()
                    if not any(s in aname for s in skip_names):
                        real_gpu = adapter
                        break
            if real_gpu is None and d:
                real_gpu = d[0]
            if real_gpu:
                name = real_gpu.get("Name", "") or ""
                vendor = "unknown"
                lname = name.lower()
                if "nvidia" in lname or "geforce" in lname or "quadro" in lname:
                    vendor = "nvidia"
                elif "amd" in lname or "radeon" in lname or "ryzen" in lname:
                    vendor = "amd"
                elif "intel" in lname or "iris" in lname or "hd graphics" in lname or "uhd graphics" in lname:
                    vendor = "intel"
                return {
                    "vendor": vendor,
                    "name": name.strip(),
                    "driver": real_gpu.get("DriverVersion", "") or "",
                    "vram_mb": round((real_gpu.get("AdapterRAM", 0) or 0) / (1024 * 1024)),
                }
        except Exception:
            pass
    return {"vendor": "unknown", "name": "Unknown", "driver": "", "vram_mb": 0}


def get_nvidia_driver_info() -> dict:
    """Get current GPU driver details (named for backwards compat — works for any vendor)."""
    gpu = detect_gpu()

    if gpu["vendor"] == "nvidia":
        telemetry_svcs = ["NvTelemetryContainer", "NvContainerLocalSystem", "NvContainerNetworkService"]
        svc_status = {}
        for svc in telemetry_svcs:
            r = run_cmd(["sc", "query", svc])
            running = "RUNNING" in r.get("out", "")
            svc_status[svc] = "running" if running else "stopped/missing"
        return {**gpu, "nvidia": True, "amd": False, "telemetry_services": svc_status}

    if gpu["vendor"] == "amd":
        telemetry_svcs = ["AMD External Events Utility", "AMD Crash Defender Service", "AMDRyzenMasterDriverV22"]
        svc_status = {}
        for svc in telemetry_svcs:
            r = run_cmd(["sc", "query", svc])
            running = "RUNNING" in r.get("out", "")
            svc_status[svc] = "running" if running else "stopped/missing"
        return {**gpu, "nvidia": False, "amd": True, "telemetry_services": svc_status}

    return {**gpu, "nvidia": False, "amd": False}


# ═══════════════════════════════════════════════════════════════════════════
# NVIDIA Clean Install
# ═══════════════════════════════════════════════════════════════════════════
def prepare_clean_install() -> dict:
    """
    Prepare for a clean NVIDIA install.
    Returns status and instructions since a full DDU run requires Safe Mode.
    We automate what we can and guide the user through the rest.
    """
    log.info("Preparing NVIDIA clean install...")
    gpu = detect_gpu()
    if gpu["vendor"] != "nvidia":
        return {"ok": False, "err": "No NVIDIA GPU detected — clean install helper is NVIDIA-only", "gpu": gpu}

    steps_done = []

    nvidia_procs = [
        "nvcontainer.exe", "nvdisplay.container.exe", "nvtelemetrycontainer.exe",
        "nvidia share.exe", "nvidia web helper.exe", "nvspcaps64.exe",
        "nvbackend.exe", "nvcplui.exe",
    ]
    for proc in nvidia_procs:
        run_cmd(["taskkill", "/f", "/im", proc])
    steps_done.append("Killed NVIDIA background processes")
    log.info("  ✓ Killed NVIDIA processes")

    nvidia_svcs = [
        "NVDisplay.ContainerLocalSystem", "NvContainerLocalSystem",
        "NvContainerNetworkService", "NvTelemetryContainer",
    ]
    for svc in nvidia_svcs:
        run_cmd(["sc", "stop", svc])
    steps_done.append("Stopped NVIDIA services")
    log.info("  ✓ Stopped NVIDIA services")

    script_content = """@echo off
chcp 65001 >nul 2>nul
echo ============================================
echo   GhostShell NVIDIA Clean Install Helper
echo ============================================
echo.
echo This script will:
echo   1. Remove current NVIDIA drivers
echo   2. Clean leftover files
echo   3. You then install the fresh driver
echo.
echo Press any key to continue...
pause >nul

echo Stopping NVIDIA services...
net stop NVDisplay.ContainerLocalSystem 2>nul
net stop NvTelemetryContainer 2>nul

echo Removing NVIDIA drivers via pnputil...
pnputil /enum-drivers | findstr /i "nvidia" > "%TEMP%\\nvidia_drivers.txt"
for /f "tokens=1,2 delims=:" %%a in ('pnputil /enum-drivers ^| findstr /i "Published Name"') do (
    echo Removing %%b
    pnputil /delete-driver %%b /force 2>nul
)

echo Cleaning leftover NVIDIA files...
rd /s /q "C:\\Program Files\\NVIDIA Corporation" 2>nul
rd /s /q "C:\\ProgramData\\NVIDIA Corporation" 2>nul
rd /s /q "%LOCALAPPDATA%\\NVIDIA" 2>nul
rd /s /q "%TEMP%\\NVIDIA" 2>nul

echo.
echo ============================================
echo   Driver removal complete!
echo   Please RESTART your PC, then install
echo   the new NVIDIA driver.
echo ============================================
pause
"""
    script_path = os.path.join(os.environ.get("APPDATA", ""), "GhostShell", "nvidia_clean.bat")
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w", encoding="ascii", errors="replace") as f:
        f.write(script_content)
    steps_done.append(f"Created clean install script: {script_path}")
    log.info(f"  ✓ Created clean script at {script_path}")

    return {
        "ok": True,
        "gpu": gpu,
        "steps_done": steps_done,
        "script_path": script_path,
        "instructions": [
            "NVIDIA processes and services have been stopped.",
            "For the cleanest install, run the generated batch script as Administrator.",
            "After the script completes, restart your PC.",
            "Then install your new NVIDIA driver with 'Custom Install' > 'Clean Install' checked.",
            "Only install the Graphics Driver — skip GeForce Experience, PhysX (unless needed), and telemetry.",
        ]
    }


# ═══════════════════════════════════════════════════════════════════════════
# Helper — enable MSI mode & high interrupt priority for a display device
# ═══════════════════════════════════════════════════════════════════════════
def _apply_msi_mode(vendor_filter: str) -> list[dict]:
    """Enable Message Signaled Interrupts for the display adapter matching vendor_filter."""
    results = []
    r = run_ps(
        f"Get-PnpDevice -Class Display | Where-Object {{$_.FriendlyName -like '*{vendor_filter}*'}} | "
        "Select-Object InstanceId,FriendlyName | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            for dev in d:
                instance_id = dev.get("InstanceId", "")
                name = dev.get("FriendlyName", "")
                if not instance_id:
                    continue
                msi_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
                ok = _reg(msi_key, "MSISupported", "1")
                log.info(f"  {'✓' if ok else '✗'} {name}: MSI mode")
                results.append({"name": f"{name} MSI Mode", "ok": ok})

                affinity_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"
                _reg(affinity_key, "DevicePriority", "3")
                results.append({"name": f"{name} Interrupt Priority", "ok": True})
        except Exception as e:
            log.warning(f"  MSI mode enum failed: {e}")
            results.append({"name": "MSI Mode", "ok": False})
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NVIDIA Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_nvidia_tweaks() -> list[dict]:
    """Apply NVIDIA performance registry tweaks."""
    log.info("Applying NVIDIA driver tweaks...")
    gpu = detect_gpu()
    if gpu["vendor"] != "nvidia":
        log.warning("No NVIDIA GPU detected, skipping NVIDIA-specific tweaks")
        return [{"name": "NVIDIA Detection", "ok": False}]

    results = []

    telemetry_svcs = ["NvTelemetryContainer"]
    for svc in telemetry_svcs:
        run_cmd(["sc", "stop", svc])
        r2 = run_cmd(["sc", "config", svc, "start=", "disabled"])
        results.append({"name": f"Disable {svc}", "ok": r2["ok"]})

    nv_tasks = [
        "NvTmRep_CrashReport1_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
        "NvTmRep_CrashReport2_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
        "NvTmRep_CrashReport3_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
        "NvTmRep_CrashReport4_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
        "NvTmMon_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
        "NvTmRepOnLogon_{B2FE1952-0186-46C3-BAEC-A80AA35AC5B8}",
    ]
    for task in nv_tasks:
        run_cmd(["schtasks", "/Change", "/TN", task, "/Disable"])
    results.append({"name": "NVIDIA Telemetry Tasks", "ok": True})

    nv_class = _find_gpu_class_key("NVIDIA")
    backup_registry(nv_class, "nvidia_class")

    nv_tweaks = [
        ("Power Mode: Max Performance", nv_class, "PerfLevelSrc", "0x2222"),
        ("Power Mode: Override", nv_class, "PowerMizerEnable", "1"),
        ("Power Mode: Level", nv_class, "PowerMizerLevel", "1"),
        ("Power Mode: AC", nv_class, "PowerMizerLevelAC", "1"),
        # beta.5: this entry writes DisableDynamicPstate (force-disable adaptive
        # clock gating so the GPU holds higher clocks) — the old label
        # "Disable Driver Shader Cache Limit" was wrong (shader cache is the
        # separate ShaderDiskCacheSizeBytes write below), and a stale "Disable
        # HDCP" comment described a tweak that was never written.
        ("Disable Dynamic P-State (hold clocks)", nv_class, "DisableDynamicPstate", "1"),
    ]

    for name, key, val, data in nv_tweaks:
        ok = _reg(key, val, data)
        log.info(f"  {'✓' if ok else '✗'} {name}")
        results.append({"name": name, "ok": ok})

    # Shader cache — disable size limit (10GB cache = fewer recompiles)
    shader_key = r"HKCU\SOFTWARE\NVIDIA Corporation\Global\NVTweak"
    _reg(shader_key, "ShaderDiskCacheSizeBytes", "10737418240")
    results.append({"name": "NVIDIA Shader Cache: 10GB", "ok": True})

    # MSI mode
    results += _apply_msi_mode("NVIDIA")

    log.info("NVIDIA tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# AMD Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_amd_tweaks() -> list[dict]:
    """Apply AMD Radeon performance & telemetry tweaks."""
    log.info("Applying AMD driver tweaks...")
    gpu = detect_gpu()
    if gpu["vendor"] != "amd":
        log.warning("No AMD GPU detected, skipping AMD-specific tweaks")
        return [{"name": "AMD Detection", "ok": False}]

    results = []

    # Disable AMD User Experience Program (telemetry)
    amd_ueip_keys = [
        r"HKLM\SOFTWARE\AMD\CN",
        r"HKCU\SOFTWARE\AMD\CN",
    ]
    for key in amd_ueip_keys:
        _reg(key, "PhysicalAutoUpdateStatus", "0")
        _reg(key, "UEPNotificationStatus", "0")
        _reg(key, "EnableULPS", "0")  # ULPS off — can cause black-screen stutters
    results.append({"name": "AMD Telemetry (UEIP) disabled", "ok": True})
    results.append({"name": "AMD ULPS disabled", "ok": True})

    # Tasks AMD installs
    amd_tasks = [
        "StartDVR",
        "StartCN",
        r"\AMD\RadeonSoftwareStartupTask",
    ]
    for task in amd_tasks:
        run_cmd(["schtasks", "/Change", "/TN", task, "/Disable"])
    results.append({"name": "AMD Scheduled Tasks disabled", "ok": True})

    # AMD display class — enable Hardware Scheduling + set power to max
    amd_class_candidates = [
        r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000",
        r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0001",
    ]
    for amd_class in amd_class_candidates:
        # Only try keys that exist
        q = run_cmd(["reg", "query", amd_class])
        if not q["ok"]:
            continue
        backup_registry(amd_class, "amd_class")
        # EnableUlps = 0: force GPU to full power (prevents "ultra low power state")
        _reg(amd_class, "EnableUlps", "0")
        _reg(amd_class, "EnableUlps_NA", "0")
        # PP_* = PowerPlay table overrides (performance bias)
        _reg(amd_class, "PP_ThermalAutoThrottlingEnable", "0")
        _reg(amd_class, "DisableFullScreenOpt", "1")
        # DisableBlockWrite = 0, KMD_DeLagEnabled = 1 (Radeon Anti-Lag enabled globally)
        _reg(amd_class, "KMD_DeLagEnabled", "1")
        # AMD Chill off (global Chill is on by default; off = max FPS)
        _reg(amd_class, "KMD_FRTEnabled", "0")
        results.append({"name": f"AMD PowerPlay tuned: {amd_class[-4:]}", "ok": True})

    # Stop AMD telemetry services
    amd_svcs_to_stop = ["AMD Crash Defender Service", "AMD External Events Utility"]
    for svc in amd_svcs_to_stop:
        run_cmd(["sc", "stop", svc])
    results.append({"name": "AMD Crash Defender stopped", "ok": True})

    # MSI mode for AMD GPU
    results += _apply_msi_mode("AMD")
    results += _apply_msi_mode("Radeon")

    log.info("AMD tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Intel Tweaks (basic)
# ═══════════════════════════════════════════════════════════════════════════
def apply_intel_tweaks() -> list[dict]:
    """Intel iGPU — minimal tweaks (basic MSI mode + performance bias)."""
    log.info("Applying Intel GPU tweaks...")
    gpu = detect_gpu()
    if gpu["vendor"] != "intel":
        return [{"name": "Intel Detection", "ok": False}]

    results = _apply_msi_mode("Intel")

    # Intel Graphics Command Center telemetry
    intel_tasks = [
        r"\IntelSURQC-Upgrade-86621605-2a0b-4128-8ffc-15514c247132-Logon",
    ]
    for task in intel_tasks:
        run_cmd(["schtasks", "/Change", "/TN", task, "/Disable"])

    log.info("Intel GPU tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Universal entry point
# ═══════════════════════════════════════════════════════════════════════════
def apply_all_gpu_tweaks() -> dict:
    """Detect GPU vendor and apply the matching tweaks (NVIDIA / AMD / Intel)."""
    log.info("═══ STARTING GPU OPTIMIZATION ═══")
    gpu = detect_gpu()
    log.info(f"  Detected: {gpu['vendor']} / {gpu['name']}")

    tweaks = []
    if gpu["vendor"] == "nvidia":
        tweaks = apply_nvidia_tweaks()
    elif gpu["vendor"] == "amd":
        tweaks = apply_amd_tweaks()
    elif gpu["vendor"] == "intel":
        tweaks = apply_intel_tweaks()
    else:
        tweaks = [{"name": "Unknown GPU vendor", "ok": False}]

    log.info("═══ GPU OPTIMIZATION COMPLETE ═══")
    return {"gpu": gpu, "tweaks": tweaks}
