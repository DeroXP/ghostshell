//! vispora-shell-try: the shell without its window, driven from a terminal
//! or a script.
//!
//!     vispora-shell-try --backend <program> [--arg <argument>]... [--cwd <folder>]
//!                       [--appdata <folder>] [--version <v>] [--readings real] [--card-reader <program>]
//!                       [--ready-within <seconds>] [--quit-within <seconds>] [--calls-wait <seconds>]
//!
//! With `--readings real` the shell answers the calls that have moved from
//! the backend itself (src/moved/: they only read; the graphics card's
//! reading is asked of the backend).  Without it every call is the backend's.
//! With `--card-reader <program>` as well, the graphics card is read by
//! that program (the reader that was built beside this tool:
//! VisporaGpuReader.exe), started while a reading is wanted, and by the
//! backend only where it is away or has none.  It reads this PC's card,
//! and only reads.
//!
//! It starts the backend it is told to start (and only that: there is no
//! default), then reads one JSON object per line from standard input and
//! answers each with one line:
//!
//!     {"do": "call", "method": "GET", "target": "/api/dashboard", "token": true}
//!         a request as the page would make it; "headers" and "body" too.
//!         With "token": true the run's secret is added, as the page adds it.
//!     {"do": "wait", "for": "ready", "within": 30}      ready | down | gone
//!     {"do": "state"}        where the shell is, its backend's process id, what it has said
//!     {"do": "token"}        the run's secret (this is a trial tool)
//!     {"do": "card", "times": 1000}
//!     {"do": "monitor", "times": 20, "every_ms": 1000}
//!         that many passes of the hardware monitor, one every so often, each
//!         timed (its ping goes where the last start call of the page said)
//!         read the graphics card that many times the way what has moved
//!         reads it (with --readings real: it asks the backend): how long
//!         one took, and what the last one said
//!     {"do": "start_other", "program": "...", "arguments": ["..."]}
//!         start another program the way the shell starts everything but its backend
//!     {"do": "quit"}         quit as the app would, answer, and end
//!     {"do": "crash"}        end at once without quitting, as a shell that died would
//!
//! At the end of its input it quits as well.  It is for trying the shell
//! against a stand-in backend (tests/shell_backend_stand_in.py) and is never
//! shipped.  Never point it at the real backend: that one changes the PC.

use std::io::{BufRead, Write};
use std::sync::Arc;
use std::time::{Duration, Instant};

use vispora_shell_core::http::Request;
use vispora_shell_core::json::Json;
use vispora_shell_core::platform::Command;
use vispora_shell_core::shell::Phase;
use vispora_shell_core::moved::{self, Moved};
use vispora_shell_core::{pack, token, tray, win, Settings, Shell};

fn text_of(phase: &Phase) -> String {
    match phase {
        Phase::Starting => "starting".into(),
        Phase::Ready => "ready".into(),
        Phase::Down(why) => format!("down: {why}"),
        Phase::Quitting => "quitting".into(),
        Phase::Gone => "gone".into(),
    }
}

fn millis(time: Option<Duration>) -> Json {
    time.map_or(Json::Null, |t| (t.as_secs_f64() * 1000.0).into())
}

fn state_of(shell: &Shell) -> Json {
    let (ready, quit) = shell.timings();
    Json::object([
        ("phase", text_of(&shell.phase()).into()),
        ("backend_pid", shell.backend_pid().map_or(Json::Null, Into::into)),
        ("card_reader_pid", shell.card_reader_pid().map_or(Json::Null, Into::into)),
        ("card_readings_from", shell.card_readings_from().map_or(Json::Null, |from| Json::object([
            ("reader", from.reader.into()), ("backend", from.backend.into()), ("nowhere", from.nowhere.into()),
        ]))),
        ("port", (shell.port() as u32).into()),
        ("listen", shell.listening_handle().into()),
        ("starts", (shell.starts() as u32).into()),
        ("ready_ms", millis(ready)),
        ("quit_ms", millis(quit)),
        ("history", shell.history().into()),
    ])
}

fn settings_from(arguments: &[String]) -> Result<Settings, String> {
    let (mut program, mut args, mut cwd, mut appdata) = (None, Vec::new(), None, String::from("C:\\nowhere"));
    let mut numbers: Vec<(String, f64)> = Vec::new();
    let mut version = None;
    let mut words = arguments.iter();
    while let Some(word) = words.next() {
        let value = words.next().ok_or_else(|| format!("{word} needs a value"))?.clone();
        match word.as_str() {
            "--backend" => program = Some(value),
            "--arg" => args.push(value),
            "--cwd" => cwd = Some(value),
            "--appdata" => appdata = value,
            "--version" => version = Some(value),
            // (looked at in main: the shell is given its door once it is made)
            "--readings" if value == "real" => {}
            "--readings" => return Err("--readings takes \"real\" and nothing else".to_string()),
            "--card-reader" => {}
            "--ready-within" | "--quit-within" | "--calls-wait" => {
                numbers.push((word.clone(), value.parse().map_err(|_| format!("{word} needs a number of seconds"))?))
            }
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let program = program.ok_or("--backend <program> is needed: this tool has no backend of its own")?;
    let mut settings = Settings::new(Command { program, args, cwd }, &appdata);
    // (as the shell says where it is: this tool stands in for it)
    settings.shell_exe = std::env::current_exe().ok().map(|exe| exe.to_string_lossy().into_owned());
    if let Some(version) = version {
        settings.version = version;
    }
    for (name, seconds) in numbers {
        let time = Duration::from_secs_f64(seconds.max(0.0));
        match name.as_str() {
            "--ready-within" => settings.ready_within = time,
            "--quit-within" => settings.quit_within = time,
            _ => settings.calls_wait = time,
        }
    }
    Ok(settings)
}

fn answer(shell: &Arc<Shell>, asked: &Json) -> Result<Json, String> {
    match asked["do"].as_str() {
        Some("call") => {
            let mut request = Request::new(asked["method"].as_str().unwrap_or("GET"), asked["target"].as_str().ok_or("\"target\" is missing")?);
            if let Json::Object(headers) = &asked["headers"] {
                for (name, value) in headers {
                    request = request.with_header(name, &value.py_str());
                }
            }
            if asked["token"].truthy() {
                request = request.with_header(token::HEADER, shell.token().as_str());
            }
            if let Some(body) = asked["body"].as_str() {
                request = request.with_body(body);
            }
            let began = Instant::now();
            let got = shell.handle(&request);
            let took = began.elapsed();
            Ok(Json::object([
                ("status", (got.status as u32).into()),
                ("headers", Json::Object(got.headers.iter().map(|(name, value)| (name.to_lowercase(), value.as_str().into())).collect())),
                ("body", String::from_utf8_lossy(&got.body).into_owned().into()),
                ("bytes", (got.body.len() as u64).into()),
                ("ms", (took.as_secs_f64() * 1000.0).into()),
            ]))
        }
        Some("wait") => {
            let wanted = asked["for"].as_str().unwrap_or("ready").to_string();
            let within = match &asked["within"] {
                Json::Int(seconds) => *seconds as f64,
                Json::Float(seconds) => *seconds,
                _ => 30.0,
            };
            let within = Duration::from_secs_f64(within.max(0.0));
            let phase = shell.wait_for(within, |phase| match wanted.as_str() {
                "ready" => *phase == Phase::Ready,
                "down" => matches!(phase, Phase::Down(_)),
                "gone" => *phase == Phase::Gone,
                _ => true,
            });
            Ok(Json::object([("phase", text_of(&phase).into())]))
        }
        Some("state") => Ok(state_of(shell)),
        // A call of the shell's own to its backend, as the tray makes them.
        Some("own") => {
            let (method, path) = (asked["method"].as_str().unwrap_or("GET"), asked["path"].as_str().ok_or("\"path\" is missing")?);
            Ok(Json::object([("answer", shell.ask_backend(method, path).unwrap_or(Json::Null))]))
        }
        // Start another program the way the shell starts everything that
        // is not its backend: see win::start_without_handles.
        Some("start_other") => {
            let program = asked["program"].as_str().ok_or("\"program\" is missing")?;
            let arguments: Vec<String> = asked["arguments"].as_array().unwrap_or(&[]).iter().map(Json::py_str).collect();
            let started = win::start_without_handles(program, &arguments);
            Ok(Json::object([("ok", started.is_ok().into()), ("err", started.err().unwrap_or_default().into())]))
        }
        Some("token") => Ok(Json::object([("token", shell.token().as_str().into())])),
        Some("card") => {
            let moved = shell.moved.lock().unwrap_or_else(|p| p.into_inner()).clone().ok_or("this run was started without --readings real")?;
            let times = asked["times"].as_int().unwrap_or(1).clamp(1, 100_000) as usize;
            let (mut took, mut last) = (Vec::with_capacity(times), None);
            for _ in 0..times {
                let began = Instant::now();
                last = moved.card.live();
                took.push(began.elapsed().as_secs_f64() * 1000.0);
            }
            took.sort_by(|a, b| a.total_cmp(b));
            let last = last.map_or(Json::Null, |live| Json::object([
                ("core_mhz", Json::Int(live.core_mhz)), ("mem_mhz", Json::Int(live.mem_mhz)), ("temp_c", Json::Int(live.temp_c)), ("power_w", live.power_w.into()),
                ("gpu_util_pct", Json::Int(live.gpu_util_pct)), ("mem_util_pct", Json::Int(live.mem_util_pct)), ("fan_pct", Json::Int(live.fan_pct)),
            ]));
            Ok(Json::object([("ms", took[took.len() / 2].into()), ("slowest_ms", took[took.len() - 1].into()), ("reading", last)]))
        }
        Some("monitor") => {
            let moved = shell.moved.lock().unwrap_or_else(|p| p.into_inner()).clone().ok_or("this run was started without --readings real")?;
            let times = asked["times"].as_int().unwrap_or(1).clamp(1, 10_000) as usize;
            let every = Duration::from_millis(asked["every_ms"].as_int().unwrap_or(1000).clamp(0, 60_000) as u64);
            let mut took = Vec::with_capacity(times);
            for _ in 0..times {
                let began = Instant::now();
                moved.hw.step(&moved.card);
                let spent = began.elapsed();
                took.push(spent.as_secs_f64() * 1000.0);
                std::thread::sleep(every.saturating_sub(spent));
            }
            took.sort_by(|a, b| a.total_cmp(b));
            let passes = moved.hw.passes();
            Ok(Json::object([("ms", took[took.len() / 2].into()), ("slowest_ms", took[took.len() - 1].into()), ("latest", moved.hw.snapshot()["latest"].clone()),
                             ("kept", passes.kept.into()), ("dropped", passes.dropped.into()), ("asked_the_backend", passes.asked_the_backend.into())]))
        }
        _ => Err("unknown \"do\"".into()),
    }
}

fn main() {
    let arguments: Vec<String> = std::env::args_os().skip(1).map(|word| word.to_string_lossy().into_owned()).collect();
    // Where the shell finds the two folders it takes from Windows, and
    // nothing else: for the test that plants variables in the environment
    // and sees the answers stay the same.
    // What the tray would show for what the backend says ("none": it
        // has not said anything yet), and one of the tray's pictures as
        // hexadecimal bytes: for the tests that hold both against today's.
    if arguments.len() == 2 && arguments[0] == "--tray-look" {
        let state = match arguments[1].as_str() {
            "none" => None,
            said => Json::parse(said).ok().as_ref().and_then(tray::State::from_json),
        };
        println!("{}", tray::look(state.as_ref()).to_json());
        return;
    }
    if arguments.len() == 2 && arguments[0] == "--tray-picture" {
        let Some(icon) = tray::Icon::named(&arguments[1]) else { std::process::exit(2) };
        println!("{}", tray::picture(icon).iter().map(|byte| format!("{byte:02x}")).collect::<String>());
        return;
    }
    // Make a pack of a folder (the backend, built as a folder), and say
    // what a pack holds: how the exe's build gets what it carries.
    //
    //     --pack <folder> <pack> <way> [--reader <VisporaGpuReader.exe>]
    //
    // With --reader the pack also carries the graphics card's reader, which
    // is built with this tool and not with the app.
    let with_reader = arguments.len() == 6 && arguments[4] == "--reader";
    if (arguments.len() == 4 || with_reader) && arguments[0] == "--pack" {
        let Some(way) = pack::Way::named(&arguments[3]) else {
            eprintln!("{} is no way to squeeze: plain, mszip, xpress-huff or lzms", arguments[3]);
            std::process::exit(2);
        };
        let more: Vec<(String, String)> = if with_reader { vec![(vispora_shell_core::gpu_reader::IN_THE_PACK.to_string(), arguments[5].clone())] } else { Vec::new() };
        let made = pack::make_with(&arguments[1], vispora_shell_core::APP_VERSION, "VisporaBackend.exe", way, &more)
            .and_then(|bytes| std::fs::write(&arguments[2], &bytes).map_err(|e| format!("{}: {e}", arguments[2])));
        if let Err(why) = made {
            eprintln!("{why}");
            std::process::exit(1);
        }
    }
    if matches!(arguments.first().map(String::as_str), Some("--pack" | "--pack-says")) && arguments.len() >= 2 {
        let file = if arguments[0] == "--pack" { &arguments[2] } else { &arguments[1] };
        let said = std::fs::read(file).map_err(|e| format!("{file}: {e}")).and_then(|bytes| {
            let read = pack::Pack::read(&bytes)?;
            Ok(Json::object([
                ("version", read.version.as_str().into()),
                ("start", read.start.as_str().into()),
                ("files", (read.files.len() as u64).into()),
                ("bytes", (read.size() as u64).into()),
                ("packed", (bytes.len() as u64).into()),
                ("id", read.id.as_str().into()),
                ("reader", read.has(vispora_shell_core::gpu_reader::IN_THE_PACK).into()),
                ("kept_as", vispora_shell_core::backend::kept_name(&read).as_str().into()),
            ]))
        });
        match said {
            Ok(said) => println!("{said}"),
            Err(why) => {
                eprintln!("{why}");
                std::process::exit(1);
            }
        }
        return;
    }
    // What has moved, on a made-up machine: for the test that asks the
    // Python module and this the same things and compares the answers
    // (tests/test_moved_perf.py).  One JSON object per line, one line back:
    //
    //     {"do": "now", "time": 1000.0}                        what time it is: time has gone by
    //     {"do": "pcs_clock", "time": 1000.0}                  the PC's own clock is set to another time: none has gone by
    //     {"do": "answers", "question": "gpu_live", "answer": {...}}   what the library says (null: nothing)
    //     {"do": "answers", "question": "wmi", "with": {"namespace": "...", "query": "..."}, "answer": [...]}
    //                                     the same for a question that is asked with words of its own
    //     {"do": "grows", "question": "cpu_times", "each_second": {...}}
    //                                     the whole numbers in that answer count up as time goes by (null: they stand still)
    //     {"do": "backend_says", "answer": {...}}   what the backend says the card says (null: it does not answer)
    //     {"do": "backend_is_asked_back", "for": "/api/monitor/"}
    //                                     from now on, a call of the shell's to its backend that begins like this
    //                                     is put to whoever drives this tool, at the moment it is made: a line
    //                                     {"asks_the_backend": "<call>"} goes out, and the next line that comes
    //                                     in is the backend's answer ({"answer": ...}; null: it does not answer)
    //     {"do": "reader", "says": {...}}  what the card's own reader says from now on: the helper's answer, or one
    //                                     of "none" (it has no reading), "silent" (no answer in time), "garbled";
    //                                     and "cannot_start": true | false
    //     {"do": "install", "status": 200, "body": "..."}
    //                                     the call that starts a driver's installer, with what the backend answers it
    //     {"do": "tend"}                  what the thread that looks after the reader does in one round
    //     {"do": "step"}                  what the sampling thread does in one round
    //     {"do": "pass"}                  what the hardware monitor's thread does in one round
    //     {"do": "asked"}                 what the library, the reader and the backend were asked so far
    //     {"do": "call", "method": "GET", "target": "/api/perf/score", "body": "..."}   ("body_hex": "..." for bytes)
    //         the shell's own answer ({"status", "body"}), or {"passed_on": true}
    //
    // Nothing of the PC is read: the machine is the table and the clock,
    // and the backend is what it is told to say.
    // (with --no-reader the made-up shell has no reader for the card, as a
    // shell whose pack carries none: the card's reading is the backend's)
    if arguments == ["--moved"] || arguments == ["--moved", "--no-reader"] {
        let (machine, backend, readers) = (moved::Fake::new(), moved::FakeBackend::new(), moved::FakeReaders::new());
        let moved = Moved::new(machine.clone(), false);
        moved.asks_the_backend_with(backend.asker());
        if arguments.len() == 1 {
            moved.card.reads_with(Arc::new(readers.clone()));
        }
        let out = std::io::stdout();
        // (a line at a time, and the input is let go of in between: while a
        // step is taken, the made-up backend may read its answer from it)
        let next_line = || {
            let mut line = String::new();
            match std::io::stdin().read_line(&mut line) {
                Ok(read) if read > 0 => Some(line),
                _ => None,
            }
        };
        while let Some(line) = next_line() {
            if line.trim().is_empty() {
                continue;
            }
            let said = match Json::parse(&line) {
                Err(why) => Json::object([("error", why.into())]),
                Ok(asked) => match asked["do"].as_str() {
                    Some("now") => {
                        machine.at(match &asked["time"] {
                            Json::Int(whole) => *whole as f64,
                            Json::Float(time) => *time,
                            _ => 0.0,
                        });
                        Json::object([("ok", true.into())])
                    }
                    Some("pcs_clock") => {
                        machine.the_pcs_clock_is_set_to(match &asked["time"] {
                            Json::Int(whole) => *whole as f64,
                            Json::Float(time) => *time,
                            _ => 0.0,
                        });
                        Json::object([("ok", true.into())])
                    }
                    Some("answers") => {
                        let answer = asked.get("answer").filter(|answer| **answer != Json::Null).cloned();
                        let with: Vec<(&str, Json)> = match asked.get("with") {
                            Some(Json::Object(pairs)) => pairs.iter().map(|(name, value)| (name.as_str(), value.clone())).collect(),
                            _ => Vec::new(),
                        };
                        machine.answers_with(asked["question"].as_str().unwrap_or(""), &with, answer);
                        Json::object([("ok", true.into())])
                    }
                    Some("grows") => {
                        machine.grows(asked["question"].as_str().unwrap_or(""), asked.get("each_second").filter(|rates| **rates != Json::Null).cloned());
                        Json::object([("ok", true.into())])
                    }
                    Some("backend_is_asked_back") => {
                        let begins = asked["for"].as_str().unwrap_or("").to_string();
                        let usual = backend.clone();
                        backend.is_answered_by(Arc::new(move |call: &str| {
                            if !call.starts_with(&begins) {
                                return usual.says_now(call);
                            }
                            {
                                let mut out = std::io::stdout().lock();
                                let _ = writeln!(out, "{}", Json::object([("asks_the_backend", call.into())]));
                                let _ = out.flush();
                            }
                            let mut line = String::new();
                            let _ = std::io::stdin().read_line(&mut line);
                            Json::parse(&line).ok().and_then(|said| said.get("answer").filter(|answer| **answer != Json::Null).cloned())
                        }));
                        Json::object([("ok", true.into())])
                    }
                    Some("backend_says") => {
                        backend.says(asked.get("answer").filter(|answer| **answer != Json::Null).cloned());
                        Json::object([("ok", true.into())])
                    }
                    Some("reader") => {
                        if let Some(says) = asked.get("says") {
                            readers.reads(match says.as_str() {
                                Some("none") => moved::FakeReading::HasNone,
                                Some("silent") => moved::FakeReading::IsSilent,
                                Some("garbled") => moved::FakeReading::Garbles,
                                _ => moved::FakeReading::Says(says.clone()),
                            });
                        }
                        if let Some(cannot) = asked.get("cannot_start") {
                            readers.cannot_start(cannot.truthy().then_some("a trial said so"));
                        }
                        Json::object([("ok", true.into())])
                    }
                    Some("install") => {
                        let status = asked["status"].as_int().unwrap_or(200) as u16;
                        let body = asked["body"].as_str().unwrap_or("").as_bytes().to_vec();
                        let running = readers.clone();
                        let mut heard_with = Vec::new();
                        let answer = moved.card.around_a_driver_install(|| {
                            // (what the backend would see at the moment it hears of the call)
                            heard_with = running.running();
                            vispora_shell_core::http::Response::new(status, "application/json", body)
                        });
                        Json::object([("status", (answer.status as u32).into()),
                                      ("readers_when_the_backend_heard", Json::Array(heard_with.into_iter().map(Into::into).collect()))])
                    }
                    Some("tend") => Json::object([("still_one", moved.card.tend().into())]),
                    Some("asked") => {
                        let (reader, gave) = readers.asked();
                        let from = moved.card.from();
                        Json::object([
                            ("from_the_reader", from.reader.into()),
                            ("from_the_backend", from.backend.into()),
                            ("from_nowhere", from.nowhere.into()),
                            ("library", Json::Array(machine.asked().into_iter().map(Into::into).collect())),
                            ("waited", machine.waited().into()),
                            ("passes_kept", moved.hw.passes().kept.into()),
                            ("passes_dropped", moved.hw.passes().dropped.into()),
                            ("backend", Json::Array(backend.asked().into_iter().map(Into::into).collect())),
                            ("reader", reader.into()),
                            ("reader_gave", gave.into()),
                            ("readers_started", readers.started().into()),
                            ("readers_running", Json::Array(readers.running().into_iter().map(Into::into).collect())),
                        ])
                    }
                    Some("step") => {
                        moved.perf.step(&moved.card);
                        Json::object([("ok", true.into())])
                    }
                    Some("pass") => Json::object([("took_one", moved.hw.round(&moved.card).into())]),
                    Some("call") => {
                        // (a body that is no text is given as hexadecimal digits)
                        let body = match asked.get("body_hex").and_then(Json::as_str) {
                            Some(hex) => (0..hex.len() / 2).filter_map(|at| u8::from_str_radix(hex.get(at * 2..at * 2 + 2)?, 16).ok()).collect(),
                            None => asked["body"].as_str().unwrap_or("").as_bytes().to_vec(),
                        };
                        let request = Request::new(asked["method"].as_str().unwrap_or("GET"), asked["target"].as_str().unwrap_or("/")).with_body(body);
                        match moved.answer(&request) {
                            Some(answer) => Json::object([("status", (answer.status as u32).into()),
                                                          ("body", String::from_utf8_lossy(&answer.body).into_owned().into())]),
                            None => Json::object([("passed_on", true.into())]),
                        }
                    }
                    _ => Json::object([("error", "unknown \"do\"".into())]),
                },
            };
            let mut out = out.lock();
            let _ = writeln!(out, "{said}");
            let _ = out.flush();
        }
        return;
    }
    // Be there and do nothing, until the input ends.  For the trial that
    // needs a program running under the backend's name in the folder a run
    // left behind: a backend that outlived its shell.
    if arguments == ["--only-be-there"] {
        let mut line = String::new();
        while std::io::stdin().read_line(&mut line).is_ok_and(|read| read > 0) {
            line.clear();
        }
        return;
    }
    if arguments == ["--where"] {
        println!("{}", Json::object([
            ("windows", win::windows_folder().unwrap_or_default().into()),
            ("administrators_folder", win::administrators_folder().unwrap_or_default().into()),
            ("all_users_folder", win::all_users_folder().unwrap_or_default().into()),
        ]));
        return;
    }
    let settings = match settings_from(&arguments) {
        Ok(settings) => settings,
        Err(why) => {
            eprintln!("{why}");
            std::process::exit(2);
        }
    };
    let shell = match Shell::new(settings, win::platform()) {
        Ok(shell) => shell,
        Err(why) => {
            eprintln!("{why}");
            std::process::exit(2);
        }
    };
    if arguments.windows(2).any(|pair| pair[0] == "--readings" && pair[1] == "real") {
        shell.reads_with(Arc::new(moved::Real), true);
    }
    if let Some(program) = arguments.windows(2).find(|pair| pair[0] == "--card-reader").map(|pair| pair[1].clone()) {
        if !shell.card_is_read_by(vispora_shell_core::gpu_reader::OwnProgram::at(program)) {
            eprintln!("--card-reader needs --readings real: without it nothing in the shell wants a reading");
            std::process::exit(2);
        }
    }
    let out = std::io::stdout();
    let say = |value: Json| {
        let mut out = out.lock();
        let _ = writeln!(out, "{value}");
        let _ = out.flush();
    };
    shell.start();
    say(Json::object([("started", true.into()), ("pid", std::process::id().into()), ("version", shell.settings.version.as_str().into())]));

    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let asked = match Json::parse(&line) {
            Ok(asked) => asked,
            Err(why) => {
                say(Json::object([("error", why.into())]));
                continue;
            }
        };
        match asked["do"].as_str() {
            Some("crash") => std::process::exit(3),
            Some("quit") => break,
            _ => say(answer(&shell, &asked).unwrap_or_else(|why| Json::object([("error", why.into())]))),
        }
    }
    let began = Instant::now();
    let code = shell.quit_and_wait("the trial is over");
    let mut last = state_of(&shell);
    last.set("exit", code.into());
    last.set("ms", (began.elapsed().as_secs_f64() * 1000.0).into());
    say(last);
    std::process::exit(code);
}
