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
    it is drawn beyond the edge of the desktop and never takes the keyboard;
  * what the shell would say in a message box goes into a file instead: a
    box would sit on the screen of whoever is at the PC, and wait.

One thing of a trial does show: the tray icon.  For the seconds a run
lasts there is a second "Vispora" icon in the notification area (where
Windows keeps new icons: behind the arrow).  It is the trial's, on the fake
machine; its menu is never opened by the trial.

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

The tray is tried without a mouse.  Its menu is read as Windows has it (the
words, the ticks, what is greyed), a click and a double click on the icon
are sent as the messages Windows sends for them, and an entry is chosen by
the message Windows sends when one is chosen.  The menu itself is never put
on the screen.

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
WM_COMMAND = 0x0111                 # what a window is sent when an entry of its menu is chosen
WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0203
TRAY_ICON_MESSAGE = 6002            # what the tray icon's window is sent for the mouse (the tray-icon crate's number)
TRAY_WINDOW_CLASS = "tray_icon_app"
MENU_BY_POSITION, MENU_GREYED, MENU_TICKED, MENU_LINE = 0x400, 0x1, 0x8, 0x800
# The tray's menu while Game Mode is off, as today's app shows it (None: a
# line).  tests/test_shell_rs.py holds this list against today's tray.
TODAYS_MENU_WHEN_OFF = ["Open Vispora", None, "○  Game Mode: off", "Game Mode", "Crosshair", None,
                        "Refresh PC now", "Clean RAM now", None, "Quit"]
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

    def __init__(self, root, window, planted=False, beside=None, folder_in=None, plant=None):
        """`beside`: another run whose folders this one shares, as a second
        person's copy on the same PC shares the place the window's folders
        are made in.  `folder_in`: where this run makes its window's folder,
        in place of its data folder.  `plant`: called with the run before
        the exe is started."""
        self.root = tempfile.mkdtemp(prefix="run-", dir=root)
        self.notes = os.path.join(self.root, "put-back.txt")
        self.calls_file = os.path.join(self.root, "calls.txt")
        self.secret_file = os.path.join(self.root, "secret.txt")
        self.messages_file = os.path.join(self.root, "messages.txt")
        self.scripts = os.path.join(self.root, "scripts")
        os.makedirs(self.scripts)
        self.env = dict(os.environ)
        for var, folder in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "Home")):
            self.env[var] = beside.env[var] if beside else os.path.join(self.root, folder)
            os.makedirs(self.env[var], exist_ok=True)
        self.data = os.path.join(self.env["APPDATA"], "Vispora")
        self.env["VISPORA_STAND_IN_NOTES"] = self.notes
        self.env["VISPORA_STAND_IN_CALLS"] = self.calls_file
        self.env["VISPORA_STAND_IN_SECRET"] = self.secret_file
        told = {
            "tag": "%d%d" % (os.getpid(), time.time_ns() % 10 ** 12),
            "window": window,
            "scripts": self.scripts,
            "messages": self.messages_file,
            "backend": {"program": sys.executable, "args": ["-u", STAND_IN], "cwd": REPO},
        }
        if folder_in:
            told["folder_in"] = folder_in
        self.env["VISPORA_SHELL_TRY"] = json.dumps(told)
        self.debug_port = self.planted_profile = self.left_behind = self.not_marked = None
        if planted:
            # What a program without rights could do before the app starts:
            # settings for WebView2 in the person's environment (a way in for
            # a debugger, a profile of its choosing), and folders made to
            # look like the window's.
            self.debug_port = free_port()
            self.planted_profile = os.path.join(self.root, "planted-profile")
            self.env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=%d" % self.debug_port
            self.env["WEBVIEW2_USER_DATA_FOLDER"] = self.planted_profile
            # One as a run that was ended from outside leaves it: with its
            # mark, which nobody holds any more.  And one without a mark,
            # made a moment ago: a folder that is being made looks like that.
            self.left_behind = os.path.join(self.data, "Vispora-window-" + "0" * 32)
            self.not_marked = os.path.join(self.data, "Vispora-window-" + "1" * 32)
            for folder in (self.left_behind, self.not_marked):
                os.makedirs(os.path.join(folder, "EBWebView", "Default", "Service Worker"))
                with open(os.path.join(folder, "EBWebView", "Default", "Service Worker", "planted.js"), "w") as f:
                    f.write("// what an earlier run left, or somebody put there")
            with open(os.path.join(self.left_behind, "in-use"), "w"):
                pass
        if plant:
            plant(self)
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

    def messages(self):
        """What the shell would have said in a message box."""
        try:
            with open(self.messages_file, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def tidied(self):
        """What each start that shares this run's folders found among the
        window's old folders, oldest first: (entries looked at, milliseconds,
        removed, left alone)."""
        return [tuple(int(n) for n in found) for found in re.findall(
            r"the window's old folders: (\d+) entries looked at in (\d+) ms, "
            r"(\d+) left behind by earlier runs and removed, (\d+) in use and left alone", self.log())]

    # ── the tray ──────────────────────────────────────────────────────
    def tray_window(self):
        """The window the tray icon's messages go to."""
        return next((w for w in windows_of(self.process.pid) if class_of(w) == TRAY_WINDOW_CLASS), None)

    def tray_said(self):
        """What the shell says its tray shows, last: (the menu's number in
        Windows, the look)."""
        found = re.findall(r"tray look (\d+) (\{.*\})", self.log())
        return (int(found[-1][0]), json.loads(found[-1][1])) if found else (0, {})

    def tray_shows(self):
        """The lines the shell wrote each time its tray changed."""
        return re.findall(r'the tray shows: (\w+), "(.*)"', self.log())

    def click_the_tray(self, double=False):
        """What Windows sends the icon's window for a click with the left button."""
        window = self.tray_window()
        if window:
            ctypes.windll.user32.PostMessageW(wintypes.HWND(window), TRAY_ICON_MESSAGE, 0,
                                              WM_LBUTTONDBLCLK if double else WM_LBUTTONUP)
        return bool(window)

    def choose(self, words, within=20):
        """Choose the entry of the tray's menu that reads `words`: what
        Windows sends when it is clicked.  False if there is none to click,
        or if the shell did not take it.

        The shell makes its menu again whenever what it shows changes, and
        an entry's number belongs to one making of the menu.  A choice that
        arrives for a menu that has just been replaced names an entry that
        is no more, and nothing happens: the shell's log says whether a
        choice was taken, and one that was not is made again."""
        ends = time.time() + within
        while time.time() < ends:
            number, look = self.tray_said()
            entry = next((e for e in look.get("menu", []) if e["text"] == words and e["kind"] != "line"), None)
            found = [(state, item) for text, state, item in menu_in_windows(number) if text == words]
            if not entry or not found:
                time.sleep(0.2)                     # (the menu is being made again)
                continue
            state, item = found[0]
            if state & (MENU_GREYED | MENU_LINE):
                return False
            taken = {"open": "the tray's menu asked for the window", "quit": "quitting (Quit in the tray's menu)"}.get(
                entry["id"], "the tray's menu: %s," % entry["id"])
            before = self.log().count(taken)
            ctypes.windll.user32.PostMessageW(wintypes.HWND(self.tray_window()), WM_COMMAND, item, 0)
            if self.wait_for(lambda: self.log().count(taken) > before, 3):
                return True
            if self.process.poll() is not None:
                return self.log().count(taken) > before
        return False

    def tray_calls(self, call):
        return [c for c in self.calls() if c[3] == "/api/tray/" + call and c[2] == "POST" and c[1] == 200]

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


def class_of(window):
    name = ctypes.create_unicode_buffer(100)
    ctypes.windll.user32.GetClassNameW(wintypes.HWND(window), name, 100)
    return name.value


def menu_in_windows(number):
    """A menu as Windows has it: (words, state, the entry's own number) from
    the top.  `number` is the menu's handle; Windows answers for a menu of
    another program too."""
    user32 = ctypes.windll.user32
    user32.GetMenuItemCount.argtypes = [wintypes.HANDLE]
    user32.GetMenuStringW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    user32.GetMenuState.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.UINT]
    user32.GetMenuItemID.argtypes, user32.GetMenuItemID.restype = [wintypes.HANDLE, ctypes.c_int], wintypes.UINT
    rows = []
    for at in range(max(user32.GetMenuItemCount(number), 0)):
        words = ctypes.create_unicode_buffer(300)
        user32.GetMenuStringW(number, at, words, 300, MENU_BY_POSITION)
        rows.append((words.value, user32.GetMenuState(number, at, MENU_BY_POSITION), user32.GetMenuItemID(number, at)))
    return rows


def same_menu(in_windows, said):
    """Is the menu Windows has the one the shell says it shows?"""
    if len(in_windows) != len(said) or not said:
        return False
    for (words, state, _), entry in zip(in_windows, said):
        if entry["kind"] == "line":
            if not state & MENU_LINE:
                return False
            continue
        if (words != entry["text"] or bool(state & MENU_GREYED) == entry["enabled"] or state & MENU_LINE
                or bool(state & MENU_TICKED) != entry["ticked"]):
            return False
    return True


def plain(text):
    """`text` with every letter a console in any language can print."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


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


def can_open(path):
    """Can this file be opened at all, by a program that lets everybody
    else read it, write it and delete it meanwhile?  Not while somebody
    holds it open for itself alone."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    read, open_existing, invalid = 0x80000000, 3, wintypes.HANDLE(-1).value
    shared_with_everybody = 7
    handle = kernel32.CreateFileW(path, read, shared_with_everybody, None, open_existing, 0, None)
    if handle == invalid:
        return False
    kernel32.CloseHandle(wintypes.HANDLE(handle))
    return True


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

HIDE = "window.pywebview.api.close_to_tray();"
A_GAME = """(function () {
    window.pywebview.api.get_api_token().then(function (token) {
        fetch('/api/stand-in/game', { method: 'POST', body: JSON.stringify({ name: %s }),
            headers: { 'X-Vispora-Token': token, 'Content-Type': 'application/json' } });
    });
})();"""


def first_screen(run):
    return run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"])


def ended(run, within=60):
    try:
        return run.process.wait(within)
    except subprocess.TimeoutExpired:
        return "still running"


class NoFurther(Exception):
    """A check failed, and the trial was asked to stop at the first one."""


def checks(root):
    """Every check, one after the other.  With VISPORA_TRY_STOP_AT_FIRST set
    in the environment, the trial ends at the first check that fails (for
    when a build is broken on purpose, to see that the trial notices)."""
    results = []
    stop_at_first = bool(os.environ.get("VISPORA_TRY_STOP_AT_FIRST"))

    def check(what, ok):
        results.append((what, bool(ok)))
        print("  %s  %s" % ("ok    " if ok else "FAILED", plain(what)))
        if not ok and stop_at_first:
            raise NoFurther()

    try:
        every_check(root, check)
    except NoFurther:
        print("  (stopped at the first check that failed)")
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed." % (len(results), len(failed)))
    return not failed


def every_check(root, check):
    elsewhere = Elsewhere()
    run = Run(root, "hidden", planted=True)
    other = None
    try:
        # The page is the app's page, loaded in the real window: it asks the
        # backend for its first screen, with the secret from its address.
        first = first_screen(run)
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
        folder = run.window_folder()
        check("the window has a folder of this run's own, and WebView2 is in it: %s" % (folder and os.path.basename(folder)),
              folder and os.path.isdir(os.path.join(folder, "EBWebView")) and os.path.dirname(folder) == run.data
              and WINDOW_FOLDER.match(os.path.basename(folder)) and folder not in (run.left_behind, run.not_marked))
        # The window's old folders are looked at once the window is there.
        tidied = run.wait_for(run.tidied, 20)
        check("a folder as a run that was ended from outside leaves it was removed, not used: %r" % (tidied,),
              tidied and tidied[0][2] == 1 and not os.path.exists(run.left_behind))
        check("one without a mark, made a moment ago, was left alone (it could be one that is being made), and not used",
              tidied and tidied[0][3] == 2
              and os.path.isfile(os.path.join(run.not_marked, "EBWebView", "Default", "Service Worker", "planted.js"))
              and os.listdir(run.not_marked) == ["EBWebView"])
        check("this run's folder is marked as in use: nobody else can open the mark", folder and not can_open(os.path.join(folder, "in-use")))

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

        # Two people signed in at one PC each run a copy, and both copies'
        # windows have their folders in the same place.  The second copy's
        # start must not take the first one's folder away under it.
        # (a file of the trial's own in the first copy's folder, named so
        # that it is the first thing anybody clearing the folder would meet)
        own_file = os.path.join(folder, "!a-file-of-the-trial.txt")
        with open(own_file, "w") as f:
            f.write("still here")
        other = Run(root, "hidden", beside=run)
        check("(a copy in another session, same place for the folders) its page came up", first_screen(other))
        theirs = other.window_folder()
        both = run.wait_for(lambda: len(run.tidied()) >= 2 and run.tidied(), 20)
        check("its start left the first copy's folder alone, and the one that may be being made: %r" % (both and both[1],),
              both and both[1][2] == 0 and both[1][3] == 3 and theirs != folder
              and os.path.isdir(os.path.join(folder, "EBWebView")) and os.path.isdir(run.not_marked))
        check("nothing in the first copy's folder was touched", os.path.isfile(own_file))
        run.in_the_page("(function () {" + SAY + "say('still-here-beside-a-second-copy'); })();")
        check("and the first copy's page goes on working", run.wait_for(lambda: run.said("still-here-beside-a-second-copy"), 20))
        other.close_the_window()
        check("the second copy quits and takes its own folder with it, and only that",
              ended(other) == 0 and theirs and not os.path.exists(theirs) and os.path.isdir(os.path.join(folder, "EBWebView")))

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
        code = ended(run)
        check("the shell ends by itself, exit code 0 (got %r)" % (code,), code == 0)
        check("the backend put the system back before it went: %r" % (run.put_back(),),
              run.put_back() == ["put back: user quit"])
        check("the backend's process is gone", backend and not running(backend))
        check("in %.1f s" % (time.time() - began), time.time() - began < 30)
        log = run.log()
        check("the secret is in no line of the log", "#t=" not in log and "token" not in log.lower() and secret not in log)
        left = [os.path.relpath(where, run.root) for where, folders, _ in os.walk(run.root)
                if ("EBWebView" in folders or os.path.basename(where).startswith("Vispora-window-")) and where != run.not_marked]
        check("the window's folder is gone after the run, and nothing of WebView2 is left anywhere else: %r" % (left,),
              folder and not os.path.exists(folder) and not left)
        found, read, _ = files_with(run.root, secret, but=(run.secret_file,))
        check("and the secret is in no file the run left (%d files): %r" % (read, found), read > 0 and not found)
        check("nothing was to be said in a message box", run.messages() == "")
    finally:
        if other:
            other.end()
        run.end()
        elsewhere.close()

    # The tray: its icon, its menu as Windows has it, a click and a double
    # click, and each entry.  Then Windows ends the session while the window
    # is hidden in the tray: the quit must still be run.
    run = Run(root, "hidden")
    try:
        check("(the tray's run) the page came up", first_screen(run))
        has_the_secret = time.time()
        again = run.window_folder()
        check("in another folder than the first run's", again and folder and again != folder and os.path.isdir(again))
        backend = run.backend_pid()
        check("the tray icon is there", run.tray_window())
        # Once the backend has said what is so, every entry can be clicked.
        said = run.wait_for(lambda: [e for e in run.tray_said()[1].get("menu", []) if e["id"] == "game-mode" and e["enabled"]], 20)
        number, look = run.tray_said()
        in_windows = menu_in_windows(number)
        check("its menu, as Windows has it, is today's menu: %r" % ([w or None for w, _, _ in in_windows],),
              said and [None if state & MENU_LINE else words for words, state, _ in in_windows] == TODAYS_MENU_WHEN_OFF)
        check("and is what the shell says it shows (ticks and greyed entries too); the status line can only be read",
              same_menu(in_windows, look.get("menu", [])) and in_windows and in_windows[2][1] & MENU_GREYED
              and not any(state & MENU_GREYED for words, state, _ in in_windows if words in ("Open Vispora", "Game Mode", "Quit")))
        check("the icon is the grey one and its tooltip the app's name", run.tray_shows()[-1:] == [("idle", "Vispora")])

        # The left button brings the window back; it does not open the menu.
        check("the window is hidden to begin with", run.window(shown=True) is None)
        check("a click on the tray icon is taken", run.click_the_tray())
        check("and brings the window back",
              run.wait_for(lambda: run.window(shown=True), 10) and "a click on the tray icon asked for the window" in run.log())
        run.in_the_page(HIDE)
        check("the page hides the window to the tray", run.wait_for(lambda: run.window(shown=True) is None, 10))
        run.click_the_tray(double=True)
        check("a double click brings it back as well",
              run.wait_for(lambda: run.window(shown=True), 10) and "a double click on the tray icon asked for the window" in run.log())
        run.in_the_page(HIDE)
        run.wait_for(lambda: run.window(shown=True) is None, 10)
        check("so does \"Open Vispora\" in the menu", run.choose("Open Vispora") and run.wait_for(lambda: run.window(shown=True), 10)
              and "the tray's menu asked for the window" in run.log())

        # The entries the backend carries out.
        check("\"Game Mode\" is carried out by the backend", run.choose("Game Mode") and run.wait_for(lambda: run.tray_calls("game-mode"), 20))
        armed = run.wait_for(lambda: run.tray_shows()[-1:] == [("armed", "Vispora: game mode armed")], 20)
        number, look = run.tray_said()
        in_windows = menu_in_windows(number)
        check("the icon, the tooltip and the menu say that Game Mode is on, with a tick: %r" % (run.tray_shows()[-1:],),
              armed and same_menu(in_windows, look["menu"]) and in_windows[2][0] == "●  Game Mode: armed"
              and [state & MENU_TICKED for words, state, _ in in_windows if words == "Game Mode"] == [MENU_TICKED])
        run.in_the_page(A_GAME % json.dumps("Trial Game"))
        playing = run.wait_for(lambda: run.tray_shows()[-1:] == [("playing", "Vispora: playing Trial Game")], 20)
        number, look = run.tray_said()
        in_windows = menu_in_windows(number)
        check("a game is running: the icon with the dot, the game's name in the tooltip and the menu, and \"Force Normal Mode\"",
              playing and same_menu(in_windows, look["menu"])
              and [words for words, _, _ in in_windows][2:5] == ["▶  Trial Game", "Game Mode", "Force Normal Mode"])
        for words, call in (("Crosshair", "crosshair"), ("Refresh PC now", "refresh-pc"), ("Clean RAM now", "clean-ram"),
                            ("Force Normal Mode", "force-normal")):
            check("\"%s\" is carried out by the backend" % words, run.choose(words) and run.wait_for(lambda: run.tray_calls(call), 20))
        check("the status line cannot be chosen", not run.choose("▶  Trial Game"))
        calls = run.page_calls()
        check("none of the tray's calls was refused (%d)" % len([c for c in calls if c[3].startswith("/api/tray/")]),
              all(status == 200 for _, status, _, path in calls if path.startswith("/api/tray/")))

        # Hidden in the tray, and Windows ends the session.
        run.in_the_page(HIDE)
        check("the window is hidden in the tray again", run.wait_for(lambda: run.window(shown=True) is None, 10))
        # (long enough for a window that is not private to have written the
        # secret down: this run's folder is looked into after the run)
        time.sleep(max(0.0, has_the_secret + WRITTEN_DOWN_AFTER_S - time.time()))
        began = time.time()
        told = run.end_the_session()                # what try_it.py sends: WM_ENDSESSION
        check("the program's windows are told that the session ends (%d of them)" % told, told >= 3)
        code = ended(run)
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
        check("and its mark is held by nobody any more: the next start will take the folder away",
              can_open(os.path.join(again, "in-use")))
    finally:
        run.end()

    # "Quit" in the tray's menu goes the way every quit goes.
    run = Run(root, "hidden")
    try:
        check("(a third run) the page came up", first_screen(run))
        run.wait_for(lambda: [e for e in run.tray_said()[1].get("menu", []) if e["id"] == "game-mode" and e["enabled"]], 20)
        folder, backend = run.window_folder(), run.backend_pid()
        began = time.time()
        check("\"Quit\" in the tray's menu is taken", run.choose("Quit"))
        code = ended(run)
        check("the shell ends, exit code 0 (got %r), in %.1f s" % (code, time.time() - began), code == 0)
        check("the backend put the system back before it went, and is gone: %r" % (run.put_back(),),
              run.put_back() == ["put back: user quit"] and backend and not running(backend))
        check("and the window's folder with it", folder and not os.path.exists(folder))
    finally:
        run.end()

    # The window's folder cannot be made: said in words of its own, not as
    # a missing WebView2.  (Where it was to be made is a file here.)
    in_the_way = os.path.join(root, "a-file-not-a-folder")
    with open(in_the_way, "w"):
        pass
    run = Run(root, "hidden", folder_in=os.path.join(in_the_way, "inside"))
    try:
        code = ended(run, 30)
        said = run.messages()
        check("where the window's folder cannot be made, the shell does not start (exit code 1, got %r)" % (code,), code == 1)
        check("and says so in words of its own: %r" % (said.split("\n")[0],),
              said.startswith("Vispora could not make the folder its window keeps its files in")
              and ("The folder is made in %s." % os.path.join(in_the_way, "inside")) in said
              and "usually fixes this" not in said and "Nothing was changed on this PC." in said)
        check("no backend was started for it", "backend started" not in run.log())
    finally:
        run.end()


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
            screen = first_screen(run)
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

    # What taking an earlier run's folder away costs a start, with a few
    # thousand files beside it that are nothing of the app's (the place the
    # app as it ships uses is Windows' own folder for temporary files).
    beside = 5000
    left = Run(root, "offscreen")
    try:
        first_screen(left)
        time.sleep(WRITTEN_DOWN_AFTER_S / 4)    # (WebView2 has made its files)
        left.end_the_session()                  # ended from outside: its folder stays
        left.process.wait(60)
    finally:
        left.end()
    time.sleep(3)

    def many_files(run):
        for n in range(beside):
            with open(os.path.join(run.data, "tmp%05d.tmp" % n), "w") as f:
                f.write("x")

    folder = left.window_folder()
    files = sum(len(names) for _, _, names in os.walk(folder)) if folder and os.path.isdir(folder) else 0
    run = Run(root, "offscreen", beside=left, plant=many_files)
    try:
        seen = run.wait_for(lambda: run.window(shown=True))
        on_screen = (time.time() - run.began) * 1000 if seen else float("nan")
        tidied = run.wait_for(lambda: len(run.tidied()) >= 2 and run.tidied(), 30)
        first_screen(run)
    finally:
        run.end()
    if tidied and folder and not os.path.exists(folder):
        entries, took, removed, _ = tidied[-1]
        print("\n  One earlier run's folder (%d files) taken away at the next start, with %d other\n"
              "  files beside it: %d ms for %d entries looked at and %d folder removed (the shell's\n"
              "  clock).  It is done on a thread of its own once the window is there: the window was\n"
              "  on screen after %.0f ms in that run." % (files, beside, took, entries, removed, on_screen))
    else:
        print("\n  The earlier run's folder was NOT taken away: %r" % (tidied,))
    print("\nThe window and the page are the real ones. The backend is the app on the fake machine:\n"
          "what depends on it (the first screen's data, the quit) is a trial's time, not a PC's.")
    return bool(tidied)


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
