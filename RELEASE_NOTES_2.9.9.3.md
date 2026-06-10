# GhostShell v2.9.9.3

Hotfix for the Benchmark Tune FPS-cap problem you spotted.

## 🐛 What was wrong

Your screenshot showed `Core +0 MHz — fps 244 / 1%low 244 / σ 0.1% / 48°C`.
Every offset was reporting **244 FPS** — which is exactly your monitor's
refresh rate (240 Hz + the overscan a vsync'd browser sees).  Because every
candidate scored identically, the benchmark always picked +0 MHz (ties broke
toward the first candidate in the ladder).

**Root cause:** the WebGL stress loop counted `requestAnimationFrame`
callbacks as "frames", and rAF is vsync-locked.  Even though we drew 4 GPU
passes per RAF, only one frame got counted per refresh tick.  So the FPS
number was a measure of **your monitor**, not your GPU.

## ✨ The fix

Three changes to the WebGL stress loop:

### 1. Per-draw counting instead of per-RAF counting
- **Before:** `frameCount++` once per RAF callback → max 244 FPS
- **After:** `frameCount += BATCH_SIZE` (32) per RAF → reports actual
  draws-per-second, uncapped from refresh

### 2. Bigger batch + GPU sync for accurate timing
- 32 draws submitted per RAF callback (was 4)
- `gl.finish()` once at end of batch — blocks JS until all 32 GPU draws
  complete, so wall-clock timing reflects real GPU time
- Per-draw frame time = `batch_time / BATCH_SIZE` → variance and 1%-low
  calculations now have real per-draw resolution

### 3. Higher canvas internal resolution
- **Before:** 512 × 512 (262 K pixels) — too small for modern GPUs to
  saturate, finished a draw in <1 ms
- **After:** 1280 × 1280 (1.6 M pixels) — ~6.4× more fragment work per
  draw.  Combined with the heavy ray-march shader from v2.9.9.2, a single
  draw now takes 1–4 ms on most cards, so a batch of 32 takes 32–128 ms
  per RAF — well above the 16.6 ms vsync budget, guaranteeing GPU-bound
  measurement.

### 4. Bigger frame-time ring buffer
- **Before:** 600 entries — at the new draw rate that was only ~0.6 s of
  history, making the 1%-low metric noisy
- **After:** 5000 entries — gives 5–25 s of history depending on GPU speed

## What you'll see

For your RTX 5070 Ti the FPS numbers will now look something like:

```
[core_each] Core +0 MHz   ← fps 580 / 1%low 540 / σ 1.2% / 51°C
[core_each] Core +25 MHz  ← fps 595 / 1%low 558 / σ 1.1% / 53°C
[core_each] Core +50 MHz  ← fps 612 / 1%low 575 / σ 1.0% / 56°C
[core_each] Core +75 MHz  ← fps 624 / 1%low 583 / σ 1.1% / 58°C
[core_each] Core +100 MHz ← fps 638 / 1%low 596 / σ 1.0% / 60°C
...
```

Each row will be visibly different from the previous one (1–3 % per 25 MHz
on a typical card), so the highest-scoring offset can actually be picked
based on real performance differences instead of all candidates tying at
your monitor's refresh rate.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading, re-run **Benchmark Tune**.  This time you'll see different
FPS numbers for each offset, and the winner will reflect the actual
best-performing setting — not the one that ties everyone else at your
monitor's refresh rate.
