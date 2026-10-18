"""A fake Windows machine for the tests.

Import this FIRST in every test file, before any app module:

    import sandbox as m

Importing it

  * points APPDATA / LOCALAPPDATA / USERPROFILE at a throwaway folder, so the
    app's data folder is never the real one (on the developer's PC the real one
    belongs to a live install);
  * replaces the registry (`winreg`) and the command runners
    (core.utils.run_cmd / run_ps) with in-memory fakes, so no test can change a
    system setting - or even start a process: subprocess.Popen raises;
  * stands in for the Windows APIs app modules call directly: process priority
    and affinity, the mouse-acceleration call, the timer-resolution request,
    and the NVIDIA clock interface (which simply reports "not available").

The fakes answer the way Windows does where the app depends on it: `reg query`
prints DWORDs as hex, `powercfg /query` leaves hidden settings out while `/qh`
lists them, `sc config` rejects a start keyword sc.exe does not know, DISM
only prints English with /english, `bcdedit /enum` has underlined headings.
Tests that need an awkward machine flip the switches below (BCD_OK, DISM_OK,
REG_FAIL, SC_FAIL, ...) or add a hook to CMD_HOOKS / PS_HOOKS.

fresh() puts the machine and the app's saved state back to the starting point;
call it at the top of every test.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import winreg as _real_winreg

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

if "config" in sys.modules or any(n == "core" or n.startswith("core.") for n in sys.modules):
    raise RuntimeError("sandbox must be imported before any app module - "
                       "put `import sandbox as m` at the very top of the test file")

# ── a throwaway user profile ──────────────────────────────────────────
ROOT = tempfile.mkdtemp(prefix="vispora_tests_")
for _var, _sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local"), ("USERPROFILE", "Profile")):
    os.environ[_var] = os.path.join(ROOT, _sub)
    os.makedirs(os.environ[_var], exist_ok=True)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.dont_write_bytecode = True

# ── nothing may start a real process ──────────────────────────────────
REAL_POPEN = subprocess.Popen


class _NoPopen(subprocess.Popen):
    def __init__(self, *a, **k):
        raise AssertionError(f"a test tried to start a real process: {a[:1]!r}")


subprocess.Popen = _NoPopen


def _refuse(what):
    def refuse(*a, **k):
        raise AssertionError(f"a test tried to call {what}: {a[:1]!r}")
    return refuse


os.system = _refuse("os.system")
if hasattr(os, "startfile"):
    os.startfile = _refuse("os.startfile")


def spawn(args, env=None, cwd=None, timeout=120):
    """Run a child Python interpreter (the one way a test may start a process):
    for the import-time behaviour that cannot be exercised twice in one
    interpreter.  Returns (exit code, stdout, stderr)."""
    p = REAL_POPEN([sys.executable, "-u"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=env, cwd=cwd or REPO, text=True, encoding="utf-8", errors="replace")
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out, err


def R(out=""):
    return {"ok": True, "out": out, "err": ""}


def F(err="failed", out=""):
    return {"ok": False, "out": out, "err": err}


# ═════════════════════════════════════════════════════════════════════
# Registry
# ═════════════════════════════════════════════════════════════════════
_HIVE_SHORT = {"HKLM": "HKLM", "HKEY_LOCAL_MACHINE": "HKLM", "HKCU": "HKCU", "HKEY_CURRENT_USER": "HKCU",
               "HKCR": "HKCR", "HKEY_CLASSES_ROOT": "HKCR", "HKU": "HKU", "HKEY_USERS": "HKU"}
_HIVE_LONG = {"HKLM": "HKEY_LOCAL_MACHINE", "HKCU": "HKEY_CURRENT_USER", "HKCR": "HKEY_CLASSES_ROOT",
              "HKU": "HKEY_USERS"}
_HIVE_IDS = {_real_winreg.HKEY_LOCAL_MACHINE: "HKLM", _real_winreg.HKEY_CURRENT_USER: "HKCU",
             _real_winreg.HKEY_CLASSES_ROOT: "HKCR", _real_winreg.HKEY_USERS: "HKU"}
_TYPE_IDS = {"REG_NONE": 0, "REG_SZ": 1, "REG_EXPAND_SZ": 2, "REG_BINARY": 3, "REG_DWORD": 4,
             "REG_MULTI_SZ": 7, "REG_QWORD": 11}
_TYPE_NAMES = {v: k for k, v in _TYPE_IDS.items()}

STORE: dict = {}        # (hive, sub-key lower) -> {value name lower: (name, type name, data)}
_KEY_CASE: dict = {}    # (hive, sub-key lower) -> the sub-key as first written
REG_FAIL: set = set()   # (key path lower, value name lower): writes and deletes are refused
WRITES: list = []       # every `reg add`: (key, value name, type, data text)
DELETES: list = []      # every `reg delete`: (key, value name or None for the whole key)


def kp(path: str):
    """'HKLM\\Software\\X' -> ('HKLM', 'software\\x')."""
    hive, _, sub = path.partition("\\")
    return _HIVE_SHORT[hive.upper()], sub.strip("\\").lower()


def _values(hive, sub, create=False):
    k = (hive, sub.lower())
    if k not in STORE and create:
        STORE[k] = {}
        _KEY_CASE[k] = sub
    return STORE.get(k)


def _key_exists(hive, sub) -> bool:
    sub = sub.lower()
    return not sub or (hive, sub) in STORE or any(h == hive and s.startswith(sub + "\\") for h, s in STORE)


def _children(hive, sub) -> list:
    sub = sub.lower()
    prefix = sub + "\\" if sub else ""
    seen, names = set(), []
    for (h, s), original in _KEY_CASE.items():
        if h == hive and s.startswith(prefix) and s != sub:
            child = original[len(prefix):].split("\\", 1)[0]
            if child.lower() not in seen:
                seen.add(child.lower())
                names.append(child)
    return names


def _refused(hive, sub, name) -> bool:
    return (f"{hive}\\{sub}".lower(), name.lower()) in REG_FAIL


def key_exists(path: str) -> bool:
    return _key_exists(*kp(path))


def rget(path: str, name: str):
    """(type name, data) of a value, or None if it does not exist."""
    v = (STORE.get(kp(path)) or {}).get(name.lower())
    return (v[1], v[2]) if v else None


def rdata(path: str, name: str, default=None):
    v = rget(path, name)
    return v[1] if v else default


def rset(path: str, name: str, data, vtype: str = None) -> None:
    if vtype is None:
        vtype = ("REG_DWORD" if isinstance(data, int) else "REG_BINARY" if isinstance(data, bytes)
                 else "REG_MULTI_SZ" if isinstance(data, list) else "REG_SZ")
    hive, _, sub = path.partition("\\")
    _values(_HIVE_SHORT[hive.upper()], sub.strip("\\"), create=True)[name.lower()] = (name, vtype, data)


def rdel(path: str, name: str) -> None:
    (STORE.get(kp(path)) or {}).pop(name.lower(), None)


def make_key(path: str) -> None:
    hive, _, sub = path.partition("\\")
    _values(_HIVE_SHORT[hive.upper()], sub.strip("\\"), create=True)


def flat() -> dict:
    """Every value on the machine: {(hive, key lower, name lower): (type, data)}."""
    return {(h, s, n): (v[1], v[2]) for (h, s), vals in STORE.items() for n, v in vals.items()}


def _delete_tree(hive, sub) -> None:
    sub = sub.lower()
    for k in [k for k in STORE if k[0] == hive and (k[1] == sub or k[1].startswith(sub + "\\"))]:
        STORE.pop(k)
        _KEY_CASE.pop(k, None)


# ── winreg ────────────────────────────────────────────────────────────
class _Key:
    def __init__(self, hive, sub):
        self.hive, self.sub = hive, sub

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def Close(self):
        pass


def _locate(key, sub_key=""):
    if isinstance(key, _Key):
        hive, base = key.hive, key.sub
    elif key in _HIVE_IDS:
        hive, base = _HIVE_IDS[key], ""
    else:
        raise OSError(6, "The handle is invalid")
    sub_key = (sub_key or "").strip("\\")
    return hive, "\\".join(p for p in (base, sub_key) if p)


def _missing():
    return FileNotFoundError(2, "The system cannot find the file specified")


def _open_key(key, sub_key="", reserved=0, access=0):
    hive, sub = _locate(key, sub_key)
    if not _key_exists(hive, sub):
        raise _missing()
    return _Key(hive, sub)


def _create_key(key, sub_key="", reserved=0, access=0):
    hive, sub = _locate(key, sub_key)
    _values(hive, sub, create=True)
    return _Key(hive, sub)


def _query_value(key, name):
    v = (_values(key.hive, key.sub) or {}).get((name or "").lower())
    if v is None:
        raise _missing()
    return v[2], _TYPE_IDS[v[1]]


def _set_value(key, name, reserved, vtype, data):
    if _refused(key.hive, key.sub, name or ""):
        raise PermissionError(13, "Access is denied")
    _values(key.hive, key.sub, create=True)[(name or "").lower()] = (name or "", _TYPE_NAMES[vtype], data)


def _delete_value(key, name):
    vals = _values(key.hive, key.sub) or {}
    if (name or "").lower() not in vals:
        raise _missing()
    if _refused(key.hive, key.sub, name or ""):
        raise PermissionError(13, "Access is denied")
    vals.pop((name or "").lower())


def _delete_key(key, sub_key):
    hive, sub = _locate(key, sub_key)
    if (hive, sub.lower()) not in STORE:
        raise _missing()
    if _children(hive, sub):
        raise PermissionError(13, "Access is denied")
    _delete_tree(hive, sub)


def _enum_key(key, index):
    names = _children(key.hive, key.sub)
    if index >= len(names):
        raise OSError(259, "No more data is available")
    return names[index]


def _enum_value(key, index):
    vals = list((_values(key.hive, key.sub) or {}).values())
    if index >= len(vals):
        raise OSError(259, "No more data is available")
    name, vtype, data = vals[index]
    return name, data, _TYPE_IDS[vtype]


def _query_info(key):
    return len(_children(key.hive, key.sub)), len(_values(key.hive, key.sub) or {}), 0


_fake_winreg = types.ModuleType("winreg")
for _n in dir(_real_winreg):
    if _n.isupper():
        setattr(_fake_winreg, _n, getattr(_real_winreg, _n))
_fake_winreg.OpenKey = _fake_winreg.OpenKeyEx = _open_key
_fake_winreg.CreateKey = _fake_winreg.CreateKeyEx = _create_key
_fake_winreg.QueryValueEx = _query_value
_fake_winreg.SetValueEx = _set_value
_fake_winreg.DeleteValue = _delete_value
_fake_winreg.DeleteKey = _delete_key
_fake_winreg.EnumKey = _enum_key
_fake_winreg.EnumValue = _enum_value
_fake_winreg.QueryInfoKey = _query_info
_fake_winreg.CloseKey = lambda key: None
_fake_winreg.FlushKey = lambda key: None
_fake_winreg.ConnectRegistry = lambda computer, key: key
_fake_winreg.HKEYType = _Key
sys.modules["winreg"] = _fake_winreg


# ── reg.exe ───────────────────────────────────────────────────────────
def _reg_text(vtype, data) -> str:
    if vtype in ("REG_DWORD", "REG_QWORD"):
        return f"0x{data:x}"
    if vtype == "REG_BINARY":
        return data.hex().upper()
    if vtype == "REG_MULTI_SZ":
        return "\\0".join(data)
    return str(data)


def _reg_parse(vtype, text):
    if vtype in ("REG_DWORD", "REG_QWORD"):
        n = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        if not 0 <= n <= (0xFFFFFFFF if vtype == "REG_DWORD" else 0xFFFFFFFFFFFFFFFF):
            raise ValueError(text)
        return n
    if vtype == "REG_BINARY":
        return bytes.fromhex(text)
    if vtype == "REG_MULTI_SZ":
        return text.split("\\0") if text else []
    return text


def _reg(a: list) -> dict:
    op = a[1].lower() if len(a) > 1 else ""
    if op in ("export", "import", "save", "load"):
        return R("The operation completed successfully.")
    if len(a) < 3:
        return F("ERROR: Invalid syntax.")
    try:
        hive, _, sub = a[2].partition("\\")
        hive, sub = _HIVE_SHORT[hive.upper()], sub.strip("\\")
    except KeyError:
        return F("ERROR: Invalid key name.")
    low = [x.lower() for x in a]
    name = a[low.index("/v") + 1] if "/v" in low else ("" if "/ve" in low else None)

    if op == "add":
        vtype = a[low.index("/t") + 1].upper() if "/t" in low else "REG_SZ"
        text = a[low.index("/d") + 1] if "/d" in low else ""
        WRITES.append((a[2], name, vtype, text))
        if name is None:
            _values(hive, sub, create=True)
            return R("The operation completed successfully.")
        if vtype not in _TYPE_IDS:
            return F("ERROR: Invalid syntax.")
        try:
            data = _reg_parse(vtype, text)
        except ValueError:
            return F("ERROR: Invalid syntax. Specify valid numeric value for '/d'.")
        if _refused(hive, sub, name):
            return F("ERROR: Access is denied.")
        _values(hive, sub, create=True)[name.lower()] = (name, vtype, data)
        return R("The operation completed successfully.")

    if op == "delete":
        DELETES.append((a[2], name))
        vals = _values(hive, sub)
        if name is None and "/va" not in low:           # the whole key
            if not sub or not _key_exists(hive, sub):
                return F("ERROR: The system was unable to find the specified registry key or value.")
            _delete_tree(hive, sub)
            return R("The operation completed successfully.")
        if "/va" in low:
            if vals is None:
                return F("ERROR: The system was unable to find the specified registry key or value.")
            vals.clear()
            return R("The operation completed successfully.")
        if vals is None or name.lower() not in vals:
            return F("ERROR: The system was unable to find the specified registry key or value.")
        if _refused(hive, sub, name):
            return F("ERROR: Access is denied.")
        vals.pop(name.lower())
        return R("The operation completed successfully.")

    if op == "query":
        if not _key_exists(hive, sub):
            return F("ERROR: The system was unable to find the specified registry key or value.")
        vals = _values(hive, sub) or {}
        full = f"{_HIVE_LONG[hive]}\\{_KEY_CASE.get((hive, sub.lower()), sub)}"
        if name is not None:
            if name.lower() not in vals:
                return F("ERROR: The system was unable to find the specified registry key or value.")
            n, vtype, data = vals[name.lower()]
            return R(f"{full}\n    {n or '(Default)'}    {vtype}    {_reg_text(vtype, data)}")
        lines = [full] + [f"    {n or '(Default)'}    {t}    {_reg_text(t, d)}" for n, t, d in vals.values()]
        lines += [""] + [f"{full}\\{c}" for c in _children(hive, sub)]
        return R("\n".join(lines).strip())
    return R()


# ═════════════════════════════════════════════════════════════════════
# Power plans
# ═════════════════════════════════════════════════════════════════════
BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
STOCK_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"     # the hidden template; not a plan you can switch to
USERPLAN = "aaaaaaaa-1111-2222-3333-444444444444"           # a plan the user made themselves

PROC_SUB = "54533251-82be-4824-96c1-47b60b740d00"
IDLE = "5d76a2ca-e8c0-402f-a133-2158492d58ad"
PARK_E = "0cc5b647-c1df-4637-891a-dec35c318583"             # core parking minimum, efficiency class 0
PARK_P = "0cc5b647-c1df-4637-891a-dec35c318584"             # ...efficiency class 1
PROC_MIN = "893dee8e-2bef-41e0-89c6-b55d0929964c"
PROC_MAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"
BOOST = "be337238-0d82-4146-a960-4f3749d470c7"
_POWER_ALIAS = {"sub_processor": PROC_SUB, "idledisable": IDLE}
_POWER_HIDDEN = {IDLE, BOOST, PARK_E, PARK_P}               # `powercfg /query` does not list these
POWER_DEFAULTS = {IDLE: [0, 0], PARK_E: [100, 10], PARK_P: [0, 5]}      # [AC, DC]; anything else is [5, 6]
POWER_LIMITS: dict = {}     # setting -> the largest value Windows accepts (100 unless listed)

PLAN = {"active": USERPLAN, "schemes": set(), "vals": {}}
PTOUCH: list = []       # every powercfg set: (scheme, setting, "ac" | "dc", value)
HIBERNATE = [True]
DUPLICATE_OK = [True]   # False: this Windows has no Ultimate Performance template
_dup = [0]


def _scheme(text: str) -> str:
    text = text.lower()
    return PLAN["active"] if text == "scheme_current" else text


def cell(scheme: str, setting: str) -> list:
    """The live [AC, DC] pair of one setting on one plan."""
    setting = _POWER_ALIAS.get(setting.lower(), setting.lower())
    return PLAN["vals"].setdefault((_scheme(scheme), setting), list(POWER_DEFAULTS.get(setting, [5, 6])))


def plan_values(scheme: str) -> dict:
    """{setting: [AC, DC]} of everything that has been read or set on a plan."""
    return {s: list(v) for (sch, s), v in PLAN["vals"].items() if sch == _scheme(scheme)}


def _powercfg(a: list) -> dict:
    op = a[1].lower().lstrip("-/") if len(a) > 1 else ""
    if op == "getactivescheme":
        return R(f"Power Scheme GUID: {PLAN['active']}  (Some plan)")
    if op in ("setactive", "s"):
        g = _scheme(a[2])
        if g not in PLAN["schemes"]:
            return F("Invalid Parameters -- try \"/?\" for help")
        PLAN["active"] = g
        return R()
    if op in ("qh", "query", "q") and len(a) >= 5:
        scheme, setting = _scheme(a[2]), _POWER_ALIAS.get(a[4].lower(), a[4].lower())
        if scheme not in PLAN["schemes"]:
            return F("The power scheme, subgroup or setting specified does not exist.")
        head = f"Power Scheme GUID: {scheme}  (Some plan)\n  Subgroup GUID: {a[3].lower()}  (A subgroup)"
        if op != "qh" and setting in _POWER_HIDDEN:
            return R(head)                      # exits 0 and simply leaves the setting out
        ac, dc = cell(scheme, setting)
        return R(f"{head}\n    Power Setting GUID: {setting}  (A setting)\n"
                 f"      Minimum Possible Setting: 0x00000000\n"
                 f"      Maximum Possible Setting: 0x{POWER_LIMITS.get(setting, 100):08x}\n"
                 f"      Possible Settings increment: 0x00000001\n"
                 f"    Current AC Power Setting Index: 0x{ac:08x}\n    Current DC Power Setting Index: 0x{dc:08x}")
    if op in ("setacvalueindex", "setdcvalueindex") and len(a) >= 6:
        if _scheme(a[2]) not in PLAN["schemes"]:
            return F("The power scheme, subgroup or setting specified does not exist.")
        if int(a[5]) > POWER_LIMITS.get(_POWER_ALIAS.get(a[4].lower(), a[4].lower()), 100):
            return F("The value is out of range for this setting.")
        which = 0 if op == "setacvalueindex" else 1
        cell(a[2], a[4])[which] = int(a[5])
        PTOUCH.append((_scheme(a[2]), _POWER_ALIAS.get(a[4].lower(), a[4].lower()), "ac" if which == 0 else "dc", int(a[5])))
        return R()
    if op == "duplicatescheme":
        if not DUPLICATE_OK[0]:
            return F("The power scheme, subgroup or setting specified does not exist.")
        _dup[0] += 1
        g = f"dddddddd-0000-0000-0000-{_dup[0]:012d}"
        PLAN["schemes"].add(g)
        return R(f"Power Scheme GUID: {g}  (Ultimate Performance)")
    if op in ("h", "hibernate") and len(a) >= 3:
        HIBERNATE[0] = a[2].lower() == "on"
        return R()
    if op in ("list", "l"):
        return R("\n".join(f"Power Scheme GUID: {g}  (Plan){' *' if g == PLAN['active'] else ''}"
                           for g in sorted(PLAN["schemes"])))
    if op == "delete" and len(a) >= 3:
        PLAN["schemes"].discard(a[2].lower())
        return R()
    return R()


# ═════════════════════════════════════════════════════════════════════
# Services, scheduled tasks, boot entry, TCP, features, NTFS, Defender
# ═════════════════════════════════════════════════════════════════════
_SVC = r"HKLM\SYSTEM\CurrentControlSet\Services"
_SC_START = {"boot": 0, "system": 1, "auto": 2, "delayed-auto": 2, "demand": 3, "disabled": 4}
SC_FAIL: set = set()    # service names (lower) sc.exe may not reconfigure
RUNNING: set = set()    # service names (lower) that are running


def svc_set(name: str, mode: str) -> None:
    rset(rf"{_SVC}\{name}", "Start", _SC_START[mode])
    if mode == "delayed-auto":
        rset(rf"{_SVC}\{name}", "DelayedAutostart", 1)
    elif mode == "auto" and rget(rf"{_SVC}\{name}", "DelayedAutostart"):
        rset(rf"{_SVC}\{name}", "DelayedAutostart", 0)


def svc_get(name: str):
    start = rdata(rf"{_SVC}\{name}", "Start")
    if start == 2:
        return "delayed-auto" if rdata(rf"{_SVC}\{name}", "DelayedAutostart") == 1 else "auto"
    return {0: "boot", 1: "system", 3: "demand", 4: "disabled"}.get(start)


def _sc(a: list) -> dict:
    op = a[1].lower() if len(a) > 1 else ""
    name = a[2] if len(a) > 2 else ""
    if svc_get(name) is None:
        return F("", "[SC] OpenService FAILED 1060:\n\nThe specified service does not exist as an installed service.")
    if op == "config":
        mode = a[a.index("start=") + 1] if "start=" in a and a.index("start=") + 1 < len(a) else ""
        if mode not in _SC_START:           # "manual", "automatic": not keywords sc.exe knows
            return F("", "[SC] ChangeServiceConfig FAILED 87:\n\nThe parameter is incorrect.")
        if name.lower() in SC_FAIL:
            return F("", "[SC] OpenService FAILED 5:\n\nAccess is denied.")
        svc_set(name, mode)
        return R("[SC] ChangeServiceConfig SUCCESS")
    if op == "stop":
        if name.lower() not in RUNNING:
            return F("", "[SC] ControlService FAILED 1062:\n\nThe service has not been started.")
        RUNNING.discard(name.lower())
        return R("STATE : 3  STOP_PENDING")
    if op == "start":
        if svc_get(name) == "disabled":
            return F("", "[SC] StartService FAILED 1058:\n\nThe service cannot be started.")
        RUNNING.add(name.lower())
        return R("STATE : 2  START_PENDING")
    if op == "query":
        return R(f"SERVICE_NAME: {name}\n        STATE              : "
                 + ("4  RUNNING" if name.lower() in RUNNING else "1  STOPPED"))
    return R()


TASKS: dict = {}        # task path lower, no leading backslash -> {"path", "enabled"}
TASK_FAIL: set = set()  # task paths (same form) schtasks may not change
TASK_NO_FLAG: set = set()   # enabled tasks whose XML has no <Enabled> element in <Settings>


def _task_key(path: str) -> str:
    return path.lstrip("\\").lower()


def task_add(path: str, enabled: bool = True) -> None:
    TASKS[_task_key(path)] = {"path": path, "enabled": enabled}


def task_on(path: str):
    t = TASKS.get(_task_key(path))
    return None if t is None else t["enabled"]


def _schtasks(a: list) -> dict:
    low = [x.lower() for x in a]
    path = a[low.index("/tn") + 1] if "/tn" in low else ""
    t = TASKS.get(_task_key(path))
    if t is None:
        return F("ERROR: The system cannot find the file specified.")
    if "/query" in low:
        flag = "" if (t["enabled"] and _task_key(path) in TASK_NO_FLAG) else \
            f"    <Enabled>{'true' if t['enabled'] else 'false'}</Enabled>\n"
        return R('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2">\n'
                 "  <Triggers>\n    <LogonTrigger>\n      <Enabled>true</Enabled>\n    </LogonTrigger>\n  </Triggers>\n"
                 f"  <Settings>\n    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n{flag}  </Settings>\n"
                 "  <Actions><Exec><Command>x.exe</Command></Exec></Actions>\n</Task>")
    if "/change" in low:
        if _task_key(path) in TASK_FAIL:
            return F("ERROR: Access is denied.")
        if "/disable" in low:
            t["enabled"] = False
        elif "/enable" in low:
            t["enabled"] = True
        return R("SUCCESS: The parameters of scheduled task have been changed.")
    return R()


BCD: dict = {}          # element -> value, as bcdedit shows them
BCD_OK = [True]         # False: the store cannot be opened (not elevated)
BCD_FAIL: set = set()   # elements (lower) /set and /deletevalue refuse


def _bcd_shown(value: str) -> str:
    return value[:1].upper() + value[1:] if value.isalpha() else value


def bcd_get(name: str):
    return next((v for k, v in BCD.items() if k.lower() == name.lower()), None)


def _bcdedit(a: list) -> dict:
    op = a[1].lower() if len(a) > 1 else ""
    if not BCD_OK[0]:
        return F("", "The boot configuration data store could not be opened.\nAccess is denied.")
    if op == "/enum":
        rows = [("identifier", "{current}"), ("device", "partition=C:"), ("path", r"\WINDOWS\system32\winload.efi"),
                ("description", "Windows 11"), ("locale", "en-US"), ("osdevice", "partition=C:"),
                ("systemroot", r"\WINDOWS")] + list(BCD.items())
        return R("\nWindows Boot Loader\n-------------------\n" + "\n".join(f"{k:<24}{v}" for k, v in rows))
    name = a[3] if len(a) > 3 else ""
    if name.lower() in BCD_FAIL:
        return F("", "An error occurred while attempting to reference the specified element.")
    existing = next((k for k in BCD if k.lower() == name.lower()), None)
    if op == "/set" and len(a) > 4:
        BCD[existing or name.lower()] = _bcd_shown(a[4])
        return R("The operation completed successfully.")
    if op == "/deletevalue":
        if existing is None:
            return F("", "An error occurred while attempting to delete the specified data element.\nElement not found.")
        BCD.pop(existing)
        return R("The operation completed successfully.")
    return R()


TCP_DEFAULTS = {"rss": "enabled", "autotuninglevel": "normal", "congestionprovider": "default",
                "ecncapability": "disabled", "timestamps": "disabled", "initialrto": "1000", "rsc": "enabled",
                "nonsackrttresiliency": "disabled", "maxsynretransmissions": "4", "fastopen": "enabled",
                "fastopenfallback": "enabled", "hystart": "enabled", "prr": "enabled", "pacingprofile": "off"}
TCP: dict = {}
STACK: dict = {}        # teredo / 6to4 / isatap / heuristics -> state
STACK_OK = [True]       # False: the NetTCPIP cmdlets are not there
TCP_OK = [True]


def _netsh(a: list) -> dict:
    low = [x.lower() for x in a]
    if low[1:4] == ["int", "tcp", "dump"]:
        if not TCP_OK[0]:
            return F("The requested operation requires elevation (Run as administrator).")
        return R("#========================\n# TCP Configuration\n#========================\npushd interface tcp\n\nreset\n"
                 "set global " + " ".join(f"{k}={v}" for k, v in TCP.items()) + "\n\npopd\n# End of TCP configuration")
    if low[1:5] == ["int", "tcp", "set", "global"]:
        for tok in a[5:]:
            k, _, v = tok.partition("=")
            if k.lower() not in TCP:        # e.g. chimney: removed from Windows years ago
                return F("", "'" + k + "' is not a valid argument for this command.")
            TCP[k.lower()] = v.lower()
        return R("Ok.")
    if low[1:5] == ["int", "tcp", "set", "heuristics"] and len(a) > 5:
        STACK["heuristics"] = low[5]
        return R("Ok.")
    if low[1] == "interface" and len(a) > 5 and low[2] in ("teredo", "6to4", "isatap") and low[3:5] == ["set", "state"]:
        STACK[low[2]] = low[5]
        return R("Ok.")
    if low[1:4] == ["int", "tcp", "reset"]:
        TCP.clear()
        TCP.update(TCP_DEFAULTS)
        return R("Ok.")
    if "show" in low:                       # localised text nothing should be parsing
        return R("Globale TCP-Parameter\n----------------------------------------------\nSkalierungsstatus : aktiviert")
    return R("Ok.")


FEATURES: dict = {}     # optional feature -> enabled?
DISM_OK = [True]        # False: DISM cannot run (not elevated)
DISM_3010: set = set()  # features whose change "needs a restart": DISM exits 3010, not 0


def _dism(a: list) -> dict:
    low = [x.lower() for x in a]
    if not DISM_OK[0]:
        return F("", "Error: 740\n\nElevated permissions are required to run DISM.")
    name = next((x.split(":", 1)[1] for x in a if x.lower().startswith("/featurename:")), "")
    real = next((k for k in FEATURES if k.lower() == name.lower()), None)
    if "/get-features" in low:
        if "/english" not in low:
            return R("Tool zur Imageverwaltung\n\nFeaturename | Status\n" + "\n".join(
                f"{k} | {'Aktiviert' if v else 'Deaktiviert'}" for k, v in FEATURES.items()))
        rows = "\n".join(f"{k:<44}| {'Enabled' if v else 'Disabled'}" for k, v in FEATURES.items())
        return R("Deployment Image Servicing and Management tool\nVersion: 10.0.26100.1\n\nImage Version: 10.0.26200.1\n\n"
                 "Features listing for package : Microsoft-Windows-Foundation-Package\n\n"
                 f"{'-' * 43} | --------\n{'Feature Name':<44}| State\n{'-' * 43} | --------\n{rows}\n\n"
                 "The operation completed successfully.")
    if "/disable-feature" in low or "/enable-feature" in low:
        if real is None:
            return F("", f"Error: 0x800f080c\n\nFeature name {name} is unknown.")
        FEATURES[real] = "/enable-feature" in low
        done = "The operation completed successfully." if "/english" in low else "Der Vorgang wurde erfolgreich beendet."
        if real.lower() in DISM_3010:
            return F("", done + "\nRestart Windows to complete this operation.")
        return R(done)
    return R()


FSUTIL: dict = {}
FSUTIL_OK = [True]


def _fsutil(a: list) -> dict:
    low = [x.lower() for x in a]
    if low[1:3] != ["behavior", "query"] and low[1:3] != ["behavior", "set"]:
        return R()
    if not FSUTIL_OK[0]:
        return F("", "Error:  Access is denied.")
    flag = low[3] if len(low) > 3 else ""
    if low[2] == "set" and len(a) > 4:
        FSUTIL[flag] = int(a[4])
        return R(f"{flag} = {a[4]}")
    if flag == "disabledeletenotify":
        return R("NTFS DisableDeleteNotify = 0  (Allows TRIM operations to be sent to the storage device)")
    if flag not in FSUTIL:
        return F("", "Error:  The parameter is incorrect.")
    if flag == "disable8dot3":              # this one is worded differently from the others
        return R(f"The registry state is: {FSUTIL[flag]} (Per volume setting - the default).")
    return R(f"{flag} = {FSUTIL[flag]}  (System Managed, Disabled)")


DEF: dict = {}
DEF_OK = [True]         # False: Defender is not running (third-party antivirus)
_DEF_ENUMS = {"SubmitSamplesConsent": {"alwaysprompt": 0, "sendsafesamples": 1, "neversend": 2, "sendallsamples": 3},
              "MAPSReporting": {"disabled": 0, "basic": 1, "advanced": 2}}


def _defender(command: str):
    if "Get-MpPreference" in command:
        if not DEF_OK[0]:
            return F("Get-MpPreference : Operation failed with the following error: 0x800106ba")
        return R(json.dumps(DEF, separators=(",", ":")))
    m = re.match(r'(Add|Remove)-MpPreference -(ExclusionPath|ExclusionProcess) "([^"]*)"', command)
    if m:
        if DEF_OK[0]:
            have = DEF.setdefault(m.group(2), [])
            same = [x for x in have if x.lower() == m.group(3).lower()]
            if m.group(1) == "Add" and not same:
                have.append(m.group(3))
            elif m.group(1) == "Remove":
                for x in same:
                    have.remove(x)
        return R()
    m = re.match(r"Set-MpPreference -(\w+) (\S+)", command)
    if m:
        if DEF_OK[0]:
            name, value = m.group(1), m.group(2)
            if name in _DEF_ENUMS:
                DEF[name] = int(value) if value.isdigit() else _DEF_ENUMS[name][value.lower()]
            else:
                DEF[name] = value
        return R()
    return None


# ═════════════════════════════════════════════════════════════════════
# Devices, processes and everything else PowerShell is asked
# ═════════════════════════════════════════════════════════════════════
PNP: dict = {}          # device class -> [{"InstanceId", "FriendlyName", "Status"}]
COUNTS: dict = {}       # "smartcard" / "bluetooth" / "wsa": how many such devices or packages
GPU: dict = {}          # what Win32_VideoController reports: {"Name", "DriverVersion", "AdapterRAM"}
CPU_NAME = ["Intel(R) Core(TM) Ultra 9 285K"]
WSL_DISTROS = [""]
ALIVE: set = set()      # pids PowerShell's Get-Process finds
PS_PRIORITY_OK = [False]    # can PowerShell set a process priority the API could not?
CMDS: list = []         # every command, as a list of strings
PSC: list = []          # every PowerShell command
CMD_HOOKS: list = []    # callables(args) -> result dict or None; asked before the built-in answers
PS_HOOKS: list = []     # callables(command) -> result dict or None


def _powershell(command: str) -> dict:
    r = _defender(command)
    if r is not None:
        return r
    if "Get-NetTeredoConfiguration" in command:
        if not STACK_OK[0]:
            return R("{}")
        return R(json.dumps({k: v.capitalize() for k, v in STACK.items()}, separators=(",", ":")))
    m = re.match(r"Get-PnpDevice -Class (\w+)", command)
    if m:
        devs = PNP.get(m.group(1), [])
        if "Status -eq 'OK'" in command:
            devs = [d for d in devs if d.get("Status", "OK") == "OK"]
        like = re.search(r"FriendlyName -like '\*([^*']+)\*'", command)
        if like:
            devs = [d for d in devs if like.group(1).lower() in d.get("FriendlyName", "").lower()]
        if "ConvertTo-Json" in command:
            picked = [{"InstanceId": d["InstanceId"], "FriendlyName": d.get("FriendlyName", "")} for d in devs]
            return R("" if not picked else json.dumps(picked[0] if len(picked) == 1 else picked))
        return R("\n".join(d["InstanceId"] for d in devs))
    if "Get-PnpDevice -PresentOnly" in command:
        return R(str(COUNTS.get("smartcard" if "SmartCardReader" in command else "bluetooth", 0)))
    if "WindowsSubsystemForAndroid" in command:
        return R(str(COUNTS.get("wsa", 0)))
    if "VirtualMachinePlatform" in command:
        on = FEATURES.get("VirtualMachinePlatform", False)
        if command.startswith("Disable-WindowsOptionalFeature"):
            FEATURES["VirtualMachinePlatform"] = False
            return R()
        if command.startswith("Enable-WindowsOptionalFeature"):
            FEATURES["VirtualMachinePlatform"] = True
            return R()
        return R(json.dumps({"State": 2 if on else 0}) if "ConvertTo-Json" in command else ("Enabled" if on else "Disabled"))
    m = re.match(r"\(Get-Process -Id (\d+) ", command)
    if m:
        return R(m.group(1) if int(m.group(1)) in ALIVE else "")
    if "PriorityClass='High'" in command:
        return R("OK" if PS_PRIORITY_OK[0] else "FAIL")
    if "Get-PhysicalDisk" in command:
        return R(json.dumps([{"MediaType": "SSD", "BusType": "NVMe"}]))
    if "Win32_Processor" in command:
        return R(json.dumps({"Name": CPU_NAME[0], "Description": "Intel64 Family 6"}))
    if "Win32_VideoController" in command:
        return R(json.dumps(GPU) if GPU else "")
    return R()


def run_ps(command: str, timeout: int = 60) -> dict:
    PSC.append(command)
    for hook in PS_HOOKS:
        r = hook(command)
        if r is not None:
            return r
    return _powershell(command.strip())


_TOOLS = {"reg": _reg, "powercfg": _powercfg, "sc": _sc, "schtasks": _schtasks, "bcdedit": _bcdedit,
          "netsh": _netsh, "dism": _dism, "fsutil": _fsutil}


def run_cmd(args: list, timeout: int = 60) -> dict:
    a = [str(x) for x in args]
    CMDS.append(a)
    for hook in CMD_HOOKS:
        r = hook(a)
        if r is not None:
            return r
    exe = os.path.splitext(os.path.basename(a[0]))[0].lower()
    if exe in _TOOLS:
        return _TOOLS[exe](a)
    if exe == "wsl":
        return R(WSL_DISTROS[0])
    if exe in ("nvidia-smi", "winget"):
        return F(f"'{exe}' is not recognized as an internal or external command")
    return R()                              # taskkill, ipconfig, ping, ...


def ran(tool: str, *words) -> list:
    """The `tool` commands (reg, sc, powercfg, ...) whose arguments contain
    every one of `words`, case-insensitively."""
    return [c for c in CMDS
            if os.path.splitext(os.path.basename(c[0]))[0].lower() == tool.lower()
            and all(any(w.lower() in part.lower() for part in c[1:]) for w in words)]


def ps_ran(*words) -> list:
    return [c for c in PSC if all(w.lower() in c.lower() for w in words)]


# ═════════════════════════════════════════════════════════════════════
# The app, wired to the fakes
# ═════════════════════════════════════════════════════════════════════
import config                                       # noqa: E402  (must come after the profile is redirected)

if not config.APPDATA_DIR.startswith(ROOT):
    raise RuntimeError(f"the app's data folder is not sandboxed: {config.APPDATA_DIR}")

import core.utils as _utils                         # noqa: E402

_utils.run_cmd, _utils.run_ps = run_cmd, run_ps     # before anything does `from core.utils import run_cmd`

from core import reg_originals, sys_state           # noqa: E402
from core import process_tuning as _pt              # noqa: E402
from core import nvapi_oc as _nvapi_oc              # noqa: E402

# The NVIDIA clock interface: never loaded, so nothing can read or write clocks.
_nvapi_oc._nvapi.load = lambda: {"ok": False, "err": "NVAPI is not available in the test sandbox"}

# The NVIDIA driver settings store: a dict.  DRS[setting id] = value set in the
# global profile (absent = not set).  DRS_OK[0] False: no NVIDIA driver.
from core import nvidia_profile as _nvp       # noqa: E402

real_nvidia_open = _nvp._open
DRS: dict = {}
DRS_OK = [True]
DRS_SAVE_OK = [True]
DRS_IGNORED: set = set()        # ids the "driver" accepts and then does not keep
DRS_CALLS: list = []


class _FakeDrsSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, sid):
        return DRS.get(sid)

    def set(self, sid, value):
        DRS_CALLS.append(("set", sid, value))
        if sid not in DRS_IGNORED:
            self._pending[sid] = int(value)
        return True

    def delete(self, sid):
        DRS_CALLS.append(("delete", sid))
        self._pending[sid] = None
        return True

    def save(self):
        DRS_CALLS.append(("save",))
        if not DRS_SAVE_OK[0]:
            self._pending.clear()
            return False
        for sid, v in self._pending.items():
            if v is None:
                DRS.pop(sid, None)
            else:
                DRS[sid] = v
        self._pending.clear()
        return True

    def close(self):
        pass

    def __init__(self):
        self._pending = {}


_nvp._open = lambda: _FakeDrsSession() if DRS_OK[0] else None

SYSTEM_MASK = 0xFFFFFF                              # 24 logical processors
CPU_LAYOUT = {"logical": 24, "p": 8, "e": 16, "hybrid": True}
real_cpu_layout = _pt.cpu_layout                    # (read-only; one test looks at the real answer)
_pt.cpu_layout = lambda: dict(CPU_LAYOUT)


class FakeKernel32:
    """Stands in for kernel32 in core.process_tuning.  A handle is the pid.

    A process is a dict in `procs`: priority, affinity, and optionally exe,
    image, ppid, created, throttle, protected (refuses everything but a
    query-only handle, like an anti-cheat-protected game), exited."""

    _QUERY_ONLY = 0x1000 | 0x00100000       # QUERY_LIMITED_INFORMATION | SYNCHRONIZE

    def __init__(self):
        self.procs, self.calls, self.win10 = {}, [], False
        self.snapshots = {}

    def OpenProcess(self, access, inherit, pid):
        p = self.procs.get(pid)
        if p is None or (p.get("protected") and access & ~self._QUERY_ONLY):
            ctypes.set_last_error(5 if p else 87)
            return None
        return pid

    def CloseHandle(self, h):
        self.snapshots.pop(h, None)
        return 1

    def QueryFullProcessImageNameW(self, h, flags, buf, size):
        p = self.procs[h]
        buf.value = p.get("image") or rf"C:\Games\{p.get('exe', 'game.exe')}"
        size._obj.value = len(buf.value)
        return 1

    def GetProcessTimes(self, h, created, exited, kernel, user):
        created._obj.value = self.procs[h].get("created", 10_000 + h)
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):
        token = 0x5000 + len(self.calls) + len(self.snapshots)
        self.snapshots[token] = [(pid_, p) for pid_, p in sorted(self.procs.items()) if not p.get("exited")]
        return token

    def _next(self, snap, entry):
        rows = self.snapshots.get(snap) or []
        if not rows:
            return 0
        pid, p = rows.pop(0)
        e = entry._obj
        e.th32ProcessID, e.th32ParentProcessID, e.szExeFile = pid, p.get("ppid", 1), p.get("exe", "game.exe")
        return 1

    Process32FirstW = _next
    Process32NextW = _next

    def GetProcessInformation(self, h, info_class, state, size):
        state._obj.ControlMask, state._obj.StateMask = self.procs[h].get("throttle", (0, 0))
        return 1

    def TerminateProcess(self, h, code):
        self.procs[h]["exited"] = True
        ALIVE.discard(h)
        self.calls.append(("terminate", h))
        return 1

    def GetExitCodeProcess(self, h, code):
        code._obj.value = 0 if self.procs[h].get("exited") else 259
        return 1

    def GetPriorityClass(self, h):
        return self.procs[h]["priority"]

    def SetPriorityClass(self, h, value):
        if not self.procs[h].get("ignore_priority"):        # (the call "succeeds" and changes nothing)
            self.procs[h]["priority"] = value
        self.calls.append(("priority", h, value))
        return 1

    def GetProcessAffinityMask(self, h, process_mask, system_mask):
        process_mask._obj.value = self.procs[h]["affinity"]
        system_mask._obj.value = SYSTEM_MASK
        return 1

    def SetProcessAffinityMask(self, h, mask):
        if not self.procs[h].get("ignore_affinity"):
            self.procs[h]["affinity"] = mask
        self.calls.append(("affinity", h, mask))
        return 1

    def SetProcessDefaultCpuSets(self, h, ids, count):
        self.procs[h]["cpusets_cleared"] = True
        return 1

    def SetProcessInformation(self, h, info_class, state, size):
        if self.win10 and state._obj.ControlMask & 4:       # the Windows 11 flag: the whole call fails
            return 0
        self.procs[h]["throttle"] = (state._obj.ControlMask, state._obj.StateMask)
        return 1


K = FakeKernel32()
_pt._k32 = K


def proc(pid: int, affinity: int = SYSTEM_MASK, exe: str = "game.exe", **more) -> None:
    """A running process for the process-tuning fake (and for Get-Process)."""
    K.procs[pid] = dict(priority=_pt.NORMAL_PRIORITY_CLASS, affinity=affinity, exe=exe)
    K.procs[pid].update(more)
    ALIVE.add(pid)


def exit_proc(pid: int) -> None:
    ALIVE.discard(pid)
    K.procs[pid]["exited"] = True


SPI: list = []          # SystemParametersInfoW calls: (action, [values...])
TIMER: list = []        # NtSetTimerResolution calls: (100 ns units, set?)


class _FakeUser32:
    def SystemParametersInfoW(self, action, param, values, flags):
        SPI.append((action, list(values) if values is not None else None))
        return 1


class _CtypesShim:
    """`ctypes` as core.competitive_latency sees it: real types, a fake user32."""
    windll = types.SimpleNamespace(user32=_FakeUser32())

    def __getattr__(self, name):
        return getattr(ctypes, name)


class _FakeNtdll:
    def NtSetTimerResolution(self, desired, set_it, current):
        TIMER.append((desired.value, bool(set_it)))
        return 0

    def NtQueryTimerResolution(self, minimum, maximum, current):
        held = any(s for _, s in TIMER[-1:])
        current._obj.value = 5000 if held else 156250
        return 0


def wire(module):
    """Call on an app module after importing it, if it talks to Windows
    directly; modules that only use run_cmd / run_ps / winreg need nothing.
    Returns the module."""
    name = module.__name__
    if name == "core.competitive_latency":
        module.ctypes = _CtypesShim()
    elif name == "core.timer_manager":
        module._ntdll = _FakeNtdll()
    elif name == "core.privacy":
        module.HOSTS_PATH = os.path.join(ROOT, "hosts")
        if not os.path.exists(module.HOSTS_PATH):
            with open(module.HOSTS_PATH, "w", encoding="utf-8") as f:
                f.write("127.0.0.1 localhost\n")
    return module


def app(name: str):
    """Import an app module by name ("core.optimizer") and wire() it."""
    import importlib
    return wire(importlib.import_module(name))


# ═════════════════════════════════════════════════════════════════════
# The starting machine
# ═════════════════════════════════════════════════════════════════════
IFACES = ("{11111111-aaaa-bbbb-cccc-000000000001}", "{22222222-aaaa-bbbb-cccc-000000000002}")
IFACES_KEY = r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
DISPLAY_CLASS = r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

# Start types as Windows ships them - except XboxGipSvc, which this user has
# disabled themselves.  A reset must leave that one disabled.
SERVICES = {
    "XblAuthManager": "demand", "XblGameSave": "demand", "XboxGipSvc": "disabled", "XboxNetApiSvc": "demand",
    "edgeupdate": "delayed-auto", "edgeupdatem": "demand", "MicrosoftEdgeElevationService": "demand",
    "Spooler": "auto", "Fax": "demand", "wisvc": "demand", "TabletInputService": "demand", "RetailDemo": "demand",
    "PhoneSvc": "demand", "icssvc": "demand", "SCardSvr": "demand", "SCPolicySvc": "demand",
    "bthserv": "demand", "WSearch": "delayed-auto", "WerSvc": "demand", "DiagTrack": "auto",
    "dmwappushservice": "demand", "lfsvc": "demand", "NvTelemetryContainer": "auto",
    "AppXSvc": "demand", "InstallService": "demand", "SysMain": "auto",
}
USER_SERVICE_OFF = "XboxGipSvc"
TELEMETRY_TASKS = (
    r"\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser",
    r"\Microsoft\Windows\Application Experience\ProgramDataUpdater",
    r"\Microsoft\Windows\Customer Experience Improvement Program\Consolidator",
    r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip",
    r"\Microsoft\Windows\Customer Experience Improvement Program\KernelCeipTask",
    r"\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector",
    r"\Microsoft\Windows\Feedback\Siuf\DmClient",
    r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload",
    r"\Microsoft\Windows\Windows Error Reporting\QueueReporting",
)
USER_TASK_OFF = r"\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip"   # the user disabled this one
MISSING_TASK = r"\Microsoft\Windows\Feedback\Siuf\DmClientOnScenarioDownload"           # not on this Windows
USER_EXCLUSIONS = [r"D:\MyMods", r"C:\Program Files (x86)\Steam"]                        # the user's own


def seed_system() -> None:
    for name, mode in SERVICES.items():
        svc_set(name, mode)
    RUNNING.update(n.lower() for n, mode in SERVICES.items() if mode in ("auto", "delayed-auto"))
    for t in TELEMETRY_TASKS:
        if t != MISSING_TASK:
            task_add(t, enabled=(t != USER_TASK_OFF))
    BCD.update({"recoveryenabled": "Yes", "bootmenupolicy": "Standard", "nx": "OptIn",
                "useplatformclock": "Yes"})                 # the last one is the user's own setting
    TCP.update(TCP_DEFAULTS)
    STACK.update({"teredo": "client", "6to4": "default", "isatap": "default", "heuristics": "enabled"})
    FEATURES.update({
        "Internet-Explorer-Optional-amd64": False, "Printing-XPSServices-Features": False,
        "Printing-PrintToPDFServices-Features": True, "FaxServicesClientPackage": False,
        "WindowsMediaPlayer": True, "MediaPlayback": True, "WorkFolders-Client": True,
        "Microsoft-Windows-Subsystem-Linux": False, "VirtualMachinePlatform": True, "HypervisorPlatform": False,
        "Microsoft-Hyper-V-All": False, "Containers-DisposableClientVM": True, "NetFx4-AdvSrvs": True,
    })
    FSUTIL.update({"disablelastaccess": 2, "disable8dot3": 2, "memoryusage": 1})
    DEF.update({"ExclusionPath": list(USER_EXCLUSIONS), "ExclusionProcess": ["mygame.exe"],
                "SubmitSamplesConsent": 1, "MAPSReporting": 2,
                "ScanScheduleTime": "02:00:00", "ScanScheduleQuickScanTime": "00:00:00"})
    for guid in IFACES:
        make_key(rf"{IFACES_KEY}\{guid}")
    PNP.update({
        "Net": [{"InstanceId": r"PCI\VEN_8086&DEV_15F3\NIC0", "FriendlyName": "Intel(R) Ethernet Controller I225-V"}],
        "USB": [{"InstanceId": r"PCI\VEN_8086&DEV_7AE0\USB0", "FriendlyName": "USB xHCI Host Controller"},
                {"InstanceId": r"PCI\VEN_1022&DEV_43F7\USB1", "FriendlyName": "USB xHCI (broken)", "Status": "Error"}],
        "Display": [{"InstanceId": r"PCI\VEN_10DE&DEV_2C05\GPU0", "FriendlyName": "NVIDIA GeForce RTX 5070 Ti"}],
    })
    PLAN.update(active=USERPLAN, schemes={BALANCED, HIGH_PERF, USERPLAN}, vals={})


def snap_system() -> dict:
    """Everything outside the registry and the power plans, for before/after
    comparisons."""
    return {
        "services": {n: svc_get(n) for n in SERVICES},
        "tasks": {t["path"]: t["enabled"] for t in TASKS.values()},
        "bcd": dict(BCD), "tcp": dict(TCP), "stack": dict(STACK), "features": dict(FEATURES),
        "fsutil": dict(FSUTIL), "defender": json.loads(json.dumps(DEF)), "hibernate": HIBERNATE[0],
        "plan": PLAN["active"],
    }


def _wipe_app_data() -> None:
    for entry in os.listdir(config.APPDATA_DIR):
        p = os.path.join(config.APPDATA_DIR, entry)
        if entry.lower().startswith("vispora.log"):
            continue                        # the log handler has it open
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
        except OSError:
            pass
    os.makedirs(config.BACKUP_DIR, exist_ok=True)


def fresh(plan: str = USERPLAN) -> None:
    """A clean machine and a clean app: nothing tweaked, nothing on record."""
    for box in (STORE, _KEY_CASE, REG_FAIL, SC_FAIL, RUNNING, TASKS, TASK_FAIL, TASK_NO_FLAG, BCD, BCD_FAIL, TCP,
                STACK, FEATURES, DISM_3010, FSUTIL, DEF, PNP, COUNTS, GPU, ALIVE, POWER_LIMITS, K.snapshots):
        box.clear()
    for log_ in (WRITES, DELETES, PTOUCH, CMDS, PSC, CMD_HOOKS, PS_HOOKS, SPI, TIMER, K.calls):
        del log_[:]
    for flag in (BCD_OK, DISM_OK, FSUTIL_OK, DEF_OK, STACK_OK, TCP_OK, DUPLICATE_OK, HIBERNATE, DRS_OK, DRS_SAVE_OK):
        flag[0] = True
    DRS.clear()
    DRS.update({_nvp.POWER_MODE: 5, _nvp.PRERENDER: 0, _nvp.SHADER_CACHE: 4096})     # this user's driver settings
    DRS_IGNORED.clear()
    del DRS_CALLS[:]
    PS_PRIORITY_OK[0] = False
    WSL_DISTROS[0] = ""
    K.procs.clear()
    K.win10 = False
    seed_system()
    PLAN["active"] = plan
    _wipe_app_data()
    reg_originals._cache = None
    reg_originals._tls.suspended = 0


def clear_logs() -> None:
    for log_ in (WRITES, DELETES, PTOUCH, CMDS, PSC):
        del log_[:]


def wait(condition, seconds: float = 6.0) -> bool:
    """Poll `condition` until it is true; False if it never is."""
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


fresh()
