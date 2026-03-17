from typing import List, Dict, Tuple
from core.utils import run_powershell, reg_set, service_set, scheduled_task_disable, log_event, create_system_restore_point
from pathlib import Path
import os

MODULE_NAME = "ghostshell.core.debloater"

DEFAULT_APPS: List[str] = [
    "Microsoft.BingNews",
    "Microsoft.BingWeather",
    "Microsoft.BingFinance",
    "Microsoft.BingSports",
    "Microsoft.GamingApp",
    "Microsoft.XboxGameOverlay",
    "Microsoft.XboxGamingOverlay",
    "Microsoft.XboxIdentityProvider",
    "Microsoft.XboxSpeechToTextOverlay",
    "Microsoft.Xbox.TCUI",
    "Microsoft.GetHelp",
    "Microsoft.Getstarted",
    "Microsoft.MicrosoftOfficeHub",
    "Microsoft.Office.OneNote",
    "Microsoft.MicrosoftSolitaireCollection",
    "Microsoft.MicrosoftStickyNotes",
    "Microsoft.MixedReality.Portal",
    "Microsoft.People",
    "Microsoft.PowerAutomateDesktop",
    "Microsoft.ScreenSketch",
    "Microsoft.SkypeApp",
    "Microsoft.StorePurchaseApp",
    "Microsoft.Todos",
    "Microsoft.WindowsAlarms",
    "Microsoft.WindowsCamera",
    "Microsoft.WindowsCommunicationsApps",
    "Microsoft.WindowsFeedbackHub",
    "Microsoft.WindowsMaps",
    "Microsoft.WindowsSoundRecorder",
    "Microsoft.YourPhone",
    "Microsoft.WindowsPhone",
    "Microsoft.ZuneMusic",
    "Microsoft.ZuneVideo",
    "Clipchamp.Clipchamp",
    "Disney.37853FC22B2CE",
    "SpotifyAB.SpotifyMusic",
    "BytedancePte.Ltd.TikTok",
    "Microsoft.549981C3F5F10",
    "MicrosoftCorporationII.QuickAssist",
    "Microsoft.OneDrive",
    "Microsoft.Edge",
    "THIRD_PARTY_BLOAT",
]

DEFAULT_SERVICES: List[str] = [
    "DiagTrack",
    "dmwappushservice",
    "SysMain",
    "WSearch",
    "MapsBroker",
    "lfsvc",
    "RetailDemo",
    "wisvc",
    "WerSvc",
]

DEFAULT_TASKS: List[str] = [
    r"\Microsoft\Windows\Application Experience\*",
    r"\Microsoft\Windows\Customer Experience Improvement Program\*",
    r"\Microsoft\Windows\Feedback\*",
    r"\Microsoft\Windows\Windows Error Reporting\*",
]

DEFAULT_FEATURES: List[str] = [
    "Disable Copilot",
    "Disable Widgets",
    "Disable Chat",
    "Disable Cortana",
    "Disable Search Highlights",
    "Disable Suggested Content",
    "Disable Tips",
    "Remove Web Experience Pack",
]


def get_default_plan() -> Dict[str, List[str]]:
    return {
        "apps": DEFAULT_APPS[:],
        "services": DEFAULT_SERVICES[:],
        "tasks": DEFAULT_TASKS[:],
        "features": DEFAULT_FEATURES[:],
    }


def debloat(selected: Dict[str, List[str]]) -> List[Tuple[str, str, bool, str]]:
    results: List[Tuple[str, str, bool, str]] = []

    # Create restore point
    try:
        ok = create_system_restore_point("GhostShell Debloat")
        results.append(("system", "RestorePoint", bool(ok), ""))
    except Exception as exc:
        results.append(("system", "RestorePoint", False, str(exc)))

    # Apps
    for app in selected.get("apps", []):
        success, message = _remove_app(app)
        results.append(("apps", app, success, message))

    # Services
    for svc in selected.get("services", []):
        try:
            ok, msg = service_set(svc, start_type="disabled", action="stop")
            results.append(("services", svc, ok, msg))
        except Exception as exc:
            results.append(("services", svc, False, str(exc)))

    # Tasks
    for task in selected.get("tasks", []):
        try:
            # Expand wildcard patterns using PowerShell
            if "*" in task:
                ps = f"Get-ScheduledTask -TaskPath \'{task.rsplit('\\\\',1)[0]}\\\\' | Select-Object -ExpandProperty TaskName"
                code, stdout, stderr = run_powershell(ps)
                if code == 0 and stdout:
                    for name in stdout.splitlines():
                        full = task.rsplit("\\", 1)[0] + "\\" + name.strip()
                        ok, msg = scheduled_task_disable(full)
                        results.append(("tasks", full, ok, msg))
                    continue
            ok, msg = scheduled_task_disable(task)
            results.append(("tasks", task, ok, msg))
        except Exception as exc:
            results.append(("tasks", task, False, str(exc)))

    # Features
    for feat in selected.get("features", []):
        success, message = _apply_feature(feat)
        results.append(("features", feat, success, message))

    return results


def _remove_app(name: str) -> Tuple[bool, str]:
    try:
        if name == "Microsoft.OneDrive":
            # Attempt winget
            ps = (
                "winget uninstall --id Microsoft.OneDrive --silent --accept-source-agreements --accept-package-agreements"
            )
            code, out, err = run_powershell(ps)
            if code != 0:
                # Fallback to OneDriveSetup.exe
                sysroot = os.environ.get("SystemRoot", r"C:\\Windows")
                setup = Path(sysroot) / "System32" / "OneDriveSetup.exe"
                if not setup.exists():
                    setup = Path(sysroot) / "SysWOW64" / "OneDriveSetup.exe"
                if setup.exists():
                    code, out, err = run_powershell(f"& '{setup}' /uninstall")
            ok = code == 0
            return ok, (err or out or "").strip()
        if name == "Microsoft.Edge":
            # Attempt uninstall via setup.exe under Edge Installer
            program_files = [
                Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")),
                Path(os.environ.get("ProgramFiles", r"C:\\Program Files")),
            ]
            ran = False
            last_msg = ""
            for base in program_files:
                installer = base / "Microsoft" / "Edge" / "Application"
                if installer.exists():
                    for version_dir in installer.iterdir():
                        path = version_dir / "Installer" / "setup.exe"
                        if path.exists():
                            cmd = f"& '{path}' --uninstall --system-level --force-uninstall"
                            code, out, err = run_powershell(cmd)
                            ran = True
                            last_msg = (err or out or "").strip()
                            if code == 0:
                                return True, last_msg
            return False, last_msg or "Edge uninstaller not found"
        if name == "THIRD_PARTY_BLOAT":
            ps = (
                "$ProgressPreference='SilentlyContinue';"
                "$pkgs = Get-AppxPackage -AllUsers | Where-Object { $_.Name -notlike 'Microsoft.WindowsStore*' -and $_.Name -notlike 'Microsoft.VCLibs*' -and $_.Name -notlike 'Microsoft.NET*' -and $_.Name -notlike 'Microsoft.UI*' -and $_.Name -notlike 'Microsoft.DesktopAppInstaller*' -and $_.SignatureKind -ne 'System' -and $_.IsFramework -ne $true };"
                "foreach($p in $pkgs){ try { Remove-AppxPackage -Package $p.PackageFullName -AllUsers -ErrorAction Continue } catch {} };"
                "Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -notlike 'Microsoft.WindowsStore*' -and $_.DisplayName -notlike 'Microsoft.VCLibs*' -and $_.DisplayName -notlike 'Microsoft.NET*' -and $_.DisplayName -notlike 'Microsoft.UI*' -and $_.DisplayName -notlike 'Microsoft.DesktopAppInstaller*' } | ForEach-Object { try { Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction Continue } catch {} }"
            )
            code, out, err = run_powershell(ps)
            return code == 0, (err or out or "").strip()
        # Generic Appx removal
        ps = (
            f"$n='{name}';"
            "$ProgressPreference='SilentlyContinue';"
            "Get-AppxPackage -AllUsers -Name $n | ForEach-Object { Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction Continue };"
            "Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -eq $n -or $_.PackageName -like (" + '"' + '${n}*' + '"' + ") } | ForEach-Object { Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction Continue }"
        )
        code, out, err = run_powershell(ps)
        return code == 0, (err or out or "").strip()
    except Exception as exc:
        return False, str(exc)


def _apply_feature(name: str) -> Tuple[bool, str]:
    try:
        key = name.strip().lower()
        if key == "disable copilot":
            reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", 1, "REG_DWORD")
            return True, "Copilot disabled"
        if key == "disable widgets":
            reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", 0, "REG_DWORD")
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa", 0, "REG_DWORD")
            return True, "Widgets disabled"
        if key == "disable chat":
            reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\Windows Chat", "ChatIcon", 3, "REG_DWORD")
            return True, "Chat disabled"
        if key == "disable cortana":
            reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\Windows Search", "AllowCortana", 0, "REG_DWORD")
            return True, "Cortana disabled"
        if key == "disable search highlights":
            reg_set("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\Windows Search", "EnableDynamicContentInWSB", 0, "REG_DWORD")
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Search", "SearchboxTaskbarMode", 0, "REG_DWORD")
            return True, "Search highlights disabled"
        if key == "disable suggested content":
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338389Enabled", 0, "REG_DWORD")
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-353698Enabled", 0, "REG_DWORD")
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-353696Enabled", 0, "REG_DWORD")
            return True, "Suggested content disabled"
        if key == "disable tips":
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SoftLandingEnabled", 0, "REG_DWORD")
            reg_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-310093Enabled", 0, "REG_DWORD")
            return True, "Tips disabled"
        if key == "remove web experience pack":
            # Try winget then fallback to Appx removal
            winget_cmd = (
                "winget uninstall --name \"Windows Web Experience Pack\" --silent --accept-source-agreements --accept-package-agreements"
            )
            code, out, err = run_powershell(winget_cmd)
            if code != 0:
                # Fallback remove appx
                ps = (
                    "$ProgressPreference='SilentlyContinue';"
                    "$packageNames=@('MicrosoftWindows.Client.WebExperience','MicrosoftWindows.Client.WebExperience_cw5n1h2txyewy');"
                    "foreach($pkg in $packageNames){"
                    " Get-AppxPackage -AllUsers -Name $pkg | ForEach-Object { Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction Continue };"
                    " Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -eq $pkg -or $_.PackageName -like \"$pkg*\" } | ForEach-Object { Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction Continue }"
                    "}"
                )
                code, out, err = run_powershell(ps)
            return (code == 0), (err or out or "").strip()
        return False, "Unknown feature"
    except Exception as exc:
        return False, str(exc)
