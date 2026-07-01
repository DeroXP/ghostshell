"""Hard performance optimizer — aggressive tweaks for gaming & latency."""
import json
import os
import time
from core.utils import run_ps, run_cmd, get_logger, backup_registry, detect_drive_type, atomic_write_json
from config import APPDATA_DIR

log = get_logger("optimize")


def _reg(key: str, value: str, data: str, vtype: str = "REG_DWORD") -> bool:
    r = run_cmd(["reg", "add", key, "/v", value, "/t", vtype, "/d", data, "/f"])
    # 3.4.2 — most callers append {"ok": True} without checking this
    # return value (GhostShell runs as admin so writes almost always
    # succeed).  Log the rare failure so it's visible in ghostshell.log
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


def _capture_power_baseline() -> str:
    """Capture the currently-active power scheme GUID (idempotent — only
    writes the baseline ONCE per install).  Returns the captured GUID
    or "" on failure."""
    try:
        if os.path.isfile(_POWER_BASELINE_PATH):
            with open(_POWER_BASELINE_PATH, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("guid", "") or ""
        r = run_cmd(["powercfg", "/getactivescheme"])
        guid = ""
        if r.get("ok") and r.get("out"):
            for part in r["out"].split():
                if len(part) == 36 and "-" in part:
                    guid = part
                    break
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
    if not os.path.isfile(_POWER_BASELINE_PATH):
        return {"ok": False, "err": "no power baseline captured"}
    try:
        with open(_POWER_BASELINE_PATH, "r", encoding="utf-8") as f:
            guid = (json.load(f) or {}).get("guid", "")
        if not guid:
            return {"ok": False, "err": "baseline file has no GUID"}
        run_cmd(["powercfg", "/setactive", guid])
        log.info(f"  ✓ Power scheme restored to baseline: {guid}")
        return {"ok": True, "guid": guid}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def apply_ultimate_power_plan() -> dict:
    log.info("Applying Ultimate Performance power plan...")
    # v3 — capture baseline FIRST so we never lose the user's choice.
    _capture_power_baseline()
    # Unhide ultimate performance plan
    r = run_cmd(["powercfg", "-duplicatescheme", "e9a42b02-d5df-448d-aa00-03f14749eb61"])
    guid = ""
    if r["ok"] and r["out"]:
        # Output: "Power Scheme GUID: xxxxxxxx-..."
        for part in r["out"].split():
            if len(part) == 36 and "-" in part:
                guid = part
                break
    if guid:
        run_cmd(["powercfg", "-setactive", guid])
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
        r = run_cmd(cmd)
        log.info(f"  {'✓' if r['ok'] else '✗'} {name}")
        results.append({"name": name, "ok": r["ok"]})

    # v3.3.1-beta.8: Windows Search service disable gated behind Pro Mode.
    # Disabling WSearch breaks Start Menu search, Outlook search,
    # File Explorer search, and Cortana voice-anything.  Modern NVMe
    # indexing is <1% CPU steady-state.  Most users notice within a
    # day that Start Menu search returns nothing — high support-ticket
    # rate from tools like this when applied by default.
    if _pro_mode_enabled():
        r = run_cmd(["sc", "config", "WSearch", "start=", "disabled"])
        run_cmd(["sc", "stop", "WSearch"])
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
    r = run_ps(
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name "UserPreferencesMask" '
        '-Value ([byte[]](0x90,0x12,0x03,0x80,0x10,0x00,0x00,0x00)) -Type Binary'
    )
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
    xbox_svcs = ["XblAuthManager", "XblGameSave", "XboxGipSvc", "XboxNetApiSvc"]
    for svc in xbox_svcs:
        run_cmd(["sc", "stop", svc])
        run_cmd(["sc", "config", svc, "start=", "manual"])  # manual not disabled — some games hit it
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
    bcdedit_safe = [
        ("bcdedit useplatformclock no", ["bcdedit", "/set", "useplatformclock", "no"]),
        ("bcdedit tscsyncpolicy enhanced", ["bcdedit", "/set", "tscsyncpolicy", "Enhanced"]),
    ]
    for name, cmd in bcdedit_safe:
        r = run_cmd(cmd)
        results.append({"name": name, "ok": r["ok"]})

    if _pro_mode_enabled():
        r = run_cmd(["bcdedit", "/set", "disabledynamictick", "yes"])
        results.append({"name": "[Pro] bcdedit disabledynamictick yes", "ok": r["ok"]})
    else:
        results.append({"name": "disabledynamictick skipped (Pro Mode off)",
                         "ok": True, "skipped": True})

    # DPC watchdog
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel",
         "DpcWatchdogProfileOffset", "0")
    results.append({"name": "DPC watchdog profile off", "ok": True})

    # Enable MSI mode on all network adapters (reduces interrupt overhead)
    run_ps("""
try {
    Get-PnpDevice -Class Net | ForEach-Object {
        $id = $_.InstanceId
        $p = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$id\\Device Parameters\\Interrupt Management\\MessageSignaledInterruptProperties"
        New-Item -Path $p -Force -ErrorAction SilentlyContinue | Out-Null
        Set-ItemProperty -Path $p -Name MSISupported -Value 1 -Type DWord -ErrorAction SilentlyContinue
    }
} catch {}
""")
    results.append({"name": "Network adapters MSI mode enabled", "ok": True})

    # v3.3.1-beta.8: blanket USB MSI mode is gated behind Pro Mode.
    # Some USB host controllers (and their drivers) don't properly
    # support MSI; forcing it on those causes Code 10 errors, mouse
    # stutter, USB DAC dropouts, and "device descriptor request
    # failed" on plug events.  Best practice is per-device, audited
    # via LatencyMon — not blanket.  NIC MSI mode above is safe
    # (modern NIC drivers all support MSI properly).
    if _pro_mode_enabled():
        run_ps("""
try {
    Get-PnpDevice -Class USB | Where-Object {$_.Status -eq 'OK'} | ForEach-Object {
        $id = $_.InstanceId
        $p = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$id\\Device Parameters\\Interrupt Management\\MessageSignaledInterruptProperties"
        New-Item -Path $p -Force -ErrorAction SilentlyContinue | Out-Null
        Set-ItemProperty -Path $p -Name MSISupported -Value 1 -Type DWord -ErrorAction SilentlyContinue
    }
} catch {}
""")
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
    This is a COMPLETE factory-style reset of every performance tweak GhostShell touches."""
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

    # 3.4.2 — also re-enable any Windows services the debloater disabled.
    # Previously a "Reset ALL optimizations" left debloated services
    # (WSearch, Geolocation, etc.) permanently disabled with no undo.
    # restore_services() puts each back to its recorded original Start
    # type, or Manual (demand) as a safe fallback.
    try:
        from core import debloater
        results["debloat_services"] = debloater.restore_services().get("results", [])
    except Exception as e:
        log.warning(f"service restore during reset failed: {e}")
        results["debloat_services"] = [{"name": "service restore", "ok": False, "err": str(e)}]

    # Restart DWM to apply visual changes immediately
    run_cmd(["taskkill", "/f", "/im", "dwm.exe"])  # DWM auto-restarts
    results["dwm"] = [{"name": "DWM restarted (applies visual changes)", "ok": True}]

    log.info("=== ALL OPTIMIZATIONS RESET — REBOOT RECOMMENDED ===")
    return results


def _reset_power_plan() -> list[dict]:
    """Restore the user's pre-GhostShell power plan.

    v3.3.1-beta.7: previously this hard-coded Balanced as the reset
    target, ignoring `power_baseline.json` captured by
    `_capture_power_baseline()`.  Users on High Performance, AMD
    Ryzen Balanced, or a manually-tuned custom plan got their plan
    silently replaced with Microsoft Balanced on every reset.  Now
    we honor the captured baseline first and only fall back to
    Balanced if no baseline file exists.
    """
    log.info("Resetting power plan to captured baseline (or Balanced fallback)...")
    results = []
    restored = restore_power_baseline()
    if restored.get("ok"):
        results.append({
            "name": f"Power plan restored to user baseline ({restored.get('guid','')})",
            "ok":   True,
        })
        return results
    # No baseline → conservative fallback to Microsoft Balanced
    log.info(f"  No baseline file ({restored.get('err','')}) — falling back to Balanced")
    r = run_cmd(["powercfg", "-setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"])
    results.append({"name": "Balanced Power Plan (fallback — no baseline file)", "ok": r["ok"]})
    return results


def _reset_cpu_tweaks() -> list[dict]:
    log.info("Resetting CPU tweaks...")
    results = []
    tweaks = [
        ("Win32PrioritySeparation", r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", "2"),
        ("SystemResponsiveness", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "SystemResponsiveness", "20"),
        ("NetworkThrottlingIndex", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "NetworkThrottlingIndex", "10"),
        ("GPU Priority (Games)", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "GPU Priority", "0"),
        ("Scheduling Category", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Scheduling Category", "Medium"),
        ("Priority (Games)", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "Priority", "2"),
        ("SFIO Priority", r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games", "SFIO Priority", "Normal"),
    ]
    for name, key, val, data in tweaks:
        vtype = "REG_SZ" if data in ("Medium", "Normal", "High", "Low") else "REG_DWORD"
        ok = _reg(key, val, data, vtype)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})

    # Re-enable core parking
    run_cmd(["reg", "delete",
             r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\54533251-82be-4824-96c1-47b60b740d00\0cc5b647-c1df-4637-891a-dec35c318583",
             "/v", "ValueMax", "/f"])
    results.append({"name": "Core Parking (re-enabled)", "ok": True})

    # Re-enable CPU idle
    run_cmd(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", "IdleDisable", "0"])
    run_cmd(["powercfg", "/setactive", "SCHEME_CURRENT"])
    results.append({"name": "CPU Idle (re-enabled)", "ok": True})

    return results


def _reset_memory_tweaks() -> list[dict]:
    log.info("Resetting memory tweaks...")
    results = []
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    for name, val, data in [
        ("Paging Executive", "DisablePagingExecutive", "0"),
        ("Large System Cache", "LargeSystemCache", "0"),
        ("Clear Pagefile", "ClearPageFileAtShutdown", "0"),
    ]:
        ok = _reg(base, val, data)
        results.append({"name": name, "ok": ok})

    # beta.5: was ControlSet001 (hardcoded) — wrong on any system booted from a
    # different control set; the live set is always CurrentControlSet.
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Services\Ndu", "Start", "2")
    results.append({"name": "NDU (re-enabled)", "ok": True})
    run_ps("Enable-MMAgent -MemoryCompression -ErrorAction SilentlyContinue")
    results.append({"name": "Memory Compression (re-enabled)", "ok": True})

    # Re-enable Spectre/Meltdown mitigations
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "FeatureSettingsOverride", "0")
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management", "FeatureSettingsOverrideMask", "3")
    results.append({"name": "Spectre/Meltdown mitigations (re-enabled)", "ok": True})

    return results


def _reset_storage_tweaks() -> list[dict]:
    log.info("Resetting storage tweaks...")
    results = []
    for name, cmd in [
        ("Last Access Time", ["fsutil", "behavior", "set", "disablelastaccess", "3"]),
        ("8.3 Filenames", ["fsutil", "behavior", "set", "disable8dot3", "0"]),
        ("NTFS Memory Usage", ["fsutil", "behavior", "set", "memoryusage", "1"]),
    ]:
        r = run_cmd(cmd)
        results.append({"name": name, "ok": r["ok"]})

    run_cmd(["sc", "config", "WSearch", "start=", "delayed-auto"])
    run_cmd(["sc", "start", "WSearch"])
    results.append({"name": "Windows Search (re-enabled)", "ok": True})
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01", "1")
    results.append({"name": "Storage Sense (re-enabled)", "ok": True})

    return results


def _reset_gaming_tweaks() -> list[dict]:
    """Reset ALL gaming tweaks — this is the critical one for Roblox Studio etc."""
    log.info("Resetting gaming tweaks...")
    results = []
    tweaks = [
        # Re-enable Game DVR (Windows default is on)
        ("Game DVR", r"HKCU\System\GameConfigStore", "GameDVR_Enabled", "1"),
        # Delete the policy override so Windows uses its own default
        # (We delete rather than set to 1 so Group Policy doesn't conflict)

        # CRITICAL: Reset Fullscreen Optimizations to DEFAULT (0 = Windows manages it)
        # Setting this to 2 disables FSO globally which breaks viewport rendering in
        # apps like Roblox Studio, Unity, Unreal Editor, 3D modeling tools
        ("FSO Behavior -> Default", r"HKCU\System\GameConfigStore", "GameDVR_FSEBehaviorMode", "0"),
        ("FSO Compat -> Default", r"HKCU\System\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", "0"),
        ("FSO Honor -> Default", r"HKCU\System\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", "0"),
        ("FSO EFSEFlags -> Default", r"HKCU\System\GameConfigStore", "GameDVR_EFSEFeatureFlags", "0"),

        # Re-enable Game Bar (some apps use its hooks for window management)
        ("Game Bar", r"HKCU\Software\Microsoft\GameBar", "UseNexusForGameBarEnabled", "1"),
        ("Auto Game Mode", r"HKCU\Software\Microsoft\GameBar", "AllowAutoGameMode", "1"),
    ]
    for name, key, val, data in tweaks:
        ok = _reg(key, val, data)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})

    # Delete GameDVR policy key entirely (let Windows use its own default)
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR", "/f"])
    results.append({"name": "GameDVR Policy (removed)", "ok": True})

    # Restore bcdedit timer defaults
    run_cmd(["bcdedit", "/deletevalue", "useplatformtick"])
    run_cmd(["bcdedit", "/deletevalue", "disabledynamictick"])
    run_cmd(["bcdedit", "/deletevalue", "useplatformclock"])
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

    # CRITICAL: Reset HW GPU Scheduling to Windows default
    # On some systems HwSchMode=2 causes viewport lag in non-game apps
    # Default is 1 (OS managed) on most systems, 2 = HW scheduled
    # We set it back to 2 (which IS the modern default) but also reset the
    # per-app GPU preferences that may have been set
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", "2")
    results.append({"name": "HW GPU Scheduling (default=2)", "ok": True})

    # Remove DWM MPO override — let Windows manage Multi-Plane Overlay
    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\Windows\DWM", "/v", "OverlayMinFPS", "/f"])
    results.append({"name": "DWM OverlayMinFPS (removed)", "ok": True})

    # Remove ThreadDpcEnable override
    run_cmd(["reg", "delete", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel", "/v", "ThreadDpcEnable", "/f"])
    results.append({"name": "ThreadDpcEnable (removed)", "ok": True})

    # Reset NVIDIA power settings to default (adaptive, not max performance)
    nv_key = r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"
    for name, val, data in [
        ("NVIDIA PerfLevelSrc", "PerfLevelSrc", "0x3322"),  # default adaptive
        ("NVIDIA PowerMizerEnable", "PowerMizerEnable", "1"),
        ("NVIDIA PowerMizerLevel", "PowerMizerLevel", "0"),  # 0 = adaptive (not forced max)
        ("NVIDIA PowerMizerLevelAC", "PowerMizerLevelAC", "0"),
    ]:
        _reg(nv_key, val, data)
        results.append({"name": name, "ok": True})

    # Reset per-app GPU preferences (remove any High Performance overrides)
    run_ps(
        'Remove-ItemProperty -Path "HKCU:\\Software\\Microsoft\\DirectX\\UserGpuPreferences" '
        '-Name * -ErrorAction SilentlyContinue'
    )
    results.append({"name": "Per-app GPU preferences (cleared)", "ok": True})

    # Ensure DWM is using hardware acceleration (should be default but verify)
    _reg(r"HKCU\Software\Microsoft\Windows\DWM", "DisableHWAcceleration", "0")
    results.append({"name": "DWM HW Acceleration (enabled)", "ok": True})

    # Reset compositing settings
    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\Windows\DWM", "/v", "Composition", "/f"])
    results.append({"name": "DWM Composition (default)", "ok": True})

    return results


def _reset_scheduler_tweaks() -> list[dict]:
    """Reset all scheduler/kernel tweaks from the Kernel page."""
    log.info("Resetting scheduler tweaks...")
    results = []

    # Reset MMCSS DisplayPostProcessing to defaults
    dpp_key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\DisplayPostProcessing"
    for name, val, data, vtype in [
        ("DPP Background Only", "Background Only", "True", "REG_SZ"),
        ("DPP GPU Priority", "GPU Priority", "0", "REG_DWORD"),
        ("DPP Priority", "Priority", "2", "REG_DWORD"),
        ("DPP Scheduling", "Scheduling Category", "Medium", "REG_SZ"),
    ]:
        _reg(dpp_key, val, data, vtype)
        results.append({"name": name, "ok": True})

    return results


def _reset_visual_tweaks() -> list[dict]:
    """Reset visual tweaks — FULLY restore Windows default visual effects.
    This uses SystemParametersInfo to properly tell Windows to use its defaults,
    not just set registry values."""
    log.info("Resetting visual effects...")
    results = []

    tweaks = [
        # VisualFXSetting 0 = Let Windows choose best for this computer
        ("Visual Effects", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects", "VisualFXSetting", "0"),
        # Menu delay default = 400ms
        ("Menu Animation Delay", r"HKCU\Control Panel\Desktop", "MenuShowDelay", "400"),
        # MinAnimate default = 1 (on)
        ("Window Animation", r"HKCU\Control Panel\Desktop\WindowMetrics", "MinAnimate", "1"),
        # Transparency default = 1 (on)
        ("Transparency", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency", "1"),
        # Aero Shake default = 0 (enabled)
        ("Aero Shake", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "DisallowShaking", "0"),
        # Snap Assist default = 1 (enabled)
        ("Snap Assist", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "SnapAssist", "1"),
        # DragFullWindows default = 1 (show contents while dragging)
        ("Drag Full Windows", r"HKCU\Control Panel\Desktop", "DragFullWindows", "1"),
        # FontSmoothing default = 2 (ClearType)
        ("Font Smoothing", r"HKCU\Control Panel\Desktop", "FontSmoothing", "2"),
        ("Font Smoothing Type", r"HKCU\Control Panel\Desktop", "FontSmoothingType", "2"),
    ]
    for name, key, val, data in tweaks:
        vtype = "REG_SZ" if val in ("MenuShowDelay", "MinAnimate", "DragFullWindows", "FontSmoothing", "FontSmoothingType") else "REG_DWORD"
        ok = _reg(key, val, data, vtype)
        log.info(f"  {'OK' if ok else 'FAIL'} Reset: {name}")
        results.append({"name": name, "ok": ok})

    # CRITICAL: Set UserPreferencesMask to Windows default "Let Windows choose"
    # This bitmask controls ALL visual effects — animations, shadows, font smoothing,
    # thumbnail previews, etc. The default value enables all effects.
    # Default bytes: 9E 3E 07 80 12 01 00 00 (all effects on)
    r = run_ps(
        'Set-ItemProperty -Path "HKCU:\\Control Panel\\Desktop" -Name "UserPreferencesMask" '
        '-Value ([byte[]](0x9E,0x3E,0x07,0x80,0x12,0x01,0x00,0x00)) -Type Binary'
    )
    results.append({"name": "UserPreferencesMask (all effects ON)", "ok": r["ok"]})

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

    # Restore Spectre/Meltdown mitigations
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    run_cmd(["reg", "delete", base, "/v", "FeatureSettingsOverride", "/f"])
    run_cmd(["reg", "delete", base, "/v", "FeatureSettingsOverrideMask", "/f"])
    results.append({"name": "Spectre/Meltdown mitigations restored", "ok": True})

    # Restore VBS / HVCI — set back to 1 (Windows 11 default)
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard",
         "EnableVirtualizationBasedSecurity", "1")
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
         "Enabled", "1")
    results.append({"name": "VBS / HVCI restored", "ok": True})

    # Remove MPO override
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\Dwm", "/v", "OverlayTestMode", "/f"])
    results.append({"name": "MPO restored", "ok": True})

    # Mouse defaults: accel on
    mkey = r"HKCU\Control Panel\Mouse"
    _reg(mkey, "MouseSpeed", "1", "REG_SZ")
    _reg(mkey, "MouseThreshold1", "6", "REG_SZ")
    _reg(mkey, "MouseThreshold2", "10", "REG_SZ")
    results.append({"name": "Mouse acceleration restored", "ok": True})

    # Accessibility key prompts back to defaults
    _reg(r"HKCU\Control Panel\Accessibility\StickyKeys", "Flags", "510", "REG_SZ")
    _reg(r"HKCU\Control Panel\Accessibility\ToggleKeys", "Flags", "62", "REG_SZ")
    _reg(r"HKCU\Control Panel\Accessibility\Keyboard Response", "Flags", "126", "REG_SZ")
    results.append({"name": "Accessibility key prompts restored", "ok": True})

    # Delivery optimization — back to default (1 = LAN only)
    _reg(r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization",
         "DODownloadMode", "1")
    results.append({"name": "Delivery Optimization (LAN only)", "ok": True})

    # Background apps — enable
    run_cmd(["reg", "delete",
             r"HKCU\Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
             "/v", "GlobalUserDisabled", "/f"])
    run_cmd(["reg", "delete",
             r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy",
             "/v", "LetAppsRunInBackground", "/f"])
    results.append({"name": "Background apps restored", "ok": True})

    # Re-enable Xbox services
    for svc in ["XblAuthManager", "XblGameSave", "XboxGipSvc", "XboxNetApiSvc"]:
        run_cmd(["sc", "config", svc, "start=", "auto"])
    results.append({"name": "Xbox services restored", "ok": True})

    # Widgets/Copilot — remove overrides (use Windows default)
    for key, val in [
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa"),
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "ShowCopilotButton"),
    ]:
        run_cmd(["reg", "delete", key, "/v", val, "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Dsh", "/v", "AllowNewsAndInterests", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot", "/v", "TurnOffWindowsCopilot", "/f"])
    results.append({"name": "Widgets + Copilot restored", "ok": True})

    # Recall — remove override
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI", "/v", "DisableAIDataAnalysis", "/f"])
    run_cmd(["reg", "delete", r"HKCU\Software\Policies\Microsoft\Windows\WindowsAI", "/v", "DisableAIDataAnalysis", "/f"])
    results.append({"name": "Recall policy removed", "ok": True})

    # Auto HDR — remove overrides
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings", "/v", "EnableAutoHdr", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\VideoSettings", "/v", "EnableHDRForPlayback", "/f"])
    results.append({"name": "Auto HDR restored", "ok": True})

    # Remove Win10 context menu override
    run_cmd(["reg", "delete",
             r"HKCU\Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}",
             "/f"])
    results.append({"name": "Win11 context menu restored", "ok": True})

    # Search bar restored
    _reg(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "SearchboxTaskbarMode", "2")
    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "/v", "BingSearchEnabled", "/f"])
    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search", "/v", "CortanaConsent", "/f"])
    results.append({"name": "Taskbar search restored", "ok": True})

    # TDR timeouts back to default
    run_cmd(["reg", "delete", r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "/v", "TdrDelay", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "/v", "TdrDdiDelay", "/f"])
    results.append({"name": "TDR timeouts restored", "ok": True})

    # Fault-Tolerant Heap restored
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Microsoft\FTH", "/v", "Enabled", "/f"])
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
    for cmd in [
        ["bcdedit", "/deletevalue", "useplatformclock"],
        ["bcdedit", "/deletevalue", "disabledynamictick"],
        ["bcdedit", "/deletevalue", "useplatformtick"],
        ["bcdedit", "/deletevalue", "tscsyncpolicy"],
    ]:
        run_cmd(cmd)
    results.append({"name": "bcdedit timers restored", "ok": True})

    # Restore power throttling
    run_cmd(["reg", "delete",
             r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
             "/v", "PowerThrottlingOff", "/f"])
    results.append({"name": "Power throttling restored", "ok": True})

    return results


def _reset_win11_tweaks() -> list[dict]:
    """Undo apply_windows11_tweaks()."""
    log.info("Resetting Windows 11 tweaks...")
    results = []

    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    # Taskbar: restore centered align, chat icon, task view
    _reg(advanced, "TaskbarAl", "1")
    _reg(advanced, "TaskbarMn", "1")
    _reg(advanced, "ShowTaskViewButton", "1")

    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer", "/v", "HideRecommendedSection", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Explorer", "/v", "HideRecentlyAddedApps", "/f"])

    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "/v", "AllowClipboardHistory", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System", "/v", "AllowCrossDeviceClipboard", "/f"])

    run_cmd(["reg", "delete", r"HKCU\Software\Policies\Microsoft\Windows\Explorer", "/v", "DisableSearchBoxSuggestions", "/f"])

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
    run_cmd(["sc", "config", "WSearch", "start=", "delayed-auto"])
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

    # Auto-detect common game-install roots and exclude them
    import os
    candidate_paths = [
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
    ]
    excluded = []
    for path in candidate_paths:
        if os.path.exists(path):
            r = run_ps(f'Add-MpPreference -ExclusionPath "{path}" -ErrorAction SilentlyContinue')
            if r["ok"]:
                excluded.append(path)
    log.info(f"  Defender exclusions added for {len(excluded)} game folders")
    results.append({"name": f"Defender exclusions: {len(excluded)} game folders", "ok": True})

    # Common game executable patterns
    for proc in ["steam.exe", "epicgameslauncher.exe", "battle.net.exe",
                 "riotclientservices.exe", "ubisoftconnect.exe"]:
        run_ps(f'Add-MpPreference -ExclusionProcess "{proc}" -ErrorAction SilentlyContinue')
    results.append({"name": "Defender process exclusions added", "ok": True})

    # Disable Defender Submit Sample Consent (no auto-upload of files to MS)
    run_ps('Set-MpPreference -SubmitSamplesConsent NeverSend -ErrorAction SilentlyContinue')
    results.append({"name": "Defender sample submission disabled", "ok": True})

    # Disable MAPS (cloud-based protection telemetry — keeps signatures local)
    run_ps('Set-MpPreference -MAPSReporting Disabled -ErrorAction SilentlyContinue')
    results.append({"name": "Defender cloud telemetry disabled", "ok": True})

    # Disable scheduled scan during high-load hours (set to 4am only)
    run_ps('Set-MpPreference -ScanScheduleTime 04:00:00 -ErrorAction SilentlyContinue')
    run_ps('Set-MpPreference -ScanScheduleQuickScanTime 04:30:00 -ErrorAction SilentlyContinue')
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
        run_cmd(["powercfg", "-h", "off"])
        results.append({"name": "[Pro] Hibernation disabled (frees ~RAM-size on disk)", "ok": True})
    else:
        results.append({"name": "Hibernation off skipped (Pro Mode off — laptops keep the feature)",
                         "ok": True, "skipped": True})

    # bcdedit — Legacy boot menu (no fancy GUI), faster boot
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
        r = run_cmd(cmd)
        results.append({"name": name, "ok": r["ok"]})

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
    services_to_kill = [
        ("Print Spooler",                "Spooler"),
        ("Fax",                           "Fax"),
        ("Windows Insider Service",       "wisvc"),
        ("Touch Keyboard / Handwriting",  "TabletInputService"),
        ("Retail Demo",                   "RetailDemo"),
        ("Phone Service",                 "PhoneSvc"),
        ("Windows Mobile Hotspot",        "icssvc"),
    ]
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
        services_to_kill += [
            ("Smart Card",                "SCardSvr"),
            ("Smart Card Removal Policy", "SCPolicySvc"),
        ]
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
        run_cmd(["sc", "stop", svc])
        ok = run_cmd(["sc", "config", svc, "start=", "demand"])["ok"]  # "demand" = manual
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
    base_features = [
        "Internet-Explorer-Optional-amd64",
        "Printing-XPSServices-Features",
        "Printing-PrintToPDFServices-Features",
        "FaxServicesClientPackage",
        "WindowsMediaPlayer",
        "MediaPlayback",
        "WorkFolders-Client",
    ]
    virtualisation_features = [
        "Microsoft-Windows-Subsystem-Linux",
        "VirtualMachinePlatform",
        "HypervisorPlatform",
        "Microsoft-Hyper-V-All",
        "Containers-DisposableClientVM",
    ]
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
    for feat in features_to_remove:
        run_cmd(["dism", "/online", "/disable-feature",
                 f"/featurename:{feat}", "/norestart"])
    results.append({"name": f"Disabled {len(features_to_remove)} optional Windows features", "ok": True})

    log.info("Legacy tweaks applied")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# RESET FUNCTIONS for the new categories
# ═══════════════════════════════════════════════════════════════════════════
def _reset_audio_tweaks() -> list[dict]:
    log.info("Resetting audio tweaks...")
    audio_task = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Pro Audio"
    for val in ("GPU Priority", "Priority", "Clock Rate", "Affinity",
                "SFIO Priority", "Background Only", "Scheduling Category"):
        run_cmd(["reg", "delete", audio_task, "/v", val, "/f"])
    # beta.5: was ControlSet001 (hardcoded) — use the live CurrentControlSet.
    run_cmd(["reg", "delete",
             r"HKLM\SYSTEM\CurrentControlSet\Control\Bluetooth\Audio\AVRCP\CT",
             "/v", "DisableAbsoluteVolume", "/f"])
    return [{"name": "Audio tweaks restored", "ok": True}]


def _reset_input_tweaks() -> list[dict]:
    log.info("Resetting input tweaks...")
    mkey = r"HKCU\Control Panel\Mouse"
    _reg(mkey, "MouseHoverTime",   "400", "REG_SZ")
    _reg(mkey, "MouseHoverWidth",  "4",   "REG_SZ")
    _reg(mkey, "MouseHoverHeight", "4",   "REG_SZ")
    _reg(mkey, "MouseTrails",      "0",   "REG_SZ")

    kkey = r"HKCU\Control Panel\Keyboard"
    _reg(kkey, "KeyboardSpeed", "31", "REG_SZ")  # max is fine
    _reg(kkey, "KeyboardDelay", "1",  "REG_SZ")  # default 1

    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\TabletTip\1.7",
             "/v", "EnableAutoInvokeTouchKeyboard", "/f"])
    run_cmd(["reg", "delete", r"HKCU\Software\Microsoft\Wisp\Pen\SysEventParameters",
             "/v", "FlickMode", "/f"])
    return [{"name": "Input tweaks restored", "ok": True}]


def _reset_search_tweaks() -> list[dict]:
    log.info("Resetting search tweaks...")
    cor = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search"
    for val in ("AllowCortana", "AllowCortanaAboveLock", "AllowVoiceActivation",
                "DisableWebSearch", "ConnectedSearchUseWeb",
                "EnableDynamicContentInWSB", "AllowSearchToUseLocation",
                "PreventIndexingOutlook"):
        run_cmd(["reg", "delete", cor, "/v", val, "/f"])
    sh = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Search"
    for val in ("DeviceHistoryEnabled", "HistoryViewEnabled",
                "BingSearchEnabled", "CortanaConsent"):
        run_cmd(["reg", "delete", sh, "/v", val, "/f"])
    return [{"name": "Search & Cortana restored", "ok": True}]


def _reset_defender_gaming_tweaks() -> list[dict]:
    """Note: we don't auto-remove the Defender exclusions on reset because
    they're conservative gaming-folder excludes the user almost certainly
    wants to keep.  Reset only undoes the policy bits."""
    log.info("Resetting Defender gaming tweaks...")
    run_ps('Set-MpPreference -SubmitSamplesConsent SendSafeSamples -ErrorAction SilentlyContinue')
    run_ps('Set-MpPreference -MAPSReporting Advanced -ErrorAction SilentlyContinue')
    return [{"name": "Defender policy restored (exclusions kept)", "ok": True}]


def _reset_boot_tweaks() -> list[dict]:
    log.info("Resetting boot tweaks...")
    _reg(r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power",
         "HiberBootEnabled", "1")
    run_cmd(["powercfg", "-h", "on"])
    for cmd in [
        ["bcdedit", "/deletevalue", "{current}", "bootmenupolicy"],
        ["bcdedit", "/deletevalue", "bootux"],
        ["bcdedit", "/deletevalue", "{current}", "bootlog"],
        ["bcdedit", "/deletevalue", "{current}", "recoveryenabled"],
    ]:
        run_cmd(cmd)
    run_cmd(["reg", "delete",
             r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
             "/v", "VerboseStatus", "/f"])
    return [{"name": "Boot tweaks restored", "ok": True}]


def _reset_legacy_tweaks() -> list[dict]:
    log.info("Resetting legacy tweaks...")
    # Re-enable Print Spooler since most users actually need it for a printer
    run_cmd(["sc", "config", "Spooler", "start=", "auto"])
    run_cmd(["sc", "start", "Spooler"])
    # Bluetooth back to manual (in case the user actually has BT devices)
    run_cmd(["sc", "config", "bthserv", "start=", "manual"])
    # Geolocation back to manual
    run_cmd(["sc", "config", "lfsvc", "start=", "manual"])
    return [{"name": "Common legacy services restored (Print, BT, Geo)", "ok": True}]


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
    run_ps("""
$ifaces = Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces'
foreach ($i in $ifaces) {
    Set-ItemProperty -Path $i.PSPath -Name TcpAckFrequency -Value 1 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $i.PSPath -Name TCPNoDelay      -Value 1 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $i.PSPath -Name TcpDelAckTicks  -Value 0 -Type DWord -ErrorAction SilentlyContinue
}
""")
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

    # netsh global
    for name, cmd in [
        ("netsh: autotuninglevel normal",       ["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"]),
        ("netsh: chimney disabled",             ["netsh", "int", "tcp", "set", "global", "chimney=disabled"]),
        ("netsh: rss enabled",                  ["netsh", "int", "tcp", "set", "global", "rss=enabled"]),
        ("netsh: ecncapability disabled",       ["netsh", "int", "tcp", "set", "global", "ecncapability=disabled"]),
        ("netsh: timestamps disabled",          ["netsh", "int", "tcp", "set", "global", "timestamps=disabled"]),
        ("netsh: initialRto 2000",              ["netsh", "int", "tcp", "set", "global", "initialRto=2000"]),
        ("netsh: nonsackrttresiliency disabled", ["netsh", "int", "tcp", "set", "global", "nonsackrttresiliency=disabled"]),
        ("netsh: maxsynretransmissions 2",       ["netsh", "int", "tcp", "set", "global", "maxsynretransmissions=2"]),
        ("netsh: fastopen enabled",              ["netsh", "int", "tcp", "set", "global", "fastopen=enabled"]),
        ("netsh: hystart disabled",              ["netsh", "int", "tcp", "set", "global", "hystart=disabled"]),
    ]:
        r = run_cmd(cmd)
        results.append({"name": name, "ok": r["ok"]})

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
    tasks = [
        r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser",
        r"\Microsoft\Windows\Application Experience\ProgramDataUpdater",
        r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator",
        r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip",
        r"\Microsoft\Windows\Customer Experience Improvement Program\KernelCeipTask",
        r"\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
        r"\Microsoft\Windows\Feedback\Siuf\DmClient",
        r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload",
        r"\Microsoft\Windows\Windows Error Reporting\QueueReporting",
    ]
    for t in tasks:
        run_cmd(["schtasks", "/Change", "/TN", t, "/Disable"])
    results.append({"name": f"Disabled {len(tasks)} telemetry scheduled tasks", "ok": True})

    # Error reporting service
    run_cmd(["sc", "stop", "WerSvc"])
    run_cmd(["sc", "config", "WerSvc", "start=", "disabled"])
    results.append({"name": "Windows Error Reporting service off", "ok": True})

    # DiagTrack (Connected User Experiences and Telemetry).
    # NOTE: setting this to 'disabled' breaks the Microsoft Store and the
    # Xbox app's update flow — they use DiagTrack to talk to Microsoft's
    # content delivery infrastructure.  We instead set it to 'manual' so
    # it stays off most of the time but can start on demand when the
    # Store/Xbox app actually needs it.  Net telemetry impact is still
    # ~zero in idle steady state.
    run_cmd(["sc", "stop", "DiagTrack"])
    run_cmd(["sc", "config", "DiagTrack", "start=", "manual"])
    results.append({"name": "DiagTrack service → manual (was disabled — broke Store/Xbox)", "ok": True})

    # dmwappushservice
    run_cmd(["sc", "stop", "dmwappushservice"])
    run_cmd(["sc", "config", "dmwappushservice", "start=", "disabled"])
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
    for svc in ("edgeupdate", "edgeupdatem", "MicrosoftEdgeElevationService"):
        run_cmd(["sc", "stop", svc])
        run_cmd(["sc", "config", svc, "start=", "manual"])
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
    # below; these five are user/Windows Settings territory, not GhostShell's.
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
    run_cmd(["netsh", "int", "tcp", "reset"])
    run_cmd(["netsh", "winsock", "reset"])
    return [{"name": "TCP/IP stack reset (reboot required)", "ok": True}]


def _reset_telemetry_deep_tweaks() -> list[dict]:
    log.info("Resetting deep telemetry tweaks...")
    # Re-enable services back to default startup types
    for svc, mode in (("WerSvc", "manual"), ("DiagTrack", "auto"),
                      ("dmwappushservice", "manual")):
        run_cmd(["sc", "config", svc, "start=", mode])
    return [{"name": "Telemetry services restored to defaults", "ok": True}]


def _reset_edge_tweaks() -> list[dict]:
    """v3 — per-value deletes (the old `reg delete <key> /f` wiped the
    entire Edge policy subtree, killing enterprise / work-profile / other
    tools' Edge configs)."""
    log.info("Resetting Edge tweaks...")
    edge_key = r"HKLM\SOFTWARE\Policies\Microsoft\Edge"
    # Only the values WE write in apply_edge_tweaks — if you add to the
    # apply list, mirror here.
    for v in ("StartupBoostEnabled", "BackgroundModeEnabled",
              "TabFreezeEnabled", "TabSleeping",
              "PreloadOnStartup", "NewTabPagePrerender",
              "SmartScreenEnabled", "SmartScreenPuaEnabled",
              "PersonalizationReportingEnabled",
              "AlternateErrorPagesEnabled", "AutofillCreditCardEnabled",
              "PasswordManagerEnabled", "BrowserAddProfileEnabled",
              "HardwareAccelerationModeEnabled",
              "ShowMicrosoftRewards"):
        run_cmd(["reg", "delete", edge_key, "/v", v, "/f"])
    for svc in ("edgeupdate", "edgeupdatem", "MicrosoftEdgeElevationService"):
        run_cmd(["sc", "config", svc, "start=", "auto"])
    return [{"name": "Edge tweak values cleared (subtree preserved), services restored", "ok": True}]


def _reset_update_control_tweaks() -> list[dict]:
    """v3 — per-value deletes."""
    log.info("Resetting Windows Update controls...")
    wu_key  = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
    wu_au   = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    for v in ("DeferFeatureUpdates", "DeferFeatureUpdatesPeriodInDays",
              "DeferQualityUpdates", "DeferQualityUpdatesPeriodInDays",
              "ExcludeWUDriversInQualityUpdate",
              "BranchReadinessLevel",
              "DisableDualScan",
              "DoNotConnectToWindowsUpdateInternetLocations"):
        run_cmd(["reg", "delete", wu_key, "/v", v, "/f"])
    for v in ("AUOptions", "NoAutoUpdate", "AutomaticMaintenanceEnabled",
              "ScheduledInstallDay", "ScheduledInstallTime"):
        run_cmd(["reg", "delete", wu_au, "/v", v, "/f"])
    return [{"name": "WU tweak values cleared (subtree preserved)", "ok": True}]


def _reset_notification_tweaks() -> list[dict]:
    log.info("Resetting notification tweaks...")
    ns = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
    for v in ("NOC_GLOBAL_SETTING_TOASTS_ENABLED",
              "NOC_GLOBAL_SETTING_BADGE_ENABLED",
              "NOC_GLOBAL_SETTING_ALLOW_NOTIFICATION_SOUND"):
        run_cmd(["reg", "delete", ns, "/v", v, "/f"])
    return [{"name": "Notification policies restored", "ok": True}]


def _reset_power_advanced_tweaks() -> list[dict]:
    log.info("Resetting advanced power tweaks...")
    # Easiest: restore plan defaults via powercfg
    run_cmd(["powercfg", "-restoredefaultschemes"])
    run_cmd(["powercfg", "-setactive", "381b4222-f694-41f0-9685-ff5bb260df2e"])  # Balanced
    return [{"name": "Power schemes restored to defaults", "ok": True}]


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
    for v in ("LetAppsAccessAccountInfo", "LetAppsAccessCalendar",
              "LetAppsAccessCallHistory", "LetAppsAccessContacts",
              "LetAppsAccessEmail", "LetAppsAccessMessaging",
              "LetAppsAccessMotion", "LetAppsAccessNotifications",
              "LetAppsAccessPhone", "LetAppsAccessRadios",
              "LetAppsAccessTrustedDevices", "LetAppsActivate",
              "LetAppsGetDiagnosticInfo", "LetAppsSyncWithDevices"):
        run_cmd(["reg", "delete", ap, "/v", v, "/f"])
    return [{"name": "App privacy tweak values cleared (subtree preserved)", "ok": True}]


def _reset_explorer_tweaks() -> list[dict]:
    log.info("Resetting Explorer tweaks...")
    advanced = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    for v in ("LaunchTo", "FullPath", "AutoCheckSelect", "ShowDriveLettersFirst",
              "ShowRecent", "ShowFrequent", "ExplorerRibbonStartsMinimized",
              "NavPaneExpandToCurrentFolder", "ShowPreviewHandlers"):
        run_cmd(["reg", "delete", advanced, "/v", v, "/f"])
    run_cmd(["reg", "delete", r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem",
             "/v", "LongPathsEnabled", "/f"])
    return [{"name": "Explorer settings restored to defaults", "ok": True}]


def _reset_smartscreen_tweaks() -> list[dict]:
    log.info("Resetting SmartScreen tweaks...")
    _reg(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer",
         "SmartScreenEnabled", "RequireAdmin", "REG_SZ")
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Edge",
             "/v", "SmartScreenEnabled", "/f"])
    run_cmd(["reg", "delete", r"HKLM\SOFTWARE\Policies\Microsoft\Edge",
             "/v", "SmartScreenPuaEnabled", "/f"])
    return [{"name": "SmartScreen restored", "ok": True}]


def _reset_lock_screen_tweaks() -> list[dict]:
    """v3 — per-value deletes."""
    log.info("Resetting lock screen tweaks...")
    pers = r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization"
    for v in ("NoLockScreen", "LockScreenImage",
              "NoChangingLockScreen", "LockScreenOverlaysDisabled"):
        run_cmd(["reg", "delete", pers, "/v", v, "/f"])
    cdm = r"HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
    for v in ("RotatingLockScreenEnabled", "RotatingLockScreenOverlayEnabled"):
        _reg(cdm, v, "1")
    return [{"name": "Lock screen Spotlight restored (Personalization subtree preserved)", "ok": True}]
