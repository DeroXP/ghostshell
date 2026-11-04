import sandbox as m
"""GPU overclock robustness (core/gpu_overclock.py): the card must end at a
known state when a reset, a cancel or a crash signal arrives while something
else is mid-way.  The NVIDIA interface is always a stand-in here."""
import json
import os
import re
import threading
import time

GO = m.app("core.gpu_overclock")
CR = m.app("core.crash_recovery")
NOTIFIER = m.app("core.notifier")
_real = {n: getattr(GO, n) for n in (
    "detect_gpu_oc_capability", "detect_oc_tools_running", "verify_oc_applied", "_activate_oc_watchdog",
    "_deactivate_oc_watchdog", "get_applied_oc", "_cache_power_default", "_nvapi", "apply_oc",
    "_reset_to_stock_hard", "load_profile", "_OC_WATCHDOG_INTERVAL_SEC")}
CAP = {"ok": True, "vendor": "nvidia",
       "limits": {"core_max_mhz": 2900, "mem_max_mhz": 14000, "power_default_w": 300, "power_max_w": 350,
                  "power_min_w": 150}}
LIVE_CSV = "2800, 14000, 70, 250.5, 99, 60, 55, 300.00, 2800, 14000, 2900, 14001"


def restore():
    for name, value in _real.items():
        setattr(GO, name, value)
    GO._deactivate_oc_watchdog()
    with GO._oc_watchdog_pause_lock:
        GO._oc_watchdog_pause_sources.clear()
    GO._stability_session["active"] = False
    GO._auto_oc_session["active"] = False
    GO.clear_crash_state()


class FakeNvapi:
    """Records writes and resets; reports the offsets in `current`."""

    def __init__(self, current=(0, 0), on_read=None, write_ok=True):
        self.current, self.on_read, self.write_ok = current, on_read, write_ok
        self.writes, self.resets = [], 0

    def is_available(self):
        return {"ok": True}

    def get_current_offsets(self):
        if self.on_read:
            self.on_read()
        return {"ok": True, "core_offset_mhz": self.current[0], "mem_offset_mhz": self.current[1]}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        return {"ok": self.write_ok, "struct_version_used": 3}

    def force_reset_all(self):
        self.resets += 1
        return {"ok": True, "steps": [{"name": "Graphics P-state freqDelta → 0", "ok": True, "err": ""}]}


def arm(core, mem, power=100):
    with GO._active_oc_lock:
        GO._active_oc.update({"core": core, "mem": mem, "power": power, "active": True})


# ── the drift watchdog ────────────────────────────────────────────────
def test_the_idle_watchdog_does_not_hold_the_lock_while_it_waits():
    """It used to wait out its interval holding _active_oc_lock, so with no
    overclock active, get_applied_oc() and every (de)activate call - the
    one at the top of emergency_reset_oc included - queued behind it, for
    minutes on Python 3.12."""
    m.fresh()
    GO._deactivate_oc_watchdog()
    GO._OC_WATCHDOG_INTERVAL_SEC = 1.0
    stop = threading.Event()
    t = threading.Thread(target=GO._oc_watchdog_loop, args=(stop,), daemon=True)
    try:
        t.start()
        time.sleep(0.1)
        waits = []
        for _ in range(4):
            t0 = time.time()
            GO.get_applied_oc()
            waits.append(time.time() - t0)
            time.sleep(0.05)
        assert max(waits) < 0.3, f"waited {max(waits):.2f}s for the lock"
    finally:
        stop.set()
        t.join(3)
        restore()


def test_a_watchdog_reassert_never_lands_after_a_reset():
    """The tick reads its target, then NVAPI, then writes.  A reset that
    disarmed it in between used to be undone by that write: the card was
    left overclocked with nothing watching it."""
    m.fresh()
    stop = threading.Event()
    try:
        # control: drift with nothing in between is re-asserted
        GO._nvapi = FakeNvapi(current=(0, 0), on_read=stop.set)
        arm(150, 800)
        GO._oc_watchdog_loop(stop)
        assert GO._nvapi.writes == [(150, 800)]

        def reset_lands_mid_tick():
            GO._deactivate_oc_watchdog()        # what reset_oc / emergency_reset_oc do first
            stop.set()
        stop.clear()
        GO._nvapi = FakeNvapi(current=(0, 0), on_read=reset_lands_mid_tick)
        arm(150, 800)
        GO._oc_watchdog_loop(stop)
        assert GO._nvapi.writes == [], "the old overclock was written back after the reset"
    finally:
        restore()


def rig_apply(nv):
    m.fresh()
    events = []
    m.CMD_HOOKS.append(lambda a: m.R() if a[0] == "nvidia-smi" else None)
    GO.detect_gpu_oc_capability = lambda: json.loads(json.dumps(CAP))
    GO.detect_oc_tools_running = lambda: {"conflict_likely": False, "tools": []}
    GO.verify_oc_applied = lambda *a, **k: {"verified": True, "warnings": []}
    GO._activate_oc_watchdog = lambda core, mem, power: events.append(("arm", core, mem))
    GO._deactivate_oc_watchdog = lambda: events.append(("disarm",))
    GO.get_applied_oc = lambda: {"core": 150, "mem": 800, "power": 100, "active": True}
    GO._cache_power_default = lambda w: None
    real_write = nv.set_offsets
    nv.set_offsets = lambda **k: events.append(("write", k["core_offset_mhz"], k["mem_offset_mhz"])) or real_write(**k)
    GO._nvapi = nv
    return events


def test_going_to_stock_disarms_the_watchdog_before_writing():
    """Disarmed after the write (and the slow verification), a watchdog tick
    in between saw the drift and wrote the old overclock back - and the
    disarm then left it on the card."""
    events = rig_apply(FakeNvapi())
    try:
        assert GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100)["ok"]
        assert events == [("disarm",), ("write", 0, 0)]
        del events[:]
        GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0, power_pct=None)
        assert events == [("write", 100, 0), ("arm", 100, 0)], "an overclock still arms after it is written"
    finally:
        restore()


# ── the stability probe ───────────────────────────────────────────────
TDRS: list = []      # timestamps of the TDR events the fake event log holds


def _event_log(command):
    if "Get-WinEvent" not in command:
        return None
    back = int(re.search(r"AddSeconds\(-(\d+)\)", command).group(1))
    since = time.time() - back
    hits = [{"Id": 153, "TimeCreated": "", "LevelDisplayName": "Error", "Msg": "TDR"} for ts in TDRS if ts >= since]
    return m.R(json.dumps(hits) if hits else "")


def rig_probe():
    m.fresh()
    del TDRS[:]
    m.PS_HOOKS.append(_event_log)
    m.CMD_HOOKS.append(lambda a: m.R(LIVE_CSV) if a[0] == "nvidia-smi" and a[1].startswith("--query-gpu=clocks.current")
                       else None)
    nv = FakeNvapi()
    GO._nvapi = nv
    return nv


def test_a_reset_during_a_probe_fails_the_probe():
    """A temperature-ceiling trip (or any emergency reset) mid-probe put the
    card back to stock, and the probe went on to report the offset under
    test as stable.  The tuner then climbed past it."""
    nv = rig_probe()
    try:
        GO.start_stability_probe(expected_core_max=2900)
        for _ in range(24):
            assert GO.run_probe_tick(frame_time_ms=10.0)["kind"] == "running"
        GO.emergency_reset_oc(reason="temp_ceiling:86c")
        assert nv.resets == 1
        tick = GO.run_probe_tick(frame_time_ms=10.0)
        assert tick.get("abort") and tick.get("kind") == "reset", "the load stops at the next tick"
        verdict = GO.end_stability_probe()
        assert verdict["stable"] is False and "temp_ceiling:86c" in verdict["abort_reason"]
    finally:
        restore()


def test_a_crash_signal_from_the_page_still_counts_after_a_reset():
    """The page fires emergency-reset on WebGL context loss before its next
    tick reports it; the tick must still record the crash."""
    rig_probe()
    try:
        GO.start_stability_probe(expected_core_max=2900)
        GO.emergency_reset_oc(reason="webgl_context_lost")
        tick = GO.run_probe_tick(frame_time_ms=10.0, context_lost=True)
        assert tick["kind"] == "context_loss" and GO.get_crash_state()["kind"] == "context_loss"
        assert GO.end_stability_probe()["kind"] == "context_loss"
    finally:
        restore()


def test_a_tdr_seen_by_the_probe_resets_the_card():
    """Context loss, artifacts and hangs reset the card at once; a TDR found
    in the event log only failed the probe and left the offset applied."""
    nv = rig_probe()
    try:
        GO.start_stability_probe(expected_core_max=2900)
        TDRS.append(time.time())
        ticks = [GO.run_probe_tick(frame_time_ms=10.0) for _ in range(5)]
        assert ticks[-1]["kind"] == "tdr"
        assert nv.resets == 1 and GO.get_crash_state()["kind"] == "tdr"
        GO.end_stability_probe()

        nv.resets = 0
        GO.clear_crash_state()
        del TDRS[:]
        GO.start_stability_probe(expected_core_max=2900)
        GO.run_probe_tick(frame_time_ms=10.0)
        TDRS.append(time.time())            # in the last seconds, after the tick's last look
        verdict = GO.end_stability_probe()
        assert verdict["kind"] == "tdr" and nv.resets == 1, "a late TDR resets the card too"
    finally:
        restore()


def test_a_tdr_just_before_the_probe_does_not_hide_one_during_it():
    """The start-of-probe snapshot looked back 10 s, the later checks only
    5 s past the start: a TDR 5-10 s before the start (the previous step's
    crash) was subtracted from a count that no longer held it."""
    rig_probe()
    try:
        TDRS.append(time.time() - 7)
        GO.start_stability_probe(expected_core_max=2900)
        TDRS.append(time.time())
        ticks = [GO.run_probe_tick(frame_time_ms=10.0) for _ in range(5)]
        assert ticks[-1]["kind"] == "tdr", f"missed the new TDR: {ticks[-1]}"
    finally:
        restore()


# ── Quick Tune (auto-OC) ──────────────────────────────────────────────
def rig_tune(apply_fn):
    m.fresh()
    GO._nvapi = FakeNvapi()
    GO.detect_gpu_oc_capability = lambda: json.loads(json.dumps(CAP))
    GO.load_profile = lambda: {"exists": False}
    GO.apply_oc = apply_fn


def test_a_cancel_during_a_step_resets_after_the_step_has_applied():
    """Cancel and the next step run on separate request threads.  A cancel
    that arrived while the step was applying its offset reset first; the
    probe offset then landed on a card nobody was tuning any more."""
    order = []
    entered, gate = threading.Event(), threading.Event()

    def slow_apply(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100):
        entered.set()
        gate.wait(5)
        order.append(("apply", core_offset_mhz))
        return {"ok": True}
    rig_tune(slow_apply)
    GO._reset_to_stock_hard = lambda attempts=3: order.append(("reset",)) or (True, {})
    try:
        assert GO.auto_oc_start(max_power=False)["ok"]
        step = threading.Thread(target=GO.auto_oc_next, daemon=True)
        step.start()
        assert entered.wait(5)
        cancel = threading.Thread(target=GO.auto_oc_cancel, daemon=True)
        cancel.start()
        time.sleep(0.3)
        gate.set()
        step.join(5)
        cancel.join(5)
        assert order == [("apply", 75), ("reset",)], order
        assert GO.auto_oc_next()["ok"] is False and order[-1] == ("reset",), "nothing applied after a cancel"
    finally:
        gate.set()
        restore()


def test_a_step_whose_offset_was_not_written_is_not_stable():
    """The probe measures what is on the card.  When the step's write was
    refused (an offset past the driver's range, say), its "stable" verdict
    was credited to the offset anyway and the search climbed on."""
    written = {}

    def apply(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100):
        ok = core_offset_mhz <= 75
        if ok:
            written["core"] = core_offset_mhz
        return {"ok": ok}
    rig_tune(apply)
    try:
        GO.auto_oc_start(max_power=False)
        assert GO.auto_oc_next()["current_core"] == 75
        assert GO.auto_oc_next({"stable": True, "reason": "", "kind": "stable"})["current_core"] == 150
        r = GO.auto_oc_next({"stable": True, "reason": "", "kind": "stable"})
        s = GO.auto_oc_status()
        assert s["stable_core"] == 75 and s["unstable_core"] == 150, s
        assert s["log"][-1]["stable"] is False and "could not be applied" in s["log"][-1]["reason"]
        assert r["current_core"] < 150 and written["core"] == 75
    finally:
        restore()


def test_the_power_limit_is_only_recorded_if_it_took():
    """The power-limit pre-step's result was thrown away: a raise that failed
    was logged as done and became the power level the run reapplied and
    saved with its result."""
    def apply_power(took):
        return lambda core_offset_mhz=0, mem_offset_mhz=0, power_pct=100: {
            "ok": True, "verification": {"power": {"ok": took}}}
    try:
        rig_tune(apply_power(False))
        assert GO.auto_oc_start(max_power=True)["pre_max_power_pct"] == 100
        rig_tune(apply_power(True))
        assert GO.auto_oc_start(max_power=True)["pre_max_power_pct"] == 116
    finally:
        restore()


# ── Benchmark Tune ────────────────────────────────────────────────────
def run_benchmark_tune(final_verdict):
    """Walk a two-rung ladder on each axis to the final validation, report
    `final_verdict` for it, and return the last answer and the resets made."""
    resets, fps = [], [100.0]
    rig_tune(lambda core_offset_mhz=0, mem_offset_mhz=0, power_pct=100: {"ok": True})
    GO._reset_to_stock_hard = lambda attempts=3: resets.append(attempts) or (True, {})

    def verdict(**more):
        fps[0] += 5                         # each higher rung measures faster, so it wins
        v = {"stable": True, "aborted": False, "abort_reason": "", "kind": "stable", "avg_fps": fps[0],
             "avg_frame_time_ms": 1000.0 / fps[0], "p99_frame_time_ms": 12.0, "frame_variance_pct": 5.0,
             "max_temp_c": 65, "avg_gpu_util_pct": 99, "frame_data_valid": True}
        v.update(more)
        return v
    assert GO.benchmark_oc_start(core_max_offset=25, mem_max_offset=100, max_power=False)["ok"]
    r = GO.benchmark_oc_next()
    while r.get("phase") != "final_validate":
        assert r["ok"], r
        r = GO.benchmark_oc_next(verdict())
    assert (r["apply_core"], r["apply_mem"]) == (25, 100)
    return GO.benchmark_oc_next(verdict(**final_verdict)), resets


def test_benchmark_tune_does_not_save_a_pair_that_failed_validation():
    """The final validation's verdict was recorded and then ignored: a pair
    that failed it was saved as the profile, reported as saved, and left on
    the card."""
    try:
        r, resets = run_benchmark_tune({"stable": False, "kind": "variance",
                                        "abort_reason": "Excessive frame time variance (62% of the mean)"})
        assert r["ok"] is False and r["done"] and r["validated"] is False, r
        assert "Final validation failed at core+25 / mem+100" in r["err"] and "Nothing was saved" in r["err"]
        assert "—" not in r["err"]
        assert not os.path.exists(GO.OC_PROFILE_PATH), "the failed pair was saved"
        assert resets == [3], "the card goes back to stock"
        assert GO.benchmark_oc_status()["active"] is False and GO.benchmark_oc_status()["best"] is None

        r, resets = run_benchmark_tune({})
        assert r["ok"] and r["done"] and r["best"]["core_offset_mhz"] == 25 and resets == []
        assert r["best"]["stock_score"] > 0 and r["best"]["gain_pct"] > 0, \
            "rung 0 seeds the stock score (it never did: every run said +0% vs stock)"
        with open(GO.OC_PROFILE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        assert (saved["core_offset_mhz"], saved["mem_offset_mhz"], saved["apply_on_startup"]) == (25, 100, False)
    finally:
        restore()


# ── reset and crash records ───────────────────────────────────────────
def test_reset_still_works_when_nvidia_smi_does_not_answer():
    """With nvidia-smi hung (after a crash, say) the GPU no longer looked
    like NVIDIA, so the user's Reset refused before touching anything and
    left the watchdog re-asserting the overclock."""
    m.fresh()
    try:
        GO.detect_gpu_oc_capability = lambda: {"ok": False, "vendor": "unknown", "limits": {
            "power_min_w": 0, "power_max_w": 0, "power_default_w": 0, "core_max_mhz": 0, "mem_max_mhz": 0}}
        GO._nvapi = FakeNvapi()
        arm(150, 800)
        r = GO.reset_oc()
        assert r["ok"] and GO._nvapi.resets == 1 and any("NVAPI" in s["name"] for s in r["steps"]), r
        assert GO.get_applied_oc()["active"] is False

        class NoNvapi(FakeNvapi):
            def is_available(self):
                return {"ok": False}
        GO._nvapi = NoNvapi()
        arm(150, 800)
        r = GO.reset_oc()
        assert r["ok"] is False and GO._nvapi.resets == 0
        assert GO.get_applied_oc()["active"] is False, "the watchdog stops even when nothing can be reset"

        GO.detect_gpu_oc_capability = lambda: json.loads(json.dumps(CAP))
        GO._nvapi = FakeNvapi()
        assert GO.reset_oc()["ok"] and GO._nvapi.resets == 1, "the normal path is unchanged"
    finally:
        restore()


def test_a_crash_is_recorded_at_the_offset_under_test():
    """The probe passed its expected boost clock as the crash offset, so the
    crash banner read "crashed at core+2900 / mem+0"."""
    rig_probe()
    try:
        arm(150, 800)
        GO.start_stability_probe(expected_core_max=2900)
        GO.emergency_reset_oc(reason="webgl_context_lost")   # the page's own reset can come first
        GO.run_probe_tick(frame_time_ms=10.0, context_lost=True)
        assert GO.get_crash_state()["crashed_at_offset"] == {"core": 150, "mem": 800}
        GO.end_stability_probe()
    finally:
        restore()


def test_the_crash_toast_says_what_happened():
    """It said the step-down was "saved as the new profile"; the saved
    profile is never touched there, and the step-down can fail."""
    toasts = []
    real_toast, real_detect = NOTIFIER.toast, CR.detect_crash
    NOTIFIER.toast = lambda title, msg, force=False: toasts.append(msg) or {"ok": True}
    CR.detect_crash = lambda exit_ts=None: {"crashed": True, "kind": "tdr", "tdr_count": 1, "crash_count": 0,
                                            "inapp_state": None, "events": []}
    m.fresh()
    try:
        for applied in (True, False):
            GO.apply_oc = lambda applied=applied, **k: {"ok": applied}
            del toasts[:]
            r = CR.handle_game_exit("game.exe", "Game", active_core=150, active_mem=500)
            assert r["blacklisted"] and r["step_down"] == {"core": 125, "mem": 400}, r
            msg = toasts[-1]
            assert "saved as the new profile" not in msg and "—" not in msg
            assert msg.endswith("Your saved OC profile was not changed."), msg
            assert ("Stepped down to core+125 / mem+400 for now." in msg) is applied, msg
            assert ("could not be stepped down" in msg) is (not applied), msg
    finally:
        NOTIFIER.toast, CR.detect_crash = real_toast, real_detect
        restore()
