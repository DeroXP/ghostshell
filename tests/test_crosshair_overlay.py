import sandbox as m

# The crosshair overlay (core/crosshair_overlay.py) against the sandbox's fake
# Win32 adapter: the window it would open, what it hands to Windows, when it
# shows and where, its hotkeys, hide-while-aiming and its message loop.  No
# real window, hook, timer or GDI object is ever created here.  The render
# function is a small stand-in, so these tests don't depend on
# core/crosshair_render.py.

import ctypes
import os
import threading
import time

from PIL import Image

from core import crosshair_overlay as xo

IMG_W, IMG_H = 7, 5
COLOR = (200, 100, 50, 128)
PREMULTIPLIED = bytes([25, 50, 100, 128])           # B, G, R scaled by 128/255, then A
RENDERS: list = []
DISPLAY1, DISPLAY2 = "\\\\.\\DISPLAY1", "\\\\.\\DISPLAY2"
VK_X, VK_N, VK_P = 0x58, 0x4E, 0x50


def _render(design, target_height, loader):
    """A stand-in for crosshair_render.render: a solid, odd-sized image."""
    RENDERS.append((dict(design), target_height))
    size = tuple(design.get("size") or (IMG_W, IMG_H))
    if design.get("kind") == "image":
        img = loader(design["image"]["id"])
        return img.copy() if img is not None else Image.new("RGBA", size, COLOR)
    return Image.new("RGBA", size, tuple(design.get("rgba") or COLOR))


def _cfg(**over) -> dict:
    cfg = {
        "enabled": True, "show_mode": "games", "active_preset": "builtin:classic",
        "designs": {"builtin:classic": {"kind": "shape", "shape": "cross"},
                    "user:0000beef": {"kind": "shape", "shape": "dot", "size": [9, 9]}},
        "images": {}, "render": _render, "game_rules": {},
        "blocked_exes": frozenset({"sotgame.exe"}), "own_exes": frozenset({"vispora.exe"}),
        "is_known_game": lambda exe: exe in ("cs2.exe", "valorant.exe", "sotgame.exe"),
        "anchor": "game", "monitor": "auto", "offset_x": 0, "offset_y": 0,
        "hide_on_aim": "off", "aim_button": "mouse2", "hide_with_cursor": False,
        "hotkeys": {"toggle": "Ctrl+Alt+X", "next": "", "prev": ""}, "exclude_from_capture": False,
    }
    cfg.update(over)
    return cfg


def _fresh():
    m.fresh()
    xo.stop()
    fake = m.FakeOverlayWin()
    xo._win = fake
    xo.on_hotkey = None
    xo._caps = None
    xo._cfg = dict(xo._DEFAULT_CFG)
    with xo._lock:
        xo._rt.update(hidden=False, test_until=0.0, exclusive=frozenset())
        del xo._last_games[:]
        xo._status = None
    del RENDERS[:]
    return fake


def _live(fn):
    """Run fn(fake) with a fresh fake adapter; the overlay is always stopped after."""
    def run():
        fake = _fresh()
        try:
            fn(fake)
        finally:
            xo.stop()
            xo.on_hotkey = None
    run.__name__ = fn.__name__
    return run


def _start(fake, exe="cs2.exe", **over):
    hwnd = fake.add_window(exe) if exe else 0
    xo.configure(_cfg(**over))
    r = xo.start()
    assert r == {"ok": True, "running": True}, r
    return hwnd


def _engine(fake, **over):
    """An overlay driven by hand, with no thread: each eng._step(now, tick) is one
    turn of the loop, so a test can pick exactly which steps are ticks."""
    xo.configure(_cfg(**over))
    eng = xo._Overlay()
    eng.hwnd = fake.hwnd = 0x4321
    return eng


def _reason(reason, seconds=3.0):
    assert m.wait(lambda: xo.get_status()["reason"] == reason, seconds), xo.get_status()


def _switch(fake, hwnd):
    fake.foreground = hwnd
    fake.post_foreground_event()


def _shows(fake):
    return [c[1][1] for c in fake.calls_to("ShowWindow")]


def _topmost_calls(fake):
    return [c for c in fake.calls_to("SetWindowPos") if c[1][1] == xo.HWND_TOPMOST]


def _settle(seconds=0.15):
    time.sleep(seconds)


# ── the sandbox and the adapter ───────────────────────────────────────
def test_the_sandbox_wires_a_fake_adapter_into_the_overlay():
    m.fresh()
    assert isinstance(xo._win, m.FakeOverlayWin)              # wired when the sandbox loaded
    again = m.app("core.crosshair_overlay")
    assert again is xo and isinstance(xo._win, m.FakeOverlayWin)
    src = open(xo.__file__, encoding="utf-8").read()
    assert "ctypes.windll" not in src and "foreground_exe_name" not in src
    assert "\u2014" not in src
    for word in ("SetForegroundWindow", "SetFocus", "SetLayeredWindowAttributes", "SetWindowsHookEx",
                 "RegisterHotKey"):
        assert word not in src, word


def test_the_real_adapter_binds_lazily_and_declares_every_prototype():
    real = xo.Win32()
    assert real._a is None                                     # importing touched no DLL
    api = real.api()                                           # loads the DLLs, opens nothing
    for dll, name, restype, argtypes in xo._SIGS:
        fn = getattr(api, name)
        if fn is None:
            continue                                           # an export this Windows lacks
        assert tuple(fn.argtypes) == tuple(argtypes), name
        assert fn.restype is restype, name
    assert api.DefWindowProcW.restype is xo.LRESULT and ctypes.sizeof(xo.LRESULT) == ctypes.sizeof(ctypes.c_void_p)
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        sizes = {"WNDCLASSEXW": 80, "MSG": 48, "BITMAPINFOHEADER": 40, "BITMAPINFO": 44, "BLENDFUNCTION": 4,
                 "MONITORINFOEXW": 104, "CURSORINFO": 24, "DEVMODEW": 220, "THREAD_POWER_THROTTLING_STATE": 12}
        for name, size in sizes.items():
            assert ctypes.sizeof(getattr(xo, name)) == size, name


def test_premultiplied_pixels_are_what_updatelayeredwindow_expects():
    img = Image.new("RGBA", (2, 1), COLOR)
    img.putpixel((1, 0), (255, 255, 255, 0))
    assert xo._premultiplied_bgra(img) == PREMULTIPLIED + bytes(4)
    assert xo._premultiplied_bgra(img.convert("RGB")) == bytes([50, 100, 200, 255, 255, 255, 255, 255])


# ── the window ────────────────────────────────────────────────────────
@_live
def test_the_window_is_layered_click_through_never_activated_and_untitled(fake):
    _start(fake)
    _reason("shown")
    (create,) = fake.calls_to("CreateWindowExW")
    ex_style, class_name, title, style = create[1][:4]
    assert ex_style == 0x080800A8 == (xo.WS_EX_LAYERED | xo.WS_EX_TRANSPARENT | xo.WS_EX_TOPMOST
                                      | xo.WS_EX_TOOLWINDOW | xo.WS_EX_NOACTIVATE)
    assert class_name == fake.class_name == "VisporaCrosshairOverlay"
    assert title == "" and style == xo.WS_POPUP
    names = [c[0] for c in fake.calls]
    first_dpi = names.index("SetThreadDpiAwarenessContext")
    assert fake.calls[first_dpi][1] == (xo.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,)
    assert first_dpi < names.index("CreateWindowExW")
    assert ("SetThreadPriority", (xo.THREAD_PRIORITY_HIGHEST,)) in [(c[0], c[1]) for c in fake.calls]
    assert "disable_power_throttling" in names and "WTSRegisterSessionNotification" in names
    assert "SetWinEventHook" in names
    overlay = fake.window_thread
    assert overlay != threading.get_ident()
    for name in ("SetThreadDpiAwarenessContext", "SetThreadPriority", "CreateWindowExW", "ShowWindow",
                 "UpdateLayeredWindow", "SetWindowPos"):
        assert {c[2] for c in fake.calls_to(name)} == {overlay}, name
    assert fake.wndproc is xo._wndproc


@_live
def test_showing_and_hiding_never_activate_anything(fake):
    _start(fake)
    _reason("shown")
    xo.set_hidden(True)
    _reason("hidden_by_hotkey")
    xo.set_hidden(False)
    _reason("shown")
    _settle(0.6)
    assert _shows(fake) == [xo.SW_SHOWNOACTIVATE, xo.SW_HIDE, xo.SW_SHOWNOACTIVATE]
    assert not [c for c in fake.calls if c[0].startswith("REFUSED")]
    for c in fake.calls_to("SetWindowPos"):
        assert not c[1][6] & xo.SWP_SHOWWINDOW, c
        assert c[1][6] & xo.SWP_NOACTIVATE, c
    assert all(c[1][6] == xo.TOPMOST_FLAGS for c in _topmost_calls(fake))
    names = [c[0] for c in fake.calls]
    assert names.index("UpdateLayeredWindow") < names.index("ShowWindow")   # drawn before it shows


@_live
def test_the_window_gets_premultiplied_pixels_of_the_rendered_size(fake):
    _start(fake)
    _reason("shown")
    hwnd, x, y, w, h, data, dib_size = fake.ulw[-1]
    assert hwnd == fake.hwnd and (w, h) == (IMG_W, IMG_H) == dib_size
    assert data == PREMULTIPLIED * (IMG_W * IMG_H)
    assert (x, y) == (960 - IMG_W // 2, 540 - IMG_H // 2)      # centre pixel on the screen's centre
    st = xo.get_status()
    assert st["image"] == {"w": IMG_W, "h": IMG_H} and st["position"] == {"x": x, "y": y}
    assert st["visible"] is True and st["game_exe"] == "cs2.exe" and st["preset"] == "builtin:classic"
    assert st["monitor"] == {"id": DISPLAY1, "name": "Display 1", "width": 1920, "height": 1080}


@_live
def test_topmost_is_reasserted_about_twice_a_second_while_visible(fake):
    _start(fake)
    _reason("shown")
    before = len(_topmost_calls(fake))
    time.sleep(1.3)
    periodic = len(_topmost_calls(fake)) - before
    assert 2 <= periodic <= 4, periodic
    fake.above = 0                                             # nothing above us: leave the z-order alone
    _settle(0.6)
    before = len(_topmost_calls(fake))
    time.sleep(1.1)
    assert len(_topmost_calls(fake)) == before
    fake.above = 0x7770
    other = fake.add_window("valorant.exe", pid=777, foreground=False)
    _switch(fake, other)                                       # a new game in front: raise at once
    assert m.wait(lambda: len(_topmost_calls(fake)) > before, 1.0)
    xo.set_hidden(True)
    _reason("hidden_by_hotkey")
    before = len(_topmost_calls(fake))
    time.sleep(0.7)
    assert len(_topmost_calls(fake)) == before                 # hidden: no z-order work


@_live
def test_the_window_procedure_always_returns_an_int(fake):
    hwnd = 0x1234
    assert xo._wndproc(hwnd, xo.WM_MOUSEACTIVATE, 0, 0) == xo.MA_NOACTIVATE == 3
    assert xo._wndproc(hwnd, xo.WM_NCHITTEST, 0, 0) == xo.HTTRANSPARENT
    assert xo._wndproc(hwnd, xo.WM_CLOSE, 0, 0) == 0
    assert xo._wndproc(hwnd, xo.WM_DPICHANGED, 0x00600060, 0) == 0
    assert type(xo._wndproc(hwnd, 0x0001, 0, 2 ** 40)) is int
    _start(fake)
    _reason("shown")
    for msg in (xo.WM_CLOSE, xo.WM_MOUSEACTIVATE, xo.WM_NCHITTEST, xo.WM_DPICHANGED, xo.WM_DISPLAYCHANGE, 0x0005):
        fake.post(msg, 0, 0)
    assert m.wait(lambda: len(fake.results) == 6, 2.0)
    got = {msg: r for msg, wp, lp, r in fake.results}
    assert all(type(r) is int for r in got.values())
    assert got[xo.WM_CLOSE] == 0 and got[xo.WM_MOUSEACTIVATE] == 3 and got[xo.WM_DPICHANGED] == 0
    _settle()
    assert fake.calls_to("DestroyWindow") == [] and xo.is_running()       # WM_CLOSE is ignored
    eng = xo._eng
    real_on_message = eng.on_message
    eng.on_message = lambda *a: 1 / 0                          # a handler that blows up
    try:
        assert xo._wndproc(fake.hwnd, xo.WM_DISPLAYCHANGE, 0, 0) == 0
    finally:
        eng.on_message = real_on_message


# ── when it shows ─────────────────────────────────────────────────────
def test_decide_covers_every_reason_in_order():
    base = dict(enabled=True, locked=False, hidden=False, exe="cs2.exe", own=False,
                own_exes={"vispora.exe"}, show_mode="games", testing=False, game_rules={},
                blocked_exes={"sotgame.exe"}, known_game=True, exclusive_exes=set(), aiming=False,
                hide_with_cursor=False, cursor_visible=False)
    show, hide = {"mode": "show"}, {"mode": "hide"}
    cases = [
        ({}, (True, "shown")),
        ({"enabled": False, "locked": True, "hidden": True}, (False, "disabled")),
        ({"locked": True, "hidden": True}, (False, "locked")),
        ({"hidden": True, "aiming": True}, (False, "hidden_by_hotkey")),
        ({"exe": "vispora.exe", "known_game": False}, (False, "vispora_in_front")),
        ({"exe": "", "own": True, "known_game": False}, (False, "vispora_in_front")),
        ({"exe": "vispora.exe", "show_mode": "always"}, (True, "shown")),
        ({"exe": "vispora.exe", "known_game": False, "testing": True}, (True, "testing")),
        ({"game_rules": {"cs2.exe": hide}}, (False, "rule_hidden")),
        ({"game_rules": {"cs2.exe": hide}, "testing": True}, (False, "rule_hidden")),
        ({"game_rules": {"cs2.exe": hide}, "show_mode": "always"}, (False, "rule_hidden")),
        ({"exe": "sotgame.exe"}, (False, "blocked_game")),
        ({"exe": "sotgame.exe", "testing": True}, (False, "blocked_game")),
        ({"exe": "sotgame.exe", "game_rules": {"sotgame.exe": show}}, (True, "shown")),
        ({"known_game": False}, (False, "no_game")),
        ({"exe": "", "known_game": False}, (False, "no_game")),
        ({"known_game": False, "game_rules": {"cs2.exe": show}}, (True, "shown")),
        ({"known_game": False, "game_rules": {"cs2.exe": {"mode": "default"}}}, (False, "no_game")),
        ({"known_game": False, "testing": True}, (True, "testing")),
        ({"show_mode": "list"}, (False, "not_in_list")),
        ({"show_mode": "list", "game_rules": {"cs2.exe": {"mode": "default"}}}, (False, "not_in_list")),
        ({"show_mode": "list", "game_rules": {"cs2.exe": show}}, (True, "shown")),
        ({"show_mode": "list", "testing": True}, (True, "testing")),
        ({"show_mode": "always", "exe": "notepad.exe", "known_game": False}, (True, "shown")),
        ({"show_mode": "always", "exe": ""}, (True, "shown")),
        ({"exclusive_exes": {"cs2.exe"}, "aiming": True}, (False, "exclusive_fullscreen")),
        ({"exclusive_exes": {"cs2.exe"}, "testing": True}, (False, "exclusive_fullscreen")),
        ({"aiming": True, "hide_with_cursor": True, "cursor_visible": True}, (False, "aiming")),
        ({"hide_with_cursor": True, "cursor_visible": True}, (False, "cursor_visible")),
        ({"hide_with_cursor": False, "cursor_visible": True}, (True, "shown")),
        ({"testing": True}, (True, "testing")),
        ({"testing": True, "aiming": True}, (False, "aiming")),
    ]
    seen = set()
    for over, want in cases:
        got = xo.decide(dict(base, **over))
        assert got == want, (over, got, want)
        seen.add(got[1])
    assert seen == {"shown", "testing", "disabled", "locked", "hidden_by_hotkey", "vispora_in_front",
                    "rule_hidden", "blocked_game", "no_game", "not_in_list", "exclusive_fullscreen",
                    "aiming", "cursor_visible"}
    no_flag = {k: v for k, v in base.items() if k != "known_game"}
    assert xo.decide(dict(no_flag, is_known_game=lambda exe: exe == "cs2.exe")) == (True, "shown")
    assert xo.decide(dict(no_flag, is_known_game=lambda exe: 1 / 0)) == (False, "no_game")
    assert xo.decide(dict(no_flag, exe="CS2.EXE", is_known_game=lambda exe: exe == "cs2.exe")) == (True, "shown")


@_live
def test_vispora_in_front_and_test_show(fake):
    me = fake.add_window("python.exe", pid=os.getpid())
    xo.configure(_cfg())
    assert xo.start()["ok"]
    _reason("vispora_in_front")
    assert xo.get_status()["game_exe"] == ""
    xo.test_show(0.6)
    _reason("testing", 1.0)
    assert xo.get_status()["visible"] is True
    _reason("vispora_in_front", 2.0)
    assert _shows(fake)[-1] == xo.SW_HIDE
    fake.windows[me]["pid"] = 5150                             # someone else's window, as vispora.exe
    fake.windows[me]["path"] = r"C:\Program Files\Vispora\Vispora.exe"
    fake.foreground = 0
    _settle()
    _switch(fake, me)
    _reason("vispora_in_front")


@_live
def test_turning_the_crosshair_on_again_clears_the_hotkey_hide(fake):
    _start(fake)
    _reason("shown")
    xo.set_hidden(True)
    _reason("hidden_by_hotkey")
    xo.configure(_cfg(enabled=False))
    _reason("disabled")
    xo.configure(_cfg(enabled=True))
    _reason("shown")


@_live
def test_session_lock_hides_and_unlock_shows_again(fake):
    _start(fake)
    _reason("shown")
    renders = len(RENDERS)
    fake.post(xo.WM_WTSSESSION_CHANGE, xo.WTS_SESSION_LOCK, 0)
    _reason("locked")
    assert _shows(fake)[-1] == xo.SW_HIDE
    fake.post(xo.WM_WTSSESSION_CHANGE, xo.WTS_SESSION_UNLOCK, 0)
    _reason("shown")
    assert len(RENDERS) == renders + 1                         # redrawn after the unlock


@_live
def test_exclusive_fullscreen_hides_and_stops_the_z_order_work(fake):
    _start(fake)
    _reason("shown")
    xo.set_exclusive_exes({"CS2.exe"})
    _reason("exclusive_fullscreen")
    before = len(_topmost_calls(fake))
    time.sleep(0.8)
    assert len(_topmost_calls(fake)) == before
    xo.set_exclusive_exes(frozenset())
    _reason("shown")


@_live
def test_rules_blocked_games_and_show_modes_on_the_live_overlay(fake):
    hwnd = _start(fake, exe="sotgame.exe")
    _reason("blocked_game")
    xo.configure(_cfg(game_rules={"sotgame.exe": {"mode": "show", "preset": None, "offset_x": None,
                                                  "offset_y": None}}))
    _reason("shown")
    xo.configure(_cfg(game_rules={"sotgame.exe": {"mode": "hide"}}))
    _reason("rule_hidden")
    note = fake.add_window("notepad.exe", pid=99, foreground=False)
    xo.configure(_cfg())
    _switch(fake, note)
    _reason("no_game")
    xo.configure(_cfg(show_mode="always"))
    _reason("shown")
    xo.configure(_cfg(show_mode="list"))
    _reason("not_in_list")
    _switch(fake, hwnd)
    _reason("not_in_list")


@_live
def test_the_foreground_hook_reacts_at_once_and_remembers_recent_exes(fake):
    _start(fake)
    _reason("shown")
    for exe in ["explorer.exe", "vispora.exe"] + [f"game{i:02d}.exe" for i in range(12)]:
        hwnd = fake.add_window(exe, pid=1000 + len(fake.windows), foreground=False)
        _switch(fake, hwnd)
        assert m.wait(lambda: xo._eng is not None and xo._eng.fg_exe == exe, 1.0), exe
    st = xo.get_status()
    assert st["last_games"] == [f"game{i:02d}.exe" for i in range(11, 1, -1)]
    assert "cs2.exe" not in st["last_games"]                   # pushed out: only the newest 10 stay
    assert st["reason"] == "no_game"


@_live
def test_a_game_added_or_removed_while_the_overlay_runs_counts_at_once(fake):
    known = {"cs2.exe"}
    _start(fake, exe="newgame.exe", is_known_game=lambda exe: exe in known)
    _reason("no_game")
    known.add("newgame.exe")                                   # added as a custom game, no new settings
    _reason("shown", 1.0)
    assert xo.get_status()["game_exe"] == "newgame.exe"
    known.discard("newgame.exe")                               # and removed again
    _reason("no_game", 1.0)
    assert xo.get_status()["game_exe"] == ""


# ── where it shows ────────────────────────────────────────────────────
MONITORS = [{"id": DISPLAY1, "width": 1920, "height": 1080, "left": 0, "top": 0, "primary": True},
            {"id": DISPLAY2, "width": 2560, "height": 1440, "left": -2560, "top": -200, "primary": False}]


def test_position_math():
    p = xo.place
    # anchor game: the game's client centre, on the monitor holding the game
    t = p({"anchor": "game", "monitor": DISPLAY1, "monitors": MONITORS, "fg_monitor": DISPLAY2,
           "game_center": (-1000, 300)})
    assert t["monitor"]["id"] == DISPLAY2 and t["centre"] == (-1000, 300)
    # no game in front: the monitor rule ("auto" = the monitor of the window in front)
    t = p({"anchor": "game", "monitor": "auto", "monitors": MONITORS, "fg_monitor": DISPLAY2, "game_center": None})
    assert t["monitor"]["id"] == DISPLAY2 and t["centre"] == (-2560 + 1280, -200 + 720)
    # anchor monitor ignores the game window
    t = p({"anchor": "monitor", "monitor": DISPLAY1, "monitors": MONITORS, "fg_monitor": DISPLAY2,
           "game_center": (5, 5)})
    assert t["monitor"]["id"] == DISPLAY1 and t["centre"] == (960, 540)
    # a display that is gone falls back to auto, and auto with nothing in front to the primary
    t = p({"anchor": "monitor", "monitor": "\\\\.\\DISPLAY9", "monitors": MONITORS, "fg_monitor": DISPLAY2})
    assert t["monitor"]["id"] == DISPLAY2
    t = p({"anchor": "monitor", "monitor": "\\\\.\\DISPLAY9", "monitors": MONITORS, "fg_monitor": None})
    assert t["monitor"]["id"] == DISPLAY1
    assert p({"anchor": "monitor", "monitor": "auto", "monitors": [], "fg_monitor": None}) is None
    # odd sizes: the image's centre pixel (w//2, h//2) lands on the centre pixel
    odd = [{"id": "\\\\.\\DISPLAY3", "width": 1279, "height": 719, "left": 0, "top": 0, "primary": True}]
    t = p({"anchor": "monitor", "monitor": "auto", "monitors": odd})
    assert t["centre"] == (639, 359)
    assert xo.window_origin((639, 359), (21, 21), (0, 0)) == (629, 349)
    assert xo.window_origin((639, 359), (22, 20), (0, 0)) == (628, 349)
    assert xo.window_origin((960, 540), (7, 5), (3, -2)) == (960 - 3 + 3, 540 - 2 - 2)
    # offsets: a rule's offset replaces the global one, axis by axis
    cfg = {"offset_x": 5, "offset_y": 6, "game_rules": {
        "a.exe": {"offset_x": -3, "offset_y": None}, "b.exe": {"offset_x": None, "offset_y": None},
        "c.exe": {"offset_x": 0, "offset_y": 0}}}
    assert xo.effective_offset(cfg, "a.exe") == (-3, 6)
    assert xo.effective_offset(cfg, "b.exe") == (5, 6)
    assert xo.effective_offset(cfg, "c.exe") == (0, 0)
    assert xo.effective_offset(cfg, "") == (5, 6)
    assert xo.effective_offset({"offset_x": "junk", "offset_y": None}, "") == (0, 0)


@_live
def test_a_windowed_game_is_centred_on_its_client_area_with_offsets(fake):
    fake.monitors.append({"id": DISPLAY2, "left": 1920, "top": 0, "right": 1920 + 2560, "bottom": 1440,
                          "primary": False})
    hwnd = fake.add_window("cs2.exe", client=(2000, 100, 801, 601), monitor=DISPLAY2)
    xo.configure(_cfg(offset_x=4, offset_y=1, game_rules={"cs2.exe": {"mode": "default", "preset": None,
                                                                      "offset_x": None, "offset_y": -7}}))
    assert xo.start()["ok"]
    _reason("shown")
    _, x, y, w, h, _, _ = fake.ulw[-1]
    assert (x + w // 2, y + h // 2) == (2000 + 400 + 4, 100 + 300 - 7)
    assert RENDERS[-1][1] == 1440                              # scaled for the game's monitor
    assert xo.get_status()["monitor"]["id"] == DISPLAY2
    fake.windows[hwnd]["client"] = (2100, 150, 801, 601)       # the window moved
    assert m.wait(lambda: fake.calls_to("SetWindowPos") and any(
        c[1][2:4] == (2100 + 400 + 4 - 3, 150 + 300 - 7 - 2) and c[1][6] == xo.MOVE_FLAGS
        for c in fake.calls_to("SetWindowPos")), 1.0)
    assert len(RENDERS) == 1                                   # moved, not redrawn
    xo.configure(_cfg(anchor="monitor", monitor=DISPLAY1))
    assert m.wait(lambda: xo.get_status()["monitor"]["id"] == DISPLAY1, 1.0)
    assert xo.get_status()["position"] == {"x": 960 - 3, "y": 540 - 2}
    assert RENDERS[-1][1] == 1080


@_live
def test_the_design_follows_the_game_rule(fake):
    cfg = _cfg(game_rules={"valorant.exe": {"mode": "default", "preset": "user:0000beef"},
                           "cs2.exe": {"mode": "default", "preset": "user:gone"}})
    assert xo.pick_design(cfg, "valorant.exe") == ("user:0000beef", cfg["designs"]["user:0000beef"])
    assert xo.pick_design(cfg, "cs2.exe") == ("builtin:classic", cfg["designs"]["builtin:classic"])
    assert xo.pick_design(cfg, "") == ("builtin:classic", cfg["designs"]["builtin:classic"])
    assert xo.pick_design(dict(cfg, active_preset="user:none"), "") == ("user:none", None)
    hwnd = _start(fake, game_rules=cfg["game_rules"])
    _reason("shown")
    assert RENDERS[-1][0]["shape"] == "cross" and xo.get_status()["preset"] == "builtin:classic"
    val = fake.add_window("valorant.exe", pid=31, foreground=False)
    _switch(fake, val)
    assert m.wait(lambda: xo.get_status()["preset"] == "user:0000beef", 1.0)
    assert m.wait(lambda: fake.ulw[-1][3:5] == (9, 9), 1.0)
    assert RENDERS[-1][0]["shape"] == "dot"
    _switch(fake, hwnd)
    assert m.wait(lambda: fake.ulw[-1][3:5] == (IMG_W, IMG_H), 1.0)
    xo.configure(_cfg(active_preset="user:missing"))
    _reason("error")
    assert xo.get_status()["last_error"] == "No crosshair design to draw."


@_live
def test_it_redraws_only_when_the_design_image_or_height_changes(fake):
    _start(fake)
    _reason("shown")
    assert len(RENDERS) == 1 and len(fake.ulw) == 1
    xo.configure(_cfg())                                       # equal settings, new objects
    xo.configure(_cfg(offset_x=2))
    assert m.wait(lambda: xo.get_status()["position"]["x"] == 960 - 3 + 2, 1.0)
    _settle()
    assert len(RENDERS) == 1 and len(fake.ulw) == 1
    designs = _cfg()["designs"]
    designs["builtin:classic"] = dict(designs["builtin:classic"], rgba=[255, 0, 0, 255])
    xo.configure(_cfg(designs=designs))
    assert m.wait(lambda: len(RENDERS) == 2, 1.0)
    assert fake.ulw[-1][5] == bytes([0, 0, 255, 255]) * (IMG_W * IMG_H)
    fake.monitors[0].update(right=2560, bottom=1440)           # the resolution changed
    fake.post(xo.WM_DISPLAYCHANGE, 32, (1440 << 16) | 2560)
    assert m.wait(lambda: len(RENDERS) == 3 and RENDERS[-1][1] == 1440, 1.0)
    pic = Image.new("RGBA", (3, 3), (0, 255, 0, 255))
    image_design = {"kind": "image", "image": {"id": "ab" * 16, "scale": 100, "opacity": 100}}
    designs = dict(designs, **{"builtin:classic": image_design})
    xo.configure(_cfg(designs=designs, images={"ab" * 16: pic}))
    assert m.wait(lambda: len(RENDERS) == 4 and fake.ulw[-1][3:5] == (3, 3), 1.0)
    xo.configure(_cfg(designs=designs, images={"ab" * 16: pic}))
    _settle()
    assert len(RENDERS) == 4
    xo.configure(_cfg(designs=designs, images={"ab" * 16: Image.new("RGBA", (5, 5), (0, 0, 255, 255))}))
    assert m.wait(lambda: len(RENDERS) == 5 and fake.ulw[-1][3:5] == (5, 5), 1.0)


@_live
def test_a_render_that_fails_is_reported_once_not_retried_every_tick(fake):
    calls = []

    def broken(design, height, loader):
        calls.append(1)
        raise ValueError("bad design")

    _start(fake, render=broken)
    _reason("error")
    _settle(0.4)
    assert len(calls) == 1
    assert "bad design" in xo.get_status()["last_error"] and xo.get_status()["visible"] is False
    fake.post(xo.WM_DISPLAYCHANGE, 32, 0)                      # a display change earns one more try
    assert m.wait(lambda: len(calls) == 2, 1.0)
    _settle(0.4)
    assert len(calls) == 2 and xo.get_status()["reason"] == "error"
    xo.configure(_cfg(render=lambda d, h, loader: Image.new("RGBA", (2000, 3), COLOR)))
    _settle(0.3)
    assert "too big" in xo.get_status()["last_error"]
    xo.configure(_cfg())
    _reason("shown")
    assert xo.get_status()["last_error"] is None


@_live
def test_a_drawing_windows_refuses_is_tried_again_not_given_up(fake):
    _start(fake)
    _reason("shown")
    fake.ulw_ok = False                                        # a display switch: Windows refuses once
    fake.post(xo.WM_DISPLAYCHANGE, 32, 0)
    _reason("error", 1.0)
    st = xo.get_status()
    assert st["visible"] is False and st["last_error"].startswith("Couldn't draw the crosshair")
    tries = len(fake.ulw)
    _settle(0.5)
    assert len(fake.ulw) == tries                              # not on every tick
    assert m.wait(lambda: len(fake.ulw) == tries + 1, 1.5)     # again after about a second
    _settle(0.5)
    assert len(fake.ulw) == tries + 1                          # then waits longer
    assert xo.get_status()["reason"] == "error"
    fake.ulw_ok = True
    fake.post(xo.WM_DISPLAYCHANGE, 32, 0)                      # the desktop is back: try at once
    _reason("shown", 0.5)
    fake.ulw_ok = False
    fake.post(xo.WM_WTSSESSION_CHANGE, xo.WTS_SESSION_UNLOCK, 0)
    _reason("error", 1.0)
    fake.ulw_ok = True                                         # no message this time: the retry finds it
    _reason("shown", 2.5)
    assert xo.get_status()["last_error"] is None and xo.get_status()["visible"] is True
    assert fake.gdi == set() and fake.bad == []                # failed drawings freed their GDI objects too


@_live
def test_the_overlay_thread_leaves_log_writing_to_another_thread(fake):
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    grab = Grab()
    xo.log.addHandler(grab)
    try:
        _start(fake, render=lambda d, h, loader: 1 / 0)       # an error on the overlay thread
        _reason("error")
        assert m.wait(lambda: any("render failed" in r.getMessage() for r in grab.records), 2.0)
        rec = next(r for r in grab.records if "render failed" in r.getMessage())
        assert rec.threadName == "crosshair-log" and rec.exc_info and rec.exc_info[0] is ZeroDivisionError
    finally:
        xo.log.removeHandler(grab)


# ── hotkeys ───────────────────────────────────────────────────────────
SPEC_TOKENS = ([chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(d) for d in range(10)]
               + [f"F{n}" for n in range(1, 25)] + [f"Num{n}" for n in range(10)]
               + ["NumAdd", "NumSub", "NumMul", "NumDiv", "NumDot", "Insert", "Delete", "Home", "End",
                  "PageUp", "PageDown", "Up", "Down", "Left", "Right", "Space", "Tab", "Enter",
                  "ScrollLock", "PrintScreen", "Minus", "Equals", "Comma", "Period", "Slash", "Semicolon",
                  "Quote", "LeftBracket", "RightBracket", "Backslash", "Backquote", "Mouse3", "Mouse4",
                  "Mouse5"])
SPEC_BARE = set([f"F{n}" for n in range(13, 25)] + [f"Num{n}" for n in range(10)]
                + ["ScrollLock", "Insert", "Home", "End", "PageUp", "PageDown", "NumAdd", "NumSub",
                   "NumMul", "NumDiv", "NumDot", "Mouse3", "Mouse4", "Mouse5"])


def test_hotkey_grammar_accepts_every_token_and_the_bare_key_rules():
    assert sorted(xo.HOTKEY_KEYS) == sorted(SPEC_TOKENS) and len(SPEC_TOKENS) == 104
    vks = set()
    for tok in SPEC_TOKENS:
        spec, err = xo.parse_hotkey(f"ctrl+alt+{tok.lower()}")
        assert err is None and spec["text"] == f"Ctrl+Alt+{tok}" and spec["key"] == tok, (tok, err)
        assert spec["mods"] == xo.MOD_CONTROL | xo.MOD_ALT and xo.format_hotkey(spec) == spec["text"]
        vks.add(spec["vk"])
        bare, err = xo.parse_hotkey(tok)
        if tok in SPEC_BARE:
            assert err is None and bare["text"] == tok and bare["mods"] == 0, tok
        else:
            assert bare is None and err == f"{tok} needs Ctrl, Alt, Shift or Win with it.", (tok, err)
    assert len(vks) == 104                                     # every token is its own key
    spec, err = xo.parse_hotkey("  win + SHIFT+alt+Ctrl+ pageup ")
    assert err is None and spec["text"] == "Ctrl+Alt+Shift+Win+PageUp" and spec["mods"] == 0xF
    assert spec["modifiers"] == ("Ctrl", "Alt", "Shift", "Win")
    assert xo.format_hotkey("x+shift") == "Shift+X" and xo.format_hotkey("Foo") == "" and xo.format_hotkey(None) == ""


def test_hotkey_tokens_are_the_standard_windows_virtual_keys():
    # A token is a virtual key, not a key position: the UI reads KeyboardEvent.keyCode
    # (the virtual key after the keyboard layout), and the overlay polls that same
    # code, so on AZERTY "Ctrl+Alt+A" is the key labelled A.
    want = {chr(c): c for c in range(0x41, 0x5B)}
    want.update({str(d): 0x30 + d for d in range(10)})
    want.update({f"F{n}": 0x6F + n for n in range(1, 25)})                     # VK_F1 0x70 .. VK_F24 0x87
    want.update({f"Num{n}": 0x60 + n for n in range(10)})                      # VK_NUMPAD0 ..
    want.update({
        "NumMul": 0x6A, "NumAdd": 0x6B, "NumSub": 0x6D, "NumDot": 0x6E, "NumDiv": 0x6F,
        "PageUp": 0x21, "PageDown": 0x22, "End": 0x23, "Home": 0x24, "Left": 0x25, "Up": 0x26,
        "Right": 0x27, "Down": 0x28, "PrintScreen": 0x2C, "Insert": 0x2D, "Delete": 0x2E,
        "Space": 0x20, "Tab": 0x09, "Enter": 0x0D, "ScrollLock": 0x91,
        "Mouse3": 0x04, "Mouse4": 0x05, "Mouse5": 0x06,
        # VK_OEM_1, VK_OEM_PLUS, VK_OEM_COMMA, VK_OEM_MINUS, VK_OEM_PERIOD, VK_OEM_2 .. VK_OEM_7
        "Semicolon": 0xBA, "Equals": 0xBB, "Comma": 0xBC, "Minus": 0xBD, "Period": 0xBE, "Slash": 0xBF,
        "Backquote": 0xC0, "LeftBracket": 0xDB, "Backslash": 0xDC, "RightBracket": 0xDD, "Quote": 0xDE,
    })
    assert xo.HOTKEY_KEYS == want
    assert (xo.VK_OEM_1, xo.VK_OEM_2, xo.VK_OEM_3, xo.VK_OEM_4, xo.VK_OEM_5, xo.VK_OEM_6, xo.VK_OEM_7) == (
        0xBA, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE)
    assert "Pause" not in xo.HOTKEY_KEYS and "Pause" not in xo.BARE_KEYS     # polling never sees it
    assert xo.BARE_KEYS <= set(xo.HOTKEY_KEYS)


def test_hotkey_grammar_rejects_with_a_plain_sentence():
    assert xo.parse_hotkey("") == (None, None) and xo.parse_hotkey("   ") == (None, None)
    assert xo.parse_hotkey(None) == (None, None)
    bad = ["Ctrl+Alt", "Ctrl", "Ctrl+A+B", "Ctrl+Foo", "Ctrl+Alt+\u00e9", "Ctrl+Alt+\u0425", "Ctrl++X", "+X",
           "Ctrl+X+", "Ctrl+Ctrl+X", "Control+X", "Ctrl+Alt+Esc", "Ctrl+Alt+Mouse1", "Ctrl+Alt+Mouse2",
           "Alt+F25", "Ctrl+Num10", "Ctrl+" + "A" * 80, "X", "F12", "Delete", "Space", "Up", "1",
           "Pause", "Ctrl+Alt+Pause", "Shift+Num1", "Ctrl+Shift+Num0", "Shift+NumDot"]
    for s in bad:
        spec, err = xo.parse_hotkey(s)
        assert spec is None and isinstance(err, str) and err.endswith("."), (s, err)
        assert "\u2014" not in err
    for junk in (5, b"Ctrl+X", ["Ctrl", "X"]):
        assert xo.parse_hotkey(junk)[0] is None and xo.parse_hotkey(junk)[1]
    assert xo.parse_hotkey("Ctrl+Alt")[1] == "Add a key after the modifiers, like Ctrl+Alt+X."
    assert xo.parse_hotkey("Ctrl+A+B")[1] == "Use one key plus Ctrl, Alt, Shift or Win, like Ctrl+Alt+X."
    assert xo.parse_hotkey("Ctrl+Foo")[1] == "Foo isn't a key Vispora can use."
    assert xo.parse_hotkey("Shift+Shift+F13")[1] == "Shift is in that hotkey twice."
    # Pause sends its press and release together, so a poll never sees it down.
    assert xo.parse_hotkey("Pause")[1] == "Pause isn't a key Vispora can use."
    # With Num Lock on, Shift turns Num1 into End (letting go of Shift first): it could never fire.
    assert xo.parse_hotkey("Shift+Num1")[1] == "Shift doesn't work with Num1. Use Ctrl, Alt or Win instead."
    assert xo.parse_hotkey("Ctrl+Shift+NumDot")[1] == "Shift doesn't work with NumDot. Use Ctrl, Alt or Win instead."
    for ok in ("Num1", "Ctrl+Num1", "Alt+NumDot", "Win+Num9", "Shift+NumAdd", "Ctrl+Shift+NumDiv"):
        spec, err = xo.parse_hotkey(ok)
        assert err is None and spec["text"] == ok, (ok, err)


def _keys(held):
    return lambda vk: vk in held


def test_hotkeys_fire_once_on_the_press_with_exactly_their_modifiers():
    hp = xo.HotkeyPoller()
    st = hp.set_bindings({"toggle": "Ctrl+Alt+X", "next": "Mouse4", "prev": "Win+F5"})
    assert st == {n: {"ok": True, "err": None} for n in ("toggle", "next", "prev")}
    held = set()
    down = _keys(held)
    assert hp.poll(down, True) == []                           # the first poll only records
    held |= {xo.VK_LCONTROL, xo.VK_LMENU, VK_X}
    assert hp.poll(down, True) == ["toggle"]
    for _ in range(5):
        assert hp.poll(down, True) == []                       # held down: auto-repeat never fires
    held.discard(VK_X)
    assert hp.poll(down, True) == []
    held.add(VK_X)
    assert hp.poll(down, True) == ["toggle"]
    held.clear()
    hp.poll(down, True)
    for extra in (xo.VK_LSHIFT, xo.VK_RWIN):                   # an extra modifier: nothing
        held |= {xo.VK_LCONTROL, xo.VK_LMENU, extra}
        hp.poll(down, True)
        held.add(VK_X)
        assert hp.poll(down, True) == [], extra
        held.clear()
        hp.poll(down, True)
    held |= {xo.VK_LCONTROL}                                   # a missing modifier: nothing
    hp.poll(down, True)
    held.add(VK_X)
    assert hp.poll(down, True) == []
    held.clear()
    hp.poll(down, True)
    held |= {xo.VK_LCONTROL, xo.VK_RMENU}                      # AltGr is never Ctrl+Alt
    hp.poll(down, True)
    held.add(VK_X)
    assert hp.poll(down, True) == []
    held.clear()
    hp.poll(down, True)
    held |= {xo.VK_RCONTROL, xo.VK_RMENU}                      # right Ctrl + right Alt is not AltGr
    hp.poll(down, True)
    held.add(VK_X)
    assert hp.poll(down, True) == ["toggle"]
    held.clear()
    hp.poll(down, True)
    held.add(xo.VK_XBUTTON1)                                   # a bare mouse button
    assert hp.poll(down, True) == ["next"]
    held.clear()
    hp.poll(down, True)
    held |= {xo.VK_LCONTROL, xo.VK_RMENU, xo.VK_XBUTTON1}     # nothing fires while AltGr is held
    assert hp.poll(down, True) == []
    held.clear()
    hp.poll(down, True)
    held |= {xo.VK_RWIN, 0x74}                                 # Win is either Windows key
    assert hp.poll(down, True) == ["prev"]


def test_hotkeys_only_act_while_the_crosshair_is_eligible():
    hp = xo.HotkeyPoller()
    hp.set_bindings({"toggle": "Ctrl+Alt+X"})
    held = set()
    down = _keys(held)
    hp.poll(down, False)
    held |= {xo.VK_LCONTROL, xo.VK_LMENU, VK_X}
    assert hp.poll(down, False) == []                          # pressed in another app
    assert hp.poll(down, True) == []                           # still held when the game comes up
    held.discard(VK_X)
    hp.poll(down, True)
    held.add(VK_X)
    assert hp.poll(down, True) == ["toggle"]
    st = hp.set_bindings({"toggle": "Ctrl+Alt+X", "next": "alt+ctrl+x", "prev": "Ctrl+Q+Q"})
    assert st["toggle"] == {"ok": True, "err": None}
    assert st["next"] == {"ok": False, "err": "Same as the Show or hide hotkey."}
    assert st["prev"]["ok"] is False and st["prev"]["err"]
    assert list(hp.bindings) == ["toggle"]


@_live
def test_hotkeys_on_the_live_overlay(fake):
    got = []

    def handler(name):
        t = threading.current_thread()
        got.append((name, t.name, t.daemon, t.ident))

    xo.on_hotkey = handler
    hwnd = _start(fake, hotkeys={"toggle": "Ctrl+Alt+X", "next": "Ctrl+Alt+N", "prev": "Ctrl+Alt+P"})
    _reason("shown")
    assert xo.get_status()["hotkeys"] == {n: {"ok": True, "err": None} for n in ("toggle", "next", "prev")}
    fake.keys |= {xo.VK_LCONTROL, xo.VK_LMENU}
    _settle()
    fake.keys.add(VK_X)
    _reason("hidden_by_hotkey", 1.0)
    _settle()
    fake.keys.discard(VK_X)
    _settle()
    fake.keys.add(VK_X)
    _reason("shown", 1.0)
    fake.keys.discard(VK_X)
    fake.keys.add(VK_N)
    assert m.wait(lambda: len(got) == 1, 1.0)
    fake.keys.discard(VK_N)
    _settle()
    fake.keys.add(VK_P)
    assert m.wait(lambda: len(got) == 2, 1.0)
    assert [g[0] for g in got] == ["next", "prev"]
    assert all(g[1] == "crosshair-hotkey" and g[2] and g[3] != fake.window_thread for g in got)
    fake.keys.discard(VK_P)
    me = fake.add_window("python.exe", pid=os.getpid(), foreground=False)
    _switch(fake, me)
    _reason("vispora_in_front")
    fake.keys.add(VK_X)                                        # pressed while Vispora is in front
    _settle(0.3)
    assert xo.get_status()["reason"] == "vispora_in_front" and not xo._rt["hidden"]
    fake.keys.clear()
    _switch(fake, hwnd)
    _reason("shown")


# ── hide while aiming, hide with the pointer ──────────────────────────
def test_aim_state_hold_and_toggle():
    a = xo.AimState()
    assert a.update("hold", True) is True and a.update("hold", False) is False
    a = xo.AimState()
    assert a.update("toggle", True) is False                   # held at the start: only recorded
    assert a.update("toggle", False) is False
    assert a.update("toggle", True) is True
    assert a.update("toggle", True) is True                    # holding doesn't flip again
    assert a.update("toggle", False) is True
    assert a.update("toggle", True) is False
    a.update("toggle", False)
    a.update("toggle", True)
    assert a.aiming is True
    a.reset()
    assert a.aiming is False and a.update("toggle", True) is False
    assert a.update("off", True) is False
    assert xo.aim_vk("mouse2", False) == xo.VK_RBUTTON and xo.aim_vk("mouse2", True) == xo.VK_LBUTTON
    assert xo.aim_vk("mouse3", True) == xo.VK_MBUTTON
    assert xo.aim_vk("mouse4", False) == xo.VK_XBUTTON1 and xo.aim_vk("mouse5", False) == xo.VK_XBUTTON2


@_live
def test_hide_while_aiming_on_the_live_overlay(fake):
    hwnd = _start(fake, hide_on_aim="hold")
    _reason("shown")
    assert m.wait(lambda: any(c[1][1] == xo.TICK_AIM_MS for c in fake.calls_to("SetWaitableTimer")), 1.0)
    fake.keys.add(xo.VK_RBUTTON)
    _reason("aiming", 1.0)
    fake.keys.clear()
    _reason("shown", 1.0)
    fake.swapped = True                                        # swapped buttons: the right one is VK_LBUTTON
    fake.keys.add(xo.VK_RBUTTON)
    _settle()
    assert xo.get_status()["reason"] == "shown"
    fake.keys = {xo.VK_LBUTTON}
    _reason("aiming", 1.0)
    fake.keys = set()
    fake.swapped = False
    _reason("shown", 1.0)
    xo.configure(_cfg(hide_on_aim="toggle", aim_button="mouse4"))
    _settle()
    fake.keys.add(xo.VK_XBUTTON1)
    _reason("aiming", 1.0)
    fake.keys.clear()
    _settle()
    assert xo.get_status()["reason"] == "aiming"               # toggled: stays hidden after release
    fake.keys.add(xo.VK_XBUTTON1)
    _reason("shown", 1.0)
    fake.keys.clear()
    _settle()
    fake.keys.add(xo.VK_XBUTTON1)
    _reason("aiming", 1.0)
    fake.keys.clear()
    other = fake.add_window("valorant.exe", pid=55, foreground=False)
    _switch(fake, other)                                       # a foreground change resets the toggle
    _reason("shown", 1.0)
    _settle()
    fake.keys.add(xo.VK_XBUTTON1)
    _reason("aiming", 1.0)
    fake.keys.clear()
    note = fake.add_window("notepad.exe", pid=56, foreground=False)
    _switch(fake, note)
    _reason("no_game")
    _switch(fake, hwnd)                                        # eligible again: not aiming
    _reason("shown", 1.0)


def test_cursor_visibility_cases():
    cv = xo.cursor_visible
    assert cv(True, xo.CURSOR_SHOWING, 0x10005) is True
    assert cv(True, 0, 0x10005) is False                       # hidden by the game
    assert cv(True, xo.CURSOR_SHOWING | xo.CURSOR_SUPPRESSED, 0x10005) is False      # touch / pen
    assert cv(True, xo.CURSOR_SUPPRESSED, 0x10005) is False
    assert cv(False, xo.CURSOR_SHOWING, 0x10005) is False      # the call failed
    assert cv(True, xo.CURSOR_SHOWING, 0) is False             # no cursor handle
    assert cv(True, None, 0x10005) is False and cv(True, "junk", 1) is False


@_live
def test_hide_with_the_pointer_on_the_live_overlay(fake):
    fake.cursor = (True, xo.CURSOR_SHOWING, 0x10005)
    _start(fake, hide_with_cursor=True)
    _reason("cursor_visible")
    fake.cursor = (True, 0, 0x10005)
    _reason("shown", 1.0)
    fake.cursor = (True, xo.CURSOR_SHOWING | xo.CURSOR_SUPPRESSED, 0x10005)
    _settle(0.3)
    assert xo.get_status()["reason"] == "shown"
    fake.cursor = (True, xo.CURSOR_SHOWING, 0x10005)
    _reason("cursor_visible", 1.0)
    xo.configure(_cfg(hide_with_cursor=False))
    _reason("shown", 1.0)


@_live
def test_no_flash_over_a_menu_on_the_step_that_makes_the_crosshair_eligible(fake):
    fake.cursor = (True, xo.CURSOR_SHOWING, 0x10005)          # the game sits in its menu
    game = fake.add_window("cs2.exe", pid=31, foreground=False)
    note = fake.add_window("notepad.exe", pid=32)
    eng = _engine(fake, hide_with_cursor=True)
    t = time.monotonic()
    eng._step(t, True)
    assert eng.reason == "no_game" and _shows(fake) == []
    fake.foreground = game                                     # the foreground hook wakes the loop: no tick
    eng.fg_dirty = True
    eng._step(t + 0.005, False)
    assert eng.reason == "cursor_visible" and _shows(fake) == []
    fake.cursor = (True, 0, 0x10005)                           # in play the game hides the pointer
    eng._step(t + 0.04, True)
    assert eng.reason == "shown" and _shows(fake) == [xo.SW_SHOWNOACTIVATE]
    fake.cursor = (True, xo.CURSOR_SHOWING, 0x10005)
    xo.configure(_cfg(hide_with_cursor=True, enabled=False))
    eng._step(t + 0.08, True)
    assert eng.reason == "disabled" and _shows(fake)[-1] == xo.SW_HIDE
    xo.configure(_cfg(hide_with_cursor=True))                 # turned on from the tray: no tick either
    eng._step(t + 0.085, False)
    assert eng.reason == "cursor_visible" and _shows(fake) == [xo.SW_SHOWNOACTIVATE, xo.SW_HIDE]
    fake.foreground = note
    eng.fg_dirty = True
    eng._step(t + 0.09, False)
    assert eng.reason == "no_game" and eng.cursor_visible is False


# ── capture exclusion ─────────────────────────────────────────────────
@_live
def test_capture_exclusion_is_set_before_the_first_show_on_2004_and_later(fake):
    fake.build = 19041
    _start(fake, exclude_from_capture=True)
    _reason("shown")
    names = [c[0] for c in fake.calls]
    assert fake.calls_to("SetWindowDisplayAffinity")[0][1] == (fake.hwnd, xo.WDA_EXCLUDEFROMCAPTURE)
    assert names.index("SetWindowDisplayAffinity") < names.index("ShowWindow")
    assert xo.get_status()["capture_excluded"] is True and xo.capabilities()["capture_exclusion"] is True
    xo.configure(_cfg(exclude_from_capture=False))
    assert m.wait(lambda: xo.get_status()["capture_excluded"] is False, 1.0)
    assert fake.calls_to("SetWindowDisplayAffinity")[-1][1] == (fake.hwnd, xo.WDA_NONE)
    fake.affinity_ok = False
    xo.configure(_cfg(exclude_from_capture=True))
    _settle()
    assert xo.get_status()["capture_excluded"] is False
    affinities = {c[1][1] for c in fake.calls_to("SetWindowDisplayAffinity")}
    assert affinities <= {xo.WDA_NONE, xo.WDA_EXCLUDEFROMCAPTURE}            # never WDA_MONITOR


@_live
def test_capture_exclusion_is_never_attempted_before_2004(fake):
    fake.build = 19040
    assert xo.capabilities()["capture_exclusion"] is False
    _start(fake, exclude_from_capture=True)
    _reason("shown")
    xo.configure(_cfg(exclude_from_capture=False))
    xo.configure(_cfg(exclude_from_capture=True))
    _settle()
    assert fake.calls_to("SetWindowDisplayAffinity") == []
    assert xo.get_status()["capture_excluded"] is False


@_live
def test_a_new_window_left_visible_to_captures_needs_no_call(fake):
    _start(fake)
    _reason("shown")
    assert fake.calls_to("SetWindowDisplayAffinity") == []


# ── stopping, GDI, the loop ───────────────────────────────────────────
@_live
def test_stop_destroys_the_window_on_its_own_thread_and_frees_every_gdi_object(fake):
    _start(fake)
    _reason("shown")
    for rgba in ([255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255]):
        designs = _cfg()["designs"]
        designs["builtin:classic"] = dict(designs["builtin:classic"], rgba=rgba)
        xo.configure(_cfg(designs=designs))
        assert m.wait(lambda: fake.ulw[-1][5][:4] == bytes(rgba[2::-1] + [255]), 1.0)
    overlay, hwnd = fake.window_thread, fake.hwnd
    assert xo.stop() == {"ok": True, "running": False}
    assert not xo.is_running() and xo._eng is None
    assert fake.destroyed == [(hwnd, overlay)] and overlay != threading.get_ident()
    names = [c[0] for c in fake.calls]
    destroy = names.index("DestroyWindow")
    assert names.index("WTSUnRegisterSessionNotification") < destroy
    assert names.index("UnhookWinEvent") < destroy
    assert fake.gdi == set() and fake.bad == []
    # every drawing: DIB selected in, old bitmap back, DIB deleted, then the DC
    seq = [(c[0], c[1]) for c in fake.calls if c[0] in ("CreateCompatibleDC", "CreateDIBSection", "SelectObject",
                                                         "DeleteObject", "DeleteDC", "UpdateLayeredWindow")]
    draws = [i for i, (n, _) in enumerate(seq) if n == "CreateCompatibleDC"]
    assert len(draws) == 4
    for i in draws:
        dc = seq[i][1][0]
        hb = seq[i + 1][1][3]
        assert [n for n, _ in seq[i:i + 7]] == ["CreateCompatibleDC", "CreateDIBSection", "SelectObject",
                                                 "UpdateLayeredWindow", "SelectObject", "DeleteObject", "DeleteDC"]
        assert seq[i + 2][1] == (dc, hb) and seq[i + 4][1] == (dc, fake.STOCK_BITMAP)
        assert seq[i + 5][1] == (hb,) and seq[i + 6][1] == (dc,)
    events = {c[1][0] for c in fake.calls_to("CreateEventW")}
    closed = {c[1][0] for c in fake.calls_to("CloseHandle")}
    assert events and events <= closed and fake.timers == {}             # wake event and timer closed
    st = xo.get_status()
    assert st["running"] is False and st["visible"] is False and st["reason"] == "not_running"
    assert xo.stop() == {"ok": True, "running": False}                     # twice is fine
    assert xo.start()["ok"] and xo.is_running()                            # and it starts again
    _reason("shown")


@_live
def test_a_start_that_fails_says_why_and_leaves_nothing_running(fake):
    fake.window_ok = False
    xo.configure(_cfg())
    r = xo.start()
    assert r["ok"] is False and "crosshair window" in r["err"]
    assert not xo.is_running() and xo._eng is None
    st = xo.get_status()
    assert st["running"] is False and "crosshair window" in st["last_error"]
    assert {c[1][0] for c in fake.calls_to("CreateEventW")} <= {c[1][0] for c in fake.calls_to("CloseHandle")}
    fake.window_ok = True
    fake.add_window("cs2.exe")
    assert xo.start()["ok"]
    _reason("shown")


@_live
def test_a_start_that_takes_too_long_closes_the_late_window_unseen(fake):
    fake.add_window("cs2.exe")
    fake.create_delay = 0.5                                    # a busy machine right after logon
    xo.configure(_cfg())
    saved = xo.START_TIMEOUT_S
    xo.START_TIMEOUT_S = 0.2
    try:
        r = xo.start()
    finally:
        xo.START_TIMEOUT_S = saved
    assert r == {"ok": False, "err": "The crosshair window took too long to open."}
    assert not xo.is_running()
    th = xo._thread
    assert m.wait(lambda: not th.is_alive(), 2.0)
    assert fake.calls_to("CreateWindowExW") and fake.destroyed == [(fake.hwnd, fake.window_thread)]
    assert _shows(fake) == [] and fake.ulw == []               # the late window never showed
    assert xo._eng is None and not xo.is_running()
    st = xo.get_status()
    assert st["running"] is False and st["last_error"] == r["err"]
    fake.create_delay = 0.0
    assert xo.start() == {"ok": True, "running": True}         # the next start begins afresh
    _reason("shown")


@_live
def test_the_loop_never_waits_less_than_5_ms(fake):
    _start(fake, hide_on_aim="hold")
    _reason("shown")
    assert xo.get_status()["timer"] == "high_res"
    time.sleep(0.5)
    assert min(fake.waits) >= xo.MIN_WAIT_MS
    xo.stop()
    fake2 = _fresh()                                           # no waitable timer at all
    fake2.timers_work = False
    _start(fake2, hide_on_aim="hold")
    _reason("shown")
    assert xo.get_status()["timer"] == "normal"
    n0 = len(fake2.waits)
    time.sleep(0.5)
    assert min(fake2.waits) >= xo.MIN_WAIT_MS and len(fake2.waits) - n0 <= 110
    xo.stop()
    fake3 = _fresh()                                           # a timer that is always signalled
    fake3.timer_always_signaled = True
    _start(fake3, hide_on_aim="hold")
    assert m.wait(lambda: xo.get_status()["timer"] == "normal", 1.0)
    n0 = len(fake3.waits)
    time.sleep(0.5)
    assert len(fake3.waits) - n0 <= 110
    _reason("shown")
    xo.stop()
    fake4 = _fresh()                                           # a wait that keeps failing
    _start(fake4)
    fake4.wait_fails = True
    n0 = len(fake4.waits)
    time.sleep(0.5)
    assert len(fake4.waits) - n0 <= 15
    t0 = time.monotonic()
    assert xo.stop()["ok"] and time.monotonic() - t0 < 1.0


@_live
def test_the_tick_follows_aiming_and_hotkeys(fake):
    hwnd = _start(fake, hotkeys={"toggle": "", "next": "", "prev": ""})
    _reason("shown")
    periods = lambda: [c[1][1] for c in fake.calls_to("SetWaitableTimer")]      # noqa: E731
    assert periods()[-1] == xo.TICK_IDLE_MS
    xo.configure(_cfg())
    assert m.wait(lambda: periods()[-1] == xo.TICK_HOTKEY_MS, 1.0)
    xo.configure(_cfg(hide_on_aim="toggle"))
    assert m.wait(lambda: periods()[-1] == xo.TICK_AIM_MS, 1.0)
    note = fake.add_window("notepad.exe", pid=90, foreground=False)
    _switch(fake, note)                                        # not eligible: hotkeys and aim can't act
    assert m.wait(lambda: periods()[-1] == xo.TICK_IDLE_MS, 1.0)
    _settle(0.2)
    n0 = len(fake.waits)
    time.sleep(0.5)
    assert len(fake.waits) - n0 <= 9                           # about 10 wake-ups a second, not 30
    _switch(fake, hwnd)
    assert m.wait(lambda: periods()[-1] == xo.TICK_AIM_MS, 1.0)
    xo.configure(_cfg())
    assert m.wait(lambda: periods()[-1] == xo.TICK_HOTKEY_MS, 1.0)


@_live
def test_a_hotkey_held_as_the_game_comes_up_never_fires(fake):
    game = fake.add_window("cs2.exe", pid=31, foreground=False)
    fake.add_window("notepad.exe", pid=32)
    eng = _engine(fake)                                        # Ctrl+Alt+X shows or hides
    t = time.monotonic()
    eng._step(t, True)
    assert eng.reason == "no_game" and eng.period_ms == xo.TICK_IDLE_MS
    fake.keys |= {xo.VK_LCONTROL, xo.VK_LMENU, VK_X}           # pressed between two slow polls
    fake.foreground = game
    eng.fg_dirty = True
    eng._step(t + 0.01, True)
    assert eng.reason == "shown" and eng.period_ms == xo.TICK_HOTKEY_MS
    assert not xo._rt["hidden"]                                # already down: not a press in the game
    fake.keys.discard(VK_X)
    eng._step(t + 0.05, True)
    fake.keys.add(VK_X)
    eng._step(t + 0.09, True)
    assert eng.reason == "hidden_by_hotkey" and eng.period_ms == xo.TICK_HOTKEY_MS


# ── monitors, capabilities, status ────────────────────────────────────
@_live
def test_list_monitors_uses_physical_modes_and_display_numbers(fake):
    fake.monitors = [{"id": "\\\\.\\DISPLAY2", "left": 0, "top": 0, "right": 2560, "bottom": 1440, "primary": True},
                     {"id": "\\\\.\\DISPLAY1", "left": -1280, "top": 0, "right": 0, "bottom": 720,
                      "primary": False}]
    fake.modes = {"\\\\.\\DISPLAY1": (1920, 1080)}             # 150 % scaling: the rect is DPI-virtualized
    assert xo.list_monitors() == [
        {"id": "\\\\.\\DISPLAY2", "name": "Display 2", "width": 2560, "height": 1440, "primary": True,
         "left": 0, "top": 0},
        {"id": "\\\\.\\DISPLAY1", "name": "Display 1", "width": 1920, "height": 1080, "primary": False,
         "left": -1280, "top": 0}]
    fake.monitors = "junk"
    assert xo.list_monitors() == []
    fake.high_res_ok = False
    xo._caps = None
    assert xo.capabilities() == {"capture_exclusion": True, "high_res_timer": False}


@_live
def test_status_has_the_documented_shape_running_or_not(fake):
    keys = {"running", "visible", "reason", "game_exe", "last_games", "monitor", "position", "image", "preset",
            "capture_excluded", "hotkeys", "timer", "last_error", "since"}
    idle = xo.get_status()
    assert set(idle) == keys and idle["running"] is False and idle["reason"] == "not_running"
    assert set(idle["hotkeys"]) == {"toggle", "next", "prev"}
    xo.configure(_cfg(hotkeys={"toggle": "Ctrl+Alt+X", "next": "Banana", "prev": ""}))
    assert xo.get_status()["hotkeys"]["next"] == {"ok": False, "err": "Banana isn't a key Vispora can use."}
    _start(fake)
    _reason("shown")
    st = xo.get_status()
    assert set(st) == keys and st["running"] is True and isinstance(st["since"], float)
    assert set(st["monitor"]) == {"id", "name", "width", "height"}
    st["last_games"].append("mutated.exe")
    assert "mutated.exe" not in xo.get_status()["last_games"]                # a copy
    assert xo.is_running()
