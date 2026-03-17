import os
from pathlib import Path

APP_NAME = "GhostShell"
APP_VERSION = "1.0.0"

# Directories
APPDATA_DIR = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
LOG_FILE = APPDATA_DIR / "ghostshell.log"
DB_FILE = APPDATA_DIR / "vault.db"
BACKUP_DIR = APPDATA_DIR / "backups"
PROFILE_DIR = APPDATA_DIR / "profiles"

# Security
VAULT_KDF_ITERATIONS = 600_000
VAULT_AUTOLOCK_SECONDS = 300
VAULT_MAX_ATTEMPTS = 10
VAULT_LOCKOUT_MINUTES = 15

# Flask
FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000
FLASK_DEBUG = False
SECRET_KEY = os.getenv("GHOSTSHELL_SECRET", os.urandom(32))

# UI
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
WINDOW_MIN_WIDTH = 1024
WINDOW_MIN_HEIGHT = 600

# Misc
RESTORE_POINT_DESCRIPTION = "GhostShell Pre-Modification"

# DNS Presets
DNS_PRESETS = {
    "cloudflare": ["1.1.1.1", "1.0.0.1"],
    "cloudflare_gaming": ["1.1.1.1", "1.0.0.1"],
    "google": ["8.8.8.8", "8.8.4.4"],
    "quad9": ["9.9.9.9", "149.112.112.112"],
    "adguard": ["94.140.14.14", "94.140.15.15"],
}
