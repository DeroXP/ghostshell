"""NVIDIA driver settings — the store behind "Manage 3D settings" in the NVIDIA
Control Panel — through NVAPI's DRS calls, on the global (base) profile.

These are the settings that change frame pacing and input lag on a GeForce
card.  The GPU tab used to write registry values instead (PerfLevelSrc,
PowerMizer…) that current drivers mostly ignore for this, and its "shader
cache 10 GB" value did not even fit the registry type it was written as.

Each setting's previous value — or the fact that it was not set — is noted
first (core/reg_originals.py, kind "nvidia-drs", scope "gpu"), so the GPU
tab's Undo puts back exactly that.  Every write is read back before it is
reported as done.  Only the settings listed in SETTINGS are ever touched.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, c_int, c_uint16, c_uint32, c_uint8, c_void_p
from typing import Optional

from core import reg_originals
from core.utils import get_logger

log = get_logger("nvidia_profile")

_KIND = "nvidia-drs"
_SCOPE = "gpu"

POWER_MODE = 0x1057EB71         # "Power management mode": 1 = prefer maximum performance
PRERENDER = 0x007BA09E          # "Maximum pre-rendered frames": 1 = Low Latency Mode "On"
SHADER_CACHE = 0x00AC8497       # "Shader disk cache maximum size", MB

# id, value, how it reads in the status, raise_only (never lower a larger value)
SETTINGS = (
    (POWER_MODE, 1, "Power management mode -> Prefer maximum performance", False),
    (PRERENDER, 1, "Low Latency Mode -> On", False),
    (SHADER_CACHE, 10240, "Shader cache size -> at least 10 GB", True),
)
_KNOWN = {f"{sid:#010x}" for sid, *_ in SETTINGS}

_OK = 0
_SETTING_NOT_FOUND = -160
_CURRENT_PROFILE = 0            # NVDRS_CURRENT_PROFILE_LOCATION: set in this profile itself


class _Binary(ctypes.Structure):
    _fields_ = [("valueLength", c_uint32), ("valueData", c_uint8 * 4096)]


class _Value(ctypes.Union):
    _fields_ = [("u32", c_uint32), ("binary", _Binary), ("wsz", c_uint16 * 2048)]


class _Setting(ctypes.Structure):          # NVDRS_SETTING_V1
    _fields_ = [("version", c_uint32), ("settingName", c_uint16 * 2048), ("settingId", c_uint32),
                ("settingType", c_int), ("settingLocation", c_int), ("isCurrentPredefined", c_uint32),
                ("isPredefinedValid", c_uint32), ("predefined", _Value), ("current", _Value)]


_VERSION = ctypes.sizeof(_Setting) | (1 << 16)


class _Session:
    """One DRS session on the base profile.  Use as a context manager."""

    def __init__(self, fns: dict):
        self.f = fns
        self.session = c_void_p()
        self.profile = c_void_p()
        if self.f["create"](ctypes.byref(self.session)) != _OK:
            raise OSError("NvAPI_DRS_CreateSession failed")
        if self.f["load"](self.session) != _OK or self.f["base"](self.session, ctypes.byref(self.profile)) != _OK:
            self.close()
            raise OSError("could not load the NVIDIA driver settings")

    def get(self, sid: int) -> Optional[int]:
        """The value set in the global profile, or None if it is not set there."""
        st = _Setting()
        st.version = _VERSION
        rc = self.f["get"](self.session, self.profile, sid, ctypes.byref(st))
        if rc == _SETTING_NOT_FOUND or (rc == _OK and st.settingLocation != _CURRENT_PROFILE):
            return None
        if rc != _OK:
            raise OSError(f"NvAPI_DRS_GetSetting({sid:#x}) returned {rc}")
        return int(st.current.u32)

    def set(self, sid: int, value: int) -> bool:
        st = _Setting()
        st.version = _VERSION
        st.settingId = sid
        st.settingType = 0                     # NVDRS_DWORD_TYPE
        st.current.u32 = int(value)
        return self.f["set"](self.session, self.profile, ctypes.byref(st)) == _OK

    def delete(self, sid: int) -> bool:
        rc = self.f["delete"](self.session, self.profile, sid)
        return rc in (_OK, _SETTING_NOT_FOUND)

    def save(self) -> bool:
        return self.f["save"](self.session) == _OK

    def close(self) -> None:
        if self.session:
            try:
                self.f["destroy"](self.session)
            except Exception:
                pass
            self.session = c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


_fns: Optional[dict] = None


def _open() -> Optional[_Session]:
    """A session on the driver's settings, or None without an NVIDIA driver."""
    global _fns
    if _fns is None:
        try:
            dll = ctypes.WinDLL("nvapi64")
            qi = dll.nvapi_QueryInterface
            qi.restype, qi.argtypes = c_void_p, [c_uint32]

            def fn(fid, *args):
                addr = qi(fid)
                if not addr:
                    raise OSError(f"NVAPI function {fid:#x} not available")
                return ctypes.CFUNCTYPE(c_int, *args)(addr)

            if fn(0x0150E828)() != _OK:                         # NvAPI_Initialize
                raise OSError("NvAPI_Initialize failed")
            _fns = {
                "create": fn(0x0694D52E, POINTER(c_void_p)),
                "load": fn(0x375DBD6B, c_void_p),
                "base": fn(0xDA8466A0, c_void_p, POINTER(c_void_p)),
                "get": fn(0x73BF8338, c_void_p, c_void_p, c_uint32, POINTER(_Setting)),
                "set": fn(0x577DD202, c_void_p, c_void_p, POINTER(_Setting)),
                "delete": fn(0xE4A26362, c_void_p, c_void_p, c_uint32),
                "save": fn(0xFCBC7E14, c_void_p),
                "destroy": fn(0xDAD9CFF8, c_void_p),
            }
        except (OSError, AttributeError) as e:
            log.debug(f"NVIDIA driver settings unavailable: {e}")
            _fns = {}
    if not _fns:
        return None
    try:
        return _Session(_fns)
    except OSError as e:
        log.warning(f"NVIDIA driver settings: {e}")
        return None


def apply() -> list:
    """Set SETTINGS on the global profile.  Returns result rows {name, ok}."""
    s = _open()
    if s is None:
        return [{"name": "NVIDIA driver settings: the driver's settings store could not be opened", "ok": False}]
    rows = []
    with s:
        planned = []
        for sid, value, text, raise_only in SETTINGS:
            try:
                cur = s.get(sid)
            except OSError as e:
                rows.append({"name": f"{text}: could not be read ({e})", "ok": False})
                continue
            if cur == value or (raise_only and cur is not None and cur >= value):
                rows.append({"name": text + " (already)", "ok": True})
                continue
            reg_originals.remember_state(_KIND, f"{sid:#010x}", cur, _SCOPE)
            planned.append((sid, value, text, s.set(sid, value)))
        saved = s.save() if planned else True
        for sid, value, text, set_ok in planned:
            try:
                ok = set_ok and saved and s.get(sid) == value
            except OSError:
                ok = False
            rows.append({"name": text if ok else f"{text}: the driver did not keep the change", "ok": ok})
    for r in rows:
        log.info(f"  {'✓' if r['ok'] else '✗'} {r['name']}")
    return rows


def restore() -> dict:
    """Put each recorded setting back (or remove it again, if it was not set).
    A record that cannot be put back stays, so the next Undo tries again."""
    records = reg_originals.state_records(_KIND, _SCOPE)
    if not records:
        return {"restored": 0, "failed": 0}
    s = _open()
    if s is None:
        return {"restored": 0, "failed": len(records)}
    done = failed = 0
    with s:
        changes = []
        for name, prior in records.items():
            if name.lower() not in _KNOWN:          # not a setting this app changes: drop the record
                log.warning(f"dropping an NVIDIA-settings record for {name!r}: not one this app changes")
                reg_originals.forget_state(_KIND, name, _SCOPE)
                continue
            sid = int(name, 16)
            ok = s.delete(sid) if prior is None else (isinstance(prior, int) and s.set(sid, prior))
            changes.append((name, sid, prior, ok))
        saved = s.save() if changes else True
        for name, sid, prior, ok in changes:
            try:
                ok = ok and saved and s.get(sid) == prior
            except OSError:
                ok = False
            if ok:
                reg_originals.forget_state(_KIND, name, _SCOPE)
                done += 1
            else:
                failed += 1
    return {"restored": done, "failed": failed}
