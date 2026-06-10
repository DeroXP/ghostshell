# GhostShell — Windows 11 Privacy & Performance Suite

A borderless desktop application for debloating, optimizing, and hardening Windows 11, with an encrypted password vault and a dark hacker-themed UI.

## Features

- **Debloater** — Remove 40+ bloatware apps, disable telemetry services & scheduled tasks, disable Copilot/Widgets/Cortana, uninstall OneDrive
- **Hard Optimizer** — Ultimate Performance power plan, CPU priority boost, core parking disable, memory compression off, SSD/NVMe tweaks, Game DVR off, fullscreen optimizations off, GPU scheduling, timer resolution
- **Network Optimizer** — DNS presets (Cloudflare/Google/Quad9/AdGuard/OpenDNS), Nagle disable, TCP/IP tuning, network throttling off, LSO disable, adapter power management, IPv6 toggle, ping testing
- **GPU Manager** — NVIDIA detection, clean driver install preparation, telemetry kill, max performance power mode, MSI mode, interrupt priority
- **Privacy Hardener** — 25+ telemetry registry tweaks, 50+ hosts file blocks, outbound firewall rules, webcam/mic control, Windows Update control, data collection kill
- **Password Vault** — PIN-protected (4-8 digits), PBKDF2 (600K iterations) key derivation, Fernet (AES-128-CBC) encryption, SQLite storage, search/filter, password generator, encrypted export/import, auto-lock
- **Temp Cleaner** — User/system temp, prefetch, WU cache, browser caches (Chrome/Firefox/Edge), recycle bin
- **Full Ghost** — One-click run of all modules in sequence

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

## Build Single .exe

```bash
pip install pyinstaller
pyinstaller build.spec
```

Output: `dist/GhostShell.exe` — portable, requests admin on launch.

## Safety

- Creates System Restore Point before modifications
- Backs up registry keys before every change
- All changes logged to `%APPDATA%/GhostShell/ghostshell.log`
- Vault encrypted with PBKDF2 + Fernet (AES)
- Everything runs locally — no data leaves your machine
