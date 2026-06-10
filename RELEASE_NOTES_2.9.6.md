# GhostShell v2.9.6

Three improvements: GPU driver auto-updater, single-instance launch behaviour, and small polish.

## 🎮 GPU driver auto-updater

A new background service that keeps your NVIDIA driver up to date.

**On startup** (and **every 12 hours** thereafter, configurable in Settings):
1. `nvidia-smi` reads your installed driver version + GPU model.
2. The model is mapped to NVIDIA's `(psid, pfid)` IDs (RTX 30/40/50 + GTX 16/10 hardcoded — the AjaxDriverService API needs them).
3. We hit `https://gfwsl.geforce.com/services_toolkit/.../AjaxDriverService.php?func=DriverManualLookup` for the latest WHQL DCH driver.
4. Versions compared as numeric tuples (e.g. `596.21 < 596.36`).
5. If newer is available → toast + Settings card show the version, release date, and size.

**Tested live** on your RTX 5070 Ti:
```
current_version: 596.21
latest_version:  596.36
update_available: True
download_url:    https://us.download.nvidia.com/Windows/596.36/...
release_date:    Tue Apr 28, 2026
size_mb:         958.86 MB
```

**Install flow:**
1. Click **Download** in the toast or Settings → driver → fetched to `%APPDATA%\GhostShell\drivers\` (15-minute timeout for the ~960 MB download).
2. Click **Install** → launches NVIDIA's installer interactively (you see their UI, choose Express/Custom, accept their EULA). Never silent — drivers are too important to install without you watching.
3. The downloaded file is cached, so re-clicking Install re-uses it instead of re-downloading.

**Settings:**
- *Auto-check on startup + every 12h* — default ON
- *Download installer in the background* — default OFF (drivers are big; opt in)

**AMD path** detects your installed driver and links you to AMD's support site — auto-installing AMD drivers requires driving their EULA which we can't do headlessly.

## 🪟 Single-instance launch

**The bug:** with GhostShell hidden in the tray, double-clicking `GhostShell.exe` from Explorer / taskbar would spawn a brand-new instance instead of just bringing the existing window forward. Two ghosts on the system, fighting for port 5987.

**The fix:** before any startup work, `main()` now claims a Windows named mutex (`Local\GhostShell-SingleInstance-{8B7A1F23}`).
- If we got the mutex → we're the one true instance, continue normally.
- If the mutex was already held → another GhostShell is alive. POST `/api/window/show` to bring it to the foreground, then `sys.exit(0)`.

This runs **before** the UAC elevation check too — so a second double-click doesn't even get a UAC prompt anymore. Verified the process count stays correct across multiple launches.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After the upgrade, head to **Settings → GPU DRIVER** to see your driver status. If your card isn't in our hardcoded ID table (RTX 30/40/50, GTX 16/10), you'll get a "manual check" link to NVIDIA's site.
