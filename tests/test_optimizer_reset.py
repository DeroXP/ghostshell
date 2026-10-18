"""Apply every optimizer tweak, press Reset All, and compare the machine with
what it was before - value by value.

The machine is a fake one (tests/sandbox.py) seeded with a user's own
settings, each different from both the Windows default and the value a tweak
writes.  "Restored" therefore means: back to what THIS user had - not to a
default.
"""
import sandbox as m

import os

from core import reg_originals as RO, sys_state as S

OPT = m.app("core.optimizer")
BP = m.app("core.boot_prep")

# The game folders that exist on this PC.  Steam's is already excluded from
# Defender scans by the user (sandbox.USER_EXCLUSIONS).
GAME_DIRS = {r"C:\Program Files (x86)\Steam", r"C:\Program Files\Epic Games", r"C:\XboxGames"}


class _Path:
    def __getattr__(self, name):
        return getattr(os.path, name)

    def exists(self, p):
        return p in GAME_DIRS if p in OPT._DEFENDER_EXCLUDE_PATHS else os.path.exists(p)


class _Os:
    path = _Path()

    def __getattr__(self, name):
        return getattr(os, name)


OPT.os = _Os()

# ── what Reset All may do to values the optimizer did not change ──────
# The per-category resets hold fixed Windows defaults (and policy values to
# delete) for things an OLDER build may have changed without keeping a
# record.  Each is applied only to a value that has no record AND still
# reads exactly the way that tweak left it, so a user's own setting is never
# one of them.
#
# FIXED_DEFAULTS is for exceptions: values a reset changes regardless.  There
# are none.  Add one only with the reason written next to it - a reset that
# touches anything not listed fails test_reset_changes_nothing_else.
FIXED_DEFAULTS: dict = {}

# The defaults used to be written unconditionally.  These are the values
# that overwrote, although the optimizer had not written them in that run -
# kept so the tests can put a user's own setting in every one of them and
# see it survive (_seed_once_overwritten).  For about half of them there is
# no fallback at all any more: it is not known what, if anything, a tweak
# ever wrote there.
_DESKTOP = r"hkcu\control panel\desktop"
_GAMECFG = r"hkcu\system\gameconfigstore"
_DWM = r"hkcu\software\microsoft\windows\dwm"
_SYSPROFILE = r"hklm\software\microsoft\windows nt\currentversion\multimedia\systemprofile"
_DPP = _SYSPROFILE + r"\tasks\displaypostprocessing"
_NV0 = r"hklm\system\currentcontrolset\control\class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"
_MEMORY = r"hklm\system\currentcontrolset\control\session manager\memory management"
_EDGE = r"hklm\software\policies\microsoft\edge"
_WU = r"hklm\software\policies\microsoft\windows\windowsupdate"
ONCE_OVERWRITTEN = {
    _DESKTOP: {"dragfullwindows", "fontsmoothing", "fontsmoothingtype", "foregroundlocktimeout"},
    _GAMECFG: {"gamedvr_dxgihonorfsewindowscompatible", "gamedvr_efsefeatureflags", "gamedvr_fsebehaviormode",
               "gamedvr_honoruserfsebehaviormode"},
    _DWM: {"disablehwacceleration", "composition", "overlayminfps"},
    _SYSPROFILE: {"networkthrottlingindex"},
    _DPP: {"background only", "gpu priority", "priority", "scheduling category", "sfio priority"},
    _NV0: {"perflevelsrc", "powermizerenable", "powermizerlevel", "powermizerlevelac"},
    _MEMORY: {"clearpagefileatshutdown", "disablepagingexecutive", "largesystemcache",
              "featuresettingsoverride", "featuresettingsoverridemask"},
    r"hklm\system\currentcontrolset\services\ndu": {"start"},
    _EDGE: {"alternateerrorpagesenabled", "autofillcreditcardenabled", "browseraddprofileenabled",
            "hardwareaccelerationmodeenabled", "newtabpageprerender", "passwordmanagerenabled", "preloadonstartup",
            "tabfreezeenabled", "tabsleeping", "smartscreenenabled", "smartscreenpuaenabled"},
    r"hklm\software\policies\microsoft\windows\appprivacy": {"letappsaccessnotifications", "letappsactivate",
                                                              "letappsgetdiagnosticinfo"},
    r"hklm\software\policies\microsoft\windows\personalization": {"lockscreenimage", "nochanginglockscreen"},
    r"hklm\software\policies\microsoft\windows\windows search": {"preventindexingoutlook"},
    _WU: {"branchreadinesslevel", "disabledualscan", "donotconnecttowindowsupdateinternetlocations"},
    _WU + r"\au": {"automaticmaintenanceenabled", "noautoupdate", "scheduledinstallday", "scheduledinstalltime"},
    r"hklm\system\currentcontrolset\control\bluetooth\audio\avrcp\ct": {"disableabsolutevolume"},
    r"hklm\system\currentcontrolset\control\power\powersettings\54533251-82be-4824-96c1-47b60b740d00"
    r"\0cc5b647-c1df-4637-891a-dec35c318583": {"valuemax"},
    r"hklm\system\currentcontrolset\control\session manager\kernel": {"threaddpcenable"},
    # written only in Pro Mode, deleted by every reset:
    r"hklm\system\currentcontrolset\control\graphicsdrivers": {"tdrdelay", "tdrddidelay"},
    r"hklm\software\microsoft\windows\dwm": {"overlaytestmode"},
}
_SERVICES_KEY = r"system\currentcontrolset\services" + "\\"


def _is_fixed_default(flat_key) -> bool:
    hive, sub, name = flat_key
    return name in FIXED_DEFAULTS.get(f"{hive}\\{sub}".lower(), ())


def _tweak_values(flat: dict) -> dict:
    """The registry minus what is compared elsewhere: a service's start type
    is changed through sc.exe and checked as a start type (snap_system)."""
    return {k: v for k, v in flat.items()
            if not (k[1].startswith(_SERVICES_KEY) and k[2] in ("start", "delayedautostart")
                    and k[1][len(_SERVICES_KEY):] in {s.lower() for s in m.SERVICES})}


def user_value(vtype, text):
    """A value a user could have had there: same type, different data."""
    if vtype in ("REG_DWORD", "REG_QWORD"):
        n = int(text, 16) if text.lower().startswith("0x") else int(text)
        return (n ^ 0x40) & 0xFFFFFFFF
    if vtype == "REG_BINARY":
        return b"\xaa" + bytes.fromhex(text)
    if vtype == "REG_MULTI_SZ":
        return ["user"] + (text.split("\\0") if text else [])
    return "user_" + text


def _flat_key(key, name):
    hive, sub = m.kp(key)
    return hive, sub, name.lower()


_TEXT_VALUES = {"dragfullwindows", "fontsmoothing", "fontsmoothingtype", "background only", "scheduling category",
                "sfio priority", "lockscreenimage"}


def _seed_once_overwritten(skip=()) -> None:
    """A user's own setting in every value a reset used to overwrite: not a
    Windows default, and not what any tweak writes."""
    for path, names in ONCE_OVERWRITTEN.items():
        for name in names:
            if _flat_key(path, name) not in skip:
                m.rset(path, name, "mine" if name in _TEXT_VALUES else 0x55)


NV_KEY = m.DISPLAY_CLASS + r"\0001"


def _nvidia_adapter() -> None:
    r"""The NVIDIA card is the second display adapter on this PC; \0000 is a
    virtual one.  (After m.fresh(), which forgets it.)"""
    m.PS_HOOKS.append(lambda c: m.R("0000|Meta Virtual Monitor\n0001|NVIDIA GeForce RTX 5070 Ti")
                      if "DriverDesc" in c else None)


def _fallbacks():
    """Every registry value Reset All holds a fixed default for, learned by
    watching one reset (in Pro Mode, which has a few more):
    [(key, value, (what a tweak leaves there, ...), the default - None for
    "no value" -, type)]."""
    m.fresh()
    OPT.set_pro_mode(True)
    _nvidia_adapter()
    seen, real = [], OPT._reg_default

    def watch(key, value, tweak, stock, vtype="REG_DWORD"):
        seen.append((key, value, tweak if isinstance(tweak, tuple) else (tweak,), stock, vtype))
        return real(key, value, tweak, stock, vtype)

    OPT._reg_default = watch
    try:
        OPT.reset_all_optimizations()
    finally:
        OPT._reg_default = real
    return seen


def _discover(pro):
    """Run optimize and reset once on a scratch machine to learn every
    registry value either of them writes or deletes."""
    m.fresh()
    OPT.set_pro_mode(pro)
    m.clear_logs()
    OPT.run_full_optimize()
    targets = {_flat_key(k, n): (k, n, t, d) for k, n, t, d in m.WRITES if n}
    m.clear_logs()
    OPT.reset_all_optimizations()
    others = {_flat_key(k, n): (k, n, t, d) for k, n, t, d in m.WRITES if n}
    others.update({_flat_key(k, n): (k, n, "REG_DWORD", "1") for k, n in m.DELETES if n})
    return targets, {fk: v for fk, v in others.items() if fk not in targets}


def _cycle(pro=False, seed=True, categories=None):
    targets, others = _discover(pro)
    m.fresh()
    OPT.set_pro_mode(pro)
    _nvidia_adapter()
    if seed:
        for i, (k, n, t, d) in enumerate(targets.values()):
            if i % 5 != 4:                          # every fifth value does not exist yet on this PC
                m.rset(k, n, user_value(t, d), t)
        for k, n, t, d in others.values():
            m.rset(k, n, user_value(t, d), t)
        _seed_once_overwritten(skip=targets)
    m.cell(m.USERPLAN, m.PROC_MIN)[:] = [12, 34]
    c = {"targets": targets, "reg_before": _tweak_values(m.flat()), "sys_before": m.snap_system()}
    m.clear_logs()
    c["optimize"] = OPT.run_full_optimize(categories)
    c["records"] = RO.summary()
    c["unrecorded"] = [v[:2] for v in targets.values() if not RO.has_original(v[0], v[1])]
    c["sys_mid"] = m.snap_system()
    c["reg_mid"] = _tweak_values(m.flat())
    m.clear_logs()
    c["reset"] = OPT.reset_all_optimizations()
    c["reg_after"], c["sys_after"] = _tweak_values(m.flat()), m.snap_system()
    return c


def _diff(a, b):
    return {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}


# ═════════════════════════════════════════════════════════════════════
# The full cycle
# ═════════════════════════════════════════════════════════════════════
def test_every_registry_value_the_optimizer_writes_is_on_record():
    c = _cycle()
    assert len(c["targets"]) > 150, "the optimizer writes a couple of hundred values"
    assert c["unrecorded"] == [], "a value written without a record can never be restored"
    assert c["records"]["registry"] == len(c["targets"])


def test_the_tweaks_really_were_applied():
    c = _cycle()
    changed = {k for k in _diff(c["reg_before"], c["reg_mid"]) if k[2]}     # (named values)
    assert changed == set(c["targets"]), "every target value changed, and nothing else did"
    before, mid = c["sys_before"], c["sys_mid"]
    assert mid["services"]["Spooler"] == "demand" and mid["services"]["WerSvc"] == "disabled"
    assert mid["services"]["XblAuthManager"] == "demand"
    assert not any(mid["tasks"].values()), "every telemetry task is off"
    assert mid["bcd"]["bootmenupolicy"] == "Legacy" and mid["bcd"]["useplatformclock"] == "No"
    assert mid["tcp"]["initialrto"] == "2000" and mid["tcp"]["hystart"] == "disabled"
    assert mid["features"]["WindowsMediaPlayer"] is False and mid["features"]["VirtualMachinePlatform"] is False
    assert mid["fsutil"]["disablelastaccess"] == 1
    assert mid["defender"]["SubmitSamplesConsent"] == 2 and mid["defender"]["ScanScheduleTime"] == "04:00:00"
    assert r"C:\Program Files\Epic Games" in mid["defender"]["ExclusionPath"]
    assert mid["plan"] not in (m.USERPLAN, m.BALANCED, m.HIGH_PERF), "on the Ultimate Performance copy"
    assert before != mid


def test_reset_restores_every_registry_value_exactly():
    c = _cycle()
    wrong = {k: v for k, v in _diff(c["reg_before"], c["reg_after"]).items() if k in c["targets"]}
    assert wrong == {}, "type and data, and gone again where it did not exist"
    absent = [k for k in c["targets"] if k not in c["reg_before"]]
    assert len(absent) > 30 and not any(k in c["reg_after"] for k in absent)


def test_reset_changes_nothing_else():
    c = _cycle()
    stray = sorted(k for k in _diff(c["reg_before"], c["reg_after"])
                   if k not in c["targets"] and not _is_fixed_default(k))
    assert stray == [], "Reset All touched values that are neither a tweak's nor on the known list"


def test_reset_on_a_clean_windows_removes_what_the_tweaks_created():
    c = _cycle(seed=False)
    left = sorted(k for k in c["targets"] if k in c["reg_after"] and not _is_fixed_default(k))
    assert left == [], "values that did not exist before the optimizer ran are still there"
    assert not m.key_exists(r"HKCU\Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}")


def test_reset_restores_services_tasks_boot_network_features_and_defender():
    c = _cycle()
    assert c["sys_after"] == c["sys_before"]
    after = c["sys_after"]
    # ...which includes the user's own choices, made before the app was ever run:
    assert after["services"][m.USER_SERVICE_OFF] == "disabled"
    assert after["services"]["WSearch"] == "delayed-auto" and after["services"]["edgeupdate"] == "delayed-auto"
    assert after["tasks"][m.USER_TASK_OFF] is False
    assert after["bcd"]["useplatformclock"] == "Yes" and after["bcd"]["recoveryenabled"] == "Yes"
    assert after["defender"]["ExclusionPath"] == m.USER_EXCLUSIONS
    assert after["defender"]["ExclusionProcess"] == ["mygame.exe"]
    assert after["features"]["Internet-Explorer-Optional-amd64"] is False, "was off before; stays off"
    assert after["plan"] == m.USERPLAN


def test_reset_puts_the_power_plan_back_and_leaves_its_settings_alone():
    c = _cycle()
    assert m.PLAN["active"] == m.USERPLAN
    assert m.cell(m.USERPLAN, m.PROC_MIN) == [12, 34]
    assert all(v == [12, 34] or v == list(m.POWER_DEFAULTS.get(s, [5, 6]))
               for s, v in m.plan_values(m.USERPLAN).items())
    assert not os.path.exists(OPT._POWER_BASELINE_PATH), "whatever the user picks next is the next baseline"
    clone = c["sys_mid"]["plan"]
    assert all(v == list(m.POWER_DEFAULTS.get(s, [5, 6])) for s, v in m.plan_values(clone).items()), \
        "the performance plan's own settings are put back too"


def test_reset_does_not_use_the_blunt_instruments():
    _cycle()
    assert not m.ran("powercfg", "restoredefaultschemes"), "that deletes every custom power plan"
    assert not m.ran("netsh", "winsock"), "that removes VPN and security-software providers"
    assert not [c for c in m.CMDS if c[0] == "netsh" and "reset" in c], "previous values are restored instead"
    assert not m.ran("dism", "/disable-feature")
    assert len(m.ran("dism", "/enable-feature")) == 6, "exactly the features that were on before"
    assert not m.ran("powercfg", "-h"), "hibernation was never switched off (Pro Mode is off)"


def test_reset_leaves_nothing_on_record():
    c = _cycle()
    left = RO.summary()
    assert (left["registry"], left["power"], left["state"]) == (0, 0, 0)
    assert all(row.get("ok") for rows in c["reset"].values() for row in rows), "no step reported a failure"
    assert "restored to the exact value" in c["reset"]["originals"][0]["name"]


def test_reset_switches_the_startup_tweaks_off():
    _cycle()
    assert BP.get_status()["persistent"] is False, "or the next launch would re-apply what was just undone"


def test_a_second_reset_changes_no_system_state():
    _cycle()
    system = m.snap_system()
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert m.snap_system() == system, "the power plan included: the user's own is not swapped for Balanced"
    assert not m.ran("sc", "config") and not m.ran("dism", "/enable-feature") and not m.ran("schtasks", "/change")
    assert not m.ran("powercfg", "setactive") and not m.PTOUCH


def test_a_second_reset_changes_no_registry_value():
    for pro in (False, True):
        c = _cycle(pro=pro)
        assert c["reg_after"] == c["reg_before"], "(the first one put everything back)"
        before = m.flat()
        m.clear_logs()
        OPT.reset_all_optimizations()
        assert m.flat() == before, f"Pro Mode {pro}"
        assert not m.WRITES and not m.DELETES, "with nothing on record, not one `reg add` or `reg delete`"
        assert not m.ps_ran("SystemParametersInfo"), "nor the live visual-effects switch"


def test_optimize_twice_then_reset_still_returns_the_first_values():
    targets, _ = _discover(False)
    m.fresh()
    for k, n, t, d in targets.values():
        m.rset(k, n, user_value(t, d), t)
    before, system = m.flat(), m.snap_system()
    OPT.run_full_optimize()
    OPT.run_full_optimize()                         # finds its own tweaks: must not record those as originals
    OPT.reset_all_optimizations()
    assert {k: v for k, v in _diff(before, m.flat()).items() if k in targets} == {}
    assert m.snap_system() == system


def test_a_value_that_cannot_be_restored_is_reported_and_kept_on_record():
    targets, _ = _discover(False)
    m.fresh()
    key, name = r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation"
    m.rset(key, name, 2)
    OPT.run_full_optimize(["cpu"])
    m.REG_FAIL.add((key.lower(), name.lower()))
    res = OPT.reset_all_optimizations()
    assert res["originals"][0]["ok"] is False
    assert RO.has_original(key, name), "the next Reset All tries again"
    m.REG_FAIL.clear()
    OPT.reset_all_optimizations()
    assert m.rdata(key, name) == 2


# ═════════════════════════════════════════════════════════════════════
# Pro Mode
# ═════════════════════════════════════════════════════════════════════
def test_pro_mode_cycle_is_exact_too():
    plain = len(_discover(False)[0])
    c = _cycle(pro=True)
    assert len(c["targets"]) > plain, "Pro Mode writes more"
    assert c["unrecorded"] == []
    assert {k: v for k, v in _diff(c["reg_before"], c["reg_after"]).items() if k in c["targets"]} == {}
    assert c["sys_after"] == c["sys_before"]
    mid = c["sys_mid"]
    assert mid["bcd"].get("disabledynamictick") == "Yes" and mid["hibernate"] is False
    assert c["sys_after"]["hibernate"] is True and m.ran("powercfg", "-h", "on")
    assert RO.noted("hibernation_disabled") is None


def test_pro_mode_search_indexing_goes_back_to_delayed_start():
    m.fresh()
    OPT.set_pro_mode(True)
    OPT.run_full_optimize(["storage"])
    assert m.svc_get("WSearch") == "disabled" and "wsearch" not in m.RUNNING
    OPT.reset_all_optimizations()
    assert m.svc_get("WSearch") == "delayed-auto" and "wsearch" in m.RUNNING


def test_pro_mode_only_tweaks_stay_off_without_it():
    c = _cycle(pro=False)
    mid = c["sys_mid"]
    assert mid["services"]["WSearch"] == "delayed-auto" and "disabledynamictick" not in mid["bcd"]
    assert mid["hibernate"] is True
    assert not m.ps_ran("Disable-PnpDevice") and not m.ps_ran("Disable-MMAgent")
    skipped = [r["name"] for rows in c["optimize"].values() if isinstance(rows, list) for r in rows if r.get("skipped")]
    assert len(skipped) >= 6


def test_usb_msi_mode_skips_devices_that_are_not_working():
    c = _cycle(pro=True)
    msi = [k for k in c["targets"] if k[2] == "msisupported"]
    assert any("usb0" in k[1] for k in msi) and not any("usb1" in k[1] for k in msi)


def test_hibernation_is_only_switched_back_on_if_this_app_switched_it_off():
    m.fresh()
    m.HIBERNATE[0] = False                          # the user turned it off themselves (dual boot)
    key = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    m.rset(key, "HiberBootEnabled", 0)              # ...and Fast Startup
    OPT.run_full_optimize(["boot"])
    OPT.reset_all_optimizations()
    assert m.HIBERNATE[0] is False and not m.ran("powercfg", "-h")
    assert m.rdata(key, "HiberBootEnabled") == 0


# ═════════════════════════════════════════════════════════════════════
# Machines the optimizer never ran on, or ran on before records existed
# ═════════════════════════════════════════════════════════════════════
def test_reset_on_an_untouched_machine_changes_no_system_state():
    m.fresh()
    before = m.snap_system()
    m.clear_logs()
    OPT.reset_all_optimizations()
    after = m.snap_system()
    for part in ("services", "tasks", "bcd", "tcp", "stack", "features", "fsutil", "defender", "hibernate", "plan"):
        assert after[part] == before[part], part
    assert not m.ran("sc", "config") and not m.ran("bcdedit", "/set") and not m.ran("bcdedit", "/deletevalue")
    assert not m.ran("dism", "/enable-feature") and not m.ran("schtasks", "/change")
    assert not [c for c in m.CMDS if c[0] == "netsh" and "reset" in c]
    assert not m.ran("powercfg", "setactive") and not m.PTOUCH, "the user's plan, and every setting on it"


# ── the registry ──────────────────────────────────────────────────────
# key, value, a setting of the user's own, what an older build's tweak left
# there, and what Reset All makes of THAT (None: the value is removed)
_PROFILE = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
_POLICIES = r"HKLM\SOFTWARE\Policies\Microsoft"
OWN_AND_OLD = [
    (r"HKCU\Control Panel\Desktop", "ForegroundLockTimeout", 100000, 0, 200000),
    (r"HKCU\Control Panel\Desktop", "MenuShowDelay", "200", "0", "400"),
    (r"HKCU\System\GameConfigStore", "GameDVR_FSEBehaviorMode", 1, 2, 0),
    (r"HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", 0x18, 40, 2),
    (_PROFILE, "NetworkThrottlingIndex", 20, 0xFFFFFFFF, 10),
    (_PROFILE, "SystemResponsiveness", 30, 0, 20),
    (_PROFILE + r"\Tasks\DisplayPostProcessing", "Priority", 4, 2, 8),
    (r"HKLM\SYSTEM\CurrentControlSet\Services\Ndu", "Start", 3, 4, 2),
    (NV_KEY, "PowerMizerLevel", 3, 1, 0),
    (_POLICIES + r"\Edge", "StartupBoostEnabled", 1, 0, None),
    (_POLICIES + r"\Windows\WindowsUpdate", "DeferFeatureUpdatesPeriodInDays", 30, 365, None),
    (_POLICIES + r"\Windows\AppPrivacy", "LetAppsAccessNotifications", 1, 2, None),
    (_POLICIES + r"\Windows\GameDVR", "AllowGameDVR", 1, 0, None),
    (r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "TdrDelay", 8, 10, None),
    (r"HKLM\SOFTWARE\Microsoft\Windows\Dwm", "OverlayTestMode", 1, 5, None),
]
# ...and settings Reset All used to overwrite that no tweak is known to have
# written.  There is no fallback for these at all now.
NOT_OURS = [
    (r"HKCU\Control Panel\Desktop", "FontSmoothing", "0"),              # ClearType off
    (r"HKCU\Control Panel\Desktop", "DragFullWindows", "0"),
    (r"HKCU\System\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1),
    (r"HKCU\Software\Microsoft\Windows\DWM", "Composition", 0),
    (_POLICIES + r"\Edge", "PasswordManagerEnabled", 0),
    (_POLICIES + r"\Windows\WindowsUpdate\AU", "NoAutoUpdate", 1),
    (_POLICIES + r"\Windows\Personalization", "LockScreenImage", r"C:\Company\lock.jpg"),
    (m.DISPLAY_CLASS + r"\0000", "PowerMizerLevel", 1),                 # not the NVIDIA adapter's key
    (r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\54533251-82be-4824-96c1-47b60b740d00"
     r"\0cc5b647-c1df-4637-891a-dec35c318583", "ValueMax", 100),        # ships with Windows
]


def test_a_users_own_settings_survive_reset_all_on_an_untouched_machine():
    m.fresh()
    _nvidia_adapter()
    for key, value, own, _, _ in OWN_AND_OLD:
        m.rset(key, value, own)
    for key, value, own in NOT_OURS:
        m.rset(key, value, own)
    before = m.flat()
    OPT.reset_all_optimizations()
    assert _diff(before, m.flat()) == {}


def test_the_same_settings_left_by_an_older_build_are_still_reset():
    m.fresh()
    _nvidia_adapter()
    for key, value, _, old, _ in OWN_AND_OLD:
        m.rset(key, value, old)                     # no record: that build kept none
    for key, value, own in NOT_OURS:
        m.rset(key, value, own)
    OPT.reset_all_optimizations()
    got = {(key, value): m.rdata(key, value) for key, value, _, _, _ in OWN_AND_OLD}
    assert got == {(key, value): after for key, value, _, _, after in OWN_AND_OLD}
    assert all(m.rdata(key, value) == own for key, value, own in NOT_OURS), "still nobody's business"


def test_no_setting_of_the_users_is_overwritten_whatever_reset_has_a_default_for():
    """The same, for every single fallback rather than a hand-picked few."""
    fallbacks = _fallbacks()
    assert len(fallbacks) > 120
    for pro in (False, True):
        m.fresh()
        OPT.set_pro_mode(pro)
        _nvidia_adapter()
        _seed_once_overwritten()
        for key, value, tweaks, _, vtype in fallbacks:
            m.rset(key, value, user_value(vtype, tweaks[0]), vtype)
        before, system = m.flat(), m.snap_system()
        m.clear_logs()
        OPT.reset_all_optimizations()
        assert _diff(before, m.flat()) == {}, f"Pro Mode {pro}"
        assert not m.WRITES and not m.DELETES, "not one `reg add` or `reg delete`"
        assert m.snap_system() == system and not m.PTOUCH
        assert not m.ps_ran("SystemParametersInfo"), "no visual tweak to undo: the live session is left alone too"


def test_every_fallback_still_undoes_what_an_older_build_left():
    fallbacks = _fallbacks()
    for pick in (0, -1):                            # where builds wrote different values: the first, then the last
        m.fresh()
        OPT.set_pro_mode(True)
        _nvidia_adapter()
        for key, value, tweaks, _, vtype in fallbacks:
            m.rset(key, value, m._reg_parse(vtype, tweaks[pick]), vtype)
        OPT.reset_all_optimizations()
        wrong = [(key, value, m.rget(key, value)) for key, value, _, stock, vtype in fallbacks
                 if (m.rget(key, value) is not None if stock is None else not RO.reads_as(key, value, stock, vtype))]
        assert wrong == [], "back to the Windows default - or gone, where Windows has no such value"
        assert m.ps_ran("SystemParametersInfo"), "the visual tweak WAS there to undo"


def test_a_value_on_another_tabs_record_is_left_to_that_tab():
    m.fresh()
    m.rset(_PROFILE, "NetworkThrottlingIndex", 20)
    RO.remember(_PROFILE, "NetworkThrottlingIndex", "4294967295", scope="network")
    m.rset(_PROFILE, "NetworkThrottlingIndex", 0xFFFFFFFF)       # the Network tab's tweak, on its own record
    OPT.reset_all_optimizations()
    assert m.rdata(_PROFILE, "NetworkThrottlingIndex") == 0xFFFFFFFF, "its undo has the real value; 10 is a guess"
    assert RO.has_original(_PROFILE, "NetworkThrottlingIndex", scope="network")
    assert RO.on_record(_PROFILE.upper(), "networkthrottlingindex"), "however the key is spelled"


def test_spectre_override_goes_as_a_pair_or_not_at_all():
    base = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
    m.fresh()
    m.rset(base, "FeatureSettingsOverride", 0)      # mitigations switched ON by hand: 0 with a mask of 3
    m.rset(base, "FeatureSettingsOverrideMask", 3)
    OPT.reset_all_optimizations()
    assert (m.rdata(base, "FeatureSettingsOverride"), m.rdata(base, "FeatureSettingsOverrideMask")) == (0, 3)
    m.rset(base, "FeatureSettingsOverride", 3)      # the tweak, from a build that kept no record
    OPT.reset_all_optimizations()
    assert m.rget(base, "FeatureSettingsOverride") is None and m.rget(base, "FeatureSettingsOverrideMask") is None


def test_pro_mode_reset_does_not_create_values_that_were_never_there():
    m.fresh()
    OPT.set_pro_mode(True)
    hvci = r"HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity"
    power = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    OPT.reset_all_optimizations()
    assert m.rget(hvci, "Enabled") is None, "this switched Memory Integrity on for people who never had it"
    assert m.rget(power, "HiberBootEnabled") is None
    m.rset(hvci, "Enabled", 0)                      # as the Pro Mode tweak leaves them
    m.rset(power, "HiberBootEnabled", 0)
    OPT.reset_all_optimizations()
    assert m.rdata(hvci, "Enabled") == 1 and m.rdata(power, "HiberBootEnabled") == 1


def test_gamedvr_policy_key_keeps_what_is_not_the_tweaks():
    key = _POLICIES + r"\Windows\GameDVR"
    m.fresh()
    m.rset(key, "AllowGameDVR", 0)                  # the tweak, no record
    m.rset(key, "SomebodyElses", 1)
    OPT.reset_all_optimizations()
    assert m.rget(key, "AllowGameDVR") is None and m.rdata(key, "SomebodyElses") == 1, \
        "the whole key used to be deleted"


def test_context_menu_override_is_only_removed_when_it_is_exactly_the_tweaks():
    ctx = r"HKCU\Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"
    inproc = ctx + r"\InprocServer32"
    m.fresh()
    m.rset(inproc, "", "")                          # the tweak: an empty default value, nothing else
    OPT.reset_all_optimizations()
    assert not m.key_exists(ctx)
    for more in ((inproc, "ThreadingModel", "Apartment"), (ctx, "", "mine"), (ctx + r"\Other", "x", 1)):
        m.fresh()
        m.rset(inproc, "", "")
        m.rset(*more)                               # ...and something that is not the tweak's
        before = m.flat()
        OPT.reset_all_optimizations()
        assert m.flat() == before, more
    m.fresh()
    m.rset(inproc, "", r"C:\Tools\menu.dll")        # somebody's own handler
    OPT.reset_all_optimizations()
    assert m.rdata(inproc, "") == r"C:\Tools\menu.dll"


def test_visual_effects_are_only_switched_live_when_the_tweak_is_being_undone():
    m.fresh()
    OPT.run_full_optimize(["visual"])
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert m.ps_ran("SystemParametersInfo"), "on record: the tweak is being undone"
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert not m.ps_ran("SystemParametersInfo"), "nothing left to undo"


def test_defender_bits_are_only_reset_from_what_the_tweak_sets():
    m.fresh()
    m.DEF.update(SubmitSamplesConsent=0, MAPSReporting=1)       # "always prompt", "basic": this user's own
    OPT.reset_all_optimizations()
    assert (m.DEF["SubmitSamplesConsent"], m.DEF["MAPSReporting"]) == (0, 1), "these became 1 and 2 at every reset"
    m.DEF.update(SubmitSamplesConsent=2, MAPSReporting=0)       # as an older build's tweak left them
    OPT.reset_all_optimizations()
    assert (m.DEF["SubmitSamplesConsent"], m.DEF["MAPSReporting"]) == (1, 2)
    m.DEF_OK[0] = False                                         # no Defender to ask: nothing is set blindly
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert not m.ps_ran("Set-MpPreference")


def test_reset_undoes_what_an_older_build_left_without_records():
    m.fresh()
    for name, mode in (("Spooler", "demand"), ("WerSvc", "disabled"), ("DiagTrack", "demand"),
                       ("edgeupdate", "demand"), ("WSearch", "disabled"), ("lfsvc", "disabled"),
                       ("dmwappushservice", "disabled")):
        m.svc_set(name, mode)
    m.BCD.update({"bootmenupolicy": "Legacy", "quietboot": "Yes", "useplatformclock": "No",
                  "disabledynamictick": "Yes", "recoveryenabled": "No", "bootlog": "No"})
    m.TCP.update({"initialrto": "2000", "maxsynretransmissions": "2", "hystart": "disabled"})
    m.FSUTIL["disablelastaccess"] = 1
    OPT.reset_all_optimizations()
    got = {n: m.svc_get(n) for n in ("Spooler", "WerSvc", "DiagTrack", "edgeupdate", "WSearch", "lfsvc",
                                     "dmwappushservice")}
    assert got == {"Spooler": "auto", "WerSvc": "demand", "DiagTrack": "auto", "edgeupdate": "delayed-auto",
                   "WSearch": "delayed-auto", "lfsvc": "demand", "dmwappushservice": "demand"}, \
        "the start types Windows ships them with"
    assert m.bcd_get("bootmenupolicy") == "Standard" and m.bcd_get("recoveryenabled") == "Yes", \
        "set back, not deleted: without recoveryenabled Windows no longer falls back to recovery"
    assert all(m.bcd_get(n) is None for n in ("quietboot", "useplatformclock", "disabledynamictick", "bootlog"))
    assert m.TCP == m.TCP_DEFAULTS and len([c for c in m.CMDS if c[:4] == ["netsh", "int", "tcp", "reset"]]) == 1
    assert m.FSUTIL["disablelastaccess"] == 2, "2 is the default; this used to write 3"
    assert m.svc_get(m.USER_SERVICE_OFF) == "disabled"


def test_the_fixed_defaults_leave_other_peoples_settings_alone():
    """Each default only applies while the setting still reads the way the
    tweak leaves it.  Anything else is somebody else's choice."""
    m.fresh()
    m.svc_set("Spooler", "disabled")                # the tweak leaves it on Manual; this is the user's
    m.svc_set("edgeupdate", "disabled")
    m.BCD.update({"bootmenupolicy": "Standard", "useplatformclock": "Yes", "bootlog": "Yes"})
    m.FSUTIL["disablelastaccess"] = 3
    m.TCP["initialrto"] = "3000"
    OPT.reset_all_optimizations()
    assert m.svc_get("Spooler") == "disabled" and m.svc_get("edgeupdate") == "disabled"
    assert m.bcd_get("useplatformclock") == "Yes" and m.bcd_get("bootlog") == "Yes"
    assert m.FSUTIL["disablelastaccess"] == 3 and m.TCP["initialrto"] == "3000"


# ═════════════════════════════════════════════════════════════════════
# Power plan baseline
# ═════════════════════════════════════════════════════════════════════
def test_the_users_plan_is_captured_before_it_is_left():
    m.fresh(plan=m.USERPLAN)
    assert OPT._capture_power_baseline() == m.USERPLAN
    m.run_cmd(["powercfg", "/setactive", m.BALANCED])
    assert OPT._capture_power_baseline() == m.USERPLAN, "captured once; later changes do not replace it"


def test_one_of_our_own_plans_is_never_taken_for_the_users():
    m.fresh(plan=m.HIGH_PERF)                       # the start-up prep already moved to High Performance
    assert OPT._capture_power_baseline() == ""
    assert not os.path.exists(OPT._POWER_BASELINE_PATH)
    OPT.atomic_write_json(OPT._POWER_BASELINE_PATH, {"guid": m.HIGH_PERF})     # written by an older build
    assert OPT._read_power_baseline() == ""
    assert OPT.restore_power_baseline()["ok"] is False


def test_ultimate_plan_is_made_once_and_reused():
    m.fresh()
    first = OPT.apply_ultimate_power_plan()["guid"]
    m.run_cmd(["powercfg", "/setactive", m.BALANCED])
    second = OPT.apply_ultimate_power_plan()["guid"]
    assert first == second == m.PLAN["active"]
    assert len(m.ran("powercfg", "-duplicatescheme")) == 1, "it used to add a new copy at every game start"
    m.PLAN["schemes"].discard(first)                # the user deleted the copy
    m.PLAN["active"] = m.BALANCED
    third = OPT.apply_ultimate_power_plan()["guid"]
    assert third != first and m.PLAN["active"] == third


def test_no_ultimate_template_falls_back_to_high_performance():
    m.fresh()
    m.DUPLICATE_OK[0] = False
    assert OPT.apply_ultimate_power_plan()["guid"] == "high-perf-fallback"
    assert m.PLAN["active"] == m.HIGH_PERF
    OPT.reset_all_optimizations()
    assert m.PLAN["active"] == m.USERPLAN


def test_reset_falls_back_to_balanced_when_the_recorded_plan_is_gone():
    m.fresh()
    OPT.apply_ultimate_power_plan()
    m.PLAN["schemes"].discard(m.USERPLAN)           # the user deleted their old plan since
    rows = OPT._reset_power_plan()
    assert m.PLAN["active"] == m.BALANCED and "fallback" in rows[0]["name"]


def test_without_a_baseline_the_users_own_plan_stays_active():
    m.fresh()                                       # on their own plan; this app never switched it
    rows = OPT._reset_power_plan()
    assert m.PLAN["active"] == m.USERPLAN and not m.ran("powercfg", "setactive"), \
        "this used to move everybody without a baseline to Balanced"
    assert rows[0]["ok"] and "left as it is" in rows[0]["name"]


def test_without_a_baseline_one_of_our_own_plans_is_still_left_for_balanced():
    m.fresh(plan=m.HIGH_PERF)                       # where an older build's start-up prep left the machine
    rows = OPT._reset_power_plan()
    assert m.PLAN["active"] == m.BALANCED and "fallback" in rows[0]["name"]


def test_cpu_idle_off_on_the_users_own_plan_survives_optimize_and_reset():
    m.fresh()
    m.cell(m.USERPLAN, m.IDLE)[:] = [1, 0]          # this user runs with CPU idle off, and means to
    OPT.run_full_optimize()
    performance = m.PLAN["active"]
    assert performance != m.USERPLAN and m.cell(performance, m.IDLE)[0] == 1, "the tweak landed on OUR plan"
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert m.PLAN["active"] == m.USERPLAN
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0], "idle was forced back on for whichever plan the reset ended on"
    assert m.cell(performance, m.IDLE) == [0, 0], "the plan that WAS changed is the one put back"
    OPT.reset_all_optimizations()                   # and again, with nothing on record any more
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0]
    assert not [t for t in m.PTOUCH if t[0] == m.USERPLAN], "no setting on the user's plan was written at all"


def test_cpu_idle_left_off_by_an_older_build_is_switched_back_on_for_our_own_plans_only():
    m.fresh(plan=m.HIGH_PERF)
    m.cell(m.HIGH_PERF, m.IDLE)[:] = [1, 0]         # the Kernel page of a build that kept no record
    m.cell(m.USERPLAN, m.IDLE)[:] = [1, 0]          # the user's own doing
    m.cell(m.BALANCED, m.IDLE)[:] = [1, 0]          # ...this too
    OPT.reset_all_optimizations()
    assert m.cell(m.HIGH_PERF, m.IDLE) == [0, 0]
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0] and m.cell(m.BALANCED, m.IDLE) == [1, 0]
    m.fresh(plan=m.HIGH_PERF)
    m.cell(m.HIGH_PERF, m.IDLE)[:] = [1, 0]
    RO.remember_power(m.PROC_SUB, m.IDLE, scope="gamemode")     # a running game's setting, on its own record
    m.clear_logs()
    OPT.reset_all_optimizations()
    assert m.cell(m.HIGH_PERF, m.IDLE) == [1, 0] and not m.PTOUCH, "game mode puts that back itself"


def test_advanced_power_tweaks_are_undone_on_the_users_own_plan():
    m.fresh()
    m.cell(m.USERPLAN, m.IDLE)[:] = [1, 0]          # this user already runs with CPU idle off
    m.cell(m.USERPLAN, m.PROC_MAX)[:] = [90, 80]
    OPT.run_full_optimize(["power_advanced"])       # no plan switch: the tweaks land on the user's plan
    assert RO.summary()["power"] == 15
    assert m.cell(m.USERPLAN, m.PROC_MAX) == [100, 80] and m.cell(m.USERPLAN, m.BOOST)[0] == 2
    assert not m.ran("powercfg", "/query"), "hidden settings can only be read with /qh"
    OPT.reset_all_optimizations()
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0], "their own idle setting, not 'idle on'"
    assert m.cell(m.USERPLAN, m.PROC_MAX) == [90, 80] and m.cell(m.USERPLAN, m.BOOST) == [5, 6]
    assert RO.summary()["power"] == 0


# ═════════════════════════════════════════════════════════════════════
# Single categories
# ═════════════════════════════════════════════════════════════════════
def test_legacy_category_respects_hardware_and_tools_in_use():
    m.fresh()
    m.COUNTS.update(smartcard=1, bluetooth=2)       # a card reader and a Bluetooth keyboard
    m.WSL_DISTROS[0] = "Ubuntu"
    rows = OPT.run_full_optimize(["legacy"])["legacy"]
    names = " | ".join(r["name"] for r in rows)
    assert "Smart card services kept" in names and "Bluetooth service kept" in names
    assert "Virtualisation features kept" in names
    assert m.svc_get("SCardSvr") == "demand" and not RO.has_state("service", "SCardSvr")
    assert m.FEATURES["VirtualMachinePlatform"] is True and m.FEATURES["Containers-DisposableClientVM"] is True
    assert m.FEATURES["WindowsMediaPlayer"] is False
    assert "Disabled 4 optional Windows feature(s) (of 7 checked" in names


def test_defender_category_without_defender_records_nothing():
    m.fresh()
    m.DEF_OK[0] = False
    OPT.run_full_optimize(["defender"])
    assert RO.summary()["state"] == 0
    OPT.reset_all_optimizations()                   # and the reset has nothing to remove


def test_defender_exclusions_from_before_records_existed_are_kept():
    m.fresh()
    m.DEF["ExclusionPath"].append(r"C:\XboxGames")  # added by an older build: cannot be told from the user's
    OPT.reset_all_optimizations()
    assert r"C:\XboxGames" in m.DEF["ExclusionPath"]


def test_network_stack_category_writes_every_interface_and_no_powershell_registry():
    m.fresh()
    OPT.run_full_optimize(["network_stack"])
    for guid in m.IFACES:
        assert m.rdata(rf"{m.IFACES_KEY}\{guid}", "TcpAckFrequency") == 1
        assert RO.has_original(rf"{m.IFACES_KEY}\{guid}", "TCPNoDelay")
    assert not m.ps_ran("Set-ItemProperty"), "PowerShell must not write the registry behind the recorder's back"
    OPT.reset_all_optimizations()
    assert m.rget(rf"{m.IFACES_KEY}\{m.IFACES[0]}", "TcpAckFrequency") is None


def test_msi_mode_writes_go_through_the_recorder():
    m.fresh()
    assert OPT._enable_msi_mode("Net") == 1
    dev = m.PNP["Net"][0]["InstanceId"]
    key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{dev}\Device Parameters\Interrupt Management" \
          r"\MessageSignaledInterruptProperties"
    assert m.rdata(key, "MSISupported") == 1 and RO.has_original(key, "MSISupported")
    m.PNP["Net"] = [{"InstanceId": 'PCI\\X" | calc'}, {"InstanceId": "no-backslash"}]
    assert OPT._enable_msi_mode("Net") == 0, "an instance id that could not be a device path is skipped"


def test_pro_mode_setting_persists():
    m.fresh()
    assert OPT.get_pro_mode() == {"enabled": False}
    assert OPT.set_pro_mode(True)["ok"] and OPT.get_pro_mode() == {"enabled": True}


def test_reset_all_repairs_the_capped_core_parking_limit():
    """An old "unpark cores" tweak set the limit of the E-core parking setting
    to 0, so Windows refuses any other value for it - game mode included."""
    key = (r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings\54533251-82be-4824-96c1-47b60b740d00"
           r"\0cc5b647-c1df-4637-891a-dec35c318583")
    m.fresh()
    m.rset(key, "ValueMax", 0)
    OPT.reset_all_optimizations()
    assert m.rdata(key, "ValueMax") == 100, "back to what Windows ships"
    m.fresh()
    m.rset(key, "ValueMax", 50)                     # anything else is somebody's own choice
    OPT.reset_all_optimizations()
    assert m.rdata(key, "ValueMax") == 50
