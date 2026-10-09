# Vispora

A Windows 10/11 desktop app for gaming performance, system clean-up and privacy.
It runs elevated, as a single portable exe: a local Flask backend with a
borderless WebView2 window on top (pywebview), plus a tray icon.

## What it does

**System**
- **Debloat & optimize** — remove bundled apps, disable telemetry services and
  tasks, power-plan / scheduler / timer / interrupt tweaks, VBS toggle.
- **Network** — DNS presets, latency tweaks, per-game QoS, a network-burst monitor.
- **Privacy** — telemetry policies, hosts-file blocking, outbound firewall rules.
- **Clean-up** — temp/junk cleaner, a disk-space analyzer, startup manager.
- **Safety net** — restore points, registry snapshots you can roll back to,
  a health audit, and automatic recovery from tweaks older builds got wrong.

**Gaming**
- **Game profiles** — detects a running game, applies the gaming profile, and
  restores the previous state when it exits. Optional background-app pauser,
  tournament mode and idle mode.
- **Latency** — a one-click competitive-latency bundle, a read-only Latency
  Doctor, and per-game advice for the settings only the game itself can change.
- **GPU** (NVIDIA) — overclocking with stability testing and auto-tune, per-game
  adaptive tuning, a hard temperature ceiling, crash recovery, driver updates.
- **Monitoring** — live hardware sensors and frame-time telemetry (PresentMon).
- **Capture** — instant-replay clips (FFmpeg or OBS), screenshots, stream helper.
- **Gamepad mapper** — remapping, macros and a virtual controller (ViGEmBus /
  HidHide). Input remapping can violate a game's anti-cheat rules; that is on you.

**Also**
- **Password vault** — PIN-protected, PBKDF2 + Fernet, stored locally.
- **Auto-update** — stable and beta channels, SHA-256-verified downloads,
  installed by a small standalone updater.

## Run from source

```bash
pip install -r requirements.txt
python app.py
```

Needs Windows and an administrator prompt (the app asks to elevate itself).

## Build

```bash
pip install pyinstaller
pyinstaller build.spec --noconfirm            # dist/Vispora.exe
pyinstaller updater_helper.spec --noconfirm   # dist/VisporaUpdater.exe
```

Release binaries are not kept in this repo; they are published through the
release server, which serves `version.json` + the exe per channel.

## Layout

| Path | What lives there |
| --- | --- |
| `app.py` | Flask routes, window/tray lifecycle, startup sequence |
| `config.py` | Names, paths and the static tweak lists |
| `core/` | One module per feature |
| `static/`, `templates/` | The UI (plain JS/CSS, no build step) |
| `updater/` | The standalone updater app (its own Flask + pywebview window) |
| `cloud/` | Optional FastAPI licensing / trial backend — see `cloud/README.md` |

## Data

Everything the app saves lives in `%APPDATA%\Vispora` (settings, vault, logs,
registry backups, snapshots). Clips go to `Videos\Vispora\Clips`, screenshots
to `Pictures\Vispora\Screenshots`.

The app was called GhostShell until 3.5.0. The first launch of a current build
moves `%APPDATA%\GhostShell` to `%APPDATA%\Vispora` and takes over whatever an
older build left on the system (autostart task, firewall and QoS rules, the
hosts-file block). An existing `Videos\GhostShell` or `Pictures\GhostShell`
folder keeps being used as it is — those are the user's own files. The old name
survives in the code only as `LEGACY_APP_NAME` in `config.py` and
`updater/paths.py`.

## License

MIT — see `LICENSE`.
