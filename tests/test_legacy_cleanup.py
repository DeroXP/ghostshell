"""What the controller emulator's removal still has to look after.

Versions up to 3.6.8 hid controllers through HidHide and listed Vispora as an
allowed program there; core/hidhide_cleanup.py takes that back once, through
a fake HidHideCLI.exe here (sandbox CMD_HOOKS, nothing really runs), and
leaves everything else in HidHide's lists alone.  ViGEmBus and HidHide are
no longer dependencies Vispora installs, and old state naming them is
dropped.  The foreground-window
helper the updater and the integrity scan rely on moved from the mapper to
core/utils.py; the last tests check they still see a game or Vispora in
front through it."""
import sandbox as m

import json
import os
import threading

from core import hidhide_cleanup as HC        # noqa: E402
from core import integrity_scan as IS         # noqa: E402
from core import updater as UP                # noqa: E402
from core import utils                        # noqa: E402

CLI = r"C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe"
HC._find_cli = lambda: CLI

# The user's DualSense, as the old mapper recorded it, and the form its
# _to_symbolic_link turned it into before --dev-hide.
PAD = r"HID\VID_054C&PID_0CE6\6&1bce44cb&0&0000"
PAD_LINK = r"\\?\hid#vid_054c&pid_0ce6#6&1bce44cb&0&0000"
PAD_USB_LINK = r"\\?\usb#vid_054c&pid_0ce6#5&2b3c4d5e&0&3"
# Entries other tools (or the user) made: never touched.
THEIR_PAD = r"HID\VID_045E&PID_0B13\7&2a1b3c4d&0&0000"
THEIR_LINK = r"\\?\HID#VID_046D&PID_C52B#8&11112222&0&0000"   # not all lower case: not Vispora's form
DS4W = r"C:\Program Files\DS4Windows\DS4Windows.exe"
VISPORA = r"C:\Program Files\Vispora\Vispora.exe"
OLD_NAME = r"D:\Games\Tools\GhostShell.exe"
PYTHON = r"C:\Python312\python.exe"


class FakeHidHide:
    """HidHideCLI.exe: lists printed the way the real one prints them
    (replayable commands), or as bare lines like older versions."""

    def __init__(self, devices=(), apps=(), cloak=False, raw=False):
        self.devices, self.apps, self.cloak, self.raw = list(devices), list(apps), cloak, raw
        self.calls = []
        self.refuse = set()         # flags answered with "Access is denied."
        self.keep = set()           # entries an unhide / unreg "succeeds" on but leaves in place
        self.gate = None            # (entered, release): hold --dev-list until released
        self.exact = False          # True: removal matches case exactly

    def __call__(self, a):
        if os.path.basename(a[0]).lower() != "hidhidecli.exe":
            return None
        args = a[1:]
        self.calls.append(args)
        flag = args[0] if args else ""
        if flag in self.refuse:
            return m.F("Access is denied.")
        if flag == "--dev-list":
            if self.gate:
                self.gate[0].set()
                self.gate[1].wait(5)
            return m.R(self._listing("--dev-hide", self.devices))
        if flag == "--app-list":
            return m.R(self._listing("--app-reg", self.apps))
        if flag == "--cloak-state":
            return m.R("--cloak-on" if self.cloak else "--cloak-off")
        if flag in ("--cloak-on", "--cloak-off"):
            self.cloak = flag == "--cloak-on"
            return m.R()
        if flag == "--dev-unhide":
            return self._remove(self.devices, args[1])
        if flag == "--app-unreg":
            return m.F("", out="Application not registered") if args[1] not in self.apps \
                else self._remove(self.apps, args[1])
        return m.F(f"unknown command {flag}")

    def _listing(self, flag, entries):
        if self.raw:
            return "\n".join(entries)
        return "\n".join(f'{flag} "{e}"' for e in entries)

    def _remove(self, entries, target):
        same = (lambda e: e == target) if self.exact else (lambda e: e.lower() == target.lower())
        if not any(same(e) for e in entries):
            return m.F("The device instance path isn't on the list")
        if target not in self.keep:
            entries[:] = [e for e in entries if not same(e)]
        return m.R()

    def ran(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


def machine(**kw) -> FakeHidHide:
    m.fresh()
    fake = FakeHidHide(**kw)
    m.CMD_HOOKS.append(fake)
    return fake


def marker(cloaked=False, hidden_ours=(PAD,), app=VISPORA, prior_devices=()):
    data = {"prior_state": {"cloaked": cloaked,
                            "hidden_devices": [f'--dev-hide "{d}"' for d in prior_devices],
                            "whitelisted_apps": []},
            "hidden_ours": list(hidden_ours), "whitelisted_app": app}
    with open(os.path.join(m.config.APPDATA_DIR, "hidhide_engaged.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def marker_exists() -> bool:
    return os.path.exists(os.path.join(m.config.APPDATA_DIR, "hidhide_engaged.json"))


def done_file() -> dict:
    with open(os.path.join(m.config.APPDATA_DIR, "hidhide_cleanup_done.json"), encoding="utf-8") as f:
        return json.load(f)


# ── HidHide ───────────────────────────────────────────────────────────
def test_a_marker_from_an_engaged_mapper_is_undone_exactly():
    hh = machine(devices=[PAD_LINK, THEIR_PAD], apps=[DS4W, VISPORA], cloak=True)
    marker(cloaked=False)
    r = HC.run_once()
    assert r["ok"] and r["done"] and r["attempts"] == 1, r
    assert hh.devices == [THEIR_PAD], "only the controller Vispora hid comes back"
    assert hh.apps == [DS4W]
    assert hh.cloak is False, "the cloak was off before the mapper turned it on"
    assert hh.ran("--dev-unhide", PAD_LINK), "the form the old mapper hid is un-hidden"
    assert not hh.ran(PAD), "the plain instance path was never Vispora's: it is never asked about"
    assert not marker_exists()
    d = done_file()
    assert d["done"] is True and d["attempts"] == 1 and d["last"] and isinstance(d["log"], list)
    assert HC.run_once() == {"ok": True, "skipped": "done"}


def test_the_cloak_stays_on_when_it_was_on_before():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA], cloak=True)
    marker(cloaked=True)
    assert HC.run_once()["ok"]
    assert hh.cloak is True and not hh.ran("--cloak-off") and not hh.ran("--cloak-on")
    assert hh.devices == [] and hh.apps == []


def test_a_device_the_user_had_hidden_before_stays_hidden():
    # The user hid their pad with HidHide's own client; the mapper then added
    # its own (bogus) form of it on top.
    hh = machine(devices=[PAD, PAD_LINK], apps=[VISPORA])
    marker(prior_devices=[PAD])
    assert HC.run_once()["ok"]
    assert hh.devices == [PAD]
    assert not hh.ran("--dev-unhide", PAD), "the user's own entry is never even asked about"


def test_an_instance_path_another_tool_hid_after_the_mapper_engaged_stays_hidden():
    # The mapper engaged with nothing hidden; DSX (or the user) then hid the
    # same pad by its instance path, and Vispora was killed with the marker
    # left behind.  Only the \\?\hid#... form the mapper wrote comes back.
    hh = machine(devices=[PAD_LINK, PAD], apps=[VISPORA], cloak=True)
    marker(cloaked=False, prior_devices=[])
    r = HC.run_once()
    assert r["ok"] and r["done"], r
    assert hh.devices == [PAD], "the other tool's entry stays"
    assert not hh.ran(PAD), "and is never passed to the CLI"
    assert hh.apps == [] and hh.cloak is False and not marker_exists()

    # The same with an entry that differs from the marker's id only in case,
    # on a CLI that removes by exact case: done in one run, not a retry loop.
    their_pad = r"HID\VID_054C&PID_0CE6\6&1BCE44CB&0&0000"
    hh = machine(devices=[PAD_LINK, their_pad], apps=[VISPORA])
    hh.exact = True
    marker(prior_devices=[])
    r = HC.run_once()
    assert r["ok"] and r["done"] and r["attempts"] == 1, r
    assert hh.devices == [their_pad] and not hh.ran(their_pad) and not hh.ran(PAD)
    assert not marker_exists() and done_file()["done"] is True


def test_without_a_marker_only_vispora_entries_go_and_the_cloak_is_left_alone():
    hh = machine(devices=[PAD_LINK, PAD_USB_LINK, THEIR_PAD, THEIR_LINK],
                 apps=[DS4W, VISPORA, OLD_NAME, PYTHON], cloak=True)
    r = HC.run_once()
    assert r["ok"] and r["done"], r
    assert hh.devices == [THEIR_PAD, THEIR_LINK]
    assert hh.apps == [DS4W, PYTHON], "python.exe is only removed when a marker says Vispora added it"
    assert hh.cloak is True and not hh.ran("--cloak-off") and not hh.ran("--cloak-state")
    for foreign in (THEIR_PAD, THEIR_LINK, DS4W, PYTHON):
        assert not hh.ran(foreign), f"{foreign} was touched"


def test_a_marker_can_say_a_source_run_registered_python():
    hh = machine(devices=[], apps=[DS4W, PYTHON], cloak=True)
    marker(cloaked=False, hidden_ours=(), app=PYTHON)
    assert HC.run_once()["ok"]
    assert hh.apps == [DS4W] and hh.cloak is False


def test_older_clis_that_print_bare_lines_are_understood():
    hh = machine(devices=[PAD_LINK, THEIR_PAD], apps=[DS4W, VISPORA], raw=True)
    r = HC.run_once()
    assert r["ok"] and r["done"], r
    assert hh.devices == [THEIR_PAD] and hh.apps == [DS4W]


def test_the_listing_parser():
    text = ('--dev-hide "HID\\VID_054C&PID_0CE6\\6&1bce44cb&0&0000"\r\n'
            '--dev-hide "\\\\?\\hid#vid_054c&pid_0ce6#6&1bce44cb&0&0000"\r\n'
            '--app-reg "C:\\Program Files\\Vispora\\Vispora.exe"\r\n')
    assert HC._parse_entries(text, "--dev-hide") == [PAD, PAD_LINK]
    assert HC._parse_entries(text, "--app-reg") == [VISPORA]
    assert HC._parse_entries("C:\\Program Files\\Vispora\\Vispora.exe\n\n", "--app-reg") == [VISPORA]
    assert HC._parse_entries("--app-reg C:\\Users\\O'Neil\\Vispora.exe", "--app-reg") == \
        ["C:\\Users\\O'Neil\\Vispora.exe"], "an unbalanced quote does not break it"
    assert HC._parse_entries("", "--dev-hide") == []
    assert HC._cloak_is_on("--cloak-on") and not HC._cloak_is_on("--cloak-off")
    assert HC._to_symbolic_link(PAD) == PAD_LINK


def test_no_hidhide_means_nothing_to_do():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA])
    HC._find_cli = lambda: None
    try:
        assert HC.run_once() == {"ok": True, "skipped": "not installed"}
        assert hh.calls == []
        assert done_file()["done"] is True
        assert HC.run_once() == {"ok": True, "skipped": "done"}
    finally:
        HC._find_cli = lambda: CLI


def test_a_partial_failure_is_retried_next_start():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA], cloak=True)
    marker(cloaked=False)
    hh.refuse.add("--app-unreg")
    r = HC.run_once()
    assert r["ok"] is False and r["done"] is False and r["attempts"] == 1, r
    assert hh.devices == [] and hh.cloak is False, "what worked is done"
    assert marker_exists(), "kept for the next try"
    assert done_file()["done"] is False and done_file()["attempts"] == 1
    hh.refuse.clear()
    r = HC.run_once()
    assert r["ok"] and r["done"] and r["attempts"] == 2, r
    assert hh.apps == [] and not marker_exists()


def test_an_entry_that_survives_its_removal_counts_as_a_failure():
    hh = machine(devices=[PAD_USB_LINK], apps=[])
    hh.keep.add(PAD_USB_LINK)
    r = HC.run_once()
    assert r["ok"] is False and any("still hidden" in f for f in r["failed"]), r


def test_an_unreadable_list_is_retried_and_gives_up_after_five_attempts():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA])
    hh.refuse.add("--dev-list")
    for n in range(1, HC.MAX_ATTEMPTS + 1):
        r = HC.run_once()
        assert r["ok"] is False and r["attempts"] == n, r
    calls = len(hh.calls)
    assert HC.run_once() == {"ok": False, "skipped": "gave up", "attempts": HC.MAX_ATTEMPTS}
    assert len(hh.calls) == calls, "no more CLI calls once it gave up"
    assert done_file()["done"] is False and done_file()["attempts"] == HC.MAX_ATTEMPTS


def test_a_second_call_while_one_runs_returns_at_once():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA])
    entered, release = threading.Event(), threading.Event()
    hh.gate = (entered, release)
    first = {}
    t = threading.Thread(target=lambda: first.update(HC.run_once()), daemon=True)
    t.start()
    try:
        assert entered.wait(5)
        assert HC.run_once() == {"ok": True, "skipped": "running"}
    finally:
        hh.gate = None
        release.set()
        t.join(5)
    assert first.get("ok") and first.get("done"), first


def test_a_damaged_marker_or_done_file_does_not_stop_it():
    hh = machine(devices=[PAD_LINK], apps=[VISPORA], cloak=True)
    for name, text in (("hidhide_engaged.json", "{not json"), ("hidhide_cleanup_done.json", "[1, 2]")):
        with open(os.path.join(m.config.APPDATA_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    r = HC.run_once()
    assert r["ok"] and r["done"], r
    assert hh.devices == [] and hh.apps == []
    assert hh.cloak is True, "a marker that can't be read says nothing about the cloak"
    assert not marker_exists()


# ── the drivers are no longer managed dependencies ────────────────────
def test_the_old_driver_dependencies_are_gone_and_forgotten():
    m.fresh()
    from core import dependencies as D
    keys = [d["key"] for d in D.DEPENDENCIES]
    assert "vigembus" not in keys and "hidhide" not in keys and "presentmon2" in keys
    D._save_state({"opt_outs": ["vigembus", "ffmpeg", "hidhide"],
                   "pending_restart": {"set_at": 1.0, "by": ["vigembus", "hidhide"]}})
    st = D._load_state()
    assert st["opt_outs"] == ["ffmpeg"] and "pending_restart" not in st, st
    assert D.get_settings()["opt_outs"] == ["ffmpeg"]
    D._save_state({"pending_restart": {"set_at": 1.0, "by": ["hidhide", "presentmon2"]}})
    assert D._load_state()["pending_restart"] == {"set_at": 1.0, "by": ["presentmon2"]}
    D._save_state({"pending_restart": {"set_at": 1.0, "by": []}})
    assert D._load_state()["pending_restart"] == {"set_at": 1.0, "by": []}, "a restart nobody named stays"


# ── the foreground helper, moved from the mapper to core/utils.py ─────
def test_the_updater_sees_a_game_in_front_through_the_moved_helper():
    m.fresh()
    saved = utils.foreground_exe_name
    try:
        utils.foreground_exe_name = lambda: "cs2.exe"
        assert UP.is_safe_to_install() == {"safe": False, "reason": "game in foreground"}
        utils.foreground_exe_name = lambda: "vispora.exe"
        assert UP.is_safe_to_install() == {"safe": False, "reason": "Vispora window in foreground"}
        assert IS._is_own_window_foreground() is True
        utils.foreground_exe_name = lambda: "notepad.exe"
        assert UP._is_game_foreground() is False and IS._is_own_window_foreground() is False
    finally:
        utils.foreground_exe_name = saved


def test_the_foreground_helper_never_raises():
    saved_path, saved_api = utils.foreground_exe_path, utils._fg_api
    try:
        utils.foreground_exe_path = lambda: r"C:\Games\Counter-Strike\CS2.EXE"
        assert utils.foreground_exe_name() == "cs2.exe"
        utils.foreground_exe_path = lambda: ""
        assert utils.foreground_exe_name() == ""
        utils.foreground_exe_path = saved_path

        class Broken:
            def __getattr__(self, name):
                raise OSError("no user32 here")

        utils._fg_api = (None, None, Broken(), Broken())
        assert utils.foreground_exe_path() == "" and utils.foreground_exe_name() == ""
        utils._fg_api = saved_api
        name = utils.foreground_exe_name()          # the real one: read only
        assert isinstance(name, str) and name == name.lower()
    finally:
        utils.foreground_exe_path, utils._fg_api = saved_path, saved_api
