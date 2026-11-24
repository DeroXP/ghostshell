import sandbox as m

# core/gpu_events.py against the sandbox's fake event log (m.EVENTS): what
# counts as which kind of event, read from records shaped like the ones Windows
# writes; the time bounds; order and duplicates; driver error bursts and their
# cache; and what happens when the log cannot be read.  No test reads the real
# event log: the module's adapter is the fake from the moment the sandbox is
# imported.
#
# Where the XML comes from.  The records marked "real" were copied from a
# Windows 11 PC (build 26300, November 2026) with the computer name, the
# program and its paths replaced.  That PC has Windows Error Reporting switched
# off and no second graphics driver, so it holds no bugcheck 1001, no
# LiveKernelEvent, no Application Hang and no Display 4101 record: those are
# marked "from documentation" and follow the layouts Microsoft documents.

import ast
import calendar
import ctypes
import os
import threading
import time

from core import gpu_events as ge

EVENT_KEYS = {"ts", "kind", "provider", "event_id", "record_id", "channel", "detail", "exe", "module", "pid", "code"}
T0 = 1793571253.6730008             # 2026-11-01T22:14:13.6730008Z, when NV_153 was written
DAY = 86400.0
LRM = chr(0x200E)                   # the left-to-right mark Windows puts around the parts of a date
EM_DASH = chr(0x2014)
BANNED = ("offset", "tdr", "blacklist", "baseline", "yielded", "stock", "strike")

HEAD = "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"

# ── real: the graphics driver's error, as current drivers write it (no message, two strings) ──
NV_153 = HEAD + (
    r"<Provider Name='nvlddmkm'/><EventID Qualifiers='0'>153</EventID><Version>0</Version><Level>2</Level>"
    r"<Task>0</Task><Opcode>0</Opcode><Keywords>0x80000000000000</Keywords>"
    r"<TimeCreated SystemTime='2026-11-01T22:14:13.6730008Z'/><EventRecordID>100713</EventRecordID><Correlation/>"
    r"<Execution ProcessID='4' ThreadID='34888'/><Channel>System</Channel><Computer>DESKTOP-TEST</Computer>"
    r"<Security/></System><EventData><Data>\Device\Video3</Data><Data>Error occurred on GPUID: 100</Data>"
    r"<Binary>00000000020030000000000099000000000000000000000000000000000000000000000000000000</Binary>"
    r"</EventData></Event>")

# ── real: a program crash (this Windows build names every value) ──
APP_1000 = HEAD + (
    r"<Provider Name='Application Error' Guid='{a0e9b465-b939-57d7-b27d-95d8e925ff57}'/><EventID>1000</EventID>"
    r"<Version>0</Version><Level>2</Level><Task>100</Task><Opcode>0</Opcode><Keywords>0x8000000000000000</Keywords>"
    r"<TimeCreated SystemTime='2026-11-11T08:09:15.6527847Z'/><EventRecordID>86807</EventRecordID><Correlation/>"
    r"<Execution ProcessID='4328' ThreadID='23956'/><Channel>Application</Channel><Computer>DESKTOP-TEST</Computer>"
    r"<Security UserID='S-1-5-18'/></System><EventData><Data Name='AppName'>TestGame-Win64-Shipping.exe</Data>"
    r"<Data Name='AppVersion'>2.2.1.792</Data><Data Name='AppTimeStamp'>6a630000</Data>"
    r"<Data Name='ModuleName'>KERNELBASE.dll</Data><Data Name='ModuleVersion'>10.0.26100.9711</Data>"
    r"<Data Name='ModuleTimeStamp'>fb9071fb</Data><Data Name='ExceptionCode'>c0000005</Data>"
    r"<Data Name='FaultingOffset'>00000000000c408a</Data><Data Name='ProcessId'>0x5d30</Data>"
    r"<Data Name='ProcessCreationTime'>0x1dd716f6a550f16</Data>"
    r"<Data Name='AppPath'>C:\Games\TestGame\TestGame-Win64-Shipping.exe</Data>"
    r"<Data Name='ModulePath'>C:\WINDOWS\System32\KERNELBASE.dll</Data>"
    r"<Data Name='IntegratorReportId'>7665ee08-81f6-44f3-b5ee-50b2f660fd65</Data>"
    r"<Data Name='PackageFullName'></Data><Data Name='PackageRelativeAppId'></Data></EventData></Event>")
APP_1000_TS = calendar.timegm((2026, 11, 11, 8, 9, 15)) + 0.6527847

# ── real: the PC lost power or was reset (written at the next start; BugcheckCode is decimal) ──
KP_41 = HEAD + (
    r"<Provider Name='Microsoft-Windows-Kernel-Power' Guid='{331c3b3a-2005-44c2-ac5e-77220c37d6b4}'/>"
    r"<EventID>41</EventID><Version>10</Version><Level>1</Level><Task>63</Task><Opcode>0</Opcode>"
    r"<Keywords>0x8000400000000002</Keywords><TimeCreated SystemTime='2026-10-25T23:04:23.6168169Z'/>"
    r"<EventRecordID>96740</EventRecordID><Correlation/><Execution ProcessID='4' ThreadID='8'/>"
    r"<Channel>System</Channel><Computer>DESKTOP-TEST</Computer><Security UserID='S-1-5-18'/></System><EventData>"
    r"<Data Name='BugcheckCode'>0</Data><Data Name='BugcheckParameter1'>0x0</Data>"
    r"<Data Name='BugcheckParameter2'>0x0</Data><Data Name='BugcheckParameter3'>0x0</Data>"
    r"<Data Name='BugcheckParameter4'>0x0</Data><Data Name='SleepInProgress'>0</Data>"
    r"<Data Name='PowerButtonTimestamp'>0</Data><Data Name='BootAppStatus'>0</Data><Data Name='Checkpoint'>0</Data>"
    r"<Data Name='ConnectedStandbyInProgress'>false</Data><Data Name='SystemSleepTransitionsToOn'>0</Data>"
    r"<Data Name='CsEntryScenarioInstanceId'>0</Data><Data Name='BugcheckInfoFromEFI'>false</Data>"
    r"<Data Name='CheckpointStatus'>0</Data><Data Name='CsEntryScenarioInstanceIdV2'>0</Data>"
    r"<Data Name='LongPowerButtonPressDetected'>false</Data><Data Name='LidReliability'>false</Data>"
    r"<Data Name='InputSuppressionState'>0</Data><Data Name='PowerButtonSuppressionState'>0</Data>"
    r"<Data Name='LidState'>3</Data><Data Name='WHEABootErrorCount'>0</Data></EventData></Event>")
KP_41_TS = calendar.timegm((2026, 10, 25, 23, 4, 23)) + 0.6168169

# ── real: "the previous shutdown was unexpected" (a local time and date, the date with direction marks) ──
EL_6008 = HEAD + (
    r"<Provider Name='EventLog'/><EventID Qualifiers='32768'>6008</EventID><Version>0</Version><Level>2</Level>"
    r"<Task>0</Task><Opcode>0</Opcode><Keywords>0x80000000000000</Keywords>"
    r"<TimeCreated SystemTime='2026-10-25T23:04:33.8678853Z'/><EventRecordID>96728</EventRecordID><Correlation/>"
    r"<Execution ProcessID='2344' ThreadID='3356'/><Channel>System</Channel><Computer>DESKTOP-TEST</Computer>"
    r"<Security/></System><EventData><Data>5:48:57 PM</Data><Data>{m}10/{m}25/{m}2026</Data><Data></Data>"
    r"<Data></Data><Data>18012</Data><Data></Data><Data></Data>"
    r"<Binary>EA070A00000019001100300039001603EA070A00000019001600300039001603DC0500003C00000001000000DC0500000100"
    r"00001E0000000000000000000000</Binary></EventData></Event>").replace("{m}", LRM)
EL_6008_TS = calendar.timegm((2026, 10, 25, 23, 4, 33)) + 0.8678853

# ── real: the event log service started, which is how a start of Windows shows up ──
EL_6005 = HEAD + (
    r"<Provider Name='EventLog'/><EventID Qualifiers='32768'>6005</EventID><Version>0</Version><Level>4</Level>"
    r"<Task>0</Task><Opcode>0</Opcode><Keywords>0x80000000000000</Keywords>"
    r"<TimeCreated SystemTime='2026-11-13T19:20:38.9463672Z'/><EventRecordID>108464</EventRecordID><Correlation/>"
    r"<Execution ProcessID='2376' ThreadID='3332'/><Channel>System</Channel><Computer>DESKTOP-TEST</Computer>"
    r"<Security/></System><EventData><Binary>EA070B0005000D00130014002600A6030000000000000000</Binary>"
    r"</EventData></Event>")
EL_6005_TS = calendar.timegm((2026, 11, 13, 19, 20, 38)) + 0.9463672


def _record(provider, event_id, when, record_id, channel, data, level=2):
    """A record with the System block in the order Windows writes it.  `provider`
    and `event_id` are the inside of their tags."""
    return HEAD + (
        "<Provider %s/><EventID%s</EventID><Version>0</Version><Level>%d</Level><Task>0</Task><Opcode>0</Opcode>"
        "<Keywords>0x80000000000000</Keywords><TimeCreated SystemTime='%s'/><EventRecordID>%d</EventRecordID>"
        "<Correlation/><Execution ProcessID='0' ThreadID='0'/><Channel>%s</Channel><Computer>DESKTOP-TEST</Computer>"
        "<Security/></System><EventData>%s</EventData></Event>"
        % (provider, event_id, level, when, record_id, channel, data))


# ── from documentation: "Display driver nvlddmkm stopped responding and has successfully recovered." ──
def _display_4101(driver="nvlddmkm", record_id=100714):
    return _record("Name='Display'", " Qualifiers='0'>4101", "2026-11-01T22:14:14.1000000Z", record_id, "System",
                   "<Data>%s</Data><Data></Data>" % driver, level=3)


# ── from documentation: "The computer has rebooted from a bugcheck. The bugcheck was: 0x00000116 (...)" ──
def _bugcheck_1001(first="0x00000116 (0xffffb38f5c2d6010, 0xfffff8056b9a4c30, 0xffffffffc000009a, "
                         "0x0000000000000004)", record_id=100800):
    return _record("Name='Microsoft-Windows-WER-SystemErrorReporting' EventSourceName='BugCheck'",
                   " Qualifiers='16384'>1001", "2026-11-01T22:20:00.5000000Z", record_id, "System",
                   r"<Data Name='param1'>%s</Data><Data Name='param2'>C:\WINDOWS\MEMORY.DMP</Data>"
                   r"<Data Name='param3'>5f3b0d1c-6a52-4d0e-9c1b-0e6f2a7d9b11</Data>" % first)


# ── from documentation: a live kernel report.  Value 2 is the event name, value 5 is P1 (hex, no prefix). ──
def _live_kernel_1001(p1="141", name="LiveKernelEvent", record_id=86900):
    values = ("", "0", name, "Not available", "0", p1, "ffffb38f5c2d6010", "fffff8056b9a4c30", "0", "3f04",
              "10_0_26100", "0_0", "768_1", "", "",
              r"\\?\C:\WINDOWS\LiveKernelReports\WATCHDOG\WATCHDOG-20261101-2214.dmp", "",
              r"\\?\C:\ProgramData\Microsoft\Windows\WER\ReportArchive\Kernel_141_1", "", "0",
              "9a1f6c0e-2b7d-4c55-8f0a-6d3e1b2c4a59", "262144", "", "0")
    return _record("Name='Windows Error Reporting'", " Qualifiers='0'>1001", "2026-11-01T22:14:20.2500000Z",
                   record_id, "Application", "".join("<Data>%s</Data>" % v for v in values), level=4)


# ── from documentation: a program that stopped responding (values without names: name, version,
#    process id in hex, start time, termination time, path, report id) ──
HANG_1002 = _record(
    "Name='Application Hang'", " Qualifiers='0'>1002", "2026-11-01T22:15:00.0000000Z", 86901, "Application",
    r"<Data>TestGame.exe</Data><Data>1.0.0.0</Data><Data>2f44</Data><Data>01dd716f6a550f16</Data>"
    r"<Data>4294967295</Data><Data>C:\Games\TestGame\TestGame.exe</Data>"
    r"<Data>0d2e7a4c-91d2-4c0b-8a37-3b5a7c1e9f20</Data><Data></Data><Data></Data>"
    r"<Data>Top level window is idle</Data><Binary>55006E006B006E006F0077006E0000000000</Binary>")

# ── from documentation: the same crash record as APP_1000 on Windows 10, where the values have no names ──
APP_1000_OLD = _record(
    "Name='Application Error'", " Qualifiers='0'>1000", "2026-11-01T22:16:00.0000000Z", 86902, "Application",
    r"<Data>TestGame.exe</Data><Data>1.0.0.0</Data><Data>5e8f2a11</Data><Data>nvwgf2umx.dll</Data>"
    r"<Data>27.21.14.5671</Data><Data>5f8e2c3b</Data><Data>c0000409</Data><Data>00000000001a2b3c</Data>"
    r"<Data>2f44</Data><Data>01d6a3b4c5d6e7f8</Data><Data>C:\Games\TestGame\TestGame.exe</Data>"
    r"<Data>C:\WINDOWS\System32\DriverStore\FileRepository\nv_dispi.inf_amd64_0123456789abcdef\nvwgf2umx.dll</Data>"
    r"<Data>0b1c2d3e-4f5a-6b7c-8d9e-0f1a2b3c4d5e</Data><Data></Data><Data></Data>")

# The 19 graphics driver errors of the real log (24 Sep to 1 Nov 2026), as seconds after the first one.
REAL_ERRORS = (0.0, 234.546, 272.207, 288.861, 299.119, 547685.14, 548352.85, 548421.325, 548490.631,
               548954.628, 548960.544, 550087.048, 550132.631, 615792.731, 886113.853, 1482806.627,
               2740472.265, 2846685.073, 3359884.419)


def _tidy():
    """The code under test closed every handle it opened and misused none."""
    assert m.EVENTS.open_handles == 0, f"{m.EVENTS.open_handles} handles left open"
    assert m.EVENTS.bad == [], m.EVENTS.bad


def _events(since=0.0, until=T0 + 400 * DAY, kinds=None):
    r = ge.query(since, until, kinds)
    assert r["ok"] is True and r["err"] == "", r
    assert set(r) == {"ok", "events", "err"}
    _tidy()
    return r["events"]


def _one(since=0.0, until=T0 + 400 * DAY, kinds=None):
    events = _events(since, until, kinds)
    assert len(events) == 1, events
    return events[0]


def _retime(xml, old, new, record_id=None):
    """The same record at another time (and under another record id)."""
    assert old in xml
    xml = xml.replace(old, new)
    if record_id is not None:
        start = xml.index("<EventRecordID>") + len("<EventRecordID>")
        xml = xml[:start] + str(record_id) + xml[xml.index("</EventRecordID>"):]
    return xml


def _nv(ts, record_id=None):
    return m.EVENTS.add("driver_error", ts, record_id=record_id)


# ═════════════════════════════════════════════════════════════════════
# What counts as what
# ═════════════════════════════════════════════════════════════════════
def test_the_real_driver_error_record_is_a_driver_error():
    m.fresh()
    m.EVENTS.add_xml("System", NV_153)
    e = _one()
    assert set(e) == EVENT_KEYS
    assert e["kind"] == "driver_error" and e["provider"] == "nvlddmkm" and e["event_id"] == 153
    assert e["record_id"] == 100713 and e["channel"] == "System"
    assert e["ts"] == T0, "the seven digits of the log's time survive"
    # The message Windows renders for it is empty, so the detail is the record's own strings.
    assert e["detail"] == r"\Device\Video3 | Error occurred on GPUID: 100"
    assert (e["exe"], e["module"], e["pid"], e["code"]) == ("", "", 0, None)


def test_driver_error_ids_13_and_14_count_and_no_other_id_does():
    m.fresh()
    for n, event_id in enumerate((13, 14, 1, 154)):
        m.EVENTS.add_xml("System", _retime(NV_153.replace(">153</EventID>", ">%d</EventID>" % event_id),
                                           "22:14:13.6730008Z", "22:14:%02d.0000000Z" % (20 + n), 200 + n))
    assert [e["event_id"] for e in _events()] == [13, 14]
    # ... and that is the module's own judgement, not only the query's: a log that hands
    # back everything in the time window gets the same answer.
    m.EVENTS.loose = True
    assert [(e["kind"], e["event_id"]) for e in _events()] == [("driver_error", 13), ("driver_error", 14)]


def test_display_4101_counts_only_when_it_names_the_nvidia_driver():
    m.fresh()
    m.EVENTS.add_xml("System", _display_4101("nvlddmkm", 300))
    m.EVENTS.add_xml("System", _display_4101("amdkmdag", 301))      # another card's driver timed out
    m.EVENTS.add_xml("System", _display_4101("igfx", 302))
    m.EVENTS.add_xml("System", _display_4101("", 303))
    e = _one()
    assert (e["kind"], e["provider"], e["event_id"], e["record_id"]) == ("driver_error", "Display", 4101, 300)
    assert "nvlddmkm" in e["detail"]
    assert _events(kinds=["driver_error"]) == [e]


def test_the_real_program_crash_record_is_an_app_crash():
    m.fresh()
    m.EVENTS.add_xml("Application", APP_1000)
    e = _one()
    assert set(e) == EVENT_KEYS
    assert (e["kind"], e["provider"], e["event_id"], e["channel"]) == ("app_crash", "Application Error", 1000,
                                                                       "Application")
    assert e["record_id"] == 86807 and abs(e["ts"] - APP_1000_TS) < 1e-6
    assert e["exe"] == "testgame-win64-shipping.exe", "file names come back lower-case, ready to compare"
    assert e["module"] == "kernelbase.dll"
    assert e["pid"] == 0x5D30, "ProcessId is hex text"
    assert e["code"] == 0xC0000005
    assert "TestGame-Win64-Shipping.exe" in e["detail"] and "KERNELBASE.dll" in e["detail"]


def test_a_crash_record_without_value_names_is_read_by_position():
    """Windows 10 writes the same values without names: the program is value 0,
    the module value 3, the exception code value 6 and the process id value 8."""
    m.fresh()
    m.EVENTS.add_xml("Application", APP_1000_OLD)
    e = _one()
    assert (e["kind"], e["exe"], e["module"], e["pid"], e["code"]) == ("app_crash", "testgame.exe", "nvwgf2umx.dll",
                                                                    0x2F44, 0xC0000409)


def test_a_named_value_is_never_read_by_position():
    """A record that has names but not the one asked for must not fall back to
    whatever sits at the old position."""
    m.fresh()
    m.EVENTS.add_xml("Application", APP_1000.replace("Name='ModuleName'", "Name='SomethingElse'")
                     .replace("Name='ProcessId'", "Name='Other'"))
    e = _one()
    assert (e["exe"], e["module"], e["pid"]) == ("testgame-win64-shipping.exe", "", 0)


def test_a_program_that_stopped_responding_is_an_app_crash():
    m.fresh()
    m.EVENTS.add_xml("Application", HANG_1002)
    e = _one()
    assert (e["kind"], e["provider"], e["event_id"]) == ("app_crash", "Application Hang", 1002)
    assert e["exe"] == "testgame.exe" and e["pid"] == 0x2F44
    assert e["module"] == "", "value 3 of a hang record is a start time, not a module"
    assert e["code"] is None
    # The layout newer builds use for crash records (named values), should hangs get it too.
    m.fresh()
    named = _record("Name='Application Hang'", ">1002", "2026-11-01T22:15:00.0000000Z", 5, "Application",
                    r"<Data Name='AppName'>TestGame.exe</Data><Data Name='AppVersion'>1.0.0.0</Data>"
                    r"<Data Name='ProcessId'>0x2f44</Data><Data Name='StartTime'>0x1dd716f6a550f16</Data>"
                    r"<Data Name='TerminationTime'>4294967295</Data>"
                    r"<Data Name='ExeFileName'>C:\Games\TestGame\TestGame.exe</Data>")
    m.EVENTS.add_xml("Application", named)
    e = _one()
    assert (e["kind"], e["exe"], e["module"], e["pid"]) == ("app_crash", "testgame.exe", "", 0x2F44)


def test_the_real_kernel_power_41_record_is_an_unexpected_shutdown():
    m.fresh()
    m.EVENTS.add_xml("System", KP_41)
    e = _one()
    assert set(e) == EVENT_KEYS
    assert (e["kind"], e["provider"], e["event_id"]) == ("unexpected_shutdown", "Microsoft-Windows-Kernel-Power", 41)
    assert e["record_id"] == 96740 and abs(e["ts"] - KP_41_TS) < 1e-6
    assert e["code"] == 0, "BugcheckCode 0: power loss or a hard reset, no bugcheck"


def test_kernel_power_41_with_a_graphics_bugcheck_code_is_a_gpu_bugcheck():
    """BugcheckCode is decimal: 278, 279, 281, 321 and 270 are 0x116, 0x117,
    0x119, 0x141 and 0x10E.  Any other code stays an unexpected shutdown."""
    cases = ((278, "gpu_bugcheck"), (279, "gpu_bugcheck"), (281, "gpu_bugcheck"), (321, "gpu_bugcheck"),
             (270, "gpu_bugcheck"), (159, "unexpected_shutdown"), (116, "unexpected_shutdown"),
             (2780, "unexpected_shutdown"), (80, "unexpected_shutdown"))
    for code, kind in cases:
        m.fresh()
        m.EVENTS.add_xml("System", KP_41.replace("<Data Name='BugcheckCode'>0</Data>",
                                                 "<Data Name='BugcheckCode'>%d</Data>" % code))
        e = _one()
        assert (e["kind"], e["code"]) == (kind, code), (code, e)
    assert {0x116, 0x117, 0x119, 0x141, 0x10E} == set(ge.GPU_BUGCHECK_CODES) == {278, 279, 281, 321, 270}
    # A code that cannot be read proves nothing about the graphics driver.
    for text in ("", "abc"):
        m.fresh()
        m.EVENTS.add_xml("System", KP_41.replace("<Data Name='BugcheckCode'>0</Data>",
                                                 "<Data Name='BugcheckCode'>%s</Data>" % text))
        e = _one()
        assert (e["kind"], e["code"]) == ("unexpected_shutdown", None)


def test_the_bugcheck_1001_record_counts_for_graphics_codes_only():
    for code in (0x116, 0x117, 0x119, 0x141, 0x10E):
        m.fresh()
        m.EVENTS.add_xml("System", _bugcheck_1001("0x%08x (0xffffb38f5c2d6010, 0x0, 0x0, 0x4)" % code))
        e = _one()
        assert (e["kind"], e["code"], e["channel"]) == ("gpu_bugcheck", code, "System"), hex(code)
        assert e["provider"] == "Microsoft-Windows-WER-SystemErrorReporting" and e["event_id"] == 1001
    # Another bugcheck, a longer number that only starts the same way, and text without a code.
    for first in ("0x0000009f (0x3, 0x0, 0x0, 0x0)", "0x00001160 (0x0, 0x0, 0x0, 0x0)", "116", ""):
        m.fresh()
        m.EVENTS.add_xml("System", _bugcheck_1001(first))
        assert _events() == [], first


def test_a_live_kernel_event_counts_for_141_and_117_only():
    for p1, code in (("141", 0x141), ("117", 0x117)):
        m.fresh()
        m.EVENTS.add_xml("Application", _live_kernel_1001(p1))
        e = _one()
        assert (e["kind"], e["code"], e["channel"]) == ("gpu_bugcheck", code, "Application")
        assert e["provider"] == "Windows Error Reporting" and e["event_id"] == 1001
    for p1, name in (("1a8", "LiveKernelEvent"), ("193", "LiveKernelEvent"), ("", "LiveKernelEvent"),
                     ("141", "APPCRASH"), ("117", "BlueScreen")):
        m.fresh()
        m.EVENTS.add_xml("Application", _live_kernel_1001(p1, name))
        assert _events() == [], (p1, name)


def test_the_real_6008_and_6005_records():
    m.fresh()
    m.EVENTS.add_xml("System", EL_6008)
    m.EVENTS.add_xml("System", EL_6005)
    shutdown, boot = _events()
    assert (shutdown["kind"], shutdown["provider"], shutdown["event_id"]) == ("unexpected_shutdown", "EventLog", 6008)
    assert shutdown["record_id"] == 96728 and abs(shutdown["ts"] - EL_6008_TS) < 1e-6 and shutdown["code"] is None
    assert "5:48:57 PM" in shutdown["detail"] and "10/25/2026" in shutdown["detail"]
    assert LRM not in shutdown["detail"], "the direction marks around the date are dropped"
    assert (boot["kind"], boot["provider"], boot["event_id"]) == ("boot", "EventLog", 6005)
    assert boot["record_id"] == 108464 and abs(boot["ts"] - EL_6005_TS) < 1e-6
    assert _events(kinds=["boot"]) == [boot]
    assert _events(kinds=["unexpected_shutdown"]) == [shutdown]


def test_a_record_in_the_wrong_channel_is_not_ours():
    """A provider name means something only in its own log."""
    m.fresh()
    m.EVENTS.loose = True
    m.EVENTS.add_xml("Application", NV_153)
    m.EVENTS.add_xml("System", APP_1000)
    assert _events() == []


# ═════════════════════════════════════════════════════════════════════
# The fake's add(): what the engine's tests build on
# ═════════════════════════════════════════════════════════════════════
def test_add_writes_a_record_of_every_kind_and_source():
    cases = (
        ("driver_error", {}, "nvlddmkm", 153, "System"),
        ("driver_error", {"event_id": 13}, "nvlddmkm", 13, "System"),
        ("driver_error", {"event_id": 14}, "nvlddmkm", 14, "System"),
        ("driver_error", {"provider": "Display"}, "Display", 4101, "System"),
        ("driver_error", {"event_id": 4101}, "Display", 4101, "System"),
        ("app_crash", {"exe": "Game.exe", "module": "nvwgf2umx.dll", "pid": 4242}, "Application Error", 1000,
         "Application"),
        ("app_crash", {"exe": "Game.exe", "pid": 4242, "provider": "Application Hang"}, "Application Hang", 1002,
         "Application"),
        ("gpu_bugcheck", {}, "Microsoft-Windows-Kernel-Power", 41, "System"),
        ("gpu_bugcheck", {"code": 0x141}, "Microsoft-Windows-Kernel-Power", 41, "System"),
        ("gpu_bugcheck", {"provider": "Microsoft-Windows-WER-SystemErrorReporting", "code": 0x119},
         "Microsoft-Windows-WER-SystemErrorReporting", 1001, "System"),
        ("gpu_bugcheck", {"provider": "Windows Error Reporting"}, "Windows Error Reporting", 1001, "Application"),
        ("gpu_bugcheck", {"provider": "Windows Error Reporting", "code": 0x117}, "Windows Error Reporting", 1001,
         "Application"),
        ("unexpected_shutdown", {}, "Microsoft-Windows-Kernel-Power", 41, "System"),
        ("unexpected_shutdown", {"code": 0x9F}, "Microsoft-Windows-Kernel-Power", 41, "System"),
        ("unexpected_shutdown", {"provider": "EventLog"}, "EventLog", 6008, "System"),
        ("unexpected_shutdown", {"event_id": 6008}, "EventLog", 6008, "System"),
        ("boot", {}, "EventLog", 6005, "System"),
    )
    for kind, extra, provider, event_id, channel in cases:
        m.fresh()
        record_id = m.EVENTS.add(kind, T0, **extra)
        e = _one()
        assert set(e) == EVENT_KEYS
        assert (e["kind"], e["provider"], e["event_id"], e["channel"]) == (kind, provider, event_id, channel), extra
        assert e["ts"] == T0 and e["record_id"] == record_id
        assert e["exe"] == extra.get("exe", "").lower() and e["pid"] == extra.get("pid", 0)
        assert e["module"] == extra.get("module", "")
        if kind == "gpu_bugcheck":
            assert e["code"] == extra.get("code", 0x141 if provider == "Windows Error Reporting" else 0x116)
        elif provider == "Microsoft-Windows-Kernel-Power":
            assert e["code"] == extra.get("code", 0)
        assert isinstance(e["ts"], float) and isinstance(e["record_id"], int) and isinstance(e["detail"], str)


def test_add_numbers_records_itself_and_takes_a_record_id():
    m.fresh()
    a = _nv(T0)
    b = _nv(T0 + 1)
    c = _nv(T0 + 2, record_id=5000)
    d = _nv(T0 + 3)
    assert b == a + 1 and c == 5000 and d == 5001
    assert [e["record_id"] for e in _events()] == [a, b, c, d]


def test_add_refuses_what_would_not_be_the_kind_asked_for():
    m.fresh()
    for kind, extra in (("driver_crash", {}), ("gpu_bugcheck", {"code": 0}), ("gpu_bugcheck", {"code": 0x9F}),
                        ("unexpected_shutdown", {"code": 0x116}), ("boot", {"provider": "nvlddmkm"}),
                        ("driver_error", {"event_id": 6008}),
                        ("gpu_bugcheck", {"provider": "Windows Error Reporting", "code": 0x116})):
        try:
            m.EVENTS.add(kind, T0, **extra)
        except ValueError:
            continue
        raise AssertionError(f"add({kind!r}, {extra}) should have been refused")
    for bad in (("Security", NV_153), ("System", "<Event/>")):
        try:
            m.EVENTS.add_xml(*bad)
        except ValueError:
            continue
        raise AssertionError(f"add_xml{bad[:1]} should have been refused")
    assert m.EVENTS.records == []


# ═════════════════════════════════════════════════════════════════════
# Time bounds
# ═════════════════════════════════════════════════════════════════════
def test_both_bounds_are_inclusive():
    m.fresh()
    for n in range(3):
        _nv(T0 + 10 * n)
    assert [e["ts"] for e in _events(T0, T0 + 10)] == [T0, T0 + 10]
    assert [e["ts"] for e in _events(T0, T0)] == [T0]
    assert [e["ts"] for e in _events(T0 + 10, T0 + 20)] == [T0 + 10, T0 + 20]
    assert _events(T0 + 20.5, T0 + 30) == []


def test_the_bounds_are_exact_although_the_query_is_in_milliseconds():
    """The query text carries milliseconds, the records 100 nanoseconds.  A
    record in the same millisecond as a bound but on the wrong side of it is
    handed over by Windows and must still be left out."""
    m.fresh()
    m.EVENTS.add_xml("System", NV_153)                          # at ...13.6730008
    assert _events(T0 + 0.0002, T0 + 5) == []
    assert m.EVENTS.render_sizes, "the log did hand the record over"
    del m.EVENTS.render_sizes[:]
    assert _events(T0 - 5, T0 - 0.0000004) == []                # ...13.6730004: the same millisecond, just before
    assert m.EVENTS.render_sizes
    assert len(_events(T0 - 0.0000004, T0 + 0.0002)) == 1


def test_without_until_the_window_ends_now():
    m.fresh()
    ge._now = lambda: T0 + 5
    _nv(T0)
    _nv(T0 + 60)                                                # stamped after "now": not there yet
    assert [e["ts"] for e in _events(T0 - 10, None)] == [T0]
    assert "@SystemTime<='2026-11-01T22:14:18.674Z'" in m.EVENTS.queries[-1][1]
    ge._now = lambda: T0 + 61
    assert [e["ts"] for e in _events(T0 - 10, None)] == [T0, T0 + 60]
    r = ge.query(T0 - 10)
    assert r["ok"] and len(r["events"]) == 2


def test_the_query_text_has_absolute_bounds_and_the_real_flags():
    m.fresh()
    _events(T0, T0 + 22, kinds=["driver_error"])
    (channel, xpath, flags), = m.EVENTS.queries
    assert channel == "System"
    assert xpath == ("*[System[((Provider[@Name='nvlddmkm'] and (EventID=153 or EventID=13 or EventID=14)) or "
                     "(Provider[@Name='Display'] and EventID=4101)) and "
                     "TimeCreated[@SystemTime>='2026-11-01T22:14:13.673Z' and "
                     "@SystemTime<='2026-11-01T22:14:35.674Z']]]")
    assert "timediff" not in xpath, "bounds relative to the clock at query time cannot be tested or replayed"
    assert flags == 0x1 | 0x200, "EvtQueryChannelPath | EvtQueryReverseDirection"


def test_a_window_that_ends_before_it_starts_is_empty_and_asks_nothing():
    m.fresh()
    _nv(T0)
    assert _events(T0 + 10, T0 - 10) == []
    assert m.EVENTS.queries == []


def test_times_outside_what_a_log_can_hold_are_tolerated():
    m.fresh()
    m.EVENTS.add("driver_error", 5.25)
    _nv(T0)
    assert [e["ts"] for e in _events(-100.0, 1e15)] == [5.25, T0]
    assert "@SystemTime>='1970-01-01T00:00:00.000Z'" in m.EVENTS.queries[-1][1]
    assert "@SystemTime<='9999-12-31T23:59:59.000Z'" in m.EVENTS.queries[-1][1]
    assert [e["ts"] for e in _events(0, 10)] == [5.25]


# ═════════════════════════════════════════════════════════════════════
# Kinds and channels
# ═════════════════════════════════════════════════════════════════════
def _one_of_each():
    m.EVENTS.add("driver_error", T0)
    m.EVENTS.add("driver_error", T0 + 1, provider="Display")
    m.EVENTS.add("app_crash", T0 + 2, exe="game.exe", pid=7)
    m.EVENTS.add("app_crash", T0 + 3, exe="game.exe", pid=7, provider="Application Hang")
    m.EVENTS.add("gpu_bugcheck", T0 + 4)
    m.EVENTS.add("gpu_bugcheck", T0 + 5, provider="Microsoft-Windows-WER-SystemErrorReporting")
    m.EVENTS.add("gpu_bugcheck", T0 + 6, provider="Windows Error Reporting")
    m.EVENTS.add("unexpected_shutdown", T0 + 7)
    m.EVENTS.add("unexpected_shutdown", T0 + 8, provider="EventLog")
    m.EVENTS.add("boot", T0 + 9)


def test_every_kind_is_found_with_one_query_per_channel():
    m.fresh()
    _one_of_each()
    events = _events()
    assert [e["kind"] for e in events] == ["driver_error", "driver_error", "app_crash", "app_crash", "gpu_bugcheck",
                                           "gpu_bugcheck", "gpu_bugcheck", "unexpected_shutdown",
                                           "unexpected_shutdown", "boot"]
    assert all(set(e) == EVENT_KEYS for e in events)
    assert [q[0] for q in m.EVENTS.queries] == ["System", "Application"]
    assert set(ge.KINDS) == {e["kind"] for e in events}


def test_kinds_narrow_the_answer_and_the_channels_read():
    m.fresh()
    _one_of_each()
    for kinds, count, channels in ((["driver_error"], 2, ["System"]),
                                   ("driver_error", 2, ["System"]),
                                   (("app_crash",), 2, ["Application"]),
                                   (["gpu_bugcheck"], 3, ["System", "Application"]),
                                   (["unexpected_shutdown"], 2, ["System"]),
                                   ({"boot"}, 1, ["System"]),
                                   (["boot", "driver_error", "unexpected_shutdown"], 5, ["System"]),
                                   (["app_crash", "driver_error"], 4, ["System", "Application"]),
                                   ([], 0, [])):
        del m.EVENTS.queries[:]
        events = _events(kinds=kinds)
        wanted = {kinds} if isinstance(kinds, str) else set(kinds)
        assert len(events) == count and {e["kind"] for e in events} <= wanted, kinds
        assert [q[0] for q in m.EVENTS.queries] == channels, kinds
    # The poll that runs every 20 seconds in a game asks only for what it needs.
    del m.EVENTS.queries[:]
    _events(kinds=["driver_error"])
    assert "Kernel-Power" not in m.EVENTS.queries[0][1] and "EventLog" not in m.EVENTS.queries[0][1]
    # Kernel-Power 41 is read for either of its two kinds and sorted out afterwards.
    assert [e["code"] for e in _events(kinds=["unexpected_shutdown"])] == [0, None]
    assert [e["provider"] for e in _events(kinds=["gpu_bugcheck"])][0] == "Microsoft-Windows-Kernel-Power"


def test_channels_narrow_the_reading_to_one_log():
    """A caller that only uses the System log's records (what stopped the PC)
    asks only that log: the Application log is not read, so it cannot fail
    the answer either."""
    m.fresh()
    _one_of_each()
    both = _events(kinds=["gpu_bugcheck"])
    assert {e["channel"] for e in both} == {"System", "Application"}
    for channels, read in (("System", ["System"]), (["System"], ["System"]), (("Application",), ["Application"]),
                           (["Application", "System"], ["System", "Application"]), (None, ["System", "Application"]),
                           ([], [])):
        del m.EVENTS.queries[:]
        r = ge.query(T0 - 400 * DAY, T0 + 400 * DAY, ["gpu_bugcheck"], channels=channels)
        assert r["ok"] is True and [q[0] for q in m.EVENTS.queries] == read, channels
        assert r["events"] == [e for e in both if e["channel"] in read], channels
    del m.EVENTS.queries[:]
    assert ge.query(T0 - 10, T0 + 100, ["app_crash"], channels="System") == {"ok": True, "events": [], "err": ""}
    assert m.EVENTS.queries == [], "no source of that kind in that log: nothing is asked"
    for channels in ("Security", ["System", "Setup"], 5, [["System"]]):
        r = ge.query(T0 - 10, T0 + 100, ["boot"], channels=channels)
        assert r["ok"] is False and r["events"] == [] and r["err"], channels
    assert m.EVENTS.queries == []
    _tidy()


def test_an_unknown_kind_is_an_error_not_an_empty_answer():
    m.fresh()
    _one_of_each()
    for kinds in (["driver_crash"], "tdr", ["driver_error", "nope"], 5, [["driver_error"]]):
        r = ge.query(T0 - 10, T0 + 100, kinds)
        assert r["ok"] is False and r["events"] == [] and r["err"], kinds
    assert m.EVENTS.queries == []
    _tidy()


# ═════════════════════════════════════════════════════════════════════
# Order and duplicates
# ═════════════════════════════════════════════════════════════════════
def test_events_come_back_oldest_first_whatever_order_the_log_has_them_in():
    """Record ids are not in time order.  In the real log the 6008 record
    (96728) was written before the Kernel-Power 41 (96740) but carries the
    later time."""
    m.fresh()
    m.EVENTS.add_xml("System", EL_6008)
    m.EVENTS.add_xml("System", KP_41)
    m.EVENTS.add_xml("Application", APP_1000)
    m.EVENTS.add_xml("System", NV_153)
    m.EVENTS.add_xml("System", EL_6005)
    events = _events()
    assert [e["record_id"] for e in events] == [96740, 96728, 100713, 86807, 108464]
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    # ... while the log itself hands them over by record number, newest first.
    handle, _err = m.EVENTS.EvtQuery("System", "*", 0x201)
    handed, _err = m.EVENTS.EvtNext(handle, 10, 0)
    assert [m.EVENTS._handles[h][1]["record_id"] for h in handed] == [108464, 100713, 96740, 96728]
    for h in handed + [handle]:
        m.EVENTS.EvtClose(h)
    _tidy()


def test_events_at_the_same_time_are_ordered_by_channel_then_record():
    m.fresh()
    m.EVENTS.add("driver_error", T0, record_id=30)
    m.EVENTS.add("app_crash", T0, exe="a.exe", record_id=40)
    m.EVENTS.add("boot", T0, record_id=20)
    m.EVENTS.add("app_crash", T0, exe="b.exe", record_id=10)
    assert [(e["channel"], e["record_id"]) for e in _events()] == [("Application", 10), ("Application", 40),
                                                                   ("System", 20), ("System", 30)]


def test_a_record_comes_back_once_and_record_ids_are_per_channel():
    m.fresh()
    m.EVENTS.add_xml("System", NV_153)
    m.EVENTS.add_xml("System", NV_153)                          # the log hands the same record over twice
    assert len(_events()) == 1
    # The same number in the other log is another record.
    m.EVENTS.add_xml("Application", _retime(APP_1000, "<EventRecordID>86807", "<EventRecordID>100713"))
    events = _events()
    assert [(e["channel"], e["record_id"], e["kind"]) for e in events] == [
        ("System", 100713, "driver_error"), ("Application", 100713, "app_crash")]


# ═════════════════════════════════════════════════════════════════════
# How the log is read
# ═════════════════════════════════════════════════════════════════════
def test_each_record_is_rendered_with_two_calls_and_a_size_in_bytes():
    m.fresh()
    m.EVENTS.add_xml("System", NV_153)
    m.EVENTS.add_xml("System", KP_41)
    _events(kinds=["driver_error", "unexpected_shutdown"])
    sizes = m.EVENTS.render_sizes
    assert len(sizes) == 4 and sizes[0] == 0 and sizes[2] == 0, "first ask how big, with no buffer"
    # UTF-16 text with its terminator: two bytes per character, not one.
    assert sorted((sizes[1], sizes[3])) == sorted(((len(NV_153) + 1) * 2, (len(KP_41) + 1) * 2))


def test_more_records_than_one_batch_are_all_read_and_closed():
    m.fresh()
    for n in range(150):
        _nv(T0 + n)
    events = _events(kinds=["driver_error"])
    assert len(events) == 150 and [e["ts"] for e in events] == [T0 + n for n in range(150)]
    assert max(m.EVENTS.next_counts) <= 64 and len(m.EVENTS.next_counts) >= 3
    assert len(m.EVENTS.render_sizes) == 300


def test_a_flood_of_records_is_capped_and_the_newest_are_kept():
    m.fresh()
    for n in range(25):
        _nv(T0 + n)
    keep = ge.MAX_RECORDS
    ge.MAX_RECORDS = 10
    try:
        events = _events(kinds=["driver_error"])
    finally:
        ge.MAX_RECORDS = keep
    assert [e["ts"] for e in events] == [T0 + n for n in range(15, 25)]
    assert sum(m.EVENTS.next_counts) == 10, "it never asks for more than the cap"
    assert len(_events(kinds=["driver_error"])) == 25


def test_queries_from_several_threads_do_not_trip_over_each_other():
    m.fresh()
    for n in range(40):
        _nv(T0 + n)
    wrong = []

    def worker():
        for _ in range(15):
            r = ge.query(T0 - 1, T0 + 100, kinds=["driver_error"])
            if not r["ok"] or len(r["events"]) != 40:
                wrong.append(r)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert wrong == []
    _tidy()


# ═════════════════════════════════════════════════════════════════════
# When the log cannot be read
# ═════════════════════════════════════════════════════════════════════
def test_a_log_that_cannot_be_read_is_an_error_with_no_events_and_no_open_handles():
    for stage in ("query", "next", "render"):
        m.fresh()
        _one_of_each()
        m.EVENTS.fail, m.EVENTS.fail_stage = True, stage
        r = ge.query(T0 - 10, T0 + 100)
        assert r["ok"] is False and r["events"] == [], stage
        assert isinstance(r["err"], str) and r["err"], stage
        assert set(r) == {"ok", "events", "err"}
        _tidy()
        # ... and it works again as soon as the log does.
        m.EVENTS.fail = False
        assert len(_events()) == 10


class _Wrapped:
    """The sandbox's log with one call replaced."""

    def __init__(self, **replaced):
        self._replaced = replaced

    def __getattr__(self, name):
        return self._replaced.get(name) or getattr(m.EVENTS, name)


def test_half_a_picture_is_no_picture():
    """One channel readable and the other not: the answer is an error, never
    the readable half.  Asking for one channel's kinds still works."""
    m.fresh()
    _one_of_each()

    def no_application_log(channel, xpath, flags):
        return (0, 5) if channel == "Application" else m.EVENTS.EvtQuery(channel, xpath, flags)

    ge._api = _Wrapped(EvtQuery=no_application_log)
    r = ge.query(T0 - 10, T0 + 100)
    assert r["ok"] is False and r["events"] == [] and "Application" in r["err"]
    _tidy()
    r = ge.query(T0 - 10, T0 + 100, kinds=["driver_error", "unexpected_shutdown", "boot"])
    assert r["ok"] is True and len(r["events"]) == 5
    _tidy()


def test_an_adapter_that_raises_is_caught_and_every_handle_still_closed():
    m.fresh()
    _one_of_each()

    def boom(*a, **k):
        raise RuntimeError("wevtapi went away")

    for name in ("EvtQuery", "EvtNext", "EvtRender"):
        ge._api = _Wrapped(**{name: boom})
        r = ge.query(T0 - 10, T0 + 100)
        assert r["ok"] is False and r["events"] == [] and r["err"], name
        _tidy()
        assert ge.any_system_record(T0 - 10, T0 + 100) is True, name     # see the test further down
        _tidy()
        ge.reset_cache()
        b = ge.driver_error_bursts()
        assert b == {"ok": False, "bursts": [], "events": 0, "covered_days": 0}, name
        _tidy()
    # A close that raises must not leave the others undone or escape.
    closed = []

    def close_then_raise(handle):
        closed.append(m.EVENTS.EvtClose(handle))
        raise OSError("close failed")

    ge._api = _Wrapped(EvtClose=close_then_raise)
    r = ge.query(T0 - 10, T0 + 100)
    assert r["ok"] is True and len(r["events"]) == 10 and all(closed)
    _tidy()
    ge._api = None
    assert ge.query(T0 - 10, T0 + 100)["ok"] is False


def test_bad_arguments_are_an_error_and_never_raise():
    m.fresh()
    _nv(T0)
    for since, until in ((None, None), ("yesterday", None), (float("nan"), T0), (T0, float("inf")), (T0, "now"),
                         ([T0], T0), (True, T0)):
        r = ge.query(since, until)
        assert r["ok"] is False and r["events"] == [] and r["err"], (since, until)
    assert m.EVENTS.queries == []
    assert len(_events(int(T0) - 1, int(T0) + 1)) == 1, "whole seconds are fine"


def test_a_record_with_characters_xml_does_not_allow_is_still_read():
    """Event data can hold a control character, which Windows renders as a
    character reference no XML parser accepts."""
    m.fresh()
    m.EVENTS.add_xml("System", NV_153.replace("Error occurred on GPUID: 100", "Error &#x1;occurred&#11; on GPUID: 100"))
    m.EVENTS.add_xml("System", _retime(NV_153.replace("GPUID: 100", "GPUID: 100" + chr(0x0B)),
                                       "22:14:13.6730008Z", "22:14:15.0000000Z", 100720))
    events = _events()
    assert [e["kind"] for e in events] == ["driver_error", "driver_error"]
    assert events[0]["detail"] == r"\Device\Video3 | Error occurred on GPUID: 100"


def test_a_record_that_is_not_well_formed_keeps_its_header():
    """XML that does not parse even after cleaning: who wrote the record, its id
    and its time can still be read, so a driver error is still counted.  Only
    its data is lost."""
    m.fresh()
    m.EVENTS.add_xml("System", NV_153.replace("</EventData>", ""))
    m.EVENTS.add_xml("System", _retime(KP_41.replace("<Data Name='BugcheckCode'>0</Data>",
                                                     "<Data Name='BugcheckCode'>278</Data>")
                                       .replace("</EventData>", ""), "23:04:23.6168169Z", "23:04:23.6168169Z", 96741))
    m.EVENTS.add_xml("System", _retime(NV_153, "22:14:13.6730008Z", "22:14:15.0000000Z", 100720)
                     .replace("<EventRecordID>100720", "<EventRecordID> 100720 "))
    stopped, broken, whole = _events()
    assert (broken["kind"], broken["record_id"], broken["ts"], broken["detail"]) == ("driver_error", 100713, T0, "")
    assert (whole["kind"], whole["record_id"]) == ("driver_error", 100720)
    # Without its data a Kernel-Power 41 cannot be called a graphics bugcheck.
    assert (stopped["kind"], stopped["code"], stopped["record_id"]) == ("unexpected_shutdown", None, 96741)
    assert all(set(e) == EVENT_KEYS for e in (stopped, broken, whole))


def test_a_record_that_cannot_be_read_at_all_is_skipped_not_fatal():
    m.fresh()
    _nv(T0)
    _nv(T0 + 1)
    _nv(T0 + 2)
    calls = []

    def render(event, size):
        ok, text, used, err = m.EVENTS.EvtRender(event, size)
        calls.append(ok)
        if ok and "<EventRecordID>1002<" in text:
            return True, "this is not an event", used, 0
        return ok, text, used, err

    ge._api = _Wrapped(EvtRender=render)
    events = _events()
    assert [e["record_id"] for e in events] == [1001, 1003] and calls.count(True) == 3


# ═════════════════════════════════════════════════════════════════════
# any_system_record
# ═════════════════════════════════════════════════════════════════════
SCM_7036 = _record("Name='Service Control Manager' Guid='{555908d1-a6d7-4695-8e1e-26931d2012f4}' "
                   "EventSourceName='Service Control Manager'", " Qualifiers='16384'>7036",
                   "2026-11-01T22:30:00.0000000Z", 100750, "System",
                   "<Data Name='param1'>Windows Update</Data><Data Name='param2'>running</Data>", level=4)
SCM_TS = calendar.timegm((2026, 11, 1, 22, 30, 0))


def test_any_system_record_sees_any_record_of_any_provider():
    m.fresh()
    assert ge.any_system_record(0, T0 + DAY) is False
    m.EVENTS.add_xml("System", SCM_7036)                        # nothing this module has a kind for
    m.EVENTS.add("app_crash", SCM_TS, exe="a.exe")             # the Application log does not count
    assert ge.any_system_record(SCM_TS - 60, SCM_TS + 60) is True
    assert ge.any_system_record(SCM_TS, SCM_TS) is True, "both bounds are inclusive"
    assert ge.any_system_record(SCM_TS - 600, SCM_TS - 1) is False
    assert ge.any_system_record(SCM_TS + 1, SCM_TS + 600) is False
    assert ge.any_system_record(SCM_TS + 60, SCM_TS - 60) is False, "a window that ends before it starts is empty"
    _tidy()
    assert ge.query(SCM_TS - 60, SCM_TS + 60, kinds=["driver_error", "boot"])["events"] == []


def test_any_system_record_looks_at_one_record_at_most():
    m.fresh()
    for n in range(500):
        m.EVENTS.add("boot", T0 + n)
    del m.EVENTS.queries[:], m.EVENTS.next_counts[:], m.EVENTS.render_sizes[:]
    assert ge.any_system_record(T0, T0 + 1000) is True
    (channel, xpath, _flags), = m.EVENTS.queries
    assert channel == "System" and "Provider" not in xpath
    assert xpath == ("*[System[TimeCreated[@SystemTime>='2026-11-01T22:14:13.673Z' and "
                     "@SystemTime<='2026-11-01T22:30:53.674Z']]]")
    assert m.EVENTS.next_counts == [1] and m.EVENTS.render_sizes == [], "one handle, never rendered"
    _tidy()


def test_any_system_record_says_yes_when_it_cannot_tell():
    """The caller takes "no record" as proof that the PC was off.  A log that
    could not be read proves nothing, so the answer must not be "no"."""
    for stage in ("query", "next"):
        m.fresh()
        m.EVENTS.fail, m.EVENTS.fail_stage = True, stage
        assert ge.any_system_record(T0, T0 + 60) is True, stage
        _tidy()
    m.fresh()
    for since, until in ((None, T0), (T0, None), ("a", "b"), (float("nan"), T0)):
        assert ge.any_system_record(since, until) is True
    assert m.EVENTS.queries == []


def test_the_newest_system_record_is_told_by_its_number_not_its_time():
    """Adaptive Tuning notes this number when a game starts: what Windows
    wrote before it is never that session's, whatever time it carries."""
    m.fresh()
    assert ge.newest_system_record_id() is None, "an empty log has no newest record"
    m.EVENTS.add("driver_error", T0, record_id=500)
    m.EVENTS.add("app_crash", T0 + 5, exe="a.exe", record_id=9000)      # the Application log does not count
    assert ge.newest_system_record_id() == 500
    m.EVENTS.add_xml("System", SCM_7036)                                # record 100750, of no kind this module has
    assert ge.newest_system_record_id() == 100750
    m.EVENTS.add("boot", T0 - 5 * DAY, record_id=100751)                # written last, with an older time on it
    del m.EVENTS.queries[:], m.EVENTS.next_counts[:]
    assert ge.newest_system_record_id() == 100751, "the order Windows wrote them in, not their times"
    (channel, xpath, flags), = m.EVENTS.queries
    assert (channel, xpath) == ("System", "*") and flags & 0x200, "newest first"
    assert m.EVENTS.next_counts == [1], "one record is read"
    _tidy()
    for stage in ("query", "next"):
        m.fresh()
        m.EVENTS.add("boot", T0)
        m.EVENTS.fail, m.EVENTS.fail_stage = True, stage
        assert ge.newest_system_record_id() is None, stage
        _tidy()


# ═════════════════════════════════════════════════════════════════════
# Driver error bursts
# ═════════════════════════════════════════════════════════════════════
def _bursts(days=30, exclude=None):
    r = ge.driver_error_bursts(days, exclude) if exclude is not None else ge.driver_error_bursts(days)
    assert set(r) == {"ok", "bursts", "events", "covered_days"}
    assert all(set(b) == {"first_ts", "last_ts", "count"} for b in r["bursts"])
    assert r["events"] == sum(b["count"] for b in r["bursts"])
    _tidy()
    return r


def test_errors_less_than_five_minutes_apart_are_one_burst():
    m.fresh()
    base = 1793571000.0             # a whole second, so the half seconds below are exact
    ge._now = lambda: base + 3600
    for seconds in (0, 100, 399.5, 699.5, 1000, 1299.9):
        _nv(base + seconds)
    r = _bursts()
    assert r["ok"] is True and r["events"] == 6
    # 0, 100 and 399.5 chain (each under 300 s after the one before); 699.5 is exactly 300 s
    # after 399.5 and starts a new burst; 1000 and 1299.9 follow 300.5 s and 299.9 s later.
    assert r["bursts"] == [{"first_ts": base, "last_ts": base + 399.5, "count": 3},
                           {"first_ts": base + 699.5, "last_ts": base + 699.5, "count": 1},
                           {"first_ts": base + 1000, "last_ts": base + 1299.9, "count": 2}]
    assert ge.BURST_GAP_S == 300


def test_the_real_log_replayed():
    """The 19 errors of the real log are 11 bursts.  Seen from 13 November only
    three fall in the last 30 days, and each is a burst of its own."""
    m.fresh()
    first = T0 - REAL_ERRORS[-1]
    m.EVENTS.add("boot", first - 1.5 * DAY)                    # the log starts a day and a half before
    for seconds in REAL_ERRORS:
        _nv(first + seconds)
    ge._now = lambda: T0 + 11.9 * DAY
    r = _bursts(120)
    assert (r["ok"], r["events"], len(r["bursts"])) == (True, 19, 11)
    assert [b["count"] for b in r["bursts"]] == [5, 1, 3, 2, 2, 1, 1, 1, 1, 1, 1]
    assert r["bursts"][0]["first_ts"] == first and abs(r["bursts"][0]["last_ts"] - (first + 299.119)) < 1e-6
    assert r["covered_days"] == 52, "the log is 52.3 days old, less than the 120 asked for"
    r = _bursts(30)
    assert (r["events"], [b["count"] for b in r["bursts"]], r["covered_days"]) == (3, [1, 1, 1], 30)
    assert _bursts(7)["events"] == 0 and _bursts(7)["bursts"] == [] and _bursts(7)["covered_days"] == 7


def test_only_driver_errors_are_counted():
    m.fresh()
    ge._now = lambda: T0 + 3600
    _one_of_each()
    r = _bursts()
    assert r["events"] == 2 and len(r["bursts"]) == 1, "nvlddmkm 153 and the Display 4101 that names it"
    assert [q[0] for q in m.EVENTS.queries[:1]] == ["System"]
    assert all(q[0] == "System" for q in m.EVENTS.queries), "the Application log is not read for this"


def test_errors_inside_an_excluded_window_are_left_out():
    m.fresh()
    ge._now = lambda: T0 + 3600
    for seconds in (0, 200, 400, 2000, 2100):
        _nv(T0 + seconds)
    assert [b["count"] for b in _bursts()["bursts"]] == [3, 2]
    # Without the error at 200 s the ones at 0 and 400 are 400 s apart: two bursts.
    r = _bursts(30, [(T0 + 150, T0 + 250)])
    assert r["events"] == 4 and [b["count"] for b in r["bursts"]] == [1, 1, 2]
    # Both ends of a window belong to it; lists work as well as tuples; several windows add up.
    r = _bursts(30, [[T0 + 2000, T0 + 2100], (T0, T0)])
    assert r["events"] == 2 and [(b["first_ts"], b["count"]) for b in r["bursts"]] == [(T0 + 200, 2)]
    r = _bursts(30, [(T0 - 10, T0 + 5000)])
    assert (r["ok"], r["events"], r["bursts"]) == (True, 0, [])
    # Entries that are not a (start, end) pair of times are ignored, not fatal.
    r = _bursts(30, [None, (T0 + 150,), "x", (T0 + 250, T0 + 150), ("a", "b"), (T0 + 150, T0 + 250)])
    assert r["events"] == 4
    assert _bursts(30, [])["events"] == 5 and _bursts(30, ())["events"] == 5


def test_covered_days_is_how_far_back_the_system_log_goes():
    for age_days, asked, expected in ((51.9, 120, 51), (51.9, 30, 30), (30.01, 30, 30), (29.99, 30, 29),
                                      (12.7, 30, 12), (0.5, 30, 0), (400.0, 365, 365)):
        m.fresh()
        ge._now = lambda: T0
        m.EVENTS.add_xml("System", SCM_7036.replace("2026-11-01T22:30:00.0000000Z",
                                                    m.EVENTS.stamp(T0 - age_days * DAY)))
        m.EVENTS.add("boot", T0 - 3600)
        m.EVENTS.add("app_crash", T0 - 500 * DAY, exe="old.exe")       # the other log does not count
        r = _bursts(asked)
        assert (r["ok"], r["covered_days"]) == (True, expected), (age_days, asked)
    # An empty System log (or one whose oldest record cannot be dated) covers nothing.
    m.fresh()
    r = _bursts()
    assert r == {"ok": True, "bursts": [], "events": 0, "covered_days": 0}


def test_the_answer_is_cached_for_five_minutes():
    m.fresh()
    clock = [T0 + 3600]
    ge._now = lambda: clock[0]
    _nv(T0)
    first = _bursts()
    asked = len(m.EVENTS.queries)
    assert first["events"] == 1 and asked >= 1
    _nv(T0 + 1000)
    clock[0] += 299
    assert _bursts() == first and len(m.EVENTS.queries) == asked, "no new read inside five minutes"
    clock[0] += 2
    later = _bursts()
    assert later["events"] == 2 and len(m.EVENTS.queries) > asked
    # What comes back is the caller's own copy, fresh from the log or from the cache.
    expected = [{"first_ts": T0, "last_ts": T0, "count": 1}, {"first_ts": T0 + 1000, "last_ts": T0 + 1000, "count": 1}]
    for answer in (later, _bursts(), _bursts()):
        assert answer["bursts"] == expected
        answer["bursts"].append("x")
        answer["bursts"][0]["count"] = 99
        answer["events"] = -1
    assert _bursts() == {"ok": True, "bursts": expected, "events": 2, "covered_days": 0}
    # reset_cache() forgets it at once.
    _nv(T0 + 2000)
    assert _bursts()["events"] == 2
    ge.reset_cache()
    assert _bursts()["events"] == 3
    # A clock that went backwards does not keep an old answer alive.
    _nv(T0 + 2500)
    clock[0] -= 1000
    assert _bursts()["events"] == 4


def test_each_question_has_its_own_cached_answer():
    m.fresh()
    ge._now = lambda: T0 + 40 * DAY
    _nv(T0)
    _nv(T0 + 35 * DAY)
    assert _bursts(30)["events"] == 1 and _bursts(90)["events"] == 2
    assert _bursts(30, [(T0 + 34 * DAY, T0 + 36 * DAY)])["events"] == 0
    asked = len(m.EVENTS.queries)
    assert _bursts(30)["events"] == 1 and _bursts(90)["events"] == 2
    assert _bursts(30, [(T0 + 34 * DAY, T0 + 36 * DAY)])["events"] == 0
    assert _bursts(30.0, [[T0 + 34 * DAY, T0 + 36 * DAY]])["events"] == 0, "the same question, written differently"
    assert len(m.EVENTS.queries) == asked


def test_a_failed_read_is_reported_and_asked_again_after_half_a_minute():
    m.fresh()
    clock = [T0 + 3600]
    ge._now = lambda: clock[0]
    _nv(T0)
    m.EVENTS.fail = True
    failed = {"ok": False, "bursts": [], "events": 0, "covered_days": 0}
    assert _bursts() == failed
    asked = len(m.EVENTS.queries)
    m.EVENTS.fail = False
    clock[0] += 29
    assert _bursts() == failed and len(m.EVENTS.queries) == asked, "not asked again every few seconds"
    clock[0] += 2
    r = _bursts()
    assert r["ok"] is True and r["events"] == 1


def test_bursts_never_raise_on_odd_arguments():
    m.fresh()
    _nv(T0)
    ge._now = lambda: T0 + 60
    assert _bursts(0) == {"ok": True, "bursts": [], "events": 0, "covered_days": 0}
    assert _bursts(-3)["ok"] is True
    for days in ("30", None, float("nan"), [30]):
        assert ge.driver_error_bursts(days) == {"ok": False, "bursts": [], "events": 0, "covered_days": 0}
    assert _bursts(30, 5)["events"] == 1 and _bursts(30, "nonsense")["events"] == 1
    assert _bursts(10 ** 9)["events"] == 1, "an absurd span is cut down, not an overflow"


# ═════════════════════════════════════════════════════════════════════
# The sandbox
# ═════════════════════════════════════════════════════════════════════
def test_the_module_only_ever_sees_the_fake_log():
    m.fresh()
    assert ge._api is m.EVENTS
    assert m.app("core.gpu_events") is ge and ge._api is m.EVENTS
    ge._api = ge.WevtApi()
    assert m.wire(ge) is ge and ge._api is m.EVENTS


def test_fresh_empties_the_log_and_puts_the_module_back():
    m.fresh()
    ge._now = lambda: T0 + 3600
    _nv(T0)
    assert _bursts()["events"] == 1
    m.EVENTS.fail, m.EVENTS.fail_stage, m.EVENTS.loose = True, "render", True
    ge._api = None
    m.fresh()
    assert ge._api is m.EVENTS
    assert abs(ge._now() - time.time()) < 5, "the clock seam is the real clock again"
    assert (m.EVENTS.fail, m.EVENTS.fail_stage, m.EVENTS.loose) == (False, "query", False)
    assert m.EVENTS.records == [] and m.EVENTS.queries == [] and m.EVENTS.open_handles == 0
    ge._now = lambda: T0 + 3600
    assert _bursts()["events"] == 0, "the cached answer went with it"
    assert _nv(T0) == 1001, "record numbers start over"


def test_the_fake_refuses_what_windows_would_refuse():
    m.fresh()
    assert m.EVENTS.EvtQuery("System", "*[System[TimeCreated[timediff(@SystemTime) <= 1000]]]", 1) == (0, 15001)
    assert m.EVENTS.EvtQuery("System", "*[System[(Provider[@Name='x'] and and) and "
                                       "TimeCreated[@SystemTime>='2026-11-01T22:14:13.673Z']]]", 1) == (0, 15001)
    assert m.EVENTS.EvtQuery("Security", "*", 1) == (0, 15007)
    handle, err = m.EVENTS.EvtQuery("System", "*", 1)
    assert handle and err == 0 and m.EVENTS.open_handles == 1
    assert m.EVENTS.EvtNext(handle, 4, 0) == ([], 259)
    assert m.EVENTS.EvtClose(handle) is True and m.EVENTS.EvtClose(handle) is False
    assert m.EVENTS.bad and m.EVENTS.open_handles == 0
    assert m.EVENTS.stamp(T0) == "2026-11-01T22:14:13.6730008Z"


# ═════════════════════════════════════════════════════════════════════
# The module itself
# ═════════════════════════════════════════════════════════════════════
def _source():
    with open(os.path.join(m.REPO, "core", "gpu_events.py"), encoding="utf-8") as f:
        return f.read()


def test_it_starts_no_process_and_reads_the_clock_in_one_place():
    """The time of day only in _now(), the time limit's clock only in
    _monotonic(): tests can stand in for either."""
    m.fresh()
    tree = ast.parse(_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            assert not {a.name for a in node.names} & {"run_ps", "run_cmd"}
    assert not imported & {"subprocess", "os", "winreg", "shutil"}, imported
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"run_ps", "run_cmd", "Popen", "startfile", "subprocess"}
    readers = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id in ("time", "datetime")
                        and n.attr in ("time", "now", "utcnow", "monotonic", "perf_counter")):
                    readers.setdefault(fn.name, []).append(n.attr)
    assert readers == {"_now": ["time"], "_monotonic": ["monotonic"]}, readers
    all_reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and n.value.id in ("time", "datetime") and n.attr in ("time", "now", "utcnow", "monotonic",
                                                                       "perf_counter")]
    assert len(all_reads) == 2, "nothing outside those two reads a clock"
    assert callable(ge._now) and abs(ge._now() - time.time()) < 5
    assert callable(ge._monotonic) and abs(ge._monotonic() - time.monotonic()) < 5


# ═════════════════════════════════════════════════════════════════════
# A slow log, and one that does not answer
# ═════════════════════════════════════════════════════════════════════
class _Seams:
    """Puts back what a test changes on the module: the time limit, its clock
    and the adapter."""

    def __enter__(self):
        self.saved = (ge._READ_DEADLINE_S, ge._monotonic, ge._api)
        return self

    def __exit__(self, *exc):
        ge._READ_DEADLINE_S, ge._monotonic, ge._api = self.saved


def test_a_scan_that_runs_into_the_evtnext_limit_carries_on_with_the_same_query():
    """EvtNext scans the channel until it has a batch.  A scan that took more
    than 2 s (a cold log seconds after a driver reset, or at login after a
    crash) answered ERROR_TIMEOUT, and the read was thrown away: the one look
    at a game's exit, or at the start after a crash, lost its evidence."""
    m.fresh()
    for n in range(150):
        _nv(T0 + n)
    timeouts = [0]
    asked = []

    def slow_next(query, count, timeout_ms):
        asked.append((query, timeout_ms))
        if len(asked) % 2:                  # every other call runs into the limit, as a slow scan does
            timeouts[0] += 1
            return [], ge.ERROR_TIMEOUT
        return m.EVENTS.EvtNext(query, count, timeout_ms)

    with _Seams():
        ge._api = _Wrapped(EvtNext=slow_next)
        events = _events(kinds=["driver_error"])
        assert [e["ts"] for e in events] == [T0 + n for n in range(150)], "every record, each once"
        assert timeouts[0] >= 3 and len(m.EVENTS.queries) == 1, "the same query carried on"
        assert len({q for q, _t in asked}) == 1
        assert all(0 < t <= ge._NEXT_TIMEOUT_MS for _q, t in asked)
        # and the one-record looks too
        asked[:] = []
        assert ge.any_system_record(T0 - 1, T0 + 1) is True and ge._oldest_system_ts() == T0
    _tidy()


def test_a_read_gives_up_at_its_deadline():
    """A log that keeps running into the limit fails the read after
    _READ_DEADLINE_S, and no EvtNext is given more time than is left."""
    m.fresh()
    _nv(T0)
    clock = [1000.0]
    asked = []

    def tick():
        clock[0] += 1.7
        return clock[0]

    def never(query, count, timeout_ms):
        asked.append(timeout_ms)
        return [], ge.ERROR_TIMEOUT

    with _Seams():
        ge._monotonic = tick
        ge._api = _Wrapped(EvtNext=never)
        r = ge.query(T0 - 10, T0 + 10, kinds=["driver_error"])
        assert r["ok"] is False and r["events"] == [] and "did not answer within 15 s" in r["err"], r
        assert 5 <= len(asked) <= 10 and all(0 < t <= 2000 for t in asked), asked
        assert asked[-1] < 2000, "the last call only got what was left"
        _tidy()
        assert ge.any_system_record(T0 - 10, T0 + 10) is True, "a log nobody could read proves nothing"
        _tidy()

        # both channels share one deadline: a slow System log leaves no time for the Application log
        clock[0] = 0.0

        def slow_next(query, count, timeout_ms):
            clock[0] += 8.0
            return m.EVENTS.EvtNext(query, count, timeout_ms)
        ge._monotonic = lambda: clock[0]
        ge._api = _Wrapped(EvtNext=slow_next)
        first = len(m.EVENTS.queries)
        r = ge.query(T0 - 10, T0 + 10)
        assert r["ok"] is False and r["events"] == [] and "ran out of time" in r["err"] and "Application" in r["err"], r
        assert [q[0] for q in m.EVENTS.queries[first:]] == ["System"], "the Application log was not asked"
        _tidy()


def test_an_evtquery_that_does_not_come_back_fails_the_read_and_is_cleaned_up_later():
    """EvtQuery has no time limit.  An event log service that did not answer
    held the caller (under Adaptive Tuning's session lock: the hand-back at
    the game's exit too) for as long as it did not answer."""
    m.fresh()
    _nv(T0)
    release = threading.Event()
    calls = []

    def stuck_query(channel, xpath, flags):
        calls.append(channel)
        release.wait(10)
        return m.EVENTS.EvtQuery(channel, xpath, flags)

    with _Seams():
        ge._READ_DEADLINE_S = 0.2
        ge._api = _Wrapped(EvtQuery=stuck_query)
        try:
            for n in range(ge._MAX_STUCK_QUERIES):
                t = time.monotonic()
                r = ge.query(T0 - 10, T0 + 10, kinds=["driver_error"])
                assert r["ok"] is False and "did not answer" in r["err"], r
                assert time.monotonic() - t < 3, "the caller was held"
            assert ge._stuck["queries"] == ge._MAX_STUCK_QUERIES
            t = time.monotonic()
            r = ge.query(T0 - 10, T0 + 10, kinds=["driver_error"])
            assert r["ok"] is False and "not answering" in r["err"] and time.monotonic() - t < 0.15, r
            assert len(calls) == ge._MAX_STUCK_QUERIES, "no more threads are started while those are out"
        finally:
            release.set()
        assert m.wait(lambda: ge._stuck["queries"] == 0 and m.EVENTS.open_handles == 0, 5), \
            "the calls that came back late had their handles closed"
        _tidy()
        ge._READ_DEADLINE_S = 15.0
        assert len(_events(T0 - 10, T0 + 10, kinds=["driver_error"])) == 1, "and the log is read again"


def test_the_real_adapter_declares_every_call_with_pointer_sized_handles():
    """Loads wevtapi.dll and looks at the declarations; it calls nothing."""
    m.fresh()
    api = ge.WevtApi()
    for name in ("EvtQuery", "EvtNext", "EvtRender", "EvtClose"):
        assert callable(getattr(api, name))
    dll = api._load()
    assert dll is not None and api._load() is dll
    assert dll.EvtQuery.restype is ctypes.c_void_p, "a handle cut down to an int breaks on 64-bit Windows"
    assert dll.EvtQuery.argtypes[0] is ctypes.c_void_p and len(dll.EvtQuery.argtypes) == 4
    assert dll.EvtNext.argtypes[0] is ctypes.c_void_p and len(dll.EvtNext.argtypes) == 6
    assert dll.EvtRender.argtypes[:2] == [ctypes.c_void_p, ctypes.c_void_p] and len(dll.EvtRender.argtypes) == 7
    assert dll.EvtClose.argtypes == [ctypes.c_void_p]
    assert ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_size_t)
    assert isinstance(ge._api, type(m.EVENTS)), "and the module in this test run is still on the fake"


def test_nothing_a_person_might_read_uses_the_words_we_avoid():
    source = _source()
    with open(os.path.join(m.HERE, "test_gpu_events.py"), encoding="utf-8") as f:
        this_file = f.read()
    assert EM_DASH not in source and EM_DASH not in this_file
    m.fresh()
    _one_of_each()
    texts = [e["detail"] for e in _events()]
    for stage in ("query", "next", "render"):
        m.EVENTS.fail, m.EVENTS.fail_stage = True, stage
        texts.append(ge.query(T0, T0 + 1)["err"])
    texts += [ge.query(None)["err"], ge.query(T0, T0 + 1, kinds=["x"])["err"]]
    assert all(texts)
    for text in texts:
        words = set(text.lower().replace("(", " ").replace(")", " ").replace(".", " ").split())
        assert not words & set(BANNED) and "AT" not in text.split() and EM_DASH not in text, text
