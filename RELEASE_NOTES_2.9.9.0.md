# GhostShell v2.9.9.0

Big update: a brand-new **Benchmark Tune** auto-OC mode, faster startup, wider OC slider range, and a UI compaction pass.

## ✨ Two auto-OC modes

The Auto Overclock card now has two buttons:

### Quick Tune (renamed, redesigned)
The familiar binary-search algorithm but with significantly tightened durations — same convergence guarantees, ~40 % faster overall:

| Phase | v2.9.8 | v2.9.9.0 |
|---|---|---|
| Jump steps | 18 s | **10 s** |
| Refinement | 25 s | **15 s** |
| Final validation | 60 s | **30 s** |
| Total typical | 5–8 min | **3–5 min** |

Crashes surface within ~8 s under heavy WebGL stress; the longer windows mostly burned time without finding new instability.

### Benchmark Tune (NEW — what you asked for)

Quick Tune asks *"how high can the GPU go before crashing?"*  
Benchmark Tune asks *"which offset gives the **best actual performance**?"*  
These are different questions — pushing further isn't always faster (thermal throttle, voltage drops, driver-side caps).

**How it works:**

1. **Maxes the power limit first** so TDP throttling doesn't skew measurements.
2. **Walks the CORE offset ladder** `[0, 50, 100, 150, 200, 250, 300]` MHz.  
   For each step:
   - Apply offset → 23 s WebGL benchmark
   - Record `avg_fps`, `1%-low fps`, `frametime σ`, `max_temp`
   - Compute a score using the same formula as the reference `stress-test.py`:
     ```
     score = avg_fps × 100
           + min_fps × 40           ← 1% lows weighted heavily
           − frametime_std × 20     ← variance penalised
           − thermal penalty
     ```
   - Crash → STOP this axis (anything higher will also crash)
3. **Picks the highest-scoring stable core offset** as the winner.
4. **Repeats the same process for MEMORY** with ladder `[0, 250, 500, 750, 1000, 1250, 1500]` MHz, applied on top of the core winner.
5. **Final 30 s validation pass** with both winners.
6. **Saves the winning combo** to your OC profile and applies it.

At the end, the UI shows a ranked table of every step tried, with the winner highlighted:

```
CORE LADDER RESULTS
Offset      Score    Avg FPS  1% Low   FT σ ms   Temp     Stable
core+0      14820.3  118.4    87.1     0.42      67°C     ✓
core+50     15412.1  121.7    91.2     0.39      68°C     ✓
core+100    16104.5  127.3    96.5     0.36      71°C     ✓
core+150    16221.0  129.1    97.8     0.41      74°C     ✓   ← winner
core+200    15890.2  130.4    92.1     0.68      78°C     ✓
core+250    0        0        0        0         62°C     ✗ tdr
```

Total runtime: 5–7 minutes. Use it when you actually want the most-FPS-not-just-most-stable result.

## 📊 Other improvements

### Wider OC slider ranges
- Core: was 0–400 MHz, **now 0–600 MHz**
- Memory: was 0–2000 MHz, **now 0–3000 MHz**

The old limits were conservative for older Pascal/Turing parts. Modern Ada/Blackwell + GDDR7 regularly take +500 MHz core and +2500 MHz mem.

### Faster startup
- `time.sleep(0.5)` after Flask thread → replaced with a port-poll loop that exits as soon as Flask actually accepts a connection. **Saves ~400 ms.**
- First-paint API calls split: dashboard loads immediately; secondary widgets (notifier status, boot-prep poll) defer to the next animation frame so the UI feels snappier.

### UI compaction
- Card padding tightened from 16 px → 12 px (vertical) and margin from 12 px → 10 px. Free ~30 px reduction on every tab without changing layouts.
- **Profiles tab:** "Active Game" merged into the Detection Engine card; the long "What Happens When a Game is Detected" explainer is now collapsed inside a `<details>` block (click to expand). Saves ~200 px by default.
- **GPU tab:** four narrow vendor cards collapsed into two wider ones. NVIDIA + Clean-Install in one card; AMD + driver tips in the other.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.

After install, head to **GPU → Auto Overclock**. The new **Benchmark Tune** button is right next to **Quick Tune**.
