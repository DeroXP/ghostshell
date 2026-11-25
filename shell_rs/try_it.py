"""Try the shell's core for real, and time it.

    cargo build --release --manifest-path shell_rs/core/Cargo.toml
    python shell_rs/try_it.py            five runs, and the middle value of each time
    python shell_rs/try_it.py 1          one run
    python shell_rs/try_it.py --moved    what a module that has moved into the shell costs, beside
                                         the same module in the backend (about a minute and a half)

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


def moved(program, appdata):
    """The performance sampler, the first module that moved into the shell
    (shell_rs/core/src/moved/perf.rs), beside the same module in the backend.

    Two shells, each with a backend on the fake machine, whose card is told
    to give every reading the same.  One answers the sampler's calls itself
    and asks its backend for the card's reading for every sample (the shell
    does not read the card itself: shell_rs/core/src/moved/mod.rs says
    why); the other passes the calls on, and the backend's own sampler
    runs.  Both sample for a minute, so that both hold the 300 samples a
    chart is made of, and then the same calls are timed in both.

    Real: the shell's sampling and its answers, and a call's way through
    the shell to a backend and back (which is now also what one reading of
    the card costs the shell).  A trial's: what the backend does for a
    reading (the fake machine's table, which costs nothing: on a PC it asks
    the native helper, 0.14 ms in docs/performance.md, or starts nvidia-smi
    without it)."""
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

    shells = {"the shell answers it": start("--readings", "real"), "passed on to the backend": start()}
    try:
        asks = {name: asker(p) for name, p in shells.items()}
        backends = {}
        for name, p in shells.items():
            json.loads(p.stdout.readline())
            assert asks[name](do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
            backends[name] = asks[name](do="state")["backend_pid"]
            assert asks[name](do="call", method="POST", target="/api/stand-in/card", token=True, body=json.dumps(A_CARD))["status"] == 200
        own, passed = asks["the shell answers it"], asks["passed on to the backend"]
        one = own(do="card", times=2000)
        assert one["reading"] and one["reading"]["temp_c"] == A_CARD["temp_c"], one
        print("One reading of the card as the shell takes it (it asks its backend, which here reads the fake machine):\n"
              "  %.3f ms (the slowest of 2,000: %.2f ms)." % (one["ms"], one["slowest_ms"]))
        began = {name: (time.perf_counter(), processor_seconds(p.pid), processor_seconds(backends[name])) for name, p in shells.items()}
        for ask in asks.values():
            said = ask(do="call", method="POST", target="/api/perf/start", token=True, body='{"consumer": "trial"}')
            assert said["status"] == 200 and json.loads(said["body"])["ok"], said
        print("Both samplers run; a minute until each holds the 300 samples a chart is made of ...")
        time.sleep(62)
        print("\nProcessor time used in that minute, as a share of one core (counted in the processor's cycles):\n")
        print("  %-28s %-12s %-14s %s" % ("", "the shell", "its backend", "together"))
        for name, p in shells.items():
            wall = time.perf_counter() - began[name][0]
            shell_used, backend_used = processor_seconds(p.pid) - began[name][1], processor_seconds(backends[name]) - began[name][2]
            held = json.loads(asks[name](do="call", method="GET", target="/api/perf/status", token=True)["body"])
            assert held["sample_count"] == 300 and held["last"]["ok"] and held["last"]["temp"] == A_CARD["temp_c"], held
            print("  %-28s %-12s %-14s %.2f%%" % (name + ":", "%.2f%%" % (100 * shell_used / wall), "%.2f%%" % (100 * backend_used / wall),
                                                 100 * (shell_used + backend_used) / wall))
            if name == "the shell answers it":
                print("  %-28s (the backend: %.2f ms of processor time for each of the %d readings it was asked for)"
                      % ("", 1000 * backend_used / (wall / 0.2), round(wall / 0.2)))
        print("\nThe same call 300 times, the middle value in milliseconds (and the slowest one in twenty):\n")
        print("  %-40s %-28s %s" % ("", *shells))
        for target in ("/api/perf/samples?seconds=60", "/api/perf/score?seconds=30", "/api/perf/status"):
            row = []
            for name in shells:
                took = sorted(asks[name](do="call", method="GET", target=target, token=True)["ms"] for _ in range(300))
                row.append("%.3f  (%.3f)" % (statistics.median(took), took[int(len(took) * 0.95)]))
            print("  %-40s %-28s %s" % (target, *row))
        seen = passed(do="state")
        assert seen["phase"] == "ready"
        for ask in asks.values():
            ask(do="call", method="POST", target="/api/perf/stop", token=True, body='{"consumer": "trial"}')
        print("\nReal: the shell's sampling and answers, and a call's way to a backend and back.\n"
              "A trial's: what the backend does for one reading of the card (the fake machine's table; on a PC it asks the helper).")
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
