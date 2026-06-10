"""GhostShellUpdater — standalone updater app.

Two-process architecture:
  • GhostShell.exe — the main app.  Lives in the user's chosen install
    directory.  When it detects an update, it:
       1. Writes install_info.json with target_dir, channel, current
          version, and PID.
       2. Spawns this updater with --auto-confirm.
       3. Exits.
  • GhostShellUpdater.exe — this app.  Lives in
    %APPDATA%/GhostShell/updater/GhostShellUpdater.exe.  Auto-installed
    by GhostShell on first run by downloading from Railway.

This file is the entry point.  We start Flask on a free port, point a
pywebview window at it, and let the user (or --auto-confirm) drive the
install through the UI.

Logic separation:
  main.py        — process entry, arg parsing, webview lifecycle
  server.py      — Flask routes + state management
  installer.py   — actual install steps (detect/close/download/swap)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from typing import Optional

# Updater's own version + the Railway server it talks to.  These are the
# only two pieces of state hard-coded into the binary — everything else
# (target dir, channel, etc) comes from args or install_info.json so the
# updater stays usable across re-locations.
UPDATER_VERSION  = "1.3.0"
SERVER_URL_DEFAULT = "https://ghostshell-site.up.railway.app"

# ─── Paths ───────────────────────────────────────────────────────────
APPDATA_DIR  = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                             "GhostShell")
UPDATER_DIR  = os.path.join(APPDATA_DIR, "updater")
STATE_PATH   = os.path.join(UPDATER_DIR, "state.json")
LOG_PATH     = os.path.join(UPDATER_DIR, "updater.log")
INSTALL_INFO = os.path.join(APPDATA_DIR, "install_info.json")  # written by GhostShell

os.makedirs(UPDATER_DIR, exist_ok=True)


def log(line: str) -> None:
    """Append a timestamped line to updater.log.  Best-effort."""
    try:
        from datetime import datetime
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except Exception:
        pass


def _find_free_port() -> int:
    """Find any free local port for the Flask backend.  Avoids hardcoded
    ports that would collide with GhostShell's 5987 or anything else."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def parse_args(argv) -> dict:
    """CLI:
        GhostShellUpdater.exe
            [--target-dir <path>]         GhostShell install directory
            [--target-exe <path>]         explicit GhostShell.exe path
            [--channel stable|beta]
            [--current-version <v>]
            [--target-version <v>]        version to install (auto if omitted)
            [--auto-confirm]              skip the "click to start" gate
            [--server <url>]              override the Railway URL
            [--silent]                    no window — install + exit
    """
    p = argparse.ArgumentParser(prog="GhostShellUpdater")
    p.add_argument("--target-dir",     default="")
    p.add_argument("--target-exe",     default="")
    p.add_argument("--channel",        default="", choices=("", "stable", "beta"))
    p.add_argument("--current-version", default="")
    p.add_argument("--target-version", default="")
    p.add_argument("--auto-confirm",   action="store_true")
    p.add_argument("--server",         default=SERVER_URL_DEFAULT)
    p.add_argument("--silent",         action="store_true")
    return vars(p.parse_args(argv))


def load_install_info() -> dict:
    """Read what GhostShell wrote about its own install (path, channel,
    version).  Returns {} if it doesn't exist — UI will then ask user."""
    if not os.path.exists(INSTALL_INFO):
        return {}
    try:
        with open(INSTALL_INFO, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        log(f"install_info.json load failed: {e}")
        return {}


def main():
    args = parse_args(sys.argv[1:])
    log(f"───────────────────────────────")
    log(f"GhostShellUpdater v{UPDATER_VERSION} invoked")
    log(f"  args: {args}")

    # Merge args + install_info.json — args win where both exist.
    info = load_install_info()
    log(f"  install_info: {info}")

    target_dir     = args["target_dir"]     or info.get("install_dir", "")
    target_exe     = args["target_exe"]     or info.get("install_path", "")
    channel        = args["channel"]        or info.get("channel", "")
    current_version = args["current_version"] or info.get("version", "")
    target_version = args["target_version"]
    server_url     = (args["server"] or SERVER_URL_DEFAULT).rstrip("/")

    # If target_exe is set but target_dir isn't, derive it
    if target_exe and not target_dir:
        target_dir = os.path.dirname(target_exe)
    # If target_dir is set but target_exe isn't, default to GhostShell.exe
    if target_dir and not target_exe:
        target_exe = os.path.join(target_dir, "GhostShell.exe")

    # v1.1.0 — if we don't know where GhostShell is (no args, no install
    # info), scan the PC for an existing install.  Found → update mode
    # with the discovered path; not found → install mode with a default
    # directory.  User can still pick a different one in the UI.
    mode = "update"
    scan_result = None
    if not target_exe:
        from updater.installer import find_existing_ghostshell, default_install_dir
        scan_result = find_existing_ghostshell()
        if scan_result.get("found"):
            target_exe = scan_result["path"]
            target_dir = scan_result["dir"]
            channel    = channel or scan_result.get("channel", "")
            current_version = current_version or scan_result.get("version", "")
            mode = "update"
            log(f"auto-scan: found existing GhostShell at {target_exe}")
        else:
            mode = "install"
            target_dir = default_install_dir()
            target_exe = os.path.join(target_dir, "GhostShell.exe")
            log(f"auto-scan: no existing GhostShell — install mode, "
                f"default dir {target_dir}")
    else:
        log(f"using explicit target from args: {target_exe}")

    # Initialize the server's state with whatever we know
    from updater.server import init_state, run_server
    init_state(
        target_dir      = target_dir,
        target_exe      = target_exe,
        channel         = channel,
        current_version = current_version,
        target_version  = target_version,
        server_url      = server_url,
        auto_confirm    = args["auto_confirm"],
        silent          = args["silent"],
        updater_version = UPDATER_VERSION,
        mode            = mode,
        scan_result     = scan_result,
    )

    if args["silent"]:
        # Headless mode — kick off install immediately and wait for it.
        # No UI window.  Useful for scripted upgrades.
        from updater.installer import run_install_sync
        log("running in --silent mode")
        rc = run_install_sync()
        log(f"silent install exit: {rc}")
        sys.exit(rc)

    # Find a free port + start Flask in a thread
    port = _find_free_port()
    log(f"Flask port: {port}")
    flask_thread = threading.Thread(
        target=lambda: run_server(port=port),
        daemon=True,
        name="updater-flask",
    )
    flask_thread.start()

    # Wait briefly for Flask to come up
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    # Open the pywebview window.  Frameless + custom titlebar (rendered
    # in index.html) so it looks like a smaller pane of GhostShell itself
    # instead of a generic OS-chrome dialog.  Rectangular aspect ratio —
    # the step list reads better as a wider+shorter panel than a square.
    try:
        import webview
        window = webview.create_window(
            "GhostShell Updater",
            f"http://127.0.0.1:{port}",
            width=720,
            height=580,
            min_size=(640, 540),
            background_color="#020203",
            frameless=True,
            easy_drag=False,
            resizable=False,
        )
        webview.start(debug=False)
    except ImportError:
        # pywebview missing — fall back to opening in default browser
        # so the user still has SOMETHING to interact with.  Highly
        # unusual but better than dying silently.
        log("pywebview not available — opening browser fallback")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
        # Block forever (Ctrl+C exits)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
