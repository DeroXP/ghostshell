"""Recoil pattern capture (v3.6).

Two methods, each producing a list of (dx, dy) deltas representing the
camera drift induced by recoil during a single magazine.  Both feed
into the same downstream pipeline:

   captured deltas  →  scaled to vstick magnitudes  →  vstick macro steps
                    →  saved as a user recoil preset
                    →  shows up in the recoil-preset picker beside the
                       bundled CS2 / Valorant / Apex patterns

────────────────────────────────────────────────────────────────────
Method A — screen tracking (continuous)
────────────────────────────────────────────────────────────────────
User aims at a flat wall in their game (windowed mode), starts the
capture session in GhostShell, then fires a full magazine WITHOUT
moving their mouse.  We grab frames at ~120 Hz from the foreground
window and compute frame-to-frame phase correlation on a small ROI
around the screen centre to estimate the per-frame camera drift in
pixels.  Accumulated drift IS the recoil pattern.

Method C — bullet-hole calibration
────────────────────────────────────────────────────────────────────
User aims at a clean wall in a training map, captures a "before"
screenshot, fires a full magazine without moving the mouse, captures
an "after" screenshot.  We diff the two, cluster the changed pixels
into bullet-hole centroids, sort top-down (matching most spray
patterns' first-shot-highest convention), and produce the trace.

────────────────────────────────────────────────────────────────────
Why this is in its own module
────────────────────────────────────────────────────────────────────
- Hard imports of `numpy` and `mss` go here so a missing dependency
  fails localised, not on app startup.
- The capture state is global to a single user session (you're not
  going to run two captures concurrently); keeping it in a module
  rather than passing around a class instance is simpler.

Anti-cheat caveat
─────────────────
Same as anti-recoil itself.  The CAPTURE side is not flagged by most
anti-cheat (overlays do screen capture all the time), but the eventual
PLAYBACK still injects input into the game.  Don't use this in ranked
competitive games.
"""

from __future__ import annotations
import json
import os
import threading
import time
from typing import Optional

from core.utils import get_logger, atomic_write_json
from config import APPDATA_DIR

log = get_logger(__name__)

USER_PRESETS_PATH = os.path.join(APPDATA_DIR, "user_recoil_presets.json")

# Hard imports for capture functionality.  Surfaced via `is_available()`
# so callers can fail gracefully when running on a build where these
# weren't bundled.
_np = None
_mss = None
_lib_err: Optional[str] = None


def _ensure_libs():
    """Lazy-import numpy and mss.  Returns True if both loaded.  Caches
    the failure reason so we don't keep retrying broken imports on every
    capture click."""
    global _np, _mss, _lib_err
    if _np is not None and _mss is not None:
        return True
    if _lib_err:
        return False
    try:
        import numpy as _np_mod
        _np = _np_mod
    except Exception as e:
        _lib_err = f"numpy unavailable: {e}"
        log.warning(_lib_err)
        return False
    try:
        import mss as _mss_mod
        _mss = _mss_mod
    except Exception as e:
        _lib_err = f"mss unavailable: {e}"
        log.warning(_lib_err)
        return False
    return True


def is_available() -> dict:
    """Probe whether the capture features can run.  UI surfaces this
    before showing the capture buttons."""
    ok = _ensure_libs()
    return {
        "available": ok,
        "err":       _lib_err,
        "numpy":     bool(_np),
        "mss":       bool(_mss),
    }


# ────────────────────────────────────────────────────────────────────
# Adaptive-tuning pause integration (v3.1)
# ────────────────────────────────────────────────────────────────────
# When a capture session is live we don't want AT pushing the GPU
# core/mem offsets up or down mid-capture — even a small clock change
# can shift muzzle-flash brightness, shadow flicker, or frame timing
# enough to bias the phase-correlation deltas.  The same applies to
# gamepad latency measurement (which times a synthetic input → screen
# response and needs a stable clock).
#
# We wrap the import + pause/resume in try/except because:
#   • adaptive_tuning is a sibling module — importing it pulls a fair
#     bit of state (perf_monitor, gpu_overclock chains).  If any of
#     that fails at module-load on a busted install we don't want the
#     capture itself to fail.
#   • pause/resume are advisory — AT functioning is a nice-to-have for
#     capture quality, not a hard requirement.

def _pause_at_for_capture(source: str) -> None:
    """Pause Adaptive Tuning for the duration of a capture/measurement.
    Stacked by source string — multiple concurrent capture types can
    each call pause() with their own source and only the LAST resume
    actually unblocks the tuner."""
    try:
        from core import adaptive_tuning
        adaptive_tuning.pause(source)
    except Exception as e:
        log.debug(f"adaptive_tuning.pause({source}) failed: {e}")


def _resume_at_for_capture(source: str) -> None:
    """Counterpart to _pause_at_for_capture.  Safe to call even if pause
    was never called (resume on an unknown source is a no-op)."""
    try:
        from core import adaptive_tuning
        adaptive_tuning.resume(source)
    except Exception as e:
        log.debug(f"adaptive_tuning.resume({source}) failed: {e}")


# ────────────────────────────────────────────────────────────────────
# Window / monitor helpers — figure out WHAT to screenshot
# ────────────────────────────────────────────────────────────────────

def _get_foreground_window_rect() -> "Optional[tuple[int, int, int, int]]":
    """Return (left, top, right, bottom) of the foreground window in
    physical screen coordinates, or None if it can't be determined.

    `mss` accepts these as `monitor` dicts so this is what feeds the
    capture loop.  We use the foreground window so the user can leave
    GhostShell open in the background and the capture follows whatever
    game window has focus when they hit Start."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        # Use GetClientRect + ClientToScreen rather than GetWindowRect
        # so we measure the GAME's render area, excluding the title bar
        # and window decorations.  Critical for windowed (not borderless)
        # mode — title-bar pixels would skew motion estimation.
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        left   = pt.x
        top    = pt.y
        right  = pt.x + rect.right
        bottom = pt.y + rect.bottom
        if right - left < 200 or bottom - top < 200:
            return None      # too small to be a game window
        return (left, top, right, bottom)
    except Exception as e:
        log.debug(f"foreground rect failed: {e}")
        return None


def _grab_region(sct, rect: "tuple[int, int, int, int]"):
    """Grab a screen region as a NumPy array (H, W, 4) BGRA."""
    left, top, right, bottom = rect
    monitor = {"left": int(left), "top": int(top),
               "width": int(right - left), "height": int(bottom - top)}
    raw = sct.grab(monitor)
    return _np.array(raw)


def _to_grayscale(rgba_arr):
    """Cheap luminance conversion — average of B+G+R, drop alpha."""
    # rgba_arr is (H, W, 4) BGRA.  Skip the alpha channel.
    bgr = rgba_arr[:, :, :3].astype(_np.float32)
    return bgr.mean(axis=2)


# ────────────────────────────────────────────────────────────────────
# Method A — Screen tracking (FFT-based phase correlation)
# ────────────────────────────────────────────────────────────────────
#
# Phase correlation finds the (dx, dy) shift between two images by
# computing the inverse FFT of their cross-power spectrum.  The peak's
# location IS the shift in pixels.  Operates on a small ROI (256x256)
# around the screen centre so background HUD elements at the edges
# don't bias the result.
#
# Why phase correlation vs. block matching:
#   - O(N log N) vs O(N²) — way faster on large ROIs
#   - Sub-pixel accuracy possible (we don't bother — pixel-level is fine)
#   - Robust to brightness changes (muzzle flash flicker)
#   - Built into NumPy via fft2 — no OpenCV dependency
#
# ROI size = 256 means a 256x256 px patch from the centre of the game
# window.  At 1080p that's about 24% of the vertical span — enough for
# meaningful texture content, small enough that background HUD doesn't
# dominate.

_ROI_SIZE = 256

# Capture state (single global session — only one capture can run at a time)
_session: Optional[dict] = None
_session_lock = threading.Lock()


def _phase_correlate(a, b) -> "tuple[int, int]":
    """Return the integer pixel shift (dx, dy) that maps frame `a` onto
    frame `b`.  In other words: positive dy means the scene appears `dy`
    pixels lower in `b` than in `a`, so the camera drifted UP by `dy`
    pixels between frames.  Returns (0, 0) on a static scene or
    degenerate FFT.

    Both inputs must be same-size 2D float arrays.

    Sign convention is critical for the recoil pattern semantics — we
    want the captured trace to record "how much did the scene move from
    frame N to frame N+1", so the cross-power spectrum must be
    `FFT(b) * conj(FFT(a))` (note the order — the reverse gives the
    inverse shift, which would invert the recoil pattern)."""
    h, w = a.shape
    fa = _np.fft.fft2(a)
    fb = _np.fft.fft2(b)
    cross = fb * _np.conj(fa)
    mag = _np.abs(cross)
    # Avoid divide-by-zero in pure black/white regions
    cross_norm = cross / (mag + 1e-9)
    r = _np.fft.ifft2(cross_norm).real
    peak = _np.unravel_index(_np.argmax(r), r.shape)
    py, px = int(peak[0]), int(peak[1])
    # Convert wrap-around index to signed shift.  Peak at (0,0) = no
    # shift; peaks on the right half wrap from negative shifts.
    if py > h // 2: py -= h
    if px > w // 2: px -= w
    return px, py


def start_screen_capture(rpm: int = 600,
                         max_duration_sec: float = 6.0,
                         capture_hz: int = 120,
                         auto_fire: bool = False) -> dict:
    """Begin a screen-tracking capture session.  The session runs in a
    background thread, polling the foreground window at `capture_hz`
    and storing per-frame motion vectors until either the user calls
    `stop_screen_capture` OR `max_duration_sec` elapses.

    `rpm` (rounds per minute) controls how the trace is later binned
    into one (dx, dy) per shot — required so the saved preset has the
    right number of steps.

    `auto_fire` (controller users): if True, GhostShell pushes RT through
    the virtual pad for the duration of the capture so the user doesn't
    need to physically hold their fire trigger.  Requires the active
    profile to be in virtual_controller mode AND the mapper master
    switch on; if those aren't true, the field is silently ignored
    (returns autofire_engaged=False so the UI can warn the user).

    Returns {ok, session_id, autofire_engaged} on success."""
    if not _ensure_libs():
        return {"ok": False, "err": _lib_err or "capture libraries unavailable"}
    # v3.1 — pause AT for the capture window so its OC changes don't
    # perturb the phase-correlation signal.  Resumed in the runner's
    # finally block (and again on stop_screen_capture as belt-and-braces).
    _pause_at_for_capture("recoil-capture-screen")
    rect = _get_foreground_window_rect()
    if rect is None:
        _resume_at_for_capture("recoil-capture-screen")
        return {"ok": False, "err": "couldn't read foreground window — is your game in windowed/borderless mode?"}

    autofire_engaged = False
    if auto_fire:
        try:
            from core import gamepad_mapper
            autofire_engaged = gamepad_mapper.set_capture_autofire(1.0)
        except Exception as e:
            log.debug(f"autofire engage failed: {e}")
            autofire_engaged = False

    global _session
    with _session_lock:
        if _session is not None and _session.get("active"):
            if autofire_engaged:
                # Roll back the RT we just pushed
                try:
                    from core import gamepad_mapper
                    gamepad_mapper.set_capture_autofire(0.0)
                except Exception: pass
            return {"ok": False, "err": "a capture session is already running"}
        sid = f"sc-{int(time.time() * 1000)}"
        # Centre ROI inside the window
        L, T, R, B = rect
        cx = (L + R) // 2
        cy = (T + B) // 2
        roi = (cx - _ROI_SIZE // 2, cy - _ROI_SIZE // 2,
               cx + _ROI_SIZE // 2, cy + _ROI_SIZE // 2)
        stop_event = threading.Event()
        _session = {
            "id":          sid,
            "method":      "screen",
            "active":      True,
            "rpm":         max(60, min(1500, int(rpm))),
            "rect":        rect,
            "roi":         roi,
            "stop_event":  stop_event,
            "deltas":      [],         # list of (dx, dy, t)
            "started_ts":  time.time(),
            "max_dur":     float(max_duration_sec),
            "capture_hz":  max(30, min(240, int(capture_hz))),
            "auto_fire":   bool(autofire_engaged),
            "err":         None,
        }
    threading.Thread(
        target=_screen_capture_runner,
        args=(_session,),
        daemon=True,
        name=f"recoil-capture-{sid}",
    ).start()
    return {"ok": True, "session_id": sid,
            "autofire_engaged": autofire_engaged,
            "autofire_requested": bool(auto_fire)}


def _screen_capture_runner(session: dict):
    """Background worker — captures frames at session['capture_hz'] and
    appends per-frame phase-correlation deltas to session['deltas'].

    Always releases auto-fire on exit (even on crash) so we can never
    leave RT held against the user's will."""
    sid = session["id"]
    roi = session["roi"]
    stop_event = session["stop_event"]
    period = 1.0 / float(session["capture_hz"])
    deadline_ts = session["started_ts"] + session["max_dur"]
    try:
        with _mss.mss() as sct:
            # Grab reference frame; bail early if the region is invalid
            ref = _to_grayscale(_grab_region(sct, roi))
            session["deltas"].append((0, 0, 0.0))
            prev = ref
            t0 = time.perf_counter()
            next_t = t0 + period
            while not stop_event.is_set():
                now = time.time()
                if now >= deadline_ts:
                    break
                # Coarse pacing — sleep until next slot.  Don't bother
                # with the busy-wait we use in the polling loop; capture
                # frames are I/O-bound on screen grabs anyway.
                sleep_for = next_t - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                next_t += period
                try:
                    cur = _to_grayscale(_grab_region(sct, roi))
                except Exception as e:
                    log.debug(f"capture {sid} grab failed: {e}")
                    continue
                dx, dy = _phase_correlate(prev, cur)
                # v3 — implausible-jump handling: muzzle flash / ADS
                # transition / scene cut.  Append (0,0) for this sample
                # (so the bin shape stays consistent at the gun's RPM)
                # AND advance prev — keeping prev at the pre-flash frame
                # would mean every subsequent correlation also looks
                # implausible against the stale reference.
                if abs(dx) > _ROI_SIZE // 2 or abs(dy) > _ROI_SIZE // 2:
                    dx, dy = 0, 0
                session["deltas"].append((int(dx), int(dy),
                                          time.perf_counter() - t0))
                prev = cur
    except Exception as e:
        session["err"] = str(e)
        log.warning(f"capture {sid} crashed: {e}")
    finally:
        # ALWAYS release autofire — never leave RT pinned
        if session.get("auto_fire"):
            try:
                from core import gamepad_mapper
                gamepad_mapper.set_capture_autofire(0.0)
            except Exception: pass
        # ALWAYS resume AT — paired with the pause at start_screen_capture.
        # Even on a crashed runner we must release the pause so AT doesn't
        # stay frozen for the rest of the GhostShell session.
        _resume_at_for_capture("recoil-capture-screen")
        session["active"] = False
        log.info(f"capture {sid} ended — {len(session['deltas'])} samples"
                 + (" (auto-fired)" if session.get("auto_fire") else ""))


# ────────────────────────────────────────────────────────────────────
# Method B — Counter-pull recording
# ────────────────────────────────────────────────────────────────────
#
# User aims at a wall, fires a full mag, and MANUALLY counter-pulls
# (drags their mouse down or pushes the right stick down) to keep the
# crosshair on the target.  GhostShell records the inputs they're
# making during the firing window.  Those deltas ARE the compensation
# pattern — when played back as anti-recoil, they reproduce what the
# user did manually.
#
# Two source modes:
#   • mouse              — record raw mouse-move deltas, sum per shot
#   • controller_rstick  — record right-stick offset, average per shot
#                           (stick positions don't accumulate the way
#                           mouse deltas do — the stick BEING at +0.5
#                           is the compensation, not "moving by +0.5")
#
# Reuses the same `_session` slot as the screen-tracking capture so
# only one capture can run at a time (avoids ambiguous state).

CP_SOURCE_MOUSE     = "mouse"
CP_SOURCE_RSTICK    = "controller_rstick"


def start_counter_pull_capture(rpm: int = 600,
                                max_duration_sec: float = 6.0,
                                source: str = CP_SOURCE_MOUSE,
                                auto_fire: bool = False) -> dict:
    """Begin a counter-pull capture session.  No screen capture, no
    optical flow — just record what the user is doing with their input
    device while they fire + manually compensate.

    `source` selects which input stream to record:
      • 'mouse'              — accumulates raw mouse cursor deltas
      • 'controller_rstick'  — samples right-stick position at the gun's
                                fire rate

    `auto_fire` (controller users): if True, GhostShell pushes RT through
    the virtual pad for the duration so the user can focus on the stick
    motion without holding the trigger.  Requires virtual_controller mode.
    """
    if source not in (CP_SOURCE_MOUSE, CP_SOURCE_RSTICK):
        return {"ok": False, "err": f"unknown source '{source}'"}

    # v3.1 — pause AT for the capture window so its clock changes can't
    # change the feel of the gun (and bias what the user counter-pulls).
    # Paired resume lives in _counter_pull_runner's finally block.
    _pause_at_for_capture("recoil-capture-counter-pull")

    autofire_engaged = False
    if auto_fire:
        try:
            from core import gamepad_mapper
            autofire_engaged = gamepad_mapper.set_capture_autofire(1.0)
        except Exception as e:
            log.debug(f"autofire engage failed: {e}")
            autofire_engaged = False

    global _session
    with _session_lock:
        if _session is not None and _session.get("active"):
            if autofire_engaged:
                try:
                    from core import gamepad_mapper
                    gamepad_mapper.set_capture_autofire(0.0)
                except Exception: pass
            _resume_at_for_capture("recoil-capture-counter-pull")
            return {"ok": False, "err": "a capture session is already running"}
        sid = f"cp-{int(time.time() * 1000)}"
        stop_event = threading.Event()
        _session = {
            "id":             sid,
            "method":         "counter_pull",
            "source":         source,
            "active":         True,
            "rpm":            max(60, min(1500, int(rpm))),
            "stop_event":     stop_event,
            # Mouse: list of (dx, dy, t).  Stick: list of (rx_norm, ry_norm, t).
            "samples":        [],
            "started_ts":     time.time(),
            # perf_counter timestamp paired with started_ts — hooks use this
            # to compute relative elapsed time without re-reading wall clock
            "started_ts_perf": time.perf_counter(),
            "max_dur":        float(max_duration_sec),
            "auto_fire":      bool(autofire_engaged),
            "err":            None,
        }
    threading.Thread(
        target=_counter_pull_runner,
        args=(_session,),
        daemon=True,
        name=f"recoil-counter-pull-{sid}",
    ).start()
    log.info(f"counter-pull capture started: source={source} rpm={rpm} "
             f"max_dur={max_duration_sec}s autofire={autofire_engaged}")
    return {
        "ok":               True,
        "session_id":       sid,
        "source":           source,
        "autofire_engaged": autofire_engaged,
        "autofire_requested": bool(auto_fire),
    }


def _counter_pull_runner(session: dict):
    """Background worker — just spins until duration elapses or stop is
    signalled.  Sample collection happens via the public hook functions
    (record_counter_pull_delta / record_counter_pull_stick) called from
    the polling loop's existing input plumbing.

    Always releases auto-fire on exit, even on crash."""
    sid = session["id"]
    stop_event = session["stop_event"]
    deadline_ts = session["started_ts"] + session["max_dur"]
    try:
        while not stop_event.is_set():
            if time.time() >= deadline_ts:
                break
            time.sleep(0.05)
    except Exception as e:
        session["err"] = str(e)
        log.warning(f"counter-pull {sid} crashed: {e}")
    finally:
        if session.get("auto_fire"):
            try:
                from core import gamepad_mapper
                gamepad_mapper.set_capture_autofire(0.0)
            except Exception: pass
        # ALWAYS resume AT — paired with the pause at start_counter_pull_capture.
        _resume_at_for_capture("recoil-capture-counter-pull")
        session["active"] = False
        log.info(f"counter-pull {sid} ended — {len(session.get('samples') or [])} samples")


def record_counter_pull_delta(dx: int, dy: int) -> None:
    """Hook from the polling loop on every mouse-move event.  Skipped
    unless a counter-pull session with source=mouse is live."""
    s = _session
    if s is None or not s.get("active"):
        return
    if s.get("method") != "counter_pull" or s.get("source") != CP_SOURCE_MOUSE:
        return
    if dx == 0 and dy == 0:
        return
    t = time.perf_counter() - (s.get("started_ts_perf") or 0.0)
    s["samples"].append({"dx": int(dx), "dy": int(dy), "t": t})


def record_counter_pull_stick(rx: int, ry: int) -> None:
    """Hook from the polling loop's per-tick stick sampling.  Skipped
    unless a counter-pull session with source=controller_rstick is live.

    `rx` / `ry` are XInput int16 values.  Normalised to -1..1 here, with
    Y FLIPPED to screen convention (XInput +y = UP, our preset format
    uses +y = DOWN to match recoil-pull semantics).  No deduping — we
    want the full position trace at sample rate so per-shot averaging
    has good signal."""
    s = _session
    if s is None or not s.get("active"):
        return
    if s.get("method") != "counter_pull" or s.get("source") != CP_SOURCE_RSTICK:
        return
    rx_norm = max(-1.0, min(1.0, rx / 32767.0))
    # Flip XInput Y → screen convention so saved preset matches the
    # recoil-pull sign convention used by the rest of the system.
    ry_norm = max(-1.0, min(1.0, -ry / 32767.0))
    t = time.perf_counter() - (s.get("started_ts_perf") or 0.0)
    s["samples"].append({"x": rx_norm, "y": ry_norm, "t": t})


def stop_counter_pull_capture() -> dict:
    """End the active counter-pull session and return the binned
    per-shot trace.  Binning method depends on source:
      • mouse              — sum within each fire-period interval
                              (deltas accumulate)
      • controller_rstick  — average within each interval (positions
                              are absolute, not deltas)"""
    global _session
    with _session_lock:
        if _session is None:
            return {"ok": False, "err": "no capture in progress"}
        if _session.get("method") != "counter_pull":
            return {"ok": False, "err": "active capture is not a counter-pull session"}
        sid = _session["id"]
        _session["stop_event"].set()
    for _ in range(30):
        if not _session.get("active"):
            break
        time.sleep(0.05)
    samples = list(_session.get("samples") or [])
    rpm     = _session.get("rpm", 600)
    src     = _session.get("source")
    err     = _session.get("err")
    with _session_lock:
        _session = None
    if not samples:
        return {"ok": False, "err": err or
                "no input recorded — did you actually move the mouse / stick?"}

    binned = _bin_counter_pull(samples, rpm, src)
    return {
        "ok":           True,
        "session_id":   sid,
        "rpm":          rpm,
        "source":       src,
        "raw_count":    len(samples),
        "binned":       binned,
        "err":          err,
    }


def _bin_counter_pull(samples: list, rpm: int, source: str) -> list:
    """Per-shot binning for counter-pull captures.  Output shape matches
    what `save_capture_as_preset` expects: list of {dx, dy} integers,
    where the integers are pixel deltas (mouse) or stick-equivalent
    pixel deltas (controller, scaled so playback feels comparable).

    Mouse path: sum dx/dy across all samples in each 60/rpm-second bin.
    Controller path: average rx/ry-normalised positions in the bin,
    scale up to a "pixel-equivalent" so the same sensitivity_scale
    knob applies on playback.  We pick scale = 200 so the user's full
    stick deflection corresponds to ~200 px-equivalent (matching the
    default sensitivity_scale of 0.005 = 200 px per full stick)."""
    if not samples:
        return []
    shot_period = 60.0 / max(60, int(rpm))
    last_t = samples[-1]["t"]
    n_shots = max(1, int(round(last_t / shot_period)))
    bins = [[0.0, 0.0, 0] for _ in range(n_shots)]   # [sum_x, sum_y, count]

    for s in samples:
        idx = min(n_shots - 1, max(0, int(s["t"] / shot_period)))
        if source == CP_SOURCE_MOUSE:
            bins[idx][0] += float(s["dx"])
            bins[idx][1] += float(s["dy"])
            bins[idx][2] += 1
        else:    # controller_rstick — accumulate normalised position for averaging
            bins[idx][0] += float(s["x"])
            bins[idx][1] += float(s["y"])
            bins[idx][2] += 1

    out = []
    if source == CP_SOURCE_MOUSE:
        # Mouse deltas pass through as-is (already pixel units).  Mouse-y
        # in screen coords is +y = DOWN, which matches the preset format.
        for sx, sy, _ in bins:
            out.append({"dx": int(round(sx)), "dy": int(round(sy))})
    else:
        # Controller stick — average position per bin, scale to
        # pixel-equivalent so the same sensitivity_scale slider works.
        STICK_TO_PX = 200.0
        for sx, sy, n in bins:
            if n == 0:
                out.append({"dx": 0, "dy": 0})
                continue
            avg_x = sx / n
            avg_y = sy / n
            out.append({"dx": int(round(avg_x * STICK_TO_PX)),
                        "dy": int(round(avg_y * STICK_TO_PX))})
    return out


def auto_fire_mag(duration_sec: float = 3.0) -> dict:
    """One-shot "fire RT for N seconds via the virtual pad" used by the
    bullet-hole capture flow.  Lets the user click "Fire" between their
    Before / After snapshots without alt-tabbing to physically hold RT.

    Returns immediately; the firing happens on a background thread.
    Polling for completion isn't necessary — duration is short and the
    UI can just `setTimeout` parallel to the call."""
    try:
        from core import gamepad_mapper
        if not gamepad_mapper.is_virtual_pad_active():
            return {"ok": False, "err": "virtual pad isn't running — "
                    "switch the active profile to Virtual controller mode "
                    "and turn the mapper master switch on first"}
    except Exception as e:
        return {"ok": False, "err": f"could not reach gamepad_mapper: {e}"}

    dur = max(0.5, min(10.0, float(duration_sec or 3.0)))

    def _runner():
        try:
            from core import gamepad_mapper
            ok = gamepad_mapper.set_capture_autofire(1.0)
            if not ok:
                return
            stop_at = time.time() + dur
            while time.time() < stop_at:
                time.sleep(0.05)
        except Exception as e:
            log.debug(f"auto_fire_mag worker failed: {e}")
        finally:
            try:
                from core import gamepad_mapper
                gamepad_mapper.set_capture_autofire(0.0)
            except Exception: pass

    threading.Thread(target=_runner, daemon=True, name="recoil-autofire").start()
    return {"ok": True, "duration_sec": dur}


def stop_screen_capture() -> dict:
    """End the active screen-tracking session and return the captured
    delta trace.  The trace can then be passed to `save_capture_as_preset`
    after the user picks a name + sensitivity scale."""
    global _session
    with _session_lock:
        if _session is None:
            return {"ok": False, "err": "no capture in progress"}
        sid = _session["id"]
        _session["stop_event"].set()
    # Wait briefly for the runner to exit cleanly
    for _ in range(30):
        if not _session.get("active"):
            break
        time.sleep(0.05)
    deltas = list(_session.get("deltas") or [])
    rpm    = _session.get("rpm", 600)
    err    = _session.get("err")
    # Move out of the active slot so a new capture can start
    with _session_lock:
        _session = None
    if not deltas:
        return {"ok": False, "err": err or "no frames captured"}
    binned = _bin_deltas_by_rpm(deltas, rpm)
    return {
        "ok":         True,
        "session_id": sid,
        "rpm":        rpm,
        "raw_count":  len(deltas),
        "binned":     binned,    # list of {dx, dy} per shot
        "err":        err,
    }


def get_capture_status() -> dict:
    """Cheap poll endpoint for the UI to show 'Capturing… Nms / Nmax,
    M samples'.  Samples count covers BOTH session types (screen
    tracking writes to `deltas`, counter-pull writes to `samples`)."""
    with _session_lock:
        if _session is None:
            return {"active": False}
        elapsed = time.time() - _session["started_ts"]
        # Both session types have one of these — pick whichever is
        # populated so the UI sees a non-zero number when input is
        # actually arriving.
        deltas  = _session.get("deltas") or []
        samples = _session.get("samples") or []
        sample_count = len(deltas) + len(samples)
        return {
            "active":       bool(_session.get("active")),
            "method":       _session.get("method"),
            "source":       _session.get("source"),
            "elapsed_sec":  round(elapsed, 2),
            "frames":       sample_count,    # legacy alias for UIs that read it
            "samples":      sample_count,
            "max_dur":      _session.get("max_dur"),
        }


def _bin_deltas_by_rpm(deltas: list, rpm: int) -> list:
    """Convert per-frame deltas into per-shot deltas.

    `deltas` is [(dx, dy, t_sec), ...] starting from t=0.  Aggregate
    deltas that fall in the same shot interval (60/rpm seconds wide)
    by summing dx/dy.  This matches the existing recoil-preset format
    where each macro step represents one shot."""
    if not deltas:
        return []
    shot_period = 60.0 / max(60, int(rpm))  # seconds per shot
    if not deltas:
        return []
    last_t = deltas[-1][2]
    n_shots = max(1, int(round(last_t / shot_period)))
    bins = [[0, 0] for _ in range(n_shots)]
    for dx, dy, t in deltas:
        idx = min(n_shots - 1, int(t / shot_period))
        bins[idx][0] += dx
        bins[idx][1] += dy
    return [{"dx": int(b[0]), "dy": int(b[1])} for b in bins]


# ────────────────────────────────────────────────────────────────────
# Method C — Bullet-hole detection
# ────────────────────────────────────────────────────────────────────
#
# State for the two-screenshot workflow lives in `_bh_state`.  The user
# clicks Capture-Before, fires a mag, clicks Capture-After.  Then we
# diff and cluster.

_bh_state: dict = {
    "before":    None,  # (H, W, 3) uint8 or None
    "after":     None,
    "rect":      None,
    "before_ts": 0.0,
    "after_ts":  0.0,
}
_bh_lock = threading.Lock()


def bullet_hole_snapshot(which: str) -> dict:
    """Capture the foreground window's current frame as either the
    'before' or 'after' reference for bullet-hole detection."""
    if not _ensure_libs():
        return {"ok": False, "err": _lib_err or "capture libraries unavailable"}
    if which not in ("before", "after"):
        return {"ok": False, "err": "which must be 'before' or 'after'"}
    rect = _get_foreground_window_rect()
    if rect is None:
        return {"ok": False, "err": "couldn't read foreground window — is your game in windowed/borderless mode?"}
    try:
        with _mss.mss() as sct:
            arr = _grab_region(sct, rect)
            # Drop alpha; keep BGR uint8
            bgr = arr[:, :, :3].copy()
    except Exception as e:
        return {"ok": False, "err": f"capture failed: {e}"}
    with _bh_lock:
        _bh_state[which] = bgr
        _bh_state["rect"] = rect
        _bh_state[f"{which}_ts"] = time.time()
    log.info(f"bullet-hole {which} snapshot captured ({bgr.shape[1]}x{bgr.shape[0]})")
    return {
        "ok":       True,
        "which":    which,
        "shape":    [int(bgr.shape[1]), int(bgr.shape[0])],
        "have_before": _bh_state.get("before") is not None,
        "have_after":  _bh_state.get("after")  is not None,
    }


def detect_bullet_holes(rpm: int = 600,
                        threshold: int = 60,
                        min_size_px: int = 12,
                        max_size_px: int = 800) -> dict:
    """Diff the before/after screenshots and return bullet-hole centroids
    sorted by firing order (top-down — matches most patterns' first-
    shot-highest convention).

    `threshold` is the per-pixel BGR-sum delta below which we consider
    a pixel unchanged (filters JPEG noise / ambient lighting drift).
    `min_size_px` / `max_size_px` filter out specks (false positives
    from particle effects) and oversized regions (blood, smoke, scope
    glints, etc.)."""
    if not _ensure_libs():
        return {"ok": False, "err": _lib_err or "capture libraries unavailable"}
    with _bh_lock:
        before = _bh_state.get("before")
        after  = _bh_state.get("after")
    if before is None or after is None:
        return {"ok": False, "err": "need both before and after snapshots first"}
    if before.shape != after.shape:
        return {"ok": False, "err": f"shape mismatch ({before.shape} vs {after.shape}) "
                                    f"— don't resize the window between snapshots"}

    # Diff in float to avoid uint8 overflow, then threshold on summed
    # absolute channel difference.
    diff = _np.abs(after.astype(_np.int16) - before.astype(_np.int16)).sum(axis=2)
    mask = diff > int(threshold)

    # Connected-components labeling without scipy.  Iterative flood-fill
    # on the boolean mask via a stack — slow in pure Python but fast in
    # NumPy when the mask is small.  For typical 1080p with maybe
    # 20-50 holes, runs in <100 ms.
    labels, num = _label_connected(mask)
    if num == 0:
        return {"ok": False, "err": "no changed regions found — try a darker wall, "
                                    "lower threshold, or different game settings"}

    # Compute centroid + area per component
    holes = []
    for label_id in range(1, num + 1):
        ys, xs = _np.where(labels == label_id)
        area = int(len(xs))
        if area < min_size_px or area > max_size_px:
            continue
        cx = float(xs.mean())
        cy = float(ys.mean())
        holes.append({"x": cx, "y": cy, "area": area})

    if not holes:
        return {"ok": False, "err": f"all changed regions filtered out by size "
                                    f"(found {num}; tune min_size_px/max_size_px)"}

    # Sort by Y DESCENDING (largest screen y first = LOWEST on screen
    # = where the FIRST shot landed).  Recoil pushes subsequent shots
    # UP the wall, so they have progressively smaller screen y.  This
    # ordering matches actual fire order for the typical first-shot-
    # lowest spray pattern.  Users can manually re-order in the UI if
    # a game's pattern fires upside-down.
    holes.sort(key=lambda h: (-h["y"], h["x"]))

    # Anchor at the first hole (= first shot fired = bottom of cluster).
    # Convert positions into deltas matching the COMPENSATION CONVENTION
    # used by the rest of the system: +dx = stick right, +dy = stick
    # down (screen pixel scaled).
    #
    # Subsequent holes drift relative to the anchor in whatever
    # direction the gun's recoil rotated it.  To COUNTER that rotation,
    # the player needs to push the stick the OPPOSITE direction.  So
    # we negate BOTH axes when emitting the binned deltas — the
    # compensation magnitude is `|hole - anchor|` in the opposite
    # direction.
    if holes:
        ax, ay = holes[0]["x"], holes[0]["y"]
        deltas = [{"dx": int(round(-(h["x"] - ax))),
                   "dy": int(round(-(h["y"] - ay)))} for h in holes]
    else:
        deltas = []

    return {
        "ok":      True,
        "rpm":     int(rpm),
        "count":   len(holes),
        "holes":   holes,        # for UI overlay
        "binned":  deltas,       # same shape as screen-track output
    }


def _label_connected(mask) -> "tuple":
    """Pure-NumPy 4-connected-component labeling.  Returns (labels, n).

    Implementation: row-by-row scan with a small union-find on labels,
    then a remap pass.  Fast enough for 1080p screenshots — runs in the
    tens of milliseconds for a few dozen connected regions."""
    h, w = mask.shape
    labels = _np.zeros((h, w), dtype=_np.int32)
    parent: list[int] = [0]    # union-find roots; index 0 = background

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb: parent[rb] = ra
            else:       parent[ra] = rb

    next_id = 1
    # First pass — assign provisional labels.  Process row-by-row for
    # cache friendliness; iterate via NumPy-level lookups.
    mask_py = mask.tolist()    # one-shot conversion; faster than per-pixel indexing
    labels_py = [[0] * w for _ in range(h)]
    for y in range(h):
        row = mask_py[y]
        for x in range(w):
            if not row[x]:
                continue
            up   = labels_py[y-1][x] if y > 0 else 0
            left = labels_py[y][x-1] if x > 0 else 0
            if up == 0 and left == 0:
                parent.append(next_id)
                labels_py[y][x] = next_id
                next_id += 1
            elif up != 0 and left != 0 and up != left:
                labels_py[y][x] = min(up, left)
                union(up, left)
            else:
                labels_py[y][x] = up if up != 0 else left

    # Second pass — remap each label to its root and densely renumber
    remap = {}
    new_id = 0
    for i in range(1, next_id):
        r = find(i)
        if r not in remap:
            new_id += 1
            remap[r] = new_id

    # Apply via NumPy.  Build a lookup table keyed by provisional label.
    lut = _np.zeros(next_id, dtype=_np.int32)
    for prov, root in [(i, find(i)) for i in range(1, next_id)]:
        lut[prov] = remap[root]
    labels = lut[_np.array(labels_py, dtype=_np.int32)]
    return labels, new_id


def reset_bullet_hole_state() -> dict:
    """Drop both before/after snapshots — used by the UI's reset button."""
    with _bh_lock:
        _bh_state["before"]    = None
        _bh_state["after"]     = None
        _bh_state["rect"]      = None
        _bh_state["before_ts"] = 0.0
        _bh_state["after_ts"]  = 0.0
    return {"ok": True}


def get_bullet_hole_state() -> dict:
    """Status query for the UI."""
    with _bh_lock:
        return {
            "have_before": _bh_state.get("before") is not None,
            "have_after":  _bh_state.get("after")  is not None,
            "before_ts":   _bh_state.get("before_ts") or 0.0,
            "after_ts":    _bh_state.get("after_ts")  or 0.0,
        }


# ────────────────────────────────────────────────────────────────────
# User-preset storage
# ────────────────────────────────────────────────────────────────────
#
# Captured patterns get saved alongside the bundled curated presets.
# Storage path: %APPDATA%/GhostShell/user_recoil_presets.json
# Schema: same as core/recoil_presets.json (so the UI can render them
# in the same picker without code changes).

def _load_user_presets() -> list:
    if not os.path.isfile(USER_PRESETS_PATH):
        return []
    try:
        with open(USER_PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return list(data.get("presets") or [])
        if isinstance(data, list):
            return data
    except Exception as e:
        log.warning(f"failed to load user presets: {e}")
    return []


def _save_user_presets(presets: list) -> None:
    atomic_write_json(USER_PRESETS_PATH, {"presets": presets})


def list_user_presets() -> list:
    """Return all user-captured presets in raw schema form."""
    return _load_user_presets()


def save_capture_as_preset(name: str,
                           binned: list,
                           rpm: int,
                           method: str,
                           sensitivity_scale: float = 0.005,
                           weapon: str = "",
                           game: str = "",
                           description: str = "",
                           play_mode: str = "once",
                           auto_hold_rt: bool = True,
                           id_override: Optional[str] = None) -> dict:
    """Convert a captured (dx, dy)-per-shot trace into the recoil-preset
    schema and persist it.

    Pixel deltas (dx, dy) are scaled by `sensitivity_scale` to map into
    vstick magnitudes (0..1).  Default 0.005 means 200 px ≈ full stick
    deflection — a starting point users adjust based on their in-game
    sensitivity.  Y-axis is FLIPPED here so the resulting preset has the
    same SCREEN convention (+y = DOWN) as the bundled patterns."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "err": "preset name required"}
    if not binned:
        return {"ok": False, "err": "captured trace is empty"}

    sens = max(0.0001, min(0.05, float(sensitivity_scale or 0.005)))

    # Convention contract for the `binned` input: dy already represents
    # the COMPENSATION DIRECTION in screen-convention units (+y = stick
    # down).  Each capture method's stop/detect function is responsible
    # for translating its raw measurements into this convention BEFORE
    # passing them in.  So save just scales and clamps — no sign flip.
    #
    # Per-method conversions (for reference):
    #   • screen tracking      — phase-correlation dy is already screen
    #                             convention; scene drifts DOWN as camera
    #                             goes UP, so dy > 0 = camera-up = stick
    #                             needs to go DOWN.  No flip.
    #   • counter-pull mouse   — user drags mouse DOWN to compensate;
    #                             mouse hook reports dy > 0 for that.  No
    #                             flip.
    #   • counter-pull stick   — record_counter_pull_stick negates XInput
    #                             ry on the way in so the binned values
    #                             are already screen convention.  No flip.
    #   • bullet holes         — detect_bullet_holes negates dy on the
    #                             way out (holes climb UP the wall →
    #                             screen dy < 0; we need compensation
    #                             dy > 0).
    steps = []
    for d in binned:
        sx = float(d.get("dx", 0)) * sens
        sy = float(d.get("dy", 0)) * sens
        sx = max(-1.0, min(1.0, sx))
        sy = max(-1.0, min(1.0, sy))
        steps.append({"x": round(sx, 3), "y": round(sy, 3)})

    # Build the preset dict matching core/recoil_presets.json schema
    pid = id_override or f"user_{int(time.time())}"
    duration_ms = max(40, min(200, int(round(60000 / max(60, int(rpm))))))
    preset = {
        "id":           pid,
        "name":         name,
        "game":         game or "(captured)",
        "weapon":       weapon or "captured",
        "rpm":          int(rpm),
        "shots":        len(steps),
        "description":  description or
            (f"User-captured via {method}.  "
             f"Sensitivity scale {sens} — re-capture or adjust scale "
             f"if compensation feels off."),
        "play_mode":    play_mode if play_mode in ("once", "loop_while_held", "repeat_n") else "once",
        "steps":        steps,
        "duration_ms":  duration_ms,
        "auto_hold_rt": bool(auto_hold_rt),
        "sensitivity_scale": sens,
        "is_user":      True,
        "captured_method": method,
        "created_ts":   time.time(),
    }

    # Persist alongside any existing user presets.  If the same id is
    # being saved (i.e. user re-captured for an existing preset), we
    # replace in place rather than duplicating.
    presets = _load_user_presets()
    for i, p in enumerate(presets):
        if p.get("id") == pid:
            presets[i] = preset
            break
    else:
        presets.append(preset)
    _save_user_presets(presets)
    log.info(f"saved user recoil preset '{name}' ({len(steps)} steps, {method})")
    return {"ok": True, "preset": preset}


def delete_user_preset(preset_id: str) -> dict:
    presets = _load_user_presets()
    out = [p for p in presets if p.get("id") != preset_id]
    if len(out) == len(presets):
        return {"ok": False, "err": "preset not found"}
    _save_user_presets(out)
    log.info(f"deleted user recoil preset '{preset_id}'")
    return {"ok": True}
