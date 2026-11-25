//! What has moved from the backend into the shell (step 4 of
//! docs/rust-port.md: the backend, one module at a time).
//!
//! A call of the page that a module here takes is answered by the shell
//! itself and does not reach the backend.  The backend's own route stays as
//! it was.  It answers when the app is started as it always was; and behind
//! the shell it answers every call the shell does not take: all of them
//! when the shell was told to leave everything to the backend, and
//! otherwise whatever of a moved path is not what the page sends (another
//! method, a body Python stops at before it does anything, a number only
//! Python can read).  So for those the answer is the backend's own by
//! construction, and for the ones the shell takes it is the same answer,
//! which tests/test_moved_perf.py proves on the same made-up machine.
//!
//! The rules, from the plan:
//!
//! * **Reading is kept apart from changing.**  Everything in this folder
//!   only reads the machine.  Its one door to it is `Readings`: a question
//!   to the native library by its name, and the time.  Nothing that changes
//!   the machine can be reached through that door: the library's actions
//!   are behind another function of its own, which is named nowhere in the
//!   shell (tests/test_moved_perf.py holds that).  Two questions take
//!   words of their own, and the door holds each to what it may be asked:
//!   WMI is asked to tell rows and nothing else, and an echo request goes to
//!   an address that is written as four numbers and to nothing else.
//! * **A real side and a made-up side, from the first handler.**  `Real`
//!   asks the library that is compiled into the shell (native/, the same
//!   code the Python app loads as a DLL).  `Fake` answers from a table and
//!   tells a time that is set by hand: the tests' machine.
//! * **The graphics card is not asked through that door.**  Asking it
//!   means starting NVIDIA's library (nvml.dll) in the asking process, and
//!   once that library has been started it stays in the process until the
//!   process ends, whatever is shut down or freed (measured:
//!   tools/measure/nvml_release.py).  A driver's installer has to replace
//!   that file.  The shell lives for as long as the app does, so it does
//!   not open the library at all: `Real` refuses every question about the
//!   card.  The card is read by a small program of the shell's own, which
//!   the shell starts while a reading is wanted and ends before a driver's
//!   installer is started; and by the backend wherever that program is
//!   away or has no reading (card.rs).  Either way it is the reading the
//!   Python modules take themselves, so a module that lives on it gives
//!   Python's answers at all times, also while a driver is installed.
//!
//! What has moved so far: the performance sampler (perf.rs) and the
//! hardware monitor (hw.rs).

pub mod card;
pub mod hw;
pub mod perf;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::http::{Request, Response};
use crate::json::Json;

pub use card::{AskBackend, Card, FakeBackend, FakeReaders, FakeReading, From, Live, Reader, Readers, Reading, Say, HOLD_FOR_AN_INSTALL_S, IDLE_S, THE_BACKENDS_READING};

/// The door to the machine, for everything that has moved: questions only.
pub trait Readings: Send + Sync {
    /// What the native library answers to the question named `question`
    /// (native/README.md lists them), or None where it has no answer.
    fn ask(&self, question: &str) -> Option<Json>;
    /// The same for one of the two questions that take words of their own:
    /// "wmi" (a `namespace` and a `query`) and "ping" (a `target` and a
    /// `timeout_ms`).
    fn ask_with(&self, question: &str, with: &[(&str, Json)]) -> Option<Json>;
    /// The time, in seconds since 1970: what Python's `time.time()` gives.
    /// It is the PC's own clock, which a person or Windows can set forward
    /// or back at any moment: for telling when something happened, never
    /// for how long something lasts.
    fn now(&self) -> f64;
    /// Seconds on a clock that only goes forward, and is set by nobody: for
    /// how long something lasts (how long a reader has been idle, how long
    /// it has to stay away).  It begins somewhere; only the difference
    /// between two readings of it says anything.
    fn steady(&self) -> f64;
    /// Let this many seconds go by (a reading that is the difference
    /// between two readings needs some time between them).
    fn wait(&self, seconds: f64);
}

/// The real machine.
pub struct Real;

impl Readings for Real {
    fn ask(&self, question: &str) -> Option<Json> {
        self.ask_with(question, &[])
    }

    fn ask_with(&self, question: &str, with: &[(&str, Json)]) -> Option<Json> {
        // (a question's name, written in this crate: never text of the page's)
        if question.is_empty() || !question.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'_') {
            return None;
        }
        // The graphics card is not asked from this process, by whatever
        // name: the library would open the driver's library to answer, and
        // that stays in the process for good (the top of this file).
        if question.starts_with("gpu_") {
            return None;
        }
        // Two questions take words of their own, and only these words;
        // every other question is its name and nothing beside it.
        let text = |name: &str| with.iter().find(|(known, _)| *known == name).and_then(|(_, value)| value.as_str());
        let may = match question {
            // WMI is asked to tell rows (the library itself runs nothing
            // that does not begin with SELECT either).
            "wmi" => with.len() == 2 && text("namespace").is_some() && text("query").is_some_and(|query| query.starts_with("SELECT ")),
            // One echo request, to an address written as four numbers: no
            // name is looked up, and nothing else is sent anywhere.
            "ping" => {
                with.len() == 2 && text("target").is_some_and(plain_ipv4)
                    && with.iter().any(|(name, value)| *name == "timeout_ms" && matches!(value, Json::Int(1..=10_000)))
            }
            _ => with.is_empty(),
        };
        if !may {
            return None;
        }
        let mut request = vec![("q".to_string(), Json::from(question))];
        request.extend(with.iter().map(|(name, value)| (name.to_string(), value.clone())));
        // `answer` is the library's door for questions.  Its other door,
        // for the two things that change the machine, is not used here or
        // anywhere in the shell.
        let said = Json::parse(&vispora_native::answer(&Json::Object(request).to_string())).ok()?;
        match said.get("ok") {
            Some(Json::Bool(true)) => said.get("data").cloned(),
            _ => None,
        }
    }

    fn now(&self) -> f64 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |since| since.as_secs_f64())
    }

    fn steady(&self) -> f64 {
        static BEGAN: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
        BEGAN.get_or_init(std::time::Instant::now).elapsed().as_secs_f64()
    }

    fn wait(&self, seconds: f64) {
        if seconds > 0.0 && seconds.is_finite() {
            std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
        }
    }
}

/// A made-up machine: it answers from a table, and its two clocks are set
/// by hand.
#[derive(Default)]
pub struct Fake {
    answers: Mutex<HashMap<String, Json>>,
    /// What counts up as time goes by: for a question, by how much each of
    /// the whole numbers in its answer grows in a second.
    growing: Mutex<HashMap<String, Json>>,
    /// The PC's own clock.
    clock: Mutex<f64>,
    /// The clock that does not jump.
    steady: Mutex<f64>,
    asked: Mutex<Vec<String>>,
    /// How long was waited so far, in all.
    waited: Mutex<f64>,
}

/// What a question is kept under in the made-up machine's table: its name,
/// and the words that go with it in the order of their names.
fn kept_as(question: &str, with: &[(&str, Json)]) -> String {
    if with.is_empty() {
        return question.to_string();
    }
    let mut with: Vec<(String, Json)> = with.iter().map(|(name, value)| (name.to_string(), value.clone())).collect();
    with.sort_by(|a, b| a.0.cmp(&b.0));
    format!("{question} {}", Json::Object(with))
}

impl Fake {
    pub fn new() -> Arc<Fake> {
        Arc::new(Fake::default())
    }

    /// What the library answers to `question` from now on (None: nothing).
    pub fn answers(&self, question: &str, answer: Option<Json>) {
        self.answers_with(question, &[], answer);
    }

    /// The same for a question that is asked with words of its own: what
    /// it answers when it is asked with exactly these.
    pub fn answers_with(&self, question: &str, with: &[(&str, Json)], answer: Option<Json>) {
        let mut answers = self.answers.lock().unwrap_or_else(|p| p.into_inner());
        match answer {
            Some(answer) => answers.insert(kept_as(question, with), answer),
            None => answers.remove(&kept_as(question, with)),
        };
    }

    /// From now on the whole numbers in the answer to `question` count up
    /// as time goes by: each by what `each_second` says under its name
    /// (None: they stand still again).  As a processor's times do.
    pub fn grows(&self, question: &str, each_second: Option<Json>) {
        let mut growing = self.growing.lock().unwrap_or_else(|p| p.into_inner());
        match each_second {
            Some(each_second) => growing.insert(question.to_string(), each_second),
            None => growing.remove(question),
        };
    }

    /// This much time has gone by: what counts up has counted.
    fn has_gone_by(&self, seconds: f64) {
        if seconds.is_nan() || seconds <= 0.0 {
            return;
        }
        let growing = self.growing.lock().unwrap_or_else(|p| p.into_inner());
        let mut answers = self.answers.lock().unwrap_or_else(|p| p.into_inner());
        for (question, each_second) in growing.iter() {
            let (Some(Json::Object(now)), Json::Object(rates)) = (answers.get_mut(question), each_second) else { continue };
            for (name, value) in now.iter_mut() {
                if let (Json::Int(whole), Some((_, Json::Int(rate)))) = (&mut *value, rates.iter().find(|(known, _)| known == name)) {
                    *whole += (*rate as f64 * seconds).round() as i128;
                }
            }
        }
    }

    /// What time it is from now on, on both clocks: time has gone by.
    pub fn at(&self, time: f64) {
        let before = self.steady();
        self.has_gone_by(time - before);
        *self.clock.lock().unwrap_or_else(|p| p.into_inner()) = time;
        *self.steady.lock().unwrap_or_else(|p| p.into_inner()) = time;
    }

    /// How long was waited so far, in all.
    pub fn waited(&self) -> f64 {
        *self.waited.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// The PC's own clock is set to another time (by the person, or by
    /// Windows): no time has gone by.
    pub fn the_pcs_clock_is_set_to(&self, time: f64) {
        *self.clock.lock().unwrap_or_else(|p| p.into_inner()) = time;
    }

    /// Every question that was asked, in order.
    pub fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }
}

impl Readings for Fake {
    fn ask(&self, question: &str) -> Option<Json> {
        self.ask_with(question, &[])
    }

    fn ask_with(&self, question: &str, with: &[(&str, Json)]) -> Option<Json> {
        let kept = kept_as(question, with);
        let answer = self.answers.lock().unwrap_or_else(|p| p.into_inner()).get(&kept).cloned();
        self.asked.lock().unwrap_or_else(|p| p.into_inner()).push(kept);
        answer
    }

    fn now(&self) -> f64 {
        *self.clock.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn steady(&self) -> f64 {
        *self.steady.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// (nobody waits: the made-up time goes by at once, on both clocks)
    fn wait(&self, seconds: f64) {
        if seconds.is_nan() || seconds <= 0.0 {
            return;
        }
        self.has_gone_by(seconds);
        *self.clock.lock().unwrap_or_else(|p| p.into_inner()) += seconds;
        *self.steady.lock().unwrap_or_else(|p| p.into_inner()) += seconds;
        *self.waited.lock().unwrap_or_else(|p| p.into_inner()) += seconds;
    }
}

/// Everything that has moved, with the door it reads the machine through.
pub struct Moved {
    pub card: Arc<Card>,
    pub perf: Arc<perf::Sampler>,
    pub hw: Arc<hw::Monitor>,
}

impl Moved {
    /// `threads`: may what has moved run threads of its own (the sampler's,
    /// the monitor's, and the one that looks after the card's reader)?  Not
    /// where a test moves the clock by hand and does every round itself
    /// (`perf::Sampler::step`, `hw::Monitor::step`, `Card::tend`).
    pub fn new(door: Arc<dyn Readings>, threads: bool) -> Arc<Moved> {
        Arc::new(Moved { card: Arc::new(Card::new(door.clone(), threads)), perf: perf::Sampler::new(threads), hw: hw::Monitor::new(door, threads) })
    }

    /// How the backend is asked: for the graphics card's reading, where the
    /// card's own reader is away or has none; and for a part of the
    /// hardware monitor's reading, where Python would start a program to
    /// take it.
    pub fn asks_the_backend_with(&self, ask: AskBackend) {
        self.card.asks_the_backend_with(ask.clone());
        self.hw.asks_the_backend_with(ask);
    }

    /// The shell quits: what has moved stops, and reads nothing any more.
    pub fn close(&self) {
        self.hw.close();
        self.card.close();
    }

    /// Is this the call that starts a graphics driver's installer?  It is
    /// the backend's to answer, but the card's reader has to be gone before
    /// the backend hears of it (`Card::around_a_driver_install`).
    ///
    /// The app starts an installer in one place, behind one call
    /// (app.py, api_driver_install).  The path is held against the route
    /// the way the web framework holds it, which reads a letter that is
    /// written as "%" and two digits as that letter: a call that the
    /// backend would take for this one is this one here too.
    pub fn starts_a_driver_install(request: &Request) -> bool {
        request.method == "POST" && as_the_backend_reads_a_path(request.path()) == THE_DRIVER_INSTALL
    }

    /// The shell's own answer to a call of the page, or None: the call is
    /// the backend's, and is passed on as every call was before step 4.
    pub fn answer(&self, request: &Request) -> Option<Response> {
        if request.path().starts_with("/api/perf/") {
            return self.perf.answer(&self.card, request);
        }
        // (the monitor's calls are known however they are written: one that
        // the backend would take for a start must not be passed on to it
        // as something else, or its own monitor would run beside this one)
        let path = as_the_backend_reads_a_path(request.path());
        if path.starts_with("/api/monitor/") {
            return self.hw.answer(&self.card, request, &path);
        }
        None
    }
}

/// The backend's call that starts a graphics driver's installer.
pub const THE_DRIVER_INSTALL: &str = "/api/driver/install";

/// A call's path with every "%" and two digits read as the letter they
/// stand for, as the backend's web framework reads a path before it looks
/// for the route.  (Letters that are not text become one that no route has.)
fn as_the_backend_reads_a_path(path: &str) -> String {
    let (bytes, mut out, mut at) = (path.as_bytes(), Vec::with_capacity(path.len()), 0);
    while at < bytes.len() {
        let hex = |byte: u8| (byte as char).to_digit(16);
        match (bytes[at], bytes.get(at + 1).copied().and_then(hex), bytes.get(at + 2).copied().and_then(hex)) {
            (b'%', Some(high), Some(low)) => {
                out.push((high * 16 + low) as u8);
                at += 3;
            }
            (byte, ..) => {
                out.push(byte);
                at += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ═════════════════════════════════════════════════════════════════════
// What a route of the backend reads off a call, the way it reads it
// ═════════════════════════════════════════════════════════════════════

/// The first value given for `name` in the call's query, as Flask's
/// `request.args.get(name)` gives it: `+` is a blank, `%41` is `A`, and
/// what is not written properly stays as it is written.
pub fn query_value(target: &str, name: &str) -> Option<String> {
    let query = target.split_once('?')?.1.split('#').next().unwrap_or("");
    query.split('&').filter(|pair| !pair.is_empty()).find_map(|pair| {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        (unquoted(key) == name).then(|| unquoted(value))
    })
}

fn unquoted(text: &str) -> String {
    let bytes = text.as_bytes();
    let (mut out, mut at) = (Vec::with_capacity(bytes.len()), 0);
    let hex = |byte: u8| (byte as char).to_digit(16).map(|digit| digit as u8);
    while at < bytes.len() {
        match bytes[at] {
            b'+' => out.push(b' '),
            b'%' if at + 2 < bytes.len() && hex(bytes[at + 1]).is_some() && hex(bytes[at + 2]).is_some() => {
                out.push(hex(bytes[at + 1]).unwrap_or(0) * 16 + hex(bytes[at + 2]).unwrap_or(0));
                at += 2;
            }
            other => out.push(other),
        }
        at += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A text for a call's query, written so that Flask reads it back as it
/// is: everything but plain letters, digits and four marks as "%" and two
/// digits.
pub fn quoted(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// Is this an IPv4 address written plainly: four numbers of one to three
/// digits, none above 255, none with a nought before it, with dots between
/// and nothing around?  (What Python takes for an IPv4 address and the
/// native library reads as one: core/native.py, _is_ipv4; native/src/ping.rs.
/// Python takes a few more ways of writing one, which the library then
/// does not read; for those, and for a name, it starts ping.exe.)
pub fn plain_ipv4(text: &str) -> bool {
    let parts: Vec<&str> = text.split('.').collect();
    parts.len() == 4 && parts.iter().all(|part| {
        (1..=3).contains(&part.len()) && part.bytes().all(|byte| byte.is_ascii_digit()) && (part.len() == 1 || !part.starts_with('0'))
            && part.parse::<u16>().is_ok_and(|number| number <= 255)
    })
}

/// What the body of a POST says to a route that reads it with
/// `request.get_json(force=True) or {}` and then takes values out of it
/// with `.get`.
#[derive(Debug, Clone, PartialEq)]
pub enum Said {
    /// This object (an empty one for a body that says nothing: `null`, `0`,
    /// `""`, `[]`, `{}`).
    Says(Json),
    /// Python stops before the route does anything: the body is no JSON,
    /// or it is something and not an object.
    PythonStops,
    /// Python may well read it, and this program cannot: it is not plain
    /// UTF-8 (Python reads JSON in UTF-16 and UTF-32 too, which it knows by
    /// a zero among the first bytes, and lets halves of a character pair
    /// through), or it is nested deeper than this program reads.
    CannotTell,
}

/// See `Said`.
pub fn said_by(body: &[u8]) -> Said {
    // (Python takes the mark off that some programs put before UTF-8)
    let (body, marked) = match body.strip_prefix(b"\xEF\xBB\xBF") {
        Some(rest) => (rest, true),
        None => (body, false),
    };
    let another_encoding = !marked
        && match body {
            [0xFF, 0xFE, ..] | [0xFE, 0xFF, ..] => true,
            [a, b, _, _, ..] => *a == 0 || *b == 0,
            [a, b] => *a == 0 || *b == 0,
            _ => false,
        };
    if another_encoding {
        return Said::CannotTell;
    }
    let Ok(text) = std::str::from_utf8(body) else {
        // Python stops at what is not UTF-8, but for one thing: it lets
        // halves of a character pair through, written as UTF-8 writes
        // characters.  This program cannot hold those.
        return if utf8_with_halves(body) { Said::CannotTell } else { Said::PythonStops };
    };
    match Json::parse(text) {
        Ok(said @ Json::Object(_)) => Said::Says(said),
        Ok(other) if !other.truthy() => Said::Says(Json::Object(Vec::new())),
        Ok(_) => Said::PythonStops,
        Err(why) if why == TOO_DEEP => Said::CannotTell,
        Err(_) => Said::PythonStops,
    }
}

/// What the reader of JSON says of a text that is nested deeper than it reads.
const TOO_DEEP: &str = "nested too deeply";

/// Would these bytes be UTF-8 if halves of a character pair (written
/// 0xED, 0xA0 to 0xBF, and one more) were let through, as Python's
/// "surrogatepass" lets them through?
fn utf8_with_halves(bytes: &[u8]) -> bool {
    let mut other = bytes.to_vec();
    let mut at = 0;
    while at + 2 < other.len() {
        if other[at] == 0xED && (0xA0..=0xBF).contains(&other[at + 1]) && (0x80..=0xBF).contains(&other[at + 2]) {
            // (a character of the same length that UTF-8 does allow)
            other[at..at + 3].copy_from_slice(&[0xEE, 0x80, 0x80]);
            at += 3;
        } else {
            at += 1;
        }
    }
    std::str::from_utf8(&other).is_ok()
}

/// What Python's `int(text)` makes of a text.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PyInt {
    /// This number.
    Is(i128),
    /// It raises: the text is no number.
    IsNot,
    /// Python may well read a number, and this program cannot say which:
    /// Python reads the digits of every script and blanks of every kind,
    /// and counts with as many digits as there are.  And what reaches
    /// Python of a letter that cannot be printed is the web framework's to
    /// decide, which is not rebuilt here.
    CannotTell,
}

/// What Python's `int(text)` gives for a text: blanks around it, one sign,
/// digits, and single underscores between digits.  Where the text is not
/// plain (a letter outside ASCII, or one that cannot be printed) or the
/// number is longer than this program counts with, the answer is that it
/// cannot tell; a caller then leaves the whole call to Python.
pub fn py_int(text: &str) -> PyInt {
    if !text.bytes().all(|byte| (b' '..=b'~').contains(&byte)) {
        return PyInt::CannotTell;
    }
    let text = text.trim_matches(' ');
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__")
        || !digits.bytes().all(|byte| byte.is_ascii_digit() || byte == b'_')
    {
        return PyInt::IsNot;
    }
    let mut value: i128 = 0;
    for digit in digits.bytes().filter(|byte| *byte != b'_') {
        value = match value.checked_mul(10).and_then(|value| value.checked_add((digit - b'0') as i128)) {
            Some(value) => value,
            None => return PyInt::CannotTell,
        };
    }
    PyInt::Is(if negative { -value } else { value })
}

/// What Flask's `_qint(name, default)` gives (app.py): the number in the
/// query, or `default` for anything that is not one.  None where Python
/// may read a number that this program cannot (`PyInt::CannotTell`): the
/// call is then Python's to answer.
pub fn query_int(target: &str, name: &str, default: i128) -> Option<i128> {
    match query_value(target, name).map(|text| py_int(&text)) {
        None | Some(PyInt::IsNot) => Some(default),
        Some(PyInt::Is(number)) => Some(number),
        Some(PyInt::CannotTell) => None,
    }
}

/// What Python's `round(value, places)` gives.  Python rounds the number as
/// it is held (which is rarely the decimal number that was written: 2.675 is
/// held as a little less, and rounds to 2.67), half to even where it is
/// held exactly; writing the number with that many places does the same.
pub fn py_round(value: f64, places: usize) -> f64 {
    if !value.is_finite() {
        return value;
    }
    format!("{value:.places$}").parse().unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_number_is_read_as_python_reads_it() {
        for (text, wanted) in [("60", Some(60)), (" 30 ", Some(30)), ("+7", Some(7)), ("-5", Some(-5)), ("007", Some(7)), ("1_000", Some(1000)),
                               ("0", Some(0)), ("-0", Some(0)), ("   12  ", Some(12)),
                               ("100000000000000000000000000000", Some(100_000_000_000_000_000_000_000_000_000)),
                               ("", None), (" ", None), ("abc", None), ("1.5", None), ("1e3", None), ("--1", None), ("+-1", None), ("- 1", None),
                               ("1_", None), ("_1", None), ("1__0", None), ("0x10", None), ("1 0", None), ("+", None), ("-", None), ("~4", None)] {
            assert_eq!(py_int(text), wanted.map_or(PyInt::IsNot, PyInt::Is), "{text:?}");
        }
        // Python may read a number here, and this program cannot say which:
        // more digits than it counts with (Python counts on), and anything
        // that is not plain (Python reads the digits of every script, and
        // takes blanks of every kind off the ends).  And a letter that
        // cannot be printed: Python takes some of those for blanks, and
        // what reaches it of them is the web framework's to say (it is not
        // what one would think: tests/test_moved_perf.py).
        assert_eq!(py_int(&"9".repeat(38)), PyInt::Is(99_999_999_999_999_999_999_999_999_999_999_999_999));
        for text in ["9".repeat(40), format!("-{}", "9".repeat(60)), "\u{661}\u{662}".to_string(), "12\u{a0}".to_string(), "\u{2003}7".to_string(),
                     "\u{ff11}".to_string(), "7\u{e9}".to_string(), "\t12\n".to_string(), "\x0b\x0c12\x1c\x1f".to_string(), "1\x0012".to_string(),
                     "12\x7f".to_string(), "\x0812".to_string(), "12\r".to_string()] {
            assert_eq!(py_int(&text), PyInt::CannotTell, "{text:?}");
        }
    }

    #[test]
    fn a_query_is_read_as_flask_reads_it() {
        let asked = |target: &str| query_value(target, "seconds");
        assert_eq!(asked("/api/perf/samples?seconds=60").as_deref(), Some("60"));
        assert_eq!(asked("/api/perf/samples").as_deref(), None);
        assert_eq!(asked("/api/perf/samples?").as_deref(), None);
        assert_eq!(asked("/api/perf/samples?other=1&seconds=5&seconds=9").as_deref(), Some("5"), "the first one");
        assert_eq!(asked("/api/perf/samples?seconds").as_deref(), Some(""));
        assert_eq!(asked("/api/perf/samples?seconds=").as_deref(), Some(""));
        assert_eq!(asked("/api/perf/samples?seconds=%33%30").as_deref(), Some("30"));
        assert_eq!(asked("/api/perf/samples?%73econds=4").as_deref(), Some("4"), "the name is written the same way");
        assert_eq!(asked("/api/perf/samples?seconds=+30").as_deref(), Some(" 30"), "a plus is a blank");
        assert_eq!(asked("/api/perf/samples?seconds=%2B30").as_deref(), Some("+30"));
        assert_eq!(asked("/api/perf/samples?seconds=1%").as_deref(), Some("1%"), "what is not written properly stays");
        assert_eq!(asked("/api/perf/samples?seconds=%zz1").as_deref(), Some("%zz1"));
        assert_eq!(asked("/api/perf/samples?seconds=7#seconds=8").as_deref(), Some("7"));
        assert_eq!(asked("/api/perf/samples?&&seconds=3&").as_deref(), Some("3"));
        assert_eq!(asked("/api/perf/samples?Seconds=3").as_deref(), None);
        for (target, wanted) in [("/x?seconds=60", 60), ("/x", 30), ("/x?seconds=", 30), ("/x?seconds=abc", 30), ("/x?seconds=-5", -5),
                                 ("/x?seconds=+12", 12), ("/x?seconds=1.5", 30)] {
            assert_eq!(query_int(target, "seconds", 30), Some(wanted), "{target}");
        }
        // Where Python may read a number and this program cannot, there is
        // no saying what Flask would give: the call is Python's.
        for target in [format!("/x?seconds={}", "9".repeat(60)), "/x?seconds=%D9%A1%D9%A2".to_string(), "/x?seconds=12%C2%A0".to_string(),
                       "/x?seconds=%FF".to_string(), "/x?seconds=%094".to_string()] {
            assert_eq!(query_int(&target, "seconds", 30), None, "{target}");
        }
        assert_eq!(query_int("/x?other=%D9%A1&seconds=4", "seconds", 30), Some(4), "another value's letters are not this one's");
    }

    #[test]
    fn the_call_that_starts_a_drivers_installer_is_known_however_it_is_written() {
        let is_it = |method: &str, target: &str| Moved::starts_a_driver_install(&Request::new(method, target));
        for target in ["/api/driver/install", "/api/driver/install?x=1", "/api/driver/install?", "/api/driver/%69nstall", "/api/%64river/install",
                       "/api%2Fdriver%2Finstall", "/%61%70%69/driver/install", "/api/driver/instal%6C?next=/api/other", "/api/driver/inst%61ll#x"] {
            assert!(is_it("POST", target), "{target}");
        }
        // (a method is passed on in capital letters, however it was written)
        assert!(is_it("post", "/api/driver/install"));
        // Another method does not start one (the backend answers it with
        // words of its own), and neither does another path.
        for (method, target) in [("GET", "/api/driver/install"), ("PUT", "/api/driver/install"), ("HEAD", "/api/driver/install"), ("OPTIONS", "/api/driver/install"),
                                 ("POST", "/api/driver/install/"), ("POST", "/api/driver/Install"), ("POST", "/api/driver/installer"), ("POST", "/api/driver"),
                                 ("POST", "/api/driver/status"), ("POST", "/api/driver/download"), ("POST", "/api/driver/install%"), ("POST", "/api/driver/install%2"),
                                 ("POST", "/api/driver/%zznstall"), ("POST", "/api/perf/start"), ("POST", "/api/other?next=/api/driver/install"),
                                 ("POST", "/api/driver/install%00"), ("POST", "/api/driver/inst%FFall")] {
            assert!(!is_it(method, target), "{method} {target}");
        }
        assert_eq!(as_the_backend_reads_a_path("/a%20b/%C3%A9/%41%zz%4"), "/a b/\u{e9}/A%zz%4");
    }

    #[test]
    fn a_number_is_rounded_as_python_rounds_it() {
        for (value, places, wanted) in [(2.675, 2, 2.67), (0.125, 2, 0.12), (0.375, 2, 0.38), (2.5, 0, 2.0), (3.5, 0, 4.0), (97.25, 1, 97.2),
                                        (97.35, 1, 97.3), (99.95, 1, 100.0), (0.05, 1, 0.1), (-0.25, 1, -0.2), (33.333333333333336, 1, 33.3),
                                        (0.0, 1, 0.0), (120.0, 1, 120.0), (48.123, 2, 48.12)] {
            assert_eq!(py_round(value, places), wanted, "round({value}, {places})");
        }
    }

    #[test]
    fn the_made_up_machine_answers_from_its_table_and_says_what_it_was_asked() {
        let machine = Fake::new();
        assert_eq!((machine.ask("gpu_live"), machine.now()), (None, 0.0));
        machine.answers("gpu_live", Some(Json::object([("temp_c", 61.into())])));
        machine.at(1000.5);
        assert_eq!(machine.ask("gpu_live").unwrap()["temp_c"].as_int(), Some(61));
        assert_eq!(machine.now(), 1000.5);
        machine.answers("gpu_live", None);
        assert_eq!(machine.ask("gpu_live"), None);
        assert_eq!(machine.asked(), ["gpu_live", "gpu_live", "gpu_live"]);
    }

    #[test]
    fn the_real_door_asks_questions_by_name_and_nothing_else() {
        // What is not a question's name is not sent anywhere.
        for not_one in ["", "gpu_live\"}", "{\"do\":\"purge_standby_list\"}", "GPU_LIVE", "gpu live", "abi\0"] {
            assert_eq!(Real.ask(not_one), None, "{not_one:?}");
        }
        // The library is in the shell and answers: which library it is.
        assert_eq!(Real.ask("abi").unwrap()["abi"].as_int(), Some(vispora_native::ABI as i128));
        // A text that would be a question with something of its own beside
        // it, if it were put into the request as it is: it is not put in.
        // (The library answers this request; what stops it is the door.)
        let smuggled = "abi\",\"x\":\"1";
        assert!(vispora_native::answer(&format!("{{\"q\":\"{smuggled}\"}}")).contains("\"ok\":true"), "the library would not have taken it anyway");
        assert_eq!(Real.ask(smuggled), None);
        // A question it does not have, and one that is an action's name:
        // no answer, and nothing done (the door for questions has no
        // actions behind it).
        assert_eq!(Real.ask("no_such_question"), None);
        assert_eq!(Real.ask("purge_standby_list"), None);
        assert_eq!(Real.ask("trim_working_sets"), None);
        assert!(Real.now() > 1_700_000_000.0);
        // The clock that does not jump goes forward, and is not the PC's own.
        let (first, wall) = (Real.steady(), Real.now());
        std::thread::sleep(std::time::Duration::from_millis(30));
        let second = Real.steady();
        assert!((0.02..5.0).contains(&(second - first)) && first < 1_000_000.0 && wall - first > 1_000_000_000.0, "{first} {second}");
    }

    #[test]
    fn the_real_door_asks_wmi_for_rows_and_pings_a_plain_address_and_nothing_else() {
        let wmi = |namespace: &str, query: &str| Real.ask_with("wmi", &[("namespace", namespace.into()), ("query", query.into())]);
        let rows = wmi("root\\cimv2", "SELECT NumberOfLogicalProcessors FROM Win32_ComputerSystem").unwrap();
        assert!(rows.as_array().is_some_and(|rows| rows.len() == 1), "{rows}");
        // (a namespace that is not there: no rows, as the app reads it)
        assert_eq!(wmi("root\\NoSuchNamespaceHere", "SELECT Name FROM Sensor"), Some(Json::Array(vec![])));
        // Only a question that begins with SELECT, with nothing else beside it.
        for query in ["select Name FROM Win32_Process", " SELECT Name FROM Win32_Process", "DELETE FROM Win32_Process", "", "SELECT"] {
            assert_eq!(wmi("root\\cimv2", query), None, "{query:?}");
        }
        assert_eq!(Real.ask_with("wmi", &[("namespace", "root\\cimv2".into()), ("query", "SELECT Name FROM Win32_Processor".into()), ("x", 1.into())]), None);
        assert_eq!(Real.ask_with("wmi", &[("query", "SELECT Name FROM Win32_Processor".into())]), None);
        assert_eq!(Real.ask_with("wmi", &[("namespace", 5.into()), ("query", "SELECT Name FROM Win32_Processor".into())]), None);
        // An echo request to this PC itself (it does not leave the PC).
        let ping = |target: Json, timeout: Json| Real.ask_with("ping", &[("target", target), ("timeout_ms", timeout)]);
        let said = ping("127.0.0.1".into(), Json::Int(500)).unwrap();
        assert_eq!(said["ok"], Json::Bool(true), "{said}");
        // Never to a name, to an address written another way, or with a
        // limit of its own choosing; and nothing is sent then.
        for target in ["localhost", "127.0.0.01", " 127.0.0.1", "127.0.0.1 ", "127.0.0", "127.0.0.1.5", "256.0.0.1", "", "\u{661}27.0.0.1"] {
            assert_eq!(ping(target.into(), Json::Int(500)), None, "{target:?}");
        }
        for timeout in [Json::Int(0), Json::Int(10_001), Json::Float(500.0), "500".into(), Json::Null] {
            assert_eq!(ping("127.0.0.1".into(), timeout.clone()), None, "{timeout}");
        }
        assert_eq!(Real.ask_with("ping", &[("target", "127.0.0.1".into())]), None);
        // Words beside any other question: not asked.
        assert_eq!(Real.ask_with("cpu_times", &[("x", 1.into())]), None);
        assert_eq!(Real.ask_with("abi", &[("q", "purge_standby_list".into())]), None);
        // The two that the hardware monitor reads, which take no words.
        assert!(Real.ask("memory").unwrap()["load_pct"].as_int().is_some_and(|load| (1..=100).contains(&load)));
        assert!(Real.ask("dpc_rate").unwrap()["per_second"].as_int().is_some());
        assert!(plain_ipv4("1.1.1.1") && plain_ipv4("255.0.10.0") && !plain_ipv4("01.1.1.1") && !plain_ipv4("1.1.1") && !plain_ipv4("1..1.1"));
    }

    #[test]
    fn a_body_is_read_as_the_backends_route_reads_it() {
        let says = |body: &[u8]| said_by(body);
        assert_eq!(says(b"{\"a\": 1}"), Said::Says(Json::parse("{\"a\": 1}").unwrap()));
        for nothing in [&b"null"[..], b"0", b"\"\"", b"[]", b"{}", b"false", b" {} ", b"\xEF\xBB\xBF{}"] {
            assert_eq!(says(nothing), Said::Says(Json::Object(Vec::new())), "{nothing:?}");
        }
        for stops in [&b""[..], b"x", b"[1]", b"\"a\"", b"5", b"true", b"{", b"\xff", b"{\"a\": \"\xc3\"}", b"\xEF\xBB\xBF\xEF\xBB\xBF{}"] {
            assert_eq!(says(stops), Said::PythonStops, "{stops:?}");
        }
        let deep = format!("{}{}", "[".repeat(80), "]".repeat(80));
        for cannot in [deep.as_bytes(), b"\xff\xfe{\0}\0", b"\xfe\xff\0{\0}", b"{\0}\0", b"\0{\0}", b"{\0", b"\0{", b"{\"a\": \"\xed\xa0\x80\"}"] {
            assert_eq!(says(cannot), Said::CannotTell, "{cannot:?}");
        }
        assert_eq!(quoted("a b&c=d/\u{f6}~-._"), "a%20b%26c%3Dd%2F%C3%B6~-._");
    }

    #[test]
    fn the_made_up_machine_has_a_clock_of_the_pcs_and_one_that_does_not_jump() {
        let machine = Fake::new();
        machine.at(1000.0);
        assert_eq!((machine.now(), machine.steady()), (1000.0, 1000.0));
        machine.the_pcs_clock_is_set_to(99_999.0);
        assert_eq!((machine.now(), machine.steady()), (99_999.0, 1000.0));
        machine.the_pcs_clock_is_set_to(5.0);
        assert_eq!((machine.now(), machine.steady()), (5.0, 1000.0));
        machine.at(1000.5);
        assert_eq!((machine.now(), machine.steady()), (1000.5, 1000.5));
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> *mut std::ffi::c_void;
    }

    /// Is a library of this name loaded in this process, as Windows has it?
    fn has_loaded(library: &str) -> bool {
        let name: Vec<u16> = library.encode_utf16().chain(std::iter::once(0)).collect();
        !unsafe { GetModuleHandleW(name.as_ptr()) }.is_null()
    }

    #[test]
    fn the_real_door_does_not_ask_the_graphics_card() {
        // On a PC with an NVIDIA card the library would answer these, and
        // to answer it opens the driver's library, which then stays in the
        // process for good.  (Without such a card this test has nothing to
        // catch: the library has no answer then anyway.)
        for question in ["gpu_live", "gpu_identity", "gpu_release", "gpu_", "gpu_anything_new"] {
            assert_eq!(Real.ask(question), None, "{question}");
        }
        assert!(!has_loaded("nvml.dll"), "the graphics driver's library is in this process");
        // (the check sees a library that is there)
        assert!(has_loaded("kernel32.dll") && !has_loaded("no-such-library.dll"));
    }
}
