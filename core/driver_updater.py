"""GPU driver auto-updater (v2.9.6).

NVIDIA: full auto-update path
  1. Detect current driver via nvidia-smi.
  2. Resolve the NVIDIA AjaxDriverService PSID + PFID for the user's GPU
     model (hardcoded table for popular cards; falls back to scraping
     NVIDIA's drivers index for unknown ones).
  3. Query the latest WHQL driver via:
        https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/
        services/AjaxDriverService.php?func=DriverManualLookup&...
     Returns Version + DownloadURL + DetailsURL + ReleaseDateTime.
  4. Compare numerically (e.g. 596.21 vs 600.85).
  5. If newer, download the .exe to APPDATA/drivers/ in the background.
  6. Surface a toast — when the user clicks "Install", we launch the
     installer interactively (NVIDIA's own UI handles the install).

AMD: detect installed version via WMI, then link the user to AMD's
download page.  Auto-installing AMD drivers from the web requires
signing-in / agreeing to EULAs that we can't drive headlessly.

Robustness:
  * All network calls have timeouts; failures degrade to "manual check".
  * Caches the PSID/PFID lookup (7-day TTL) so we don't hammer NVIDIA.
  * Periodic check runs every CHECK_INTERVAL_HOURS — no traffic when the
    setting is off.
  * Never auto-installs.  User confirmation required.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger, run_ps, atomic_write_json

log = get_logger("driver_updater")

DRIVERS_DIR     = os.path.join(APPDATA_DIR, "drivers")
SETTINGS_PATH   = os.path.join(APPDATA_DIR, "driver_updater_settings.json")
PSID_CACHE_PATH = os.path.join(APPDATA_DIR, "nvidia_driver_lookup_cache.json")
CHECK_INTERVAL_HOURS = 12

os.makedirs(DRIVERS_DIR, exist_ok=True)

NVIDIA_API_BASE = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/"
    "AjaxDriverService.php"
)

# ═══════════════════════════════════════════════════════════════════════════
# Hardcoded PSID/PFID mapping for popular GPUs.
#
# psid = NVIDIA "Product Series" id, pfid = the specific "Product" id — the two
# values DriverManualLookup needs.  Both come straight from NVIDIA's live
# product-lookup service:
#   .../Download/API/lookupValueSearch.aspx?TypeID=2&ParentID=1        (series → psid)
#   .../Download/API/lookupValueSearch.aspx?TypeID=3&ParentID=<psid>   (products → pfid)
#
# 3.4.3-beta.4 — the ENTIRE table was regenerated against that live service.
# The old values were stale: every pfid was wrong, and the RTX 50- and GTX
# 16-series even had the wrong psid (they pointed at the 40-series-laptop and
# MX300-laptop series).  Modern *unified* drivers masked it for some cards (a
# wrong pfid that happens to land on another current card returns the same
# Game-Ready build), but it silently handed RTX 40/30 owners an OUTDATED
# version and returned NO driver at all for the RTX 2070 SUPER.  If you ever
# edit this table, bump LOOKUP_TABLE_VERSION below so stale on-disk caches are
# discarded.
#
# Format: substring-of-model-name -> (psid, pfid).  Match is "first hit wins",
# so more-specific names come first ("Ti SUPER" before "Ti" before plain).
# Matched case-insensitively against nvidia-smi's reported GPU name.
# ═══════════════════════════════════════════════════════════════════════════
NVIDIA_GPU_LOOKUP = [
    # ─── RTX 50 Series (Blackwell, psid 131) ──────────────────────────────
    ("rtx 5090",          (131, 1066)),
    ("rtx 5080",          (131, 1065)),
    ("rtx 5070 ti",       (131, 1068)),
    ("rtx 5070",          (131, 1070)),
    ("rtx 5060 ti",       (131, 1076)),
    ("rtx 5060",          (131, 1078)),
    ("rtx 5050",          (131, 1090)),

    # ─── RTX 40 Series (Ada Lovelace, psid 127) ───────────────────────────
    ("rtx 4090",          (127, 995)),
    ("rtx 4080 super",    (127, 1041)),
    ("rtx 4080",          (127, 999)),
    ("rtx 4070 ti super", (127, 1040)),
    ("rtx 4070 ti",       (127, 1001)),
    ("rtx 4070 super",    (127, 1039)),
    ("rtx 4070",          (127, 1015)),
    ("rtx 4060 ti",       (127, 1022)),
    ("rtx 4060",          (127, 1023)),

    # ─── RTX 30 Series (Ampere, psid 120) ─────────────────────────────────
    ("rtx 3090 ti",       (120, 985)),
    ("rtx 3090",          (120, 930)),
    ("rtx 3080 ti",       (120, 964)),
    ("rtx 3080",          (120, 929)),
    ("rtx 3070 ti",       (120, 965)),
    ("rtx 3070",          (120, 933)),
    ("rtx 3060 ti",       (120, 934)),
    ("rtx 3060",          (120, 942)),
    ("rtx 3050",          (120, 975)),

    # ─── RTX 20 Series (Turing, psid 107) ─────────────────────────────────
    ("rtx 2080 ti",       (107, 877)),
    ("rtx 2080 super",    (107, 904)),
    ("rtx 2080",          (107, 879)),
    ("rtx 2070 super",    (107, 903)),
    ("rtx 2070",          (107, 880)),
    ("rtx 2060 super",    (107, 902)),
    ("rtx 2060",          (107, 887)),

    # ─── GTX 16 Series (Turing, psid 112) ─────────────────────────────────
    ("gtx 1660 ti",       (112, 892)),
    ("gtx 1660 super",    (112, 910)),
    ("gtx 1660",          (112, 895)),
    ("gtx 1650 super",    (112, 911)),
    ("gtx 1650",          (112, 897)),
    ("gtx 1630",          (112, 993)),

    # ─── GTX 10 Series (Pascal, psid 101) ─────────────────────────────────
    ("gtx 1080 ti",       (101, 845)),
    ("gtx 1080",          (101, 815)),
    ("gtx 1070 ti",       (101, 859)),
    ("gtx 1070",          (101, 816)),
    ("gtx 1060",          (101, 817)),
    ("gtx 1050 ti",       (101, 825)),
    ("gtx 1050",          (101, 826)),
    ("gt 1030",           (101, 852)),
]

# Bump whenever NVIDIA_GPU_LOOKUP changes so any cached (psid,pfid) entries
# written by an older build are ignored instead of overriding corrected IDs.
LOOKUP_TABLE_VERSION = 2

# NVIDIA driver-search OS ids (from the live lookupValueSearch TypeID=4 list).
# 3.4.3-beta.5 — the old code hardcoded 57 with a comment claiming it was
# "Windows 11"; 57 is actually *Windows 10 64-bit* and Windows 11 is a
# separate id (135).  Today NVIDIA serves the same DCH driver to both, so it
# worked by luck — but Windows 10 is EOL (Oct 2025) and NVIDIA is winding down
# its Win10 driver listings, so a hardcoded 57 would eventually return stale or
# empty results for the Windows-11 majority.  We now pick the id at runtime.
NVIDIA_OS_ID_WIN10_X64 = 57
NVIDIA_OS_ID_WIN11     = 135


def _nvidia_os_id() -> int:
    """Return the NVIDIA OS id matching the running Windows.  Windows 11 still
    reports major.minor 10.0 but with build >= 22000."""
    try:
        ver = sys.getwindowsversion()
        if getattr(ver, "major", 0) >= 10 and getattr(ver, "build", 0) >= 22000:
            return NVIDIA_OS_ID_WIN11
    except Exception:
        pass
    return NVIDIA_OS_ID_WIN10_X64


# ═══════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════
_DEFAULT_SETTINGS = {
    "auto_check":    True,    # Check on startup + every 12h
    "auto_download": False,   # Download installer once a new version is detected
                              # (default OFF — drivers are large, ask first)
    "last_check_ts": 0.0,
}


def _load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {**_DEFAULT_SETTINGS, **(d if isinstance(d, dict) else {})}
    except Exception:
        return dict(_DEFAULT_SETTINGS)


def _save_settings(s: dict):
    atomic_write_json(SETTINGS_PATH, s)


def get_settings() -> dict:
    return _load_settings()


def set_settings(auto_check: Optional[bool] = None,
                 auto_download: Optional[bool] = None) -> dict:
    s = _load_settings()
    if auto_check    is not None: s["auto_check"]    = bool(auto_check)
    if auto_download is not None: s["auto_download"] = bool(auto_download)
    _save_settings(s)
    return {"ok": True, "settings": s}


# ═══════════════════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════════════════
_state = {
    "vendor":             None,    # 'nvidia' | 'amd' | 'unknown'
    "model":              None,    # GPU display name
    "current_version":    None,    # currently installed driver
    "latest_version":     None,    # latest from vendor API
    "update_available":   False,
    "download_url":       None,
    "release_url":        None,    # link to release notes
    "release_date":       None,
    "size_mb":            None,
    "checking":           False,
    "downloading":        False,
    "installing":         False,
    "install_result":     None,    # None | "success" | "unconfirmed"
    "install_message":    None,
    "download_progress":  0,
    "downloaded_path":    None,
    "last_check_ts":      0,
    "error":              None,
    "manual_link":        None,    # for AMD / unknown — point user at vendor site
}


# ═══════════════════════════════════════════════════════════════════════════
# GPU detection
# ═══════════════════════════════════════════════════════════════════════════
def _detect_nvidia() -> Optional[dict]:
    """Use nvidia-smi to read GPU model + currently installed driver version."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return None     # No NVIDIA driver installed at all
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    line = r.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    return {
        "vendor":          "nvidia",
        "model":           parts[0],
        "current_version": parts[1],
    }


def _detect_amd() -> Optional[dict]:
    r = run_ps(
        "Get-CimInstance Win32_VideoController | "
        "Where-Object { ($_.Name -like '*Radeon*') -or ($_.Name -like '*AMD*') } | "
        "Select-Object Name,DriverVersion | ConvertTo-Json -Compress",
        timeout=10,
    )
    if not (r.get("ok") and r.get("out")):
        return None
    try:
        data = json.loads(r["out"])
    except Exception:
        return None
    if isinstance(data, list):
        if not data: return None
        data = data[0]
    name = (data.get("Name") or "").strip()
    if not name:
        return None
    return {
        "vendor":          "amd",
        "model":           name,
        "current_version": (data.get("DriverVersion") or "").strip(),
    }


def detect_gpu() -> dict:
    """NVIDIA preferred (better support); AMD second; otherwise unknown."""
    g = _detect_nvidia()
    if g: return g
    g = _detect_amd()
    if g: return g
    return {"vendor": "unknown", "model": "Unknown GPU", "current_version": ""}


# ═══════════════════════════════════════════════════════════════════════════
# NVIDIA latest-driver lookup
# ═══════════════════════════════════════════════════════════════════════════
def _http_get_json(url: str, timeout: int = 15) -> dict:
    """GET → JSON.  Sets a UA so NVIDIA's WAF doesn't 403 us."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GhostShell/2.9.6",
            "Accept":     "application/json,text/javascript,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _resolve_nvidia_ids(gpu_name: str) -> Optional[tuple]:
    """Map a GPU model string to NVIDIA's (psid, pfid) — the IDs needed
    for the AjaxDriverService.

    Strategy:
      1. Cache hit on previously-resolved entry → use cached IDs.
      2. Try the hardcoded NVIDIA_GPU_LOOKUP table (most popular cards).
      3. Otherwise return None and the caller falls back to manual link.
    """
    # Cache check
    if os.path.exists(PSID_CACHE_PATH):
        try:
            with open(PSID_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
            entry = cache.get(gpu_name.lower())
            # Ignore entries written by an older lookup table (tv mismatch)
            # so a stale cached (psid,pfid) can't override a corrected one.
            if (entry and entry.get("tv") == LOOKUP_TABLE_VERSION
                    and time.time() - entry.get("ts", 0) < 7 * 86400):
                return (entry["psid"], entry["pfid"])
        except Exception:
            pass

    name_lower = gpu_name.lower()
    for needle, ids in NVIDIA_GPU_LOOKUP:
        if needle in name_lower:
            # Save to cache
            try:
                cache = {}
                if os.path.exists(PSID_CACHE_PATH):
                    with open(PSID_CACHE_PATH, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                cache[gpu_name.lower()] = {"psid": ids[0], "pfid": ids[1],
                                           "ts": time.time(),
                                           "tv": LOOKUP_TABLE_VERSION}
                atomic_write_json(PSID_CACHE_PATH, cache)
            except Exception:
                pass
            return ids

    return None


def _check_nvidia_latest(gpu_name: str) -> dict:
    """Hit NVIDIA's API and return latest driver info.

    Returns one of:
      {ok:True, version, download_url, release_url, release_date, size_mb}
      {ok:False, err, manual_link}     ← user should be sent to NVIDIA's site
    """
    ids = _resolve_nvidia_ids(gpu_name)
    manual_link = ("https://www.nvidia.com/Download/index.aspx?lang=en-us")
    if not ids:
        return {
            "ok": False,
            "err": f"GPU not in our model database — please check NVIDIA's site for {gpu_name}",
            "manual_link": manual_link,
        }
    psid, pfid = ids
    url = (f"{NVIDIA_API_BASE}?func=DriverManualLookup"
           f"&psid={psid}&pfid={pfid}"
           f"&osID={_nvidia_os_id()}"
           f"&languageCode=1033&beta=0&isWHQL=1&dch=1"
           f"&sort1=0&numberOfResults=1")
    try:
        data = _http_get_json(url)
    except urllib.error.URLError as e:
        return {"ok": False, "err": f"Could not reach NVIDIA API: {e}", "manual_link": manual_link}
    except Exception as e:
        return {"ok": False, "err": f"NVIDIA API error: {e}", "manual_link": manual_link}

    success = data.get("Success")
    ids_arr = data.get("IDS") or []
    if not (success in (1, "1") and ids_arr):
        return {
            "ok": False,
            "err": "NVIDIA API returned no drivers (your card may not be in the queried series)",
            "manual_link": manual_link,
        }
    info = ids_arr[0].get("downloadInfo") or {}

    return {
        "ok":            True,
        "version":       (info.get("Version") or "").strip(),
        "download_url":  (info.get("DownloadURL") or "").strip(),
        "release_url":   (info.get("DetailsURL") or "").strip(),
        "release_date":  (info.get("ReleaseDateTime") or "").strip(),
        "size_mb":       (info.get("DownloadURLFileSize") or "").strip(),
    }


def _version_tuple(v: str) -> tuple:
    """Parse '596.21' or '32.0.15.6094' into a comparable 4-tuple.

    Pads to 4 components so '596' and '596.0.0.0' compare equal — same
    reasoning as `updater._parse_version` (avoid the (a,b) < (a,b,0)
    Python tuple semantics that would falsely flag an update).
    """
    parts = [int(p) for p in re.findall(r"\d+", v or "")]
    if not parts:
        return (0, 0, 0, 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


# ═══════════════════════════════════════════════════════════════════════════
# Check
# ═══════════════════════════════════════════════════════════════════════════
def check_for_update(force: bool = False) -> dict:
    """Detect GPU, query the latest driver, populate _state."""
    if _state["checking"]:
        return _state.copy()
    _state["checking"] = True
    _state["error"]    = None
    try:
        gpu = detect_gpu()
        _state["vendor"]          = gpu.get("vendor")
        _state["model"]           = gpu.get("model")
        _state["current_version"] = gpu.get("current_version")
        _state["update_available"] = False
        _state["latest_version"]   = None
        _state["download_url"]     = None
        _state["release_url"]      = None
        _state["release_date"]     = None
        _state["size_mb"]          = None
        _state["manual_link"]      = None

        if gpu["vendor"] == "nvidia":
            r = _check_nvidia_latest(gpu["model"])
            if r["ok"]:
                _state["latest_version"] = r["version"]
                _state["download_url"]   = r["download_url"]
                _state["release_url"]    = r["release_url"]
                _state["release_date"]   = r["release_date"]
                _state["size_mb"]        = r["size_mb"]
                cur = _version_tuple(gpu["current_version"])
                lat = _version_tuple(r["version"])
                _state["update_available"] = (lat > cur)
                if _state["update_available"]:
                    log.info(f"NVIDIA driver update: {gpu['current_version']} -> {r['version']}")
                else:
                    log.info(f"NVIDIA driver up to date: {gpu['current_version']}")
            else:
                _state["error"]       = r.get("err")
                _state["manual_link"] = r.get("manual_link")
        elif gpu["vendor"] == "amd":
            # AMD: just point at the download page.  Auto-install would
            # require driving the AMD installer EULA which we can't.
            _state["manual_link"] = "https://www.amd.com/en/support"
            _state["error"]       = ("AMD: please use the AMD support site to download the latest "
                                      "Adrenalin driver for your card.")
        else:
            _state["error"] = "Could not detect a supported GPU (NVIDIA / AMD)."

        _state["last_check_ts"] = time.time()
        s = _load_settings()
        s["last_check_ts"] = _state["last_check_ts"]
        _save_settings(s)
        return _state.copy()
    finally:
        _state["checking"] = False


# ═══════════════════════════════════════════════════════════════════════════
# Download + install
# ═══════════════════════════════════════════════════════════════════════════
def _prune_old_drivers(keep_fname: str = "") -> int:
    """Delete every *.exe in DRIVERS_DIR except `keep_fname`.  Returns
    bytes freed.

    3.4.3 — NVIDIA driver installers are ~960 MB each.  The old code
    never removed superseded ones, so the drivers cache grew by ~1 GB
    per driver release (the dev machine had 2.7 GB / 3 installers).
    Once a newer driver exists the old installer is useless — we keep
    only the one we're about to download / just downloaded.  Best-
    effort; per-file errors are ignored."""
    freed = 0
    try:
        if not os.path.isdir(DRIVERS_DIR):
            return 0
        for name in os.listdir(DRIVERS_DIR):
            if not name.lower().endswith(".exe"):
                continue
            if name == keep_fname:
                continue
            p = os.path.join(DRIVERS_DIR, name)
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                freed += sz
            except OSError:
                continue
        if freed:
            log.info(f"Pruned old driver installers — freed "
                     f"{freed / (1024*1024):.0f} MB from {DRIVERS_DIR}")
    except Exception as e:
        log.debug(f"driver prune failed: {e}")
    return freed


def download_driver() -> dict:
    """Pull the .exe to APPDATA/drivers/.  Idempotent — if a file with the
    expected size already exists, returns immediately.
    """
    if not _state.get("download_url"):
        return {"ok": False, "err": "No download URL available — run a check first"}
    if _state["downloading"]:
        return {"ok": False, "err": "Already downloading"}

    url = _state["download_url"]
    fname = url.split("/")[-1].split("?")[0] or f"nvidia_{_state.get('latest_version','x')}.exe"
    if not fname.lower().endswith(".exe"):
        fname += ".exe"
    dest = os.path.join(DRIVERS_DIR, fname)

    # 3.4.3 — clear any older driver installers before pulling this one
    # so the cache never holds more than the current version (~960 MB
    # instead of growing ~1 GB per release).
    _prune_old_drivers(keep_fname=fname)

    # Already downloaded?  NVIDIA installers are >100 MB, so use that as
    # a sanity threshold.
    if os.path.exists(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
        _state["downloaded_path"]   = dest
        _state["download_progress"] = 100
        return {"ok": True, "path": dest, "cached": True}

    _state["downloading"]       = True
    _state["download_progress"] = 0
    log.info(f"Downloading NVIDIA driver from {url}")
    try:
        cmd = (
            "$ProgressPreference = 'SilentlyContinue'; "
            "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
            f'Invoke-WebRequest -Uri "{url}" -OutFile "{dest}" -UseBasicParsing'
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=900,    # 15 min for ~800 MB
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _state["downloading"] = False
        return {"ok": False, "err": "Download timed out (>15 min)"}
    except Exception as e:
        _state["downloading"] = False
        return {"ok": False, "err": str(e)}
    finally:
        _state["downloading"] = False

    if r.returncode != 0 or not os.path.exists(dest):
        return {"ok": False, "err": (r.stderr or r.stdout or "Download failed")[:200]}
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    _state["downloaded_path"]   = dest
    _state["download_progress"] = 100
    log.info(f"Driver download complete: {dest} ({size_mb:.1f} MB)")
    return {"ok": True, "path": dest, "size_mb": round(size_mb, 1)}


def _query_nvidia_version() -> str:
    """Read just the installed NVIDIA driver version via nvidia-smi.
    Returns '' if unavailable.  Used by the post-install watcher to
    confirm the version actually changed (cheap — one field)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    return r.stdout.strip().splitlines()[0].strip()


def _watch_install(target_version: str, prev_version: str):
    """Closed-loop confirmation that a launched driver install actually
    landed.

    3.4.3-beta.4 — `install_driver()` can only verify that the shell
    *accepted* the launch; it cannot tell whether the installer ran or
    succeeded (we launch de-elevated via explorer, so there's no child
    handle to wait on).  This watcher polls nvidia-smi slowly (every
    30 s, up to 45 min) and, the moment the live driver version rises to
    the target — or simply changes, if we have no target — it records
    success (which the UI surfaces as a toast on its next status poll)
    and prunes the now-useless ~960 MB installer.

    On timeout it just clears the `installing` flag and leaves
    `install_result` as "unconfirmed".  We deliberately DON'T raise a
    failure toast: NVIDIA's own installer window already reports its
    errors, and a false "install failed" would be worse than silence.
    The 30 s cadence keeps CPU negligible while minimized in tray."""
    deadline = time.time() + 45 * 60
    tgt = _version_tuple(target_version) if target_version else None
    try:
        while time.time() < deadline:
            time.sleep(30)
            cur = _query_nvidia_version()
            if not cur:
                continue
            changed = (cur != prev_version)
            reached = (tgt is None) or (_version_tuple(cur) >= tgt)
            if changed and reached:
                _state["current_version"]  = cur
                _state["update_available"] = False
                _state["install_result"]   = "success"
                _state["install_message"]  = f"Driver updated to {cur}."
                log.info(f"Driver install confirmed — now on {cur}")
                # The installer has served its purpose; reclaim the space.
                try:
                    p = _state.get("downloaded_path")
                    if p and os.path.exists(p):
                        sz = os.path.getsize(p)
                        os.remove(p)
                        _state["downloaded_path"] = None
                        log.info(f"Removed used driver installer "
                                 f"({sz / (1024*1024):.0f} MB)")
                except OSError:
                    pass
                return
        _state["install_result"]  = "unconfirmed"
        _state["install_message"] = ("Could not confirm the driver version "
                                     "changed — check NVIDIA's installer window.")
        log.info("Driver install watcher timed out without a version change")
    finally:
        _state["installing"] = False


def install_driver() -> dict:
    """Launch the downloaded NVIDIA installer the SAME way a user
    double-clicking it in File Explorer would.

    3.4.3-beta.3 FIX — the long-standing "GhostShell-installed drivers
    error but website-downloaded ones work" bug.

    Root cause was NOT the download: the file is byte-for-byte valid
    (Authenticode signature verifies).  It was the LAUNCH.  The old
    code did:
        subprocess.Popen([path],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    GhostShell runs elevated (uac_admin manifest), so the installer
    inherited GhostShell's already-elevated token in a detached,
    console-less, new-process-group context.  NVIDIA's installer is a
    self-extracting bootstrapper that unpacks to C:\\NVIDIA and then
    re-launches setup.exe plus component sub-installers (Display
    Driver, HD Audio, PhysX, …).  That multi-process hand-off breaks
    when the bootstrapper is started pre-elevated + detached — the GUI
    appears but the install fails partway, consistently, on every
    machine.  A website download works because the user double-clicks
    it from (medium-integrity) Explorer, which launches the installer
    de-elevated and lets it run its OWN fresh UAC elevation.

    Fix: launch via `explorer.exe <path>` — the documented way to drop
    from an elevated process back to the user's normal medium-IL token.
    That reproduces a double-click exactly: correct interactive-desktop
    context, correct working directory, and the installer does its own
    UAC elevation instead of inheriting ours.
    """
    path = _state.get("downloaded_path")
    if not path or not os.path.exists(path):
        return {"ok": False, "err": "Driver installer not downloaded yet"}
    if _state.get("installing"):
        return {"ok": False, "err": "An install is already in progress"}

    launched = False
    method = ""
    # Primary — explorer.exe mediates the launch at the user's integrity
    # level (de-elevates), exactly like double-clicking in File Explorer.
    try:
        subprocess.Popen(["explorer.exe", path], close_fds=True)
        launched = True
        method = "explorer"
    except Exception as e:
        log.warning(f"explorer-mediated driver launch failed: {e}")

    # Fallback — ShellExecute 'open' with the installer's own folder as
    # the working dir.  Still routes through the shell (interactive-
    # desktop aware) unlike a raw detached Popen; only used if explorer
    # itself couldn't be spawned.
    if not launched:
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "open", path, None,
                os.path.dirname(path), 1)   # SW_SHOWNORMAL
            launched = rc > 32   # ShellExecute returns >32 on success
            method = f"shellexecute(rc={rc})"
        except Exception as e:
            return {"ok": False, "err": f"Could not launch installer: {e}"}

    if not launched:
        return {"ok": False,
                "err": "The shell refused to launch the installer — "
                       "open it manually from " + path}

    log.info(f"Driver installer launched via {method} (de-elevated like a "
             f"double-click): {path}")

    # Closed-loop confirmation (3.4.3-beta.4): the shell accepting the
    # launch does NOT mean the install ran or succeeded.  Kick off a slow
    # background watcher that confirms via nvidia-smi, surfaces a success
    # toast, and reclaims the installer once the new driver is live.
    _state["installing"]      = True
    _state["install_result"]  = None
    _state["install_message"] = None
    target = _state.get("latest_version") or ""
    prev   = _state.get("current_version") or ""
    threading.Thread(target=_watch_install, args=(target, prev),
                     daemon=True).start()

    return {"ok": True,
            "msg": "NVIDIA installer launched — GhostShell will confirm once "
                   "the new driver is active."}


def get_status() -> dict:
    s = _state.copy()
    s["settings"] = _load_settings()
    s["check_interval_hours"] = CHECK_INTERVAL_HOURS
    return s


# ═══════════════════════════════════════════════════════════════════════════
# Background tasks
# ═══════════════════════════════════════════════════════════════════════════
def _maybe_auto_download():
    s = _load_settings()
    if not s.get("auto_download", False):
        return
    if not _state.get("update_available"):
        return
    if _state.get("downloaded_path"):   # already on disk
        return
    log.info("Auto-downloading driver in background...")
    download_driver()


def _check_and_maybe_download():
    try:
        check_for_update()
        _maybe_auto_download()
    except Exception as e:
        log.warning(f"Driver check cycle failed: {e}")


def _periodic_loop():
    interval_s = CHECK_INTERVAL_HOURS * 3600
    while True:
        time.sleep(interval_s)
        s = _load_settings()
        if not s.get("auto_check", True):
            continue
        log.info(f"Periodic driver check ({CHECK_INTERVAL_HOURS}h)...")
        _check_and_maybe_download()


def check_in_background():
    """Called once on app startup."""
    s = _load_settings()
    if s.get("auto_check", True):
        threading.Thread(target=_check_and_maybe_download, daemon=True).start()
    threading.Thread(target=_periodic_loop, daemon=True).start()
