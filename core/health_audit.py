"""System Health Audit — fast, read-only checks that catch common
gaming-PC misconfigurations.

Each check returns:
    {
      'id':       short stable id (for UI keys + future fix-action wiring)
      'category': 'display' / 'memory' / 'gpu' / 'power' / 'storage' /
                  'network' / 'system' / 'vispora'
      'name':     human-readable label
      'severity': 'ok' / 'warn' / 'crit'
      'current':  what the system is reporting right now
      'expected': what we think it *should* be (None when there's no canonical value)
      'detail':   one-line explanation
      'fix':      optional dict {'kind': 'route', 'url': '/api/...'} if a
                  one-click fix exists.  UI renders a "Fix it" button.
    }

This module is intentionally cheap to call repeatedly — the dashboard
re-runs `audit()` every 30 s.  No registry writes, no shell commands
beyond the read-only WMI/PowerShell queries we already use elsewhere.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from core.utils import get_logger, run_ps

log = get_logger("health_audit")


# ─── Helper: WMI / PowerShell wrappers ───────────────────────────────────
def _ps_json(cmd: str, timeout: int = 6) -> Any:
    """Run a PS one-liner that ends in `ConvertTo-Json` and parse the result.
    Returns the parsed JSON, or None on any failure."""
    r = run_ps(cmd, timeout=timeout)
    if not r["ok"] or not r["out"]:
        return None
    try:
        return json.loads(r["out"])
    except json.JSONDecodeError:
        return None


# ═══════════════════════════════════════════════════════════════════════
# DISPLAY checks
# ═══════════════════════════════════════════════════════════════════════
def _check_refresh_rate() -> list[dict]:
    """Compare each monitor's current refresh rate to its max supported
    rate.  The classic 'I bought a 240 Hz monitor and it's running at
    60 Hz' miss."""
    data = _ps_json(
        "Get-CimInstance -Namespace 'root\\wmi' -ClassName 'WmiMonitorListedSupportedSourceModes' "
        "-ErrorAction SilentlyContinue | "
        "ForEach-Object { @{InstanceName=$_.InstanceName; "
        "MaxRate=($_.MonitorSourceModes | Measure-Object VerticalActiveTime -Maximum).Maximum} } | "
        "ConvertTo-Json"
    )
    # That WMI source returns vertical times not Hz directly — fall back to
    # the simpler CIM_VideoController / Win32_VideoController which exposes
    # current refresh rate, then read the max from each connected monitor.
    cur = _ps_json(
        "Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CurrentRefreshRate -gt 0 } | "
        "Select-Object Name, CurrentRefreshRate, MaxRefreshRate, VideoModeDescription | "
        "ConvertTo-Json"
    )
    if cur is None:
        return [{
            "id": "display.refresh", "category": "display",
            "name": "Refresh rate", "severity": "warn",
            "current": "unknown", "expected": None,
            "detail": "Could not read display refresh rate from WMI.",
        }]
    if isinstance(cur, dict):
        cur = [cur]
    out = []
    for m in cur:
        rate = m.get("CurrentRefreshRate")
        max_ = m.get("MaxRefreshRate") or 0
        name = (m.get("Name") or "Display").strip()
        if not rate:
            continue
        # Win32_VideoController.MaxRefreshRate is often unreliable — use
        # the EDID-listed max from WmiMonitorListedSupportedSourceModes
        # if available.  If unavailable, just report the current rate.
        if max_ and max_ > rate + 5:
            out.append({
                "id": f"display.refresh.{name}",
                "category": "display",
                "name": f"Refresh rate — {name}",
                "severity": "warn",
                "current": f"{rate} Hz",
                "expected": f"{max_} Hz",
                "detail": (f"Monitor reports max {max_} Hz but Windows is set to "
                           f"{rate} Hz.  Right-click desktop → Display settings → "
                           f"Advanced display → Choose a refresh rate."),
            })
        else:
            out.append({
                "id": f"display.refresh.{name}",
                "category": "display",
                "name": f"Refresh rate — {name}",
                "severity": "ok",
                "current": f"{rate} Hz",
                "expected": None,
                "detail": "Running at the highest rate Windows reports for this display.",
            })
    return out


def _check_hdr() -> list[dict]:
    """HDR availability + current state.  If the monitor supports HDR but
    Windows has it off, mention that — most games look better with HDR
    on if the panel actually supports it."""
    cmd = (
        "$reg = 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\GraphicsDrivers\\Configuration'; "
        "$on = $false; $supported = $false; "
        "Get-ChildItem -Path $reg -Recurse -ErrorAction SilentlyContinue | ForEach-Object { "
        "  $v = Get-ItemProperty -Path $_.PsPath -ErrorAction SilentlyContinue; "
        "  if ($v.AdvancedColorSupported) { $supported = $true } "
        "  if ($v.AdvancedColorEnabled)   { $on = $true } "
        "}; "
        "@{Supported=$supported; Enabled=$on} | ConvertTo-Json"
    )
    d = _ps_json(cmd)
    if not isinstance(d, dict):
        return []
    if not d.get("Supported"):
        return [{
            "id": "display.hdr", "category": "display",
            "name": "HDR", "severity": "ok",
            "current": "Not supported by display", "expected": None,
            "detail": "No connected display reports HDR / Advanced Color support.",
        }]
    if d.get("Enabled"):
        return [{
            "id": "display.hdr", "category": "display",
            "name": "HDR", "severity": "ok",
            "current": "On", "expected": None,
            "detail": "HDR is enabled in Windows.",
        }]
    return [{
        "id": "display.hdr", "category": "display",
        "name": "HDR", "severity": "warn",
        "current": "Off", "expected": "On (display supports it)",
        "detail": "Your display supports HDR but Windows has it off. "
                  "Settings → Display → Use HDR.",
    }]


# ═══════════════════════════════════════════════════════════════════════
# MEMORY checks
# ═══════════════════════════════════════════════════════════════════════
# The rated speed (MT/s) as RAM vendors spell it in part numbers:
#   Corsair CMK32GX5M2B6000C36, ADATA AX5U6000C3016G  -> "6000C"
#   G.Skill F5-6000J3636F16G, F4-3600C16D             -> "F5-6000", "F4-3600"
#   TeamGroup FF3D532G6000HC38A                        -> "6000HC"
#   Kingston KF560C36BBE-16, KF432C16                  -> DDR"5" "60" = 6000, DDR"4" "32" = 3200
#   Crucial / Micron CT16G56C46U5, BL16G36C16U4,
#     MTC8C1084S1UC56BD1                               -> "56C", "36C", "UC56" = 5600 / 3600
_RATED_PATTERNS = (
    (re.compile(r"F[45]-(\d{4})"), 1),
    (re.compile(r"(\d{4})(?:C|HC|CL|J)\d"), 1),
    (re.compile(r"\bK[FV][A-Z]?[45](\d{2})C"), 100),
    (re.compile(r"(?:\d{1,2}G|UC)(\d{2})(?:C|B|A)"), 100),
)
_DDR4, _DDR5 = 26, 34                                       # SMBIOSMemoryType
_JEDEC_TOP = {_DDR4: 3200, _DDR5: 5600}                    # the fastest standard (non-XMP) speed of each


def _rated_speed(part: str, memory_type: int = 0):
    """A kit's rated speed from its part number, or None if it is not one of
    the spellings above (or the number makes no sense for the memory type)."""
    part = (part or "").upper().replace(" ", "")
    lo, hi = {_DDR4: (1600, 5333), _DDR5: (4000, 10000)}.get(memory_type, (1600, 10000))
    for rx, scale in _RATED_PATTERNS:
        for m in rx.finditer(part):
            mts = int(m.group(1)) * scale
            if lo <= mts <= hi:
                return mts
    return None


def _check_ram_speed() -> list[dict]:
    """Is the RAM running at the speed the kit is sold for?  The classic loss
    is XMP / EXPO left off in the BIOS: a 6000 MT/s kit running at 4800.

    Windows only reports the speed the memory runs at (ConfiguredClockSpeed)
    and the module's standard base speed (Speed) — not its XMP rating — so
    the rating is read from the part number where the vendor's spelling is
    known.  (This used to call any DDR4 kit up to 4800 MT/s "running at base
    speed", and to report XMP as active when the base and running speeds were
    the same, which is what XMP being off looks like.)"""
    d = _ps_json(
        "Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | "
        "Select-Object Speed, ConfiguredClockSpeed, Capacity, Manufacturer, PartNumber, SMBIOSMemoryType | "
        "ConvertTo-Json"
    )
    if d is None:
        return []
    if isinstance(d, dict):
        d = [d]
    if not d:
        return []
    running = min((s.get("ConfiguredClockSpeed") or 0) for s in d)
    base = max((s.get("Speed") or 0) for s in d)
    if not running:
        return []
    mtype = int(d[0].get("SMBIOSMemoryType") or 0)
    kind = {_DDR4: "DDR4", _DDR5: "DDR5"}.get(mtype, "RAM")
    rated = [r for r in (_rated_speed(s.get("PartNumber"), mtype) for s in d) if r]
    rated_kit = min(rated) if rated else None           # a mixed set runs at the slowest kit's rating
    row = {"id": "memory.speed", "category": "memory", "name": "RAM speed", "current": f"{running} MT/s"}
    if rated_kit and running < rated_kit - 100:
        return [dict(row, severity="warn", expected=f"{rated_kit} MT/s (what the kit is rated for)",
                     detail=f"Your {kind} kit is rated {rated_kit} MT/s but runs at {running}.  Enable XMP / "
                            f"EXPO / DOCP in the BIOS — it is a free and often large gain in CPU-bound games.")]
    if base and running < base - 100:
        return [dict(row, severity="warn", expected=f"{base} MT/s (the modules' base speed)",
                     detail="The RAM runs below even its standard speed.  Check the memory settings in the BIOS.")]
    if rated_kit:
        return [dict(row, severity="ok", expected=None,
                     detail=f"Running at the kit's rated {rated_kit} MT/s.")]
    top = _JEDEC_TOP.get(mtype)
    if top and running <= top:
        return [dict(row, severity="warn", expected="Your kit's rated speed (on its label)",
                     detail=f"{running} MT/s is a standard {kind} speed, which is what XMP / EXPO being off looks "
                            f"like — but this kit's rating cannot be read here.  If its label says faster, enable "
                            f"XMP / EXPO in the BIOS.")]
    return [dict(row, severity="ok", expected=None,
                 detail=f"Running above the standard {kind} speeds, so XMP / EXPO is on.")]


# ═══════════════════════════════════════════════════════════════════════
# GPU checks
# ═══════════════════════════════════════════════════════════════════════
def _check_vbs() -> list[dict]:
    """VBS / HVCI on means a 5-25% gaming FPS hit."""
    d = _ps_json(
        "$d = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace 'root\\Microsoft\\Windows\\DeviceGuard' "
        "-ErrorAction SilentlyContinue; "
        "@{VBS=($d.VirtualizationBasedSecurityStatus -eq 2); "
        "  HVCI=($d.SecurityServicesRunning -contains 2)} | ConvertTo-Json"
    )
    if not isinstance(d, dict):
        return []
    on = bool(d.get("VBS")) or bool(d.get("HVCI"))
    if on:
        return [{
            "id": "gpu.vbs", "category": "gpu",
            "name": "VBS / Memory Integrity",
            "severity": "warn",
            "current": "On",
            "expected": "Off (for gaming)",
            "detail": "Microsoft themselves recommend disabling for gaming. "
                      "Use the Kernel tab → VBS card → Disable.",
            "fix": {"kind": "navigate", "page": "kernel"},
        }]
    return [{
        "id": "gpu.vbs", "category": "gpu",
        "name": "VBS / Memory Integrity", "severity": "ok",
        "current": "Off", "expected": None,
        "detail": "VBS + HVCI are off — gaming-friendly state.",
    }]


def _check_hw_gpu_scheduling() -> list[dict]:
    """Hardware-accelerated GPU scheduling (HwSchMode = 2) is what
    enables Resizable BAR / DLSS3 / etc. on modern setups."""
    cmd = (
        "(Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers' "
        "-Name HwSchMode -ErrorAction SilentlyContinue).HwSchMode"
    )
    r = run_ps(cmd)
    val = (r.get("out") or "").strip()
    if not val.isdigit():
        return [{
            "id": "gpu.hwsched", "category": "gpu",
            "name": "Hardware GPU Scheduling",
            "severity": "warn",
            "current": "Unknown", "expected": "2 (on)",
            "detail": "Could not read HwSchMode.",
        }]
    mode = int(val)
    return [{
        "id": "gpu.hwsched", "category": "gpu",
        "name": "Hardware GPU Scheduling",
        "severity": "ok" if mode == 2 else "warn",
        "current": "On" if mode == 2 else "Off",
        "expected": "On",
        "detail": "Required for DLSS3 / FrameGen / latest features."
                  if mode != 2 else "Enabled.",
        "fix": {"kind": "route", "method": "POST",
                "url": "/api/optimize/run",
                "body": {"categories": ["gaming"]}}
                if mode != 2 else None,
    }]


def _check_gpu_driver_age() -> list[dict]:
    """Warn if NVIDIA/AMD driver is more than 6 months old.  Old drivers
    miss new optimizations + game-day day-1 fixes."""
    d = _ps_json(
        "Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | "
        "Where-Object { $_.AdapterCompatibility -match 'NVIDIA|AMD|Intel' } | "
        "Select-Object Name, AdapterCompatibility, DriverVersion, DriverDate | "
        "ConvertTo-Json"
    )
    if d is None: return []
    if isinstance(d, dict): d = [d]
    out = []
    import datetime, re
    now = datetime.datetime.now()
    for v in d:
        dd = v.get("DriverDate")
        if not dd:
            continue
        # CIM datetime comes as e.g. "/Date(1234567890000)/" or a string
        if isinstance(dd, dict) and "DateTime" in dd:
            dd = dd["DateTime"]
        ts = None
        try:
            # Try parsing common formats
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(dd))
            if m:
                ts = datetime.datetime(int(m[1]), int(m[2]), int(m[3]))
            else:
                m2 = re.search(r"(\d+)", str(dd))
                if m2:
                    ts = datetime.datetime.fromtimestamp(int(m2[1]) / 1000)
        except Exception:
            pass
        if ts is None:
            continue
        age_days = (now - ts).days
        name = v.get("Name", "GPU")
        ver  = v.get("DriverVersion", "?")
        if age_days > 180:
            out.append({
                "id": f"gpu.driver.{name}", "category": "gpu",
                "name": f"{name} driver age",
                "severity": "warn",
                "current": f"{age_days} days old (v{ver})",
                "expected": "≤ 180 days",
                "detail": "Driver is old — newer versions usually have meaningful "
                          "perf + bug fixes.  Settings → GPU Driver → Check now.",
                "fix": {"kind": "navigate", "page": "settings"},
            })
        else:
            out.append({
                "id": f"gpu.driver.{name}", "category": "gpu",
                "name": f"{name} driver age",
                "severity": "ok",
                "current": f"{age_days} days old (v{ver})",
                "expected": None,
                "detail": "Driver is recent.",
            })
    return out


# ═══════════════════════════════════════════════════════════════════════
# POWER checks
# ═══════════════════════════════════════════════════════════════════════
def _check_power_plan() -> list[dict]:
    r = run_ps("(powercfg /getactivescheme)", timeout=4)
    out = (r.get("out") or "").strip()
    is_balanced = "Balanced" in out and "Ultimate" not in out
    is_power_saver = "Power saver" in out
    if is_power_saver:
        sev = "crit"; expected = "Ultimate Performance or High Performance"
        detail = "Power saver mode is on — your CPU is throttled. Switch via Optimize → Power Plan."
    elif is_balanced:
        sev = "warn"; expected = "Ultimate Performance"
        detail = "Balanced power plan limits CPU boost. Optimize tab → Power Plan applies the Ultimate plan."
    else:
        sev = "ok"; expected = None
        detail = out.split("(")[-1].rstrip(")").strip() if "(" in out else out
    return [{
        "id": "power.plan", "category": "power",
        "name": "Power Plan",
        "severity": sev,
        "current": out.split("(")[-1].rstrip(")").strip() if "(" in out else out,
        "expected": expected,
        "detail": detail,
        "fix": {"kind": "route", "method": "POST",
                "url": "/api/optimize/run",
                "body": {"categories": ["power"]}}
                if sev != "ok" else None,
    }]


# ═══════════════════════════════════════════════════════════════════════
# STORAGE checks
# ═══════════════════════════════════════════════════════════════════════
def _check_trim_enabled() -> list[dict]:
    r = run_ps("fsutil behavior query disabledeletenotify NTFS")
    out = (r.get("out") or "").strip()
    enabled = "= 0" in out
    return [{
        "id": "storage.trim", "category": "storage",
        "name": "TRIM (NTFS)",
        "severity": "ok" if enabled else "warn",
        "current": "Enabled" if enabled else "Disabled",
        "expected": "Enabled",
        "detail": "TRIM keeps SSDs fast over time."
                  if enabled else
                  "TRIM is off — SSD performance will degrade.  "
                  "Run Optimize → Storage to re-enable.",
        "fix": {"kind": "route", "method": "POST",
                "url": "/api/optimize/run",
                "body": {"categories": ["storage"]}}
                if not enabled else None,
    }]


# ═══════════════════════════════════════════════════════════════════════
# SYSTEM / VISPORA state
# ═══════════════════════════════════════════════════════════════════════
def _check_vispora_state() -> list[dict]:
    out = []
    # Game mode running?
    try:
        from core import game_profiles
        st = game_profiles.get_engine_status()
        on = bool(st.get("monitoring"))
        out.append({
            "id": "vispora.gamemode", "category": "vispora",
            "name": "Game Mode (auto-apply)",
            "severity": "ok" if on else "warn",
            "current": "On" if on else "Off",
            "expected": "On",
            "detail": "Watching for games — applies gaming profile automatically."
                      if on else
                      "Vispora isn't watching for game launches.  "
                      "Toggle from the Settings tab or the system tray.",
        })
    except Exception:
        pass

    # Temp ceiling watchdog?
    try:
        from core import temp_ceiling
        s = temp_ceiling.get_status()
        running = bool(s.get("running"))
        ceiling = s.get("settings", {}).get("ceiling_c", "?")
        out.append({
            "id": "vispora.temp_ceiling", "category": "vispora",
            "name": "GPU temp ceiling",
            "severity": "ok" if running else "warn",
            "current": f"{ceiling}°C ({'armed' if running else 'off'})",
            "expected": "armed",
            "detail": "Background watchdog reverts the OC if GPU exceeds the ceiling."
                      if running else
                      "The GPU temp watchdog is off.  Enable via the GPU tab → Hard Temp Ceiling.",
        })
    except Exception:
        pass

    # Adaptive Tuning?
    try:
        from core import adaptive_tuning
        s = adaptive_tuning.get_settings()
        global_on = bool(s.get("enabled_globally"))
        # Count enabled games
        states = adaptive_tuning.get_game_state()
        enabled_n = sum(1 for v in states.values() if v.get("enabled"))
        out.append({
            "id": "vispora.adaptive", "category": "vispora",
            "name": "Adaptive Tuning",
            "severity": "ok" if global_on else "warn",
            "current": (f"On — {enabled_n} game(s) opted in" if global_on
                        else "Off (master switch)"),
            "expected": None,
            "detail": "Tunes per-game OC offsets during real play."
                      if global_on else
                      "Master switch is off — Vispora isn't learning per-game OC profiles.",
        })
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════
def audit() -> dict:
    """Run every check and return a structured result.

    Returns:
      {
        'checks':  [check, ...],
        'counts':  {'ok': N, 'warn': N, 'crit': N},
        'overall': 'ok' / 'warn' / 'crit',
      }
    """
    log.debug("Running system health audit...")
    checks: list[dict] = []
    for fn in (_check_refresh_rate, _check_hdr,
               _check_ram_speed,
               _check_vbs, _check_hw_gpu_scheduling, _check_gpu_driver_age,
               _check_power_plan,
               _check_trim_enabled,
               _check_vispora_state):
        try:
            checks.extend(fn() or [])
        except Exception as e:
            log.warning(f"audit check {fn.__name__} failed: {e}")
    counts = {"ok": 0, "warn": 0, "crit": 0}
    for c in checks:
        sev = c.get("severity", "ok")
        if sev not in counts:
            sev = "ok"
        counts[sev] += 1
    overall = "crit" if counts["crit"] else ("warn" if counts["warn"] else "ok")
    return {"checks": checks, "counts": counts, "overall": overall}
