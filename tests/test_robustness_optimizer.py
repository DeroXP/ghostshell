import sandbox as m

# Robustness of the optimizer area: what happens when a step fails, when
# something a record points at is gone, and when output does not look the way
# the happy path assumes.

import json
import os
import threading
import time
import types

from core import reg_originals as RO, sys_state as S

OPT = m.app("core.optimizer")
ST = m.app("core.scheduler_tweaks")
TM = m.app("core.timer_manager")
BP = m.app("core.boot_prep")
V = m.app("core.vbs_toggle")
SNAP = m.app("core.snapshots")
P = m.app("core.power")
AR = m.app("core.auto_recover")

POWER = r"HKLM\SYSTEM\CurrentControlSet\Control\Power"
OTHER_PLAN = "bbbbbbbb-5555-6666-7777-888888888888"     # another plan of the user's own
HVCI = r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity"
RUN = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
KEY = r"HKLM\SOFTWARE\Vendor\Thing"
FSE = r"HKCU\System\GameConfigStore"
GAMES = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
TASK = m.TELEMETRY_TASKS[0]


def _rows(result, category, name):
    return [r["ok"] for r in result[category] if r["name"] == name]


def _write(key, name, data, vtype="REG_DWORD"):
    """What every tweak does: note the prior value, then write."""
    RO.remember(key, name, data, vtype)
    return m.run_cmd(["reg", "add", key, "/v", name, "/t", vtype, "/d", data, "/f"])["ok"]


def _fail(*words):
    """A command hook: the matching command fails; anything else runs."""
    return lambda a: m.F("failed") if all(w.lower() in [p.lower() for p in a] for w in words) else None


# ── hibernation (Pro Mode) ────────────────────────────────────────────
def test_hibernation_the_user_had_off_stays_off_after_reset_all():
    m.fresh()
    OPT.set_pro_mode(True)
    m.HIBERNATE[0] = False
    m.rset(POWER, "HibernateEnabled", 0)            # `powercfg -h off`, done by the user
    OPT.run_full_optimize(["boot"])
    assert RO.noted("hibernation_disabled") is None, "it was off already: not this app's doing"
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert m.HIBERNATE[0] is False and not m.ran("powercfg", "-h")


def test_hibernation_that_was_on_is_noted_and_switched_back_on():
    m.fresh()
    OPT.set_pro_mode(True)
    m.rset(POWER, "HibernateEnabled", 1)
    OPT.run_full_optimize(["boot"])
    assert RO.noted("hibernation_disabled") and m.HIBERNATE[0] is False
    OPT.reset_all_optimizations()
    assert m.HIBERNATE[0] is True and RO.noted("hibernation_disabled") is None


def test_hibernation_that_could_not_be_switched_back_on_stays_noted():
    m.fresh()
    OPT.set_pro_mode(True)
    OPT.run_full_optimize(["boot"])
    m.CMD_HOOKS.append(lambda a: m.F("There is not enough space on the disk.")
                       if a[:3] == ["powercfg", "-h", "on"] else None)
    res = OPT.reset_all_optimizations()
    assert res["boot"][0]["ok"] is False, "reported, not 'restored'"
    assert RO.noted("hibernation_disabled"), "kept, so the next Reset All tries again"
    del m.CMD_HOOKS[:]
    res = OPT.reset_all_optimizations()
    assert res["boot"][0]["ok"] is True and m.HIBERNATE[0] is True
    assert RO.noted("hibernation_disabled") is None


# ── power settings that cannot be recorded are not changed ────────────
def test_an_advanced_power_setting_that_cannot_be_read_is_left_alone():
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.F("unreadable") if a[:2] == ["powercfg", "/qh"] and a[-1] == m.BOOST else None)
    rows = OPT.run_full_optimize(["power_advanced"])["power_advanced"]
    assert [r["ok"] for r in rows if "boost" in r["name"].lower()] == [False]
    assert not [t for t in m.PTOUCH if t[1] == m.BOOST], "changed with no record, it could never be undone"
    assert m.cell(m.USERPLAN, m.BOOST) == [5, 6]
    assert RO.summary()["power"] == 14, "the other fourteen are recorded and applied"


def test_cpu_idle_is_not_switched_off_when_it_could_not_be_put_back():
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.F("unreadable") if a[:2] == ["powercfg", "/qh"] else None)
    assert ST.toggle_cpu_idle(True)["ok"] is False
    assert m.cell(m.USERPLAN, m.IDLE) == [0, 0] and not m.PTOUCH
    del m.CMD_HOOKS[:]
    assert ST.toggle_cpu_idle(True)["ok"] is True and m.cell(m.USERPLAN, m.IDLE)[0] == 1


# ── records that point at something deleted since ─────────────────────
def test_power_records_on_a_deleted_plan_are_dropped_not_failed_at_every_reset():
    m.fresh()
    OPT.run_full_optimize(["power", "power_advanced"])
    clone = m.PLAN["active"]
    assert clone != m.USERPLAN and RO.summary()["power"] == 15
    m.run_cmd(["powercfg", "/setactive", m.USERPLAN])
    m.PLAN["schemes"].discard(clone)                # the user deletes the Ultimate Performance copy
    m.CMD_HOOKS.append(lambda a: m.F("busy") if a[:2] == ["powercfg", "/list"] else None)
    res = OPT.reset_all_optimizations()
    assert res["originals"][0]["ok"] is False and RO.summary()["power"] == 15, \
        "a listing that failed says nothing about the plan: kept"
    del m.CMD_HOOKS[:]
    res = OPT.reset_all_optimizations()
    assert res["originals"][0]["ok"] is True and RO.summary()["power"] == 0
    assert m.PLAN["active"] == m.USERPLAN


def test_a_baseline_plan_deleted_since_gives_way_to_the_plan_the_user_is_on_now():
    m.fresh(plan=m.USERPLAN)
    OPT.apply_ultimate_power_plan()                 # baseline: the user's plan
    m.PLAN["schemes"].discard(m.USERPLAN)           # ...which an update removes
    m.PLAN["schemes"].add(OTHER_PLAN)
    m.run_cmd(["powercfg", "/setactive", OTHER_PLAN])   # the user picks another plan of their own
    m.CMD_HOOKS.append(lambda a: m.F("busy") if a[:2] == ["powercfg", "/list"] else None)
    assert OPT._read_power_baseline() == m.USERPLAN, "not dropped on a failed listing"
    del m.CMD_HOOKS[:]
    OPT.apply_ultimate_power_plan()                 # Apply All again (or the next launch's prep)
    assert OPT._read_power_baseline() == OTHER_PLAN
    OPT.reset_all_optimizations()
    assert m.PLAN["active"] == OTHER_PLAN, "the stale baseline used to send this user to Balanced"


def test_a_service_uninstalled_since_is_not_a_failure_at_every_reset():
    m.fresh()
    S.set_service_start("DiagTrack", "demand", stop=True)
    m.run_cmd(["reg", "delete", r"HKLM\SYSTEM\CurrentControlSet\Services\DiagTrack", "/f"])
    m.clear_logs()
    assert S.restore_kind("service") == {"restored": 1, "failed": 0}
    assert not RO.has_state("service") and not m.ran("sc")


# ── snapshots ─────────────────────────────────────────────────────────
def test_automatic_snapshots_are_pruned_to_the_newest_ten():
    m.fresh()
    manual = SNAP.take_snapshot("mine")["id"]
    ids = []
    for i in range(12):
        time.sleep(0.02)                            # (distinct timestamps to sort by)
        ids.append(SNAP.auto_snapshot_before(f"apply {i}"))
    assert all(ids)
    assert {s["id"] for s in SNAP.list_snapshots()} == set(ids[2:]) | {manual}, \
        "one is taken at every Apply and Reset All, and none was ever deleted"


def test_a_snapshot_whose_meta_cannot_be_written_leaves_nothing_behind():
    m.fresh()
    real = SNAP.atomic_write_json
    SNAP.atomic_write_json = lambda path, data: False
    try:
        r = SNAP.take_snapshot("x")
    finally:
        SNAP.atomic_write_json = real
    assert r["ok"] is False and os.listdir(SNAP.SNAPSHOTS_DIR) == []
    snap = SNAP.take_snapshot("y")
    assert snap["ok"] and SNAP.get_snapshot(snap["id"])["name"] == "y"
    assert sorted(os.listdir(os.path.join(SNAP.SNAPSHOTS_DIR, snap["id"]))) == ["meta.json"]


# ── standby list, ISLC ────────────────────────────────────────────────
def test_standby_purge_switches_on_the_privilege_it_needs():
    m.fresh()
    TM._islc_state["clears"] = 0
    m.PS_HOOKS.append(lambda c: m.R("FALLBACK") if "MemoryAPI" in c else None)
    r = TM.clear_standby_list()
    assert r["ok"] is False and r["fallback"], "the fallback frees nothing: it used to say ok"
    assert TM._islc_state["clears"] == 0
    assert m.ps_ran("RtlAdjustPrivilege(13, true, false")
    del m.PS_HOOKS[:]
    m.PS_HOOKS.append(lambda c: m.R("OK") if "MemoryAPI" in c or "public class SB" in c else None)
    assert TM.clear_standby_list() == {"ok": True} and TM._islc_state["clears"] == 1
    assert BP._clear_standby_list() is True and m.ps_ran("RtlAdjustPrivilege(13,true,false")


def test_islc_stopped_and_started_again_keeps_one_loop():
    m.fresh()
    real_time = TM.time
    TM.time = types.SimpleNamespace(sleep=lambda s: time.sleep(0.02))
    try:
        TM.start_islc()
        first = TM._islc_state["thread"]
        TM.stop_islc()
        TM.start_islc()                             # within one sleep of the first loop
        second = TM._islc_state["thread"]
        assert second is not first
        assert m.wait(lambda: not first.is_alive(), 2), "the old loop woke up and ran beside the new one"
        assert second.is_alive()
    finally:
        TM.stop_islc()
        m.wait(lambda: not TM._islc_state["thread"].is_alive(), 2)
        TM.time = real_time
    assert not second.is_alive()


# ── results that used to say ok whatever happened ─────────────────────
def test_hpet_row_says_whether_it_was_done():
    m.fresh()
    OPT.set_pro_mode(True)
    m.PS_HOOKS.append(lambda c: m.F("Disable-PnpDevice : Generic failure") if "Disable-PnpDevice" in c else None)
    assert _rows(OPT.run_full_optimize(["latency"]), "latency", "[Pro] HPET disabled") == [False]


def test_power_plan_fallback_says_whether_it_switched():
    m.fresh()
    m.DUPLICATE_OK[0] = False                       # no Ultimate Performance template
    m.PLAN["schemes"].discard(m.HIGH_PERF)          # ...and no High Performance plan (Modern Standby)
    assert OPT.apply_ultimate_power_plan()["ok"] is False and m.PLAN["active"] == m.USERPLAN
    m.PLAN["schemes"].add(m.HIGH_PERF)
    assert OPT.apply_ultimate_power_plan()["ok"] is True and m.PLAN["active"] == m.HIGH_PERF


def test_memory_integrity_off_is_not_reported_while_the_lock_flag_could_not_be_cleared():
    m.fresh()
    m.rset(HVCI, "Enabled", 1)
    m.rset(HVCI, "Locked", 1)
    m.REG_FAIL.add((HVCI.lower(), "locked"))
    assert V.disable_hvci()["ok"] is False
    m.REG_FAIL.clear()
    assert V.disable_hvci()["ok"] is True and m.rdata(HVCI, "Locked") == 0


def test_trim_check_reads_the_ntfs_flag_not_any_zero():
    for ntfs, ok in (("1", False), ("0", True)):
        m.fresh()
        out = (f"NTFS DisableDeleteNotify = {ntfs}  (TRIM)\n"
               "ReFS DisableDeleteNotify = 0  (Allows TRIM operations to be sent to the storage device)")
        m.CMD_HOOKS.append(lambda a, out=out: m.R(out) if a[1:4] == ["behavior", "query", "disabledeletenotify"]
                           else None)
        assert _rows(OPT.run_full_optimize(["storage"]), "storage", "TRIM Enabled") == [ok], ntfs


# ── shutdown / restart ────────────────────────────────────────────────
def _slow_cleanup():
    """Stands in for the cleanup pass (the real one empties the real TEMP
    folder): it runs until `gate` is set."""
    started, gate = threading.Event(), threading.Event()
    P._quick_cleanup = lambda: (started.set(), gate.wait(5))
    return started, gate


def test_cancel_pressed_during_the_cleanup_pass_stops_the_shutdown():
    m.fresh()
    real = P._quick_cleanup
    try:
        started, gate = _slow_cleanup()
        assert P.shutdown(delay_sec=10)["ok"] and started.wait(5)
        assert P.cancel()["ok"], "there was one to cancel, though `shutdown /a` finds none yet"
        gate.set()
        time.sleep(0.3)
        assert not m.ran("shutdown", "/s"), "the forced shutdown used to go ahead after the cleanup"
        started, gate = _slow_cleanup()             # and without Cancel it does go ahead
        assert P.restart(delay_sec=10)["ok"] and started.wait(5)
        gate.set()
        assert m.wait(lambda: m.ran("shutdown", "/r"), 3)
    finally:
        P._quick_cleanup = real


# ── startup items ─────────────────────────────────────────────────────
def test_startup_entries_with_spaces_in_their_names_are_listed_and_disabled_whole():
    m.fresh()
    m.rset(RUN, "Razer Synapse", r'"C:\Program Files\Razer\Synapse3.exe" --startup')
    m.rset(RUN, "Razer", r"C:\Other\razer.exe")    # another entry, named like the first word
    m.rset(RUN, "Tool", r"%ProgramFiles%\Tool\tool.exe", "REG_EXPAND_SZ")
    items = {i["name"]: i["command"] for i in OPT.scan_startup_items() if i["location"] == "HKCU\\Run"}
    assert items == {"Razer Synapse": r'"C:\Program Files\Razer\Synapse3.exe" --startup',
                     "Razer": r"C:\Other\razer.exe", "Tool": r"%ProgramFiles%\Tool\tool.exe"}
    assert OPT.disable_startup_item("Razer Synapse", "HKCU\\Run")["ok"]
    assert m.rget(RUN, "Razer Synapse") is None and m.rdata(RUN, "Razer") == r"C:\Other\razer.exe", \
        "this used to delete 'Razer', the wrong entry"


# ── legacy recovery: each check once per machine ──────────────────────
def _recover():
    return [r["id"] for r in AR.recover_legacy_bad_tweaks()["reverted"]]


def test_legacy_recovery_runs_each_check_once_per_machine():
    m.fresh()
    m.rset(FSE, "GameDVR_FSEBehaviorMode", 2)       # left by an old build
    assert _recover() == ["fso_global"]
    assert m.rdata(FSE, "GameDVR_FSEBehaviorMode") == 0
    assert AR.get_pending_recovery_report()["pending"], "the report and its toast are as they were"
    m.rset(FSE, "GameDVR_FSEBehaviorMode", 2)       # now the user's own choice
    m.clear_logs()
    assert _recover() == [] and m.rdata(FSE, "GameDVR_FSEBehaviorMode") == 2, \
        "it used to be reverted at every launch"
    assert not m.CMDS and not m.ps_ran("ScheduledDefrag"), "a finished check is not even run"


def test_a_legacy_check_whose_revert_failed_runs_again():
    m.fresh()
    m.rset(FSE, "GameDVR_FSEBehaviorMode", 2)
    m.REG_FAIL.add((FSE.lower(), "gamedvr_fsebehaviormode"))
    assert _recover() == [], "not reported as reverted"
    assert m.rdata(FSE, "GameDVR_FSEBehaviorMode") == 2
    m.REG_FAIL.clear()
    assert _recover() == ["fso_global"] and m.rdata(FSE, "GameDVR_FSEBehaviorMode") == 0


def test_a_legacy_check_that_could_not_look_runs_again():
    m.fresh()
    m.BCD["recoveryenabled"] = "No"
    m.BCD_OK[0] = False                             # the boot store could not be opened this time
    assert _recover() == [], "not taken for 'nothing there'"
    m.BCD_OK[0] = True
    assert _recover() == ["winre_disabled"] and m.bcd_get("recoveryenabled") == "Yes"


def test_a_legacy_check_added_in_a_later_version_still_runs_once():
    m.fresh()
    with open(AR._DONE_PATH, "w", encoding="utf-8") as f:
        json.dump({"done": ["_check_fso_global"]}, f)       # what an earlier version finished
    m.rset(FSE, "GameDVR_FSEBehaviorMode", 2)
    m.rset(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", "HubMode", 1)
    assert _recover() == ["hubmode"]
    assert m.rdata(FSE, "GameDVR_FSEBehaviorMode") == 2
    with open(AR._DONE_PATH, encoding="utf-8") as f:
        assert {"_check_fso_global", "_check_hubmode", "_check_memory_placebos"} <= set(json.load(f)["done"])


# ── the records file ──────────────────────────────────────────────────
def test_a_damaged_records_file_is_moved_aside_not_overwritten():
    m.fresh()
    with open(RO._PATH, "w", encoding="utf-8") as f:
        f.write('{"registry": {"optimizer||HKLM\\\\X|V": {"type": "REG_DW')
    RO._cache = None
    assert RO.summary()["registry"] == 0
    folder = os.path.dirname(RO._PATH)
    aside = [n for n in os.listdir(folder) if n.startswith("originals.json.corrupt-")]
    assert len(aside) == 1 and not os.path.exists(RO._PATH)
    with open(os.path.join(folder, aside[0]), encoding="utf-8") as f:
        assert f.read().endswith('"REG_DW'), "kept as it was"
    m.rset(KEY, "V", 9)
    _write(KEY, "V", "1")                           # a new file for the records from here on
    RO._cache = None
    assert RO.has_original(KEY, "V")


def test_a_records_file_that_cannot_be_read_is_never_saved_over():
    m.fresh()
    m.rset(KEY, "A", 1)
    _write(KEY, "A", "2")                           # on record, on disk
    RO._cache = None                                # the app starts again...
    locked, real_open = [True], open

    def held_open(path, *a, **k):                   # ...while a scanner holds the file
        if locked[0] and os.path.abspath(path) == os.path.abspath(RO._PATH):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_open(path, *a, **k)

    RO.open = held_open
    try:
        m.rset(KEY, "B", 1)
        _write(KEY, "B", "2")
        assert RO.has_original(KEY, "B"), "kept for this session"
        with real_open(RO._PATH, encoding="utf-8") as f:
            assert list(json.load(f)["registry"]) == [f"optimizer||{KEY}|A"], \
                "not replaced by a file with only this session's records"
        locked[0] = False
        m.rset(KEY, "C", 1)
        _write(KEY, "C", "2")                       # readable again: both are kept
    finally:
        del RO.open
    RO._cache = None
    assert all(RO.has_original(KEY, n) for n in "ABC")
    assert RO.restore_all() == {"restored": 3, "failed": 0}
    assert [m.rdata(KEY, n) for n in "ABC"] == [1, 1, 1]


# ── nothing is changed without a record of what it was ────────────────
def test_features_count_only_what_dism_really_switched_off():
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.F("", "Error: 0x800f0922") if "/featurename:MediaPlayback" in a else None)
    m.DISM_3010.add("windowsmediaplayer")           # done, but DISM exits 3010: a restart finishes it
    assert S.disable_features(["WindowsMediaPlayer", "MediaPlayback", "Internet-Explorer-Optional-amd64"]) == 1
    assert m.FEATURES["WindowsMediaPlayer"] is False and m.FEATURES["MediaPlayback"] is True


def test_features_are_left_alone_when_dism_cannot_list_them():
    m.fresh()
    m.CMD_HOOKS.append(_fail("/get-features"))
    assert S.disable_features(["WindowsMediaPlayer"]) is None
    rows = OPT.run_full_optimize(["legacy"])["legacy"]
    assert {"name": "Optional Windows features: couldn't read the current state, nothing changed",
            "ok": False} in rows
    assert m.FEATURES["WindowsMediaPlayer"] is True and not m.ran("dism", "/disable-feature")


def test_defender_is_left_alone_when_its_settings_cannot_be_read():
    m.fresh()
    m.PS_HOOKS.append(lambda c: m.F("Get-MpPreference : timed out") if "Get-MpPreference" in c else None)
    before = json.loads(json.dumps(m.DEF))
    assert S.add_defender_exclusion("ExclusionPath", r"C:\XboxGames", current=None) is False
    assert S.set_defender_pref("MAPSReporting", "Disabled", 0, current=None) is False
    rows = OPT.run_full_optimize(["defender"])["defender"]
    assert rows == [{"name": "Defender: couldn't read the current state, nothing changed", "ok": False}]
    assert m.DEF == before and not m.ps_ran("Add-MpPreference") and not m.ps_ran("Set-MpPreference")
    assert RO.summary()["state"] == 0


def test_boot_tcp_task_ntfs_and_service_changes_need_their_current_state():
    m.fresh()
    m.CMD_HOOKS.extend([_fail("bcdedit", "/enum"), _fail("netsh", "dump"), _fail("schtasks", "/query"),
                        _fail("fsutil", "query")])
    m.rset(r"HKLM\SYSTEM\CurrentControlSet\Services\Fax", "Start", b"\x03", "REG_BINARY")   # unreadable
    snap = m.snap_system()
    assert S.set_bcd("quietboot", "yes") is False
    assert S.set_tcp_global("initialRto", "2000") is False
    assert S.disable_task(TASK) is False
    assert S.set_fsutil_behavior("disablelastaccess", 1) is False
    assert S.set_service_start("Fax", "disabled", stop=True) is False
    res = OPT.run_full_optimize(["latency", "boot", "network_stack"])
    assert {"name": "Boot settings (bcdedit): couldn't read the current state, nothing changed",
            "ok": False} in res["latency"] + res["boot"]
    assert {"name": "netsh TCP settings: couldn't read the current state, nothing changed",
            "ok": False} in res["network_stack"]
    assert m.snap_system() == snap and RO.summary()["state"] == 0
    assert not m.ran("bcdedit", "/set") and not m.ran("netsh", "global") and not m.ran("schtasks", "/change")
    assert not m.ran("fsutil", "set") and not m.ran("sc")


def test_telemetry_row_counts_the_tasks_really_disabled():
    m.fresh()
    rows = OPT.run_full_optimize(["telemetry_deep"])["telemetry_deep"]
    n = len(m.TELEMETRY_TASKS) - 1                  # one of them is not on this Windows
    assert {"name": f"Disabled {n} telemetry scheduled tasks", "ok": True} in rows


# ── startup prep ──────────────────────────────────────────────────────
def test_startup_prep_reports_a_registry_write_that_failed():
    m.wait(lambda: not BP._state["running"])
    m.fresh()
    BP._state.update(running=False, done=False, steps=[], started_at=0, finished_at=0)
    m.REG_FAIL.add((GAMES.lower(), "priority"))
    BP.prep_for_gaming_async()
    assert m.wait(lambda: BP._state["done"] and not BP._state["running"])
    steps = {s["name"]: s["ok"] for s in BP._state["steps"]}
    assert steps["MMCSS gaming profile"] is False, "it said done whatever the writes did"
    assert steps["Foreground boost"] is True and steps["Network throttle off"] is True
    assert m.rdata(GAMES, "Scheduling Category") == "High", "the step's other writes are still made"
