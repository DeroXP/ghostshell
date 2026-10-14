"""The standalone updater (updater/): finding the install, closing the app,
download + SHA-256 check, the exe swap with backup and rollback.

Everything it would touch is faked or redirected: the release server is a
table of responses, tasklist / taskkill / the app launch are recorded, and
the "install" is a folder inside the test sandbox.  Program Files is pointed
at the sandbox too, so the install scan never looks at the real one.
"""
import sandbox as m

import hashlib
import io
import json
import os
import shutil
import sys
import time
import types
import urllib.request

import config

os.environ["ProgramFiles"] = os.path.join(m.ROOT, "pf")
os.environ["ProgramFiles(x86)"] = os.path.join(m.ROOT, "pf86")
for _d in ("pf", "pf86"):
    os.makedirs(os.path.join(m.ROOT, _d), exist_ok=True)

import updater.paths as P                           # noqa: E402
import updater.server as SV                         # noqa: E402
import updater.installer as I                       # noqa: E402
import updater.main as UM                           # noqa: E402

SERVER = "https://vispora.up.railway.app"
OLD = b"old build " * 20000
NEW = b"new build " * 20000                         # (anything under 100 KB is taken for an error page)


# ── the release server ────────────────────────────────────────────────
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

    def publish(self, channel="stable", version="3.6.1", body=NEW, sha=None, base=SERVER):
        meta = {"available": True, "version": version, "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest() if sha is None else sha,
                "download_url": f"{base}/download/{channel}"}
        self.routes[f"{base}/version/{channel}"] = json.dumps(meta).encode()
        self.routes[f"{base}/download/{channel}"] = body


# ── tasklist / taskkill / launching the app ───────────────────────────
class FakeProcesses:
    def __init__(self):
        self.running, self.commands, self.launched, self.polite = {}, [], [], True

    def run(self, args, **kw):
        self.commands.append(list(args))
        out = ""
        if args[0] == "tasklist":
            image = args[2].split(" eq ", 1)[1]
            pids = self.running.get(image.lower(), [])
            out = "\n".join(f'"{image}","{pid}","Console","1","120,000 K"' for pid in pids) or \
                "INFO: No tasks are running which match the specified criteria."
        elif args[0] == "taskkill" and (self.polite or "/F" in args):
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


def setup(mode="update", exe_name="GhostShell.exe", channel="stable", running=()):
    """A fresh install folder and updater state.  Returns the app-exe path."""
    m.fresh()
    os.makedirs(P.UPDATER_DIR, exist_ok=True)
    NET.routes.clear()
    del NET.calls[:], PROC.commands[:], PROC.launched[:]
    PROC.running.clear()
    PROC.polite = True
    I.shutil = shutil
    _n[0] += 1
    folder = os.path.join(m.ROOT, "installs", str(_n[0]))
    target = os.path.join(folder, exe_name)
    if mode == "update":
        os.makedirs(folder)
        with open(target, "wb") as f:
            f.write(OLD)
        PROC.running[exe_name.lower()] = list(running)
    SV._state.update(cancelled=False, error=None, phase="ready", progress=0, channel_check=None)
    SV.init_state(target_dir=folder, target_exe=target, channel=channel, current_version="3.6.0",
                  target_version="", server_url=SERVER, auto_confirm=False, silent=True,
                  updater_version=UM.UPDATER_VERSION, mode=mode, scan_result=None)
    return target


def steps():
    return {s["key"]: s["status"] for s in SV.get_state_snapshot()["steps"]}


def backups(target):
    folder, name = os.path.split(target)
    return sorted(f for f in os.listdir(folder)
                  if f.startswith(name + ".bak.") and f[len(name) + 5:].isdigit())


def read(path):
    with open(path, "rb") as f:
        return f.read()


# ═════════════════════════════════════════════════════════════════════
# Names and folders
# ═════════════════════════════════════════════════════════════════════
def test_updater_is_sandboxed():
    assert P.APPDATA_DIR.startswith(m.ROOT) and P.INSTALL_INFO.startswith(m.ROOT)
    assert P.APPDATA_DIR == config.APPDATA_DIR, "the updater and the app must agree on the data folder"


def test_updater_talks_to_the_same_server_as_the_app():
    assert UM.SERVER_URL_DEFAULT == config.UPDATE_SERVER_URL_DEFAULT == SERVER
    assert config.is_dead_update_server("https://" + UM._DEAD_SERVER_HOST)
    assert [int(x) for x in UM.UPDATER_VERSION.split(".")] >= [1, 4, 4]


def test_an_existing_exe_keeps_its_filename():
    folder = os.path.join(m.ROOT, "pick")
    os.makedirs(folder)
    assert I.pick_app_exe(folder) == os.path.join(folder, "Vispora.exe"), "a fresh install gets the new name"
    open(os.path.join(folder, "GhostShell.exe"), "wb").close()
    assert I.pick_app_exe(folder) == os.path.join(folder, "GhostShell.exe"), \
        "shortcuts, taskbar pins and the autostart task all point at the old name"
    open(os.path.join(folder, "Vispora.exe"), "wb").close()
    assert I.pick_app_exe(folder) == os.path.join(folder, "Vispora.exe")


# ═════════════════════════════════════════════════════════════════════
# Finding the install
# ═════════════════════════════════════════════════════════════════════
def test_install_is_found_through_the_apps_own_record():
    target = setup()
    with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
        json.dump({"install_path": target, "channel": "beta", "version": "3.6.0"}, f)
    r = I.find_existing_install()
    assert (r["found"], r["path"], r["source"], r["channel"], r["version"]) == (True, target, "install_info", "beta", "3.6.0")
    assert r["dir"] == os.path.dirname(target)


def test_a_stale_record_falls_back_to_the_standard_folders():
    setup()
    with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
        json.dump({"install_path": os.path.join(m.ROOT, "moved-away", "Vispora.exe")}, f)
    old_style = os.path.join(os.environ["ProgramFiles"], "GhostShell")       # a pre-rename install
    os.makedirs(old_style)
    open(os.path.join(old_style, "GhostShell.exe"), "wb").close()
    r = I.find_existing_install()
    assert r["found"] and r["source"] == "standard" and r["path"] == os.path.join(old_style, "GhostShell.exe")
    shutil.rmtree(old_style)
    r = I.find_existing_install()
    assert r["found"] is False and r["path"] == ""
    assert any(c.endswith(r"Vispora\Vispora.exe") for c in r["candidates"])
    assert any(c.endswith(r"GhostShell\GhostShell.exe") for c in r["candidates"]), "both names are looked for"
    assert all(c.startswith(m.ROOT) or c.startswith("install_info.json") for c in r["candidates"]), \
        "the scan stayed inside the sandbox"


def test_a_damaged_record_is_not_fatal():
    setup()
    with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert I.find_existing_install()["found"] is False
    assert UM.load_install_info() == {}


def test_default_install_folder_is_only_a_suggestion():
    setup()
    suggested = I.default_install_dir()
    assert suggested == os.path.join(os.environ["ProgramFiles"], "Vispora")
    assert not os.path.exists(suggested), "nothing is created until the install runs"


# ═════════════════════════════════════════════════════════════════════
# Closing the running app
# ═════════════════════════════════════════════════════════════════════
def test_running_copies_are_found_by_the_name_of_the_exe_being_replaced():
    target = setup(running=(111, 222))
    assert I._find_app_pids("GhostShell.exe") == [111, 222]
    assert I._find_app_pids("Vispora.exe") == []
    del PROC.commands[:]
    r = I._close_app(target)
    assert r["ok"] and "gracefully" in r["msg"]
    asked = [c for c in PROC.commands if c[0] == "tasklist"]
    assert all("IMAGENAME eq GhostShell.exe" in c for c in asked), \
        "a process it cannot find is a locked exe the swap would then fail on"
    assert not [c for c in PROC.commands if c[0] == "taskkill" and "/F" in c], "asked politely first, and that was enough"


def test_app_that_is_not_running_skips_the_close_step():
    target = setup()
    assert I._close_app(target) == {"ok": True, "msg": "not running", "skipped": True}
    assert not [c for c in PROC.commands if c[0] == "taskkill"]


def test_app_that_ignores_a_polite_close_is_force_closed():
    target = setup(running=(111,))
    PROC.polite = False
    r = I._close_app(target, timeout_s=0.2)
    assert r == {"ok": True, "msg": "force-closed"}
    assert ["taskkill", "/F", "/PID", "111"] in PROC.commands


# ═════════════════════════════════════════════════════════════════════
# The whole update
# ═════════════════════════════════════════════════════════════════════
def test_update_replaces_the_exe_in_place_and_keeps_one_backup():
    target = setup(running=(111,))
    NET.publish()
    assert I.run_install_pipeline() == 0
    assert read(target) == NEW
    assert os.path.basename(target) == "GhostShell.exe" and not os.path.exists(
        os.path.join(os.path.dirname(target), "Vispora.exe")), "updated under the name it already had"
    assert len(backups(target)) == 1 and read(os.path.join(os.path.dirname(target), backups(target)[0])) == OLD
    assert PROC.launched == [[target]], "the new build is started"
    assert set(steps().values()) == {"done"} and SV.get_state_snapshot()["phase"] == "done"
    info = json.load(open(P.INSTALL_INFO, encoding="utf-8"))
    assert (info["install_path"], info["channel"], info["version"]) == (target, "stable", "3.6.1")
    assert not [f for f in os.listdir(P.UPDATER_DIR) if f.endswith(".part")], "no download left behind"
    assert NET.calls == [f"{SERVER}/version/stable", f"{SERVER}/download/stable"]


def test_beta_channel_is_asked_for_the_beta_build():
    target = setup(channel="beta")
    NET.publish(channel="beta", version="3.7.0-beta.1")
    assert I.run_install_pipeline() == 0
    assert NET.calls[0] == f"{SERVER}/version/beta" and read(target) == NEW
    assert json.load(open(P.INSTALL_INFO, encoding="utf-8"))["version"] == "3.7.0-beta.1"


def test_fresh_install_creates_the_folder_and_closes_nothing():
    target = setup(mode="install", exe_name="Vispora.exe")
    NET.publish()
    assert not os.path.exists(os.path.dirname(target))
    assert I.run_install_pipeline() == 0
    assert read(target) == NEW and backups(target) == []
    assert steps()["close"] == "skipped" and not PROC.commands, "nothing to close: tasklist is not even asked"
    assert PROC.launched == [[target]]


def test_a_download_that_fails_its_hash_check_is_not_installed():
    target = setup()
    NET.publish(sha="0" * 64)
    assert I.run_install_pipeline() == 6
    assert read(target) == OLD and backups(target) == [] and PROC.launched == []
    assert "SHA-256 mismatch" in SV.get_state_snapshot()["error"] and steps()["verify"] == "error"
    assert not [f for f in os.listdir(P.UPDATER_DIR) if f.endswith(".part")], "the bad download is deleted"


def test_a_server_that_sends_no_hash_is_refused():
    target = setup()
    NET.publish(sha="")
    assert I.run_install_pipeline() == 6, "fail closed: a missing hash looks the same as a stripped one"
    assert read(target) == OLD and PROC.launched == []
    assert "refusing to install an unverified binary" in SV.get_state_snapshot()["error"]


def test_an_error_page_is_not_mistaken_for_a_build():
    target = setup()
    NET.publish(body=b"<html>404 Not Found</html>")
    assert I.run_install_pipeline() == 5
    assert read(target) == OLD and "server error page" in SV.get_state_snapshot()["error"]


def test_unreachable_server_changes_nothing():
    target = setup(running=(111,))
    assert I.run_install_pipeline() == 4
    assert read(target) == OLD and backups(target) == [] and PROC.launched == []
    assert steps()["channel"] == "error"


def test_nothing_published_on_the_channel():
    target = setup()
    NET.routes[f"{SERVER}/version/stable"] = json.dumps({"available": False}).encode()
    assert I.run_install_pipeline() == 4 and read(target) == OLD
    assert "no release published" in SV.get_state_snapshot()["error"]


def test_failed_swap_puts_the_old_exe_back():
    target = setup()
    NET.publish()

    class StubbornDisk:
        def __getattr__(self, name):
            return getattr(shutil, name)

        def copy2(self, src, dst):
            if dst == target and src.endswith(".part"):
                raise PermissionError(13, "file is in use by another process")      # antivirus, say
            return shutil.copy2(src, dst)

    I.shutil = StubbornDisk()
    try:
        assert I.run_install_pipeline() == 7
    finally:
        I.shutil = shutil
    assert read(target) == OLD, "rolled back from the backup made a moment earlier"
    assert "rolled_back=True" in SV.get_state_snapshot()["error"] and PROC.launched == []
    assert steps()["install"] == "error"


def test_old_backups_are_pruned_once_a_new_one_exists():
    target = setup()
    NET.publish()
    folder = os.path.dirname(target)
    for name in ("GhostShell.exe.bak.111", "GhostShell.exe.bak.222", "GhostShell.exe.bak.txt", "Other.exe.bak.5",
                 "notes.txt"):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(b"x")
    assert I.run_install_pipeline() == 0
    left = sorted(os.listdir(folder))
    assert len(backups(target)) == 1 and "GhostShell.exe.bak.111" not in left and "GhostShell.exe.bak.222" not in left, \
        "every update used to leave a full copy of the previous exe behind for good"
    assert {"GhostShell.exe.bak.txt", "Other.exe.bak.5", "notes.txt"} <= set(left), "only its own backups are removed"


def test_cancelled_before_it_starts():
    target = setup()
    NET.publish()
    SV._state["cancelled"] = True
    assert I.run_install_pipeline() == 1
    assert read(target) == OLD and NET.calls == [] and PROC.launched == []


def test_missing_target_or_channel_is_reported_not_guessed():
    setup()
    SV._state["channel"] = ""
    assert I.run_install_pipeline() == 2 and "release channel" in SV.get_state_snapshot()["error"]
    setup()
    SV._state["target_exe"] = ""
    assert I.run_install_pipeline() == 2 and "target install path" in SV.get_state_snapshot()["error"]


# ═════════════════════════════════════════════════════════════════════
# Command line, as the app (or an old install) starts it
# ═════════════════════════════════════════════════════════════════════
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


def test_silent_run_with_everything_on_the_command_line():
    target = setup()
    NET.publish(channel="beta")
    assert _run_main("--silent", "--target-exe", target, "--channel", "beta") == 0
    assert read(target) == NEW and SV.get_state_snapshot()["channel"] == "beta"
    assert SV.get_state_snapshot()["updater_version"] == UM.UPDATER_VERSION


def test_an_old_install_passing_the_retired_server_is_pointed_at_the_live_one():
    target = setup()
    NET.publish()
    code = _run_main("--silent", "--target-dir", os.path.dirname(target), "--channel", "stable",
                     "--server", "https://ghostshell-site.up.railway.app/")
    assert code == 0 and read(target) == NEW
    assert NET.calls[0] == f"{SERVER}/version/stable", "the old hostname has been dead since July"
    assert SV.get_state_snapshot()["target_exe"] == target, "the exe in that folder, under the name it has"


def test_without_arguments_the_apps_record_says_what_to_update():
    target = setup()
    NET.publish(channel="beta")
    with open(P.INSTALL_INFO, "w", encoding="utf-8") as f:
        json.dump({"install_path": target, "install_dir": os.path.dirname(target), "channel": "beta",
                   "version": "3.6.0"}, f)
    assert _run_main("--silent") == 0
    assert read(target) == NEW and NET.calls[0] == f"{SERVER}/version/beta"


def test_command_line_defaults():
    args = UM.parse_args([])
    assert args["server"] == SERVER and args["channel"] == "" and not args["silent"] and not args["auto_confirm"]
    assert UM.parse_args(["--auto-confirm", "--channel", "beta"])["auto_confirm"] is True


# ═════════════════════════════════════════════════════════════════════
# Its own window
# ═════════════════════════════════════════════════════════════════════
def test_window_page_and_status():
    setup()
    c = SV.app.test_client()
    page = c.get("/")
    assert page.status_code == 200 and b"Vispora" in page.data and b"GhostShell" not in page.data
    status = c.get("/api/status").get_json()
    assert [s["key"] for s in status["steps"]] == ["close", "channel", "download", "verify", "install", "launch"]
    assert status["updater_version"] == UM.UPDATER_VERSION and status["mode"] == "update"
    for asset in ("/static/app.js", "/static/style.css"):
        assert c.get(asset).status_code == 200, asset


def test_window_can_change_channel_and_folder():
    target = setup()
    c = SV.app.test_client()
    assert c.post("/api/set-channel", json={"channel": "nightly"}).get_json()["ok"] is False
    assert c.post("/api/set-channel", json={"channel": "beta"}).get_json() == {"ok": True, "channel": "beta"}
    assert c.post("/api/set-target-dir", json={"target_dir": ""}).get_json()["ok"] is False
    missing = os.path.join(m.ROOT, "no-such-parent", "Vispora")
    assert c.post("/api/set-target-dir", json={"target_dir": missing}).get_json()["ok"] is False
    assert c.post("/api/set-target-dir", json={"target_dir": os.path.dirname(target)}).get_json()["ok"] is True
    assert SV.get_state_snapshot()["target_exe"] == target
    assert c.post("/api/cancel").get_json()["ok"] and SV.is_cancelled()
