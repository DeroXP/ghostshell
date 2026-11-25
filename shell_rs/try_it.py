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


def processor_seconds(process):
    """How much processor time a process has used so far, as Windows counts it."""
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    if not kernel32.GetProcessTimes(int(process._handle), *[ctypes.byref(t) for t in times]):
        return float("nan")
    return sum((t.dwHighDateTime << 32 | t.dwLowDateTime) for t in times[2:]) / 1e7


def moved(program, appdata):
    """The performance sampler, the first module that moved into the shell
    (shell_rs/core/src/moved/perf.rs), beside the same module in the backend.

    Two shells, each with a backend on the fake machine.  One reads this
    PC's graphics card itself and answers the sampler's calls itself; the
    other passes them on, and the backend's sampler runs on the fake
    machine, which is told to give every sample the same reading.  Both
    sample for a minute, so that both hold the 300 samples a chart is made
    of, and then the same calls are timed in both.

    Real: the shell's reading of this PC's card (it only reads), its
    sampling, its answers, and a call's way through the shell to a backend
    and back.  A trial's: the backend's reading (the fake machine's table,
    which costs nothing: on a PC it asks the native helper, 0.14 ms in
    docs/performance.md, or starts nvidia-smi without it)."""
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
        for name, p in shells.items():
            json.loads(p.stdout.readline())
            assert asks[name](do="wait", **{"for": "ready"}, within=90)["phase"] == "ready"
        own, passed = asks["the shell answers it"], asks["passed on to the backend"]
        one = own(do="reading", question="gpu_live", times=2000)
        if not one["answer"] or one["answer"].get("temp_c") is None:
            print("This PC's graphics card gives the shell no reading (no NVIDIA card, or no driver): the sampler's calls are\n"
                  "the backend's on such a PC, and there is nothing of the shell's to time.")
            return 0
        print("One reading of this PC's card by the shell, in its own process: %.3f ms (the slowest of 2,000: %.2f ms)."
              % (one["ms"], one["slowest_ms"]))
        assert passed(do="call", method="POST", target="/api/stand-in/card", token=True, body=json.dumps(A_CARD))["status"] == 200
        began = {name: (time.perf_counter(), processor_seconds(p)) for name, p in shells.items()}
        for ask in asks.values():
            said = ask(do="call", method="POST", target="/api/perf/start", token=True, body='{"consumer": "trial"}')
            assert said["status"] == 200 and json.loads(said["body"])["ok"], said
        print("Both samplers run; a minute until each holds the 300 samples a chart is made of ...")
        time.sleep(62)
        for name, p in shells.items():
            wall, used = time.perf_counter() - began[name][0], processor_seconds(p) - began[name][1]
            held = json.loads(asks[name](do="call", method="GET", target="/api/perf/status", token=True)["body"])
            print("  %-26s %d samples held; the shell's process used %.2f s of processor time in %.0f s (%.2f%% of one core)"
                  % (name + ":", held["sample_count"], used, wall, 100 * used / wall))
            assert held["sample_count"] == 300 and held["last"]["ok"], held
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
        print("\nReal: the shell's own reading, sampling and answers, and a call's way to a backend and back.\n"
              "A trial's: what the backend's sampler reads (the fake machine's table; on a PC it asks the helper).")
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
