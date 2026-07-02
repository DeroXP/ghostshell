"""Game-exit crash detection + step-down + post-game notification.

When a game closes, this module decides whether it crashed (vs. the
user quitting normally) by looking at:
  • Windows Event Log TDR / driver-crash events in the last 10 s
  • The in-app `gpu_overclock.get_crash_state()` flag (set by the
    stress-test probe + GPU live-monitor when context-loss / pixel
    artifacts / hangs are detected)
  • Whether the game exited with active OC offsets

If a crash is detected AND the game had OC offsets active, GhostShell:
  1. Adds the failing offset to that game's permanent crash blacklist
  2. Computes a safe step-down — highest known-good offset below the
     failing one (per axis: core / mem)
  3. Optionally re-saves the stepped-down profile + applies it
  4. Fires a Windows toast within ~5 s explaining what happened

Persistent files in `%APPDATA%\\GhostShell\\`:
  crash_blacklist.json  — per-game list of failed (core, mem) pairs
  crash_history.json    — chronological log of all detected crashes

Public API:
  handle_game_exit(exe, display_name, exit_ts, was_active)  → primary entry
  detect_crash(exit_ts, lookback_s)                          → bool+detail
  is_offset_blacklisted(exe, core, mem)                      → bool
  add_to_blacklist(exe, core, mem, kind)                     → void
  compute_safe_step_down(exe, core, mem)                     → {core, mem}
  get_blacklist(exe=None)                                    → dict | list
  get_history(limit=50)                                      → list
  clear_blacklist(exe=None)                                  → ok
  clear_history()                                            → ok
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger, atomic_write_json

log = get_logger("crash_recovery")

BLACKLIST_PATH = os.path.join(APPDATA_DIR, "crash_blacklist.json")
HISTORY_PATH   = os.path.join(APPDATA_DIR, "crash_history.json")

# Look back this far in the Windows Event Log for TDR signals after a
# game exit.  10 s captures most "process died right after a TDR" cases
# without false-positiving on a TDR that happened minutes earlier.
CRASH_LOOKBACK_S = 10

# Minimum core/mem step-down per axis when we step down (MHz).
# Bigger steps give a cushion against repeat-crashing the same game.
STEP_DOWN_CORE_MHZ = 25
STEP_DOWN_MEM_MHZ  = 100


# ─── Persistence helpers ─────────────────────────────────────────────────
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
    """Normalize exe names for blacklist keys — lowercase, strip path."""
    if not exe:
        return ""
    return os.path.basename(exe).lower()


# ─── Blacklist API ───────────────────────────────────────────────────────
def get_blacklist(exe: str | None = None):
    """Return blacklist for one game (list of failed offsets) or all
    games (dict)."""
    bl = _load_json(BLACKLIST_PATH, {})
    if exe is None:
        return bl
    return bl.get(_norm_exe(exe), [])


def is_offset_blacklisted(exe: str, core: int, mem: int) -> bool:
    """True iff this exact (core, mem) pair has crashed for this game.
    A crash at lower offsets *also* implies higher offsets are unsafe —
    so we treat 'crashed at core+170' as 'avoid core ≥ 170 too'."""
    fails = get_blacklist(exe)
    for f in fails:
        if int(f.get("core", 0)) <= core and int(f.get("mem", 0)) <= mem:
            return True
    return False


def add_to_blacklist(exe: str, core: int, mem: int, kind: str = "tdr") -> None:
    bl = _load_json(BLACKLIST_PATH, {})
    key = _norm_exe(exe) or "unknown"
    fails = bl.get(key, [])
    # Don't duplicate the same (core,mem) entry — just bump the seen count
    for f in fails:
        if int(f.get("core", -1)) == core and int(f.get("mem", -1)) == mem:
            f["count"] = int(f.get("count", 1)) + 1
            f["last_seen"] = time.time()
            f["kind"] = kind   # latest kind wins
            _save_json(BLACKLIST_PATH, bl)
            log.info(f"Blacklist hit: {key} core+{core}/mem+{mem} ({kind}) "
                     f"now seen {f['count']}x")
            return
    fails.append({
        "core":      int(core),
        "mem":       int(mem),
        "kind":      kind,
        "first_seen": time.time(),
        "last_seen": time.time(),
        "count":     1,
    })
    bl[key] = fails
    _save_json(BLACKLIST_PATH, bl)
    log.info(f"Added to crash blacklist: {key} core+{core}/mem+{mem} ({kind})")
    # v3.1 — driver-level crashes (TDR, BSOD) at a specific offset are
    # evidence the offset itself is unsafe, not just unsafe-for-this-game.
    # Mirror to the cross-game blacklist so any other game's AT also
    # refuses this exact offset.  Game-only crashes (e.g. game.exe died
    # with a code-fault unrelated to OC) get kind="game-crash" and are
    # NOT mirrored.
    DRIVER_LEVEL_KINDS = {"tdr", "bsod", "wer", "driver", "thermal", "stuck-clock"}
    if (kind or "").lower() in DRIVER_LEVEL_KINDS:
        try:
            from core import adaptive_tuning
            adaptive_tuning.add_cross_game_blacklist(
                int(core), int(mem),
                source_exe=key, reason=kind,
            )
        except Exception as e:
            log.debug(f"cross-game blacklist mirror failed: {e}")


def compute_safe_step_down(exe: str, core: int, mem: int) -> dict:
    """Return the {core, mem} offset to step down to after a crash.

    Strategy: step both axes back by their respective fixed steps, then
    if that's still blacklisted (rare but possible), keep stepping until
    we find a safe value — but never below 0."""
    new_core = max(0, int(core) - STEP_DOWN_CORE_MHZ)
    new_mem  = max(0, int(mem)  - STEP_DOWN_MEM_MHZ)

    # Make sure the stepped-down values aren't themselves blacklisted.
    # If they are, keep stepping in 25/100 chunks until we find safety.
    safety_cap = 50
    while safety_cap > 0 and is_offset_blacklisted(exe, new_core, new_mem):
        new_core = max(0, new_core - STEP_DOWN_CORE_MHZ)
        new_mem  = max(0, new_mem  - STEP_DOWN_MEM_MHZ)
        if new_core == 0 and new_mem == 0:
            break
        safety_cap -= 1

    return {"core": new_core, "mem": new_mem}


def clear_blacklist(exe: str | None = None) -> dict:
    """Clear blacklist for one game or all games."""
    if exe is None:
        ok = _save_json(BLACKLIST_PATH, {})
        return {"ok": ok, "cleared": "all"}
    bl = _load_json(BLACKLIST_PATH, {})
    key = _norm_exe(exe)
    if key in bl:
        del bl[key]
        _save_json(BLACKLIST_PATH, bl)
        return {"ok": True, "cleared": key}
    return {"ok": True, "cleared": None, "msg": "not found"}


# ─── History ─────────────────────────────────────────────────────────────
def get_history(limit: int = 50) -> list:
    h = _load_json(HISTORY_PATH, [])
    if not isinstance(h, list):
        return []
    return list(h[-limit:])


def get_recent_crashes(exe: str | None = None,
                       within_sec: float = 86400) -> list:
    """v3.1 — used by adaptive_tuning's auto-revert detection.  Returns
    crash entries from the history that fall within `within_sec` of
    'now' and optionally match a specific exe.  History entries with
    no `ts` are skipped."""
    import time as _t
    cutoff = _t.time() - float(within_sec or 0)
    h = _load_json(HISTORY_PATH, [])
    if not isinstance(h, list):
        return []
    out = []
    target = _norm_exe(exe) if exe else None
    for entry in h:
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry.get("ts", 0))
        except Exception:
            continue
        if ts < cutoff:
            continue
        if target and _norm_exe(entry.get("exe", "")) != target:
            continue
        out.append(entry)
    return out


def _append_history(entry: dict) -> None:
    h = _load_json(HISTORY_PATH, [])
    if not isinstance(h, list):
        h = []
    h.append(entry)
    _save_json(HISTORY_PATH, h[-200:])   # cap at 200 entries


def clear_history() -> dict:
    return {"ok": _save_json(HISTORY_PATH, [])}


# ─── Crash classification ────────────────────────────────────────────────
def detect_crash(exit_ts: Optional[float] = None,
                 lookback_s: int = CRASH_LOOKBACK_S) -> dict:
    """Inspect Windows Event Log + in-app crash flag to decide whether
    the game exited normally or crashed.  Lookback window starts
    `lookback_s` BEFORE `exit_ts` (defaults to now - lookback_s).

    Returns:
      {
        'crashed': bool,
        'kind':    str,    # 'tdr' | 'driver_crash' | 'inapp_signal' | 'normal'
        'tdr_count': int,
        'crash_count': int,
        'inapp_state': dict | None,
        'events':  list,
      }
    """
    # Defer the heavy gpu_overclock import — keeps module load cheap.
    from core import gpu_overclock

    # 1) Windows Event Log TDR / driver crash
    drv = gpu_overclock.check_driver_events(seconds_back=lookback_s)
    tdr_count   = int(drv.get("tdr_count")   or 0) if drv.get("ok") else 0
    crash_count = int(drv.get("crash_count") or 0) if drv.get("ok") else 0
    events      = drv.get("events", []) if drv.get("ok") else []

    # 2) In-app crash flag (set by the live monitor / stress probe when
    #    pixel-checksum mismatch / context loss / frame timeout fires)
    inapp = {}
    try:
        inapp = gpu_overclock.get_crash_state() or {}
    except Exception:
        pass
    inapp_recent = False
    if inapp.get("crashed_ts"):
        age = time.time() - float(inapp["crashed_ts"])
        if age <= lookback_s + 5:
            inapp_recent = True

    if crash_count > 0:
        kind = "driver_crash"
        crashed = True
    elif tdr_count > 0:
        kind = "tdr"
        crashed = True
    elif inapp_recent:
        kind = "inapp_signal"
        crashed = True
    else:
        kind = "normal"
        crashed = False

    return {
        "crashed":     crashed,
        "kind":        kind,
        "tdr_count":   tdr_count,
        "crash_count": crash_count,
        "inapp_state": inapp if inapp.get("crashed_ts") else None,
        "events":      events[:5],
    }


def _proven_oc_floor() -> "tuple[int, int]":
    """beta.5 — the user's PROVEN OC (their saved manual profile) is the floor
    below which a crash is NOT attributable to an OC climb.  Returns (0, 0) if
    no profile is saved (pure-stock user) so any active OC is still evaluated."""
    try:
        from core import gpu_overclock
        p = gpu_overclock.load_profile()
        if not p or not p.get("exists"):
            return (0, 0)
        return (int(p.get("core_offset_mhz") or 0),
                int(p.get("mem_offset_mhz")  or 0))
    except Exception as e:
        log.debug(f"could not read proven OC floor: {e}")
        return (0, 0)


# ─── Main entry — called by game_profiles on every game exit ─────────────
def handle_game_exit(exe: str, display_name: str | None,
                     active_core: int, active_mem: int) -> dict:
    """Process a game-exit event.  Decides if it was a crash, blacklists,
    steps down, notifies.  Idempotent — safe to call on normal exits too
    (it just returns early).

    Args:
      exe:          executable name (e.g. 'cs2.exe')
      display_name: friendly name for the toast
      active_core:  core offset MHz that was active when the game ran
      active_mem:   mem offset MHz that was active when the game ran

    Returns:
      {'crashed': bool, 'kind': str, 'step_down': {...} or None,
       'blacklisted': bool, 'notification_fired': bool}
    """
    detection = detect_crash()
    if not detection["crashed"]:
        return {
            "crashed":            False,
            "kind":               "normal",
            "step_down":          None,
            "blacklisted":        False,
            "notification_fired": False,
        }

    name = display_name or exe or "the game"
    log.warning(f"⚠ {name} crashed during play (kind={detection['kind']}, "
                f"tdr={detection['tdr_count']}, crash={detection['crash_count']}, "
                f"oc=core+{active_core}/mem+{active_mem})")

    # beta.5 — Only blame the OC if it was pushed ABOVE the user's PROVEN
    # baseline (their saved manual OC profile).  A crash at/below the proven
    # floor is not an OC-climb fault — it's the game/driver — so we must NOT
    # blacklist it (blacklisting is >=, so blacklisting the baseline would make
    # Adaptive Tuning refuse the user's own proven OC and ratchet it downward).
    # This is what protects a startup-guard crash (card sitting at baseline)
    # from poisoning the baseline.
    floor_core, floor_mem = _proven_oc_floor()
    oc_climbed = (active_core > floor_core) or (active_mem > floor_mem)
    blacklisted = False
    step_down: dict | None = None
    if oc_climbed:
        add_to_blacklist(exe, active_core, active_mem, detection["kind"])
        blacklisted = True
        step_down = compute_safe_step_down(exe, active_core, active_mem)

        # beta.5 — CRITICAL: NEVER write the user's saved manual OC profile
        # here.  The old code called gpu_overclock.save_profile({...,
        # apply_on_startup:True}, auto=True), which overwrote the user's proven
        # baseline with a crash-derived value AND re-armed an unproven OC at
        # boot — the exact data-corruption + crash-on-startup pair the beta.5
        # incident was about.  The saved profile is immutable ground truth.  We
        # still apply a TRANSIENT step-down to the card so the user isn't left
        # sitting on the crashing clock; Adaptive Tuning's own state + the crash
        # blacklist (read by _is_safe_to_step_to / _detect_auto_revert) carry
        # the persistent learning — not the user's profile.
        try:
            from core import gpu_overclock
            apply_r = gpu_overclock.apply_oc(
                core_offset_mhz=step_down["core"],
                mem_offset_mhz=step_down["mem"],
                power_pct=100,                  # don't touch the power slider
            )
            log.info(
                f"Crash auto-recovery: transient step-down to core+"
                f"{step_down['core']}/mem+{step_down['mem']} "
                f"(apply_ok={apply_r.get('ok')}) — user profile left untouched"
            )
        except Exception as e:
            log.error(f"Crash auto-recovery failed: {e}")
    elif active_core > 0 or active_mem > 0:
        log.info(
            f"{name} crashed at/below the proven OC floor "
            f"(core+{active_core}/mem+{active_mem} ≤ core+{floor_core}/"
            f"mem+{floor_mem}) — not blaming the OC, no blacklist/step-down"
        )

    # ── Toast within 5 s ───────────────────────────────────────────────
    notif_ok = False
    try:
        from core import notifier
        if blacklisted and step_down is not None:
            title = f"{name} crashed"
            msg = (f"GPU TDR detected at core+{active_core} / mem+{active_mem}. "
                   f"Stepped down to core+{step_down['core']} / "
                   f"mem+{step_down['mem']} and saved as the new profile.")
        elif step_down is None and (active_core or active_mem):
            title = f"{name} crashed"
            msg = (f"Detected a {detection['kind']} during play, but no OC "
                   f"step-down was needed.")
        else:
            title = f"{name} crashed"
            msg = ("Detected a driver crash during play.  GhostShell wasn't "
                   "applying any OC, so this looks like a game / driver issue.")
        notifier.toast(title, msg, force=True)
        notif_ok = True
    except Exception as e:
        log.debug(f"crash toast failed: {e}")

    # ── History ────────────────────────────────────────────────────────
    _append_history({
        "ts":           time.time(),
        "exe":          _norm_exe(exe),
        "display_name": display_name,
        "kind":         detection["kind"],
        "tdr_count":    detection["tdr_count"],
        "crash_count":  detection["crash_count"],
        "active_core":  active_core,
        "active_mem":   active_mem,
        "blacklisted":  blacklisted,
        "step_down":    step_down,
        "events":       detection["events"][:3],
    })

    return {
        "crashed":            True,
        "kind":               detection["kind"],
        "step_down":          step_down,
        "blacklisted":        blacklisted,
        "notification_fired": notif_ok,
    }
