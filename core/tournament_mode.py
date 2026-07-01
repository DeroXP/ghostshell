"""Tournament Mode — one-button "before a match" macro.

When ON:
  - Background Pauser ON + pause now (browsers + AI apps frozen)
  - AT mode → competitive for whatever game is currently running
  - High Performance power plan
  - System toast notifications disabled
  - Windows Update service paused (no auto-reboot popup mid-match)
  - Search Indexer (WSearch) stopped
  - Win11 Widgets service stopped
  - OneDrive killed (if running) — eliminates I/O bursts
  - Optional auto-timer (default 60 min) — auto-reverts on expiry

When OFF:
  - Every change above is rolled back from the saved baseline.
  - Even if GhostShell crashes mid-tournament, the boot-time check
    `recover_if_stale()` catches a stuck enabled-state and reverts.

Persistence file: %APPDATA%\\GhostShell\\tournament_mode.json
    {
      "enabled": bool,
      "started_at": ts,
      "expires_at": ts | 0,         # 0 = no auto-timer
      "duration_min": 60,
      "baseline": {
          "power_plan_guid": "...",
          "wuauserv_running": bool,
          "wsearch_running": bool,
          "widgets_running": bool,
          "onedrive_was_running": bool,
          "toasts_enabled": bool,
          "at_mode_changes": [{"exe": "...", "prev_mode": "..."}],
          "pauser_was_enabled": bool
      },
      "last_actions": [str, ...]    # cosmetic: shown in UI
    }
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
from typing import Optional

from core.utils import get_logger, run_cmd, run_ps, atomic_write_json
from config import APPDATA_DIR

log = get_logger(__name__)

STATE_PATH = os.path.join(APPDATA_DIR, "tournament_mode.json")

# Power plan GUIDs (well-known)
POWER_HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
POWER_BALANCED  = "381b4222-f694-41f0-9685-ff5bb260df2e"
POWER_ULTIMATE  = "e9a42b02-d5df-448d-aa00-03f14749eb61"  # Pro/Workstation only


# ────────────────────────────────────────────────────────────────────
# Persistence
# ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {
            "enabled": False, "started_at": 0, "expires_at": 0,
            "duration_min": 60, "baseline": {}, "last_actions": [],
        }


def _save_state(state: dict):
    atomic_write_json(STATE_PATH, state)


def _wipe_state():
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────
# Probes (read current system state — used to build baseline)
# ────────────────────────────────────────────────────────────────────

def _probe_active_power_plan() -> str:
    r = run_cmd(["powercfg", "/getactivescheme"], timeout=5)
    if not r.get("ok"):
        return ""
    m = re.search(r"GUID:\s*([0-9a-f-]{36})", r.get("out", ""), re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _probe_service_running(name: str) -> bool:
    r = run_ps(f"try {{ (Get-Service -Name '{name}' -ErrorAction Stop).Status }} catch {{ '' }}",
               timeout=5)
    return "Running" in (r.get("out") or "")


def _probe_process_running(exe_name: str) -> bool:
    r = run_ps(f"@(Get-Process -Name '{exe_name.replace('.exe','')}' -ErrorAction SilentlyContinue).Count",
               timeout=5)
    try:
        return int((r.get("out") or "0").strip()) > 0
    except ValueError:
        return False


def _probe_toasts_enabled() -> bool:
    """Toast (notification) global toggle.  Reading absent = enabled."""
    r = run_cmd(["reg", "query",
                 r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings",
                 "/v", "NOC_GLOBAL_SETTING_TOASTS_ENABLED"], timeout=5)
    out = r.get("out") or ""
    if "REG_DWORD" not in out:
        return True   # value absent → toasts enabled (default)
    m = re.search(r"0x([0-9a-f]+)", out, re.IGNORECASE)
    if not m:
        return True
    return int(m.group(1), 16) != 0


# ────────────────────────────────────────────────────────────────────
# Mutators (apply / revert each tweak)
# ────────────────────────────────────────────────────────────────────

def _set_power_plan(guid: str) -> bool:
    if not guid:
        return False
    r = run_cmd(["powercfg", "/setactive", guid], timeout=5)
    return bool(r.get("ok"))


def _stop_service(name: str) -> bool:
    r = run_ps(f"try {{ Stop-Service -Name '{name}' -Force -ErrorAction Stop; 'OK' }} "
               f"catch {{ 'FAIL: ' + $_.Exception.Message }}", timeout=10)
    return "OK" in (r.get("out") or "")


def _start_service(name: str) -> bool:
    r = run_ps(f"try {{ Start-Service -Name '{name}' -ErrorAction Stop; 'OK' }} "
               f"catch {{ 'FAIL: ' + $_.Exception.Message }}", timeout=10)
    return "OK" in (r.get("out") or "")


def _kill_process(exe_name: str) -> bool:
    proc = exe_name.replace(".exe", "")
    r = run_ps(f"try {{ Stop-Process -Name '{proc}' -Force -ErrorAction Stop; 'OK' }} "
               f"catch {{ 'FAIL: ' + $_.Exception.Message }}", timeout=10)
    return "OK" in (r.get("out") or "")


def _launch_onedrive() -> bool:
    """Restart OneDrive after Tournament Mode ends."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\OneDrive\OneDrive.exe"),
        r"C:\Program Files\Microsoft OneDrive\OneDrive.exe",
        r"C:\Program Files (x86)\Microsoft OneDrive\OneDrive.exe",
    ]
    exe = next((c for c in candidates if os.path.isfile(c)), None)
    if not exe:
        return False
    try:
        # Detached so it survives GhostShell exiting
        import subprocess
        subprocess.Popen([exe, "/background"], close_fds=True,
                         creationflags=0x00000008)  # DETACHED_PROCESS
        return True
    except OSError:
        return False


def _set_toasts(enabled: bool) -> bool:
    val = 1 if enabled else 0
    r = run_cmd(["reg", "add",
                 r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings",
                 "/v", "NOC_GLOBAL_SETTING_TOASTS_ENABLED",
                 "/t", "REG_DWORD", "/d", str(val), "/f"], timeout=5)
    return bool(r.get("ok"))


# ────────────────────────────────────────────────────────────────────
# Enable / Disable
# ────────────────────────────────────────────────────────────────────

def get_status() -> dict:
    state = _load_state()
    if state.get("enabled") and state.get("expires_at"):
        state["seconds_remaining"] = max(0, int(state["expires_at"] - time.time()))
    else:
        state["seconds_remaining"] = 0
    return state


def enable(duration_min: int = 60) -> dict:
    """Capture baseline, apply every Tournament tweak, persist state.

    duration_min == 0 means 'no auto-timer — leave on until manually disabled'.
    """
    cur = _load_state()
    if cur.get("enabled"):
        return {"ok": False, "err": "Tournament Mode already enabled",
                "status": get_status()}

    log.info(f"Tournament Mode: ENABLING for {duration_min} min")
    actions: list[str] = []
    baseline: dict = {}

    # 1. Background Pauser
    try:
        from core import background_pauser
        bp_settings = background_pauser.get_settings()
        baseline["pauser_was_enabled"] = bool(bp_settings.get("enabled"))
        if not bp_settings.get("enabled"):
            background_pauser.set_settings(enabled=True)
        r = background_pauser.pause_now(reason="tournament-enable")
        if r.get("ok") and r.get("paused"):
            actions.append(f"Paused {len(r['paused'])} bg apps "
                           f"(~{r['total_freed_mb']} MB freed)")
        else:
            actions.append("Bg pauser: nothing eligible to pause")
    except Exception as e:
        log.warning(f"pauser hook failed: {e}")
        actions.append(f"Pauser: error ({e})")

    # 2. AT mode → competitive for the active game
    baseline["at_mode_changes"] = []
    try:
        from core import game_profiles, adaptive_tuning
        active = game_profiles.get_active_game_info()
        if active and active.get("exe"):
            exe = active["exe"]
            cur_state = adaptive_tuning.get_game_state(exe) or {}
            prev_mode = cur_state.get("mode") or "balanced"
            if prev_mode != "competitive":
                adaptive_tuning.set_game_mode(exe, "competitive")
                baseline["at_mode_changes"].append(
                    {"exe": exe, "prev_mode": prev_mode})
                actions.append(f"AT mode → competitive for {exe} "
                               f"(was {prev_mode})")
    except Exception as e:
        log.debug(f"AT mode swap failed: {e}")

    # 3. Power plan → High Performance
    try:
        baseline["power_plan_guid"] = _probe_active_power_plan()
        if _set_power_plan(POWER_HIGH_PERF):
            actions.append("Power plan → High Performance")
        else:
            actions.append("Power plan: failed (kept current)")
    except Exception as e:
        log.debug(f"power plan swap failed: {e}")

    # 4. Toasts off
    try:
        baseline["toasts_enabled"] = _probe_toasts_enabled()
        if baseline["toasts_enabled"]:
            _set_toasts(False)
            actions.append("Notifications → off")
    except Exception as e:
        log.debug(f"toast toggle failed: {e}")

    # 5. Stop services
    for svc, label in [("wuauserv", "Windows Update"),
                       ("WSearch", "Search Indexer"),
                       ("WidgetService", "Widgets")]:
        try:
            running = _probe_service_running(svc)
            baseline[f"{svc.lower()}_running"] = running
            if running:
                if _stop_service(svc):
                    actions.append(f"{label} service: stopped")
        except Exception as e:
            log.debug(f"stop service {svc} failed: {e}")

    # 6. Kill OneDrive (sync hammers the disk)
    try:
        baseline["onedrive_was_running"] = _probe_process_running("onedrive.exe")
        if baseline["onedrive_was_running"]:
            if _kill_process("onedrive.exe"):
                actions.append("OneDrive sync: stopped")
    except Exception as e:
        log.debug(f"onedrive kill failed: {e}")

    # ── Persist final state ────────────────────────────────────────
    now = time.time()
    expires = now + (duration_min * 60) if duration_min > 0 else 0
    state = {
        "enabled": True,
        "started_at": now,
        "expires_at": expires,
        "duration_min": int(duration_min),
        "baseline": baseline,
        "last_actions": actions,
    }
    _save_state(state)

    # Spin up the auto-timer thread (no-op if duration == 0)
    if expires:
        _start_timer_thread()

    log.info(f"Tournament Mode ON — {len(actions)} actions taken, "
             f"expires {time.strftime('%H:%M:%S', time.localtime(expires)) if expires else 'never'}")
    return {"ok": True, "actions": actions, "status": get_status()}


def disable(reason: str = "manual") -> dict:
    """Roll back every change captured in baseline."""
    state = _load_state()
    if not state.get("enabled"):
        return {"ok": False, "err": "Tournament Mode is not enabled"}

    log.info(f"Tournament Mode: DISABLING (reason: {reason})")
    baseline = state.get("baseline", {}) or {}
    actions: list[str] = []

    # 1. Resume bg pauser + restore prior enable state
    try:
        from core import background_pauser
        r = background_pauser.resume_now(reason=f"tournament-disable: {reason}")
        if r.get("resumed"):
            actions.append(f"Resumed {len(r['resumed'])} bg apps")
        if not baseline.get("pauser_was_enabled", False):
            background_pauser.set_settings(enabled=False)
            actions.append("Bg pauser: turned back off")
    except Exception as e:
        log.debug(f"pauser restore failed: {e}")

    # 2. Restore AT modes
    try:
        from core import adaptive_tuning
        for change in baseline.get("at_mode_changes", []):
            try:
                adaptive_tuning.set_game_mode(change["exe"], change["prev_mode"])
                actions.append(f"AT mode → {change['prev_mode']} for {change['exe']}")
            except Exception as e:
                log.debug(f"AT restore for {change['exe']} failed: {e}")
    except Exception as e:
        log.debug(f"AT module import failed: {e}")

    # 3. Power plan
    prev_guid = baseline.get("power_plan_guid")
    if prev_guid:
        try:
            if _set_power_plan(prev_guid):
                actions.append("Power plan: restored")
        except Exception as e:
            log.debug(f"power plan restore failed: {e}")

    # 4. Toasts
    if baseline.get("toasts_enabled"):
        try:
            _set_toasts(True)
            actions.append("Notifications → restored")
        except Exception as e:
            log.debug(f"toast restore failed: {e}")

    # 5. Restart services
    for svc, label in [("wuauserv", "Windows Update"),
                       ("WSearch", "Search Indexer"),
                       ("WidgetService", "Widgets")]:
        if baseline.get(f"{svc.lower()}_running"):
            try:
                if _start_service(svc):
                    actions.append(f"{label} service: restarted")
            except Exception as e:
                log.debug(f"start service {svc} failed: {e}")

    # 6. OneDrive
    if baseline.get("onedrive_was_running"):
        try:
            if _launch_onedrive():
                actions.append("OneDrive: relaunched")
        except Exception as e:
            log.debug(f"onedrive relaunch failed: {e}")

    # Wipe state
    _wipe_state()
    log.info(f"Tournament Mode OFF — {len(actions)} restore actions taken")
    return {"ok": True, "actions": actions}


# ────────────────────────────────────────────────────────────────────
# Auto-timer
# ────────────────────────────────────────────────────────────────────

_timer_thread: Optional[threading.Thread] = None
_timer_stop = threading.Event()


def _timer_loop():
    while not _timer_stop.is_set():
        time.sleep(15)
        try:
            state = _load_state()
            if not state.get("enabled"):
                break
            exp = state.get("expires_at", 0) or 0
            if exp and time.time() >= exp:
                log.info("Tournament Mode timer expired — auto-disabling")
                disable(reason="timer")
                break
        except Exception as e:
            log.debug(f"timer loop tick failed: {e}")


def _start_timer_thread():
    global _timer_thread
    _timer_stop.clear()
    if _timer_thread and _timer_thread.is_alive():
        return
    _timer_thread = threading.Thread(target=_timer_loop, daemon=True,
                                     name="tournament-timer")
    _timer_thread.start()


def extend_timer(extra_min: int) -> dict:
    state = _load_state()
    if not state.get("enabled"):
        return {"ok": False, "err": "Tournament Mode not enabled"}
    if not state.get("expires_at"):
        return {"ok": False, "err": "Currently no auto-timer (indefinite mode)"}
    state["expires_at"] += extra_min * 60
    state["duration_min"] = state.get("duration_min", 0) + int(extra_min)
    _save_state(state)
    log.info(f"Tournament Mode timer extended by {extra_min} min")
    return {"ok": True, "status": get_status()}


# ────────────────────────────────────────────────────────────────────
# Boot-time stale-state recovery
# ────────────────────────────────────────────────────────────────────

def recover_if_stale() -> dict:
    """Call at GhostShell startup.  Detects:
      a) enabled=true with expired timer  → disable+restore
      b) enabled=true with no expiry but started >24h ago → disable+restore
      c) enabled=true and timer still valid → restart timer thread"""
    state = _load_state()
    if not state.get("enabled"):
        return {"ok": True, "stale": False}

    now = time.time()
    exp = state.get("expires_at", 0) or 0
    started = state.get("started_at", 0) or 0

    is_stale = (
        (exp and now >= exp) or
        (not exp and started and now - started > 86_400)
    )
    if is_stale:
        log.warning("Tournament Mode: detected stale enabled state from previous "
                    "run — auto-disabling now")
        return disable(reason="stale-recovery")

    # Still valid — restart the timer thread so it fires on time
    if exp:
        _start_timer_thread()
    return {"ok": True, "stale": False, "status": get_status()}
