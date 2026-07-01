"""Community game library — crowd-sourced known-games list.

When a user adds a game to their custom-games list (because GhostShell
didn't recognize their game), we POST that exe + display name to the
Railway server's /community/games endpoint.  Other users fetch
/community/games on boot and auto-merge the result into their local
KNOWN_GAME_EXES.

Privacy
───────
Submission payload: { exe, name, anon_id, ghostshell_version }
  • exe       lowercase exe filename (e.g. "cs2.exe")
  • name      display name (e.g. "Counter-Strike 2")
  • anon_id   random 16-char per-install ID — server uses it for dedup
              + light abuse heuristics, never linked to anything else
  • version   GhostShell version (for filtering ancient builds out)

No PII, no path info, no user identifier.

Opt-out
───────
Settings.community.enabled = False suppresses all network I/O.

Local fallback
──────────────
If Railway is unreachable, GhostShell still works fine — just doesn't
auto-add community games until next successful fetch.  Cached list is
persisted so a brief outage doesn't wipe it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.request
from typing import Optional

from config import APP_VERSION, APPDATA_DIR, UPDATE_SERVER_URL_DEFAULT
from core.utils import get_logger, atomic_write_json, atomic_write_text

log = get_logger(__name__)


SETTINGS_PATH       = os.path.join(APPDATA_DIR, "community_settings.json")
COMMUNITY_CACHE_PATH = os.path.join(APPDATA_DIR, "community_games_cache.json")
ANON_ID_PATH        = os.path.join(APPDATA_DIR, "community_anon_id")

_DEFAULTS = {
    "enabled":         True,           # default ON; user can opt out
    "server_url":      UPDATE_SERVER_URL_DEFAULT,
    "auto_submit":     True,           # POST custom games we add locally
    "auto_merge":      True,           # GET + add to KNOWN_GAME_EXES on boot
    "last_fetch_at":   0.0,
    "last_submit_at":  0.0,
}

# Light client-side validation — keep parity with the server's deny list
# so we don't bother POSTing things that are obviously not games.
_CLIENT_DENY = {
    "explorer.exe", "dwm.exe", "taskhostw.exe", "svchost.exe",
    "runtimebroker.exe", "winlogon.exe", "csrss.exe", "smss.exe",
    "lsass.exe", "services.exe", "wininit.exe", "system",
    "chrome.exe", "firefox.exe", "msedge.exe", "brave.exe",
    "opera.exe", "vivaldi.exe", "iexplore.exe", "safari.exe",
    "discord.exe", "slack.exe", "teams.exe", "zoom.exe",
    "spotify.exe", "obs64.exe", "obs.exe", "streamlabs obs.exe",
    "code.exe", "devenv.exe", "pycharm64.exe", "rider64.exe",
    "notepad.exe", "notepad++.exe", "wordpad.exe", "calc.exe",
    "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe",
    "conhost.exe", "wt.exe", "openssh.exe",
    "ghostshell.exe", "ghostshellupdater.exe", "vispora.exe", "visporaupdater.exe",
    "presentmon.exe", "rtss.exe", "msi afterburner.exe",
}


# ─── Settings ───────────────────────────────────────────────────────

def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return {**_DEFAULTS, **data}
    except Exception:
        return dict(_DEFAULTS)


def _save_settings(s: dict) -> None:
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load_settings()


def set_settings(**kwargs) -> dict:
    s = _load_settings()
    for k in ("enabled", "auto_submit", "auto_merge"):
        if k in kwargs and kwargs[k] is not None:
            s[k] = bool(kwargs[k])
    if "server_url" in kwargs and isinstance(kwargs["server_url"], str):
        s["server_url"] = kwargs["server_url"].rstrip("/")
    _save_settings(s)
    return {"ok": True, "settings": s}


# ─── Anonymous ID (random per-install) ──────────────────────────────

def _get_anon_id() -> str:
    """16-char random ID stored at install time.  Used by the server
    for dedup + rate-limit, never linked to anything user-identifiable."""
    if os.path.exists(ANON_ID_PATH):
        try:
            with open(ANON_ID_PATH, "r", encoding="utf-8") as f:
                aid = f.read().strip()
            if aid and len(aid) >= 8:
                return aid
        except Exception: pass
    aid = secrets.token_hex(8)
    atomic_write_text(ANON_ID_PATH, aid)
    return aid


# ─── Client-side validation ─────────────────────────────────────────

def _valid_exe(exe: str) -> bool:
    exe = (exe or "").strip().lower()
    if not exe.endswith(".exe"):  return False
    if len(exe) < 5 or len(exe) > 64: return False
    if exe in _CLIENT_DENY:       return False
    # Allow letters, digits, underscore, period, dash, space (some game
    # exes have spaces).  No path separators, no weird unicode.
    for c in exe:
        if not (c.isalnum() or c in ". _-"):
            return False
    return True


def _valid_name(name: str) -> bool:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 80: return False
    # Just bound the length + no control characters
    for c in name:
        if ord(c) < 32: return False
    return True


# ─── Submit ─────────────────────────────────────────────────────────

def submit_game(exe: str, name: str) -> dict:
    """POST a custom game to the community server.  Skipped if community
    sharing is disabled.  Best-effort — network errors are logged but
    don't fail GhostShell's local add."""
    s = _load_settings()
    if not s.get("enabled") or not s.get("auto_submit"):
        return {"ok": True, "skipped": "community sharing off"}
    if not _valid_exe(exe):
        return {"ok": False, "err": f"exe failed local validation: {exe!r}"}
    if not _valid_name(name):
        return {"ok": False, "err": f"name failed local validation: {name!r}"}

    payload = {
        "exe":     exe.lower(),
        "name":    name,
        "anon_id": _get_anon_id(),
        "version": APP_VERSION,
    }
    url = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/") + "/community/submit"

    def _post():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent":   f"GhostShell/{APP_VERSION}",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read()
            s2 = _load_settings()
            s2["last_submit_at"] = time.time()
            _save_settings(s2)
            log.info(f"community submit ok: {exe} → {url}")
        except Exception as e:
            log.debug(f"community submit failed (best-effort): {e}")

    # Fire-and-forget — never block the UI thread on community I/O.
    threading.Thread(target=_post, daemon=True,
                     name="community-submit").start()
    return {"ok": True, "submitted": True, "exe": exe}


# ─── Fetch ──────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if not os.path.exists(COMMUNITY_CACHE_PATH):
        return {"games": [], "fetched_at": 0.0}
    try:
        with open(COMMUNITY_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"games": [], "fetched_at": 0.0}
    except Exception:
        return {"games": [], "fetched_at": 0.0}


def _save_cache(data: dict) -> None:
    atomic_write_json(COMMUNITY_CACHE_PATH, data)


def fetch_games(force: bool = False) -> dict:
    """GET /community/games.  Caches result for 6 hours.  Returns
    {ok, games, from_cache, fetched_at}.  Errors are logged but don't
    raise — the cache is used as fallback."""
    s = _load_settings()
    if not s.get("enabled") or not s.get("auto_merge"):
        return {"ok": True, "skipped": "community disabled",
                "games": _load_cache().get("games", [])}

    cache = _load_cache()
    age = time.time() - (cache.get("fetched_at") or 0)
    if not force and age < 6 * 3600 and cache.get("games"):
        return {"ok": True, "games": cache["games"],
                "from_cache": True, "fetched_at": cache["fetched_at"]}

    url = (s.get("server_url") or UPDATE_SERVER_URL_DEFAULT).rstrip("/") + "/community/games"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": f"GhostShell/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        games = data.get("games") or []
        # Validate each entry — server might be wrong/old
        clean = [g for g in games
                  if isinstance(g, dict)
                  and _valid_exe(g.get("exe", ""))
                  and _valid_name(g.get("name", ""))]
        _save_cache({"games": clean, "fetched_at": time.time()})
        s2 = _load_settings(); s2["last_fetch_at"] = time.time(); _save_settings(s2)
        log.info(f"community fetch ok: {len(clean)} games from {url}")
        return {"ok": True, "games": clean,
                "from_cache": False, "fetched_at": time.time()}
    except Exception as e:
        log.debug(f"community fetch failed, using cache: {e}")
        return {"ok": False, "err": str(e),
                "games":      cache.get("games", []),
                "from_cache": True,
                "fetched_at": cache.get("fetched_at", 0)}


def merge_into_known_games_async() -> None:
    """Fetch + merge community games into KNOWN_GAME_EXES.  Runs on a
    background thread; safe to call at GhostShell boot."""
    def _worker():
        try:
            r = fetch_games()
            if not (r.get("ok") or r.get("games")):
                return
            added = 0
            try:
                from core import game_profiles
                for g in r["games"]:
                    exe = (g.get("exe") or "").lower()
                    if exe and exe not in game_profiles.KNOWN_GAME_EXES:
                        game_profiles.KNOWN_GAME_EXES.add(exe)
                        added += 1
                if added:
                    log.info(f"community: added {added} games to KNOWN_GAME_EXES")
            except Exception as e:
                log.debug(f"community merge failed: {e}")
        except Exception as e:
            log.debug(f"community merge worker failed: {e}")
    threading.Thread(target=_worker, daemon=True,
                     name="community-merge").start()


# ─── Status ─────────────────────────────────────────────────────────

def get_status() -> dict:
    s = _load_settings()
    cache = _load_cache()
    return {
        "settings":    s,
        "anon_id":     _get_anon_id(),
        "cached_games": len(cache.get("games", [])),
        "cache_age_s": int(time.time() - (cache.get("fetched_at") or 0)),
    }
