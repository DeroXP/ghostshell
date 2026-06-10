# GhostShell v2.9.4

Two important fixes in this build:

## 🎯 True base-clock detection

**The problem:** When another OC tool (Afterburner, a saved profile, leftover driver state) has an active offset applied, `nvidia-smi clocks.max.graphics` reports `stock + offset`. v2.9.3 took that inflated value as "stock", so:

- Targets were wrong (UI showed *target 3140 MHz* when the real target should have been *2915 MHz*)
- Reset-to-stock looked like a +225 MHz overclock
- Verify thought you needed to be at 3090 to be "stock"

In your last screenshot, GhostShell saw the GPU at `3090 MHz core / 14001 MHz mem` and treated those as the base clocks — but with `+225 / +1125` already active in NVAPI, your **true** base clocks were actually:
- Core stock: `3090 − 225 = 2865 MHz`
- Memory stock: `14001 − 1125 = 12876 MHz`

**The fix:** v2.9.4 captures `true_stock = nvidia_smi_max − active_NVAPI_offset` on first launch, persists it to `%APPDATA%\GhostShell\gpu_stock_baseline.json`, and uses it as the source of truth for all future target / verify math. Even if Afterburner re-applies its boost later, GhostShell still knows what your real stock is.

A new endpoint `POST /api/gpu/oc/baseline` lets you re-capture if you ever need to (e.g. after a driver update).

## 🔄 Auto-update reliability

The auto-updater now actually tells you whether it worked.

**Before:** click "Install & Restart", app dies, new exe relaunches… and you have no idea whether the swap landed or you're still on the old build.

**Now:**
- Right before the install batch runs, GhostShell records `previous_version: 2.9.3` and `pending_install_version: 2.9.4`.
- The improved install batch (60 s PID wait + 20 copy retries) writes `last_install.status = OK` or `FAIL: <reason>` so the next launch can read the result.
- On startup, the new build compares `APP_VERSION` to those flags and:
  - **Success path:** Windows toast notification *"GhostShell updated to v2.9.4"* + green in-app banner confirming the upgrade landed.
  - **Failure path:** Windows toast *"Update install failed"* + red in-app banner explaining what happened (usually antivirus locking the file).
- The pending flags clear themselves after one read so the toast doesn't repeat on every launch.

The install batch itself is hardier:
- Waits up to 60 s for the running PID (was 30 s) — antivirus first-launch scans take longer than expected.
- 20 retries × 1 s on the copy (was 10) — survives transient locks.
- Always launches *something* at the end, even on failure, so you're never left with no window.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

If you're already on v2.9.0+, the auto-updater on the running build will detect this release within 6 hours (or hit ⟳ in the title bar to check now). Click **Install & Restart** in the toast.

**This time you'll know whether it worked** — when the new build comes up, you'll see a green *"Updated to v2.9.4"* banner near the top of the window for 8 seconds.
