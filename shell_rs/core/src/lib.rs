//! The Rust shell for Vispora, without its window.
//!
//! The shell is to become Vispora.exe (docs/rust-port.md, step 3): it opens
//! the window at once, starts the Python backend as a program of its own,
//! answers the page's calls itself where a Rust handler exists and passes
//! every other call on to the backend, and owns quitting.
//!
//! This crate is everything of that which needs no window:
//!
//! * `router`: which request is answered where, and which is refused;
//! * `page`: the interface files, as the backend serves them today;
//! * `forward`: passing a call on to the backend, on 127.0.0.1 and nowhere
//!   else;
//! * `shell`: starting the backend, noticing when it stops, starting it
//!   again, and quitting with a time limit;
//! * `instance`: is this the one copy, and does it have the rights it
//!   needs;
//! * `token`: the secret of one run, which tells the shell's own page from a
//!   stranger and the shell's calls from anybody else's;
//! * `tray`: which picture the tray icon shows, its tooltip and its menu,
//!   decided from what the backend says;
//! * `trouble`: what the person is told when the app cannot start;
//! * `platform` and `win`: the doors to the outside (starting a program,
//!   random numbers), and the real ones.
//!
//! The window and the tray icon themselves are the crate next to this one
//! (shell_rs/app).  Nothing in a release uses this crate yet.
//!
//! No outside crates (see Cargo.toml).

#![cfg(windows)]

pub mod backend;
pub mod forward;
pub mod http;
pub mod instance;
pub mod pack;
pub mod page;
pub mod platform;
pub mod router;
pub mod shell;
pub mod token;
pub mod tray;
pub mod trouble;
pub mod win;

pub use vispora_updater_core::json;
/// Where the app keeps its data (the folder the page shows, and the logs).
pub use vispora_updater_core::paths;

/// The local time, as the app's logs write it.
pub fn stamp() -> String {
    vispora_updater_core::win::platform().clock.stamp()
}

/// The app's version and name, from config.py (see build.rs).
pub const APP_VERSION: &str = env!("VISPORA_APP_VERSION");
pub const APP_NAME: &str = env!("VISPORA_APP_NAME");

pub use shell::{Settings, Shell};
