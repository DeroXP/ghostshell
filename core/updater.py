"""GhostShell Auto-Updater (v3+) — checks the Railway-hosted release
server and self-updates.

v3 swap: the data source moved from GitHub releases to a self-hosted
Flask landing page at UPDATE_SERVER_URL.  Two channels:
  - stable  — confirmed-working builds (currently empty)
  - beta    — newest builds, where v3.x lives

Settings (controlled by the user via Settings → Updates):
  * channel        — "stable" | "beta"  (default "beta" until first stable ships)
  * server_url     — overrideable; defaults to UPDATE_SERVER_URL_DEFAULT
  * auto_check     — check on startup + every 6 hours (default ON)
  * auto_download  — download new build in background once detected (default ON)
  * auto_install   — apply silently and restart on download finish (default OFF)

State machine:
  idle → checking → up-to-date
                 → update-available  → downloading → ready-to-install
                                                  → install-failed
                                                  → installing  → restart

Server endpoint contract (matches ghostshell-site):
  GET /version/<channel>  →
    {
      "available":     bool,
      "version":       "3.0.0-beta.1",   # null if no release yet
      "channel":       "beta",
      "released_at":   "2026-05-10T14:16:00Z",
      "notes":         "...",
      "sha256":        "abc123...",
      "size_bytes":    44012345,
      "download_url":  "https://.../download/<channel>"
    }

The installer writes a small .bat that waits for the running GhostShell
process to exit, copies the new exe in place, restarts, then deletes itself.
"""
import json
import os
import sys
import time
import zipfile
import shutil
import subprocess
import threading
from typing import Optional

from config import (APP_VERSION, APPDATA_DIR,
                    APP_CHANNEL_DEFAULT, UPDATE_SERVER_URL_DEFAULT)
from core.utils import get_logger, atomic_write_json
import hashlib

log = get_logger("updater")

# v3 — release server moved off GitHub.  The legacy GitHub URLs are kept
# in comments for archeology; if someone wants to point a self-hosted
# fork at GitHub releases they can rewrite check_for_update().
#   GITHUB_REPO         = "DeroXP/ghostshell"
#   GITHUB_RELEASES_URL = "https://api.github.com/repos/.../releases?per_page=15"

UPDATE_DIR = os.path.join(APPDATA_DIR, "updates")
UPDATER_DIR = os.path.join(APPDATA_DIR, "updater")        # standalone updater lives here
UPDATER_EXE = os.path.join(UPDATER_DIR, "GhostShellUpdater.exe")
# v1.2.0 — sidecar that records which updater version we have on disk
# so the boot-time refresh check can compare against /version/updater
# without launching the binary.
UPDATER_VERSION_PATH = os.path.join(UPDATER_DIR, "updater_version.json")
INSTALL_INFO_PATH = os.path.join(APPDATA_DIR, "install_info.json")
SETTINGS_PATH = os.path.join(APPDATA_DIR, "updater_settings.json")
PERIODIC_CHECK_HOURS = 6
VALID_CHANNELS = ("stable", "beta")

os.makedirs(UPDATE_DIR,  exist_ok=True)
os.makedirs(UPDATER_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# install_info.json — written by GhostShell, read by the standalone updater
# ═══════════════════════════════════════════════════════════════════════════
def write_install_info() -> dict:
    """Persist where GhostShell is installed + which channel + which
    version it is, so the standalone updater can find + replace us.

    Called on every GhostShell startup so the file always tracks the
    current truth (user may have moved the exe between runs)."""
    try:
        info = {
            "install_path": sys.executable if getattr(sys, "frozen", False)
                              else os.path.abspath(sys.argv[0]),
            "install_dir":  os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                              else os.path.dirname(os.path.abspath(sys.argv[0])),
            "channel":      _load_settings().get("channel", APP_CHANNEL_DEFAULT),
            "version":      APP_VERSION,
            "updated_at":   time.time(),
        }
        atomic_write_json(INSTALL_INFO_PATH, info)
        return info
    except Exception as e:
        log.debug(f"install_info.json write failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Updater binary management — auto-fetch from Railway on first run
# ═══════════════════════════════════════════════════════════════════════════
_updater_install_state = {
    "running":    False,
    "downloaded": 0,
    "total":      0,
    "err":        None,
    "path":       None,
}
_updater_install_lock = threading.Lock()


def is_updater_installed() -> bool:
    """Updater binary present on disk?  Version freshness handled by
    `ensure_updater_up_to_date_async()` — this one just checks bytes."""
    return os.path.isfile(UPDATER_EXE) and os.path.getsize(UPDATER_EXE) > 100 * 1024


def _read_installed_updater_version() -> str:
    """Return the version string for the updater binary on disk, or ''
    when unknown (either not installed or downloaded by an older
    GhostShell build that didn't write the sidecar)."""
    if not os.path.isfile(UPDATER_VERSION_PATH):
        return ""
    try:
        with open(UPDATER_VERSION_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("version", "") or ""
    except Exception:
        return ""


def _write_installed_updater_version(version: str) -> None:
    """Record the version we just downloaded next to the binary so the
    next boot can compare against /version/updater without spawning it."""
    atomic_write_json(UPDATER_VERSION_PATH, {"version": version, "downloaded_at": time.time()})


def ensure_updater_installed_async() -> dict:
    """Kick off a background download of the updater if it's missing.
    No-op when already installed.  Callable from anywhere — main use
    is the boot-time check in app.py."""
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return {"ok": True, "running": True}
        if is_updater_installed():
            return {"ok": True, "already_installed": True}
        _updater_install_state.update({"running": True, "downloaded": 0,
                                         "total": 0, "err": None})
    threading.Thread(target=_download_updater_worker, daemon=True,
                     name="updater-bootstrap").start()
    return {"ok": True, "started": True}


def ensure_updater_up_to_date_async() -> dict:
    """v1.2.0 — beefier variant that ALSO refreshes the local updater
    binary when Railway is hosting a newer version.

    Boot flow:
      1. If the updater isn't installed → start a download (same as
         ensure_updater_installed_async).
      2. If installed → ping /version/updater and compare against the
         sidecar updater_version.json.  Remote newer → re-download in
         the background.  Pre-1.2.0 GhostShells didn't write the
         sidecar, so an empty local version triggers a one-time refresh
         on first boot of a 1.2.0+ build.

    Always non-blocking — the worker handles the actual network I/O on
    its own thread.  Returns immediately so the boot path doesn't stall
    on flaky internet."""
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return {"ok": True, "running": True}
    if not is_updater_installed():
        return ensure_updater_installed_async()
    # Already installed — kick off a non-blocking remote-version check
    threading.Thread(target=_updater_refresh_worker, daemon=True,
                     name="updater-refresh-check").start()
    return {"ok": True, "checking": True}


def _updater_refresh_worker() -> None:
    """Hit /version/updater + maybe re-download.  Best-effort — any
    failure is logged at debug level and ignored (we have a working
    updater binary on disk; staleness is a soft problem)."""
    try:
        s = _load_settings()
        base = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")
        url = base + "/version/updater"
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": f"GhostShell/{APP_VERSION}-updater-refresh"})
        with urllib.request.urlopen(req, timeout=15) as r:
            remote = json.loads(r.read())
    except Exception as e:
        log.debug(f"updater-refresh check failed (probably offline): {e}")
        return
    remote_ver = (remote or {}).get("version") or ""
    if not remote_ver or not (remote or {}).get("available"):
        return
    local_ver = _read_installed_updater_version()
    try:
        if local_ver:
            if _parse_version(local_ver) >= _parse_version(remote_ver):
                log.debug(f"updater is up to date (local={local_ver})")
                return
            log.info(f"Updater on disk is v{local_ver}, "
                     f"v{remote_ver} available — refreshing")
        else:
            log.info(f"Updater on disk has no version sidecar — "
                     f"refreshing to v{remote_ver}")
    except Exception as e:
        log.debug(f"updater-refresh comparison failed: {e}")
        return
    # Re-arm install state so the worker can run, then drive the same
    # download worker we use for first-install.
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return
        _updater_install_state.update({"running": True, "downloaded": 0,
                                         "total": 0, "err": None})
    # Pass the version string through so the worker writes the sidecar
    threading.Thread(target=_download_updater_worker,
                     args=(remote_ver,),
                     daemon=True, name="updater-refresh-dl").start()


def _download_updater_worker(remote_version: str = "") -> None:
    """Stream the standalone updater from Railway to %APPDATA%/GhostShell/
    updater/GhostShellUpdater.exe.  Hits /download/updater on whatever
    server_url the user is configured against.

    When `remote_version` is passed (refresh path), we also write the
    sidecar file recording what version we just put on disk so future
    boots can compare without re-downloading."""
    try:
        s = _load_settings()
        base = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")
        url = base + "/download/updater"
        log.info(f"Auto-installing updater from {url}")
        import urllib.request

        # Fetch the expected SHA-256 first so the downloaded binary can be
        # verified — same fail-closed posture as app-update downloads.  A
        # deploy landing between this fetch and the download shows up as a
        # mismatch → clean retry on the next boot.
        expected_sha = ""
        try:
            vreq = urllib.request.Request(base + "/version/updater", headers={
                "User-Agent": f"GhostShell/{APP_VERSION}-updater-bootstrap"})
            with urllib.request.urlopen(vreq, timeout=15) as vr:
                expected_sha = ((json.loads(vr.read()) or {}).get("sha256") or "").lower()
        except Exception as e:
            log.debug(f"updater version pre-fetch failed: {e}")
        if not expected_sha:
            raise RuntimeError("server did not provide a SHA-256 for the "
                               "updater — refusing to install an unverified binary")

        os.makedirs(UPDATER_DIR, exist_ok=True)
        tmp = UPDATER_EXE + ".part"
        req = urllib.request.Request(url, headers={
            "User-Agent": f"GhostShell/{APP_VERSION}-updater-bootstrap"})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0) or 0)
            with _updater_install_lock:
                _updater_install_state["total"] = total
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    downloaded += len(chunk)
                    with _updater_install_lock:
                        _updater_install_state["downloaded"] = downloaded
        sz = os.path.getsize(tmp)
        if sz < 1024 * 1024:    # <1 MB = clearly wrong
            os.remove(tmp)
            raise RuntimeError(
                f"updater download too small ({sz} B) — Railway might not "
                f"have the updater hosted yet, or returned an error page")
        if h.hexdigest().lower() != expected_sha:
            os.remove(tmp)
            raise RuntimeError(
                f"updater download failed SHA-256 check (expected "
                f"{expected_sha[:16]}…, got {h.hexdigest()[:16]}…) — refusing "
                f"to install; will retry next boot")
        os.replace(tmp, UPDATER_EXE)
        if remote_version:
            _write_installed_updater_version(remote_version)
        with _updater_install_lock:
            _updater_install_state["path"] = UPDATER_EXE
            _updater_install_state["running"] = False
        log.info(f"Updater installed at {UPDATER_EXE} ({sz} B)"
                 + (f" — v{remote_version}" if remote_version else ""))
    except Exception as e:
        # Clean up a partial .part so a network drop mid-download doesn't
        # leave stale bytes on disk for the next boot's retry to trip over.
        try:
            _part = UPDATER_EXE + ".part"
            if os.path.exists(_part):
                os.remove(_part)
        except OSError:
            pass
        with _updater_install_lock:
            _updater_install_state["err"] = str(e)
            _updater_install_state["running"] = False
        log.warning(f"Updater bootstrap failed: {e}")


def get_updater_install_status() -> dict:
    with _updater_install_lock:
        return dict(_updater_install_state)

# ═══════════════════════════════════════════════════════════════════════════
# Settings (persisted to APPDATA)
# ═══════════════════════════════════════════════════════════════════════════
_DEFAULT_SETTINGS = {
    "auto_check": True,
    "auto_download": True,
    "auto_install": False,        # OFF by default — needs explicit user opt-in
    "last_check_ts": 0.0,
    "last_seen_version": "",      # tracks "user already saw this version's notes"
    # v2.9.4 — post-update detection.
    "previous_version":         "",
    "pending_install_version":  "",
    # v3 — update channel + server.
    # "stable"   → confirmed-working builds.  Currently empty until a
    #              build graduates from beta.
    # "beta"     → newest builds, where v3.x lives.  Default for now.
    "channel":    APP_CHANNEL_DEFAULT,
    "server_url": UPDATE_SERVER_URL_DEFAULT,
}


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {**_DEFAULT_SETTINGS, **(data if isinstance(data, dict) else {})}
        # One-time migration: the Railway service was renamed and the old
        # ghostshell-site domain is dead.  A saved server_url pointing at it
        # would strand this install's update checks forever, so rewrite it
        # to the new default and persist the fix.
        from config import UPDATE_SERVER_URL_LEGACY
        saved_url = (merged.get("server_url") or "")
        if "ghostshell-site.up.railway.app" in saved_url:
            merged["server_url"] = UPDATE_SERVER_URL_DEFAULT
            log.info(f"Migrated saved update-server URL {saved_url!r} → "
                     f"{UPDATE_SERVER_URL_DEFAULT!r} (old Railway domain is dead)")
            try:
                atomic_write_json(SETTINGS_PATH, merged)
            except Exception:
                pass    # in-memory value is migrated either way
        return merged
    except Exception as e:
        log.warning(f"Could not load updater settings, using defaults: {e}")
        return dict(_DEFAULT_SETTINGS)


def _save_settings(settings: dict):
    atomic_write_json(SETTINGS_PATH, settings)


def get_settings() -> dict:
    return _load_settings()


def set_settings(auto_check: Optional[bool] = None,
                 auto_download: Optional[bool] = None,
                 auto_install: Optional[bool] = None,
                 channel: Optional[str] = None,
                 server_url: Optional[str] = None) -> dict:
    s = _load_settings()
    if auto_check    is not None: s["auto_check"]    = bool(auto_check)
    if auto_download is not None: s["auto_download"] = bool(auto_download)
    if auto_install  is not None: s["auto_install"]  = bool(auto_install)
    if channel       is not None:
        if channel not in VALID_CHANNELS:
            return {"ok": False,
                    "err": f"channel must be one of {VALID_CHANNELS}, got {channel!r}"}
        s["channel"] = channel
    if server_url    is not None:
        url = (server_url or "").strip().rstrip("/")
        if url.startswith(("http://", "https://")):
            s["server_url"] = url
        else:
            return {"ok": False, "err": "server_url must start with http:// or https://"}
    _save_settings(s)
    log.info(f"Updater settings saved: check={s['auto_check']} "
             f"download={s['auto_download']} install={s['auto_install']} "
             f"channel={s.get('channel', APP_CHANNEL_DEFAULT)} "
             f"server={s.get('server_url', UPDATE_SERVER_URL_DEFAULT)}")
    return {"ok": True, "settings": s}


# v3 — channel filtering is done server-side now.  The Railway endpoint
# serves /version/<channel>, which only returns the release published on
# that channel.  No client-side filter needed.  Function kept as a stub
# to avoid breaking any imports, but always returns True.
def _release_passes_channel_filter(release: dict, channel: str) -> bool:
    return True


# ═══════════════════════════════════════════════════════════════════════════
# In-memory state — read by /api/updater/status
# ═══════════════════════════════════════════════════════════════════════════
_update_state = {
    "checking": False,
    "downloading": False,
    "installing": False,
    "progress": 0,                   # 0-100
    "latest_version": None,
    "latest_url": None,
    "latest_notes": None,
    "latest_sha256": None,           # v3 — sha256 from server, verified post-download
    "latest_size": 0,                # v3 — bytes
    "update_available": False,
    "download_complete": False,
    "pending_install_path": None,    # path to the downloaded .exe / .zip ready to apply
    "error": None,
}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
def _parse_version(v: str) -> tuple:
    """Parse a version string into a comparable 5-tuple.

    Supported forms:
        '2.9.8'              → (2, 9, 8, 0, 0)
        '2.9.8.0'            → (2, 9, 8, 0, 0)   — equal to 2.9.8
        '2.9.8.1'            → (2, 9, 8, 1, 0)   — newer than 2.9.8.0 ✓
        '3.0.0'              → (3, 0, 0, 0, 0)
        '3.0.0-beta.1'       → (3, 0, 0, 0, -101)   — sorts BEFORE 3.0.0
        '3.0.0-beta.5'       → (3, 0, 0, 0, -105)   — newer than -beta.1
        '3.0.0-rc.1'         → (3, 0, 0, 0, -11)
        'v3.0.0-beta.1'      → (3, 0, 0, 0, -101)   (strips leading v)

    The 5th component (prerelease ordinal) is NEGATIVE for prereleases
    so the natural tuple comparison correctly sorts '3.0.0-beta.1' as
    older than '3.0.0' and as older than '3.0.0-beta.5'.
    """
    v = (v or "").strip().lstrip("vV")
    if not v:
        return (0, 0, 0, 0, 0)
    # Split into core + prerelease tag
    if "-" in v:
        core, pre = v.split("-", 1)
    else:
        core, pre = v, ""
    parts = []
    for p in core.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    parts = parts[:4]
    # Prerelease ordinal — alpha < beta < rc < final.  The final release
    # gets pre_ord=0; any prerelease is negative.
    pre_ord = 0
    if pre:
        # base + n  — higher N within a category sorts NEWER.
        # Bands are 1000 wide so no realistic prerelease counter (rc.999
        # etc.) can overflow into a higher category's band.  Ordering:
        # alpha (-3000) < beta (-2000) < rc (-1000) < final (0).
        # alpha.5 = -2995 < beta.1 = -1999 < beta.99 = -1901 < rc.1 = -999 < final = 0.
        kind_map = {"alpha": -3000, "beta": -2000, "rc": -1000}
        pre_parts = pre.split(".")
        base = kind_map.get(pre_parts[0].lower(), -500)
        n = 0
        if len(pre_parts) > 1:
            try: n = int(pre_parts[1])
            except ValueError: n = 0
        pre_ord = base + n
    return tuple(parts + [pre_ord])


def _ps_download(url: str, dest: str, timeout_s: int = 300) -> bool:
    """Download via PowerShell — no extra Python deps."""
    cmd = (
        '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; '
        f'Invoke-WebRequest -Uri "{url}" -OutFile "{dest}" -UseBasicParsing'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.error("  Download timed out")
        return False
    except Exception as e:
        log.error(f"  Download error: {e}")
        return False
    if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True
    log.error(f"  Download failed (rc={r.returncode}): {r.stderr[:200]}")
    return False


def _ps_get_json(url: str, timeout_s: int = 30) -> Optional[dict]:
    """GET → JSON via PowerShell."""
    cmd = (
        '[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; '
        f'(Invoke-WebRequest -Uri "{url}" -UseBasicParsing -Headers @{{"User-Agent"="GhostShell-Updater"}}).Content'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.warning("  Update server request timed out")
        return None
    except Exception as e:
        log.warning(f"  Update server request error: {e}")
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Public API: check / download / install
# ═══════════════════════════════════════════════════════════════════════════
def check_for_update() -> dict:
    """Hit the Railway release server and update _update_state with the
    result.  v3 swap from GitHub — the server's /version/<channel>
    endpoint returns one JSON object per channel containing the latest
    version + sha256 + download URL.

    Returns the standard {ok, update_available, current, latest, ...}
    payload that the UI consumes.
    """
    if _update_state["checking"]:
        return {"ok": False, "err": "Already checking"}

    log.info("Checking for updates...")
    _update_state["checking"] = True
    _update_state["error"] = None
    # v3 — clear cached "latest_*" up front.  Otherwise switching channel
    # from beta→stable (where stable currently has no release) would
    # leave the stale beta latest_url / sha256 in state, and a racing
    # download_update() call could trigger an unintended re-download.
    _update_state["latest_version"]   = None
    _update_state["latest_url"]       = None
    _update_state["latest_sha256"]    = None
    _update_state["latest_size"]      = 0
    _update_state["update_available"] = False

    try:
        s = _load_settings()
        channel    = s.get("channel",    APP_CHANNEL_DEFAULT)
        server_url = s.get("server_url", UPDATE_SERVER_URL_DEFAULT).rstrip("/")
        endpoint   = f"{server_url}/version/{channel}"

        data = _ps_get_json(endpoint)
        if data is None or not isinstance(data, dict):
            _update_state["error"] = f"Could not reach update server ({server_url})"
            log.warning(f"  update check failed against {endpoint}")
            return {"ok": False, "err": _update_state["error"]}

        available    = bool(data.get("available"))
        latest_ver_s = data.get("version")
        notes        = (data.get("notes") or "")[:1500]
        sha256       = data.get("sha256") or ""
        download_url = data.get("download_url") or ""
        size_bytes   = int(data.get("size_bytes") or 0)

        if not available or not latest_ver_s:
            log.info(f"  No release published on '{channel}' channel")
            _update_state["update_available"] = False
            _update_state["latest_version"]   = None
            _update_state["latest_url"]       = None
            _update_state["latest_notes"]     = notes
            _update_state["latest_sha256"]    = None
            _update_state["latest_size"]      = 0
            # Persist last-check ts regardless of result
            s["last_check_ts"] = time.time()
            _save_settings(s)
            return {
                "ok": True, "update_available": False,
                "current": APP_VERSION, "latest": None,
                "channel": channel, "server_url": server_url,
                "message": f"No release published on the {channel} channel yet.",
            }

        latest_ver  = _parse_version(latest_ver_s)
        current_ver = _parse_version(APP_VERSION)
        is_newer    = latest_ver > current_ver

        _update_state["latest_version"]   = latest_ver_s.lstrip("vV")
        _update_state["latest_notes"]     = notes
        _update_state["latest_url"]       = download_url
        _update_state["latest_sha256"]    = sha256
        _update_state["latest_size"]      = size_bytes
        _update_state["update_available"] = is_newer

        # Persist last-check timestamp
        s["last_check_ts"] = time.time()
        _save_settings(s)

        if is_newer:
            log.info(f"  Update available on '{channel}': "
                     f"v{APP_VERSION} -> v{latest_ver_s}")
            return {
                "ok": True, "update_available": True,
                "current": APP_VERSION, "latest": latest_ver_s,
                "notes": notes,
                "download_url": download_url,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "channel": channel, "server_url": server_url,
            }
        log.info(f"  Already up to date on '{channel}' (v{APP_VERSION})")
        return {
            "ok": True, "update_available": False,
            "current": APP_VERSION, "latest": latest_ver_s,
            "channel": channel, "server_url": server_url,
        }
    finally:
        _update_state["checking"] = False


def _prune_old_cached_installers(keep: str = "") -> int:
    """Delete every GhostShell_*/Vispora_*.exe / .zip in UPDATE_DIR except
    `keep` (a bare filename).  Returns bytes freed.  Best-effort; swallows
    per-file errors (a file might be the one currently being read).

    3.4.3 — stops the updates cache from accumulating one ~44 MB
    installer per version.  The standalone updater does its own
    download for the actual install, so these cached copies are only
    used for the in-app 'already downloaded' fast-path — we never need
    more than the current target.

    Checks both the legacy "ghostshell_" prefix and the current
    "vispora_" prefix (post exe-rename) so any installer cached before
    the rename still gets cleaned up instead of sitting there forever."""
    freed = 0
    try:
        if not os.path.isdir(UPDATE_DIR):
            return 0
        for name in os.listdir(UPDATE_DIR):
            low = name.lower()
            if not (low.startswith("ghostshell_") or low.startswith("vispora_")):
                continue
            if not (low.endswith(".exe") or low.endswith(".zip")
                    or low.endswith(".part")):
                continue
            if name == keep:
                continue
            p = os.path.join(UPDATE_DIR, name)
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                freed += sz
            except OSError:
                continue
        if freed:
            log.info(f"Pruned old cached installers — freed "
                     f"{freed / (1024*1024):.0f} MB from {UPDATE_DIR}")
    except Exception as e:
        log.debug(f"installer prune failed: {e}")
    return freed


def download_update() -> dict:
    """Download the latest release asset to UPDATE_DIR.

    Returns ok=True with `path` once the download is in place; sets
    `download_complete=True` and `pending_install_path` in state.
    Idempotent — if the file already exists with non-zero size, skips
    the network round-trip.
    """
    if not _update_state["update_available"] or not _update_state["latest_url"]:
        return {"ok": False, "err": "No update to download"}
    if _update_state["downloading"]:
        return {"ok": False, "err": "Download already in progress"}

    url = _update_state["latest_url"]
    version = _update_state["latest_version"] or "latest"
    # beta.11 — flipped the default from .zip to .exe.  The Railway
    # release server has always shipped .exe payloads; the old code
    # only saved as .exe when the URL literally ended in `.exe`, which
    # broke on `/download/beta` style URLs and routed downloads to
    # _apply_zip_update() (a no-op on frozen builds).  install_update_now
    # also now sniffs the MZ header as a fallback, but defaulting to
    # .exe avoids the bad name in the first place.
    ul = url.lower()
    if ul.endswith(".zip"):
        ext = ".zip"
    else:
        ext = ".exe"
    dest = os.path.join(UPDATE_DIR, f"Vispora_{version}{ext}")

    # 3.4.3 — prune stale cached installers before caching the new one.
    # Each downloaded build is ~44 MB and the old code never removed
    # superseded copies — they only got swept by the integrity scan's
    # 7-day age sweep, so a burst of updates could pile up 500 MB+ in
    # %APPDATA%/GhostShell/updates.  Now we keep ONLY the version we're
    # about to cache; everything else (the actual install already
    # happened, and the standalone updater re-downloads on demand) is
    # dead weight.  Best-effort — never blocks the download.
    _prune_old_cached_installers(keep=os.path.basename(dest))

    # Already downloaded?
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        log.info(f"  Update already downloaded: {dest}")
        _update_state["download_complete"] = True
        _update_state["pending_install_path"] = dest
        _update_state["progress"] = 100
        return {"ok": True, "path": dest, "cached": True}

    _update_state["downloading"] = True
    _update_state["progress"] = 0
    _update_state["download_complete"] = False
    log.info(f"Downloading {url} -> {dest}")
    try:
        ok = _ps_download(url, dest)
    finally:
        _update_state["downloading"] = False

    if not ok:
        _update_state["progress"] = 0
        _update_state["error"] = "Download failed"
        return {"ok": False, "err": "Download failed"}

    # v3 — verify SHA-256 against the server-provided hash.  Belt-and-
    # suspenders: catches CDN corruption + MITM rewriting + truncated
    # downloads.  Fail CLOSED if the server didn't supply one — a missing
    # hash is indistinguishable from a MITM/misconfigured server stripping
    # it, and installing an unverified binary is exactly what this check
    # exists to prevent.  (The live release server always includes
    # sha256; there is no legacy-server case left to accommodate.)
    expected_sha = _update_state.get("latest_sha256") or ""
    if not expected_sha:
        err = ("Server did not provide a SHA-256 hash for this build — "
               "refusing to install an unverified binary.")
        log.warning(err)
        _update_state["error"] = err
        try: os.remove(dest)
        except OSError: pass
        return {"ok": False, "err": err}
    try:
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual_sha = h.hexdigest()
    except Exception as e:
        _update_state["error"] = f"Hash check failed: {e}"
        try: os.remove(dest)
        except OSError: pass
        return {"ok": False, "err": _update_state["error"]}
    if actual_sha.lower() != expected_sha.lower():
        err = (f"Downloaded file failed SHA-256 check — "
               f"expected {expected_sha[:16]}…, got {actual_sha[:16]}…. "
               "File may be corrupted; download will retry on next check.")
        log.warning(err)
        _update_state["error"] = err
        try: os.remove(dest)
        except OSError: pass
        return {"ok": False, "err": err}
    log.info(f"  SHA-256 verified")

    _update_state["progress"] = 100
    _update_state["download_complete"] = True
    _update_state["pending_install_path"] = dest
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    log.info(f"  Downloaded {size_mb:.1f} MB — ready to install")
    return {"ok": True, "path": dest, "size_mb": round(size_mb, 1)}


def install_update_now() -> dict:
    """Spawn the swap-and-restart batch, then exit the app.

    NOTE: the app process must die for the batch to overwrite the .exe.
    We schedule `os._exit(0)` 800ms after launching the batch so this
    response can be returned to the UI first.
    """
    path = _update_state.get("pending_install_path")
    if not path or not os.path.exists(path):
        # Try downloading on-demand if user clicked Install before download finished
        dl = download_update()
        if not dl.get("ok"):
            return {"ok": False, "err": "No update downloaded yet"}
        path = _update_state["pending_install_path"]

    # beta.11 — sniff the MZ magic header instead of trusting the file
    # name.  Historically download_update() picked the extension from
    # the URL suffix; a server URL without an explicit `.exe` caused
    # the payload (a real Windows .exe) to be saved as .zip and then
    # routed here to _apply_zip_update which no-ops on frozen builds,
    # leaving the user clicking Install with no observable effect.
    # Looking at the first two bytes is fast and reliable for any
    # Windows PE.
    if _looks_like_exe(path):
        return _apply_exe_update_async(path)
    if path.lower().endswith(".zip"):
        return _apply_zip_update(path)
    return {"ok": False, "err": "Unrecognised update format"}


def _looks_like_exe(path: str) -> bool:
    """Return True if `path` starts with the `MZ` magic that begins
    every Windows PE/COFF executable.  Used to detect misnamed
    downloads (see install_update_now)."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Internal: apply an .exe update by spawning a swap script
# ═══════════════════════════════════════════════════════════════════════════
def _apply_exe_update_async(new_exe: str) -> dict:
    """Hand off to the standalone GhostShellUpdater app.

    v3.2.2 rearchitecture: instead of bundling an updater binary
    INSIDE GhostShell.exe and shelling out to it, GhostShell now
    auto-downloads a separate full-blown updater app to
    %APPDATA%/GhostShell/updater/GhostShellUpdater.exe (Flask + pywebview
    UI, ~30 MB).  At install time we just spawn that updater with the
    target_dir + channel + version pre-populated via CLI args and
    install_info.json, then exit.  The updater does everything from
    there — including downloading the actual new build itself, so
    GhostShell never needs to touch the .exe payload at all.

    Why this is structurally better:
      • The updater has its own UI window so the user can see progress
        even after GhostShell exits.
      • The updater is self-contained — it can update GhostShell from
        anywhere, including portable installs, and across moves.
      • install_info.json travels with %APPDATA%, so the updater always
        knows where the current GhostShell lives.
      • New GhostShell builds don't ship a stale bundled updater —
        the updater updates ITSELF independently.

    The `new_exe` param is now ignored (kept for back-compat) — the
    standalone updater does its own download.
    """
    log.info("Handing off to standalone GhostShell Updater...")

    if not getattr(sys, "frozen", False):
        # Running from source — no in-place swap possible
        log.info("  Running from source — skipping in-place swap")
        return {
            "ok": True,
            "msg": f"Update available. Build a new exe to apply.",
        }

    # Make sure the standalone updater is installed.  Normally it's
    # already there from the boot-time bootstrap, but if the user
    # manually triggered an install before that finished, do it now
    # (blocking — only takes a few seconds).
    if not is_updater_installed():
        log.info("  Updater not installed — fetching from Railway now")
        ensure_updater_installed_async()
        # Wait up to 60s for the download to complete
        deadline = time.time() + 60
        while time.time() < deadline:
            st = get_updater_install_status()
            if not st["running"]:
                break
            time.sleep(0.5)
        if not is_updater_installed():
            err = get_updater_install_status().get("err") or "unknown"
            return {"ok": False,
                    "err": f"Standalone updater unavailable: {err}.  "
                           f"Try restarting GhostShell with internet "
                           f"available."}

    # Refresh install_info.json so the updater has the latest current
    # state (path, channel, version)
    info = write_install_info()

    # Record the version transition for the post-install toast
    s = _load_settings()
    s["previous_version"]        = APP_VERSION
    s["pending_install_version"] = _update_state.get("latest_version") or ""
    _save_settings(s)

    pid = os.getpid()
    current_exe = sys.executable
    channel    = s.get("channel", APP_CHANNEL_DEFAULT)
    server_url = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")

    args = [
        UPDATER_EXE,
        "--target-exe",       current_exe,
        "--target-dir",       os.path.dirname(current_exe),
        "--channel",          channel,
        "--current-version",  APP_VERSION,
        "--target-version",   _update_state.get("latest_version") or "",
        "--server",           server_url,
        "--auto-confirm",
    ]
    log.info(f"  Spawning: {' '.join(args)}")

    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        # NOTE: we do NOT set CREATE_NO_WINDOW here — the updater is a
        # GUI app and the user needs to see its window.  DETACHED_PROCESS
        # is what we care about; that ensures the updater outlives us.
        subprocess.Popen(
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        log.error(f"  Could not spawn standalone updater: {e}")
        return {"ok": False, "err": f"Could not spawn updater: {e}"}

    log.info("  Standalone updater launched. GhostShell exiting...")
    _update_state["installing"] = True

    # Give the HTTP response a moment to flush, then die so the updater
    # can swap our exe without a file lock.
    def _suicide():
        time.sleep(0.8)
        log.info("Exiting for update install...")
        os._exit(0)
    threading.Thread(target=_suicide, daemon=True).start()

    return {"ok": True,
            "msg": "GhostShell Updater launching — this window will close and "
                   "the updater will handle the rest.",
            "restart_required": True,
            "method": "standalone-updater"}


def _apply_exe_update_via_bat(new_exe: str, current_exe: str) -> dict:
    """Legacy fallback: write update.bat and spawn it.  Kept for builds
    that don't yet have GhostShellUpdater.exe bundled — ie. the user is
    updating FROM an old build TO a new one, but the old build's bundle
    doesn't include the helper.  Logic is identical to the pre-v3.2.1
    path."""
    s = _load_settings()
    s["previous_version"]        = APP_VERSION
    s["pending_install_version"] = _update_state.get("latest_version") or ""
    _save_settings(s)

    pid = os.getpid()
    bat_path    = os.path.join(UPDATE_DIR, "update.bat")
    status_path = os.path.join(UPDATE_DIR, "last_install.status")

    bat = (
        '@echo off\r\n'
        'setlocal\r\n'
        'echo === GhostShell Update ===\r\n'
        f'echo Waiting for PID {pid} to exit...\r\n'
        'powershell -NoProfile -Command '
        f'"Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue" >nul 2>nul\r\n'
        'set wait=0\r\n'
        ':wait_loop\r\n'
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        'if errorlevel 1 goto pid_gone\r\n'
        'set /a wait+=1\r\n'
        'if %wait% gtr 60 goto pid_gone\r\n'
        'timeout /t 1 /nobreak >nul\r\n'
        'goto wait_loop\r\n'
        ':pid_gone\r\n'
        'set retries=20\r\n'
        ':try_copy\r\n'
        f'copy /y "{new_exe}" "{current_exe}" >nul 2>nul\r\n'
        'if %errorlevel% == 0 goto copy_ok\r\n'
        'set /a retries-=1\r\n'
        'if %retries% gtr 0 (\r\n'
        '  timeout /t 1 /nobreak >nul\r\n'
        '  goto try_copy\r\n'
        ')\r\n'
        f'echo FAIL: copy failed after 20 retries > "{status_path}"\r\n'
        'echo *** Update copy failed — opening existing GhostShell. ***\r\n'
        'goto launch\r\n'
        ':copy_ok\r\n'
        f'echo OK > "{status_path}"\r\n'
        'echo Update applied successfully.\r\n'
        ':launch\r\n'
        f'start "" "{current_exe}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(bat_path, "w", encoding="ascii", errors="replace", newline="") as f:
        f.write(bat)

    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        log.error(f"  Could not spawn updater script: {e}")
        return {"ok": False, "err": f"Could not spawn updater: {e}"}

    log.info(f"  Updater script launched (legacy .bat path).")
    _update_state["installing"] = True
    def _suicide():
        time.sleep(0.8)
        log.info("Exiting for update install...")
        os._exit(0)
    threading.Thread(target=_suicide, daemon=True).start()
    return {"ok": True,
            "msg": "Update installing — GhostShell will restart in a moment.",
            "restart_required": True,
            "method": "legacy-bat"}


def _apply_zip_update(zip_path: str) -> dict:
    """Source .zip update — extracts to dev source dir.  No-op when frozen."""
    log.info("Applying .zip update...")
    if getattr(sys, "frozen", False):
        return {
            "ok": True,
            "msg": f"Source update saved to {zip_path}. Run from source to apply.",
            "path": zip_path,
        }

    app_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    extract_dir = os.path.join(UPDATE_DIR, "extracted")
    try:
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # Find app.py in the extracted tree
        source_dir = extract_dir
        for cur, dirs, files in os.walk(extract_dir):
            if "app.py" in files:
                source_dir = cur
                break

        if not os.path.exists(os.path.join(source_dir, "app.py")):
            return {"ok": False, "err": "Invalid update — app.py not found"}

        updated = 0
        for cur, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", ".git", ".github", "node_modules", "build", "dist", "dist_new")]
            rel = os.path.relpath(cur, source_dir)
            target = os.path.join(app_dir, rel) if rel != "." else app_dir
            os.makedirs(target, exist_ok=True)
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                try:
                    shutil.copy2(os.path.join(cur, f), os.path.join(target, f))
                    updated += 1
                except Exception:
                    pass

        log.info(f"  {updated} files updated. Restart to apply.")
        return {"ok": True, "msg": f"Updated {updated} files. Restart to use the new version.",
                "files_updated": updated, "restart_required": True}
    except zipfile.BadZipFile:
        return {"ok": False, "err": "Corrupted zip"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Status (consumed by the UI)
# ═══════════════════════════════════════════════════════════════════════════
def get_update_status() -> dict:
    s = _load_settings()
    # v3 — include the safety state so the UI can show:
    #   "Update v3.0.0-beta.2 ready — will auto-install when you stop
    #   gaming (currently in foreground: cs2.exe)"
    safety = is_safe_to_install()
    return {
        "current_version":   APP_VERSION,
        "latest_version":    _update_state["latest_version"],
        "update_available":  _update_state["update_available"],
        "checking":          _update_state["checking"],
        "downloading":       _update_state["downloading"],
        "installing":        _update_state["installing"],
        "download_complete": _update_state["download_complete"],
        "progress":          _update_state["progress"],
        "error":             _update_state["error"],
        "notes":             _update_state["latest_notes"],
        # v3 — surface sha256 + size for the settings UI to display
        "latest_sha256":     _update_state.get("latest_sha256"),
        "latest_size":       _update_state.get("latest_size", 0),
        "settings":          s,
        # Channel options for the UI's dropdown — kept here so the front
        # end doesn't have to hardcode the list.
        "valid_channels":    list(VALID_CHANNELS),
        "default_server_url": UPDATE_SERVER_URL_DEFAULT,
        # v3 — install-safety state.  `install_blocked_reason` is empty
        # when auto-install would fire right now.
        "install_safe":              safety["safe"],
        "install_blocked_reason":    "" if safety["safe"] else safety["reason"],
        "user_idle_threshold_sec":   USER_IDLE_THRESHOLD_SEC,
    }


# ═══════════════════════════════════════════════════════════════════════════
# v3 — Safety gating: don't auto-install while user is busy
# ═══════════════════════════════════════════════════════════════════════════
#
# Rules (all must pass for auto-install to fire):
#   • No game in foreground         — would yank the user out mid-match
#   • GhostShell window not focused — user is actively configuring
#   • User input idle for >= 5 min  — keyboard / mouse untouched
#
# Manual install (the user clicking "Install now") bypasses this gate —
# it's an explicit consent action.

USER_IDLE_THRESHOLD_SEC = 5 * 60      # 5 minutes of no input
SAFETY_GATE_RECHECK_SEC = 120          # try again every 2 min when deferred


def _get_user_idle_sec() -> float:
    """Return seconds since last keyboard / mouse activity.  Uses
    user32.GetLastInputInfo — fast (microseconds) and reliable on
    Windows.  Returns 0.0 if we can't read it for any reason (defaults
    to "user IS active" — we'd rather defer than install at the wrong
    time)."""
    try:
        import ctypes
        from ctypes import wintypes
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD)]
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _is_game_foreground() -> bool:
    """True if the foreground window belongs to a known game exe.
    Returns False if we can't determine it (fail-OPEN here — if we
    can't tell, we shouldn't block the update forever)."""
    try:
        from core.gamepad_mapper import _get_foreground_exe_name
        exe = _get_foreground_exe_name()
        if not exe:
            return False
        from core.game_profiles import KNOWN_GAME_EXES
        return exe.lower() in {e.lower() for e in KNOWN_GAME_EXES}
    except Exception:
        return False


def _is_ghostshell_foreground() -> bool:
    """True if our own window (GhostShell or Vispora, pre/post rename) is
    in the foreground."""
    try:
        from core.gamepad_mapper import _get_foreground_exe_name
        exe = (_get_foreground_exe_name() or "").lower()
        return exe in ("ghostshell.exe", "vispora.exe")
    except Exception:
        return False


def is_safe_to_install() -> dict:
    """Return {safe: bool, reason: str} indicating whether auto-install
    can fire right now without disrupting the user.  Caller logs the
    reason on defer so the in-app log shows why an update is waiting."""
    if _is_game_foreground():
        return {"safe": False, "reason": "game in foreground"}
    if _is_ghostshell_foreground():
        return {"safe": False, "reason": "GhostShell window in foreground"}
    idle = _get_user_idle_sec()
    if idle < USER_IDLE_THRESHOLD_SEC:
        return {"safe": False,
                "reason": f"user active recently ({int(idle)}s < {USER_IDLE_THRESHOLD_SEC}s threshold)"}
    return {"safe": True, "reason": f"system idle ({int(idle)}s)"}


# ═══════════════════════════════════════════════════════════════════════════
# Background tasks: startup check, periodic check, opportunistic auto-download
# ═══════════════════════════════════════════════════════════════════════════
def _maybe_auto_download():
    """If settings allow, download right after detection.  Gating on the
    install step happens via the safety check in _try_auto_install."""
    s = _load_settings()
    if not s.get("auto_download", True):
        return
    if not _update_state["update_available"] or _update_state["download_complete"]:
        return
    log.info("Auto-downloading update in background...")
    download_update()
    if s.get("auto_install", False):
        _try_auto_install()


def _try_auto_install(quiet: bool = False) -> dict:
    """Auto-install the staged update IF it's safe to do so right now.
    Otherwise returns {ok: False, deferred: True, reason: str} and the
    install will be retried on the next periodic check (or by the
    dedicated safety-gate recheck loop)."""
    if not _update_state.get("download_complete"):
        return {"ok": False, "err": "no download to install"}
    s = _load_settings()
    if not s.get("auto_install", False):
        return {"ok": False, "err": "auto_install setting is off"}
    safety = is_safe_to_install()
    if not safety["safe"]:
        if not quiet:
            log.info(f"Auto-install deferred: {safety['reason']}")
        return {"ok": False, "deferred": True, "reason": safety["reason"]}
    log.info(f"Auto-install proceeding: {safety['reason']}")
    return install_update_now()


def _check_and_maybe_download():
    """One iteration of the check / download cycle."""
    try:
        check_for_update()
        _maybe_auto_download()
    except Exception as e:
        log.warning(f"Update cycle failed: {e}")


def _periodic_loop():
    """Re-check every PERIODIC_CHECK_HOURS hours while the app runs."""
    interval_s = PERIODIC_CHECK_HOURS * 3600
    while True:
        time.sleep(interval_s)
        s = _load_settings()
        if not s.get("auto_check", True):
            continue
        log.info(f"Periodic update check ({PERIODIC_CHECK_HOURS}h)...")
        _check_and_maybe_download()


def _safety_gate_loop():
    """v3 — short-period recheck.  If we have a downloaded update waiting
    and auto-install is on but we previously deferred for safety, retry
    every SAFETY_GATE_RECHECK_SEC.  Exits the loop once an install
    actually fires (process is about to be replaced anyway)."""
    while True:
        time.sleep(SAFETY_GATE_RECHECK_SEC)
        try:
            s = _load_settings()
            if not s.get("auto_install", False):
                continue
            if not _update_state.get("download_complete"):
                continue
            if _update_state.get("installing"):
                return
            r = _try_auto_install(quiet=True)
            if r.get("ok"):
                return    # install fired — process will exit shortly
        except Exception as e:
            log.debug(f"safety gate loop tick failed: {e}")


def check_update_background():
    """Called from app.py on startup.

    1. Fires the initial check + opportunistic download in a thread.
    2. Spawns a long-lived periodic-check thread.
    3. v3 — spawns the safety-gate recheck loop so a downloaded-but-
       deferred update auto-applies the moment the user goes idle.
    """
    s = _load_settings()
    if s.get("auto_check", True):
        threading.Thread(target=_check_and_maybe_download, daemon=True).start()
    threading.Thread(target=_periodic_loop,     daemon=True, name="updater-periodic").start()
    threading.Thread(target=_safety_gate_loop,  daemon=True, name="updater-safety-gate").start()


# v2.9.4 — post-install detection.
# Called from app.py once on startup.  Compares APP_VERSION against the
# version recorded right before the last "Install & Restart" click.
# Returns:
#   {'state': 'updated'|'install_failed'|'idle', 'previous': '...',
#    'current': APP_VERSION, 'expected': '...'}
def check_post_install_state() -> dict:
    """Detect whether we just finished an in-place update.

    Reads the `previous_version` / `pending_install_version` set by
    `_apply_exe_update_async`, and the install.status file written by
    update.bat, to figure out whether the swap succeeded.

    Always clears the pending fields after reading so we don't double-
    fire the notification on subsequent launches.
    """
    s = _load_settings()
    prev    = s.get("previous_version", "")
    pending = s.get("pending_install_version", "")

    if not prev and not pending:
        return {"state": "idle", "current": APP_VERSION}

    # Read batch's status file if present
    status_path = os.path.join(UPDATE_DIR, "last_install.status")
    bat_status = None
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8", errors="replace") as f:
                bat_status = f.read().strip()
        except Exception:
            pass

    # Decide outcome
    if APP_VERSION != prev:
        # The exe being executed reports a different version than what we
        # were running before the install — the swap landed.
        outcome = {
            "state":    "updated",
            "previous": prev,
            "current":  APP_VERSION,
            "expected": pending,
            "bat":      bat_status or "OK",
        }
        log.info(f"Post-install: updated from v{prev} -> v{APP_VERSION}")
    else:
        # Same version still running — either the swap was rejected (file
        # locked, AV) or the user double-launched the old exe.
        outcome = {
            "state":    "install_failed",
            "previous": prev,
            "current":  APP_VERSION,
            "expected": pending,
            "bat":      bat_status or "no status file",
        }
        log.warning(f"Post-install: still on v{APP_VERSION}, "
                    f"expected v{pending} (bat status: {bat_status})")

    # Clear flags so we don't repeat ourselves next launch
    s["previous_version"]        = ""
    s["pending_install_version"] = ""
    _save_settings(s)

    # Best-effort cleanup of the status file
    try:
        if os.path.exists(status_path):
            os.remove(status_path)
    except Exception:
        pass

    return outcome
