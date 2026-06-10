# GhostShell v2.9.1 — hotfix

Hotfix for `NVAPI_NOT_SUPPORTED (-104)` when applying OC offsets on Blackwell
(RTX 5070 Ti, RTX 5080, RTX 5090) with driver 596.x.

## 🐛 Fixes

### `SetPstates20` rejected on Blackwell when sending core + memory together
v2.9.0 read the live P-state blob, modified the core *and* memory clock
entries, then wrote the whole thing back.  The Blackwell driver rejects
this round-trip with `-104 NVAPI_NOT_SUPPORTED` — it wants a *sparse*
request that touches only the fields you intend to change.

**Fix:** each clock domain now gets its own NVAPI call:
- **Core clock** → `NvAPI_GPU_SetClockBoostTable` (V/F-curve write — the
  only path that actually moves the boost ceiling on Boost-3.0+ GPUs)
- **Memory clock** → `NvAPI_GPU_SetPstates20` with `numPstates=1, numClocks=1`
  (sparse memory-only request)

Splitting the calls means a failure in one domain no longer takes down the other,
and the driver no longer sees a "review every field" packet.

### Aggressive reset to clear stuck OC state
Previously, if a v2.9.0 user ended up with a stuck offset from a failed
write (e.g. `+2000 MHz` memory persisting across sessions even after
clicking Reset), the reset path also went through the same broken
SetPstates20 round-trip and quietly failed.

**Fix:** the new `force_reset_all()` clears the V/F curve AND every
P-state freqDelta independently — if any single sub-step fails, the
others still run.  The Reset button in the UI now reports each NVAPI
sub-step individually so it's obvious what worked and what didn't.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading from v2.9.0

If you're already running v2.9.0, the auto-updater will pick this release up
on its next 6-hour check (or immediately on the next launch).  Click **Install
& Restart** in the toast and you're on 2.9.1.

If you had a stuck +2000 MHz memory offset from v2.9.0, click **Reset to
Stock** after upgrading — the new aggressive reset will clear it.
