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
(For the calls a shell makes for a part of the hardware monitor's or the
network monitor's pass, the path is written with its query: which part,
and for the ping, to whom.)

POST /api/stand-in/network-game with {"exe": "..."} makes the game monitor
say to the network monitor that this game runs ({"exe": null}: none), as it
says it on a PC while a game is played.
With VISPORA_STAND_IN_SECRET=<file> it writes the run's secret there, as it
was handed over: that is how a trial knows what to look for when it checks
that the secret is nowhere it should not be.  (The file is the trial's own,
in its throwaway folder; the real backend writes the secret nowhere.)

One call is the stand-in's own and not the app's: POST /api/stand-in/game
with {"name": "..."} makes the fake machine look as if that game were
running ({"name": null}: as if it had ended).  That is how a trial sees the
tray icon change, which on a real PC takes a real game.  It needs the
run's secret like every other call.  So does POST /api/stand-in/tray-state
with {"broken": true}, after which the app's call that says what the tray
shows fails until {"broken": false}: that is how a trial sees how often a
backend that is up, and gives no usable answer, is asked.
"""
import sandbox as m                                 # noqa: F401  (first: the fake machine)

import os
import subprocess
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
    secret = os.environ.get("VISPORA_STAND_IN_SECRET")
    if secret:
        with open(secret, "w", encoding="utf-8") as f:
            f.write(handoff["token"])
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
            path = A.request.full_path.rstrip("?") if A.request.path in ("/api/monitor/part", "/api/network/part") else A.request.path
            with open(calls, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {answer.status_code} {A.request.method} {path}\n")
            return answer

        A.app.after_request(note)
    @A.app.route("/api/stand-in/game", methods=["POST"])
    def a_game_runs_or_not():
        from core import game_profiles
        name = (A.request.get_json(silent=True) or {}).get("name") or None
        game_profiles._engine["active_game"] = name
        game_profiles._engine["active_pid"] = 4242 if name else None
        return A.jsonify({"ok": True, "active_game": name})

    @A.app.route("/api/stand-in/network-game", methods=["POST"])
    def the_network_monitors_game():
        from core import game_profiles
        exe = (A.request.get_json(silent=True) or {}).get("exe") or None
        game_profiles.get_active_game_info = (lambda: {"exe": exe, "display": exe, "pid": 4242, "oc_core": 0, "oc_mem": 0}) if exe else (lambda: None)
        return A.jsonify({"ok": True, "exe": exe})

    @A.app.route("/api/stand-in/card", methods=["POST"])
    def the_card_says():
        # What the native helper says about the graphics card on the fake
        # machine (nothing, unless a trial says so here): what the backend's
        # own sampler samples, and what the shell is told when it asks the
        # backend for the card's reading (/api/gpu/oc/live).
        says = A.request.get_json(force=True, silent=True) or None
        if says:
            m.NATIVE["gpu_live"] = says
        else:
            m.NATIVE.pop("gpu_live", None)
        return A.jsonify({"ok": True, "card": says})

    # The call that starts a graphics driver's installer.  Nothing of an
    # install is ever run by a trial, not on the fake machine either: the
    # app's own function is taken out of the call, which answers what a
    # trial tells it to (that an installer was started, or that none was).
    # At the moment the call arrives it looks whether a reader for the
    # graphics card runs on this PC, and keeps what it found: the shell has
    # to have ended its reader before it passes the call on.
    install = {"starts": False, "readers_when_asked": []}

    def readers_now():
        listed = m.REAL_POPEN([os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "tasklist.exe"), "/fi",
                               "imagename eq VisporaGpuReader.exe", "/fo", "csv", "/nh"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).communicate()[0]
        return sorted(int(row.split('","')[1]) for row in listed.splitlines() if row.lower().startswith('"visporagpureader.exe"'))

    def the_install_is_asked_for():
        install["readers_when_asked"].append(readers_now())
        if install["starts"]:
            return A.jsonify({"ok": True, "msg": "NVIDIA installer launched. (A trial: nothing was started.)"})
        return A.jsonify({"ok": False, "err": "Driver installer not downloaded yet"})

    A.app.view_functions["api_driver_install"] = the_install_is_asked_for

    @A.app.route("/api/stand-in/driver-install", methods=["POST"])
    def an_install_starts_or_not():
        told = A.request.get_json(force=True, silent=True) or {}
        if "starts" in told:
            install["starts"] = bool(told["starts"])
        return A.jsonify({"ok": True, "starts": install["starts"], "readers_when_asked": install["readers_when_asked"]})

    whole = A.app.view_functions["api_tray_state"]

    @A.app.route("/api/stand-in/tray-state", methods=["POST"])
    def the_trays_state_fails_or_not():
        broken = bool((A.request.get_json(silent=True) or {}).get("broken"))
        A.app.view_functions["api_tray_state"] = (
            lambda: (A.jsonify({"ok": False, "err": "made to fail for a trial"}), 500)) if broken else whole
        return A.jsonify({"ok": True, "broken": broken})

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
