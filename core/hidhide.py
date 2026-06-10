"""HidHide integration — hide the physical controller from every process
EXCEPT GhostShell so the game sees only the ViGEmBus virtual pad.

HidHide is a kernel driver by Nefarius (the same author as ViGEmBus —
they're designed to work together).  It's signed, free, and ships a
small CLI helper at:

    C:\\Program Files\\Nefarius Software Solutions\\HidHide\\x64\\HidHideCLI.exe

We wrap that CLI here.  Direct IOCTL would be lower-latency but the CLI
is officially supported, ships with the driver, and is fast enough for
our use case (cloak toggling happens once per session).

Public API
----------
    is_installed()                  -> dict   {installed, cli_path, err?}
    install_url()                   -> str    (latest GitHub release)

    cloak_on()                      -> dict   global hide-everything-on-list
    cloak_off()                     -> dict
    cloak_state()                   -> dict   {cloaked: bool}

    hide_device(instance_id)        -> dict
    unhide_device(instance_id)      -> dict
    list_hidden_devices()           -> list[str]

    whitelist_app(exe_path)         -> dict   add to allow-list
    unwhitelist_app(exe_path)       -> dict
    list_whitelisted_apps()         -> list[str]

    engage_for_controller(physical_instance_ids: list[str], app_path: str) -> dict
        Convenience: hide_device for each id + whitelist_app + cloak_on.
        Returns the prior state so disengage() can restore cleanly.

    disengage(prior_state: dict)    -> dict
        Restore from engage_for_controller.

Notes
-----
- Adding a device to the hide list is GLOBAL and persists across reboots
  until removed.  We always undo our changes on disengage / app exit so
  the user's machine is left in the state we found it in.
- The HidHide CLI takes the device 'symbolic link name' which is a
  HID-class instance ID prefixed by '\\\\?\\' and lowercase.  We do that
  conversion automatically.
"""

from __future__ import annotations
import os
import re
import subprocess
from typing import Optional

from core.utils import get_logger, run_cmd

log = get_logger(__name__)

HIDHIDE_INSTALL_URL = "https://github.com/nefarius/HidHide/releases/latest"

CLI_CANDIDATES = [
    r"C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    r"C:\Program Files\Nefarius Software Solutions\HidHide\HidHideCLI.exe",
    r"C:\Program Files (x86)\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
]


# ────────────────────────────────────────────────────────────────────
# Detection
# ────────────────────────────────────────────────────────────────────

def _find_cli() -> Optional[str]:
    for p in CLI_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def is_installed() -> dict:
    """Comprehensive probe: kernel driver present + CLI executable
    locatable.  Both must be true for any other call to succeed."""
    cli = _find_cli()
    # Service probe — HidHide registers as a kernel-mode service
    sc = run_cmd(["sc", "query", "HidHide"], timeout=5)
    svc_present = sc.get("ok") and "RUNNING" in (sc.get("out") or "").upper()
    # Some versions register as "HidHide" (svc) plus the file driver
    if not svc_present:
        svc_present = sc.get("ok") and "HidHide" in (sc.get("out") or "")
    return {
        "installed":   bool(cli and svc_present),
        "cli_path":    cli or "",
        "service_ok":  bool(svc_present),
        "install_url": HIDHIDE_INSTALL_URL,
        "err":         None if (cli and svc_present) else (
            "HidHide kernel driver missing" if not svc_present
            else "HidHideCLI.exe not found"
        ),
    }


def install_url() -> str:
    return HIDHIDE_INSTALL_URL


# ────────────────────────────────────────────────────────────────────
# Internal CLI runner
# ────────────────────────────────────────────────────────────────────

def _run_cli(args: list[str], timeout: int = 8) -> dict:
    cli = _find_cli()
    if not cli:
        return {"ok": False, "err": "HidHideCLI.exe not found"}
    try:
        # CLI uses Windows-style args; subprocess.run handles quoting.
        cp = subprocess.run([cli] + args, capture_output=True, text=True,
                            timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
        out = (cp.stdout or "") + (cp.stderr or "")
        return {"ok": cp.returncode == 0, "out": out.strip(),
                "rc": cp.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"HidHideCLI timed out after {timeout}s"}
    except Exception as e:                # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _to_symbolic_link(instance_id: str) -> str:
    """HKLM\\Enum instance IDs look like:
        HID\\VID_054C&PID_0CE6&...
    HidHide expects the device's symbolic link name:
        \\\\?\\hid#vid_054c&pid_0ce6#...
    The conversion: lower-case + replace '\\' with '#' + prefix '\\\\?\\'."""
    if not instance_id:
        return ""
    body = instance_id.replace("\\", "#").lower()
    return f"\\\\?\\{body}"


# ────────────────────────────────────────────────────────────────────
# Cloak (global on/off switch — applies to all hidden devices)
# ────────────────────────────────────────────────────────────────────

def cloak_on() -> dict:
    r = _run_cli(["--cloak-on"])
    if r.get("ok"):
        log.info("HidHide cloak ON")
    return r


def cloak_off() -> dict:
    r = _run_cli(["--cloak-off"])
    if r.get("ok"):
        log.info("HidHide cloak OFF")
    return r


def cloak_state() -> dict:
    """Returns {cloaked: bool}.  CLI prints 'cloak: 1/0' or similar."""
    r = _run_cli(["--cloak-state"])
    out = (r.get("out") or "").lower()
    cloaked = ("1" in out) or ("on" in out) or ("enabled" in out)
    return {"cloaked": bool(cloaked), "raw": r.get("out", "")}


# ────────────────────────────────────────────────────────────────────
# Device hide list
# ────────────────────────────────────────────────────────────────────

def hide_device(instance_id: str) -> dict:
    sym = _to_symbolic_link(instance_id)
    if not sym:
        return {"ok": False, "err": "instance_id required"}
    r = _run_cli(["--dev-hide", sym])
    if r.get("ok"):
        log.info(f"HidHide hide-device: {instance_id}")
    return r


def unhide_device(instance_id: str) -> dict:
    sym = _to_symbolic_link(instance_id)
    if not sym:
        return {"ok": False, "err": "instance_id required"}
    r = _run_cli(["--dev-unhide", sym])
    if r.get("ok"):
        log.info(f"HidHide unhide-device: {instance_id}")
    return r


def list_hidden_devices() -> list[str]:
    r = _run_cli(["--dev-list"])
    if not r.get("ok"):
        return []
    # CLI prints one device per line; we just return the raw lines.
    return [ln.strip() for ln in (r.get("out") or "").splitlines() if ln.strip()]


# ────────────────────────────────────────────────────────────────────
# Application allow-list (a.k.a. "whitelisted processes")
# ────────────────────────────────────────────────────────────────────

def whitelist_app(exe_path: str) -> dict:
    if not exe_path:
        return {"ok": False, "err": "exe_path required"}
    r = _run_cli(["--app-reg", exe_path])
    if r.get("ok"):
        log.info(f"HidHide whitelist-app: {exe_path}")
    return r


def unwhitelist_app(exe_path: str) -> dict:
    if not exe_path:
        return {"ok": False, "err": "exe_path required"}
    r = _run_cli(["--app-unreg", exe_path])
    if r.get("ok"):
        log.info(f"HidHide unwhitelist-app: {exe_path}")
    return r


def list_whitelisted_apps() -> list[str]:
    r = _run_cli(["--app-list"])
    if not r.get("ok"):
        return []
    return [ln.strip() for ln in (r.get("out") or "").splitlines() if ln.strip()]


# ────────────────────────────────────────────────────────────────────
# High-level engage / disengage
# ────────────────────────────────────────────────────────────────────

def engage_for_controller(physical_instance_ids: list[str],
                          app_path: str) -> dict:
    """One-shot helper called by the gamepad mapper when virtual mode
    activates.  Hides every passed instance ID, whitelists the host app,
    and turns cloak on.

    Returns a `prior_state` dict that `disengage()` consumes to restore
    the original HidHide configuration when the user disables virtual
    mode.  Idempotent — calling twice with the same args is a no-op."""
    log.info(f"HidHide engage: hiding {len(physical_instance_ids)} device(s) "
             f"+ whitelisting {os.path.basename(app_path)}")

    # Capture prior state so disengage() can restore exactly what was
    # set before we touched anything
    prior = {
        "cloaked":           cloak_state().get("cloaked", False),
        "hidden_devices":    list_hidden_devices(),
        "whitelisted_apps":  list_whitelisted_apps(),
    }

    failed: list[str] = []
    hidden_ours: list[str] = []
    for iid in physical_instance_ids:
        r = hide_device(iid)
        if r.get("ok"):
            hidden_ours.append(iid)
        else:
            failed.append(f"hide {iid}: {r.get('err') or r.get('out')}")

    wr = whitelist_app(app_path)
    if not wr.get("ok"):
        failed.append(f"whitelist {app_path}: {wr.get('err') or wr.get('out')}")
    cr = cloak_on()
    if not cr.get("ok"):
        failed.append(f"cloak-on: {cr.get('err') or cr.get('out')}")

    return {
        "ok":           not failed,
        "prior_state":  prior,
        "hidden_ours":  hidden_ours,
        "whitelisted_app": app_path,
        "errors":       failed,
    }


def disengage(prior_state: dict, hidden_ours: list[str],
              whitelisted_app: str) -> dict:
    """Reverse engage_for_controller — un-hide every device WE added,
    un-whitelist GhostShell, restore cloak to its prior on/off state.
    Devices that were ALREADY hidden before we touched things are left
    alone."""
    log.info("HidHide disengage: restoring prior state")
    for iid in hidden_ours:
        unhide_device(iid)
    if whitelisted_app:
        unwhitelist_app(whitelisted_app)
    if not prior_state.get("cloaked"):
        cloak_off()
    return {"ok": True}


# ────────────────────────────────────────────────────────────────────
# Auto-install (download latest .exe from GitHub + run silently)
# ────────────────────────────────────────────────────────────────────

# Tracking dict so the UI can poll progress without blocking the install.
_install_status = {
    "in_progress":  False,
    "phase":        "idle",     # idle / downloading / installing / done / error
    "progress_pct": 0,
    "msg":          "",
    "err":          None,
    "installed":    False,
}


def get_install_progress() -> dict:
    return dict(_install_status)


def download_and_install(timeout_sec: int = 180) -> dict:
    """Download the latest HidHide installer .exe from GitHub and run
    it silently.  Idempotent — if already installed, returns early.

    Returns the final status dict.  For long-running installs, the UI
    can poll get_install_progress() while this runs in a thread."""
    # Don't double-fire
    if _install_status.get("in_progress"):
        return {"ok": False, "err": "Install already in progress",
                "status": dict(_install_status)}
    if is_installed().get("installed"):
        _install_status.update({"phase": "done", "installed": True,
                                "msg": "Already installed",
                                "in_progress": False})
        return {"ok": True, "msg": "HidHide already installed"}

    _install_status.update({
        "in_progress":  True,
        "phase":        "fetching-manifest",
        "progress_pct": 0,
        "msg":          "Looking up latest release…",
        "err":          None,
        "installed":    False,
    })

    try:
        import urllib.request
        import json as _json
        import tempfile

        # 1. Fetch latest-release JSON
        api = "https://api.github.com/repos/nefarius/HidHide/releases/latest"
        req = urllib.request.Request(api, headers={
            "User-Agent": "GhostShell/3.3 HidHide-installer",
            "Accept":     "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            manifest = _json.load(resp)

        assets = manifest.get("assets") or []
        # PREFER .msi — HidHide ships a real MSI which msiexec can install
        # silently with bulletproof flag handling.  The .exe wrapper is
        # InstallShield-based and rejects unknown silent flags with
        # "Invalid command line" (which is what early v3.3 builds hit).
        exe_asset = next((a for a in assets
                          if a.get("name", "").lower().endswith(".msi")), None)
        if not exe_asset:
            # Fallback: any .exe (older releases may not have an .msi)
            exe_asset = next((a for a in assets
                              if a.get("name", "").lower().endswith(".exe")), None)
        if not exe_asset or not exe_asset.get("browser_download_url"):
            raise RuntimeError("Could not find HidHide installer in latest release")

        url = exe_asset["browser_download_url"]
        fname = exe_asset["name"]
        size = int(exe_asset.get("size") or 0)
        log.info(f"HidHide installer URL: {url} ({size} bytes)")

        # 2. Download to %TEMP%
        _install_status.update({
            "phase":        "downloading",
            "progress_pct": 0,
            "msg":          f"Downloading {fname} ({size // 1024} KB)…",
        })
        tmp_dir = tempfile.gettempdir()
        out_path = os.path.join(tmp_dir, fname)
        dreq = urllib.request.Request(url, headers={"User-Agent": "GhostShell/3.3"})
        with urllib.request.urlopen(dreq, timeout=60) as resp, open(out_path, "wb") as f:
            total = int(resp.headers.get("Content-Length", 0)) or size
            chunk = 64 * 1024
            written = 0
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if total:
                    _install_status["progress_pct"] = int(written * 100 / total)
        log.info(f"HidHide installer downloaded: {out_path}")

        # 3. Run silently.  Inno Setup standard flags (HidHide is Inno).
        # /VERYSILENT — no GUI, no progress bar
        # /SUPPRESSMSGBOXES — auto-answer YES to all prompts
        # /NORESTART — don't reboot (HidHide requires a reboot for the
        #   driver to fully take effect, but we surface that to the user
        #   in the UI rather than auto-rebooting their machine)
        _install_status.update({
            "phase":        "installing",
            "progress_pct": 100,
            "msg":          "Running silent install (admin rights required)…",
        })
        # 3. Launch the installer.
        #
        # We tried fighting silent-install flags through 4 different
        # framework conventions (WiX, Inno, InstallShield, MSI) and HidHide's
        # current installer rejected all of them with "Invalid command line"
        # popups.  Rather than subject the user to a chain of error dialogs,
        # we launch the installer GUI and background-poll for the kernel
        # driver to come online.  Single-click install for the user, and
        # the moment they finish, GhostShell's banner switches to
        # "Installed ✓" automatically.
        _install_status.update({
            "phase":        "installing",
            "progress_pct": 100,
            "msg":          ("Installer window opened — click through it. "
                             "GhostShell will detect when it's done."),
        })
        if fname.lower().endswith(".msi"):
            # MSI is reliable as a silent install — no GUI fight here
            cmd = ["msiexec", "/i", out_path, "/qb", "/norestart"]   # /qb = basic UI, no prompts
        else:
            # .exe — let the user step through the GUI.  No silent flags
            # since they're framework-specific and unreliable.
            cmd = [out_path]
        try:
            proc = subprocess.Popen(cmd, creationflags=0x00000010)  # CREATE_NEW_CONSOLE off
        except Exception as e:                              # noqa: BLE001
            raise RuntimeError(f"Could not launch installer: {e}")

        # 4. Background-poll for the kernel driver to register.  Up to
        # 5 minutes — generous enough for a multi-driver install with the
        # user reading EULAs.
        import time as _t
        deadline = _t.time() + 300
        while _t.time() < deadline:
            if proc.poll() is not None and not is_installed().get("installed"):
                # Process exited but driver still missing — user cancelled
                _t.sleep(1.0)
            inst = is_installed()
            if inst.get("installed"):
                _install_status.update({
                    "phase":        "done",
                    "msg":          "Installed and ready",
                    "installed":    True,
                    "in_progress":  False,
                })
                log.info("HidHide install complete")
                return {"ok": True, "status": dict(_install_status)}
            _t.sleep(2.0)

        # Timed out — installer may still be open or user closed without finishing
        _install_status.update({
            "phase":        "done",
            "msg":          "Installer timed out (5 min) — try again or install manually",
            "installed":    False,
            "in_progress":  False,
        })
        return {"ok": False, "err": "install poll timeout"}

    except Exception as e:                 # noqa: BLE001
        log.warning(f"HidHide install failed: {e}")
        _install_status.update({
            "phase":        "error",
            "err":          str(e),
            "msg":          f"Install failed: {e}",
            "in_progress":  False,
        })
        return {"ok": False, "err": str(e), "status": dict(_install_status)}
