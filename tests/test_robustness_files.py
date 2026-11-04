import sandbox as m
# Robustness of the cleaners, scanners and monitors: core/cleaner.py,
# core/disk_analyzer.py, core/integrity_scan.py, core/library_scanner.py,
# core/hw_monitor.py, core/nvidia_clean.py and core/vault.py.
#
# Everything these tests clean or scan is made under m.ROOT.  TEMP and TMP are
# not redirected by the sandbox, so any test that reaches code reading them
# points them at a sandbox folder first and puts them back after.

import gc
import importlib
import json
import os
import threading
import time
import unittest

import config

CL = m.app("core.cleaner")
DA = m.app("core.disk_analyzer")
IS = m.app("core.integrity_scan")
LS = m.app("core.library_scanner")
HW = m.app("core.hw_monitor")
NC = m.app("core.nvidia_clean")


def _tree(name, files):
    root = os.path.join(m.ROOT, name)
    os.makedirs(root, exist_ok=True)
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


def _junction(target, link):
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
    except (ImportError, AttributeError, OSError) as e:
        raise unittest.SkipTest(f"this Python cannot create a junction: {e}")


class _Env:
    """Set environment variables for the length of a `with` block."""

    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ── cleaner ───────────────────────────────────────────────────────────
def test_cleaner_will_not_empty_a_drive_root_or_a_folder_holding_the_profile():
    """TEMP set to the wrong place handed these to _safe_rmtree, which emptied them.
    (Only the check itself is asked about a real drive root.)"""
    profile = os.environ["USERPROFILE"]
    assert profile.startswith(m.ROOT) and config.APPDATA_DIR.startswith(m.ROOT)
    for broad in ("C:\\", "D:\\", "c:", profile, os.environ["LOCALAPPDATA"], config.APPDATA_DIR,
                  os.path.dirname(config.APPDATA_DIR), m.ROOT):
        assert CL._too_broad_to_clean(broad), broad
    temp = _tree("temp_c", {"a.tmp": "x", "sub/b.log": "yy"})
    assert not CL._too_broad_to_clean(temp)
    assert not CL._too_broad_to_clean(os.path.join(os.environ["LOCALAPPDATA"], "Temp"))

    keep = os.path.join(profile, "Documents", "thesis.docx")
    os.makedirs(os.path.dirname(keep), exist_ok=True)
    with open(keep, "w", encoding="utf-8") as f:
        f.write("years of work")
    assert CL._safe_rmtree(profile) == (0, 0)
    assert os.path.exists(keep)
    assert CL._safe_rmtree(temp) == (2, 3), "an ordinary temp folder is still cleaned"


def test_temp_scan_counts_only_what_the_clean_can_remove():
    precious = _tree("big_game", {"data.pak": "x" * 1000})
    temp = _tree("temp_d", {"old.tmp": "xx", "_MEI555/base_library.zip": "y" * 100})
    link = os.path.join(temp, "link")
    _junction(precious, link)
    try:
        saved = CL._get_clean_locations
        CL._get_clean_locations = lambda: [{"name": "User Temp", "path": temp, "cat": "System"}]
        try:
            rows = CL.scan_temp_sizes()
        finally:
            CL._get_clean_locations = saved
        assert (rows[0]["files"], rows[0]["size"]) == (1, 2), \
            "the junction's target and the running app's runtime are never freed by the clean"
        assert CL._measure_dir(temp) == 2 and CL._count_files(temp) == 1, "the junk scan's walkers skip the same"
    finally:
        os.rmdir(link)                                  # the junction, not its target
    assert os.path.exists(os.path.join(precious, "data.pak"))


class _DriveQ:
    """`os` as core.cleaner sees it, with a sandbox folder standing in for
    the root of drive Q:."""

    def __init__(self, root):
        self.root = root

    def __getattr__(self, name):
        return getattr(os, name)

    def scandir(self, p):
        if p == "Q:\\":
            return os.scandir(self.root)
        if len(p) <= 3 and p[1:2] == ":":
            raise AssertionError(f"the test reached a real drive: {p}")
        return os.scandir(p)


def test_disk_junk_scan_and_clean_agree_and_neither_follows_a_junction():
    drive = _tree("drive_q", {"Templates/report.log": "x" * 50, "Templates/keep.docx": "x",
                              "temp/a.tmp": "xx", "old.bak": "xxx"})
    docs = _tree("docs_q", {"thesis.docx.bak": "years", "notes.log": "x"})
    _junction(docs, os.path.join(drive, "work"))        # D:\Work -> a folder inside Documents
    saved = CL.os
    CL.os = _DriveQ(drive)
    try:
        found = {r["name"]: (r["files"], r["size"]) for r in CL.scan_disk_junk("Q:")}
        assert found == {"Junk in Templates": (1, 50), "Root junk files": (1, 3), "Temp dir: temp": (1, 2)}, found
        out = CL.clean_disk_junk(["Q:"])
    finally:
        CL.os = saved
        os.rmdir(os.path.join(drive, "work"))
    assert (out["total_files"], out["total_freed"]) == (3, 55), out
    assert sorted(os.listdir(docs)) == ["notes.log", "thesis.docx.bak"], "deleted through the junction"
    assert os.path.exists(os.path.join(drive, "Templates", "keep.docx"))


class _PathAllDirs:
    """os.path for core.cleaner in which every folder exists."""

    def __getattr__(self, name):
        return getattr(os.path, name)

    @staticmethod
    def isdir(p):
        return bool(p)


class _OsAllDirs:
    """`os` for core.cleaner that pretends every folder exists and refuses
    to walk or delete anything."""
    path = _PathAllDirs()

    def __getattr__(self, name):
        if name in ("walk", "remove", "unlink", "rmdir", "scandir"):
            raise AssertionError(f"the test reached os.{name}")
        return getattr(os, name)


def test_disk_junk_scan_lists_exactly_the_folders_the_clean_empties():
    """The scan's "reclaimable" counted browser, Discord, Teams, Spotify,
    Steam-dump, thumbnail and installer caches the clean never touches, and
    the user's installed fonts as a "Font Cache"."""
    temp = _tree("temp_f", {})
    cleaned, scanned, cats = [], [], {}
    saved = (CL.os, CL._safe_rmtree, CL._measure_dir, CL._count_files)
    CL.os = _OsAllDirs()
    CL._safe_rmtree = lambda p: cleaned.append(p) or (0, 0)        # records, deletes nothing
    CL._measure_dir = lambda p: scanned.append(p) or 1
    CL._count_files = lambda p: 1
    try:
        with _Env(TEMP=temp):
            assert CL._safe_rmtree is not saved[1]
            CL._clean_known_junk_dirs()
            CL._scan_known_junk_dirs(cats)
    finally:
        CL.os, CL._safe_rmtree, CL._measure_dir, CL._count_files = saved
    assert len(cleaned) == 10 and sorted(scanned) == sorted(cleaned), (scanned, cleaned)
    fonts = os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts")
    assert fonts not in scanned and "Font Cache" not in cats


def test_ram_clean_switches_on_the_privilege_the_standby_purge_needs():
    """An elevated token holds SeProfileSingleProcessPrivilege switched off,
    and the purge failed every time without it.  (Checked as text: nothing
    is run for real here.)"""
    m.fresh()
    assert CL.run_ps is m.run_ps
    CL.clean_ram()
    purge = [c for c in m.PSC if "NtSetSystemInformation" in c]
    assert len(purge) == 1, purge
    go = purge[0][purge[0].index("bool Go()"):]
    assert "RtlAdjustPrivilege(13,true," in go
    assert go.index("RtlAdjustPrivilege(") < go.index("NtSetSystemInformation(80,"), "switched on before the purge"


# ── disk analyzer ─────────────────────────────────────────────────────
def test_disk_scan_counts_a_junctioned_tree_once():
    """Every C: has "Documents and Settings" -> C:\\Users at its root."""
    root = _tree("scan_root", {"Users/a.bin": "x" * 1000, "top.txt": "y" * 10})
    link = os.path.join(root, "Documents and Settings")
    _junction(os.path.join(root, "Users"), link)
    try:
        res = DA._scan_via_scandir(root, threading.Event(), DA._new_state(root))
    finally:
        os.rmdir(link)
    assert (res["total_bytes"], res["total_files"]) == (1010, 2), (res["total_bytes"], res["total_files"])


def test_a_cancelled_scan_winding_down_leaves_the_next_scan_alone():
    root = _tree("scan_root2", {"d/f.bin": "x"})
    old, new = DA._new_state(root), DA._new_state(root)
    old["state"] = "cancelling"
    saved = DA._active_state
    DA._active_state = new                          # the user already started the next scan
    try:
        stop = threading.Event()
        stop.set()
        DA._run_scan_worker(root, stop, old)
    finally:
        DA._active_state = saved
    assert old["state"] == "cancelled"
    assert new["state"] == "running" and new["progress"]["files"] == 0, new


def test_scan_progress_moves_while_the_scan_runs():
    root = _tree("scan_root3", {f"many/f{i}.txt": "x" for i in range(1100)})
    state = DA._new_state(root)
    DA._walk_subdir(os.path.join(root, "many"), threading.Event(), None, state)
    assert state["progress"]["files"] >= 1000 and state["progress"]["bytes"] >= 1000, state["progress"]


def test_disk_scan_end_to_end():
    root = _tree("scan_root4", {"a/b/c.bin": "x" * 300, "a/d.bin": "x" * 200, "e.bin": "x" * 5})
    r = DA.start_scan(root)
    assert r["ok"], r
    assert m.wait(lambda: DA.get_status()["state"] not in ("running", "cancelling"), 30)
    assert DA.get_status()["state"] == "done", DA.get_status()
    res = DA.get_result()
    assert res["ok"] and res["total_bytes"] == 505 and res["total_files"] == 3, res


# ── integrity scan ────────────────────────────────────────────────────
def test_integrity_temp_sweep_skips_a_temp_that_holds_the_profile():
    m.fresh()
    profile = os.environ["USERPROFILE"]
    assert profile.startswith(m.ROOT)
    old = os.path.join(profile, "Documents", "old.docx")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, "w", encoding="utf-8") as f:
        f.write("x")
    month_ago = time.time() - 30 * 86400
    os.utime(old, (month_ago, month_ago))
    with _Env(TEMP=profile, TMP=profile):
        out = IS._phase_b_temp_cleanup(time.time() + 60, {"clean_windows_temp": True})
    assert os.path.exists(old), "a week-old file of the user's was deleted"
    assert out["skipped"].get("too_broad_blocked") and os.path.realpath(profile) not in out["dirs_scanned"], out

    assert IS._too_broad_for_temp("C:\\") and IS._too_broad_for_temp("D:\\")
    assert not IS._too_broad_for_temp(os.path.join(os.environ["LOCALAPPDATA"], "Temp"))
    temp = _tree("temp_e", {"stale.tmp": "z"})
    os.utime(os.path.join(temp, "stale.tmp"), (month_ago, month_ago))
    with _Env(TEMP=temp, TMP=temp):
        out = IS._phase_b_temp_cleanup(time.time() + 60, {"clean_windows_temp": True})
    assert not os.path.exists(os.path.join(temp, "stale.tmp")) and out["bytes_freed"] >= 1, \
        "an ordinary temp folder is still swept"


def test_health_probe_needs_no_wmic_and_no_english_event_level_names():
    calls = []

    class _Done:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake_run(args, **kw):
        calls.append(list(args))
        text = " ".join(args)
        if "Win32_DiskDrive" in text:
            return _Done("PC,Samsung SSD 990 PRO 2TB,OK\nPC,WDC WD10EZEX-08WN4A0,Pred Fail\n")
        if "Get-WinEvent" in text:
            return _Done("12|Disk\n3|Service Control Manager\n")
        raise FileNotFoundError(args[0])            # wmic.exe is not on current Windows 11

    saved = IS.subprocess.run
    IS.subprocess.run = fake_run
    try:
        c = IS._phase_c_health_probe(time.time() + 60)
    finally:
        IS.subprocess.run = saved
    assert not [a for a in calls if a[0].lower().startswith("wmic")]
    events = next(a for a in calls if "Get-WinEvent" in " ".join(a))
    assert "LevelDisplayName" not in " ".join(events), "the display name is translated on a non-English Windows"
    kinds = {f["kind"]: f for f in c["findings"]}
    assert kinds["smart_warning"]["drives"] == [{"model": "WDC WD10EZEX-08WN4A0", "status": "Pred Fail"}]
    assert kinds["service_errors"]["groups"] == [{"provider": "Disk", "count": 12}]


# ── library scanner ───────────────────────────────────────────────────
def _manifest(lib, appid, name):
    with open(os.path.join(lib, f"appmanifest_{appid}.acf"), "w", encoding="utf-8") as f:
        f.write(f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"installdir"\t\t"{name}"\n}}\n')
    os.makedirs(os.path.join(lib, "common", name), exist_ok=True)


def test_steam_main_library_once_and_a_library_with_brackets_in_its_path():
    m.fresh()
    steam = os.path.join(m.ROOT, "Steam")
    second = os.path.join(m.ROOT, "Games [SSD]", "SteamLibrary")
    for lib in (os.path.join(steam, "steamapps"), os.path.join(second, "steamapps")):
        os.makedirs(lib, exist_ok=True)
    _manifest(os.path.join(steam, "steamapps"), 100, "Alpha")
    _manifest(os.path.join(second, "steamapps"), 200, "Beta")
    vdf = '"libraryfolders"\n{\n' + "".join(
        f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{p.replace(chr(92), chr(92) * 2)}"\n\t}}\n'
        for i, p in enumerate((steam, second))) + "}\n"
    with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8") as f:
        f.write(vdf)
    # Steam keeps SteamPath in lower case with forward slashes.
    m.rset(r"HKCU\SOFTWARE\Valve\Steam", "SteamPath", steam.lower().replace("\\", "/"))
    titles = sorted(g["title"] for g in LS.scan_steam())
    assert titles == ["Alpha", "Beta"], titles


def test_ubisoft_title_comes_from_its_forward_slash_install_dir():
    m.fresh()
    game = os.path.join(m.ROOT, "Ubisoft", "Rainbow Six Siege")
    os.makedirs(game, exist_ok=True)
    m.rset(r"HKLM\SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs\635", "InstallDir",
           game.replace("\\", "/") + "/")
    assert [g["title"] for g in LS.scan_ubisoft()] == ["Rainbow Six Siege"]


def test_epic_game_on_a_drive_that_is_gone_is_not_listed():
    m.fresh()
    pd = os.path.join(m.ROOT, "ProgramData")
    manifests = os.path.join(pd, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    os.makedirs(manifests, exist_ok=True)
    here = os.path.join(m.ROOT, "Epic Games", "Here")
    os.makedirs(here, exist_ok=True)
    for name, path in (("Here", here), ("Gone", os.path.join(m.ROOT, "unplugged", "Gone"))):
        with open(os.path.join(manifests, f"{name}.item"), "w", encoding="utf-8") as f:
            json.dump({"DisplayName": name, "InstallLocation": path, "LaunchExecutable": "x.exe"}, f)
    with _Env(PROGRAMDATA=pd):
        assert [g["title"] for g in LS.scan_epic()] == ["Here"]


# ── hardware monitor ──────────────────────────────────────────────────
def test_a_nan_sensor_reading_never_reaches_the_snapshot():
    m.fresh()
    m.PS_HOOKS.append(lambda c: m.R(
        '[{"Name":"GPU Core","SensorType":"Temperature","Value":NaN,"Identifier":"/gpu-nvidia/0/temperature/0"},'
        '{"Name":"GPU Hot Spot","SensorType":"Temperature","Value":61.5,"Identifier":"/gpu-nvidia/0/temperature/2"},'
        '{"Name":"GPU Core","SensorType":"Load","Value":NaN,"Identifier":"/gpu-nvidia/0/load/0"}]')
        if "LibreHardwareMonitor" in c and "gpu" in c else None)
    m.PS_HOOKS.append(lambda c: m.R('{"dpc_pct":NaN,"isr_pct":0.1,"ok":true}') if "DPC Time" in c else None)
    m.PS_HOOKS.append(lambda c: m.R("NaN") if "% Processor Time" in c else None)

    gpu = HW._read_gpu_via_lhm()
    assert gpu["temp"] == 61.5 and gpu["load"] is None, gpu
    assert HW._sample_dpc()["dpc_pct"] is None
    HW._cpu_prev_sample = {"ts": 0.0, "idle": 0, "kernel": 0, "user": 0}     # first sample: Get-Counter
    assert HW._sample_cpu()["load"] is None
    json.dumps([gpu, HW._sample_dpc()], allow_nan=False)


def test_ping_time_is_read_on_a_non_english_windows():
    m.fresh()
    for line, want in (("Reply from 1.1.1.1: bytes=32 time=9ms TTL=57", 9),
                       ("Antwort von 1.1.1.1: Bytes=32 Zeit=12ms TTL=57", 12),
                       ("Antwort von 1.1.1.1: Bytes=32 Zeit<1ms TTL=57", 1),
                       ("R\u00e9ponse de 1.1.1.1\u00a0: octets=32 temps=14 ms TTL=57", 14),
                       ("1.1.1.1 \u304b\u3089\u306e\u5fdc\u7b54: \u30d0\u30a4\u30c8\u6570 =32 \u6642\u9593 =21ms TTL=57", 21)):
        out = f"\nPinging 1.1.1.1 with 32 bytes of data:\n{line}\n\nPackets: Sent = 1, Received = 1, Lost = 0\n"
        m.CMD_HOOKS[:] = [lambda a, out=out: m.R(out) if a[0] == "ping" else None]
        assert HW._sample_ping("1.1.1.1") == want, line
    m.CMD_HOOKS[:] = [lambda a: m.R("\nPing wird ausgef\u00fchrt\nZeit\u00fcberschreitung der Anforderung.\n")
                      if a[0] == "ping" else None]
    assert HW._sample_ping("1.1.1.1") is None


def test_nvidia_smi_with_two_cards():
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.R("45, 30, 1800, 2048, 8192\n38, 0, 210, 100, 4096") if a[0] == "nvidia-smi" else None)
    g = HW._sample_gpu()
    assert (g["temp"], g["load"], g["clock"], g["vram_pct"]) == (45, 30, 1800, 25.0), g
    m.CMD_HOOKS[:] = [lambda a: m.R("NVIDIA GeForce RTX 4090, 560.94, 24564\nNVIDIA GeForce RTX 3060, 560.94, 12288")
                      if a[0] == "nvidia-smi" else None]
    d = NC.detect_gpu()
    assert (d["vendor"], d["name"], d["vram_mb"]) == ("nvidia", "NVIDIA GeForce RTX 4090", 24564), d


def test_stop_then_start_leaves_one_monitor_loop():
    m.fresh()
    names = ("_sample_cpu", "_sample_gpu", "_sample_ram", "_sample_dpc", "_sample_ping")
    saved = {n: getattr(HW, n) for n in names}
    HW._sample_cpu = lambda: {"temp": None, "load": 5.0, "clock": None}
    HW._sample_gpu = lambda: {"temp": None, "load": None, "clock": None, "vram_pct": None}
    HW._sample_ram = lambda: {"used_pct": 10, "standby_mb": 0}
    HW._sample_dpc = lambda: {"dpc_pct": None, "isr_pct": None, "dpc_rate": None, "ok": False, "perfmon_ok": False}
    HW._sample_ping = lambda target="1.1.1.1": None
    HW._monitor_state["latest"] = {}
    second = None
    try:
        HW.start_monitor()
        first = HW._monitor_state["thread"]
        assert m.wait(lambda: HW._monitor_state["latest"]), "the loop never sampled"
        HW.stop_monitor()
        HW.start_monitor()                          # within the first loop's one-second sleep
        second = HW._monitor_state["thread"]
        assert second is not first
        assert m.wait(lambda: not first.is_alive(), 4), "the first loop carried on beside the second"
        assert second.is_alive()
    finally:
        HW.stop_monitor()
        if second is not None:
            m.wait(lambda: not second.is_alive(), 4)
        for n, f in saved.items():
            setattr(HW, n, f)


# ── GPU tab ───────────────────────────────────────────────────────────
def test_clean_install_script_removes_only_nvidia_driver_packages():
    m.fresh()
    saved = NC.detect_gpu
    NC.detect_gpu = lambda: {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 5070 Ti", "driver": "1", "vram_mb": 16000}
    try:
        out = NC.prepare_clean_install()
    finally:
        NC.detect_gpu = saved
    assert out["ok"] and out["script_path"].startswith(m.ROOT)
    with open(out["script_path"], encoding="ascii") as f:
        script = f.read()
    deleting = [ln for ln in script.splitlines() if "/delete-driver" in ln]
    assert deleting and all("ProviderName -like '*NVIDIA*'" in ln for ln in deleting), deleting
    assert "Published Name" not in script, "that loop fed every third-party driver on the PC to pnputil"


def test_msi_interrupt_priority_row_reports_a_failed_write():
    m.fresh()
    key = (r"HKLM\SYSTEM\CurrentControlSet\Enum\PCI\VEN_10DE&DEV_2C05\GPU0\Device Parameters"
           r"\Interrupt Management\Affinity Policy")
    m.REG_FAIL.add((key.lower(), "devicepriority"))
    rows = {r["name"]: r["ok"] for r in NC._apply_msi_mode("NVIDIA")}
    assert rows == {"NVIDIA GeForce RTX 5070 Ti MSI Mode": True,
                    "NVIDIA GeForce RTX 5070 Ti Interrupt Priority": False}, rows


def _class_key(index):
    return m.DISPLAY_CLASS + "\\" + index


def _amd_machine(adapters):
    """`adapters`: [(display-class index, DriverDesc)]."""
    m.fresh()
    m.PS_HOOKS.append(lambda c: m.R("\n".join(f"{i}|{d}" for i, d in adapters)) if "DriverDesc" in c else None)
    for i, _ in adapters:
        m.make_key(_class_key(i))
    for t in NC._AMD_TASKS:
        m.task_add(t)


def _amd_tweaks():
    saved = NC.detect_gpu
    NC.detect_gpu = lambda: {"vendor": "amd", "name": "AMD Radeon RX 7900 XTX", "driver": "1", "vram_mb": 24576}
    try:
        return NC.apply_amd_tweaks()
    finally:
        NC.detect_gpu = saved


def test_amd_tweaks_go_only_to_amd_adapters_and_each_row_reports_its_writes():
    adapters = [("0000", "Intel(R) UHD Graphics 770"), ("0001", "Meta Virtual Monitor"),
                ("0002", "AMD Radeon RX 7900 XTX"), ("0003", "Radeon (TM) RX 480 Graphics")]
    _amd_machine(adapters)
    m.REG_FAIL.add((_class_key("0003").lower(), "kmd_frtenabled"))
    m.TASKS.pop(m._task_key(NC._AMD_TASKS[0]))              # a task this driver version does not install
    rows = {r["name"]: r["ok"] for r in _amd_tweaks()}
    assert m.rdata(_class_key("0002"), "EnableUlps") == 0 and m.rdata(_class_key("0003"), "EnableUlps") == 0
    assert m.rget(_class_key("0000"), "EnableUlps") is None and m.rget(_class_key("0001"), "EnableUlps") is None, \
        "written to the Intel iGPU's or the virtual monitor's key"
    assert rows["AMD PowerPlay tuned: 0002"] is True and rows["AMD PowerPlay tuned: 0003"] is False, rows
    assert rows["AMD Telemetry (UEIP) disabled"] is True and rows["AMD ULPS disabled"] is True
    assert rows["AMD Scheduled Tasks disabled"] is True, "a task that is not installed is not a failure"

    _amd_machine(adapters)
    m.TASK_FAIL.add(m._task_key(NC._AMD_TASKS[1]))          # there, and the change is refused
    m.REG_FAIL.add((r"hkcu\software\amd\cn", "uepnotificationstatus"))
    rows = {r["name"]: r["ok"] for r in _amd_tweaks()}
    assert rows["AMD Scheduled Tasks disabled"] is False and rows["AMD Telemetry (UEIP) disabled"] is False, rows


def test_amd_tweaks_touch_no_adapter_key_when_the_display_class_cannot_be_read():
    m.fresh()
    m.make_key(_class_key("0000"))                           # the key the old code wrote to blindly
    m.PS_HOOKS.append(lambda c: m.F("Access is denied.") if "DriverDesc" in c else None)
    rows = _amd_tweaks()
    assert not [w for w in m.WRITES if w[0].lower().startswith(m.DISPLAY_CLASS.lower())], m.WRITES
    assert {"name": "AMD PowerPlay: no AMD adapter found in the display class", "ok": False} in rows


# ── vault ─────────────────────────────────────────────────────────────
def test_a_damaged_vault_file_does_not_stop_the_app_from_starting():
    try:
        V = importlib.import_module("core.vault")
    except ImportError as e:
        raise unittest.SkipTest(f"the vault's dependencies are not installed here: {e}")
    m.fresh()
    assert config.VAULT_DB_PATH.startswith(m.ROOT)
    with open(config.VAULT_DB_PATH, "wb") as f:
        f.write(b"not a database, " * 64)
    try:
        importlib.reload(V)                         # what app.py's import does at start-up
    finally:
        gc.collect()
        os.remove(config.VAULT_DB_PATH)
        importlib.reload(V)
    assert V.is_pin_set() is False


def _fresh_vault():
    try:
        V = importlib.import_module("core.vault")
    except ImportError as e:
        raise unittest.SkipTest(f"the vault's dependencies are not installed here: {e}")
    m.fresh()
    assert config.VAULT_DB_PATH.startswith(m.ROOT)
    V._init_db()
    V.lock()
    V._vault_state.update(failed_attempts=0, lockout_until=0)
    return V


def _all_at_once(fn, args_list):
    start = threading.Barrier(len(args_list))
    results = []

    def run(*args):
        start.wait()
        results.append(fn(*args))

    threads = [threading.Thread(target=run, args=a) for a in args_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def test_parallel_pin_guesses_cannot_get_past_the_attempt_limit():
    V = _fresh_vault()
    assert V.setup_pin("1234")["ok"]
    V.lock()
    checked = []
    real_hash = V._hash_pin

    def wrong_and_slow(pin, salt):              # the real check takes about as long
        checked.append(pin)
        time.sleep(0.2)
        return "0" * 64

    V._hash_pin = wrong_and_slow
    tries = 15
    try:
        results = _all_at_once(V.unlock, [("9999",)] * tries)
    finally:
        V._hash_pin = real_hash
        V._vault_state.update(failed_attempts=0, lockout_until=0)
    assert len(checked) == V.VAULT_MAX_PIN_ATTEMPTS, f"{len(checked)} guesses were checked"
    assert sum(1 for r in results if r.get("locked_out")) == tries - V.VAULT_MAX_PIN_ATTEMPTS + 1, results


def test_two_pin_setups_at_once_leave_one_pin_and_a_key_that_matches_it():
    V = _fresh_vault()
    results = _all_at_once(lambda pin: (pin, V.setup_pin(pin)), [("1111",), ("2222",)])
    won = [pin for pin, r in results if r["ok"]]
    assert len(won) == 1, results
    assert V.add_entry("site", "me", "secret")["ok"]
    V.lock()
    assert V.unlock(won[0])["ok"]
    got = V.get_entries()
    assert got["count"] == 1 and not got["had_decrypt_errors"], "an entry saved that session could not be read back"
    V.lock()
