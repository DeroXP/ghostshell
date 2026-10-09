"""Names and on-disk locations shared by the updater's modules.

The updater is built and shipped separately from the app, so it cannot import
the app's config.py; the few names both sides must agree on are repeated here.
"""
from __future__ import annotations

import os
from datetime import datetime

APP_NAME = "Vispora"
# The app's name until 3.5.0.  Installs from that era still have a
# GhostShell.exe and a %APPDATA%/GhostShell folder on disk, and this updater
# has to be able to find and update them (config.LEGACY_APP_NAME is the same
# thing on the app side).
LEGACY_APP_NAME = "GhostShell"

# New name first, so an on-disk Vispora.exe wins over a leftover GhostShell.exe.
APP_EXE_NAMES = (f"{APP_NAME}.exe", f"{LEGACY_APP_NAME}.exe")


def _resolve_appdata_dir() -> str:
    """The folder the app keeps its data in: %APPDATA%/Vispora, or the
    pre-rename %APPDATA%/GhostShell while that is still the only one on disk.

    Mirrors config._resolve_appdata_dir() in the app, minus the rename: the app
    moves the old folder across on its own first launch, and the updater —
    which normally runs from inside that folder — only follows where it is.
    It must never create the new folder next to a populated old one, or the
    app would start using the empty one and the user's data would look gone.
    """
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    current = os.path.join(root, APP_NAME)
    legacy = os.path.join(root, LEGACY_APP_NAME)
    if os.path.isdir(legacy) and not os.path.isdir(current):
        return legacy
    return current


APPDATA_DIR  = _resolve_appdata_dir()
UPDATER_DIR  = os.path.join(APPDATA_DIR, "updater")
LOG_PATH     = os.path.join(UPDATER_DIR, "updater.log")
INSTALL_INFO = os.path.join(APPDATA_DIR, "install_info.json")  # written by the app

os.makedirs(UPDATER_DIR, exist_ok=True)


def log(line: str) -> None:
    """Append a timestamped line to updater.log.  Best-effort."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except Exception:
        pass
