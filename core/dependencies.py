"""Optional-dependency orchestrator (v3.2-beta.7).

GhostShell relies on several third-party tools for features that require
either kernel-level work (driver installs) or system-level tracing (ETW).
Some of these can be auto-downloaded + auto-installed; others need the
user to grab them from the vendor's site.  This module unifies the
detection + install + restart logic in one place.

Dependencies tracked
────────────────────
  PresentMon 1.x     — frame timing for D3D9-12.  Bundled / auto-DL.
                        Already handled by core/frame_telemetry.py.
  PresentMon 2.x     — frame timing for D3D + OpenGL + Vulkan.
                        Auto-installed via GitHub MSI + msiexec /quiet.
                        Service install — may need restart on first run.
  RTSS (standalone)  — broadest frame coverage (DLL injection).
                        Guru3D doesn't expose stable download URLs, so
                        we open the browser to their page and detect
                        the install via the existing shared-memory probe.
  ViGEmBus           — virtual gamepad driver.  Required for the
                        anti-recoil / mapper to emit synthetic input.
                        Auto-installed from GitHub.  Driver install,
                        usually needs restart.
  HidHide            — hides physical controllers so games see only
                        the virtual one.  Auto-installed from GitHub.
                        Driver install, may need restart.

Boot-time behaviour
───────────────────
On Flask startup, `check_on_boot()` runs in a background thread.  If
the `auto_install_dependencies` setting is on (default True), missing
auto-installable deps get installed in sequence with progress reported
via `get_progress()`.  RTSS doesn't auto-install but is surfaced in
the result so the UI can show a clear "Click to install" button.

Restart handling
────────────────
Each install function returns `{ok, needs_restart, message}`.  After
the boot-time install pass, if any dep set `needs_restart=True`, we
write a `pending_restart` flag that the UI polls and renders a
"Restart now / later" banner with a 10-min countdown.

State persistence
─────────────────
  %APPDATA%/GhostShell/dependencies_state.json
    { last_check_at, installed_versions, pending_restart, opt_outs[] }
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Optional, Callable

from config import APPDATA_DIR
from core.utils import get_logger

log = get_logger("dependencies")

# ─── Paths + URLs ────────────────────────────────────────────────────
STATE_PATH      = os.path.join(APPDATA_DIR, "dependencies_state.json")
INSTALLER_DIR   = os.path.join(APPDATA_DIR, "installers")

# PresentMon 2.x — fairly stable URL (versioned, hosted on GitHub releases)
# beta.5: bumped 2.3.0 -> 2.4.1 (current latest; same MSI naming) and dropped
# the bogus "InspectionMonitor" name — the real service is registered as
# "Intel PresentMon Service" (display) / "PresentMonService" (short name).
PM2_VERSION     = "2.4.1"
PM2_MSI_URL     = f"https://github.com/GameTechDev/PresentMon/releases/download/v{PM2_VERSION}/PresentMon-v{PM2_VERSION}.msi"
PM2_SVC_NAMES   = ("Intel PresentMon Service", "PresentMonService")

# ViGEmBus — virtual gamepad driver
VIGEMBUS_VERSION = "1.22.0"
VIGEMBUS_URL     = f"https://github.com/ViGEm/ViGEmBus/releases/download/v{VIGEMBUS_VERSION}/ViGEmBus_{VIGEMBUS_VERSION}_x64_x86_arm64.exe"
VIGEMBUS_SVC     = "ViGEmBus"

# HidHide — hides physical controllers
HIDHIDE_VERSION  = "1.5.230"
HIDHIDE_URL      = f"https://github.com/ViGEm/HidHide/releases/download/v{HIDHIDE_VERSION}.0/HidHide_{HIDHIDE_VERSION}_x64.exe"
HIDHIDE_SVC      = "HidHide"

# RTSS — Guru3D's URLs aren't stable so we just direct users to the page
RTSS_PAGE_URL    = "https://www.guru3d.com/files-details/rtss-rivatuner-statistics-server-download.html"

# WebView2 runtime — the Chromium engine that renders GhostShell's UI.
# Ships with Win11 + most Win10-with-Edge, but absent on LTSC / stripped
# installs where pywebview silently falls back to the ancient MSHTML
# engine and the modern UI breaks + loses GPU acceleration.  winget
# package installs the Evergreen bootstrapper.
WEBVIEW2_PACKAGE_ID = "Microsoft.EdgeWebView2Runtime"


# LibreHardwareMonitor — required for CPU package temp on modern desktops.
# Windows has no native CPU temp API; the WMI namespace LHM exposes is what
# every monitoring tool (HWiNFO, GhostShell, Task Manager Beta, etc.) reads.
# Free + open-source; available via winget.  Unlike RTSS, LHM doesn't
# overlay anything in-game and doesn't fight Afterburner.
LHM_PACKAGE_ID   = "LibreHardwareMonitor.LibreHardwareMonitor"
LHM_INSTALL_PATHS = [
    r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitor.exe",
    r"C:\Program Files (x86)\LibreHardwareMonitor\LibreHardwareMonitor.exe",
    # winget's "scope user" install lands here
    os.path.join(os.environ.get("LOCALAPPDATA",
                                 os.path.expanduser(r"~\AppData\Local")),
                 "Microsoft", "WinGet", "Packages",
                 "LibreHardwareMonitor.LibreHardwareMonitor_Microsoft.Winget.Source_8wekyb3d8bbwe",
                 "LibreHardwareMonitor.exe"),
]

# Used by all subprocess calls to keep windows hidden
_CREATE_NO_WINDOW = 0x08000000


# ─── State persistence ───────────────────────────────────────────────

_state_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress: dict = {
    "running":       False,
    "current_dep":   None,
    "current_step":  None,
    "downloaded":    0,
    "total":         0,
    "completed":     [],
    "failed":        [],
    "needs_restart": False,
    "started_at":    0.0,
    "ended_at":      0.0,
}


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.debug(f"state save failed: {e}")


def _set_progress(**fields) -> None:
    with _progress_lock:
        _progress.update(fields)


def get_progress() -> dict:
    """Status of the most recent / in-flight install batch.  UI polls
    this to render the banner + progress bar."""
    with _progress_lock:
        p = dict(_progress)
    p["state"] = _load_state()
    return p


# ─── Detection helpers ───────────────────────────────────────────────

def _service_query(name: str) -> dict:
    """Returns {installed, running, name}.  Empty/falsy when not present."""
    try:
        r = subprocess.run(
            ["sc", "query", name],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=4,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "STATE" in out:
            running = "RUNNING" in out
            return {"installed": True, "running": running, "name": name}
    except Exception: pass
    return {"installed": False, "running": False, "name": name}


def _detect_pm2() -> dict:
    for n in PM2_SVC_NAMES:
        svc = _service_query(n)
        if svc.get("installed"):
            return svc
    return {"installed": False, "running": False}


def _detect_vigembus() -> dict:
    return _service_query(VIGEMBUS_SVC)


def _detect_hidhide() -> dict:
    return _service_query(HIDHIDE_SVC)


def _find_lhm_exe() -> Optional[str]:
    """Locate LibreHardwareMonitor.exe.  Looks at canonical install paths
    first; falls back to scanning any 'LibreHardwareMonitor' dir under
    Program Files in case the user picked a custom install path."""
    for p in LHM_INSTALL_PATHS:
        if p and os.path.isfile(p):
            return p
    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),
               os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidate = os.path.join(pf, "LibreHardwareMonitor", "LibreHardwareMonitor.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _lhm_wmi_namespace_present() -> bool:
    """True if the LHM WMI provider has been registered (i.e. LHM has
    been launched at least once after install).  The CPU temp reader
    in hw_monitor.py queries this namespace."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance -Namespace root/LibreHardwareMonitor "
             "-ClassName __NAMESPACE -ErrorAction SilentlyContinue "
             "| Select-Object -First 1 Name"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def _lhm_process_running() -> bool:
    """Cheap process-list check.  We use this to decide whether the
    boot-time auto-launch should fire."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LibreHardwareMonitor.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = (r.stdout or "").strip()
        return bool(out) and "LibreHardwareMonitor.exe" in out
    except Exception:
        return False


def _detect_lhm() -> dict:
    """Three-state probe: installed on disk, WMI namespace registered,
    process currently running.  GhostShell needs the WMI namespace to
    read CPU temp; the binary launches the provider on startup."""
    exe = _find_lhm_exe()
    return {
        "installed":       bool(exe),
        "exe_path":        exe,
        "wmi_registered":  _lhm_wmi_namespace_present(),
        "running":         _lhm_process_running(),
    }


def _ensure_lhm_settings_minimized(exe_path: str) -> None:
    """Write LHM's settings file BEFORE first launch so it starts hidden
    in the tray instead of popping a window.  LHM keeps its config next
    to the binary (LibreHardwareMonitor.config), so we write into the
    exe's parent dir.

    Format is .NET app.config XML — a single <appSettings> block with
    one <add key="..." value="..."/> per setting.  Keys we care about
    map to LHM's tray-behavior options."""
    if not exe_path:
        return False
    cfg_path = os.path.join(os.path.dirname(exe_path),
                             "LibreHardwareMonitor.config")
    # 3.5.2 — use LHM's REAL settings keys.  Verified against LHM source
    # (LibreHardwareMonitor.Windows.Forms/UI/MainForm.cs):
    #   new UserOption("startMinMenuItem", false, startMinMenuItem, _settings)
    #   new UserOption("minTrayMenuItem",  true,  minTrayMenuItem,  _settings)
    #   new UserOption("minCloseMenuItem", false, minCloseMenuItem, _settings)
    # i.e. the persisted key is the bare menu-item name — NO ".Checked"
    # suffix.  The old list seeded made-up names ("startupMinimized",
    # "closeMinimizes", …) that LHM simply ignores, so the main window
    # popped up on every launch even though our config write "succeeded".
    #   startMinMenuItem = Options → Start Minimized   (THE fix)
    #   minTrayMenuItem  = Options → Minimize To Tray
    #   minCloseMenuItem = Options → Minimize On Close
    tray_keys = {
        "startMinMenuItem":  "true",
        "minTrayMenuItem":   "true",
        "minCloseMenuItem":  "true",
    }
    seed_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<configuration>\n'
        '  <appSettings>\n'
        + "".join(f'    <add key="{k}" value="{v}"/>\n'
                  for k, v in tray_keys.items()) +
        '  </appSettings>\n'
        '</configuration>\n'
    )
    # Fresh install path — no existing config, drop in our seed.
    if not os.path.exists(cfg_path):
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(seed_xml)
            log.debug(f"seeded LHM startup config: {cfg_path}")
            return True
        except Exception as e:
            log.debug(f"LHM config seed to {cfg_path} failed: {e}")
        return False
    # Existing config (LHM has run before) — only INJECT the tray keys
    # if they're missing.  Don't disturb user-customized keys (sensor
    # plot visibility, window position, theme, etc.).
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            existing = f.read()
    except Exception as e:
        log.debug(f"LHM config read failed: {e}")
        return False
    # 3.5.2 — FORCE-correct existing values, don't just inject missing keys.
    # After LHM runs once it persists its own defaults (…Checked = false), so
    # "key already present" used to mean "leave the window-popping value in
    # place".  Now: rewrite a present-but-false value to true, and inject any
    # key that's absent.
    import re as _re
    changed = []
    for k, v in tray_keys.items():
        pat = _re.compile(
            r'(<add\s+key="' + _re.escape(k) + r'"\s+value=")([^"]*)(")')
        m = pat.search(existing)
        if m:
            if m.group(2).lower() != v:
                existing = pat.sub(lambda mm: mm.group(1) + v + mm.group(3),
                                   existing, count=1)
                changed.append(f"{k}={v} (was {m.group(2)})")
        else:
            if "</appSettings>" not in existing:
                log.debug("LHM config missing </appSettings>; not modifying")
                return False
            existing = existing.replace(
                "</appSettings>",
                f'    <add key="{k}" value="{v}" />\n  </appSettings>', 1)
            changed.append(f"{k}={v} (injected)")
    if not changed:
        return False     # already correct
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(existing)
        log.info(f"LHM tray config corrected: {', '.join(changed)}")
        return True
    except Exception as e:
        log.debug(f"LHM config write failed: {e}")
    return False


def start_lhm() -> dict:
    """Launch LHM if it's installed but not currently running.  Spawned
    detached + minimized so the user doesn't get a popped-up window —
    we just want the WMI provider live.  Polls for up to 8 seconds
    waiting for the namespace to appear before returning."""
    exe = _find_lhm_exe()
    if not exe:
        return {"ok": False, "message": "LibreHardwareMonitor not installed"}
    if _lhm_process_running():
        return {"ok": True, "already_running": True,
                "wmi_registered": _lhm_wmi_namespace_present()}
    _ensure_lhm_settings_minimized(exe)
    # Spawn detached so it outlives GhostShell.  Don't use CREATE_NO_WINDOW
    # — LHM is a GUI app, it needs to attach to the interactive desktop
    # for the tray icon.  DETACHED_PROCESS unhooks lifetime, which is
    # what we actually care about.
    try:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [exe],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            cwd=os.path.dirname(exe) or None,
        )
    except Exception as e:
        return {"ok": False, "message": f"could not spawn LHM: {e}"}
    # Wait for the WMI namespace to come up (typically ~2s)
    for _ in range(16):
        time.sleep(0.5)
        if _lhm_wmi_namespace_present():
            return {"ok": True, "running": True, "wmi_registered": True,
                    "message": "LHM launched, CPU temp will populate within a few seconds"}
    return {"ok": True, "running": _lhm_process_running(),
            "wmi_registered": False,
            "message": "LHM launched but WMI namespace not visible yet — "
                       "first launch can take 10-15s on slower disks"}


def _detect_webview2_runtime() -> dict:
    """Reuse capabilities._detect_webview2 (registry check for the
    Evergreen runtime).  Returns {installed}."""
    try:
        from core import capabilities
        return {"installed": bool(capabilities._detect_webview2())}
    except Exception:
        return {"installed": False}


def install_webview2() -> dict:
    """Install the Evergreen WebView2 runtime via winget.  Makes the
    modern GPU-accelerated UI engine available on LTSC / stripped
    Windows where it's missing.  No launch step — the runtime is a
    system component pywebview links against, not an app."""
    if _detect_webview2_runtime().get("installed"):
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "WebView2 runtime already present"}
    winget = _winget_path()
    if not winget:
        return {"ok": False, "needs_restart": False,
                "message": "winget not available — install WebView2 manually "
                           "from https://developer.microsoft.com/microsoft-edge/webview2/"}
    _set_progress(current_step="Installing WebView2 runtime via winget…")
    install_cmd = [
        winget, "install", "--exact",
        "--id", WEBVIEW2_PACKAGE_ID,
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]
    log_path = ""
    try:
        r = subprocess.run(
            install_cmd, capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW, timeout=900,
        )
        log_path = _save_install_log("webview2-winget", install_cmd, r.returncode,
                                      r.stdout, r.stderr)
        if r.returncode == 0 or r.returncode == -1978335189:
            time.sleep(1)
            installed = _detect_webview2_runtime().get("installed")
            return {"ok": True,
                    # New runtime needs a GhostShell restart to bind to it.
                    "needs_restart": not installed or True,
                    "log_path": log_path,
                    "message": "WebView2 runtime installed — restart GhostShell "
                               "to render with the modern engine"}
        return {"ok": False, "needs_restart": False, "log_path": log_path,
                "message": f"winget install failed (rc={r.returncode}): "
                           f"{((r.stderr or '') or (r.stdout or ''))[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "needs_restart": False, "log_path": log_path,
                "message": "WebView2 install timed out"}
    except Exception as e:
        return {"ok": False, "needs_restart": False, "log_path": log_path,
                "message": f"WebView2 install raised: {e}"}


def install_lhm() -> dict:
    """Install LibreHardwareMonitor via winget, write the start-minimized
    settings file, and launch it so its WMI provider becomes available.

    Modern desktops have no Windows-native CPU temperature API; this is
    what makes the CPU TEMP tile on the live dashboard actually populate.
    """
    if _detect_lhm().get("installed"):
        # Already installed — just make sure it's running so WMI is live.
        if not _lhm_wmi_namespace_present():
            return start_lhm()
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "LibreHardwareMonitor already installed and active"}

    winget = _winget_path()
    if not winget:
        return {"ok": False, "needs_restart": False,
                "message": "winget not available — install LibreHardwareMonitor "
                           "manually from https://github.com/LibreHardwareMonitor/LibreHardwareMonitor"}

    _set_progress(current_step="Installing LibreHardwareMonitor via winget…")
    install_cmd = [
        winget, "install",
        "--exact",
        "--id", LHM_PACKAGE_ID,
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]
    log_path = ""
    try:
        r = subprocess.run(
            install_cmd,
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=600,
        )
        log_path = _save_install_log("lhm-winget", install_cmd, r.returncode,
                                      r.stdout, r.stderr)
        # 0 = success, -1978335189 (0x8A15002B) = "already installed"
        if r.returncode == 0 or r.returncode == -1978335189:
            _set_progress(current_step="Starting LibreHardwareMonitor…")
            time.sleep(1.5)
            launch = start_lhm()
            return {"ok": True, "needs_restart": False,
                    "log_path": log_path,
                    "running": launch.get("running", False),
                    "wmi_registered": launch.get("wmi_registered", False),
                    "message": launch.get("message")
                               or "LibreHardwareMonitor installed and started"}
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": f"winget install failed (rc={r.returncode}): "
                           f"{((r.stderr or '') or (r.stdout or ''))[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": "LHM install timed out after 10 minutes"}
    except Exception as e:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": f"LHM install raised: {e}"}


RTSS_INSTALL_PATHS = [
    r"C:\Program Files (x86)\RivaTuner Statistics Server\RTSS.exe",
    r"C:\Program Files\RivaTuner Statistics Server\RTSS.exe",
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "RivaTuner Statistics Server", "RTSS.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "RivaTuner Statistics Server", "RTSS.exe"),
]


def _find_rtss_exe() -> Optional[str]:
    """Locate RTSS.exe on disk if it's installed.  Different from the
    shared-memory check — the OSD service may be installed but not
    running yet (typical right after a winget install)."""
    for p in RTSS_INSTALL_PATHS:
        if p and os.path.isfile(p):
            return p
    return None


def _detect_rtss() -> dict:
    """Returns {installed, running, exe_path}.

    v3.2-beta.9 — separates 'installed' (file on disk) from 'running'
    (shared memory present).  Beta.8 conflated them, so a successful
    winget install showed as 'not installed' until the user manually
    launched RTSS.  Now the boot check can detect 'installed but not
    running' and auto-launch it."""
    exe_path = _find_rtss_exe()
    running = False
    try:
        from core import frame_telemetry
        running = frame_telemetry._rtss_mapping_exists()
    except Exception:
        pass
    return {
        "installed": bool(exe_path) or running,
        "running":   running,
        "exe_path":  exe_path,
    }


def start_rtss() -> dict:
    """Launch RTSS if installed but not running.  Returns whether the
    shared memory mapping appears within ~6 s — the canonical sign that
    RTSS has fully started and is ready to be read."""
    exe_path = _find_rtss_exe()
    if not exe_path:
        return {"ok": False, "message": "RTSS not installed"}

    # Check if it's already running before we start a second instance
    try:
        from core import frame_telemetry
        if frame_telemetry._rtss_mapping_exists():
            return {"ok": True, "already_running": True}
    except Exception: pass

    # Spawn detached so it survives GhostShell exiting.  CREATE_NEW_CONSOLE
    # is intentionally NOT set — RTSS attaches to the user's interactive
    # desktop session (TaskTray + OSD).  Without DETACHED_PROCESS Windows
    # would tie its lifetime to our subprocess.
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            [exe_path],
            shell=False,
            cwd=os.path.dirname(exe_path),
            creationflags=(DETACHED_PROCESS | _CREATE_NO_WINDOW),
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "message": f"Popen failed: {e}"}

    # Wait up to 6 s for the shared memory mapping to come up
    try:
        from core import frame_telemetry
        for _ in range(12):
            time.sleep(0.5)
            if frame_telemetry._rtss_mapping_exists():
                return {"ok": True, "running": True}
    except Exception: pass
    return {"ok": True, "running": False,
            "message": "RTSS launched but shared memory not yet visible "
                       "(might need user interactive session — restart Windows or login)"}


# ─── Download helper ─────────────────────────────────────────────────

def _download(url: str, dest: str, progress_cb: Optional[Callable] = None) -> bool:
    """Stream a URL to a local file.  Returns True on success.  Sets
    _progress.downloaded / .total when progress_cb is the default."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "GhostShell-Dependencies/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0) or 0)
            _set_progress(downloaded=0, total=total)
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    _set_progress(downloaded=downloaded)
                    if progress_cb:
                        try: progress_cb(downloaded, total)
                        except Exception: pass
        sz = os.path.getsize(tmp)
        if sz < 50 * 1024:   # smaller than 50 KB = probably a 404 HTML page
            os.remove(tmp)
            log.warning(f"download too small ({sz} B) — likely 404: {url}")
            return False
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log.warning(f"download failed: {url} — {e}")
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except Exception: pass
        return False


# ─── Pending-restart detection ───────────────────────────────────────

def _windows_pending_reboot() -> bool:
    """Check Windows' canonical pending-reboot registry keys.  Returns
    True if a reboot was requested by ANY installer since last boot —
    useful for the post-install prompt."""
    try:
        import winreg
        keys_to_check = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations"),
        ]
        for root, sub in keys_to_check:
            try:
                k = winreg.OpenKey(root, sub)
                winreg.CloseKey(k)
                return True
            except FileNotFoundError: continue
            except Exception: continue
    except Exception: pass
    return False


# ─── Install functions (one per dep) ─────────────────────────────────

def install_presentmon2() -> dict:
    """Auto-install PresentMon 2.x via MSI.  Quiet install, no reboot.
    Returns {ok, needs_restart, message}."""
    svc = _detect_pm2()
    if svc.get("installed"):
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "PresentMon 2.x service already installed"}

    _set_progress(current_step=f"Downloading PresentMon 2.x ({PM2_VERSION})…")
    msi_path = os.path.join(INSTALLER_DIR, f"PresentMon-v{PM2_VERSION}.msi")
    if not _download(PM2_MSI_URL, msi_path):
        return {"ok": False, "needs_restart": False,
                "message": "Download failed — check internet connection"}

    _set_progress(current_step="Installing PresentMon 2.x service…")
    try:
        r = subprocess.run(
            ["msiexec", "/i", msi_path, "/quiet", "/norestart",
             "ALLUSERS=1"],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=300,
        )
        if r.returncode in (0, 3010):  # 3010 = success but needs restart
            needs_restart = (r.returncode == 3010) or _windows_pending_reboot()
            # Clean up the installer to save 122 MB of disk
            try: os.remove(msi_path)
            except Exception: pass
            return {"ok": True, "needs_restart": needs_restart,
                    "message": f"PresentMon 2.x installed (rc={r.returncode})"}
        return {"ok": False, "needs_restart": False,
                "message": f"msiexec failed (rc={r.returncode}): {r.stderr[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "needs_restart": False,
                "message": "msiexec hung (>5 min)"}
    except Exception as e:
        return {"ok": False, "needs_restart": False,
                "message": f"install error: {e}"}


def install_vigembus() -> dict:
    """Auto-install ViGEmBus virtual gamepad driver."""
    svc = _detect_vigembus()
    if svc.get("installed"):
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "ViGEmBus already installed"}

    _set_progress(current_step=f"Downloading ViGEmBus ({VIGEMBUS_VERSION})…")
    exe_path = os.path.join(INSTALLER_DIR, f"ViGEmBus_{VIGEMBUS_VERSION}.exe")
    if not _download(VIGEMBUS_URL, exe_path):
        return {"ok": False, "needs_restart": False,
                "message": "Download failed"}

    _set_progress(current_step="Installing ViGEmBus driver (signed) …")
    try:
        # ViGEmBus installer is a Wix bundle wrapping an MSI; silent
        # install flags from Burn are /quiet /norestart
        r = subprocess.run(
            [exe_path, "/quiet", "/norestart"],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=300,
        )
        needs_restart = (r.returncode == 3010) or _windows_pending_reboot()
        try: os.remove(exe_path)
        except Exception: pass
        if r.returncode in (0, 3010):
            return {"ok": True, "needs_restart": needs_restart,
                    "message": f"ViGEmBus installed (rc={r.returncode})"}
        return {"ok": False, "needs_restart": False,
                "message": f"installer failed (rc={r.returncode})"}
    except Exception as e:
        return {"ok": False, "needs_restart": False, "message": str(e)}


INSTALL_LOG_DIR = os.path.join(APPDATA_DIR, "install_logs")


def _save_install_log(dep_key: str, command: list, rc, stdout: str, stderr: str) -> str:
    """Persist install command output so the user can read why something
    failed.  Surfaces in the UI as a 'View log' link."""
    try:
        os.makedirs(INSTALL_LOG_DIR, exist_ok=True)
        path = os.path.join(INSTALL_LOG_DIR,
                             f"{dep_key}_{int(time.time())}.log")
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"=== command ===\n{' '.join(command)}\n\n")
            f.write(f"=== rc: {rc} ===\n\n")
            f.write(f"--- stdout ---\n{stdout or '(empty)'}\n\n")
            f.write(f"--- stderr ---\n{stderr or '(empty)'}\n")
        return path
    except Exception:
        return ""


def _winget_path() -> Optional[str]:
    """Resolve a working winget binary.  PATH lookup works on most
    interactive sessions, but when GhostShell runs elevated under a
    different token the user-scoped WindowsApps path can be missing.
    Falls back to the known absolute paths."""
    # 1. PATH lookup
    try:
        r = subprocess.run(
            ["winget", "--version"],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=3,
        )
        if r.returncode == 0 and "v" in (r.stdout or "").lower():
            return "winget"
    except Exception: pass
    # 2. LocalAppData WindowsApps (user-scoped)
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        cand = os.path.join(appdata, "Microsoft", "WindowsApps", "winget.exe")
        if os.path.isfile(cand):
            return cand
    # 3. System32 (rare but exists on some installs)
    cand = r"C:\Windows\System32\winget.exe"
    if os.path.isfile(cand):
        return cand
    return None


# ─── FFmpeg (clipper + screenshotter) ────────────────────────────────
FFMPEG_PACKAGE_ID = "Gyan.FFmpeg.Essentials"   # ~80 MB essentials build

def _detect_ffmpeg() -> dict:
    """Returns {installed, path, version}.  Looks at PATH first
    (typical winget install adds itself), then standard install dirs."""
    # 1. Try PATH lookup
    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=4,
        )
        if r.returncode == 0 and "ffmpeg version" in (r.stdout or "").lower():
            # Extract version string
            first = (r.stdout.splitlines() or [""])[0]
            return {"installed": True, "path": "ffmpeg",
                    "version_line": first[:80]}
    except Exception: pass
    # 2. Known winget install paths
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        os.path.join(pf, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(pf, "Gyan", "FFmpeg", "bin", "ffmpeg.exe"),
        # 3.4.2 — was a hardcoded developer home path (C:\Users\Ginld\...)
        # that shipped in the binary.  Use the current user's WinGet
        # packages dir instead.  (The walk below is the real lookup;
        # this entry is a cheap fast-path that candidates[:2] doesn't
        # even reach — kept env-correct for safety.)
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Microsoft", "WinGet", "Packages"),
    ]
    # Also scan WinGet's user packages dir for any ffmpeg under it
    local_pkgs = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Microsoft", "WinGet", "Packages")
    if os.path.isdir(local_pkgs):
        try:
            for pkg in os.listdir(local_pkgs):
                if "FFmpeg" in pkg or "ffmpeg" in pkg:
                    for root, _, files in os.walk(os.path.join(local_pkgs, pkg)):
                        if "ffmpeg.exe" in files:
                            return {"installed": True,
                                    "path": os.path.join(root, "ffmpeg.exe"),
                                    "version_line": ""}
        except Exception: pass
    for c in candidates[:2]:
        if os.path.isfile(c):
            return {"installed": True, "path": c, "version_line": ""}
    return {"installed": False, "path": "", "version_line": ""}


def install_ffmpeg() -> dict:
    """Install FFmpeg via winget (Gyan.FFmpeg.Essentials).  Used by the
    clipper for screen capture + encoding, and as a fallback by the
    screenshotter for video frame grabs.

    The essentials build (~80 MB) is the smallest official FFmpeg with
    h264/hevc encoders and modern format support — full build is 200+ MB
    which is overkill for our screen-capture-and-encode use case."""
    if _detect_ffmpeg().get("installed"):
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "FFmpeg already installed"}

    winget = _winget_path()
    if not winget:
        return {"ok": False, "needs_restart": False,
                "message": "winget not available — install FFmpeg manually "
                           "from https://www.gyan.dev/ffmpeg/builds/"}

    _set_progress(current_step="Installing FFmpeg via winget…")
    install_cmd = [
        winget, "install",
        "--exact",
        "--id", FFMPEG_PACKAGE_ID,
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]
    log_path = ""
    try:
        r = subprocess.run(
            install_cmd,
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=900,    # MSI can take a while; FFmpeg is ~80 MB
        )
        log_path = _save_install_log("ffmpeg-winget", install_cmd, r.returncode,
                                      r.stdout, r.stderr)
        if r.returncode == 0 or r.returncode == -1978335189:  # already installed
            # winget links to PATH but the running process won't pick it up
            # without a new shell; verify via file lookup as the source of truth.
            time.sleep(1)
            if _detect_ffmpeg().get("installed"):
                return {"ok": True, "needs_restart": False,
                        "log_path": log_path,
                        "message": "FFmpeg installed"}
            return {"ok": True, "needs_restart": False,
                    "log_path": log_path,
                    "message": "FFmpeg installed (PATH refresh may need a "
                               "new shell — restart GhostShell after this "
                               "install batch)"}
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": f"winget install failed (rc={r.returncode}): "
                           f"{((r.stderr or '') or (r.stdout or ''))[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": "winget install timed out (>15 min)"}
    except Exception as e:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": str(e)}


def install_rtss() -> dict:
    """Auto-install RTSS (RivaTuner Statistics Server) via winget, then
    launch it.  v3.2-beta.9 adds:
      • robust winget binary lookup (PATH + LocalAppData fallback)
      • full stdout/stderr capture to %APPDATA%/GhostShell/install_logs/
      • post-install launch of RTSS.exe (autostart doesn't fire in
        non-interactive admin contexts — we trigger it ourselves)
      • returns {installed, running, log_path} so the UI can show the
        right state and offer 'Start RTSS' if needed."""
    # If RTSS is already running, we're done.  If it's only installed
    # (file on disk) but not running, still try to start it before
    # falling through to a re-install.
    state = _detect_rtss()
    if state.get("running"):
        return {"ok": True, "already_installed": True, "running": True,
                "needs_restart": False,
                "message": "RTSS already running"}
    if state.get("installed") and not state.get("running"):
        # Don't re-install — just launch
        launch = start_rtss()
        return {"ok": True, "already_installed": True,
                "running": launch.get("running", False),
                "needs_restart": False,
                "message": launch.get("message")
                            or ("RTSS launched"
                                if launch.get("running")
                                else "RTSS installed; launch attempted but shared memory not yet visible")}

    # Not installed.  Use winget.
    winget = _winget_path()
    if not winget:
        return {"ok": False, "needs_restart": False,
                "message": "winget not available on this system. Install "
                           "the App Installer from Microsoft Store, or use "
                           "the Guru3D download link below."}

    _set_progress(current_step="Installing RTSS via winget (Guru3D.RTSS)…")
    install_cmd = [
        winget, "install",
        "--exact",
        "--id", "Guru3D.RTSS",
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
        "--scope", "machine",
    ]
    log_path = ""
    try:
        r = subprocess.run(
            install_cmd,
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=600,
        )
        log_path = _save_install_log("rtss-winget", install_cmd, r.returncode,
                                      r.stdout, r.stderr)

        # winget exit codes (from winget's Common/AppInstallerSharedLib):
        #   0           = success
        #   -1978335189 = APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED
        #   -1978335212 = APPINSTALLER_CLI_ERROR_NO_APPLICABLE_INSTALLER
        if r.returncode == -1978335212:
            # Retry without machine scope
            install_cmd_user = [
                winget, "install",
                "--exact",
                "--id", "Guru3D.RTSS",
                "--silent",
                "--accept-source-agreements",
                "--accept-package-agreements",
                "--disable-interactivity",
            ]
            r = subprocess.run(
                install_cmd_user,
                capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW,
                timeout=600,
            )
            log_path = _save_install_log("rtss-winget-user", install_cmd_user,
                                          r.returncode, r.stdout, r.stderr)

        if r.returncode == 0 or r.returncode == -1978335189:
            # Installed.  RTSS's autostart entry only fires at next user
            # login (HKLM\Run or HKCU\Run depending on scope).  We've
            # got the user logged in NOW, so just launch it ourselves —
            # which also confirms the install actually worked.
            _set_progress(current_step="Starting RTSS…")
            # Give the filesystem a beat to settle after install
            time.sleep(1.5)
            launch = start_rtss()
            running = launch.get("running", False)
            if running:
                return {"ok": True, "needs_restart": False,
                        "running": True,
                        "log_path": log_path,
                        "message": "RTSS installed and started"}
            return {"ok": True, "needs_restart": False,
                    "running": False,
                    "log_path": log_path,
                    "message": "RTSS installed but the OSD service didn't come up. "
                               "Click 'Start RTSS' in the diagnostic panel, or "
                               "restart Windows to trigger its autostart."}
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": f"winget install failed (rc={r.returncode}): "
                           f"{((r.stderr or '') or (r.stdout or ''))[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": "winget install timed out (>10 min)"}
    except Exception as e:
        return {"ok": False, "needs_restart": False,
                "log_path": log_path,
                "message": str(e)}


def install_hidhide() -> dict:
    """Auto-install HidHide controller-hiding driver."""
    svc = _detect_hidhide()
    if svc.get("installed"):
        return {"ok": True, "already_installed": True,
                "needs_restart": False,
                "message": "HidHide already installed"}

    _set_progress(current_step=f"Downloading HidHide ({HIDHIDE_VERSION})…")
    exe_path = os.path.join(INSTALLER_DIR, f"HidHide_{HIDHIDE_VERSION}.exe")
    if not _download(HIDHIDE_URL, exe_path):
        return {"ok": False, "needs_restart": False,
                "message": "Download failed"}

    _set_progress(current_step="Installing HidHide driver…")
    try:
        r = subprocess.run(
            [exe_path, "/quiet", "/norestart"],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=300,
        )
        needs_restart = (r.returncode == 3010) or _windows_pending_reboot()
        try: os.remove(exe_path)
        except Exception: pass
        if r.returncode in (0, 3010):
            return {"ok": True, "needs_restart": needs_restart,
                    "message": f"HidHide installed (rc={r.returncode})"}
        return {"ok": False, "needs_restart": False,
                "message": f"installer failed (rc={r.returncode})"}
    except Exception as e:
        return {"ok": False, "needs_restart": False, "message": str(e)}


# ─── Dependency manifest ─────────────────────────────────────────────
# Each entry: {key, label, description, detect, install, opt_outable, browser_only}
# - opt_outable: shown in settings; user can disable for that dep
# - browser_only: cannot auto-install; opens vendor page in browser

DEPENDENCIES = [
    {
        "key":          "presentmon1",
        "label":        "PresentMon 1.x (frame timing — D3D9-12)",
        "description":  "Microsoft's frame-timing tool. Used by Adaptive Tuning to read FPS, 1%-low, and frametime σ for D3D9-12 games.",
        "auto_install": True,
        "detect":       lambda: bool(_detect_pm1_path()),
        "install":      lambda: _install_pm1_wrapper(),
        "size":         "380 KB",
    },
    {
        "key":          "presentmon2",
        "label":        "PresentMon 2.x (frame timing — adds OpenGL + Vulkan)",
        "description":  "Microsoft's newer frame-timing service. Captures the full ETW provider set — D3D, OpenGL, and Vulkan. Lets AT see frame data for Minecraft Java and other non-D3D games.",
        "auto_install": True,
        "detect":       lambda: _detect_pm2().get("installed"),
        "install":      install_presentmon2,
        "size":         "122 MB MSI",
    },
    {
        "key":          "vigembus",
        "label":        "ViGEmBus (virtual gamepad driver)",
        "description":  "Required for the anti-recoil / gamepad mapper to emit synthetic Xbox controller input. Signed driver from the ViGEm project.",
        "auto_install": True,
        "detect":       lambda: _detect_vigembus().get("installed"),
        "install":      install_vigembus,
        "size":         "6 MB",
    },
    {
        "key":          "hidhide",
        "label":        "HidHide (hide physical controllers from games)",
        "description":  "Optional helper for the gamepad mapper — hides your physical controller so the game only sees GhostShell's virtual one, avoiding double-input. Signed driver.",
        "auto_install": True,
        "detect":       lambda: _detect_hidhide().get("installed"),
        "install":      install_hidhide,
        "size":         "8 MB",
    },
    {
        "key":          "ffmpeg",
        "label":        "FFmpeg (clipping + screenshot encoding)",
        "description":  "Used by GhostShell's Capture feature to record gameplay clips and encode them efficiently (NVENC if you have an NVIDIA GPU). Installed silently via winget (Gyan.FFmpeg.Essentials package). Without it, the clipper is unavailable; screenshots still work via mss.",
        "auto_install": True,
        "browser_only": False,
        "detect":       lambda: _detect_ffmpeg().get("installed"),
        "install":      install_ffmpeg,
        "browser_url":  "https://www.gyan.dev/ffmpeg/builds/",
        "size":         "~80 MB via winget",
    },
    {
        "key":          "webview2",
        "label":        "WebView2 Runtime (modern UI engine)",
        "description":  "The Chromium engine that renders GhostShell's interface with GPU acceleration. Ships with Windows 11 and most Windows 10 installs, but is missing on LTSC / stripped builds where the UI would otherwise fall back to the ancient MSHTML engine (no GPU compositing, broken layout). Installed silently via winget (Microsoft.EdgeWebView2Runtime). Requires a GhostShell restart to take effect.",
        "auto_install": True,
        "browser_only": False,
        "detect":       lambda: _detect_webview2_runtime().get("installed"),
        "install":      install_webview2,
        "browser_url":  "https://developer.microsoft.com/microsoft-edge/webview2/",
        "size":         "~150 MB Evergreen bootstrapper",
    },
    {
        "key":          "lhm",
        "label":        "LibreHardwareMonitor (CPU temperature)",
        "description":  "Windows has no native CPU temperature API. LHM is the free + open-source library every monitoring tool (HWiNFO, this app, Task Manager Beta, etc.) uses to read CPU package temp via WMI. Without it, the CPU TEMP tile on the live dashboard cannot populate. Installed silently via winget, launched minimized to the system tray with WMI provider enabled. Doesn't overlay anything in-game; doesn't fight Afterburner.",
        "auto_install": True,
        "browser_only": False,
        "detect":       lambda: _detect_lhm().get("installed"),
        "install":      install_lhm,
        "browser_url":  "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor",
        "size":         "~3 MB via winget",
    },
    # beta.15 — RTSS is no longer a managed dependency.  GhostShell hasn't
    # used RTSS for frame telemetry since beta.5 (replaced by PresentMon
    # 1.x + 2.x), and any auto-install / auto-launch behaviour ended up
    # fighting Afterburner's OC offsets on user systems where both were
    # installed.  We still keep `_detect_rtss()` for diagnostics / the
    # OC-tool conflict warning, but no UI button asks the user to install
    # or start it, and no boot-time code launches it.
]


def _detect_pm1_path():
    """Wrap frame_telemetry's path lookup so the manifest can reference it."""
    try:
        from core import frame_telemetry
        return frame_telemetry._find_presentmon_path()
    except Exception:
        return None


def _install_pm1_wrapper():
    """Drive the existing PresentMon 1.x async install + wait for it."""
    try:
        from core import frame_telemetry
        r = frame_telemetry.install_presentmon()
        if r.get("already_installed"):
            return {"ok": True, "already_installed": True,
                    "needs_restart": False, "message": "PresentMon 1.x already installed"}
        # Poll until done or timeout
        for _ in range(60):
            time.sleep(1)
            p = frame_telemetry.install_progress()
            if p["state"] == "installed":
                return {"ok": True, "needs_restart": False,
                        "message": "PresentMon 1.x installed"}
            if p["state"] == "error":
                return {"ok": False, "needs_restart": False,
                        "message": p.get("err") or "install failed"}
        return {"ok": False, "needs_restart": False,
                "message": "install timed out"}
    except Exception as e:
        return {"ok": False, "needs_restart": False, "message": str(e)}


# ─── Public API ──────────────────────────────────────────────────────

def check_all() -> dict:
    """Return status of every tracked dependency.  Fast — only does
    sc query calls + file existence checks, no network.

    v3.2-beta.9 — also returns per-dep `running` status when meaningful
    (currently only RTSS distinguishes installed vs running)."""
    out = []
    for dep in DEPENDENCIES:
        try:
            present = bool(dep["detect"]())
        except Exception as e:
            present = False
            log.debug(f"detect({dep['key']}) failed: {e}")
        running = None
        # Pull running state for deps where it's distinct from installed
        if dep["key"] == "rtss":
            try:
                rtss = _detect_rtss()
                running = bool(rtss.get("running"))
                # If detected installed but not running, treat the row
                # specially so the UI can show "Start RTSS" instead of
                # "Install".
            except Exception: pass
        out.append({
            "key":          dep["key"],
            "label":        dep["label"],
            "description":  dep["description"],
            "size":         dep.get("size"),
            "auto_install": dep.get("auto_install", True),
            "browser_only": dep.get("browser_only", False),
            "browser_url":  dep.get("browser_url"),
            "installed":    present,
            "running":      running,
        })
    return {"deps": out, "checked_at": time.time()}


def install_missing(only: Optional[list] = None,
                    skip_opt_outs: bool = True) -> dict:
    """Install every missing auto-installable dep that the user hasn't
    opted out of.  Runs in the calling thread — use install_missing_async()
    for the background variant.

    Args:
        only:  if set, install only these dep keys
        skip_opt_outs: respect the per-dep opt_out list in state
    """
    state    = _load_state()
    opt_outs = set(state.get("opt_outs", []) or []) if skip_opt_outs else set()

    completed   = []
    failed      = []
    skipped     = []
    needs_rs    = False

    deps_to_run = [d for d in DEPENDENCIES if d.get("auto_install")
                   and not d.get("browser_only")
                   and (only is None or d["key"] in only)]

    _set_progress(running=True, started_at=time.time(),
                  completed=[], failed=[], needs_restart=False,
                  current_dep=None, current_step=None)

    for dep in deps_to_run:
        if dep["key"] in opt_outs:
            skipped.append(dep["key"])
            continue
        try:
            if dep["detect"]():
                log.info(f"dep {dep['key']} already present")
                completed.append({"key": dep["key"], "already_installed": True})
                continue
        except Exception: pass

        _set_progress(current_dep=dep["key"], current_step=f"Installing {dep['label']}…",
                      downloaded=0, total=0)
        log.info(f"installing dep: {dep['key']}")
        try:
            r = dep["install"]()
        except Exception as e:
            r = {"ok": False, "needs_restart": False, "message": str(e)}
        if r.get("ok"):
            completed.append({"key": dep["key"], **r})
        else:
            failed.append({"key": dep["key"], **r})
        if r.get("needs_restart"):
            needs_rs = True
        with _progress_lock:
            _progress["completed"] = list(completed)
            _progress["failed"]    = list(failed)
            _progress["needs_restart"] = needs_rs

    # Persist
    state["last_check_at"] = time.time()
    if needs_rs:
        state["pending_restart"] = {
            "set_at":  time.time(),
            "by":      [c["key"] for c in completed if c.get("needs_restart")],
        }
    _save_state(state)
    _set_progress(running=False, ended_at=time.time(),
                  current_dep=None, current_step=None)

    return {
        "completed": completed,
        "failed":    failed,
        "skipped":   skipped,
        "needs_restart": needs_rs,
    }


def install_missing_async(only: Optional[list] = None) -> dict:
    """Kick off install_missing() on a background thread.  Returns
    immediately; poll get_progress() to track."""
    with _progress_lock:
        if _progress["running"]:
            return {"ok": False, "err": "an install batch is already running"}
    threading.Thread(
        target=lambda: install_missing(only=only),
        daemon=True,
        name="dep-install",
    ).start()
    return {"ok": True, "started": True}


def check_on_boot() -> dict:
    """Called by app.py at Flask startup on a background thread.  Reads
    the user's opt-in setting and either silently auto-installs missing
    deps or just records what's missing for the UI to surface.

    v3.2-beta.9 — also runs a "post-install nudge" pass: deps that are
    installed on disk but not active right now (RTSS in particular) get
    launched here.  Without this nudge, a user that installed RTSS via
    a previous GhostShell boot would see 'not running' forever until
    they manually started it."""
    from config import APP_NAME

    state = _load_state()
    auto_install_enabled = state.get("auto_install_enabled", True)

    log.info(f"{APP_NAME}: dependency boot check (auto_install={auto_install_enabled})")
    status = check_all()
    missing_auto = [d for d in status["deps"]
                     if not d["installed"] and d.get("auto_install")
                     and not d.get("browser_only")]
    missing_manual = [d for d in status["deps"]
                       if not d["installed"] and d.get("browser_only")]

    # beta.15 — RTSS auto-launch nudge removed (used to fight Afterburner).
    # 3.3.3 — LHM nudge added.  LHM is harmless (no in-game overlay, no
    # OC writes) and is the only way to read CPU temp on modern Windows
    # desktops.  If installed but not running, launch it minimized to
    # the tray so its WMI provider becomes available.
    nudge_results: list = []
    try:
        lhm_state = _detect_lhm()
        if lhm_state.get("installed") and not lhm_state.get("wmi_registered"):
            log.info("LHM installed but WMI namespace not active — launching")
            launch = start_lhm()
            nudge_results.append({
                "key":     "lhm",
                "launched": True,
                "running":  bool(launch.get("running")),
                "wmi_registered": bool(launch.get("wmi_registered")),
                "message":  launch.get("message"),
            })
        elif lhm_state.get("installed") and lhm_state.get("running"):
            # 3.5.2 — self-heal a window-popping LHM.  If the on-disk config
            # needed correcting (start-minimized keys absent/false), the
            # RUNNING instance both shows its main window AND would rewrite
            # the bad values back on graceful exit (LHM persists its settings
            # at shutdown).  Force-kill (skips its config save) and relaunch —
            # start_lhm re-verifies the config and LHM comes back in the tray.
            exe = _find_lhm_exe()
            if exe and _ensure_lhm_settings_minimized(exe):
                log.info("LHM running with window-popping config — restarting it hidden")
                try:
                    subprocess.run(["taskkill", "/F", "/IM", "LibreHardwareMonitor.exe"],
                                   capture_output=True, timeout=10)
                    time.sleep(1.0)
                except Exception as e:
                    log.debug(f"LHM force-kill failed: {e}")
                launch = start_lhm()
                nudge_results.append({
                    "key":     "lhm",
                    "launched": True,
                    "restarted_hidden": True,
                    "running":  bool(launch.get("running")),
                    "wmi_registered": bool(launch.get("wmi_registered")),
                    "message":  "Restarted Libre Hardware Monitor minimized to the tray",
                })
    except Exception as e:
        log.debug(f"LHM boot-time nudge failed: {e}")

    if missing_auto and auto_install_enabled:
        log.info(f"auto-installing {len(missing_auto)} missing deps: "
                 f"{[d['key'] for d in missing_auto]}")
        r = install_missing()
        return {"checked": True, "auto_installed": r,
                "nudges": nudge_results,
                "manual_pending": [d["key"] for d in missing_manual]}
    return {"checked": True, "auto_installed": None,
            "nudges": nudge_results,
            "missing_auto":   [d["key"] for d in missing_auto],
            "manual_pending": [d["key"] for d in missing_manual]}


# ─── Restart helpers ─────────────────────────────────────────────────

def has_pending_restart() -> dict:
    """Returns whether a restart is recommended.  Combines two signals:
    (a) our own dep-install marker, (b) Windows' own pending-reboot keys."""
    state = _load_state()
    own   = bool(state.get("pending_restart"))
    win   = _windows_pending_reboot()
    info  = {"pending":     own or win,
             "by_ghostshell": own,
             "by_windows":  win,
             "details":     state.get("pending_restart", {})}
    return info


def request_restart_now() -> dict:
    """Initiate a Windows restart in 10 seconds.  User can cancel with
    `shutdown /a` if they change their mind."""
    try:
        subprocess.run(
            ["shutdown", "/r", "/t", "10",
             "/c", "GhostShell installed driver updates that need a restart."],
            creationflags=_CREATE_NO_WINDOW,
        )
        # Clear our own pending marker so we don't re-prompt
        state = _load_state()
        state.pop("pending_restart", None)
        _save_state(state)
        return {"ok": True, "in_seconds": 10}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def cancel_pending_restart() -> dict:
    """User clicked 'I'll restart later'.  Clear our marker so the
    banner stops appearing this session — Windows' own marker will
    persist and reappear next time GhostShell starts."""
    state = _load_state()
    state.pop("pending_restart", None)
    state["restart_deferred_at"] = time.time()
    _save_state(state)
    return {"ok": True, "deferred": True}


# ─── Settings ────────────────────────────────────────────────────────

def set_auto_install_enabled(enabled: bool) -> dict:
    state = _load_state()
    state["auto_install_enabled"] = bool(enabled)
    _save_state(state)
    return {"ok": True, "enabled": bool(enabled)}


def set_opt_out(key: str, opted_out: bool) -> dict:
    state = _load_state()
    opt_outs = set(state.get("opt_outs", []) or [])
    if opted_out:
        opt_outs.add(key)
    else:
        opt_outs.discard(key)
    state["opt_outs"] = sorted(opt_outs)
    _save_state(state)
    return {"ok": True, "opt_outs": list(opt_outs)}


def get_settings() -> dict:
    state = _load_state()
    return {
        "auto_install_enabled": state.get("auto_install_enabled", True),
        "opt_outs":             state.get("opt_outs", []) or [],
        "last_check_at":        state.get("last_check_at"),
        "pending_restart":      state.get("pending_restart"),
    }
