//! The graphics card's reader, started for real: its own program
//! (VisporaGpuReader.exe, as cargo built it for this test), and two of
//! Windows' own programs standing in for a reader that says nothing, or
//! says something that is no answer.
//!
//! What is read is this PC's graphics card, through the driver's own
//! library, and nothing else; nothing is changed.  On a PC without an
//! NVIDIA card the reader says that it has no reading, which is an answer
//! too.
#![cfg(windows)]

use std::io::{BufRead, Read, Write};
use std::sync::Arc;
use std::time::{Duration, Instant};

use vispora_shell_core::gpu_reader::{OwnProgram, GONE_WITHIN, IN_THE_PACK, PROGRAM};
use vispora_shell_core::moved::{Reader, Readers};
use vispora_shell_core::win;

const READER: &str = env!("CARGO_BIN_EXE_VisporaGpuReader");

/// One of these tests at a time.  They start programs, some of them in
/// Rust's own way, which hands a program everything that is inheritable at
/// that moment: side by side, one test's helper would get the listening
/// socket another test is looking at.
fn alone() -> std::sync::MutexGuard<'static, ()> {
    static ONE_AT_A_TIME: std::sync::Mutex<()> = std::sync::Mutex::new(());
    ONE_AT_A_TIME.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn of_windows(program: &str) -> String {
    format!("{}\\System32\\{program}", win::windows_folder().unwrap())
}

/// Is a program running under this process number, with this name?
fn runs(pid: u32, name: &str) -> bool {
    let listed = std::process::Command::new(of_windows("tasklist.exe")).args(["/fi", &format!("PID eq {pid}"), "/fo", "csv", "/nh"]).output().unwrap();
    String::from_utf8_lossy(&listed.stdout).to_lowercase().contains(&format!("\"{}\",\"{pid}\"", name.to_lowercase()))
}

fn start(program: &str) -> Arc<dyn Reader> {
    OwnProgram::at(program).start().unwrap_or_else(|why| panic!("{program}: {why}"))
}

#[test]
fn the_reader_reads_this_pcs_card_and_is_gone_when_it_is_ended() {
    let _alone = alone();
    assert!(READER.ends_with(PROGRAM) && IN_THE_PACK.ends_with(PROGRAM), "{READER}");
    let reader = start(READER);
    assert!(reader.pid() != std::process::id() && runs(reader.pid(), PROGRAM), "the reader is not a program of its own");
    let began = Instant::now();
    let first = reader.read(Duration::from_secs(10)).expect("the reader answers");
    let first_took = began.elapsed();
    match &first {
        // A card: what the helper says of it, with its temperature.
        Some(said) => {
            assert!(said.is_object() && said["temp_c"].as_int().is_some_and(|degrees| (0..150).contains(&degrees)), "{said}");
            // Every later answer is there at once, and is a reading as well.
            let began = Instant::now();
            for _ in 0..50 {
                let again = reader.read(Duration::from_secs(1)).unwrap().expect("a card that answered answers again");
                assert!(again["temp_c"].as_int().is_some());
            }
            assert!(began.elapsed() < Duration::from_secs(2), "50 readings took {:?}", began.elapsed());
        }
        // No NVIDIA card or driver: it says that it has none, and again.
        None => assert_eq!(reader.read(Duration::from_secs(1)), Ok(None)),
    }
    assert!(first_took < Duration::from_secs(10), "{first_took:?}");
    // Ended: seen to be gone, at once, and it answers nothing any more.
    let began = Instant::now();
    assert!(reader.end(), "it was not seen to go");
    assert!(began.elapsed() < Duration::from_secs(2) && !runs(reader.pid(), PROGRAM), "it is still there {:?} after it was ended", began.elapsed());
    assert!(reader.read(Duration::from_secs(1)).is_err());
    assert!(reader.end(), "ending it twice");
}

#[test]
fn a_reader_that_is_let_go_of_is_ended() {
    let _alone = alone();
    let reader = start(READER);
    let pid = reader.pid();
    assert!(runs(pid, PROGRAM));
    drop(reader);
    assert!(!runs(pid, PROGRAM), "nobody holds the reader any more, and it still runs");
}

#[test]
fn a_reader_that_says_nothing_in_time_gives_no_reading_in_that_time() {
    let _alone = alone();
    // (Windows' `sort` reads everything before it says anything)
    let silent = start(&of_windows("sort.exe"));
    let began = Instant::now();
    let said = silent.read(Duration::from_millis(300));
    let took = began.elapsed();
    assert_eq!(said, Err("no answer within 300 ms".to_string()));
    assert!(took >= Duration::from_millis(300) && took < Duration::from_millis(900), "{took:?}");
    // It can be ended while somebody waits for it: the wait is over at once.
    let waiting = {
        let silent = silent.clone();
        std::thread::spawn(move || {
            let began = Instant::now();
            (silent.read(Duration::from_secs(30)), began.elapsed())
        })
    };
    std::thread::sleep(Duration::from_millis(200));
    assert!(silent.end());
    let (said, took) = waiting.join().unwrap();
    assert_eq!(said, Err("it has ended".to_string()));
    assert!(took < Duration::from_secs(3), "the one who waited went on waiting for {took:?}");
    assert!(!runs(silent.pid(), "sort.exe"));
}

#[test]
fn what_is_no_answer_of_the_helpers_is_not_taken_for_one() {
    let _alone = alone();
    // (Windows' `cmd` answers a line with its own greeting: text, and no answer)
    let other = start(&of_windows("cmd.exe"));
    let said = other.read(Duration::from_secs(5));
    assert!(said.as_ref().is_err_and(|why| why.starts_with("its answer could not be read")), "{said:?}");
    assert!(other.end());
}

#[test]
fn a_program_that_is_not_there_or_is_not_named_whole_is_not_started() {
    let _alone = alone();
    for (program, why) in [("C:\\nowhere\\VisporaGpuReader.exe", "it could not be started"), ("VisporaGpuReader.exe", "not a full path"),
                           ("..\\VisporaGpuReader.exe", "not a full path"), ("", "not a full path")] {
        let said = OwnProgram::at(program).start().err().unwrap_or_else(|| panic!("{program:?} was started"));
        assert!(said.contains(why), "{program:?}: {said}");
    }
}

#[test]
fn the_program_gets_its_two_pipe_ends_and_nothing_else_of_this_programs() {
    let _alone = alone();
    // The shell's listening socket is inheritable on purpose (its backend
    // inherits it).  A program started with pipes must not get it: if it
    // did, the port would go on being listened on after the shell let go
    // of it, for as long as that program lives.
    let knocks = |port: u16| std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)).is_ok();

    // First, that this is how it would show: a program started with
    // everything inheritable handed to it keeps the port open.
    let listening = win::platform().listen().unwrap();
    let port = listening.port();
    assert!(knocks(port));
    let mut heir = std::process::Command::new(of_windows("sort.exe")).stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::null()).spawn().unwrap();
    drop(listening);
    let kept_open = knocks(port);
    heir.kill().unwrap();
    heir.wait().unwrap();
    assert!(kept_open, "the listening socket is not inherited by a program that inherits everything: this test would show nothing");
    assert!(!knocks(port), "the port is still open with every program gone that could hold it");

    // The same with a program started with pipes: the port is closed the
    // moment this program lets go of it.
    let listening = win::platform().listen().unwrap();
    let port = listening.port();
    let (program, to_it, mut from_it) = win::start_with_pipes(&of_windows("sort.exe")).unwrap();
    drop(listening);
    assert!(!program.is_gone());
    assert!(!knocks(port), "a program started with pipes inherited the shell's listening socket");

    // And its two ends are its own: what is written to the one it reads,
    // and when this program's end is closed its input ends, which nothing
    // else keeps open.  (`sort` then writes what it read, and ends.)
    let mut to_it = to_it;
    to_it.write_all(b"b\r\na\r\n").unwrap();
    drop(to_it);
    let mut sorted = String::new();
    from_it.read_to_string(&mut sorted).unwrap();
    assert_eq!(sorted, "a\r\nb\r\n");
    let began = Instant::now();
    while !program.is_gone() && began.elapsed() < Duration::from_secs(5) {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(program.is_gone(), "its input ended and it did not");
    assert!(program.end(GONE_WITHIN));
}

#[test]
fn a_program_with_pipes_starts_in_its_own_folder_and_not_in_this_programs() {
    let _alone = alone();
    // (the folder a program is started in is one of the places Windows
    // looks for the libraries it asks for: the reader's is the folder its
    // pack was unpacked into, never the one the shell happens to be in)
    let (program, mut to_it, mut from_it) = win::start_with_pipes(&of_windows("cmd.exe")).unwrap();
    to_it.write_all(b"cd\r\nexit\r\n").unwrap();
    drop(to_it);
    let mut said = String::new();
    from_it.read_to_string(&mut said).unwrap();
    let (said, its_own, mine) = (said.to_lowercase(), of_windows("").trim_end_matches('\\').to_lowercase(), std::env::current_dir().unwrap().to_string_lossy().to_lowercase());
    assert!(said.lines().any(|line| line.trim() == its_own), "not started in {its_own}:\n{said}");
    assert!(mine != its_own && !said.contains(&mine), "started in this program's folder ({mine}):\n{said}");
    assert!(program.end(GONE_WITHIN));
}

#[test]
fn the_reader_ends_by_itself_when_its_input_ends_and_takes_nothing_from_its_input() {
    let _alone = alone();
    let (program, mut to_it, from_it) = win::start_with_pipes(READER).unwrap();
    let mut from_it = std::io::BufReader::new(from_it);
    // Whatever a line holds, the answer is the card's reading (or that
    // there is none): never anything the line asked for.
    let mut answers = Vec::new();
    for line in ["", "{\"q\":\"abi\"}", "{\"do\":\"purge_standby_list\"}", "{\"q\":\"gpu_release\",\"hold_s\":3600}", "C:\\Windows\\System32\\nvml.dll"] {
        to_it.write_all(format!("{line}\n").as_bytes()).unwrap();
        let mut answer = String::new();
        from_it.read_line(&mut answer).unwrap();
        let said = vispora_shell_core::json::Json::parse(answer.trim_end()).unwrap_or_else(|why| panic!("{answer:?}: {why}"));
        assert!(said.get("abi").is_none() && said["data"].get("abi").is_none() && said["data"].get("released").is_none(), "{line:?} was listened to: {answer}");
        answers.push(match said.get("ok") {
            Some(vispora_shell_core::json::Json::Bool(true)) => said["data"].get("temp_c").is_some(),
            _ => false,
        });
    }
    // (all of one kind: a card that answers, or none)
    assert!(answers.iter().all(|a| *a == answers[0]), "{answers:?}");
    // (the hold it was "asked" for did not happen: it still reads, if it ever did)
    drop(to_it);
    let began = Instant::now();
    while !program.is_gone() && began.elapsed() < Duration::from_secs(5) {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(program.is_gone(), "the reader's input ended and the reader did not");
    let mut rest = String::new();
    from_it.read_to_string(&mut rest).unwrap();
    assert_eq!(rest, "", "it said something nobody asked for");
}

#[test]
fn started_with_no_input_at_all_the_reader_ends_at_once_and_reads_nothing() {
    let _alone = alone();
    // (as a double click starts it, or anybody with a command line and words of their own)
    let began = Instant::now();
    let done = std::process::Command::new(READER).args(["--read", "C:\\somewhere\\nvml.dll", "gpu_release"]).stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped()).output().unwrap();
    assert!(done.status.success() && done.stdout.is_empty(), "{:?}: {:?}", done.status, String::from_utf8_lossy(&done.stdout));
    assert!(began.elapsed() < Duration::from_secs(5));
}
