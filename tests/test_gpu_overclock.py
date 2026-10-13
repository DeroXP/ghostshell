"""GPU overclock safety (core/gpu_overclock.py, core/nvapi_oc.py): what
apply_oc() writes, what it reports, and when the watchdog and the exit hook
may act.  No clock is read or written here - the sandbox keeps the NVIDIA
interface unloaded, and apply_oc() is driven against a stand-in for it."""
import sandbox as m

from core import nvapi_oc as NV

GO = m.app("core.gpu_overclock")
LIMITS = {"core_max_mhz": 3000, "mem_max_mhz": 15000, "power_default_w": 300, "power_max_w": 350,
          "power_min_w": 150}


class FakeNvapi:
    def __init__(self, available=True, write_ok=True):
        self.available, self.write_ok, self.writes = available, write_ok, []

    def is_available(self):
        return {"ok": self.available, "err": "" if self.available else "nvapi64.dll not found"}

    def set_offsets(self, core_offset_mhz=0, mem_offset_mhz=0):
        self.writes.append((core_offset_mhz, mem_offset_mhz))
        return {"ok": self.write_ok, "struct_version_used": 3, "err": "" if self.write_ok else "driver said no"}


watchdog: list = []
_real = {n: getattr(GO, n) for n in ("detect_gpu_oc_capability", "detect_oc_tools_running", "verify_oc_applied",
                                     "_activate_oc_watchdog", "_deactivate_oc_watchdog", "get_applied_oc",
                                     "_cache_power_default", "_nvapi")}


def rig(vendor="nvidia", nvapi=None, tools=(), power_in_force=110):
    m.fresh()
    del watchdog[:]
    m.CMD_HOOKS.append(lambda a: m.R() if a[0] == "nvidia-smi" else None)
    GO.detect_gpu_oc_capability = lambda: {"ok": True, "vendor": vendor, "limits": dict(LIMITS)}
    GO.detect_oc_tools_running = lambda: {"conflict_likely": bool(tools), "tools": [{"name": t} for t in tools]}
    GO.verify_oc_applied = lambda *a, **k: {"verified": True, "warnings": []}
    GO._activate_oc_watchdog = lambda core, mem, power: watchdog.append(("arm", core, mem, power))
    GO._deactivate_oc_watchdog = lambda: watchdog.append(("disarm",))
    GO.get_applied_oc = lambda: {"core": 0, "mem": 0, "power": power_in_force}
    GO._cache_power_default = lambda w: None
    GO._nvapi = nvapi if nvapi is not None else FakeNvapi()
    return GO._nvapi


def power_commands():
    return [c[2] for c in m.CMDS if c[:2] == ["nvidia-smi", "-pl"]]


# ── the sandbox itself ────────────────────────────────────────────────
def test_the_real_nvidia_interface_is_never_loaded_in_tests():
    assert NV.is_available()["ok"] is False and NV._this_process_wrote_offsets is False
    assert NV.set_offsets(core_offset_mhz=50, mem_offset_mhz=100)["ok"] is False
    assert NV._nvapi.dll is None and NV._nvapi.initialized is False
    assert NV._this_process_wrote_offsets is True, "noted before the attempt, so a half-made write is still reset"
    NV._this_process_wrote_offsets = False


# ── apply_oc ──────────────────────────────────────────────────────────
def test_offsets_and_power_limit_are_written():
    nv = rig()
    r = GO.apply_oc(core_offset_mhz=150, mem_offset_mhz=800, power_pct=110)
    assert r["ok"] and r["applied"] == {"core_offset_mhz": 150, "mem_offset_mhz": 800, "power_pct": 110}
    assert nv.writes == [(150, 800)] and power_commands() == ["330"]
    assert watchdog == [("arm", 150, 800, 110)]


def test_power_none_leaves_the_limit_exactly_as_it_is():
    """Callers that only mean to move the clocks pass None.  They used to pass
    nothing, which meant 100 % - silently undoing a limit the user had raised."""
    nv = rig(power_in_force=110)
    r = GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0, power_pct=None)
    assert r["ok"] and power_commands() == [], "no power-limit command at all"
    assert any(s["name"] == "Power limit left unchanged" for s in r["steps"])
    assert r["applied"]["power_pct"] == 110 and watchdog == [("arm", 100, 0, 110)], \
        "the limit in force is carried forward for the watchdog, not replaced by 100"
    assert nv.writes == [(100, 0)]


def test_power_100_means_set_it_to_stock():
    rig()
    GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0, power_pct=100)
    assert power_commands() == ["300"]


def test_requests_are_clamped_to_the_safe_range():
    nv = rig()
    r = GO.apply_oc(core_offset_mhz=5000, mem_offset_mhz=99999, power_pct=500)
    assert nv.writes == [(GO.MAX_CORE_OFFSET_MHZ, GO.MAX_MEM_OFFSET_MHZ)]
    assert r["applied"]["power_pct"] == GO.MAX_POWER_PCT
    assert power_commands() == ["350"], "never above what the card itself allows"
    rig()
    GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=1)
    assert power_commands() == ["150"], "never below the card's minimum (50 % would be 150 W here)"


def test_asking_for_stock_disarms_the_watchdog():
    rig()
    GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100)
    assert watchdog == [("disarm",)], "or it writes the previous overclock straight back within seconds"


def test_ok_is_false_when_the_offsets_were_not_written():
    """Callers act on `ok`: Adaptive Tuning records the offset as applied and
    climbs on from there.  It used to be True no matter what."""
    rig(nvapi=FakeNvapi(available=False))
    r = GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0)
    assert r["ok"] is False and "not applied" in r["err"]
    rig(nvapi=FakeNvapi(write_ok=False))
    r = GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0)
    assert r["ok"] is False and any("NVAPI offset apply failed" in s["name"] for s in r["steps"])
    GO._nvapi = None
    assert GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0)["ok"] is False


def test_stock_request_without_the_interface_is_still_a_success():
    rig(nvapi=FakeNvapi(available=False))
    r = GO.apply_oc(core_offset_mhz=0, mem_offset_mhz=0, power_pct=100)
    assert r["ok"] is True, "nothing but the power limit was asked for"


def test_other_vendors_are_refused_before_anything_is_written():
    nv = rig(vendor="amd")
    r = GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=100)
    assert r["ok"] is False and "not supported on amd" in r["err"]
    assert nv.writes == [] and not m.ran("nvidia-smi") and watchdog == []


def test_a_running_overclocking_tool_is_reported():
    rig(tools=("MSI Afterburner",))
    r = GO.apply_oc(core_offset_mhz=100, mem_offset_mhz=0)
    assert r["steps"][0]["ok"] is False and "MSI Afterburner" in r["steps"][0]["name"]


# ── exit hook ─────────────────────────────────────────────────────────
def test_exit_hook_only_resets_a_card_this_process_overclocked():
    """It used to reset unconditionally - so a second copy of the app, which
    only hands off to the first and exits, took the first one's overclock
    with it."""
    resets = []
    real_reset, real_flag = NV.force_reset_all, NV._this_process_wrote_offsets
    NV.force_reset_all = lambda: resets.append(1) or {"ok": True}
    try:
        NV._this_process_wrote_offsets = False
        NV._shutdown_cleanup()
        assert resets == []
        NV._this_process_wrote_offsets = True
        NV._shutdown_cleanup()
        assert resets == [1]
    finally:
        NV.force_reset_all, NV._this_process_wrote_offsets = real_reset, real_flag


def test_restore_the_real_functions():
    for name, fn in _real.items():
        setattr(GO, name, fn)
    assert GO.apply_oc.__module__ == "core.gpu_overclock"
