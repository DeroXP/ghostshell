from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from config import PROFILE_DIR
from core import dns_manager, system_info, utils


def safe_name(name: str) -> str:
    invalid_chars = set('<>:"/\\|?*\r\n\t')
    cleaned = (name or "").strip()
    if not cleaned:
        return "profile"
    sanitized_chars: List[str] = []
    for ch in cleaned:
        if ch in invalid_chars:
            sanitized_chars.append("_")
        else:
            sanitized_chars.append(ch)
        if len(sanitized_chars) >= 64:
            break
    sanitized = "".join(sanitized_chars).strip(" .")
    return sanitized or "profile"


def export_profile(name: str, extras: dict | None = None) -> Tuple[bool, str, str]:
    action = "export"
    utils.ensure_app_dirs()
    profile_name = safe_name(name)
    path = PROFILE_DIR / f"{profile_name}.json"
    try:
        if extras is None:
            extras = {}
        if not isinstance(extras, dict):
            raise ValueError("extras must be a dictionary")
        snapshot = _build_snapshot(extras)
        path.parent.mkdir(parents=True, exist_ok=True)
        utils.safe_write_text(path, json.dumps(snapshot, indent=2, sort_keys=True))
        message = f"Profile '{profile_name}' saved to {path}"
        utils.log_event("profile", action, "success", message)
        return True, message, str(path)
    except Exception as exc:
        error_message = f"Failed to export profile '{profile_name}': {exc}"
        utils.log_event("profile", action, "error", error_message)
        return False, error_message, ""


def import_profile(path_or_name: str, apply: bool = False) -> Tuple[bool, str]:
    action = "import"
    utils.ensure_app_dirs()
    resolved_path = _resolve_profile_path(path_or_name)
    if not resolved_path.exists():
        message = f"Profile not found: {resolved_path}"
        utils.log_event("profile", action, "error", message)
        return False, message

    try:
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
    except Exception as exc:
        message = f"Failed to read profile: {exc}"
        utils.log_event("profile", action, "error", message)
        return False, message

    name = resolved_path.stem
    if not isinstance(data, dict) or "version" not in data:
        message = "Invalid profile format"
        utils.log_event("profile", action, "error", message)
        return False, message

    apply_msg = ""
    if apply:
        ok, msg = _apply_dns_settings(data)
        apply_msg = f" Apply: {msg}"
        utils.log_event("profile", "apply", "success" if ok else "error", msg)

    message = f"Profile '{name}' loaded.{apply_msg}"
    utils.log_event("profile", action, "success", message)
    return True, message


def list_profiles() -> List[Dict[str, Any]]:
    utils.ensure_app_dirs()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for p in sorted(PROFILE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            stat = p.stat()
            created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            items.append({
                "name": p.stem,
                "path": str(p),
                "size_bytes": stat.st_size,
                "created": created,
            })
        except Exception:
            continue
    return items


def delete_profile(name: str) -> Tuple[bool, str]:
    action = "delete"
    resolved_path = _resolve_profile_path(name)
    try:
        if resolved_path.exists():
            resolved_path.unlink()
            message = f"Profile '{resolved_path.stem}' deleted"
            utils.log_event("profile", action, "success", message)
            return True, message
        else:
            message = f"Profile not found: {resolved_path.stem}"
            utils.log_event("profile", action, "error", message)
            return False, message
    except Exception as exc:
        message = f"Failed to delete profile: {exc}"
        utils.log_event("profile", action, "error", message)
        return False, message


# --- internals ---

def _resolve_profile_path(path_or_name: str) -> Path:
    candidate = Path(path_or_name)
    if candidate.suffix.lower() == ".json" and candidate.exists():
        return candidate
    return PROFILE_DIR / f"{safe_name(path_or_name)}.json"


def _build_snapshot(extras: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    os_info = _safe_call(system_info.get_os_info)
    cpu_info = _safe_call(system_info.get_cpu_info)
    gpu_info = _safe_call(system_info.get_gpu_info)
    dns_map = _safe_call(dns_manager.get_current_dns) or {}
    dns_global = _infer_global_dns(dns_map)

    snapshot = {
        "version": "1.0",
        "created_at": now,
        "os": os_info,
        "cpu": cpu_info,
        "gpu": gpu_info,
        "dns": dns_map,
        "dns_global": dns_global,
        "notes": extras or {},
    }
    return snapshot


def _safe_call(fn):
    try:
        return fn()
    except Exception:
        return None


def _infer_global_dns(dns_map: Dict[str, Any]) -> List[str]:
    pairs: List[str] = []
    for adapter, value in (dns_map or {}).items():
        primary, secondary = _extract_dns_pair(value)
        if primary:
            key = f"{primary}|{secondary or ''}"
            pairs.append(key)
    if not pairs:
        return []
    most_common, _ = Counter(pairs).most_common(1)[0]
    p, s = most_common.split("|", 1)
    return [p] + ([s] if s else [])


def _extract_dns_pair(value: Any) -> Tuple[str | None, str | None]:
    primary: str | None = None
    secondary: str | None = None
    if isinstance(value, dict):
        primary = _get_first_value(value, ("primary", "Primary", "dns1", "preferred", "Preferred"))
        secondary = _get_first_value(value, ("secondary", "Secondary", "dns2", "alternate", "Alternate"))
    elif isinstance(value, (list, tuple, set)):
        entries = [str(item).strip() for item in value if isinstance(item, (str, int, float))]
        if entries:
            primary = entries[0]
        if len(entries) > 1:
            secondary = entries[1]
    elif isinstance(value, str):
        tokens = [token.strip() for token in re.split(r"[,\s;]+", value) if token.strip()]
        if tokens:
            primary = tokens[0]
        if len(tokens) > 1:
            secondary = tokens[1]
    return primary or None, secondary or None


def _get_first_value(mapping: Dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        if key in mapping and isinstance(mapping[key], str):
            candidate = mapping[key].strip()
            if candidate:
                return candidate
    return None


def _apply_dns_settings(snapshot: Dict[str, Any]) -> Tuple[bool, str]:
    dns_global = snapshot.get("dns_global")
    if not isinstance(dns_global, list) or not dns_global:
        return False, "DNS settings not available in profile"
    primary = dns_global[0]
    secondary = dns_global[1] if len(dns_global) > 1 else None
    if not isinstance(primary, str) or not primary.strip():
        return False, "Primary DNS entry is invalid"
    try:
        dns_manager.apply_custom(primary.strip(), secondary.strip() if isinstance(secondary, str) and secondary.strip() else None)
        return True, "DNS settings applied successfully"
    except Exception as exc:
        return False, f"Failed to apply DNS settings: {exc}"
