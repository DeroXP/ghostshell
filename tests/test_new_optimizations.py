"""The three optimizations added in 3.6.3: NVIDIA driver settings through the
driver's own settings store (core/nvidia_profile.py, on the GPU tab), the
flip model for windowed games as a lasting Gaming tweak (core/optimizer.py),
and a RAM-speed check that can tell XMP / EXPO being off (core/health_audit.py)."""
import sandbox as m

import unittest

from core import reg_originals as RO

NP = m.app("core.nvidia_profile")
NC = m.app("core.nvidia_clean")
OPT = m.app("core.optimizer")
HA = m.app("core.health_audit")

DX = r"HKCU\Software\Microsoft\DirectX\UserGpuPreferences"
DX_VAL = "DirectXUserGlobalSettings"


def rows_text(rows):
    return " | ".join(r["name"] for r in rows)


# ═════════════════════════════════════════════════════════════════════
# NVIDIA driver settings
# ═════════════════════════════════════════════════════════════════════
def test_driver_settings_are_set_on_the_global_profile_and_read_back():
    m.fresh()
    rows = NP.apply()
    assert all(r["ok"] for r in rows), rows_text(rows)
    assert m.DRS == {NP.POWER_MODE: 1, NP.PRERENDER: 1, NP.SHADER_CACHE: 10240}
    assert "Power management mode -> Prefer maximum performance" in rows_text(rows)
    assert "Low Latency Mode -> On" in rows_text(rows)
    assert m.DRS_CALLS.count(("save",)) == 1, "one save for all of them"
    assert RO.state_records("nvidia-drs", "gpu") == {"0x1057eb71": 5, "0x007ba09e": 0, "0x00ac8497": 4096}


def test_a_larger_shader_cache_is_never_shrunk():
    m.fresh()
    m.DRS[NP.SHADER_CACHE] = 16384                  # what the developer's PC has
    rows = NP.apply()
    assert m.DRS[NP.SHADER_CACHE] == 16384 and "Shader cache size -> at least 10 GB (already)" in rows_text(rows)
    assert "0x00ac8497" not in RO.state_records("nvidia-drs", "gpu")


def test_settings_already_in_place_are_not_recorded_or_written():
    m.fresh()
    m.DRS.update({NP.POWER_MODE: 1, NP.PRERENDER: 1, NP.SHADER_CACHE: 10240})
    rows = NP.apply()
    assert all(r["ok"] and r["name"].endswith("(already)") for r in rows)
    assert m.DRS_CALLS == [] and RO.state_records("nvidia-drs", "gpu") == {}


def test_a_setting_the_driver_does_not_keep_is_reported_as_not_done():
    m.fresh()
    m.DRS_IGNORED.add(NP.PRERENDER)
    rows = NP.apply()
    bad = [r for r in rows if not r["ok"]]
    assert len(bad) == 1 and bad[0]["name"] == "Low Latency Mode -> On: the driver did not keep the change"
    m.fresh()
    m.DRS_SAVE_OK[0] = False
    assert not any(r["ok"] for r in NP.apply()), "nothing is reported as done when the save fails"


def test_undo_puts_back_exactly_what_was_there():
    m.fresh()
    del m.DRS[NP.PRERENDER]                         # not set in the global profile at all
    before = dict(m.DRS)
    NP.apply()
    assert NP.restore() == {"restored": 3, "failed": 0}
    assert m.DRS == before, "values back, and the one that was not set removed again"
    assert RO.state_records("nvidia-drs", "gpu") == {}
    assert NP.restore() == {"restored": 0, "failed": 0}


def test_a_failed_undo_keeps_the_records_for_the_next_try():
    m.fresh()
    NP.apply()
    m.DRS_SAVE_OK[0] = False
    assert NP.restore() == {"restored": 0, "failed": 3}
    assert len(RO.state_records("nvidia-drs", "gpu")) == 3
    m.DRS_SAVE_OK[0] = True
    assert NP.restore()["restored"] == 3 and m.DRS[NP.POWER_MODE] == 5


def test_a_record_for_some_other_setting_is_dropped_not_applied():
    m.fresh()
    RO.remember_state("nvidia-drs", "0x12345678", 7, "gpu")      # planted in the user-writable records file
    assert NP.restore() == {"restored": 0, "failed": 0}
    assert 0x12345678 not in m.DRS and RO.state_records("nvidia-drs", "gpu") == {}


def test_without_an_nvidia_driver_nothing_is_claimed():
    m.fresh()
    m.DRS_OK[0] = False
    rows = NP.apply()
    assert rows == [{"name": "NVIDIA driver settings: the driver's settings store could not be opened", "ok": False}]
    assert RO.state_records("nvidia-drs", "gpu") == {}


def test_gpu_tab_applies_and_undoes_the_driver_settings():
    m.fresh()
    NC.detect_gpu = lambda: {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 5070 Ti", "driver": "1", "vram_mb": 16000}
    m.PNP["Display"] = [{"InstanceId": r"PCI\VEN_10DE&DEV_2C05\GPU0", "FriendlyName": "NVIDIA GeForce RTX 5070 Ti"}]
    before = dict(m.DRS)
    rows = NC.apply_nvidia_tweaks()
    assert "Low Latency Mode -> On" in rows_text(rows) and m.DRS[NP.POWER_MODE] == 1
    assert "Shader Cache: 10GB" not in rows_text(rows), "the registry write that never worked is gone"
    assert not [w for w in m.WRITES if w[1] == "ShaderDiskCacheSizeBytes"]
    res = NC.undo_gpu_tweaks()
    assert res["ok"] and "3 NVIDIA driver setting(s) restored" in rows_text(res["results"])
    assert m.DRS == before


def test_reset_all_does_not_touch_the_gpu_tabs_driver_settings():
    m.fresh()
    NP.apply()
    OPT.reset_all_optimizations()
    assert m.DRS[NP.POWER_MODE] == 1, "the GPU tab has its own undo"


def test_real_driver_settings_can_be_read():
    """Read-only, against this PC's NVIDIA driver: opens its settings store,
    reads the three values, closes it.  Nothing is set or saved."""
    saved_fns = NP._fns
    NP._fns = None
    try:
        s = m.real_nvidia_open()
        if s is None:
            raise unittest.SkipTest("no NVIDIA driver on this PC")
        with s:
            values = {sid: s.get(sid) for sid, *_ in NP.SETTINGS}
        assert all(v is None or isinstance(v, int) for v in values.values()), values
    finally:
        NP._fns = saved_fns


# ═════════════════════════════════════════════════════════════════════
# Optimizations for windowed games
# ═════════════════════════════════════════════════════════════════════
def test_flip_model_is_switched_on_keeping_the_other_directx_settings():
    m.fresh()
    m.rset(DX, DX_VAL, "AutoHDREnable=1;VRROptimizeEnable=0;")
    rows = OPT.run_full_optimize(["gaming"])["gaming"]
    assert {"name": "Optimizations for windowed games (flip model)", "ok": True} in rows
    assert m.rdata(DX, DX_VAL) == "AutoHDREnable=1;VRROptimizeEnable=0;SwapEffectUpgradeEnable=1;"
    OPT.reset_all_optimizations()
    assert m.rdata(DX, DX_VAL) == "AutoHDREnable=1;VRROptimizeEnable=0;", "exactly the user's string again"


def test_flip_model_on_a_clean_windows_and_when_already_on():
    m.fresh()
    OPT.run_full_optimize(["gaming"])
    assert m.rdata(DX, DX_VAL) == "SwapEffectUpgradeEnable=1;"
    OPT.reset_all_optimizations()
    assert m.rget(DX, DX_VAL) is None
    m.fresh()
    m.rset(DX, DX_VAL, "SwapEffectUpgradeEnable=1;AutoHDREnable=1;")
    m.clear_logs()
    OPT.run_full_optimize(["gaming"])
    assert not [w for w in m.WRITES if w[1] == DX_VAL] and not RO.has_original(DX, DX_VAL)


def test_flip_model_set_by_the_optimizer_survives_a_competitive_latency_session():
    CL = m.app("core.competitive_latency")
    m.fresh()
    OPT.run_full_optimize(["gaming"])
    CL.apply()
    CL.reset()
    assert m.rdata(DX, DX_VAL) == "SwapEffectUpgradeEnable=1;"


# ═════════════════════════════════════════════════════════════════════
# RAM speed
# ═════════════════════════════════════════════════════════════════════
def ram(*sticks):
    HA._ps_json = lambda cmd, timeout=6: [dict(zip(("PartNumber", "Speed", "ConfiguredClockSpeed", "SMBIOSMemoryType"), s))
                                          for s in sticks]
    return HA._check_ram_speed()[0]


def test_ram_kit_running_below_its_rating_is_flagged():
    r = ram(("CMK32GX5M2B6000C36", 4800, 4800, 34), ("CMK32GX5M2B6000C36", 4800, 4800, 34))
    assert r["severity"] == "warn" and "rated 6000 MT/s but runs at 4800" in r["detail"]


def test_ram_kit_at_its_rating_is_ok():
    r = ram(("MTC8C1084S1UC56BD1 BF", 5600, 5600, 34), ("MTC8C1084S1UC56BD1 BF", 5600, 5600, 34))   # this PC
    assert r["severity"] == "ok" and r["detail"] == "Running at the kit's rated 5600 MT/s."
    assert ram(("F5-6000J3636F16G", 4800, 6000, 34))["severity"] == "ok"


def test_ddr4_with_xmp_on_is_not_called_slow():
    """DDR4 at 3600 used to be reported as "running at JEDEC base speed"."""
    r = ram(("F4-3600C16D-32GTZNC", 2133, 3600, 26))
    assert r["severity"] == "ok"
    r = ram(("UNKNOWNPART", 2133, 3600, 26))
    assert r["severity"] == "ok" and "so XMP / EXPO is on" in r["detail"]


def test_unknown_kit_at_a_standard_speed_is_a_hedged_warning():
    r = ram(("M425R1GB4BB0-CWM0D", 4800, 4800, 34))
    assert r["severity"] == "warn" and "cannot be read here" in r["detail"]
    assert ram(("HMA82GU6CJR8N-XN", 3200, 2666, 26))["severity"] == "warn"


def test_ram_running_below_its_base_speed_is_flagged():
    r = ram(("UNKNOWN", 5600, 4000, 34))
    assert r["severity"] == "warn" and "below even its standard speed" in r["detail"]


def test_part_number_spellings():
    cases = [("CMK32GX5M2B6000C36", 34, 6000), ("F4-3600C16D-32GTZNC", 26, 3600), ("FF3D532G6000HC38ADC01", 34, 6000),
             ("KF560C36BBE-16", 34, 6000), ("KF432C16BB/16", 26, 3200), ("CT16G56C46U5.M8A1", 34, 5600),
             ("BL16G36C16U4B.M16FE1", 26, 3600), ("MTC8C1084S1UC56BD1 BF", 34, 5600), ("M425R1GB4BB0-CWM0D", 34, None),
             ("", 34, None), (None, 26, None)]
    assert [(p, HA._rated_speed(p, t)) for p, t, want in cases if HA._rated_speed(p, t) != want] == []
