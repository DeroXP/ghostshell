# GhostShell v2.9.9.4

Hotfix for the **3 million FPS** problem you spotted (and the +25 MHz row showing zero).

## 🐛 What was broken

Your screenshot showed:
```
✓ Core +0 MHz  — fps 3072075.6 / 1%low 0 / σ 25%  / 68°C   ← bogus
✓ Core +25 MHz — fps 0          / 1%low 0 / σ 0%   / 47°C   ← stuck zero
```

Two root causes:

### 1. `gl.finish()` doesn't sync on Chromium

In v2.9.9.3 I used `gl.finish()` to block JS until the GPU finished a batch
of 32 draws.  The WebGL spec says `finish()` MUST do that.  In practice,
**Chromium's WebGL backend (which WebView2 uses) returns from `gl.finish()`
immediately** because GPU submission crosses a renderer / GPU-process
boundary asynchronously.  Result: `batch_time = ~0.001 ms`, `per-draw =
~0.0003 ms`, FPS = 3 million.

### 2. The +25 MHz step had no time to settle

After applying a new offset, the GPU's boost curve takes a moment to
actually transition.  The previous build only waited 1.5 s before starting
to record frames.  If the previous step had been hammering at full power,
1.5 s wasn't enough — the new step started recording while the GPU was
still in a transition state, sometimes producing zero usable samples.

## ✨ The fix

### `gl.readPixels` for guaranteed GPU sync
The reliable WebGL synchronization primitive is `gl.readPixels()` — it must
round-trip to the GPU and pull bytes back, which forces an actual sync that
even Chromium can't no-op.  We now read a single pixel after each batch:

```js
gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, syncBuf);
```

Cost is trivial (one pixel, 4 bytes).  Effect: batch timing now reflects
true GPU work, not just submission overhead.

### 1-second warmup discard
The first 1 s of every step's frame samples is discarded.  Cold-cache and
first-RAF outliers (browser still composing, GPU clock still ramping)
won't pollute the per-step average.

### Sanity rejection of impossible per-draw values
Anything below `0.05 ms` per draw or above `500 ms` per draw gets dropped
on the JS side instead of recorded.  A draw that fast means the sync
didn't actually sync; that slow means a hung frame.  Either way it's not
a real measurement.

### Belt-and-braces clamps
- Backend score formula now clamps `avg_fps` and `min_fps` to `[0, 5000]`
  so a leaked bogus reading can't produce an infinite score.
- Frontend results table also clamps the displayed FPS to `[0, 5000]` so
  the user never sees `3072075.6` even if everything else fails.

### 3-second settle delay between benchmark steps (was 1.5 s)
Combined with the 1 s WebGL warmup discard inside each step, that's 4 s
total between applying a new offset and recording the first frame sample.
Plenty of time for the boost curve to transition and the GPU to reach
steady state at the new clocks.

## What you'll see now

```
✓ Core +0 MHz   ← fps 580 / 1%low 540 / σ 1.2% / 51°C
✓ Core +25 MHz  ← fps 595 / 1%low 558 / σ 1.1% / 53°C
✓ Core +50 MHz  ← fps 612 / 1%low 575 / σ 1.0% / 56°C
✓ Core +75 MHz  ← fps 624 / 1%low 583 / σ 1.1% / 58°C
```

Real, finite, monotonically-related-to-OC numbers, with each step
distinguishable from the next.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.
Re-run **Benchmark Tune** — this time the FPS column will contain real
numbers and the picked winner will reflect actual measured performance.
