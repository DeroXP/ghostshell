"""Try the shell's core for real, and time it.

    cargo build --release --manifest-path shell_rs/core/Cargo.toml
    python shell_rs/try_it.py            five runs, and the middle value of each time
    python shell_rs/try_it.py 1          one run

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


def main(argv):
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
