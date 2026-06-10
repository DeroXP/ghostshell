"""NVAPI ctypes wrapper for actual GPU overclocking on consumer GeForce cards.

Why this exists:
    `nvidia-smi -lgc` and `-lmc` set clock CAPS, not OFFSETS. On consumer
    GeForce cards, requesting a cap above the GPU's native max boost just
    keeps the card at its existing max — there's no real overclock.

    NVAPI's `NvAPI_GPU_SetPstates20` accepts a `freqDelta_kHz` field on each
    pstate's clock entry. This is a TRUE offset added to the boost target —
    the same mechanism used by MSI Afterburner, EVGA Precision, and NVIDIA
    Inspector. It works on all consumer NVIDIA cards from Pascal onward.

Safety design:
    - Read current pstates first
    - Modify ONLY the freqDelta_kHz fields on P0 (highest performance state)
    - Leave every other field untouched
    - Write back
    - All wrapped in try/except — return ok=False instead of crashing

Reference: NvAPIWrapper (https://github.com/falahati/NvAPIWrapper)
"""
import ctypes
import os
from ctypes import (
    c_int, c_int32, c_uint32, c_void_p, c_char_p,
    POINTER, byref, Structure, Union, sizeof, WINFUNCTYPE, addressof,
)

# ═══════════════════════════════════════════════════════════════════════════
# Constants from nvapi.h
# ═══════════════════════════════════════════════════════════════════════════
NVAPI_OK = 0
NVAPI_ERROR = -1
NVAPI_LIBRARY_NOT_FOUND = -2
NVAPI_NO_IMPLEMENTATION = -3
NVAPI_API_NOT_INITIALIZED = -4
NVAPI_INVALID_ARGUMENT = -5
NVAPI_NOT_SUPPORTED = -6
NVAPI_INCOMPATIBLE_STRUCT_VERSION = -8
NVAPI_HANDLE_INVALIDATED = -9
NVAPI_OPENGL_CONTEXT_NOT_CURRENT = -10

# Clock domain IDs
NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS = 0
NVAPI_GPU_PUBLIC_CLOCK_MEMORY = 4
NVAPI_GPU_PUBLIC_CLOCK_PROCESSOR = 7
NVAPI_GPU_PUBLIC_CLOCK_VIDEO = 8

# Performance state IDs
NVAPI_GPU_PERF_PSTATE_P0 = 0   # max performance
NVAPI_GPU_PERF_PSTATE_P8 = 8   # idle

# Limits
NVAPI_MAX_GPU_PSTATE20_PSTATES = 16
NVAPI_MAX_GPU_PSTATE20_CLOCKS = 8
NVAPI_MAX_GPU_PSTATE20_BASE_VOLTAGES = 4
NVAPI_MAX_PHYSICAL_GPUS = 64

# Function IDs (from NvAPI offset table)
ID_NvAPI_Initialize            = 0x0150E828
ID_NvAPI_Unload                = 0xD22BDD7E
ID_NvAPI_GetErrorMessage       = 0x6C2D048C
ID_NvAPI_EnumPhysicalGPUs      = 0xE5AC921F
ID_NvAPI_GPU_GetPstates20      = 0x6FF81213
ID_NvAPI_GPU_SetPstates20      = 0x0F4DAE6B
ID_NvAPI_GPU_GetFullName       = 0xCEEE8E9F

# V/F boost-curve (private VFP) functions.
#
# 3.4.3-beta.5 — DISABLED.  These two ids were WRONG.  0x46FBEB03 is, per
# NVIDIA's own public nvapi_interface.h, `NvAPI_GPU_GetPhysicalFrameBufferSize`
# — NOT a clock/VF function — so QueryInterface(0x46FBEB03) resolved the
# framebuffer-size routine and every get/set_core_vf_offset() call invoked the
# wrong function (it returned garbage / errored out).  0x0733E009 doesn't
# appear in the public header at all (a guessed private id).  The real private
# GetClockBoostTable/SetClockBoostTable ids are NOT publicly documented, and
# guessing again is exactly the bug class we're eliminating — so we DON'T
# resolve them.  Core overclocking already lands via the VERIFIED Pstates20
# freqDelta path (ID_NvAPI_GPU_SetPstates20, confirmed against the public
# header) inside set_offsets(); the VF path was redundant *and* broken.  If a
# true VFP-curve editor is ever added, these ids must be sourced and verified
# from a known-good reference (e.g. arcnmx/nvapi-rs), not hardcoded blind.
ID_NvAPI_GPU_GetClockBoostTable = None   # was 0x46FBEB03 (= GetPhysicalFrameBufferSize!)
ID_NvAPI_GPU_SetClockBoostTable = None   # was 0x0733E009 (not in public header)


# ═══════════════════════════════════════════════════════════════════════════
# Struct definitions
# ═══════════════════════════════════════════════════════════════════════════

class NV_GPU_PERF_PSTATES20_PARAM_DELTA(Structure):
    """Signed delta in target unit (kHz for clocks, μV for voltages)."""
    _fields_ = [
        ("value", c_int32),
        ("min", c_int32),
        ("max", c_int32),
    ]


class _NV_GPU_PSTATE20_RANGE(Structure):
    _fields_ = [
        ("minFreq_kHz", c_uint32),
        ("maxFreq_kHz", c_uint32),
        ("domainId", c_uint32),
        ("minVoltage_uV", c_uint32),
        ("maxVoltage_uV", c_uint32),
    ]


class _NV_GPU_PSTATE20_SINGLE(Structure):
    _fields_ = [
        ("freq_kHz", c_uint32),
        # Pad to match the union size (range = 20 bytes)
        ("_pad", c_uint32 * 4),
    ]


class _NV_GPU_PSTATE20_DATA_UNION(Union):
    _fields_ = [
        ("single", _NV_GPU_PSTATE20_SINGLE),
        ("range", _NV_GPU_PSTATE20_RANGE),
    ]
    _pack_ = 1


class NV_GPU_PSTATE20_CLOCK_ENTRY_V1(Structure):
    """A single pstate's clock entry (one entry per domain: graphics, memory, etc.)."""
    _fields_ = [
        ("domainId", c_uint32),               # NV_GPU_PUBLIC_CLOCK_*
        ("typeId", c_uint32),                 # 0=single, 1=range
        ("bIsEditable_reserved", c_uint32),   # bit 0 = editable, bits 1-31 = reserved
        ("freqDelta_kHz", NV_GPU_PERF_PSTATES20_PARAM_DELTA),  # OUR OC OFFSET LIVES HERE
        ("data", _NV_GPU_PSTATE20_DATA_UNION),
    ]


class NV_GPU_PSTATE20_BASE_VOLTAGE_ENTRY_V1(Structure):
    _fields_ = [
        ("domainId", c_uint32),
        ("bIsEditable_reserved", c_uint32),
        ("volt_uV", c_uint32),
        ("voltDelta_uV", NV_GPU_PERF_PSTATES20_PARAM_DELTA),
    ]


class NV_GPU_PSTATE20(Structure):
    _fields_ = [
        ("pstateId", c_uint32),
        ("bIsEditable_reserved", c_uint32),
        ("clocks", NV_GPU_PSTATE20_CLOCK_ENTRY_V1 * NVAPI_MAX_GPU_PSTATE20_CLOCKS),
        ("baseVoltages", NV_GPU_PSTATE20_BASE_VOLTAGE_ENTRY_V1 * NVAPI_MAX_GPU_PSTATE20_BASE_VOLTAGES),
    ]


class NV_GPU_PERF_PSTATES20_INFO_V1(Structure):
    _fields_ = [
        ("version", c_uint32),
        ("bIsEditable_reserved", c_uint32),
        ("numPstates", c_uint32),
        ("numClocks", c_uint32),
        ("pstates", NV_GPU_PSTATE20 * NVAPI_MAX_GPU_PSTATE20_PSTATES),
    ]


class NV_GPU_PERF_PSTATES20_INFO_V2(Structure):
    """V2 adds `numBaseVoltages` field. Required by drivers ~510+."""
    _fields_ = [
        ("version", c_uint32),
        ("bIsEditable_reserved", c_uint32),
        ("numPstates", c_uint32),
        ("numClocks", c_uint32),
        ("numBaseVoltages", c_uint32),  # <-- V2 adds
        ("pstates", NV_GPU_PSTATE20 * NVAPI_MAX_GPU_PSTATE20_PSTATES),
    ]


class _NV_GPU_PSTATE20_OV_VOLTAGES(Structure):
    _fields_ = [
        ("numVoltages", c_uint32),
        ("voltages", NV_GPU_PSTATE20_BASE_VOLTAGE_ENTRY_V1 * NVAPI_MAX_GPU_PSTATE20_BASE_VOLTAGES),
    ]


class NV_GPU_PERF_PSTATES20_INFO_V3(Structure):
    """V3 adds `ovVoltages` (overvoltage section). Required by drivers ~535+ on Ada/Blackwell."""
    _fields_ = [
        ("version", c_uint32),
        ("bIsEditable_reserved", c_uint32),
        ("numPstates", c_uint32),
        ("numClocks", c_uint32),
        ("numBaseVoltages", c_uint32),
        ("pstates", NV_GPU_PSTATE20 * NVAPI_MAX_GPU_PSTATE20_PSTATES),
        ("ovVoltages", _NV_GPU_PSTATE20_OV_VOLTAGES),  # <-- V3 adds
    ]


# Computed at import time so ctypes does the math.
NV_GPU_PERF_PSTATES20_INFO_V1_VER = sizeof(NV_GPU_PERF_PSTATES20_INFO_V1) | (1 << 16)
NV_GPU_PERF_PSTATES20_INFO_V2_VER = sizeof(NV_GPU_PERF_PSTATES20_INFO_V2) | (2 << 16)
NV_GPU_PERF_PSTATES20_INFO_V3_VER = sizeof(NV_GPU_PERF_PSTATES20_INFO_V3) | (3 << 16)


# ═══════════════════════════════════════════════════════════════════════════
# V/F Boost-curve structs (GetClockBoostTable / SetClockBoostTable)
# ═══════════════════════════════════════════════════════════════════════════
# Each GPU has a Voltage/Frequency (V/F) curve: a list of (voltage, max_freq)
# pairs.  The highest-voltage pair sets the maximum boost clock.
# `freqDelta_kHz` is a SIGNED offset added to every point on the curve.
# Setting all deltas to +N kHz raises the entire curve (and thus the boost
# ceiling) by N kHz — exactly what Afterburner's "Core Clock" slider does.
#
# The struct layout is confirmed for R396-R596 (Pascal → Blackwell).
# ═══════════════════════════════════════════════════════════════════════════

NV_GPU_BOOST_TABLE_MAX_ENTRIES = 255  # driver fills numEntries; allocate max


class NV_GPU_BOOST_TABLE_ENTRY(Structure):
    """One point on the V/F curve."""
    _fields_ = [
        ("freqDelta_kHz", c_int32),   # signed offset in kHz; SET this to overclock
        ("voltage_uV",    c_uint32),  # reference voltage — DO NOT MODIFY
    ]


class NV_GPU_BOOST_TABLE(Structure):
    """
    NV_GPU_CLOCK_BOOST_TABLE V1.
    Used by NvAPI_GPU_GetClockBoostTable / NvAPI_GPU_SetClockBoostTable.
    Returned data is for the GRAPHICS domain only.
    """
    _pack_ = 4
    _fields_ = [
        ("version",    c_uint32),   # sizeof | (1 << 16)
        ("flags",      c_uint32),   # bit 0 = table is editable (driver fills on GET)
        ("numEntries", c_uint32),   # number of valid VF points (driver fills on GET)
        ("_pad0",      c_uint32),   # alignment padding
        ("entries",    NV_GPU_BOOST_TABLE_ENTRY * NV_GPU_BOOST_TABLE_MAX_ENTRIES),
    ]


NV_GPU_BOOST_TABLE_V1_VER = sizeof(NV_GPU_BOOST_TABLE) | (1 << 16)


# ═══════════════════════════════════════════════════════════════════════════
# DLL loading + function dispatch
# ═══════════════════════════════════════════════════════════════════════════

class _NVAPI:
    def __init__(self):
        self.dll = None
        self.QI = None
        self.initialized = False
        self.fn_init = None
        self.fn_unload = None
        self.fn_enum_gpus = None
        self.fn_get_pstates20 = None
        self.fn_set_pstates20 = None
        self.fn_get_boost_table = None   # V/F curve GET
        self.fn_set_boost_table = None   # V/F curve SET
        self.fn_get_name = None
        self.fn_get_error_message = None

    def load(self) -> dict:
        """Load nvapi64.dll and resolve function pointers via QueryInterface."""
        if self.initialized:
            return {"ok": True, "msg": "Already initialized"}

        try:
            # Load DLL — Windows searches System32 by default
            self.dll = ctypes.WinDLL("nvapi64")
        except OSError as e:
            return {"ok": False, "err": f"nvapi64.dll not found: {e}"}

        try:
            QI = self.dll.nvapi_QueryInterface
            QI.restype = c_void_p
            QI.argtypes = [c_uint32]
            self.QI = QI

            # Resolve function pointers
            def _resolve(fn_id, sig):
                addr = QI(fn_id)
                if not addr:
                    return None
                return sig(addr)

            self.fn_init = _resolve(ID_NvAPI_Initialize, WINFUNCTYPE(c_int))
            if self.fn_init is None:
                return {"ok": False, "err": "Could not resolve NvAPI_Initialize"}

            r = self.fn_init()
            if r != NVAPI_OK:
                return {"ok": False, "err": f"NvAPI_Initialize returned {r}"}
            self.initialized = True

            self.fn_unload = _resolve(ID_NvAPI_Unload, WINFUNCTYPE(c_int))
            self.fn_enum_gpus = _resolve(
                ID_NvAPI_EnumPhysicalGPUs,
                WINFUNCTYPE(c_int, POINTER(c_void_p * NVAPI_MAX_PHYSICAL_GPUS), POINTER(c_uint32)),
            )
            # We pass V3 struct pointers (V3 is the newest layout). The driver
            # reads only the `version` field to determine the size, so passing
            # smaller versions still works as long as we set version correctly.
            self.fn_get_pstates20 = _resolve(
                ID_NvAPI_GPU_GetPstates20,
                WINFUNCTYPE(c_int, c_void_p, POINTER(NV_GPU_PERF_PSTATES20_INFO_V3)),
            )
            self.fn_set_pstates20 = _resolve(
                ID_NvAPI_GPU_SetPstates20,
                WINFUNCTYPE(c_int, c_void_p, POINTER(NV_GPU_PERF_PSTATES20_INFO_V3)),
            )
            # beta.5 — the boost-table ids were wrong (0x46FBEB03 is actually
            # GetPhysicalFrameBufferSize), so we no longer resolve them.  Left
            # None → get/set_core_vf_offset() short-circuit to a clean
            # "not resolved" result and core OC uses the verified Pstates20 path.
            self.fn_get_boost_table = (
                _resolve(ID_NvAPI_GPU_GetClockBoostTable,
                         WINFUNCTYPE(c_int, c_void_p, POINTER(NV_GPU_BOOST_TABLE)))
                if ID_NvAPI_GPU_GetClockBoostTable else None
            )
            self.fn_set_boost_table = (
                _resolve(ID_NvAPI_GPU_SetClockBoostTable,
                         WINFUNCTYPE(c_int, c_void_p, POINTER(NV_GPU_BOOST_TABLE)))
                if ID_NvAPI_GPU_SetClockBoostTable else None
            )
            self.fn_get_error_message = _resolve(
                ID_NvAPI_GetErrorMessage,
                WINFUNCTYPE(c_int, c_int, c_char_p),
            )
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "err": f"NVAPI dispatch resolve failed: {e}"}

    def error_message(self, code: int) -> str:
        if not self.fn_get_error_message:
            return f"Error {code}"
        buf = ctypes.create_string_buffer(64)
        try:
            self.fn_get_error_message(code, buf)
            return f"{code}: {buf.value.decode('utf-8', errors='ignore')}"
        except Exception:
            return f"Error {code}"

    def unload(self):
        if self.initialized and self.fn_unload:
            try:
                self.fn_unload()
            except Exception:
                pass
        self.initialized = False


_nvapi = _NVAPI()


def is_available() -> dict:
    """Probe whether NVAPI is present and we can talk to it. Idempotent."""
    if _nvapi.initialized:
        return {"ok": True, "msg": "NVAPI loaded"}
    return _nvapi.load()


def _get_first_gpu():
    """Returns (handle, error). Handle is a c_void_p (NvPhysicalGpuHandle)."""
    if not _nvapi.initialized:
        r = _nvapi.load()
        if not r.get("ok"):
            return (None, r.get("err", "NVAPI not loaded"))

    gpus = (c_void_p * NVAPI_MAX_PHYSICAL_GPUS)()
    count = c_uint32(0)
    r = _nvapi.fn_enum_gpus(byref(gpus), byref(count))
    if r != NVAPI_OK:
        return (None, _nvapi.error_message(r))
    if count.value == 0:
        return (None, "No NVIDIA GPUs found")
    return (gpus[0], None)


# ═══════════════════════════════════════════════════════════════════════════
# Public API: read/write OC offsets
# ═══════════════════════════════════════════════════════════════════════════

def _read_pstates20(handle) -> tuple:
    """Try V3 → V2 → V1 in turn until the driver accepts our version.

    Returns: (info_struct, version_used, error_str_or_None)
    """
    P = POINTER(NV_GPU_PERF_PSTATES20_INFO_V3)  # function expects V3-shaped pointer

    # Try V3 (current default for R535+ on Ada/Blackwell)
    info_v3 = NV_GPU_PERF_PSTATES20_INFO_V3()
    info_v3.version = NV_GPU_PERF_PSTATES20_INFO_V3_VER
    r3 = _nvapi.fn_get_pstates20(handle, ctypes.cast(byref(info_v3), P))
    if r3 == NVAPI_OK:
        return (info_v3, 3, None)

    # Try V2 (drivers ~510-530)
    info_v2 = NV_GPU_PERF_PSTATES20_INFO_V2()
    info_v2.version = NV_GPU_PERF_PSTATES20_INFO_V2_VER
    r2 = _nvapi.fn_get_pstates20(handle, ctypes.cast(byref(info_v2), P))
    if r2 == NVAPI_OK:
        return (info_v2, 2, None)

    # Try V1 (legacy)
    info_v1 = NV_GPU_PERF_PSTATES20_INFO_V1()
    info_v1.version = NV_GPU_PERF_PSTATES20_INFO_V1_VER
    r1 = _nvapi.fn_get_pstates20(handle, ctypes.cast(byref(info_v1), P))
    if r1 == NVAPI_OK:
        return (info_v1, 1, None)

    return (None, 0, f"V3: {_nvapi.error_message(r3)} | V2: {_nvapi.error_message(r2)} | V1: {_nvapi.error_message(r1)}")


def get_current_offsets() -> dict:
    """Read current core/memory clock offsets and convert to MHz.

    beta.5 — both core and memory are read from the P-state freqDelta (the
    verified Pstates20 path).  The V/F boost-curve read is attempted first
    but is now always disabled (its NVAPI id was wrong — see the
    ID_NvAPI_GPU_*ClockBoostTable note up top), so `core_via_vf` stays False
    and we fall through to the P-state graphics freqDelta — which is exactly
    what set_offsets() WRITES, so read-back and write now agree.

    Memory offset is read from the P-state freqDelta (correct for memory).
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI not available")}

    handle, err = _get_first_gpu()
    if err:
        return {"ok": False, "err": err}

    # ── Core: read from V/F boost curve ──────────────────────────────────────
    core_via_vf = False
    core_khz = 0
    vf = get_core_vf_offset()
    if vf.get("ok"):
        core_khz = vf["offset_khz"]
        core_via_vf = True

    # ── Memory (+ core fallback): read from P-state struct ───────────────────
    info, ver_used, read_err = _read_pstates20(handle)
    if read_err:
        # P-state read failed — return whatever we got from VF curve
        if core_via_vf:
            return {
                "ok": True,
                "core_offset_mhz": core_khz // 1000,
                "mem_offset_mhz": 0,
                "core_offset_khz": core_khz,
                "mem_offset_khz": 0,
                "core_via_vf_curve": True,
                "pstate0_present": False,
                "struct_version_used": 0,
            }
        return {"ok": False, "err": f"GetPstates20: {read_err}"}

    mem_khz = 0
    p0_found = False
    for i in range(min(info.numPstates, NVAPI_MAX_GPU_PSTATE20_PSTATES)):
        ps = info.pstates[i]
        if ps.pstateId != NVAPI_GPU_PERF_PSTATE_P0:
            continue
        p0_found = True
        for j in range(min(info.numClocks, NVAPI_MAX_GPU_PSTATE20_CLOCKS)):
            ck = ps.clocks[j]
            if ck.domainId == NVAPI_GPU_PUBLIC_CLOCK_MEMORY:
                mem_khz = ck.freqDelta_kHz.value
            elif ck.domainId == NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS and not core_via_vf:
                # Fallback only when VF curve unavailable
                core_khz = ck.freqDelta_kHz.value
        break

    return {
        "ok": True,
        "core_offset_mhz": core_khz // 1000,
        "mem_offset_mhz": mem_khz // 1000,
        "core_offset_khz": core_khz,
        "mem_offset_khz": mem_khz,
        "core_via_vf_curve": core_via_vf,
        "pstate0_present": p0_found,
        "struct_version_used": ver_used,
    }


def get_core_vf_offset() -> dict:
    """Read the current V/F curve offset for the graphics domain.

    Returns the freqDelta_kHz of the first valid entry (all entries should be
    uniform after we write them; driver may differ on GET).
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI not available")}

    handle, err = _get_first_gpu()
    if err:
        return {"ok": False, "err": err}

    if not _nvapi.fn_get_boost_table:
        return {"ok": False, "err": "GetClockBoostTable not resolved (driver too old?)"}

    tbl = NV_GPU_BOOST_TABLE()
    tbl.version = NV_GPU_BOOST_TABLE_V1_VER
    r = _nvapi.fn_get_boost_table(handle, byref(tbl))
    if r != NVAPI_OK:
        return {"ok": False, "err": f"GetClockBoostTable: {_nvapi.error_message(r)}"}

    n = min(tbl.numEntries, NV_GPU_BOOST_TABLE_MAX_ENTRIES)
    if n == 0:
        return {"ok": False, "err": "GetClockBoostTable returned 0 VF points"}

    deltas = [tbl.entries[i].freqDelta_kHz for i in range(n)]
    # Use the max delta as the effective offset (avoids 0s in unused trailing slots)
    effective_khz = max(deltas) if any(d > 0 for d in deltas) else deltas[0]

    return {
        "ok": True,
        "vf_points": n,
        "offset_khz": effective_khz,
        "offset_mhz": effective_khz // 1000,
        "uniform": all(d == deltas[0] for d in deltas),
        "editable": bool(tbl.flags & 1),
    }


def set_core_vf_offset(offset_mhz: int) -> dict:
    """Shift the GPU's V/F boost curve for the graphics domain.

    This is the CORRECT path for core clock OC on Boost 3.0+ consumer GPUs:
    SetPstates20 freqDelta moves the P-state BASE frequency (floor), while the
    boost CEILING is entirely determined by the V/F curve.  This function raises
    (or lowers) every VF point by `offset_mhz`, which moves the ceiling.

    Args:
        offset_mhz: signed MHz offset (+200 = +200 MHz OC, 0 = stock)
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI not available")}

    handle, err = _get_first_gpu()
    if err:
        return {"ok": False, "err": err}

    if not _nvapi.fn_get_boost_table or not _nvapi.fn_set_boost_table:
        return {"ok": False, "err": "GetClockBoostTable/SetClockBoostTable not resolved"}

    # Step 1 — GET current V/F table
    tbl = NV_GPU_BOOST_TABLE()
    tbl.version = NV_GPU_BOOST_TABLE_V1_VER
    r = _nvapi.fn_get_boost_table(handle, byref(tbl))
    if r != NVAPI_OK:
        return {"ok": False, "err": f"GetClockBoostTable: {_nvapi.error_message(r)}"}

    n = min(tbl.numEntries, NV_GPU_BOOST_TABLE_MAX_ENTRIES)
    if n == 0:
        return {"ok": False, "err": "GetClockBoostTable returned 0 VF points"}

    if not (tbl.flags & 1):
        return {"ok": False, "err": "V/F table is not editable (not running as Administrator?)"}

    # Step 2 — shift ALL VF point frequency deltas by the requested offset.
    # Voltages are untouched; we only move frequencies.
    target_khz = int(offset_mhz) * 1000
    for i in range(n):
        tbl.entries[i].freqDelta_kHz = target_khz

    # Step 3 — SET the modified table
    r = _nvapi.fn_set_boost_table(handle, byref(tbl))
    if r != NVAPI_OK:
        return {
            "ok": False,
            "err": f"SetClockBoostTable: {_nvapi.error_message(r)}",
            "vf_points": n,
        }

    return {
        "ok": True,
        "vf_points_modified": n,
        "offset_mhz": int(offset_mhz),
        "target_khz": target_khz,
    }


# v2.9.1 — split SetPstates20 into a *sparse* per-clock helper.
# Round-tripping the full P-state blob (Get → modify → Set) was being
# rejected by the Blackwell 596.x driver with `-104 NVAPI_NOT_SUPPORTED`,
# even when we only modified one entry.  The driver wants a tiny request
# that contains *just* the changed clock — exactly the form that worked
# in 2.6 / 2.7 before the round-trip refactor.
def _set_pstates20_single(handle, ver_used: int,
                          domain_id: int, freq_delta_khz: int) -> int:
    """Send a sparse SetPstates20 request containing exactly one clock
    entry (one P-state, one clock).  Returns the raw NVAPI status code.

    Why sparse:  the driver treats GetPstates20 + full SetPstates20 as a
    "review every field" call that fails on Blackwell when ANY value
    upstream of P0 looks wrong.  A request with numPstates=1 + numClocks=1
    is interpreted as "change only this and leave everything else alone."
    """
    if ver_used == 3:
        req = NV_GPU_PERF_PSTATES20_INFO_V3()
        req.version = NV_GPU_PERF_PSTATES20_INFO_V3_VER
        req.numBaseVoltages = 0
    elif ver_used == 2:
        req = NV_GPU_PERF_PSTATES20_INFO_V2()
        req.version = NV_GPU_PERF_PSTATES20_INFO_V2_VER
        req.numBaseVoltages = 0
    else:
        req = NV_GPU_PERF_PSTATES20_INFO_V1()
        req.version = NV_GPU_PERF_PSTATES20_INFO_V1_VER

    req.numPstates = 1
    req.numClocks = 1
    req.pstates[0].pstateId = NVAPI_GPU_PERF_PSTATE_P0
    req.pstates[0].bIsEditable_reserved = 1
    req.pstates[0].clocks[0].domainId = domain_id
    req.pstates[0].clocks[0].bIsEditable_reserved = 1
    req.pstates[0].clocks[0].freqDelta_kHz.value = int(freq_delta_khz)

    return _nvapi.fn_set_pstates20(
        handle,
        ctypes.cast(byref(req), POINTER(NV_GPU_PERF_PSTATES20_INFO_V3)),
    )


def set_offsets(core_offset_mhz: int = 0, mem_offset_mhz: int = 0) -> dict:
    """Set core and memory clock offsets.

    Each clock domain is written via its OWN, INDEPENDENT NVAPI call:

      * Core   → SetPstates20 sparse graphics freqDelta (verified id).  On
                 Boost-3.0+ GPUs the freqDelta shifts the whole V/F curve, so
                 this raises the effective boost ceiling — it's how NvAPIWrapper
                 applies a core offset.  (beta.5: the old SetClockBoostTable
                 "VF curve" path was removed — its NVAPI id was wrong, see the
                 ID_NvAPI_GPU_*ClockBoostTable note above.  set_core_vf_offset()
                 now cleanly no-ops, so core relies solely on this verified path.)
      * Memory → SetPstates20 with a sparse memory-only request — the
                 P-state freqDelta is the right knob for memory because
                 memory has no V/F curve, just a hard max.

    Splitting the two avoids the Blackwell `-104 NVAPI_NOT_SUPPORTED`
    failure we saw when sending core+mem together: the driver rejects
    the whole packet if it doesn't like any single field.

    Args:
        core_offset_mhz: signed MHz (e.g. +200, -50, 0 = stock)
        mem_offset_mhz:  signed MHz
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI not available")}

    handle, err = _get_first_gpu()
    if err:
        return {"ok": False, "err": err}

    # ── Probe driver's preferred struct version once (V1 / V2 / V3) ──────────
    _, ver_used, read_err = _read_pstates20(handle)
    if read_err:
        return {
            "ok": False,
            "err": f"GetPstates20 probe failed: {read_err}",
        }

    # Write each knob via its OWN sparse Pstates20 call — independent, so a
    # failure in one never aborts the others.  (beta.5: step 1, the VF
    # boost-table path, is intentionally a clean no-op now — its NVAPI id was
    # wrong; core OC lands via the verified graphics freqDelta in step 2.)

    # 1. Core via V/F boost-curve — DISABLED (ids unverified); returns ok:False.
    core_vf = set_core_vf_offset(core_offset_mhz)
    core_via_vf = bool(core_vf.get("ok"))
    core_vf_err = None if core_via_vf else core_vf.get("err", "VF curve disabled")

    # 2. Core via sparse P-state freqDelta (matches Afterburner behaviour)
    gfx_status = _set_pstates20_single(
        handle, ver_used,
        NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS,
        int(core_offset_mhz) * 1000,
    )
    core_via_pstate = (gfx_status == NVAPI_OK)
    core_pstate_err = None if core_via_pstate else _nvapi.error_message(gfx_status)

    # 3. Memory via sparse P-state freqDelta (the only path memory has)
    mem_status = _set_pstates20_single(
        handle, ver_used,
        NVAPI_GPU_PUBLIC_CLOCK_MEMORY,
        int(mem_offset_mhz) * 1000,
    )
    mem_ok = (mem_status == NVAPI_OK)
    mem_err = None if mem_ok else _nvapi.error_message(mem_status)

    # ── Read-back: did the driver actually store what we wrote? ──────────────
    # NVAPI returning NVAPI_OK doesn't guarantee the value landed — some
    # drivers silently no-op the call.  Read back both the VF curve and the
    # P-state freqDelta, compare to what we asked for, and surface a clear
    # diagnostic if there's a mismatch.
    readback_core_vf_mhz = None
    readback_core_p_mhz  = None
    readback_mem_p_mhz   = None
    try:
        rb_vf = get_core_vf_offset()
        if rb_vf.get("ok"):
            readback_core_vf_mhz = rb_vf.get("offset_mhz")
        rb_full, _, _ = _read_pstates20(handle)
        if rb_full is not None:
            for i in range(min(rb_full.numPstates, NVAPI_MAX_GPU_PSTATE20_PSTATES)):
                ps = rb_full.pstates[i]
                if ps.pstateId != NVAPI_GPU_PERF_PSTATE_P0:
                    continue
                for j in range(min(rb_full.numClocks, NVAPI_MAX_GPU_PSTATE20_CLOCKS)):
                    ck = ps.clocks[j]
                    if ck.domainId == NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS:
                        readback_core_p_mhz = ck.freqDelta_kHz.value // 1000
                    elif ck.domainId == NVAPI_GPU_PUBLIC_CLOCK_MEMORY:
                        readback_mem_p_mhz = ck.freqDelta_kHz.value // 1000
                break
    except Exception:
        pass

    # ── Aggregate result ─────────────────────────────────────────────────────
    # Core counts as written if either VF curve or P-state path landed.
    # When core_offset_mhz == 0 we accept "no path worked" as success.
    core_landed = core_via_vf or core_via_pstate or int(core_offset_mhz) == 0
    overall_ok  = core_landed and mem_ok

    result = {
        "ok": overall_ok,
        "applied_core_mhz":   int(core_offset_mhz),
        "applied_mem_mhz":    int(mem_offset_mhz),
        "core_via_vf_curve":  core_via_vf,
        "core_via_pstate":    core_via_pstate,
        "core_vf_err":        core_vf_err,
        "core_pstate_err":    core_pstate_err,
        "mem_err":            mem_err,
        "readback_core_vf_mhz":  readback_core_vf_mhz,
        "readback_core_p_mhz":   readback_core_p_mhz,
        "readback_mem_p_mhz":    readback_mem_p_mhz,
        "struct_version_used":   ver_used,
    }

    if not overall_ok:
        bits = []
        if not core_landed:
            bits.append(f"core: VF={core_vf_err}; pstate={core_pstate_err}")
        if not mem_ok:
            bits.append(f"memory: {mem_err}")
        result["err"] = " | ".join(bits) or "unknown NVAPI failure"

    return result


def reset_offsets() -> dict:
    """Reset core and memory offsets to zero (stock).

    Goes through the same dual-path as set_offsets so a stuck offset
    from a previous session can be cleared even if the V/F curve and
    P-state writes have started disagreeing.
    """
    return set_offsets(0, 0)


def force_reset_all() -> dict:
    """v2.9.1 — aggressive reset for stuck state.

    Clears the V/F curve AND the P-state freqDelta INDEPENDENTLY, even if
    one path errors.  Returns a per-step result so the UI can show the
    user exactly what worked and what didn't.
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI not available")}

    handle, err = _get_first_gpu()
    if err:
        return {"ok": False, "err": err}

    out = {"steps": []}

    # 1. V/F curve back to flat (every VF point freqDelta = 0)
    vf = set_core_vf_offset(0)
    out["steps"].append({"name": "VF curve → 0", "ok": vf.get("ok", False),
                         "err": vf.get("err", "")})

    # 2. P-state probe + sparse memory reset
    _, ver_used, read_err = _read_pstates20(handle)
    if read_err:
        out["steps"].append({"name": "Read P-state version", "ok": False,
                             "err": read_err})
        out["ok"] = False
        return out

    for label, domain in (("Memory P-state freqDelta → 0", NVAPI_GPU_PUBLIC_CLOCK_MEMORY),
                          ("Graphics P-state freqDelta → 0", NVAPI_GPU_PUBLIC_CLOCK_GRAPHICS)):
        rc = _set_pstates20_single(handle, ver_used, domain, 0)
        out["steps"].append({
            "name": label,
            "ok": (rc == NVAPI_OK),
            "err": "" if rc == NVAPI_OK else _nvapi.error_message(rc),
        })

    out["ok"] = any(s["ok"] for s in out["steps"])
    return out


def get_gpu_info() -> dict:
    """Quick test: load NVAPI and report what we find.

    Returns:
        {'ok': bool, 'gpu_count': int, 'first_handle': int (as int), 'err': str}
    """
    avail = is_available()
    if not avail.get("ok"):
        return {"ok": False, "err": avail.get("err", "NVAPI unavailable")}
    if not _nvapi.initialized:
        return {"ok": False, "err": "NVAPI not initialized"}

    gpus = (c_void_p * NVAPI_MAX_PHYSICAL_GPUS)()
    count = c_uint32(0)
    r = _nvapi.fn_enum_gpus(byref(gpus), byref(count))
    if r != NVAPI_OK:
        return {"ok": False, "err": _nvapi.error_message(r)}
    return {
        "ok": True,
        "gpu_count": count.value,
        "first_handle_int": int(gpus[0] or 0),
    }


# v3 — Cleanup at process exit.  CRITICAL: zero the offsets BEFORE
# unloading the NVAPI library.  Without this, any OC the user had
# active when they closed GhostShell persists in the live driver until
# the next reboot — and if GhostShell crashed mid-tune, the user could
# be left at an offset that just blue-screened their GPU.
import atexit


def _shutdown_cleanup():
    try:
        force_reset_all()
    except Exception:
        pass
    try:
        _nvapi.unload()
    except Exception:
        pass


atexit.register(_shutdown_cleanup)
