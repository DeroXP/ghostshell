"""Snapshots & Restore — the undo layer for GhostShell.

A snapshot is a named, timestamped bundle of registry exports covering
every path GhostShell ever writes to.  Taking a snapshot is fast (single
`reg export` per path).  Restoring re-imports those .reg files, undoing
any change made between the snapshot and now.

Lives in `%APPDATA%\\GhostShell\\snapshots\\<id>\\`:
    meta.json     — name, description, timestamp, scope, file list
    <path>.reg    — one .reg export per registry hive path

Public API:
    take_snapshot(name, description='', scope='full')  → snapshot id
    list_snapshots()                                    → list[meta]
    get_snapshot(id)                                    → meta dict
    restore_snapshot(id)                                → result dict
    delete_snapshot(id)                                 → result dict
    auto_snapshot_before(operation_name)                → id (or None)
    prune_old(keep_last=10)                             → count deleted

Conservative by default: auto-snapshots are kept for the last 10
events; manual snapshots are kept until the user deletes them.

Scope: 'full' covers every HKLM / HKCU path GhostShell modules touch.
The list is curated below — when adding a new optimizer category, also
add its registry paths to `_GS_REGISTRY_SCOPE` so they're captured in
future snapshots.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger, run_cmd

log = get_logger("snapshots")

SNAPSHOTS_DIR = os.path.join(APPDATA_DIR, "snapshots")

# ─── Curated GhostShell registry scope ───────────────────────────────────
# Every path this list covers gets exported on each "full" snapshot.
# Adding a path here makes it part of all FUTURE snapshots; existing
# snapshots are unchanged.
#
# Each entry is (label, key_path).  Label is shown in the UI's
# "scope" field; it doesn't have to be unique.
_GS_REGISTRY_SCOPE: list[tuple[str, str]] = [
    # ── CPU / scheduler / priority ──
    ("priority_control",   r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl"),
    ("session_manager",    r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager"),
    ("system_profile",     r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"),

    # ── Power / processor settings ──
    ("power_settings",     r"HKLM\SYSTEM\CurrentControlSet\Control\Power"),
    ("power_throttling",   r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling"),

    # ── Memory ──
    ("memory_mgmt",        r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"),

    # ── Storage / FS ──
    ("filesystem",         r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem"),

    # ── GPU / graphics ──
    ("graphics_drivers",   r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers"),
    ("device_guard",       r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard"),
    ("dwm_lm",             r"HKLM\SOFTWARE\Microsoft\Windows\Dwm"),

    # ── Game DVR / Game Bar / Game Config Store ──
    ("gameconfigstore",    r"HKCU\System\GameConfigStore"),
    ("gamebar",            r"HKCU\Software\Microsoft\GameBar"),
    ("gamedvr_policy",     r"HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR"),

    # ── Visual / desktop ──
    ("desktop",            r"HKCU\Control Panel\Desktop"),
    ("desktop_winmetrics", r"HKCU\Control Panel\Desktop\WindowMetrics"),
    ("visual_effects",     r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"),
    ("explorer_advanced",  r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"),
    ("themes_personalize", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"),

    # ── Mouse / keyboard / input ──
    ("mouse",              r"HKCU\Control Panel\Mouse"),
    ("keyboard",           r"HKCU\Control Panel\Keyboard"),
    ("accessibility_sk",   r"HKCU\Control Panel\Accessibility\StickyKeys"),
    ("accessibility_tk",   r"HKCU\Control Panel\Accessibility\ToggleKeys"),
    ("accessibility_kr",   r"HKCU\Control Panel\Accessibility\Keyboard Response"),

    # ── Search / Cortana ──
    ("search_user",        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search"),
    ("search_policy",      r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search"),
    ("explorer_policy",    r"HKCU\Software\Policies\Microsoft\Windows\Explorer"),

    # ── Telemetry / privacy ──
    ("data_collection",    r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection"),
    ("app_compat",         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppCompat"),
    ("cdm",                r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"),
    ("cloud_content",      r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent"),
    ("app_privacy",        r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy"),
    ("windows_ai_lm",      r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI"),

    # ── Update / Delivery ──
    ("wu_policy",          r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"),
    ("delivery_opt",       r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization"),

    # ── Edge ──
    ("edge_policy",        r"HKLM\SOFTWARE\Policies\Microsoft\Edge"),

    # ── Notifications / lock screen ──
    ("notifications",      r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"),
    ("personalization",    r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization"),

    # ── Storage Sense / OneDrive ──
    ("storage_sense",      r"HKCU\Software\Microsoft\Windows\CurrentVersion\StorageSense"),
]


# ─── Helpers ─────────────────────────────────────────────────────────────
def _safe_filename(s: str) -> str:
    """Slugify for filesystem use."""
    return re.sub(r"[^a-z0-9_-]", "_", (s or "").lower())[:60] or "snapshot"


def _new_snapshot_id(name: str) -> str:
    return f"{int(time.time())}_{_safe_filename(name)}"


def _meta_path(snap_dir: str) -> str:
    return os.path.join(snap_dir, "meta.json")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}".rstrip("0").rstrip(".")
        n_bytes /= 1024.0
    return f"{n_bytes:.1f} TB"


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ─── Take snapshot ───────────────────────────────────────────────────────
def take_snapshot(name: str, description: str = "",
                  scope: str = "full",
                  paths: Optional[list[tuple[str, str]]] = None,
                  auto: bool = False) -> dict:
    """Create a new snapshot bundle.

    Args:
      name:        short user-visible name (e.g. "before hard tweaks")
      description: optional free-text note
      scope:       'full' uses the curated GhostShell-touched paths.
                   'custom' uses `paths` argument exclusively.
      paths:       list of (label, key) tuples; required if scope='custom'
      auto:        marks this as an automatic snapshot — eligible for
                   pruning by `prune_old`.

    Returns:
      meta dict on success, or {'ok': False, 'err': ...}
    """
    name = (name or "snapshot").strip()
    _ensure_dir(SNAPSHOTS_DIR)
    snap_id  = _new_snapshot_id(name)
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    _ensure_dir(snap_dir)

    targets = paths if scope == "custom" else _GS_REGISTRY_SCOPE
    if not targets:
        shutil.rmtree(snap_dir, ignore_errors=True)
        return {"ok": False, "err": "no paths to snapshot"}

    log.info(f"Taking snapshot '{name}' (id={snap_id}) — {len(targets)} paths")
    files: list[dict] = []
    started = time.time()

    for label, key in targets:
        out_file = os.path.join(snap_dir, f"{_safe_filename(label)}.reg")
        # Use /y to overwrite without prompting; suppresses interactive
        # confirmation dialog that would otherwise hang on first call.
        r = run_cmd(["reg", "export", key, out_file, "/y"], timeout=30)
        ok = r["ok"] and os.path.exists(out_file) and os.path.getsize(out_file) > 0
        size = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        files.append({
            "label":  label,
            "key":    key,
            "file":   os.path.basename(out_file),
            "ok":     ok,
            "size_bytes": size,
            "err":    "" if ok else (r.get("err") or "export failed"),
        })

    elapsed = round(time.time() - started, 2)
    succeeded = sum(1 for f in files if f["ok"])
    failed_paths = [f for f in files if not f["ok"]]

    meta = {
        "id":           snap_id,
        "name":         name,
        "description":  description,
        "scope":        scope,
        "auto":         bool(auto),
        "ts":           time.time(),
        "elapsed_s":    elapsed,
        "files":        files,
        "succeeded":    succeeded,
        "total":        len(targets),
        "failed_paths": [f["key"] for f in failed_paths],
        "size_bytes":   _dir_size(snap_dir),
    }
    try:
        with open(_meta_path(snap_dir), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        log.error(f"Failed to write snapshot meta: {e}")
        shutil.rmtree(snap_dir, ignore_errors=True)
        return {"ok": False, "err": f"meta write failed: {e}"}

    log.info(f"Snapshot '{name}' done: {succeeded}/{len(targets)} paths "
             f"({_human_size(meta['size_bytes'])}, {elapsed}s)")
    if failed_paths:
        log.warning(f"  {len(failed_paths)} path(s) failed to export")

    return {"ok": True, **meta}


# ─── List / get / delete ─────────────────────────────────────────────────
def _read_meta(snap_dir: str) -> Optional[dict]:
    p = _meta_path(snap_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_snapshots() -> list[dict]:
    if not os.path.exists(SNAPSHOTS_DIR):
        return []
    out = []
    for d in os.listdir(SNAPSHOTS_DIR):
        full = os.path.join(SNAPSHOTS_DIR, d)
        if not os.path.isdir(full):
            continue
        meta = _read_meta(full)
        if meta is None:
            continue
        # Refresh size in case the user moved files
        meta["size_bytes"] = _dir_size(full)
        out.append(meta)
    out.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return out


def get_snapshot(snap_id: str) -> Optional[dict]:
    full = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(full):
        return None
    return _read_meta(full)


def delete_snapshot(snap_id: str) -> dict:
    full = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(full):
        return {"ok": False, "err": "snapshot not found"}
    try:
        shutil.rmtree(full)
        log.info(f"Deleted snapshot {snap_id}")
        return {"ok": True, "deleted": snap_id}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ─── Restore ─────────────────────────────────────────────────────────────
def restore_snapshot(snap_id: str) -> dict:
    """Re-import every .reg file in a snapshot bundle.  This OVERWRITES
    the current registry values for those paths.  Reboot recommended
    after a full restore so kernel-level changes (Memory Mgmt, Session
    Mgr, etc.) re-load from the restored values."""
    full = os.path.join(SNAPSHOTS_DIR, snap_id)
    meta = _read_meta(full)
    if meta is None:
        return {"ok": False, "err": "snapshot not found"}

    log.info(f"Restoring snapshot {snap_id} ('{meta.get('name')}')...")
    started = time.time()
    results: list[dict] = []
    for entry in meta.get("files", []):
        if not entry.get("ok"):
            results.append({"label": entry.get("label"),
                            "ok": False, "err": "skipped — original export failed"})
            continue
        reg_file = os.path.join(full, entry["file"])
        if not os.path.exists(reg_file):
            results.append({"label": entry.get("label"),
                            "ok": False, "err": "file missing"})
            continue
        # `reg import` reads and re-writes values in the export's hive
        r = run_cmd(["reg", "import", reg_file], timeout=30)
        ok = bool(r["ok"])
        results.append({
            "label": entry.get("label"),
            "key":   entry.get("key"),
            "ok":    ok,
            "err":   "" if ok else (r.get("err") or "import failed"),
        })

    elapsed = round(time.time() - started, 2)
    succeeded = sum(1 for r in results if r["ok"])
    log.info(f"Restore {snap_id} done: {succeeded}/{len(results)} paths "
             f"({elapsed}s)")

    return {
        "ok":              succeeded > 0,
        "id":              snap_id,
        "name":            meta.get("name"),
        "succeeded":       succeeded,
        "total":           len(results),
        "elapsed_s":       elapsed,
        "results":         results,
        "reboot_recommended": True,
    }


# ─── Auto-snapshot helper for callers (optimizer, etc.) ──────────────────
def auto_snapshot_before(operation_name: str) -> Optional[str]:
    """Convenience wrapper: take an automatic snapshot before a
    destructive bulk operation.  Returns the snapshot id on success
    or None on failure (which never blocks the caller — the caller
    should proceed regardless).

    Auto-snapshots are eligible for pruning by `prune_old`."""
    try:
        r = take_snapshot(
            name=f"auto: before {operation_name}",
            description=f"Automatic snapshot before {operation_name}.",
            scope="full",
            auto=True,
        )
        if r.get("ok"):
            return r.get("id")
    except Exception as e:
        log.warning(f"auto_snapshot_before failed: {e}")
    return None


def prune_old(keep_last: int = 10) -> dict:
    """Delete old AUTO snapshots beyond the keep_last cap.  Manual
    snapshots are never pruned automatically."""
    snaps = list_snapshots()
    auto_snaps = [s for s in snaps if s.get("auto")]
    auto_snaps.sort(key=lambda m: m.get("ts", 0), reverse=True)
    deletable = auto_snaps[keep_last:]
    deleted = 0
    for s in deletable:
        try:
            r = delete_snapshot(s["id"])
            if r.get("ok"):
                deleted += 1
        except Exception:
            pass
    return {"ok": True, "deleted": deleted, "kept": min(len(auto_snaps), keep_last)}
