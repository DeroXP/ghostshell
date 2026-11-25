//! The shell from the backend's start to its end: starting it, noticing that
//! it stopped, which request is answered where, and quitting.
//!
//! Nothing is started for real: the backend is a stand-in in this test
//! (common/mod.rs), and the limits are short, so that a backend that never
//! comes up or will not go is over in a fraction of a second.

#![cfg(windows)]

mod common;

use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use common::*;
use vispora_shell_core::http::Request;
use vispora_shell_core::json::Json;
use vispora_shell_core::shell::{Phase, NEVER_UP};
use vispora_shell_core::token;

fn down(phase: &Phase) -> bool {
    matches!(phase, Phase::Down(_))
}

// ═════════════════════════════════════════════════════════════════════
// Starting
// ═════════════════════════════════════════════════════════════════════

#[test]
fn the_backend_is_started_with_a_hand_over_and_asked_whether_it_is_up() {
    let rig = Rig::ready();
    assert_eq!(rig.stage.count(), 1);
    let backend = rig.stage.last();
    assert_eq!(backend.command, rig.shell.settings.backend);
    // The hand-over: the socket to serve on, the secret, and who the shell is.
    let given = Json::parse(&backend.handoff).unwrap();
    let Json::Object(fields) = &given else { panic!("{given}") };
    let mut names: Vec<&str> = fields.iter().map(|(name, _)| name.as_str()).collect();
    names.sort();
    assert_eq!(names, ["listen", "port", "shell_pid", "shell_started", "token"]);
    assert_eq!((given["shell_pid"].as_int(), given["shell_started"].as_int()), (Some(4242), Some(SHELL_STARTED as i128)));
    assert_eq!((given["listen"].as_int(), given["port"].as_int()), (Some(HANDLE as i128), Some(rig.shell.port() as i128)));
    assert!(backend.port >= 1024 && backend.port == rig.stage.port());
    assert_eq!(backend.token, rig.shell.token().as_str());
    assert_eq!(backend.token.len(), 43);
    assert_eq!(rig.shell.fragment(), format!("#t={}", backend.token));
    // Asked with the secret, like every call.
    let hello = &backend.seen.lock().unwrap()[0];
    assert_eq!((hello.method.as_str(), hello.target.as_str()), ("GET", "/api/shell/hello"));
    assert_eq!(hello.header(token::HEADER), Some(backend.token.as_str()));
    assert_eq!(rig.shell.backend_pid(), Some(backend.pid));
    let history = rig.shell.history().join("\n");
    assert!(history.contains("backend started (pid 1000") && history.contains("backend ready after"), "{history}");
    assert!(!history.contains(&backend.token), "the secret is in no log line");
    assert!(rig.shell.timings().0.is_some());
}

#[test]
fn the_page_is_there_at_once_and_calls_wait_for_a_backend_that_is_still_starting() {
    let slow = Behaviour { up_after: Duration::from_millis(500), ..Behaviour::default() };
    let rig = Rig::with(vec![slow], settings());
    let began = Instant::now();
    rig.shell.start();
    // The page and its files do not wait for anything.
    assert_eq!(rig.shell.handle(&Request::new("GET", "/")).status, 200);
    assert_eq!(rig.shell.handle(&Request::new("GET", "/static/js/app.js")).status, 200);
    assert!(began.elapsed() < Duration::from_millis(400), "{:?}", began.elapsed());
    assert_eq!(rig.shell.phase(), Phase::Starting);
    // A call that has to be passed on is held until the backend is up.
    let answer = rig.call("GET", "/api/echo");
    assert_eq!(answer.status, 200, "{}", text(&answer));
    assert!(began.elapsed() >= Duration::from_millis(450), "{:?}", began.elapsed());
    assert_eq!(rig.shell.phase(), Phase::Ready);
}

#[test]
fn a_call_does_not_wait_for_ever() {
    let mut settings = settings();
    settings.calls_wait = Duration::from_millis(200);
    let rig = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], settings);
    rig.shell.start();
    let began = Instant::now();
    let answer = rig.call("GET", "/api/echo");
    assert_eq!(answer.status, 503);
    assert_eq!(answer.to_json()["err"].as_str(), Some("Vispora is still starting. Try again in a moment."));
    assert!(began.elapsed() >= Duration::from_millis(190) && began.elapsed() < Duration::from_secs(2), "{:?}", began.elapsed());
}

#[test]
fn a_backend_that_never_comes_up_is_ended_started_again_and_then_given_up_on() {
    let mut settings = settings();
    settings.ready_within = Duration::from_millis(150);
    let rig = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], settings);
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(10), down);
    assert_eq!(phase, Phase::Down(NEVER_UP.into()));
    // The first start and two more (settings.restarts), each ended.
    assert_eq!(rig.stage.count(), 3);
    for n in 0..3 {
        assert!(!rig.stage.backend(n).alive() && *rig.stage.backend(n).was_ended_by_force.lock().unwrap(), "backend {n}");
    }
    // Calls say so at once, in the same words.
    let began = Instant::now();
    let answer = rig.call("GET", "/api/echo");
    assert_eq!(answer.status, 503);
    assert!(answer.to_json()["err"].as_str().unwrap().starts_with("Vispora's background program does not start."));
    assert!(began.elapsed() < Duration::from_millis(500));
    let history = rig.shell.history().join("\n");
    assert!(history.contains("the backend did not come up (it did not answer within 0 s), 3 times in a row"), "{history}");
    // ... and it can still be quit.
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    assert_eq!(rig.shell.phase(), Phase::Gone);
    assert_eq!(rig.stage.count(), 3, "nothing is started after that");
}

#[test]
fn a_backend_of_another_build_is_not_this_shells() {
    let rig = Rig::with(vec![Behaviour { version: "1.0.0".into(), ..Behaviour::default() }], settings());
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(10), down);
    assert_eq!(phase, Phase::Down("Vispora's files do not belong together. Download Vispora again.".into()));
    assert_eq!(rig.stage.count(), 1, "starting it again would not make it another build");
    assert!(*rig.stage.last().was_ended_by_force.lock().unwrap());
    assert!(rig.shell.history().join("\n").contains("the backend is version \"1.0.0\" and the shell is \"7.7.7\""));
    assert_eq!(rig.call("GET", "/api/echo").status, 503);
}

#[test]
fn a_backend_that_cannot_be_started_is_said_and_not_tried_for_ever() {
    let rig = Rig::with(vec![], settings());
    *rig.stage.cannot_start.lock().unwrap() = Some("Access is denied. (os error 5)".into());
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(10), down);
    let Phase::Down(why) = phase else { panic!("{phase:?}") };
    assert_eq!(why, "Vispora could not start its background program (Access is denied. (os error 5)). Close it and open it again.");
    assert_eq!(rig.stage.count(), 0);
    std::thread::sleep(Duration::from_millis(100));
    assert!(down(&rig.shell.phase()));
}

// ═════════════════════════════════════════════════════════════════════
// Staying up
// ═════════════════════════════════════════════════════════════════════

#[test]
fn a_backend_that_dies_is_started_again_with_the_same_secret() {
    let dies = Behaviour { ends_after: Some((Duration::from_millis(300), 3)), ..Behaviour::default() };
    let rig = Rig::with(vec![dies, Behaviour::default()], settings());
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert_eq!(rig.call("GET", "/api/echo").status, 200);
    assert!(wait(|| rig.stage.count() == 2, 10.0), "{:?}", rig.shell.history());
    assert_eq!(rig.wait_ready(), Phase::Ready);
    let (first, second) = (rig.stage.backend(0), rig.stage.backend(1));
    assert!(!first.alive() && second.alive());
    assert_eq!(first.token, second.token, "the page keeps its secret");
    assert_ne!(first.pid, second.pid);
    // The page goes on as if nothing had happened.
    let answer = rig.call("GET", "/api/echo");
    assert_eq!(answer.status, 200);
    assert_eq!(second.calls().len(), 1);
    assert!(rig.shell.history().join("\n").contains("the backend stopped (exit code 3): starting it again"));
    assert!(rig.exits().is_empty(), "the shell did not go with it");
}

#[test]
fn a_backend_that_keeps_dying_is_given_up_on() {
    let dies = Behaviour { ends_after: Some((Duration::from_millis(120), 1)), ..Behaviour::default() };
    let rig = Rig::with(vec![dies], settings());
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(10), down);
    assert_eq!(phase, Phase::Down("Vispora's background program keeps stopping. Close Vispora and open it again.".into()));
    assert_eq!(rig.stage.count(), 3);
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(rig.stage.count(), 3, "and that is the end of it");
    assert!(rig.exits().is_empty());
}

#[test]
fn a_backend_that_leaves_on_its_own_takes_the_shell_with_it() {
    // Exit code 0 without having been told to quit: it meant to go (the
    // updater has been started and is about to replace the app).
    let leaves = Behaviour { ends_after: Some((Duration::from_millis(250), 0)), ..Behaviour::default() };
    let rig = Rig::with(vec![leaves], settings());
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert_eq!(rig.shell.wait_for(Duration::from_secs(10), |phase| *phase == Phase::Gone), Phase::Gone);
    assert!(wait(|| rig.exits() == [0], 5.0), "{:?}", rig.exits());
    assert_eq!(rig.stage.count(), 1, "it is not started again");
    assert_eq!(rig.pane.hidden.load(Ordering::SeqCst), 1);
    assert_eq!(rig.stage.last().times_asked("/api/window/quit"), 0, "there was nobody left to tell");
}

// ═════════════════════════════════════════════════════════════════════
// Which request is answered where
// ═════════════════════════════════════════════════════════════════════

#[test]
fn the_page_and_its_files_are_the_shells_and_need_no_secret() {
    let rig = Rig::ready();
    let page = rig.shell.handle(&Request::new("GET", "/"));
    assert_eq!(page.status, 200);
    assert_eq!((page.header("Content-Type"), page.header("Cache-Control")), (Some("text/html; charset=utf-8"), Some("no-store")));
    let html = text(&page);
    assert!(html.contains("v7.7.7") && html.contains("C:\\Users\\A &amp; B\\AppData\\Roaming\\Vispora"));
    assert!(!html.contains(rig.shell.token().as_str()), "the secret is handed over in the address, never served");
    for (path, kind) in [("/static/js/app.js", "text/javascript; charset=utf-8"), ("/static/css/style.css", "text/css; charset=utf-8")] {
        let file = rig.shell.handle(&Request::new("GET", path));
        assert_eq!((file.status, file.header("Content-Type")), (200, Some(kind)), "{path}");
        assert!(file.body.len() > 10_000);
    }
    assert_eq!(rig.shell.handle(&Request::new("GET", "/?x=1")).status, 200);
    // Anything else there is not: said the way the backend says it.
    for path in ["/static/js/other.js", "/favicon.ico", "/app.py", "/static/../config.py", "/apix"] {
        let answer = rig.shell.handle(&Request::new("GET", path));
        assert_eq!(answer.status, 404, "{path}");
        assert_eq!(answer.to_json(), Json::object([("ok", false.into()), ("err", "Endpoint not found".into()), ("path", path.into())]));
    }
    let wrong = rig.shell.handle(&Request::new("POST", "/"));
    assert_eq!((wrong.status, wrong.to_json()["err"].as_str()), (405, Some("Method POST not allowed for /")));
    assert!(rig.stage.last().calls().is_empty(), "none of that is the backend's business");
}

#[test]
fn a_call_that_is_not_from_the_apps_own_page_is_refused_before_anything_happens() {
    let rig = Rig::ready();
    let secret = rig.shell.token().as_str().to_string();
    let other_page = "https://evil.example";
    let refused: Vec<(Request, &str)> = vec![
        (Request::new("GET", "/api/echo"), "missing or wrong session token"),
        (Request::new("GET", "/api/echo").with_header(token::HEADER, ""), "missing or wrong session token"),
        (Request::new("GET", "/api/echo").with_header(token::HEADER, &"x".repeat(43)), "missing or wrong session token"),
        (Request::new("GET", "/api/echo").with_header(token::HEADER, &secret[..42]), "missing or wrong session token"),
        (rig.asking("POST", "/api/echo").with_header("Origin", other_page), "cross-origin request refused"),
        (rig.asking("POST", "/api/echo").with_header("Origin", "null"), "cross-origin request refused"),
        (rig.asking("GET", "/api/echo").with_header("Sec-Fetch-Site", "cross-site"), "cross-origin request refused"),
        (rig.asking("GET", "/api/echo").with_header("Sec-Fetch-Site", "same-site"), "cross-origin request refused"),
        // The shell's own calls are no exception: least of all quit.
        (Request::new("POST", "/api/window/quit"), "missing or wrong session token"),
        (Request::new("POST", "/api/window/show"), "missing or wrong session token"),
        (rig.asking("POST", "/api/window/quit").with_header("Origin", other_page), "cross-origin request refused"),
    ];
    for (request, why) in &refused {
        let answer = rig.shell.handle(request);
        assert_eq!(answer.status, 403, "{request:?}");
        assert_eq!(answer.to_json(), Json::object([("ok", false.into()), ("err", (*why).into())]), "{request:?}");
    }
    assert_eq!(rig.shell.phase(), Phase::Ready, "nobody was quit");
    assert!(rig.stage.last().calls().is_empty(), "and nothing reached the backend");
    assert_eq!(rig.pane.shown.load(Ordering::SeqCst), 0);
    // From its own page, said by the browser or not said at all: taken.
    for request in [
        rig.asking("POST", "/api/echo").with_header("Origin", &rig.shell.settings.origin).with_header("Sec-Fetch-Site", "same-origin"),
        rig.asking("GET", "/api/echo").with_header("Sec-Fetch-Site", "none"),
        rig.asking("GET", "/api/echo"),
    ] {
        assert_eq!(rig.shell.handle(&request).status, 200, "{request:?}");
    }
}

#[test]
fn the_windows_calls_are_answered_by_the_shell() {
    let rig = Rig::ready();
    for (path, count) in [("/api/window/minimize", &rig.pane.minimized), ("/api/window/close", &rig.pane.hidden), ("/api/window/show", &rig.pane.shown)] {
        let answer = rig.call("POST", path);
        assert_eq!((answer.status, answer.to_json()), (200, Json::object([("ok", true.into())])), "{path}");
        assert_eq!(count.load(Ordering::SeqCst), 1, "{path}");
        let wrong = rig.call("GET", path);
        assert_eq!((wrong.status, wrong.to_json()["err"].as_str().map(str::to_string)), (405, Some(format!("Method GET not allowed for {path}"))));
    }
    assert!(rig.stage.last().calls().is_empty(), "the backend has no window to do it to");
    // Hide and switch the game monitor on: the first is the shell's, the
    // second the backend's, and the answer says what really happened.
    let both = rig.call("POST", "/api/window/close-and-game-mode");
    assert_eq!(both.status, 200);
    assert_eq!(rig.pane.hidden.load(Ordering::SeqCst), 2);
    assert_eq!(rig.stage.last().times_asked("/api/window/close-and-game-mode"), 1);
    let said = both.to_json();
    assert_eq!((&said["ok"], &said["hidden"], &said["game_monitor"]["ok"]), (&Json::Bool(true), &Json::Bool(true), &Json::Bool(true)));
    // Before there is a window, the answer is an honest no.
    *rig.shell.window.lock().unwrap() = None;
    assert_eq!(rig.call("POST", "/api/window/minimize").to_json(), Json::object([("ok", false.into())]));
}

// ═════════════════════════════════════════════════════════════════════
// Quitting
// ═════════════════════════════════════════════════════════════════════

/// A shell whose backend takes `restoring` to put the system back, and whose
/// exit hook writes down whether the backend was still alive when it ran.
fn quitting(on_quit: OnQuit) -> (Rig, Arc<Mutex<Vec<bool>>>) {
    let rig = Rig::with(vec![Behaviour { on_quit, ..Behaviour::default() }], settings());
    let alive_at_exit: Arc<Mutex<Vec<bool>>> = Arc::default();
    let (seen, stage) = (alive_at_exit.clone(), rig.stage.clone());
    *rig.shell.exit_hook.lock().unwrap() = Some(Arc::new(move |_code| seen.lock().unwrap().push(stage.last().alive())));
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    (rig, alive_at_exit)
}

#[test]
fn quit_takes_the_window_away_tells_the_backend_and_waits_for_it() {
    let (rig, alive_at_exit) = quitting(OnQuit::Leaves(Duration::from_millis(400)));
    let began = Instant::now();
    let answer = rig.call("POST", "/api/window/quit");
    assert_eq!(answer.to_json(), Json::object([("ok", true.into()), ("msg", "Goodbye".into())]));
    assert!(began.elapsed() < Duration::from_millis(300), "the page has its answer at once");
    assert!(wait(|| rig.pane.hidden.load(Ordering::SeqCst) == 1, 0.3), "and the window is out of sight at once");
    assert_eq!(rig.shell.phase(), Phase::Quitting);
    assert!(alive_at_exit.lock().unwrap().is_empty(), "the process does not end while the system is being put back");
    assert_eq!(rig.shell.wait_for(Duration::from_secs(10), |phase| *phase == Phase::Gone), Phase::Gone);
    assert!(began.elapsed() >= Duration::from_millis(390), "{:?}", began.elapsed());
    assert!(wait(|| alive_at_exit.lock().unwrap().len() == 1, 5.0));
    assert_eq!(*alive_at_exit.lock().unwrap(), [false], "the exit hook ran once, after the backend had ended");
    let backend = rig.stage.last();
    assert_eq!(backend.times_asked("/api/window/quit"), 1);
    let call = backend.calls().into_iter().find(|c| c.target == "/api/window/quit").unwrap();
    assert_eq!((call.method.as_str(), call.header(token::HEADER)), ("POST", Some(backend.token.as_str())));
    assert!(!*backend.was_ended_by_force.lock().unwrap());
    assert!(rig.shell.timings().1.unwrap() >= Duration::from_millis(390));
    // After that nothing is passed on and nothing is started.
    let late = rig.call("GET", "/api/echo");
    assert_eq!((late.status, late.to_json()["err"].as_str()), (503, Some("Vispora is closing.")));
    assert_eq!(rig.stage.count(), 1);
}

#[test]
fn however_many_ask_there_is_one_quit_and_everybody_waits_for_it() {
    let (rig, alive_at_exit) = quitting(OnQuit::Leaves(Duration::from_millis(400)));
    // Was the backend still alive when each of them was let go on?
    let back: Arc<Mutex<Vec<bool>>> = Arc::default();
    let callers: Vec<_> = (0..8)
        .map(|n| {
            let (shell, stage, seen, request) = (rig.shell.clone(), rig.stage.clone(), back.clone(), rig.asking("POST", "/api/window/quit"));
            std::thread::spawn(move || {
                if n % 2 == 0 {
                    shell.handle(&request); // the page's Quit, pressed again and again
                }
                shell.quit_and_wait("the window is gone"); // what the window's owner does before the process ends
                seen.lock().unwrap().push(stage.last().alive());
            })
        })
        .collect();
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(*back.lock().unwrap(), Vec::<bool>::new(), "somebody was let go on while the backend was putting the system back");
    for caller in callers {
        caller.join().unwrap();
    }
    assert_eq!(*back.lock().unwrap(), [false; 8]);
    assert_eq!(rig.stage.last().times_asked("/api/window/quit"), 1, "the backend was told once");
    assert_eq!(*alive_at_exit.lock().unwrap(), [false], "and the exit hook ran once");
    assert_eq!(rig.pane.hidden.load(Ordering::SeqCst), 1);
}

#[test]
fn a_backend_that_will_not_go_is_ended_at_the_limit() {
    let (rig, alive_at_exit) = quitting(OnQuit::Stays);
    let began = Instant::now();
    assert_eq!(rig.shell.quit_and_wait("tray"), 0);
    let took = began.elapsed();
    let limit = rig.shell.settings.quit_within;
    assert!(took >= limit && took < limit + Duration::from_secs(2), "{took:?}");
    let backend = rig.stage.last();
    assert_eq!(backend.times_asked("/api/window/quit"), 1, "it was asked");
    assert!(*backend.was_ended_by_force.lock().unwrap() && !backend.alive());
    assert!(wait(|| alive_at_exit.lock().unwrap().len() == 1, 5.0));
    assert_eq!(*alive_at_exit.lock().unwrap(), [false]);
    assert!(rig.shell.history().join("\n").contains("had not ended 1 s after it was told to quit, and was ended"), "{:?}", rig.shell.history());
}

#[test]
fn a_quit_before_the_backend_is_up_does_not_wait_the_long_limit() {
    let rig = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], settings());
    rig.shell.start();
    assert!(wait(|| rig.stage.count() == 1, 5.0));
    let began = Instant::now();
    assert_eq!(rig.shell.quit_and_wait("changed my mind"), 0);
    let took = began.elapsed();
    assert!(took >= rig.shell.settings.quit_unready_within && took < rig.shell.settings.quit_within, "{took:?}");
    assert!(!rig.stage.last().alive());
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(rig.stage.count(), 1, "nothing is started once the shell is quitting");
    assert_eq!(rig.exits(), [0]);
}

#[test]
fn a_backend_that_dies_during_the_quit_is_not_started_again() {
    let (rig, alive_at_exit) = quitting(OnQuit::Stays);
    rig.shell.quit("tray");
    assert!(wait(|| rig.stage.last().times_asked("/api/window/quit") == 1, 5.0));
    rig.stage.last().end(5);
    assert_eq!(rig.shell.wait_for(Duration::from_secs(10), |phase| *phase == Phase::Gone), Phase::Gone);
    assert!(wait(|| alive_at_exit.lock().unwrap().len() == 1, 5.0));
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(rig.stage.count(), 1);
    assert!(!*rig.stage.last().was_ended_by_force.lock().unwrap(), "it had ended by itself");
}

#[test]
fn a_shell_that_was_never_started_can_be_quit() {
    let rig = Rig::with(vec![], settings());
    assert_eq!(rig.shell.quit_and_wait("nothing to do"), 0);
    assert_eq!((rig.shell.phase(), rig.stage.count()), (Phase::Gone, 0));
    assert!(wait(|| rig.exits() == [0], 5.0), "{:?}", rig.exits());
    // Starting it after that starts nothing.
    rig.shell.start();
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!((rig.shell.phase(), rig.stage.count()), (Phase::Gone, 0));
}

// ═════════════════════════════════════════════════════════════════════
// From the review of the first version
// ═════════════════════════════════════════════════════════════════════

#[test]
fn the_socket_is_the_shells_from_before_the_first_backend_to_after_the_last() {
    // Every call carries the secret, and a call goes to whoever listens.
    // So the port must never be free for somebody else to listen on: not
    // before a backend is up, not while one is started again, not after.
    let dies = Behaviour { ends_after: Some((Duration::from_millis(300), 3)), ..Behaviour::default() };
    let rig = Rig::with(vec![dies, Behaviour::default()], settings());
    let taken = |port: u16| std::net::TcpListener::bind(("127.0.0.1", port)).is_err();
    // Made with the shell, before anything is started or asked.
    assert_eq!((rig.stage.listens.load(Ordering::SeqCst), rig.stage.count()), (1, 0));
    let port = rig.shell.port();
    assert_eq!(port, rig.stage.port());
    assert!(taken(port), "the port was free before the first backend");
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert!(taken(port));
    // The first backend dies and a second is started: on the same socket.
    assert!(wait(|| rig.stage.count() == 2 && !rig.stage.backend(0).alive(), 10.0));
    assert!(taken(port), "the port was free while the backend was being started again");
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert_eq!(rig.stage.listens.load(Ordering::SeqCst), 1, "one socket for the shell's whole life");
    let (first, second) = (rig.stage.backend(0), rig.stage.backend(1));
    assert_eq!((first.port, first.listen), (port, HANDLE as i128));
    assert_eq!((second.port, second.listen), (port, HANDLE as i128));
    assert_eq!(rig.call("GET", "/api/echo").status, 200);
    // Whatever either of them was ever asked, it was asked on that socket.
    for backend in [&first, &second] {
        let seen = backend.seen.lock().unwrap();
        assert!(!seen.is_empty() && seen.iter().all(|call| call.header("Host") == Some(&format!("127.0.0.1:{port}"))));
    }
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    assert!(taken(port), "the port was free while the shell still existed");
}

#[test]
fn a_backend_that_never_comes_up_is_given_up_on_however_long_each_try_takes() {
    // The proportions of the real limits: one try (ready_within, 90 s) is
    // so long that there are never more than a few inside the time the
    // restarts are counted in (restarts_within, 300 s).  Counted by the
    // clock alone, a backend that never comes up would be started for ever.
    let mut settings = settings();
    settings.ready_within = Duration::from_millis(300);
    settings.hello_within = Duration::from_millis(100);
    settings.restarts_within = Duration::from_millis(350);
    settings.restarts = 2;
    let rig = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], settings);
    let began = Instant::now();
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(8), down);
    assert_eq!(phase, Phase::Down(NEVER_UP.into()),
               "after {:?}: {:?}", began.elapsed(), rig.shell.history());
    assert_eq!(rig.stage.count(), 3, "the first try and two more");
    assert!(began.elapsed() < Duration::from_secs(4), "{:?}", began.elapsed());
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(rig.stage.count(), 3, "and no fourth");
    // One that comes up in between starts the count again.
    let up_once = Behaviour { ends_after: Some((Duration::from_millis(150), 3)), ..Behaviour::default() };
    let never = Behaviour { never_up: true, ..Behaviour::default() };
    let mut settings = self::settings();
    settings.ready_within = Duration::from_millis(200);
    settings.hello_within = Duration::from_millis(100);
    settings.restarts_within = Duration::from_millis(100);
    settings.restarts = 1;
    let rig = Rig::with(vec![never.clone(), up_once, never], settings);
    rig.shell.start();
    assert!(down(&rig.shell.wait_for(Duration::from_secs(8), down)), "{:?}", rig.shell.history());
    assert_eq!(rig.stage.count(), 4, "never up, up and died, never up, never up: {:?}", rig.shell.history());
}

#[test]
fn a_quit_waits_for_a_backend_that_is_being_started() {
    // The quit comes while the backend's process is being made: it exists
    // already, or is about to, and the shell does not have it yet.  A quit
    // that did not wait would be over, and the shell's process gone, with a
    // backend left behind that is just beginning its start-up work.
    let rig = Rig::with(vec![Behaviour::default()], settings());
    // What the exit hook saw: how many backends there were, how many alive.
    let at_exit: Arc<Mutex<Vec<(usize, usize)>>> = Arc::default();
    let (seen, stage) = (at_exit.clone(), rig.stage.clone());
    *rig.shell.exit_hook.lock().unwrap() = Some(Arc::new(move |_code| {
        let started = stage.started.lock().unwrap();
        seen.lock().unwrap().push((started.len(), started.iter().filter(|backend| backend.alive()).count()));
    }));
    rig.stage.start_is_held.store(true, Ordering::SeqCst);
    rig.shell.start();
    assert!(wait(|| rig.stage.starts_begun.load(Ordering::SeqCst) == 1, 5.0));
    rig.shell.quit("changed my mind");
    std::thread::sleep(Duration::from_millis(300));
    assert!(at_exit.lock().unwrap().is_empty(), "the quit was over while a backend was being started");
    assert_eq!(rig.shell.phase(), Phase::Quitting);
    rig.stage.start_is_held.store(false, Ordering::SeqCst);
    assert_eq!(rig.shell.wait_for(Duration::from_secs(10), |phase| *phase == Phase::Gone), Phase::Gone);
    assert!(wait(|| at_exit.lock().unwrap().len() == 1, 5.0));
    assert_eq!(*at_exit.lock().unwrap(), [(1, 0)], "the backend that was started in between was dealt with before the shell went");
    // Dealt with as any other: told to quit, which it did.
    let backend = rig.stage.last();
    assert_eq!(backend.times_asked("/api/window/quit"), 1);
    assert!(!*backend.was_ended_by_force.lock().unwrap());
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!((rig.stage.count(), rig.shell.phase()), (1, Phase::Gone));
}

#[test]
fn an_answer_that_trickles_in_does_not_hold_the_start_up() {
    // "Are you up" answered a byte at a time is never silent, and would be
    // waited for long past the time a backend is given to come up.
    let mut settings = settings();
    settings.ready_within = Duration::from_millis(500);
    settings.hello_within = Duration::from_millis(150);
    settings.restarts = 0;
    let trickles = Behaviour { drips_hello: Some(Duration::from_millis(40)), ..Behaviour::default() };
    let rig = Rig::with(vec![trickles], settings);
    let began = Instant::now();
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(8), down);
    assert!(down(&phase), "{phase:?}");
    assert!(began.elapsed() < Duration::from_secs(3), "the start-up was held for {:?}", began.elapsed());
    assert_eq!(rig.stage.count(), 1);
}

#[test]
fn the_window_is_not_hidden_when_the_game_monitor_was_not_switched_on() {
    let mut settings = settings();
    settings.calls_wait = Duration::from_millis(150);
    let rig = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], settings);
    rig.shell.start();
    let answer = rig.call("POST", "/api/window/close-and-game-mode");
    assert_eq!(answer.status, 503);
    assert_eq!(answer.to_json()["err"].as_str(), Some("Vispora is still starting. Try again in a moment."));
    assert_eq!(rig.pane.hidden.load(Ordering::SeqCst), 0, "the window went although nothing was switched on");
}

#[test]
fn a_call_is_not_waited_for_once_the_backend_is_gone_for_good() {
    // The shell's socket takes a call in whether or not a backend is there
    // to answer.  A call made just as the backend dies must not sit in the
    // queue until the long limit on silence: when the shell has given up on
    // the backend, the wait is over.
    let mut settings = settings();
    settings.restarts = 0;
    settings.forward.silence = Duration::from_secs(60);
    let rig = Rig::with(vec![Behaviour::default(), Behaviour { never_up: true, ..Behaviour::default() }], settings);
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    let (shell, request) = (rig.shell.clone(), rig.asking("GET", "/api/slow?15000"));
    let began = Instant::now();
    let call = std::thread::spawn(move || shell.handle(&request));
    assert!(wait(|| rig.stage.last().times_asked("/api/slow?15000") == 1, 5.0));
    rig.stage.last().end(3);
    let answer = call.join().unwrap();
    assert_eq!(answer.status, 502);
    assert!(began.elapsed() < Duration::from_secs(10), "{:?}", began.elapsed());
}
