from __future__ import annotations

import ipaddress
import re
import subprocess
from typing import Dict, Iterable, List, Optional, Tuple

from core.utils import flush_dns_cache, list_adapters, log_event, set_dns_all
from config import DNS_PRESETS

PRESETS: Dict[str, List[str]] = {k: v[:] for k, v in DNS_PRESETS.items()}
_ADAPTER_LINE_RE = re.compile(r"^\s*.+adapter\s+(?P<name>[^:]+):\s*$", re.IGNORECASE)


def available_presets() -> Dict[str, List[str]]:
    return {name: servers[:] for name, servers in PRESETS.items()}


def get_current_dns() -> Dict[str, List[str]]:
    dns_map: Dict[str, List[str]] = {}
    try:
        process = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except Exception as exc:
        log_event("dns", "get_current_dns", "error", str(exc))
        return dns_map

    output = process.stdout or ""
    if not output:
        log_event("dns", "get_current_dns", "warning", "No output from ipconfig")
        return dns_map

    return _parse_ipconfig_dns(output)


def apply_preset(name: str) -> Tuple[bool, str]:
    servers = PRESETS.get((name or "").lower()) or PRESETS.get(name or "")
    if not servers:
        msg = f"Unknown preset: {name}"
        log_event("dns", "apply_preset", "error", msg)
        return False, msg
    ok, message = set_dns_all(servers[0], servers[1] if len(servers) > 1 else None)
    return ok, message


def apply_custom(primary: str, secondary: Optional[str] = None) -> Tuple[bool, str]:
    servers, err = _validate_dns_servers(primary, secondary)
    if err:
        log_event("dns", "apply_custom", "error", err)
        return False, err
    ok, message = set_dns_all(servers[0], servers[1] if len(servers) > 1 else None)
    return ok, message


def _validate_dns_servers(primary: Optional[str], secondary: Optional[str]) -> Tuple[List[str], Optional[str]]:
    servers: List[str] = []
    if not primary:
        return servers, "Primary DNS is required"
    try:
        ipaddress.ip_address(primary)
        servers.append(primary)
    except ValueError:
        return servers, "Primary DNS is invalid"

    if secondary:
        try:
            ipaddress.ip_address(secondary)
            if secondary not in servers:
                servers.append(secondary)
        except ValueError:
            return servers, "Secondary DNS is invalid"
    return servers, None


def _parse_ipconfig_dns(output: str) -> Dict[str, List[str]]:
    dns_map: Dict[str, List[str]] = {}
    current_adapter: Optional[str] = None
    collecting_dns = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            collecting_dns = False
            continue

        adapter_match = _ADAPTER_LINE_RE.match(line)
        if adapter_match:
            current_adapter = adapter_match.group("name").strip()
            collecting_dns = False
            continue

        if current_adapter is None:
            continue

        lowered = stripped.lower()
        if "dns servers" in lowered:
            collecting_dns = True
            dns_entries = dns_map.setdefault(current_adapter, [])
            after_colon = stripped.split(":", 1)
            if len(after_colon) == 2:
                first_value = after_colon[1].strip()
                if first_value:
                    dns_entries.append(first_value)
            continue

        if collecting_dns:
            if ". . ." in stripped and ":" in stripped:
                collecting_dns = False
                continue
            if raw_line.startswith((" ", "\t")):
                value = stripped
                if value:
                    dns_map.setdefault(current_adapter, []).append(value)
                continue
            collecting_dns = False

    return {name: entries for name, entries in dns_map.items() if entries}
