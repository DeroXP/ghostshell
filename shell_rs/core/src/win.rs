//! The real doors (see platform.rs): a program started without a console
//! window, a free port, and Windows' own random numbers.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::net::TcpListener;
use std::os::windows::process::CommandExt as _;
use std::process::Stdio;

use crate::platform::{Child, Command, Platform, HANDOFF_VARIABLE};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 2;

#[link(name = "bcrypt")]
extern "system" {
    fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, length: u32, flags: u32) -> i32;
}

pub struct Windows;

/// The real platform.
pub fn platform() -> Box<dyn Platform> {
    Box::new(Windows)
}

struct Started(std::process::Child);

impl Child for Started {
    fn id(&self) -> u32 {
        self.0.id()
    }

    fn exit_code(&mut self) -> Option<i32> {
        match self.0.try_wait() {
            Ok(Some(status)) => Some(status.code().unwrap_or(-1)),
            Ok(None) => None,
            // (it cannot be asked any more: for the shell, that is a backend that is gone)
            Err(_) => Some(-1),
        }
    }

    fn kill(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl Platform for Windows {
    fn start(&self, command: &Command, handoff: &str) -> Result<Box<dyn Child>, String> {
        let mut process = std::process::Command::new(&command.program);
        process
            .args(&command.args)
            .env(HANDOFF_VARIABLE, handoff)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        if let Some(folder) = &command.cwd {
            process.current_dir(folder);
        }
        process.spawn().map(|child| Box::new(Started(child)) as Box<dyn Child>).map_err(|e| format!("{}: {e}", command.program))
    }

    fn free_port(&self) -> Result<u16, String> {
        // Windows hands out a port nobody has; it is let go of at once, for
        // the backend to take.
        let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
        listener.local_addr().map(|address| address.port()).map_err(|e| e.to_string())
    }

    fn random(&self, bytes: &mut [u8]) -> Result<(), String> {
        // SAFETY: the buffer is `bytes`, with its own length.
        let status = unsafe { BCryptGenRandom(std::ptr::null_mut(), bytes.as_mut_ptr(), bytes.len() as u32, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
        if status == 0 {
            Ok(())
        } else {
            Err(format!("Windows gave no random numbers (status {status:#x})"))
        }
    }

    fn own_pid(&self) -> u32 {
        std::process::id()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_random_numbers_are_random() {
        let (mut a, mut b) = ([0u8; 32], [0u8; 32]);
        Windows.random(&mut a).unwrap();
        Windows.random(&mut b).unwrap();
        assert_ne!(a, b);
        assert!(a.iter().any(|x| *x != a[0]));
    }

    #[test]
    fn a_free_port_can_be_listened_on() {
        let port = Windows.free_port().unwrap();
        assert!(port >= 1024);
        assert!(TcpListener::bind(("127.0.0.1", port)).is_ok());
    }

    #[test]
    fn a_program_is_started_with_the_hand_over_in_its_environment_only() {
        // cmd.exe, which is on every Windows: it ends with the exit code 7
        // when the variable is what was handed over, and with 3 otherwise.
        let command = Command {
            program: "cmd.exe".into(),
            args: vec!["/d".into(), "/c".into(), format!("if \"%{HANDOFF_VARIABLE}%\"==\"here it is\" (exit 7) else (exit 3)")],
            cwd: None,
        };
        let mut child = Windows.start(&command, "here it is").unwrap();
        assert!(child.id() > 0);
        let ends = std::time::Instant::now() + std::time::Duration::from_secs(20);
        let code = loop {
            if let Some(code) = child.exit_code() {
                break code;
            }
            assert!(std::time::Instant::now() < ends, "cmd.exe did not end");
            std::thread::sleep(std::time::Duration::from_millis(20));
        };
        assert_eq!(code, 7);
        assert!(std::env::var(HANDOFF_VARIABLE).is_err(), "the shell's own environment does not carry it");
        // A program that is not there is an error, not a panic.
        let missing = Command { program: "no-such-program-here.exe".into(), args: vec![], cwd: None };
        assert!(Windows.start(&missing, "x").is_err());
        // Ending one that would run on.
        let waits = Command { program: "cmd.exe".into(), args: vec!["/d".into(), "/c".into(), "pause".into()], cwd: None };
        let mut child = Windows.start(&waits, "x").unwrap();
        // (stdin is closed for it, so `pause` may end on its own; either way it is gone after kill)
        child.kill();
        assert!(child.exit_code().is_some());
    }
}
