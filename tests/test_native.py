"""The native helper (core/native.py and the Rust library in native/): that the
app gets the same answers from it as it got from PowerShell, nvidia-smi and
ping, that nothing is started when it answers, and that every caller falls
back to the old way when the helper is missing, old, or wrong.

The real library is never loaded by the app in these tests: the sandbox
answers for it from m.NATIVE (empty = "not installed"), and for its two
actions from m.NATIVE_ACTS.  The loader and the call itself are tested against
a stand-in library; the last test asks the real one a few read-only questions,
on a PC where it has been built, and has it trim this test's own process (the
one action a test ever carries out for real).

The start-up readings have a file of their own: tests/test_native_startup.py.
"""
import sandbox as m

import ctypes
import json
import os
import re
import sys
import tempfile
import types

from core import native as N

GP = m.app("core.game_profiles")
HW = m.app("core.hw_monitor")
from core import gpu_overclock as GO, idle_mode as IM, net_monitor as NM         # noqa: E402

GP._get_foreground_pid = lambda: FOREGROUND[0]
FOREGROUND = [0]
MB = 1048576


# ═════════════════════════════════════════════════════════════════════
# The loader and the call, against a stand-in for the library
# ═════════════════════════════════════════════════════════════════════
class _Fn:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *a):
        return self.fn(*a)


class FakeLib:
    """What ctypes.WinDLL returns for the helper: the three exported
    functions.  `answer` is what the library replies with: a value (wrapped as
    a good answer), raw bytes, or a callable taking the request.  `acted`
    lists the requests that came in through the door for actions."""

    def __init__(self, abi=N.ABI, answer=None):
        self.requests, self.sizes, self.answer, self.acted = [], [], answer, []
        self.vispora_native_abi = _Fn(lambda: abi)
        self.vispora_native_call = _Fn(self._call)
        self.vispora_native_act = _Fn(self._act)

    def _act(self, request, length, buf, cap):
        self.acted.append(json.loads(request[:length].decode("utf-8")))
        if cap < 4096:
            return -4096                    # as the library does: refused before anything is done
        return self._call(request, length, buf, cap)

    def _call(self, request, length, buf, cap):
        req = json.loads(request[:length].decode("utf-8"))
        self.requests.append(req)
        self.sizes.append(cap)
        a = self.answer(req) if callable(self.answer) else self.answer
        if isinstance(a, int):
            return a
        out = a if isinstance(a, bytes) else json.dumps({"ok": True, "data": a}).encode("utf-8")
        if len(out) > cap:
            return -len(out)
        ctypes.memmove(buf, out, len(out))
        return len(out)


def with_library(lib):
    """Put a stand-in library in the loader's place and return the real call."""
    N._state.update(tried=True, lib=lib, why="", path="(test)", buffer=N._FIRST_BUFFER, crashes=0)
    N._complained.clear()
    return m.real_native_call


def log_lines(word):
    from core.utils import get_log_buffer
    return [e["msg"] for e in get_log_buffer() if word in e["msg"]]


def test_the_sandbox_keeps_the_real_library_out():
    m.fresh()
    assert N.call is not m.real_native_call and not N.available()
    assert N.windowed_processes() is None and N.gpu_live() is None and N.process_io() is None
    assert N.cpu_clock_mhz() is None and N.cpu_times() is None and N.wmi("root\\cimv2", "SELECT * FROM X") is None
    assert N.ping("1.1.1.1") is None
    assert [q for q, _ in m.NATIVE_CALLS] == ["windowed_processes", "gpu_live", "process_io", "cpu_clock",
                                              "cpu_times", "wmi", "ping"]


def test_the_library_is_looked_for_in_one_fixed_place():
    here = os.path.dirname(os.path.dirname(os.path.abspath(N.__file__)))
    assert N.dll_path() == os.path.join(here, "native", "target", "release", "vispora_native.dll")
    sys.frozen, sys._MEIPASS = True, r"C:\Temp\_MEI1234"
    try:
        assert N.dll_path() == r"C:\Temp\_MEI1234\vispora_native.dll"
    finally:
        del sys.frozen, sys._MEIPASS
    # No setting and no environment variable can point it elsewhere: the app
    # runs elevated, and the library is code.
    source = open(N.__file__, encoding="utf-8").read()
    assert "os.environ" not in source and "getenv" not in source


def test_a_missing_library_is_simply_not_used():
    lib, why = m.real_native_open(os.path.join(m.ROOT, "nowhere", "vispora_native.dll"))
    assert lib is None and why == "not installed"
    N._state.update(tried=True, lib=None, why=why)
    assert m.real_native_call("windowed_processes") is None


class _Ctypes:
    """ctypes as core.native sees it while a test loads a stand-in library."""

    def __init__(self, lib):
        self._lib = lib

    def WinDLL(self, path):
        if isinstance(self._lib, Exception):
            raise self._lib
        return self._lib

    def __getattr__(self, name):
        return getattr(ctypes, name)


def _open_standing_in(lib):
    path = os.path.join(tempfile.mkdtemp(dir=m.ROOT), "vispora_native.dll")
    open(path, "wb").close()
    real, N.ctypes = N.ctypes, _Ctypes(lib)
    try:
        return m.real_native_open(path)
    finally:
        N.ctypes = real


def test_a_library_of_another_version_is_not_used():
    lib, why = _open_standing_in(FakeLib())
    assert lib is not None and why == ""
    lib, why = _open_standing_in(FakeLib(abi=N.ABI + 1))
    assert lib is None and f"version {N.ABI + 1}" in why
    lib, why = _open_standing_in(OSError(193, "%1 is not a valid Win32 application"))
    assert lib is None and "could not be loaded" in why
    lib, why = _open_standing_in(types.SimpleNamespace())           # some other DLL of that name
    assert lib is None and "could not be loaded" in why
    # The version before this one had no door for actions.  It says so by its
    # number, and is refused for that, not for the missing function.
    old = FakeLib(abi=N.ABI - 1)
    del old.vispora_native_act
    lib, why = _open_standing_in(old)
    assert lib is None and f"version {N.ABI - 1}" in why
    # The right number without that door is not our library.
    broken = FakeLib()
    del broken.vispora_native_act
    lib, why = _open_standing_in(broken)
    assert lib is None and "could not be loaded" in why


def test_the_abi_number_is_the_one_the_library_is_built_with():
    rust = open(os.path.join(m.REPO, "native", "src", "lib.rs"), encoding="utf-8").read()
    found = re.search(r"pub const ABI: u32 = (\d+);", rust)
    assert found and int(found.group(1)) == N.ABI


def test_a_question_and_its_answer():
    lib = FakeLib(answer=lambda req: {"asked": req})
    call = with_library(lib)
    assert call("ping", target="1.1.1.1", timeout_ms=500) == {"asked": {"q": "ping", "target": "1.1.1.1",
                                                                        "timeout_ms": 500}}
    assert lib.requests == [{"q": "ping", "target": "1.1.1.1", "timeout_ms": 500}]


def test_a_long_answer_gets_a_larger_buffer():
    rows = [{"pid": i, "name": "process-%04d.exe" % i, "other_bytes": i * 1000} for i in range(4000)]
    lib = FakeLib(answer=rows)
    call = with_library(lib)
    assert call("process_io") == rows
    assert len(lib.sizes) == 2 and lib.sizes[0] == N._FIRST_BUFFER and lib.sizes[1] > lib.sizes[0]
    assert call("process_io") == rows and len(lib.sizes) == 3, "the larger size is remembered"
    with_library(FakeLib(answer=-(N._MAX_BUFFER + 1)))
    assert m.real_native_call("process_io") is None, "an answer it could never hold"


def test_an_answer_that_is_not_one_is_none():
    for raw in (b"not json", b"[1,2]", b'{"ok":false,"err":"no NVIDIA driver"}', b'{"data":[1]}', b"\xff\xfe", 0):
        call = with_library(FakeLib(answer=raw))
        assert call("gpu_live") is None, raw
    assert with_library(FakeLib(answer=b'{"ok":true,"data":{"Value":NaN}}'))("wmi") == {"Value": None}


def test_a_failure_is_logged_once_not_at_every_tick():
    call = with_library(FakeLib(answer=b'{"ok":false,"err":"no NVIDIA driver on this PC"}'))
    before = len(log_lines("no NVIDIA driver on this PC"))
    for _ in range(50):
        assert call("gpu_live") is None
    assert len(log_lines("no NVIDIA driver on this PC")) == before + 1


def test_a_library_that_keeps_crashing_is_switched_off():
    def boom(req):
        raise OSError("exception: access violation reading 0x0000000000000000")
    lib = FakeLib(answer=boom)
    call = with_library(lib)
    for _ in range(N._GIVE_UP_AFTER):
        assert call("gpu_live") is None
    assert N._state["lib"] is None and "switched off" in N._state["why"]
    assert call("gpu_live") is None and len(lib.requests) == N._GIVE_UP_AFTER, "it is not asked again"
    # One crash among good answers does not switch it off.
    flaky = {"n": 0}

    def sometimes(req):
        flaky["n"] += 1
        if flaky["n"] % 3 == 0:
            raise OSError("exception: access violation")
        return {"ok": 1}
    call = with_library(FakeLib(answer=sometimes))
    assert [call("x") for _ in range(12)].count(None) == 4 and N._state["lib"] is not None


# ═════════════════════════════════════════════════════════════════════
# Actions: their own door, asked once
# ═════════════════════════════════════════════════════════════════════
def test_an_action_goes_through_its_own_door():
    lib = FakeLib(answer=lambda req: {"processes": 300, "trimmed": 250} if req.get("do") else {"asked": req})
    with_library(lib)
    assert m.real_native_act("trim_working_sets") == {"processes": 300, "trimmed": 250}
    assert lib.acted == [{"do": "trim_working_sets"}], "sent as an action, through the action door"
    assert "q" not in lib.acted[0]
    # A question never arrives there.
    assert m.real_native_call("process_io") == {"asked": {"q": "process_io"}}
    assert len(lib.acted) == 1


def test_an_action_is_never_asked_twice():
    # The answer does not fit: a question is asked again with more room, an
    # action is not (it would be carried out again).
    lib = FakeLib(answer=-100_000)
    with_library(lib)
    assert m.real_native_act("purge_standby_list") is None
    assert len(lib.acted) == 1 and lib.sizes == [N._ACTION_BUFFER]
    assert N._ACTION_BUFFER >= 4096, "the library refuses an action a smaller buffer"
    # The library gave up, answered an error, or crashed: None, and asked once.
    for answer in (0, b'{"ok":false,"err":"the standby list can only be cleared with administrator rights"}'):
        lib = FakeLib(answer=answer)
        with_library(lib)
        assert m.real_native_act("purge_standby_list") is None and len(lib.acted) == 1

    def boom(req):
        raise OSError("exception: access violation")
    lib = FakeLib(answer=boom)
    with_library(lib)
    assert m.real_native_act("trim_working_sets") is None and len(lib.acted) == 1


def test_the_sandbox_keeps_the_actions_out_too():
    m.fresh()
    assert N.act is not m.real_native_act
    assert N.trim_working_sets() is None and N.purge_standby_list() is False
    assert m.NATIVE_ACTED == [("trim_working_sets", {}), ("purge_standby_list", {})]
    assert m.NATIVE_CALLS == [], "an action is never sent as a question"
    m.NATIVE_ACTS.update(trim_working_sets={"processes": 310, "trimmed": 262, "debug_privilege": True},
                         purge_standby_list={"purged": True})
    assert N.trim_working_sets() == {"processes": 310, "trimmed": 262, "reached_services": True}
    assert N.purge_standby_list() is True
    m.NATIVE_ACTS["trim_working_sets"] = {"processes": 310, "trimmed": 40}
    assert N.trim_working_sets()["reached_services"] is False, "not said, so not assumed"
    for bad in ({"processes": "many", "trimmed": 1}, {"trimmed": 1}, [1, 2], "done"):
        m.NATIVE_ACTS["trim_working_sets"] = bad
        assert N.trim_working_sets() is None, bad
    for bad in ({"purged": False}, {"purged": 1}, {}, "yes"):
        m.NATIVE_ACTS["purge_standby_list"] = bad
        assert N.purge_standby_list() is False, bad


def test_only_the_two_memory_actions_use_the_action_door():
    source = open(N.__file__, encoding="utf-8").read()
    assert sorted(re.findall(r'\bact\("(\w+)"', source)) == ["purge_standby_list", "trim_working_sets"]
    rust = open(os.path.join(m.REPO, "native", "src", "lib.rs"), encoding="utf-8").read()
    actions = rust[rust.index("fn dispatch_action"):rust.index("// The three functions the DLL exports")]
    questions = rust[rust.index("fn dispatch("):rust.index("fn dispatch_action")]
    assert sorted(re.findall(r'^        "(\w+)" =>', actions, re.M)) == ["purge_standby_list", "trim_working_sets"]
    assert "actions::" not in questions, "no question can reach the code that changes something"
    # And the command-line tool only asks questions.
    tool = open(os.path.join(m.REPO, "native", "src", "main.rs"), encoding="utf-8").read()
    assert "vispora_native::answer(" in tool and "vispora_native::act(" not in tool


# ═════════════════════════════════════════════════════════════════════
# The answers are checked before anything uses them
# ═════════════════════════════════════════════════════════════════════
def test_answers_of_the_wrong_shape_are_refused():
    m.fresh()
    good = [{"pid": 10, "name": "cs2", "ws": 5 * MB}]
    m.NATIVE["windowed_processes"] = good
    assert N.windowed_processes() == good
    for bad in ({"pid": 10}, [{"pid": "10", "name": "cs2", "ws": 1}], [{"pid": 10, "name": "cs2"}],
                [{"pid": True, "name": "cs2", "ws": 1}], [["cs2"]], "cs2", [{"pid": float("nan"), "name": "a", "ws": 1}]):
        m.NATIVE["windowed_processes"] = bad
        assert N.windowed_processes() is None, bad
    m.NATIVE["process_io"] = [{"pid": 4, "name": "System", "other_bytes": 12}]
    assert N.process_io() == [{"pid": 4, "name": "System", "other_bytes": 12}]
    m.NATIVE["process_io"] = [{"pid": 4, "name": "System"}]
    assert N.process_io() is None
    m.NATIVE["gpu_live"] = {"temp_c": 61, "gpu_util_pct": 40, "power_mw": None, "fan_pct": "33"}
    assert N.gpu_live() is None, "a text where a number belongs"
    m.NATIVE["gpu_live"] = {"temp_c": 61, "gpu_util_pct": 40, "extra": 1}
    live = N.gpu_live()
    assert set(live) == set(N._GPU_FIELDS) and live["temp_c"] == 61 and live["power_mw"] is None
    m.NATIVE["cpu_clock"] = {"mhz": 0}
    assert N.cpu_clock_mhz() is None
    m.NATIVE["cpu_clock"] = {"mhz": 3700}
    assert N.cpu_clock_mhz() == 3700
    m.NATIVE["cpu_times"] = {"total": 0, "idle": 0, "dpc": 0, "interrupt": 0, "processors": 24}
    assert N.cpu_times() is None
    m.NATIVE["cpu_times"] = {"total": 10, "idle": 5, "dpc": 1, "interrupt": 1}
    assert N.cpu_times() is None
    m.NATIVE["wmi"] = [{"Name": "CPU Package", "Value": 51.0}, "junk"]
    assert N.wmi("root\\x", "SELECT * FROM Sensor") is None
    m.NATIVE["wmi"] = []
    assert N.wmi("root\\x", "SELECT * FROM Sensor") == []


def test_start_up_answers_of_the_wrong_shape_are_refused():
    m.fresh()
    m.NATIVE["gpu_identity"] = {"name": " NVIDIA GeForce RTX 5070 Ti ", "driver_version": "621.40",
                                "mem_total_bytes": 17094934528}
    assert N.gpu_identity() == {"name": "NVIDIA GeForce RTX 5070 Ti", "driver_version": "621.40",
                                "mem_total_bytes": 17094934528}
    m.NATIVE["gpu_identity"] = {"name": "Old Card", "driver_version": None, "mem_total_bytes": None}
    assert N.gpu_identity() == {"name": "Old Card", "driver_version": None, "mem_total_bytes": None}
    for bad in ({"name": ""}, {"name": 5}, {"driver_version": "1.0"}, ["x"], {"name": "x", "mem_total_bytes": "16 GB"}):
        m.NATIVE["gpu_identity"] = bad
        assert N.gpu_identity() is None, bad

    m.NATIVE["service_state"] = {"installed": True, "state": 4}
    assert N.service_state("Spooler") == {"installed": True, "running": True}
    m.NATIVE["service_state"] = {"installed": True, "state": 1}
    assert N.service_state("Spooler") == {"installed": True, "running": False}
    m.NATIVE["service_state"] = {"installed": False, "state": None}
    assert N.service_state("NoSuch") == {"installed": False, "running": False}
    for bad in ({"installed": "yes", "state": 4}, {"state": 4}, {"installed": True, "state": None},
                {"installed": True, "state": "RUNNING"}, [True]):
        m.NATIVE["service_state"] = bad
        assert N.service_state("Spooler") is None, bad
    del m.NATIVE_CALLS[:]
    assert N.service_state("") is None and N.service_state(None) is None and m.NATIVE_CALLS == []

    m.NATIVE["power_scheme"] = {"guid": m.HIGH_PERF.upper(), "schemes": [m.BALANCED, m.HIGH_PERF]}
    assert N.power_scheme() == {"guid": m.HIGH_PERF, "schemes": [m.BALANCED, m.HIGH_PERF]}
    for bad in ({"guid": "High performance", "schemes": []}, {"guid": m.BALANCED}, {"guid": m.BALANCED, "schemes": ["x"]},
                {"schemes": [m.BALANCED]}, m.BALANCED):
        m.NATIVE["power_scheme"] = bad
        assert N.power_scheme() is None, bad

    m.NATIVE["power_setting"] = {"scheme": m.BALANCED, "ac": 100, "dc": 5, "min": 0, "max": 100, "increment": 1}
    assert N.power_setting("SCHEME_CURRENT", m.PROC_SUB, m.PROC_MAX) == {"ac": 100, "dc": 5, "max": 100}
    assert m.NATIVE_CALLS[-1] == ("power_setting", {"scheme": "current", "sub": m.PROC_SUB, "setting": m.PROC_MAX})
    m.NATIVE["power_setting"] = {"ac": 1, "dc": 0, "min": None, "max": None, "increment": None}
    assert N.power_setting(m.BALANCED, m.PROC_SUB, m.IDLE) == {"ac": 1, "dc": 0, "max": None}
    for bad in ({"ac": 1}, {"ac": "1", "dc": 0}, [1, 0], {"ac": 1, "dc": 0, "max": "100"}):
        m.NATIVE["power_setting"] = bad
        assert N.power_setting(m.BALANCED, m.PROC_SUB, m.IDLE) is None, bad
    # powercfg's own names are not sent: the caller turns them into GUIDs, and
    # a name it does not know goes to powercfg, which does.
    m.NATIVE["power_setting"] = {"ac": 1, "dc": 0}
    del m.NATIVE_CALLS[:]
    for scheme, sub, setting in (("SCHEME_CURRENT", "SUB_PROCESSOR", m.IDLE), (m.BALANCED, m.PROC_SUB, "IDLEDISABLE"),
                                 ("SCHEME_BALANCED", m.PROC_SUB, m.IDLE), (m.BALANCED, m.PROC_SUB, m.IDLE + "; calc"),
                                 (None, m.PROC_SUB, m.IDLE)):
        assert N.power_setting(scheme, sub, setting) is None, (scheme, sub, setting)
    assert m.NATIVE_CALLS == []


def test_ping_only_takes_an_ipv4_address():
    m.fresh()
    m.NATIVE["ping"] = {"ok": True, "ms": 0}
    assert N.ping("1.1.1.1") == {"ok": True, "ms": 1}, "under a millisecond reads 1, as ping.exe's '<1ms' did"
    assert m.NATIVE_CALLS == [("ping", {"target": "1.1.1.1", "timeout_ms": 500})]
    del m.NATIVE_CALLS[:]
    for target in ("example.com", "1.1.1", "1.1.1.256", "::1", "1.1.1.1; calc", "", None, 8):
        assert N.ping(target) is None, target
    assert m.NATIVE_CALLS == [], "a host name is ping.exe's to look up: the helper is not asked"
    m.NATIVE["ping"] = {"ok": False}
    assert N.ping("10.255.255.1") == {"ok": False}
    m.NATIVE["ping"] = {"ok": True}
    assert N.ping("1.1.1.1") is None


# ═════════════════════════════════════════════════════════════════════
# Game mode's window scan
# ═════════════════════════════════════════════════════════════════════
PROCESSES = [   # (pid, image name, working set in bytes)
    (4242, "cs2.exe", 3012 * MB + 700_000),
    (5001, "explorer.exe", 180 * MB),
    (5002, "chrome.exe", 950 * MB),
    (5003, "Discord.exe", 400 * MB),
    (5010, "valorant-win64-shipping.exe", 4100 * MB),
    (5011, "notepad.exe", 12 * MB),
]


def _short(name):                       # the name Get-Process prints
    return name[:-4] if name.lower().endswith(".exe") else name


def powershell_answers(processes):
    """Get-Process's answer for these processes, as PowerShell prints it."""
    rows = [{"Id": pid, "ProcessName": _short(name), "WS": round(ws / MB)} for pid, name, ws in processes]
    rows.sort(key=lambda r: (r["ProcessName"].lower(), r["Id"]))
    text = "" if not rows else json.dumps(rows[0] if len(rows) == 1 else rows, separators=(",", ":"))
    m.PS_HOOKS.append(lambda c: m.R(text) if "MainWindowHandle" in c else None)


def native_answers(processes):
    m.NATIVE["windowed_processes"] = [{"pid": pid, "name": _short(name), "ws": ws} for pid, name, ws in processes]


def test_the_window_scan_finds_the_same_games_either_way():
    for foreground in (0, 4242, 5010):
        FOREGROUND[0] = foreground
        m.fresh()
        powershell_answers(PROCESSES)
        old = GP._scan_windowed_games()
        assert m.ps_ran("MainWindowHandle") and not m.NATIVE
        m.fresh()
        native_answers(list(reversed(PROCESSES)))               # (whatever order the helper lists them in)
        new = GP._scan_windowed_games()
        assert new == old, (foreground, new, old)
        assert m.PSC == [] and m.CMDS == [], "nothing was started"
        assert [c["exe"] for c in new] == (["cs2.exe", "valorant-win64-shipping.exe"] if foreground == 4242
                                           else ["valorant-win64-shipping.exe", "cs2.exe"])
        assert new[0]["foreground"] == (foreground != 0)
    FOREGROUND[0] = 0


def test_the_working_set_is_rounded_the_way_powershell_rounded_it():
    m.fresh()
    # [int]2.5 is 2 and [int]3.5 is 4 in PowerShell (to the even number).
    native_answers([(1, "cs2.exe", int(2.5 * MB)), (2, "csgo.exe", int(3.5 * MB)), (3, "valorant.exe", 6 * MB - 1)])
    assert {c["exe"]: c["ws_mb"] for c in GP._scan_windowed_games()} == {"cs2.exe": 2, "csgo.exe": 4, "valorant.exe": 6}


def test_games_with_the_same_score_keep_get_process_order():
    m.fresh()
    same = [(30, "valorant.exe", 100 * MB), (10, "cs2.exe", 100 * MB), (20, "cs2.exe", 100 * MB)]
    powershell_answers(same)
    old = [c["pid"] for c in GP._scan_windowed_games()]
    m.fresh()
    native_answers(same)
    assert [c["pid"] for c in GP._scan_windowed_games()] == old == [10, 20, 30]


def test_no_window_at_all_is_no_game_not_a_failed_scan():
    m.fresh()
    native_answers([])
    assert GP._scan_windowed_games() == [] and GP.running_game_candidates() == [] and m.PSC == []
    native_answers([(5001, "explorer.exe", 180 * MB)])
    assert GP._scan_windowed_games() == [] and GP.get_active_games() == []


def test_without_the_helper_powershell_is_asked_as_before():
    m.fresh()
    powershell_answers(PROCESSES[:1])                           # one process: PowerShell prints an object, not a list
    assert [c["exe"] for c in GP._scan_windowed_games()] == ["cs2.exe"]
    assert len(m.ps_ran("MainWindowHandle")) == 1 and m.NATIVE_CALLS[0][0] == "windowed_processes"
    m.fresh()
    m.PS_HOOKS.append(lambda c: m.F("Timed out") if "MainWindowHandle" in c else None)
    assert GP._scan_windowed_games() is None and GP.running_game_candidates() is None, "a failed scan is not 'no game'"
    m.fresh()
    m.NATIVE["windowed_processes"] = [{"pid": 1}]               # a helper that answers nonsense
    powershell_answers(PROCESSES)
    assert len(GP._scan_windowed_games()) == 2 and m.ps_ran("MainWindowHandle")


def test_the_other_watchers_use_the_same_scan():
    m.fresh()
    native_answers(PROCESSES)
    assert [g["exe"] for g in GP.running_game_candidates()] == ["valorant-win64-shipping.exe", "cs2.exe"]
    assert IM._a_game_is_running() is True
    native_answers(PROCESSES[1:4])
    assert GP.running_game_candidates() == [] and IM._a_game_is_running() is False
    assert m.PSC == [], "Idle Mode's check every 20 seconds starts nothing either"


# ═════════════════════════════════════════════════════════════════════
# The graphics card's live readings
# ═════════════════════════════════════════════════════════════════════
LIVE = {"temp_c": 55, "gpu_util_pct": 64, "mem_util_pct": 10, "core_mhz": 3225, "mem_mhz": 16226,
        "core_max_mhz": 3090, "mem_max_mhz": 14001, "core_app_mhz": None, "mem_app_mhz": None, "power_mw": 147560,
        "power_limit_mw": 300000, "fan_pct": 33, "mem_used_bytes": int(10021.64 * MB), "mem_total_bytes": 16303 * MB}
# (10021.64 MiB in use: nvidia-smi prints that rounded, 10022, as measured against the real card)
SMI = {"clocks.current.graphics": "3225", "clocks.current.memory": "16226", "temperature.gpu": "55.10",
       "power.draw": "147.56", "utilization.gpu": "64", "utilization.memory": "10", "fan.speed": "33",
       "power.limit": "300.00", "clocks.applications.graphics": "[N/A]", "clocks.applications.memory": "[N/A]",
       "clocks.max.graphics": "3090", "clocks.max.memory": "14001", "memory.used": "10022", "memory.total": "16303"}


def nvidia_smi_answers(values=SMI):
    def hook(a):
        if a[0] != "nvidia-smi":
            return None
        fields = next(x for x in a if x.startswith("--query-gpu=")).split("=", 1)[1].split(",")
        return m.R(", ".join(values[f.strip()] for f in fields))
    m.CMD_HOOKS.append(hook)


def test_live_gpu_state_is_the_same_either_way():
    m.fresh()
    nvidia_smi_answers()
    old = GO.read_live_state()
    assert old["ok"] and len(m.ran("nvidia-smi")) == 1
    m.fresh()
    m.NATIVE["gpu_live"] = dict(LIVE)
    new = GO.read_live_state()
    assert new == old, (new, old)
    assert m.CMDS == [] and m.PSC == [], "no nvidia-smi was started"
    assert (new["temp_c"], new["power_w"], new["power_limit_w"], new["core_app_mhz"]) == (55, 147.56, 300.0, 0)
    assert all(type(new[k]) is type(old[k]) for k in old), "ints stay ints and watts stay floats"


def test_a_reading_without_the_temperature_is_not_taken():
    m.fresh()
    nvidia_smi_answers()
    m.NATIVE["gpu_live"] = dict(LIVE, temp_c=None)
    assert GO.read_live_state()["temp_c"] == 55 and len(m.ran("nvidia-smi")) == 1, \
        "the temperature ceiling reads this: nvidia-smi is asked instead"
    m.fresh()
    m.NATIVE["gpu_live"] = dict(LIVE, temp_c=None)
    assert GO.read_live_state() == {"ok": False, "err": "No NVIDIA GPU / nvidia-smi missing"}
    m.fresh()
    nvidia_smi_answers()
    m.NATIVE["gpu_live"] = {"temp_c": "hot"}                    # nonsense from the helper
    assert GO.read_live_state()["temp_c"] == 55


def test_what_the_card_does_not_report_is_zero_as_from_nvidia_smi():
    m.fresh()
    m.NATIVE["gpu_live"] = {"temp_c": 40}
    live = GO.read_live_state()
    assert live["ok"] and live["temp_c"] == 40
    assert all(live[k] == 0 for k in live if k not in ("ok", "temp_c"))
    assert isinstance(live["power_w"], float) and isinstance(live["fan_pct"], int)


def test_the_monitor_page_gpu_tile_is_the_same_either_way():
    m.fresh()
    nvidia_smi_answers()
    old = HW._sample_gpu()
    assert old == {"temp": 55, "load": 64, "clock": 3225, "vram_pct": 61.5}, \
        "a temperature printed with decimals is read (int('55.10') used to blank the whole tile)"
    assert not m.ps_ran("LibreHardwareMonitor"), "and so the sensor fallback is not needed"
    m.fresh()
    m.NATIVE["gpu_live"] = dict(LIVE)
    assert HW._sample_gpu() == old and m.CMDS == [] and m.PSC == []


def test_a_driver_install_makes_the_helper_let_go_of_the_nvidia_library():
    from core import driver_updater as DU
    m.fresh()
    DU._release_gpu_library()                                   # no helper: nothing to do, nothing raised
    assert m.NATIVE_CALLS == [("gpu_release", {"hold_s": DU._GPU_LIBRARY_HOLD_S})]
    m.fresh()
    m.NATIVE["gpu_release"] = lambda hold_s: {"released": True, "hold_s": hold_s}
    DU._release_gpu_library()
    assert m.NATIVE_CALLS == [("gpu_release", {"hold_s": 900})] and N.gpu_release(-5) is True
    assert m.NATIVE_CALLS[-1] == ("gpu_release", {"hold_s": 0})
    # ...and it is asked before the installer is started, not after.
    import inspect
    body = inspect.getsource(DU.install_driver)
    assert 0 < body.index("_release_gpu_library()") < body.index('subprocess.Popen(["explorer.exe"')


# ═════════════════════════════════════════════════════════════════════
# The hardware monitor's other readings
# ═════════════════════════════════════════════════════════════════════
SENSORS = [
    {"Name": "CPU Core #1", "SensorType": "Temperature", "Value": 48.0, "Identifier": "/intelcpu/0/temperature/0"},
    {"Name": "CPU Package", "SensorType": "Temperature", "Value": 52.375, "Identifier": "/intelcpu/0/temperature/8"},
    {"Name": "Core (Tctl/Tdie)", "SensorType": "Temperature", "Value": 61.0, "Identifier": "/amdcpu/0/temperature/2"},
    {"Name": "GPU Core", "SensorType": "Temperature", "Value": 55.0, "Identifier": "/gpu-nvidia/0/temperature/0"},
    {"Name": "GPU Core", "SensorType": "Load", "Value": 64.0, "Identifier": "/gpu-nvidia/0/load/0"},
    {"Name": "GPU Core", "SensorType": "Clock", "Value": 3225.4, "Identifier": "/gpu-nvidia/0/clock/0"},
    {"Name": "CPU Total", "SensorType": "Load", "Value": 12.0, "Identifier": "/intelcpu/0/load/0"},
]
ASKED: list = []


def wmi_answers(tables):
    """tables: {namespace lower: rows, or a callable(query) -> rows}.  A
    namespace that is not listed does not exist (the helper answers [])."""
    del ASKED[:]

    def answer(namespace, query):
        ASKED.append((namespace, query))
        rows = tables.get(namespace.lower(), [])
        rows = rows(query) if callable(rows) else rows
        if "sensortype = 'temperature'" in query.lower():
            rows = [r for r in rows if r.get("SensorType") == "Temperature"]
        return [dict(r) for r in rows]
    m.NATIVE["wmi"] = answer


def lhm_powershell_answers(rows):
    """What the PowerShell pipelines print for these LibreHardwareMonitor rows."""
    def hook(c):
        if "root/LibreHardwareMonitor" not in c:
            return None
        if "Identifier -like '*gpu*'" in c:
            picked = [r for r in rows if "gpu" in r["Identifier"].lower()]
            return m.R(json.dumps(picked) if picked else "")
        names = re.findall(r"-like '\*([^*']+)\*'", c)
        picked = [r for r in rows if r["SensorType"] == "Temperature"
                  and any(n.lower() in r["Name"].lower() for n in names)]
        return m.R(json.dumps({"Value": picked[0]["Value"]}) if picked else "")
    m.PS_HOOKS.append(hook)


def test_cpu_temperature_is_the_same_either_way():
    for rows in (SENSORS, SENSORS[2:], SENSORS[3:], [dict(SENSORS[1], Value=None)], [dict(SENSORS[1], Value=28.0)],
                 [dict(SENSORS[1], Value=250.0)], []):
        m.fresh()
        lhm_powershell_answers(rows)
        old = HW._read_cpu_temp_via_lhm()
        m.fresh()
        wmi_answers({"root\\librehardwaremonitor": rows})
        assert HW._read_cpu_temp_via_lhm() == old, (rows, old)
        assert m.PSC == [], "the helper asked WMI itself"
    m.fresh()
    wmi_answers({"root\\librehardwaremonitor": SENSORS})
    assert HW._read_cpu_temp_via_lhm() == 52.4, "the first sensor named Package, CCD1 or Tctl"
    assert ASKED[0][0] == "root\\LibreHardwareMonitor"


def test_the_other_temperature_sources_are_asked_without_powershell_too():
    m.fresh()
    wmi_answers({"root\\openhardwaremonitor": [{"Name": "CPU Package", "SensorType": "Temperature", "Value": 47.26}],
                 "root\\wmi": [{"CurrentTemperature": 3200}, {"CurrentTemperature": 3300}],
                 "root\\cimv2": [{"HighPrecisionTemperature": 3102}, {"HighPrecisionTemperature": 3256},
                                 {"HighPrecisionTemperature": None}]})
    assert HW._read_cpu_temp_via_ohm() == 47.3
    assert HW._read_cpu_temp_via_msacpi() == 46.9, "the first zone, tenths of a kelvin (320.0 K)"
    assert HW._read_cpu_temp_via_perf_thermal() == 52.5, "the warmest zone"
    wmi_answers({"root\\wmi": [{"CurrentTemperature": 3010}], "root\\cimv2": [{"HighPrecisionTemperature": 0}]})
    assert HW._read_cpu_temp_via_msacpi() is None, "27.85 degrees is the placeholder of a zone without a sensor"
    assert HW._read_cpu_temp_via_perf_thermal() is None and HW._read_cpu_temp_via_ohm() is None
    assert m.PSC == []
    # Without the helper each of them is a PowerShell call, as before.
    m.fresh()
    m.PS_HOOKS.append(lambda c: m.R('{"CurrentTemperature":3200}') if "MSAcpi" in c else None)
    assert HW._read_cpu_temp_via_msacpi() == 46.9 and len(m.PSC) == 1


def test_gpu_sensors_from_librehardwaremonitor_are_the_same_either_way():
    m.fresh()
    lhm_powershell_answers(SENSORS)
    old = HW._read_gpu_via_lhm()
    assert old == {"temp": 55.0, "load": 64.0, "clock": 3225}
    m.fresh()
    wmi_answers({"root\\librehardwaremonitor": SENSORS})
    assert HW._read_gpu_via_lhm() == old and m.PSC == []
    wmi_answers({"root\\librehardwaremonitor": SENSORS[:3]})    # no GPU sensor at all
    assert HW._read_gpu_via_lhm() == {"temp": None, "load": None, "clock": None} and m.PSC == []


def _times(total, dpc, interrupt, processors=24):
    return {"total": total, "idle": total // 2, "dpc": dpc, "interrupt": interrupt, "processors": processors}


def cpu_times_answers(readings):
    left = list(readings)
    m.NATIVE["cpu_times"] = lambda: left.pop(0) if left else None
    return left


def test_dpc_and_interrupt_time_from_the_helper():
    m.fresh()
    HW._cpu_times_prev.update(totals=None, at=0.0)
    HW._CPU_TIMES_FIRST_WAIT_S = 0.0
    second = 24 * 10_000_000                                    # one second on 24 processors, in 100 ns units
    left = cpu_times_answers([_times(1000 * second, 5_000_000, 2_000_000),
                              _times(1001 * second, 5_000_000 + 240_000, 2_000_000 + 120_000),
                              _times(1002 * second, 5_000_000 + 240_000 + 1_234_567, 2_000_000 + 120_000)])
    first = HW._sample_dpc()
    assert (first["dpc_pct"], first["isr_pct"], first["perfmon_ok"], first["ok"]) == (0.1, 0.05, True, True), first
    assert len(left) == 1, "the first pass reads twice: there was nothing to compare with"
    second_pass = HW._sample_dpc()
    assert (second_pass["dpc_pct"], second_pass["isr_pct"]) == (0.514, 0.0), second_pass
    assert left == [] and m.PSC == [], "one reading a pass from then on, and no Get-Counter"
    assert isinstance(first["dpc_rate"], int) or first["dpc_rate"] is None    # (the DPC rate still comes from the NT call)


def test_dpc_falls_back_to_get_counter():
    m.fresh()
    HW._cpu_times_prev.update(totals=None, at=0.0)
    m.PS_HOOKS.append(lambda c: m.R('{"dpc_pct":0.123,"isr_pct":0.045,"ok":true}') if "DPC Time" in c else None)
    got = HW._sample_dpc()                                      # no helper
    assert (got["dpc_pct"], got["isr_pct"], got["perfmon_ok"]) == (0.123, 0.045, True) and len(m.PSC) == 1
    # A helper whose totals do not move (or that stops answering) is not believed.
    m.fresh()
    HW._cpu_times_prev.update(totals=None, at=0.0)
    HW._CPU_TIMES_FIRST_WAIT_S = 0.0
    cpu_times_answers([_times(500, 1, 1), _times(500, 1, 1)])
    m.PS_HOOKS.append(lambda c: m.R('{"dpc_pct":0.2,"isr_pct":0.1,"ok":true}') if "DPC Time" in c else None)
    assert HW._sample_dpc()["dpc_pct"] == 0.2 and len(m.PSC) == 1
    # A reading from before the monitor was switched off is not "the previous pass".
    m.fresh()
    HW._cpu_times_prev.update(totals=_times(1, 0, 0), at=HW.time.monotonic() - 3600)
    left = cpu_times_answers([_times(10_000, 100, 0), _times(20_000, 200, 0)])
    assert HW._sample_dpc()["dpc_pct"] == 1.0 and left == []


def test_ping_from_the_helper_and_from_ping_exe():
    m.fresh()
    m.NATIVE["ping"] = {"ok": True, "ms": 12}
    assert HW._sample_ping("1.1.1.1") == 12 and m.CMDS == []
    m.NATIVE["ping"] = {"ok": False}
    assert HW._sample_ping("1.1.1.1") is None and m.CMDS == [], "no answer in time: ping.exe is not tried as well"
    m.CMD_HOOKS.append(lambda a: m.R("Reply from 93.184.216.34: bytes=32 time=23ms TTL=57") if a[0] == "ping" else None)
    assert HW._sample_ping("example.com") == 23 and len(m.ran("ping", "example.com")) == 1
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.R("Reply from 1.1.1.1: bytes=32 time=9ms TTL=57") if a[0] == "ping" else None)
    assert HW._sample_ping("1.1.1.1") == 9 and len(m.ran("ping")) == 1, "without the helper"


def test_a_whole_pass_of_the_monitor_starts_nothing():
    m.fresh()
    HW._cpu_times_prev.update(totals=None, at=0.0)
    HW._CPU_TIMES_FIRST_WAIT_S = 0.0
    HW._cpu_temp_source = ""
    # The processor load is read through the NT API by Python itself and needs
    # time to pass between two readings; these passes run back to back.
    real_load, HW._read_cpu_load_via_ntapi = HW._read_cpu_load_via_ntapi, lambda: 12.5
    try:
        _a_whole_pass()
    finally:
        HW._read_cpu_load_via_ntapi = real_load


def _a_whole_pass():
    m.NATIVE.update({"cpu_clock": {"mhz": 3700}, "gpu_live": dict(LIVE), "ping": {"ok": True, "ms": 11}})
    wmi_answers({"root\\librehardwaremonitor": SENSORS})
    ticks = {"n": 0}

    def times():
        ticks["n"] += 1
        return _times(ticks["n"] * 240_000_000, ticks["n"] * 240_000, ticks["n"] * 24_000)
    m.NATIVE["cpu_times"] = times
    for _ in range(3):
        cpu, gpu, dpc, ping = HW._sample_cpu(), HW._sample_gpu(), HW._sample_dpc(), HW._sample_ping("1.1.1.1")
        assert (cpu["clock"], cpu["temp"], cpu["temp_source"]) == (3700, 52.4, "LibreHardwareMonitor")
        assert gpu["temp"] == 55 and ping == 11 and (dpc["dpc_pct"], dpc["isr_pct"]) == (0.1, 0.01)
    assert m.PSC == [] and m.CMDS == [], "no PowerShell, no nvidia-smi, no ping.exe: nine of them a pass before"
    # The same pass without the helper, on the same machine: the old way, and the same readings.
    m.fresh()
    HW._cpu_temp_source = ""
    nvidia_smi_answers()
    lhm_powershell_answers(SENSORS)
    m.PS_HOOKS.append(lambda c: m.R("3700") if "CurrentClockSpeed" in c else None)
    m.PS_HOOKS.append(lambda c: m.R('{"dpc_pct":0.1,"isr_pct":0.01,"ok":true}') if "DPC Time" in c else None)
    m.CMD_HOOKS.append(lambda a: m.R("Reply from 1.1.1.1: bytes=32 time=11ms TTL=57") if a[0] == "ping" else None)
    cpu, gpu, dpc, ping = HW._sample_cpu(), HW._sample_gpu(), HW._sample_dpc(), HW._sample_ping("1.1.1.1")
    assert (cpu["clock"], cpu["temp"], gpu["temp"], dpc["dpc_pct"], ping) == (3700, 52.4, 55, 0.1, 11)
    assert len(m.PSC) == 3 and len(m.CMDS) == 2


# ═════════════════════════════════════════════════════════════════════
# The network monitor's per-process sampling
# ═════════════════════════════════════════════════════════════════════
class _FakePsutil(types.ModuleType):
    def __init__(self):
        super().__init__("psutil")
        self.rows, self.iterated, self.nic = [], 0, [0, 0]

    def net_io_counters(self):
        return types.SimpleNamespace(bytes_recv=self.nic[0], bytes_sent=self.nic[1])

    def process_iter(self, attrs=None):
        self.iterated += 1
        for pid, name, other in self.rows:
            yield types.SimpleNamespace(info={"pid": pid, "name": name,
                                              "io_counters": types.SimpleNamespace(other_bytes=other)})


CLOCK = [1000.0]


def _fresh_network_monitor():
    m.fresh()
    ps = _FakePsutil()
    sys.modules["psutil"] = ps
    CLOCK[0] = 1000.0
    NM.time = types.SimpleNamespace(time=lambda: CLOCK[0])      # the monitor's clock: a second is exactly a second
    NM._wanted = lambda: None           # reading the result does not start the real sampler here
    NM._per_proc.clear()
    NM._history.clear()
    NM._last_nic_rx = NM._last_nic_tx = 0
    NM._last_sample_t = 0.0
    NM._get_active_game_exe = lambda: "cs2.exe"
    return ps


def _two_samples(feed):
    """Two sampling passes one second apart; feed(i) installs the counters of pass i."""
    feed(0)
    NM._sample_once()
    CLOCK[0] += 1.0
    feed(1)
    NM._sample_once()
    return NM.get_live()


TRAFFIC = [  # (pid, name, other-I/O bytes at the first pass, at the second)
    (0, "System Idle Process", 0, 0),
    (4, "System", 1000, 9_000_000),
    (4242, "cs2.exe", 5_000_000, 5_600_000),
    (6001, "OneDrive.exe", 20_000_000, 28_000_000),
    (6002, "chrome.exe", 1_000_000, 1_250_000),
    (6003, "idle.exe", 77, 77),
]


def test_per_process_traffic_is_the_same_either_way():
    ps = _fresh_network_monitor()

    def from_psutil(i):
        ps.rows = [(pid, name, (a, b)[i]) for pid, name, a, b in TRAFFIC]
        ps.nic = [1000 + i * 9_000_000, 500 + i * 400_000]
    old = _two_samples(from_psutil)
    assert ps.iterated == 2 and [r["name"] for r in old["top"]] == ["system", "onedrive.exe", "cs2.exe", "chrome.exe"]

    ps = _fresh_network_monitor()

    def from_helper(i):
        m.NATIVE["process_io"] = [{"pid": pid, "name": name, "other_bytes": (a, b)[i]} for pid, name, a, b in TRAFFIC]
        ps.nic = [1000 + i * 9_000_000, 500 + i * 400_000]
    new = _two_samples(from_helper)
    assert ps.iterated == 0, "psutil did not open a single process"
    assert new == old, (new, old)
    assert new["total_rx_per_sec"] == 9_000_000 and new["top"][1]["bytes_per_sec"] == 8_000_000


def test_a_burst_is_still_noticed_from_the_helper_readings():
    ps = _fresh_network_monitor()
    total = [20_000_000]

    def feed(i):
        total[0] += 8_000_000
        m.NATIVE["process_io"] = [{"pid": 6001, "name": "OneDrive.exe", "other_bytes": total[0]},
                                  {"pid": 4242, "name": "cs2.exe", "other_bytes": total[0] * 2}]
    feed(0)
    NM._sample_once()
    for _ in range(NM.BURST_CONSECUTIVE):
        CLOCK[0] += 1.0
        feed(1)
        NM._sample_once()
    alert = NM.get_live()["burst_alert"]
    assert alert and (alert["pid"], alert["name"], alert["active_game"]) == (6001, "onedrive.exe", "cs2.exe"), alert
    assert ps.iterated == 0
    # A process that has gone is forgotten, as before.
    m.NATIVE["process_io"] = [{"pid": 4242, "name": "cs2.exe", "other_bytes": 1}]
    NM._sample_once()
    assert set(NM._per_proc) == {4242}


# ═════════════════════════════════════════════════════════════════════
# The build, and the real library where it has been built
# ═════════════════════════════════════════════════════════════════════
def test_the_build_bundles_the_library():
    spec = open(os.path.join(m.REPO, "build.spec"), encoding="utf-8").read()
    assert "vispora_native.dll" in spec and "native" in spec
    assert re.search(r"binaries=_pyaudio_binaries \+ _native_binaries", spec), \
        "the library goes beside the app's other bundled files, where core/native.py looks for it"


def test_the_real_library_where_it_has_been_built():
    """Read-only questions to the library itself, and one action on this
    test's own process.  On a PC where it has not been built this only checks
    that its absence is reported as such."""
    path = N.dll_path()
    lib, why = m.real_native_open(path)
    if not os.path.isfile(path):
        assert lib is None and why == "not installed"
        return
    assert lib is not None, f"native/target/release holds a library that cannot be used ({why}): rebuild it"
    call = with_library(lib)
    try:
        procs = call("process_io")
        assert isinstance(procs, list) and len(procs) > 20
        mine = [p for p in procs if p["pid"] == os.getpid()]
        assert mine and mine[0]["name"].lower().startswith("python") and mine[0]["other_bytes"] >= 0, mine
        assert [p["name"] for p in procs if p["pid"] == 0] == ["System Idle Process"]
        windows = call("windowed_processes")
        assert isinstance(windows, list) and all(set(w) >= {"pid", "name", "ws"} for w in windows)
        assert all(not w["name"].lower().endswith(".exe") and w["ws"] > 0 for w in windows), windows
        assert {w["pid"] for w in windows} <= {p["pid"] for p in procs}
        times = call("cpu_times")
        assert times["processors"] == os.cpu_count() and 0 < times["idle"] <= times["total"]
        assert 0 <= times["dpc"] <= times["total"] and 0 <= times["interrupt"] <= times["total"]
        assert call("cpu_clock")["mhz"] > 0
        gpu = call("gpu_live")
        assert gpu is None or (isinstance(gpu, dict) and set(N._GPU_FIELDS) <= set(gpu))
        assert call("no such question") is None
        assert call("wmi", namespace="root\\cimv2", query="SELECT Caption FROM Win32_OperatingSystem")[0]["Caption"]
        assert call("wmi", namespace="root\\no_such_namespace", query="SELECT * FROM Nothing") == []
        # The start-up questions (all of them only read).
        assert call("service_state", name="EventLog") == {"installed": True, "state": 4}
        assert call("service_state", name="VisporaNoSuchService_7f3a") == {"installed": False, "state": None}
        plan = call("power_scheme")
        assert plan["guid"] in plan["schemes"] and len(plan["guid"]) == 36
        cap = call("power_setting", scheme="current", sub=m.PROC_SUB, setting=m.PROC_MAX)
        assert cap["scheme"] == plan["guid"] and 0 <= cap["ac"] <= cap["max"] <= 100, cap
        assert call("power_setting", scheme="current", sub=m.PROC_SUB,
                    setting="12345678-1234-1234-1234-123456789abc") is None
        card = call("gpu_identity")
        assert card is None or (card["name"] and set(card) == {"name", "driver_version", "mem_total_bytes"}), card
        # An action's name is not a question, and the one action tried here is
        # on this test's own process only (its working set is trimmed; Windows
        # hands the pages back as they are used).
        assert call("trim_working_sets") is None and call("purge_standby_list") is None
        done = m.real_native_act("trim_working_sets", pids=[os.getpid()])
        assert done and done["processes"] == 1 and done["trimmed"] == 1, done
        assert m.real_native_act("process_io") is None, "a question is not an action"
    finally:
        N._state.update(tried=True, lib=None, why="(test over)")
