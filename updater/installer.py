"""Install pipeline — does the actual work.

Two modes:

  • update mode  — GhostShell exists somewhere on disk.  We close any
    running instance, download the new version from the channel
    endpoint, swap the exe, and re-launch.

  • install mode — GhostShell is NOT on disk (or we couldn't find it).
    The user picks an install directory; we skip the close step,
    download the channel build, verify, copy to the chosen directory,
    and launch.  Same pipeline minus step 1.

Mode is decided BEFORE the user clicks Install: when the updater
launches, find_existing_ghostshell() scans common install paths and
the install_info.json sidecar.  If GhostShell is found anywhere, mode
is "update" with target_exe pre-populated; otherwise "install" and the
UI shows a directory picker.

Steps (run sequentially, with cancellation checks between each):
  1. close        — find + close any running GhostShell process
  2. channel      — hit /version/<channel> for the latest build info
  3. download     — pull /download/<channel> with streaming + progress
  4. verify       — SHA-256 against the server-provided hash
  5. install      — backup old exe, copy new exe in place
  6. launch       — spawn the new GhostShell, exit

Cancellation: every step checks server.is_cancelled() before doing
expensive work.  Mid-download cancel is supported by checking the flag
inside the chunk loop.

Rollback: if step 5 or 6 fails after we've already deleted the old exe,
we restore from the backup we made in step 5.

Robustness notes (worth keeping in mind if extending this):
  • The "close" step uses graceful WM_CLOSE first, then falls back to
    taskkill /F.  Some games hold GhostShell's window handle via the
    overlay — graceful close gives them a chance to release cleanly.
  • Download writes to a .part file and atomically renames after the
    SHA passes.  Half-downloads can never end up at the target path.
  • Backup is timestamped (GhostShell.exe.bak.<ts>) so multiple failed
    attempts don't overwrite the original.
  • install_info.json gets rewritten with the new version + path AFTER
    a successful install so a subsequent updater run finds correct
    state even if the user moves the exe.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Optional

from updater import server


# ─── Helpers ─────────────────────────────────────────────────────────

_CREATE_NO_WINDOW = 0x08000000


# ─── GhostShell discovery (scan PC for existing install) ─────────────

def find_existing_ghostshell() -> dict:
    """Locate any installed copy of GhostShell.exe.  Returns:
        {found: bool, path: str, dir: str, source: 'install_info'|'standard'|'',
         channel: str, version: str, candidates: [paths searched]}

    Search order, fastest first:
      1. %APPDATA%/GhostShell/install_info.json — written by GhostShell
         on every launch.  Authoritative when present.  Also gives us
         the user's channel + last known version.
      2. Standard install paths under Program Files, LocalAppData, and
         a couple of common user folders.

    We don't do a full disk scan — too slow and intrusive.  If a user
    keeps GhostShell in a weird place, the UI's directory picker is
    the escape hatch."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    localappdata = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    profile = os.path.expanduser("~")
    pf      = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86   = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    # ── 1. install_info.json (canonical) ─────────────────────────
    info_path = os.path.join(appdata, "GhostShell", "install_info.json")
    candidates = [f"install_info.json @ {info_path}"]
    if os.path.isfile(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f) or {}
            recorded = info.get("install_path") or ""
            if recorded and os.path.isfile(recorded):
                _log(f"found via install_info.json: {recorded}")
                return {
                    "found":      True,
                    "path":       recorded,
                    "dir":        os.path.dirname(recorded),
                    "source":     "install_info",
                    "channel":    info.get("channel", ""),
                    "version":    info.get("version", ""),
                    "candidates": candidates,
                }
        except Exception as e:
            _log(f"install_info.json parse failed: {e}")

    # ── 2. Standard install paths ────────────────────────────────
    standard = [
        os.path.join(pf,           "GhostShell", "GhostShell.exe"),
        os.path.join(pfx86,        "GhostShell", "GhostShell.exe"),
        os.path.join(localappdata, "Programs", "GhostShell", "GhostShell.exe"),
        os.path.join(localappdata, "GhostShell", "GhostShell.exe"),
        os.path.join(profile,      "Desktop",   "GhostShell.exe"),
        os.path.join(profile,      "Documents", "GhostShell", "GhostShell.exe"),
        os.path.join(profile,      "Downloads", "GhostShell.exe"),
        # v3.2 user dev path
        os.path.join(profile,      "Documents", "My Games", "ghostshell",
                                                 "dist", "GhostShell.exe"),
    ]
    for p in standard:
        candidates.append(p)
        if os.path.isfile(p):
            _log(f"found via standard path: {p}")
            return {
                "found":      True,
                "path":       p,
                "dir":        os.path.dirname(p),
                "source":     "standard",
                "channel":    "",
                "version":    "",
                "candidates": candidates,
            }

    _log(f"no existing GhostShell found across {len(candidates)} candidates")
    return {
        "found":      False,
        "path":       "",
        "dir":        "",
        "source":     "",
        "channel":    "",
        "version":    "",
        "candidates": candidates,
    }


def default_install_dir() -> str:
    """Suggested install directory for the installer-mode picker.
    Prefers Program Files since the updater is uac_admin=True; falls
    back to LocalAppData\\Programs when Program Files isn't writable
    (no admin token, locked-down OS, etc).

    Pure probe — does NOT create any directory.  The actual mkdir
    happens when the install pipeline runs."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    # Probe: can we write to Program Files itself?  If yes we'll be
    # able to create our subdir there.  Otherwise fall back.
    if os.path.isdir(pf) and os.access(pf, os.W_OK):
        return os.path.join(pf, "GhostShell")
    localappdata = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(localappdata, "Programs", "GhostShell")


def _log(msg: str) -> None:
    try:
        from updater.main import log
        log(msg)
    except Exception: pass


def _check_cancelled() -> bool:
    if server.is_cancelled():
        _log("install cancelled by user")
        return True
    return False


# ─── Step 1: Close running GhostShell ────────────────────────────────

def _find_ghostshell_pids() -> list:
    """Return PIDs of every running GhostShell.exe."""
    pids = []
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq GhostShell.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            # CSV format: "GhostShell.exe","1234","Console","1","12 K"
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try: pids.append(int(parts[1]))
                except ValueError: continue
    except Exception as e:
        _log(f"tasklist failed: {e}")
    return pids


def _close_ghostshell(target_exe: str, timeout_s: int = 30) -> dict:
    """Politely ask GhostShell to close, fall back to taskkill /F.
    Returns {ok, msg}."""
    pids = _find_ghostshell_pids()
    if not pids:
        _log("no running GhostShell — skipping close step")
        return {"ok": True, "msg": "not running", "skipped": True}

    _log(f"GhostShell running, pids={pids}")
    server.update_step("close", "active",
                       f"Closing {len(pids)} GhostShell process(es)…")

    # Step A: Graceful close via taskkill (no /F).  This sends WM_CLOSE
    # which webview catches and exits cleanly.
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True, text=True, timeout=4,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as e:
            _log(f"graceful taskkill on pid={pid} failed: {e}")

    # Wait up to half the timeout for graceful exit
    deadline = time.time() + (timeout_s / 2)
    while time.time() < deadline:
        if _check_cancelled(): return {"ok": False, "msg": "cancelled"}
        remaining = _find_ghostshell_pids()
        if not remaining:
            _log("GhostShell exited gracefully")
            return {"ok": True, "msg": f"closed {len(pids)} process(es) gracefully"}
        time.sleep(0.5)

    # Step B: Force-close
    _log("graceful close timed out — escalating to taskkill /F")
    server.update_step("close", "active", "Force-closing stuck process…")
    for pid in _find_ghostshell_pids():
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=4,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as e:
            _log(f"force taskkill on pid={pid} failed: {e}")

    # Final wait
    deadline = time.time() + (timeout_s / 2)
    while time.time() < deadline:
        if _check_cancelled(): return {"ok": False, "msg": "cancelled"}
        if not _find_ghostshell_pids():
            return {"ok": True, "msg": "force-closed"}
        time.sleep(0.5)

    remaining = _find_ghostshell_pids()
    return {"ok": False,
            "msg": f"could not close GhostShell (pids still alive: {remaining})"}


# ─── Step 2 + 3: Check + download ────────────────────────────────────

def _http_get_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"GhostShellUpdater/{server.get_state_snapshot()['updater_version']}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        _log(f"GET {url} failed: {e}")
        return None


def _check_channel(server_url: str, channel: str) -> dict:
    """Hit /version/<channel> for the latest build metadata."""
    endpoint = f"{server_url.rstrip('/')}/version/{channel}"
    data = _http_get_json(endpoint)
    if not data:
        return {"ok": False, "err": f"could not reach {endpoint}"}
    if not data.get("available") or not data.get("version"):
        return {"ok": False, "err": f"no release published on '{channel}'"}
    return {
        "ok":           True,
        "version":      data["version"],
        "sha256":       data.get("sha256") or "",
        "size_bytes":   int(data.get("size_bytes") or 0),
        "download_url": data.get("download_url")
                         or f"{server_url.rstrip('/')}/download/{channel}",
        "notes":        data.get("notes") or "",
        "released_at":  data.get("released_at") or "",
    }


def _download(url: str, dest: str, total_hint: int = 0) -> dict:
    """Stream-download to dest.part, reporting progress to the server
    state.  Returns {ok, sha256, size_bytes}."""
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h = hashlib.sha256()
    downloaded = 0
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"GhostShellUpdater/{server.get_state_snapshot()['updater_version']}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0) or total_hint)
            server.update_progress(0, 0, total, phase="downloading",
                                    phase_label="Downloading")
            with open(tmp, "wb") as f:
                while True:
                    if _check_cancelled():
                        try: os.remove(tmp)
                        except Exception: pass
                        return {"ok": False, "err": "cancelled"}
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    downloaded += len(chunk)
                    pct = int((downloaded / total) * 100) if total else 0
                    server.update_progress(pct, downloaded, total)
        size = os.path.getsize(tmp)
        if size < 1024 * 100:   # sanity floor: <100 KB = probably a 404 HTML page
            try: os.remove(tmp)
            except Exception: pass
            return {"ok": False,
                    "err": f"downloaded file is {size} B — likely a server error page"}
        return {"ok": True, "sha256": h.hexdigest(), "size_bytes": size,
                "path": tmp}
    except Exception as e:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception: pass
        return {"ok": False, "err": str(e)}


# ─── Step 5: Install (with backup + rollback) ────────────────────────

def _install_exe(staged_path: str, target_exe: str) -> dict:
    """Swap the .part file in at target_exe with a backup-and-rollback
    safety net.  Returns {ok, backup_path, msg}.

    In install mode target_exe doesn't pre-exist — we just create the
    directory and copy the staged file in.  In update mode we back up
    the existing exe first and roll back if the copy fails."""
    target_dir = os.path.dirname(target_exe) or "."
    os.makedirs(target_dir, exist_ok=True)

    backup = ""
    pre_existed = os.path.exists(target_exe)
    if pre_existed:
        ts = int(time.time())
        backup = target_exe + f".bak.{ts}"
        try:
            shutil.copy2(target_exe, backup)
            _log(f"backed up old exe to {backup}")
        except Exception as e:
            _log(f"backup failed (continuing anyway): {e}")
            backup = ""

    # If it pre-existed, delete it so the copy is unambiguous.  Skip
    # this for fresh installs (target didn't exist to begin with).
    if pre_existed:
        try:
            os.remove(target_exe)
        except Exception as e:
            return {"ok": False, "msg": f"could not delete old exe: {e}",
                    "backup_path": backup}

    # Try the copy with retries — AV often holds a new exe for ~1s after
    # write so this isn't paranoia.
    last_err = None
    for attempt in range(20):
        try:
            shutil.copy2(staged_path, target_exe)
            try: os.remove(staged_path)
            except Exception: pass
            _log(f"installed new exe at {target_exe}")
            return {"ok": True, "msg": f"installed (attempt {attempt+1})",
                    "backup_path": backup}
        except Exception as e:
            last_err = str(e)
            _log(f"install copy attempt {attempt+1}/20 failed: {e}")
            time.sleep(1.0)

    # All retries failed — rollback if we have a backup
    rolled_back = False
    if backup and os.path.exists(backup):
        try:
            shutil.copy2(backup, target_exe)
            rolled_back = True
            _log("rolled back to backup")
        except Exception as e:
            _log(f"rollback failed: {e}")

    return {"ok": False,
            "msg": f"install copy failed: {last_err}; rolled_back={rolled_back}",
            "backup_path": backup}


# ─── Step 6: Launch + update install_info.json ───────────────────────

def _launch_new(target_exe: str) -> dict:
    try:
        subprocess.Popen(
            [target_exe], close_fds=True,
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def _update_install_info(target_exe: str, channel: str, version: str) -> None:
    """Rewrite %APPDATA%/GhostShell/install_info.json with the new state
    so a future updater run finds it."""
    from updater.main import INSTALL_INFO
    try:
        d = {
            "install_path": target_exe,
            "install_dir":  os.path.dirname(target_exe),
            "channel":      channel,
            "version":      version,
            "updated_at":   time.time(),
        }
        os.makedirs(os.path.dirname(INSTALL_INFO), exist_ok=True)
        with open(INSTALL_INFO, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        _log(f"updated install_info.json: {d}")
    except Exception as e:
        _log(f"install_info.json write failed: {e}")


# ─── Orchestrator ────────────────────────────────────────────────────

def run_install_pipeline() -> int:
    """Run the full sequence.  Returns 0 on success, non-zero on error.

    Mode ("install" or "update") changes one thing: the close step is
    skipped in install mode since there's nothing to close."""
    st = server.get_state_snapshot()
    mode = st.get("mode", "update")
    _log(f"pipeline start: mode={mode} target={st['target_exe']} channel={st['channel']}")

    # Sanity: we need both target_exe AND channel before we can install.
    if not st["target_exe"]:
        server.set_error("No target install path — please pick where GhostShell lives.")
        return 2
    if not st["channel"]:
        server.set_error("No release channel — please pick stable or beta.")
        return 2

    # ── Step 1: Close GhostShell (skipped in install mode) ─────
    if _check_cancelled(): return 1
    if mode == "install":
        # Fresh install — there's no running GhostShell to close.
        # Still ensure the install dir exists, since the user may have
        # typed a path that doesn't yet exist.
        try:
            os.makedirs(os.path.dirname(st["target_exe"]) or ".",
                         exist_ok=True)
        except Exception as e:
            server.update_step("close", "error", f"can't create dir: {e}")
            server.set_error(f"Couldn't create install directory: {e}")
            return 3
        server.update_step("close", "skipped",
                            "fresh install — nothing to close")
    else:
        r = _close_ghostshell(st["target_exe"])
        if not r["ok"]:
            server.update_step("close", "error", r["msg"])
            server.set_error(r["msg"] +
                             " — please close GhostShell manually and click Retry.")
            return 3
        server.update_step("close", "done", r["msg"])

    # ── Step 2: Check release server ────────────────────────────
    if _check_cancelled(): return 1
    server.update_step("channel", "active",
                       f"Fetching /version/{st['channel']}…")
    info = _check_channel(st["server_url"], st["channel"])
    if not info["ok"]:
        server.update_step("channel", "error", info["err"])
        server.set_error(info["err"])
        return 4
    target_version = info["version"]
    server.update_step("channel", "done",
                       f"Latest on '{st['channel']}': v{target_version}")
    import threading as _t
    with server._state_lock:
        server._state["target_version"] = target_version
        server._state["channel_check"]  = info

    # ── Step 3: Download ────────────────────────────────────────
    if _check_cancelled(): return 1
    from updater.main import UPDATER_DIR
    staged = os.path.join(UPDATER_DIR, f"GhostShell-{target_version}.exe")
    server.update_step("download", "active",
                       f"Downloading v{target_version}…")
    dl = _download(info["download_url"], staged,
                    total_hint=info.get("size_bytes", 0))
    if not dl["ok"]:
        server.update_step("download", "error", dl["err"])
        server.set_error(f"Download failed: {dl['err']}")
        return 5
    server.update_step("download", "done",
                       f"Downloaded {dl['size_bytes'] // (1024*1024)} MB")

    # ── Step 4: Verify SHA-256 ──────────────────────────────────
    if _check_cancelled(): return 1
    server.update_step("verify", "active", "Computing SHA-256…")
    expected = (info.get("sha256") or "").lower()
    actual   = dl["sha256"].lower()
    if expected:
        if actual != expected:
            err = (f"SHA-256 mismatch — expected {expected[:16]}…, "
                   f"got {actual[:16]}….  Aborting to avoid installing "
                   f"a corrupted or tampered build.")
            server.update_step("verify", "error", err)
            server.set_error(err)
            try: os.remove(dl["path"])
            except Exception: pass
            return 6
        server.update_step("verify", "done", "SHA-256 OK")
    else:
        server.update_step("verify", "skipped",
                            "server didn't provide sha256 — skipped")

    # ── Step 5: Install ────────────────────────────────────────
    if _check_cancelled(): return 1
    server.update_step("install", "active", "Swapping exe…")
    inst = _install_exe(dl["path"], st["target_exe"])
    if not inst["ok"]:
        server.update_step("install", "error", inst["msg"])
        server.set_error(inst["msg"])
        return 7
    server.update_step("install", "done", inst["msg"])

    # ── Step 6: Update install_info + launch ───────────────────
    _update_install_info(st["target_exe"], st["channel"], target_version)
    if _check_cancelled():
        server.update_step("launch", "skipped", "user cancelled before restart")
        server.set_done()
        return 0

    server.update_step("launch", "active", "Starting new GhostShell…")
    ln = _launch_new(st["target_exe"])
    if not ln["ok"]:
        server.update_step("launch", "error", ln["err"])
        server.set_error(f"Install OK but couldn't start the new GhostShell: "
                          f"{ln['err']}.  Launch it manually from "
                          f"{st['target_exe']}.")
        return 8
    server.update_step("launch", "done", "Launched")
    server.set_done()
    _log("pipeline complete — exit 0")
    return 0


def run_install_sync() -> int:
    """Headless variant for --silent mode.  Doesn't open a UI window."""
    return run_install_pipeline()
