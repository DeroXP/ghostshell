"""What has moved from the backend into the shell: the performance sampler.

Step 4 of docs/rust-port.md moves the backend into the shell one module at a
time.  The first is core/perf_monitor.py: what the graphics card is doing,
five times a second, for the Monitor page's live charts and its stability
score.  The shell (shell_rs/core/src/moved/perf.rs) answers its five calls
itself.  The Python module and its routes are as they were, and answer when
the app is started as it always was.

The card's reading is not taken in the shell's own process.  Reading the
card means starting NVIDIA's library in the reading process, and once
started it never leaves that process (tools/measure/nvml_release.py shows
it), while a driver's installer has to replace the file.  So the shell never
opens the library.  The card is read by a small program of the shell's own
(VisporaGpuReader: the native helper, asked its one question about the
card), which the shell starts while a reading is wanted and ends before a
driver's installer is started; and by the backend wherever that reader is
away or has no reading (GET /api/gpu/oc/live:
`gpu_overclock.read_live_state()`, the very reading the Python sampler
takes).  Either way it is the reading Python would have taken.

"The same answers, proven on the same fake machine before anything is
removed": here both are asked the same things, one after the other, on the
same made-up machine, and every answer is compared.

  * The machine is what the backend's native helper says about the card
    (m.NATIVE in the sandbox), what nvidia-smi prints where the helper says
    nothing, and a clock that is set by hand.
  * The Python module is the real one, its routes called through Flask.  Only
    its thread is taken out: one round of its own loop is run where the
    script says "step", and its own end where it was told to stop.
  * The shell's side is the real code, in the trial tool
    (`vispora-shell-try --moved`), which reads the same script.  Its reader
    is a made-up one that says what the backend's helper says (it is the
    same helper, in another process); and before every sample the tool is
    told what the backend's call for the card's reading answers at that
    moment.  Its own made-up library is made to say something else of the
    card: the shell's own process must not ask it.

Where the shell does not take a call (it passes it on, so the backend
answers), Python must have stopped before it touched the sampler, or the
script says that Python reads what the shell cannot: then the backend's
answer is the answer, by construction.

Nothing of the PC is read: no card, no clock.
"""
import sandbox as m

import json
import os
import random
import re
import subprocess
import threading
import time
import types
import unittest

import test_shell_rs as S                           # (its helpers: where the trial tool is, the shell's source)

try:
    import flask                                    # noqa: F401
    import app as A
    import config
    PM = m.app("core.perf_monitor")
    GO = m.app("core.gpu_overclock")
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


# ═════════════════════════════════════════════════════════════════════
# The two sides
# ═════════════════════════════════════════════════════════════════════
class Python:
    """core/perf_monitor.py behind its routes, on the fake machine, with the
    clock set by hand and its thread run a round at a time."""

    def __init__(self):
        self.time = 0.0
        self.sampled = 0                            # how many samples its thread has taken
        self.from_helper = 0                        # ... and how many of them were the helper's reading (the rest: nvidia-smi's, or none)
        self.thread_of = None                       # the stop event of the thread that would be running
        self.smi = None                             # what nvidia-smi prints on this machine (None: it is not there)
        m.CMD_HOOKS.append(self.nvidia_smi)
        self.saved = (PM.time, PM.threading, dict(m.NATIVE))
        PM.time = types.SimpleNamespace(time=lambda: self.time)
        PM.threading = types.SimpleNamespace(Event=threading.Event, Thread=self.thread)
        with PM._lock:
            PM._state.update(running=False, thread=None, stop_event=None, last_sample=None, started_at=None)
            PM._state["samples"] = PM.deque(maxlen=PM.RING_BUFFER_SIZE)
            PM._state["consumers"] = set()
        m.NATIVE.pop("gpu_live", None)
        self.client = A.app.test_client()
        self.headers = {"Host": f"127.0.0.1:{config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}

    def nvidia_smi(self, command):
        if os.path.splitext(os.path.basename(command[0]))[0].lower() != "nvidia-smi":
            return None
        return m.R(self.smi) if self.smi else m.F("'nvidia-smi' is not recognized as an internal or external command")

    def card_as_the_backend_reads_it(self):
        """What the backend's own call for the card's reading answers now
        (app.py, /api/gpu/oc/live): what the shell is given for a sample."""
        answer = self.client.get("/api/gpu/oc/live", headers=self.headers)
        assert answer.status_code == 200, answer.get_data(as_text=True)
        return answer.get_json()

    def thread(self, target=None, args=(), **_):
        assert target is PM._sampler_loop
        side = self

        class NotStarted:
            def start(self):
                side.thread_of = args[0]
        return NotStarted()

    def state(self):
        with PM._lock:
            return (PM._state["running"], PM._state["stop_event"], sorted(map(repr, PM._state["consumers"])), PM._state["started_at"],
                    list(PM._state["samples"]), PM._state["last_sample"])

    def do(self, step):
        kind = step["do"]
        if kind == "now":
            self.time = step["time"]
        elif kind == "smi":
            self.smi = step["says"]
        elif kind == "answers":
            if step["answer"] is None:
                m.NATIVE.pop(step["question"], None)
            else:
                m.NATIVE[step["question"]] = step["answer"]
        elif kind == "step":
            event = self.thread_of
            if event is None:
                return None
            if event.is_set():
                PM._sampler_loop(event)             # (told to stop: it takes no sample, and ends as it ends)
                self.thread_of = None
            else:
                class OneRound:
                    def __init__(self):
                        self.asked = 0

                    def is_set(self):
                        self.asked += 1
                        return self.asked > 1

                    def wait(self, _seconds):
                        pass
                self.from_helper += GO._live_state_from_native() is not None
                PM._sampler_loop(OneRound())
                self.sampled += 1
        elif kind in ("reader", "tend"):
            pass                                    # (the shell's own reader: Python has none)
        elif kind == "pcs_clock":
            self.time = step["time"]                # (the PC's clock is set to another time: `time.time()` says it)
        elif kind == "install":
            # The call that starts a driver's installer.  Nothing is
            # started: where the script says that one was, the app tells
            # its helper to let go, as it does before it starts one.
            if json.loads(step["body"] or "null") and json.loads(step["body"]).get("ok"):
                from core import driver_updater
                m.NATIVE["gpu_release"] = lambda hold_s: {"released": True, "hold_s": hold_s}
                try:
                    driver_updater._release_gpu_library()
                finally:
                    m.NATIVE.pop("gpu_release", None)
        else:
            before = self.state()
            answer = self.client.open(step["target"], method=step["method"], data=step.get("body", "").encode("utf-8"), headers=self.headers)
            return {"status": answer.status_code, "body": answer.get_data(as_text=True), "touched": self.state() != before}
        return None

    def close(self):
        m.CMD_HOOKS.remove(self.nvidia_smi)
        PM.time, PM.threading = self.saved[0], self.saved[1]
        m.NATIVE.clear()
        m.NATIVE.update(self.saved[2])
        with PM._lock:
            PM._state.update(running=False, thread=None, stop_event=None, last_sample=None, started_at=None)
            PM._state["consumers"] = set()
            PM._state["samples"] = PM.deque(maxlen=PM.RING_BUFFER_SIZE)


class Shell:
    """shell_rs/core/src/moved/, in the trial tool, on its made-up machine.
    `reader`: has it a reader of its own for the card (as a shell whose pack
    carries one), or none (the card's reading is the backend's)?"""

    def __init__(self, reader=True):
        self.tool = m.REAL_POPEN([S.TRY, "--moved"] + ([] if reader else ["--no-reader"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def tell(self, step):
        self.tool.stdin.write(json.dumps(step) + "\n")
        self.tool.stdin.flush()
        said = json.loads(self.tool.stdout.readline())
        assert "error" not in said, said
        return said

    def do(self, step):
        if step["do"] == "smi":
            return None                             # (the fake machine's nvidia-smi is the backend's to start, never the shell's)
        if step["do"] == "answers" and step["question"] == "gpu_live":
            # What the backend's helper says of the card, the card's reader
            # says too: it is the same helper, asked in another process.
            self.tell({"do": "reader", "says": "none" if step["answer"] is None else step["answer"]})
            # The library in the shell's own process does not say it: it
            # says something else, at all times, so that a shell that asked
            # it would be found out.
            step = dict(step, answer=NOT_THE_CARD)
        said = self.tell(step)
        return said if step["do"] in ("call", "asked", "install", "tend") else None

    def close(self):
        self.tool.stdin.close()
        self.tool.wait(20)


UNORDERED = ("consumers", "still_running_for")      # (a set in Python: listed in whatever order it happens to have)


def same(shells, pythons):
    if isinstance(shells, dict) and isinstance(pythons, dict):
        return set(shells) == set(pythons) and all(
            (sorted(map(repr, shells[k])) == sorted(map(repr, pythons[k])) if k in UNORDERED else same(shells[k], pythons[k])) for k in shells)
    if isinstance(shells, list) and isinstance(pythons, list):
        return len(shells) == len(pythons) and all(same(a, b) for a, b in zip(shells, pythons))
    if isinstance(shells, bool) or isinstance(pythons, bool) or shells is None or pythons is None:
        return shells is pythons
    return shells == pythons


def both(script, reader=True):
    """Ask the Python module and the shell the same things in the same
    order.  Every answer the shell gives must be Python's; where the shell
    passes a call on, Python must not have touched the sampler.  Returns how
    many calls the shell answered and how many it passed on.

    Before every sample the shell's side is told what the backend's own
    call for the card's reading answers on this machine at that moment.
    At the end: for every sample Python took, the shell took one reading,
    from its reader or from the backend; the backend was asked for the card
    exactly where the reader gave none; and the library in the shell's own
    process was never asked.  What the shell's side counted is kept in
    `both.counted`."""
    python, shell = Python(), Shell(reader)
    answered = passed_on = 0
    try:
        for at, step in enumerate(script):
            if step["do"] == "step":
                shell.do({"do": "backend_says", "answer": python.card_as_the_backend_reads_it()})
            pythons, shells = python.do(step), shell.do(step)
            if step["do"] != "call":
                continue
            what = f"step {at}: {step['method']} {step['target']} {step.get('body', '')!r}"
            if shells.get("passed_on"):
                passed_on += 1
                assert not pythons["touched"], f"{what}: the shell passed it on, and the backend's sampler did something with it"
                # Passed on, Python answers: with words of its own for what
                # it stops at; or, for a number it reads and the shell
                # cannot, from its own sampler (which the script says so of).
                stops = pythons["status"] != 200 or step["method"] in ("HEAD", "OPTIONS")
                assert stops != bool(step.get("python_reads_it")), f"{what}: the shell passed on what Python answers: {pythons}"
            else:
                answered += 1
                assert (shells["status"], pythons["status"]) == (200, 200), f"{what}: {shells} / {pythons}"
                assert same(json.loads(shells["body"]), json.loads(pythons["body"])), \
                    f"{what}:\n  the shell:  {shells['body']}\n  the Python: {pythons['body']}"
        asked = shell.do({"do": "asked"})
        assert asked["library"] == [], f"the shell asked the library in its own process: {asked['library'][:5]}"
        assert set(asked["backend"]) <= {"/api/gpu/oc/live"}, sorted(set(asked["backend"]))
        took = (asked["from_the_reader"], asked["from_the_backend"], asked["from_nowhere"])
        assert sum(took) == python.sampled, (took, python.sampled)
        assert len(asked["backend"]) == took[1] + took[2], "the backend was asked for the card where the reader had a reading, or not asked where it had none"
        # (never more from the reader than Python took from its helper: a
        # reader that gave nothing is away for a while, and the backend's
        # reading is then the helper's in the backend, which is the same)
        assert took[0] <= python.from_helper and asked["reader"] <= python.sampled, (took, python.from_helper, asked["reader"])
        if not reader:
            assert (took[0], asked["readers_started"]) == (0, 0)
        both.counted = dict(asked, sampled=python.sampled, from_helper=python.from_helper)
        return answered, passed_on
    finally:
        python.close()
        shell.close()


both.counted = {}


def now(time):
    return {"do": "now", "time": time}


def card(**says):
    return {"do": "answers", "question": "gpu_live", "answer": says or None}


def get(target):
    return {"do": "call", "method": "GET", "target": target}


def post(target, body):
    return {"do": "call", "method": "POST", "target": target, "body": body if isinstance(body, str) else json.dumps(body)}


STEP = {"do": "step"}
EVERYTHING = [get("/api/perf/status"), get("/api/perf/samples"), get("/api/perf/samples?seconds=10"), get("/api/perf/score"),
              get("/api/perf/score?seconds=5"), get("/api/perf/score?seconds=60")]
A_CARD = dict(temp_c=61, gpu_util_pct=97, mem_util_pct=40, core_mhz=2100, mem_mhz=9501, core_max_mhz=2200, mem_max_mhz=9800, core_app_mhz=2100,
              mem_app_mhz=9501, power_mw=215437, power_limit_mw=320000, fan_pct=55, mem_used_bytes=5_000_000_000, mem_total_bytes=12_000_000_000)
NOT_THE_CARD = dict(A_CARD, temp_c=13, gpu_util_pct=13, mem_util_pct=13, core_mhz=1313, mem_mhz=1313, power_mw=13_000, fan_pct=13)


def tool():
    app()
    return S.TRY


# ═════════════════════════════════════════════════════════════════════
# The same answers
# ═════════════════════════════════════════════════════════════════════
def test_the_monitor_page_gets_the_same_answers_from_the_shell():
    """What the Monitor page does: it says it wants the sampler, asks for
    the chart and the score every two seconds, and says when it leaves."""
    if not tool():
        return
    script = [now(1000.0), card(**A_CARD), *EVERYTHING, post("/api/perf/start", {"consumer": "hwmon-page"}), *EVERYTHING]
    for round in range(1, 61):
        warm = dict(A_CARD, temp_c=58 + round // 10, gpu_util_pct=90 + round % 7, core_mhz=2100 - (round % 3) * 15, power_mw=200_000 + round * 333)
        script += [now(1000.0 + round * 0.2), card(**warm), STEP]
        if round % 10 == 0:
            script += [get("/api/perf/samples?seconds=60"), get("/api/perf/score?seconds=30"), get("/api/perf/status")]
    script += [post("/api/perf/stop", {"consumer": "hwmon-page"}), *EVERYTHING, STEP, *EVERYTHING, now(1100.0), *EVERYTHING]
    answered, passed_on = both(script)
    assert (answered, passed_on) == (sum(1 for step in script if step["do"] == "call"), 0)
    # Every one of the sixty samples was the reader's: one reader, started
    # for the first of them, and the backend never asked for the card.
    counted = both.counted
    assert (counted["sampled"], counted["from_the_reader"], counted["readers_started"], counted["backend"]) == (60, 60, 1, []), counted
    # A shell without a reader of its own (its pack carries none): the same
    # answers, every reading the backend's.
    answered, passed_on = both(script, reader=False)
    assert (answered, passed_on) == (sum(1 for step in script if step["do"] == "call"), 0)
    assert (both.counted["from_the_backend"], len(both.counted["backend"])) == (60, 60)


def test_starting_and_stopping_give_the_same_answers():
    if not tool():
        return
    s = [now(50.0), card(**A_CARD)]
    start, stop = (lambda body: post("/api/perf/start", body)), (lambda body: post("/api/perf/stop", body))
    # Nobody yet; one, twice; a second; one leaves; the other leaves; again.
    s += [stop({}), get("/api/perf/status"), start({"consumer": "a"}), start({"consumer": "a"}), start({"consumer": "b"}), STEP, now(50.2), STEP,
          get("/api/perf/status"), stop({"consumer": "a"}), stop({"consumer": "nobody"}), stop({"consumer": "b"}), get("/api/perf/status"),
          stop({"consumer": "b"}), STEP, get("/api/perf/status"), stop({"consumer": "b"}), stop({})]
    # Told to stop and started again before its thread has ended, and after.
    s += [now(60.0), start({}), STEP, stop({}), start({"consumer": "again"}), get("/api/perf/status"), now(60.2), STEP, STEP, get("/api/perf/status"),
          stop({"consumer": "again"}), STEP, now(70.0), start({"consumer": "later"}), get("/api/perf/status"), get("/api/perf/samples")]
    # With force, whoever else wants it; and what counts as force.
    for force in (True, 1, "yes", [0], {"a": 1}, 0.5, False, 0, 0.0, "", None, [], {}):
        s += [start({"consumer": "a"}), start({"consumer": "b"}), STEP, stop({"consumer": "a", "force": force}), get("/api/perf/status"),
              stop({"force": True}), STEP, get("/api/perf/status")]
    # Whatever is sent for a consumer that Python can keep: no name at all,
    # nothing, numbers and truth values (1, 1.0 and true are one to Python).
    for named in ("", "manual", None, 0, 1, 1.0, True, False, 2.5, -7, "1", "hwmon-page", "über"):
        s += [start({"consumer": named}), get("/api/perf/status")]
    for named in (1.0, False, None, "1", "", 2.5, "nobody", "hwmon-page", "manual", -7, "über"):
        s += [stop({"consumer": named}), get("/api/perf/status")]
    s += [STEP, get("/api/perf/status")]
    # A body that says nothing is an empty one; the last of two names counts.
    for body in ("{}", "null", "false", "0", "0.0", '""', "[]", " {} ", '{"consumer": "x", "consumer": "y"}', '{"force": 1, "force": 0, "consumer": "y"}',
                 '{"other": 1}', '{"consumer": "\\u0061"}'):
        s += [start(body), get("/api/perf/status"), stop(body), STEP, get("/api/perf/status")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered > 150


def test_what_the_shell_passes_on_is_what_python_stops_at_before_the_sampler():
    """Another method, a body that is no JSON, JSON that is something and no
    object, a consumer that cannot be kept in a set: Python answers each of
    those in words of its own without touching the sampler, so the shell
    hands them to the backend and its answer is the answer."""
    if not tool():
        return
    s = [now(10.0), card(**A_CARD), post("/api/perf/start", {"consumer": "there"}), STEP]
    not_taken = []
    for target in ("/api/perf/start", "/api/perf/stop"):
        for body in ("", "not json", "{", "[1]", '"text"', "7", "true", "1.5", '{"consumer": []}', '{"consumer": {"a": 1}}', '{"consumer": [1], "force": true}',
                     "{'consumer': 'a'}", '{"consumer": "a",}'):
            not_taken.append(post(target, body))
        not_taken += [{"do": "call", "method": method, "target": target, "body": "{}"} for method in ("GET", "PUT", "DELETE", "PATCH")]
    for target in ("/api/perf/status", "/api/perf/samples", "/api/perf/score"):
        not_taken += [{"do": "call", "method": method, "target": target, "body": "{}"} for method in ("POST", "PUT", "DELETE", "PATCH")]
    not_taken += [get("/api/perf/"), get("/api/perf"), get("/api/perf/status/"), get("/api/perf/other"), get("/api/perf/Status")]
    for call in not_taken:
        s += [call, get("/api/perf/status")]
    answered, passed_on = both(s)
    assert passed_on == len(not_taken) and answered == len(not_taken) + 1


def test_the_seconds_asked_for_are_read_the_same_way():
    if not tool():
        return
    s = [now(500.0), card(**A_CARD), post("/api/perf/start", {})]
    for round in range(80):
        s += [now(500.0 + round * 0.2), card(**dict(A_CARD, gpu_util_pct=60 + round % 30, temp_c=50 + round % 9)), STEP]
    asked = ["", "?", "?seconds=", "?seconds", "?seconds=5", "?seconds=0", "?seconds=-3", "?seconds=+4", "?seconds=%2B4", "?seconds=%20%204%20", "?seconds=007",
             "?seconds=1_0", "?seconds=1__0", "?seconds=_1", "?seconds=4.5", "?seconds=4e0", "?seconds=abc", "?seconds=0x10", "?seconds=--4",
             "?seconds=%33%30", "?%73econds=7", "?seconds=6&seconds=9", "?other=1&seconds=8", "?Seconds=8", "?seconds=3#seconds=9", "?&&seconds=2&",
             "?seconds=16", "?seconds=15.8", "?seconds=100000", "?seconds=99999999999999999999999999", "?seconds=-99999999999999999999999999",
             "?seconds=4%", "?seconds=%zz4", "?seconds=4;seconds=9", "?seconds=4&x=%ff", "?seconds=%7e4",
             "?seconds=" + "9" * 38, "?seconds=-" + "9" * 38, "?x=%D9%A1&seconds=4"]
    for query in asked:
        s += [get("/api/perf/samples" + query), get("/api/perf/score" + query)]
    # A number that Python reads and the shell cannot say it reads the same:
    # more digits than the shell counts with, the digits of another script,
    # a blank that is not a plain one, a letter that cannot be printed (what
    # reaches Python of those is the web framework's to say).  The shell
    # does not take the call.  (Python then answers from its own sampler,
    # which behind the shell holds nothing: here it holds what the script
    # gave it.)
    beyond = ["?seconds=" + "9" * 40, "?seconds=-" + "9" * 60, "?seconds=%D9%A1%D9%A2", "?seconds=12%C2%A0", "?seconds=%E2%80%837", "?seconds=%EF%BC%91",
              "?seconds=7%C3%A9", "?seconds=%FF", "?seconds=%0b%0c4%1c%1f", "?seconds=4%00", "?seconds=%7f4", "?seconds=%094", "?seconds=4%0a",
              "?seconds=4%0d"]
    for query in beyond:
        s += [dict(get("/api/perf/samples" + query), python_reads_it=True), dict(get("/api/perf/score" + query), python_reads_it=True)]
    answered, passed_on = both(s)
    assert (answered, passed_on) == (1 + 2 * len(asked), 2 * len(beyond))


def test_whatever_the_card_says_makes_the_same_sample():
    """Every shape of an answer the backend's helper can give for the card,
    and the ones it should never give: what is taken, what is 0, and what
    makes a sample that says "no reading" (there is no nvidia-smi on this
    machine).  The shell is given the backend's reading of each."""
    if not tool():
        return
    says = [A_CARD, dict(temp_c=40), dict(temp_c=0), dict(A_CARD, fan_pct=None, power_mw=None), dict(A_CARD, temp_c=53.9, core_mhz=2099.99, power_mw=1499.6),
            dict(A_CARD, power_mw=1), dict(A_CARD, power_mw=4), dict(A_CARD, power_mw=5), dict(A_CARD, power_mw=15), dict(A_CARD, power_mw=215435),
            dict(A_CARD, power_mw=2675), dict(A_CARD, power_mw=1005), dict(A_CARD, power_mw=999999995), dict(A_CARD, gpu_util_pct=0), dict(A_CARD, temp_c=-5),
            dict(A_CARD, core_mhz=-1), dict(A_CARD, extra="ignored"), dict(temp_c=61, gpu_util_pct=True), dict(A_CARD, temp_c=None), dict(gpu_util_pct=50),
            dict(A_CARD, mem_total_bytes="a lot"), dict(A_CARD, fan_pct=[55]), dict(A_CARD, temp_c="61"), dict(A_CARD, core_mhz={"mhz": 2100}),
            dict(A_CARD, temp_c=False), A_CARD]
    s = [now(1.0), card(**A_CARD), post("/api/perf/start", {})]
    for at, said in enumerate(says):
        s += [now(1.0 + at * 0.2), {"do": "answers", "question": "gpu_live", "answer": said}, STEP, get("/api/perf/status")]
    # An answer that is no object, and none at all.
    for at, said in enumerate(([1, 2], "warm", 61, None, A_CARD)):
        s += [now(20.0 + at * 0.2), {"do": "answers", "question": "gpu_live", "answer": said}, STEP, get("/api/perf/status")]
    s += [get("/api/perf/samples"), get("/api/perf/score")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered == len(says) + 5 + 3


def test_the_score_is_the_same_for_whatever_the_card_does():
    """The score is three standard deviations and five roundings.  A card
    that idles, one that works steadily, one that jumps about, one that
    comes and goes, each over more samples than are kept, and asked about
    windows of every length: made up from a fixed seed, so the same at every
    run."""
    if not tool():
        return
    made_up = random.Random(20261125)
    kinds = {
        "idle": lambda: dict(gpu_util_pct=made_up.randint(0, 14), core_mhz=made_up.choice((300, 405, 600)), temp_c=made_up.randint(38, 44)),
        "steady": lambda: dict(gpu_util_pct=made_up.randint(96, 99), core_mhz=made_up.choice((2100, 2100, 2100, 2085)), temp_c=made_up.randint(66, 68)),
        "jumpy": lambda: dict(gpu_util_pct=made_up.choice((99, 97, 40, 20, 75)), core_mhz=made_up.randint(1500, 2200), temp_c=made_up.randint(55, 83)),
        "comes and goes": lambda: dict(gpu_util_pct=made_up.choice((0, 3, 14, 15, 16, 60, 99)), core_mhz=made_up.randint(300, 2200),
                                       temp_c=made_up.choice((0, 0, 45, 60, 71))),
        "anything": lambda: dict(gpu_util_pct=made_up.randint(0, 100), core_mhz=made_up.randint(0, 3000), temp_c=made_up.randint(0, 95),
                                 power_mw=made_up.randint(0, 450_000), fan_pct=made_up.randint(0, 100), mem_mhz=made_up.randint(0, 11000)),
    }
    asked = 0
    for name, says in kinds.items():
        s = [now(5000.0), card(**A_CARD), post("/api/perf/start", {"consumer": name})]
        for round in range(340):
            reading = dict(A_CARD, **says())
            if made_up.random() < 0.03:
                reading = None                      # (the card gives no reading for once)
            s += [now(5000.0 + round * 0.2 + made_up.random() * 0.01), {"do": "answers", "question": "gpu_live", "answer": reading}, STEP]
            if round % 17 == 16 or round in (8, 9, 10, 299, 300, 301):
                s += [get(f"/api/perf/score?seconds={made_up.choice((1, 2, 3, 5, 10, 20, 30, 45, 60, 120))}"), get("/api/perf/score"),
                      get(f"/api/perf/samples?seconds={made_up.choice((1, 7, 60))}")]
        s += [get("/api/perf/status"), get("/api/perf/samples")]
        answered, passed_on = both(s)
        assert passed_on == 0, name
        asked += answered
    assert asked > 350


def test_the_score_at_its_edges_is_the_same():
    """Exactly 85 and exactly 65, where one verdict becomes another; a card
    that never says its temperature; use of exactly 15 and of 14; ten
    samples and nine; a clock, a use and a temperature that take every
    point away."""
    if not tool():
        return
    def turn(round, one, other):
        return one if round % 2 == 0 else other
    series = {
        "exactly 85": [dict(temp_c=60, gpu_util_pct=turn(r, 85, 55), core_mhz=2100) for r in range(12)],
        "exactly 65": [dict(temp_c=turn(r, 50, 75), gpu_util_pct=turn(r, 90, 80), core_mhz=2100) for r in range(12)],
        "no temperature": [dict(temp_c=0, gpu_util_pct=99, core_mhz=2100) for r in range(12)],
        "use of 15": [dict(temp_c=60, gpu_util_pct=15, core_mhz=2100) for r in range(12)],
        "use of 14": [dict(temp_c=60, gpu_util_pct=14, core_mhz=2100) for r in range(12)],
        "ten samples": [dict(temp_c=60, gpu_util_pct=99, core_mhz=2100) for r in range(10)],
        "nine samples": [dict(temp_c=60, gpu_util_pct=99, core_mhz=2100) for r in range(9)],
        "every point gone": [dict(temp_c=turn(r, 40, 80), gpu_util_pct=turn(r, 15, 100), core_mhz=turn(r, 300, 2900)) for r in range(12)],
        "one working sample among idle ones": [dict(temp_c=60, gpu_util_pct=99 if r == 5 else 3, core_mhz=2100) for r in range(12)],
        "a reading in between that is none": [None if r % 3 == 0 else dict(temp_c=60 + r, gpu_util_pct=50 + r, core_mhz=2100 - r) for r in range(15)],
    }
    answered = 0
    for name, cards in series.items():
        s = [now(100.0), card(**A_CARD), post("/api/perf/start", {"consumer": name})]
        for round, says in enumerate(cards):
            s += [now(100.0 + round * 0.25), {"do": "answers", "question": "gpu_live", "answer": says and dict(A_CARD, **says)}, STEP]
        s += [now(100.0 + len(cards) * 0.25), get("/api/perf/score?seconds=60"), get("/api/perf/score"), get("/api/perf/samples"), get("/api/perf/status")]
        done, passed_on = both(s)
        assert passed_on == 0, name
        answered += done
    assert answered == 5 * len(series)
    # (and that these are the edges they are said to be: Python's own word)
    python = Python()
    try:
        for name, wanted in (("exactly 85", (85.0, "healthy")), ("exactly 65", (65.0, "warn")), ("no temperature", (100.0, "healthy")),
                             ("use of 14", (None, "idle")), ("nine samples", (None, "no-data")), ("every point gone", (0.0, "unstable"))):
            python.do(now(100.0))
            python.do(card(**A_CARD))
            python.do(post("/api/perf/stop", {"force": True}))
            python.do(STEP)
            python.do(post("/api/perf/start", {}))
            for round, says in enumerate(series[name]):
                python.do(now(100.0 + round * 0.25))
                python.do({"do": "answers", "question": "gpu_live", "answer": says and dict(A_CARD, **says)})
                python.do(STEP)
            python.do(now(100.0 + len(series[name]) * 0.25))
            score = json.loads(python.do(get("/api/perf/score?seconds=60"))["body"])
            assert (score["score"], score["verdict"]) == wanted, (name, score)
    finally:
        python.close()


def test_a_card_that_answers_only_to_nvidia_smi_gives_the_same_samples():
    """The card can stop answering the backend's helper in the middle of a
    run: the driver's library is closed for a driver install (fifteen
    minutes), or the driver was reset (the helper waits half a minute).  And
    on some PCs it never answers the helper.  Python's sampler asks
    nvidia-smi for every sample then, and its chart goes on.  The shell
    starts no nvidia-smi and reads no card: its samples are the backend's
    reading, which is that same reading.  So the answers stay Python's.

    On this machine nvidia-smi is there (the sandbox's is not, unless a test
    says so), and prints what a card prints."""
    if not tool():
        return
    rows = ["2100, 9501, 61, 215.44, 97, 40, 55, 320.00, 2100, 9501, 2200, 9800",
            "1987, 9000, 53.64, 180.07, 88, 35, [N/A], 320.00, [N/A], [N/A], 2200, 9800",
            "0, 0, 0, 0.00, 0, 0, 0, 0.00, 0, 0, 0, 0",
            "2100, 9501, 61, [N/A], 97, 40, 55, [N/A], 2100, 9501, 2200, 9800",
            "[N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A], [N/A]",
            "2100,9501,61,215.44,97,40,55,320.00,2100,9501,2200,9800\n1500, 7000, 40, 100.00, 10, 5, 30, 200.00, 1500, 7000, 1800, 8000",
            "not what nvidia-smi prints", None]
    s = [now(100.0), card(**A_CARD), post("/api/perf/start", {"consumer": "hwmon-page"}), STEP, get("/api/perf/status")]
    at = 100.0
    for round, row in enumerate(rows * 2):
        at += 0.2
        # (the helper says nothing: the card stopped answering it)
        s += [now(at), card(), {"do": "smi", "says": row}, STEP, get("/api/perf/status")]
        if round % 4 == 3:
            # ... and answers again for a moment, with nvidia-smi still there
            at += 0.2
            s += [now(at), card(**dict(A_CARD, temp_c=44, gpu_util_pct=20 + round)), STEP, get("/api/perf/status")]
    # A minute of it: the chart and the score are of nvidia-smi's readings.
    for round in range(60):
        at += 0.2
        s += [now(at), {"do": "smi", "says": f"{2000 + round % 7 * 20}, 9501, {60 + round % 5}, {150 + round}.5, {70 + round % 30}, 40, 55, 320.00, 0, 0, 2200, 9800"},
              card(), STEP]
    s += [get("/api/perf/samples?seconds=60"), get("/api/perf/score?seconds=30"), get("/api/perf/score?seconds=12"), get("/api/perf/status")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered == 2 + 2 * len(rows) + 4 + 4
    # (and that this was nvidia-smi's reading, not a sample that says "none")
    python = Python()
    try:
        python.do(card(**A_CARD))
        python.do(post("/api/perf/start", {}))
        python.do(card())
        python.do({"do": "smi", "says": rows[1]})
        assert python.card_as_the_backend_reads_it()["temp_c"] == 53
        python.do(STEP)
        last = json.loads(python.do(get("/api/perf/status"))["body"])["last"]
        assert (last["ok"], last["temp"], last["power"], last["fan"]) == (True, 53, 180.07, 0)
    finally:
        python.close()


def test_while_a_drivers_installer_runs_the_samples_stay_pythons():
    """The page asks for a driver's install.  The shell ends its reader
    before the backend hears of it, and starts none for fifteen minutes; the
    backend tells its own helper to let go for as long, and Python's sampler
    reads the card through nvidia-smi meanwhile.  The shell's samples are
    the backend's reading for that time: the same ones."""
    if not tool():
        return
    smi = lambda temp: {"do": "smi", "says": f"2100, 9501, {temp}, 215.44, 97, 40, 55, 320.00, 2100, 9501, 2200, 9800"}    # noqa: E731
    started = {"do": "install", "status": 200, "body": json.dumps({"ok": True, "msg": "NVIDIA installer launched."})}
    refused = {"do": "install", "status": 200, "body": json.dumps({"ok": False, "err": "An install is already in progress"})}
    s = [now(100.0), card(**A_CARD), post("/api/perf/start", {"consumer": "hwmon-page"})]
    for round in range(3):
        s += [now(100.0 + round * 0.2), STEP]
    s += [get("/api/perf/status"), now(100.6), started,
          # (from here the backend's helper stays off the library, and nvidia-smi answers)
          card(), smi(58)]
    for round in range(1, 21):
        s += [now(100.6 + round * 0.2), smi(58 + round % 4), STEP]
    # The PC's clock is set forward by a day in the middle of it: no time
    # has gone by, and the reader stays away (the sample is stamped with
    # the new time, in both).
    s += [{"do": "pcs_clock", "time": 100.6 + 86_400.0}, STEP]
    s += [get("/api/perf/status"), get("/api/perf/samples"), get("/api/perf/score")]
    # Ten minutes in the page asks again and is refused; nothing changes.
    s += [now(700.0), refused, STEP, now(999.0), STEP, now(1000.5), STEP, get("/api/perf/status")]
    # The fifteen minutes are over: the helper answers again, in both.
    s += [now(1000.6), card(**dict(A_CARD, temp_c=64)), STEP, now(1000.8), STEP, get("/api/perf/status"), get("/api/perf/samples?seconds=5")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered == sum(1 for step in s if step["do"] == "call")
    counted = both.counted
    # Three readings of the first reader, twenty-four of the backend while
    # it was away, two of the second reader; and no reader in between.
    assert (counted["from_the_reader"], counted["from_the_backend"], counted["readers_started"], counted["reader"]) == (5, 24, 2, 5), counted
    # An install that was not started: the reader is away for the call, and
    # back for the next sample.
    not_started = {"do": "install", "status": 200, "body": json.dumps({"ok": False, "err": "Driver installer not downloaded yet"})}
    s = [now(100.0), card(**A_CARD), post("/api/perf/start", {}), STEP, now(100.2), not_started, STEP, now(100.4), STEP, get("/api/perf/status")]
    both(s)
    assert (both.counted["from_the_reader"], both.counted["from_the_backend"], both.counted["readers_started"]) == (3, 0, 2), both.counted
    # (what the backend would have found at the moment it heard of the call)
    shell = Shell()
    try:
        shell.do(card(**A_CARD))
        shell.do(post("/api/perf/start", {}))
        shell.do(STEP)
        assert shell.do({"do": "asked"})["readers_running"] == [1]
        assert shell.do(started) == {"status": 200, "readers_when_the_backend_heard": []}
        assert shell.do({"do": "asked"})["readers_running"] == []
    finally:
        shell.close()


def test_a_reader_that_gives_nothing_leaves_the_samples_as_python_has_them():
    """The card is fine and the reader is not: it does not answer in time,
    answers what cannot be read, cannot be started.  The shell ends it,
    takes the backend's reading (the same card, read by the backend's
    helper), and tries a reader again half a minute later.  And a reader
    nobody asked for ten seconds is ended, and started again when a reading
    is wanted."""
    if not tool():
        return
    reader = lambda **does: dict({"do": "reader"}, **does)                # noqa: E731
    s = [now(10.0), card(**A_CARD), post("/api/perf/start", {}), STEP, get("/api/perf/status")]
    at = 10.0
    troubles = (reader(says="silent"), reader(says="garbled"), reader(says="none"), reader(says=dict(A_CARD, temp_c=None)))
    for trouble in troubles:
        at += 0.2
        s += [now(at), trouble, STEP, get("/api/perf/status")]
        # ... it is in order again at once, and not asked for half a minute
        s += [reader(says=A_CARD)]
        for later in (0.2, 15.0, 29.8):
            s += [now(at + later), STEP]
        at += 30.0
        s += [now(at), STEP, get("/api/perf/status")]
    # Nobody asks for ten seconds (the page was left and the sampler
    # stopped): the reader is ended.
    s += [post("/api/perf/stop", {}), STEP, now(at + 5.0), {"do": "tend"}, now(at + 10.0), {"do": "tend"}]
    # The next reading that is wanted starts another: which cannot be
    # started just then, and can half a minute later.
    s += [reader(cannot_start=True), now(at + 20.0), post("/api/perf/start", {}), STEP, get("/api/perf/status"), reader(cannot_start=False),
          now(at + 25.0), STEP, now(at + 50.0), STEP, get("/api/perf/status"), get("/api/perf/samples?seconds=60")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered == sum(1 for step in s if step["do"] == "call")
    counted = both.counted
    # One sample of each trouble and three within each pause are the
    # backend's, and two while no reader could be started; the rest are a
    # reader's.
    backends = 4 * len(troubles) + 2
    assert (counted["from_the_backend"], counted["from_nowhere"], counted["from_the_reader"]) == (backends, 0, counted["sampled"] - backends), counted
    # A reader for the first sample, one after each trouble, one after the
    # idle one was ended: and that last one runs, and no other.
    assert (counted["readers_started"], counted["readers_running"]) == (len(troubles) + 2, [len(troubles) + 2]), counted
    # (the idle one was there after five seconds, and gone after ten)
    shell = Shell()
    try:
        for step in (now(50.0), card(**A_CARD), post("/api/perf/start", {}), STEP, post("/api/perf/stop", {}), STEP):
            shell.do(step)
        shell.do(now(59.9))
        assert shell.do({"do": "tend"}) == {"still_one": True}
        shell.do(now(60.0))
        assert shell.do({"do": "tend"}) == {"still_one": False} and shell.do({"do": "asked"})["readers_running"] == []
    finally:
        shell.close()


def test_without_any_reading_the_samples_say_so_on_both_sides():
    """No NVIDIA card, no driver, no nvidia-smi: the backend has no reading,
    Python's samples say "no reading", and so do the shell's.  The sampler
    is the shell's all the same: every call is answered, none passed on."""
    if not tool():
        return
    s = [now(1.0), card(), *EVERYTHING, post("/api/perf/start", {"consumer": "hwmon-page"})]
    for round in range(1, 16):
        s += [now(1.0 + round * 0.2), STEP]
    s += [*EVERYTHING, post("/api/perf/stop", {"consumer": "hwmon-page"}), STEP, *EVERYTHING]
    # ... and a card that turns up later (the driver was installed).
    s += [now(10.0), post("/api/perf/start", {"consumer": "hwmon-page"}), STEP, card(**A_CARD), now(10.2), STEP, get("/api/perf/status"),
          {"do": "smi", "says": "2100, 9501, 61, 215.44, 97, 40, 55, 320.00, 2100, 9501, 2200, 9800"}, card(), now(10.4), STEP, get("/api/perf/status")]
    answered, passed_on = both(s)
    assert passed_on == 0 and answered == sum(1 for step in s if step["do"] == "call")
    python = Python()
    try:
        python.do(post("/api/perf/start", {}))
        python.do(STEP)
        assert python.card_as_the_backend_reads_it() == {"ok": False, "err": "No NVIDIA GPU / nvidia-smi missing"}
        assert json.loads(python.do(get("/api/perf/status"))["body"])["last"] == dict(ts=0.0, ok=False, core=0, mem=0, temp=0, power=0.0, util=0, memutil=0, fan=0)
    finally:
        python.close()


# ═════════════════════════════════════════════════════════════════════
# What has moved only reads, and what has not moved is as it was
# ═════════════════════════════════════════════════════════════════════
def moved_sources():
    folder = os.path.join(S.CORE, "src", "moved")
    return {name: open(os.path.join(folder, name), encoding="utf-8").read() for name in sorted(os.listdir(folder)) if name.endswith(".rs")}


def test_what_has_moved_can_only_read_the_machine():
    """The helper has two doors: `answer` for questions, which only read, and
    `act` for the two things that change the machine.  The shell uses the
    first, in one place, and names the second nowhere.  And what has moved
    reaches the machine through that one place only: it starts no program,
    writes no file, and calls nothing of Windows itself.  (What it asks of
    the backend, it asks through a way the shell hands it: it opens no
    connection of its own.)"""
    sources = moved_sources()
    assert set(sources) == {"card.rs", "hw.rs", "mod.rs", "perf.rs"}, "a new module has moved: this test and the plan in docs/rust-port.md have to know it"
    everything = {os.path.join(where, name): open(os.path.join(where, name), encoding="utf-8").read()
                  for crate in (S.CORE, os.path.join(os.path.dirname(S.CORE), "app")) for where, _, names in os.walk(os.path.join(crate, "src"))
                  for name in names if name.endswith(".rs")}
    assert len(everything) > 15
    def not_its_tests(text):
        return text[:text.index("#[cfg(test)]")] if "#[cfg(test)]" in text else text

    # The helper is asked in two places, each once: by the shell's door (any
    # question but one about the card), and by the card's reader, which is a
    # program of its own (its one question, about the card).
    asks = {os.path.relpath(path, os.path.dirname(S.CORE)): not_its_tests(text).count("vispora_native::")
            for path, text in everything.items() if "vispora_native::" in not_its_tests(text)}
    assert asks == {os.path.join("core", "src", "moved", "mod.rs"): 1, os.path.join("core", "src", "bin", "gpu_reader.rs"): 1}, asks
    for path, text in everything.items():
        assert "vispora_native::act" not in text and "native::act(" not in text, f"{path} names the helper's door for actions"
    door = sources["mod.rs"]
    real = door[door.index("impl Readings for Real {"):door.index("/// A made-up machine")]
    assert "!question.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'_')" in real, "a question is a name, never a request's text"
    # The helper is asked in one place, through its door for questions: a
    # question by its name, with nothing beside it but for two questions,
    # each held to what it may be asked: WMI to tell rows, and an echo
    # request to an address written as four numbers.
    assert real.count("vispora_native::answer(") == 1 and real.count("vispora_native::") == 1
    assert "vispora_native::answer(&Json::Object(request).to_string())" in real
    assert ('"wmi" => with.len() == 2 && text("namespace").is_some() && text("query").is_some_and(|query| query.starts_with("SELECT ")),' in real
            and 'with.len() == 2 && text("target").is_some_and(plain_ipv4)' in real and "_ => with.is_empty()," in real)
    assert real.index("if !may {\n            return None;\n        }") < real.index("vispora_native::answer(")
    # The door is all there is: a question, the time, a clock that does not
    # jump, and letting time go by.
    trait = door[door.index("pub trait Readings: Send + Sync {"):door.index("/// The real machine.")]
    assert re.findall(r"fn (\w+)\(", trait) == ["ask", "ask_with", "now", "steady", "wait"]
    for name, text in sources.items():
        code = text[:text.index("#[cfg(test)]")]
        for word in ("std::process", "Command::new", "std::fs", "File::", "extern \"system\"", "unsafe", "crate::win", "crate::platform", "std::env",
                     "std::net", "crate::forward", "crate::gpu_reader"):
            assert word not in code, f"moved/{name} reaches the machine past its door: {word}"
    # The reader's program asks its one question and nothing else: the
    # question is written in it, nothing that comes in is put into it, and
    # it reads nothing of its command line or its environment.
    reader = not_its_tests(S.rust("src", "bin", "gpu_reader.rs"))
    assert 'const THE_QUESTION: &str = "{\\"q\\":\\"gpu_live\\"}";' in reader and "|| vispora_native::answer(THE_QUESTION)" in reader
    for word in ("std::env::args", "env::var", "format!(", "std::fs", "std::process", "std::net", "unsafe", "extern \"system\"", "vispora_shell_core"):
        assert word not in reader, f"the reader's program has {word}"
    assert reader.count("std::env::") == 1 and "std::env::remove_var(A_PLACE_FROM_THE_ENVIRONMENT);" in reader
    assert 'const A_PLACE_FROM_THE_ENVIRONMENT: &str = "ProgramW6432";' in reader
    helper = S.repo("native", "src", "gpu.rs")
    assert re.findall(r'std::env::var\("(\w+)"\)', helper) == ["ProgramW6432"], "the helper looks for the library in another place of the environment's as well"
    # The helper's own list of what a question can be has no action in it.
    native = S.repo("native", "src", "lib.rs")
    questions = native[native.index("fn dispatch(request: &str)"):native.index("fn dispatch_action(request: &str)")]
    for action in re.findall(r'^        "(\w+)" =>', native[native.index("fn dispatch_action(request: &str)"):], re.M):
        assert f'"{action}"' not in questions, action
    assert "pub fn answer(request: &str) -> String {\n    reply_text(std::panic::catch_unwind(|| dispatch(request)))" in native


def test_the_shell_takes_the_samplers_calls_and_no_others():
    """The calls the shell answers itself are the ones app.py has for the
    sampler, each with the method app.py takes it with."""
    source = S.repo("app.py")
    routes = set()
    for path, methods in re.findall(r'^@app\.route\("(/api/perf/[^"]*)"(?:, methods=\[([^\]]*)\])?\)', source, re.M):
        for method in (re.findall(r'"(\w+)"', methods) or ["GET"]):
            routes.add((method, path))
    assert routes == {("GET", "/api/perf/status"), ("GET", "/api/perf/samples"), ("GET", "/api/perf/score"), ("POST", "/api/perf/start"),
                      ("POST", "/api/perf/stop")}
    perf = moved_sources()["perf.rs"]
    taken = perf[perf.index("let answer = match (method, path) {"):perf.index("Some(Response::json(200, &answer))")]
    assert set(re.findall(r'\("(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)", "(/api/[^"]+)"\)', taken)) == routes
    assert "_ => return None," in taken, "what is not one of them is the backend's"
    door = moved_sources()["mod.rs"]
    answer = door[door.index("pub fn answer(&self, request: &Request) -> Option<Response> {"):door.index("// What a route of the backend reads off a call")]
    assert re.findall(r'starts_with\("([^"]+)"\)', answer) == ["/api/perf/", "/api/monitor/"] and "        }\n        None\n    }\n" in answer
    # The router asks what every call is asked first, and only then what has moved.
    router = S.rust("src", "router.rs")
    handle = router[router.index("pub fn handle(self: &Arc<Self>, request: &Request) -> Response {"):router.index("/// Why a call is not from the app's own page")]
    assert handle.index("if let Some(refusal) = self.stranger(request) {") < handle.index("moved.as_ref().and_then(|moved| moved.answer(request))") \
        < handle.index("if let Some(moved) = moved.filter(|_| crate::moved::Moved::starts_a_driver_install(request)) {") \
        < handle.index("return moved.card.around_a_driver_install(|| self.pass_on(request));") < handle.index("self.pass_on(request)\n            }")
    assert handle.count("moved.answer(") == 1 and handle.count("around_a_driver_install(") == 1


def test_the_backends_sampler_and_its_routes_are_as_they_were():
    """The Python module is not changed and not removed: it answers when the
    app is started as it always was, and behind the shell whenever the shell
    does not take a call."""
    app()
    source = S.repo("app.py")
    for route in ('@app.route("/api/perf/status")\ndef api_perf_status():', '@app.route("/api/perf/samples")\ndef api_perf_samples():',
                  '@app.route("/api/perf/score")\ndef api_perf_score():', '@app.route("/api/perf/start", methods=["POST"])\ndef api_perf_start():',
                  '@app.route("/api/perf/stop", methods=["POST"])\ndef api_perf_stop():'):
        assert route in source, route
    assert "return jsonify(perf_monitor.compute_stability_score(seconds=seconds))" in source
    # Behind the shell or not, the routes are there: nothing in them asks.
    routes = source[source.index("# ─── Live Performance Monitor (v3)"):source.index("# ─── Adaptive Tuning")]
    assert "_behind_shell" not in routes
    python = Python()
    try:
        python.do(card(**A_CARD))
        python.do(now(100.0))
        assert json.loads(python.do(post("/api/perf/start", {"consumer": "hwmon-page"}))["body"]) == {"ok": True, "consumers": ["hwmon-page"]}
        for round in range(12):
            python.do(now(100.0 + round * 0.2))
            python.do(STEP)
        score = json.loads(python.do(get("/api/perf/score"))["body"])
        assert (score["verdict"], score["score"], score["samples_used"]) == ("healthy", 100.0, 12)
    finally:
        python.close()
    # The shell answers them only when it was given its door; and it is told not to with one word.
    main = S.app_crate("src", "main.rs")
    assert 'const ALL_TO_BACKEND: &str = "--all-to-backend";' in main
    given = main[main.index("if all_to_backend {"):main.index("// The window's own folder, new at every start")]
    assert given.index("} else {") < given.index("shell.reads_with(Arc::new(moved::Real), true);")
    assert main.count("reads_with(") == 1 and "shell.moved" not in main
    shell = S.rust("src", "shell.rs")
    assert "moved: Mutex::new(None)," in shell, "a shell that was given no door passes every call on"


def test_the_lock_files_gained_this_repositorys_own_helper_and_nothing_else():
    """The shell asks the native helper as an ordinary Rust library
    (docs/rust-port.md).  It is a crate of this repository with no crates of
    its own: the lock files name it, without a place it was downloaded from,
    and nothing came with it."""
    helper = S.repo("native", "Cargo.toml")
    assert re.search(r"^\[dependencies\]\s*\n\s*\n\[profile\.release\]", helper, re.M), "the helper has dependencies now"
    for lock in (S.rust("Cargo.lock"), S.app_crate("Cargo.lock")):
        packages = re.split(r"\n\[\[package\]\]\n", lock)[1:]
        ours = {re.search(r'name = "([^"]+)"', p).group(1): p for p in packages if "source = " not in p}
        assert set(ours) - {"vispora-shell"} == {"vispora-shell-core", "vispora-updater-core", "vispora-native"}
        assert ours["vispora-native"].strip() == 'name = "vispora-native"\nversion = "0.1.0"', "it brings nothing with it"
        assert '"vispora-native",' in ours["vispora-shell-core"]
    manifest = S.rust("Cargo.toml")
    assert re.findall(r"^([\w-]+) = \{ path = ", manifest, re.M) == ["vispora-updater-core", "vispora-native"]


# ═════════════════════════════════════════════════════════════════════
# The driver's library, and a driver's installer
# ═════════════════════════════════════════════════════════════════════
def test_the_shell_never_opens_the_drivers_library():
    """Today's app asks its helper to let go of the driver's library
    (nvml.dll) before it starts NVIDIA's installer, and to leave it alone
    for fifteen minutes: the installer has to replace the file
    (core/driver_updater.py).  That reaches the backend's copy of the helper.

    Letting go is not enough.  Once NVIDIA's library has been started in a
    process it stays in that process, whatever is shut down or freed
    afterwards (tools/measure/nvml_release.py: run it to see).  The shell
    lives as long as the app does.  So the shell does not open the library,
    at any time: its door refuses every question about the card before the
    helper is asked.  The card is read by a program of its own, which can be
    ended, and by the backend.

    Here: that the door refuses them, that the helper opens the library for
    those questions and for no others, and that the backend's own letting go
    is as it was.  That the library is not in the shell's process is seen
    in the shell's own tests (cargo test, on a PC with an NVIDIA card), in
    the test below that runs the trial tool with its reader, and in the
    trial of the window (shell_rs/app/try_it.py --moved)."""
    app()
    from core import driver_updater as DU
    door = moved_sources()["mod.rs"]
    real = door[door.index("impl Readings for Real {"):door.index("/// A made-up machine")]
    assert real.index('if question.starts_with("gpu_") {\n            return None;\n        }') < real.index("vispora_native::answer(")
    # The helper's questions that open the library are the ones about the
    # card, and each of those is named "gpu_...".
    native = S.repo("native", "src", "lib.rs")
    questions = native[native.index("fn dispatch(request: &str)"):native.index("fn dispatch_action(request: &str)")]
    arms = re.findall(r'^        "(\w+)" => (.*?)(?=^        "\w+" =>|^        other =>|^        _ =>)', questions, re.M | re.S)
    assert len(arms) > 10, len(arms)
    of_the_card = {name for name, does in arms if "gpu::" in does}
    assert of_the_card == {"gpu_live", "gpu_identity", "gpu_release"} and all(name.startswith("gpu_") for name in of_the_card)
    folder = os.path.join(os.path.dirname(os.path.dirname(S.CORE)), "native", "src")
    names_it = sorted(name for name in os.listdir(folder) if name.endswith(".rs")
                      and re.search(r"nvml|gpu::", open(os.path.join(folder, name), encoding="utf-8").read(), re.I))
    assert names_it == ["gpu.rs", "lib.rs"], f"the driver's library is named somewhere else in the helper: {names_it}"
    # Nothing else in the shell names the driver's library (outside what is
    # said in a comment, and its tests): the one library the shell opens by
    # name is Windows' own for unpacking.
    for crate in (S.CORE, os.path.join(os.path.dirname(S.CORE), "app")):
        for where, _, names in os.walk(os.path.join(crate, "src")):
            for name in (name for name in names if name.endswith(".rs")):
                text = open(os.path.join(where, name), encoding="utf-8").read()
                code = "\n".join(line for line in text.split("#[cfg(test)]\nmod tests")[0].splitlines() if not line.strip().startswith("//"))
                assert "nvml" not in code.lower(), f"{name} names the driver's library"
                assert ("LoadLibrary" in code) == (name == "win.rs"), name
                # (the card's question is written in the reader's program, and nowhere in the shell)
                assert ("gpu_live" in code) == (os.path.basename(where) == "bin" and name == "gpu_reader.rs"), f"{name} names the card's question"
    win = S.rust("src", "win.rs")
    assert win.count("LoadLibraryExW(") == 2 and "let library = LoadLibraryExW(name.as_ptr(), std::ptr::null_mut(), LOAD_LIBRARY_SEARCH_SYSTEM32);" in win
    assert os.path.exists(os.path.join(os.path.dirname(os.path.dirname(S.CORE)), "tools", "measure", "nvml_release.py"))
    # The backend's part is as it was: the app lets go once it knows it will
    # start the installer, and before it does; asked of its helper once, for
    # that long.
    updater = S.repo("core", "driver_updater.py")
    install = updater[updater.index("def install_driver() -> dict:"):updater.index("def get_status() -> dict:")]
    assert install.index('return {"ok": False, "err": "An install is already in progress"}') < install.index("_release_gpu_library()") \
        < install.index('subprocess.Popen(["explorer.exe", path]')
    del m.NATIVE_CALLS[:]
    m.NATIVE["gpu_release"] = lambda hold_s: {"released": True, "hold_s": hold_s}
    try:
        DU._release_gpu_library()
    finally:
        m.NATIVE.pop("gpu_release", None)
    assert m.NATIVE_CALLS == [("gpu_release", {"hold_s": 900})]


def test_the_backends_reading_of_the_card_is_what_its_sampler_reads():
    """Where the card's own reader is away or has no reading, the shell asks
    the backend for the card's reading with the call the GPU overclock page
    uses for its live numbers.  That call is `read_live_state()` and nothing
    else: the very reading the Python sampler takes.  It only reads; nothing
    of the overclock's own code has moved or is called from the shell."""
    app()
    source = S.repo("app.py")
    assert ('@app.route("/api/gpu/oc/live")\ndef api_gpu_oc_live():\n    """Live GPU stats: clocks, temp, power, utilization. Poll this during stress tests."""\n'
            '    return jsonify(gpu_overclock.read_live_state())') in source
    assert "s = gpu_overclock.read_live_state()" in S.repo("core", "perf_monitor.py")
    sources = moved_sources()
    card_rs = sources["card.rs"]
    assert 'pub const THE_BACKENDS_READING: &str = "/api/gpu/oc/live";' in card_rs
    code = "".join(text[:text.index("#[cfg(test)]")] for text in sources.values())
    assert code.count('"/api/') == 2 + code.count('"/api/perf/') + code.count('"/api/monitor/'), "the shell names another call of the backend's as well"
    assert 'pub const THE_DRIVER_INSTALL: &str = "/api/driver/install";' in sources["mod.rs"]
    backends = card_rs[card_rs.index("fn the_backends(&self) -> Option<Live> {"):card_rs.index("impl State {")]
    assert "let said = ask(THE_BACKENDS_READING)?;" in backends and card_rs.count("ask(THE_BACKENDS_READING)") == 1
    # The card is read in two places and no third: the reader, where one may
    # be asked, and the backend.  Never through the door to the shell's own
    # library, which is used for the time and nothing else.
    live = card_rs[card_rs.index("pub fn live(&self) -> Option<Live> {"):card_rs.index("/// What the card's own reader says now: all of it.")]
    assert live.index("if let Some(reading) = self.from_the_reader() {") < live.index("let live = self.the_backends();")
    reader = card_rs[card_rs.index("pub fn from_the_reader(&self) -> Option<Reading> {"):card_rs.index("/// The reader to ask now, started if need be")]
    assert reader.index("self.the_reader(now)?") < reader.index("reader.read(within)") and "the_backends" not in reader
    code = card_rs[:card_rs.index("#[cfg(test)]")]
    assert code.count("door.") == 2 and "self.inner.door.now()" in code and "self.inner.door.steady()" in code, "the card asks the shell's own door for something"
    # How long a reader is idle, away or paused is counted on the clock
    # that does not jump; the PC's own clock is for what a reading is
    # stamped with, and is not used in the card at all.
    assert code.count("self.steady()") == 3 and code.count("self.now()") == 0
    # (the sampler reads the card through that and through nothing else)
    perf = sources["perf.rs"]
    assert perf[:perf.index("#[cfg(test)]")].count("card.live()") == 1 and "Readings" not in perf[:perf.index("#[cfg(test)]")]
    # How the shell asks: as it asks for the tray, with the run's secret,
    # and only a backend that is up.
    shell = S.rust("src", "shell.rs")
    assert 'moved.asks_the_backend_with(Arc::new(move |path: &str| shell.upgrade()?.ask_backend("GET", path)));' in shell
    python = Python()
    try:
        client = python.client
        # The helper answers: its reading, as the sampler takes it.
        python.do(card(**A_CARD))
        said = client.get("/api/gpu/oc/live", headers=python.headers).get_json()
        assert said == GO.read_live_state() and (said["ok"], said["temp_c"], said["power_w"]) == (True, 61, 215.44)
        # The helper says nothing, nvidia-smi does; and neither does.
        python.do(card())
        python.do({"do": "smi", "says": "2100, 9501, 58.4, 190.20, 90, 40, 55, 320.00, 2100, 9501, 2200, 9800"})
        said = client.get("/api/gpu/oc/live", headers=python.headers).get_json()
        assert (said["ok"], said["temp_c"], said["power_w"]) == (True, 58, 190.2)
        python.do({"do": "smi", "says": None})
        assert client.get("/api/gpu/oc/live", headers=python.headers).get_json() == {"ok": False, "err": "No NVIDIA GPU / nvidia-smi missing"}
    finally:
        python.close()


# ═════════════════════════════════════════════════════════════════════
# The card's own reader, and a driver's installer
# ═════════════════════════════════════════════════════════════════════
READER = S.built_tool("VisporaGpuReader.exe")


def holds(pid, library):
    """Is a library of this name loaded in the process `pid`, as Windows
    lists it?  (A read.)"""
    listed = m.REAL_POPEN(["tasklist", "/m", library, "/fi", "PID eq %d" % pid, "/fo", "csv", "/nh"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).communicate()[0]
    return '"%d"' % pid in listed


def test_the_install_begins_in_one_place_and_both_sides_agree_on_it():
    """The reader has to be gone before a driver's installer is started.
    The app starts one in one place, behind one call of the page, and that
    call goes through the shell: the shell ends its reader before it passes
    the call on, and keeps none for as long as the backend keeps off its own
    copy of the library, unless the backend says that nothing was started."""
    app()
    from core import driver_updater as DU
    source, updater = S.repo("app.py"), S.repo("core", "driver_updater.py")
    # One way in: the route, and the function it calls, which has no other caller.
    assert '@app.route("/api/driver/install", methods=["POST"])\ndef api_driver_install():\n    return jsonify(driver_updater.install_driver())' in source
    assert source.count("install_driver(") == 1
    core = os.path.join(os.path.dirname(os.path.dirname(S.CORE)), "core")
    called = [(name, line.strip()) for name in sorted(os.listdir(core)) if name.endswith(".py")
              for line in open(os.path.join(core, name), encoding="utf-8", errors="replace").read().splitlines()
              if "install_driver(" in line and not line.startswith("def install_driver(") and "`install_driver()`" not in line]
    assert called == [], f"an installer is started from somewhere else as well, which the shell does not see: {called}"
    # Nothing but that function starts the installer's program.
    install = updater[updater.index("def install_driver() -> dict:"):updater.index("def get_status() -> dict:")]
    assert install.index('return {"ok": False, "err": "An install is already in progress"}') < install.index("_release_gpu_library()") \
        < install.index('subprocess.Popen(["explorer.exe", path]')
    assert updater.count("_release_gpu_library()") == 1 + 1, "the app lets go of the library somewhere else as well"
    # It says "ok" only when it started one: that is what the shell goes by.
    assert install.count('"ok": True') == 1 and install.rindex('return {"ok": True,') > install.index("_release_gpu_library()")
    sources = moved_sources()
    assert f"pub const HOLD_FOR_AN_INSTALL_S: f64 = {float(DU._GPU_LIBRARY_HOLD_S)};" in sources["card.rs"]
    assert 'request.method == "POST" && as_the_backend_reads_a_path(request.path()) == THE_DRIVER_INSTALL' in sources["mod.rs"]
    around = sources["card.rs"]
    around = around[around.index("pub fn around_a_driver_install("):around.index("/// The shell quits: the reader is ended")]
    # Counted as on its way, the reader ended and seen to be gone, and only
    # then passed on; counted off when it is answered, and a time to stay
    # away only where the backend says that an installer was started.
    assert around.index("st.installs += 1;") < around.index("st.reader.take()") < around.index("reader.end()") < around.index("let answer = pass_on();") \
        < around.index('let started = answer.status == 200 && answer.to_json().get("ok").is_some_and(Json::truthy);') \
        < around.index("st.installs -= 1;") < around.index("if started {\n                // (later than the time of any installer that was started before)\n"
                                                           "                st.away_until = Some(until);")
    assert "let away = st.installs > 0 || st.away_until.is_some_and(|until| now < until);" in sources["card.rs"]
    # The way the web framework reads a path, and so which calls are this
    # one: the app's own routing, asked with a few ways of writing it.
    client = A.app.test_client()
    headers = {"Host": f"127.0.0.1:{config.APP_PORT}", A._API_TOKEN_HEADER: A._API_TOKEN}
    install_was = A.app.view_functions["api_driver_install"]
    reached = []
    A.app.view_functions["api_driver_install"] = lambda: (reached.append(1), A.jsonify({"ok": False, "err": "a test: nothing is started"}))[1]
    try:
        for written, is_it in (("/api/driver/install", True), ("/api/driver/install?x=1", True), ("/api/driver/%69nstall", True), ("/api/%64river/install", True),
                               ("/api/driver/instal%6C", True), ("/api/driver/install/", False), ("/api/driver/Install", False), ("/api/driver/installer", False),
                               ("/api/driver/install%00", False)):
            del reached[:]
            client.post(written, headers=headers, data=b"{}")
            assert bool(reached) == is_it, f"{written}: the app's own routing {'took' if reached else 'did not take'} it for the install"
        del reached[:]
        for method in ("get", "put", "delete", "patch", "head", "options"):
            getattr(client, method)("/api/driver/install", headers=headers)
        assert reached == [], "a method that is not POST reached the install"
    finally:
        A.app.view_functions["api_driver_install"] = install_was


def test_the_trial_tool_with_its_reader_and_a_drivers_install_for_real():
    """The shell without a window (the trial tool), a real reader beside it
    (VisporaGpuReader.exe, reading this PC's card: it only reads), and the
    stand-in backend.  The sampler's readings are the reader's; the shell's
    own process never has the driver's library in it; and when the page
    asks for a driver's install, there is no reader at the moment the
    backend hears of it.  Nothing is installed or started: the stand-in
    only says that an installer was, or was not."""
    app()
    if not (S.TRY and READER):
        return
    t = S.Trial(tool_args=["--readings", "real", "--card-reader", READER])
    try:
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        shell_pid = t.first["pid"]
        said = lambda call: json.loads(call["body"])                        # noqa: E731
        status = lambda: said(t.call("GET", "/api/perf/status"))            # noqa: E731

        def wait_for(what, within=20):
            ends = time.time() + within
            while time.time() < ends:
                got = what()
                if got:
                    return got
                time.sleep(0.05)
            return what()

        # The backend's own reading, on its fake machine: 71 degrees.
        t.call("POST", "/api/stand-in/card", body=json.dumps(dict(A_CARD, temp_c=71)))
        assert t.ask(do="state")["card_reader_pid"] is None and not holds(shell_pid, "nvml.dll"), "a reader, or the library, before anybody wants a reading"
        assert said(t.call("POST", "/api/perf/start", body='{"consumer": "trial"}'))["ok"] is True
        assert wait_for(lambda: status()["sample_count"] >= 4), status()
        state = t.ask(do="state")
        if state["card_readings_from"]["reader"] == 0:
            # This PC's card gives the reader no reading (no NVIDIA card or
            # driver): the reader was ended, and the samples are the backend's.
            assert state["card_reader_pid"] is None and status()["last"]["temp"] == 71, state
            assert any("gave no reading" in line for line in state["history"]), state["history"]
            assert not holds(shell_pid, "nvml.dll")
            return
        reader_pid = state["card_reader_pid"]
        assert reader_pid and reader_pid != shell_pid and state["card_readings_from"]["backend"] == 0, state
        # The library is in the reader, and not in the shell.
        assert holds(reader_pid, "nvml.dll") and not holds(shell_pid, "nvml.dll")
        assert status()["last"]["ok"] is True

        # The page asks for a driver's install, and the backend starts none.
        assert said(t.call("POST", "/api/stand-in/driver-install", body='{"starts": false}'))["ok"] is True
        answer = t.call("POST", "/api/driver/install", body="{}")
        assert (answer["status"], said(answer)["ok"]) == (200, False), answer
        seen = said(t.call("POST", "/api/stand-in/driver-install", body='{"starts": true}'))["readers_when_asked"]
        assert seen == [[]], f"when the backend heard of the install, a reader was running: {seen}"
        assert S.ended(reader_pid, 5), "the reader that was ended for the install is still there"
        # ... and the reader is back for the next readings: another one.
        second = wait_for(lambda: t.ask(do="state")["card_reader_pid"])
        assert second and second != reader_pid and holds(second, "nvml.dll")

        # The page asks again, and the backend says that it started one.
        answer = t.call("POST", "/api/driver/install", body="{}")
        assert (answer["status"], said(answer)["ok"]) == (200, True), answer
        seen = said(t.call("POST", "/api/stand-in/driver-install", body='{"starts": true}'))["readers_when_asked"]
        assert seen == [[], []], seen
        assert S.ended(second, 5)
        # No reader now, and none comes back; no process of the shell's has
        # the library; and the sampler goes on, with the backend's reading.
        before = status()["sample_count"]
        time.sleep(1.5)
        state, now = t.ask(do="state"), status()
        assert state["card_reader_pid"] is None and not holds(shell_pid, "nvml.dll"), state
        assert now["running"] is True and now["last"] == dict(now["last"], ok=True, temp=71), now
        assert state["card_readings_from"]["backend"] >= 5 and (now["sample_count"] > before or now["sample_count"] == 300)
        lines = "\n".join(state["history"])
        for line in (f"the graphics card's reader was ended first (pid {reader_pid}), and was seen to be gone",
                     "no graphics driver's installer was started: the graphics card's reader may be started again",
                     f"the graphics card's reader was ended first (pid {second}), and was seen to be gone",
                     "a graphics driver's installer was started: no reader for the graphics card for 900 s, the backend is asked meanwhile"):
            assert line in lines, f"not in the shell's log: {line}\n{lines}"
    finally:
        t.close()


def test_the_reader_goes_when_nobody_wants_it_and_when_the_shell_is_gone():
    """Ten seconds after the last reading that was asked for, the reader is
    ended.  And it never outlives the shell: not a quit, and not a shell
    that is ended by force (Windows ends the reader's job with it)."""
    app()
    if not (S.TRY and READER):
        return
    said = lambda call: json.loads(call["body"])                            # noqa: E731

    def a_running_reader(t):
        """The reader's process once the sampler has taken samples from it
        (None on a PC whose card gives a reader no reading)."""
        assert t.ask(do="wait", **{"for": "ready"}, within=90) == {"phase": "ready"}
        assert said(t.call("POST", "/api/perf/start", body='{"consumer": "trial"}'))["ok"] is True
        ends = time.time() + 20
        while time.time() < ends and said(t.call("GET", "/api/perf/status"))["sample_count"] < 3:
            time.sleep(0.05)
        state = t.ask(do="state")
        return state["card_reader_pid"] if state["card_readings_from"]["reader"] else None

    # Nobody wants readings any more: gone after ten seconds, and not before five.
    t = S.Trial(tool_args=["--readings", "real", "--card-reader", READER])
    try:
        reader_pid = a_running_reader(t)
        if reader_pid is None:
            return
        t.call("POST", "/api/perf/stop", body='{"consumer": "trial"}')
        stopped = time.time()
        time.sleep(5.0)
        assert t.ask(do="state")["card_reader_pid"] == reader_pid, "the reader was ended within five seconds of the last reading"
        assert S.ended(reader_pid, 12), "the reader still runs %.0f s after the last reading was asked for" % (time.time() - stopped)
        took = time.time() - stopped
        assert 9.0 <= took <= 14.0, took
        state = t.ask(do="state")
        assert state["card_reader_pid"] is None
        assert f"the graphics card's reader was ended (pid {reader_pid}): nobody asked for a reading for 10 s" in state["history"]
        # A quit ends the one that runs then.
        assert said(t.call("POST", "/api/perf/start", body='{"consumer": "trial"}'))["ok"] is True
        ends = time.time() + 20
        again = None
        while time.time() < ends and not again:
            again = t.ask(do="state")["card_reader_pid"]
            time.sleep(0.05)
        assert again and again != reader_pid
        t.tell(do="quit")
        assert t.answer(60)["phase"] == "gone"
        assert S.ended(again, 5), "the reader outlived the shell's quit"
    finally:
        t.close()

    # The shell is ended by force, with the sampler running.
    t = S.Trial(tool_args=["--readings", "real", "--card-reader", READER])
    try:
        reader_pid = a_running_reader(t)
        backend = t.ask(do="state")["backend_pid"]
        t.process.kill()                                # (the trial tool's own process, and no other)
        t.process.wait(30)
        assert S.ended(reader_pid, 5), "the reader outlived a shell that was ended by force"
        assert S.ended(backend, 30), "the stand-in backend outlived the shell"
    finally:
        t.close()
