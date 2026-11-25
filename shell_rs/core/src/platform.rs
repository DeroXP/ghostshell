//! The shell's doors to the outside, so that the tests can stand in for
//! them: starting the backend, the socket the backend serves on, and the
//! window.  (Calls to the backend are real connections to 127.0.0.1 in the
//! tests too: forward.rs.)

/// How the backend is started.
#[derive(Debug, Clone, PartialEq)]
pub struct Command {
    pub program: String,
    pub args: Vec<String>,
    /// The folder it is started in; the shell's own when None.
    pub cwd: Option<String>,
}

/// The name of the environment variable the backend finds its hand-over in
/// (app.py: _SHELL_HANDOFF_VAR).
pub const HANDOFF_VARIABLE: &str = "VISPORA_SHELL";

/// A backend that was started.
pub trait Child: Send {
    fn id(&self) -> u32;
    /// Its exit code once it has ended, None while it runs.
    fn exit_code(&mut self) -> Option<i32>;
    /// End it now.  The last resort: a backend that is ended this way does
    /// not put the system back.
    fn kill(&mut self);
}

/// The socket the backend serves on.
///
/// The shell makes it, before any backend exists, and keeps it for as long
/// as the shell lives.  Every backend the shell starts inherits it and
/// accepts the shell's calls on it; the backend never opens a port of its
/// own.  So the port is never free and never anybody else's: not in the
/// moment before a backend is up, not while one is being started again, not
/// after one has died.  That matters because every call carries the run's
/// secret, and a call goes to whoever is listening.
pub trait Listening: Send + Sync {
    /// The port on 127.0.0.1.
    fn port(&self) -> u16;
    /// The number a program started by this one finds the socket under.
    fn handle(&self) -> u64;
}

pub trait Platform: Send + Sync {
    /// A socket listening on 127.0.0.1, on a port Windows picks, that no
    /// other program can listen on as well and that a program started with
    /// `start` inherits.
    fn listen(&self) -> Result<Box<dyn Listening>, String>;
    /// Start the backend, with `handoff` in its environment under
    /// HANDOFF_VARIABLE and nowhere else (not on its command line, which
    /// other programs can read).
    fn start(&self, command: &Command, handoff: &str) -> Result<Box<dyn Child>, String>;
    /// Random bytes, for the run's secret.
    fn random(&self, bytes: &mut [u8]) -> Result<(), String>;
    /// This process's id, which the backend watches: when the shell is gone,
    /// the backend puts the system back and goes too.
    fn own_pid(&self) -> u32;
    /// When this process was started, as Windows counts it.  Handed over
    /// with the id: an id is given out again once its process has ended,
    /// and the two together tell the shell from whoever has its number
    /// later.
    fn own_start(&self) -> u64;
}

/// The window, as far as the page's calls reach it.  Each answer says
/// whether it was done.
pub trait Window: Send + Sync {
    fn minimize(&self) -> bool;
    /// Out of sight, into the tray.
    fn hide(&self) -> bool;
    fn show(&self) -> bool;
}
