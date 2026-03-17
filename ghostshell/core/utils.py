import ctypes
import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Generator, Iterable

import psutil
import winreg
import wmi
from config import (
    APP_NAME,
    APPDATA_DIR as _APPDATA_DIR,
    BACKUP_DIR as _BACKUP_DIR,
    LOG_FILE as _LOG_FILE,
    PROFILE_DIR as _PROFILE_DIR,
    RESTORE_POINT_DESCRIPTION,
)

APPDATA_DIR = Path(_APPDATA_DIR)
BACKUP_DIR = Path(_BACKUP_DIR)
LOG_FILE = Path(_LOG_FILE)
PROFILE_DIR = Path(_PROFILE_DIR)

LOG_BUFFER_MAX_LINES = 2000
LOG_STREAM_HEARTBEAT_SECONDS = 5
CREATE_NO_WINDOW = 0x08000000

__all__ = [
    "ensure_app_dirs",
    "get_logger",
    "tail_logs",
    "stream_logs",
    "log_event",
    "is_admin",
    "relaunch_as_admin",
    "run_cmd",
    "run_powershell",
    "reg_get",
    "reg_set",
    "reg_delete_value",
    "reg_create_key",
    "reg_export",
    "backup_file",
    "safe_write_text",
    "safe_write_bytes",
    "create_system_restore_point",
    "service_set",
    "scheduled_task_disable",
    "list_adapters",
    "set_dns_all",
    "flush_dns_cache",
    "ping",
    "human_bytes",
    "generate_guid",
    "mask",
]


def ensure_app_dirs() -> None:
    """Ensure required application directories and log paths exist."""
    for path in (APPDATA_DIR, BACKUP_DIR, PROFILE_DIR, LOG_FILE.parent):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logging.getLogger("ghostshell").debug("Failed to create directory %s: %s", path, exc)


ensure_app_dirs()


class InMemoryLogHandler(logging.Handler):
    """Logging handler storing log records in an in-memory ring buffer."""

    def __init__(self, max_lines: int = LOG_BUFFER_MAX_LINES):
        super().__init__()
        self.max_lines = max_lines
        self.buffer: deque[str] = deque(maxlen=max_lines)
        self.lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self.lock:
                self.buffer.append(msg)
        except Exception:
            pass

    def tail(self, lines: int = 200) -> str:
        with self.lock:
            return "\n".join(list(self.buffer)[-lines:])


_memory_handler = InMemoryLogHandler()


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("ghostshell")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    _memory_handler.setFormatter(fmt)
    _memory_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(_memory_handler)
    logger.propagate = False

    return logger


_logger = _configure_logger()


def get_logger() -> logging.Logger:
    """Return the configured GhostShell logger."""
    return _logger


def tail_logs(lines: int = 200) -> str:
    """Return the last N lines from the in-memory log buffer."""
    return _memory_handler.tail(lines)


def stream_logs() -> Iterable[str]:
    """Yield Server-Sent Event formatted log lines from the in-memory buffer, with heartbeat."""
    last_sent = 0
    while True:
        current = _memory_handler.buffer
        size = len(current)
        if size > last_sent:
            with _memory_handler.lock:
                chunk = list(current)[last_sent:]
            for line in chunk:
                yield f"data: {line}\n\n"
            last_sent = size
        else:
            # heartbeat to keep SSE alive
            yield "data: \n\n"
            time.sleep(LOG_STREAM_HEARTBEAT_SECONDS)


def log_event(module: str, action: str, status: str, details: str = "") -> None:
    """Log a structured event line."""
    msg = f"[{module}] {action} | {status} | {details}".strip()
    _logger.info(msg)


# --- Admin elevation ---

def is_admin() -> bool:
    """Return True if the current process has administrative privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args: list[str] | None = None) -> None:
    """Relaunch the current Python script with admin rights using UAC prompt.

    Raises RuntimeError if user declines UAC or on failure.
    """
    params = " ".join([f'"{arg}"' for arg in (extra_args or sys.argv)])
    try:
        hinstance = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if int(hinstance) <= 32:
            raise RuntimeError("User declined UAC or ShellExecute failed")
        os._exit(0)
    except Exception as exc:
        log_event("utils", "relaunch_as_admin", "error", str(exc))
        raise RuntimeError(str(exc))


# --- Subprocess helpers ---

def _popen(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            stdout, stderr = proc.communicate(timeout=max(1, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return -1, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")
        return (
            proc.returncode,
            (stdout or b"").decode("utf-8", errors="ignore"),
            (stderr or b"").decode("utf-8", errors="ignore"),
        )
    except Exception as exc:
        return 1, "", str(exc)
    finally:
        duration = int((time.time() - start) * 1000)
        log_event("utils", "run_cmd", "done", f"{' '.join(cmd)} ({duration}ms)")


def run_cmd(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run a command, capturing stdout/stderr. Returns (code, stdout, stderr)."""
    return _popen(cmd, timeout)


def run_powershell(ps_script: str, timeout: int = 600) -> tuple[int, str, str]:
    """Execute a PowerShell script with Bypass policy and no profile."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_script,
    ]
    code, stdout, stderr = _popen(cmd, timeout)
    log_event("utils", "powershell", "success" if code == 0 else "error", stderr or stdout)
    return code, stdout, stderr


# --- Registry helpers ---
_REG_ROOTS = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
}

_REG_TYPES = {
    None: winreg.REG_SZ,
    "REG_SZ": winreg.REG_SZ,
    "REG_DWORD": winreg.REG_DWORD,
    "REG_QWORD": winreg.REG_QWORD,
    "REG_BINARY": winreg.REG_BINARY,
    "REG_MULTI_SZ": winreg.REG_MULTI_SZ,
}


def _open_key(root: str, path: str, access: int) -> winreg.HKEYType | None:
    root_key = _REG_ROOTS.get(root.upper())
    if not root_key:
        return None
    try:
        return winreg.OpenKey(root_key, path, 0, access)
    except FileNotFoundError:
        return None


def reg_get(root: str, path: str, name: str, default: Any = None) -> Any:
    """Get a registry value, returning default if not found."""
    try:
        with _open_key(root, path, winreg.KEY_READ) as k:
            if k is None:
                return default
            value, _typ = winreg.QueryValueEx(k, name)
            return value
    except FileNotFoundError:
        return default
    except Exception as exc:
        log_event("utils", "reg_get", "error", f"{root}\\{path} {name}: {exc}")
        return default


def reg_create_key(root: str, path: str) -> None:
    """Create a registry key if it does not exist."""
    try:
        root_key = _REG_ROOTS.get(root.upper())
        if not root_key:
            return
        winreg.CreateKeyEx(root_key, path, 0, winreg.KEY_WRITE)
        log_event("utils", "reg_create_key", "success", f"{root}\\{path}")
    except Exception as exc:
        log_event("utils", "reg_create_key", "error", f"{root}\\{path}: {exc}")


def reg_set(root: str, path: str, name: str, value: Any, type_name: str | None = None) -> None:
    """Set a registry value, creating key if necessary."""
    try:
        reg_create_key(root, path)
        root_key = _REG_ROOTS.get(root.upper())
        if not root_key:
            return
        with winreg.OpenKey(root_key, path, 0, winreg.KEY_WRITE) as k:
            typ = _REG_TYPES.get(type_name, winreg.REG_SZ)
            if typ == winreg.REG_DWORD and isinstance(value, bool):
                value = int(value)
            winreg.SetValueEx(k, name, 0, typ, value)
        log_event("utils", "reg_set", "success", f"{root}\\{path} {name}={value}")
    except Exception as exc:
        log_event("utils", "reg_set", "error", f"{root}\\{path} {name}: {exc}")


def reg_delete_value(root: str, path: str, name: str) -> None:
    """Delete a registry value if it exists."""
    try:
        with _open_key(root, path, winreg.KEY_SET_VALUE) as k:
            if k is None:
                return
            winreg.DeleteValue(k, name)
        log_event("utils", "reg_delete_value", "success", f"{root}\\{path} {name}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_event("utils", "reg_delete_value", "error", f"{root}\\{path} {name}: {exc}")


def reg_export(paths: list[tuple[str, str]], export_path: Path) -> bool:
    """Export registry keys to a .reg file using reg.exe. Returns True if at least one succeeded."""
    export_path = Path(export_path)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    success = False
    for root, path in paths:
        regroot = {
            "HKLM": "HKEY_LOCAL_MACHINE",
            "HKCU": "HKEY_CURRENT_USER",
        }.get(root.upper(), root)
        out = export_path.with_suffix("")
        # Append root sanitized path to filename to avoid overwriting
        safe_name = f"{regroot.replace('HKEY_', '')}_{path.replace('\\', '_').strip('_')}"
        target = export_path.parent / (safe_name + ".reg")
        code, stdout, stderr = run_cmd(["reg", "export", f"{regroot}\\{path}", str(target), "/y"]) 
        ok = code == 0
        success = success or ok
        log_event("utils", "reg_export", "success" if ok else "error", stderr or stdout)
    return success


# --- Backup helpers ---

def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_file(src: Path, backups_root: Path | None = None) -> Path:
    """Backup a file to a timestamped path under backups_root. Returns backup path. Does not raise on failure."""
    try:
        src = Path(src)
        if not src.exists():
            return src
        root = Path(backups_root) if backups_root else BACKUP_DIR
        rel = src.name
        dest_dir = root / _timestamp()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / rel
        shutil.copy2(src, dest)
        log_event("utils", "backup_file", "success", f"{src} -> {dest}")
        return dest
    except Exception as exc:
        log_event("utils", "backup_file", "error", f"{src}: {exc}")
        return src


def safe_write_text(path: Path, content: str, make_backup: bool = True) -> None:
    """Write text to file safely, creating dirs and optional backup."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if make_backup and path.exists():
            backup_file(path)
        path.write_text(content, encoding="utf-8")
        log_event("utils", "safe_write_text", "success", str(path))
    except Exception as exc:
        log_event("utils", "safe_write_text", "error", f"{path}: {exc}")


def safe_write_bytes(path: Path, data: bytes, make_backup: bool = True) -> None:
    """Write bytes to file safely, creating dirs and optional backup."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if make_backup and path.exists():
            backup_file(path)
        path.write_bytes(data)
        log_event("utils", "safe_write_bytes", "success", str(path))
    except Exception as exc:
        log_event("utils", "safe_write_bytes", "error", f"{path}: {exc}")


# --- Restore point ---

def create_system_restore_point(description: str | None = None) -> bool:
    """Create a Windows system restore point. Returns False if not permitted or failed."""
    desc = description or RESTORE_POINT_DESCRIPTION
    ps = (
        f"Checkpoint-Computer -Description \"{desc}\" -RestorePointType 'MODIFY_SETTINGS'"
    )
    code, stdout, stderr = run_powershell(ps, timeout=900)
    ok = code == 0
    log_event("utils", "create_restore_point", "success" if ok else "error", stderr or stdout)
    return ok


# --- Services and scheduled tasks ---

def service_set(name: str, start_type: str | None = None, action: str | None = None) -> tuple[bool, str]:
    """Configure a service. start_type in {auto, demand, disabled}. action in {start, stop}."""
    cmds = []
    if start_type:
        st_map = {"auto": "auto", "demand": "demand", "disabled": "disabled"}
        st = st_map.get(start_type.lower())
        if st:
            cmds.append(["sc", "config", name, f"start= {st}"])
    if action:
        act = action.lower()
        if act == "start":
            cmds.append(["sc", "start", name])
        elif act == "stop":
            cmds.append(["sc", "stop", name])
    last_out = ""
    ok = True
    for c in cmds:
        code, stdout, stderr = run_cmd(c)
        last_out = stderr or stdout
        ok = ok and (code == 0)
    log_event("utils", "service_set", "success" if ok else "error", f"{name}: {last_out}")
    return ok, last_out


def scheduled_task_disable(path: str) -> tuple[bool, str]:
    """Disable a scheduled task by path."""
    code, stdout, stderr = run_cmd(["schtasks", "/Change", "/TN", path, "/DISABLE"]) 
    ok = code == 0
    log_event("utils", "scheduled_task_disable", "success" if ok else "error", stderr or stdout)
    return ok, (stderr or stdout)


# --- Network utilities ---

def list_adapters() -> list[dict]:
    """List active network adapters with basic info."""
    adapters = []
    try:
        for name, addrs in psutil.net_if_addrs().items():
            stats = psutil.net_if_stats().get(name)
            if stats is None:
                continue
            ipv4 = next((a.address for a in addrs if a.family.name == 'AF_INET' if hasattr(a, 'family')), None)
            ipv6 = next((a.address for a in addrs if a.family.name == 'AF_INET6' if hasattr(a, 'family')), None)
            adapters.append({
                "name": name,
                "enabled": bool(stats.isup),
                "speed_mbps": getattr(stats, 'speed', None),
                "mtu": getattr(stats, 'mtu', None),
                "ipv4": ipv4,
                "ipv6": ipv6,
                "dns": [],
            })
    except Exception as exc:
        log_event("utils", "list_adapters", "error", str(exc))
    return adapters


def set_dns_all(primary: str, secondary: str | None = None) -> tuple[bool, str]:
    """Set DNS servers on all active adapters using netsh and flush DNS."""
    ok = True
    last = ""
    try:
        # Query interface names via netsh to map to indexes
        code, stdout, stderr = run_cmd(["netsh", "interface", "ipv4", "show", "interfaces"]) 
        interfaces = []
        for line in (stdout or "").splitlines():
            # Idx     Met         MTU          State                Name
            m = re.search(r"^\s*(\d+)\s+\d+\s+\d+\s+\S+\s+(.+)$", line)
            if m:
                interfaces.append((m.group(1), m.group(2).strip()))
        for idx, name in interfaces:
            cmds = [["netsh", "interface", "ipv4", "set", "dnsservers", f"name={name}", f"source=static", f"address={primary}", "validate=no"]]
            if secondary:
                cmds.append(["netsh", "interface", "ipv4", "add", "dnsservers", f"name={name}", f"address={secondary}", "index=2", "validate=no"])
            for c in cmds:
                code, out, err = run_cmd(c)
                last = err or out
                ok = ok and (code == 0)
        flush_dns_cache()
    except Exception as exc:
        ok = False
        last = str(exc)
    log_event("utils", "set_dns_all", "success" if ok else "error", last)
    return ok, last


def flush_dns_cache() -> None:
    """Flush the DNS resolver cache."""
    code, stdout, stderr = run_cmd(["ipconfig", "/flushdns"]) 
    status = "success" if code == 0 else "error"
    log_event("utils", "flush_dns_cache", status, stderr or stdout)


def ping(host: str, count: int = 4, timeout_ms: int = 1000) -> dict:
    """Ping a host and return metrics."""
    cmd = ["ping", "-n", str(max(1, count)), "-w", str(max(1, timeout_ms)), host]
    code, stdout, stderr = run_cmd(cmd)
    output = stdout or stderr
    metrics = {"avg": None, "min": None, "max": None, "packet_loss": None, "success": code == 0}
    if not output:
        log_event("utils", "ping", "error", f"{host}: No output")
        return metrics

    loss_match = re.search(r"Lost = \d+ \((\d+)% loss\)", output)
    stats_match = re.search(
        r"Minimum = (\d+)ms, Maximum = (\d+)ms, Average = (\d+)ms", output
    )
    if loss_match:
        metrics["packet_loss"] = int(loss_match.group(1))
    if stats_match:
        metrics["min"] = int(stats_match.group(1))
        metrics["max"] = int(stats_match.group(2))
        metrics["avg"] = int(stats_match.group(3))
    log_event("utils", "ping", "success" if code == 0 else "error", f"{host}: {metrics}")
    return metrics


def human_bytes(n: int) -> str:
    """Return human-readable byte size."""
    if n < 0:
        return "-" + human_bytes(-n)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024


def generate_guid() -> str:
    """Generate a random GUID."""
    value = str(uuid.uuid4())
    log_event("utils", "generate_guid", "success", value)
    return value


def mask(s: str, show: int = 4) -> str:
    """Mask middle portion of a string, showing limited characters."""
    if not s:
        return ""
    if len(s) <= show * 2:
        return "*" * len(s)
    return f"{s[:show]}{'*' * (len(s) - 2 * show)}{s[-show:]}"