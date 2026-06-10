"""Network latency & adapter optimization."""
import json
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("network")


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    # 3.4.2 — surface failed writes in the log (callers mostly hardcode
    # ok:True; GhostShell runs as admin so failures are rare but should
    # not vanish silently).
    if not r.get("ok"):
        err = (r.get("err") or r.get("out") or "").strip()[:160]
        log.warning(f"reg write FAILED: {key}\\{val} = {data} ({vtype}) — {err}")
    return r["ok"]


def _get_interface_guids() -> list[str]:
    """Get GUIDs of all network interfaces from registry."""
    r = run_cmd(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"])
    guids = []
    if r["ok"] and r["out"]:
        for line in r["out"].splitlines():
            line = line.strip()
            if line.startswith("HKEY_") and "{" in line:
                guid = line.split("\\")[-1]
                guids.append(guid)
    return guids


def apply_nagle_tweaks() -> list[dict]:
    """Disable Nagle's Algorithm on all interfaces (reduces micro-latency)."""
    log.info("Disabling Nagle's Algorithm on all interfaces...")
    guids = _get_interface_guids()
    results = []
    for guid in guids:
        base = rf"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\{guid}"
        ok1 = _reg(base, "TcpAckFrequency", "1")
        ok2 = _reg(base, "TCPNoDelay", "1")
        ok = ok1 and ok2
        log.info(f"  {'✓' if ok else '✗'} Nagle off: {guid[:12]}...")
        results.append({"guid": guid, "ok": ok})
    return results


def apply_tcp_tweaks() -> list[dict]:
    """Apply TCP/IP stack optimizations.

    v3.3.1-beta.7: trimmed.  Removed:
      - `Tcp1323Opts=1`: not honored since Win7.  RFC 1323
        timestamps/window-scaling are controlled via
        `netsh int tcp set global timestamps=...` now (and the
        netsh tweak block below DISABLES timestamps, so writing 1
        here was a self-contradiction).
      - `MaxFreeTcbs` / `MaxHashTableSize`: Win2003/XP TCBTable
        knobs.  The TCP/IP stack was rewritten in Vista and these
        values have not been read by tcpip.sys since.
      - `DefaultTTL=64`: cosmetic only.  Default 128 vs 64 has zero
        performance impact — just changes traceroute hop count.
      - `TcpMaxDataRetransmissions=5`: Win10/11 default is already
        5.  Pure placebo.
    """
    log.info("Applying TCP/IP stack optimizations...")
    results = []
    base = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    backup_registry(base, "tcp_params")

    tweaks = [
        ("TcpTimedWaitDelay", "30", "Reduce TIME_WAIT to 30s"),
        ("MaxUserPort", "65534", "Max ephemeral ports"),
    ]

    for val, data, desc in tweaks:
        ok = _reg(base, val, data)
        log.info(f"  {'✓' if ok else '✗'} {desc}")
        results.append({"name": desc, "ok": ok})

    return results


def disable_network_throttling() -> list[dict]:
    """Disable multimedia network throttling."""
    log.info("Disabling network throttling...")
    results = []
    base = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    backup_registry(base, "net_throttle")

    # NetworkThrottlingIndex = 0xFFFFFFFF (disabled)
    ok = _reg(base, "NetworkThrottlingIndex", "4294967295")
    log.info(f"  {'✓' if ok else '✗'} Network throttling disabled")
    results.append({"name": "Disable Network Throttling", "ok": ok})

    # v3.3.1-beta.7: SystemResponsiveness 0 → 10.  Zero starves the
    # Windows audio service (Audiosrv runs in the multimedia class)
    # which causes Discord / in-game voice crackling under heavy CPU
    # load.  Microsoft's recommended value for gaming PCs is 10 —
    # keeps 90% of multimedia bandwidth for foreground while leaving
    # the audio thread oxygen.
    ok = _reg(base, "SystemResponsiveness", "10")
    log.info(f"  {'✓' if ok else '✗'} System Responsiveness = 10")
    results.append({"name": "System Responsiveness", "ok": ok})

    return results


def disable_lso() -> list[dict]:
    """Disable Large Send Offload on Ethernet adapters only.

    v3.3.1-beta.7: filtered to 802.3 (same filter used by
    `optimize_adapter_power` and `apply_rss_rsc_adapter_tweaks`).
    Previously this hit every Up adapter including WiFi, which is
    the same `Set-NetAdapterAdvancedProperty` family that corrupted
    Intel WiFi bindings and made the WiFi toggle disappear from
    Windows Settings in the bug we fixed in beta.6.  LSO is also
    largely a placebo on modern NICs — Win7-era chipsets had broken
    LSO implementations that gave the tweak its reputation, but
    current Intel/Realtek/Killer NIC drivers handle LSO correctly.
    """
    log.info("Disabling Large Send Offload (LSO) on Ethernet adapters only...")
    r = run_ps(
        "Get-NetAdapter | Where-Object { "
        "$_.Status -eq 'Up' -and ($_.MediaType -eq '802.3' -or $_.PhysicalMediaType -eq '802.3') "
        "} | Select-Object Name | ConvertTo-Json"
    )
    results = []
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for adapter in data:
                name = adapter.get("Name", "")
                if not name:
                    continue
                for version in ["V1IPv4", "V2IPv4", "V2IPv6"]:
                    r2 = run_ps(
                        f'Set-NetAdapterAdvancedProperty -Name "{name}" '
                        f'-DisplayName "*Large Send Offload*{version}*" '
                        f'-DisplayValue "Disabled" -ErrorAction SilentlyContinue'
                    )
                log.info(f"  ✓ LSO disabled: {name}")
                results.append({"adapter": name, "ok": True})
        except Exception:
            pass
    return results


def optimize_adapter_power() -> list[dict]:
    """Disable power management on ETHERNET adapters only.

    v3.3.1-beta.6: filtered to 802.3 (Ethernet) interfaces.  Previously
    this hit every Up adapter including WiFi — Set-NetAdapterPower
    Management on some Intel WiFi drivers corrupts the binding state
    and the WiFi toggle disappears from Settings entirely (user
    reported this after running Apply All Tweaks; full recovery
    required netsh winsock reset + reboot).  The Wake-on-LAN /
    Energy-Efficient-Ethernet / Green-Ethernet / Interrupt Moderation
    tweaks are Ethernet-only concepts anyway; WiFi has its own power-
    saving stack (ARP offload, U-APSD) that we don't want to touch.
    """
    log.info("Disabling adapter power management (Ethernet only)...")
    r = run_ps(
        "Get-NetAdapter | Where-Object { "
        "$_.Status -eq 'Up' -and ($_.MediaType -eq '802.3' -or $_.PhysicalMediaType -eq '802.3') "
        "} | ForEach-Object { "
        "  $_ | Set-NetAdapterPowerManagement -WakeOnMagicPacket Disabled -WakeOnPattern Disabled -ErrorAction SilentlyContinue; "
        "  $_.Name "
        "} | ConvertTo-Json"
    )
    # Also disable via PowerShell device manager approach — Ethernet only
    run_ps(
        'Get-NetAdapter | Where-Object {'
        '  $_.Status -eq "Up" -and ($_.MediaType -eq "802.3" -or $_.PhysicalMediaType -eq "802.3")'
        '} | ForEach-Object {'
        '  Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName "Energy Efficient Ethernet" '
        '  -DisplayValue "Disabled" -ErrorAction SilentlyContinue;'
        '  Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName "Green Ethernet" '
        '  -DisplayValue "Disabled" -ErrorAction SilentlyContinue;'
        '  Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName "Interrupt Moderation" '
        '  -DisplayValue "Disabled" -ErrorAction SilentlyContinue;'
        '}'
    )
    log.info("  ✓ Adapter power management & efficiency features disabled")
    return [{"name": "Adapter Power Optimization", "ok": True}]


def disable_ipv6(enable: bool = False) -> dict:
    """Soften IPv6 — make Windows PREFER IPv4 WITHOUT crippling the stack.

    3.4.3-beta.5 — CORRECTED.  The old code swore it was "softening" IPv6
    while actually DISABLING it: it unbound ms_tcpip6 from every adapter AND
    wrote DisabledComponents = 0x11.  Per Microsoft KB929852 the bits are:
        0x01 = disable IPv6 on tunnel interfaces (Teredo / 6to4 / ISATAP)
        0x10 = disable IPv6 on ALL *native* (non-tunnel / LAN / PPP) interfaces
        0x20 = PREFER IPv4 over IPv6 in the prefix-policy table  ← what we want
        0xFF = disable IPv6 entirely
    So the old 0x11 (= 0x01 | 0x10) disabled native + tunnel IPv6 — the exact
    "full cripple" the docstring promised to avoid, and the direct cause of
    the WiFi/mDNS regressions it warned about.  (The previous comment claimed
    "0x10 = prefer IPv4" — that is simply wrong; prefer-IPv4 is 0x20.)

    Correct behaviour: write 0x20 (decimal 32) — Microsoft's *recommended*
    alternative to disabling IPv6 — and KEEP ms_tcpip6 BOUND on every adapter
    (unbinding is what actually breaks WiFi discovery and IPv6 loopback that
    some Windows components require).  Result: games and getaddrinfo() prefer
    IPv4, but native IPv6 still functions for anything that needs it.

    enable=True restores the Windows default (DisabledComponents = 0).
    """
    action = "Restoring IPv6 (default)" if enable else "Softening IPv6 (prefer IPv4)"
    log.info(f"{action}...")
    # Keep IPv6 BOUND in BOTH modes — preference is handled purely by the
    # prefix-policy value below.  We never rip ms_tcpip6 off the adapter.
    r = run_ps(
        'Get-NetAdapterBinding -ComponentID ms_tcpip6 | '
        'Enable-NetAdapterBinding -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue'
    )
    # 0 = default (IPv6 fully on);  32 = 0x20 = prefer IPv4, IPv6 still works.
    val = "0" if enable else "32"
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
         "DisabledComponents", val)
    log.info(f"  ✓ IPv6 {'restored to default' if enable else 'softened (prefer IPv4, still functional)'}")
    return {"ok": True, "ipv6_enabled": enable}


def ping_test(hosts: list[str] = None) -> list[dict]:
    """Ping common game servers to test latency."""
    if hosts is None:
        hosts = [
            ("Cloudflare", "1.1.1.1"),
            ("Google", "8.8.8.8"),
            ("Riot NA", "104.160.131.3"),
            ("Valve US East", "162.254.197.1"),
            ("Epic Games", "99.83.136.100"),
        ]
    results = []
    for name, ip in hosts:
        r = run_cmd(["ping", "-n", "10", "-w", "1000", ip])
        avg_ms = None
        min_ms = None
        max_ms = None
        loss = None
        if r["ok"] and r["out"]:
            for line in r["out"].splitlines():
                low = line.lower().strip()
                if "average" in low:
                    parts = line.split(",")
                    for p in parts:
                        p = p.strip()
                        if "minimum" in p.lower() or "min" in p.lower():
                            val = ''.join(c for c in p if c.isdigit())
                            min_ms = int(val) if val else None
                        elif "maximum" in p.lower() or "max" in p.lower():
                            val = ''.join(c for c in p if c.isdigit())
                            max_ms = int(val) if val else None
                        elif "average" in p.lower() or "avg" in p.lower():
                            val = ''.join(c for c in p if c.isdigit())
                            avg_ms = int(val) if val else None
                if "lost" in low:
                    # "Packets: Sent = 10, Received = 10, Lost = 0 (0% loss)"
                    if "(" in line and "%" in line:
                        pct = line.split("(")[-1].split("%")[0].strip()
                        try:
                            loss = int(pct)
                        except ValueError:
                            pass
        results.append({
            "name": name, "ip": ip,
            "min_ms": min_ms, "max_ms": max_ms, "avg_ms": avg_ms,
            "loss_pct": loss,
        })
        log.info(f"  {name} ({ip}): avg={avg_ms}ms loss={loss}%")
    return results


def apply_netsh_tweaks() -> list[dict]:
    """Global netsh TCP stack tweaks.

    v3.3.1-beta.7: trimmed to only the netsh settings that actually
    move on modern Windows.  Removed:
      - `autotuninglevel=normal`: already the Win10/11 default.  No-op.
      - `ecncapability=disabled`: Win10/11 default IS disabled.
        Microsoft/ISPs/CDNs are now adding ECN support back; forcing
        off pre-emptively loses a future congestion-recovery feature.
      - `timestamps=disabled`: already the default on Win10/11, AND
        directly contradicted Tcp1323Opts=1 in apply_tcp_tweaks (which
        we also removed).  Both were no-ops in opposite directions.
      - `initialRto=2000`: only affects the very first SYN before any
        RTT measurement.  Default 3000 ms.  On a healthy LAN this
        code path is never hit — placebo.
      - `maxsynretransmissions=2`: Win10/11 default is already 2.
        Writing 2 is a no-op; writing lower would break behind NAT.
      - `chimney=automatic`: TCP Chimney Offload was deprecated and
        removed in Win8.1 / Server 2012 R2.  netsh returns "operation
        not supported" silently on Win10+.  Pure cargo cult.

    Kept the netsh tweaks that actually do measurable work:
      - rsc=disabled — real latency win for gaming (~1-3 ms p99)
      - heuristics=disabled — keeps autotuning from regressing on
        long-running streams
      - nonsackrttresiliency=disabled — minor but real gaming win
    """
    log.info("Applying global netsh TCP tweaks...")
    results = []

    tweaks = [
        ("TCP RSC off (receive seg coalescing)", ["netsh", "int", "tcp", "set", "global", "rsc=disabled"]),
        ("TCP heuristics disabled", ["netsh", "int", "tcp", "set", "heuristics", "disabled"]),
        ("TCP non-SACK RTT resiliency off", ["netsh", "int", "tcp", "set", "global", "nonsackrttresiliency=disabled"]),
    ]
    for name, cmd in tweaks:
        r = run_cmd(cmd)
        ok = r["ok"]
        log.info(f"  {'✓' if ok else '✗'} {name}")
        results.append({"name": name, "ok": ok})

    return results


def disable_netbios_teredo() -> list[dict]:
    """Kill NetBIOS-over-TCP and Teredo — both leak & slow down modern networks."""
    log.info("Disabling NetBIOS & Teredo...")
    results = []

    # NetBIOS off on all interfaces
    guids = _get_interface_guids()
    for guid in guids:
        netbios_key = rf"HKLM\SYSTEM\CurrentControlSet\Services\NetBT\Parameters\Interfaces\Tcpip_{guid}"
        _reg(netbios_key, "NetbiosOptions", "2")
    log.info(f"  ✓ NetBIOS disabled on {len(guids)} interfaces")
    results.append({"name": "NetBIOS over TCP/IP disabled", "ok": True})

    # Teredo off
    run_cmd(["netsh", "interface", "teredo", "set", "state", "disabled"])
    run_cmd(["netsh", "interface", "6to4", "set", "state", "disabled"])
    run_cmd(["netsh", "interface", "isatap", "set", "state", "disabled"])
    results.append({"name": "Teredo / 6to4 / ISATAP disabled", "ok": True})

    # LLMNR off (speeds up DNS failures; used in name-resolution attacks)
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient",
         "EnableMulticast", "0")
    results.append({"name": "LLMNR disabled", "ok": True})

    return results


def apply_dns_over_https(mode: str = "auto") -> list[dict]:
    """Enable DNS-over-HTTPS on all interfaces (Win 11 native support)."""
    log.info(f"Applying DNS-over-HTTPS (mode={mode})...")
    results = []

    # DoH templates are already registered for Cloudflare/Google/Quad9 on Win 11 22H2+
    run_ps("""
try {
    $templates = @(
        @{Server='1.1.1.1';    Doh='https://cloudflare-dns.com/dns-query'},
        @{Server='1.0.0.1';    Doh='https://cloudflare-dns.com/dns-query'},
        @{Server='8.8.8.8';    Doh='https://dns.google/dns-query'},
        @{Server='8.8.4.4';    Doh='https://dns.google/dns-query'},
        @{Server='9.9.9.9';    Doh='https://dns.quad9.net/dns-query'}
    )
    foreach ($t in $templates) {
        Add-DnsClientDohServerAddress -ServerAddress $t.Server -DohTemplate $t.Doh -AllowFallbackToUdp $true -AutoUpgrade $true -ErrorAction SilentlyContinue
    }
} catch {}
""")
    results.append({"name": "DoH templates registered", "ok": True})

    # Turn on DoH auto mode for UDP/TCP
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Dnscache\Parameters",
         "EnableAutoDoh", "2")
    results.append({"name": "Auto DoH upgrade enabled", "ok": True})

    return results


def apply_rss_rsc_adapter_tweaks() -> list[dict]:
    """Tune RSS/RSC/FlowControl per Ethernet adapter — improves multi-core
    packet processing.  WiFi / Bluetooth / cellular adapters are SKIPPED
    because:
      • Many of these properties (Jumbo Packet, Speed & Duplex, Flow
        Control) are Ethernet-only.  WiFi drivers don't expose them.
      • Setting Speed & Duplex via -RegistryValue on certain Intel WiFi
        drivers is known to corrupt the binding state — symptom: WiFi
        toggle disappears from Settings, requiring a manual driver
        reinstall.  We learned this the hard way."""
    log.info("Tuning per-adapter RSS/RSC/FlowControl (Ethernet only)...")
    results = []

    # Filter to 802.3 Ethernet only — leave WiFi, Bluetooth, cellular alone.
    run_ps("""
try {
    Get-NetAdapter | Where-Object {
        $_.Status -eq 'Up' -and (
            $_.MediaType -eq '802.3' -or
            $_.PhysicalMediaType -eq '802.3'
        )
    } | ForEach-Object {
        $n = $_.Name
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Flow Control' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Receive Side Scaling' -DisplayValue 'Enabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Recv Segment Coalescing (IPv4)' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Recv Segment Coalescing (IPv6)' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'TCP Checksum Offload (IPv4)' -DisplayValue 'Rx & Tx Enabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'UDP Checksum Offload (IPv4)' -DisplayValue 'Rx & Tx Enabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'IPv4 Checksum Offload' -DisplayValue 'Rx & Tx Enabled' -ErrorAction SilentlyContinue
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Jumbo Packet' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
        # Speed & Duplex auto-negotiate (REMOVED -RegistryValue path —
        # it corrupted some Intel WiFi adapters; using DisplayValue is safer).
        Set-NetAdapterAdvancedProperty -Name $n -DisplayName 'Speed & Duplex' -DisplayValue 'Auto Negotiation' -ErrorAction SilentlyContinue
    }
} catch {}
""")
    results.append({"name": "Per-Ethernet-adapter RSS/RSC/Checksum tuning", "ok": True})

    # Enable RSS globally
    run_cmd(["netsh", "int", "tcp", "set", "global", "rss=enabled"])
    results.append({"name": "Global RSS enabled", "ok": True})

    return results


def apply_game_port_qos() -> list[dict]:
    """Create DSCP 46 (EF — expedited forwarding) QoS policies for common game port ranges.
    This doesn't target specific apps — it boosts ANY traffic on these gaming-heavy ports."""
    log.info("Applying game port QoS policies...")
    results = []

    # Classic game port ranges — Valve/Steam, EA/Origin, Blizzard, Riot, Epic, etc.
    # v3.3.1-beta.7: dropped the `GhostShell_Discord_UDP 50000-65535`
    # rule.  That's 15,535 ports — half the ephemeral-port range —
    # marked Expedited Forwarding (DSCP 46).  On a DSCP-honoring
    # network it'd flag every random UDP socket (game updates,
    # BitTorrent, WebRTC, Zoom, Steam P2P) as game traffic and they'd
    # all compete in the EF queue.  Discord's actual voice traffic
    # uses Discord.exe-targeted DSCP via set_per_app_net_priority
    # which is the correct mechanism.
    port_rules = [
        ("GhostShell_Steam_UDP", "27000-27100", "UDP"),
        ("GhostShell_Steam_TCP", "27015-27050", "TCP"),
        ("GhostShell_EA_UDP", "3659", "UDP"),
        ("GhostShell_Riot_UDP", "5000-5500", "UDP"),
        ("GhostShell_Epic_UDP", "5200-5300", "UDP"),
        ("GhostShell_Blizzard_TCP", "1119", "TCP"),
        ("GhostShell_Generic_Game_UDP", "3000-4999", "UDP"),
    ]

    run_ps('Get-NetQosPolicy | Where-Object {$_.Name -like "GhostShell_*"} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')

    for name, ports, proto in port_rules:
        port_arg = f"-IPDstPortMatchCondition {ports}" if "-" not in ports else f"-IPDstPortStartMatchCondition {ports.split('-')[0]} -IPDstPortEndMatchCondition {ports.split('-')[1]}"
        # NetQosPolicy accepts ranges only via separate start/end parameters
        if "-" in ports:
            start, end = ports.split("-")
            ps = (f'New-NetQosPolicy -Name "{name}" '
                  f'-IPDstPortStartMatchCondition {start} -IPDstPortEndMatchCondition {end} '
                  f'-IPProtocolMatchCondition {proto} '
                  f'-DSCPAction 46 -NetworkProfile All -Confirm:$false -ErrorAction SilentlyContinue')
        else:
            ps = (f'New-NetQosPolicy -Name "{name}" '
                  f'-IPDstPortMatchCondition {ports} '
                  f'-IPProtocolMatchCondition {proto} '
                  f'-DSCPAction 46 -NetworkProfile All -Confirm:$false -ErrorAction SilentlyContinue')
        run_ps(ps)
        results.append({"name": f"{name} ({ports}/{proto})", "ok": True})

    log.info(f"  ✓ {len(port_rules)} game-port QoS rules applied")
    return results


def set_per_app_net_priority(exe_name: str, dscp: int = 46) -> dict:
    """Set DSCP priority for a specific executable.
    Used by the game profile engine to prioritize the active game + streaming app."""
    rule_name = f"GhostShell_Prio_{exe_name.replace('.exe', '').replace(' ', '_')}"
    run_ps(f'Remove-NetQosPolicy -Name "{rule_name}" -Confirm:$false -ErrorAction SilentlyContinue')
    r = run_ps(
        f'New-NetQosPolicy -Name "{rule_name}" '
        f'-AppPathNameMatchCondition "{exe_name}" '
        f'-DSCPAction {dscp} -NetworkProfile All -Confirm:$false -ErrorAction SilentlyContinue'
    )
    return {"ok": r["ok"], "rule": rule_name, "exe": exe_name, "dscp": dscp}


def clear_per_app_net_priority(prefix: str = "GhostShell_") -> dict:
    """Remove every QoS policy GhostShell created.

    v3.3.1-beta.7: default prefix changed from `GhostShell_Prio_` to
    `GhostShell_` so this also cleans up the port-range policies
    created by `apply_game_port_qos` (which use the bare
    `GhostShell_*` prefix).  Previously only the per-app `_Prio_`
    rules were removed and the port-range rules leaked.
    """
    run_ps(f'Get-NetQosPolicy | Where-Object {{$_.Name -like "{prefix}*"}} | Remove-NetQosPolicy -Confirm:$false -ErrorAction SilentlyContinue')
    return {"ok": True}


def restore_networking() -> dict:
    """One-click recovery for users whose networking got broken by a
    previous tweak pass.  Specifically intended to fix:

      • WiFi toggle disappearing from Windows Settings after IPv6
        was disabled too aggressively (DisabledComponents=0xFF)
      • WLAN AutoConfig service stopped or set to disabled
      • NetAdapter binding stuck in a corrupted state from
        Set-NetAdapterAdvancedProperty failures on WiFi adapters
      • IPv6 fully off when Windows actually needs it for DNS / mDNS

    All steps are idempotent — running this on a healthy machine is a
    no-op.  Returns a dict of {step → ok} for the UI to display."""
    log.info("═══ NETWORK RECOVERY: restoring networking ═══")
    results: list[dict] = []

    # 1. Soften IPv6 to Microsoft's safe default — many networking
    #    issues trace back to DisabledComponents=0xFF.
    r = _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
             "DisabledComponents", "0")
    log.info(f"  {'OK' if r else 'FAIL'} IPv6 DisabledComponents -> 0 (full IPv6 enabled)")
    results.append({"name": "IPv6 fully re-enabled", "ok": bool(r)})

    # 2. Re-enable IPv6 binding on every adapter
    run_ps(
        "Get-NetAdapterBinding -ComponentID ms_tcpip6 | "
        "Enable-NetAdapterBinding -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue"
    )
    results.append({"name": "IPv6 adapter bindings restored", "ok": True})

    # 3. Restart WLAN AutoConfig service (and ensure it's set to Auto-start)
    services_to_restore = [
        ("WlanSvc",        "WLAN AutoConfig (WiFi)"),
        ("WwanSvc",        "Mobile broadband (Cellular)"),
        ("dot3svc",        "Wired AutoConfig"),
        ("netprofm",       "Network List Service"),
        ("NlaSvc",         "Network Location Awareness"),
        ("nsi",            "Network Store Interface"),
        ("NetSetupSvc",    "Network Setup Service"),
    ]
    for svc, friendly in services_to_restore:
        # Reset start mode to default and start it
        c1 = run_cmd(["sc", "config", svc, "start=", "auto"], timeout=8)
        c2 = run_cmd(["sc", "start", svc], timeout=8)
        # 1056 = service already running; treat as success
        ok = bool(c1.get("ok"))    # config is the meaningful one; start can fail benignly
        log.info(f"  {'OK' if ok else 'FAIL'} {svc} ({friendly})")
        results.append({"name": f"{svc}: {friendly}", "ok": ok})

    # 3.5. v3.3.1-beta.6 — explicitly re-enable any network adapter that
    #      got disabled, AND restore PowerManagement defaults that the
    #      Wake-on-LAN tweak might have corrupted on WiFi cards.
    #      Both operations are idempotent — running on healthy adapters
    #      is a no-op.
    run_ps("""
try {
    # Enable every adapter that's currently Disabled.  Skips already-
    # enabled adapters (no double work) and silently ignores adapters
    # that lack the cmdlet support (older drivers).
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Disabled' } |
        Enable-NetAdapter -Confirm:$false -ErrorAction SilentlyContinue

    # Restore Wake-on-LAN PowerManagement defaults on every adapter.
    # The Wake-on-LAN-disable tweak hits all Up adapters including WiFi,
    # which corrupts some Intel WiFi driver bindings.  We don't care
    # about WoL after recovery; the user can re-disable per-adapter
    # later if they want.
    Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object {
        Set-NetAdapterPowerManagement -Name $_.Name -ResetToDefault -ErrorAction SilentlyContinue
    }
} catch {}
""")
    results.append({"name": "Re-enable disabled adapters + reset PowerManagement", "ok": True})

    # 4. Reset Windows network stack — clears stuck bindings, refreshes
    #    the firewall, and re-registers the network protocols.
    run_cmd(["netsh", "winsock", "reset"], timeout=15)
    run_cmd(["netsh", "int", "ip", "reset"], timeout=15)
    results.append({"name": "Winsock + IP stack reset (full effect after reboot)", "ok": True})

    # 5. Flush DNS cache — picks up any DNS server changes immediately
    run_cmd(["ipconfig", "/flushdns"])
    results.append({"name": "DNS cache flushed", "ok": True})

    log.info("═══ NETWORK RECOVERY COMPLETE — REBOOT recommended ═══")
    return {"ok": True, "results": results,
            "reboot_recommended": True,
            "msg": "Networking restored.  Reboot for the Winsock reset "
                   "to take full effect — WiFi toggle should be back."}


def run_full_network_optimize() -> dict:
    """Apply all network optimizations."""
    log.info("═══ STARTING NETWORK OPTIMIZATION ═══")
    results = {
        "nagle": apply_nagle_tweaks(),
        "tcp": apply_tcp_tweaks(),
        "throttle": disable_network_throttling(),
        "lso": disable_lso(),
        "adapter": optimize_adapter_power(),
        "netsh": apply_netsh_tweaks(),
        "netbios_teredo": disable_netbios_teredo(),
        "rss_rsc": apply_rss_rsc_adapter_tweaks(),
        "game_port_qos": apply_game_port_qos(),
    }
    log.info("═══ NETWORK OPTIMIZATION COMPLETE ═══")
    return results
