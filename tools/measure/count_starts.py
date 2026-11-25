"""Count the processes the app starts: at start-up, for the first screen, per
idle minute, in a game, and at quit.

Runs entirely inside the test sandbox (tests/sandbox.py): the registry, the
command runners, the process API and the NVIDIA interface are fakes, the data
folder is a throwaway one, and no real process can start (a counting stand-in
replaces subprocess.Popen).  The network is blocked.  app.main() is never
called; the start-up tasks it would start are called one by one.  (The
integrity scan is left out on purpose: it deletes real temporary files, and
the sandbox does not fake the file system.)

    python tools/measure/count_starts.py [idle_seconds] [game_seconds] [--helper]

--helper counts again with the native helper answering (a stand-in for it, as
in the tests), to show what it takes away.  The fake machine answers at once,
so the loops tick at the rate they are written for: the per-minute numbers are
what the code asks for, and a real PC, where every start takes time, gets
slightly fewer.  Takes about four minutes with the default 60-second windows.

The start is counted twice.  The fake machine begins as a PC the app has never
run on, so the first pass includes what only a first start does (one-time
repairs, registry values written for the first time, the user's power plan
switched).  The second pass, "a later start", is every start after that.  It
is made in the same process: what the app keeps in files and in the registry
carries over as it would, and the few things it only remembers while it runs
are reset by hand (see later_start).
"""
import collections
import io
import json
import os
import socket
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_numbers = [float(a) for a in sys.argv[1:] if not a.startswith("-")]
IDLE_S = _numbers[0] if _numbers else 60.0
GAME_S = _numbers[1] if len(_numbers) > 1 else 60.0
HELPER = "--helper" in sys.argv        # count again with the native helper answering (a stand-in for it)
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, REPO)

import sandbox as m                                             # noqa: E402  (first, before any app module)
import subprocess                                               # noqa: E402

# ── counting ──────────────────────────────────────────────────────────
PHASE = ["import"]
EVENTS = []                 # (phase, program, caller, seconds since start)
T0 = time.time()
_SKIP = {"sandbox", "__main__", "subprocess", "core.utils", "threading"}


def _caller() -> str:
    f = sys._getframe(2)
    while f is not None:
        mod = f.f_globals.get("__name__", "?")
        if mod not in _SKIP:
            return f"{mod}.{f.f_code.co_name}"
        f = f.f_back
    return "?"


def _thread() -> str:
    t = threading.current_thread()
    if t is threading.main_thread():
        return "main"
    name = t.name
    return name[name.index("(") + 1:-1] if "(" in name else name


def _note(program: str) -> None:
    EVENTS.append((PHASE[0], program.lower(), _caller(), round(time.time() - T0, 2), _thread()))


# The fake machine, made to look like the owner's: an NVIDIA card whose
# nvidia-smi answers, LibreHardwareMonitor's WMI sensors, a working DPC counter.
_SMI = {"temperature.gpu": "45", "utilization.gpu": "3", "utilization.memory": "1", "clocks.current.graphics": "600",
        "clocks.current.memory": "810", "clocks.max.graphics": "3090", "clocks.max.memory": "14001",
        "clocks.applications.graphics": "[N/A]", "clocks.applications.memory": "[N/A]", "power.draw": "30.00",
        "power.limit": "300.00", "power.default_limit": "300.00", "power.max_limit": "330.00",
        "power.min_limit": "150.00", "fan.speed": "30", "memory.used": "1200", "memory.total": "16303",
        "name": "NVIDIA GeForce RTX 5070 Ti", "driver_version": "621.40", "index": "0", "uuid": "GPU-0",
        "pci.bus_id": "00000000:01:00.0"}
WINDOWED = []               # what the visible-window scan finds: [{"Id", "ProcessName", "WS"}]


def cmd_hook(a):
    exe = os.path.splitext(os.path.basename(a[0]))[0].lower()
    _note(exe + ".exe")
    if exe == "nvidia-smi":
        q = next((x for x in a if x.startswith("--query-gpu=")), None)
        if q:
            return m.R(", ".join(_SMI.get(f.strip(), "0") for f in q.split("=", 1)[1].split(",")))
        return m.R("")
    return None


def ps_hook(command):
    _note("powershell.exe")
    c = command
    if "MainWindowHandle" in c:
        return m.R(json.dumps(WINDOWED) if WINDOWED else "")
    if "root/LibreHardwareMonitor" in c and "Temperature" in c:
        return m.R('{"Value":52.0}')
    if "CurrentClockSpeed" in c and "Select-Object" not in c:
        return m.R("3700")
    if "% DPC Time" in c:
        return m.R('{"dpc_pct":0.12,"isr_pct":0.08,"ok":true}')
    return None


m.CMD_HOOKS.append(cmd_hook)
m.PS_HOOKS.append(ps_hook)

if HELPER:
    # The native helper, as the app sees it: the same fake machine, asked in-process.
    _ticks = [0]

    def _cpu_times():
        _ticks[0] += 1
        return {"total": _ticks[0] * 240_000_000, "idle": _ticks[0] * 200_000_000, "dpc": _ticks[0] * 240_000,
                "interrupt": _ticks[0] * 24_000, "processors": 24}
    m.NATIVE.update({
        "windowed_processes": lambda: [{"pid": w["Id"], "name": w["ProcessName"], "ws": w["WS"] * 1048576} for w in WINDOWED],
        "gpu_live": {"temp_c": 45, "gpu_util_pct": 3, "mem_util_pct": 1, "core_mhz": 600, "mem_mhz": 810,
                     "core_max_mhz": 3090, "mem_max_mhz": 14001, "core_app_mhz": None, "mem_app_mhz": None,
                     "power_mw": 30000, "power_limit_mw": 300000, "fan_pct": 30, "mem_used_bytes": 1200 * 1048576,
                     "mem_total_bytes": 16303 * 1048576},
        "cpu_clock": {"mhz": 3700},
        "cpu_times": _cpu_times,
        "ping": {"ok": True, "ms": 11},
        "process_io": [],
    })

    # The start-up questions (step 2): what WMI says about this fake PC, the
    # card's name, and the power plans and services of the same fake machine
    # that powercfg and sc read (m.native_machine).
    _GIB = 1024 ** 3
    _WMI = {
        "win32_operatingsystem": [{"Caption": "Microsoft Windows 11 Home", "Version": "10.0.26300",
                                   "BuildNumber": "26300", "OSArchitecture": "64-bit",
                                   "TotalVisibleMemorySize": 66_900_000, "FreePhysicalMemory": 40_140_000}],
        "win32_processor": [{"Name": "Intel(R) Core(TM) Ultra 9 285K", "NumberOfCores": 24,
                             "NumberOfLogicalProcessors": 24, "MaxClockSpeed": 3700, "CurrentClockSpeed": 3700}],
        "win32_physicalmemory": [{"Capacity": 32 * _GIB, "Speed": 6000, "ConfiguredClockSpeed": 5600}] * 2,
        "win32_logicaldisk": [{"DeviceID": "C:", "Size": 1999 * _GIB, "FreeSpace": 512 * _GIB}],
        "win32_videocontroller": [{"Name": "NVIDIA GeForce RTX 5070 Ti", "DriverVersion": "32.0.15.2140",
                                   "AdapterRAM": 4293918720, "PNPDeviceID": "PCI\\VEN_10DE&DEV_2C05",
                                   "Status": "OK"}],
        "msft_dnsclientserveraddress": [{"InterfaceAlias": "Ethernet", "ServerAddresses": ["1.1.1.1", "1.0.0.1"]}],
        "msft_physicaldisk": [{"MediaType": 4, "BusType": 17}],
    }

    def _wmi(namespace="", query=""):
        words = query.split()
        cls = words[words.index("FROM") + 1].lower() if "FROM" in words else ""
        if cls in _WMI:
            return _WMI[cls]
        if cls == "sensor" and "librehardwaremonitor" in namespace.lower():
            return [{"Name": "CPU Package", "SensorType": "Temperature", "Value": 52.0}]
        return []           # (LibreHardwareMonitor's namespace list included: it is not installed here)

    m.native_machine()
    m.NATIVE.update({
        "wmi": _wmi,
        "gpu_identity": {"name": _SMI["name"], "driver_version": _SMI["driver_version"],
                         "mem_total_bytes": int(_SMI["memory.total"]) * 1048576},
    })
    # The two memory steps of the start-up preparation: the helper's actions.
    m.NATIVE_ACTS.update({"trim_working_sets": {"processes": 300, "trimmed": 250, "debug_privilege": True},
                          "purge_standby_list": {"purged": True}})


class CountingPopen:
    """Stands in for subprocess.Popen: records the command, starts nothing,
    answers like a command that printed nothing and exited 0."""
    _pid = [50000]

    def __init__(self, args, *a, **k):
        self.args = args
        first = args if isinstance(args, str) else (args[0] if args else "?")
        _note(os.path.basename(str(first).split(" ")[0].strip('"')) or "?")
        self._text = bool(k.get("text") or k.get("universal_newlines") or k.get("encoding") or k.get("errors"))
        empty = "" if self._text else b""
        mk = io.StringIO if self._text else io.BytesIO
        self.stdout = mk(empty) if k.get("stdout") == subprocess.PIPE else None
        self.stderr = mk(empty) if k.get("stderr") == subprocess.PIPE else None
        self.stdin = mk() if k.get("stdin") == subprocess.PIPE else None
        self.returncode = 0
        CountingPopen._pid[0] += 4
        self.pid = CountingPopen._pid[0]

    def communicate(self, input=None, timeout=None):
        empty = "" if self._text else b""
        return (empty if self.stdout is not None else None, empty if self.stderr is not None else None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass

    terminate = send_signal = kill

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


subprocess.Popen = CountingPopen


def _no_network(*a, **k):
    raise OSError("the network is blocked in this measurement")


socket.create_connection = _no_network
_real_connect = socket.socket.connect
socket.socket.connect = lambda self, *a, **k: _no_network()

# ── the app ───────────────────────────────────────────────────────────
import app as A                                                 # noqa: E402
from core import (adaptive_tuning, auto_recover, autostart, background_pauser, boot_prep, capabilities, clipper,    # noqa: E402
                  community, debloater, dependencies, driver_updater, game_profiles, gpu_overclock, hidhide_cleanup,
                  hw_monitor, idle_mode, privacy, startup_manager, temp_ceiling, tournament_mode, updater)

for name in ("core.competitive_latency", "core.timer_manager", "core.privacy", "core.gpu_events"):
    m.app(name)
from core import competitive_latency                            # noqa: E402

A._set_responsive_mode = lambda: None       # (would change this script's own priority; starts no process)
A._set_efficiency_mode = lambda: None
game_profiles._notifier = None
FAILED = []


def step(label, fn):
    print(f"[{time.time()-T0:6.1f}s] {PHASE[0][:28]} | {label}", file=sys.stderr, flush=True)
    try:
        return fn()
    except BaseException as e:                                  # a refused os.startfile is an AssertionError
        FAILED.append((PHASE[0], label, f"{type(e).__name__}: {e}"[:160]))
        return None


def settle(seconds=1.0, quiet=0.6, cap=None):
    """Wait until no new process start has been counted for a moment, but
    never longer than `cap` (in a game the app never goes quiet)."""
    hard = time.time() + (cap if cap is not None else seconds * 3 + 5)
    end = time.time() + seconds
    last = len(EVENTS)
    mark = time.time()
    while time.time() < min(end, hard):
        time.sleep(0.05)
        if len(EVENTS) != last:
            last, mark = len(EVENTS), time.time()
            end = max(end, mark + quiet)
        elif time.time() - mark > quiet:
            break


client = A.app.test_client()
HDR = {"Host": f"127.0.0.1:{A.config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}


def start_up(which: str) -> None:
    """Everything a start does, in main()'s order: before the window, the
    background tasks, the first screen's data.  `which` names the pass."""
    # ── 1. start-up, before the window ────────────────────────────────
    PHASE[0] = f"1{which} start-up, before the window opens"
    step("post-install state", updater.check_post_install_state)
    step("mic restore", privacy.restore_microphone_access)
    step("store restore", privacy.restore_store_updates)
    step("game bar block", debloater.block_gamebar_protocols)
    step("adaptive recover", adaptive_tuning.recover_interrupted_session)
    step("game mode boot restore", game_profiles.check_and_restore_on_boot)
    step("adaptive orphan", adaptive_tuning.clear_runtime_if_orphaned)
    step("pauser orphans", background_pauser.recover_orphans)
    step("tournament stale", tournament_mode.recover_if_stale)
    step("webview gpu args", capabilities.webview2_gpu_args)
    step("capabilities", capabilities.get)
    settle()

    # ── 2. start-up, background tasks ─────────────────────────────────
    PHASE[0] = f"2{which} start-up, background tasks"
    step("autostart migrate", autostart.migrate_from_legacy_runkey)
    step("update check", updater.check_update_background)
    done = threading.Event()
    step("boot prep", lambda: boot_prep.prep_for_gaming_async(on_done=lambda d: done.set()))
    done.wait(20)
    step("saved overclock", gpu_overclock.apply_saved_profile_on_startup)
    step("saved overclock done", gpu_overclock.mark_startup_apply_done)
    step("driver check", driver_updater.detect_gpu)     # (what check_in_background's first check asks the machine)
    step("competitive latency", competitive_latency.apply_on_boot)
    step("startup manager", startup_manager.run_on_boot)
    step("idle mode", idle_mode.run_on_boot)
    step("hidhide cleanup", hidhide_cleanup.run_once)
    step("dependency check", dependencies.check_on_boot)
    step("community games", community.merge_into_known_games_async)
    step("clip settings", clipper.get_settings)
    step("find ffmpeg", clipper._find_ffmpeg)
    step("auto recover", auto_recover.recover_legacy_bad_tweaks)
    step("install info", updater.write_install_info)
    step("updater bootstrap", updater.ensure_updater_up_to_date_async)
    settle(6.0, 1.5)

    # ── 3. the first screen ───────────────────────────────────────────
    PHASE[0] = f"3{which} first screen (dashboard data)"
    for route in ("/api/dashboard", "/api/notifier/status", "/api/boot-prep/status"):
        step(route, lambda r=route: client.get(r, headers=HDR))
    settle()


def later_start() -> None:
    """Make the next start_up() a start of the app on a PC it has run on
    before.  Files and the registry carry over by themselves; this resets what
    the app only remembers while it runs, which a new process would not have:
    the start-up preparation's "done", the capability check's answer, and an
    FFmpeg found on PATH."""
    PHASE[0] = "(between two starts)"
    settle(2.0, 1.0)
    boot_prep._state.update(running=False, done=False, steps=[], started_at=0, finished_at=0)
    capabilities._cache = None
    if hasattr(dependencies, "_ffmpeg_on_path"):
        dependencies._ffmpeg_on_path = None


start_up(" FIRST start on this PC:")
later_start()
start_up(" a LATER start:")

# ── 4. one idle minute: monitors on, no game ──────────────────────────
PHASE[0] = "(warm-up)"
if temp_ceiling.get_settings().get("enabled"):
    step("temp ceiling", temp_ceiling.start_watchdog)
step("game monitor", game_profiles.auto_enable_on_boot_if_set)
time.sleep(4.0)
PHASE[0] = "4 idle, per minute (defaults: game monitor + temperature ceiling)"
n0 = len(EVENTS)
time.sleep(IDLE_S)
idle_default = len(EVENTS) - n0

PHASE[0] = "(warm-up)"
step("idle mode on", lambda: idle_mode.set_settings(enabled=True) if hasattr(idle_mode, "set_settings") else None)
step("idle mode arm", idle_mode.run_on_boot)
time.sleep(1.0)
PHASE[0] = "5 idle, per minute (owner's switches: + Idle Mode)"
n0 = len(EVENTS)
time.sleep(IDLE_S)
idle_owner = len(EVENTS) - n0

# ── 5. quit at idle ───────────────────────────────────────────────────
# (the game monitor is restarted afterwards: quitting stops it)
PHASE[0] = "6 quit, no game running"
step("quit at idle", lambda: A._restore_system_before_exit("measurement"))
settle(3.0, 1.0)

# ── 6. the Monitor page: passes of the hardware monitor ───────────────
PHASE[0] = "7 Monitor page open, per pass of its loop"
samples = []
hw_monitor._read_cpu_load_via_ntapi()          # (the loop's first pass has run: there is a previous reading)
for _ in range(4):
    time.sleep(1.0)
    before = [e for e in EVENTS if e[4] == "main"]
    hw_monitor._sample_cpu(); hw_monitor._sample_gpu(); hw_monitor._sample_ram()
    hw_monitor._sample_dpc(); hw_monitor._sample_ping("1.1.1.1")
    samples.append(len([e for e in EVENTS if e[4] == "main"]) - len(before))
monitor_per_pass = samples[-1]

# ── 7. a game: start, one minute, close ───────────────────────────────
PHASE[0] = "(warm-up)"
step("game monitor again", game_profiles.start_monitoring)
time.sleep(4.0)
m.proc(4242, exe="cod.exe", image=r"C:\Games\cod\cod.exe")
for pid, exe in ((7001, "chrome.exe"), (7002, "discord.exe"), (7003, "explorer.exe"), (7004, "steam.exe")):
    m.proc(pid, exe=exe)
if "cod.exe" not in game_profiles.KNOWN_GAME_EXES:
    game_profiles.KNOWN_GAME_EXES.add("cod.exe")
PHASE[0] = "8 a game starts (game mode applied)"
WINDOWED.append({"Id": 4242, "ProcessName": "cod", "WS": 6020})
ok = m.wait(lambda: game_profiles._engine.get("gaming_mode"), 12)
time.sleep(8.0)
game_started = bool(ok)
PHASE[0] = "9 in a game, per minute"
n0 = len(EVENTS)
time.sleep(GAME_S)
in_game = len(EVENTS) - n0
PHASE[0] = "10 quit while a game is running"
step("quit in game", lambda: A._restore_system_before_exit("measurement"))
settle(6.0, 2.0, cap=15)

# ── report ────────────────────────────────────────────────────────────
by_phase = collections.OrderedDict()
for phase, program, caller, _t, thread in EVENTS:
    if phase.startswith("("):
        continue
    d = by_phase.setdefault(phase, {"total": 0, "by_program": collections.Counter(), "by_caller": collections.Counter(),
                                    "by_thread": collections.Counter()})
    d["total"] += 1
    d["by_program"][program] += 1
    d["by_caller"][caller] += 1
    d["by_thread"][thread] += 1
report = {"native_helper": HELPER, "python": sys.version.split()[0], "idle_window_s": IDLE_S, "game_window_s": GAME_S,
          "game_mode_came_on": game_started, "monitor_starts_per_pass": samples, "phases": {}}
for phase, d in by_phase.items():
    scale = 60.0 / IDLE_S if phase.startswith(("4 ", "5 ")) else (60.0 / GAME_S if phase.startswith("9 ") else 1.0)
    report["phases"][phase] = {
        "process_starts": round(d["total"] * scale, 1),
        "by_program": {k: round(v * scale, 1) for k, v in d["by_program"].most_common()},
        "by_caller": {k: round(v * scale, 1) for k, v in d["by_caller"].most_common(14)},
        "by_thread": {k: round(v * scale, 1) for k, v in d["by_thread"].most_common(8)},
    }
report["steps_that_raised"] = FAILED
print(json.dumps(report, indent=1))
sys.stdout.flush()
os._exit(0)
