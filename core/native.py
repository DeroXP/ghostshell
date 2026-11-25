"""The native helper: a small Rust library (native/ in the repository) that
answers a few questions the app asks all the time, inside the app's own
process, where each of them used to start a program.

Why it exists: on the owner's PC the running app was measured starting about
250 nvidia-smi processes a minute during a game and a PowerShell every three
seconds while idle, and one thread spent a quarter of a processor core reading
each process's I/O counters one process at a time (docs/performance.md has the
numbers).  One call into this library answers the same question in well under
a millisecond.

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

The questions (the "q" of a call) and their answers are listed in
native/README.md.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading

from core.utils import get_logger

log = get_logger("native")

ABI = 1                     # must equal ABI in native/src/lib.rs; a library of another version is not used
DLL_NAME = "vispora_native.dll"
_FIRST_BUFFER = 64 * 1024
_MAX_BUFFER = 16 * 1024 * 1024
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
    try:
        lib = _load()
        if lib is None:
            return None
        request = json.dumps({**args, "q": query}, separators=(",", ":")).encode("utf-8")
        size = _state["buffer"]
        raw = None
        for _ in range(4):
            buf = ctypes.create_string_buffer(size)
            n = int(lib.vispora_native_call(request, len(request), buf, size))
            if n > 0:
                raw = buf.raw[:n]
                break
            if n == 0:                                      # the library gave up without an answer
                _complain(query, "no answer")
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
