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

Every result is read back, not assumed: a call that "succeeds" while the
process ends up otherwise is reported as not done.  read_state() is what the
game monitor uses to notice when a game (or its launcher, or anti-cheat) has
changed something back.  The rest is read-only bookkeeping for the monitor:
whether a process is still the one it was (alive), which processes run a
given exe (find_image), and a cheap process list (processes) — all through the
API, because the PowerShell calls used before took seconds under load and,
when they timed out, a running game was taken for one that had exited.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional

from core.utils import get_logger

log = get_logger("proc_tune")

HIGH_PRIORITY_CLASS = 0x00000080
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
NORMAL_PRIORITY_CLASS = 0x00000020
_ACCESS = 0x0200 | 0x1000 | 0x2000      # SET_INFORMATION | QUERY_LIMITED_INFORMATION | SET_LIMITED_INFORMATION
_QUERY = 0x1000                         # PROCESS_QUERY_LIMITED_INFORMATION: granted even for most protected processes
_TERMINATE = 0x0001
_POWER_THROTTLING = 4                   # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_EXECUTION_SPEED = 0x1
_IGNORE_TIMER_RESOLUTION = 0x4          # Windows 11
_STILL_ACTIVE = 259
_NO_SUCH_PROCESS = 87                   # ERROR_INVALID_PARAMETER from OpenProcess
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


class _ProcessEntry(ctypes.Structure):      # PROCESSENTRY32W
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


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
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        for name, args in (("SetProcessDefaultCpuSets", [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]),
                           ("GetProcessInformation", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD])):
            try:
                getattr(k, name).argtypes = args
            except AttributeError:
                pass                    # not on this Windows; the callers cope
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


def _throttle_exempt(k, handle) -> Optional[bool]:
    """Is the process exempt from power throttling, as Windows reports it
    back?  None where this Windows cannot say."""
    state = _PowerThrottlingState(1, 0, 0)
    try:
        if not k.GetProcessInformation(handle, _POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)):
            return None
    except Exception:           # (AttributeError: no such call on this Windows)
        return None
    return bool(state.ControlMask & _EXECUTION_SPEED) and not (state.StateMask & _EXECUTION_SPEED)


def _masks(k, handle):
    proc, system = ctypes.c_size_t(0), ctypes.c_size_t(0)
    if k.GetProcessAffinityMask(handle, ctypes.byref(proc), ctypes.byref(system)) and system.value:
        return proc.value, system.value
    return None, None


def _created(k, handle) -> Optional[int]:
    times = [ctypes.c_ulonglong(0) for _ in range(4)]
    if k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return int(times[0].value)
    return None


def tune(pid: int, all_cores: bool = True) -> dict:
    """Apply the per-process half of game mode to `pid`.

    Returns {"ok", "priority", "affinity", "throttling", "prior", "err"}.
    Each part is True only if reading it back afterwards shows it in effect.
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
        out["priority"] = bool(k.SetPriorityClass(h, HIGH_PRIORITY_CLASS)) \
            and k.GetPriorityClass(h) == HIGH_PRIORITY_CLASS

        if all_cores:
            was, want = _masks(k, h)
            if want:
                if was != want and k.SetProcessAffinityMask(h, want):
                    out["prior"]["affinity"] = was
                now, _ = _masks(k, h)
                out["affinity"] = {"ok": now == want, "cores": bin(want).count("1"), "was": bin(was).count("1"),
                                   "changed": now == want and was != want}
            try:
                k.SetProcessDefaultCpuSets(h, None, 0)      # clear any process-wide CPU-set restriction
            except Exception:
                pass

        # Honouring the game's timer resolution is a Windows 11 flag; where it
        # is not known the whole call fails, so fall back to the part that is.
        set_ok = (_set_throttling(k, h, _EXECUTION_SPEED | _IGNORE_TIMER_RESOLUTION)
                  or _set_throttling(k, h, _EXECUTION_SPEED))
        exempt = _throttle_exempt(k, h)
        out["throttling"] = set_ok and exempt is not False
    finally:
        k.CloseHandle(h)
    return out


def read_state(pid: int) -> dict:
    """What the process is set to right now, read-only: {"ok", "priority",
    "affinity", "system", "throttle_exempt", "err"} (masks as integers;
    throttle_exempt None where Windows cannot say)."""
    out = {"ok": False, "priority": None, "affinity": None, "system": None, "throttle_exempt": None}
    k = _api()
    h = k.OpenProcess(_QUERY, False, int(pid))
    if not h:
        out["err"] = f"could not open process (error {ctypes.get_last_error()})"
        return out
    try:
        if not _running(k, h):
            out["err"] = "process has exited"
            return out
        out["ok"] = True
        out["priority"] = int(k.GetPriorityClass(h) or 0) or None
        out["affinity"], out["system"] = _masks(k, h)
        out["throttle_exempt"] = _throttle_exempt(k, h)
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


def set_priority(pid: int, priority_class: int) -> tuple:
    """Set just the priority class (for the streaming apps game mode raises).
    Returns (done, previous class), done meaning it reads back as asked."""
    k = _api()
    h = k.OpenProcess(_ACCESS, False, int(pid))
    if not h:
        return False, None
    try:
        if not _running(k, h):
            return False, None
        before = int(k.GetPriorityClass(h) or 0) or None
        done = bool(k.SetPriorityClass(h, int(priority_class))) and k.GetPriorityClass(h) == int(priority_class)
        return done, before
    finally:
        k.CloseHandle(h)


def terminate(pid: int) -> bool:
    k = _api()
    h = k.OpenProcess(_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(k.TerminateProcess(h, 1))
    finally:
        k.CloseHandle(h)


# ── finding and following a game's processes (read-only) ─────────────
def processes() -> Optional[list]:
    """Every running process: [{"pid", "ppid", "exe"}], or None if the list
    could not be taken."""
    k = _api()
    snap = k.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID_HANDLE:
        return None
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        out = []
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append({"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                        "exe": entry.szExeFile})
            ok = k.Process32NextW(snap, ctypes.byref(entry))
        return out
    finally:
        k.CloseHandle(snap)


def image_path(pid: int) -> str:
    """Full path of the process's exe, or "" if it cannot be read."""
    k = _api()
    h = k.OpenProcess(_QUERY, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k.CloseHandle(h)


def identity(pid: int) -> dict:
    """{"image", "created"}: enough to recognise this process later, after its
    pid may have gone to another one."""
    k = _api()
    h = k.OpenProcess(_QUERY, False, int(pid))
    if not h:
        return {"image": "", "created": None}
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        image = buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        return {"image": image, "created": _created(k, h)}
    finally:
        k.CloseHandle(h)


def alive(pid: int, created: Optional[int] = None, exe: str = "") -> Optional[bool]:
    """Is this process still running — and still the same one (`created`, from
    identity())?  None when it cannot be told; a caller must not take that for
    an exit."""
    k = _api()
    h = k.OpenProcess(_QUERY, False, int(pid))
    if not h:
        if ctypes.get_last_error() == _NO_SUCH_PROCESS:
            return False
        procs = processes()             # refused: the process list still names it, or not
        if procs is None:
            return None
        return any(p["pid"] == int(pid) and (not exe or p["exe"].lower() == exe.lower()) for p in procs)
    try:
        if not _running(k, h):
            return False
        if created is not None:
            now = _created(k, h)
            if now is not None and now != created:
                return False            # the pid belongs to another process by now
        return True
    finally:
        k.CloseHandle(h)


def find_image(image: str, exe: str = "") -> list:
    """Pids of every running process of this exe: the same full path, or —
    where a process's path cannot be read — the same file name."""
    exe = (exe or os.path.basename(image or "")).lower()
    if not exe:
        return []
    procs = processes() or []
    want = os.path.normcase(image) if image else ""
    found = []
    for p in procs:
        if p["exe"].lower() != exe:
            continue
        path = image_path(p["pid"])
        if not want or not path or os.path.normcase(path) == want:
            found.append(p["pid"])
    return found


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