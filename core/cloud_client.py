"""HTTP client for the GhostShell-Cloud backend.

Design rules (in priority order):
  1. **Dormant by default.**  If the user hasn't set a cloud URL in
     Settings, every method here is a no-op.  No network calls, no
     surprises.  This is what makes the cloud feature truly opt-in.
  2. **Never block the UI.**  Every method is a fire-and-forget thread.
     Network failures get logged and the request lands in the offline
     retry queue, but they never raise into the calling code.
  3. **Anonymous.**  We send the device UUID in the X-Device-ID header
     and that's the only credential the server gets.  No tokens, no PII.
  4. **Crash-safe.**  Settings sync uses optimistic write — we apply
     locally first, then push.  If the push fails the queue picks it up
     later.

Settings are stored in `%APPDATA%\\GhostShell\\cloud_client_settings.json`:
    {
      "cloud_url":            "https://ghostshell-cloud.up.railway.app",
      "telemetry_opt_in":     false,
      "sync_opt_in":          false,
      "news_opt_in":          true,
      "update_channel":       "stable",
      "last_heartbeat":       "2026-05-07T14:33:21",
      "last_register_version": "0.1.0"
    }
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from config import APPDATA_DIR
from core.device_id import collect_hardware_fingerprint, get_device_id
from core.utils import get_logger, atomic_write_json

log = get_logger("cloud_client")

# ─── Persistence ─────────────────────────────────────────────────────────
SETTINGS_PATH    = os.path.join(APPDATA_DIR, "cloud_client_settings.json")
RETRY_QUEUE_PATH = os.path.join(APPDATA_DIR, "cloud_retry_queue.json")

DEFAULT_SETTINGS: dict[str, Any] = {
    "cloud_url":             "",
    "telemetry_opt_in":      False,
    "sync_opt_in":           False,
    "news_opt_in":           True,
    "update_channel":        "stable",
    "last_heartbeat":        None,
    "last_register_version": None,
}

# How often (in seconds) to send a heartbeat while the app is running.
HEARTBEAT_INTERVAL_SEC = 6 * 60 * 60   # 6 hours
HEARTBEAT_TIMEOUT_SEC  = 10
REGISTER_TIMEOUT_SEC   = 15
SETTINGS_TIMEOUT_SEC   = 8
RETRY_INTERVAL_SEC     = 15 * 60       # process queue every 15 min

_settings_lock = threading.Lock()
_queue_lock    = threading.Lock()

# A heartbeat thread, started lazily by `start_background_loop()`.
_bg_thread: threading.Thread | None = None
_bg_stop:   threading.Event       = threading.Event()


# ─── Settings file ───────────────────────────────────────────────────────
def _load_settings() -> dict[str, Any]:
    with _settings_lock:
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(data)
                return merged
        except Exception as e:
            log.warning(f"Could not load cloud settings ({e}); using defaults")
        return dict(DEFAULT_SETTINGS)


def _save_settings(s: dict[str, Any]) -> None:
    with _settings_lock:
        atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict[str, Any]:
    """Public read.  Returned dict is safe to expose to the UI directly."""
    return _load_settings()


def set_cloud_url(url: str) -> dict[str, Any]:
    """Set the backend URL.  An empty string disables cloud entirely."""
    url = (url or "").strip().rstrip("/")
    s = _load_settings()
    s["cloud_url"] = url
    _save_settings(s)
    log.info(f"Cloud URL set to {url!r} ({'enabled' if url else 'disabled'})")
    if url:
        # Eagerly register/refresh hardware so the dashboard is populated.
        threading.Thread(target=register, daemon=True).start()
    return s


def update_local_settings(**kwargs) -> dict[str, Any]:
    """Update opt-in flags / update channel locally and push to cloud
    asynchronously.  The local copy is the source of truth — even if the
    push fails, the UI reflects the new value immediately.
    """
    allowed = {"telemetry_opt_in", "sync_opt_in", "news_opt_in", "update_channel"}
    s = _load_settings()
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k == "update_channel" and v not in ("stable", "beta"):
            # v3 — channels renamed from "insiders" → "beta".  Silently
            # remap legacy values from older cloud snapshots.
            if v == "insiders":
                v = "beta"
            else:
                log.warning(f"Ignoring invalid update_channel={v!r}")
                continue
            continue
        s[k] = v
    _save_settings(s)

    if is_enabled():
        threading.Thread(target=_push_settings, daemon=True).start()
    return s


# ─── Public state ────────────────────────────────────────────────────────
def is_enabled() -> bool:
    """True iff the user has opted into cloud by setting a URL."""
    return bool(_load_settings().get("cloud_url"))


def device_id() -> str:
    return get_device_id()


# ─── HTTP helpers ────────────────────────────────────────────────────────
def _request(method: str, path: str, body: dict | None = None,
             timeout: float = 10.0) -> dict[str, Any]:
    """Minimal HTTP wrapper using urllib (no extra deps).  Returns:
        {"ok": bool, "status": int, "data": dict|None, "err": str}
    """
    s = _load_settings()
    base = (s.get("cloud_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "status": 0, "data": None, "err": "cloud disabled"}

    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept":       "application/json",
        "User-Agent":   "GhostShell-Client",
        "X-Device-ID":  get_device_id(),
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return {"ok": 200 <= resp.status < 300,
                    "status": resp.status, "data": parsed, "err": ""}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return {"ok": False, "status": e.code, "data": None,
                "err": f"HTTP {e.code}: {err_body[:200]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": 0, "data": None,
                "err": f"network: {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": 0, "data": None,
                "err": f"{type(e).__name__}: {e}"}


# ─── Retry queue ─────────────────────────────────────────────────────────
def _load_queue() -> list[dict]:
    with _queue_lock:
        try:
            if os.path.exists(RETRY_QUEUE_PATH):
                with open(RETRY_QUEUE_PATH, "r", encoding="utf-8") as f:
                    q = json.load(f)
                if isinstance(q, list):
                    return q
        except Exception as e:
            log.warning(f"Could not load retry queue ({e}); discarding")
        return []


def _save_queue(q: list[dict]) -> None:
    with _queue_lock:
        try:
            os.makedirs(APPDATA_DIR, exist_ok=True)
            with open(RETRY_QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(q, f, indent=2)
        except Exception as e:
            log.warning(f"Could not save retry queue: {e}")


def _enqueue(method: str, path: str, body: dict | None) -> None:
    q = _load_queue()
    q.append({
        "method": method, "path": path, "body": body,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempts": 0,
    })
    # Cap queue length to keep AppData bounded.
    if len(q) > 50:
        q = q[-50:]
    _save_queue(q)


def _drain_queue() -> None:
    if not is_enabled():
        return
    q = _load_queue()
    if not q:
        return
    survivors: list[dict] = []
    for item in q:
        item["attempts"] = int(item.get("attempts", 0)) + 1
        r = _request(item["method"], item["path"], item.get("body"),
                     timeout=SETTINGS_TIMEOUT_SEC)
        if r["ok"]:
            log.info(f"Drained queued {item['method']} {item['path']}")
            continue
        # Drop after 10 attempts.  Better to lose the queued event than
        # to spam the server forever.
        if item["attempts"] < 10:
            survivors.append(item)
        else:
            log.warning(f"Dropping queued {item['method']} {item['path']} after 10 attempts")
    _save_queue(survivors)


# ─── Public actions ──────────────────────────────────────────────────────
def register() -> dict[str, Any]:
    """Register or refresh this device.  Idempotent on the server.
    Updates `last_register_version` so we know when to re-register
    (e.g. after a hardware swap detected via /api/system).
    """
    if not is_enabled():
        return {"ok": False, "err": "cloud disabled"}
    hw = collect_hardware_fingerprint()
    body = {"device_id": get_device_id(), "hardware": hw}
    r = _request("POST", "/v1/devices/register", body, timeout=REGISTER_TIMEOUT_SEC)
    if r["ok"]:
        log.info(f"Registered with cloud (gpu={hw.get('gpu_model')!r}, "
                 f"new={r['data'].get('is_new') if r.get('data') else '?'})")
        s = _load_settings()
        s["last_register_version"] = hw.get("app_version")
        s["last_heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Server returns the canonical settings — pull them down so the UI
        # reflects whatever the user has on their other devices.
        cloud_settings = (r.get("data") or {}).get("settings") or {}
        for k in ("telemetry_opt_in", "sync_opt_in", "news_opt_in", "update_channel"):
            if k in cloud_settings:
                s[k] = cloud_settings[k]
        _save_settings(s)
    else:
        log.warning(f"Cloud register failed ({r['err']})")
        _enqueue("POST", "/v1/devices/register", body)
    return r


def heartbeat(force: bool = False) -> dict[str, Any]:
    """Refresh `last_seen`.  Skips the call if we did one in the last
    HEARTBEAT_INTERVAL_SEC unless `force=True`.
    """
    if not is_enabled():
        return {"ok": False, "err": "cloud disabled"}

    s = _load_settings()
    if not force:
        last = s.get("last_heartbeat")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                if age < HEARTBEAT_INTERVAL_SEC:
                    return {"ok": True, "skipped": True, "age": int(age)}
            except Exception:
                pass

    did = get_device_id()
    body = {"hardware": collect_hardware_fingerprint()}
    r = _request("POST", f"/v1/devices/{did}/heartbeat", body,
                 timeout=HEARTBEAT_TIMEOUT_SEC)
    if r["ok"]:
        s["last_heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save_settings(s)
    elif r.get("status") == 404:
        # Server lost us (TTL prune, or a DB wipe in early dev).
        # Re-register and try again.
        log.info("Cloud doesn't know this device; re-registering")
        register()
    else:
        log.debug(f"Heartbeat failed ({r['err']}); will retry")
    return r


def _push_settings() -> dict[str, Any]:
    """Push current local settings to the cloud."""
    if not is_enabled():
        return {"ok": False, "err": "cloud disabled"}
    s = _load_settings()
    body = {
        "telemetry_opt_in": bool(s.get("telemetry_opt_in", False)),
        "sync_opt_in":      bool(s.get("sync_opt_in", False)),
        "news_opt_in":      bool(s.get("news_opt_in", True)),
        "update_channel":   s.get("update_channel", "stable"),
    }
    did = get_device_id()
    r = _request("PUT", f"/v1/devices/{did}/settings", body,
                 timeout=SETTINGS_TIMEOUT_SEC)
    if r["ok"]:
        log.info("Synced settings to cloud")
    elif r.get("status") == 404:
        register()  # auto-recover
    else:
        log.warning(f"Settings push failed ({r['err']}); queued")
        _enqueue("PUT", f"/v1/devices/{did}/settings", body)
    return r


def fetch_remote_settings() -> dict[str, Any]:
    """Pull the canonical settings from the cloud.  Useful when the user
    enables cloud on a second device — they should see whatever they
    chose on their first device.
    """
    if not is_enabled():
        return {"ok": False, "err": "cloud disabled"}
    did = get_device_id()
    r = _request("GET", f"/v1/devices/{did}/settings", timeout=SETTINGS_TIMEOUT_SEC)
    if r["ok"] and isinstance(r.get("data"), dict):
        s = _load_settings()
        for k in ("telemetry_opt_in", "sync_opt_in", "news_opt_in", "update_channel"):
            if k in r["data"]:
                s[k] = r["data"][k]
        _save_settings(s)
    elif r.get("status") == 404:
        register()
    return r


# ─── Background loop ─────────────────────────────────────────────────────
def _background_loop() -> None:
    """Slow-tick loop: heartbeat + drain retry queue.  Runs at most once
    per minute, but inside the loop we use the per-action thresholds so
    we don't actually hit the network every minute.
    """
    log.info("cloud_client background loop started")
    # First boot: register or heartbeat.  Skip if cloud is disabled — the
    # user can flip it on without restarting the app, and we'll pick up
    # next loop iteration.
    try:
        if is_enabled():
            register()
    except Exception as e:
        log.warning(f"Startup register failed: {e}")

    last_retry = 0.0
    while not _bg_stop.is_set():
        try:
            if is_enabled():
                # Heartbeat is internally rate-limited to HEARTBEAT_INTERVAL_SEC.
                heartbeat()
                if time.time() - last_retry > RETRY_INTERVAL_SEC:
                    _drain_queue()
                    last_retry = time.time()
        except Exception as e:
            log.warning(f"cloud_client loop tick failed: {e}")
        # Wake every 60s so a settings change (cloud_url toggled on) is
        # picked up promptly.
        if _bg_stop.wait(60):
            break
    log.info("cloud_client background loop stopped")


def start_background_loop() -> None:
    """Idempotent — safe to call from app startup even if the user hasn't
    enabled cloud yet.  The loop itself checks `is_enabled()` each tick.
    """
    global _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return
    _bg_stop.clear()
    _bg_thread = threading.Thread(
        target=_background_loop, name="cloud-client-loop", daemon=True,
    )
    _bg_thread.start()


def stop_background_loop() -> None:
    """Used on app shutdown.  Best-effort — we're a daemon thread anyway."""
    _bg_stop.set()


# ─── Diagnostics (for the Settings panel) ────────────────────────────────
def health_check() -> dict[str, Any]:
    """One-shot reachability probe.  Used by the Settings UI to show a
    green/red dot next to the cloud URL field.
    """
    if not is_enabled():
        return {"ok": False, "enabled": False, "err": "cloud URL not set"}
    r = _request("GET", "/healthz", timeout=5.0)
    return {
        "ok":      r["ok"],
        "enabled": True,
        "status":  r.get("status", 0),
        "data":    r.get("data"),
        "err":     r.get("err", ""),
    }
