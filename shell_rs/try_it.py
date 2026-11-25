"""Try the shell's core for real, and time it.

    cargo build --release --manifest-path shell_rs/core/Cargo.toml
    python shell_rs/try_it.py            five runs, and the middle value of each time
    python shell_rs/try_it.py 1          one run
    python shell_rs/try_it.py --moved    what a module that has moved into the shell costs, beside
                                         the same module in the backend, and what the graphics card's
                                         reader costs (about a minute and a half; it reads this PC's card)

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


def main(argv):
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
