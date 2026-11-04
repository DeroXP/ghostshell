"""DNS configuration manager."""
import ipaddress
import json
import re
from config import DNS_PRESETS
from core import reg_originals
from core.utils import run_ps, run_cmd, get_logger

log = get_logger("dns")

# What apply_dns() found on each interface (its static IPv4 servers, or [] for
# DHCP), noted the first time it changes one, by interface GUID; reset puts
# exactly that back.  Kept with the app's other records (core/reg_originals).
_DNS_KIND = _DNS_SCOPE = "dns"
_TCPIP_IFACES = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"

# PowerShell writes its output in the console code page, which is not the one
# it is read back with: an adapter named with an accent or in Cyrillic came
# back mangled, or made the whole read fail.  As \u escapes JSON stays ASCII.
_ASCII_JSON = " | ForEach-Object { [regex]::Replace($_, '[^\\x00-\\x7f]', { '\\u{0:x4}' -f [int][char]$args[0].Value }) }"


def get_current_dns() -> list[dict]:
    """Get current DNS config for every active adapter.  Cross-references
    Get-DnsClientServerAddress with Get-NetAdapter so we know each
    adapter's kind (ethernet / wifi / etc.) — the UI uses this to show
    a clear list with type icons instead of just the first adapter."""
    # Active adapter kinds keyed by InterfaceIndex
    active = {a["index"]: a for a in get_active_adapters()}

    r = run_ps(
        "Get-DnsClientServerAddress -AddressFamily IPv4 | "
        "Select-Object InterfaceAlias,InterfaceIndex,ServerAddresses | ConvertTo-Json" + _ASCII_JSON
    )
    adapters = []
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for item in data:
                idx = item.get("InterfaceIndex", 0)
                # Skip adapters that aren't currently up — unconnected
                # WiFi / disabled Ethernet would otherwise clutter the UI
                if idx not in active:
                    continue
                adapters.append({
                    "name":  item.get("InterfaceAlias", ""),
                    "index": idx,
                    "dns":   item.get("ServerAddresses", []),
                    "kind":  active[idx].get("kind", "other"),
                    "desc":  active[idx].get("desc", ""),
                    "speed": active[idx].get("speed", ""),
                })
        except Exception:
            pass
    return adapters


def _classify_adapter_kind(media_type: str, phys_media: str, ifdesc: str) -> str:
    """Bucket an adapter into a UI-friendly kind based on its MediaType,
    PhysicalMediaType, and InterfaceDescription.  Get-NetAdapter returns:
        MediaType:         '802.3' (Ethernet) | 'Native 802.11' (Wi-Fi)
                           | '1394' (FireWire) | 'WirelessWAN' (cellular)
        PhysicalMediaType: 'Native 802.11' | '802.3' | 'BlueTooth' | etc.
    Plus the description usually has 'Wireless' / 'Wi-Fi' / 'Ethernet' /
    'Bluetooth' as a fallback signal."""
    mt = (media_type or "").lower()
    pm = (phys_media or "").lower()
    desc = (ifdesc or "").lower()
    if "802.11" in mt or "802.11" in pm or "wi-fi" in desc or "wireless" in desc:
        return "wifi"
    if "bluetooth" in pm or "bluetooth" in desc:
        return "bluetooth"
    if "wirelesswan" in mt.replace(" ", ""):
        return "cellular"
    if "802.3" in mt or "ethernet" in desc:
        return "ethernet"
    if "loopback" in pm or "loopback" in desc:
        return "loopback"
    if "tunnel" in pm or "tap" in desc or "tun" in desc or "vpn" in desc:
        return "virtual"
    return "other"


def _get_adapters_raw(active_only: bool) -> list[dict]:
    """Internal: query Get-NetAdapter and return enriched dicts.  When
    active_only=True the PowerShell filter clamps to Status=Up."""
    filter_clause = " | Where-Object {$_.Status -eq 'Up'}" if active_only else ""
    r = run_ps(
        f"Get-NetAdapter{filter_clause} | "
        "Select-Object Name,InterfaceIndex,InterfaceGuid,InterfaceDescription,MacAddress,"
        "MediaType,PhysicalMediaType,LinkSpeed,Status | ConvertTo-Json" + _ASCII_JSON
    )
    adapters = []
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for item in data:
                kind = _classify_adapter_kind(
                    item.get("MediaType", ""),
                    item.get("PhysicalMediaType", ""),
                    item.get("InterfaceDescription", ""),
                )
                # Skip loopback + virtual adapters — clutter without action
                if kind in ("loopback", "virtual"):
                    continue
                adapters.append({
                    "name":   item.get("Name", ""),
                    "index":  item.get("InterfaceIndex", 0),
                    "guid":   str(item.get("InterfaceGuid") or "").lower(),
                    "desc":   item.get("InterfaceDescription", ""),
                    "mac":    item.get("MacAddress", ""),
                    "kind":   kind,
                    "speed":  item.get("LinkSpeed", ""),
                    "status": str(item.get("Status", "")),   # Up | Disconnected | Disabled | etc.
                })
        except Exception:
            pass
    return adapters


def get_active_adapters() -> list[dict]:
    """Active (Status=Up) network adapters.  Used by DNS apply / network
    tweaks since those operations only make sense on connected adapters."""
    return _get_adapters_raw(active_only=True)


def get_all_adapters() -> list[dict]:
    """Every recognized adapter, including disconnected/disabled ones.
    Used by the UI so users can see WiFi listed even when it's not
    currently connected — gives them confidence the app supports it."""
    return _get_adapters_raw(active_only=False)


def _static_dns(guid: str):
    """The interface's static IPv4 DNS servers, [] when they come from DHCP,
    None if that cannot be read.  From the registry: nothing localised."""
    v = reg_originals.read_value(rf"{_TCPIP_IFACES}\{guid}", "NameServer")
    if v is None:
        return None
    return [s for s in re.split(r"[,\s]+", str(v.get("data") or "")) if s]


def _recorded_servers(rec) -> list:
    """The static servers on a record, [] for DHCP.  Anything but a list of
    IPv4 addresses counts as DHCP, so a damaged record cannot point DNS
    anywhere."""
    servers = rec.get("servers") if isinstance(rec, dict) else None
    try:
        return [str(ipaddress.IPv4Address(s)) for s in servers] if isinstance(servers, list) else []
    except ValueError:
        return []


def apply_dns(preset: str = None, primary: str = None, secondary: str = None) -> dict:
    """Apply DNS to all active adapters. Use preset name or custom primary/secondary."""
    if preset and preset in DNS_PRESETS:
        p = DNS_PRESETS[preset]
        primary = p["primary"]
        secondary = p["secondary"]
        log.info(f"Applying {p['name']} DNS ({primary}, {secondary})...")
    elif primary:
        log.info(f"Applying custom DNS ({primary}, {secondary or 'none'})...")
    else:
        return {"ok": False, "err": "No DNS specified"}

    adapters = get_active_adapters()
    if not adapters:
        log.warning("No active network adapters found")
        return {"ok": False, "err": "No active adapters"}

    results = []
    for adapter in adapters:
        name = adapter["name"]
        # What reset puts back.  Noted the first time only: a second preset
        # must not take the first one's servers for the user's own.
        prior = _static_dns(adapter["guid"]) if adapter.get("guid") else None
        if prior is not None:
            reg_originals.remember_state(_DNS_KIND, adapter["guid"], {"servers": prior, "name": name}, _DNS_SCOPE)
        # netsh takes the interface index as well as its name, and the index
        # survives names with quotes or characters the console mangles.
        target = f"name={adapter.get('index') or name}"
        # Set primary DNS
        r1 = run_cmd(["netsh", "interface", "ip", "set", "dns", target, "static", primary])
        # Set secondary DNS
        r2 = {"ok": True}
        if secondary:
            r2 = run_cmd(["netsh", "interface", "ip", "add", "dns", target, secondary, "index=2"])

        ok = r1["ok"] and r2["ok"]
        log.info(f"  {'✓' if ok else '✗'} {name}: {primary}, {secondary or 'N/A'}")
        results.append({"adapter": name, "ok": ok})

    # Flush DNS cache
    flush = run_cmd(["ipconfig", "/flushdns"])
    log.info(f"  {'✓' if flush['ok'] else '✗'} DNS cache flushed")

    return {"ok": all(r["ok"] for r in results), "adapters": results}


def reset_dns_to_dhcp() -> dict:
    """Reset all adapters to DHCP DNS.  An adapter apply_dns() changed gets
    back exactly what it had then: its own static servers, or DHCP.  One with
    nothing on record (changed by a build that kept none) goes to DHCP."""
    log.info("Resetting DNS to DHCP (auto)...")
    records = reg_originals.state_records(_DNS_KIND, _DNS_SCOPE)
    adapters = get_active_adapters()
    active = {a.get("guid") for a in adapters}
    if any(g not in active for g in records):
        # Changed while connected, not connected now: still gets its own back.
        adapters += [a for a in get_all_adapters() if a.get("guid") in records and a.get("guid") not in active]
    results = []
    for adapter in adapters:
        name = adapter["name"]
        target = f"name={adapter.get('index') or name}"
        guid = adapter.get("guid")
        servers = _recorded_servers(records.get(guid)) if guid in records else []
        if servers:
            r = run_cmd(["netsh", "interface", "ip", "set", "dns", target, "static", servers[0], "validate=no"])
            for i, server in enumerate(servers[1:], start=2):
                if r["ok"]:
                    r = run_cmd(["netsh", "interface", "ip", "add", "dns", target, server, f"index={i}", "validate=no"])
        else:
            r = run_cmd(["netsh", "interface", "ip", "set", "dns", target, "dhcp"])
        if r["ok"] and guid in records:
            reg_originals.forget_state(_DNS_KIND, guid, _DNS_SCOPE)
        log.info(f"  {'✓' if r['ok'] else '✗'} {name}: {', '.join(servers) if servers else 'DHCP'}")
        results.append({"adapter": name, "ok": r["ok"]})
    run_cmd(["ipconfig", "/flushdns"])
    return {"ok": all(r["ok"] for r in results), "adapters": results}


def test_dns_latency(servers: list[str] = None) -> list[dict]:
    """Ping DNS servers and return latency results."""
    if servers is None:
        servers = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "94.140.14.14", "208.67.222.222"]
    results = []
    for server in servers:
        r = run_cmd(["ping", "-n", "4", "-w", "1000", server])
        avg_ms = None
        if r["ok"] and r["out"]:
            # Parse "Average = Xms".  The labels are localised ("Mittelwert",
            # "Moyenne"...), so by position: the round-trip line is the one
            # with three "= <n><unit>" figures, minimum, maximum, average.
            for line in r["out"].splitlines():
                times = re.findall(r"=\s*(\d+)\s*(?=[^\W\d_])", line)
                if len(times) == 3:
                    avg_ms = int(times[2])
        # Find which preset this belongs to
        name = server
        for k, v in DNS_PRESETS.items():
            if v["primary"] == server:
                name = v["name"]
                break
        results.append({"server": server, "name": name, "avg_ms": avg_ms})
        log.info(f"  DNS ping {name} ({server}): {avg_ms}ms" if avg_ms else f"  DNS ping {name}: timeout")
    return sorted(results, key=lambda x: x["avg_ms"] if x["avg_ms"] is not None else 9999)
