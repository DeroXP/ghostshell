"""Game Library Scanner — discover what games are installed across all major
PC launchers so GhostShell can pre-populate AT profiles and surface them on
the dashboard before the user has to launch each title once.

Supported launchers:
    Steam        — libraryfolders.vdf + appmanifest_*.acf parsing
    Epic Games   — %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item
    Riot Games   — known install paths + registry InstallLocation
    Battle.net   — registry per-product InstallPath
    GOG Galaxy   — registry HKLM\\SOFTWARE\\WOW6432Node\\GOG.com\\Games
    Ubisoft      — registry HKLM\\SOFTWARE\\WOW6432Node\\Ubisoft\\Launcher\\Installs
    EA App       — registry + default install dirs
    Microsoft / Xbox — C:\\XboxGames\\* directory scan

Each scanner is best-effort and exception-safe: failure of one launcher
never blocks discovery via another.  Results are cached in
%APPDATA%\\GhostShell\\library_cache.json and only re-scanned on demand.

Each detected game is enriched with:
    title           — display name
    exe             — best-guess executable filename (lowercase)
    launcher        — which launcher reported it
    install_path    — root install directory
    known           — True if exe matches KNOWN_GAME_EXES
    mode_hint       — competitive / visual / balanced (from AUTO_CLASSIFY) or None

Public API:
    scan_all(force=False)   -> {"games": [...], "scanned_at": ts}
    get_cached()            -> last cached scan or empty
    get_known_only()        -> filter to only games GhostShell already
                                recognizes (so the dashboard doesn't list
                                every Unity tool / launcher EXE)
"""

from __future__ import annotations
import json
import os
import re
import time
import glob
from typing import Optional

from core.utils import get_logger
from config import APPDATA_DIR

log = get_logger(__name__)

CACHE_FILE = os.path.join(APPDATA_DIR, "library_cache.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Files that look like .exe but are NEVER the actual game — skip when picking
# a "best exe" from an install dir.
_LAUNCHER_NOISE = {
    # Common redist installers
    "vcredist_x86.exe", "vcredist_x64.exe", "vc_redist.x86.exe", "vc_redist.x64.exe",
    "dxsetup.exe", "dxwebsetup.exe", "directx_jun2010_redist.exe",
    "dotnetfx.exe", "dotnetfx35.exe", "dotnetfx40_full_x86_x64.exe",
    "oalinst.exe", "physx_9.13.0604_systemsoftware.exe",
    "ueprereqsetup_x64.exe", "ueprereqsetup_x86.exe",
    # Anti-cheat installers
    "easyanticheat_setup.exe", "easyanticheat_eos_setup.exe",
    "battleye_launcher.exe", "be_service.exe", "beservice_x64.exe",
    # Crash handlers (Unity / Unreal)
    "unitycrashhandler64.exe", "unitycrashhandler32.exe",
    "ue4prereqsetup_x64.exe", "crashreportclient.exe",
    # Misc
    "uninst.exe", "uninstall.exe", "unins000.exe", "unins001.exe",
    "vivoxbridge.exe", "epicwebhelper.exe",
    # Source-engine / Valve tools (appear in TF2, HL:Alyx, etc.)
    "vconsole2.exe", "vconsole.exe", "qc_eyes.exe", "studiomdl.exe",
    "vrad3.exe", "vrad.exe", "vbsp.exe", "vvis.exe", "hlmv.exe",
    "hlfaceposer.exe", "elementviewer.exe", "dmxedit.exe", "dmxconvert.exe",
    "glview.exe", "motionmapper.exe", "serverbrowser.exe",
    "tools.exe", "vpk.exe", "captioncompiler.exe", "particletool.exe",
    # Common tool / redist patterns
    "busybox.exe", "busybox64.exe", "7z.exe", "7za.exe",
    "celeste-uninstall.exe", "ffmpeg.exe", "python.exe",
    # Aimlabs / lab tools
    "aimlablinkshandler.exe", "aimlabnotificationhandler.exe",
    # Mojang / launcher subsidiaries
    "mcplaceholderstub.exe",
}

# Directories under a Steam install that are never the game itself
_INSTALL_NOISE_DIRS = (
    "redist", "_commonredist", "directx", "dxsetup", "dotnet",
    "easyanticheat", "battleye", "vivox", "ueprereqsetup",
    "_redist", "vc_redist", "support",
)


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except (OSError, PermissionError):
        return []


def _is_noise_dir(rel_path: str) -> bool:
    rel_lower = rel_path.lower().replace("/", "\\")
    return any(noise in rel_lower for noise in _INSTALL_NOISE_DIRS)


def _read_registry_value(hive: str, subkey: str, value: str) -> Optional[str]:
    """Read a registry string value safely.  Returns None on any failure."""
    try:
        import winreg
        hkey_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
                    "HKCU": winreg.HKEY_CURRENT_USER}
        with winreg.OpenKey(hkey_map[hive], subkey) as k:
            v, _ = winreg.QueryValueEx(k, value)
            return str(v) if v else None
    except (FileNotFoundError, OSError, KeyError):
        return None


def _enum_registry_subkeys(hive: str, subkey: str) -> list[str]:
    try:
        import winreg
        hkey_map = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
                    "HKCU": winreg.HKEY_CURRENT_USER}
        out = []
        with winreg.OpenKey(hkey_map[hive], subkey) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        return out
    except (FileNotFoundError, OSError, KeyError):
        return []


def _pick_best_exe(install_path: str, max_depth: int = 4,
                   title_hint: str = "") -> Optional[str]:
    """Walk an install dir up to max_depth and return the .exe most likely
    to be the actual game.

    Heuristic (in order):
      1. Skip files matching _LAUNCHER_NOISE (tools, redists, crash handlers)
      2. Skip files smaller than 500KB (tiny stubs)
      3. Score each candidate:
         - depth 0 (install root)        +1000
         - depth 1                        +400
         - depth 2                        +100
         - exe name contains sanitized
           title hint                     +500
         - file size in MB (capped 200)   +size_mb
      4. Highest score wins
    """
    if not install_path or not os.path.isdir(install_path):
        return None

    base_depth = install_path.rstrip("\\").count("\\")
    title_token = re.sub(r"[^a-z0-9]", "", (title_hint or "").lower())[:12]

    candidates: list[tuple[int, str]] = []  # (score, full_path)

    for root, dirs, files in os.walk(install_path):
        depth = root.count("\\") - base_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        rel = os.path.relpath(root, install_path)
        if rel != "." and _is_noise_dir(rel):
            dirs[:] = []
            continue

        for fname in files:
            low = fname.lower()
            if not low.endswith(".exe"):
                continue
            if low in _LAUNCHER_NOISE:
                continue
            full = os.path.join(root, fname)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < 500_000:
                continue

            score = 0
            # Depth: prefer .exes at install root, but a deep "shipping" exe
            # still wins because the canonical-game boost below is bigger
            if depth == 0:
                score += 1000
            elif depth == 1:
                score += 400
            elif depth == 2:
                score += 100
            # Canonical UE / Frostbite "shipping" binaries: this IS the game
            if low.endswith("-win64-shipping.exe") or low.endswith("-wingdk-shipping.exe") \
                    or low.endswith("shipping.exe"):
                score += 1500
            # Penalize obvious launcher / server / tool patterns
            if low.endswith("_launcher.exe") or low.endswith("launcher.exe"):
                score -= 600
            if low.endswith("server.exe") or low.endswith("server64.exe") \
                    or low.startswith("dedicated") or "dedicatedserver" in low:
                score -= 800
            if low.endswith("editor.exe") or low.endswith("_editor.exe") \
                    or low.endswith("viewer.exe") or "elementviewer" in low \
                    or low.endswith("_dev.exe") or "_test" in low:
                score -= 500
            # Title-name match boost
            if title_token and title_token in re.sub(r"[^a-z0-9]", "", low):
                score += 500
            # Size in MB, capped — prefer larger but don't let it dominate
            score += min(int(size / (1024 * 1024)), 200)
            candidates.append((score, full))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _enrich(detected: dict) -> dict:
    """Tag a detected game with KNOWN/mode hint based on its exe."""
    try:
        from core.game_profiles import KNOWN_GAME_EXES
    except Exception:
        KNOWN_GAME_EXES = set()
    try:
        from core.adaptive_tuning import AUTO_CLASSIFY
    except Exception:
        AUTO_CLASSIFY = {}

    exe_low = (detected.get("exe") or "").lower()
    detected["known"] = exe_low in KNOWN_GAME_EXES
    detected["mode_hint"] = AUTO_CLASSIFY.get(exe_low)
    return detected


# ---------------------------------------------------------------------------
# STEAM
# ---------------------------------------------------------------------------

def _find_steam_root() -> Optional[str]:
    # Prefer HKCU (per-user install) over HKLM
    p = _read_registry_value("HKCU", r"SOFTWARE\Valve\Steam", "SteamPath")
    if p and os.path.isdir(p):
        return p.replace("/", "\\")
    p = _read_registry_value("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")
    if p and os.path.isdir(p):
        return p
    # Fallback: default install path
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(guess):
            return guess
    return None


def _parse_libraryfolders_vdf(path: str) -> list[str]:
    """libraryfolders.vdf has a key/value structure.  We only need the
    "path" entries.  This is a tiny ad-hoc parser that doesn't need a full
    VDF library."""
    libs: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        p = m.group(1).replace("\\\\", "\\")
        if os.path.isdir(p):
            libs.append(p)
    return libs


def _parse_appmanifest(path: str) -> Optional[dict]:
    """Return (appid, name, installdir) from a single appmanifest_*.acf file."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    m_appid = re.search(r'"appid"\s*"(\d+)"', text)
    m_name = re.search(r'"name"\s*"([^"]+)"', text)
    m_dir = re.search(r'"installdir"\s*"([^"]+)"', text)
    if not (m_name and m_dir):
        return None
    return {
        "appid": m_appid.group(1) if m_appid else "",
        "name": m_name.group(1),
        "installdir": m_dir.group(1),
    }


def scan_steam() -> list[dict]:
    out: list[dict] = []
    steam_root = _find_steam_root()
    if not steam_root:
        return out

    primary_lib = os.path.join(steam_root, "steamapps")
    libs = [primary_lib]
    libfolders = os.path.join(primary_lib, "libraryfolders.vdf")
    if os.path.isfile(libfolders):
        for extra in _parse_libraryfolders_vdf(libfolders):
            extra_steamapps = os.path.join(extra, "steamapps")
            if os.path.isdir(extra_steamapps) and extra_steamapps not in libs:
                libs.append(extra_steamapps)

    for lib in libs:
        for manifest in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            info = _parse_appmanifest(manifest)
            if not info:
                continue
            # Filter out Steamworks Common Redistributables (appid 228980) and tools
            if info["appid"] in ("228980", "250820", "346110", "365670"):
                continue
            install_path = os.path.join(lib, "common", info["installdir"])
            if not os.path.isdir(install_path):
                continue
            best_exe = _pick_best_exe(install_path, title_hint=info["name"])
            exe_name = os.path.basename(best_exe) if best_exe else ""
            out.append(_enrich({
                "title": info["name"],
                "exe": exe_name.lower(),
                "exe_path": best_exe or "",
                "launcher": "Steam",
                "install_path": install_path,
                "appid": info["appid"],
            }))
    log.info(f"steam scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# EPIC
# ---------------------------------------------------------------------------

def scan_epic() -> list[dict]:
    out: list[dict] = []
    pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    manifest_dir = os.path.join(pd, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if not os.path.isdir(manifest_dir):
        return out
    for f in glob.glob(os.path.join(manifest_dir, "*.item")):
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        title = data.get("DisplayName") or ""
        install_path = data.get("InstallLocation") or ""
        launch_exe = data.get("LaunchExecutable") or ""
        if not (title and install_path):
            continue
        # LaunchExecutable is relative to InstallLocation
        full_exe = os.path.join(install_path, launch_exe) if launch_exe else ""
        if not (full_exe and os.path.isfile(full_exe)):
            full_exe = _pick_best_exe(install_path, title_hint=title) or ""
        exe_name = os.path.basename(full_exe).lower() if full_exe else ""
        out.append(_enrich({
            "title": title,
            "exe": exe_name,
            "exe_path": full_exe,
            "launcher": "Epic",
            "install_path": install_path,
        }))
    log.info(f"epic scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# RIOT
# ---------------------------------------------------------------------------

def scan_riot() -> list[dict]:
    out: list[dict] = []
    candidates = [
        ("League of Legends", r"C:\Riot Games\League of Legends", "leagueclient.exe"),
        ("VALORANT",          r"C:\Riot Games\VALORANT\live",     "valorant.exe"),
    ]
    for title, path, exe in candidates:
        if os.path.isdir(path):
            exe_path = os.path.join(path, exe)
            best = exe_path if os.path.isfile(exe_path) else (_pick_best_exe(path, title_hint=title) or "")
            out.append(_enrich({
                "title": title,
                "exe": (os.path.basename(best) if best else exe).lower(),
                "exe_path": best,
                "launcher": "Riot",
                "install_path": path,
            }))
    log.info(f"riot scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# BATTLE.NET
# ---------------------------------------------------------------------------

# Map of registry product code → display name, for products we know about.
# Battle.net stores per-product InstallPath under
#   HKLM\SOFTWARE\WOW6432Node\Blizzard Entertainment\<product>
_BLIZZ_PRODUCTS = {
    "World of Warcraft":    "World of Warcraft",
    "Diablo IV":            "Diablo IV",
    "Diablo III":           "Diablo III",
    "Diablo II Resurrected":"Diablo II Resurrected",
    "Hearthstone":          "Hearthstone",
    "Heroes of the Storm":  "Heroes of the Storm",
    "Overwatch":            "Overwatch 2",
    "StarCraft II":         "StarCraft II",
    "StarCraft":            "StarCraft Remastered",
    "Warcraft III":         "Warcraft III: Reforged",
    "Call of Duty Modern Warfare": "Call of Duty: Modern Warfare",
    "Call of Duty BOCW":    "Call of Duty: Black Ops Cold War",
}


def scan_battlenet() -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\WOW6432Node\Blizzard Entertainment"
    products = _enum_registry_subkeys("HKLM", base)
    if not products:
        # fallback: also check 64-bit hive
        products = _enum_registry_subkeys("HKLM", r"SOFTWARE\Blizzard Entertainment")
    for prod in products:
        # Try common value names
        path = (_read_registry_value("HKLM", f"{base}\\{prod}", "InstallPath")
                or _read_registry_value("HKLM", f"{base}\\{prod}", "GamePath")
                or _read_registry_value("HKLM", f"SOFTWARE\\Blizzard Entertainment\\{prod}", "InstallPath"))
        if not (path and os.path.isdir(path)):
            continue
        title = _BLIZZ_PRODUCTS.get(prod, prod)
        best = _pick_best_exe(path, title_hint=title) or ""
        out.append(_enrich({
            "title": title,
            "exe": (os.path.basename(best) if best else "").lower(),
            "exe_path": best,
            "launcher": "Battle.net",
            "install_path": path,
        }))
    log.info(f"battle.net scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# GOG GALAXY
# ---------------------------------------------------------------------------

def scan_gog() -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\WOW6432Node\GOG.com\Games"
    ids = _enum_registry_subkeys("HKLM", base)
    if not ids:
        ids = _enum_registry_subkeys("HKLM", r"SOFTWARE\GOG.com\Games")
        base = r"SOFTWARE\GOG.com\Games"
    for gid in ids:
        path = _read_registry_value("HKLM", f"{base}\\{gid}", "path")
        name = _read_registry_value("HKLM", f"{base}\\{gid}", "gameName") or ""
        exe = _read_registry_value("HKLM", f"{base}\\{gid}", "exe") or ""
        if not (path and os.path.isdir(path)):
            continue
        full_exe = os.path.join(path, exe) if exe else ""
        if not (full_exe and os.path.isfile(full_exe)):
            full_exe = _pick_best_exe(path, title_hint=name or gid) or ""
        out.append(_enrich({
            "title": name or gid,
            "exe": (os.path.basename(full_exe) if full_exe else "").lower(),
            "exe_path": full_exe,
            "launcher": "GOG",
            "install_path": path,
        }))
    log.info(f"gog scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# UBISOFT CONNECT
# ---------------------------------------------------------------------------

def scan_ubisoft() -> list[dict]:
    out: list[dict] = []
    base = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    ids = _enum_registry_subkeys("HKLM", base)
    if not ids:
        return out
    for gid in ids:
        path = _read_registry_value("HKLM", f"{base}\\{gid}", "InstallDir")
        if not (path and os.path.isdir(path)):
            continue
        # Use install dir basename as fallback title
        title = os.path.basename(path.rstrip("\\")) or f"Ubisoft Game {gid}"
        best = _pick_best_exe(path, title_hint=title) or ""
        out.append(_enrich({
            "title": title,
            "exe": (os.path.basename(best) if best else "").lower(),
            "exe_path": best,
            "launcher": "Ubisoft",
            "install_path": path,
        }))
    log.info(f"ubisoft scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# EA APP / ORIGIN
# ---------------------------------------------------------------------------

def scan_ea() -> list[dict]:
    out: list[dict] = []
    seen_paths = set()
    # Default install roots
    candidates = [
        r"C:\Program Files\EA Games",
        r"C:\Program Files (x86)\Origin Games",
        r"C:\Program Files\Origin Games",
        r"C:\Program Files (x86)\EA Games",
    ]
    for root in candidates:
        if not os.path.isdir(root):
            continue
        for sub in _safe_listdir(root):
            game_dir = os.path.join(root, sub)
            if not os.path.isdir(game_dir) or game_dir in seen_paths:
                continue
            seen_paths.add(game_dir)
            best = _pick_best_exe(game_dir, title_hint=sub) or ""
            if not best:
                continue
            out.append(_enrich({
                "title": sub,
                "exe": os.path.basename(best).lower(),
                "exe_path": best,
                "launcher": "EA",
                "install_path": game_dir,
            }))
    log.info(f"ea scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# MICROSOFT STORE / XBOX
# ---------------------------------------------------------------------------

def scan_xbox() -> list[dict]:
    out: list[dict] = []
    candidates = [r"C:\XboxGames", r"D:\XboxGames", r"E:\XboxGames"]
    for root in candidates:
        if not os.path.isdir(root):
            continue
        for sub in _safe_listdir(root):
            game_dir = os.path.join(root, sub)
            if not os.path.isdir(game_dir):
                continue
            content = os.path.join(game_dir, "Content")
            search_in = content if os.path.isdir(content) else game_dir
            best = _pick_best_exe(search_in, title_hint=sub) or ""
            if not best:
                continue
            out.append(_enrich({
                "title": sub,
                "exe": os.path.basename(best).lower(),
                "exe_path": best,
                "launcher": "Xbox",
                "install_path": game_dir,
            }))
    log.info(f"xbox scan: {len(out)} titles")
    return out


# ---------------------------------------------------------------------------
# AGGREGATE
# ---------------------------------------------------------------------------

def _dedupe(games: list[dict]) -> list[dict]:
    """Same exe via two launchers (rare but happens — Cyberpunk on GOG +
    Steam) → keep first occurrence."""
    seen: set[str] = set()
    out: list[dict] = []
    for g in games:
        key = (g.get("exe") or "") + "|" + (g.get("install_path") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


def scan_all(force: bool = False) -> dict:
    """Run every scanner and return the merged result.  If force=False and
    a recent cache exists (< 6 hours), return the cache instead."""
    cached = _load_cache()
    if not force and cached and (time.time() - cached.get("scanned_at", 0)) < 6 * 3600:
        log.info("library scan: returning cached result")
        return cached

    log.info("library scan: starting full scan")
    games: list[dict] = []
    for name, scanner in (
        ("steam", scan_steam),
        ("epic", scan_epic),
        ("riot", scan_riot),
        ("battlenet", scan_battlenet),
        ("gog", scan_gog),
        ("ubisoft", scan_ubisoft),
        ("ea", scan_ea),
        ("xbox", scan_xbox),
    ):
        try:
            games.extend(scanner())
        except Exception as e:  # noqa: BLE001
            log.warning(f"{name} scanner failed: {e}")

    games = _dedupe(games)
    games.sort(key=lambda g: ((not g.get("known", False)), g.get("title", "").lower()))

    result = {
        "games": games,
        "scanned_at": time.time(),
        "total": len(games),
        "known": sum(1 for g in games if g.get("known")),
    }
    _save_cache(result)
    log.info(f"library scan: {result['total']} total / {result['known']} known")
    return result


def get_cached() -> dict:
    cached = _load_cache()
    if cached:
        return cached
    return {"games": [], "scanned_at": 0, "total": 0, "known": 0}


def get_known_only() -> list[dict]:
    """Filter to titles GhostShell already recognizes (cleaner UI)."""
    cached = get_cached()
    return [g for g in cached.get("games", []) if g.get("known")]


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[dict]:
    if not os.path.isfile(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_cache(data: dict):
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.warning(f"failed to write library cache: {e}")
