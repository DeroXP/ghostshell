"""The Rust shell's core (shell_rs/core), held against the app it is to
start (app.py).

The shell has its own tests, against a stand-in backend that exists only in
them (shell_rs/core/tests/, run by `cargo test`).  What is checked here is
what only this side can check:

  * that the shell and the app agree on the hand-over, on the calls the
    shell makes and answers, and on the words of a refusal;
  * that the page the shell serves is made of the app's own files;
  * and, on a PC where the shell's trial tool has been built, the two
    together: the tool starts a real process, the app on the fake machine of
    tests/sandbox.py (tests/shell_backend_stand_in.py), and the page, the
    calls, the quit and the shell going away are tried for real.

The real backend is never started: it changes the PC it runs on.
"""
import sandbox as m

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unittest

import config

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

SHELL = os.path.join(m.REPO, "shell_rs")
CORE = os.path.join(SHELL, "core")
STAND_IN = os.path.join(m.HERE, "shell_backend_stand_in.py")


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def rust(*path):
    with open(os.path.join(CORE, *path), encoding="utf-8") as f:
        return f.read()


def repo(*path):
    with open(os.path.join(m.REPO, *path), encoding="utf-8") as f:
        return f.read()


def built_tool(name):
    """The shell's trial tool, where cargo put it: in the folder
    CARGO_TARGET_DIR names (the build output is kept out of the repository,
    see README.md), or in the crate's own target folder.  None on a PC where
    it has not been built: the tests that need it then have nothing to run,
    and cargo test covers the same ground with its own stand-in."""
    folders = [os.path.join(CORE, "target")]
    if os.environ.get("CARGO_TARGET_DIR"):
        folders.insert(0, os.environ["CARGO_TARGET_DIR"])
    for folder in folders:
        path = os.path.join(folder, "release", name)
        if os.path.isfile(path):
            return path
    return None


TRY = built_tool("vispora-shell-try.exe")


# ═════════════════════════════════════════════════════════════════════
# What the shell and the app have to agree on
# ═════════════════════════════════════════════════════════════════════
def test_the_core_uses_no_outside_crates():
    manifest = rust("Cargo.toml")
    wanted = manifest[manifest.index("[dependencies]"):manifest.index("[profile.release]")]
    lines = [line for line in wanted.splitlines()[1:] if line.strip() and not line.startswith("#")]
    assert lines == ['vispora-updater-core = { path = "../../updater_rs/core" }'], \
        "the one dependency is a crate of this repository that has none itself"
    assert "[build-dependencies]" not in manifest and "[dev-dependencies]" not in manifest
    lock = rust("Cargo.lock")
    names = re.findall(r'^name = "([^"]+)"$', lock, re.M)
    assert sorted(names) == ["vispora-shell-core", "vispora-updater-core"]
    assert "source = " not in lock and "checksum = " not in lock, "a build of the shell's core downloads nothing"


def test_the_version_is_written_in_config_py_only():
    build = rust("build.rs")
    assert '("APP_VERSION", "VISPORA_APP_VERSION")' in build and "config.py" in build
    # The line the build looks for is there, in the form it looks for.
    found = re.findall(r'^APP_VERSION = "([^"]+)"', repo("config.py"), re.M)
    assert found == [config.APP_VERSION]
    assert re.findall(r'^APP_NAME = "([^"]+)"', repo("config.py"), re.M) == [config.APP_NAME]
    assert 'pub const APP_VERSION: &str = env!("VISPORA_APP_VERSION");' in rust("src", "lib.rs")
    assert "version: crate::APP_VERSION.to_string()," in rust("src", "shell.rs")


def test_the_page_is_made_of_the_apps_own_files():
    page = rust("src", "page.rs")
    for name in ("templates/index.html", "static/js/app.js", "static/css/style.css"):
        assert f'"../../../{name}"' in page, name
        assert os.path.isfile(os.path.join(m.REPO, *name.split("/")))
    # The template uses two names and nothing else of Jinja, so that filling
    # those two in is all there is to do.
    template = repo("templates", "index.html")
    used = set(re.findall(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", template, re.S))
    assert used == {"{{ version }}", "{{ appdata_dir }}"}, used
    assert 'render_template("index.html", version=config.APP_VERSION,' in repo("app.py")
    assert "appdata_dir=config.APPDATA_DIR)" in repo("app.py")
    # ... and the page asks for no other file of the app's.
    assert set(re.findall(r'(?:src|href)="(/[^"]+)"', template)) == {"/static/js/app.js", "/static/css/style.css"}
    assert sorted(os.listdir(os.path.join(m.REPO, "static", "js"))) == ["app.js"]
    assert sorted(os.listdir(os.path.join(m.REPO, "static", "css"))) == ["style.css"]


def test_the_shell_and_the_app_agree_on_the_hand_over():
    a = app()
    platform, shell, token = rust("src", "platform.rs"), rust("src", "shell.rs"), rust("src", "token.rs")
    assert f'pub const HANDOFF_VARIABLE: &str = "{a._SHELL_HANDOFF_VAR}";' in platform
    assert f'pub const HEADER: &str = "{a._API_TOKEN_HEADER}";' in token
    # What the shell hands over is what the app reads, and all of it.
    start = shell[shell.index("let handoff = Json::object(["):shell.index(".to_string();", shell.index("let handoff = Json::object(["))]
    handed = re.findall(r'\("(\w+)", ', start)
    taken = a._take_shell_handoff(["Vispora.exe", "--backend"], {a._SHELL_HANDOFF_VAR: json.dumps(
        {"port": 50000, "listen": 812, "token": "x" * 43, "shell_pid": 9, "shell_started": 134088000000000000})})
    assert sorted(handed) == sorted(taken) == ["listen", "port", "shell_pid", "shell_started", "token"]
    # The secret the shell makes is one the app takes: 43 of these letters.
    assert 'b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"' in token
    # The win.rs door puts it in the environment and nowhere else.
    win = rust("src", "win.rs")
    assert ".env(HANDOFF_VARIABLE, handoff)" in win and ".args(&command.args)" in win
    assert "handoff" not in win[win.index(".args(&command.args)"):win.index(".env(HANDOFF_VARIABLE, handoff)")]


def test_the_calls_the_shell_makes_and_answers_are_the_apps():
    a = app()
    routes = {rule.rule: rule.methods for rule in a.app.url_map.iter_rules()}
    shell, router = rust("src", "shell.rs"), rust("src", "router.rs")
    # What the shell asks its backend.
    assert 'Request::new("GET", "/api/shell/hello")' in shell and "GET" in routes["/api/shell/hello"]
    assert 'Request::new("POST", "/api/window/quit")' in shell and "POST" in routes["/api/window/quit"]
    # What the shell answers in the backend's place: the window's calls,
    # every one of which the app has, with POST.
    listed = router[router.index("const WINDOW_CALLS"):router.index("];", router.index("const WINDOW_CALLS"))]
    window_calls = re.findall(r'"(/api/window/[a-z-]+)"', listed)
    in_app = sorted(rule for rule in routes if rule.startswith("/api/window/"))
    assert sorted(window_calls) == in_app, "a window call the app has and the shell does not answer, or the other way round"
    for call in window_calls:
        assert "POST" in routes[call] and "GET" not in routes[call], call
        assert f'"{call}" =>' in router, call
    # A refusal is worded as the app words it, so that the page reads both alike.
    source = repo("app.py")
    for words in ("cross-origin request refused", "missing or wrong session token", "Endpoint not found"):
        assert f'"{words}"' in source and f'"{words}"' in router, words
    assert 'f"Method {request.method} not allowed for {request.path}"' in source
    assert 'format!("Method {method} not allowed for {path}")' in router
    # The same three questions, in the same order.
    guard = router[router.index("fn stranger"):router.index("/// Hand a call to the backend")]
    assert guard.index('"Origin"') < guard.index('"Sec-Fetch-Site"') < guard.index("token::HEADER")
    assert '"same-origin"' in guard and '"none"' in guard


def test_the_port_is_never_free_for_somebody_else_to_listen_on():
    """Every call carries the secret, and a call goes to whoever listens on
    the port.  The first version of the shell asked Windows for a free port,
    let go of it for the backend to take, and asked "are you up", with the
    secret, of whoever was there meanwhile.  Now the shell makes the
    listening socket itself, not shareable, keeps it for as long as it
    lives, and the backend inherits it."""
    platform, win, shell = rust("src", "platform.rs"), rust("src", "win.rs"), rust("src", "shell.rs")
    for name in os.listdir(os.path.join(CORE, "src")):
        if name.endswith(".rs"):
            assert "free_port" not in rust("src", name), f"{name}: a port that is let go of is anybody's"
    assert "fn listen(&self) -> Result<Box<dyn Listening>, String>;" in platform
    # Not shareable, and that is said before the address is taken.
    made = win[win.index("fn listen(&self) -> Result<Box<dyn Listening>, String> {"):win.index("fn start(&self")]
    assert made.index("SO_EXCLUSIVEADDRUSE") < made.index("bind_local(made.socket, 0)") < made.index("listen(made.socket,")
    assert "HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT" in made, "the backend finds it among the handles it inherits"
    assert "host: [127, 0, 0, 1]" in win
    # Made with the shell, before anything is started or asked, and kept in it.
    new = shell[shell.index("pub fn new(settings: Settings"):shell.index("fn state(&self)")]
    assert "let listening = platform.listen()?;" in new and "listening," in new
    assert "listening: Box<dyn Listening>," in shell
    # Handed over, and every call the shell makes goes to that socket's port.
    assert '("listen", self.listening.handle().into()),' in shell
    assert "forward::send(self.listening.port(), &ask, &limits)" in shell, "the first question"
    assert "Phase::Ready => return Ok(self.listening.port())," in shell, "what is passed on"
    assert shell.count("forward::send(") == 2 and "(self.listening.port(), st.was_ready)" in shell, "and the quit"
    # The app's side: it serves on that socket and opens no port of its own.
    a = app()
    assert "fd=_shell_listener.fileno()" in repo("app.py")
    assert "SO_ACCEPTCONN" in repo("app.py") and '("127.0.0.1", port)' in repo("app.py")
    assert a._shell_listener is None, "an ordinary start has no such socket"


def test_the_backend_is_only_ever_asked_on_this_pc():
    forward = rust("src", "forward.rs")
    assert forward.count("TcpStream::connect") == 1
    assert "let address = SocketAddr::from(([127, 0, 0, 1], port));" in forward
    assert "TcpStream::connect_timeout(&address, limits.connect)" in forward
    for name in os.listdir(os.path.join(CORE, "src")):
        if name.endswith(".rs") and name != "forward.rs":
            shipped = rust("src", name).split("#[cfg(test)]")[0]        # (not the file's own tests)
            assert "TcpStream" not in shipped, f"{name} opens a connection of its own"
    # What the browser says about where a call came from is not passed on.
    passed = forward[forward.index("const PASSED_ON"):forward.index("];", forward.index("const PASSED_ON"))]
    assert re.findall(r'"([a-z-]+)"', passed) == ["content-type", "accept", "accept-language", "x-vispora-token"]


def test_the_stand_in_backend_is_the_app_on_the_fake_machine():
    stand_in = repo("tests", "shell_backend_stand_in.py")
    code = stand_in[stand_in.index('"""', 3):]
    assert code.index("import sandbox as m") < code.index("import app as A"), "the fake machine first"
    for used in ("A._take_shell_handoff()", "A._serve_for_the_shell(handoff)", "A.start_flask", "A._leave_with_the_shell()"):
        assert used in code, used
    assert "LIFETIME_S" in code and "threading.Timer(LIFETIME_S" in code, "it never outlives a trial that went wrong"


# ═════════════════════════════════════════════════════════════════════
# The two together, on a PC where the trial tool has been built
# ═════════════════════════════════════════════════════════════════════
class Trial:
    """The shell's trial tool with the stand-in backend, and a conversation
    with it: one JSON line asked, one answered."""

    def __init__(self, *backend_args, quit_within=None, notes=None):
        command = [TRY, "--backend", sys.executable, "--arg", "-u", "--arg", STAND_IN, "--arg", "--backend"]
        for arg in backend_args:
            command += ["--arg", arg]
        command += ["--appdata", config.APPDATA_DIR, "--ready-within", "90"]
        if quit_within is not None:
            command += ["--quit-within", str(quit_within)]
        env = dict(os.environ)
        if notes:
            env["VISPORA_STAND_IN_NOTES"] = notes
        self.process = m.REAL_POPEN(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, cwd=m.REPO, text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.first = self.answer(30)

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def answer(self, within=120):
        try:
            line = self.lines.get(timeout=within)
        except queue.Empty:
            raise AssertionError(f"the shell's trial tool said nothing for {within} s")
        if line is None:
            raise AssertionError(f"the shell's trial tool ended: {self.process.stderr.read()}")
        return json.loads(line)

    def ask(self, within=120, **what):
        self.process.stdin.write(json.dumps(what) + "\n")
        self.process.stdin.flush()
        return self.answer(within)

    def call(self, method, target, token=True, headers=None, body=None):
        return self.ask(do="call", method=method, target=target, token=token, headers=headers or {}, body=body)

    def tell(self, **what):
        """Say something that is not answered (the tool ends on it)."""
        self.process.stdin.write(json.dumps(what) + "\n")
        self.process.stdin.flush()

    def close(self):
        """Whatever is left: the tool, and through its quit the backend."""
        if self.process.poll() is None:
            try:
                self.process.stdin.close()              # the end of its input: it quits
                self.process.wait(60)
            except Exception:
                self.process.kill()


def can_be_listened_on(port):
    """Can a socket of this (other) program listen on 127.0.0.1:`port`?
    Tried the plain way, and the way that gets a second listener onto a port
    whose first listener asked for a shareable address, as a web server's
    own listener does on Windows."""
    import socket
    got = []
    for sharing in (False, True):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sharing:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            got.append(True)
        except OSError:
            got.append(False)
        finally:
            s.close()
    return tuple(got)


def ended(pid, within=30):
    """Has the process `pid` ended, at the latest `within` seconds from now?"""
    done = []
    waiter = threading.Thread(target=lambda: done.append(A._wait_for_process_to_end(pid)), daemon=True)
    waiter.start()
    waiter.join(within)
    return done == ["ended"]


def test_the_shell_and_the_backend_together():
    a = app()
    if not TRY:
        return
    notes = os.path.join(m.ROOT, "stand-in-notes-1.txt")
    t = Trial(notes=notes)
    try:
        assert t.first["started"] is True
        assert t.first["version"] == config.APP_VERSION, \
            "the trial tool on disk was built for another version of the app: build it again"
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        state = t.ask(do="state")
        backend = state["backend_pid"]
        # The port is the shell's own socket, which the backend inherited:
        # nobody else can listen there, whichever way they ask.
        port = state["port"]
        assert port >= 1024 and can_be_listened_on(port) == (False, False), "another listener got onto the shell's port"
        assert state["starts"] == 1 and isinstance(backend, int) and backend not in (os.getpid(), t.first["pid"])
        assert 0 < state["ready_ms"] < 90_000
        ours = a.app.test_client()
        host = {"Host": f"127.0.0.1:{config.APP_PORT}"}

        # The page is the page the app serves, to the letter, and so are its files.
        page = t.call("GET", "/", token=False)
        served = ours.get("/", headers=host)
        assert page["status"] == served.status_code == 200
        assert page["body"] == served.data.decode("utf-8"), "the shell's page is not the app's"
        assert page["headers"]["content-type"] == served.headers["Content-Type"] == "text/html; charset=utf-8"
        for path in ("/static/js/app.js", "/static/css/style.css"):
            mine, theirs = t.call("GET", path, token=False), ours.get(path, headers=host)
            assert mine["status"] == 200 and mine["body"] == theirs.data.decode("utf-8"), path
            assert mine["bytes"] == len(theirs.data), path

        # A call goes through to the backend and comes back.
        token = t.ask(do="token")["token"]
        assert len(token) == 43
        hello = t.call("GET", "/api/shell/hello")
        assert hello["status"] == 200
        assert json.loads(hello["body"]) == {"ok": True, "app": config.APP_NAME, "version": config.APP_VERSION,
                                             "pid": backend, "behind_shell": True}
        dashboard = t.call("GET", "/api/dashboard")
        assert dashboard["status"] == 200 and "module_status" in json.loads(dashboard["body"]), dashboard["body"][:300]
        saved = t.call("POST", "/api/updater/settings", headers={"Content-Type": "application/json"},
                       body=json.dumps({"auto_check": False}))
        assert saved["status"] == 200 and json.loads(saved["body"]).get("ok") is True, saved["body"][:300]
        assert json.loads(t.call("GET", "/api/updater/settings")["body"])["settings"]["auto_check"] is False

        # A stranger is refused by the shell in the words the app would use.
        for refused, theirs in (
            (t.call("GET", "/api/dashboard", token=False), ours.get("/api/dashboard", headers=host)),
            (t.call("POST", "/api/network/undo", headers={"Origin": "https://evil.example"}),
             ours.post("/api/network/undo", headers={**host, a._API_TOKEN_HEADER: a._API_TOKEN, "Origin": "https://evil.example"})),
        ):
            assert refused["status"] == theirs.status_code == 403
            assert json.loads(refused["body"]) == theirs.get_json()
        # A path nobody has is the backend's to say.
        missing = t.call("GET", "/api/no/such/thing")
        theirs = ours.get("/api/no/such/thing", headers={**host, a._API_TOKEN_HEADER: a._API_TOKEN})
        assert missing["status"] == theirs.status_code == 404 and json.loads(missing["body"]) == theirs.get_json()
        # The window is the shell's, and the trial tool has none.
        assert json.loads(t.call("POST", "/api/window/show")["body"]) == {"ok": False}

        # Quit: the backend puts the system back, ends, and only then the shell.
        t.tell(do="quit")
        last = t.answer(60)
        assert last["exit"] == 0 and last["phase"] == "gone" and 0 < last["quit_ms"] < 30_000, last
        said = "\n".join(last["history"])
        assert "the backend took the quit (status 200)" in said and "the backend ended (exit code 0)" in said, said
        assert token not in said, "the secret is in no line the shell says"
        assert t.process.wait(30) == 0
        assert ended(backend), "the backend outlived the shell's quit"
        assert can_be_listened_on(port) == (True, True), "the port is anybody's again once the shell has ended"
        with open(notes, encoding="utf-8") as f:
            assert f.read() == "put back: user quit\n"
    finally:
        t.close()


def test_a_backend_that_will_not_go_is_ended_at_the_limit():
    app()
    if not TRY:
        return
    t = Trial("--ignore-quit", quit_within=1.5)
    try:
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        backend = t.ask(do="state")["backend_pid"]
        began = time.time()
        t.tell(do="quit")
        last = t.answer(60)
        took = time.time() - began
        assert last["phase"] == "gone" and 1.4 < took < 20, took
        assert "had not ended 1 s after it was told to quit, and was ended" in "\n".join(last["history"]), last["history"]
        assert t.process.wait(30) == 0
        assert ended(backend), "a backend that would not go is still there"
    finally:
        t.close()


def test_the_backend_goes_when_the_shell_is_gone():
    app()
    if not TRY:
        return
    notes = os.path.join(m.ROOT, "stand-in-notes-3.txt")
    t = Trial(notes=notes)
    try:
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        backend = t.ask(do="state")["backend_pid"]
        t.tell(do="crash")                              # the shell ends without a word, as a crash would
        assert t.process.wait(30) == 3
        assert ended(backend), "a backend was left behind by a shell that died"
        with open(notes, encoding="utf-8") as f:
            assert f.read() == "put back: shell gone\n", "it put the system back before it went"
    finally:
        t.close()
