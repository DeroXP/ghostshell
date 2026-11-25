# Vispora

A Windows 10/11 desktop app for gaming performance, system clean-up and privacy.
It runs elevated, as a single portable exe: a local Flask backend with a
borderless WebView2 window on top (pywebview), plus a tray icon.

## What it does

**System**
- **Debloat & optimize**: remove bundled apps, disable telemetry services and
  tasks, power-plan / scheduler / timer / interrupt tweaks, VBS toggle.
- **Network**: DNS presets, latency tweaks, per-game QoS, a network-burst monitor.
- **Privacy**: telemetry policies, hosts-file blocking, outbound firewall rules.
- **Clean-up**: temp/junk cleaner, a disk-space analyzer, startup manager.
- **Safety net**: restore points, registry snapshots you can roll back to,
  a health audit, and automatic recovery from tweaks older builds got wrong.

**Gaming**
- **Game profiles**: detects a running game, applies the gaming profile, and
  restores the previous state when it exits. Optional background-app pauser,
  tournament mode and idle mode.
- **Latency**: a one-click competitive-latency bundle, a read-only Latency
  Doctor, and per-game advice for the settings only the game itself can change.
- **GPU** (NVIDIA): overclocking with stability testing and auto-tune, Adaptive
  Tuning (runs games on your saved overclock and eases it off per game if the
  graphics driver keeps having problems there), a hard temperature ceiling,
  crash recovery, driver updates.
- **Monitoring**: live hardware sensors.
- **Capture**: instant-replay clips (FFmpeg or OBS), screenshots, stream helper.
- **Crosshair overlay**: a custom crosshair in its own see-through window, never
  touching the game. Works over windowed, borderless and most fullscreen games;
  it can't draw over true exclusive fullscreen.

**Also**
- **Password vault**: PIN-protected, PBKDF2 + Fernet, stored locally.
- **Auto-update**: stable and beta channels, from GitHub's releases with the
  update server as the fallback, SHA-256-verified downloads, installed by a
  small standalone updater.

## Run from source

```bash
pip install -r requirements.txt
python app.py
```

Needs Windows and an administrator prompt (the app asks to elevate itself).

## Build

```bash
cargo build --release --manifest-path native/Cargo.toml   # the native helper (needs Rust)
pip install pyinstaller
pyinstaller build.spec --noconfirm            # dist/Vispora.exe
pyinstaller updater_helper.spec --noconfirm   # dist/VisporaUpdater.exe
```

The first line builds `native/target/release/vispora_native.dll`, a small Rust
library that answers the app's most frequent questions (which processes have a
window, the graphics card's readings, what the first screen shows, and so on)
without starting a program for each. Run it again after every change under
`native/`. `build.spec` bundles the library when it is there and warns when it
is not; a library left over from before such a change stops the build. A
build without it works the same, only slower: the app then asks PowerShell and
`nvidia-smi` as before. See [native/README.md](native/README.md).

The updater exists twice for now. `updater_helper.spec` builds the Python one
(version 1.5.0), which is the one releases carry. Its replacement in Rust is
in `updater_rs/` (version 2.0.0), has not been released yet, and is built with

```bash
cargo build --release --locked --manifest-path updater_rs/app/Cargo.toml
```

into `updater_rs/app/target/release/VisporaUpdater.exe`. The first time, that
build downloads Tauri and the crates it needs from crates.io; which ones, in
which versions, is fixed by `updater_rs/app/Cargo.lock`. See
[updater_rs/README.md](updater_rs/README.md).

The shell that is to become `Vispora.exe` is written in `shell_rs/`. It
carries the app in its own exe; `backend.spec` builds the app as a folder
for that. It is not part of any release yet, and `build.spec` is still what
a release is built with; see [shell_rs/README.md](shell_rs/README.md), "One
file", for how the one file is built.

**Where Rust's build output goes.** It is large (over a gigabyte for the
updater's window) and changes at every build. If this repository is in a
folder that a program keeps in sync (OneDrive), keep that output out of it.
Name a folder elsewhere, in the terminal the builds are run in:

```powershell
$env:CARGO_TARGET_DIR = "$env:LOCALAPPDATA\vispora-build"
cargo build --release --locked --manifest-path updater_rs/app/Cargo.toml
cargo build --release --manifest-path shell_rs/core/Cargo.toml
cargo build --release --locked --manifest-path shell_rs/app/Cargo.toml
```

The tests and the trial scripts look in that folder first for what cargo
built (`tests/test_shell_rs.py`, `tests/test_updater_rs.py`,
`shell_rs/try_it.py`, `shell_rs/app/try_it.py`, `updater_rs/app/try_it.py`), so run them from the same
terminal. **Build `native/` without it** (a new terminal, or
`Remove-Item Env:CARGO_TARGET_DIR`): `build.spec` and the app look for the
helper in `native/target`, and that folder is small.

Release binaries are not kept in this repo. They are published in two places,
and an installed copy asks them in this order:

1. **GitHub releases** of this repository. A release is tagged `v<version>`
   and carries `Vispora.exe` and the updater as `VisporaUpdater-<version>.exe`
   (attach the current updater to every release). A release marked as a
   pre-release only reaches the beta channel. Downloads are checked against
   the SHA-256 GitHub records for each uploaded file.
2. **The update server**, which serves `version.json` + the exe per channel.

Settings > Auto-Update > Update source picks one of them only. A server URL
of your own is asked before GitHub. `update_sources.py` holds the rules and
is shared by the app and the updater.

## Test

```bash
python tests/run_all.py            # everything (about a minute)
python tests/run_all.py reset game # only the files whose name contains a word
python tests/run_all.py -v         # list every test, not just failures
python tests/run_all.py --allow-skips   # a skipped test does not fail the run
```

Run it before a release, with the interpreter the release is built with.
It needs nothing beyond the app's own requirements (no pytest).

The tests run the real code against a fake machine (`tests/sandbox.py`): an
in-memory registry, power plans, services, scheduled tasks, boot entry and so
on, with `reg`, `powercfg`, `sc`, `bcdedit`, DISM and PowerShell answered the
way Windows answers them.  Nothing on the PC is read or changed, the app's
data folder is a throwaway one, and a test that tries to start a real process
fails.  That is what lets "apply every tweak, press Reset All, compare the
machine value by value" run in seconds.

Add a test by writing a `test_*` function in a `tests/test_*.py` file; start
the file with `import sandbox as m`, before any app import.

One test is the exception to "nothing on the PC is read": on a PC where the
native helper has been built, `tests/test_native.py` asks the real library a
few read-only questions (the process list, the processor's clock, the active
power plan) and has it trim that test's own memory. The Rust side has tests of
its own: `cargo test --release --manifest-path native/Cargo.toml`.

The Rust updater has the Python updater's cases as its own tests, against a
fake network and fake processes:
`cargo test --release --manifest-path updater_rs/core/Cargo.toml`. Its real
exe can be tried against a made-up install in a temporary folder with
`python updater_rs/app/try_it.py`; read the top of that file first.

The shell's core has its tests in `shell_rs/core`
(`cargo test --release --manifest-path shell_rs/core/Cargo.toml`), against a
backend that exists only in the test. `tests/test_backend_mode.py`,
`tests/test_shell_rs.py`, `tests/test_tray_calls.py` and
`tests/test_one_file.py` check the app's side of the arrangement (the third
holds the shell's tray icon against today's, the fourth is about the shell
carrying the app in its exe), and, where
the shell's trial tool has been built, start it for real with the app on the
fake machine as its backend. The real backend is never started by a test.

## Layout

| Path | What lives there |
| --- | --- |
| `app.py` | Flask routes, window/tray lifecycle, startup sequence |
| `config.py` | Names, paths and the static tweak lists |
| `update_sources.py` | Where releases are published (GitHub, the update server) and how each answer is read |
| `core/` | One module per feature |
| `native/` | The native helper: a Rust library for the readings the app takes all the time and the ones a start waits for (see `native/README.md`) |
| `static/`, `templates/` | The UI (plain JS/CSS, no build step) |
| `updater/` | The standalone updater app (its own Flask + pywebview window) |
| `shell_rs/` | The shell that is to become `Vispora.exe`, in Rust, not released yet: its core (which request is answered where, the backend's start and stop, quitting), its window, its tray, and carrying the app in its own exe. See `shell_rs/README.md` |
| `backend.spec` | Builds the app as a folder for the shell to carry. Not used by a release; `build.spec` is |
| `updater_rs/` | The same updater rewritten in Rust, not released yet: `core/` does the work with no outside crates, `app/` is its window (Tauri). See `updater_rs/README.md` |
| `tests/` | The test suite and the fake machine it runs on (see [Test](#test)) |
| `cloud/` | Optional FastAPI licensing / trial backend (see `cloud/README.md`) |
| `docs/` | Where the app's time goes (`performance.md`) and the order of the move to Rust (`rust-port.md`) |
| `tools/measure/` | The scripts behind `docs/performance.md`; none of them changes a setting on the PC |

## Data

Everything the app saves lives in `%APPDATA%\Vispora` (settings, vault, logs,
registry backups, snapshots). Clips go to `Videos\Vispora\Clips`, screenshots
to `Pictures\Vispora\Screenshots`.

The app was called GhostShell until 3.5.0. The first launch of a current build
moves `%APPDATA%\GhostShell` to `%APPDATA%\Vispora` and takes over whatever an
older build left on the system (autostart task, firewall and QoS rules, the
hosts-file block). An existing `Videos\GhostShell` or `Pictures\GhostShell`
folder keeps being used as it is, since those are the user's own files. The old name
survives in the code only as `LEGACY_APP_NAME` in `config.py` and
`updater/paths.py`.

## License

MIT. See `LICENSE`.
