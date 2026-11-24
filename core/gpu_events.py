"""Graphics trouble from the Windows event log, read inside this process.

Adaptive Tuning needs to know when the graphics driver reported an error, when
a game crashed and when the PC stopped without shutting down.  Windows writes
all of that to the System and Application event logs.  This module reads them
through wevtapi.dll (EvtQuery, EvtNext, EvtRender, EvtClose).  It never starts
PowerShell or any other process, it only reads, and no public function raises.

Every call to Windows goes through the module-level adapter `_api` (class
WevtApi, a private WinDLL), so tests can swap in a fake.  `_now()` is the one
place the time of day is read, `_monotonic()` the one place a read's time
limit is measured.

One read (one EvtQuery and the EvtNext calls on it) gives up after
_READ_DEADLINE_S seconds.  EvtQuery has no time limit of its own, so it runs on
a helper thread the caller stops waiting for; each EvtNext is given what is
left of the time, at most _NEXT_TIMEOUT_MS, and a scan that runs into that
limit carries on with the same query.  A read can therefore keep its caller
for up to _READ_DEADLINE_S seconds, never for good.

What counts as what:

  driver_error         System: nvlddmkm 153, 13 and 14.  The rendered message
                       is empty on current drivers, so the EventData strings
                       are read: a device path and "Error occurred on GPUID:
                       100".  Display 4101 counts only when its data names
                       nvlddmkm (another graphics driver timing out says
                       nothing about this card).
  app_crash            Application: Application Error 1000 and Application
                       Hang 1002.
  gpu_bugcheck         System: Kernel-Power 41 whose BugcheckCode is 0x116,
                       0x117, 0x119, 0x141 or 0x10E, and WER-SystemErrorReporting
                       1001 whose first value starts with one of those codes.
                       Application: Windows Error Reporting 1001 for a
                       LiveKernelEvent 141 or 117.
  unexpected_shutdown  System: Kernel-Power 41 with any other code, and
                       EventLog 6008.
  boot                 System: EventLog 6005 (the event log service started).

The two 1001 records are never written on a PC where Windows Error Reporting is
switched off, and a Vispora privacy tweak switches it off.  Kernel-Power 41 is
always written, which is why it is a source of its own.  Kernel-Power 41 and
EventLog 6008 carry the time of the NEXT start of Windows, not the moment the PC
stopped.  Record ids are per channel and not in time order, so no time is
ever taken from the order Windows returns records in.  (The order itself is
the order Windows wrote them in, which is all newest_system_record_id() and
the oldest record's time are read from.)
"""
from __future__ import annotations

import calendar
import ctypes
import datetime
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes as W

from core.utils import get_logger

log = get_logger(__name__)

KINDS = ("driver_error", "app_crash", "gpu_bugcheck", "unexpected_shutdown", "boot")
CHANNELS = ("System", "Application")

# VIDEO_TDR_FAILURE, VIDEO_TDR_TIMEOUT_DETECTED, VIDEO_SCHEDULER_INTERNAL_ERROR,
# VIDEO_ENGINE_TIMEOUT_DETECTED, VIDEO_MEMORY_MANAGEMENT_INTERNAL
GPU_BUGCHECK_CODES = frozenset((0x116, 0x117, 0x119, 0x141, 0x10E))
LIVE_KERNEL_CODES = frozenset((0x141, 0x117))      # the live (non-fatal) reports about the graphics driver

BURST_GAP_S = 300.0         # driver errors less than this far apart are one burst
CACHE_S = 300.0             # driver_error_bursts: how long an answer is reused
CACHE_FAILED_S = 30.0       # ... and how long a failed read is
MAX_RECORDS = 20000         # per channel and query; the newest are kept (a 20 MB log holds about this many)

# (kinds a source can turn into, channel, provider, event ids)
_SOURCES = (
    (("driver_error",), "System", "nvlddmkm", (153, 13, 14)),
    (("driver_error",), "System", "Display", (4101,)),
    (("gpu_bugcheck", "unexpected_shutdown"), "System", "Microsoft-Windows-Kernel-Power", (41,)),
    (("gpu_bugcheck",), "System", "Microsoft-Windows-WER-SystemErrorReporting", (1001,)),
    (("unexpected_shutdown",), "System", "EventLog", (6008,)),
    (("boot",), "System", "EventLog", (6005,)),
    (("app_crash",), "Application", "Application Error", (1000,)),
    (("app_crash",), "Application", "Application Hang", (1002,)),
    (("gpu_bugcheck",), "Application", "Windows Error Reporting", (1001,)),
)

ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
ERROR_TIMEOUT = 1460
_EVT_QUERY_CHANNEL_PATH = 0x1
_EVT_QUERY_REVERSE_DIRECTION = 0x200
_EVT_RENDER_EVENT_XML = 1
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
_BATCH = 64                 # event handles per EvtNext
# What one EvtNext may take.  EvtNext scans the channel until it has a batch,
# and a scan of a cold or large log (seconds after a driver reset, at login
# after a crash) can take longer: it then answers ERROR_TIMEOUT and the same
# query carries on where it was, until the read's deadline below.
_NEXT_TIMEOUT_MS = 2000
# One read, EvtQuery and every EvtNext, ends after this long: an event log
# service that does not answer fails the read instead of holding the caller.
_READ_DEADLINE_S = 15.0
# EvtQuery calls given up on that have still not come back.  While this many
# are stuck, a read fails at once instead of starting another helper thread.
_MAX_STUCK_QUERIES = 2


# ═════════════════════════════════════════════════════════════════════
# Windows
# ═════════════════════════════════════════════════════════════════════
class WevtApi:
    """The four wevtapi.dll calls, bound on a private WinDLL the first time one
    is needed.  They report failure through return values and never raise:

        EvtQuery(channel, xpath, flags)     -> (handle, error)          handle 0 on failure
        EvtNext(query, count, timeout_ms)   -> (handles, error)         [] with error 259 at the end
        EvtRender(event, size)              -> (ok, text, used, error)  size and used are bytes
        EvtClose(handle)                    -> bool

    `error` is the Windows error code, 0 when the call worked.
    """

    def __init__(self):
        self._dll = None
        self._load_error = 0
        self._lock = threading.Lock()

    def _load(self):
        if self._dll is not None or self._load_error:
            return self._dll
        with self._lock:
            if self._dll is None and not self._load_error:
                try:
                    # A private instance, so the argtypes set here never clash with the
                    # bindings of another module, and looked for in System32 only.
                    dll = ctypes.WinDLL("wevtapi", use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
                    handle, dword = ctypes.c_void_p, W.DWORD      # EVT_HANDLE is pointer sized
                    dll.EvtQuery.argtypes = [handle, W.LPCWSTR, W.LPCWSTR, dword]
                    dll.EvtQuery.restype = handle
                    dll.EvtNext.argtypes = [handle, dword, ctypes.POINTER(handle), dword, dword,
                                            ctypes.POINTER(dword)]
                    dll.EvtNext.restype = W.BOOL
                    dll.EvtRender.argtypes = [handle, handle, dword, dword, ctypes.c_void_p,
                                              ctypes.POINTER(dword), ctypes.POINTER(dword)]
                    dll.EvtRender.restype = W.BOOL
                    dll.EvtClose.argtypes = [handle]
                    dll.EvtClose.restype = W.BOOL
                    self._dll = dll
                except Exception as e:
                    self._load_error = int(getattr(e, "winerror", 0) or 126)     # 126: module not found
                    log.warning("wevtapi.dll could not be loaded: %s", e)
        return self._dll

    def EvtQuery(self, channel, xpath, flags):
        dll = self._load()
        if dll is None:
            return 0, self._load_error
        handle = dll.EvtQuery(None, channel, xpath, flags)
        if not handle:
            return 0, ctypes.get_last_error() or -1
        return handle, 0

    def EvtNext(self, query, count, timeout_ms):
        dll = self._load()
        if dll is None:
            return [], self._load_error
        handles = (ctypes.c_void_p * count)()
        returned = W.DWORD(0)
        if not dll.EvtNext(query, count, handles, timeout_ms, 0, ctypes.byref(returned)):
            return [], ctypes.get_last_error() or -1
        return [handles[i] for i in range(returned.value) if handles[i]], 0

    def EvtRender(self, event, size):
        dll = self._load()
        if dll is None:
            return False, "", 0, self._load_error
        used, properties = W.DWORD(0), W.DWORD(0)
        size = max(0, int(size))
        buf = ctypes.create_unicode_buffer(size // 2 + 1) if size else None
        if not dll.EvtRender(None, event, _EVT_RENDER_EVENT_XML, size, buf,
                             ctypes.byref(used), ctypes.byref(properties)):
            return False, "", used.value, ctypes.get_last_error() or -1
        return True, (buf.value if buf is not None else ""), used.value, 0

    def EvtClose(self, handle):
        dll = self._load()
        return bool(dll is not None and handle and dll.EvtClose(handle))


_api = WevtApi()


def _now() -> float:
    """The one place this module reads the time of day."""
    return time.time()


def _monotonic() -> float:
    """The one place a read's time limit is measured."""
    return time.monotonic()


# ═════════════════════════════════════════════════════════════════════
# Reading a channel
# ═════════════════════════════════════════════════════════════════════
class _ReadError(Exception):
    """The log could not be read.  The text says where and with which code."""


_warned: dict = {}
_warned_lock = threading.Lock()


def _warn(text: str) -> None:
    """Log a warning, but the same one at most every ten minutes: a log that
    cannot be read is asked again every few seconds."""
    try:
        now = _now()
        with _warned_lock:
            last = _warned.get(text)
            if last is not None and 0 <= now - last < 600:
                return
            if len(_warned) > 50:
                _warned.clear()
            _warned[text] = now
    except Exception:
        pass
    log.warning(text)


def _render(api, event) -> str:
    """One record as XML text.  EvtRender is called twice: the first call, with
    no buffer, fails with ERROR_INSUFFICIENT_BUFFER and says how many BYTES it
    needs; the second fills a buffer of that size."""
    ok, text, used, err = api.EvtRender(event, 0)
    for _ in range(3):
        if ok:
            return text
        if err != ERROR_INSUFFICIENT_BUFFER or not used or used < 0:
            break
        ok, text, used, err = api.EvtRender(event, used)
    if ok:
        return text
    raise _ReadError("EvtRender failed (error %s)" % err)


_stuck = {"queries": 0}
_stuck_lock = threading.Lock()


def _open_query(api, channel: str, xpath: str, flags: int, wait_s: float):
    """api.EvtQuery, given at most `wait_s` seconds: (handle, error).

    EvtQuery is a call into the event log service with no time limit.  It runs
    on a helper thread; when it has not answered in time the read fails, and
    the helper closes the handle it gets once the call does come back.  While
    _MAX_STUCK_QUERIES such calls are still out, no new one is started.  When
    no thread can be started, EvtQuery is called here, as it used to be.
    Raises _ReadError, or what EvtQuery raised."""
    if wait_s <= 0:
        raise _ReadError("the read ran out of time before the %s log was asked" % channel)
    with _stuck_lock:
        if _stuck["queries"] >= _MAX_STUCK_QUERIES:
            raise _ReadError("the event log service is not answering (the %s log was not asked)" % channel)
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["answer"] = api.EvtQuery(channel, xpath, flags)
        except BaseException as e:          # handed to the caller
            box["raised"] = e
        with _stuck_lock:
            done.set()
            given_up = box.get("given_up", False)
            if given_up:
                _stuck["queries"] -= 1
        if given_up:
            handle = (box.get("answer") or (0, 0))[0]
            if handle:
                try:
                    api.EvtClose(handle)
                except Exception:
                    pass

    try:
        threading.Thread(target=run, daemon=True, name="event-log-query").start()
    except RuntimeError:
        return api.EvtQuery(channel, xpath, flags)
    if not done.wait(max(0.0, wait_s)):
        with _stuck_lock:
            if not done.is_set():
                box["given_up"] = True
                _stuck["queries"] += 1
        if box.get("given_up"):
            raise _ReadError("EvtQuery did not answer on the %s log within %g s" % (channel, wait_s))
    if "raised" in box:
        raise box["raised"]
    return box["answer"]


def _deadline() -> float:
    """When a read that starts now gives up (on the _monotonic() clock)."""
    return _monotonic() + _READ_DEADLINE_S


def _read(channel: str, xpath: str, limit=None, reverse: bool = True, render: bool = True, deadline=None) -> list:
    """The records `xpath` selects in `channel`, as XML text, newest first when
    `reverse`, at most `limit` of them (MAX_RECORDS when None).  With render
    off, one empty string per record.  Every handle is closed whatever happens.
    Gives up at `deadline` (_deadline() when None: _READ_DEADLINE_S from now).
    Raises _ReadError."""
    api = _api
    capped = limit is None
    if capped:
        limit = MAX_RECORDS
    flags = _EVT_QUERY_CHANNEL_PATH | (_EVT_QUERY_REVERSE_DIRECTION if reverse else 0)
    if deadline is None:
        deadline = _deadline()
    query_handle, err = _open_query(api, channel, xpath, flags, deadline - _monotonic())
    if not query_handle:
        raise _ReadError("EvtQuery failed on the %s log (error %s)" % (channel, err))
    out = []
    try:
        while len(out) < limit:
            left_ms = int((deadline - _monotonic()) * 1000.0)
            if left_ms <= 0:
                raise _ReadError("the %s log did not answer within %g s" % (channel, _READ_DEADLINE_S))
            handles, err = api.EvtNext(query_handle, min(_BATCH, limit - len(out)), min(_NEXT_TIMEOUT_MS, left_ms))
            if not handles:
                if err in (0, ERROR_NO_MORE_ITEMS):
                    return out
                if err == ERROR_TIMEOUT:
                    continue                # a long scan: the same query carries on where it was
                raise _ReadError("EvtNext failed on the %s log (error %s)" % (channel, err))
            try:
                for event in handles:
                    out.append(_render(api, event) if render else "")
            finally:
                for event in handles:
                    try:
                        api.EvtClose(event)
                    except Exception:
                        pass
        if capped:
            _warn("the %s event log has more than %d matching records; only the newest were read"
                  % (channel, limit))
        return out
    finally:
        try:
            api.EvtClose(query_handle)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════
# Time bounds and the query text
# ═════════════════════════════════════════════════════════════════════
_EPOCH = datetime.datetime(1970, 1, 1)
_MAX_TS = 253402300799.0            # 9999-12-31T23:59:59Z


def _number(value):
    """A finite float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _iso(ts: float, up: bool) -> str:
    """Seconds since 1970 as the UTC text the event log compares against, to the
    millisecond: rounded down for a lower bound, up for an upper one, so the
    query never leaves out a record the exact bound would keep."""
    ts = min(max(ts, 0.0), _MAX_TS)
    ms = int(math.ceil(ts * 1000.0) if up else math.floor(ts * 1000.0))
    seconds, ms = divmod(ms, 1000)
    d = _EPOCH + datetime.timedelta(seconds=seconds)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (d.year, d.month, d.day, d.hour, d.minute, d.second, ms)


def _time_clause(since: float, until: float) -> str:
    return "TimeCreated[@SystemTime>='%s' and @SystemTime<='%s']" % (_iso(since, False), _iso(until, True))


def _selector(channel: str, wanted) -> str:
    """The provider and id part of the query for the sources of the `wanted`
    kinds in `channel`, or "" when the channel has none."""
    ids_by_provider: dict = {}
    for kinds, source_channel, provider, ids in _SOURCES:
        if source_channel == channel and any(k in wanted for k in kinds):
            have = ids_by_provider.setdefault(provider, [])
            have.extend(i for i in ids if i not in have)
    parts = []
    for provider, ids in ids_by_provider.items():
        id_text = " or ".join("EventID=%d" % i for i in ids)
        if len(ids) > 1:
            id_text = "(%s)" % id_text
        parts.append("(Provider[@Name='%s'] and %s)" % (provider, id_text))
    return " or ".join(parts)


def _asked_channels(channels) -> tuple:
    """The channels a query may read, in CHANNELS' order (None: both)."""
    if channels is None:
        return CHANNELS
    if isinstance(channels, str):
        channels = (channels,)
    try:
        asked = frozenset(channels)
    except TypeError:
        raise _ReadError("channels must be a list of channel names")
    unknown = sorted(str(c) for c in asked if c not in CHANNELS)
    if unknown:
        raise _ReadError("unknown channel: %s" % ", ".join(unknown))
    return tuple(c for c in CHANNELS if c in asked)


def _wanted(kinds) -> frozenset:
    if kinds is None:
        return frozenset(KINDS)
    if isinstance(kinds, str):
        kinds = (kinds,)
    try:
        wanted = frozenset(kinds)
    except TypeError:
        raise _ReadError("kinds must be a list of kind names")
    unknown = sorted(str(k) for k in wanted if k not in KINDS)
    if unknown:
        raise _ReadError("unknown kind: %s" % ", ".join(unknown))
    return wanted


# ═════════════════════════════════════════════════════════════════════
# From XML to an event
# ═════════════════════════════════════════════════════════════════════
_TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z?$")
_CHAR_REF = re.compile(r"&#(?:[xX]([0-9A-Fa-f]+)|([0-9]+));")
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_MARKS = re.compile("[\u200e\u200f\u202a-\u202e]")      # direction marks Windows puts around dates
_LEADING_HEX = re.compile(r"\s*0[xX]([0-9A-Fa-f]+)")
_HEADER = (re.compile(r"<Provider\s[^>]*?Name=['\"]([^'\"]*)['\"]"), re.compile(r"<EventID[^>]*>\s*(\d+)\s*</EventID>"),
           re.compile(r"<EventRecordID>\s*(\d+)\s*</EventRecordID>"),
           re.compile(r"<TimeCreated\s[^>]*?SystemTime=['\"]([^'\"]+)['\"]"))


def _parse_time(text: str) -> float:
    """'2026-11-01T22:14:13.6730008Z' as seconds since 1970 (UTC).  The log has
    seven digits after the point, one more than datetime keeps."""
    mo = _TIME.match((text or "").strip())
    if not mo:
        raise ValueError("unexpected time: %r" % (text,))
    year, month, day, hour, minute, second = (int(g) for g in mo.groups()[:6])
    whole = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    fraction = mo.group(7) or "0"
    if whole < 0:
        return whole + float("0." + fraction)
    return float("%d.%s" % (whole, fraction))


def _xml_ok(code_point: int) -> bool:
    return (code_point in (9, 10, 13) or 0x20 <= code_point <= 0xD7FF or 0xE000 <= code_point <= 0xFFFD
            or 0x10000 <= code_point <= 0x10FFFF)


def _clean(text: str) -> str:
    """Event data can hold characters XML does not allow (as text or as a
    character reference), which the parser refuses.  Drop them."""
    def ref(mo):
        try:
            code_point = int(mo.group(1), 16) if mo.group(1) else int(mo.group(2))
        except ValueError:
            return ""
        return mo.group(0) if _xml_ok(code_point) else ""
    return _NOT_XML.sub("", _CHAR_REF.sub(ref, text))


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(node, name: str):
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _parse_header(xml_text: str, channel: str):
    """What can still be said about a record whose XML does not parse: who wrote
    it, its id, its number and its time, with no data.  None when not even that
    can be found."""
    try:
        provider, event_id, record_id, when = (rx.search(xml_text).group(1) for rx in _HEADER)
        return {"provider": provider, "event_id": int(event_id), "record_id": int(record_id),
                "ts": _parse_time(when), "channel": channel, "data": []}
    except Exception:
        return None


def _parse(xml_text: str, channel: str):
    """One rendered record as a plain dict, or None when it cannot be read:
    {"provider", "event_id", "record_id", "ts", "channel", "data"} where data is
    the EventData strings in order, as (name or None, text).  A record that is
    not well-formed XML even after cleaning keeps its header and loses its data,
    so a driver error is still a driver error."""
    try:
        try:
            root = ET.fromstring(xml_text)
        except Exception:               # ParseError, or a lone surrogate that cannot be encoded
            root = ET.fromstring(_clean(xml_text))
        if _local(root.tag) != "Event":
            return None
        system = _child(root, "System")
        provider = _child(system, "Provider").get("Name") or ""
        event_id = int((_child(system, "EventID").text or "").strip())
        record_id = int((_child(system, "EventRecordID").text or "").strip())
        ts = _parse_time(_child(system, "TimeCreated").get("SystemTime"))
        data = []
        event_data = _child(root, "EventData")
        if event_data is not None:
            for item in event_data:
                if _local(item.tag) == "Data":
                    data.append((item.get("Name"), item.text or ""))
        return {"provider": provider, "event_id": event_id, "record_id": record_id, "ts": ts,
                "channel": channel, "data": data}
    except Exception as e:
        raw = _parse_header(xml_text, channel) if isinstance(xml_text, str) else None
        _warn("an event log record in %s could not be read%s (%s)"
              % (channel, "" if raw is None else " in full", type(e).__name__))
        return raw


def _field(data: list, name: str, position=None) -> str:
    """An EventData string by its Name, else by position (older Windows builds
    write the same strings without names), else ""."""
    for item_name, text in data:
        if item_name == name:
            return text.strip()
    if position is not None and 0 <= position < len(data) and data[position][0] is None:
        return data[position][1].strip()
    return ""


def _int_text(text: str, base: int):
    """'0x5d30', '5d30' (base 16) or '278' (base 10) as an int, else None."""
    text = (text or "").strip().lower()
    try:
        if text.startswith("0x"):
            return int(text, 16)
        return int(text, base) if text else None
    except ValueError:
        return None


def _file_name(text: str) -> str:
    return text.replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()


def _event(raw: dict, kind: str, detail: str = "", exe: str = "", module: str = "", pid: int = 0, code=None) -> dict:
    return {"ts": raw["ts"], "kind": kind, "provider": raw["provider"], "event_id": raw["event_id"],
            "record_id": raw["record_id"], "channel": raw["channel"], "detail": detail[:300],
            "exe": exe, "module": module, "pid": pid, "code": code}


def _classify(raw: dict):
    """The Event for a parsed record, or None when the record is not one of ours.
    Decided here from the record itself, never from what the query asked for."""
    provider, event_id, data = raw["provider"].lower(), raw["event_id"], raw["data"]
    strings = [text.strip() for _name, text in data if text and text.strip()]

    if raw["channel"] == "System":
        if provider == "nvlddmkm" and event_id in (153, 13, 14):
            return _event(raw, "driver_error", " | ".join(strings))
        if provider == "display" and event_id == 4101:
            if any("nvlddmkm" in s.lower() for s in strings):
                return _event(raw, "driver_error",
                              "the display driver %s stopped responding and recovered" % strings[0])
            return None
        if provider == "microsoft-windows-kernel-power" and event_id == 41:
            code = _int_text(_field(data, "BugcheckCode", 0), 10)
            if code in GPU_BUGCHECK_CODES:
                return _event(raw, "gpu_bugcheck", "Windows stopped with bugcheck 0x%X" % code, code=code)
            if code:
                detail = "Windows stopped with bugcheck 0x%X" % code
            elif code == 0:
                detail = "the PC stopped without shutting down (no bugcheck code)"
            else:
                detail = "the PC stopped without shutting down"
            return _event(raw, "unexpected_shutdown", detail, code=code)
        if provider == "microsoft-windows-wer-systemerrorreporting" and event_id == 1001:
            first = data[0][1] if data else ""
            mo = _LEADING_HEX.match(first)
            code = int(mo.group(1), 16) if mo else None
            if code in GPU_BUGCHECK_CODES:
                return _event(raw, "gpu_bugcheck", "Windows restarted from bugcheck " + first.strip(), code=code)
            return None
        if provider == "eventlog" and event_id == 6008:
            when = _MARKS.sub("", " ".join(t.strip() for _name, t in data[:2] if t and t.strip()))
            return _event(raw, "unexpected_shutdown",
                          ("the previous shutdown at %s was unexpected" % when) if when
                          else "the previous shutdown was unexpected")
        if provider == "eventlog" and event_id == 6005:
            return _event(raw, "boot", "the event log service started")
        return None

    if raw["channel"] == "Application":
        if provider == "application error" and event_id == 1000:
            app = _field(data, "AppName", 0)
            module = _field(data, "ModuleName", 3)
            code = _int_text(_field(data, "ExceptionCode", 6), 16)
            detail = "%s stopped working in %s" % (app or "a program", module or "an unknown module")
            if code is not None:
                detail += " (exception %08x)" % code
            return _event(raw, "app_crash", detail, exe=_file_name(app), module=_file_name(module),
                          pid=_int_text(_field(data, "ProcessId", 8), 16) or 0, code=code)
        if provider == "application hang" and event_id == 1002:
            # Position 3 of a hang record is a start time, not a module: no positional fallback.
            app = _field(data, "AppName", 0)
            return _event(raw, "app_crash", "%s stopped responding" % (app or "a program"),
                          exe=_file_name(app), module=_file_name(_field(data, "ModuleName")),
                          pid=_int_text(_field(data, "ProcessId", 2), 16) or 0)
        if provider == "windows error reporting" and event_id == 1001:
            if _field(data, "EventName", 2).lower() != "livekernelevent":
                return None
            code = _int_text(_field(data, "P1", 5), 16)
            if code in LIVE_KERNEL_CODES:
                return _event(raw, "gpu_bugcheck", "Windows reported a live kernel event %x" % code, code=code)
            return None
    return None


# ═════════════════════════════════════════════════════════════════════
# Public
# ═════════════════════════════════════════════════════════════════════
def query(since, until=None, kinds=None, channels=None) -> dict:
    """Events with since <= ts <= until (seconds since 1970; until None means
    now), oldest first: {"ok": bool, "events": [Event], "err": str}.

    Event = {"ts", "kind", "provider", "event_id", "record_id", "channel",
    "detail", "exe", "module", "pid", "code"}.  exe and module are lower-case
    file names ("" when the record has none), pid is 0 when unknown, code is
    the bugcheck code (gpu_bugcheck, and the Kernel-Power unexpected_shutdown)
    or the exception code (app_crash), else None.

    `kinds` narrows the answer to those kinds (a name or a list of names) and
    the reading to the channels that can hold them: one EvtQuery per channel.
    `channels` ("System", "Application" or a list of them) narrows the reading
    further, for a caller that only uses one log's records: the other log is
    then not read at all, so it cannot fail the answer either.
    An event is identified by (channel, record_id); it appears once.

    When any part of the log cannot be read the answer is ok False with NO
    events, never half a picture.  (One exception, logged when it happens: a
    channel with more than MAX_RECORDS matching records gives its newest
    MAX_RECORDS.)  Both channels together get _READ_DEADLINE_S seconds.
    Never raises."""
    try:
        wanted = _wanted(kinds)
        asked = _asked_channels(channels)
        lo = _number(since)
        hi = _now() if until is None else _number(until)
        if lo is None or hi is None:
            raise _ReadError("since and until must be times in seconds")
        found: dict = {}
        if hi >= lo:
            clause = _time_clause(lo, hi)
            deadline = _deadline()
            for channel in asked:
                selector = _selector(channel, wanted)
                if not selector:
                    continue
                for xml_text in _read(channel, "*[System[(%s) and %s]]" % (selector, clause), deadline=deadline):
                    raw = _parse(xml_text, channel)
                    event = _classify(raw) if raw else None
                    if event is None or event["kind"] not in wanted or not lo <= event["ts"] <= hi:
                        continue
                    found.setdefault((channel, event["record_id"]), event)
        events = sorted(found.values(), key=lambda e: (e["ts"], e["channel"], e["record_id"]))
        return {"ok": True, "events": events, "err": ""}
    except _ReadError as e:
        _warn("event log: %s" % e)
        return {"ok": False, "events": [], "err": str(e)}
    except Exception as e:
        _warn("event log: the read failed (%s: %s)" % (type(e).__name__, e))
        return {"ok": False, "events": [],
                "err": "The Windows event log could not be read (%s)." % type(e).__name__}


def any_system_record(since, until) -> bool:
    """Did Windows write anything at all to the System log between `since` and
    `until` (to the millisecond)?  Looks at one record at most.

    A log that cannot be read answers True: the caller takes a quiet log as
    proof that the PC was off, and a log nobody could read proves nothing.
    Never raises."""
    try:
        lo, hi = _number(since), _number(until)
        if lo is None or hi is None:
            raise _ReadError("since and until must be times in seconds")
        if hi < lo:
            return False
        return bool(_read("System", "*[System[%s]]" % _time_clause(lo, hi), limit=1, render=False))
    except _ReadError as e:
        _warn("event log: %s" % e)
    except Exception as e:
        _warn("event log: the read failed (%s: %s)" % (type(e).__name__, e))
    return True


def newest_system_record_id():
    """The record number of the newest record in the System log right now, or
    None when that cannot be told.

    Windows numbers the records of a log in the order it writes them, so a
    caller can tell "this record was written before that moment" without
    trusting the clock.  One record is read.  Never raises."""
    try:
        for xml_text in _read("System", "*", limit=1, reverse=True):
            raw = _parse(xml_text, "System")
            if raw:
                return int(raw["record_id"])
    except _ReadError as e:
        _warn("event log: %s" % e)
    except Exception as e:
        _warn("event log: the read failed (%s: %s)" % (type(e).__name__, e))
    return None


def _oldest_system_ts():
    """When the oldest record still in the System log was written, or None."""
    try:
        for xml_text in _read("System", "*", limit=1, reverse=False):
            raw = _parse(xml_text, "System")
            if raw:
                return raw["ts"]
    except _ReadError as e:
        _warn("event log: %s" % e)
    except Exception as e:
        _warn("event log: the read failed (%s: %s)" % (type(e).__name__, e))
    return None


def _chain(times: list) -> list:
    """Times as bursts: a time less than BURST_GAP_S after the one before it
    belongs to the same burst."""
    bursts = []
    for ts in sorted(times):
        if bursts and ts - bursts[-1]["last_ts"] < BURST_GAP_S:
            bursts[-1]["last_ts"] = ts
            bursts[-1]["count"] += 1
        else:
            bursts.append({"first_ts": ts, "last_ts": ts, "count": 1})
    return bursts


def _windows(exclude) -> tuple:
    out = []
    try:
        for item in exclude or ():
            try:
                start, end = _number(item[0]), _number(item[1])
            except (TypeError, IndexError, KeyError):
                continue
            if start is not None and end is not None and end >= start:
                out.append((start, end))
    except TypeError:
        pass
    return tuple(sorted(set(out)))


_cache: dict = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    """Forget every cached driver_error_bursts answer."""
    with _cache_lock:
        _cache.clear()


def _copy(result: dict) -> dict:
    return {"ok": result["ok"], "bursts": [dict(b) for b in result["bursts"]],
            "events": result["events"], "covered_days": result["covered_days"]}


def driver_error_bursts(days=30, exclude=None) -> dict:
    """The graphics driver errors of the last `days` days, as bursts:
    {"ok": bool, "bursts": [{"first_ts", "last_ts", "count"}], "events": int,
    "covered_days": int}.

    Errors less than five minutes apart are one burst.  `exclude` is a list of
    (start, end) times; an error inside one of them is left out (of the bursts
    and of "events", which is the number of errors that were counted).
    covered_days is how far back the System log really goes, in whole days and
    at most `days`: 0 means the log is under a day old or its age is unknown,
    so nothing can be said about "the last N days".

    The answer is reused for five minutes (a failed read for 30 seconds);
    reset_cache() forgets it.  Never raises."""
    failed = {"ok": False, "bursts": [], "events": 0, "covered_days": 0}
    try:
        span = _number(days)
        if span is None or span <= 0:
            return dict(failed, ok=span is not None, bursts=[])
        span = min(span, 3650.0)
        windows = _windows(exclude)
        key = (span, windows)
        now = _now()
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and 0 <= now - hit[0] < (CACHE_S if hit[1]["ok"] else CACHE_FAILED_S):
                return _copy(hit[1])
        read = query(now - span * 86400.0, now, kinds=("driver_error",))
        if read["ok"]:
            times = [e["ts"] for e in read["events"] if not any(a <= e["ts"] <= b for a, b in windows)]
            oldest = _oldest_system_ts()
            covered = 0 if oldest is None else int(min(span, max(0.0, (now - oldest) / 86400.0)))
            result = {"ok": True, "bursts": _chain(times), "events": len(times), "covered_days": covered}
        else:
            result = dict(failed, bursts=[])
        with _cache_lock:
            if len(_cache) >= 16:
                _cache.clear()
            _cache[key] = (now, result)
        return _copy(result)
    except Exception as e:
        _warn("event log: counting driver errors failed (%s: %s)" % (type(e).__name__, e))
        return dict(failed, bursts=[])
