"""app.py as the backend of the shell (shell_rs/).

The shell is Vispora.exe in the making: it owns the window, the tray and
quitting, and starts this app as its backend with `--backend` and a
hand-over in one environment variable.  No release starts the app this way
yet.  What is checked here is the app's side of that arrangement:

  * the hand-over: read, checked, and taken out of the environment;
  * behind the shell, only the shell's address and secret are answered;
  * the backend leaves when the shell is gone, after putting the system
    back, and does not mistake "cannot be watched" for "gone";
  * main() leaves to the shell what is the shell's.

The other side, and the two together, are in tests/test_shell_rs.py.
"""
import sandbox as m

import contextlib
import inspect
import json
import sys
import time
import unittest

import config

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

PORT = 49213
TOKEN = "s3cret-" + "t" * 36 + "_AZaz09"
SHELL_PID = 4321


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def environment(**changes):
    given = {"port": PORT, "token": TOKEN, "shell_pid": SHELL_PID}
    given.update(changes)
    return {app()._SHELL_HANDOFF_VAR: json.dumps(given)}


@contextlib.contextmanager
def behind_the_shell(pid=SHELL_PID):
    """The app module as a shell's backend, and as it was again afterwards."""
    a = app()
    before = (a._behind_shell, a._API_TOKEN, a._LOCAL_HOSTS, a._LOCAL_ORIGINS, a._TOKEN_EXEMPT_PATHS, config.APP_PORT)
    a._serve_for_the_shell({"port": PORT, "token": TOKEN, "shell_pid": pid})
    try:
        yield a
    finally:
        (a._behind_shell, a._API_TOKEN, a._LOCAL_HOSTS, a._LOCAL_ORIGINS, a._TOKEN_EXEMPT_PATHS, config.APP_PORT) = before


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
    assert got == {"port": PORT, "token": TOKEN, "shell_pid": SHELL_PID}
    assert env == {"PATH": "x"}, "the secret is not inherited by the programs the app starts"


def test_a_hand_over_that_cannot_be_used_is_refused():
    a = app()
    argv = ["Vispora.exe", "--backend"]
    bad = [{}, {a._SHELL_HANDOFF_VAR: ""}, {a._SHELL_HANDOFF_VAR: "not json"}, {a._SHELL_HANDOFF_VAR: "[]"},
           {a._SHELL_HANDOFF_VAR: json.dumps({"port": PORT, "token": TOKEN})},
           {a._SHELL_HANDOFF_VAR: json.dumps({"port": PORT, "shell_pid": 1})}]
    bad += [environment(port=p) for p in (80, 1023, 65536, 0, -1, str(PORT), True, PORT + 0.5, None)]
    bad += [environment(token=t) for t in ("short", "x" * 31, "x" * 40 + "!", "x" * 40 + " ", "é" * 40, 12345, None,
                                           ["x" * 40])]
    bad += [environment(shell_pid=p) for p in (0, -5, "4321", True, 1.5, None)]
    for env in bad:
        try:
            a._take_shell_handoff(argv, env)
        except a.ShellHandoffError:
            assert a._SHELL_HANDOFF_VAR not in env, "taken out of the environment also when it is refused"
        else:
            raise AssertionError(f"taken as a hand-over: {env}")
    # ... and the edges of what is taken.
    for good in (environment(port=1024), environment(port=65535), environment(token="A" * 32),
                 environment(shell_pid=1)):
        assert a._take_shell_handoff(argv, good) is not None


# ═════════════════════════════════════════════════════════════════════
# Whom the backend answers
# ═════════════════════════════════════════════════════════════════════
def test_behind_the_shell_only_the_shells_address_and_secret_are_answered():
    a = app()
    own_token, own_host = a._API_TOKEN, f"127.0.0.1:{config.APP_PORT}"
    with behind_the_shell():
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
            outcome = a._leave_with_the_shell(wait=lambda pid: seen.append(("wait", pid)) or "ended",
                                              leave=lambda code: seen.append(("leave", code)))
    finally:
        a._restore_system_before_exit = real_restore
    assert outcome == "ended"
    assert seen == [("wait", 777), ("restore", "shell gone"), ("leave", 0)], \
        "the system is put back before the process goes, as on every quit"


def test_a_shell_that_cannot_be_watched_is_not_taken_for_one_that_is_gone():
    a = app()
    seen = []
    real_restore = a._restore_system_before_exit
    a._restore_system_before_exit = lambda reason: seen.append(("restore", reason))
    try:
        with behind_the_shell():
            outcome = a._leave_with_the_shell(wait=lambda pid: "cannot be watched (Windows error 5)",
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
