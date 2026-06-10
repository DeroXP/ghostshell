"""ViGEmBus integration — creates a virtual Xbox 360 controller and lets
the gamepad mapper forward physical-controller state through it.

This is the v3.2 piece that gives us TRUE button-to-button remap (the
game sees ONLY the virtual controller's state, not the physical one).
v1's `key_emulation` mode just adds keyboard events on top of the
existing physical-controller stream — useful but limited.

Architecture
------------
    Physical controller  →  XInput poll @ 1 kHz  →  apply remap table  →
        →  vgamepad / ViGEmBus  →  virtual Xbox 360  →  game

ViGEmBus is a kernel driver by Nefarius (open source, signed).  Users
install it once via .msi (~3 MB) — no reboot needed.  Without it,
`vgamepad.VX360Gamepad()` raises a clear error which we surface in the
UI as "ViGEmBus not installed → install it from <link>".

What's NOT in v3.2
------------------
- HidHide (the companion driver that hides the physical controller from
  the game) is intentionally separate.  Without HidHide the game sees
  BOTH the physical pad AND our virtual one.  Workarounds:
    * Disable Steam Input for the game in Steam settings
    * Or: Game uses XInput → unplug physical / virtual takes a free slot
  Documented in the UI.

Public API
----------
    is_available()   -> bool  (vgamepad importable + ViGEmBus driver installed)
    install_url()    -> str   (GitHub release URL for ViGEmBus.msi)
    create()         -> VirtualPad | None
    VirtualPad.set_state(physical_state, mapping)
    VirtualPad.close()
"""

from __future__ import annotations
import threading
from typing import Optional, Any

from core.utils import get_logger
from core import xinput_ctypes as xi

log = get_logger(__name__)

VIGEMBUS_INSTALL_URL = (
    "https://github.com/nefarius/ViGEmBus/releases/latest"
)

# Lazy-loaded vgamepad module — fail soft so the rest of GhostShell
# doesn't crash on systems that never installed it.
_vg = None
_vg_import_err: Optional[str] = None


def _load_vg():
    global _vg, _vg_import_err
    if _vg is not None or _vg_import_err is not None:
        return _vg
    try:
        import vgamepad as vg
        _vg = vg
    except Exception as e:        # noqa: BLE001
        _vg_import_err = str(e)
        log.debug(f"vgamepad import failed: {e}")
    return _vg


# ────────────────────────────────────────────────────────────────────
# Capability probe
# ────────────────────────────────────────────────────────────────────

def is_available() -> dict:
    """Returns a dict with three booleans + the install URL:
        { vgamepad_installed: bool, vigembus_installed: bool,
          available: bool, install_url: str, err: str | None }

    `available` is True only when BOTH the Python lib AND the kernel
    driver are usable — i.e. we can actually create a virtual pad."""
    vg = _load_vg()
    if vg is None:
        return {
            "vgamepad_installed":  False,
            "vigembus_installed":  False,
            "available":           False,
            "install_url":         VIGEMBUS_INSTALL_URL,
            "err":                 _vg_import_err or "vgamepad not installed",
        }
    # Try a one-shot create + immediate close to confirm the driver itself
    # is responsive.  Cheap (~5 ms).
    try:
        gp = vg.VX360Gamepad()
        # Some versions need an explicit reset/close; not all expose .__del__
        try: del gp
        except Exception: pass
        return {
            "vgamepad_installed":  True,
            "vigembus_installed":  True,
            "available":           True,
            "install_url":         VIGEMBUS_INSTALL_URL,
            "err":                 None,
        }
    except Exception as e:        # noqa: BLE001
        return {
            "vgamepad_installed":  True,
            "vigembus_installed":  False,
            "available":           False,
            "install_url":         VIGEMBUS_INSTALL_URL,
            "err":                 f"ViGEmBus driver missing or unresponsive: {e}",
        }


# ────────────────────────────────────────────────────────────────────
# VirtualPad — wraps a single VX360Gamepad with a clean set_state API
# ────────────────────────────────────────────────────────────────────

# Pad type constants
PAD_TYPE_X360 = "x360"
PAD_TYPE_DS4  = "ds4"
SUPPORTED_PAD_TYPES = (PAD_TYPE_X360, PAD_TYPE_DS4)

# Cached enum maps per pad type — built lazily once vgamepad is loaded.
_X360_BUTTON_MAP: Optional[dict] = None
_DS4_BUTTON_MAP:  Optional[dict] = None
_DS4_DPAD_MAP:    Optional[dict] = None


def _build_x360_button_map(vg) -> dict:
    """Map our internal Xbox-naming buttons → vgamepad XUSB_BUTTON enum."""
    XB = vg.XUSB_BUTTON
    return {
        "A":          XB.XUSB_GAMEPAD_A,
        "B":          XB.XUSB_GAMEPAD_B,
        "X":          XB.XUSB_GAMEPAD_X,
        "Y":          XB.XUSB_GAMEPAD_Y,
        "LB":         XB.XUSB_GAMEPAD_LEFT_SHOULDER,
        "RB":         XB.XUSB_GAMEPAD_RIGHT_SHOULDER,
        "BACK":       XB.XUSB_GAMEPAD_BACK,
        "START":      XB.XUSB_GAMEPAD_START,
        "LSTICK":     XB.XUSB_GAMEPAD_LEFT_THUMB,
        "RSTICK":     XB.XUSB_GAMEPAD_RIGHT_THUMB,
        "DPAD_UP":    XB.XUSB_GAMEPAD_DPAD_UP,
        "DPAD_DOWN":  XB.XUSB_GAMEPAD_DPAD_DOWN,
        "DPAD_LEFT":  XB.XUSB_GAMEPAD_DPAD_LEFT,
        "DPAD_RIGHT": XB.XUSB_GAMEPAD_DPAD_RIGHT,
    }


def _build_ds4_button_map(vg) -> dict:
    """Map our internal Xbox-naming buttons → vgamepad DS4_BUTTONS enum.
    Translation rule: same logical position, different label.
       A     → CROSS
       B     → CIRCLE
       X     → SQUARE
       Y     → TRIANGLE
       LB    → SHOULDER_LEFT (L1)
       RB    → SHOULDER_RIGHT (R1)
       BACK  → SHARE
       START → OPTIONS
       LSTICK→ THUMB_LEFT (L3)
       RSTICK→ THUMB_RIGHT (R3)

    Note: DPAD on DS4 is a SINGLE direction enum (NORTH, NORTHEAST, …),
    not 4 separate buttons.  Handled separately in _ds4_dpad_for()."""
    DB = vg.DS4_BUTTONS
    return {
        "A":      DB.DS4_BUTTON_CROSS,
        "B":      DB.DS4_BUTTON_CIRCLE,
        "X":      DB.DS4_BUTTON_SQUARE,
        "Y":      DB.DS4_BUTTON_TRIANGLE,
        "LB":     DB.DS4_BUTTON_SHOULDER_LEFT,
        "RB":     DB.DS4_BUTTON_SHOULDER_RIGHT,
        "BACK":   DB.DS4_BUTTON_SHARE,
        "START":  DB.DS4_BUTTON_OPTIONS,
        "LSTICK": DB.DS4_BUTTON_THUMB_LEFT,
        "RSTICK": DB.DS4_BUTTON_THUMB_RIGHT,
        # DPAD entries are NOT in this map — DS4 dpad is a separate
        # enum that takes 9 directional values (none + 8 compass points).
    }


def _build_ds4_dpad_map(vg) -> dict:
    """8-way DS4 dpad direction lookup.  Key is a frozenset of pressed
    DPAD_* names (since combining UP+RIGHT must produce NORTHEAST)."""
    DD = vg.DS4_DPAD_DIRECTIONS
    return {
        frozenset():                         DD.DS4_BUTTON_DPAD_NONE,
        frozenset(["DPAD_UP"]):              DD.DS4_BUTTON_DPAD_NORTH,
        frozenset(["DPAD_UP", "DPAD_RIGHT"]):DD.DS4_BUTTON_DPAD_NORTHEAST,
        frozenset(["DPAD_RIGHT"]):           DD.DS4_BUTTON_DPAD_EAST,
        frozenset(["DPAD_DOWN", "DPAD_RIGHT"]):DD.DS4_BUTTON_DPAD_SOUTHEAST,
        frozenset(["DPAD_DOWN"]):            DD.DS4_BUTTON_DPAD_SOUTH,
        frozenset(["DPAD_DOWN", "DPAD_LEFT"]):DD.DS4_BUTTON_DPAD_SOUTHWEST,
        frozenset(["DPAD_LEFT"]):            DD.DS4_BUTTON_DPAD_WEST,
        frozenset(["DPAD_UP", "DPAD_LEFT"]): DD.DS4_BUTTON_DPAD_NORTHWEST,
    }


def _ds4_dpad_for(pressed: set, dpad_map: dict):
    """Pick the 8-way DS4 dpad direction matching the currently-pressed
    DPAD_* set.  Falls back to NONE if the combination isn't a clean
    cardinal/intercardinal (e.g. UP+DOWN simultaneously)."""
    dp = pressed & {"DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"}
    direction = dpad_map.get(frozenset(dp))
    if direction is None:
        direction = dpad_map[frozenset()]    # NONE
    return direction


class VirtualPad:
    """Wraps a vgamepad VX360Gamepad OR VDS4Gamepad.  Thread-safe via a
    single lock.  `set_state` does an atomic build → flush so frames are
    coherent on the wire."""

    def __init__(self, vg, gp, pad_type: str,
                 button_map: dict,
                 dpad_map: Optional[dict] = None):
        self._vg = vg
        self._gp = gp
        self._pad_type = pad_type           # "x360" or "ds4"
        self._button_map = button_map
        self._dpad_map = dpad_map           # only set for DS4
        self._lock = threading.Lock()

    @property
    def pad_type(self) -> str:
        return self._pad_type

    def set_state(self,
                  physical: dict,
                  mapping: dict,
                  passthrough_axes: bool = True) -> None:
        """Update the virtual controller state for one frame."""
        with self._lock:
            gp = self._gp
            try:
                gp.reset()
            except Exception:
                pass

            # ── Resolve button targets (same logic for both pad types) ──
            now_pressed = set(physical.get("pressed", []))
            target_pressed: set = set()
            for phys_btn in now_pressed:
                binding = mapping.get(phys_btn) if mapping else None
                if not binding or binding.get("type") in (None, "passthrough"):
                    target_pressed.add(phys_btn)
                elif binding.get("type") == "disabled":
                    continue
                elif binding.get("type") == "vbutton":
                    target_pressed.add(binding.get("vbutton") or phys_btn)
                # key/macro/mouse mappings don't affect the virtual pad

            # ── Push buttons + axes per pad type ───────────────────
            try:
                if self._pad_type == PAD_TYPE_X360:
                    self._push_x360(gp, target_pressed, physical, passthrough_axes)
                else:
                    self._push_ds4(gp, target_pressed, physical, passthrough_axes)
            except Exception as e:
                log.debug(f"virtual pad push failed ({self._pad_type}): {e}")

            # ── Flush to driver ────────────────────────────────────
            try:
                gp.update()
            except Exception as e:
                log.warning(f"virtual pad update failed: {e}")

    def _push_x360(self, gp, target_pressed: set, physical: dict,
                   passthrough_axes: bool):
        for btn in target_pressed:
            enum_btn = self._button_map.get(btn)
            if enum_btn is not None:
                gp.press_button(button=enum_btn)
        if passthrough_axes:
            gp.left_joystick(x_value=physical.get("lx", 0),
                             y_value=physical.get("ly", 0))
            gp.right_joystick(x_value=physical.get("rx", 0),
                              y_value=physical.get("ry", 0))
            gp.left_trigger(value=physical.get("lt", 0))
            gp.right_trigger(value=physical.get("rt", 0))

    def _push_ds4(self, gp, target_pressed: set, physical: dict,
                  passthrough_axes: bool):
        # Face buttons + bumpers + back/start/L3/R3 PLUS the DS4-only
        # special buttons (PS button, touchpad press).  The special
        # buttons go through a different vgamepad API (press_special_button
        # vs press_button) because they live in a separate report byte
        # (bSpecial) than the regular wButtons word.
        SPECIAL = {"PS", "TOUCHPAD"}
        for btn in target_pressed:
            if btn in SPECIAL:
                try:
                    SB = self._vg.DS4_SPECIAL_BUTTONS
                    enum = (SB.DS4_SPECIAL_BUTTON_PS if btn == "PS"
                            else SB.DS4_SPECIAL_BUTTON_TOUCHPAD)
                    gp.press_special_button(enum)
                except Exception as e:
                    log.debug(f"DS4 special-button {btn} failed: {e}")
                continue
            enum_btn = self._button_map.get(btn)
            if enum_btn is not None:
                gp.press_button(button=enum_btn)
        # DPAD — DS4 expects ONE direction value (8-way + NONE)
        if self._dpad_map is not None:
            direction = _ds4_dpad_for(target_pressed, self._dpad_map)
            try:
                gp.directional_pad(direction=direction)
            except Exception:
                pass
        # Sticks: DS4 API takes float -1..1.  Convert from XInput int16.
        #
        # Y-axis CONVENTION FLIP — XInput uses +y=UP (math convention),
        # vgamepad's DS4 API uses +y=DOWN (DS4's native HID convention,
        # 0x00=up 0x80=center 0xFF=down).  We negate Y on the way out so
        # passthrough actually preserves orientation: pushing the physical
        # stick up shows the virtual stick going up.  X axes match between
        # conventions, no flip there.
        if passthrough_axes:
            try:
                gp.left_joystick_float(
                    x_value_float=max(-1.0, min(1.0,  physical.get("lx", 0) / 32767)),
                    y_value_float=max(-1.0, min(1.0, -physical.get("ly", 0) / 32767)),
                )
                gp.right_joystick_float(
                    x_value_float=max(-1.0, min(1.0,  physical.get("rx", 0) / 32767)),
                    y_value_float=max(-1.0, min(1.0, -physical.get("ry", 0) / 32767)),
                )
                # DS4 triggers are 0-255 same as Xbox
                gp.left_trigger(value=int(physical.get("lt", 0)))
                gp.right_trigger(value=int(physical.get("rt", 0)))
            except Exception as e:
                log.debug(f"DS4 axes update failed: {e}")

    def close(self):
        with self._lock:
            try:
                self._gp.reset()
                self._gp.update()
            except Exception:
                pass
            self._gp = None


# ────────────────────────────────────────────────────────────────────
# Factory
# ────────────────────────────────────────────────────────────────────

def create(pad_type: str = PAD_TYPE_X360) -> Optional[VirtualPad]:
    """Create a virtual controller.  pad_type:
       'x360' (default) → Xbox 360 controller (XInput)
       'ds4'            → DualShock 4 (DInput-style, PlayStation prompts in games)
    Returns None if ViGEmBus isn't available — caller should fall back to
    key_emulation mode."""
    vg = _load_vg()
    if vg is None:
        return None
    if pad_type not in SUPPORTED_PAD_TYPES:
        log.warning(f"Unknown pad_type {pad_type!r}, falling back to x360")
        pad_type = PAD_TYPE_X360
    try:
        if pad_type == PAD_TYPE_DS4:
            gp = vg.VDS4Gamepad()
        else:
            gp = vg.VX360Gamepad()
    except Exception as e:        # noqa: BLE001
        log.warning(f"VirtualPad create ({pad_type}) failed: {e}")
        return None

    global _X360_BUTTON_MAP, _DS4_BUTTON_MAP, _DS4_DPAD_MAP
    if pad_type == PAD_TYPE_X360:
        if _X360_BUTTON_MAP is None:
            _X360_BUTTON_MAP = _build_x360_button_map(vg)
        return VirtualPad(vg, gp, PAD_TYPE_X360, _X360_BUTTON_MAP)
    else:
        if _DS4_BUTTON_MAP is None:
            _DS4_BUTTON_MAP = _build_ds4_button_map(vg)
        if _DS4_DPAD_MAP is None:
            _DS4_DPAD_MAP = _build_ds4_dpad_map(vg)
        return VirtualPad(vg, gp, PAD_TYPE_DS4,
                          _DS4_BUTTON_MAP, _DS4_DPAD_MAP)


# ────────────────────────────────────────────────────────────────────
# Auto-install (download latest from GitHub + run silently)
# ────────────────────────────────────────────────────────────────────

_install_status = {
    "in_progress":  False,
    "phase":        "idle",
    "progress_pct": 0,
    "msg":          "",
    "err":          None,
    "installed":    False,
}


def get_install_progress() -> dict:
    return dict(_install_status)


def download_and_install(timeout_sec: int = 180) -> dict:
    """Download the latest ViGEmBus installer .exe / .msi from GitHub
    and run it silently.  Idempotent."""
    import os, subprocess, tempfile, time as _t
    if _install_status.get("in_progress"):
        return {"ok": False, "err": "Install already in progress"}
    if is_available().get("available"):
        _install_status.update({"phase": "done", "installed": True,
                                "msg": "Already installed",
                                "in_progress": False})
        return {"ok": True, "msg": "ViGEmBus already installed"}

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
        api = "https://api.github.com/repos/nefarius/ViGEmBus/releases/latest"
        req = urllib.request.Request(api, headers={
            "User-Agent": "GhostShell/3.3 ViGEmBus-installer",
            "Accept":     "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            manifest = _json.load(resp)

        assets = manifest.get("assets") or []
        # Prefer .msi (msiexec silent flags are bulletproof; the .exe is
        # an InstallShield wrapper that rejects unknown silent flags).
        exe_asset = next((a for a in assets
                          if a.get("name", "").lower().endswith(".msi")), None)
        if not exe_asset:
            exe_asset = next((a for a in assets
                              if a.get("name", "").lower().endswith(".exe")), None)
        if not exe_asset:
            raise RuntimeError("Could not find ViGEmBus installer in latest release")
        url = exe_asset["browser_download_url"]
        fname = exe_asset["name"]
        size = int(exe_asset.get("size") or 0)

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

        _install_status.update({
            "phase":        "installing",
            "progress_pct": 100,
            "msg":          "Running silent install…",
        })
        # Launch installer.  Same approach as HidHide — let the user click
        # through the GUI rather than fight framework-specific silent flags.
        _install_status.update({
            "phase":        "installing",
            "progress_pct": 100,
            "msg":          ("Installer window opened — click through it. "
                             "GhostShell will detect when it's done."),
        })
        if fname.lower().endswith(".msi"):
            cmd = ["msiexec", "/i", out_path, "/qb", "/norestart"]
        else:
            cmd = [out_path]
        try:
            proc = subprocess.Popen(cmd, creationflags=0x00000010)
        except Exception as e:                      # noqa: BLE001
            raise RuntimeError(f"Could not launch installer: {e}")

        # Background-poll for the driver to come online (up to 5 min)
        deadline = _t.time() + 300
        while _t.time() < deadline:
            avail = is_available()
            if avail.get("available"):
                _install_status.update({
                    "phase":       "done",
                    "msg":         "Installed and ready",
                    "installed":   True,
                    "in_progress": False,
                })
                log.info("ViGEmBus install complete")
                return {"ok": True}
            _t.sleep(2.0)

        _install_status.update({
            "phase":       "done",
            "msg":         "Installer timed out (5 min) — try again or install manually",
            "installed":   False,
            "in_progress": False,
        })
        return {"ok": False, "err": "install poll timeout"}
    except Exception as e:                 # noqa: BLE001
        log.warning(f"ViGEmBus install failed: {e}")
        _install_status.update({
            "phase":       "error",
            "err":         str(e),
            "msg":         f"Install failed: {e}",
            "in_progress": False,
        })
        return {"ok": False, "err": str(e)}
