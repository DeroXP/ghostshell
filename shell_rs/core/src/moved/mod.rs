//! What has moved from the backend into the shell (step 4 of
//! docs/rust-port.md: the backend, one module at a time).
//!
//! A call of the page that a module here takes is answered by the shell
//! itself, from readings the shell takes itself, and does not reach the
//! backend.  The backend's own route stays as it was.  It answers when the
//! app is started as it always was; and behind the shell it answers every
//! call the shell does not take: all of them when the shell was told to
//! leave everything to the backend, and otherwise whatever of a moved path
//! is not what the page sends (another method, a body Python stops at
//! before it does anything).  So for those the answer is the backend's own
//! by construction, and for the ones the shell takes it is the same answer,
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

/// Everything that has moved, with the door it reads the machine through.
pub struct Moved {
    door: Arc<dyn Readings>,
    pub perf: Arc<perf::Sampler>,
}

impl Moved {
    /// `threads`: may what has moved run threads of its own (the sampler's)?
    /// Not where a test moves the clock by hand and takes every reading
    /// itself (`perf::Sampler::step`).
    pub fn new(door: Arc<dyn Readings>, threads: bool) -> Arc<Moved> {
        Arc::new(Moved { door, perf: perf::Sampler::new(threads) })
    }

    /// The shell's own answer to a call of the page, or None: the call is
    /// the backend's, and is passed on as every call was before step 4.
    pub fn answer(&self, request: &Request) -> Option<Response> {
        if request.path().starts_with("/api/perf/") {
            return self.perf.answer(&self.door, request);
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

/// What Python's `int(text)` gives for a text, or None where it raises:
/// blanks around it, one sign, digits, and single underscores between
/// digits.  (Digits of other scripts, which Python also reads, are not read
/// here: no page of the app writes a number that way.)
pub fn py_int(text: &str) -> Option<i128> {
    let text = text.trim();
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    let mut value: i128 = 0;
    for letter in digits.chars().filter(|letter| *letter != '_') {
        value = value.checked_mul(10)?.checked_add(letter.to_digit(10)? as i128)?;
    }
    Some(if negative { -value } else { value })
}

/// What Flask's `_qint(name, default)` gives (app.py): the number in the
/// query, or `default` for anything that is not one.
pub fn query_int(target: &str, name: &str, default: i128) -> i128 {
    query_value(target, name).and_then(|text| py_int(&text)).unwrap_or(default)
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
                               ("0", Some(0)), ("-0", Some(0)), ("\t12\n", Some(12)), ("100000000000000000000000000000", Some(100_000_000_000_000_000_000_000_000_000)),
                               ("", None), (" ", None), ("abc", None), ("1.5", None), ("1e3", None), ("--1", None), ("+-1", None), ("- 1", None),
                               ("1_", None), ("_1", None), ("1__0", None), ("0x10", None), ("1 0", None), ("+", None), ("-", None)] {
            assert_eq!(py_int(text), wanted, "{text:?}");
        }
        // More digits than this program counts with: not a number here.
        assert_eq!(py_int(&"9".repeat(60)), None);
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
            assert_eq!(query_int(target, "seconds", 30), wanted, "{target}");
        }
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
        // A question it does not have, and one that is an action's name:
        // no answer, and nothing done (the door for questions has no
        // actions behind it).
        assert_eq!(Real.ask("no_such_question"), None);
        assert_eq!(Real.ask("purge_standby_list"), None);
        assert_eq!(Real.ask("trim_working_sets"), None);
        assert!(Real.now() > 1_700_000_000.0);
    }
}
