"""Hardware capability detection — single source of truth for what
GhostShell can and can't do on the machine it's running on.

Historically GhostShell assumed an NVIDIA discrete GPU everywhere:
GPU OC, Adaptive Tuning, the live-dash GPU tiles, and the driver
updater all read nvidia-smi / NVAPI and went blank or errored on
AMD / Intel / iGPU / CPU-only systems.  3.4.0 introduces this module
as the one place that answers:

  • Which GPU vendor + model is present?
  • What "tier" is it (discrete / modern-iGPU / weak-iGPU / VM / none)?
  • Can we overclock it?           (NVIDIA dGPU only, for now)
  • Can we read GPU telemetry?     (NVIDIA via nvidia-smi; AMD/Intel via LHM)
  • Can we hardware-encode clips?  (nvenc / qsv / amf, else CPU libx264)
  • Is this a laptop?              (battery present)
  • Is WebView2 (the modern UI engine) installed?

The UI calls /api/system/capabilities to hide NVIDIA-only pages on
non-NVIDIA machines, and app.py reads the GPU tier at boot to decide
how aggressively to enable WebView2 GPU rasterization.

Everything here is best-effort + cached — detection runs once per
process (hardware doesn't change mid-session) with a manual refresh
hook for the rare hot-plug eGPU case.
"""
from __future__ import annotations

import threading
from core.utils import get_logger

log = get_logger("capabilities")

_lock = threading.Lock()
_cache: dict | None = None


# ─── GPU tier classification ─────────────────────────────────────────
# Used by both the UI gating and the WebView2 GPU-rasterization policy.
#   "discrete"   — real dGPU (RTX/GTX, Radeon RX/Pro, Arc).  Force accel.
#   "igpu_modern"— Iris Xe, Radeon 680M/780M, UHD 7xx+.  Allow accel.
#   "igpu_weak"  — old UHD/HD Graphics, Vega 3/8.  Software render.
#   "vm"         — virtual display adapter.  Software render.
#   "none"       — no usable adapter detected.  Software render.

def _classify_gpu_tier(name: str, vendor: str) -> str:
    n = (name or "").lower()
    # Virtual machines / remote display first — these never accelerate well
    vm_markers = ("microsoft basic", "microsoft remote", "vmware",
                  "virtualbox", "hyper-v", "citrix", "parsec", "ddm",
                  "meta virtual", "aspeed", "matrox")
    if any(m in n for m in vm_markers):
        return "vm"
    # Discrete NVIDIA
    if any(m in n for m in ("geforce", "rtx", "gtx", "quadro", "tesla",
                            "titan", "nvidia rtx", "nvidia geforce")):
        return "discrete"
    # Discrete AMD — RX series, Radeon Pro, but NOT integrated "Radeon Graphics"
    if vendor == "AMD":
        if "radeon graphics" in n or "vega" in n and "rx vega" not in n:
            # bare "AMD Radeon(TM) Graphics" = APU integrated; "Vega 8/11" = APU
            return "igpu_modern" if ("680m" in n or "780m" in n or "760m" in n) else "igpu_weak"
        if any(m in n for m in ("radeon rx", "radeon pro", "rx vega",
                                "radeon vii", "instinct")):
            return "discrete"
        # Unknown AMD — assume modern iGPU rather than block
        return "igpu_modern"
    # Intel
    if vendor == "Intel":
        if "arc" in n:                       # Intel Arc dGPU
            return "discrete"
        if "iris xe" in n or "iris plus" in n:
            return "igpu_modern"
        if "uhd" in n:
            # UHD 7xx (Alder/Raptor Lake) modern; UHD 6xx and older weak
            if any(g in n for g in ("770", "750", "730", "710", "uhd graphics 7")):
                return "igpu_modern"
            return "igpu_weak"
        if "hd graphics" in n:               # old HD Graphics 4000-6000
            return "igpu_weak"
        return "igpu_weak"
    return "none"


def _detect_webview2() -> bool:
    """True if the Evergreen WebView2 runtime (or a fixed-version build)
    is installed.  Checks the registry keys Microsoft documents for
    runtime detection.  WebView2 ships with Win11 + most Win10-with-Edge
    but is absent on LTSC / stripped installs, where pywebview silently
    falls back to the ancient MSHTML engine and the modern UI breaks."""
    try:
        import winreg
    except Exception:
        return False
    # Evergreen runtime GUID (per Microsoft docs)
    GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + GUID),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + GUID),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + GUID),
    ]
    for root, path in candidates:
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                # "pv" is the installed version; "0.0.0.0" means not really there
                if pv and pv != "0.0.0.0":
                    return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False


def _detect_battery() -> bool:
    """Laptop heuristic — does a battery exist?  Used to label the
    machine as a laptop so we can tune messaging (e.g. don't push
    aggressive OC on a thermally-limited laptop dGPU)."""
    try:
        import ctypes
        from ctypes import wintypes

        class _SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus",        wintypes.BYTE),
                ("BatteryFlag",         wintypes.BYTE),
                ("BatteryLifePercent",  wintypes.BYTE),
                ("SystemStatusFlag",    wintypes.BYTE),
                ("BatteryLifeTime",     wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD),
            ]
        st = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            # BatteryFlag 128 = "no system battery"
            return st.BatteryFlag != 128
    except Exception:
        pass
    return False


def _detect_nvenc() -> bool:
    """Does the clipper's encoder probe find a hardware H.264 encoder?
    Mirrors clipper._pick_encoder so capabilities stays in sync with
    what capture will actually use.

    3.4.0 fix: this used to call a non-existent clipper._select_encoder
    (the real function is _pick_encoder and it needs the ffmpeg path).
    The hasattr() guard meant the bug failed silently — hw_encode was
    ALWAYS False even on NVENC-capable cards, so clips fell back to
    CPU libx264.  Now resolves ffmpeg via _find_ffmpeg and calls
    _pick_encoder correctly."""
    try:
        from core import clipper
        ff = clipper._find_ffmpeg()
        if not ff:
            return False     # no ffmpeg → no hw encode possible anyway
        enc = clipper._pick_encoder(ff)
        # hw encoders: h264_nvenc / hevc_nvenc / h264_amf / h264_qsv.
        # Only libx264 (CPU) means no hardware path.
        return bool(enc) and enc != "libx264"
    except Exception as e:
        log.debug(f"nvenc probe failed: {e}")
    return False


def _detect_nvidia_oc() -> bool:
    """NVIDIA OC capability via gpu_overclock's own probe."""
    try:
        from core import gpu_overclock
        cap = gpu_overclock.detect_gpu_oc_capability()
        return bool(cap.get("ok"))
    except Exception as e:
        log.debug(f"nvidia OC probe failed: {e}")
    return False


def detect(force: bool = False) -> dict:
    """Return the capability dict, computing it once and caching.
    Pass force=True to recompute (e.g. after an eGPU hot-plug)."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)

    caps: dict = {
        "gpu_vendor":       "Unknown",
        "gpu_name":         "Unknown",
        "gpu_tier":         "none",
        "all_gpus":         [],
        "has_nvidia":       False,
        "has_nvidia_oc":    False,
        "has_hw_encode":    False,
        "has_gpu_telemetry": False,
        "is_laptop":        False,
        "webview2_present": False,
    }

    # ── GPUs ──
    try:
        from core import system_info
        gpus = system_info._detect_gpus() or []
    except Exception as e:
        log.debug(f"GPU enumeration failed: {e}")
        gpus = []
    caps["all_gpus"] = gpus

    # Pick the "primary" GPU = the highest-tier one present.  A laptop
    # with Intel iGPU + NVIDIA dGPU should report as NVIDIA discrete so
    # the OC + telemetry features light up.
    tier_rank = {"discrete": 4, "igpu_modern": 3, "igpu_weak": 2,
                 "vm": 1, "none": 0}
    primary = None
    primary_rank = -1
    for g in gpus:
        tier = _classify_gpu_tier(g.get("name", ""), g.get("vendor", ""))
        g["tier"] = tier
        if tier_rank.get(tier, 0) > primary_rank:
            primary_rank = tier_rank.get(tier, 0)
            primary = g
    if primary:
        caps["gpu_vendor"] = primary.get("vendor", "Unknown")
        caps["gpu_name"]   = primary.get("name", "Unknown")
        caps["gpu_tier"]   = primary.get("tier", "none")
        caps["has_nvidia"] = (primary.get("vendor") == "NVIDIA")

    # ── Per-vendor capability probes ──
    caps["has_nvidia_oc"]     = caps["has_nvidia"] and _detect_nvidia_oc()
    caps["has_hw_encode"]     = _detect_nvenc()
    # Telemetry: NVIDIA always (nvidia-smi); AMD/Intel via LHM when its
    # WMI namespace is live (we auto-install LHM in 3.3.3).
    if caps["has_nvidia"]:
        caps["has_gpu_telemetry"] = True
    else:
        try:
            from core import dependencies
            caps["has_gpu_telemetry"] = bool(dependencies._lhm_wmi_namespace_present())
        except Exception:
            caps["has_gpu_telemetry"] = False

    caps["is_laptop"]        = _detect_battery()
    caps["webview2_present"] = _detect_webview2()

    log.info(
        f"Capabilities: gpu={caps['gpu_vendor']} ({caps['gpu_name']}) "
        f"tier={caps['gpu_tier']} oc={caps['has_nvidia_oc']} "
        f"hw_encode={caps['has_hw_encode']} telemetry={caps['has_gpu_telemetry']} "
        f"laptop={caps['is_laptop']} webview2={caps['webview2_present']}"
    )

    with _lock:
        _cache = dict(caps)
    return caps


def get() -> dict:
    """Cheap accessor — returns the cached caps, computing on first call."""
    return detect(force=False)


def webview2_gpu_args() -> str:
    """Return the WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS string tuned to
    the detected GPU tier.  Called by app.py BEFORE webview.start().

    Policy:
      discrete / igpu_modern : enable GPU rasterization + zero-copy.
                               Ignore the blocklist on discrete only
                               (modern iGPUs sometimes legitimately
                               blocklisted for driver bugs — respect it).
      igpu_weak / vm / none  : force software rendering.  Half-accelerated
                               states on weak/buggy iGPU drivers are
                               jankier than clean SwiftShader software.
    """
    tier = get().get("gpu_tier", "none")
    if tier == "discrete":
        return ("--enable-gpu-rasterization --enable-zero-copy "
                "--ignore-gpu-blocklist --enable-accelerated-2d-canvas")
    if tier == "igpu_modern":
        return ("--enable-gpu-rasterization --enable-zero-copy "
                "--enable-accelerated-2d-canvas")
    # weak iGPU / VM / none → clean software path
    return "--disable-gpu --disable-gpu-compositing"
