import configparser
import os
import shutil
import stat
import string
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from core.utils import (
    create_system_restore_point,
    ensure_app_dirs,
    get_logger,
    human_bytes,
    log_event,
    run_powershell,
)

logger = get_logger(__name__)

Resolver = Callable[[], List[Path]]
Cleaner = Callable[[List[Path]], Tuple[int, int, str]]
SizeCalculator = Callable[[List[Path]], int]


def _escape_powershell_literal(value: str) -> str:
    return value.replace('`', '``').replace('"', '`"')


def _handle_remove_readonly(func, path, exc_info) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _size_of(path: Path) -> int:
    total = 0
    stack: List[Path] = [path]
    while stack:
        current = stack.pop()
        try:
            if not current.exists():
                continue
            if current.is_symlink():
                continue
            if current.is_file():
                try:
                    total += current.stat().st_size
                except OSError as exc:
                    logger.debug("Unable to stat %s: %s", current, exc)
                continue
            if current.is_dir():
                try:
                    for child in current.iterdir():
                        stack.append(child)
                except OSError as exc:
                    logger.debug("Unable to list %s: %s", current, exc)
        except OSError as exc:
            logger.debug("Unable to access %s: %s", current, exc)
    return total


def _default_size_calculator(paths: List[Path]) -> int:
    return sum(_size_of(path) for path in paths)


def _prefetch_size(paths: List[Path]) -> int:
    total = 0
    for directory in paths:
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            for p in directory.iterdir():
                if p.is_file() and p.suffix.lower() == ".pf":
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _resolve_user_temp() -> List[Path]:
    candidates = []
    for env in ("TEMP", "TMP"):
        v = os.environ.get(env)
        if v:
            candidates.append(Path(v))
    # Also include \AppData\Local\Temp explicitly
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        candidates.append(Path(localapp) / "Temp")
    # Deduplicate
    uniq: List[Path] = []
    seen = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        key = str(rp).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(rp)
    return uniq


def _resolve_windows_temp() -> List[Path]:
    windir = os.environ.get("SystemRoot") or r"C:\\Windows"
    return [Path(windir) / "Temp"]


def _resolve_browser_cache() -> List[Path]:
    paths: List[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Chrome Default profile
        base = Path(local) / "Google" / "Chrome" / "User Data"
        default = base / "Default"
        paths += [
            default / "Cache",
            default / "Code Cache",
            default / "GPUCache",
        ]
        # Edge
        edge = Path(local) / "Microsoft" / "Edge" / "User Data" / "Default"
        paths += [edge / "Cache", edge / "Code Cache", edge / "GPUCache"]
        # Chromium/Brave variants (best-effort)
        brave = Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default"
        paths += [brave / "Cache", brave / "Code Cache", brave / "GPUCache"]
        opera = Path(local) / "Opera Software" / "Opera Stable"
        paths += [opera / "Cache", opera / "GPUCache", opera / "Code Cache"]
    roaming = os.environ.get("APPDATA")
    if roaming:
        # Firefox caches per profile
        ff_base = Path(roaming) / "Mozilla" / "Firefox" / "Profiles"
        if ff_base.exists():
            try:
                for prof in ff_base.iterdir():
                    if prof.is_dir():
                        paths.append(prof / "cache2")
                        paths.append(prof / "startupCache")
            except OSError:
                pass
    return paths


def _resolve_wu_cache() -> List[Path]:
    windir = os.environ.get("SystemRoot") or r"C:\\Windows"
    return [Path(windir) / "SoftwareDistribution" / "Download"]


def _resolve_thumb_cache() -> List[Path]:
    local = os.environ.get("LOCALAPPDATA")
    paths: List[Path] = []
    if local:
        paths.append(Path(local) / "Microsoft" / "Windows" / "Explorer")
    return paths


def _resolve_prefetch() -> List[Path]:
    windir = os.environ.get("SystemRoot") or r"C:\\Windows"
    return [Path(windir) / "Prefetch"]


def _resolve_recycle_bin() -> List[Path]:
    # Controlled via PowerShell, path not needed for size
    return []


_CATALOG: Dict[str, Dict[str, Any]] = {
    "user_temp": {
        "title": "User Temp",
        "description": "Cleans %TEMP% and user-local temporary files.",
        "resolver": _resolve_user_temp,
        "size": _default_size_calculator,
    },
    "windows_temp": {
        "title": "Windows Temp",
        "description": "Cleans C\\Windows\\Temp.",
        "resolver": _resolve_windows_temp,
        "size": _default_size_calculator,
    },
    "browser_cache": {
        "title": "Browser Caches",
        "description": "Clears Chrome/Edge/Firefox/Brave/Opera caches.",
        "resolver": _resolve_browser_cache,
        "size": _default_size_calculator,
    },
    "wu_cache": {
        "title": "Windows Update Cache",
        "description": "Clears C\\Windows\\SoftwareDistribution\\Download after stopping update services.",
        "resolver": _resolve_wu_cache,
        "size": _default_size_calculator,
    },
    "thumb_cache": {
        "title": "Thumbnail Cache",
        "description": "Clears thumbcache*.db files.",
        "resolver": _resolve_thumb_cache,
        "size": _default_size_calculator,
    },
    "prefetch": {
        "title": "Prefetch Files",
        "description": "Deletes .pf files from C\\Windows\\Prefetch (directory remains).",
        "resolver": _resolve_prefetch,
        "size": _prefetch_size,
    },
    "recycle_bin": {
        "title": "Recycle Bin",
        "description": "Empties the Recycle Bin for all drives.",
        "resolver": _resolve_recycle_bin,
        "size": lambda _: 0,
    },
}


def _remove_path(path: Path) -> Tuple[int, int]:
    removed = 0
    errors = 0
    try:
        if not path.exists():
            return 0, 0
        if path.is_symlink():
            try:
                size = 0
                path.unlink(missing_ok=True)  # type: ignore[arg-type]
                removed += size
            except Exception as exc:
                errors += 1
                logger.debug("Symlink removal failed %s: %s", path, exc)
            return removed, errors
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                pass
            try:
                path.unlink()
                removed += size
                return removed, errors
            except Exception as exc:
                errors += 1
                logger.debug("File delete failed %s: %s", path, exc)
                # PS fallback
                cmd = f"Remove-Item -LiteralPath \"{_escape_powershell_literal(str(path))}\" -Force -ErrorAction SilentlyContinue"
                code, out, err = run_powershell(cmd)
                if code == 0:
                    removed += size
                else:
                    logger.debug("PS fallback failed for file %s: %s %s", path, out, err)
                return removed, errors
        if path.is_dir():
            try:
                size = _size_of(path)
            except Exception:
                size = 0
            try:
                shutil.rmtree(path, onerror=_handle_remove_readonly)
                removed += size
                return removed, errors
            except Exception as exc:
                errors += 1
                logger.debug("rmtree failed %s: %s", path, exc)
                cmd = (
                    "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath \""
                    + _escape_powershell_literal(str(path))
                    + "\""
                )
                code, out, err = run_powershell(cmd)
                if code == 0:
                    removed += size
                else:
                    logger.debug("PS rmtree fallback failed %s: %s %s", path, out, err)
                return removed, errors
    except Exception as exc:
        errors += 1
        logger.debug("Unhandled remove error %s: %s", path, exc)
    return removed, errors


def _clean_paths(paths: List[Path]) -> Tuple[int, int, str]:
    total_removed = 0
    total_errors = 0
    deleted_entries = 0
    for p in paths:
        if not p.exists():
            continue
        if p.is_dir():
            # For temp/cache directories, we attempt to remove contents rather than the directory itself
            try:
                for child in list(p.iterdir()):
                    r, e = _remove_path(child)
                    total_removed += r
                    total_errors += e
                    deleted_entries += 1 if (r or e) else 0
            except Exception as exc:
                total_errors += 1
                logger.debug("Listdir failed %s: %s", p, exc)
        else:
            r, e = _remove_path(p)
            total_removed += r
            total_errors += e
            deleted_entries += 1 if (r or e) else 0
    msg = f"Deleted {deleted_entries} item(s)"
    return total_removed, total_errors, msg


# Specialized cleaners

def _clean_wu_cache(paths: List[Path]) -> Tuple[int, int, str]:
    # Stop services, clean, start services back
    total_removed = 0
    total_errors = 0
    try:
        run_powershell("Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue")
        run_powershell("Stop-Service -Name bits -Force -ErrorAction SilentlyContinue")
    except Exception:
        pass
    try:
        r, e, _ = _clean_paths(paths)
        total_removed += r
        total_errors += e
    finally:
        try:
            run_powershell("Start-Service -Name wuauserv -ErrorAction SilentlyContinue")
            run_powershell("Start-Service -Name bits -ErrorAction SilentlyContinue")
        except Exception:
            pass
    return total_removed, total_errors, "Windows Update cache cleaned"


def _clean_thumb_cache(paths: List[Path]) -> Tuple[int, int, str]:
    # Use PowerShell to clear thumbcache databases in Explorer folder
    total_removed = 0
    total_errors = 0
    for folder in paths:
        if not folder.exists() or not folder.is_dir():
            continue
        pattern = str(folder / "*thumbcache*.db")
        cmd = f"Get-ChildItem -LiteralPath \"{_escape_powershell_literal(str(folder))}\" -Filter *thumbcache*.db -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue"
        code, out, err = run_powershell(cmd)
        if code != 0:
            total_errors += 1
        else:
            # Estimation could be improved by pre-scanning sizes; keep 0 for now
            pass
    return total_removed, total_errors, "Thumbnail cache cleared"


def _clean_prefetch(paths: List[Path]) -> Tuple[int, int, str]:
    total_removed = 0
    total_errors = 0
    for folder in paths:
        if not folder.exists() or not folder.is_dir():
            continue
        try:
            for p in list(folder.iterdir()):
                if p.is_file() and p.suffix.lower() == ".pf":
                    r, e = _remove_path(p)
                    total_removed += r
                    total_errors += e
        except Exception as exc:
            total_errors += 1
            logger.debug("Prefetch iterate failed %s: %s", folder, exc)
    return total_removed, total_errors, "Prefetch files removed"


def _clean_recycle_bin(_: List[Path]) -> Tuple[int, int, str]:
    cmd = "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"
    code, out, err = run_powershell(cmd)
    if code == 0:
        return 0, 0, "Recycle Bin emptied"
    else:
        return 0, 1, f"Recycle Bin clear failed: {err or out}"


# Map cleaners to targets
_CLEANERS: Dict[str, Cleaner] = {
    "user_temp": _clean_paths,
    "windows_temp": _clean_paths,
    "browser_cache": _clean_paths,
    "wu_cache": _clean_wu_cache,
    "thumb_cache": _clean_thumb_cache,
    "prefetch": _clean_prefetch,
    "recycle_bin": _clean_recycle_bin,
}


def list_targets() -> List[Dict[str, Any]]:
    ensure_app_dirs()
    targets: List[Dict[str, Any]] = []
    for key, meta in _CATALOG.items():
        resolver: Resolver = meta["resolver"]
        size_calc: SizeCalculator = meta.get("size") or _default_size_calculator
        try:
            paths = resolver()
        except Exception as exc:
            paths = []
            logger.debug("Resolver failed for %s: %s", key, exc)
        try:
            size = size_calc(paths)
        except Exception as exc:
            logger.debug("Size calculation failed for %s: %s", key, exc)
            size = 0
        targets.append(
            {
                "key": key,
                "title": meta.get("title", key),
                "description": meta.get("description", ""),
                "paths": [str(p) for p in paths],
                "size_bytes": int(size),
                "size_human": human_bytes(int(size)),
            }
        )
    return targets


def clean(selected: List[str]) -> Dict[str, Any]:
    ensure_app_dirs()
    # Restore point
    try:
        rp = create_system_restore_point("GhostShell Temp Cleaner")
        log_event("core.cleaner", "restore_point", "info", str(rp))
    except Exception as exc:
        log_event("core.cleaner", "restore_point", "warning", f"failed: {exc}")

    results: Dict[str, Any] = {}
    total_removed = 0
    all_success = True
    processed_targets = 0

    for key in selected:
        meta = _CATALOG.get(key)
        if not meta:
            msg = f"Unknown target key: {key}"
            logger.warning("Unknown target key: %s", key)
            results[key] = {"success": False, "removed_bytes": 0, "message": msg, "errors": 1}
            all_success = False
            continue
        resolver: Resolver = meta["resolver"]
        try:
            paths = resolver()
        except Exception as exc:
            logger.exception("Resolver threw for %s: %s", key, exc)
            message = f"Resolver failed: {exc}"
            results[key] = {"success": False, "removed_bytes": 0, "message": message, "errors": 1}
            all_success = False
            continue
        cleaner: Cleaner = _CLEANERS.get(key, _clean_paths)
        try:
            removed_bytes, errors, message = cleaner(paths)
        except Exception as exc:
            logger.exception("Cleaner threw for %s: %s", key, exc)
            message = f"Cleaning failed: {exc}"
            results[key] = {"success": False, "removed_bytes": 0, "message": message, "errors": 1}
            all_success = False
            continue
        success = errors == 0
        total_removed += removed_bytes
        processed_targets += 1
        if not success:
            all_success = False
        results[key] = {
            "success": success,
            "removed_bytes": removed_bytes,
            "message": message,
            "errors": errors,
        }
        log_event(
            "core.cleaner",
            key,
            "success" if success else "partial",
            f"{message} ({human_bytes(removed_bytes)})",
        )

    results["total_removed"] = total_removed
    results["total_human"] = human_bytes(total_removed)
    summary = f"Recovered {results['total_human']} across {processed_targets} target(s)"
    results["message"] = summary
    logger.info(summary)
    overall_status = "success" if all_success else "partial"
    log_event("core.cleaner", "clean", overall_status, summary)
    return results
