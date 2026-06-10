# GhostShell v2.9.5

Three improvements: a benchmark crash fix, a new Settings tab, and start-on-boot.

## 🐛 Benchmark zero-data fix

**The bug:** Running "Benchmark Stock vs OC" produced a comparison table where every metric was `0` — `0 FPS`, `0 ms`, `0 °C`. The benchmark looked like it ran for 70 seconds but nothing actually happened.

**Root cause:** `_runAutoStabilityStep()` is shared between auto-tune and the benchmark, and its measurement loop had a guard:

```js
if (!_ocState.autoActive) break;
```

`autoActive` is only true during *auto-tune*, never during a benchmark. So the loop bailed on its very first iteration and the verdict came back empty.

**Fix:** new `_ocState.benchmarkActive` flag, set inside a `try/finally` around the benchmark, and the loop now accepts either signal:

```js
if (!_ocState.autoActive && !_ocState.benchmarkActive) break;
```

Benchmarks now actually measure.

## ⚙️ Settings tab

New entry in the sidebar, just above Logs. Houses everything that was previously buried in modals:

- **Startup** — toggle "Start GhostShell at Windows login" + adjust the post-login delay
- **Auto-update** — auto-check / auto-download / auto-install toggles, plus a manual "Check now"
- **Notifications** — Windows toast on/off + a "Test notification" button
- **GPU stock baseline** — shows your true stock clocks and lets you re-capture them
- **About** — version + APPDATA path

## 🪟 Start on boot — but politely

A new opt-in: GhostShell can register itself in `HKCU\…\Run` to launch at Windows login. Two-stage so it doesn't fight the OS for resources during boot:

**Stage 1 — at login** (Windows side):
A tiny PowerShell launcher sleeps for `delay_seconds` (default 30 s, configurable 0–600), then starts `GhostShell.exe --from-autostart` hidden.

**Stage 2 — when GhostShell launches** (app side):
The `--from-autostart` flag tells `app.py`:

| Task | Manual launch | Autostart launch |
|---|---|---|
| Window visibility | shown | **hidden** (tray-only — no focus stealing) |
| GitHub update check | immediate | **+90 s** |
| Boot-prep async work | immediate | **+60 s** |
| Saved-OC profile apply | immediate | **+15 s** |

Net effect: when you sign in, your desktop appears on time, your usual startup apps run unhindered, and GhostShell quietly slots in a minute or so later. Click the tray icon when you want to see it.

No admin needed for the registration — `HKCU` is per-user. Toggling autostart updates the registry value immediately; toggling the delay re-registers with the new wait time.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |
