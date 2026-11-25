//! The shell's doors to the outside, so that the tests can stand in for
//! them: starting the backend, and the window.  (Calls to the backend are
//! real connections to 127.0.0.1 in the tests too: forward.rs.)

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

pub trait Platform: Send + Sync {
    /// Start the backend, with `handoff` in its environment under
    /// HANDOFF_VARIABLE and nowhere else (not on its command line, which
    /// other programs can read).
    fn start(&self, command: &Command, handoff: &str) -> Result<Box<dyn Child>, String>;
    /// A port on 127.0.0.1 that nothing listens on right now.
    fn free_port(&self) -> Result<u16, String>;
    /// Random bytes, for the run's secret.
    fn random(&self, bytes: &mut [u8]) -> Result<(), String>;
    /// This process's id, which the backend watches: when the shell is gone,
    /// the backend puts the system back and goes too.
    fn own_pid(&self) -> u32;
}

/// The window, as far as the page's calls reach it.  Each answer says
/// whether it was done.
pub trait Window: Send + Sync {
    fn minimize(&self) -> bool;
    /// Out of sight, into the tray.
    fn hide(&self) -> bool;
    fn show(&self) -> bool;
}
