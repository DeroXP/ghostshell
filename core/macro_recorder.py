"""Macro recorder — capture a live sequence of inputs (any source) and
turn it into a v3.4-format macro step list.

v3.6 — source filters + countdown + analog recording:
  - User picks WHAT to record per session: any combination of keyboard /
    mouse-buttons / mouse-movement / controller-buttons / controller-
    sticks / controller-triggers.  Defaults exclude the noisy ones
    (mouse-move, stick samples) so the common "record some keypresses"
    flow stays uncluttered.
  - 2-second countdown (configurable) before recording flips active so
    the user has time to alt-tab / focus the target window.
  - Analog inputs (sticks, triggers, mouse-move) are sampled at a
    configurable rate (default 30 Hz) and quantized into vstick / vtrigger
    / mouse_move macro steps.

Flow:
  1. UI calls start_recording(filters, start_delay_sec, ...)
  2. Recorder enters PHASE 'countdown' for `start_delay_sec` seconds
     (a background thread runs the timer; status endpoint reports
     remaining time).
  3. Recorder flips to PHASE 'recording'.
  4. Mapper feeds digital input events through record_event(...) and
     analog samples through record_stick_sample / record_trigger_sample
     / record_mouse_move.
  5. UI calls stop_recording() — returns generated step list.

Step generation:
  • Digital pairs: same as before — press/release pairs sorted by
    release time, hold_ms = release - press, delay_after_ms = next
    press - this release.
  • Analog: each sample (taken at sample_hz cadence) becomes a single
    `tap`-mode vstick / vtrigger step with down_ms = sample period, so
    consecutive samples blend smoothly during playback.
  • Mouse-move: similarly chunked into mouse_move steps with cumulative
    dx/dy per chunk.
"""

from __future__ import annotations
import time
import threading
from typing import Optional

from core.utils import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────────────
# Recording state — module-global, single session at a time
# ────────────────────────────────────────────────────────────────────

# Default filter set — the legacy "record everything digital, nothing
# analog" behavior so existing UI flow keeps working when callers pass
# no filters.  Analog-off-by-default also keeps the step count tidy.
DEFAULT_FILTERS = {
    "keyboard":            True,
    "mouse_buttons":       True,
    "mouse_move":          False,
    "controller_buttons":  True,
    "controller_sticks":   False,
    "controller_triggers": True,    # LT/RT digital threshold events
}

_state = {
    "phase":         "idle",     # "idle" | "countdown" | "recording" | "stopped"
    "started_at":    0.0,         # perf_counter when phase flipped to recording
    "countdown_ends_at": 0.0,    # wall-clock; 0 if no countdown active
    "events":        [],          # digital press/release events
    "stick_samples": [],          # (ts, side, x, y)
    "trigger_samples": [],        # (ts, side, value_0_255)
    "mouse_moves":   [],          # (ts, dx, dy)
    "filters":       dict(DEFAULT_FILTERS),
    "stick_sample_hz":      30,
    "trigger_sample_hz":    30,
    "mouse_move_sample_hz": 30,
    # Last-sampled timestamps per channel for rate limiting
    "_last_stick_ts":   {"left": 0.0, "right": 0.0},
    "_last_trigger_ts": {"lt": 0.0, "rt": 0.0},
    "_last_mouse_move_ts": 0.0,
    # Last-recorded values for change detection (skip dupes)
    "_last_stick_val":   {"left": (0, 0), "right": (0, 0)},
    "_last_trigger_val": {"lt": 0, "rt": 0},
}
_lock = threading.Lock()


def is_recording() -> bool:
    """Convenience for callers that just want to know if events should
    be forwarded.  Returns True only during the live recording phase
    (NOT during the countdown)."""
    return _state["phase"] == "recording"


def get_status() -> dict:
    with _lock:
        phase = _state["phase"]
        n_events  = len(_state["events"])
        n_sticks  = len(_state["stick_samples"])
        n_trigs   = len(_state["trigger_samples"])
        n_mmoves  = len(_state["mouse_moves"])
        elapsed = (time.perf_counter() - _state["started_at"]) if phase == "recording" else 0.0
        countdown_remaining = 0.0
        if phase == "countdown":
            countdown_remaining = max(0.0, _state["countdown_ends_at"] - time.time())
    return {
        # Legacy `active` field kept so older UI that only checks "active"
        # still treats the live phase as on.
        "active":      phase == "recording",
        "phase":       phase,
        "elapsed_sec": round(elapsed, 2),
        "countdown_remaining_sec": round(countdown_remaining, 2),
        "events":      n_events,
        "stick_samples":   n_sticks,
        "trigger_samples": n_trigs,
        "mouse_moves":     n_mmoves,
        "filters":     dict(_state["filters"]),
    }


def start_recording(skip_move: bool = True,
                    start_delay_sec: float = 2.0,
                    filters: Optional[dict] = None,
                    stick_sample_hz: int = 30,
                    trigger_sample_hz: int = 30,
                    mouse_move_sample_hz: int = 30) -> dict:
    """Begin a recording session.  Discards any prior session.

    `start_delay_sec` is a 2-second countdown by default — the recorder
    enters PHASE 'countdown' immediately and flips to 'recording' when
    the countdown elapses.  This gives the user a window to alt-tab,
    focus a target window, or get their hands ready.

    `filters` is a partial dict overlaid on DEFAULT_FILTERS.  Pass any
    subset of: keyboard, mouse_buttons, mouse_move, controller_buttons,
    controller_sticks, controller_triggers.  Unspecified keys keep their
    defaults.

    `skip_move` is the legacy alias for `filters['mouse_move']=False`;
    if you pass it, it overrides whatever filters might have said about
    mouse movement.  Keeps older UI calls working unchanged.
    """
    eff_filters = dict(DEFAULT_FILTERS)
    if isinstance(filters, dict):
        for k, v in filters.items():
            if k in eff_filters:
                eff_filters[k] = bool(v)
    if skip_move:
        # Explicit legacy override — wins over filters dict
        eff_filters["mouse_move"] = False

    delay = max(0.0, min(10.0, float(start_delay_sec or 0.0)))

    with _lock:
        _state["phase"]            = "countdown" if delay > 0 else "recording"
        _state["started_at"]       = time.perf_counter() if delay <= 0 else 0.0
        _state["countdown_ends_at"]= time.time() + delay if delay > 0 else 0.0
        _state["events"]           = []
        _state["stick_samples"]    = []
        _state["trigger_samples"]  = []
        _state["mouse_moves"]      = []
        _state["filters"]          = eff_filters
        _state["stick_sample_hz"]      = max(5, min(120, int(stick_sample_hz   or 30)))
        _state["trigger_sample_hz"]    = max(5, min(120, int(trigger_sample_hz or 30)))
        _state["mouse_move_sample_hz"] = max(5, min(120, int(mouse_move_sample_hz or 30)))
        _state["_last_stick_ts"]    = {"left": 0.0, "right": 0.0}
        _state["_last_trigger_ts"]  = {"lt": 0.0, "rt": 0.0}
        _state["_last_mouse_move_ts"] = 0.0
        _state["_last_stick_val"]   = {"left": (0, 0), "right": (0, 0)}
        _state["_last_trigger_val"] = {"lt": 0, "rt": 0}

    if delay > 0:
        # Wake-up timer — flips phase to "recording" when countdown ends
        threading.Thread(target=_countdown_to_recording,
                         args=(delay,),
                         daemon=True,
                         name="macro-rec-countdown").start()
    log.info(f"macro recording session armed — countdown {delay}s, "
             f"filters {eff_filters}")
    return {"ok": True, "phase": _state["phase"], "countdown_sec": delay,
            "filters": eff_filters}


def _countdown_to_recording(delay: float):
    time.sleep(delay)
    with _lock:
        if _state["phase"] != "countdown":
            return    # cancelled or already changed
        _state["phase"]      = "recording"
        _state["started_at"] = time.perf_counter()
        _state["countdown_ends_at"] = 0.0
    log.info("macro recording phase → recording")


def cancel_recording() -> dict:
    with _lock:
        _state["phase"]   = "idle"
        _state["events"]  = []
        _state["stick_samples"]   = []
        _state["trigger_samples"] = []
        _state["mouse_moves"]     = []
    log.info("macro recording cancelled")
    return {"ok": True}


# ────────────────────────────────────────────────────────────────────
# Per-event hooks (called from the mapper polling loop / input_hooks)
# ────────────────────────────────────────────────────────────────────

def record_event(source: str, trigger: str, pressed: bool,
                 vk: Optional[int] = None) -> None:
    """Hook called from the mapper polling loop on every digital input
    transition.  Cheap when not recording — phase check is the first
    thing we do.

    Filtering rules:
      keyboard events            → record_keyboard
      mouse buttons + wheel      → record_mouse_buttons
      mouse movement (M_MOVE)    → record_mouse_move (handled separately
                                   via record_mouse_move_delta — this
                                   path only fires for transitions, not
                                   per-pixel deltas)
      controller buttons         → record_controller_buttons
      controller LT/RT digital   → record_controller_triggers
    """
    if _state["phase"] != "recording":
        return
    f = _state["filters"]
    # Routing
    if source == "keyboard":
        if not f.get("keyboard"): return
    elif source == "mouse":
        if trigger == "M_MOVE":
            # M_MOVE digital events from the hook layer — analog deltas
            # come through record_mouse_move_delta separately.  Skip.
            return
        if not f.get("mouse_buttons"): return
    elif source == "controller":
        if trigger in ("LT", "RT"):
            if not f.get("controller_triggers"): return
        else:
            if not f.get("controller_buttons"): return
    else:
        return    # unknown source

    with _lock:
        # Re-check phase now that we hold the lock — it's checked
        # unlocked above purely so the common not-recording case (this
        # runs on every input event, up to 8 kHz) doesn't pay lock
        # overhead, but that means phase could have flipped to
        # "stopped"/"idle" between the check above and here. Without
        # this re-check, a stray event could land in a list that
        # stop_recording() already snapshotted, or worse, in the NEXT
        # session's freshly-reset list if a restart raced in between.
        if _state["phase"] != "recording":
            return
        _state["events"].append({
            "ts":      time.perf_counter() - _state["started_at"],
            "source":  source,
            "trigger": trigger,
            "pressed": bool(pressed),
            "vk":      vk,
        })


def record_stick_sample(side: str, x_raw: int, y_raw: int) -> None:
    """Hook called from the mapper polling loop on every tick with the
    current stick state for a given side.  Internally rate-limits to
    stick_sample_hz and skips duplicates, so callers can call freely
    even at 8 kHz tick rates."""
    if _state["phase"] != "recording":
        return
    if not _state["filters"].get("controller_sticks"):
        return
    if side not in ("left", "right"):
        return
    now = time.perf_counter()
    period = 1.0 / float(_state["stick_sample_hz"])
    last_ts = _state["_last_stick_ts"][side]
    if now - last_ts < period:
        return
    # Skip if value is essentially the same as the last sample (ignore
    # tiny jitter under ~3% of full range).  Keeps the step count down
    # when the user isn't moving the stick.
    last_x, last_y = _state["_last_stick_val"][side]
    if abs(x_raw - last_x) < 1000 and abs(y_raw - last_y) < 1000:
        # Still bump the sample timestamp so we don't repeatedly check
        _state["_last_stick_ts"][side] = now
        return
    _state["_last_stick_ts"][side]  = now
    _state["_last_stick_val"][side] = (int(x_raw), int(y_raw))
    with _lock:
        # Re-check phase under the lock — see record_event()'s comment.
        if _state["phase"] != "recording":
            return
        _state["stick_samples"].append({
            "ts":   now - _state["started_at"],
            "side": side,
            "x":    int(x_raw),
            "y":    int(y_raw),
        })


def record_trigger_sample(side: str, value_0_255: int) -> None:
    """Same idea, for analog LT/RT triggers."""
    if _state["phase"] != "recording":
        return
    if not _state["filters"].get("controller_triggers"):
        return
    if side not in ("lt", "rt"):
        return
    now = time.perf_counter()
    period = 1.0 / float(_state["trigger_sample_hz"])
    last_ts = _state["_last_trigger_ts"][side]
    if now - last_ts < period:
        return
    last_v = _state["_last_trigger_val"][side]
    v = max(0, min(255, int(value_0_255)))
    if abs(v - last_v) < 5:    # ~2% of full range
        _state["_last_trigger_ts"][side] = now
        return
    _state["_last_trigger_ts"][side]  = now
    _state["_last_trigger_val"][side] = v
    with _lock:
        # Re-check phase under the lock — see record_event()'s comment.
        if _state["phase"] != "recording":
            return
        _state["trigger_samples"].append({
            "ts":    now - _state["started_at"],
            "side":  side,
            "value": v,
        })


def record_mouse_move_delta(dx: int, dy: int) -> None:
    """Hook called from the input_hooks mouse path on every move event.
    Accumulates deltas inside the current sample window (rate-limited
    to mouse_move_sample_hz)."""
    if _state["phase"] != "recording":
        return
    if not _state["filters"].get("mouse_move"):
        return
    now = time.perf_counter()
    period = 1.0 / float(_state["mouse_move_sample_hz"])
    last_ts = _state["_last_mouse_move_ts"]
    with _lock:
        # Re-check phase under the lock — see record_event()'s comment.
        if _state["phase"] != "recording":
            return
        if now - last_ts < period:
            # Aggregate into the most recent sample if it exists
            if _state["mouse_moves"]:
                last = _state["mouse_moves"][-1]
                last["dx"] += int(dx)
                last["dy"] += int(dy)
                return
        _state["_last_mouse_move_ts"] = now
        _state["mouse_moves"].append({
            "ts":  now - _state["started_at"],
            "dx":  int(dx),
            "dy":  int(dy),
        })


# ────────────────────────────────────────────────────────────────────
# Step generation
# ────────────────────────────────────────────────────────────────────

def stop_recording() -> dict:
    """End the session and return the generated step list."""
    with _lock:
        if _state["phase"] not in ("recording", "countdown"):
            return {"ok": False, "err": "not recording"}
        _state["phase"] = "stopped"
        events     = list(_state["events"])
        sticks     = list(_state["stick_samples"])
        triggers   = list(_state["trigger_samples"])
        mmoves     = list(_state["mouse_moves"])
        stick_hz   = _state["stick_sample_hz"]
        trig_hz    = _state["trigger_sample_hz"]
        mmove_hz   = _state["mouse_move_sample_hz"]

    digital_steps  = _generate_steps(events)
    stick_steps    = _generate_stick_steps(sticks, stick_hz)
    trigger_steps  = _generate_trigger_steps(triggers, trig_hz)
    mmove_steps    = _generate_mouse_move_steps(mmoves, mmove_hz)

    # Merge in chronological order so playback feels natural.  Each
    # category is already chronological; just interleave on the
    # per-step timestamp we recorded alongside.
    all_steps_with_ts = []
    # Digital steps don't carry a ts in the saved schema — we track
    # via release_ts during pairing.  Re-tag here using the original
    # release timestamps of the underlying pairs so the merge is
    # ordered correctly.
    for s, t in _digital_steps_with_ts(events):
        all_steps_with_ts.append((s, t))
    for s in stick_steps:
        all_steps_with_ts.append((s, s.pop("_ts", 0.0)))
    for s in trigger_steps:
        all_steps_with_ts.append((s, s.pop("_ts", 0.0)))
    for s in mmove_steps:
        all_steps_with_ts.append((s, s.pop("_ts", 0.0)))
    all_steps_with_ts.sort(key=lambda x: x[1])
    final_steps = [s for s, _ in all_steps_with_ts]

    log.info(
        f"macro recording stopped: "
        f"{len(events)} digital + {len(sticks)} stick + "
        f"{len(triggers)} trigger + {len(mmoves)} mouse-move samples "
        f"→ {len(final_steps)} steps"
    )
    return {
        "ok":    True,
        "steps": final_steps,
        "raw_event_count": len(events),
        "raw_stick_samples":   len(sticks),
        "raw_trigger_samples": len(triggers),
        "raw_mouse_moves":     len(mmoves),
    }


def _vk_from_trigger(trigger: str) -> int:
    """Re-derive a VK code for a keyboard trigger by name when the
    original event didn't include one."""
    try:
        from core import input_hooks
        return input_hooks.VK_BY_NAME.get((trigger or "").lower(), 0)
    except Exception:
        return 0


def _generate_steps(events: list[dict]) -> list[dict]:
    """Pair down/up events from the recording into macro steps."""
    return [s for s, _ in _digital_steps_with_ts(events)]


def _digital_steps_with_ts(events: list[dict]) -> list:
    """Same as `_generate_steps` but each step is paired with its
    release-time timestamp, so the merge step in stop_recording can
    interleave with stick/trigger/mouse-move steps in chronological
    order."""
    if not events:
        return []
    events = sorted(events, key=lambda e: e["ts"])

    open_presses: dict[tuple[str, str], dict] = {}
    pairs: list[dict] = []
    for ev in events:
        key = (ev["source"], ev["trigger"])
        if ev["pressed"]:
            if ev["trigger"] in ("M_WHEEL_UP", "M_WHEEL_DOWN"):
                pairs.append({
                    "press_ts":   ev["ts"],
                    "release_ts": ev["ts"] + 0.001,
                    "source":     ev["source"],
                    "trigger":    ev["trigger"],
                    "vk":         ev.get("vk"),
                })
                continue
            open_presses[key] = ev
        else:
            press = open_presses.pop(key, None)
            if press is None:
                continue
            pairs.append({
                "press_ts":   press["ts"],
                "release_ts": ev["ts"],
                "source":     press["source"],
                "trigger":    press["trigger"],
                "vk":         press.get("vk") or ev.get("vk"),
            })
    if open_presses:
        last_ts = events[-1]["ts"] + 0.05
        for press in open_presses.values():
            pairs.append({
                "press_ts":   press["ts"],
                "release_ts": last_ts,
                "source":     press["source"],
                "trigger":    press["trigger"],
                "vk":         press.get("vk"),
            })
    pairs.sort(key=lambda p: p["release_ts"])

    out: list = []
    last_release_ts = 0.0
    for p in pairs:
        press_ts   = p["press_ts"]
        release_ts = p["release_ts"]
        hold_ms    = max(1, int((release_ts - press_ts) * 1000))
        gap_ms     = max(0, int((press_ts - last_release_ts) * 1000))
        last_release_ts = release_ts
        step = _build_step(p, hold_ms)
        if step is None:
            continue
        # Bake the gap into the previous step's delay_after_ms
        if out and gap_ms > 0:
            prev_step, _ = out[-1]
            inner_key = next((k for k in ("key", "mouse", "vbutton")
                              if k in prev_step), None)
            if inner_key:
                prev_step[inner_key]["delay_after_ms"] = gap_ms
        out.append((step, release_ts))
    return out


def _build_step(pair: dict, hold_ms: int) -> Optional[dict]:
    """Translate a (source, trigger, vk) pair into a v3.4 macro step."""
    src  = pair["source"]
    trig = pair["trigger"]
    vk   = pair.get("vk") or _vk_from_trigger(trig)
    if src == "keyboard":
        if not vk:
            return None
        return {"key": {"vk": vk, "down_ms": hold_ms, "delay_after_ms": 0}}
    if src == "mouse":
        if trig in ("M_LEFT", "M_RIGHT", "M_MIDDLE", "M_X1", "M_X2",
                    "M_WHEEL_UP", "M_WHEEL_DOWN"):
            return {"mouse": {"button": trig, "down_ms": hold_ms,
                               "delay_after_ms": 0}}
        return None
    if src == "controller":
        return {"vbutton": {"vbutton": trig, "down_ms": hold_ms,
                             "delay_after_ms": 0}}
    return None


def _generate_stick_steps(samples: list, sample_hz: int) -> list[dict]:
    """Convert (ts, side, x, y) stick samples into vstick macro steps.

    Each sample becomes a tap-mode vstick step held for
    1000/sample_hz ms, so consecutive samples stitch into a smooth
    playback at the recorded rate.  Y-axis FLIPPED at the boundary —
    XInput reports +y=UP but our preset / step format uses screen
    convention +y=DOWN, matching the recoil-preset schema."""
    if not samples:
        return []
    period_ms = max(10, int(round(1000.0 / max(5, sample_hz))))
    out = []
    for s in samples:
        x_norm = max(-1.0, min(1.0, s["x"] / 32767.0))
        y_norm = max(-1.0, min(1.0, s["y"] / 32767.0))
        step = {
            "vstick": {
                "stick":          s["side"],
                "x":              round(x_norm, 4),
                # Flip XInput → screen convention
                "y":              round(-y_norm, 4),
                "down_ms":        period_ms,
                "delay_after_ms": 0,
                "ramp_ms":        0,
                "mode":           "tap",
            },
            "_ts": s["ts"],
        }
        out.append(step)
    return out


def _generate_trigger_steps(samples: list, sample_hz: int) -> list[dict]:
    """Convert (ts, side, value) trigger samples into vtrigger steps."""
    if not samples:
        return []
    period_ms = max(10, int(round(1000.0 / max(5, sample_hz))))
    out = []
    for s in samples:
        out.append({
            "vtrigger": {
                "trigger":        s["side"],
                "value":          round(s["value"] / 255.0, 4),
                "down_ms":        period_ms,
                "delay_after_ms": 0,
                "mode":           "tap",
            },
            "_ts": s["ts"],
        })
    return out


def _generate_mouse_move_steps(samples: list, sample_hz: int) -> list[dict]:
    """Convert (ts, dx, dy) mouse-move samples into mouse_move steps.
    The macro runner has a `mouse_move` step kind that emits a relative
    SendInput MOUSEEVENTF_MOVE with the configured (dx, dy)."""
    if not samples:
        return []
    period_ms = max(10, int(round(1000.0 / max(5, sample_hz))))
    out = []
    for s in samples:
        if s["dx"] == 0 and s["dy"] == 0:
            continue
        out.append({
            "mouse_move": {
                "dx":             int(s["dx"]),
                "dy":             int(s["dy"]),
                "delay_after_ms": period_ms,
            },
            "_ts": s["ts"],
        })
    return out
