//! The updater's four doors to the outside: the network, other processes,
//! the disk operation that can fail half-way, and the clock.
//!
//! The pipeline only ever goes through these.  The real ones are in win.rs;
//! the tests put a table of answers, a list of fake processes, a disk that
//! fails on demand and a clock that does not wait in their place, so that no
//! test downloads anything, closes anything or touches a real install.

use std::io;

/// The body of an answer that is being downloaded.
pub trait Body {
    /// What the server said the length is, if it said.
    fn content_length(&self) -> Option<u64>;
    /// The next bytes, into `buf`; 0 at the end.
    fn read(&mut self, buf: &mut [u8]) -> Result<usize, String>;
}

pub trait Net: Send + Sync {
    /// GET `url` and hand back its body to read piece by piece.  Anything but
    /// a 2xx answer (after following redirects) is an error.
    fn open(&self, url: &str, headers: &[(&str, &str)], timeout_s: u32) -> Result<Box<dyn Body>, String>;

    /// GET `url` and read all of it (the small JSON answers).
    fn get(&self, url: &str, headers: &[(&str, &str)], timeout_s: u32) -> Result<Vec<u8>, String> {
        let mut body = self.open(url, headers, timeout_s)?;
        let mut out = Vec::new();
        let mut buf = [0u8; 65536];
        loop {
            let n = body.read(&mut buf)?;
            if n == 0 {
                return Ok(out);
            }
            out.extend_from_slice(&buf[..n]);
            if out.len() > 64 * 1024 * 1024 {
                return Err("the answer is far too large to be one".into());
            }
        }
    }
}

pub trait Processes: Send + Sync {
    /// Run a command (tasklist, taskkill) hidden, wait for it, and hand back
    /// what it printed.
    fn run(&self, args: &[String], timeout_s: u32) -> Result<String, String>;
    /// Start a program and let it go (the app, once it is installed).
    fn spawn(&self, args: &[String]) -> Result<(), String>;
}

pub trait Disk: Send + Sync {
    /// Copy a file with its timestamps, replacing `to`.  The one disk
    /// operation the swap is built around, and the one that fails in real
    /// life (antivirus holding the new exe, a full disk).
    fn copy(&self, from: &str, to: &str) -> io::Result<()>;
}

pub trait Clock: Send + Sync {
    /// Seconds since 1970.
    fn now(&self) -> f64;
    fn sleep(&self, seconds: f64);
    /// The local time as "2026-11-24T22:15:03", for the log.
    fn stamp(&self) -> String;
}

/// Everything outside the updater, in one place.
pub struct Platform {
    pub net: Box<dyn Net>,
    pub procs: Box<dyn Processes>,
    pub disk: Box<dyn Disk>,
    pub clock: Box<dyn Clock>,
}
