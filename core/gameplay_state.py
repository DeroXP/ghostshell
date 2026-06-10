"""Gameplay-vs-menu classifier (v3.2-beta.3).

Adaptive Tuning's stability score is only meaningful during actual
gameplay.  Menus, loading screens, pause screens, and multiplayer
lobbies all look different to the GPU than real play:

  • Cyberpunk: gameplay ~75 fps, menus ~240 fps (uncapped)
  • Call of Duty: lobby ~80 fps, gameplay ~140 fps
  • Most games: menu = static UI = vsync'd at refresh rate, near-zero
    frametime variance, low GPU util

If AT scored a step-up during a menu (e.g. "score 100, frametime σ
0.2 ms, util steady") it would learn that the OC is "stable" based
on a workload completely unlike actual gameplay.  Then when the
player returns to combat the OC crashes the GPU.  Or AT step-down
fires during a low-fps lobby and undoes a valid OC.

This module classifies each AT poll as one of:

    "gameplay"          confident the player is in real gameplay
    "menu"              confident the player is in a menu / lobby
    "transitional"      can't tell yet — recently switched
    "baseline-building" gathering initial samples after engagement

AT skips decisions whenever the state isn't "gameplay".  The
classification is based on three orthogonal signals:

  1. FPS deviation from rolling median (BIDIRECTIONAL — fps can
     spike OR drop in menus, depending on game).
  2. GPU utilization drop relative to baseline.
  3. Frametime variance collapse (menus tend to be perfectly steady
     because nothing dynamic is rendering).

Each signal contributes weighted +/- points toward menu_score and
gameplay_score.  Final state is gameplay_score - menu_score with
hysteresis to prevent flapping on a single weird reading.

Per-game baselines are persisted to disk so the second session at
the same game inherits the median observations from the first.

Public API
──────────
    observe(exe, stats)          push a sample into the rolling window
    classify(exe, stats=None)    classify the current state
    get_state(exe)               current cached state + reasons
    get_baseline(exe)            inspect the baseline used
    recalibrate_baseline(exe)    snapshot current as fresh baseline
    reset(exe)                   wipe state for one game
"""
from __future__ import annotations

import ctypes
import json
import os
import statistics
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("gameplay_state")

# ─── Paths + tuning knobs ────────────────────────────────────────────
BASELINES_PATH    = os.path.join(APPDATA_DIR, "gameplay_baselines.json")
SAMPLES_WINDOW    = 600        # ring buffer size per game (~10 min @ 1 Hz)
MIN_SAMPLES_FOR_CLASSIFY = 60  # need ~60 s of data before we'll classify

# Hysteresis — N consecutive classifications of the OPPOSITE state
# before transitioning.  Prevents flapping when a quick reload screen
# blips us into "menu" for 2 s.
HYSTERESIS_TICKS  = 3

# Signal thresholds — picked from observing real games during dev.
# FPS deviation: >1.5x baseline = menu, <0.5x baseline = menu (lobby)
FPS_MENU_HIGH_RATIO   = 1.5
FPS_MENU_LOW_RATIO    = 0.5
FPS_GAMEPLAY_LOW_RATIO  = 0.8
FPS_GAMEPLAY_HIGH_RATIO = 1.2

UTIL_MENU_DROP_RATIO  = 0.7        # current_util < 70% of baseline = menu signal
UTIL_GAMEPLAY_BAND    = 0.10       # within ±10% of baseline = gameplay
UTIL_ABSOLUTE_MENU    = 15         # absolute floor — below this = almost certainly menu

FT_VAR_MENU_QUIET     = 0.3        # current variance < 30% of baseline = vsync'd menu
FT_VAR_GAMEPLAY_BAND  = (0.7, 1.5) # within this multiple of baseline = gameplay

# v3.2-beta.4 — extra signals
CPU_MENU_DROP_RATIO   = 0.7        # game process CPU < 70% of baseline = menu signal
CPU_GAMEPLAY_BAND     = 0.15       # within ±15% of baseline = gameplay
MEMBW_MENU_FLAT_RATIO = 0.4        # membw stddev < 40% of baseline stddev = static menu
MEMBW_GAMEPLAY_BAND   = (0.6, 1.6) # membw stddev within this band of baseline = gameplay
MENU_SIG_FPS_TOL      = 0.10       # current fps within ±10% of a signature
MENU_SIG_UTIL_TOL     = 12         # current util within ±12 abs points
AUTO_LEARN_MIN_TICKS  = 30         # need this many consecutive menu ticks to auto-learn
MAX_MENU_SIGNATURES   = 5          # cap learned signatures per game

# Confidence weights
W_FPS_MENU_HIGH       = 0.45
W_FPS_MENU_LOW        = 0.35
W_FPS_GAMEPLAY        = 0.40
W_UTIL_MENU_DROP      = 0.20
W_UTIL_GAMEPLAY       = 0.20
W_UTIL_ABSOLUTE       = 0.40
W_FTVAR_MENU          = 0.25
W_FTVAR_GAMEPLAY      = 0.20
# v3.2-beta.4 additions
W_FG_NOT_GAME         = 0.50    # game is not the foreground window — dominant signal
W_CPU_MENU_DROP       = 0.25
W_CPU_GAMEPLAY        = 0.15
W_MEMBW_MENU          = 0.20
W_MEMBW_GAMEPLAY      = 0.15
W_KNOWN_MENU_SIG      = 0.50    # current sample matches a learned menu signature

# ─── Per-target PID tracking (for foreground + CPU) ─────────────────
# AT calls set_target() on engage with the game's PID so we can sample
# the right process's CPU + check whether it owns the foreground window.
_target_pid: dict = {}        # exe -> pid
_psutil_proc: dict = {}       # exe -> psutil.Process handle (cached)
_target_lock = threading.Lock()


def set_target(exe: str, pid: Optional[int]) -> None:
    """AT calls this on engage so the classifier knows which PID to
    sample CPU/foreground for.  pid=None clears the target."""
    key = (exe or "").lower().split("\\")[-1].split("/")[-1].strip()
    if not key:
        return
    with _target_lock:
        if pid:
            _target_pid[key] = int(pid)
            # Reset psutil handle so cpu_percent starts fresh
            try:
                import psutil
                _psutil_proc[key] = psutil.Process(int(pid))
                # First cpu_percent call sets the reference time; the
                # second (next observe()) returns a meaningful value.
                try: _psutil_proc[key].cpu_percent(interval=None)
                except Exception: pass
            except Exception as e:
                log.debug(f"psutil.Process({pid}) failed: {e}")
                _psutil_proc.pop(key, None)
        else:
            _target_pid.pop(key, None)
            _psutil_proc.pop(key, None)


def _get_foreground_pid() -> Optional[int]:
    """Return the PID that owns the foreground window, or None.  Used
    to detect alt-tabs — if the user pulled GhostShell / Chrome / Discord
    forward, they aren't actively playing right now."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) if pid.value else None
    except Exception:
        return None


def _get_process_cpu(exe_key: str) -> Optional[float]:
    """Per-process CPU usage (percent).  Returns None if psutil failed
    or the process is gone.  cpu_percent(interval=None) averages over
    time since the last call on the same Process object — that's why
    we cache the Process handle in `_psutil_proc`."""
    with _target_lock:
        proc = _psutil_proc.get(exe_key)
    if not proc:
        return None
    try:
        return float(proc.cpu_percent(interval=None))
    except Exception:
        # Process likely died — drop the cached handle so we don't
        # keep retrying on a stale PID.
        with _target_lock:
            _psutil_proc.pop(exe_key, None)
        return None


# ─── Persistent baselines + learned menu signatures ──────────────────
# Per-game baseline = {fps_p50, util_p50, ftvar_p50, cpu_p50,
#                       membw_std_p50, sample_count, updated_at,
#                       manual: bool,
#                       menu_signatures: [{fps_lo, fps_hi, util_lo,
#                                          util_hi, source, learned_at}]}
_baselines: dict = {}
_baselines_lock = threading.Lock()


def _load_baselines() -> None:
    global _baselines
    if not os.path.exists(BASELINES_PATH):
        _baselines = {}
        return
    try:
        with open(BASELINES_PATH, "r", encoding="utf-8") as f:
            _baselines = json.load(f) or {}
    except Exception as e:
        log.warning(f"Could not load baselines: {e}")
        _baselines = {}


def _save_baselines() -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        tmp = BASELINES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_baselines, f, indent=2)
        os.replace(tmp, BASELINES_PATH)
    except Exception as e:
        log.warning(f"Could not save baselines: {e}")


_load_baselines()


# ─── Rolling sample windows + state ──────────────────────────────────
_samples: dict = {}      # exe -> deque[{ts, fps, util, ftvar, power}]
_state_cache: dict = {}  # exe -> {state, confidence, reasons, ticks_in_state}
_consecutive_alt: dict = {} # exe -> count of consecutive alt-state classifications
_samples_lock = threading.Lock()


def _ensure(exe: str) -> None:
    with _samples_lock:
        if exe not in _samples:
            _samples[exe] = deque(maxlen=SAMPLES_WINDOW)
        if exe not in _state_cache:
            _state_cache[exe] = {
                "state":      "baseline-building",
                "confidence": 0.0,
                "reasons":    [],
                "ticks_in_state": 0,
                "updated_at": time.time(),
            }
        if exe not in _consecutive_alt:
            _consecutive_alt[exe] = 0


def _norm_exe(exe: str) -> str:
    return (exe or "").lower().split("\\")[-1].split("/")[-1].strip()


# ─── Observation + baseline-building ─────────────────────────────────

def observe(exe: str, fps: Optional[float] = None,
            util: Optional[float] = None,
            ftvar: Optional[float] = None,
            power: Optional[float] = None,
            membw: Optional[float] = None) -> dict:
    """Push one sample into the rolling window for this game.  AT
    calls this on every poll with the latest perf_monitor + frame
    telemetry numbers.  Returns the freshly-computed classification.

    v3.2-beta.4 — also samples per-process CPU usage and foreground
    state internally (no need for AT to pass them).
    """
    key = _norm_exe(exe)
    if not key:
        return {"state": "unknown", "reasons": ["no exe"]}
    _ensure(key)

    # v3.2-beta.4 — sample CPU + foreground state here so AT doesn't
    # have to do it.  These are cheap reads.
    cpu = _get_process_cpu(key)
    with _target_lock:
        target_pid = _target_pid.get(key)
    fg_pid = _get_foreground_pid()
    fg_is_game = (target_pid is not None and fg_pid is not None and target_pid == fg_pid)

    sample = {
        "ts":         time.time(),
        "fps":        float(fps)   if fps   is not None else None,
        "util":       float(util)  if util  is not None else None,
        "ftvar":      float(ftvar) if ftvar is not None else None,
        "power":      float(power) if power is not None else None,
        "membw":      float(membw) if membw is not None else None,
        "cpu":        cpu,                                       # process %
        "fg_is_game": bool(fg_is_game) if target_pid else None,  # None when we don't know the PID
    }
    with _samples_lock:
        _samples[key].append(sample)
        sample_count = len(_samples[key])

    # After we have enough samples, lazily update the baseline once a
    # minute.  Use trimmed median so brief excursions (a loading screen
    # blip) don't poison the baseline.
    with _baselines_lock:
        b = _baselines.get(key)
        needs_update = (
            b is None or
            (not b.get("manual") and
             time.time() - (b.get("updated_at") or 0) > 60)
        )
    if needs_update and sample_count >= MIN_SAMPLES_FOR_CLASSIFY:
        _rebuild_baseline(key)

    return classify(key, sample)


def _trimmed_median(values: list, trim_pct: float = 0.1) -> Optional[float]:
    """Median over the middle (1 - 2*trim_pct) of the values.  Robust
    to short bursts of menu data poisoning the baseline."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    cut = int(n * trim_pct)
    if cut > 0:
        vals = vals[cut:-cut] or vals
    return statistics.median(vals)


def _membw_stddev(samples: list) -> Optional[float]:
    """Std-dev of memory bandwidth over the window.  High = active
    texture streaming (gameplay), low = static (menu)."""
    vals = [s.get("membw") for s in samples if s.get("membw") is not None]
    if len(vals) < 5:
        return None
    try:
        return statistics.pstdev(vals)
    except Exception:
        return None


def _rebuild_baseline(exe: str) -> None:
    """Recompute per-game baseline from the rolling window using a
    trimmed median.  Skips if there isn't enough data yet."""
    with _samples_lock:
        snapshot = list(_samples.get(exe, []))
    if len(snapshot) < MIN_SAMPLES_FOR_CLASSIFY:
        return
    fps_p50   = _trimmed_median([s["fps"]   for s in snapshot])
    util_p50  = _trimmed_median([s["util"]  for s in snapshot])
    ftvar_p50 = _trimmed_median([s["ftvar"] for s in snapshot])
    power_p50 = _trimmed_median([s["power"] for s in snapshot])
    cpu_p50   = _trimmed_median([s["cpu"]   for s in snapshot])
    membw_std = _membw_stddev(snapshot)
    with _baselines_lock:
        prev = _baselines.get(exe, {})
        # Don't overwrite a manual baseline silently
        if prev.get("manual"):
            return
        prev_sigs = prev.get("menu_signatures", [])  # preserve learned sigs
        _baselines[exe] = {
            "fps_p50":         fps_p50,
            "util_p50":        util_p50,
            "ftvar_p50":       ftvar_p50,
            "power_p50":       power_p50,
            "cpu_p50":         cpu_p50,
            "membw_std_p50":   membw_std,
            "sample_count":    len(snapshot),
            "updated_at":      time.time(),
            "manual":          False,
            "menu_signatures": prev_sigs,
        }
        _save_baselines()
    log.debug(f"baseline rebuilt for {exe}: fps={fps_p50} util={util_p50} "
              f"ftvar={ftvar_p50} cpu={cpu_p50} membw_std={membw_std}")


def recalibrate_baseline(exe: str) -> dict:
    """User-driven snapshot of the current rolling window as the new
    baseline (marked manual so auto-rebuild stops touching it).  Useful
    for 'I AM in gameplay right now — anchor here'."""
    key = _norm_exe(exe)
    if not key:
        return {"ok": False, "err": "no exe"}
    _ensure(key)
    with _samples_lock:
        snapshot = list(_samples.get(key, []))
    if len(snapshot) < 10:
        return {"ok": False, "err": "not enough samples yet (need ~10 s of play)"}
    fps_p50   = _trimmed_median([s["fps"]   for s in snapshot])
    util_p50  = _trimmed_median([s["util"]  for s in snapshot])
    ftvar_p50 = _trimmed_median([s["ftvar"] for s in snapshot])
    power_p50 = _trimmed_median([s["power"] for s in snapshot])
    cpu_p50   = _trimmed_median([s["cpu"]   for s in snapshot])
    membw_std = _membw_stddev(snapshot)
    with _baselines_lock:
        prev_sigs = _baselines.get(key, {}).get("menu_signatures", [])
        _baselines[key] = {
            "fps_p50":         fps_p50,
            "util_p50":        util_p50,
            "ftvar_p50":       ftvar_p50,
            "power_p50":       power_p50,
            "cpu_p50":         cpu_p50,
            "membw_std_p50":   membw_std,
            "sample_count":    len(snapshot),
            "updated_at":      time.time(),
            "manual":          True,
            "menu_signatures": prev_sigs,
        }
        _save_baselines()
    log.info(f"baseline manually recalibrated for {key}: "
             f"fps={fps_p50} util={util_p50} ftvar={ftvar_p50} cpu={cpu_p50}")
    return {"ok": True, "baseline": _baselines[key]}


def add_menu_signature(exe: str, source: str = "manual") -> dict:
    """Record the current sample's fps/util as a known menu signature.

    Used by:
      • User-clicked "This is the menu — remember it" button (source="manual")
      • Auto-learner when state has been "menu" with high confidence for
        AUTO_LEARN_MIN_TICKS consecutive ticks (source="auto")

    Stored as a tolerance box (fps ±10%, util ±12).  Capped at
    MAX_MENU_SIGNATURES per game so a noisy session can't fill the list.
    """
    key = _norm_exe(exe)
    if not key:
        return {"ok": False, "err": "no exe"}
    _ensure(key)
    with _samples_lock:
        if not _samples[key]:
            return {"ok": False, "err": "no samples yet"}
        s = _samples[key][-1]
    fps  = s.get("fps")
    util = s.get("util")
    if fps is None or util is None:
        return {"ok": False, "err": "missing fps or util in current sample"}
    sig = {
        "fps_lo":     round(fps * (1 - MENU_SIG_FPS_TOL), 1),
        "fps_hi":     round(fps * (1 + MENU_SIG_FPS_TOL), 1),
        "util_lo":    max(0,   round(util - MENU_SIG_UTIL_TOL, 1)),
        "util_hi":    min(100, round(util + MENU_SIG_UTIL_TOL, 1)),
        "fps":        round(fps, 1),
        "util":       round(util, 1),
        "source":     source,
        "learned_at": time.time(),
    }
    with _baselines_lock:
        b = _baselines.setdefault(key, {})
        sigs = b.get("menu_signatures") or []
        # Don't add a duplicate (within the existing tolerance of another)
        for existing in sigs:
            if (existing["fps_lo"] <= fps <= existing["fps_hi"] and
                existing["util_lo"] <= util <= existing["util_hi"]):
                return {"ok": True, "duplicate_of": existing, "added": False}
        # FIFO eviction — keep the most recent N
        sigs.append(sig)
        if len(sigs) > MAX_MENU_SIGNATURES:
            sigs = sigs[-MAX_MENU_SIGNATURES:]
        b["menu_signatures"] = sigs
        _baselines[key] = b
        _save_baselines()
    log.info(f"menu signature added for {key} ({source}): fps≈{fps:.0f} util≈{util:.0f}")
    return {"ok": True, "added": True, "signature": sig,
            "total_signatures": len(sigs)}


def remove_menu_signature(exe: str, index: int) -> dict:
    key = _norm_exe(exe)
    with _baselines_lock:
        b = _baselines.get(key)
        if not b:
            return {"ok": False, "err": "no baseline"}
        sigs = b.get("menu_signatures") or []
        if 0 <= index < len(sigs):
            removed = sigs.pop(index)
            b["menu_signatures"] = sigs
            _save_baselines()
            return {"ok": True, "removed": removed}
    return {"ok": False, "err": "index out of range"}


def _sample_matches_menu_sig(sample: dict, sigs: list) -> Optional[dict]:
    """If the sample's (fps, util) falls inside any known menu
    signature's tolerance box, return that signature."""
    fps = sample.get("fps")
    util = sample.get("util")
    if fps is None or util is None or not sigs:
        return None
    for sig in sigs:
        if (sig["fps_lo"] <= fps <= sig["fps_hi"] and
            sig["util_lo"] <= util <= sig["util_hi"]):
            return sig
    return None


def _current_membw_std(exe: str, window: int = 30) -> Optional[float]:
    """Std-dev of memory bandwidth over the last `window` samples (NOT
    the baseline window).  Used to compare against baseline membw_std."""
    cutoff = time.time() - window
    with _samples_lock:
        recent = [s for s in _samples.get(exe, []) if s["ts"] >= cutoff]
    return _membw_stddev(recent)


def reset(exe: str) -> dict:
    """Wipe this game's samples + baseline + state."""
    key = _norm_exe(exe)
    with _samples_lock:
        _samples.pop(key, None)
        _state_cache.pop(key, None)
        _consecutive_alt.pop(key, None)
    with _baselines_lock:
        _baselines.pop(key, None)
        _save_baselines()
    return {"ok": True, "reset": key}


# ─── Classification ──────────────────────────────────────────────────

def classify(exe: str, sample: Optional[dict] = None) -> dict:
    """Return current classification:
        {state, confidence, reasons, baseline, current}
    """
    key = _norm_exe(exe)
    _ensure(key)
    with _baselines_lock:
        baseline = dict(_baselines.get(key, {})) or {}
    with _samples_lock:
        if sample is None:
            if not _samples[key]:
                return _state_cache[key] | {"baseline": baseline, "current": None}
            sample = _samples[key][-1]
        sample_count = len(_samples[key])

    state_prev = _state_cache[key]["state"]

    # v3.2-beta.5 — we used to require fps_p50 to leave baseline-building.
    # That breaks for any game where the frame-telemetry source can't
    # see frames (Minecraft Java's OpenGL is invisible to PresentMon 1.x,
    # for example).  Now we accept ANY usable baseline — util or fps
    # alone is enough.  Each signal block below independently no-ops
    # when its required field is missing.
    have_any_baseline = bool(baseline and (
        baseline.get("fps_p50")  is not None or
        baseline.get("util_p50") is not None
    ))
    if not have_any_baseline:
        new_state = "baseline-building"
        reasons = [f"gathering samples ({sample_count}/{MIN_SAMPLES_FOR_CLASSIFY})"]
        confidence = 0.0
        return _commit_state(key, new_state, confidence, reasons, sample, baseline)

    # Compute scores
    menu_score     = 0.0
    gameplay_score = 0.0
    reasons = []

    fps   = sample.get("fps")
    util  = sample.get("util")
    ftvar = sample.get("ftvar")

    b_fps  = baseline.get("fps_p50")
    b_util = baseline.get("util_p50")
    b_ftv  = baseline.get("ftvar_p50")

    # ── FPS deviation (the user's idea: bidirectional) ───────────────
    if fps is not None and b_fps and b_fps > 0:
        ratio = fps / b_fps
        if ratio >= FPS_MENU_HIGH_RATIO:
            menu_score += W_FPS_MENU_HIGH
            reasons.append(
                f"FPS {fps:.0f} is {ratio:.1f}× baseline ({b_fps:.0f}) — "
                f"menus often uncap to higher framerates"
            )
        elif ratio <= FPS_MENU_LOW_RATIO:
            menu_score += W_FPS_MENU_LOW
            reasons.append(
                f"FPS {fps:.0f} is {ratio:.1f}× baseline ({b_fps:.0f}) — "
                f"lobbies / menus can run lower than active play"
            )
        elif FPS_GAMEPLAY_LOW_RATIO <= ratio <= FPS_GAMEPLAY_HIGH_RATIO:
            gameplay_score += W_FPS_GAMEPLAY
            reasons.append(f"FPS within ±20% of baseline ({fps:.0f} vs {b_fps:.0f})")

    # ── GPU util drop ────────────────────────────────────────────────
    if util is not None:
        if util < UTIL_ABSOLUTE_MENU:
            menu_score += W_UTIL_ABSOLUTE
            reasons.append(f"GPU util {util:.0f}% (<{UTIL_ABSOLUTE_MENU}% absolute floor)")
        elif b_util and b_util > 0:
            uratio = util / b_util
            if uratio < UTIL_MENU_DROP_RATIO:
                menu_score += W_UTIL_MENU_DROP
                reasons.append(
                    f"GPU util {util:.0f}% is {uratio:.0%} of baseline ({b_util:.0f}%)"
                )
            elif abs(uratio - 1.0) <= UTIL_GAMEPLAY_BAND:
                gameplay_score += W_UTIL_GAMEPLAY
                reasons.append(f"GPU util steady near baseline ({util:.0f}% vs {b_util:.0f}%)")

    # ── Frametime variance collapse (vsync'd menus go ~flat) ─────────
    if ftvar is not None and b_ftv and b_ftv > 0:
        vratio = ftvar / b_ftv
        if vratio < FT_VAR_MENU_QUIET:
            menu_score += W_FTVAR_MENU
            reasons.append(
                f"frametime variance collapsed ({vratio:.0%} of baseline) — "
                f"vsync'd static menu UI"
            )
        elif FT_VAR_GAMEPLAY_BAND[0] <= vratio <= FT_VAR_GAMEPLAY_BAND[1]:
            gameplay_score += W_FTVAR_GAMEPLAY
            reasons.append("frametime variance within typical gameplay range")

    # ── v3.2-beta.4: foreground window check (strongest alt-tab signal) ──
    fg_is_game = sample.get("fg_is_game")
    if fg_is_game is False:
        menu_score += W_FG_NOT_GAME
        reasons.append("game is not the foreground window (alt-tabbed or minimized)")
    elif fg_is_game is True:
        # v3.2-beta.5 — small gameplay bias when the game is focused.
        # Without this, degraded-mode classification (no FPS data
        # available — common for OpenGL games like Minecraft Java that
        # PresentMon 1.x can't see) gets stuck near zero net score
        # and AT never leaves "transitional".  The bias is small so
        # menu signals can still dominate, but it ensures the default
        # state during normal play is "gameplay" not "transitional".
        gameplay_score += 0.20
        reasons.append("game window is foregrounded (focus signal)")

    # ── v3.2-beta.4: per-process CPU drop ────────────────────────────
    cpu = sample.get("cpu")
    b_cpu = baseline.get("cpu_p50")
    if cpu is not None and b_cpu and b_cpu > 5:   # baseline must be meaningful
        cratio = cpu / b_cpu
        if cratio < CPU_MENU_DROP_RATIO:
            menu_score += W_CPU_MENU_DROP
            reasons.append(
                f"game CPU usage {cpu:.0f}% is {cratio:.0%} of baseline "
                f"({b_cpu:.0f}%) — physics/AI/network paused?"
            )
        elif abs(cratio - 1.0) <= CPU_GAMEPLAY_BAND:
            gameplay_score += W_CPU_GAMEPLAY
            reasons.append(f"game CPU steady near baseline ({cpu:.0f}% vs {b_cpu:.0f}%)")

    # ── v3.2-beta.4: VRAM bandwidth variance ─────────────────────────
    # Static menus stop streaming new textures = membw flattens out.
    # We compare the last-30s membw stddev against the baseline-window
    # membw stddev.  Soft signal — some games preload everything.
    b_membw_std = baseline.get("membw_std_p50")
    cur_membw_std = _current_membw_std(exe)
    if cur_membw_std is not None and b_membw_std and b_membw_std > 1.0:
        mratio = cur_membw_std / b_membw_std
        if mratio < MEMBW_MENU_FLAT_RATIO:
            menu_score += W_MEMBW_MENU
            reasons.append(
                f"VRAM-bandwidth variance collapsed ({mratio:.0%} of baseline) — "
                f"no active texture streaming"
            )
        elif MEMBW_GAMEPLAY_BAND[0] <= mratio <= MEMBW_GAMEPLAY_BAND[1]:
            gameplay_score += W_MEMBW_GAMEPLAY
            reasons.append("VRAM-bandwidth variance typical of active rendering")

    # ── v3.2-beta.4: learned menu signatures (per-game knowledge) ────
    sigs = baseline.get("menu_signatures") or []
    matched = _sample_matches_menu_sig(sample, sigs)
    if matched is not None:
        menu_score += W_KNOWN_MENU_SIG
        reasons.append(
            f"matches learned menu signature "
            f"(fps≈{matched.get('fps','?')}, util≈{matched.get('util','?')}, "
            f"learned {matched.get('source','?')})"
        )

    # ── Decide ───────────────────────────────────────────────────────
    # v3.2-beta.5 — use a looser threshold when running in degraded
    # mode (no FPS baseline available, e.g. PresentMon can't see the
    # game's presents).  We have fewer signals so the net score is
    # naturally smaller; without this tweak AT would sit in
    # "transitional" forever and never make decisions.
    have_fps_baseline = baseline.get("fps_p50") is not None
    threshold = 0.30 if have_fps_baseline else 0.15
    net = gameplay_score - menu_score
    if net >= threshold:
        instantaneous = "gameplay"
        confidence = min(1.0, gameplay_score)
    elif net <= -threshold:
        instantaneous = "menu"
        confidence = min(1.0, menu_score)
    else:
        instantaneous = "transitional"
        confidence = max(menu_score, gameplay_score)

    # ── Hysteresis ───────────────────────────────────────────────────
    # We require N consecutive "alternative" classifications before
    # transitioning out of the current state.  This protects against
    # one-tick flaps (loading screen blip).
    if instantaneous == state_prev or state_prev in ("baseline-building", "transitional"):
        _consecutive_alt[key] = 0
        new_state = instantaneous
    else:
        _consecutive_alt[key] = _consecutive_alt.get(key, 0) + 1
        if _consecutive_alt[key] >= HYSTERESIS_TICKS:
            new_state = instantaneous
            _consecutive_alt[key] = 0
        else:
            # Stay in previous state but flag we're "drifting"
            new_state = state_prev
            reasons.insert(0,
                f"hysteresis: would switch to '{instantaneous}' "
                f"after {HYSTERESIS_TICKS - _consecutive_alt[key]} more consecutive ticks"
            )

    return _commit_state(key, new_state, confidence, reasons, sample, baseline)


def _commit_state(exe: str, new_state: str, confidence: float,
                  reasons: list, sample: dict, baseline: dict) -> dict:
    """Update the per-game cache and return the structured result.

    v3.2-beta.4 — when the state has been "menu" with high confidence
    for AUTO_LEARN_MIN_TICKS consecutive ticks AND the current sample
    doesn't match an existing signature, auto-learn the signature.
    This means the second time the player enters this menu (or the
    same menu in their next session), classification fires instantly
    on a single sample instead of needing the full hysteresis window.
    """
    prev = _state_cache[exe]
    if new_state == prev["state"]:
        ticks = prev["ticks_in_state"] + 1
    else:
        log.info(f"gameplay_state[{exe}]: {prev['state']} → {new_state}  "
                 f"(conf {confidence:.2f}) — {reasons[0] if reasons else '?'}")
        ticks = 1
    _state_cache[exe] = {
        "state":          new_state,
        "confidence":     round(confidence, 2),
        "reasons":        reasons,
        "ticks_in_state": ticks,
        "updated_at":     time.time(),
    }

    # Auto-learn menu signatures.  Only fires while we're solidly in
    # menu state — single-sample heuristic matches are NOT used as
    # the trigger (that would be circular).
    if (new_state == "menu" and
        confidence >= 0.6 and
        ticks == AUTO_LEARN_MIN_TICKS and
        sample.get("fps") is not None and
        sample.get("util") is not None):
        try:
            res = add_menu_signature(exe, source="auto")
            if res.get("added"):
                log.info(f"auto-learned menu signature for {exe}: "
                         f"fps≈{sample['fps']:.0f} util≈{sample['util']:.0f}")
        except Exception as e:
            log.debug(f"auto-learn failed: {e}")

    return {
        "state":          new_state,
        "confidence":     round(confidence, 2),
        "reasons":        reasons,
        "ticks_in_state": ticks,
        "baseline":       baseline,
        "current":        sample,
    }


# ─── Read API ────────────────────────────────────────────────────────

def get_state(exe: str) -> dict:
    """Latest cached classification + baseline + last sample.  Cheap,
    safe to call from the UI poll."""
    key = _norm_exe(exe)
    if not key:
        return {"state": "unknown"}
    _ensure(key)
    with _samples_lock:
        last_sample = _samples[key][-1] if _samples[key] else None
        sample_count = len(_samples[key])
    with _baselines_lock:
        baseline = dict(_baselines.get(key, {}))
    cache = dict(_state_cache.get(key, {}))
    cache["baseline"]     = baseline
    cache["current"]      = last_sample
    cache["sample_count"] = sample_count
    return cache


def get_baseline(exe: str) -> dict:
    """Per-game baseline (or {} if not yet established)."""
    key = _norm_exe(exe)
    with _baselines_lock:
        return dict(_baselines.get(key, {}))


def get_all_baselines() -> dict:
    """Bulk read for the diagnostics UI."""
    with _baselines_lock:
        return dict(_baselines)
