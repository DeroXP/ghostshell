# GhostShell v2.9.9.2

Three significant fixes — manual sliders, benchmark stress shader, and **GPU-crash recovery**.

## 🎚 Manual sliders actually use the new max

In v2.9.9.1 the backend clamps were bumped to 950 / 4500 MHz, but a leftover line in the JS (`coreSlider.max = 400; memSlider.max = 2000`) was overwriting the HTML attribute back to the old defaults on every page load. So your sliders kept showing +400 / +2000.

**Fix:** the JS now reads `limits.core_max_offset` / `limits.mem_max_offset` from the `/api/gpu/oc/capability` response (newly exposed in this release) and applies them to the slider's `max` attribute. Sliders go to **+950 / +4500** as intended.

## 🔥 Benchmark stress shader matches stress-test.py

The Benchmark Tune stress shader was a relatively cheap mandelbrot + noise loop — useful for crash detection but not load-equivalent to the reference `stress-test.py` script. Scores between GhostShell and the script weren't directly comparable.

**v2.9.9.2 ports the shader verbatim from your stress-test.py reference:**

- 18-iteration rotating noise loop (matrix rotations + sin/cos + hash-based noise per iter)
- 14-iteration fractal-fold loop (`q = abs(q) / dot(q, q + 0.7) - 0.6`)
- `pow(col, vec3(0.85))` final tone-map
- The 4×4 deterministic checksum block at the bottom-left is preserved on top so the pixel-correctness GPU-crash detector still works

This is **roughly 6× the GPU work per pixel** — your card will reach the same throttling / TDP / clock-dip behaviour it sees in the reference script, so scores are directly comparable.

## 🛡 GPU-crash detection + auto-recovery

You said *"my GPU disconnects for a little bit or my screen just goes black for a little bit"*. The previous build would just mark that step "unstable" and march on to the next offset, often crashing the GPU again.

v2.9.9.2 turns crash recovery into a first-class system:

### Detection signals (any one of these triggers)

| Signal | Where | Latency |
|---|---|---|
| WebGL context lost | Frontend `webglcontextlost` event | < 50 ms |
| Pixel checksum mismatch (≥2 frames) | Frontend pixel readback | < 1 s |
| nvidia-smi non-response | Backend `read_live_state()` | ~5 s timeout |
| nvidia-smi gap > 5 s during probe | Backend watchdog | next probe tick |
| Driver TDR (nvlddmkm 153 / 4101) | Backend Event Log scan | every 5th tick |

### What happens on detection

1. **Backend** records the crash in `_crash_state` (kind, offset, timestamp).
2. **`emergency_reset_oc()` fires immediately** — best-effort all-paths reset:
   - VF curve → 0
   - Sparse SetPstates20 graphics → 0
   - Sparse SetPstates20 memory → 0
   - `nvidia-smi -rgc` and `-rmc` to clear any clock locks
   - Each step in its own try/except so a degraded NVAPI can't block the others
   - Returns `ok: True` if AT LEAST ONE path landed
3. **Frontend** receives `kind=context_loss/hang/tdr/crash` from the probe verdict and:
   - Hard-stops the auto-tune / benchmark loop (no more steps)
   - Waits 4 seconds for the GPU to settle
   - Pops a red **"⚠ Auto-tune stopped"** banner with the crashing offset and reason
4. **Belt-and-braces:** the WebGL `webglcontextlost` event handler ALSO POSTs `/api/gpu/oc/emergency-reset` directly, so reset fires even if the probe-tick path is blocked.

### After-the-fact recovery

When you come back to GhostShell after the screen has gone black and recovered, the page-load handler polls `/api/gpu/oc/crash-state`. If a crash happened in the last 10 minutes, you get a banner like:

> ⚠ Recent GPU crash detected
> GPU recovered from a context_loss at core+275 / mem+1500 (47s ago).
> NVAPI offsets were automatically reset to stock. Try a smaller offset next time.

### New API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/gpu/oc/emergency-reset` | POST | Best-effort, never-raises reset |
| `/api/gpu/oc/crash-state` | GET | Most recent crash record (for the UI banner) |
| `/api/gpu/oc/crash-state` | POST | Acknowledge / clear the recent crash |

### How crash-recovery makes Benchmark Tune actually safe

Before v2.9.9.2, a Benchmark Tune session that crashed at core+225 would treat that as "unstable" and try core+250 next — guaranteed to crash again, possibly worse. Now the session **stops immediately on crash**, leaves the GPU at stock, and shows you the banner with the failing offset. Re-run with a lower `core_max_offset` if you keep hitting the same wall.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading:
- Manual OC sliders go to +950 / +4500 (no more 400/2000 cap)
- Benchmark Tune now uses the heavier ray-march shader from your reference script
- Any GPU crash during Quick Tune or Benchmark Tune triggers an immediate reset + a clear red banner — no more silently grinding through more bad offsets after a black screen
