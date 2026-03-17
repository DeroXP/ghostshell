import os
import re
import zipfile
import tempfile
import shutil
import json
from typing import Tuple, Dict, Optional, List, Any
from pathlib import Path

import requests

from core.utils import run_powershell, reg_set, service_set, log_event


def _execute_powershell(command: str) -> Tuple[int, str, str]:
    try:
        result = run_powershell(command)
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)

    if isinstance(result, tuple):
        rc = result[0] if len(result) > 0 and isinstance(result[0], int) else 0
        stdout = result[1] if len(result) > 1 else ""
        stderr = result[2] if len(result) > 2 else ""
    else:
        rc = 0
        stdout = str(result) if result is not None else ""
        stderr = ""
    return rc, stdout, stderr


def detect_gpu() -> Dict[str, str]:
    action = "detect_gpu"
    try:
        cmd = "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader"
        rc, stdout, stderr = _execute_powershell(cmd)
        if rc == 0 and stdout.strip():
            first_line = stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 2:
                info = {
                    "vendor": "NVIDIA",
                    "model": parts[0],
                    "driver_version": parts[1],
                }
                log_event("gpu", action, "success", f"Detected via nvidia-smi: {info}")
                return info
        if stderr:
            log_event("gpu", action, "info", f"nvidia-smi not available: {stderr}")
    except Exception as exc:
        log_event("gpu", action, "error", f"nvidia-smi detection failed: {exc}")

    try:
        ps_cmd = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterCompatibility,DriverVersion | ConvertTo-Json"
        )
        rc, stdout, stderr = _execute_powershell(ps_cmd)
        if rc == 0 and stdout:
            data = json.loads(stdout)
            if isinstance(data, list) and data:
                first = data[0]
            elif isinstance(data, dict):
                first = data
            else:
                first = {}
            vendor = str(first.get("AdapterCompatibility") or "")
            model = str(first.get("Name") or "")
            driver = str(first.get("DriverVersion") or "")
            info = {"vendor": vendor, "model": model, "driver_version": driver}
            log_event("gpu", action, "success", f"Detected via WMI: {info}")
            return info
    except Exception as exc:
        log_event("gpu", action, "error", f"WMI detection failed: {exc}")

    return {"vendor": "Unknown", "model": "Unknown", "driver_version": ""}


def download_ddu(dest_dir: str) -> Tuple[bool, str, Optional[str]]:
    action = "download_ddu"
    mirrors = [
        "https://www.wagnardsoft.com/Downloads/DDU.zip",
        "https://www.wagnardsoft.com/DDU-Release.zip",
    ]
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out_path = str(Path(dest_dir) / "DDU.zip")

    for url in mirrors:
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and r.content:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                log_event("gpu", action, "success", f"Downloaded from {url}")
                return True, "Downloaded DDU", out_path
        except Exception as exc:
            log_event("gpu", action, "info", f"Mirror failed {url}: {exc}")
    msg = "Failed to download DDU from mirrors"
    log_event("gpu", action, "error", msg)
    return False, msg, None


def download_latest_driver(model_hint: str, dest_dir: str) -> Tuple[bool, str, Optional[str]]:
    action = "download_driver"
    # Use NVIDIA AjaxDriverService to query latest GRD
    params = {
        "func": "DRIVER_SEARCH",
        "psid": 120,  # GeForce
        "pfid": 929,  # broad Ampere/Ada fallback
        "osID": 57,   # Windows 11 64-bit
        "osC": 57,
        "languageCode": 1033,
        "isWHQL": 1,
        "dch": 1,
        "page": 1,
        "sort1": "0",
        "numberOfResults": 1,
    }
    try:
        r = requests.get(
            "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php",
            params=params,
            timeout=30,
        )
        if r.status_code == 200 and r.text:
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else json.loads(r.text)
            # The response format is odd; try to locate URL heuristically
            text = json.dumps(data)
            m = re.search(r"https?:\\/\\/[^\\\"]+NVIDIA[^\\\"]+\\.exe", text)
            if m:
                url = m.group(0).encode("utf-8").decode("unicode_escape")
                Path(dest_dir).mkdir(parents=True, exist_ok=True)
                out_path = str(Path(dest_dir) / "NVIDIA_Driver.exe")
                with requests.get(url, stream=True, timeout=120) as resp:
                    resp.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                f.write(chunk)
                log_event("gpu", action, "success", f"Downloaded driver from {url}")
                return True, "Downloaded latest driver", out_path
    except Exception as exc:
        log_event("gpu", action, "info", f"Driver lookup failed: {exc}")

    msg = "Manual download required. Visit https://www.nvidia.com/Download/index.aspx"
    log_event("gpu", action, "warning", msg)
    return False, msg, None


def create_safe_mode_script(driver_installer_path: str, ddu_zip_path: str, work_dir: str) -> Tuple[bool, str, str]:
    action = "create_safe_mode_script"
    try:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        cleanup = work / "safe_mode_cleanup.ps1"
        install = work / "normal_mode_install.ps1"
        readme = work / "README.txt"

        cleanup.write_text(
            """
$ErrorActionPreference = 'SilentlyContinue'
$work = Split-Path -Parent $MyInvocation.MyCommand.Path
$dduZip = Join-Path $work 'DDU.zip'
$dduDir = Join-Path $work 'DDU'
if (Test-Path $dduDir) { Remove-Item -Recurse -Force $dduDir }
New-Item -ItemType Directory -Path $dduDir | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($dduZip, $dduDir)
$exe = Get-ChildItem -Path $dduDir -Recurse -Include *DDU*.exe | Select-Object -First 1
if ($exe) {
  & $exe.FullName /GPU=NVIDIA /silent
}
Write-Host 'DDU cleanup complete. Rebooting to normal mode...'
shutdown /r /t 3
""".strip(),
            encoding="utf-8",
        )

        install.write_text(
            f"""
$ErrorActionPreference = 'SilentlyContinue'
$driver = "{driver_installer_path.replace('`', '``')}"
if (Test-Path $driver) {{
  Start-Process -FilePath $driver -ArgumentList '-s -noreboot' -Wait
  Write-Host 'NVIDIA driver installed silently.'
}} else {{
  Write-Host 'Driver installer not found: ' $driver
}}
""".strip(),
            encoding="utf-8",
        )

        readme.write_text(
            f"""
NVIDIA CLEAN INSTALL GUIDE (GhostShell)

1) Boot into Windows Safe Mode (Shift+Restart → Troubleshoot → Advanced options → Startup Settings → Restart → 4)
2) In Safe Mode, run PowerShell as Administrator and execute:
   powershell -ExecutionPolicy Bypass -File "{cleanup}"
   This will extract DDU and remove NVIDIA/AMD/Intel GPU drivers, then reboot to normal mode.
3) After reboot to normal Windows, run:
   powershell -ExecutionPolicy Bypass -File "{install}"
   This installs the downloaded NVIDIA Game Ready driver silently (no GeForce Experience).

If any step fails, download the latest driver manually from:
https://www.nvidia.com/Download/index.aspx

Files prepared in this folder:
- {Path(ddu_zip_path)} (DDU archive)
- {Path(driver_installer_path)} (NVIDIA driver installer)
- safe_mode_cleanup.ps1
- normal_mode_install.ps1
""".strip(),
            encoding="utf-8",
        )
        msg = f"Scripts created at {work}"
        log_event("gpu", action, "success", msg)
        return True, msg, str(readme)
    except Exception as exc:
        msg = f"Failed to create scripts: {exc}"
        log_event("gpu", action, "error", msg)
        return False, msg, ""


def apply_registry_tweaks() -> Tuple[bool, str]:
    action = "apply_registry_tweaks"
    steps: List[str] = []
    errors: List[str] = []

    try:
        service_set("NvTelemetryContainer", start_type="disabled", action="stop")
        steps.append("Disabled NvTelemetryContainer service")
    except Exception as exc:
        errors.append(f"Service tweak failed: {exc}")

    rc, stdout, stderr = _execute_powershell("nvidia-smi -pm 1")
    if rc == 0:
        steps.append("Enabled persistent power management via nvidia-smi")
    else:
        errors.append(f"nvidia-smi power mgmt failed: {stderr or stdout}")

    try:
        reg_set("HKLM", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2, "REG_DWORD")
        steps.append("Set HwSchMode=2 (Hardware Accelerated GPU Scheduling)")
    except Exception as exc:
        errors.append(f"Registry tweak failed: {exc}")

    msg = "; ".join(steps + errors)
    log_event("gpu", action, "success" if not errors else "warning", msg)
    return (not errors), msg
