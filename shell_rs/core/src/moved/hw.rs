//! The hardware monitor: processor, graphics card, memory, how busy the
//! kernel is with deferred work, and a ping, once a second, for the Monitor
//! page.
//!
//! This is core/hw_monitor.py, moved: its loop, and the three calls that
//! belong to it (start, stop, snapshot).  The Python module stays, and stays
//! what answers when the app is started as it always was.  Its other two
//! calls stay the backend's behind the shell too: the list of programs
//! (`/api/monitor/processes`) starts PowerShell, and the benchmark
//! (`/api/monitor/benchmark`) starts PowerShell three times and writes a
//! file.  Neither is a reading that is taken all the time: each is started
//! by a button, once.
//!
//! **Where each reading comes from**, and it is the same place Python reads:
//!
//! * the processor's load, clock, and its time in DPCs and interrupts; the
//!   memory in use and the standby list; DPCs a second: the native library
//!   in the shell (`Readings::ask`).  Python reads three of these with
//!   calls of its own to Windows, which are the calls the library makes.
//! * the processor's temperature: WMI, through the library, in the order
//!   Python asks (LibreHardwareMonitor, OpenHardwareMonitor, the ACPI zone,
//!   the thermal zone's counter).
//! * the graphics card: its own reader (`Card::from_the_reader`: never the
//!   shell's own door, card.rs says why), then LibreHardwareMonitor for
//!   what the card does not say.
//! * the ping: one echo request through the library, to the address the
//!   page's start call names ("1.1.1.1" where it names none, and the page
//!   names none), with half a second to answer.  Only to an address written
//!   plainly as four numbers (`plain_ipv4`): no name is looked up here.
//!
//! **The shell starts no program for a reading.**  Python does, where its
//! helper has no answer: nvidia-smi for a card the helper cannot read,
//! PowerShell for a counter that gives nothing, ping.exe for a name or an
//! address written another way.  In each of those places the shell asks the
//! backend for that part of the pass (`THE_BACKENDS_PART`), and the backend
//! takes it with the very function its own loop would have used.  So the
//! page gets Python's answer there too, and the program is the backend's to
//! start, as it is today.  The same where Python would read a value in a
//! way of its own that this program does not copy (a number written as
//! text, a name that is a list).  If the backend's function raises, its
//! loop would have thrown the whole pass away; so does this one.  Where
//! there is no backend to ask, that reading is nothing for that pass.
//!
//! **One of each.**  The backend's own loop is started by its start call
//! and by nothing else, and behind the shell a start call reaches the
//! backend only where Python stops before it starts anything (a body that
//! is no JSON).  A body that this program cannot read and Python might is
//! refused here, not passed on.  The calls are known however their path is
//! written (mod.rs, `Moved::answer`).  What is kept from pass to pass is in
//! the shell: a backend that is started again changes nothing of it.
//!
//! It only reads.  Starting and stopping it starts and stops a thread of
//! the shell's own.  The standby list is read, never emptied.
//!
//! **It sleeps when nobody looks**, as Python's (core/hw_monitor.py): once
//! nobody has asked for the snapshot or started it for `IDLE_STOP_S`, its
//! loop ends; "running" stays what the person set; the next snapshot (or
//! start) forgets what was read before the sleep and wakes it.  A monitor
//! that was stopped is never woken by a snapshot.  **A place in WMI that
//! had nothing is not asked again for `QUIET_S`**, unless it gave a reading
//! within the last `QUIET_S` (`Before::quiet`, `Before::noted`), the same
//! rule as Python's `_from_a_place`.  Where the shell asks its backend for
//! a place's answer (one only Python reads), the place is still the
//! shell's to keep track of: the backend reads it as it is.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;

use super::{plain_ipv4, py_round, quoted, said_by, AskBackend, Card, Readings, Said, Say};
use crate::http::{Request, Response};
use crate::json::Json;

/// One pass every this many seconds.
pub const EVERY_S: f64 = 1.0;
/// Nobody has looked for this long: the loop sleeps (core/hw_monitor.py,
/// IDLE_STOP_S).
pub const IDLE_STOP_S: f64 = 30.0;
/// A place that had nothing is not asked again for this long, unless it
/// gave a reading within the last as long (core/hw_monitor.py, QUIET_S).
pub const QUIET_S: f64 = 30.0;
/// The places, by the names Python gives them.
pub const CPU_LHM: &str = "cpu temperature: LibreHardwareMonitor";
pub const CPU_OHM: &str = "cpu temperature: OpenHardwareMonitor";
pub const CPU_ACPI: &str = "cpu temperature: ACPI zone";
pub const CPU_THERMAL: &str = "cpu temperature: thermal zone counter";
pub const GPU_LHM: &str = "graphics card: LibreHardwareMonitor";
/// How many readings of each kind are kept: a minute of them.
pub const KEPT: usize = 60;
/// A reading that is the difference between two readings, with nothing to
/// compare with yet: how long the first one spans.
const FIRST_WAIT_S: f64 = 0.25;
/// Older than this is not "the pass before" (the monitor was off).
const STALE_S: f64 = 10.0;
/// The backend's call for one part of a pass, taken the way its own loop
/// takes it (app.py, api_monitor_part).  Behind the shell only.
pub const THE_BACKENDS_PART: &str = "/api/monitor/part";
/// Where the ping goes when the start call names no target.
pub const THE_USUAL_TARGET: &str = "1.1.1.1";
/// How long an echo request may take, in milliseconds.
pub const PING_WITHIN_MS: i128 = 500;

/// Above these the page is told so.
const TOO_WARM_CPU: f64 = 90.0;
const TOO_WARM_GPU: f64 = 85.0;
const TOO_FULL_RAM: f64 = 90.0;
const TOO_FULL_VRAM: f64 = 95.0;

/// What is asked of WMI.  Every word of it is written here: nothing of a
/// call's goes into a question.
const LIBRE: &str = "root\\LibreHardwareMonitor";
const OPEN: &str = "root\\OpenHardwareMonitor";
const TEMPERATURE_SENSORS: &str = "SELECT Name, SensorType, Value FROM Sensor WHERE SensorType = 'Temperature'";
const ALL_SENSORS: &str = "SELECT Name, SensorType, Value, Identifier FROM Sensor";
const ACPI: &str = "root\\WMI";
const ACPI_ZONES: &str = "SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature";
const CIMV2: &str = "root\\cimv2";
const THERMAL_ZONES: &str = "SELECT HighPrecisionTemperature FROM Win32_PerfFormattedData_Counters_ThermalZoneInformation";

/// What is kept of the passes, by the names the page knows.  (Three are
/// kept empty, as Python keeps them: nothing writes them.)
const KEPT_OF: [&str; 15] = ["cpu_temp", "cpu_load", "cpu_clock", "gpu_temp", "gpu_load", "gpu_clock", "gpu_vram_pct", "ram_pct", "ram_standby_mb",
                             "disk_read_bps", "disk_write_bps", "net_ping_ms", "dpc_latency_us", "dpc_pct", "isr_pct"];

/// This reading is not the shell's to take: Python would start a program
/// for it, or read a value in a way of its own.  The backend is asked.
struct NotMine;

/// A part raised in the backend: the pass is not kept.
struct Raised;

/// What the backend says of a part of a pass.
enum Part {
    /// Its reading.
    Says(Json),
    /// There is no backend to ask, or it did not answer.
    HasNone,
    /// Its function raised: Python's loop throws such a pass away.
    Raised,
}

/// The processor's times, summed over all processors (native/src/cpu.rs).
#[derive(Debug, Clone, Copy, PartialEq)]
struct Times {
    total: i128,
    idle: i128,
    dpc: i128,
    interrupt: i128,
    processors: i128,
}

struct Cpu {
    temp: Json,
    source: String,
    load: Json,
    clock: Json,
}

struct Gpu {
    temp: Json,
    load: Json,
    clock: Json,
    vram: Json,
}

struct Dpc {
    dpc: Json,
    isr: Json,
    rate: Json,
    ok: bool,
    perfmon_ok: bool,
}

/// How many passes were taken, and what became of them.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Passes {
    /// Passes whose readings were kept.
    pub kept: u64,
    /// Passes that were thrown away because a part raised in the backend.
    pub dropped: u64,
    /// How often the backend was asked for a part.
    pub asked_the_backend: u64,
}

struct State {
    /// As the Python module keeps it: true from a start to a stop, also
    /// while the loop sleeps.
    running: bool,
    /// Which start (or wake) the loop that runs now belongs to; None while
    /// it sleeps.
    thread: Option<u64>,
    /// When somebody last looked (a snapshot, a start), on the clock that
    /// does not jump.
    last_wanted: f64,
    starts: u64,
    latest: Option<Json>,
    alerts: Vec<String>,
    /// Where the ping goes: whatever the start call named.
    target: Json,
    kept: Vec<VecDeque<Json>>,
    passes: Passes,
    backend: Option<AskBackend>,
    say: Option<Say>,
    /// The shell quits: nothing is started any more.
    closed: bool,
}

/// What a pass compares its readings with.  Kept from pass to pass, and
/// through a stop and a start, as the Python module keeps it.
#[derive(Default)]
struct Before {
    /// The processor's times at the reading before, for its load: total
    /// and idle.
    load: Option<(i128, i128)>,
    /// The same for its time in DPCs and interrupts, and when that was (on
    /// the clock that does not jump).
    shares: Option<(Times, f64)>,
    /// Which source of the processor's temperature was said in the log last.
    said_source: Option<String>,
    /// The places in WMI, by name: until when one is quiet, and when it last
    /// gave a reading.
    places: HashMap<&'static str, (f64, Option<f64>)>,
}

impl Before {
    /// Is the place quiet at `now`?
    fn quiet(&self, place: &str, now: f64) -> bool {
        self.places.get(place).is_some_and(|(until, _)| now < *until)
    }

    /// What the place gave, asked at `now`: a reading, or nothing.
    fn noted(&mut self, place: &'static str, now: f64, a_reading: bool) {
        let (until, had) = self.places.entry(place).or_insert((0.0, None));
        if a_reading {
            *had = Some(now);
        } else if had.is_none_or(|had| now - had > QUIET_S) {
            *until = now + QUIET_S;
        }
    }
}

/// The monitor.
pub struct Monitor {
    door: Arc<dyn Readings>,
    /// Does a start run a thread?  (Not where a test takes every pass by
    /// hand: `step`.)
    threads: bool,
    state: Mutex<State>,
    /// Held for a whole pass: one pass at a time.
    before: Mutex<Before>,
    /// Rung when the loop is told to stop.
    told: Condvar,
}

/// What Python's `_int(value)` gives (core/native.py): the whole number,
/// or None where it raises.
fn whole(value: Option<&Json>) -> Option<i128> {
    match value {
        Some(Json::Int(whole)) => Some(*whole),
        Some(Json::Float(number)) if number.is_finite() => Some(number.trunc() as i128),
        _ => None,
    }
}

/// `str(value or "")`, for a name that is looked into.  Not this program's
/// to say for a list or an object: Python writes those in a way of its own.
fn text_of(value: Option<&Json>) -> Result<String, NotMine> {
    match value {
        Some(Json::Array(_) | Json::Object(_)) if value.is_some_and(Json::truthy) => Err(NotMine),
        Some(value) => Ok(value.py_str_or_empty()),
        None => Ok(String::new()),
    }
}

/// Is this a processor's temperature?  Between 10 and 120 degrees, and not
/// the 27.85 (or 28) that a thermal zone without a sensor reports.
fn sane(degrees: f64) -> bool {
    10.0 < degrees && degrees < 120.0 && (degrees - 27.85).abs() >= 0.5 && (degrees - 28.0).abs() >= 0.5
}

/// `round(float(value), 1) if _sane_temp(value) else None`.
fn a_temperature(value: Option<&Json>) -> Result<Option<f64>, NotMine> {
    let degrees = match value {
        Some(Json::Int(whole)) => *whole as f64,
        Some(Json::Float(number)) => *number,
        Some(Json::Bool(truth)) => f64::from(u8::from(*truth)),
        // (Python reads a number out of a text, in more ways than one)
        Some(Json::Str(_)) => return Err(NotMine),
        _ => return Ok(None),
    };
    Ok(sane(degrees).then(|| py_round(degrees, 1)))
}

/// The value of the first sensor whose name has one of `wanted` in it.
fn first_named<'a>(rows: &'a [Json], wanted: &[&str]) -> Result<Option<&'a Json>, NotMine> {
    for row in rows {
        let name = text_of(row.get("Name"))?.to_lowercase();
        if wanted.iter().any(|word| name.contains(word)) {
            return Ok(row.get("Value"));
        }
    }
    Ok(None)
}

/// A thermal zone's reading (tenths of a kelvin) in degrees, or None where
/// it is missing or no real temperature.
fn tenths_of_kelvin(value: Option<&Json>) -> Option<f64> {
    let tenths = match value {
        Some(Json::Int(whole)) if *whole > 0 => *whole as f64,
        Some(Json::Float(number)) if *number > 0.0 => *number,
        _ => return None,
    };
    let degrees = tenths / 10.0 - 273.15;
    sane(degrees).then(|| py_round(degrees, 1))
}

/// How full the card's memory is, from the bytes its reader says: whole
/// megabytes first, as nvidia-smi prints them, then a percentage with one
/// place.
fn vram_of(used: Option<i128>, total: Option<i128>) -> Json {
    let (Some(used), Some(total)) = (used, total) else { return Json::Null };
    let megabytes = |bytes: i128| py_round(bytes as f64 / 1_048_576.0, 0) as i128;
    let (used, total) = (megabytes(used), megabytes(total));
    if total > 0 {
        Json::Float(py_round(used as f64 / total as f64 * 100.0, 1))
    } else {
        Json::Int(0)
    }
}

/// `value and value > limit`.
fn above(value: &Json, limit: f64) -> bool {
    match value {
        Json::Int(whole) => *whole as f64 > limit,
        Json::Float(number) => *number > limit,
        _ => false,
    }
}

fn or_nothing(value: Option<&Json>) -> Json {
    value.cloned().unwrap_or(Json::Null)
}

impl Monitor {
    pub fn new(door: Arc<dyn Readings>, threads: bool) -> Arc<Monitor> {
        let state = State { running: false, thread: None, last_wanted: 0.0, starts: 0, latest: None, alerts: Vec::new(), target: THE_USUAL_TARGET.into(),
                            kept: KEPT_OF.iter().map(|_| VecDeque::with_capacity(KEPT)).collect(), passes: Passes::default(), backend: None, say: None,
                            closed: false };
        Arc::new(Monitor { door, threads, state: Mutex::new(state), before: Mutex::new(Before::default()), told: Condvar::new() })
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// How the backend is asked for a part of a pass.
    pub fn asks_the_backend_with(&self, ask: AskBackend) {
        self.state().backend = Some(ask);
    }

    /// Where to say what the monitor does.
    pub fn says_with(&self, say: Say) {
        self.state().say = Some(say);
    }

    fn say(&self, line: &str) {
        let say = self.state().say.clone();
        if let Some(say) = say {
            say(line);
        }
    }

    /// How many passes were taken so far, and what became of them.
    pub fn passes(&self) -> Passes {
        self.state().passes
    }

    /// The shell's answer to one of the monitor's calls, or None: the call
    /// is the backend's.  `path` is the call's path as the backend's web
    /// framework reads it.
    pub fn answer(self: &Arc<Self>, card: &Arc<Card>, request: &Request, path: &str) -> Option<Response> {
        let answer = match (request.method.as_str(), path) {
            ("POST", "/api/monitor/start") => match said_by(&request.body) {
                // (`data.get("ping_target", "1.1.1.1")`; a text in which the
                // reader of JSON had to put the replacement mark for half a
                // character pair is not the text Python has)
                Said::Says(said) if !said.get("ping_target").and_then(Json::as_str).is_some_and(|target| target.contains('\u{FFFD}')) => {
                    self.start(card, said.get("ping_target").cloned().unwrap_or_else(|| THE_USUAL_TARGET.into()))
                }
                // (Python says so in words of its own, and starts nothing)
                Said::PythonStops => return None,
                // Not passed on: Python might read it and start its own
                // monitor, beside this one.
                Said::Says(_) | Said::CannotTell => {
                    let refusal = [("ok", false.into()), ("err", "The body of this call is not plain JSON in UTF-8, or is nested too deeply to read, \
                                                                 or names a target with half a character pair in it.".into())];
                    return Some(Response::json(400, &Json::object(refusal)));
                }
            },
            // (the route reads nothing of a stop's body)
            ("POST", "/api/monitor/stop") => self.stop(),
            ("GET", "/api/monitor/snapshot") => self.look(card),
            _ => return None,
        };
        Some(Response::json(200, &answer))
    }

    /// Start the monitor, with the ping going to `target`.
    /// (A start while it sleeps wakes it, and answers as a start while it
    /// runs.)
    pub fn start(self: &Arc<Self>, card: &Arc<Card>, target: Json) -> Json {
        let mut st = self.state();
        st.last_wanted = self.door.steady();
        if st.running {
            if let Some(this) = self.claim_a_wake(&mut st) {
                drop(st);
                self.wake(card, this);
            }
            return Json::object([("ok", true.into()), ("msg", "Already running".into())]);
        }
        st.running = true;
        st.target = target;
        st.starts += 1;
        let this = st.starts;
        st.thread = Some(this);
        drop(st);
        // (a loop started after the quit ends before its first pass)
        if self.threads {
            let (monitor, card) = (self.clone(), card.clone());
            if let Err(why) = std::thread::Builder::new().name("hw-monitor".to_string()).spawn(move || monitor.run(&card, this)) {
                // (no thread could be made: nothing reads, and that is said as "not running")
                let mut st = self.state();
                if st.thread == Some(this) {
                    st.thread = None;
                    st.running = false;
                }
                drop(st);
                self.say(&format!("the hardware monitor could not be started: no thread could be made for it ({why})"));
                return Json::object([("ok", true.into())]);
            }
        }
        self.say("the hardware monitor was started");
        Json::object([("ok", true.into())])
    }

    /// Stop it.  The loop ends by itself, after the pass it is in.
    pub fn stop(&self) -> Json {
        let was = std::mem::replace(&mut self.state().running, false);
        self.told.notify_all();
        if was {
            self.say("the hardware monitor was stopped");
        }
        Json::object([("ok", true.into())])
    }

    /// The shell quits: the loop ends, and none is started any more.
    pub fn close(&self) {
        {
            let mut st = self.state();
            st.closed = true;
            st.running = false;
        }
        self.told.notify_all();
    }

    /// The snapshot, asked for by the page: asking is looking, and a monitor
    /// that is on and asleep is woken (and forgets first).
    pub fn look(self: &Arc<Self>, card: &Arc<Card>) -> Json {
        let wake = {
            let mut st = self.state();
            st.last_wanted = self.door.steady();
            if st.running { self.claim_a_wake(&mut st) } else { None }
        };
        if let Some(this) = wake {
            self.wake(card, this);
        }
        self.snapshot()
    }

    /// A monitor that is on and whose loop sleeps: the wake is this caller's
    /// (decided with the state held, so that two who look at once wake one
    /// loop), and what was read before the sleep is forgotten at once, so
    /// that nobody who looks in the meantime sees it: the tiles and the
    /// minute of history are as in the first second after a start.
    fn claim_a_wake(&self, st: &mut State) -> Option<u64> {
        if st.thread.is_some() || st.closed {
            return None;
        }
        st.starts += 1;
        st.thread = Some(st.starts);
        st.latest = None;
        st.alerts.clear();
        for kept in &mut st.kept {
            kept.clear();
        }
        st.thread
    }

    /// Start the loop of `this`, whose first pass compares with nothing
    /// older than the sleep.  (No pass of it has begun yet: its thread is
    /// made below.)
    fn wake(self: &Arc<Self>, card: &Arc<Card>, this: u64) {
        {
            let mut before = self.before.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            (before.load, before.shares) = (None, None);
        }
        if self.threads {
            let (monitor, card) = (self.clone(), card.clone());
            if let Err(why) = std::thread::Builder::new().name("hw-monitor".to_string()).spawn(move || monitor.run(&card, this)) {
                let mut st = self.state();
                if st.thread == Some(this) {
                    st.thread = None;
                }
                drop(st);
                self.say(&format!("the hardware monitor could not be woken: no thread could be made for it ({why})"));
                return;
            }
        }
        self.say("the hardware monitor wakes: somebody looks again");
    }

    /// After a pass (and the rest of its second): nobody has looked for
    /// longer than `IDLE_STOP_S`, and the loop of `this` sleeps?
    fn falls_asleep(&self, this: u64) -> bool {
        let asleep = {
            let mut st = self.state();
            let asleep = st.thread == Some(this) && self.door.steady() - st.last_wanted > IDLE_STOP_S;
            if asleep {
                st.thread = None;
            }
            asleep
        };
        if asleep {
            self.say(&format!("the hardware monitor sleeps: nobody has looked for {IDLE_STOP_S:.0} s"));
        }
        asleep
    }

    /// The latest readings, and the minute before them.
    pub fn snapshot(&self) -> Json {
        let st = self.state();
        let history = KEPT_OF.iter().zip(&st.kept).map(|(name, kept)| (name.to_string(), Json::Array(kept.iter().cloned().collect()))).collect();
        Json::object([
            ("running", st.running.into()),
            ("latest", st.latest.clone().unwrap_or_else(|| Json::Object(Vec::new()))),
            ("alerts", Json::Array(st.alerts.iter().map(|alert| alert.as_str().into()).collect())),
            ("history", Json::Object(history)),
        ])
    }

    /// What the loop of the start that counts does in one round: a pass,
    /// if it still runs, and whether anybody still looks.  Did it take a
    /// pass?  (For a test that takes every round by hand.)
    pub fn round(&self, card: &Card) -> bool {
        let this = {
            let st = self.state();
            match st.thread {
                Some(this) if st.running && !st.closed => this,
                _ => return false,
            }
        };
        if !self.round_of(card, this) {
            return false;
        }
        self.falls_asleep(this);
        true
    }

    /// A pass, if the loop of start `this` still runs.
    fn round_of(&self, card: &Card, this: u64) -> bool {
        {
            let st = self.state();
            if !st.running || st.thread != Some(this) || st.closed {
                return false;
            }
        }
        self.step(card);
        true
    }

    /// The loop: a pass, then the rest of the second, for as long as this
    /// start is the one that counts.
    fn run(&self, card: &Card, this: u64) {
        loop {
            let began = self.door.steady();
            if !self.round_of(card, this) {
                break;
            }
            let left = EVERY_S - (self.door.steady() - began);
            if left > 0.0 {
                let st = self.state();
                let _ = self.told.wait_timeout_while(st, Duration::from_secs_f64(left), |st| st.running && st.thread == Some(this) && !st.closed);
            }
            if self.falls_asleep(this) {
                break;
            }
        }
    }

    /// One pass: every reading once, in Python's order.  (The time is taken
    /// first, as Python takes it.)
    pub fn step(&self, card: &Card) {
        let mut before = self.before.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let ts = self.door.now();
        let read = (|| {
            let cpu = self.cpu(&mut before)?;
            let gpu = self.gpu(card, &mut before)?;
            let (used, standby) = self.ram()?;
            let dpc = self.dpc(&mut before)?;
            let target = self.state().target.clone();
            let ping = self.ping(&target)?;
            Some((cpu, gpu, used, standby, dpc, ping))
        })();
        let Some((cpu, gpu, used, standby, dpc, ping)) = read else {
            // (a part raised in the backend, as it would have in its own
            // loop, which keeps nothing of such a pass)
            self.state().passes.dropped += 1;
            return;
        };
        let mut st = self.state();
        let mut keep = |name: &str, value: &Json, always: bool| {
            if always || *value != Json::Null {
                if let Some(kept) = KEPT_OF.iter().position(|known| *known == name).and_then(|at| st.kept.get_mut(at)) {
                    if kept.len() == KEPT {
                        kept.pop_front();
                    }
                    kept.push_back(value.clone());
                }
            }
        };
        keep("cpu_temp", &cpu.temp, false);
        keep("cpu_load", &cpu.load, false);
        keep("cpu_clock", &cpu.clock, false);
        keep("gpu_temp", &gpu.temp, false);
        keep("gpu_load", &gpu.load, false);
        keep("gpu_clock", &gpu.clock, false);
        keep("gpu_vram_pct", &gpu.vram, false);
        keep("ram_pct", &used, true);
        keep("ram_standby_mb", &standby, true);
        keep("dpc_pct", &dpc.dpc, false);
        keep("isr_pct", &dpc.isr, false);
        keep("net_ping_ms", &ping, false);
        let mut alerts = Vec::new();
        if above(&cpu.temp, TOO_WARM_CPU) {
            alerts.push(format!("CPU temp: {}\u{b0}C", cpu.temp.py_str()));
        }
        if above(&gpu.temp, TOO_WARM_GPU) {
            alerts.push(format!("GPU temp: {}\u{b0}C", gpu.temp.py_str()));
        }
        if above(&used, TOO_FULL_RAM) {
            alerts.push(format!("RAM: {}%", used.py_str()));
        }
        if above(&gpu.vram, TOO_FULL_VRAM) {
            alerts.push(format!("VRAM: {}%", gpu.vram.py_str()));
        }
        st.latest = Some(Json::object([
            ("ts", ts.into()),
            ("cpu_temp", cpu.temp),
            ("cpu_temp_source", cpu.source.into()),
            ("cpu_load", cpu.load),
            ("cpu_clock", cpu.clock),
            ("gpu_temp", gpu.temp),
            ("gpu_load", gpu.load),
            ("gpu_clock", gpu.clock),
            ("gpu_vram_pct", gpu.vram),
            ("ram_pct", used),
            ("ram_standby_mb", standby),
            ("dpc_pct", dpc.dpc),
            ("isr_pct", dpc.isr),
            ("dpc_rate", dpc.rate),
            ("dpc_ok", dpc.ok.into()),
            ("dpc_perfmon_ok", dpc.perfmon_ok.into()),
            // (kept for a page that still asks for it: real microseconds
            // would need a trace the app does not run)
            ("dpc_latency_us", Json::Null),
            ("net_ping_ms", ping),
        ]));
        st.alerts = alerts;
        st.passes.kept += 1;
    }

    /// What the backend says of the part named `name`.
    fn the_backends(&self, name: &str, target: Option<&str>) -> Part {
        let ask = {
            let mut st = self.state();
            if st.backend.is_some() {
                st.passes.asked_the_backend += 1;
            }
            st.backend.clone()
        };
        let Some(ask) = ask else { return Part::HasNone };
        let call = match target {
            Some(target) => format!("{THE_BACKENDS_PART}?name={name}&target={}", quoted(target)),
            None => format!("{THE_BACKENDS_PART}?name={name}"),
        };
        match ask(&call) {
            Some(said) if said.get("ok").is_some_and(Json::truthy) => match said.get("reading") {
                Some(reading @ Json::Object(_)) => Part::Says(reading.clone()),
                _ => Part::HasNone,
            },
            Some(said) if said.get("raised").is_some_and(Json::truthy) => Part::Raised,
            _ => Part::HasNone,
        }
    }

    // ─── The processor ───────────────────────────────────────────────

    /// The processor's times now, as the app takes the library's answer
    /// (core/native.py, cpu_times).
    fn times(&self) -> Option<Times> {
        let said = self.door.ask("cpu_times")?;
        let times = Times { total: whole(said.get("total"))?, idle: whole(said.get("idle"))?, dpc: whole(said.get("dpc"))?,
                            interrupt: whole(said.get("interrupt"))?, processors: whole(said.get("processors"))? };
        (times.total > 0 && times.processors > 0).then_some(times)
    }

    /// How busy the processors were since the reading before, in percent.
    /// None the first time (there is nothing to compare with), and where
    /// the times cannot be read or did not move.
    fn load(&self, before: &mut Before) -> Option<f64> {
        let now = self.times()?;
        let was = before.load.replace((now.total, now.idle))?;
        let (total, idle) = (now.total - was.0, now.idle - was.1);
        if total <= 0 {
            return None;
        }
        Some(py_round((total - idle) as f64 / total as f64 * 100.0, 1))
    }

    /// The first processor's clock in MHz (core/native.py, cpu_clock_mhz).
    fn clock(&self) -> Option<i128> {
        whole(self.door.ask("cpu_clock")?.get("mhz")).filter(|mhz| *mhz > 0)
    }

    /// The rows WMI gives for a question, as the app takes them
    /// (core/native.py, wmi): a list of objects, or it is not an answer.
    fn rows(&self, namespace: &str, query: &str) -> Result<Vec<Json>, NotMine> {
        match self.door.ask_with("wmi", &[("namespace", namespace.into()), ("query", query.into())]) {
            Some(Json::Array(rows)) if rows.iter().all(Json::is_object) => Ok(rows),
            _ => Err(NotMine),
        }
    }

    /// The temperature one place gives, read as Python reads it.
    fn temp_at(&self, place: &str) -> Result<Option<f64>, NotMine> {
        match place {
            CPU_LHM => {
                let rows = self.rows(LIBRE, TEMPERATURE_SENSORS)?;
                a_temperature(first_named(&rows, &["package", "ccd1", "tctl"])?)
            }
            CPU_OHM => {
                let rows = self.rows(OPEN, TEMPERATURE_SENSORS)?;
                a_temperature(first_named(&rows, &["package", "ccd1"])?)
            }
            // (the first zone)
            CPU_ACPI => Ok(self.rows(ACPI, ACPI_ZONES)?.first().and_then(|zone| tenths_of_kelvin(zone.get("CurrentTemperature")))),
            // (the warmest zone: Python's `max`, which keeps the first of equals)
            _ => {
                let rows = self.rows(CIMV2, THERMAL_ZONES)?;
                let mut warmest: Option<&Json> = None;
                for reading in rows.iter().filter_map(|zone| zone.get("HighPrecisionTemperature")).filter(|reading| matches!(reading, Json::Int(_) | Json::Float(_))) {
                    let number = |value: &Json| match value {
                        Json::Int(whole) => *whole as f64,
                        Json::Float(number) => *number,
                        _ => 0.0,
                    };
                    if warmest.map_or(true, |so_far| number(reading) > number(so_far)) {
                        warmest = Some(reading);
                    }
                }
                Ok(warmest.and_then(|reading| tenths_of_kelvin(Some(reading))))
            }
        }
    }

    /// The processor's temperature and where it was read, from the first of
    /// Python's four places that has a real one, each asked as the place
    /// allows.  Where a place's answer is one only Python reads, the backend
    /// reads that place (as it is: the place is the shell's to keep track of).
    fn temp(&self, before: &mut Before) -> Result<Option<(f64, &'static str)>, Raised> {
        let places = [(CPU_LHM, "temp_lhm", "LibreHardwareMonitor"), (CPU_OHM, "temp_ohm", "OpenHardwareMonitor"),
                      (CPU_ACPI, "temp_acpi", "MSAcpi_ThermalZone"), (CPU_THERMAL, "temp_thermal", "PerfFormatted ThermalZone")];
        for (place, part, source) in places {
            let now = self.door.steady();
            if before.quiet(place, now) {
                continue;
            }
            let degrees = match self.temp_at(place) {
                Ok(degrees) => degrees,
                Err(NotMine) => match self.the_backends(part, None) {
                    Part::Raised => return Err(Raised),
                    Part::Says(said) => match said.get("degrees") {
                        Some(Json::Float(degrees)) => Some(*degrees),
                        Some(Json::Int(degrees)) => Some(*degrees as f64),
                        _ => None,
                    },
                    Part::HasNone => None,
                },
            };
            before.noted(place, now, degrees.is_some());
            if let Some(degrees) = degrees {
                return Ok(Some((degrees, source)));
            }
        }
        Ok(None)
    }

    fn cpu(&self, before: &mut Before) -> Option<Cpu> {
        let mut load = self.load(before);
        if load.is_none() {
            // Nothing to compare with yet (or the times did not move):
            // read again a moment later.
            self.door.wait(FIRST_WAIT_S);
            load = self.load(before);
        }
        let clock = self.clock();
        let mut cpu = Cpu { temp: Json::Null, source: String::new(), load: load.map_or(Json::Null, Json::Float), clock: clock.map_or(Json::Null, Json::Int) };
        // Where Python would start PowerShell (the times or the clock gave
        // nothing): the backend's reading of the load and the clock, for
        // what is missing here.
        if load.is_none() || clock.is_none() {
            match self.the_backends("cpu", None) {
                Part::Raised => return None,
                Part::HasNone => {}
                Part::Says(said) => {
                    if load.is_none() {
                        cpu.load = or_nothing(said.get("load"));
                    }
                    if clock.is_none() {
                        cpu.clock = or_nothing(said.get("clock"));
                    }
                }
            }
        }
        match self.temp(before) {
            Err(Raised) => return None,
            Ok(Some((degrees, source))) => (cpu.temp, cpu.source) = (Json::Float(degrees), source.to_string()),
            Ok(None) => {}
        }
        // Said once, and again when it changes: which source pays off on
        // this PC, or that none does.
        let source = if cpu.temp == Json::Null { "none".to_string() } else { cpu.source.clone() };
        if before.said_source.as_deref() != Some(source.as_str()) && !source.is_empty() {
            if cpu.temp == Json::Null {
                self.say("the hardware monitor has no temperature of the processor: neither LibreHardwareMonitor nor another source in WMI gave a real one");
            } else {
                self.say(&format!("the hardware monitor reads the processor's temperature from {source} ({}\u{b0}C)", cpu.temp.py_str()));
            }
            before.said_source = Some(source);
        }
        Some(cpu)
    }

    // ─── The graphics card ───────────────────────────────────────────

    /// The card's temperature, load and clock from LibreHardwareMonitor,
    /// for a card (or a part of a reading) that NVIDIA's library does not
    /// give: each the sensor that Python picks.
    fn gpu_from_lhm(&self) -> Result<(Json, Json, Json), NotMine> {
        let rows = self.rows(LIBRE, ALL_SENSORS)?;
        let mut of_a_card = Vec::new();
        for row in &rows {
            if text_of(row.get("Identifier"))?.to_lowercase().contains("gpu") {
                of_a_card.push(row);
            }
        }
        let mut read = (Json::Null, Json::Null, Json::Null);
        if of_a_card.is_empty() {
            return Ok(read);
        }
        // The first sensor of a kind whose name has one of `wanted` in it,
        // the earlier word first; else the first sensor of that kind.
        fn pick<'a>(of_a_card: &[&'a Json], kind: &str, wanted: &[&str]) -> Result<Option<&'a Json>, NotMine> {
            let of_the_kind: Vec<&'a Json> = of_a_card
                .iter()
                .copied()
                .filter(|row| row.get("SensorType").and_then(Json::as_str) == Some(kind) && !matches!(row.get("Value"), None | Some(Json::Null)))
                .collect();
            for word in wanted {
                for row in &of_the_kind {
                    let name = match row.get("Name") {
                        Some(Json::Str(name)) => name.to_lowercase(),
                        // (a name that is something and no text: Python raises here)
                        Some(other) if other.truthy() => return Err(NotMine),
                        _ => String::new(),
                    };
                    if name.contains(word) {
                        return Ok(row.get("Value"));
                    }
                }
            }
            Ok(of_the_kind.first().and_then(|row| row.get("Value")))
        }
        let temp = pick(&of_a_card, "Temperature", &["gpu core", "core", "hot spot", "gpu"])?;
        let load = pick(&of_a_card, "Load", &["gpu core", "gpu", "core", "d3d"])?;
        let clock = pick(&of_a_card, "Clock", &["gpu core", "core", "gpu"])?;
        // `float(value)`: None where Python stops with what it has so far.
        let number = |value: &Json| -> Result<Option<f64>, NotMine> {
            match value {
                Json::Int(whole) => Ok(Some(*whole as f64)),
                Json::Float(number) if number.is_finite() => Ok(Some(*number)),
                Json::Bool(truth) => Ok(Some(f64::from(u8::from(*truth)))),
                Json::Float(_) | Json::Str(_) => Err(NotMine),
                _ => Ok(None),
            }
        };
        if let Some(temp) = temp {
            match number(temp)? {
                Some(degrees) => read.0 = Json::Float(py_round(degrees, 1)),
                None => return Ok(read),
            }
        }
        if let Some(load) = load {
            match number(load)? {
                Some(percent) => read.1 = Json::Float(py_round(percent, 1)),
                None => return Ok(read),
            }
        }
        if let Some(clock) = clock {
            if let Some(mhz) = number(clock)? {
                read.2 = Json::Int(mhz.trunc() as i128);
            }
        }
        Ok(read)
    }

    fn gpu(&self, card: &Card, before: &mut Before) -> Option<Gpu> {
        let mut gpu = match card.from_the_reader() {
            Some(said) => Gpu { temp: said.value("temp_c").map_or(Json::Null, Json::Int), load: said.value("gpu_util_pct").map_or(Json::Null, Json::Int),
                                clock: said.value("core_mhz").map_or(Json::Null, Json::Int), vram: vram_of(said.value("mem_used_bytes"), said.value("mem_total_bytes")) },
            // The reader is away or has no reading: Python would start
            // nvidia-smi.  The backend's reading of the card itself.
            None => match self.the_backends("gpu", None) {
                Part::Raised => return None,
                Part::Says(said) => Gpu { temp: or_nothing(said.get("temp")), load: or_nothing(said.get("load")), clock: or_nothing(said.get("clock")),
                                          vram: or_nothing(said.get("vram_pct")) },
                Part::HasNone => Gpu { temp: Json::Null, load: Json::Null, clock: Json::Null, vram: Json::Null },
            },
        };
        if gpu.temp == Json::Null || gpu.load == Json::Null {
            // LibreHardwareMonitor's sensors, as the place allows; where its
            // answer is one only Python reads, the backend reads the place.
            let now = self.door.steady();
            let (temp, load, clock) = if before.quiet(GPU_LHM, now) {
                (Json::Null, Json::Null, Json::Null)
            } else {
                let read = match self.gpu_from_lhm() {
                    Ok(read) => read,
                    Err(NotMine) => match self.the_backends("gpu_lhm", None) {
                        Part::Raised => return None,
                        Part::Says(said) => (or_nothing(said.get("temp")), or_nothing(said.get("load")), or_nothing(said.get("clock"))),
                        Part::HasNone => (Json::Null, Json::Null, Json::Null),
                    },
                };
                before.noted(GPU_LHM, now, read.0 != Json::Null || read.1 != Json::Null || read.2 != Json::Null);
                read
            };
            for (mine, theirs) in [(&mut gpu.temp, temp), (&mut gpu.load, load), (&mut gpu.clock, clock)] {
                if *mine == Json::Null {
                    *mine = theirs;
                }
            }
        }
        Some(gpu)
    }

    // ─── Memory ──────────────────────────────────────────────────────

    /// How much of the memory is in use (percent) and how large the standby
    /// list is (megabytes).  The list is read, never emptied.
    fn ram(&self) -> Option<(Json, Json)> {
        let said = self.door.ask("memory");
        let count = |name: &str| match said.as_ref().and_then(|said| said.get(name)) {
            Some(Json::Int(whole)) if *whole >= 0 => Some(*whole),
            _ => None,
        };
        // (nought where Windows would not say, as Python has it)
        let used = count("load_pct").map_or(Json::Int(0), |load| Json::Float(load as f64));
        // (a page size Windows does not tell is the usual one, as Python has it)
        let size = count("page_size").filter(|size| *size > 0).unwrap_or(4096);
        let standby = match count("standby_pages") {
            Some(pages) => Json::Int(py_round((pages * size) as f64 / 1_048_576.0, 0) as i128),
            // Windows would not tell the list: Python would start
            // PowerShell for it.  The backend's reading of the memory.
            _ => match self.the_backends("ram", None) {
                Part::Raised => return None,
                Part::Says(said) => said.get("standby_mb").cloned().unwrap_or(Json::Int(0)),
                Part::HasNone => Json::Int(0),
            },
        };
        Some((used, standby))
    }

    // ─── DPCs and interrupts ─────────────────────────────────────────

    /// The share of the processors' time spent in DPCs and in interrupts
    /// since the pass before, in percent with three places.  None where the
    /// times cannot be read or did not move.
    fn shares(&self, before: &mut Before) -> Option<(f64, f64)> {
        let mut now = self.times()?;
        let recent = before.shares.filter(|(was, at)| self.door.steady() - at <= STALE_S && was.processors == now.processors);
        let was = match recent {
            Some((was, _)) => was,
            None => {
                // Nothing recent to compare with: a second reading a moment later.
                let first = now;
                self.door.wait(FIRST_WAIT_S);
                now = self.times()?;
                first
            }
        };
        before.shares = Some((now, self.door.steady()));
        let total = now.total - was.total;
        if total <= 0 {
            return None;
        }
        let share = |now: i128, was: i128| {
            let percent = (now - was) as f64 / total as f64 * 100.0;
            py_round(if percent > 0.0 { percent } else { 0.0 }, 3)
        };
        Some((share(now.dpc, was.dpc), share(now.interrupt, was.interrupt)))
    }

    fn dpc(&self, before: &mut Before) -> Option<Dpc> {
        let (dpc, isr, perfmon_ok) = match self.shares(before) {
            Some((dpc, isr)) => (Json::Float(dpc), Json::Float(isr), true),
            // The times gave nothing: Python would start PowerShell for
            // the two counters.  The backend's reading of them.
            None => match self.the_backends("dpc", None) {
                Part::Raised => return None,
                Part::Says(said) => (or_nothing(said.get("dpc_pct")), or_nothing(said.get("isr_pct")), said.get("perfmon_ok").is_some_and(Json::truthy)),
                Part::HasNone => (Json::Null, Json::Null, false),
            },
        };
        let rate = match self.door.ask("dpc_rate").as_ref().and_then(|said| said.get("per_second")) {
            Some(Json::Int(rate)) => Json::Int(*rate),
            _ => Json::Null,
        };
        let ok = perfmon_ok || rate != Json::Null;
        Some(Dpc { dpc, isr, rate, ok, perfmon_ok })
    }

    // ─── The ping ────────────────────────────────────────────────────

    /// How long one echo request to `target` takes, in milliseconds (at
    /// least 1), or nothing where there was no answer.
    ///
    /// What the shell sends: one echo request with 32 bytes of the
    /// library's own in it, to `target`, and only if `target` is a text
    /// that is an IPv4 address written plainly.  It looks no name up.  For
    /// any other text Python starts ping.exe, which the shell does not: the
    /// backend is asked, with that text.  What is no text gets no ping at
    /// all, from Python or from here.
    fn ping(&self, target: &Json) -> Option<Json> {
        let Json::Str(target) = target else { return Some(Json::Null) };
        if plain_ipv4(target) {
            let said = self.door.ask_with("ping", &[("target", target.as_str().into()), ("timeout_ms", Json::Int(PING_WITHIN_MS))]);
            match said.as_ref().and_then(|said| said.get("ok")) {
                Some(Json::Bool(false)) => return Some(Json::Null),
                Some(Json::Bool(true)) => {
                    if let Some(ms) = whole(said.as_ref().and_then(|said| said.get("ms"))) {
                        return Some(Json::Int(ms.max(1)));
                    }
                }
                _ => {}
            }
        }
        match self.the_backends("ping", Some(target)) {
            Part::Raised => None,
            Part::Says(said) => Some(or_nothing(said.get("ms"))),
            Part::HasNone => Some(Json::Null),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::{Fake, FakeBackend, FakeReaders, FakeReading, Moved};
    use super::*;

    const A_CARD: &str = r#"{"temp_c": 61, "gpu_util_pct": 97, "mem_util_pct": 40, "core_mhz": 2100, "mem_mhz": 9501, "power_mw": 215437, "fan_pct": 55,
                            "mem_used_bytes": 5000000000, "mem_total_bytes": 12000000000}"#;

    struct Rig {
        machine: Arc<Fake>,
        backend: Arc<FakeBackend>,
        readers: Arc<FakeReaders>,
        moved: Arc<Moved>,
    }

    fn wmi(machine: &Fake, namespace: &str, query: &str, rows: &str) {
        machine.answers_with("wmi", &[("namespace", namespace.into()), ("query", query.into())], Some(Json::parse(rows).unwrap()));
    }

    /// A PC where everything answers: eight processors a quarter busy, an
    /// NVIDIA card, LibreHardwareMonitor, a ping that comes back.
    fn a_machine_with(threads: bool) -> Rig {
        let (machine, backend, readers) = (Fake::new(), FakeBackend::new(), FakeReaders::new());
        machine.at(1000.0);
        machine.answers("cpu_times", Some(Json::parse(r#"{"total": 8000000000, "idle": 6000000000, "dpc": 4000000, "interrupt": 2000000, "processors": 8}"#).unwrap()));
        machine.grows("cpu_times", Some(Json::parse(r#"{"total": 80000000, "idle": 60000000, "dpc": 400000, "interrupt": 80000}"#).unwrap()));
        machine.answers("cpu_clock", Some(Json::parse(r#"{"mhz": 3700, "max_mhz": 3700, "processors": 8}"#).unwrap()));
        machine.answers("memory", Some(Json::parse(r#"{"load_pct": 43, "standby_pages": 971766, "page_size": 4096}"#).unwrap()));
        machine.answers("dpc_rate", Some(Json::parse(r#"{"per_second": 245, "processors": 8}"#).unwrap()));
        machine.answers_with("ping", &[("target", "1.1.1.1".into()), ("timeout_ms", Json::Int(500))], Some(Json::parse(r#"{"ok": true, "ms": 11}"#).unwrap()));
        wmi(&machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "Core #1", "Value": 48.0}, {"Name": "CPU Package", "Value": 52.44}]"#);
        wmi(&machine, OPEN, TEMPERATURE_SENSORS, "[]");
        wmi(&machine, ACPI, ACPI_ZONES, "[]");
        wmi(&machine, CIMV2, THERMAL_ZONES, "[]");
        wmi(&machine, LIBRE, ALL_SENSORS, "[]");
        readers.reads(FakeReading::Says(Json::parse(A_CARD).unwrap()));
        let moved = Moved::new(machine.clone(), threads);
        moved.asks_the_backend_with(backend.asker());
        moved.card.reads_with(Arc::new(readers.clone()));
        Rig { machine, backend, readers, moved }
    }

    fn a_machine() -> Rig {
        a_machine_with(false)
    }

    fn j(text: &str) -> Json {
        Json::parse(text).unwrap()
    }

    impl Rig {
        fn call(&self, method: &str, target: &str, body: &str) -> Option<(u16, Json)> {
            self.moved.answer(&Request::new(method, target).with_body(body)).map(|answer| (answer.status, answer.to_json()))
        }

        fn pass(&self, at: f64) -> Json {
            self.machine.at(at);
            self.moved.hw.step(&self.moved.card);
            self.moved.hw.snapshot()["latest"].clone()
        }

        fn part(&self, name: &str, says: &str) {
            self.backend.says_to(&format!("{THE_BACKENDS_PART}?name={name}"), Some(Json::parse(says).unwrap()));
        }
    }

    #[test]
    fn a_pass_reads_everything_once_and_the_page_is_told() {
        let rig = a_machine();
        assert_eq!(rig.call("GET", "/api/monitor/snapshot", "").unwrap().1,
                   j(r#"{"running": false, "latest": {}, "alerts": [], "history": {"cpu_temp": [], "cpu_load": [], "cpu_clock": [], "gpu_temp": [], "gpu_load": [],
                         "gpu_clock": [], "gpu_vram_pct": [], "ram_pct": [], "ram_standby_mb": [], "disk_read_bps": [], "disk_write_bps": [], "net_ping_ms": [],
                         "dpc_latency_us": [], "dpc_pct": [], "isr_pct": []}}"#));
        assert_eq!(rig.call("POST", "/api/monitor/start", "{}").unwrap(), (200, Json::parse(r#"{"ok": true}"#).unwrap()));
        assert_eq!(rig.call("POST", "/api/monitor/start", "{}").unwrap(), (200, Json::parse(r#"{"ok": true, "msg": "Already running"}"#).unwrap()));
        // The first pass has nothing to compare the processor's times
        // with: it waits a quarter of a second for the load, and another
        // for the shares.
        let first = rig.pass(1000.0);
        assert_eq!(rig.machine.waited(), 0.5);
        assert_eq!(first,
                   j(r#"{"ts": 1000.0, "cpu_temp": 52.4, "cpu_temp_source": "LibreHardwareMonitor", "cpu_load": 25.0, "cpu_clock": 3700, "gpu_temp": 61,
                         "gpu_load": 97, "gpu_clock": 2100, "gpu_vram_pct": 41.7, "ram_pct": 43.0, "ram_standby_mb": 3796, "dpc_pct": 0.5, "isr_pct": 0.1,
                         "dpc_rate": 245, "dpc_ok": true, "dpc_perfmon_ok": true, "dpc_latency_us": null, "net_ping_ms": 11}"#));
        // Later passes compare with the pass before, and wait for nothing.
        let second = rig.pass(1001.0);
        assert_eq!((rig.machine.waited(), &second["cpu_load"], &second["dpc_pct"], &second["ts"]), (0.5, &Json::Float(25.0), &Json::Float(0.5), &Json::Float(1001.0)));
        let told = rig.call("GET", "/api/monitor/snapshot", "").unwrap().1;
        assert_eq!((&told["running"], &told["history"]["cpu_load"], &told["history"]["net_ping_ms"], &told["history"]["disk_read_bps"]),
                   (&Json::Bool(true), &j("[25.0, 25.0]"), &j("[11, 11]"), &j("[]")));
        // Nothing was asked of the backend, the card was read by its
        // reader, and the door was asked nothing about the card.
        assert_eq!((rig.backend.asked(), rig.moved.card.from().reader, rig.moved.hw.passes()), (vec![], 2, Passes { kept: 2, dropped: 0, asked_the_backend: 0 }));
        assert!(rig.machine.asked().iter().all(|question| !question.starts_with("gpu_")), "{:?}", rig.machine.asked());
        assert_eq!(rig.call("POST", "/api/monitor/stop", "whatever").unwrap(), (200, Json::parse(r#"{"ok": true}"#).unwrap()));
        assert_eq!(rig.call("GET", "/api/monitor/snapshot", "").unwrap().1["running"], Json::Bool(false));
    }

    #[test]
    fn a_minute_is_kept_and_what_is_too_warm_or_too_full_is_said() {
        let rig = a_machine();
        for round in 0..70 {
            rig.pass(1000.0 + round as f64);
        }
        let told = rig.moved.hw.snapshot();
        for name in ["cpu_temp", "cpu_load", "gpu_temp", "ram_pct", "ram_standby_mb", "dpc_pct", "isr_pct", "net_ping_ms", "gpu_vram_pct", "cpu_clock", "gpu_clock", "gpu_load"] {
            assert_eq!(told["history"][name].as_array().unwrap().len(), KEPT, "{name}");
        }
        assert_eq!(told["alerts"], Json::Array(vec![]));
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "CPU Package", "Value": 91.26}]"#);
        rig.machine.answers("memory", Some(Json::parse(r#"{"load_pct": 93, "standby_pages": null, "page_size": 4096}"#).unwrap()));
        rig.readers.reads(FakeReading::Says(Json::parse(r#"{"temp_c": 86, "mem_used_bytes": 11600000000, "mem_total_bytes": 12000000000}"#).unwrap()));
        rig.pass(1070.0);
        let degrees = '\u{b0}';
        assert_eq!(rig.moved.hw.snapshot()["alerts"], j(&format!(r#"["CPU temp: 91.3{degrees}C", "GPU temp: 86{degrees}C", "RAM: 93.0%", "VRAM: 96.7%"]"#)));
        // At the limit is not above it; and nothing is not above anything.
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "CPU Package", "Value": 90.0}]"#);
        rig.machine.answers("memory", Some(Json::parse(r#"{"load_pct": 90, "standby_pages": 0, "page_size": 4096}"#).unwrap()));
        rig.readers.reads(FakeReading::Says(Json::parse(r#"{"temp_c": 85}"#).unwrap()));
        rig.pass(1071.0);
        assert_eq!(rig.moved.hw.snapshot()["alerts"], Json::Array(vec![]));
    }

    #[test]
    fn the_processors_temperature_is_the_first_real_one_of_four_sources() {
        let rig = a_machine();
        let passes = std::cell::Cell::new(0.0);
        let read = |_: f64| {
            // (a second later each time)
            passes.set(passes.get() + 1.0);
            let latest = rig.pass(1000.0 + passes.get());
            (latest["cpu_temp"].clone(), latest["cpu_temp_source"].as_str().unwrap_or("").to_string())
        };
        assert_eq!(read(1000.0), (Json::Float(52.4), "LibreHardwareMonitor".to_string()));
        // No sensor of that name, a reading that is no temperature, the
        // 27.85 of a zone without a sensor: the next source is asked.
        for not_one in [r#"[{"Name": "Core #1", "Value": 48.0}]"#, r#"[{"Name": "CPU Package", "Value": 0}]"#, r#"[{"Name": "CPU Package", "Value": null}]"#,
                        r#"[{"Name": "CPU Package", "Value": 27.9}]"#, r#"[{"Name": "CPU Package", "Value": 120}]"#, r#"[{"Name": "CPU Package", "Value": true}]"#,
                        r#"[{"Name": "CPU Package"}, {"Name": "CPU Package", "Value": 50}]"#, "[]"] {
            wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, not_one);
            wmi(&rig.machine, OPEN, TEMPERATURE_SENSORS, r#"[{"Name": "Tctl", "Value": 99}, {"Name": "CCD1 (Tdie)", "Value": 47.25}]"#);
            assert_eq!(read(1001.0), (Json::Float(47.2), "OpenHardwareMonitor".to_string()), "{not_one}");
        }
        wmi(&rig.machine, OPEN, TEMPERATURE_SENSORS, "[]");
        wmi(&rig.machine, ACPI, ACPI_ZONES, r#"[{"CurrentTemperature": 3200}, {"CurrentTemperature": 3500}]"#);
        assert_eq!(read(1002.0), (Json::Float(46.9), "MSAcpi_ThermalZone".to_string()), "the first zone, in tenths of a kelvin");
        for not_one in [r#"[{"CurrentTemperature": 3010}]"#, r#"[{"CurrentTemperature": 0}]"#, r#"[{"CurrentTemperature": true}]"#, r#"[{"CurrentTemperature": "3200"}]"#,
                        r#"[{}]"#, "[]"] {
            wmi(&rig.machine, ACPI, ACPI_ZONES, not_one);
            wmi(&rig.machine, CIMV2, THERMAL_ZONES, r#"[{"HighPrecisionTemperature": 3100}, {"HighPrecisionTemperature": 3256.5}, {"HighPrecisionTemperature": "9999"}, {}]"#);
            assert_eq!(read(1003.0), (Json::Float(52.5), "PerfFormatted ThermalZone".to_string()), "{not_one}: the warmest zone");
        }
        wmi(&rig.machine, CIMV2, THERMAL_ZONES, r#"[{"HighPrecisionTemperature": 3011}]"#);
        assert_eq!(read(1004.0), (Json::Null, String::new()), "no source has a real one");
        assert_eq!(rig.backend.asked(), Vec::<String>::new(), "none of that is the backend's");
    }

    #[test]
    fn where_python_would_start_a_program_the_backend_is_asked_for_that_part() {
        // The processor's clock gives nothing: Python starts PowerShell
        // for it.  The backend's reading of the processor is asked, and
        // only the clock is taken from it.
        let rig = a_machine();
        rig.machine.answers("cpu_clock", None);
        rig.part("cpu", r#"{"ok": true, "reading": {"temp": 77.7, "temp_source": "elsewhere", "load": 99.9, "clock": 3601}}"#);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["cpu_clock"], &latest["cpu_load"], &latest["cpu_temp"]), (&Json::Int(3601), &Json::Float(25.0), &Json::Float(52.4)));
        assert_eq!(rig.backend.asked(), ["/api/monitor/part?name=cpu"]);
        // The times give nothing: the load and the shares are the backend's.
        let rig = a_machine();
        rig.machine.answers("cpu_times", None);
        rig.part("cpu", r#"{"ok": true, "reading": {"temp": null, "load": 12.3, "clock": 1}}"#);
        rig.part("dpc", r#"{"ok": true, "reading": {"dpc_pct": 0.25, "isr_pct": 0.125, "dpc_rate": 1, "ok": true, "perfmon_ok": true}}"#);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["cpu_load"], &latest["cpu_clock"], &latest["dpc_pct"], &latest["isr_pct"], &latest["dpc_rate"], &latest["dpc_perfmon_ok"]),
                   (&Json::Float(12.3), &Json::Int(3700), &Json::Float(0.25), &Json::Float(0.125), &Json::Int(245), &Json::Bool(true)));
        assert_eq!(rig.backend.asked(), ["/api/monitor/part?name=cpu", "/api/monitor/part?name=dpc"]);
        // The backend has no counters either: the rate alone says "ok".
        rig.part("dpc", r#"{"ok": true, "reading": {"dpc_pct": null, "isr_pct": null, "dpc_rate": 1, "ok": true, "perfmon_ok": false}}"#);
        let latest = rig.pass(1001.0);
        assert_eq!((&latest["dpc_pct"], &latest["dpc_ok"], &latest["dpc_perfmon_ok"]), (&Json::Null, &Json::Bool(true), &Json::Bool(false)));
        rig.machine.answers("dpc_rate", None);
        let latest = rig.pass(1002.0);
        assert_eq!((&latest["dpc_rate"], &latest["dpc_ok"]), (&Json::Null, &Json::Bool(false)));
        // Windows does not tell the standby list: the backend's reading of the memory.
        let rig = a_machine();
        rig.machine.answers("memory", Some(Json::parse(r#"{"load_pct": 43, "standby_pages": null, "page_size": 4096}"#).unwrap()));
        rig.part("ram", r#"{"ok": true, "reading": {"used_pct": 99.0, "standby_mb": 1234}}"#);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["ram_pct"], &latest["ram_standby_mb"]), (&Json::Float(43.0), &Json::Int(1234)));
        rig.machine.answers("memory", None);
        let latest = rig.pass(1001.0);
        assert_eq!((&latest["ram_pct"], &latest["ram_standby_mb"]), (&Json::Int(0), &Json::Int(1234)));
        assert_eq!(rig.backend.asked(), ["/api/monitor/part?name=ram", "/api/monitor/part?name=ram"]);
        // A sensor's value that is a text: Python reads a number out of it.
        let rig = a_machine();
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "CPU Package", "Value": "52.4"}]"#);
        rig.part("temp_lhm", r#"{"ok": true, "reading": {"degrees": 52.4}}"#);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["cpu_temp"], latest["cpu_temp_source"].as_str(), &latest["cpu_load"], &latest["cpu_clock"]),
                   (&Json::Float(52.4), Some("LibreHardwareMonitor"), &Json::Float(25.0), &Json::Int(3700)));
        // No backend to ask: that reading is nothing, and the pass is kept.
        let rig = a_machine();
        rig.machine.answers("cpu_clock", None);
        rig.backend.says(None);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["cpu_clock"], &latest["cpu_load"], rig.moved.hw.passes().kept), (&Json::Null, &Json::Float(25.0), 1));
    }

    #[test]
    fn the_card_is_the_readers_and_the_backends_where_the_reader_has_none() {
        let rig = a_machine();
        // What the card does not say is nothing, and LibreHardwareMonitor
        // is asked for it.
        rig.readers.reads(FakeReading::Says(Json::parse(r#"{"temp_c": 61, "core_mhz": 2100}"#).unwrap()));
        wmi(&rig.machine, LIBRE, ALL_SENSORS, r#"[{"Name": "GPU Core", "SensorType": "Load", "Value": 33.26, "Identifier": "/gpu-nvidia/0/load/0"},
                                                  {"Name": "GPU Core", "SensorType": "Temperature", "Value": 99, "Identifier": "/gpu-nvidia/0/temperature/0"},
                                                  {"Name": "CPU Total", "SensorType": "Load", "Value": 5, "Identifier": "/intelcpu/0/load/0"}]"#);
        let latest = rig.pass(1000.0);
        assert_eq!((&latest["gpu_temp"], &latest["gpu_load"], &latest["gpu_clock"], &latest["gpu_vram_pct"]), (&Json::Int(61), &Json::Float(33.3), &Json::Int(2100), &Json::Null));
        assert_eq!(rig.backend.asked(), Vec::<String>::new());
        // The reader has no reading (no NVIDIA card, or one that answers
        // only to nvidia-smi): the backend's reading of the card, whole.
        rig.readers.reads(FakeReading::HasNone);
        rig.part("gpu", r#"{"ok": true, "reading": {"temp": 70, "load": 88, "clock": 1800, "vram_pct": 12.5}}"#);
        let latest = rig.pass(1001.0);
        assert_eq!((&latest["gpu_temp"], &latest["gpu_load"], &latest["gpu_clock"], &latest["gpu_vram_pct"]), (&Json::Int(70), &Json::Int(88), &Json::Int(1800), &Json::Float(12.5)));
        assert_eq!(rig.backend.asked(), ["/api/monitor/part?name=gpu"]);
        // No backend either: LibreHardwareMonitor's, as after an nvidia-smi that said nothing.
        rig.backend.says_to("/api/monitor/part?name=gpu", None);
        let latest = rig.pass(1002.0);
        assert_eq!((&latest["gpu_temp"], &latest["gpu_load"], &latest["gpu_clock"], &latest["gpu_vram_pct"]), (&Json::Float(99.0), &Json::Float(33.3), &Json::Null, &Json::Null));
        // The door was never asked about the card.
        assert!(rig.machine.asked().iter().all(|question| !question.starts_with("gpu_")));
        assert_eq!(vram_of(Some(5_000_000_000), Some(12_000_000_000)), Json::Float(41.7));
        assert_eq!((vram_of(Some(1), Some(0)), vram_of(None, Some(1)), vram_of(Some(1), None)), (Json::Int(0), Json::Null, Json::Null));
    }

    impl Rig {
        /// A round of the loop at `at`: did it take a pass?
        fn round(&self, at: f64) -> bool {
            self.machine.at(at);
            self.moved.hw.round(&self.moved.card)
        }

        fn look(&self) -> Json {
            self.call("GET", "/api/monitor/snapshot", "").unwrap().1
        }

        fn asleep(&self) -> bool {
            self.moved.hw.state().thread.is_none()
        }

        /// How often each place of the processor's temperature was asked so far.
        fn asked_of_wmi(&self, query: &str, namespace: &str) -> usize {
            let kept = format!("wmi {}", Json::Object(vec![("namespace".to_string(), namespace.into()), ("query".to_string(), query.into())]));
            self.machine.asked().iter().filter(|asked| **asked == kept).count()
        }
    }

    #[test]
    fn it_sleeps_when_nobody_looks_and_a_look_wakes_it_without_the_old_numbers() {
        let rig = a_machine();
        rig.machine.at(1000.0);
        assert_eq!(rig.call("POST", "/api/monitor/start", "{}").unwrap().1, j(r#"{"ok": true}"#));
        for second in 0..=30 {
            assert!(rig.round(1000.0 + second as f64), "{second}");
            assert!(!rig.asleep(), "{second} seconds after the start: nobody looked for no longer than 30 s");
        }
        // 30.25 seconds after the start nobody has looked for more than 30 s.
        assert!(rig.round(1030.25));
        assert!(rig.asleep());
        assert!(!rig.round(1031.0), "a loop that sleeps takes no pass");
        assert_eq!(rig.moved.hw.passes().kept, 32);
        assert_eq!(rig.moved.hw.snapshot()["running"], Json::Bool(true), "running is what the person set");
        // A look wakes it, and the old numbers are gone.
        rig.machine.at(1040.0);
        let woken = rig.look();
        assert_eq!((&woken["running"], &woken["latest"], &woken["alerts"], &woken["history"]["cpu_load"]), (&Json::Bool(true), &j("{}"), &j("[]"), &j("[]")));
        assert!(!rig.asleep());
        // The first pass after it compares with nothing older than the sleep:
        // it waits for its second reading, as a first pass does.
        let waited = rig.machine.waited();
        assert!(rig.round(1040.0));
        assert_eq!(rig.machine.waited() - waited, 0.5);
        assert_eq!(rig.moved.hw.snapshot()["history"]["cpu_load"], j("[25.0]"));
        // Looked at every three seconds, it does not sleep.
        for second in 1..40 {
            if second % 3 == 0 {
                rig.look();
            }
            rig.round(1040.0 + second as f64);
        }
        assert!(!rig.asleep());
        // At the edge: 30 seconds after a look it is awake, a little later
        // it sleeps (a pass every second, so that none waits within itself).
        rig.machine.at(1110.0);
        rig.look();
        for second in 1111..=1140 {
            rig.round(second as f64);
        }
        assert!(!rig.asleep(), "30 seconds is not more than 30");
        rig.round(1140.5);
        assert!(rig.asleep());
        // The PC's own clock set far ahead, or back, changes nothing of it:
        // the half minute is the monitor's own clock's.
        rig.machine.at(1200.0);
        rig.look();
        for (second, the_pcs_clock) in [(1201.0, 9_000_000_000.0), (1202.0, 10.0), (1203.0, 9_000_000_000.0)] {
            rig.machine.at(second);
            rig.machine.the_pcs_clock_is_set_to(the_pcs_clock);
            assert!(rig.moved.hw.round(&rig.moved.card) && !rig.asleep(), "the PC's clock at {the_pcs_clock}");
        }
    }

    #[test]
    fn a_stopped_monitor_is_never_woken_and_a_start_while_it_sleeps_wakes_it() {
        let rig = a_machine();
        rig.machine.at(1000.0);
        rig.call("POST", "/api/monitor/start", "{}");
        rig.round(1000.0);
        rig.round(1031.0);
        assert!(rig.asleep());
        // Stopped while it sleeps: a look neither wakes it nor forgets.
        rig.call("POST", "/api/monitor/stop", "");
        let stopped = rig.look();
        assert_eq!(stopped["running"], Json::Bool(false));
        assert!(matches!(stopped["latest"]["ts"], Json::Float(_)), "what it read before is kept, as after a stop today");
        assert!(rig.asleep() && !rig.round(1040.0));
        // Started again: an ordinary start, which forgets nothing either.
        assert_eq!(rig.call("POST", "/api/monitor/start", "{}").unwrap().1, j(r#"{"ok": true}"#));
        assert!(rig.round(1041.0));
        assert_eq!(rig.moved.hw.snapshot()["history"]["cpu_load"].as_array().unwrap().len(), 3);
        // A start while it sleeps wakes it, and answers as a start while it runs.
        rig.round(1072.0);
        assert!(rig.asleep());
        assert_eq!(rig.call("POST", "/api/monitor/start", r#"{"ping_target": "9.9.9.9"}"#).unwrap().1, j(r#"{"ok": true, "msg": "Already running"}"#));
        assert!(!rig.asleep());
        assert_eq!(rig.moved.hw.state().target, Json::from(THE_USUAL_TARGET), "the target is the one it was started with");
        assert_eq!(rig.moved.hw.snapshot()["history"]["cpu_load"], j("[]"));
        // Two looks at once wake one loop.
        rig.round(1080.0);
        rig.round(1111.0);
        let starts = rig.moved.hw.state().starts;
        rig.look();
        rig.look();
        assert_eq!(rig.moved.hw.state().starts, starts + 1);
    }

    #[test]
    fn a_place_that_had_nothing_is_asked_about_twice_a_minute_and_one_that_works_every_pass() {
        // No source of the processor's temperature at all.
        let rig = a_machine();
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, "[]");
        for second in 0..=60 {
            rig.pass(1000.0 + second as f64);
        }
        // (asked in the first pass at 1000.25, after its two waits: quiet until
        // 1030.25, so asked again at 1031, and quiet until 1061)
        for (namespace, query) in [(LIBRE, TEMPERATURE_SENSORS), (OPEN, TEMPERATURE_SENSORS), (ACPI, ACPI_ZONES), (CIMV2, THERMAL_ZONES)] {
            assert_eq!(rig.asked_of_wmi(query, namespace), 2, "{namespace}: twice in a minute");
        }
        // A source that works is asked every pass; one miss does not quiet it.
        let rig = a_machine();
        for second in 0..5 {
            rig.pass(1000.0 + second as f64);
        }
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, "[]");
        assert_eq!(rig.pass(1005.0)["cpu_temp"], Json::Null);
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "CPU Package", "Value": 50.0}]"#);
        assert_eq!(rig.pass(1006.0)["cpu_temp"], Json::Float(50.0), "asked again at once: it gave a reading within 30 s");
        assert_eq!(rig.asked_of_wmi(TEMPERATURE_SENSORS, LIBRE), 7);
        // Gone for longer than 30 s since its last reading: quiet.
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, "[]");
        for second in 7..=37 {
            rig.pass(1000.0 + second as f64);
        }
        let asked = rig.asked_of_wmi(TEMPERATURE_SENSORS, LIBRE);
        rig.pass(1038.0);
        rig.pass(1039.0);
        assert_eq!(rig.asked_of_wmi(TEMPERATURE_SENSORS, LIBRE), asked, "quiet now");
        // A source that comes (LibreHardwareMonitor started) is seen within 30 s.
        let rig = a_machine();
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, "[]");
        rig.pass(1000.0);
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, r#"[{"Name": "CPU Package", "Value": 51.0}]"#);
        // (asked at 1000.25, after the first pass's waits: quiet until 1030.25)
        assert_eq!(rig.pass(1005.0)["cpu_temp"], Json::Null);
        assert_eq!(rig.pass(1030.0)["cpu_temp"], Json::Null);
        assert_eq!(rig.pass(1030.5)["cpu_temp"], Json::Float(51.0));
        // The PC's own clock set back or forward changes nothing of it.
        let rig = a_machine();
        wmi(&rig.machine, LIBRE, TEMPERATURE_SENSORS, "[]");
        rig.pass(1000.0);
        rig.machine.the_pcs_clock_is_set_to(5_000_000.0);
        rig.moved.hw.step(&rig.moved.card);
        rig.machine.the_pcs_clock_is_set_to(10.0);
        rig.moved.hw.step(&rig.moved.card);
        assert_eq!(rig.asked_of_wmi(TEMPERATURE_SENSORS, LIBRE), 1);
        // The card's sensors in LibreHardwareMonitor are a place too.
        let rig = a_machine();
        rig.readers.reads(FakeReading::Says(j(r#"{"temp_c": 61}"#)));
        for second in 0..=60 {
            rig.pass(1000.0 + second as f64);
        }
        assert_eq!(rig.asked_of_wmi(ALL_SENSORS, LIBRE), 2);
    }

    #[test]
    fn the_ping_goes_to_a_plain_address_and_everything_else_is_the_backends_or_nothing() {
        let rig = a_machine();
        assert_eq!(rig.call("POST", "/api/monitor/start", r#"{"ping_target": "9.9.9.9"}"#).unwrap().0, 200);
        rig.machine.answers_with("ping", &[("target", "9.9.9.9".into()), ("timeout_ms", Json::Int(500))], Some(Json::parse(r#"{"ok": true, "ms": 0}"#).unwrap()));
        assert_eq!(rig.pass(1000.0)["net_ping_ms"], Json::Int(1), "an answer in under a millisecond is 1, as ping prints it");
        rig.machine.answers_with("ping", &[("target", "9.9.9.9".into()), ("timeout_ms", Json::Int(500))], Some(Json::parse(r#"{"ok": false}"#).unwrap()));
        assert_eq!(rig.pass(1001.0)["net_ping_ms"], Json::Null, "no answer");
        assert_eq!(rig.backend.asked(), Vec::<String>::new());
        // The library could not send: Python starts ping.exe.
        rig.machine.answers_with("ping", &[("target", "9.9.9.9".into()), ("timeout_ms", Json::Int(500))], None);
        rig.backend.says_to("/api/monitor/part?name=ping&target=9.9.9.9", Some(Json::parse(r#"{"ok": true, "reading": {"ms": 23}}"#).unwrap()));
        assert_eq!(rig.pass(1002.0)["net_ping_ms"], Json::Int(23));
        // A name, and an address written another way: the shell sends
        // nothing and looks nothing up.
        for (target, call) in [("example.com", "/api/monitor/part?name=ping&target=example.com"), ("001.1.1.1", "/api/monitor/part?name=ping&target=001.1.1.1"),
                               (" 1.1.1.1", "/api/monitor/part?name=ping&target=%201.1.1.1"), ("a b&c=d", "/api/monitor/part?name=ping&target=a%20b%26c%3Dd"),
                               ("", "/api/monitor/part?name=ping&target="), ("h\u{f6}st", "/api/monitor/part?name=ping&target=h%C3%B6st")] {
            rig.moved.hw.stop();
            rig.moved.hw.start(&rig.moved.card, target.into());
            rig.backend.says_to(call, Some(Json::parse(r#"{"ok": true, "reading": {"ms": 7}}"#).unwrap()));
            let asked_before = rig.machine.asked().iter().filter(|question| question.starts_with("ping")).count();
            assert_eq!(rig.pass(1003.0)["net_ping_ms"], Json::Int(7), "{target:?}");
            assert_eq!(rig.backend.asked().last().map(String::as_str), Some(call));
            assert_eq!(rig.machine.asked().iter().filter(|question| question.starts_with("ping")).count(), asked_before, "{target:?}: the library was asked to send");
        }
        // What is no text: no ping, and nobody is asked.
        let asked_before = (rig.backend.asked().len(), rig.machine.asked().iter().filter(|question| question.starts_with("ping")).count());
        for target in ["null", "5", "true", "[\"1.1.1.1\"]", "{\"a\": 1}", "1.5"] {
            rig.moved.hw.stop();
            assert_eq!(rig.call("POST", "/api/monitor/start", &format!(r#"{{"ping_target": {target}}}"#)).unwrap().0, 200);
            assert_eq!(rig.pass(1004.0)["net_ping_ms"], Json::Null, "{target}");
        }
        assert_eq!((rig.backend.asked().len(), rig.machine.asked().iter().filter(|question| question.starts_with("ping")).count()), asked_before);
    }

    #[test]
    fn a_part_that_raises_in_the_backend_drops_the_pass_as_pythons_loop_does() {
        let rig = a_machine();
        let kept = rig.pass(1000.0);
        rig.moved.hw.stop();
        rig.moved.hw.start(&rig.moved.card, "a-name".into());
        rig.backend.says_to("/api/monitor/part?name=ping&target=a-name", Some(Json::parse(r#"{"ok": false, "raised": true, "err": "ValueError: ..."}"#).unwrap()));
        assert_eq!(rig.pass(1001.0), kept, "nothing of the pass is kept");
        assert_eq!(rig.moved.hw.passes(), Passes { kept: 1, dropped: 1, asked_the_backend: 1 });
        assert_eq!(rig.moved.hw.snapshot()["history"]["cpu_load"].as_array().unwrap().len(), 1);
        // An answer that is neither a reading nor "it raised" is no answer.
        rig.backend.says_to("/api/monitor/part?name=ping&target=a-name", Some(Json::parse(r#"{"ok": false, "err": "Endpoint not found"}"#).unwrap()));
        assert_eq!((rig.pass(1002.0)["net_ping_ms"].clone(), rig.moved.hw.passes().kept), (Json::Null, 2));
    }

    #[test]
    fn each_kind_of_part_that_raises_in_the_backend_drops_the_pass() {
        const RAISED: &str = r#"{"ok": false, "raised": true, "err": "OSError: ..."}"#;
        // Each part is asked where Python would start a program for it, and
        // raises there; the parts before it in the pass answer.  (A place of
        // the temperature, and LibreHardwareMonitor's sensors of the card,
        // are asked of the backend where the helper has no answer for them.)
        let cases: [(&str, fn(&Rig)); 7] = [
            ("cpu", |rig| rig.machine.answers("cpu_clock", None)),
            ("temp_lhm", |rig| rig.machine.answers_with("wmi", &[("namespace", LIBRE.into()), ("query", TEMPERATURE_SENSORS.into())], None)),
            ("gpu_lhm", |rig| {
                rig.readers.reads(FakeReading::Says(j(r#"{"temp_c": 61}"#)));
                rig.machine.answers_with("wmi", &[("namespace", LIBRE.into()), ("query", ALL_SENSORS.into())], None);
            }),
            ("gpu", |rig| rig.readers.reads(FakeReading::HasNone)),
            ("ram", |rig| rig.machine.answers("memory", Some(j(r#"{"load_pct": 43, "standby_pages": null, "page_size": 4096}"#)))),
            ("dpc", |rig| {
                rig.machine.answers("cpu_times", None);
                rig.part("cpu", r#"{"ok": true, "reading": {"load": 5.0, "clock": 1, "temp": null}}"#);
            }),
            ("ping", |rig| {
                rig.moved.hw.stop();
                rig.moved.hw.start(&rig.moved.card, "a-name".into());
            }),
        ];
        for (name, makes_it_asked) in cases {
            let rig = a_machine();
            let kept = rig.pass(1000.0);
            makes_it_asked(&rig);
            let call = if name == "ping" { "/api/monitor/part?name=ping&target=a-name".to_string() } else { format!("/api/monitor/part?name={name}") };
            rig.backend.says_to(&call, Some(j(RAISED)));
            assert_eq!(rig.pass(1001.0), kept, "{name}: nothing of the pass is kept");
            assert_eq!((rig.moved.hw.passes().kept, rig.moved.hw.passes().dropped), (1, 1), "{name}");
            assert_eq!(rig.backend.asked().last(), Some(&call), "{name}: the part that raised was the last thing asked");
            // ... and where the part says that it has no reading, the pass is kept.
            rig.backend.says_to(&call, Some(j(r#"{"ok": false, "err": "Endpoint not found"}"#)));
            rig.pass(1002.0);
            assert_eq!((rig.moved.hw.passes().kept, rig.moved.hw.passes().dropped), (2, 1), "{name}");
        }
    }

    #[test]
    fn the_calls_are_taken_as_python_takes_them_and_a_start_is_never_passed_on_where_python_would_start() {
        let rig = a_machine();
        // Python stops before it starts anything: the backend's to say.
        for body in ["", "not json", "{", "[1]", "\"text\"", "5", "true", "{\"ping_target\": }"] {
            assert_eq!(rig.call("POST", "/api/monitor/start", body), None, "{body:?}");
        }
        assert_eq!(rig.moved.hw.snapshot()["running"], Json::Bool(false));
        // A body that says nothing is an empty one.
        for body in ["{}", "null", "0", "\"\"", "[]", "false", " {} ", "\u{feff}{}"] {
            assert_eq!(rig.call("POST", "/api/monitor/start", body).unwrap(), (200, Json::parse(r#"{"ok": true}"#).unwrap()), "{body:?}");
            assert_eq!(rig.moved.hw.state().target, Json::from(THE_USUAL_TARGET));
            rig.moved.hw.stop();
        }
        // The last of two targets counts, as in a Python dict.
        rig.call("POST", "/api/monitor/start", r#"{"ping_target": "8.8.8.8", "ping_target": "9.9.9.9"}"#).unwrap();
        assert_eq!(rig.moved.hw.state().target, Json::from("9.9.9.9"));
        // A start while it runs changes nothing, the target included.
        assert_eq!(rig.call("POST", "/api/monitor/start", r#"{"ping_target": "8.8.4.4"}"#).unwrap().1["msg"].as_str(), Some("Already running"));
        assert_eq!(rig.moved.hw.state().target, Json::from("9.9.9.9"));
        rig.moved.hw.stop();
        // What Python may read and this program cannot is refused here,
        // and nothing is started, here or (it is not passed on) there.
        let deep = format!("{{\"ping_target\": {}1{}}}", "[".repeat(80), "]".repeat(80));
        let not_plain: [&[u8]; 7] = [deep.as_bytes(), b"\xff\xfe{\0}\0", b"{\0}\0", b"\0{\0}", b"{\"ping_target\": \"\xed\xa0\x80\"}",
                                     b"{\"ping_target\": \"\\ud800\"}", b"{\"ping_target\": \"a\\udc00\"}"];
        for body in not_plain {
            let refused = rig.moved.answer(&Request::new("POST", "/api/monitor/start").with_body(body.to_vec())).unwrap();
            assert_eq!((refused.status, refused.to_json()["ok"].clone()), (400, Json::Bool(false)), "{body:?}");
            assert_eq!(rig.moved.hw.snapshot()["running"], Json::Bool(false));
        }
        // What is not UTF-8 in another way Python stops at, and says so.
        for body in [&b"\xff"[..], b"{\"ping_target\": \"\xc3\"}", b"{\"ping_target\": \"\xed\x9f\"}"] {
            assert_eq!(rig.moved.answer(&Request::new("POST", "/api/monitor/start").with_body(body.to_vec())).map(|answer| answer.status), None, "{body:?}");
        }
        // The calls are known however their path is written.
        assert_eq!(rig.call("POST", "/api/monitor/%73tart", "{}").unwrap().0, 200);
        assert_eq!(rig.call("GET", "/api/%6Donitor/snapshot?x=1", "").unwrap().1["running"], Json::Bool(true));
        assert_eq!(rig.call("POST", "/api%2Fmonitor%2Fstop", "").unwrap().0, 200);
        // Everything else of the monitor's is the backend's: another
        // method, the list of programs, the benchmark, the backend's own
        // call for a part.
        for (method, target) in [("GET", "/api/monitor/start"), ("PUT", "/api/monitor/start"), ("GET", "/api/monitor/stop"), ("POST", "/api/monitor/snapshot"),
                                 ("HEAD", "/api/monitor/snapshot"), ("OPTIONS", "/api/monitor/start"), ("GET", "/api/monitor/processes"),
                                 ("POST", "/api/monitor/benchmark"), ("GET", "/api/monitor/part?name=cpu"), ("POST", "/api/monitor/start/"),
                                 ("POST", "/api/monitor/Start"), ("GET", "/api/monitor/"), ("GET", "/api/monitor")] {
            assert_eq!(rig.call(method, target, "{}"), None, "{method} {target}");
        }
    }

    #[test]
    fn what_is_compared_with_is_kept_through_a_stop_and_goes_stale_for_the_shares() {
        let rig = a_machine();
        rig.pass(1000.0);
        assert_eq!(rig.machine.waited(), 0.5);
        // Five seconds later: both compare with the pass before.
        let latest = rig.pass(1005.0);
        assert_eq!((rig.machine.waited(), &latest["cpu_load"], &latest["dpc_pct"]), (0.5, &Json::Float(25.0), &Json::Float(0.5)));
        // A minute later: the load still does (Python keeps no limit for
        // it), the shares take a reading of their own again.
        rig.machine.grows("cpu_times", Some(Json::parse(r#"{"total": 80000000, "idle": 40000000, "dpc": 800000, "interrupt": 80000}"#).unwrap()));
        let latest = rig.pass(1065.0);
        assert_eq!((rig.machine.waited(), &latest["cpu_load"], &latest["dpc_pct"]), (0.75, &Json::Float(50.0), &Json::Float(1.0)));
        // Another number of processors: the same.
        rig.machine.answers("cpu_times", Some(Json::parse(r#"{"total": 20000000000, "idle": 15000000000, "dpc": 400000000, "interrupt": 200000000, "processors": 16}"#).unwrap()));
        rig.pass(1066.0);
        assert_eq!(rig.machine.waited(), 1.0);
        // Times that did not move give nothing: the backend's.  (The
        // first pass after they stopped still sees the last of them move.)
        rig.machine.grows("cpu_times", None);
        rig.part("cpu", r#"{"ok": true, "reading": {"load": 7.5}}"#);
        rig.part("dpc", r#"{"ok": true, "reading": {"dpc_pct": 0.75, "isr_pct": 0.5, "perfmon_ok": true}}"#);
        rig.pass(1067.0);
        let latest = rig.pass(1068.0);
        assert_eq!((&latest["cpu_load"], &latest["dpc_pct"], rig.machine.waited()), (&Json::Float(7.5), &Json::Float(0.75), 1.25));
    }

    #[test]
    fn started_for_real_it_reads_once_a_second_and_ends_when_it_is_stopped() {
        // A thread of its own, on a made-up machine whose clocks stand
        // still: a pass, then the rest of the second.
        let rig = a_machine_with(true);
        rig.moved.hw.start(&rig.moved.card, THE_USUAL_TARGET.into());
        // (the first pass waits half a made-up second, which the loop
        // counts as gone: the second comes after half a real one, every
        // later one a second after the one before)
        let began = std::time::Instant::now();
        while rig.moved.hw.passes().kept < 3 && began.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(20));
        }
        let took = began.elapsed();
        assert!(rig.moved.hw.passes().kept == 3 && took > Duration::from_millis(1300) && took < Duration::from_secs(6), "{took:?}");
        // Stopped and started again at once: one loop, not two (two would
        // take six passes in the time one takes three).
        rig.moved.hw.stop();
        rig.moved.hw.start(&rig.moved.card, THE_USUAL_TARGET.into());
        std::thread::sleep(Duration::from_millis(2500));
        let kept = rig.moved.hw.passes().kept;
        assert!((5..=7).contains(&kept), "{kept} passes");
        // Closed, as at a quit: the loop ends, and no start begins one.
        rig.moved.close();
        std::thread::sleep(Duration::from_millis(300));
        let at_the_close = rig.moved.hw.passes().kept;
        rig.moved.hw.start(&rig.moved.card, THE_USUAL_TARGET.into());
        std::thread::sleep(Duration::from_millis(1300));
        assert_eq!(rig.moved.hw.passes().kept, at_the_close);
    }

    #[test]
    fn started_for_real_it_sleeps_and_eight_looks_at_once_wake_one_loop() {
        let rig = a_machine_with(true);
        let hw = &rig.moved.hw;
        let until = |what: &dyn Fn() -> bool| {
            let began = std::time::Instant::now();
            while !what() && began.elapsed() < Duration::from_secs(10) {
                std::thread::sleep(Duration::from_millis(20));
            }
            what()
        };
        hw.start(&rig.moved.card, THE_USUAL_TARGET.into());
        assert!(until(&|| hw.passes().kept >= 1));
        // Nobody has looked for longer than 30 s, by the monitor's own
        // clock: it sleeps after the pass it is in, and takes no more.
        rig.machine.at(1031.0);
        assert!(until(&|| hw.state().thread.is_none()));
        let asleep = hw.passes().kept;
        std::thread::sleep(Duration::from_millis(1500));
        assert_eq!(hw.passes().kept, asleep, "a loop that sleeps takes no pass");
        assert_eq!(hw.snapshot()["running"], Json::Bool(true));
        // Eight look at once: one loop is woken (two would take twice the
        // passes in the same time).
        let starts = hw.state().starts;
        let looks: Vec<_> = (0..8).map(|_| {
            let (hw, card) = (hw.clone(), rig.moved.card.clone());
            std::thread::spawn(move || hw.look(&card))
        }).collect();
        for look in looks {
            look.join().unwrap();
        }
        assert_eq!(hw.state().starts, starts + 1);
        std::thread::sleep(Duration::from_millis(2600));
        let woken = hw.passes().kept - asleep;
        assert!((2..=5).contains(&woken), "{woken} passes in 2.6 s");
        // Asleep again, and stopped while it sleeps: a look does not wake it.
        rig.machine.at(1070.0);
        assert!(until(&|| hw.state().thread.is_none()));
        hw.stop();
        hw.look(&rig.moved.card);
        let kept = hw.passes().kept;
        std::thread::sleep(Duration::from_millis(1300));
        assert!(hw.state().thread.is_none() && hw.passes().kept == kept && hw.snapshot()["running"] == Json::Bool(false));
        rig.moved.close();
    }
}
