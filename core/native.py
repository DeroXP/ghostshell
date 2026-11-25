"""The native helper: a small Rust library (native/ in the repository) that
answers a few questions the app asks all the time, inside the app's own
process, where each of them used to start a program.

Why it exists: on the owner's PC the running app was measured starting about
250 nvidia-smi processes a minute during a game and a PowerShell every three
seconds while idle, and one thread spent a quarter of a processor core reading
each process's I/O counters one process at a time (docs/performance.md has the
numbers).  One call into this library answers the same question in a fraction
of a millisecond (the card's readings) to nine milliseconds (the whole process
list), and starts nothing.

It is optional on purpose.  Every function here returns None when the library
is not there (a build without it, a source checkout that never ran cargo), is
the wrong version, or could not answer, and every caller then asks the way it
always did: PowerShell, nvidia-smi, ping, psutil.  Nothing in the app may
depend on the helper being present.

The library is loaded from one fixed place only, next to the app's own files
(the unpacked bundle of a built exe, native/target/release of a checkout), by
its full path.  There is deliberately no setting or environment variable for
it: the app runs elevated, and a path anyone could set would be a way to run
their code as administrator.

Almost everything here is a QUESTION: it reads the machine and changes
nothing (call()).  Two things are ACTIONS, which free memory at start-up
(act(), at the end of this file).  The library keeps the two apart behind
separate exported functions, and so does this module.

The questions (the "q" of a call), the actions (the "do") and their answers
are listed in native/README.md.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading

from core.utils import get_logger

log = get_logger("native")

ABI = 2                     # must equal ABI in native/src/lib.rs; a library of another version is not used
DLL_NAME = "vispora_native.dll"
_FIRST_BUFFER = 64 * 1024
_MAX_BUFFER = 16 * 1024 * 1024
_ACTION_BUFFER = 8192       # an action is asked once, with room to spare (the library refuses less than 4096)
_GIVE_UP_AFTER = 5          # calls in a row that crashed inside the library

_lock = threading.Lock()
_state = {"tried": False, "lib": None, "why": "not looked for yet", "path": "", "buffer": _FIRST_BUFFER, "crashes": 0}
_complained: set = set()    # the questions a failure has been logged for (once each)


def dll_path() -> str:
    """Where the library has to be: beside the app's bundled files in a built
    exe, native/target/release in a source checkout."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(base, DLL_NAME)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "native", "target", "release", DLL_NAME)


def _open(path: str):
    """Load the library at `path` and check that it is ours and the version
    this code was written for.  Returns (library or None, why not)."""
    if sys.platform != "win32":
        return None, "not Windows"
    if not os.path.isfile(path):
        return None, "not installed"
    try:
        lib = ctypes.WinDLL(path)           # (a full path: Windows looks nowhere else for it)
        lib.vispora_native_abi.argtypes = []
        lib.vispora_native_abi.restype = ctypes.c_uint32
        lib.vispora_native_call.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32]
        lib.vispora_native_call.restype = ctypes.c_int32
        found = int(lib.vispora_native_abi())
        if found == ABI:
            lib.vispora_native_act.argtypes = lib.vispora_native_call.argtypes
            lib.vispora_native_act.restype = ctypes.c_int32
    except Exception as e:
        return None, f"could not be loaded: {e}"
    if found != ABI:
        return None, f"version {found}, this app needs version {ABI}"
    return lib, ""


def _load():
    """The loaded library, or None.  Looked for once per run."""
    with _lock:
        if not _state["tried"]:
            _state["tried"] = True
            _state["path"] = dll_path()
            _state["lib"], _state["why"] = _open(_state["path"])
            if _state["lib"] is not None:
                log.info(f"Native helper loaded ({_state['path']})")
            else:
                log.info(f"Native helper not in use ({_state['why']}): PowerShell and nvidia-smi answer instead")
        return _state["lib"]


def available() -> bool:
    """Is the helper loaded and in use?  Never raises."""
    try:
        return _load() is not None
    except Exception:
        return False


def status() -> dict:
    """For the log and the diagnostics page: in use or not, and why not."""
    on = available()
    return {"available": on, "path": _state["path"], "why": "" if on else _state["why"], "abi": ABI}


def _complain(query: str, what: str) -> None:
    """Log a failed question once, not at every tick of a loop."""
    if query not in _complained:
        _complained.add(query)
        log.info(f"Native helper could not answer '{query}' ({what}): the old way answers instead")


def call(query: str, /, **args):
    """Ask the helper one question, e.g. call("ping", target="1.1.1.1").
    Returns its answer (whatever JSON value that question answers with), or
    None when the helper is not there or could not answer.  Never raises."""
    return _exchange("vispora_native_call", query, {**args, "q": query}, again=True)


def act(action: str, /, **args):
    """Have the helper carry out one of its actions (see the end of this
    file).  Returns what it reports, or None when the helper is not there or
    did not do it.  Never raises.  Asked exactly once: an action is never
    repeated because its answer did not fit."""
    return _exchange("vispora_native_act", action, {**args, "do": action}, again=False)


def _exchange(export: str, query: str, request: dict, again: bool):
    """Send one request through one of the library's two doors and read the
    answer.  `again`: may the request be sent again with a larger buffer when
    the answer did not fit (questions: yes; actions: never)."""
    try:
        lib = _load()
        if lib is None:
            return None
        send = getattr(lib, export)
        request = json.dumps(request, separators=(",", ":")).encode("utf-8")
        size = _state["buffer"] if again else _ACTION_BUFFER
        raw = None
        for _ in range(4 if again else 1):
            buf = ctypes.create_string_buffer(size)
            n = int(send(request, len(request), buf, size))
            if n > 0:
                raw = buf.raw[:n]
                break
            if n == 0:                                      # the library gave up without an answer
                _complain(query, "no answer")
                return None
            if not again:
                _complain(query, "the answer did not fit")
                return None
            size = max(size * 2, -n + 1024)                 # too small: -n bytes are needed
            if size > _MAX_BUFFER:
                _complain(query, "the answer is too large")
                return None
            _state["buffer"] = size
        if raw is None:
            return None
        _state["crashes"] = 0
        answer = json.loads(raw.decode("utf-8"), parse_constant=lambda _c: None)    # (a bare NaN: read as missing)
        if not isinstance(answer, dict) or not answer.get("ok"):
            _complain(query, str((answer or {}).get("err") if isinstance(answer, dict) else "not an answer")[:200])
            return None
        return answer.get("data")
    except OSError as e:
        # An access violation inside the library arrives here.  A few in a row
        # and the helper is left alone for the rest of the run.
        _state["crashes"] += 1
        if _state["crashes"] >= _GIVE_UP_AFTER and _state["lib"] is not None:
            _state["lib"], _state["why"] = None, f"switched off after {_GIVE_UP_AFTER} failed calls in a row: {e}"
            log.warning(f"Native helper {_state['why']}")
        else:
            _complain(query, str(e))
        return None
    except Exception as e:
        _complain(query, str(e))
        return None


# ═════════════════════════════════════════════════════════════════════
# The questions.  Each checks the shape of the answer and returns None for
# anything it does not recognise, so a caller only ever has two cases:
# a good answer, or "ask the old way".
# ═════════════════════════════════════════════════════════════════════
def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ValueError(v)
    return int(v)


def _opt_int(v):
    return None if v is None else _int(v)


def windowed_processes():
    """Every process that owns a visible top-level window that has no owner
    (what .NET calls its main window, and what Get-Process's MainWindowHandle
    is not 0 for): [{"pid", "name", "ws"}], name being the image name without
    ".exe" as Get-Process prints it and ws the working set in bytes."""
    rows = call("windowed_processes")
    if not isinstance(rows, list):
        return None
    try:
        return [{"pid": _int(r["pid"]), "name": str(r["name"]), "ws": _int(r["ws"])} for r in rows]
    except (KeyError, TypeError, ValueError):
        _complain("windowed_processes", "an answer of the wrong shape")
        return None


def process_io():
    """Every process on the machine: [{"pid", "name", "other_bytes"}].  name is
    the image name ("chrome.exe"; "System Idle Process" for pid 0) and
    other_bytes the bytes of I/O that is neither a read nor a write, which on
    Windows includes network traffic (what psutil's io_counters().other_bytes
    is)."""
    rows = call("process_io")
    if not isinstance(rows, list):
        return None
    try:
        return [{"pid": _int(r["pid"]), "name": str(r["name"]), "other_bytes": _int(r["other_bytes"])} for r in rows]
    except (KeyError, TypeError, ValueError):
        _complain("process_io", "an answer of the wrong shape")
        return None


_GPU_FIELDS = ("temp_c", "gpu_util_pct", "mem_util_pct", "core_mhz", "mem_mhz", "core_max_mhz", "mem_max_mhz",
               "core_app_mhz", "mem_app_mhz", "power_mw", "power_limit_mw", "fan_pct", "mem_used_bytes",
               "mem_total_bytes")


def gpu_live():
    """The first NVIDIA card's live readings, from the driver's own NVML
    library (what nvidia-smi reads): a dict with every key of _GPU_FIELDS, each
    an int or None where the card does not report it.  None when there is no
    NVIDIA card or driver."""
    d = call("gpu_live")
    if not isinstance(d, dict):
        return None
    try:
        return {k: _opt_int(d.get(k)) for k in _GPU_FIELDS}
    except (TypeError, ValueError):
        _complain("gpu_live", "an answer of the wrong shape")
        return None


def gpu_identity():
    """What the first NVIDIA card is: {"name", "driver_version",
    "mem_total_bytes"} (the last two None where the driver does not say), as
    `nvidia-smi --query-gpu=name,driver_version,memory.total` prints them.
    None when there is no NVIDIA card or driver."""
    d = call("gpu_identity")
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"].strip():
        return None
    driver = d.get("driver_version")
    try:
        return {"name": d["name"].strip(), "driver_version": driver.strip() if isinstance(driver, str) else None,
                "mem_total_bytes": _opt_int(d.get("mem_total_bytes"))}
    except (TypeError, ValueError):
        _complain("gpu_identity", "an answer of the wrong shape")
        return None


def gpu_release(hold_s: int = 0) -> bool:
    """Let go of the NVIDIA library and do not open it again for `hold_s`
    seconds (a driver installer is about to replace the file); gpu_live()
    answers None for that long, so callers use nvidia-smi.  True when the
    helper took the call."""
    return call("gpu_release", hold_s=max(0, int(hold_s))) is not None


def cpu_clock_mhz():
    """The first processor's current clock in MHz (what Win32_Processor's
    CurrentClockSpeed reports), or None."""
    d = call("cpu_clock")
    try:
        mhz = _int(d["mhz"]) if isinstance(d, dict) else 0
    except (KeyError, TypeError, ValueError):
        return None
    return mhz if mhz > 0 else None


def cpu_times():
    """Processor time since boot summed over all processors, in 100 ns units:
    {"total", "idle", "dpc", "interrupt", "processors"}.  Two readings a moment
    apart give the share of time spent in each."""
    d = call("cpu_times")
    if not isinstance(d, dict):
        return None
    try:
        out = {k: _int(d[k]) for k in ("total", "idle", "dpc", "interrupt", "processors")}
    except (KeyError, TypeError, ValueError):
        _complain("cpu_times", "an answer of the wrong shape")
        return None
    return out if out["total"] > 0 and out["processors"] > 0 else None


def ping(target: str, timeout_ms: int = 500):
    """One echo request to an IPv4 address.  {"ok": True, "ms": n} when it
    answered (n is at least 1, as ping prints "<1ms"), {"ok": False} when it
    did not, None when the helper cannot do it (a host name: ping.exe looks
    those up) or is not there."""
    if not isinstance(target, str) or not _is_ipv4(target):
        return None
    d = call("ping", target=target, timeout_ms=int(timeout_ms))
    if not isinstance(d, dict) or not isinstance(d.get("ok"), bool):
        return None
    if not d["ok"]:
        return {"ok": False}
    try:
        return {"ok": True, "ms": max(1, _int(d["ms"]))}
    except (KeyError, TypeError, ValueError):
        return None


def _is_ipv4(text: str) -> bool:
    parts = text.strip().split(".")
    return len(parts) == 4 and all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts)


def wmi(namespace: str, query: str):
    """Run one WMI query (WQL) and return its rows as a list of dicts, the
    values as WMI gives them (numbers, strings, booleans, None).  None when
    the helper is not there or the query failed (a namespace that does not
    exist, access denied) - which a caller treats like a PowerShell call that
    printed nothing."""
    rows = call("wmi", namespace=namespace, query=query)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None
    return rows


def service_state(name: str):
    """Is a Windows service installed, and what is it doing?  {"installed":
    bool, "running": bool}, what `sc query <name>` tells.  None when the
    helper is not there or the service manager would not say."""
    if not isinstance(name, str) or not name.strip():
        return None
    d = call("service_state", name=name)
    if not isinstance(d, dict) or not isinstance(d.get("installed"), bool):
        return None
    try:
        state = _opt_int(d.get("state"))
    except (TypeError, ValueError):
        return None
    if d["installed"] and state is None:
        return None
    return {"installed": d["installed"], "running": d["installed"] and state == 4}


def _is_guid(text) -> bool:
    return (isinstance(text, str) and len(text) == 36 and text.count("-") == 4
            and all(c in "0123456789abcdefABCDEF-" for c in text))


def power_scheme():
    """The power plans: {"guid": the active plan, "schemes": [every plan]},
    GUIDs in lower case, as `powercfg /getactivescheme` and `powercfg /list`
    print them.  None when the helper is not there or Windows would not say."""
    d = call("power_scheme")
    if not isinstance(d, dict) or not _is_guid(d.get("guid")):
        return None
    schemes = d.get("schemes")
    if not isinstance(schemes, list) or not all(_is_guid(g) for g in schemes):
        return None
    return {"guid": d["guid"].lower(), "schemes": [g.lower() for g in schemes]}


def power_setting(scheme: str, sub: str, setting: str):
    """One setting of one power plan: {"ac", "dc", "max"}.  ac and dc are the
    two current values `powercfg /qh` prints; max is the largest value Windows
    accepts for it, None for a setting that is a list of choices.

    `scheme` is a plan's GUID or "current"; `sub` and `setting` are GUIDs (the
    caller turns powercfg's aliases into GUIDs first).  None when the helper is
    not there, when anything is not a GUID, or when the plan or the setting
    does not exist - which is when powercfg prints an error."""
    current = isinstance(scheme, str) and scheme.strip().lower() in ("current", "scheme_current")
    if not (current or _is_guid(scheme)) or not _is_guid(sub) or not _is_guid(setting):
        return None
    d = call("power_setting", scheme="current" if current else scheme, sub=sub, setting=setting)
    if not isinstance(d, dict):
        return None
    try:
        return {"ac": _int(d["ac"]), "dc": _int(d["dc"]), "max": _opt_int(d.get("max"))}
    except (KeyError, TypeError, ValueError):
        _complain("power_setting", "an answer of the wrong shape")
        return None


# ═════════════════════════════════════════════════════════════════════
# The actions.  The only two things the helper does that change anything:
# both free memory, neither changes a setting, and nothing has to be put
# back afterwards.  They go through act(), never call().
# ═════════════════════════════════════════════════════════════════════
def trim_working_sets():
    """Ask Windows to trim the working set of every process (the memory each
    keeps in RAM; it comes back as the process uses it).  {"processes": n,
    "trimmed": m, "reached_services": bool} when it was done, None when the
    helper is not there or did not do it.

    reached_services: the helper had the debug privilege while it opened the
    processes, which is what lets an elevated program reach Windows' own
    services (the PowerShell this replaces always had it).  False without
    administrator rights."""
    d = act("trim_working_sets")
    if not isinstance(d, dict):
        return None
    try:
        return {"processes": _int(d["processes"]), "trimmed": _int(d["trimmed"]),
                "reached_services": d.get("debug_privilege") is True}
    except (KeyError, TypeError, ValueError):
        return None


def purge_standby_list() -> bool:
    """Drop Windows' standby memory list (cached file pages nobody is using).
    True when it was done; False when the helper is not there or Windows
    refused (it needs administrator rights)."""
    d = act("purge_standby_list")
    return isinstance(d, dict) and d.get("purged") is True
