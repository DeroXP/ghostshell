"""Temp file cleaner, RAM cleaner, and disk junk cleaner."""
import os
import json
import shutil
from core.utils import run_ps, run_cmd, get_logger

log = get_logger("cleaner")


def _is_in_pyinstaller_extract(p: str) -> bool:
    """True if any path segment looks like a PyInstaller --onefile
    extraction folder (`_MEI<random>`).

    v3.3.1-beta.4 fix for the "[Errno 2] No such file or directory:
    _MEI234242/base_library.zip" crash users saw when clicking
    Apply on the NVIDIA GPU card.  Walking %TEMP% with rmtree was
    deleting OUR OWN running PyInstaller bundle's bundled Python
    stdlib — the very next lazy import after cleanup blew up.
    Skipping any `_MEI*` segment also protects every OTHER
    PyInstaller-bundled app the user might be running (ShareX,
    OBS portable, etc.).
    """
    try:
        for seg in os.path.normpath(p).split(os.sep):
            if seg.startswith("_MEI"):
                return True
    except Exception:
        pass
    return False


def _safe_rmtree(path: str) -> tuple[int, int]:
    files = 0
    size = 0
    if not os.path.isdir(path):
        return 0, 0
    for root, dirs, filenames in os.walk(path, topdown=False):
        for f in filenames:
            fp = os.path.join(root, f)
            # Don't delete files inside any PyInstaller bundle's
            # extracted runtime — corrupts running apps' Python
            # stdlib (see _is_in_pyinstaller_extract).
            if _is_in_pyinstaller_extract(fp):
                continue
            try:
                s = os.path.getsize(fp)
                os.remove(fp)
                files += 1
                size += s
            except (PermissionError, OSError):
                pass
        for d in dirs:
            dp = os.path.join(root, d)
            if d.startswith("_MEI") or _is_in_pyinstaller_extract(dp):
                continue
            try:
                os.rmdir(dp)
            except (PermissionError, OSError):
                pass
    return files, size


def _format_size(b: int) -> str:
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"


# ═══════════════════════════════════════════════════════════════
# Temp File Cleaner (original)
# ═══════════════════════════════════════════════════════════════
def _get_clean_locations() -> list[dict]:
    temp = os.environ.get("TEMP", r"C:\Users\Default\AppData\Local\Temp")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    locations = [
        {"name": "User Temp", "path": temp, "cat": "System"},
        {"name": "Windows Temp", "path": r"C:\Windows\Temp", "cat": "System"},
        {"name": "Prefetch", "path": r"C:\Windows\Prefetch", "cat": "System"},
        {"name": "WU Cache", "path": r"C:\Windows\SoftwareDistribution\Download", "cat": "System"},
        {"name": "Thumbnails", "path": os.path.join(localappdata, "Microsoft", "Windows", "Explorer"), "cat": "System"},
    ]
    # Browser caches
    for name, rel in [
        ("Chrome Cache", os.path.join("Google", "Chrome", "User Data", "Default", "Cache")),
        ("Chrome Code Cache", os.path.join("Google", "Chrome", "User Data", "Default", "Code Cache")),
        ("Edge Cache", os.path.join("Microsoft", "Edge", "User Data", "Default", "Cache")),
    ]:
        p = os.path.join(localappdata, rel)
        if os.path.isdir(p):
            locations.append({"name": name, "path": p, "cat": "Browser"})
    # Firefox
    ff = os.path.join(localappdata, "Mozilla", "Firefox", "Profiles")
    if os.path.isdir(ff):
        for prof in os.listdir(ff):
            cp = os.path.join(ff, prof, "cache2")
            if os.path.isdir(cp):
                locations.append({"name": f"Firefox Cache", "path": cp, "cat": "Browser"})
                break
    return locations


def scan_temp_sizes() -> list[dict]:
    locations = _get_clean_locations()
    results = []
    for loc in locations:
        total = 0; count = 0
        if os.path.isdir(loc["path"]):
            for root, dirs, files in os.walk(loc["path"]):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f)); count += 1
                    except OSError:
                        pass
        results.append({"name": loc["name"], "path": loc["path"], "size": total, "size_str": _format_size(total), "files": count, "category": loc["cat"]})
    return results


def clean_location(path: str) -> dict:
    log.info(f"Cleaning: {path}")
    files, size = _safe_rmtree(path)
    log.info(f"  Removed {files} files ({_format_size(size)})")
    return {"ok": True, "files": files, "freed": size, "freed_str": _format_size(size)}


def clean_all() -> dict:
    log.info("=== STARTING TEMP CLEAN ===")
    locations = _get_clean_locations()
    total_files = 0; total_size = 0; results = []
    for loc in locations:
        r = clean_location(loc["path"])
        total_files += r["files"]; total_size += r["freed"]
        results.append({**loc, **r})
    run_ps('Clear-RecycleBin -Force -ErrorAction SilentlyContinue')
    results.append({"name": "Recycle Bin", "ok": True, "files": 0, "freed": 0})
    log.info(f"=== TEMP CLEAN DONE -- {total_files} files, {_format_size(total_size)} freed ===")
    return {"ok": True, "total_files": total_files, "total_freed": total_size, "total_freed_str": _format_size(total_size), "locations": results}


# ═══════════════════════════════════════════════════════════════
# RAM Cleaner
# ═══════════════════════════════════════════════════════════════
def clean_ram() -> dict:
    log.info("Cleaning RAM...")
    r_before = run_ps("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory")
    free_before = 0
    if r_before["ok"] and r_before["out"]:
        try: free_before = int(r_before["out"].strip())
        except ValueError: pass

    # Flush working sets
    run_ps("""
try {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Diagnostics;
public class WS { [DllImport("kernel32.dll")] public static extern bool SetProcessWorkingSetSize(IntPtr p, int a, int b);
public static int Go() { int c=0; foreach(Process p in Process.GetProcesses()){try{SetProcessWorkingSetSize(p.Handle,-1,-1);c++;}catch{}} return c; }}
'@ -ErrorAction SilentlyContinue; [WS]::Go()
} catch { 0 }
""")
    # Clear standby
    run_ps("""
try {
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class SB { [DllImport("ntdll.dll")] public static extern uint NtSetSystemInformation(int c, IntPtr i, int l);
public static bool Go() { int v=4; IntPtr p=Marshal.AllocHGlobal(4); Marshal.WriteInt32(p,v); uint r=NtSetSystemInformation(80,p,4); Marshal.FreeHGlobal(p); return r==0; }}
'@ -ErrorAction SilentlyContinue; [SB]::Go()
} catch { $false }
""")
    run_ps("[GC]::Collect(); [GC]::WaitForPendingFinalizers(); [GC]::Collect()")

    r_after = run_ps("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory")
    free_after = 0
    if r_after["ok"] and r_after["out"]:
        try: free_after = int(r_after["out"].strip())
        except ValueError: pass
    freed_kb = max(0, free_after - free_before)
    freed = freed_kb * 1024
    log.info(f"=== RAM CLEAN DONE -- freed ~{_format_size(freed)} ===")
    return {"ok": True, "freed": freed, "freed_str": _format_size(freed), "free_before_mb": round(free_before/1024), "free_after_mb": round(free_after/1024), "results": []}


# ═══════════════════════════════════════════════════════════════
# Disk Junk Cleaner — multi-disk, safe, lightweight
# ═══════════════════════════════════════════════════════════════
# NEVER delete these extensions
_PROTECTED_EXT = {
    ".doc",".docx",".xls",".xlsx",".ppt",".pptx",".pdf",".txt",".rtf",".odt",".csv",
    ".jpg",".jpeg",".png",".gif",".bmp",".tiff",".webp",".svg",".raw",".heic",".ico",
    ".mp4",".avi",".mkv",".mov",".wmv",".flv",".webm",".m4v",".mpg",".mpeg",
    ".mp3",".wav",".flac",".aac",".ogg",".wma",".m4a",".opus",
    ".zip",".rar",".7z",".tar",".gz",".bz2",".xz",
    ".exe",".msi",".iso",".dll",".sys",".inf",".cat",
    ".py",".js",".ts",".html",".css",".java",".cpp",".c",".h",".cs",".go",".rs",".rb",".php",".swift",".kt",
    ".psd",".ai",".indd",".sketch",".fig",".blend",
    ".db",".sqlite",".sql",".mdb",".accdb",
    ".json",".xml",".yaml",".yml",".ini",".cfg",".conf",".reg",
    ".ttf",".otf",".woff",".woff2",
    ".lnk",".url",
    ".sav",".save",  # game saves
}

# Directories NEVER to enter (case-insensitive)
_NEVER_ENTER = {
    "documents","desktop","pictures","videos","music","downloads",
    "onedrive","dropbox","google drive","icloud drive",
    "program files","program files (x86)","windows","programdata",
    "users","$recycle.bin","system volume information","recovery",
    ".git",".svn","node_modules",".venv","venv","__pycache__",
    "appdata",  # we handle specific appdata subdirs ourselves
}

# Junk extensions we actively look for
_JUNK_EXT = {
    ".tmp",".temp",".log",".old",".bak",".dmp",".etl",".chk",
    ".gid",".wer",".nfo",".err",".crash",".mdmp",".hdmp",
    ".~tmp",".~lock",
}

# Junk filenames
_JUNK_FILES = {"thumbs.db","desktop.ini","debug.log","error.log"}

# Directory names at a drive root that are safe to wipe WHOLE.  3.4.2:
# exact-match only.  The old code substring-matched ("temp"/"cache"/etc
# in the name) and would _safe_rmtree any folder like D:\MyGameTempSaves
# or D:\cache_of_important_stuff.  A folder literally NAMED one of these
# is junk; a folder that merely CONTAINS the word is the user's data.
_JUNK_DIR_NAMES = {
    "temp", "tmp", "cache", "crashdumps", "crash dumps",
    ".cache", "logs", "log",
}


def get_disk_list() -> list[dict]:
    r = run_ps("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID,Size,FreeSpace,VolumeName | ConvertTo-Json")
    drives = []
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list): data = [data]
            for drv in data:
                t = drv.get("Size",0) or 0; f = drv.get("FreeSpace",0) or 0
                drives.append({"letter": drv.get("DeviceID","?"), "name": drv.get("VolumeName","") or "", "total_gb": round(t/(1024**3),1), "free_gb": round(f/(1024**3),1), "used_pct": round((1-f/t)*100) if t else 0})
        except Exception: pass
    return drives


def scan_disk_junk(drive_letter: str) -> list[dict]:
    """Scan a specific drive for junk. Returns categorized results."""
    log.info(f"Scanning {drive_letter} for junk...")
    categories = {}

    def _add(cat, size):
        if cat not in categories:
            categories[cat] = {"files": 0, "size": 0}
        categories[cat]["files"] += 1
        categories[cat]["size"] += size

    # -- Strategy 1: Scan well-known junk locations for this drive --
    is_c = drive_letter.upper().startswith("C")
    root = drive_letter + "\\"

    if is_c:
        _scan_known_junk_dirs(categories)

    # -- Strategy 2: Walk the drive root (shallow, 2 levels max) for junk files --
    try:
        for entry in os.scandir(root):
            try:
                if entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in _JUNK_EXT or entry.name.lower() in _JUNK_FILES:
                        _add("Root junk files", entry.stat().st_size)
                elif entry.is_dir(follow_symlinks=False):
                    dn = entry.name.lower()
                    if dn in _NEVER_ENTER:
                        continue
                    # Check if it's a temp/cache directory
                    if any(k in dn for k in ("temp","tmp","cache","log","crash")):
                        s = _measure_dir(entry.path)
                        if s > 0:
                            _add(f"Temp dir: {entry.name}", s)
                    else:
                        # Shallow scan (1 more level) for junk files in non-protected dirs
                        _scan_shallow_junk(entry.path, categories, entry.name)
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass

    results = []
    for cat, info in categories.items():
        if info["size"] > 0:
            results.append({"name": cat, "files": info["files"], "size": info["size"], "size_str": _format_size(info["size"])})
    results.sort(key=lambda x: x["size"], reverse=True)
    return results


def _scan_known_junk_dirs(categories):
    """Scan well-known Windows junk/cache directories on C: drive."""
    localappdata = os.environ.get("LOCALAPPDATA", "")
    temp = os.environ.get("TEMP", "")
    appdata = os.environ.get("APPDATA", "")

    known = [
        ("User Temp", temp),
        ("Windows Temp", r"C:\Windows\Temp"),
        ("Prefetch", r"C:\Windows\Prefetch"),
        ("WU Downloads", r"C:\Windows\SoftwareDistribution\Download"),
        ("Crash Dumps", os.path.join(localappdata, "CrashDumps") if localappdata else ""),
        ("Internet Cache", os.path.join(localappdata, "Microsoft", "Windows", "INetCache") if localappdata else ""),
        ("Thumbnails", os.path.join(localappdata, "Microsoft", "Windows", "Explorer") if localappdata else ""),
        ("Installer Cache", r"C:\Windows\Installer\$PatchCache$"),
        ("Font Cache", os.path.join(localappdata, "Microsoft", "Windows", "Fonts") if localappdata else ""),
        ("D3D Shader Cache", os.path.join(localappdata, "D3DSCache") if localappdata else ""),
        ("NVIDIA Shader Cache", os.path.join(localappdata, "NVIDIA", "DXCache") if localappdata else ""),
        ("NVIDIA GL Cache", os.path.join(localappdata, "NVIDIA", "GLCache") if localappdata else ""),
        ("AMD Shader Cache", os.path.join(localappdata, "AMD", "DxCache") if localappdata else ""),
        ("Steam Dumps", os.path.join(os.environ.get("ProgramFiles(x86)",""), "Steam", "dumps")),
        ("Chrome Cache", os.path.join(localappdata, "Google", "Chrome", "User Data", "Default", "Cache") if localappdata else ""),
        ("Chrome Code Cache", os.path.join(localappdata, "Google", "Chrome", "User Data", "Default", "Code Cache") if localappdata else ""),
        ("Edge Cache", os.path.join(localappdata, "Microsoft", "Edge", "User Data", "Default", "Cache") if localappdata else ""),
        ("Discord Cache", os.path.join(appdata, "discord", "Cache") if appdata else ""),
        ("Teams Cache", os.path.join(appdata, "Microsoft", "Teams", "Cache") if appdata else ""),
        ("Spotify Cache", os.path.join(localappdata, "Spotify", "Storage") if localappdata else ""),
    ]

    for name, path in known:
        if path and os.path.isdir(path):
            s = _measure_dir(path)
            if s > 0:
                if name not in categories:
                    categories[name] = {"files": 0, "size": 0}
                fc = _count_files(path)
                categories[name]["files"] += fc
                categories[name]["size"] += s


def _scan_shallow_junk(dir_path, categories, parent_name):
    """Scan one level deep inside a directory for junk files only."""
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file(follow_symlinks=False):
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in _JUNK_EXT or entry.name.lower() in _JUNK_FILES:
                    cat = f"Junk in {parent_name}"
                    if cat not in categories:
                        categories[cat] = {"files": 0, "size": 0}
                    try:
                        categories[cat]["files"] += 1
                        categories[cat]["size"] += entry.stat().st_size
                    except OSError:
                        pass
    except (PermissionError, OSError):
        pass


def _measure_dir(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try: total += os.path.getsize(os.path.join(root, f))
                except OSError: pass
    except (PermissionError, OSError): pass
    return total


def _count_files(path):
    count = 0
    try:
        for root, dirs, files in os.walk(path):
            count += len(files)
    except (PermissionError, OSError): pass
    return count


def clean_disk_junk(drive_letters: list[str]) -> dict:
    """Clean junk from selected drives."""
    log.info(f"=== DISK CLEAN: {', '.join(drive_letters)} ===")
    total_files = 0; total_size = 0; results = []

    for letter in drive_letters:
        df = 0; ds = 0
        is_c = letter.upper().startswith("C")

        if is_c:
            df, ds = _clean_known_junk_dirs()

        # Clean junk files at drive root + shallow subdirs
        root = letter + "\\"
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in _JUNK_EXT or entry.name.lower() in _JUNK_FILES:
                            s = entry.stat().st_size
                            os.remove(entry.path); df += 1; ds += s
                    elif entry.is_dir(follow_symlinks=False):
                        dn = entry.name.lower()
                        if dn in _NEVER_ENTER: continue
                        # 3.4.2: EXACT name match (was substring) — only
                        # wipe a folder literally named temp/cache/etc,
                        # not any folder containing the word.  Everything
                        # else gets shallow junk-file cleanup, which only
                        # removes individual _JUNK_EXT files, never whole
                        # user folders.
                        if dn in _JUNK_DIR_NAMES:
                            f2, s2 = _safe_rmtree(entry.path); df += f2; ds += s2
                        else:
                            f3, s3 = _clean_shallow_junk(entry.path); df += f3; ds += s3
                except (PermissionError, OSError): pass
        except (PermissionError, OSError): pass

        total_files += df; total_size += ds
        results.append({"drive": letter, "files": df, "freed": ds, "freed_str": _format_size(ds)})
        log.info(f"  {letter}: {df} files, {_format_size(ds)}")

    log.info(f"=== DISK CLEAN DONE -- {total_files} files, {_format_size(total_size)} freed ===")
    return {"ok": True, "total_files": total_files, "total_freed": total_size, "total_freed_str": _format_size(total_size), "drives": results}


def _clean_known_junk_dirs() -> tuple[int, int]:
    """Clean well-known junk dirs on C:. Returns (files, bytes)."""
    total_f = 0; total_s = 0
    localappdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    temp = os.environ.get("TEMP", "")

    paths = [
        temp, r"C:\Windows\Temp", r"C:\Windows\Prefetch",
        r"C:\Windows\SoftwareDistribution\Download",
        os.path.join(localappdata, "CrashDumps") if localappdata else "",
        os.path.join(localappdata, "Microsoft", "Windows", "INetCache") if localappdata else "",
        os.path.join(localappdata, "D3DSCache") if localappdata else "",
        os.path.join(localappdata, "NVIDIA", "DXCache") if localappdata else "",
        os.path.join(localappdata, "NVIDIA", "GLCache") if localappdata else "",
        os.path.join(localappdata, "AMD", "DxCache") if localappdata else "",
    ]
    for p in paths:
        if p and os.path.isdir(p):
            f, s = _safe_rmtree(p)
            total_f += f; total_s += s
    return total_f, total_s


def _clean_shallow_junk(dir_path) -> tuple[int, int]:
    """Remove only junk-extension files from a directory (no recursion)."""
    f = 0; s = 0
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file(follow_symlinks=False):
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in _JUNK_EXT or entry.name.lower() in _JUNK_FILES:
                    try:
                        sz = entry.stat().st_size; os.remove(entry.path); f += 1; s += sz
                    except (PermissionError, OSError): pass
    except (PermissionError, OSError): pass
    return f, s
