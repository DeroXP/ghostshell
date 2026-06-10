"""DNS configuration manager."""
import json
from config import DNS_PRESETS
from core.utils import run_ps, run_cmd, get_logger

log = get_logger("dns")


def get_current_dns() -> list[dict]:
    """Get current DNS config for every active adapter.  Cross-references
    Get-DnsClientServerAddress with Get-NetAdapter so we know each
    adapter's kind (ethernet / wifi / etc.) — the UI uses this to show
    a clear list with type icons instead of just the first adapter."""
    # Active adapter kinds keyed by InterfaceIndex
    active = {a["index"]: a for a in get_active_adapters()}

    r = run_ps(
        "Get-DnsClientServerAddress -AddressFamily IPv4 | "
        "Select-Object InterfaceAlias,InterfaceIndex,ServerAddresses | ConvertTo-Json"
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
        "Select-Object Name,InterfaceIndex,InterfaceDescription,MacAddress,"
        "MediaType,PhysicalMediaType,LinkSpeed,Status | ConvertTo-Json"
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
        # Set primary DNS
        r1 = run_cmd(["netsh", "interface", "ip", "set", "dns", f"name={name}", "static", primary])
        # Set secondary DNS
        r2 = {"ok": True}
        if secondary:
            r2 = run_cmd(["netsh", "interface", "ip", "add", "dns", f"name={name}", secondary, "index=2"])

        ok = r1["ok"]
        log.info(f"  {'✓' if ok else '✗'} {name}: {primary}, {secondary or 'N/A'}")
        results.append({"adapter": name, "ok": ok})

    # Flush DNS cache
    flush = run_cmd(["ipconfig", "/flushdns"])
    log.info(f"  {'✓' if flush['ok'] else '✗'} DNS cache flushed")

    return {"ok": all(r["ok"] for r in results), "adapters": results}


def reset_dns_to_dhcp() -> dict:
    """Reset all adapters to DHCP DNS."""
    log.info("Resetting DNS to DHCP (auto)...")
    adapters = get_active_adapters()
    results = []
    for adapter in adapters:
        name = adapter["name"]
        r = run_cmd(["netsh", "interface", "ip", "set", "dns", f"name={name}", "dhcp"])
        log.info(f"  {'✓' if r['ok'] else '✗'} {name}: DHCP")
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
            # Parse "Average = Xms"
            for line in r["out"].splitlines():
                if "Average" in line or "average" in line.lower():
                    parts = line.split("=")
                    if len(parts) >= 2:
                        val = parts[-1].strip().replace("ms", "").strip()
                        try:
                            avg_ms = int(val)
                        except ValueError:
                            pass
        # Find which preset this belongs to
        name = server
        for k, v in DNS_PRESETS.items():
            if v["primary"] == server:
                name = v["name"]
                break
        results.append({"server": server, "name": name, "avg_ms": avg_ms})
        log.info(f"  DNS ping {name} ({server}): {avg_ms}ms" if avg_ms else f"  DNS ping {name}: timeout")
    return sorted(results, key=lambda x: x["avg_ms"] if x["avg_ms"] is not None else 9999)
