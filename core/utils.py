"""GhostShell centralized logging and utility helpers."""
import logging
import logging.handlers
import os
import time
import subprocess
import sys
import json
from datetime import datetime
from collections import deque
from config import LOG_FILE_PATH, APPDATA_DIR, BACKUP_DIR

# ---------------------------------------------------------------------------
# In-memory log ring buffer so the UI can stream entries
# ---------------------------------------------------------------------------
_LOG_BUFFER: deque = deque(maxlen=2000)


class _UIHandler(logging.Handler):
    """Push every log record into the ring buffer for the /api/logs endpoint."""

    def emit(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "module": record.name.replace("ghostshell.", ""),
            "msg": self.format(record),
        }
        _LOG_BUFFER.append(entry)


def get_log_buffer():
    return list(_LOG_BUFFER)


def clear_log_buffer():
    _LOG_BUFFER.clear()


# ---------------------------------------------------------------------------
# Configure root logger
# ---------------------------------------------------------------------------
# v3.2.3 — disk + perf-conscious logging.
#
# Before: a plain FileHandler at logging.DEBUG appended forever.  The
# ghostshell.log file in %APPDATA% grew unbounded — a 1 MB log after a
# casual session, several megabytes for heavy users.  Every poll cycle
# (perf_monitor at 5 Hz, gamepad at 30 Hz, etc) also wrote a debug
# entry, contributing to UI lag during high-activity sessions because
# we were paying the file-write cost on every tick.
#
# After:
#   * RotatingFileHandler caps a single log at 5 MB and keeps 3 rotated
#     backups — total disk ceiling is ~20 MB.  Old logs roll off
#     automatically; no manual cleanup needed.
#   * Default level is INFO (was DEBUG).  Pulls a TON of per-tick noise
#     out of the file + buffer, which translates directly to less I/O
#     and a smoother UI.  Anyone debugging a real issue can opt in via
#     GHOSTSHELL_LOG_LEVEL=DEBUG.
os.makedirs(APPDATA_DIR, exist_ok=True)

LOG_MAX_BYTES   = 5 * 1024 * 1024   # 5 MB per file
LOG_BACKUP_COUNT = 3                 # keep ghostshell.log + .1 + .2 + .3

_formatter = logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s  %(message)s", datefmt="%H:%M:%S")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
    delay=True,          # don't open the file until first write
)
_file_handler.setFormatter(_formatter)

_ui_handler = _UIHandler()
_ui_handler.setFormatter(logging.Formatter("%(message)s"))

_root = logging.getLogger("ghostshell")

# Honor an env override for debugging.  Production default is INFO.
_level_name = (os.environ.get("GHOSTSHELL_LOG_LEVEL") or "INFO").upper()
_root.setLevel(getattr(logging, _level_name, logging.INFO))
_root.addHandler(_file_handler)
_root.addHandler(_ui_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ghostshell.{name}")


def set_log_level(level: str) -> dict:
    """Bump the runtime log level — useful when the user wants to
    diagnose an issue without restarting GhostShell.  Persists nothing —
    the next launch goes back to the default."""
    lvl = (level or "").upper()
    target = getattr(logging, lvl, None)
    if target is None or not isinstance(target, int):
        return {"ok": False, "err": f"unknown level: {level}"}
    _root.setLevel(target)
    return {"ok": True, "level": lvl}


def get_log_level() -> str:
    return logging.getLevelName(_root.level)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    """Return True if running with administrator privileges."""
    if sys.platform != "win32":
        return os.getuid() == 0
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_ps(command: str, timeout: int = 60) -> dict:
    """Run a PowerShell command and return {'ok': bool, 'out': str, 'err': str}."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": r.returncode == 0, "out": r.stdout.strip(), "err": r.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "Timed out"}
    except FileNotFoundError:
        return {"ok": False, "out": "", "err": "PowerShell not found"}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


def run_cmd(args: list, timeout: int = 60) -> dict:
    """Run an arbitrary command list."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": r.returncode == 0, "out": r.stdout.strip(), "err": r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


def atomic_write_json(path: str, data) -> bool:
    """Write JSON to `path` without ever leaving a truncated/corrupt file behind.

    Writes to a sibling temp file first, then `os.replace()`s it into place —
    `os.replace` is atomic on Windows (same volume), so a crash/kill mid-write
    lands on either the old complete file or the new complete file, never a
    half-written one. Settings/state readers already fall back to defaults on
    a bad json.load, but that fallback silently loses the user's saved data —
    this prevents the corruption that fallback was papering over."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        get_logger("utils").warning(f"atomic_write_json failed for {path}: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> bool:
    """Same crash-safety as atomic_write_json, for plain-text files (raw
    IDs, the Windows hosts file, etc.) that aren't JSON-encoded."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except Exception as e:
        get_logger("utils").warning(f"atomic_write_text failed for {path}: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


# 3.4.3 — cap on how many .reg backups we keep.  Every tweak apply
# calls backup_registry(); the old code never pruned, so the backups
# dir grew one tiny .reg per tweak forever (the dev machine had 702
# files / 78 MB).  We keep the most-recent N by mtime — plenty for
# "undo a recent change", bounded so it can't balloon.
_BACKUP_KEEP_MAX = 200


def _prune_registry_backups(keep: int = _BACKUP_KEEP_MAX) -> int:
    """Delete oldest .reg backups beyond `keep`.  Returns count removed.
    Best-effort; never raises."""
    removed = 0
    try:
        files = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                 if f.lower().endswith(".reg")]
        if len(files) <= keep:
            return 0
        # Sort newest-first by mtime, delete everything past `keep`.
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[keep:]:
            try:
                os.remove(p); removed += 1
            except OSError:
                continue
    except Exception:
        pass
    return removed


def backup_registry(key_path: str, filename: str) -> bool:
    """Export a registry key to a .reg file in the backup directory."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(BACKUP_DIR, f"{filename}_{ts}.reg")
    r = run_cmd(["reg", "export", key_path, out, "/y"])
    # Keep the backups dir bounded.  Prune occasionally (not every call —
    # listdir on each backup would be wasteful); a cheap modulo gate on
    # the timestamp seconds runs it roughly 1-in-10 writes.
    try:
        if int(ts[-1]) % 3 == 0:
            _prune_registry_backups()
    except Exception:
        pass
    return r["ok"]


def create_restore_point(description: str = "GhostShell Pre-Modification") -> bool:
    """Create a Windows System Restore point."""
    log = get_logger("utils")
    log.info("Creating system restore point...")
    # Enable restore on C: first
    run_ps('Enable-ComputerRestore -Drive "C:\\"')
    r = run_ps(
        f'Checkpoint-Computer -Description "{description}" -RestorePointType "MODIFY_SETTINGS" -ErrorAction Stop'
    )
    if r["ok"]:
        log.info("Restore point created successfully")
    else:
        log.warning(f"Restore point may have failed: {r['err']}")
    return r["ok"]


def detect_drive_type() -> str:
    """Return 'ssd', 'nvme', or 'hdd'."""
    r = run_ps("Get-PhysicalDisk | Select-Object MediaType,BusType | ConvertTo-Json")
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for disk in data:
                bus = str(disk.get("BusType", "")).lower()
                media = str(disk.get("MediaType", "")).lower()
                if "nvme" in bus:
                    return "nvme"
                if "ssd" in media or "solid" in media:
                    return "ssd"
            return "hdd"
        except json.JSONDecodeError:
            pass
    return "unknown"
