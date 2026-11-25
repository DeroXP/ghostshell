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
    it, so its log and its WebView2 profile land there;
  * the window is hidden for the checks, and for the times and the picture
    it is drawn beyond the edge of the desktop and never takes the keyboard.

With --show the window is a real one in the middle of the screen, on the
fake machine: what is clicked there changes nothing on this PC.  Quit it
from its own X.

Only the build made WITHOUT the `elevated` feature is run (the command
above).  The build that ships asks for administrator rights when it starts;
it is refused here, and so is an elevated terminal.

It is not part of `python tests/run_all.py`.
"""
import ctypes
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
STAND_IN = os.path.join(REPO, "tests", "shell_backend_stand_in.py")
WINDOW_TITLE = "Vispora"
NO_WINDOW = 0x08000000
WM_CLOSE = 0x0010


def built(name):
    outside = os.path.join(os.environ.get("CARGO_TARGET_DIR") or HERE, "release", name)
    return outside if os.path.isfile(outside) else os.path.join(HERE, "target", "release", name)


EXE = built("VisporaShell.exe")


def refuse(why):
    print("Not started: " + why)
    sys.exit(2)


class Run:
    """One start of the exe, with everything it is given."""

    def __init__(self, root, window):
        self.root = tempfile.mkdtemp(prefix="run-", dir=root)
        self.notes = os.path.join(self.root, "put-back.txt")
        self.calls_file = os.path.join(self.root, "calls.txt")
        self.env = dict(os.environ)
        for var, folder in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "Home")):
            self.env[var] = os.path.join(self.root, folder)
            os.makedirs(self.env[var])
        self.env["VISPORA_STAND_IN_NOTES"] = self.notes
        self.env["VISPORA_STAND_IN_CALLS"] = self.calls_file
        self.env["VISPORA_SHELL_TRY"] = json.dumps({
            "tag": "%d%d" % (os.getpid(), int(time.time() * 1000) % 10 ** 9),
            "window": window,
            "backend": {"program": sys.executable, "args": ["-u", STAND_IN], "cwd": REPO},
        })
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
                rows = [line.split() for line in f.read().splitlines()]
        except OSError:
            return []
        return [(float(t) - self.began, int(status), method, path) for t, status, method, path in rows]

    def put_back(self):
        try:
            with open(self.notes, encoding="utf-8") as f:
                return f.read().splitlines()
        except OSError:
            return []

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

    run = Run(root, "hidden")
    try:
        # The page is the app's page, loaded in the real window: it asks the
        # backend for its first screen, with the secret from its address.
        first = run.wait_for(lambda: [c for c in run.page_calls() if c[3] == "/api/dashboard"])
        check("the window loaded the page, and the page got its first screen from the backend", first)
        calls = run.page_calls()
        check("every call of the page was taken (%d calls, none refused)" % len(calls),
              calls and all(status != 403 for _, status, _, _ in calls))
        log = run.log()
        check("one backend was started, with no window shown", log.count("backend started (pid ") == 1
              and run.window(shown=True) is None and run.window(shown=False) is not None)
        backend = run.backend_pid()

        # A second start asks the first for its window and leaves, and starts nothing.
        second = run.again()
        check("a second start leaves at once (exit code 0)", second.wait(30) == 0)
        asked = run.wait_for(lambda: "another start of the app asked for the window" in run.log(), within=10)
        check("the first copy was asked for its window", asked)
        check("no second backend was started", run.log().count("backend started (pid ") == 1)
        shown = run.wait_for(lambda: run.window(shown=True), within=10)
        check("and showed it", shown)

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
        check("the secret is in no line of the log", "#t=" not in log and "token" not in log.lower())
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
            t["quit: until the shell's process has ended"] = (time.time() - began) * 1000
            t["... and the backend's is gone"] = 0.0 if not (backend and running(backend)) else float("nan")
            rows.append(t)
        finally:
            run.end()
    print("%d run(s); the middle value of each, in milliseconds (lowest to highest)\n" % runs)
    for name in rows[0]:
        values = [row[name] for row in rows]
        if name.startswith("..."):
            print("  %-52s %s" % (name, "yes, every time" if all(v == 0.0 for v in values) else "NOT every time"))
        else:
            print("  %-52s %9.0f   (%.0f to %.0f)" % (name, statistics.median(values), min(values), max(values)))
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
