"""What the system looked like before we changed it.

Every tweak the optimizer applies overwrites something.  "Reset All" used to
put back hard-coded "Windows defaults" — which are a guess: they are wrong
for anyone whose own setting was not the default, they miss values that never
made it into a reset list, and a few of them were not the Windows default at
all.  This module remembers the real prior value the first time a tweak
changes it, so a reset can put back exactly that.

Recorded per item, once (first change wins):
  • registry values   — type + data, or the fact that the value did not exist
  • power-plan settings — the AC / DC index on the plan that was changed
  • other state        — service start types, scheduled tasks, boot entries…
                         (storage only; core/sys_state.py reads and restores)
  • plain notes        — "we turned hibernation off", for things with no value

Only real changes are recorded: if a value already equals what a tweak is
about to write, then we (or the user) set it earlier and its true original is
unknown — recording it now would make a later reset restore the tweak.

Records are kept per SCOPE — the feature that made the change ("optimizer",
"competitive", …) — so one feature's reset puts back only what that feature
changed.  If two features change the same value in turn, each records what it
found, and undoing them in reverse order unwinds correctly.

Stored in %APPDATA%/Vispora/originals.json.
"""
from __future__ import annotations

import json
import os
import re
import threading
import winreg
from typing import Optional

from config import APPDATA_DIR
from core.utils import get_logger, run_cmd, atomic_write_json

log = get_logger("originals")

_PATH = os.path.join(APPDATA_DIR, "originals.json")
_lock = threading.RLock()
_cache: Optional[dict] = None
_tls = threading.local()   # .suspended: set, per thread, while a reset is running — resets must never record

ABSENT = "ABSENT"

# Scopes that are applied when a game starts and undone when it closes.
_TRANSIENT_SCOPES = ("competitive",)


def _is_suspended() -> bool:
    return bool(getattr(_tls, "suspended", 0))


def _take_over_transient(section: dict, scope: str, tail: str) -> bool:
    """A lasting tweak is about to write a value that a per-game mode changed
    and still has on record (the user ran the optimizer with a game open).

    What is in the registry right now is the per-game mode's value, not the
    user's — recording it would make Reset All "restore" a gaming tweak.  The
    real original is the one the per-game mode noted, so the lasting scope
    inherits that record, and the per-game mode gives it up: when the game
    closes it must leave the value the optimizer has just set alone.
    Returns True when the records changed."""
    if scope in _TRANSIENT_SCOPES:
        return False
    changed = False
    for t in _TRANSIENT_SCOPES:
        rec = section.pop(f"{t}||{tail}", None)
        if rec is not None:
            section.setdefault(f"{scope}||{tail}", rec)
            changed = True
    return changed

_HIVES = {
    "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKCU": winreg.HKEY_CURRENT_USER,  "HKEY_CURRENT_USER":  winreg.HKEY_CURRENT_USER,
    "HKCR": winreg.HKEY_CLASSES_ROOT,  "HKEY_CLASSES_ROOT":  winreg.HKEY_CLASSES_ROOT,
    "HKU":  winreg.HKEY_USERS,         "HKEY_USERS":         winreg.HKEY_USERS,
}
_TYPE_NAMES = {
    winreg.REG_DWORD: "REG_DWORD", winreg.REG_QWORD: "REG_QWORD", winreg.REG_SZ: "REG_SZ",
    winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ", winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
    winreg.REG_BINARY: "REG_BINARY",
}
_TYPE_IDS = {v: k for k, v in _TYPE_NAMES.items()}


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
        for section in ("registry", "power", "notes", "state"):
            _cache.setdefault(section, {})
    return _cache


def _save() -> None:
    atomic_write_json(_PATH, _load())


def _split(key_path: str):
    hive, _, sub = key_path.partition("\\")
    return _HIVES.get(hive.upper()), sub


# ── registry ──────────────────────────────────────────────────────────
def read_value(key_path: str, value_name: str) -> Optional[dict]:
    """Current state of a registry value: {"type": "REG_…", "data": …},
    {"type": ABSENT} when it (or its key) does not exist, or None when it
    cannot be read at all (unknown hive, access denied, exotic type)."""
    hive, sub = _split(key_path)
    if hive is None:
        return None
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_QUERY_VALUE) as k:
            data, vtype = winreg.QueryValueEx(k, value_name)
    except FileNotFoundError:
        return {"type": ABSENT}
    except OSError:
        return None
    name = _TYPE_NAMES.get(vtype)
    if name is None:
        return None
    if isinstance(data, bytes):
        data = data.hex()
    return {"type": name, "data": data}


def _same(current: dict, new_data: str, new_type: str) -> bool:
    """Is `current` already what a `reg add /t new_type /d new_data` would produce?"""
    if current["type"] != new_type:
        return False
    if new_type in ("REG_DWORD", "REG_QWORD"):
        try:
            return int(current["data"]) == int(str(new_data), 0)
        except (TypeError, ValueError):
            return False
    return str(current["data"]) == str(new_data)


def remember(key_path: str, value_name: str, new_data: str, new_type: str = "REG_DWORD",
             scope: str = "optimizer") -> None:
    """Call just BEFORE writing a registry value.  Records its prior state the
    first time it is really being changed.  Never raises."""
    if _is_suspended():
        return
    try:
        tail = f"{key_path}|{value_name}"
        ident = f"{scope}||{tail}"
        with _lock:
            reg = _load()["registry"]
            if _take_over_transient(reg, scope, tail):
                _save()
            if ident in reg:
                return
            current = read_value(key_path, value_name)
            if current is None or (current["type"] != ABSENT and _same(current, new_data, new_type)):
                return
            reg[ident] = current
            _save()
    except Exception as e:
        log.debug(f"could not record original of {key_path}\\{value_name}: {e}")


def _write_back(key_path: str, value_name: str, original: dict) -> bool:
    hive, sub = _split(key_path)
    if hive is None:
        return False
    try:
        if original["type"] == ABSENT:
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, value_name)
            except FileNotFoundError:
                pass                      # already gone — that IS the original state
            return True
        vtype = _TYPE_IDS[original["type"]]
        data = original["data"]
        if original["type"] == "REG_BINARY":
            data = bytes.fromhex(data)
        with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, value_name, 0, vtype, data)
        return True
    except Exception as e:
        log.warning(f"could not restore {key_path}\\{value_name}: {e}")
        return False


def has_original(key_path: str, value_name: str, scope: str = "optimizer") -> bool:
    with _lock:
        return f"{scope}||{key_path}|{value_name}" in _load()["registry"]


# ── power-plan settings ───────────────────────────────────────────────
_HEX = re.compile(r"0x[0-9a-fA-F]{8}")


def _active_scheme() -> str:
    r = run_cmd(["powercfg", "/getactivescheme"])
    for part in (r.get("out") or "").split():
        if len(part) == 36 and part.count("-") == 4:
            return part
    return ""


def query_power(scheme: str, sub: str, setting: str) -> Optional[tuple]:
    """(ac_index, dc_index) of one power setting, or None if it can't be read.
    The label text is localised, so this goes by position: the last two hex
    values powercfg prints are the current AC and DC indices.

    /qh, not /query: /query leaves out hidden settings altogether (processor
    idle disable, boost mode, ...) while still exiting 0, so those could
    never be read — and so never recorded or restored."""
    r = run_cmd(["powercfg", "/qh", scheme, sub, setting])
    found = _HEX.findall(r.get("out") or "") if r.get("ok") else []
    if len(found) < 2:
        return None
    return int(found[-2], 16), int(found[-1], 16)


# The powercfg aliases this codebase uses, as the GUIDs they stand for — so a
# setting has ONE identity however a caller happens to name it.
_POWER_ALIASES = {
    "sub_processor": "54533251-82be-4824-96c1-47b60b740d00",
    "idledisable":   "5d76a2ca-e8c0-402f-a133-2158492d58ad",
}


def remember_power(sub: str, setting: str, scope: str = "optimizer") -> None:
    """Call before changing a setting on the ACTIVE power plan."""
    if _is_suspended():
        return
    try:
        sub = _POWER_ALIASES.get(sub.lower(), sub.lower())
        setting = _POWER_ALIASES.get(setting.lower(), setting.lower())
        scheme = _active_scheme()
        if not scheme:
            return
        tail = f"{scheme}|{sub}|{setting}".lower()
        ident = f"{scope}||{tail}"
        with _lock:
            power = _load()["power"]
            if _take_over_transient(power, scope, tail):
                _save()
            if ident in power:
                return
            cur = query_power(scheme, sub, setting)
            if cur is None:
                return
            power[ident] = {"ac": cur[0], "dc": cur[1]}
            _save()
    except Exception as e:
        log.debug(f"could not record power setting {sub}/{setting}: {e}")


# ── notes ─────────────────────────────────────────────────────────────
def note(name: str, value=True) -> None:
    """Remember a fact that has no value to read back ("we disabled hibernation")."""
    if _is_suspended():
        return
    with _lock:
        notes = _load()["notes"]
        if name not in notes:
            notes[name] = value
            _save()


def noted(name: str):
    with _lock:
        return _load()["notes"].get(name)


# ── everything that is not a registry value or a power setting ────────
# Storage only.  What each kind means, how its current state is read and how
# it is put back lives in core/sys_state.py.
def remember_state(kind: str, name: str, prior, scope: str = "optimizer") -> bool:
    """Record `prior` (any JSON value, None included) as what `name` was
    before its first change.  The caller has already read the current state
    and established that this IS a change.  True if it was recorded now."""
    if _is_suspended():
        return False
    try:
        ident = f"{scope}||{kind}|{name}"
        with _lock:
            state = _load()["state"]
            if ident in state:
                return False
            state[ident] = {"prior": prior}
            _save()
        return True
    except Exception as e:
        log.debug(f"could not record {kind} {name}: {e}")
        return False


def state_records(kind: str, scope: str = "optimizer") -> dict:
    """{name: prior} for every record of `kind` in `scope`."""
    prefix = f"{scope}||{kind}|"
    with _lock:
        return {i[len(prefix):]: (r.get("prior") if isinstance(r, dict) else None)
                for i, r in _load()["state"].items() if i.startswith(prefix)}


def has_state(kind: str, name: Optional[str] = None, scope: str = "optimizer") -> bool:
    """Is there a record for this item — or, without a name, for any item of
    this kind?"""
    with _lock:
        state = _load()["state"]
        if name is not None:
            return f"{scope}||{kind}|{name}" in state
        prefix = f"{scope}||{kind}|"
        return any(i.startswith(prefix) for i in state)


def forget_state(kind: str, name: str, scope: str = "optimizer") -> None:
    with _lock:
        if _load()["state"].pop(f"{scope}||{kind}|{name}", None) is not None:
            _save()


def subkeys(key_path: str) -> list:
    """Names of a registry key's sub-keys (read-only); [] if it can't be read."""
    hive, sub = _split(key_path)
    names: list = []
    if hive is None:
        return names
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
            while True:
                try:
                    names.append(winreg.EnumKey(k, len(names)))
                except OSError:
                    break
    except OSError:
        pass
    return names


# ── reset ─────────────────────────────────────────────────────────────
class suspended:
    """`with reg_originals.suspended():` — writes made inside are not recorded.
    Per thread: a game that starts while a reset is running on another thread
    must still have its own changes recorded, or they could never be undone."""
    def __enter__(self):
        _tls.suspended = getattr(_tls, "suspended", 0) + 1
    def __exit__(self, *exc):
        _tls.suspended = max(0, getattr(_tls, "suspended", 1) - 1)


def restore_all(scope: str = "optimizer") -> dict:
    """Put back everything `scope` has on record, then forget it.  Returns
    counts.  An item that fails to restore stays on record so a later reset
    retries it."""
    prefix = f"{scope}||"
    with _lock:
        data = _load()
        done = failed = 0
        for ident in [i for i in data["registry"] if i.startswith(prefix)]:
            key_path, _, value_name = ident[len(prefix):].rpartition("|")
            if _write_back(key_path, value_name, data["registry"][ident]):
                data["registry"].pop(ident); done += 1
            else:
                failed += 1
        touched_schemes = set()
        for ident in [i for i in data["power"] if i.startswith(prefix)]:
            scheme, sub, setting = ident[len(prefix):].split("|")
            rec = data["power"][ident]
            ok_ac = run_cmd(["powercfg", "/setacvalueindex", scheme, sub, setting, str(rec["ac"])]).get("ok")
            ok_dc = run_cmd(["powercfg", "/setdcvalueindex", scheme, sub, setting, str(rec["dc"])]).get("ok")
            if ok_ac and ok_dc:
                data["power"].pop(ident); done += 1
                touched_schemes.add(scheme)
            else:
                failed += 1
        if touched_schemes:         # (nothing to ask powercfg about otherwise)
            active = _active_scheme().lower()
            if active in touched_schemes:
                run_cmd(["powercfg", "/setactive", active])      # make the plan re-read its settings
        _save()
    log.info(f"[{scope}] restored {done} recorded original value(s)"
             + (f", {failed} failed" if failed else ""))
    return {"restored": done, "failed": failed}


def forget_notes(*names: str) -> None:
    with _lock:
        notes = _load()["notes"]
        for n in names:
            notes.pop(n, None)
        _save()


def summary() -> dict:
    with _lock:
        d = _load()
        return {"registry": len(d["registry"]), "power": len(d["power"]), "state": len(d["state"]),
                "notes": dict(d["notes"])}