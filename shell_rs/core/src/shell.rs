//! The backend's start and stop, and quitting.
//!
//! The backend is the Python app, started as a program of its own with
//! `--backend` and a hand-over in its environment: the socket to serve on,
//! the run's secret, and who the shell is (app.py, "Running behind the
//! shell").
//!
//! The socket is the shell's.  It is made when the shell is, it cannot be
//! listened on by anybody else, and the shell keeps it until it ends; each
//! backend inherits it.  So every call the shell makes, the first "are you
//! up" included, goes to a port that only this shell's backends can answer
//! on.  That is what lets a call carry the run's secret: there is no moment
//! in which somebody else could be listening there.
//!
//! From the start of a backend on:
//!
//! * It is **starting** until it answers `/api/shell/hello` with the version
//!   this shell was built with.  Calls from the page that have to be passed
//!   on wait for that, up to a limit (router.rs).
//! * It is **ready** until its process ends.  If it ends on its own with exit
//!   code 0, it left because it meant to (an update is being installed) and
//!   the shell goes too.  If it ends any other way, it is started again, a
//!   few times; after that the shell says so and stops trying.
//! * **Quitting**, however it was asked for and however often: the window is
//!   taken out of sight at once, the backend is told to put the system back
//!   and go (`/api/window/quit`, the call the page always made), and the
//!   shell waits for its process to end.  Not for ever: after the limit the
//!   backend is ended.  Only then is the shell **gone**, and only then does
//!   its process exit.
//!
//! Nothing waits without a limit, and whoever asks for the quit, or waits for
//! it, waits for the same one.

use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::forward;
use crate::http::Request;
use crate::json::Json;
use crate::platform::{Child, Command, Listening, Platform, Window};
use crate::token::{self, Token};

/// What the shell is told when it is made.
#[derive(Debug, Clone)]
pub struct Settings {
    /// How the backend is started.
    pub backend: Command,
    /// The build this shell belongs to.  A backend that says another version
    /// is not this shell's backend.
    pub version: String,
    /// The folder the app keeps its data in (shown in the page).
    pub appdata_dir: String,
    /// Where the page lives, as its own requests name it.
    pub origin: String,
    /// How long a backend may take to answer for the first time.
    pub ready_within: Duration,
    /// How long one "are you up" may take, all of it.
    pub hello_within: Duration,
    /// How long a call from the page waits for a backend that is not up yet.
    pub calls_wait: Duration,
    /// How long a backend that has been up may take to put the system back
    /// and end, once it is told to quit.
    pub quit_within: Duration,
    /// The same for a backend that never came up: it has little to put back.
    pub quit_unready_within: Duration,
    /// How often a backend that stopped is started again: that many times
    /// within `restarts_within` for one that had been up, that many times
    /// in a row for one that never came up (however long each try took).
    /// And how long the shell waits before each.
    pub restarts: usize,
    pub restarts_within: Duration,
    pub restart_pause: Duration,
    /// How often a backend is looked at: while it starts, and once it is up.
    pub look_while_starting: Duration,
    pub look_when_ready: Duration,
    pub forward: forward::Limits,
    /// How long a call of the shell's own may take (the tray's: what to
    /// show, and what a menu entry asks for).
    pub own_calls_within: Duration,
    /// Where the shell's own exe is: what a person starts.  Handed to the
    /// backend, which needs it where it names the app's exe (the entry that
    /// starts the app at sign-in).  None where that is of no use (tests).
    pub shell_exe: Option<String>,
}

impl Settings {
    pub fn new(backend: Command, appdata_dir: &str) -> Settings {
        Settings {
            backend,
            version: crate::APP_VERSION.to_string(),
            appdata_dir: appdata_dir.to_string(),
            origin: "http://vispora.localhost".to_string(),
            // Python, Flask and the app's modules load in a second or two;
            // a cold disk and a virus scanner can make that many.
            ready_within: Duration::from_secs(90),
            hello_within: Duration::from_secs(2),
            calls_wait: Duration::from_secs(30),
            // Quitting undoes game mode, hands the graphics card back and
            // waits for FFmpeg and PresentMon to stop.  Nobody has timed it
            // on a real PC yet (docs/performance.md); half a minute is
            // meant to be far more than it takes.  A backend that is ended
            // at the limit has not put everything back: the next start does.
            quit_within: Duration::from_secs(30),
            quit_unready_within: Duration::from_secs(5),
            restarts: 3,
            restarts_within: Duration::from_secs(5 * 60),
            restart_pause: Duration::from_secs(1),
            look_while_starting: Duration::from_millis(50),
            look_when_ready: Duration::from_millis(250),
            forward: forward::Limits::default(),
            own_calls_within: Duration::from_secs(10),
            shell_exe: None,
        }
    }
}

/// Where the shell is with its backend.
#[derive(Debug, Clone, PartialEq)]
pub enum Phase {
    /// A backend has been started (or is about to be) and does not answer yet.
    Starting,
    Ready,
    /// The backend is gone and is not started again: why, in words for the
    /// person at the PC.
    Down(String),
    Quitting,
    Gone,
}

struct State {
    phase: Phase,
    child: Option<Box<dyn Child>>,
    /// A backend is being started right now: its process may exist already
    /// and is not in `child` yet.  A quit waits for this to be over.
    launching: bool,
    /// How the backend is started, where that had to be found out first
    /// (`prepare_hook`); `settings.backend` otherwise.
    backend: Option<Command>,
    /// How many backends in a row never came up.
    never_up_in_a_row: usize,
    /// Has a backend been up in this run?
    was_ready: bool,
    /// When backends were started.
    starts: Vec<Instant>,
    /// How long the last start took until the backend answered.
    ready_took: Option<Duration>,
    quit_took: Option<Duration>,
    exit_code: i32,
    history: Vec<String>,
}

type ExitHook = Arc<dyn Fn(i32) + Send + Sync>;
type LogHook = Arc<dyn Fn(&str) + Send + Sync>;
type PrepareHook = Arc<dyn Fn() -> Result<Command, String> + Send + Sync>;

pub struct Shell {
    pub settings: Settings,
    token: Token,
    platform: Box<dyn Platform>,
    /// The socket every backend serves on (platform.rs), for as long as the
    /// shell lives.
    listening: Box<dyn Listening>,
    state: Mutex<State>,
    /// Rung whenever `state.phase` changes, for everybody who waits for one.
    changed: Condvar,
    /// The window, once there is one.
    pub window: Mutex<Option<Arc<dyn Window>>>,
    /// Ends the process, once the shell is gone.
    pub exit_hook: Mutex<Option<ExitHook>>,
    /// Takes every line the shell has to say (the app writes them to its log).
    pub log_hook: Mutex<Option<LogHook>>,
    /// Finds out how the backend is started, where that takes finding out:
    /// a shell that carries its backend unpacks it first (backend.rs).
    /// Asked once, before the first start, on the thread that starts the
    /// backend, so that the window is not kept waiting for it.
    pub prepare_hook: Mutex<Option<PrepareHook>>,
}

/// What the page is told when the backend never came up.  It may be a PC
/// on which opening the app again does not help either (old network filter
/// software can keep a program from using a socket it inherits), so it
/// says where the reason is written down.
pub const NEVER_UP: &str = concat!(
    "Vispora's background program does not start. Close Vispora and open it again. ",
    "If that does not help, the reason is in the file vispora.log in Vispora's data folder."
);

/// How a backend that was being watched stopped being watched.
enum Watched {
    /// The shell is quitting: the end of the backend is the quit's business.
    Quitting,
    /// It ended on its own, cleanly, after having been up.
    Left,
    /// It is another build than this shell.
    Wrong(String),
    /// It ended after having been up.
    Stopped(String),
    /// It never came up: it ended first, or did not answer in time.
    NeverUp(String),
}

impl Shell {
    pub fn new(settings: Settings, platform: Box<dyn Platform>) -> Result<Arc<Shell>, String> {
        let token = Token::random(|bytes| platform.random(bytes))?;
        // Before anything is started, and before anybody is asked anything.
        let listening = platform.listen()?;
        Ok(Arc::new(Shell {
            settings,
            token,
            platform,
            listening,
            state: Mutex::new(State {
                phase: Phase::Starting,
                child: None,
                launching: false,
                backend: None,
                never_up_in_a_row: 0,
                was_ready: false,
                starts: Vec::new(),
                ready_took: None,
                quit_took: None,
                exit_code: 0,
                history: Vec::new(),
            }),
            changed: Condvar::new(),
            window: Mutex::new(None),
            exit_hook: Mutex::new(None),
            log_hook: Mutex::new(None),
            prepare_hook: Mutex::new(None),
        }))
    }

    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub(crate) fn window(&self) -> Option<Arc<dyn Window>> {
        self.window.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone()
    }

    /// The run's secret.
    pub fn token(&self) -> &Token {
        &self.token
    }

    /// What the window's address ends in, so that the page has the secret:
    /// `#t=...`.  A fragment is never sent anywhere; the page takes it out of
    /// its address bar when it has read it (static/js/app.js).
    pub fn fragment(&self) -> String {
        format!("#t={}", self.token.as_str())
    }

    pub fn phase(&self) -> Phase {
        self.state().phase.clone()
    }

    /// The port of the shell's own listening socket, on 127.0.0.1.
    pub fn port(&self) -> u16 {
        self.listening.port()
    }

    /// The number the backend finds that socket under.
    pub fn listening_handle(&self) -> u64 {
        self.listening.handle()
    }

    /// The backend's process id, while there is one.
    pub fn backend_pid(&self) -> Option<u32> {
        self.state().child.as_ref().map(|child| child.id())
    }

    pub fn starts(&self) -> usize {
        self.state().starts.len()
    }

    /// How long the last start took until the backend answered, and how long
    /// quitting took.
    pub fn timings(&self) -> (Option<Duration>, Option<Duration>) {
        let st = self.state();
        (st.ready_took, st.quit_took)
    }

    /// What the shell has said so far, oldest first.
    pub fn history(&self) -> Vec<String> {
        self.state().history.clone()
    }

    pub fn say(&self, line: &str) {
        {
            let mut st = self.state();
            if st.history.len() < 500 {
                st.history.push(line.to_string());
            }
        }
        let hook = self.log_hook.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone();
        if let Some(hook) = hook {
            hook(line);
        }
    }

    /// Wait until `wanted` says yes to the phase, at most `at_most`.  The
    /// phase it ended on.
    pub fn wait_for(&self, at_most: Duration, wanted: impl Fn(&Phase) -> bool) -> Phase {
        let give_up = Instant::now() + at_most;
        let mut st = self.state();
        loop {
            if wanted(&st.phase) {
                return st.phase.clone();
            }
            let left = give_up.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return st.phase.clone();
            }
            st = self.changed.wait_timeout(st, left).unwrap_or_else(|poisoned| poisoned.into_inner()).0;
        }
    }

    /// The port of a backend that is up, for a call that has to be passed
    /// on.  Waits for one that is starting; says why not otherwise.
    pub(crate) fn port_when_ready(&self) -> Result<u16, (u16, String)> {
        let give_up = Instant::now() + self.settings.calls_wait;
        let mut st = self.state();
        loop {
            match &st.phase {
                Phase::Ready => return Ok(self.listening.port()),
                Phase::Down(why) => return Err((503, why.clone())),
                Phase::Quitting | Phase::Gone => return Err((503, "Vispora is closing.".to_string())),
                Phase::Starting => {}
            }
            let left = give_up.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err((503, "Vispora is still starting. Try again in a moment.".to_string()));
            }
            st = self.changed.wait_timeout(st, left).unwrap_or_else(|poisoned| poisoned.into_inner()).0;
        }
    }

    // ─── Starting, and staying up ────────────────────────────────────

    /// Start the backend, and keep it: on a thread of its own, until the
    /// shell quits or gives up.
    pub fn start(self: &Arc<Self>) {
        let me = Arc::clone(self);
        let made = std::thread::Builder::new().name("shell-backend".into()).spawn(move || me.keep());
        if let Err(e) = made {
            self.give_up(format!("Vispora could not start its background program ({e}). Close it and open it again."));
        }
    }

    fn keep(self: &Arc<Self>) {
        if !self.prepared() {
            return;
        }
        loop {
            match self.start_backend() {
                Ok(true) => {}
                Ok(false) => return,
                Err(why) => {
                    self.say(&format!("the backend could not be started: {why}"));
                    self.give_up(format!("Vispora could not start its background program ({why}). Close it and open it again."));
                    return;
                }
            }
            let stopped = match self.watch() {
                Watched::Quitting => return,
                Watched::Left => {
                    self.say("the backend ended on its own (exit code 0): the shell goes too");
                    self.quit("the backend left");
                    return;
                }
                Watched::Wrong(why) => {
                    self.say(&why);
                    self.give_up("Vispora's files do not belong together. Download Vispora again.".to_string());
                    return;
                }
                Watched::Stopped(what) => what,
                Watched::NeverUp(what) => {
                    // Counted by tries, not by the clock: a try that fails
                    // slowly is still a try that failed.
                    let tries = self.state().never_up_in_a_row;
                    if tries > self.settings.restarts {
                        self.say(&format!("the backend did not come up ({what}), {tries} times in a row: not started again"));
                        self.give_up(NEVER_UP.to_string());
                        return;
                    }
                    what
                }
            };
            let recent = {
                let st = self.state();
                st.starts.iter().filter(|at| at.elapsed() <= self.settings.restarts_within).count()
            };
            if recent > self.settings.restarts {
                self.say(&format!("the backend stopped ({stopped}) for the {recent}. time in a row: not started again"));
                self.give_up("Vispora's background program keeps stopping. Close Vispora and open it again.".to_string());
                return;
            }
            self.say(&format!("the backend stopped ({stopped}): starting it again"));
            // A moment first, so that a backend that dies at once is not
            // started in a tight circle.  (A quit ends the wait.)
            if matches!(self.wait_for(self.settings.restart_pause, |phase| matches!(phase, Phase::Quitting | Phase::Gone)),
                        Phase::Quitting | Phase::Gone) {
                return;
            }
        }
    }

    /// Find out how the backend is started, where there is something to
    /// find out.  False when there is nothing to start: it could not be
    /// made ready, or the shell is quitting.
    fn prepared(&self) -> bool {
        let Some(prepare) = self.prepare_hook.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone() else { return true };
        {
            let mut st = self.state();
            if matches!(st.phase, Phase::Quitting | Phase::Gone) {
                return false;
            }
            // A quit waits for this as it waits for a start in flight: what
            // is being made ready is the quit's to take away again, and it
            // cannot take away what is still being made.
            st.launching = true;
        }
        let made = prepare();
        let mut st = self.state();
        st.launching = false;
        self.changed.notify_all();
        match made {
            Ok(command) => {
                st.backend = Some(command);
                true
            }
            Err(why) => {
                drop(st);
                self.say(&format!("the backend could not be made ready: {why}"));
                self.give_up(format!("Vispora could not get its background program ready ({why}). Close it and open it again."));
                false
            }
        }
    }

    /// The backend will not be started again.
    fn give_up(&self, why: String) {
        let mut st = self.state();
        if !matches!(st.phase, Phase::Quitting | Phase::Gone) {
            st.phase = Phase::Down(why);
            self.changed.notify_all();
        }
    }

    /// Start one backend.  False when the shell is quitting and none is
    /// wanted any more.
    fn start_backend(&self) -> Result<bool, String> {
        {
            let mut st = self.state();
            if matches!(st.phase, Phase::Quitting | Phase::Gone) {
                return Ok(false);
            }
            // From here until the new process is in `child`, a quit waits:
            // it must not be over while a backend is being started that it
            // has not seen.
            st.launching = true;
        }
        let port = self.listening.port();
        let handoff = Json::object([
            ("port", (port as u32).into()),
            ("listen", self.listening.handle().into()),
            ("token", self.token.as_str().into()),
            ("shell_pid", self.platform.own_pid().into()),
            ("shell_started", self.platform.own_start().into()),
            ("shell_exe", self.settings.shell_exe.as_deref().map_or(Json::Null, Json::from)),
        ])
        .to_string();
        let command = self.state().backend.clone().unwrap_or_else(|| self.settings.backend.clone());
        let started = self.platform.start(&command, &handoff);
        let mut st = self.state();
        st.launching = false;
        self.changed.notify_all();
        let child = started?;
        let pid = child.id();
        // In `child` whatever has happened meanwhile: if a quit has begun,
        // it is waiting for exactly this, and deals with the new backend as
        // with any other (tells it, waits, ends it).
        st.child = Some(child);
        st.starts.push(Instant::now());
        st.ready_took = None;
        let wanted = !matches!(st.phase, Phase::Quitting | Phase::Gone);
        if wanted {
            st.phase = Phase::Starting;
        }
        drop(st);
        self.say(&format!("backend started (pid {pid}, port {port})"));
        Ok(wanted)
    }

    /// Look at the backend until it stops, or until the shell quits.
    fn watch(&self) -> Watched {
        let started = Instant::now();
        let mut ready = false;
        loop {
            {
                let mut st = self.state();
                if matches!(st.phase, Phase::Quitting | Phase::Gone) {
                    return Watched::Quitting;
                }
                if let Some(code) = st.child.as_mut().and_then(|child| child.exit_code()) {
                    st.child = None;
                    if !ready {
                        st.never_up_in_a_row += 1;
                        return Watched::NeverUp(format!("it ended with exit code {code} before it answered"));
                    }
                    st.phase = Phase::Starting;
                    self.changed.notify_all();
                    return if code == 0 { Watched::Left } else { Watched::Stopped(format!("exit code {code}")) };
                }
            }
            if !ready {
                match self.hello() {
                    Some(version) if version == self.settings.version => {
                        let mut st = self.state();
                        if matches!(st.phase, Phase::Quitting | Phase::Gone) {
                            return Watched::Quitting;
                        }
                        st.phase = Phase::Ready;
                        st.was_ready = true;
                        st.never_up_in_a_row = 0;
                        st.ready_took = Some(started.elapsed());
                        self.changed.notify_all();
                        drop(st);
                        ready = true;
                        self.say(&format!("backend ready after {} ms", started.elapsed().as_millis()));
                    }
                    Some(other) => {
                        self.end_backend();
                        return Watched::Wrong(format!(
                            "the backend is version {other:?} and the shell is {:?}: not this shell's backend",
                            self.settings.version
                        ));
                    }
                    None if started.elapsed() > self.settings.ready_within => {
                        self.end_backend();
                        self.state().never_up_in_a_row += 1;
                        return Watched::NeverUp(format!("it did not answer within {} s", self.settings.ready_within.as_secs()));
                    }
                    None => {}
                }
            }
            std::thread::sleep(if ready { self.settings.look_when_ready } else { self.settings.look_while_starting });
        }
    }

    /// Ask the backend whether it is up.  Its version when it is.
    ///
    /// Asked on the shell's own socket, so whoever answers is a backend this
    /// shell started.  While none is accepting yet, the question waits in
    /// the socket's queue and is given up on after `hello_within`, all in
    /// all: an answer that trickles in is not waited for either.
    fn hello(&self) -> Option<String> {
        let ask = Request::new("GET", "/api/shell/hello").with_header(token::HEADER, self.token.as_str());
        let limits = forward::Limits {
            connect: Duration::from_millis(250),
            silence: self.settings.hello_within,
            whole: Some(self.settings.hello_within),
            body: 64 * 1024,
        };
        let answer = forward::send(self.listening.port(), &ask, &limits).ok()?;
        let said = answer.to_json();
        if answer.status != 200 || !said["ok"].truthy() {
            return None;
        }
        Some(said["version"].as_str().unwrap_or("").to_string())
    }

    /// Ask the backend something on the shell's own behalf, with the run's
    /// secret (the tray does: what to show, and what a menu entry asks
    /// for).  Only a backend that is up is asked.  None when there is none,
    /// or when it does not answer "200" in time.
    pub fn ask_backend(&self, method: &str, path: &str) -> Option<Json> {
        if self.phase() != Phase::Ready {
            return None;
        }
        let mut ask = Request::new(method, path).with_header(token::HEADER, self.token.as_str());
        if method != "GET" {
            ask = ask.with_header("Content-Type", "application/json").with_body(b"{}".to_vec());
        }
        let limits = forward::Limits {
            connect: Duration::from_millis(250),
            silence: self.settings.own_calls_within,
            whole: Some(self.settings.own_calls_within),
            body: 64 * 1024,
        };
        let answer = forward::send(self.listening.port(), &ask, &limits).ok()?;
        (answer.status == 200).then(|| answer.to_json())
    }

    /// End the backend now, without asking it.
    fn end_backend(&self) {
        let child = self.state().child.take();
        if let Some(mut child) = child {
            child.kill();
        }
    }

    // ─── Quitting ────────────────────────────────────────────────────

    /// Begin to quit, and come back at once.  However many ask, and whoever
    /// asks first, there is one quit.
    pub fn quit(self: &Arc<Self>, reason: &str) {
        {
            let mut st = self.state();
            if matches!(st.phase, Phase::Quitting | Phase::Gone) {
                return;
            }
            st.phase = Phase::Quitting;
            self.changed.notify_all();
        }
        self.say(&format!("quitting ({reason})"));
        let me = Arc::clone(self);
        if std::thread::Builder::new().name("shell-quit".into()).spawn(move || me.finish()).is_err() {
            // (no thread to be had: then on this one)
            self.finish();
        }
    }

    /// Quit, and come back when the backend's process has ended.  The exit
    /// code for the shell's own process.  Everybody who calls this waits for
    /// the same quit, and nobody waits longer than the quit's own limits.
    pub fn quit_and_wait(self: &Arc<Self>, reason: &str) -> i32 {
        self.quit(reason);
        // (a little more than the quit can take, so that a mistake in it is a
        // failed check and not a wait without an end)
        let slack = (self.settings.quit_within / 4).max(Duration::from_secs(2));
        let at_most = self.settings.quit_within + self.settings.quit_unready_within + slack;
        self.wait_for(at_most, |phase| *phase == Phase::Gone);
        self.state().exit_code
    }

    /// The quit itself.  Runs once.
    fn finish(self: &Arc<Self>) {
        let began = Instant::now();
        // Out of sight first: to the person at the PC, that is the quit.
        if let Some(window) = self.window() {
            window.hide();
        }
        // A backend that is being started this very moment is waited for:
        // its process may exist already, and a quit that did not see it
        // would be over, and the shell's process gone, with a backend left
        // behind that is just beginning its start-up work.
        let (port, was_ready) = {
            let patience = began + self.settings.quit_within;
            let mut st = self.state();
            while st.launching && Instant::now() < patience {
                st = self.changed.wait_timeout(st, Duration::from_millis(50)).unwrap_or_else(|poisoned| poisoned.into_inner()).0;
            }
            (self.listening.port(), st.was_ready)
        };
        let limit = if was_ready { self.settings.quit_within } else { self.settings.quit_unready_within };
        let give_up = Instant::now().max(began) + limit;

        // Tell the backend, on a thread of its own: its answer only comes
        // when it has put the system back, and the wait below is for its
        // process, whatever becomes of the answer.
        if self.state().child.is_some() {
            let me = Arc::clone(self);
            let _ = std::thread::Builder::new().name("shell-quit-call".into()).spawn(move || {
                let ask = Request::new("POST", "/api/window/quit")
                    .with_header(token::HEADER, me.token.as_str())
                    .with_header("Content-Type", "application/json")
                    .with_body("{}");
                let limits = forward::Limits {
                    connect: Duration::from_millis(250),
                    silence: limit + Duration::from_secs(1),
                    whole: Some(limit + Duration::from_secs(1)),
                    body: 64 * 1024,
                };
                while Instant::now() < give_up && me.state().child.is_some() {
                    match forward::send(port, &ask, &limits) {
                        Ok(answer) => {
                            me.say(&format!("the backend took the quit (status {})", answer.status));
                            return;
                        }
                        // (not listening yet, or not any more: ask again shortly)
                        Err(_) => std::thread::sleep(Duration::from_millis(100)),
                    }
                }
            });
        }

        let code = loop {
            {
                let mut st = self.state();
                match st.child.as_mut().map(|child| child.exit_code()) {
                    None => break 0,
                    Some(Some(code)) => {
                        st.child = None;
                        drop(st);
                        self.say(&format!("the backend ended (exit code {code}) {} ms after the quit began", began.elapsed().as_millis()));
                        break 0;
                    }
                    Some(None) => {}
                }
            }
            if Instant::now() >= give_up {
                self.end_backend();
                self.say(&format!(
                    "the backend had not ended {} s after it was told to quit, and was ended. What it had not put back, the next start puts back.",
                    limit.as_secs()
                ));
                break 0;
            }
            std::thread::sleep(self.settings.look_while_starting);
        };

        {
            let mut st = self.state();
            st.phase = Phase::Gone;
            st.exit_code = code;
            st.quit_took = Some(began.elapsed());
            self.changed.notify_all();
        }
        let hook = self.exit_hook.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone();
        if let Some(hook) = hook {
            hook(code);
        }
    }
}
