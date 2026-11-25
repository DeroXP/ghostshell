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

// ═════════════════════════════════════════════════════════════════════
// The one copy (instance.rs), other programs, and a plain message
// ═════════════════════════════════════════════════════════════════════

const SYNCHRONIZE: u32 = 0x0010_0000;
const EVENT_MODIFY_STATE: u32 = 2;
const ERROR_ACCESS_DENIED: u32 = 5;
const ERROR_ALREADY_EXISTS: u32 = 183;
const ERROR_CANCELLED: u32 = 1223;
const TOKEN_QUERY: u32 = 8;
const TOKEN_ELEVATION: u32 = 20;
const INFINITE: u32 = 0xFFFF_FFFF;
const SW_SHOWNORMAL: i32 = 1;

/// Who may do what with the "show your window" event: anybody signed in at
/// this PC's screen may ring it and wait on it (a second start has no
/// administrator rights when it rings), administrators and Windows itself
/// everything.  The last part says that a program without administrator
/// rights may ring it although one with them made it.
const WHO_MAY_RING: &str = "D:(A;;0x00100002;;;IU)(A;;GA;;;BA)(A;;GA;;;SY)S:(ML;;NW;;;ME)";

#[repr(C)]
struct SecurityAttributes {
    length: u32,
    descriptor: *mut c_void,
    inherit: i32,
}

#[link(name = "kernel32")]
extern "system" {
    fn GetLastError() -> u32;
    fn CloseHandle(handle: *mut c_void) -> i32;
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn CreateMutexW(attributes: *const SecurityAttributes, owned: i32, name: *const u16) -> *mut c_void;
    fn OpenMutexW(access: u32, inherit: i32, name: *const u16) -> *mut c_void;
    fn CreateEventW(attributes: *const SecurityAttributes, manual: i32, set: i32, name: *const u16) -> *mut c_void;
    fn OpenEventW(access: u32, inherit: i32, name: *const u16) -> *mut c_void;
    fn SetEvent(event: *mut c_void) -> i32;
    fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
}

#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: *mut c_void, access: u32, token: *mut *mut c_void) -> i32;
    fn GetTokenInformation(token: *mut c_void, class: u32, information: *mut c_void, length: u32, needed: *mut u32) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(text: *const u16, revision: u32, descriptor: *mut *mut c_void, size: *mut u32) -> i32;
}

#[link(name = "shell32")]
extern "system" {
    fn ShellExecuteW(owner: *mut c_void, verb: *const u16, file: *const u16, parameters: *const u16, folder: *const u16, show: i32) -> isize;
}

#[link(name = "user32")]
extern "system" {
    fn MessageBoxW(owner: isize, text: *const u16, title: *const u16, kind: u32) -> i32;
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// The names the copies of one build know each other by.
#[derive(Debug, Clone, PartialEq)]
pub struct Names {
    pub one_copy: String,
    pub show_window: String,
}

impl Names {
    /// The app's own.  The first is the name the Python app has always held
    /// while it runs, so that an old copy and a new one see each other.
    pub fn of_the_app() -> Names {
        Names {
            one_copy: r"Local\Vispora-SingleInstance-{8B7A1F23}".to_string(),
            show_window: r"Local\Vispora-ShowWindow-{8B7A1F23}".to_string(),
        }
    }

    /// For a trial: names of its own, so that a trial never takes itself
    /// for a second copy of the installed app, or the other way round.
    pub fn for_a_trial(tag: &str) -> Names {
        let tag: String = tag.chars().filter(|c| c.is_ascii_alphanumeric()).take(40).collect();
        Names { one_copy: format!(r"Local\Vispora-Trial-{tag}-OneCopy"), show_window: format!(r"Local\Vispora-Trial-{tag}-ShowWindow") }
    }
}

/// Windows, for instance.rs.
pub struct ThisSession {
    names: Names,
}

pub fn session(names: Names) -> ThisSession {
    ThisSession { names }
}

/// What keeps this copy the one copy: the two names, held until the
/// process ends.
struct TheOne {
    _one_copy: isize,
    show_window: isize,
}

impl crate::instance::Held for TheOne {
    fn when_asked_to_show(&self, show: Box<dyn Fn() + Send>) {
        let event = self.show_window;
        if event == 0 {
            return;
        }
        let _ = std::thread::Builder::new().name("shell-show-window".into()).spawn(move || {
            // SAFETY: the event is held by TheOne, which lives as long as the process.
            while unsafe { WaitForSingleObject(event as *mut c_void, INFINITE) } == 0 {
                show();
            }
        });
    }
}

/// One argument as it has to be written on a command line so that the
/// program started with it reads it back as it was.
pub fn quoted(argument: &str) -> String {
    if !argument.is_empty() && !argument.contains([' ', '\t', '"']) {
        return argument.to_string();
    }
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in argument.chars() {
        match c {
            '\\' => slashes += 1,
            '"' => {
                out.extend(std::iter::repeat('\\').take(slashes * 2 + 1));
                out.push('"');
                slashes = 0;
            }
            other => {
                out.extend(std::iter::repeat('\\').take(slashes));
                slashes = 0;
                out.push(other);
            }
        }
    }
    out.extend(std::iter::repeat('\\').take(slashes * 2));
    out.push('"');
    out
}

impl crate::instance::Session for ThisSession {
    fn ask_other_to_show(&self) -> bool {
        let name = wide(&self.names.show_window);
        // SAFETY: a name that ends in a NUL; the handle is closed again.
        unsafe {
            let event = OpenEventW(EVENT_MODIFY_STATE, 0, name.as_ptr());
            if event.is_null() {
                return false;
            }
            let rung = SetEvent(event) != 0;
            CloseHandle(event);
            rung
        }
    }

    fn another_is_running(&self) -> bool {
        let name = wide(&self.names.one_copy);
        // SAFETY: as above.
        unsafe {
            let held = OpenMutexW(SYNCHRONIZE, 0, name.as_ptr());
            if !held.is_null() {
                CloseHandle(held);
                return true;
            }
            // (it is there, and held by a program with more rights than this one)
            GetLastError() == ERROR_ACCESS_DENIED
        }
    }

    fn has_administrator_rights(&self) -> bool {
        let (mut token, mut elevated, mut length) = (std::ptr::null_mut(), 0u32, 0u32);
        // SAFETY: this process's own token, read into four bytes, and closed.
        unsafe {
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return false;
            }
            let read = GetTokenInformation(token, TOKEN_ELEVATION, &mut elevated as *mut u32 as *mut c_void, 4, &mut length);
            CloseHandle(token);
            read != 0 && elevated != 0
        }
    }

    fn start_with_administrator_rights(&self, arguments: &[String]) -> Result<(), String> {
        let program = std::env::current_exe().map_err(|e| e.to_string())?;
        let parameters = arguments.iter().map(|a| quoted(a)).collect::<Vec<_>>().join(" ");
        let (verb, program, parameters) = (wide("runas"), wide(&program.to_string_lossy()), wide(&parameters));
        // SAFETY: three strings that end in a NUL and outlive the call.
        let started = unsafe { ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), program.as_ptr(), parameters.as_ptr(), std::ptr::null(), SW_SHOWNORMAL) };
        if started > 32 {
            return Ok(());
        }
        // SAFETY: reads the calling thread's last error.
        Err(match unsafe { GetLastError() } {
            ERROR_CANCELLED => "Windows asked whether Vispora may make changes to this PC, and the answer was no".to_string(),
            code => format!("Windows error {code}"),
        })
    }

    fn become_the_one(&self) -> Result<Option<Box<dyn crate::instance::Held>>, String> {
        let (one_copy, show_window, who) = (wide(&self.names.one_copy), wide(&self.names.show_window), wide(WHO_MAY_RING));
        // SAFETY: names that end in a NUL; a descriptor Windows allocates
        // and that is freed here; handles that are kept or closed.
        unsafe {
            let held = CreateMutexW(std::ptr::null(), 0, one_copy.as_ptr());
            let error = GetLastError();
            if held.is_null() {
                return if error == ERROR_ACCESS_DENIED { Ok(None) } else { Err(format!("Windows error {error}")) };
            }
            if error == ERROR_ALREADY_EXISTS {
                CloseHandle(held);
                return Ok(None);
            }
            let mut descriptor = std::ptr::null_mut();
            let described = ConvertStringSecurityDescriptorToSecurityDescriptorW(who.as_ptr(), 1, &mut descriptor, std::ptr::null_mut()) != 0;
            let attributes = SecurityAttributes { length: std::mem::size_of::<SecurityAttributes>() as u32, descriptor, inherit: 0 };
            let event = CreateEventW(if described { &attributes } else { std::ptr::null() }, 0, 0, show_window.as_ptr());
            if described {
                LocalFree(descriptor);
            }
            // (without the event this copy cannot be asked for its window;
            // it is still the one copy)
            Ok(Some(Box::new(TheOne { _one_copy: held as isize, show_window: event as isize })))
        }
    }
}

/// Start another program, or open a link or a folder, the way Explorer
/// would, and not as a child of this program.
///
/// The shell's listening socket is inheritable on purpose: the backend
/// inherits it.  Nothing else the shell ever starts is to get it, so nothing
/// else is started the way the backend is.  What is started here inherits
/// no handle of this program's.
pub fn start_other(target: &str, parameters: &str) -> Result<(), String> {
    let (verb, target_w, parameters_w) = (wide("open"), wide(target), wide(parameters));
    let parameters_ptr = if parameters.is_empty() { std::ptr::null() } else { parameters_w.as_ptr() };
    // SAFETY: strings that end in a NUL and outlive the call.
    let started = unsafe { ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), target_w.as_ptr(), parameters_ptr, std::ptr::null(), SW_SHOWNORMAL) };
    if started > 32 {
        Ok(())
    } else {
        // SAFETY: reads the calling thread's last error.
        Err(format!("{target}: Windows error {}", unsafe { GetLastError() }))
    }
}

/// Show `text` in a plain Windows message box and wait for its OK: for
/// what has to be said when there is no window to say it in.
pub fn tell(title: &str, text: &str) {
    const WARNING: u32 = 0x30;
    const IN_FRONT: u32 = 0x1_0000;
    let (text, title) = (wide(text), wide(title));
    // SAFETY: both strings end in a NUL and outlive the call.
    unsafe { MessageBoxW(0, text.as_ptr(), title.as_ptr(), WARNING | IN_FRONT) };
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::instance::Session as _;
    use std::net::{TcpListener, TcpStream};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    #[test]
    fn the_one_copy_is_one_and_can_be_asked_for_its_window() {
        // Names of this test's own: nothing of the installed app is touched.
        let names = Names::for_a_trial(&format!("test{}", std::process::id()));
        assert!(names.one_copy.starts_with(r"Local\Vispora-Trial-test") && names != Names::of_the_app());
        let first = session(names.clone());
        assert!(!first.ask_other_to_show() && !first.another_is_running(), "nobody is there yet");
        let held = first.become_the_one().unwrap().expect("the first start is the one copy");
        let asked = Arc::new(AtomicUsize::new(0));
        let count = asked.clone();
        held.when_asked_to_show(Box::new(move || {
            count.fetch_add(1, Ordering::SeqCst);
        }));
        // A second start: there is a copy, it cannot take the name, and it
        // can ask the first for its window.
        let second = session(names);
        assert!(second.another_is_running());
        assert!(second.become_the_one().unwrap().is_none());
        assert!(second.ask_other_to_show());
        let ends = std::time::Instant::now() + Duration::from_secs(5);
        while asked.load(Ordering::SeqCst) == 0 && std::time::Instant::now() < ends {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(asked.load(Ordering::SeqCst), 1, "the first copy was not told");
        assert!(second.ask_other_to_show());
        while asked.load(Ordering::SeqCst) == 1 && std::time::Instant::now() < ends {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(asked.load(Ordering::SeqCst), 2);
        // (whether this test runs with administrator rights is not its business: only that the question has an answer)
        let _ = second.has_administrator_rights();
    }

    #[test]
    fn the_apps_own_names_are_the_ones_the_python_app_holds() {
        let names = Names::of_the_app();
        assert_eq!(names.one_copy, r"Local\Vispora-SingleInstance-{8B7A1F23}");
        assert!(names.show_window.starts_with(r"Local\Vispora-") && names.show_window != names.one_copy);
        // A trial's tag cannot smuggle another name in.
        assert_eq!(Names::for_a_trial(r"a\b {c}!").one_copy, r"Local\Vispora-Trial-abc-OneCopy");
    }

    #[test]
    fn an_argument_is_written_so_that_it_is_read_back_as_it_was() {
        for (argument, written) in [
            ("--from-autostart", "--from-autostart"),
            ("", "\"\""),
            ("a b", "\"a b\""),
            (r"C:\Program Files\Vispora\", "\"C:\\Program Files\\Vispora\\\\\""),
            (r#"say "hi""#, r#""say \"hi\"""#),
            (r#"x\"y"#, r#""x\\\"y""#),
            (r"C:\plain\path", r"C:\plain\path"),
        ] {
            assert_eq!(quoted(argument), written, "{argument}");
        }
    }

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
        // (A moment may pass first.  The socket is inherited by whatever
        // program this process starts while it exists, which in the shell
        // is the backend alone and in a test run can be another test's
        // cmd.exe; the port is free when the last of them has ended.)
        let ends = std::time::Instant::now() + Duration::from_secs(20);
        while can_be_joined(port) != (true, true) && std::time::Instant::now() < ends {
            std::thread::sleep(Duration::from_millis(20));
        }
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
