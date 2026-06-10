"""Privacy hardening — telemetry, hosts blocking, firewall, data collection."""
import os
import shutil
from datetime import datetime
from config import TELEMETRY_HOSTS, FIREWALL_BLOCK_EXES
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("privacy")

HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_MARKER = "# === GhostShell Telemetry Block ==="


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry & Data Collection
# ═══════════════════════════════════════════════════════════════════════════
def apply_telemetry_tweaks() -> list[dict]:
    """Disable all Windows telemetry and data collection."""
    log.info("Disabling telemetry & data collection...")
    results = []

    backup_registry(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "telemetry_policy")
    backup_registry(r"HKCU\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "adv_info")

    tweaks = [
        # Telemetry level to Security (0)
        ("Telemetry: Security Only",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", "0"),
        ("Telemetry: Max Level",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "MaxTelemetryAllowed", "0"),
        # Disable diagnostic data
        ("Disable Diagnostic Data",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Diagnostics\DiagTrack", "ShowedToastAtLevel", "1"),
        ("Diagnostic Data Opt-Out",
         r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection", "AllowTelemetry", "0"),
        # Disable advertising ID
        ("Disable Advertising ID",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", "0"),
        ("Disable Ad Info Policy",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo", "DisabledByGroupPolicy", "1"),
        # Disable activity history
        ("Disable Activity History",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "EnableActivityFeed", "0"),
        ("Disable Activity Upload",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "UploadUserActivities", "0"),
        ("Disable Activity Publish",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "PublishUserActivities", "0"),
        # Disable online speech recognition
        ("Disable Speech Recognition",
         r"HKCU\Software\Microsoft\Speech_OneCore\Settings\OnlineSpeechPrivacy", "HasAccepted", "0"),
        ("Disable Speech Policy",
         r"HKLM\SOFTWARE\Policies\Microsoft\InputPersonalization", "AllowInputPersonalization", "0"),
        # Disable inking & typing personalization
        ("Disable Inking Personalization",
         r"HKCU\Software\Microsoft\InputPersonalization", "RestrictImplicitInkCollection", "1"),
        ("Disable Typing Personalization",
         r"HKCU\Software\Microsoft\InputPersonalization", "RestrictImplicitTextCollection", "1"),
        ("Disable Personalization Trained",
         r"HKCU\Software\Microsoft\InputPersonalization\TrainedDataStore", "HarvestContacts", "0"),
        # Disable tailored experiences
        ("Disable Tailored Experiences",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled", "0"),
        # Disable location tracking
        ("Disable Location",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors", "DisableLocation", "1"),
        ("Disable Location Consent",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location", "Value", "Deny"),
        # Disable Find My Device
        ("Disable Find My Device",
         r"HKLM\SOFTWARE\Policies\Microsoft\FindMyDevice", "AllowFindMyDevice", "0"),
        # Disable cloud clipboard sync
        ("Disable Cloud Clipboard",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "AllowClipboardHistory", "0"),
        ("Disable Clipboard Sync",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "AllowCrossDeviceClipboard", "0"),
        # Disable app launch tracking
        ("Disable App Launch Tracking",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "Start_TrackProcs", "0"),
        # Disable settings sync
        ("Disable Settings Sync",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\SettingSync", "DisableSettingSync", "2"),
        # Disable Wi-Fi Sense
        ("Disable Wi-Fi Sense",
         r"HKLM\SOFTWARE\Microsoft\WcmSvc\wifinetworkmanager\config", "AutoConnectAllowedOEM", "0"),
        # Disable SmartScreen sending data
        ("Disable SmartScreen Data",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "EnableSmartScreen", "0"),
        # Disable error reporting
        ("Disable Error Reporting",
         r"HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting", "Disabled", "1"),
        # Disable customer experience
        ("Disable CEIP",
         r"HKLM\SOFTWARE\Policies\Microsoft\SQMClient\Windows", "CEIPEnable", "0"),
    ]

    for name, key, val, data in tweaks:
        vtype = "REG_SZ" if data in ("Deny",) else "REG_DWORD"
        ok = _reg(key, val, data, vtype)
        log.info(f"  {'✓' if ok else '✗'} {name}")
        results.append({"name": name, "ok": ok})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Hosts File Blocking
# ═══════════════════════════════════════════════════════════════════════════
def get_hosts_status() -> dict:
    """Check current hosts file blocking status."""
    blocked_count = 0
    ghostshell_active = False
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if HOSTS_MARKER in content:
            ghostshell_active = True
            blocked_count = content.count("0.0.0.0 ")
    except Exception:
        pass
    return {"active": ghostshell_active, "blocked_count": blocked_count}


def apply_hosts_blocking() -> dict:
    """Block telemetry domains via hosts file."""
    log.info("Applying hosts file telemetry blocking...")

    # Backup hosts file
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(os.environ.get("APPDATA", ""), "GhostShell", "backups", f"hosts_{ts}.bak")
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.copy2(HOSTS_PATH, backup)
        log.info(f"  ✓ Hosts backup: {backup}")
    except Exception as e:
        log.warning(f"  ⚠ Hosts backup failed: {e}")

    # Read current hosts
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        content = ""

    # Remove existing GhostShell block if present
    if HOSTS_MARKER in content:
        parts = content.split(HOSTS_MARKER)
        content = parts[0].rstrip() + "\n"

    # Append new block
    block = f"\n{HOSTS_MARKER}\n"
    block += f"# Added by GhostShell on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    block += f"# {len(TELEMETRY_HOSTS)} domains blocked\n"
    for host in TELEMETRY_HOSTS:
        block += f"0.0.0.0 {host}\n"
    block += f"{HOSTS_MARKER}\n"

    try:
        with open(HOSTS_PATH, "w", encoding="utf-8") as f:
            f.write(content + block)
        log.info(f"  ✓ Blocked {len(TELEMETRY_HOSTS)} telemetry domains")
        # Flush DNS
        run_cmd(["ipconfig", "/flushdns"])
        return {"ok": True, "count": len(TELEMETRY_HOSTS)}
    except PermissionError:
        log.error("  ✗ Permission denied writing hosts file (need admin)")
        return {"ok": False, "err": "Permission denied — run as administrator"}
    except Exception as e:
        log.error(f"  ✗ Hosts file error: {e}")
        return {"ok": False, "err": str(e)}


def remove_hosts_blocking() -> dict:
    """Remove GhostShell entries from hosts file."""
    log.info("Removing hosts file blocking...")
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if HOSTS_MARKER in content:
            parts = content.split(HOSTS_MARKER)
            content = parts[0].rstrip() + "\n"
            if len(parts) > 2:
                content += parts[2]  # keep anything after second marker
            with open(HOSTS_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            run_cmd(["ipconfig", "/flushdns"])
            log.info("  ✓ Hosts blocking removed")
            return {"ok": True}
        log.info("  ⚠ No GhostShell entries found in hosts")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Firewall Rules
# ═══════════════════════════════════════════════════════════════════════════
def apply_firewall_rules() -> list[dict]:
    """Create outbound firewall rules to block telemetry executables."""
    log.info("Creating firewall rules...")
    results = []

    for exe in FIREWALL_BLOCK_EXES:
        name_part = os.path.basename(exe).replace(".exe", "")
        rule_name = f"GhostShell_Block_{name_part}"

        # Remove existing rule first
        run_ps(f'Remove-NetFirewallRule -DisplayName "{rule_name}" -ErrorAction SilentlyContinue')

        r = run_ps(
            f'New-NetFirewallRule -DisplayName "{rule_name}" -Direction Outbound '
            f'-Action Block -Program "{exe}" -Enabled True -Profile Any '
            f'-Description "GhostShell: Block telemetry" -ErrorAction Stop'
        )
        ok = r["ok"]
        log.info(f"  {'✓' if ok else '✗'} Block outbound: {name_part}")
        results.append({"name": name_part, "exe": exe, "ok": ok})

    return results


def remove_firewall_rules() -> dict:
    """Remove all GhostShell firewall rules."""
    log.info("Removing GhostShell firewall rules...")
    r = run_ps('Get-NetFirewallRule -DisplayName "GhostShell_Block_*" | Remove-NetFirewallRule')
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# Webcam Control
# ═══════════════════════════════════════════════════════════════════════════
# NOTE: Microphone toggle was REMOVED in v3.1 — flipping the global mic
# capability gate broke UWP Discord / Teams / in-game voice for users.
# Mic privacy is now strictly managed by the user via Windows Settings.
def get_device_access_status() -> dict:
    """Check global webcam access status."""
    cam_key = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
    cam_r = run_cmd(["reg", "query", cam_key, "/v", "Value"])
    cam_on = "Allow" in cam_r.get("out", "")
    return {"webcam_enabled": cam_on}


def toggle_webcam(enable: bool) -> dict:
    val = "Allow" if enable else "Deny"
    ok = _reg(
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam",
        "Value", val, "REG_SZ"
    )
    log.info(f"  {'✓' if ok else '✗'} Webcam {'enabled' if enable else 'disabled'}")
    return {"ok": ok, "enabled": enable}


def restore_store_updates() -> dict:
    """Idempotent v3.1 fix.  Reverts every registry / service change
    GhostShell ever made that prevents the Microsoft Store / Xbox app
    from updating.  Called from app boot so users don't have to find
    a button.  Safe to call repeatedly."""
    log.info("Restoring Microsoft Store / Xbox app updates (v3.1 fix)...")
    actions = []

    # 1. DiagTrack — was set to disabled by apply_telemetry_tweaks; the
    #    Store and Xbox app need it to talk to Microsoft's content servers.
    #    Set to manual so it idle/cold most of the time but can start when
    #    Store/Xbox actually needs it.
    r1 = run_cmd(["sc", "config", "DiagTrack", "start=", "manual"])
    actions.append(("DiagTrack → manual", r1.get("ok", False)))
    # Don't auto-START — Store will start it on demand.  But if it's
    # currently stopped, that's fine; first Store update click will warm it.

    # 2. LetAppsRunInBackground — the Store needs to run its updater in the
    #    background, so delete the policy that denied it.
    r2 = run_cmd(["reg", "delete",
                  r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                  "/v", "LetAppsRunInBackground", "/f"])
    actions.append(("Deleted LetAppsRunInBackground policy", r2.get("ok", False)))

    # 3. LetAppsGetDiagnosticInfo — Store/Xbox use this for update telemetry
    r3 = run_cmd(["reg", "delete",
                  r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                  "/v", "LetAppsGetDiagnosticInfo", "/f"])
    actions.append(("Deleted LetAppsGetDiagnosticInfo policy", r3.get("ok", False)))

    # 4. RemoveWindowsStore — sometimes set by aggressive privacy tools.
    #    Delete it just in case.
    for hive in ("HKLM", "HKCU"):
        rR = run_cmd(["reg", "delete",
                      f"{hive}\\SOFTWARE\\Policies\\Microsoft\\WindowsStore",
                      "/v", "RemoveWindowsStore", "/f"])
        if rR.get("ok"):
            actions.append((f"Deleted {hive} RemoveWindowsStore", True))

    # 5. AppX deployment service — Store can't install/update without this
    rA = run_cmd(["sc", "config", "AppXSvc", "start=", "manual"])
    actions.append(("AppXSvc → manual", rA.get("ok", False)))

    # 6. Microsoft Store Install Service — directly responsible for Store
    #    package updates
    rI = run_cmd(["sc", "config", "InstallService", "start=", "auto"])
    actions.append(("InstallService → auto", rI.get("ok", False)))

    log.info(f"  Store/Xbox update restore: {actions}")
    return {"ok": True, "actions": actions}


def restore_microphone_access() -> dict:
    """Idempotent v3.1 fix.  Reverts every registry path GhostShell ever
    touched that could have killed mic access on existing installs.

    Called from app boot so users don't have to find a button.
    Safe to call repeatedly — never blocks the mic, only ever permits it."""
    log.info("Restoring microphone access (v3.1 fix)...")
    actions = []

    # 1. Global per-user consent gate
    ok = _reg(
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone",
        "Value", "Allow", "REG_SZ"
    )
    actions.append(("ConsentStore Allow", ok))

    # 2. AppPrivacy policy that v3.0 set to Deny — delete it so apps fall
    #    back to user-controlled defaults
    r = run_cmd(["reg", "delete",
                 r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                 "/v", "LetAppsAccessMicrophone", "/f"])
    actions.append(("Deleted LetAppsAccessMicrophone policy", r.get("ok", False)))

    # 3. Same value sometimes lands in HKCU — sweep it too
    r2 = run_cmd(["reg", "delete",
                  r"HKCU\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                  "/v", "LetAppsAccessMicrophone", "/f"])
    actions.append(("Deleted HKCU LetAppsAccessMicrophone policy", r2.get("ok", False)))

    log.info(f"  Microphone access restore: {actions}")
    return {"ok": True, "actions": actions}


# ═══════════════════════════════════════════════════════════════════════════
# Windows Update Control
# ═══════════════════════════════════════════════════════════════════════════
def apply_update_tweaks() -> list[dict]:
    """Configure Windows Update to be less aggressive."""
    log.info("Configuring Windows Update...")
    results = []
    backup_registry(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "win_update")

    tweaks = [
        # Disable driver updates via WU
        ("Block Driver Updates via WU",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "ExcludeWUDriversInQualityUpdate", "1"),
        # Disable feature updates for 365 days
        ("Defer Feature Updates 365d",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "DeferFeatureUpdates", "1"),
        ("Feature Update Deferral Days",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "DeferFeatureUpdatesPeriodInDays", "365"),
        # Disable auto-restart
        ("Disable Auto Restart",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "NoAutoRebootWithLoggedOnUsers", "1"),
        # Notify before download
        ("Notify Before Download",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", "AUOptions", "2"),
        # Set wide active hours
        ("Active Hours Start",
         r"HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", "ActiveHoursStart", "6"),
        ("Active Hours End",
         r"HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", "ActiveHoursEnd", "23"),
    ]

    for name, key, val, data in tweaks:
        ok = _reg(key, val, data)
        log.info(f"  {'✓' if ok else '✗'} {name}")
        results.append({"name": name, "ok": ok})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Full privacy harden
# ═══════════════════════════════════════════════════════════════════════════
def run_full_privacy() -> dict:
    """Run all privacy hardening."""
    log.info("═══ STARTING PRIVACY HARDENING ═══")
    results = {
        "telemetry": apply_telemetry_tweaks(),
        "hosts": apply_hosts_blocking(),
        "firewall": apply_firewall_rules(),
        "devices": get_device_access_status(),
        "updates": apply_update_tweaks(),
    }
    log.info("═══ PRIVACY HARDENING COMPLETE ═══")
    return results
