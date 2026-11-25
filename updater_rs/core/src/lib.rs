//! Vispora's standalone updater, without its window.
//!
//! This is the Python updater (updater/, version 1.5.0) in Rust: the same
//! command line, the same places asked in the same order, the same checks
//! before a download is trusted, the same backup, swap and rollback, the same
//! install_info.json and log file.  The Python tests for that updater are the
//! specification, ported in tests/.
//!
//! What is where:
//!   sources.rs    where releases are published and what counts as one
//!                 (update_sources.py)
//!   installer.rs  the pipeline: find, close, download, verify, swap, launch
//!                 (updater/installer.py)
//!   server.rs     what the window asks and is told (updater/server.py)
//!   cli.rs        the command line and the start-up (updater/main.py)
//!   state.rs      what the window is shown
//!   paths.rs      names and folders (updater/paths.py)
//!   platform.rs   the doors to the outside: network, processes, disk, clock
//!   win.rs        the real ones
//!   json.rs, sha256.rs
//!
//! It uses no crates from outside this folder.  The window is a separate
//! crate (updater_rs/app) that only shows what this one does.

#![cfg(windows)]

pub mod cli;
pub mod installer;
pub mod json;
pub mod paths;
pub mod platform;
pub mod server;
pub mod sha256;
pub mod sources;
pub mod state;
pub mod win;

/// This updater's own version.  The app tells updaters apart by it (1.5.0 was
/// the first that knew GitHub and --source), and a release carries the file
/// as VisporaUpdater-<version>.exe.  2.0.0 is the first one in Rust.
pub const UPDATER_VERSION: &str = env!("CARGO_PKG_VERSION");

pub use installer::Updater;
