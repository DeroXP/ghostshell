"""Background GPU performance sampler — 5 Hz ring buffer + stability score.

This is the data layer that feeds:
  • The Hardware Monitor tab's "Live Performance" sparklines
  • Adaptive Tuning v1 (next phase) — uses the stability score to
    decide when an OC offset is healthy enough to nudge upward
  • Crash diagnostics — the last few seconds of samples are dumped to
    the crash history when a TDR fires, so you can see the spike
    that preceded the crash

Data source is `gpu_overclock.read_live_state()` (which calls
`nvidia-smi` once).  We sample at 5 Hz (every 200 ms) which is plenty
fast to catch stutter / clock-drop events without taxing the system.

Ring buffer holds the last 300 samples = 60 s of history.

Stability score (0–100):
  Higher = more stable.  Computed from three sub-scores:
    • clock consistency  — low std-dev of GPU clock means no throttling
    • temperature range  — narrow temp range under load means thermals
                           are well controlled
    • util sustained     — when util is high, it should *stay* high
                           (a stable workload doesn't bounce 99→40→99)

  This is the signal Adaptive Tuning will use to know "the OC is
  working, try nudging it up" vs. "stop, this offset is making the
  GPU misbehave even if it hasn't crashed yet".
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from collections import deque
from typing import Any, Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("perf_monitor")

# Sampling cadence
SAMPLE_INTERVAL_S = 0.20    # 5 Hz
RING_BUFFER_SIZE  = 300     # = 60 s at 5 Hz

# Idle = no sustained GPU work; we collect samples but don't compute
# stability over them since variance is meaningless when util is near 0.
# 15% catches light games like Roblox / League / older titles while still
# rejecting truly idle desktop workloads.
IDLE_UTIL_THRESHOLD = 15

_state: dict[str, Any] = {
    "running":     False,
    "thread":      None,
    "stop_event":  None,
    "samples":     deque(maxlen=RING_BUFFER_SIZE),
    "last_sample": None,
    "started_at":  None,
    "consumers":   set(),  # callers can ref-count to keep it running
}
_lock = threading.Lock()


# ─── Sampling loop ────────────────────────────────────────────────────────
def _sampler_loop() -> None:
    log.info(f"perf_monitor sampler started ({SAMPLE_INTERVAL_S * 1000:.0f}ms cadence)")
    from core import gpu_overclock

    stop_event = _state["stop_event"]
    while stop_event is not None and not stop_event.is_set():
        ts = time.time()
        try:
            s = gpu_overclock.read_live_state()
        except Exception as e:
            log.debug(f"sampler tick failed: {e}")
            s = {"ok": False}

        sample = {
            "ts":      ts,
            "ok":      bool(s.get("ok")),
            "core":    int(s.get("core_mhz")     or 0),
            "mem":     int(s.get("mem_mhz")      or 0),
            "temp":    int(s.get("temp_c")       or 0),
            "power":   float(s.get("power_w")    or 0.0),
            "util":    int(s.get("gpu_util_pct") or 0),
            "memutil": int(s.get("mem_util_pct") or 0),
            "fan":     int(s.get("fan_pct")      or 0),
        }
        with _lock:
            _state["samples"].append(sample)
            _state["last_sample"] = sample

        # Sleep with early wake if stop_event fires
        stop_event.wait(SAMPLE_INTERVAL_S)

    with _lock:
        _state["running"] = False
    log.info("perf_monitor sampler stopped")


# ─── Lifecycle (with refcount) ────────────────────────────────────────────
def start(consumer: str = "manual") -> dict:
    """Start the sampler (idempotent).  Each `consumer` adds a refcount;
    the sampler keeps running as long as ≥1 consumer is registered.

    Typical consumers: 'hwmon-page', 'game-mode', 'adaptive-tuning'.
    Use the same string in stop() to release the refcount."""
    with _lock:
        _state["consumers"].add(consumer)
        if _state["running"]:
            return {"ok": True, "msg": "already running",
                    "consumers": list(_state["consumers"])}
        _state["stop_event"]  = threading.Event()
        _state["running"]     = True
        _state["started_at"]  = time.time()
        _state["samples"]     = deque(maxlen=RING_BUFFER_SIZE)
        t = threading.Thread(target=_sampler_loop, daemon=True,
                             name="perf-sampler")
        _state["thread"] = t
        t.start()
        log.info(f"perf_monitor consumers: {list(_state['consumers'])}")
    return {"ok": True, "consumers": list(_state["consumers"])}


def stop(consumer: str = "manual", force: bool = False) -> dict:
    """Release a consumer's refcount.  When the last consumer leaves
    (or `force=True`) the sampler thread stops."""
    with _lock:
        _state["consumers"].discard(consumer)
        remaining = list(_state["consumers"])
        if remaining and not force:
            log.info(f"perf_monitor consumer '{consumer}' released; "
                     f"still active for: {remaining}")
            return {"ok": True, "still_running_for": remaining}
        if not _state["running"]:
            return {"ok": True, "msg": "already stopped"}
        ev = _state["stop_event"]
        _state["consumers"].clear()
    if ev is not None:
        ev.set()
    return {"ok": True, "stopped": True}


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


# ─── Data accessors ──────────────────────────────────────────────────────
def get_samples(seconds: int = 60) -> list[dict]:
    """Return samples from the last `seconds` seconds (bounded by the
    ring buffer's max of 60 s)."""
    cutoff = time.time() - seconds
    with _lock:
        return [s for s in list(_state["samples"]) if s["ts"] >= cutoff]


def get_status() -> dict:
    with _lock:
        samples = list(_state["samples"])
        running = _state["running"]
        last = _state["last_sample"]
        consumers = list(_state["consumers"])
        started = _state["started_at"]
    return {
        "running":     running,
        "consumers":   consumers,
        "started_at":  started,
        "sample_count": len(samples),
        "max_samples":  RING_BUFFER_SIZE,
        "interval_ms":  int(SAMPLE_INTERVAL_S * 1000),
        "last":         last,
    }


# ─── Stability score ─────────────────────────────────────────────────────
def _stddev_safe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    try:
        return statistics.pstdev(values)
    except statistics.StatisticsError:
        return 0.0


def compute_stability_score(seconds: int = 30) -> dict:
    """Score 0-100 over the last `seconds` of samples.  Higher = stabler.

    Sub-scores:
      • clock_score   (0-40): low GPU clock std-dev under load
      • temp_score    (0-30): temperature stayed in a narrow range
      • util_score    (0-30): when GPU was busy, it stayed busy
                              (stuttering OC produces flapping util)

    Returns a verdict string + the sub-score breakdown for the UI:
      verdict ∈ {idle, healthy, warn, unstable, no-data}
    """
    samples = get_samples(seconds)
    # Latest util reading — exposed even when score can't be computed,
    # so the UI can tell the user "GPU at 12%, waiting for ≥15%"
    last_util = samples[-1]["util"] if samples else 0
    last_temp = samples[-1]["temp"] if samples else 0
    avg_util  = (sum(s["util"] for s in samples) / len(samples)) if samples else 0
    max_util  = max((s["util"] for s in samples), default=0)

    if len(samples) < 10:
        return {
            "score":      None,
            "verdict":    "no-data",
            "reason":     f"need ≥10 samples, have {len(samples)}",
            "subs":       {},
            "samples_used": len(samples),
            "last_util":  last_util,
            "last_temp":  last_temp,
            "avg_util":   round(avg_util, 1),
            "max_util":   max_util,
            "idle_threshold": IDLE_UTIL_THRESHOLD,
        }

    # Filter for "active" samples — when GPU is doing real work
    active = [s for s in samples if s["util"] >= IDLE_UTIL_THRESHOLD and s["ok"]]
    active_pct = len(active) / len(samples) if samples else 0

    if not active:
        return {
            "score":      None,
            "verdict":    "idle",
            "reason":     f"GPU idle (max util in window: {max_util}%, "
                          f"need ≥{IDLE_UTIL_THRESHOLD}%)",
            "subs":       {},
            "samples_used": len(samples),
            "last_util":  last_util,
            "last_temp":  last_temp,
            "avg_util":   round(avg_util, 1),
            "max_util":   max_util,
            "idle_threshold": IDLE_UTIL_THRESHOLD,
        }

    clocks = [s["core"]  for s in active]
    temps  = [s["temp"]  for s in active if s["temp"] > 0]
    utils  = [s["util"]  for s in active]

    # ── Clock score: low std-dev = stable boost ──────────────────────
    clock_std = _stddev_safe([float(c) for c in clocks])
    # 0 MHz std = 40 pts; 200 MHz std = 0 pts.  Linear in between.
    clock_score = max(0.0, 40.0 - (clock_std / 200.0 * 40.0))

    # ── Temp score: narrow range = good cooling ──────────────────────
    if temps:
        temp_range = max(temps) - min(temps)
        # 0°C range = 30; 25°C range = 0.  Linear.
        temp_score = max(0.0, 30.0 - (temp_range / 25.0 * 30.0))
    else:
        temp_score = 30.0

    # ── Util consistency: when busy, stayed busy ─────────────────────
    util_std = _stddev_safe([float(u) for u in utils])
    # 0% std = 30; 30% std = 0
    util_score = max(0.0, 30.0 - (util_std / 30.0 * 30.0))

    score = round(clock_score + temp_score + util_score, 1)

    if score >= 85:
        verdict = "healthy"
    elif score >= 65:
        verdict = "warn"
    else:
        verdict = "unstable"

    return {
        "score":   score,
        "verdict": verdict,
        "subs": {
            "clock_score":  round(clock_score, 1),
            "temp_score":   round(temp_score, 1),
            "util_score":   round(util_score, 1),
            "clock_std":    round(clock_std, 1),
            "temp_range":   (max(temps) - min(temps)) if temps else 0,
            "util_std":     round(util_std, 1),
            "active_pct":   round(active_pct * 100, 1),
        },
        "samples_used":   len(active),
        "window_s":       seconds,
        "last_util":      last_util,
        "last_temp":      last_temp,
        "avg_util":       round(avg_util, 1),
        "max_util":       max_util,
        "idle_threshold": IDLE_UTIL_THRESHOLD,
    }


# ─── Convenience: tail snapshot for UI charting ──────────────────────────
def get_chart_data(seconds: int = 60) -> dict:
    """Return the last N seconds in chart-ready format.  Each axis is
    its own array so the canvas renderer can pick which to plot."""
    samples = get_samples(seconds)
    return {
        "ts":     [s["ts"]    for s in samples],
        "core":   [s["core"]  for s in samples],
        "mem":    [s["mem"]   for s in samples],
        "temp":   [s["temp"]  for s in samples],
        "power":  [s["power"] for s in samples],
        "util":   [s["util"]  for s in samples],
        "fan":    [s["fan"]   for s in samples],
        "count":  len(samples),
        "window_s": seconds,
    }
