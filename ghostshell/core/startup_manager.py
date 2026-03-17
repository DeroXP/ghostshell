from typing import List, Dict, Tuple
import json
import winreg
from pathlib import Path

from core.utils import log_event
from config import APPDATA_DIR

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
WOW64_64KEY = getattr(winreg, "KEY_WOW64_64KEY", 0)
APPDATA_PATH = Path(APPDATA_DIR)
BACKUP_FILE = APPDATA_PATH / "startup_backup.json"
LOCATION_ROOTS = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
}


def _normalize_location(location: str) -> str:
    return location.strip().upper() if isinstance(location, str) else ""


def _open_run_key(root: int, access: int):
    return winreg.OpenKey(root, RUN_KEY_PATH, 0, access | WOW64_64KEY)


def _load_backup() -> Dict[str, str]:
    if not BACKUP_FILE.exists():
        return {}
    try:
        with BACKUP_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log_event("startup", "backup_load", "failure", str(exc))
        return {}
    if not isinstance(data, dict):
        return {}
    sanitized: Dict[str, str] = {}
    for key, value in data.items():
        sanitized[str(key)] = "" if value is None else str(value)
    return sanitized


def _save_backup(data: Dict[str, str]) -> bool:
    try:
        BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        with BACKUP_FILE.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return True
    except OSError as exc:
        log_event("startup", "backup_save", "failure", str(exc))
        return False


def _write_backup_entry(identifier: str, command: str) -> bool:
    data = _load_backup()
    data[identifier] = command
    return _save_backup(data)


def _remove_backup_entry(identifier: str) -> None:
    data = _load_backup()
    if identifier in data:
        del data[identifier]
        _save_backup(data)


def _get_backup_command(identifier: str) -> str | None:
    return _load_backup().get(identifier)


def list_startup() -> List[Dict]:
    items: List[Dict] = []
    for loc, root in LOCATION_ROOTS.items():
        try:
            with _open_run_key(root, winreg.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        name, value, _typ = winreg.EnumValue(key, i)
                        items.append({
                            "name": name,
                            "command": value,
                            "location": loc,
                            "enabled": True,
                        })
                        i += 1
                    except OSError:
                        break
        except FileNotFoundError:
            continue
        except OSError as exc:
            log_event("startup", "enumerate", "failure", f"{loc}: {exc}")
    return items


def disable_item(location: str, name: str) -> Tuple[bool, str]:
    normalized_location = _normalize_location(location)
    if normalized_location not in LOCATION_ROOTS:
        log_event("startup", "disable", "failure", f"Invalid location: {location}")
        return False, "Invalid registry location. Use HKCU or HKLM."
    if not isinstance(name, str) or not name.strip():
        log_event("startup", "disable", "failure", "Missing startup item name")
        return False, "Startup item name is required."
    root = LOCATION_ROOTS[normalized_location]
    key = None
    try:
        key = _open_run_key(root, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        try:
            _, command, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            command = None
        try:
            winreg.DeleteValue(key, name)
            if command:
                _write_backup_entry(f"{normalized_location}/{name}", str(command))
        except FileNotFoundError:
            pass
        log_event("startup", "disable", "success", f"{normalized_location}/{name}")
        return True, "Startup item disabled."
    except OSError as exc:
        log_event("startup", "disable", "failure", f"{normalized_location}/{name}: {exc}")
        return False, "Failed to disable startup item."
    finally:
        if key:
            winreg.CloseKey(key)


def enable_item(location: str, name: str, command: str | None = None) -> Tuple[bool, str]:
    normalized_location = _normalize_location(location)
    if normalized_location not in LOCATION_ROOTS:
        log_event("startup", "enable", "failure", f"Invalid location: {location}")
        return False, "Invalid registry location. Use HKCU or HKLM."
    if not isinstance(name, str) or not name.strip():
        log_event("startup", "enable", "failure", "Missing startup item name")
        return False, "Startup item name is required."
    trimmed_name = name.strip()
    identifier = f"{normalized_location}/{trimmed_name}"
    if command is None:
        command_str = _get_backup_command(identifier)
        if command_str is None:
            log_event("startup", "enable", "failure", f"{identifier}: no backup command")
            return False, "No backup found for this startup item. Provide the command explicitly."
    else:
        command_str = str(command)
    if not command_str or not command_str.strip():
        log_event("startup", "enable", "failure", f"{identifier}: empty command")
        return False, "Startup command cannot be empty."
    root = LOCATION_ROOTS[normalized_location]
    key = None
    try:
        try:
            key = _open_run_key(root, winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            key = winreg.CreateKeyEx(root, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE | WOW64_64KEY)
    except OSError as exc:
        log_event("startup", "enable", "failure", f"{identifier}: {exc}")
        return False, "Unable to access startup registry key."
    try:
        winreg.SetValueEx(key, trimmed_name, 0, winreg.REG_SZ, command_str)
    except OSError as exc:
        log_event("startup", "enable", "failure", f"{identifier}: write error {exc}")
        return False, "Failed to enable startup item."
    finally:
        if key:
            winreg.CloseKey(key)
    _remove_backup_entry(identifier)
    log_event("startup", "enable", "success", f"Enabled {identifier}")
    return True, "Startup item enabled."
