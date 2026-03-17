from typing import List, Tuple, Dict, Callable
from core.utils import reg_set, run_powershell, log_event, list_adapters, set_dns_all, flush_dns_cache, ping


def _run_ps(cmd: str) -> Tuple[bool, str]:
    try:
        code, out, err = run_powershell(cmd)
        return (code == 0), (err or out or "").strip()
    except Exception as exc:
        return False, str(exc)


_TWEAK_CATALOG: List[Dict[str, str]] = [
    {
        "key": "disable_nagle",
        "title": "Disable Nagle's Algorithm",
        "description": "Sets TcpAckFrequency and TCPNoDelay to 1 for all adapters to reduce latency.",
        "category": "TCP/IP",
    },
    {
        "key": "tcp_params",
        "title": "Optimize TCP Parameters",
        "description": "Tunes TcpTimedWaitDelay, MaxUserPort, and TcpMaxDataRetransmissions for better throughput.",
        "category": "TCP/IP",
    },
    {
        "key": "network_throttling",
        "title": "Disable Network Throttling",
        "description": "Removes multimedia network throttling limits.",
        "category": "System",
    },
    {
        "key": "system_responsiveness",
        "title": "Optimize System Responsiveness",
        "description": "Sets SystemResponsiveness to 0 for real-time networking.",
        "category": "System",
    },
    {
        "key": "disable_lso",
        "title": "Disable Large Send Offload",
        "description": "Turns off LSO on all network adapters.",
        "category": "Adapter",
    },
    {
        "key": "disable_ipv6",
        "title": "Disable IPv6 Stack",
        "description": "Disables IPv6 via DisabledComponents registry flag.",
        "category": "TCP/IP",
    },
    {
        "key": "disable_qos",
        "title": "Disable QoS Packet Scheduler",
        "description": "Disables Psched service to avoid QoS throttling.",
        "category": "System",
    },
    {
        "key": "wifi_sense",
        "title": "Disable Wi-Fi Sense",
        "description": "Turns off Wi-Fi Sense related features.",
        "category": "Wireless",
    },
    {
        "key": "adapter_power",
        "title": "Disable Adapter Power Saving",
        "description": "Disables NIC power saving features via registry.",
        "category": "Adapter",
    },
    {
        "key": "interrupt_moderation_off",
        "title": "Disable Interrupt Moderation",
        "description": "Turns off interrupt moderation on all adapters.",
        "category": "Adapter",
    },
]


def _apply_disable_nagle() -> Tuple[bool, str]:
    command = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-Item 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces' |"
        " Get-ChildItem | ForEach-Object { "
        " New-ItemProperty -Path $_.PSPath -Name 'TcpAckFrequency' -Value 1 -PropertyType DWord -Force | Out-Null;"
        " New-ItemProperty -Path $_.PSPath -Name 'TcpNoDelay' -Value 1 -PropertyType DWord -Force | Out-Null;"
        " }"
    )
    return _run_ps(command)


def _apply_tcp_params() -> Tuple[bool, str]:
    command = (
        "$ErrorActionPreference='SilentlyContinue';"
        "New-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Force | Out-Null;"
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'TcpTimedWaitDelay' -Value 30 -Type DWord;"
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'MaxUserPort' -Value 65534 -Type DWord;"
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'TcpMaxDataRetransmissions' -Value 5 -Type DWord;"
    )
    return _run_ps(command)


def _apply_network_throttling() -> Tuple[bool, str]:
    command = (
        "$ErrorActionPreference='SilentlyContinue';"
        "New-Item -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile' -Force | Out-Null;"
        "Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile' -Name 'NetworkThrottlingIndex' -Value 0xffffffff -Type DWord;"
        "Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile' -Name 'SystemResponsiveness' -Value 0 -Type DWord;"
    )
    return _run_ps(command)


def _apply_system_responsiveness() -> Tuple[bool, str]:
    command = (
        "Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile' -Name 'SystemResponsiveness' -Value 0 -Type DWord"
    )
    return _run_ps(command)


def _apply_disable_lso() -> Tuple[bool, str]:
    command = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetAdapter | ForEach-Object { "
        " Disable-NetAdapterLso -Name $_.Name -IPv4 -NoRestart -ErrorAction SilentlyContinue;"
        " Disable-NetAdapterLso -Name $_.Name -IPv6 -NoRestart -ErrorAction SilentlyContinue;"
        "}"
    )
    return _run_ps(command)


def _apply_disable_ipv6() -> Tuple[bool, str]:
    command = (
        "New-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters' -Force | Out-Null;"
        "Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters' -Name 'DisabledComponents' -Value 0xff -Type DWord"
    )
    return _run_ps(command)


def _apply_disable_qos() -> Tuple[bool, str]:
    command = (
        "Set-Service -Name 'Psched' -StartupType Disabled;"
        "Stop-Service -Name 'Psched' -ErrorAction SilentlyContinue"
    )
    return _run_ps(command)


def _apply_wifi_sense() -> Tuple[bool, str]:
    try:
        reg_set("HKLM", r"SOFTWARE\Microsoft\PolicyManager\default\WiFi\AllowWiFiHotSpotReporting", "value", 0, "REG_DWORD")
        reg_set("HKLM", r"SOFTWARE\Microsoft\PolicyManager\default\WiFi\AllowAutoConnectToWiFiSenseHotspots", "value", 0, "REG_DWORD")
        return True, "Wi-Fi Sense disabled"
    except Exception as exc:
        return False, str(exc)


def _apply_adapter_power() -> Tuple[bool, str]:
    # {4d36e972-e325-11ce-bfc1-08002be10318}
    try:
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
        count = 0
        for i in range(0, 64):
            sub = f"{i:04d}"
            path = f"{base}\{sub}"
            try:
                reg_set("HKLM", path, "PnPCapabilities", 0x24, "REG_DWORD")
                count += 1
            except Exception:
                continue
        if count:
            return True, f"Power management disabled on {count} adapters"
        return False, "No adapter class entries updated"
    except Exception as exc:
        return False, f"Failed to update adapter power settings: {exc}"


def _apply_interrupt_moderation() -> Tuple[bool, str]:
    command = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetAdapter | ForEach-Object { "
        "Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName 'Interrupt Moderation' "
        "-DisplayValue Disabled -NoRestart -ErrorAction SilentlyContinue; "
        "}"
    )
    success, output = _run_ps(command)
    if success:
        return True, "Interrupt moderation disabled for all adapters."
    return False, f"Failed to disable interrupt moderation: {output}"


_TWEAK_FUNCTIONS: Dict[str, Callable[[], Tuple[bool, str]]] = {
    "disable_nagle": _apply_disable_nagle,
    "tcp_params": _apply_tcp_params,
    "network_throttling": _apply_network_throttling,
    "system_responsiveness": _apply_system_responsiveness,
    "disable_lso": _apply_disable_lso,
    "disable_ipv6": _apply_disable_ipv6,
    "disable_qos": _apply_disable_qos,
    "wifi_sense": _apply_wifi_sense,
    "adapter_power": _apply_adapter_power,
    "interrupt_moderation_off": _apply_interrupt_moderation,
}
