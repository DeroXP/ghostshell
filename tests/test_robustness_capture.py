import sandbox as m

# Capture and telemetry under awkward conditions: PresentMon 1.x launch and
# restarts (core/frame_telemetry.py), the FFmpeg clip buffer
# (core/clipper.py), the OBS connection (core/obs_backend.py,
# core/stream_helper.py) and the hotkey listener (core/capture_hotkeys.py).
# Nothing here starts a real process, ETW session, capture or hotkey.

import ctypes
import io
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import types
import unittest
from datetime import datetime

FT = m.app("core.frame_telemetry")
C = m.app("core.clipper")
O = m.app("core.obs_backend")
SH = m.app("core.stream_helper")
H = m.app("core.capture_hotkeys")
S = m.app("core.screenshotter")
_real_start_presentmon = FT._start_presentmon


class _OneSecond:
    """datetime as the save functions see it: every call lands in the same second."""

    @staticmethod
    def now():
        return datetime(2026, 11, 4, 12, 0, 0)


class _Overlap:
    """Counts how many calls are inside at the same moment."""

    def __init__(self):
        self.lock, self.inside, self.peak = threading.Lock(), 0, 0

    def __enter__(self):
        with self.lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        time.sleep(0.3)                 # long enough for a second press to arrive

    def __exit__(self, *exc):
        with self.lock:
            self.inside -= 1
        return False


def _on_two_threads(fn):
    results = []
    threads = [threading.Thread(target=lambda: results.append(fn())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return results

# The arguments PresentMon 1.10 accepts: its own argument table, read out of
# the binary the app downloads.  Anything else makes it print usage and exit.
PM1_ARGS = {
    "captureall", "process_name", "exclude", "process_id", "etl_file", "output_file", "output_stdout",
    "multi_csv", "no_csv", "no_top", "qpc_time", "qpc_time_s", "hotkey", "delay", "timed", "exclude_dropped",
    "scroll_indicator", "no_track_display", "track_debug", "simple", "verbose", "session_name",
    "stop_existing_session", "terminate_existing", "dont_restart_as_admin", "restart_as_admin",
    "terminate_on_proc_exit", "terminate_after_timed", "date_time", "track_gpu", "track_gpu_video",
    "track_input", "track_mixed_reality", "include_mixed_reality", "help",
}
PM1_HEADER = ("Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,Dropped,"
              "TimeInSeconds,msInPresentAPI,msBetweenPresents,AllowsTearing,PresentMode,"
              "msUntilRenderComplete,msUntilDisplayed,msBetweenDisplayChange\n")


class FakeProc:
    """A child process: stdout lines are handed out as they are queued, and
    terminate() ends the stream the way a closing pipe does."""

    def __init__(self, lines=(), alive=True):
        self.q = queue.Queue()
        for ln in lines:
            self.q.put(ln)
        self.returncode = None if alive else 0
        self.terminated = False
        self.stdout = self
        self.pid = 4242

    def readline(self):
        return self.q.get(timeout=10)

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = 1
        self.q.put("")

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def _subprocess_with(**overrides):
    ns = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("_")})
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _no_presentmon_2():
    FT._detect_pm2_service = lambda: {"installed": False, "running": False, "name": None}
    FT._find_pm2_cli = lambda: None
    FT._stray_session_cleanup_done = True


# ── frame telemetry ───────────────────────────────────────────────────
def test_presentmon_1_gets_arguments_it_knows_and_its_frames_arrive():
    m.fresh()
    _no_presentmon_2()
    FT._find_presentmon_path = lambda: r"C:\fake\PresentMon.exe"
    launched = []

    def popen(argv, **kw):
        launched.append((argv, kw))
        return FakeProc([PM1_HEADER] + [f"game.exe,4321,0x0000000000ABCDEF,DXGI,1,0,0,{i * 0.0167:.6f},"
                                         f"0.120,16.667,0,Hardware: Independent Flip,1.0,2.0,16.667\n"
                                         for i in range(5)])

    FT.subprocess = _subprocess_with(Popen=popen)
    tied = []
    real_tie = C.tie_to_app_lifetime
    C.tie_to_app_lifetime = tied.append
    try:
        with FT._samples_lock:
            FT._samples.clear()
        h = FT._start_presentmon(4321, prefer_pm2=False)
        assert h and h["variant"] == "pm1"
        assert tied == [h["proc"]], "PresentMon is not tied to the app's lifetime"
        argv, kw = launched[0]
        flags = [a[1:] for a in argv[1:] if a.startswith("-")]
        assert set(flags) <= PM1_ARGS, f"PresentMon 1.10 exits on an unknown argument: {set(flags) - PM1_ARGS}"
        assert "terminate_existing" not in flags, "it stops the session and exits without capturing"
        assert "output_file" not in flags, "PresentMon holds that file exclusively while capturing"
        assert "output_stdout" in flags and h.get("stream")
        assert kw["stdout"] == subprocess.PIPE
        assert kw["stderr"] == subprocess.DEVNULL, "an unread stderr pipe fills up and stalls PresentMon"
        assert m.wait(lambda: len(FT._samples) == 5), "the rows on stdout never reached the ring buffer"
        assert {s["frametime_ms"] for s in FT._samples} == {16.667}
        assert {s["pid"] for s in FT._samples} == {4321}
        FT._stop_presentmon(h)
        assert h["proc"].terminated and not h["reader_thread"].is_alive()
    finally:
        FT.subprocess = subprocess
        C.tie_to_app_lifetime = real_tie


def test_retarget_stops_the_previous_sampler():
    m.fresh()
    _no_presentmon_2()
    FT._find_presentmon_path = lambda: r"C:\fake\PresentMon.exe"
    handles = []

    def start(pid, prefer_pm2=True):
        handles.append({"proc": FakeProc(), "variant": "pm1", "stream": True})
        return handles[-1]

    FT._start_presentmon = start
    try:
        FT.start_tracking(pid=100, exe_name="a.exe")
        first_ev, first_thread = FT._state["stop_event"], FT._state["thread"]
        FT.start_tracking(pid=200, exe_name="b.exe")
        assert first_ev.is_set(), "the first sampler was never told to stop"
        assert m.wait(lambda: not first_thread.is_alive()), "two samplers now run side by side"
        assert handles[0]["proc"].terminated and FT._state["pm_proc"] is handles[1]
    finally:
        FT.stop_tracking()
        FT._start_presentmon = _real_start_presentmon


def test_presentmon_restarted_while_tracking_stops_is_not_left_running():
    m.fresh()
    _no_presentmon_2()
    FT._find_presentmon_path = lambda: r"C:\fake\PresentMon.exe"
    entered, gate = threading.Event(), threading.Event()
    dead = {"proc": FakeProc(alive=False), "variant": "pm1", "stream": True}     # hit its -timed cap
    late = {"proc": FakeProc(), "variant": "pm1", "stream": True}
    calls = []

    def start(pid, prefer_pm2=True):
        calls.append(pid)
        if len(calls) == 1:
            return dead
        entered.set()
        gate.wait(10)                   # the restart takes a while (sc queries, spawn)
        return late

    FT._start_presentmon = start
    try:
        FT.start_tracking(pid=777, exe_name="g.exe")
        assert entered.wait(10), "the sampler did not restart the exited PresentMon"
        FT.stop_tracking()              # the game closed meanwhile
        gate.set()
        assert m.wait(lambda: late["proc"].terminated), "a PresentMon nobody tracks was left running"
        assert FT._state["pm_proc"] is None
    finally:
        gate.set()
        FT.stop_tracking()
        FT._start_presentmon = _real_start_presentmon


# ── clip buffer ───────────────────────────────────────────────────────
class FakeFfmpeg:
    def __init__(self, lines, rc):
        self.lines, self.rc = list(lines), rc
        self.stderr = self

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def poll(self):
        return None if self.lines else self.rc


def test_ffmpeg_crash_gives_the_priority_boost_back():
    m.fresh()
    restored = []
    real = C._restore_self_priority
    C._restore_self_priority = restored.append
    try:
        C._state["running"] = True
        C._prev_priority_class = 0x20
        C._watcher_loop(FakeFfmpeg([b"[ddagrab] Desktop duplication lost\n"], rc=1), threading.Event())
        assert C._state["running"] is False and "rc=1" in C._state["last_err"]
        assert restored == [0x20], "Vispora stayed at HIGH priority after FFmpeg died"
        assert C._prev_priority_class is None

        # a deliberate stop leaves the restore to stop_buffer()
        C._state["running"] = False
        C._prev_priority_class = 0x20
        ev = threading.Event()
        ev.set()
        C._watcher_loop(FakeFfmpeg([], rc=1), ev)
        assert restored == [0x20] and C._prev_priority_class == 0x20
    finally:
        C._restore_self_priority = real
        C._prev_priority_class = None
        C._state.update(running=False, last_err="")


def test_start_does_not_take_over_a_running_buffer():
    m.fresh()
    calls = []
    saved = (O.is_ready_for_recording, O.setup_scene, O.ensure_replay_buffer)
    O.is_ready_for_recording = lambda: True
    O.setup_scene = lambda **kw: calls.append("setup") or {"ok": True}
    O.ensure_replay_buffer = lambda **kw: {"ok": True, "running": True, "buffer_seconds": 60}
    try:
        C._state.update(running=True, capture_method="ddagrab", ffmpeg_pid=999, segment_dir=r"C:\buf")
        r = C.start_buffer()
        assert r.get("already_running"), ascii(r)
        assert calls == [], "the OBS path ran over a live FFmpeg buffer"
        assert C._state["capture_method"] == "ddagrab" and C._state["ffmpeg_pid"] == 999
    finally:
        O.is_ready_for_recording, O.setup_scene, O.ensure_replay_buffer = saved
        C._state.update(running=False, capture_method="", ffmpeg_pid=0, segment_dir="")


def _ffmpeg_token(s):
    """How FFmpeg's concat demuxer reads a quoted file name (av_get_token)."""
    out, i, quoted = [], 0, False
    while i < len(s):
        c = s[i]
        if quoted:
            if c == "'":
                quoted = False
            else:
                out.append(c)
        elif c == "'":
            quoted = True
        elif c == "\\" and i + 1 < len(s):
            i += 1
            out.append(s[i])
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _make_ring(seg_dir):
    """Five finished 2-second segments, oldest first."""
    os.makedirs(seg_dir, exist_ok=True)
    now = time.time()
    for i in range(5):
        p = os.path.join(seg_dir, f"buf_{i:03d}.ts")
        with open(p, "wb") as f:
            f.write(b"\x47" * 40 * 1024)
        os.utime(p, (now - 10 + i * 2, now - 10 + i * 2))


def _save_from_ring(seg_dir):
    """Run save_clip() over five 2-second segments in `seg_dir`, with the
    concat step faked.  Returns (result, the concat list FFmpeg was given)."""
    _make_ring(seg_dir)
    C.set_settings(output_dir=os.path.join(m.ROOT, "Clips"))
    lists = []

    def run(argv, **kw):
        with open(argv[argv.index("-i") + 1], encoding="utf-8") as f:
            lists.append(f.read())
        with open(argv[-1], "wb") as f:
            f.write(b"mp4")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    C.subprocess = _subprocess_with(run=run)
    real_fg = C._foreground_window_info
    C._foreground_window_info = lambda: {}
    try:
        C._state.update(running=True, segment_dir=seg_dir, ffmpeg_path="ffmpeg", capture_method="ddagrab")
        return C.save_clip(seconds=6), (lists[0] if lists else "")
    finally:
        C.subprocess = subprocess
        C._foreground_window_info = real_fg
        C._state.update(running=False, segment_dir="", ffmpeg_path="", capture_method="")


def test_clip_save_works_without_obs():
    m.fresh()
    r, _ = _save_from_ring(os.path.join(m.ROOT, "ring", "session_1"))
    assert r["ok"] and os.path.isfile(r["path"]), ascii(r)
    assert r["source_segments"] == 4, "the newest (still being written) segment is left out"


def test_clip_is_saved_when_the_profile_path_has_a_quote():
    m.fresh()
    r, concat = _save_from_ring(os.path.join(m.ROOT, "Users", "O'Brien", "clipper_buffer", "session_1"))
    assert r["ok"], ascii(r)
    names = [_ffmpeg_token(ln[len("file "):]) for ln in concat.splitlines()]
    assert len(names) == 4
    for n in names:
        assert os.path.isfile(n), f"FFmpeg would look for {n!r}"


def test_a_double_press_saves_two_clips_one_after_the_other():
    m.fresh()
    seg_dir = os.path.join(m.ROOT, "ring2", "session_1")
    _make_ring(seg_dir)
    out_dir = os.path.join(m.ROOT, "Clips2")
    C.set_settings(output_dir=out_dir)
    overlap = _Overlap()

    def run(argv, **kw):
        with overlap:
            with open(argv[-1], "wb") as f:
                f.write(b"mp4")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    saved = (C.subprocess, C._foreground_window_info, C.datetime)
    C.subprocess, C._foreground_window_info, C.datetime = _subprocess_with(run=run), (lambda: {}), _OneSecond
    try:
        C._state.update(running=True, segment_dir=seg_dir, ffmpeg_path="ffmpeg", capture_method="ddagrab")
        results = _on_two_threads(lambda: C.save_clip(seconds=6))
        assert len(results) == 2 and all(r["ok"] for r in results), ascii(results)
        assert overlap.peak == 1, "two saves ran at once over one concat list"
        names = sorted(os.path.basename(r["path"]) for r in results)
        assert names == ["clip_2026-11-04_12-00-00-2.mp4", "clip_2026-11-04_12-00-00.mp4"], names
        assert sorted(os.listdir(out_dir)) == names
    finally:
        C.subprocess, C._foreground_window_info, C.datetime = saved
        C._state.update(running=False, segment_dir="", ffmpeg_path="", capture_method="")


class FakeCaptureFfmpeg:
    """The FFmpeg start_buffer() launches: stderr stays open until it stops."""

    def __init__(self):
        self.pid, self.returncode = 5150, None
        self.stdin = io.BytesIO()       # stop_buffer() sends "q" here
        self.stderr = self
        self.done = threading.Event()

    def readline(self):
        self.done.wait(10)
        return b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        self.done.set()
        return 0

    def terminate(self):
        self.returncode = 1
        self.done.set()

    kill = terminate


def test_the_capture_ffmpeg_is_tied_to_the_app_lifetime():
    m.fresh()
    child, tied = FakeCaptureFfmpeg(), []
    region = {"mode": "monitor", "x": 0, "y": 0, "w": 1920, "h": 1080, "title": "", "source": "test",
              "monitor_idx": 0, "is_virtual_screen": False}
    names = ("subprocess", "_find_ffmpeg", "_pick_encoder", "_resolve_capture_region", "_boost_self_priority",
             "_restore_self_priority", "tie_to_app_lifetime", "_ddagrab_avail_cache")
    saved = {n: getattr(C, n) for n in names}
    C.subprocess = _subprocess_with(Popen=lambda *a, **k: child)
    C._find_ffmpeg = lambda: "ffmpeg"
    C._pick_encoder = lambda ffmpeg_path, preference="auto", verify=False: "libx264"
    C._resolve_capture_region = lambda target: dict(region)
    C._boost_self_priority = lambda *a: None                 # (this test process keeps its own priority)
    C._restore_self_priority = lambda *a: None
    C.tie_to_app_lifetime = tied.append
    C._ddagrab_avail_cache = False
    try:
        assert C.start_buffer()["ok"]
        assert tied == [child], "the capture FFmpeg would outlive a crashed or killed app"
        assert C.stop_buffer()["was_running"] and child.stdin.getvalue() == b"q\n", "normal stop unchanged"
    finally:
        for n, v in saved.items():
            setattr(C, n, v)
        C._state.update(running=False)


def test_a_tied_child_ends_when_the_app_does():
    m.fresh()
    # (a child Python interpreter is the one process a test may start)
    child = m.REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        if not C.tie_to_app_lifetime(child):
            raise unittest.SkipTest("this process may not create or nest a job object")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        assert k32.CloseHandle(C._job)   # what Windows does when the app exits, however it exits
        C._job = None
        child.wait(timeout=10)          # raises TimeoutExpired if it was left running
    finally:
        if child.poll() is None:
            child.kill()
        C._job = None


def test_a_child_that_cannot_join_the_job_runs_on_and_is_reported_once():
    m.fresh()
    warnings = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = warnings.append
    C.log.addHandler(handler)
    C._job_warned = False
    try:
        assert C.tie_to_app_lifetime(types.SimpleNamespace()) is False      # no process handle to give
        assert C.tie_to_app_lifetime(types.SimpleNamespace()) is False
        assert len(warnings) == 1
    finally:
        C.log.removeHandler(handler)


# ── screenshots ───────────────────────────────────────────────────────
def test_a_double_press_takes_two_screenshots_one_after_the_other():
    m.fresh()
    overlap = _Overlap()

    def to_png(rgb, size, output):
        with overlap:
            with open(output, "wb") as f:
                f.write(b"\x89PNG" + rgb)

    class Grab:
        rgb, size = b"pixels", (2, 2)

    class Sct:
        monitors = [{}, {"left": 0, "top": 0, "width": 2, "height": 2}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, region):
            return Grab()

    tools = types.SimpleNamespace(to_png=to_png)
    fake_mss = types.SimpleNamespace(mss=Sct, tools=tools)
    saved = (S._mss, S._foreground_window_info, S.datetime, sys.modules.get("mss"))
    S._mss, S.datetime = fake_mss, _OneSecond
    S._foreground_window_info = lambda: {"hwnd": 1, "left": 0, "top": 0, "right": 200, "bottom": 200,
                                         "exe": "game.exe", "pid": 5}
    sys.modules["mss"] = fake_mss
    try:
        results = _on_two_threads(lambda: S.take_screenshot("foreground"))
        assert len(results) == 2 and all(r["ok"] for r in results), ascii(results)
        assert overlap.peak == 1, "two captures wrote at once"
        names = sorted(os.path.basename(r["path"]) for r in results)
        assert names == ["game_2026-11-04_12-00-00-2.png", "game_2026-11-04_12-00-00.png"], names
        assert all(os.path.getsize(r["path"]) > 0 for r in results)
    finally:
        S._mss, S._foreground_window_info, S.datetime = saved[:3]
        if saved[3] is None:
            sys.modules.pop("mss", None)
        else:
            sys.modules["mss"] = saved[3]


def test_auto_encoder_skips_hardware_encoders_this_pc_cannot_open():
    m.fresh()
    ran = []

    def machine(working):
        def run(argv, **kw):
            ran.append(argv)
            if "-encoders" in argv:      # what Gyan's builds list on every PC
                return types.SimpleNamespace(returncode=0, stderr="", stdout=(
                    " V....D h264_amf  AMD AMF H.264 Encoder\n V....D h264_nvenc  NVIDIA NVENC H.264 encoder\n"
                    " V..... h264_qsv  H.264 (Intel Quick Sync Video)\n V....D libx264  libx264 H.264\n"))
            enc = argv[argv.index("-c:v") + 1]
            return types.SimpleNamespace(returncode=0 if enc in working else 1, stdout="",
                                         stderr="" if enc in working else "Cannot load nvcuda.dll")
        return run

    try:
        C._encoder_ok_cache.clear()
        C.subprocess = _subprocess_with(run=machine({"h264_qsv"}))           # Intel graphics only
        assert C._pick_encoder("ffmpeg", "auto", verify=True) == "h264_qsv"
        probes = len(ran)
        assert C._pick_encoder("ffmpeg", "auto", verify=True) == "h264_qsv"
        assert len(ran) == probes + 1, "the test encodes are not repeated on every start"

        C._encoder_ok_cache.clear()
        C.subprocess = _subprocess_with(run=machine(set()))                 # no usable GPU encoder
        assert C._pick_encoder("ffmpeg", "auto", verify=True) == "libx264"

        C._encoder_ok_cache.clear()
        C.subprocess = _subprocess_with(run=machine({"h264_nvenc", "h264_qsv"}))
        assert C._pick_encoder("ffmpeg", "auto", verify=True) == "h264_nvenc"
        del ran[:]
        assert C._pick_encoder("ffmpeg", "h264_amf") == "h264_amf" and ran == [], "an explicit choice is kept"

        # capabilities.detect() runs at app start, before the window opens:
        # it still gets the one quick listing call and no test encodes
        C._encoder_ok_cache.clear()
        C.subprocess = _subprocess_with(run=machine(set()))
        del ran[:]
        assert C._pick_encoder("ffmpeg") == "h264_nvenc"
        assert len(ran) == 1 and "-encoders" in ran[0], "the app start now waits on test encodes"
    finally:
        C.subprocess = subprocess
        C._encoder_ok_cache.clear()


def test_starting_the_buffer_is_what_verifies_the_encoder():
    m.fresh()
    asked = []

    class Picked(Exception):
        pass

    def pick(ffmpeg_path, preference="auto", verify=False):
        asked.append(verify)
        raise Picked()                  # stop before anything is spawned

    real = (C._find_ffmpeg, C._pick_encoder)
    C._find_ffmpeg, C._pick_encoder = (lambda: "ffmpeg"), pick
    try:
        try:
            C.start_buffer()
        except Picked:
            pass
        assert asked == [True]
    finally:
        C._find_ffmpeg, C._pick_encoder = real


# ── OBS ───────────────────────────────────────────────────────────────
class FakeObsClient:
    made = []

    def __init__(self, host="localhost", port=4455, password="", timeout=None):
        self.closed, self.fail = 0, None
        FakeObsClient.made.append(self)

    def get_version(self):
        return types.SimpleNamespace(obs_version="30.2.0", obs_web_socket_version="5.5.0", rpc_version=1,
                                     platform="windows", platform_description="Windows 11")

    def disconnect(self):
        self.closed += 1

    def get_scene_list(self):
        if self.fail:
            raise self.fail
        return types.SimpleNamespace(scenes=[])

    def get_replay_buffer_status(self):
        return types.SimpleNamespace(output_active=False)


class OBSSDKRequestError(Exception):
    """What obsws_python raises when OBS answers a request with an error."""


def test_obs_going_away_is_noticed_and_a_reconnect_closes_the_old_socket():
    m.fresh()
    sys.modules["obsws_python"] = types.SimpleNamespace(ReqClient=FakeObsClient)
    del FakeObsClient.made[:]
    try:
        assert O.connect()["connected"]
        first = O._client
        O.connect(force_reconnect=True)
        assert first.closed == 1, "the replaced client's socket was left open"
        second = O._client
        assert second is not first and second.closed == 0

        second.fail = ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        assert O.get_recording_status()["err"]
        assert O._conn_state["connected"] is False, "still shown as connected, so it never reconnects"
        assert second.closed == 1 and O._client is None

        assert O.connect()["connected"]
        third = O._client
        third.fail = OBSSDKRequestError("GetSceneList", 600)
        O.get_recording_status()
        assert O._conn_state["connected"] is True and third.closed == 0, "a refused request is not a lost link"
    finally:
        O.disconnect()
        sys.modules.pop("obsws_python", None)


def test_stream_helper_waits_for_obs_and_shares_the_client_one_call_at_a_time():
    m.fresh()
    SH.set_settings(enabled=True)
    SH._last_active_exe = ""
    unlocked = []

    class Client:
        def __getattr__(self, name):
            def call(*a, **k):
                if not O._client_lock.locked():
                    unlocked.append(name)
                return types.SimpleNamespace(scenes=[], inputs=[], scene_items=[])
            return call

    try:
        O._client, O._conn_state["connected"] = None, False
        r = SH.update_for_game("Game.exe")
        assert not r["ok"] and SH._last_active_exe == "", "the game was marked done while OBS was down"

        O._client, O._conn_state["connected"] = Client(), True
        r = SH.update_for_game("Game.exe")
        assert r["ok"], ascii(r)
        assert SH._last_active_exe == "Game.exe"
        assert unlocked == [], f"requests sent without obs_backend's lock: {unlocked}"
        assert not O._client_lock.locked()
    finally:
        O._client, O._conn_state["connected"] = None, False
        SH._last_active_exe = ""
        SH.set_settings(enabled=False)


# ── hotkeys ───────────────────────────────────────────────────────────
class FakeUser32:
    taken = {0x79}                      # F10 belongs to another app

    def RegisterHotKey(self, hwnd, hk_id, mods, vk):
        return 0 if vk in self.taken else 1

    def UnregisterHotKey(self, hwnd, hk_id):
        return 1

    def PeekMessageW(self, *a):
        return 0

    def TranslateMessage(self, *a):
        return 0

    def DispatchMessageW(self, *a):
        return 0


class _Ctypes:
    windll = types.SimpleNamespace(user32=FakeUser32())

    @staticmethod
    def GetLastError():
        return 1409                     # ERROR_HOTKEY_ALREADY_REGISTERED

    def __getattr__(self, name):
        return getattr(ctypes, name)


def test_hotkey_error_shows_the_real_code_and_goes_away_after_a_good_rebind():
    m.fresh()
    H.ctypes = _Ctypes()
    try:
        H.register_binding("save_clip", "F10", lambda: None)
        H.register_binding("screenshot", "F9", lambda: None)
        H.start()
        # (ascii(): the message has an em dash, which the runner's console cannot print)
        assert m.wait(lambda: "1409" in H.get_status()["last_err"]), ascii(H.get_status()["last_err"])
        assert m.wait(lambda: H._registered == {2: "F9"})        # the pass is done
        assert H.get_status()["bindings"] == [{"name": "screenshot", "hotkey": "F9"}], \
            "the UI lists F10 as Registered although Windows refused it"
        H.register_binding("save_clip", "F11", lambda: None)
        assert m.wait(lambda: H.get_status()["last_err"] == ""), \
            f"F11 registered fine but the UI still shows: {ascii(H.get_status()['last_err'])}"
        assert m.wait(lambda: len(H.get_status()["bindings"]) == 2)
        assert {b["hotkey"] for b in H.get_status()["bindings"]} == {"F9", "F11"}
        H.stop()
        assert H.get_status()["bindings"] == [] and not H._registered, "nothing is held once stopped"
    finally:
        H.stop()
        H.unregister_binding("save_clip")
        H.unregister_binding("screenshot")
        H.ctypes = ctypes
