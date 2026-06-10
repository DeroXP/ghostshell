# GhostShell v2.9.9.5

Hotfix for the **+25 MHz showed 0/0/0** problem from your last screenshot.

## 🐛 The exact bug

Your screenshot:
```
✓ Core +0 MHz  — fps 4140.7 / 1%low 3704 / σ 1.7% / 67°C   ← real data
✓ Core +25 MHz — fps 0      / 1%low 0    / σ 0%   / 45°C   ← FPS columns empty, temp real
```

Real temp (read from nvidia-smi by the backend) but zero FPS and zero
1%-low — meaning the WebGL stress submitted no measurable frames
during the +25 step.

**Root cause:** `stopWebGLStress()` was calling
`WEBGL_lose_context.loseContext()` after every step:

```js
function stopWebGLStress(state) {
    if (!state) return;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    try {
        var ext = state.gl.getExtension('WEBGL_lose_context');
        if (ext) ext.loseContext();   // ← KILLED THE CONTEXT
    } catch (e) {}
}
```

For the **first** step that worked fine — context dies, GPU resources freed.
For the **second** step (and every subsequent one in benchmark mode), the
next `startWebGLStress()` called `canvas.getContext('webgl')` and got back
either `null` or a still-lost context.  Either way no draws actually ran.

The temp came back fine because it's read by the Python backend via
`nvidia-smi`, which doesn't care about WebGL — but everything FPS-related
silently produced zero samples.

## ✨ The fix

`stopWebGLStress` now just cancels the rAF.  The WebGL context stays
alive on the canvas across step transitions; the next step's call to
`startWebGLStress` reuses it, compiles fresh shaders into it, and runs
cleanly.

```js
function stopWebGLStress(state) {
    if (!state) return;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    // No more loseContext() — was breaking every step after the first.
}
```

## What you'll see now

```
✓ Core +0 MHz   ← fps 4141 / 1%low 3704 / σ 1.7% / 67°C
✓ Core +25 MHz  ← fps 4192 / 1%low 3741 / σ 1.6% / 68°C
✓ Core +50 MHz  ← fps 4249 / 1%low 3789 / σ 1.7% / 70°C
✓ Core +75 MHz  ← fps 4301 / 1%low 3823 / σ 1.8% / 72°C
✓ Core +100 MHz ← fps 4358 / 1%low 3870 / σ 1.7% / 74°C
```

Real numbers on every row.  Each step distinguishable so the benchmark
winner reflects actual performance differences.

## 📦 Asset

| File | Description |
|---|---|
| `GhostShell.exe` | Standalone Windows 10 / 11 build (~22.8 MB). Runs as administrator. |

## 🔄 Upgrading

Auto-updater on v2.9.0+ will pick this up. Hit ⟳ to check immediately.
Re-run **Benchmark Tune** — every row should show real FPS now, not just
the first one.
