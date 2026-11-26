"""Try the shell's core for real, and time it.

    cargo build --release --manifest-path shell_rs/core/Cargo.toml
    python shell_rs/try_it.py            five runs, and the middle value of each time
    python shell_rs/try_it.py 1          one run
    python shell_rs/try_it.py --moved    what a module that has moved into the shell costs, beside
                                         the same module in the backend, and what the graphics card's
                                         reader costs (about a minute and a half; it reads this PC's card)
    python shell_rs/try_it.py --monitor  the same for the hardware monitor (about a minute and a half; it
                                         reads this PC, and its echo requests go to 127.0.0.1 only)

This starts the shell's trial tool (vispora-shell-try: the shell without a
window), which starts a backend, and takes the times that matter to somebody
waiting for the app: how soon the page is there, how soon the backend
answers, what a call costs on its way through the shell, and how long a quit
takes until both processes are gone.

The backend it starts is NOT the real one.  It is the app on the fake
machine of tests/sandbox.py (tests/shell_backend_stand_in.py): the same
Python, Flask and routes, with nothing on the PC read or changed.  So:

  * the shell's own times are real (its start, the page, its share of a
    call);
  * the backend's times are a trial's.  A real backend does its start-up
    work (recovery steps, boot prep) and a real quit undoes what a session
    changed; neither happens on the fake machine.

Never point the trial tool at the real backend: it changes the PC and would
meet a Vispora that is already running.

It is not part of `python tests/run_all.py`.
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STAND_IN = os.path.join(REPO, "tests", "shell_backend_stand_in.py")
NO_WINDOW = 0x08000000


def tool():
    folders = [os.path.join(HERE, "core", "target")]
    if os.environ.get("CARGO_TARGET_DIR"):
        folders.insert(0, os.environ["CARGO_TARGET_DIR"])
    for folder in folders:
        path = os.path.join(folder, "release", "vispora-shell-try.exe")
        if os.path.isfile(path):
            return path
    sys.exit("Not started: vispora-shell-try.exe has not been built (see the top of this file). "
             "If it was built into another folder, name that folder in CARGO_TARGET_DIR.")


def one_run(program, appdata):
    times = {}
    began = time.perf_counter()
    since = lambda: (time.perf_counter() - began) * 1000     # noqa: E731
    p = subprocess.Popen([program, "--backend", sys.executable, "--arg", "-u", "--arg", STAND_IN, "--arg", "--backend",
                          "--appdata", appdata],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, text=True,
                         encoding="utf-8", creationflags=NO_WINDOW)

    def ask(**what):
        p.stdin.write(json.dumps(what) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        if not line:
            sys.exit("The trial tool ended: " + p.stderr.read())
        return json.loads(line)

    try:
        json.loads(p.stdout.readline())
        times["the shell's process is up"] = since()
        page = ask(do="call", method="GET", target="/")
        assert page["status"] == 200
        times["the page is there (asked for and answered)"] = since()
        assert ask(do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
        times["the backend answers (first page usable)"] = since()
        state = ask(do="state")
        times["... of which the backend's own start"] = state["ready_ms"]
        first = ask(do="call", method="GET", target="/api/dashboard", token=True)
        assert first["status"] == 200, first["body"][:200]
        times["the first screen's data (on the fake machine)"] = first["ms"]
        calls = [ask(do="call", method="GET", target="/api/shell/hello", token=True)["ms"] for _ in range(300)]
        times["a call passed on to the backend and back"] = statistics.median(calls)
        times["... the slowest one in twenty of them"] = sorted(calls)[int(len(calls) * 0.95)]
        own = [ask(do="call", method="POST", target="/api/window/show", token=True)["ms"] for _ in range(300)]
        times["a call the shell answers itself"] = statistics.median(own)
        pages = [ask(do="call", method="GET", target="/")["ms"] for _ in range(50)]
        times["the page, each time it is asked for"] = statistics.median(pages)
        quit_began = time.perf_counter()
        p.stdin.write(json.dumps({"do": "quit"}) + "\n")
        p.stdin.flush()
        last = json.loads(p.stdout.readline())
        assert last["phase"] == "gone", last
        times["quit: until the backend's process has ended"] = last["quit_ms"]
        p.wait(60)
        times["quit: until the shell's process has ended too"] = (time.perf_counter() - quit_began) * 1000
        return times
    finally:
        if p.poll() is None:
            p.kill()


A_CARD = {"temp_c": 61, "gpu_util_pct": 97, "mem_util_pct": 40, "core_mhz": 2100, "mem_mhz": 9501, "core_max_mhz": 2200, "mem_max_mhz": 9800,
          "core_app_mhz": 2100, "mem_app_mhz": 9501, "power_mw": 215437, "power_limit_mw": 320000, "fan_pct": 55,
          "mem_used_bytes": 5_000_000_000, "mem_total_bytes": 12_000_000_000}


def processor_seconds(pid):
    """How much processor time the process `pid` has used so far.  Counted
    in the processor's own cycles, which Windows keeps exactly, and not in
    the time Windows charges at its clock's ticks: work that comes in short
    bursts, as five small calls a second do, is charged wrongly by those.
    (Asking takes the right to ask and nothing else.)"""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryProcessCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
    kernel32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if processor_seconds.cycles_a_second is None:
        # How many cycles a second of work is: this thread, kept busy for a while.
        def mine():
            cycles = ctypes.c_uint64(0)
            kernel32.QueryThreadCycleTime(kernel32.GetCurrentThread(), ctypes.byref(cycles))
            return cycles.value
        rates = []
        for _ in range(5):
            began, cycles = time.perf_counter(), mine()
            while time.perf_counter() - began < 0.1:
                pass
            rates.append((mine() - cycles) / (time.perf_counter() - began))
        processor_seconds.cycles_a_second = max(rates)      # (the highest: the round nothing else took time from)
    process = kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return float("nan")
    try:
        cycles = ctypes.c_uint64(0)
        if not kernel32.QueryProcessCycleTime(process, ctypes.byref(cycles)):
            return float("nan")
        return cycles.value / processor_seconds.cycles_a_second
    finally:
        kernel32.CloseHandle(process)


processor_seconds.cycles_a_second = None


def memory_of(pid):
    """What the process `pid` holds in memory now, in bytes: (what is in
    memory at this moment, what is its own and no other program's)."""
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in (
            "peak_working_set", "working_set", "peak_paged_pool", "paged_pool", "peak_other_pool", "other_pool", "page_file", "peak_page_file", "private")]
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return float("nan"), float("nan")
    try:
        counters = Counters()
        counters.size = ctypes.sizeof(Counters)
        if not kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), counters.size):
            return float("nan"), float("nan")
        return counters.working_set, counters.private
    finally:
        kernel32.CloseHandle(process)


def reader_beside(program):
    """The graphics card's reader, as cargo built it beside the trial tool."""
    return os.path.join(os.path.dirname(program), "VisporaGpuReader.exe")


def a_readers_start(reader, runs=7):
    """How long it is from starting the reader's program to its first
    answer, and whether that answer is a reading: the middle of `runs`
    starts, in milliseconds.  (This PC's card is read, and only read.)"""
    took, reads = [], False
    for _ in range(runs):
        began = time.perf_counter()
        p = subprocess.Popen([reader], stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=NO_WINDOW)
        try:
            p.stdin.write(b"\n")
            p.stdin.flush()
            said = json.loads(p.stdout.readline() or b"{}")
            took.append((time.perf_counter() - began) * 1000)
            reads = bool(said.get("ok"))
        finally:
            p.stdin.close()
            p.wait(10)
    return statistics.median(took), min(took), max(took), reads


def moved(program, appdata):
    """The performance sampler, the first module that moved into the shell
    (shell_rs/core/src/moved/perf.rs), beside the same module in the backend.

    Three shells, each with a backend on the fake machine, whose card is
    told to give every reading the same:

      * one answers the sampler's calls itself and reads the card through
        its own reader (VisporaGpuReader.exe, as it was built beside the
        trial tool): this PC's real card, which it only reads;
      * one answers the calls itself and has no reader: every reading is a
        call to its backend (as a shell whose pack carries no reader, and
        as it is while a driver is being installed);
      * one passes the calls on, and the backend's own sampler runs.

    Each samples for a minute, so that each holds the 300 samples a chart is
    made of, and then the same calls are timed in all three.

    Real: the reader and its reading of this PC's card, the shell's
    sampling and its answers, and a call's way through the shell to a
    backend and back.  A trial's: what the backend does for a reading (the
    fake machine's table, which costs nothing: on a PC it asks the native
    helper, 0.14 ms in docs/performance.md, or starts nvidia-smi without
    it)."""
    reader = reader_beside(program)
    if not os.path.isfile(reader):
        sys.exit("Not started: %s has not been built (it is built with the trial tool)." % reader)

    def start(*more):
        return subprocess.Popen([program, "--backend", sys.executable, "--arg", "-u", "--arg", STAND_IN, "--arg", "--backend",
                                 "--appdata", appdata, *more],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, text=True,
                                encoding="utf-8", creationflags=NO_WINDOW)

    def asker(p):
        def ask(**what):
            p.stdin.write(json.dumps(what) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
            if not line:
                sys.exit("The trial tool ended: " + p.stderr.read())
            return json.loads(line)
        return ask

    middle, least, most, reads = a_readers_start(reader)
    if not reads:
        print("This PC's graphics card gives the reader no reading (no NVIDIA card, or no driver): behind the shell every\n"
              "reading is the backend's on such a PC, and the reader is started now and then to see whether that has changed.\n"
              "A start of the reader that finds no card: %.0f ms (%.0f to %.0f)." % (middle, least, most))
        return 0
    print("The reader's start, from its program being started to its first reading of this PC's card:\n"
          "  %.0f ms (%.0f to %.0f in 7 starts)." % (middle, least, most))

    WITH, WITHOUT, PASSED = "the shell, with its reader", "the shell, asking its backend", "passed on to the backend"
    shells = {WITH: start("--readings", "real", "--card-reader", reader), WITHOUT: start("--readings", "real"), PASSED: start()}
    try:
        asks = {name: asker(p) for name, p in shells.items()}
        backends = {}
        for name, p in shells.items():
            json.loads(p.stdout.readline())
            assert asks[name](do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
            backends[name] = asks[name](do="state")["backend_pid"]
            assert asks[name](do="call", method="POST", target="/api/stand-in/card", token=True, body=json.dumps(A_CARD))["status"] == 200
        print("\nOne reading of the card as the shell takes it, the middle of 2,000 (and the slowest):")
        for name in (WITH, WITHOUT):
            asks[name](do="card", times=1)              # (the first one starts the reader)
            one = asks[name](do="card", times=2000)
            assert one["reading"], one
            print("  %-32s %.3f ms  (%.2f ms)" % (name + ":", one["ms"], one["slowest_ms"]))
        reader_pid = asks[WITH](do="state")["card_reader_pid"]
        assert reader_pid, "no reader runs"
        in_memory, its_own = memory_of(reader_pid)
        print("\nThe reader's process while it runs: %.1f MB in memory, %.1f MB of them its own." % (in_memory / 1e6, its_own / 1e6))
        began = {name: (time.perf_counter(), processor_seconds(p.pid), processor_seconds(backends[name])) for name, p in shells.items()}
        reader_began = processor_seconds(reader_pid)
        for ask in asks.values():
            said = ask(do="call", method="POST", target="/api/perf/start", token=True, body='{"consumer": "trial"}')
            assert said["status"] == 200 and json.loads(said["body"])["ok"], said
        print("All three samplers run; a minute until each holds the 300 samples a chart is made of ...")
        time.sleep(62)
        print("\nProcessor time used in that minute, as a share of one core (counted in the processor's cycles):\n")
        print("  %-32s %-11s %-11s %-12s %s" % ("", "the shell", "its reader", "its backend", "together"))
        for name, p in shells.items():
            wall = time.perf_counter() - began[name][0]
            shell_used, backend_used = processor_seconds(p.pid) - began[name][1], processor_seconds(backends[name]) - began[name][2]
            state = asks[name](do="state")
            reader_used = processor_seconds(reader_pid) - reader_began if name == WITH else 0.0
            if name == WITH:
                assert state["card_reader_pid"] == reader_pid and state["card_readings_from"]["backend"] == 0, state
            held = json.loads(asks[name](do="call", method="GET", target="/api/perf/status", token=True)["body"])
            assert held["sample_count"] == 300 and held["last"]["ok"], held
            print("  %-32s %-11s %-11s %-12s %.2f%%" % (name + ":", "%.2f%%" % (100 * shell_used / wall),
                                                       ("%.2f%%" % (100 * reader_used / wall)) if name == WITH else "-",
                                                       "%.2f%%" % (100 * backend_used / wall), 100 * (shell_used + reader_used + backend_used) / wall))
        print("\nThe same call 300 times, the middle value in milliseconds (and the slowest one in twenty):\n")
        print("  %-32s %-28s %-28s %s" % ("", *shells))
        for target in ("/api/perf/samples?seconds=60", "/api/perf/score?seconds=30", "/api/perf/status"):
            row = []
            for name in shells:
                took = sorted(asks[name](do="call", method="GET", target=target, token=True)["ms"] for _ in range(300))
                row.append("%.3f  (%.3f)" % (statistics.median(took), took[int(len(took) * 0.95)]))
            print("  %-32s %-28s %-28s %s" % (target, *row))
        for ask in asks.values():
            ask(do="call", method="POST", target="/api/perf/stop", token=True, body='{"consumer": "trial"}')
        print("\nReal: the reader and its reading of this PC's card, the shell's sampling and answers, and a call's way to a\n"
              "backend and back.  A trial's: what the backend does for one reading of the card (the fake machine's table;\n"
              "on a PC it asks the helper).")
        return 0
    finally:
        for p in shells.values():
            try:
                p.stdin.write(json.dumps({"do": "quit"}) + "\n")
                p.stdin.flush()
                p.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()


# The backend's own hardware monitor, for a minute, in a Python process of
# its own: core/hw_monitor.py with the app's native helper (as a source
# checkout has it built), its data folder redirected, its echo requests to
# 127.0.0.1.  It only reads.  It says how many programs it started.  It is
# asked for the snapshot every three seconds, as the page asks while it is
# on screen: a monitor nobody looks at sleeps after half a minute.
THE_BACKENDS_MONITOR = r"""
import atexit, json, os, shutil, statistics, sys, tempfile, time
root = tempfile.mkdtemp(prefix="vispora_measure_")
atexit.register(shutil.rmtree, root, True)
for var, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
    os.environ[var] = os.path.join(root, sub)
    os.makedirs(os.environ[var], exist_ok=True)
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import config
assert config.APPDATA_DIR.startswith(root), config.APPDATA_DIR
import core.utils as U
from core import native
if not native.available():
    print(json.dumps({"error": "the native helper is not built: " + native.status()["why"]}))
    sys.exit(0)
started = []
_ps, _cmd = U.run_ps, U.run_cmd
def run_ps(command, timeout=60):
    started.append("powershell")
    return _ps(command, timeout)
def run_cmd(args, timeout=60):
    started.append(os.path.basename(str(args[0])))
    return _cmd(args, timeout)
U.run_ps, U.run_cmd = run_ps, run_cmd
from core import hw_monitor
print(json.dumps({"ready": True}), flush=True)
sys.stdin.readline()
hw_monitor.start_monitor("127.0.0.1")
end = time.monotonic() + float(sys.argv[2])
while time.monotonic() < end:
    time.sleep(min(3.0, max(0.0, end - time.monotonic())))
    hw_monitor.get_monitor_snapshot()
hw_monitor.stop_monitor()
time.sleep(1.5)
passes = len(hw_monitor._metrics["cpu_load"])
in_the_loop = len(started)
took = []
for _ in range(int(sys.argv[3])):
    began = time.perf_counter()
    hw_monitor._sample_cpu(); hw_monitor._sample_gpu(); hw_monitor._sample_ram(); hw_monitor._sample_dpc(); hw_monitor._sample_ping("127.0.0.1")
    spent = time.perf_counter() - began
    took.append(spent * 1000)
    time.sleep(max(0.0, 1.0 - spent))
took.sort()
print(json.dumps({"passes": passes, "started_in_the_loop": in_the_loop, "started": len(started), "ms": statistics.median(took), "slowest_ms": took[-1],
                  "latest": hw_monitor._monitor_state["latest"]}, default=str), flush=True)
"""


def monitor(program, appdata, seconds=60, timed=20):
    """The hardware monitor, the second module that moved into the shell
    (shell_rs/core/src/moved/hw.rs), beside the same module in the backend.

    Both read this PC, at the same time, for a minute, and only read: the
    processor's times and clock, the memory and the standby list (read, not
    emptied), DPCs a second, WMI for the processor's temperature, the
    graphics card, and one echo request a second to 127.0.0.1.

      * the shell (the trial tool, with the card's reader beside it): its
        monitor is started by the page's call, and its processor time, its
        reader's and its backend's are counted (the backend is the stand-in
        on the fake machine, and is asked for a part of a pass only where
        this PC leaves the shell without a reading);
      * the backend's own monitor: core/hw_monitor.py in a Python process of
        its own, with the native helper as a source checkout has it built.

    The page's call for the snapshot is made of each every three seconds,
    as the page makes it while it is on screen (a monitor that nobody looks
    at sleeps after half a minute).  Then each takes 20 more passes, one a
    second, each of them timed."""
    reader = reader_beside(program)
    if not os.path.isfile(reader):
        sys.exit("Not started: %s has not been built (it is built with the trial tool)." % reader)
    shell = subprocess.Popen([program, "--backend", sys.executable, "--arg", "-u", "--arg", STAND_IN, "--arg", "--backend", "--appdata", appdata,
                              "--readings", "real", "--card-reader", reader],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, text=True, encoding="utf-8",
                             creationflags=NO_WINDOW)
    python = subprocess.Popen([sys.executable, "-u", "-c", THE_BACKENDS_MONITOR, REPO, str(seconds), str(timed)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    # (a third: a shell that passes the monitor's calls on, for the time of
    # the page's call answered by a backend whose own monitor ran a minute)
    passing = subprocess.Popen([program, "--backend", sys.executable, "--arg", "-u", "--arg", STAND_IN, "--arg", "--backend", "--appdata", appdata],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO, text=True, encoding="utf-8",
                               creationflags=NO_WINDOW)
    try:
        ask = None

        def asking(p):
            def ask(**what):
                p.stdin.write(json.dumps(what) + "\n")
                p.stdin.flush()
                line = p.stdout.readline()
                if not line:
                    sys.exit("The trial tool ended: " + p.stderr.read())
                return json.loads(line)
            return ask
        ask, ask_passing = asking(shell), asking(passing)
        json.loads(shell.stdout.readline())
        assert ask(do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
        json.loads(passing.stdout.readline())
        assert ask_passing(do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
        said = ask_passing(do="call", method="POST", target="/api/monitor/start", token=True, body='{"ping_target": "127.0.0.1"}')
        assert said["status"] == 200, said
        backend = ask(do="state")["backend_pid"]
        ready = json.loads(python.stdout.readline() or "{}")
        if not ready.get("ready"):
            sys.exit("The backend's monitor could not be measured: %r %s" % (ready, python.stderr.read()[-2000:]))
        said = ask(do="call", method="POST", target="/api/monitor/start", token=True, body='{"ping_target": "127.0.0.1"}')
        assert said["status"] == 200 and json.loads(said["body"]) == {"ok": True}, said
        python.stdin.write("go\n")
        python.stdin.flush()
        began = time.perf_counter()
        counted = {"shell": processor_seconds(shell.pid), "backend": processor_seconds(backend), "python": processor_seconds(python.pid)}
        print("Both monitors run, the shell's and the backend's own; a minute ...")
        while time.perf_counter() - began < seconds:
            time.sleep(max(0.0, min(3.0, seconds - (time.perf_counter() - began))))
            for asks in (ask, ask_passing):         # (the page on screen; the shell that passes it on, too)
                asks(do="call", method="GET", target="/api/monitor/snapshot", token=True)
        wall = time.perf_counter() - began
        used = {"shell": processor_seconds(shell.pid) - counted["shell"], "backend": processor_seconds(backend) - counted["backend"],
                "python": processor_seconds(python.pid) - counted["python"]}
        reader_pid = ask(do="state")["card_reader_pid"]
        snapshot = json.loads(ask(do="call", method="GET", target="/api/monitor/snapshot", token=True)["body"])
        answered = {}
        for name, asks in (("the shell", ask), ("passed on to the backend", ask_passing)):
            took = sorted(asks(do="call", method="GET", target="/api/monitor/snapshot", token=True)["ms"] for _ in range(300))
            answered[name] = (statistics.median(took), took[int(len(took) * 0.95)])
        ask(do="call", method="POST", target="/api/monitor/stop", token=True)
        reader_used = processor_seconds(reader_pid) if reader_pid else 0.0
        mine = ask(do="monitor", times=timed, every_ms=1000)
        theirs = json.loads(python.stdout.readline() or "{}")
        if "error" in theirs or "ms" not in theirs:
            sys.exit("The backend's monitor could not be measured: %r %s" % (theirs, python.stderr.read()[-2000:]))
        print("\nProcessor time used in that minute, as a share of one core (counted in the processor's cycles):\n")
        print("  %-36s %s" % ("the shell's monitor:", "%.2f%%" % (100 * used["shell"] / wall)))
        print("  %-36s %s" % ("  its card's reader (since its start):", "%.2f%%" % (100 * reader_used / wall)))
        print("  %-36s %s" % ("  its backend (the stand-in):", "%.2f%%" % (100 * used["backend"] / wall)))
        print("  %-36s %s" % ("the backend's own monitor (Python):", "%.2f%%" % (100 * used["python"] / wall)))
        print("\nPasses in that minute: the shell's %d, the backend's own %d.  Programs started by the backend's own: %d."
              % (len(snapshot["history"]["cpu_load"]), theirs["passes"], theirs["started_in_the_loop"]))
        print("The shell asked its backend for a part of a pass %d times (0: this PC gave it every reading itself)." % mine["asked_the_backend"])
        print("\nThe page's call for the snapshot (a minute of readings), 300 times: the middle value in milliseconds (the slowest in twenty):\n")
        for name, (middle, slow) in answered.items():
            print("  %-36s %.3f  (%.3f)" % (name + ":", middle, slow))
        print("\nOne pass, timed, the middle of %d taken one a second (and the slowest), in milliseconds:\n" % timed)
        print("  %-36s %.2f  (%.2f)" % ("the shell:", mine["ms"], mine["slowest_ms"]))
        print("  %-36s %.2f  (%.2f)" % ("the backend's own (Python):", theirs["ms"], theirs["slowest_ms"]))
        print("\nThe last readings of each, side by side:\n")
        for name in ("cpu_temp", "cpu_temp_source", "cpu_load", "cpu_clock", "gpu_temp", "gpu_load", "gpu_clock", "gpu_vram_pct", "ram_pct", "ram_standby_mb",
                     "dpc_pct", "isr_pct", "dpc_rate", "net_ping_ms"):
            print("  %-18s %-24s %s" % (name, json.dumps(mine["latest"].get(name)), json.dumps(theirs["latest"].get(name))))
        return 0
    finally:
        for p in (shell, python, passing):
            try:
                if p is not python:
                    p.stdin.write(json.dumps({"do": "quit"}) + "\n")
                    p.stdin.flush()
                p.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()


def main(argv):
    if argv == ["--monitor"]:
        with tempfile.TemporaryDirectory(prefix="vispora-shell-trial-") as appdata:
            return monitor(tool(), appdata)
    if argv == ["--moved"]:
        with tempfile.TemporaryDirectory(prefix="vispora-shell-trial-") as appdata:
            return moved(tool(), appdata)
    runs = int(argv[0]) if argv else 5
    program = tool()
    with tempfile.TemporaryDirectory(prefix="vispora-shell-trial-") as appdata:
        all_runs = [one_run(program, appdata) for _ in range(runs)]
    print(f"{runs} run(s); the middle value of each, in milliseconds (lowest to highest)\n")
    for name in all_runs[0]:
        values = [run[name] for run in all_runs]
        print(f"  {name:52s} {statistics.median(values):9.1f}   ({min(values):.1f} to {max(values):.1f})")
    print("\nThe backend in this trial is the app on the fake machine: its times are a trial's, not a PC's.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
