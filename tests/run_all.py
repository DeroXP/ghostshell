"""Run the app's test suite.

    python tests/run_all.py              every tests/test_*.py
    python tests/run_all.py reset game   only files whose name contains "reset" or "game"
    python tests/run_all.py -v           also list the tests that passed

Each test file runs in its own Python process.  That is deliberate: the files
install a fake machine (tests/sandbox.py) before importing any app module, and
several of them change import-time behaviour that cannot be undone in a running
interpreter.  No third-party test runner is needed — a test is any function
named test_* in a test_*.py file, and it fails by raising (normally an assert).
unittest.SkipTest marks a test as skipped.

Releases are built with a different interpreter than the default one on this
machine, so before a release run the suite on that one too:

    <path to the build's python.exe> tests/run_all.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _run_file(path: str) -> int:
    """Child mode: import one test file and run its test_* functions."""
    os.chdir(REPO)
    for p in (REPO, HERE):
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.dont_write_bytecode = True
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except BaseException:
        print(f"ERROR {name} (could not be imported)\n{traceback.format_exc()}", flush=True)
        return 1
    failed = 0
    for attr, fn in list(vars(module).items()):
        if not attr.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {attr}", flush=True)
        except unittest.SkipTest as e:
            print(f"SKIP  {attr}: {e}", flush=True)
        except AssertionError as e:
            failed += 1
            frame = traceback.extract_tb(e.__traceback__)[-1]
            detail = str(e) or (frame.line or "")
            print(f"FAIL  {attr} (line {frame.lineno}): {detail}", flush=True)
        except BaseException:
            failed += 1
            print(f"ERROR {attr}\n{traceback.format_exc()}", flush=True)
    return 1 if failed else 0


def main(argv: list) -> int:
    if len(argv) >= 2 and argv[0] == "--file":
        code = _run_file(argv[1])
        sys.stdout.flush()
        os._exit(code)      # no exit hooks, no waiting on the app's daemon threads

    verbose = "-v" in argv
    wanted = [a.lower() for a in argv if not a.startswith("-")]
    files = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    if wanted:
        files = [f for f in files if any(w in f.lower() for w in wanted)]
    if not files:
        print("no test files matched")
        return 1

    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    totals = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    started = time.time()
    for f in files:
        t0 = time.time()
        proc = subprocess.run([sys.executable, "-u", os.path.abspath(__file__), "--file", os.path.join(HERE, f)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=REPO)
        lines = (proc.stdout or "").splitlines()
        counts = {k: sum(1 for ln in lines if ln.startswith(k + " ")) for k in totals}
        for k in totals:
            totals[k] += counts[k]
        crashed = proc.returncode != 0 and not (counts["FAIL"] or counts["ERROR"])
        if crashed:
            totals["ERROR"] += 1
        bad = counts["FAIL"] + counts["ERROR"] + (1 if crashed else 0)
        status = "ok" if not bad else "FAILED"
        skipped = f", {counts['SKIP']} skipped" if counts["SKIP"] else ""
        print(f"{f:<32} {counts['PASS']:>3} passed{skipped}  {status}  ({time.time() - t0:.1f}s)")
        showing = False
        for ln in lines:
            head = ln.split(" ", 1)[0]
            if head in totals:
                showing = head in ("FAIL", "ERROR") or (head == "SKIP") or (verbose and head == "PASS")
            if showing:
                print("    " + ln)
        if crashed:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
            print("    the test process stopped early (exit code %s):" % proc.returncode)
            for ln in tail:
                print("    " + ln)
    bad = totals["FAIL"] + totals["ERROR"]
    print(f"\n{totals['PASS']} passed, {bad} failed, {totals['SKIP']} skipped "
          f"in {time.time() - started:.0f}s" + ("" if bad else "  -  ALL PASS"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
