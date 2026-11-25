"""A backend for trying the shell (shell_rs/) without the real one.

The real backend is app.py as it runs on a PC: it changes the system when it
starts and would meet a Vispora that is already running.  This is app.py on
the fake machine of tests/sandbox.py instead: the same routes, the same check
of who is calling, the same hand-over from the shell, the same quit, with
nothing on the PC read or changed and the app's data in a throwaway folder.

    python tests/shell_backend_stand_in.py --backend [--slow-start SECONDS]
                                                      [--ignore-quit] [--die-with CODE]

It is started by the shell's trial tool (vispora-shell-try, and through it by
tests/test_shell_rs.py and shell_rs/try_it.py) with the shell's hand-over in
its environment, as the real backend would be.  It is not a test and is not
run by tests/run_all.py.

What it leaves out is what app.py's main() does around the server: the
start-up work (which the sandbox does not cover in one piece) and the window,
which behind the shell there is none of anyway.

    --slow-start S   listen only after S seconds, as a slow start would
    --ignore-quit    answer the quit and stay: a backend that hangs in it
    --die-with N     end with exit code N instead of listening

Whatever it is told, it ends by itself ten minutes after its start, so that
nothing is left behind by a trial that went wrong.

With VISPORA_STAND_IN_NOTES=<file> in its environment it writes a line there
each time it puts the (fake) system back, with the reason: that is how a test
sees that a backend restored before it went.  With VISPORA_STAND_IN_CALLS=<file>
it writes a line for every call it answers (the time, the status, the
method and the path): that is how a trial sees what the window's page asked.
"""
import sandbox as m                                 # noqa: F401  (first: the fake machine)

import os
import sys
import threading
import time

import app as A

LIFETIME_S = 600
_really_exit = os._exit


def _value(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main():
    try:
        handoff = A._take_shell_handoff()
    except A.ShellHandoffError as e:
        print(f"Not started: {e}", file=sys.stderr)
        return 2
    if handoff is None:
        print("Not started: this is a backend for the shell's trial tool; it needs --backend and a hand-over",
              file=sys.stderr)
        return 2
    threading.Timer(LIFETIME_S, lambda: _really_exit(4)).start()
    A._serve_for_the_shell(handoff)
    notes = os.environ.get("VISPORA_STAND_IN_NOTES")
    if notes:
        restore = A._restore_system_before_exit

        def noted(reason):
            restore(reason)
            with open(notes, "a", encoding="utf-8") as f:
                f.write(f"put back: {reason}\n")

        A._restore_system_before_exit = noted
    calls = os.environ.get("VISPORA_STAND_IN_CALLS")
    if calls:
        def note(answer):
            with open(calls, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {answer.status_code} {A.request.method} {A.request.path}\n")
            return answer

        A.app.after_request(note)
    if "--die-with" in sys.argv:
        return int(_value("--die-with", "1"))
    time.sleep(float(_value("--slow-start", "0")))
    if "--ignore-quit" in sys.argv:
        os._exit = lambda code: None                # what the quit, and the shell going away, end the process with
    threading.Thread(target=A.start_flask, daemon=True, name="stand-in-server").start()
    A._leave_with_the_shell()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    _really_exit(main())
