//! What the shell's tests run against: a backend that exists only in the
//! test.
//!
//! Nothing is started for real here.  "Starting the backend" makes a stand-in:
//! a thread that listens on the port the shell picked, on 127.0.0.1, and
//! answers the few calls a test needs the way the real backend would (it
//! checks the Host and the secret as app.py does), and a "process" that is
//! alive until it is told to quit, is ended, or decides to die.  How a
//! stand-in behaves is set by the test, one behaviour per start.

#![allow(dead_code)]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use vispora_shell_core::http::{Request, Response};
use vispora_shell_core::json::Json;
use vispora_shell_core::platform::{Child, Command, Platform, Window};
use vispora_shell_core::shell::Phase;
use vispora_shell_core::{token, Settings, Shell};

pub const VERSION: &str = "7.7.7";

/// What a stand-in does when it is told to quit.
#[derive(Clone, Debug, PartialEq)]
pub enum OnQuit {
    /// Puts the system back (which takes this long), answers, and ends.
    Leaves(Duration),
    /// Answers and stays, as a backend that hangs in its quit would.
    Stays,
}

/// How one stand-in behaves.
#[derive(Clone, Debug)]
pub struct Behaviour {
    /// How long after its start it begins to listen.
    pub up_after: Duration,
    /// It never listens at all.
    pub never_up: bool,
    pub version: String,
    pub on_quit: OnQuit,
    /// It ends on its own this long after its start, with this exit code.
    pub ends_after: Option<(Duration, i32)>,
}

impl Default for Behaviour {
    fn default() -> Behaviour {
        Behaviour { up_after: Duration::ZERO, never_up: false, version: VERSION.into(), on_quit: OnQuit::Leaves(Duration::ZERO), ends_after: None }
    }
}

/// One call as the stand-in received it.
#[derive(Clone, Debug)]
pub struct Seen {
    pub method: String,
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Seen {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

/// One stand-in backend: its "process" and what it has been asked.
pub struct StandIn {
    pub pid: u32,
    pub port: u16,
    pub token: String,
    pub shell_pid: i128,
    pub command: Command,
    pub handoff: String,
    behaviour: Behaviour,
    started: Instant,
    /// Its exit code, once it has ended.
    ended: Mutex<Option<i32>>,
    pub was_ended_by_force: Mutex<bool>,
    pub seen: Mutex<Vec<Seen>>,
}

impl StandIn {
    pub fn alive(&self) -> bool {
        self.exit_code().is_none()
    }

    fn exit_code(&self) -> Option<i32> {
        let mut ended = self.ended.lock().unwrap();
        if ended.is_none() {
            if let Some((after, code)) = self.behaviour.ends_after {
                if self.started.elapsed() >= after {
                    *ended = Some(code);
                }
            }
        }
        *ended
    }

    pub fn end(&self, code: i32) {
        self.ended.lock().unwrap().get_or_insert(code);
    }

    /// The calls it received, without the shell's own "are you up".
    pub fn calls(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().iter().filter(|s| s.target != "/api/shell/hello").cloned().collect()
    }

    pub fn times_asked(&self, target: &str) -> usize {
        self.seen.lock().unwrap().iter().filter(|s| s.target == target).count()
    }

    fn listen(self: Arc<Self>) {
        if self.behaviour.never_up {
            return;
        }
        while self.started.elapsed() < self.behaviour.up_after {
            if !self.alive() {
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        let Ok(listener) = TcpListener::bind(("127.0.0.1", self.port)) else { return };
        listener.set_nonblocking(true).unwrap();
        while self.alive() {
            match listener.accept() {
                Ok((stream, _)) => {
                    let me = self.clone();
                    std::thread::spawn(move || me.answer(stream));
                }
                Err(_) => std::thread::sleep(Duration::from_millis(2)),
            }
        }
    }

    fn answer(&self, stream: TcpStream) {
        let _ = stream.set_nonblocking(false);
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut first = String::new();
        if reader.read_line(&mut first).unwrap_or(0) == 0 {
            return;
        }
        let mut words = first.split_whitespace();
        let (method, target) = (words.next().unwrap_or("").to_string(), words.next().unwrap_or("").to_string());
        let mut headers = Vec::new();
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 || line.trim().is_empty() {
                break;
            }
            if let Some((name, value)) = line.split_once(':') {
                headers.push((name.trim().to_string(), value.trim().to_string()));
            }
        }
        let length = headers.iter().find(|(n, _)| n.eq_ignore_ascii_case("content-length")).and_then(|(_, v)| v.parse::<usize>().ok()).unwrap_or(0);
        let mut body = vec![0u8; length];
        let _ = reader.read_exact(&mut body);
        let seen = Seen { method: method.clone(), target: target.clone(), headers, body };
        self.seen.lock().unwrap().push(seen.clone());
        let raw = self.reply(&seen);
        let mut stream = stream;
        let _ = stream.write_all(&raw);
        let _ = stream.flush();
    }

    /// The whole answer, as it goes over the wire.
    fn reply(&self, seen: &Seen) -> Vec<u8> {
        let json = |status: &str, value: Json| plain(status, "application/json", value.to_string().as_bytes());
        let no = |status: &str, why: &str| json(status, Json::object([("ok", false.into()), ("err", why.into())]));
        // What app.py asks of every call first.
        if seen.header("Host") != Some(&format!("127.0.0.1:{}", self.port)) {
            return no("403 FORBIDDEN", "bad host");
        }
        let path = seen.target.split('?').next().unwrap_or("");
        if path.starts_with("/api/") {
            if seen.header("Origin").is_some() || seen.header("Sec-Fetch-Site").is_some_and(|s| s != "same-origin" && s != "none") {
                return no("403 FORBIDDEN", "cross-origin request refused");
            }
            if seen.header(token::HEADER) != Some(self.token.as_str()) {
                return no("403 FORBIDDEN", "missing or wrong session token");
            }
        }
        let query = seen.target.split_once('?').map_or("", |(_, q)| q);
        match (seen.method.as_str(), path) {
            ("GET", "/api/shell/hello") => json("200 OK", Json::object([
                ("ok", true.into()), ("app", "Vispora".into()), ("version", self.behaviour.version.as_str().into()),
                ("pid", self.pid.into()), ("behind_shell", true.into()),
            ])),
            ("POST", "/api/window/quit") => {
                match &self.behaviour.on_quit {
                    OnQuit::Leaves(restoring) => {
                        std::thread::sleep(*restoring);
                        self.end(0);
                    }
                    OnQuit::Stays => {}
                }
                json("200 OK", Json::object([("ok", true.into()), ("msg", "Goodbye".into())]))
            }
            ("POST", "/api/window/close-and-game-mode") => json("200 OK", Json::object([
                ("ok", true.into()), ("hidden", false.into()), ("game_monitor", Json::object([("ok", true.into())])),
            ])),
            (_, "/api/echo") => json("200 OK", Json::object([
                ("ok", true.into()), ("method", seen.method.as_str().into()), ("target", seen.target.as_str().into()),
                ("headers", Json::Object(seen.headers.iter().map(|(n, v)| (n.to_lowercase(), v.as_str().into())).collect())),
                ("body", String::from_utf8_lossy(&seen.body).into_owned().into()),
            ])),
            ("GET", "/api/slow") => {
                std::thread::sleep(Duration::from_millis(query.parse().unwrap_or(100)));
                json("200 OK", Json::object([("ok", true.into())]))
            }
            ("POST", "/api/die") => {
                self.end(query.parse().unwrap_or(3));
                json("200 OK", Json::object([("ok", true.into())]))
            }
            ("GET", "/api/big") => plain("200 OK", "application/octet-stream", &vec![b'x'; query.parse().unwrap_or(1000)]),
            ("GET", "/api/in-pieces") => {
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n\
                  5\r\nhello\r\n1;note=1\r\n \r\nC\r\nfrom pieces!\r\n0\r\nX-After: 1\r\n\r\n".to_vec()
            }
            ("GET", "/api/to-the-end") => b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nServer: stand-in\r\n\r\nall there is".to_vec(),
            ("GET", "/api/cut-short") => b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nonly this much".to_vec(),
            ("GET", "/api/not-http") => b"SSH-2.0-OpenSSH_9.0\r\n\r\n".to_vec(),
            ("GET", "/api/empty") => b"HTTP/1.1 204 NO CONTENT\r\nX-Kept: yes\r\nKeep-Alive: timeout=5\r\n\r\n".to_vec(),
            _ => json("404 NOT FOUND", Json::object([("ok", false.into()), ("err", "Endpoint not found".into()), ("path", path.into())])),
        }
    }
}

fn plain(status: &str, kind: &str, body: &[u8]) -> Vec<u8> {
    let mut out = format!(
        "HTTP/1.1 {status}\r\nServer: Werkzeug/3.0 Python/3.12\r\nDate: Mon, 23 Nov 2026 10:00:00 GMT\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    out.extend_from_slice(body);
    out
}

struct Handle(Arc<StandIn>);

impl Child for Handle {
    fn id(&self) -> u32 {
        self.0.pid
    }
    fn exit_code(&mut self) -> Option<i32> {
        self.0.exit_code()
    }
    fn kill(&mut self) {
        if self.0.alive() {
            *self.0.was_ended_by_force.lock().unwrap() = true;
        }
        self.0.end(-9);
    }
}

/// The platform of the tests: every "start" makes a stand-in.
#[derive(Default)]
pub struct Stage {
    /// How the next stand-ins behave, in order; the last one goes on for ever.
    pub behaviours: Mutex<VecDeque<Behaviour>>,
    pub started: Mutex<Vec<Arc<StandIn>>>,
    /// Set: starting fails with this.
    pub cannot_start: Mutex<Option<String>>,
    pids: AtomicU32,
    randoms: AtomicUsize,
}

impl Stage {
    pub fn backend(&self, n: usize) -> Arc<StandIn> {
        self.started.lock().unwrap()[n].clone()
    }
    pub fn last(&self) -> Arc<StandIn> {
        self.started.lock().unwrap().last().expect("a backend was started").clone()
    }
    pub fn count(&self) -> usize {
        self.started.lock().unwrap().len()
    }
}

struct StageDoor(Arc<Stage>);

impl Platform for StageDoor {
    fn start(&self, command: &Command, handoff: &str) -> Result<Box<dyn Child>, String> {
        if let Some(why) = self.0.cannot_start.lock().unwrap().clone() {
            return Err(why);
        }
        let given = Json::parse(handoff).map_err(|e| format!("the hand-over is not JSON: {e}"))?;
        let behaviour = {
            let mut next = self.0.behaviours.lock().unwrap();
            if next.len() > 1 { next.pop_front().unwrap() } else { next.front().cloned().unwrap_or_default() }
        };
        let stand_in = Arc::new(StandIn {
            pid: 1000 + self.0.pids.fetch_add(1, Ordering::SeqCst),
            port: given["port"].as_int().unwrap_or(0) as u16,
            token: given["token"].as_str().unwrap_or("").to_string(),
            shell_pid: given["shell_pid"].as_int().unwrap_or(0),
            command: command.clone(),
            handoff: handoff.to_string(),
            behaviour,
            started: Instant::now(),
            ended: Mutex::new(None),
            was_ended_by_force: Mutex::new(false),
            seen: Mutex::new(Vec::new()),
        });
        self.0.started.lock().unwrap().push(stand_in.clone());
        let listener = stand_in.clone();
        std::thread::spawn(move || listener.listen());
        Ok(Box::new(Handle(stand_in)))
    }

    fn free_port(&self) -> Result<u16, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
        Ok(listener.local_addr().unwrap().port())
    }

    fn random(&self, bytes: &mut [u8]) -> Result<(), String> {
        let run = self.0.randoms.fetch_add(1, Ordering::SeqCst);
        bytes.iter_mut().enumerate().for_each(|(i, b)| *b = (i * 31 + run * 17 + 5) as u8);
        Ok(())
    }

    fn own_pid(&self) -> u32 {
        4242
    }
}

/// A window that only counts.
#[derive(Default)]
pub struct Pane {
    pub minimized: AtomicUsize,
    pub hidden: AtomicUsize,
    pub shown: AtomicUsize,
}

impl Window for Pane {
    fn minimize(&self) -> bool {
        self.minimized.fetch_add(1, Ordering::SeqCst);
        true
    }
    fn hide(&self) -> bool {
        self.hidden.fetch_add(1, Ordering::SeqCst);
        true
    }
    fn show(&self) -> bool {
        self.shown.fetch_add(1, Ordering::SeqCst);
        true
    }
}

/// A shell on the stage, with short limits.
pub struct Rig {
    pub shell: Arc<Shell>,
    pub stage: Arc<Stage>,
    pub pane: Arc<Pane>,
    /// The exit codes the exit hook was called with.
    pub exits: Arc<Mutex<Vec<i32>>>,
}

impl Drop for Rig {
    fn drop(&mut self) {
        // Whatever a test left running ends here, within the quit's limits.
        self.shell.quit_and_wait("the test is over");
        for stand_in in self.stage.started.lock().unwrap().iter() {
            stand_in.end(-1);
        }
    }
}

pub fn settings() -> Settings {
    let command = Command { program: "backend.exe".into(), args: vec!["--backend".into()], cwd: Some("C:\\somewhere".into()) };
    let mut settings = Settings::new(command, "C:\\Users\\A & B\\AppData\\Roaming\\Vispora");
    settings.version = VERSION.into();
    settings.ready_within = Duration::from_secs(3);
    settings.calls_wait = Duration::from_secs(3);
    settings.quit_within = Duration::from_millis(1500);
    settings.quit_unready_within = Duration::from_millis(400);
    settings.restarts = 2;
    settings.restart_pause = Duration::from_millis(20);
    settings.look_while_starting = Duration::from_millis(5);
    settings.look_when_ready = Duration::from_millis(5);
    settings
}

impl Rig {
    /// A shell whose backends behave as given, one behaviour per start (the
    /// last for every start after it).  Not started yet.
    pub fn with(behaviours: Vec<Behaviour>, settings: Settings) -> Rig {
        let stage = Arc::new(Stage::default());
        stage.behaviours.lock().unwrap().extend(behaviours);
        let shell = Shell::new(settings, Box::new(StageDoor(stage.clone()))).unwrap();
        let pane = Arc::new(Pane::default());
        *shell.window.lock().unwrap() = Some(pane.clone());
        let exits: Arc<Mutex<Vec<i32>>> = Arc::default();
        let seen = exits.clone();
        *shell.exit_hook.lock().unwrap() = Some(Arc::new(move |code| seen.lock().unwrap().push(code)));
        Rig { shell, stage, pane, exits }
    }

    /// A shell with a well-behaved backend, started and ready.
    pub fn ready() -> Rig {
        let rig = Rig::with(vec![Behaviour::default()], settings());
        rig.shell.start();
        assert_eq!(rig.wait_ready(), Phase::Ready, "{:?}", rig.shell.history());
        rig
    }

    pub fn wait_ready(&self) -> Phase {
        self.shell.wait_for(Duration::from_secs(10), |phase| *phase == Phase::Ready)
    }

    /// A call as the page makes it: with the secret.
    pub fn call(&self, method: &str, target: &str) -> Response {
        self.shell.handle(&self.asking(method, target))
    }

    pub fn asking(&self, method: &str, target: &str) -> Request {
        Request::new(method, target).with_header(token::HEADER, self.shell.token().as_str())
    }

    pub fn exits(&self) -> Vec<i32> {
        self.exits.lock().unwrap().clone()
    }
}

/// Wait (for real) until `condition` holds; false if it never does.
pub fn wait(condition: impl Fn() -> bool, seconds: f64) -> bool {
    let end = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < end {
        if condition() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    condition()
}

pub fn text(answer: &Response) -> String {
    String::from_utf8_lossy(&answer.body).into_owned()
}
