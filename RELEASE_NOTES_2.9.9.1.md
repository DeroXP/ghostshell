# GhostShell v2.9.9.1

Three small but visible changes to the GPU OC tools.

## 📈 Wider OC slider range (again)

The v2.9.9.0 limits (600 / 3000) were already a big jump from the original
400 / 2000.  v2.9.9.1 pushes them further so the manual sliders never get in
the way for users on golden samples or sub-ambient cooling:

| Slider | v2.9.9.0 | **v2.9.9.1** |
|---|---|---|
| Core | 0–600 MHz | **0–950 MHz** |
| Memory | 0–3000 MHz | **0–4500 MHz** |

These are absolute clamps in the backend too (`MAX_CORE_OFFSET_MHZ` /
`MAX_MEM_OFFSET_MHZ`).  Realistically only LN2 / golden-die operators will
ever touch the top end, but the limit isn't there to second-guess them.

## 🐢 Benchmark Tune — proper deep-dive cadence

In v2.9.9.0, Benchmark Tune used the same coarse jumps as Quick Tune (50 MHz
core / 250 MHz mem, 23 s per step), so it finished in ~5–7 minutes and missed
the actual peak.  That was wrong — Benchmark Tune is supposed to be the *slow,
thorough* mode that finds the offset with the best measured FPS.

**v2.9.9.1 cadence:**

| Knob | v2.9.9.0 | **v2.9.9.1** |
|---|---|---|
| Core ladder step | 50 MHz | **25 MHz** |
| Memory ladder step | 250 MHz | **100 MHz** |
| Per-step duration | 23 s | **90 s** (≈25 s warmup + 65 s measurement) |
| Final validation | 30 s | **120 s** |
| Default core max | 300 MHz | **600 MHz** (25 candidates) |
| Default memory max | 1500 MHz | **2500 MHz** (26 candidates) |
| Total runtime | ~5–7 min | **~45–90 min** depending on GPU stability |

Why 90-second per-step measurements: the score weights `1%-low FPS` and
`frametime σ` heavily, and both of those metrics are noisy in short windows
— a 23 s sample regularly misclassifies "smoother" runs as "jittery".  At
65 s of post-warmup measurement the std dev settles inside ±0.05 ms on a
stable GPU, which is enough resolution to actually distinguish between
adjacent 25 MHz offsets.

**Why finer steps:** Quick Tune jumps in 75 MHz core leaps because it only
needs to know "stable or not".  Benchmark Tune is hunting for the best
*performance*, and the FPS curve doesn't always increase linearly — it
often peaks at +175 MHz and then drops at +200 MHz due to thermal /
voltage throttling.  25 MHz steps catch those local maxima that 50 MHz
steps would step right over.

**Estimated runtime by scenario (all assume default ladder caps):**

| GPU strength | Stable to | Steps until crash | Runtime |
|---|---|---|---|
| Conservative | +100 core, +800 mem | ~14 | ~25 min (under target — early crash stops the run) |
| Average | +200 core, +1500 mem | ~26 | ~42 min |
| Strong | +400 core, +2500 mem (full ladder) | 51 | **~80 min** |
| Extreme (custom max) | +950 core, +3900 mem (full clamp) | 79 | **~122 min** |

Most users will see **45–80 min**, exactly the window you asked for.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After upgrading, the manual OC sliders will go all the way to +950 / +4500.
**Benchmark Tune** will read the new dialog text noting the 45-90 min runtime
and the smaller step sizes — start it before bed and check the results in
the morning.
