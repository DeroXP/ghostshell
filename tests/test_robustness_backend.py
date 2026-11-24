import sandbox as m

# app.py lifecycle and core/utils helpers: the local server saying why it
# could not start, the window closed from outside ending like Quit, what Quit
# restores, boot recovery, command output in another code page, saving a file
# another thread is saving or reading, the diagnostics snapshot, and which
# hardware encoders capabilities counts.

import ast
import importlib.util
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest

import config
import core.utils as U

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

HOST = {"Host": f"127.0.0.1:{config.APP_PORT}"}


def _app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def _real_utils():
    """core/utils.py as shipped: the sandbox swaps its command runners for
    fakes, so load a second copy of the file for the real ones."""
    spec = importlib.util.spec_from_file_location("_real_core_utils", os.path.join(m.REPO, "core", "utils.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = logging.getLogger("vispora")
    root.removeHandler(mod._file_handler)           # the first copy already logs
    root.removeHandler(mod._ui_handler)
    return mod


# ── core/utils ────────────────────────────────────────────────────────
def test_command_output_the_ansi_code_page_cannot_decode_does_not_fail_the_call():
    """Console tools print in the OEM code page: "ü" is byte 0x81 in cp850,
    which cp1252 has no character for, and the whole call came back failed
    although the command had worked."""
    real = _real_utils()
    child = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'f\\x81r ok')"]
    saved = subprocess.Popen
    subprocess.Popen = m.REAL_POPEN                 # a child Python, as sandbox.spawn() starts
    try:
        r = real.run_cmd(child, timeout=60)
    finally:
        subprocess.Popen = saved
    assert r["ok"] and r["out"].endswith("r ok"), ascii(r)


def test_powershell_output_keeps_letters_outside_the_console_code_page():
    """PowerShell wrote to the pipe in the OEM code page: a u-umlaut came
    back mangled and Cyrillic as "?" in every PowerShell read."""
    real = _real_utils()
    word = "für Привет"
    saved = subprocess.Popen
    subprocess.Popen = m.REAL_POPEN                 # PowerShell echoing two strings; nothing is changed
    try:
        r = real.run_ps(f'Write-Output "{word}"; @{{name = "{word}"}} | ConvertTo-Json -Compress', timeout=60)
    finally:
        subprocess.Popen = saved
    lines = r["out"].splitlines()
    assert r["ok"] and lines[0] == word and json.loads(lines[1]) == {"name": word}, ascii(r)


def test_two_threads_saving_one_file_each_write_their_own_temp_file():
    """They shared one temp file, so their writes landed in the same file
    and the mix could be what got installed."""
    path = os.path.join(m.ROOT, "atomic_race", "state.json")
    both_written = threading.Barrier(2, timeout=10)
    temps, real_dump, real_replace = [], json.dump, os.replace

    def dump(data, f, **kw):
        real_dump(data, f, **kw)
        if getattr(f, "name", "").startswith(path):
            both_written.wait()                     # both temp files written, neither closed yet

    def replace(src, dst):
        if dst == path:
            temps.append(src)
        return real_replace(src, dst)

    json.dump, os.replace = dump, replace
    saved = []
    try:
        writers = [threading.Thread(target=lambda d=d: saved.append(U.atomic_write_json(path, d)))
                   for d in ({"who": "a", "pad": "x" * 5000}, {"who": "b", "pad": "x"})]
        for t in writers:
            t.start()
        for t in writers:
            t.join(15)
    finally:
        json.dump, os.replace = real_dump, real_replace
    assert len(set(temps)) == 2, temps
    assert saved == [True, True], "the replace that met the other one's should have waited for it"
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["who"] in ("a", "b")


def test_a_save_waits_for_a_reader_that_briefly_has_the_file_open():
    """Windows refuses to replace a file another handle has open; the save
    used to give up at once and the change was lost."""
    path = os.path.join(m.ROOT, "atomic_reader", "state.json")
    assert U.atomic_write_json(path, {"v": 1})
    reader = open(path, encoding="utf-8")           # a thread in the middle of reading it
    threading.Timer(0.15, reader.close).start()
    try:
        assert U.atomic_write_json(path, {"v": 2}), "gave up although the reader let go in time"
    finally:
        reader.close()
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"v": 2}
    assert os.listdir(os.path.dirname(path)) == ["state.json"]


def test_a_save_that_cannot_replace_the_file_gives_up_and_removes_its_temp_file():
    path = os.path.join(m.ROOT, "atomic_held", "state.txt")
    assert U.atomic_write_text(path, "old")
    with open(path, encoding="utf-8"):              # held open for the whole save
        assert U.atomic_write_text(path, "new") is False
    with open(path, encoding="utf-8") as f:
        assert f.read() == "old"
    assert os.listdir(os.path.dirname(path)) == ["state.txt"]


def test_a_save_is_on_the_disk_before_it_replaces_the_file():
    """Renamed before the data reached the disk, a crash or power cut right
    after a save could leave the file zero-filled (the undo records in
    originals.json with it)."""
    path = os.path.join(m.ROOT, "atomic_fsync", "originals.json")
    events, real_fsync, real_replace = [], os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.fstat(fd).st_size))      # flushed first: the size is all there
        real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", os.path.basename(dst)))
        real_replace(src, dst)

    os.fsync, os.replace = fsync, replace
    try:
        assert U.atomic_write_json(path, {"undo": [1, 2, 3]})
        assert U.atomic_write_text(path + ".txt", "hello")
    finally:
        os.fsync, os.replace = real_fsync, real_replace
    assert events == [("fsync", os.path.getsize(path)), ("replace", "originals.json"),
                      ("fsync", 5), ("replace", "originals.json.txt")], events


# ── app.py ────────────────────────────────────────────────────────────
def test_a_server_that_cannot_start_says_why_in_the_log():
    """Werkzeug reports a port it cannot bind on stderr, which the windowed
    exe does not have, and ends the server thread: the window stayed blank
    with nothing in the log."""
    a = _app()
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)      # as a port Windows has reserved
    held.bind(("127.0.0.1", 0))
    held.listen()
    port, saved, ended = held.getsockname()[1], config.APP_PORT, []

    def run():
        try:
            a.start_flask()
        except BaseException as e:                  # noqa: BLE001
            ended.append(e)

    config.APP_PORT = port
    try:
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(15)
    finally:
        config.APP_PORT = saved
        held.close()
    assert ended and isinstance(ended[0], SystemExit), "the port is taken; the server cannot have started"
    said = [e["msg"] for e in U.get_log_buffer() if f"Local server on port {port} stopped" in e["msg"]]
    assert said and "Error(" in said[-1], said


def test_closing_the_window_from_outside_ends_the_process_the_way_quit_does():
    """Alt+F4, the taskbar's Close window and signing out make webview.start()
    return; main() used to just return, skipping the restore of game mode,
    paused programs and the clip recorder."""
    src = inspect.getsource(_app().main)
    after_start = src[src.index("webview.start(debug=False)\n"):src.index("except ImportError:")]
    assert "_restore_system_before_exit(" in after_start and "os._exit(0)" in after_start, after_start


def test_a_failing_boot_recovery_does_not_stop_the_app_starting():
    """The one start-up step main() did not guard: an exception in it ended
    main() before the server, the window or the tray had started."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_app().main)))

    def calls(statements):
        return {getattr(c.func, "attr", "") for s in statements for c in ast.walk(s) if isinstance(c, ast.Call)}

    assert any("check_and_restore_on_boot" in calls(t.body)
               and any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in t.handlers)
               for t in ast.walk(tree) if isinstance(t, ast.Try))


def test_quitting_while_a_restore_is_under_way_does_not_restore_twice():
    """The game has just closed and the monitor is restoring when the app
    quits.  The quit read "game mode on" outside the lock, waited, then
    restored again with no records left and wrote the old baseline back."""
    a = _app()
    gp = a.game_profiles
    m.fresh()
    restores, holding = [], threading.Event()

    def monitor_restoring():                        # holds the lock and clears the flag, as a restore does
        with gp._session_lock:
            holding.set()
            time.sleep(0.3)
            gp._engine["gaming_mode"] = False

    saved = gp.restore_normal_mode
    gp.restore_normal_mode = lambda reason="game exit": restores.append(reason)
    gp._engine["gaming_mode"] = True
    t = threading.Thread(target=monitor_restoring)
    t.start()
    try:
        assert holding.wait(5)
        a._restore_system_before_exit("user quit")
    finally:
        t.join(5)
        gp.restore_normal_mode = saved
        gp._engine["gaming_mode"] = False
    assert restores == [], restores


def test_quitting_mid_game_restores_even_if_the_engine_status_cannot_be_read():
    """get_engine_status() raising used to skip the restore and leave the
    monitor running."""
    a = _app()
    gp = a.game_profiles
    m.fresh()
    restores = []
    saved = gp.restore_normal_mode, gp.get_engine_status
    gp.restore_normal_mode = lambda reason="game exit": restores.append(reason)
    gp.get_engine_status = lambda: {}["no status"]
    gp._engine.update(gaming_mode=True, monitoring=True)
    try:
        a._restore_system_before_exit("user quit")
        monitoring = gp._engine["monitoring"]
    finally:
        gp.restore_normal_mode, gp.get_engine_status = saved
        gp._engine.update(gaming_mode=False, monitoring=False)
    assert restores == ["user quit"] and monitoring is False, (restores, monitoring)


def test_quit_resumes_every_paused_program_and_ends_tournament_mode():
    """Quit only resumed what game mode had paused: programs paused by hand,
    from the Network tab or by Tournament Mode stayed frozen, and Tournament
    Mode's stopped services and muted notifications stayed that way."""
    a = _app()
    m.fresh()
    calls = []

    def tournament_off(reason="manual"):
        calls.append(("tournament off", reason))
        raise OSError("sc.exe went away")           # one step failing must not skip the next

    saved = a.tournament_mode.disable, a.background_pauser.resume_now
    a.tournament_mode.disable = tournament_off
    a.background_pauser.resume_now = lambda reason="manual": calls.append(("resume all", reason)) or {"ok": True}
    try:
        a._restore_system_before_exit("user quit")
    finally:
        a.tournament_mode.disable, a.background_pauser.resume_now = saved
    assert calls == [("tournament off", "user quit"), ("resume all", "user quit")], calls


def test_diagnostics_snapshot_reports_ram_and_drives():
    a = _app()
    m.fresh()

    def answer(command):
        if "Win32_PhysicalMemory" in command:
            return m.R(json.dumps([{"Capacity": 16 * 1024 ** 3, "Speed": 6000, "ConfiguredClockSpeed": 6000}] * 2))
        if "Win32_LogicalDisk" in command:
            return m.R(json.dumps({"DeviceID": "C:", "Size": 2000 * 1024 ** 3, "FreeSpace": 500 * 1024 ** 3}))
        return None

    m.PS_HOOKS.append(answer)
    try:
        r = a.app.test_client().get("/api/diagnostics/snapshot",
                                    headers={**HOST, a._API_TOKEN_HEADER: a._API_TOKEN})
    finally:
        m.PS_HOOKS.remove(answer)
    hw = r.get_json()["hardware"]
    assert hw["ram_gb"] == 32.0, hw
    assert [d["letter"] for d in hw["drives"]] == ["C:"], hw


def test_the_snapshot_does_not_depend_on_the_windows_version_query_answering():
    """platform.platform() and platform.win32_ver() ask WMI, and when that
    fails Python runs `ver`.  The sandbox refuses real processes, so about one
    run of this file in fifteen failed on the route above (seen on 3.12)."""
    import platform
    a = _app()
    m.fresh()
    real_query = getattr(platform, "_wmi_query", None)
    if real_query is None:
        raise unittest.SkipTest("this Python's platform module has no WMI query")
    saved_uname = getattr(platform, "_uname_cache", None)
    saved_platforms = dict(getattr(platform, "_platform_cache", {}))

    def no_answer(*a, **k):
        raise OSError("WMI did not answer")

    platform._wmi_query = no_answer
    platform._uname_cache = None
    getattr(platform, "_platform_cache", {}).clear()
    try:
        assert platform.win32_ver()[1], "read from sys.getwindowsversion(), without a process"
        r = a.app.test_client().get("/api/diagnostics/snapshot",
                                    headers={**HOST, a._API_TOKEN_HEADER: a._API_TOKEN})
        assert r.status_code == 200 and "hardware" in r.get_json(), r.get_json()
    finally:
        platform._wmi_query = real_query
        platform._uname_cache = saved_uname
        getattr(platform, "_platform_cache", {}).update(saved_platforms)


# ── core/capabilities ─────────────────────────────────────────────────
def test_a_hardware_encoder_only_counts_when_its_gpu_is_there():
    """The Gyan FFmpeg builds list NVENC, AMF and QSV on every PC, so
    has_hw_encode was true everywhere, a PC without any GPU included."""
    caps, clipper = m.app("core.capabilities"), m.app("core.clipper")
    m.fresh()
    gyan = ("Encoders:\n V....D libx264    libx264 H.264 / AVC\n V....D h264_amf   AMD AMF H.264 Encoder\n"
            " V....D h264_nvenc NVIDIA NVENC H.264 encoder\n V....D h264_qsv   H264 (Intel Quick Sync Video)\n")
    cpu_only = "Encoders:\n V....D libx264    libx264 H.264 / AVC\n"
    listing = [gyan]

    def ffmpeg(args):
        return m.R(listing[0]) if args[1:] == ["-hide_banner", "-encoders"] else None

    saved = clipper._find_ffmpeg
    clipper._find_ffmpeg = lambda: r"C:\ffmpeg\bin\ffmpeg.exe"
    m.CMD_HOOKS.append(ffmpeg)
    try:
        assert caps._detect_nvenc(set()) is False and caps._detect_nvenc({"Unknown"}) is False
        for vendor in ("NVIDIA", "AMD", "Intel"):
            assert caps._detect_nvenc({vendor}) is True, vendor
        listing[0] = cpu_only
        assert caps._detect_nvenc({"NVIDIA", "AMD", "Intel"}) is False, "a build without them"
    finally:
        clipper._find_ffmpeg = saved
        m.CMD_HOOKS.remove(ffmpeg)


# ── core/dependencies: PresentMon only for the crosshair ──────────────
class _Installs:
    """Every dependency's detect and install replaced by stand-ins, so nothing
    is downloaded or run: `present` says what is installed, `installed`
    lists the keys each install was asked for."""

    def __init__(self, D, present=()):
        self.D, self.present, self.installed = D, set(present), []
        self.saved = [(d, d["detect"], d["install"]) for d in D.DEPENDENCIES]
        self.saved_lhm = D._detect_lhm

    def __enter__(self):
        for d in self.D.DEPENDENCIES:
            d["detect"] = (lambda key=d["key"]: key in self.present)
            d["install"] = (lambda key=d["key"]: self.installed.append(key) or {"ok": True, "needs_restart": False})
        self.D._detect_lhm = lambda: {}
        self.D._boot_check_done.clear()
        return self

    def __exit__(self, *exc):
        for d, detect, install in self.saved:
            d["detect"], d["install"] = detect, install
        self.D._detect_lhm = self.saved_lhm
        self.D._boot_check_done.clear()


def test_presentmon_is_not_installed_for_everyone_at_start_up_only_while_the_crosshair_is_on():
    """Both builds (the 2.x one a 122 MB MSI that registers a service) were
    installed at every start on a PC without them, for the old tuner; only
    the crosshair reads them now."""
    m.fresh()
    D, X = m.app("core.dependencies"), m.app("core.crosshair")
    entries = {d["key"]: d for d in D.DEPENDENCIES}
    assert entries["presentmon1"]["auto_install"] is False and entries["presentmon2"]["auto_install"] is False
    assert set(D.CROSSHAIR_DEPS) == {"presentmon1", "presentmon2"}
    assert [d["key"] for d in D.check_all()["deps"] if d["key"].startswith("presentmon")] == ["presentmon1",
                                                                                            "presentmon2"], \
        "still detected and listed"
    on = [False]
    saved = X.is_enabled
    X.is_enabled = lambda: on[0]
    try:
        everything_else = [d["key"] for d in D.DEPENDENCIES if d["key"] not in D.CROSSHAIR_DEPS]
        with _Installs(D, present=everything_else) as box:
            r = D.check_on_boot()
            assert box.installed == [] and "crosshair" not in r, (box.installed, r)
            assert sorted(r["missing_auto"]) == [], "PresentMon is not missing for a start-up install"
        with _Installs(D, present=everything_else) as box:
            on[0] = True
            r = D.check_on_boot()
            assert box.installed == ["presentmon1", "presentmon2"], box.installed
            assert r["crosshair"]["started"] is True
        with _Installs(D, present=everything_else + ["presentmon1", "presentmon2"]) as box:
            assert D.check_on_boot()["crosshair"]["started"] is False and box.installed == []
        with _Installs(D, present=everything_else) as box:
            D.set_auto_install_enabled(False)
            try:
                assert D.check_on_boot()["crosshair"]["started"] is False and box.installed == []
            finally:
                D.set_auto_install_enabled(True)
        with _Installs(D) as box:                   # (FFmpeg and the rest missing too: one batch, then PresentMon)
            on[0] = False
            D.check_on_boot()
            assert "presentmon1" not in box.installed and "presentmon2" not in box.installed and box.installed
    finally:
        X.is_enabled = saved


def test_switching_the_crosshair_on_installs_presentmon_when_it_is_missing():
    m.fresh()
    D, X = m.app("core.dependencies"), m.app("core.crosshair")
    everything_else = [d["key"] for d in D.DEPENDENCIES if d["key"] not in D.CROSSHAIR_DEPS]
    with _Installs(D, present=everything_else) as box:
        r = D.install_for_crosshair()
        assert r == {"ok": True, "started": False, "reason": "after the start-up check"} and box.installed == [], \
            "before the start-up check is over it starts nothing (that check does it when it ends)"
        D._boot_check_done.set()
        assert D.install_for_crosshair()["started"] is True
        assert m.wait(lambda: box.installed == ["presentmon1", "presentmon2"]), box.installed
        assert m.wait(lambda: not D.get_progress()["running"])
        box.present.update({"presentmon1", "presentmon2"})
        del box.installed[:]
        assert D.install_for_crosshair()["started"] is True
        time.sleep(0.2)
        assert box.installed == [], "already there: nothing installed"
        D.set_auto_install_enabled(False)
        try:
            assert D.install_for_crosshair()["started"] is False
        finally:
            D.set_auto_install_enabled(True)
        D.set_opt_out("presentmon2", True)          # an opt-out is respected
        box.present.difference_update({"presentmon1", "presentmon2"})
        try:
            assert D.install_for_crosshair()["started"] is True
            assert m.wait(lambda: box.installed == ["presentmon1"]) and not D.get_progress()["running"]
            time.sleep(0.1)
            assert box.installed == ["presentmon1"], box.installed
        finally:
            D.set_opt_out("presentmon2", False)
        # The Install button names them: installed although they are not automatic any more.
        del box.installed[:]
        assert D.install_missing(only=["presentmon2"])["completed"] == [{"key": "presentmon2", "ok": True,
                                                                         "needs_restart": False}]
        assert box.installed == ["presentmon2"]
        del box.installed[:]
        D.install_missing()                          # every automatic one: not PresentMon
        assert box.installed == []

    asked = []
    saved = D.install_for_crosshair
    D.install_for_crosshair = lambda: asked.append(1) or {"ok": True, "started": False}
    try:
        assert X.set_settings({"enabled": True})["ok"] and asked == [1], "switched on: asked"
        assert X.set_settings({"enabled": True, "offset_x": 3})["ok"] and asked == [1], "already on: not again"
        assert X.set_settings({"enabled": False})["ok"] and X.set_settings({"enabled": True})["ok"]
        assert asked == [1, 1]
        D.install_for_crosshair = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        assert X.set_settings({"enabled": False})["ok"] and X.set_settings({"enabled": True})["ok"], \
            "a failure there never stands in the way of the switch"
    finally:
        D.install_for_crosshair = saved
        X.set_settings({"enabled": False})
        X.stop()
