//! The graphics card's reader, from the shell's side: its program is
//! started, asked, and ended.
//!
//! The program is VisporaGpuReader (src/bin/gpu_reader.rs), and
//! moved/card.rs says when it runs and why it is a program of its own.
//! Here: how.
//!
//! * **Where it is started from.**  The shell runs with administrator
//!   rights, and what it starts runs with them.  So the reader is never
//!   started by a path beside the shell's exe, which is in a folder any
//!   program of the person's can write to.  It is carried in the pack
//!   beside the backend (`IN_THE_PACK`) and started from the folder the
//!   pack was unpacked into: the administrators' own, checked against the
//!   exe and held (backend.rs).  A trial, which has no rights, starts the
//!   one that was built beside its own tool.
//! * **What it is given**: a pipe to ask it through and a pipe for its
//!   answers, and nothing else of the shell's (win::start_with_pipes).
//!   Not the listening socket, no port, no named pipe, no file, no word on
//!   its command line.
//! * **It never outlives the shell**: its input ends when the shell is
//!   gone, and it is in a job that Windows ends with the shell.
//! * **A reading never waits for it longer than it is given.**  Its
//!   answers are read by a thread of their own, so the one who asks waits
//!   for the answer with a limit, and without one for nothing.

use std::io::{BufRead, Read, Write};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::json::Json;
use crate::moved::{Reader, Readers};
use crate::win;

/// The reader's program.
pub const PROGRAM: &str = "VisporaGpuReader.exe";
/// Where it is in a pack, and so in the folder a pack is unpacked into:
/// beside the app's own copy of the C runtime it needs, which is the one
/// Windows then finds for it.
pub const IN_THE_PACK: &str = "_internal\\VisporaGpuReader.exe";
/// How long a reader that was ended is waited for until it is gone.
pub const GONE_WITHIN: Duration = Duration::from_secs(5);
/// An answer is one line, and never longer than this.
const LONGEST_ANSWER: u64 = 64 * 1024;

/// The reader's program at one place.
pub struct OwnProgram {
    program: String,
}

impl OwnProgram {
    /// `program`: the full path of the reader's exe.
    pub fn at(program: impl Into<String>) -> Arc<OwnProgram> {
        Arc::new(OwnProgram { program: program.into() })
    }
}

impl Readers for OwnProgram {
    fn start(&self) -> Result<Arc<dyn Reader>, String> {
        let (process, to_it, from_it) = win::start_with_pipes(&self.program)?;
        let (lines, answers) = std::sync::mpsc::channel::<String>();
        // What it says is read here, a line at a time, until its output
        // ends: which it does when the program has ended, and not before.
        let reading = std::thread::Builder::new().name("gpu-reader-answers".to_string()).spawn(move || {
            let mut from_it = std::io::BufReader::new(from_it);
            loop {
                let mut line = String::new();
                match from_it.by_ref().take(LONGEST_ANSWER).read_line(&mut line) {
                    Ok(read) if read > 0 && line.ends_with('\n') => {
                        if lines.send(line).is_err() {
                            break;
                        }
                    }
                    // (the end, a line without an end, or one that is too long: no more answers)
                    _ => break,
                }
            }
        });
        if let Err(why) = reading {
            process.end(GONE_WITHIN);
            return Err(format!("no thread for its answers: {why}"));
        }
        Ok(Arc::new(Running { process, asking: Mutex::new((to_it, answers)) }))
    }
}

/// What a line of the reader's says.  The helper's answer is
/// {"ok": true, "data": {...}} or {"ok": false, "err": "..."}: the reading,
/// or None for "there is none".  Anything else is no answer of the
/// helper's, and is said to be none: it is a reader that does not work,
/// which is not the same as a PC without a card.
fn what_it_said(line: &str) -> Result<Option<Json>, String> {
    let said = Json::parse(line.trim_end()).map_err(|why| format!("its answer could not be read: {why}"))?;
    match said.get("ok") {
        Some(Json::Bool(true)) => Ok(said.get("data").cloned()),
        Some(Json::Bool(false)) => Ok(None),
        _ => Err("its answer could not be read: it is not the helper's".to_string()),
    }
}

/// A reader that runs.
struct Running {
    process: win::Piped,
    /// The end to ask it through, and its answers as they come.  One
    /// question at a time.
    asking: Mutex<(std::fs::File, Receiver<String>)>,
}

impl Reader for Running {
    fn read(&self, within: Duration) -> Result<Option<Json>, String> {
        let mut asking = self.asking.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let (to_it, answers) = &mut *asking;
        // (an answer nobody waited for any more is not this question's)
        while answers.try_recv().is_ok() {}
        to_it.write_all(b"\n").map_err(|why| format!("it could not be asked: {why}"))?;
        let line = answers.recv_timeout(within).map_err(|why| match why {
            RecvTimeoutError::Timeout => format!("no answer within {} ms", within.as_millis()),
            RecvTimeoutError::Disconnected => "it has ended".to_string(),
        })?;
        what_it_said(&line)
    }

    fn end(&self) -> bool {
        self.process.end(GONE_WITHIN)
    }

    fn pid(&self) -> u32 {
        self.process.pid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_line_of_the_readers_is_the_helpers_answer_or_no_answer_at_all() {
        // A reading; and the helper saying that it has none.
        let reading = what_it_said("{\"ok\":true,\"data\":{\"temp_c\":61,\"fan_pct\":null}}\r\n").unwrap().unwrap();
        assert_eq!((reading["temp_c"].as_int(), &reading["fan_pct"]), (Some(61), &Json::Null));
        assert_eq!(what_it_said("{\"ok\":false,\"err\":\"no NVIDIA driver library (nvml.dll) on this PC\"}\n"), Ok(None));
        assert_eq!(what_it_said("{\"ok\":true}"), Ok(None), "an answer without its reading");
        // What is not the helper's answer is not taken for "this PC has no
        // card": text that is no JSON, and JSON that says nothing of "ok",
        // or says it in another way than the helper does.
        for other in ["", "Microsoft Windows [Version 10.0]", "{", "{}", "61", "[true]", "null", "{\"ok\":\"true\"}", "{\"ok\":1,\"data\":{\"temp_c\":61}}",
                      "{\"ok\":null}", "{\"data\":{\"temp_c\":61}}"] {
            let said = what_it_said(other);
            assert!(said.as_ref().is_err_and(|why| why.starts_with("its answer could not be read")), "{other:?}: {said:?}");
        }
    }
}

