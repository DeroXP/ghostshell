"""GPU Overclocking — safe, reversible core/memory/power tuning with stability testing.

Supports NVIDIA (primary via nvidia-smi) and AMD (power limit via WMI/registry;
full OC requires user-installed MSI Afterburner or OverdriveNTool).

Overall flow:
  1. detect_gpu() → vendor, name, max/min clocks, max/min power
  2. read_live_state() → current clocks, temp, power draw, utilization
  3. apply_oc(core_mhz, mem_mhz, power_pct) → apply with safety clamps
  4. reset_oc() → fully revert to stock
  5. run_stability_test() → JS-side WebGL kicks off; Python polls nvidia-smi
  6. auto_overclock() → step up in increments, test stability, save best safe profile
  7. save/load/apply_profile() → JSON in APPDATA; applied on GhostShell startup

Safety rails:
  * Core offset clamped to +250 MHz max (most GPUs stable up to +150 MHz)
  * Memory offset clamped to +1500 MHz max (GDDR6/6X typical headroom)
  * Power limit clamped to GPU's own reported min/max
  * Temp abort at 85°C — auto-rolls back and marks step unstable
  * Clock-lock via `nvidia-smi -lgc` always includes a minimum so idle behavior works
"""
import json
import os
import threading
import time
from typing import Optional
from core.utils import run_cmd, run_ps, get_logger, atomic_write_json
from config import APPDATA_DIR

# NVAPI integration — used for TRUE clock offsets on consumer GeForce.
# (nvidia-smi -lgc/-lmc only sets clock CAPS on consumer cards, not real OC offsets.)
try:
    from core import nvapi_oc as _nvapi
except Exception as _e:
    _nvapi = None

log = get_logger("gpu_oc")

OC_PROFILE_PATH       = os.path.join(APPDATA_DIR, "gpu_oc_profile.json")
OC_HISTORY_PATH       = os.path.join(APPDATA_DIR, "gpu_oc_history.json")
# v2.9.4 — true stock baseline cache.
# nvidia-smi reports `clocks.max.graphics` as (base + active OC offset).
# When another OC tool (Afterburner) has applied a residual offset, we'd
# wrongly treat the inflated value as stock and end up showing wrong
# targets ("base 3090" when the user's actual base is 2865).  This file
# remembers the value with the offset *backed out* so subsequent runs
# always see real stock, even if the OC tool re-applies its boost.
STOCK_BASELINE_PATH   = os.path.join(APPDATA_DIR, "gpu_stock_baseline.json")

# v2.9.9.1 — extreme-headroom clamps for users on golden samples / sub-ambient
# cooling.  Almost no card will reach these in practice (a great Blackwell die
# tops out around +500-600 core / +3000 mem on air) but the clamps no longer
# get in the way for the lucky few.  Auto-tune still stops well before these.
MAX_CORE_OFFSET_MHZ = 950   # was 600 — accommodates LN2 / golden-sample territory
MAX_MEM_OFFSET_MHZ  = 4500  # was 3000 — headroom for binned GDDR7
MIN_POWER_PCT = 50   # never go below 50% power draw
MAX_POWER_PCT = 120  # above this is rarely possible anyway
TEMP_ABORT_C = 87    # stability abort threshold (raised slightly for performance mode)
TEMP_WARN_C = 80     # warn the user


def _clamp(val, lo, hi):
    """Clamp val to [lo, hi]."""
    return max(lo, min(hi, val))


# v2.9.3 — detect other OC tools running.
# Symptom: GhostShell's NVAPI writes return OK but `clocks.max.graphics` /
# `clocks.max.memory` don't change.  The most common cause is MSI Afterburner
# (or its RTSS companion, EVGA Precision, ASUS GPU Tweak, etc.) running in
# the background and re-asserting its own offsets every few seconds.
_OC_TOOL_PROCESSES = {
    "msiafterburner.exe":         "MSI Afterburner",
    "rtss.exe":                   "RivaTuner Statistics Server (Afterburner)",
    "rtssharedmemoryreader.exe":  "RTSS",
    "evgaprecisionx1.exe":        "EVGA Precision X1",
    "evgaprecisionx.exe":         "EVGA Precision X",
    "gputweakiii.exe":             "ASUS GPU Tweak III",
    "gputweakii.exe":              "ASUS GPU Tweak II",
    "aorus_engine.exe":           "Gigabyte AORUS Engine",
    "firestorm.exe":              "Zotac FireStorm",
    "nvidiainspector.exe":        "NVIDIA Inspector",
    "nvidiaprofileinspector.exe": "NVIDIA Profile Inspector",
    "thunder_master.exe":         "Galax Xtreme Tuner / Thunder Master",
    "msi_dragon_center.exe":      "MSI Dragon Center",
    "msi_center.exe":              "MSI Center",
}


def get_oc_diagnostic() -> dict:
    """Comprehensive snapshot of every OC-relevant signal.

    Designed for the new v2.9.3 "Diagnostic" panel — shows:
      * nvidia-smi current + max + applications clocks (the *truth* the GPU reports)
      * NVAPI VF-curve offset (graphics V/F curve)
      * NVAPI P-state freqDelta for graphics + memory
      * Conflicting OC tools running
      * Admin status

    Lets the user (and us) instantly see whether the driver is honoring writes.
    """
    out = {
        "ok": True,
        "admin": False,
        "nvidia_smi": {},
        "nvapi": {},
        "conflicts": detect_oc_tools_running(),
    }

    # Admin
    try:
        from core.utils import is_admin
        out["admin"] = is_admin()
    except Exception:
        pass

    # nvidia-smi snapshot
    state = read_live_state()
    if state.get("ok"):
        out["nvidia_smi"] = {
            "core_max_mhz":  state.get("core_max_mhz"),
            "core_cur_mhz":  state.get("core_mhz"),
            "core_app_mhz":  state.get("core_app_mhz"),
            "mem_max_mhz":   state.get("mem_max_mhz"),
            "mem_cur_mhz":   state.get("mem_mhz"),
            "mem_app_mhz":   state.get("mem_app_mhz"),
            "power_draw_w":  state.get("power_w"),
            "power_limit_w": state.get("power_limit_w"),
            "temp_c":        state.get("temp_c"),
        }
    else:
        out["nvidia_smi"] = {"err": state.get("err", "nvidia-smi failed")}

    # NVAPI snapshot
    if _nvapi is not None and _nvapi.is_available().get("ok"):
        try:
            cur = _nvapi.get_current_offsets()
            out["nvapi"] = {
                "ok":              cur.get("ok", False),
                "core_offset_mhz": cur.get("core_offset_mhz"),
                "mem_offset_mhz":  cur.get("mem_offset_mhz"),
                "via_vf_curve":    cur.get("core_via_vf_curve"),
                "struct_version":  cur.get("struct_version_used"),
                "err":             cur.get("err"),
            }
        except Exception as e:
            out["nvapi"] = {"err": str(e)}
    else:
        out["nvapi"] = {"err": "NVAPI not available"}

    return out


def detect_oc_tools_running() -> dict:
    """Scan running processes for known GPU OC / monitoring tools that
    may override or conflict with GhostShell's NVAPI writes.

    Returns:
        {'ok': bool, 'tools': [{'exe': 'msiafterburner.exe', 'name': '...'}, ...],
         'conflict_likely': bool}
    """
    r = run_ps(
        "Get-Process | Select-Object -ExpandProperty ProcessName "
        "| ConvertTo-Json -Compress",
        timeout=8,
    )
    if not (r["ok"] and r["out"]):
        return {"ok": False, "tools": [], "conflict_likely": False, "err": r.get("err", "")}

    try:
        names = json.loads(r["out"])
    except json.JSONDecodeError:
        return {"ok": False, "tools": [], "conflict_likely": False,
                "err": "Could not parse Get-Process output"}
    if not isinstance(names, list):
        names = [names]

    # Build lowercase set of running .exe names
    running = {(str(n) + ".exe").lower() for n in names if n}

    found = []
    for exe, label in _OC_TOOL_PROCESSES.items():
        if exe in running:
            found.append({"exe": exe, "name": label})

    return {
        "ok": True,
        "tools": found,
        "conflict_likely": len(found) > 0,
    }

# ═══════════════════════════════════════════════════════════════════════════
# Detection & monitoring
# ═══════════════════════════════════════════════════════════════════════════

def _nvidia_smi_query(fields: str) -> list:
    """Return a list of dicts queried from nvidia-smi. Empty on failure."""
    r = run_cmd(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        timeout=10,
    )
    if not r["ok"] or not r["out"]:
        return []
    rows = []
    for line in r["out"].splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) == len(fields.split(",")):
            rows.append(dict(zip([f.strip() for f in fields.split(",")], vals)))
    return rows


# ═══════════════════════════════════════════════════════════════════════════
# v2.9.4 — TRUE stock baseline detection
# ═══════════════════════════════════════════════════════════════════════════
# Why we need this: if Afterburner / a saved profile / leftover driver state
# is currently applying e.g. +225 MHz core, nvidia-smi reports `clocks.max.
# graphics = stock_base + 225`.  Treating that inflated value as "base" makes
# every target calculation wrong:
#     user requests +50 → UI shows "target 3140" but real target should be
#     2865 + 50 = 2915.  And reset-to-stock looks like a +225 OC.
#
# Fix: at startup, ask nvidia-smi what max is AND ask NVAPI what offset is
# active.  Subtract.  Persist the result so it survives reboots and is
# stable even when another tool keeps re-applying its OC.

def _load_stock_baseline() -> dict | None:
    """Read APPDATA/gpu_stock_baseline.json if it exists, else None."""
    if not os.path.exists(STOCK_BASELINE_PATH):
        return None
    try:
        with open(STOCK_BASELINE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("core_stock_mhz") and data.get("mem_stock_mhz"):
            return data
    except Exception as e:
        log.warning(f"Could not load stock baseline: {e}")
    return None


def _save_stock_baseline(data: dict):
    atomic_write_json(STOCK_BASELINE_PATH, data)


def _capture_stock_baseline() -> dict:
    """Compute the GPU's TRUE stock max clock by reading nvidia-smi and
    subtracting whatever NVAPI offset is currently active.

    Equation:  true_stock_max = nvidia_smi_max - nvapi_active_offset
    """
    rows = _nvidia_smi_query(
        "name,clocks.max.graphics,clocks.max.memory,power.default_limit"
    )
    if not rows:
        return {"ok": False, "err": "nvidia-smi not available"}
    r = rows[0]

    def _i(v, d=0):
        try: return int(float(v))
        except Exception: return d

    nvsmi_core_max = _i(r.get("clocks.max.graphics"))
    nvsmi_mem_max  = _i(r.get("clocks.max.memory"))

    nvapi_core_off = 0
    nvapi_mem_off  = 0
    if _nvapi is not None:
        try:
            if _nvapi.is_available().get("ok"):
                no = _nvapi.get_current_offsets()
                if no.get("ok"):
                    nvapi_core_off = int(no.get("core_offset_mhz", 0) or 0)
                    nvapi_mem_off  = int(no.get("mem_offset_mhz", 0) or 0)
        except Exception:
            pass

    if nvsmi_core_max <= 0 or nvsmi_mem_max <= 0:
        return {"ok": False, "err": "Could not read max clocks from nvidia-smi"}

    return {
        "ok": True,
        "name":            r.get("name", ""),
        "core_stock_mhz":  nvsmi_core_max - nvapi_core_off,
        "mem_stock_mhz":   nvsmi_mem_max  - nvapi_mem_off,
        "captured_at":     time.time(),
        "captured_with":   {
            "nvsmi_core_max":    nvsmi_core_max,
            "nvsmi_mem_max":     nvsmi_mem_max,
            "nvapi_core_offset": nvapi_core_off,
            "nvapi_mem_offset":  nvapi_mem_off,
        },
    }


def get_current_offsets() -> dict:
    """Public wrapper around NVAPI's applied core/mem offset read.
    Returns {ok, core_offset_mhz, mem_offset_mhz, ...} or
    {ok: False, err} when NVAPI is unavailable (non-NVIDIA GPU, no
    driver, etc.).

    3.4.2 — added because app.py's diagnostics snapshot + the AT drift
    check were referencing gpu_overclock.get_current_offsets() which
    didn't exist (the impl lives in nvapi_oc).  A hasattr() guard hid
    the miss, so the snapshot's current_offsets field was always None.
    Delegates to the same _nvapi call the watchdog uses internally."""
    if _nvapi is None:
        return {"ok": False, "err": "NVAPI module unavailable"}
    try:
        if not _nvapi.is_available().get("ok"):
            return {"ok": False, "err": "NVAPI not available on this GPU"}
        return _nvapi.get_current_offsets()
    except Exception as e:
        return {"ok": False, "err": str(e)}


def get_stock_baseline() -> dict:
    """Return the cached stock baseline, capturing one if missing.

    Public — also exposed via /api/gpu/oc/baseline for the UI.
    """
    saved = _load_stock_baseline()
    if saved:
        return {"ok": True, "from_cache": True, **saved}
    cap = _capture_stock_baseline()
    if cap.get("ok"):
        _save_stock_baseline(cap)
        log.info(f"Stock baseline captured: core={cap['core_stock_mhz']} MHz "
                 f"mem={cap['mem_stock_mhz']} MHz "
                 f"(nvsmi_core_max={cap['captured_with']['nvsmi_core_max']}, "
                 f"nvapi_core_offset={cap['captured_with']['nvapi_core_offset']})")
    return cap


def recalibrate_stock_baseline() -> dict:
    """Force a fresh stock-baseline capture, overwriting any cached value.

    Useful when the user has just installed a new driver / changed
    Afterburner state / wants to re-detect.
    """
    cap = _capture_stock_baseline()
    if cap.get("ok"):
        _save_stock_baseline(cap)
        log.info(
            f"Stock baseline recalibrated: core={cap['core_stock_mhz']} MHz "
            f"mem={cap['mem_stock_mhz']} MHz"
        )
    return cap


def detect_gpu_oc_capability() -> dict:
    """Detect GPU and report which OC features are available.

    Returns:
        {
          'ok': bool,
          'vendor': 'nvidia'|'amd'|'intel'|'unknown',
          'name': str,
          'supports': {'power_limit': bool, 'core_clock': bool, 'mem_clock': bool},
          'limits': {'power_min_w': int, 'power_max_w': int, 'power_default_w': int,
                     'core_max_mhz': int, 'mem_max_mhz': int},
          'err': str,
        }
    """
    # NVIDIA path
    rows = _nvidia_smi_query(
        "name,power.min_limit,power.max_limit,power.default_limit,clocks.max.graphics,clocks.max.memory"
    )
    if rows:
        r = rows[0]
        def _i(v):
            try:
                return int(float(v))
            except Exception:
                return 0

        nvsmi_core_max = _i(r.get("clocks.max.graphics"))
        nvsmi_mem_max  = _i(r.get("clocks.max.memory"))

        # v2.9.4 — use the cached TRUE stock baseline if we have one.
        # This way `core_max_mhz` and `mem_max_mhz` always reflect the
        # GPU's real base clock, not a value inflated by an active OC
        # offset from another tool.
        baseline = get_stock_baseline()
        if baseline.get("ok"):
            true_core_max = int(baseline["core_stock_mhz"])
            true_mem_max  = int(baseline["mem_stock_mhz"])
        else:
            true_core_max = nvsmi_core_max
            true_mem_max  = nvsmi_mem_max

        return {
            "ok": True,
            "vendor": "nvidia",
            "name": r.get("name", "NVIDIA GPU"),
            "supports": {
                "power_limit": True,
                "core_clock": True,
                "mem_clock": True,
            },
            "limits": {
                "power_min_w":     _i(r.get("power.min_limit")),
                "power_max_w":     _i(r.get("power.max_limit")),
                "power_default_w": _i(r.get("power.default_limit")),
                # core_max_mhz / mem_max_mhz are the TRUE stock — i.e. what
                # the card runs at with zero offset.  All target / verify
                # math is built on these.
                "core_max_mhz":    true_core_max,
                "mem_max_mhz":     true_mem_max,
                # v2.9.9.2 — expose the OFFSET ceilings the backend will
                # accept so the UI's manual sliders honour the new high
                # limits (950 / 4500) without being hardcoded in JS.
                "core_max_offset": MAX_CORE_OFFSET_MHZ,
                "mem_max_offset":  MAX_MEM_OFFSET_MHZ,
                # Expose the inflated current readings too in case the UI
                # wants to show "currently displayed by nvidia-smi" alongside.
                "core_smi_max_mhz": nvsmi_core_max,
                "mem_smi_max_mhz":  nvsmi_mem_max,
                "baseline_source":  "cache" if baseline.get("ok") else "nvidia-smi",
            },
            "err": "",
        }

    # AMD/Intel path — report limited support
    ps = run_ps(
        "Get-CimInstance Win32_VideoController | Select-Object Name | ConvertTo-Json",
        timeout=10,
    )
    if ps["ok"] and ps["out"]:
        try:
            d = json.loads(ps["out"])
            if not isinstance(d, list):
                d = [d]
            for a in d:
                name = (a.get("Name", "") or "").lower()
                if "radeon" in name or "amd" in name:
                    return {
                        "ok": True,
                        "vendor": "amd",
                        "name": a.get("Name", "AMD GPU"),
                        "supports": {
                            "power_limit": False,  # requires OverdriveNTool
                            "core_clock": False,
                            "mem_clock": False,
                        },
                        "limits": {
                            "power_min_w": 0, "power_max_w": 0, "power_default_w": 0,
                            "core_max_mhz": 0, "mem_max_mhz": 0,
                        },
                        "err": "AMD overclocking requires MSI Afterburner or OverdriveNTool installed separately",
                    }
                if "intel" in name or "iris" in name:
                    return {
                        "ok": True,
                        "vendor": "intel",
                        "name": a.get("Name", "Intel GPU"),
                        "supports": {"power_limit": False, "core_clock": False, "mem_clock": False},
                        "limits": {"power_min_w": 0, "power_max_w": 0, "power_default_w": 0, "core_max_mhz": 0, "mem_max_mhz": 0},
                        "err": "Intel integrated GPUs cannot be overclocked from software",
                    }
        except Exception as e:
            log.warning(f"WMI GPU detect parse failed: {e}")

    return {
        "ok": False,
        "vendor": "unknown",
        "name": "Unknown",
        "supports": {"power_limit": False, "core_clock": False, "mem_clock": False},
        "limits": {"power_min_w": 0, "power_max_w": 0, "power_default_w": 0, "core_max_mhz": 0, "mem_max_mhz": 0},
        "err": "No supported GPU detected",
    }


def read_live_state() -> dict:
    """Read real-time GPU stats. Fast — polls nvidia-smi once."""
    rows = _nvidia_smi_query(
        "clocks.current.graphics,clocks.current.memory,temperature.gpu,power.draw,"
        "utilization.gpu,utilization.memory,fan.speed,power.limit,"
        "clocks.applications.graphics,clocks.applications.memory,clocks.max.graphics,clocks.max.memory"
    )
    if not rows:
        return {"ok": False, "err": "No NVIDIA GPU / nvidia-smi missing"}

    r = rows[0]
    def _f(v, d=0.0):
        try:
            return float(v)
        except Exception:
            return d
    def _i(v, d=0):
        try:
            return int(float(v))
        except Exception:
            return d

    return {
        "ok": True,
        "core_mhz": _i(r.get("clocks.current.graphics")),
        "mem_mhz": _i(r.get("clocks.current.memory")),
        "temp_c": _i(r.get("temperature.gpu")),
        "power_w": _f(r.get("power.draw")),
        "power_limit_w": _f(r.get("power.limit")),
        "gpu_util_pct": _i(r.get("utilization.gpu")),
        "mem_util_pct": _i(r.get("utilization.memory")),
        "fan_pct": _i(r.get("fan.speed")),
        "core_app_mhz": _i(r.get("clocks.applications.graphics")),
        "mem_app_mhz": _i(r.get("clocks.applications.memory")),
        "core_max_mhz": _i(r.get("clocks.max.graphics")),
        "mem_max_mhz": _i(r.get("clocks.max.memory")),
    }


def verify_oc_applied(
    target_core_offset: int,
    target_mem_offset: int,
    target_power_pct: int,
    *,
    stock_core_max_mhz: int | None = None,
    stock_mem_max_mhz: int | None = None,
    stock_power_w: float | int | None = None,
) -> dict:
    """Read back the GPU's actual state and confirm the OC was genuinely applied.

    Verification strategy:
      Core  — compare the post-apply max clock against the pre-apply baseline
               plus the requested offset, then cross-check NVAPI's reported VF
               offset when available.
      Memory — compare the post-apply max memory clock against the pre-apply
               baseline plus the requested offset.
      Power  — compare the current power limit against the requested percentage
               of the pre-apply baseline/default power.

    The optional stock_* baselines should be captured before making changes.
    If they are omitted, the current values reported by nvidia-smi are used as
    a best-effort fallback.
    """
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"ok": False, "verified": False,
                "err": f"Verify only supported on NVIDIA, not {cap['vendor']}"}

    state = read_live_state()
    if not state["ok"]:
        return {"ok": False, "verified": False, "err": state.get("err", "live read failed")}

    baseline_core_max = int(stock_core_max_mhz or cap["limits"]["core_max_mhz"] or 0)
    baseline_mem_max = int(stock_mem_max_mhz or cap["limits"]["mem_max_mhz"] or 0)
    baseline_power_w = float(
        stock_power_w if stock_power_w is not None else cap["limits"]["power_default_w"] or 0
    )

    actual_core_max = int(state.get("core_max_mhz", 0))
    actual_mem_max = int(state.get("mem_max_mhz", 0))
    actual_power_w = float(state.get("power_limit_w", 0))

    expected_core_max = baseline_core_max + int(target_core_offset) if baseline_core_max > 0 else 0
    expected_mem_max = baseline_mem_max + int(target_mem_offset) if baseline_mem_max > 0 else 0
    expected_power_w = int(round(baseline_power_w * target_power_pct / 100)) if baseline_power_w > 0 else 0

    # ── NVAPI read-back is the PRIMARY source of truth ──────────────────────
    # On Blackwell, nvidia-smi `clocks.max.graphics` and `clocks.max.memory`
    # do NOT reliably update when an NVAPI offset is written — they often
    # report a cached value while the GPU is actually running at the OC'd
    # clock under load (`clocks.current.*`).  So we trust NVAPI for "did
    # the offset land?" and use nvidia-smi only for cross-context.
    nvapi_core_vf_offset = None
    nvapi_mem_offset = None
    nvapi_available = False
    if _nvapi is not None and _nvapi.is_available().get("ok"):
        no = _nvapi.get_current_offsets()
        if no.get("ok"):
            nvapi_available = True
            nvapi_core_vf_offset = no.get("core_offset_mhz", 0)
            nvapi_mem_offset = no.get("mem_offset_mhz", 0)

    actual_core_cur = int(state.get("core_mhz", 0))
    actual_mem_cur  = int(state.get("mem_mhz", 0))

    # ── Match decision ──────────────────────────────────────────────────────
    # Primary: NVAPI's stored offset matches what we asked for (±5 / ±15 MHz).
    # Secondary: clocks.max moved (some drivers do update this).
    # We accept EITHER signal — we don't require both, because Blackwell is
    # known to leave clocks.max stale even when the OC is live.
    # v3 — track the NVAPI-specific match separately from the combined
    # decision.  Used downstream in `warnings` to produce a precise
    # "NVAPI didn't store the offset" error vs a "verification couldn't
    # confirm" hedge.  Without these, the warning branches at line ~593
    # and ~605 referenced undefined names and threw NameError silently.
    core_nvapi_match = (
        abs((nvapi_core_vf_offset or 0) - int(target_core_offset)) <= 5
        if nvapi_available else False
    )
    mem_nvapi_match = (
        abs((nvapi_mem_offset or 0) - int(target_mem_offset)) <= 15
        if nvapi_available else False
    )
    if nvapi_available:
        # NVAPI is the authoritative source — if the driver stored what we
        # wrote, the OC is in place at the API level.
        core_match = core_nvapi_match
        mem_match  = mem_nvapi_match
    else:
        # NVAPI unavailable — fall back to the unreliable nvidia-smi `clocks.max`
        # comparison.  Better than nothing.
        core_match = (
            abs(actual_core_max - expected_core_max) <= 15
            if expected_core_max > 0 else target_core_offset == 0
        )
        mem_match = (
            abs(actual_mem_max - expected_mem_max) <= 25
            if expected_mem_max > 0 else target_mem_offset == 0
        )

    power_match = False
    if expected_power_w > 0:
        power_match = abs(actual_power_w - expected_power_w) <= 5
    elif target_power_pct == 100:
        power_match = True

    warnings = []
    # Only warn when the NVAPI write truly didn't land — which is the
    # condition that actually affects the user.  A stale clocks.max with
    # a happy NVAPI is fine and we say so in the per-field msg.
    if not core_match and target_core_offset != 0:
        if nvapi_available and not core_nvapi_match:
            warnings.append(
                f"Core: NVAPI did not store the offset (got {nvapi_core_vf_offset:+d}, want {target_core_offset:+d} MHz). "
                f"Likely cause: another OC tool (Afterburner, RTSS, Precision X1) is overriding it. "
                f"Close it and try again."
            )
        elif not nvapi_available:
            warnings.append(
                f"Core: NVAPI unavailable — cannot verify offset. "
                f"nvidia-smi reports max {actual_core_max} MHz (expected {expected_core_max} MHz)"
            )
    if not mem_match and target_mem_offset != 0:
        if nvapi_available and not mem_nvapi_match:
            warnings.append(
                f"Memory: NVAPI did not store the offset (got {nvapi_mem_offset:+d}, want {target_mem_offset:+d} MHz). "
                f"Likely cause: another OC tool overriding GhostShell."
            )
        elif not nvapi_available:
            warnings.append(
                f"Memory: NVAPI unavailable — cannot verify."
            )
    if not power_match and target_power_pct != 100:
        warnings.append(
            f"Power: actual {actual_power_w:.0f}W, expected {expected_power_w}W"
        )

    def _core_msg():
        if core_match:
            if nvapi_available and nvapi_core_vf_offset is not None:
                # "+35 MHz applied (live: 3125 MHz)" reads more clearly than max-only
                live = f", live {actual_core_cur} MHz" if actual_core_cur else ""
                return f"✓ NVAPI offset {nvapi_core_vf_offset:+d} MHz applied{live}"
            return f"✓ max {actual_core_max} MHz"
        return f"NVAPI offset {nvapi_core_vf_offset if nvapi_core_vf_offset is not None else '?':+d} MHz, expected {target_core_offset:+d} MHz"

    def _mem_msg():
        if mem_match:
            if nvapi_available and nvapi_mem_offset is not None:
                live = f", live {actual_mem_cur} MHz" if actual_mem_cur else ""
                return f"✓ NVAPI offset {nvapi_mem_offset:+d} MHz applied{live}"
            return f"✓ max {actual_mem_max} MHz"
        return f"NVAPI offset {nvapi_mem_offset if nvapi_mem_offset is not None else '?':+d} MHz, expected {target_mem_offset:+d} MHz"

    def _power_msg():
        if power_match:
            return f"✓ {actual_power_w:.0f}W"
        return f"{actual_power_w:.0f}W (expected {expected_power_w}W)"

    return {
        "ok": True,
        "verified": core_match and mem_match and power_match,
        "core": {
            "requested_offset": target_core_offset,
            "baseline_max": baseline_core_max,
            "expected_max": expected_core_max,
            "actual_max": actual_core_max,
            "nvapi_vf_offset_mhz": nvapi_core_vf_offset,
            "ok": core_match,
            "msg": _core_msg(),
        },
        "mem": {
            "requested_offset": target_mem_offset,
            "baseline_max": baseline_mem_max,
            "expected_max": expected_mem_max,
            "actual_max": actual_mem_max,
            "nvapi_offset_mhz": nvapi_mem_offset,
            "ok": mem_match,
            "msg": _mem_msg(),
        },
        "power": {
            "requested_pct": target_power_pct,
            "requested_w": expected_power_w,
            "actual_w": actual_power_w,
            "baseline_w": baseline_power_w,
            "ok": power_match,
            "msg": _power_msg(),
        },
        "warnings": warnings,
        "nvapi_available": nvapi_available,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Apply / reset
# ═══════════════════════════════════════════════════════════════════════════

def apply_oc(core_offset_mhz: int = 0, mem_offset_mhz: int = 0, power_pct: int = 100) -> dict:
    """Apply core/memory clock offsets and power limit.

    Args:
        core_offset_mhz: offset added to stock boost clock (0 = stock)
        mem_offset_mhz: offset added to stock memory clock (0 = stock)
        power_pct: power limit as percentage of default (100 = stock)

    Returns dict with per-step results.
    """
    cap = detect_gpu_oc_capability()
    if not cap["ok"] or cap["vendor"] != "nvidia":
        return {"ok": False, "err": f"OC not supported on {cap.get('vendor', 'unknown')} from software", "steps": []}

    # Clamp
    core_offset_mhz = _clamp(int(core_offset_mhz), -500, MAX_CORE_OFFSET_MHZ)
    mem_offset_mhz = _clamp(int(mem_offset_mhz), -1000, MAX_MEM_OFFSET_MHZ)
    power_pct = _clamp(int(power_pct), MIN_POWER_PCT, MAX_POWER_PCT)

    limits = cap["limits"]
    stock_core_max = limits["core_max_mhz"]
    stock_mem_max = limits["mem_max_mhz"]
    stock_power_w = limits["power_default_w"]
    max_power_w = limits["power_max_w"]
    min_power_w = limits["power_min_w"]
    _cache_power_default(stock_power_w)   # beta.9 — for emergency_reset_oc

    steps = []

    # v2.9.3 — surface conflicting OC-tool processes BEFORE we write anything.
    # If Afterburner / RTSS / Precision is running, our writes will be silently
    # overridden a few seconds later — the user needs to know.
    conflict = detect_oc_tools_running()
    if conflict.get("conflict_likely"):
        labels = ", ".join(t["name"] for t in conflict["tools"])
        steps.append({
            "name": f"⚠ Conflicting OC tool(s) running: {labels}",
            "ok": False,
            "err": (
                "Close these tools (and their startup entries) before applying "
                "OC in GhostShell.  They re-assert their own clock offsets every "
                "few seconds and will silently override GhostShell's writes."
            ),
        })
        log.warning(f"OC tool conflict detected: {labels}")

    # 1. Persistence mode (required for clock locks to stick)
    r = run_cmd(["nvidia-smi", "-pm", "1"], timeout=10)
    steps.append({"name": "Enable persistence mode", "ok": r["ok"], "out": r.get("out", ""), "err": r.get("err", "")})

    # 2. Power limit
    if stock_power_w > 0 and power_pct != 100:
        target_w = int(stock_power_w * power_pct / 100)
        if max_power_w > 0:
            target_w = min(target_w, max_power_w)
        if min_power_w > 0:
            target_w = max(target_w, min_power_w)
        r = run_cmd(["nvidia-smi", "-pl", str(target_w)], timeout=10)
        steps.append({"name": f"Set power limit → {target_w}W ({power_pct}%)", "ok": r["ok"], "err": r.get("err", "")})
    else:
        # Reset to default
        if stock_power_w > 0:
            r = run_cmd(["nvidia-smi", "-pl", str(stock_power_w)], timeout=10)
            steps.append({"name": f"Reset power limit → {stock_power_w}W (default)", "ok": r["ok"], "err": r.get("err", "")})

    applied_method = "none"

    # 3 & 4. Core + Memory clock offsets — prefer NVAPI because it modifies
    #        the actual clock offset rather than a simple cap.
    if _nvapi is not None:
        nvapi_avail = _nvapi.is_available()
        if nvapi_avail.get("ok"):
            nvapi_result = _nvapi.set_offsets(core_offset_mhz=core_offset_mhz, mem_offset_mhz=mem_offset_mhz)
            if nvapi_result.get("ok"):
                applied_method = "nvapi"
                steps.append({
                    "name": f"NVAPI: core{core_offset_mhz:+d} MHz / memory{mem_offset_mhz:+d} MHz (struct V{nvapi_result.get('struct_version_used','?')})",
                    "ok": True,
                    "err": "",
                })
                log.info(f"NVAPI applied: core{core_offset_mhz:+d} mem{mem_offset_mhz:+d}")
            else:
                err = nvapi_result.get("err", "unknown NVAPI error")
                steps.append({"name": f"NVAPI offset apply failed: {err}", "ok": False, "err": err})
                log.warning(f"NVAPI failed, falling back to nvidia-smi: {err}")
        else:
            steps.append({"name": f"NVAPI not available: {nvapi_avail.get('err', '?')}", "ok": False, "err": ""})
    else:
        steps.append({"name": "NVAPI module failed to import — clock offsets unavailable", "ok": False, "err": ""})

    # Verify the result against the pre-apply baselines.
    verification = verify_oc_applied(
        core_offset_mhz,
        mem_offset_mhz,
        power_pct,
        stock_core_max_mhz=stock_core_max,
        stock_mem_max_mhz=stock_mem_max,
        stock_power_w=stock_power_w,
    )

    # v2.9.2 — drop the nvidia-smi -lgc / -lmc "fallback".  Those commands set
    # a clock CAP (locked range), not an offset; on consumer GeForce when the
    # requested cap exceeds the hardware native max they just no-op, and the
    # green "✓ Fallback" log lines were misleading users into thinking the
    # OC had landed.
    #
    # Replace with: if NVAPI returned OK but verify still fails (the boost
    # ceiling didn't actually move — a known Blackwell driver behaviour),
    # surface a clear, honest diagnostic so the user knows what's going on.
    if applied_method == "nvapi" and not verification.get("verified", False) \
       and (core_offset_mhz != 0 or mem_offset_mhz != 0):
        # Show the user the NVAPI read-back vs nvidia-smi read-back so it's
        # obvious whether the driver stored our value but didn't apply it
        # to the boost ceiling.
        v_core = (verification.get("core") or {})
        v_mem  = (verification.get("mem")  or {})
        diag = (
            f"NVAPI accepted the writes but the GPU's reported max clock did not move. "
            f"core: nvidia-smi max {v_core.get('actual_max','?')} MHz "
            f"(VF read-back {v_core.get('nvapi_vf_offset_mhz','?')} MHz); "
            f"memory: nvidia-smi max {v_mem.get('actual_max','?')} MHz "
            f"(P-state read-back {v_mem.get('nvapi_offset_mhz','?')} MHz). "
            f"This is a known Blackwell-driver limitation: try a newer NVIDIA driver, "
            f"or use MSI Afterburner / NVIDIA Inspector for core overclocking."
        )
        steps.append({
            "name": "Verification failed — driver did not honor NVAPI write",
            "ok": False,
            "err": diag,
        })
        log.warning(f"Apply: {diag}")

    steps.append({
        "name": "Verification",
        "ok": verification.get("verified", False),
        "err": "; ".join(verification.get("warnings", [])),
    })

    log.info(
        f"OC applied: core+{core_offset_mhz} mem+{mem_offset_mhz} power {power_pct}% "
        f"verified={verification.get('verified', False)}"
    )
    # v3.3.1-beta.4: kick the watchdog so it re-asserts these offsets
    # if the driver / another OC tool drops them.  Even runs when
    # GhostShell's window is minimized — daemon thread, no UI gating.
    # We activate even if verification failed because the user
    # explicitly requested these offsets; if NVAPI's accepting writes
    # but the boost ceiling didn't move, the watchdog won't fix that
    # particular bug but it also won't make things worse.
    if core_offset_mhz != 0 or mem_offset_mhz != 0 or power_pct != 100:
        _activate_oc_watchdog(core_offset_mhz, mem_offset_mhz, power_pct)
    return {
        "ok": True,
        "applied": {
            "core_offset_mhz": core_offset_mhz,
            "mem_offset_mhz": mem_offset_mhz,
            "power_pct": power_pct,
        },
        "verified": verification.get("verified", False),
        "verification": verification,
        "steps": steps,
    }

# ═══════════════════════════════════════════════════════════════════════════
# v2.9.9.2 — GPU CRASH RECOVERY
# ═══════════════════════════════════════════════════════════════════════════
# When an OC pushes too far, on Blackwell / Ada the symptoms are:
#   * Display flickers black for 1-3 seconds (driver TDR resets the GPU)
#   * nvidia-smi briefly stops responding
#   * WebGL canvas loses its context
#   * Windows Event Log gets an nvlddmkm 153 / 4101
#
# If we don't react fast, the auto-tune loop happily applies the NEXT step
# while the GPU is still recovering — second crash, possibly worse.
#
# The strategy:
#   1. Frontend WebGL `webglcontextlost` listener immediately POSTs to
#      /api/gpu/oc/emergency-reset (no waiting for the next probe tick).
#   2. Backend probe `tick` ALSO calls emergency_reset_oc the moment it
#      sees `context_lost`, `hang`, or a fresh TDR event.
#   3. emergency_reset_oc tries every NVAPI surface independently, never
#      raises, and reports per-step success.  Even if half the API is
#      degraded post-crash, the parts that still work fire.
#   4. Sessions (auto-OC, benchmark) check `crashed=True` and STOP — they
#      do not advance to the next step.
#   5. A short 3-second settle wait before reading state again, so the GPU
#      has time to come back from the TDR.
# ═══════════════════════════════════════════════════════════════════════════

# Crash state shared by all sessions.  When set, run_probe_tick sees it
# on the next call and immediately reports `aborted=True, kind='gpu_crash'`.
_crash_state = {
    "crashed_ts":         0.0,
    "crashed_at_offset":  None,    # {"core": int, "mem": int}
    "kind":               None,    # 'context_loss' | 'tdr' | 'hang' | 'watchdog'
    "recovery_attempted": False,
    "last_safe_offset":   None,    # last offset known to be stable
    "last_smi_ok_ts":     0.0,     # watchdog: when did nvidia-smi last succeed?
}

# beta.9 — cached stock power-limit (W).  Populated whenever we read the GPU's
# capability/limits during normal operation, so emergency_reset_oc() can restore
# the power limit at crash time WITHOUT running a slow capability probe.  A
# power-induced crash needs power dropped back to stock, not just clocks zeroed.
_power_default_w = 0


def _cache_power_default(default_w) -> None:
    global _power_default_w
    try:
        w = int(default_w or 0)
        if w > 0:
            _power_default_w = w
    except Exception:
        pass


def emergency_reset_oc(reason: str = "unspecified") -> dict:
    """Best-effort, never-raises reset for crash recovery.

    Tries every NVAPI surface independently — VF curve, P-state graphics,
    P-state memory, plus nvidia-smi locks — and reports which ones worked.
    Returns ok=True if AT LEAST ONE path landed; the user can rely on the
    GPU being closer to stock than it was before.

    Should be safe to call from any thread, including from inside the
    crash detection probe tick — never blocks more than ~3 seconds total.
    """
    log.warning(f"EMERGENCY RESET requested (reason: {reason})")
    started = time.time()
    steps = []
    TIME_BUDGET_S = 3.0     # hard wall-clock cap (docstring promise)

    def _over_budget() -> bool:
        return (time.time() - started) > TIME_BUDGET_S

    # 0. beta.9 — DROP POWER LIMIT BACK TO STOCK FIRST.  A crash at a high
    #    power limit + high clock leaves the card drawing far past stock if we
    #    only zero the clocks.  Restore the cached stock power BEFORE the clock
    #    reset so the card is thermally/electrically safe even if a later step
    #    times out.  2s timeout; skip if we never learned the default.
    if _power_default_w > 0:
        try:
            r = run_cmd(["nvidia-smi", "-pl", str(int(_power_default_w))], timeout=2)
            ok = bool(r.get("ok"))
            steps.append({"name": f"nvidia-smi -pl {_power_default_w}W (stock power)",
                          "ok": ok, "err": (r.get("err") or "")[:120]})
            if not ok:
                log.error(f"Emergency reset: power restore to {_power_default_w}W FAILED: "
                          f"{(r.get('err') or '')[:160]}")
        except Exception as e:
            steps.append({"name": "nvidia-smi -pl (stock power, exception)", "ok": False, "err": str(e)[:120]})

    # 1. NVAPI force-reset (covers VF curve + sparse P-state writes)
    if _nvapi is not None and not _over_budget():
        try:
            r = _nvapi.force_reset_all()
            for s in r.get("steps", []):
                steps.append({
                    "name": f"NVAPI: {s.get('name','')}",
                    "ok": s.get("ok", False),
                    "err": s.get("err", "")[:120],
                })
        except Exception as e:
            steps.append({"name": "NVAPI force_reset_all (exception)", "ok": False, "err": str(e)[:120]})

    # 2. nvidia-smi clock locks — clear them too in case a fallback applied any.
    #    2s each (was 5s) + skip once we blow the time budget so a wedged smi
    #    can't stall recovery well past the promised ~3s.
    for cmd, label in [(["nvidia-smi", "-rgc"], "nvidia-smi -rgc (core lock)"),
                        (["nvidia-smi", "-rmc"], "nvidia-smi -rmc (mem lock)")]:
        if _over_budget():
            steps.append({"name": label, "ok": False, "err": "skipped — reset time budget exceeded"})
            continue
        try:
            r = run_cmd(cmd, timeout=2)
            steps.append({"name": label, "ok": r.get("ok", False), "err": (r.get("err") or "")[:120]})
        except Exception as e:
            steps.append({"name": label, "ok": False, "err": str(e)[:120]})

    elapsed = time.time() - started
    any_ok = any(s["ok"] for s in steps)
    timed_out = elapsed > TIME_BUDGET_S
    _crash_state["recovery_attempted"] = True

    log.warning(f"Emergency reset complete in {elapsed:.2f}s — "
                f"any_ok={any_ok}, timed_out={timed_out}, "
                f"steps={[s['name'] for s in steps if s['ok']]}")
    if not any_ok:
        log.error("EMERGENCY RESET: NO reset path succeeded — card may still be OC'd!")
    return {
        "ok":          any_ok,
        "timed_out":   timed_out,
        "power_restored": bool(_power_default_w > 0 and any(
            s["ok"] for s in steps if "stock power" in s["name"])),
        "elapsed_s":   round(elapsed, 2),
        "reason":      reason,
        "steps":       steps,
        "crash_state": dict(_crash_state),
    }


def mark_gpu_crashed(kind: str, offset_core: int = 0, offset_mem: int = 0) -> dict:
    """Record that a crash just happened at this offset, then trigger recovery.

    Called from:
      - run_probe_tick when context_lost / hang / TDR is observed
      - frontend's webglcontextlost listener (via /api/gpu/oc/emergency-reset)
      - the watchdog when nvidia-smi has been unresponsive too long
    """
    _crash_state["crashed_ts"]        = time.time()
    _crash_state["crashed_at_offset"] = {"core": int(offset_core), "mem": int(offset_mem)}
    _crash_state["kind"]              = kind
    _crash_state["recovery_attempted"] = False
    log.error(f"GPU crash recorded: kind={kind} at core+{offset_core} / mem+{offset_mem}")

    # Trigger emergency reset right away — the offending offset must be
    # cleared before the GPU comes back from the TDR.
    return emergency_reset_oc(reason=f"gpu_crash:{kind}")


def get_crash_state() -> dict:
    """Return the most recent crash record (if any).  Frontend polls this."""
    return dict(_crash_state)


def clear_crash_state():
    """Called once the user acknowledges or starts a fresh tune."""
    _crash_state["crashed_ts"]         = 0.0
    _crash_state["crashed_at_offset"]  = None
    _crash_state["kind"]               = None
    _crash_state["recovery_attempted"] = False


def reset_oc() -> dict:
    """Fully revert to stock clocks and power.

    v2.9.1 — uses the aggressive `force_reset_all()` path so a stuck VF-curve
    offset from a previous session can be cleared even when SetPstates20 is
    rejecting full-blob writes.  Each NVAPI sub-step is reported individually
    so the UI shows exactly which lever moved.
    """
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"ok": False, "err": "Reset only supported on NVIDIA", "steps": []}

    steps = []

    # NVAPI: aggressive reset — VF curve to 0 AND every P-state freqDelta to 0
    if _nvapi is not None and _nvapi.is_available().get("ok"):
        r = _nvapi.force_reset_all()
        for sub in r.get("steps", []):
            steps.append({
                "name": f"NVAPI: {sub.get('name','')}",
                "ok":   sub.get("ok", False),
                "err":  sub.get("err", ""),
            })
        if not r.get("ok"):
            steps.append({
                "name": "NVAPI: nothing reset — try restarting GhostShell as Administrator",
                "ok": False, "err": r.get("err", ""),
            })

    # Also clear any nvidia-smi clock locks (in case they were applied as fallback)
    r = run_cmd(["nvidia-smi", "-rgc"], timeout=10)
    steps.append({"name": "Reset nvidia-smi core lock (-rgc)", "ok": r["ok"], "err": r.get("err", "")})
    r = run_cmd(["nvidia-smi", "-rmc"], timeout=10)
    steps.append({"name": "Reset nvidia-smi memory lock (-rmc)", "ok": r.get("ok", False), "err": r.get("err", "")})

    # Reset power to default
    default_w = cap["limits"].get("power_default_w", 0)
    if default_w > 0:
        r = run_cmd(["nvidia-smi", "-pl", str(default_w)], timeout=10)
        steps.append({"name": f"Reset power limit → {default_w}W", "ok": r["ok"], "err": r.get("err", "")})

    log.info("OC fully reset to stock")
    # v3.3.1-beta.4: tell the watchdog to stop re-asserting offsets.
    _deactivate_oc_watchdog()
    return {"ok": True, "steps": steps}


def _reset_to_stock_hard(attempts: int = 3) -> "tuple[bool, dict]":
    """beta.9 — reset to stock with retries; return (ok, detail).

    reset_oc() ALWAYS returns ok:True regardless of whether its steps actually
    worked, so a post-crash abort that "reset to stock" could be lying while the
    card is still OC'd.  This inspects the real step results, escalates to
    emergency_reset_oc between tries, and only reports ok when at least one
    reset path genuinely landed.  On total failure it deactivates the watchdog
    so it can't re-push the crashed offset."""
    detail = {"attempts": []}
    for _ in range(max(1, attempts)):
        landed = False
        try:
            r = reset_oc()
            steps = r.get("steps", [])
            nvapi_ok = any(s.get("ok") and "NVAPI" in s.get("name", "") for s in steps)
            smi_ok   = any(s.get("ok") and "nvidia-smi" in s.get("name", "").lower() for s in steps)
            landed = bool(nvapi_ok or smi_ok)
            detail["attempts"].append({
                "kind": "reset_oc", "landed": landed,
                "steps": [{"name": s.get("name"), "ok": s.get("ok")} for s in steps],
            })
        except Exception as e:
            detail["attempts"].append({"kind": "reset_oc", "landed": False, "err": str(e)[:160]})
        if landed:
            return True, detail
        # Escalate to the emergency path (also drops power) before retrying.
        try:
            er = emergency_reset_oc(reason="reset_to_stock_retry")
            detail["attempts"].append({"kind": "emergency_reset", "landed": bool(er.get("ok"))})
            if er.get("ok"):
                return True, detail
        except Exception as e:
            detail["attempts"].append({"kind": "emergency_reset", "landed": False, "err": str(e)[:160]})
        time.sleep(0.5)
    # Nothing worked — make sure the watchdog can't re-assert the crashed offset.
    try: _deactivate_oc_watchdog()
    except Exception: pass
    return False, detail


# ═══════════════════════════════════════════════════════════════════════════
# OC Watchdog (v3.3.1-beta.4+)
# ───────────────────────────────────────────────────────────────────────────
# Re-asserts the user's active OC offsets so other tools (Afterburner / RTSS /
# Precision X1) can't silently overwrite them, AND so an unexpected driver
# event (mode-set, crash recovery, p-state reload, full-screen game's HDR
# toggle) can't quietly drop our offset back to 0.  Runs continuously as a
# daemon thread — fires even when GhostShell's window is minimized, which
# is the whole point of this feature.
#
# Drift detection: every 5 s we read `clocks.max.graphics` + `clocks.max.memory`
# from nvidia-smi and compare against `stock_baseline + saved_offset`.  If
# either is off by more than the user-specified 150 MHz tolerance, we
# re-issue the NVAPI offset write.  The tolerance is wide enough that
# legitimate dynamic clocking + Nvidia's own boost-curve variance won't
# trigger us; only a real loss of the offset will.
# ═══════════════════════════════════════════════════════════════════════════

_OC_WATCHDOG_INTERVAL_SEC = 5.0
_OC_DRIFT_TOLERANCE_MHZ   = 150          # user requirement: "150 leniency"

_active_oc = {
    "core":   0,
    "mem":    0,
    "power":  100,
    "active": False,
}
_active_oc_lock     = threading.Lock()
_oc_watchdog_thread: Optional[threading.Thread] = None
_oc_watchdog_stop:   Optional[threading.Event]  = None
_oc_watchdog_drift_corrections = 0       # total drift events since process start
_oc_watchdog_last_drift = {}             # last drift snapshot for status surface
# beta.14 — multi-source pause set.  Other subsystems (adaptive_tuning,
# stress tests, manual diagnostics) can pause the watchdog while they own
# the offsets so we don't fight them on every poll.  Only resumes once
# every source has called back.  Stored as a set of free-text identifiers
# so different callers self-identify without coordination.
# beta.9 — {source: expiry_ts_or_None}.  A source with an expiry auto-clears if
# it's never explicitly resumed (orphaned auto-OC session that stopped calling
# next()), so the watchdog can't stay paused forever.  Sources with expiry=None
# (e.g. adaptive_tuning during a game) never auto-expire.
_oc_watchdog_pause_sources: dict = {}
_oc_watchdog_pause_lock              = threading.Lock()


def _prune_expired_pauses() -> None:
    """Drop pause sources whose max-age has elapsed.  Caller holds the lock."""
    now = time.time()
    expired = [s for s, exp in _oc_watchdog_pause_sources.items()
               if exp is not None and now > exp]
    for s in expired:
        del _oc_watchdog_pause_sources[s]
        log.warning(f"OC watchdog pause source '{s}' auto-expired (orphaned session) — resuming")


def _activate_oc_watchdog(core_offset_mhz: int, mem_offset_mhz: int, power_pct: int) -> None:
    """Mark an OC profile as active + ensure the watchdog thread is running."""
    global _oc_watchdog_thread, _oc_watchdog_stop
    with _active_oc_lock:
        _active_oc.update({
            "core":   int(core_offset_mhz),
            "mem":    int(mem_offset_mhz),
            "power":  int(power_pct),
            "active": True,
        })
    if _oc_watchdog_thread and _oc_watchdog_thread.is_alive():
        return
    _oc_watchdog_stop = threading.Event()
    _oc_watchdog_thread = threading.Thread(
        target=_oc_watchdog_loop, args=(_oc_watchdog_stop,),
        daemon=True, name="oc-watchdog",
    )
    _oc_watchdog_thread.start()
    log.info(f"OC watchdog started (target core+{core_offset_mhz} mem+{mem_offset_mhz}, "
             f"tolerance ±{_OC_DRIFT_TOLERANCE_MHZ} MHz)")


def _deactivate_oc_watchdog() -> None:
    """Mark profile as inactive.  The thread keeps running (cheap idle wait)
    so a subsequent apply_oc lights it back up without a thread restart."""
    with _active_oc_lock:
        _active_oc.update({"core": 0, "mem": 0, "power": 100, "active": False})
    log.info("OC watchdog deactivated")


def pause_oc_watchdog(source: str, max_age_s: Optional[float] = None) -> dict:
    """beta.14 — let another subsystem (typically AT) own the offsets
    temporarily without GhostShell's stock watchdog stepping on them.

    Multiple sources stack — the watchdog only resumes work once every
    pause source has called resume_oc_watchdog().  Source names are
    free-form strings; use a stable identifier per subsystem so the
    resume call lines up with the pause call.

    beta.9 — pass `max_age_s` for a self-healing pause: if the caller never
    resumes (e.g. an auto-OC run whose UI navigated away), the pause auto-clears
    after that many seconds so the watchdog can't stay off forever.  AT passes
    no max_age (its pause spans a whole game session).
    """
    src = str(source or "external")
    exp = (time.time() + float(max_age_s)) if max_age_s else None
    with _oc_watchdog_pause_lock:
        _oc_watchdog_pause_sources[src] = exp
        sources = sorted(_oc_watchdog_pause_sources)
    log.info(f"OC watchdog paused by {src} (active pause sources: {sources}, "
             f"max_age={max_age_s})")
    return {"ok": True, "paused": True, "sources": sources}


def resume_oc_watchdog(source: str) -> dict:
    """Lift one source's pause.  Watchdog resumes its drift checks the
    moment the pause-source set is empty again.  Idempotent."""
    src = str(source or "external")
    with _oc_watchdog_pause_lock:
        _oc_watchdog_pause_sources.pop(src, None)
        _prune_expired_pauses()
        sources = sorted(_oc_watchdog_pause_sources)
    log.info(f"OC watchdog resume({src}) — remaining pause sources: {sources}")
    return {"ok": True, "paused": bool(sources), "sources": sources}


def is_oc_watchdog_paused() -> bool:
    with _oc_watchdog_pause_lock:
        _prune_expired_pauses()
        return bool(_oc_watchdog_pause_sources)


def get_oc_watchdog_pause_sources() -> list:
    """For diagnostics + the watchdog status panel."""
    with _oc_watchdog_pause_lock:
        _prune_expired_pauses()
        return sorted(_oc_watchdog_pause_sources)


def _oc_watchdog_loop(stop: threading.Event) -> None:
    """Polling loop: detect drift between the offsets we asked NVAPI to
    apply and the offsets NVAPI is currently reporting back.  If the
    offsets diverge by more than a tiny tolerance, re-issue the write.

    v3.3.1-beta.13 rewrite — the old loop compared `clocks.max.graphics`
    from nvidia-smi against `stock_baseline + target_offset`.  That
    field is unreliable on RTX 50-series and (with RTSS/Afterburner
    running alongside) routinely under-reports the boost ceiling even
    when the offset is still applied — producing endless "OC drift"
    log spam against a perfectly-applied OC.  Reading the offset
    directly via NVAPI's V/F curve + P-state freqDelta is the source
    of truth and immune to nvidia-smi's quirks.  Falls back to the
    old clocks.max heuristic only when NVAPI is unavailable.
    """
    global _oc_watchdog_drift_corrections, _oc_watchdog_last_drift
    # Offset comparison can be tight — offsets don't drift in small
    # increments, they either hold or get reset to 0 (or another tool
    # writes its own value).  5 MHz absorbs any kHz-rounding noise
    # without flapping.
    _OFFSET_EQUAL_TOLERANCE_MHZ = 5
    while not stop.is_set():
        try:
            with _active_oc_lock:
                if not _active_oc["active"]:
                    stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue
                target_core = _active_oc["core"]
                target_mem  = _active_oc["mem"]

            # beta.14 — skip drift checks entirely while another subsystem
            # (typically adaptive_tuning) owns the offsets.  Saves NVAPI
            # calls and avoids "OC drift" log spam during AT step decisions.
            if is_oc_watchdog_paused():
                stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue

            nvapi_ok = _nvapi is not None and _nvapi.is_available().get("ok")

            # ── Primary path: compare offsets via NVAPI ─────────────
            if nvapi_ok:
                try:
                    cur = _nvapi.get_current_offsets()
                except Exception as e:
                    cur = {"ok": False, "err": str(e)}
                if cur.get("ok"):
                    actual_core_off = int(cur.get("core_offset_mhz", 0))
                    actual_mem_off  = int(cur.get("mem_offset_mhz", 0))
                    core_drift_off  = actual_core_off - target_core
                    mem_drift_off   = actual_mem_off  - target_mem
                    need_reapply = (
                        abs(core_drift_off) > _OFFSET_EQUAL_TOLERANCE_MHZ
                        or abs(mem_drift_off)  > _OFFSET_EQUAL_TOLERANCE_MHZ
                    )
                    if need_reapply:
                        _oc_watchdog_drift_corrections += 1
                        _oc_watchdog_last_drift = {
                            "ts":             time.time(),
                            "source":         "nvapi_offset",
                            "core_actual":    actual_core_off,
                            "core_expected":  target_core,
                            "core_drift_mhz": core_drift_off,
                            "mem_actual":     actual_mem_off,
                            "mem_expected":   target_mem,
                            "mem_drift_mhz":  mem_drift_off,
                        }
                        log.warning(
                            f"OC offset drift: core +{actual_core_off} "
                            f"vs +{target_core} (Δ {core_drift_off:+d}), "
                            f"mem +{actual_mem_off} vs +{target_mem} "
                            f"(Δ {mem_drift_off:+d}). Re-asserting."
                        )
                        try:
                            _nvapi.set_offsets(core_offset_mhz=target_core,
                                                mem_offset_mhz=target_mem)
                        except Exception as e:
                            log.debug(f"OC watchdog re-assert raised: {e}")
                    stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue
                # NVAPI couldn't read offsets — fall through to legacy path

            # ── Fallback path: legacy clocks.max heuristic ──────────
            # Used only when NVAPI is unavailable (no driver, AMD GPU
            # mis-typed as Nvidia, etc).  Loose tolerance because the
            # `clocks.max.graphics` field fluctuates with boost state
            # and power/temp throttling.
            state = read_live_state()
            if not state.get("ok"):
                stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue
            saved = _load_stock_baseline()
            if not saved:
                # No baseline + NVAPI failed → can't reason about drift.
                # Skip rather than blind re-assert (since NVAPI is gone
                # anyway, blind re-assert wouldn't do anything).
                stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue
            stock_core = int(saved.get("core_stock_mhz", 0))
            stock_mem  = int(saved.get("mem_stock_mhz", 0))
            if stock_core <= 0 or stock_mem <= 0:
                stop.wait(_OC_WATCHDOG_INTERVAL_SEC); continue
            expected_core_max = stock_core + target_core
            expected_mem_max  = stock_mem  + target_mem
            actual_core_max   = int(state.get("core_max_mhz", 0))
            actual_mem_max    = int(state.get("mem_max_mhz", 0))
            core_drift = (actual_core_max - expected_core_max) if actual_core_max else 0
            mem_drift  = (actual_mem_max  - expected_mem_max)  if actual_mem_max  else 0
            need_reapply = (abs(core_drift) > _OC_DRIFT_TOLERANCE_MHZ
                              or abs(mem_drift)  > _OC_DRIFT_TOLERANCE_MHZ)
            if need_reapply:
                _oc_watchdog_drift_corrections += 1
                _oc_watchdog_last_drift = {
                    "ts":              time.time(),
                    "source":          "nvidia_smi_max",
                    "core_actual":     actual_core_max,
                    "core_expected":   expected_core_max,
                    "core_drift_mhz":  core_drift,
                    "mem_actual":      actual_mem_max,
                    "mem_expected":    expected_mem_max,
                    "mem_drift_mhz":   mem_drift,
                }
                log.warning(
                    f"OC drift (clocks.max fallback): core {actual_core_max} "
                    f"vs {expected_core_max} (Δ {core_drift:+d}), mem "
                    f"{actual_mem_max} vs {expected_mem_max} (Δ {mem_drift:+d})."
                )
                # NVAPI is unavailable in this branch, so we can't
                # re-issue offsets.  Just log and wait.
        except Exception as e:
            log.debug(f"OC watchdog tick: {e}")
        stop.wait(_OC_WATCHDOG_INTERVAL_SEC)


def get_oc_watchdog_status() -> dict:
    """Return current watchdog state for the UI."""
    with _active_oc_lock:
        active = bool(_active_oc["active"])
        target_core = _active_oc["core"]
        target_mem  = _active_oc["mem"]
    return {
        "active":            active,
        "running":           bool(_oc_watchdog_thread and _oc_watchdog_thread.is_alive()),
        "target_core_offset": target_core,
        "target_mem_offset":  target_mem,
        "tolerance_mhz":      _OC_DRIFT_TOLERANCE_MHZ,
        "interval_sec":       _OC_WATCHDOG_INTERVAL_SEC,
        "drift_corrections":  _oc_watchdog_drift_corrections,
        "last_drift":         dict(_oc_watchdog_last_drift),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Driver-event scan — TDR (Timeout Detection & Recovery) is the surest sign
# that an OC crashed the GPU.  Windows logs nvlddmkm Event ID 153 (typically)
# or 4101 ("Display driver stopped responding and has recovered") when this
# happens.  amdkmdag is the AMD equivalent.
# ═══════════════════════════════════════════════════════════════════════════

def check_driver_events(seconds_back: int = 300) -> dict:
    """Count GPU driver TDR / crash events in Windows Event Log.

    Args:
        seconds_back: how far back to look (default 5 min).

    Returns:
        {
          'ok': bool,
          'tdr_count': int,        # count of recovery / timeout events
          'crash_count': int,      # count of severe / non-recoverable
          'events': [{'id': int, 'time': str, 'msg': str}, ...]  # latest 5
        }
    """
    seconds_back = max(1, int(seconds_back))
    # Single PS pipeline. -ErrorAction SilentlyContinue keeps an empty result quiet.
    cmd = (
        f"$start = (Get-Date).AddSeconds(-{seconds_back}); "
        "Get-WinEvent -FilterHashtable @{LogName='System'; "
        "ProviderName=@('nvlddmkm','nvlddmkmoem','amdkmdag','iaLPSSi_GPIO'); "
        "StartTime=$start} -MaxEvents 25 -ErrorAction SilentlyContinue | "
        "Select-Object Id, TimeCreated, LevelDisplayName, "
        "@{N='Msg';E={$_.Message.Substring(0,[Math]::Min(180,$_.Message.Length))}} "
        "| ConvertTo-Json -Compress"
    )
    r = run_ps(cmd, timeout=10)
    if not r["ok"]:
        return {"ok": False, "tdr_count": 0, "crash_count": 0, "events": [],
                "err": r.get("err") or "Get-WinEvent failed"}

    out = (r.get("out") or "").strip()
    if not out:
        return {"ok": True, "tdr_count": 0, "crash_count": 0, "events": []}

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "tdr_count": 0, "crash_count": 0, "events": [],
                "err": "Could not parse Get-WinEvent JSON output"}

    if not isinstance(data, list):
        data = [data]

    # Event IDs commonly logged on TDR / driver hang
    TDR_IDS = {153, 4101, 1003}            # 4101 = recovered, 153 = timeout reset
    CRASH_IDS = {141, 142, 143, 4102}      # 141 = bug-check, 4102 = not recoverable

    tdr = 0
    crash = 0
    events = []
    for ev in data[:25]:
        try:
            evid = int(ev.get("Id") or 0)
        except (TypeError, ValueError):
            evid = 0
        if evid in TDR_IDS:
            tdr += 1
        elif evid in CRASH_IDS:
            crash += 1
        if len(events) < 5:
            events.append({
                "id": evid,
                "time": str(ev.get("TimeCreated") or ""),
                "level": str(ev.get("LevelDisplayName") or ""),
                "msg": str(ev.get("Msg") or "")[:180],
            })

    return {"ok": True, "tdr_count": tdr, "crash_count": crash, "events": events}


# ═══════════════════════════════════════════════════════════════════════════
# Stability test — poll-based while WebGL stresses the GPU in the UI
# ═══════════════════════════════════════════════════════════════════════════

_session_lock = threading.Lock()  # protects _stability_session and _auto_oc_session

_stability_session = {
    "active": False,
    "started": 0.0,
    "samples": [],
    "max_temp": 0,
    "min_core": 999999,
    "max_core": 0,
    "max_mem": 0,
    "thermal_throttle": False,
    "hang_detected": False,
    "aborted": False,
    "abort_reason": "",
    # Robust signals
    "target_core_max": 0,         # expected boost ceiling (used for clock-dip detection)
    "low_util_count": 0,          # tracks frames where utilization unexpectedly low
    "frame_time_ms": [],          # frame times reported by JS WebGL probe
    "context_lost": False,        # WebGL context lost = GPU crashed
    "pixel_artifacts": 0,         # count of frames with bad pixel checksums
    "tdr_event_count_at_start": 0,
}


def start_stability_probe(expected_core_max: int = 0) -> dict:
    """Begin a stability probe session.

    Args:
        expected_core_max: target boost clock ceiling. Used to detect clock-dips
                           (sign of TDP throttling at this OC level).
    """
    # Snapshot driver event count NOW so we only count NEW events during the test
    ev = check_driver_events(seconds_back=10)
    tdr_baseline = ev.get("tdr_count", 0)

    _stability_session.update({
        "active": True,
        "started": time.time(),
        "samples": [],
        "max_temp": 0,
        "min_core": 999999,
        "max_core": 0,
        "max_mem": 0,
        "thermal_throttle": False,
        "hang_detected": False,
        "aborted": False,
        "abort_reason": "",
        "target_core_max": int(expected_core_max),
        "low_util_count": 0,
        "frame_time_ms": [],
        "context_lost": False,
        "pixel_artifacts": 0,
        "tdr_event_count_at_start": tdr_baseline,
    })
    log.info(f"Stability probe started (target core max: {expected_core_max} MHz, TDR baseline: {tdr_baseline})")
    return {"ok": True, "target_core_max": expected_core_max}


def run_probe_tick(frame_time_ms: float = None, context_lost: bool = False, pixel_check_failed: bool = False) -> dict:
    """Poll GPU state and update session metrics.

    Args (all optional, sent by frontend):
        frame_time_ms: WebGL frame time in ms — high variance signals instability
        context_lost: True if WebGL context was lost (clear sign of GPU crash)
        pixel_check_failed: True if our reference pixel didn't match expected color (artifact)

    Stability signals checked:
      1. Driver hang: nvidia-smi stops responding
      2. Thermal abort: temp >= TEMP_ABORT_C (87°C)
      3. WebGL context loss: GPU driver reset on the page
      4. New TDR events in Event Log since probe started
      5. Pixel correctness: rendered pixels don't match reference (artifact)
      6. Clock dip: actual core MHz drops >8% below target during sustained load
      7. Util sanity: utilization < 70% during what should be 95-100% workload
      8. Thermal throttle: 5+ consecutive samples at >= 83°C
    """
    if not _stability_session["active"]:
        return {"ok": False, "err": "No active probe session"}

    sess = _stability_session

    # Signal 1: WebGL context lost (immediate fail) — GPU likely TDR'd
    if context_lost:
        sess["context_lost"] = True
        sess["aborted"] = True
        sess["abort_reason"] = "WebGL context lost — GPU driver reset"
        log.error("Stability probe: WebGL context lost (GPU crashed)")
        # v2.9.9.2 — IMMEDIATELY trigger emergency reset.  The user's display
        # may already be black/recovering; we want NVAPI offsets cleared
        # before the driver fully comes back so we don't crash again.
        try:
            mark_gpu_crashed("context_loss",
                             offset_core=sess.get("target_core_max", 0),
                             offset_mem=0)
        except Exception as e:
            log.warning(f"Emergency reset call failed: {e}")
        return {"ok": False, "abort": True, "reason": sess["abort_reason"], "kind": "context_loss"}

    # Signal 2: Pixel correctness check
    if pixel_check_failed:
        sess["pixel_artifacts"] += 1
        if sess["pixel_artifacts"] >= 2:  # 2+ frames of artifacts = unstable
            sess["aborted"] = True
            sess["abort_reason"] = f"Visual artifacts detected ({sess['pixel_artifacts']} frames with bad pixel checksums)"
            log.warning(sess["abort_reason"])
            # Visual artifacts ≠ full crash, but they're a strong signal
            # the OC is corrupting memory accesses.  Reset to be safe.
            try:
                mark_gpu_crashed("artifacts",
                                 offset_core=sess.get("target_core_max", 0),
                                 offset_mem=0)
            except Exception as e:
                log.warning(f"Emergency reset call failed: {e}")
            return {"ok": False, "abort": True, "reason": sess["abort_reason"], "kind": "artifacts"}

    # Read GPU state — also serves as a watchdog.  If nvidia-smi has been
    # silent too long, the driver is wedged even if we never got a
    # context-loss event.
    state = read_live_state()
    now_ts = time.time()
    if state["ok"]:
        _crash_state["last_smi_ok_ts"] = now_ts
    else:
        sess["hang_detected"] = True
        sess["aborted"] = True
        sess["abort_reason"] = "Driver stopped responding (nvidia-smi failed)"
        log.error("Stability probe: driver hang detected (nvidia-smi failed)")
        try:
            mark_gpu_crashed("hang",
                             offset_core=sess.get("target_core_max", 0),
                             offset_mem=0)
        except Exception as e:
            log.warning(f"Emergency reset call failed: {e}")
        return {"ok": False, "hang": True, "err": state.get("err", ""), "kind": "hang"}

    # v2.9.9.2 — secondary watchdog: nvidia-smi succeeded NOW but if the
    # PREVIOUS call took >5s (i.e. our last_smi_ok_ts was stale), the GPU
    # was probably mid-crash and just came back.  Treat as a soft signal
    # — don't abort, but log it so the user knows.
    if _crash_state["last_smi_ok_ts"] > 0 and (now_ts - _crash_state["last_smi_ok_ts"]) > 5:
        log.warning(f"nvidia-smi gap of {now_ts - _crash_state['last_smi_ok_ts']:.1f}s — possible brief GPU stall")

    sample = {"t": time.time() - sess["started"], **state}
    if frame_time_ms is not None:
        sample["frame_time_ms"] = frame_time_ms
        sess["frame_time_ms"].append(frame_time_ms)
    sess["samples"].append(sample)
    sess["max_temp"] = max(sess["max_temp"], state["temp_c"])
    if state["core_mhz"] > 0:
        sess["min_core"] = min(sess["min_core"], state["core_mhz"])
        sess["max_core"] = max(sess["max_core"], state["core_mhz"])
    if state.get("mem_mhz", 0) > 0:
        sess["max_mem"] = max(sess["max_mem"], state["mem_mhz"])

    # Signal 3: Thermal abort
    if state["temp_c"] >= TEMP_ABORT_C:
        sess["aborted"] = True
        sess["abort_reason"] = f"Thermal abort: GPU hit {state['temp_c']}°C (limit {TEMP_ABORT_C}°C)"
        sess["thermal_throttle"] = True
        log.warning(sess["abort_reason"])
        return {"ok": False, "abort": True, "state": state, "reason": sess["abort_reason"], "kind": "thermal"}

    # Signal 4: Driver TDR events (only after a few warmup samples to avoid false positives)
    if len(sess["samples"]) >= 3 and len(sess["samples"]) % 5 == 0:
        ev = check_driver_events(seconds_back=int(sample["t"]) + 5)
        new_tdrs = ev.get("tdr_count", 0) - sess["tdr_event_count_at_start"]
        if new_tdrs > 0:
            sess["aborted"] = True
            sess["abort_reason"] = f"Driver TDR event(s) logged: {new_tdrs} new TDR/crash event(s) in Event Log"
            log.error(sess["abort_reason"])
            return {"ok": False, "abort": True, "state": state, "reason": sess["abort_reason"], "kind": "tdr"}

    # Signal 5: Clock dip detection — only after warmup (sample 5+) and only if util is high
    if len(sess["samples"]) >= 6 and sess["target_core_max"] > 0 and state["gpu_util_pct"] >= 80:
        # If actual clock is more than 8% below target while at high util, that's TDP throttle
        target = sess["target_core_max"]
        if state["core_mhz"] < target * 0.92 and state["core_mhz"] > 0:
            recent_dips = sum(
                1 for s in sess["samples"][-5:]
                if s.get("core_mhz", 0) < target * 0.92 and s.get("gpu_util_pct", 0) >= 80
            )
            if recent_dips >= 4:  # 4 of last 5 samples dipped
                sess["thermal_throttle"] = True
                # Don't abort — just flag it. User can decide if this OC is "stable" or just power-limited.

    # Signal 6: Util sanity — if user is running stress at 100% but util keeps dropping, scheduling issue
    if state["gpu_util_pct"] < 50 and len(sess["samples"]) >= 5:
        sess["low_util_count"] += 1
    else:
        sess["low_util_count"] = max(0, sess["low_util_count"] - 1)

    # Signal 7: Thermal throttle warning (sustained high temp)
    recent = sess["samples"][-5:]
    if len(recent) >= 5 and all(s["temp_c"] >= 83 for s in recent):
        sess["thermal_throttle"] = True

    return {"ok": True, "state": state, "elapsed": sample["t"], "kind": "running"}


def end_stability_probe() -> dict:
    """Finish the session and report verdict.

    Performs a final driver-event scan to catch TDRs that fired in the last second.
    """
    if not _stability_session["active"]:
        return {"ok": False, "err": "No active probe session"}

    sess = _stability_session
    sess["active"] = False
    duration = time.time() - sess["started"]

    # Final TDR scan
    ev = check_driver_events(seconds_back=int(duration) + 5)
    new_tdrs = ev.get("tdr_count", 0) - sess["tdr_event_count_at_start"]
    if new_tdrs > 0 and not sess["aborted"]:
        sess["aborted"] = True
        sess["abort_reason"] = f"Late TDR detected on probe end: {new_tdrs} driver TDR event(s)"
        log.warning(sess["abort_reason"])

    # Frame time stats
    fts = sess["frame_time_ms"]
    avg_ft = sum(fts) / len(fts) if fts else 0.0
    # 1% high frame times (the worst 1% — laggiest moments)
    sorted_fts = sorted(fts, reverse=True) if fts else []
    p99 = sorted_fts[len(sorted_fts) // 100] if len(sorted_fts) >= 100 else (max(fts) if fts else 0)
    avg_fps = (1000.0 / avg_ft) if avg_ft > 0 else 0.0

    # Frame variance — high variance with no other issues = instability marker
    if len(fts) > 10:
        mean = avg_ft
        var = sum((ft - mean) ** 2 for ft in fts) / len(fts)
        std = var ** 0.5
        variance_pct = (std / mean * 100) if mean > 0 else 0
    else:
        variance_pct = 0.0

    # Verdict
    stable = (not sess["aborted"]) and (not sess["hang_detected"]) and (not sess["context_lost"])
    if variance_pct > 50 and len(fts) > 30:
        # Crazy frame variance with no thermal/TDR/hang explanation = very likely unstable
        stable = False
        sess["abort_reason"] = sess["abort_reason"] or f"Excessive frame time variance ({variance_pct:.0f}% σ/mean) — probable hidden instability"

    # beta.9 — NO-FRAMES GUARD.  A run that captured (almost) no frames did NOT
    # actually pass — the stress load never really ran (WebGL context died
    # silently, tab backgrounded, telemetry absent).  The old code left `stable`
    # True in that case, so the tuner counted a false pass and could climb past
    # the real ceiling.  Treat too-few-frames as UNSTABLE/invalid.
    MIN_FRAMES_FOR_VERDICT = 20
    frame_data_valid = len(fts) >= MIN_FRAMES_FOR_VERDICT
    if not frame_data_valid and stable:
        stable = False
        sess["abort_reason"] = sess["abort_reason"] or (
            f"No frame data ({len(fts)} frames captured) — stress load did not "
            f"run; treating as unstable rather than a pass")

    # v3.1.1 — classify the failure kind so auto_oc_next can distinguish a
    # hard crash (driver TDR / context loss / hang / pixel artifacts) from
    # a soft fail (thermal / frame variance / low util).  Without this,
    # hard crashes silently downgrade to soft fails and the binary-search
    # refiner happily probes near-crash offsets, sometimes climbing back
    # above the real ceiling on subsequent false-stable readings.
    if not stable:
        if sess.get("context_lost"):
            kind = "context_loss"
        elif sess.get("hang_detected"):
            kind = "hang"
        elif new_tdrs > 0:
            kind = "tdr"
        elif sess.get("pixel_artifacts", 0) > 0:
            kind = "artifacts"
        elif not frame_data_valid:
            kind = "no_frames"
        elif sess.get("thermal_throttle"):
            kind = "thermal"
        elif variance_pct > 50:
            kind = "variance"
        else:
            kind = "soft"
    else:
        kind = "stable"

    verdict = {
        "ok": True,
        "stable": stable,
        "kind": kind,
        "duration_s": round(duration, 1),
        "max_temp_c": sess["max_temp"],
        "min_core_mhz": sess["min_core"] if sess["min_core"] != 999999 else 0,
        "max_core_mhz": sess["max_core"],
        "max_mem_mhz": sess["max_mem"],
        "thermal_throttle": sess["thermal_throttle"],
        "hang_detected": sess["hang_detected"],
        "context_lost": sess["context_lost"],
        "tdr_count": new_tdrs,
        "pixel_artifacts": sess["pixel_artifacts"],
        "low_util_samples": sess["low_util_count"],
        "aborted": sess["aborted"],
        "abort_reason": sess["abort_reason"],
        "sample_count": len(sess["samples"]),
        # Performance stats
        "avg_fps": round(avg_fps, 1),
        "avg_frame_time_ms": round(avg_ft, 2),
        "p99_frame_time_ms": round(p99, 2),
        "frame_variance_pct": round(variance_pct, 1),
        # beta.9 — frame-data validity so the benchmark scorer / auto-OC refiner
        # can reject a run that never actually measured anything (vs treating it
        # as a fast, stable pass).
        "frame_count": len(fts),
        "frame_data_valid": frame_data_valid,
        # beta.9 — real average GPU utilisation over the run, so the benchmark
        # scorer can use it instead of a hardcoded 95 (which made util unable to
        # differentiate rungs and let a barely-loaded run score like a busy one).
        "avg_gpu_util_pct": round(
            sum(s.get("gpu_util_pct", 0) for s in sess["samples"]) / len(sess["samples"]), 1
        ) if sess["samples"] else 0.0,
    }
    log.info(f"Stability probe ended: stable={stable}, max_temp={sess['max_temp']}, fps={avg_fps:.1f}, variance={variance_pct:.0f}%, reason={sess['abort_reason'] or 'OK'}")
    return verdict


# ═══════════════════════════════════════════════════════════════════════════
# Auto-OC v2 — Binary Search + Jump-Step Algorithm
# ═══════════════════════════════════════════════════════════════════════════
#
# Old approach: linear ladder (+15, +30, +45 …) stopping at first crash.
# Problem: SLOW (10 steps × 45s) and stops AT first crash so the real ceiling
# might be just above the crash point — never gets explored.
#
# New approach (per axis):
#   PHASE 1 (jump):   +X, +2X, +3X, … until crash. Big leaps to find the
#                     general region of instability fast.
#   PHASE 2 (refine): binary search between last_stable and first_unstable
#                     until gap ≤ tolerance. Always converges in O(log N).
#
# Speedups:
#   - Per-step durations are PHASE-AWARE (jump=18s, refine=25s, final=60s).
#   - Smaller initial step counts because jumps are big (75 MHz core / 250 MHz mem).
#   - Same total time budget, but explores 2-3× higher ceilings before backing off.
#
# Returns a `recommended_duration_s` field so the frontend uses the right
# stress duration per phase instead of a hardcoded 45s.
# ═══════════════════════════════════════════════════════════════════════════

CORE_REFINE_TOLERANCE_MHZ = 5    # stop core refinement when gap ≤ 5 MHz (tight = pushes to true ceiling)
MEM_REFINE_TOLERANCE_MHZ = 15    # stop memory refinement when gap ≤ 15 MHz

_auto_oc_session = {
    "active": False,
    "phase": "idle",
    # idle | core_jump | core_refine | mem_jump | mem_refine | final_validation | done
    "step": 0,
    "max_iters": 14,             # absolute step cap to prevent runaway
    "current_core": 0,
    "current_mem": 0,
    "stable_core": 0,            # known-stable lower bound for core
    "unstable_core": None,       # known-unstable upper bound for core (None if not yet found)
    "stable_mem": 0,
    "unstable_mem": None,
    "core_jump_mhz": 75,
    "mem_jump_mhz": 250,
    "core_max_offset": MAX_CORE_OFFSET_MHZ,
    "mem_max_offset": MAX_MEM_OFFSET_MHZ,
    "started": 0.0,
    "log": [],                   # list of {step, phase, core, mem, stable, reason, max_temp}
    "done": False,
    "best": None,
    "validated": False,
}


def auto_oc_start(core_step_mhz: int = 75, mem_step_mhz: int = 250, max_steps: int = 14, max_power: bool = True) -> dict:
    """Begin a binary-search auto-OC session.

    Args:
        core_step_mhz: jump step for core (default 75 MHz)
        mem_step_mhz: jump step for memory (default 250 MHz)
        max_steps: hard step cap (default 14 — typically only 6-9 needed)
        max_power: if True (default), maxes the power limit to its hardware ceiling
                   BEFORE searching for clock ceiling. This unlocks higher stable
                   clocks because TDP throttling is removed from the equation.

    The frontend drives it via repeated calls to auto_oc_next(prev_result).
    Each response includes `recommended_duration_s` for the stress test.
    """
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"ok": False, "err": "Auto-OC only supported on NVIDIA cards"}
    _cache_power_default(cap.get("limits", {}).get("power_default_w"))

    # beta.9 — pause the drift watchdog for the whole tuning session.  Otherwise
    # it re-asserts the offset mid-stress (masking the instability we're trying
    # to measure) and, worse, re-pushes a just-crashed offset within ~5s while
    # emergency_reset_oc is trying to recover.  20-min self-healing cap so an
    # abandoned run (UI navigated away) can't leave the watchdog off forever.
    try: pause_oc_watchdog("auto_oc", max_age_s=20 * 60)
    except Exception as e: log.debug(f"pause_oc_watchdog(auto_oc) failed: {e}")

    # Step 0: Max power limit FIRST — gives clocks more headroom to find their real ceiling.
    if max_power:
        limits = cap.get("limits", {})
        max_w = limits.get("power_max_w", 0)
        default_w = limits.get("power_default_w", 0)
        if max_w > default_w > 0:
            pct = int(max_w * 100 / default_w)
            r = apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=pct)
            log.info(f"Auto-OC pre-step: power limit maxed → {max_w}W ({pct}% of default {default_w}W)")
            _auto_oc_session["pre_max_power_pct"] = pct
        else:
            _auto_oc_session["pre_max_power_pct"] = 100
    else:
        _auto_oc_session["pre_max_power_pct"] = 100

    # v3.1.1 — clear any stale OOB crash record from before the session
    # starts so the new session's out-of-band detector only sees crashes
    # that actually happened during this run.
    try: clear_crash_state()
    except Exception: pass

    _auto_oc_session.update({
        "active": True,
        "phase": "core_jump",
        "step": 0,
        "max_iters": max(8, min(20, max_steps)),
        "current_core": 0,
        "current_mem": 0,
        "stable_core": 0,
        "unstable_core": None,
        "stable_mem": 0,
        "unstable_mem": None,
        "core_jump_mhz": max(25, min(150, core_step_mhz)),
        "mem_jump_mhz": max(100, min(500, mem_step_mhz)),
        "core_max_offset": MAX_CORE_OFFSET_MHZ,
        "mem_max_offset": MAX_MEM_OFFSET_MHZ,
        "started": time.time(),
        "log": [],
        "done": False,
        "best": None,
        "validated": False,
        "stock_core_max": cap.get("limits", {}).get("core_max_mhz", 0),
        "stock_mem_max": cap.get("limits", {}).get("mem_max_mhz", 0),
        # v3.1.1 — session-wide crash hardening
        "session_unstable_floor_core": None,   # lowest core offset that ever crashed
        "session_unstable_floor_mem":  None,   # lowest mem offset that ever crashed
        "crash_count_core":            0,
        "crash_count_mem":             0,
        "_last_oob_crash_ts":          0.0,
    })
    log.info(f"Auto-OC v3 started: core_jump={core_step_mhz} mem_jump={mem_step_mhz} max_iters={max_steps} max_power={max_power}")
    return {
        "ok": True,
        "phase": "core_jump",
        "estimated_steps": 9,
        "estimated_time_min": 5,
        "pre_max_power_pct": _auto_oc_session.get("pre_max_power_pct", 100),
    }


def _phase_duration(phase: str) -> int:
    """Recommended stress test duration per phase.

    v2.9.9.0 — Quick Tune mode tightened to be ~40% faster than the v2.9.8
    timings.  Crashes surface within 8-10s under heavy WebGL stress, so
    longer windows mostly burn time without finding new instability.
    For users who want maximum confidence, the new Benchmark Tune mode
    (separate code path) runs longer per step *and* measures performance.
    """
    if phase in ("core_jump", "mem_jump"):
        return 10   # was 18 — crash detection is fast at high stress
    if phase in ("core_refine", "mem_refine"):
        return 15   # was 25 — refinement still needs a bit more confidence
    if phase == "final_validation":
        return 30   # was 60 — sufficient for last-mile validation
    # v2.9.9.7 — post-crash sanity check.  15s is plenty: if the GPU is
    # going to crash again at the last-stable offset, it crashes within
    # the first 8s under heavy WebGL stress.  We give it 7s of headroom.
    if phase in ("_post_crash_settle_core", "_post_crash_settle_mem"):
        return 15
    return 15


def _next_test_value(current: int, stable: int, unstable, jump: int,
                     max_offset: int, tolerance: int,
                     session_floor=None) -> tuple:
    """Compute next value to test using jump-or-refine logic.

    Args:
        session_floor: if not None, NEVER return a value at or above this
                       offset.  This is the session-wide crash floor — any
                       offset that ever crashed (whether the per-probe verdict
                       caught it or not, including out-of-band TDR detection).
                       Provides survival against missed crash detections.

    Returns: (next_value, new_phase_state)
        new_phase_state in {"jump", "refine", "done"}
    """
    # v3.1.1 — apply session crash floor to the effective ceiling.  We
    # leave a small margin (jump // 4, min 10) so we don't probe right
    # up against the known-crash point.
    effective_max = max_offset
    if session_floor is not None:
        margin = max(10, jump // 4)
        effective_max = min(max_offset, max(0, session_floor - margin))
        # Also tighten the unstable bound — if a session crash was lower
        # than our current unstable_core, use the lower one as the upper.
        if unstable is None or unstable > session_floor:
            unstable = session_floor

    # If we don't yet know an unstable upper bound: keep jumping
    if unstable is None:
        nxt = current + jump
        if nxt > effective_max:
            # We've hit the safety ceiling without finding instability.
            # Treat current as the converged max.
            return (min(current, effective_max), "done")
        return (nxt, "jump")

    # We have both bounds — binary search
    if (unstable - stable) <= tolerance:
        return (min(stable, effective_max), "done")
    mid = (stable + unstable) // 2
    # Safety clamp — bring mid below the effective ceiling if needed
    if mid > effective_max:
        mid = effective_max
        if mid <= stable:
            return (stable, "done")
    return (mid, "refine")


def auto_oc_next(prev_result: dict = None) -> dict:
    """Advance auto-OC. See module docstring at top of section for the algorithm."""
    sess = _auto_oc_session
    if not sess["active"]:
        return {"ok": False, "err": "No active auto-OC session"}

    # v3.1.1 — Out-of-band crash detection.  If `crash_recovery.mark_gpu_crashed`
    # fired since the session started (i.e. a TDR / driver crash was logged by
    # the kernel hook OUTSIDE the per-probe verdict path), force-treat the
    # last test as a hard crash regardless of what the verdict said.  This
    # fixes the case where the WebGL probe ends, returns "stable", and THEN
    # the TDR fires a few hundred ms later — the per-probe verdict misses
    # the crash and the binary-search refiner walks UP past the real ceiling.
    crash_state = get_crash_state()
    crashed_during_session = bool(
        crash_state.get("crashed_ts", 0.0) and
        crash_state["crashed_ts"] > sess.get("started", 0.0) and
        crash_state.get("crashed_ts", 0.0) > sess.get("_last_oob_crash_ts", 0.0)
    )
    if crashed_during_session and prev_result is not None:
        # Force-flag this as a hard crash; downstream logic does the
        # right thing (locks stable_core, advances to next axis).
        sess["_last_oob_crash_ts"] = crash_state["crashed_ts"]
        prev_result = dict(prev_result)
        prev_result["stable"]  = False
        prev_result["kind"]    = (prev_result.get("kind") or "") + " oob_crash"
        prev_result["reason"]  = (
            (prev_result.get("reason") or "") +
            f" [out-of-band crash detected: {crash_state.get('kind', 'unknown')}]"
        ).strip()
        log.error(f"Auto-OC: out-of-band crash detected at offset "
                  f"core+{sess.get('current_core', 0)} / mem+{sess.get('current_mem', 0)} "
                  f"(kind={crash_state.get('kind')}) — forcing step-down")

        # v3.1.1 — RETRACT stable_core / stable_mem if we can't trust them.
        # When a probe falsely reported a high offset as stable (e.g. TDR
        # fired right after probe end), the binary-search refiner had
        # already bumped stable_core up to that offset.  If we don't
        # retract it, future binary-search mid-points keep computing
        # against the false stable, and we end up testing offsets ABOVE
        # what's actually safe.  Drop stable_core by one jump step so
        # the search restarts in known-safer territory.
        if sess.get("phase") in ("core_jump", "core_refine"):
            cur  = int(sess.get("current_core", 0))
            sc   = int(sess.get("stable_core", 0))
            jump = int(sess.get("core_jump_mhz", 75))
            if sc >= cur and sc > 0:
                new_sc = max(0, cur - jump)
                log.warning(
                    f"Auto-OC: retracting stable_core +{sc} → +{new_sc} "
                    f"(probe missed the actual crash boundary)"
                )
                sess["stable_core"] = new_sc
        elif sess.get("phase") in ("mem_jump", "mem_refine"):
            cur  = int(sess.get("current_mem", 0))
            sm   = int(sess.get("stable_mem", 0))
            jump = int(sess.get("mem_jump_mhz", 250))
            if sm >= cur and sm > 0:
                new_sm = max(0, cur - jump)
                log.warning(
                    f"Auto-OC: retracting stable_mem +{sm} → +{new_sm} "
                    f"(probe missed the actual crash boundary)"
                )
                sess["stable_mem"] = new_sm

    # Step 1: Process previous result (if any) — update bounds & log
    if prev_result is not None:
        stable = bool(prev_result.get("stable"))
        kind   = (prev_result.get("kind") or "").lower()
        reason = (prev_result.get("reason") or "").lower()
        # v2.9.9.7 — distinguish a "soft fail" (thermal abort, frame variance,
        # low util) from a "hard crash" (TDR / WebGL context loss / driver
        # hang / pixel artifacts).  Hard crashes mean the GPU literally
        # disconnected for a moment; we don't want the binary-search refiner
        # to keep probing in that range and trigger more crashes.
        #
        # v3.1.1 — also check `reason` as a fallback.  The frontend has
        # historically not always forwarded the `kind` field, but reason
        # usually contains the human-readable abort string ("Driver TDR
        # event(s) logged", "WebGL context lost", etc.).  Matching on
        # either prevents hard crashes from being silently downgraded to
        # soft fails.
        _HARD_TOKENS = ("context", "hang", "tdr", "artifact", "crash",
                        "driver", "stopped responding", "webgl", "oob_crash",
                        "out-of-band")
        is_hard_crash = (
            any(k in kind for k in _HARD_TOKENS) or
            any(k in reason for k in _HARD_TOKENS)
        )
        entry = {
            "step":        sess["step"],
            "phase":       sess["phase"],
            "core_offset": sess["current_core"],
            "mem_offset":  sess["current_mem"],
            "stable":      stable,
            "reason":      prev_result.get("reason", ""),
            "kind":        kind,
            "hard_crash":  is_hard_crash,
            "max_temp":    prev_result.get("max_temp_c", 0),
        }
        sess["log"].append(entry)
        log.info(f"Auto-OC step {sess['step']} ({sess['phase']}): "
                 f"core+{sess['current_core']} mem+{sess['current_mem']} → "
                 f"{'STABLE' if stable else 'UNSTABLE: ' + entry['reason']}"
                 f"{' [HARD CRASH]' if is_hard_crash else ''}")

        # v3.1.1 — session-wide crash floor.  Once a core offset has crashed
        # (or the GPU crashed at all during the session), NEVER test at or
        # above that point again, even if some later probe falsely reports
        # the lower value as stable.  Survives missed verdicts.
        if not stable and sess["phase"] in ("core_jump", "core_refine"):
            floor = sess.get("session_unstable_floor_core")
            cur   = int(sess.get("current_core", 0))
            if floor is None or cur < floor:
                sess["session_unstable_floor_core"] = cur
        if not stable and sess["phase"] in ("mem_jump", "mem_refine"):
            floor = sess.get("session_unstable_floor_mem")
            cur   = int(sess.get("current_mem", 0))
            if floor is None or cur < floor:
                sess["session_unstable_floor_mem"] = cur

        if sess["phase"] in ("core_jump", "core_refine"):
            if stable:
                # v3.1.1 — refuse to mark current_core as stable if it's at
                # or above the session crash floor.  Probe miss → bail.
                floor = sess.get("session_unstable_floor_core")
                if floor is not None and sess["current_core"] >= floor:
                    log.warning(
                        f"Auto-OC: probe reported core+{sess['current_core']} "
                        f"stable, but session crash floor is core+{floor} — "
                        f"treating as unstable (probe likely missed a TDR)"
                    )
                    stable = False
                    # Tighten unstable_core down to (or below) the floor
                    if (sess["unstable_core"] is None or
                            sess["current_core"] < sess["unstable_core"]):
                        sess["unstable_core"] = sess["current_core"]
                else:
                    sess["stable_core"] = sess["current_core"]
            if not stable:
                if sess["unstable_core"] is None or sess["current_core"] < sess["unstable_core"]:
                    sess["unstable_core"] = sess["current_core"]
                # v3.1.1 — count crashes per session.  After 3 crashes we
                # bail to the last-stable value instead of continuing to
                # probe near the unstable edge.
                # beta.9 — only HARD crashes (TDR/context/hang/artifacts) consume
                # this budget.  Soft/environmental fails (thermal throttle, frame
                # variance, no-frames measurement misses) still set the unstable
                # bound above and let the binary search continue, but must not
                # prematurely lock the axis before the true ceiling is found.
                if is_hard_crash:
                    sess["crash_count_core"] = int(sess.get("crash_count_core", 0)) + 1
                if (sess["crash_count_core"] >= 3 and
                        sess["stable_core"] > 0 and
                        sess["phase"] in ("core_jump", "core_refine")):
                    log.warning(
                        f"Auto-OC: 3+ core crashes this session — "
                        f"locking core+{sess['stable_core']} and advancing to memory"
                    )
                    sess["phase"] = "_post_crash_settle_core"
                # v2.9.9.7 — STEP-DOWN behaviour.  On a hard crash we abandon
                # this axis and lock whatever stable value we already have.
                # (If stable_core == 0 we never found ANY stable point — abort
                # the session because the GPU isn't safe at +0 either, which
                # means something else is wrong.)
                elif is_hard_crash:
                    if sess["stable_core"] > 0:
                        log.warning(
                            f"Hard crash at core+{sess['current_core']} — locking "
                            f"core+{sess['stable_core']} as winner and advancing to memory axis"
                        )
                        sess["phase"]     = "_post_crash_settle_core"
                        # The frontend will receive a "_post_crash_settle_core"
                        # phase response telling it to apply stable_core, run
                        # a 15s sanity probe, then call back.  We use a leading
                        # underscore so older frontends just see it as a
                        # short stress step, but new frontends can show a
                        # "Recovering from crash, locking core+X" status.
                    else:
                        log.error("Hard crash at the very first core test — aborting session")
                        sess["phase"] = "done"
                        sess["aborted"] = True
                        sess["abort_reason"] = (
                            f"GPU crashed at core+{sess['current_core']} with no known-stable "
                            f"lower bound. Try lowering the core jump step or starting from a "
                            f"lower power limit."
                        )
        elif sess["phase"] in ("mem_jump", "mem_refine"):
            if stable:
                # v3.1.1 — mirror of core path: refuse stable mark above floor
                floor = sess.get("session_unstable_floor_mem")
                if floor is not None and sess["current_mem"] >= floor:
                    log.warning(
                        f"Auto-OC: probe reported mem+{sess['current_mem']} "
                        f"stable, but session crash floor is mem+{floor} — "
                        f"treating as unstable (probe likely missed a TDR)"
                    )
                    stable = False
                    if (sess["unstable_mem"] is None or
                            sess["current_mem"] < sess["unstable_mem"]):
                        sess["unstable_mem"] = sess["current_mem"]
                else:
                    sess["stable_mem"] = sess["current_mem"]
            if not stable:
                if sess["unstable_mem"] is None or sess["current_mem"] < sess["unstable_mem"]:
                    sess["unstable_mem"] = sess["current_mem"]
                # v3.1.1 — mirror of core: 3+ mem crashes lock the axis
                # beta.9 — hard crashes only (see core axis note).
                if is_hard_crash:
                    sess["crash_count_mem"] = int(sess.get("crash_count_mem", 0)) + 1
                if sess["crash_count_mem"] >= 3:
                    log.warning(
                        f"Auto-OC: 3+ mem crashes this session — "
                        f"locking mem+{sess['stable_mem']} and advancing to final validation"
                    )
                    sess["phase"] = "_post_crash_settle_mem"
                elif is_hard_crash:
                    # Lock memory at last-stable (could be 0 if memory crashes
                    # immediately — that just means the core OC interferes
                    # with memory at any positive offset; core-only is fine).
                    log.warning(
                        f"Hard crash at mem+{sess['current_mem']} — locking "
                        f"mem+{sess['stable_mem']} as winner and advancing to final validation"
                    )
                    sess["phase"] = "_post_crash_settle_mem"
        elif sess["phase"] in ("_post_crash_settle_core", "_post_crash_settle_mem"):
            # Frontend just ran a 15s sanity probe at the last-stable offset.
            # If THAT crashed too, the GPU isn't in a recoverable state — bail.
            if is_hard_crash:
                log.error("Last-stable offset crashed during post-crash sanity probe — aborting")
                sess["phase"] = "done"
                sess["aborted"] = True
                sess["abort_reason"] = (
                    "GPU crashed even at the previously-stable offset during recovery. "
                    "Aborting; the GPU may need a driver restart or reboot."
                )
            elif sess["phase"] == "_post_crash_settle_core":
                # Core axis sanity passed — advance to memory axis cleanly
                sess["phase"]         = "mem_jump"
                sess["current_mem"]   = 0
                sess["unstable_mem"]  = None
            elif sess["phase"] == "_post_crash_settle_mem":
                # Memory axis sanity passed — advance to final validation
                sess["phase"]         = "final_validation"
                sess["current_core"]  = sess["stable_core"]
                sess["current_mem"]   = sess["stable_mem"]
        elif sess["phase"] == "final_validation":
            sess["validated"] = stable
            if not stable:
                # Final validation crashed — back off MINIMALLY (just past the noise floor).
                # We're at or near the true ceiling; a small step is enough.
                old_core = sess["stable_core"]
                sess["stable_core"] = max(0, old_core - 15)
                sess["stable_mem"]  = max(0, sess["stable_mem"] - 30)
                log.warning(f"Final validation unstable; minimal backoff to "
                            f"core+{sess['stable_core']} mem+{sess['stable_mem']}")
            sess["phase"] = "done"

    # Step 2: Hard step cap
    sess["step"] += 1
    if sess["step"] > sess["max_iters"] + 4:  # +4 safety buffer
        log.warning("Auto-OC hit max iteration cap; finalizing with current best")
        sess["phase"] = "done"

    # Step 3: Compute next phase + value
    while True:
        # CORE phases ─────────────────────────────────
        if sess["phase"] in ("core_jump", "core_refine"):
            nxt, kind = _next_test_value(
                sess["current_core"], sess["stable_core"], sess["unstable_core"],
                sess["core_jump_mhz"], sess["core_max_offset"], CORE_REFINE_TOLERANCE_MHZ,
                session_floor=sess.get("session_unstable_floor_core"),
            )
            if kind == "done":
                # Core converged — start memory phase
                log.info(f"Auto-OC core converged: stable=+{sess['stable_core']} MHz")
                sess["phase"] = "mem_jump"
                sess["current_mem"] = 0
                sess["unstable_mem"] = None
                continue  # re-enter loop to compute mem step
            sess["current_core"] = nxt
            sess["phase"] = "core_refine" if kind == "refine" else "core_jump"
            break

        # MEM phases ──────────────────────────────────
        if sess["phase"] in ("mem_jump", "mem_refine"):
            nxt, kind = _next_test_value(
                sess["current_mem"], sess["stable_mem"], sess["unstable_mem"],
                sess["mem_jump_mhz"], sess["mem_max_offset"], MEM_REFINE_TOLERANCE_MHZ,
                session_floor=sess.get("session_unstable_floor_mem"),
            )
            if kind == "done":
                log.info(f"Auto-OC mem converged: stable=+{sess['stable_mem']} MHz")
                sess["phase"] = "final_validation"
                sess["current_core"] = sess["stable_core"]
                sess["current_mem"] = sess["stable_mem"]
                break
            sess["current_mem"] = nxt
            sess["phase"] = "mem_refine" if kind == "refine" else "mem_jump"
            break

        # POST-CRASH SETTLE (v2.9.9.7) ────────────────
        # We just had a hard crash on this axis.  Apply the last-known-stable
        # offset and run a 15s sanity probe before advancing.  If THIS crashes
        # too, the prev-result handler above will abort the session; otherwise
        # the prev-result handler advances the phase to mem_jump / final_validation.
        if sess["phase"] == "_post_crash_settle_core":
            sess["current_core"] = sess["stable_core"]
            sess["current_mem"]  = 0
            break
        if sess["phase"] == "_post_crash_settle_mem":
            sess["current_core"] = sess["stable_core"]
            sess["current_mem"]  = sess["stable_mem"]
            break

        # FINAL VALIDATION ────────────────────────────
        if sess["phase"] == "final_validation":
            sess["current_core"] = sess["stable_core"]
            sess["current_mem"] = sess["stable_mem"]
            break

        # DONE ────────────────────────────────────────
        if sess["phase"] == "done":
            # v2.9.9.7 — distinguish "completed normally" from "aborted because
            # we ran out of recovery options".  Aborted sessions reset to
            # stock (because no profile is safe to save) and tell the
            # frontend to show the crash banner instead of the success toast.
            aborted = bool(sess.get("aborted"))
            # beta.9 — the tuning session is over either way; hand the drift
            # watchdog back so it can re-protect the (now stock or validated) OC.
            try: resume_oc_watchdog("auto_oc")
            except Exception: pass
            if aborted:
                # Don't save a profile.  FAIL-LOUD reset to stock: retry a few
                # times, and if every reset path fails, tell the user plainly
                # that the card may still be OC'd (rather than the old code's
                # silent try/except that claimed success regardless).
                reset_ok, reset_detail = _reset_to_stock_hard(attempts=3)
                sess["best"]   = None
                sess["done"]   = True
                sess["active"] = False
                log.error(f"Auto-OC aborted: {sess.get('abort_reason') or 'unknown'} "
                          f"(reset_ok={reset_ok})")
                return {
                    "ok":      True,
                    "done":    True,
                    "phase":   "done",
                    "aborted": True,
                    "abort_reason": sess.get("abort_reason") or "",
                    "reset_ok": reset_ok,
                    "reset_warning": (None if reset_ok else
                        "Could not confirm reset to stock — your card may still be "
                        "overclocked. A reboot is recommended to be safe."),
                    "reset_detail": reset_detail,
                    "log":     sess["log"],
                }
            best = {
                "core_offset_mhz": sess["stable_core"],
                "mem_offset_mhz": sess["stable_mem"],
                "power_pct": sess.get("pre_max_power_pct", 100),  # keep maxed power
            }
            apply_oc(**best)
            # beta.9 — OPT-IN reapply: save WITHOUT arming apply_on_startup so a
            # once-validated lab OC never auto-reapplies on boot (that could
            # black-screen the desktop if it later destabilizes, with no way into
            # the app).  The UI exposes a "reapply this OC every boot" toggle the
            # user turns on once they trust it.
            save_profile({**best, "apply_on_startup": False}, auto=True)
            sess["best"] = best
            sess["done"] = True
            sess["active"] = False
            log.info(f"Auto-OC v3 complete: core+{best['core_offset_mhz']} mem+{best['mem_offset_mhz']} power={best['power_pct']}% (validated={sess.get('validated')})")
            return {
                "ok": True,
                "done": True,
                "phase": "done",
                "best": best,
                "validated": sess.get("validated", False),
                "apply_on_startup": False,
                "log": sess["log"],
            }

    # Step 4: Apply the next test OC and return
    apply_oc(
        core_offset_mhz=sess["current_core"],
        mem_offset_mhz=sess["current_mem"],
        power_pct=100,
    )

    duration = _phase_duration(sess["phase"])
    label = _phase_label(sess)
    return {
        "ok": True,
        "done": False,
        "phase": sess["phase"],
        "step": sess["step"],
        "max_step": sess["max_iters"],
        "current_core": sess["current_core"],
        "current_mem": sess["current_mem"],
        "stable_core": sess["stable_core"],
        "stable_mem": sess["stable_mem"],
        "unstable_core": sess["unstable_core"],
        "unstable_mem": sess["unstable_mem"],
        "recommended_duration_s": duration,
        "step_label": label,
    }


def _phase_label(sess: dict) -> str:
    """Human-readable label for the current step."""
    p = sess["phase"]
    if p == "core_jump":
        return f"Core jump: trying +{sess['current_core']} MHz (looking for first crash)"
    if p == "core_refine":
        return f"Core refine: testing +{sess['current_core']} MHz between +{sess['stable_core']} (stable) and +{sess['unstable_core']} (unstable)"
    if p == "mem_jump":
        return f"Memory jump: trying +{sess['current_mem']} MHz (looking for first crash)"
    if p == "mem_refine":
        return f"Memory refine: testing +{sess['current_mem']} MHz between +{sess['stable_mem']} (stable) and +{sess['unstable_mem']} (unstable)"
    if p == "final_validation":
        return f"Final validation: 60s confidence test at core+{sess['current_core']} mem+{sess['current_mem']}"
    # v2.9.9.7 — post-crash recovery probe.  Tells the user we're stepping
    # down rather than dying.
    if p == "_post_crash_settle_core":
        return f"Recovering from core crash — locking core+{sess['stable_core']} and verifying"
    if p == "_post_crash_settle_mem":
        return f"Recovering from memory crash — locking mem+{sess['stable_mem']} and verifying"
    return p


def auto_oc_cancel() -> dict:
    """Abort auto-OC and reset to stock.  Idempotent — safe to call with no
    active session (returns ok even so)."""
    was_active = bool(_auto_oc_session.get("active"))
    _auto_oc_session["active"] = False
    _auto_oc_session["done"] = False
    # beta.9 — always hand the watchdog back, then fail-loud reset.
    try: resume_oc_watchdog("auto_oc")
    except Exception: pass
    reset_ok, reset_detail = _reset_to_stock_hard(attempts=3)
    log.info(f"Auto-OC cancelled (was_active={was_active}), reset_ok={reset_ok}")
    return {
        "ok": True,
        "reset_ok": reset_ok,
        "reset_warning": (None if reset_ok else
            "Could not confirm reset to stock — your card may still be "
            "overclocked. A reboot is recommended to be safe."),
    }


def auto_oc_status() -> dict:
    """Return current auto-OC session state for UI polling."""
    return dict(_auto_oc_session)


# ═══════════════════════════════════════════════════════════════════════════
# v2.9.9.0 — BENCHMARK-TUNE auto-OC (Mode 2 — slower but smarter)
# ═══════════════════════════════════════════════════════════════════════════
# Where "Quick Tune" hunts for the highest stable offset, Benchmark Tune
# hunts for the offset that produces the *best actual performance*.
#
# Why this matters: on real GPUs, more OC ≠ more FPS.  Push too far and
# you hit thermal throttling, voltage drops, or driver-side caps that
# silently lower performance even though no crash occurs.  The result is
# a "stable" +400 MHz that's actually slower than +250 MHz.
#
# Algorithm (per axis — runs core first, then memory):
#   1. Walk a fixed offset ladder (e.g. 0, 50, 100, 150, 200, 250, 300 MHz).
#   2. For each candidate:
#         a) Apply the offset.
#         b) Run a 5-second warmup + 18-second WebGL benchmark.
#         c) Record verdict: avg_fps, 1%-low fps, frametime σ, max_temp.
#         d) Compute a score (matches the stress-test.py reference):
#               score = avg_fps * 100
#                     + min_fps * 40           (1% lows weighted heavily)
#                     - frametime_std * 20     (variance penalised)
#                     - thermal_penalty
#         e) If the step crashed → mark unstable, STOP this axis (we've
#             passed the ceiling — anything beyond is also unstable).
#   3. Pick the highest-scoring STABLE candidate as the axis winner.
#   4. Move to the next axis.
#   5. Final 30 s validation pass with both winners applied.
#   6. Save the winning combo to the OC profile.
#
# Total runtime: ~6 steps × 23 s × 2 axes + 30 s validation ≈ 5–6 minutes.

# Score weights tuned to favour smooth-and-fast over peak-and-jittery
# (matches the formula in the user's stress-test.py reference).
_BENCH_W_AVG_FPS    = 100.0
_BENCH_W_MIN_FPS    = 40.0
_BENCH_W_FT_STD_PEN = 20.0
_BENCH_W_TEMP_HOT   = 30.0   # per °C above 89
_BENCH_W_TEMP_COOL  = 20.0   # bonus for staying < 70 °C


def _bench_score(avg_fps: float, min_fps: float, frametime_std_ms: float,
                 max_temp_c: float, gpu_util_pct: float) -> float:
    """Stress-test reference score formula. Higher = better.

    v2.9.9.4 — sanity-clamp suspicious values BEFORE scoring.  The frontend
    tries hard to reject bogus per-draw timings (Chromium's gl.finish() lies),
    but if anything slips through we don't want a 3-million-FPS reading to
    "win" the benchmark.  Realistic ranges:
        avg_fps:    0–5000   (above 5k = sync didn't actually sync)
        min_fps:    0–5000
        ft_std_ms:  0–500    (variance > 500ms = stalled, not stable)
    """
    avg_fps  = max(0.0, min(5000.0, float(avg_fps or 0.0)))
    min_fps  = max(0.0, min(5000.0, float(min_fps or 0.0)))
    ft_std   = max(0.0, min(500.0,  float(frametime_std_ms or 0.0)))

    s = avg_fps * _BENCH_W_AVG_FPS
    s += min_fps * _BENCH_W_MIN_FPS
    s -= ft_std * _BENCH_W_FT_STD_PEN
    if max_temp_c is not None:
        if max_temp_c >= 90:
            s -= (max_temp_c - 89) * _BENCH_W_TEMP_HOT
        elif max_temp_c < 70:
            s += _BENCH_W_TEMP_COOL
    if gpu_util_pct is not None:
        s += min(gpu_util_pct, 100.0) * 1.5
    return max(s, 0.0)


_benchmark_oc_session = {
    "active":            False,
    "phase":             "idle",
    # idle | core_each | core_summary | mem_each | mem_summary | final_validate | done
    "step_idx":          0,                   # current ladder index in this axis
    "core_ladder":       [],                  # list of MHz offsets to try
    "mem_ladder":        [],
    "core_results":      [],                  # [{offset, avg_fps, min_fps, ft_std, temp, score, stable}]
    "mem_results":       [],
    "core_winner":       None,                # best-scoring stable offset
    "mem_winner":        None,
    "started":           0.0,
    "done":              False,
    "best":              None,                # final {core, mem, score, stock_score, gain_pct}
    "stock_score":       None,                # baseline at +0/+0 for delta calc
    "log":               [],
}


# v2.9.9.1 — Benchmark Tune cadence rebalanced.
# Old (v2.9.9.0): 50 MHz core / 250 MHz mem steps, 23s per step → ~5-7 min total.
# That mirrored Quick Tune's coarse jumps and missed the actual optimum.
# New target: 45-90 min for a comprehensive hunt.  Smaller steps surface
# the actual peak, longer per-step durations average out frame-time noise
# (especially for the 1%-low calculation, which is sensitive to short windows).
BENCH_CORE_STEP_MHZ          = 25     # was 50
BENCH_MEM_STEP_MHZ           = 100    # was 250
BENCH_PER_STEP_DURATION_S    = 90     # was 23 — 25s warmup + 65s measurement
BENCH_FINAL_VALIDATION_S     = 120    # was 30 — final pass with both winners
BENCH_MAX_LADDER_LEN         = 40     # 25 MHz × up to ~1000 MHz core ceiling


def _default_core_ladder(max_offset: int) -> list[int]:
    """Build the core-clock offset ladder.

    First entry MUST be 0 so we capture a stock baseline score before any OC.
    Subsequent steps are BENCH_CORE_STEP_MHZ apart up to `max_offset` (clamped
    at MAX_CORE_OFFSET_MHZ).  Smaller steps than Quick Tune so we can find the
    actual performance peak — not just where it crashes.
    """
    cap = min(max_offset, MAX_CORE_OFFSET_MHZ)
    if cap <= 0:
        return [0]
    rungs = [0]
    step = BENCH_CORE_STEP_MHZ
    cur = step
    while cur <= cap and len(rungs) < BENCH_MAX_LADDER_LEN:
        rungs.append(cur)
        cur += step
    return rungs


def _default_mem_ladder(max_offset: int) -> list[int]:
    """Memory ladder uses bigger steps because GDDR is more forgiving."""
    cap = min(max_offset, MAX_MEM_OFFSET_MHZ)
    if cap <= 0:
        return [0]
    rungs = [0]
    step = BENCH_MEM_STEP_MHZ
    cur = step
    while cur <= cap and len(rungs) < BENCH_MAX_LADDER_LEN:
        rungs.append(cur)
        cur += step
    return rungs


def benchmark_oc_start(core_max_offset: int = 600, mem_max_offset: int = 2500,
                       max_power: bool = True) -> dict:
    """Begin a Benchmark-Tune session.

    Args:
        core_max_offset: ceiling for the core ladder (default 600 MHz).
                         With 25 MHz steps that's up to 25 candidates.
        mem_max_offset:  ceiling for the memory ladder (default 2500 MHz).
                         With 100 MHz steps that's up to 26 candidates.
        max_power:       if True, max the power limit BEFORE benchmarking
                         so thermal/TDP throttling doesn't skew scores.

    Total runtime depends on where the GPU starts crashing (early crash =
    early stop); typical 45-90 minutes.
    """
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"ok": False, "err": "Benchmark-Tune only supported on NVIDIA cards"}
    _cache_power_default(cap.get("limits", {}).get("power_default_w"))

    # beta.9 — pause the drift watchdog for the whole benchmark session so it
    # can't re-assert an offset mid-measurement or fight post-crash recovery.
    # 4-hour self-healing cap (benchmark runs are long) so an abandoned run
    # can't leave the watchdog off forever.
    try: pause_oc_watchdog("bench_oc", max_age_s=4 * 3600)
    except Exception as e: log.debug(f"pause_oc_watchdog(bench_oc) failed: {e}")

    # Always start from stock for a clean baseline
    reset_oc()

    if max_power:
        limits = cap.get("limits", {})
        max_w = limits.get("power_max_w", 0)
        default_w = limits.get("power_default_w", 0)
        if max_w > default_w > 0:
            pct = int(max_w * 100 / default_w)
            apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=pct)
            log.info(f"Benchmark-Tune: power limit maxed → {max_w}W ({pct}%)")

    core_ladder = _default_core_ladder(core_max_offset)
    mem_ladder  = _default_mem_ladder(mem_max_offset)

    # v3.1.2 — clear stale OOB crash record so this session only sees
    # crashes that actually happen during the run.
    try: clear_crash_state()
    except Exception: pass

    _benchmark_oc_session.update({
        "active":      True,
        "phase":       "core_each",
        "step_idx":    0,
        "core_ladder": core_ladder,
        "mem_ladder":  mem_ladder,
        "core_results":[],
        "mem_results": [],
        "core_winner": None,
        "mem_winner":  None,
        "stock_score": None,
        "started":     time.time(),
        "done":        False,
        "best":        None,
        "log":         [],
        # v3.1.2 — crash hardening (mirrors auto_oc_session)
        "session_unstable_floor_core": None,
        "session_unstable_floor_mem":  None,
        "_last_oob_crash_ts":          0.0,
    })

    total_steps = len(core_ladder) + len(mem_ladder) + 1
    # Time = step_count × per_step (90s) + final validation (120s).  Convert to min.
    estimated_min = int(round((total_steps * BENCH_PER_STEP_DURATION_S
                              + BENCH_FINAL_VALIDATION_S) / 60.0))
    log.info(f"Benchmark-Tune started: core ladder {core_ladder} ({len(core_ladder)} steps), "
             f"mem ladder {mem_ladder} ({len(mem_ladder)} steps), "
             f"~{estimated_min} min if no early crash")
    return {
        "ok": True,
        "phase": "core_each",
        "core_ladder": core_ladder,
        "mem_ladder":  mem_ladder,
        "estimated_steps": total_steps,
        "estimated_time_min": estimated_min,
        "per_step_duration_s": BENCH_PER_STEP_DURATION_S,
    }


def benchmark_oc_next(prev_result: Optional[dict] = None) -> dict:
    """Drive the benchmark state machine.

    Frontend pattern (mirrors auto_oc_next):
        loop:
            r = benchmark_oc_next(prev)
            if r['done']: break
            apply_oc(r['apply_core'], r['apply_mem'], 100)        # frontend handles
            verdict = run_stability_step(r['recommended_duration_s'])
            prev = build_verdict_payload(verdict)

    Each response includes:
        phase, step_idx, total_steps, apply_core, apply_mem,
        recommended_duration_s, current_label
    """
    sess = _benchmark_oc_session
    if not sess["active"]:
        return {"ok": False, "err": "No benchmark session active"}

    phase = sess["phase"]

    # v3.1.2 — Out-of-band crash detection (mirrors auto_oc_next).
    # If a TDR / BSOD / driver crash was recorded by crash_recovery
    # SINCE this benchmark session started, force the most recent step
    # to be marked unstable.  This protects against the case where a
    # benchmark step's stability probe ends ~ms before a TDR fires —
    # without this, the step gets a real "score" and the ladder marches
    # right on into higher (impossible) offsets.
    crash_state = get_crash_state()
    oob_crashed = bool(
        prev_result is not None and
        crash_state.get("crashed_ts", 0.0) and
        crash_state["crashed_ts"] > sess.get("started", 0.0) and
        crash_state["crashed_ts"] > sess.get("_last_oob_crash_ts", 0.0)
    )
    if oob_crashed:
        sess["_last_oob_crash_ts"] = crash_state["crashed_ts"]
        prev_result = dict(prev_result)
        prev_result["stable"]    = False
        prev_result["aborted"]   = True
        prev_result["kind"]      = (prev_result.get("kind") or "") + " oob_crash"
        prev_result["abort_reason"] = (
            (prev_result.get("abort_reason") or "") +
            f" [out-of-band crash detected: {crash_state.get('kind', 'unknown')}]"
        ).strip()
        log.error(f"Benchmark-Tune: out-of-band crash detected at offset "
                  f"core+{sess.get('_pending_core', 0)} / mem+{sess.get('_pending_mem', 0)} "
                  f"(kind={crash_state.get('kind')}) — marking step unstable")

    # ── Record previous step's verdict ──────────────────────────────────
    if prev_result is not None and phase != "idle":
        # Compute score from the verdict the frontend just collected
        avg_fps        = float(prev_result.get("avg_fps") or 0.0)
        avg_ft_ms      = float(prev_result.get("avg_frame_time_ms") or 0.0)
        p99_ft_ms      = float(prev_result.get("p99_frame_time_ms") or 0.0)
        ft_var_pct     = float(prev_result.get("frame_variance_pct") or 0.0)
        max_temp       = float(prev_result.get("max_temp_c") or 0.0)
        max_core_mhz   = float(prev_result.get("max_core_mhz") or 0.0)
        max_mem_mhz    = float(prev_result.get("max_mem_mhz") or 0.0)
        gpu_util       = float(prev_result.get("avg_gpu_util_pct") or 0.0)
        # beta.9 — a run with no/insufficient frame data did not really measure
        # this offset; never let it count as stable or seed the stock baseline.
        frame_ok       = bool(prev_result.get("frame_data_valid", True)) and avg_fps > 0
        stable         = (bool(prev_result.get("stable", False))
                          and not prev_result.get("aborted", False)
                          and frame_ok)

        # v3.1.2 — hard-crash detection via kind+reason (matches auto_oc_next).
        # On hard crash, also invalidate any HIGHER-offset results recorded
        # earlier in this session — if a higher offset was previously marked
        # "stable" but a lower one just crashed, the higher one must have
        # also been a miss.
        kind_str   = (prev_result.get("kind") or "").lower()
        reason_str = (prev_result.get("abort_reason") or "").lower()
        _HARD_TOKENS = ("context", "hang", "tdr", "artifact", "crash",
                        "driver", "stopped responding", "webgl", "oob_crash",
                        "out-of-band")
        is_hard_crash = (
            any(k in kind_str for k in _HARD_TOKENS) or
            any(k in reason_str for k in _HARD_TOKENS)
        )

        # Convert variance % back to a stddev-in-ms (verdict gives σ/mean × 100)
        ft_std_ms = (ft_var_pct / 100.0) * avg_ft_ms if avg_ft_ms > 0 else 0.0
        # 1% low FPS = 1000 / p99 frame time
        min_fps = (1000.0 / p99_ft_ms) if p99_ft_ms > 0 else 0.0

        # beta.9 — pass the REAL average util (was hardcoded 95, which flattened
        # the util term across every rung).  Unstable / invalid-frame runs score 0.
        score = _bench_score(avg_fps, min_fps, ft_std_ms, max_temp, gpu_util) if stable else 0.0

        record = {
            "offset_core":    sess.get("_pending_core", 0),
            "offset_mem":     sess.get("_pending_mem", 0),
            "avg_fps":        round(avg_fps, 1),
            "min_fps":        round(min_fps, 1),
            "frametime_avg_ms": round(avg_ft_ms, 2),
            "frametime_std_ms": round(ft_std_ms, 2),
            "max_temp_c":     int(max_temp),
            "max_core_mhz":   int(max_core_mhz),
            "max_mem_mhz":    int(max_mem_mhz),
            "score":          round(score, 1),
            "stable":         stable,
            "hard_crash":     is_hard_crash,
            "abort_reason":   prev_result.get("abort_reason") or "",
        }

        if phase == "core_each":
            sess["core_results"].append(record)
            # beta.9 — only seed the stock baseline from a STABLE rung 0.  A
            # rung-0 (+0/+0) that read "unstable" is a measurement miss (no
            # frames); using its 0 score as the baseline made every later rung's
            # gain_pct look enormous.
            if sess["step_idx"] == 0 and stable:
                sess["stock_score"] = record["score"]
            log.info(f"Benchmark-Tune core+{record['offset_core']}: "
                     f"score={record['score']} fps={record['avg_fps']} "
                     f"1%low={record['min_fps']} ft_std={record['frametime_std_ms']}ms "
                     f"temp={record['max_temp_c']}°C stable={record['stable']}"
                     f"{' [HARD CRASH]' if is_hard_crash else ''}")
            # Crash → end this axis early (anything higher will also crash)
            if not stable:
                # v3.1.2 — set session crash floor + invalidate the immediately
                # preceding ladder rungs (within 2 step-sizes of the crash).
                # In a forward-walking ladder a crash at +X means the probes
                # at +X-25 and +X-50 are SUSPECT — the GPU might already have
                # been unstable there, but the per-probe verdict was lenient.
                # Throwing them out is safer than trusting them as winners.
                bad_off = record["offset_core"]
                floor   = sess.get("session_unstable_floor_core")
                if floor is None or bad_off < floor:
                    sess["session_unstable_floor_core"] = bad_off
                # Invalidate records within `BENCH_CORE_STEP_MHZ * 2` below
                # the crash.  This catches the "probe ended ~ms before TDR
                # fired" miss-detection pattern.  Lower offsets are kept —
                # they were probed earlier and are reasonable winners.
                invalidation_window = BENCH_CORE_STEP_MHZ * 2
                for r in sess["core_results"]:
                    if r is record:
                        continue
                    if (r["stable"] and
                        r["offset_core"] < bad_off and
                        (bad_off - r["offset_core"]) <= invalidation_window):
                        log.warning(
                            f"Benchmark-Tune: retroactively invalidating "
                            f"core+{r['offset_core']} (within {invalidation_window} "
                            f"MHz of crash at core+{bad_off} — probe likely "
                            f"missed an early-stage instability)"
                        )
                        r["stable"]     = False
                        r["score"]      = 0.0
                        r["hard_crash"] = True
                        r["abort_reason"] = (r.get("abort_reason") or "") + \
                            f" [invalidated: crashed {bad_off - r['offset_core']} MHz higher]"
                sess["phase"] = "core_summary"

        elif phase == "mem_each":
            sess["mem_results"].append(record)
            log.info(f"Benchmark-Tune mem+{record['offset_mem']}: "
                     f"score={record['score']} fps={record['avg_fps']} "
                     f"1%low={record['min_fps']} stable={record['stable']}"
                     f"{' [HARD CRASH]' if is_hard_crash else ''}")
            if not stable:
                bad_off = record["offset_mem"]
                floor   = sess.get("session_unstable_floor_mem")
                if floor is None or bad_off < floor:
                    sess["session_unstable_floor_mem"] = bad_off
                # Same window-based invalidation as core
                invalidation_window = BENCH_MEM_STEP_MHZ * 2
                for r in sess["mem_results"]:
                    if r is record:
                        continue
                    if (r["stable"] and
                        r["offset_mem"] < bad_off and
                        (bad_off - r["offset_mem"]) <= invalidation_window):
                        log.warning(
                            f"Benchmark-Tune: retroactively invalidating "
                            f"mem+{r['offset_mem']} (within {invalidation_window} "
                            f"MHz of crash at mem+{bad_off})"
                        )
                        r["stable"]     = False
                        r["score"]      = 0.0
                        r["hard_crash"] = True
                        r["abort_reason"] = (r.get("abort_reason") or "") + \
                            f" [invalidated: crashed {bad_off - r['offset_mem']} MHz higher]"
                sess["phase"] = "mem_summary"

        elif phase == "final_validate":
            # Final pass — record but don't change winner choice
            sess["final_record"] = record
            sess["phase"] = "done"
            sess["done"]  = True
            sess["active"] = False

    # ── Advance state machine ──────────────────────────────────────────
    phase = sess["phase"]   # may have changed above

    # Core axis: walk the ladder
    if phase == "core_each":
        # v3.1.2 — skip ladder rungs at or above the session crash floor.
        # Even with the retroactive-invalidation guard, never even APPLY
        # an offset we know is unsafe — saves time + avoids re-crashing.
        floor = sess.get("session_unstable_floor_core")
        while (sess["step_idx"] < len(sess["core_ladder"]) and
               floor is not None and
               sess["core_ladder"][sess["step_idx"]] >= floor):
            log.info(f"Benchmark-Tune: skipping core+{sess['core_ladder'][sess['step_idx']]} "
                     f"(at/above session crash floor +{floor})")
            sess["step_idx"] += 1
        if sess["step_idx"] < len(sess["core_ladder"]):
            offset = sess["core_ladder"][sess["step_idx"]]
            sess["_pending_core"] = offset
            sess["_pending_mem"]  = 0
            sess["step_idx"]      += 1
            return {
                "ok":           True,
                "phase":        "core_each",
                "step_idx":     sess["step_idx"],
                "total_steps":  len(sess["core_ladder"]) + len(sess["mem_ladder"]) + 1,
                "apply_core":   offset,
                "apply_mem":    0,
                # v2.9.9.1 — 90s per step for accurate FPS / 1%-low / variance.
                # Frontend already applies a settle delay before the run, so
                # the full 90s is split as ~25s warmup + ~65s measurement.
                "recommended_duration_s": BENCH_PER_STEP_DURATION_S,
                "current_label": f"Core +{offset} MHz",
            }
        sess["phase"] = "core_summary"

    if phase == "core_summary" or sess["phase"] == "core_summary":
        # Pick the highest-scoring STABLE core offset
        stable_records = [r for r in sess["core_results"] if r["stable"]]
        if stable_records:
            winner = max(stable_records, key=lambda r: r["score"])
            sess["core_winner"] = winner["offset_core"]
            log.info(f"Core winner: +{winner['offset_core']} MHz (score {winner['score']})")
        else:
            sess["core_winner"] = 0
            log.warning("Benchmark-Tune: no stable core offset found, using +0")
        # Apply the winning core offset before starting the memory ladder
        apply_oc(core_offset_mhz=sess["core_winner"], mem_offset_mhz=0, power_pct=100)
        sess["phase"]    = "mem_each"
        sess["step_idx"] = 0
        phase = "mem_each"

    if phase == "mem_each":
        # v3.1.2 — same ladder-skip protection for memory
        floor_m = sess.get("session_unstable_floor_mem")
        while (sess["step_idx"] < len(sess["mem_ladder"]) and
               floor_m is not None and
               sess["mem_ladder"][sess["step_idx"]] >= floor_m):
            log.info(f"Benchmark-Tune: skipping mem+{sess['mem_ladder'][sess['step_idx']]} "
                     f"(at/above session crash floor +{floor_m})")
            sess["step_idx"] += 1
        if sess["step_idx"] < len(sess["mem_ladder"]):
            offset = sess["mem_ladder"][sess["step_idx"]]
            sess["_pending_core"] = sess["core_winner"]
            sess["_pending_mem"]  = offset
            sess["step_idx"]      += 1
            return {
                "ok":           True,
                "phase":        "mem_each",
                "step_idx":     sess["step_idx"],
                "total_steps":  len(sess["core_ladder"]) + len(sess["mem_ladder"]) + 1,
                "apply_core":   sess["core_winner"],
                "apply_mem":    offset,
                "recommended_duration_s": BENCH_PER_STEP_DURATION_S,
                "current_label": f"Core +{sess['core_winner']} MHz, Mem +{offset} MHz",
            }
        sess["phase"] = "mem_summary"

    if sess["phase"] == "mem_summary":
        stable_records = [r for r in sess["mem_results"] if r["stable"]]
        if stable_records:
            winner = max(stable_records, key=lambda r: r["score"])
            sess["mem_winner"] = winner["offset_mem"]
            log.info(f"Memory winner: +{winner['offset_mem']} MHz (score {winner['score']})")
        else:
            sess["mem_winner"] = 0
        sess["phase"] = "final_validate"

    if sess["phase"] == "final_validate":
        # Apply the combined winner and run a longer validation pass
        apply_oc(core_offset_mhz=sess["core_winner"],
                 mem_offset_mhz=sess["mem_winner"], power_pct=100)
        sess["_pending_core"] = sess["core_winner"]
        sess["_pending_mem"]  = sess["mem_winner"]
        return {
            "ok":           True,
            "phase":        "final_validate",
            "step_idx":     len(sess["core_ladder"]) + len(sess["mem_ladder"]) + 1,
            "total_steps":  len(sess["core_ladder"]) + len(sess["mem_ladder"]) + 1,
            "apply_core":   sess["core_winner"],
            "apply_mem":    sess["mem_winner"],
            "recommended_duration_s": BENCH_FINAL_VALIDATION_S,
            "current_label": f"Final validation — core +{sess['core_winner']}, mem +{sess['mem_winner']}",
        }

    # done
    if sess["phase"] == "done":
        # Compose the final summary
        final = sess.get("final_record") or {}
        gain_pct = 0.0
        stock = sess.get("stock_score") or 0.0
        if stock > 0:
            gain_pct = round(((final.get("score", 0) - stock) / stock) * 100.0, 1)

        sess["best"] = {
            "core_offset_mhz": sess["core_winner"],
            "mem_offset_mhz":  sess["mem_winner"],
            "power_pct":       100,
            "stock_score":     stock,
            "best_score":      final.get("score", 0),
            "gain_pct":        gain_pct,
            "avg_fps":         final.get("avg_fps"),
            "min_fps":         final.get("min_fps"),
            "frametime_std_ms": final.get("frametime_std_ms"),
        }

        # Save winning OC to profile (opt-in reapply — never auto-applies on boot)
        try:
            save_profile({
                "core_offset_mhz": sess["core_winner"],
                "mem_offset_mhz":  sess["mem_winner"],
                "power_pct":       100,
                "apply_on_startup": False,
                "tuned_via": "benchmark",
                "score": final.get("score", 0),
            })
        except Exception as e:
            log.warning(f"Could not save benchmark-tune profile: {e}")

        # beta.9 — session over: mark inactive + hand the watchdog back.
        sess["active"] = False
        sess["done"]   = True
        try: resume_oc_watchdog("bench_oc")
        except Exception: pass

        return {
            "ok":   True,
            "done": True,
            "best": sess["best"],
            "apply_on_startup": False,
            "core_results": sess["core_results"],
            "mem_results":  sess["mem_results"],
            "final": final,
        }

    return {"ok": False, "err": f"Unknown phase: {sess['phase']}"}


def benchmark_oc_cancel() -> dict:
    """Abort benchmark-tune and reset to stock.  Idempotent."""
    _benchmark_oc_session["active"] = False
    _benchmark_oc_session["done"]   = False
    try: resume_oc_watchdog("bench_oc")
    except Exception: pass
    reset_ok, _ = _reset_to_stock_hard(attempts=3)
    log.info(f"Benchmark-Tune cancelled, reset_ok={reset_ok}")
    return {
        "ok": True,
        "reset_ok": reset_ok,
        "reset_warning": (None if reset_ok else
            "Could not confirm reset to stock — your card may still be "
            "overclocked. A reboot is recommended to be safe."),
    }


def benchmark_oc_status() -> dict:
    return dict(_benchmark_oc_session)


# ═══════════════════════════════════════════════════════════════════════════
# Benchmark — before/after FPS comparison
# ═══════════════════════════════════════════════════════════════════════════
#
# A benchmark is two stability probes back-to-back: one at stock, one at OC.
# The frontend drives the WebGL workload; this module just records the
# verdict from each probe and computes deltas.
# ═══════════════════════════════════════════════════════════════════════════

_benchmark_state = {
    "active": False,
    "phase": "idle",   # idle | stock_warmup | stock_measure | oc_warmup | oc_measure | done
    "started": 0.0,
    "stock_result": None,
    "oc_result": None,
    "saved_profile": None,  # so we can restore it after benchmark
}


def benchmark_start(saved_oc: dict = None) -> dict:
    """Start a before/after benchmark. Frontend will drive the workload.

    Args:
        saved_oc: dict with core_offset_mhz, mem_offset_mhz, power_pct to apply
                  during the OC phase. If None, uses current saved profile.
    """
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"ok": False, "err": "Benchmark requires NVIDIA GPU"}

    # Determine OC config to use
    if saved_oc is None:
        p = load_profile()
        if not p.get("exists"):
            return {"ok": False, "err": "No saved OC profile and none provided. Apply an OC first."}
        saved_oc = {
            "core_offset_mhz": p.get("core_offset_mhz", 0),
            "mem_offset_mhz": p.get("mem_offset_mhz", 0),
            "power_pct": p.get("power_pct", 100),
        }

    # First step: reset to stock for the baseline measurement
    reset_oc()

    _benchmark_state.update({
        "active": True,
        "phase": "stock_measure",
        "started": time.time(),
        "stock_result": None,
        "oc_result": None,
        "saved_profile": saved_oc,
    })
    log.info(f"Benchmark started — stock first, then OC: {saved_oc}")
    return {
        "ok": True,
        "phase": "stock_measure",
        "next_action": "run_stress",
        "duration_s": 30,
        "label": "Stock baseline measurement",
    }


def benchmark_record_result(result: dict) -> dict:
    """Frontend submits the verdict from a probe. We move to next phase.

    Args:
        result: dict from end_stability_probe() — has avg_fps, min_core_mhz, etc.
    """
    if not _benchmark_state["active"]:
        return {"ok": False, "err": "No active benchmark"}

    sess = _benchmark_state
    if sess["phase"] == "stock_measure":
        sess["stock_result"] = result
        # Now apply OC
        oc = sess["saved_profile"]
        apply_oc(**oc)
        # Brief settle delay (apply_oc returns fast but the GPU needs ~1s for clocks to stabilize)
        time.sleep(2)
        sess["phase"] = "oc_measure"
        log.info(f"Benchmark stock done: {result.get('avg_fps')} FPS — applied OC, ready for OC measurement")
        return {
            "ok": True,
            "phase": "oc_measure",
            "next_action": "run_stress",
            "duration_s": 30,
            "label": f"OC measurement (core+{oc['core_offset_mhz']} mem+{oc['mem_offset_mhz']} power {oc['power_pct']}%)",
            "stock_result": result,
        }

    elif sess["phase"] == "oc_measure":
        sess["oc_result"] = result
        sess["phase"] = "done"
        sess["active"] = False
        # Compute deltas
        stock = sess["stock_result"] or {}
        oc = result or {}
        delta = _compute_benchmark_delta(stock, oc)
        # Use a safe default before applying the +.1f format spec — None or
        # missing keys would otherwise raise TypeError inside the log call.
        _fps_pct = delta.get('fps_pct') or 0.0
        log.info(f"Benchmark complete — stock {stock.get('avg_fps')} FPS vs OC {oc.get('avg_fps')} FPS (Δ {_fps_pct:+.1f}%)")
        return {
            "ok": True,
            "phase": "done",
            "done": True,
            "stock": stock,
            "oc": oc,
            "delta": delta,
            "applied_oc": sess["saved_profile"],
        }

    return {"ok": False, "err": f"Unexpected phase: {sess['phase']}"}


def _compute_benchmark_delta(stock: dict, oc: dict) -> dict:
    """Compute performance delta between stock and OC results."""
    s_fps = float(stock.get("avg_fps", 0) or 0)
    o_fps = float(oc.get("avg_fps", 0) or 0)
    fps_delta = o_fps - s_fps
    fps_pct = (fps_delta / s_fps * 100) if s_fps > 0 else 0

    s_ft = float(stock.get("avg_frame_time_ms", 0) or 0)
    o_ft = float(oc.get("avg_frame_time_ms", 0) or 0)
    ft_delta_ms = o_ft - s_ft

    s_p99 = float(stock.get("p99_frame_time_ms", 0) or 0)
    o_p99 = float(oc.get("p99_frame_time_ms", 0) or 0)
    p99_delta = o_p99 - s_p99

    s_core = int(stock.get("max_core_mhz", 0) or 0)
    o_core = int(oc.get("max_core_mhz", 0) or 0)
    core_delta = o_core - s_core

    s_temp = int(stock.get("max_temp_c", 0) or 0)
    o_temp = int(oc.get("max_temp_c", 0) or 0)
    temp_delta = o_temp - s_temp

    return {
        "fps_delta": round(fps_delta, 1),
        "fps_pct": round(fps_pct, 1),
        "frame_time_delta_ms": round(ft_delta_ms, 2),
        "p99_delta_ms": round(p99_delta, 2),
        "core_delta_mhz": core_delta,
        "temp_delta_c": temp_delta,
    }


def benchmark_cancel() -> dict:
    """Abort benchmark. Restores OC profile if one was active."""
    if _benchmark_state.get("phase") in ("oc_measure", "done"):
        # OC was applied; leave it
        pass
    else:
        # Stock phase was active — re-apply saved profile
        p = _benchmark_state.get("saved_profile")
        if p:
            apply_oc(**p)
    _benchmark_state["active"] = False
    _benchmark_state["phase"] = "idle"
    return {"ok": True}


def benchmark_status() -> dict:
    return dict(_benchmark_state)


# ═══════════════════════════════════════════════════════════════════════════
# Profile persistence
# ═══════════════════════════════════════════════════════════════════════════

def save_profile(profile: dict, auto: bool = False) -> dict:
    """Persist an OC profile to disk. `profile` has core_offset_mhz, mem_offset_mhz, power_pct."""
    try:
        profile = {
            "core_offset_mhz": int(profile.get("core_offset_mhz", 0)),
            "mem_offset_mhz": int(profile.get("mem_offset_mhz", 0)),
            "power_pct": int(profile.get("power_pct", 100)),
            "apply_on_startup": bool(profile.get("apply_on_startup", True)),
            "created_auto": bool(auto),
            "saved_at": time.time(),
        }
        atomic_write_json(OC_PROFILE_PATH, profile)

        # Append to history
        history = []
        if os.path.exists(OC_HISTORY_PATH):
            try:
                with open(OC_HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(profile)
        history = history[-50:]  # keep last 50
        atomic_write_json(OC_HISTORY_PATH, history)

        log.info(f"OC profile saved: {profile}")
        return {"ok": True, "profile": profile}
    except Exception as e:
        log.error(f"Failed to save OC profile: {e}")
        return {"ok": False, "err": str(e)}


def set_profile_apply_on_startup(enabled: bool) -> dict:
    """beta.9 — atomically flip the 'reapply on every boot' flag on the CURRENT
    saved profile, without the caller having to re-send the whole profile.

    Backs the opt-in UI toggle: auto-tune saves the found OC with
    apply_on_startup=False; the user enables boot-reapply here once they trust
    it.  Preserves created_auto and the rest of the profile."""
    p = load_profile()
    if not p or not p.get("exists"):
        return {"ok": False, "err": "No saved OC profile to update"}
    return save_profile({
        "core_offset_mhz": p.get("core_offset_mhz", 0),
        "mem_offset_mhz":  p.get("mem_offset_mhz", 0),
        "power_pct":       p.get("power_pct", 100),
        "apply_on_startup": bool(enabled),
    }, auto=bool(p.get("created_auto", False)))


def load_profile() -> dict:
    """Read the saved OC profile. Returns None-equivalent dict if none exists."""
    if not os.path.exists(OC_PROFILE_PATH):
        return {"exists": False}
    try:
        with open(OC_PROFILE_PATH, "r", encoding="utf-8") as f:
            p = json.load(f)
        p["exists"] = True
        return p
    except Exception as e:
        log.warning(f"Failed to load OC profile: {e}")
        return {"exists": False, "err": str(e)}


def delete_profile() -> dict:
    """Remove the saved profile and reset GPU to stock."""
    try:
        if os.path.exists(OC_PROFILE_PATH):
            os.remove(OC_PROFILE_PATH)
        reset_oc()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def get_history() -> list:
    """Return list of recent OC attempts."""
    if not os.path.exists(OC_HISTORY_PATH):
        return []
    try:
        with open(OC_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def apply_saved_profile_on_startup() -> dict:
    """Called from app.py at startup. Loads profile and applies it silently."""
    p = load_profile()
    if not p.get("exists") or not p.get("apply_on_startup", True):
        return {"applied": False, "reason": "No saved profile or apply-on-startup disabled"}

    # Only apply if NVIDIA card present
    cap = detect_gpu_oc_capability()
    if cap["vendor"] != "nvidia":
        return {"applied": False, "reason": f"GPU vendor is {cap['vendor']}, not NVIDIA"}

    result = apply_oc(
        core_offset_mhz=p.get("core_offset_mhz", 0),
        mem_offset_mhz=p.get("mem_offset_mhz", 0),
        power_pct=p.get("power_pct", 100),
    )
    log.info(f"OC profile applied on startup: core+{p.get('core_offset_mhz')} mem+{p.get('mem_offset_mhz')} power {p.get('power_pct')}%")
    return {"applied": result.get("ok", False), "profile": p, "result": result}
