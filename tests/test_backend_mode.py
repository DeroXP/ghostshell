"""app.py as the backend of the shell (shell_rs/).

The shell is Vispora.exe in the making: it owns the window, the tray and
quitting, and starts this app as its backend with `--backend` and a
hand-over in one environment variable.  No release starts the app this way
yet.  What is checked here is the app's side of that arrangement:

  * the hand-over: read, checked, and taken out of the environment;
  * the backend serves on the socket the shell made and hands over, and on
    no port of its own;
  * behind the shell, only the shell's address and secret are answered;
  * the backend leaves when the shell is gone, after putting the system
    back, and does not mistake "cannot be watched" for "gone";
  * main() leaves to the shell what is the shell's.

The other side, and the two together, are in tests/test_shell_rs.py.
"""
import sandbox as m

import contextlib
import ctypes
import inspect
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

import config

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

PORT = 49213
TOKEN = "s3cret-" + "t" * 36 + "_AZaz09"
SHELL_PID = 4321
SHELL_STARTED = 134088000000000000              # when that process was started, as Windows counts it
LISTEN = 812                                     # the number the shell's socket is handed over under


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def environment(**changes):
    given = {"port": PORT, "listen": LISTEN, "token": TOKEN, "shell_pid": SHELL_PID, "shell_started": SHELL_STARTED}
    given.update(changes)
    return {app()._SHELL_HANDOFF_VAR: json.dumps(given)}


def a_listening_socket(address="127.0.0.1", listening=True):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((address, 0))
    if listening:
        sock.listen(16)
    return sock, sock.getsockname()[1]


@contextlib.contextmanager
def behind_the_shell(pid=SHELL_PID):
    """The app module as a shell's backend, and as it was again afterwards.
    Gives the port of the socket "the shell" made for it."""
    a = app()
    before = (a._behind_shell, a._shell_listener, a._API_TOKEN, a._LOCAL_HOSTS, a._LOCAL_ORIGINS,
              a._TOKEN_EXEMPT_PATHS, config.APP_PORT)
    made, port = a_listening_socket()
    a._serve_for_the_shell({"port": port, "listen": made.fileno(), "token": TOKEN, "shell_pid": pid,
                            "shell_started": SHELL_STARTED})
    try:
        yield port
    finally:
        a._shell_listener.detach()                  # (the same socket as `made`, which closes it)
        made.close()
        (a._behind_shell, a._shell_listener, a._API_TOKEN, a._LOCAL_HOSTS, a._LOCAL_ORIGINS,
         a._TOKEN_EXEMPT_PATHS, config.APP_PORT) = before


# ═════════════════════════════════════════════════════════════════════
# The hand-over
# ═════════════════════════════════════════════════════════════════════
def test_an_ordinary_start_has_no_hand_over():
    a = app()
    assert a._take_shell_handoff(["Vispora.exe"], {}) is None
    assert a._take_shell_handoff(["Vispora.exe", "--from-autostart"], {"PATH": "x"}) is None
    # (the program's own name is not an argument)
    assert a._take_shell_handoff(["--backend"], environment()) is None
    # A variable left in the environment by whoever started the app is not
    # passed on to the programs the app starts.
    env = {**environment(), "PATH": "x"}
    assert a._take_shell_handoff(["Vispora.exe"], env) is None and env == {"PATH": "x"}


def test_the_hand_over_is_read_and_taken_out_of_the_environment():
    a = app()
    env = {**environment(), "PATH": "x"}
    got = a._take_shell_handoff(["Vispora.exe", "--backend", "--from-autostart"], env)
    assert got == {"port": PORT, "listen": LISTEN, "token": TOKEN, "shell_pid": SHELL_PID,
                   "shell_started": SHELL_STARTED}
    assert env == {"PATH": "x"}, "the secret is not inherited by the programs the app starts"


def test_a_hand_over_that_cannot_be_used_is_refused():
    a = app()
    argv = ["Vispora.exe", "--backend"]
    bad = [{}, {a._SHELL_HANDOFF_VAR: ""}, {a._SHELL_HANDOFF_VAR: "not json"}, {a._SHELL_HANDOFF_VAR: "[]"},
           {a._SHELL_HANDOFF_VAR: json.dumps({"port": PORT, "token": TOKEN})},
           {a._SHELL_HANDOFF_VAR: json.dumps({"port": PORT, "shell_pid": 1})},
           # (the hand-over of the first version of the shell: no socket, no start time)
           {a._SHELL_HANDOFF_VAR: json.dumps({"port": PORT, "token": TOKEN, "shell_pid": SHELL_PID})}]
    bad += [environment(port=p) for p in (80, 1023, 65536, 0, -1, str(PORT), True, PORT + 0.5, None)]
    bad += [environment(token=t) for t in ("short", "x" * 31, "x" * 40 + "!", "x" * 40 + " ", "é" * 40, 12345, None,
                                           ["x" * 40])]
    bad += [environment(shell_pid=p) for p in (0, -5, "4321", True, 1.5, None)]
    bad += [environment(shell_started=s) for s in (0, -1, "134088000000000000", True, 1.5, None)]
    bad += [environment(listen=h) for h in (0, -1, "812", True, 8.5, None)]
    for env in bad:
        try:
            a._take_shell_handoff(argv, env)
        except a.ShellHandoffError:
            assert a._SHELL_HANDOFF_VAR not in env, "taken out of the environment also when it is refused"
        else:
            raise AssertionError(f"taken as a hand-over: {env}")
    # ... and the edges of what is taken.
    for good in (environment(port=1024), environment(port=65535), environment(token="A" * 32),
                 environment(shell_pid=1), environment(shell_started=1), environment(listen=4)):
        assert a._take_shell_handoff(argv, good) is not None


# ═════════════════════════════════════════════════════════════════════
# Whom the backend answers
# ═════════════════════════════════════════════════════════════════════
def test_behind_the_shell_only_the_shells_address_and_secret_are_answered():
    a = app()
    own_token, own_host = a._API_TOKEN, f"127.0.0.1:{config.APP_PORT}"
    with behind_the_shell() as PORT:
        c = a.app.test_client()
        host = {"Host": f"127.0.0.1:{PORT}"}
        hello = c.get("/api/shell/hello", headers={**host, a._API_TOKEN_HEADER: TOKEN})
        assert hello.status_code == 200
        assert hello.get_json() == {"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION,
                                    "pid": hello.get_json()["pid"], "behind_shell": True}
        # Not the address it had on its own, and not localhost: the shell asks for 127.0.0.1.
        for other in (own_host, f"localhost:{PORT}", f"127.0.0.1:{PORT + 1}", "evil.example"):
            r = c.get("/api/shell/hello", headers={"Host": other, a._API_TOKEN_HEADER: TOKEN})
            assert r.status_code == 403 and r.get_json()["err"] == "bad host", other
        # Not the secret it made up for itself, and not none.
        for token in (own_token, "", "x" * len(TOKEN)):
            r = c.get("/api/shell/hello", headers={**host, a._API_TOKEN_HEADER: token})
            assert r.status_code == 403 and r.get_json()["err"] == "missing or wrong session token"
        assert c.get("/api/shell/hello", headers=host).status_code == 403
        # The call a second copy of the app made without the secret is the shell's now.
        assert c.post("/api/window/show", headers=host).status_code == 403
        shown = c.post("/api/window/show", headers={**host, a._API_TOKEN_HEADER: TOKEN})
        assert shown.status_code == 200 and shown.get_json() == {"ok": False}, "there is no window here to show"
        # A page somewhere that made the browser call: refused as before.
        r = c.post("/api/network/undo", headers={**host, a._API_TOKEN_HEADER: TOKEN, "Origin": "https://evil.example"})
        assert r.status_code == 403
    # (and as it was again, for the tests that follow)
    c = a.app.test_client()
    assert c.get("/api/shell/hello", headers={"Host": own_host, a._API_TOKEN_HEADER: own_token}).status_code == 200
    assert c.post("/api/window/show", headers={"Host": own_host}).status_code == 200


def test_hello_answers_an_ordinary_start_too():
    a = app()
    r = a.app.test_client().get("/api/shell/hello",
                                headers={"Host": f"127.0.0.1:{config.APP_PORT}", a._API_TOKEN_HEADER: a._API_TOKEN})
    assert r.status_code == 200 and r.get_json()["behind_shell"] is False


# ═════════════════════════════════════════════════════════════════════
# Leaving
# ═════════════════════════════════════════════════════════════════════
def test_the_backend_leaves_when_the_shell_is_gone():
    a = app()
    seen = []
    real_restore = a._restore_system_before_exit

    def restore(reason):
        seen.append(("restore", reason))
        real_restore(reason)                        # (for real, on the fake machine)

    a._restore_system_before_exit = restore
    try:
        with behind_the_shell(pid=777):
            outcome = a._leave_with_the_shell(wait=lambda pid, started: seen.append(("wait", pid, started)) or "ended",
                                              leave=lambda code: seen.append(("leave", code)))
    finally:
        a._restore_system_before_exit = real_restore
    assert outcome == "ended"
    assert seen == [("wait", 777, SHELL_STARTED), ("restore", "shell gone"), ("leave", 0)], \
        "the system is put back before the process goes, as on every quit"


def test_a_shell_that_cannot_be_watched_is_not_taken_for_one_that_is_gone():
    a = app()
    seen = []
    real_restore = a._restore_system_before_exit
    a._restore_system_before_exit = lambda reason: seen.append(("restore", reason))
    try:
        with behind_the_shell():
            outcome = a._leave_with_the_shell(wait=lambda pid, started: "cannot be watched (Windows error 5)",
                                              leave=lambda code: seen.append(("leave", code)))
    finally:
        a._restore_system_before_exit = real_restore
    assert outcome == "cannot be watched (Windows error 5)" and seen == []


def test_waiting_for_a_process_really_waits_for_it():
    a = app()
    if sys.platform != "win32":
        assert a._wait_for_process_to_end(1).startswith("cannot be watched")
        return
    child = m.REAL_POPEN([sys.executable, "-c", "import time; time.sleep(0.8)"])
    started = time.time()
    assert a._wait_for_process_to_end(child.pid) == "ended"
    waited = time.time() - started
    assert child.wait(5) == 0 and 0.3 < waited < 30, waited
    # A process that has already ended, and an id no process has.
    started = time.time()
    assert a._wait_for_process_to_end(child.pid) == "ended"
    assert a._wait_for_process_to_end(0x7FFFFFF0) == "ended"
    assert time.time() - started < 5


def _started(process):
    """When a process this test started was started, as Windows counts it."""
    times = [ctypes.c_ulonglong() for _ in range(4)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    assert kernel32.GetProcessTimes(int(process._handle), *[ctypes.byref(t) for t in times])
    return times[0].value


def test_a_process_that_only_has_the_shells_number_is_not_the_shell():
    """A process id is given out again once its process has ended.  If the
    shell is gone before the backend looks, the number may be another
    program's by then, and the backend would wait for that one to end.  The
    shell hands its start time over with its id; the two together are its."""
    a = app()
    if sys.platform != "win32":
        return
    child = m.REAL_POPEN([sys.executable, "-c", "import time; time.sleep(1.5)"])
    try:
        started = _started(child)
        assert started > 130000000000000000
        # The right number and another start time: somebody else.  Said at
        # once, while that process is still running.
        began = time.time()
        assert a._wait_for_process_to_end(child.pid, started + 10_000_000) == "ended"
        assert a._wait_for_process_to_end(child.pid, 1) == "ended"
        assert time.time() - began < 1.0 and child.poll() is None
        # The right number and the right start time: waited for.
        assert a._wait_for_process_to_end(child.pid, started) == "ended"
        assert child.wait(5) == 0 and time.time() - began > 0.8
    finally:
        child.kill()


# ═════════════════════════════════════════════════════════════════════
# The socket the shell hands over
# ═════════════════════════════════════════════════════════════════════
def test_the_handed_over_socket_has_to_be_what_the_hand_over_says():
    a = app()
    made, port = a_listening_socket()
    try:
        # As it arrives from the shell: inheritable, which is how the backend
        # got it.  The backend must not hand it on to the programs it starts.
        made.set_inheritable(True)
        assert made.get_inheritable() is True
        adopted = a._adopt_shell_listener(made.fileno(), port)
        assert adopted.fileno() == made.fileno() and adopted.getsockname() == ("127.0.0.1", port)
        assert adopted.get_inheritable() is False and made.get_inheritable() is False, \
            "the programs the backend starts would get the shell's socket"
        adopted.detach()
        refused = []
        # Another port than it says; a socket that is not listening; one on
        # every address of the PC; and a number that is no socket at all.
        idle, idle_port = a_listening_socket(listening=False)
        # (bound, not listening: listening on every address would have Windows ask the person at the PC about it)
        wide, wide_port = a_listening_socket(address="0.0.0.0", listening=False)
        for handle, said_port in ((made.fileno(), port + 1), (idle.fileno(), idle_port), (wide.fileno(), wide_port),
                                  (0x7FFFFFF0, port)):
            try:
                a._adopt_shell_listener(handle, said_port)
            except a.ShellHandoffError as e:
                refused.append(str(e))
        assert len(refused) == 4, refused
        assert all("the socket the shell handed over" in why for why in refused), refused
        # A refusal leaves the socket as it was: it is not this function's to close.
        assert made.getsockname() == ("127.0.0.1", port) and idle.getsockname()[1] == idle_port
        idle.close()
        wide.close()
    finally:
        made.close()


def test_behind_the_shell_the_backend_serves_on_the_shells_socket_and_no_other():
    """The backend never opens a port of its own behind the shell: a port
    that is let go of for it to take can be taken by somebody else in
    between, who is then sent the secret with the next call."""
    a = app()
    src = inspect.getsource(a.start_flask)
    behind, alone = src.index("if _shell_listener is not None:"), src.index("app.run(host=")
    assert behind < src.index("fd=_shell_listener.fileno()") < src.index("            return\n") < alone
    assert "_shell_listener = _adopt_shell_listener(handoff[\"listen\"], handoff[\"port\"])" in \
        inspect.getsource(a._serve_for_the_shell)
    # For real, in this process: the app's server on a socket made here.
    with behind_the_shell() as port:
        threading.Thread(target=a.start_flask, daemon=True, name="served-on-the-shells-socket").start()

        def ask(token):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/shell/hello",
                                             headers={a._API_TOKEN_HEADER: token})
            try:
                with urllib.request.urlopen(request, timeout=20) as answer:
                    return answer.status, json.loads(answer.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        status, said = ask(TOKEN)
        assert status == 200 and said["behind_shell"] is True and said["version"] == config.APP_VERSION
        assert ask("x" * len(TOKEN)) == (403, {"ok": False, "err": "missing or wrong session token"})


def test_main_leaves_to_the_shell_what_is_the_shells():
    src = inspect.getsource(app().main)
    # Telling a second copy from the first, and the administrator prompt.
    assert "    if shell is None and not _acquire_single_instance_lock():\n" in src
    assert '    if shell is None and sys.platform == "win32" and not is_admin():\n' in src
    assert src.index("_take_shell_handoff()") < src.index("_acquire_single_instance_lock()")
    # A hand-over that cannot be used ends the start, with the exit code of a wrong command line.
    assert "except ShellHandoffError as e:" in src and "sys.exit(2)" in src
    # The server is up before the backend settles down to wait for the
    # shell, and the window and the tray are never reached.
    served = src.index("flask_thread.start()")
    stays = src.index("        _leave_with_the_shell()\n")
    window = src.index("import webview")
    assert served < stays < window
    between = src[stays:window]
    assert "threading.Event().wait()" in between and "\n        return\n" in between
    assert src.index("        _crosshair_allow_start()\n") < stays, "no window will ever say the crosshair may start"
    # Everything else a start does is done behind the shell as well: the
    # recovery steps come before the point where the two ways part.
    for step in ("recover_interrupted_session()", "check_and_restore_on_boot()", "recover_orphans()",
                 "updater.check_update_background", "boot_prep.prep_for_gaming_async"):
        assert src.index(step) < stays, step


def test_a_quit_behind_the_shell_does_not_wait_half_a_second_for_nobody():
    """The quit used to end the process half a second after answering, so
    that the answer reached the window.  The shell does not wait for the
    answer but for the process, so behind it a moment is enough."""
    a = app()
    alone, behind = a._QUIT_ANSWER_HEAD_START_S
    assert alone == 0.5 and 0 < behind <= 0.1
    src = inspect.getsource(a.api_window_quit)
    assert "time.sleep(_QUIT_ANSWER_HEAD_START_S[_behind_shell is not None])" in src
    # Still after the system has been put back, and still on a thread of its
    # own, so that the answer goes out first.
    assert src.index('_restore_system_before_exit("user quit")') < src.index("def _suicide():")
    assert "threading.Thread(target=_suicide, daemon=True).start()" in src
