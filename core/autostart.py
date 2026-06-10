"""GhostShell start-on-boot management — Scheduled Task edition (v2.9.7).

WHY THE REWRITE
---------------
The earlier (v2.9.5 / v2.9.6) implementation used `HKCU\\…\\Run` to register a
PowerShell command line that did `Start-Sleep -Seconds N; Start-Process
GhostShell.exe`.  Two real problems with that:

  1. A PowerShell process is alive in the user's session for the entire
     delay window.  It's hidden (`-WindowStyle Hidden`) but it shows up
     in Task Manager — and "powershell.exe with no parent app" is the
     exact thing security-conscious users right-click → End Task on.
     If they kill it, GhostShell never launches.

  2. Run-key entries inherit the launching user's privileges.  Since
     GhostShell.exe has `uac_admin=True`, the launch would trigger a
     UAC prompt at every login — the kind of thing that trains users
     to click "Yes" on UAC dialogs without reading them.

Scheduled Tasks fix both:
  * The Task Scheduler service handles the delay — no user-visible
    process exists during the wait.
  * A task with `RunLevel="HighestAvailable"` runs elevated WITHOUT a
    UAC prompt at login.  This is the same trick Steam, Discord,
    OneDrive, etc. use to launch elevated quietly.

Implementation:
  * `enable()`  → writes a tiny XML, imports it via `schtasks /Create /XML`.
  * `disable()` → `schtasks /Delete /TN <name> /F`.
  * `set_delay()` → re-write the XML with a new <Delay>PTNNs</Delay>.
  * `get_status()` → `schtasks /Query /TN <name>` to confirm registration.

Scheduled task creation requires admin rights, which GhostShell already has
(`uac_admin=True` in the build spec), so we're not adding any new privilege
requirement.

MIGRATION
---------
Any users who enabled autostart on v2.9.5/v2.9.6 have a leftover entry in
`HKCU\\…\\Run`.  On enable() we always wipe that entry too so the OS
doesn't end up running both.
"""
import json
import os
import subprocess
import sys
from typing import Optional

from config import APP_NAME, APPDATA_DIR
from core.utils import get_logger

log = get_logger("autostart")

STARTUP_DELAY_SECONDS_DEFAULT = 30
TASK_NAME       = "GhostShell-Autostart"
SETTINGS_PATH   = os.path.join(APPDATA_DIR, "autostart_settings.json")
LEGACY_RUN_KEY  = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_VALUE    = APP_NAME


# ═══════════════════════════════════════════════════════════════════════════
# Settings persistence
# ═══════════════════════════════════════════════════════════════════════════
def _load_settings() -> dict:
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"enabled": False, "delay_seconds": STARTUP_DELAY_SECONDS_DEFAULT}


def _save_settings(d: dict):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save autostart settings: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
def _current_exe_path() -> Optional[str]:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def _run(cmd_list, timeout=20) -> dict:
    """Wraps subprocess.run with the standard hidden-window flags."""
    try:
        r = subprocess.run(
            cmd_list,
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {
            "ok":  r.returncode == 0,
            "out": (r.stdout or "").strip(),
            "err": (r.stderr or "").strip(),
            "rc":  r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "Timed out", "rc": -1}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e), "rc": -1}


def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_task_xml(exe_path: str, delay_seconds: int) -> str:
    """Generate the Task Scheduler XML.

    Key fields:
      <LogonTrigger> — fires when this user signs in.
      <Delay>PT{N}S</Delay> — wait this long after login before running.
      <RunLevel>HighestAvailable</RunLevel> — runs elevated, no UAC prompt.
      <Hidden>true</Hidden> — task doesn't show in regular UI.
      <RestartOnFailure> — if our exe crashes within a minute of launch,
                           the task retries 3 times.
    The user identifier uses the simple `DOMAIN\\USER` form (Task Scheduler
    will resolve the SID itself), which avoids us needing to call
    LookupAccountName from Python.
    """
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    user = user.lstrip("\\")  # in case USERDOMAIN is empty
    delay_seconds = max(0, min(600, int(delay_seconds)))

    # NOTE: Task XML must be UTF-16 LE with BOM when written to disk.  We
    # generate as a regular Python str here and the writer encodes correctly.
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo>\n'
        '    <Description>GhostShell startup launcher (delayed)</Description>\n'
        '    <Author>GhostShell</Author>\n'
        '    <URI>\\' + TASK_NAME + '</URI>\n'
        '  </RegistrationInfo>\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        f'      <UserId>{_xml_escape(user)}</UserId>\n'
        f'      <Delay>PT{delay_seconds}S</Delay>\n'
        '    </LogonTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        f'      <UserId>{_xml_escape(user)}</UserId>\n'
        '      <LogonType>InteractiveToken</LogonType>\n'
        '      <RunLevel>HighestAvailable</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>false</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n'
        '    <IdleSettings>\n'
        '      <StopOnIdleEnd>false</StopOnIdleEnd>\n'
        '      <RestartOnIdle>false</RestartOnIdle>\n'
        '    </IdleSettings>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>true</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>\n'
        '    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n'
        '    <WakeToRun>false</WakeToRun>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '    <Priority>7</Priority>\n'
        '    <RestartOnFailure>\n'
        '      <Interval>PT1M</Interval>\n'
        '      <Count>3</Count>\n'
        '    </RestartOnFailure>\n'
        '  </Settings>\n'
        '  <Actions Context="Author">\n'
        '    <Exec>\n'
        f'      <Command>{_xml_escape(exe_path)}</Command>\n'
        '      <Arguments>--from-autostart</Arguments>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )


def _purge_legacy_run_key():
    """One-shot cleanup: remove old v2.9.5/2.9.6 Run-key entry, if any."""
    r = _run(["reg", "query", LEGACY_RUN_KEY, "/v", LEGACY_VALUE])
    if r["ok"]:
        log.info("Found legacy Run-key autostart entry — removing")
        _run(["reg", "delete", LEGACY_RUN_KEY, "/v", LEGACY_VALUE, "/f"])


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════
def get_status() -> dict:
    """Returns whether the scheduled task exists, the configured delay, etc."""
    settings = _load_settings()
    exe = _current_exe_path()

    # Cross-check: does the task actually exist?
    task_exists = False
    q = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    if q["ok"]:
        task_exists = True

    # Detect if there's a leftover legacy Run-key entry too (UI can warn).
    legacy = _run(["reg", "query", LEGACY_RUN_KEY, "/v", LEGACY_VALUE])

    return {
        "ok":            True,
        "enabled":       task_exists,
        "task_name":     TASK_NAME,
        "exe_path":      exe,
        "frozen":        getattr(sys, "frozen", False),
        "delay_seconds": int(settings.get("delay_seconds", STARTUP_DELAY_SECONDS_DEFAULT)),
        "method":        "scheduled_task",
        "legacy_run_key_present": legacy["ok"],
    }


def enable(delay_seconds: Optional[int] = None) -> dict:
    """Register GhostShell as a scheduled task that runs at logon.

    Args:
        delay_seconds: how long after login to wait before launching.
                       Defaults to last-saved value (or 30s on first use).
                       Clamped to [0, 600].
    """
    exe = _current_exe_path()
    if not exe:
        return {"ok": False,
                "err": "Autostart only works on the built .exe (run pyinstaller build.spec first)"}
    if not os.path.exists(exe):
        return {"ok": False, "err": f"Executable not found: {exe}"}

    settings = _load_settings()
    if delay_seconds is None:
        delay_seconds = int(settings.get("delay_seconds", STARTUP_DELAY_SECONDS_DEFAULT))
    delay_seconds = max(0, min(600, int(delay_seconds)))

    # Always purge the legacy Run-key entry before creating the task — so a
    # user upgrading from 2.9.5/2.9.6 doesn't end up double-launching.
    _purge_legacy_run_key()

    # Write the XML.  schtasks expects UTF-16 LE with BOM.
    import tempfile
    xml = _build_task_xml(exe, delay_seconds)
    fd, xml_path = tempfile.mkstemp(prefix="ghostshell_autostart_", suffix=".xml",
                                    dir=APPDATA_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xff\xfe")                                # UTF-16 LE BOM
            f.write(xml.encode("utf-16-le"))

        # /F overwrites if the task already exists — perfect for re-enable
        # with a different delay.
        r = _run(["schtasks", "/Create", "/XML", xml_path, "/TN", TASK_NAME, "/F"], timeout=30)
    finally:
        try: os.remove(xml_path)
        except Exception: pass

    if not r["ok"]:
        # Common failure modes: "Access denied" → process not actually
        # admin; "Invalid XML" → schema mismatch on an old Win10 build.
        return {"ok": False,
                "err": f"schtasks /Create failed (rc={r['rc']}): "
                       f"{(r['err'] or r['out'])[:300]}"}

    settings["enabled"] = True
    settings["delay_seconds"] = delay_seconds
    _save_settings(settings)
    log.info(f"Autostart enabled — scheduled task '{TASK_NAME}', "
             f"delay {delay_seconds}s, exe {exe}")
    return {
        "ok": True,
        "delay_seconds": delay_seconds,
        "exe_path":      exe,
        "method":        "scheduled_task",
    }


def disable() -> dict:
    """Remove the scheduled task.  Always succeeds even if not registered."""
    r = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    # rc=1 with "cannot find" is fine — nothing to delete
    not_found = "cannot find" in (r["err"] + r["out"]).lower()
    success = r["ok"] or not_found

    # Also nuke any legacy Run-key entry just in case.
    _purge_legacy_run_key()

    settings = _load_settings()
    settings["enabled"] = False
    _save_settings(settings)
    log.info("Autostart disabled (scheduled task removed)")
    return {
        "ok":     success,
        "err":    "" if success else r["err"],
        "method": "scheduled_task",
    }


def set_delay(delay_seconds: int) -> dict:
    """Update the saved delay.  Re-registers the task if currently enabled."""
    delay_seconds = max(0, min(600, int(delay_seconds)))
    settings = _load_settings()
    settings["delay_seconds"] = delay_seconds
    _save_settings(settings)
    if get_status().get("enabled"):
        return enable(delay_seconds=delay_seconds)
    return {"ok": True, "delay_seconds": delay_seconds, "method": "scheduled_task"}


def migrate_from_legacy_runkey() -> dict:
    """Called once on startup.

    If the user had autostart enabled on v2.9.5/v2.9.6 (via Run key) and is
    now on v2.9.7 (which uses scheduled tasks), automatically migrate them.
    Conditions for migration:
      * autostart_settings.json says enabled = true, AND
      * The scheduled task does NOT exist yet, AND
      * Either the legacy Run-key entry exists, OR settings claims enabled.

    Auto-migration is safe — it only fires when the user previously
    expressed an intent to autostart.  Off-by-default users see no change.
    """
    settings = _load_settings()
    if not settings.get("enabled"):
        return {"ok": True, "migrated": False, "reason": "autostart not enabled"}

    status = get_status()
    if status.get("enabled"):
        # Task already exists — nothing to migrate.  Wipe any leftover
        # Run-key entry just in case so the OS doesn't double-launch.
        _purge_legacy_run_key()
        return {"ok": True, "migrated": False, "reason": "task already registered"}

    if not _current_exe_path():
        return {"ok": True, "migrated": False, "reason": "not frozen exe"}

    log.info("Autostart enabled in settings but no task registered — migrating from legacy Run key")
    r = enable(delay_seconds=settings.get("delay_seconds", STARTUP_DELAY_SECONDS_DEFAULT))
    return {
        "ok":       r.get("ok", False),
        "migrated": r.get("ok", False),
        "delay_seconds": r.get("delay_seconds"),
        "err":      r.get("err"),
    }
