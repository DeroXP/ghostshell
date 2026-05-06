# GhostShell — Windows 11 Privacy / Debloater / Optimizer Suite

A borderless, dark, "covert terminal" desktop app that hard-debloats, hardens privacy, and aggressively optimizes fresh Windows 11 installs. GhostShell runs a Flask backend in a pywebview window, delivers a neon-cyan hacker UI, and applies system changes via PowerShell, registry edits, services, and tasks — all with logging, backups, and a System Restore point.

## What This Does
- Detects your hardware and OS for a live dashboard (CPU, GPU, RAM, storage, network)
- Streams a real-time log (terminal-style) of every operation with timestamps
- Runs a comprehensive Windows 11 debloat (Appx, features, services, scheduled tasks)
- Applies aggressive gaming-focused optimizations (power, CPU, memory, storage, graphics, visuals)
- Configures DNS and network latency tweaks with presets and tests
- Guides a clean NVIDIA driver installation flow and applies GPU registry tweaks
- Hardens privacy: telemetry level 0, hosts blocking, firewall blocks, Edge privacy, device toggles
- Spoofs identifiers (MachineGuid, ProductId, MAC, computer name, volume overlays) with backup & restore
- Provides an encrypted password vault protected by PIN (bcrypt + PBKDF2 + Fernet), with generator & auto-lock

## Screenshots / Preview
GhostShell uses a matte-black UI with neon cyan accents, matrix-rain canvas background, glitch title text, custom scan-line overlay, and a frameless draggable title bar with custom window buttons.

## Requirements
- Windows 11 (64-bit). Some features work on Windows 10, but the target is 11.
- Administrator privileges to apply system changes (UAC prompt will be shown).

## Setup — Step by Step

Step 1: Open your terminal (PowerShell recommended)

Step 2: Navigate to the project folder
  cd Ghost_Shell/ghostshell

Step 3: (Optional) Create and activate a virtual environment
  python -m venv .venv
  .venv\Scripts\Activate
  You should see (.venv) in your prompt.

Step 4: Install dependencies
  pip install -r requirements.txt
  You should see packages like Flask, pywebview, wmi, cryptography, bcrypt being installed.

Step 5: Run GhostShell (developer mode)
  python app.py
  You should see: a borderless window titled "GhostShell 1.0.0" with neon UI. The log panel will start filling.

Step 6: (Optional) Build a single-file .exe with PyInstaller
  pyinstaller --clean --noconfirm build.spec
  When it finishes, check dist/GhostShell/GhostShell.exe — it will request UAC on start.

## Configuration
Settings are in config.py — no secrets are hardcoded. Key values:
- FLASK_HOST=127.0.0.1  # Local web server host
- FLASK_PORT=5000       # Local web server port
- APP_NAME=GhostShell   # Window title and app data folder name
- VAULT_*               # Vault KDF iterations, autolock timeout, lockout attempts
- DNS_PRESETS           # DNS providers used by the Network module
- APPDATA_DIR           # Default: %APPDATA%/GhostShell (stores logs, DB, backups, profiles)

Example: To change the web port to 5001
- Edit config.py: FLASK_PORT = 5001

## How to Run
- Developer: python app.py
  You should see: "Flask running on http://127.0.0.1:5000" and a GhostShell window will appear.
- Portable .exe: dist/GhostShell/GhostShell.exe (after building). It will prompt for Administrator.

If you see the Elevation screen, click "Relaunch as Administrator" and accept the UAC prompt.

## How to Use (Quick Walkthrough)
- Dashboard: Opens with OS, CPU, GPU, RAM, storage, and network info. Status badges reflect which modules have run.
- Logs: The terminal panel scrolls with every action (you can keep this visible while running modules).
- Debloat: Removes Microsoft bloat apps, disables telemetry/feedback tasks, and turns off unwanted features. Create a restore point automatically.
- Optimize: Aggressive performance tweaks (power, CPU core parking off, memory compression off, storage fsutil, Game DVR off, HAGS on, visuals to performance, timer tweaks).
- Network: Set DNS (Cloudflare, Google, Quad9, AdGuard, or custom), apply latency registry tweaks, disable LSO/IPv6/QoS if desired, and test ping.
- GPU: Detects GPU/driver, prepares a safe-mode DDU cleanup + silent NVIDIA driver install flow, and applies registry/service tweaks.
- Privacy: Sets telemetry to 0 (Security), blocks domains via hosts, adds firewall blocks, hardens Edge privacy, and toggles camera/mic permissions; adjusts Windows Update behavior.
- HWID: Randomize MachineGuid, ProductId, MAC addresses, and computer name; backup & restore previous IDs. Reboot is recommended after applying.
- Vault: Set a PIN on first use; entries are encrypted using PBKDF2 + Fernet. Includes search, add/edit/delete, tags, password generator, and clipboard auto-clear.

Note: Many module actions require a reboot to fully take effect. The app will indicate where needed.

## Project Structure
```
ghostshell/
├── app.py                    # Flask app + pywebview launcher
├── config.py                 # App configuration constants
├── requirements.txt          # Python dependencies
├── build.spec                # PyInstaller build (onefile, uac-admin), bundles assets/templates/static
├── assets/
│   └── ghostshell.ico        # App icon
├── core/
│   ├── __init__.py
│   ├── utils.py              # Logging, admin elevation, PowerShell/registry/services/tasks helpers, DNS, ping
│   ├── system_info.py        # Dashboard detection (OS, CPU, GPU, RAM, storage, network, status badges)
│   ├── debloater.py          # Appx removal, features toggles, services/tasks disable, OneDrive/Edge helpers
│   ├── optimizer.py          # Power, CPU, memory, storage, gaming, visuals (aggressive) tweaks
│   ├── network_tweaks.py     # TCP/adapter tweaks (Nagle, LSO, throttling, responsiveness, IPv6, QoS, etc.)
│   ├── dns_manager.py        # DNS presets + apply to all adapters, current DNS parsing, flush cache
│   ├── nvidia_clean.py       # Clean NVIDIA install wizard scripts + registry tweaks
│   ├── privacy.py            # Telemetry level 0, hosts blocking, firewall rules, Edge privacy, device toggles
│   ├── hwid_spoofer.py       # Randomize IDs with backup/restore (MachineGuid, ProductId, MAC, name, volumes)
│   ├── startup_manager.py    # Enable/disable startup items with backup
│   └── vault.py              # Encrypted password vault (PIN bcrypt, PBKDF2, Fernet), clipboard auto-clear
├── templates/
│   ├── index.html            # Main UI (frameless title bar, live dashboard, log viewport)
│   └── elevate.html          # Elevation relaunch screen
└── static/
    ├── css/style.css         # Hacker theme: neon cyan, glitch, scan-lines, themed scrollbars
    └── js/app.js             # SSE log streaming, matrix rain, window controls, dashboard fetch
```

## Troubleshooting
- If you see ImportError for "wmi" or "pywin32": run
  pip install -r requirements.txt
  Ensure you are on Windows with a Python build that supports those packages.
- If port 5000 is busy: edit config.py and set FLASK_PORT = 5001, then run again.
- If UAC is denied: GhostShell cannot apply system changes. Re-run and accept the prompt.
- If the window doesn’t appear: your firewall may be blocking the local server. Allow localhost or change the port.
- If DNS changes don’t apply: try running the app as Administrator; also check adapter status and re-run.

## License
MIT — Free to use.
