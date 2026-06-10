"""System information detection for the dashboard."""
import json
import platform
import os
from core.utils import run_ps, run_cmd, get_logger, detect_drive_type

log = get_logger("sysinfo")


def get_system_info() -> dict:
    """Gather comprehensive system information."""
    info = {
        "os": _get_os_info(),
        "cpu": _get_cpu_info(),
        "gpu": _get_gpu_info(),
        "ram": _get_ram_info(),
        "storage": _get_storage_info(),
        "network": _get_network_info(),
    }
    log.info("System info collected")
    return info


def _get_os_info() -> dict:
    r = run_ps(
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            return {
                "name": d.get("Caption", "Windows"),
                "version": d.get("Version", ""),
                "build": d.get("BuildNumber", ""),
                "arch": d.get("OSArchitecture", ""),
            }
        except Exception:
            pass
    return {
        "name": platform.system() + " " + platform.release(),
        "version": platform.version(),
        "build": "",
        "arch": platform.machine(),
    }


def _get_cpu_info() -> dict:
    r = run_ps(
        "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if isinstance(d, list):
                d = d[0]
            return {
                "name": d.get("Name", "Unknown").strip(),
                "cores": d.get("NumberOfCores", 0),
                "threads": d.get("NumberOfLogicalProcessors", 0),
                "max_clock": d.get("MaxClockSpeed", 0),
                "cur_clock": d.get("CurrentClockSpeed", 0),
            }
        except Exception:
            pass
    return {"name": platform.processor() or "Unknown", "cores": os.cpu_count() or 0,
            "threads": os.cpu_count() or 0, "max_clock": 0, "cur_clock": 0}


def _get_gpu_info() -> dict:
    # Try nvidia-smi first — most reliable for NVIDIA
    r2 = run_cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"])
    if r2["ok"] and r2["out"]:
        parts = r2["out"].split(",")
        if len(parts) >= 3:
            return {
                "name": parts[0].strip(),
                "driver": parts[1].strip(),
                "vram_gb": round(int(parts[2].strip()) / 1024, 1),
                "status": "OK",
            }

    # Fallback: WMI — but filter out virtual/basic adapters
    r = run_ps(
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM,Status | ConvertTo-Json"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            # Filter: prefer real GPU over virtual/basic display
            skip_names = ["virtual", "basic", "meta", "microsoft", "remote", "vmware", "hyper-v", "parsec"]
            real_gpu = None
            for adapter in d:
                name = (adapter.get("Name", "") or "").lower()
                if not any(s in name for s in skip_names):
                    real_gpu = adapter
                    break
            if real_gpu is None and d:
                real_gpu = d[0]  # fallback to first if all are virtual
            if real_gpu:
                vram_bytes = real_gpu.get("AdapterRAM", 0) or 0
                vram_gb = round(vram_bytes / (1024 ** 3), 1) if vram_bytes else 0
                return {
                    "name": (real_gpu.get("Name", "Unknown") or "Unknown").strip(),
                    "driver": real_gpu.get("DriverVersion", "") or "",
                    "vram_gb": vram_gb,
                    "status": real_gpu.get("Status", "") or "",
                }
        except Exception:
            pass
    return {"name": "Unknown", "driver": "", "vram_gb": 0, "status": ""}


def _gpu_vendor_from(name: str, pnp_id: str = "") -> str:
    """Map a video-controller name / PNPDeviceID to a vendor string.
    PNP IDs carry the PCI vendor ID which is unambiguous:
      VEN_10DE = NVIDIA, VEN_1002 = AMD, VEN_8086 = Intel."""
    pid = (pnp_id or "").upper()
    if "VEN_10DE" in pid: return "NVIDIA"
    if "VEN_1002" in pid: return "AMD"
    if "VEN_8086" in pid: return "Intel"
    n = (name or "").lower()
    if any(m in n for m in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla", "titan")):
        return "NVIDIA"
    if any(m in n for m in ("amd", "radeon", "vega", "ryzen", "firepro")):
        return "AMD"
    if any(m in n for m in ("intel", "iris", "uhd", "hd graphics", "arc")):
        return "Intel"
    return "Unknown"


def _detect_gpus() -> list:
    """Enumerate EVERY video controller on the system (a gaming laptop
    typically has two: the Intel/AMD iGPU + an NVIDIA/AMD dGPU).  Returns
    a list of {name, vendor, vram_gb, driver}.  Used by core.capabilities
    to pick the highest-tier GPU + decide which features to light up.

    Skips the obvious virtual / remote-display adapters so a Parsec or
    RDP session doesn't get mistaken for the real GPU."""
    out: list = []
    r = run_ps(
        "Get-CimInstance Win32_VideoController "
        "| Select-Object Name,DriverVersion,AdapterRAM,PNPDeviceID,Status "
        "| ConvertTo-Json -Compress"
    )
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            skip = ("virtual", "basic display", "basic render", "meta",
                    "remote", "vmware", "hyper-v", "parsec", "citrix",
                    "ddm", "aspeed", "matrox", "idd")
            for adapter in d:
                name = (adapter.get("Name", "") or "").strip()
                if not name:
                    continue
                if any(s in name.lower() for s in skip):
                    continue
                vram_bytes = adapter.get("AdapterRAM", 0) or 0
                # AdapterRAM is a signed 32-bit field in WMI — it wraps
                # negative / caps at ~4 GB for cards with more VRAM.
                # Treat negatives + the 4 GB cap as "unknown, use nvidia-smi".
                vram_gb = 0
                if vram_bytes and vram_bytes > 0:
                    vram_gb = round(vram_bytes / (1024 ** 3), 1)
                out.append({
                    "name":    name,
                    "vendor":  _gpu_vendor_from(name, adapter.get("PNPDeviceID", "")),
                    "vram_gb": vram_gb,
                    "driver":  adapter.get("DriverVersion", "") or "",
                })
        except Exception:
            pass
    # If nvidia-smi reports a card WMI missed (rare, but happens when the
    # NVIDIA adapter is in a weird power state), make sure it's listed.
    if not any(g["vendor"] == "NVIDIA" for g in out):
        nv = run_cmd(["nvidia-smi", "--query-gpu=name,memory.total",
                      "--format=csv,noheader,nounits"])
        if nv["ok"] and nv["out"]:
            parts = nv["out"].split(",")
            if parts and parts[0].strip():
                vram = 0
                if len(parts) >= 2:
                    try: vram = round(int(parts[1].strip()) / 1024, 1)
                    except ValueError: pass
                out.append({"name": parts[0].strip(), "vendor": "NVIDIA",
                            "vram_gb": vram, "driver": ""})
    return out


def _get_ram_info() -> dict:
    r = run_ps(
        "Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed | ConvertTo-Json"
    )
    total = 0
    speed = 0
    sticks = 0
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            sticks = len(d)
            for stick in d:
                total += stick.get("Capacity", 0)
                s = stick.get("ConfiguredClockSpeed", 0) or stick.get("Speed", 0)
                if s > speed:
                    speed = s
        except Exception:
            pass
    total_gb = round(total / (1024 ** 3), 1) if total else 0

    # Current usage
    r2 = run_ps(
        "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json"
    )
    used_pct = 0
    if r2["ok"] and r2["out"]:
        try:
            d2 = json.loads(r2["out"])
            t = d2.get("TotalVisibleMemorySize", 1)
            f = d2.get("FreePhysicalMemory", 0)
            used_pct = round((1 - f / t) * 100) if t else 0
        except Exception:
            pass

    return {"total_gb": total_gb, "speed_mhz": speed, "sticks": sticks, "used_pct": used_pct}


def _get_storage_info() -> dict:
    drive_type = detect_drive_type()
    r = run_ps(
        "Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | Select-Object DeviceID,Size,FreeSpace | ConvertTo-Json"
    )
    drives = []
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            for drv in d:
                total = drv.get("Size", 0) or 0
                free = drv.get("FreeSpace", 0) or 0
                drives.append({
                    "letter": drv.get("DeviceID", "?"),
                    "total_gb": round(total / (1024 ** 3), 1),
                    "free_gb": round(free / (1024 ** 3), 1),
                    "used_pct": round((1 - free / total) * 100) if total else 0,
                })
        except Exception:
            pass
    return {"type": drive_type, "drives": drives}


def _get_network_info() -> dict:
    # Current DNS
    r = run_ps(
        "Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object {$_.ServerAddresses} | Select-Object InterfaceAlias,ServerAddresses | ConvertTo-Json"
    )
    dns_servers = []
    adapter = ""
    if r["ok"] and r["out"]:
        try:
            d = json.loads(r["out"])
            if not isinstance(d, list):
                d = [d]
            for item in d:
                addrs = item.get("ServerAddresses", [])
                if addrs:
                    adapter = item.get("InterfaceAlias", "")
                    dns_servers = addrs
                    break
        except Exception:
            pass
    return {"adapter": adapter, "dns": dns_servers}
