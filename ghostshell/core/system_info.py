import platform
import subprocess
import re
import json
import shutil
import socket
import winreg
from datetime import datetime
from typing import Any, Dict, List, Optional

import psutil
import wmi

try:
    import requests
except Exception:  # pragma: no cover - requests may be unavailable
    requests = None  # type: ignore

try:
    from core.utils import get_logger, human_bytes
except Exception:  # pragma: no cover - fallback if core utils unavailable
    import logging

    def get_logger(name: str) -> logging.Logger:
        logging.basicConfig(level=logging.INFO)
        return logging.getLogger(name)

    def human_bytes(num: float, precision: int = 2) -> str:
        if num is None:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        idx = 0
        value = float(num)
        while value >= 1024 and idx < len(units) - 1:
            value /= 1024
            idx += 1
        return f"{value:.{precision}f} {units[idx]}"

LOGGER = get_logger(__name__)

_DEFAULT_WMI: Optional[wmi.WMI] = None
_STORAGE_WMI: Optional[wmi.WMI] = None
_DRIVE_LETTER_MAP: Dict[str, int] = {}
_DISK_INDEX_INFO: Dict[int, Dict[str, Any]] = {}
_FORM_FACTOR_MAP = {
    0: "Unknown",
    1: "Other",
    2: "SIP",
    3: "DIP",
    4: "ZIP",
    5: "SOJ",
    6: "Proprietary",
    7: "SIMM",
    8: "DIMM",
    9: "TSOP",
    10: "PGA",
    11: "RIMM",
    12: "SODIMM",
    13: "SRIMM",
    14: "SMD",
    15: "SSMP",
    16: "QFP",
    17: "TQFP",
    18: "SOIC",
    19: "LCC",
    20: "PLCC",
    21: "BGA",
    22: "FPBGA",
    23: "LGA",
    24: "FB-DIMM",
}


def _get_wmi(namespace: Optional[str] = None) -> Optional[wmi.WMI]:
    global _DEFAULT_WMI, _STORAGE_WMI
    try:
        if namespace:
            if _STORAGE_WMI is None:
                _STORAGE_WMI = wmi.WMI(namespace=namespace)
            return _STORAGE_WMI
        if _DEFAULT_WMI is None:
            _DEFAULT_WMI = wmi.WMI()
        return _DEFAULT_WMI
    except Exception as exc:
        LOGGER.debug("WMI init error: %s", exc)
        return None


def get_os_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "build": platform.version(),
        "edition": None,
        "install_date": None,
    }
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            edition, _ = winreg.QueryValueEx(k, "EditionID")
            current_build, _ = winreg.QueryValueEx(k, "CurrentBuildNumber")
            install_date, _ = winreg.QueryValueEx(k, "InstallDate")
            info["edition"] = str(edition)
            info["build"] = str(current_build)
            try:
                info["install_date"] = datetime.utcfromtimestamp(int(install_date)).isoformat() + "Z"
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        LOGGER.debug("OS info registry error: %s", exc)
    return info


def get_cpu_info() -> Dict[str, Any]:
    cpu = {
        "model": platform.processor() or None,
        "physical_cores": psutil.cpu_count(logical=False) or 0,
        "logical_cores": psutil.cpu_count(logical=True) or 0,
        "base_clock_mhz": None,
        "current_clock_mhz": None,
        "utilization_percent": psutil.cpu_percent(interval=0.5),
    }
    try:
        w = _get_wmi()
        if w:
            for p in w.Win32_Processor():
                if not cpu["model"]:
                    cpu["model"] = p.Name.strip()
                try:
                    cpu["base_clock_mhz"] = int(p.MaxClockSpeed)
                except Exception:
                    pass
                break
        freqs = psutil.cpu_freq()
        if freqs:
            cpu["current_clock_mhz"] = round(freqs.current, 1)
            if not cpu["base_clock_mhz"] and freqs.max:
                cpu["base_clock_mhz"] = int(freqs.max)
    except Exception as exc:
        LOGGER.debug("CPU info error: %s", exc)
    return cpu


def _parse_nv_smi() -> Optional[Dict[str, Any]]:
    try:
        proc = subprocess.run(["nvidia-smi", "--query-gpu=driver_version,name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=2)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.split(",")]
            if len(parts) >= 3:
                return {
                    "driver_version": parts[0],
                    "model": parts[1],
                    "vram_mb": int(re.sub(r"[^0-9]", "", parts[2]) or 0),
                }
    except Exception:
        return None
    return None


def get_gpu_info() -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []
    try:
        w = _get_wmi()
        if w:
            for vc in w.Win32_VideoController():
                item = {
                    "model": getattr(vc, "Name", None),
                    "driver_version": getattr(vc, "DriverVersion", None),
                    "vram_mb": None,
                }
                try:
                    vram = int(getattr(vc, "AdapterRAM", 0))
                    item["vram_mb"] = int(vram / (1024 * 1024)) if vram else None
                except Exception:
                    pass
                gpus.append(item)
    except Exception as exc:
        LOGGER.debug("GPU WMI error: %s", exc)

    nv = _parse_nv_smi()
    if nv:
        # Try to merge into first NVIDIA entry
        for gpu in gpus:
            if gpu.get("model") and "nvidia" in gpu["model"].lower():
                gpu.update({k: v for k, v in nv.items() if v})
                break
        else:
            gpus.append(nv)
    return gpus


def get_ram_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2),
        "speed_mhz": None,
        "slots_used": None,
        "form_factor": None,
    }
    try:
        w = _get_wmi()
        if w:
            speeds = []
            form_factors = set()
            count = 0
            for mem in w.Win32_PhysicalMemory():
                count += 1
                try:
                    if getattr(mem, "Speed", None):
                        speeds.append(int(mem.Speed))
                    ff = int(getattr(mem, "FormFactor", 0))
                    if ff in _FORM_FACTOR_MAP:
                        form_factors.add(_FORM_FACTOR_MAP[ff])
                except Exception:
                    pass
            info["slots_used"] = count
            info["speed_mhz"] = int(sum(speeds) / len(speeds)) if speeds else None
            info["form_factor"] = ", ".join(sorted(form_factors)) if form_factors else None
    except Exception as exc:
        LOGGER.debug("RAM WMI error: %s", exc)
    return info


def _detect_drive_type(drive_letter: str) -> str:
    # Attempt MSFT_PhysicalDisk via root\Microsoft\Windows\Storage
    try:
        swmi = _get_wmi(namespace="root\Microsoft\Windows\Storage")
        if swmi:
            for d in swmi.MSFT_PhysicalDisk():
                # MediaType: 0=Unspecified, 1=HDD, 3=SSD, 4=SCM
                mt = int(getattr(d, "MediaType", 0) or 0)
                if mt == 1:
                    return "HDD"
                elif mt in (3, 4):
                    return "SSD"
    except Exception:
        pass
    # Fallback: Win32_DiskDrive model heuristics
    try:
        w = _get_wmi()
        if w:
            for dd in w.Win32_DiskDrive():
                model = (getattr(dd, "Model", "") or "").upper()
                if "NVME" in model:
                    return "NVMe"
                if any(k in model for k in ("SSD", "SOLID STATE")):
                    return "SSD"
                if any(k in model for k in ("HDD", "HARD DISK")):
                    return "HDD"
    except Exception:
        pass
    return "Unknown"


def get_storage_info() -> List[Dict[str, Any]]:
    disks: List[Dict[str, Any]] = []
    try:
        for part in psutil.disk_partitions(all=False):
            if not part.mountpoint or not re.match(r"^[A-Z]:\\\\?$", part.mountpoint + ("\\" if not part.mountpoint.endswith("\\") else "")):
                # Expect drive letters only
                pass
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except PermissionError:
                continue
            dtype = _detect_drive_type(part.device[:2])
            disks.append({
                "name": part.device,
                "type": dtype,
                "filesystem": part.fstype,
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
            })
    except Exception as exc:
        LOGGER.debug("Storage info error: %s", exc)
    return disks


def get_network_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"adapters": [], "dns": [], "public_ip": None}
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for name, addr_list in addrs.items():
            st = stats.get(name)
            if st is None:
                continue
            ipv4 = [a.address for a in addr_list if getattr(a, "family", None).name == "AF_INET"] if addr_list else []
            ipv6 = [a.address for a in addr_list if getattr(a, "family", None).name == "AF_INET6"] if addr_list else []
            info["adapters"].append({
                "name": name,
                "up": bool(getattr(st, "isup", False)),
                "speed_mbps": getattr(st, "speed", None),
                "mtu": getattr(st, "mtu", None),
                "ipv4": ipv4,
                "ipv6": ipv6,
            })
        # DNS via ipconfig /all parse
        try:
            proc = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True, timeout=4)
            dns = []
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if "DNS Servers" in line:
                        parts = line.split(":", 1)
                        if len(parts) == 2:
                            dns_ip = parts[1].strip()
                            if dns_ip:
                                dns.append(dns_ip)
                    elif line.startswith("\t") and re.match(r"\s*\d+\.\d+\.\d+\.\d+", line):
                        dns.append(line.strip())
                info["dns"] = dns
        except Exception:
            pass
        # Public IP best-effort
        if requests:
            try:
                r = requests.get("https://api.ipify.org", params={"format": "json"}, timeout=2)
                if r.ok:
                    info["public_ip"] = r.json().get("ip")
            except Exception:
                pass
    except Exception as exc:
        LOGGER.debug("Network info error: %s", exc)
    return info


def get_status_badges() -> Dict[str, bool]:
    status_keys = ["debloat", "optimize", "privacy", "network", "gpu", "hwid", "vault"]
    statuses = {key: False for key in status_keys}
    path = r"Software\GhostShell\Status"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            for state in status_keys:
                try:
                    value, _ = winreg.QueryValueEx(key, state)
                    if isinstance(value, str):
                        statuses[state] = value.strip().lower() in {"1", "true", "yes", "enabled"}
                    elif isinstance(value, (int, float)):
                        statuses[state] = bool(value)
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    except OSError as exc:
        LOGGER.debug("Status badge registry read error: %s", exc)
    return statuses


def get_dashboard() -> Dict[str, Any]:
    dashboard: Dict[str, Any] = {
        "os": {},
        "cpu": {},
        "gpu": [],
        "ram": {},
        "storage": [],
        "network": {},
        "status": {},
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    collectors = [
        ("os", get_os_info),
        ("cpu", get_cpu_info),
        ("gpu", get_gpu_info),
        ("ram", get_ram_info),
        ("storage", get_storage_info),
        ("network", get_network_info),
        ("status", get_status_badges),
    ]
    for key, func in collectors:
        try:
            dashboard[key] = func()
        except Exception as exc:
            LOGGER.error("Collector %s failed: %s", key, exc)
            dashboard[key] = {} if key != "gpu" and key != "storage" else []
    try:
        json.dumps(dashboard)
    except TypeError as exc:
        LOGGER.error("Dashboard serialization error: %s", exc)
    return dashboard
