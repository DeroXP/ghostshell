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
    assert sorted(handed) == sorted(taken) == ["listen", "port", "shell_exe", "shell_pid", "shell_started", "token"]
    # Where the shell is: said by the shell that knows it, as nothing by one that does not.
    assert '("shell_exe", self.settings.shell_exe.as_deref().map_or(Json::Null, Json::from)),' in shell
    assert "settings.shell_exe = std::env::current_exe().ok()" in app_crate("src", "main.rs")
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
    assert "(self.listening.port(), st.was_ready)" in shell, "and the quit"
    # ... and the shell's own calls (the tray's), which go there too.
    own = shell[shell.index("pub fn ask_backend("):shell.index("/// End the backend now, without asking it.")]
    assert "if self.phase() != Phase::Ready {\n            return None;\n        }" in own
    assert "forward::send(self.listening.port(), &ask, &limits)" in own and ".with_header(token::HEADER, self.token.as_str())" in own
    assert shell.count("forward::send(") == 3, "the first question, the quit, and the shell's own calls: nothing else is sent"
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


def app_crate(*path):
    with open(os.path.join(SHELL, "app", *path), encoding="utf-8") as f:
        return f.read()


def registry_source(crate, *path):
    """A file of an outside crate, in the version the lock file pins, where
    cargo keeps what it downloaded.  None on a PC that never built the window."""
    version = re.search(r'name = "%s"\nversion = "([^"]+)"' % re.escape(crate), app_crate("Cargo.lock")).group(1)
    # (the person's own folder: the sandbox points USERPROFILE somewhere else)
    home = os.environ.get("HOMEDRIVE", "") + os.environ.get("HOMEPATH", "")
    kept = os.path.join(os.environ.get("CARGO_HOME") or os.path.join(home, ".cargo"), "registry", "src")
    for index in (os.listdir(kept) if os.path.isdir(kept) else []):
        found = os.path.join(kept, index, f"{crate}-{version}", *path)
        if os.path.isfile(found):
            with open(found, encoding="utf-8") as f:
                return f.read()
    return None


# ═════════════════════════════════════════════════════════════════════
# The window (shell_rs/app)
# ═════════════════════════════════════════════════════════════════════
# What Tauri's tray feature adds to the lock file, and nothing else may:
# the tray crate itself, and three that only a Linux build would compile.
THE_TRAYS_CRATES = {("tray-icon", "0.25.1"), ("libappindicator", "0.9.0"), ("libappindicator-sys", "0.9.0"),
                    ("libloading", "0.7.4")}


def test_the_window_brings_no_crate_but_tauris_own_and_its_tray():
    manifest = app_crate("Cargo.toml")
    pinned = dict(re.findall(r'^(tauri(?:-build)?) = \{ version = "=(\d+\.\d+\.\d+)"', manifest, re.M))
    theirs = dict(re.findall(r'^(tauri(?:-build)?) = \{ version = "=(\d+\.\d+\.\d+)"',
                             repo("updater_rs", "app", "Cargo.toml"), re.M))
    assert pinned == theirs and set(pinned) == {"tauri", "tauri-build"}, "the versions the updater's window pinned"
    assert 'default-features = false, features = ["wry", "custom-protocol", "tray-icon"]' in manifest, \
        "the window and the tray icon, and nothing else"
    assert "plugin" not in manifest, "a feature of Tauri's own, not a plugin crate"
    wanted = manifest[manifest.index("[dependencies]"):manifest.index("[profile.release]")]
    # (the core twice: for the exe, and for the script that builds it, which
    # reads the pack the exe is to carry before the exe is made)
    assert sorted(re.findall(r"^([\w-]+) = ", wanted, re.M)) == ["tauri", "tauri-build", "vispora-shell-core", "vispora-shell-core"]

    def outside(lock):
        packages = re.split(r"\n\[\[package\]\]\n", lock)[1:]
        found = set()
        for package in packages:
            name, version = re.match(r'name = "([^"]+)"\nversion = "([^"]+)"', package).groups()
            if "source = " in package:
                assert 'source = "registry+https://github.com/rust-lang/crates.io-index"' in package, name
                assert re.search(r'^checksum = "[0-9a-f]{64}"$', package, re.M), name
                found.add((name, version))
            else:
                assert name.startswith("vispora-"), name
        return found

    ours, updaters = outside(app_crate("Cargo.lock")), outside(repo("updater_rs", "app", "Cargo.lock"))
    assert len(updaters) > 100 and updaters <= ours, sorted(updaters - ours)
    assert ours - updaters == THE_TRAYS_CRATES, sorted(ours ^ updaters ^ THE_TRAYS_CRATES)
    assert f"{len(ours)} outside crates" in repo("shell_rs", "README.md"), len(ours)
    assert "cargo build --release --locked --manifest-path shell_rs/app/Cargo.toml" in repo("shell_rs", "README.md")


def test_the_window_is_the_python_apps_window():
    python, main = repo("app.py"), app_crate("src", "main.rs")
    made = python[python.index("_webview_window = webview.create_window("):python.index("if from_autostart:\n            global _window_hidden")]
    width, height = re.search(r"width=(\d+),\s*height=(\d+)", made).groups()
    least = re.search(r"min_size=\((\d+), (\d+)\)", made).groups()
    assert f".inner_size({width}.0, {height}.0)" in main and f".min_inner_size({least[0]}.0, {least[1]}.0)" in main
    colour = re.search(r'background_color="#([0-9a-f]{6})"', made).group(1)
    red, green, blue = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
    assert f".background_color(Color({red}, {green}, {blue}, 255))" in main
    assert "frameless=True" in made and ".decorations(false)" in main
    assert "hidden=from_autostart" in made and "_ if from_autostart => window.visible(false)," in main
    assert ".title(APP_NAME)" in main and "config.APP_NAME," in made
    # The page is opened with the secret at the end of its address, as it always was.
    assert 'f"http://127.0.0.1:{config.APP_PORT}/#t={_API_TOKEN}"' in python
    assert 'format!("{PAGE}{}", shell.fragment())' in main and 'format!("#t={}", self.token.as_str())' in rust("src", "shell.rs")
    # What the page asks goes to the core, and nowhere else.
    assert "let answer = shell.handle(&request);" in main
    # A window closed from outside does not end the process before the backend has put the system back.
    assert 'shell.quit_and_wait("the window was closed")' in main
    assert main.index("app.run_return(") < main.index('shell.quit_and_wait("the window was closed")')


def test_signing_out_does_not_skip_the_quit():
    """When Windows signs the person out, shuts down or replaces the app's
    files, the window's loop is told so and the process is ended inside that
    very call: nothing written after the loop is reached.  The loop's last
    word (Exit) still is, so the quit is there."""
    main = app_crate("src", "main.rs")
    loop = main[main.index("let code = app.run_return("):main.index('let quit = shell.quit_and_wait("the window was closed");')]
    assert ("if let RunEvent::Exit = event {\n"
            "            at_the_end.quit_and_wait(\"the window is gone\");\n"
            "        }") in loop
    # When the shell is gone, the window is taken away, and the loop ends
    # with its last window.  The loop is not told to end: the window would
    # still be there, WebView2 would keep the window's folder, and the
    # folder could not be removed (the trial's "Quit" from the tray sees
    # that it is).  And told to end a loop that is over, Tauri ends the
    # process on the spot.
    hook = main[main.index("*shell.exit_hook.lock()"):main.index("shell.start();")]
    assert "let _ = last_window.0.destroy();" in hook
    assert ".exit(" not in main.replace("std::process::exit(", ""), "nothing tells the loop to end"
    tauri = registry_source("tauri", "src", "app.rs")
    if tauri:
        told = tauri[tauri.index("pub fn exit(&self, exit_code: i32) {"):]
        told = told[:told.index("\n  }\n")]
        assert "self.runtime_handle.request_exit(exit_code)" in told and "std::process::exit(exit_code);" in told
    assert "app.run_return(|_, _| {})" not in main
    # What it rests on: the window library ends the process right after
    # that last word.  If a later version stops doing either, this says so.
    tao = registry_source("tao", "src", "platform_impl", "windows", "event_loop.rs")
    if tao:
        at = tao.index("win32wm::WM_ENDSESSION => {")
        handled = tao[at:tao.index("LRESULT(0)", at)]
        assert handled.index("event_loop_runner.loop_destroyed();") < handled.index("std::process::exit(0);")
    assert "what try_it.py sends" in app_crate("try_it.py") and "WM_ENDSESSION = 0x0016" in app_crate("try_it.py")


def test_the_page_finds_what_it_asked_pywebview_for():
    """static/js/app.js was written for pywebview's window and is not changed.
    Everything it asks of `window.pywebview.api` is there in the shell's
    window, and the parts of the page that moved pywebview's window move
    this one."""
    page, script = rust("src", "page.rs"), repo("static", "js", "app.js")
    shim = page[page.index('const SHIM: &str = r#"'):page.index('"#;', page.index('const SHIM: &str = r#"'))]
    asked = set(re.findall(r"\bapi\.([a-z_]+)\b", script))
    assert asked == {"get_api_token", "minimize", "close_to_tray"}, asked
    offered = set(re.findall(r"^        ([a-z_]+): function", shim, re.M))
    a = app()
    in_python = {name for name in vars(a.WindowJSAPI) if not name.startswith("_")}
    assert offered == in_python and asked <= offered, (offered, in_python)
    # Each of them is a call to the shell, with the secret, which answers it itself.
    router = rust("src", "router.rs")
    for path in ("/api/window/minimize", "/api/window/close", "/api/window/show"):
        assert f"ask('{path}')" in shim and f'"{path}" =>' in router, path
    assert "'X-Vispora-Token': token" in shim and a._API_TOKEN_HEADER == "X-Vispora-Token"
    # Dragging: only the marked parts themselves, as pywebview did it, not the buttons inside them.
    assert "pywebview-drag-region" in repo("templates", "index.html")
    assert ".pywebview-drag-region { -webkit-app-region: drag; }" in shim
    assert ".pywebview-drag-region *:not(.pywebview-drag-region) { -webkit-app-region: no-drag; }" in shim
    assert "-webkit-app-region" not in repo("static", "css", "style.css"), "the page's own style does not do it"
    # It is given to the window's own page by the window, and never served.
    assert ".initialization_script(page::shim(shell.token(), &shell.settings.origin))" in app_crate("src", "main.rs")
    assert "SHIM" not in router and "shim" not in router
    # The window runs it in every frame it ever shows: before it names the
    # secret, the script looks whether it is the app's own page, at the top.
    body = shim[shim.index("(function () {"):]
    first = body.splitlines()[1].strip()
    assert first == 'if (window.top !== window.self || location.origin !== "%ORIGIN%") { return; }', first
    assert body.index(first) < body.index("%TOKEN%")
    assert '.replace("%ORIGIN%", &origin)' in page


def test_the_windows_page_cannot_reach_tauris_commands():
    main = app_crate("src", "main.rs")
    for word in ("invoke_handler", "generate_handler", "tauri::command", ".plugin("):
        assert word not in main, word
    conf = json.loads(app_crate("tauri.conf.json"))
    assert conf["app"]["security"]["capabilities"] == [] and conf["app"]["windows"] == []
    assert not conf["app"].get("withGlobalTauri") and conf["bundle"]["active"] is False
    assert not os.path.isdir(os.path.join(SHELL, "app", "capabilities"))
    assert "NewWindowResponse::Deny" in main
    # The window stays on the app's own page, and "the app's own" is the
    # whole of where a page is from: not the host name alone, under which
    # another scheme or another port is another place.
    assert main.count(".on_navigation(") == 1
    assert ".on_navigation(move |url| page::is_the_apps_page(url.as_str(), &own_page))" in main
    assert "let (own_page, for_links) = (shell.settings.origin.clone(), shell.clone());" in main
    assert "host_str()" not in main and "url.scheme()" not in main
    page, shell = rust("src", "page.rs"), rust("src", "shell.rs")
    rule = page[page.index("pub fn is_the_apps_page("):page.index("const SHIM: &str")]
    assert "match address.strip_prefix(origin) {" in rule and "rest.is_empty() || rest.starts_with(['/', '?', '#'])," in rule
    assert 'origin: "http://vispora.localhost".to_string(),' in shell
    # A link is handed to Explorer, which opens it in the person's browser
    # without this program's rights; and Explorer is started without this
    # program's handles (the listening socket among them).
    win = rust("src", "win.rs")
    assert "win::open_link(url.as_str())" in main
    assert "start_other" not in main and "start_other" not in win
    assert 'wide("open")' not in win, "the shell does not open things itself: it runs with administrator rights"
    opened = win[win.index("pub fn open_link("):win.index("/// Windows' own folder")]
    assert 'format!("{}\\\\explorer.exe", windows_folder()?)' in opened and "start_line_without_handles(&explorer, &line)" in opened
    assert 'let line = line_for_a_link(&explorer, link).ok_or("not a web address")?;' in opened
    assert 'link.starts_with("https://") || link.starts_with("http://")' in opened
    assert "open_link(" not in win[win.index("#[cfg(test)]"):], "no test hands anything to Explorer"
    started = win[win.index("fn start_line_without_handles("):win.index("/// Open a link in the person's browser")]
    assert "let inherit_handles = 0;" in started and "std::ptr::null(), inherit_handles, 0," in started
    assert "std::process::Command" not in main, "the window starts nothing itself"
    # WebView2 is asked for before a window is made, and a missing one is said in a plain message.
    run = main[main.index("fn run("):]
    assert run.index("tauri::webview_version().map_err(no_window)?") < run.index("Shell::new(") < run.index("WebviewWindowBuilder::new(")
    assert "tell(trial.as_ref(), &trouble.words());" in main and "None => win::tell(APP_NAME, text)," in main


def test_what_keeps_the_app_from_starting_is_said_in_words_of_its_own():
    """A folder that cannot be made was reported as a missing WebView2, and
    the person sent to install it.  Each kind of trouble has its own words
    (core/src/trouble.rs, with a test of its own there)."""
    main, trouble = app_crate("src", "main.rs"), rust("src", "trouble.rs")
    run = main[main.index("fn run("):]
    assert "-> Result<i32, Trouble> {" in run
    assert "Shell::new(settings, win::platform()).map_err(Trouble::Shell)?" in run
    assert run.count("Trouble::Folder {") == 2, "where the folder is made cannot be found, and it cannot be made"
    assert "win::private_folder(&folder_in, open_to).map_err(|why| Trouble::Folder { place: folder_in.clone(), why })?" in run
    for kind in ("Shell(String)", "Folder { place: String, why: String }", "Window(String)", "Backend(String)"):
        assert kind in trouble, kind
    said = trouble[:trouble.index("#[cfg(test)]")]
    # Named where the window cannot be opened (twice), and where it is not
    # to blame (the window's folder, and the backend's unpacking).
    assert said.count("Microsoft Edge WebView2") == 4 and said.count("nothing to do with Microsoft Edge WebView2") == 2
    assert "Microsoft Edge WebView2" not in main, "the words are in one place"
    # In a trial the words go into a file: a box would sit on somebody's screen.
    assert "Some(file) => {" in main and '"messages": self.messages_file,' in app_crate("try_it.py")
    assert "says so in words of its own" in app_crate("try_it.py")


def test_nothing_of_a_run_is_kept_in_the_windows_folder_and_nothing_found_there_is_used():
    """WebView2 keeps a profile folder.  A fixed one in the person's AppData
    can be read by any of their programs (the page keeps the run's secret in
    its session storage) and written by them before the next start (what is
    planted there runs in the app's page).  So: a private window, in a folder
    that is new at every start, closed to programs without the app's rights,
    and removed afterwards."""
    main, win = app_crate("src", "main.rs"), rust("src", "win.rs")
    made = main[main.index("WebviewWindowBuilder::new("):main.index(".map_err(no_window)?;", main.index("WebviewWindowBuilder::new("))]
    assert ".incognito(true)" in made and ".data_directory(PathBuf::from(&own_folder.path))" in made
    assert made.count(".data_directory(") == 1 and '"webview"' not in main
    run = main[main.index("fn run("):]
    assert "win::administrators_folder().map_err(" in run and "(place, win::OpenTo::Administrators)" in run
    assert "unwrap_or_else(|| folders.appdata_dir.clone()), win::OpenTo::ThisAccount)" in run
    assert run.index("let own_folder = win::private_folder(&folder_in, open_to)") < run.index("WebviewWindowBuilder::new(")
    # Removed when the run is over.
    assert run.index("let code = app.run_return(") < run.index("if !own_folder.remove() {")
    # The folder: a name nobody can guess, never one that is there already,
    # and nobody in it but the ones named and Windows itself.
    folder = win[win.index("fn written(open_to: OpenTo)"):win.index("/// Make the folder `path`, which must not be there yet: owned by")]
    assert "Windows.random(&mut random)?;" in folder and "let made = CreateDirectoryW(path_w.as_ptr(), &attributes);" in folder
    assert 'OpenTo::Administrators => Ok("BA".to_string()),' in folder
    assert "private_folder_named(parent, WINDOW_FOLDER_PREFIX, open_to)" in folder
    assert 'closed_folder(&path, &format!("O:{who}{}", open_only_to(&who)))?;' in folder, "whose it is, and who gets in"
    assert 'format!("D:P(A;OICI;FA;;;{who})(A;OICI;FA;;;SY)")' in folder
    assert "std::fs::create_dir(&path)" not in folder, "every folder is made closed"
    # Where that is, is not taken from the environment, which is the
    # person's programs' to set: see the test after this one.
    where = win[win.index("pub fn windows_folder("):win.index("// The window's own folder, one per run")]
    assert "GetSystemWindowsDirectoryW(path.as_mut_ptr(), MAX_PATH as u32)" in where
    assert 'let folder = format!("{}\\\\Temp", windows_folder()?);' in where
    for word in ("std::env::var", "SHGetFolderPath", "SHGetKnownFolderPath", "GetTempPath", "temp_dir()"):
        assert word not in where, word
    for word in ("ProgramData", "temp_dir()", "TEMP"):
        assert word not in main, word
    # The trial looks into the folder for the secret, during a run and after it.
    trial = app_crate("try_it.py")
    assert "the run's secret is in no file of the window's folder" in trial and "the window's folder is gone after the run, and nothing of WebView2 is left anywhere else" in trial


def test_a_start_never_takes_away_a_folder_that_is_in_use_and_never_keeps_the_window_waiting():
    """Two people signed in at one PC each run a copy ("is a copy running"
    is asked per session), and both copies' windows have their folders in
    the same place.  A start removed every folder of the window's kind it
    found there: the other copy's, under its running window, among them.
    Now a run marks its folder for as long as it runs, and a folder whose
    mark is held is not touched.

    And the tidy-up asked Windows about every entry of the place before it
    looked at its name (thousands, in Windows' own Temp folder), before the
    window was made.  Now: the name first, and after the window is there."""
    main, win = app_crate("src", "main.rs"), rust("src", "win.rs")
    # The mark: made with the folder, held so that nobody else can open it.
    folder = win[win.index("pub fn private_folder("):win.index("/// Make the folder `path`, which must not be there yet: owned by")]
    assert "match hold_mark(&path) {" in folder and "Ok(in_use) => return Ok(OwnFolder { path, in_use: Some(in_use) })," in folder
    assert ".create_new(true).share_mode(nobody_else).open(format!(\"{folder}\\\\{IN_USE_MARK}\"))" in folder
    assert "let nobody_else = 0;" in folder
    # The tidy-up: the name first, then only what the listing already said.
    tidy = win[win.index("pub fn remove_private_folders("):win.index("/// Open what is under the mark's name in `folder`")]
    named = tidy.index("if !ONE_RUNS_FOLDERS.iter().any(|prefix| name.starts_with(prefix)) {\n            continue;")
    assert named < tidy.index("entry.metadata()") < tidy.index("left_behind(&path, &found)")
    assert "symlink_metadata" not in tidy, "a question to Windows for every entry of the place"
    # What may be removed: the mark is there, is a plain file, and nobody
    # holds it; or there is no mark and the folder is too old to be one that
    # is being made.
    left = win[win.index("fn open_mark("):win.index("/// Take every setting for WebView2")]
    assert "Ok(mark) => is_a_plain_file(&mark)," in left and "Err(_) => false," in left and "Ok(_) => true," not in left
    assert "is_some_and(|age| age >= MARK_EXPECTED_WITHIN)" in left
    # The mark is opened for this program alone, without following a link,
    # and without handing this program's rights to whatever is at the other
    # end.  (Only a folder of this program's own making is asked for its
    # mark at all: tests/test_one_file.py has that.)
    opened = left[:left.index("/// Only a plain file is a mark")]
    for said in (".share_mode(nobody_else)", ".custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)", ".security_qos_flags(SECURITY_IDENTIFICATION)",
                 ".open(folder.join(IN_USE_MARK))"):
        assert said in opened, said
    assert "const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;" in win and "const SECURITY_IDENTIFICATION: u32 = 0x0001_0000;" in win
    assert "found.is_file() && found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0" in left
    assert win.count(".open(path.join(IN_USE_MARK))") == 0, "the mark is opened in one place"
    # In the window's program: on a thread of its own, after the window is made.
    run = main[main.index("fn run("):]
    window_made = run.index(".map_err(no_window)?;", run.index("WebviewWindowBuilder::new("))
    tidied = run.index("let tidied = win::remove_private_folders(&tidy_in, open_to);")
    assert window_made < run.index("std::thread::spawn(move || {", window_made) < tidied < run.index("shell.start();")
    assert run.count("remove_private_folders(") == 1
    # The trial tries both for real.
    trial = app_crate("try_it.py")
    assert "its start left the first copy's folder alone" in trial and "this run's folder is marked as in use" in trial
    assert "taken away at the next start, with %d other" in trial


def test_where_the_shell_keeps_things_does_not_follow_the_environment():
    """The app runs with administrator rights; the environment it is started
    with can be set by a program that has none.  The two folders the shell
    takes from Windows (Windows' own, for explorer.exe, and the one the
    window's folder is made in) are asked for in a way that does not look
    at it.  Tried for real: the trial tool is started with every variable
    that names such a place pointing at a planted folder that has all of
    them inside, and says where it would go."""
    app()
    if not TRY:
        return
    planted = os.path.join(m.ROOT, "planted")
    for inside in ("ProgramData", "Temp", os.path.join("Windows", "Temp"), os.path.join("System32")):
        os.makedirs(os.path.join(planted, inside), exist_ok=True)

    def asked(env):
        out = m.REAL_POPEN([TRY, "--where"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        said, complaint = out.communicate(timeout=30)
        assert out.returncode == 0, complaint
        return json.loads(said)

    honest = asked(dict(os.environ))
    assert os.path.isfile(os.path.join(honest["windows"], "explorer.exe")), honest
    assert honest["administrators_folder"] == honest["windows"] + "\\Temp" and os.path.isdir(honest["administrators_folder"])
    env = {name: value for name, value in os.environ.items()
           if name.upper() not in ("SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "PROGRAMDATA", "ALLUSERSPROFILE", "TEMP", "TMP", "PUBLIC")}
    env.update(SystemDrive=planted, SystemRoot=os.path.join(planted, "Windows"), windir=os.path.join(planted, "Windows"),
               ProgramData=os.path.join(planted, "ProgramData"), ALLUSERSPROFILE=os.path.join(planted, "ProgramData"),
               TEMP=os.path.join(planted, "Temp"), TMP=os.path.join(planted, "Temp"), PUBLIC=planted)
    assert asked(env) == honest, "the environment decided where the shell keeps things"


def test_webview2_takes_no_settings_from_the_environment():
    """WebView2 reads settings from the environment: where to take the
    browser's programs from, what to start them with, where the profile is.
    A program without rights can set them for the person and wait for the
    next start of the app, which has rights.  So they are removed first
    thing, before anything of WebView2 is touched."""
    main, win = app_crate("src", "main.rs"), rust("src", "win.rs")
    body = main[main.index("fn main() {"):main.index("/// Open the window, start the backend")]
    statements = [line.strip() for line in body.splitlines()[1:] if line.strip() and not line.strip().startswith("//")]
    assert statements[0] == "let ignored = win::clear_webview2_variables();", statements[0]
    assert main.count("tauri::") and body.index("win::clear_webview2_variables()") < body.index("instance::begin(")
    assert "WEBVIEW2_" not in main, "the shell sets none of them: what it wants it says in the window's options"
    cleared = win[win.index("pub fn clear_webview2_variables("):win.index("#[cfg(test)]")]
    assert '.filter(|name| name.to_ascii_uppercase().starts_with("WEBVIEW2_"))' in cleared and "std::env::remove_var(name);" in cleared
    assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in app_crate("try_it.py")


def test_asking_for_rights_cannot_go_round_in_a_circle():
    """Where Windows starts the new copy without rights all the same, each
    copy would start the next and leave.  The copy that was started for
    rights carries a mark; with the mark and without rights it says so and
    stops.  The mark goes to that copy and nowhere else."""
    main, instance = app_crate("src", "main.rs"), rust("src", "instance.rs")
    assert 'pub const ASKED_FOR_RIGHTS: &str = "--asked-for-rights";' in instance
    order = instance[instance.index("pub fn begin("):instance.index("#[cfg(test)]")]
    stops = order.index("if arguments.iter().any(|argument| argument == ASKED_FOR_RIGHTS) {\n            return Begin::NotElevated(")
    assert order.index("!session.has_administrator_rights()") < stops < order.index("marked.push(ASKED_FOR_RIGHTS.to_string());") \
        < order.index("session.start_with_administrator_rights(&marked)")
    # The backend is given two words of the shell's own choosing, and none of what the shell was started with.
    given = [line.strip() for line in main.splitlines() if "backend.args.push(" in line]
    assert given == ['backend.args.push("--backend".to_string());', 'backend.args.push("--from-autostart".to_string());'], given
    assert "backend.args.extend" not in main and "args: arguments" not in main
    assert "--asked-for-rights" in repo("shell_rs", "README.md")


def test_a_first_copy_is_looked_for_before_anything_asks_for_rights():
    """A second start must not put Windows' prompt on the screen only to say
    that the app is running.  So the exe itself does not ask for rights (a
    manifest that did would put the prompt before everything); the shell
    asks, after it has looked for a first copy."""
    main, instance = app_crate("src", "main.rs"), rust("src", "instance.rs")
    manifest, build = app_crate("app.manifest"), app_crate("build.rs")
    assert 'level="asInvoker"' in manifest and "requireAdministrator" not in manifest
    assert 'include_str!("app.manifest")' in build and '!manifest.contains("requireAdministrator")' in build
    assert re.search(r'^default = \["elevated"\]$', app_crate("Cargo.toml"), re.M)
    assert 'const ASKS_FOR_RIGHTS: bool = cfg!(feature = "elevated");' in main
    # In main(): first copy, then everything else.
    body = main[main.index("fn main() {"):main.index("/// Open the window, start the backend")]
    begun = body.index("instance::begin(&win::session(names), ASKS_FOR_RIGHTS, &arguments)")
    assert begun < body.index("paths::Paths::resolve(") < body.index("Settings::new(backend,") < body.index("run(settings,")
    assert "Begin::Shown | Begin::Running | Begin::Elevating => return," in body
    # In begin(): show the other, then rights, then the name.
    order = instance[instance.index("pub fn begin("):instance.index("#[cfg(test)]")]
    assert order.index("session.ask_other_to_show()") < order.index("session.another_is_running()") \
        < order.index("session.has_administrator_rights()") < order.index("session.become_the_one()")
    # The name is the one the Python app holds, so that an old copy and a new one see each other.
    win = rust("src", "win.rs")
    name = re.search(r'MUTEX_NAME\s*=\s*r"([^"]+)"', repo("app.py")).group(1)
    assert f'one_copy: r"{name}".to_string(),' in win


def test_a_trial_of_the_window_never_meets_the_installed_app():
    main, trial, manifest = app_crate("src", "main.rs"), app_crate("try_it.py"), app_crate("Cargo.toml")
    names = [name.lower() for name in re.findall(r'^name = "([^"]+)"$', manifest, re.M)]
    assert "vispora" not in names and "ghostshell" not in names and "visporashell" in names, \
        "no exe named after the app is ever started in a trial"
    # A trial build takes its backend and its names from the trial, the build that ships reads none of it.
    assert "if ASKS_FOR_RIGHTS {\n        return None;\n    }" in main
    assert "win::Names::for_a_trial(&trial.tag)" in main and "win::Names::of_the_app" in main
    assert "if !ASKS_FOR_RIGHTS && trial.is_none() {" in main, "a trial build without a trial does not act as the app"
    # The script: the stand-in backend, redirected folders, never elevated, never the build that ships.
    assert 'STAND_IN = os.path.join(REPO, "tests", "shell_backend_stand_in.py")' in trial
    assert '"backend": {"program": sys.executable, "args": ["-u", STAND_IN], "cwd": REPO},' in trial
    for folder in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        assert f'("{folder}", ' in trial, folder
    assert 'refuse("this terminal runs as administrator' in trial
    assert 'if b"VISPORA_SHELL_TRY" not in f.read():' in trial
    assert "taskkill\", \"/PID\", str(self.process.pid)" in trial and "/IM" not in trial, \
        "only the process the trial started is ever ended, and by its number"


def test_what_else_the_shell_starts_does_not_get_its_socket():
    """The shell's listening socket is inheritable on purpose: the backend
    inherits it.  Nothing else the shell starts must (Explorer, which is
    handed the links the page opens, is the one such program)."""
    app()
    if not TRY:
        return
    t = Trial()
    try:
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        state = t.ask(do="state")
        handle, port = state["listen"], state["port"]
        out = os.path.join(m.ROOT, "inherited.txt")
        looks = ("import socket, sys\n"
                 "try:\n"
                 "    s = socket.socket(fileno=int(sys.argv[1]))\n"
                 "    got = 'got it' if s.getsockname()[1] == int(sys.argv[2]) else 'another socket'\n"
                 "    s.detach()\n"
                 "except OSError:\n"
                 "    got = 'nothing there'\n"
                 "open(sys.argv[3], 'w').write(got)\n")
        script = os.path.join(m.ROOT, "looks_for_the_socket.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(looks)
        started = t.ask(do="start_other", program=sys.executable.replace("python.exe", "pythonw.exe"),
                        arguments=[script, str(handle), str(port), out])
        assert started["ok"] is True, started
        deadline = time.time() + 30
        while not os.path.isfile(out) and time.time() < deadline:
            time.sleep(0.05)
        with open(out, encoding="utf-8") as f:
            assert f.read() == "nothing there", "a program the shell started got the shell's listening socket"
    finally:
        t.close()


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
