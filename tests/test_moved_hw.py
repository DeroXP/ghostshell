"""What has moved from the backend into the shell: the hardware monitor.

The second piece of step 4 (docs/rust-port.md) is core/hw_monitor.py: the
processor, the graphics card, the memory, the time spent in DPCs and
interrupts and a ping, once a second, for the Monitor page.  The shell
(shell_rs/core/src/moved/hw.rs) answers three of its calls itself: start,
stop and the snapshot.  The other two stay the backend's (the list of
programs and the benchmark start PowerShell, and are started by a button).

"The same answers, proven on the same fake machine before anything is
removed": the Python module and the shell are asked the same things, one
after the other, on the same made-up machine, and every answer is compared,
as for the performance sampler (tests/test_moved_perf.py).

  * The machine is: the processor's times (counting up as time goes by), its
    clock, the memory and the standby list, DPCs a second, the rows WMI
    gives for each of Python's questions, what an echo request to each
    address answers, what the graphics card says; and, for Python's own
    fallbacks, what PowerShell, nvidia-smi and ping.exe print.  Python reads
    three of these through calls of its own to Windows (ctypes): those are
    made to read the same machine.
  * The Python module is the real one, its routes called through Flask, its
    loop run a round at a time.
  * The shell's side is the real code, in the trial tool
    (`vispora-shell-try --moved`).  Its door to the machine answers from
    the same table.  Its card's reader says what the backend's helper says
    of the card.  Where it asks its backend for a part of a pass (because
    Python would start a program there, or read a value in a way of its
    own), the tool puts the call to this test at that moment, and the test
    answers it with the Python app's own route for it, in a state of its
    own (a backend is another process).

Programs: the shell starts none (tests/test_moved_perf.py holds that nothing
in moved/ can).  Here: in every pass in which Python's own loop started one
(PowerShell, nvidia-smi, ping.exe), the shell asked its backend, and the page
got Python's answer all the same.

The monitor sleeps when nobody looks, and a place in WMI that had nothing is
not asked again every second (core/hw_monitor.py says how): the loop is
taken a round at a time on both sides, so that a pass, the rest of its
second and the check of whether anybody still looks are each side's own; a
pass may be taken without a look after it, so that the monitor can be left
alone; and how often each place was asked is counted on both sides.

Nothing of the PC is read: no processor, no card, no clock, no network.
"""
import sandbox as m

import ctypes
import json
import os
import re
import subprocess
import types
import unittest
from collections import Counter

import test_shell_rs as S

try:
    import app as A
    import config
    HW = m.app("core.hw_monitor")
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def tool():
    app()
    return S.TRY


# The questions Python asks of WMI, by a short name.
WMI = {
    "lhm_temps": ("root\\LibreHardwareMonitor", "SELECT Name, SensorType, Value FROM Sensor WHERE SensorType = 'Temperature'"),
    "ohm_temps": ("root\\OpenHardwareMonitor", "SELECT Name, SensorType, Value FROM Sensor WHERE SensorType = 'Temperature'"),
    "acpi": ("root\\WMI", "SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature"),
    "thermal": ("root\\cimv2", "SELECT HighPrecisionTemperature FROM Win32_PerfFormattedData_Counters_ThermalZoneInformation"),
    "lhm_all": ("root\\LibreHardwareMonitor", "SELECT Name, SensorType, Value, Identifier FROM Sensor"),
}
# What Python starts PowerShell for, by a short name, and words of its command.
POWERSHELL = [("dpc", ("% DPC Time",)), ("cpu_load", ("% Processor Time",)), ("cpu_clock", ("CurrentClockSpeed",)),
              ("lhm_temps", ("LibreHardwareMonitor", "Temperature")), ("lhm_gpu", ("LibreHardwareMonitor", "*gpu*")),
              ("ohm_temps", ("OpenHardwareMonitor",)), ("acpi", ("MSAcpi_ThermalZoneTemperature",)), ("thermal", ("ThermalZoneInformation",)),
              ("standby", ("Standby Cache",))]
FAILED = -1073741822                                # (an NTSTATUS that is not success)
THE_SHELLS_CARD = dict(temp_c=13, gpu_util_pct=13, mem_util_pct=13, core_mhz=1313, mem_mhz=1313, power_mw=13_000, fan_pct=13)


def plain_ipv4(text):
    """An address as the helper reads one (Rust's Ipv4Addr): four numbers of
    one to three ASCII digits, none above 255, none with a nought before it."""
    parts = text.split(".")
    return len(parts) == 4 and all(p and len(p) <= 3 and p.isascii() and p.isdigit() and (len(p) == 1 or p[0] != "0") and int(p) <= 255
                                   for p in parts)


class Python:
    """core/hw_monitor.py behind its routes, on the made-up machine."""

    def __init__(self):
        self.pc = self.steady = 0.0
        self.times, self.times_at, self.rates = None, 0.0, None
        self.cpu_clock = self.memory = self.dpc_rate = self.card = None
        self.wmi, self.pings, self.ps, self.ping_exe = {}, {}, {}, {}
        self.smi = None
        self.in_backend = False
        self.current = None                         # the thread the loop takes itself for
        self.started_in_pass = []                   # programs Python's own pass started
        self.started_by_the_backend = []            # ... and its routes, for the shell
        self.kept = self.dropped = 0
        self.parts = []                             # what the shell asked the backend
        self.wmi_asked = set()                      # what Python asked WMI
        self.wmi_counts = Counter()                 # ... in its own passes, and how often
        self.own_starts = Counter()                 # the programs its own passes started, and how often
        self.saved = (HW.time, HW.threading, HW.ctypes, HW.run_cmd, dict(m.NATIVE))
        HW.time = types.SimpleNamespace(time=lambda: self.pc, monotonic=lambda: self.steady, sleep=self.sleep)
        HW.threading = types.SimpleNamespace(Thread=self.thread, current_thread=lambda: self.current)
        windll = types.SimpleNamespace(ntdll=types.SimpleNamespace(NtQuerySystemInformation=self.nt_query),
                                       kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=self.memory_status, GetSystemInfo=self.system_info))
        HW.ctypes = types.SimpleNamespace(**{name: getattr(ctypes, name) for name in dir(ctypes) if not name.startswith("__")})
        HW.ctypes.windll = windll
        real_cmd = HW.run_cmd

        def run_cmd(args, timeout=60):
            # (what is not text cannot be put on a command line: subprocess raises, and run_cmd says it failed)
            if not all(isinstance(a, str) for a in args):
                return {"ok": False, "out": "", "err": "expected str, bytes or os.PathLike object"}
            return real_cmd(args, timeout)
        HW.run_cmd = run_cmd
        m.NATIVE.clear()
        m.NATIVE.update({"cpu_times": lambda: self.times_now(), "cpu_clock": lambda: self.cpu_clock,
                         "wmi": self.helper_wmi, "ping": self.helper_ping,
                         "gpu_live": lambda: dict(self.card) if self.card else None})
        m.PS_HOOKS.insert(0, self.powershell)
        m.CMD_HOOKS.insert(0, self.command)
        self.reset()
        self.backend = {"cpu_prev": {"ts": 0.0, "idle": 0, "kernel": 0, "user": 0}, "times_prev": {"totals": None, "at": 0.0}, "source": ""}
        self.client = A.app.test_client()
        self.headers = {"Host": f"127.0.0.1:{config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}

    @staticmethod
    def reset():
        HW._monitor_state.update(running=False, thread=None, latest={}, alerts=[], ping_target="1.1.1.1", last_wanted=0.0)
        HW._places.clear()
        for kept in HW._metrics.values():
            kept.clear()
        HW._cpu_prev_sample = {"ts": 0.0, "idle": 0, "kernel": 0, "user": 0}
        HW._cpu_times_prev.update(totals=None, at=0.0)
        HW._cpu_temp_source = ""

    def close(self):
        HW.time, HW.threading, HW.ctypes, HW.run_cmd = self.saved[:4]
        m.NATIVE.clear()
        m.NATIVE.update(self.saved[4])
        m.PS_HOOKS.remove(self.powershell)
        m.CMD_HOOKS.remove(self.command)
        self.reset()

    # ─── The made-up machine, as Python reads it ─────────────────────
    def times_now(self):
        if self.times is None:
            return None
        now = dict(self.times)
        for name, rate in (self.rates or {}).items():
            now[name] += round(rate * (self.steady - self.times_at))
        return now

    def go_by(self, seconds):
        self.pc += seconds
        self.steady += seconds

    def sleep(self, seconds):
        if self.in_backend:
            return                                  # (another process: the made-up time here is the page's)
        if seconds == HW._CPU_TIMES_FIRST_WAIT_S:
            self.go_by(seconds)                     # a wait inside a pass
        # (the rest of the second after a pass: the script says what time it is next)

    def helper_wmi(self, namespace, query):
        self.wmi_asked.add((namespace, query))
        if not self.in_backend:
            self.wmi_counts[(namespace, query)] += 1
        return self.wmi.get((namespace, query))

    def helper_ping(self, target, timeout_ms=500):
        assert timeout_ms == 500
        target = target.strip()                     # (the helper reads the address as Rust does)
        return self.pings.get(target) if plain_ipv4(target) else None

    def nt_query(self, kind, info_ref, size, returned_ref):
        info = info_ref._obj
        if kind == 8:                               # the processors' times
            now = self.times_now()
            if now is None:
                return FAILED
            info[0].IdleTime, info[0].KernelTime, info[0].UserTime = now["idle"], now["total"], 0
            return 0
        if kind == 23:                              # DPCs a second
            if self.dpc_rate is None:
                return FAILED
            info[0].DpcRate = self.dpc_rate["per_second"]
            return 0
        if kind == 80:                              # the standby list
            pages = (self.memory or {}).get("standby_pages")
            if pages is None:
                return FAILED
            info.PageCountByPriority[0] = pages
            return 0
        raise AssertionError(f"Python asked Windows for something new: class {kind}")

    def memory_status(self, status_ref):
        load = (self.memory or {}).get("load_pct")
        if load is None:
            return 0
        status_ref._obj.dwMemoryLoad = load
        return 1

    def system_info(self, info_ref):
        info_ref._obj.dwPageSize = (self.memory or {}).get("page_size") or 0

    def started(self, what):
        (self.started_by_the_backend if self.in_backend else self.started_in_pass).append(what)
        if not self.in_backend:
            self.own_starts[what] += 1

    def powershell(self, command):
        for name, words in POWERSHELL:
            if all(word in command for word in words):
                self.started(f"powershell {name}")
                return m.R(self.ps.get(name) or "")
        return None

    def command(self, args):
        exe = os.path.splitext(os.path.basename(args[0]))[0].lower()
        if exe == "nvidia-smi":
            self.started("nvidia-smi")
            return m.R(self.smi) if self.smi else m.F("'nvidia-smi' is not recognized as an internal or external command")
        if exe == "ping":
            self.started(f"ping {args[-1]}")
            return m.R(self.ping_exe.get(args[-1], ""))
        return None

    def thread(self, target=None, daemon=None, **_):
        assert target is HW._monitor_loop

        class NotStarted:
            def start(self):
                pass
        return NotStarted()

    # ─── What the script says ────────────────────────────────────────
    def do(self, step):
        kind = step["do"]
        if kind == "now":
            self.pc = self.steady = step["time"]
        elif kind == "pcs_clock":
            self.pc = step["time"]
        elif kind == "times":
            self.times, self.times_at = step["says"], self.steady
        elif kind == "times_grow":
            self.times, self.times_at, self.rates = self.times_now(), self.steady, step["each_second"]
        elif kind == "clock":
            self.cpu_clock = step["says"]
        elif kind == "memory":
            self.memory = step["says"]
        elif kind == "dpc_rate":
            self.dpc_rate = step["says"]
        elif kind == "wmi":
            self.wmi[WMI[step["of"]]] = step["rows"]
        elif kind == "ping":
            self.pings[step["target"]] = step["says"]
        elif kind == "card":
            self.card = step["says"]
        elif kind == "smi":
            self.smi = step["says"]
        elif kind == "powershell":
            self.ps[step["for"]] = step["says"]
        elif kind == "ping_exe":
            self.ping_exe[step["target"]] = step["says"]
        elif kind == "reader":
            pass                                    # (the shell's own reader: Python has none)
        elif kind == "install":
            if json.loads(step["body"] or "null") and json.loads(step["body"]).get("ok"):
                from core import driver_updater
                m.NATIVE["gpu_release"] = lambda hold_s: {"released": True, "hold_s": hold_s}
                try:
                    driver_updater._release_gpu_library()
                finally:
                    m.NATIVE.pop("gpu_release", None)
        elif kind == "pass":
            return self.a_pass()
        elif kind == "call":
            before = self.monitor_state()
            body = step.get("body", "")
            answer = self.client.open(step["target"], method=step["method"], data=body.encode("utf-8") if isinstance(body, str) else body,
                                      headers=self.headers)
            return {"status": answer.status_code, "body": answer.get_data(as_text=True), "touched": self.monitor_state() != before}
        else:
            raise AssertionError(step)
        return None

    @staticmethod
    def monitor_state():
        st = HW._monitor_state
        return st["running"], id(st["thread"]), repr(st["ping_target"])

    def a_pass(self):
        """One round of the loop of the start that counts (a pass, the rest
        of its second, and whether anybody still looks): True if it took a
        pass.  (After it the loop may sleep: its thread is then None.)"""
        st = HW._monitor_state
        if not st["running"] or st["thread"] is None:
            return False
        me, before = st["thread"], st["latest"]
        self.current = me
        self.started_in_pass = []
        HW._one_round(me)
        if st["latest"] is before:
            self.dropped += 1
        else:
            self.kept += 1
        return True

    def as_the_backend(self, call):
        """The backend's answer to a call of the shell's: the app's own route,
        in a state of the backend's own."""
        self.parts.append(call)
        mine = (HW._cpu_prev_sample, dict(HW._cpu_times_prev), HW._cpu_temp_source)
        HW._cpu_prev_sample, HW._cpu_temp_source = self.backend["cpu_prev"], self.backend["source"]
        HW._cpu_times_prev.update(self.backend["times_prev"])
        self.in_backend, A._behind_shell = True, {"shell_pid": 0}
        try:
            answer = self.client.get(call, headers=self.headers)
        finally:
            self.in_backend, A._behind_shell = False, None
            self.backend = {"cpu_prev": HW._cpu_prev_sample, "times_prev": dict(HW._cpu_times_prev), "source": HW._cpu_temp_source}
            HW._cpu_prev_sample, HW._cpu_temp_source = mine[0], mine[2]
            HW._cpu_times_prev.update(mine[1])
        return answer.get_json() if answer.status_code == 200 else None


class Shell:
    """shell_rs/core/src/moved/, in the trial tool, on its made-up machine.
    Its calls to its backend are put to `python` as they are made."""

    def __init__(self, python, reader=True):
        self.python = python
        self.asked_in_pass = []
        self.tool = m.REAL_POPEN([S.TRY, "--moved"] + ([] if reader else ["--no-reader"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.tell({"do": "backend_is_asked_back", "for": "/api/"})

    def tell(self, step):
        self.tool.stdin.write(json.dumps(step) + "\n")
        self.tool.stdin.flush()
        while True:
            said = json.loads(self.tool.stdout.readline())
            if "asks_the_backend" in said:
                assert said["asks_the_backend"].startswith("/api/monitor/part?"), said
                self.asked_in_pass.append(said["asks_the_backend"])
                self.tool.stdin.write(json.dumps({"answer": self.python.as_the_backend(said["asks_the_backend"])}) + "\n")
                self.tool.stdin.flush()
                continue
            assert "error" not in said, said
            return said

    def do(self, step):
        kind = step["do"]
        if kind in ("smi", "powershell", "ping_exe"):
            return None                             # (programs: the backend's to start, never the shell's)
        if kind == "times":
            step = {"do": "answers", "question": "cpu_times", "answer": step["says"]}
        elif kind == "times_grow":
            step = {"do": "grows", "question": "cpu_times", "each_second": step["each_second"]}
        elif kind in ("clock", "memory", "dpc_rate"):
            step = {"do": "answers", "question": {"clock": "cpu_clock"}.get(kind, kind), "answer": step["says"]}
        elif kind == "wmi":
            namespace, query = WMI[step["of"]]
            step = {"do": "answers", "question": "wmi", "with": {"namespace": namespace, "query": query}, "answer": step["rows"]}
        elif kind == "ping":
            step = {"do": "answers", "question": "ping", "with": {"target": step["target"], "timeout_ms": 500}, "answer": step["says"]}
        elif kind == "card":
            # What the backend's helper says of the card, the card's reader
            # says too.  The library in the shell's own process says
            # something else, so that a shell that asked it would be found out.
            self.tell({"do": "reader", "says": "none" if step["says"] is None else step["says"]})
            step = {"do": "answers", "question": "gpu_live", "answer": dict(step["says"] or {}, **THE_SHELLS_CARD)}
        elif kind == "pass":
            self.asked_in_pass = []
            return self.tell({"do": "pass"})["took_one"]
        elif kind == "call":
            body = step.get("body", "")
            step = {key: value for key, value in step.items() if key not in ("body", "refused")}
            step["body" if isinstance(body, str) else "body_hex"] = body if isinstance(body, str) else body.hex()
        said = self.tell(step)
        return said if kind in ("call", "asked", "install") else None

    def close(self):
        self.tool.stdin.close()
        self.tool.wait(20)


def same(shells, pythons, where=""):
    """The same JSON value, a whole number where Python has one and a number
    with a fraction where Python has that."""
    if isinstance(shells, dict) and isinstance(pythons, dict):
        assert set(shells) == set(pythons), f"{where}: {sorted(set(shells) ^ set(pythons))}"
        for key in shells:
            same(shells[key], pythons[key], f"{where}.{key}")
        return
    if isinstance(shells, list) and isinstance(pythons, list):
        assert len(shells) == len(pythons), f"{where}: {shells} / {pythons}"
        for at, (a, b) in enumerate(zip(shells, pythons)):
            same(a, b, f"{where}[{at}]")
        return
    assert type(shells) is type(pythons) and shells == pythons, f"{where}: the shell {shells!r}, Python {pythons!r}"


SNAPSHOT = {"do": "call", "method": "GET", "target": "/api/monitor/snapshot"}
# What Python starts PowerShell for in a place of WMI (the four of the
# processor's temperature, LibreHardwareMonitor's sensors of the card).
PLACES_IN_POWERSHELL = ("powershell lhm_temps", "powershell ohm_temps", "powershell acpi", "powershell thermal", "powershell lhm_gpu")


def both(script, reader=True):
    """Ask the Python module and the shell the same things in the same
    order, and compare every answer, and the snapshot after every pass (but
    after a pass with "look": False, which leaves the monitor alone).
    Returns what was counted: "looks" is, for every snapshot, whether it
    said running, whether it had a latest reading, and how many seconds of
    the processor's load it held; "latests" is its latest readings, with
    the time of the monitor's own clock at which it was asked."""
    python = Python()
    shell = Shell(python, reader)
    counted = {"answered": 0, "passed_on": 0, "refused": 0, "passes": 0, "no_pass": 0, "python_started": 0, "shell_asked": 0, "looks": [],
               "latests": []}
    try:
        for at, step in enumerate(script):
            what = f"step {at}: {step}"[:300]
            if step.get("refused"):
                # Not passed on, so the backend never hears of it: the shell
                # says no, and starts nothing.
                said = shell.do(step)
                assert said.get("status") == 400 and not said.get("passed_on"), f"{what}: {said}"
                counted["refused"] += 1
                continue
            pythons, shells = python.do(step), shell.do(step)
            if step["do"] == "pass":
                assert pythons == shells, f"{what}: a pass in Python {pythons}, in the shell {shells}"
                if pythons:
                    counted["passes"] += 1
                    if python.started_in_pass:
                        counted["python_started"] += 1
                        assert shell.asked_in_pass, f"{what}: Python started {python.started_in_pass}, and the shell asked its backend nothing"
                    counted["shell_asked"] += bool(shell.asked_in_pass)
                    steps = [SNAPSHOT] if step.get("look", True) else []
                else:
                    counted["no_pass"] += 1
                    steps = []
            elif step["do"] == "call":
                steps = [step]
            else:
                continue
            for call in steps:
                pythons = python.do(call) if call is not step else pythons
                shells = shell.do(call) if call is not step else shells
                ask = f"{what}: {call['method']} {call['target']} {call.get('body', '')!r}"[:400]
                if shells.get("passed_on"):
                    counted["passed_on"] += 1
                    assert not pythons["touched"], f"{ask}: the shell passed it on, and the backend's monitor did something with it"
                    continue
                counted["answered"] += 1
                assert shells["status"] == pythons["status"] == 200, f"{ask}: {shells} / {pythons}"
                same(json.loads(shells["body"]), json.loads(pythons["body"]), ask)
                if call["target"] == SNAPSHOT["target"]:
                    said = json.loads(pythons["body"])
                    counted["looks"].append((said["running"], bool(said["latest"]), len(said["history"]["cpu_load"])))
                    counted["latests"].append((python.steady, said["latest"]))
        asked = shell.do({"do": "asked"})
        assert all(not question.startswith("gpu_") for question in asked["library"]), "the shell asked the library in its own process about the card"
        allowed = re.compile(r"^(cpu_times|cpu_clock|memory|dpc_rate|wmi \{.*\}|ping \{\"target\":\"[0-9.]+\",\"timeout_ms\":500\})$")
        assert all(allowed.match(question) for question in asked["library"]), sorted({q for q in asked["library"] if not allowed.match(q)})
        assert all(call.startswith("/api/monitor/part?name=") for call in asked["backend"]), sorted(set(asked["backend"]))
        assert (asked["passes_kept"], asked["passes_dropped"]) == (python.kept, python.dropped), (asked, python.kept, python.dropped)
        if not reader:
            assert (asked["from_the_reader"], asked["readers_started"]) == (0, 0)
        assert python.wmi_asked <= set(WMI.values()), sorted(python.wmi_asked - set(WMI.values()))
        # Every place was asked as often by the shell as by Python's own
        # passes; and where Python's own pass started PowerShell for a
        # place, the shell's backend did, as often.
        shells_wmi = Counter(tuple(json.loads(question[len("wmi "):]).values()) for question in asked["library"] if question.startswith("wmi "))
        assert shells_wmi == python.wmi_counts, (shells_wmi, python.wmi_counts)
        backends_starts = Counter(python.started_by_the_backend)
        for program in PLACES_IN_POWERSHELL:
            assert backends_starts[program] == python.own_starts[program], (program, backends_starts[program], python.own_starts[program])
        counted.update(asked, parts=list(python.parts), started_by_the_backend=list(python.started_by_the_backend),
                       wmi_counts={name: python.wmi_counts[WMI[name]] for name in WMI}, own_starts=dict(python.own_starts))
        return counted
    finally:
        python.close()
        shell.close()


# ═════════════════════════════════════════════════════════════════════
# The script's words
# ═════════════════════════════════════════════════════════════════════
def now(time):
    return {"do": "now", "time": time}


def call(method, target, body=""):
    return {"do": "call", "method": method, "target": target, "body": body if isinstance(body, (str, bytes)) else json.dumps(body)}


def start(body="{}"):
    return call("POST", "/api/monitor/start", body)


def wmi(of, rows):
    return {"do": "wmi", "of": of, "rows": rows}


def ps(name, says):
    return {"do": "powershell", "for": name, "says": says}


PASS, STOP = {"do": "pass"}, call("POST", "/api/monitor/stop")
TIMES = {"total": 8_000_000_000, "idle": 6_000_000_000, "dpc": 4_000_000, "interrupt": 2_000_000, "processors": 8}
A_QUARTER_BUSY = {"total": 80_000_000, "idle": 60_000_000, "dpc": 400_000, "interrupt": 80_000}
THREE_QUARTERS_BUSY = {"total": 80_000_000, "idle": 20_000_000, "dpc": 2_400_000, "interrupt": 800_000}
A_CARD = dict(temp_c=61, gpu_util_pct=97, mem_util_pct=40, core_mhz=2100, mem_mhz=9501, core_max_mhz=2200, mem_max_mhz=9800, core_app_mhz=2100,
              mem_app_mhz=9501, power_mw=215437, power_limit_mw=320000, fan_pct=55, mem_used_bytes=5_000_000_000, mem_total_bytes=12_000_000_000)
LHM_TEMPS = [{"Name": "Core #1", "SensorType": "Temperature", "Value": 48.0}, {"Name": "CPU Package", "SensorType": "Temperature", "Value": 52.44}]
AMD_SENSORS = [{"Name": "GPU Core", "SensorType": "Temperature", "Value": 66.5, "Identifier": "/gpu-amd/0/temperature/0"},
               {"Name": "GPU Hot Spot", "SensorType": "Temperature", "Value": 80.25, "Identifier": "/gpu-amd/0/temperature/1"},
               {"Name": "D3D 3D", "SensorType": "Load", "Value": 42.25, "Identifier": "/gpu-amd/0/load/1"},
               {"Name": "GPU Core", "SensorType": "Clock", "Value": 2450.7, "Identifier": "/gpu-amd/0/clock/0"},
               {"Name": "CPU Total", "SensorType": "Load", "Value": 5.0, "Identifier": "/amdcpu/0/load/0"}]


def a_whole_machine():
    """Everything answers: eight processors a quarter busy, LibreHardwareMonitor,
    an NVIDIA card, a ping that comes back."""
    return [now(1000.0), {"do": "times", "says": TIMES}, {"do": "times_grow", "each_second": A_QUARTER_BUSY},
            {"do": "clock", "says": {"mhz": 3700, "max_mhz": 3700, "processors": 8}},
            {"do": "memory", "says": {"load_pct": 43, "standby_pages": 971_766, "page_size": 4096}},
            {"do": "dpc_rate", "says": {"per_second": 245, "processors": 8}},
            wmi("lhm_temps", LHM_TEMPS), wmi("ohm_temps", []), wmi("acpi", []), wmi("thermal", []), wmi("lhm_all", []),
            {"do": "ping", "target": "1.1.1.1", "says": {"ok": True, "ms": 11}}, {"do": "card", "says": A_CARD}]


# ═════════════════════════════════════════════════════════════════════
# The same answers
# ═════════════════════════════════════════════════════════════════════
def test_the_monitor_page_gets_the_same_answers_from_the_shell():
    """What the Monitor page does: the snapshot, a start, the snapshot every
    three seconds, and a stop; seventy passes of a machine whose numbers
    move, to beyond the minute that is kept and over every line at which
    the page is warned."""
    if not tool():
        return
    script = a_whole_machine() + [SNAPSHOT, start(), SNAPSHOT]
    for round in range(70):
        script += [now(1000.0 + round), {"do": "card", "says": dict(A_CARD, temp_c=60 + round % 30, gpu_util_pct=round % 101, core_mhz=1500 + round * 7,
                                                                      mem_used_bytes=11_000_000_000 + round * 17_000_000)},
                   {"do": "memory", "says": {"load_pct": 80 + round % 15, "standby_pages": 900_000 + round * 1013, "page_size": 4096}},
                   {"do": "dpc_rate", "says": {"per_second": 200 + round * 3, "processors": 8}},
                   wmi("lhm_temps", [{"Name": "CPU Package", "SensorType": "Temperature", "Value": 70.0 + (round % 25) * 0.95}]), PASS]
        if round % 10 == 3:
            script.append({"do": "times_grow", "each_second": dict(A_QUARTER_BUSY, idle=60_000_000 - round * 400_000, dpc=400_000 + round * 4000)})
        if round % 3 == 0:
            script.append(SNAPSHOT)
    # The PC's clock is set back and forward: a reading is stamped with it,
    # and how long ago the pass before was is not counted on it.
    script += [now(1070.0), {"do": "pcs_clock", "time": 10.0}, PASS, SNAPSHOT, now(1071.0), {"do": "pcs_clock", "time": 2_000_000_000.0}, PASS, SNAPSHOT,
               now(1072.0), PASS]
    script += [STOP, SNAPSHOT, PASS, now(1080.0), SNAPSHOT]
    counted = both(script)
    assert (counted["passes"], counted["passed_on"], counted["refused"], counted["shell_asked"], counted["waited"]) == (73, 0, 0, 0, 0.5), counted
    assert counted["backend"] == [] and counted["from_the_reader"] == 73 and counted["readers_started"] == 1, counted
    # A shell without a reader of its own (its pack carries none): the same
    # answers, the card from the backend's part for it.
    counted = both(script, reader=False)
    assert counted["passes"] == 73 and counted["parts"] == ["/api/monitor/part?name=gpu"] * 73, counted


def test_where_python_falls_back_the_page_gets_pythons_answer():
    """Each of the places where Python leaves its first way of reading: no
    LibreHardwareMonitor, no OpenHardwareMonitor, no ACPI zone, a counter
    that gives nothing, a card that answers only to nvidia-smi, no card; and
    where the helper itself has no answer, so that Python starts PowerShell,
    nvidia-smi or ping.exe."""
    if not tool():
        return
    s = a_whole_machine() + [start(), PASS]
    t = [1001.0]

    def a_pass(*changes, later=1):
        t[0] += later
        return [now(t[0]), *changes, PASS]
    # The processor's temperature, source by source.
    s += a_pass(wmi("lhm_temps", []), wmi("ohm_temps", [{"Name": "Tctl", "SensorType": "Temperature", "Value": 99},
                                                        {"Name": "CCD1 (Tdie)", "SensorType": "Temperature", "Value": 47.25}]))
    s += a_pass(wmi("lhm_temps", [{"Name": "CPU Package", "SensorType": "Temperature", "Value": 27.9}]))
    s += a_pass(wmi("lhm_temps", []), wmi("ohm_temps", []), wmi("acpi", [{"CurrentTemperature": 3200}, {"CurrentTemperature": 3500}]))
    s += a_pass(wmi("acpi", [{"CurrentTemperature": 3010}]), wmi("thermal", [{"HighPrecisionTemperature": 3100}, {"HighPrecisionTemperature": 3256.5},
                                                                             {"HighPrecisionTemperature": "9999"}, {}]))
    s += a_pass(wmi("thermal", [{"HighPrecisionTemperature": 3011}]))
    s += a_pass(wmi("acpi", []), wmi("thermal", []))
    # The helper's WMI gives no answer at all: Python asks PowerShell.
    for of, says in (("lhm_temps", '{"Value": 55.55}'), ("ohm_temps", '{"Value": 56.5}'), ("acpi", '{"CurrentTemperature": 3300}'),
                     ("thermal", '{"HighPrecisionTemperature": 3350}')):
        s += [ps(of, says)]
        s += a_pass(wmi(of, None))
        s += a_pass(ps(of, ""))
    s += a_pass(*(wmi(of, []) for of in ("lhm_temps", "ohm_temps", "acpi", "thermal")), wmi("lhm_temps", LHM_TEMPS))
    # Values Python reads in a way of its own: a sensor's value as text, a name that is a list.
    s += a_pass(wmi("lhm_temps", [{"Name": "CPU Package", "SensorType": "Temperature", "Value": "61.25"}]))
    s += a_pass(wmi("lhm_temps", [{"Name": "CPU Package", "SensorType": "Temperature", "Value": " 6_3.75\n"}]))
    s += a_pass(wmi("lhm_temps", [{"Name": ["CPU Package"], "SensorType": "Temperature", "Value": 62.0}]))
    s += a_pass(wmi("lhm_temps", LHM_TEMPS))
    # The processor's times give nothing: the load and the shares are
    # PowerShell's, and with PowerShell giving nothing, nothing.
    s += a_pass({"do": "times", "says": None}, ps("cpu_load", "37.5"), ps("dpc", '{"dpc_pct": 0.125, "isr_pct": 0.0625, "ok": true}'))
    s += a_pass(ps("cpu_load", "NaN"), ps("dpc", '{"ok": false}'))
    s += a_pass(ps("cpu_load", ""), ps("dpc", ""))
    s += a_pass({"do": "times", "says": TIMES})
    s += a_pass()
    # The clock, the standby list, DPCs a second.
    s += a_pass({"do": "clock", "says": None}, ps("cpu_clock", "3600"))
    s += a_pass(ps("cpu_clock", "fast"))
    s += a_pass({"do": "clock", "says": {"mhz": 0, "max_mhz": 3700, "processors": 8}})
    s += a_pass({"do": "clock", "says": {"mhz": 3701, "max_mhz": 3700, "processors": 8}})
    s += a_pass({"do": "memory", "says": {"load_pct": 51, "standby_pages": None, "page_size": 4096}}, ps("standby", "2048"))
    s += a_pass(ps("standby", "0"))
    s += a_pass({"do": "memory", "says": None})
    s += a_pass({"do": "memory", "says": {"load_pct": None, "standby_pages": 5, "page_size": 65536}}, {"do": "dpc_rate", "says": None})
    s += a_pass({"do": "memory", "says": {"load_pct": 44, "standby_pages": 600_000, "page_size": 0}})
    s += a_pass({"do": "memory", "says": {"load_pct": 91, "standby_pages": 262_144, "page_size": 4096}}, {"do": "dpc_rate", "says": {"per_second": 0, "processors": 8}})
    # The graphics card.
    s += a_pass({"do": "card", "says": dict(A_CARD, gpu_util_pct=None)}, wmi("lhm_all", [{"Name": "GPU Core", "SensorType": "Load", "Value": 33.26,
                                                                                         "Identifier": "/gpu-nvidia/0/load/0"}]))
    # (the card says its temperature and load: LibreHardwareMonitor is not asked, though it has a clock)
    s += a_pass({"do": "card", "says": dict(A_CARD, mem_used_bytes=None, core_mhz=None)},
                wmi("lhm_all", [{"Name": "GPU Core", "SensorType": "Clock", "Value": 1999.5, "Identifier": "/gpu-nvidia/0/clock/0"}]))
    s += a_pass({"do": "card", "says": None}, {"do": "smi", "says": "61, 97, 2100, 5000, 12000"})    # answers only to nvidia-smi
    s += a_pass({"do": "smi", "says": "55.10, 12, 1500, 300, 0"})
    s += a_pass({"do": "smi", "says": None}, wmi("lhm_all", AMD_SENSORS))                           # no NVIDIA card: LibreHardwareMonitor
    s += a_pass(wmi("lhm_all", [{"Name": 5, "SensorType": "Load", "Value": 1, "Identifier": "gpu"}]))   # (Python's loop raises here)
    s += a_pass(wmi("lhm_all", [{"Name": "GPU", "SensorType": "Temperature", "Value": "hot", "Identifier": "gpu"}]))
    s += a_pass(wmi("lhm_all", None), ps("lhm_gpu", '[{"Name": "GPU Core", "SensorType": "Temperature", "Value": 71.5, "Identifier": "/gpu/0"}]'))
    s += a_pass(wmi("lhm_all", []), ps("lhm_gpu", ""))                                              # no card at all
    s += a_pass({"do": "card", "says": A_CARD})
    s += a_pass({"do": "card", "says": dict(A_CARD, temp_c=None)}, {"do": "smi", "says": "70, 5, 300, 1, 2"})
    # (a while later: a reader that gave nothing is left alone for a while)
    s += a_pass({"do": "card", "says": A_CARD}, later=120)
    s += a_pass({"do": "reader", "says": "silent"})                                                 # the reader gives nothing: the backend's
    s += a_pass({"do": "reader", "says": "garbled"}, later=60)
    s += a_pass({"do": "reader", "says": A_CARD}, later=120)
    # A driver's installer was started: no reader for 900 seconds.
    s += [{"do": "install", "status": 200, "body": '{"ok": true}'}]
    s += a_pass()
    s += a_pass(later=899)
    s += a_pass(later=2)
    # The ping.
    s += a_pass({"do": "ping", "target": "1.1.1.1", "says": {"ok": False}})
    s += a_pass({"do": "ping", "target": "1.1.1.1", "says": {"ok": True, "ms": 0}})
    s += a_pass({"do": "ping", "target": "1.1.1.1", "says": None}, {"do": "ping_exe", "target": "1.1.1.1", "says": "Reply from 1.1.1.1: bytes=32 time<1ms TTL=57"})
    s += a_pass({"do": "ping_exe", "target": "1.1.1.1", "says": "Antwort von 1.1.1.1: Bytes=32 Zeit=12ms TTL=57"})
    s += a_pass({"do": "ping_exe", "target": "1.1.1.1", "says": "Request timed out."})
    s += a_pass({"do": "ping", "target": "1.1.1.1", "says": {"ok": True, "ms": 9}})
    s += [SNAPSHOT, STOP, SNAPSHOT]
    counted = both(s)
    assert counted["passes"] == sum(1 for step in s if step is PASS) and counted["passes_dropped"] == 1, counted
    assert counted["python_started"] >= 20 and counted["shell_asked"] >= counted["python_started"], counted
    assert {part.split("&")[0] for part in counted["parts"]} == {f"/api/monitor/part?name={name}" for name in A._MONITOR_PARTS}, counted["parts"]
    for program in ("powershell cpu_load", "powershell dpc", "powershell cpu_clock", "powershell lhm_temps", "powershell standby", "powershell lhm_gpu",
                    "nvidia-smi", "ping 1.1.1.1"):
        assert program in counted["started_by_the_backend"], (program, sorted(set(counted["started_by_the_backend"])))
    # And without a reader: the card is the backend's part in every pass.
    counted = both(s, reader=False)
    assert counted["from_the_reader"] == 0 and counted["parts"].count("/api/monitor/part?name=gpu") >= counted["passes"] - 1, counted


QUIET = {"do": "pass", "look": False}             # a pass, and nobody looks


def at(time):
    return now(time)


def test_it_sleeps_when_nobody_looks_and_wakes_when_somebody_does_as_python_does():
    """Looked at every three seconds, then not at all: after half a minute
    the loop sleeps, and takes no pass; the next look wakes it, without the
    old numbers.  At the very edge: a pass exactly 30 seconds after a look
    is still taken, the one after it sleeps.  A Stop while it sleeps: it
    stays stopped, and a look does not wake it.  A start while it sleeps
    wakes it and answers as a start while it runs.  The PC's clock set
    forward and back changes none of it."""
    if not tool():
        return
    s = a_whole_machine() + [start()]
    for second in range(12):                        # looked at every three seconds
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    for second in range(12, 41):                    # ... the last look at 1009: asleep after the pass at 1040
        s += [at(1000.0 + second), QUIET]
    # While it sleeps the PC is busier, and then not: a first pass after the
    # wake that compared with the last one before the sleep would show it.
    s += [{"do": "times_grow", "each_second": THREE_QUARTERS_BUSY}]
    for second in range(41, 45):
        s += [at(1000.0 + second), QUIET]
    s += [{"do": "times_grow", "each_second": A_QUARTER_BUSY}, SNAPSHOT]   # (index 4 of the looks: woken, nothing old)
    s += [at(1046.0), QUIET, SNAPSHOT, at(1047.0), QUIET, SNAPSHOT]
    for second in range(48, 78):                    # the pass at 1077, exactly 30 s after the look at 1047: awake
        s += [at(1000.0 + second), QUIET]
    s += [SNAPSHOT]
    for second in range(78, 109):                   # ... and the one at 1108, 31 s after the look at 1077: asleep
        s += [at(1000.0 + second), QUIET]
    s += [at(1108.5), QUIET, SNAPSHOT]
    for second in range(109, 142):
        s += [at(1000.0 + second), QUIET]
    s += [STOP, SNAPSHOT, at(1143.0), QUIET, SNAPSHOT, start(), at(1144.0), QUIET, SNAPSHOT]
    for second in range(145, 180):
        s += [at(1000.0 + second), QUIET]
    s += [start({"ping_target": "9.9.9.9"}), SNAPSHOT, at(1181.0), QUIET, SNAPSHOT]
    s += [{"do": "pcs_clock", "time": 9_000_000.0}, at(1182.0), {"do": "pcs_clock", "time": 9_000_000.0}, QUIET, SNAPSHOT]
    for second in range(183, 220):                  # the PC's clock goes on far ahead: the monitor's does not
        s += [at(1000.0 + second), {"do": "pcs_clock", "time": 9_000_000.0 + second}, QUIET]
    s += [{"do": "pcs_clock", "time": 50.0}, SNAPSHOT, QUIET, SNAPSHOT]
    counted = both(s)
    looks = counted["looks"]
    # (running, a latest reading, seconds of history) at each look
    assert looks[:4] == [(True, True, 1), (True, True, 4), (True, True, 7), (True, True, 10)], looks[:4]
    assert looks[4] == (True, False, 0), "the look that wakes it shows nothing of before the sleep"
    assert looks[5:7] == [(True, True, 1), (True, True, 2)], looks[5:7]
    assert looks[7] == (True, True, 32), "a pass exactly 30 s after a look is taken"
    assert looks[8] == (True, False, 0), looks[8]
    assert looks[9][:2] == (False, True) and looks[10] == looks[9], "stopped while it sleeps: it stays so, and keeps what it read"
    assert looks[11][0] and looks[11][2] == looks[9][2] + 1, "an ordinary start after it"
    assert looks[12] == (True, False, 0) and looks[13] == (True, True, 1), "a start while it sleeps wakes it"
    assert looks[14] == (True, True, 2), looks[14]
    assert looks[15] == (True, False, 0) and looks[16] == (True, True, 1), "the PC's clock ahead: it slept by the monitor's own clock"
    # No pass while it slept: 1041 to 1044, 1108.5, 1140 and 1141, 1143
    # (stopped), 1176 to 1179, 1214 to 1219.
    assert counted["no_pass"] == 4 + 1 + 2 + 1 + 4 + 6, counted["no_pass"]


def test_a_pass_right_after_one_that_ran_past_its_second_starts_nothing_on_either_side():
    """The first pass after a start or a wake waits half a second within
    itself and may run past its second; the next then comes at once, and
    reads the processor's times a moment after the last reading, the totals
    not moved.  Both sides read the shares again a moment later: no
    PowerShell in Python, no part asked by the shell.  The same where the
    last reading is younger than a quarter of a second and the totals
    moved; a quarter of a second old, it is compared with."""
    if not tool():
        return
    s = a_whole_machine() + [start(), at(1000.0), PASS, PASS]          # (the second at the moment the first ended)
    s += [at(1000.875), PASS, at(1001.375), PASS]                       # (0.125 s after the last reading; then 0.25 s)
    for second in range(2, 36):                                         # nobody looks: it sleeps
        s += [at(1000.0 + second), QUIET]
    s += [SNAPSHOT, at(1040.0), PASS, PASS, at(1041.0), PASS]           # woken, and the same again
    counted = both(s)
    assert counted["no_pass"] > 0 and counted["passes"] >= 9, counted
    assert counted["python_started"] == 0 and counted["shell_asked"] == 0, (counted["own_starts"], counted["parts"])
    assert not any("name=dpc" in part for part in counted["parts"]) and "powershell dpc" not in counted["own_starts"]


def a_minute_of_nothing(until=1060):
    """A machine with no source of the processor's temperature, and a card
    that says no load (so that LibreHardwareMonitor's sensors of the card
    are asked too), looked at as the page does, from 1000 to `until`."""
    s = a_whole_machine() + [wmi("lhm_temps", []), {"do": "card", "says": dict(A_CARD, gpu_util_pct=None)}, start()]
    for second in range(until - 999):
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    return s


def temperatures(counted, of="cpu_temp"):
    """What each look said of `of`, by the second it was asked."""
    return {round(time): latest.get(of) for time, latest in counted["latests"]}


def test_a_place_that_had_nothing_is_asked_twice_a_minute_on_both_sides():
    """No source of the processor's temperature at all: each of the five
    places is asked twice in a minute, not sixty times.  (The first pass,
    at 1000, waits within itself, so each is quiet until just after 1030,
    asked at 1031, and quiet until 1061.)  Then LibreHardwareMonitor is
    started, at 1045: seen at 1061, within half a minute.  It misses once:
    asked again at the next pass, which has its number.  It is gone: asked
    every pass for half a minute after its last reading, then quiet."""
    if not tool():
        return
    counted = both(a_minute_of_nothing(until=1059))
    assert counted["passes"] == 60 and counted["passes_dropped"] == 0, counted
    assert counted["wmi_counts"] == {"lhm_temps": 2, "ohm_temps": 2, "acpi": 2, "thermal": 2, "lhm_all": 2}, counted["wmi_counts"]
    s = a_minute_of_nothing(until=1044)
    s += [wmi("lhm_temps", LHM_TEMPS), wmi("lhm_all", [{"Name": "GPU Core", "SensorType": "Load", "Value": 12.5, "Identifier": "/gpu-nvidia/0/load/0"}])]
    for second in range(45, 100):                   # LibreHardwareMonitor is started at 1045
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    s += [wmi("lhm_temps", []), at(1100.0), PASS, wmi("lhm_temps", LHM_TEMPS), at(1101.0), PASS]
    s += [wmi("lhm_temps", [])]
    for second in range(102, 140):                  # gone at 1102: its last reading at 1101
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    counted = both(s)
    assert counted["passes"] == 140 and counted["passes_dropped"] == 0, counted
    seen, load = temperatures(counted), temperatures(counted, "gpu_load")
    assert (seen[1045], seen[1060], seen[1063]) == (None, None, 52.4), seen
    assert (load[1060], load[1063]) == (None, 12.5), load
    assert (seen[1099], seen[1100], seen[1101], seen[1102]) == (52.4, None, 52.4, None), seen
    # LibreHardwareMonitor: 1000, 1031; every pass from 1061 to 1101 (41),
    # the miss at 1100 among them; gone, every pass to 1132 (31), then quiet.
    # The other three: 1000, 1031; at 1100 (the miss), quiet until 1130, and
    # at 1130.  The card's sensors: 1000, 1031, every pass from 1061 (79).
    assert counted["wmi_counts"] == {"lhm_temps": 2 + 41 + 31, "ohm_temps": 4, "acpi": 4, "thermal": 4, "lhm_all": 2 + 79}, counted["wmi_counts"]


def test_where_the_helper_cannot_ask_wmi_powershell_is_quiet_by_the_same_rule():
    """The helper gives no answer for any place: Python then starts
    PowerShell for it, and the shell asks its backend for that place, which
    starts PowerShell as often.  A place that had nothing is asked twice a
    minute this way too, and one that has a reading every pass:
    OpenHardwareMonitor answers PowerShell from 1045, and is seen at 1061."""
    if not tool():
        return
    s = a_whole_machine() + [wmi(of, None) for of in ("lhm_temps", "ohm_temps", "acpi", "thermal", "lhm_all")]
    s += [{"do": "card", "says": dict(A_CARD, gpu_util_pct=None)}, start()]
    for second in range(45):
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    s += [ps("ohm_temps", '{"Value": 56.5}')]
    for second in range(45, 100):
        s += [at(1000.0 + second), QUIET] + ([SNAPSHOT] if second % 3 == 0 else [])
    counted = both(s)
    seen = temperatures(counted)
    assert (seen[1045], seen[1060], seen[1063], seen[1099]) == (None, None, 56.5, 56.5), seen
    started = {program: counted["own_starts"].get(program, 0) for program in PLACES_IN_POWERSHELL}
    assert started == {"powershell lhm_temps": 4, "powershell ohm_temps": 2 + 39, "powershell acpi": 2, "powershell thermal": 2,
                       "powershell lhm_gpu": 4}, started
    assert counted["wmi_counts"] == started_as_wmi(started), counted["wmi_counts"]


def started_as_wmi(started):
    """(the helper was asked first each time, and had no answer)"""
    return {"lhm_temps": started["powershell lhm_temps"], "ohm_temps": started["powershell ohm_temps"], "acpi": started["powershell acpi"],
            "thermal": started["powershell thermal"], "lhm_all": started["powershell lhm_gpu"]}


def test_the_ping_target_is_taken_as_python_takes_it():
    """The target comes from the page's start call, and the page names
    none.  What the shell does with each kind of text, and with what is no
    text, against what Python does."""
    if not tool():
        return
    s = a_whole_machine() + [{"do": "ping", "target": "9.9.9.9", "says": {"ok": True, "ms": 4}}]
    no_key = object()
    for target, ping_exe in ((no_key, None), (None, None), ("9.9.9.9", None), (" 9.9.9.9", None), ("9.9.9.9 ", None), ("001.1.1.1", "Reply from 1.1.1.1: time=7ms TTL=57"),
                             ("1.1.1.256", ""), ("example.com", "Reply from 93.184.215.14: bytes=32 time=23ms TTL=57"), ("", ""),
                             ("a b&c=d", "Reply: temps=31 ms TTL=50"), ("höst", "Reply: time=5ms TTL=50"), ("1.1.1.1\u0000", ""),
                             (5, None), (True, None), (1.5, None), (["1.1.1.1"], None), ({"a": 1}, None), ("null", "")):
        body = {} if target is no_key else {"ping_target": target}
        if ping_exe is not None:
            s.append({"do": "ping_exe", "target": target, "says": ping_exe})
        s += [STOP, start(body), now(s[0]["time"] + len(s)), PASS, SNAPSHOT]
    # The last of two names counts; a start while it runs changes nothing.
    s += [STOP, start('{"ping_target": "8.8.8.8", "ping_target": "9.9.9.9"}'), now(5000.0), PASS, start({"ping_target": "example.com"}), now(5001.0), PASS]
    s += [STOP, start(b'\xef\xbb\xbf{"ping_target": "9.9.9.9"}'), now(5002.0), PASS]
    counted = both(s)
    assert counted["refused"] == 0 and counted["passed_on"] == 0, counted
    sent = sorted({part for part in counted["parts"] if "name=ping" in part})
    assert sent == ["/api/monitor/part?name=ping&target=",
                    "/api/monitor/part?name=ping&target=%209.9.9.9", "/api/monitor/part?name=ping&target=001.1.1.1",
                    "/api/monitor/part?name=ping&target=1.1.1.1%00", "/api/monitor/part?name=ping&target=1.1.1.256",
                    "/api/monitor/part?name=ping&target=9.9.9.9%20", "/api/monitor/part?name=ping&target=a%20b%26c%3Dd",
                    "/api/monitor/part?name=ping&target=example.com", "/api/monitor/part?name=ping&target=h%C3%B6st",
                    "/api/monitor/part?name=ping&target=null"], sent
    asked = sorted({question for question in counted["library"] if question.startswith("ping")})
    assert asked == ['ping {"target":"1.1.1.1","timeout_ms":500}', 'ping {"target":"9.9.9.9","timeout_ms":500}'], asked


def test_what_the_shell_passes_on_is_what_python_stops_at_and_what_it_refuses_python_would_not():
    """A start whose body Python stops at (it starts nothing, and says so in
    words of its own) is passed on.  One that Python might read and this
    program cannot is refused, not passed on: Python would start its own
    monitor beside the shell's.  Every other call of the monitor's is the
    backend's: another method, the list of programs, the benchmark, the
    backend's own call for a part (which is not there for a page)."""
    if not tool():
        return
    s = a_whole_machine()
    for body in ("", "not json", "{", "[1]", '"text"', "7", "true", "1.5", "{'ping_target': '9.9.9.9'}", '{"ping_target": "a",}', b"\xff{}",
                 b'{"ping_target": "\xc3"}', '{"ping_target": "\u0001"}'):
        s += [start(body), SNAPSHOT]
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        s += [call(method, "/api/monitor/start", "{}"), call(method, "/api/monitor/stop")]
    s += [call(method, "/api/monitor/snapshot") for method in ("POST", "PUT", "DELETE")]
    s += [call("GET", "/api/monitor/processes"), call("POST", "/api/monitor/benchmark"), call("GET", "/api/monitor/part?name=cpu"),
          call("POST", "/api/monitor/start/", "{}"), call("POST", "/api/monitor/Start", "{}"), call("GET", "/api/monitor/"), call("GET", "/api/monitor")]
    deep = '{"ping_target": ' + "[" * 80 + '"9.9.9.9"' + "]" * 80 + "}"
    for body in ('{"ping_target": "\\ud800"}', '{"ping_target": "a\\udc00b"}', deep, b"\xff\xfe{\x00}\x00", b"{\x00}\x00", b"\x00{\x00}",
                 b'{"ping_target": "\xed\xa0\x80"}'):
        s += [dict(start(body), refused=True), SNAPSHOT]
    # Written another way, the calls are the shell's all the same.
    s += [call("POST", "/api/monitor/%73tart", "{}"), call("GET", "/api/%6Donitor/snapshot?x=1"), now(1001.0), PASS, call("POST", "/api%2Fmonitor%2Fstop"), SNAPSHOT]
    counted = both(s)
    assert counted["refused"] == 7 and counted["passed_on"] == 13 + 8 + 3 + 7, counted


# ═════════════════════════════════════════════════════════════════════
# One of each, and what the backend gives the shell
# ═════════════════════════════════════════════════════════════════════
def test_the_backends_part_is_there_only_behind_the_shell_and_never_starts_its_monitor():
    app()
    python = Python()
    try:
        for step in a_whole_machine():
            python.do(step)
        python.do({"do": "card", "says": None})
        # Started as it always was, the app has no such call.
        names = ("cpu", "temp_lhm", "temp_ohm", "temp_acpi", "temp_thermal", "gpu", "gpu_lhm", "ram", "dpc", "ping")
        assert set(A._MONITOR_PARTS) == set(names)
        for name in names:
            assert python.client.get(f"/api/monitor/part?name={name}", headers=python.headers).status_code == 404
        # Behind the shell: each part is the loop's own function, and the
        # backend's monitor is not started by any of them.  A place is read
        # as it is: the shell keeps track of which are quiet.
        HW._places.update({place: {"quiet_until": 9e9, "had": None} for place in (HW.CPU_LHM, HW.CPU_OHM, HW.CPU_ACPI, HW.CPU_THERMAL, HW.GPU_LHM)})
        said = {name: python.as_the_backend(f"/api/monitor/part?name={name}&target=1.1.1.1") for name in names}
        assert said["ping"] == {"ok": True, "reading": {"ms": 11}}
        assert said["ram"] == {"ok": True, "reading": {"used_pct": 43.0, "standby_mb": 3796}}
        assert said["gpu"]["reading"] == {"temp": None, "load": None, "clock": None, "vram_pct": None}
        assert said["gpu_lhm"]["reading"] == {"temp": None, "load": None, "clock": None}
        assert said["cpu"]["reading"] == {"temp": None, "load": said["cpu"]["reading"]["load"], "clock": 3700}
        assert said["temp_lhm"]["reading"] == {"degrees": 52.4}
        assert all(said[name]["reading"] == {"degrees": None} for name in ("temp_ohm", "temp_acpi", "temp_thermal"))
        assert all(place["quiet_until"] == 9e9 for place in HW._places.values()), "a part does not touch the places"
        assert set(said["dpc"]["reading"]) == {"dpc_pct", "isr_pct", "dpc_rate", "ok", "perfmon_ok"} and said["dpc"]["reading"]["dpc_rate"] == 245
        for not_one in ("/api/monitor/part", "/api/monitor/part?name=", "/api/monitor/part?name=loop", "/api/monitor/part?name=_monitor_loop"):
            assert python.as_the_backend(not_one) is None, not_one
        assert HW._monitor_state["running"] is False and HW._monitor_state["thread"] is None
        # A reading that raises is said to have raised: the shell drops the
        # pass, as the loop does.
        python.do(wmi("lhm_all", [{"Name": 5, "SensorType": "Load", "Value": 1, "Identifier": "gpu"}]))
        raised = python.as_the_backend("/api/monitor/part?name=gpu_lhm")
        assert raised["ok"] is False and raised["raised"] is True and raised["err"].startswith("AttributeError"), raised
    finally:
        python.close()


def test_the_shell_takes_the_monitor_from_its_first_call_and_never_runs_it_beside_the_backends():
    """One of each.  The shell answers the monitor's calls from the moment
    the app's shell is made (before its backend is started, before a page
    is shown), unless it was told to leave everything to the backend: then
    it answers none of them, and the backend's own monitor runs as it does
    today.  Its loop and what it keeps are the shell's: a backend that is
    started again has nothing of them to lose."""
    main = S.repo("shell_rs", "app", "src", "main.rs")
    assert "    if all_to_backend {\n" in main and "        shell.reads_with(Arc::new(moved::Real), true);\n" in main
    # (in `run`, before the window is made, and so before any page asks anything)
    run = main[main.index("\nfn run("):]
    assert run.index("shell.reads_with(Arc::new(moved::Real), true);") < run.index("tauri::Builder::default()")
    door = S.rust("src", "moved", "mod.rs")
    answer = door[door.index("    pub fn answer(&self, request: &Request) -> Option<Response> {"):door.index("/// The backend's call that starts a graphics driver's installer.")]
    assert "as_the_backend_reads_a_path(request.path())" in answer and 'path.starts_with("/api/monitor/")' in answer
    hw = S.rust("src", "moved", "hw.rs")
    taken = hw[hw.index("    pub fn answer(self: &Arc<Self>"):hw.index("    /// Start the monitor, with the ping going to `target`.")]
    assert re.findall(r'\("(\w+)", "(/api/monitor/\w+)"\)', taken) == [("POST", "/api/monitor/start"), ("POST", "/api/monitor/stop"),
                                                                        ("GET", "/api/monitor/snapshot")]
    # Behind the shell, the backend's loop can only be started by a start
    # that reaches it: only the route starts it.
    source = open(HW.__file__ if A is not None else os.path.join(m.REPO, "core", "hw_monitor.py"), encoding="utf-8").read()
    assert source.count("threading.Thread(target=_monitor_loop") == 1
    # ... by a start, or by a look that wakes it, each with the lock held:
    # two who look at once wake one loop.
    assert source.count("_start_the_loop()") == 3 and source.count("_wake()") == 3
    for where in ("def start_monitor(", "def get_monitor_snapshot("):
        body = source[source.index(where):source.index("\ndef ", source.index(where))]
        assert "    with _run_lock:\n" in body and body.index("with _run_lock:") < body.index("_wake()"), where
    everything = "".join(open(os.path.join(m.REPO, name), encoding="utf-8").read() for name in ("app.py",))
    assert everything.count("hw_monitor.start_monitor(") == 1 and "def api_monitor_start():" in everything


def test_the_shell_asks_its_backend_for_one_call_and_for_parts_by_name():
    """What the monitor asks of the backend: the one call for a part of a
    pass, by the part's name, and the page's ping target where that part is
    the ping.  Nothing else of the backend's is named in it."""
    hw = S.rust("src", "moved", "hw.rs")
    code = hw[:hw.index("#[cfg(test)]")]
    assert re.findall(r'"(/api/[^"]*)"', code) == ["/api/monitor/part", "/api/monitor/start", "/api/monitor/stop", "/api/monitor/snapshot"]
    asks = code[code.index("    fn the_backends(&self, name: &str, target: Option<&str>) -> Part {"):code.index("    // ─── The processor")]
    assert 'format!("{THE_BACKENDS_PART}?name={name}&target={}", quoted(target))' in asks and 'format!("{THE_BACKENDS_PART}?name={name}")' in asks
    assert code.count("self.the_backends(") == 7 and code.count("ask(&call)") == 1 and code.count("self.the_backends(part, None)") == 1
    assert re.findall(r'self\.the_backends\("(\w+)", (None|Some\(target\))\)', code) == [("cpu", "None"), ("gpu", "None"), ("gpu_lhm", "None"), ("ram", "None"),
                                                                                       ("dpc", "None"), ("ping", "Some(target)")]
    # (the four places of the processor's temperature, by the names of their parts)
    assert re.findall(r'\((CPU_\w+), "(temp_\w+)", "', code) == [("CPU_LHM", "temp_lhm"), ("CPU_OHM", "temp_ohm"), ("CPU_ACPI", "temp_acpi"),
                                                                    ("CPU_THERMAL", "temp_thermal")]


def test_the_questions_are_pythons_word_for_word():
    """What the shell asks WMI is what Python asks, and the backend's parts
    are the loop's own functions."""
    app()
    hw = S.rust("src", "moved", "hw.rs")
    python = open(HW.__file__, encoding="utf-8").read()
    # (that Python asks these and no others is held by every run of both())
    for namespace, query in WMI.values():
        assert f'"{query}"' in hw and namespace.replace("\\", "\\\\") in hw, (namespace, query)
    assert HW._TEMPERATURE_SENSORS == WMI["lhm_temps"][1] and "_sample_cpu" in python
    assert A._MONITOR_PARTS.keys() == {"cpu", "temp_lhm", "temp_ohm", "temp_acpi", "temp_thermal", "gpu", "gpu_lhm", "ram", "dpc", "ping"}
    for name, function in (("cpu", "_cpu_load_and_clock()"), ("temp_lhm", "_read_cpu_temp_via_lhm()"), ("temp_ohm", "_read_cpu_temp_via_ohm()"),
                           ("temp_acpi", "_read_cpu_temp_via_msacpi()"), ("temp_thermal", "_read_cpu_temp_via_perf_thermal()"),
                           ("gpu", "_gpu_from_the_card()"), ("gpu_lhm", "_read_gpu_via_lhm()"), ("ram", "_sample_ram()"), ("dpc", "_sample_dpc()"),
                           ("ping", "_sample_ping(target)")):
        assert f'"{name}": lambda target: ' in S.repo("app.py") and f"hw_monitor.{function}" in S.repo("app.py"), name
        assert f'self.the_backends("{name}"' in hw or f', "{name}", "' in hw, name
    # The places, the times: the same on both sides.
    for name in ("CPU_LHM", "CPU_OHM", "CPU_ACPI", "CPU_THERMAL", "GPU_LHM"):
        assert f'pub const {name}: &str = "{getattr(HW, name)}";' in hw, name
    assert HW.IDLE_STOP_S == 30.0 and "pub const IDLE_STOP_S: f64 = 30.0;" in hw
    assert HW.QUIET_S == 30.0 and "pub const QUIET_S: f64 = 30.0;" in hw
    assert HW._CPU_TIMES_FIRST_WAIT_S == 0.25 and "const FIRST_WAIT_S: f64 = 0.25;" in hw
    assert HW._CPU_TIMES_STALE_S == 10.0 and "const STALE_S: f64 = 10.0;" in hw
    assert HW.THRESHOLDS == {"cpu_temp": 90, "gpu_temp": 85, "ram_pct": 90, "gpu_vram_pct": 95}
    for line in ("const TOO_WARM_CPU: f64 = 90.0;", "const TOO_WARM_GPU: f64 = 85.0;", "const TOO_FULL_RAM: f64 = 90.0;", "const TOO_FULL_VRAM: f64 = 95.0;"):
        assert line in hw, line
    assert list(HW._metrics) == re.findall(r'"(\w+)"', hw[hw.index("const KEPT_OF: [&str; 15] = ["):hw.index("];", hw.index("const KEPT_OF"))])
    assert all(kept.maxlen == 60 for kept in HW._metrics.values()) and "pub const KEPT: usize = 60;" in hw
