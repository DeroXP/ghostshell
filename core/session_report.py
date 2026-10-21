"""What a game session looked like, for the game mode panel afterwards.

Measured while the game runs, at each game-mode tick:
  • how busy the P-cores and the E-cores were, from Windows' per-processor
    time counters;
  • how much of the CPU the game used (its processes' CPU time — readable
    through a query-only handle, which even anti-cheat-protected games
    grant) and how much everything else did (the rest of the busy time);
  • how many background programs were in efficiency mode.

What it cannot say is which core each background program ran on: Windows
only records that in a kernel trace, far too heavy to run beside a game.
What shows whether efficiency mode helps is the comparison with an earlier
session of the same game with it switched off: with it on, the same
background work turns up on the E-cores instead of the P-cores.

Reports are kept in game_profiles/session_reports.json, the last 20.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from config import APPDATA_DIR
from core import process_tuning
from core.utils import atomic_write_json, get_logger

log = get_logger("session_report")

_FILE = os.path.join(APPDATA_DIR, "game_profiles", "session_reports.json")
_KEEP = 20
_MIN_SECONDS = 60           # shorter than this says nothing
_lock = threading.Lock()
_state: dict = {}


def start(game: str, exe: str, eco_on: bool) -> None:
    with _lock:
        times = process_tuning.cpu_times()
        _state.clear()
        _state.update(game=game, exe=(exe or "").lower(), eco_on=bool(eco_on), started=time.time(),
                      classes=process_tuning.core_classes(), first=times, last=times,
                      game_cpu=0, game_last={}, ticks=0)


def tick(game_pids) -> None:
    """One reading while the game is running."""
    with _lock:
        if not _state:
            return
        for pid in game_pids or ():
            now = process_tuning.process_cpu(pid)
            if now is None:
                continue
            before = _state["game_last"].get(pid)
            if before is not None and now >= before:
                _state["game_cpu"] += now - before
            _state["game_last"][pid] = now
        times = process_tuning.cpu_times()
        if times and len(times) == len(_state["first"] or []):
            _state["last"] = times
            _state["ticks"] += 1


def _share(busy: int, total: int) -> Optional[float]:
    return round(100.0 * busy / total, 1) if total > 0 else None


def finish(eco_programs: int = 0, eco_processes: int = 0) -> Optional[dict]:
    """The report of the session that has just ended (up to its last tick
    with the game running), saved; None when there is not enough to say."""
    with _lock:
        st = dict(_state)
        _state.clear()
    if not st or not st.get("first") or not st.get("last"):
        return None
    first, last, classes = st["first"], st["last"], st.get("classes") or []
    seconds = (sum(t for _, t in last) - sum(t for _, t in first)) / 1e7 / max(1, len(first))
    if seconds < _MIN_SECONDS:
        return None
    sums = {True: [0, 0], False: [0, 0]}
    for i, ((b0, t0), (b1, t1)) in enumerate(zip(first, last)):
        is_p = classes[i] if i < len(classes) else True
        sums[is_p][0] += b1 - b0
        sums[is_p][1] += t1 - t0
    busy = sums[True][0] + sums[False][0]
    total = sums[True][1] + sums[False][1]
    game = min(st["game_cpu"], busy)
    report = {
        "game": st["game"], "exe": st["exe"], "ended": round(time.time()), "minutes": round(seconds / 60, 1),
        "p_busy": _share(*sums[True]), "e_busy": _share(*sums[False]),
        "game_share": _share(game, total), "other_share": _share(busy - game, total),
        "eco_on": st["eco_on"], "eco_programs": int(eco_programs), "eco_processes": int(eco_processes),
    }
    _save(report)
    log.info(f"Session report: {report}")
    return report


def _load() -> list:
    try:
        with open(_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(report: dict) -> None:
    try:
        atomic_write_json(_FILE, (_load() + [report])[-_KEEP:])
    except Exception as e:
        log.debug(f"could not save the session report: {e}")


def _line(r: dict) -> str:
    cores = f"P-cores {r['p_busy']}% busy" + (f", E-cores {r['e_busy']}% busy" if r.get("e_busy") is not None else "")
    return (f"{cores}; the game used {r['game_share']}% of the CPU, everything else {r['other_share']}%")


def latest() -> Optional[dict]:
    """The newest report, with the lines the panel shows — and the last
    session of the same game with efficiency mode the other way round, to
    compare with."""
    reports = _load()
    if not reports:
        return None
    r = dict(reports[-1])
    when = time.strftime("%b %d %H:%M", time.localtime(r["ended"]))
    lines = [f"Last session: {r['game']}, {r['minutes']} min (ended {when})", _line(r)]
    if r.get("eco_on"):
        lines.append(f"Background programs on the efficiency cores: {r['eco_programs']} "
                     f"({r['eco_processes']} processes)")
    else:
        lines.append("Background programs on the efficiency cores: off")
    other = next((o for o in reversed(reports[:-1])
                  if o.get("exe") == r.get("exe") and bool(o.get("eco_on")) != bool(r.get("eco_on"))), None)
    if other:
        lines.append(f"Same game with efficiency cores {'on' if other.get('eco_on') else 'off'} "
                     f"({time.strftime('%b %d', time.localtime(other['ended']))}): {_line(other)}")
    r["lines"] = lines
    return r
