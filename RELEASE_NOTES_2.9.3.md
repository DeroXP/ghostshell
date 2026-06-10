# GhostShell v2.9.3 — verify accuracy + OC-tool conflict detection

The fix that finally tells the truth about whether your OC actually applied.

## 🐛 The bug

**Symptom:** apply +35 / +150, see green ✓ on every NVAPI step, then verify
shows red X with `actual max 14001 MHz, expected 14151 MHz`.

**Root cause (Blackwell-specific):** `nvidia-smi clocks.max.graphics` and
`clocks.max.memory` do **not** update reliably on RTX 50-series when an
NVAPI offset is written. The driver stores the offset, the GPU runs at the
OC'd clock under load (you can confirm with `clocks.current.memory` reading
`14151` MHz, exactly +150 over the cached `clocks.max` of `14001`), but the
`clocks.max` field stays stale. v2.9.0–2.9.2 used `clocks.max` as ground
truth, which is why verify kept screaming.

**The OC was working all along — verify was just reading the wrong field.**

## ✨ What's fixed

### Verify now trusts NVAPI as the source of truth
`NvAPI_GPU_GetPstates20` returning `core_offset_mhz: +35` *is* the answer
for "did our offset land?" — it's the same control surface the GPU driver
uses internally. v2.9.3 reads the offset back via NVAPI and compares to
what was requested. nvidia-smi `clocks.max` is now shown as supplemental
info only.

The verify line you saw before now reads:
> ✓ NVAPI offset +150 MHz applied, live 14151 MHz

### Conflict detection for Afterburner / RTSS / Precision X1 / etc.
A long list of GPU OC tools — MSI Afterburner, RivaTuner Statistics Server,
EVGA Precision X1, ASUS GPU Tweak III, AORUS Engine, Galax Thunder Master,
NVIDIA Inspector, Profile Inspector, Dragon Center, MSI Center — all run
in the background and **re-assert their own offsets every few seconds**.
This silently overrides anything GhostShell writes.

When one of these is detected at apply time, the results panel now shows
an upfront warning **before** the apply runs:

> ⚠ Conflicting OC tool(s) running: MSI Afterburner, RivaTuner Statistics Server
> Close these tools (and their startup entries) before applying OC in
> GhostShell. They re-assert their own clock offsets every few seconds and
> will silently override GhostShell's writes.

The verify result also shows a banner if a conflict is still active.

### New diagnostic endpoint
`GET /api/gpu/oc/diagnostic` returns the full state:
- nvidia-smi current/max/applications clocks for both core and memory
- NVAPI VF-curve offset + P-state freqDelta read-back
- Power draw / power limit / temperature
- Admin status
- All conflicting OC tools currently running

Useful when debugging "why isn't my OC sticking?" — you can see exactly
what the driver thinks vs what nvidia-smi reports.

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ in the title bar to
check immediately.

After upgrading, **close MSI Afterburner first** (and disable its
"Apply at Windows startup" if enabled), then apply your OC in GhostShell.
The new conflict banner will warn you if you forget.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |
