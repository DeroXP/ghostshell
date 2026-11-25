//! The real doors (see platform.rs): the socket the backend serves on, a
//! program started without a console window, and Windows' own random
//! numbers.
//!
//! Windows functions are declared by hand, as in native/ and updater_rs/:
//! few enough that a bindings crate would only add outside code.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::os::windows::process::CommandExt as _;
use std::process::Stdio;

use crate::platform::{Child, Command, Listening, Platform, HANDOFF_VARIABLE};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 2;
const HANDLE_FLAG_INHERIT: u32 = 1;

const AF_INET: i32 = 2;
const SOCK_STREAM: i32 = 1;
const IPPROTO_TCP: i32 = 6;
const SOL_SOCKET: i32 = 0xFFFF;
/// "Nobody else may listen here": the opposite number of SO_REUSEADDR (4).
const SO_EXCLUSIVEADDRUSE: i32 = !4;
const INVALID_SOCKET: usize = usize::MAX;
/// How many calls may wait to be accepted (while a backend starts, say).
const WAITING_CALLS: i32 = 128;

/// An address as Windows' sockets take it: 127.0.0.1 and a port.
#[repr(C)]
struct Address {
    family: u16,
    /// (the high byte first)
    port: [u8; 2],
    host: [u8; 4],
    unused: [u8; 8],
}

#[link(name = "bcrypt")]
extern "system" {
    fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, length: u32, flags: u32) -> i32;
}

#[link(name = "ws2_32")]
extern "system" {
    fn WSAStartup(version: u16, data: *mut u8) -> i32;
    fn WSAGetLastError() -> i32;
    fn socket(family: i32, kind: i32, protocol: i32) -> usize;
    fn setsockopt(socket: usize, level: i32, name: i32, value: *const u8, length: i32) -> i32;
    fn bind(socket: usize, address: *const Address, length: i32) -> i32;
    fn listen(socket: usize, waiting: i32) -> i32;
    fn getsockname(socket: usize, address: *mut Address, length: *mut i32) -> i32;
    fn closesocket(socket: usize) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn SetHandleInformation(handle: *mut c_void, mask: u32, flags: u32) -> i32;
    fn GetCurrentProcess() -> *mut c_void;
    fn GetProcessTimes(process: *mut c_void, created: *mut u64, ended: *mut u64, kernel: *mut u64, user: *mut u64) -> i32;
}

pub struct Windows;

/// The real platform.
pub fn platform() -> Box<dyn Platform> {
    Box::new(Windows)
}

/// The shell's listening socket.  Closed when the shell lets go of it, which
/// is when the shell ends.
struct Socket {
    socket: usize,
    port: u16,
}

impl Listening for Socket {
    fn port(&self) -> u16 {
        self.port
    }
    fn handle(&self) -> u64 {
        self.socket as u64
    }
}

impl Drop for Socket {
    fn drop(&mut self) {
        // SAFETY: a socket this value made and nobody else closes.
        unsafe { closesocket(self.socket) };
    }
}

fn socket_error(what: &str) -> String {
    // SAFETY: reads the calling thread's last socket error.
    format!("{what} (Windows socket error {})", unsafe { WSAGetLastError() })
}

/// A socket of this process that is not listening yet.
fn new_socket() -> Result<usize, String> {
    // (asking Windows for its sockets, which every program has to do first;
    // it may be asked more than once)
    let mut data = [0u64; 64];
    // SAFETY: `data` is larger than the structure Windows fills in.
    let status = unsafe { WSAStartup(0x0202, data.as_mut_ptr() as *mut u8) };
    if status != 0 {
        return Err(format!("Windows gave no sockets (error {status})"));
    }
    // SAFETY: plain values.
    let made = unsafe { socket(AF_INET, SOCK_STREAM, IPPROTO_TCP) };
    if made == INVALID_SOCKET {
        return Err(socket_error("no socket could be made"));
    }
    Ok(made)
}

/// Bind `socket` to 127.0.0.1 and `port` (0: one Windows picks).
fn bind_local(socket: usize, port: u16) -> bool {
    let address = Address { family: AF_INET as u16, port: port.to_be_bytes(), host: [127, 0, 0, 1], unused: [0; 8] };
    // SAFETY: the address is a whole one and outlives the call.
    unsafe { bind(socket, &address, std::mem::size_of::<Address>() as i32) == 0 }
}

impl Platform for Windows {
    fn listen(&self) -> Result<Box<dyn Listening>, String> {
        let made = new_socket()?;
        // From here on `made` is closed by the Socket's Drop on every way out.
        let mut made = Socket { socket: made, port: 0 };
        let yes = 1i32.to_ne_bytes();
        // SAFETY: the option's value is four bytes that outlive the call;
        // the address read back is a whole one, with its length.
        unsafe {
            // Before the address is taken: with this, no other program can
            // listen on it as well, whatever that program asks for.
            if setsockopt(made.socket, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, yes.as_ptr(), 4) != 0 {
                return Err(socket_error("the socket could not be made the shell's alone"));
            }
            if !bind_local(made.socket, 0) {
                return Err(socket_error("no port on 127.0.0.1 could be taken"));
            }
            if listen(made.socket, WAITING_CALLS) != 0 {
                return Err(socket_error("the socket could not be listened on"));
            }
            let mut address = Address { family: 0, port: [0; 2], host: [0; 4], unused: [0; 8] };
            let mut length = std::mem::size_of::<Address>() as i32;
            if getsockname(made.socket, &mut address, &mut length) != 0 || address.host != [127, 0, 0, 1] {
                return Err(socket_error("the socket's port could not be read"));
            }
            made.port = u16::from_be_bytes(address.port);
            // The backend finds it among the handles it inherits.
            if SetHandleInformation(made.socket as *mut c_void, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) == 0 {
                return Err("the socket could not be made inheritable".to_string());
            }
        }
        Ok(Box::new(made))
    }

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

    fn own_start(&self) -> u64 {
        let (mut created, mut ended, mut kernel, mut user) = (0u64, 0u64, 0u64, 0u64);
        // SAFETY: four places for the four times, and this process itself.
        unsafe { GetProcessTimes(GetCurrentProcess(), &mut created, &mut ended, &mut kernel, &mut user) };
        created
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{TcpListener, TcpStream};
    use std::time::Duration;

    #[link(name = "kernel32")]
    extern "system" {
        fn GetHandleInformation(handle: *mut c_void, flags: *mut u32) -> i32;
    }

    #[link(name = "ws2_32")]
    extern "system" {
        fn getsockopt(socket: usize, level: i32, name: i32, value: *mut u8, length: *mut i32) -> i32;
    }

    const SO_REUSEADDR: i32 = 4;

    /// Can another socket listen on `port` as well?  Tried the plain way
    /// and the way that gets past a listener that asked for a shareable
    /// address (which is how a second program gets onto a web server's port).
    fn can_be_joined(port: u16) -> (bool, bool) {
        let plain = TcpListener::bind(("127.0.0.1", port)).is_ok();
        let other = new_socket().unwrap();
        let yes = 1i32.to_ne_bytes();
        // SAFETY: as in listen() above.
        let sharing = unsafe {
            setsockopt(other, SOL_SOCKET, SO_REUSEADDR, yes.as_ptr(), 4);
            let bound = bind_local(other, port);
            closesocket(other);
            bound
        };
        (plain, sharing)
    }

    #[test]
    fn the_shells_socket_cannot_be_joined_by_anybody() {
        let listening = Windows.listen().unwrap();
        let port = listening.port();
        assert!(port >= 1024);
        // Asked for, not left to what Windows does with a socket that asks for nothing.
        let (mut alone, mut length) = ([0u8; 4], 4i32);
        // SAFETY: four bytes for the option's value, and their length.
        let read = unsafe { getsockopt(listening.handle() as usize, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, alone.as_mut_ptr(), &mut length) };
        assert_eq!((read, i32::from_ne_bytes(alone) != 0), (0, true), "the socket is not marked as the shell's alone");
        assert_eq!(can_be_joined(port), (false, false), "another listener got onto the shell's port");
        // A call to it is taken in at once and waits: nobody has to be
        // accepting yet, and nobody else can.
        let waiting = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(2));
        assert!(waiting.is_ok());
        assert_eq!(can_be_joined(port), (false, false));
        // Let go of, the port is anybody's again: the shell holds it for as
        // long as it lives, and no longer.
        drop(waiting);
        drop(listening);
        assert_eq!(can_be_joined(port), (true, true));
    }

    #[test]
    fn a_socket_that_asks_for_a_shareable_address_can_be_joined() {
        // The other half of the reason: this is what a web server's own
        // listener asks for (Werkzeug does), and why the backend does not
        // make its own.  If this ever stops being true, the test above
        // proves less than it seems to.
        let first = new_socket().unwrap();
        let yes = 1i32.to_ne_bytes();
        // SAFETY: as in listen() above.
        let port = unsafe {
            setsockopt(first, SOL_SOCKET, SO_REUSEADDR, yes.as_ptr(), 4);
            assert!(bind_local(first, 0));
            listen(first, 8);
            let mut address = Address { family: 0, port: [0; 2], host: [0; 4], unused: [0; 8] };
            let mut length = std::mem::size_of::<Address>() as i32;
            getsockname(first, &mut address, &mut length);
            u16::from_be_bytes(address.port)
        };
        assert!(can_be_joined(port).1, "a second listener on a shareable address");
        // SAFETY: the socket made above.
        unsafe { closesocket(first) };
    }

    #[test]
    fn the_shells_socket_is_inherited_and_its_number_is_what_is_handed_over() {
        let listening = Windows.listen().unwrap();
        let mut flags = 0u32;
        // SAFETY: a handle of this process and a place for its flags.
        assert_ne!(unsafe { GetHandleInformation(listening.handle() as *mut c_void, &mut flags) }, 0);
        assert_eq!(flags & HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        // Two shells on one PC do not get in each other's way.
        let second = Windows.listen().unwrap();
        assert_ne!(listening.port(), second.port());
        assert_ne!(listening.handle(), second.handle());
    }

    #[test]
    fn windows_random_numbers_are_random() {
        let (mut a, mut b) = ([0u8; 32], [0u8; 32]);
        Windows.random(&mut a).unwrap();
        Windows.random(&mut b).unwrap();
        assert_ne!(a, b);
        assert!(a.iter().any(|x| *x != a[0]));
    }

    #[test]
    fn this_process_knows_when_it_was_started() {
        let started = Windows.own_start();
        // (in hundred-nanosecond steps since 1601: later than 2020, and the same when asked again)
        assert!(started > 132_000_000_000_000_000, "{started}");
        assert_eq!(started, Windows.own_start());
        assert_eq!(Windows.own_pid(), std::process::id());
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
        let ends = std::time::Instant::now() + Duration::from_secs(20);
        let code = loop {
            if let Some(code) = child.exit_code() {
                break code;
            }
            assert!(std::time::Instant::now() < ends, "cmd.exe did not end");
            std::thread::sleep(Duration::from_millis(20));
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
