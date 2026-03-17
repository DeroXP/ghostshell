import re
import textwrap
from pathlib import Path
from typing import Tuple

from config import APPDATA_DIR
from core.utils import ensure_app_dirs, log_event, run_powershell, safe_write_text

MODULE_NAME = "maintenance"
DEFAULT_TASK_NAME = "GhostShellMaintenance"
SCRIPT_FILENAME = "maintenance.ps1"
LOG_FILENAME = "maintenance.log"


def get_task_name(default: str = DEFAULT_TASK_NAME) -> str:
    name = default if isinstance(default, str) else DEFAULT_TASK_NAME
    name = name.strip()
    sanitized = re.sub(r"[^A-Za-z0-9_\- ]+", "", name)
    sanitized = sanitized.strip()[:238]
    return sanitized or DEFAULT_TASK_NAME


def install_task(task_name: str | None = None) -> Tuple[bool, str]:
    resolved_name = get_task_name(task_name or DEFAULT_TASK_NAME)
    script_path = APPDATA_DIR / SCRIPT_FILENAME
    log_path = APPDATA_DIR / LOG_FILENAME

    try:
        ensure_app_dirs()
        script_contents = _build_maintenance_script(log_path)
        safe_write_text(script_path, script_contents)
    except Exception as exc:
        message = f"Failed to prepare maintenance script: {exc}"
        log_event(MODULE_NAME, "install", "error", message)
        return False, message

    script_path_str = _escape_ps_string(str(script_path))
    task_name_ps = _escape_ps_string(resolved_name)
    ps_script = textwrap.dedent(
        f"""
        $scriptPath = "{script_path_str}"
        $taskCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
        schtasks.exe /Create /TN "{task_name_ps}" /TR $taskCmd /SC ONLOGON /RL HIGHEST /F
        """
    ).strip()

    return _execute_ps(ps_script, "install", f"Scheduled task '{resolved_name}' installed.")


def remove_task(task_name: str | None = None) -> Tuple[bool, str]:
    resolved_name = get_task_name(task_name or DEFAULT_TASK_NAME)
    task_name_ps = _escape_ps_string(resolved_name)
    ps_script = f"schtasks.exe /Delete /TN \"{task_name_ps}\" /F"
    return _execute_ps(ps_script, "remove", f"Scheduled task '{resolved_name}' removed.")


def run_now(task_name: str | None = None) -> Tuple[bool, str]:
    resolved_name = get_task_name(task_name or DEFAULT_TASK_NAME)
    task_name_ps = _escape_ps_string(resolved_name)
    ps_script = f"schtasks.exe /Run /TN \"{task_name_ps}\""
    return _execute_ps(ps_script, "run", f"Scheduled task '{resolved_name}' started.")


# --- helpers ---

def _execute_ps(ps_script: str, action: str, success_message: str) -> Tuple[bool, str]:
    try:
        code, out, err = run_powershell(ps_script)
        ok = code == 0
        msg = (err or out or "").strip() or success_message
        log_event(MODULE_NAME, action, "success" if ok else "error", msg)
        return ok, msg
    except Exception as exc:
        log_event(MODULE_NAME, action, "error", str(exc))
        return False, str(exc)


def _escape_ps_string(s: str) -> str:
    # Escape for double-quoted PowerShell literal
    return s.replace("`", "``").replace("\"", "`\"")


def _build_maintenance_script(log_path: Path) -> str:
    log_path_ps = _escape_ps_string(str(log_path))
    script = f"""
    $ErrorActionPreference = 'SilentlyContinue'

    function Write-Log($msg) {{
        $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        $line = "[$ts] $msg"
        try {{ Add-Content -LiteralPath "{log_path_ps}" -Value $line -Encoding UTF8 }} catch {{}}
    }}

    Write-Log "Maintenance started"

    try {{ Clear-RecycleBin -Force -ErrorAction SilentlyContinue; Write-Log "Recycle Bin cleared" }} catch {{ Write-Log "Recycle Bin clear failed: $($_.Exception.Message)" }}

    $tempPaths = @(
        "$env:TEMP\\*",
        "$env:TMP\\*",
        "$env:LOCALAPPDATA\\Temp\\*",
        "C:\\Windows\\Temp\\*"
    )

    function Clear-Targets([string[]]$paths) {{
        foreach ($p in $paths) {{
            try {{ Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }} catch {{}}
        }}
    }}

    Clear-Targets -paths $tempPaths

    $browserCaches = @(
        "$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\Cache\\*",
        "$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\Code Cache\\*",
        "$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\GPUCache\\*",
        "$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\Service Worker\\CacheStorage\\*",
        "$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\\Cache\\*",
        "$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\\Code Cache\\*",
        "$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\\GPUCache\\*",
        "$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\\Service Worker\\CacheStorage\\*",
        "$env:APPDATA\\Mozilla\\Firefox\\Profiles\\*\\cache2\\*",
        "$env:APPDATA\\Mozilla\\Firefox\\Profiles\\*\\startupCache\\*"
    )
    Clear-Targets -paths $browserCaches

    $services = @("wuauserv", "bits")
    foreach ($svc in $services) {{
        try {{ Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue }} catch {{ Write-Log "Failed to stop service $svc: $($_.Exception.Message)" }}
    }}

    Clear-Targets -paths @("C:\\Windows\\SoftwareDistribution\\Download\\*")

    foreach ($svc in $services) {{
        try {{ Start-Service -Name $svc -ErrorAction SilentlyContinue }} catch {{ Write-Log "Failed to start service $svc: $($_.Exception.Message)" }}
    }}

    Clear-Targets -paths @("C:\\Windows\\Prefetch\\*.pf")

    try {{ ipconfig /flushdns | Out-Null; Write-Log "DNS cache flushed" }} catch {{ Write-Log "DNS flush failed: $($_.Exception.Message)" }}

    Write-Log "Maintenance completed"
    """
    return textwrap.dedent(script).strip() + "\n"
