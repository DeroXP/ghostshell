"""Vispora configuration constants."""
import os
import sys

APP_NAME = "Vispora"
APP_VERSION = "3.6.3"
APP_PORT = 5987

# Until 3.5.0 the app shipped as "GhostShell", and installs from that era still
# carry the old name on disk: the exe filename, the %APPDATA% folder, the
# autostart task, firewall / QoS rules, the hosts-file marker.  Nothing is
# created under this name any more — it is kept so the code can recognise what
# an older build left behind and adopt or remove it.
LEGACY_APP_NAME = "GhostShell"

APP_EXE_NAME            = f"{APP_NAME}.exe"
UPDATER_EXE_NAME        = f"{APP_NAME}Updater.exe"
LEGACY_APP_EXE_NAME     = f"{LEGACY_APP_NAME}.exe"
LEGACY_UPDATER_EXE_NAME = f"{LEGACY_APP_NAME}Updater.exe"

# Image names of our own processes, lower-cased — for the "never pause / flag /
# report ourselves" lists and the "is our own window focused" checks.  An
# install that was updated in place keeps its original exe filename, so the
# old names stay in the set.
OWN_PROCESS_NAMES = frozenset(n.lower() for n in (
    APP_EXE_NAME, UPDATER_EXE_NAME, LEGACY_APP_EXE_NAME, LEGACY_UPDATER_EXE_NAME))

# Release channel + update server.  "stable" is the default; "beta" stays
# available for early-access testers.
APP_CHANNEL_DEFAULT = "stable"
UPDATE_SERVER_URL_DEFAULT = "https://vispora.up.railway.app"
# The release server's previous hostname.  It has been dead (404) since the
# Railway service was renamed on 2026-07-01, so a saved server_url that still
# points at it would strand that install's update checks forever.
_DEAD_UPDATE_SERVER_HOST = "ghostshell-site.up.railway.app"


def is_dead_update_server(url: str) -> bool:
    """True if `url` points at the retired release-server hostname.  Settings
    loaders use this to rewrite a stale saved URL to the current default."""
    return _DEAD_UPDATE_SERVER_HOST in (url or "")


# ── Data folder ──────────────────────────────────────────────────────────
def _is_app_process() -> bool:
    """True when this interpreter IS the app (the frozen exe, or `python
    app.py`) rather than a test, tool or REPL that merely imports config."""
    if getattr(sys, "frozen", False):
        return True
    main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    return os.path.basename(main_file).lower() == "app.py"


def _resolve_appdata_dir() -> str:
    """Return the folder everything the app persists lives in.

    That is %APPDATA%/Vispora.  An install that predates the rename has its
    data (vault, license, profiles, OC baselines) in %APPDATA%/GhostShell, so
    the first launch of a renamed build moves that folder across with a single
    atomic rename — it either lands whole or not at all, never half-copied.

    The rename fails while any other process holds a file open in the old
    folder (typically the standalone updater that just launched us).  In that
    case, and for anything that isn't the app itself, keep using the old
    folder as-is; the next launch tries again.

    The standalone updater resolves the same folder the same way, minus the
    rename (updater/main.py) — keep the two in step.
    """
    root = os.environ.get("APPDATA", os.path.expanduser("~"))
    current = os.path.join(root, APP_NAME)
    legacy = os.path.join(root, LEGACY_APP_NAME)
    if os.path.isdir(current) or not os.path.isdir(legacy):
        return current
    if _is_app_process():
        try:
            os.rename(legacy, current)
            return current
        except OSError:
            pass
    return legacy


def _adopt_legacy_log(log_path: str) -> None:
    """Carry the pre-rename log file and its rotated copies over to the new
    filename so the log history stays in one place.  Best-effort."""
    folder, name = os.path.split(log_path)
    legacy_name = name.replace(APP_NAME.lower(), LEGACY_APP_NAME.lower())
    for suffix in ("", ".1", ".2", ".3"):
        old = os.path.join(folder, legacy_name + suffix)
        new = log_path + suffix
        try:
            if os.path.isfile(old) and not os.path.exists(new):
                os.rename(old, new)
        except OSError:
            pass


APPDATA_DIR = _resolve_appdata_dir()
VAULT_DB_PATH = os.path.join(APPDATA_DIR, "vault.db")
LOG_FILE_PATH = os.path.join(APPDATA_DIR, "vispora.log")
BACKUP_DIR = os.path.join(APPDATA_DIR, "backups")

for d in [APPDATA_DIR, BACKUP_DIR]:
    os.makedirs(d, exist_ok=True)
if _is_app_process():
    _adopt_legacy_log(LOG_FILE_PATH)


def user_media_dir(library: str, leaf: str) -> str:
    """Default save folder for clips / screenshots:
    <profile>/<library>/Vispora/<leaf>.  A user who already has a
    <library>/GhostShell folder from before the rename keeps saving there —
    those are their own files, and quietly moving them (or splitting the
    collection across two folders) would be worse than an old folder name."""
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    current = os.path.join(profile, library, APP_NAME)
    legacy = os.path.join(profile, library, LEGACY_APP_NAME)
    parent = legacy if (os.path.isdir(legacy) and not os.path.isdir(current)) else current
    return os.path.join(parent, leaf)


# Vault security
VAULT_PBKDF2_ITERATIONS = 600_000
VAULT_AUTO_LOCK_SECONDS = 300
VAULT_MAX_PIN_ATTEMPTS = 10
VAULT_LOCKOUT_SECONDS = 900

# DNS presets
DNS_PRESETS = {
    "cloudflare": {"name": "Cloudflare", "primary": "1.1.1.1", "secondary": "1.0.0.1", "desc": "Fast & private"},
    "google": {"name": "Google", "primary": "8.8.8.8", "secondary": "8.8.4.4", "desc": "Reliable & fast"},
    "quad9": {"name": "Quad9", "primary": "9.9.9.9", "secondary": "149.112.112.112", "desc": "Security focused"},
    "adguard": {"name": "AdGuard", "primary": "94.140.14.14", "secondary": "94.140.15.15", "desc": "Ad-blocking DNS"},
    "opendns": {"name": "OpenDNS", "primary": "208.67.222.222", "secondary": "208.67.220.220", "desc": "Cisco security"},
}

# Bloatware
BLOATWARE_APPS = [
    {"id": "Microsoft.BingNews", "name": "Bing News", "cat": "Bing"},
    {"id": "Microsoft.BingWeather", "name": "Bing Weather", "cat": "Bing"},
    {"id": "Microsoft.BingFinance", "name": "Bing Finance", "cat": "Bing"},
    {"id": "Microsoft.BingSports", "name": "Bing Sports", "cat": "Bing"},
    {"id": "Microsoft.GamingApp", "name": "Xbox Gaming App", "cat": "Xbox"},
    {"id": "Microsoft.XboxGameOverlay", "name": "Xbox Game Overlay", "cat": "Xbox"},
    {"id": "Microsoft.XboxGamingOverlay", "name": "Xbox Gaming Overlay", "cat": "Xbox"},
    {"id": "Microsoft.XboxIdentityProvider", "name": "Xbox Identity", "cat": "Xbox"},
    {"id": "Microsoft.XboxSpeechToTextOverlay", "name": "Xbox Speech", "cat": "Xbox"},
    {"id": "Microsoft.Xbox.TCUI", "name": "Xbox TCUI", "cat": "Xbox"},
    {"id": "Microsoft.GetHelp", "name": "Get Help", "cat": "System"},
    {"id": "Microsoft.Getstarted", "name": "Tips / Get Started", "cat": "System"},
    {"id": "Microsoft.MicrosoftOfficeHub", "name": "Office Hub", "cat": "Office"},
    {"id": "Microsoft.Office.OneNote", "name": "OneNote", "cat": "Office"},
    {"id": "Microsoft.MicrosoftSolitaireCollection", "name": "Solitaire", "cat": "Games"},
    {"id": "Microsoft.MicrosoftStickyNotes", "name": "Sticky Notes", "cat": "System"},
    {"id": "Microsoft.MixedReality.Portal", "name": "Mixed Reality", "cat": "System"},
    {"id": "Microsoft.People", "name": "People", "cat": "Social"},
    {"id": "Microsoft.PowerAutomateDesktop", "name": "Power Automate", "cat": "Office"},
    {"id": "Microsoft.ScreenSketch", "name": "Snipping Tool", "cat": "System"},
    {"id": "Microsoft.SkypeApp", "name": "Skype", "cat": "Social"},
    {"id": "Microsoft.Todos", "name": "To Do", "cat": "Office"},
    {"id": "Microsoft.WindowsAlarms", "name": "Alarms & Clock", "cat": "System"},
    {"id": "Microsoft.WindowsCamera", "name": "Camera", "cat": "System"},
    {"id": "Microsoft.WindowsCommunicationsApps", "name": "Mail & Calendar", "cat": "Social"},
    {"id": "Microsoft.WindowsFeedbackHub", "name": "Feedback Hub", "cat": "System"},
    {"id": "Microsoft.WindowsMaps", "name": "Maps", "cat": "System"},
    {"id": "Microsoft.WindowsSoundRecorder", "name": "Sound Recorder", "cat": "System"},
    {"id": "Microsoft.YourPhone", "name": "Your Phone / Phone Link", "cat": "Social"},
    {"id": "Microsoft.ZuneMusic", "name": "Groove Music", "cat": "Media"},
    {"id": "Microsoft.ZuneVideo", "name": "Movies & TV", "cat": "Media"},
    {"id": "Clipchamp.Clipchamp", "name": "Clipchamp", "cat": "Media"},
    {"id": "Disney.37853FC22B2CE", "name": "Disney+", "cat": "Media"},
    {"id": "SpotifyAB.SpotifyMusic", "name": "Spotify", "cat": "Media"},
    {"id": "BytedancePte.Ltd.TikTok", "name": "TikTok", "cat": "Social"},
    {"id": "Microsoft.549981C3F5F10", "name": "Cortana", "cat": "System"},
    {"id": "MicrosoftCorporationII.QuickAssist", "name": "Quick Assist", "cat": "System"},
    {"id": "Microsoft.StorePurchaseApp", "name": "Store Purchase", "cat": "System"},
    {"id": "Microsoft.Wallet", "name": "Microsoft Pay", "cat": "System"},
    {"id": "Microsoft.WindowsPhone", "name": "Phone Companion", "cat": "Social"},
    {"id": "MicrosoftCorporationII.MicrosoftFamily", "name": "Family Safety", "cat": "System"},
    {"id": "Microsoft.OutlookForWindows", "name": "New Outlook", "cat": "Office"},
    {"id": "MSTeams", "name": "Microsoft Teams", "cat": "Social"},
]

# Services to disable
SERVICES_TO_DISABLE = [
    {"id": "DiagTrack", "name": "Connected User Experiences & Telemetry", "cat": "Telemetry"},
    {"id": "dmwappushservice", "name": "WAP Push Message Routing", "cat": "Telemetry"},
    {"id": "SysMain", "name": "Superfetch", "cat": "Performance", "cond": "ssd_only"},
    {"id": "WSearch", "name": "Windows Search Indexer", "cat": "Performance"},
    {"id": "MapsBroker", "name": "Downloaded Maps Manager", "cat": "Bloat"},
    {"id": "lfsvc", "name": "Geolocation Service", "cat": "Privacy"},
    {"id": "RetailDemo", "name": "Retail Demo Service", "cat": "Bloat"},
    {"id": "wisvc", "name": "Windows Insider Service", "cat": "Bloat"},
    {"id": "WerSvc", "name": "Windows Error Reporting", "cat": "Telemetry"},
    {"id": "TabletInputService", "name": "Tablet Input Service", "cat": "Bloat"},
    {"id": "PcaSvc", "name": "Program Compatibility Assistant", "cat": "Performance"},
    {"id": "WMPNetworkSvc", "name": "WMP Network Sharing", "cat": "Bloat"},
    {"id": "AJRouter", "name": "AllJoyn Router", "cat": "Bloat"},
    {"id": "NvTelemetryContainer", "name": "NVIDIA Telemetry", "cat": "Telemetry"},
    {"id": "fhsvc", "name": "File History Service", "cat": "Bloat"},
    {"id": "InstallService", "name": "MS Store Install Service", "cat": "Bloat"},
]

# Telemetry hosts to block
TELEMETRY_HOSTS = [
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
    "diagnostics.support.microsoft.com",
    "corp.sts.microsoft.com",
    "statsfe1.ws.microsoft.com",
    "pre.footprintpredict.com",
    "feedback.microsoft-hohm.com",
    "feedback.search.microsoft.com",
    "rad.msn.com",
    "preview.msn.com",
    "ad.doubleclick.net",
    "ads.msn.com",
    "ads1.msads.net",
    "ads1.msn.com",
    "a.ads1.msn.com",
    "a.ads2.msn.com",
    "ca.telemetry.microsoft.com",
    "oca.microsoft.com",
    "oca.report.microsoft.com",
    "ssw.live.com",
    "az361816.vo.msecnd.net",
    "az512334.vo.msecnd.net",
    "activity.windows.com",
    "bingapis.com",
    "msedge.net",
    "data.microsoft.com",
]

# Scheduled tasks to disable
SCHEDULED_TASKS_TO_DISABLE = [
    r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser",
    r"\Microsoft\Windows\Application Experience\ProgramDataUpdater",
    r"\Microsoft\Windows\Application Experience\StartupAppTask",
    r"\Microsoft\Windows\Autochk\Proxy",
    r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator",
    r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip",
    r"\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
    r"\Microsoft\Windows\Feedback\Siuf\DmClient",
    r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload",
    r"\Microsoft\Windows\Windows Error Reporting\QueueReporting",
    r"\Microsoft\Windows\Maps\MapsUpdateTask",
    r"\Microsoft\Windows\Maps\MapsToastTask",
    r"\Microsoft\Windows\Shell\FamilySafetyMonitor",
    r"\Microsoft\Windows\Shell\FamilySafetyRefreshTask",
]

# Firewall executables to block outbound
FIREWALL_BLOCK_EXES = [
    r"C:\Windows\System32\DiagTrackRunner.exe",
    r"C:\Windows\System32\CompatTelRunner.exe",
    r"C:\Windows\System32\DeviceCensus.exe",
    r"C:\Windows\System32\smartscreen.exe",
]
