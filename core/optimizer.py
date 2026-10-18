"""Hard performance optimizer — aggressive tweaks for gaming & latency."""
import json
import os
import time
from core.utils import run_ps, run_cmd, get_logger, backup_registry, detect_drive_type, atomic_write_json
from config import APPDATA_DIR
from core import reg_originals, sys_state

log = get_logger("optimize")


def _reg(key: str, value: str, data: str, vtype: str = "REG_DWORD") -> bool:
    # Note what was there before we change it, so Reset All can put back the
    # user's real value rather than a guessed default (core/reg_originals.py).
    reg_originals.remember(key, value, data, vtype)
    r = run_cmd(["reg", "add", key, "/v", value, "/t", vtype, "/d", data, "/f"])
    # 3.4.2 — most callers append {"ok": True} without checking this
    # return value (Vispora runs as admin so writes almost always
    # succeed).  Log the rare failure so it's visible in vispora.log
    # for diagnostics instead of vanishing silently.
    if not r.get("ok"):
        err = (r.get("err") or r.get("out") or "").strip()[:160]
        log.warning(f"reg write FAILED: {key}\\{value} = {data} ({vtype}) — {err}")
    return r["ok"]


# ═══════════════════════════════════════════════════════════════════════════
# Power Plan
# ═══════════════════════════════════════════════════════════════════════════
# v3 — Persistent baseline of the user's power scheme so we can restore
# it on "undo" / clean uninstall.  Captured BEFORE we change anything,
# so even a crash leaves the prior GUID on disk for next-launch recovery.
_POWER_BASELINE_PATH = os.path.join(APPDATA_DIR, "power_baseline.json")

# ═══════════════════════════════════════════════════════════════════════════
# What this module changes OUTSIDE the registry
# ───────────────────────────────────────────────────────────────────────────
# Named once, here, so the apply code, the reset code and the list of things a
# reset may touch (sys_state.allow) cannot drift apart.  All of these go
# through core/sys_state.py, which notes each one's previous state before
# changing it; Reset All puts back exactly that.
# ═══════════════════════════════════════════════════════════════════════════
_XBOX_SERVICES = ("XblAuthManager", "XblGameSave", "XboxGipSvc", "XboxNetApiSvc")
_EDGE_SERVICES = ("edgeupdate", "edgeupdatem", "MicrosoftEdgeElevationService")
_LEGACY_SERVICES = (
    ("Print Spooler",                "Spooler"),
    ("Fax",                           "Fax"),
    ("Windows Insider Service",       "wisvc"),
    ("Touch Keyboard / Handwriting",  "TabletInputService"),
    ("Retail Demo",                   "RetailDemo"),
    ("Phone Service",                 "PhoneSvc"),
    ("Windows Mobile Hotspot",        "icssvc"),
)
_SMARTCARD_SERVICES = (
    ("Smart Card",                "SCardSvr"),
    ("Smart Card Removal Policy", "SCPolicySvc"),
)
_OTHER_SERVICES = ("bthserv", "WSearch", "WerSvc", "DiagTrack", "dmwappushservice")
_TELEMETRY_TASKS = (
    r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser",
    r"\Microsoft\Windows\Application Experience\ProgramDataUpdater",
    r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator",
    r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip",
    r"\Microsoft\Windows\Customer Experience Improvement Program\KernelCeipTask",
    r"\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
    r"\Microsoft\Windows\Feedback\Siuf\DmClient",
    r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload",
    r"\Microsoft\Windows\Windows Error Reporting\QueueReporting",
)
_BASE_FEATURES = (
    "Internet-Explorer-Optional-amd64",
    "Printing-XPSServices-Features",
    "Printing-PrintToPDFServices-Features",
    "FaxServicesClientPackage",
    "WindowsMediaPlayer",
    "MediaPlayback",
    "WorkFolders-Client",
)
_VIRTUALISATION_FEATURES = (
    "Microsoft-Windows-Subsystem-Linux",
    "VirtualMachinePlatform",
    "HypervisorPlatform",
    "Microsoft-Hyper-V-All",
    "Containers-DisposableClientVM",
)
# Boot-entry elements set by the latency and boot categories.
_BCD_ELEMENTS = ("useplatformclock", "tscsyncpolicy", "disabledynamictick",
                 "bootmenupolicy", "quietboot", "bootlog")
_TCP_GLOBAL_TWEAKS = (
    ("autotuninglevel", "normal"),
    ("chimney", "disabled"),
    ("rss", "enabled"),
    ("ecncapability", "disabled"),
    ("timestamps", "disabled"),
    ("initialRto", "2000"),
    ("nonsackrttresiliency", "disabled"),
    ("maxsynretransmissions", "2"),
    ("fastopen", "enabled"),
    ("hystart", "disabled"),
)
_DEFENDER_EXCLUDE_PATHS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"C:\Program Files\Epic Games",
    r"C:\Program Files (x86)\Epic Games",
    r"C:\Program Files (x86)\Battle.net",
    r"C:\Program Files (x86)\Riot Games",
    r"C:\Program Files\Ubisoft",
    r"C:\Program Files (x86)\Ubisoft",
    r"C:\Program Files\EA Games",
    r"C:\Program Files\WindowsApps",  # Xbox Game Pass installs
    r"C:\XboxGames",                   # Xbox Game Pass install root
    r"C:\Program Files (x86)\GOG Galaxy",
)
_DEFENDER_EXCLUDE_PROCESSES = ("steam.exe", "epicgameslauncher.exe", "battle.net.exe",
                               "riotclientservices.exe", "ubisoftconnect.exe")

sys_state.allow("service", _XBOX_SERVICES + _EDGE_SERVICES + _OTHER_SERVICES
                + tuple(svc for _, svc in _LEGACY_SERVICES + _SMARTCARD_SERVICES))
sys_state.allow("task", _TELEMETRY_TASKS)
sys_state.allow("feature", _BASE_FEATURES + _VIRTUALISATION_FEATURES)
sys_state.allow("bcd", _BCD_ELEMENTS)
sys_state.allow("tcp", [param for param, _ in _TCP_GLOBAL_TWEAKS])
sys_state.allow("fsutil", ["disablelastaccess"])
sys_state.allow("defender", ["SubmitSamplesConsent", "MAPSReporting",
                             "ScanScheduleTime", "ScanScheduleQuickScanTime"])
sys_state.allow("defender-exclusion",
                [f"ExclusionPath:{p}" for p in _DEFENDER_EXCLUDE_PATHS]
                + [f"ExclusionProcess:{p}" for p in _DEFENDER_EXCLUDE_PROCESSES])


def _service_default(name: str, tweak: str, stock: str, start: bool = False) -> None:
    """Fixed-default fallback for a service that an older build changed
    without keeping a record: back to the start type Windows ships it with.

    Only when there is no record (what IS on record is restored exactly by
    the final pass of Reset All), and only when the service is still set the
    way the tweak leaves it — any other start type is somebody else's choice
    and is left alone."""
    if reg_originals.has_state("service", name):
        return
    if tweak == stock or sys_state.service_start(name) != tweak:
        return
    run_cmd(["sc", "config", name, "start=", stock])
    if start:
        run_cmd(["sc", "start", name])


def _bcd_default(bcd, name: str, tweak: str, stock=None) -> None:
    """The same for one boot-entry element: if it has no record and still
    reads the way the tweak leaves it, put back the Windows default — `stock`,
    or no value at all.  `bcd` is one sys_state.bcd_values() reading."""
    if not bcd or reg_originals.has_state("bcd", name):
        return
    cur = bcd.get(name.lower())
    if cur is None or not sys_state.bcd_same(cur, tweak):
        return
    if stock is None:
        run_cmd(["bcdedit", "/deletevalue", "{current}", name])
    else:
        run_cmd(["bcdedit", "/set", "{current}", name, stock])


def _reg_default(key: str, value: str, tweak, stock, vtype: str = "REG_DWORD") -> bool:
    """The same for one registry value: if it is on nobody's record and still
    reads the way the tweak leaves it, put back the Windows default — `stock`,
    or, with stock=None, no value at all.

    `tweak` is the data the tweak writes (a tuple where builds differed).
    A value on record is left to whoever recorded it: the optimizer's come
    back with the final pass of Reset All, another tab's (GPU, Network, ...)
    with that tab's own undo.  A value that reads any other way, or does not
    exist, is not something the tweak left behind and is not touched.

    False only if a default that was called for could not be written."""
    if reg_originals.on_record(key, value):
        return True
    if not any(reg_originals.reads_as(key, value, t, vtype)
               for t in (tweak if isinstance(tweak, tuple) else (tweak,))):
        return True
    if stock is None:
        return run_cmd(["reg", "delete", key, "/v", value, "/f"])["ok"]
    return _reg(key, value, stock, vtype)


_DX_PREFS = r"HKCU\Software\Microsoft\DirectX\UserGpuPreferences"
_DX_GLOBAL = "DirectXUserGlobalSettings"


def _enable_windowed_flip_model() -> bool:
    """SwapEffectUpgradeEnable=1 inside the semicolon-separated global DirectX
    settings string, keeping every other entry in it (Auto HDR, VRR…).  The
    whole string's previous value is on record, so Reset All puts back exactly
    what was there."""
    cur = reg_originals.read_value(_DX_PREFS, _DX_GLOBAL) or {}
    text = cur.get("data") if cur.get("type") == "REG_SZ" else ""
    pairs = {}
    for part in (text or "").split(";"):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            pairs[k.strip()] = v.strip()
    if pairs.get("SwapEffectUpgradeEnable") == "1":
        return True
    pairs["SwapEffectUpgradeEnable"] = "1"
    new = ";".join(f"{k}={v}" for k, v in pairs.items()) + ";"
    return _reg(_DX_PREFS, _DX_GLOBAL, new, "REG_SZ") and \
        (reg_originals.read_value(_DX_PREFS, _DX_GLOBAL) or {}).get("data") == new


def _enable_msi_mode(device_class: str, working_only: bool = False) -> int:
    """Switch the devices of one PnP class to message-signalled interrupts.
    PowerShell only lists the devices; every value is written through _reg,
    so what it was before is on record.  (This used to be a PowerShell block
    that wrote the registry itself — invisible to Reset All, so permanent.)"""
    only = " | Where-Object {$_.Status -eq 'OK'}" if working_only else ""
    r = run_ps(f"Get-PnpDevice -Class {device_class} -ErrorAction SilentlyContinue{only} "
               f"| ForEach-Object {{ $_.InstanceId }}")
    done = 0
    for dev in ((r.get("out") or "").splitlines() if r.get("ok") else []):
        dev = dev.strip()
        if "\\" not in dev or any(c in dev for c in '|"'):
            continue
        if _reg(rf"HKLM\SYSTEM\CurrentControlSet\Enum\{dev}\Device Parameters"
                r"\Interrupt Management\MessageSignaledInterruptProperties", "MSISupported", "1"):
            done += 1
    return done

# ═══════════════════════════════════════════════════════════════════════════
# Pro Mode (v3.3.1-beta.8+)
# ───────────────────────────────────────────────────────────────────────────
# Apply All Tweaks is safe by default — every tweak the audit flagged
# as "real benefit but real side-effect rate" lives behind this gate.
# Users who know what they're doing flip the toggle on the Optimizer
# page; everyone else gets a conservative pass.
#
# Tweaks gated behind Pro Mode:
#   - VBS / HVCI / Credential Guard disable (Spectre-class hardening)
#   - Spectre / Meltdown CPU mitigations disable
#   - MPO (Multi-Plane Overlay) disable (DWM compositor flag)
#   - HPET (High Precision Event Timer) disable
#   - bcdedit disabledynamictick yes (laptop battery cost)
#   - Blanket USB MSI mode (per-device audit recommended instead)
#   - Raised TDR delays (10/20s instead of 2s default)
#   - Windows Search service disable
#   - Memory Compression disable (only useful on >=32GB RAM)
#   - Hibernation off (laptops lose their battery feature)
# ═══════════════════════════════════════════════════════════════════════════

_PRO_MODE_PATH = os.path.join(APPDATA_DIR, "optimizer_pro_settings.json")


def get_pro_mode() -> dict:
    """Returns {enabled: bool}.  Default OFF."""
    try:
        if os.path.isfile(_PRO_MODE_PATH):
            with open(_PRO_MODE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return {"enabled": bool(data.get("enabled", False))}
    except Exception as e:
        log.debug(f"get_pro_mode: {e}")
    return {"enabled": False}


def set_pro_mode(enabled: bool) -> dict:
    """Persist the Pro Mode toggle.  Called from the UI."""
    try:
        atomic_write_json(_PRO_MODE_PATH, {"enabled": bool(enabled)})
        log.info(f"Pro Mode → {'ON' if enabled else 'OFF'}")
        return {"ok": True, "enabled": bool(enabled)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def _pro_mode_enabled() -> bool:
    """Single source of truth checked by individual risky tweaks."""
    return bool(get_pro_mode().get("enabled"))


_STOCK_ULTIMATE_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
_HIGH_PERF_GUID      = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
_BALANCED_GUID       = "381b4222-f694-41f0-9685-ff5bb260df2e"


def _own_power_plans() -> set:
    """Power plans Vispora itself switches to.  Finding one of these active
    says nothing about what the user had chosen: the start-up prep moves to
    High Performance at every launch, so by the time anything asked "what is
    the user's plan?" the answer was already ours — and that is what got
    saved as the baseline Reset All goes back to."""
    own = {_STOCK_ULTIMATE_GUID, _HIGH_PERF_GUID}
    clone = str(reg_originals.noted("ultimate_plan_guid") or "").lower()
    if clone:
        own.add(clone)
    return own


def active_power_plan() -> str:
    """GUID of the active power plan, or "" if it cannot be read."""
    r = run_cmd(["powercfg", "/getactivescheme"])
    for part in (r.get("out") or "").split():
        if len(part) == 36 and part.count("-") == 4:
            return part
    return ""


def _read_power_baseline() -> str:
    """The recorded pre-Vispora plan, or "" when there is none worth trusting."""
    try:
        with open(_POWER_BASELINE_PATH, "r", encoding="utf-8") as f:
            guid = str((json.load(f) or {}).get("guid", "") or "")
    except (OSError, ValueError):
        return ""
    return "" if guid.lower() in _own_power_plans() else guid


def _forget_power_baseline() -> None:
    """After a reset the user is back on their own plan.  Whatever they pick
    from here on is the next baseline, so the old record must not outlive it."""
    try:
        os.remove(_POWER_BASELINE_PATH)
    except OSError:
        pass


def _capture_power_baseline() -> str:
    """Capture the currently-active power scheme GUID (idempotent — only
    writes the baseline ONCE per install).  Returns the captured GUID
    or "" on failure."""
    try:
        guid = _read_power_baseline()
        if guid:
            return guid
        guid = active_power_plan()
        if guid.lower() in _own_power_plans():
            return ""           # already on one of ours — not the user's choice
        if guid:
            atomic_write_json(_POWER_BASELINE_PATH, {"guid": guid, "captured_ts": time.time()})
            log.info(f"  ✓ Power scheme baseline captured: {guid}")
        return guid
    except Exception as e:
        log.debug(f"power baseline capture failed: {e}")
        return ""


def restore_power_baseline() -> dict:
    """Restore the previously-captured power scheme.  Called by the
    'Undo optimizations' / 'Restore from snapshot' flows and at clean
    uninstall.  Safe no-op when no baseline was captured."""
    guid = _read_power_baseline()
    if not guid:
        return {"ok": False, "err": "no power baseline captured"}
    try:
        if not run_cmd(["powercfg", "/setactive", guid]).get("ok"):
            return {"ok": False, "err": f"recorded plan {guid} no longer exists"}
        log.info(f"  ✓ Power scheme restored to baseline: {guid}")
        return {"ok": True, "guid": guid}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def apply_ultimate_power_plan() -> dict:
    log.info("Applying Ultimate Performance power plan...")
    # v3 — capture baseline FIRST so we never lose the user's choice.
    _capture_power_baseline()
    # Reuse the copy we made last time.  `-duplicatescheme` creates a NEW
    # plan on every call, and this runs at every game start for Max-FPS
    # targets — the plan list filled up with "Ultimate Performance" clones.
    guid = str(reg_originals.noted("ultimate_plan_guid") or "")
    if guid and not run_cmd(["powercfg", "-setactive", guid])["ok"]:
        reg_originals.forget_notes("ultimate_plan_guid")     # the user deleted it; make a new one
        guid = ""
    if not guid:
        # Unhide ultimate performance plan
        r = run_cmd(["powercfg", "-duplicatescheme", "e9a42b02-d5df-448d-aa00-03f14749eb61"])
        if r["ok"] and r["out"]:
            # Output: "Power Scheme GUID: xxxxxxxx-..."
            for part in r["out"].split():
                if len(part) == 36 and "-" in part:
                    guid = part
                    break
        if guid:
            reg_originals.note("ultimate_plan_guid", guid)
            run_cmd(["powercfg", "-setactive", guid])
    if guid:
        log.info(f"  ✓ Ultimate Performance plan active (GUID: {guid})")
        return {"ok": True, "guid": guid}

    # Fallback: set high performance
    run_cmd(["powercfg", "-setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"])
    log.info("  ✓ High Performance plan set (Ultimate not available)")
    return {"ok": True, "guid": "high-perf-fallback"}


# ═══════════════════════════════════════════════════════════════════════════
# CPU Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_cpu_tweaks() -> list[dict]:
    log.info("Applying CPU optimizations...")
    results = []
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "cpu_priority")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\Power", "cpu_power")

    tweaks = [
        # Foreground app priority boost (short, variable, high foreground boost)
        ("Foreground Priority Boost",
         r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
         "Win32PrioritySeparation", "38"),
        # v3.3.1-beta.7: removed the raw-registry core-parking write
        # against `0cc5b647-...\ValueMax`.  apply_ultimate_power_plan
        # already disables core parking via the Ultimate Performance
        # scheme (sets CPMINCORES/CPMAXCORES to 100% the right way).
        # The raw-registry write only set a scheme-private "value max"
        # field that the Power Manager re-reads from the active scheme
        # anyway — pure duplicate.
        # v3.3.1-beta.7: SystemResponsiveness 0 → 10.  Setting to 0
        # starves the Windows audio service (Audiosrv runs in the
        # multimedia class) which causes Discord/in-game voice
        # crackling under heavy CPU load.  Microsoft's recommended
        # value for gaming PCs is 10 — keeps 90% for foreground while
        # leaving the audio thread oxygen.
        ("System Responsiveness",
         r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile",
         "SystemResponsiveness", "10"),
        # GPU scheduling priority
        ("GPU Scheduling Priority",
         r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "GPU Priority", "8"),
        ("Games Scheduling Category",
         r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "Scheduling Category", "High"),  # This is REG_SZ
        ("Games SFIO Priority",
         r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "SFIO Priority", "High"),  # REG_SZ
        ("Games Task Priority",
         r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
         "Priority", "6"),
    ]

    for name, key, val, data in tweaks:
        vtype = "REG_SZ" if data in ("High", "Low", "Normal") else "REG_DWORD"
        ok = _reg(key, val, data, vtype)
        status = "✓" if ok else "✗"
        log.info(f"  {status} {name}")
        results.append({"name": name, "ok": ok})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Memory Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_memory_tweaks() -> list[dict]:
    log.info("Applying memory optimizations...")
    results = []
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "mem_mgmt")

    tweaks = [
        # Disable paging executive — keep kernel in RAM
        # v3.3.1-beta.7: removed four placebos that haven't done
        # anything on Windows 10/11 with ≥8 GB RAM:
        #   - DisablePagingExecutive=1: kernel paging behavior; modern
        #     NT memory manager keeps kernel resident when there's
        #     pressure-free RAM.  Mattered on 1-2 GB XP/Vista systems.
        #   - LargeSystemCache=0: Win2000/XP working-set bias.
        #     Modern NT memory manager ignores the value.
        #   - ClearPageFileAtShutdown=1: security/forensics setting, not
        #     a performance setting.  Adds 30-120 s to shutdown by
        #     zeroing the entire pagefile.  Comment said "perf"; this
        #     was actively making shutdown slower.
        #   - Ndu Start=4: the "memory leak" was a Win8 bug Microsoft
        #     fixed in 2017.  Disabling NDU just blanks Task Manager's
        #     per-process Network tab.
    ]

    for name, key, val, data in tweaks:
        ok = _reg(key, val, data)
        status = "✓" if ok else "✗"
        log.info(f"  {status} {name}")
        results.append({"name": name, "ok": ok})

    # v3.3.1-beta.8: Memory Compression disable gated behind Pro Mode.
    # On 16 GB or less, disabling Memory Compression makes pagefile
    # pressure worse — Compression on a Zen4/Alder Lake CPU is faster
    # than swap on an NVMe.  Only helps users with 32 GB+ AND a
    # game/workload that's actually memory-bound on its working set
    # (rare).  Without RAM detection, we just gate it.
    if _pro_mode_enabled():
        r = run_ps("Disable-MMAgent -MemoryCompression -ErrorAction SilentlyContinue")
        ok = r["ok"] or "already" in r["err"].lower()
        log.info(f"  {'✓' if ok else '✗'} [Pro] Disable Memory Compression")
        results.append({"name": "[Pro] Disable Memory Compression", "ok": ok})
    else:
        results.append({"name": "Memory Compression disable skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Storage Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_storage_tweaks() -> list[dict]:
    log.info("Applying storage optimizations...")
    results = []
    drive_type = detect_drive_type()

    # fsutil tweaks
    fs_tweaks = [
        ("Disable Last Access Time", ["fsutil", "behavior", "set", "disablelastaccess", "1"]),
        # v3.3.1-beta.7: removed two placebos on modern Windows:
        #   - disable8dot3: Win10/11 already does the right thing
        #     per-volume by default (off on data volumes, on for
        #     system).  fsutil only affects newly-created files
        #     anyway; existing 8.3 names aren't stripped.  IO impact
        #     unmeasurable.
        #   - NTFS memoryusage=2: Doubles paged-pool budget for NTFS
        #     metadata cache.  Mattered on 2-4 GB Windows installs;
        #     on a 16 GB+ system the cache already fits.
    ]
    for name, cmd in fs_tweaks:
        # cmd is ["fsutil", "behavior", "set", flag, value] — set through
        # sys_state, which notes the flag's previous value first.
        r = {"ok": sys_state.set_fsutil_behavior(cmd[3], int(cmd[4]))}
        log.info(f"  {'✓' if r['ok'] else '✗'} {name}")
        results.append({"name": name, "ok": r["ok"]})

    # v3.3.1-beta.8: Windows Search service disable gated behind Pro Mode.
    # Disabling WSearch breaks Start Menu search, Outlook search,
    # File Explorer search, and Cortana voice-anything.  Modern NVMe
    # indexing is <1% CPU steady-state.  Most users notice within a
    # day that Start Menu search returns nothing — high support-ticket
    # rate from tools like this when applied by default.
    if _pro_mode_enabled():
        r = {"ok": sys_state.set_service_start("WSearch", "disabled", stop=True)}
        log.info(f"  {'✓' if r['ok'] else '✗'} [Pro] Disable Search Indexing")
        results.append({"name": "[Pro] Disable Search Indexing", "ok": r["ok"]})
    else:
        results.append({"name": "Search Indexing disable skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # Disable Storage Sense
    ok = _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy",
              "01", "0")
    log.info(f"  {'✓' if ok else '✗'} Disable Storage Sense")
    results.append({"name": "Disable Storage Sense", "ok": ok})

    # SSD/NVMe: verify TRIM
    if drive_type in ("ssd", "nvme"):
        r = run_cmd(["fsutil", "behavior", "query", "disabledeletenotify"])
        trim_ok = "0" in r.get("out", "")
        log.info(f"  {'✓' if trim_ok else '⚠'} TRIM {'enabled' if trim_ok else 'check manually'}")
        results.append({"name": "TRIM Enabled", "ok": trim_ok})

        # v3.3.1-beta.7: removed the "disable SSD defrag schedule" tweak.
        # Windows 10/11's scheduled "Defrag" task is NOT defragging SSDs —
        # it's running TRIM/Retrim on the weekly schedule, which is
        # exactly what SSDs want.  Disabling the task disables TRIM and
        # silently degrades long-term SSD performance.  Classic
        # "optimizer guide" footgun.

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Gaming Tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_gaming_tweaks() -> list[dict]:
    log.info("Applying gaming optimizations...")
    results = []
    backup_registry(r"HKCU\System\GameConfigStore", "game_config")
    backup_registry(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "gpu_sched")

    tweaks = [
        # Disable Game DVR
        ("Disable Game DVR",
         r"HKCU\System\GameConfigStore", "GameDVR_Enabled", "0"),
        ("Disable Game DVR (Policy)",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR", "AllowGameDVR", "0"),
        # beta.5: relabeled — AllowAutoGameMode=1 ENABLES Windows Game Mode
        # (foreground-game prioritization), which is the intent; the old
        # "Disable Game Bar" label was misleading (1 = on, not off).  The
        # Game Bar overlay itself is disabled by the UseNexus entry below
        # plus the Game DVR keys above.
        ("Enable Auto Game Mode",
         r"HKCU\Software\Microsoft\GameBar", "AllowAutoGameMode", "1"),
        # Disable the Game Bar overlay/nexus
        ("Disable Game Bar Overlay",
         r"HKCU\Software\Microsoft\GameBar", "UseNexusForGameBarEnabled", "0"),
        # v3.3.1-beta.7: removed global FSO-disable (GameDVR_FSEBehaviorMode=2).
        # The _reset_gaming_tweaks block's own comment correctly identifies
        # that this breaks viewport rendering in Roblox Studio, Unity,
        # Unreal Editor, and 3D modeling tools.  Microsoft's per-app FSO
        # override (right-click .exe → Compatibility → "Disable fullscreen
        # optimizations") is the correct path for users who need it on
        # specific titles — applying it globally has measurable side
        # effects across creative tooling.
        # Hardware-accelerated GPU scheduling
        ("HW GPU Scheduling",
         r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", "2"),
    ]

    for name, key, val, data in tweaks:
        ok = _reg(key, val, data)
        status = "✓" if ok else "✗"
        log.info(f"  {status} {name}")
        results.append({"name": name, "ok": ok})

    # Windows 11 "Optimizations for windowed games": borderless and windowed
    # DirectX 10/11 games use the flip presentation model, which reaches
    # exclusive-fullscreen latency and lets VRR work in borderless.  Until now
    # only Competitive Latency switched it on, for its own session.
    ok = _enable_windowed_flip_model()
    log.info(f"  {'✓' if ok else '✗'} Optimizations for windowed games")
    results.append({"name": "Optimizations for windowed games (flip model)", "ok": ok})

    # Timer resolution — bcdedit
    # v3.3.1-beta.7: removed `useplatformtick yes`.  On Zen2+/Skylake+
    # systems with reliable invariant TSC, Windows already uses TSC
    # for tick interrupts; the bcdedit flag is a no-op.  On older
    # platforms it forces HPET/ACPI-based ticks which RAISES idle
    # wakeups — opposite of what gamers want.
    # `disabledynamictick yes` is genuinely risky (murders laptop
    # battery, occasional clock_watchdog_timeout BSOD on some Ryzen
    # platforms) — moving it to pro-mode in beta.8.  Removing the
    # duplicate-write from gaming category now.
    bcdedit_cmds = [
    ]
    for name, cmd in bcdedit_cmds:
        r = run_cmd(cmd)
        log.info(f"  {'✓' if r['ok'] else '✗'} {name}")
        results.append({"name": name, "ok": r["ok"]})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Visual Performance
# ═══════════════════════════════════════════════════════════════════════════
def apply_visual_tweaks() -> list[dict]:
    log.info("Applying visual performance tweaks...")
    results = []
    backup_registry(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "visual_fx")

    tweaks = [
        # Set visual effects to best performance
        ("Visual Effects Best Perf",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
         "VisualFXSetting", "2"),
        # Disable animations
        ("Disable Menu Animation",
         r"HKCU\Control Panel\Desktop", "MenuShowDelay", "0"),
        ("Disable Window Animation",
         r"HKCU\Control Panel\Desktop\WindowMetrics", "MinAnimate", "0"),
        # Disable transparency
        ("Disable Transparency",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
         "EnableTransparency", "0"),
        # Disable Aero Shake
        ("Disable Aero Shake",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "DisallowShaking", "1"),
        # Disable Snap Assist animation
        ("Disable Snap Flyout",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "SnapAssist", "0"),
    ]

    for name, key, val, data in tweaks:
        vtype = "REG_SZ" if val == "MenuShowDelay" or val == "MinAnimate" else "REG_DWORD"
        ok = _reg(key, val, data, vtype)
        log.info(f"  {'✓' if ok else '✗'} {name}")
        results.append({"name": name, "ok": ok})

    # Apply UserPreferencesMask for all visual effects off
    # (Through _reg rather than PowerShell, so the user's own mask is on
    # record and Reset All restores it instead of a generic "all on" one.)
    r = {"ok": _reg(r"HKCU\Control Panel\Desktop", "UserPreferencesMask",
                    "9012038010000000", "REG_BINARY")}
    log.info(f"  {'✓' if r['ok'] else '✗'} UserPreferencesMask (all effects off)")
    results.append({"name": "UserPreferencesMask", "ok": r["ok"]})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Startup scan & manage
# ═══════════════════════════════════════════════════════════════════════════
def scan_startup_items() -> list[dict]:
    """List startup items with their enable/disable status."""
    r = run_ps(
        "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location,User | ConvertTo-Json"
    )
    items = []
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            for item in data:
                items.append({
                    "name": item.get("Name", ""),
                    "command": item.get("Command", ""),
                    "location": item.get("Location", ""),
                    "user": item.get("User", ""),
                })
        except Exception:
            pass

    # Also check the registry Run keys
    for hive, label in [
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "HKCU\\Run"),
        (r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run", "HKLM\\Run"),
    ]:
        r2 = run_cmd(["reg", "query", hive])
        if r2["ok"] and r2["out"]:
            for line in r2["out"].splitlines():
                line = line.strip()
                if "REG_SZ" in line or "REG_EXPAND_SZ" in line:
                    parts = line.split(maxsplit=2)
                    if len(parts) >= 3:
                        items.append({
                            "name": parts[0],
                            "command": parts[2] if len(parts) > 2 else "",
                            "location": label,
                            "user": "",
                        })
    return items


def disable_startup_item(name: str, location: str) -> dict:
    """Remove a startup entry from the registry."""
    log.info(f"Disabling startup item: {name} ({location})")
    if "HKCU" in location and "Run" in location:
        key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    elif "HKLM" in location and "Run" in location:
        key = r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run"
    else:
        return {"ok": False, "err": "Unsupported location"}

    r = run_cmd(["reg", "delete", key, "/v", name, "/f"])
    return {"ok": r["ok"], "name": name}


# ═══════════════════════════════════════════════════════════════════════════
# Hard Tweaks (aggressive) — user-accepted trade-offs
# ═══════════════════════════════════════════════════════════════════════════
def apply_hard_tweaks() -> list[dict]:
    """
    Aggressive tweaks users explicitly opted into. These sacrifice some
    background-app smoothness and security hardening in exchange for latency.
    Every change is reversible via reset_all_optimizations().
    """
    log.info("Applying HARD tweaks (aggressive)...")
    results = []

    # 0x28 = Short quantum, variable, high foreground — most aggressive value
    ok = _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl",
              "Win32PrioritySeparation", "40")
    results.append({"name": "Win32PrioritySeparation -> 0x28 (max foreground)", "ok": ok})

    # v3.3.1-beta.8: Spectre/Meltdown/HVCI/MPO disable gated behind Pro Mode.
    # All three give measurable CPU/GPU gains in pathological workloads but
    # carry real side effects: Spectre/HVCI weaken kernel hardening against
    # bring-your-own-vulnerable-driver attacks; MPO disable fixed an
    # NVIDIA driver bug from 2021-2022 that has been resolved in 537+
    # drivers and now disabling MPO actively *causes* HDR/G-Sync hand-off
    # issues + tearing in borderless windowed mode.
    if _pro_mode_enabled():
        # Disable Spectre / Meltdown CPU mitigations
        base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
        _reg(base, "FeatureSettingsOverride", "3")
        _reg(base, "FeatureSettingsOverrideMask", "3")
        results.append({"name": "[Pro] Spectre/Meltdown mitigations disabled", "ok": True})

        # Disable VBS / HVCI (Memory Integrity)
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard",
             "EnableVirtualizationBasedSecurity", "0")
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
             "Enabled", "0")
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\CredentialGuard",
             "Enabled", "0")
        results.append({"name": "[Pro] VBS / HVCI disabled (Memory Integrity off)", "ok": True})

        # Disable MPO (Multi-Plane Overlay)
        _reg(r"HKLM\SOFTWARE\Microsoft\Windows\Dwm", "OverlayTestMode", "5")
        results.append({"name": "[Pro] MPO disabled (legacy NVIDIA stutter workaround)", "ok": True})
    else:
        results.append({"name": "Spectre / HVCI / MPO disable skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # Disable mouse acceleration (raw input for gaming)
    mkey = r"HKCU\Control Panel\Mouse"
    _reg(mkey, "MouseSpeed", "0", "REG_SZ")
    _reg(mkey, "MouseThreshold1", "0", "REG_SZ")
    _reg(mkey, "MouseThreshold2", "0", "REG_SZ")
    results.append({"name": "Mouse acceleration disabled", "ok": True})

    # Disable Sticky/Filter/Toggle keys prompts (they break games)
    _reg(r"HKCU\Control Panel\Accessibility\StickyKeys", "Flags", "506", "REG_SZ")
    _reg(r"HKCU\Control Panel\Accessibility\ToggleKeys", "Flags", "58", "REG_SZ")
    _reg(r"HKCU\Control Panel\Accessibility\Keyboard Response", "Flags", "122", "REG_SZ")
    results.append({"name": "Accessibility key prompts disabled", "ok": True})

    # Disable Windows Update P2P delivery optimization (eats bandwidth in background)
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization",
         "DODownloadMode", "0")
    results.append({"name": "Windows Update P2P disabled", "ok": True})

    # Disable background apps
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
         "GlobalUserDisabled", "1")
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
         "LetAppsRunInBackground", "2")
    results.append({"name": "Background apps disabled", "ok": True})

    # Disable Windows Tips, Spotlight, Suggestions, Content Delivery
    cdm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
    for val, data in [
        ("SilentInstalledAppsEnabled", "0"),
        ("SystemPaneSuggestionsEnabled", "0"),
        ("SoftLandingEnabled", "0"),
        ("RotatingLockScreenEnabled", "0"),
        ("RotatingLockScreenOverlayEnabled", "0"),
        ("SubscribedContent-338388Enabled", "0"),  # Start suggestions
        ("SubscribedContent-338389Enabled", "0"),  # Tips
        ("SubscribedContent-310093Enabled", "0"),  # Welcome
        ("OemPreInstalledAppsEnabled", "0"),
        ("PreInstalledAppsEnabled", "0"),
        ("PreInstalledAppsEverEnabled", "0"),
    ]:
        _reg(cdm, val, data)
    results.append({"name": "Tips/Spotlight/Suggestions disabled", "ok": True})

    # Turn off Windows 11 Widgets & Copilot button
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests", "0")
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "TaskbarDa", "0")  # Widgets icon off
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
         "ShowCopilotButton", "0")
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
         "TurnOffWindowsCopilot", "1")
    results.append({"name": "Widgets + Copilot button disabled", "ok": True})

    # Disable Recall (Copilot+ snapshot feature — privacy + perf)
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
         "DisableAIDataAnalysis", "1")
    _reg(r"HKCU\Software\Policies\Microsoft\Windows\WindowsAI",
         "DisableAIDataAnalysis", "1")
    results.append({"name": "Windows Recall disabled", "ok": True})

    # Disable Auto HDR (causes issues on non-HDR displays + SDR games)
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings",
         "EnableAutoHdr", "0")
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings",
         "EnableHDRForPlayback", "0")
    results.append({"name": "Auto HDR disabled", "ok": True})

    # Restore Windows 10-style right-click context menu (faster in Win11 Explorer)
    run_cmd(["reg", "add",
             r"HKCU\Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32",
             "/ve", "/t", "REG_SZ", "/d", "", "/f"])
    results.append({"name": "Win10 context menu restored (fewer clicks)", "ok": True})

    # Disable SearchUI / Search in taskbar
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search",
         "SearchboxTaskbarMode", "0")
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search",
         "BingSearchEnabled", "0")
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search",
         "CortanaConsent", "0")
    results.append({"name": "Taskbar search + Bing integration disabled", "ok": True})

    # Disable Xbox Live services (unless running an Xbox-required game)
    for svc in _XBOX_SERVICES:
        # manual start (sc calls it "demand"), not disabled — some games hit it
        sys_state.set_service_start(svc, "demand", stop=True)
    results.append({"name": "Xbox services set to manual", "ok": True})

    # v3.3.1-beta.8: TDR delay raises gated behind Pro Mode.  Default
    # is 2s on Win10/11.  Raising to 10s/20s means a hung GPU driver
    # now hangs the whole desktop for 10s before recovery — which is
    # what ML/CUDA workloads want but the opposite of what gamers
    # want.  Modern game crashes due to TDR are vanishingly rare.
    if _pro_mode_enabled():
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
             "TdrDelay", "10")
        _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
             "TdrDdiDelay", "20")
        results.append({"name": "[Pro] GPU TDR timeouts increased", "ok": True})
    else:
        results.append({"name": "GPU TDR raise skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # Disable Fault-Tolerant Heap (can stutter when it kicks in for misbehaving apps)
    _reg(r"HKLM\SOFTWARE\Microsoft\FTH", "Enabled", "0")
    results.append({"name": "Fault-Tolerant Heap disabled", "ok": True})

    log.info("HARD tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Latency Tweaks — target DPC/ISR latency specifically
# ═══════════════════════════════════════════════════════════════════════════
def apply_latency_tweaks() -> list[dict]:
    """Target DPC/ISR latency — timer precision, interrupt affinity.

    v3.3.1-beta.8: the truly risky bits (HPET disable, dynamictick,
    blanket USB MSI mode) are gated behind Pro Mode now.  Apply All
    in non-Pro Mode still does the safe latency tweaks (tscsyncpolicy
    enhanced, DPC watchdog profile off, NIC MSI mode — Ethernet only).
    """
    log.info("Applying latency tweaks...")
    results = []

    # v3.3.1-beta.8: HPET disable + disabledynamictick gated behind
    # Pro Mode.
    #   HPET — most modern systems use TSC for the kernel clock and
    #     ignore HPET; on a non-trivial minority (older Intel chipsets,
    #     some AMD X470/B450 boards, hypervisor dual-boot configs)
    #     disabling HPET causes clock drift, monitor wake-from-sleep
    #     failures, and occasional BSOD on resume.
    #   disabledynamictick — forces the kernel to keep ticking at the
    #     full timer rate even when idle.  Murders laptop battery
    #     (15-30%) and can cause clock_watchdog_timeout BSODs on some
    #     Ryzen platforms.  Modern games raise timer resolution via
    #     timeBeginPeriod themselves; they don't benefit from this.
    if _pro_mode_enabled():
        r = run_ps(
            "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or "
            "$_.FriendlyName -like '*HPET*'} | Disable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue"
        )
        results.append({"name": "[Pro] HPET disabled", "ok": True})
    else:
        results.append({"name": "HPET disable skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # bcdedit timer tweaks.  v3.3.1-beta.8: dynamictick gated; the
    # other three (useplatformclock no, useplatformtick yes had been
    # removed earlier, tscsyncpolicy enhanced) are safe enough to
    # always apply.
    bcd = sys_state.bcd_values()    # one read; each element's previous value is noted before it changes
    for name, element, value in [
        ("bcdedit useplatformclock no", "useplatformclock", "no"),
        ("bcdedit tscsyncpolicy enhanced", "tscsyncpolicy", "Enhanced"),
    ]:
        results.append({"name": name, "ok": sys_state.set_bcd(element, value, current=bcd)})

    if _pro_mode_enabled():
        ok = sys_state.set_bcd("disabledynamictick", "yes", current=bcd)
        results.append({"name": "[Pro] bcdedit disabledynamictick yes", "ok": ok})
    else:
        results.append({"name": "disabledynamictick skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # DPC watchdog
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel",
         "DpcWatchdogProfileOffset", "0")
    results.append({"name": "DPC watchdog profile off", "ok": True})

    # Enable MSI mode on all network adapters (reduces interrupt overhead)
    _enable_msi_mode("Net")
    results.append({"name": "Network adapters MSI mode enabled", "ok": True})

    # v3.3.1-beta.8: blanket USB MSI mode is gated behind Pro Mode.
    # Some USB host controllers (and their drivers) don't properly
    # support MSI; forcing it on those causes Code 10 errors, mouse
    # stutter, USB DAC dropouts, and "device descriptor request
    # failed" on plug events.  Best practice is per-device, audited
    # via LatencyMon — not blanket.  NIC MSI mode above is safe
    # (modern NIC drivers all support MSI properly).
    if _pro_mode_enabled():
        _enable_msi_mode("USB", working_only=True)
        results.append({"name": "[Pro] USB controllers MSI mode enabled", "ok": True})
    else:
        results.append({"name": "USB MSI mode skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # DPC latency: disable power throttling
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
         "PowerThrottlingOff", "1")
    results.append({"name": "Power throttling globally disabled", "ok": True})

    log.info("Latency tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Windows 11-specific tweaks
# ═══════════════════════════════════════════════════════════════════════════
def apply_windows11_tweaks() -> list[dict]:
    """Win11-only UX bloat — taskbar, Start, Recall, telemetry additions."""
    log.info("Applying Windows 11 specific tweaks...")
    results = []

    # Taskbar: align left (less cursor travel), hide chat icon, hide task view
    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    _reg(advanced, "TaskbarAl", "0")      # left-align
    _reg(advanced, "TaskbarMn", "0")      # chat off
    _reg(advanced, "ShowTaskViewButton", "0")
    results.append({"name": "Taskbar decluttered", "ok": True})

    # Start menu: no recommended files
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer",
         "HideRecommendedSection", "1")
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer",
         "HideRecentlyAddedApps", "1")
    results.append({"name": "Start menu recommendations hidden", "ok": True})

    # Disable File Explorer compact view + ads
    _reg(advanced, "UseCompactMode", "0")
    _reg(advanced, "ShowSyncProviderNotifications", "0")  # OneDrive ads
    results.append({"name": "File Explorer ads disabled", "ok": True})

    # Disable "News and interests" (lock screen)
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Feeds",
         "ShellFeedsTaskbarViewMode", "2")
    results.append({"name": "News & interests disabled", "ok": True})

    # Disable Phone Link
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\MobilityCenter",
         "RunOnServerStartup", "0")
    results.append({"name": "Phone Link startup disabled", "ok": True})

    # Disable Windows Spotlight on desktop
    _reg(r"HKCU\Software\Policies\Microsoft\Windows\CloudContent",
         "DisableSpotlightCollectionOnDesktop", "1")
    results.append({"name": "Desktop Spotlight disabled", "ok": True})

    # Explorer: always show file extensions + hidden files (security + clarity)
    _reg(advanced, "HideFileExt", "0")
    _reg(advanced, "Hidden", "1")
    results.append({"name": "File extensions + hidden files shown", "ok": True})

    # Disable the Windows 11 animated "suggestions" popup under Start
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement",
         "ScoobeSystemSettingEnabled", "0")
    results.append({"name": "Start animated suggestions disabled", "ok": True})

    # Disable Clipboard history + cloud sync
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
         "AllowClipboardHistory", "0")
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
         "AllowCrossDeviceClipboard", "0")
    results.append({"name": "Clipboard history + sync disabled", "ok": True})

    # Disable Web search in Windows Search
    _reg(r"HKCU\Software\Policies\Microsoft\Windows\Explorer",
         "DisableSearchBoxSuggestions", "1")
    results.append({"name": "Web search in Start disabled", "ok": True})

    log.info("Windows 11 tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Full optimize (convenience)
# ═══════════════════════════════════════════════════════════════════════════
def run_full_optimize(categories: list[str] | None = None) -> dict:
    """Run all optimization categories. Pass a list to select specific ones."""
    log.info("═══ STARTING FULL OPTIMIZATION ═══")
    # v3.1 — auto-snapshot before any bulk apply.  Cheap (~1s), safe,
    # gives the user one-click undo if any tweak breaks something.
    try:
        from core import snapshots
        cats_label = ",".join(categories) if categories else "all"
        snap_id = snapshots.auto_snapshot_before(f"apply {cats_label}")
        if snap_id:
            log.info(f"Pre-apply snapshot taken: {snap_id}")
    except Exception as e:
        log.debug(f"Pre-apply snapshot failed (continuing anyway): {e}")
    all_cats = ["power", "cpu", "memory", "storage", "gaming", "visual",
                "hard", "latency", "win11",
                # v3 first wave (audio/input/search/defender/boot/legacy)
                "audio", "input", "search", "defender", "boot", "legacy",
                # v3 second wave (~100 more tweaks)
                "network_stack", "telemetry_deep", "edge", "update_control",
                "notifications", "power_advanced", "app_privacy",
                "explorer", "smartscreen", "lock_screen"]
    if categories is None:
        categories = all_cats

    dispatch = {
        "power":    apply_ultimate_power_plan,
        "cpu":      apply_cpu_tweaks,
        "memory":   apply_memory_tweaks,
        "storage":  apply_storage_tweaks,
        "gaming":   apply_gaming_tweaks,
        "visual":   apply_visual_tweaks,
        "hard":     apply_hard_tweaks,
        "latency":  apply_latency_tweaks,
        "win11":    apply_windows11_tweaks,
        "audio":    apply_audio_tweaks,
        "input":    apply_input_tweaks,
        "search":   apply_search_tweaks,
        "defender": apply_defender_gaming_tweaks,
        "boot":     apply_boot_tweaks,
        "legacy":   apply_legacy_tweaks,
        # v3 second wave
        "network_stack":   apply_network_stack_tweaks,
        "telemetry_deep":  apply_telemetry_deep_tweaks,
        "edge":            apply_edge_tweaks,
        "update_control":  apply_update_control_tweaks,
        "notifications":   apply_notification_tweaks,
        "power_advanced":  apply_power_advanced_tweaks,
        "app_privacy":     apply_app_privacy_tweaks,
        "explorer":        apply_explorer_tweaks,
        "smartscreen":     apply_smartscreen_tweaks,
        "lock_screen":     apply_lock_screen_tweaks,
    }
    results = {}
    for cat in categories:
        fn = dispatch.get(cat)
        if fn:
            results[cat] = fn()
    log.info("═══ OPTIMIZATION COMPLETE ═══")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# RESET ALL — revert every optimization back to Windows defaults
# ═══════════════════════════════════════════════════════════════════════════
def reset_all_optimizations() -> dict:
    """Revert ALL optimizer + kernel + scheduler tweaks back to safe Windows defaults.
    This is a COMPLETE factory-style reset of every performance tweak Vispora touches."""
    log.info("=== RESETTING ALL OPTIMIZATIONS TO DEFAULTS ===")
    # v3.1 — auto-snapshot before reset, since reset is destructive
    # (overwrites every tweak the user has accumulated, possibly across
    # weeks of testing).  If they wanted to undo a single tweak instead,
    # they can restore this snapshot and apply just the one they wanted gone.
    try:
        from core import snapshots
        snap_id = snapshots.auto_snapshot_before("reset all optimizations")
        if snap_id:
            log.info(f"Pre-reset snapshot taken: {snap_id}")
    except Exception as e:
        log.debug(f"Pre-reset snapshot failed (continuing anyway): {e}")
    results = {}
    # The per-category resets below write stock defaults — for what an older
    # build changed without keeping a record, and nothing else (see
    # _run_category_resets).  They must not be recorded as "originals"
    # themselves, and they are not the last word: once they have run, every
    # value whose real prior state we have on record is put back to exactly
    # that (reg_originals.restore_all).
    with reg_originals.suspended():
        results.update(_run_category_resets())

    restored = reg_originals.restore_all("optimizer")
    results["originals"] = [{
        "name": (f"{restored['restored']} setting(s) restored to the exact value they had "
                 f"before Vispora changed them"),
        "ok": restored["failed"] == 0,
    }]
    # ...and the same for everything that is not a registry value: service
    # start types, scheduled tasks, boot entries, TCP parameters, Windows
    # features, Defender preferences and exclusions (core/sys_state.py).
    results["system"] = sys_state.restore_all("optimizer") or [
        {"name": "Services, tasks, boot, network, Windows features: nothing on record to restore", "ok": True}]

    # The start-up prep re-applies a handful of these same tweaks, and the
    # High Performance plan, every time the app opens — so a reset used to
    # last until the next launch.  It now stays off until asked for again.
    try:
        from core import boot_prep
        boot_prep.set_persistent_allowed(False)
    except Exception as e:
        log.debug(f"could not switch off start-up tweaks: {e}")
    # The game monitor puts a saved "normal" state back when a game closes.
    # That snapshot predates this reset.
    try:
        from core import game_profiles
        game_profiles.refresh_baseline_after_reset()
    except Exception as e:
        log.debug(f"could not refresh the game-monitor baseline: {e}")

    # 3.4.2 — also re-enable any Windows services the debloater disabled.
    # Previously a "Reset ALL optimizations" left debloated services
    # (WSearch, Geolocation, etc.) permanently disabled with no undo.
    # restore_services() puts each back to its recorded original Start
    # type; one that is disabled with no record is not touched.
    try:
        from core import debloater
        services = debloater.restore_services()
        results["debloat_services"] = [
            {"name": f"{r.get('id')} service → {r.get('start') or 'could not be restored'}", "ok": bool(r.get("ok"))}
            for r in services.get("results", [])]
        if services.get("unrecorded"):
            results["debloat_services"].append({
                "name": (f"Left disabled — no previous start type on record: {', '.join(services['unrecorded'])} "
                         f"(if an early version of the debloater did that, services.msc switches them back on)"),
                "ok": True})
        results["debloat_tasks"] = debloater.restore_scheduled_tasks().get("results", [])
    except Exception as e:
        log.warning(f"service restore during reset failed: {e}")
        results["debloat_services"] = [{"name": "service restore", "ok": False, "err": str(e)}]

    # Restart DWM to apply visual changes immediately
    run_cmd(["taskkill", "/f", "/im", "dwm.exe"])  # DWM auto-restarts
    results["dwm"] = [{"name": "DWM restarted (applies visual changes)", "ok": True}]

    log.info("=== ALL OPTIMIZATIONS RESET — REBOOT RECOMMENDED ===")
    return results


def _run_category_resets() -> dict:
    """The fixed-default fallbacks, category by category.

    One rule for all of them (_reg_default, _service_default, _bcd_default):
    a default is applied only to something that has no record AND still
    reads exactly the way the tweak leaves it.  So on a machine no tweak was
    applied to, and on a second Reset All, these change nothing.  Where it is
    not known what a tweak wrote, there is no fallback for it at all."""
    results = {}
    results["power"] = _reset_power_plan()
    results["cpu"] = _reset_cpu_tweaks()
    results["memory"] = _reset_memory_tweaks()
    results["storage"] = _reset_storage_tweaks()
    results["gaming"] = _reset_gaming_tweaks()
    results["visual"] = _reset_visual_tweaks()
    results["gpu_rendering"] = _reset_gpu_rendering()
    results["scheduler"] = _reset_scheduler_tweaks()
    results["hard"] = _reset_hard_tweaks()
    results["latency"] = _reset_latency_tweaks()
    results["win11"] = _reset_win11_tweaks()
    # v3 — reset the new categories too
    results["audio"]    = _reset_audio_tweaks()
    results["input"]    = _reset_input_tweaks()
    results["search"]   = _reset_search_tweaks()
    results["defender"] = _reset_defender_gaming_tweaks()
    results["boot"]     = _reset_boot_tweaks()
    results["legacy"]   = _reset_legacy_tweaks()
    # v3 second wave
    results["network_stack"]  = _reset_network_stack_tweaks()
    results["telemetry_deep"] = _reset_telemetry_deep_tweaks()
    results["edge"]           = _reset_edge_tweaks()
    results["update_control"] = _reset_update_control_tweaks()
    results["notifications"]  = _reset_notification_tweaks()
    results["power_advanced"] = _reset_power_advanced_tweaks()
    results["app_privacy"]    = _reset_app_privacy_tweaks()
    results["explorer"]       = _reset_explorer_tweaks()
    results["smartscreen"]    = _reset_smartscreen_tweaks()
    results["lock_screen"]    = _reset_lock_screen_tweaks()
    return results


def _reset_power_plan() -> list[dict]:
    """Restore the user's pre-Vispora power plan.

    v3.3.1-beta.7: previously this hard-coded Balanced as the reset
    target, ignoring `power_baseline.json` captured by
    `_capture_power_baseline()`.  Users on High Performance, AMD
    Ryzen Balanced, or a manually-tuned custom plan got their plan
    silently replaced with Microsoft Balanced on every reset.  Now
    we honor the captured baseline first and only fall back to
    Balanced if no baseline file exists — and the machine is on one of
    the plans this app switches to.
    """
    log.info("Resetting power plan to captured baseline (or Balanced fallback)...")
    results = []
    restored = restore_power_baseline()
    if restored.get("ok"):
        _forget_power_baseline()
        results.append({
            "name": f"Power plan restored to user baseline ({restored.get('guid','')})",
            "ok":   True,
        })
        return results
    # No baseline.  Balanced is the fixed default for a machine an older
    # build left on one of our own plans.  Any other plan is the user's
    # choice: this used to move everybody without a baseline to Balanced,
    # whether or not this app had ever switched plans.
    if active_power_plan().lower() not in _own_power_plans():
        results.append({"name": "Power plan left as it is (not one Vispora switched to)", "ok": True})
        return results
    log.info(f"  No baseline file ({restored.get('err','')}) — falling back to Balanced")
    r = run_cmd(["powercfg", "-setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"])
    results.append({"name": "Balanced Power Plan (fallback — no baseline file)", "ok": r["ok"]})
    return results


def _reset_cpu_tweaks() -> list[dict]:
    log.info("Resetting CPU tweaks...")
    results = []
    # name, key, value, what the tweak leaves there, the Windows default
    tweaks = [
        # 38 from this page and the start-up prep, 40 from the Hard tweaks and the Kernel page
        ("Win32PrioritySeparation", r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", ("38", "40"), "2"),
        # 10 since 3.3.1; 0 before
        ("SystemResponsiveness", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", ("10", "0"), "20"),
        ("NetworkThrottlingIndex", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "NetworkThrottlingIndex", "4294967295", "10"),
        # The tweak writes 8, which is also the stock value.  0 is what
        # Reset All itself wrote here up to 3.5.2.
        ("GPU Priority (Games)", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "GPU Priority", "0", "8"),
        ("Scheduling Category", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Scheduling Category", "High", "Medium"),
        ("Priority (Games)", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Priority", "6", "2"),
        ("SFIO Priority", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "SFIO Priority", "High", "Normal"),
        # Set to 0 by the start-up prep; was in no reset list at all.
        ("ForegroundLockTimeout", r"HKCU\Control Panel\Desktop", "ForegroundLockTimeout", "0", "200000"),
    ]
    for name, key, val, tweak, data in tweaks:
        vtype = "REG_SZ" if data in ("Medium", "Normal", "High", "Low") else "REG_DWORD"
        ok = _reg_default(key, val, tweak, data, vtype)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})

    # Core parking: the long-removed tweak (and the "unpark cores" registry
    # tweak it came from) set ValueMax of the "core parking min cores" setting
    # to 0, where Windows ships 100.  With 0 there, Windows refuses any other
    # value for that setting, so nothing — game mode included — can keep the
    # E-cores unparked.  (Reset All used to delete ValueMax outright, on every
    # machine; a value that reads 100, or anything but 0, is left alone.)
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\54533251-82be-4824-96c1-47b60b740d00"
                 r"\0cc5b647-c1df-4637-891a-dec35c318583", "ValueMax", "0", "100")

    # Re-enable CPU idle where an older build left it switched off without a
    # record — on the plans this app switches the machine to.  (This used to
    # force it on for the ACTIVE plan, which by now is the user's own again:
    # anybody who runs with idle off on purpose lost that at every reset.)
    # A setting that is on record goes back with the final pass of Reset All.
    for plan in sorted(_own_power_plans()):
        if reg_originals.has_power("SUB_PROCESSOR", "IdleDisable", scope=None, scheme=plan):
            continue
        cur = reg_originals.query_power(plan, "SUB_PROCESSOR", "IdleDisable")
        if not cur or cur[0] != 1:
            continue
        run_cmd(["powercfg", "/setacvalueindex", plan, "SUB_PROCESSOR", "IdleDisable", "0"])
        if active_power_plan().lower() == plan:
            run_cmd(["powercfg", "/setactive", plan])
    results.append({"name": "CPU Idle (re-enabled on Vispora's own power plans, where an older version left it off)",
                    "ok": True})

    return results


def _reset_memory_tweaks() -> list[dict]:
    log.info("Resetting memory tweaks...")
    results = []
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    # Tweaks removed in 3.3.1 that older builds applied: each wrote 1.
    # (LargeSystemCache was in this list too.  That tweak wrote 0, the
    # Windows default, so there is nothing of it to put back.)
    for name, val, tweak, data in [
        ("Paging Executive", "DisablePagingExecutive", "1", "0"),
        ("Clear Pagefile", "ClearPageFileAtShutdown", "1", "0"),
    ]:
        ok = _reg_default(base, val, tweak, data)
        results.append({"name": name, "ok": ok})

    # beta.5: was ControlSet001 (hardcoded) — wrong on any system booted from a
    # different control set; the live set is always CurrentControlSet.
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Services\Ndu", "Start", "4", "2")
    results.append({"name": "NDU (re-enabled)", "ok": True})
    run_ps("Enable-MMAgent -MemoryCompression -ErrorAction SilentlyContinue")
    results.append({"name": "Memory Compression (re-enabled)", "ok": True})

    # (The Spectre/Meltdown override is undone in _reset_hard_tweaks.  The two
    # values were also written here first — 0 and 3 — for everybody.)

    return results


def _reset_storage_tweaks() -> list[dict]:
    log.info("Resetting storage tweaks...")
    results = []
    # Fixed defaults — for what an older build changed without keeping a
    # record.  Each is applied only while the flag still reads the way that
    # tweak left it; whatever IS on record is restored exactly by the final
    # pass of Reset All.
    for name, flag, tweak, stock in [
        # 2 = "let Windows decide", the default (this used to write 3)
        ("Last Access Time", "disablelastaccess", 1, 2),
        # 2 = per volume, the default (this used to write 0: "create 8.3
        # names on every volume", which is not how Windows ships)
        ("8.3 Filenames", "disable8dot3", 1, 2),
        ("NTFS Memory Usage", "memoryusage", 2, 1),
    ]:
        if reg_originals.has_state("fsutil", flag) or sys_state.fsutil_behavior(flag) != tweak:
            continue
        r = run_cmd(["fsutil", "behavior", "set", flag, str(stock)])
        results.append({"name": name, "ok": r["ok"]})

    _service_default("WSearch", tweak="disabled", stock="delayed-auto", start=True)
    results.append({"name": "Windows Search (re-enabled)", "ok": True})
    _reg_default(r"HKCU\Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01", "0", "1")
    results.append({"name": "Storage Sense (re-enabled)", "ok": True})

    return results


def _reset_gaming_tweaks() -> list[dict]:
    """Reset ALL gaming tweaks — this is the critical one for Roblox Studio etc."""
    log.info("Resetting gaming tweaks...")
    results = []
    # name, key, value, what the tweak leaves there, the Windows default
    tweaks = [
        # Re-enable Game DVR (Windows default is on)
        ("Game DVR", r"HKCU\System\GameConfigStore", "GameDVR_Enabled", "0", "1"),

        # CRITICAL: Reset Fullscreen Optimizations to DEFAULT (0 = Windows manages it)
        # Setting this to 2 disables FSO globally which breaks viewport rendering in
        # apps like Roblox Studio, Unity, Unreal Editor, 3D modeling tools
        ("FSO Behavior -> Default", r"HKCU\System\GameConfigStore", "GameDVR_FSEBehaviorMode", "2", "0"),
        # (GameDVR_DXGIHonorFSEWindowsCompatible, GameDVR_HonorUserFSEBehaviorMode
        # and GameDVR_EFSEFeatureFlags were set to 0 here as well.  What the
        # tweak removed in 3.3.1 wrote to them is not known, so they are left
        # as they are.)

        # Re-enable Game Bar (some apps use its hooks for window management)
        ("Game Bar", r"HKCU\Software\Microsoft\GameBar", "UseNexusForGameBarEnabled", "0", "1"),
        # (AllowAutoGameMode: the tweak writes 1, and 1 is what this wrote
        # back, so there is nothing to undo without a record.)
    ]
    for name, key, val, tweak, data in tweaks:
        ok = _reg_default(key, val, tweak, data)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})

    # Remove the policy override so Windows uses its own default — the one
    # value the tweak writes.  (This used to delete the whole GameDVR policy
    # key, with whatever else an administrator had put there.)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR", "AllowGameDVR", "0", None)
    results.append({"name": "GameDVR Policy (removed)", "ok": True})

    # Restore bcdedit timer defaults
    bcd = sys_state.bcd_values()
    for element, tweak in (("useplatformtick", "yes"), ("disabledynamictick", "yes"), ("useplatformclock", "no")):
        _bcd_default(bcd, element, tweak)
    results.append({"name": "Timer settings (all restored to default)", "ok": True})

    # Re-enable HPET if it was disabled
    run_ps(
        "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or $_.FriendlyName -like '*HPET*'} "
        "| Enable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue"
    )
    results.append({"name": "HPET (re-enabled)", "ok": True})

    return results


def _reset_gpu_rendering() -> list[dict]:
    """Reset GPU-related settings that affect rendering in ALL apps (not just games).
    This is the key fix for Roblox Studio, Unity, Blender, etc being laggy."""
    log.info("Resetting GPU rendering settings...")
    results = []

    # (HW GPU Scheduling: the tweak writes HwSchMode=2 and so did this reset
    # — 2 is the modern default — so without a record there is nothing to
    # put back.  A value that was different before is on record.)

    # Remove DWM MPO override — let Windows manage Multi-Plane Overlay
    _reg_default(r"HKCU\Software\Microsoft\Windows\DWM", "OverlayMinFPS", "0", None)
    results.append({"name": "DWM OverlayMinFPS (removed)", "ok": True})

    # Remove ThreadDpcEnable override
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel", "ThreadDpcEnable", "0", None)
    results.append({"name": "ThreadDpcEnable (removed)", "ok": True})

    # Reset NVIDIA power settings to default (adaptive, not max performance)
    # — on the NVIDIA adapter's own key.  This was hard-coded to \0000, which
    # on a hybrid laptop is the integrated GPU and can be a virtual adapter.
    # (PowerMizerEnable is not here any more: the GPU tab writes 1, and 1 is
    # what this wrote back.)
    from core import nvidia_clean
    nv_key = nvidia_clean._find_gpu_class_key("NVIDIA", or_first=False)
    for name, val, tweak, data in [
        ("NVIDIA PerfLevelSrc", "PerfLevelSrc", "0x2222", "0x3322"),  # default adaptive
        ("NVIDIA PowerMizerLevel", "PowerMizerLevel", "1", "0"),  # 0 = adaptive (not forced max)
        ("NVIDIA PowerMizerLevelAC", "PowerMizerLevelAC", "1", "0"),
    ]:
        if nv_key:      # (no NVIDIA adapter: nothing to reset)
            results.append({"name": name, "ok": _reg_default(nv_key, val, tweak, data)})

    # (This used to clear EVERY value under DirectX\UserGpuPreferences — the
    # per-app "which GPU" choices from Windows Settings, none of which this
    # app ever writes.  On a dual-GPU laptop that sent games back to the
    # integrated GPU.)

    # (It also set DWM\DisableHWAcceleration to 0 and deleted DWM\Composition.
    # Which tweak, if any, ever wrote those is not known, so whatever is
    # there is left as it is.)

    return results


def _reset_scheduler_tweaks() -> list[dict]:
    """Reset all scheduler/kernel tweaks from the Kernel page."""
    log.info("Resetting scheduler tweaks...")
    results = []

    # Reset MMCSS DisplayPostProcessing to defaults
    dpp_key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\DisplayPostProcessing"
    # The Kernel page's tweak writes this task's stock values (True / 8 / 8 /
    # High), so the tweak itself leaves nothing to put back.  What does need
    # undoing is Reset All as it was up to 3.5.2: it wrote 0 / 2 / Medium
    # here, lower than Windows ships the task.
    # (SFIO Priority was set to Normal here as well.  The tweak writes High;
    # which of the two Windows ships has not been established, so that value
    # is left as it is.)
    for name, val, left, data, vtype in [
        ("DPP GPU Priority", "GPU Priority", "0", "8", "REG_DWORD"),
        ("DPP Priority", "Priority", "2", "8", "REG_DWORD"),
        ("DPP Scheduling", "Scheduling Category", "Medium", "High", "REG_SZ"),
    ]:
        ok = _reg_default(dpp_key, val, left, data, vtype)
        results.append({"name": name, "ok": ok})

    return results


def _reset_visual_tweaks() -> list[dict]:
    """Reset visual tweaks — FULLY restore Windows default visual effects.
    This uses SystemParametersInfo to properly tell Windows to use its defaults,
    not just set registry values."""
    log.info("Resetting visual effects...")
    results = []

    # name, key, value, what the tweak leaves there, the Windows default, type
    tweaks = [
        # VisualFXSetting 0 = Let Windows choose best for this computer
        ("Visual Effects", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting", "2", "0", "REG_DWORD"),
        # Menu delay default = 400ms
        ("Menu Animation Delay", r"HKCU\Control Panel\Desktop", "MenuShowDelay", "0", "400", "REG_SZ"),
        # MinAnimate default = 1 (on)
        ("Window Animation", r"HKCU\Control Panel\Desktop\WindowMetrics", "MinAnimate", "0", "1", "REG_SZ"),
        # Transparency default = 1 (on)
        ("Transparency", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", "0", "1", "REG_DWORD"),
        # Aero Shake default = 0 (enabled)
        ("Aero Shake", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "DisallowShaking", "1", "0", "REG_DWORD"),
        # Snap Assist default = 1 (enabled)
        ("Snap Assist", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "SnapAssist", "0", "1", "REG_DWORD"),
        # CRITICAL: UserPreferencesMask back to Windows default "Let Windows choose"
        # This bitmask controls ALL visual effects — animations, shadows, font smoothing,
        # thumbnail previews, etc. The default value enables all effects.
        # Default bytes: 9E 3E 07 80 12 01 00 00 (all effects on)
        ("UserPreferencesMask (all effects ON)", r"HKCU\Control Panel\Desktop", "UserPreferencesMask",
         "9012038010000000", "9E3E078012010000", "REG_BINARY"),
        # (DragFullWindows, FontSmoothing and FontSmoothingType were set to
        # their defaults here too.  No tweak writes them: a user who had
        # switched ClearType off got it back at every reset.)
    ]
    # Is any of these on record to be put back, or still the way the tweak
    # leaves it?  If not, there is no visual tweak here to undo.
    undoing = any(reg_originals.has_original(key, val) or reg_originals.reads_as(key, val, tweak, vtype)
                  for _, key, val, tweak, _, vtype in tweaks)
    for name, key, val, tweak, data, vtype in tweaks:
        ok = _reg_default(key, val, tweak, data, vtype)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})
    if not undoing:
        # What follows changes settings of the running session and saves
        # them in the user's profile.  It used to run at every reset.
        return results

    # Use SystemParametersInfo to apply visual effects change immediately (no reboot needed)
    # This triggers Windows to re-read all the visual effect settings
    run_ps("""
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public class SPI {
    [DllImport("user32.dll")] public static extern bool SystemParametersInfo(int a, int b, int c, int d);
    public static void Refresh() {
        SystemParametersInfo(0x0013, 0, 0, 0x03);  // SPI_SETDRAGFULLWINDOWS
        SystemParametersInfo(0x1017, 0, 1, 0x03);  // SPI_SETCLIENTAREAANIMATION
        SystemParametersInfo(0x1019, 0, 1, 0x03);  // SPI_SETANIMATION
    }
}
'@ -ErrorAction SilentlyContinue
try { [SPI]::Refresh() } catch {}
""")
    results.append({"name": "Visual effects applied immediately", "ok": True})

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Reset: Hard / Latency / Win11
# ═══════════════════════════════════════════════════════════════════════════
def _reset_hard_tweaks() -> list[dict]:
    """Undo apply_hard_tweaks()."""
    log.info("Resetting hard tweaks...")
    results = []

    # Restore Spectre/Meltdown mitigations.  The tweak is the pair 3 / 3.  A
    # mask of 3 on its own is not it — that is also how the mitigations are
    # switched ON by hand — so the mask only goes together with an override
    # that this fallback removes.
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    if not reg_originals.on_record(base, "FeatureSettingsOverride") \
            and reg_originals.reads_as(base, "FeatureSettingsOverride", "3"):
        _reg_default(base, "FeatureSettingsOverride", "3", None)
        _reg_default(base, "FeatureSettingsOverrideMask", "3", None)
    results.append({"name": "Spectre/Meltdown mitigations restored", "ok": True})

    # VBS / HVCI.  Where we have the prior values on record, the final pass
    # of Reset All restores exactly those.  Without a record, only switch
    # them back on if Pro Mode — the one path through which this app turns
    # them off — is enabled.  Forcing them to 1 unconditionally switched
    # Memory Integrity ON for users who never had it and never touched it
    # here, bypassing Windows' own driver-compatibility check.
    dg = r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard"
    hvci = dg + r"\Scenarios\HypervisorEnforcedCodeIntegrity"
    if reg_originals.has_original(dg, "EnableVirtualizationBasedSecurity") \
            or reg_originals.has_original(hvci, "Enabled"):
        results.append({"name": "VBS / HVCI restored to their previous setting", "ok": True})
    elif _pro_mode_enabled():
        # (only while they still read 0, the way the tweak leaves them — a
        # value that was never set is not created here)
        _reg_default(dg, "EnableVirtualizationBasedSecurity", "0", "1")
        _reg_default(hvci, "Enabled", "0", "1")
        results.append({"name": "VBS / HVCI re-enabled", "ok": True})
    else:
        results.append({"name": "VBS / HVCI left as they are (not changed by Vispora)", "ok": True})

    # Remove MPO override
    _reg_default(r"HKLM\SOFTWARE\Microsoft\Windows\Dwm", "OverlayTestMode", "5", None)
    results.append({"name": "MPO restored", "ok": True})

    # Mouse defaults: accel on
    mkey = r"HKCU\Control Panel\Mouse"
    _reg_default(mkey, "MouseSpeed", "0", "1", "REG_SZ")
    _reg_default(mkey, "MouseThreshold1", "0", "6", "REG_SZ")
    _reg_default(mkey, "MouseThreshold2", "0", "10", "REG_SZ")
    results.append({"name": "Mouse acceleration restored", "ok": True})

    # Accessibility key prompts back to defaults
    _reg_default(r"HKCU\Control Panel\Accessibility\StickyKeys", "Flags", "506", "510", "REG_SZ")
    _reg_default(r"HKCU\Control Panel\Accessibility\ToggleKeys", "Flags", "58", "62", "REG_SZ")
    _reg_default(r"HKCU\Control Panel\Accessibility\Keyboard Response", "Flags", "122", "126", "REG_SZ")
    results.append({"name": "Accessibility key prompts restored", "ok": True})

    # Delivery optimization — back to default (1 = LAN only).  0 is this
    # category's tweak; the Windows Update category writes 1 itself.
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization",
                 "DODownloadMode", "0", "1")
    results.append({"name": "Delivery Optimization (LAN only)", "ok": True})

    # Background apps — enable
    _reg_default(r"HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
                 "GlobalUserDisabled", "1", None)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
                 "LetAppsRunInBackground", "2", None)
    results.append({"name": "Background apps restored", "ok": True})

    # Xbox services.  The tweak sets them to Manual — which is also how
    # Windows ships them, so there is no default to go back to.  (This used
    # to set all four to Automatic.)  A start type that WAS different before
    # is on record and comes back with the final pass of Reset All.
    results.append({"name": "Xbox services: Manual is the Windows default", "ok": True})

    # Widgets/Copilot — remove overrides (use Windows default)
    for key, val in [
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa"),
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "ShowCopilotButton"),
        (r"HKLM\SOFTWARE\Policies\Microsoft\Dsh", "AllowNewsAndInterests"),
    ]:
        _reg_default(key, val, "0", None)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", "1", None)
    results.append({"name": "Widgets + Copilot restored", "ok": True})

    # Recall — remove override
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", "1", None)
    _reg_default(r"HKCU\Software\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", "1", None)
    results.append({"name": "Recall policy removed", "ok": True})

    # Auto HDR — remove overrides
    _reg_default(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings", "EnableAutoHdr", "0", None)
    _reg_default(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings", "EnableHDRForPlayback", "0", None)
    results.append({"name": "Auto HDR restored", "ok": True})

    # Remove Win10 context menu override — if it is the tweak's and nothing
    # more: the key with one sub-key, InprocServer32, whose only value is an
    # empty default.  (Deleted unconditionally before, whatever was in it.)
    ctx = r"HKCU\Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"
    if reg_originals.value_names(ctx) == [] \
            and [s.lower() for s in reg_originals.subkeys(ctx)] == ["inprocserver32"] \
            and reg_originals.value_names(ctx + r"\InprocServer32") == [""] \
            and not reg_originals.subkeys(ctx + r"\InprocServer32") \
            and reg_originals.reads_as(ctx + r"\InprocServer32", "", "", "REG_SZ"):
        run_cmd(["reg", "delete", ctx, "/f"])
    results.append({"name": "Win11 context menu restored", "ok": True})

    # Search bar restored
    _reg_default(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "SearchboxTaskbarMode", "0", "2")
    _reg_default(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "BingSearchEnabled", "0", None)
    _reg_default(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "CortanaConsent", "0", None)
    results.append({"name": "Taskbar search restored", "ok": True})

    # TDR timeouts back to default
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "TdrDelay", "10", None)
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "TdrDdiDelay", "20", None)
    results.append({"name": "TDR timeouts restored", "ok": True})

    # Fault-Tolerant Heap restored
    _reg_default(r"HKLM\SOFTWARE\Microsoft\FTH", "Enabled", "0", None)
    results.append({"name": "FTH restored", "ok": True})

    return results


def _reset_latency_tweaks() -> list[dict]:
    """Undo apply_latency_tweaks()."""
    log.info("Resetting latency tweaks...")
    results = []

    # Re-enable HPET
    run_ps(
        "Get-PnpDevice | Where-Object {$_.FriendlyName -like '*High Precision*' -or "
        "$_.FriendlyName -like '*HPET*'} | Enable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue"
    )
    results.append({"name": "HPET re-enabled", "ok": True})

    # Restore bcdedit defaults
    bcd = sys_state.bcd_values()
    for element, tweak in (("useplatformclock", "no"), ("disabledynamictick", "yes"),
                           ("useplatformtick", "yes"), ("tscsyncpolicy", "Enhanced")):
        _bcd_default(bcd, element, tweak)
    results.append({"name": "bcdedit timers restored", "ok": True})

    # Restore power throttling
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
                 "PowerThrottlingOff", "1", None)
    results.append({"name": "Power throttling restored", "ok": True})

    return results


def _reset_win11_tweaks() -> list[dict]:
    """Undo apply_windows11_tweaks()."""
    log.info("Resetting Windows 11 tweaks...")
    results = []

    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    # Taskbar: restore centered align, chat icon, task view
    _reg_default(advanced, "TaskbarAl", "0", "1")
    _reg_default(advanced, "TaskbarMn", "0", "1")
    _reg_default(advanced, "ShowTaskViewButton", "0", "1")

    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer", "HideRecommendedSection", "1", None)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer", "HideRecentlyAddedApps", "1", None)

    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "AllowClipboardHistory", "0", None)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "AllowCrossDeviceClipboard", "0", None)

    _reg_default(r"HKCU\Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", "1", None)

    results.append({"name": "Windows 11 UX restored", "ok": True})
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Audio — MMCSS task tuning + low-latency audio path
# ═══════════════════════════════════════════════════════════════════════════
def apply_audio_tweaks() -> list[dict]:
    """Audio latency + MMCSS Audio task tuning.  Helps voice chat / mic
    latency and prevents audio stutter under load.  No game audio quality
    is sacrificed."""
    log.info("Applying audio optimizations...")
    results = []

    # MMCSS Audio task — same approach as the Games task already has,
    # applied to the Pro Audio system task (used by all WASAPI / DirectSound
    # / Dolby pipelines).
    audio_task = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Pro Audio"
    for name, val, data, vtype in [
        ("Audio Background Only", "Background Only", "False", "REG_SZ"),
        ("Audio GPU Priority",    "GPU Priority",    "8",     "REG_DWORD"),
        ("Audio Priority",        "Priority",        "1",     "REG_DWORD"),
        ("Audio Scheduling",      "Scheduling Category", "High", "REG_SZ"),
        ("Audio SFIO Priority",   "SFIO Priority",   "High",  "REG_SZ"),
        ("Audio Clock Rate",      "Clock Rate",      "10000", "REG_DWORD"),
        ("Audio Affinity",        "Affinity",        "0",     "REG_DWORD"),
    ]:
        _reg(audio_task, val, data, vtype)
        results.append({"name": name, "ok": True})

    # v3.3.1-beta.7: removed `DisableAbsoluteVolume=1`.  The original
    # comment claimed it fixes Bluetooth audio stutter — that was true
    # for the specific Win10 1607-1709 absolute-volume bug.  Microsoft
    # re-enabled absolute volume by default in 1803+ because disabling
    # it actively *causes* the stuttering / one-channel-only / "tinny
    # after pause" symptoms on modern headsets (AirPods, Sony XM4/XM5,
    # Bose QC45, HyperX Cloud, Galaxy Buds).  The headsets people own
    # in 2025-2026 need the protocol on, not off.

    # Disable Audio Service idle disconnect
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Audiosrv",
         "Start", "2")
    results.append({"name": "Audio service auto-start", "ok": True})

    log.info("Audio tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Input — mouse + keyboard responsiveness
# ═══════════════════════════════════════════════════════════════════════════
def apply_input_tweaks() -> list[dict]:
    """Tighten mouse + keyboard latency / response curves.  Different
    from the Hard-Tweak mouse-accel kill — this is response-time focused."""
    log.info("Applying input tweaks...")
    results = []

    # Mouse hover delay (0ms = no delay before tooltip-trigger sniffs)
    _reg(r"HKCU\Control Panel\Mouse", "MouseHoverTime", "0", "REG_SZ")
    results.append({"name": "Mouse hover delay → 0 ms", "ok": True})
    # Mouse hover detection geometry (1px so tooltips don't sniff hover)
    _reg(r"HKCU\Control Panel\Mouse", "MouseHoverWidth", "1", "REG_SZ")
    _reg(r"HKCU\Control Panel\Mouse", "MouseHoverHeight", "1", "REG_SZ")
    results.append({"name": "Mouse hover geometry → 1 px", "ok": True})
    # Mouse trails off
    _reg(r"HKCU\Control Panel\Mouse", "MouseTrails", "0", "REG_SZ")
    results.append({"name": "Mouse trails disabled", "ok": True})
    # Active window tracking off (so cursor doesn't focus windows on hover)
    _reg(r"HKCU\Control Panel\Desktop", "ActiveWindowTracking", "0", "REG_DWORD")
    results.append({"name": "Active window tracking off", "ok": True})

    # Keyboard repeat — fastest
    _reg(r"HKCU\Control Panel\Keyboard", "KeyboardSpeed", "31", "REG_SZ")
    _reg(r"HKCU\Control Panel\Keyboard", "KeyboardDelay", "0",  "REG_SZ")
    results.append({"name": "Keyboard repeat rate → max", "ok": True})

    # Touch keyboard auto-popup off (keyboard pops up at random in Win11)
    _reg(r"HKCU\Software\Microsoft\TabletTip\1.7",
         "EnableAutoInvokeTouchKeyboard", "0")
    results.append({"name": "Touch keyboard auto-popup off", "ok": True})

    # Pen flicks off (ghost-touches on convertibles)
    _reg(r"HKCU\Software\Microsoft\Wisp\Pen\SysEventParameters",
         "FlickMode", "0")
    results.append({"name": "Pen flicks off", "ok": True})

    log.info("Input tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Search — Cortana, Bing, search history & suggestions
# ═══════════════════════════════════════════════════════════════════════════
def apply_search_tweaks() -> list[dict]:
    """Kill Cortana, search history, Bing-in-Start, and the constant
    online suggestions in the Start search box."""
    log.info("Applying search & Cortana tweaks...")
    results = []

    cor = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search"
    for name, val, data in [
        ("Disable Cortana",                        "AllowCortana", "0"),
        ("Disable Cortana on lock screen",         "AllowCortanaAboveLock", "0"),
        ("Disable Cortana voice activation",       "AllowVoiceActivation", "0"),
        ("Disable web search in Start",            "DisableWebSearch", "1"),
        ("Disable web result connection",          "ConnectedSearchUseWeb", "0"),
        ("Disable search highlights",              "EnableDynamicContentInWSB", "0"),
        ("Disable search box suggestions",         "AllowSearchToUseLocation", "0"),
        # v3.3.1-beta.7: removed `PreventIndexingOutlook=1`.  The label
        # claimed "indexer auto-throttle" but the actual effect is just
        # to break Outlook search.  Outlook users would notice within a
        # day that their inbox search returns nothing.
    ]:
        _reg(cor, val, data)
        results.append({"name": name, "ok": True})

    # User-side search/history tweaks
    sh = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search"
    for name, val, data in [
        ("Disable device search history",      "DeviceHistoryEnabled", "0"),
        ("Disable search history",             "HistoryViewEnabled", "0"),
        ("Disable search box auto-suggest",    "BingSearchEnabled", "0"),
        ("Disable Cortana consent",            "CortanaConsent", "0"),
    ]:
        _reg(sh, val, data)
        results.append({"name": name, "ok": True})

    # Cortana service to disabled (won't break anything; Win11 uses Search)
    sys_state.set_service_start("WSearch", "delayed-auto")
    results.append({"name": "Search indexer → delayed start", "ok": True})

    log.info("Search tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Defender — gaming exclusions (NOT disable-all)
# ═══════════════════════════════════════════════════════════════════════════
def apply_defender_gaming_tweaks() -> list[dict]:
    """Tell Microsoft Defender to skip scanning the obvious game-install
    locations.  Defender real-time protection stays on — we just stop it
    eating CPU on Steam shaders, Epic CDN downloads, etc.  This is the
    safe middle ground between 'disable Defender' (no) and 'tank 5–10%
    CPU during play' (no thanks)."""
    log.info("Applying Defender gaming exclusions...")
    results = []

    # What Defender is set to right now — so only exclusions that are not
    # already there get recorded (and removed again by Reset All), and each
    # preference's own previous value is what a reset goes back to.
    prefs = sys_state.defender_prefs()

    # Auto-detect common game-install roots and exclude them
    excluded = []
    for path in _DEFENDER_EXCLUDE_PATHS:
        if os.path.exists(path) and sys_state.add_defender_exclusion("ExclusionPath", path, current=prefs):
            excluded.append(path)
    log.info(f"  Defender exclusions added for {len(excluded)} game folders")
    results.append({"name": f"Defender exclusions: {len(excluded)} game folders", "ok": True})

    # Common game executable patterns
    for proc in _DEFENDER_EXCLUDE_PROCESSES:
        sys_state.add_defender_exclusion("ExclusionProcess", proc, current=prefs)
    results.append({"name": "Defender process exclusions added", "ok": True})

    # Disable Defender Submit Sample Consent (no auto-upload of files to MS)
    sys_state.set_defender_pref("SubmitSamplesConsent", "NeverSend", 2, current=prefs)
    results.append({"name": "Defender sample submission disabled", "ok": True})

    # Disable MAPS (cloud-based protection telemetry — keeps signatures local)
    sys_state.set_defender_pref("MAPSReporting", "Disabled", 0, current=prefs)
    results.append({"name": "Defender cloud telemetry disabled", "ok": True})

    # Disable scheduled scan during high-load hours (set to 4am only)
    sys_state.set_defender_pref("ScanScheduleTime", "04:00:00", "04:00:00", current=prefs)
    sys_state.set_defender_pref("ScanScheduleQuickScanTime", "04:30:00", "04:30:00", current=prefs)
    results.append({"name": "Defender scans → 4 AM only", "ok": True})

    log.info("Defender gaming exclusions applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Boot — fast boot, hibernation, boot menu, GUI boot
# ═══════════════════════════════════════════════════════════════════════════
def apply_boot_tweaks() -> list[dict]:
    """Strip boot-time bloat — Fast Boot off (causes weird state on
    Windows 11), hibernation off, boot menu in legacy mode (no GUI
    splash), boot logging off."""
    log.info("Applying boot tweaks...")
    results = []

    # Disable Fast Boot (HiberBootEnabled) — Win11's "fast startup" caches
    # a hibernation image on shutdown, causing Wake-after-Sleep weirdness
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power",
         "HiberBootEnabled", "0")
    results.append({"name": "Fast Startup disabled", "ok": True})

    # v3.3.1-beta.8: hibernation off gated behind Pro Mode.  On
    # laptops, disabling hibernation removes the user's ability to
    # close the lid + come back hours later with battery preserved
    # — they have to fully shut down or rely on standby (which drains
    # battery).  Also disables Fast Startup as a side-effect.  Saves
    # ~40% of RAM on disk (e.g. 13 GB on a 32 GB system) which is
    # meaningful on a small SSD but unmeasurable on modern 1 TB+ NVMe.
    if _pro_mode_enabled():
        reg_originals.note("hibernation_disabled")
        run_cmd(["powercfg", "-h", "off"])
        results.append({"name": "[Pro] Hibernation disabled (frees ~RAM-size on disk)", "ok": True})
    else:
        results.append({"name": "Hibernation off skipped (Pro Mode off — laptops keep the feature)",
                         "ok": True, "skipped": True})

    # bcdedit — Legacy boot menu (no fancy GUI), faster boot
    bcd = sys_state.bcd_values()
    for name, cmd in [
        ("Boot menu policy: Legacy",   ["bcdedit", "/set", "{current}", "bootmenupolicy", "Legacy"]),
        # v3.3.1-beta.7: replaced `bootux disabled` with `quietboot yes`.
        # `bootux` was a Win7/8 BCD option — silently ignored on Win10/11
        # so the comment "no fancy GUI" claim was untrue.  `quietboot`
        # is the modern equivalent and actually hides the spinner.
        ("Boot UX: quiet boot",        ["bcdedit", "/set", "{current}", "quietboot", "yes"]),
        ("Boot debug off",             ["bcdedit", "/set", "{current}", "bootlog", "no"]),
        # v3.3.1-beta.7: removed `recoveryenabled No`.  This disables the
        # Windows Recovery Environment (WinRE) entry on the boot menu.
        # When a bad update / driver crashes the system, the user can no
        # longer access "Startup Repair / Reset this PC / System Restore"
        # — they'd need a Windows installation USB.  No measurable
        # performance benefit; catastrophic on real boot failures.
    ]:
        # cmd is ["bcdedit", "/set", "{current}", element, value] — set
        # through sys_state, which notes what the element was before.
        results.append({"name": name, "ok": sys_state.set_bcd(cmd[3], cmd[4], current=bcd)})

    # Show verbose status messages on boot (helps diagnose hangs)
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
         "VerboseStatus", "1")
    results.append({"name": "Verbose boot status messages on", "ok": True})

    # v3 — removed: a previous tweak wrote "LastKnownGood\\Enabled" as
    # a single value name to Session Manager\Configuration Manager.
    # That's a malformed key path (backslash inside value name) and
    # didn't actually disable LKG anyway.  Dropped — Windows' LKG
    # behaviour is already minimal on Win10/11 and a stale tweak that
    # does nothing is just clutter on disk.

    log.info("Boot tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Legacy — disable Print Spooler, Fax, IE, WSL, Hyper-V, Sandbox
# ═══════════════════════════════════════════════════════════════════════════
def apply_legacy_tweaks() -> list[dict]:
    """Turn off legacy + rarely-used Windows features that consume RAM,
    DPC time, or just sit there as attack surface.  Anything you actually
    use, you can re-enable from this list via the Reset button."""
    log.info("Applying legacy/feature tweaks...")
    results = []

    # v3 — services set to MANUAL (not disabled) so Windows can still
    # auto-trigger them when an app actually needs them (e.g. printing
    # a doc relaunches Spooler).  Same pattern we settled on for DiagTrack.
    # Smart card / Bluetooth services are gated behind hardware detection
    # so we never lock users out of smart-card or BT-keyboard login.
    services_to_kill = list(_LEGACY_SERVICES)
    # Smart card services — skip if the user has a reader plugged in
    # (PIV / CAC / YubiKey logins depend on these services).
    try:
        sc_check = run_ps(
            "(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Class -eq 'SmartCardReader' }).Count"
        )
        has_smartcard = int((sc_check.get("out") or "0").strip() or "0") > 0
    except Exception:
        has_smartcard = True   # fail-CLOSED: leave smart card alone if probe fails
    if not has_smartcard:
        services_to_kill += list(_SMARTCARD_SERVICES)
    else:
        results.append({"name": "Smart card services kept (reader detected)", "ok": True})
    # Bluetooth — skip if a BT HID device (keyboard / mouse) is present;
    # killing bthserv on a system that depends on a BT keyboard for
    # Windows login would lock the user out.
    try:
        bt_check = run_ps(
            "(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Class -eq 'Bluetooth' -or $_.Class -eq 'HIDClass' "
            "  -and $_.InstanceId -like 'BTHENUM*' }).Count"
        )
        has_bt = int((bt_check.get("out") or "0").strip() or "0") > 0
    except Exception:
        has_bt = True
    if not has_bt:
        services_to_kill.append(("Bluetooth Support", "bthserv"))
    else:
        results.append({"name": "Bluetooth service kept (BT devices detected)", "ok": True})
    # Geolocation — leave alone; some apps (Weather, Maps, Find My Device)
    # hang waiting for the service if it's hard-disabled.

    for label, svc in services_to_kill:
        ok = sys_state.set_service_start(svc, "demand", stop=True)  # "demand" = manual
        results.append({"name": f"{label} service → manual", "ok": ok})

    # Optional Windows features that take CPU + RAM and most gaming PCs
    # don't need.  We use DISM in /norestart mode so the user can choose
    # when to reboot.
    #
    # v3 — virtualisation features (WSL, VirtualMachinePlatform, Hyper-V,
    # HypervisorPlatform, Sandbox) are GATED behind a usage check.  If
    # the user has WSL distros installed OR Docker Desktop OR an Android
    # subsystem, we skip those — disabling them silently breaks a working
    # dev environment.
    base_features = list(_BASE_FEATURES)
    virtualisation_features = list(_VIRTUALISATION_FEATURES)
    try:
        # Quick probes — if any return non-empty / non-zero, the user
        # is actively using virtualisation and we don't touch these.
        wsl_count   = run_cmd(["wsl", "--list", "--quiet"])
        docker_run  = run_cmd(["sc", "query", "com.docker.service"])
        wsa_check   = run_ps("(Get-AppxPackage -Name 'MicrosoftCorporationII.WindowsSubsystemForAndroid').Count")
        has_wsl     = bool((wsl_count.get("out") or "").strip())
        has_docker  = "RUNNING" in (docker_run.get("out") or "").upper() or "STOPPED" in (docker_run.get("out") or "").upper()
        has_wsa     = int((wsa_check.get("out") or "0").strip() or "0") > 0
        uses_virt   = has_wsl or has_docker or has_wsa
    except Exception:
        uses_virt = True  # fail-CLOSED — don't risk it if probe fails
    features_to_remove = list(base_features)
    if not uses_virt:
        features_to_remove += virtualisation_features
    else:
        results.append({"name": "Virtualisation features kept (WSL/Docker/WSA detected)", "ok": True})
    # One DISM query says which of these are on.  Only those are switched
    # off — and noted, so Reset All can switch exactly those back on (it used
    # to leave every one of them off for good).
    n_off = sys_state.disable_features(features_to_remove)
    results.append({"name": f"Disabled {n_off} optional Windows feature(s) "
                            f"(of {len(features_to_remove)} checked; the rest were already off)", "ok": True})

    log.info("Legacy tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# RESET FUNCTIONS for the new categories
# ═══════════════════════════════════════════════════════════════════════════
def _reset_audio_tweaks() -> list[dict]:
    log.info("Resetting audio tweaks...")
    audio_task = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Pro Audio"
    # Of the seven values the tweak writes here, six are the ones Windows
    # ships (0 / False / 10000 / 8 / 1 / High); only SFIO Priority differs.
    # (All seven were written back at every reset — after an earlier version
    # of this function had DELETED them at every reset.  Values that are
    # missing for that reason are not recreated here: a value that does not
    # exist is not one the tweak left.)
    _reg_default(audio_task, "SFIO Priority", "High", "Normal", "REG_SZ")
    # beta.5: was ControlSet001 (hardcoded) — use the live CurrentControlSet.
    # (A tweak removed in 3.3.1; it wrote 1.)
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\Bluetooth\Audio\AVRCP\CT",
                 "DisableAbsoluteVolume", "1", None)
    return [{"name": "Audio tweaks restored", "ok": True}]


def _reset_input_tweaks() -> list[dict]:
    log.info("Resetting input tweaks...")
    mkey = r"HKCU\Control Panel\Mouse"
    _reg_default(mkey, "MouseHoverTime",   "0", "400", "REG_SZ")
    _reg_default(mkey, "MouseHoverWidth",  "1", "4",   "REG_SZ")
    _reg_default(mkey, "MouseHoverHeight", "1", "4",   "REG_SZ")
    # (MouseTrails 0 and KeyboardSpeed 31: what the tweak writes is what was
    # written back here, so there is nothing to undo without a record.)

    kkey = r"HKCU\Control Panel\Keyboard"
    _reg_default(kkey, "KeyboardDelay", "0", "1", "REG_SZ")  # default 1

    _reg_default(r"HKCU\Software\Microsoft\TabletTip\1.7",
                 "EnableAutoInvokeTouchKeyboard", "0", None)
    _reg_default(r"HKCU\Software\Microsoft\Wisp\Pen\SysEventParameters",
                 "FlickMode", "0", None)
    return [{"name": "Input tweaks restored", "ok": True}]


def _reset_search_tweaks() -> list[dict]:
    log.info("Resetting search tweaks...")
    cor = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search"
    # value, what the tweak leaves there
    for val, tweak in (("AllowCortana", "0"), ("AllowCortanaAboveLock", "0"), ("AllowVoiceActivation", "0"),
                       ("DisableWebSearch", "1"), ("ConnectedSearchUseWeb", "0"),
                       ("EnableDynamicContentInWSB", "0"), ("AllowSearchToUseLocation", "0"),
                       ("PreventIndexingOutlook", "1")):      # (the last one: removed in 3.3.1)
        _reg_default(cor, val, tweak, None)
    sh = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search"
    for val in ("DeviceHistoryEnabled", "HistoryViewEnabled",
                "BingSearchEnabled", "CortanaConsent"):
        _reg_default(sh, val, "0", None)
    return [{"name": "Search & Cortana restored", "ok": True}]


def _reset_defender_gaming_tweaks() -> list[dict]:
    """Preferences and exclusions that are on record are put back by the
    final pass of Reset All: each preference to its previous value, and
    exactly the exclusions this app added removed again.

    What an older build changed has no record.  For that, the two policy bits
    go to their Windows defaults — each only while it still reads the way the
    tweak leaves it (NeverSend / Disabled); "always prompt" or "basic" is the
    user's own setting, and used to be overwritten here.  The exclusions
    stay: they cannot be told apart from ones the user added."""
    log.info("Resetting Defender gaming tweaks...")
    # name, how the tweak's value reads back, the Windows default
    bits = [b for b in (("SubmitSamplesConsent", 2, "SendSafeSamples"), ("MAPSReporting", 0, "Advanced"))
            if not reg_originals.has_state("defender", b[0])]
    prefs = (sys_state.defender_prefs() or {}) if bits else {}
    for name, tweak, stock in bits:
        if prefs.get(name) == tweak:
            run_ps(f'Set-MpPreference -{name} {stock} -ErrorAction SilentlyContinue')
    n = len(reg_originals.state_records("defender-exclusion"))
    return [{"name": (f"Defender policy restored; {n} exclusion(s) added by Vispora will be removed" if n else
                      "Defender policy restored (no exclusions on record; any added by an earlier version are kept)"),
             "ok": True}]


def _reset_boot_tweaks() -> list[dict]:
    log.info("Resetting boot tweaks...")
    # Fast Startup and hibernation are only switched back on if this app
    # switched them off.  Doing it unconditionally re-enabled Fast Startup
    # and recreated a RAM-sized hiberfil.sys for people who had turned them
    # off themselves (deliberately, on a dual-boot machine).
    hb_key = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    if _pro_mode_enabled():
        _reg_default(hb_key, "HiberBootEnabled", "0", "1")
    if reg_originals.noted("hibernation_disabled"):
        run_cmd(["powercfg", "-h", "on"])
        reg_originals.forget_notes("hibernation_disabled")
    # Fixed defaults for boot elements an older build changed without a
    # record, each only while it still reads the way that tweak left it.
    bcd = sys_state.bcd_values()
    # Set, not deleted: Windows ships these two present.  Deleting them is
    # not "the default" — without recoveryenabled the machine no longer falls
    # back to the recovery environment after failed boots, and this ran for
    # everyone, whether or not a tweak had ever touched it.
    _bcd_default(bcd, "bootmenupolicy", "Legacy", stock="Standard")
    _bcd_default(bcd, "recoveryenabled", "No", stock="Yes")
    _bcd_default(bcd, "bootux", "disabled")
    _bcd_default(bcd, "bootlog", "no")
    _bcd_default(bcd, "quietboot", "yes")       # was never reset at all
    _reg_default(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                 "VerboseStatus", "1", None)
    return [{"name": "Boot tweaks restored", "ok": True}]


def _reset_legacy_tweaks() -> list[dict]:
    log.info("Resetting legacy tweaks...")
    # Start types that are on record come back with the final pass of Reset
    # All.  These are the fixed defaults for what an older build changed:
    # the tweak leaves every one of these services on Manual, and only the
    # Print Spooler ships as anything else.
    _service_default("Spooler", tweak="demand", stock="auto", start=True)
    _service_default("lfsvc", tweak="disabled", stock="demand")     # older builds disabled Geolocation
    rows = [{"name": "Legacy services restored (previous start types where on record)", "ok": True}]
    if not reg_originals.has_state("feature"):
        rows.append({"name": "Windows features: none on record to switch back on (any that an earlier "
                             "version turned off are under 'Turn Windows features on or off')", "ok": True})
    return rows


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Network Stack — TCP/IP advanced
# ═══════════════════════════════════════════════════════════════════════════
def apply_network_stack_tweaks() -> list[dict]:
    """Deep TCP/IP stack tuning beyond what /api/network/optimize already
    does — Nagle off per-interface, RWIN tuning, ack frequency, congestion
    control, RFC1122 urgent pointer, IRPStackSize, DNS cache size.
    Reboot required."""
    log.info("Applying network stack tweaks...")
    results = []

    # Per-NIC: TcpAckFrequency = 1 (no delayed ACK), TCPNoDelay = 1 (Nagle off)
    # Written value by value through _reg.  This used to be a PowerShell loop
    # that set the registry itself, so nothing was on record and the
    # per-adapter changes survived every reset.
    ifaces_key = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    for iface in reg_originals.subkeys(ifaces_key):
        for val, data in (("TcpAckFrequency", "1"), ("TCPNoDelay", "1"), ("TcpDelAckTicks", "0")):
            _reg(rf"{ifaces_key}\{iface}", val, data)
    results.append({"name": "Per-NIC: Nagle off, ACK delay off", "ok": True})

    # Global TCP/IP parameters
    tcpip = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    for name, val, data in [
        ("TCP TimedWaitDelay → 30s",          "TcpTimedWaitDelay", "30"),
        ("Max user ports → 65534",             "MaxUserPort", "65534"),
        ("DefaultTTL → 64",                    "DefaultTTL", "64"),
        ("SACK enabled",                       "SackOpts", "1"),
        ("Tcp1323Opts (window scaling)",       "Tcp1323Opts", "1"),
        ("MaxFreeTcbs",                        "MaxFreeTcbs", "65536"),
        ("MaxHashTableSize",                   "MaxHashTableSize", "65536"),
        ("DisableTaskOffload",                 "DisableTaskOffload", "0"),
        ("EnablePMTUDiscovery",                "EnablePMTUDiscovery", "1"),
        ("EnablePMTUBHDetect",                 "EnablePMTUBHDetect", "0"),
        ("EnableICMPRedirect",                 "EnableICMPRedirect", "0"),
        ("KeepAliveTime",                      "KeepAliveTime", "300000"),
    ]:
        ok = _reg(tcpip, val, data)
        results.append({"name": name, "ok": ok})

    # netsh global — each parameter's previous value is noted before it changes
    tcp = sys_state.tcp_globals()
    for param, value in _TCP_GLOBAL_TWEAKS:
        results.append({"name": f"netsh: {param} {value}",
                        "ok": sys_state.set_tcp_global(param, value, current=tcp)})

    log.info("Network stack tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Telemetry Deep — beyond what's in privacy tab
# ═══════════════════════════════════════════════════════════════════════════
def apply_telemetry_deep_tweaks() -> list[dict]:
    """Burns telemetry deeper than the Privacy tab — disables CEIP,
    application impact telemetry, inventory collector, application
    compatibility scheduler, customer experience tasks, error reporting."""
    log.info("Applying deep telemetry tweaks...")
    results = []

    # Telemetry policy — set to Security (0) on Win11 Enterprise/Edu, 1 elsewhere
    dc = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection"
    for name, val, data in [
        ("Telemetry → Security/Basic",       "AllowTelemetry", "0"),
        ("MS Account telemetry off",          "AllowDeviceNameInTelemetry", "0"),
        ("Disable diagnostic data viewer",    "DisableDiagnosticDataViewer", "1"),
        ("Disable diagnostic data history",   "DisableDiagnosticDataHistory", "1"),
        ("Disable feedback notifications",    "DoNotShowFeedbackNotifications", "1"),
        ("Disable advertising ID",            "DisableAdvertisingId", "1"),
    ]:
        _reg(dc, val, data)
        results.append({"name": name, "ok": True})

    # CEIP and Application Impact / Inventory
    sqm = r"HKLM\SOFTWARE\Policies\Microsoft\SQMClient\Windows"
    _reg(sqm, "CEIPEnable", "0")
    results.append({"name": "Customer Experience Improvement Program off", "ok": True})

    # Application Telemetry / Compat
    appcompat = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppCompat"
    for name, val, data in [
        ("App Compat AITEnable",       "AITEnable", "0"),
        ("App Compat DisableInventory", "DisableInventory", "1"),
        ("App Compat DisableUAR",       "DisableUAR", "1"),
    ]:
        _reg(appcompat, val, data)
        results.append({"name": name, "ok": True})

    # Disable known telemetry scheduled tasks
    tasks = _TELEMETRY_TASKS
    for t in tasks:
        sys_state.disable_task(t)       # notes that it was enabled, so Reset All can re-enable it
    results.append({"name": f"Disabled {len(tasks)} telemetry scheduled tasks", "ok": True})

    # Error reporting service
    sys_state.set_service_start("WerSvc", "disabled", stop=True)
    results.append({"name": "Windows Error Reporting service off", "ok": True})

    # DiagTrack (Connected User Experiences and Telemetry).
    # NOTE: setting this to 'disabled' breaks the Microsoft Store and the
    # Xbox app's update flow — they use DiagTrack to talk to Microsoft's
    # content delivery infrastructure.  We instead set it to 'manual' so
    # it stays off most of the time but can start on demand when the
    # Store/Xbox app actually needs it.  Net telemetry impact is still
    # ~zero in idle steady state.
    sys_state.set_service_start("DiagTrack", "demand", stop=True)
    results.append({"name": "DiagTrack service → manual (was disabled — broke Store/Xbox)", "ok": True})

    # dmwappushservice
    sys_state.set_service_start("dmwappushservice", "disabled", stop=True)
    results.append({"name": "WAP Push Message Routing off", "ok": True})

    log.info("Deep telemetry tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Edge — disable startup boost, prefetch, sidebar, background
# ═══════════════════════════════════════════════════════════════════════════
def apply_edge_tweaks() -> list[dict]:
    """Microsoft Edge ships with several pre-launch / always-on services
    that eat RAM even when you don't use the browser.  This kills them."""
    log.info("Applying Microsoft Edge tweaks...")
    results = []

    edge = r"HKLM\SOFTWARE\Policies\Microsoft\Edge"
    for name, val, data in [
        ("Edge Startup Boost off",            "StartupBoostEnabled", "0"),
        ("Edge Background Mode off",          "BackgroundModeEnabled", "0"),
        ("Edge Hub apps preload off",         "HubsSidebarEnabled", "0"),
        ("Edge sidebar off",                  "ShowMicrosoftRewards", "0"),
        ("Edge address bar suggestions off",  "AddressBarMicrosoftSearchInBingProviderEnabled", "0"),
        ("Edge first-run experience off",     "HideFirstRunExperience", "1"),
        ("Edge bing chat off",                "DiscoverPageContextEnabled", "0"),
        ("Edge personalization off",          "PersonalizationReportingEnabled", "0"),
        ("Edge spotlight off",                "SpotlightExperiencesAndRecommendationsEnabled", "0"),
        ("Edge new tab page tip off",         "NewTabPageContentEnabled", "0"),
        ("Edge experimentation off",          "ExperimentationAndConfigurationServiceControl", "0"),
    ]:
        _reg(edge, val, data)
        results.append({"name": name, "ok": True})

    # Edge "Update Service" — running even when Edge isn't open
    for svc in _EDGE_SERVICES:
        sys_state.set_service_start(svc, "demand", stop=True)
    results.append({"name": "Edge update services → manual", "ok": True})

    log.info("Edge tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Windows Update — defer, P2P off, drivers off
# ═══════════════════════════════════════════════════════════════════════════
def apply_update_control_tweaks() -> list[dict]:
    """Hand control of Windows Update back to the user.  Doesn't disable
    updates entirely — defers feature updates 365 days, kills WUDO P2P,
    blocks driver updates via WU (you control your GPU drivers)."""
    log.info("Applying Windows Update controls...")
    results = []

    wu = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
    for name, val, data in [
        ("Block driver updates via WU",     "ExcludeWUDriversInQualityUpdate", "1"),
        ("Defer feature updates 365 days",  "DeferFeatureUpdates", "1"),
        ("DeferFeatureUpdatesPeriodInDays", "DeferFeatureUpdatesPeriodInDays", "365"),
        ("Defer quality updates 14 days",   "DeferQualityUpdates", "1"),
        ("DeferQualityUpdatesPeriodInDays", "DeferQualityUpdatesPeriodInDays", "14"),
    ]:
        _reg(wu, val, data)
        results.append({"name": name, "ok": True})

    # AU = automatic update settings
    au = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    for name, val, data in [
        ("Notify-only mode",                  "AUOptions", "2"),
        ("No auto reboot with logged users",  "NoAutoRebootWithLoggedOnUsers", "1"),
        ("Disable WU auto-restart",           "AlwaysAutoRebootAtScheduledTime", "0"),
    ]:
        _reg(au, val, data)
        results.append({"name": name, "ok": True})

    # Delivery Optimization → LAN only (or off entirely if you prefer)
    do_key = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization"
    _reg(do_key, "DODownloadMode", "1")  # 0=off, 1=LAN, 2=group, 3=internet+LAN
    results.append({"name": "Delivery Optimization → LAN only", "ok": True})

    log.info("Windows Update controls applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Notifications — kill the nag everywhere
# ═══════════════════════════════════════════════════════════════════════════
def apply_notification_tweaks() -> list[dict]:
    """Disable lock screen notifications, action center toasts, suggestion
    notifications, finish setup, and the 'finish setting up your device'
    nag screen."""
    log.info("Applying notification tweaks...")
    results = []

    # User-side notification settings
    ns = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
    _reg(ns, "NOC_GLOBAL_SETTING_TOASTS_ENABLED",          "0")
    _reg(ns, "NOC_GLOBAL_SETTING_BADGE_ENABLED",           "0")
    _reg(ns, "NOC_GLOBAL_SETTING_ALLOW_NOTIFICATION_SOUND","0")
    results.append({"name": "Toast notifications globally off", "ok": True})

    # Lock screen notifications
    pn = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\PushNotifications"
    _reg(pn, "ToastEnabled", "0")
    _reg(pn, "LockScreenToastEnabled", "0")
    results.append({"name": "Lock screen notifications off", "ok": True})

    # Suggestion notifications + finish-setup nags
    cm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement"
    _reg(cm, "ScoobeSystemSettingEnabled", "0")
    results.append({"name": "Finish-setup nags off", "ok": True})

    # Action Center / Quick Actions
    pol = r"HKCU\Software\Policies\Microsoft\Windows\Explorer"
    _reg(pol, "DisableNotificationCenter", "0")  # 0 in user-pol means available; we set globally below
    polsys = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer"
    _reg(polsys, "DisableNotificationCenter", "0")
    results.append({"name": "Action Center available", "ok": True})

    # Suppress Win11 Spotlight/feature ads in notifications
    cdm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
    for val in ("SubscribedContent-353698Enabled",   # Spotlight on lock screen
                "SubscribedContent-353696Enabled",
                "SubscribedContent-310093Enabled",   # Welcome page
                "SubscribedContent-202914Enabled",
                "SubscribedContent-280815Enabled",
                "FeatureManagementEnabled",
                "ContentDeliveryAllowed"):
        _reg(cdm, val, "0")
    results.append({"name": "All Spotlight & feature ads off", "ok": True})

    # Cloud content / "tips, tricks, suggestions"
    cc = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent"
    _reg(cc, "DisableSoftLanding", "1")
    _reg(cc, "DisableWindowsConsumerFeatures", "1")
    _reg(cc, "DisableThirdPartySuggestions", "1")
    results.append({"name": "Cloud content suggestions disabled", "ok": True})

    log.info("Notification tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Power Advanced — deep power-plan config beyond Ultimate
# ═══════════════════════════════════════════════════════════════════════════
def apply_power_advanced_tweaks() -> list[dict]:
    """Tighten the active power plan beyond just selecting Ultimate
    Performance — disables every battery-saving micro-tweak that adds
    ms-of-latency under load (USB suspend, PCIe link state, hard disk
    sleep, processor throttling, monitor sleep)."""
    log.info("Applying advanced power tweaks...")
    results = []

    # All of these reference the *active* scheme, so you can apply them
    # regardless of which plan the user is on.
    cmds = [
        ("USB Selective Suspend off",      ["powercfg", "/setacvalueindex", "scheme_current", "2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226", "0"]),
        ("PCIe Link State off",             ["powercfg", "/setacvalueindex", "scheme_current", "501a4d13-42af-4429-9fd1-a8218c268e20", "ee12f906-d277-404b-b6da-e5fa1a576df5", "0"]),
        ("Hard disk: never sleep",          ["powercfg", "/setacvalueindex", "scheme_current", "0012ee47-9041-4b5d-9b77-535fba8b1442", "6738e2c4-e8a5-4a42-b16a-e040e769756e", "0"]),
        ("Min processor state → 100%",      ["powercfg", "/setacvalueindex", "scheme_current", "54533251-82be-4824-96c1-47b60b740d00", "893dee8e-2bef-41e0-89c6-b55d0929964c", "100"]),
        ("Max processor state → 100%",      ["powercfg", "/setacvalueindex", "scheme_current", "54533251-82be-4824-96c1-47b60b740d00", "bc5038f7-23e0-4960-96da-33abaf5935ec", "100"]),
        ("CPU boost mode → Aggressive",     ["powercfg", "/setacvalueindex", "scheme_current", "54533251-82be-4824-96c1-47b60b740d00", "be337238-0d82-4146-a960-4f3749d470c7", "2"]),
        ("CPU idle disable",                ["powercfg", "/setacvalueindex", "scheme_current", "54533251-82be-4824-96c1-47b60b740d00", "5d76a2ca-e8c0-402f-a133-2158492d58ad", "1"]),
        ("System cooling: Active",          ["powercfg", "/setacvalueindex", "scheme_current", "54533251-82be-4824-96c1-47b60b740d00", "94d3a615-a899-4ac5-ae2b-e4d8f634367f", "1"]),
        ("Monitor sleep: never",            ["powercfg", "/setacvalueindex", "scheme_current", "7516b95f-f776-4464-8c53-06167f40cc99", "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e", "0"]),
        ("System sleep: never",             ["powercfg", "/setacvalueindex", "scheme_current", "238c9fa8-0aad-41ed-83f4-97be242c8f20", "29f6c1db-86da-48c5-9fdb-f2b67b1f44da", "0"]),
        ("System unattended sleep: never",  ["powercfg", "/setacvalueindex", "scheme_current", "238c9fa8-0aad-41ed-83f4-97be242c8f20", "7bc4a2f9-d8fc-4469-b07b-33eb785aaca0", "0"]),
        ("Hibernate timeout: never",        ["powercfg", "/setacvalueindex", "scheme_current", "238c9fa8-0aad-41ed-83f4-97be242c8f20", "9d7815a6-7ee4-497e-8888-515a05f02364", "0"]),
        ("Wake timers off",                 ["powercfg", "/setacvalueindex", "scheme_current", "238c9fa8-0aad-41ed-83f4-97be242c8f20", "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d", "0"]),
        ("Lid close → Do nothing",          ["powercfg", "/setacvalueindex", "scheme_current", "4f971e89-eebd-4455-a8de-9e59040e7347", "5ca83367-6e45-459f-a27b-476b1d01c936", "0"]),
        ("Power button → Shutdown",         ["powercfg", "/setacvalueindex", "scheme_current", "4f971e89-eebd-4455-a8de-9e59040e7347", "7648efa3-dd9c-4e3e-b566-50f929386280", "3"]),
    ]
    for name, cmd in cmds:
        reg_originals.remember_power(cmd[3], cmd[4])    # sub-group, setting — before it changes
        r = run_cmd(cmd)
        results.append({"name": name, "ok": r["ok"]})

    run_cmd(["powercfg", "/setactive", "scheme_current"])
    results.append({"name": "Active scheme refreshed", "ok": True})

    log.info("Advanced power tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: App Privacy — per-app permissions
# ═══════════════════════════════════════════════════════════════════════════
def apply_app_privacy_tweaks() -> list[dict]:
    """Deny apps default access to every privacy-sensitive surface in
    Win11.  User can re-grant per-app from Settings as needed.  Affects
    what app installations get on first launch."""
    log.info("Applying app privacy tweaks...")
    results = []

    ap = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy"
    # NOTE: the following are intentionally NOT in this list:
    #   - LetAppsAccessMicrophone — denying it breaks Discord/Teams/in-game
    #     voice that goes through the Windows capability gate
    #   - LetAppsAccessHumanInterfaceDevice — controllers and HID devices
    #     need it
    #   - LetAppsRunInBackground — denying it breaks Microsoft Store and
    #     Xbox app auto-update (they need to run their updater in the bg)
    #   - LetAppsGetDiagnosticInfo — Store needs this for update telemetry
    #   - LetAppsAccessNotifications (v3.3.1-beta.7) — denying it kills
    #     toast notifications for Discord, Slack, Steam friends, Teams,
    #     OBS, and every other modern app.  Users notice immediately.
    # The user's privacy is still well-served by the other ~17 permissions
    # below; these five are user/Windows Settings territory, not Vispora's.
    permissions = [
        "LetAppsAccessLocation",       "LetAppsAccessCamera",
        "LetAppsAccessAccountInfo",
        "LetAppsAccessCalendar",       "LetAppsAccessCallHistory",
        "LetAppsAccessContacts",       "LetAppsAccessEmail",
        "LetAppsAccessMessaging",
        "LetAppsAccessTasks",          "LetAppsAccessPhone",
        "LetAppsAccessRadios",         "LetAppsAccessTrustedDevices",
        "LetAppsActivateWithVoice",    "LetAppsActivateWithVoiceAboveLock",
        "LetAppsAccessGazeInput",
        "LetAppsSyncWithDevices",      "LetAppsAccessMotion",
    ]
    for p in permissions:
        # 2 = deny by default
        _reg(ap, p, "2")
    results.append({"name": f"Denied default app access on {len(permissions)} permissions (mic/HID/bg/diag intentionally untouched)", "ok": True})

    # v3 — REMOVED: per-user Location ConsentStore="Deny" write.
    # Same class of footgun as the microphone Deny we had to roll
    # back earlier — silently breaks Weather, Maps, Find My Device,
    # any location-aware game.  Apps that need location now get the
    # user's actual choice via the normal Windows consent UI.

    log.info("App privacy tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: File Explorer — quality of life
# ═══════════════════════════════════════════════════════════════════════════
def apply_explorer_tweaks() -> list[dict]:
    """File Explorer polish — long paths, this PC default view, no Quick
    Access nag, no folder thumbnails (perf), checkboxes off, status bar
    on, recents off."""
    log.info("Applying Explorer tweaks...")
    results = []

    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    for name, val, data in [
        ("Launch Explorer to → This PC",  "LaunchTo", "1"),
        ("Show file extensions",           "HideFileExt", "0"),
        ("Show hidden files",              "Hidden", "1"),
        ("Show empty drives",              "HideDrivesWithNoMedia", "0"),
        ("Show full path in title bar",    "FullPath", "1"),
        ("Auto-expand to current folder",  "NavPaneExpandToCurrentFolder", "0"),
        ("Disable preview pane peek",      "ShowPreviewHandlers", "0"),
        ("Disable selection checkboxes",   "AutoCheckSelect", "0"),
        ("Show drive letters first",       "ShowDriveLettersFirst", "4"),
        ("Don't show recently used in QA", "ShowRecent", "0"),
        ("Don't show frequently used",     "ShowFrequent", "0"),
        ("Don't show OneDrive sync ads",   "ShowSyncProviderNotifications", "0"),
        ("Show ribbon collapsed",          "ExplorerRibbonStartsMinimized", "4"),
    ]:
        _reg(advanced, val, data)
        results.append({"name": name, "ok": True})

    # Long Paths support (max path 260 → 32767)
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
         "LongPathsEnabled", "1")
    results.append({"name": "Long paths support enabled (>260 chars)", "ok": True})

    # v3.3.1-beta.7: removed HubMode=1.  Microsoft removed honoring of
    # this registry value in Win11 23H2+ — the value sits in the
    # registry but File Explorer ignores it.  Was a no-op on every
    # modern Windows install.

    log.info("Explorer tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: SmartScreen — disable web filter (PUA stays via Defender)
# ═══════════════════════════════════════════════════════════════════════════
def apply_smartscreen_tweaks() -> list[dict]:
    """Disable Microsoft SmartScreen's URL/download filter — adds latency
    on every download since it phones home before letting the file open.
    Defender's PUA / real-time protection stays on regardless."""
    log.info("Applying SmartScreen tweaks...")
    results = []

    sys = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
    _reg(sys, "SmartScreenEnabled", "Off", "REG_SZ")
    results.append({"name": "Explorer SmartScreen off", "ok": True})

    # Edge SmartScreen
    edge = r"HKLM\SOFTWARE\Policies\Microsoft\Edge"
    _reg(edge, "SmartScreenEnabled", "0")
    _reg(edge, "SmartScreenPuaEnabled", "0")
    results.append({"name": "Edge SmartScreen off", "ok": True})

    # Store SmartScreen
    sse = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System"
    _reg(sse, "EnableSmartScreen", "0")
    results.append({"name": "Store SmartScreen off", "ok": True})

    # Per-user SmartScreen for downloads
    ps = r"HKCU\Software\Microsoft\Windows\CurrentVersion\AppHost"
    _reg(ps, "EnableWebContentEvaluation", "0")
    _reg(ps, "PreventOverride", "0")
    results.append({"name": "Web content evaluation off", "ok": True})

    # WebThreatDefense (Win11 24H2 phishing protection — pings MS on logon)
    wtd = r"HKCU\Software\Microsoft\Windows\CurrentVersion\WebThreatDefense"
    _reg(wtd, "ServiceEnabled", "0")
    _reg(wtd, "NotifyMalicious", "0")
    _reg(wtd, "NotifyPasswordReuse", "0")
    _reg(wtd, "NotifyUnsafeApp", "0")
    results.append({"name": "Win11 24H2 web threat defense off", "ok": True})

    log.info("SmartScreen tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# NEW CATEGORY: Lock Screen — strip the Win11 lock screen
# ═══════════════════════════════════════════════════════════════════════════
def apply_lock_screen_tweaks() -> list[dict]:
    """Clean up the Win11 lock screen — no ads, no Spotlight rotating
    backgrounds, no fun-fact tips, no Cortana on lock, no fingerprint
    'sign in faster' suggestion every boot."""
    log.info("Applying lock screen tweaks...")
    results = []

    cdm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
    for val in ("RotatingLockScreenEnabled",
                "RotatingLockScreenOverlayEnabled",
                "SubscribedContent-338387Enabled",   # Lock screen tips
                "SubscribedContent-338388Enabled",
                "SubscribedContent-353694Enabled",
                "SubscribedContent-353695Enabled"):
        _reg(cdm, val, "0")
    results.append({"name": "Lock screen Spotlight + tips off", "ok": True})

    # Personalization: don't show ads on lock screen
    pers = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization"
    _reg(pers, "NoLockScreen", "0")  # we still WANT a lock screen, just no ads
    _reg(pers, "LockScreenOverlaysDisabled", "1")
    results.append({"name": "Lock screen ads off", "ok": True})

    # Hello fingerprint nag
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\PenWorkspace",
         "PenWorkspaceButtonDesiredVisibility", "0")
    results.append({"name": "Pen workspace nag off", "ok": True})

    # Disable login screen "1st sign-in animation"
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
         "EnableFirstLogonAnimation", "0")
    results.append({"name": "First-logon animation off", "ok": True})

    # Last-signed-in user displayed (small attack surface, but useful for solo PCs)
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
         "DontDisplayLastUserName", "0")
    results.append({"name": "Show last user on lock screen", "ok": True})

    log.info("Lock screen tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# RESET FUNCTIONS for the 10 newer categories
# ═══════════════════════════════════════════════════════════════════════════
def _reset_network_stack_tweaks() -> list[dict]:
    log.info("Resetting network stack tweaks...")
    if reg_originals.has_state("tcp"):
        # Each parameter's previous value is on record; the final pass of
        # Reset All puts those back, which a blanket reset would then undo.
        return [{"name": "TCP parameters: previous values will be restored", "ok": True}]
    # No record — changed by an older build, or not at all.  Reset the TCP
    # parameters only if some still read the way the tweak leaves them.
    # (`netsh winsock reset` used to run here as well.  Nothing this app does
    # touches the Winsock catalog, and resetting it removes the providers
    # that VPN clients and security software install there.)
    cur = sys_state.tcp_globals() or {}
    if any(cur.get(k) == v for k, v in (("initialrto", "2000"), ("maxsynretransmissions", "2"),
                                        ("hystart", "disabled"))):
        run_cmd(["netsh", "int", "tcp", "reset"])
        return [{"name": "TCP parameters reset to Windows defaults (reboot required)", "ok": True}]
    return [{"name": "TCP parameters: none of this app's tweaks present", "ok": True}]


def _reset_telemetry_deep_tweaks() -> list[dict]:
    log.info("Resetting deep telemetry tweaks...")
    # Fixed defaults for services an older build changed without a record.
    # ("manual" is not a start type sc.exe accepts — it is "demand" — so two
    # of the three commands that used to be here never did anything.)
    _service_default("WerSvc", tweak="disabled", stock="demand")
    _service_default("DiagTrack", tweak="demand", stock="auto")
    _service_default("dmwappushservice", tweak="disabled", stock="demand")
    rows = [{"name": "Telemetry services restored (previous start types where on record)", "ok": True}]
    if not reg_originals.has_state("task"):
        rows.append({"name": "Telemetry scheduled tasks: none on record to re-enable (any that an earlier "
                             "version disabled can be switched back on in Task Scheduler)", "ok": True})
    return rows


def _reset_edge_tweaks() -> list[dict]:
    """v3 — per-value deletes (the old `reg delete <key> /f` wiped the
    entire Edge policy subtree, killing enterprise / work-profile / other
    tools' Edge configs)."""
    log.info("Resetting Edge tweaks...")
    edge_key = r"HKLM\SOFTWARE\Policies\Microsoft\Edge"
    # Only the values WE write in apply_edge_tweaks / apply_smartscreen_tweaks
    # (every one of them as 0) — if you add to the apply list, mirror here.
    # (TabFreezeEnabled, TabSleeping, PreloadOnStartup, NewTabPagePrerender,
    # AlternateErrorPagesEnabled, AutofillCreditCardEnabled,
    # PasswordManagerEnabled, BrowserAddProfileEnabled and
    # HardwareAccelerationModeEnabled were deleted here as well.  Nothing in
    # this app writes them, and what an older build may have written is not
    # known: an administrator's or the user's own Edge policy stays.)
    for v in ("StartupBoostEnabled", "BackgroundModeEnabled",
              "SmartScreenEnabled", "SmartScreenPuaEnabled",
              "PersonalizationReportingEnabled",
              "ShowMicrosoftRewards"):
        _reg_default(edge_key, v, "0", None)
    # Of the three Edge services only edgeupdate ships as anything other
    # than Manual, which is what the tweak sets.  (This used to set all
    # three to Automatic.)
    _service_default("edgeupdate", tweak="demand", stock="delayed-auto")
    return [{"name": "Edge tweak values cleared (subtree preserved), services restored", "ok": True}]


def _reset_update_control_tweaks() -> list[dict]:
    """v3 — per-value deletes."""
    log.info("Resetting Windows Update controls...")
    wu_key  = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
    wu_au   = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    # value, what the tweak leaves there (this page's and the Privacy tab's)
    for v, tweak in (("DeferFeatureUpdates", "1"), ("DeferFeatureUpdatesPeriodInDays", "365"),
                     ("DeferQualityUpdates", "1"), ("DeferQualityUpdatesPeriodInDays", "14"),
                     ("ExcludeWUDriversInQualityUpdate", "1")):
        _reg_default(wu_key, v, tweak, None)
    _reg_default(wu_au, "AUOptions", "2", None)
    # (BranchReadinessLevel, DisableDualScan,
    # DoNotConnectToWindowsUpdateInternetLocations and, under AU, NoAutoUpdate,
    # AutomaticMaintenanceEnabled, ScheduledInstallDay and ScheduledInstallTime
    # were deleted here as well.  Nothing in this app writes them, and what
    # an older build may have written is not known: somebody's own Windows
    # Update policy stays.)
    return [{"name": "WU tweak values cleared (subtree preserved)", "ok": True}]


def _reset_notification_tweaks() -> list[dict]:
    log.info("Resetting notification tweaks...")
    ns = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
    for v in ("NOC_GLOBAL_SETTING_TOASTS_ENABLED",
              "NOC_GLOBAL_SETTING_BADGE_ENABLED",
              "NOC_GLOBAL_SETTING_ALLOW_NOTIFICATION_SOUND"):
        _reg_default(ns, v, "0", None)
    return [{"name": "Notification policies restored", "ok": True}]


def _reset_power_advanced_tweaks() -> list[dict]:
    log.info("Resetting advanced power tweaks...")
    # The settings this tweak changed are restored, each to the value it had
    # on the plan it was changed on, by the final pass of Reset All.  This
    # used to run `powercfg -restoredefaultschemes` and force Balanced, which
    # DELETED every custom / OEM power plan on the machine — including the
    # user's own plan that _reset_power_plan() had restored a moment earlier.
    n = reg_originals.summary()["power"]
    if n:
        return [{"name": f"{n} power-plan setting(s) will be restored to their previous values", "ok": True}]
    return [{"name": "Advanced power settings: no previous values on record — left as they are "
                     "(Windows: Power Options → Restore default settings for this plan)", "ok": True}]


def _reset_app_privacy_tweaks() -> list[dict]:
    """v3 — per-value deletes (avoids wiping unrelated AppPrivacy
    values another tool or admin policy may have placed there)."""
    log.info("Resetting app privacy tweaks...")
    ap = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy"
    # Only the LetApps* values we actually wrote.  We intentionally
    # leave the mic / camera / contacts ConsentStore alone (those
    # need to be re-set by the user via Windows Settings if they want
    # them back on — auto-restoring would create a privacy regression
    # for users who genuinely disabled them).
    # Every one of these is written as 2 ("deny"), and removed only while it
    # still reads 2.  LetAppsAccessNotifications and LetAppsGetDiagnosticInfo
    # are no longer applied; older builds denied those too.
    # ("LetAppsActivate" was deleted here as well.  Nothing writes a value of
    # that name.)
    for v in ("LetAppsAccessAccountInfo", "LetAppsAccessCalendar",
              "LetAppsAccessCallHistory", "LetAppsAccessContacts",
              "LetAppsAccessEmail", "LetAppsAccessMessaging",
              "LetAppsAccessMotion", "LetAppsAccessNotifications",
              "LetAppsAccessPhone", "LetAppsAccessRadios",
              "LetAppsAccessTrustedDevices",
              "LetAppsGetDiagnosticInfo", "LetAppsSyncWithDevices"):
        _reg_default(ap, v, "2", None)
    return [{"name": "App privacy tweak values cleared (subtree preserved)", "ok": True}]


def _reset_explorer_tweaks() -> list[dict]:
    log.info("Resetting Explorer tweaks...")
    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    # value, what the tweak leaves there
    for v, tweak in (("LaunchTo", "1"), ("FullPath", "1"), ("AutoCheckSelect", "0"), ("ShowDriveLettersFirst", "4"),
                     ("ShowRecent", "0"), ("ShowFrequent", "0"), ("ExplorerRibbonStartsMinimized", "4"),
                     ("NavPaneExpandToCurrentFolder", "0"), ("ShowPreviewHandlers", "0")):
        _reg_default(advanced, v, tweak, None)
    _reg_default(r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
                 "LongPathsEnabled", "1", None)
    return [{"name": "Explorer settings restored to defaults", "ok": True}]


def _reset_smartscreen_tweaks() -> list[dict]:
    log.info("Resetting SmartScreen tweaks...")
    _reg_default(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer",
                 "SmartScreenEnabled", "Off", "RequireAdmin", "REG_SZ")
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Edge", "SmartScreenEnabled", "0", None)
    _reg_default(r"HKLM\SOFTWARE\Policies\Microsoft\Edge", "SmartScreenPuaEnabled", "0", None)
    return [{"name": "SmartScreen restored", "ok": True}]


def _reset_lock_screen_tweaks() -> list[dict]:
    """v3 — per-value deletes."""
    log.info("Resetting lock screen tweaks...")
    pers = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization"
    _reg_default(pers, "NoLockScreen", "0", None)
    _reg_default(pers, "LockScreenOverlaysDisabled", "1", None)
    # (LockScreenImage and NoChangingLockScreen were deleted here as well.
    # Nothing in this app writes them: a lock-screen picture set by policy
    # is somebody else's.)
    cdm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
    for v in ("RotatingLockScreenEnabled", "RotatingLockScreenOverlayEnabled"):
        _reg_default(cdm, v, "0", "1")
    return [{"name": "Lock screen Spotlight restored (Personalization subtree preserved)", "ok": True}]
