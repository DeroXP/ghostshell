"""One-time undo of what the removed Gamepad Mapper left in HidHide.

Versions up to 3.6.8 drove HidHide (a driver that hides controllers from
games) through its command-line tool: they hid the user's controller, added
Vispora to HidHide's list of allowed programs and turned its cloak on.
HidHide keeps all of that across restarts, so it outlives the app.  This
module takes Vispora's own changes back, once, and leaves every other entry
in HidHide's lists alone.  It never uninstalls HidHide or the ViGEm Bus
Driver: other tools (DS4Windows and the like) may rely on them.

run_once() is called on a worker thread at every start; after one complete
run (or HidHide not being installed) it only reads its done file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
from datetime import datetime

import config
from core import utils

log = utils.get_logger("hidhide_cleanup")

CLI_CANDIDATES = [
    r"C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    r"C:\Program Files\Nefarius Software Solutions\HidHide\HidHideCLI.exe",
    r"C:\Program Files (x86)\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
]

# Written by the old mapper while it had HidHide engaged:
#   {"prior_state": {"cloaked": bool, "hidden_devices": [lines],
#                    "whitelisted_apps": [lines]},
#    "hidden_ours": [device instance ids], "whitelisted_app": "<path>" | ""}
MARKER_NAME = "hidhide_engaged.json"
DONE_NAME = "hidhide_cleanup_done.json"
MAX_ATTEMPTS = 5

_CLI_TIMEOUT = 20
_LOG_KEEP = 200
# The device-path form the old mapper passed to --dev-hide (see
# _to_symbolic_link).  Only Vispora wrote entries like these.
_VISPORA_DEVICE_PREFIXES = ("\\\\?\\hid#", "\\\\?\\usb#")

_lock = threading.Lock()


def _find_cli() -> str | None:
    for path in CLI_CANDIDATES:
        try:
            if os.path.isfile(path):
                return path
        except Exception:
            pass
    return None


def _marker_path() -> str:
    return os.path.join(config.APPDATA_DIR, MARKER_NAME)


def _done_path() -> str:
    return os.path.join(config.APPDATA_DIR, DONE_NAME)


def _to_symbolic_link(instance_id: str) -> str:
    """The form the old mapper turned an instance id into before passing it
    to --dev-hide: lower case, backslashes as '#', prefixed with '\\\\?\\'.
    HID\\VID_054C&PID_0CE6\\6&1bce44cb&0&0000 became
    \\\\?\\hid#vid_054c&pid_0ce6#6&1bce44cb&0&0000."""
    if not instance_id:
        return ""
    return "\\\\?\\" + instance_id.replace("\\", "#").lower()


# ── the CLI and its output ────────────────────────────────────────────
def _run(cli: str, *args: str) -> dict:
    """One CLI call.  utils.run_cmd is looked up now, not at import, so the
    test sandbox's stand-in is the one used.  stdout and stderr together are
    the output."""
    try:
        r = utils.run_cmd([cli, *args], timeout=_CLI_TIMEOUT) or {}
    except Exception as e:
        return {"ok": False, "out": str(e)}
    out = "\n".join(x for x in (r.get("out") or "", r.get("err") or "") if x)
    return {"ok": bool(r.get("ok")), "out": out.replace("\x00", "").strip()}


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _tokens(text: str) -> list:
    try:
        return shlex.split(text or "", posix=False)
    except ValueError:
        # An unbalanced quote (an unquoted path with an apostrophe in it):
        # quoted strings or runs of non-blanks will do.
        return re.findall(r'"[^"]*"|\S+', text or "")


def _parse_entries(text: str, flag: str) -> list:
    """What a --dev-list / --app-list run lists.  Current CLIs print
    replayable commands (--dev-hide "HID\\...", --app-reg "C:\\...exe"):
    take the argument after each `flag`.  Older ones printed one bare path
    per line."""
    text = (text or "").replace("\x00", "")
    tokens = _tokens(text)
    if any(t.startswith("--") for t in tokens):
        out = []
        for i, tok in enumerate(tokens[:-1]):
            if tok.lower() == flag:
                arg = _unquote(tokens[i + 1])
                if arg and not arg.startswith("--"):
                    out.append(arg)
        return out
    return [arg for arg in (_unquote(line) for line in text.splitlines()) if arg]


def _cloak_is_on(text: str) -> bool:
    return any(t.lower() == "--cloak-on" for t in _tokens(text))


def _cloak_is_off(text: str) -> bool:
    toks = [t.lower() for t in _tokens(text)]
    return "--cloak-off" in toks and "--cloak-on" not in toks


def _is_vispora_device(arg: str) -> bool:
    return arg == arg.lower() and arg.startswith(_VISPORA_DEVICE_PREFIXES)


def _is_own_app(arg: str) -> bool:
    return os.path.basename(arg).lower() in config.OWN_PROCESS_NAMES


# ── state files ───────────────────────────────────────────────────────
def _load_done() -> dict:
    try:
        with open(_done_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_done(done: bool, attempts: int, entries: list) -> None:
    utils.atomic_write_json(_done_path(), {
        "done": bool(done), "attempts": int(attempts),
        "last": datetime.now().isoformat(timespec="seconds"),
        "log": [str(e) for e in entries][-_LOG_KEEP:],
    })


def _read_marker() -> tuple:
    """(present, contents).  An unreadable marker is present but empty:
    the device and app rules below still run, the cloak is left alone."""
    path = _marker_path()
    if not os.path.exists(path):
        return False, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return True, (data if isinstance(data, dict) else {})
    except (OSError, ValueError):
        return True, {}


def _as_int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


# ── the cleanup ───────────────────────────────────────────────────────
def run_once() -> dict:
    """Undo the old mapper's HidHide changes, if that is still to do.
    Returns {"ok", "done", "attempts", "changed", "failed", "log"}, or
    {"ok", "skipped": "running" | "done" | "not installed" | "gave up"}.
    Never raises."""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "skipped": "running"}
    try:
        return _run_locked()
    except Exception as e:
        log.warning(f"legacy HidHide cleanup failed: {e}")
        return {"ok": False, "err": str(e)}
    finally:
        _lock.release()


def _run_locked() -> dict:
    state = _load_done()
    if state.get("done"):
        return {"ok": True, "skipped": "done"}
    attempts = _as_int(state.get("attempts"))
    if attempts >= MAX_ATTEMPTS:
        return {"ok": False, "skipped": "gave up", "attempts": attempts}
    attempts += 1
    old_log = state.get("log")
    history = [e for e in old_log if isinstance(e, str)] if isinstance(old_log, list) else []
    # Counted before the work, so a run that never finishes (the app quit,
    # the CLI hung) still uses up one of the attempts.
    _save_done(False, attempts, history)

    cli = _find_cli()
    if not cli:
        _save_done(True, attempts, history + [f"attempt {attempts}: HidHide is not installed"])
        return {"ok": True, "skipped": "not installed"}

    run_log = [f"attempt {attempts}: {datetime.now().isoformat(timespec='seconds')}"]
    changed, failed = _cleanup(cli, run_log)
    ok = not failed
    _save_done(ok, attempts, history + run_log)
    if ok:
        log.info(f"legacy HidHide cleanup done ({len(changed)} change(s))")
    else:
        log.warning(f"legacy HidHide cleanup attempt {attempts} of {MAX_ATTEMPTS} "
                    f"incomplete: {'; '.join(failed)[:300]}")
    return {"ok": ok, "done": ok, "attempts": attempts, "changed": changed,
            "failed": failed, "log": run_log}


def _cleanup(cli: str, run_log: list) -> tuple:
    changed: list = []
    failed: list = []

    present, marker = _read_marker()
    prior = marker.get("prior_state") if isinstance(marker.get("prior_state"), dict) else {}
    ours = marker.get("hidden_ours")
    hidden_ours = [s.strip() for s in ours if isinstance(s, str) and s.strip()] if isinstance(ours, list) else []
    marker_app = marker.get("whitelisted_app")
    marker_app = marker_app.strip() if isinstance(marker_app, str) else ""
    # Devices that were hidden before the mapper engaged were the user's own.
    prior_lines = prior.get("hidden_devices") if isinstance(prior.get("hidden_devices"), list) else []
    prior_devices = {d.lower() for d in _parse_entries(
        "\n".join(str(x) for x in prior_lines), "--dev-hide")}

    devs = _run(cli, "--dev-list")
    apps = _run(cli, "--app-list")
    listed_devs = _parse_entries(devs["out"], "--dev-hide") if devs["ok"] else []
    listed_apps = _parse_entries(apps["out"], "--app-reg") if apps["ok"] else []
    if not devs["ok"]:
        failed.append(f"couldn't list hidden devices: {devs['out'][:200]}")
    if not apps["ok"]:
        failed.append(f"couldn't list allowed programs: {apps['out'][:200]}")

    # Devices to un-hide: the \\?\hid#... form of each marker device, the
    # only form the old mapper ever passed to --dev-hide, plus any leftover
    # entry in that form.  The plain instance path (HID\VID_...\...) is never
    # touched: Vispora never wrote it, so an entry like that is the user's or
    # another tool's (DSX, BetterJoy and the like hide pads that way).
    dev_targets: list = []
    for iid in hidden_ours:
        form = _to_symbolic_link(iid)
        if form and form not in dev_targets and form not in prior_devices:
            dev_targets.append(form)
    for arg in listed_devs:
        if _is_vispora_device(arg) and arg not in dev_targets:
            dev_targets.append(arg)

    app_targets: list = []
    if marker_app:
        app_targets.append(marker_app)
    for arg in listed_apps:
        if _is_own_app(arg) and arg.lower() not in {a.lower() for a in app_targets}:
            app_targets.append(arg)

    dev_ok: dict = {}
    for form in dev_targets:
        r = _run(cli, "--dev-unhide", form)
        dev_ok[form] = r["ok"]
        run_log.append(f"--dev-unhide {form}: " + ("ok" if r["ok"] else f"refused ({r['out'][:120]})"))
    app_ok: dict = {}
    for path in app_targets:
        r = _run(cli, "--app-unreg", path)
        app_ok[path] = r["ok"]
        run_log.append(f"--app-unreg {path}: " + ("ok" if r["ok"] else f"refused ({r['out'][:120]})"))

    # The cloak only goes back off when the marker says it was off before
    # the mapper turned it on.  Without a marker it is never touched.
    if present and "cloaked" in prior and not prior.get("cloaked"):
        st = _run(cli, "--cloak-state")
        if st["ok"] and _cloak_is_off(st["out"]):
            run_log.append("cloak already off")
        else:
            r = _run(cli, "--cloak-off")
            run_log.append("--cloak-off: " + ("ok" if r["ok"] else f"refused ({r['out'][:120]})"))
            if r["ok"]:
                changed.append("cloak off")
            else:
                failed.append("couldn't turn the cloak off")

    # Check against the lists again: the CLI may refuse to remove an entry
    # it no longer has (a marker device that is already un-hidden), and a
    # listed entry it said it removed must really be gone.
    after_devs = after_apps = None
    if dev_targets:
        r = _run(cli, "--dev-list")
        if r["ok"]:
            after_devs = {d.lower() for d in _parse_entries(r["out"], "--dev-hide")}
    if app_targets:
        r = _run(cli, "--app-list")
        if r["ok"]:
            after_apps = {a.lower() for a in _parse_entries(r["out"], "--app-reg")}

    for form in dev_targets:
        if after_devs is not None:
            gone = form.lower() not in after_devs
        else:
            gone = dev_ok.get(form, False)
        if gone:
            if dev_ok.get(form):
                changed.append(f"un-hid {form}")
        else:
            failed.append(f"{form} is still hidden")
    for path in app_targets:
        if after_apps is not None:
            gone = path.lower() not in after_apps
        else:
            gone = app_ok.get(path, False)
        if gone:
            if app_ok.get(path):
                changed.append(f"unregistered {path}")
        else:
            failed.append(f"{path} is still on the allowed list")

    if present and not failed:
        try:
            os.remove(_marker_path())
            run_log.append("marker removed")
        except FileNotFoundError:
            pass
        except OSError as e:
            failed.append(f"couldn't remove the marker: {e}")
    for f in failed:
        run_log.append(f"not done: {f}")
    return changed, failed
