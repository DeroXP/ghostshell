"""Try the Rust updater's exe for real, in a folder of its own.

    cargo build --release --locked --no-default-features --manifest-path updater_rs/app/Cargo.toml

    python updater_rs/app/try_it.py                  the checks; no window is shown
    python updater_rs/app/try_it.py --show           the window, to look at and click
    python updater_rs/app/try_it.py --picture x.png  a picture of the window

The cargo tests (updater_rs/core/tests) cover what the updater decides, with
a fake network and fake processes.  This script covers what they cannot: the
exe itself.  It starts the real VisporaUpdater.exe, with its real window, its
real HTTP client and the real file system, and watches it replace an exe.

What it is pointed at is all made up, and nothing of the PC's own is touched:

  * the "install" is a temporary folder, and the app in it is SandboxApp.exe;
  * the "update server" is a listener on 127.0.0.1 that this script starts,
    and it is the only place the updater is told to ask;
  * APPDATA, LOCALAPPDATA, USERPROFILE and Program Files point into the
    temporary folder for the updater, so its log, its install record and its
    WebView2 profile land there;
  * for the checks the window is never shown, and for the picture it is
    drawn beyond the edge of the desktop and does not take the keyboard.

With --show the window is a real one in the middle of the screen: look at
it, drag it by its title bar, press Browse, press Install, close it.  That
run's server has no build to offer, so Install ends with "no release
published" before anything is closed or replaced, whichever folder was
picked.

It is not part of `python tests/run_all.py`: it needs a built exe and
WebView2, and it starts a real program.

It only runs the build made WITHOUT the `elevated` feature (the command
above).  The exe that ships asks for administrator rights and is refused
here, and so is an elevated terminal.

NEVER change APP_NAME to Vispora.exe or GhostShell.exe.  The updater closes
the app it is about to replace by its file name, wherever that app runs
from.  With one of those names this script would close the real, running
Vispora.
"""
import ctypes
import hashlib
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))


def built(crate, name):
    """Where cargo put `name`: the folder CARGO_TARGET_DIR names, when the
    build output is kept out of the repository (README.md), else the crate's
    own target folder."""
    outside = os.path.join(os.environ.get("CARGO_TARGET_DIR") or crate, "release", name)
    return outside if os.path.isfile(outside) else os.path.join(crate, "target", "release", name)


TRIAL_EXE = built(HERE, "VisporaUpdater.exe")
# What is "installed" and what is "downloaded" has to be a real program,
# because the updater starts the new one when it is done.  This one reads its
# standard input, finds nothing there and ends: it opens no file and no
# connection (see updater_rs/core/src/bin/decide.rs).
STAND_IN = built(os.path.join(HERE, "..", "core"), "vispora-updater-decide.exe")
APP_NAME = "SandboxApp.exe"
WINDOW_TITLE = "Vispora Updater"
OLD_VERSION, NEW_VERSION = "1.0.0", "9.9.9"
LIMIT_S = 120
NO_WINDOW = 0x08000000


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def refuse(why):
    print("Not started: " + why)
    sys.exit(2)


class Server:
    """The update server, as far as the updater asks it anything."""

    def __init__(self, new_build, sha=None, offers=True, slow=False):
        self.asked = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.asked.append(self.path)
                if self.path.startswith("/version/") and not offers:
                    body, kind = b'{"available": false}', "application/json"
                elif self.path == "/version/stable":
                    body = json.dumps({
                        "available": True,
                        "version": NEW_VERSION,
                        "size_bytes": len(new_build),
                        "sha256": sha or sha256(new_build),
                        "notes": "A build that exists only in this trial.",
                        "download_url": outer.base + "/download/stable",
                    }).encode()
                    kind = "application/json"
                elif self.path == "/download/stable" and offers:
                    body, kind = new_build, "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if slow and kind != "application/json":
                        # A few seconds for the file, so that something can
                        # happen in the middle of it.
                        for start in range(0, len(body), len(body) // 20):
                            self.wfile.write(body[start:start + len(body) // 20])
                            self.wfile.flush()
                            time.sleep(0.25)
                    else:
                        self.wfile.write(body)
                except OSError:
                    pass  # the updater hung up, which one of the checks is about

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Trial:
    """One run of the exe against a fresh folder."""

    def __init__(self, name, root, old_build, new_build, sha=None, offers=True, slow=False):
        self.name = name
        self.root = os.path.join(root, name.replace(" ", "_"))
        self.install = os.path.join(self.root, "install")
        self.app = os.path.join(self.install, APP_NAME)
        os.makedirs(self.install)
        with open(self.app, "wb") as f:
            f.write(old_build)
        self.server = Server(new_build, sha, offers, slow)
        self.env = dict(os.environ)
        for var, folder in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "Home"),
                            ("ProgramFiles", "ProgramFiles"), ("ProgramFiles(x86)", "ProgramFiles86")):
            self.env[var] = os.path.join(self.root, folder)
            os.makedirs(self.env[var])
        self.env["VISPORA_UPDATER_TRY"] = "hidden"
        self.data = os.path.join(self.env["APPDATA"], "Vispora")
        self.code = None

    def start(self, *extra, arguments=None):
        if arguments is None:
            arguments = ["--target-dir", self.install, "--target-exe", self.app, "--channel", "stable",
                         "--current-version", OLD_VERSION, "--target-version", NEW_VERSION,
                         "--source", "server", "--server", self.server.base]
        return subprocess.Popen([TRIAL_EXE, *arguments, *extra], env=self.env, cwd=self.root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self, *extra, env=None, arguments=None):
        self.env.update(env or {})
        child = self.start(*extra, arguments=arguments)
        try:
            self.code = child.wait(LIMIT_S)
        except subprocess.TimeoutExpired:
            stop(child)
            self.code = "still running after %d s (stopped)" % LIMIT_S
        self.server.close()
        return self

    def installed(self):
        with open(self.app, "rb") as f:
            return f.read()

    def backups(self):
        return [n for n in os.listdir(self.install) if n.startswith(APP_NAME + ".bak.")]

    def log(self):
        try:
            with open(os.path.join(self.data, "updater", "updater.log"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def record(self):
        try:
            with open(os.path.join(self.data, "install_info.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


def stop(child):
    """End the process this script started, and what that one started."""
    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    child.wait()


def stand_in_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + APP_NAME, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return APP_NAME.lower() in out.lower()


# ─── The checks ──────────────────────────────────────────────────────

def checks(root, old_build, new_build):
    results = []

    def check(trial, what, ok):
        results.append((trial.name, what, bool(ok)))

    def updated(trial):
        record = trial.record()
        check(trial, "exit code 0 (got %r)" % (trial.code,), trial.code == 0)
        check(trial, "the app's exe is the new build", trial.installed() == new_build)
        check(trial, "one backup, and it is the old build",
              len(trial.backups()) == 1
              and open(os.path.join(trial.install, trial.backups()[0]), "rb").read() == old_build)
        check(trial, "install_info.json names the new version and this exe",
              record.get("version") == NEW_VERSION and record.get("install_path") == trial.app
              and record.get("channel") == "stable")
        check(trial, "the server was asked for the version, then for the file",
              trial.server.asked[:1] == ["/version/stable"] and trial.server.asked[-1:] == ["/download/stable"])
        check(trial, "nothing was closed by force", "taskkill" not in trial.log())

    t = Trial("with its window", root, old_build, new_build).run("--auto-confirm")
    updated(t)
    check(t, "the page started the install and closed the window",
          "opening the window" in t.log() and "without a window" not in t.log())
    elsewhere = {var: os.listdir(t.env[var]) for var in ("LOCALAPPDATA", "USERPROFILE", "ProgramFiles",
                                                         "ProgramFiles(x86)")}
    check(t, "all it wrote is in the app's data folder and the install (else: %r)" % (
        {k: v for k, v in elsewhere.items() if v},),
          os.listdir(t.env["APPDATA"]) == ["Vispora"] and not any(elsewhere.values())
          and sorted(os.listdir(t.data)) == ["install_info.json", "updater"]
          and os.path.isdir(os.path.join(t.data, "updater", "webview")))

    t = Trial("silent", root, old_build, new_build).run("--silent")
    updated(t)
    check(t, "no window was opened", "running in --silent mode" in t.log()
          and not os.path.isdir(os.path.join(t.data, "updater", "webview")))

    t = Trial("WebView2 missing", root, old_build, new_build)
    t.run("--auto-confirm", env={"WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": os.path.join(t.root, "no-such-folder")})
    updated(t)
    check(t, "the update went through without a window", "installing without a window" in t.log())

    t = Trial("a file that fails its check", root, old_build, new_build, sha="0" * 64).run("--silent")
    check(t, "exit code 6, the file did not verify (got %r)" % (t.code,), t.code == 6)
    check(t, "the app's exe is still the old build", t.installed() == old_build)
    check(t, "no backup was left", t.backups() == [])
    check(t, "install_info.json was not written", t.record() == {})

    t = Trial("the window closed in the middle of a download", root, old_build, new_build, slow=True)
    child = t.start("--auto-confirm")
    deadline = time.time() + 30
    while "/download/stable" not in t.server.asked and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    window = find_window(child.pid, shown=False)
    if window:
        ctypes.windll.user32.PostMessageW(wintypes.HWND(window), 0x0010, 0, 0)  # WM_CLOSE, what Alt+F4 sends
    try:
        t.code = child.wait(30)
    except subprocess.TimeoutExpired:
        stop(child)
        t.code = "still running 30 s after its window was closed (stopped)"
    t.server.close()
    check(t, "it ends by itself, exit code 0 (got %r)" % (t.code,), window and t.code == 0)
    check(t, "the install was cancelled, not cut off", "install cancelled by user" in t.log())
    check(t, "the app's exe is still the old build, with no backup beside it",
          t.installed() == old_build and t.backups() == [])
    check(t, "install_info.json was not written", t.record() == {})

    t = Trial("nothing published", root, old_build, new_build, offers=False).run("--silent")
    check(t, "exit code 4, no build to install (got %r)" % (t.code,), t.code == 4)
    check(t, "the app's exe is still the old build, and nothing was downloaded",
          t.installed() == old_build and "/download/stable" not in t.server.asked)

    t = Trial("a wrong argument", root, old_build, new_build)
    t.run(arguments=["--no-such-argument"])
    check(t, "exit code 2 (got %r)" % (t.code,), t.code == 2)
    check(t, "nothing was asked and nothing changed", t.server.asked == [] and t.installed() == old_build)

    # The new "app" was started by each update and ends by itself.
    deadline = time.time() + 10
    while stand_in_running() and time.time() < deadline:
        time.sleep(0.2)
    results.append(("afterwards", "no %s is left running" % APP_NAME, not stand_in_running()))

    last = None
    for name, what, ok in results:
        if name != last:
            print("\n" + name)
            last = name
        print("  %s  %s" % ("ok    " if ok else "FAILED", what))
    failed = [r for r in results if not r[2]]
    print("\n%d checks, %d failed." % (len(results), len(failed)))
    return not failed


# ─── The window, to look at ──────────────────────────────────────────

def show(root, old_build, new_build):
    t = Trial("by hand", root, old_build, new_build, offers=False)
    del t.env["VISPORA_UPDATER_TRY"]
    print("The updater's window is open. Its server has nothing to offer, so Install\n"
          "ends with \"no release published\" and nothing is closed or replaced.\n"
          "Close the window when you have seen enough.")
    code = t.start().wait()
    t.server.close()
    print("Closed, exit code %r. The made-up app is %s." % (
        code, "untouched" if t.installed() == old_build else "CHANGED, which it should not be"))
    return t.installed() == old_build


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
    drawn = user32.PrintWindow(hwnd, memory, 2)  # 2: with what the graphics card composes (the page)
    # BITMAPINFOHEADER: 32 bits a pixel, top row first.
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


def picture(root, old_build, new_build, path):
    ctypes.windll.user32.SetProcessDPIAware()
    t = Trial("a picture", root, old_build, new_build, offers=False)
    t.env["VISPORA_UPDATER_TRY"] = "offscreen"
    child = t.start()
    try:
        deadline = time.time() + 30
        hwnd = None
        while hwnd is None and time.time() < deadline and child.poll() is None:
            time.sleep(0.25)
            hwnd = find_window(child.pid)
        if hwnd is None:
            print("The window did not come up.")
            return False
        time.sleep(3)  # the page loads, asks for its state and paints
        drawn, width, height, shades = take_picture(hwnd, path)
        print("%s: %d x %d, %s" % (path, width, height, "drawn" if drawn and shades > 1 else "EMPTY"))
        return drawn and shades > 1
    finally:
        stop(child)
        t.server.close()


def main(argv):
    if sys.platform != "win32":
        refuse("this is a Windows program.")
    if ctypes.windll.shell32.IsUserAnAdmin():
        refuse("this terminal runs as administrator. Use an ordinary one.")
    for path, what in ((TRIAL_EXE, "the trial build (see the top of this file)"),
                       (STAND_IN, "updater_rs/core, built (cargo build --release)")):
        if not os.path.isfile(path):
            refuse("%s is missing: it needs %s." % (path, what))
    with open(TRIAL_EXE, "rb") as f:
        exe = f.read()
    if b'level="requireAdministrator"' in exe or b'level="asInvoker"' not in exe:
        refuse("VisporaUpdater.exe is the build that ships (it asks for administrator "
               "rights). Build it with --no-default-features first.")
    if stand_in_running():
        refuse("a program named %s is already running." % APP_NAME)

    with open(STAND_IN, "rb") as f:
        new_build = f.read()
    # The "installed" build: the same program with a few bytes after its end,
    # so that it is another file with another hash.
    old_build = new_build + b"\0an older build\0"
    root = tempfile.mkdtemp(prefix="vispora-updater-trial-")
    ok = False
    try:
        if argv[:1] == ["--show"]:
            ok = show(root, old_build, new_build)
        elif argv[:1] == ["--picture"] and len(argv) == 2:
            ok = picture(root, old_build, new_build, os.path.abspath(argv[1]))
        elif not argv:
            ok = checks(root, old_build, new_build)
        else:
            print(__doc__)
            return 2
    finally:
        if ok:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("Kept for a look: " + root)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
