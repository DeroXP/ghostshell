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
    assert_eq!(names, ["listen", "port", "shell_exe", "shell_pid", "shell_started", "token"]);
    assert_eq!(given["shell_exe"], Json::Null, "a shell that does not know where its exe is says nothing, not something made up");
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
fn the_shells_own_calls_carry_the_secret_and_never_wait_for_a_backend() {
    // (the tray makes them: what to show, and what a menu entry asks for)
    let mut how = settings();
    how.own_calls_within = Duration::from_millis(300);
    how.shell_exe = Some("C:\\Program Files\\Vispora\\Vispora.exe".into());
    let rig = Rig::with(vec![Behaviour::default()], how);
    // No backend yet: nothing is asked, and nothing is waited for.
    let began = Instant::now();
    assert_eq!(rig.shell.ask_backend("GET", "/api/echo"), None);
    assert!(began.elapsed() < Duration::from_millis(100));
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    // The backend is told where the shell's exe is: that is what a person starts.
    let given = Json::parse(&rig.stage.last().handoff).unwrap();
    assert_eq!(given["shell_exe"].as_str(), Some("C:\\Program Files\\Vispora\\Vispora.exe"));

    let said = rig.shell.ask_backend("POST", "/api/echo").expect("an answer");
    assert_eq!((said["method"].as_str(), said["target"].as_str(), said["body"].as_str()), (Some("POST"), Some("/api/echo"), Some("{}")));
    assert_eq!(said["headers"]["x-vispora-token"].as_str(), Some(rig.shell.token().as_str()));
    assert_eq!(said["headers"]["content-type"].as_str(), Some("application/json"));
    assert_eq!(said["headers"]["origin"], Json::Null, "it is not a page's call");
    let said = rig.shell.ask_backend("GET", "/api/echo").expect("an answer");
    assert_eq!((said["method"].as_str(), said["body"].as_str()), (Some("GET"), Some("")));
    // Anything but "200" is no answer, and neither is one that takes too long.
    assert_eq!(rig.shell.ask_backend("GET", "/api/no-such-call"), None);
    assert_eq!(rig.shell.ask_backend("GET", "/api/empty"), None);
    let began = Instant::now();
    assert_eq!(rig.shell.ask_backend("GET", "/api/slow?2000"), None);
    assert!(began.elapsed() < Duration::from_millis(1500), "it waited {:?}", began.elapsed());
    // After the quit there is nobody to ask.
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    assert_eq!(rig.shell.ask_backend("GET", "/api/echo"), None);
}

#[test]
fn how_the_backend_is_started_is_found_out_once_before_the_first_start() {
    // (a shell that carries its backend unpacks it first)
    let rig = Rig::with(vec![Behaviour { ends_after: Some((Duration::from_millis(150), 9)), ..Behaviour::default() }, Behaviour::default()], settings());
    let asked = Arc::new(Mutex::new(0));
    let counted = asked.clone();
    *rig.shell.prepare_hook.lock().unwrap() = Some(Arc::new(move || {
        *counted.lock().unwrap() += 1;
        Ok(vispora_shell_core::platform::Command { program: "C:\\unpacked\\VisporaBackend.exe".into(), args: vec!["--backend".into()], cwd: None })
    }));
    rig.shell.start();
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert_eq!(rig.stage.last().command.program, "C:\\unpacked\\VisporaBackend.exe", "it was started from somewhere else");
    // The first one dies; the one started in its place comes from the same
    // place, and nothing is found out a second time.
    assert!(wait(|| rig.stage.count() == 2, 5.0), "{:?}", rig.shell.history());
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert_eq!(rig.stage.last().command.program, "C:\\unpacked\\VisporaBackend.exe");
    assert_eq!(*asked.lock().unwrap(), 1);
}

#[test]
fn a_backend_that_cannot_be_made_ready_is_not_started_and_the_page_is_told_why() {
    let rig = Rig::with(vec![Behaviour::default()], settings());
    *rig.shell.prepare_hook.lock().unwrap() = Some(Arc::new(|| Err("there is no room left on the drive".to_string())));
    rig.shell.start();
    let phase = rig.shell.wait_for(Duration::from_secs(5), down);
    let Phase::Down(why) = phase else { panic!("{phase:?}") };
    assert_eq!(why, "Vispora could not get its background program ready (there is no room left on the drive). Close it and open it again.");
    let answer = rig.call("GET", "/api/echo");
    assert!(answer.status == 503 && text(&answer).contains("there is no room left on the drive"), "{} {}", answer.status, text(&answer));
    // (nothing is started a moment later either)
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(rig.stage.count(), 0, "something was started all the same");
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    assert_eq!(rig.stage.count(), 0);
}

#[test]
fn a_shell_that_was_quit_before_it_was_started_makes_no_backend_ready() {
    // (nothing is unpacked for an app that is already leaving)
    let rig = Rig::with(vec![Behaviour::default()], settings());
    let asked = Arc::new(Mutex::new(0));
    let counted = asked.clone();
    *rig.shell.prepare_hook.lock().unwrap() = Some(Arc::new(move || {
        *counted.lock().unwrap() += 1;
        Ok(vispora_shell_core::platform::Command { program: "C:\\unpacked\\VisporaBackend.exe".into(), args: Vec::new(), cwd: None })
    }));
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    rig.shell.start();
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!((*asked.lock().unwrap(), rig.stage.count()), (0, 0), "a backend was made ready, or started, after the quit");
}

#[test]
fn a_quit_waits_for_a_backend_that_is_being_made_ready_and_starts_none() {
    let rig = Rig::with(vec![Behaviour::default()], settings());
    let (begun, done) = (Arc::new(Mutex::new(false)), Arc::new(Mutex::new(false)));
    let (begun_there, done_there) = (begun.clone(), done.clone());
    *rig.shell.prepare_hook.lock().unwrap() = Some(Arc::new(move || {
        *begun_there.lock().unwrap() = true;
        std::thread::sleep(Duration::from_millis(400));
        *done_there.lock().unwrap() = true;
        Ok(vispora_shell_core::platform::Command { program: "C:\\unpacked\\VisporaBackend.exe".into(), args: Vec::new(), cwd: None })
    }));
    rig.shell.start();
    assert!(wait(|| *begun.lock().unwrap(), 5.0));
    // The quit is over only when what was being made ready is ready: its
    // folder is the quit's to take away, whole.
    assert_eq!(rig.shell.quit_and_wait("test"), 0);
    assert!(*done.lock().unwrap(), "the quit was over while the backend was still being unpacked");
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(rig.stage.count(), 0, "a backend was started after the quit");
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

// ═════════════════════════════════════════════════════════════════════
// What has moved from the backend into the shell
// ═════════════════════════════════════════════════════════════════════

/// A shell that reads a made-up machine.  (Its library would say what a
/// card says, with a temperature the backend never says: the shell does
/// not ask it for the card.)
fn with_a_machine(rig: &Rig) -> Arc<vispora_shell_core::moved::Fake> {
    let machine = vispora_shell_core::moved::Fake::new();
    machine.answers("gpu_live", Some(Json::object([("temp_c", 13.into()), ("gpu_util_pct", 97.into()), ("core_mhz", 2100.into())])));
    machine.at(1000.0);
    rig.shell.reads_with(machine.clone(), false);
    machine
}

const THE_CARD: &str = "/api/gpu/oc/live";

#[test]
fn a_call_that_has_moved_is_answered_by_the_shell_and_does_not_reach_the_backend() {
    // (a backend that takes a while to come up: the shell's own answers do not wait for it)
    let rig = Rig::with(vec![Behaviour { up_after: Duration::from_millis(400), ..Behaviour::default() }], settings());
    with_a_machine(&rig);
    rig.shell.start();
    let began = Instant::now();
    let post = |target: &str, body: &str| rig.shell.handle(&rig.asking("POST", target).with_body(body));
    let started = post("/api/perf/start", r#"{"consumer": "hwmon-page"}"#);
    assert_eq!((started.status, text(&started)), (200, r#"{"ok":true,"consumers":["hwmon-page"]}"#.to_string()));
    let status = rig.call("GET", "/api/perf/status");
    assert_eq!((status.status, status.header("Content-Type")), (200, Some("application/json")));
    assert_eq!((status.to_json()["running"].truthy(), status.to_json()["max_samples"].as_int()), (true, Some(300)));
    assert_eq!(rig.call("GET", "/api/perf/samples?seconds=5").to_json()["window_s"].as_int(), Some(5));
    assert_eq!(rig.call("GET", "/api/perf/score").to_json()["verdict"].as_str(), Some("no-data"));
    assert_eq!(text(&post("/api/perf/stop", r#"{"consumer": "hwmon-page"}"#)), r#"{"ok":true,"stopped":true}"#);
    assert!(began.elapsed() < Duration::from_millis(300) && rig.shell.phase() == Phase::Starting, "a call that has moved waited for the backend");
    assert_eq!(rig.wait_ready(), Phase::Ready);
    assert!(rig.stage.last().calls().is_empty(), "a call that has moved reached the backend: {:?}", rig.stage.last().calls());
    // Every other call is the backend's, as before.
    assert_eq!(rig.call("GET", "/api/echo").status, 200);
    assert_eq!(rig.stage.last().times_asked("/api/echo"), 1);
}

#[test]
fn a_call_that_has_moved_is_asked_what_every_call_is_asked() {
    let rig = Rig::ready();
    with_a_machine(&rig);
    // Without the run's secret, from another page: refused before anything is answered or started.
    let strangers = [Request::new("GET", "/api/perf/status"),
                     Request::new("POST", "/api/perf/start").with_body("{}"),
                     rig.asking("POST", "/api/perf/start").with_header("Origin", "https://example.org").with_body("{}"),
                     rig.asking("GET", "/api/perf/samples").with_header("Sec-Fetch-Site", "cross-site")];
    for stranger in strangers {
        let answer = rig.shell.handle(&stranger);
        assert_eq!(answer.status, 403, "{} {}", stranger.method, stranger.target);
    }
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    assert!(!moved.perf.is_running(), "a stranger started the sampler");
}

#[test]
fn the_cards_reading_is_asked_of_the_backend_with_the_runs_secret() {
    // (a backend that takes a while to come up, and after it one that never does)
    let rig = Rig::with(vec![Behaviour { up_after: Duration::from_millis(300), ..Behaviour::default() }, Behaviour { never_up: true, ..Behaviour::default() }],
                        settings());
    let machine = with_a_machine(&rig);
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    rig.shell.start();
    let post = |target: &str, body: &str| rig.shell.handle(&rig.asking("POST", target).with_body(body));
    let last = || rig.call("GET", "/api/perf/status").to_json()["last"].to_string();
    post("/api/perf/start", "{}");
    // No backend yet: there is no reading, and a sample says so at once.
    let began = Instant::now();
    moved.perf.step(&moved.card);
    assert_eq!(last(), r#"{"ts":1000.0,"ok":false,"core":0,"mem":0,"temp":0,"power":0.0,"util":0,"memutil":0,"fan":0}"#);
    assert!(began.elapsed() < Duration::from_millis(200) && rig.shell.phase() == Phase::Starting, "a sample waited for the backend");
    assert_eq!(rig.wait_ready(), Phase::Ready);
    let backend = rig.stage.last();
    assert_eq!(backend.times_asked(THE_CARD), 0);
    // The backend is up: every sample is one reading of the backend's.
    machine.at(1000.2);
    moved.perf.step(&moved.card);
    assert_eq!(last(), r#"{"ts":1000.2,"ok":true,"core":1800,"mem":7000,"temp":71,"power":123.45,"util":88,"memutil":33,"fan":60}"#);
    *backend.card.lock().unwrap() = Json::parse(r#"{"ok": true, "core_mhz": 2100, "temp_c": 64, "power_w": 250.5, "gpu_util_pct": 99}"#).unwrap();
    machine.at(1000.4);
    moved.perf.step(&moved.card);
    assert_eq!(last(), r#"{"ts":1000.4,"ok":true,"core":2100,"mem":0,"temp":64,"power":250.5,"util":99,"memutil":0,"fan":0}"#);
    let seen = backend.calls();
    assert_eq!(seen.iter().map(|seen| (seen.method.as_str(), seen.target.as_str())).collect::<Vec<_>>(), [("GET", THE_CARD), ("GET", THE_CARD)]);
    for call in &seen {
        // As the shell's own calls for the tray: with the secret, and with
        // nothing a page would add.
        assert_eq!(call.header(token::HEADER), Some(rig.shell.token().as_str()));
        assert!(call.header("Origin").is_none() && call.header("Cookie").is_none() && call.body.is_empty());
    }
    // The backend has no reading either (no NVIDIA card): a sample says so.
    *backend.card.lock().unwrap() = Json::parse(r#"{"ok": false, "err": "No NVIDIA GPU / nvidia-smi missing"}"#).unwrap();
    moved.perf.step(&moved.card);
    assert!(last().contains(r#""ok":false,"core":0,"mem":0,"temp":0"#), "{}", last());
    // The shell's own library was not asked for the card at any time.
    assert!(machine.asked().is_empty(), "{:?}", machine.asked());
    // The page's own call for the card's reading stays what it was: the backend's.
    let asked = backend.times_asked(THE_CARD);
    assert_eq!(rig.call("GET", THE_CARD).to_json()["err"].as_str(), Some("No NVIDIA GPU / nvidia-smi missing"));
    assert_eq!(backend.times_asked(THE_CARD), asked + 1);
    // The reading comes back, and then the backend is gone (the one
    // started in its place is not up): no reading, and no waiting for one.
    *backend.card.lock().unwrap() = a_card();
    moved.perf.step(&moved.card);
    assert!(last().contains(r#""ok":true,"core":1800"#), "{}", last());
    let asked = backend.times_asked(THE_CARD);
    backend.end(1);
    assert_ne!(rig.shell.wait_for(Duration::from_secs(10), |phase| *phase != Phase::Ready), Phase::Ready);
    let began = Instant::now();
    moved.perf.step(&moved.card);
    assert!(last().contains(r#""ok":false,"core":0"#) && began.elapsed() < Duration::from_millis(200), "{} after {:?}", last(), began.elapsed());
    assert_eq!(backend.times_asked(THE_CARD), asked, "a backend that is gone was asked");
}

/// A shell that reads a made-up machine and has made-up readers for the
/// card, which say what the helper says of a card at 61 degrees.
fn with_readers(rig: &Rig) -> (Arc<vispora_shell_core::moved::Fake>, Arc<vispora_shell_core::moved::FakeReaders>) {
    let machine = with_a_machine(rig);
    let readers = vispora_shell_core::moved::FakeReaders::new();
    readers.reads(vispora_shell_core::moved::FakeReading::Says(Json::parse(r#"{"temp_c": 61, "gpu_util_pct": 97, "core_mhz": 2100, "power_mw": 215437}"#).unwrap()));
    assert!(rig.shell.card_is_read_by(Arc::new(readers.clone())));
    (machine, readers)
}

const THE_INSTALL: &str = "/api/driver/install";

#[test]
fn the_cards_reading_is_the_readers_and_the_backend_is_not_asked() {
    let rig = Rig::ready();
    let (machine, readers) = with_readers(&rig);
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    let last = || rig.call("GET", "/api/perf/status").to_json()["last"].to_string();
    assert_eq!(rig.shell.card_reader_pid(), None);
    rig.shell.handle(&rig.asking("POST", "/api/perf/start").with_body("{}"));
    assert!(readers.running().is_empty(), "a reader runs before a reading was wanted");
    for round in 0..5 {
        machine.at(1000.0 + round as f64 * 0.2);
        moved.perf.step(&moved.card);
    }
    assert_eq!(last(), r#"{"ts":1000.8,"ok":true,"core":2100,"mem":0,"temp":61,"power":215.44,"util":97,"memutil":0,"fan":0}"#);
    assert_eq!((readers.started(), readers.asked(), rig.shell.card_reader_pid()), (1, (5, 5), Some(1)));
    assert!(rig.stage.last().calls().is_empty(), "the backend was asked: {:?}", rig.stage.last().calls());
    assert!(rig.shell.history().iter().any(|line| line == "the graphics card's reader was started (pid 1)"), "{:?}", rig.shell.history());
    // The reader has no reading: the backend's it is, from the same call as before.
    readers.reads(vispora_shell_core::moved::FakeReading::HasNone);
    machine.at(1001.0);
    moved.perf.step(&moved.card);
    assert_eq!(last(), r#"{"ts":1001.0,"ok":true,"core":1800,"mem":7000,"temp":71,"power":123.45,"util":88,"memutil":33,"fan":60}"#);
    assert_eq!(rig.stage.last().times_asked(THE_CARD), 1);
    assert!(readers.running().is_empty() && machine.asked().is_empty());
    // A shell that answers nothing itself has nobody who wants a reading.
    let other = Rig::ready();
    assert!(!other.shell.card_is_read_by(Arc::new(vispora_shell_core::moved::FakeReaders::new())));
}

#[test]
fn before_the_backend_hears_of_a_drivers_install_the_reader_is_gone() {
    let rig = Rig::ready();
    let (machine, readers) = with_readers(&rig);
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    let backend = rig.stage.last();
    // (what the backend would find at the moment the call reaches it)
    let found = Arc::new(Mutex::new(Vec::new()));
    let (seen, running) = (found.clone(), readers.clone());
    *backend.when_asked_to_install.lock().unwrap() = Some(Arc::new(move || seen.lock().unwrap().push(running.running())));
    let sample = |at: f64| {
        machine.at(at);
        moved.perf.step(&moved.card);
        rig.call("GET", "/api/perf/status").to_json()["last"]["temp"].as_int()
    };
    rig.shell.handle(&rig.asking("POST", "/api/perf/start").with_body("{}"));
    assert_eq!((sample(1000.0), readers.running()), (Some(61), vec![1]));

    // What is not the app's own page asking does not end the reader, and
    // does not reach the backend.
    for stranger in [Request::new("POST", THE_INSTALL).with_body("{}"), rig.asking("POST", THE_INSTALL).with_header("Origin", "https://example.org").with_body("{}")] {
        assert_eq!(rig.shell.handle(&stranger).status, 403);
    }
    // Neither does another method (the backend answers that with words of its own).
    rig.call("GET", THE_INSTALL);
    assert_eq!((readers.running(), found.lock().unwrap().len(), backend.times_asked(THE_INSTALL)), (vec![1], 0, 1));

    // The page asks, and no installer is started (it was not downloaded):
    // the reader was gone when the backend heard, the answer is the
    // backend's, and the next reading starts a reader again.
    let answer = rig.shell.handle(&rig.asking("POST", THE_INSTALL).with_body("{}"));
    assert_eq!((answer.status, text(&answer)), (200, r#"{"ok":false,"err":"Driver installer not downloaded yet"}"#.to_string()));
    assert_eq!(*found.lock().unwrap(), [Vec::<u32>::new()], "the backend heard of the install with the reader still running");
    assert_eq!((sample(1000.2), readers.running(), backend.times_asked(THE_CARD)), (Some(61), vec![2], 0));

    // Written another way, which the backend's web framework reads as the
    // same call (this stand-in does not, and says it has no such call):
    // the reader is ended for it all the same.
    rig.shell.handle(&rig.asking("POST", "/api/driver/%69nstall").with_body("{}"));
    assert!(readers.running().is_empty(), "a call the backend would take for the install did not end the reader");
    assert_eq!((sample(1000.4), readers.running()), (Some(61), vec![3]));

    // The page asks again and an installer is started: the reader is gone
    // before, and stays away for 900 seconds, with the backend's reading
    // meanwhile.
    *backend.install_says.lock().unwrap() = Json::parse(r#"{"ok": true, "msg": "NVIDIA installer launched."}"#).unwrap();
    let answer = rig.shell.handle(&rig.asking("POST", "/api/driver/install?from=the-page").with_body("{}"));
    assert_eq!((answer.status, text(&answer)), (200, r#"{"ok":true,"msg":"NVIDIA installer launched."}"#.to_string()));
    assert_eq!(*found.lock().unwrap(), [Vec::<u32>::new(), Vec::new()]);
    for later in [0.2, 60.0, 899.0] {
        assert_eq!((sample(1000.4 + later), readers.running().len()), (Some(71), 0), "{later} s after the installer was started");
    }
    assert_eq!((readers.started(), backend.times_asked(THE_CARD)), (3, 3));
    assert_eq!((sample(1900.4), readers.running()), (Some(61), vec![4]));
    let said = rig.shell.history().join("\n");
    for line in ["a graphics driver's installer may be about to start: the graphics card's reader was ended first (pid 1), and was seen to be gone",
                 "no graphics driver's installer was started: the graphics card's reader may be started again",
                 "a graphics driver's installer may be about to start: the graphics card's reader was ended first (pid 3), and was seen to be gone",
                 "a graphics driver's installer was started: no reader for the graphics card for 900 s, the backend is asked meanwhile"] {
        assert!(said.contains(line), "not said: {line}\n{said}");
    }
    assert!(machine.asked().is_empty());
}

#[test]
fn a_drivers_install_is_passed_on_as_ever_where_the_shell_reads_nothing_itself() {
    let rig = Rig::ready();
    let answer = rig.shell.handle(&rig.asking("POST", THE_INSTALL).with_body("{}"));
    assert_eq!((answer.status, text(&answer)), (200, r#"{"ok":false,"err":"Driver installer not downloaded yet"}"#.to_string()));
    assert_eq!(rig.stage.last().times_asked(THE_INSTALL), 1);
    // ... and where it does, but has no reader of its own for the card.
    with_a_machine(&rig);
    let answer = rig.shell.handle(&rig.asking("POST", THE_INSTALL).with_body("{}"));
    assert_eq!((answer.status, rig.stage.last().times_asked(THE_INSTALL)), (200, 2));
    // A backend that is not there: the page is told so, as for any call,
    // and that is "no installer was started".
    let down = Rig::with(vec![Behaviour { never_up: true, ..Behaviour::default() }], vispora_shell_core::Settings { calls_wait: Duration::from_millis(100), ..settings() });
    let (machine, readers) = with_readers(&down);
    let moved = down.shell.moved.lock().unwrap().clone().unwrap();
    down.shell.start();
    moved.perf.start(&moved.card, "x".into());
    moved.perf.step(&moved.card);
    assert_eq!(readers.running(), [1]);
    let answer = down.shell.handle(&down.asking("POST", THE_INSTALL).with_body("{}"));
    assert_ne!(answer.status, 200);
    machine.at(1000.2);
    moved.perf.step(&moved.card);
    assert_eq!((readers.started(), readers.running()), (2, vec![2]), "the reader stayed away though no installer was started");
}

#[test]
fn at_the_quit_the_reader_is_ended_first_and_none_is_started_after() {
    let rig = Rig::ready();
    let (machine, readers) = with_readers(&rig);
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    moved.perf.start(&moved.card, "x".into());
    moved.perf.step(&moved.card);
    assert_eq!(readers.running(), [1]);
    // (what the backend would find when it is told to quit)
    assert_eq!(rig.shell.quit_and_wait("a test"), 0);
    assert!(readers.running().is_empty(), "the reader outlived the quit");
    let said = rig.shell.history();
    let at = |line: &str| said.iter().position(|said| said.starts_with(line)).unwrap_or_else(|| panic!("not said: {line}\n{said:?}"));
    assert!(at("the graphics card's reader was ended (pid 1): the app quits") < at("the backend took the quit"), "{said:?}");
    machine.at(2000.0);
    moved.perf.step(&moved.card);
    assert_eq!(readers.started(), 1, "a reader was started after the quit");
}

#[test]
fn with_its_thread_the_sampler_asks_the_backend_five_times_a_second_and_only_while_it_runs() {
    let rig = Rig::ready();
    let machine = vispora_shell_core::moved::Fake::new();
    let moved = rig.shell.reads_with(machine.clone(), true);
    let backend = rig.stage.last();
    let post = |target: &str, body: &str| text(&rig.shell.handle(&rig.asking("POST", target).with_body(body)));
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(backend.times_asked(THE_CARD), 0, "the card is read before anybody wants the sampler");
    assert_eq!(post("/api/perf/start", r#"{"consumer": "hwmon-page"}"#), r#"{"ok":true,"consumers":["hwmon-page"]}"#);
    let began = Instant::now();
    while backend.times_asked(THE_CARD) < 4 && began.elapsed() < Duration::from_secs(10) {
        std::thread::sleep(Duration::from_millis(10));
    }
    let took = began.elapsed();
    assert!(backend.times_asked(THE_CARD) >= 4 && took >= Duration::from_millis(500) && took < Duration::from_millis(2500), "four samples in {took:?}");
    let status = rig.call("GET", "/api/perf/status").to_json();
    assert_eq!((status["last"]["ok"].truthy(), status["last"]["temp"].as_int(), &status["last"]["power"]), (true, Some(71), &Json::Float(123.45)));
    assert_eq!(post("/api/perf/stop", r#"{"consumer": "hwmon-page"}"#), r#"{"ok":true,"stopped":true}"#);
    let told = Instant::now();
    while moved.perf.is_running() && told.elapsed() < Duration::from_secs(10) {
        std::thread::sleep(Duration::from_millis(5));
    }
    let asked = backend.times_asked(THE_CARD);
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(backend.times_asked(THE_CARD), asked, "the backend was asked for the card after the sampler stopped");
    assert!(backend.calls().iter().all(|seen| seen.target == THE_CARD), "{:?}", backend.calls());
    assert!(machine.asked().is_empty(), "{:?}", machine.asked());
}

#[test]
fn what_the_shell_does_not_take_of_a_moved_path_is_the_backends() {
    let rig = Rig::ready();
    with_a_machine(&rig);
    let backend = rig.stage.last();
    // Another method, a body Python stops at, a path the sampler does not
    // have, a number of seconds that Python may read and the shell cannot.
    let long = format!("/api/perf/samples?seconds={}", "9".repeat(60));
    let not_taken = [rig.asking("POST", "/api/perf/status"), rig.asking("GET", "/api/perf/start"),
                     rig.asking("POST", "/api/perf/start").with_body("not json"), rig.asking("POST", "/api/perf/stop").with_body("[1]"),
                     rig.asking("POST", "/api/perf/start").with_body(r#"{"consumer": []}"#), rig.asking("GET", "/api/perf/other"),
                     rig.asking("GET", &long), rig.asking("GET", "/api/perf/score?seconds=%D9%A1%D9%A2")];
    for (n, request) in not_taken.iter().enumerate() {
        rig.shell.handle(request);
        let seen = backend.calls();
        assert_eq!(seen.len(), n + 1, "{} {} was not passed on", request.method, request.target);
        let last = seen.last().unwrap();
        assert_eq!((last.method.as_str(), last.target.as_str(), &last.body), (request.method.as_str(), request.target.as_str(), &request.body));
    }
    let moved = rig.shell.moved.lock().unwrap().clone().unwrap();
    assert!(!moved.perf.is_running());
}

#[test]
fn without_a_door_to_the_machine_every_call_is_the_backends_as_before() {
    let rig = Rig::ready();
    assert!(rig.shell.moved.lock().unwrap().is_none());
    for target in ["/api/perf/status", "/api/perf/samples?seconds=60", "/api/perf/score"] {
        rig.call("GET", target);
        assert_eq!(rig.stage.last().times_asked(target), 1, "{target}");
    }
    rig.shell.handle(&rig.asking("POST", "/api/perf/start").with_body(r#"{"consumer": "hwmon-page"}"#));
    assert_eq!(rig.stage.last().times_asked("/api/perf/start"), 1);
    assert_eq!(rig.stage.last().times_asked(THE_CARD), 0, "a shell that has taken nothing over asked the backend for the card");
}
