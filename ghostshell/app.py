from pathlib import Path
import threading
import time
import socket
import logging
import os
import base64
from typing import Any, Iterable, Optional, Dict

from flask import Flask, Response, jsonify, render_template, request, stream_with_context, make_response
from werkzeug.exceptions import HTTPException, BadRequest
import webview

from config import (
    FLASK_HOST,
    FLASK_PORT,
    APP_NAME,
    APP_VERSION,
    WINDOW_WIDTH,
    WINDOW_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_MIN_HEIGHT,
)
from core import utils
from core.system_info import get_dashboard
from core import debloater, optimizer, privacy, dns_manager, network_tweaks, startup_manager
from core.vault import Vault

utils.ensure_app_dirs()
logger = utils.get_logger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

if os.name != "nt":
    logger.warning("GhostShell is designed for Windows environments. Current platform: %s", os.name)

BASE_DIR = Path(__file__).resolve().parent
logger.debug("GhostShell module initialized at %s", BASE_DIR)

app = Flask(__name__, static_folder="static", template_folder="templates")

WindowType = Any
_window_lock = threading.Lock()
_window_ref: Optional[WindowType] = None

# Singleton vault
VAULT = Vault.open_or_init()


def _is_api_request() -> bool:
    try:
        path = request.path
    except RuntimeError:
        path = ""
    return path.startswith("/api/")


def _json(required: Iterable[str] | None = None) -> Dict[str, Any]:
    data = request.get_json(silent=True) or {}
    if required:
        missing = [k for k in required if k not in data]
        if missing:
            raise BadRequest(f"Missing fields: {', '.join(missing)}")
    return data


def get_window() -> Optional[WindowType]:
    global _window_ref
    with _window_lock:
        if _window_ref is not None:
            return _window_ref
        windows = getattr(webview, "windows", [])
        if windows:
            _window_ref = windows[0]
            return _window_ref
        return None


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    logger.warning("HTTP error %s on %s: %s", exc.code, request.path, exc.description)
    if _is_api_request():
        response = jsonify({"error": exc.description or str(exc), "code": exc.code})
        response.status_code = exc.code
        return response
    return render_template("index.html")


@app.get("/")
def index():
    # If not admin, show elevate screen
    if not utils.is_admin():
        return render_template("elevate.html")
    return render_template("index.html")


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "version": APP_VERSION})


@app.post("/api/elevate")
def api_elevate():
    if utils.is_admin():
        return jsonify({"ok": True, "already_admin": True})
    try:
        utils.relaunch_as_admin()
        return jsonify({"ok": True, "relaunching": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/logs/tail")
def api_logs_tail():
    tail = list(utils.tail_logs(400))
    return jsonify({"lines": tail})


@app.get("/api/logs/stream")
def api_logs_stream():
    def _gen():
        for line in utils.stream_logs():
            if line is None:
                yield ": keep-alive\n\n"
            else:
                yield f"data: {line}\n\n"
    return Response(stream_with_context(_gen()), mimetype="text/event-stream")


@app.get("/api/ping")
def api_ping():
    host = request.args.get("host", "127.0.0.1")
    return jsonify(utils.ping(host))


@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(get_dashboard())


@app.post("/api/restore-point")
def api_restore_point():
    ok = utils.create_system_restore_point("GhostShell Pre-Modification")
    return jsonify({"ok": bool(ok)})


@app.post("/api/window/<action>")
def api_window_action(action: str):
    window = get_window()
    if not window:
        return jsonify({"error": "window not available"}), 400
    try:
        if action == "minimize":
            window.minimize()
            utils.log_event("app", "window", "minimize")
        elif action == "close":
            window.destroy()
            utils.log_event("app", "window", "close")
        else:
            return jsonify({"error": "unsupported action"}), 400
    except Exception as exc:
        utils.log_event("app", "window", "error", str(exc))
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "action": action})


# ===== Debloater =====
@app.get("/api/debloat/plan")
def api_debloat_plan():
    return jsonify(debloater.get_default_plan())


@app.post("/api/debloat/run")
def api_debloat_run():
    payload = _json(required=["selected"])  # expected dict
    selected = payload.get("selected") or {}
    results = debloater.debloat(selected)
    return jsonify({"results": results})


# ===== Optimizer =====
@app.get("/api/optimize/plan")
def api_optimize_plan():
    return jsonify({"catalog": optimizer.get_tweak_catalog()})


@app.post("/api/optimize/apply")
def api_optimize_apply():
    payload = _json(required=["selected"])
    selected = payload.get("selected") or []
    results = optimizer.apply_tweaks(list(selected))
    return jsonify({"results": results})


# ===== Network =====
@app.get("/api/network/presets")
def api_network_presets():
    return jsonify(dns_manager.available_presets())


@app.get("/api/network/current")
def api_network_current():
    return jsonify({
        "dns": dns_manager.get_current_dns(),
        "adapters": utils.list_adapters(),
    })


@app.post("/api/network/apply_preset")
def api_network_apply_preset():
    payload = _json(required=["name"])
    ok, message = dns_manager.apply_preset(payload["name"]) 
    return jsonify({"ok": ok, "message": message})


@app.post("/api/network/apply_custom")
def api_network_apply_custom():
    payload = _json(required=["primary"])  # secondary optional
    ok, message = dns_manager.apply_custom(payload["primary"], payload.get("secondary"))
    return jsonify({"ok": ok, "message": message})


@app.get("/api/network/test_latency")
def api_network_test_latency():
    hosts = request.args.get("hosts", "1.1.1.1,8.8.8.8").split(",")
    try:
        count = int(request.args.get("count", "4"))
    except ValueError:
        count = 4
    results = {}
    # Use utils.ping inline per host
    for h in [x.strip() for x in hosts if x.strip()]:
        results[h] = utils.ping(h, count=count)
    return jsonify({"results": results})


# ===== Privacy =====
@app.post("/api/privacy/harden")
def api_privacy_harden():
    return jsonify(privacy.harden_all())


# ===== Startup Manager =====
@app.get("/api/startup/list")
def api_startup_list():
    return jsonify(startup_manager.list_startup())


@app.post("/api/startup/disable")
def api_startup_disable():
    payload = _json(required=["location", "name"])
    ok, message = startup_manager.disable_item(payload["location"], payload["name"]) 
    return jsonify({"ok": ok, "message": message})


@app.post("/api/startup/enable")
def api_startup_enable():
    payload = _json(required=["location", "name"])  # command optional
    ok, message = startup_manager.enable_item(payload["location"], payload["name"], payload.get("command"))
    return jsonify({"ok": ok, "message": message})


# ===== Vault =====
@app.get("/api/vault/status")
def api_vault_status():
    return jsonify({
        "initialized": VAULT.is_initialized(),
        "has_pin": VAULT.has_pin(),
        "unlocked": VAULT.is_unlocked(),
    })


@app.post("/api/vault/create_pin")
def api_vault_create_pin():
    payload = _json(required=["pin"])
    VAULT.create_pin(str(payload["pin"]))
    return jsonify({"ok": True})


@app.post("/api/vault/unlock")
def api_vault_unlock():
    payload = _json(required=["pin"])
    ok = VAULT.unlock(str(payload["pin"]))
    return jsonify({"ok": bool(ok)})


@app.post("/api/vault/lock")
def api_vault_lock():
    VAULT.lock()
    return jsonify({"ok": True})


@app.get("/api/vault/entries")
def api_vault_entries_list():
    query = request.args.get("query")
    tag = request.args.get("tag")
    entries = VAULT.list_entries(query=query, tag=tag)
    return jsonify(entries)


@app.post("/api/vault/entries")
def api_vault_entries_add():
    payload = _json(required=["service", "username", "password"])  # url, notes, tags optional
    entry_id = VAULT.add_entry(
        payload["service"], payload["username"], payload["password"],
        payload.get("url"), payload.get("notes"), payload.get("tags"),
    )
    return jsonify({"id": entry_id})


@app.get("/api/vault/entries/<int:entry_id>")
def api_vault_entry_get(entry_id: int):
    data = VAULT.get_entry(entry_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    return jsonify(data)


@app.put("/api/vault/entries/<int:entry_id>")
def api_vault_entry_update(entry_id: int):
    payload = _json()  # arbitrary fields validated in update_entry
    ok = VAULT.update_entry(entry_id, **payload)
    return jsonify({"ok": bool(ok)})


@app.delete("/api/vault/entries/<int:entry_id>")
def api_vault_entry_delete(entry_id: int):
    ok = VAULT.delete_entry(entry_id)
    return jsonify({"ok": bool(ok)})


@app.get("/api/vault/export")
def api_vault_export():
    blob = VAULT.export_encrypted()
    resp = make_response(blob)
    resp.headers["Content-Type"] = "application/octet-stream"
    resp.headers["Content-Disposition"] = 'attachment; filename="vault.dat"'
    return resp


@app.post("/api/vault/import")
def api_vault_import():
    data = request.get_data()
    if not data:
        payload = _json(required=["blob_b64"])
        data = base64.b64decode(payload["blob_b64"]) if payload.get("blob_b64") else b""
    imported = VAULT.import_encrypted(data)
    return jsonify({"imported": imported})


# ===== Server + Window bootstrap =====

def start_flask():
    utils.log_event("app", "flask", "start", f"{FLASK_HOST}:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True, use_reloader=False)


def wait_for_server_ready(host: str, port: int, timeout: float = 15.0) -> bool:
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    utils.log_event("app", "startup", "begin", APP_VERSION)
    flask_thread = threading.Thread(target=start_flask, name="GhostShell-Server", daemon=True)
    flask_thread.start()

    if not wait_for_server_ready(FLASK_HOST, FLASK_PORT):
        utils.log_event("app", "startup", "error", "Flask server timeout")
        return

    url = f"http://{FLASK_HOST}:{FLASK_PORT}"
    window = webview.create_window(
        title=f"{APP_NAME} {APP_VERSION}",
        url=url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        resizable=True,
        frameless=True,
        easy_drag=False,
    )
    global _window_ref
    with _window_lock:
        _window_ref = window

    utils.log_event("app", "window", "create", url)
    try:
        webview.start()
    finally:
        utils.log_event("app", "shutdown", "end")


if __name__ == "__main__":
    main()
