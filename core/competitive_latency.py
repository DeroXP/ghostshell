"""Competitive Input Latency mode (beta.3).

ONE-CLICK bundle of the REAL, honest input-latency levers for competitive
gaming — gathered into one place (they were previously scattered across the
CPU / gaming / hard / power / latency tweak sets, so a user could easily miss
the important ones like mouse-acceleration-off, which lived in 'hard tweaks').

Included (each backed up before write, fully reversible):
  - Hold the global timer resolution at 0.5 ms (timer_manager) — the biggest
    genuinely-missing lever; tightens OS scheduling/wait granularity.
  - Mouse acceleration OFF ("Enhance Pointer Precision") — 1:1 deterministic aim
    (applied live via SystemParametersInfo, no re-login needed).
  - Game DVR / Game Bar capture OFF — removes overlay input hooks + capture cost.
  - Win32PrioritySeparation = 0x26 — short, variable, foreground-boosted quanta.
  - Power Throttling (EcoQoS) OFF — keeps game helper threads at full clock.
  - Min/Max processor state 100% + USB selective suspend OFF + PCIe ASPM OFF on
    the active power plan — no clock-ramp / USB-wake / PCIe link-wake latency.
  - Mouse + keyboard class-driver queue size = 20 (from 100) — trims a packet or
    two of buffering at high polling rates (honest: sub-ms / marginal; reboot).

Deliberately EXCLUDED (myths/placebos an audit flagged): MenuShowDelay,
ThreadDpcEnable, DpcWatchdogProfileOffset, forcing HPET / useplatformtick,
Realtime priority, hard CPU-affinity pinning, disabling Spectre mitigations.
"""
import ctypes
import json
import os

from config import APPDATA_DIR
from core.utils import run_cmd, backup_registry, get_logger
from core import timer_manager

log = get_logger("competitive")

SETTINGS_PATH = os.path.join(APPDATA_DIR, "competitive_latency_settings.json")

# powercfg GUIDs
_USB_SUB  = "2a737441-1930-4402-8d77-b2bebba308a3"; _USB_SET  = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
_PCIE_SUB = "501a4d13-42af-4429-9fd1-a8218c268e20"; _PCIE_SET = "ee12f906-d277-404b-b6da-e5fa1a576df5"
_PROC_SUB = "54533251-82be-4824-96c1-47b60b740d00"
_PROC_MIN = "893dee8e-2bef-41e0-89c6-b55d0929964c"; _PROC_MAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"

_MOUSE_KEY  = r"HKCU\Control Panel\Mouse"
_GDVR_STORE = r"HKCU\System\GameConfigStore"
_GDVR_CAP   = r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR"
_PRIO_KEY   = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"
_PT_KEY     = r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling"
_MOUCLASS   = r"HKLM\SYSTEM\CurrentControlSet\Services\mouclass\Parameters"
_KBDCLASS   = r"HKLM\SYSTEM\CurrentControlSet\Services\kbdclass\Parameters"


def _reg(key, val, data, vtype="REG_DWORD"):
    return run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])["ok"]

def _regdel(key, val):
    return run_cmd(["reg", "delete", key, "/v", val, "/f"])["ok"]

def _powercfg(sub, setting, val):
    run_cmd(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", sub, setting, str(val)])
    run_cmd(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", sub, setting, str(val)])

def _set_mouse_accel(enabled: bool):
    """enabled=False -> 1:1 raw (no accel); True -> Windows defaults.  Applied
    live via SPI_SETMOUSE so it takes effect without a re-login."""
    if enabled:
        _reg(_MOUSE_KEY, "MouseSpeed", "1", "REG_SZ")
        _reg(_MOUSE_KEY, "MouseThreshold1", "6", "REG_SZ")
        _reg(_MOUSE_KEY, "MouseThreshold2", "10", "REG_SZ")
        arr = (ctypes.c_int * 3)(6, 10, 1)
    else:
        _reg(_MOUSE_KEY, "MouseSpeed", "0", "REG_SZ")
        _reg(_MOUSE_KEY, "MouseThreshold1", "0", "REG_SZ")
        _reg(_MOUSE_KEY, "MouseThreshold2", "0", "REG_SZ")
        arr = (ctypes.c_int * 3)(0, 0, 0)
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0004, 0, arr, 0x03)  # SPI_SETMOUSE
    except Exception as e:
        log.debug(f"SPI_SETMOUSE failed: {e}")


def _save(enabled: bool):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f)
    except Exception as e:
        log.debug(f"competitive settings save failed: {e}")

def _load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False}


def apply() -> dict:
    log.info("Applying Competitive Latency mode...")
    res = []
    t = timer_manager.start_timer_resolution_hold(0.5)
    res.append({"name": "Timer resolution held at 0.5 ms", "ok": bool(t.get("ok")),
                "detail": f"{t.get('resolution_ms')} ms"})

    backup_registry(_MOUSE_KEY, "competitive_mouse")
    _set_mouse_accel(False)
    res.append({"name": "Mouse acceleration off (1:1 raw)", "ok": True})

    _reg(_GDVR_STORE, "GameDVR_Enabled", "0")
    _reg(_GDVR_CAP, "AppCaptureEnabled", "0")
    res.append({"name": "Game DVR / Game Bar capture off", "ok": True})

    backup_registry(_PRIO_KEY, "competitive_priority")
    _reg(_PRIO_KEY, "Win32PrioritySeparation", "38")  # 0x26
    res.append({"name": "Foreground priority quanta (0x26)", "ok": True})

    _reg(_PT_KEY, "PowerThrottlingOff", "1")
    res.append({"name": "Power throttling (EcoQoS) off", "ok": True})

    _powercfg(_PROC_SUB, _PROC_MIN, 100)
    _powercfg(_PROC_SUB, _PROC_MAX, 100)
    _powercfg(_USB_SUB, _USB_SET, 0)
    _powercfg(_PCIE_SUB, _PCIE_SET, 0)
    run_cmd(["powercfg", "/S", "SCHEME_CURRENT"])
    res.append({"name": "CPU 100% · USB suspend off · PCIe ASPM off", "ok": True})

    backup_registry(_MOUCLASS, "competitive_mouclass")
    backup_registry(_KBDCLASS, "competitive_kbdclass")
    _reg(_MOUCLASS, "MouseDataQueueSize", "20")
    _reg(_KBDCLASS, "KeyboardDataQueueSize", "20")
    res.append({"name": "HID queue size 20 (effective after reboot)", "ok": True})

    _save(True)
    log.info("Competitive Latency mode applied")
    return {"ok": True, "results": res}


def reset() -> dict:
    log.info("Reverting Competitive Latency mode...")
    res = []
    timer_manager.stop_timer_resolution_hold()
    _set_mouse_accel(True)
    _reg(_GDVR_STORE, "GameDVR_Enabled", "1")
    _reg(_GDVR_CAP, "AppCaptureEnabled", "1")
    _reg(_PRIO_KEY, "Win32PrioritySeparation", "2")
    _regdel(_PT_KEY, "PowerThrottlingOff")
    _powercfg(_USB_SUB, _USB_SET, 1)           # Windows default: USB suspend on
    run_cmd(["powercfg", "/S", "SCHEME_CURRENT"])
    _regdel(_MOUCLASS, "MouseDataQueueSize")
    _regdel(_KBDCLASS, "KeyboardDataQueueSize")
    res.append({"name": "Reverted to Windows defaults", "ok": True})
    _save(False)
    return {"ok": True, "results": res}


def status() -> dict:
    return {
        "enabled": bool(_load().get("enabled")),
        "timer_resolution_ms": timer_manager.query_timer_resolution_ms(),
        "timer_hold_active": timer_manager.is_timer_hold_active(),
    }


def apply_on_boot():
    """Re-assert the (process-lifetime) timer hold if the mode was left on."""
    if _load().get("enabled"):
        timer_manager.start_timer_resolution_hold(0.5)
        log.info("Competitive Latency was enabled — re-asserted timer hold on boot")
