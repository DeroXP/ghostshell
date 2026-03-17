import math
import os
from typing import List, Tuple, Dict, Callable

from core.utils import reg_set, run_powershell, service_set, log_event

MODULE_NAME = os.path.splitext(os.path.basename(__file__))[0]
REG_DWORD = "REG_DWORD"
TweakExecutor = Callable[[], Tuple[bool, str]]


_TWEAK_CATALOG: List[Dict[str, str]] = [
    {
        "key": "powerplan_ultimate",
        "title": "Ultimate Performance Power Plan",
        "description": "Activates Microsoft's Ultimate Performance power plan or creates a custom high-performance plan.",
        "category": "Power",
    },
    {
        "key": "cpu_core_parking_off",
        "title": "Disable CPU Core Parking",
        "description": "Force all CPU cores to stay unparked for maximum responsiveness.",
        "category": "CPU",
    },
    {
        "key": "cpu_priority_foreground",
        "title": "Foreground Priority Boost",
        "description": "Bias CPU scheduling toward foreground apps for better UI responsiveness.",
        "category": "CPU",
    },
    {
        "key": "speculative_mitigations_off",
        "title": "Disable Speculative Execution Mitigations",
        "description": "Disables spectre/meltdown mitigations for lower latency (reduces security).",
        "category": "CPU",
    },
    {
        "key": "mem_disable_compression",
        "title": "Disable Memory Compression",
        "description": "Stops the Windows memory compression store to save CPU cycles.",
        "category": "Memory",
    },
    {
        "key": "mem_large_system_cache",
        "title": "Disable Large System Cache",
        "description": "Ensures memory is available for applications instead of the system cache.",
        "category": "Memory",
    },
    {
        "key": "mem_disable_paging_exec",
        "title": "Disable Paging Executive",
        "description": "Prevents paging of kernel-mode drivers for slightly faster access.",
        "category": "Memory",
    },
    {
        "key": "mem_ndu_disable",
        "title": "Disable NDU Service",
        "description": "Disables network data usage monitoring to save overhead.",
        "category": "Memory",
    },
    {
        "key": "storage_fsutil",
        "title": "FS Util Optimizations",
        "description": "Disable last access, 8.3 filenames, and bump NTFS memory usage.",
        "category": "Storage",
    },
    {
        "key": "storage_disable_indexing",
        "title": "Disable Windows Search Indexing",
        "description": "Stops and disables the Windows Search indexer.",
        "category": "Storage",
    },
    {
        "key": "gaming_dvr_off",
        "title": "Disable Game DVR and Bar",
        "description": "Shuts off Game DVR and Game Bar for best performance.",
        "category": "Gaming",
    },
    {
        "key": "gaming_fullscreen_opt_off",
        "title": "Disable Fullscreen Optimizations",
        "description": "Forces legacy fullscreen behavior globally.",
        "category": "Gaming",
    },
    {
        "key": "gpu_hardware_sched_on",
        "title": "Enable Hardware-Accelerated GPU Scheduling",
        "description": "Turns on Windows GPU scheduling (WDDM 2.7+).",
        "category": "GPU",
    },
    {
        "key": "timer_resolution",
        "title": "High Precision Timer",
        "description": "Enables platform tick and disables dynamic tick (may require reboot).",
        "category": "System",
    },
    {
        "key": "visual_best_performance",
        "title": "Best Performance Visuals",
        "description": "Disables animations, transparency, and visual effects.",
        "category": "Visual",
    },
]


# --- Tweak executors ---

def _tweak_powerplan_ultimate() -> Tuple[bool, str]:
    # Duplicate and set Ultimate Performance plan
    dup = "powercfg -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61"
    code, out, err = run_powershell(f"cmd /c {dup}")
    msg = (err or out or "").strip()
    if code == 0:
        # Extract GUID from output if present
        guid = None
        for token in msg.split():
            if token.count('-') == 4 and len(token) >= 36:
                guid = token.strip('{}')
                break
        if guid:
            code2, out2, err2 = run_powershell(f"cmd /c powercfg -setactive {guid}")
            return (code2 == 0), (err2 or out2 or msg)
    # Fallback: set high performance plan
    code3, out3, err3 = run_powershell("cmd /c powercfg -setactive SCHEME_MIN")
    return (code3 == 0), (err3 or out3 or msg)


def _tweak_cpu_core_parking_off() -> Tuple[bool, str]:
    try:
        base = r"SYSTEM\\CurrentControlSet\\Control\\Power\\PowerSettings"
        for guid in (
            r"0cc5b647-c1df-4637-891a-dec35c318583",  # Processor performance core parking min cores
            r"ea062031-0e34-4ff1-9b6d-eb1059334028",  # Processor performance core parking max cores
        ):
            path = f"{base}\\{guid}\\{guid}"
            reg_set("HKLM", path, "ValueMin", 0, REG_DWORD)
            reg_set("HKLM", path, "ValueMax", 0, REG_DWORD)
        return True, "Core parking disabled"
    except Exception as exc:
        return False, str(exc)


def _tweak_cpu_priority_foreground() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 56, REG_DWORD)
        return True, "Foreground priority set"
    except Exception as exc:
        return False, str(exc)


def _tweak_speculative_mitigations_off() -> Tuple[bool, str]:
    try:
        base = r"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management"
        reg_set("HKLM", base, "FeatureSettingsOverride", 3, REG_DWORD)
        reg_set("HKLM", base, "FeatureSettingsOverrideMask", 3, REG_DWORD)
        return True, "Speculative mitigations disabled (reduced security)"
    except Exception as exc:
        return False, str(exc)


def _tweak_mem_disable_compression() -> Tuple[bool, str]:
    code, out, err = run_powershell("Disable-MMAgent -MemoryCompression")
    return (code == 0), (err or out or "").strip()


def _tweak_mem_large_system_cache() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management", "LargeSystemCache", 0, REG_DWORD)
        return True, "LargeSystemCache=0"
    except Exception as exc:
        return False, str(exc)


def _tweak_mem_disable_paging_exec() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management", "DisablePagingExecutive", 1, REG_DWORD)
        return True, "DisablePagingExecutive=1"
    except Exception as exc:
        return False, str(exc)


def _tweak_mem_ndu_disable() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SYSTEM\\ControlSet001\\Services\\Ndu", "Start", 4, REG_DWORD)
        return True, "NDU disabled"
    except Exception as exc:
        return False, str(exc)


def _tweak_storage_fsutil() -> Tuple[bool, str]:
    cmds = [
        "cmd /c fsutil behavior set disablelastaccess 1",
        "cmd /c fsutil behavior set disable8dot3 1",
        "cmd /c reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem /v NtfsMemoryUsage /t REG_DWORD /d 2 /f",
    ]
    last = ""
    ok = True
    for c in cmds:
        code, out, err = run_powershell(c)
        last = (err or out or "").strip()
        ok = ok and (code == 0)
    return ok, last


def _tweak_storage_disable_indexing() -> Tuple[bool, str]:
    # Disable Windows Search service
    ok, msg = service_set("WSearch", start_type="disabled", action="stop")
    return ok, msg


def _tweak_gaming_dvr_off() -> Tuple[bool, str]:
    try:
        reg_set("HKCU", r"System\\GameConfigStore", "GameDVR_Enabled", 0, REG_DWORD)
        reg_set("HKCU", r"System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2, REG_DWORD)
        reg_set("HKLM", r"SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0, REG_DWORD)
        return True, "Game DVR and Bar disabled"
    except Exception as exc:
        return False, str(exc)


def _tweak_gaming_fullscreen_opt_off() -> Tuple[bool, str]:
    try:
        reg_set("HKCU", r"System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2, REG_DWORD)
        return True, "Fullscreen optimizations disabled"
    except Exception as exc:
        return False, str(exc)


def _tweak_gpu_hardware_sched_on() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 2, REG_DWORD)
        return True, "Hardware-accelerated GPU scheduling enabled"
    except Exception as exc:
        return False, str(exc)


def _tweak_timer_resolution() -> Tuple[bool, str]:
    code1, out1, err1 = run_powershell("cmd /c bcdedit /set useplatformtick yes")
    code2, out2, err2 = run_powershell("cmd /c bcdedit /set disabledynamictick yes")
    ok = (code1 == 0) and (code2 == 0)
    msg = (err1 or out1 or "").strip() + "; " + (err2 or out2 or "").strip()
    return ok, msg or "Timer resolution tweaks applied (reboot may be required)"


def _tweak_visual_best_performance() -> Tuple[bool, str]:
    try:
        reg_set("HKCU", r"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\VisualEffects", "VisualFXSetting", 2, REG_DWORD)
        reg_set("HKCU", r"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Animations", 0, REG_DWORD)
        reg_set("HKCU", r"Software\\Microsoft\\Windows\\DWM", "AllowTransparency", 0, REG_DWORD)
        return True, "Visual effects minimized"
    except Exception as exc:
        return False, str(exc)


_TWEAK_FUNCTIONS: Dict[str, TweakExecutor] = {
    "powerplan_ultimate": _tweak_powerplan_ultimate,
    "cpu_core_parking_off": _tweak_cpu_core_parking_off,
    "cpu_priority_foreground": _tweak_cpu_priority_foreground,
    "speculative_mitigations_off": _tweak_speculative_mitigations_off,
    "mem_disable_compression": _tweak_mem_disable_compression,
    "mem_large_system_cache": _tweak_mem_large_system_cache,
    "mem_disable_paging_exec": _tweak_mem_disable_paging_exec,
    "mem_ndu_disable": _tweak_mem_ndu_disable,
    "storage_fsutil": _tweak_storage_fsutil,
    "storage_disable_indexing": _tweak_storage_disable_indexing,
    "gaming_dvr_off": _tweak_gaming_dvr_off,
    "gaming_fullscreen_opt_off": _tweak_gaming_fullscreen_opt_off,
    "gpu_hardware_sched_on": _tweak_gpu_hardware_sched_on,
    "timer_resolution": _tweak_timer_resolution,
    "visual_best_performance": _tweak_visual_best_performance,
}


def get_tweak_catalog() -> List[Dict]:
    return [dict(entry) for entry in _TWEAK_CATALOG]


def apply_tweaks(selected: List[str]) -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []
    if not selected:
        return results
    keys = {entry["key"] for entry in _TWEAK_CATALOG}
    for key in selected:
        if key not in keys:
            msg = "Unknown tweak"
            log_event(MODULE_NAME, key, "error", msg)
            results.append((key, False, msg))
            continue
        func = _TWEAK_FUNCTIONS.get(key)
        if not func:
            msg = "Not implemented"
            log_event(MODULE_NAME, key, "error", msg)
            results.append((key, False, msg))
            continue
        try:
            ok, message = func()
        except Exception as exc:
            ok, message = False, str(exc)
        log_event(MODULE_NAME, key, "success" if ok else "error", message)
        results.append((key, ok, message))
    return results
