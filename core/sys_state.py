"""Prior state of the things a tweak changes that are NOT registry values or
power-plan settings: service start types, scheduled tasks, boot (BCD)
entries, global TCP parameters, optional Windows features, NTFS behaviour
flags and Defender preferences.

Same contract as core/reg_originals.py, which stores the records: the prior
state is noted the first time something is REALLY being changed, and a reset
puts back exactly that.  Without this, "Reset All" wrote guessed defaults for
these — several of them wrong (Xbox services were set to Automatic, they
ship Manual; two services were "restored" with a start keyword sc.exe does
not accept) — and never undid others at all: the scheduled tasks and Windows
features the optimizer switches off stayed off for good.

Every reader below is language-independent on purpose (registry values, task
XML, the script `netsh ... dump` prints, DISM /English, PowerShell enum
values).  A reader that parses English labels records nothing on a German
or Japanese Windows, and fails silently.

Restores only act on names registered with allow().  The records live in a
file under the user's profile; this keeps a damaged or tampered file from
turning Reset All into "reconfigure any service / boot entry you like".
"""
from __future__ import annotations

import json
import re
from typing import Optional

from core import reg_originals
from core.utils import run_cmd, run_ps, get_logger, is_safe_ps_literal

log = get_logger("sys_state")

_allowed: dict = {}


def allow(kind: str, names) -> None:
    """Register names this app itself changes, per kind.  Called at import by
    the modules that own the lists (and by every setter, for its own name)."""
    _allowed.setdefault(kind, set()).update(str(n).lower() for n in names)


def _ok(result) -> bool:
    return bool(result and result.get("ok"))


# ── services ──────────────────────────────────────────────────────────
_SVC_ROOT = r"HKLM\SYSTEM\CurrentControlSet\Services"
SERVICE_MODES = ("auto", "delayed-auto", "demand", "disabled")      # sc.exe's own keywords


def service_start(name: str) -> Optional[str]:
    """A service's start type as sc.exe spells it, or None if it is not
    installed (or is a boot/system driver, which nothing here touches)."""
    v = reg_originals.read_value(rf"{_SVC_ROOT}\{name}", "Start")
    if not v or v.get("type") != "REG_DWORD":
        return None
    if v["data"] == 2:
        d = reg_originals.read_value(rf"{_SVC_ROOT}\{name}", "DelayedAutostart") or {}
        return "delayed-auto" if d.get("data") == 1 else "auto"
    return {3: "demand", 4: "disabled"}.get(v["data"])


def set_service_start(name: str, mode: str, stop: bool = False, scope: str = "optimizer") -> bool:
    """`sc config <name> start= <mode>` (and `sc stop`), noting the start
    type it had first."""
    allow("service", [name])
    cur = service_start(name)
    if cur is not None and cur != mode:
        reg_originals.remember_state("service", name, cur, scope)
    if stop:
        run_cmd(["sc", "stop", name])
    return _ok(run_cmd(["sc", "config", name, "start=", mode]))


def _put_service(name: str, prior) -> bool:
    if prior not in SERVICE_MODES:
        return False
    if not _ok(run_cmd(["sc", "config", name, "start=", prior])):
        return False
    if prior in ("auto", "delayed-auto"):
        run_cmd(["sc", "start", name])      # it used to be running; a reboot would start it anyway
    return True


# ── scheduled tasks ───────────────────────────────────────────────────
def task_enabled(path: str) -> Optional[bool]:
    """Is the task enabled?  None if it does not exist or cannot be read.
    From the task's XML — the Status column of a plain query is localised."""
    r = run_cmd(["schtasks", "/Query", "/TN", path, "/XML"])
    if not _ok(r):
        return None
    settings = re.search(r"<Settings>(.*?)</Settings>", r.get("out") or "", re.S | re.I)
    if not settings:
        return None
    flag = re.search(r"<Enabled>\s*(true|false)\s*</Enabled>", settings.group(1), re.I)
    return True if flag is None else flag.group(1).lower() == "true"    # the element is optional; absent = enabled


def disable_task(path: str, scope: str = "optimizer") -> bool:
    allow("task", [path])
    if task_enabled(path):      # only a task that is enabled now is ours to switch back on later
        reg_originals.remember_state("task", path, True, scope)
    return _ok(run_cmd(["schtasks", "/Change", "/TN", path, "/Disable"]))


def _put_task(path: str, prior) -> bool:
    return _ok(run_cmd(["schtasks", "/Change", "/TN", path, "/Enable"]))


# ── boot configuration (BCD) ──────────────────────────────────────────
_BCD_YES, _BCD_NO = {"yes", "on", "true", "1"}, {"no", "off", "false", "0"}


def bcd_values() -> Optional[dict]:
    """{element: value} of the running OS's boot entry, or None if the store
    cannot be read.  Element names and values are not localised."""
    r = run_cmd(["bcdedit", "/enum", "{current}"])
    if not _ok(r):
        return None
    vals = {}
    lines = (r.get("out") or "").splitlines()
    for i, line in enumerate(lines):
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt and set(nxt) == {"-"}:
            continue                        # a section heading ("Windows Boot Loader"), underlined
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*)\s+(\S.*?)\s*$", line)
        if m:
            vals[m.group(1).lower()] = m.group(2)
    # A real loader entry always says where Windows is; anything else is an
    # error message that happened to look like "word  words".
    return vals if ("osdevice" in vals or "systemroot" in vals) else None


def bcd_same(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or (a in _BCD_YES and b in _BCD_YES) or (a in _BCD_NO and b in _BCD_NO)


def set_bcd(name: str, value: str, scope: str = "optimizer", current: Optional[dict] = None) -> bool:
    """`bcdedit /set {current} <name> <value>`, noting what the element was
    (None when it was not set at all).  Pass `current` from one bcd_values()
    call when setting several."""
    allow("bcd", [name])
    vals = current if current is not None else bcd_values()
    if vals is not None:
        cur = vals.get(name.lower())
        if cur is None or not bcd_same(cur, value):
            reg_originals.remember_state("bcd", name, cur, scope)
    return _ok(run_cmd(["bcdedit", "/set", "{current}", name, value]))


def _put_bcd(name: str, prior) -> bool:
    if prior is None:
        if _ok(run_cmd(["bcdedit", "/deletevalue", "{current}", name])):
            return True
        vals = bcd_values()                 # deletevalue also fails when it is already gone
        return vals is not None and name.lower() not in vals
    if not re.fullmatch(r"[A-Za-z0-9]{1,40}", str(prior)):
        return False
    return _ok(run_cmd(["bcdedit", "/set", "{current}", name, str(prior)]))


# ── global TCP parameters ─────────────────────────────────────────────
def tcp_globals() -> Optional[dict]:
    """{parameter: value} from `netsh int tcp dump`, which prints the current
    configuration as the very `set global a=b c=d` command that recreates it
    — the same text in every display language, unlike `show global`."""
    r = run_cmd(["netsh", "int", "tcp", "dump"])
    if not _ok(r):
        return None
    vals = {}
    for line in (r.get("out") or "").splitlines():
        parts = line.strip().split()
        if [p.lower() for p in parts[:2]] == ["set", "global"]:
            for tok in parts[2:]:
                k, sep, v = tok.partition("=")
                if sep and v:
                    vals[k.lower()] = v
    return vals or None


def set_tcp_global(name: str, value, scope: str = "optimizer", current: Optional[dict] = None) -> bool:
    allow("tcp", [name])
    vals = current if current is not None else tcp_globals()
    cur = (vals or {}).get(name.lower())
    if cur is not None and cur.lower() != str(value).lower():
        reg_originals.remember_state("tcp", name, cur, scope)
    return _ok(run_cmd(["netsh", "int", "tcp", "set", "global", f"{name}={value}"]))


def _put_tcp(name: str, prior) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9]{1,24}", str(prior)):
        return False
    return _ok(run_cmd(["netsh", "int", "tcp", "set", "global", f"{name}={prior}"]))


# ── IPv6 transition tunnels, TCP window-scaling heuristics ────────────
# name: (the netsh command that sets it, minus the value; the values it takes)
_IPSTACK = {
    "teredo":     (["netsh", "interface", "teredo", "set", "state"],
                   ("disabled", "default", "client", "enterpriseclient", "natawareclient", "server")),
    "6to4":       (["netsh", "interface", "6to4", "set", "state"], ("disabled", "enabled", "default")),
    "isatap":     (["netsh", "interface", "isatap", "set", "state"], ("disabled", "enabled", "default")),
    "heuristics": (["netsh", "int", "tcp", "set", "heuristics"], ("disabled", "enabled", "default")),
}
# Read through the NetTCPIP cmdlets: their values are enum names, where
# `netsh ... show state` prints localised text.  Each one on its own, so a
# cmdlet this Windows lacks costs only its own entry.
_IPSTACK_READ = (
    "$o = @{}; "
    "try { $o['teredo'] = [string](Get-NetTeredoConfiguration -ErrorAction Stop).Type } catch {}; "
    "try { $o['6to4'] = [string](Get-Net6to4Configuration -ErrorAction Stop).State } catch {}; "
    "try { $o['isatap'] = [string](Get-NetIsatapConfiguration -ErrorAction Stop).State } catch {}; "
    "try { $o['heuristics'] = [string](Get-NetTCPSetting -SettingName Internet -ErrorAction Stop).ScalingHeuristics } catch {}; "
    "$o | ConvertTo-Json -Compress"
)


def ip_stack_states() -> Optional[dict]:
    """{"teredo" | "6to4" | "isatap" | "heuristics": state, lower-cased}, or
    None if nothing could be read."""
    r = run_ps(_IPSTACK_READ, timeout=30)
    try:
        data = json.loads(r.get("out") or "") if _ok(r) else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None
    return {k: str(v).lower() for k, v in data.items() if k in _IPSTACK and v} or None


def set_ip_stack_state(name: str, value: str, scope: str = "optimizer",
                       current: Optional[dict] = None) -> bool:
    """Set one of the _IPSTACK switches, noting the state it was in.  A state
    netsh could not be told to go back to (Teredo "relay", say) is not noted."""
    cmd, accepted = _IPSTACK[name]
    allow("ipstack", [name])
    vals = current if current is not None else ip_stack_states()
    cur = (vals or {}).get(name)
    if cur in accepted and cur != value:
        reg_originals.remember_state("ipstack", name, cur, scope)
    return _ok(run_cmd(cmd + [value]))


def _put_ip_stack(name: str, prior) -> bool:
    if name not in _IPSTACK or prior not in _IPSTACK[name][1]:
        return False
    return _ok(run_cmd(_IPSTACK[name][0] + [prior]))


# ── optional Windows features ─────────────────────────────────────────
def enabled_features() -> Optional[set]:
    """Lower-cased names of the optional features that are on, or None if
    DISM cannot be asked.  One call for the whole list."""
    r = run_cmd(["dism", "/online", "/english", "/get-features", "/format:table"], timeout=240)
    if not _ok(r):
        return None
    on, rows = set(), 0
    for line in (r.get("out") or "").splitlines():
        name, sep, state = line.partition("|")
        name, state = name.strip(), state.strip().lower()
        if not sep or not name or name.startswith("-") or name.lower() == "feature name":
            continue
        rows += 1
        if state.startswith("enable"):          # "Enabled", "Enable Pending"
            on.add(name.lower())
    return on if rows else None


def disable_features(names, scope: str = "optimizer") -> int:
    """Switch optional features off; returns how many were actually on.  A
    feature that is already off is skipped — each DISM call takes seconds."""
    names = list(names)
    allow("feature", names)
    on = enabled_features()
    changed = 0
    for n in names:
        if on is not None:
            if n.lower() not in on:
                continue
            reg_originals.remember_state("feature", n, True, scope)
        changed += 1
        run_cmd(["dism", "/online", "/disable-feature", f"/featurename:{n}", "/norestart"], timeout=600)
    return changed


def _put_feature(name: str, prior) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name):
        return False
    r = run_cmd(["dism", "/online", "/english", "/enable-feature", f"/featurename:{name}", "/all", "/norestart"],
                timeout=900)
    # DISM exits 3010, not 0, when the change needs a restart to finish.
    return _ok(r) or "completed successfully" in (r.get("out") or "").lower()


# ── NTFS behaviour flags ──────────────────────────────────────────────
def fsutil_behavior(name: str) -> Optional[int]:
    # "DisableLastAccess = 1 (...)", but also "The registry state is: 1 (...)"
    r = run_cmd(["fsutil", "behavior", "query", name])
    m = re.search(r"[=:]\s*(\d+)", r.get("out") or "") if _ok(r) else None
    return int(m.group(1)) if m else None


def set_fsutil_behavior(name: str, value: int, scope: str = "optimizer") -> bool:
    allow("fsutil", [name])
    cur = fsutil_behavior(name)
    if cur is not None and cur != int(value):
        reg_originals.remember_state("fsutil", name, cur, scope)
    return _ok(run_cmd(["fsutil", "behavior", "set", name, str(int(value))]))


def _put_fsutil(name: str, prior) -> bool:
    if isinstance(prior, bool) or not isinstance(prior, int) or not 0 <= prior <= 9:
        return False
    return _ok(run_cmd(["fsutil", "behavior", "set", name, str(prior)]))


# ── Microsoft Defender preferences ────────────────────────────────────
_DEFENDER_READ = (
    "$p = Get-MpPreference -ErrorAction Stop; "
    "@{ExclusionPath=@($p.ExclusionPath | Where-Object {$_}); "
    "ExclusionProcess=@($p.ExclusionProcess | Where-Object {$_}); "
    "SubmitSamplesConsent=[int]$p.SubmitSamplesConsent; MAPSReporting=[int]$p.MAPSReporting; "
    "ScanScheduleTime=[string]$p.ScanScheduleTime; "
    "ScanScheduleQuickScanTime=[string]$p.ScanScheduleQuickScanTime} | ConvertTo-Json -Compress"
)
_DEFENDER_ENUMS = ("SubmitSamplesConsent", "MAPSReporting")
_DEFENDER_TIMES = ("ScanScheduleTime", "ScanScheduleQuickScanTime")


def defender_prefs() -> Optional[dict]:
    r = run_ps(_DEFENDER_READ, timeout=60)
    try:
        data = json.loads(r.get("out") or "") if _ok(r) else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else None


def set_defender_pref(name: str, value: str, reads_back_as, scope: str = "optimizer",
                      current: Optional[dict] = None) -> bool:
    """`Set-MpPreference -<name> <value>`.  `reads_back_as` is how that value
    looks in defender_prefs() (an int for the enums, "HH:MM:SS" for times)."""
    allow("defender", [name])
    if current and current.get(name) is not None and current[name] != reads_back_as:
        reg_originals.remember_state("defender", name, current[name], scope)
    return _ok(run_ps(f"Set-MpPreference -{name} {value} -ErrorAction SilentlyContinue"))


def _put_defender_pref(name: str, prior) -> bool:
    if name in _DEFENDER_ENUMS:
        if isinstance(prior, bool) or not isinstance(prior, int) or not 0 <= prior <= 9:
            return False
    elif name in _DEFENDER_TIMES:
        if not isinstance(prior, str) or not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", prior):
            return False
    else:
        return False
    return _ok(run_ps(f"Set-MpPreference -{name} {prior} -ErrorAction SilentlyContinue"))


def add_defender_exclusion(kind: str, value: str, scope: str = "optimizer",
                           current: Optional[dict] = None) -> bool:
    """kind: "ExclusionPath" or "ExclusionProcess".  Only an exclusion that
    was not already there is recorded — so a reset removes what we added and
    leaves the user's own exclusions alone."""
    if kind not in ("ExclusionPath", "ExclusionProcess") or not is_safe_ps_literal(value):
        return False
    name = f"{kind}:{value}"
    allow("defender-exclusion", [name])
    if current is not None:
        have = {str(x).lower() for x in (current.get(kind) or [])}
        if value.lower() not in have:
            reg_originals.remember_state("defender-exclusion", name, True, scope)
    return _ok(run_ps(f'Add-MpPreference -{kind} "{value}" -ErrorAction SilentlyContinue'))


def _put_exclusion(name: str, prior) -> bool:
    kind, _, value = name.partition(":")
    if kind not in ("ExclusionPath", "ExclusionProcess") or not is_safe_ps_literal(value):
        return False
    return _ok(run_ps(f'Remove-MpPreference -{kind} "{value}" -ErrorAction SilentlyContinue'))


# ── reset ─────────────────────────────────────────────────────────────
_RESTORERS = (      # kind, how the result line reads, how one record is put back
    ("service",            "{n} service(s) back to their previous start type", _put_service),
    ("task",               "{n} scheduled task(s) re-enabled",                 _put_task),
    ("bcd",                "{n} boot setting(s) restored",                     _put_bcd),
    ("tcp",                "{n} TCP setting(s) restored",                      _put_tcp),
    ("ipstack",            "{n} IPv6-tunnel / TCP-heuristics setting(s) restored", _put_ip_stack),
    ("fsutil",             "{n} file-system setting(s) restored",              _put_fsutil),
    ("defender",           "{n} Defender preference(s) restored",              _put_defender_pref),
    ("defender-exclusion", "{n} Defender exclusion(s) removed",                _put_exclusion),
    ("feature",            "{n} Windows feature(s) switched back on",          _put_feature),
)


def restore_kind(kind: str, scope: str = "optimizer", only: Optional[str] = None) -> dict:
    """Put back every record of one kind (or just the one named `only`),
    forgetting each as it succeeds.  A record that fails stays, so the next
    reset tries again."""
    put = {k: p for k, _, p in _RESTORERS}[kind]
    done = failed = 0
    for name, prior in reg_originals.state_records(kind, scope).items():
        if only is not None and name != only:
            continue
        if name.lower() not in _allowed.get(kind, ()):
            log.warning(f"dropping a {kind} record for {name!r}: not something this app changes")
            reg_originals.forget_state(kind, name, scope)
            continue
        try:
            good = bool(put(name, prior))
        except Exception as e:
            log.warning(f"could not restore {kind} {name}: {e}")
            good = False
        if good:
            reg_originals.forget_state(kind, name, scope)
            done += 1
        else:
            failed += 1
    if done or failed:
        log.info(f"[{scope}] {kind}: restored {done}" + (f", {failed} failed" if failed else ""))
    return {"restored": done, "failed": failed}


def restore_all(scope: str = "optimizer") -> list:
    """Put back everything on record for `scope`.  Returns one result row
    ({"name", "ok"}) per kind that had something to restore."""
    rows = []
    for kind, label, _ in _RESTORERS:
        if not reg_originals.has_state(kind, scope=scope):
            continue
        r = restore_kind(kind, scope)
        if r["restored"] or r["failed"]:
            rows.append({"name": label.format(n=r["restored"])
                                 + (f" ({r['failed']} could not be)" if r["failed"] else ""),
                         "ok": r["failed"] == 0})
    return rows