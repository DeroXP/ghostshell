"""Try the shell's window for real, out of the way, and time it.

    cargo build --release --locked --no-default-features --manifest-path shell_rs/app/Cargo.toml

    python shell_rs/app/try_it.py                  the checks; the window is never shown
    python shell_rs/app/try_it.py --one-file       only the checks of the exe carrying its backend
    python shell_rs/app/try_it.py --one-file --built-for-every-start
                                                   the same, for a trial build that was made with
                                                   --features unpack-at-every-start
    python shell_rs/app/try_it.py --moved          only the checks of what has moved from the backend into the shell
    python shell_rs/app/try_it.py --time           the times, five runs
    python shell_rs/app/try_it.py --time --pack P  ... and what unpacking the pack P costs, both ways
    python shell_rs/app/try_it.py --time --pack P --carried-by E
                                                   the same, with a trial build E that carries P itself
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

One file: the exe carries its backend as a pack and unpacks it before it
starts it.  A trial gives the exe a pack to unpack (a made-up one for the
checks: a folder of five small files; the real backend's for the times,
with --pack) and starts its stand-in whatever was unpacked.  So the
unpacking, the checking and the taking away are the real ones, with real
files where the real pack is used, and nothing of the real backend is ever
started.

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


def built(name, crate=None):
    """Where cargo put `name`: in the folder CARGO_TARGET_DIR names (the
    build output is kept out of the repository, see README.md), or in the
    target folder of the crate it belongs to (`crate`; this one's own if
    none is named).  Where it would be in the crate's own folder, if it is
    nowhere: that is the place to name when saying that it is missing."""
    folders = [os.path.join(crate or HERE, "target")]
    if os.environ.get("CARGO_TARGET_DIR"):
        folders.insert(0, os.environ["CARGO_TARGET_DIR"])
    for folder in folders:
        path = os.path.join(folder, "release", name)
        if os.path.isfile(path):
            return path
    return path


# The exe is this crate's.  The trial tool, which also makes packs
# (shell_rs/README.md, "One file"), is the core's: built with the core, and
# where each crate builds into its own folder, it is in the core's.
CORE = os.path.normpath(os.path.join(HERE, "..", "core"))
EXE = built("VisporaShell.exe")
PACK_TOOL = built("vispora-shell-try.exe", CORE)
# The graphics card's reader is the core's as well (shell_rs/README.md,
# "What has moved from the backend").  A pack carries it; the exe starts it
# from where the pack was unpacked.
READER = built("VisporaGpuReader.exe", CORE)
READER_IN_A_FOLDER = os.path.join("_internal", "VisporaGpuReader.exe")


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

    def __init__(self, root, window, planted=False, beside=None, folder_in=None, plant=None, pack=None, keep=None, moved=None):
        """`beside`: another run whose folders this one shares, as a second
        person's copy on the same PC shares the place the window's folders
        are made in.  `folder_in`: where this run makes its window's folder,
        in place of its data folder.  `plant`: called with the run before
        the exe is started.  `pack`: what the exe unpacks as its backend
        before it starts the stand-in; `keep`: once per version (True) or at
        every start (False)."""
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
        if pack:
            told["pack"] = pack
        if keep is not None:
            told["keep"] = keep
        if moved is not None:
            told["moved"] = moved
        self.env["VISPORA_SHELL_TRY"] = json.dumps(told)
        self.debug_port = self.planted_profile = self.left_behind = self.not_marked = self.somebody_elses = None
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
            # Both made as the shell makes its own (whose they are, and who
            # gets in).  And one with the same name and the same mark that
            # somebody else made, as any program may in that place.
            self.left_behind = os.path.join(self.data, "Vispora-window-" + "0" * 32)
            self.not_marked = os.path.join(self.data, "Vispora-window-" + "1" * 32)
            self.somebody_elses = os.path.join(self.data, "Vispora-window-" + "2" * 32)
            os.makedirs(self.data, exist_ok=True)
            for folder in (self.left_behind, self.not_marked, self.somebody_elses):
                if folder == self.somebody_elses:
                    os.mkdir(folder)
                else:
                    make_closed_folder(folder)
                os.makedirs(os.path.join(folder, "EBWebView", "Default", "Service Worker"))
                with open(os.path.join(folder, "EBWebView", "Default", "Service Worker", "planted.js"), "w") as f:
                    f.write("// what an earlier run left, or somebody put there")
            for folder in (self.left_behind, self.somebody_elses):
                with open(os.path.join(folder, "in-use"), "w"):
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
        removed, left alone, somebody else's and not touched)."""
        return [tuple(int(n) for n in found) for found in re.findall(
            r"the window's old folders: (\d+) entries looked at in (\d+) ms, "
            r"(\d+) left behind by earlier runs and removed, (\d+) in use and left alone, "
            r"(\d+) not this program's making and not touched", self.log())]

    def unpacked(self):
        """What the shell last said about unpacking its backend: which way
        ("run": for this run; "kept-new": into a folder that stays;
        "kept-found": found there and checked), the folder, and how long."""
        ways = (("run", r"the background program was unpacked for this run: (\d+) files, (\d+) bytes, (\d+) ms, into (.+)"),
                ("kept-new", r"the background program was unpacked into (.+), to stay: (\d+) files, (\d+) bytes, (\d+) ms"),
                ("kept-found", r"the background program is in (.+) from an earlier start, and is what the exe carries: "
                               r"(\d+) files, (\d+) bytes, checked in (\d+) ms"))
        last = {}
        for line in self.log().splitlines():
            for kind, pattern in ways:
                found = re.search(pattern, line)
                if found:
                    parts = found.groups()
                    folder = parts[3] if kind == "run" else parts[0]
                    last = {"kind": kind, "folder": folder.strip(), "ms": int(parts[2] if kind == "run" else parts[3])}
            ready = re.search(r"the background program was ready to be started (\d+) ms after that began", line)
            if ready and last:
                last["ready_ms"] = int(ready.group(1))
        return last

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

    def wait_for_any(self, what, within=90):
        """As `wait_for`, also when the exe has ended."""
        ends = time.time() + within
        while time.time() < ends:
            got = what()
            if got:
                return got
            time.sleep(0.05)
        return what()

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


def make_closed_folder(path):
    """Make the folder `path` as the shell makes its own in a trial: this
    account's, open to this account and to Windows itself, and to nobody
    else, with nothing taken over from the folder above."""
    who = own_account()
    described = "O:%sD:P(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)" % (who, who)

    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(described, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = Attributes(ctypes.sizeof(Attributes), descriptor, 0)
    made, error = kernel.CreateDirectoryW(path, ctypes.byref(attributes)), ctypes.get_last_error()
    kernel.LocalFree(descriptor)
    if not made:
        raise ctypes.WinError(error)


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


def made_up_backend(root, name, more=b"", reader=False, runtime=True):
    """A small folder that stands for a backend, and the pack of it: (the
    pack's file, what the pack tool says about it, the folder).  With
    `reader` the pack also carries the graphics card's reader, as it was
    built beside the pack tool, and the folder has the C runtime the reader
    needs beside it, as the app's own folder has (this PC's copy of it;
    not with `runtime` False)."""
    folder = os.path.join(root, "made-up-" + name, "VisporaBackend")
    os.makedirs(os.path.join(folder, "_internal", "certifi"))
    for path, holds in (("VisporaBackend.exe", b"MZ not really a program" + more),
                        (os.path.join("_internal", "python312.dll"), b"a library, a library, a library. " * 4000),
                        (os.path.join("_internal", "certifi", "cacert.pem"), b"-----BEGIN CERTIFICATE-----\n" * 900),
                        (os.path.join("_internal", "empty.txt"), b""),
                        (os.path.join("_internal", "noise.bin"), os.urandom(5000))):
        with open(os.path.join(folder, path), "wb") as f:
            f.write(holds)
    if reader and runtime:
        shutil.copyfile(os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "vcruntime140.dll"),
                        os.path.join(folder, "_internal", "VCRUNTIME140.dll"))
    pack = os.path.join(root, "made-up-" + name, "backend.pack")
    made = subprocess.run([PACK_TOOL, "--pack", folder, pack, "lzms"] + (["--reader", READER] if reader else []), capture_output=True, text=True,
                          creationflags=NO_WINDOW)
    return pack, (json.loads(made.stdout) if made.returncode == 0 else {}), folder


def files_of(folder, but=()):
    """Every file in `folder` (but the ones named): its path in the folder,
    and what it holds."""
    found = {}
    for where, _, names in os.walk(folder):
        for name in names:
            path = os.path.relpath(os.path.join(where, name), folder).lower()
            if path in but:
                continue
            with open(os.path.join(where, name), "rb") as f:
                found[path] = f.read()
    return found


def same_files(folder, source, but=()):
    """Does `folder` hold exactly what `source` holds (and what is named in `but`)?"""
    try:
        there = files_of(folder, but=tuple(own.lower() for own in but))
    except OSError:
        return False
    # (what is named in `but` has to be there: it is the caller's own)
    return bool(there) and there == files_of(source) and all(os.path.exists(os.path.join(folder, own)) for own in but)


def closed_like_the_windows(folder):
    return who_gets_in(folder, folder + ".who-gets-in.txt") == "D:P(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)" % own_account()


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


TRAY_STATE = """(function () {
    window.pywebview.api.get_api_token().then(function (token) {
        fetch('/api/stand-in/tray-state', { method: 'POST', body: JSON.stringify({ broken: %s }),
            headers: { 'X-Vispora-Token': token, 'Content-Type': 'application/json' } });
    });
})();"""


# What the Monitor page does with the performance sampler, and what it was
# told, said in a call the stand-in backend writes down.
THE_SAMPLER = """(function () {
    window.pywebview.api.get_api_token().then(function (token) {
        var with_it = { 'X-Vispora-Token': token, 'Content-Type': 'application/json' };
        var body = JSON.stringify({ consumer: 'trial' });
        fetch('/api/perf/start', { method: 'POST', body: body, headers: with_it })
            .then(function () { return new Promise(function (go_on) { setTimeout(go_on, 1500); }); })
            .then(function () { return fetch('/api/perf/status', { headers: with_it }); })
            .then(function (answer) { return answer.json(); })
            .then(function (status) {
                var last = status.last ? (status.last.ok ? 'a-reading' : 'no-reading') : 'nothing';
                return fetch('/api/trial/sampler/' + (status.running ? 'running' : 'not-running') + '/' + status.sample_count + '/' + last,
                             { headers: with_it });
            })
            .then(function () { return fetch('/api/perf/score?seconds=30', { headers: with_it }); })
            .then(function (answer) { return answer.json(); })
            .then(function (score) { return fetch('/api/trial/score/' + score.verdict + '/' + score.samples_used, { headers: with_it }); })
            .then(function () { return fetch('/api/perf/stop', { method: 'POST', body: body, headers: with_it }); })
            .then(function () { return fetch('/api/trial/sampler-done', { headers: with_it }); });
    });
})();"""


# The page makes a call and says what came of it in a call of its own, which
# the stand-in backend writes down: /api/trial/<name>/<what was told>.
A_CALL = """(function () {
    window.pywebview.api.get_api_token().then(function (token) {
        var with_it = { 'X-Vispora-Token': token, 'Content-Type': 'application/json' };
        fetch(%(path)s, { method: %(method)s, headers: with_it, body: %(body)s })
            .then(function (answer) { return answer.json(); })
            .then(function (said) { return fetch('/api/trial/' + %(name)s + '/' + [%(tell)s].join('/'), { headers: with_it }); });
    });
})();"""
THE_LAST_SAMPLE = "said.running, said.sample_count, said.last ? said.last.ok : 'nothing', said.last ? said.last.temp : 'nothing'"


def the_page_calls(run, method, path, body=None, tell="said.ok"):
    """Have the app's page make a call, and give back what it told of the
    answer (text, one for each thing asked for), or None."""
    the_page_calls.made += 1
    name = "call-%d" % the_page_calls.made
    run.in_the_page(A_CALL % {"path": json.dumps(path), "method": json.dumps(method), "name": json.dumps(name), "tell": tell,
                              "body": "null" if body is None else json.dumps(json.dumps(body))})
    told = run.wait_for(lambda: [c[3] for c in run.calls() if c[3].startswith("/api/trial/%s/" % name)], 20)
    return told[-1].split("/")[4:] if told else None


the_page_calls.made = 0


def holds(pid, library):
    """Is a library of this name loaded in the process `pid`?  As Windows
    lists it."""
    listed = subprocess.run(["tasklist", "/m", library, "/fi", "PID eq %d" % pid, "/fo", "csv", "/nh"], capture_output=True, text=True,
                            creationflags=NO_WINDOW).stdout
    return '"%d"' % pid in listed


THE_DRIVERS_LIBRARY = "nvml.dll"     # NVIDIA's, which a driver's installer has to be able to replace
THE_CARD = "/api/gpu/oc/live"        # the backend's call for the card's reading


def image_of(pid):
    """The program file of the process `pid`, as Windows has it ("" where
    it cannot be asked: the process is gone)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        name, length = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return name.value if kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)) else ""
    finally:
        kernel32.CloseHandle(process)


def libraries_of(pid, name):
    """The files of the libraries named `name` that are loaded in the
    process `pid`, as Windows has them.  (A read.)"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.K32EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.K32GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(0x0410, False, pid)             # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if not process:
        return []
    try:
        modules, needed = (wintypes.HMODULE * 2048)(), wintypes.DWORD(0)
        if not kernel32.K32EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
            return []
        found = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            file = ctypes.create_unicode_buffer(1024)
            kernel32.K32GetModuleFileNameExW(process, module, file, 1024)
            if os.path.basename(file.value).lower() == name.lower():
                found.append(file.value)
        return found
    finally:
        kernel32.CloseHandle(process)


def readers_of(run):
    """The process numbers of the readers this run's shell has started, in order (its log)."""
    return [int(pid) for pid in re.findall(r"the graphics card's reader was started \(pid (\d+)\)", run.log())]


def the_cards_reader(root, check):
    """The graphics card's own reader: a small program the shell starts
    while a reading is wanted, from the folder its pack was unpacked into,
    and ends when nobody wants one, before the backend hears of a driver's
    install, at the quit, and with a shell that is ended by force.

    It reads this PC's graphics card through the driver's own library, and
    only reads.  Nothing is installed or started: the stand-in backend only
    says that an installer was, or was not."""
    pack, says, source = made_up_backend(root, "with-reader", reader=True)
    check("a pack was made that carries the graphics card's reader, and the C runtime it needs beside it: %r" % ({k: says.get(k) for k in ("files", "reader")},),
          says.get("files") == 7 and says.get("reader") is True)
    its_card = {"temp_c": 71, "gpu_util_pct": 50, "mem_util_pct": 20, "core_mhz": 1800, "mem_mhz": 7000, "power_mw": 123450, "fan_pct": 60}
    library = THE_DRIVERS_LIBRARY

    def asked_for_the_card(run):
        return len([c for c in run.calls() if c[2:] == ("GET", THE_CARD)])

    def a_reader_runs(run):
        """The page wants the sampler: the reader that then runs (None where there is none)."""
        the_page_calls(run, "POST", "/api/stand-in/card", its_card)
        started = the_page_calls(run, "POST", "/api/perf/start", {"consumer": "trial"})
        pid = run.wait_for(lambda: readers_of(run)[-1:], 10)
        time.sleep(1.0)
        return (pid[0] if pid and started == ["true"] else None)

    # A build that is told nothing keeps its backend's folder, and the reader in it.
    run = Run(root, "hidden", pack=pack)
    try:
        up = first_screen(run)
        shell_pid = run.process.pid
        folder = run.unpacked().get("folder", "")
        reader_exe = os.path.join(folder, READER_IN_A_FOLDER)
        check("the window is up, the reader was unpacked with the backend, and no reader runs before anybody wants a reading",
              up and os.path.isfile(reader_exe) and "the graphics card has a reader of its own in that folder" in run.log() and readers_of(run) == [])
        pid = a_reader_runs(run)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        if pid and "gave no reading" in run.log():
            # (no NVIDIA card or driver on this PC: the reader says so and is ended)
            check("this PC's card gives a reader no reading: it was ended, and the samples are the backend's (71 degrees): %r" % (last,),
                  last and last[2:] == ["true", "71"] and not running(pid) and not holds(shell_pid, library))
            quit_by_its_window(run)
            return
        check("the page wants the sampler: a reader runs, and its program is the one in the unpacked folder: %s" % os.path.basename(image_of(pid or 0)),
              pid and running(pid) and os.path.normcase(image_of(pid)) == os.path.normcase(reader_exe))
        check("the C runtime in the reader's process is the copy beside it in that folder, not one from elsewhere on the PC: %s" % (libraries_of(pid or 0, "vcruntime140.dll"),),
              pid and [os.path.normcase(found) for found in libraries_of(pid, "vcruntime140.dll")] == [os.path.normcase(os.path.join(folder, "_internal", "VCRUNTIME140.dll"))])
        check("the graphics driver's library is in the reader's process, and not in the shell's", pid and holds(pid, library) and not holds(shell_pid, library))
        check("the samples are the reader's (this PC's card), and the backend is not asked for the card: %r" % (last,),
              last and last[0] == "true" and last[2] == "true" and asked_for_the_card(run) == 0)

        # The page asks for a driver's install; the backend starts none.
        told = the_page_calls(run, "POST", "/api/driver/install", {})
        seen = the_page_calls(run, "POST", "/api/stand-in/driver-install", {"starts": True}, tell="JSON.stringify(said.readers_when_asked)")
        check("the page asks for a driver's install, which the backend does not start: no reader ran when the backend heard of it, and that one is gone: %r" % (seen,),
              told == ["false"] and seen == ["[[]]"] and pid and not running(pid))
        second = run.wait_for(lambda: [p for p in readers_of(run) if p != pid][-1:], 10)
        check("... and another reader is there for the next readings", second and running(second[0]) and holds(second[0], library))

        # The page asks again; the backend says that an installer was started.
        told = the_page_calls(run, "POST", "/api/driver/install", {})
        seen = the_page_calls(run, "POST", "/api/stand-in/driver-install", {}, tell="JSON.stringify(said.readers_when_asked)")
        time.sleep(1.5)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        check("the page asks again and an installer is started: no reader when the backend heard, none since, and no process of the shell's holds the library",
              told == ["true"] and seen == ["[[],[]]"] and second and not running(second[0]) and len(readers_of(run)) == 2 and not holds(shell_pid, library))
        check("the sampler goes on with the backend's reading meanwhile (71 degrees on its fake machine): %r" % (last,),
              last and last[0] == "true" and last[2:] == ["true", "71"] and asked_for_the_card(run) >= 4)
        check("the shell quits as ever (exit code %r), and the folder that stays is whole, the reader in it" % (quit_by_its_window(run),),
              run.process.returncode == 0 and same_files(folder, source, but=(READER_IN_A_FOLDER,)))
    finally:
        run.end()

    # Unpacked for the run: the page leaves, and the reader goes ten seconds later.
    run = Run(root, "hidden", pack=pack, keep=False)
    try:
        up = first_screen(run)
        folder = run.unpacked().get("folder", "")
        pid = a_reader_runs(run)
        the_page_calls(run, "POST", "/api/perf/stop", {"consumer": "trial"})
        left = time.time()
        time.sleep(5.0)
        still = bool(pid and running(pid))
        gone = run.wait_for(lambda: pid and not running(pid), 12)
        took = time.time() - left
        check("the page leaves: five seconds later the reader is still there, and ten seconds after the last reading it is ended (%.1f s)" % took,
              up and still and gone and 8.5 <= took <= 14.0 and "nobody asked for a reading for 10 s" in run.log())
        # ... and is started again when the page comes back, and ended by the quit.
        again = a_reader_runs(run)
        check("the page comes back and another reader runs; at the quit it is ended first, and the folder for the run goes (exit code %r)"
              % (quit_by_its_window(run),),
              again and again != pid and not running(again) and run.process.returncode == 0 and folder and not os.path.exists(folder)
              and ("the graphics card's reader was ended (pid %d): the app quits" % again) in run.log())
    finally:
        run.end()

    # A pack whose reader does not have its C runtime beside it: the reader
    # is not started (Windows would give it a copy from somewhere else),
    # and the card's reading is the backend's.
    bare, bare_says, _ = made_up_backend(root, "reader-without-runtime", reader=True, runtime=False)
    run = Run(root, "hidden", pack=bare, keep=False)
    try:
        up = first_screen(run)
        the_page_calls(run, "POST", "/api/stand-in/card", its_card)
        the_page_calls(run, "POST", "/api/perf/start", {"consumer": "trial"})
        time.sleep(1.5)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        check("a pack with the reader and without its C runtime: no reader is started, the log says why, and the samples are the backend's: %r" % (last,),
              up and bare_says.get("reader") is True and bare_says.get("files") == 6 and readers_of(run) == []
              and "not the C runtime it needs beside it: the reader is not started" in run.log() and last and last[2:] == ["true", "71"])
        quit_by_its_window(run)
    finally:
        run.end()

    # A shell that is ended by force takes its reader with it.
    run = Run(root, "hidden", pack=pack, keep=False)
    try:
        up = first_screen(run)
        pid = a_reader_runs(run)
        was_there = bool(pid and running(pid))
        run.process.kill()                              # (the trial's own exe, and no other process)
        run.process.wait(30)
        gone = run.wait_for_any(lambda: pid and not running(pid), 5)
        check("a shell that is ended by force leaves no reader behind", up and was_there and gone)
    finally:
        run.end()


def moved(root, check):
    """What has moved from the backend into the shell: the performance
    sampler's calls are answered by the shell and do not reach the backend.
    Each of its samples is the backend's reading of the card (here: what the
    stand-in backend's fake machine says), and the shell's own process never
    opens the graphics driver's library, whatever the page asks for.

    Nothing of this PC's card is read by the trial, and nothing is installed
    or started: the call that would start a driver's installer goes to the
    stand-in backend, on whose fake machine there is no installer."""
    its_card = {"temp_c": 71, "gpu_util_pct": 50, "mem_util_pct": 20, "core_mhz": 1800, "mem_mhz": 7000, "power_mw": 123450, "fan_pct": 60}

    def the_page_uses_the_sampler(run):
        run.in_the_page(THE_SAMPLER)
        done = run.wait_for(lambda: [c for c in run.calls() if c[3] == "/api/trial/sampler-done"], 30)
        calls = run.calls()
        told = [c[3].split("/")[4:] for c in calls if c[3].startswith("/api/trial/sampler/")]
        score = [c[3].split("/")[4:] for c in calls if c[3].startswith("/api/trial/score/")]
        reached = [c[3] for c in calls if c[3].startswith("/api/perf/")]
        return bool(done), (told[0] if told else None), (score[0] if score else None), reached

    def asked_for_the_card(run):
        return len([c for c in run.calls() if c[2:] == ("GET", THE_CARD)])

    run = Run(root, "hidden")
    try:
        up = first_screen(run)
        pid = run.process.pid
        sees = holds(pid, "kernel32.dll") and not holds(pid, "no-such-library.dll")
        before = holds(pid, THE_DRIVERS_LIBRARY)
        check("the window is up; the list of the shell's loaded libraries can be read, and the graphics driver's library is not in it", up and sees and not before)
        time.sleep(1.0)
        check("nobody wants the sampler yet: the backend was not asked for the card", asked_for_the_card(run) == 0)

        # The Monitor page: start, a second and a half, status, score, stop.
        the_page_calls(run, "POST", "/api/stand-in/card", its_card)
        done, told, score, reached = the_page_uses_the_sampler(run)
        asked = asked_for_the_card(run)
        check("the performance sampler's calls are answered by the shell: %r, score %r" % (told, score),
              done and told and told[0] == "running" and 4 <= int(told[1]) <= 12 and told[2] == "a-reading"
              and score and score[0] in ("no-data", "idle", "healthy", "warn", "unstable"))
        check("none of them reached the backend, which was asked for the card's reading instead, once for a sample (%d times)" % asked,
              done and reached == [] and told and int(told[1]) <= asked <= int(told[1]) + 3)
        time.sleep(1.0)
        check("after the page said it leaves, the backend is not asked for the card any more", asked_for_the_card(run) == asked)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        check("the samples are the backend's reading (71 degrees on its fake machine, not this PC's card): %r" % (last,),
              last and last[0] == "false" and last[2:] == ["true", "71"])
        check("the shell's process still does not hold the graphics driver's library", not holds(pid, THE_DRIVERS_LIBRARY))

        # The sampler runs, and the page asks for a driver's install.
        started = the_page_calls(run, "POST", "/api/perf/start", {"consumer": "trial"})
        the_page_calls(run, "POST", "/api/stand-in/card", dict(its_card, temp_c=64))
        told = the_page_calls(run, "POST", "/api/driver/install", {}, tell="said.ok, (said.err || '').indexOf('not downloaded') >= 0")
        install = [c for c in run.calls() if c[2:] == ("POST", "/api/driver/install")]
        check("the call for a driver's install is the backend's as ever (on the fake machine it says there is no installer): %r" % (told,),
              started == ["true"] and told == ["false", "true"] and len(install) == 1 and install[0][1] == 200)
        time.sleep(1.2)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        check("the sampler goes on meanwhile with the backend's reading (64 degrees now): %r" % (last,),
              last and last[0] == "true" and last[2:] == ["true", "64"] and asked_for_the_card(run) >= asked + 4)
        # The backend has no reading (as with no NVIDIA card at all).
        the_page_calls(run, "POST", "/api/stand-in/card", {})
        time.sleep(0.8)
        last = the_page_calls(run, "GET", "/api/perf/status", tell=THE_LAST_SAMPLE)
        check("where the backend has no reading, a sample says so, and the sampler stays the shell's: %r" % (last,),
              last and last[0] == "true" and last[2:] == ["false", "0"] and not [c for c in run.calls() if c[3].startswith("/api/perf/")])
        the_page_calls(run, "POST", "/api/perf/stop", {"consumer": "trial"})
        check("at no time did the shell's process hold the graphics driver's library", not holds(pid, THE_DRIVERS_LIBRARY) and holds(pid, "kernel32.dll"))
        check("the shell quits as ever with a sampler that ran (exit code %r)" % (quit_by_its_window(run),), run.process.returncode == 0)
    finally:
        run.end()

    run = Run(root, "hidden", moved=False)
    try:
        up = first_screen(run)
        done, told, score, reached = the_page_uses_the_sampler(run)
        check("told to leave every call to the backend, the shell passes the sampler's on, and says so in its log: %r" % (reached,),
              up and done and reached == ["/api/perf/start", "/api/perf/status", "/api/perf/score", "/api/perf/stop"]
              and "every call is the backend's" in run.log())
        check("the backend's own sampler is as it was, and the shell does not ask for the card: %r" % (told,),
              done and told and told[0] == "running" and told[2] in ("no-reading", "nothing") and asked_for_the_card(run) == 0)
        quit_by_its_window(run)
    finally:
        run.end()

    the_cards_reader(root, check)


def first_screen(run):
    return run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"])


def ended(run, within=60):
    try:
        return run.process.wait(within)
    except subprocess.TimeoutExpired:
        return "still running"


class NoFurther(Exception):
    """A check failed, and the trial was asked to stop at the first one."""


def checks(root, which=None):
    """Every check, one after the other (or those of `which` alone).  With
    VISPORA_TRY_STOP_AT_FIRST set in the environment, the trial ends at the
    first check that fails (for when a build is broken on purpose, to see
    that the trial notices)."""
    results = []
    stop_at_first = bool(os.environ.get("VISPORA_TRY_STOP_AT_FIRST"))

    def check(what, ok):
        results.append((what, bool(ok)))
        print("  %s  %s" % ("ok    " if ok else "FAILED", plain(what)))
        if not ok and stop_at_first:
            raise NoFurther()

    try:
        (which or every_check)(root, check)
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
              and WINDOW_FOLDER.match(os.path.basename(folder)) and folder not in (run.left_behind, run.not_marked, run.somebody_elses))
        # The window's old folders are looked at once the window is there.
        tidied = run.wait_for(run.tidied, 20)
        check("a folder as a run that was ended from outside leaves it was removed, not used: %r" % (tidied,),
              tidied and tidied[0][2] == 1 and not os.path.exists(run.left_behind))
        check("one without a mark, made a moment ago, was left alone (it could be one that is being made), and not used",
              tidied and tidied[0][3] == 2
              and os.path.isfile(os.path.join(run.not_marked, "EBWebView", "Default", "Service Worker", "planted.js"))
              and os.listdir(run.not_marked) == ["EBWebView"])
        check("one with the same name and the same mark that somebody else made was not touched, and not used",
              tidied and tidied[0][4] == 1 and os.path.isdir(run.somebody_elses)
              and sorted(os.listdir(run.somebody_elses)) == ["EBWebView", "in-use"]
              and os.path.isfile(os.path.join(run.somebody_elses, "EBWebView", "Default", "Service Worker", "planted.js"))
              and who_gets_in(run.somebody_elses, os.path.join(run.root, "somebody-who.txt")) != who_gets_in(run.not_marked, os.path.join(run.root, "ours-who.txt")))
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
                if ("EBWebView" in folders or os.path.basename(where).startswith("Vispora-window-"))
                and where not in (run.not_marked, run.somebody_elses)]
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

        # A backend that is up and gives no usable answer is asked every
        # three seconds like any other, not four times a second.
        run.in_the_page(TRAY_STATE % "true")
        failing = run.wait_for(lambda: [c for c in run.calls() if c[3] == "/api/tray/state" and c[1] == 500], 20)
        time.sleep(4.5)
        asked = [c for c in run.calls() if c[3] == "/api/tray/state" and c[1] == 500]
        check("a backend whose answer is of no use is asked for the tray every three seconds, not four times a second "
              "(%d times in the 4.5 s after the first)" % (len(asked) - 1), failing and 1 <= len(asked) - 1 <= 2)
        run.in_the_page(TRAY_STATE % "false")
        check("and the tray goes on when it answers again",
              run.wait_for(lambda: [c for c in run.calls() if c[3] == "/api/tray/state" and c[1] == 200 and c[0] > asked[-1][0]], 20))

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
    moved(root, check)
    one_file(root, check)


# What a start says it did with the folders that stay and are not its own:
# taken away, left because a copy is using them, left because they are not
# the program's making.
OTHER_VERSIONS = (r"from other versions: (\d+) taken away, (\d+) left because they could not be taken away now "
                  r"\(held open, or something in them is in use\), (\d+) left because they are not this program's making")


def can_be_renamed(folder):
    """Can the folder be given another name (and so be exchanged for
    something else)?  It gets its own name back."""
    try:
        os.rename(folder, folder + "-elsewhere")
    except OSError:
        return False
    os.rename(folder + "-elsewhere", folder)
    return True


def quit_by_its_window(run):
    run.close_the_window()
    return ended(run)


# Was the trial build made for the other way (--features
# unpack-at-every-start)?  Said on the command line: --built-for-every-start.
BUILT_FOR_EVERY_START = False


def one_file(root, check):
    """The exe carries its backend and unpacks it before it starts it: both
    ways of keeping it, and what is done with a folder that does not pass.
    Each run says which way it wants, but for the first: that one says
    nothing, and gets the way the build was made for."""
    pack, says, source = made_up_backend(root, "first")
    check("a pack was made of a made-up backend: %r" % (says,), says.get("files") == 5 and says.get("start") == "VisporaBackend.exe")
    mine = "D:P(A;OICI;FA;;;%s)(A;OICI;FA;;;SY)" % own_account()

    # A build that is told nothing.  The owner's choice is what a build is
    # unless it was made for the other way: unpacked once per version, into
    # a folder that stays.
    told_nothing = Run(root, "hidden", pack=pack)
    try:
        up = first_screen(told_nothing)
        got = told_nothing.unpacked()
        folder = got.get("folder", "")
        if BUILT_FOR_EVERY_START:
            check("told nothing, a build made with --features unpack-at-every-start unpacks for the run: %r" % (got.get("kind"),),
                  up and got.get("kind") == "run" and re.match(r"^Vispora-backend-run-[0-9a-f]{32}$", os.path.basename(folder)))
            check("... and at its quit the folder goes (exit code %r)" % (quit_by_its_window(told_nothing),),
                  told_nothing.process.returncode == 0 and folder and not os.path.exists(folder))
        else:
            check("told nothing, the build keeps its backend: unpacked into a folder that stays (a build made with --features "
                  "unpack-at-every-start is tried with --built-for-every-start): %r" % (got.get("kind"),),
                  up and got.get("kind") == "kept-new" and os.path.basename(folder) == says.get("kept_as"))
            check("... and after its quit the folder is still there (exit code %r)" % (quit_by_its_window(told_nothing),),
                  told_nothing.process.returncode == 0 and os.path.isdir(folder) and same_files(folder, source))
    finally:
        told_nothing.end()

    # Unpacked at every start, into a folder for that run.
    run = Run(root, "hidden", pack=pack, keep=False)
    try:
        check("(unpacked at every start) the page came up: the backend was started once it was unpacked", first_screen(run))
        got = run.unpacked()
        folder = got.get("folder", "")
        check("the backend was unpacked for this run, into a folder of this run's own: %s" % os.path.basename(folder),
              got.get("kind") == "run" and os.path.dirname(folder) == run.data and re.match(r"^Vispora-backend-run-[0-9a-f]{32}$", os.path.basename(folder)))
        check("the folder holds what the exe carries and the run's mark, and nothing else", same_files(folder, source, but=("in-use",)))
        check("it is closed to everybody but the trial's own account and Windows itself, and marked as in use",
              who_gets_in(folder, os.path.join(run.root, "backend-who.txt")) == mine and not can_open(os.path.join(folder, "in-use")))
        check("at the quit the folder goes (exit code %r)" % (quit_by_its_window(run),), run.process.returncode == 0 and not os.path.exists(folder))
    finally:
        run.end()

    # A run that Windows ended leaves its folder; the next start takes it away.
    run = Run(root, "hidden", pack=pack, keep=False)
    after = None
    try:
        first_screen(run)
        left, window_left = run.unpacked().get("folder", ""), run.window_folder()
        run.end_the_session()
        check("a run that Windows ended (exit code %r) leaves its backend's folder behind, with nobody holding its mark" % (ended(run),),
              os.path.isdir(left) and can_open(os.path.join(left, "in-use")))
        time.sleep(3)                               # (WebView2's programs let go of the window's folder)
        # A backend that outlived its shell: a program is still running in
        # the folder, under the backend's name, when the next start comes.
        # (The trial's own tool, told to be there and do nothing.  A file
        # beside it that comes first when the folder is read: taking a
        # folder away stops at what Windows will not let go, not before.)
        its_program, comes_first = os.path.join(left, "VisporaBackend.exe"), os.path.join(left, "0-comes-first.txt")
        shutil.copyfile(PACK_TOOL, its_program)
        with open(comes_first, "w") as f:
            f.write("still needed")
        outlived = subprocess.Popen([its_program, "--only-be-there"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        try:
            meanwhile = Run(root, "hidden", pack=pack, keep=False, beside=run)
            try:
                first_screen(meanwhile)
                tidied = meanwhile.wait_for(lambda: len(meanwhile.tidied()) >= 2 and meanwhile.tidied(), 20)
                check("a start while a program still runs in that folder leaves the folder whole, and takes the window's away: %r"
                      % (tidied and tidied[-1],),
                      tidied and tidied[-1][2] == 1 and not os.path.exists(window_left) and outlived.poll() is None
                      and os.path.isfile(comes_first) and os.path.isfile(os.path.join(left, "in-use"))
                      and os.path.isfile(os.path.join(left, "_internal", "python312.dll")))
                quit_by_its_window(meanwhile)
            finally:
                meanwhile.end()
        finally:
            outlived.stdin.close()
            outlived.wait(20)
        after = Run(root, "hidden", pack=pack, keep=False, beside=run)
        first_screen(after)
        tidied = after.wait_for(lambda: len(after.tidied()) >= 3 and after.tidied(), 20)
        check("once that program has ended, the next start takes the folder away: %r" % (tidied and tidied[-1],),
              tidied and tidied[-1][2] == 1 and not os.path.exists(left))
        check("and has a backend folder of its own", after.unpacked().get("folder") not in ("", None, left) and os.path.isdir(after.unpacked()["folder"]))
        quit_by_its_window(after)
    finally:
        if after:
            after.end()
        run.end()

    # Unpacked once per version, into a folder that stays.
    home = Run(root, "hidden", pack=pack, keep=True)
    try:
        check("(unpacked once per version) the page came up", first_screen(home))
        got = home.unpacked()
        kept = got.get("folder", "")
        check("the first start unpacked it into a folder that stays, named after the version and the pack: %s" % os.path.basename(kept),
              got.get("kind") == "kept-new" and os.path.dirname(kept) == home.data and os.path.basename(kept) == says.get("kept_as"))
        check("the folder holds exactly what the exe carries, and is closed like the window's",
              same_files(kept, source) and who_gets_in(kept, os.path.join(home.root, "kept-who.txt")) == mine)
        check("it stays after the quit (exit code %r), and nothing half made beside it" % (quit_by_its_window(home),),
              home.process.returncode == 0 and os.path.isdir(kept) and not [n for n in os.listdir(home.data) if n.endswith(".making")])
    finally:
        home.end()

    def later(what, keep=True, pack=pack):
        """Another start in the same place; what it said about unpacking, its
        log, and what it said it did with other versions' folders (the log
        is the place's: only what this start added to it)."""
        before = len(re.findall(OTHER_VERSIONS, home.log()))
        run = Run(root, "hidden", pack=pack, keep=keep, beside=home)
        try:
            up = first_screen(run)
            got = run.unpacked()
            others = (run.wait_for(lambda: re.findall(OTHER_VERSIONS, run.log())[before:], 5) or [])
            code = quit_by_its_window(run)
            return bool(up) and code == 0, got, run.log(), others
        finally:
            run.end()

    up, got, log, _ = later("the next start")
    check("the next start finds the folder, holds it against the exe and uses it: nothing is unpacked (checked in %s ms)" % got.get("ms"),
          up and got.get("kind") == "kept-found" and got.get("folder") == kept)

    # The shell's own folder, and no longer what the exe carries (a virus
    # scanner took a file away, say): never put right in place.  It is
    # taken away whole and made again, and the backend is run from that.
    program, too_many = os.path.join(kept, "VisporaBackend.exe"), os.path.join(kept, "_internal", "left-from-before.txt")
    with open(program, "r+b") as f:
        f.write(b"XX")                              # (two other letters, the same length)
    with open(too_many, "w") as f:
        f.write("not the exe's")
    up, got, log, _ = later("a start with a changed program in the folder")
    check("the shell's own folder whose program was changed is taken away whole and made again: %s" % got.get("kind"),
          up and got.get("kind") == "kept-new" and got.get("folder") == kept
          and "did not hold what the exe carries" in log and "taken away to be made again" in log)
    check("what is in it now is what the exe carries, and nothing from before", same_files(kept, source) and not os.path.exists(too_many))

    # Another version: its folder stays, the other one's goes.
    newer, says_newer, source_newer = made_up_backend(root, "newer", more=b", in another version")
    up, got, log, others = later("another version's start", pack=newer)
    kept_newer = got.get("folder", "")
    check("another version unpacks into a folder of its own and takes the other one's away: %r" % (others,),
          up and got.get("kind") == "kept-new" and os.path.basename(kept_newer) == says_newer.get("kept_as") != says.get("kept_as")
          and others == [("1", "0", "0")] and not os.path.exists(kept) and same_files(kept_newer, source_newer))

    # A copy that is running holds its folder: no start takes it away, and
    # nothing can be put in its place.
    open_copy = Run(root, "hidden", pack=newer, keep=True, beside=home)
    try:
        check("(a copy of that version stays open) it runs from the folder that stays",
              first_screen(open_copy) and open_copy.unpacked().get("kind") == "kept-found")
        check("while it runs, its folder cannot be given another name: nothing can be put in its place", not can_be_renamed(kept_newer))
        up, got, log, others = later("a start of the first version meanwhile")
        check("a start of another version meanwhile makes its own folder and leaves that one as it is: %r" % (others,),
              up and got.get("kind") == "kept-new" and got.get("folder") == kept and others == [("0", "1", "0")]
              and same_files(kept_newer, source_newer))
        # The folder a copy runs from is no longer what the exe carries.
        # While that copy runs, nobody takes the folder away under it: a
        # second copy of the version unpacks for its run instead.
        its_program = os.path.join(kept_newer, "VisporaBackend.exe")
        with open(its_program, "r+b") as f:
            f.write(b"XX")
        up, got, log, _ = later("a second copy of that version meanwhile", pack=newer)
        try:
            with open(its_program, "rb") as f:
                begins = f.read(2)
        except OSError:
            begins = None                           # (it was taken away)
        check("a folder that a copy runs from and that is not what the exe carries any more is left as it is: "
              "a second copy unpacks for its run instead",
              up and got.get("kind") == "run" and "could not be taken away to be made again (a program holds the folder open)" in log
              and begins == b"XX")
        check("the open copy's page goes on working, and it quits as ever (exit code %r)" % (quit_by_its_window(open_copy),),
              open_copy.process.returncode == 0)
        check("once it is gone, its folder can be given another name again", can_be_renamed(kept_newer))
    finally:
        open_copy.end()
    # A build that keeps none takes away what a build that kept one left.
    # (The first version's folder went already: the second copy of the
    # other version took it away, as any start does with a version that is
    # not its own and that nobody is using.)
    check("the first version's folder was taken away by that second copy's start", not os.path.exists(kept))
    up, got, log, others = later("a start of a build that keeps none", keep=False, pack=newer)
    check("a build that keeps no folder takes away the one an earlier build kept: %r" % (others,),
          up and got.get("kind") == "run" and others == [("1", "0", "0")] and not os.path.exists(kept_newer)
          and not os.path.exists(kept))

    # Somebody made a folder of that name first, with the right files in it.
    def somebody_was_first(run):
        shutil.copytree(source, os.path.join(run.data, says["kept_as"]))
        shutil.copytree(source, os.path.join(run.data, "Vispora-backend-kept-0.0.0-somebody-elses"))

    run = Run(root, "hidden", pack=pack, keep=True, plant=somebody_was_first)
    try:
        up, got = first_screen(run), run.unpacked()
        theirs = os.path.join(run.data, says["kept_as"])
        check("a folder of the right name that somebody else made is not used, though the right files are in it",
              up and got.get("kind") == "run" and "is not this program's folder" in run.log())
        check("and is not made the program's own in place", same_files(theirs, source)
              and who_gets_in(theirs, os.path.join(run.root, "theirs-who.txt")) != mine)
        others = run.wait_for(lambda: re.findall(OTHER_VERSIONS, run.log()), 5)
        check("a folder of another version's name that somebody else made is not taken away either: %r" % (others,),
              others == [("0", "0", "1")] and same_files(os.path.join(run.data, "Vispora-backend-kept-0.0.0-somebody-elses"), source))
        quit_by_its_window(run)
    finally:
        run.end()

    # What the exe carries cannot be read: said in words of its own.
    spoiled = os.path.join(root, "not-a-pack.pack")
    with open(pack, "rb") as f:
        whole = f.read()
    with open(spoiled, "wb") as f:
        f.write(whole[:len(whole) // 2])
    run = Run(root, "hidden", pack=spoiled, keep=False)
    try:
        code = ended(run, 60)
        said = run.messages()
        check("a backend that cannot be unpacked is not started (exit code 1, got %r), and it is said in words of its own: %r"
              % (code, said.split("\n")[0]),
              code == 1 and said.startswith("Vispora could not get its background program ready.") and "usually fixes this" not in said
              and "backend started" not in run.log())
        check("nothing was left behind for it", not [n for n in os.listdir(run.data) if n.startswith(("Vispora-backend-", "Vispora-window-"))])
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
        entries, took, removed = tidied[-1][:3]
        print("\n  One earlier run's folder (%d files) taken away at the next start, with %d other\n"
              "  files beside it: %d ms for %d entries looked at and %d folder removed (the shell's\n"
              "  clock).  It is done on a thread of its own once the window is there: the window was\n"
              "  on screen after %.0f ms in that run." % (files, beside, took, entries, removed, on_screen))
    else:
        print("\n  The earlier run's folder was NOT taken away: %r" % (tidied,))
    print("\nThe window and the page are the real ones. The backend is the app on the fake machine:\n"
          "what depends on it (the first screen's data, the quit) is a trial's time, not a PC's.")
    return bool(tidied)


def unpacking_times(root, pack, runs=3, carried_by=None):
    """What carrying the backend in the exe costs a start, both ways, with
    the pack `pack` (the real backend's, to mean anything).  Its files are
    unpacked, checked and taken away for real; what is started is the
    stand-in.

    `carried_by`: a trial build that was built with this pack in it (see
    shell_rs/README.md, "One file").  It is given no pack: it unpacks what
    it carries, as the app would."""
    global EXE
    said = subprocess.run([PACK_TOOL, "--pack-says", pack], capture_output=True, text=True, creationflags=NO_WINDOW)
    if said.returncode != 0:
        print("That is not a pack: " + said.stderr.strip())
        return False
    says = json.loads(said.stdout)
    print("\nThe pack: %d files, %.1f MB unpacked, %.1f MB in the exe.\n" % (says["files"], says["bytes"] / 1e6, says["packed"] / 1e6))
    handed = pack
    if carried_by:
        if os.path.basename(carried_by).lower() in ("vispora.exe", "ghostshell.exe"):
            refuse("an exe named after the app is never started by a trial.")
        with open(carried_by, "rb") as f, open(pack, "rb") as p:
            exe, carried = f.read(), p.read()
        if b"VISPORA_SHELL_TRY" not in exe:
            refuse("%s is the build that is meant to ship. It is never started here." % carried_by)
        if carried not in exe:
            refuse("%s does not carry that pack." % carried_by)
        print("The exe carries the pack itself: %.1f MB, of which %.1f MB are the shell.\n" % (len(exe) / 1e6, (len(exe) - len(carried)) / 1e6))
        EXE, handed = carried_by, None

    def one(keep, beside=None):
        run = Run(root, "offscreen", pack=handed, keep=keep, beside=beside)
        try:
            seen = run.wait_for(lambda: run.window(shown=True))
            on_screen = (time.time() - run.began) * 1000 if seen else float("nan")
            screen = first_screen(run)
            got = run.unpacked()
            time.sleep(1.0)
            began = time.time()
            run.close_the_window()
            run.process.wait(120)
            return run, {"window": on_screen, "unpack": float(got.get("ms", "nan")), "ready": float(got.get("ready_ms", "nan")),
                         "screen": screen[0][0] * 1000 if screen else float("nan"), "quit": (time.time() - began) * 1000,
                         "kind": got.get("kind"), "folder": got.get("folder", "")}
        finally:
            run.end()

    def show(name, rows, what):
        def middle(key):
            values = sorted(row[key] for row in rows)
            return "%6.0f (%.0f to %.0f)" % (statistics.median(values), values[0], values[-1])
        print("  %s  [%d start(s), all %s]" % (name, len(rows), "/".join(sorted({str(r["kind"]) for r in rows}))))
        print("      %-58s %s ms" % (what, middle("unpack")))
        print("      %-58s %s ms" % ("the backend could be started, after the window was made", middle("ready")))
        print("      %-58s %s ms" % ("a window on screen", middle("window")))
        print("      %-58s %s ms" % ("the first screen has its data (a trial's backend)", middle("screen")))
        print("      %-58s %s ms" % ("quit: until the shell's process has ended", middle("quit")))

    every = [one(False)[1] for _ in range(runs)]
    show("Unpacked at every start, taken away at the quit", every, "unpacking %.0f MB into a folder for the run" % (says["bytes"] / 1e6))
    home, first = one(True)
    show("Unpacked once per version: the first start", [first], "unpacking %.0f MB into a folder that stays" % (says["bytes"] / 1e6))
    later = [one(True, beside=home)[1] for _ in range(runs)]
    show("Unpacked once per version: every later start", later, "holding the folder against the exe, file by file")
    kept = first["folder"]
    on_disk = sum(os.path.getsize(os.path.join(where, name)) for where, _, names in os.walk(kept) for name in names) if os.path.isdir(kept) else 0
    print("\n  On the PC between runs: nothing (unpacked at every start); %.1f MB in one folder (once per version)." % (on_disk / 1e6))
    print("  The files and the unpacking are the real ones; the program that is started is the stand-in.")
    return all(row["kind"] == "run" for row in every) and first["kind"] == "kept-new" and all(row["kind"] == "kept-found" for row in later)


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
    if "--built-for-every-start" in argv:
        global BUILT_FOR_EVERY_START
        BUILT_FOR_EVERY_START = True
        argv = [word for word in argv if word != "--built-for-every-start"]
    # (the checks of the one file, and its times, make and read packs)
    needs_the_tool = not argv or argv in (["--one-file"], ["--moved"]) or argv[:2] == ["--time", "--pack"]
    if needs_the_tool and not os.path.isfile(PACK_TOOL):
        refuse("%s is missing: build it first (cargo build --release --manifest-path shell_rs/core/Cargo.toml). "
               "It makes the packs that the exe is given to unpack." % PACK_TOOL)
    if needs_the_tool and not os.path.isfile(READER):
        refuse("%s is missing: build it first (cargo build --release --manifest-path shell_rs/core/Cargo.toml). "
               "It is the graphics card's reader, which a pack carries." % READER)
    root = tempfile.mkdtemp(prefix="vispora-shell-window-trial-")
    ok = False
    try:
        if not argv:
            ok = checks(root)
        elif argv == ["--one-file"]:
            ok = checks(root, one_file)
        elif argv == ["--moved"]:
            ok = checks(root, moved)
        elif argv == ["--time"]:
            ok = times(root)
        elif argv[:2] == ["--time", "--pack"] and len(argv) == 3:
            ok = unpacking_times(root, os.path.abspath(argv[2]))
        elif argv[:2] == ["--time", "--pack"] and len(argv) == 5 and argv[3] == "--carried-by":
            ok = unpacking_times(root, os.path.abspath(argv[2]), carried_by=os.path.abspath(argv[4]))
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
