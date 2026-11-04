import sandbox as m

# Robustness of the update and helper-install paths: the exe swap never leaves
# a user without a working exe, an interrupted download is never taken for a
# finished one, failures are not reported as successes, and an error report
# does not carry the Windows user name.
#
# Same fakes as test_updater.py: the release server is a table of responses,
# tasklist / taskkill / launching the app are recorded, and the "install" is a
# folder inside the test sandbox.  Nothing here reaches the network.

import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
import urllib.request

_REAL_PROGRAM_FILES = (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"))
os.environ["ProgramFiles"] = os.path.join(m.ROOT, "pf")
os.environ["ProgramFiles(x86)"] = os.path.join(m.ROOT, "pf86")
for _d in ("pf", "pf86"):
    os.makedirs(os.path.join(m.ROOT, _d), exist_ok=True)

import updater.paths as P                           # noqa: E402
import updater.server as SV                         # noqa: E402
import updater.installer as I                       # noqa: E402
import updater.main as UM                           # noqa: E402
from core import autostart as A                     # noqa: E402
from core import dependencies as D                  # noqa: E402
from core import driver_updater as DU               # noqa: E402
from core import error_reporter as ER               # noqa: E402
from core import updater as U                       # noqa: E402
from core import utils                              # noqa: E402

SERVER = "https://vispora.up.railway.app"
OLD = b"old build " * 20000
NEW = b"new build " * 20000                         # (anything under 100 KB is taken for an error page)


# ── the release server, tasklist / taskkill / launching the app ───────
class _Response:
    def __init__(self, body, headers):
        self._body, self.headers = io.BytesIO(body), headers

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:
    def __init__(self):
        self.routes, self.calls = {}, []

    def urlopen(self, req, timeout=0):
        url = getattr(req, "full_url", req)
        self.calls.append(url)
        if url not in self.routes:
            raise OSError(f"no route to {url}")
        body = self.routes[url]
        return _Response(body, {"Content-Length": str(len(body))})

    def publish(self, channel="stable", version="3.6.1", body=NEW):
        meta = {"available": True, "version": version, "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(), "download_url": f"{SERVER}/download/{channel}"}
        self.routes[f"{SERVER}/version/{channel}"] = json.dumps(meta).encode()
        self.routes[f"{SERVER}/download/{channel}"] = body


class FakeProcesses:
    def __init__(self):
        self.running, self.commands, self.launched = {}, [], []

    def run(self, args, **kw):
        self.commands.append(list(args))
        out = ""
        if args[0] == "tasklist":
            image = args[2].split(" eq ", 1)[1]
            pids = self.running.get(image.lower(), [])
            out = "\n".join(f'"{image}","{pid}","Console","1","120,000 K"' for pid in pids) or \
                "INFO: No tasks are running which match the specified criteria."
        elif args[0] == "taskkill":
            pid = int(args[args.index("/PID") + 1])
            for pids in self.running.values():
                if pid in pids:
                    pids.remove(pid)
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)

    def Popen(self, args, **kw):
        self.launched.append(list(args))
        return types.SimpleNamespace(pid=4321)


NET, PROC = FakeNet(), FakeProcesses()
I.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=NET.urlopen))
I.subprocess = types.SimpleNamespace(run=PROC.run, Popen=PROC.Popen)
I.time = types.SimpleNamespace(time=time.time, sleep=lambda seconds: time.sleep(0.01))
_n = [0]


def setup(running=()):
    """A fresh install folder with the old build in it.  Returns the exe path."""
    m.fresh()
    os.makedirs(P.UPDATER_DIR, exist_ok=True)
    NET.routes.clear()
    del NET.calls[:], PROC.commands[:], PROC.launched[:]
    PROC.running.clear()
    I.shutil = shutil
    _n[0] += 1
    folder = os.path.join(m.ROOT, "installs", str(_n[0]))
    target = os.path.join(folder, "Vispora.exe")
    os.makedirs(folder)
    with open(target, "wb") as f:
        f.write(OLD)
    PROC.running["vispora.exe"] = list(running)
    SV._state.update(cancelled=False, error=None, phase="ready", progress=0, channel_check=None)
    SV.init_state(target_dir=folder, target_exe=target, channel="stable", current_version="3.6.0",
                  target_version="", server_url=SERVER, auto_confirm=True, silent=False,
                  updater_version=UM.UPDATER_VERSION, mode="update", scan_result=None)
    return target


def steps():
    return {s["key"]: s["status"] for s in SV.get_state_snapshot()["steps"]}


def backups(target):
    folder, name = os.path.split(target)
    return sorted(f for f in os.listdir(folder) if re.fullmatch(re.escape(name) + r"\.bak\.\d+", f))


def read(path):
    with open(path, "rb") as f:
        return f.read()


class _SlowSwap:
    """shutil as the installer sees it, holding the copy of the new exe over
    the old one until `release` is set (antivirus scanning the new exe, say).
    By then the old exe has been moved out of the way."""

    def __init__(self, target):
        self.target, self.in_swap, self.release = target, threading.Event(), threading.Event()

    def __getattr__(self, name):
        return getattr(shutil, name)

    def copy2(self, src, dst):
        if dst == self.target and src.endswith(".part"):
            self.in_swap.set()
            self.release.wait(10)
        return shutil.copy2(src, dst)


def _run_main(*argv):
    saved = sys.argv
    sys.argv = ["VisporaUpdater.exe", *argv]
    try:
        UM.main()
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = saved
    return None


# ═════════════════════════════════════════════════════════════════════
# The exe swap
# ═════════════════════════════════════════════════════════════════════
def test_no_backup_means_the_old_exe_is_left_alone():
    target = setup(running=(111,))
    NET.publish()

    class FullDisk:
        def __getattr__(self, name):
            return getattr(shutil, name)

        def copy2(self, src, dst):
            if re.search(r"\.bak\.\d+$", dst):
                with open(dst, "wb") as f:
                    f.write(OLD[:1000])                 # part of it, then the disk is full
                raise OSError(28, "No space left on device")
            if dst == target:
                raise PermissionError(13, "The process cannot access the file")
            return shutil.copy2(src, dst)

    I.shutil = FullDisk()
    try:
        assert I.run_install_pipeline() == 7
    finally:
        I.shutil = shutil
    assert os.path.isfile(target) and read(target) == OLD, \
        "the old exe was deleted with no backup to roll back to, and the new one never arrived"
    assert backups(target) == [], "the partial backup is not left behind"
    assert PROC.launched == [] and steps()["install"] == "error"
    assert "back up" in SV.get_state_snapshot()["error"]


def test_the_close_button_mid_swap_waits_for_the_swap():
    target = setup()
    NET.publish()
    gate, exits = _SlowSwap(target), []

    class Os:                                       # os as updater.server sees it, minus the exit
        def __getattr__(self, name):
            return getattr(os, name)

        def _exit(self, code):
            exits.append((code, SV._install_thread.is_alive(), os.path.isfile(target)))

    I.shutil, SV.os = gate, Os()
    try:
        c = SV.app.test_client()
        assert c.post("/api/start").get_json() == {"ok": True}
        assert gate.in_swap.wait(10), "the pipeline reached the swap"
        assert not os.path.exists(target), "(the old exe is out of the way, the new one not yet in)"
        assert c.post("/api/quit").get_json() == {"ok": True}
        time.sleep(0.8)
        assert exits == [], "the updater exited with neither the old exe nor the new one in place"
        gate.release.set()
        assert m.wait(lambda: exits), "it exits once the swap is done"
    finally:
        gate.release.set()
        I.shutil, SV.os = shutil, os
        SV._install_thread.join(10)
    assert exits == [(0, False, True)] and read(target) == NEW
    assert PROC.launched == [], "the user closed it: the new build is not started behind their back"


def test_closing_the_window_mid_swap_waits_for_the_swap():
    target = setup()
    NET.publish()
    gate, released = _SlowSwap(target), []

    def let_the_swap_finish():
        released.append(time.time())
        gate.release.set()

    class Webview:                                  # the window: Alt+F4 while the swap runs
        @staticmethod
        def create_window(*a, **k):
            return object()

        @staticmethod
        def start(debug=False):
            SV.app.test_client().post("/api/start")     # what the page does for --auto-confirm
            assert gate.in_swap.wait(10)
            threading.Timer(0.5, let_the_swap_finish).start()

    saved = (sys.modules.get("webview"), SV.run_server, UM.socket)
    sys.modules["webview"] = Webview
    SV.run_server = lambda port: None
    UM.socket = types.SimpleNamespace(socket=socket.socket,
                                      create_connection=lambda *a, **k: contextlib.nullcontext())
    I.shutil = gate
    try:
        _run_main("--target-exe", target, "--channel", "stable", "--auto-confirm")
        returned = time.time()
        assert released and returned >= released[0], "the updater exited with the swap half done"
        assert not SV._install_thread.is_alive()
    finally:
        gate.release.set()
        if saved[0] is None:
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = saved[0]
        SV.run_server, UM.socket, I.shutil = saved[1], saved[2], shutil
        SV._install_thread.join(10)
    assert read(target) == NEW


def test_a_new_exe_that_will_not_start_is_rolled_back():
    for case in ("blocked", "quarantined", "restore fails"):
        target = setup()
        NET.publish()
        started = []

        def popen(args, **kw):
            started.append(read(args[0]) if os.path.exists(args[0]) else None)
            if started[-1] == NEW:
                if case == "quarantined":
                    os.remove(args[0])                  # antivirus took the new exe away
                raise OSError(225, "Operation did not complete successfully because the file contains a virus")
            return types.SimpleNamespace(pid=4321)

        class Disk:
            def __getattr__(self, name):
                return getattr(shutil, name)

            def copy2(self, src, dst):
                if case == "restore fails" and re.search(r"\.bak\.\d+$", src):
                    raise PermissionError(13, "Access is denied")
                return shutil.copy2(src, dst)

        I.subprocess, I.shutil = types.SimpleNamespace(run=PROC.run, Popen=popen), Disk()
        try:
            assert I.run_install_pipeline() == 8, case
        finally:
            I.subprocess, I.shutil = types.SimpleNamespace(run=PROC.run, Popen=PROC.Popen), shutil
        err = SV.get_state_snapshot()["error"]
        assert len(backups(target)) == 1, f"{case}: the backup is kept"
        if case == "restore fails":
            assert started == [NEW] and "saved as" in err and backups(target)[0] in err, (case, err)
            continue
        assert os.path.isfile(target) and read(target) == OLD, f"{case}: the previous version is not back"
        assert started == [NEW, OLD], f"{case}: the previous version was not started"
        assert "put back and started" in err, err
        assert steps()["install"] == "error" and steps()["launch"] == "error"


def test_a_cancelled_download_leaves_no_part_file():
    setup()

    class Body:
        headers = {"Content-Length": str(len(NEW))}

        def __init__(self):
            self.reads = 0

        def read(self, n=-1):
            self.reads += 1
            if self.reads == 2:
                SV._state["cancelled"] = True       # the user clicks Cancel mid-download
            return NEW[:65536]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    saved = I.urllib
    I.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
        Request=urllib.request.Request, urlopen=lambda req, timeout=0: Body()))
    try:
        dest = os.path.join(P.UPDATER_DIR, "Vispora-3.6.1.exe")
        assert I._download(f"{SERVER}/download/stable", dest) == {"ok": False, "err": "cancelled"}
    finally:
        I.urllib = saved
    assert not os.path.exists(dest + ".part"), "a cancelled download used to stay on disk for good"


def test_the_local_swap_script_keeps_a_non_ascii_path():
    m.fresh()
    try:
        "\u00f6\u00fc".encode("oem")
    except UnicodeEncodeError:
        raise unittest.SkipTest("this Windows' console code page has no \u00f6 or \u00fc")
    os.makedirs(U.UPDATE_DIR, exist_ok=True)
    new_exe = os.path.join(U.UPDATE_DIR, "Vispora_3.6.8.exe")
    target = "C:\\Users\\J\u00f6rg M\u00fcller\\Desktop\\Vispora.exe"
    r = U._apply_exe_update_via_bat(new_exe, target)
    assert r["ok"] is False, "(the sandbox refuses to start cmd.exe)"
    # cmd.exe reads a batch file in the console (OEM) code page
    text = read(os.path.join(U.UPDATE_DIR, "update.bat")).decode("oem")
    assert f'copy /y "{new_exe}" "{target}"' in text, "the path in the copy command was mangled"
    assert f'start "" "{target}"' in text


# ═════════════════════════════════════════════════════════════════════
# The app's side of an install (core/updater.py)
# ═════════════════════════════════════════════════════════════════════
UPDATER_BUILD = b"MZ updater " * 110000            # over the 1 MB an updater download must have


class _Clock:
    """time as core.updater sees it: it only moves when a test moves it."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        time.sleep(0.005)


@contextlib.contextmanager
def _app_side(served):
    """core.updater as an installed (frozen) copy with a verified update
    staged, auto-install on and the user idle.  The release server answers
    /version/updater for UPDATER_BUILD and sends served["body"] for
    /download/updater; spawns are recorded instead of started, and the exit
    that follows a hand-off is skipped."""
    m.fresh()
    os.makedirs(U.UPDATE_DIR, exist_ok=True)
    os.makedirs(U.UPDATER_DIR, exist_ok=True)
    staged = os.path.join(U.UPDATE_DIR, "Vispora_3.6.8.exe")
    with open(staged, "wb") as f:
        f.write(b"MZ" + NEW)
    clock = _Clock()
    served.update(downloads=[], spawned=[], clock=clock)

    def urlopen(req, timeout=0):
        url = getattr(req, "full_url", req)
        if url.endswith("/version/updater"):
            body = json.dumps({"available": True, "version": "1.4.5",
                               "sha256": hashlib.sha256(UPDATER_BUILD).hexdigest()}).encode()
        elif url.endswith("/download/updater"):
            served["downloads"].append(clock.now)
            body = served["body"]
        else:
            raise OSError(f"no route to {url}")
        return _Response(body, {"Content-Length": str(len(body))})

    def popen(args, creationflags=0, **kw):
        served["spawned"].append((list(args), creationflags))
        if creationflags & 0x01000000 and served.get("no_breakaway"):
            raise PermissionError(5, "Access is denied")        # the job does not allow breakaway
        return types.SimpleNamespace(pid=4321)

    saved = (U.urllib, U.subprocess, U.time, U.is_safe_to_install, U._game_running, U._exit_for_update)
    U.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen))
    U.subprocess = types.SimpleNamespace(Popen=popen, CREATE_NO_WINDOW=0x08000000)
    U.time = clock
    U.is_safe_to_install = lambda: {"safe": True, "reason": "system idle"}
    U._game_running = lambda: ""
    U._exit_for_update = lambda: None
    sys.frozen = True
    U._update_state.update(download_complete=True, update_available=True, pending_install_path=staged,
                           latest_version="3.6.8", latest_url=f"{SERVER}/download/stable.exe",
                           installing=False, install_after_game=False)
    U._updater_install_state.update(running=False, err=None, failures=0, failed_at=0.0)
    assert U.set_settings(auto_install=True)["ok"]
    try:
        yield served
    finally:
        U.urllib, U.subprocess, U.time, U.is_safe_to_install, U._game_running, U._exit_for_update = saved
        del sys.frozen
        U._update_state.update(download_complete=False, update_available=False, pending_install_path=None,
                               installing=False)
        U._updater_install_state.update(running=False, failures=0, failed_at=0.0)


def test_the_updater_hand_off_breaks_away_from_the_apps_job():
    detached = 0x00000008 | 0x00000200                  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with _app_side({"body": UPDATER_BUILD}) as s:
        with open(U.UPDATER_EXE, "wb") as f:
            f.write(UPDATER_BUILD)                      # the updater is on disk already
        for denied in (False, True):
            s["no_breakaway"] = denied
            del s["spawned"][:]
            U._update_state["installing"] = False
            assert U.install_update_now(force=True)["ok"]
            flags = [flag for _, flag in s["spawned"]]
            assert flags == ([detached | 0x01000000, detached] if denied else [detached | 0x01000000]), \
                f"job denies breakaway={denied}: {[hex(f) for f in flags]}"
            assert all(args[0] == U.UPDATER_EXE for args, _ in s["spawned"])


def test_a_failing_updater_download_is_tried_less_and_less_often():
    with _app_side({"body": b"<html>502 Bad Gateway</html>" * 40000}) as s:    # full size, not the build
        clock = s["clock"]

        def ticks(n):
            """The safety-gate loop: one automatic try per interval.  Returns
            the ticks on which the updater was downloaded."""
            tried = []
            for tick in range(1, n + 1):
                clock.now += U.SAFETY_GATE_RECHECK_SEC
                before = len(s["downloads"])
                assert U._try_auto_install(quiet=True)["ok"] is False
                if len(s["downloads"]) > before:
                    tried.append(tick)
            return tried

        assert ticks(449) == [1, 2, 4, 8, 16, 32, 64, 128, 256, 436], \
            "the wait doubles after every failure, up to 6 hours (180 ticks)"
        before = len(s["downloads"])
        r = U.install_update_now()                      # the user clicks Install now
        assert r["ok"] is False and len(s["downloads"]) == before + 1, "a manual install tries at once"

        s["body"] = UPDATER_BUILD
        assert U.install_update_now()["ok"], "and installs once the download works"
        assert U._updater_install_state["failures"] == 0
        assert s["spawned"][-1][0][0] == U.UPDATER_EXE

        os.remove(U.UPDATER_EXE)                        # gone again, and failing again
        s["body"] = b"<html>502 Bad Gateway</html>" * 40000
        U._update_state["installing"] = False
        assert ticks(3) == [1, 2], "after a success the wait starts over"


def test_the_first_updater_download_records_its_version():
    with _app_side({"body": UPDATER_BUILD}) as s:
        U._download_updater_worker()                    # the first-run bootstrap: no version handed in
        assert U.is_updater_installed() and U._read_installed_updater_version() == "1.4.5"
        U._updater_refresh_worker()                     # the next boot's freshness check
        assert m.wait(lambda: not U._updater_install_state["running"])
        assert len(s["downloads"]) == 1, "the next boot downloaded the same updater again"


# ═════════════════════════════════════════════════════════════════════
# The updater window
# ═════════════════════════════════════════════════════════════════════
def _edge():
    for base in _REAL_PROGRAM_FILES:
        exe = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(exe):
            return exe
    return None


def test_install_does_not_start_when_the_typed_folder_is_refused():
    """Loaded in headless Edge with fetch() answered by a stub: the backend
    refuses the folder typed into the install-path box."""
    edge = _edge()
    if not edge:
        raise unittest.SkipTest("Microsoft Edge not found")
    template = io.open(os.path.join(m.REPO, "updater", "templates", "index.html"), encoding="utf-8").read()
    script = pathlib.Path(m.REPO, "updater", "static", "app.js").as_uri()
    stub = ("<script>window.__calls=[];"
            "var STATUS={mode:'install',phase:'ready',target_dir:'C:/Program Files/Vispora',target_exe:'',"
            "channel:'stable',steps:[],updater_version:'1',progress:0,error:null,auto_confirm:false};"
            "window.fetch=function(path,opts){window.__calls.push(((opts&&opts.method)||'GET')+' '+path);"
            "var body=path==='/api/set-target-dir'?{ok:false,err:'parent directory not found: D:/Games'}"
            ":path==='/api/status'?STATUS:{ok:true};"
            "return Promise.resolve({json:function(){return Promise.resolve(body);}});};</script>")
    check = ("<script>var inp=document.getElementById('target-dir-input');"
             "inp.value='D:/Games/Vispora';inp.dataset.userEdited='1';"
             "onStart().then(function(){return pollStatus();}).then(function(){"
             "var box=document.getElementById('error-box'),out=document.createElement('pre');"
             "out.textContent='CALLS='+JSON.stringify(window.__calls.filter(function(c){"
             "return c.indexOf('/api/status')<0;}))+' SHOWN='+(box.style.display!=='none')+' ERROR='+box.textContent;"
             "document.body.appendChild(out);});</script>")
    page = os.path.join(m.ROOT, "updater_page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(template.replace("{{ updater_version }}", "1").replace(
            '<script src="/static/app.js"></script>', stub + f'<script src="{script}"></script>' + check))
    env = dict(os.environ)
    env["ProgramFiles(x86)"], env["ProgramFiles"] = (v or "" for v in _REAL_PROGRAM_FILES)
    try:
        p = m.REAL_POPEN([edge, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
                          f"--user-data-dir={os.path.join(m.ROOT, 'edge-profile')}", "--virtual-time-budget=3000",
                          "--dump-dom", pathlib.Path(page).as_uri()],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        out = p.communicate(timeout=90)[0].decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise unittest.SkipTest(f"headless Edge did not run: {e}")
    found = re.search(r"CALLS=(\[.*?\]) SHOWN=(\w+) ERROR=([^<]*)", out)
    if not found:
        raise unittest.SkipTest("headless Edge printed no result")
    assert json.loads(found.group(1)) == ["POST /api/set-target-dir"], \
        f"the install started anyway, into the folder that was there before: {found.group(1)}"
    assert found.group(2) == "true" and "parent directory not found" in found.group(3), \
        "the status poll wiped the reason the folder was refused"


# ═════════════════════════════════════════════════════════════════════
# GPU driver download
# ═════════════════════════════════════════════════════════════════════
def test_an_interrupted_driver_download_is_not_taken_for_a_finished_one():
    m.fresh()
    os.makedirs(DU.DRIVERS_DIR, exist_ok=True)
    url = ("https://us.download.nvidia.com/Windows/596.36/"
           "596.36-desktop-win10-win11-64bit-international-dch-whql.exe")
    DU._state.update(download_url=url, latest_version="596.36", downloaded_path=None, downloading=False,
                     download_progress=0)
    dest = os.path.join(DU.DRIVERS_DIR, url.rsplit("/", 1)[1])
    saved = DU.subprocess

    def powershell(outcome):
        def run(args, **kw):
            out_file = re.search(r'-OutFile "([^"]+)"', args[-1]).group(1)
            with open(out_file, "wb") as f:
                f.truncate(101 * 1024 * 1024)       # what had arrived: over the "already downloaded" size
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(args, kw.get("timeout"))
            return types.SimpleNamespace(returncode=0 if outcome == "ok" else 1, stdout="",
                                         stderr="The response ended prematurely.")
        return types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                     CREATE_NO_WINDOW=0x08000000)

    try:
        for outcome in ("dropped", "timeout"):
            DU.subprocess = powershell(outcome)
            r = DU.download_driver()
            assert r["ok"] is False, outcome
            assert not os.path.exists(dest), f"{outcome}: a cut-off installer sits where the finished one goes"
            assert os.listdir(DU.DRIVERS_DIR) == [], f"{outcome}: the partial download is left behind"
            assert DU._state["downloading"] is False
        DU.subprocess = powershell("ok")
        r = DU.download_driver()
        assert r["ok"] and r["path"] == dest and os.listdir(DU.DRIVERS_DIR) == [os.path.basename(dest)]
    finally:
        DU.subprocess = saved
        DU._state.update(download_url=None, downloaded_path=None, downloading=False)


# ═════════════════════════════════════════════════════════════════════
# Start with Windows
# ═════════════════════════════════════════════════════════════════════
def test_turning_autostart_off_on_a_german_windows():
    m.fresh()
    registered, locked = {A.TASK_NAME}, set()

    def schtasks(cmd, timeout=20):                  # schtasks / reg as they answer in German
        if cmd[0] == "reg":
            return {"ok": False, "out": "", "rc": 1,
                    "err": "FEHLER: Der angegebene Registrierungsschl\u00fcssel bzw. Wert wurde nicht gefunden."}
        name = cmd[cmd.index("/TN") + 1]
        if name not in registered:
            return {"ok": False, "out": "", "rc": 1,
                    "err": "FEHLER: Das System kann die angegebene Datei nicht finden."}
        if "/Delete" in cmd:
            if name in locked:
                return {"ok": False, "out": "", "rc": 1, "err": "FEHLER: Zugriff verweigert"}
            registered.discard(name)
            return {"ok": True, "err": "", "rc": 0,
                    "out": f'ERFOLGREICH: Die geplante Aufgabe "{name}" wurde erfolgreich gel\u00f6scht.'}
        return {"ok": True, "out": "", "err": "", "rc": 0}

    saved = A._run
    A._run = schtasks
    try:
        r = A.disable()
        assert r["ok"] and registered == set(), \
            f"the pre-rename task was never there, so nothing failed - yet the toggle flipped back on: {r}"
        registered.add(A.TASK_NAME)
        locked.add(A.TASK_NAME)
        r = A.disable()
        assert r["ok"] is False and "Zugriff" in r["err"], "a delete that really failed is still reported"
    finally:
        A._run = saved


# ═════════════════════════════════════════════════════════════════════
# Restart after driver installs
# ═════════════════════════════════════════════════════════════════════
def test_a_refused_restart_is_not_reported_as_started():
    m.fresh()
    D._save_state({"pending_restart": {"set_at": 1.0, "by": ["vigembus"]}})
    answer, ran = [types.SimpleNamespace(returncode=1190, stdout="",
                                         stderr="A system shutdown has already been scheduled.(1190)")], []
    saved = D.subprocess

    def run(args, **kw):
        ran.append(list(args))
        return answer[0]

    D.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    try:
        r = D.request_restart_now()
        assert r["ok"] is False and "already been scheduled" in r["err"], r
        assert D._load_state().get("pending_restart"), "nothing restarts, so the banner has to keep asking"
        answer[0] = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        assert D.request_restart_now() == {"ok": True, "in_seconds": 10}
        assert "pending_restart" not in D._load_state()
        assert ran[-1][:4] == ["shutdown", "/r", "/t", "10"]
    finally:
        D.subprocess = saved


# ═════════════════════════════════════════════════════════════════════
# Error reports
# ═════════════════════════════════════════════════════════════════════
def test_an_error_report_does_not_carry_the_user_name():
    m.fresh()
    name = "J\u00f6rg M\u00fcller"
    profile = "C:\\Users\\" + name
    saved = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = profile
    try:
        path = profile + "\\AppData\\Roaming\\Vispora\\updates\\Vispora_3.6.8.exe"
        log = utils.get_logger("updater")
        log.info(f"Downloading {SERVER}/download/stable.exe -> {path}")
        log.warning(f"could not remove {path!r}")                       # a repr: doubled backslashes
        r = ER.build_report("Vispora error",
                            f"Install failed: {OSError(13, 'Access is denied', path.upper())}",
                            "Traceback:\n  File \"" + path.replace("\\", "/") + "\", line 1",
                            "it broke while I was in " + profile + "\\Documents")
    finally:
        os.environ["USERPROFILE"] = saved
    texts = [r["kind"], r["message"], r["detail"], r["user_note"]] + [e["msg"] for e in r["logs"]]
    leaked = [t for t in texts if name.lower() in t.lower()]
    assert leaked == [], f"sent off the machine with the user name in it: {ascii(leaked)}"
    assert "%USERPROFILE%" in r["message"] and "%USERPROFILE%" in r["detail"]
    assert sum("%USERPROFILE%" in e["msg"] for e in r["logs"]) == 2
    assert sum(name in e["msg"] for e in utils.get_log_buffer()) == 2, "the app's own log view keeps the real path"


def test_an_error_report_names_the_cpu():
    m.fresh()
    key = r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    m.rset(key, "ProcessorNameString", "Intel(R) Core(TM) Ultra 9 285K   ")
    assert ER._build_environment().get("cpu") == "Intel(R) Core(TM) Ultra 9 285K", \
        "wmic.exe is gone from current Windows 11, so the CPU name was never sent"
    m.rset(key, "ProcessorNameString", "X" * 200)
    assert ER._build_environment()["cpu"] == "X" * 80
    m.fresh()
    assert "cpu" not in ER._build_environment(), "no such value: left out, nothing raised"


def test_an_error_report_with_a_broken_character_is_still_built():
    m.fresh()
    # A JS message cut with .slice() in the middle of an emoji: a lone surrogate.
    r = ER.build_report("Vispora error", "Could not save \ud83d", "", "")
    assert re.fullmatch(r"[0-9a-f]{16}", r["hash"])
