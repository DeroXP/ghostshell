//! vispora-shell-try: the shell without its window, driven from a terminal
//! or a script.
//!
//!     vispora-shell-try --backend <program> [--arg <argument>]... [--cwd <folder>]
//!                       [--appdata <folder>] [--version <v>]
//!                       [--ready-within <seconds>] [--quit-within <seconds>] [--calls-wait <seconds>]
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
    if arguments.len() == 4 && arguments[0] == "--pack" {
        let Some(way) = pack::Way::named(&arguments[3]) else {
            eprintln!("{} is no way to squeeze: plain, mszip, xpress-huff or lzms", arguments[3]);
            std::process::exit(2);
        };
        let made = pack::make(&arguments[1], vispora_shell_core::APP_VERSION, "VisporaBackend.exe", way)
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
