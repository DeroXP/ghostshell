"""What sits next to Adaptive Tuning and has to share the graphics card with it
(core/gpu_overclock.py, core/idle_mode.py, core/temp_ceiling.py) plus the
PresentMon bookkeeping (core/dependencies.py, core/frame_telemetry.py).

The card is always a stand-in here: nothing reads or writes a real clock, the
event log is the sandbox's (m.EVENTS), and the drift watchdog only ticks when
a test runs one tick by hand."""
import sandbox as m

import json
import os
import re
import subprocess
import sys
import threading
import time
import types

GO = m.app("core.gpu_overclock")
IM = m.app("core.idle_mode")
D = m.app("core.dependencies")
FT = m.app("core.frame_telemetry")
TC = m.app("core.temp_ceiling")
NOTIFIER = m.app("core.notifier")
AT = m.app("core.adaptive_tuning")      # the real engine: if it cannot be imported, this file must not pass

LIMITS = {"core_max_mhz": 2900, "mem_max_mhz": 14000, "power_default_w": 300, "power_max_w": 350,
          "power_min_w": 150}
LIVE_CSV = "2800, 14000, 70, 250.5, 99, 60, 55, 300.00, 2800, 14000, 2900, 14001"
REFUSAL = {"ok": False, "refused": "adaptive_session",
           "err": "Close the game first. A test can't run while a game is using the graphics card."}
BANNED = re.compile(r"offset|tdr|blacklist|baseline|yielded|stock|strike", re.I)
MINE = "adaptive_tuning"
EM_DASH = chr(0x2014)
STABLE = {"stable": True, "reason": "", "kind": "stable"}

_REAL = {n: getattr(GO, n) for n in (
    "detect_gpu_oc_capability", "detect_oc_tools_running", "verify_oc_applied", "_nvapi", "time", "apply_oc",
    "load_profile", "save_profile", "_reset_to_stock_hard", "_oc_watchdog_thread", "_activate_oc_watchdog",
    "_deactivate_oc_watchdog")}
_REAL_IM = {n: getattr(IM, n) for n in ("gpu_overclock", "_load", "_engage", "_a_game_is_running")}


class Card:
    """The graphics card as the app sees it through NVAPI: holds what was
    last written, and records every write and reset."""

    def __init__(self, current=(0, 0), write_ok=True, available=True):
        self.current, self.write_ok, self.available = tuple(current), write_ok, available
        self.writes, self.resets = [], 0
        self.on_read = self.on_write = self.on_reset = None

    def is_available(self):
        return {"ok": self.available, "err": "" if self.available else "nvapi64.dll not found"}

    def get_current_offsets(self):
        hook, self.on_read = self.on_read, None             # once
        if hook:
            hook()
        return {"ok": True, "core_offset_mhz": self.current[0], "mem_offset_mhz": self.current[1]}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        if self.on_write:
            self.on_write(core_offset_mhz, mem_offset_mhz)
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        if self.write_ok:
            self.current = (core_offset_mhz, mem_offset_mhz)
        return {"ok": self.write_ok, "struct_version_used": 3, "err": "" if self.write_ok else "driver said no"}

    def force_reset_all(self):
        if self.on_reset:
            self.on_reset()
        self.resets += 1
        self.current = (0, 0)
        return {"ok": True, "steps": [{"name": "Graphics P-state freqDelta -> 0", "ok": True, "err": ""}]}


class Clock:
    """The time module as gpu_overclock sees it: it only moves when told to."""

    def __init__(self):
        self.now = float(int(time.time()))

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


_REAL_TC_STATE = dict(TC._state)
_REAL_TOAST = NOTIFIER.toast


def restore():
    for name, value in _REAL.items():
        setattr(GO, name, value)
    for name, value in _REAL_IM.items():
        setattr(IM, name, value)
    GO.__dict__.pop("open", None)
    GO._deactivate_oc_watchdog()
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    for sess in (GO._stability_session, GO._auto_oc_session, GO._benchmark_oc_session, GO._benchmark_state):
        sess["active"] = False
    GO._benchmark_state.update(phase="idle", saved_profile=None)
    GO.clear_crash_state()
    GO._tuning_window.update(open=False, start=0.0, last=0.0, saved=0.0)
    del GO._tuning_windows_unsaved[:]
    GO._timeline_pending.clear()
    IM._state.update(engaged=False, restore=None)
    TC._state.update(_REAL_TC_STATE)
    NOTIFIER.toast = _REAL_TOAST


def rig(card=None, vendor="nvidia", clock=False):
    """A clean machine with an NVIDIA card the app can write (the stand-in)."""
    m.fresh()
    restore()
    m.CMD_HOOKS.append(lambda a: (m.R(LIVE_CSV) if a[1].startswith("--query-gpu=clocks.current") else m.R())
                       if a[0] == "nvidia-smi" else None)
    GO.detect_gpu_oc_capability = lambda: {"ok": True, "vendor": vendor, "limits": dict(LIMITS)}
    GO.detect_oc_tools_running = lambda: {"conflict_likely": False, "tools": []}
    GO.verify_oc_applied = lambda *a, **k: {"verified": True, "warnings": []}
    GO._oc_watchdog_thread = types.SimpleNamespace(is_alive=lambda: True)    # no thread: ticks are run by hand
    GO._nvapi = card if card is not None else Card()
    if clock:
        GO.time = Clock()
    return GO._nvapi


def rig_tuner(clock=True):
    """rig() for driving Quick Tune and Benchmark Tune: no saved profile, and
    their own applies and resets only answer."""
    card = rig(clock=clock)
    GO.load_profile = lambda: {"exists": False}
    GO.apply_oc = lambda core_offset_mhz=0, mem_offset_mhz=0, power_pct=100: {"ok": True}
    GO._reset_to_stock_hard = lambda attempts=3: (True, {})
    return card


_ABSENT = object()


class at_hooks:
    """core.adaptive_tuning for one test.  `session` and `engage` stand in for
    session_active and try_engage_running_game; None removes the function,
    which is how the module looks before the new engine is in."""

    def __init__(self, session=None, engage=None):
        self.want = {"session_active": session, "try_engage_running_game": engage}

    def __enter__(self):
        self.saved = {n: AT.__dict__.get(n, _ABSENT) for n in self.want}
        for name, fn in self.want.items():
            if fn is not None:
                setattr(AT, name, fn)
            elif name in AT.__dict__:
                delattr(AT, name)
        return self

    def __exit__(self, *exc):
        for name, old in self.saved.items():
            if old is not _ABSENT:
                setattr(AT, name, old)
            elif name in AT.__dict__:
                delattr(AT, name)


def tick(card, during=None):
    """One watchdog tick, by hand.  `during` runs in the middle of it: after
    it has read its target, when it reads the card."""
    stop = threading.Event()

    def mid_tick():
        try:
            if during:
                during()
        finally:
            stop.set()
    card.on_read = mid_tick
    GO._oc_watchdog_loop(stop)
    assert card.on_read is None, "the watchdog did not look at the card (not armed, or paused)"


def probe(seconds=10):
    """One stress run as the GPU page drives it: start, a tick, end."""
    r = GO.start_stability_probe(expected_core_max=2900)
    assert r["ok"], r
    GO.time.sleep(seconds)
    GO.run_probe_tick(frame_time_ms=10.0)
    return GO.end_stability_probe()


def saved_windows():
    with open(GO.TUNING_WINDOWS_PATH, encoding="utf-8") as f:
        return json.load(f)["windows"]


def saved_timeline():
    with open(GO.OC_TIMELINE_PATH, encoding="utf-8") as f:
        return json.load(f)["entries"]


# ── apply_oc and the drift watchdog ───────────────────────────────────
def test_the_watchdog_target_moves_before_the_write_and_under_its_lock():
    """apply_oc() wrote the new clocks first and told the watchdog last, after
    the slow verification.  Both now happen in one go under the lock the
    watchdog's re-assert takes."""
    card = rig()
    seen = {}

    def on_write(core, mem):
        seen["target"] = GO.get_applied_oc()
        other = []

        def try_lock():
            got = GO._active_oc_lock.acquire(blocking=False)
            if got:
                GO._active_oc_lock.release()
            other.append(got)
        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        seen["lock_free"] = other[0]
    card.on_write = on_write
    try:
        r = GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)
        assert r["ok"] and card.writes == [(450, 2225)]
        assert seen["target"] == {"core": 450, "mem": 2225, "power": 100, "active": True}, \
            "the watchdog still had its old target when the new clocks were written"
        assert seen["lock_free"] is False, "the write was not made under the watchdog's lock"

        # a write the driver refuses leaves the watchdog on what was asked for, as before
        card.on_write, card.write_ok = None, False
        r = GO.apply_oc(core_offset_mhz=500, mem_offset_mhz=2300, power_pct=100)
        assert r["ok"] is False and GO.get_applied_oc()["core"] == 500

        # power only (the idle park): armed with no clocks to hold
        card.write_ok = True
        GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=70)
        assert GO.get_applied_oc() == {"core": 0, "mem": 0, "power": 70, "active": True}

        # stock: disarmed, and it stays that way
        GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100)
        assert GO.get_applied_oc()["active"] is False and card.current == (0, 0)
    finally:
        restore()


def test_a_watchdog_tick_during_an_apply_does_not_put_the_old_clocks_back():
    """The owner's log, ten times: Idle Mode had parked the card (watchdog
    armed with +0 / +0), a game started, apply_oc() wrote +450 / +2225, and a
    watchdog tick in the same second saw "drift" from its old target and
    wrote +0 / +0 back.  The game ran without the overclock for up to 5 s and
    the verification blamed the driver."""
    try:
        # the fixture can show the fault: a write the watchdog was not told about is undone
        card = rig()
        GO._activate_oc_watchdog(0, 0, 70)
        tick(card, during=lambda: card.set_offsets(core_offset_mhz=450, mem_offset_mhz=2225))
        assert card.writes == [(450, 2225), (0, 0)] and card.current == (0, 0)

        # as it happened: the tick comes while apply_oc() is still verifying what it wrote
        card = rig()
        GO._activate_oc_watchdog(0, 0, 70)
        GO.verify_oc_applied = lambda *a, **k: tick(card) or {"verified": True, "warnings": []}
        assert GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)["ok"]
        assert card.writes == [(450, 2225)], f"the watchdog wrote its old target back: {card.writes}"
        assert card.current == (450, 2225)

        # and a tick that was already under way when the apply came
        card = rig()
        GO._activate_oc_watchdog(0, 0, 70)
        tick(card, during=lambda: GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100))
        assert card.writes == [(450, 2225)], f"the watchdog wrote its old target back: {card.writes}"
        assert card.current == (450, 2225)
        assert GO.get_applied_oc() == {"core": 450, "mem": 2225, "power": 100, "active": True}

        # and it still does its job afterwards: a drop to 0 is put right
        card.current = (0, 0)
        tick(card)
        assert card.writes[-1] == (450, 2225) and card.current == (450, 2225)
    finally:
        restore()


def test_a_reset_still_disarms_the_watchdog_before_it_touches_the_card():
    """reset_oc() and the emergency reset already had the right order; this
    keeps it there."""
    card = rig()
    try:
        for reset in (GO.reset_oc, lambda: GO.emergency_reset_oc(reason="temp_ceiling:86c")):
            GO._activate_oc_watchdog(150, 800, 100)
            card.current = (150, 800)
            armed_at_reset = []
            card.on_reset = lambda: armed_at_reset.append(GO.get_applied_oc()["active"])
            assert reset()["ok"]
            assert armed_at_reset == [False], "the watchdog was still armed when the card was reset"
            assert card.current == (0, 0) and card.writes == []
    finally:
        restore()


# ── who wrote the clocks ──────────────────────────────────────────────
def test_every_clock_write_that_is_not_adaptive_tunings_own_is_counted():
    """A game session tells "someone else changed the overclock" from "the
    driver dropped it" by this count."""
    rig()

    def moved(fn):
        before = GO.external_write_seq()
        fn()
        return GO.external_write_seq() - before
    try:
        assert moved(lambda: GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=100)) == 1
        assert moved(lambda: GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=100, owner=MINE)) == 0
        assert moved(lambda: GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0, power_pct=None, owner=MINE)) == 0
        assert moved(lambda: GO.write_offsets(100, 400)) == 1
        assert moved(lambda: GO.write_offsets(100, 400, owner=MINE)) == 0
        assert moved(GO.reset_oc) == 1
        assert moved(lambda: GO.emergency_reset_oc(reason="temp_ceiling:86c")) == 1, "a ceiling trip counts"
        assert moved(GO.delete_profile) >= 1
        assert moved(lambda: (GO.get_applied_oc(), GO.get_current_offsets(), GO.held_at(time.time()),
                              GO.tuning_windows(), GO.is_tuning_active())) == 0, "reading counts nothing"
    finally:
        restore()


def test_the_count_moves_before_the_card_is_touched():
    """A session that looks while a slow apply is still on its way must
    already see it."""
    card = rig()
    try:
        at_write = []
        card.on_write = lambda core, mem: at_write.append(GO.external_write_seq())
        card.on_reset = lambda: at_write.append(GO.external_write_seq())
        before = GO.external_write_seq()
        GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=100)
        GO.write_offsets(100, 400)
        GO.reset_oc()
        assert at_write == [before + 1, before + 2, before + 3]
    finally:
        restore()


def test_a_write_that_is_refused_is_not_counted():
    card = rig(vendor="amd")
    try:
        before = GO.external_write_seq()
        assert GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=100)["ok"] is False
        GO._nvapi = Card(available=False)
        assert GO.write_offsets(100, 400)["ok"] is False
        assert GO.external_write_seq() == before and card.writes == []
    finally:
        restore()


def test_the_temperature_ceilings_own_resets_are_counted_apart():
    """A game session held for heat took the ceiling's second reset (and one
    it saw under way) for the user's own change.  The ceiling's resets are
    now counted on their own as well, in the same step as the total."""
    card = rig()
    try:
        total, ceiling = GO.external_write_counts()
        assert (total, ceiling) == (GO.external_write_seq(), GO.ceiling_reset_seq())
        at_reset = []
        card.on_reset = lambda: at_reset.append(GO.external_write_counts())
        GO.emergency_reset_oc(reason="temp_ceiling:86c")
        assert GO.external_write_counts() == (total + 1, ceiling + 1)
        assert at_reset == [(total + 1, ceiling + 1)], "both moved before the card was touched"
        for other in (lambda: GO.emergency_reset_oc(reason="gpu_crash:tdr"), GO.reset_oc,
                      lambda: GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=100),
                      lambda: GO.write_offsets(100, 400)):
            other()
        assert GO.external_write_counts() == (total + 5, ceiling + 1), "only the ceiling's resets"
        GO.write_offsets(100, 400, owner=MINE)
        GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=None, owner=MINE)
        assert GO.external_write_counts() == (total + 5, ceiling + 1), "Adaptive Tuning's own: neither"
    finally:
        restore()


def test_a_trip_of_the_temperature_ceiling_is_on_record_before_its_reset():
    """The trip was recorded after the reset, the toast and the trip log: a
    look in between saw the reset and no trip.  "Notify only" was recorded
    like a reset although the card was left as it was."""
    card = rig(Card(current=(450, 2225)))
    toasts = []
    NOTIFIER.toast = lambda title, msg, force=False: toasts.append(msg) or {"ok": True}
    settings = dict(TC.DEFAULT_SETTINGS)
    try:
        GO._activate_oc_watchdog(450, 2225, 100)
        seen = []
        card.on_reset = lambda: seen.append((TC.get_status(), GO.ceiling_reset_seq()))
        ceiling_before = GO.ceiling_reset_seq()
        t = time.time()
        TC._do_trip(86, dict(settings, action="reset"))
        assert card.resets == 1 and card.current == (0, 0)
        status, ceiling_at_reset = seen[0]
        assert status["last_trip_at"] is not None and status["last_trip_at"] >= t
        assert status["last_trip_action"] == "reset" and status["cooldown_remaining_s"] > 0
        assert ceiling_at_reset == ceiling_before + 1
        after = TC.get_status()
        assert after["last_trip_action"] == "reset" and after["tripped_count"] == status["tripped_count"] + 1
        assert after["cooldown_remaining_s"] >= 59, "the cooldown counts from the end of the reset"

        # notify only: on record as such, and nothing is written
        card.current = (450, 2225)
        seq = GO.external_write_counts()
        TC._do_trip(86, dict(settings, action="notify"))
        assert card.resets == 1 and card.current == (450, 2225) and GO.external_write_counts() == seq
        status = TC.get_status()
        assert status["last_trip_action"] == "notify" and status["cooldown_remaining_s"] > 0
        assert set(status) >= {"tripped_count", "last_trip_at", "last_trip_temp_c", "cooldown_remaining_s"}
        assert len(toasts) == 2
    finally:
        restore()


def hold_back(step):
    """A CMD_HOOKS hook that keeps every nvidia-smi call whose arguments
    start with `step` waiting until `.go` is set; `.at` is set once one waits."""
    at, go = threading.Event(), threading.Event()

    def hook(a):
        if a[0] == "nvidia-smi" and a[1:2] == [step]:
            at.set()
            go.wait(10)
        return None
    hook.at, hook.go = at, go
    return hook


def test_a_clock_write_under_way_can_be_waited_for():
    """apply_oc() and reset_oc() count themselves at once and write the card
    seconds later.  A game found in between took "before" from the old values
    and then wrote over the user's Apply or Reset.  Adaptive Tuning can now
    wait until no write but its own is under way."""
    card = rig()
    try:
        assert GO.wait_external_writes(0) is True, "nothing under way"
        for start, step in ((lambda: GO.apply_oc(core_offset_mhz=200, mem_offset_mhz=900, power_pct=100), "-pm"),
                            (GO.reset_oc, "-rgc"),
                            (lambda: GO.emergency_reset_oc(reason="gpu_crash:tdr"), "-rgc"),
                            (GO.delete_profile, "-rgc")):
            hook = hold_back(step)
            m.CMD_HOOKS.insert(0, hook)
            t = threading.Thread(target=start, daemon=True)
            t.start()
            assert hook.at.wait(5)
            t0 = time.time()
            assert GO.wait_external_writes(0.1) is False and time.time() - t0 < 2
            got = []
            waiter = threading.Thread(target=lambda: got.append(GO.wait_external_writes(10)), daemon=True)
            waiter.start()
            hook.go.set()
            waiter.join(5)
            t.join(5)
            assert got == [True] and GO.wait_external_writes(0) is True
            m.CMD_HOOKS.remove(hook)
        assert card.writes == [(200, 900)] and card.resets == 3, "each of them went through"

        # Adaptive Tuning's own writes are not waited for (it waits under its own lock)
        hook = hold_back("-pm")
        m.CMD_HOOKS.insert(0, hook)
        for own in (lambda: GO.apply_oc(450, 2225, None, MINE), lambda: GO.apply_oc(core_offset_mhz=450, owner=MINE)):
            t = threading.Thread(target=own, daemon=True)
            t.start()
            assert hook.at.wait(5)
            assert GO.wait_external_writes(0) is True
            hook.go.set()
            t.join(5)
            hook.at.clear()
            hook.go.clear()
        m.CMD_HOOKS.remove(hook)

        # one that raises still ends, and bad timeouts never raise
        GO.detect_gpu_oc_capability = lambda: (_ for _ in ()).throw(OSError("nvidia-smi went away"))
        try:
            GO.apply_oc(core_offset_mhz=100)
        except OSError:
            pass
        assert GO.wait_external_writes(0) is True
        assert GO.wait_external_writes(None) is True and GO.wait_external_writes("soon") is True
    finally:
        restore()


def test_dropping_the_hold_leaves_the_card_and_the_counts_alone():
    """After a game, Adaptive Tuning hands back clocks the app was not holding
    before it (another tool's).  The hand-back's own apply armed the drift
    watchdog, which then fought every later change in the other tool."""
    card = rig(clock=True)
    try:
        GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=500, power_pct=100, owner=MINE)
        assert GO.get_applied_oc()["active"] is True
        seq, writes, resets = GO.external_write_counts(), list(card.writes), card.resets
        m.clear_logs()
        GO.time.now += 10
        assert GO.drop_hold() is None
        assert GO.get_applied_oc() == {"core": 0, "mem": 0, "power": 100, "active": False}
        assert card.current == (100, 500) and card.writes == writes and card.resets == resets
        assert m.CMDS == [] and m.PSC == [], "no power command, no process"
        assert GO.external_write_counts() == seq
        assert GO.held_at(GO.time.now) == (100, 500), "those clocks are still on the card"
        card.current = (0, 0)                               # the other tool takes them off
        idle_watchdog_pass()
        assert card.writes == writes, "nothing puts them back"
        GO.drop_hold()
        assert GO.get_applied_oc()["active"] is False, "twice is fine"
    finally:
        restore()


# ── write_offsets: the light path ─────────────────────────────────────
def test_write_offsets_is_one_driver_write_and_starts_no_process():
    """apply_oc() runs a PowerShell tool scan and several nvidia-smi calls;
    that is no way to re-assert or lower a clock in the middle of a game."""
    card = rig()
    try:
        m.clear_logs()
        r = GO.write_offsets(338, 1669, owner=MINE)
        assert r == {"ok": True, "err": ""}
        assert card.writes == [(338, 1669)] and card.resets == 0
        assert m.CMDS == [] and m.PSC == [], "a process was started"
        assert GO.get_applied_oc() == {"core": 338, "mem": 1669, "power": 100, "active": True}

        assert GO.write_offsets(core_offset_mhz=225, mem_offset_mhz=1113, owner=MINE)["ok"], "the long names work too"
        assert card.current == (225, 1113)
        GO.write_offsets(5000, 99999)
        assert card.current == (GO.MAX_CORE_OFFSET_MHZ, GO.MAX_MEM_OFFSET_MHZ), "clamped like apply_oc()"
        GO.write_offsets(-9000, -9000)
        assert card.current == (-500, -1000)
    finally:
        restore()


def test_write_offsets_leaves_the_power_limit_and_moves_the_watchdog_with_it():
    card = rig()
    try:
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=110)
        m.clear_logs()
        assert GO.write_offsets(338, 1669, owner=MINE)["ok"]
        assert GO.get_applied_oc() == {"core": 338, "mem": 1669, "power": 110, "active": True}, \
            "the limit in force is carried forward"
        assert not m.ran("nvidia-smi"), "the power limit was touched"

        # no overclock at a raised limit: still something for the watchdog to hold
        GO.write_offsets(0, 0, owner=MINE)
        assert GO.get_applied_oc() == {"core": 0, "mem": 0, "power": 110, "active": True} and card.current == (0, 0)

        # no overclock at the normal limit: nothing to hold
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)
        GO.write_offsets(0, 0, owner=MINE)
        assert GO.get_applied_oc()["active"] is False and card.current == (0, 0)
    finally:
        restore()


def test_write_offsets_says_when_the_card_did_not_take_the_write():
    try:
        card = rig(Card(write_ok=False))
        r = GO.write_offsets(338, 1669, owner=MINE)
        assert r["ok"] is False and r["err"] == "driver said no" and set(r) == {"ok", "err"}
        assert card.current == (0, 0)

        card = rig(Card(available=False))
        r = GO.write_offsets(338, 1669, owner=MINE)
        assert r == {"ok": False, "err": "nvapi64.dll not found"} and card.writes == []
        assert GO.get_applied_oc()["active"] is False, "nothing was written, so nothing is held"

        rig()
        GO._nvapi = None
        r = GO.write_offsets(338, 1669)
        assert r["ok"] is False and r["err"] and not BANNED.search(r["err"])
        r = GO.write_offsets("fast", None)
        assert r["ok"] is False and not BANNED.search(r["err"])

        class Broken(Card):
            def set_offsets(self, **k):
                raise OSError("nvapi64.dll went away")
        rig(Broken())
        r = GO.write_offsets(338, 1669)
        assert r["ok"] is False and "went away" in r["err"], "it never raises"
    finally:
        restore()


def test_write_offsets_gives_way_to_a_write_that_came_first():
    """`if_seq` closes the gap between a session seeing "nobody else wrote"
    and its own write: the check is made under the lock the write is made
    under."""
    card = rig()
    try:
        seq = GO.external_write_seq()
        assert GO.write_offsets(338, 1669, owner=MINE, if_seq=seq)["ok"]
        assert GO.write_offsets(225, 1113, owner=MINE, if_seq=seq)["ok"], "its own writes do not move the count"
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)       # the user, on the GPU page
        r = GO.write_offsets(225, 1113, owner=MINE, if_seq=seq)
        assert r["ok"] is False and r.get("superseded") is True and not BANNED.search(r["err"])
        assert card.current == (450, 2225) and GO.get_applied_oc()["core"] == 450, "the user's write stands"
    finally:
        restore()


def test_a_watchdog_tick_during_write_offsets_does_not_undo_it():
    card = rig(Card(current=(450, 2225)))
    try:
        GO._activate_oc_watchdog(450, 2225, 100)
        tick(card, during=lambda: GO.write_offsets(338, 1669, owner=MINE))
        assert card.writes == [(338, 1669)] and card.current == (338, 1669)
    finally:
        restore()


# ── what the app held on the card, over time ──────────────────────────
def test_held_at_follows_what_the_app_put_on_the_card():
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        assert GO.held_at(t0) is None, "nothing on record yet"
        clock.now = t0 + 10
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)
        clock.now = t0 + 20
        GO.write_offsets(338, 1669, owner=MINE)
        clock.now = t0 + 30
        GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=70)          # the idle park: no clocks held
        clock.now = t0 + 40
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)
        clock.now = t0 + 50
        GO.reset_oc()

        assert GO.held_at(t0 + 9) is None, "before the first line: not known"
        assert GO.held_at(t0 + 10) == (450, 2225) and GO.held_at(t0 + 19.9) == (450, 2225)
        assert GO.held_at(t0 + 25) == (338, 1669)
        assert GO.held_at(t0 + 35) == (0, 0)
        assert GO.held_at(t0 + 45) == (450, 2225)
        assert GO.held_at(t0 + 50) == (0, 0) and GO.held_at(t0 + 99999) == (0, 0)
        assert GO.held_at(None) is None and GO.held_at("yesterday") is None
        assert saved_timeline() == [
            {"ts": t0 + 10, "core": 450, "mem": 2225}, {"ts": t0 + 20, "core": 338, "mem": 1669},
            {"ts": t0 + 30, "core": 0, "mem": 0}, {"ts": t0 + 40, "core": 450, "mem": 2225},
            {"ts": t0 + 50, "core": 0, "mem": 0}]
    finally:
        restore()


DAY = 86400.0


def write_timeline(rows):
    with open(GO.OC_TIMELINE_PATH, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "entries": [{"ts": ts, "core": core, "mem": mem} for ts, core, mem in rows]}, f)


def test_the_timeline_only_grows_on_a_change_and_covers_at_least_100_days():
    """It kept the last 200 changes.  Someone who plays every day (a game
    start, a hand-back, an idle park and its end: several changes a day) had
    a record of a few weeks, and the health line's 90 days asked held_at()
    about times it no longer knew.  Now the lines of the last 100 days are
    kept, with the one in force when they began, at most 3000."""
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)
        for _ in range(5):                                  # the same values again: re-asserts, a re-apply
            clock.now += 20
            GO.write_offsets(450, 2225, owner=MINE)
            GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=110)
        assert len(saved_timeline()) == 1
        clock.now += 20
        GO.reset_oc()
        GO.reset_oc()
        GO.emergency_reset_oc(reason="again")
        assert len(saved_timeline()) == 2

        # a change a day for 130 days
        write_timeline([(t0 - (130 - i) * DAY, i, 0) for i in range(130)])
        clock.now = t0
        GO.write_offsets(900, 0, owner=MINE)
        rows = saved_timeline()
        assert len(rows) == 101 and rows[0]["core"] == 30 and rows[-1]["core"] == 900
        assert rows[0]["ts"] == t0 - 100 * DAY, "the line in force 100 days ago is kept"
        assert GO.held_at(t0 - 100 * DAY) == (30, 0) and GO.held_at(t0 - 99.5 * DAY) == (30, 0)
        assert GO.held_at(t0 - 89.5 * DAY) == (40, 0), "the health line's 90 days are all known"
        assert GO.held_at(t0 - 100.5 * DAY) is None, "older than the record goes back: not known"

        # 200 lines are no longer the limit; 3000 are
        write_timeline([(t0 - 10 * DAY + i * 60, i, 0) for i in range(3500)])
        clock.now = t0
        GO.write_offsets(901, 0, owner=MINE)
        rows = saved_timeline()
        assert len(rows) == 3000 and rows[0]["core"] == 501 and rows[-1]["core"] == 901
        assert GO.held_at(t0 + 99999) == (901, 0)
    finally:
        restore()


def busy(path):
    """open() as gpu_overclock sees it, with `path` held open by another
    program (a virus scanner, a backup tool): reading it fails for now."""
    real_open = open

    def opener(file, *a, **k):
        if isinstance(file, str) and os.path.abspath(file) == os.path.abspath(path):
            raise PermissionError(13, "The process cannot access the file because it is being used", file)
        return real_open(file, *a, **k)
    return opener


def test_held_at_is_not_known_when_the_record_cannot_be_read():
    """A damaged record was read as empty and saved over at the next change,
    with nothing set aside or said.  Now it is set aside first; one that
    merely cannot be opened just now is left alone."""
    rig(clock=True)
    clock = GO.time
    aside = os.path.splitext(GO.OC_TIMELINE_PATH)[0] + ".damaged.json"
    try:
        with open(GO.OC_TIMELINE_PATH, "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert GO.held_at(clock.now) is None
        assert not os.path.exists(aside), "reading changes nothing"
        clock.now += 5
        GO.apply_oc(core_offset_mhz=450, mem_offset_mhz=2225, power_pct=100)       # the next change starts it again
        assert GO.held_at(clock.now) == (450, 2225) and len(saved_timeline()) == 1
        with open(aside, encoding="utf-8") as f:
            assert f.read() == "{ not json", "the damaged record is kept, set aside"
        with open(GO.OC_TIMELINE_PATH, "w", encoding="utf-8") as f:
            json.dump([[clock.now - 100, 100, 200], "junk", [clock.now - 50]], f)   # an older shape, damaged rows
        assert GO.held_at(clock.now) == (100, 200)
        with open(GO.OC_TIMELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": "nothing"}, f)                     # JSON, but no record in it
        assert GO.held_at(clock.now) is None
        clock.now += 5
        GO.write_offsets(338, 1669, owner=MINE)
        assert [r["core"] for r in saved_timeline()] == [338]

        # the file is in use: nothing is saved over it, and the change waits for the next write
        before = saved_timeline()
        GO.open = busy(GO.OC_TIMELINE_PATH)
        clock.now += 5
        GO.write_offsets(225, 1113, owner=MINE)
        assert GO.held_at(clock.now) is None, "not known while it cannot be read"
        del GO.open
        assert saved_timeline() == before
        clock.now += 5
        GO.write_offsets(100, 500, owner=MINE)
        assert [r["core"] for r in saved_timeline()] == [338, 225, 100]
    finally:
        restore()


# ── when a test ran on the GPU page ───────────────────────────────────
def test_a_stability_test_is_on_record_from_its_start_to_its_end():
    """Tests crash the driver on purpose.  The health line leaves out the
    errors logged while one ran."""
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        with at_hooks():
            assert GO.tuning_windows() == []
            assert GO.start_stability_probe(expected_core_max=2900)["ok"]
            clock.now = t0 + 20
            GO.run_probe_tick(frame_time_ms=10.0)
            assert GO.tuning_windows(grace_s=0) == [(t0, t0 + 20)], "a test still running is already on record"
            clock.now = t0 + 60
            GO.end_stability_probe()
        assert GO.tuning_windows(grace_s=0) == [(t0, t0 + 60)]
        assert GO.tuning_windows() == [(t0, t0 + 180)], "errors in the 120 s after a test belong to it"
        assert saved_windows() == [[t0, t0 + 60]]
    finally:
        restore()


def test_a_tuning_run_and_its_probes_are_one_window():
    rig_tuner()
    clock = GO.time
    t0 = clock.now
    try:
        with at_hooks():
            assert GO.auto_oc_start(max_power=False)["ok"]
            clock.now += 5
            GO.auto_oc_next()
            for _ in range(3):
                clock.now += 1
                probe(seconds=10)
                clock.now += 1
                assert GO.auto_oc_next(dict(STABLE))["ok"]
            assert len(GO.tuning_windows()) == 1
            clock.now += 2
            GO.auto_oc_cancel()
            end = clock.now
            assert GO.tuning_windows(grace_s=0) == [(t0, end)] and end - t0 == 43

            # the stock-vs-OC benchmark: two probes, one window
            clock.now += 600
            b0 = clock.now
            assert GO.benchmark_start(saved_oc={"core_offset_mhz": 150, "mem_offset_mhz": 800, "power_pct": 100})["ok"]
            assert GO.benchmark_record_result(probe(seconds=30))["phase"] == "oc_measure"
            assert GO.benchmark_record_result(probe(seconds=30))["done"]
            assert GO.tuning_windows(grace_s=0) == [(t0, end), (b0, clock.now)]

            # Benchmark Tune
            clock.now += 600
            c0 = clock.now
            assert GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)["ok"]
            clock.now += 3
            GO.benchmark_oc_next()
            probe(seconds=10)
            clock.now += 1
            GO.benchmark_oc_cancel()
            assert GO.tuning_windows(grace_s=0) == [(t0, end), (b0, b0 + 62), (c0, clock.now)]
        assert saved_windows() == [[t0, end], [b0, b0 + 62], [c0, c0 + 14]]
    finally:
        restore()


def test_a_test_that_never_ended_is_on_record_up_to_its_last_sign_of_life():
    """The page went away, or the PC froze with the test's clocks on the
    card.  What is on disk must already cover it."""
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        with at_hooks():
            GO.start_stability_probe(expected_core_max=2900)
            assert saved_windows() == [[t0, t0]], "written when the test starts"
            for age in (10, 20, 31, 40, 50, 62):
                clock.now = t0 + age
                GO.run_probe_tick(frame_time_ms=10.0)
            assert saved_windows() == [[t0, t0 + 62]], "the saved end is never more than 30 s behind"
            clock.now = t0 + 70
            GO.run_probe_tick(frame_time_ms=10.0)
            assert saved_windows() == [[t0, t0 + 62]] and GO.tuning_windows(grace_s=0) == [(t0, t0 + 70)]

            # the next test, an hour later: the abandoned one ends where it was last heard from
            clock.now = t0 + 3600
            GO.start_stability_probe(expected_core_max=2900)
            clock.now += 15
            GO.run_probe_tick(frame_time_ms=10.0)
            GO.end_stability_probe()
        assert GO.tuning_windows(grace_s=0) == [(t0, t0 + 70), (t0 + 3600, t0 + 3615)]
    finally:
        restore()


def test_only_the_last_50_test_windows_are_kept():
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        with at_hooks():
            for _ in range(55):
                probe(seconds=10)
                clock.now += 90
        wins = GO.tuning_windows(grace_s=0)
        assert len(wins) == 50 and len(saved_windows()) == 50
        assert wins[0] == (t0 + 5 * 100, t0 + 5 * 100 + 10) and wins[-1] == (t0 + 54 * 100, t0 + 54 * 100 + 10)
        assert GO.tuning_windows_since() == t0 + 5 * 100, \
            "the five windows dropped are no longer on record: what came before the oldest one kept is not known"

        with open(GO.TUNING_WINDOWS_PATH, "w", encoding="utf-8") as f:
            f.write("not json at all")
        assert GO.tuning_windows() == [], "an unreadable record is no record"
    finally:
        restore()


def saved_record():
    with open(GO.TUNING_WINDOWS_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_the_test_windows_say_since_when_they_are_known():
    """3.7.0 kept no record of its tests.  After the update the driver errors
    its tests caused were counted as the user's own for 30 days.  The record
    now says when it began, and nothing before that is taken as known."""
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    aside = os.path.splitext(GO.TUNING_WINDOWS_PATH)[0] + ".damaged.json"
    try:
        # the first start of this version: no record yet, so it begins now
        assert not os.path.exists(GO.TUNING_WINDOWS_PATH)
        assert GO.tuning_windows_since() == t0
        assert saved_record() == {"version": 1, "since": t0, "windows": []}
        clock.now += 3600
        assert GO.tuning_windows_since() == t0, "asked again later: still when it began"
        with at_hooks():
            probe(seconds=10)
        assert GO.tuning_windows_since() == t0 and saved_record()["since"] == t0
        assert saved_windows() == [[t0 + 3600, t0 + 3610]]

        # a test that ran before anyone asked starts the record
        rig(clock=True)
        clock = GO.time
        t1 = clock.now
        with at_hooks():
            probe(seconds=10)
        assert saved_record()["since"] == t1 and GO.tuning_windows_since() == t1

        # a record written before it said when it began: it begins at its oldest window
        rig(clock=True)
        clock = GO.time
        with open(GO.TUNING_WINDOWS_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "windows": [[clock.now - 900, clock.now - 800], [clock.now - 500, clock.now - 400]]}, f)
        assert GO.tuning_windows_since() == clock.now - 900
        assert saved_record()["since"] == clock.now - 900 and len(saved_windows()) == 2

        # a damaged record is set aside, kept, and the record begins again now
        rig(clock=True)
        clock = GO.time
        with open(GO.TUNING_WINDOWS_PATH, "w", encoding="utf-8") as f:
            f.write('{"version": 1, "since": 12')
        assert GO.tuning_windows() == []
        clock.now += 60
        assert GO.tuning_windows_since() == clock.now
        with open(aside, encoding="utf-8") as f:
            assert f.read() == '{"version": 1, "since": 12'
        assert saved_record() == {"version": 1, "since": clock.now, "windows": []}

        # a record that cannot be opened just now: not known, and not saved over
        GO.open = busy(GO.TUNING_WINDOWS_PATH)
        assert GO.tuning_windows_since() is None
        del GO.open
        assert saved_record()["since"] == clock.now
    finally:
        restore()


def test_a_test_window_that_could_not_be_saved_is_not_lost():
    """The record is in use when a test ends: the window stays known and
    reaches the disk with the next save."""
    rig(clock=True)
    clock = GO.time
    t0 = clock.now
    try:
        with at_hooks():
            GO.start_stability_probe(expected_core_max=2900)
            clock.now = t0 + 20
            GO.run_probe_tick(frame_time_ms=10.0)
            GO.open = busy(GO.TUNING_WINDOWS_PATH)
            clock.now = t0 + 40
            GO.end_stability_probe()
            assert GO.tuning_windows(grace_s=0) == [(t0, t0 + 40)], "known while the file is in use"
            del GO.open
            assert saved_windows() == [[t0, t0]], "the end was not saved"
            assert GO.tuning_windows(grace_s=0) == [(t0, t0 + 40)], "but it is still known"
            clock.now = t0 + 1000
            probe(seconds=10)
        assert saved_windows() == [[t0, t0 + 40], [t0 + 1000, t0 + 1010]]
    finally:
        restore()


# ── is_tuning_active ──────────────────────────────────────────────────
def test_a_stability_test_whose_page_went_away_stops_counting_as_running():
    """Only end_stability_probe() cleared the flag.  Leave the page mid-test
    and Adaptive Tuning was told "a test is running" until the app was
    restarted: no game was looked after, Idle Mode never parked."""
    rig(clock=True)
    clock = GO.time
    try:
        with at_hooks():
            assert GO.is_tuning_active() is False
            GO.start_stability_probe(expected_core_max=2900)
            assert GO.is_tuning_active() is True
            clock.now += 179
            assert GO.is_tuning_active() is True
            clock.now += 2
            assert GO.is_tuning_active() is False, "no tick for 181 s and it still counts as running"
            GO.run_probe_tick(frame_time_ms=10.0)
            assert GO.is_tuning_active() is True, "the page is back"
            for _ in range(4):                               # a long test: every tick renews it
                clock.now += 100
                GO.run_probe_tick(frame_time_ms=10.0)
                assert GO.is_tuning_active() is True
            GO.end_stability_probe()
            assert GO.is_tuning_active() is False

            # the benchmark has the same hole
            GO.benchmark_start(saved_oc={"core_offset_mhz": 150, "mem_offset_mhz": 800, "power_pct": 100})
            assert GO.is_tuning_active() is True
            clock.now += 181
            assert GO.is_tuning_active() is False
            GO.benchmark_record_result({"avg_fps": 100.0})
            assert GO.is_tuning_active() is True, "a recorded result is a sign of life"
            GO.benchmark_cancel()
            assert GO.is_tuning_active() is False

        # a flag with no time on record counts: never guess that a test is over
        GO._stability_session.update(active=True, started=0.0, last_tick=0.0)
        assert GO.is_tuning_active() is True
    finally:
        restore()


def test_a_tuning_run_counts_until_it_ends():
    """Quick Tune and Benchmark Tune go minutes between steps and have their
    own clean-up for an abandoned run."""
    rig_tuner()
    clock = GO.time
    try:
        with at_hooks():
            GO.auto_oc_start(max_power=False)
            GO.auto_oc_next()
            clock.now += 400
            assert GO.is_tuning_active() is True
            GO.auto_oc_cancel()
            assert GO.is_tuning_active() is False
            GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)
            clock.now += 400
            assert GO.is_tuning_active() is True
            GO.benchmark_oc_cancel()
            assert GO.is_tuning_active() is False
    finally:
        restore()


def test_a_tuning_run_counts_as_running_from_its_start():
    """Quick Tune, Benchmark Tune and the benchmark marked themselves active
    only after a slow first step (a power limit write, a reset: seconds on a
    real PC).  A game found in those seconds was taken on by Adaptive Tuning
    and the run then went on under it."""
    seen = []
    try:
        card = rig_tuner()
        GO.apply_oc = lambda core_offset_mhz=0, mem_offset_mhz=0, power_pct=100: (
            seen.append(("apply", GO.is_tuning_active())) or {"ok": True, "verification": {"power": {"ok": True}}})
        card.on_reset = lambda: seen.append(("reset", GO.is_tuning_active()))
        with at_hooks(session=lambda: False):
            assert GO.auto_oc_start(max_power=True)["ok"]
            GO.auto_oc_cancel()
            assert GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=True)["ok"]
            GO.benchmark_oc_cancel()
            assert start_benchmark()["ok"]
            GO.benchmark_cancel()
        assert ("apply", True) in seen and ("reset", True) in seen
        assert all(active for _what, active in seen), seen
        assert GO.is_tuning_active() is False and GO._tuning_starts == 0

        # refused, or not started: never counted
        with at_hooks(session=lambda: True):
            assert GO.auto_oc_start(max_power=True) == REFUSAL
        assert GO.is_tuning_active() is False and GO._tuning_starts == 0
        with at_hooks(session=lambda: False):
            GO.detect_gpu_oc_capability = lambda: {"ok": False, "vendor": "amd", "limits": {}}
            assert GO.auto_oc_start(max_power=False)["ok"] is False
        assert GO.is_tuning_active() is False and GO._tuning_starts == 0

        # a start that raises: no longer counted, its window ends, and Adaptive Tuning may look again
        rig_tuner()
        told = []

        def broken(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100):
            raise OSError("nvidia-smi went away")
        GO.apply_oc = broken
        with at_hooks(session=lambda: False, engage=lambda: told.append(GO.is_tuning_active())):
            try:
                GO.auto_oc_start(max_power=True)
            except OSError:
                pass
        assert GO.is_tuning_active() is False and GO._tuning_starts == 0 and told == [False]
        assert GO._tuning_window["open"] is False
    finally:
        restore()


def idle_watchdog_pass(until=None):
    """Let the real watchdog loop run with nothing to hold, until `until` is
    true (or for a moment)."""
    stop = threading.Event()
    t = threading.Thread(target=GO._oc_watchdog_loop, args=(stop,), daemon=True)
    t.start()
    try:
        if until:
            assert m.wait(until, 5), "the watchdog never got round to it"
        else:
            time.sleep(0.15)
    finally:
        stop.set()
        t.join(5)
    assert not t.is_alive()


def test_an_abandoned_tuning_run_is_cleaned_up_even_with_nothing_on_the_card():
    """A run whose page went away is ended once its pause on the watchdog
    expires, but only the watchdog looked, and only while it had an overclock
    to hold.  A run that had just crashed the driver (card reset, nothing
    held) and was then left stayed "active" until the app was restarted."""
    rig_tuner()
    clock = GO.time
    resets, told = [], []
    GO._reset_to_stock_hard = lambda attempts=3: resets.append(attempts) or (True, {})
    try:
        with at_hooks(session=lambda: False, engage=lambda: told.append(GO.is_tuning_active())):
            assert GO.auto_oc_start(max_power=False)["ok"]
            GO.auto_oc_next()
            GO.emergency_reset_oc(reason="gpu_crash:context_loss")      # the step crashed; then the page was closed
            assert GO.get_applied_oc()["active"] is False

            clock.now += 300
            idle_watchdog_pass()
            assert GO.is_tuning_active() is True and resets == [] and told == [], "still within a step's time"

            clock.now += 301                                            # 10 minutes with no step
            idle_watchdog_pass(until=lambda: told)
            assert GO._auto_oc_session["active"] is False and GO.is_tuning_active() is False
            assert resets == [2] and told == [False], "ended once, and Adaptive Tuning told once"
            idle_watchdog_pass()
            assert resets == [2] and told == [False]

        # if a game session does hold the card by then, its clocks are not the run's to reset
        del resets[:]
        GO._auto_oc_session["active"] = True
        with at_hooks(session=lambda: True):
            GO._abandon_orphaned_tuning("auto_oc")
        assert resets == [] and GO._auto_oc_session["active"] is False
    finally:
        restore()


# ── the start-up apply ────────────────────────────────────────────────
def test_the_startup_apply_says_when_it_is_over():
    """A game already running when the app starts is picked up while the
    saved profile may still be on its way to the card.  Adaptive Tuning waits
    for this before it notes what to hand the card back to."""
    card = rig()
    profile = {"core_offset_mhz": 450, "mem_offset_mhz": 2225, "power_pct": 100, "apply_on_startup": True}
    try:
        GO._startup_apply_done.clear()
        t = time.time()
        assert GO.wait_startup_apply(0) is False and GO.wait_startup_apply(0.05) is False
        assert time.time() - t < 2 and GO.wait_startup_apply(None) is False
        GO.mark_startup_apply_done()
        assert GO.wait_startup_apply(0) is True and GO.wait_startup_apply(15) is True

        # someone already waiting is let go
        GO._startup_apply_done.clear()
        got = []
        waiter = threading.Thread(target=lambda: got.append(GO.wait_startup_apply(10)), daemon=True)
        waiter.start()
        GO.mark_startup_apply_done()
        waiter.join(5)
        assert got == [True]

        # the apply itself releases it, whichever way it ends
        GO._startup_apply_done.clear()
        assert GO.apply_saved_profile_on_startup()["applied"] is False and GO.wait_startup_apply(0), "no profile"

        GO.save_profile(dict(profile, apply_on_startup=False))
        GO._startup_apply_done.clear()
        assert GO.apply_saved_profile_on_startup()["applied"] is False and GO.wait_startup_apply(0), "switched off"

        GO.save_profile(profile)
        GO._startup_apply_done.clear()
        during = []
        card.on_write = lambda core, mem: during.append(GO.wait_startup_apply(0))
        assert GO.apply_saved_profile_on_startup()["applied"] is True and card.current == (450, 2225)
        assert during == [False] and GO.wait_startup_apply(0) is True, "released after the write, not before"

        GO._startup_apply_done.clear()
        GO.detect_gpu_oc_capability = lambda: {"ok": False, "vendor": "amd", "limits": {}}
        assert GO.apply_saved_profile_on_startup()["applied"] is False and GO.wait_startup_apply(0), "another vendor"

        GO._startup_apply_done.clear()

        def broken():
            raise OSError("the profile is locked")
        GO.load_profile = broken
        try:
            GO.apply_saved_profile_on_startup()
        except OSError:
            pass
        assert GO.wait_startup_apply(0) is True, "an apply that failed must not leave every game start waiting"
    finally:
        restore()


# ── the tests on the GPU page and a game session ──────────────────────
def start_benchmark():
    return GO.benchmark_start(saved_oc={"core_offset_mhz": 150, "mem_offset_mhz": 800, "power_pct": 100})


def test_the_four_tests_refuse_to_start_while_a_game_session_is_open():
    """Quick Tune, Benchmark Tune, the stability test and the benchmark all
    reset or rewrite the clocks.  Under a running game that Adaptive Tuning
    is looking after, none of them may start."""
    card = rig(Card(current=(338, 1669)))
    try:
        GO._activate_oc_watchdog(338, 1669, 100)
        seq = GO.external_write_seq()
        m.clear_logs()
        with at_hooks(session=lambda: True):
            for start in (GO.start_stability_probe, GO.auto_oc_start, GO.benchmark_oc_start, start_benchmark):
                assert start() == REFUSAL
        assert card.writes == [] and card.resets == 0 and card.current == (338, 1669)
        assert m.CMDS == [] and m.PSC == [], "nothing may even be asked of the card"
        assert GO.is_tuning_active() is False and GO.tuning_windows() == []
        assert GO.external_write_seq() == seq and GO.is_oc_watchdog_paused() is False
        assert GO.get_applied_oc() == {"core": 338, "mem": 1669, "power": 100, "active": True}
        assert not BANNED.search(REFUSAL["err"]) and EM_DASH not in REFUSAL["err"]
        assert GO.SESSION_REFUSAL == REFUSAL["err"]

        # a probe left flagged by a test whose page went away is not picked up by the
        # refused test's ticks (a page that does not look at the refusal sends them anyway)
        GO._stability_session.update(active=True, started=time.time() - 900, last_tick=time.time() - 900,
                                     aborted=False, samples=[])
        with at_hooks(session=lambda: True):
            assert GO.start_stability_probe(expected_core_max=2900) == REFUSAL
            assert GO.run_probe_tick(frame_time_ms=10.0, context_lost=True) == {"ok": False,
                                                                               "err": "No active probe session"}
            assert GO.end_stability_probe()["ok"] is False
        assert card.resets == 0 and card.current == (338, 1669) and GO.external_write_seq() == seq
    finally:
        restore()


def test_the_tests_start_as_before_when_no_session_is_open_or_it_cannot_be_asked():
    """The check must never be the reason a test cannot run: a module that
    has no session_active() yet, one that raises, and one that says no all
    let the test start."""
    def boom():
        raise RuntimeError("state file locked")
    try:
        for session in (None, boom, lambda: False, lambda: 0):
            rig_tuner()
            with at_hooks(session=session):
                assert GO.start_stability_probe(expected_core_max=2900) == {"ok": True, "target_core_max": 2900}
                GO.end_stability_probe()
                assert GO.auto_oc_start(max_power=False)["ok"]
                GO.auto_oc_cancel()
                assert GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)["ok"]
                GO.benchmark_oc_cancel()
                assert start_benchmark()["ok"]
                GO.benchmark_cancel()

        # not even loaded (nothing imported it): no session can be open
        rig_tuner()
        real = sys.modules.pop("core.adaptive_tuning")
        try:
            assert GO.start_stability_probe(expected_core_max=2900)["ok"]
            GO.end_stability_probe()
            assert "core.adaptive_tuning" not in sys.modules, "asking must not import it"
        finally:
            sys.modules["core.adaptive_tuning"] = real
    finally:
        restore()


def test_adaptive_tuning_is_told_when_a_test_ends():
    """A game that starts while a test runs is not looked after.  When the
    test ends, Adaptive Tuning gets the chance to pick it up: once per run,
    after the run stopped counting as active, and not for the probes that
    make up a longer run."""
    told = []
    crash = {"stable": False, "reason": "WebGL context lost: GPU driver reset", "kind": "context_loss"}
    try:
        rig_tuner()
        with at_hooks(session=lambda: False, engage=lambda: told.append(GO.is_tuning_active())):
            # the stability test
            GO.start_stability_probe(expected_core_max=2900)
            GO.run_probe_tick(frame_time_ms=10.0)
            assert told == []
            GO.end_stability_probe()
            assert told == [False]

            # Quick Tune, cancelled: its probes say nothing, the cancel does
            GO.auto_oc_start(max_power=False)
            GO.auto_oc_next()
            probe()
            GO.auto_oc_next(dict(STABLE))
            probe()
            assert len(told) == 1, "a step's probe is not the end of the run"
            GO.auto_oc_cancel()
            assert len(told) == 2

            # Quick Tune giving up after a crash
            GO.auto_oc_start(max_power=False)
            GO.auto_oc_next()
            r = GO.auto_oc_next(dict(crash))
            assert r["done"] and r["aborted"] and len(told) == 3

            # Quick Tune running to its end
            GO.auto_oc_start(max_power=False)
            r = GO.auto_oc_next()
            for _ in range(60):
                if r.get("done"):
                    break
                assert len(told) == 3
                r = GO.auto_oc_next(dict(STABLE))
            assert r["done"] and r.get("best") and len(told) == 4

            # Benchmark Tune: cancelled, then run to its end
            GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)
            GO.benchmark_oc_next()
            probe()
            assert len(told) == 4
            GO.benchmark_oc_cancel()
            assert len(told) == 5
            fps = [100.0]

            def verdict():
                fps[0] += 5
                return {"stable": True, "aborted": False, "abort_reason": "", "kind": "stable", "avg_fps": fps[0],
                        "avg_frame_time_ms": 1000.0 / fps[0], "p99_frame_time_ms": 12.0, "frame_variance_pct": 5.0,
                        "max_temp_c": 65, "avg_gpu_util_pct": 99, "frame_data_valid": True}
            GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)
            r = GO.benchmark_oc_next()
            for _ in range(20):
                if r.get("done"):
                    break
                assert len(told) == 5
                r = GO.benchmark_oc_next(verdict())
            assert r["done"] and r["ok"] and len(told) == 6

            # the benchmark: two probes and two results, told at the second
            start_benchmark()
            GO.benchmark_record_result(probe())
            assert len(told) == 6
            assert GO.benchmark_record_result(probe())["done"] and len(told) == 7
            start_benchmark()
            GO.benchmark_cancel()
            assert len(told) == 8

            # an abandoned tuning run that the app cleans up by itself
            GO.auto_oc_start(max_power=False)
            GO.auto_oc_next()
            GO._abandon_orphaned_tuning("auto_oc")
            assert len(told) == 9 and GO._auto_oc_session["active"] is False

            # nothing running: a cancel or an end is not a run ending
            GO.auto_oc_cancel()
            GO.benchmark_oc_cancel()
            GO.benchmark_cancel()
            assert GO.end_stability_probe()["ok"] is False
            assert len(told) == 9
        assert told == [False] * 9, "it was told while a test still counted as running"
    finally:
        restore()


def test_a_failure_in_adaptive_tuning_does_not_break_the_test_that_ended():
    def boom():
        raise RuntimeError("engine fell over")
    try:
        rig_tuner()
        with at_hooks(session=lambda: False, engage=boom):
            verdict = probe()
            assert verdict["ok"] and "stable" in verdict and GO.is_tuning_active() is False
            GO.auto_oc_start(max_power=False)
            GO.auto_oc_next()
            assert GO.auto_oc_cancel()["ok"]
    finally:
        restore()


def test_a_cancel_with_no_run_active_leaves_a_game_sessions_card_alone():
    """The page sends both cancels after a start that failed (so after a
    refusal too), and each cancel reset the card whether or not anything was
    running.  Under a game session that would take its overclock away."""
    card = rig(Card(current=(338, 1669)))
    try:
        GO._activate_oc_watchdog(338, 1669, 100)
        GO._benchmark_state["saved_profile"] = {"core_offset_mhz": 450, "mem_offset_mhz": 2225, "power_pct": 100}
        seq = GO.external_write_seq()
        with at_hooks(session=lambda: True):
            r = GO.auto_oc_cancel()
            assert r["ok"] and r["skipped"] == "adaptive_session" and r["reset_ok"] is None, r
            r = GO.benchmark_oc_cancel()
            assert r["ok"] and r["skipped"] == "adaptive_session" and r["reset_ok"] is None, r
            assert GO.benchmark_cancel() == {"ok": True, "skipped": "adaptive_session"}
        assert card.writes == [] and card.resets == 0 and card.current == (338, 1669)
        assert GO.external_write_seq() == seq
        assert GO.get_applied_oc() == {"core": 338, "mem": 1669, "power": 100, "active": True}

        # with no session the cancel still makes sure the card is clean, as it always did
        with at_hooks(session=lambda: False):
            r = GO.auto_oc_cancel()
        assert r["reset_ok"] is True and card.resets == 1 and card.current == (0, 0)
        assert GO.get_applied_oc()["active"] is False
    finally:
        restore()


# ── driver events ─────────────────────────────────────────────────────
def display_record(ts, driver="nvlddmkm", event_id=4101):
    """Windows' own record of a driver reset ("Display driver nvlddmkm stopped
    responding and has successfully recovered"): source Display, the driver's
    name as its first value.  Returns the record id."""
    record_id = max([r["record_id"] for r in m.EVENTS.records] + [5000]) + 1
    return m.EVENTS.add_xml("System", (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Display'/>"
        "<EventID Qualifiers='0'>%d</EventID><Version>0</Version><Level>3</Level>"
        "<TimeCreated SystemTime='%s'/><EventRecordID>%d</EventRecordID><Channel>System</Channel>"
        "<Computer>DESKTOP-TEST</Computer></System><EventData><Data>%s</Data><Data></Data></EventData></Event>"
        % (event_id, m.EVENTS.stamp(ts), record_id, driver)))


def test_windows_own_record_of_a_driver_reset_is_counted():
    """check_driver_events asked Get-WinEvent for five driver names.  For each
    name a PC does not have (and when nothing matched) PowerShell exits with
    code 1, and the answer was "could not be read" on every real PC: the
    tests on the GPU page and crash recovery never saw a driver error.  It now
    reads the event log in this process, the way Adaptive Tuning does, and
    counts the same records."""
    rig()

    def scan(*records, seconds_back=60):
        m.EVENTS.clear()
        now = GO.time.time()
        for n, (kind, extra) in enumerate(records):
            ts = now - len(records) + n
            if kind == "display":
                display_record(ts, **extra)
            else:
                m.EVENTS.add("driver_error", ts, **extra)
        r = GO.check_driver_events(seconds_back=seconds_back)
        assert r["ok"] and r["crash_count"] == 0 and set(r) == {"ok", "tdr_count", "crash_count", "events"}, r
        assert all(set(e) == {"id", "time", "level", "msg"} for e in r["events"]), r
        return r["tdr_count"], [e["id"] for e in r["events"]]
    try:
        assert scan(("display", {})) == (1, [4101])
        assert m.ps_ran("Get-WinEvent") == [] and m.PSC == [], "no PowerShell is started"
        channel, xpath, _flags = m.EVENTS.queries[-1]
        assert channel == "System" and "'Display'" in xpath and "'nvlddmkm'" in xpath

        assert scan(("nv", {})) == (1, [153]), "the driver's own record, as before"
        assert scan(("nv", {}), ("display", {})) == (2, [4101, 153]), "one reset is often logged as both"
        assert scan(("nv", {"event_id": 13}), ("nv", {"event_id": 14})) == (2, [14, 13]), \
            "13 and 14 count, as they do for Adaptive Tuning"
        assert scan(("display", {"driver": "igfx"})) == (0, []), "another display driver's reset is not this card's"
        assert scan(("display", {"driver": ""})) == (0, []), "nor one that names no driver (as Adaptive Tuning reads it)"
        assert scan(("display", {"event_id": 4107})) == (0, []), "only the reset record of that source counts"
        assert scan() == (0, [])
        assert scan(*[("nv", {})] * 7) == (7, [153] * 5), "every error is counted, the latest five listed"

        m.EVENTS.clear()
        m.EVENTS.add("driver_error", GO.time.time() - 120)
        assert GO.check_driver_events(seconds_back=60)["tdr_count"] == 0, "older than asked: not counted"
        assert GO.check_driver_events(seconds_back=180)["tdr_count"] == 1
        r = GO.check_driver_events(seconds_back=180)
        assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", r["events"][0]["time"]), r
        assert r["events"][0]["level"] == "Error" and "GPUID" in r["events"][0]["msg"]

        m.EVENTS.fail = True
        r = GO.check_driver_events(seconds_back=60)
        assert r["ok"] is False and r["tdr_count"] == 0 and r["events"] == [] and r["err"], r
    finally:
        restore()


def test_a_probe_fails_on_a_reset_that_only_windows_logged():
    card = rig()
    try:
        with at_hooks():
            GO._activate_oc_watchdog(450, 2225, 100)
            card.current = (450, 2225)
            GO.start_stability_probe(expected_core_max=2900)
            display_record(time.time())
            ticks = [GO.run_probe_tick(frame_time_ms=10.0) for _ in range(5)]
            assert ticks[-1]["kind"] == "tdr", ticks[-1]
            assert card.resets == 1 and card.current == (0, 0), "the clocks that reset the driver come off"
            assert GO.end_stability_probe()["stable"] is False
    finally:
        restore()


# ── Idle Mode ─────────────────────────────────────────────────────────
def rig_idle():
    """Idle Mode with a stand-in card API that records what it would apply."""
    applied = []
    IM.gpu_overclock = types.SimpleNamespace(
        get_applied_oc=lambda: {"core": 338, "mem": 1669, "power": 100},
        detect_gpu_oc_capability=lambda: {"ok": True, "vendor": "nvidia"},
        apply_oc=lambda core_offset_mhz=0, mem_offset_mhz=0, power_pct=100:
            applied.append((core_offset_mhz, mem_offset_mhz, power_pct)) or {"ok": True},
        is_tuning_active=lambda: False)
    IM._state.update(engaged=False, restore=None)
    IM._save({**IM._DEFAULTS, "enabled": True, "switch_power_plan": False, "park_oc": True, "idle_power_pct": 70})
    return applied


def one_idle_poll():
    """Run the watcher for one poll with no game in sight and no grace period."""
    stop = threading.Event()
    polls = []

    def no_game_then_stop():
        polls.append(1)
        if len(polls) > 1:
            stop.set()
            return True
        return False
    IM._a_game_is_running = no_game_then_stop
    IM._load = lambda: {**IM._DEFAULTS, "enabled": True, "grace_minutes": 0, "poll_seconds": 0,
                        "switch_power_plan": False, "park_oc": True, "idle_power_pct": 70}
    engages = []
    IM._engage = lambda: engages.append(1)
    IM._loop(stop)
    return len(engages)


def test_idle_mode_does_not_park_while_a_game_session_is_open():
    """Parking writes no overclock and a lower power limit.  Under a game
    session that is somebody else writing the clocks: Adaptive Tuning would
    step back and the game would run without its overclock."""
    def boom():
        raise RuntimeError("state file locked")
    rig()
    try:
        # the watcher's own check
        with at_hooks(session=lambda: True):
            assert one_idle_poll() == 0
        with at_hooks(session=boom):
            assert one_idle_poll() == 0, "when it cannot tell, it does not park"
        with at_hooks(session=lambda: False):
            assert one_idle_poll() == 1
        with at_hooks():
            assert one_idle_poll() == 1, "no session_active() yet: as before"
        IM.gpu_overclock = types.SimpleNamespace(is_tuning_active=lambda: True)
        with at_hooks(session=lambda: False):
            assert one_idle_poll() == 0, "and still not during a test on the GPU page"
        restore()

        # and the park itself, whoever calls it
        applied = rig_idle()
        with at_hooks(session=lambda: True):
            IM._engage()
        assert applied == [] and IM._state["engaged"] is False and IM._state["restore"] is None
        with at_hooks(session=lambda: False):
            IM._engage()
        assert applied == [(0, 0, 70)] and IM._state["engaged"] is True
        assert IM._state["restore"]["oc"] == {"core": 338, "mem": 1669, "power": 100}
    finally:
        restore()


def test_the_idle_unpark_is_not_undone_by_the_watchdog():
    """The real sequence from the owner's log: parked on the desktop
    (watchdog armed with +0 / +0 at 70 % power), a game starts, Idle Mode
    puts +450 / +2225 back, and the watchdog tick that was already under way
    wrote +0 / +0 over it."""
    parked = {"power_guid": "", "oc": {"core": 450, "mem": 2225, "power": 100}}
    try:
        # the tick comes while the un-park is still verifying what it wrote
        card = rig()
        GO._activate_oc_watchdog(0, 0, 70)                                   # as the park left it
        IM._state.update(engaged=True, restore=dict(parked))
        GO.verify_oc_applied = lambda *a, **k: tick(card) or {"verified": True, "warnings": []}
        IM._restore_if_engaged("game detected")
        assert card.writes == [(450, 2225)] and card.current == (450, 2225), card.writes
        assert GO.get_applied_oc() == {"core": 450, "mem": 2225, "power": 100, "active": True}
        assert IM._state["engaged"] is False

        # a tick already under way when the un-park comes
        card = rig()
        GO._activate_oc_watchdog(0, 0, 70)
        IM._state.update(engaged=True, restore=dict(parked))
        tick(card, during=lambda: IM._restore_if_engaged("game detected"))
        assert card.writes == [(450, 2225)] and card.current == (450, 2225), card.writes

        # the park, the other way round: neither tick puts the overclock back
        GO.verify_oc_applied = lambda *a, **k: tick(card) or {"verified": True, "warnings": []}
        GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=70)
        assert card.writes == [(450, 2225), (0, 0)] and card.current == (0, 0)
    finally:
        restore()


# ── PresentMon ────────────────────────────────────────────────────────
def fake_sc(installed=(), binpath=""):
    """subprocess.run for `sc query` / `sc qc`: the named services exist."""
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[0] == "sc" and argv[1] == "query" and argv[2] in installed:
            return types.SimpleNamespace(returncode=0, stderr="", stdout=(
                f"\nSERVICE_NAME: {argv[2]} \n        TYPE               : 10  WIN32_OWN_PROCESS  \n"
                f"        STATE              : 4  RUNNING \n"))
        if argv[0] == "sc" and argv[1] == "qc" and argv[2] in installed:
            return types.SimpleNamespace(returncode=0, stderr="", stdout=(
                f"[SC] QueryServiceConfig SUCCESS\n\nSERVICE_NAME: {argv[2]}\n"
                f"        START_TYPE         : 2   AUTO_START\n"
                f'        BINARY_PATH_NAME   : "{binpath}"\n'
                f"        DISPLAY_NAME       : PresentMon Shared Service\n"))
        return types.SimpleNamespace(returncode=1060, stderr="", stdout=(
            "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\n\n"
            "The specified service does not exist as an installed service.\n"))
    run.calls = calls
    return run


def test_the_presentmon_2_4_service_is_found_under_its_own_name():
    """2.4 registers "PresentMonSharedService".  The app only knew the older
    names, so its boot check never found the service it had installed and
    ran the 122 MB installer again on every start."""
    m.fresh()
    real_d, real_run = D.subprocess, subprocess.run
    downloads = []
    real_download = D._download
    D._download = lambda url, dest, progress_cb=None: downloads.append(url) or False
    try:
        for name in ("PresentMonSharedService", "Intel PresentMon Service", "PresentMonService"):
            run = fake_sc(installed=(name,))
            D.subprocess = types.SimpleNamespace(run=run)
            assert D._detect_pm2() == {"installed": True, "running": True, "name": name}
            entry = next(d for d in D.DEPENDENCIES if d["key"] == "presentmon2")
            assert entry["detect"]() is True
            r = D.install_presentmon2()
            assert r["ok"] and r["already_installed"] and downloads == [], "installed again over itself"
            assert not any(c[0] == "msiexec" for c in run.calls)

            subprocess.run = run
            assert FT._detect_pm2_service() == {"installed": True, "running": True, "name": name}
        assert D.PM2_SVC_NAMES[0] == FT.PM2_SERVICE_NAMES[0] == "PresentMonSharedService", \
            "the current name is asked for first"

        run = fake_sc()
        D.subprocess = types.SimpleNamespace(run=run)
        subprocess.run = run
        assert D._detect_pm2()["installed"] is False and FT._detect_pm2_service()["installed"] is False
        assert D.install_presentmon2()["ok"] is False and len(downloads) == 1, "a missing service is still installed"
    finally:
        D.subprocess, subprocess.run, D._download = real_d, real_run, real_download


def test_the_presentmon_cli_is_found_beside_the_shared_service():
    """2.4 keeps the service in Intel\\PresentMonSharedService and the tools
    in Intel\\PresentMon.  Walking up from the service used to land in the
    older layout's install folder; now it lands in Intel itself."""
    m.fresh()
    intel = os.path.join(m.ROOT, "Program Files", "Intel")
    service = os.path.join(intel, "PresentMonSharedService", "PresentMonService.exe")
    cli = os.path.join(intel, "PresentMon", "PresentMonConsoleApplication", "PresentMon-2.4.1-x64.exe")
    decoy = os.path.join(intel, "A Tuning Utility", "bin", "PresentMon-0.0.1-x64.exe")
    old_service = os.path.join(m.ROOT, "Old", "PresentMon", "Service", "PresentMonService.exe")
    old_cli = os.path.join(m.ROOT, "Old", "PresentMon", "PresentMonConsoleApplication", "PresentMon-2.3.0-x64.exe")
    for path in (service, cli, decoy, old_service, old_cli):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    real_run, real_hints = subprocess.run, FT.PM2_INSTALL_HINTS
    FT.PM2_INSTALL_HINTS = []
    try:
        subprocess.run = fake_sc(installed=("PresentMonSharedService",), binpath=service)
        assert FT._find_pm2_cli() == cli
        subprocess.run = fake_sc(installed=("Intel PresentMon Service",), binpath=old_service)
        assert FT._find_pm2_cli() == old_cli, "the older layout still works"
        subprocess.run = fake_sc()
        assert FT._find_pm2_cli() is None
    finally:
        subprocess.run, FT.PM2_INSTALL_HINTS = real_run, real_hints


def test_the_presentmon_entries_say_what_they_are_for_now():
    """They said "Used by Adaptive Tuning to read FPS".  Nothing in Adaptive
    Tuning reads frames any more; the crosshair's fullscreen check does."""
    for key in ("presentmon1", "presentmon2"):
        text = next(d for d in D.DEPENDENCIES if d["key"] == key)["description"]
        assert "Lets the crosshair tell when a game is in exclusive fullscreen" in text, text
        assert "Adaptive Tuning" not in text and not re.search(r"\bAT\b", text), text
        assert not BANNED.search(text) and EM_DASH not in text, text
    listed = {d["key"]: d["description"] for d in D.check_all()["deps"]}
    assert "crosshair" in listed["presentmon1"] and "crosshair" in listed["presentmon2"]


def test_restore_the_real_functions():
    restore()
    assert GO.apply_oc.__module__ == "core.gpu_overclock" and GO.time is time
    assert IM.gpu_overclock is GO and sys.modules["core.adaptive_tuning"] is AT
