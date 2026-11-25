"""Vispora Auto-Updater (v3+): checks where releases are published and
self-updates.

v3 swap: the data source moved from GitHub releases to a self-hosted
Flask landing page at UPDATE_SERVER_URL.  Two channels:
  - stable  — confirmed-working builds (currently empty)
  - beta    — newest builds, where v3.x lives

3.8.0: GitHub releases are back as a second place, and the first one asked.
The update server is the fallback (see update_sources.py for the order and
for how a release on GitHub is read).  Everything after the check is the
same whichever place answered: the download is checked against the SHA-256
that place gave, staged, and installed by the standalone updater.

Settings (controlled by the user via Settings → Updates):
  * channel        — "stable" | "beta"  (default "beta" until first stable ships)
  * update_source  : "auto" (GitHub, then the update server) | "github" | "server"
  * server_url     — overrideable; defaults to UPDATE_SERVER_URL_DEFAULT
  * auto_check     — check on startup + every 6 hours (default ON)
  * auto_download  — download new build in background once detected (default ON)
  * auto_install   — apply silently and restart on download finish (default OFF)

State machine:
  idle → checking → up-to-date
                 → update-available  → downloading → ready-to-install
                                                  → install-failed
                                                  → installing  → restart

Server endpoint contract (matches the release-site repo):
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

The installer writes a small .bat that waits for the running Vispora
process to exit, copies the new exe in place, restarts, then deletes itself.
"""
import json
import os
import sys
import time
import zipfile
import shutil
import subprocess
import re
import threading
import urllib.request
from typing import Optional

from config import (APP_NAME, LEGACY_APP_NAME, APP_VERSION, APPDATA_DIR,
                    APP_CHANNEL_DEFAULT, UPDATE_SERVER_URL_DEFAULT,
                    UPDATER_EXE_NAME, LEGACY_UPDATER_EXE_NAME,
                    OWN_PROCESS_NAMES, is_dead_update_server)
from core.utils import get_logger, atomic_write_json
import hashlib
import update_sources

log = get_logger("updater")

UPDATE_DIR = os.path.join(APPDATA_DIR, "updates")
UPDATER_DIR = os.path.join(APPDATA_DIR, "updater")        # standalone updater lives here
UPDATER_EXE = os.path.join(UPDATER_DIR, UPDATER_EXE_NAME)
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
# Which exe is the app
# ═══════════════════════════════════════════════════════════════════════════
# An update swaps the app's exe and starts it again.  Started as it always
# was, the app's exe is this process's own, and nothing is set here.  Behind
# the shell (shell_rs/) this process is the app's backend, unpacked
# somewhere by the shell, and the exe a person has is the shell: the shell
# says where it is when it starts the backend (app.py, _serve_for_the_shell),
# and that is the exe an update is for.  Swapping the backend's own exe
# would change a file the shell unpacks again anyway, and leave the app as
# it was.  No release starts the app that way yet.
_app_exe = None


def use_app_exe(path) -> None:
    """The exe an update swaps from now on; None for this process's own."""
    global _app_exe
    _app_exe = path or None


def _the_apps_exe() -> str:
    """The exe an update swaps, and starts again afterwards."""
    return _app_exe or sys.executable


# ═══════════════════════════════════════════════════════════════════════════
# install_info.json — written by the app, read by the standalone updater
# ═══════════════════════════════════════════════════════════════════════════
def write_install_info() -> dict:
    """Persist where Vispora is installed + which channel + which
    version it is, so the standalone updater can find + replace us.

    Called on every Vispora startup so the file always tracks the
    current truth (user may have moved the exe between runs)."""
    try:
        # (behind the shell: the shell's exe, see _the_apps_exe)
        installed = _app_exe or (sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0]))
        info = {
            "install_path": installed,
            "install_dir":  os.path.dirname(installed),
            "channel":      _load_settings().get("channel", APP_CHANNEL_DEFAULT),
            "version":      APP_VERSION,
            "updated_at":   time.time(),
            # Where this install gets its updates, for an updater that is
            # started by hand (one the app starts is told on its command line).
            "update_source": update_sources.source_setting(_load_settings().get("update_source")),
        }
        atomic_write_json(INSTALL_INFO_PATH, info)
        return info
    except Exception as e:
        log.debug(f"install_info.json write failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Updater binary management: auto-fetch on first run (GitHub, else the update server)
# ═══════════════════════════════════════════════════════════════════════════
_updater_install_state = {
    "running":    False,
    "downloaded": 0,
    "total":      0,
    "err":        None,
    "path":       None,
    "failures":   0,        # downloads failed in a row (automatic retries back off)
    "failed_at":  0.0,
}
_updater_install_lock = threading.Lock()


def _adopt_legacy_updater() -> None:
    """An updater downloaded before the rename sits in UPDATER_DIR under its
    old filename.  Take it over under the new one rather than downloading
    the same ~19 MB again, and never leave the old file behind as an orphan.
    Best-effort — if the file is busy, the next boot tries again."""
    legacy = os.path.join(UPDATER_DIR, LEGACY_UPDATER_EXE_NAME)
    try:
        if not os.path.isfile(legacy):
            return
        if os.path.exists(UPDATER_EXE):
            os.remove(legacy)
        else:
            os.rename(legacy, UPDATER_EXE)
    except OSError as e:
        log.debug(f"could not adopt pre-rename updater binary: {e}")


def is_updater_installed() -> bool:
    """Updater binary present on disk?  Version freshness handled by
    `ensure_updater_up_to_date_async()` — this one just checks bytes."""
    return os.path.isfile(UPDATER_EXE) and os.path.getsize(UPDATER_EXE) > 100 * 1024


def _read_installed_updater_version() -> str:
    """Return the version string for the updater binary on disk, or ''
    when unknown (either not installed or downloaded by an older
    build that didn't write the sidecar)."""
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


# ── Where releases come from: GitHub first, the update server second ──────
_github_answer = {"at": 0.0, "releases": None}
_github_lock = threading.Lock()
GITHUB_ANSWER_KEEP_S = 300      # the app's check and the updater's ask the same thing a moment apart


def _forget_github_answer() -> None:
    with _github_lock:
        _github_answer.update(at=0.0, releases=None)


def _github_releases(fresh: bool = False) -> Optional[list]:
    """GitHub's list of this app's releases, or None when GitHub did not
    answer (offline, its hourly limit for this address, an answer that is not
    a list).  A good answer is kept for five minutes unless `fresh`: without
    a sign-in GitHub allows 60 requests an hour per address."""
    now = time.time()
    with _github_lock:
        kept = _github_answer["releases"]
        if not fresh and kept is not None and 0 <= now - _github_answer["at"] < GITHUB_ANSWER_KEEP_S:
            return kept
    data = _http_get_json(update_sources.GITHUB_RELEASES_URL, timeout_s=15,
                          headers=update_sources.GITHUB_HEADERS, want=list)
    if data is None:
        return None
    with _github_lock:
        _github_answer.update(at=now, releases=data)
    return data


def _places_text(places) -> str:
    """"GitHub or the update server", for a sentence about who was asked."""
    return " or ".join(update_sources.SOURCE_NAMES[x] for x in places) or "nobody"


def _updater_offers(s: Optional[dict] = None, agent: str = "updater-bootstrap", fresh: bool = False):
    """The standalone updater as each place offers it, one place at a time in
    the order of update_sources.source_order(): dicts {"version", "sha256",
    "download_url", "source"} ("version" may be "" on a server that does not
    say).  A place that did not answer, has no updater, or gives no SHA-256
    to check a download against yields nothing.  Lazy: the next place is only
    asked when the caller goes on.  `fresh`: do not use a kept GitHub answer."""
    s = s if s is not None else _load_settings()
    channel = s.get("channel", APP_CHANNEL_DEFAULT)
    base = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")
    for source in update_sources.source_order(s.get("update_source"), base):
        if source == "github":
            releases = _github_releases(fresh=fresh)
            offer = update_sources.github_updater(releases, channel) if releases is not None else None
            if offer and offer["available"]:
                yield {"version": offer["version"], "sha256": offer["sha256"],
                       "download_url": offer["download_url"], "source": "github"}
            continue
        try:
            req = urllib.request.Request(base + "/version/updater", headers={
                "User-Agent": f"{APP_NAME}/{APP_VERSION}-{agent}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                meta = json.loads(r.read())
        except Exception as e:
            log.debug(f"updater version check against the update server failed (probably offline): {e}")
            continue
        if not isinstance(meta, dict) or meta.get("available") is False:
            continue                # the server says it has none
        sha = str(meta.get("sha256") or "").strip().lower()
        if not sha:
            continue                # nothing to check a download against
        yield {"version": str(meta.get("version") or ""), "sha256": sha,
               "download_url": base + "/download/updater", "source": "server"}


def _updater_offer(s: Optional[dict] = None, agent: str = "updater-bootstrap") -> Optional[dict]:
    """The standalone updater as the first place that has one offers it (see
    _updater_offers), or None."""
    return next(_updater_offers(s, agent), None)


def _updater_knows_sources() -> bool:
    """Can the updater on disk install from GitHub (1.5.0 on)?  One with no
    recorded version is taken for an older one."""
    v = _read_installed_updater_version()
    return (bool(v) and is_updater_installed()
            and _parse_version(v) >= _parse_version(update_sources.UPDATER_MIN_FOR_SOURCES))


def _refresh_updater_now(timeout_s: float = 90.0) -> bool:
    """Get an updater that can install from GitHub and wait for it (install
    time, when the one on disk is too old for the update at hand).  True when
    the updater on disk can do that afterwards.

    Only an updater that is new enough is downloaded: fetching the old one
    again changes nothing, and the automatic install would do it every two
    minutes.  When no place has one, that counts as a failed try, so the
    automatic install backs off (see _updater_retry_wait)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline and get_updater_install_status()["running"]:
        time.sleep(0.5)             # a download started at boot: let it finish first
    if _updater_knows_sources():
        return True
    need = _parse_version(update_sources.UPDATER_MIN_FOR_SOURCES)
    offer = None
    try:
        offer = next((o for o in _updater_offers(agent="updater-refresh", fresh=True)
                      if o["version"] and _parse_version(o["version"]) >= need), None)
    except Exception as e:
        log.debug(f"could not look for a newer updater: {e}")
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return False            # somebody else's download is still going: not now
        if offer is None:
            _updater_install_state["err"] = "no updater that can install from GitHub is published yet"
            _updater_install_state["failures"] += 1
            _updater_install_state["failed_at"] = time.time()
            return False
        _updater_install_state.update({"running": True, "downloaded": 0, "total": 0, "err": None})
    _download_updater_worker(offer["version"], offer)
    return _updater_knows_sources()


def ensure_updater_installed_async() -> dict:
    """Kick off a background download of the updater if it's missing.
    No-op when already installed.  Callable from anywhere — main use
    is the boot-time check in app.py."""
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return {"ok": True, "running": True}
        _adopt_legacy_updater()
        if is_updater_installed():
            return {"ok": True, "already_installed": True}
        _updater_install_state.update({"running": True, "downloaded": 0,
                                         "total": 0, "err": None})
    threading.Thread(target=_download_updater_worker, daemon=True,
                     name="updater-bootstrap").start()
    return {"ok": True, "started": True}


def ensure_updater_up_to_date_async() -> dict:
    """v1.2.0 — beefier variant that ALSO refreshes the local updater
    binary when GitHub or the update server has a newer version.

    Boot flow:
      1. If the updater isn't installed → start a download (same as
         ensure_updater_installed_async).
      2. If installed → ping /version/updater and compare against the
         sidecar updater_version.json.  Remote newer → re-download in
         the background.  Pre-1.2.0 builds didn't write the
         sidecar, so an empty local version triggers a one-time refresh
         on first boot of a 1.2.0+ build.

    Always non-blocking — the worker handles the actual network I/O on
    its own thread.  Returns immediately so the boot path doesn't stall
    on flaky internet."""
    # beta.11 — local update mode has no server to fetch/refresh the
    # standalone updater from, and doesn't need it (installs go through the
    # self-contained bat swap).  Skip quietly.
    try:
        if (_load_settings().get("local_update_dir") or "").strip():
            log.info("Updater self-refresh skipped (local update mode)")
            return {"ok": True, "skipped": "local update mode"}
    except Exception:
        pass
    with _updater_install_lock:
        if _updater_install_state["running"]:
            return {"ok": True, "running": True}
        _adopt_legacy_updater()
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
        offer = _updater_offer(agent="updater-refresh")
    except Exception as e:
        log.debug(f"updater-refresh check failed (probably offline): {e}")
        return
    remote_ver = (offer or {}).get("version") or ""
    if not remote_ver:
        return
    local_ver = _read_installed_updater_version()
    try:
        if local_ver:
            if _parse_version(local_ver) >= _parse_version(remote_ver):
                log.debug(f"updater is up to date (local={local_ver})")
                return
            log.info(f"Updater on disk is v{local_ver}, "
                     f"v{remote_ver} available; refreshing")
        else:
            log.info(f"Updater on disk has no version sidecar; "
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
    # Pass the version string through so the worker writes the sidecar, and
    # the offer so it downloads exactly what was compared.
    threading.Thread(target=_download_updater_worker,
                     args=(remote_ver, offer),
                     daemon=True, name="updater-refresh-dl").start()


def _fetch_updater(offer: dict) -> int:
    """Stream the updater `offer` describes to UPDATER_EXE and return its
    size.  Raises when the download fails, is too small to be the updater, or
    does not match the offer's SHA-256; nothing is put in place then."""
    url = offer["download_url"]
    expected_sha = str(offer["sha256"]).lower()
    if not _is_http_url(url):
        raise RuntimeError(f"refusing to download the updater from a non-HTTP address: {url!r}")
    log.info(f"Auto-installing updater from {url}")
    os.makedirs(UPDATER_DIR, exist_ok=True)
    tmp = UPDATER_EXE + ".part"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"{APP_NAME}/{APP_VERSION}-updater-bootstrap"})
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
            raise RuntimeError(
                f"updater download too small ({sz} B): the place it came from "
                f"might not have the updater yet, or returned an error page")
        if h.hexdigest().lower() != expected_sha:
            raise RuntimeError(
                f"updater download failed SHA-256 check (expected "
                f"{expected_sha[:16]}…, got {h.hexdigest()[:16]}…), so refusing "
                f"to install; will retry next boot")
        os.replace(tmp, UPDATER_EXE)
        return sz
    finally:
        # Whatever went wrong, no partial file stays behind for the next
        # try to trip over.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _download_updater_worker(remote_version: str = "", offer: Optional[dict] = None) -> None:
    """Put the standalone updater in UPDATER_EXE from the first place that
    has it: GitHub, or /download/updater on whatever server_url the user is
    configured against (see _updater_offers).  When a place offers it but the
    download from there fails, the next place is tried.

    The expected SHA-256 comes with the offer, before the download, so the
    binary can be verified: same fail-closed posture as app-update downloads.
    The version that hash belongs to is recorded next to the binary so future
    boots can compare without re-downloading.  `offer`: what a refresh check
    found, tried first so the same thing is downloaded that was compared
    (`remote_version` is its version, for an offer that does not say)."""
    try:
        on_disk = _read_installed_updater_version() if is_updater_installed() else ""
        s = _load_settings()
        base = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")
        asked = update_sources.source_order(s.get("update_source"), base)
        tried, last_err = [], None

        def candidates():
            if offer:
                yield offer
            yield from _updater_offers(s)

        for cand in candidates():
            if not cand.get("sha256") or cand["source"] in tried:
                continue
            version = str(cand.get("version") or "") or (remote_version if cand is offer else "")
            if (cand is not offer and on_disk and version
                    and _parse_version(version) <= _parse_version(on_disk)):
                continue            # a fallback must not put an older updater over the one on disk
            tried.append(cand["source"])
            try:
                sz = _fetch_updater(cand)
            except Exception as e:
                last_err = e
                log.warning(f"Updater download from {update_sources.SOURCE_NAMES[cand['source']]} failed: {e}")
                continue
            if version:
                _write_installed_updater_version(version)
            with _updater_install_lock:
                _updater_install_state["path"] = UPDATER_EXE
                _updater_install_state["running"] = False
                _updater_install_state["failures"] = 0
            log.info(f"Updater installed at {UPDATER_EXE} ({sz} B)"
                     + (f", v{version}" if version else ""))
            return
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"{_places_text(asked)} gave no updater with a SHA-256; "
                           "refusing to install an unverified binary")
    except Exception as e:
        with _updater_install_lock:
            _updater_install_state["err"] = str(e)
            _updater_install_state["running"] = False
            _updater_install_state["failures"] += 1
            _updater_install_state["failed_at"] = time.time()
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
    # 3.8.0, where updates come from: "auto" asks GitHub first and the
    # update server second (the other way round when server_url is somebody's
    # own server); "github" / "server" ask that one only.
    "update_source": "auto",
    # beta.11 — LOCAL UPDATE MODE.  When set to an existing folder containing
    # version.json + Vispora.exe, the updater checks THAT folder instead of the
    # HTTP release server: version compare, sha-256, staging, and the headless
    # install gating all work exactly the same — just sourced from disk.  Lets
    # updates keep flowing with the Railway server stopped.  Empty = server mode.
    "local_update_dir": "",
}


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {**_DEFAULT_SETTINGS, **(data if isinstance(data, dict) else {})}
        # One-time migration: the Railway service was renamed and its old
        # domain is dead.  A saved server_url pointing at it would strand
        # this install's update checks forever, so rewrite it to the new
        # default and persist the fix.
        saved_url = (merged.get("server_url") or "")
        if is_dead_update_server(saved_url):
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
                 server_url: Optional[str] = None,
                 local_update_dir: Optional[str] = None,
                 update_source: Optional[str] = None) -> dict:
    s = _load_settings()
    source_before = (s.get("channel"), s.get("server_url"), s.get("local_update_dir"),
                     update_sources.source_setting(s.get("update_source")))
    if update_source is not None:
        if update_source not in update_sources.SOURCE_SETTINGS:
            return {"ok": False,
                    "err": f"update_source must be one of {update_sources.SOURCE_SETTINGS}, got {update_source!r}"}
        s["update_source"] = update_source
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
    if local_update_dir is not None:
        d = (local_update_dir or "").strip()
        if d and not os.path.isdir(d):
            return {"ok": False,
                    "err": f"local_update_dir does not exist or is not a folder: {d}"}
        s["local_update_dir"] = d   # empty string = back to server mode
    _save_settings(s)
    if (s.get("channel"), s.get("server_url"), s.get("local_update_dir"),
            update_sources.source_setting(s.get("update_source"))) != source_before:
        # What we know (and may have staged) came from the previous channel /
        # server.  Installing it now could silently downgrade the app, so drop
        # it and let the next check ask the new source.
        _update_state["update_available"] = False
        _update_state["latest_version"] = None
        _update_state["latest_url"] = None
        _update_state["latest_sha256"] = None
        _update_state["latest_source"] = None
        _forget_staged_download()
        _forget_github_answer()
    log.info(f"Updater settings saved: check={s['auto_check']} "
             f"download={s['auto_download']} install={s['auto_install']} "
             f"channel={s.get('channel', APP_CHANNEL_DEFAULT)} "
             f"source={update_sources.source_setting(s.get('update_source'))} "
             f"server={s.get('server_url', UPDATE_SERVER_URL_DEFAULT)} "
             f"local_dir={s.get('local_update_dir') or '(server mode)'}")
    return {"ok": True, "settings": s}


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
    "latest_source": None,           # where the last check found it: "github" | "server" | "local"
    "update_available": False,
    "download_complete": False,
    "pending_install_path": None,    # path to the downloaded .exe / .zip ready to apply
    # What that file is: {"version", "source"}.  A later check that fails
    # clears latest_*, but the file is still the build it was.
    "staged": None,
    "install_after_game": False,     # the user asked to install while a game ran: do it when it closes
    "error": None,
}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
# The same reading of a version as the standalone updater uses: it lives in
# update_sources.py, which both sides import.
_parse_version = update_sources.parse_version


# Both helpers used to shell out to PowerShell's Invoke-WebRequest with the URL
# interpolated into the command line.  The URL is not ours to trust — it comes
# from user-editable settings and from the server's own JSON — and PowerShell
# expands $(...) inside a double-quoted string, so a crafted server_url or
# download_url ran arbitrary commands in this elevated process.  urllib has no
# shell in the loop (and is what the updater bootstrap above already uses).
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,39}")


def _is_http_url(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _http_download(url: str, dest: str, timeout_s: int = 60) -> bool:
    """Stream `url` to `dest` via a .part file, updating the progress state."""
    if not _is_http_url(url):
        log.error(f"  Refusing to download from non-HTTP URL: {url!r}")
        return False
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-Updater"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    _update_state["progress"] = min(99, done * 100 // total)
        if os.path.getsize(tmp) == 0:
            raise OSError("server sent an empty file")
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log.error(f"  Download failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _http_get_json(url: str, timeout_s: int = 30, headers: Optional[dict] = None, want: type = dict):
    """GET → parsed JSON of the kind `want` (an object unless told
    otherwise), or None on any failure.  `headers` are sent on top of ours."""
    if not _is_http_url(url):
        log.warning(f"  Refusing non-HTTP update server URL: {url!r}")
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-Updater", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning(f"  Update request failed ({url.split('?', 1)[0]}): {e}")
        return None
    return data if isinstance(data, want) else None


def _ask_github(channel: str) -> Optional[dict]:
    """What GitHub offers `channel` (see update_sources.github_app_release),
    or None when GitHub did not answer."""
    releases = _github_releases(fresh=True)
    if releases is None:
        return None
    return update_sources.github_app_release(releases, channel)


def _ask_server(server_url: str, channel: str) -> Optional[dict]:
    """What the update server's /version/<channel> offers, in the same shape
    as _ask_github, or None when it did not answer."""
    data = _http_get_json(f"{server_url}/version/{channel}")
    if data is None:
        return None
    version = data.get("version")
    try:
        size_bytes = int(data.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    return {"available": bool(data.get("available")) and bool(version),
            "version": str(version or ""), "sha256": str(data.get("sha256") or ""),
            "size_bytes": size_bytes, "download_url": str(data.get("download_url") or ""),
            "notes": str(data.get("notes") or ""), "source": "server"}


def _stamp_last_check() -> None:
    """Record when the last check finished.  The settings are read again
    first: a check takes a while, and a setting the user changed meanwhile
    must not be written over with what it was when the check began."""
    s = _load_settings()
    s["last_check_ts"] = time.time()
    _save_settings(s)


def _where_from(s: dict) -> tuple:
    """The settings that decide where updates come from."""
    return (s.get("channel", APP_CHANNEL_DEFAULT), s.get("server_url", UPDATE_SERVER_URL_DEFAULT),
            (s.get("local_update_dir") or "").strip(), update_sources.source_setting(s.get("update_source")))


def _could_not_reach(places, server_url: str) -> str:
    """The error line for places that did not answer."""
    if list(places) == ["server"]:
        return f"Could not reach update server ({server_url})"
    if list(places) == ["github"]:
        return "Could not reach GitHub"
    return f"Could not reach GitHub or the update server ({server_url})"


def _forget_staged_download() -> None:
    _update_state["download_complete"] = False
    _update_state["pending_install_path"] = None
    _update_state["staged"] = None
    _update_state["progress"] = 0


def _keep_staged_download_only_for(version: Optional[str]) -> None:
    """Forget a staged installer unless it is the build `version` (None =
    forget whatever is staged).  A check that finds a different latest build
    — or none — must not leave an older download marked ready, or the
    auto-installer goes on to install something that is no longer current."""
    staged = os.path.basename(_update_state.get("pending_install_path") or "")
    if not version or os.path.splitext(staged)[0] != f"{APP_NAME}_{version}":
        _forget_staged_download()


# ═══════════════════════════════════════════════════════════════════════════
# Public API: check / download / install
# ═══════════════════════════════════════════════════════════════════════════
def _check_local_update(local_dir: str, s: dict) -> dict:
    """beta.11 — LOCAL update source: read version.json + Vispora.exe from a
    folder on disk instead of the HTTP release server.

    The folder layout is exactly what the Railway repo already uses
    (releases/<channel>/version.json + Vispora.exe), so pointing this at that
    working copy means new builds keep flowing with the server stopped.  The
    sha256/size the server used to compute per-request are computed here from
    the exe itself; everything downstream (staged copy, hash re-verify,
    install gating, bat swap) is unchanged."""
    vjson_path = os.path.join(local_dir, "version.json")
    exe_path   = os.path.join(local_dir, "Vispora.exe")
    if not os.path.isfile(vjson_path) or not os.path.isfile(exe_path):
        err = (f"Local update folder is missing version.json or Vispora.exe: "
               f"{local_dir}")
        _update_state["error"] = err
        log.warning(f"  {err}")
        return {"ok": False, "err": err, "source": "local"}
    try:
        with open(vjson_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        latest_ver_s = str(data.get("version") or "").strip()
        notes        = (data.get("notes") or "")[:1500]
        if not latest_ver_s:
            raise ValueError("version.json has no 'version' field")
        h = hashlib.sha256()
        with open(exe_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha256     = h.hexdigest()
        size_bytes = os.path.getsize(exe_path)
    except Exception as e:
        err = f"Could not read local update folder ({local_dir}): {e}"
        _update_state["error"] = err
        log.warning(f"  {err}")
        return {"ok": False, "err": err, "source": "local"}

    latest_ver  = _parse_version(latest_ver_s)
    current_ver = _parse_version(APP_VERSION)
    is_newer    = latest_ver > current_ver

    _update_state["latest_version"]   = latest_ver_s.lstrip("vV")
    _update_state["latest_notes"]     = notes
    _update_state["latest_url"]       = "local:" + exe_path
    _update_state["latest_source"]    = "local"
    _update_state["latest_sha256"]    = sha256
    _update_state["latest_size"]      = size_bytes
    _update_state["update_available"] = is_newer
    _keep_staged_download_only_for(_update_state["latest_version"] if is_newer else None)

    _stamp_last_check()

    if is_newer:
        log.info(f"  Local update available: v{APP_VERSION} -> v{latest_ver_s} "
                 f"({local_dir})")
    else:
        log.info(f"  Already up to date vs local folder (v{APP_VERSION})")
    return {
        "ok": True, "update_available": is_newer,
        "current": APP_VERSION, "latest": latest_ver_s,
        "notes": notes if is_newer else "",
        "sha256": sha256, "size_bytes": size_bytes,
        "channel": s.get("channel", APP_CHANNEL_DEFAULT),
        "server_url": "local:" + local_dir,
        "source": "local",
    }


def check_for_update() -> dict:
    """Hit the release source and update _update_state with the result.

    beta.11 — when settings.local_update_dir is set, the "release source" is
    that folder on disk (see _check_local_update).  Otherwise GitHub's
    releases are asked first and the update server's /version/<channel>
    second (update_sources.source_order has the exceptions).

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
    _update_state["latest_source"]    = None
    _update_state["update_available"] = False

    try:
        s = _load_settings()

        # beta.11 — local mode takes over the whole check when configured.
        local_dir = (s.get("local_update_dir") or "").strip()
        if local_dir:
            return _check_local_update(local_dir, s)

        channel    = s.get("channel",    APP_CHANNEL_DEFAULT)
        server_url = s.get("server_url", UPDATE_SERVER_URL_DEFAULT).rstrip("/")
        order      = update_sources.source_order(s.get("update_source"), server_url)

        # Ask each place in turn.  The first that has a release for this
        # channel is the answer.  One that answers "nothing published" is
        # remembered while the next is asked; one that does not answer at all
        # (offline, GitHub's hourly limit) is passed over.
        info, silent = None, []
        for source in order:
            answer = _ask_github(channel) if source == "github" else _ask_server(server_url, channel)
            if answer is None:
                silent.append(source)
                continue
            if info is None or answer["available"]:
                info = answer
            if info["available"]:
                break
        if silent:
            log.info("  no answer from " + " or ".join(update_sources.SOURCE_NAMES[x] for x in silent)
                     + (f"; using {update_sources.SOURCE_NAMES[info['source']]}" if info else ""))

        if _where_from(_load_settings()) != _where_from(s):
            # The channel, the source or the server was changed while the
            # places were being asked.  What came back answers the old
            # question: drop it and let the next check ask the new one.
            log.info("  update settings changed during the check; result dropped")
            return {"ok": False, "err": "The update settings changed during the check. Check again."}

        if info is None or (silent and not info["available"]):
            # Nobody answered, or the only answer was "nothing here" while
            # another place could not be reached: that is not "up to date",
            # and what is staged is still the newest we know of.
            _update_state["error"] = _could_not_reach(silent, server_url)
            log.warning(f"  update check failed: {_update_state['error']}")
            return {"ok": False, "err": _update_state["error"]}

        source       = info["source"]
        latest_ver_s = info.get("version") or ""
        notes        = (info.get("notes") or "")[:1500]
        sha256       = info.get("sha256") or ""
        download_url = info.get("download_url") or ""
        size_bytes   = int(info.get("size_bytes") or 0)

        if not info["available"] or not latest_ver_s:
            log.info(f"  No release published on '{channel}' channel")
            _keep_staged_download_only_for(None)
            _update_state["update_available"] = False
            _update_state["latest_version"]   = None
            _update_state["latest_url"]       = None
            _update_state["latest_notes"]     = notes
            _update_state["latest_sha256"]    = None
            _update_state["latest_size"]      = 0
            # Persist last-check ts regardless of result
            _stamp_last_check()
            return {
                "ok": True, "update_available": False,
                "current": APP_VERSION, "latest": None,
                "channel": channel, "server_url": server_url, "source": source,
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
        _update_state["latest_source"]    = source
        _update_state["update_available"] = is_newer
        _keep_staged_download_only_for(_update_state["latest_version"] if is_newer else None)

        # Persist last-check timestamp
        _stamp_last_check()

        where = update_sources.SOURCE_NAMES[source]
        if is_newer:
            log.info(f"  Update available on '{channel}' from {where}: "
                     f"v{APP_VERSION} -> v{latest_ver_s}")
            return {
                "ok": True, "update_available": True,
                "current": APP_VERSION, "latest": latest_ver_s,
                "notes": notes,
                "download_url": download_url,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "channel": channel, "server_url": server_url, "source": source,
            }
        log.info(f"  Already up to date on '{channel}' (v{APP_VERSION}, asked {where})")
        return {
            "ok": True, "update_available": False,
            "current": APP_VERSION, "latest": latest_ver_s,
            "channel": channel, "server_url": server_url, "source": source,
        }
    finally:
        _update_state["checking"] = False


def _prune_old_cached_installers(keep: str = "") -> int:
    """Delete every cached installer (.exe / .zip) in UPDATE_DIR except
    `keep` (a bare filename).  Returns bytes freed.  Best-effort; swallows
    per-file errors (a file might be the one currently being read).

    3.4.3 — stops the updates cache from accumulating one ~44 MB
    installer per version.  The standalone updater does its own
    download for the actual install, so these cached copies are only
    used for the in-app 'already downloaded' fast-path — we never need
    more than the current target.

    Matches the pre-rename filename prefix as well as the current one, so
    an installer cached before the rename still gets cleaned up instead of
    sitting there forever."""
    prefixes = (f"{APP_NAME.lower()}_", f"{LEGACY_APP_NAME.lower()}_")
    freed = 0
    try:
        if not os.path.isdir(UPDATE_DIR):
            return 0
        for name in os.listdir(UPDATE_DIR):
            low = name.lower()
            if not low.startswith(prefixes):
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
            log.info(f"Pruned old cached installers: freed "
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
    # `version` comes from the server and becomes part of a filename.
    if not _VERSION_RE.fullmatch(version):
        err = f"Update server sent a malformed version string: {version!r}"
        log.warning(err)
        _update_state["error"] = err
        return {"ok": False, "err": err}
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
    dest = os.path.join(UPDATE_DIR, f"{APP_NAME}_{version}{ext}")

    # 3.4.3 — prune stale cached installers before caching the new one.
    # Each downloaded build is ~44 MB and the old code never removed
    # superseded copies — they only got swept by the integrity scan's
    # 7-day age sweep, so a burst of updates could pile up 500 MB+ in
    # UPDATE_DIR.  Now we keep ONLY the version we're
    # about to cache; everything else (the actual install already
    # happened, and the standalone updater re-downloads on demand) is
    # dead weight.  Best-effort — never blocks the download.
    _prune_old_cached_installers(keep=os.path.basename(dest))

    # Already downloaded?  Skip the transfer, but NOT the SHA-256 check below:
    # the file may be a truncated leftover, or a same-version rebuild may have
    # replaced what the server offers since it was cached.
    cached = os.path.exists(dest) and os.path.getsize(dest) > 1024

    source = _update_state.get("latest_source")
    err = _stage(url, _update_state.get("latest_sha256") or "", dest, cached)
    if err and source in update_sources.SOURCES:
        # The place that has the release could not deliver the file (its file
        # host blocked or failing, a broken transfer).  If the other place
        # has this same version, take it from there, checked against that
        # place's own SHA-256.
        other = _same_build_elsewhere(version, source)
        if other:
            log.info(f"  {err}  Trying {update_sources.SOURCE_NAMES[other['source']]} for v{version}.")
            _update_state["latest_url"]    = other["download_url"]
            _update_state["latest_sha256"] = other["sha256"]
            _update_state["latest_size"]   = other["size_bytes"]
            _update_state["latest_source"] = source = other["source"]
            err = _stage(other["download_url"], other["sha256"], dest, False)
    if err:
        _update_state["progress"] = 0
        _update_state["error"] = err
        return {"ok": False, "err": err}

    _update_state["progress"] = 100
    _update_state["download_complete"] = True
    _update_state["pending_install_path"] = dest
    _update_state["staged"] = {"version": version, "source": source}
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    log.info(f"  Downloaded {size_mb:.1f} MB, ready to install")
    return {"ok": True, "path": dest, "size_mb": round(size_mb, 1), "cached": cached}


def _same_build_elsewhere(version: str, tried: str) -> Optional[dict]:
    """The build `version` as another place than `tried` offers it (the
    answer of _ask_github / _ask_server), or None when no other place is
    asked with these settings, answers, or has exactly that version with a
    SHA-256."""
    s = _load_settings()
    channel = s.get("channel", APP_CHANNEL_DEFAULT)
    server_url = s.get("server_url", UPDATE_SERVER_URL_DEFAULT).rstrip("/")
    for source in update_sources.source_order(s.get("update_source"), server_url):
        if source == tried:
            continue
        answer = _ask_github(channel) if source == "github" else _ask_server(server_url, channel)
        if (answer and answer["available"] and answer.get("sha256") and answer.get("download_url")
                and _parse_version(answer["version"]) == _parse_version(version)):
            return answer
    return None


def _stage(url: str, expected_sha: str, dest: str, cached: bool) -> Optional[str]:
    """Bring the build at `url` to `dest` (`cached`: it is there already) and
    check it against `expected_sha`.  None when it is in place and verified;
    otherwise the error line, with nothing left at `dest`."""
    _update_state["downloading"] = True
    _update_state["progress"] = 0
    _update_state["download_complete"] = False
    try:
        if cached:
            log.info(f"  Update already downloaded: {dest}, re-verifying")
            ok = True
        elif url.startswith("local:"):
            # beta.11 — local source: "download" = copy the exe from the local
            # update folder.  The sha-256 verify below still runs on the staged
            # copy, so a mid-copy modification or truncation is caught the same
            # way a corrupted network download would be.
            src = url[len("local:"):]
            log.info(f"Copying {src} -> {dest}")
            try:
                shutil.copy2(src, dest)
                ok = True
            except Exception as e:
                log.warning(f"  Local update copy failed: {e}")
                ok = False
        else:
            log.info(f"Downloading {url} -> {dest}")
            ok = _http_download(url, dest)
    finally:
        _update_state["downloading"] = False

    if not ok:
        return "Download failed"

    def refuse(err):
        log.warning(err)
        try:
            os.remove(dest)
        except OSError:
            pass
        return err

    # v3: verify SHA-256 against the hash the release's place gave.  Belt-
    # and-suspenders: catches CDN corruption + MITM rewriting + truncated
    # downloads.  Fail CLOSED if none was supplied: a missing hash is
    # indistinguishable from a MITM/misconfigured server stripping it, and
    # installing an unverified binary is exactly what this check exists to
    # prevent.  (The live release server and GitHub always include one.)
    if not expected_sha:
        return refuse("Server did not provide a SHA-256 hash for this build; "
                      "refusing to install an unverified binary.")
    try:
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual_sha = h.hexdigest()
    except Exception as e:
        return refuse(f"Hash check failed: {e}")
    if actual_sha.lower() != str(expected_sha).lower():
        return refuse(f"Downloaded file failed SHA-256 check: "
                      f"expected {str(expected_sha)[:16]}…, got {actual_sha[:16]}…. "
                      "File may be corrupted; download will retry on next check.")
    log.info("  SHA-256 verified")
    return None


def _game_running() -> str:
    """The game game mode is tracking right now, or "".  Installing restarts
    Vispora, and game mode with it — for a few seconds mid-game."""
    try:
        from core import game_profiles
        e = game_profiles._engine
        return (e.get("active_game") or "A game") if e.get("gaming_mode") else ""
    except Exception:
        return ""


_INSTALL_AFTER_GAME_DELAY_S = 10        # let the post-game refresh and crash check run first


def on_game_closed() -> None:
    """Game mode has ended: an install held back for the game goes ahead."""
    if not _update_state.get("install_after_game"):
        return

    def later():
        time.sleep(_INSTALL_AFTER_GAME_DELAY_S)
        if _game_running():
            return                      # another game has started: wait for that one too
        _update_state["install_after_game"] = False
        log.info("The game has closed; installing the update that was waiting for it")
        try:
            install_update_now()
        except Exception as e:
            log.warning(f"Install after the game failed: {e}")

    threading.Thread(target=later, daemon=True, name="updater-after-game").start()


_install_lock = threading.Lock()


def install_update_now(force: bool = False) -> dict:
    """Install the staged update (see _install_update_now), one call at a
    time: starting it can take a while when a newer updater has to be fetched
    first, and a second click in that time must not start a second updater
    on the same files."""
    if _update_state.get("installing"):
        return {"ok": False, "err": "The update is already being installed."}
    if not _install_lock.acquire(blocking=False):
        return {"ok": False, "err": "The install is already being started. Give it a moment."}
    try:
        return _install_update_now(force)
    finally:
        _install_lock.release()


def _install_update_now(force: bool = False) -> dict:
    """Spawn the swap-and-restart batch, then exit the app.

    NOTE: the app process must die for the batch to overwrite the .exe.
    We schedule `os._exit(0)` 800ms after launching the batch so this
    response can be returned to the UI first.

    While game mode is tracking a game it waits instead, and installs when
    the game closes — unless `force`.
    """
    game = "" if force else _game_running()
    if game:
        _update_state["install_after_game"] = True
        log.info(f"Install held back: {game} is running, so it installs when the game closes")
        return {"ok": True, "deferred": True,
                "msg": f"{game} is running. The update installs as soon as it closes."}
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
        # beta.11 — local mode installs via the self-contained bat swap.  The
        # standalone updater re-downloads the build from the HTTP
        # server itself (ignoring our staged file), which can't work when the
        # update source is a folder on disk / the server is stopped.  The
        # staged file here is already sha-256-verified, so the direct swap is
        # exactly as trustworthy.
        if (_update_state.get("latest_url") or "").startswith("local:"):
            if not getattr(sys, "frozen", False):
                return {"ok": True,
                        "msg": "Update staged. Running from source, so no exe swap."}
            return _apply_exe_update_via_bat(path, _the_apps_exe())
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
# Called just before the process exits to install an update.  os._exit()
# skips atexit handlers, so anything that has to be undone first (gaming
# tweaks, a tuning run's offsets, suspended processes) is done through this.
# app.py registers its own restore routine here at startup.
_pre_exit_hook = None


def set_pre_exit_hook(fn) -> None:
    global _pre_exit_hook
    _pre_exit_hook = fn


def _exit_for_update() -> None:
    time.sleep(0.8)          # let the HTTP response flush first
    log.info("Exiting for update install...")
    if _pre_exit_hook is not None:
        try:
            _pre_exit_hook("update install")
        except Exception as e:
            log.warning(f"pre-exit restore failed: {e}")
    os._exit(0)


def _apply_exe_update_async(new_exe: str) -> dict:
    """Hand off to the standalone updater app.

    v3.2.2 rearchitecture: instead of bundling an updater binary
    INSIDE the app exe and shelling out to it, Vispora now
    auto-downloads a separate full-blown updater app to UPDATER_EXE
    (Flask + pywebview UI, ~30 MB).  At install time we just spawn that
    updater with the target_dir + channel + version pre-populated via CLI
    args and install_info.json, then exit.  The updater does everything
    from there — including downloading the actual new build itself, so
    Vispora never needs to touch the .exe payload at all.

    Why this is structurally better:
      • The updater has its own UI window so the user can see progress
        even after Vispora exits.
      • The updater is self-contained — it can update Vispora from
        anywhere, including portable installs, and across moves.
      • install_info.json travels with %APPDATA%, so the updater always
        knows where the current install lives.
      • New Vispora builds don't ship a stale bundled updater —
        the updater updates ITSELF independently.

    The `new_exe` param is now ignored (kept for back-compat) — the
    standalone updater does its own download.
    """
    log.info(f"Handing off to standalone {APP_NAME} Updater...")

    if not getattr(sys, "frozen", False):
        # Running from source — no in-place swap possible
        log.info("  Running from source, skipping in-place swap")
        return {
            "ok": True,
            "msg": f"Update available. Build a new exe to apply.",
        }

    # Make sure the standalone updater is installed.  Normally it's
    # already there from the boot-time bootstrap, but if the user
    # manually triggered an install before that finished, do it now
    # (blocking — only takes a few seconds).
    if not is_updater_installed():
        log.info("  Updater not installed, fetching it now")
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
                           f"Try restarting {APP_NAME} with internet "
                           f"available."}

    # What is being installed is what was staged: a check that failed since
    # then has cleared latest_*, but the file is still the build it was, from
    # the place it came from.
    staged = _update_state.get("staged") or {}
    target_version = staged.get("version") or _update_state.get("latest_version") or ""
    found_on = staged.get("source") or _update_state.get("latest_source") or "server"
    s = _load_settings()
    channel    = s.get("channel", APP_CHANNEL_DEFAULT)
    server_url = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/")

    # An updater from before 1.5.0 only knows the update server.  Handed an
    # update that was found on GitHub it would close the app, ask the server,
    # and stop there with the app closed if the server does not have it.  So
    # fetch the current updater first, and do not start an install the
    # updater on disk cannot finish.
    tell_source = _updater_knows_sources()
    if not tell_source and found_on == "github":
        log.info("  The updater on disk predates GitHub updates, looking for the current one first")
        tell_source = _refresh_updater_now()
    if not tell_source and found_on == "github":
        # No newer updater to be had.  The old one can still do it when the
        # update server has this build too.
        on_server = None
        if "server" in update_sources.source_order(s.get("update_source"), server_url):
            on_server = _ask_server(server_url, channel)
        if not (on_server and on_server["available"] and on_server.get("sha256") and target_version
                and _parse_version(on_server["version"]) >= _parse_version(target_version)):
            err = get_updater_install_status().get("err") or "no newer updater could be downloaded"
            log.warning(f"  Install not started: the updater on disk cannot install from GitHub ({err})")
            return {"ok": False,
                    "err": "This update is on GitHub, and the updater on this PC is too old to "
                           f"install from there ({err}). "
                           f"Try again in a few minutes, or get {APP_NAME} from "
                           f"{update_sources.GITHUB_RELEASES_PAGE}."}
        log.info("  The update server has this build too: the updater on disk installs it from there")

    # Refresh install_info.json so the updater has the latest current
    # state (path, channel, version)
    write_install_info()

    # Record the version transition for the post-install toast
    s = _load_settings()
    s["previous_version"]        = APP_VERSION
    s["pending_install_version"] = target_version
    _save_settings(s)

    current_exe = _the_apps_exe()

    args = [
        UPDATER_EXE,
        "--target-exe",       current_exe,
        "--target-dir",       os.path.dirname(current_exe),
        "--channel",          channel,
        "--current-version",  APP_VERSION,
        "--target-version",   target_version,
        "--server",           server_url,
        "--auto-confirm",
    ]
    if tell_source:
        # (An older updater would stop at an argument it does not know.)
        args += ["--source", update_sources.source_setting(s.get("update_source"))]
    log.info(f"  Spawning: {' '.join(args)}")

    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        # NOTE: we do NOT set CREATE_NO_WINDOW here — the updater is a
        # GUI app and the user needs to see its window.  DETACHED_PROCESS
        # is what we care about; that ensures the updater outlives us.
        # Breakaway as for the swap script below: started by the autostart
        # task, the app runs inside the task's job object.
        try:
            subprocess.Popen(
                args,
                creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                               | CREATE_BREAKAWAY_FROM_JOB),
                close_fds=True,
            )
        except OSError:
            # Job denies breakaway: spawn without it.
            subprocess.Popen(
                args,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
    except Exception as e:
        log.error(f"  Could not spawn standalone updater: {e}")
        return {"ok": False, "err": f"Could not spawn updater: {e}"}

    log.info(f"  Standalone updater launched. {APP_NAME} exiting...")
    _update_state["installing"] = True

    # Give the HTTP response a moment to flush, then die so the updater
    # can swap our exe without a file lock.
    threading.Thread(target=_exit_for_update, daemon=True).start()

    return {"ok": True,
            "msg": f"{APP_NAME} Updater launching. This window will close and "
                   "the updater will handle the rest.",
            "restart_required": True,
            "method": "standalone-updater"}


def _apply_exe_update_via_bat(new_exe: str, current_exe: str) -> dict:
    """Legacy fallback: write update.bat and spawn it.  Kept for builds
    that don't yet have the standalone updater — ie. the user is
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

    # beta.11 — PyInstaller ONEFILE runs as TWO processes of the same exe
    # (bootloader parent + Python child); waiting on our own PID alone leaves
    # the sibling still holding the exe image, so the copy below bounced off a
    # file lock 20 times and the update silently failed.  After the PID wait,
    # ALSO wait for the exe's image name to vanish from tasklist entirely
    # (bounded), then force-kill any zombie as a last resort — the app already
    # committed to exiting when this script was spawned.
    exe_image = os.path.basename(current_exe)
    # 3.5.2 — bat hardening after a real in-the-field failure (bat left on
    # disk, no status file, no copy, no relaunch — it died silently):
    #   • Writes a STARTED marker FIRST, so "no status file" now cleanly means
    #     "the script never even ran" vs "it ran and failed" (status says FAIL)
    #     vs "it succeeded" (OK).
    #   • Sleeps use `ping -n 2 127.0.0.1` instead of `timeout /t 1` —
    #     `timeout` requires an interactive console and errors out instantly
    #     in a hidden/detached cmd, turning every wait loop into a fast-spin.
    #   • Spawn flags below no longer combine DETACHED_PROCESS with
    #     CREATE_NO_WINDOW (they're mutually exclusive console modes; DETACHED
    #     wins and leaves cmd with NO console at all), and add
    #     CREATE_BREAKAWAY_FROM_JOB so the script survives the app's job
    #     object being torn down on exit (kill-on-close jobs kill detached
    #     children too — the silent death we observed).
    bat = (
        '@echo off\r\n'
        'setlocal\r\n'
        f'echo STARTED > "{status_path}"\r\n'
        f'echo === {APP_NAME} Update ===\r\n'
        f'echo Waiting for PID {pid} to exit...\r\n'
        'powershell -NoProfile -Command '
        f'"Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue" >nul 2>nul\r\n'
        'set wait=0\r\n'
        ':wait_loop\r\n'
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        'if errorlevel 1 goto pid_gone\r\n'
        'set /a wait+=1\r\n'
        'if %wait% gtr 60 goto pid_gone\r\n'
        'ping -n 2 127.0.0.1 >nul\r\n'
        'goto wait_loop\r\n'
        ':pid_gone\r\n'
        f'echo Waiting for all {exe_image} processes to exit...\r\n'
        'set iwait=0\r\n'
        ':image_loop\r\n'
        f'tasklist /FI "IMAGENAME eq {exe_image}" 2>nul | find /I "{exe_image}" >nul\r\n'
        'if errorlevel 1 goto image_gone\r\n'
        'set /a iwait+=1\r\n'
        f'if %iwait% gtr 30 taskkill /F /IM "{exe_image}" >nul 2>nul\r\n'
        'if %iwait% gtr 35 goto image_gone\r\n'
        'ping -n 2 127.0.0.1 >nul\r\n'
        'goto image_loop\r\n'
        ':image_gone\r\n'
        'set retries=20\r\n'
        ':try_copy\r\n'
        f'copy /y "{new_exe}" "{current_exe}" >nul 2>nul\r\n'
        'if %errorlevel% == 0 goto copy_ok\r\n'
        'set /a retries-=1\r\n'
        'if %retries% gtr 0 (\r\n'
        '  ping -n 2 127.0.0.1 >nul\r\n'
        '  goto try_copy\r\n'
        ')\r\n'
        f'echo FAIL: copy failed after 20 retries > "{status_path}"\r\n'
        f'echo *** Update copy failed, opening existing {APP_NAME}. ***\r\n'
        'goto launch\r\n'
        ':copy_ok\r\n'
        f'echo OK > "{status_path}"\r\n'
        'echo Update applied successfully.\r\n'
        ':launch\r\n'
        f'start "" "{current_exe}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    # cmd.exe reads a batch file in the console (OEM) code page.  Written as
    # ASCII, an "ö" in a path (a user name, say) became "?" and every command
    # using that path failed: the copy, the status file, the restart.
    with open(bat_path, "w", encoding="oem", errors="replace", newline="") as f:
        f.write(bat)

    try:
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                               | CREATE_BREAKAWAY_FROM_JOB),
                close_fds=True,
            )
        except OSError:
            # Job denies breakaway — spawn without it (still hidden-console).
            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
    except Exception as e:
        log.error(f"  Could not spawn updater script: {e}")
        return {"ok": False, "err": f"Could not spawn updater: {e}"}

    log.info(f"  Updater script launched (legacy .bat path).")
    _update_state["installing"] = True
    threading.Thread(target=_exit_for_update, daemon=True).start()
    return {"ok": True,
            "msg": f"Update installing. {APP_NAME} will restart in a moment.",
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
            return {"ok": False, "err": "Invalid update: app.py not found"}

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
        # Where the last check found what it found ("github" | "server" |
        # "local", None before the first check), and the places that are
        # asked, in order, with the settings as they are.
        "source":            _update_state.get("latest_source"),
        "source_order":      (["local"] if (s.get("local_update_dir") or "").strip() else
                              list(update_sources.source_order(
                                  s.get("update_source"),
                                  s.get("server_url") or UPDATE_SERVER_URL_DEFAULT))),
        "valid_sources":     list(update_sources.SOURCE_SETTINGS),
        "releases_page":     update_sources.GITHUB_RELEASES_PAGE,
        # v3 — install-safety state.  `install_blocked_reason` is empty
        # when auto-install would fire right now.
        "install_safe":              safety["safe"],
        "install_blocked_reason":    "" if safety["safe"] else safety["reason"],
        "user_idle_threshold_sec":   USER_IDLE_THRESHOLD_SEC,
        # An install the user asked for that is waiting for the game to close.
        "install_after_game":        bool(_update_state.get("install_after_game")),
    }


# ═══════════════════════════════════════════════════════════════════════════
# v3 — Safety gating: don't auto-install while user is busy
# ═══════════════════════════════════════════════════════════════════════════
#
# Rules (all must pass for auto-install to fire):
#   • No game running under game mode, and none in the foreground
#   • Vispora window not focused    — user is actively configuring
#   • User input idle for >= 5 min  — keyboard / mouse untouched
#
# Manual install (the user clicking "Install now") bypasses this gate —
# it's an explicit consent action — except that while game mode tracks a
# game it waits for the game to close (install_update_now), unless forced.

USER_IDLE_THRESHOLD_SEC = 5 * 60      # 5 minutes of no input
SAFETY_GATE_RECHECK_SEC = 120          # try again every 2 min when deferred
UPDATER_RETRY_MAX_SEC   = 6 * 3600     # longest wait between automatic tries of a failing updater download


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
        # Both values are DWORD tick counts.  ctypes hands GetTickCount back as
        # a signed int, which turns negative after 24.9 days of uptime — the
        # subtraction then clamped to 0 and the user never counted as idle.
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def _is_game_foreground() -> bool:
    """True if the foreground window belongs to a known game exe.
    Returns False if we can't determine it (fail-OPEN here — if we
    can't tell, we shouldn't block the update forever)."""
    try:
        from core import utils
        exe = utils.foreground_exe_name()
        if not exe:
            return False
        from core.game_profiles import KNOWN_GAME_EXES
        return exe.lower() in {e.lower() for e in KNOWN_GAME_EXES}
    except Exception:
        return False


def _is_own_window_foreground() -> bool:
    """True if one of our own windows is in the foreground."""
    try:
        from core import utils
        return (utils.foreground_exe_name() or "").lower() in OWN_PROCESS_NAMES
    except Exception:
        return False


def is_safe_to_install() -> dict:
    """Return {safe: bool, reason: str} indicating whether auto-install
    can fire right now without disrupting the user.  Caller logs the
    reason on defer so the in-app log shows why an update is waiting."""
    game = _game_running()
    if game:
        return {"safe": False, "reason": f"{game} is running"}
    if _is_game_foreground():
        return {"safe": False, "reason": "game in foreground"}
    if _is_own_window_foreground():
        return {"safe": False, "reason": f"{APP_NAME} window in foreground"}
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


def _updater_retry_wait() -> float:
    """Seconds an automatic install still has to wait before downloading the
    standalone updater again.  Every download that failed in a row doubles
    the wait, from SAFETY_GATE_RECHECK_SEC up to UPDATER_RETRY_MAX_SEC."""
    with _updater_install_lock:
        failures = _updater_install_state["failures"]
        failed_at = _updater_install_state["failed_at"]
    if not failures:
        return 0.0
    wait = min(SAFETY_GATE_RECHECK_SEC * 2 ** (failures - 1), UPDATER_RETRY_MAX_SEC)
    return max(0.0, failed_at + wait - time.time())


def _try_auto_install(quiet: bool = False) -> dict:
    """Auto-install the staged update IF it's safe to do so right now.
    Otherwise returns {ok: False, deferred: True, reason: str} and the
    install will be retried on the next periodic check (or by the
    dedicated safety-gate recheck loop)."""
    if not _update_state.get("download_complete") or not _update_state.get("update_available"):
        return {"ok": False, "err": "no download to install"}
    s = _load_settings()
    if not s.get("auto_install", False):
        return {"ok": False, "err": "auto_install setting is off"}
    safety = is_safe_to_install()
    if not safety["safe"]:
        if not quiet:
            log.info(f"Auto-install deferred: {safety['reason']}")
        return {"ok": False, "deferred": True, "reason": safety["reason"]}
    # Server mode installs through the standalone updater.  While it is not
    # on disk and its download keeps failing, don't fetch it again every
    # couple of minutes.  (Install now from the UI still tries at once.)
    staged_on = (_update_state.get("staged") or {}).get("source") or _update_state.get("latest_source")
    updater_missing = not is_updater_installed() or (staged_on == "github" and not _updater_knows_sources())
    if not (s.get("local_update_dir") or "").strip() and updater_missing:
        wait = _updater_retry_wait()
        if wait > 0:
            reason = f"the updater download keeps failing; next try in {int(wait)}s"
            if not quiet:
                log.info(f"Auto-install deferred: {reason}")
            return {"ok": False, "deferred": True, "reason": reason}
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
        # 3.5.2 — the bat now writes STARTED first, so the status string
        # distinguishes the failure modes:
        #   no status file → the swap script never ran at all (spawn failed)
        #   "STARTED"      → it ran but was killed mid-swap (job teardown / AV)
        #   "FAIL: …"      → it ran to completion but every copy was rejected
        bs = (bat_status or "").upper()
        if not bat_status:
            detail = "the update script never ran (spawn failed)"
        elif bs.startswith("STARTED"):
            detail = ("the update script was killed mid-swap "
                      "(likely by the app's process teardown or antivirus)")
        else:
            detail = bat_status
        outcome = {
            "state":    "install_failed",
            "previous": prev,
            "current":  APP_VERSION,
            "expected": pending,
            "bat":      detail,
        }
        log.warning(f"Post-install: still on v{APP_VERSION}, "
                    f"expected v{pending} (bat status: {bat_status or 'none'})")

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
