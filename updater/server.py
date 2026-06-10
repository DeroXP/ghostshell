"""Updater Flask backend — REST + UI server.

Routes:
  GET  /                      → UI (index.html)
  GET  /api/status            → current install state + progress
  POST /api/start             → kick off the install pipeline
  POST /api/cancel            → cancel an in-flight install
  POST /api/set-channel       → user picked a channel
  POST /api/set-target-dir    → user picked an install location
  POST /api/launch-app        → spawn the (just-installed) GhostShell
  POST /api/quit              → close the updater window
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_from_directory

# Shared state — populated by main.py's init_state(), mutated by the
# install pipeline, read by the UI's polling loop.
_state = {
    "updater_version":   "?",
    "target_dir":        "",
    "target_exe":        "",
    "channel":           "",
    "current_version":   "",
    "target_version":    "",
    "server_url":        "",
    "auto_confirm":      False,
    "silent":            False,
    # v1.1.0 — "install" or "update".  Set by main.py based on whether
    # an existing GhostShell.exe was found.  The UI shows a different
    # title + install-dir picker in install mode, and the pipeline
    # skips the close step.
    "mode":              "update",
    "scan_result":       None,

    # Operation status
    "phase":             "ready",       # ready|closing|downloading|verifying|installing|launching|done|error|cancelled
    "phase_label":       "Ready",
    "progress":          0,              # 0-100
    "downloaded_bytes":  0,
    "total_bytes":       0,
    "steps":             [],             # [{key, label, status: pending|active|done|error|skipped, detail}]
    "error":             None,
    "cancelled":         False,
    "started_at":        0.0,
    "ended_at":          0.0,
    "channel_check":     None,           # {available, version, sha256, size_bytes, notes, download_url}
}
_state_lock = threading.Lock()
_install_thread: Optional[threading.Thread] = None


# Standard 5-step pipeline.  Order MUST match the UI's renderer.
PIPELINE_STEPS = [
    {"key": "close",     "label": "Closing GhostShell"},
    {"key": "channel",   "label": "Checking release server"},
    {"key": "download",  "label": "Downloading new version"},
    {"key": "verify",    "label": "Verifying integrity"},
    {"key": "install",   "label": "Installing"},
    {"key": "launch",    "label": "Restarting GhostShell"},
]


def init_state(**kwargs) -> None:
    """Called by main.py before Flask starts.  Seeds the install
    context — target dir, channel, etc."""
    with _state_lock:
        _state.update(kwargs)
        _state["steps"] = [
            {**s, "status": "pending", "detail": ""}
            for s in PIPELINE_STEPS
        ]


def get_state_snapshot() -> dict:
    with _state_lock:
        return {
            **_state,
            "steps": [dict(s) for s in _state["steps"]],
        }


def update_step(key: str, status: str, detail: str = "") -> None:
    """Mark a step as pending/active/done/error/skipped.  UI polls
    /api/status and re-renders the pipeline checklist."""
    with _state_lock:
        for s in _state["steps"]:
            if s["key"] == key:
                s["status"] = status
                if detail:
                    s["detail"] = detail
                break


def update_progress(progress: int, downloaded: int = 0, total: int = 0,
                     phase: str = "", phase_label: str = "") -> None:
    with _state_lock:
        _state["progress"] = max(0, min(100, int(progress)))
        if downloaded: _state["downloaded_bytes"] = int(downloaded)
        if total:      _state["total_bytes"]      = int(total)
        if phase:      _state["phase"]            = phase
        if phase_label: _state["phase_label"]     = phase_label


def set_error(msg: str) -> None:
    with _state_lock:
        _state["phase"]       = "error"
        _state["phase_label"] = "Error"
        _state["error"]       = msg
        _state["ended_at"]    = time.time()


def is_cancelled() -> bool:
    with _state_lock:
        return _state["cancelled"]


def set_done() -> None:
    with _state_lock:
        _state["phase"]       = "done"
        _state["phase_label"] = "Update complete"
        _state["progress"]    = 100
        _state["ended_at"]    = time.time()


# ─── Flask app ───────────────────────────────────────────────────────

# Path that points into the updater package — when running frozen,
# PyInstaller will lay these out under sys._MEIPASS/updater/...
import sys
def _resource_dir() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    if "_MEIPASS" in dir(sys) or os.path.exists(os.path.join(base, "updater", "templates")):
        return os.path.join(base, "updater")
    return base


_RES_DIR = _resource_dir()
app = Flask(
    __name__,
    template_folder=os.path.join(_RES_DIR, "templates"),
    static_folder=os.path.join(_RES_DIR, "static"),
)


@app.route("/")
def index():
    return render_template("index.html",
                            updater_version=_state.get("updater_version", "?"))


@app.route("/api/status")
def api_status():
    return jsonify(get_state_snapshot())


@app.route("/api/start", methods=["POST"])
def api_start():
    global _install_thread
    if _install_thread and _install_thread.is_alive():
        return jsonify({"ok": False, "err": "install already running"})
    # Reset state for a fresh attempt
    with _state_lock:
        _state["phase"]       = "ready"
        _state["phase_label"] = "Starting"
        _state["error"]       = None
        _state["cancelled"]   = False
        _state["progress"]    = 0
        _state["started_at"]  = time.time()
        _state["ended_at"]    = 0.0
        for s in _state["steps"]:
            s["status"] = "pending"
            s["detail"] = ""
    from updater import installer
    _install_thread = threading.Thread(
        target=installer.run_install_pipeline,
        daemon=True,
        name="updater-pipeline",
    )
    _install_thread.start()
    return jsonify({"ok": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    with _state_lock:
        _state["cancelled"]   = True
        _state["phase"]       = "cancelled"
        _state["phase_label"] = "Cancelled"
    return jsonify({"ok": True})


@app.route("/api/set-channel", methods=["POST"])
def api_set_channel():
    data = request.get_json(force=True) or {}
    ch = data.get("channel")
    if ch not in ("stable", "beta"):
        return jsonify({"ok": False, "err": "channel must be 'stable' or 'beta'"})
    with _state_lock:
        _state["channel"] = ch
    return jsonify({"ok": True, "channel": ch})


@app.route("/api/set-target-dir", methods=["POST"])
def api_set_target_dir():
    data = request.get_json(force=True) or {}
    d = (data.get("target_dir") or "").strip()
    if not d:
        return jsonify({"ok": False, "err": "target_dir required"})
    # Validate it exists or that the parent does (we'll create the dir)
    parent = os.path.dirname(d) or d
    if not os.path.isdir(parent):
        return jsonify({"ok": False, "err": f"parent directory not found: {parent}"})
    with _state_lock:
        _state["target_dir"] = d
        _state["target_exe"] = os.path.join(d, "GhostShell.exe")
    return jsonify({"ok": True, "target_dir": d})


@app.route("/api/launch-app", methods=["POST"])
def api_launch_app():
    """User clicked 'Launch GhostShell' after the install finished."""
    import subprocess
    target = _state.get("target_exe", "")
    if not target or not os.path.isfile(target):
        return jsonify({"ok": False, "err": f"GhostShell not found at {target}"})
    try:
        subprocess.Popen(
            [target], close_fds=True,
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Tear down the updater process.  Used by the 'Close' button."""
    def _exit():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    """Re-run the PC scan for an existing GhostShell.exe.  Useful when
    the user just moved or installed GhostShell and wants to switch
    from install mode to update mode without restarting the updater."""
    from updater import installer
    found = installer.find_existing_ghostshell()
    with _state_lock:
        _state["scan_result"] = found
        if found.get("found"):
            _state["mode"]       = "update"
            _state["target_exe"] = found["path"]
            _state["target_dir"] = found["dir"]
            if found.get("channel") and not _state["channel"]:
                _state["channel"] = found["channel"]
            if found.get("version") and not _state["current_version"]:
                _state["current_version"] = found["version"]
        else:
            _state["mode"] = "install"
            # Suggest a default install dir if the user hasn't picked one
            if not _state["target_dir"]:
                _state["target_dir"] = installer.default_install_dir()
                _state["target_exe"] = os.path.join(_state["target_dir"],
                                                     "GhostShell.exe")
    return jsonify({"ok": True, "mode": _state["mode"], "found": found})


@app.route("/api/browse-folder", methods=["POST"])
def api_browse_folder():
    """Open the native folder-picker dialog via pywebview's open_file_dialog
    helper.  Returns the picked folder or empty string on cancel."""
    try:
        import webview
        windows = webview.windows
        if not windows:
            return jsonify({"ok": False, "err": "no webview window"})
        # FOLDER_DIALOG = 2 in pywebview
        result = windows[0].create_file_dialog(
            webview.FOLDER_DIALOG,
            allow_multiple=False,
            directory=_state.get("target_dir") or "",
        )
        if not result:
            return jsonify({"ok": True, "cancelled": True, "path": ""})
        picked = result[0] if isinstance(result, (list, tuple)) else result
        # Update the state with the new path
        with _state_lock:
            _state["target_dir"] = picked
            _state["target_exe"] = os.path.join(picked, "GhostShell.exe")
        return jsonify({"ok": True, "path": picked})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


def run_server(port: int):
    """Block — runs Flask on the given port.  Called from main.py's
    thread."""
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True,
             use_reloader=False)
