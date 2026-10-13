"""Tuning of ONE process — the game being tracked — through the Win32 API:
priority class, CPU affinity, CPU sets and power throttling.

Game mode used to do the first of these by starting PowerShell (half a
second, and silent on failure) and the second not at all: an old "best cores"
mask was removed because it gave hybrid-CPU games only some of their P-cores,
and nothing replaced it.  So a game that inherited a narrow affinity from its
launcher, or from an earlier build of this app, simply kept it.

What tune() guarantees, where Windows lets us open the process:
  • High priority class.
  • Affinity = every logical processor, and no process-wide CPU-set
    restriction: P-cores AND E-cores.  This lifts restrictions; it does not
    tell the scheduler where to put threads.  On hybrid CPUs Windows keeps
    preferring the P-cores for the heaviest threads and spreads the rest over
    the E-cores, which is the behaviour a game wants.
  • Exempt from power throttling ("efficiency mode"), which on hybrid CPUs
    means being moved to E-cores at low clock speed — and, on Windows 11,
    its timer-resolution requests are honoured even while it is not the
    foreground window.

Anti-cheat protected games refuse the handle; tune() then reports ok=False
and nothing about the process is changed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger

log = get_logger("proc_tune")

HIGH_PRIORITY_CLASS = 0x00000080
NORMAL_PRIORITY_CLASS = 0x00000020
_ACCESS = 0x0200 | 0x1000 | 0x2000      # SET_INFORMATION | QUERY_LIMITED_INFORMATION | SET_LIMITED_INFORMATION
_POWER_THROTTLING = 4                   # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_EXECUTION_SPEED = 0x1
_IGNORE_TIMER_RESOLUTION = 0x4          # Windows 11


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


_k32 = None


def _api():
    """kernel32 with the signatures spelled out — HANDLE and DWORD_PTR are
    64-bit, and ctypes' default of int silently truncates both."""
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        mask = ctypes.c_size_t
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetPriorityClass.restype = wintypes.DWORD
        k.GetPriorityClass.argtypes = [wintypes.HANDLE]
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(mask), ctypes.POINTER(mask)]
        k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, mask]
        k.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        try:
            k.SetProcessDefaultCpuSets.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
        except AttributeError:
            pass                        # not on this Windows; tune() copes
        _k32 = k
    return _k32


def _running(k, handle) -> bool:
    """False for a process that has exited but whose object is still around
    (someone holds a handle): it opens fine, and half the calls "succeed"."""
    code = wintypes.DWORD(0)
    return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE


def _set_throttling(k, handle, control: int) -> bool:
    state = _PowerThrottlingState(1, control, 0)        # StateMask 0: the controlled behaviours are switched OFF
    return bool(k.SetProcessInformation(handle, _POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)))


def tune(pid: int, all_cores: bool = True) -> dict:
    """Apply the per-process half of game mode to `pid`.

    Returns {"ok", "priority", "affinity", "throttling", "prior", "err"}.
    `affinity` is None when it was not touched, else {"ok", "cores", "was",
    "changed"} (counts of logical processors).  `prior` is what release()
    needs to put the process back."""
    out = {"ok": False, "priority": False, "affinity": None, "throttling": False, "prior": {}}
    k = _api()
    h = k.OpenProcess(_ACCESS, False, int(pid))
    if not h:
        code = ctypes.get_last_error()
        out["err"] = "access denied (protected process)" if code == 5 else f"could not open process (error {code})"
        return out
    try:
        if not _running(k, h):
            out["err"] = "process has exited"
            return out
        out["ok"] = True
        out["prior"]["priority"] = int(k.GetPriorityClass(h) or NORMAL_PRIORITY_CLASS)
        out["priority"] = bool(k.SetPriorityClass(h, HIGH_PRIORITY_CLASS))

        if all_cores:
            proc, system = ctypes.c_size_t(0), ctypes.c_size_t(0)
            if k.GetProcessAffinityMask(h, ctypes.byref(proc), ctypes.byref(system)) and system.value:
                was, want = proc.value, system.value
                done = True
                if was != want:
                    done = bool(k.SetProcessAffinityMask(h, want))
                    if done:
                        out["prior"]["affinity"] = was
                out["affinity"] = {"ok": done, "cores": bin(want).count("1"), "was": bin(was).count("1"),
                                   "changed": done and was != want}
            try:
                k.SetProcessDefaultCpuSets(h, None, 0)      # clear any process-wide CPU-set restriction
            except Exception:
                pass

        # Honouring the game's timer resolution is a Windows 11 flag; where it
        # is not known the whole call fails, so fall back to the part that is.
        out["throttling"] = (_set_throttling(k, h, _EXECUTION_SPEED | _IGNORE_TIMER_RESOLUTION)
                             or _set_throttling(k, h, _EXECUTION_SPEED))
    finally:
        k.CloseHandle(h)
    return out


def release(pid: int, prior: Optional[dict]) -> bool:
    """Undo tune() on a process that is still running (game mode switched off
    mid-game).  A process that has exited simply cannot be opened: False."""
    if not prior:
        return False
    k = _api()
    h = k.OpenProcess(_ACCESS, False, int(pid))
    if not h:
        return False
    try:
        if not _running(k, h):
            return False
        k.SetPriorityClass(h, int(prior.get("priority") or NORMAL_PRIORITY_CLASS))
        if prior.get("affinity"):
            k.SetProcessAffinityMask(h, int(prior["affinity"]))
        _set_throttling(k, h, 0)        # ControlMask 0: back to letting Windows decide
        return True
    finally:
        k.CloseHandle(h)


def cpu_layout() -> dict:
    """{"logical": n, "p": n, "e": n, "hybrid": bool} — logical processors in
    total and per core type.  On a non-hybrid CPU every core counts as "p"."""
    out = {"logical": 0, "p": 0, "e": 0, "hybrid": False}
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        size = wintypes.DWORD(0)
        k.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))         # 0 = RelationProcessorCore
        buf = (ctypes.c_ubyte * size.value)()
        if not k.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(size)):
            return out
        raw = bytes(buf)
        per_class: dict = {}
        off = 0
        while off + 32 <= size.value:
            rel = int.from_bytes(raw[off:off + 4], "little")
            length = int.from_bytes(raw[off + 4:off + 8], "little")
            if length <= 0:
                break
            if rel == 0:
                eff = raw[off + 9]                                               # PROCESSOR_RELATIONSHIP.EfficiencyClass
                groups = int.from_bytes(raw[off + 30:off + 32], "little")
                bits = 0
                for g in range(groups):                                          # GROUP_AFFINITY is 16 bytes, mask first
                    m = off + 32 + g * 16
                    bits += bin(int.from_bytes(raw[m:m + 8], "little")).count("1")
                per_class[eff] = per_class.get(eff, 0) + bits
            off += length
        if per_class:
            top = max(per_class)                                                 # higher class = faster core
            out["logical"] = sum(per_class.values())
            out["p"] = per_class[top]
            out["e"] = out["logical"] - out["p"]
            out["hybrid"] = len(per_class) > 1
    except Exception as e:
        log.debug(f"cpu_layout failed: {e}")
    return out


def describe_cores(layout: Optional[dict] = None) -> str:
    """"24 cores (8 P + 16 E)" on a hybrid CPU, "16 cores" otherwise."""
    lay = layout or cpu_layout()
    if not lay.get("logical"):
        return "every core"
    if lay.get("hybrid"):
        return f"{lay['logical']} cores ({lay['p']} P + {lay['e']} E)"
    return f"{lay['logical']} cores"