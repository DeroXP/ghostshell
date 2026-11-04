import sandbox as m
"""Input remapping robustness: the gamepad mapper, its macros and virtual
pad, the low-level hooks, HidHide, the macro recorder and recoil capture.

Nothing here touches real input: no hook is installed, no key or mouse event
is sent, no virtual controller is created and HidHide's CLI never runs.  The
mapper's output functions record into SENT, XInput reads come from PADS, the
virtual pad is a FakePad, and Adaptive Tuning is a stub."""

import ctypes
import json
import logging
import os
import sys
import threading
import time
import types
from ctypes import wintypes


def _refuse(what):
    def refuse(*a, **k):
        raise AssertionError(f"a test reached {what}")
    return refuse


# Adaptive Tuning: only its pause / resume bookkeeping matters here.
PAUSED: set = set()
_at = types.ModuleType("core.adaptive_tuning")
_at.pause = lambda source: PAUSED.add(source)
_at.resume = lambda source: PAUSED.discard(source)
sys.modules["core.adaptive_tuning"] = _at
import core                                                    # noqa: E402
core.adaptive_tuning = _at

from core import gamepad_mapper as gm                          # noqa: E402
from core import xinput_ctypes as xi                           # noqa: E402
from core import input_hooks, hidhide, virtual_gamepad         # noqa: E402
from core import macro_recorder, recoil_capture                # noqa: E402

SENT: list = []
gm._send_key = lambda vk, key_up: SENT.append(("key", vk, "up" if key_up else "down"))
gm._send_mouse_button = lambda b, key_up: SENT.append(("mouse", b, "up" if key_up else "down"))
gm._send_unicode_char = lambda c: SENT.append(("char", c))
gm._send_mouse_wheel = lambda direction_up, ticks=1: SENT.append(("wheel", direction_up))
gm._send_mouse_move = lambda dx, dy: SENT.append(("move", dx, dy))
gm._user32.SendInput = _refuse("SendInput")
gm._winmm = None                                    # no timeBeginPeriod
gm._get_foreground_exe_name = lambda: ""
input_hooks._user32.SetWindowsHookExW = _refuse("SetWindowsHookExW")
hidhide._run_cli = _refuse("the HidHide CLI")
virtual_gamepad._load_vg = lambda: None             # never vgamepad / ViGEmBus

PADS: dict = {}                                     # XInput slot -> state, or absent


def _get_state(i):
    s = PADS.get(i)
    return dict(s, pressed=list(s["pressed"])) if s else None


xi.get_state = _get_state
_real_connected_slots = xi.get_connected_slots


def _pad(pressed=(), lt=0, rt=0, lx=0, ly=0, rx=0, ry=0, packet=1):
    return {"packet": packet, "buttons": 0, "pressed": list(pressed), "lt": lt, "rt": rt,
            "lx": lx, "ly": ly, "rx": rx, "ry": ry}


class FakePad:
    def __init__(self, pad_type="x360"):
        self.pad_type = pad_type
        self.frames: list = []
        self.closed = False

    def set_state(self, physical, mapping, passthrough_axes=True):
        self.frames.append(dict(physical))

    def close(self):
        self.closed = True


virtual_gamepad.create = lambda pad_type="x360": FakePad(pad_type)

HH: dict = {}                                       # what the fake HidHide was asked to do


def _hidhide(installed=False):
    HH.clear()
    HH.update(engage=[], disengage=[])
    hidhide.is_installed = lambda: {"installed": installed, "install_url": ""}

    def engage(ids, app_path):
        HH["engage"].append(list(ids))
        return {"ok": True, "prior_state": {"cloaked": False}, "hidden_ours": list(ids),
                "whitelisted_app": app_path, "errors": []}

    def disengage(prior, hidden_ours, whitelisted_app):
        HH["disengage"].append(list(hidden_ours))
        return {"ok": True}

    hidhide.engage_for_controller = engage
    hidhide.disengage = disengage
    gm._resolve_physical_controller_ids = lambda profile: [r"HID\VID_045E&PID_0B13&IG_00\8&1"]


def _profile(mode="virtual_controller", mappings=None, poll_hz=1000):
    return gm._ensure_profile_v35_fields({"name": "P", "remap_mode": mode, "poll_hz": poll_hz,
                                          "mappings": list(mappings or [])})


def _map(trigger, output, source="controller", mid="m1"):
    return {"id": mid, "input": {"source": source, "trigger": trigger}, "output": output}


def _use(profile, enabled=True):
    gm._state_cache = {"profiles": {"P": profile}, "active_profile": "P", "enabled": enabled,
                       "toggle_hotkey_vk": 0, "toggle_hotkey_name": ""}


def _fresh():
    m.fresh()
    gm._stop.clear()
    gm._release_all_outputs()
    del SENT[:]
    PADS.clear()
    PAUSED.clear()
    xi.get_connected_slots = _real_connected_slots
    _hidhide()
    gm._thread = None
    gm._state_cache = None
    gm._virtual_pad = gm._virtual_pad_for_profile = gm._virtual_pad_type = None
    gm._virtual_slots = set()
    gm._vpad_retry_after = 0.0
    gm._hidhide_engaged = False
    gm._hidhide_prior_state = None
    gm._hidhide_hidden_ours = []
    gm._hidhide_whitelisted_app = ""
    gm._hidhide_last_retry_ts = 0.0
    gm._hidhide_last_engage_err = ""
    for i in range(4):
        gm._prev_buttons[i] = set()
        gm._prev_triggers[i] = {"LT": False, "RT": False}
    recoil_capture._session = None


def _run_loop():
    gm._thread = threading.Thread(target=gm._polling_loop, daemon=True, name="test-mapper-loop")
    gm._thread.start()
    return gm._thread


def _stop_loop(t):
    gm._stop.set()
    t.join(5)
    assert not t.is_alive(), "the poll loop did not stop"


def _join(name_prefix, seconds=3.0):
    for t in list(threading.enumerate()):
        if t.name.startswith(name_prefix):
            t.join(seconds)


# ── the low-level hooks ───────────────────────────────────────────────
def test_hook_procedures_return_a_full_lresult():
    """Windows reads a hook's return as a pointer-sized LRESULT, and a
    non-zero one from a low-level hook swallows the key or click."""
    word = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(input_hooks.LowLevelKbdProc._restype_) == word
    assert ctypes.sizeof(input_hooks._user32.CallNextHookEx.restype) == word
    proc = input_hooks.LowLevelKbdProc(lambda n, w, l: 0)
    as_windows_calls_it = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                                             wintypes.LPARAM)(ctypes.cast(proc, ctypes.c_void_p).value)
    seen = {as_windows_calls_it(i, i, -i) for i in range(2000)}
    assert seen == {0}, f"a hook that returned 0 reached Windows as {sorted(hex(v) for v in seen)}"


# ── the virtual pad when the physical controller goes away ────────────
def test_virtual_pad_goes_neutral_when_its_controller_disappears():
    _fresh()
    _use(_profile())
    pad = FakePad()
    gm._virtual_pad, gm._virtual_pad_for_profile, gm._virtual_pad_type = pad, "P", "x360"
    PADS[0] = _pad(pressed=["A"], rt=255, lx=20000)
    t = _run_loop()
    try:
        assert m.wait(lambda: pad.frames and pad.frames[-1]["lx"] > 10000), "no frame forwarded"
        del PADS[0]                                 # battery dead, cable pulled
        last = lambda: pad.frames[-1]               # noqa: E731
        assert m.wait(lambda: last()["lx"] == 0 and last()["rt"] == 0 and last()["pressed"] == [], 2), \
            f"the game keeps seeing the last frame: {last()}"
    finally:
        _stop_loop(t)


def test_a_pad_that_vanishes_at_a_slot_refresh_still_releases_its_keys():
    """Two pads; the one holding A disappears just before the poll loop
    rebuilds its slot list.  Its release must still be dispatched."""
    _fresh()
    _use(_profile("key_emulation", [_map("A", {"target": "key", "vk": 0x41})]))
    PADS[0] = _pad()
    PADS[1] = _pad(pressed=["A"])
    unplug = threading.Event()

    def connected_slots():
        if unplug.is_set():
            PADS.pop(1, None)                       # gone by the time the list is rebuilt
        return _real_connected_slots()

    xi.get_connected_slots = connected_slots
    t = _run_loop()
    try:
        assert m.wait(lambda: ("key", 0x41, "down") in SENT, 3)
        unplug.set()
        assert m.wait(lambda: ("key", 0x41, "up") in SENT, 3), "the key stays down: the slot was never polled again"
    finally:
        _stop_loop(t)


# ── HidHide ───────────────────────────────────────────────────────────
def test_the_hidhide_retry_does_not_stall_the_poll_thread():
    _fresh()
    probes = []

    def slow_probe():                               # sc.exe / PowerShell / the CLI
        probes.append(threading.current_thread().name)
        time.sleep(0.6)
        return {"installed": False}

    hidhide.is_installed = slow_probe
    gm._virtual_pad, gm._virtual_pad_for_profile, gm._virtual_pad_type = FakePad(), "P", "x360"
    t0 = time.perf_counter()
    gm._ensure_virtual_pad("virtual_controller", "P", _profile())
    took = time.perf_counter() - t0
    assert took < 0.3, f"the poll thread waited {took:.2f}s for the HidHide retry"
    assert m.wait(lambda: probes, 2) and probes[0] != threading.current_thread().name
    gm._ensure_virtual_pad("virtual_controller", "P", _profile())     # still within the 5 s throttle
    _join("hidhide-retry")
    assert len(probes) == 1


def test_quitting_right_after_the_pad_came_up_leaves_nothing_hidden():
    """The engage runs on a thread 0.4 s (plus a device scan) after the pad
    is created.  If the app quits in between, it must not land afterwards."""
    _fresh()
    _hidhide(installed=True)
    prof = _profile()
    assert m.wait(lambda: gm._ensure_virtual_pad("virtual_controller", "P", prof)    # poll-loop ticks
                  or isinstance(gm._virtual_pad, FakePad), 3)
    gm.shutdown()
    _join("vpad-post-create")
    assert HH["engage"] == [], "HidHide engaged after the mapper had shut down"
    assert not gm._hidhide_engaged


def test_creating_the_pad_does_not_wait_for_the_device_scan():
    """The scan that tells our pad's devices from the user's is a PowerShell
    call; while the poll thread waited on it, the hook queue overflowed."""
    _fresh()
    from core import interrupt_affinity as ia
    phys, ours = r"HID\VID_045E&PID_0B13&IG_00\8&1", r"HID\VID_045E&PID_028E&IG_00\9&2"
    scans = []

    def powershell_scan(command):
        if "XnaComposite" not in command:
            return None
        scans.append(threading.current_thread().name)
        time.sleep(0.5)
        ids = [phys] if len(scans) == 1 else [phys, ours]       # before / after the pad exists
        return m.R(json.dumps([{"name": "Controller", "instance_id": i} for i in ids]))

    m.PS_HOOKS.append(powershell_scan)
    ia.record_known_vigembus_ids(set())
    prof = _profile()
    t0 = time.perf_counter()
    gm._ensure_virtual_pad("virtual_controller", "P", prof)
    took = time.perf_counter() - t0
    assert took < 0.2, f"the poll thread waited {took:.2f}s for the device scan"
    assert gm._virtual_pad is None and m.wait(lambda: scans, 1) and scans[0] != threading.current_thread().name
    assert m.wait(lambda: gm._ensure_virtual_pad("virtual_controller", "P", prof)
                  or isinstance(gm._virtual_pad, FakePad), 3)
    _join("vpad-post-create")
    assert ia.get_known_vigembus_ids() == {ours}, "the pad's own devices were not told apart from the user's"


def test_an_engage_queued_for_a_closed_or_replaced_pad_never_lands():
    _fresh()
    _hidhide(installed=True)
    prof = _profile()
    gm._virtual_pad = FakePad()
    gen = gm._pad_generation
    gm._pad_generation += 1                          # replaced meanwhile
    gm._hidhide_engage_for_pad(prof, gen)
    assert HH["engage"] == []
    gm._hidhide_engage_for_pad(prof, gm._pad_generation)
    assert len(HH["engage"]) == 1 and gm._hidhide_engaged
    gm._hidhide_disengage()


def test_a_retry_click_while_engaged_does_not_engage_twice():
    """A stale "click to retry" badge used to clear the engaged flag and
    engage again; the second engage saw our own hidden devices as the
    user's, so the disengage left them hidden."""
    _fresh()
    _hidhide(installed=True)
    prof = _profile()
    _use(prof)
    gm._virtual_pad = FakePad()
    gm._hidhide_engage_if_needed(prof)
    assert len(HH["engage"]) == 1
    r = gm.force_hidhide_engage()
    assert r["ok"] and r["engaged"]
    assert len(HH["engage"]) == 1, "engaged a second time on top of our own engage"
    gm._hidhide_disengage()
    assert HH["disengage"] == [[r"HID\VID_045E&PID_0B13&IG_00\8&1"]]


def test_a_retry_click_with_no_virtual_pad_hides_nothing():
    _fresh()
    _hidhide(installed=True)
    _use(_profile(), enabled=False)                  # the mapper is off
    r = gm.force_hidhide_engage()
    assert not r["ok"] and "virtual pad" in r["err"]
    assert HH["engage"] == [] and not gm._hidhide_engaged


# ── capture auto-fire ─────────────────────────────────────────────────
def test_capture_autofire_is_released_even_with_the_pad_down():
    _fresh()
    gm._vtrigger_contrib["rt"][gm._CAPTURE_AUTOFIRE_MID] = 1.0     # pulled while the pad was up
    assert gm._virtual_pad is None
    gm.set_capture_autofire(0.0)
    assert gm._CAPTURE_AUTOFIRE_MID not in gm._vtrigger_contrib["rt"], "RT stays pulled for the next pad"
    assert gm.set_capture_autofire(1.0) is False
    assert gm._CAPTURE_AUTOFIRE_MID not in gm._vtrigger_contrib["rt"]


# ── leaving the app ───────────────────────────────────────────────────
def test_shutdown_lets_running_macros_release_their_keys():
    """Every quit path ends in os._exit straight after shutdown(), so a key
    a macro holds must be up by the time shutdown() returns."""
    _fresh()
    old = sys.getswitchinterval()
    sys.setswitchinterval(1.0)                      # no thread switch unless someone waits
    try:
        gm._run_macro([{"key": {"vk": 0x42, "mode": "down"}}], "macro-1")
        assert m.wait(lambda: ("key", 0x42, "down") in SENT, 2)
        gm.shutdown()
        assert ("key", 0x42, "up") in SENT, "the key was still held when shutdown() returned"
        assert not gm._active_macros
    finally:
        sys.setswitchinterval(old)
        gm._stop.clear()


# ── the virtual pad driver ────────────────────────────────────────────
def test_a_failing_virtual_pad_driver_is_logged_once_not_every_tick():
    class Gp:
        fail = True

        def reset(self): pass
        def press_button(self, button): pass
        def left_joystick(self, x_value, y_value): pass
        def right_joystick(self, x_value, y_value): pass
        def left_trigger(self, value): pass
        def right_trigger(self, value): pass

        def update(self):
            if self.fail:
                raise Exception("VIGEM_ERROR_BUS_NOT_FOUND")

    gp = Gp()
    pad = virtual_gamepad.VirtualPad(None, gp, "x360", {})
    warnings = []
    handler = logging.Handler()
    handler.emit = lambda rec: warnings.append(rec) if rec.levelno >= logging.WARNING else None
    virtual_gamepad.log.addHandler(handler)
    try:
        for _ in range(500):                        # 1/8 s of polling at 4 kHz
            pad.set_state({"pressed": ["A"], "lx": 100}, {})
        assert len(warnings) == 1, f"{len(warnings)} warnings for one dead driver"
        gp.fail = False
        pad.set_state({"pressed": []}, {})
        gp.fail = True
        pad.set_state({"pressed": []}, {})
        assert len(warnings) == 2, "a new failure after a recovery is reported again"
    finally:
        virtual_gamepad.log.removeHandler(handler)


# ── the macro recorder ────────────────────────────────────────────────
def test_a_restarted_recording_keeps_its_own_countdown():
    _fresh()
    try:
        macro_recorder.start_recording(start_delay_sec=0.3)
        macro_recorder.cancel_recording()
        macro_recorder.start_recording(start_delay_sec=1.2)
        time.sleep(0.6)                             # the first countdown has run out
        assert macro_recorder.get_status()["phase"] == "countdown", \
            "the cancelled session's timer started the new one early"
        assert m.wait(lambda: macro_recorder.get_status()["phase"] == "recording", 2)
    finally:
        macro_recorder.cancel_recording()


# ── recoil capture ────────────────────────────────────────────────────
def test_a_second_capture_start_leaves_the_running_capture_alone():
    _fresh()
    mid = gm._CAPTURE_AUTOFIRE_MID
    gm._virtual_pad = FakePad()
    real_libs, real_rect = recoil_capture._ensure_libs, recoil_capture._get_foreground_window_rect
    try:
        r = recoil_capture.start_counter_pull_capture(max_duration_sec=5, auto_fire=True)
        assert r["ok"] and r["autofire_engaged"]
        again = recoil_capture.start_counter_pull_capture(max_duration_sec=5, auto_fire=True)
        assert not again["ok"]
        assert gm._vtrigger_contrib["rt"].get(mid) == 1.0, "the running capture lost its auto-fire RT"
        assert "recoil-capture-counter-pull" in PAUSED, "Adaptive Tuning unpaused mid-capture"
        recoil_capture._ensure_libs = lambda: True
        recoil_capture._get_foreground_window_rect = lambda: (0, 0, 1280, 720)
        again = recoil_capture.start_screen_capture(auto_fire=True)
        assert not again["ok"]
        assert gm._vtrigger_contrib["rt"].get(mid) == 1.0, "the running capture lost its auto-fire RT"
        assert "recoil-capture-screen" not in PAUSED, "paused for a capture that never ran"
    finally:
        recoil_capture._ensure_libs, recoil_capture._get_foreground_window_rect = real_libs, real_rect
        recoil_capture.stop_counter_pull_capture()
    _join("recoil-counter-pull-")
    assert mid not in gm._vtrigger_contrib["rt"] and PAUSED == set()


def test_a_capture_cannot_be_asked_to_run_for_ever():
    """Closing the dialog does not stop a capture; with auto-fire, RT stays
    pulled until it ends on its own."""
    _fresh()
    r = recoil_capture.start_counter_pull_capture(max_duration_sec=86400)
    try:
        assert r["ok"] and recoil_capture._session["max_dur"] == 60.0
    finally:
        recoil_capture.stop_counter_pull_capture()
    _join("recoil-counter-pull-")
    real = (recoil_capture._ensure_libs, recoil_capture._get_foreground_window_rect,
            recoil_capture._screen_capture_runner)
    recoil_capture._ensure_libs = lambda: True
    recoil_capture._get_foreground_window_rect = lambda: (0, 0, 1280, 720)
    recoil_capture._screen_capture_runner = lambda session: None     # no screen is grabbed
    try:
        r = recoil_capture.start_screen_capture(max_duration_sec=float("nan"))
        assert r["ok"] and recoil_capture._session["max_dur"] == 60.0
    finally:
        (recoil_capture._ensure_libs, recoil_capture._get_foreground_window_rect,
         recoil_capture._screen_capture_runner) = real
        recoil_capture._session = None


def _second_stop_clears_the_slot_while(stop, sess):
    """Run `stop` while another stop finishes first and empties the slot."""
    out = {}
    th = threading.Thread(target=lambda: out.update(r=stop()))
    th.start()
    time.sleep(0.15)                                # waiting for the runner
    with recoil_capture._session_lock:
        recoil_capture._session = None              # the other stop got there first
    sess["active"] = False
    th.join(3)
    return out.get("r")


def test_two_stops_at_once_both_get_an_answer():
    """The UI's auto-stop and the Stop button can arrive together.  (The
    sessions stand in for live ones whose runner has not finished yet.)"""
    _fresh()
    sess = {"id": "cp-1", "method": "counter_pull", "source": "mouse", "active": True,
            "stop_event": threading.Event(), "samples": [{"dx": 3, "dy": 4, "t": 0.01}],
            "rpm": 600, "err": None, "started_ts": time.time()}
    recoil_capture._session = sess
    got = _second_stop_clears_the_slot_while(recoil_capture.stop_counter_pull_capture, sess)
    assert got is not None, "the slower stop raised"
    assert got["ok"] and got["raw_count"] == 1
    assert recoil_capture.stop_counter_pull_capture() == {"ok": False, "err": "no capture in progress"}

    sess = {"id": "sc-1", "method": "screen", "active": True, "stop_event": threading.Event(),
            "deltas": [(0, 0, 0.0), (2, 3, 0.05)], "rpm": 600, "err": None, "started_ts": time.time()}
    recoil_capture._session = sess
    got = _second_stop_clears_the_slot_while(recoil_capture.stop_screen_capture, sess)
    assert got is not None, "the slower stop raised"
    assert got["ok"] and got["raw_count"] == 2
    assert recoil_capture.stop_screen_capture() == {"ok": False, "err": "no capture in progress"}


def test_a_damaged_user_presets_file_is_kept_not_overwritten():
    _fresh()
    path = recoil_capture.USER_PRESETS_PATH
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"presets": [{"id": "user_1", "name": "my AK"')     # cut off
    assert recoil_capture.list_user_presets() == []
    r = recoil_capture.save_capture_as_preset("new one", [{"dx": 0, "dy": 10}], 600, "screen")
    assert r["ok"]
    assert [p["name"] for p in recoil_capture.list_user_presets()] == ["new one"]
    with open(path + ".corrupt", encoding="utf-8") as f:
        assert '"my AK"' in f.read(), "the old presets were lost"
    os.remove(path + ".corrupt")


# ── the virtual-pad self test (last: it fast-forwards the mapper's clock) ──
class XInputVisiblePad(FakePad):
    """A virtual X360 pad as ViGEm makes one: XInput reports what it is told."""

    def __init__(self, slot):
        super().__init__()
        self.slot = slot
        PADS[slot] = _pad()

    def set_state(self, physical, mapping, passthrough_axes=True):
        super().set_state(physical, mapping)
        if not self.closed:
            PADS[self.slot] = _pad(pressed=physical.get("pressed", []), lt=physical.get("lt", 0),
                                   rt=physical.get("rt", 0), packet=len(self.frames) + 1)

    def close(self):
        super().close()
        PADS.pop(self.slot, None)


class _QuickTestSequence:
    """`time` for the mapper, with the self test's 0.6 s pauses cut short."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, seconds):
        if threading.current_thread().name == "vpad-test":
            seconds = min(seconds, 0.1)
        time.sleep(seconds)


def test_the_virtual_pad_test_never_fires_the_users_mappings():
    """With no mapper pad (a key-emulation profile), the self test makes its
    own pad.  Its button sequence must not be read back as the user pressing
    those buttons."""
    _fresh()
    buttons = list(xi.BUTTON_NAMES_DIGITAL)
    _use(_profile("key_emulation", [_map(b, {"target": "key", "vk": 0x41 + i}, mid=f"m{i}")
                                    for i, b in enumerate(buttons)]))
    made = []
    virtual_gamepad.create = lambda pad_type="x360": made.append(XInputVisiblePad(3)) or made[-1]
    gm.time = _QuickTestSequence()
    t = _run_loop()
    try:
        assert gm.test_virtual_pad()["ok"]
        _join("vpad-test", 10)
        assert made and made[0].closed and len(made[0].frames) > 10, "the test sequence did not run"
        assert [s for s in SENT if s[0] == "key"] == [], "the test pad's presses fired the user's mappings"
        assert not gm._test_pad_slots
    finally:
        _stop_loop(t)
        gm.time = time
        virtual_gamepad.create = lambda pad_type="x360": FakePad(pad_type)
