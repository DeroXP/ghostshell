//! The graphics card, for everything that has moved and reads it.
//!
//! Reading the card means starting NVIDIA's library (nvml.dll) in the
//! reading process, and a library that was started stays in its process
//! until the process ends (tools/measure/nvml_release.py).  A driver's
//! installer has to replace that file.  So the shell's own process never
//! opens it (`Real`, in mod.rs, refuses every question about the card), and
//! the card is read in one of two other places:
//!
//! * **The reader**: a small program of the shell's own (VisporaGpuReader,
//!   src/bin/gpu_reader.rs) that holds the library and does nothing but
//!   answer for the card.  The shell starts it when a reading is wanted,
//!   and ends it:
//!   - when nobody asked for a reading for `IDLE_S`;
//!   - before the call that starts a driver's installer is passed on to
//!     the backend, and then it stays away for as long as the backend
//!     stays away from its own copy of the library (`HOLD_FOR_AN_INSTALL_S`),
//!     unless the backend says that no installer was started;
//!   - when it gives no reading: it does not answer within its limit, its
//!     answer cannot be read, or it says that it has none (no NVIDIA card,
//!     a driver that stopped answering).  It is then tried again after a
//!     pause, `PAUSE_S` at first and twice as long each time in a row, up
//!     to `LONGEST_PAUSE_S`;
//!   - when the shell quits.
//! * **The backend**, wherever the reader is not asked or gave nothing: its
//!   call for the card's reading, which is `gpu_overclock.read_live_state()`
//!   and nothing else.  That is the reading the Python modules take
//!   themselves (its own copy of the helper, or nvidia-smi), so a module
//!   that lives on it gives Python's answers.
//!
//! A reading from the reader is the helper's answer, read the way Python
//! reads it (core/native.py, gpu_live; core/gpu_overclock.py,
//! _live_state_from_native).  So whichever of the two a reading comes from,
//! it is the reading Python would have taken.
//!
//! How long a reader has been idle, how long it has to stay away, and how
//! long a pause lasts is counted on a clock that does not jump (the door's
//! `steady`), as the helper counts its own hold: the PC's own clock can be
//! set forward or back in the middle of a driver's install.
//!
//! Nothing here starts a program or calls Windows: a reader is reached
//! through `Readers`, which has a real side (src/gpu_reader.rs) and a
//! made-up one (`FakeReaders`).

use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use super::{py_round, Readings};
use crate::http::Response;
use crate::json::Json;

/// A way to ask the backend something with GET: its answer, or None where
/// there is no backend to ask or it did not answer "200" in time.
pub type AskBackend = Arc<dyn Fn(&str) -> Option<Json> + Send + Sync>;
/// A way to say something in the shell's log.
pub type Say = Arc<dyn Fn(&str) + Send + Sync>;

/// The backend's call that gives the card's reading as the app reads it
/// (app.py, api_gpu_oc_live: `gpu_overclock.read_live_state()` and nothing
/// else).  It is a call of the GPU overclock page, and it only reads: none
/// of the overclock's own code has moved or is called from here.
pub const THE_BACKENDS_READING: &str = "/api/gpu/oc/live";

/// How long the reader stays away once a driver's installer was started:
/// as long as the backend stays away from its own copy of the library
/// (core/driver_updater.py, _GPU_LIBRARY_HOLD_S).
pub const HOLD_FOR_AN_INSTALL_S: f64 = 900.0;
/// Nobody asked for a reading for this long: the reader is ended.  Longer
/// than the gap between two readings of anything that reads the card
/// regularly (once a second at the slowest), so that a reader is not ended
/// and started between two of them; short enough that the driver's library
/// is held for seconds, not minutes, after the last one who wanted it.
pub const IDLE_S: f64 = 10.0;
/// A reader's first answer has to be there within this: its program is
/// started and the driver's library loaded for it (some tens of
/// milliseconds on a warm disk).
pub const FIRST_ANSWER_WITHIN: Duration = Duration::from_secs(3);
/// Every later answer within this (one takes about a tenth of a
/// millisecond).
pub const ANSWER_WITHIN: Duration = Duration::from_secs(1);
/// A reader that gave no reading is tried again after this long, as the
/// helper itself waits after a driver stopped answering.
pub const PAUSE_S: f64 = 30.0;
/// ... and after twice as long each time in a row, up to this: on a PC
/// without an NVIDIA card a reader is started and ended now and then, not
/// twice a minute for ever.
pub const LONGEST_PAUSE_S: f64 = 480.0;

/// What the helper says about the card, each a whole number or nothing.
/// (core/native.py, _GPU_FIELDS: an answer is taken only if every one of
/// them is.)
const GPU_FIELDS: [&str; 14] = ["temp_c", "gpu_util_pct", "mem_util_pct", "core_mhz", "mem_mhz", "core_max_mhz", "mem_max_mhz", "core_app_mhz",
                                "mem_app_mhz", "power_mw", "power_limit_mw", "fan_pct", "mem_used_bytes", "mem_total_bytes"];

/// What the card says, in the app's own form: what
/// `gpu_overclock.read_live_state()` gives, as far as what has moved uses it.
#[derive(Debug, Clone, PartialEq)]
pub struct Live {
    pub core_mhz: i128,
    pub mem_mhz: i128,
    pub temp_c: i128,
    pub power_w: f64,
    pub gpu_util_pct: i128,
    pub mem_util_pct: i128,
    pub fan_pct: i128,
}

/// A reader that is running.
pub trait Reader: Send + Sync {
    /// What the helper says of the card now, in its own form: its answer
    /// (None where it says that it has none), or why there is no answer.
    /// Comes back within `within`, whatever the reader does.
    fn read(&self, within: Duration) -> Result<Option<Json>, String>;
    /// End it now.  True when it is seen to be gone.  It may be asked while
    /// a reading is being waited for, which then comes back without one.
    fn end(&self) -> bool;
    /// Its process, for the log.
    fn pid(&self) -> u32;
}

/// The way to start a reader.
pub trait Readers: Send + Sync {
    fn start(&self) -> Result<Arc<dyn Reader>, String>;
}

/// Where the readings came from so far.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct From {
    /// From the card's own reader.
    pub reader: u64,
    /// From the backend.
    pub backend: u64,
    /// From neither: there was no reading.
    pub nowhere: u64,
}

struct State {
    from: From,
    backend: Option<AskBackend>,
    readers: Option<Arc<dyn Readers>>,
    say: Option<Say>,
    /// The reader that is running.
    reader: Option<Arc<dyn Reader>>,
    /// It has answered once (which limit its next answer has).
    answered: bool,
    /// Calls for a driver's install that are on their way to the backend
    /// and not answered yet: no reader while there is one.
    installs: u32,
    /// A driver's installer was started: no reader until then (on the
    /// clock that does not jump, as the next two).
    away_until: Option<f64>,
    /// A reader gave no reading: none is started until then.
    pause_until: Option<f64>,
    /// How long the next such pause is.
    next_pause: f64,
    /// When a reading was last asked for.
    last_wanted: f64,
    /// A thread is looking after the reader (`tend`).
    watched: bool,
    /// The shell quits: no reader any more.
    closed: bool,
}

struct Inner {
    door: Arc<dyn Readings>,
    /// May a thread look after the reader?  (Not where a test moves the
    /// clock by hand and calls `tend` itself.)
    threads: bool,
    state: Mutex<State>,
}

/// The card.
#[derive(Clone)]
pub struct Card {
    inner: Arc<Inner>,
}

impl Card {
    pub(super) fn new(door: Arc<dyn Readings>, threads: bool) -> Card {
        let state = State { from: From::default(), backend: None, readers: None, say: None, reader: None, answered: false, installs: 0, away_until: None, pause_until: None,
                            next_pause: PAUSE_S, last_wanted: 0.0, watched: false, closed: false };
        Card { inner: Arc::new(Inner { door, threads, state: Mutex::new(state) }) }
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.inner.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The time, on the PC's own clock: for what a reading is stamped with.
    pub fn now(&self) -> f64 {
        self.inner.door.now()
    }

    /// Seconds on the clock that does not jump: for everything here that
    /// lasts a while.
    fn steady(&self) -> f64 {
        self.inner.door.steady()
    }

    /// How the backend is asked.
    pub fn asks_the_backend_with(&self, ask: AskBackend) {
        self.state().backend = Some(ask);
    }

    /// How a reader is started.  Without one the card's reading is the
    /// backend's at all times.
    pub fn reads_with(&self, readers: Arc<dyn Readers>) {
        self.state().readers = Some(readers);
    }

    /// Where to say what becomes of a reader.
    pub fn says_with(&self, say: Say) {
        self.state().say = Some(say);
    }

    fn say(&self, line: &str) {
        let say = self.state().say.clone();
        if let Some(say) = say {
            say(line);
        }
    }

    /// Where the readings came from so far.
    pub fn from(&self) -> From {
        self.state().from
    }

    /// The running reader's process, if one runs.
    pub fn reader_pid(&self) -> Option<u32> {
        self.state().reader.as_ref().map(|reader| reader.pid())
    }

    /// What the card says now.  The reader is asked where it may be; the
    /// backend where it may not, or gave nothing.  None where neither has
    /// a reading.
    pub fn live(&self) -> Option<Live> {
        let now = self.steady();
        if let Some((reader, within)) = self.the_reader(now) {
            let read = match reader.read(within) {
                Ok(Some(said)) => taken(&said).ok_or_else(|| "its answer is not a reading".to_string()),
                Ok(None) => Err("it has no reading of a card".to_string()),
                Err(why) => Err(why),
            };
            match read {
                Ok(live) => {
                    let mut st = self.state();
                    if st.reader.as_ref().is_some_and(|running| Arc::ptr_eq(running, &reader)) {
                        st.answered = true;
                        st.next_pause = PAUSE_S;
                    }
                    st.from.reader += 1;
                    return Some(live);
                }
                Err(why) => self.gave_nothing(&reader, now, &why),
            }
        }
        let live = self.the_backends();
        let mut st = self.state();
        match live {
            Some(_) => st.from.backend += 1,
            None => st.from.nowhere += 1,
        }
        live
    }

    /// The reader to ask now, started if need be, and how long its answer
    /// may take.  None where no reader may be asked.
    fn the_reader(&self, now: f64) -> Option<(Arc<dyn Reader>, Duration)> {
        let mut said = None;
        let found = {
            let mut st = self.state();
            st.last_wanted = now;
            let away = st.installs > 0 || st.away_until.is_some_and(|until| now < until);
            if st.closed || away || st.pause_until.is_some_and(|until| now < until) {
                return None;
            }
            (st.away_until, st.pause_until) = (None, None);
            if st.reader.is_none() {
                // (started with the lock held: one start, whoever asks)
                match st.readers.clone()?.start() {
                    Ok(reader) => {
                        said = Some(format!("the graphics card's reader was started (pid {})", reader.pid()));
                        st.reader = Some(reader);
                        st.answered = false;
                        if self.inner.threads && !st.watched {
                            let card = self.clone();
                            st.watched = std::thread::Builder::new().name("gpu-reader-idle".to_string()).spawn(move || card.look_after()).is_ok();
                        }
                    }
                    Err(why) => {
                        let pause = st.pause();
                        st.pause_until = Some(now + pause);
                        said = Some(format!("the graphics card's reader could not be started ({why}): the backend is asked for the card, and it is tried again in {pause:.0} s"));
                    }
                }
            }
            let within = if st.answered { ANSWER_WITHIN } else { FIRST_ANSWER_WITHIN };
            st.reader.clone().map(|reader| (reader, within))
        };
        if let Some(line) = said {
            self.say(&line);
        }
        found
    }

    /// The reader gave no reading: it is ended, and tried again later.
    fn gave_nothing(&self, reader: &Arc<dyn Reader>, now: f64, why: &str) {
        let pause = {
            let mut st = self.state();
            // (not if it was taken away meanwhile: for an install, at the quit, or as idle)
            if st.reader.as_ref().is_some_and(|running| Arc::ptr_eq(running, reader)) {
                st.reader = None;
                let pause = st.pause();
                st.pause_until = Some(now + pause);
                Some(pause)
            } else {
                None
            }
        };
        let gone = reader.end();
        if let Some(pause) = pause {
            self.say(&format!("the graphics card's reader gave no reading ({why}): it was ended{}, the backend is asked for the card, and it is tried again in {pause:.0} s",
                              if gone { "" } else { " and was not seen to go" }));
        }
    }

    /// The thread that looks after a running reader.
    fn look_after(&self) {
        loop {
            std::thread::sleep(Duration::from_secs(1));
            if !self.tend() {
                break;
            }
        }
    }

    /// End the reader if nobody asked for a reading for `IDLE_S`.  Is there
    /// still a reader to look after?  (Called once a second by the thread
    /// that looks after it; by hand where there is no such thread.)
    pub fn tend(&self) -> bool {
        let now = self.steady();
        let idle = {
            let mut st = self.state();
            if st.reader.is_some() && now - st.last_wanted < IDLE_S {
                return true;
            }
            st.watched = false;
            st.reader.take()
        };
        if let Some(reader) = idle {
            let gone = reader.end();
            self.say(&format!("the graphics card's reader was ended (pid {}): nobody asked for a reading for {IDLE_S:.0} s{}", reader.pid(),
                              if gone { "" } else { "; it was not seen to go" }));
        }
        false
    }

    /// Around the call that starts a graphics driver's installer
    /// (`pass_on` hands it to the backend).  The reader is ended, and seen
    /// to be gone, before the backend hears of the call, and none is
    /// started while the call is on its way: however many such calls are
    /// on their way at once.  If the backend then says that an installer
    /// was started, no reader is started for `HOLD_FOR_AN_INSTALL_S` from
    /// its answer on; if it says anything else, this call leaves nothing
    /// behind, and a reader may be started at the next reading (unless an
    /// installer that was started before still has its time to run).
    pub fn around_a_driver_install(&self, pass_on: impl FnOnce() -> Response) -> Response {
        let reader = {
            let mut st = self.state();
            st.installs += 1;
            st.reader.take()
        };
        match &reader {
            Some(reader) if reader.end() => self.say(&format!("a graphics driver's installer may be about to start: the graphics card's reader was ended first (pid {}), \
                                                                and was seen to be gone", reader.pid())),
            Some(reader) => self.say(&format!("a graphics driver's installer may be about to start: the graphics card's reader was ended first (pid {}), \
                                               and was NOT seen to go", reader.pid())),
            None => {}
        }
        let answer = pass_on();
        // (the backend lets go of its own copy only once it knows that it
        // starts the installer, and says so with "ok")
        let started = answer.status == 200 && answer.to_json().get("ok").is_some_and(Json::truthy);
        {
            // (counted from the answer: the backend let go before it answered)
            let until = self.steady() + HOLD_FOR_AN_INSTALL_S;
            let mut st = self.state();
            st.installs -= 1;
            if started {
                // (later than the time of any installer that was started before)
                st.away_until = Some(until);
            }
        }
        if started {
            self.say(&format!("a graphics driver's installer was started: no reader for the graphics card for {HOLD_FOR_AN_INSTALL_S:.0} s, the backend is asked meanwhile"));
        } else if reader.is_some() {
            self.say("no graphics driver's installer was started: the graphics card's reader may be started again");
        }
        answer
    }

    /// The shell quits: the reader is ended, and none is started any more.
    pub fn close(&self) {
        let reader = {
            let mut st = self.state();
            st.closed = true;
            st.reader.take()
        };
        if let Some(reader) = reader {
            let gone = reader.end();
            self.say(&format!("the graphics card's reader was ended (pid {}): the app quits{}", reader.pid(), if gone { "" } else { "; it was not seen to go" }));
        }
    }

    /// The backend's reading.  None where there is no backend to ask (it is
    /// still starting, or gone), and where it has no reading either (its
    /// answer says so: no NVIDIA card, no driver, no nvidia-smi).
    fn the_backends(&self) -> Option<Live> {
        let ask = self.state().backend.clone()?;
        let said = ask(THE_BACKENDS_READING)?;
        if !said.get("ok").is_some_and(Json::truthy) {
            return None;
        }
        // Each value as the Python modules read it off that answer:
        // `int(s.get(name) or 0)`, and `float(s.get(name) or 0.0)` for the watts.
        let number = |name: &str| match said.get(name) {
            Some(Json::Int(whole)) => *whole as f64,
            Some(Json::Float(number)) if number.is_finite() => *number,
            Some(Json::Bool(true)) => 1.0,
            _ => 0.0,
        };
        let whole = |name: &str| match said.get(name) {
            Some(Json::Int(whole)) => *whole,
            _ => number(name).trunc() as i128,
        };
        Some(Live { core_mhz: whole("core_mhz"), mem_mhz: whole("mem_mhz"), temp_c: whole("temp_c"), power_w: number("power_w"),
                    gpu_util_pct: whole("gpu_util_pct"), mem_util_pct: whole("mem_util_pct"), fan_pct: whole("fan_pct") })
    }
}

impl State {
    /// How long the pause is that begins now; the next one in a row is
    /// twice as long.
    fn pause(&mut self) -> f64 {
        let pause = self.next_pause;
        self.next_pause = (pause * 2.0).min(LONGEST_PAUSE_S);
        pause
    }
}

/// The helper's answer about the card, as the app takes it (core/native.py,
/// gpu_live, and core/gpu_overclock.py, _live_state_from_native): every
/// value a whole number or nothing, and never without the temperature.
/// None for an answer that Python does not take (it then asks nvidia-smi).
fn taken(said: &Json) -> Option<Live> {
    // (an answer that is no object has none of the values, and so no temperature)
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
    let value = |name: &str| values.iter().find(|(known, _)| *known == name).and_then(|(_, value)| *value);
    let whole = |name: &str| value(name).unwrap_or(0);
    value("temp_c")?;
    // (the library counts milliwatts; the app shows watts with two places)
    let watts = value("power_mw").map_or(0.0, |milliwatts| py_round(milliwatts as f64 / 1000.0, 2));
    Some(Live { core_mhz: whole("core_mhz"), mem_mhz: whole("mem_mhz"), temp_c: whole("temp_c"), power_w: watts, gpu_util_pct: whole("gpu_util_pct"),
                mem_util_pct: whole("mem_util_pct"), fan_pct: whole("fan_pct") })
}

// ═════════════════════════════════════════════════════════════════════
// The made-up sides
// ═════════════════════════════════════════════════════════════════════

/// A made-up backend: asked, it says what it was told to say, whatever
/// it is asked for, and keeps what it was asked.
#[derive(Default)]
pub struct FakeBackend {
    says: Mutex<Option<Json>>,
    asked: Mutex<Vec<String>>,
}

impl FakeBackend {
    pub fn new() -> Arc<FakeBackend> {
        Arc::new(FakeBackend::default())
    }

    /// From now on it says this (None: it does not answer).
    pub fn says(&self, answer: Option<Json>) {
        *self.says.lock().unwrap_or_else(|p| p.into_inner()) = answer;
    }

    /// What it was asked so far, in order.
    pub fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// The way to ask it (`Card::asks_the_backend_with`).
    pub fn asker(self: &Arc<Self>) -> AskBackend {
        let backend = self.clone();
        Arc::new(move |path: &str| {
            backend.asked.lock().unwrap_or_else(|p| p.into_inner()).push(path.to_string());
            backend.says.lock().unwrap_or_else(|p| p.into_inner()).clone()
        })
    }
}

/// What a made-up reader does when it is asked.
#[derive(Debug, Clone, PartialEq)]
pub enum FakeReading {
    /// The helper's answer about the card.
    Says(Json),
    /// The helper says that it has no reading.
    HasNone,
    /// It does not answer in time.
    IsSilent,
    /// What it answers cannot be read.
    Garbles,
}

#[derive(Default)]
struct FakeReadersState {
    reading: Option<FakeReading>,
    cannot_start: Option<String>,
    will_not_go: bool,
    started: u32,
    /// The ones that run, by their made-up process numbers.
    running: Vec<u32>,
    /// What happened, in order: "start 1", "ask 1", "end 1", ...
    happened: Vec<String>,
    asked: u32,
    gave: u32,
}

/// Made-up readers: each "program" that is started is a number, and says
/// what all of them are told to say.
#[derive(Default)]
pub struct FakeReaders {
    state: Mutex<FakeReadersState>,
}

struct FakeReader {
    of: Arc<FakeReaders>,
    pid: u32,
}

impl FakeReaders {
    pub fn new() -> Arc<FakeReaders> {
        Arc::new(FakeReaders::default())
    }

    fn state(&self) -> MutexGuard<'_, FakeReadersState> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// From now on a reader does this when it is asked (at first: it has
    /// no reading).
    pub fn reads(&self, reading: FakeReading) {
        self.state().reading = Some(reading);
    }

    /// From now on no reader can be started, for this reason (None: one can).
    pub fn cannot_start(&self, why: Option<&str>) {
        self.state().cannot_start = why.map(str::to_string);
    }

    /// From now on a reader that is ended is not seen to go.
    pub fn will_not_go(&self, stays: bool) {
        self.state().will_not_go = stays;
    }

    /// The readers that run.
    pub fn running(&self) -> Vec<u32> {
        self.state().running.clone()
    }

    /// How many were started so far.
    pub fn started(&self) -> u32 {
        self.state().started
    }

    /// How many times a reader was asked, and how many of those gave the
    /// helper's answer.
    pub fn asked(&self) -> (u32, u32) {
        let st = self.state();
        (st.asked, st.gave)
    }

    /// Everything that happened, in order.
    pub fn happened(&self) -> Vec<String> {
        self.state().happened.clone()
    }
}

impl Readers for Arc<FakeReaders> {
    fn start(&self) -> Result<Arc<dyn Reader>, String> {
        let mut st = self.state();
        if let Some(why) = st.cannot_start.clone() {
            st.happened.push("no start".to_string());
            return Err(why);
        }
        st.started += 1;
        let pid = st.started;
        st.running.push(pid);
        st.happened.push(format!("start {pid}"));
        Ok(Arc::new(FakeReader { of: self.clone(), pid }))
    }
}

impl Reader for FakeReader {
    fn read(&self, within: Duration) -> Result<Option<Json>, String> {
        let mut st = self.of.state();
        st.happened.push(format!("ask {}", self.pid));
        st.asked += 1;
        if !st.running.contains(&self.pid) {
            return Err("it has ended".to_string());
        }
        match st.reading.clone().unwrap_or(FakeReading::HasNone) {
            FakeReading::Says(said) => {
                st.gave += 1;
                Ok(Some(said))
            }
            FakeReading::HasNone => Ok(None),
            FakeReading::IsSilent => Err(format!("no answer within {} ms", within.as_millis())),
            FakeReading::Garbles => Err("its answer could not be read".to_string()),
        }
    }

    fn end(&self) -> bool {
        let mut st = self.of.state();
        if st.running.contains(&self.pid) {
            st.happened.push(format!("end {}", self.pid));
        }
        if st.will_not_go {
            return false;
        }
        st.running.retain(|pid| *pid != self.pid);
        true
    }

    fn pid(&self) -> u32 {
        self.pid
    }
}

#[cfg(test)]
mod tests {
    use super::super::{Fake, Moved};
    use super::*;

    /// What the helper says about a card, and that reading as the app has it.
    fn helper(temp: i128) -> Json {
        Json::object([("temp_c", Json::Int(temp)), ("gpu_util_pct", 97.into()), ("mem_util_pct", 40.into()), ("core_mhz", 2100.into()), ("mem_mhz", 9501.into()),
                      ("power_mw", 215_437.into()), ("fan_pct", 55.into())])
    }

    fn of_the_reader(temp: i128) -> Option<Live> {
        Some(Live { core_mhz: 2100, mem_mhz: 9501, temp_c: temp, power_w: 215.44, gpu_util_pct: 97, mem_util_pct: 40, fan_pct: 55 })
    }

    /// What the backend says the card says (always 71 degrees), and that reading.
    fn backend_says() -> Json {
        Json::parse(r#"{"ok": true, "core_mhz": 1800, "mem_mhz": 7000, "temp_c": 71, "power_w": 123.45, "gpu_util_pct": 88, "mem_util_pct": 33, "fan_pct": 60}"#).unwrap()
    }

    fn of_the_backend() -> Option<Live> {
        Some(Live { core_mhz: 1800, mem_mhz: 7000, temp_c: 71, power_w: 123.45, gpu_util_pct: 88, mem_util_pct: 33, fan_pct: 60 })
    }

    struct Rig {
        machine: Arc<Fake>,
        backend: Arc<FakeBackend>,
        readers: Arc<FakeReaders>,
        card: Card,
        said: Arc<Mutex<Vec<String>>>,
    }

    impl Rig {
        /// A made-up machine at 1000 seconds, a backend with a reading, and
        /// readers that give the helper's reading of 61 degrees.
        fn new() -> Rig {
            let (machine, backend, readers) = (Fake::new(), FakeBackend::new(), FakeReaders::new());
            machine.at(1000.0);
            // (the shell's own library would say something else of the card: it is not asked)
            machine.answers("gpu_live", Some(helper(13)));
            backend.says(Some(backend_says()));
            readers.reads(FakeReading::Says(helper(61)));
            let moved = Moved::new(machine.clone(), false);
            let card = (*moved.card).clone();
            card.asks_the_backend_with(backend.asker());
            card.reads_with(Arc::new(readers.clone()));
            let said = Arc::new(Mutex::new(Vec::new()));
            let lines = said.clone();
            card.says_with(Arc::new(move |line: &str| lines.lock().unwrap().push(line.to_string())));
            Rig { machine, backend, readers, card, said }
        }

        fn said(&self) -> String {
            self.said.lock().unwrap().join("\n")
        }

        fn last_said(&self) -> String {
            self.said.lock().unwrap().last().cloned().unwrap_or_default()
        }
    }

    impl Drop for Rig {
        fn drop(&mut self) {
            if !std::thread::panicking() {
                assert!(self.machine.asked().is_empty(), "the card was asked through the shell's own door: {:?}", self.machine.asked());
            }
        }
    }

    #[test]
    fn a_reading_is_the_readers_and_the_reader_is_started_when_one_is_wanted() {
        let rig = Rig::new();
        assert!(rig.readers.running().is_empty() && rig.readers.started() == 0, "a reader runs before anybody wants a reading");
        assert_eq!(rig.card.reader_pid(), None);
        assert_eq!(rig.card.live(), of_the_reader(61));
        assert_eq!((rig.readers.running(), rig.card.reader_pid()), (vec![1], Some(1)));
        assert_eq!(rig.said(), "the graphics card's reader was started (pid 1)");
        // The same reader for every reading after it; the backend is not asked.
        rig.readers.reads(FakeReading::Says(helper(62)));
        for _ in 0..5 {
            rig.machine.at(rig.machine.now() + 0.2);
            assert_eq!(rig.card.live(), of_the_reader(62));
        }
        assert_eq!((rig.readers.started(), rig.readers.asked(), rig.backend.asked().len()), (1, (6, 6), 0));
        assert_eq!(rig.card.from(), From { reader: 6, backend: 0, nowhere: 0 });
        // ... the backend where the reader gives nothing, and neither where neither has one.
        rig.readers.reads(FakeReading::HasNone);
        rig.card.live();
        rig.backend.says(None);
        rig.card.live();
        assert_eq!(rig.card.from(), From { reader: 6, backend: 1, nowhere: 1 });
    }

    #[test]
    fn the_helpers_answer_is_read_as_python_reads_it() {
        // What the card does not say is 0, a number with a fraction is cut,
        // and watts are milliwatts with two places, rounded as Python rounds.
        let whole = |pairs: &str| taken(&Json::parse(pairs).unwrap());
        assert_eq!(whole(r#"{"temp_c": 53.9, "fan_pct": null, "power_mw": 1}"#),
                   Some(Live { core_mhz: 0, mem_mhz: 0, temp_c: 53, power_w: 0.0, gpu_util_pct: 0, mem_util_pct: 0, fan_pct: 0 }));
        for (milliwatts, watts) in [(215_435, 215.44), (2675, 2.67), (1005, 1.0), (15, 0.01), (5, 0.01), (4, 0.0), (999_999_995, 999_999.99)] {
            assert_eq!(whole(&format!(r#"{{"temp_c": 60, "power_mw": {milliwatts}}}"#)).unwrap().power_w, watts, "{milliwatts}");
        }
        assert_eq!(whole(r#"{"temp_c": 0}"#).unwrap().temp_c, 0);
        assert_eq!(whole(r#"{"temp_c": -5, "core_mhz": -1, "extra": "ignored"}"#).map(|live| (live.temp_c, live.core_mhz)), Some((-5, -1)));
        // Not taken: no temperature, a value that is no number (also one
        // that is not used), an answer that is no object.
        for not_one in [r#"{"gpu_util_pct": 97}"#, r#"{"temp_c": null, "core_mhz": 2100}"#, r#"{"temp_c": 61, "mem_total_bytes": "a lot"}"#, r#"{"temp_c": true}"#,
                        r#"{"temp_c": "61"}"#, r#"{"temp_c": 61, "fan_pct": [55]}"#, r#"{"temp_c": 61, "gpu_util_pct": false}"#, "[1, 2]", r#""warm""#, "61", "null"] {
            assert_eq!(whole(not_one), None, "{not_one}");
        }
        assert_eq!(taken(&Json::object([("temp_c", f64::NAN.into())])), None);
    }

    #[test]
    fn where_the_reader_gives_nothing_the_reading_is_the_backends_and_the_reader_is_ended() {
        for (nothing, why) in [(FakeReading::HasNone, "it has no reading of a card"), (FakeReading::IsSilent, "no answer within 3000 ms"),
                               (FakeReading::Garbles, "its answer could not be read"),
                               (FakeReading::Says(Json::object([("gpu_util_pct", 97.into())])), "its answer is not a reading")] {
            let rig = Rig::new();
            rig.readers.reads(nothing.clone());
            assert_eq!(rig.card.live(), of_the_backend(), "{nothing:?}");
            assert!(rig.readers.running().is_empty(), "{nothing:?}: the reader that gave nothing still runs");
            assert_eq!(rig.readers.happened(), ["start 1", "ask 1", "end 1"]);
            assert_eq!(rig.last_said(), format!("the graphics card's reader gave no reading ({why}): it was ended, the backend is asked for the card, and it is tried again in 30 s"));
            assert_eq!(rig.backend.asked(), [THE_BACKENDS_READING]);
            // Until the pause is over no reader is started, whatever one
            // would say by now: the backend's reading it is.
            rig.readers.reads(FakeReading::Says(helper(61)));
            for later in [0.2, 5.0, 29.9] {
                rig.machine.at(1000.0 + later);
                assert_eq!(rig.card.live(), of_the_backend(), "{later} s later");
            }
            assert_eq!((rig.readers.started(), rig.backend.asked().len()), (1, 4));
            rig.machine.at(1030.0);
            assert_eq!(rig.card.live(), of_the_reader(61));
            assert_eq!((rig.readers.started(), rig.readers.running(), rig.backend.asked().len()), (2, vec![2], 4));
        }
    }

    #[test]
    fn a_later_answer_has_a_shorter_limit_than_the_first() {
        let rig = Rig::new();
        assert_eq!(rig.card.live(), of_the_reader(61));
        rig.readers.reads(FakeReading::IsSilent);
        rig.machine.at(1000.2);
        assert_eq!(rig.card.live(), of_the_backend());
        assert!(rig.last_said().contains("(no answer within 1000 ms)"), "{}", rig.last_said());
        // And the next reader's first answer has the long one again.
        rig.machine.at(1031.0);
        assert_eq!(rig.card.live(), of_the_backend());
        assert!(rig.last_said().contains("(no answer within 3000 ms)"), "{}", rig.last_said());
    }

    #[test]
    fn the_pause_grows_while_readers_keep_giving_nothing_and_begins_anew_after_a_reading() {
        let rig = Rig::new();
        rig.readers.reads(FakeReading::HasNone);
        let mut at = 1000.0;
        for pause in [30.0, 60.0, 120.0, 240.0, 480.0, 480.0, 480.0] {
            rig.machine.at(at);
            assert_eq!(rig.card.live(), of_the_backend());
            assert!(rig.last_said().ends_with(&format!("it is tried again in {pause:.0} s")), "{}", rig.last_said());
            let started = rig.readers.started();
            rig.machine.at(at + pause - 0.5);
            rig.card.live();
            assert_eq!(rig.readers.started(), started, "a reader was started {pause} s into a pause of that length, less half a second");
            at += pause;
        }
        assert_eq!(rig.readers.started(), 7);
        // One reading, and the next pause is the short one again.
        rig.readers.reads(FakeReading::Says(helper(61)));
        rig.machine.at(at);
        assert_eq!(rig.card.live(), of_the_reader(61));
        rig.readers.reads(FakeReading::HasNone);
        rig.machine.at(at + 1.0);
        assert_eq!(rig.card.live(), of_the_backend());
        assert!(rig.last_said().ends_with("it is tried again in 30 s"), "{}", rig.last_said());
    }

    #[test]
    fn a_reader_that_cannot_be_started_leaves_the_reading_to_the_backend() {
        let rig = Rig::new();
        rig.readers.cannot_start(Some("the program is not there"));
        assert_eq!(rig.card.live(), of_the_backend());
        assert_eq!(rig.said(), "the graphics card's reader could not be started (the program is not there): the backend is asked for the card, and it is tried again in 30 s");
        rig.machine.at(1029.0);
        assert_eq!(rig.card.live(), of_the_backend());
        assert_eq!(rig.readers.happened(), ["no start"], "it was tried again within the pause");
        rig.readers.cannot_start(None);
        rig.machine.at(1030.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
        // Without a way to start one at all: the backend's, and nothing said.
        let (machine, backend) = (Fake::new(), FakeBackend::new());
        backend.says(Some(backend_says()));
        let moved = Moved::new(machine.clone(), false);
        moved.asks_the_backend_with(backend.asker());
        assert_eq!(moved.card.live(), of_the_backend());
        // ... and without a backend either, none.
        backend.says(None);
        assert_eq!(moved.card.live(), None);
        assert!(machine.asked().is_empty());
    }

    #[test]
    fn where_neither_has_a_reading_there_is_none() {
        let rig = Rig::new();
        rig.readers.reads(FakeReading::HasNone);
        for none in [Some(r#"{"ok": false, "err": "No NVIDIA GPU / nvidia-smi missing", "temp_c": 99}"#), Some(r#"{"temp_c": 99}"#), Some(r#"{"ok": 0, "temp_c": 9}"#),
                     Some("[1, 2]"), None] {
            rig.backend.says(none.map(|text| Json::parse(text).unwrap()));
            assert_eq!(rig.card.live(), None, "{none:?}");
        }
        // The backend's reading as Python reads it: what is not there,
        // nothing and false are 0, and true is 1; a number with a fraction
        // is cut; whole watts are watts.
        rig.backend.says(Some(Json::parse(r#"{"ok": 1, "core_mhz": 1800.9, "temp_c": 70.0, "power_w": 100, "gpu_util_pct": null, "fan_pct": false, "mem_util_pct": true}"#).unwrap()));
        assert_eq!(rig.card.live(), Some(Live { core_mhz: 1800, mem_mhz: 0, temp_c: 70, power_w: 100.0, gpu_util_pct: 0, mem_util_pct: 1, fan_pct: 0 }));
    }

    #[test]
    fn a_reader_nobody_asks_any_more_is_ended_after_a_while() {
        let rig = Rig::new();
        assert!(!rig.card.tend(), "there is a reader to look after before one was started");
        assert_eq!(rig.card.live(), of_the_reader(61));
        // Asked again within the while: it stays.
        for later in [1.0, 5.0, 9.5, 19.0] {
            rig.machine.at(1000.0 + later);
            assert!(rig.card.tend(), "{later} s");
            if later == 9.5 {
                rig.card.live();
            }
        }
        assert_eq!(rig.readers.running(), [1]);
        // Ten seconds after the last reading that was asked for: ended.
        rig.machine.at(1019.5);
        assert!(!rig.card.tend());
        assert!(rig.readers.running().is_empty());
        assert_eq!(rig.last_said(), "the graphics card's reader was ended (pid 1): nobody asked for a reading for 10 s");
        assert!(!rig.card.tend());
        // No pause follows: the next reading that is wanted starts one.
        rig.machine.at(1019.75);
        assert_eq!(rig.card.live(), of_the_reader(61));
        assert_eq!((rig.readers.started(), rig.backend.asked().len()), (2, 0));
    }

    fn an_answer(body: &str) -> Response {
        Response::new(200, "application/json", body.as_bytes().to_vec())
    }

    #[test]
    fn before_a_drivers_installer_is_started_the_reader_is_gone_and_it_stays_away() {
        let rig = Rig::new();
        assert_eq!(rig.card.live(), of_the_reader(61));
        rig.machine.at(1005.0);
        let readers = rig.readers.clone();
        let answer = rig.card.around_a_driver_install(|| {
            // (when the backend hears of the call, no reader runs)
            assert!(readers.running().is_empty(), "the call was passed on with the reader still running");
            an_answer(r#"{"ok": true, "msg": "NVIDIA installer launched."}"#)
        });
        assert_eq!((answer.status, String::from_utf8(answer.body).unwrap()), (200, r#"{"ok": true, "msg": "NVIDIA installer launched."}"#.to_string()), "the answer is the backend's, as it was");
        assert_eq!(rig.said().lines().skip(1).collect::<Vec<_>>(),
                   ["a graphics driver's installer may be about to start: the graphics card's reader was ended first (pid 1), and was seen to be gone",
                    "a graphics driver's installer was started: no reader for the graphics card for 900 s, the backend is asked meanwhile"]);
        // For 900 seconds the reading is the backend's and no reader is started.
        for later in [0.0, 0.2, 60.0, 899.9] {
            rig.machine.at(1005.0 + later);
            assert_eq!(rig.card.live(), of_the_backend(), "{later} s after");
            assert!(!rig.card.tend());
        }
        assert_eq!((rig.readers.started(), rig.readers.running().len(), rig.backend.asked().len()), (1, 0, 4));
        rig.machine.at(1905.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
        assert_eq!(rig.readers.started(), 2);
    }

    #[test]
    fn an_install_that_was_not_started_lets_the_reader_come_back_at_once() {
        // Whatever the backend says that is not "an installer was started".
        for (status, body) in [(200, r#"{"ok": false, "err": "Driver installer not downloaded yet"}"#), (200, r#"{"ok": false, "err": "An install is already in progress"}"#),
                               (200, r#"{"msg": "no word of ok"}"#), (200, "not json"), (200, r#"{"ok": 0}"#), (500, r#"{"ok": true}"#), (502, ""), (403, r#"{"ok": true}"#)] {
            let rig = Rig::new();
            assert_eq!(rig.card.live(), of_the_reader(61));
            let readers = rig.readers.clone();
            let answer = rig.card.around_a_driver_install(|| {
                assert!(readers.running().is_empty(), "the reader was not ended before the call was passed on");
                Response::new(status, "application/json", body.as_bytes().to_vec())
            });
            assert_eq!((answer.status, answer.body.as_slice()), (status, body.as_bytes()));
            assert_eq!(rig.last_said(), "no graphics driver's installer was started: the graphics card's reader may be started again", "{status} {body}");
            rig.machine.at(1000.2);
            assert_eq!(rig.card.live(), of_the_reader(61), "{status} {body}");
            assert_eq!((rig.readers.started(), rig.backend.asked().len()), (2, 0));
        }
        // "ok" is whatever Python takes for true.
        for truth in ["true", "1", "\"yes\"", "[0]", "0.5"] {
            let rig = Rig::new();
            rig.card.around_a_driver_install(|| an_answer(&format!(r#"{{"ok": {truth}}}"#)));
            assert_eq!(rig.card.live(), of_the_backend(), "{truth}");
            assert_eq!(rig.readers.started(), 0);
        }
    }

    #[test]
    fn an_install_asked_for_again_while_one_runs_leaves_the_first_ones_time_as_it_was() {
        let rig = Rig::new();
        rig.card.around_a_driver_install(|| an_answer(r#"{"ok": true}"#));
        // Ten minutes in, the page asks again and the backend refuses:
        // the reader stays away until the first one's 900 seconds are over.
        rig.machine.at(1600.0);
        rig.card.around_a_driver_install(|| an_answer(r#"{"ok": false, "err": "An install is already in progress"}"#));
        rig.machine.at(1899.0);
        assert_eq!(rig.card.live(), of_the_backend());
        rig.machine.at(1900.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
        // Asked again and started again: 900 seconds from then.
        rig.machine.at(2000.0);
        rig.card.around_a_driver_install(|| an_answer(r#"{"ok": true}"#));
        rig.machine.at(2500.0);
        rig.card.around_a_driver_install(|| an_answer(r#"{"ok": true}"#));
        rig.machine.at(3399.0);
        assert_eq!(rig.card.live(), of_the_backend());
        assert_eq!(rig.readers.running().len(), 0);
        rig.machine.at(3400.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
    }

    #[test]
    fn calls_for_an_install_that_overlap_and_start_nothing_leave_nothing_behind() {
        // Two calls, the second begun while the first is on its way, and
        // the first answered first.  Neither starts an installer.
        let rig = Rig::new();
        assert_eq!(rig.card.live(), of_the_reader(61));
        let (first_may_end, ended) = (Arc::new(std::sync::Barrier::new(2)), Arc::new(std::sync::Barrier::new(2)));
        let first = {
            let (card, may_end, ended) = (rig.card.clone(), first_may_end.clone(), ended.clone());
            std::thread::spawn(move || {
                card.around_a_driver_install(|| {
                    may_end.wait();
                    an_answer(r#"{"ok": false, "err": "Driver installer not downloaded yet"}"#)
                });
                ended.wait();
            })
        };
        // (the first is on its way: the reader is gone, and none is started)
        while !rig.readers.running().is_empty() {
            std::thread::sleep(Duration::from_millis(1));
        }
        rig.machine.at(1001.0);
        assert_eq!(rig.card.live(), of_the_backend());
        rig.card.around_a_driver_install(|| {
            first_may_end.wait();
            ended.wait();
            // (the first has been answered; this one is still on its way)
            assert_eq!(rig.card.live(), of_the_backend());
            assert_eq!(rig.readers.started(), 1, "a reader was started while a call for an install was on its way");
            an_answer(r#"{"ok": false, "err": "An install is already in progress"}"#)
        });
        first.join().unwrap();
        // Both answered, neither started one: the next reading has a reader.
        rig.machine.at(1001.2);
        assert_eq!(rig.card.live(), of_the_reader(61), "the reader stayed away though no installer was started");
        assert_eq!(rig.readers.started(), 2);
        // The same with one of the two starting an installer: 900 seconds
        // from its answer, whichever of the two is answered last.
        for the_first_starts_it in [true, false] {
            let rig = Rig::new();
            let (yes, no) = (r#"{"ok": true}"#, r#"{"ok": false}"#);
            let (first_may_end, ended) = (Arc::new(std::sync::Barrier::new(2)), Arc::new(std::sync::Barrier::new(2)));
            let first = {
                let (card, may_end, ended) = (rig.card.clone(), first_may_end.clone(), ended.clone());
                std::thread::spawn(move || {
                    card.around_a_driver_install(|| {
                        may_end.wait();
                        an_answer(if the_first_starts_it { yes } else { no })
                    });
                    ended.wait();
                })
            };
            rig.card.around_a_driver_install(|| {
                first_may_end.wait();
                ended.wait();
                an_answer(if the_first_starts_it { no } else { yes })
            });
            first.join().unwrap();
            rig.machine.at(1899.0);
            assert_eq!(rig.card.live(), of_the_backend(), "the first starts it: {the_first_starts_it}");
            rig.machine.at(1900.0);
            assert_eq!(rig.card.live(), of_the_reader(61), "the first starts it: {the_first_starts_it}");
        }
    }

    #[test]
    fn how_long_things_last_is_not_counted_on_the_pcs_own_clock() {
        // An installer was started, and the PC's clock is set forward by a
        // day: the reader stays away for its 900 seconds all the same.
        let rig = Rig::new();
        // (the PC's clock says something else than the clock that counts,
        // as it does on any PC: the one began in 1970, the other at a start)
        rig.machine.the_pcs_clock_is_set_to(1000.0 + 500_000.0);
        rig.card.around_a_driver_install(|| an_answer(r#"{"ok": true}"#));
        rig.machine.the_pcs_clock_is_set_to(1000.0 + 586_400.0);
        assert_eq!(rig.card.live(), of_the_backend(), "a reader was started early because the PC's clock was set forward");
        assert_eq!(rig.readers.started(), 0);
        // ... and set back by a day: it does not stay away for a day.
        rig.machine.the_pcs_clock_is_set_to(1000.0 - 86_400.0);
        rig.machine.at(1900.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
        // An idle reader is ended after ten seconds that went by, whatever
        // the PC's clock says: not early because it was set forward, and
        // not never because it was set back.
        rig.machine.the_pcs_clock_is_set_to(1900.0 + 3600.0);
        assert!(rig.card.tend(), "a reader was ended as idle because the PC's clock was set forward");
        rig.machine.at(1909.5);
        rig.machine.the_pcs_clock_is_set_to(100.0);
        assert!(rig.card.tend());
        rig.machine.at(1910.0);
        rig.machine.the_pcs_clock_is_set_to(100.0);
        assert!(!rig.card.tend(), "an idle reader stayed because the PC's clock was set back");
        assert!(rig.readers.running().is_empty());
        // And the pause after a reader gave nothing.
        rig.readers.reads(FakeReading::HasNone);
        assert_eq!(rig.card.live(), of_the_backend());
        rig.readers.reads(FakeReading::Says(helper(61)));
        rig.machine.the_pcs_clock_is_set_to(1_000_000.0);
        assert_eq!(rig.card.live(), of_the_backend(), "a reader was tried again early because the PC's clock was set forward");
        rig.machine.at(1940.0);
        rig.machine.the_pcs_clock_is_set_to(0.0);
        assert_eq!(rig.card.live(), of_the_reader(61));
        // What a reading is stamped with is the PC's own clock, as it was.
        assert_eq!(rig.card.now(), 0.0);
    }

    #[test]
    fn a_reader_that_is_not_seen_to_go_is_said_and_the_call_is_passed_on_all_the_same() {
        let rig = Rig::new();
        assert_eq!(rig.card.live(), of_the_reader(61));
        rig.readers.will_not_go(true);
        let passed_on = std::cell::Cell::new(false);
        rig.card.around_a_driver_install(|| {
            passed_on.set(true);
            an_answer(r#"{"ok": true}"#)
        });
        assert!(passed_on.get());
        assert!(rig.said().contains("the graphics card's reader was ended first (pid 1), and was NOT seen to go"), "{}", rig.said());
        // It is not asked again, and no other is started meanwhile.
        assert_eq!(rig.card.live(), of_the_backend());
        assert_eq!(rig.readers.started(), 1);
    }

    #[test]
    fn a_reading_that_is_waited_for_while_the_reader_is_taken_away_is_the_backends_and_sets_no_pause() {
        // The install's call comes between a reading being asked for and
        // its answer: the reader is ended under it.
        struct Taken {
            card: Mutex<Option<Card>>,
            ended: Mutex<bool>,
        }
        impl Reader for Taken {
            fn read(&self, _: Duration) -> Result<Option<Json>, String> {
                let card = self.card.lock().unwrap().clone().unwrap();
                card.around_a_driver_install(|| Response::new(200, "application/json", br#"{"ok": false}"#.to_vec()));
                Err("it ended".to_string())
            }
            fn end(&self) -> bool {
                *self.ended.lock().unwrap() = true;
                true
            }
            fn pid(&self) -> u32 {
                7
            }
        }
        struct One(Arc<Taken>, Mutex<u32>);
        impl Readers for One {
            fn start(&self) -> Result<Arc<dyn Reader>, String> {
                *self.1.lock().unwrap() += 1;
                Ok(self.0.clone())
            }
        }
        let rig = Rig::new();
        let reader = Arc::new(Taken { card: Mutex::new(Some(rig.card.clone())), ended: Mutex::new(false) });
        let readers = Arc::new(One(reader.clone(), Mutex::new(0)));
        rig.card.reads_with(readers.clone());
        assert_eq!(rig.card.live(), of_the_backend());
        assert!(*reader.ended.lock().unwrap());
        assert!(!rig.said().contains("gave no reading"), "a pause was set for a reader that was taken away: {}", rig.said());
        // (no installer was started: the next reading starts a reader at once)
        *reader.card.lock().unwrap() = None;
        assert_eq!(*readers.1.lock().unwrap(), 1);
    }

    #[test]
    fn at_the_quit_the_reader_is_ended_and_none_is_started_any_more() {
        let rig = Rig::new();
        assert_eq!(rig.card.live(), of_the_reader(61));
        rig.card.close();
        assert!(rig.readers.running().is_empty());
        assert_eq!(rig.last_said(), "the graphics card's reader was ended (pid 1): the app quits");
        assert_eq!(rig.card.live(), of_the_backend());
        rig.machine.at(5000.0);
        assert_eq!(rig.card.live(), of_the_backend());
        assert_eq!(rig.readers.started(), 1);
        rig.card.close();
    }

    #[test]
    fn with_its_thread_an_idle_reader_is_ended_by_itself() {
        let (machine, readers) = (Fake::new(), FakeReaders::new());
        machine.at(1000.0);
        readers.reads(FakeReading::Says(helper(61)));
        let moved = Moved::new(machine.clone(), true);
        moved.card.reads_with(Arc::new(readers.clone()));
        assert_eq!(moved.card.live(), of_the_reader(61));
        // (the clock is the machine's: moved on by hand, looked at by the thread)
        std::thread::sleep(Duration::from_millis(1300));
        assert_eq!(readers.running(), [1], "ended while it was wanted a moment ago");
        machine.at(1010.0);
        let began = std::time::Instant::now();
        while !readers.running().is_empty() && began.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(readers.running().is_empty(), "an idle reader was not ended");
        // Wanted again: a new one, and a thread looks after that one too.
        assert_eq!(moved.card.live(), of_the_reader(61));
        machine.at(1020.0);
        let began = std::time::Instant::now();
        while !readers.running().is_empty() && began.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert_eq!((readers.started(), readers.running().len()), (2, 0));
        assert!(machine.asked().is_empty());
    }
}
