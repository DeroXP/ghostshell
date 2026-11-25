"""Try the shell's window for real, out of the way, and time it.

    cargo build --release --locked --no-default-features --manifest-path shell_rs/app/Cargo.toml

    python shell_rs/app/try_it.py                  the checks; the window is never shown
    python shell_rs/app/try_it.py --time           the times, five runs
    python shell_rs/app/try_it.py --picture x.png  a picture of the window
    python shell_rs/app/try_it.py --show           the window, to look at and click

The cargo tests cover what the shell decides.  This script covers what they
cannot: the exe itself, with its real window, the app's real page in it, and
a backend it starts.  What it is given is made up, and nothing of the PC's
own is touched:

  * the backend is NOT the real one.  It is the app on the fake machine of
    tests/sandbox.py (tests/shell_backend_stand_in.py), as a real process;
  * the exe is VisporaShell.exe, never an exe named after the app, and it
    knows itself by names of this trial's own: it never takes the installed
    Vispora for its first copy, or the other way round;
  * APPDATA, LOCALAPPDATA and USERPROFILE point into a temporary folder for
    it, so its log and the window's own folder land there;
  * the window is hidden for the checks, and for the times and the picture
    it is drawn beyond the edge of the desktop and never takes the keyboard.

With --show the window is a real one in the middle of the screen, on the
fake machine: what is clicked there changes nothing on this PC.  Quit it
from its own X.

The checks also look at what another program on the PC could get at: the
window's folder is searched for the run's secret while the app runs and must
be gone afterwards; settings for WebView2 are planted in the environment and
must not be used; a page from somewhere else is put into a frame and must
find nothing of the app's there; the page is sent somewhere else and must
stay.  "Somewhere else" is this PC every time (127.0.0.1): nothing is asked
of the internet, and no link is opened in a browser.

Only the build made WITHOUT the `elevated` feature is run (the command
above).  The build that ships asks for administrator rights when it starts;
it is refused here, and so is an elevated terminal.

It is not part of `python tests/run_all.py`.
"""
import ctypes
import http.server
import json
import os
import re
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
STAND_IN = os.path.join(REPO, "tests", "shell_backend_stand_in.py")
WINDOW_TITLE = "Vispora"
NO_WINDOW = 0x08000000
WM_CLOSE = 0x0010
WM_ENDSESSION = 0x0016
WINDOW_FOLDER = re.compile(r"^Vispora-window-[0-9a-f]{32}$")
# How long after the page has its secret the window's folder is searched for
# it.  A window that is not private writes it down twice: with the page's
# session storage, 4.4 to 4.7 s after the page had it, and with the address
# the window was opened at, in its history, after 9.4 s (measured here, four
# runs).  A search before that finds nothing in either kind of window and
# shows nothing.
WRITTEN_DOWN_AFTER_S = 12


def built(name):
    outside = os.path.join(os.environ.get("CARGO_TARGET_DIR") or HERE, "release", name)
    return outside if os.path.isfile(outside) else os.path.join(HERE, "target", "release", name)


EXE = built("VisporaShell.exe")


def refuse(why):
    print("Not started: " + why)
    sys.exit(2)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Elsewhere:
    """A page that is not the app's, served on this PC only: what a frame in
    the window would be if the page ever showed one.  It looks for what the
    window gives the app's own page, and says what it found."""

    PAGE = (b"<!doctype html><script>\n"
            b"var has = (window.pywebview && window.pywebview.api) ? 'has-the-api' : 'has-nothing';\n"
            b"fetch('/saw/' + has);\n"
            b"</script>\n")

    def __init__(self):
        seen = self.seen = []
        page = self.PAGE

        class Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = page if self.path == "/frame.html" else b""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Run:
    """One start of the exe, with everything it is given."""

    def __init__(self, root, window, planted=False):
        self.root = tempfile.mkdtemp(prefix="run-", dir=root)
        self.notes = os.path.join(self.root, "put-back.txt")
        self.calls_file = os.path.join(self.root, "calls.txt")
        self.secret_file = os.path.join(self.root, "secret.txt")
        self.scripts = os.path.join(self.root, "scripts")
        os.makedirs(self.scripts)
        self.env = dict(os.environ)
        for var, folder in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "Home")):
            self.env[var] = os.path.join(self.root, folder)
            os.makedirs(self.env[var])
        self.data = os.path.join(self.env["APPDATA"], "Vispora")
        self.env["VISPORA_STAND_IN_NOTES"] = self.notes
        self.env["VISPORA_STAND_IN_CALLS"] = self.calls_file
        self.env["VISPORA_STAND_IN_SECRET"] = self.secret_file
        self.env["VISPORA_SHELL_TRY"] = json.dumps({
            "tag": "%d%d" % (os.getpid(), int(time.time() * 1000) % 10 ** 9),
            "window": window,
            "scripts": self.scripts,
            "backend": {"program": sys.executable, "args": ["-u", STAND_IN], "cwd": REPO},
        })
        self.debug_port = self.planted_profile = self.left_behind = None
        if planted:
            # What a program without rights could do before the app starts:
            # settings for WebView2 in the person's environment (a way in for
            # a debugger, a profile of its choosing), and a folder made to
            # look like one the window left behind.
            self.debug_port = free_port()
            self.planted_profile = os.path.join(self.root, "planted-profile")
            self.env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=%d" % self.debug_port
            self.env["WEBVIEW2_USER_DATA_FOLDER"] = self.planted_profile
            self.left_behind = os.path.join(self.data, "Vispora-window-" + "0" * 32)
            os.makedirs(os.path.join(self.left_behind, "EBWebView", "Default", "Service Worker"))
            with open(os.path.join(self.left_behind, "EBWebView", "Default", "Service Worker", "planted.js"), "w") as f:
                f.write("// what an earlier run left, or somebody put there")
        self.began = time.time()
        self.process = subprocess.Popen([EXE], env=self.env, cwd=self.root)

    def again(self, *arguments):
        """A second start of the same app."""
        return subprocess.Popen([EXE, *arguments], env=self.env, cwd=self.root)

    def log(self):
        try:
            with open(os.path.join(self.env["APPDATA"], "Vispora", "shell.log"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def calls(self):
        """What the backend was asked: (seconds since the start, status, method, path)."""
        try:
            with open(self.calls_file, encoding="utf-8") as f:
                # (whole lines only: the backend may be in the middle of writing the last one)
                rows = [line.split() for line in f.read().split("\n")[:-1]]
        except OSError:
            return []
        return [(float(row[0]) - self.began, int(row[1]), row[2], row[3]) for row in rows if len(row) == 4]

    def put_back(self):
        try:
            with open(self.notes, encoding="utf-8") as f:
                return f.read().splitlines()
        except OSError:
            return []

    def secret(self):
        """The run's secret, as the stand-in backend was handed it."""
        try:
            with open(self.secret_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def window_folder(self):
        found = re.findall(r"the window's folder for this run: (.+)", self.log())
        return found[-1].strip() if found else None

    def said(self, what):
        """Did a script in the page say `what`?  (It says things by asking
        the backend for a path that does not exist, with the secret.)"""
        return [call for call in self.calls() if call[3] == "/api/trial/" + what]

    def in_the_page(self, script):
        """Run `script` in the window's page (a trial build takes scripts from a folder)."""
        name = os.path.join(self.scripts, "%d" % (time.time() * 1000))
        with open(name + ".tmp", "w", encoding="utf-8") as f:
            f.write(script)
        os.replace(name + ".tmp", name + ".js")

    def end_the_session(self):
        """What Windows sends when the person signs out or shuts down: to
        every window of the program, the ones nobody sees among them."""
        windows = windows_of(self.process.pid)
        for window in windows:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(window), WM_ENDSESSION, 1, 0)
        return len(windows)

    def wait_for(self, what, within=90):
        ends = time.time() + within
        while time.time() < ends:
            got = what()
            if got:
                return got
            if self.process.poll() is not None:
                return None
            time.sleep(0.01)
        return None

    def page_calls(self):
        """The page's own calls (not the shell's "are you up")."""
        return [call for call in self.calls() if call[3] != "/api/shell/hello"]

    def backend_pid(self):
        found = re.findall(r"backend started \(pid (\d+)", self.log())
        return int(found[-1]) if found else None

    def window(self, shown=True):
        return find_window(self.process.pid, shown)

    def close_the_window(self):
        window = self.window(shown=False)
        if window:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(window), WM_CLOSE, 0, 0)   # what Alt+F4 sends
        return bool(window)

    def end(self):
        if self.process.poll() is None:
            if self.close_the_window():
                try:
                    self.process.wait(60)
                except subprocess.TimeoutExpired:
                    pass
        if self.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)


def windows_of(pid):
    """Every top-level window of that process, shown or not."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found


def find_window(pid, shown=True):
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
        title = ctypes.create_unicode_buffer(200)
        user32.GetWindowTextW(wintypes.HWND(hwnd), title, 200)
        if owner.value == pid and title.value == WINDOW_TITLE and (
                not shown or user32.IsWindowVisible(wintypes.HWND(hwnd))):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def who_gets_in(folder, note):
    """Who gets into `folder`, the way Windows writes such a list down (the
    same in every language).  icacls writes it into the file `note`."""
    subprocess.run(["icacls", folder, "/save", note, "/q"], capture_output=True, creationflags=NO_WINDOW)
    try:
        with open(note, encoding="utf-16-le") as f:    # (as icacls writes it, without a mark at the start)
            lines = [line.strip() for line in f.read().splitlines()]
    except (OSError, UnicodeError):
        return ""
    return next((line for line in lines if line.startswith("D:")), "")


def own_account():
    """The account this script runs under, the way Windows writes it (S-1-5-...)."""
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    found = re.search(r'"(S-1-5-[\d-]+)"', out)
    return found.group(1) if found else "?"


def files_with(folder, secret, but=()):
    """The files under `folder` that hold `secret`, written either of the two
    ways a browser writes text down; how many files were read; how many
    could not be."""
    wanted = (secret.encode("ascii"), secret.encode("utf-16-le"))
    found, read, closed = [], 0, 0
    for where, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(where, name)
            if path in but:
                continue
            try:
                with open(path, "rb") as f:
                    content = f.read()
            except OSError:
                closed += 1
                continue
            read += 1
            if any(w in content for w in wanted):
                found.append(os.path.relpath(path, folder))
    return found, read, closed


SAY = r"""
    function say(what) {
        return window.pywebview.api.get_api_token().then(function (token) {
            return fetch('/api/trial/' + what, { headers: { 'X-Vispora-Token': token } });
        });
    }
    function has(where) { return (where.pywebview && where.pywebview.api) ? 'has-the-api' : 'has-nothing'; }
"""
# The app's own page has what the window gives it; a frame in it has not:
# neither an empty one, nor one that shows a file of the app's own (the app's
# own place, but not the top of the window), nor a page from somewhere else.
FRAMES = "(function () {" + SAY + r"""
    say('the-page-' + has(window));
    var empty = document.createElement('iframe');
    document.body.appendChild(empty);
    setTimeout(function () { say('an-empty-frame-' + has(empty.contentWindow)); }, 500);
    var own = document.createElement('iframe');
    own.onload = function () { say('a-frame-of-its-own-' + has(own.contentWindow)); };
    own.src = '/static/css/style.css';
    document.body.appendChild(own);
    var other = document.createElement('iframe');
    other.src = 'http://127.0.0.1:%d/frame.html';
    document.body.appendChild(other);
})();"""
# The page is sent to another port and another scheme under its own host
# name (nothing listens on either: this PC, and refused at once).  If the
# window went, the page is gone and says nothing any more.
ELSEWHERE = "(function () {" + SAY + r"""
    location.href = 'http://vispora.localhost:9/elsewhere';
    setTimeout(function () { location.href = 'https://vispora.localhost:9/elsewhere'; }, 700);
    setTimeout(function () { say('still-the-apps-page'); }, 2500);
})();"""


def running(pid):
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return ('"%d"' % pid) in out


def take_picture(hwnd, path):
    """What Windows draws for that window (PrintWindow), written as a PNG."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.restype, user32.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = wintypes.HDC, [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes, gdi32.DeleteDC.argtypes = [wintypes.HGDIOBJ], [wintypes.HDC]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(memory, bitmap)
    drawn = user32.PrintWindow(hwnd, memory, 2)   # 2: with what the graphics card composes (the page)
    header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory, bitmap, 0, height, pixels, header, 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory)
    user32.ReleaseDC(None, screen)
    bgra = pixels.raw
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    rows = b"".join(b"\0" + bytes(rgb[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
    return bool(drawn), width, height, len(set(bgra[0::4][::997]))


# ─── The checks ──────────────────────────────────────────────────────

def checks(root):
    results = []

    def check(what, ok):
        results.append((what, bool(ok)))
        print("  %s  %s" % ("ok    " if ok else "FAILED", what))

    elsewhere = Elsewhere()
    run = Run(root, "hidden", planted=True)
    try:
        # The page is the app's page, loaded in the real window: it asks the
        # backend for its first screen, with the secret from its address.
        first = run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"])
        has_the_secret = time.time()
        check("the window loaded the page, and the page got its first screen from the backend", first)
        calls = run.page_calls()
        check("every call of the page was taken (%d calls, none refused)" % len(calls),
              calls and all(status != 403 for _, status, _, _ in calls))
        log = run.log()
        check("one backend was started, with no window shown", log.count("backend started (pid ") == 1
              and run.window(shown=True) is None and run.window(shown=False) is not None)
        backend = run.backend_pid()

        # What another program could have prepared before the start.
        ignored = re.search(r"settings for WebView2 were in the environment and were not used: (.+)", log)
        check("settings for WebView2 planted in the environment were not used (the log names both)",
              ignored and {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_USER_DATA_FOLDER"}
              == set(ignored.group(1).strip().split(", ")))
        check("no debugger's way in was opened (port %d), no profile made where the planted setting said"
              % run.debug_port, not listening(run.debug_port) and not os.path.exists(run.planted_profile))
        check("a folder made to look like one the window left behind was removed, not used",
              not os.path.exists(run.left_behind) and "removed 1 folder(s) of the window" in log)
        folder = run.window_folder()
        check("the window has a folder of this run's own, and WebView2 is in it: %s" % (folder and os.path.basename(folder)),
              folder and os.path.isdir(os.path.join(folder, "EBWebView")) and os.path.dirname(folder) == run.data
              and WINDOW_FOLDER.match(os.path.basename(folder)) and folder != run.left_behind)

        gets_in = who_gets_in(folder, os.path.join(run.root, "who-gets-in.txt")) if folder else ""
        check("the folder is closed to everybody but the trial's own account and Windows itself: %s" % gets_in,
              gets_in == "D:P(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)" % own_account())

        # What the window gives the app's page, it gives to nothing else.
        run.in_the_page(FRAMES % elsewhere.port)
        check("the app's own page has what it asks the window for", run.wait_for(lambda: run.said("the-page-has-the-api"), 20))
        check("an empty frame in the page has none of it",
              run.wait_for(lambda: run.said("an-empty-frame-has-nothing"), 20) and not run.said("an-empty-frame-has-the-api"))
        check("a frame that shows one of the app's own files has none of it",
              run.wait_for(lambda: run.said("a-frame-of-its-own-has-nothing"), 20) and not run.said("a-frame-of-its-own-has-the-api"))
        run.wait_for(lambda: [p for p in elsewhere.seen if p.startswith("/saw/")], 20)
        check("a page from somewhere else, in a frame, has none of it: %r" % (elsewhere.seen,),
              elsewhere.seen == ["/frame.html", "/saw/has-nothing"])
        # The window stays on the app's page.
        run.in_the_page(ELSEWHERE)
        check("sent to another port and another scheme under its own host name, the window stays",
              run.wait_for(lambda: run.said("still-the-apps-page"), 20))

        # A second start asks the first for its window and leaves, and starts nothing.
        second = run.again()
        try:
            left = second.wait(30)
        except subprocess.TimeoutExpired:
            left = "still running"
            second.kill()
        check("a second start leaves at once (exit code 0, got %r)" % (left,), left == 0)
        asked = run.wait_for(lambda: "another start of the app asked for the window" in run.log(), within=10)
        check("the first copy was asked for its window", asked)
        check("no second backend was started", run.log().count("backend started (pid ") == 1)
        shown = run.wait_for(lambda: run.window(shown=True), within=10)
        check("and showed it", shown)

        # Nothing of the run is written where another program can read it.
        secret = run.secret()
        time.sleep(max(0.0, has_the_secret + WRITTEN_DOWN_AFTER_S - time.time()))
        found, read, closed = files_with(folder, secret) if secret and folder else (["?"], 0, 0)
        check("the run's secret is in no file of the window's folder, %d s after the page had it "
              "(%d files read, %d held closed by WebView2): %r" % (WRITTEN_DOWN_AFTER_S, read, closed, found),
              len(secret) >= 32 and read > 0 and not found)

        # Closing the window from outside quits the app: the backend puts
        # the (fake) system back and ends, and only then the shell.
        began = time.time()
        check("the window takes a close from outside", run.close_the_window())
        try:
            code = run.process.wait(60)
        except subprocess.TimeoutExpired:
            code = "still running"
        check("the shell ends by itself, exit code 0 (got %r)" % (code,), code == 0)
        check("the backend put the system back before it went: %r" % (run.put_back(),),
              run.put_back() == ["put back: user quit"])
        check("the backend's process is gone", backend and not running(backend))
        check("in %.1f s" % (time.time() - began), time.time() - began < 30)
        log = run.log()
        check("the secret is in no line of the log", "#t=" not in log and "token" not in log.lower() and secret not in log)
        left = [os.path.relpath(where, run.root) for where, folders, _ in os.walk(run.root)
                if "EBWebView" in folders or os.path.basename(where).startswith("Vispora-window-")]
        check("the window's folder is gone after the run, and nothing of WebView2 is left anywhere else: %r" % (left,),
              folder and not os.path.exists(folder) and not left)
        found, read, _ = files_with(run.root, secret, but=(run.secret_file,))
        check("and the secret is in no file the run left (%d files): %r" % (read, found), read > 0 and not found)
    finally:
        run.end()
        elsewhere.close()

    # Signing out, shutting down, or Windows replacing the app's files: the
    # window is told, and the process is ended as soon as it has answered.
    # The quit has to be over by then.
    run = Run(root, "hidden")
    try:
        check("(a second run) the page came up again", run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"]))
        has_the_secret = time.time()
        again = run.window_folder()
        check("in another folder than the first run's", again and folder and again != folder and os.path.isdir(again))
        backend = run.backend_pid()
        # (long enough for a window that is not private to have written the
        # secret down: this run's folder is looked into after the run)
        time.sleep(max(0.0, has_the_secret + WRITTEN_DOWN_AFTER_S - time.time()))
        began = time.time()
        told = run.end_the_session()                # what try_it.py sends: WM_ENDSESSION
        check("the program's windows are told that the session ends (%d of them)" % told, told >= 2)
        try:
            code = run.process.wait(60)
        except subprocess.TimeoutExpired:
            code = "still running"
        took = time.time() - began
        check("the backend's process was gone before the shell's", backend and not running(backend))
        check("the shell ended, exit code 0 (got %r)" % (code,), code == 0)
        check("the backend put the system back first: %r" % (run.put_back(),), run.put_back() == ["put back: user quit"])
        check("in %.1f s (Windows gives a program about five)" % took, took < 5)
        # The window's folder cannot be taken away at that moment (WebView2
        # is still in it); the next start does that.  What is in it until then:
        time.sleep(3)                               # (WebView2's programs end and let go of their files)
        found, read, closed = files_with(again, run.secret()) if again and run.secret() else (["?"], 0, 0)
        check("the folder this run had to leave behind does not hold the secret "
              "(%d files read, %d could not be): %r" % (read, closed, found), read > 0 and not found)
    finally:
        run.end()
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed." % (len(results), len(failed)))
    return not failed


# ─── The times ───────────────────────────────────────────────────────

def times(root, runs=5):
    rows = []
    for _ in range(runs):
        run = Run(root, "offscreen")
        try:
            t = {}
            seen = run.wait_for(lambda: run.window(shown=True))
            t["a window on screen"] = (time.time() - run.began) * 1000 if seen else float("nan")
            first = run.wait_for(run.page_calls)
            t["the page is loaded and makes its first call"] = first[0][0] * 1000 if first else float("nan")
            screen = run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"])
            t["the first screen has its data (first page usable)"] = screen[0][0] * 1000 if screen else float("nan")
            time.sleep(1.0)                     # (let the page finish its first round of calls)
            began = time.time()
            run.close_the_window()
            backend = run.backend_pid()
            run.process.wait(60)
            ended = (time.time() - began) * 1000
            gone = re.findall(r"the backend ended \(exit code -?\d+\) (\d+) ms after the quit began", run.log())
            t["quit: until the backend has ended (the shell's clock)"] = float(gone[-1]) if gone else float("nan")
            t["quit: until the shell's process has ended"] = ended
            t["... and the backend's is gone"] = 0.0 if not (backend and running(backend)) else float("nan")
            rows.append(t)
        finally:
            run.end()
    print("%d run(s); the middle value of each, in milliseconds (lowest to highest)\n" % runs)
    for name in rows[0]:
        values = [row[name] for row in rows]
        if name.startswith("..."):
            print("  %-56s %s" % (name, "yes, every time" if all(v == 0.0 for v in values) else "NOT every time"))
        else:
            print("  %-56s %9.0f   (%.0f to %.0f)" % (name, statistics.median(values), min(values), max(values)))
    print("\nThe window and the page are the real ones. The backend is the app on the fake machine:\n"
          "what depends on it (the first screen's data, the quit) is a trial's time, not a PC's.")
    return True


# ─── To look at ──────────────────────────────────────────────────────

def picture(root, path):
    ctypes.windll.user32.SetProcessDPIAware()
    run = Run(root, "offscreen")
    try:
        if not run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"]):
            print("The page did not come up.")
            return False
        time.sleep(3)                           # the page fills in what it was told and paints
        drawn, width, height, shades = take_picture(run.window(), path)
        print("%s: %d x %d, %s" % (path, width, height, "drawn" if drawn and shades > 1 else "EMPTY"))
        return drawn and shades > 1
    finally:
        run.end()


def show(root):
    run = Run(root, "shown")
    print("The shell's window is open, on the fake machine: nothing clicked there changes this PC.\n"
          "Quit it from its own X (Quit Vispora).")
    code = run.process.wait()
    print("Closed, exit code %r. The backend put back: %r" % (code, run.put_back()))
    return code == 0


def main(argv):
    if sys.platform != "win32":
        refuse("this is a Windows program.")
    if ctypes.windll.shell32.IsUserAnAdmin():
        refuse("this terminal runs as administrator. Use an ordinary one.")
    if not os.path.isfile(EXE):
        refuse("%s is missing: build it first (see the top of this file)." % EXE)
    with open(EXE, "rb") as f:
        if b"VISPORA_SHELL_TRY" not in f.read():
            refuse("VisporaShell.exe is the build that is meant to ship (it asks for administrator rights). "
                   "Build it with --no-default-features first.")
    root = tempfile.mkdtemp(prefix="vispora-shell-window-trial-")
    ok = False
    try:
        if not argv:
            ok = checks(root)
        elif argv == ["--time"]:
            ok = times(root)
        elif argv[:1] == ["--picture"] and len(argv) == 2:
            ok = picture(root, os.path.abspath(argv[1]))
        elif argv == ["--show"]:
            ok = show(root)
        else:
            print(__doc__)
            return 2
    finally:
        if ok:
            time.sleep(0.5)
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("Kept for a look: " + root)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
