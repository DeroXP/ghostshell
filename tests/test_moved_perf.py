"""What has moved from the backend into the shell: the performance sampler.

Step 4 of docs/rust-port.md moves the backend into the shell one module at a
time.  The first is core/perf_monitor.py: what the graphics card is doing,
five times a second, for the Monitor page's live charts and its stability
score.  The shell (shell_rs/core/src/moved/perf.rs) answers its five calls
itself.  The Python module and its routes are as they were, and answer when
the app is started as it always was.

The card's reading is not the shell's own.  Reading the card means starting
NVIDIA's library in the reading process, and once started it never leaves
that process (tools/measure/nvml_release.py shows it), while a driver's
installer has to replace the file.  So the shell asks its backend for the
reading (GET /api/gpu/oc/live: `gpu_overclock.read_live_state()`, the very
reading the Python sampler takes) and never opens the library.

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
    (`vispora-shell-try --moved`), which reads the same script.  Before
    every sample it is told what the backend's call for the card's reading
    answers at that moment.  Its own made-up library is made to say
    something else of the card: the shell must not ask it.

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
                PM._sampler_loop(OneRound())
                self.sampled += 1
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
    """shell_rs/core/src/moved/, in the trial tool, on its made-up machine."""

    def __init__(self):
        self.tool = m.REAL_POPEN([S.TRY, "--moved"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def do(self, step):
        if step["do"] == "smi":
            return None                             # (the fake machine's nvidia-smi is the backend's to start, never the shell's)
        if step["do"] == "answers" and step["question"] == "gpu_live":
            # What the backend's helper says of the card, the shell's own
            # library does not say here: it says something else, at all
            # times, so that a shell that asked it would be found out.
            step = dict(step, answer=NOT_THE_CARD)
        self.tool.stdin.write(json.dumps(step) + "\n")
        self.tool.stdin.flush()
        said = json.loads(self.tool.stdout.readline())
        assert "error" not in said, said
        return said if step["do"] in ("call", "asked") else None

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


def both(script):
    """Ask the Python module and the shell the same things in the same
    order.  Every answer the shell gives must be Python's; where the shell
    passes a call on, Python must not have touched the sampler.  Returns how
    many calls the shell answered and how many it passed on.

    Before every sample the shell's side is told what the backend's own
    call for the card's reading answers on this machine at that moment.
    At the end: the shell asked the backend for the card once for every
    sample Python took, and its own library never."""
    python, shell = Python(), Shell()
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
        assert asked["library"] == [], f"the shell asked its own library: {asked['library'][:5]}"
        assert asked["backend"] == ["/api/gpu/oc/live"] * python.sampled, (len(asked["backend"]), python.sampled, sorted(set(asked["backend"])))
        return answered, passed_on
    finally:
        python.close()
        shell.close()


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
    assert set(sources) == {"mod.rs", "perf.rs"}, "a new module has moved: this test and the plan in docs/rust-port.md have to know it"
    everything = {os.path.join(where, name): open(os.path.join(where, name), encoding="utf-8").read()
                  for crate in (S.CORE, os.path.join(os.path.dirname(S.CORE), "app")) for where, _, names in os.walk(os.path.join(crate, "src"))
                  for name in names if name.endswith(".rs")}
    assert len(everything) > 15
    def not_its_tests(text):
        return text[:text.index("#[cfg(test)]")] if "#[cfg(test)]" in text else text

    asks = {path: not_its_tests(text).count("vispora_native::") for path, text in everything.items() if "vispora_native::" in not_its_tests(text)}
    assert list(asks.values()) == [1] and list(asks)[0].endswith(os.path.join("moved", "mod.rs")), asks
    for path, text in everything.items():
        assert "vispora_native::act" not in text and "native::act(" not in text, f"{path} names the helper's door for actions"
    door = sources["mod.rs"]
    real = door[door.index("impl Readings for Real {"):door.index("/// A made-up machine")]
    assert "!question.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'_')" in real, "a question is a name, never a request's text"
    # The helper is asked in one place, through its door for questions: a
    # question by its name, with nothing beside it.
    assert real.count("vispora_native::answer(") == 1 and real.count("vispora_native::") == 1
    assert 'vispora_native::answer(&format!("{{\\"q\\":\\"{question}\\"}}"))' in real
    # The door is all there is: a question, and the time.
    trait = door[door.index("pub trait Readings: Send + Sync {"):door.index("/// The real machine.")]
    assert re.findall(r"fn (\w+)\(", trait) == ["ask", "now"]
    for name, text in sources.items():
        code = text[:text.index("#[cfg(test)]")]
        for word in ("std::process", "Command::new", "std::fs", "File::", "extern \"system\"", "unsafe", "crate::win", "crate::platform", "std::env",
                     "std::net", "crate::forward"):
            assert word not in code, f"moved/{name} reaches the machine past its door: {word}"
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
    assert re.findall(r'starts_with\("([^"]+)"\)', answer) == ["/api/perf/"] and "        }\n        None\n    }\n" in answer
    # The router asks what every call is asked first, and only then what has moved.
    router = S.rust("src", "router.rs")
    handle = router[router.index("pub fn handle(self: &Arc<Self>, request: &Request) -> Response {"):router.index("/// Why a call is not from the app's own page")]
    assert handle.index("if let Some(refusal) = self.stranger(request) {") < handle.index("moved.and_then(|moved| moved.answer(request))") \
        < handle.index("self.pass_on(request)\n            }")
    assert handle.count("moved.answer(") == 1


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
    The shell has a copy of its own, and nobody tells that one anything.

    And telling it would not be enough.  Once NVIDIA's library has been
    started in a process it stays in that process, whatever is shut down or
    freed afterwards (tools/measure/nvml_release.py: run it to see).  The
    shell lives as long as the app does.  So the shell does not open the
    library, at any time: its door refuses every question about the card
    before the helper is asked, and the card's reading is the backend's.

    Here: that the door refuses them, that the helper opens the library for
    those questions and for no others, and that the backend's own letting go
    is as it was.  That the library is not in the shell's process is seen
    in the shell's own tests (cargo test, on a PC with an NVIDIA card) and
    in the trial of the window (shell_rs/app/try_it.py --moved)."""
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
    """The shell asks the backend for the card's reading with the call the
    GPU overclock page uses for its live numbers.  That call is
    `read_live_state()` and nothing else: the very reading the Python
    sampler takes.  It only reads; nothing of the overclock's own code has
    moved or is called from the shell."""
    app()
    source = S.repo("app.py")
    assert ('@app.route("/api/gpu/oc/live")\ndef api_gpu_oc_live():\n    """Live GPU stats: clocks, temp, power, utilization. Poll this during stress tests."""\n'
            '    return jsonify(gpu_overclock.read_live_state())') in source
    assert "s = gpu_overclock.read_live_state()" in S.repo("core", "perf_monitor.py")
    door = moved_sources()["mod.rs"]
    assert 'pub const THE_BACKENDS_READING: &str = "/api/gpu/oc/live";' in door
    code = door[:door.index("#[cfg(test)]")]
    assert code.count('"/api/') == 1 + code.count('"/api/perf/'), "the shell asks the backend something else as well"
    live = door[door.index("pub fn live(&self) -> Option<Live> {"):door.index("/// Everything that has moved, with the door it reads the machine through.")]
    assert "let said = ask(THE_BACKENDS_READING)?;" in live and "self.door" not in live, "the card's reading is the backend's, and nothing else's"
    # (the sampler reads the card through that and through nothing else)
    perf = moved_sources()["perf.rs"]
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
