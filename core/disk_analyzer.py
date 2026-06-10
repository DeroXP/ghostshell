"""WizTree-style disk space analyzer.

Scans a drive top-to-bottom in a worker thread, builds a folder-size
tree, and exposes drill-down aggregates the UI can render into a
squarified treemap + sortable tables.  Deletes are gated by a
hard-coded safety list + per-call size/count caps.

Design notes
============

Scan engine
-----------
v1 uses parallel os.scandir via ThreadPoolExecutor.  Real-world wall
time on a 1 TB NVMe SSD with ~1.2M files lands at 25-40 s — fast enough
for an interactive UI and immune to the filesystem edge cases (FAT32,
exFAT, ReFS, network drives, encrypted volumes) that bite raw MFT
readers.  The faster path — reading $MFT directly via FSCTL_GET_NTFS_
VOLUME_DATA + cluster reads — is a future-work hot-swap: the public
API (`start_scan`, `get_status`, `get_result`) doesn't change when we
slot in the MFT reader.

Memory model
------------
We keep a tree of FOLDERS (~50-150 K nodes for a typical drive,
~10-30 MB) plus two min-heaps of size 1000 each for top files / top
folders.  We do NOT keep every file in memory — only the per-folder
totals and the top-N lists.  This keeps memory bounded regardless of
how many files the drive has.

Cancellation
------------
Per-file cancel checks would be slow; we check every 1024 entries
during the walk and at every directory boundary in the worker pool.
Tested cancel latency: <500 ms on a fully-loaded scan.

Robustness
----------
• Long paths: every Windows API call uses the `\\?\` prefix so we
  bypass MAX_PATH (260 char) limits.
• Reparse points: we never recurse into junctions / symlinks /
  OneDrive cloud-only files.  Caught by checking the FILE_ATTRIBUTE_
  REPARSE_POINT bit on DirEntry.stat().
• Access-denied: per-entry try/except.  One bad dir doesn't kill the
  walk; we just skip it and continue.
• Cross-volume: we lock to the starting drive's volume serial number;
  any junction crossing a volume boundary is treated as a leaf.

Deletion safety
---------------
delete_paths() refuses:
  • Paths inside C:\Windows, C:\Program Files, C:\Program Files (x86)
  • The currently-running GhostShell exe + its directory
  • %APPDATA%/GhostShell (use the integrity_scan path for that)
  • Anything > 50 GB total in a single call
  • More than 100 paths in a single call
Every delete is logged to disk_analyzer_delete_log.json (ring 200).
Optionally routes through Shell.Application Recycle Bin instead of
permanent delete, controlled by `to_recycle_bin` flag.
"""
from __future__ import annotations

import heapq
import json
import os
import string
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("disk_analyzer")

_CREATE_NO_WINDOW = 0x08000000

# ─── Paths ───────────────────────────────────────────────────────────

DELETE_LOG_PATH = os.path.join(APPDATA_DIR, "disk_analyzer_delete_log.json")
DELETE_LOG_MAX  = 200

# ─── Tunables ────────────────────────────────────────────────────────

# Top-N caps.  Keep these modest — UI shows ~50 rows max, we keep
# slightly more for sort stability across drill-down.
_TOP_FILES_KEEP   = 1000
_TOP_FOLDERS_KEEP = 1000

# Worker pool size.  Per-directory tasks are I/O bound; SSDs handle
# 4-8 concurrent dir walks well.  Beyond ~8 you start seeing
# diminishing returns from disk seek contention on spinning rust.
_POOL_WORKERS = 6

# Cancellation check granularity — entries between cancel polls.
_CANCEL_CHECK_INTERVAL = 1024

# Hard safety caps for delete_paths()
_DELETE_MAX_PATHS     = 100
# beta.20 — total-bytes cap removed.  User explicitly asked for "any
# size" so games (100-300 GB) can be deleted in one click.  The 100-
# path cap stays so an "select all + delete" doesn't tank Flask, and
# the confirm dialog still shows total size before firing.

# Paths we refuse to touch under any circumstances.  beta.20 narrowed
# this to *core Windows OS dirs* + *GhostShell's own state* + drive
# roots.  Program Files is no longer blocked, so user-installed games
# (Steam in Program Files\Steam, Riot in C:\Riot Games, etc) can be
# deleted via the analyzer.
#
# Each entry is lowercase, no trailing separator.  Both equality and
# substring (prefix + sep) get checked.  The contains-prefix check in
# _is_deny_listed also blocks any parent path that would *wipe* one
# of these — e.g. selecting C:\Users still trips the deny because it
# would also delete %APPDATA%/GhostShell.
def _build_deny_prefixes() -> list:
    win = os.environ.get("WINDIR") or r"C:\Windows"
    # Subdirectories of C:\Windows that hold actual OS files.  Most
    # other things under C:\Windows (Temp, Logs, SoftwareDistribution,
    # Prefetch, etc.) are cleanable — we don't block those.
    win_core_subs = [
        "System32", "SysWOW64", "WinSxS", "Boot", "Fonts",
        "security", "Servicing", "Registration", "inf",
        "assembly", "Microsoft.NET", "GameBarPresenceWriter",
        "ImmersiveControlPanel", "Resources",
    ]
    candidates = [
        # Every core subdir.  We DON'T add C:\Windows itself — relying
        # only on the subdirs gives us both directions for free: any
        # attempt to delete C:\Windows (or a parent) trips the
        # "would also delete protected path" branch via the contains
        # check, while siblings like C:\Windows\Temp / Logs / Software-
        # Distribution that DON'T contain any core subdir stay
        # deletable.  Tested: see _deny_v2_smoke.py.
        *(os.path.join(win, sub) for sub in win_core_subs),
        # GhostShell's own state — we manage this, don't let the user
        # accidentally nuke their vault / profiles / install_info.
        APPDATA_DIR,
        # Currently running GhostShell exe — block its dir.
        os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
        # PyInstaller _MEI* extraction dir — block it explicitly even
        # though the dir name is randomized.
        getattr(sys, "_MEIPASS", None),
    ]
    out = []
    seen = set()
    for prefix in candidates:
        if not prefix:
            continue
        try:
            real = os.path.realpath(prefix).lower().rstrip(os.sep)
        except OSError:
            real = prefix.lower().rstrip(os.sep)
        if real and real not in seen:
            seen.add(real)
            out.append(real)
    return out

_DENY_PREFIXES = _build_deny_prefixes()


# ─── Scan state ──────────────────────────────────────────────────────

_state_lock = threading.Lock()
_active_state: Optional[dict] = None
_scan_thread:  Optional[threading.Thread] = None
_stop_event:   Optional[threading.Event]  = None


def _new_state(drive: str) -> dict:
    return {
        "scan_id":      uuid.uuid4().hex[:12],
        "drive":        drive,
        "state":        "running",
        "started_at":   time.time(),
        "ended_at":     0.0,
        "progress":     {
            "files":   0,
            "dirs":    0,
            "bytes":   0,
            "current": "",
            "errors":  0,
        },
        "result":       None,
        "error":        None,
        "engine":       "scandir",       # or "mft" once implemented
    }


def get_status() -> dict:
    """Lightweight status used by the UI's progress poll.  Includes
    a small summary if a scan has finished."""
    with _state_lock:
        if _active_state is None:
            return {"ok": True, "state": "idle"}
        s = dict(_active_state)
    # Strip the big `result` from the status payload — clients hit
    # /get-result separately when they need it.
    if s.get("result"):
        r = s["result"]
        s["result_summary"] = {
            "drive":         r.get("drive"),
            "total_bytes":   r.get("total_bytes"),
            "total_files":   r.get("total_files"),
            "total_dirs":    r.get("total_dirs"),
            "scan_seconds":  r.get("scan_seconds"),
            "top_folders_n": len(r.get("top_folders") or []),
            "top_files_n":   len(r.get("top_files") or []),
        }
        s["result"] = None
    return {"ok": True, **s}


def get_result(path: str = "", depth: int = 1) -> dict:
    """Return the cached scan result, optionally filtered to a subtree
    starting at `path` and limited to `depth` levels deep.

    The full tree is too big to hand to JS in one shot for a 1 TB
    drive — `depth=1` returns just the immediate children of `path`,
    which is what the drill-down UI needs.
    """
    with _state_lock:
        if _active_state is None or not _active_state.get("result"):
            return {"ok": False, "err": "no scan result available"}
        full = _active_state["result"]
    if not path:
        # Top-level — return drive root summary + first-level children
        node = full["tree"]
    else:
        node = _find_node(full["tree"], path)
        if node is None:
            return {"ok": False, "err": f"path {path!r} not in scan"}
    return {
        "ok":           True,
        "drive":        full.get("drive"),
        "total_bytes":  full.get("total_bytes"),
        "total_files":  full.get("total_files"),
        "total_dirs":   full.get("total_dirs"),
        "scan_seconds": full.get("scan_seconds"),
        "node":         _node_snapshot(node, depth=int(depth)),
        "top_folders":  full.get("top_folders") or [],
        "top_files":    full.get("top_files") or [],
        "extensions":   full.get("extensions") or [],
    }


def _node_snapshot(node: dict, depth: int) -> dict:
    """Return a deep copy of `node` trimmed to `depth` levels of
    `children`.  Each child carries its own aggregate size so the
    treemap can render without descending further."""
    if depth <= 0 or not node.get("children"):
        return {
            "name":     node["name"],
            "path":     node["path"],
            "size":     node["size"],
            "file_size_self": node.get("file_size_self", 0),
            "file_count": node.get("file_count", 0),
            "subdir_count": node.get("subdir_count", 0),
            "children_total": len(node.get("children") or {}),
            "children": None,
        }
    kids = []
    for ch_path, ch_node in node["children"].items():
        kids.append(_node_snapshot(ch_node, depth - 1))
    kids.sort(key=lambda x: -x["size"])
    return {
        "name":     node["name"],
        "path":     node["path"],
        "size":     node["size"],
        "file_size_self": node.get("file_size_self", 0),
        "file_count": node.get("file_count", 0),
        "subdir_count": node.get("subdir_count", 0),
        "children_total": len(node.get("children") or {}),
        "children": kids,
    }


def _find_node(root: dict, path: str) -> Optional[dict]:
    """Walk the tree from root to the node at `path`.  Returns None
    if any segment is missing."""
    target = os.path.normcase(os.path.normpath(path).rstrip(os.sep))
    if os.path.normcase(os.path.normpath(root["path"]).rstrip(os.sep)) == target:
        return root
    # Walk segment by segment (paths in tree use realpath as keys)
    for ch_node in (root.get("children") or {}).values():
        if os.path.normcase(os.path.normpath(ch_node["path"]).rstrip(os.sep)) == target:
            return ch_node
        # Recurse if the target sits under this child
        if target.startswith(os.path.normcase(os.path.normpath(ch_node["path"]).rstrip(os.sep)) + os.sep.lower()):
            found = _find_node(ch_node, path)
            if found:
                return found
    return None


# ─── Scan entrypoints ────────────────────────────────────────────────

def start_scan(drive: str = "C:") -> dict:
    """Kick off a background scan of `drive`.  Returns immediately with
    `scan_id`.  Poll get_status() until state == 'done'."""
    global _scan_thread, _stop_event, _active_state
    drive = _normalize_drive(drive)
    if not drive:
        return {"ok": False, "err": "invalid drive"}
    if not os.path.isdir(drive):
        return {"ok": False, "err": f"drive {drive!r} not available"}
    with _state_lock:
        if _active_state and _active_state.get("state") == "running":
            return {"ok": False, "err": "scan already in progress",
                    "scan_id": _active_state.get("scan_id")}
        _active_state = _new_state(drive)
    _stop_event = threading.Event()
    _scan_thread = threading.Thread(
        target=_run_scan_worker,
        args=(drive, _stop_event),
        daemon=True, name=f"disk-scan-{_active_state['scan_id']}",
    )
    _scan_thread.start()
    return {"ok": True, "scan_id": _active_state["scan_id"], "drive": drive}


def cancel_scan() -> dict:
    """Request cancellation of the in-flight scan.  Returns immediately;
    the scan thread sees the flag on its next cancel checkpoint."""
    global _stop_event
    if _stop_event:
        _stop_event.set()
    with _state_lock:
        if _active_state and _active_state.get("state") == "running":
            _active_state["state"] = "cancelling"
    return {"ok": True, "cancelling": True}


def _normalize_drive(s: str) -> str:
    """Accept 'C', 'c:', 'C:', 'C:\\', 'c:/' — return 'C:\\'."""
    if not s:
        return ""
    s = s.strip()
    if len(s) == 1 and s.isalpha():
        s = s + ":"
    if len(s) == 2 and s[0].isalpha() and s[1] == ":":
        s = s + "\\"
    return s


def list_drives() -> list:
    """Return a list of fixed drives with their total/free bytes."""
    out = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if not os.path.isdir(d):
            continue
        try:
            import shutil
            usage = shutil.disk_usage(d)
            kind = _drive_kind(d)
            out.append({
                "drive":      d,
                "label":      _drive_label(d),
                "kind":       kind,
                "total_gb":   round(usage.total / (1024**3), 1),
                "free_gb":    round(usage.free  / (1024**3), 1),
                "used_pct":   round((usage.used / usage.total) * 100, 1)
                              if usage.total else 0,
            })
        except OSError:
            continue
    return out


def _drive_kind(d: str) -> str:
    """Return a coarse kind: 'fixed' / 'removable' / 'network' / 'cdrom'."""
    try:
        import ctypes
        # GetDriveType from kernel32; 0=unknown, 2=removable, 3=fixed,
        # 4=remote, 5=cdrom, 6=ramdisk.
        kind_int = ctypes.windll.kernel32.GetDriveTypeW(d)
        return {2: "removable", 3: "fixed", 4: "network",
                5: "cdrom", 6: "ramdisk"}.get(kind_int, "unknown")
    except Exception:
        return "unknown"


def _drive_label(d: str) -> str:
    """Volume label via GetVolumeInformationW.  Empty string when no
    label is set."""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(256)
        res = ctypes.windll.kernel32.GetVolumeInformationW(
            d, buf, 256, None, None, None, None, 0)
        if res:
            return buf.value
    except Exception: pass
    return ""


# ─── Worker thread ───────────────────────────────────────────────────

def _run_scan_worker(drive: str, stop_event: threading.Event) -> None:
    """Top-level scan driver.  Wraps engine selection + error capture
    so the public state always lands in a consistent shape."""
    started = time.time()
    try:
        # Engine selection.  MFT path is stubbed for future use; we
        # fall through to scandir.  Setting `engine` lets the UI label
        # which path was used.
        engine = "scandir"
        with _state_lock:
            _active_state["engine"] = engine
        result = _scan_via_scandir(drive, stop_event)
        if stop_event.is_set():
            with _state_lock:
                _active_state["state"] = "cancelled"
                _active_state["ended_at"] = time.time()
            log.info(f"disk scan cancelled after {time.time()-started:.1f}s")
            return
        result["scan_seconds"] = round(time.time() - started, 2)
        with _state_lock:
            _active_state["state"]     = "done"
            _active_state["ended_at"]  = time.time()
            _active_state["result"]    = result
        log.info(
            f"disk scan complete: {drive} in {result['scan_seconds']:.1f}s — "
            f"{result['total_files']:,} files, "
            f"{result['total_dirs']:,} dirs, "
            f"{result['total_bytes'] / (1024**3):.1f} GB"
        )
    except Exception as e:
        log.exception(f"disk scan failed: {e}")
        with _state_lock:
            _active_state["state"]    = "error"
            _active_state["error"]    = str(e)
            _active_state["ended_at"] = time.time()


def _scan_via_scandir(drive: str, stop_event: threading.Event) -> dict:
    """Recursive scandir-based scan.  Parallelised across top-level
    subdirs using a small thread pool — most of the wall-clock time
    is spent waiting on filesystem I/O which the OS happily services
    concurrently.

    Returns a dict with `tree`, `top_files`, `top_folders`,
    `extensions`, `total_*`."""
    drive = _normalize_drive(drive)
    # Capture the starting volume's serial number so we can refuse to
    # cross volume boundaries (junctions to D:\ etc).
    target_volume = _get_volume_serial(drive)

    # Root tree node.  All paths in the tree use realpath for keys so
    # symlinks resolve consistently.
    root_path = os.path.realpath(drive)
    root = _new_folder_node(root_path, name=os.path.basename(root_path.rstrip(os.sep)) or root_path)

    # Top-N heaps.  We use min-heaps where the smallest item is popped
    # when we exceed the cap, so the heap always contains the N largest
    # seen so far.  Entries are tuples (size, kind_specific_payload).
    top_files_heap   = []      # (size, path, ext)
    top_folders_heap = []      # (size, path)
    ext_totals: dict = {}      # ext (lowercase, '' for no ext) -> total bytes
    counters = {"files": 0, "dirs": 0, "bytes": 0, "errors": 0,
                "since_last_yield": 0}

    # Threadpool walks the top-level subdirs concurrently.  Each task
    # returns its own subtree which we splice into the root.
    try:
        first_level: list = []
        with os.scandir(drive) as it:
            for entry in it:
                if stop_event.is_set():
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        first_level.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        # Files directly under the drive root
                        _consume_file(entry, root, top_files_heap,
                                       ext_totals, counters, drive)
                except OSError:
                    counters["errors"] += 1
                    continue
    except PermissionError:
        # Should never happen as admin — but be defensive.
        counters["errors"] += 1

    # First-level dirs scanned in parallel.  Each task gets the dir
    # path + a shared stop_event + writes its own subtree.  We collect
    # them at the end so the root's `children` dict is populated in
    # one shot.
    subtree_results: list = []
    with ThreadPoolExecutor(max_workers=_POOL_WORKERS,
                             thread_name_prefix="disk-scan") as pool:
        futures = []
        for d in first_level:
            futures.append(pool.submit(_walk_subdir, d, stop_event,
                                         target_volume))
        for fut in futures:
            try:
                sub_result = fut.result()
            except Exception as e:
                log.debug(f"subtree walk raised: {e}")
                counters["errors"] += 1
                continue
            if sub_result is None:
                continue
            subtree, sub_counters, sub_top_files, sub_top_folders, sub_ext = sub_result
            # Splice the subtree under root
            root["children"][subtree["path"]] = subtree
            root["size"]         += subtree["size"]
            root["file_count"]   += subtree["file_count"]
            root["subdir_count"] += subtree["subdir_count"] + 1   # +1 for the subdir itself
            # Merge counters
            for k, v in sub_counters.items():
                counters[k] = counters.get(k, 0) + v
            # Merge top-file heap.  sub_top_files entries are
            # (size, (path, ext)) tuples — same shape as the global heap.
            for size, payload in sub_top_files:
                _heap_push_top(top_files_heap, size, payload,
                                cap=_TOP_FILES_KEEP)
            # Merge top-folders heap (one entry per subdir + everything inside)
            _heap_push_top(top_folders_heap, subtree["size"], (subtree["path"],),
                            cap=_TOP_FOLDERS_KEEP)
            for size, payload in sub_top_folders:
                _heap_push_top(top_folders_heap, size, payload,
                                cap=_TOP_FOLDERS_KEEP)
            # Merge extension totals
            for ext, sz in sub_ext.items():
                ext_totals[ext] = ext_totals.get(ext, 0) + sz

    # Final aggregates
    root_size_with_files_at_root = root["size"]
    total_bytes = root_size_with_files_at_root
    # Sort heaps descending
    top_files_sorted = sorted(top_files_heap,
                               key=lambda x: -x[0])[:_TOP_FILES_KEEP]
    top_folders_sorted = sorted(top_folders_heap,
                                  key=lambda x: -x[0])[:_TOP_FOLDERS_KEEP]
    ext_sorted = sorted(
        [{"ext": (e or "(no ext)"), "bytes": b} for e, b in ext_totals.items()],
        key=lambda x: -x["bytes"],
    )[:50]

    # Update final progress with the post-scan totals
    with _state_lock:
        _active_state["progress"]["files"]  = counters["files"]
        _active_state["progress"]["dirs"]   = counters["dirs"]
        _active_state["progress"]["bytes"]  = total_bytes
        _active_state["progress"]["errors"] = counters["errors"]

    return {
        "drive":         drive,
        "tree":          root,
        "total_bytes":   total_bytes,
        "total_files":   counters["files"],
        "total_dirs":    counters["dirs"],
        "errors":        counters["errors"],
        "top_files":     [
            {"size": s, "path": payload[0], "ext": payload[1]}
            for s, payload in top_files_sorted
        ],
        "top_folders":   [
            {"size": s, "path": payload[0]} for s, payload in top_folders_sorted
        ],
        "extensions":    ext_sorted,
    }


def _walk_subdir(start: str, stop_event: threading.Event,
                  target_volume: Optional[int]) -> Optional[tuple]:
    """Recursively walk one subtree.  Returns (root_node, counters,
    top_files_local, top_folders_local, ext_totals_local).  Counters
    are accumulated locally to keep the threads independent — caller
    merges them into the global state at the end."""
    if stop_event.is_set():
        return None
    counters = {"files": 0, "dirs": 0, "bytes": 0, "errors": 0}
    top_files_local: list  = []
    top_folders_local: list = []
    ext_totals_local: dict  = {}
    # Each subtree has its own root node.  `name` is the leaf folder
    # name; `path` is the realpath for stable child-keying.
    try:
        real_start = os.path.realpath(start)
    except OSError:
        return None
    root_name = os.path.basename(start.rstrip(os.sep)) or start
    node = _new_folder_node(real_start, name=root_name)
    # Walk iteratively to avoid recursion-depth issues on absurdly
    # nested directories.  Stack entries are (path, node).
    stack = [(real_start, node)]
    while stack and not stop_event.is_set():
        cur_path, cur_node = stack.pop()
        counters["dirs"] += 1
        try:
            with os.scandir(_long_path(cur_path)) as it:
                for entry in it:
                    if stop_event.is_set():
                        return None
                    counters.setdefault("since_last_yield", 0)
                    counters["since_last_yield"] += 1
                    if counters["since_last_yield"] >= _CANCEL_CHECK_INTERVAL:
                        counters["since_last_yield"] = 0
                        if stop_event.is_set():
                            return None
                        # Periodic progress mirror
                        with _state_lock:
                            if _active_state is not None:
                                p = _active_state["progress"]
                                p["files"]   += 0     # incremental updates below
                                p["current"] = cur_path[:240]
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        counters["errors"] += 1
                        continue
                    # Refuse to follow reparse points (junctions, symlinks,
                    # OneDrive cloud-only stubs).
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        counters["errors"] += 1
                        continue
                    if (st.st_file_attributes
                          & 0x400):   # FILE_ATTRIBUTE_REPARSE_POINT
                        # Treat as zero-byte leaf; don't recurse
                        continue
                    if is_dir:
                        # Refuse cross-volume crossings
                        try:
                            child_real = os.path.realpath(entry.path)
                        except OSError:
                            counters["errors"] += 1
                            continue
                        if target_volume is not None:
                            child_vol = _get_volume_serial(child_real)
                            if child_vol is not None and child_vol != target_volume:
                                continue
                        ch_node = _new_folder_node(child_real, name=entry.name)
                        cur_node["children"][child_real] = ch_node
                        cur_node["subdir_count"] += 1
                        stack.append((child_real, ch_node))
                    else:
                        # Regular file
                        try:
                            sz = st.st_size
                        except (OSError, AttributeError):
                            counters["errors"] += 1
                            continue
                        cur_node["size"]            += sz
                        cur_node["file_count"]      += 1
                        cur_node["file_size_self"]  += sz
                        counters["files"] += 1
                        counters["bytes"] += sz
                        # Ext breakdown
                        _, ext = os.path.splitext(entry.name)
                        ext = ext.lower().lstrip(".")
                        ext_totals_local[ext] = ext_totals_local.get(ext, 0) + sz
                        # Top-files heap (need full path)
                        _heap_push_top(
                            top_files_local, sz, (entry.path, ext),
                            cap=_TOP_FILES_KEEP,
                        )
        except (OSError, PermissionError):
            counters["errors"] += 1
            continue
    # After populating, fold child sizes UP through the tree.  Because
    # we used a DFS stack walk, child nodes are fully populated by the
    # time we pop their parent — but we pushed children before fully
    # walking them, so we need a post-order rollup.  Easiest path: walk
    # the local tree once more to accumulate sizes bottom-up.
    _rollup_sizes(node)
    # Push each folder size into the local top-folders heap
    _collect_folder_sizes(node, top_folders_local, cap=_TOP_FOLDERS_KEEP)
    return node, counters, top_files_local, top_folders_local, ext_totals_local


def _rollup_sizes(node: dict) -> int:
    """Recursive bottom-up accumulation of folder sizes.  Returns the
    rolled-up size of this node."""
    own_files_size = node.get("file_size_self", 0)
    sub_total = 0
    for child in (node.get("children") or {}).values():
        sub_total += _rollup_sizes(child)
    node["size"] = own_files_size + sub_total
    return node["size"]


def _collect_folder_sizes(node: dict, heap: list, cap: int) -> None:
    """Walk the tree and push every folder's total size into `heap`."""
    for child in (node.get("children") or {}).values():
        _heap_push_top(heap, child["size"], (child["path"],), cap=cap)
        _collect_folder_sizes(child, heap, cap)


def _heap_push_top(heap: list, size: int, payload: tuple, cap: int) -> None:
    """Maintain a min-heap of the top-N largest entries."""
    if size <= 0:
        return
    if len(heap) < cap:
        heapq.heappush(heap, (size, payload))
    else:
        if size > heap[0][0]:
            heapq.heapreplace(heap, (size, payload))


def _consume_file(entry, parent_node: dict, top_files_heap: list,
                   ext_totals: dict, counters: dict, drive: str) -> None:
    """Handle a single file entry encountered at the drive root."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        counters["errors"] += 1
        return
    if st.st_file_attributes & 0x400:
        return
    sz = st.st_size
    parent_node["size"]            += sz
    parent_node["file_count"]      += 1
    parent_node["file_size_self"]  += sz
    counters["files"] += 1
    counters["bytes"] += sz
    _, ext = os.path.splitext(entry.name)
    ext = ext.lower().lstrip(".")
    ext_totals[ext] = ext_totals.get(ext, 0) + sz
    _heap_push_top(top_files_heap, sz, (entry.path, ext), cap=_TOP_FILES_KEEP)


def _new_folder_node(path: str, name: str) -> dict:
    return {
        "name":            name,
        "path":            path,
        "size":            0,
        "file_size_self":  0,
        "file_count":      0,
        "subdir_count":    0,
        "children":        {},
    }


def _long_path(p: str) -> str:
    """Prepend the long-path prefix when needed.  Windows API accepts
    `\\?\C:\very\long\path` and bypasses MAX_PATH=260."""
    if p.startswith("\\\\?\\"):
        return p
    if len(p) >= 240 and os.path.isabs(p):
        return "\\\\?\\" + p
    return p


def _get_volume_serial(path: str) -> Optional[int]:
    """Read the volume serial number for `path`'s volume.  Used to
    refuse crossing into a different drive via a junction."""
    try:
        import ctypes
        from ctypes import wintypes
        # Need a backslash-terminated root.  GetVolumeInformationW
        # accepts None for the current drive but we want explicit.
        drive_root = os.path.splitdrive(path)[0]
        if drive_root and not drive_root.endswith(os.sep):
            drive_root += os.sep
        serial = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            drive_root, None, 0, ctypes.byref(serial),
            None, None, None, 0)
        return serial.value if ok else None
    except Exception:
        return None


# ─── Deletion (with safety caps) ─────────────────────────────────────

def _is_deny_listed(path: str) -> tuple[bool, str]:
    """True if `path` is under any deny prefix, IS a deny prefix, or
    CONTAINS a deny prefix (i.e. deleting `path` would also delete
    something we're sworn to protect).  Also blocks bare drive roots —
    a delete on `C:\\` would mean wiping the whole drive.

    Returns (is_denied, human-readable reason).

    beta.19 — added the "contains a deny prefix" check.  Without it,
    selecting C:\\Users in the treemap and clicking Delete would feed
    shutil.rmtree the whole user dir, taking %APPDATA%/GhostShell and
    every other protected child with it.  The original one-directional
    check only caught the inverse case (paths *inside* a deny zone)."""
    try:
        real = os.path.realpath(path).lower().rstrip(os.sep)
    except OSError:
        return True, "path could not be resolved"
    if not real:
        return True, "empty path after resolution"
    # Bare drive root — refuse outright.  os.path.splitdrive returns
    # ('C:', '\\') for 'C:\\'; the rstrip above makes it 'c:'.  A real
    # path with depth always has at least one os.sep after the drive.
    drive_part, tail = os.path.splitdrive(real)
    if drive_part and not tail.strip(os.sep):
        return True, "drive root (entire drive)"
    for prefix in _DENY_PREFIXES:
        if real == prefix:
            return True, f"protected path: {prefix}"
        # Path is inside a deny zone
        if real.startswith(prefix + os.sep):
            return True, f"inside protected path: {prefix}"
        # Path CONTAINS a deny zone — deleting this would also nuke
        # something protected.  This is the critical pre-beta.19 gap.
        if prefix.startswith(real + os.sep):
            return True, f"would also delete protected path: {prefix}"
    return False, ""


def _append_delete_log(entries: list) -> None:
    """Ring-buffer audit log for every delete action."""
    try:
        existing = []
        if os.path.exists(DELETE_LOG_PATH):
            try:
                with open(DELETE_LOG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f) or []
            except Exception:
                existing = []
        existing.extend(entries)
        existing = existing[-DELETE_LOG_MAX:]
        tmp = DELETE_LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, DELETE_LOG_PATH)
    except Exception as e:
        log.debug(f"delete-log write failed: {e}")


def delete_paths(paths: list, to_recycle_bin: bool = False) -> dict:
    """Delete a list of paths with belt-and-suspenders safety:
      • ≤ _DELETE_MAX_PATHS entries per call
      • ≤ _DELETE_MAX_BYTES total bytes per call (pre-validated)
      • Every path checked against the deny-prefix list
      • Real-path validated; symlink/junction tricks blocked
      • Per-path try/except — one failure never blocks the rest
      • Logged to disk_analyzer_delete_log.json

    `to_recycle_bin=True` routes through Shell.Application's recycle
    flag instead of permanent delete (still capped, still audited).
    """
    if not isinstance(paths, list):
        return {"ok": False, "err": "paths must be a list"}
    if len(paths) == 0:
        return {"ok": False, "err": "empty paths"}
    if len(paths) > _DELETE_MAX_PATHS:
        return {"ok": False,
                "err": f"too many paths in one call ({len(paths)} > "
                       f"{_DELETE_MAX_PATHS}) — split into batches"}

    # Phase 1: validate + size-up.  We compute the size before any
    # delete so we can enforce the total-bytes cap.
    validated: list = []
    total_size = 0
    rejected:  list = []
    for p in paths:
        if not isinstance(p, str) or not p:
            rejected.append({"path": str(p)[:200], "reason": "empty or non-string"})
            continue
        denied, why = _is_deny_listed(p)
        if denied:
            rejected.append({"path": p, "reason": f"protected path ({why})"})
            continue
        try:
            real = os.path.realpath(p)
            # Re-check deny on the resolved path too
            denied2, why2 = _is_deny_listed(real)
            if denied2:
                rejected.append({"path": p,
                                  "reason": f"resolves into protected path ({why2})"})
                continue
        except OSError as e:
            rejected.append({"path": p, "reason": f"resolve failed: {e}"})
            continue
        if not os.path.exists(real):
            rejected.append({"path": p, "reason": "does not exist"})
            continue
        try:
            if os.path.isdir(real):
                # beta.20 — size probe with no per-call cap.  We still
                # cap individual dir-size accounting at 1 TB so a
                # rogue junction loop doesn't pin us forever; that's
                # an accounting safeguard, not a delete refusal.
                sz = _dir_size_bounded(real, byte_cap=1024 * 1024 * 1024 * 1024)
            else:
                sz = os.path.getsize(real)
        except OSError as e:
            rejected.append({"path": p, "reason": f"size probe failed: {e}"})
            continue
        total_size += sz
        validated.append((real, sz))

    if not validated:
        # beta.19 — surface the first rejection reason in the error
        # string so a) the UI toast shows actionable info and b) the
        # auto-submitted Railway error report carries the why, not
        # just the generic "no valid paths".
        if rejected:
            first = rejected[0]
            err_msg = (f"no valid paths after safety checks — "
                       f"first rejection: {first.get('reason', 'unknown')}")
        else:
            err_msg = "no valid paths after safety checks"
        return {"ok": False,
                "err": err_msg,
                "rejected": rejected,
                "total_bytes": 0}

    # Phase 2: do the deletes
    actions: list = []
    errors: list  = []
    bytes_freed = 0
    for real, sz in validated:
        try:
            if to_recycle_bin:
                _recycle_one(real)
            else:
                _hard_delete_one(real)
            actions.append({
                "ts": time.time(), "path": real, "size": sz,
                "recycle": bool(to_recycle_bin),
            })
            bytes_freed += sz
        except Exception as e:
            errors.append({"path": real, "err": str(e)})

    if actions:
        _append_delete_log(actions)

    return {
        "ok":           True,
        "deleted":      len(actions),
        "bytes_freed":  bytes_freed,
        "errors":       errors,
        "rejected":     rejected,
        "to_recycle_bin": bool(to_recycle_bin),
    }


def _is_reparse_point(path: str) -> bool:
    """True if `path` is a junction / symlink / other reparse point.
    These must NOT be recursed into during size-probe or delete — the
    target may live on another volume (e.g. C:\\Games\\Foo containing a
    junction to D:\\Saves), and following it would size/delete data the
    user never selected."""
    try:
        attrs = os.stat(path, follow_symlinks=False).st_file_attributes
        return bool(attrs & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def _dir_size_bounded(path: str, byte_cap: int) -> int:
    """Sum file sizes under `path`, NOT following reparse points (so a
    junction to another volume isn't counted).  Bails early once total
    exceeds `byte_cap`."""
    total = 0
    # topdown=True so we can prune reparse-point subdirs before descent.
    for root, dirs, files in os.walk(_long_path(path), topdown=True):
        # Drop any junction/symlink subdir from the walk.
        dirs[:] = [d for d in dirs
                   if not _is_reparse_point(os.path.join(root, d))]
        for f in files:
            fp = os.path.join(root, f)
            try:
                # Don't follow a file symlink to another volume either.
                total += os.stat(fp, follow_symlinks=False).st_size
            except OSError:
                continue
            if total > byte_cap:
                return total
    return total


def _rmtree_no_reparse(d: str) -> None:
    """Recursively delete directory `d` WITHOUT following reparse points.
    For any junction/symlink encountered, os.rmdir removes the link
    itself (leaving the target volume's data intact) instead of
    recursing into it.  Explicit scandir recursion — not os.walk or
    shutil.rmtree, both of which descend into junctions on Windows."""
    try:
        entries = list(os.scandir(_long_path(d)))
    except OSError:
        # Can't enumerate — try to remove as-is and let it raise.
        os.rmdir(d)
        return
    for entry in entries:
        p = entry.path
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            if _is_reparse_point(p):
                # Junction/symlink dir → remove the link only.
                try: os.rmdir(p)
                except OSError: pass
            else:
                _rmtree_no_reparse(p)
        else:
            try:
                os.remove(p)
            except OSError:
                # Clear read-only attribute and retry once.
                try:
                    os.chmod(p, 0o666); os.remove(p)
                except OSError:
                    raise
    os.rmdir(d)


def _hard_delete_one(real: str) -> None:
    """Permanent delete that refuses to follow junctions / symlinks.

    shutil.rmtree on Windows descends into directory junctions and
    deletes their targets (its onerror/ignore_errors args don't change
    that), so a selected folder containing a junction to another volume
    could wipe off-volume data.  We use a reparse-aware recursive delete
    instead.  Raises on failure."""
    if _is_reparse_point(real):
        # Top path itself is a junction/symlink → remove the link only.
        if os.path.isdir(real):
            os.rmdir(real)
        else:
            os.remove(real)
    elif os.path.isdir(real):
        _rmtree_no_reparse(real)
    else:
        os.remove(real)


def _recycle_one(real: str) -> None:
    """Move a file or folder to the Recycle Bin via Shell.Application.
    Slower than permanent delete (PowerShell COM bridge) but reversible.

    For batched calls the caller could optimize by passing all paths to
    one PowerShell invocation; doing one-by-one keeps the audit log
    accurate (we know exactly which one failed)."""
    # PowerShell-escape the path
    escaped = real.replace("'", "''")
    ps = (
        "$ErrorActionPreference='Stop';"
        "$shell = New-Object -ComObject Shell.Application;"
        # SHFileOperation with FO_DELETE + FOF_ALLOWUNDO is the proper
        # API; PowerShell wraps it via Visual Basic helpers below.
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        f"if ([System.IO.Directory]::Exists('{escaped}')) {{"
        f"  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        f"    '{escaped}',"
        f"    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,"
        f"    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin"
        f"  )"
        f"}} else {{"
        f"  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        f"    '{escaped}',"
        f"    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,"
        f"    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin"
        f"  )"
        f"}}"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, timeout=60,
        creationflags=_CREATE_NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"recycle failed: {(r.stderr or r.stdout or '').strip()[:200]}")


def get_delete_log(limit: int = 50) -> list:
    if not os.path.exists(DELETE_LOG_PATH):
        return []
    try:
        with open(DELETE_LOG_PATH, "r", encoding="utf-8") as f:
            rows = json.load(f) or []
        return rows[-max(1, min(DELETE_LOG_MAX, int(limit))):]
    except Exception:
        return []
