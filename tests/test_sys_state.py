"""core/sys_state.py - prior state of what a tweak changes outside the
registry: services, scheduled tasks, the boot entry, TCP globals, IPv6
tunnels, Windows features, NTFS flags and Defender preferences."""
import sandbox as m

from core import reg_originals as RO, sys_state as S

TASK = m.TELEMETRY_TASKS[0]


# ── services ──────────────────────────────────────────────────────────
def test_service_start_types_are_read_from_the_registry():
    m.fresh()
    assert S.service_start("Spooler") == "auto"
    assert S.service_start("WSearch") == "delayed-auto"
    assert S.service_start("Fax") == "demand"
    assert S.service_start("XboxGipSvc") == "disabled"
    assert S.service_start("NoSuchService") is None
    m.rset(r"HKLM\SYSTEM\CurrentControlSet\Services\disk", "Start", 0)     # a boot driver: not ours to touch
    assert S.service_start("disk") is None
    assert not m.ran("sc"), "nothing localised is parsed: sc.exe is not asked"


def test_service_change_is_recorded_and_restored():
    m.fresh()
    assert S.set_service_start("WSearch", "disabled", stop=True) is True
    assert m.svc_get("WSearch") == "disabled" and "wsearch" not in m.RUNNING
    assert RO.state_records("service") == {"WSearch": "delayed-auto"}
    assert S.restore_kind("service") == {"restored": 1, "failed": 0}
    assert m.svc_get("WSearch") == "delayed-auto", "delayed start, not plain automatic"
    assert "wsearch" in m.RUNNING, "it used to be running, so it is started again"
    assert not RO.has_state("service")


def test_service_already_set_that_way_is_not_recorded():
    m.fresh()
    S.set_service_start("XboxGipSvc", "disabled")       # the user disabled it themselves
    S.set_service_start("Fax", "demand")                # already manual
    S.set_service_start("NoSuchService", "demand")      # not installed
    assert not RO.has_state("service")


def test_first_service_change_wins():
    m.fresh()
    S.set_service_start("DiagTrack", "demand")
    S.set_service_start("DiagTrack", "disabled")
    assert RO.state_records("service") == {"DiagTrack": "auto"}


def test_manual_service_is_not_started_by_a_restore():
    m.fresh()
    S.set_service_start("Fax", "disabled")
    m.clear_logs()
    S.restore_kind("service")
    assert m.svc_get("Fax") == "demand"
    assert not [c for c in m.CMDS if c[:2] == ["sc", "start"]]


def test_service_that_cannot_be_restored_stays_on_record():
    m.fresh()
    S.set_service_start("Spooler", "demand")
    m.SC_FAIL.add("spooler")
    assert S.restore_kind("service") == {"restored": 0, "failed": 1}
    assert RO.has_state("service", "Spooler")
    m.SC_FAIL.clear()
    assert S.restore_kind("service") == {"restored": 1, "failed": 0}
    assert m.svc_get("Spooler") == "auto"


def test_sc_rejects_start_keywords_it_does_not_know():
    """Two reset commands used to say start= manual, which sc.exe refuses -
    the fake has to refuse it too or that bug could come back unseen."""
    m.fresh()
    assert m.run_cmd(["sc", "config", "Fax", "start=", "manual"])["ok"] is False
    assert m.run_cmd(["sc", "config", "Fax", "start=", "demand"])["ok"] is True


# ── scheduled tasks ───────────────────────────────────────────────────
def test_task_state_comes_from_the_settings_block_of_its_xml():
    m.fresh()
    assert S.task_enabled(TASK) is True
    m.TASKS[m._task_key(TASK)]["enabled"] = False
    assert S.task_enabled(TASK) is False, "the trigger's own <Enabled>true</Enabled> must not be mistaken for it"
    assert S.task_enabled(m.MISSING_TASK) is None


def test_task_without_an_enabled_element_is_enabled():
    m.fresh()
    m.TASK_NO_FLAG.add(m._task_key(TASK))
    assert "<Enabled>false" not in m.run_cmd(["schtasks", "/Query", "/TN", TASK, "/XML"])["out"]
    assert S.task_enabled(TASK) is True


def test_only_a_task_that_was_enabled_is_switched_back_on():
    m.fresh()
    assert S.disable_task(TASK) is True
    assert S.disable_task(m.USER_TASK_OFF) is True      # was off already: the user's choice
    assert S.disable_task(m.MISSING_TASK) is False
    assert RO.state_records("task") == {TASK: True}
    assert S.restore_kind("task") == {"restored": 1, "failed": 0}
    assert m.task_on(TASK) is True and m.task_on(m.USER_TASK_OFF) is False


def test_task_scopes_are_separate():
    m.fresh()
    S.disable_task(TASK, scope="debloat")
    assert S.restore_kind("task") == {"restored": 0, "failed": 0}
    assert m.task_on(TASK) is False
    assert S.restore_kind("task", scope="debloat")["restored"] == 1


# ── boot entry ────────────────────────────────────────────────────────
def test_bcd_values_skip_headings_and_need_a_real_entry():
    m.fresh()
    vals = S.bcd_values()
    assert vals["useplatformclock"] == "Yes" and vals["bootmenupolicy"] == "Standard"
    assert "windows" not in vals, "'Windows Boot Loader' is a heading, not an element"
    m.BCD_OK[0] = False
    assert S.bcd_values() is None
    m.BCD_OK[0] = True
    m.CMD_HOOKS.append(lambda a: m.R("The boot configuration data store could not be opened")
                       if a[0] == "bcdedit" else None)
    assert S.bcd_values() is None, "an error message that looks like 'word  words' is not an entry"


def test_bcd_same():
    assert S.bcd_same("Yes", "yes") and S.bcd_same("on", "Yes") and S.bcd_same("No", "off")
    assert S.bcd_same("Enhanced", "enhanced") and not S.bcd_same("Yes", "No")
    assert not S.bcd_same("Legacy", "Standard")


def test_bcd_change_records_value_or_absence_and_restores_it():
    m.fresh()
    S.set_bcd("useplatformclock", "no")                 # the user had Yes
    S.set_bcd("disabledynamictick", "yes")              # was not set at all
    S.set_bcd("bootmenupolicy", "Standard")             # already that: nothing to record
    assert RO.state_records("bcd") == {"useplatformclock": "Yes", "disabledynamictick": None}
    assert m.bcd_get("useplatformclock") == "No" and m.bcd_get("disabledynamictick") == "Yes"
    assert S.restore_kind("bcd") == {"restored": 2, "failed": 0}
    assert m.bcd_get("useplatformclock") == "Yes", "back to the user's own setting, not deleted"
    assert m.bcd_get("disabledynamictick") is None, "and the one that was absent is gone again"


def test_bcd_absent_restore_succeeds_when_it_is_already_gone():
    m.fresh()
    S.set_bcd("quietboot", "yes")
    m.BCD.pop("quietboot")                              # someone else removed it meanwhile
    assert S.restore_kind("bcd") == {"restored": 1, "failed": 0}


def test_unreadable_boot_store_records_nothing():
    m.fresh()
    m.BCD_OK[0] = False
    assert S.set_bcd("quietboot", "yes") is False
    assert not RO.has_state("bcd")


def test_restore_one_bcd_element_only():
    m.fresh()
    S.set_bcd("useplatformclock", "no")
    S.set_bcd("quietboot", "yes")
    assert S.restore_kind("bcd", only="useplatformclock") == {"restored": 1, "failed": 0}
    assert m.bcd_get("quietboot") == "Yes" and RO.has_state("bcd", "quietboot")


# ── TCP globals and the IPv6 tunnels ──────────────────────────────────
def test_tcp_globals_come_from_the_dump():
    m.fresh()
    vals = S.tcp_globals()
    assert vals["rsc"] == "enabled" and vals["initialrto"] == "1000"
    assert m.ran("netsh", "dump") and not m.ran("netsh", "show")
    m.TCP_OK[0] = False
    assert S.tcp_globals() is None


def test_tcp_change_is_recorded_and_restored():
    m.fresh()
    assert S.set_tcp_global("initialRto", "2000") is True
    assert S.set_tcp_global("rss", "enabled") is True           # already that
    assert S.set_tcp_global("chimney", "disabled") is False     # not a parameter on this Windows
    assert RO.state_records("tcp") == {"initialRto": "1000"}
    assert S.restore_kind("tcp") == {"restored": 1, "failed": 0}
    assert m.TCP["initialrto"] == "1000"
    assert not m.ran("netsh", "tcp", "reset"), "one parameter is put back, not the whole stack reset"


def test_ip_stack_states_are_read_as_enum_names():
    m.fresh()
    assert S.ip_stack_states() == {"teredo": "client", "6to4": "default", "isatap": "default",
                                   "heuristics": "enabled"}
    m.STACK_OK[0] = False
    assert S.ip_stack_states() is None


def test_ip_stack_change_is_recorded_and_restored():
    m.fresh()
    S.set_ip_stack_state("teredo", "disabled")
    S.set_ip_stack_state("heuristics", "disabled")
    assert RO.state_records("ipstack") == {"teredo": "client", "heuristics": "enabled"}
    assert S.restore_kind("ipstack") == {"restored": 2, "failed": 0}
    assert m.STACK["teredo"] == "client" and m.STACK["heuristics"] == "enabled"


def test_a_state_netsh_cannot_be_told_to_return_to_is_not_recorded():
    m.fresh()
    m.STACK["teredo"] = "relay"
    S.set_ip_stack_state("teredo", "disabled")
    assert not RO.has_state("ipstack")


# ── Windows features ──────────────────────────────────────────────────
def test_enabled_features_need_english_dism():
    m.fresh()
    on = S.enabled_features()
    assert "windowsmediaplayer" in on and "internet-explorer-optional-amd64" not in on
    assert all("/english" in [p.lower() for p in c] for c in m.ran("dism"))
    german = m.run_cmd(["dism", "/online", "/get-features", "/format:table"])["out"]
    assert "Enabled" not in german, "without /english the state words are localised"
    m.DISM_OK[0] = False
    assert S.enabled_features() is None


def test_only_features_that_were_on_are_switched_off_and_back_on():
    m.fresh()
    n = S.disable_features(["WindowsMediaPlayer", "Internet-Explorer-Optional-amd64", "MediaPlayback"])
    assert n == 2, "the one that was already off is skipped"
    assert len(m.ran("dism", "/disable-feature")) == 2
    assert RO.state_records("feature") == {"WindowsMediaPlayer": True, "MediaPlayback": True}
    assert S.restore_kind("feature") == {"restored": 2, "failed": 0}
    assert m.FEATURES["WindowsMediaPlayer"] and m.FEATURES["MediaPlayback"]
    assert m.FEATURES["Internet-Explorer-Optional-amd64"] is False, "never ours: stays off"


def test_feature_restore_that_needs_a_restart_counts_as_done():
    m.fresh()
    S.disable_features(["WindowsMediaPlayer"])
    m.DISM_3010.add("windowsmediaplayer")               # DISM exits 3010, not 0
    assert S.restore_kind("feature") == {"restored": 1, "failed": 0}


# ── NTFS flags ────────────────────────────────────────────────────────
def test_fsutil_reads_both_output_shapes():
    m.fresh()
    assert S.fsutil_behavior("disablelastaccess") == 2      # "name = 2 (...)"
    assert S.fsutil_behavior("disable8dot3") == 2           # "The registry state is: 2 (...)"
    m.FSUTIL_OK[0] = False
    assert S.fsutil_behavior("disablelastaccess") is None


def test_fsutil_change_is_recorded_and_restored():
    m.fresh()
    S.set_fsutil_behavior("disablelastaccess", 1)
    S.set_fsutil_behavior("memoryusage", 1)                 # already 1
    assert RO.state_records("fsutil") == {"disablelastaccess": 2}
    S.restore_kind("fsutil")
    assert m.FSUTIL["disablelastaccess"] == 2


# ── Defender ──────────────────────────────────────────────────────────
def test_defender_preferences_restore_to_their_previous_values():
    m.fresh()
    prefs = S.defender_prefs()
    assert prefs["SubmitSamplesConsent"] == 1 and prefs["ScanScheduleTime"] == "02:00:00"
    S.set_defender_pref("SubmitSamplesConsent", "NeverSend", 2, current=prefs)
    S.set_defender_pref("MAPSReporting", "Advanced", 2, current=prefs)      # already Advanced
    S.set_defender_pref("ScanScheduleTime", "04:00:00", "04:00:00", current=prefs)
    assert RO.state_records("defender") == {"SubmitSamplesConsent": 1, "ScanScheduleTime": "02:00:00"}
    assert m.DEF["SubmitSamplesConsent"] == 2
    assert S.restore_kind("defender") == {"restored": 2, "failed": 0}
    assert m.DEF["SubmitSamplesConsent"] == 1 and m.DEF["ScanScheduleTime"] == "02:00:00"


def test_only_exclusions_this_app_added_are_removed():
    m.fresh()
    prefs = S.defender_prefs()
    mine, theirs = r"C:\Program Files\Epic Games", m.USER_EXCLUSIONS[1]
    assert S.add_defender_exclusion("ExclusionPath", mine, current=prefs)
    assert S.add_defender_exclusion("ExclusionPath", theirs.upper(), current=prefs)    # the user's, other case
    assert S.add_defender_exclusion("ExclusionProcess", "steam.exe", current=prefs)
    assert set(RO.state_records("defender-exclusion")) == {f"ExclusionPath:{mine}", "ExclusionProcess:steam.exe"}
    assert S.restore_kind("defender-exclusion") == {"restored": 2, "failed": 0}
    assert m.DEF["ExclusionPath"] == m.USER_EXCLUSIONS, "the user's own exclusions are still there"
    assert m.DEF["ExclusionProcess"] == ["mygame.exe"]


def test_no_defender_means_nothing_recorded():
    m.fresh()
    m.DEF_OK[0] = False                                 # third-party antivirus
    prefs = S.defender_prefs()
    assert prefs is None
    S.set_defender_pref("MAPSReporting", "Disabled", 0, current=prefs)
    S.add_defender_exclusion("ExclusionPath", r"C:\XboxGames", current=prefs)
    assert RO.summary()["state"] == 0


def test_exclusion_that_could_break_out_of_the_command_is_refused():
    m.fresh()
    for bad in ('C:\\x" ; Remove-Item C:\\ -Recurse ; "', "C:\\$(calc)", "C:\\a`nb", ""):
        assert S.add_defender_exclusion("ExclusionPath", bad, current={}) is False
    assert S.add_defender_exclusion("ExclusionExtension", "exe", current={}) is False
    assert not m.ps_ran("Add-MpPreference")


# ── restoring only what this app changes ──────────────────────────────
def test_a_record_for_something_this_app_never_changes_is_dropped():
    """The records file is user-writable.  A planted entry must not turn a
    reset into 'reconfigure any service or boot entry you like'."""
    m.fresh()
    m.svc_set("WinDefend", "auto")
    RO.remember_state("service", "WinDefend", "disabled")
    RO.remember_state("bcd", "testsigning", "Yes")
    RO.remember_state("task", r"\Evil\Task", True)
    m.clear_logs()
    rows = S.restore_all()
    assert m.svc_get("WinDefend") == "auto" and m.bcd_get("testsigning") is None
    assert not m.ran("sc", "config") and not m.ran("bcdedit", "/set") and not m.ran("schtasks", "/change")
    assert RO.summary()["state"] == 0 and rows == []


def test_malformed_prior_values_are_refused():
    m.fresh()
    S.allow("service", ["Spooler"]); S.allow("bcd", ["quietboot"]); S.allow("tcp", ["rsc"])
    S.allow("fsutil", ["disablelastaccess"]); S.allow("defender", ["MAPSReporting", "ScanScheduleTime"])
    S.allow("ipstack", ["teredo"]); S.allow("feature", ["Bad Name;x"])
    S.allow("defender-exclusion", ['ExclusionPath:C:\\x" ; calc ; "'])
    RO.remember_state("service", "Spooler", "auto & calc")
    RO.remember_state("bcd", "quietboot", "yes & calc")
    RO.remember_state("tcp", "rsc", "enabled rss=disabled")
    RO.remember_state("fsutil", "disablelastaccess", "2 & calc")
    RO.remember_state("defender", "MAPSReporting", "0; calc")
    RO.remember_state("defender", "ScanScheduleTime", "04:00:00; calc")
    RO.remember_state("ipstack", "teredo", "relay")
    RO.remember_state("feature", "Bad Name;x", True)
    RO.remember_state("defender-exclusion", 'ExclusionPath:C:\\x" ; calc ; "', True)
    m.clear_logs()
    rows = S.restore_all()
    assert not [c for c in m.CMDS if "calc" in " ".join(c)] and not m.ps_ran("calc")
    assert not m.ran("dism", "/enable-feature")
    assert rows and all(r["ok"] is False for r in rows), "reported as failures, not silently skipped"


def test_restore_all_reports_one_row_per_kind():
    m.fresh()
    S.set_service_start("Spooler", "demand")
    S.set_service_start("Fax", "disabled")
    S.disable_task(TASK)
    S.set_bcd("quietboot", "yes")
    rows = S.restore_all()
    names = [r["name"] for r in rows]
    assert names == ["2 service(s) back to their previous start type", "1 scheduled task(s) re-enabled",
                     "1 boot setting(s) restored"]
    assert all(r["ok"] for r in rows)
    assert S.restore_all() == [], "nothing left on record"
