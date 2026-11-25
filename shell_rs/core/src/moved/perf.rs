//! The performance sampler: what the graphics card is doing, five times a
//! second, for the Monitor page's live charts and its stability score.
//!
//! This is core/perf_monitor.py, moved.  The Python module stays, and stays
//! what answers when the app is started as it always was.  Here: the same
//! five calls, the same answers (tests/test_moved_perf.py asks both the same
//! things on the same made-up machine and compares), and one reading of
//! the card for each sample, taken in the shell's own process.
//!
//! It only reads.  Starting and stopping it starts and stops a thread of
//! the shell's own and changes nothing on the PC.  It runs only while
//! somebody wants it (the Monitor page says so, and says when it leaves).
//!
//! Where the shell does not take a call:
//!
//! * The card gives the shell no reading.  The Python module then asks
//!   nvidia-smi, which the shell does not start.  Whether the card answers
//!   is asked once, at the first call, and if it does not, every call of
//!   the sampler's is the backend's for the rest of the run.  So there is
//!   one sampler, in one place, and a PC whose card answers only to
//!   nvidia-smi is served as it was.
//! * A call that is not what the page sends: another method, a body that
//!   is no JSON, or JSON that Python cannot use (it stops at those before
//!   it touches the sampler, and says so in words of its own: those words
//!   are the backend's to say).

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;

use super::{py_round, query_int, Readings};
use crate::http::{Request, Response};
use crate::json::Json;

/// One sample every this long.
pub const EVERY: Duration = Duration::from_millis(200);
/// How many samples are kept: a minute of them.
pub const KEPT: usize = 300;
/// Below this much use, the card is doing nothing that a score could be
/// made of.
const IDLE_UTIL: i128 = 15;
/// What the library says about the card, each a whole number or nothing.
/// (core/native.py, _GPU_FIELDS: an answer is taken only if every one of
/// them is.)
const GPU_FIELDS: [&str; 14] = ["temp_c", "gpu_util_pct", "mem_util_pct", "core_mhz", "mem_mhz", "core_max_mhz", "mem_max_mhz", "core_app_mhz",
                                "mem_app_mhz", "power_mw", "power_limit_mw", "fan_pct", "mem_used_bytes", "mem_total_bytes"];

/// One reading of the card.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub ts: f64,
    /// Did the card give a reading?  Everything else is 0 where it did not.
    pub ok: bool,
    pub core: i128,
    pub mem: i128,
    pub temp: i128,
    pub power: f64,
    pub util: i128,
    pub memutil: i128,
    pub fan: i128,
}

impl Sample {
    fn to_json(&self) -> Json {
        Json::object([
            ("ts", self.ts.into()),
            ("ok", self.ok.into()),
            ("core", Json::Int(self.core)),
            ("mem", Json::Int(self.mem)),
            ("temp", Json::Int(self.temp)),
            ("power", self.power.into()),
            ("util", Json::Int(self.util)),
            ("memutil", Json::Int(self.memutil)),
            ("fan", Json::Int(self.fan)),
        ])
    }
}

/// What the card says now, as the app takes it (core/native.py, gpu_live,
/// and core/gpu_overclock.py, _live_state_from_native): every value a whole
/// number or nothing, and never without the temperature.  None for an
/// answer that is not taken.
fn card(door: &dyn Readings) -> Option<Vec<(&'static str, Option<i128>)>> {
    let said = door.ask("gpu_live")?;
    if !said.is_object() {
        return None;
    }
    let mut values = Vec::with_capacity(GPU_FIELDS.len());
    for name in GPU_FIELDS {
        values.push((name, match said.get(name) {
            None | Some(Json::Null) => None,
            Some(Json::Int(whole)) => Some(*whole),
            // (Python's int() of a number with a fraction cuts the fraction off)
            Some(Json::Float(number)) if number.is_finite() => Some(number.trunc() as i128),
            Some(_) => return None,
        }));
    }
    values.iter().any(|(name, value)| *name == "temp_c" && value.is_some()).then_some(values)
}

/// A sample of what the card says at `ts` (None: it said nothing usable).
fn sample_of(ts: f64, card: Option<Vec<(&'static str, Option<i128>)>>) -> Sample {
    let Some(card) = card else {
        return Sample { ts, ok: false, core: 0, mem: 0, temp: 0, power: 0.0, util: 0, memutil: 0, fan: 0 };
    };
    let value = |name: &str| card.iter().find(|(known, _)| *known == name).and_then(|(_, value)| *value);
    let whole = |name: &str| value(name).unwrap_or(0);
    // (the library counts milliwatts; the app shows watts with two places)
    let watts = value("power_mw").map_or(0.0, |milliwatts| py_round(milliwatts as f64 / 1000.0, 2));
    Sample { ts, ok: true, core: whole("core_mhz"), mem: whole("mem_mhz"), temp: whole("temp_c"), power: watts, util: whole("gpu_util_pct"),
             memutil: whole("mem_util_pct"), fan: whole("fan_pct") }
}

struct State {
    /// Is the card's reading the shell's to take?  Asked at the first call.
    mine: Option<bool>,
    /// As the Python module keeps it: true from a start until the sampling
    /// thread has ended.
    running: bool,
    /// Which start the thread that samples now belongs to.
    thread: Option<u64>,
    /// That thread was told to stop, and has not ended yet.
    told_to_stop: bool,
    starts: u64,
    /// Who wants the sampler, in the order they asked: a set.
    consumers: Vec<Json>,
    started_at: Option<f64>,
    samples: VecDeque<Sample>,
    last: Option<Sample>,
}

/// The sampler.
pub struct Sampler {
    state: Mutex<State>,
    /// Rung when the sampling thread is told to stop.
    told: Condvar,
    /// Does a start run a thread?  (Not where a test takes every sample by
    /// hand: `step`.)
    threads: bool,
}

/// Are two of the things a consumer can be named by the same to Python?
/// (Names are texts.  But whatever JSON value is sent is kept, and Python
/// holds a number, a truth value and a number with a fraction for the same
/// thing where they count the same.)
fn same_to_python(a: &Json, b: &Json) -> bool {
    fn number(value: &Json) -> Option<f64> {
        match value {
            Json::Bool(truth) => Some(if *truth { 1.0 } else { 0.0 }),
            Json::Int(whole) => Some(*whole as f64),
            Json::Float(number) => Some(*number),
            _ => None,
        }
    }
    match (a, b) {
        (Json::Null, Json::Null) => true,
        (Json::Str(a), Json::Str(b)) => a == b,
        (Json::Int(a), Json::Int(b)) => a == b,
        _ => matches!((number(a), number(b)), (Some(a), Some(b)) if a == b),
    }
}

/// What a POST of the page says, as the route reads it
/// (`request.get_json(force=True) or {}`), or None where Python stops
/// before it touches the sampler: a body that is no JSON, or JSON that is
/// something and not an object.
fn said(request: &Request) -> Option<Json> {
    let said = Json::parse(std::str::from_utf8(&request.body).ok()?).ok()?;
    match said {
        Json::Object(_) => Some(said),
        other if !other.truthy() => Some(Json::Object(Vec::new())),
        _ => None,
    }
}

/// The consumer a POST names (`data.get("consumer", "manual")`), or None for
/// one that Python cannot keep in a set (a list, an object).
fn consumer(said: &Json) -> Option<Json> {
    match said.get("consumer") {
        None => Some("manual".into()),
        Some(Json::Array(_) | Json::Object(_)) => None,
        Some(named) => Some(named.clone()),
    }
}

impl Sampler {
    pub fn new(threads: bool) -> Arc<Sampler> {
        Arc::new(Sampler {
            state: Mutex::new(State { mine: None, running: false, thread: None, told_to_stop: false, starts: 0, consumers: Vec::new(), started_at: None,
                                      samples: VecDeque::with_capacity(KEPT), last: None }),
            told: Condvar::new(),
            threads,
        })
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The shell's answer to one of the sampler's calls, or None: the call
    /// is the backend's (see the top of this file).
    pub fn answer(self: &Arc<Self>, door: &Arc<dyn Readings>, request: &Request) -> Option<Response> {
        let (method, path, target) = (request.method.as_str(), request.path(), request.target.as_str());
        let answer = match (method, path) {
            ("GET", "/api/perf/status") | ("GET", "/api/perf/samples") | ("GET", "/api/perf/score") => {
                if !self.is_mine(door) {
                    return None;
                }
                match path {
                    "/api/perf/status" => self.status(),
                    "/api/perf/samples" => self.chart(door.now(), query_int(target, "seconds", 60)),
                    _ => self.score(door.now(), query_int(target, "seconds", 30)),
                }
            }
            ("POST", "/api/perf/start") | ("POST", "/api/perf/stop") => {
                let said = said(request)?;
                let consumer = consumer(&said)?;
                if !self.is_mine(door) {
                    return None;
                }
                if path == "/api/perf/start" {
                    self.start(door, consumer)
                } else {
                    self.stop(consumer, said.get("force").is_some_and(Json::truthy))
                }
            }
            _ => return None,
        };
        Some(Response::json(200, &answer))
    }

    /// Does the card give the shell a reading?  Asked once.
    fn is_mine(&self, door: &Arc<dyn Readings>) -> bool {
        if let Some(mine) = self.state().mine {
            return mine;
        }
        // (asked without the lock: the first reading loads the driver's library)
        let answers = card(&**door).is_some();
        *self.state().mine.get_or_insert(answers)
    }

    /// Start sampling for `consumer`, if nobody has yet.
    pub fn start(self: &Arc<Self>, door: &Arc<dyn Readings>, consumer: Json) -> Json {
        let mut st = self.state();
        if !st.consumers.iter().any(|known| same_to_python(known, &consumer)) {
            st.consumers.push(consumer);
        }
        let consumers = Json::Array(st.consumers.clone());
        // (running, and told to stop: a thread on its way out.  It is not
        // waited for: a new one is started, and the old one ends by itself.)
        if st.running && st.thread.is_some() && !st.told_to_stop {
            return Json::object([("ok", true.into()), ("msg", "already running".into()), ("consumers", consumers)]);
        }
        st.starts += 1;
        let this = st.starts;
        st.thread = Some(this);
        st.told_to_stop = false;
        st.running = true;
        st.started_at = Some(door.now());
        st.samples.clear();
        drop(st);
        if self.threads {
            let (sampler, door) = (self.clone(), door.clone());
            let started = std::thread::Builder::new().name("perf-sampler".to_string()).spawn(move || sampler.sample_until_told(&*door, this));
            if started.is_err() {
                // (no thread could be made: nothing samples, and that is said as "not running")
                let mut st = self.state();
                if st.thread == Some(this) {
                    st.thread = None;
                    st.running = false;
                }
            }
        }
        Json::object([("ok", true.into()), ("consumers", consumers)])
    }

    /// `consumer` does not want the sampler any more.  It stops when nobody
    /// does, or at once with `force`.
    pub fn stop(&self, consumer: Json, force: bool) -> Json {
        let mut st = self.state();
        st.consumers.retain(|known| !same_to_python(known, &consumer));
        if !st.consumers.is_empty() && !force {
            return Json::object([("ok", true.into()), ("still_running_for", Json::Array(st.consumers.clone()))]);
        }
        if !st.running {
            return Json::object([("ok", true.into()), ("msg", "already stopped".into())]);
        }
        st.consumers.clear();
        if st.thread.is_some() {
            st.told_to_stop = true;
        }
        drop(st);
        self.told.notify_all();
        Json::object([("ok", true.into()), ("stopped", true.into())])
    }

    /// Take one sample.
    fn sample(&self, door: &dyn Readings) {
        let ts = door.now();
        let sample = sample_of(ts, card(door));
        let mut st = self.state();
        if st.samples.len() == KEPT {
            st.samples.pop_front();
        }
        st.samples.push_back(sample.clone());
        st.last = Some(sample);
    }

    /// The thread of the start `this`: a sample, a pause, until it is told
    /// to stop.
    fn sample_until_told(&self, door: &dyn Readings, this: u64) {
        let stays = |st: &State| st.thread == Some(this) && !st.told_to_stop;
        loop {
            if !stays(&self.state()) {
                break;
            }
            self.sample(door);
            let st = self.state();
            drop(self.told.wait_timeout_while(st, EVERY, |st| stays(st)).unwrap_or_else(|poisoned| poisoned.into_inner()));
        }
        // (a newer start may have taken this one's place already)
        let mut st = self.state();
        if st.thread == Some(this) {
            st.thread = None;
            st.told_to_stop = false;
            st.running = false;
        }
    }

    /// What the sampling thread does in one round, done by hand: a sample
    /// if it is to go on, its end if it was told to stop.  For a sampler
    /// that runs no thread (`threads` false), where the clock and the card
    /// are a test's.
    pub fn step(&self, door: &dyn Readings) {
        let (alive, told) = {
            let st = self.state();
            (st.thread.is_some(), st.told_to_stop)
        };
        if !alive {
            return;
        }
        if told {
            let mut st = self.state();
            st.thread = None;
            st.told_to_stop = false;
            st.running = false;
        } else {
            self.sample(door);
        }
    }

    pub fn is_running(&self) -> bool {
        self.state().running
    }

    /// Whether it runs, for whom, and the last sample.
    pub fn status(&self) -> Json {
        let st = self.state();
        Json::object([
            ("running", st.running.into()),
            ("consumers", Json::Array(st.consumers.clone())),
            ("started_at", st.started_at.map_or(Json::Null, Into::into)),
            ("sample_count", (st.samples.len() as u64).into()),
            ("max_samples", (KEPT as u64).into()),
            ("interval_ms", (EVERY.as_millis() as u64).into()),
            ("last", st.last.as_ref().map_or(Json::Null, Sample::to_json)),
        ])
    }

    /// The samples of the last `seconds` seconds.
    fn since(&self, now: f64, seconds: i128) -> Vec<Sample> {
        let from = now - seconds as f64;
        self.state().samples.iter().filter(|sample| sample.ts >= from).cloned().collect()
    }

    /// The last `seconds` seconds, one list for each thing the chart draws.
    pub fn chart(&self, now: f64, seconds: i128) -> Json {
        let samples = self.since(now, seconds);
        let whole = |of: fn(&Sample) -> i128| Json::Array(samples.iter().map(|sample| Json::Int(of(sample))).collect());
        Json::object([
            ("ts", Json::Array(samples.iter().map(|sample| sample.ts.into()).collect())),
            ("core", whole(|sample| sample.core)),
            ("mem", whole(|sample| sample.mem)),
            ("temp", whole(|sample| sample.temp)),
            ("power", Json::Array(samples.iter().map(|sample| sample.power.into()).collect())),
            ("util", whole(|sample| sample.util)),
            ("fan", whole(|sample| sample.fan)),
            ("count", (samples.len() as u64).into()),
            ("window_s", Json::Int(seconds)),
        ])
    }

    /// How steady the card ran in the last `seconds` seconds, 0 to 100:
    /// its clock (40 of the points), its temperature (30) and how busy it
    /// stayed while it was busy (30).  No score for too few samples, or for
    /// a card that did nothing.
    pub fn score(&self, now: f64, seconds: i128) -> Json {
        let samples = self.since(now, seconds);
        let count = samples.len();
        let last_util = samples.last().map_or(0, |sample| sample.util);
        let last_temp = samples.last().map_or(0, |sample| sample.temp);
        let avg_util = if count == 0 { 0.0 } else { samples.iter().map(|sample| sample.util).sum::<i128>() as f64 / count as f64 };
        let max_util = samples.iter().map(|sample| sample.util).max().unwrap_or(0);
        let none = |verdict: &str, reason: String| {
            Json::object([
                ("score", Json::Null),
                ("verdict", verdict.into()),
                ("reason", reason.into()),
                ("subs", Json::Object(Vec::new())),
                ("samples_used", (count as u64).into()),
                ("last_util", Json::Int(last_util)),
                ("last_temp", Json::Int(last_temp)),
                ("avg_util", py_round(avg_util, 1).into()),
                ("max_util", Json::Int(max_util)),
                ("idle_threshold", Json::Int(IDLE_UTIL)),
            ])
        };
        if count < 10 {
            return none("no-data", format!("need \u{2265}10 samples, have {count}"));
        }
        let active: Vec<&Sample> = samples.iter().filter(|sample| sample.util >= IDLE_UTIL && sample.ok).collect();
        if active.is_empty() {
            return none("idle", format!("GPU idle (max util in window: {max_util}%, need \u{2265}{IDLE_UTIL}%)"));
        }
        let active_pct = active.len() as f64 / count as f64;
        let clocks: Vec<i128> = active.iter().map(|sample| sample.core).collect();
        let temps: Vec<i128> = active.iter().map(|sample| sample.temp).filter(|temp| *temp > 0).collect();
        let utils: Vec<i128> = active.iter().map(|sample| sample.util).collect();

        // The clock: none of the points are lost while it does not move,
        // all of them when it moves by 200 MHz (one standard deviation).
        let clock_std = spread(&clocks);
        let clock_score = (40.0 - (clock_std / 200.0 * 40.0)).max(0.0);
        // The temperature: all points for staying where it is, none for 25 degrees between the lowest and the highest.
        let temp_range = match (temps.iter().max(), temps.iter().min()) {
            (Some(highest), Some(lowest)) => highest - lowest,
            _ => 0,
        };
        let temp_score = if temps.is_empty() { 30.0 } else { (30.0 - (temp_range as f64 / 25.0 * 30.0)).max(0.0) };
        // How busy: all points for staying as busy as it was, none for 30 points of spread.
        let util_std = spread(&utils);
        let util_score = (30.0 - (util_std / 30.0 * 30.0)).max(0.0);

        let score = py_round(clock_score + temp_score + util_score, 1);
        let verdict = if score >= 85.0 {
            "healthy"
        } else if score >= 65.0 {
            "warn"
        } else {
            "unstable"
        };
        Json::object([
            ("score", score.into()),
            ("verdict", verdict.into()),
            ("subs", Json::object([
                ("clock_score", py_round(clock_score, 1).into()),
                ("temp_score", py_round(temp_score, 1).into()),
                ("util_score", py_round(util_score, 1).into()),
                ("clock_std", py_round(clock_std, 1).into()),
                ("temp_range", Json::Int(temp_range)),
                ("util_std", py_round(util_std, 1).into()),
                ("active_pct", py_round(active_pct * 100.0, 1).into()),
            ])),
            ("samples_used", (active.len() as u64).into()),
            ("window_s", Json::Int(seconds)),
            ("last_util", Json::Int(last_util)),
            ("last_temp", Json::Int(last_temp)),
            ("avg_util", py_round(avg_util, 1).into()),
            ("max_util", Json::Int(max_util)),
            ("idle_threshold", Json::Int(IDLE_UTIL)),
        ])
    }
}

/// How far whole numbers are spread around their mean: the standard
/// deviation of all of them (Python's `statistics.pstdev`), 0 for fewer
/// than two.  Counted in whole numbers up to the last step, as Python
/// counts it in fractions: n times the sum of the squares, less the square
/// of the sum, over n squared, is the variance, exactly.
fn spread(values: &[i128]) -> f64 {
    if values.len() < 2 {
        return 0.0;
    }
    let n = values.len() as i128;
    let (sum, squares) = values.iter().fold((0i128, 0i128), |(sum, squares), value| (sum + value, squares + value * value));
    let over_n_squared = n * squares - sum * sum;
    (over_n_squared as f64 / (n * n) as f64).sqrt()
}

#[cfg(test)]
mod tests {
    use super::super::Fake;
    use super::*;

    fn reading(temp: i128, util: i128, core: i128) -> Json {
        Json::object([("temp_c", Json::Int(temp)), ("gpu_util_pct", Json::Int(util)), ("mem_util_pct", 40.into()), ("core_mhz", Json::Int(core)),
                      ("mem_mhz", 9501.into()), ("power_mw", 215_437.into()), ("fan_pct", 55.into())])
    }

    fn a_machine() -> (Arc<Fake>, Arc<dyn Readings>, Arc<Sampler>) {
        let machine = Fake::new();
        machine.answers("gpu_live", Some(reading(61, 97, 2100)));
        machine.at(1000.0);
        let door: Arc<dyn Readings> = machine.clone();
        (machine, door, Sampler::new(false))
    }

    fn call(sampler: &Arc<Sampler>, door: &Arc<dyn Readings>, method: &str, target: &str, body: &str) -> Option<Json> {
        let answer = sampler.answer(door, &Request::new(method, target).with_body(body))?;
        assert_eq!((answer.status, answer.header("Content-Type")), (200, Some("application/json")));
        Some(answer.to_json())
    }

    #[test]
    fn a_reading_becomes_a_sample_as_the_app_makes_one() {
        let (machine, door, _) = a_machine();
        let taken = sample_of(5.0, card(&*door));
        assert_eq!(taken, Sample { ts: 5.0, ok: true, core: 2100, mem: 9501, temp: 61, power: 215.44, util: 97, memutil: 40, fan: 55 });
        // What the card does not say is 0, and a number with a fraction is cut.
        machine.answers("gpu_live", Some(Json::object([("temp_c", 53.9.into()), ("fan_pct", Json::Null), ("power_mw", 1.into())])));
        assert_eq!(sample_of(5.0, card(&*door)), Sample { ts: 5.0, ok: true, core: 0, mem: 0, temp: 53, power: 0.0, util: 0, memutil: 0, fan: 0 });
        // Not taken: no temperature, a value that is no number (also one the
        // sampler does not use), an answer that is no object, no answer.
        let nothing = Sample { ts: 5.0, ok: false, core: 0, mem: 0, temp: 0, power: 0.0, util: 0, memutil: 0, fan: 0 };
        for not_one in [Some(Json::object([("gpu_util_pct", 97.into())])), Some(Json::object([("temp_c", Json::Null), ("core_mhz", 2100.into())])),
                        Some(Json::object([("temp_c", 61.into()), ("mem_total_bytes", "a lot".into())])), Some(Json::object([("temp_c", true.into())])),
                        Some(Json::object([("temp_c", f64::NAN.into())])), Some(Json::Array(Vec::new())), Some("warm".into()), None] {
            machine.answers("gpu_live", not_one.clone());
            assert_eq!(sample_of(5.0, card(&*door)), nothing, "{not_one:?}");
        }
    }

    #[test]
    fn it_starts_for_whoever_asks_and_stops_when_the_last_one_leaves() {
        let (machine, door, sampler) = a_machine();
        let post = |target: &str, body: &str| call(&sampler, &door, "POST", target, body).unwrap().to_string();
        assert_eq!(post("/api/perf/start", r#"{"consumer": "hwmon-page"}"#), r#"{"ok":true,"consumers":["hwmon-page"]}"#);
        assert!(sampler.is_running());
        assert_eq!(post("/api/perf/start", r#"{"consumer": "hwmon-page"}"#), r#"{"ok":true,"msg":"already running","consumers":["hwmon-page"]}"#);
        assert_eq!(post("/api/perf/start", "{}"), r#"{"ok":true,"msg":"already running","consumers":["hwmon-page","manual"]}"#);
        for round in 0..3 {
            machine.at(1000.2 + round as f64 * 0.2);
            sampler.step(&*door);
        }
        let status = sampler.status();
        assert_eq!((status["sample_count"].as_int(), status["max_samples"].as_int(), status["interval_ms"].as_int()), (Some(3), Some(300), Some(200)));
        assert_eq!((status["running"].truthy(), &status["started_at"]), (true, &Json::Float(1000.0)));
        assert_eq!(status["last"].to_string(), r#"{"ts":1000.6,"ok":true,"core":2100,"mem":9501,"temp":61,"power":215.44,"util":97,"memutil":40,"fan":55}"#);
        // One of two leaves: it goes on.  The other leaves: it is told to
        // stop, and is running until its thread has ended.
        assert_eq!(post("/api/perf/stop", r#"{"consumer": "manual"}"#), r#"{"ok":true,"still_running_for":["hwmon-page"]}"#);
        assert_eq!(post("/api/perf/stop", r#"{"consumer": "hwmon-page"}"#), r#"{"ok":true,"stopped":true}"#);
        assert!(sampler.is_running(), "it says it stopped before its thread has ended");
        sampler.step(&*door);
        assert!(!sampler.is_running());
        assert_eq!(sampler.status()["sample_count"].as_int(), Some(3), "a sample was taken after it was told to stop");
        assert_eq!(post("/api/perf/stop", "{}"), r#"{"ok":true,"msg":"already stopped"}"#);
        // The samples and the last one stay until the next start, which begins anew.
        assert_eq!(sampler.chart(1001.0, 60)["count"].as_int(), Some(3));
        machine.at(2000.0);
        assert_eq!(post("/api/perf/start", "null"), r#"{"ok":true,"consumers":["manual"]}"#);
        let status = sampler.status();
        assert_eq!((status["sample_count"].as_int(), &status["started_at"], status["last"]["ts"].clone()), (Some(0), &Json::Float(2000.0), Json::Float(1000.6)));
        // With force, whoever else wants it.
        post("/api/perf/start", r#"{"consumer": "somebody"}"#);
        assert_eq!(post("/api/perf/stop", r#"{"consumer": "nobody", "force": 1}"#), r#"{"ok":true,"stopped":true}"#);
        assert_eq!(sampler.status()["consumers"].to_string(), "[]");
        // Told to stop and started again before its thread has ended: a new
        // one samples, and it is running all along.
        assert_eq!(post("/api/perf/start", r#"{"consumer": "again"}"#), r#"{"ok":true,"consumers":["again"]}"#);
        sampler.step(&*door);
        assert!(sampler.is_running() && sampler.status()["sample_count"].as_int() == Some(1));
    }

    #[test]
    fn a_consumer_is_whatever_python_would_keep_for_one() {
        let (_, door, sampler) = a_machine();
        let post = |target: &str, body: &str| call(&sampler, &door, "POST", target, body).map(|said| said.to_string());
        // A name is a text; but a number, a truth value and nothing at all
        // are kept as well, and 1, 1.0 and true are one and the same.
        assert_eq!(post("/api/perf/start", r#"{"consumer": 1}"#).unwrap(), r#"{"ok":true,"consumers":[1]}"#);
        for same in ["true", "1.0"] {
            assert_eq!(post("/api/perf/start", &format!(r#"{{"consumer": {same}}}"#)).unwrap(), r#"{"ok":true,"msg":"already running","consumers":[1]}"#);
        }
        assert_eq!(post("/api/perf/start", r#"{"consumer": null}"#).unwrap(), r#"{"ok":true,"msg":"already running","consumers":[1,null]}"#);
        assert_eq!(post("/api/perf/start", r#"{"consumer": "1"}"#).unwrap(), r#"{"ok":true,"msg":"already running","consumers":[1,null,"1"]}"#);
        assert_eq!(post("/api/perf/stop", r#"{"consumer": true}"#).unwrap(), r#"{"ok":true,"still_running_for":[null,"1"]}"#);
        // The last of two that name the same consumer is the one that counts.
        assert_eq!(post("/api/perf/stop", r#"{"consumer": "x", "consumer": "1"}"#).unwrap(), r#"{"ok":true,"still_running_for":[null]}"#);
        // What Python stops at before it touches the sampler is not the
        // shell's to answer: no JSON, JSON that is something and no object,
        // a consumer that cannot be kept in a set.
        for not_taken in ["", "{", "not json", "[1]", "\"text\"", "7", "true", r#"{"consumer": []}"#, r#"{"consumer": {"a": 1}}"#] {
            assert_eq!(post("/api/perf/start", not_taken), None, "{not_taken:?}");
            assert_eq!(post("/api/perf/stop", not_taken), None, "{not_taken:?}");
        }
        assert_eq!(sampler.status()["consumers"].to_string(), "[null]", "a call that was not taken changed something");
        // Nothing at all in a body that is JSON: as an empty object.
        for nothing in ["null", "false", "0", "0.0", "\"\"", "[]", "{}"] {
            assert_eq!(post("/api/perf/start", nothing).unwrap(), r#"{"ok":true,"msg":"already running","consumers":[null,"manual"]}"#, "{nothing}");
        }
        // Force is whatever Python takes for true.
        for (force, stops) in [("0", false), ("\"\"", false), ("null", false), ("[]", false), ("false", false), ("\"no\"", true), ("[0]", true), ("0.5", true)] {
            post("/api/perf/start", r#"{"consumer": "a"}"#);
            post("/api/perf/start", r#"{"consumer": "b"}"#);
            let said = post("/api/perf/stop", &format!(r#"{{"consumer": "a", "force": {force}}}"#)).unwrap();
            assert_eq!(said.contains("\"stopped\":true"), stops, "force {force}: {said}");
            post("/api/perf/stop", r#"{"force": true}"#);
            sampler.step(&*door);
        }
    }

    #[test]
    fn only_what_the_page_sends_is_taken() {
        let (_, door, sampler) = a_machine();
        for (method, target) in [("POST", "/api/perf/status"), ("GET", "/api/perf/start"), ("GET", "/api/perf/stop"), ("PUT", "/api/perf/samples"),
                                 ("DELETE", "/api/perf/score"), ("HEAD", "/api/perf/status"), ("OPTIONS", "/api/perf/start"), ("GET", "/api/perf/"),
                                 ("GET", "/api/perf/status/"), ("GET", "/api/perf/other"), ("GET", "/api/perf")] {
            assert_eq!(call(&sampler, &door, method, target, "{}"), None, "{method} {target}");
        }
        assert!(!sampler.is_running() && sampler.state().mine.is_none(), "a call that was not taken did something");
        assert!(call(&sampler, &door, "GET", "/api/perf/status?x=1", "").is_some());
    }

    #[test]
    fn a_card_that_gives_the_shell_no_reading_leaves_the_sampler_to_the_backend() {
        // (Python then asks nvidia-smi, which the shell does not start)
        let (machine, door, sampler) = a_machine();
        machine.answers("gpu_live", None);
        let all = [("GET", "/api/perf/status"), ("GET", "/api/perf/samples"), ("GET", "/api/perf/score"), ("POST", "/api/perf/start"), ("POST", "/api/perf/stop")];
        for (method, target) in all {
            assert_eq!(call(&sampler, &door, method, target, "{}"), None, "{method} {target}");
        }
        assert_eq!(machine.asked(), ["gpu_live"], "it is asked once, at the first call");
        // For the rest of the run, also when the card answers later: there
        // is one sampler, and it is the backend's by now.
        machine.answers("gpu_live", Some(reading(61, 97, 2100)));
        for (method, target) in all {
            assert_eq!(call(&sampler, &door, method, target, "{}"), None);
        }
        assert!(!sampler.is_running());
        // And the other way round: a card that answered at the first call
        // and stops answering gives samples that say so.
        let (machine, door, sampler) = a_machine();
        call(&sampler, &door, "POST", "/api/perf/start", "{}").unwrap();
        machine.answers("gpu_live", None);
        sampler.step(&*door);
        assert_eq!(sampler.status()["last"].to_string(), r#"{"ts":1000.0,"ok":false,"core":0,"mem":0,"temp":0,"power":0.0,"util":0,"memutil":0,"fan":0}"#);
    }

    #[test]
    fn the_chart_and_the_score_are_of_the_last_seconds_asked_for() {
        let (machine, door, sampler) = a_machine();
        sampler.start(&door, "test".into());
        // 400 samples, a fifth of a second apart: 300 are kept.
        for round in 0..400 {
            machine.at(1000.0 + round as f64 * 0.2);
            machine.answers("gpu_live", Some(reading(60 + (round % 3), 90 + (round % 5), 2100 + (round % 2) * 10)));
            sampler.step(&*door);
        }
        let now = 1000.0 + 399.0 * 0.2;
        machine.at(now);
        assert_eq!(sampler.status()["sample_count"].as_int(), Some(300));
        let get = |target: &str| call(&sampler, &door, "GET", target, "").unwrap();
        let chart = get("/api/perf/samples");
        assert_eq!((chart["count"].as_int(), chart["window_s"].as_int()), (Some(300), Some(60)));
        for list in ["ts", "core", "mem", "temp", "power", "util", "fan"] {
            assert_eq!(chart[list].as_array().map(<[Json]>::len), Some(300), "{list}");
        }
        assert_eq!((&chart["ts"].as_array().unwrap()[299], &chart["core"].as_array().unwrap()[299]), (&Json::Float(now), &Json::Int(2110)));
        let chart = get("/api/perf/samples?seconds=2");
        assert_eq!((chart["count"].as_int(), chart["window_s"].as_int()), (Some(11), Some(2)), "a sample that is exactly that old is one of them");
        assert_eq!(get("/api/perf/samples?seconds=0")["count"].as_int(), Some(1));
        assert_eq!(get("/api/perf/samples?seconds=-1").to_string(), r#"{"ts":[],"core":[],"mem":[],"temp":[],"power":[],"util":[],"fan":[],"count":0,"window_s":-1}"#);
        assert_eq!(get("/api/perf/samples?seconds=abc")["window_s"].as_int(), Some(60));

        // The score: a steady card.
        let score = get("/api/perf/score");
        assert_eq!((score["verdict"].as_str(), score["window_s"].as_int(), score["samples_used"].as_int()), (Some("healthy"), Some(30), Some(151)));
        assert_eq!(score["subs"].to_string(),
                   r#"{"clock_score":39.0,"temp_score":27.6,"util_score":28.6,"clock_std":5.0,"temp_range":2,"util_std":1.4,"active_pct":100.0}"#);
        assert_eq!((&score["score"], &score["avg_util"], score["max_util"].as_int(), score["idle_threshold"].as_int()),
                   (&Json::Float(95.2), &Json::Float(92.0), Some(94), Some(15)));
        // Too few samples, and a card that does nothing: no score, and why.
        let few = get("/api/perf/score?seconds=1");
        assert_eq!(few.to_string(), "{\"score\":null,\"verdict\":\"no-data\",\"reason\":\"need \\u226510 samples, have 6\",\"subs\":{},\"samples_used\":6,\
                                     \"last_util\":94,\"last_temp\":60,\"avg_util\":92.3,\"max_util\":94,\"idle_threshold\":15}");
        machine.answers("gpu_live", Some(reading(40, 3, 300)));
        for round in 400..560 {
            machine.at(1000.0 + round as f64 * 0.2);
            sampler.step(&*door);
        }
        machine.at(1000.0 + 559.0 * 0.2);
        let idle = get("/api/perf/score");
        assert_eq!((idle["verdict"].as_str(), idle["reason"].as_str(), idle["samples_used"].as_int(), &idle["score"]),
                   (Some("idle"), Some("GPU idle (max util in window: 3%, need \u{2265}15%)"), Some(151), &Json::Null));
        // A card whose clock and use jump about, and that warms up by 30 degrees.
        for round in 560..720 {
            machine.at(1000.0 + round as f64 * 0.2);
            machine.answers("gpu_live", Some(reading(50 + (round - 560) / 5, if round % 2 == 0 { 99 } else { 20 }, 1500 + (round % 4) * 250)));
            sampler.step(&*door);
        }
        machine.at(1000.0 + 719.0 * 0.2);
        let shaky = get("/api/perf/score");
        assert_eq!((shaky["verdict"].as_str(), &shaky["score"]), (Some("unstable"), &Json::Float(0.0)), "{shaky}");
    }

    #[test]
    fn the_score_at_its_edges() {
        // Twelve samples, each (temperature, use, clock), and the score of them.
        let scored = |cards: Vec<(i128, i128, i128)>| {
            let (machine, door, sampler) = a_machine();
            sampler.start(&door, "edges".into());
            for (round, (temp, util, core)) in cards.iter().enumerate() {
                machine.at(100.0 + round as f64 * 0.25);
                machine.answers("gpu_live", Some(reading(*temp, *util, *core)));
                sampler.step(&*door);
            }
            sampler.score(100.0 + cards.len() as f64 * 0.25, 60)
        };
        let twelve = |of: fn(i128) -> (i128, i128, i128)| (0..12).map(of).collect::<Vec<_>>();
        let turn = |round: i128, one: i128, other: i128| if round % 2 == 0 { one } else { other };
        // Exactly 85 is healthy (all of the clock's and the temperature's
        // points, and half of the points for staying busy).
        let healthy = scored(twelve(|round| (60, if round % 2 == 0 { 85 } else { 55 }, 2100)));
        assert_eq!((&healthy["score"], healthy["verdict"].as_str()), (&Json::Float(85.0), Some("healthy")), "{healthy}");
        // Exactly 65 is a warning, not unstable (none of the temperature's
        // points: 25 degrees between the coolest and the warmest).
        let warn = scored((0..12).map(|round| (turn(round, 50, 75), turn(round, 90, 80), 2100)).collect());
        assert_eq!((&warn["score"], warn["verdict"].as_str()), (&Json::Float(65.0), Some("warn")), "{warn}");
        assert_eq!(warn["subs"].to_string(), r#"{"clock_score":40.0,"temp_score":0.0,"util_score":25.0,"clock_std":0.0,"temp_range":25,"util_std":5.0,"active_pct":100.0}"#);
        // A card that never says its temperature loses no points for it.
        let no_temperature = scored(twelve(|_| (0, 99, 2100)));
        assert_eq!((&no_temperature["score"], &no_temperature["subs"]["temp_score"], no_temperature["subs"]["temp_range"].as_int()),
                   (&Json::Float(100.0), &Json::Float(30.0), Some(0)));
        // Use of exactly 15 is work, 14 is not; ten samples are enough, nine are not.
        assert_eq!(scored(twelve(|_| (60, 15, 2100)))["verdict"].as_str(), Some("healthy"));
        assert_eq!(scored(twelve(|_| (60, 14, 2100)))["verdict"].as_str(), Some("idle"));
        assert_eq!(scored((0..10).map(|_| (60, 99, 2100)).collect())["verdict"].as_str(), Some("healthy"));
        assert_eq!(scored((0..9).map(|_| (60, 99, 2100)).collect())["verdict"].as_str(), Some("no-data"));
        // Points are never less than nothing: a clock all over the place,
        // use that jumps from 15 to 100, and 40 degrees between.
        let shaky = scored((0..12).map(|round| (turn(round, 40, 80), turn(round, 15, 100), turn(round, 300, 2900))).collect());
        assert_eq!((&shaky["score"], shaky["verdict"].as_str()), (&Json::Float(0.0), Some("unstable")), "{shaky}");
    }

    #[test]
    fn the_spread_is_pythons() {
        // (statistics.pstdev of the same numbers, to the last place Python shows)
        assert_eq!(spread(&[]), 0.0);
        assert_eq!(spread(&[2100]), 0.0);
        assert_eq!(spread(&[2100, 2100, 2100]), 0.0);
        assert_eq!(spread(&[2100, 2110]), 5.0);
        assert_eq!(spread(&[90, 91, 92, 93, 94]), 1.4142135623730951);
        assert_eq!(spread(&[1, 2, 3, 4, 100]), 39.01281840626232);
        assert_eq!(spread(&[2000, 2150, 1990, 2100, 2100, 2075, 1800]), 107.98242487383145);
    }

    #[test]
    fn with_threads_it_samples_by_itself_until_it_is_told_to_stop() {
        let machine = Fake::new();
        machine.answers("gpu_live", Some(reading(61, 97, 2100)));
        machine.at(1000.0);
        let door: Arc<dyn Readings> = machine.clone();
        let sampler = Sampler::new(true);
        assert!(sampler.answer(&door, &Request::new("POST", "/api/perf/start").with_body("{}")).is_some());
        let waited = std::time::Instant::now();
        while sampler.status()["sample_count"].as_int() < Some(3) && waited.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(20));
        }
        let took = waited.elapsed();
        assert!(sampler.status()["sample_count"].as_int() >= Some(3), "no samples after {took:?}");
        assert!(took >= Duration::from_millis(300), "three samples in {took:?}: it does not pause between them");
        // Told to stop: it ends at once, not after its pause, and takes no more.
        sampler.stop("manual".into(), false);
        let told = std::time::Instant::now();
        while sampler.is_running() && told.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(2));
        }
        assert!(!sampler.is_running() && told.elapsed() < Duration::from_millis(150), "it took {:?} to stop", told.elapsed());
        let taken = sampler.status()["sample_count"].as_int();
        std::thread::sleep(Duration::from_millis(450));
        assert_eq!(sampler.status()["sample_count"].as_int(), taken);
    }
}
