from typing import List, Tuple, Dict
from pathlib import Path
import os

from core.utils import (
    reg_set,
    reg_delete_value,
    reg_create_key,
    backup_file,
    safe_write_text,
    run_powershell,
    log_event,
    create_system_restore_point,
)
from config import APP_NAME


HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
EDGE_PATHS = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\ Program Files".replace(" ", "")))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
]


def get_registry_plan() -> Dict[str, List[Tuple[str, str, str, object]]]:
    plan: Dict[str, List[Tuple[str, str, str, object]]] = {
        "telemetry": [
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0),
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowDeviceNameInTelemetry", 0),
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "DisableEnterpriseAuthProxy", 1),
            ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection", "AllowTelemetry", 0),
        ],
        "ads": [
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", 0),
        ],
        "activity": [
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "EnableActivityFeed", 0),
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "PublishUserActivities", 0),
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "UploadUserActivities", 0),
        ],
        "speech_inking": [
            ("HKCU", r"Software\Microsoft\Speech_OneCore\Settings\SpeechPrivacy", "Consent", 0),
            ("HKCU", r"Software\Microsoft\InputPersonalization", "RestrictImplicitTextCollection", 1),
            ("HKCU", r"Software\Microsoft\InputPersonalization", "RestrictImplicitInkCollection", 1),
        ],
        "tailored": [
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled", 0),
        ],
        "location": [
            ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors", "DisableLocation", 1),
        ],
        "find_device": [
            ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Device Location", "DisableIfUserNotSignedIn", 1),
        ],
        "clipboard": [
            ("HKCU", r"Software\Microsoft\Clipboard", "EnableClipboardHistory", 1),
            ("HKCU", r"Software\Microsoft\Clipboard", "EnableCloudClipboard", 0),
        ],
        "privacy_ui": [
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338389Enabled", 0),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-353698Enabled", 0),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-353696Enabled", 0),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "Start_NotifyNewApps", 0),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "EnableBalloonTips", 0),
        ],
    }
    return plan


def apply_registry(plan: Dict[str, List[Tuple[str, str, str, object]]] | None = None) -> List[Tuple[str, str, bool, str]]:
    results: List[Tuple[str, str, bool, str]] = []
    reg_plan = plan or get_registry_plan()
    for category, items in reg_plan.items():
        for root, path, name, value in items:
            try:
                typ = "REG_DWORD" if isinstance(value, int) else "REG_SZ"
                reg_set(root, path, name, value, typ)
                results.append((category, f"{root}\\{path}::{name}", True, ""))
            except Exception as exc:
                log_event("privacy", f"Registry set failed: {root} {path} {name}: {exc}")
                results.append((category, f"{root}\\{path}::{name}", False, str(exc)))
    return results


def update_hosts(domains: List[str]) -> Tuple[bool, str, int]:
    try:
        backup_file(HOSTS_PATH)
        existing = set()
        if HOSTS_PATH.exists():
            content = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in content:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split()
                    if len(parts) >= 2:
                        existing.add(parts[1].lower())
        to_add = []
        for d in domains:
            d = d.strip().lower()
            if d and d not in existing:
                to_add.append(f"0.0.0.0 {d}")
        if to_add:
            new_content = (HOSTS_PATH.read_text(encoding="utf-8", errors="ignore") if HOSTS_PATH.exists() else "")
            new_content += "\n# GhostShell telemetry blocks\n" + "\n".join(to_add) + "\n"
            safe_write_text(HOSTS_PATH, new_content)
        log_event("privacy", f"Hosts updated: +{len(to_add)} entries")
        return True, "Hosts updated", len(to_add)
    except Exception as exc:
        log_event("privacy", f"Hosts update failed: {exc}")
        return False, str(exc), 0


def add_firewall_blocks(executables: List[str]) -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []
    for exe in executables:
        try:
            name = f"{APP_NAME} Block {Path(exe).name}"
            ps = (
                f"$e=\"{exe}\"; if(Test-Path $e){{ New-NetFirewallRule -DisplayName \"{name}\" -Direction Outbound -Action Block -Program $e -Enabled True -Profile Any -EdgeTraversalPolicy Block -ErrorAction SilentlyContinue | Out-Null; Write-Output 'OK' }} else {{ Write-Output 'MISSING' }}"
            )
            code, out, err = run_powershell(ps)
            ok = code == 0 and "OK" in (out or "")
            msg = (err or out or "").strip()
            results.append((exe, ok, msg))
        except Exception as exc:
            results.append((exe, False, str(exc)))
    return results


def set_windows_update(mode: str = "notify") -> Tuple[bool, str]:
    try:
        # Notify only mode
        reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "AUOptions", 2, "REG_DWORD")
        # Disable driver updates via WU
        reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "ExcludeWUDriversInQualityUpdate", 1, "REG_DWORD")
        # Defer feature updates
        reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "DeferFeatureUpdates", 1, "REG_DWORD")
        reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "DeferFeatureUpdatesPeriodInDays", 365, "REG_DWORD")
        log_event("privacy", "Windows Update set to notify-only")
        return True, "Configured Windows Update policies"
    except Exception as exc:
        log_event("privacy", f"Windows Update policy failed: {exc}")
        return False, str(exc)


def set_webcam_microphone(disable_webcam: bool, disable_mic: bool) -> Tuple[bool, str]:
    try:
        val_cam = "Deny" if disable_webcam else "Allow"
        val_mic = "Deny" if disable_mic else "Allow"
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
        reg_set("HKLM", f"{base}\\webcam", "Value", val_cam, "REG_SZ")
        reg_set("HKLM", f"{base}\\microphone", "Value", val_mic, "REG_SZ")
        log_event("privacy", f"Webcam/Mic set: webcam={val_cam} mic={val_mic}")
        return True, "Device privacy updated"
    except Exception as exc:
        log_event("privacy", f"Device privacy failed: {exc}")
        return False, str(exc)


def adjust_edge_privacy() -> Tuple[bool, str]:
    try:
        if not any(p.exists() for p in EDGE_PATHS):
            return True, "Edge not found"
        base = r"SOFTWARE\Policies\Microsoft\Edge"
        reg_set("HKLM", base, "MetricsReportingEnabled", 0, "REG_DWORD")
        reg_set("HKLM", base, "UserFeedbackAllowed", 0, "REG_DWORD")
        reg_set("HKLM", base, "SmartScreenEnabled", 0, "REG_DWORD")
        reg_set("HKLM", base, "PersonalizationReportingEnabled", 0, "REG_DWORD")
        log_event("privacy", "Edge privacy policies applied")
        return True, "Edge privacy hardened"
    except Exception as exc:
        log_event("privacy", f"Edge privacy failed: {exc}")
        return False, str(exc)


def _telemetry_domains() -> List[str]:
    return [
        "vortex.data.microsoft.com",
        "vortex-win.data.microsoft.com",
        "telecommand.telemetry.microsoft.com",
        "telecommand.telemetry.microsoft.com.nsatc.net",
        "oca.telemetry.microsoft.com",
        "sqm.telemetry.microsoft.com",
        "watson.telemetry.microsoft.com",
        "redir.metaservices.microsoft.com",
        "choice.microsoft.com",
        "choice.microsoft.com.nsatc.net",
        "df.telemetry.microsoft.com",
        "reports.wes.df.telemetry.microsoft.com",
        "wes.df.telemetry.microsoft.com",
        "services.wes.df.telemetry.microsoft.com",
        "sqm.df.telemetry.microsoft.com",
        "telemetry.microsoft.com",
        "watson.ppe.telemetry.microsoft.com",
        "telemetry.appex.bing.net",
        "telemetry.urs.microsoft.com",
        "settings-sandbox.data.microsoft.com",
        "survey.watson.microsoft.com",
        "watson.live.com",
        "statsfe2.ws.microsoft.com",
        "corpext.msitadfs.glbdns2.microsoft.com",
        "compatexchange.cloudapp.net",
        "a-0001.a-msedge.net",
        "statsfe2.update.microsoft.com.akadns.net",
        "fe2.update.microsoft.com.akadns.net",
        "sls.update.microsoft.com",
        "arc.msn.com",
    ]


def _firewall_executables() -> List[str]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\\Windows"))
    return [
        str(system_root / "System32" / "CompatTelRunner.exe"),
        str(system_root / "System32" / "DeviceCensus.exe"),
        str(system_root / "System32" / "DiagTrackRunner.exe"),
        str(system_root / "System32" / "backgroundTaskHost.exe"),
        str(system_root / "SystemApps" / "Microsoft.Windows.Search_cw5n1h2txyewy" / "SearchUI.exe"),
    ]


def _normalize_restore_point_result(result: object) -> Tuple[bool, str]:
    if isinstance(result, tuple):
        if len(result) == 0:
            return False, ""
        if len(result) == 1:
            return bool(result[0]), ""
        if len(result) >= 2:
            return bool(result[0]), str(result[1])
    return bool(result), ""


def harden_all() -> Dict[str, object]:
    summary: Dict[str, object] = {}
    try:
        rp_result = create_system_restore_point(f"{APP_NAME} Privacy Hardening")
    except Exception as exc:
        rp_result = (False, f"Failed to create restore point: {exc}")
        log_event("privacy", str(rp_result))
    rp_success, rp_message = _normalize_restore_point_result(rp_result)
    summary["restore_point"] = {"success": rp_success, "message": rp_message}

    registry_results = apply_registry()
    registry_success_count = sum(1 for _, _, success, _ in registry_results if success)
    summary["registry"] = {
        "total": len(registry_results),
        "success": registry_success_count,
        "results": registry_results,
    }

    hosts_status = update_hosts(_telemetry_domains())
    summary["hosts"] = {"status": hosts_status[0], "message": hosts_status[1], "added": hosts_status[2]}

    firewall_results = add_firewall_blocks(_firewall_executables())
    summary["firewall"] = firewall_results

    wu_status = set_windows_update("notify")
    summary["windows_update"] = {"success": wu_status[0], "message": wu_status[1]}

    devices_status = set_webcam_microphone(disable_webcam=True, disable_mic=True)
    summary["devices"] = {"success": devices_status[0], "message": devices_status[1]}

    edge_status = adjust_edge_privacy()
    summary["edge"] = {"success": edge_status[0], "message": edge_status[1]}

    log_event("privacy", "Harden all complete")
    return summary
