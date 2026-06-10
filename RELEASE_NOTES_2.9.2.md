# GhostShell v2.9.2 — hotfix

Targeted fix for "NVAPI says OK but the GPU's reported max clock doesn't move"
on Blackwell (RTX 50-series) and a few other quality-of-life clean-ups in the
overclock apply path.

## 🐛 Fixes

### Removed the misleading `nvidia-smi -lgc / -lmc` "fallback"
When NVAPI returned OK but the verify caught that the boost ceiling didn't
move, GhostShell would dutifully run `nvidia-smi -lgc 210,3160` and
`-lmc 405,14351` and log them as "✓ Fallback applied". On consumer GeForce
**these commands only set a clock CAP, not an offset** — so when the cap is
above the hardware native max, they no-op silently. The green check marks
were giving users false reassurance.

The fallback is gone. When NVAPI accepts the write but the boost ceiling
doesn't move, you now see an honest diagnostic instead:

> ⚠ Verification failed — driver did not honor NVAPI write
> NVAPI accepted the writes but the GPU's reported max clock did not move.
> core: nvidia-smi max 3090 MHz (VF read-back +70 MHz);
> memory: nvidia-smi max 14001 MHz (P-state read-back +0 MHz).
> This is a known Blackwell-driver limitation: try a newer NVIDIA driver,
> or use MSI Afterburner / NVIDIA Inspector for core overclocking.

### Core OC now writes the V/F curve AND the P-state freqDelta
MSI Afterburner and NVIDIA Inspector both write **two** NVAPI surfaces for
core OC: the V/F boost table (shifts the curve) and the P-state graphics
freqDelta (locks the floor). v2.9.1 only wrote the V/F curve. On some
driver versions, both writes are required for the boost ceiling to actually
move. v2.9.2 writes both, in two independent sparse calls, so a failure
in one path no longer takes down the other.

### Read-back diagnostic added
After every NVAPI apply, GhostShell now reads back:
- The V/F curve offset
- The P-state graphics freqDelta
- The P-state memory freqDelta

These values are surfaced in the verify result so it's obvious when the
driver stored the write but didn't translate it to a real clock change
(the Blackwell behaviour above).

### Aggressive Reset to Stock
The Reset button now uses the new `force_reset_all()` path that clears the
V/F curve **and** every P-state freqDelta independently. If one sub-step
fails (privileges, struct mismatch), the others still run. Each sub-step is
reported in the UI so it's clear which lever moved.

## 🔄 Upgrading from v2.9.0 / v2.9.1

The auto-updater on v2.9.0+ will pick this up on its next 6-hour check, or
hit the ⟳ icon in the title bar to check immediately. After installing,
click **Reset to Stock** once to clear any stuck offsets from earlier
sessions before applying a new OC.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |
