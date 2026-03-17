from pathlib import Path
import threading
import time
import socket
import logging
import os
import base64
from typing import Any, Iterable, Optional, Dict, List

from flask import Flask, Response, jsonify, render_template, request, stream_with_context, make_response, current_app
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
from core import benchmark

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
        return None


@app.after_request
def add_common_headers(response: Response):
    # Force no-store for API routes
    try:
        path = request.path
    except RuntimeError:
        path = ""
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(Exception)
def handle_exception(e: Exception):
    code = 500
    if isinstance(e, HTTPException):
        code = e.code or 500
    logger.exception("Unhandled exception: %s", e)
    if _is_api_request():
        return jsonify({"error": str(e)}), code
    return render_template("index.html"), code


@app.get("/")
def home():
    # Elevation page is optional — here we just render the index directly
    return render_template("index.html")


@app.get("/api/dashboard")
def api_dashboard():
    data = get_dashboard()
    return jsonify(data)


# ===== Logs SSE =====
@app.get("/api/logs/stream")
def api_logs_stream():
    def event_stream():
        for line in utils.stream_logs(heartbeat=utils.LOG_STREAM_HEARTBEAT_SECONDS):
            yield f"data: {line}\n\n"
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


# ===== Window Controls =====

def get_window() -> Optional[WindowType]:
    global _window_ref
    with _window_lock:
        return _window_ref


@app.post("/api/window/minimize")
def api_window_minimize():
    win = get_window()
    if win:
        try:
            webview.windows[0].minimize()
            utils.log_event("ui", "window", "minimize")
        except Exception as exc:
            utils.log_event("ui", "window", "error", str(exc))
    return jsonify({"ok": True})


@app.post("/api/window/close")
def api_window_close():
    win = get_window()
    if win:
        try:
            webview.windows[0].destroy()
            utils.log_event("ui", "window", "close")
        except Exception as exc:
            utils.log_event("ui", "window", "error", str(exc))
    return jsonify({"ok": True})


# ===== Elevation =====
@app.get("/elevate")
def elevate_page():
    return render_template("elevate.html")


@app.post("/api/elevate")
def api_elevate():
    if utils.is_admin():
        return jsonify({"already_admin": True})
    try:
        relaunched = utils.relaunch_as_admin()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"relaunching": bool(relaunched)})


# ===== Modules: Debloater =====
@app.post("/api/debloat/execute")
def api_debloat_execute():
    payload = _json()  # optional toggles later
    result = debloater.run_debloat()
    return jsonify(result)


# ===== Modules: Optimizer =====
@app.get("/api/optimizer/catalog")
def api_optimizer_catalog():
    return jsonify({"tweaks": optimizer.get_tweak_catalog()})


@app.post("/api/optimizer/apply")
def api_optimizer_apply():
    payload = _json(required=["selected"])
    selected = payload.get("selected") or []
    results = optimizer.apply_tweaks(selected)
    return jsonify({"results": results})


# ===== Modules: Privacy =====
@app.post("/api/privacy/harden")
def api_privacy_harden():
    summary = privacy.harden_all()
    return jsonify(summary)


# ===== Modules: DNS & Network =====
@app.get("/api/dns/presets")
def api_dns_presets():
    return jsonify({"presets": dns_manager.available_presets()})


@app.post("/api/dns/apply")
def api_dns_apply():
    data = _json()
    if "preset" in data:
        ok, msg = dns_manager.apply_preset(data.get("preset"))
    else:
        ok, msg = dns_manager.apply_custom(data.get("primary"), data.get("secondary"))
    return jsonify({"success": ok, "message": msg})


@app.get("/api/network/catalog")
def api_network_catalog():
    return jsonify({"tweaks": network_tweaks.get_tweak_catalog()})


@app.post("/api/network/apply")
def api_network_apply():
    data = _json(required=["selected"])
    selected = data.get("selected") or []
    results = network_tweaks.apply_tweaks(selected)
    return jsonify({"results": results})


@app.post("/api/network/ping")
def api_network_ping():
    data = _json(required=["host"])
    host = str(data.get("host") or "")
    metrics = utils.ping(host)
    return jsonify(metrics)


# ===== Modules: Startup Manager =====
@app.get("/api/startup/list")
def api_startup_list():
    return jsonify({"items": startup_manager.list_items()})


@app.post("/api/startup/disable")
def api_startup_disable():
    data = _json(required=["location", "name"])
    ok, msg = startup_manager.disable(data.get("location"), data.get("name"))
    return jsonify({"success": ok, "message": msg})


@app.post("/api/startup/enable")
def api_startup_enable():
    data = _json(required=["location", "name"])
    ok, msg = startup_manager.enable(data.get("location"), data.get("name"), data.get("command"))
    return jsonify({"success": ok, "message": msg})


# ===== Modules: Vault =====
@app.post("/api/vault/setup")
def api_vault_setup():
    data = _json(required=["pin"])
    ok = VAULT.setup_pin(data["pin"])
    return jsonify({"ok": ok})


@app.post("/api/vault/unlock")
def api_vault_unlock():
    data = _json(required=["pin"])
    ok, msg = VAULT.unlock(data["pin"])
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/vault/lock")
def api_vault_lock():
    VAULT.lock()
    return jsonify({"ok": True})


@app.get("/api/vault/entries")
def api_vault_entries():
    VAULT.autolock_check()
    return jsonify({"entries": VAULT.list_entries()})


@app.post("/api/vault/add")
def api_vault_add():
    data = _json(required=["service", "username", "password"])  # url, notes optional
    entry_id = VAULT.add_entry(
        service=data.get("service", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        url=data.get("url"),
        notes=data.get("notes"),
        tags=data.get("tags"),
    )
    return jsonify({"id": entry_id})


@app.post("/api/vault/update")
def api_vault_update():
    data = _json(required=["id"])
    ok = VAULT.update_entry(
        entry_id=int(data.get("id")),
        service=data.get("service"),
        username=data.get("username"),
        password=data.get("password"),
        url=data.get("url"),
        notes=data.get("notes"),
        tags=data.get("tags"),
    )
    return jsonify({"ok": ok})


@app.post("/api/vault/delete")
def api_vault_delete():
    data = _json(required=["id"])
    ok = VAULT.delete_entry(int(data.get("id")))
    return jsonify({"ok": ok})


@app.post("/api/vault/copy")
def api_vault_copy():
    data = _json(required=["id"])
    ok = VAULT.copy_password_to_clipboard(int(data.get("id")))
    return jsonify({"ok": ok})


@app.get("/api/vault/export")
def api_vault_export():
    blob = VAULT.export_encrypted()
    b64 = base64.b64encode(blob).decode("ascii")
    resp = make_response(jsonify({"blob_b64": b64}))
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


# ===== Cleaner APIs =====
import core.cleaner as cleaner

@app.route("/api/cleaner/targets", methods=["GET"])
def api_cleaner_targets() -> Response:
    try:
        targets = cleaner.list_targets()
        response = jsonify({"targets": targets})
    except Exception as exc:
        utils.log_event("cleaner_targets_error", str(exc))
        response = jsonify({"error": "Unable to retrieve cleaner targets"})
        response.status_code = 500
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/cleaner/clean", methods=["POST"])
def api_cleaner_clean() -> Response:
    payload = request.get_json(silent=True)
    if not payload or "selected" not in payload:
        response = jsonify({"error": "Request JSON must include a 'selected' list"})
        response.status_code = 400
        response.headers["Cache-Control"] = "no-store"
        return response

    selected = payload.get("selected")
    if not isinstance(selected, list) or not selected:
        response = jsonify({"error": "'selected' must be a non-empty list"})
        response.status_code = 400
        response.headers["Cache-Control"] = "no-store"
        return response

    try:
        results = cleaner.clean(selected)
        response = jsonify({"results": results})
    except Exception as exc:
        utils.log_event("cleaner_clean_error", str(exc))
        response = jsonify({"error": "Cleaner execution failed"})
        response.status_code = 500

    response.headers["Cache-Control"] = "no-store"
    return response


# ===== GPU + HWID APIs =====
from typing import Any, Dict
from core import nvidia_clean, hwid_spoofer


def _build_no_store_response(payload: Dict[str, Any], status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


def _result_to_success_message(result: Any, fallback_message: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"success": True, "message": fallback_message}
    if isinstance(result, dict):
        if "success" in result:
            payload["success"] = bool(result["success"])
        if result.get("message"):
            payload["message"] = str(result["message"])
    elif isinstance(result, (list, tuple)):
        if len(result) >= 1:
            payload["success"] = bool(result[0])
        if len(result) >= 2 and result[1] is not None:
            payload["message"] = str(result[1])
    elif isinstance(result, bool):
        payload["success"] = result
    elif result is not None:
        payload["message"] = str(result)
    return payload


def _normalize_prepare_result(result: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": True,
        "message": "Preparation completed successfully.",
        "readme": None,
    }
    if isinstance(result, dict):
        if "success" in result:
            payload["success"] = bool(result["success"])
        if result.get("message"):
            payload["message"] = str(result["message"])
        if "readme" in result:
            payload["readme"] = result["readme"]
        elif "readme_path" in result:
            payload["readme"] = result["readme_path"]
    elif isinstance(result, (list, tuple)):
        if len(result) >= 1:
            payload["success"] = bool(result[0])
        if len(result) >= 2 and result[1] is not None:
            payload["message"] = str(result[1])
        if len(result) >= 3:
            payload["readme"] = result[2]
    elif isinstance(result, bool):
        payload["success"] = result
    elif result is not None:
        payload["message"] = str(result)
    return payload


@app.get("/api/gpu/info")
def api_gpu_info():
    try:
        info = nvidia_clean.detect_gpu()
        return _build_no_store_response({"info": info})
    except Exception as exc:
        utils.log_event("gpu", "api", "error", str(exc))
        return _build_no_store_response({"error": "Unable to retrieve GPU information."}, 500)


@app.post("/api/gpu/prepare")
def api_gpu_prepare():
    data = request.get_json(silent=True) or {}
    workdir = data.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        return _build_no_store_response({"error": "workdir must be a string."}, 400)
    try:
        result = nvidia_clean.prepare_clean_install_scripts(workdir=workdir)
        payload = _normalize_prepare_result(result)
        return _build_no_store_response(payload)
    except Exception as exc:
        utils.log_event("gpu", "api", "error", str(exc))
        return _build_no_store_response({"error": "Unable to prepare GPU clean install scripts."}, 500)


@app.post("/api/gpu/tweaks")
def api_gpu_tweaks():
    try:
        result = nvidia_clean.apply_registry_tweaks()
        payload = _result_to_success_message(result, "Registry tweaks applied successfully.")
        return _build_no_store_response(payload)
    except Exception as exc:
        utils.log_event("gpu", "api", "error", str(exc))
        return _build_no_store_response({"error": "Unable to apply GPU tweaks."}, 500)


@app.post("/api/hwid/apply")
def api_hwid_apply():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _build_no_store_response({"error": "JSON body is required."}, 400)
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        return _build_no_store_response({"error": '"changes" must be a non-empty object.'}, 400)
    try:
        result = hwid_spoofer.apply(changes)
        payload = _result_to_success_message(result, "HWID changes applied successfully.")
        return _build_no_store_response(payload)
    except Exception as exc:
        utils.log_event("hwid", "api", "error", str(exc))
        return _build_no_store_response({"error": "Unable to apply HWID changes."}, 500)


@app.post("/api/hwid/restore")
def api_hwid_restore():
    try:
        result = hwid_spoofer.restore()
        payload = _result_to_success_message(result, "HWID restored successfully.")
        return _build_no_store_response(payload)
    except Exception as exc:
        utils.log_event("hwid", "api", "error", str(exc))
        return _build_no_store_response({"error": "Unable to restore HWID."}, 500)


# Utils
@app.route('/api/utils/guid', methods=['GET'])
def api_utils_guid() -> Response:
    try:
        guid = utils.generate_guid()
        response = jsonify({'guid': guid})
    except Exception:
        app.logger.exception("Failed to generate GUID")
        response = jsonify({'error': 'Failed to generate GUID'})
        response.status_code = 500
    response.headers['Cache-Control'] = 'no-store'
    return response


# ===== Benchmark APIs =====
@app.route("/api/benchmark/run")
def api_benchmark_run() -> Response:
    targets_param = request.args.get("targets")
    duration_param = request.args.get("duration")

    duration_s: Optional[float] = None
    if duration_param not in (None, ""):
        try:
            duration_s = float(duration_param)
        except (TypeError, ValueError):
            return jsonify({"error": "duration must be a valid number"}), 400
        if duration_s <= 0:
            return jsonify({"error": "duration must be greater than zero"}), 400

    targets: Optional[List[str]] = None
    if targets_param:
        parsed_targets = [target.strip() for target in targets_param.split(",") if target.strip()]
        if parsed_targets:
            targets = parsed_targets

    try:
        # If duration_s is None, benchmark.run_benchmark will use its default
        result = benchmark.run_benchmark(targets=targets, duration_s=duration_s or 2.0)
    except Exception as exc:
        current_app.logger.exception("Benchmark execution failed")
        return jsonify({"error": str(exc)}), 500

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/benchmark/history")
def api_benchmark_history() -> Response:
    try:
        history = benchmark.load_history()
    except Exception as exc:
        current_app.logger.exception("Failed to load benchmark history")
        return jsonify({"error": str(exc)}), 500
    return jsonify(history)


@app.route("/api/benchmark/clear", methods=["POST"])
def api_benchmark_clear() -> Response:
    try:
        benchmark.clear_history()
    except Exception as exc:
        current_app.logger.exception("Failed to clear benchmark history")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


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
