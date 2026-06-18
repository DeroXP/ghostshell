"""Windows toast notifications via native WinRT APIs (no extra deps)."""
import os
import json
import threading
from core.utils import run_ps, get_logger

log = get_logger("notifier")

APP_ID = "Vispora"
_SETTINGS_FILE = None


def _settings_path() -> str:
    global _SETTINGS_FILE
    if _SETTINGS_FILE is None:
        from config import APPDATA_DIR
        _SETTINGS_FILE = os.path.join(APPDATA_DIR, "notifier_settings.json")
    return _SETTINGS_FILE


def _load_settings() -> dict:
    p = _settings_path()
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {"enabled": True}


def _save_settings(s: dict):
    try:
        with open(_settings_path(), "w") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save notifier settings: {e}")


def is_enabled() -> bool:
    return bool(_load_settings().get("enabled", True))


def set_enabled(enabled: bool) -> dict:
    s = _load_settings()
    s["enabled"] = bool(enabled)
    _save_settings(s)
    log.info(f"Notifications {'enabled' if enabled else 'disabled'}")
    return {"ok": True, "enabled": s["enabled"]}


def _escape_xml(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def toast(title: str, message: str, force: bool = False) -> dict:
    """Show a Windows toast notification. Non-blocking (runs in thread)."""
    if not force and not is_enabled():
        return {"ok": False, "err": "notifications disabled"}

    t = _escape_xml(title)
    m = _escape_xml(message)

    # Use PowerShell + Windows.UI.Notifications (built-in on Win10/11 — no BurntToast needed)
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
    $template = @'
<toast activationType="protocol" launch="">
  <visual>
    <binding template="ToastGeneric">
      <text>{t}</text>
      <text>{m}</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
'@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($toast)
    Write-Output 'OK'
}} catch {{
    Write-Output ('ERR ' + $_.Exception.Message)
}}
"""

    def _run():
        r = run_ps(ps, timeout=6)
        if not (r["ok"] and "OK" in r["out"]):
            log.debug(f"Toast fallback: {r.get('err') or r.get('out')}")
            # Fallback: msg.exe popup (works even without WinRT)
            try:
                import subprocess
                subprocess.Popen(
                    ["msg", "*", "/TIME:6", f"{title}: {message}"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


def notify_game_detected(game_name: str, exe: str) -> dict:
    return toast(
        "Vispora — Game Detected",
        f"Applying gaming optimizations for {game_name}",
    )


def notify_game_exited(game_name: str) -> dict:
    return toast(
        "Vispora — Game Closed",
        f"{game_name} exited. Normal mode restored.",
    )


def notify_boot_prep_done(duration_sec: float) -> dict:
    return toast(
        "Vispora — Ready",
        f"System primed for gaming in {duration_sec:.1f}s",
    )


# v2.9.4 — post-update notification.  Fires after the auto-updater
# replaces the .exe and the new build re-launches.
def notify_update_installed(new_version: str, previous_version: str) -> dict:
    return toast(
        f"Vispora updated to v{new_version}",
        f"Successfully upgraded from v{previous_version}. Click the Vispora window to see what's new.",
        force=True,   # don't let user-disabled notifications hide an update success
    )


def notify_update_failed(reason: str) -> dict:
    return toast(
        "Vispora — Update install failed",
        f"The update did not apply: {reason}. The current version is unchanged.",
        force=True,
    )
