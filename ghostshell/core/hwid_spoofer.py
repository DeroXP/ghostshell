import json
import os
import random
import re
import string
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import winreg
import wmi

from config import APPDATA_DIR
from core.utils import (
    backup_file,
    generate_guid,
    log_event,
    mask,
    reg_set,
    run_powershell,
)

MACHINE_GUID_PATH = r"SOFTWARE\Microsoft\Cryptography"
MACHINE_GUID_VALUE = "MachineGuid"
PRODUCT_ID_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
PRODUCT_ID_VALUE = "ProductId"
COMPUTER_NAME_PATH_ACTIVE = r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName"
COMPUTER_NAME_PATH = r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName"
NIC_CLASS_PATH = r"SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}"
MOUNT_POINTS_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2"

BACKUP_FILE = Path(APPDATA_DIR) / "hwid_backup.json"


HEX12_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
PRODUCT_ID_RE = re.compile(r"^[0-9]{5}-[0-9]{5}-[0-9]{5}-[0-9]{5}$")
COMPUTER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")


def _read_registry_value(root: int, path: str, value_name: str) -> Optional[str]:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | getattr(winreg, 'KEY_WOW64_64KEY', 0)) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            if isinstance(value, str):
                return value
            if isinstance(value, int):
                return str(value)
            return None
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_event("hwid", "read_registry", "error", f"{path}:{value_name} {exc}")
        return None


def _write_registry_value(root: int, path: str, value_name: str, data: str, reg_type: int = winreg.REG_SZ) -> bool:
    try:
        reg_set("HKLM" if root == winreg.HKEY_LOCAL_MACHINE else "HKCU", path, value_name, data, "REG_SZ" if reg_type == winreg.REG_SZ else "REG_DWORD")
        return True
    except Exception as exc:
        log_event("hwid", "write_registry", "error", f"{path}:{value_name} {exc}")
        return False


def _delete_registry_value(root: int, path: str, value_name: str) -> bool:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE | getattr(winreg, 'KEY_WOW64_64KEY', 0)) as key:
            winreg.DeleteValue(key, value_name)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log_event("hwid", "delete_registry", "error", f"{path}:{value_name} {exc}")
        return False


# ---------------- Current values ----------------

def _current_machine_guid() -> Optional[str]:
    return _read_registry_value(winreg.HKEY_LOCAL_MACHINE, MACHINE_GUID_PATH, MACHINE_GUID_VALUE)


def _current_product_id() -> Optional[str]:
    return _read_registry_value(winreg.HKEY_LOCAL_MACHINE, PRODUCT_ID_PATH, PRODUCT_ID_VALUE)


def _current_computer_name() -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, COMPUTER_NAME_PATH, 0, winreg.KEY_READ | getattr(winreg, 'KEY_WOW64_64KEY', 0)) as k:
            v, _ = winreg.QueryValueEx(k, "ComputerName")
            return str(v)
    except Exception:
        return None


def _collect_adapters() -> List[Dict[str, str]]:
    adapters: List[Dict[str, str]] = []
    base = NIC_CLASS_PATH
    for i in range(0, 64):
        sub = f"{i:04d}"
        path = f"{base}\\{sub}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | getattr(winreg, 'KEY_WOW64_64KEY', 0)) as key:
                try:
                    desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                except FileNotFoundError:
                    desc = ""
                try:
                    guid, _ = winreg.QueryValueEx(key, "NetCfgInstanceId")
                except FileNotFoundError:
                    guid = ""
                try:
                    mac_override, _ = winreg.QueryValueEx(key, "NetworkAddress")
                except FileNotFoundError:
                    mac_override = ""
                if desc and "Wi-Fi Direct" in str(desc):
                    continue
                adapters.append({
                    "registry_path": path,
                    "desc": str(desc or ""),
                    "guid": str(guid or "").upper(),
                    "override": str(mac_override or ""),
                })
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return adapters


def _current_macs() -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for a in _collect_adapters():
        if a.get("guid"):
            out[a["guid"]] = {
                "desc": a.get("desc", ""),
                "override": a.get("override", ""),
            }
    return out


def get_current() -> Dict[str, object]:
    data: Dict[str, object] = {
        "machine_guid": _current_machine_guid() or "",
        "product_id": _current_product_id() or "",
        "computer_name": _current_computer_name() or "",
        "mac_addresses": _current_macs(),
        "volume_serials": _get_volume_serials(),
    }
    # Mask MAC overrides for display
    for guid, info in list(data.get("mac_addresses", {}).items()):
        ov = info.get("override") or ""
        if isinstance(ov, str) and len(ov) >= 12:
            info["override_masked"] = ov[:2] + "*" * 8 + ov[-2:]
    return data


# ---------------- Generation ----------------

def _random_mac() -> str:
    # Locally administered unicast MAC: set second least significant bit of first octet
    first = random.randint(0x00, 0xFF) | 0x02
    first &= 0xFE
    octets = [first] + [random.randint(0x00, 0xFF) for _ in range(5)]
    return "".join(f"{b:02X}" for b in octets)


def _random_product_id() -> str:
    return "-".join("".join(random.choice(string.digits) for _ in range(5)) for _ in range(4))


def generate(plan: Dict[str, bool] | None = None) -> Dict[str, str]:
    if plan is None:
        plan = {k: True for k in ("machine_guid", "product_id", "computer_name", "mac_addresses", "volume_serials")}
    out: Dict[str, str] = {}
    if plan.get("machine_guid"):
        out["machine_guid"] = generate_guid()
    if plan.get("product_id"):
        out["product_id"] = _random_product_id()
    if plan.get("computer_name"):
        base = "GHOST" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5))
        out["computer_name"] = base[:15]
    if plan.get("mac_addresses"):
        # Indicate that per-adapter MACs will be generated during apply based on available adapters
        out["mac_addresses"] = "auto"
    if plan.get("volume_serials"):
        out["volume_serials"] = "overlay"
    return out


# ---------------- Apply ----------------

def _apply_machine_guid(value: str, messages: List[str]) -> bool:
    ok = _write_registry_value(winreg.HKEY_LOCAL_MACHINE, MACHINE_GUID_PATH, MACHINE_GUID_VALUE, value)
    messages.append("MachineGuid updated (reboot required)")
    return ok


def _apply_product_id(value: str, messages: List[str]) -> bool:
    if not PRODUCT_ID_RE.match(value or ""):
        messages.append("Invalid ProductId format")
        return False
    ok = _write_registry_value(winreg.HKEY_LOCAL_MACHINE, PRODUCT_ID_PATH, PRODUCT_ID_VALUE, value)
    messages.append("ProductId updated")
    return ok


def _apply_computer_name(value: str, messages: List[str]) -> bool:
    if not COMPUTER_NAME_RE.match(value or ""):
        messages.append("Invalid computer name (alnum-dash up to 15 chars)")
        return False
    try:
        _ = _write_registry_value(winreg.HKEY_LOCAL_MACHINE, COMPUTER_NAME_PATH_ACTIVE, "ComputerName", value)
        _ = _write_registry_value(winreg.HKEY_LOCAL_MACHINE, COMPUTER_NAME_PATH, "ComputerName", value)
        code, out, err = run_powershell(f"wmic computersystem where name='%COMPUTERNAME%' call rename name='{value}'")
        messages.append("ComputerName updated (reboot recommended)")
        return True
    except Exception as exc:
        messages.append(f"ComputerName update failed: {exc}")
        return False


def _apply_mac_changes(mac_overrides: Dict[str, str], messages: List[str]) -> bool:
    # Write overrides to registry, then disable/enable adapters using PowerShell
    adapters = _collect_adapters()
    index = {a.get("guid"): a for a in adapters}
    any_changed = False
    for guid, value in mac_overrides.items():
        item = index.get(guid.upper()) if guid else None
        if not item:
            continue
        path = item.get("registry_path")
        if not path:
            continue
        if value and not HEX12_RE.match(value):
            continue
        if value:
            _write_registry_value(winreg.HKEY_LOCAL_MACHINE, path, "NetworkAddress", value)
        else:
            _delete_registry_value(winreg.HKEY_LOCAL_MACHINE, path, "NetworkAddress")
        any_changed = True
    if any_changed:
        # Try to bounce adapters; ignore errors
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {"
            " Disable-NetAdapter -Name $_.Name -Confirm:$false -PassThru | Out-Null;"
            " Start-Sleep -Milliseconds 500;"
            " Enable-NetAdapter -Name $_.Name -Confirm:$false -PassThru | Out-Null;"
            "}"
        )
        run_powershell(ps)
        messages.append("MAC overrides applied (adapters restarted)")
    return True


def _get_volume_serials() -> Dict[str, str]:
    try:
        code, out, err = run_powershell("Get-Volume | Select-Object DriveLetter,FileSystemLabel,UniqueId | ConvertTo-Json")
        if code == 0 and out:
            data = json.loads(out)
            if isinstance(data, list):
                return {str(item.get("DriveLetter") or ""): str(item.get("UniqueId") or "") for item in data if item}
    except Exception:
        pass
    return {}


def _apply_volume_overlays(mapping: Dict[str, str], messages: List[str]) -> bool:
    # Not practically changeable without reformat. We record desired overlays only.
    try:
        backup = _load_backup()
        backup.setdefault("volume_serials", {}).update(mapping)
        _save_backup(backup)
        messages.append("Volume serial overlays stored (display only; cannot modify NTFS serial)")
        return True
    except Exception as exc:
        messages.append(f"Volume overlay save failed: {exc}")
        return False


def backup_exists() -> bool:
    return BACKUP_FILE.exists()


def _load_backup() -> Dict[str, object]:
    if not BACKUP_FILE.exists():
        return {}
    try:
        return json.loads(BACKUP_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_backup(data: Dict[str, object]) -> None:
    BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def generate(plan: Dict[str, bool] | None = None) -> Dict[str, str]:  # type: ignore[override]
    return generate.__wrapped__(plan)  # type: ignore[attr-defined]


def apply(changes: Dict[str, str]) -> Tuple[bool, str]:
    messages: List[str] = []
    success = True

    # Save backup first time
    if not backup_exists():
        original = get_current()
        try:
            _save_backup(original)
            messages.append("Backup created")
        except Exception as exc:
            messages.append(f"Backup failed: {exc}")

    mg = changes.get("machine_guid")
    if mg:
        success &= _apply_machine_guid(mg, messages)

    pid = changes.get("product_id")
    if pid:
        success &= _apply_product_id(pid, messages)

    cname = changes.get("computer_name")
    if cname:
        success &= _apply_computer_name(cname, messages)

    macs: Dict[str, str] = {}
    for k, v in changes.items():
        if k.startswith("mac_"):
            guid = k.split("_", 1)[1].upper()
            macs[guid] = v
    if macs:
        _apply_mac_changes(macs, messages)

    overlays = changes.get("volume_serials_overlay")
    if isinstance(overlays, dict):
        _apply_volume_overlays(overlays, messages)

    msg = "; ".join(messages) if messages else "No changes applied"
    log_event("hwid", "apply", "success" if success else "warning", msg)
    return bool(success), msg


def restore() -> Tuple[bool, str]:
    if not backup_exists():
        return False, "No backup to restore"
    backup = _load_backup()
    messages: List[str] = []
    success = True

    if isinstance(backup.get("machine_guid"), str):
        success &= _apply_machine_guid(str(backup.get("machine_guid")), messages)
    if isinstance(backup.get("product_id"), str):
        success &= _apply_product_id(str(backup.get("product_id")), messages)
    if isinstance(backup.get("computer_name"), str):
        success &= _apply_computer_name(str(backup.get("computer_name")), messages)

    mac_info = backup.get("mac_addresses")
    if isinstance(mac_info, dict):
        macs: Dict[str, str] = {}
        for guid, info in mac_info.items():
            if isinstance(info, dict):
                macs[str(guid).upper()] = str(info.get("override") or "")
            elif isinstance(info, str):
                macs[str(guid).upper()] = info
        _apply_mac_changes(macs, messages)

    overlay = backup.get("volume_serials")
    if isinstance(overlay, dict):
        _apply_volume_overlays(overlay, messages)

    msg = "; ".join(messages) if messages else "Backup restored"
    log_event("hwid", "restore", "success" if success else "warning", msg)
    return bool(success), msg
