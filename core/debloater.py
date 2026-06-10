"""Windows 11 Debloater — removes bloatware, disables services & scheduled tasks."""
import json
import os
from config import (BLOATWARE_APPS, SERVICES_TO_DISABLE,
                    SCHEDULED_TASKS_TO_DISABLE, APPDATA_DIR)
from core.utils import run_ps, run_cmd, get_logger, backup_registry, detect_drive_type

log = get_logger("debloat")

# 3.4.2 — sidecar recording each service's ORIGINAL `Start` type before
# we disable it, so "Restore services" puts back the user's real prior
# state instead of a hardcoded guess.  Map of {service_id: start_int}
# where Windows Start values are 0=boot 1=system 2=auto 3=demand
# 4=disabled.
_SERVICE_BACKUP_PATH = os.path.join(APPDATA_DIR, "debloat_service_backup.json")
_START_INT_TO_KEYWORD = {0: "boot", 1: "system", 2: "auto", 3: "demand", 4: "disabled"}


def _read_service_start(service_id: str):
    """Return the current `Start` DWORD for a service (int), or None if
    it can't be read (service absent, etc.)."""
    r = run_cmd(["reg", "query",
                 rf"HKLM\SYSTEM\CurrentControlSet\Services\{service_id}",
                 "/v", "Start"])
    if not r.get("ok") or not r.get("out"):
        return None
    for line in r["out"].splitlines():
        if "Start" in line and "REG_DWORD" in line:
            try:
                return int(line.rsplit("0x", 1)[1], 16)
            except (ValueError, IndexError):
                return None
    return None


def _load_service_backup() -> dict:
    if not os.path.exists(_SERVICE_BACKUP_PATH):
        return {}
    try:
        with open(_SERVICE_BACKUP_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_service_backup(data: dict) -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        tmp = _SERVICE_BACKUP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _SERVICE_BACKUP_PATH)
    except Exception as e:
        log.debug(f"service backup save failed: {e}")


# ---------------------------------------------------------------------------
# Detect which bloatware is actually installed
# ---------------------------------------------------------------------------
def scan_installed_apps() -> list[dict]:
    """Return the BLOATWARE_APPS list annotated with 'installed' booleans."""
    r = run_ps("Get-AppxPackage -AllUsers | Select-Object Name | ConvertTo-Json")
    installed_names: set[str] = set()
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            installed_names = {p.get("Name", "") for p in data}
        except Exception:
            pass

    results = []
    for app in BLOATWARE_APPS:
        pkg = app["id"]
        # Partial match (some package names have version suffixes)
        found = any(pkg.lower() in n.lower() for n in installed_names) if installed_names else False
        results.append({**app, "installed": found})
    return results


def scan_services() -> list[dict]:
    """Return SERVICES_TO_DISABLE annotated with current status."""
    r = run_ps("Get-Service | Select-Object Name,StartType,Status | ConvertTo-Json")
    svc_map: dict = {}
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for s in data:
                svc_map[s.get("Name", "").lower()] = {
                    "start": str(s.get("StartType", "")),
                    "status": str(s.get("Status", "")),
                }
        except Exception:
            pass

    drive = detect_drive_type()
    results = []
    for svc in SERVICES_TO_DISABLE:
        info = svc_map.get(svc["id"].lower(), {})
        skip = False
        if svc.get("cond") == "ssd_only" and drive == "hdd":
            skip = True
        results.append({
            **svc,
            "current_start": info.get("start", "Unknown"),
            "current_status": info.get("status", "Unknown"),
            "skip": skip,
        })
    return results


# ---------------------------------------------------------------------------
# Removal actions
# ---------------------------------------------------------------------------
def remove_app(package_id: str) -> dict:
    """Remove an AppX package for all users."""
    log.info(f"Removing package: {package_id}")
    r = run_ps(
        f'Get-AppxPackage -AllUsers *{package_id}* | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue'
    )
    # Also remove provisioned package so it doesn't reinstall
    run_ps(
        f'Get-AppxProvisionedPackage -Online | Where-Object {{$_.PackageName -like "*{package_id}*"}} '
        f'| Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue'
    )
    if r["ok"] or "does not exist" in r["err"].lower():
        log.info(f"  ✓ Removed {package_id}")
        return {"ok": True, "id": package_id}
    log.warning(f"  ✗ Failed {package_id}: {r['err']}")
    return {"ok": False, "id": package_id, "err": r["err"]}


def remove_apps_batch(package_ids: list[str]) -> list[dict]:
    """Remove multiple apps."""
    return [remove_app(pid) for pid in package_ids]


def disable_service(service_id: str) -> dict:
    """Stop and disable a Windows service.

    3.4.2 — captures the service's CURRENT Start type into the backup
    sidecar BEFORE disabling, so restore_services() can put back the
    user's real prior state.  Won't overwrite an existing backup entry
    (so re-running debloat doesn't record 'disabled' as the original)."""
    log.info(f"Disabling service: {service_id}")
    backup_registry(
        rf"HKLM\SYSTEM\CurrentControlSet\Services\{service_id}",
        f"svc_{service_id}"
    )
    # Record the original Start type once.
    backup = _load_service_backup()
    if service_id not in backup:
        cur = _read_service_start(service_id)
        if cur is not None and cur != 4:   # don't record an already-disabled svc
            backup[service_id] = cur
            _save_service_backup(backup)
    r1 = run_cmd(["sc", "stop", service_id])
    r2 = run_cmd(["sc", "config", service_id, "start=", "disabled"])
    if r2["ok"]:
        log.info(f"  ✓ Disabled {service_id}")
        return {"ok": True, "id": service_id}
    log.warning(f"  ✗ Failed to disable {service_id}: {r2['err']}")
    return {"ok": False, "id": service_id, "err": r2["err"]}


def disable_services_batch(service_ids: list[str]) -> list[dict]:
    return [disable_service(sid) for sid in service_ids]


def enable_service(service_id: str, start_keyword: str = "demand") -> dict:
    """Restore a service's start type (and start it if auto).  Used by
    restore_services().  start_keyword: boot/system/auto/demand/disabled."""
    r = run_cmd(["sc", "config", service_id, "start=", start_keyword])
    started = False
    if r.get("ok") and start_keyword in ("auto", "boot", "system"):
        s = run_cmd(["sc", "start", service_id])
        started = bool(s.get("ok"))
    if r.get("ok"):
        log.info(f"  ✓ Restored {service_id} → start={start_keyword}"
                 + (" (started)" if started else ""))
        return {"ok": True, "id": service_id, "start": start_keyword, "started": started}
    log.warning(f"  ✗ Failed to restore {service_id}: {r.get('err')}")
    return {"ok": False, "id": service_id, "err": r.get("err")}


def restore_services() -> dict:
    """Re-enable every service the debloater disabled, restoring each to
    its recorded original Start type.  For any service we disabled
    without a recorded original (edge case), fall back to 'demand'
    (Manual) — the safe Windows default that won't auto-run but is
    available on request.

    Called by the optimizer's reset-to-defaults flow and exposed via
    /api/debloat/restore-services so the user can always undo a debloat
    pass (previously there was NO way to re-enable disabled services)."""
    backup = _load_service_backup()
    # Restore everything we have a record for, plus any service in the
    # manifest that's currently disabled (covers records lost to a
    # wiped sidecar).
    targets = dict(backup)
    for svc in SERVICES_TO_DISABLE:
        sid = svc["id"]
        if sid not in targets:
            cur = _read_service_start(sid)
            if cur == 4:   # currently disabled but no record → safe default
                targets[sid] = 3   # demand / Manual
    results = []
    for sid, start_int in targets.items():
        kw = _START_INT_TO_KEYWORD.get(int(start_int), "demand")
        results.append(enable_service(sid, kw))
    # Clear the backup once restored.
    if backup:
        _save_service_backup({})
    ok = sum(1 for r in results if r.get("ok"))
    log.info(f"restore_services: re-enabled {ok}/{len(results)} services")
    return {"ok": True, "restored": ok, "total": len(results), "results": results}


def disable_scheduled_task(task_path: str) -> dict:
    """Disable a scheduled task."""
    log.info(f"Disabling task: {task_path}")
    r = run_ps(f'Disable-ScheduledTask -TaskName "{task_path}" -ErrorAction SilentlyContinue')
    # Alternative via schtasks
    if not r["ok"]:
        r = run_cmd(["schtasks", "/Change", "/TN", task_path, "/Disable"])
    if r["ok"]:
        log.info(f"  ✓ Disabled task {task_path}")
        return {"ok": True, "task": task_path}
    log.warning(f"  ✗ Failed task {task_path}: {r['err']}")
    return {"ok": False, "task": task_path, "err": r["err"]}


def disable_all_scheduled_tasks() -> list[dict]:
    return [disable_scheduled_task(t) for t in SCHEDULED_TASKS_TO_DISABLE]


# ---------------------------------------------------------------------------
# Feature / registry tweaks
# ---------------------------------------------------------------------------
def disable_features() -> list[dict]:
    """Disable Windows 11 features via registry."""
    log.info("Disabling Windows 11 features...")
    results = []
    tweaks = [
        # Disable Copilot
        ("Copilot", r"HKCU\Software\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", "REG_DWORD", "1"),
        # Disable Widgets
        ("Widgets", r"HKLM\SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", "REG_DWORD", "0"),
        # Disable Chat/Teams on taskbar
        ("Teams Chat", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarMn", "REG_DWORD", "0"),
        # Disable Search Highlights
        ("Search Highlights", r"HKCU\Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDynamicSearchBoxEnabled", "REG_DWORD", "0"),
        # Disable Start Menu suggestions
        ("Start Suggestions", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338388Enabled", "REG_DWORD", "0"),
        ("Start Suggestions 2", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338389Enabled", "REG_DWORD", "0"),
        ("Start Suggestions 3", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-310093Enabled", "REG_DWORD", "0"),
        # Disable Tips
        ("Tips & Suggestions", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SoftLandingEnabled", "REG_DWORD", "0"),
        # Disable Cortana
        ("Cortana", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search", "AllowCortana", "REG_DWORD", "0"),
        # Disable Web Search in Start
        ("Web Search Start", r"HKCU\Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", "REG_DWORD", "1"),
        # Disable lock screen tips
        ("Lock Screen Tips", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "RotatingLockScreenOverlayEnabled", "REG_DWORD", "0"),
        # Disable auto-install suggested apps
        ("Auto Install Apps", r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SilentInstalledAppsEnabled", "REG_DWORD", "0"),
    ]

    for name, key, value, vtype, data in tweaks:
        r = run_cmd(["reg", "add", key, "/v", value, "/t", vtype, "/d", data, "/f"])
        ok = r["ok"]
        if ok:
            log.info(f"  ✓ Disabled: {name}")
        else:
            log.warning(f"  ✗ Failed: {name} — {r['err']}")
        results.append({"name": name, "ok": ok})

    return results


def uninstall_onedrive() -> dict:
    """Uninstall OneDrive."""
    log.info("Uninstalling OneDrive...")
    # Kill OneDrive
    run_cmd(["taskkill", "/f", "/im", "OneDrive.exe"])

    # Try standard uninstall paths
    paths = [
        r"C:\Windows\System32\OneDriveSetup.exe",
        r"C:\Windows\SysWOW64\OneDriveSetup.exe",
    ]
    for p in paths:
        r = run_cmd([p, "/uninstall"])
        if r["ok"]:
            log.info("  ✓ OneDrive uninstalled")
            return {"ok": True}

    # Fallback: winget
    r = run_cmd(["winget", "uninstall", "Microsoft.OneDrive", "--silent"])
    if r["ok"]:
        log.info("  ✓ OneDrive uninstalled via winget")
        return {"ok": True}

    log.warning("  ✗ OneDrive uninstall failed")
    return {"ok": False, "err": "Could not uninstall OneDrive"}


# ---------------------------------------------------------------------------
# v3.3 — Block Game Bar URI schemes (silent no-op handlers)
# ---------------------------------------------------------------------------
#
# After Xbox Game Bar is debloated, anything in Windows that tries to invoke
# the ms-gamebar: / ms-gamebarservices: / ms-gamingoverlay: protocol URLs
# pops up the Windows "Open With" dialog ("Get an app to open this 'ms-gamebar'
# link").  That's because the URI scheme is still registered system-wide
# but the handler is gone.
#
# Fix: register a no-op handler in HKCU\Software\Classes — Windows now
# silently invokes our stub instead of asking the user.  Per-user override
# = no admin needed for this specific path, and uninstalling GhostShell
# doesn't leave system-wide remnants.
#
# Idempotent: re-running just refreshes the same registry values.

GAMEBAR_PROTOCOLS = (
    "ms-gamebar",
    "ms-gamebarservices",
    "ms-gamingoverlay",
)


def block_gamebar_protocols() -> list[dict]:
    """Register no-op handlers for the three Xbox Game Bar URI schemes
    so the 'Get an app to open this' picker stops appearing after
    debloat.  Sets the protocol's open command to a quick no-op so
    Windows fires-and-forgets it."""
    log.info("Blocking Game Bar URI scheme popups...")
    results = []
    # The no-op command — exits immediately, returns 0, no window flashes
    # because we're already running with hidden console (CREATE_NO_WINDOW
    # is what cmd /c gives us when invoked from a hidden parent).
    NOOP_CMD = r'cmd.exe /c exit'
    for scheme in GAMEBAR_PROTOCOLS:
        try:
            base = rf"HKCU\Software\Classes\{scheme}"
            # Required values for a custom URL protocol
            run_cmd(["reg", "add", base, "/ve", "/t", "REG_SZ",
                     "/d", f"URL:{scheme}", "/f"])
            run_cmd(["reg", "add", base, "/v", "URL Protocol",
                     "/t", "REG_SZ", "/d", "", "/f"])
            # The shell\open\command tree
            cmd_key = rf"{base}\shell\open\command"
            r = run_cmd(["reg", "add", cmd_key, "/ve", "/t", "REG_SZ",
                         "/d", NOOP_CMD, "/f"])
            ok = bool(r.get("ok"))
            log.info(f"  {'OK' if ok else 'FAIL'} blocked {scheme}://")
            results.append({"scheme": scheme, "ok": ok})
        except Exception as e:                       # noqa: BLE001
            log.warning(f"failed to block {scheme}: {e}")
            results.append({"scheme": scheme, "ok": False, "err": str(e)})
    return results


def unblock_gamebar_protocols() -> list[dict]:
    """Reverse block_gamebar_protocols — for users who reinstalled Game
    Bar and want the original handler back."""
    log.info("Unblocking Game Bar URI schemes...")
    results = []
    for scheme in GAMEBAR_PROTOCOLS:
        base = rf"HKCU\Software\Classes\{scheme}"
        r = run_cmd(["reg", "delete", base, "/f"])
        results.append({"scheme": scheme, "ok": bool(r.get("ok"))})
    return results


# ---------------------------------------------------------------------------
# Full debloat (convenience)
# ---------------------------------------------------------------------------
def run_full_debloat(app_ids: list[str] | None = None,
                     service_ids: list[str] | None = None,
                     do_tasks: bool = True,
                     do_features: bool = True,
                     do_onedrive: bool = True) -> dict:
    """Run a complete debloat pass. Pass None to use all defaults."""
    log.info("═══ STARTING FULL DEBLOAT ═══")
    results = {"apps": [], "services": [], "tasks": [], "features": [],
               "onedrive": {}, "gamebar_blocks": []}

    if app_ids is None:
        app_ids = [a["id"] for a in BLOATWARE_APPS]
    results["apps"] = remove_apps_batch(app_ids)

    if service_ids is None:
        service_ids = [s["id"] for s in SERVICES_TO_DISABLE if not s.get("cond")]
        # Add conditional ones if applicable
        drive = detect_drive_type()
        if drive in ("ssd", "nvme"):
            service_ids += [s["id"] for s in SERVICES_TO_DISABLE if s.get("cond") == "ssd_only"]
    results["services"] = disable_services_batch(service_ids)

    if do_tasks:
        results["tasks"] = disable_all_scheduled_tasks()

    if do_features:
        results["features"] = disable_features()

    if do_onedrive:
        results["onedrive"] = uninstall_onedrive()

    # v3.3 — block the ms-gamebar:* URL protocols.  After Game Bar is
    # debloated, system processes that try to invoke these still pop the
    # "Get an app to open this link" picker.  No-op handlers silence it.
    results["gamebar_blocks"] = block_gamebar_protocols()

    ok_count = sum(1 for r in results["apps"] if r["ok"])
    ok_count += sum(1 for r in results["services"] if r["ok"])
    ok_count += sum(1 for r in results["tasks"] if r["ok"])
    ok_count += sum(1 for r in results["features"] if r["ok"])
    total = len(results["apps"]) + len(results["services"]) + len(results["tasks"]) + len(results["features"])

    log.info(f"═══ DEBLOAT COMPLETE — {ok_count}/{total} operations succeeded ═══")
    return results
