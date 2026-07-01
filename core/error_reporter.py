"""Error reporting backend — POSTs error reports to the Railway server
so the GhostShell maintainer can see + patch them.

Replaces the old "open a pre-filled GitHub issue" flow.  GitHub
required the user to have an account, hit New Issue, paste the body,
and click Submit — friction high enough that most errors went
unreported.  POSTing to Railway is one click.

Privacy
───────
Payload contains:
  • Anonymous install ID (random per-PC, no PII)
  • Error kind, message, stack/detail
  • App version + admin/non-admin flag
  • OS version, CPU name, GPU name
  • Last 30 log lines (already redacted by perf_monitor / etc — no creds)
  • Optional user "what I was doing" note

NOT sent:
  • Username, full disk paths beyond what's already in error messages
  • install_info.json contents
  • Vault data, recoil presets, gamepad config
  • Anything else not explicitly listed

Reports live at:
    %APPDATA%/GhostShell/error_reports/<timestamp>_<hash>.json
... AND on Railway under /errors/list (admin-gated by a token).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from typing import Optional

from config import APP_VERSION, APPDATA_DIR, UPDATE_SERVER_URL_DEFAULT
from core.utils import get_logger, atomic_write_json

log = get_logger(__name__)


REPORTS_DIR = os.path.join(APPDATA_DIR, "error_reports")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "error_reporter_settings.json")

_DEFAULTS = {
    "enabled":           True,           # default ON; opt-out in Settings
    "server_url":        UPDATE_SERVER_URL_DEFAULT,
    "last_sent_at":      0.0,
    "last_sent_count":   0,              # this-session counter
    "include_log_lines": True,           # last 30 lines of ghostshell.log
}


# ─── Settings ──────────────────────────────────────────────────────

def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        merged = {**_DEFAULTS, **data}
        # Migrate a saved server_url still pointing at the dead pre-rename
        # Railway host (service renamed ghostshell-site → vispora).
        if "ghostshell-site.up.railway.app" in (merged.get("server_url") or ""):
            merged["server_url"] = UPDATE_SERVER_URL_DEFAULT
            atomic_write_json(SETTINGS_PATH, merged)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def _save_settings(s: dict) -> None:
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load_settings()


def set_settings(**kwargs) -> dict:
    s = _load_settings()
    if "enabled" in kwargs and kwargs["enabled"] is not None:
        s["enabled"] = bool(kwargs["enabled"])
    if "include_log_lines" in kwargs and kwargs["include_log_lines"] is not None:
        s["include_log_lines"] = bool(kwargs["include_log_lines"])
    if "server_url" in kwargs and isinstance(kwargs["server_url"], str):
        s["server_url"] = kwargs["server_url"].rstrip("/")
    _save_settings(s)
    return {"ok": True, "settings": s}


# ─── Anon ID — reuse the community module's ID ─────────────────────

def _get_anon_id() -> str:
    try:
        from core import community
        return community._get_anon_id()
    except Exception:
        return "anon_unknown"


# ─── Recent-error dedup ────────────────────────────────────────────

_seen_hashes_lock = threading.Lock()
_seen_hashes: dict = {}     # hash -> timestamp


def _hash_report(kind: str, message: str, detail: str) -> str:
    """Stable hash of the error identity (kind + message + first 200 of
    detail).  Used both client-side and server-side for dedup."""
    h = hashlib.sha256()
    h.update((kind or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update((message or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update((detail or "")[:200].encode("utf-8"))
    return h.hexdigest()[:16]


def _recently_seen(hash_: str, within_sec: int = 60) -> bool:
    """Has this exact error been sent in the last N seconds?  Stops
    runaway spam during a tight error loop."""
    now = time.time()
    with _seen_hashes_lock:
        # Prune old entries
        for k in list(_seen_hashes.keys()):
            if now - _seen_hashes[k] > within_sec * 4:
                _seen_hashes.pop(k, None)
        last = _seen_hashes.get(hash_, 0)
        if now - last < within_sec:
            return True
        _seen_hashes[hash_] = now
        return False


# ─── Report assembly ────────────────────────────────────────────────

def _build_environment() -> dict:
    """Compose the env-context block: app version, OS, hardware.
    Every field is best-effort — missing info shouldn't block the report."""
    env = {
        "app_version":  APP_VERSION,
        "anon_id":      _get_anon_id(),
        "ts":           time.time(),
    }
    try:
        import platform
        env["os_platform"] = platform.platform()
        env["os_release"]  = platform.release()
    except Exception: pass
    try:
        from core.utils import is_admin
        env["admin"] = bool(is_admin())
    except Exception: pass
    try:
        import subprocess
        r = subprocess.run(
            ["wmic", "cpu", "get", "name"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line and "Name" not in line:
                env["cpu"] = line[:80]
                break
    except Exception: pass
    try:
        from core import gpu_overclock
        cap = gpu_overclock.detect_gpu_oc_capability()
        env["gpu"] = (cap.get("name") or cap.get("vendor") or "")[:80]
    except Exception: pass
    return env


def _recent_log_tail(n: int = 30) -> list:
    """Pull the last N entries from utils' in-memory buffer.  Doesn't
    touch disk."""
    try:
        from core.utils import get_log_buffer
        buf = get_log_buffer() or []
        return buf[-n:]
    except Exception: return []


def build_report(kind: str, message: str, detail: str = "",
                  user_note: str = "") -> dict:
    """Pure helper — assembles the report payload without sending it.
    Useful when the UI wants to PREVIEW what will be sent before the
    user clicks Submit."""
    s = _load_settings()
    payload = {
        "kind":      str(kind  or "")[:100],
        "message":   str(message or "")[:2000],
        "detail":    str(detail  or "")[:8000],
        "user_note": str(user_note or "")[:1500],
        "env":       _build_environment(),
        "logs":      _recent_log_tail(30) if s.get("include_log_lines") else [],
    }
    payload["hash"] = _hash_report(payload["kind"], payload["message"],
                                     payload["detail"])
    return payload


# ─── Local persistence ─────────────────────────────────────────────

_LOCAL_REPORTS_CAP = 200


def _prune_local_reports() -> None:
    """v3.3.1-beta.7: keep at most `_LOCAL_REPORTS_CAP` local copies.
    Without this the folder grows forever (one file per unique error
    hash) — a noisy month with intermittent network errors can leave
    thousands of files behind.  Called after every save."""
    try:
        if not os.path.isdir(REPORTS_DIR):
            return
        files = []
        for fn in os.listdir(REPORTS_DIR):
            if not fn.endswith(".json"): continue
            full = os.path.join(REPORTS_DIR, fn)
            try: st = os.stat(full)
            except OSError: continue
            files.append((st.st_mtime, full))
        if len(files) <= _LOCAL_REPORTS_CAP:
            return
        files.sort()  # oldest first
        for _, path in files[: len(files) - _LOCAL_REPORTS_CAP]:
            try: os.remove(path)
            except OSError: pass
    except Exception as e:
        log.debug(f"_prune_local_reports: {e}")


def _save_local(report: dict) -> str:
    """Save a local copy of the report so the user can still review it
    (and recover the body for a manual submission) if the Railway POST
    fails.  Returns the saved path."""
    try:
        ts = int(time.time())
        fn = f"{ts}_{report.get('hash','?')}.json"
        path = os.path.join(REPORTS_DIR, fn)
        atomic_write_json(path, report)
        _prune_local_reports()
        return path
    except Exception as e:
        log.debug(f"local error report save failed: {e}")
        return ""


# ─── Submit ────────────────────────────────────────────────────────

def submit_report(kind: str, message: str, detail: str = "",
                   user_note: str = "") -> dict:
    """Send a single error report to the Railway server.  Returns
    {ok, hash, server_response, local_path, duplicate}."""
    s = _load_settings()
    if not s.get("enabled"):
        # Even when disabled, save locally so the user can grab it later
        report = build_report(kind, message, detail, user_note)
        local = _save_local(report)
        return {"ok": True, "skipped": "error reporting disabled",
                "hash": report["hash"], "local_path": local}

    report = build_report(kind, message, detail, user_note)
    h = report["hash"]
    if _recently_seen(h):
        log.debug(f"error report dedup: {h} seen recently — skipping POST")
        return {"ok": True, "duplicate": True, "hash": h}

    local_path = _save_local(report)
    url = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/") + "/errors/submit"

    try:
        body = json.dumps(report).encode("utf-8")
        if len(body) > 100 * 1024:
            # Trim oversized payloads — server caps at 100 KB anyway
            report["detail"] = report["detail"][:4000]
            report["logs"] = report["logs"][-15:]
            body = json.dumps(report).encode("utf-8")

        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent":   f"GhostShell/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            resp_body = r.read()
            status = r.status

        s["last_sent_at"]    = time.time()
        s["last_sent_count"] = int(s.get("last_sent_count", 0)) + 1
        _save_settings(s)
        log.info(f"error report sent: {h} → {url} (status {status})")
        try: server_resp = json.loads(resp_body)
        except Exception: server_resp = {"raw": resp_body[:300].decode("utf-8", "replace")}
        return {"ok": True, "hash": h, "local_path": local_path,
                "server_response": server_resp,
                "url": url, "status": status}
    except Exception as e:
        log.warning(f"error report send failed: {e}")
        return {"ok": False, "err": str(e),
                "hash": h, "local_path": local_path,
                "url": url}


def submit_report_async(kind: str, message: str, detail: str = "",
                         user_note: str = "") -> None:
    """Fire-and-forget variant.  Use this from inside other modules
    when you want to report an error but don't want to block."""
    def _worker():
        try: submit_report(kind, message, detail, user_note)
        except Exception as e: log.debug(f"async report failed: {e}")
    threading.Thread(target=_worker, daemon=True,
                     name="error-report-async").start()


# ─── Status ────────────────────────────────────────────────────────

def list_local_reports(limit: int = 50) -> list:
    """Return recent local reports for review."""
    if not os.path.isdir(REPORTS_DIR):
        return []
    items = []
    try:
        for fn in os.listdir(REPORTS_DIR):
            full = os.path.join(REPORTS_DIR, fn)
            if not os.path.isfile(full): continue
            if not fn.endswith(".json"): continue
            try:
                st = os.stat(full)
                with open(full, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception: continue
            items.append({
                "path":    full,
                "name":    fn,
                "size":    st.st_size,
                "mtime":   st.st_mtime,
                "kind":    data.get("kind", ""),
                "message": data.get("message", "")[:200],
                "hash":    data.get("hash", ""),
            })
    except Exception: return []
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def get_status() -> dict:
    s = _load_settings()
    return {
        "settings":     s,
        "anon_id":      _get_anon_id(),
        "local_reports": len(list_local_reports(200)),
    }
