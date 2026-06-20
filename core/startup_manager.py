"""Startup Manager (beta.8) — gracefully launch a user's app list after Vispora boots.

When Vispora finishes starting (and the desktop has settled), it launches each
enabled app in the user's list, in order, STAGGERED (a gap between each so they
don't all spawn at once), DE-ELEVATED (started via explorer.exe so they run as
the normal user, not under Vispora's admin token), and SKIPPING anything that's
already running.  Everything is opt-in and the list is fully user-managed.
"""
import json
import os
import subprocess
import threading
import time

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("startup_mgr")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "startup_manager_settings.json")
_CREATE_NO_WINDOW = 0x08000000

_DEFAULTS = {
    "enabled": False,
    "initial_delay_s": 8,     # wait after Vispora boots before starting anything
    "stagger_s": 3,           # gap between consecutive launches
    "skip_if_running": True,
    "apps": [],               # [{id,name,path,delay_s,enabled,exists}]
}


def _load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {**_DEFAULTS, **(d if isinstance(d, dict) else {})}
    except Exception:
        return dict(_DEFAULTS)


def _save(s: dict):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log.warning(f"startup settings save failed: {e}")


def get_settings() -> dict:
    s = _load()
    for a in s.get("apps", []):
        a["exists"] = os.path.exists(a.get("path", ""))
    return s


def add_app(path, name=None, delay_s=0) -> dict:
    path = (path or "").strip().strip('"')
    if not path:
        return {"ok": False, "err": "No path given"}
    s = _load()
    nm = name or os.path.splitext(os.path.basename(path))[0] or path
    app = {
        "id": f"app{int(time.time() * 1000)}",
        "name": nm, "path": path,
        "delay_s": int(delay_s or 0), "enabled": True,
        "exists": os.path.exists(path),
    }
    s["apps"].append(app)
    _save(s)
    return {"ok": True, "app": app}


def remove_app(app_id) -> dict:
    s = _load()
    s["apps"] = [a for a in s.get("apps", []) if a.get("id") != app_id]
    _save(s)
    return {"ok": True}


def update_app(app_id, **fields) -> dict:
    s = _load()
    for a in s.get("apps", []):
        if a.get("id") == app_id:
            for k in ("name", "path", "delay_s", "enabled"):
                if fields.get(k) is not None:
                    a[k] = fields[k]
            a["exists"] = os.path.exists(a.get("path", ""))
            break
    _save(s)
    return {"ok": True}


def reorder(order_ids) -> dict:
    s = _load()
    idx = {aid: i for i, aid in enumerate(order_ids or [])}
    s["apps"].sort(key=lambda a: idx.get(a.get("id"), 9999))
    _save(s)
    return {"ok": True}


def set_settings(enabled=None, initial_delay_s=None, stagger_s=None, skip_if_running=None) -> dict:
    s = _load()
    if enabled is not None:          s["enabled"] = bool(enabled)
    if initial_delay_s is not None:  s["initial_delay_s"] = max(0, min(120, int(initial_delay_s)))
    if stagger_s is not None:        s["stagger_s"] = max(0, min(60, int(stagger_s)))
    if skip_if_running is not None:  s["skip_if_running"] = bool(skip_if_running)
    _save(s)
    return {"ok": True, "settings": get_settings()}


def _is_running(path) -> bool:
    exe = os.path.basename(path)
    if not exe:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        return exe.lower() in (r.stdout or "").lower()
    except Exception:
        return False


def _launch_one(app, skip_if_running) -> dict:
    name = app.get("name") or app.get("path")
    path = app.get("path", "")
    if not path or not os.path.exists(path):
        log.warning(f"startup: '{name}' path missing — skip")
        return {"name": name, "ok": False, "reason": "missing"}
    if skip_if_running and _is_running(path):
        log.info(f"startup: '{name}' already running — skip")
        return {"name": name, "ok": True, "reason": "already-running"}
    try:
        # De-elevate via explorer.exe — runs at the user's normal integrity
        # level (like a double-click), NOT under Vispora's admin token, so apps
        # behave exactly as they would if the user launched them themselves.
        subprocess.Popen(["explorer.exe", path], close_fds=True)
        log.info(f"startup: launched '{name}'")
        return {"name": name, "ok": True, "reason": "launched"}
    except Exception as e:
        log.warning(f"startup: '{name}' launch failed: {e}")
        return {"name": name, "ok": False, "reason": str(e)}


def launch_all(manual=False) -> dict:
    s = _load()
    if not manual and not s.get("enabled"):
        return {"ok": True, "skipped": "disabled"}
    apps = [a for a in s.get("apps", []) if a.get("enabled", True)]
    skip = s.get("skip_if_running", True)
    stagger = s.get("stagger_s", 3)
    results = []
    for i, a in enumerate(apps):
        if a.get("delay_s"):
            time.sleep(a["delay_s"])
        results.append(_launch_one(a, skip))
        if i < len(apps) - 1 and stagger:
            time.sleep(stagger)
    log.info(f"startup: launched {sum(1 for r in results if r['reason'] == 'launched')}/{len(apps)}")
    return {"ok": True, "results": results}


def launch_all_async(manual=False):
    threading.Thread(target=lambda: launch_all(manual=manual), daemon=True).start()
    return {"ok": True, "started": True}


def run_on_boot():
    """Called once on app startup.  Arms the staggered launch if enabled."""
    s = _load()
    enabled_apps = [a for a in s.get("apps", []) if a.get("enabled", True)]
    if not s.get("enabled") or not enabled_apps:
        return
    def _delayed():
        time.sleep(s.get("initial_delay_s", 8))
        launch_all()
    threading.Thread(target=_delayed, daemon=True).start()
    log.info(f"Startup Manager armed: {len(enabled_apps)} app(s), "
             f"{s.get('initial_delay_s')}s initial + {s.get('stagger_s')}s stagger")
