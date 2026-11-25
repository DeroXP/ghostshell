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
//!   shell (tests/test_moved_perf.py holds that).
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
//!   card, and the card's reading is asked of the backend (`Card`), which
//!   reads the card the way it always did (its own copy of the library, or
//!   nvidia-smi).  It is the reading the Python modules take themselves, so
//!   a module that lives on it gives Python's answers at all times, also
//!   while a driver is installed.
//!
//! What has moved so far: the performance sampler (perf.rs).

pub mod perf;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::http::{Request, Response};
use crate::json::Json;

/// The door to the machine, for everything that has moved: questions only.
pub trait Readings: Send + Sync {
    /// What the native library answers to the question named `question`
    /// (native/README.md lists them), or None where it has no answer.
    fn ask(&self, question: &str) -> Option<Json>;
    /// The time, in seconds since 1970: what Python's `time.time()` gives.
    fn now(&self) -> f64;
}

/// The real machine.
pub struct Real;

impl Readings for Real {
    fn ask(&self, question: &str) -> Option<Json> {
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
        // `answer` is the library's door for questions.  Its other door,
        // for the two things that change the machine, is not used here or
        // anywhere in the shell.
        let said = Json::parse(&vispora_native::answer(&format!("{{\"q\":\"{question}\"}}"))).ok()?;
        match said.get("ok") {
            Some(Json::Bool(true)) => said.get("data").cloned(),
            _ => None,
        }
    }

    fn now(&self) -> f64 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |since| since.as_secs_f64())
    }
}

/// A made-up machine: it answers from a table, and its clock is set by hand.
#[derive(Default)]
pub struct Fake {
    answers: Mutex<HashMap<String, Json>>,
    clock: Mutex<f64>,
    asked: Mutex<Vec<String>>,
}

impl Fake {
    pub fn new() -> Arc<Fake> {
        Arc::new(Fake::default())
    }

    /// What the library answers to `question` from now on (None: nothing).
    pub fn answers(&self, question: &str, answer: Option<Json>) {
        let mut answers = self.answers.lock().unwrap_or_else(|p| p.into_inner());
        match answer {
            Some(answer) => answers.insert(question.to_string(), answer),
            None => answers.remove(question),
        };
    }

    /// What time it is from now on.
    pub fn at(&self, time: f64) {
        *self.clock.lock().unwrap_or_else(|p| p.into_inner()) = time;
    }

    /// Every question that was asked, in order.
    pub fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }
}

impl Readings for Fake {
    fn ask(&self, question: &str) -> Option<Json> {
        self.asked.lock().unwrap_or_else(|p| p.into_inner()).push(question.to_string());
        self.answers.lock().unwrap_or_else(|p| p.into_inner()).get(question).cloned()
    }

    fn now(&self) -> f64 {
        *self.clock.lock().unwrap_or_else(|p| p.into_inner())
    }
}

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

    /// The way to ask it (`Moved::asks_the_backend_with`).
    pub fn asker(self: &Arc<Self>) -> AskBackend {
        let backend = self.clone();
        Arc::new(move |path: &str| {
            backend.asked.lock().unwrap_or_else(|p| p.into_inner()).push(path.to_string());
            backend.says.lock().unwrap_or_else(|p| p.into_inner()).clone()
        })
    }
}

// ═════════════════════════════════════════════════════════════════════
// The graphics card: its reading is the backend's
// ═════════════════════════════════════════════════════════════════════

/// A way to ask the backend something with GET: its answer, or None where
/// there is no backend to ask or it did not answer "200" in time.
pub type AskBackend = Arc<dyn Fn(&str) -> Option<Json> + Send + Sync>;

/// The backend's call that gives the card's reading as the app reads it
/// (app.py, api_gpu_oc_live: `gpu_overclock.read_live_state()` and nothing
/// else).  It is a call of the GPU overclock page, and it only reads: none
/// of the overclock's own code has moved or is called from here.
pub const THE_BACKENDS_READING: &str = "/api/gpu/oc/live";

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

/// The card, for everything that has moved and reads it (see the top of
/// this file for why the shell does not read it itself).
pub struct Card {
    door: Arc<dyn Readings>,
    backend: Mutex<Option<AskBackend>>,
}

impl Card {
    fn new(door: Arc<dyn Readings>) -> Card {
        Card { door, backend: Mutex::new(None) }
    }

    pub fn now(&self) -> f64 {
        self.door.now()
    }

    /// What the card says now: the backend's reading of it.  None where
    /// there is no backend to ask (it is still starting, or gone), and
    /// where it has no reading either (its answer says so: no NVIDIA card,
    /// no driver, no nvidia-smi).
    pub fn live(&self) -> Option<Live> {
        let ask = self.backend.lock().unwrap_or_else(|p| p.into_inner()).clone()?;
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

/// Everything that has moved, with the door it reads the machine through.
pub struct Moved {
    pub card: Arc<Card>,
    pub perf: Arc<perf::Sampler>,
}

impl Moved {
    /// `threads`: may what has moved run threads of its own (the sampler's)?
    /// Not where a test moves the clock by hand and takes every reading
    /// itself (`perf::Sampler::step`).
    pub fn new(door: Arc<dyn Readings>, threads: bool) -> Arc<Moved> {
        Arc::new(Moved { card: Arc::new(Card::new(door)), perf: perf::Sampler::new(threads) })
    }

    /// How the backend is asked for what the shell does not read itself
    /// (the graphics card).
    pub fn asks_the_backend_with(&self, ask: AskBackend) {
        *self.card.backend.lock().unwrap_or_else(|p| p.into_inner()) = Some(ask);
    }

    /// The shell's own answer to a call of the page, or None: the call is
    /// the backend's, and is passed on as every call was before step 4.
    pub fn answer(&self, request: &Request) -> Option<Response> {
        if request.path().starts_with("/api/perf/") {
            return self.perf.answer(&self.card, request);
        }
        None
    }
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
    fn the_cards_reading_is_the_backends_read_as_python_reads_it() {
        let machine = Fake::new();
        machine.at(1000.5);
        let moved = Moved::new(machine.clone(), false);
        // No backend to ask: no reading.  And the card is not asked through
        // the door, whatever the made-up machine would say: the shell does
        // not open the driver's library.
        machine.answers("gpu_live", Some(Json::object([("temp_c", 61.into())])));
        assert_eq!((moved.card.live(), moved.card.now()), (None, 1000.5));
        assert!(machine.asked().is_empty(), "the card was asked through the shell's own door: {:?}", machine.asked());
        // A backend to ask: its reading, asked for with its own call.
        let says = Arc::new(Mutex::new(None::<Json>));
        let (answer, asked) = (says.clone(), Arc::new(Mutex::new(Vec::new())));
        let seen = asked.clone();
        moved.asks_the_backend_with(Arc::new(move |path: &str| {
            seen.lock().unwrap().push(path.to_string());
            answer.lock().unwrap().clone()
        }));
        let backend_says = |text: Option<&str>| {
            *says.lock().unwrap() = text.map(|text| Json::parse(text).unwrap());
            moved.card.live()
        };
        let whole = r#"{"ok": true, "core_mhz": 2100, "mem_mhz": 9501, "temp_c": 61, "power_w": 215.44, "power_limit_w": 320.0, "gpu_util_pct": 97,
                        "mem_util_pct": 40, "fan_pct": 55, "core_app_mhz": 2100, "mem_app_mhz": 9501, "core_max_mhz": 2200, "mem_max_mhz": 9800}"#;
        assert_eq!(backend_says(Some(whole)), Some(Live { core_mhz: 2100, mem_mhz: 9501, temp_c: 61, power_w: 215.44, gpu_util_pct: 97, mem_util_pct: 40, fan_pct: 55 }));
        assert_eq!(*asked.lock().unwrap(), [THE_BACKENDS_READING]);
        // What is not there, nothing and false are 0, and true is 1; a
        // number with a fraction is cut; whole watts are watts.
        assert_eq!(backend_says(Some(r#"{"ok": true, "core_mhz": 1800.9, "temp_c": 70.0, "power_w": 100, "gpu_util_pct": null, "fan_pct": false, "mem_util_pct": true}"#)),
                   Some(Live { core_mhz: 1800, mem_mhz: 0, temp_c: 70, power_w: 100.0, gpu_util_pct: 0, mem_util_pct: 1, fan_pct: 0 }));
        assert_eq!(backend_says(Some(r#"{"ok": 1}"#)), Some(Live { core_mhz: 0, mem_mhz: 0, temp_c: 0, power_w: 0.0, gpu_util_pct: 0, mem_util_pct: 0, fan_pct: 0 }));
        // The backend has no reading either, or says something else, or nothing.
        for none in [Some(r#"{"ok": false, "err": "No NVIDIA GPU / nvidia-smi missing", "temp_c": 99}"#), Some(r#"{"temp_c": 99}"#), Some(r#"{"ok": 0, "temp_c": 9}"#),
                     Some(r#"[1, 2]"#), Some(r#""warm""#), None] {
            assert_eq!(backend_says(none), None, "{none:?}");
        }
        assert!(machine.asked().is_empty());
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
