"""Vispora Configuration Constants."""
import os

APP_NAME = "Vispora"
APP_VERSION = "3.5.1-beta.6"
APP_PORT = 5987

# Vispora rebrand note: the on-disk AppData folder, the update-server host, the
# update routes, and (for now) the shipped exe filename DELIBERATELY stay named
# "GhostShell" so the existing installed base keeps auto-updating and keeps its
# saved data (profiles, license, OC baselines).  Only user-visible branding +
# the theme changed in this release.  See APPDATA_DIR below.

# v3 — release channel + update server.  Default flipped to "stable"
# now that v3.0.0 has shipped as the first stable build.  Beta channel
# stays available for early-access testers.
APP_CHANNEL_DEFAULT = "stable"
UPDATE_SERVER_URL_DEFAULT = "https://vispora.up.railway.app"
# The Railway service was renamed ghostshell-site → vispora (2026-07-01).
# The old ghostshell-site.up.railway.app domain is DEAD (404) — binaries
# ≤3.5.0-beta.11 / stable 3.4.3 point at it and can only recover if the
# old domain is re-added as an alias on Railway or the user updates
# manually.  core/updater.py migrates any SAVED old server_url on load.
UPDATE_SERVER_URL_LEGACY = "https://ghostshell-site.up.railway.app"
UPDATE_CHECK_INTERVAL_SEC = 3600    # check once per hour while running

# Paths.  APPDATA_DIR stays "GhostShell" on purpose (see rebrand note up top) —
# renaming it would strand every existing user's saved data and desync the
# standalone updater, which independently computes this same path.
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GhostShell")
UPDATE_STATE_PATH = os.path.join(APPDATA_DIR, "update_state.json")
PENDING_UPDATE_PATH = os.path.join(APPDATA_DIR, "pending_update.exe")
VAULT_DB_PATH = os.path.join(APPDATA_DIR, "vault.db")
LOG_FILE_PATH = os.path.join(APPDATA_DIR, "ghostshell.log")
BACKUP_DIR = os.path.join(APPDATA_DIR, "backups")
PROFILE_DIR = os.path.join(APPDATA_DIR, "profiles")

# Ensure dirs exist
for d in [APPDATA_DIR, BACKUP_DIR, PROFILE_DIR]:
    os.makedirs(d, exist_ok=True)

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
