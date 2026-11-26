"""What has moved from the backend into the shell: the network monitor.

The third piece of step 4 (docs/rust-port.md) is core/net_monitor.py: how
many bytes a second the network cards move, which programs move the most,
and a warning when a program other than the game moves more than 5 MB a
second for three seconds in a row while a game runs.  It runs while the
Network page reads it, and stops by itself half a minute after the last
read.  The shell (shell_rs/core/src/moved/net.rs) answers the three calls
that read it (live, history, top).  Pausing and resuming a program change
the PC and stay the backend's.

"The same answers, proven on the same fake machine before anything is
removed": the Python module and the shell are asked the same things, one
after the other, on the same made-up machine, and every answer is compared,
as for the sampler and the hardware monitor.

  * The machine is: the network cards and their byte counts (what psutil's
    net_io_counters() reads, and the helper's network_cards), every
    program's bytes (the helper's process_io, and psutil's process_iter
    where the helper has none), the game the game monitor says runs, and
    two clocks set by hand.
  * Python's psutil is a made-up one that reads that machine.  What psutil
    does with the counts in Python (a card of two names, a count that goes
    back) is psutil's own code where psutil is installed (the Python
    releases are built with), and otherwise this file's copy of it, which
    `test_the_copy_of_psutils_wrap_is_psutils` holds against psutil's own.
  * The Python module is the real one, its routes called through Flask,
    its sampler's loop run a round at a time.
  * The shell's side is the real code, in the trial tool.  Its door
    answers from the same machine; where it asks its backend for a part of
    a pass (which game runs; the programs, where the helper has none), the
    tool puts the call to this test, which answers it with the Python
    app's own route.

Nothing of the PC is read, and no program is paused or resumed: those two
calls are never made here.
"""
import sandbox as m

import json
import os
import re
import subprocess
import sys
import threading
import types
import unittest

import test_shell_rs as S

try:
    import psutil as _real_psutil                   # (only for its wrap code: it never reads this PC here)
    PSUTILS_WRAP = _real_psutil._common._WrapNumbers
except ImportError:
    _real_psutil, PSUTILS_WRAP = None, None

try:
    import app as A
    import config
    NM = m.app("core.net_monitor")
    GP = m.app("core.game_profiles")
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


def tool():
    app()
    return S.TRY


class OurWrap:
    """psutil's _WrapNumbers (psutil/_common.py), as this file copies it for
    a Python without psutil: a count that is lower than the last one is
    taken to have started again from nought, and the last one is added on
    from then on; a card that is gone takes what was added on along."""

    def __init__(self):
        self.cache, self.reminders, self.reminder_keys = {}, {}, {}

    def run(self, input_dict, name):
        if name not in self.cache:
            self.cache[name] = input_dict
            self.reminders[name], self.reminder_keys[name] = {}, {}
            return input_dict
        old_dict = self.cache[name]
        for gone in set(old_dict) - set(input_dict):
            for remkey in self.reminder_keys[name].pop(gone, set()):
                self.reminders[name].pop(remkey, None)
        new_dict = {}
        for key, values in input_dict.items():
            if key not in old_dict:
                new_dict[key] = values
                continue
            bits = []
            for i, value in enumerate(values):
                remkey = (key, i)
                if value < old_dict[key][i]:
                    self.reminders[name][remkey] = self.reminders[name].get(remkey, 0) + old_dict[key][i]
                    self.reminder_keys[name].setdefault(key, set()).add(remkey)
                bits.append(value + self.reminders[name].get(remkey, 0))
            new_dict[key] = tuple(bits)
        self.cache[name] = input_dict
        return new_dict


Wrap = PSUTILS_WRAP or OurWrap


class Machine(types.ModuleType):
    """psutil, made up: it reads the made-up machine's cards and programs."""

    def __init__(self, side):
        super().__init__("psutil")
        self.side = side

    def net_io_counters(self):
        cards = self.side.cards
        if cards is None:
            raise RuntimeError("GetIfEntry or GetIfEntry2 call failed.")
        raw = {}
        for name, sent, recv in cards:
            raw[name] = (sent, recv, 0, 0, 0, 0, 0, 0)
        if not raw:
            return None
        raw = self.side.wrap.run(raw, "psutil.net_io_counters")
        sums = [sum(x) for x in zip(*raw.values())]
        return types.SimpleNamespace(bytes_sent=sums[0], bytes_recv=sums[1])

    def process_iter(self, attrs=None):
        if self.side.psutil_rows == "raises":
            raise RuntimeError("could not list the processes")
        for pid, name, other in self.side.psutil_rows:
            yield types.SimpleNamespace(info={"pid": pid, "name": name, "io_counters": None if other is None else types.SimpleNamespace(other_bytes=other)})


class OneRound:
    """The sampler's stop event, for one round of its loop: the loop asks it
    whether to stop before the round, under the sampling lock, after the
    wait (which takes no time), and once more, which ends the round."""

    def __init__(self, real):
        self.real, self.waited, self.after = real, False, 0

    def is_set(self):
        if self.waited:
            self.after += 1
            if self.after >= 2:
                return True
        return self.real.is_set()

    def wait(self, _seconds):
        self.waited = True
        return self.real.is_set()

    def set(self):
        self.real.set()


INITIAL = {"total_rx_per_sec": 0, "total_tx_per_sec": 0, "top": [], "burst_alert": None, "last_sampled_at": 0}


class Python:
    """core/net_monitor.py behind its routes, on the made-up machine."""

    def __init__(self):
        self.pc = self.steady = 0.0
        self.cards, self.helper_rows, self.psutil_rows, self.game = None, None, [], None
        self.wrap = Wrap()
        self.attempts = self.kept = self.dropped = 0
        self.parts = []
        self.saved = (NM.time, NM.threading, NM._sample_once, sys.modules.get("psutil"), GP.get_active_game_info, dict(m.NATIVE))
        NM.time = types.SimpleNamespace(time=lambda: self.pc, monotonic=lambda: self.steady)
        NM.threading = types.SimpleNamespace(Event=threading.Event, Thread=self.thread, Lock=threading.Lock)
        original = self.saved[2]

        def counting():
            self.attempts += 1
            try:
                original()
            except Exception:
                self.dropped += 1
                raise
            self.kept += 1
        NM._sample_once = counting
        sys.modules["psutil"] = Machine(self)
        GP.get_active_game_info = lambda: {"exe": self.game, "display": "A game", "pid": 4242, "oc_core": 0, "oc_mem": 0} if self.game else None
        m.NATIVE.clear()
        m.NATIVE["process_io"] = lambda: None if self.helper_rows is None else [{"pid": pid, "name": name, "other_bytes": other} for pid, name, other in self.helper_rows]
        self.reset()
        self.client = A.app.test_client()
        self.headers = {"Host": f"127.0.0.1:{config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}

    @staticmethod
    def reset():
        NM._thread, NM._stop, NM._last_wanted = None, threading.Event(), 0.0
        NM._history.clear()
        NM._per_proc.clear()
        NM._last_nic_rx = NM._last_nic_tx = 0
        NM._last_sample_t = 0.0
        NM._latest.clear()
        NM._latest.update(INITIAL)

    def close(self):
        NM.time, NM.threading, NM._sample_once = self.saved[:3]
        if self.saved[3] is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = self.saved[3]
        GP.get_active_game_info = self.saved[4]
        m.NATIVE.clear()
        m.NATIVE.update(self.saved[5])
        self.reset()

    def thread(self, target=None, args=(), daemon=None, name=None):
        assert target is NM._loop

        class NotStarted:
            def __init__(self):
                self.args, self.alive = args, False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive
        return NotStarted()

    def a_round(self):
        """One round of the loop of the sampler that runs: did it take a sample?"""
        thread = NM._thread
        if thread is None or not thread.alive:
            return False
        me, before = thread.args[0], self.attempts
        NM._loop(OneRound(me))
        if me.is_set():
            thread.alive = False                    # (the loop has ended)
        return self.attempts > before

    def state(self):
        return id(NM._thread), NM._stop.is_set(), NM._last_wanted

    def do(self, step):
        kind = step["do"]
        if kind == "now":
            self.pc = self.steady = step["time"]
        elif kind == "pcs_clock":
            self.pc = step["time"]
        elif kind == "cards":
            self.cards = step["says"]
        elif kind == "programs":
            self.helper_rows = step["says"]
        elif kind == "psutil":
            self.psutil_rows = step["says"]
        elif kind == "game":
            self.game = step["exe"]
        elif kind == "round":
            return self.a_round()
        elif kind == "call":
            before = self.state()
            answer = self.client.open(step["target"], method=step["method"], headers=self.headers)
            return {"status": answer.status_code, "body": answer.get_data(as_text=True), "touched": self.state() != before}
        else:
            raise AssertionError(step)
        return None

    def as_the_backend(self, call):
        self.parts.append(call)
        A._behind_shell = {"shell_pid": 0}
        try:
            answer = self.client.get(call, headers=self.headers)
        finally:
            A._behind_shell = None
        return answer.get_json() if answer.status_code == 200 else None


class Shell:
    """shell_rs/core/src/moved/, in the trial tool, on its made-up machine."""

    def __init__(self, python):
        self.python, self.asked_in_round = python, []
        self.tool = m.REAL_POPEN([S.TRY, "--moved"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.tell({"do": "backend_is_asked_back", "for": "/api/"})

    def tell(self, step):
        self.tool.stdin.write(json.dumps(step) + "\n")
        self.tool.stdin.flush()
        while True:
            said = json.loads(self.tool.stdout.readline())
            if "asks_the_backend" in said:
                call = said["asks_the_backend"]
                assert call.startswith("/api/network/part?name="), said
                self.asked_in_round.append(call)
                self.tool.stdin.write(json.dumps({"answer": self.python.as_the_backend(call)}) + "\n")
                self.tool.stdin.flush()
                continue
            assert "error" not in said, said
            return said

    def do(self, step):
        kind = step["do"]
        if kind in ("psutil", "game"):
            return None                             # (the backend's: the shell asks it for them)
        if kind == "cards":
            says = step["says"]
            step = {"do": "answers", "question": "network_cards",
                    "answer": None if says is None else [{"name": name, "sent": sent, "recv": recv} for name, sent, recv in says]}
        elif kind == "programs":
            says = step["says"]
            step = {"do": "answers", "question": "process_io",
                    "answer": None if says is None else [{"pid": pid, "name": name, "other_bytes": other} for pid, name, other in says]}
        elif kind == "round":
            self.asked_in_round = []
            return self.tell({"do": "net_round"})["took_one"]
        elif kind == "call":
            step = {key: value for key, value in step.items() if key not in ("refused", "shell_only")}
        said = self.tell(step)
        return said if kind in ("call", "asked") else None

    def close(self):
        self.tool.stdin.close()
        self.tool.wait(20)


def same(shells, pythons, where=""):
    if isinstance(shells, dict) and isinstance(pythons, dict):
        assert set(shells) == set(pythons), f"{where}: {sorted(set(shells) ^ set(pythons))}"
        for key in shells:
            same(shells[key], pythons[key], f"{where}.{key}")
        return
    if isinstance(shells, list) and isinstance(pythons, list):
        assert len(shells) == len(pythons), f"{where}: {shells} / {pythons}"
        for at, (a, b) in enumerate(zip(shells, pythons)):
            same(a, b, f"{where}[{at}]")
        return
    assert type(shells) is type(pythons) and shells == pythons, f"{where}: the shell {shells!r}, Python {pythons!r}"


def both(script):
    """Ask the Python module and the shell the same things in the same
    order, and compare every answer.  Returns what was counted."""
    python = Python()
    shell = Shell(python)
    counted = {"answered": 0, "passed_on": 0, "refused": 0, "rounds": 0, "asked_for_programs": 0}
    try:
        for at, step in enumerate(script):
            what = f"step {at}: {step}"[:300]
            if step.get("refused") or step.get("shell_only"):
                said = shell.do(step)
                if step.get("refused"):
                    assert said.get("status") == 400 and not said.get("passed_on"), f"{what}: {said}"
                    counted["refused"] += 1
                else:
                    assert said.get("passed_on"), f"{what}: {said}"
                    counted["passed_on"] += 1
                continue
            pythons, shells = python.do(step), shell.do(step)
            if step["do"] == "round":
                assert pythons == shells, f"{what}: a round in Python {pythons}, in the shell {shells}"
                counted["rounds"] += pythons
                if pythons:
                    wanted = python.helper_rows is None
                    asked = [call for call in shell.asked_in_round if call.endswith("=processes")]
                    assert bool(asked) == wanted, f"{what}: the helper had{' no' if wanted else ''} programs, and the shell asked its backend {asked}"
                    counted["asked_for_programs"] += bool(asked)
                    assert "/api/network/part?name=game" in shell.asked_in_round, f"{what}: the game was not asked for"
                continue
            if step["do"] != "call":
                continue
            ask = f"{what}: {step['method']} {step['target']}"[:400]
            if shells.get("passed_on"):
                counted["passed_on"] += 1
                assert not pythons["touched"], f"{ask}: the shell passed it on, and the backend's sampler did something with it"
                continue
            counted["answered"] += 1
            assert shells["status"] == pythons["status"] == 200, f"{ask}: {shells} / {pythons}"
            if step["method"] == "HEAD":
                assert shells["body"] == pythons["body"] == "", ask
            else:
                same(json.loads(shells["body"]), json.loads(pythons["body"]), ask)
        asked = shell.do({"do": "asked"})
        assert set(asked["library"]) <= {"network_cards", "process_io"}, sorted(set(asked["library"]))
        assert all(call in ("/api/network/part?name=game", "/api/network/part?name=processes") for call in asked["backend"]), sorted(set(asked["backend"]))
        assert (asked["net_kept"], asked["net_dropped"]) == (python.kept, python.dropped), (asked, python.kept, python.dropped)
        counted.update(asked, parts=list(python.parts))
        return counted
    finally:
        python.close()
        shell.close()


# ═════════════════════════════════════════════════════════════════════
# The script's words
# ═════════════════════════════════════════════════════════════════════
MB = 1024 * 1024


def now(time):
    return {"do": "now", "time": time}


def get(target):
    return {"do": "call", "method": "GET", "target": target}


def cards(*rows):
    return {"do": "cards", "says": list(rows)}


def programs(*rows):
    return {"do": "programs", "says": list(rows)}


ROUND, LIVE, HISTORY = {"do": "round"}, get("/api/network/live"), get("/api/network/history?seconds=60")


def a_machine():
    return [now(1000.0), cards(("Ethernet", 1_000, 2_000), ("Wi-Fi", 0, 0)),
            programs((0, "System Idle Process", 0), (4, "System", 0), (100, "Steam.exe", 10_000), (200, "cs2.exe", 0), (300, "Discord.exe", 0)),
            {"do": "game", "exe": None}]


# ═════════════════════════════════════════════════════════════════════
# The same answers
# ═════════════════════════════════════════════════════════════════════
def test_the_network_page_gets_the_same_answers_from_the_shell():
    """What the Network page does: the live figures and the minute of
    totals every two seconds, while a game starts, a download wakes up for a
    while, programs come and go; then it is left, and opened again."""
    if not tool():
        return
    s = a_machine() + [LIVE, HISTORY]
    sent, recv, steam, game, chat = 1_000, 2_000, 10_000, 0, 0
    for second in range(1, 75):
        sent += 40_000 + second * 1_000
        recv += 300_000 + (second % 7) * 50_000 + (6 * MB if 20 <= second < 32 else 0)
        steam += 6 * MB + second if 20 <= second < 32 else (second % 3) * 1_000
        game += 2 * MB if second >= 10 else 0
        chat += 6 * MB if 40 <= second < 45 else 512           # (the chat program is never taken for a burst)
        rows = [(0, "System Idle Process", 0), (4, "System", 0), (100, "Steam.exe", steam), (200, "cs2.exe", game), (300, "Discord.exe", chat)]
        if 30 <= second < 50:
            rows.append((400 + second // 10, "Updater.exe", second * 777))   # (one that changes its process every ten seconds)
        if 50 <= second < 56:
            rows += [(1000 + n, f"helper{n}.exe", second * (1000 + 37 * n)) for n in range(20)]   # (twenty that move: fifteen are shown)
        s += [now(1000.0 + second), cards(("Ethernet", sent, recv), ("Wi-Fi", 0, 0)), programs(*rows)]
        if second == 10:
            s.append({"do": "game", "exe": "cs2.exe"})
        if second == 60:
            s.append({"do": "game", "exe": None})
        s.append(ROUND)
        if second % 2 == 0 and second < 60:
            s += [LIVE, HISTORY]
    # The page was left at 60 s: 31 seconds later the sampler stops itself.
    s += [now(1100.0), ROUND, LIVE, HISTORY, get("/api/network/top"), now(1101.0), ROUND, now(1102.0), cards(("Ethernet", sent + 99, recv + 99)),
          programs((100, "Steam.exe", steam + 9 * MB), (200, "cs2.exe", game + 9 * MB), (300, "Discord.exe", chat + 1)), ROUND, LIVE,
          now(1103.0), programs((100, "Steam.exe", steam + 10 * MB), (200, "cs2.exe", game + 10 * MB), (300, "Discord.exe", chat + 2)), ROUND, LIVE]
    counted = both(s)
    assert counted["rounds"] > 70 and counted["passed_on"] == 0 and counted["answered"] == sum(1 for step in s if step["do"] == "call"), counted
    assert counted["asked_for_programs"] == 0 and counted["net_dropped"] == 0, counted


def test_where_python_reads_another_way_the_page_gets_pythons_answer():
    """The helper with no list of programs (psutil's, read in the backend:
    a program it cannot read, one with no name, a listing that raises); the
    cards' counts that cannot be read, that are empty, that go back, two
    cards of one name, a card that goes; a program's count that goes back,
    a program that is gone and its id given to another; the PC's clock set
    back between two samples."""
    if not tool():
        return
    s = a_machine() + [LIVE, ROUND]
    t = [1000.0]

    def later(*changes, by=1.0):
        t[0] += by
        return [now(t[0]), *changes, ROUND, LIVE]
    s += later(cards(("Ethernet", 5_000, 9_000), ("Wi-Fi", 7, 7)), programs((100, "Steam.exe", 20_000), (200, "cs2.exe", 5)))
    # The helper has no list: psutil's.
    s += later({"do": "programs", "says": None},
               {"do": "psutil", "says": [(100, "Steam.exe", 30_000), (200, "CS2.EXE", 6 * MB), (500, None, 10), (600, "locked.exe", None), (700, "Äpfel.exe", 1)]})
    s += later({"do": "psutil", "says": [(100, "Steam.exe", 31_000 + 6 * MB), (200, "CS2.EXE", 12 * MB), (500, None, 20), (700, "Äpfel.exe", 9)]})
    s += later({"do": "psutil", "says": "raises"}, cards(("Ethernet", 8_000, 12_000)))
    s += later({"do": "psutil", "says": [(100, "Steam.exe", 40_000 + 12 * MB), (200, "CS2.EXE", 18 * MB)]}, by=2.0)
    s += later(programs((100, "Steam.exe", 50_000 + 18 * MB), (200, "cs2.exe", 24 * MB)))
    # The cards.
    s += later({"do": "cards", "says": None})
    s += later(cards(("Ethernet", 20_000, 30_000)))
    s += later({"do": "cards", "says": []})
    s += later(cards(("Ethernet", 21_000, 31_000), ("Ethernet", 500, 600), ("Wi-Fi", 1, 1)))
    s += later(cards(("Ethernet", 100, 200), ("Wi-Fi", 2, 2)))                 # (it went back: psutil adds the old count on)
    s += later(cards(("Ethernet", 1_100, 1_200), ("Wi-Fi", 3, 3)))
    s += later(cards(("Wi-Fi", 4, 4)))                                          # (a card that goes)
    s += later(cards(("Ethernet", 50, 60), ("Wi-Fi", 5, 5)))                   # (and comes back with nothing added on)
    # The programs: a count that goes back, an id given to another program.
    s += later(programs((100, "Steam.exe", 10), (200, "cs2.exe", 24 * MB + 5)))
    s += later(programs((100, "Steam.exe", 20), (201, "cs2.exe", 0)))
    s += later(programs((100, "notepad.exe", 9_999), (201, "cs2.exe", 5)))
    # A burst with a game running and one without; one at exactly the line.
    s += [{"do": "game", "exe": "cs2.exe"}]
    steam = 9_999
    for _ in range(4):
        steam += 5 * MB
        s += later(programs((100, "notepad.exe", steam), (201, "cs2.exe", 5), (300, "Discord.exe", 0)))
    s += [{"do": "game", "exe": None}]
    for _ in range(3):
        steam += 5 * MB - 1
        s += later(programs((100, "notepad.exe", steam), (201, "cs2.exe", 5)))
    other = 0
    for step in range(4):
        steam, other = steam + 6 * MB, other + (9 if step % 2 else 7) * MB
        s += later(programs((100, "notepad.exe", steam), (202, "sync.exe", other)))
    # The PC's clock set a long way forward while the counts move: it goes on.
    s += [{"do": "pcs_clock", "time": t[0] + 90_000}, programs((100, "notepad.exe", steam + MB), (202, "sync.exe", other + MB)), ROUND, LIVE, now(t[0] + 2),
          programs((100, "notepad.exe", steam + 2 * MB), (202, "sync.exe", other + 3 * MB)), ROUND, LIVE]
    t[0] += 2
    # The PC's clock set back between two samples, and forward.
    s += [{"do": "pcs_clock", "time": t[0] - 100}, ROUND, LIVE, HISTORY]
    s += [{"do": "pcs_clock", "time": t[0] + 5000}, ROUND, LIVE, get("/api/network/history?seconds=10")]
    counted = both(s)
    assert counted["asked_for_programs"] == 4 and counted["net_dropped"] == 1, counted


def test_the_calls_are_taken_as_python_takes_them():
    """The seconds of the history and the number of programs, written every
    way; HEAD, which the backend answers by running the reading's route;
    other methods; the calls written another way.  What Python stops at is
    passed on; a number Python may read and the shell cannot is refused."""
    if not tool():
        return
    s = a_machine() + [LIVE, ROUND, now(1001.0), programs((100, "Steam.exe", 20_000), (200, "cs2.exe", 7_000), (300, "Discord.exe", 5)), ROUND]
    for asked in ("", "?seconds=", "?seconds=0", "?seconds=1", "?seconds=-5", "?seconds=+3", "?seconds=abc", "?seconds=1.5", "?seconds=%2B2",
                  "?seconds=99999999999999999999", "?seconds=-99999999999999999999", "?seconds=6&seconds=0", "?Seconds=0"):
        s.append(get("/api/network/history" + asked))
    for asked in ("", "?limit=0", "?limit=1", "?limit=2", "?limit=-1", "?limit=-2", "?limit=-9", "?limit=99", "?limit=x", "?limit= 1 "):
        s.append(get("/api/network/top" + asked))
    s += [{"do": "call", "method": "HEAD", "target": "/api/network/live"}, {"do": "call", "method": "HEAD", "target": "/api/network/history"}]
    s += [get("/api/network/%6Cive"), get("/api%2Fnetwork%2Ftop?limit=1"), get("/api/network/hist%6Fry")]
    for method, target in (("POST", "/api/network/live"), ("PUT", "/api/network/history"), ("DELETE", "/api/network/top"), ("PATCH", "/api/network/live"),
                           ("OPTIONS", "/api/network/live"), ("GET", "/api/network/live/"), ("GET", "/api/network/Live")):
        s.append({"do": "call", "method": method, "target": target})
    # (never made to Python: they pause and resume a program)
    for target in ("/api/network/pause-process", "/api/network/resume-process"):
        s.append({"do": "call", "method": "POST", "target": target, "shell_only": True})
    for target in ("/api/network/history?seconds=%D9%A1%D9%A2", "/api/network/top?limit=" + "9" * 60):
        s.append({"do": "call", "method": "GET", "target": target, "refused": True})
    s += [LIVE]
    counted = both(s)
    assert counted["refused"] == 2 and counted["passed_on"] == 9, counted


def test_the_copy_of_psutils_wrap_is_psutils():
    """Where psutil is installed (the Python releases are built with), this
    file's copy of its wrap code gives what psutil's own gives."""
    if sys.version_info[:2] == (3, 12):
        assert PSUTILS_WRAP is not None, "psutil is not installed for the Python releases are built with"
    if PSUTILS_WRAP is None:
        return
    import random
    rng = random.Random(7)
    ours, theirs = OurWrap(), PSUTILS_WRAP()
    counts = {}
    for _ in range(400):
        names = rng.sample(["a", "b", "c", "d"], rng.randint(0, 4))
        raw = {}
        for name in names:
            counts[name] = rng.choice([counts.get(name, 0) + rng.randint(0, 50), rng.randint(0, 20)])
            raw[name] = (counts[name], counts[name] * 2, 0, 0, 0, 0, 0, 0)
        if raw:
            assert ours.run(dict(raw), "n") == theirs.run(dict(raw), "n")


# ═════════════════════════════════════════════════════════════════════
# One of each, and what the shell asks
# ═════════════════════════════════════════════════════════════════════
def test_the_backends_part_is_there_only_behind_the_shell_and_never_starts_its_sampler():
    app()
    python = Python()
    try:
        for step in a_machine():
            python.do(step)
        python.do({"do": "game", "exe": "cs2.exe"})
        for name in ("game", "processes"):
            assert python.client.get(f"/api/network/part?name={name}", headers=python.headers).status_code == 404
        assert python.as_the_backend("/api/network/part?name=game") == {"ok": True, "reading": {"exe": "cs2.exe"}}
        rows = python.as_the_backend("/api/network/part?name=processes")["reading"]["rows"]
        assert rows == [[0, "system idle process", 0], [4, "system", 0], [100, "steam.exe", 10_000], [200, "cs2.exe", 0], [300, "discord.exe", 0]], rows
        python.do({"do": "programs", "says": None})
        python.do({"do": "psutil", "says": "raises"})
        raised = python.as_the_backend("/api/network/part?name=processes")
        assert raised["ok"] is False and raised["raised"] is True, raised
        for not_one in ("/api/network/part", "/api/network/part?name=", "/api/network/part?name=_loop", "/api/network/part?name=start"):
            assert python.as_the_backend(not_one) is None, not_one
        assert NM._thread is None, "a part started the backend's sampler"
    finally:
        python.close()


def test_the_shell_reads_only_and_asks_its_backend_for_two_parts():
    net = S.rust("src", "moved", "net.rs")
    code = net[:net.index("#[cfg(test)]")]
    assert re.findall(r'"(/api/[^"]*)"', code) == ["/api/network/part", "/api/network/live", "/api/network/history", "/api/network/top"]
    assert re.findall(r'self\.the_backends\("(\w+)"\)', code) == ["processes", "game"] and code.count("ask(&format!") == 1
    assert re.findall(r'self\.door\.ask\("(\w+)"\)', code) == ["network_cards", "process_io"] and "ask_with" not in code
    # The never-flagged names are Python's.
    app()
    listed = re.findall(r'"([^"]+)"', code[code.index("pub const NEVER_FLAG"):code.index("];", code.index("pub const NEVER_FLAG"))])
    assert sorted(listed) == sorted(NM.NEVER_FLAG), sorted(set(listed) ^ set(NM.NEVER_FLAG))
    for name, value in (("SAMPLE_INTERVAL", "1.0"), ("HISTORY_LEN", "60"), ("IDLE_STOP_S", "30.0"), ("BURST_BYTES_PER_SEC", "5 * 1024 * 1024"),
                        ("BURST_CONSECUTIVE", "3")):
        assert re.search(rf"pub const {name}: \w+ = {re.escape(value)};", code), name
    assert (NM.SAMPLE_INTERVAL, NM.HISTORY_LEN, NM.IDLE_STOP_S, NM.BURST_BYTES_PER_SEC, NM.BURST_CONSECUTIVE) == (1.0, 60, 30.0, 5 * MB, 3)
    # The parts are the module's own functions.
    source = S.repo("app.py")
    assert '"game": lambda: {"exe": net_monitor._get_active_game_exe()},' in source
    assert "net_monitor._process_io_rows(psutil)" in source
    # Pausing and resuming stay the backend's.
    door = S.rust("src", "moved", "mod.rs")
    answer = door[door.index("    pub fn answer(&self, request: &Request) -> Option<Response> {"):door.index("/// The backend's call that starts a graphics driver's installer.")]
    assert 'path.starts_with("/api/network/")' in answer
    taken = code[code.index("        let answer = match path {"):code.index("        let mut response = Response::json(200, &answer);")]
    assert re.findall(r'"(/api/network/\w+)" =>', taken) == ["/api/network/live", "/api/network/history", "/api/network/top"]
    assert "pause" not in taken and "resume" not in taken


def test_the_helpers_question_for_the_cards_only_reads():
    """The helper's new question asks Windows for the adapters and their
    counts and nothing else; it is a question, not an action."""
    nics = S.repo("native", "src", "nics.rs")
    calls = set(re.findall(r"\b(Get\w+|Set\w+|Nt\w+|Create\w+|Delete\w+)\(", nics[:nics.index("#[cfg(test)]")]))
    assert calls == {"GetAdaptersAddresses", "GetIfEntry2"}, calls
    lib = S.repo("native", "src", "lib.rs")
    questions = lib[lib.index("fn dispatch(request: &str)"):lib.index("fn dispatch_action(request: &str)")]
    assert '"network_cards" => nics::network_cards().map(Into::into),' in questions and "nics::" not in lib[lib.index("fn dispatch_action(request: &str)"):]
