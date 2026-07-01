"""Auto-recovery for tweaks GhostShell used to apply that turned out
to be actively harmful.

Background
──────────
GhostShell shipped with a wide net of Windows optimizations.  The
v3.3.1 pre-stable audit (see beta.7's commit message) identified
several tweaks that older builds had applied which actively break
real user workflows:

  - Bluetooth absolute-volume disable → AirPods / Sony XM / QuadCast
    stutter & one-channel-only on modern Windows
  - `recoveryenabled No` → users can't access Startup Repair when a
    bad update breaks boot
  - Scheduled Defrag task disabled → silently disables weekly TRIM
    on SSDs (the task isn't actually defragging; it's running TRIM)
  - FSO=2 → breaks Roblox Studio / Unity / Unreal Editor viewport
  - PreventIndexingOutlook=1 → breaks Outlook search
  - LetAppsAccessNotifications=2 → kills Discord/Steam toasts
  - Ndu Start=4 → blanks Task Manager network tab (fixed bug from 2017)
  - Memory placebos (DisablePagingExecutive, LargeSystemCache,
    ClearPageFileAtShutdown) → no perf benefit, ClearPageFile adds
    30-120s to shutdown
  - HubMode=1 → no-op on Win11 23H2+ (orphan policy key)
  - SystemResponsiveness=0 → starves the Windows audio service
  - bcdedit useplatformtick yes → forces HPET on modern TSC systems

Users who upgraded from old GhostShell builds still have those
settings in the registry / BCD store.  Apply All no longer applies
them, but they don't get undone on upgrade.  This module fixes that
on boot — once.

How it works
────────────
1. Boot hook calls `recover_legacy_bad_tweaks()`.
2. The function checks each known-bad setting via `reg query` /
   `bcdedit /enum` / `Get-ScheduledTask`.
3. If still applied, it reverts to the safe default.
4. Whatever it reverted gets written to `recovery_report.json` in
   APPDATA.
5. UI on first paint reads that file via `/api/recover/last-report`
   and shows a one-time apology toast/modal listing what changed.
6. After the user acknowledges, the report file gets renamed with a
   `.seen` suffix so it doesn't fire again.

Idempotent — safe to call repeatedly; only reports changes the
first time each bad value is observed.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from config import APPDATA_DIR
from core.utils import run_cmd, run_ps, get_logger, atomic_write_json

log = get_logger("auto_recover")


# Where the recovery report lives.  The UI reads this once on first
# paint after upgrade and renames it so it doesn't fire again.
_REPORT_PATH       = os.path.join(APPDATA_DIR, "recovery_report.json")
_REPORT_SEEN_PATH  = _REPORT_PATH + ".seen"


# ─── Recovery primitives ───────────────────────────────────────────

def _reg_query(key: str, value: str) -> Optional[str]:
    """Read a registry value.  Returns the value's data string, or
    None if the key/value doesn't exist."""
    r = run_cmd(["reg", "query", key, "/v", value], timeout=5)
    if not r.get("ok"):
        return None
    # Output looks like:
    #   <key>
    #       <value>    REG_DWORD    0x1
    for line in (r.get("out") or "").splitlines():
        line = line.strip()
        if line.startswith(value + " ") or line.startswith(value + "\t"):
            parts = line.split()
            if len(parts) >= 3:
                return parts[-1]
    return None


def _reg_delete(key: str, value: str) -> bool:
    r = run_cmd(["reg", "delete", key, "/v", value, "/f"], timeout=5)
    return bool(r.get("ok"))


def _reg_set_dword(key: str, value: str, data: str) -> bool:
    r = run_cmd(["reg", "add", key, "/v", value, "/t", "REG_DWORD",
                  "/d", data, "/f"], timeout=5)
    return bool(r.get("ok"))


# ─── Individual tweak checks ───────────────────────────────────────
#
# Each function: probe → if applied, revert → return a dict or None.
# Return shape: { id, label, action, restored_to } when something was
# reverted; None when nothing needed doing.

def _check_bluetooth_absolute_volume() -> Optional[dict]:
    cur = _reg_query(
        r"HKLM\SYSTEM\CurrentControlSet\Control\Bluetooth\Audio\AVRCP\CT",
        "DisableAbsoluteVolume",
    )
    if cur and cur.lower() in ("0x1", "0x00000001"):
        _reg_delete(r"HKLM\SYSTEM\CurrentControlSet\Control\Bluetooth\Audio\AVRCP\CT",
                    "DisableAbsoluteVolume")
        return {
            "id":             "bluetooth_absolute_volume",
            "label":          "Bluetooth absolute volume re-enabled",
            "explanation":    "An older GhostShell build disabled this — caused "
                              "audio stutter and one-channel-only playback on "
                              "modern headsets (AirPods, Sony XM, HyperX QuadCast, etc).",
            "action":         "removed registry value DisableAbsoluteVolume",
        }
    return None


def _check_recoveryenabled() -> Optional[dict]:
    r = run_cmd(["bcdedit", "/enum", "{current}"], timeout=5)
    if not r.get("ok"):
        return None
    for line in (r.get("out") or "").splitlines():
        line = line.strip().lower()
        if line.startswith("recoveryenabled") and "no" in line:
            run_cmd(["bcdedit", "/set", "{current}", "recoveryenabled", "Yes"], timeout=5)
            return {
                "id":          "winre_disabled",
                "label":       "Windows Recovery Environment re-enabled",
                "explanation": "An older GhostShell build set `recoveryenabled No` "
                               "in the boot config.  That prevented you from accessing "
                               "Startup Repair / Reset this PC / System Restore when "
                               "a bad update broke boot — you'd have needed a Windows "
                               "installation USB to recover.",
                "action":      "bcdedit /set {current} recoveryenabled Yes",
            }
    return None


def _check_ssd_defrag_task() -> Optional[dict]:
    r = run_ps(
        "(Get-ScheduledTask -TaskName 'ScheduledDefrag' -ErrorAction SilentlyContinue).State",
        timeout=8,
    )
    out = (r.get("out") or "").strip()
    if out and out.lower() == "disabled":
        run_ps(
            "Get-ScheduledTask -TaskName 'ScheduledDefrag' -ErrorAction SilentlyContinue | "
            "Enable-ScheduledTask -ErrorAction SilentlyContinue",
            timeout=8,
        )
        return {
            "id":          "ssd_defrag_disabled",
            "label":       "Weekly SSD TRIM schedule re-enabled",
            "explanation": "An older GhostShell build disabled the 'ScheduledDefrag' "
                           "task, thinking it was defragging your SSD.  It actually "
                           "runs TRIM/Retrim — the operation SSDs need to maintain "
                           "long-term performance.  Disabling it silently degrades "
                           "SSD speed over months.",
            "action":      "Enable-ScheduledTask ScheduledDefrag",
        }
    return None


def _check_fso_global() -> Optional[dict]:
    cur = _reg_query(r"HKCU\System\GameConfigStore", "GameDVR_FSEBehaviorMode")
    if cur and cur.lower() in ("0x2", "0x00000002"):
        _reg_set_dword(r"HKCU\System\GameConfigStore",
                        "GameDVR_FSEBehaviorMode", "0")
        return {
            "id":          "fso_global",
            "label":       "Fullscreen Optimizations restored to default",
            "explanation": "An older GhostShell build set GameDVR_FSEBehaviorMode=2 "
                           "(global FSO disable).  That breaks viewport rendering in "
                           "Roblox Studio, Unity Editor, Unreal Editor, and most 3D "
                           "modeling tools — they render at black or wrong scale.",
            "action":      "GameDVR_FSEBehaviorMode 2 → 0 (Windows default)",
        }
    return None


def _check_prevent_indexing_outlook() -> Optional[dict]:
    cur = _reg_query(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search",
                      "PreventIndexingOutlook")
    if cur and cur.lower() not in ("0x0", "0x00000000"):
        _reg_delete(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search",
                    "PreventIndexingOutlook")
        return {
            "id":          "outlook_indexing",
            "label":       "Outlook search re-enabled",
            "explanation": "An older GhostShell build set PreventIndexingOutlook=1 "
                           "with a label claiming 'search indexer auto-throttle'.  "
                           "The actual effect was just to break Outlook's inbox "
                           "search — emails wouldn't show up in search results.",
            "action":      "removed PreventIndexingOutlook policy",
        }
    return None


def _check_notifications_deny() -> Optional[dict]:
    cur = _reg_query(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                      "LetAppsAccessNotifications")
    if cur and cur.lower() in ("0x2", "0x00000002"):
        _reg_delete(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                    "LetAppsAccessNotifications")
        return {
            "id":          "notifications_deny",
            "label":       "App notifications re-enabled",
            "explanation": "An older GhostShell build denied LetAppsAccessNotifications.  "
                           "That killed toast notifications for Discord, Steam friends, "
                           "Slack, Teams, OBS, and basically every modern app that "
                           "uses the Windows notification system.",
            "action":      "removed LetAppsAccessNotifications=2 policy",
        }
    return None


def _check_ndu_disabled() -> Optional[dict]:
    cur = _reg_query(r"HKLM\SYSTEM\CurrentControlSet\Services\Ndu", "Start")
    if cur and cur.lower() in ("0x4", "0x00000004"):
        _reg_set_dword(r"HKLM\SYSTEM\CurrentControlSet\Services\Ndu", "Start", "2")
        return {
            "id":          "ndu_disabled",
            "label":       "Network Data Usage tracking re-enabled",
            "explanation": "An older GhostShell build disabled the NDU driver, citing "
                           "a 'memory leak'.  That leak was a Windows 8 bug Microsoft "
                           "fixed in 2017.  The only thing disabling NDU does now is "
                           "blank Task Manager's per-process Network tab.",
            "action":      "Ndu Start 4 (disabled) → 2 (auto)",
        }
    return None


def _check_memory_placebos() -> list[dict]:
    """Remove the no-op memory placebos.  None of these moved on
    Win10/11 with ≥8 GB RAM."""
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    reverted = []
    # DisablePagingExecutive=1 — set it back to 0 (default)
    if _reg_query(base, "DisablePagingExecutive") in ("0x1", "0x00000001"):
        _reg_set_dword(base, "DisablePagingExecutive", "0")
        reverted.append({
            "id":          "paging_executive",
            "label":       "DisablePagingExecutive set back to default",
            "explanation": "An older GhostShell build set this to 1.  On modern "
                           "Windows with ≥8 GB RAM this is a no-op (the kernel "
                           "memory manager keeps the kernel resident under "
                           "pressure-free RAM regardless).",
            "action":      "DisablePagingExecutive 1 → 0",
        })
    # ClearPageFileAtShutdown=1 — actively makes shutdown 30-120s slower
    if _reg_query(base, "ClearPageFileAtShutdown") in ("0x1", "0x00000001"):
        _reg_set_dword(base, "ClearPageFileAtShutdown", "0")
        reverted.append({
            "id":          "clear_pagefile_at_shutdown",
            "label":       "ClearPageFileAtShutdown set back to default",
            "explanation": "An older GhostShell build set this to 1 with a 'perf' "
                           "label.  It's actually a security/forensics setting "
                           "that adds 30-120 seconds to shutdown by zeroing the "
                           "entire pagefile — opposite of a perf tweak.",
            "action":      "ClearPageFileAtShutdown 1 → 0",
        })
    return reverted


def _check_system_responsiveness() -> Optional[dict]:
    """v3.3.1-beta.7 set this to 10; older builds set it to 0.  0
    starves the Windows audio service and causes Discord / in-game
    voice crackling under load."""
    cur = _reg_query(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
                      "SystemResponsiveness")
    if cur and cur.lower() in ("0x0", "0x00000000"):
        _reg_set_dword(
            r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
            "SystemResponsiveness", "10",
        )
        return {
            "id":          "system_responsiveness_zero",
            "label":       "Audio service breathing room restored",
            "explanation": "An older GhostShell build set SystemResponsiveness=0, "
                           "which dedicated 100% of multimedia bandwidth to "
                           "foreground threads — starving the Windows audio "
                           "service.  Symptoms: Discord crackling, in-game voice "
                           "stuttering under CPU load.  Microsoft's recommended "
                           "gaming value is 10 (= 90% foreground, 10% audio).",
            "action":      "SystemResponsiveness 0 → 10",
        }
    return None


def _check_useplatformtick() -> Optional[dict]:
    r = run_cmd(["bcdedit", "/enum", "{current}"], timeout=5)
    if not r.get("ok"):
        return None
    if "useplatformtick" in (r.get("out") or "").lower():
        run_cmd(["bcdedit", "/deletevalue", "{current}", "useplatformtick"], timeout=5)
        return {
            "id":          "useplatformtick",
            "label":       "Platform-tick BCD flag removed",
            "explanation": "An older GhostShell build set `useplatformtick yes` "
                           "in the boot config.  On Zen2+/Skylake+ systems this "
                           "forces the kernel to use HPET/ACPI for tick interrupts "
                           "instead of TSC — RAISES idle wakeups instead of "
                           "lowering them.  Removed.",
            "action":      "bcdedit /deletevalue useplatformtick",
        }
    return None


def _check_hubmode() -> Optional[dict]:
    cur = _reg_query(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer",
                      "HubMode")
    if cur and cur.lower() in ("0x1", "0x00000001"):
        _reg_delete(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", "HubMode")
        return {
            "id":          "hubmode",
            "label":       "HubMode orphan policy removed",
            "explanation": "An older GhostShell build set HubMode=1 to hide Quick "
                           "Access from File Explorer's nav pane.  Microsoft removed "
                           "honoring of this registry value in Win11 23H2+, so the "
                           "value just sits as an orphan policy — removed cleanly.",
            "action":      "removed HubMode value",
        }
    return None


# ─── Public entry ──────────────────────────────────────────────────

def recover_legacy_bad_tweaks() -> dict:
    """Run all the checks above, revert anything found, write a report.

    Idempotent — safe to call on every boot.  Once a report has been
    written and the UI marks it seen (rename to `.seen`), subsequent
    boots find nothing to revert (since the values are already
    reverted) and write nothing new."""
    log.info("Checking for legacy bad-tweak residue…")
    checks = [
        _check_bluetooth_absolute_volume,
        _check_recoveryenabled,
        _check_ssd_defrag_task,
        _check_fso_global,
        _check_prevent_indexing_outlook,
        _check_notifications_deny,
        _check_ndu_disabled,
        _check_system_responsiveness,
        _check_useplatformtick,
        _check_hubmode,
    ]
    reverted: list[dict] = []
    for fn in checks:
        try:
            r = fn()
            if r:
                reverted.append(r)
                log.info(f"  reverted: {r['label']}")
        except Exception as e:
            log.debug(f"  recovery check {fn.__name__} raised: {e}")
    # Memory placebos return a list, handle separately
    try:
        reverted.extend(_check_memory_placebos())
    except Exception as e:
        log.debug(f"  memory placebo check raised: {e}")

    if reverted:
        try:
            atomic_write_json(_REPORT_PATH, {
                "ts":         time.time(),
                "count":      len(reverted),
                "reverted":   reverted,
            })
            log.info(f"Auto-recover: reverted {len(reverted)} legacy bad tweaks; "
                     f"report at {_REPORT_PATH}")
        except Exception as e:
            log.warning(f"Could not write recovery report: {e}")
    else:
        log.info("Auto-recover: no legacy bad tweaks found.")

    return {"ok": True, "reverted": reverted}


def get_pending_recovery_report() -> dict:
    """UI calls this on first paint.  Returns the unseen report if
    any, or {pending: False}."""
    if not os.path.isfile(_REPORT_PATH):
        return {"pending": False}
    try:
        with open(_REPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return {"pending": True, **data}
    except Exception as e:
        log.debug(f"get_pending_recovery_report: {e}")
        return {"pending": False}


def mark_recovery_seen() -> dict:
    """UI calls this once the user has acknowledged the toast / modal.
    Renames the report to .seen so it doesn't fire on next boot."""
    if not os.path.isfile(_REPORT_PATH):
        return {"ok": True, "was_pending": False}
    try:
        # Atomic-ish: remove any old .seen then rename
        try: os.remove(_REPORT_SEEN_PATH)
        except OSError: pass
        os.rename(_REPORT_PATH, _REPORT_SEEN_PATH)
        return {"ok": True, "was_pending": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}
