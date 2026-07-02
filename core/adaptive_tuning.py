"""Adaptive Tuning v1 — auto-tunes the GPU OC during real gameplay.

Premise: stress tests are great for finding "stable for 60 seconds with
a synthetic workload" — but real games have different memory access
patterns, different temperatures, and different shader loads.  An OC
that passed Furmark may crash CS2 in week 2.  An OC that's stable in
CS2 may blue-screen Warzone.

So instead of one global OC, GhostShell learns *per-game* offsets
during play.  Conservative algorithm:

  1. Game starts → apply this game's last-known-good offsets
  2. Wait WARMUP seconds for the game to settle
  3. Every POLL seconds, sample perf_monitor's stability score (0-100)
  4. After CONSECUTIVE_GOOD samples at score ≥ MIN_SCORE: bump offset up
  5. Each step is core_step_mhz on the core axis, mem_step_mhz on memory.
     Alternate axes so neither pulls too far ahead.
  6. NEVER step into an offset on the per-game crash blacklist
     (managed by crash_recovery)
  7. NEVER exceed MAX_CORE/MAX_MEM safety caps
  8. Score < 65 (unstable) → step DOWN one step + tag this offset
     'warned' so it takes 2x as many good samples next time before AT
     will try it again
  9. Game exits cleanly → save current offsets as 'best_stable'
 10. Game crashes → crash_recovery handles step-down + blacklist; AT
     reads the new state on the next launch and starts from there

State is persisted per-game in:
    %APPDATA%\\GhostShell\\adaptive_tuning_state.json
Settings (global + per-game overrides) in:
    %APPDATA%\\GhostShell\\adaptive_tuning_settings.json
Step history (last 200 events across all games) in:
    %APPDATA%\\GhostShell\\adaptive_tuning_history.json

Public API:
    on_game_start(exe, display_name)   → called by game_profiles
    on_game_exit(exe, display_name)    → called by game_profiles
    get_settings()                      → global + per-game settings
    set_settings(**kwargs)              → update + persist
    get_game_state(exe=None)            → one game's state, or all
    set_game_enabled(exe, enabled)      → toggle AT for one game
    reset_game_state(exe=None)          → wipe one game (or all)
    get_history(limit=50)               → step events
    get_status()                        → live status (current game, step #)
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

from config import APPDATA_DIR
from core.utils import get_logger, atomic_write_json

log = get_logger("adaptive")

SETTINGS_PATH = os.path.join(APPDATA_DIR, "adaptive_tuning_settings.json")
STATE_PATH    = os.path.join(APPDATA_DIR, "adaptive_tuning_state.json")
HISTORY_PATH  = os.path.join(APPDATA_DIR, "adaptive_tuning_history.json")
# v3.1 — cross-game crash blacklist.  Driver-level crashes at a given
# (core, mem) offset get recorded here by crash_recovery and AT refuses
# to push back to those exact offsets in ANY game.  Persists across
# launches.
CROSS_GAME_BLACKLIST_PATH = os.path.join(APPDATA_DIR, "adaptive_tuning_xgame_blacklist.json")

# v3.1 — Aggressive opening phase.  For the first OPENING_PHASE_SEC
# after engage, the tuning loop uses reduced warmup + relaxed thresholds
# so it finds the rough ceiling faster.  After this window, it switches
# to normal-mode cautious behavior.
OPENING_PHASE_SEC = 5 * 60          # 5 minutes
OPENING_PHASE_WARMUP_RATIO = 0.5    # use 50% of normal warmup time
OPENING_PHASE_GOOD_RATIO   = 0.5    # need 50% of normal "consecutive good"

# v3.1 — Step-size hot-streak.  After N successful step-ups without a
# step-down, the step size doubles temporarily so we close the gap to
# the real ceiling faster.  Step-down resets the streak.
HOT_STREAK_THRESHOLD = 3
HOT_STREAK_MULTIPLIER = 2

# v3.1 — Confidence tiers based on stable_sessions + stable_minutes
# accumulated at the current offset since last step-up or step-down.
CONFIDENCE_TIERS = {
    "high":   {"sessions": 10, "minutes": 360},   # 10 sessions OR 6 hours
    "medium": {"sessions": 3,  "minutes": 60},    # 3 sessions OR 1 hour
    # anything else = "low"
}

# v3.1 — Auto-revert window.  If a crash is recorded within this many
# seconds of a step-up, that step-up is reverted automatically + the
# offset blacklisted for this game.
AUTO_REVERT_WINDOW_SEC = 24 * 3600

# v3.1 — Pause API.  Other features (anti-recoil capture, latency probe)
# call pause() to stop AT from changing OC during their measurement
# window.  Set-backed so multiple pause sources stack without breaking.
_pause_sources: "set[str]" = set()
_pause_lock = threading.Lock()

# Master defaults — conservative.  These are the *fallback* values used
# when a per-game mode preset doesn't override them.  Each game has its
# own mode (balanced / competitive / visual) which selects from
# MODE_PRESETS below.
DEFAULT_SETTINGS: dict[str, Any] = {
    # Master switch — opt-in.  Even when this is True, individual games
    # default to disabled until the user turns each one on (intentional —
    # we don't want AT touching cards mid-tournament).
    "enabled_globally":             False,
    # Default per-game enabled state for newly-seen games
    "default_per_game_enabled":     False,
    # Default mode for new games when AUTO_CLASSIFY doesn't recognize them
    "default_mode":                 "balanced",
}


# ─── Tuning modes ────────────────────────────────────────────────────────
# Each game picks a mode that controls how aggressively AT pushes the OC.
#
# competitive — input-latency focused.  Smaller steps, higher score
#               threshold (only step up at very high confidence), lower
#               offset caps (don't push thermal headroom that risks
#               frametime spikes), longer warmup.  For CS2/Valorant/etc.
#
# visual      — performance focused.  Bigger steps, lower threshold (more
#               aggressive), shorter warmup, higher caps.  For
#               Cyberpunk/RDR2/AAA single-player.
#
# balanced    — middle ground (current v1 behavior).  Default for unknown
#               games.
MODE_PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "core_step_mhz":             25,
        "mem_step_mhz":              100,
        "warmup_seconds":            60,
        "score_poll_interval_s":     30,
        "consecutive_good_required": 5,
        "min_score_to_step_up":      85,
        "max_score_to_step_down":    65,
        "max_core_offset":           300,
        "max_mem_offset":            1500,
    },
    "competitive": {
        "core_step_mhz":             15,    # smaller steps
        "mem_step_mhz":              50,
        "warmup_seconds":            90,    # longer warmup before tuning
        "score_poll_interval_s":     20,    # tighter polling
        "consecutive_good_required": 8,     # need more confirmation
        "min_score_to_step_up":      92,    # only step at very high confidence
        "max_score_to_step_down":    78,    # back off sooner
        "max_core_offset":           200,   # cap lower — don't risk thermals
        "max_mem_offset":            800,
    },
    "visual": {
        "core_step_mhz":             30,    # bigger steps
        "mem_step_mhz":              150,
        "warmup_seconds":            45,
        "score_poll_interval_s":     30,
        "consecutive_good_required": 4,     # promote faster
        "min_score_to_step_up":      80,    # less stringent
        "max_score_to_step_down":    60,
        "max_core_offset":           350,   # higher headroom
        "max_mem_offset":            1800,
    },
}


# Auto-classify well-known titles so users don't have to think about it.
# Anything not in this map defaults to settings["default_mode"].
AUTO_CLASSIFY: dict[str, str] = {
    # ── Competitive shooters / MOBAs / esports ──
    "cs2.exe":                            "competitive",
    "csgo.exe":                           "competitive",
    "valorant.exe":                       "competitive",
    "valorant-win64-shipping.exe":        "competitive",
    "rainbowsix.exe":                     "competitive",
    "r6sgame.exe":                        "competitive",
    "overwatch.exe":                      "competitive",
    "overwatch2.exe":                     "competitive",
    "leagueoflegends.exe":                "competitive",
    "league of legends.exe":              "competitive",
    "rocketleague.exe":                   "competitive",
    "fortniteclient-win64-shipping.exe":  "competitive",
    "r5apex.exe":                         "competitive",
    "r5apex_dx12.exe":                    "competitive",
    "destiny2.exe":                       "competitive",
    "thefinals.exe":                      "competitive",
    "discovery.exe":                      "competitive",
    "marvel-win64-shipping.exe":          "competitive",  # Marvel Rivals
    "deltaforce.exe":                     "competitive",
    "splitgate2.exe":                     "competitive",
    "dota2.exe":                          "competitive",
    "starcraft2.exe":                     "competitive",

    # ── Cinematic / open-world / visual-heavy AAA ──
    "cyberpunk2077.exe":                  "visual",
    "rdr2.exe":                           "visual",
    "redm.exe":                           "visual",
    "horizonzerodawn.exe":                "visual",
    "horizonforbiddenwest.exe":           "visual",
    "starfield.exe":                      "visual",
    "alanwake2.exe":                      "visual",
    "hellblade2.exe":                     "visual",
    "senuasaga-win64-shipping.exe":       "visual",
    "wukong.exe":                         "visual",
    "blackmythwukong.exe":                "visual",
    "spiderman2.exe":                     "visual",
    "marvel-spiderman.exe":               "visual",
    "hogwartslegacy.exe":                 "visual",
    "godofwarragnarok.exe":               "visual",
    "godofwar.exe":                       "visual",
    "lastofuspart1.exe":                  "visual",
    "lastofusii.exe":                     "visual",
    "diablo iv.exe":                      "visual",
    "diabloiv.exe":                       "visual",
    "diablo4.exe":                        "visual",
    "wow.exe":                            "visual",
    "ff7rebirth.exe":                     "visual",
    "ff16.exe":                           "visual",
    "stalker2.exe":                       "visual",
    "stalker2-win64-shipping.exe":        "visual",
    "msfs2024.exe":                       "visual",
    "robloxplayerbeta.exe":               "visual",   # GPU-light, FPS focus
    "minecraft.exe":                      "visual",
    "javaw.exe":                          "visual",   # Minecraft Java
}


# ── Per-game optimization TARGET (beta.13) ──────────────────────────────
# A higher-level intent than `mode`: what is this game FOR?
#   auto        — no override; behave as before (mode-driven AT).
#   max_fps     — GPU-bound title, push raw native FPS: hold the user's
#                 PROVEN saved OC at full GPU power + Ultimate power plan,
#                 and keep AT OFF (its mode caps sit BELOW a tuned user OC,
#                 so letting it manage would only cap the card down).
#   low_latency — latency/frametime-sensitive (competitive shooters):
#                 engage the competitive-latency tweaks for the session;
#                 GPU OC isn't the lever (these titles are CPU-bound).
#   balanced    — explicit "just the normal adaptive behaviour".
# Read by game_profiles.apply_target() at game-start.
VALID_TARGETS = ("auto", "max_fps", "low_latency", "balanced")

# Auto-assigned target for known titles so the right posture applies the
# FIRST time the game is seen — no manual setup.  Spider-Man (Insomniac,
# GPU-bound → raw FPS) → max_fps; Call of Duty (IW engine, CPU-bound →
# fast/consistent inputs) → low_latency.
AUTO_TARGET = {
    # Spider-Man PC ports (Nixxes) — raw native FPS, Frame-Gen left off by user
    "spider-man.exe":       "max_fps",
    "milesmorales.exe":     "max_fps",
    "spider-man2.exe":      "max_fps",
    "marvel-spiderman.exe": "max_fps",
    "spiderman2.exe":       "max_fps",
    # Call of Duty — all IW-engine titles — input timing + frametime
    "modernwarfare.exe":    "low_latency",
    "blackopscoldwar.exe":  "low_latency",
    "vanguard.exe":         "low_latency",
    "cod22-cod.exe":        "low_latency",
    "cod23-cod.exe":        "low_latency",
    "cod.exe":              "low_latency",   # CoD-HQ launcher (BO6 / WZ / MWII+III)
}


def _auto_classify_target(exe: str) -> str:
    return AUTO_TARGET.get((exe or "").lower().strip(), "auto")


def _auto_classify(exe: str) -> str:
    """Pick a default mode for a newly-seen game based on a small built-in
    table of known titles.  Falls back to settings.default_mode."""
    key = (exe or "").lower().strip()
    if key in AUTO_CLASSIFY:
        return AUTO_CLASSIFY[key]
    # Fallback: use the user's configured default
    try:
        return get_settings().get("default_mode", "balanced")
    except Exception:
        return "balanced"


def _preset_for(exe: str) -> dict:
    """Return the active tuning preset for this game.  Handles missing /
    invalid mode values by falling back to balanced."""
    state = get_game_state(exe)
    mode = state.get("mode", "balanced")
    return MODE_PRESETS.get(mode, MODE_PRESETS["balanced"])


# Live state for the currently-active game (if any)
_runtime: dict[str, Any] = {
    "active_exe":      None,
    "active_display":  None,
    "loop_thread":     None,
    "loop_stop":       None,
    "current_core":    0,
    "current_mem":     0,
    "consecutive_good": 0,
    "step_count":      0,
    "started_at":      None,
    "last_score":      None,
    "last_verdict":    None,
    # v3.1 — surfaced in the UI so the user knows WHY AT isn't stepping
    # (e.g. "GPU at 8%, waiting for ≥15%" for light games like Roblox)
    "last_util":       None,
    "last_reason":     None,
    "last_poll_at":    None,
    # v3.2 — frame telemetry + score sub-breakdown for the diagnostics panel
    "last_frame_stats":None,
    "last_score_subs": {},
    "last_temp_c":     0,
    "last_power_pct":  0,
    # v3.2-beta.3 — gameplay-vs-menu classifier surface
    "gameplay_phase":    "unknown",    # gameplay / menu / transitional / baseline-building
    "gameplay_conf":     0.0,
    "gameplay_reasons":  [],
    "gameplay_baseline": {},
}
_lock = threading.Lock()


# ─── Persistence helpers (shared with crash_recovery + temp_ceiling pattern) ─
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load {os.path.basename(path)} ({e}); using default")
        return default


def _save_json(path: str, data) -> bool:
    return atomic_write_json(path, data)


def _norm_exe(exe: str) -> str:
    if not exe:
        return ""
    return os.path.basename(exe).lower()


def _target_process_alive(exe: str) -> bool:
    """Check whether any process matching this exe is currently running.
    Used by the tuning loop to self-clean when game_profiles missed the
    exit event (e.g. AT was engaged via user toggle on a running game
    that game_profiles never picked up)."""
    target = _norm_exe(exe)
    if not target:
        return False
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                name = (p.info.get("name") or "").lower()
            except Exception:
                continue
            if name == target:
                return True
    except Exception as e:
        log.debug(f"_target_process_alive check failed: {e}")
        # Fail open — better to keep the loop running on a transient
        # psutil error than to spuriously self-clean.
        return True
    return False


# ─── v3.1 GPU health probe + pause API ───────────────────────────────────

def _get_gpu_health() -> dict:
    """Read the latest perf_monitor sample for thermal + power gating.
    Returns:
        temp_c       int   — current GPU temperature (0 on failure)
        power_w      float — current GPU power draw
        power_max_w  float — power limit (TDP)
        power_pct    int   — usage as % of TDP
    """
    try:
        from core import perf_monitor
        # v3.2-beta.5 — perf_monitor.get_status() returns the latest
        # sample under the key "last" (not "last_sample" — that was a
        # stale name from an earlier version).  This caused the live
        # row to display "temp 0° pwr 0%" while the diagnostic panel
        # showed the correct readings, because the diagnostic panel
        # read perf_monitor._state directly.
        st = perf_monitor.get_status()
        last = (st or {}).get("last") or {}
        temp = int(last.get("temp") or 0)
        pwr  = float(last.get("power") or 0.0)
    except Exception:
        return {"temp_c": 0, "power_w": 0.0, "power_max_w": 0.0, "power_pct": 0}
    # Pull the TDP from gpu_overclock.capability so we can compute the %.
    try:
        from core import gpu_overclock
        cap = gpu_overclock.detect_gpu_oc_capability()
        max_w = float(((cap or {}).get("limits") or {}).get("power_max_w") or 0.0)
    except Exception:
        max_w = 0.0
    pct = int(round((pwr / max_w) * 100)) if max_w > 0 else 0
    return {"temp_c": temp, "power_w": pwr, "power_max_w": max_w, "power_pct": pct}


def pause(source: str) -> dict:
    """Pause AT step-up/down decisions.  Multiple sources can stack —
    AT only resumes when ALL sources have called resume().  Used by
    recoil_capture (capture sessions) and the latency probe so AT's
    OC writes don't perturb the measurement."""
    src = str(source or "external")
    with _pause_lock:
        _pause_sources.add(src)
        sources = set(_pause_sources)
    log.info(f"AT paused by {src} (active pause sources: {sources})")
    return {"ok": True, "paused": True, "sources": sorted(sources)}


def resume(source: str) -> dict:
    """Remove one pause source.  AT auto-resumes when the set is empty."""
    src = str(source or "external")
    with _pause_lock:
        _pause_sources.discard(src)
        sources = set(_pause_sources)
    log.info(f"AT resume({src}) — remaining sources: {sources}")
    return {"ok": True, "paused": bool(sources), "sources": sorted(sources)}


def is_paused() -> bool:
    with _pause_lock:
        return bool(_pause_sources)


def get_pause_sources() -> list:
    with _pause_lock:
        return sorted(_pause_sources)


# ─── v3.1 Cross-game crash blacklist ─────────────────────────────────────

def _load_cross_game_blacklist() -> list:
    """Read the cross-game blacklist.  Each entry is {core, mem, ts,
    source_exe, reason}.  Returns a list."""
    raw = _load_json(CROSS_GAME_BLACKLIST_PATH, [])
    return raw if isinstance(raw, list) else []


def _save_cross_game_blacklist(entries: list) -> None:
    _save_json(CROSS_GAME_BLACKLIST_PATH, entries[:500])   # cap to last 500


def add_cross_game_blacklist(core: int, mem: int,
                              source_exe: str = "",
                              reason: str = "driver-crash") -> dict:
    """Record (core, mem) as a known-bad offset across ALL games.
    Called by crash_recovery when a driver-level event fires.  De-dupes
    by exact (core, mem) match."""
    entries = _load_cross_game_blacklist()
    if any(int(e.get("core", -1)) == int(core) and int(e.get("mem", -1)) == int(mem)
           for e in entries):
        return {"ok": True, "duplicate": True, "core": core, "mem": mem}
    entries.append({
        "core":       int(core),
        "mem":        int(mem),
        "ts":         time.time(),
        "source_exe": str(source_exe or ""),
        "reason":     str(reason or "driver-crash"),
    })
    _save_cross_game_blacklist(entries)
    log.warning(f"AT cross-game blacklist: core+{core}/mem+{mem} "
                f"(from {source_exe or 'unknown'}, {reason})")
    return {"ok": True, "core": core, "mem": mem, "count": len(entries)}


def get_cross_game_blacklist() -> list:
    return _load_cross_game_blacklist()


def clear_cross_game_blacklist() -> dict:
    _save_cross_game_blacklist([])
    log.info("AT cross-game blacklist cleared")
    return {"ok": True}


def _is_cross_game_blacklisted(core: int, mem: int) -> bool:
    """Exact-match check.  Caller can step PAST a blacklisted offset
    if intermediate offsets are clean — we only block the exact pair."""
    for e in _load_cross_game_blacklist():
        if int(e.get("core", -1)) == int(core) and int(e.get("mem", -1)) == int(mem):
            return True
    return False


# ─── Settings ────────────────────────────────────────────────────────────
def get_settings() -> dict:
    cur = _load_json(SETTINGS_PATH, {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update(cur)
    return merged


def set_settings(**kwargs) -> dict:
    prev = get_settings()
    cur = dict(prev)
    allowed = set(DEFAULT_SETTINGS.keys())
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if isinstance(DEFAULT_SETTINGS[k], bool):
            cur[k] = bool(v)
        elif isinstance(DEFAULT_SETTINGS[k], int):
            try: cur[k] = int(v)
            except (TypeError, ValueError): pass
        elif isinstance(DEFAULT_SETTINGS[k], float):
            try: cur[k] = float(v)
            except (TypeError, ValueError): pass
        else:
            cur[k] = v
    _save_json(SETTINGS_PATH, cur)
    log.info(f"Adaptive Tuning settings updated: {kwargs}")

    # Retroactive engagement — if global was just flipped ON and a game
    # is running with per-game AT enabled, engage now instead of waiting
    # for next launch.
    if not prev.get("enabled_globally") and cur.get("enabled_globally"):
        try:
            r = engage_for_active_game()
            log.info(f"AT post-toggle retroactive engage: {r}")
        except Exception as e:
            log.debug(f"retroactive engage failed: {e}")
    return cur


# ─── Per-game state ──────────────────────────────────────────────────────
def _user_oc_baseline() -> "tuple[int, int]":
    """Read the user's currently-saved manual OC profile from
    gpu_overclock and return (core_offset, mem_offset).  This is the
    starting point for any new AT-tracked game: AT searches OUTWARD
    from where the user has already proven the OC is good, instead of
    crawling up from 0.  Returns (0, 0) when no profile is saved or
    the read fails."""
    try:
        from core import gpu_overclock
        p = gpu_overclock.load_profile()
        if not p or not p.get("exists"):
            return (0, 0)
        return (
            int(p.get("core_offset_mhz") or 0),
            int(p.get("mem_offset_mhz")  or 0),
        )
    except Exception as e:
        log.debug(f"could not read user OC baseline: {e}")
        return (0, 0)


def _empty_state(exe: str) -> dict:
    # v3 — seed the per-game baseline from the user's saved manual OC
    # profile instead of (0, 0).  The user has presumably already tuned
    # their card to a known-good baseline via the GPU OC page; AT's job
    # is to find a SLIGHTLY HIGHER per-game ceiling, not to redo the
    # baseline discovery from scratch.  Without this, every new game
    # took ~10-30 minutes of accumulated playtime crawling back up to
    # the user's own ceiling before it could even start exploring.
    user_core, user_mem = _user_oc_baseline()
    tgt = _auto_classify_target(exe)
    return {
        "exe":               _norm_exe(exe),
        # beta.13 — max_fps/low_latency targets OWN the perf posture, so AT
        # starts OFF for them (their mode caps sit below a tuned OC anyway).
        # Everything else honors the global default-per-game-enabled setting.
        "enabled":           False if tgt in ("max_fps", "low_latency")
                             else bool(get_settings().get("default_per_game_enabled", False)),
        # v3.1 — tuning mode chosen at game-creation time, user-editable.
        # Auto-classified for known titles (CS2 → competitive, Cyberpunk
        # → visual, etc.); falls back to settings.default_mode.
        "mode":              _auto_classify(exe),
        # beta.13 — per-game optimization target (see VALID_TARGETS).
        "target":            tgt,
        # v3 — baselines start at the user's current OC, not 0.
        "current_core":      user_core,
        "current_mem":       user_mem,
        "best_stable_core":  user_core,
        "best_stable_mem":   user_mem,
        # User OC baseline that AT must NEVER step below — this is the
        # user's known-good config and they expect it to stay applied.
        "baseline_core":     user_core,
        "baseline_mem":      user_mem,
        "warned_offsets":    [],   # [{core, mem, ts}]
        "session_count":     0,
        "session_minutes":   0.0,
        "step_count":        0,
        "last_session_at":   None,
        "last_session_outcome": None,    # 'clean' / 'crash' / 'aborted'
        "converged":         False,
        # v3.1 — per-game safety + tuning knobs.
        "thermal_limit_c":   82,        # refuse step-up if temp ≥ this
        "power_bound_pct":   95,        # refuse core step-up if power ≥ this
        "step_multiplier":   1.0,       # 0.5–2.0; scales core_step + mem_step
        "dry_run":           False,     # log decisions but skip apply_oc
        # v3.1 — runtime stats that drive the confidence indicator + the
        # hot-streak adaptive step sizing.  Persist across sessions so
        # the user keeps "AT is confident this OC is stable" badges across
        # restarts.
        "hot_streak":        0,         # consecutive step-ups since last step-down
        "stable_sessions":   0,         # crash-free sessions at current offset
        "stable_minutes":    0.0,       # cumulative minutes at current offset
        "confidence":        "low",     # low / medium / high
        "last_step_at":      0.0,       # timestamp of last up/down decision
        "last_step_was_up":  False,
        "last_step_offset":  {"core": user_core, "mem": user_mem},  # for auto-revert
        # v3.1 — opening-phase deadline.  When > now(), the tuning loop
        # uses halved warmup + consecutive_good so AT explores aggressively
        # during the first 5 min of a fresh session.  Set by _engage().
        "opening_phase_until": 0.0,
    }


def _migrate_state(state: dict, exe: str) -> dict:
    """Backfill any new fields onto an older state row.  Called on every
    load so v1 → v3.1 upgrades silently get the mode + baseline + new
    safety/confidence fields set."""
    if "mode" not in state or state["mode"] not in MODE_PRESETS:
        state["mode"] = _auto_classify(exe)
    # beta.13 — backfill the optimization target (auto-classify known games
    # so an existing profile for Spider-Man / CoD picks up the right posture).
    if "target" not in state or state["target"] not in VALID_TARGETS:
        state["target"] = _auto_classify_target(state.get("exe", exe))
    # v3 — older rows pre-date the baseline_* fields.  Pull the user's
    # current OC profile as the baseline.  Also lift current_/best_stable_
    # up to the baseline if they're below it (the user has clearly
    # tuned the baseline since the row was created).
    if "baseline_core" not in state or "baseline_mem" not in state:
        bc, bm = _user_oc_baseline()
        state["baseline_core"] = bc
        state["baseline_mem"]  = bm
    bc = int(state.get("baseline_core", 0))
    bm = int(state.get("baseline_mem",  0))
    if int(state.get("current_core", 0)) < bc:
        state["current_core"] = bc
    if int(state.get("current_mem", 0))  < bm:
        state["current_mem"]  = bm
    if int(state.get("best_stable_core", 0)) < bc:
        state["best_stable_core"] = bc
    if int(state.get("best_stable_mem", 0))  < bm:
        state["best_stable_mem"]  = bm
    # v3.1 — backfill safety + tuning knobs + confidence runtime
    state.setdefault("thermal_limit_c",  82)
    state.setdefault("power_bound_pct",  95)
    state.setdefault("step_multiplier",  1.0)
    state.setdefault("dry_run",          False)
    state.setdefault("hot_streak",       0)
    state.setdefault("stable_sessions",  0)
    state.setdefault("stable_minutes",   0.0)
    state.setdefault("confidence",       "low")
    state.setdefault("last_step_at",     0.0)
    state.setdefault("last_step_was_up", False)
    state.setdefault("last_step_offset",
                     {"core": int(state.get("current_core", 0)),
                      "mem":  int(state.get("current_mem",  0))})
    state.setdefault("opening_phase_until", 0.0)
    return state


def get_game_state(exe: Optional[str] = None):
    states = _load_json(STATE_PATH, {})
    if exe is None:
        # Migrate every row on bulk-read so the UI sees complete data
        for k, v in states.items():
            _migrate_state(v, k)
        return states
    key = _norm_exe(exe)
    if key in states:
        return _migrate_state(states[key], key)
    return _empty_state(exe)


def _save_game_state(exe: str, state: dict) -> None:
    states = _load_json(STATE_PATH, {})
    states[_norm_exe(exe)] = state
    _save_json(STATE_PATH, states)


def set_game_enabled(exe: str, enabled: bool) -> dict:
    states = _load_json(STATE_PATH, {})
    key = _norm_exe(exe)
    was_enabled = bool(states.get(key, {}).get("enabled", False))
    if key not in states:
        states[key] = _empty_state(exe)
    states[key]["enabled"] = bool(enabled)
    _save_json(STATE_PATH, states)
    log.info(f"Adaptive Tuning {'enabled' if enabled else 'disabled'} for {key}")

    # Retroactive engagement: if just flipped ON and this game is running
    if enabled and not was_enabled:
        try:
            from core import game_profiles
            active = game_profiles.get_active_game_info()
            if active and _norm_exe(active.get("exe", "")) == key:
                r = engage_for_active_game()
                log.info(f"AT post-game-toggle engage: {r}")
        except Exception as e:
            log.debug(f"retroactive engage failed: {e}")
    return states[key]


def reseed_from_baseline(exe: Optional[str] = None) -> dict:
    """v3 — re-seed game(s) to the user's current OC profile as the
    new baseline, without wiping session count / step history / warned
    list.  Useful when:
      * The user tunes their manual OC to a higher baseline and wants
        AT to start exploring from there instead of the old lower base.
      * AT has wandered to an OC that proved unreliable and the user
        wants to reset the search starting point without losing the
        accumulated stability data.

    Sets `current_*` and `best_stable_*` and `baseline_*` all to the
    current user OC.  Pass exe=None to reseed every tracked game."""
    user_core, user_mem = _user_oc_baseline()
    states = _load_json(STATE_PATH, {})
    if exe is None:
        # Reseed every game
        for key, s in states.items():
            s["baseline_core"]    = user_core
            s["baseline_mem"]     = user_mem
            s["current_core"]     = user_core
            s["current_mem"]      = user_mem
            s["best_stable_core"] = user_core
            s["best_stable_mem"]  = user_mem
            s["converged"]        = False
        _save_json(STATE_PATH, states)
        log.info(f"AT reseeded ALL games to baseline core+{user_core}/mem+{user_mem}")
        return {"ok": True, "reseeded": "all",
                "baseline_core": user_core, "baseline_mem": user_mem}
    key = _norm_exe(exe)
    if key not in states:
        states[key] = _empty_state(exe)
    s = states[key]
    s["baseline_core"]    = user_core
    s["baseline_mem"]     = user_mem
    s["current_core"]     = user_core
    s["current_mem"]      = user_mem
    s["best_stable_core"] = user_core
    s["best_stable_mem"]  = user_mem
    s["converged"]        = False
    _save_json(STATE_PATH, states)
    log.info(f"AT reseeded {key} to baseline core+{user_core}/mem+{user_mem}")
    return {"ok": True, "reseeded": key,
            "baseline_core": user_core, "baseline_mem": user_mem}


def reset_game_state(exe: Optional[str] = None) -> dict:
    states = _load_json(STATE_PATH, {})
    if exe is None:
        _save_json(STATE_PATH, {})
        return {"ok": True, "cleared": "all"}
    key = _norm_exe(exe)
    if key in states:
        del states[key]
        _save_json(STATE_PATH, states)
    return {"ok": True, "cleared": key}


def set_game_mode(exe: str, mode: str) -> dict:
    """Set the tuning mode for one game.  Valid modes:
       'balanced' / 'competitive' / 'visual'.  If AT is currently engaged
       for this game, the loop picks up the new preset on its next poll
       — no game restart required."""
    if mode not in MODE_PRESETS:
        return {"ok": False, "err": f"invalid mode '{mode}'",
                "valid": list(MODE_PRESETS.keys())}
    states = _load_json(STATE_PATH, {})
    key = _norm_exe(exe)
    if key not in states:
        states[key] = _empty_state(exe)
    states[key]["mode"] = mode
    _save_json(STATE_PATH, states)
    log.info(f"AT mode set: {key} → {mode}")
    # Reset the consecutive-good counter so a stricter mode doesn't
    # accidentally inherit progress from a more lenient one.
    with _lock:
        if _runtime.get("active_exe") == key:
            _runtime["consecutive_good"] = 0
    return {"ok": True, "exe": key, "mode": mode}


def set_game_target(exe: str, target: str) -> dict:
    """Set the per-game optimization TARGET (beta.13).  Valid:
       auto | max_fps | low_latency | balanced.  Read by
       game_profiles.apply_target() at game-start to compose the right
       posture (max_fps → hold proven OC + full GPU power + Ultimate plan;
       low_latency → competitive-latency tweaks for the session).

       max_fps/low_latency also switch AT OFF for this game: the target
       owns the GPU/latency posture, and AT's mode caps sit below a tuned
       OC (so it could only cap a max_fps card down)."""
    if target not in VALID_TARGETS:
        return {"ok": False, "err": f"invalid target '{target}'",
                "valid": list(VALID_TARGETS)}
    states = _load_json(STATE_PATH, {})
    key = _norm_exe(exe)
    if key not in states:
        states[key] = _empty_state(exe)
    states[key]["target"] = target
    if target in ("max_fps", "low_latency"):
        states[key]["enabled"] = False
    _save_json(STATE_PATH, states)
    log.info(f"AT target set: {key} → {target}"
             + (" (AT auto-disabled — target owns the posture)"
                if target in ("max_fps", "low_latency") else ""))
    return {"ok": True, "exe": key, "target": target,
            "at_enabled": bool(states[key]["enabled"])}


def set_game_knobs(exe: str,
                   thermal_limit_c: Optional[float]  = None,
                   power_bound_pct: Optional[float]  = None,
                   step_multiplier: Optional[float]  = None,
                   dry_run:         Optional[bool]   = None) -> dict:
    """Per-game advanced knobs (v3.1).  Any None field is left unchanged.

      • thermal_limit_c (60-95)  — AT will skip step-ups when GPU temp
                                   is at or above this in Celsius.
      • power_bound_pct (50-100) — AT will skip *core* step-ups when GPU
                                   power draw % of TDP is at or above this.
                                   (Mem step-ups are unaffected — they
                                   barely move power.)
      • step_multiplier (0.5-2.0)— global scaler on the per-game step
                                   size.  Hot-streak multiplier still
                                   compounds on top of this.
      • dry_run (bool)           — when True, AT runs all of its logic
                                   but doesn't actually apply OC changes
                                   to the GPU.  History still records
                                   what it WOULD have done — useful for
                                   verifying AT behaviour on a new game
                                   without risking a crash."""
    states = _load_json(STATE_PATH, {})
    key = _norm_exe(exe)
    if key not in states:
        states[key] = _empty_state(exe)
    st = states[key]
    changes = {}
    if thermal_limit_c is not None:
        try:
            v = max(60.0, min(95.0, float(thermal_limit_c)))
        except (TypeError, ValueError):
            return {"ok": False, "err": "thermal_limit_c must be numeric"}
        st["thermal_limit_c"] = v
        changes["thermal_limit_c"] = v
    if power_bound_pct is not None:
        try:
            v = max(50.0, min(100.0, float(power_bound_pct)))
        except (TypeError, ValueError):
            return {"ok": False, "err": "power_bound_pct must be numeric"}
        st["power_bound_pct"] = v
        changes["power_bound_pct"] = v
    if step_multiplier is not None:
        try:
            v = max(0.5, min(2.0, float(step_multiplier)))
        except (TypeError, ValueError):
            return {"ok": False, "err": "step_multiplier must be numeric"}
        st["step_multiplier"] = v
        changes["step_multiplier"] = v
    if dry_run is not None:
        st["dry_run"] = bool(dry_run)
        changes["dry_run"] = bool(dry_run)
    _save_json(STATE_PATH, states)
    if changes:
        log.info(f"AT knobs set: {key} → {changes}")
    return {"ok": True, "exe": key, "changes": changes,
            "thermal_limit_c": st.get("thermal_limit_c"),
            "power_bound_pct": st.get("power_bound_pct"),
            "step_multiplier": st.get("step_multiplier"),
            "dry_run":         st.get("dry_run", False)}


def apply_best_stable(exe: str) -> dict:
    """Manually push this game's `best_stable_core` / `best_stable_mem`
    offsets to the GPU + saved profile.  Useful for:
      • previewing the learned profile before a game session
      • re-applying after a manual reset
      • applying without waiting for the game to launch"""
    state = get_game_state(exe)
    bc = int(state.get("best_stable_core", 0))
    bm = int(state.get("best_stable_mem", 0))
    if bc == 0 and bm == 0:
        return {"ok": False, "err": "no best-stable offsets recorded yet "
                                    "(this game hasn't completed a session)"}
    ok = _apply_offsets(exe, bc, bm, "manual-apply-best")
    return {"ok": ok, "core": bc, "mem": bm}


def clear_blacklist_only(exe: Optional[str] = None) -> dict:
    """Clear the per-game crash blacklist + warned_offsets list, but
    KEEP the best_stable / current_core / current_mem / session counts.

    Useful after a driver update — the old crashed offsets may now be
    stable on the new driver, so it's worth letting AT try them again
    without losing all the learned-good state.  This is a less-nuclear
    option than `reset_game_state`."""
    # Clear per-game warned_offsets in our state file
    states = _load_json(STATE_PATH, {})
    if exe is None:
        for key in states:
            states[key]["warned_offsets"] = []
        cleared_at = "all"
    else:
        key = _norm_exe(exe)
        if key in states:
            states[key]["warned_offsets"] = []
            cleared_at = key
        else:
            cleared_at = None
    _save_json(STATE_PATH, states)

    # Clear the crash_recovery blacklist for this game (or all)
    try:
        from core import crash_recovery
        cr = crash_recovery.clear_blacklist(exe)
    except Exception as e:
        cr = {"ok": False, "err": str(e)}

    return {
        "ok":            True,
        "warned_cleared": cleared_at,
        "blacklist":      cr,
    }


# ─── History ─────────────────────────────────────────────────────────────
def get_history(limit: int = 50) -> list:
    h = _load_json(HISTORY_PATH, [])
    if not isinstance(h, list):
        return []
    return list(h[-limit:])


def _append_history(entry: dict) -> None:
    h = _load_json(HISTORY_PATH, [])
    if not isinstance(h, list):
        h = []
    h.append(entry)
    _save_json(HISTORY_PATH, h[-200:])


# ─── Status (for UI) ─────────────────────────────────────────────────────
def clear_runtime_if_orphaned() -> dict:
    """One-shot orphan check.  If the runtime is tracking an exe whose
    process no longer exists, wipe the runtime back to idle.  Called
    from app startup so a previous-run stale state can't survive a
    GhostShell restart."""
    with _lock:
        cur_exe = _runtime.get("active_exe")
    if not cur_exe:
        return {"orphan": False}
    if _target_process_alive(cur_exe):
        return {"orphan": False, "active_exe": cur_exe}
    log.warning(f"AT orphan detected: runtime says '{cur_exe}' is active but "
                f"no such process is running — clearing runtime")
    with _lock:
        _runtime["active_exe"]      = None
        _runtime["active_display"]  = None
        _runtime["loop_thread"]     = None
        _runtime["loop_stop"]       = None
        _runtime["current_core"]    = 0
        _runtime["current_mem"]     = 0
        _runtime["consecutive_good"] = 0
        _runtime["step_count"]      = 0
        _runtime["started_at"]      = None
        _runtime["last_score"]      = None
        _runtime["last_verdict"]    = None
    return {"orphan": True, "cleared_exe": cur_exe}


def get_status() -> dict:
    with _lock:
        st = dict(_runtime)
    s = get_settings()

    # Pull the active game's mode + preset (so the UI can show
    # mode-specific thresholds + cap info)
    active_mode = None
    active_preset: dict = {}
    active_state: dict = {}
    if st["active_exe"]:
        active_mode = _get_mode(st["active_exe"])
        active_preset = MODE_PRESETS.get(active_mode, MODE_PRESETS["balanced"])
        active_state  = get_game_state(st["active_exe"])

    # Phase: 'idle' / 'warmup' / 'tuning' / 'paused' / 'opening'.  UI
    # uses this to show the right messaging — "warming up (45/60s)" vs
    # "GPU at 8%, waiting" vs "paused for capture session".
    phase = "idle"
    seconds_in_session = None
    warmup_remaining = None
    if st["started_at"]:
        seconds_in_session = round(time.time() - st["started_at"], 1)
        # v3.1 — opening phase uses reduced warmup
        eff_warmup, _ = _effective_thresholds(active_preset or MODE_PRESETS["balanced"],
                                              active_state or {})
        if seconds_in_session < eff_warmup:
            phase = "warmup"
            warmup_remaining = round(eff_warmup - seconds_in_session, 1)
        elif is_paused():
            phase = "paused"
        elif _in_opening_phase(active_state or {}):
            phase = "opening"
        else:
            phase = "tuning"

    # v3.1 — live GPU health snapshot, useful for the UI's badge
    health = _get_gpu_health()
    # beta.14 — PresentMon-2 install hint.  AT prefers PM2 for frame
    # telemetry (more accurate frametime stats than PM1); when only
    # PM1 is available we surface a one-time nudge to the UI.  Flag
    # stays True every poll while AT is engaged + PM2 missing — JS
    # de-dupes via toast rate limiter so the user only sees it once.
    pm2_nudge: dict = {"needs_pm2": False}
    if st.get("active_exe"):
        try:
            from core import frame_telemetry
            pm2 = frame_telemetry._detect_pm2_service() or {}
            if not pm2.get("installed"):
                pm2_nudge = {
                    "needs_pm2": True,
                    "url": "https://github.com/GameTechDev/PresentMon/releases/latest",
                    "msg": ("AT is running on PresentMon 1.x.  Install PresentMon 2 "
                            "from Intel/GameTechDev (free) for better frametime accuracy."),
                }
        except Exception as _e:
            log.debug(f"pm2 nudge probe failed: {_e}")
    return {
        "settings":           s,
        "modes":              list(MODE_PRESETS.keys()),
        "mode_presets":       MODE_PRESETS,
        "active_exe":         st["active_exe"],
        "active_display":     st["active_display"],
        "active_mode":        active_mode,
        "active_preset":      active_preset,
        "current_core":       st["current_core"],
        "current_mem":        st["current_mem"],
        "consecutive_good":   st["consecutive_good"],
        "step_count":         st["step_count"],
        "started_at":         st["started_at"],
        "last_score":         st["last_score"],
        "last_verdict":       st["last_verdict"],
        "last_util":          st["last_util"],
        "last_reason":        st["last_reason"],
        "last_poll_at":       st["last_poll_at"],
        "phase":              phase,
        "seconds_in_session": seconds_in_session,
        "warmup_remaining":   warmup_remaining,
        "loop_running":       bool(st["loop_thread"] and st["loop_thread"].is_alive()),
        # v3.1 additions
        "paused":             is_paused(),
        "pause_sources":      get_pause_sources(),
        "gpu_health":         health,
        "pm2_install_hint":   pm2_nudge,
        "hot_streak":         int((active_state or {}).get("hot_streak", 0)),
        "confidence":         (active_state or {}).get("confidence", "low"),
        "stable_sessions":    int((active_state or {}).get("stable_sessions", 0)),
        "stable_minutes":     float((active_state or {}).get("stable_minutes", 0.0)),
        "baseline_core":      int((active_state or {}).get("baseline_core", 0)),
        "baseline_mem":       int((active_state or {}).get("baseline_mem", 0)),
        "in_opening_phase":   _in_opening_phase(active_state or {}),
        "dry_run":            bool((active_state or {}).get("dry_run", False)),
        "step_multiplier":    float((active_state or {}).get("step_multiplier", 1.0)),
        "thermal_limit_c":    int((active_state or {}).get("thermal_limit_c", 82)),
        "power_bound_pct":    int((active_state or {}).get("power_bound_pct", 95)),
        # v3.2 — frame telemetry surface for the AT card live row
        "frame_stats":        st.get("last_frame_stats"),
        "score_subs":         st.get("last_score_subs") or {},
        # v3.2-beta.3 — gameplay-vs-menu classifier
        "gameplay_phase":     st.get("gameplay_phase", "unknown"),
        "gameplay_conf":      st.get("gameplay_conf", 0.0),
        "gameplay_reasons":   st.get("gameplay_reasons", []),
        "gameplay_baseline":  st.get("gameplay_baseline", {}),
    }


def get_diagnostics() -> dict:
    """Comprehensive AT diagnostic dump — what is AT seeing right now,
    and why is it (not) acting.  Powers the AT diagnostic panel in the
    UI so users don't have to guess whether AT is engaged / why scores
    are None / whether frame telemetry is reaching it.

    This is the answer to "I have AT on but nothing happens during games"
    — it surfaces every reason AT might be inactive in one structured
    response."""
    s = get_settings()
    with _lock:
        rt = dict(_runtime)

    # Active game from game_profiles (what AT *should* be tracking)
    foreground = None
    fg_known   = False
    fg_state   = None
    try:
        from core import game_profiles
        info = game_profiles.get_active_game_info()
        if info:
            foreground = info
            ex = (info.get("exe") or "").lower()
            fg_known = ex in game_profiles.KNOWN_GAME_EXES
            states_all = get_game_state()
            fg_state = states_all.get(_norm_exe(ex))
    except Exception as e:
        log.debug(f"diag: game_profiles read failed: {e}")

    # perf_monitor health
    pm_status = {"running": False, "samples": 0, "last_util": None}
    try:
        from core import perf_monitor
        pm_status["running"] = bool(perf_monitor._state.get("running"))
        pm_status["samples"] = len(perf_monitor._state.get("samples", []))
        ls = perf_monitor._state.get("last_sample") or {}
        pm_status["last_util"] = ls.get("util")
        pm_status["last_core_mhz"] = ls.get("core")
        pm_status["last_temp"]     = ls.get("temp")
    except Exception as e:
        pm_status["err"] = str(e)

    # frame_telemetry status
    ft_status = {"available": False}
    try:
        from core import frame_telemetry
        ft_status = frame_telemetry.get_status()
    except Exception as e:
        ft_status["err"] = str(e)

    # Diagnose the engagement gate — list every reason AT might NOT be acting
    blockers = []
    if not s.get("enabled_globally"):
        blockers.append({
            "level": "error",
            "code":  "global_off",
            "msg":   "AT master switch is OFF (Adaptive Tuning toggle).",
            "fix":   "Turn on the master switch at the top of the AT card.",
        })
    if foreground is None:
        blockers.append({
            "level": "info",
            "code":  "no_foreground_game",
            "msg":   "No game detected in the foreground.",
            "fix":   "Launch a game (windowed/borderless) — GhostShell scans the foreground window every 3 s.",
        })
    elif not fg_known:
        blockers.append({
            "level": "warn",
            "code":  "unknown_game",
            "msg":   f"{foreground.get('exe')} is not in GhostShell's known-games list.",
            "fix":   "Click 'Track this game' (or add it via Game Profiles → Custom Games) so AT can engage on it.",
        })
    elif fg_state and not fg_state.get("enabled"):
        blockers.append({
            "level": "warn",
            "code":  "per_game_off",
            "msg":   f"AT is disabled for {foreground.get('exe')} specifically.",
            "fix":   "Toggle the switch on this game's row in the AT card.",
        })
    if is_paused():
        blockers.append({
            "level": "info",
            "code":  "paused",
            "msg":   f"AT is currently paused — sources: {', '.join(get_pause_sources()) or 'unknown'}",
            "fix":   "Click 'Resume AT' once the active capture/measurement is done.",
        })

    # Engagement state breakdown
    engaged = rt.get("active_exe") is not None
    seconds_in = (time.time() - rt["started_at"]) if rt.get("started_at") else None

    # What does AT need next?
    next_action = None
    if engaged and seconds_in is not None and not is_paused():
        active_state = get_game_state(rt["active_exe"])
        eff_warmup, eff_cg = _effective_thresholds(
            MODE_PRESETS.get(_get_mode(rt["active_exe"]), MODE_PRESETS["balanced"]),
            active_state,
        )
        if seconds_in < eff_warmup:
            next_action = {
                "phase":     "warmup",
                "remaining": round(eff_warmup - seconds_in, 1),
                "msg":       f"In warmup — {round(eff_warmup - seconds_in, 1)} s left "
                             f"before AT starts scoring stability.",
            }
        elif rt.get("last_score") is None:
            next_action = {
                "phase":  "waiting-for-load",
                "msg":    rt.get("last_reason") or "Waiting for ≥15% sustained GPU load.",
            }
        else:
            cg = rt.get("consecutive_good", 0)
            next_action = {
                "phase":         "tuning",
                "consecutive_good": cg,
                "consecutive_required": eff_cg,
                "msg": f"Score {rt.get('last_score')} ({rt.get('last_verdict')}) — "
                       f"need {eff_cg} consecutive 'healthy' polls to step up "
                       f"({cg}/{eff_cg} so far).",
            }

    return {
        "settings":       s,
        "engaged":        engaged,
        "blockers":       blockers,
        "foreground":     foreground,
        "foreground_known": fg_known,
        "perf_monitor":   pm_status,
        "frame_telemetry": ft_status,
        "runtime":        {
            "active_exe":     rt.get("active_exe"),
            "active_display": rt.get("active_display"),
            "current_core":   rt.get("current_core"),
            "current_mem":    rt.get("current_mem"),
            "consecutive_good": rt.get("consecutive_good"),
            "step_count":     rt.get("step_count"),
            "started_at":     rt.get("started_at"),
            "seconds_in_session": round(seconds_in, 1) if seconds_in else None,
            "last_score":     rt.get("last_score"),
            "last_verdict":   rt.get("last_verdict"),
            "last_util":      rt.get("last_util"),
            "last_temp_c":    rt.get("last_temp_c"),
            "last_power_pct": rt.get("last_power_pct"),
            "last_reason":    rt.get("last_reason"),
            "last_poll_at":   rt.get("last_poll_at"),
            "last_frame_stats": rt.get("last_frame_stats"),
            "last_score_subs":  rt.get("last_score_subs"),
            "gameplay_phase":    rt.get("gameplay_phase"),
            "gameplay_conf":     rt.get("gameplay_conf"),
            "gameplay_reasons":  rt.get("gameplay_reasons"),
            "gameplay_baseline": rt.get("gameplay_baseline"),
        },
        "next_action":    next_action,
        "paused":         is_paused(),
        "pause_sources":  get_pause_sources(),
    }


# ─── Step decision logic ─────────────────────────────────────────────────
def _is_safe_to_step_to(exe: str, core: int, mem: int,
                         axis: str = "either") -> tuple[bool, str]:
    """Returns (safe, reason).  Checks the per-game mode caps + the
    per-game crash blacklist + the cross-game blacklist + GPU health
    gates (thermal + power-bound) before letting the loop apply a new
    offset.

    `axis` is "core" / "mem" / "either" — used by the power-bound check
    so a power-saturated GPU still lets memory step-ups through (memory
    OC doesn't draw materially more power on most cards).
    """
    p = _preset_for(exe)
    state = get_game_state(exe)
    if core > p["max_core_offset"]:
        return False, f"core+{core} exceeds {_get_mode(exe)}-mode cap ({p['max_core_offset']})"
    if mem > p["max_mem_offset"]:
        return False, f"mem+{mem} exceeds {_get_mode(exe)}-mode cap ({p['max_mem_offset']})"
    # Per-game crash blacklist (managed by crash_recovery)
    try:
        from core import crash_recovery
        if crash_recovery.is_offset_blacklisted(exe, core, mem):
            return False, f"blacklisted (crashed previously at ≤ this offset)"
    except Exception as e:
        log.debug(f"per-game blacklist check failed: {e}")
    # v3.1 — cross-game blacklist (driver-level crashes recorded
    # against the exact offset in any game).
    try:
        if _is_cross_game_blacklisted(core, mem):
            return False, f"cross-game blacklist hit (core+{core}/mem+{mem} crashed driver elsewhere)"
    except Exception as e:
        log.debug(f"cross-game blacklist check failed: {e}")
    # v3.1 — thermal gate.  Refuse step-up when GPU temp is within
    # thermal-throttle range; new clocks need headroom to be tested
    # under sustained load.
    health = _get_gpu_health()
    thermal_limit = int(state.get("thermal_limit_c", 82))
    if health["temp_c"] > 0 and health["temp_c"] >= thermal_limit:
        return False, f"thermal: {health['temp_c']}°C ≥ {thermal_limit}°C limit"
    # v3.1 — power-bound: if GPU is at >= power_bound_pct of TDP, a core
    # step-up just causes throttling.  Memory step-ups are still allowed
    # (memory OC doesn't move the power needle materially on most cards).
    power_bound = int(state.get("power_bound_pct", 95))
    if (axis == "core" and health["power_pct"] > 0
            and health["power_pct"] >= power_bound):
        return False, (f"power-bound: {health['power_pct']}% of "
                       f"{int(health['power_max_w'])}W ≥ {power_bound}% "
                       "— additional core would throttle")
    return True, "ok"


def _get_mode(exe: str) -> str:
    return get_game_state(exe).get("mode", "balanced")


# ─── v3.1 step-size + phase helpers ──────────────────────────────────────

def _effective_step(state: dict, preset: dict, axis: str) -> int:
    """Compute the actual step size for this decision, factoring in:
       1. Per-game step_multiplier (0.5–2.0)
       2. Hot-streak (3+ successful step-ups in a row → 2× temporarily)

    Hot-streak rationale: once AT has demonstrated several stable
    step-ups in this session, we have evidence the card is comfortable
    well above its current operating point; doubling the step lets us
    close on the real ceiling 2× faster.  A step-down resets the streak,
    so we never overshoot more than one big-step level deep.
    """
    base = int(preset["core_step_mhz" if axis == "core" else "mem_step_mhz"])
    mult = float(state.get("step_multiplier", 1.0) or 1.0)
    mult = max(0.5, min(2.0, mult))
    step = max(1, int(round(base * mult)))
    if int(state.get("hot_streak", 0)) >= HOT_STREAK_THRESHOLD:
        step *= HOT_STREAK_MULTIPLIER
    return step


def _in_opening_phase(state: dict) -> bool:
    """True for the first OPENING_PHASE_SEC after engage, during which
    thresholds are relaxed so AT closes on the rough ceiling quickly."""
    return time.time() < float(state.get("opening_phase_until", 0.0) or 0.0)


def _effective_thresholds(preset: dict, state: dict) -> "tuple[int, int]":
    """Returns (warmup_seconds, consecutive_good_required) after applying
    opening-phase reductions if we're still in that window."""
    warmup       = int(preset["warmup_seconds"])
    consecutive  = int(preset["consecutive_good_required"])
    if _in_opening_phase(state):
        warmup      = max(15, int(warmup * OPENING_PHASE_WARMUP_RATIO))
        consecutive = max(2,  int(consecutive * OPENING_PHASE_GOOD_RATIO))
    return warmup, consecutive


def _update_confidence(state: dict) -> str:
    """Recompute the confidence tier from stable_sessions + stable_minutes.
    Mutates state['confidence'] and returns the new tier."""
    sess = int(state.get("stable_sessions", 0))
    mins = float(state.get("stable_minutes", 0.0))
    tier = "low"
    if (sess >= CONFIDENCE_TIERS["high"]["sessions"]
            or mins >= CONFIDENCE_TIERS["high"]["minutes"]):
        tier = "high"
    elif (sess >= CONFIDENCE_TIERS["medium"]["sessions"]
            or mins >= CONFIDENCE_TIERS["medium"]["minutes"]):
        tier = "medium"
    state["confidence"] = tier
    return tier


def _detect_auto_revert(exe: str, state: dict) -> "tuple[int, int] | None":
    """If a crash was recorded within AUTO_REVERT_WINDOW_SEC of the last
    step-up to the current offset, return the offset to revert TO
    (the pre-step value).  Otherwise None.

    Called once at engage time so a crashed game's next launch starts
    one step BELOW the offset that crashed it, without waiting for the
    score-based step-down to figure it out."""
    if not state.get("last_step_was_up"):
        return None
    last_at = float(state.get("last_step_at", 0.0))
    if last_at <= 0 or (time.time() - last_at) > AUTO_REVERT_WINDOW_SEC:
        return None
    try:
        from core import crash_recovery
        recent = crash_recovery.get_recent_crashes(exe=exe,
                                                   within_sec=AUTO_REVERT_WINDOW_SEC)
    except Exception:
        recent = []
    # Did any crash happen AFTER the last step-up?
    if not any(float(c.get("ts", 0)) >= last_at for c in (recent or [])):
        return None
    prev = state.get("last_step_offset") or {}
    pc = int(prev.get("core", state.get("baseline_core", 0)))
    pm = int(prev.get("mem",  state.get("baseline_mem",  0)))
    # Clamp at baseline so we never revert below user's manual OC
    bc = int(state.get("baseline_core", 0))
    bm = int(state.get("baseline_mem",  0))
    return (max(bc, pc), max(bm, pm))


_AT_DRIFT_TOLERANCE_MHZ = 5    # offsets are integer MHz; 5 absorbs rounding


def _at_check_offset_drift(exe: str, target_core: int, target_mem: int) -> dict:
    """beta.14 — AT-owned drift check.  Called by the tuning loop right
    before it reads the latest stability score, so step decisions are
    always based on a confirmed-applied offset.  Replaces the stock
    `_oc_watchdog_loop` for the duration of an AT session (the watchdog
    is paused via gpu_overclock.pause_oc_watchdog in _engage).

    Compares the NVAPI-reported applied offset to what AT thinks it
    last wrote.  If a third party (Afterburner, the user) overwrote
    AT's value, re-asserts it via nvapi.set_offsets so the next poll
    measures a known offset.  Logs at INFO level when re-asserting so
    the user can see why the score might temporarily spike.

    Returns {checked, drifted, reapplied, actual_core, actual_mem}.
    Best-effort — any NVAPI exception is caught and the loop continues
    (the next iteration will retry the check)."""
    out = {"checked": False, "drifted": False, "reapplied": False,
           "actual_core": 0, "actual_mem": 0}
    try:
        from core import gpu_overclock as _gp
        if _gp._nvapi is None or not _gp._nvapi.is_available().get("ok"):
            return out
        cur = _gp._nvapi.get_current_offsets()
        if not cur.get("ok"):
            return out
        actual_core = int(cur.get("core_offset_mhz", 0))
        actual_mem  = int(cur.get("mem_offset_mhz",  0))
        out.update({"checked": True, "actual_core": actual_core,
                     "actual_mem": actual_mem})
        core_off_drift = actual_core - int(target_core)
        mem_off_drift  = actual_mem  - int(target_mem)
        drifted = (abs(core_off_drift) > _AT_DRIFT_TOLERANCE_MHZ
                   or abs(mem_off_drift) > _AT_DRIFT_TOLERANCE_MHZ)
        if drifted:
            out["drifted"] = True
            log.info(
                f"AT drift detected ({exe}): core +{actual_core} vs "
                f"+{int(target_core)} (Δ {core_off_drift:+d}), mem "
                f"+{actual_mem} vs +{int(target_mem)} (Δ {mem_off_drift:+d}). "
                f"Re-asserting AT's offsets."
            )
            try:
                _gp._nvapi.set_offsets(core_offset_mhz=int(target_core),
                                       mem_offset_mhz=int(target_mem))
                out["reapplied"] = True
            except Exception as e:
                log.debug(f"AT drift re-assert raised: {e}")
        return out
    except Exception as e:
        log.debug(f"_at_check_offset_drift raised: {e}")
        return out


def _apply_offsets(exe: str, core: int, mem: int, reason: str) -> bool:
    """Push offsets to the GPU + update saved profile + log to history.
    Returns True iff the apply was reported successful by gpu_overclock.

    v3.1 — respects per-game `dry_run`: when True, logs the decision
    but skips the actual GPU write + profile save.  Useful for nervous
    users who want to see what AT would do without risking OC during
    a high-stakes match."""
    # v3.1 — dry-run short-circuit
    try:
        st = get_game_state(exe)
        if bool(st.get("dry_run")):
            log.info(f"AT [DRY-RUN] would apply core+{core}/mem+{mem} ({reason})")
            return True
    except Exception: pass
    try:
        from core import gpu_overclock
        # Check temp ceiling state — if we're in cooldown after a trip,
        # don't try to push the OC up.  (The temp ceiling already reset
        # us to stock; AT shouldn't fight it.)
        try:
            from core import temp_ceiling
            tc = temp_ceiling.get_status()
            if tc.get("cooldown_remaining_s") is not None:
                log.warning(f"AT skipping apply — temp_ceiling in cooldown for "
                            f"{tc['cooldown_remaining_s']}s")
                return False
        except Exception:
            pass

        result = gpu_overclock.apply_oc(
            core_offset_mhz=core,
            mem_offset_mhz=mem,
            power_pct=100,
        )
        ok = bool(result.get("ok"))
        if ok:
            # Persist to the saved-profile slot too so subsequent boots /
            # manual checks see what AT picked.
            gpu_overclock.save_profile({
                "core_offset_mhz": core,
                "mem_offset_mhz":  mem,
                "power_pct":       100,
                "apply_on_startup": True,
            }, auto=True)
        log.info(f"AT apply core+{core}/mem+{mem} → {'OK' if ok else 'FAIL'} ({reason})")
        return ok
    except Exception as e:
        log.error(f"AT apply failed: {e}")
        return False


# ─── Tuning loop ─────────────────────────────────────────────────────────
def _tuning_loop(exe: str, display_name: str, stop_event: threading.Event) -> None:
    """Per-session loop.  Sleeps WARMUP, then evaluates the stability
    score every POLL seconds and decides whether to step up, step down,
    or hold.  Reads tuning parameters from the per-game MODE preset
    (balanced / competitive / visual) — not from global settings — so
    a competitive title is tuned conservatively even on a system where
    the default mode is visual."""
    p = _preset_for(exe)
    mode = _get_mode(exe)
    log.info(f"AT loop started for {display_name} ({exe}) — mode={mode}, "
             f"step={p['core_step_mhz']}/{p['mem_step_mhz']}, "
             f"thresholds={p['max_score_to_step_down']}/{p['min_score_to_step_up']}")
    # v3.1 — opening-phase aware warmup + consecutive-good threshold.
    # During the first OPENING_PHASE_SEC of the session, we run with
    # reduced warmup so AT closes on the rough ceiling faster.
    initial_state = get_game_state(exe)
    warmup, consecutive_required = _effective_thresholds(p, initial_state)
    poll = int(p["score_poll_interval_s"])
    good_threshold = int(p["min_score_to_step_up"])
    bad_threshold  = int(p["max_score_to_step_down"])
    # Step sizes are recomputed per-decision via _effective_step so
    # hot-streak doubling activates dynamically.  Keep base values for
    # logging / fallback.
    core_step = int(p["core_step_mhz"])
    mem_step  = int(p["mem_step_mhz"])
    log.info(f"  opening-phase: {'YES' if _in_opening_phase(initial_state) else 'no'} "
             f"warmup={warmup}s consecutive_required={consecutive_required}")

    # Warm-up — give the game time to load shaders, populate VRAM, etc.
    if stop_event.wait(warmup):
        return

    # Alternate which axis we bump first — start with core (more visible
    # FPS gain), alternate with mem on every other promotion.
    next_axis = "core"

    # Track consecutive missed-process polls.  We only self-clean after a few
    # in a row so a brief PID gap (alt-tab to launcher, etc.) doesn't bounce us.
    missed_proc_polls = 0
    MISSED_PROC_THRESHOLD = 3

    # v3.1.2 — Out-of-band crash detection (mirrors auto_oc_next /
    # benchmark_oc_next).  AT's primary stability signal is the
    # perf_monitor score derived from clock variance + temp + util
    # consistency — but a clean TDR can occur AFTER the score samples
    # were taken (driver crashes during a frame, stops responding,
    # recovers).  When that happens the next AT poll might see a
    # "healthy" score because perf_monitor's last samples were pre-
    # crash.  This OOB check looks at crash_recovery's authoritative
    # crash record and forces a step-down + warning entry the moment
    # the kernel-side hook fired, regardless of what the score says.
    _last_oob_crash_ts_seen = 0.0

    while not stop_event.is_set():
        # ── Liveness check.  game_profiles is supposed to call on_game_exit
        # when the tracked game closes, but if AT was engaged via user
        # toggle (engage_for_active_game) and game_profiles never had this
        # exe in its monitor, no exit event will arrive.  Detect that here
        # and self-clean so we don't loop forever showing "playing now".
        if not _target_process_alive(exe):
            missed_proc_polls += 1
            if missed_proc_polls >= MISSED_PROC_THRESHOLD:
                log.info(f"AT self-clean: {exe} no longer running — releasing engagement")
                # Defer to the normal exit handler so session totals + notify
                # logic run.  Pass was_crash=False since we don't know.
                try:
                    on_game_exit(exe, display_name, was_crash=False,
                                 reason="self-clean: process gone")
                except Exception as e:
                    log.warning(f"AT self-clean on_game_exit failed: {e}")
                # Even if on_game_exit failed, force the runtime back to
                # idle so the UI stops showing 'playing now' forever.
                with _lock:
                    _runtime["active_exe"]      = None
                    _runtime["active_display"]  = None
                    _runtime["loop_thread"]     = None
                    _runtime["loop_stop"]       = None
                return
        else:
            missed_proc_polls = 0

        # v3.1.2 — Out-of-band crash detection.  Did a TDR / driver crash
        # / BSOD fire since this AT session started, BUT after the last
        # one we already handled?  If so, force an immediate step-down
        # to baseline regardless of what perf_monitor's score says.  We
        # also blacklist the offset (so future sessions know it failed)
        # and append a step_down history entry tagged with the crash kind.
        try:
            from core import gpu_overclock
            cs = gpu_overclock.get_crash_state()
            session_started = (_runtime.get("started_at") or 0.0)
            cts = float(cs.get("crashed_ts") or 0.0)
            if (cts and
                cts > session_started and
                cts > _last_oob_crash_ts_seen):
                _last_oob_crash_ts_seen = cts
                with _lock:
                    cc = _runtime["current_core"]
                    cm = _runtime["current_mem"]
                state = get_game_state(exe)
                # Mark this offset as a hard crash point
                state["warned_offsets"].append({
                    "core": cc, "mem": cm, "ts": time.time(),
                    "score": None, "verdict": "oob_crash",
                    "kind": cs.get("kind") or "unknown",
                })
                state["warned_offsets"] = state["warned_offsets"][-30:]
                # Driver-level crashes are cross-game by nature.  Mirror
                # into the cross-game blacklist so other games inherit
                # the bad-offset knowledge (paths like TDR / context_loss
                # are driver issues, not game issues).
                driver_kinds = ("tdr", "context_loss", "hang", "bsod",
                                "driver", "thermal", "stuck_clock")
                if any(k in (cs.get("kind") or "").lower() for k in driver_kinds):
                    try:
                        add_cross_game_blacklist(
                            cc, cm,
                            source_exe=exe,
                            reason=f"oob_crash:{cs.get('kind') or 'unknown'}",
                        )
                    except Exception as _e:
                        log.debug(f"AT OOB cross-blacklist add failed: {_e}")
                # Step down to baseline immediately — this is more
                # aggressive than the score-based step-down (which moves
                # one step) because a crash means we definitely went too
                # far, and the score-based step-down might take another
                # poll cycle to fire.
                floor_core = int(state.get("baseline_core", 0))
                floor_mem  = int(state.get("baseline_mem",  0))
                state["current_core"]    = floor_core
                state["current_mem"]     = floor_mem
                state["converged"]       = False
                state["hot_streak"]      = 0
                state["stable_sessions"] = 0
                state["stable_minutes"]  = 0.0
                state["last_step_at"]      = time.time()
                state["last_step_was_up"]  = False
                state["last_step_offset"]  = {"core": cc, "mem": cm}
                _update_confidence(state)
                _save_game_state(exe, state)
                _append_history({
                    "ts": time.time(), "exe": _norm_exe(exe),
                    "display_name": display_name,
                    "action": "step_down",
                    "from_core": cc, "from_mem": cm,
                    "to_core": floor_core, "to_mem": floor_mem,
                    "score": None, "verdict": "oob_crash",
                    "temp_c":   0, "power_pct": 0,
                    "step_size": {"core": max(0, cc - floor_core),
                                  "mem":  max(0, cm - floor_mem)},
                    "reason": f"out-of-band crash detected: {cs.get('kind') or 'unknown'}",
                })
                if _apply_offsets(exe, floor_core, floor_mem, "oob-step-down"):
                    with _lock:
                        _runtime["current_core"] = floor_core
                        _runtime["current_mem"]  = floor_mem
                        _runtime["consecutive_good"] = 0
                        _runtime["step_count"] += 1
                log.error(
                    f"AT OOB step-down: {exe} crashed at core+{cc}/mem+{cm} "
                    f"(kind={cs.get('kind')}) — reverted to baseline "
                    f"core+{floor_core}/mem+{floor_mem}"
                )
                # Clear the crash state so we don't re-fire on the next
                # poll for the same crash event.
                try: gpu_overclock.clear_crash_state()
                except Exception: pass
                # Sleep one full poll cycle to let perf_monitor's score
                # window refresh before we make any more decisions.
                if stop_event.wait(poll):
                    return
                continue
        except Exception as e:
            log.debug(f"AT OOB crash check failed: {e}")

        # v3.1 — paused?  Skip decisions while a recoil capture / latency
        # probe / external feature has asked AT to hold still.
        if is_paused():
            with _lock:
                _runtime["last_reason"] = f"paused: {', '.join(sorted(_pause_sources)) or 'unknown'}"
            log.debug(f"AT loop paused — sources: {sorted(_pause_sources)}")
            if stop_event.wait(poll):
                return
            continue

        # beta.14 — AT-owned drift check.  Stock OC watchdog is paused
        # while AT runs (see gpu_overclock.pause_oc_watchdog call in
        # _engage); this is what replaces it.  Run once per poll cycle
        # immediately before sampling, so the score we read in a moment
        # reflects a known-good offset.  Cheap — one NVAPI read per
        # ~5-30s poll.
        try:
            with _lock:
                _at_target_core = int(_runtime.get("current_core", 0))
                _at_target_mem  = int(_runtime.get("current_mem",  0))
            _at_check_offset_drift(exe, _at_target_core, _at_target_mem)
        except Exception as _e:
            log.debug(f"AT drift check failed: {_e}")

        try:
            from core import perf_monitor
            score_data = perf_monitor.compute_stability_score(seconds=poll + 5)
            score = score_data.get("score")
            verdict = score_data.get("verdict", "no-data")

            # v3.2 — frame-telemetry-aware scoring.  Pull recent frame
            # stats from RTSS (if available).  If the GPU looks "healthy"
            # to nvidia-smi but the game is actually stuttering (high
            # frametime variance), DOWNGRADE the verdict so AT stops
            # stepping up.  This is the central fix for the v3.1.x
            # complaint "AT thinks everything's fine but the game stutters".
            frame_stats = None
            try:
                from core import frame_telemetry
                frame_stats = frame_telemetry.get_recent_stats(poll + 5)
            except Exception as _e:
                log.debug(f"frame_telemetry.get_recent_stats failed: {_e}")

            frame_downgrade_reason = ""
            if frame_stats and frame_stats.get("available"):
                ft_var_pct = frame_stats.get("frametime_var_pct") or 0
                # Frametime σ > 25% of mean = visibly stuttery.  This is
                # a stronger signal than util_score because it measures
                # what the PLAYER actually feels.
                if ft_var_pct >= 40 and verdict in ("healthy", "warn"):
                    verdict = "unstable"
                    frame_downgrade_reason = (
                        f"frametime σ {ft_var_pct:.0f}% of mean — "
                        f"game is stuttering badly even though GPU looks OK"
                    )
                    log.warning(f"AT verdict downgrade: {frame_downgrade_reason}")
                elif ft_var_pct >= 25 and verdict == "healthy":
                    # Soft downgrade — hold instead of stepping up
                    verdict = "warn"
                    frame_downgrade_reason = (
                        f"frametime σ {ft_var_pct:.0f}% — holding "
                        f"(no step-up while frames are uneven)"
                    )
                    log.info(f"AT verdict softened: {frame_downgrade_reason}")

            # v3.1 — refresh effective thresholds each tick so we
            # transition out of the opening phase cleanly when the
            # window expires (no need to restart the loop).
            state_now = get_game_state(exe)
            _, consecutive_required = _effective_thresholds(p, state_now)
            # GPU health for logging + step-up gating
            health = _get_gpu_health()

            # v3.2-beta.3 — gameplay-vs-menu classification.  AT should
            # only make decisions during *actual gameplay*; menus, loading
            # screens, multiplayer lobbies, and pause screens all produce
            # GPU + frame data wildly different from real play, and acting
            # on them would teach AT bogus stability info.  We feed every
            # poll into the classifier and check the resulting state.
            #
            # v3.2-beta.4 — also pass membw (memory bandwidth util) for
            # the VRAM-streaming-variance signal.  CPU + foreground are
            # sampled inside the classifier itself (no extra IO here).
            try:
                from core import gameplay_state, perf_monitor as _pm
                # Latest perf_monitor sample's memutil = memory bandwidth %
                last = _pm._state.get("last_sample") or {}
                membw_val = last.get("memutil")
                gs_result = gameplay_state.observe(
                    exe,
                    fps   = (frame_stats or {}).get("avg_fps") if frame_stats else None,
                    util  = score_data.get("last_util"),
                    ftvar = (frame_stats or {}).get("frametime_var_pct") if frame_stats else None,
                    power = health.get("power_pct"),
                    membw = float(membw_val) if membw_val is not None else None,
                )
                gameplay_phase = gs_result.get("state", "unknown")
            except Exception as _e:
                log.debug(f"gameplay_state.observe failed: {_e}")
                gs_result      = {"state": "unknown", "reasons": []}
                gameplay_phase = "unknown"

            with _lock:
                _runtime["last_score"]   = score
                _runtime["last_verdict"] = verdict
                _runtime["last_util"]    = score_data.get("last_util")
                _runtime["last_reason"]  = score_data.get("reason") or frame_downgrade_reason
                _runtime["last_poll_at"] = time.time()
                _runtime["last_temp_c"]  = health.get("temp_c", 0)
                _runtime["last_power_pct"] = health.get("power_pct", 0)
                _runtime["last_frame_stats"] = frame_stats if (frame_stats and frame_stats.get("available")) else None
                _runtime["last_score_subs"]  = score_data.get("subs") or {}
                _runtime["gameplay_phase"]    = gameplay_phase
                _runtime["gameplay_conf"]     = gs_result.get("confidence", 0.0)
                _runtime["gameplay_reasons"]  = gs_result.get("reasons", [])
                _runtime["gameplay_baseline"] = gs_result.get("baseline", {})

            # v3.2-beta.3 — skip tuning decisions when not in confirmed
            # gameplay.  We still record scores into the live state so
            # the diagnostic panel can show the user *why* AT is idle.
            if gameplay_phase not in ("gameplay",):
                with _lock:
                    if gameplay_phase == "baseline-building":
                        _runtime["last_reason"] = (
                            f"baseline-building — gathering reference samples "
                            f"for this game ({gs_result.get('reasons', [''])[0] if gs_result.get('reasons') else ''})"
                        )
                    elif gameplay_phase == "menu":
                        _runtime["last_reason"] = (
                            "menu detected — skipping tuning decisions "
                            f"({gs_result.get('reasons', [''])[0] if gs_result.get('reasons') else ''})"
                        )
                    elif gameplay_phase == "transitional":
                        _runtime["last_reason"] = (
                            "transitional state — waiting for stable classification"
                        )
                log.debug(f"AT skipping decision: phase={gameplay_phase} "
                          f"conf={gs_result.get('confidence', 0):.2f}")
                if stop_event.wait(poll):
                    return
                continue

            if score is None:
                # Idle / no data → just keep sampling, don't change anything.
                # Surface util in the log so the user can see why.
                log.debug(f"AT poll: no score ({verdict}) — "
                          f"util={score_data.get('last_util')}% "
                          f"avg={score_data.get('avg_util')}% "
                          f"max={score_data.get('max_util')}%")
            elif verdict == "unstable" or (score is not None and score < bad_threshold):
                # Step DOWN — the offset isn't holding.  Mark warned so
                # we have to earn back trust before re-trying.
                with _lock:
                    cc = _runtime["current_core"]
                    cm = _runtime["current_mem"]
                state = get_game_state(exe)
                state["warned_offsets"].append({
                    "core": cc, "mem": cm, "ts": time.time(),
                    "score": score, "verdict": verdict,
                })
                state["warned_offsets"] = state["warned_offsets"][-30:]
                # v3 — floor the step-down at the USER'S baseline OC, not at 0.
                # The user has presumably tuned their card to a known-good
                # config; AT going below that would undo their work.  If
                # instability is observed at exactly the baseline, the floor
                # holds and AT just stops stepping (let crash_recovery decide
                # if the baseline itself needs revisiting).
                # v3.1 — use adaptive step (hot-streak resets to base on
                # step-down, but per-game multiplier still applies).
                eff_core_step = _effective_step(state, p, "core")
                eff_mem_step  = _effective_step(state, p, "mem")
                floor_core = int(state.get("baseline_core", 0))
                floor_mem  = int(state.get("baseline_mem",  0))
                new_core = max(floor_core, cc - eff_core_step)
                new_mem  = max(floor_mem,  cm - eff_mem_step)
                state["current_core"] = new_core
                state["current_mem"]  = new_mem
                state["converged"]    = False
                # v3.1 — reset hot streak; this step proved the streak
                # was over-shooting.  Also reset confidence-window
                # accumulators (we moved offsets, the old "stable at X"
                # observation no longer applies to the new offset).
                state["hot_streak"]      = 0
                state["stable_sessions"] = 0
                state["stable_minutes"]  = 0.0
                state["last_step_at"]      = time.time()
                state["last_step_was_up"]  = False
                state["last_step_offset"]  = {"core": cc, "mem": cm}
                _update_confidence(state)
                _save_game_state(exe, state)

                _append_history({
                    "ts": time.time(), "exe": _norm_exe(exe),
                    "display_name": display_name,
                    "action": "step_down",
                    "from_core": cc, "from_mem": cm,
                    "to_core": new_core, "to_mem": new_mem,
                    "score": score, "verdict": verdict,
                    "temp_c":   health.get("temp_c", 0),
                    "power_pct": health.get("power_pct", 0),
                    "step_size": {"core": eff_core_step, "mem": eff_mem_step},
                    "reason": "score below threshold",
                })

                if _apply_offsets(exe, new_core, new_mem, "step-down"):
                    with _lock:
                        _runtime["current_core"] = new_core
                        _runtime["current_mem"]  = new_mem
                        _runtime["consecutive_good"] = 0
                        _runtime["step_count"] += 1
                log.warning(f"AT step DOWN to core+{new_core}/mem+{new_mem} "
                            f"(score {score}, {verdict})")
            elif verdict == "healthy" and score >= good_threshold:
                with _lock:
                    _runtime["consecutive_good"] += 1
                    cg = _runtime["consecutive_good"]
                log.debug(f"AT score {score} healthy ({cg}/{consecutive_required})")
                # Accumulate stable_minutes on EVERY healthy poll, not
                # just on step-ups — that's what drives the confidence
                # tier "this OC has been stable for 6 hours".
                state = get_game_state(exe)
                state["stable_minutes"] = float(state.get("stable_minutes", 0.0)) + (poll / 60.0)
                _update_confidence(state)
                _save_game_state(exe, state)
                if cg >= consecutive_required:
                    # Try to bump up — alternate axis.  v3.1 uses
                    # adaptive step (per-game multiplier + hot-streak
                    # doubling after 3 consecutive successful step-ups).
                    with _lock:
                        cc = _runtime["current_core"]
                        cm = _runtime["current_mem"]
                    state = get_game_state(exe)
                    eff_core_step = _effective_step(state, p, "core")
                    eff_mem_step  = _effective_step(state, p, "mem")
                    if next_axis == "core":
                        proposed_core, proposed_mem = cc + eff_core_step, cm
                        axis_for_safety = "core"
                    else:
                        proposed_core, proposed_mem = cc, cm + eff_mem_step
                        axis_for_safety = "mem"

                    # v3.1 — pass the axis so the power-bound gate can
                    # let mem step-ups through even when the GPU is
                    # power-saturated.
                    safe, reason = _is_safe_to_step_to(
                        exe, proposed_core, proposed_mem, axis=axis_for_safety)
                    if not safe:
                        # Try the other axis instead
                        if next_axis == "core":
                            alt_core, alt_mem = cc, cm + eff_mem_step
                            alt_axis = "mem"
                        else:
                            alt_core, alt_mem = cc + eff_core_step, cm
                            alt_axis = "core"
                        safe2, _ = _is_safe_to_step_to(
                            exe, alt_core, alt_mem, axis=alt_axis)
                        if safe2:
                            proposed_core, proposed_mem = alt_core, alt_mem
                            next_axis = "mem" if next_axis == "core" else "core"
                            safe = True
                        else:
                            # Both axes blocked — log a "skip" so users
                            # can see WHY AT stopped stepping (thermal /
                            # power / blacklist / cap).  Only call this
                            # "converged" if the block reason isn't
                            # transient (thermal / power can clear when
                            # the GPU cools / drops below TDP).
                            transient = ("thermal" in reason or "power-bound" in reason)
                            if transient:
                                log.info(f"AT step deferred (transient): {reason}")
                                _append_history({
                                    "ts": time.time(), "exe": _norm_exe(exe),
                                    "display_name": display_name,
                                    "action": "skip",
                                    "from_core": cc, "from_mem": cm,
                                    "to_core": cc, "to_mem": cm,
                                    "score": score, "verdict": verdict,
                                    "temp_c":    health.get("temp_c", 0),
                                    "power_pct": health.get("power_pct", 0),
                                    "reason": reason,
                                })
                                # Hold; don't reset consecutive_good
                                # (we'll re-try next poll).  Slightly
                                # longer sleep to give thermal / power
                                # state time to change.
                                stop_event.wait(poll * 2)
                                continue
                            log.info(f"AT converged for {display_name}: "
                                     f"core+{cc}/mem+{cm} (both axes blocked: {reason})")
                            state = get_game_state(exe)
                            state["best_stable_core"] = cc
                            state["best_stable_mem"]  = cm
                            state["converged"]       = True
                            _save_game_state(exe, state)
                            _append_history({
                                "ts": time.time(), "exe": _norm_exe(exe),
                                "display_name": display_name,
                                "action": "converged",
                                "from_core": cc, "from_mem": cm,
                                "to_core": cc, "to_mem": cm,
                                "score": score, "verdict": verdict,
                                "temp_c":    health.get("temp_c", 0),
                                "power_pct": health.get("power_pct", 0),
                                "reason": reason,
                            })
                            with _lock:
                                _runtime["consecutive_good"] = 0
                            # Long sleep — nothing to do but watch stability
                            stop_event.wait(poll * 4)
                            continue

                    # Apply!  v3.1 records step metadata for the rich
                    # decision log + ticks hot_streak.
                    if _apply_offsets(exe, proposed_core, proposed_mem, "step-up"):
                        state = get_game_state(exe)
                        state["current_core"] = proposed_core
                        state["current_mem"]  = proposed_mem
                        state["best_stable_core"] = max(state["best_stable_core"], cc)
                        state["best_stable_mem"]  = max(state["best_stable_mem"],  cm)
                        state["step_count"] += 1
                        state["converged"] = False
                        state["hot_streak"]       = int(state.get("hot_streak", 0)) + 1
                        # Reset stable counters at new offset
                        state["stable_minutes"]   = 0.0
                        state["stable_sessions"]  = 0
                        _update_confidence(state)
                        state["last_step_at"]     = time.time()
                        state["last_step_was_up"] = True
                        state["last_step_offset"] = {"core": cc, "mem": cm}
                        _save_game_state(exe, state)
                        _append_history({
                            "ts": time.time(), "exe": _norm_exe(exe),
                            "display_name": display_name,
                            "action": "step_up",
                            "from_core": cc, "from_mem": cm,
                            "to_core": proposed_core, "to_mem": proposed_mem,
                            "score": score, "verdict": verdict,
                            "axis": next_axis,
                            "temp_c":    health.get("temp_c", 0),
                            "power_pct": health.get("power_pct", 0),
                            "hot_streak": int(state.get("hot_streak", 0)),
                            "step_size": {"core": eff_core_step, "mem": eff_mem_step},
                            "opening_phase": _in_opening_phase(state),
                        })
                        with _lock:
                            _runtime["current_core"] = proposed_core
                            _runtime["current_mem"]  = proposed_mem
                            _runtime["consecutive_good"] = 0
                            _runtime["step_count"] += 1
                        hot_tag = " [HOT-STREAK 2×]" if int(state.get("hot_streak", 0)) >= HOT_STREAK_THRESHOLD else ""
                        log.info(f"AT step UP {next_axis} to core+{proposed_core}/"
                                 f"mem+{proposed_mem} (score {score}, "
                                 f"step_size=core{eff_core_step}/mem{eff_mem_step}"
                                 f"{hot_tag})")
                        # Alternate axis next time
                        next_axis = "mem" if next_axis == "core" else "core"
            else:
                # 'warn' or borderline — hold without stepping.  Reset the
                # consecutive_good counter so we don't sneak up on a step
                # after a single good sample.
                with _lock:
                    _runtime["consecutive_good"] = 0
                log.debug(f"AT hold: score {score}/{verdict}")
        except Exception as e:
            log.warning(f"AT loop tick failed: {e}")

        if stop_event.wait(poll):
            return

    log.info(f"AT loop stopped for {display_name}")


# ─── Entry points (called by game_profiles) ──────────────────────────────
def on_game_start(exe: str, display_name: str | None) -> dict:
    """Hook called by game_profiles when a tracked game starts.

    ALWAYS records the game in the state file (so it shows up in the AT
    UI for opt-in), then decides whether AT should engage.  Engagement
    requires both the global master switch + this specific game's
    per-game enable flag to be on.

    Use `engage_for_active_game()` to retroactively engage when those
    flags get flipped on after the game has already started.
    """
    s = get_settings()

    # ── Always record the game so the user can see it / opt-in ──────
    states = _load_json(STATE_PATH, {})
    key = _norm_exe(exe)
    if key not in states:
        states[key] = _empty_state(exe)
        states[key]["last_session_at"] = time.time()
        _save_json(STATE_PATH, states)
        log.info(f"AT registered new game: {key} (enabled={states[key]['enabled']})")
    state = states[key]

    if not s.get("enabled_globally"):
        return {"engaged": False, "tracked": True,
                "reason": "AT globally disabled"}

    if not state.get("enabled"):
        return {"engaged": False, "tracked": True,
                "reason": "per-game AT disabled"}

    return _engage(exe, display_name, state)


def _engage(exe: str, display_name: str | None, state: dict) -> dict:
    """Apply this game's saved offsets + spin up the tuning loop.
    Shared by `on_game_start` (called from game_profiles when a game
    launches) AND `engage_for_active_game` (called when AT settings
    flip on while a game is already running).

    v3.1 — sets `opening_phase_until` so the tuning loop runs with
    relaxed thresholds for the first OPENING_PHASE_SEC.  Also probes
    for an auto-revert candidate (recent crash after the last step-up)
    and rolls the offset back one level BEFORE applying."""
    # v3.1 — auto-revert detection.  If a crash was recorded after the
    # last step-up and within the auto-revert window, step the offset
    # back to the pre-step value + record a step_down in history.
    revert = _detect_auto_revert(exe, state)
    if revert is not None:
        rc, rm = revert
        log.warning(f"AT auto-revert for {display_name}: crash after step-up at "
                    f"core+{state.get('current_core',0)}/mem+{state.get('current_mem',0)} — "
                    f"reverting to core+{rc}/mem+{rm}")
        state["current_core"]    = rc
        state["current_mem"]     = rm
        state["hot_streak"]      = 0
        state["stable_sessions"] = 0
        state["stable_minutes"]  = 0.0
        state["last_step_was_up"]= False
        _update_confidence(state)
        _save_game_state(exe, state)
        _append_history({
            "ts": time.time(), "exe": _norm_exe(exe),
            "display_name": display_name or exe,
            "action": "auto_revert",
            "from_core": int(state.get("last_step_offset", {}).get("core", rc)),
            "from_mem":  int(state.get("last_step_offset", {}).get("mem",  rm)),
            "to_core":   rc, "to_mem": rm,
            "reason":    "crash within auto-revert window",
        })

    cc = int(state.get("current_core", 0))
    cm = int(state.get("current_mem",  0))
    # v3.1 — opening phase: relaxed thresholds for the first 5 min so
    # AT closes on the rough ceiling faster.  Set BEFORE the loop starts.
    state["opening_phase_until"] = time.time() + OPENING_PHASE_SEC
    _save_game_state(exe, state)
    # beta.14 — pause the stock OC drift watchdog while AT owns the
    # offsets.  AT has its own drift check (`_at_check_offset_drift`)
    # that runs in the tuning loop right before each step decision, so
    # there's no reason to have two threads competing to write NVAPI.
    # Paired with resume in on_game_exit / _cleanup_runtime.
    try:
        gpu_overclock.pause_oc_watchdog("adaptive_tuning")
    except Exception as e:
        log.debug(f"pause_oc_watchdog failed: {e}")
    apply_ok = _apply_offsets(exe, cc, cm, "engage") if (cc or cm) else True

    # Stop any existing loop (shouldn't be one but be defensive)
    with _lock:
        if _runtime["loop_stop"] is not None:
            try: _runtime["loop_stop"].set()
            except Exception: pass

        ev = threading.Event()
        _runtime["active_exe"]       = _norm_exe(exe)
        _runtime["active_display"]   = display_name
        _runtime["current_core"]     = cc
        _runtime["current_mem"]      = cm
        _runtime["consecutive_good"] = 0
        _runtime["step_count"]       = 0
        _runtime["started_at"]       = time.time()
        _runtime["last_score"]       = None
        _runtime["last_verdict"]     = None
        _runtime["loop_stop"]        = ev
        t = threading.Thread(
            target=_tuning_loop,
            args=(exe, display_name or exe, ev),
            daemon=True,
            name=f"adaptive-tuning-{_norm_exe(exe)}",
        )
        _runtime["loop_thread"] = t
        t.start()

    # Make sure perf_monitor is running so the loop has data
    try:
        from core import perf_monitor
        perf_monitor.start(consumer="adaptive-tuning")
    except Exception:
        pass

    # v3.2 — also spin up frame telemetry tracking for this game.  Tries
    # to attach to the game's PID via game_profiles; falls back to the
    # exe-name match inside the telemetry sampler.  Frame data is
    # advisory — if RTSS isn't running we degrade silently to GPU-only
    # stability scoring (the historic v3.1 behaviour).
    target_pid = None
    try:
        from core import frame_telemetry, game_profiles
        try:
            info = game_profiles.get_active_game_info()
            if info and (info.get("exe") or "").lower() == _norm_exe(exe):
                target_pid = info.get("pid")
        except Exception: pass
        frame_telemetry.start_tracking(pid=target_pid, exe_name=_norm_exe(exe))
    except Exception as e:
        log.debug(f"frame_telemetry.start_tracking failed: {e}")

    # v3.2-beta.4 — tell the gameplay classifier which PID to sample
    # CPU + foreground state for.  Without this, the classifier can't
    # use those signals (it'd have no idea which process to look at).
    try:
        from core import gameplay_state
        gameplay_state.set_target(_norm_exe(exe), target_pid)
    except Exception as e:
        log.debug(f"gameplay_state.set_target failed: {e}")

    log.info(f"AT engaged for {display_name} starting at core+{cc}/mem+{cm} "
             f"(apply_ok={apply_ok})")
    return {
        "engaged":       True,
        "tracked":       True,
        "starting_core": cc,
        "starting_mem":  cm,
        "apply_ok":      apply_ok,
    }


def engage_for_active_game() -> dict:
    """Retroactive engagement.  Called by `set_settings` and
    `set_game_enabled` when those toggles flip on — checks if a tracked
    game is currently running and engages AT for it without waiting
    for the next launch.

    Skips silently if:
      • No active game
      • AT globally disabled
      • Per-game AT disabled
      • A loop is already running for this game
    """
    s = get_settings()
    if not s.get("enabled_globally"):
        return {"engaged": False, "reason": "global off"}
    try:
        from core import game_profiles
        active = game_profiles.get_active_game_info()
    except Exception:
        active = None
    if not active or not active.get("exe"):
        return {"engaged": False, "reason": "no active game"}
    state = get_game_state(active["exe"])
    if not state.get("enabled"):
        return {"engaged": False, "reason": "per-game off",
                "exe": active["exe"]}
    with _lock:
        if _runtime.get("active_exe") == _norm_exe(active["exe"]):
            return {"engaged": False, "reason": "already engaged",
                    "exe": active["exe"]}
    log.info(f"AT retroactive engagement: {active.get('display') or active['exe']}")
    return _engage(active["exe"], active.get("display"), state)


def on_game_exit(exe: str, display_name: str | None,
                 was_crash: bool = False, reason: str = "") -> dict:
    """Called by game_profiles when a tracked game exits, OR by the
    tuning loop's self-clean path when it detects the game's process is
    gone but no exit event arrived (game_profiles missed it).  Stops the
    tuning loop, saves the session summary, releases the perf consumer.

    `reason` lets callers distinguish e.g. 'self-clean: process gone'
    from a normal exit — only used for logging and the notification text.
    """
    with _lock:
        ev = _runtime["loop_stop"]
        started = _runtime["started_at"]
        cc = _runtime["current_core"]
        cm = _runtime["current_mem"]
        active_exe = _runtime["active_exe"]

    if active_exe != _norm_exe(exe):
        # The runtime isn't currently tracking this exe — nothing to
        # update.  But still try to fire a notification + return cleanly.
        log.debug(f"on_game_exit({exe}): AT not active here "
                  f"(runtime active_exe={active_exe})")
        return {"updated": False, "reason": "AT not active for this game"}

    if ev is not None:
        try: ev.set()
        except Exception: pass

    # Update session totals
    state = get_game_state(exe)
    if started is not None:
        minutes = (time.time() - started) / 60.0
        state["session_minutes"] = round(state.get("session_minutes", 0) + minutes, 2)
    state["session_count"] = int(state.get("session_count", 0)) + 1
    state["last_session_at"] = time.time()
    state["last_session_outcome"] = "crash" if was_crash else "clean"
    state["current_core"] = cc
    state["current_mem"]  = cm
    if not was_crash:
        # Survived another session at this offset — bump the "best stable"
        state["best_stable_core"] = max(state.get("best_stable_core", 0), cc)
        state["best_stable_mem"]  = max(state.get("best_stable_mem", 0),  cm)
        # v3.1 — clean exit at current offset increments the stable-
        # session counter, which feeds the confidence tier.  Crash
        # exits reset stable_sessions to 0 (we no longer trust this
        # offset until we earn confidence again).
        state["stable_sessions"] = int(state.get("stable_sessions", 0)) + 1
    else:
        state["stable_sessions"] = 0
        state["stable_minutes"]  = 0.0
    _update_confidence(state)
    _save_game_state(exe, state)

    # Release perf_monitor consumer
    try:
        from core import perf_monitor
        perf_monitor.stop(consumer="adaptive-tuning")
    except Exception:
        pass

    # v3.2 — stop frame telemetry tracker (keeps the ring buffer so the
    # UI can show the post-session summary).
    try:
        from core import frame_telemetry
        frame_telemetry.stop_tracking()
    except Exception:
        pass

    # beta.14 — hand the OC watchdog back to stock control now that AT
    # is no longer babysitting offsets.  Matches the pause_oc_watchdog
    # call in _engage.  Best-effort: a missed resume doesn't damage
    # anything (next reset_oc clears the active flag anyway).
    try:
        gpu_overclock.resume_oc_watchdog("adaptive_tuning")
    except Exception as e:
        log.debug(f"resume_oc_watchdog failed: {e}")

    # v3.2-beta.4 — clear gameplay classifier's target PID so the
    # foreground/CPU samplers stop pointing at a dead process.
    try:
        from core import gameplay_state
        gameplay_state.set_target(_norm_exe(exe), None)
    except Exception:
        pass

    # Wipe runtime
    with _lock:
        _runtime["active_exe"]      = None
        _runtime["active_display"]  = None
        _runtime["loop_thread"]     = None
        _runtime["loop_stop"]       = None
        _runtime["current_core"]    = 0
        _runtime["current_mem"]     = 0
        _runtime["consecutive_good"] = 0
        _runtime["step_count"]      = 0
        _runtime["started_at"]      = None
        _runtime["last_score"]      = None
        _runtime["last_verdict"]    = None

    log.info(f"AT session ended for {display_name}: "
             f"{'CRASH' if was_crash else 'clean'}, "
             f"final offsets core+{cc}/mem+{cm}, reason={reason or 'normal exit'}")

    # If this exit came via the self-clean path (game_profiles missed the
    # exit and never fired its own restore_normal_mode + notification),
    # fire a notification ourselves so the user sees that the game closed.
    if reason and "self-clean" in reason:
        try:
            from core import notifier
            notifier.notify_game_exited(game_name=display_name or exe)
        except Exception as e:
            log.debug(f"AT self-clean notification failed: {e}")

    return {
        "updated":         True,
        "final_core":      cc,
        "final_mem":       cm,
        "was_crash":       was_crash,
        "session_minutes": state["session_minutes"],
        "session_count":   state["session_count"],
    }
