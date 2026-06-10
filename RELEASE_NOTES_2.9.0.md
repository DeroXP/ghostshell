# GhostShell v2.9.0

First public release since v2.1.1 — a huge update with major new features, a true working GPU overclock path, and an auto-updater that handles future releases.

## ✨ New features

### Auto-updater
- GhostShell now checks GitHub for new releases automatically (every 6 hours + on launch).
- Three configurable behaviours, off-by-default for the disruptive one:
  - **Check automatically** (on)
  - **Download in background** (on)
  - **Install silently and restart** (off — opt-in)
- A non-intrusive toast appears in the bottom-right corner when a new build is ready. One click installs it and restarts the app cleanly.

### Smarter close button
- Pressing the title-bar X now opens a choice dialog:
  - **▶ Hide + Game Mode** — minimises to tray and arms the game-detection monitor so the gaming profile auto-applies as soon as a game launches.
  - **○ Hide to Tray** — legacy hide-to-tray behaviour.
  - **✕ Quit GhostShell** — fully exits, restoring any active gaming tweaks first so the system is left clean.

### Multi-executable game detection
- When several known game executables are running at once (e.g. a launcher plus the actual game, or two games side-by-side), GhostShell now picks the right one instead of grabbing whatever PowerShell happened to enumerate first.
- Candidates are scored by foreground-window ownership (+1000) and working-set size; the highest-scoring process wins.
- Once a game is being tracked, GhostShell stays focused on it until that process exits.

## 🛠 Major fixes

### True GPU overclocking on Blackwell / Ada / Ampere
- The verify screen used to falsely report "✓ OC applied" when the core clock had not actually moved. Root cause: `nvidia-smi -lgc` only sets a *cap*, not an offset; and `NvAPI_GPU_SetPstates20` adjusts the P-state base, not the boost ceiling.
- v2.9.0 ships a real V/F-curve writer using `NvAPI_GPU_GetClockBoostTable` / `SetClockBoostTable` — the same mechanism MSI Afterburner uses. Memory keeps using `SetPstates20`, which works correctly for memory.
- Verify now uses `nvidia-smi clocks.max.graphics` as ground truth; false-positive verifies are no longer possible.

### Crash fixes
- `_clamp` was called inside `apply_oc()` but never defined — Apply would return an HTML 500 page that the JS client surfaced as `"Unexpected token '<'"`.
- `check_driver_events()` was called from the stability probe in three places but never defined — would crash the very first probe tick.
- `delta.get('fps_pct'):+.1f` could throw `TypeError` when the benchmark delta lacked the key.

### Robustness pass
- **Backend**: every Flask route now goes through global error handlers — uncaught exceptions return clean JSON instead of HTML, so the front-end always sees a structured `{ok:false, err:...}` response.
- **Frontend**: `apiGet` / `apiPost` rewritten with content-type checking, HTTP status awareness, and `AbortController` timeouts (30 s GET / 60 s POST). The Apply button is debounced so a double-click can't fire two parallel NVAPI writes.
- **Game profiles**: stability-probe and auto-OC sessions are now protected by a `threading.Lock` to prevent concurrent ticks from racing.
- **Hardware monitor**: silent failures in the polling loop now log a warning so they don't disappear into the void.
- **ISLC loop** (RAM standby cleaner) now has a 10-error circuit breaker so a permission regression can't busy-spin forever.

### UI polish
- Loading spinner state for async buttons (`.btn.loading`).
- Empty-state placeholder class with consistent typography.
- `:focus-visible` outlines for keyboard accessibility on every interactive control.
- Disabled buttons are visibly distinct (grayscale + 0.4 opacity vs the previous 0.3 ghost).
- Sliders unified — one stylesheet rule, no more conflicting OC-slider overrides.
- `.btn-danger` deduped (two competing rules collapsed into one).
- Title-bar buttons gained `:active` press feedback.
- Page-switch teardown now aborts in-flight stress / auto-OC tests so timers don't leak across tabs.
- Matrix-rain canvas uses `requestAnimationFrame` instead of `setInterval`, so it auto-pauses when the window is hidden.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading from v2.1.1

Just download the new `GhostShell.exe` and replace the old one. All user data lives in `%APPDATA%\GhostShell\` and is preserved.

After this release, GhostShell will keep itself updated automatically — you can leave it running in the tray and new versions will be fetched and installed (with your permission) when they ship.
