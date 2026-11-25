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
    fn BCryptHash(algorithm: *mut c_void, secret: *mut u8, secret_length: u32, input: *const u8, input_length: u32, output: *mut u8, output_length: u32) -> i32;
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
const MAX_PATH: usize = 260;

/// Who may do what with the "show your window" event: anybody signed in at
/// this PC's screen may ring it and wait on it (a second start has no
/// administrator rights when it rings), administrators and Windows itself
/// everything.  The last part says that a program without administrator
/// rights may ring it although one with them made it.
const WHO_MAY_RING: &str = "D:(A;;0x00100002;;;IU)(A;;GA;;;BA)(A;;GA;;;SY)S:(ML;;NW;;;ME)";

/// What Windows is told about a program it is to start (nothing, here)
/// and what it says back.
#[repr(C)]
struct StartupInfo {
    size: u32,
    reserved: *mut u16,
    desktop: *mut u16,
    title: *mut u16,
    position_and_size: [u32; 7],
    flags: u32,
    show: u16,
    reserved_size: u16,
    reserved_bytes: *mut u8,
    standard: [*mut c_void; 3],
}

#[repr(C)]
struct ProcessInformation {
    process: *mut c_void,
    thread: *mut c_void,
    pid: u32,
    thread_id: u32,
}

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
    fn CreateDirectoryW(path: *const u16, attributes: *const SecurityAttributes) -> i32;
    fn MoveFileExW(from: *const u16, to: *const u16, flags: u32) -> i32;
    fn LoadLibraryExW(name: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn GetSystemWindowsDirectoryW(path: *mut u16, length: u32) -> u32;
    #[allow(clippy::too_many_arguments)]
    fn CreateProcessW(program: *const u16, line: *mut u16, process: *const c_void, thread: *const c_void, inherit: i32, flags: u32,
                      environment: *const c_void, folder: *const u16, startup: *const StartupInfo, started: *mut ProcessInformation) -> i32;
}

#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: *mut c_void, access: u32, token: *mut *mut c_void) -> i32;
    fn GetTokenInformation(token: *mut c_void, class: u32, information: *mut c_void, length: u32, needed: *mut u32) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(text: *const u16, revision: u32, descriptor: *mut *mut c_void, size: *mut u32) -> i32;
    fn ConvertSidToStringSidW(account: *mut c_void, text: *mut *mut u16) -> i32;
    #[allow(clippy::too_many_arguments)]
    fn GetNamedSecurityInfoW(name: *const u16, kind: u32, what: u32, owner: *mut *mut c_void, group: *mut *mut c_void,
                             list: *mut *mut c_void, audit: *mut *mut c_void, descriptor: *mut *mut c_void) -> u32;
    #[allow(clippy::too_many_arguments)]
    fn GetSecurityInfo(handle: *mut c_void, kind: u32, what: u32, owner: *mut *mut c_void, group: *mut *mut c_void,
                       list: *mut *mut c_void, audit: *mut *mut c_void, descriptor: *mut *mut c_void) -> u32;
    fn ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor: *mut c_void, revision: u32, what: u32, text: *mut *mut u16, length: *mut u32) -> i32;
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

/// Start a program that is not the backend, with none of this program's
/// handles.
///
/// The shell's listening socket is inheritable on purpose: the backend
/// inherits it.  Rust starts every program with inheritance on, so nothing
/// but the backend is started Rust's way.  What is started here is started
/// by Windows' own call with inheritance off.
pub fn start_without_handles(program: &str, arguments: &[String]) -> Result<u32, String> {
    let line = std::iter::once(quoted(program)).chain(arguments.iter().map(|a| quoted(a))).collect::<Vec<_>>().join(" ");
    start_line_without_handles(program, &line)
}

fn start_line_without_handles(program: &str, line: &str) -> Result<u32, String> {
    let (program_w, mut line_w) = (wide(program), wide(line));
    // SAFETY: strings that end in a NUL; two structures of the sizes Windows
    // expects, zeroed but for the first one's own size; handles closed again.
    unsafe {
        let mut startup: StartupInfo = std::mem::zeroed();
        startup.size = std::mem::size_of::<StartupInfo>() as u32;
        let mut started: ProcessInformation = std::mem::zeroed();
        let inherit_handles = 0;
        if CreateProcessW(program_w.as_ptr(), line_w.as_mut_ptr(), std::ptr::null(), std::ptr::null(), inherit_handles, 0,
                          std::ptr::null(), std::ptr::null(), &startup, &mut started) == 0 {
            return Err(format!("{program}: Windows error {}", GetLastError()));
        }
        CloseHandle(started.thread);
        CloseHandle(started.process);
        Ok(started.pid)
    }
}

/// Open a link in the person's browser, by way of Explorer.
///
/// The shell runs with administrator rights.  Asked to open a link itself,
/// Windows would start the browser as a child of the shell, with those
/// rights, and would look up which program opens links in the person's own
/// part of the registry, which any program may change.  So the link is
/// handed to Explorer (by its full path in Windows' folder, not looked up),
/// and Explorer, which is the desktop the person is signed in to, opens it
/// as it opens anything that is clicked there.
///
/// Only a web address is taken, and only one that is a single plain word.
pub fn open_link(link: &str) -> Result<(), String> {
    let explorer = format!("{}\\explorer.exe", windows_folder()?);
    let line = line_for_a_link(&explorer, link).ok_or("not a web address")?;
    start_line_without_handles(&explorer, &line).map(|_| ())
}

/// What Explorer is started with for `link`, or None for anything that is
/// not a plain web address: no blank, no quotation mark, no backslash, no
/// letter outside ASCII (the window hands links over written that way).
/// The link is always put in quotation marks: Explorer reads commas and
/// equals signs outside them as its own.
fn line_for_a_link(explorer: &str, link: &str) -> Option<String> {
    let web = link.starts_with("https://") || link.starts_with("http://");
    let plain = link.len() <= 2000 && link.chars().all(|c| c.is_ascii_graphic() && c != '"' && c != '\\');
    (web && plain).then(|| format!("{} \"{link}\"", quoted(explorer)))
}

/// Windows' own folder (`C:\Windows`), asked of Windows by the one call
/// that does not look at the environment, which the person's programs can
/// change.
pub fn windows_folder() -> Result<String, String> {
    let mut path = [0u16; MAX_PATH];
    // SAFETY: a buffer and its length.
    let length = unsafe { GetSystemWindowsDirectoryW(path.as_mut_ptr(), MAX_PATH as u32) } as usize;
    if length == 0 || length >= MAX_PATH {
        return Err("Windows did not say where it is installed".to_string());
    }
    Ok(String::from_utf16_lossy(&path[..length]))
}

/// Where the app, which runs with administrator rights, keeps what is its
/// own for one run: Windows' folder for temporary files (`C:\Windows\Temp`).
/// People without administrator rights can make files and folders in it,
/// but can neither look into it nor rename or remove what somebody else
/// made there.
///
/// Not the folder for all users' data (`C:\ProgramData`), although it would
/// do as well: where that is, Windows works out from the SystemDrive
/// variable of the program that asks.  Tried: with the variable pointing at
/// a folder that has a ProgramData of its own, both of Windows' calls for it
/// answer with that folder.  A variable is the person's programs' to set,
/// and a folder of theirs is theirs to rename what is in it.
pub fn administrators_folder() -> Result<String, String> {
    let folder = format!("{}\\Temp", windows_folder()?);
    if !std::path::Path::new(&folder).is_dir() {
        return Err(format!("{folder} is missing"));
    }
    Ok(folder)
}

/// Where the app, which runs with administrator rights, keeps what is to
/// stay on the PC: the folder for all users' data (`C:\ProgramData`).
/// People without administrator rights can make folders in it, and can
/// neither rename nor remove what somebody else made there.  None where it
/// is not where it is on every ordinary PC.
///
/// Windows' calls for this folder follow the SystemDrive variable of the
/// program that asks (see administrators_folder), so it is not asked for:
/// it is taken to be on the drive Windows is on, which is where it is
/// unless somebody moved it when Windows was set up, and it has to be a
/// folder that is really there, and no link.
pub fn all_users_folder() -> Option<String> {
    use std::os::windows::fs::MetadataExt as _;
    let windows = windows_folder().ok()?;
    let drive = windows.get(..3).filter(|drive| drive.ends_with(":\\"))?;
    let folder = format!("{drive}ProgramData");
    let found = std::fs::symlink_metadata(&folder).ok()?;
    (found.is_dir() && found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0).then_some(folder)
}

// ═════════════════════════════════════════════════════════════════════
// The window's own folder, one per run
// ═════════════════════════════════════════════════════════════════════
// WebView2 keeps a profile folder.  A fixed one in the person's AppData can
// be read by any of their programs (and the page keeps the run's secret in
// its session storage), and written by them before the next start (and what
// is planted there runs inside the app's page).  So the window is private
// (nothing of a run is written down for a later one), and its folder is new
// at every start, under a name nobody can guess, made so that only
// administrators can read or write it, and removed when the run is over.
//
// A run that is ended from outside cannot remove its folder, so a start
// removes what earlier runs left behind.  It has to tell those from a
// folder that is in use: "is a copy running" is asked per session of
// Windows, so two people signed in at one PC each run a copy, and each
// copy's start looks at the same place.  A run therefore marks its folder
// (see IN_USE_MARK), and a folder whose mark is held is never touched.

/// How the folders begin, so that what a run left behind is recognised.
pub const WINDOW_FOLDER_PREFIX: &str = "Vispora-window-";
/// The folder a run unpacks its backend into, where it is unpacked for one
/// run (backend.rs).  Made, marked and taken away like the window's.
pub const BACKEND_FOLDER_PREFIX: &str = "Vispora-backend-run-";
/// Every kind of folder that is one run's own.
const ONE_RUNS_FOLDERS: [&str; 2] = [WINDOW_FOLDER_PREFIX, BACKEND_FOLDER_PREFIX];
/// The file in a window's folder that its run keeps open, for as long as
/// it runs, in a way that lets nobody else open it.  A run that has ended,
/// however it ended, holds it no more.
pub const IN_USE_MARK: &str = "in-use";
/// A folder without the mark is being made at this very moment (the folder
/// comes first, its mark a moment later), or was left by a run that ended
/// in that moment.  A start leaves it alone until it is this old.
pub const MARK_EXPECTED_WITHIN: std::time::Duration = std::time::Duration::from_secs(600);

/// The window's folder of one run, and what says that it is in use.
#[derive(Debug)]
pub struct OwnFolder {
    pub path: String,
    in_use: Option<std::fs::File>,
}

impl OwnFolder {
    /// Take the folder away: the run is over.
    pub fn remove(mut self) -> bool {
        self.in_use.take();
        remove_private_folder(&self.path)
    }
}

/// What a start found among the folders of one run (remove_private_folders).
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Tidied {
    /// How many entries of the folder above were looked at, by name alone.
    pub entries: usize,
    /// Folders that earlier runs left behind, and that are gone now.
    pub removed: usize,
    /// Folders of this program's making that were left alone: in use by a
    /// copy in another session, too new to tell, or with a program in them
    /// that is still running.
    pub left: usize,
    /// Folders with the name that are not of this program's making.
    /// Nothing in them was opened, and they were not touched.
    pub not_ours: usize,
}
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
const SECURITY_IDENTIFICATION: u32 = 0x0001_0000;
const TOKEN_USER: u32 = 1;

/// Who may get into the window's folder.  Either way: everything, also in
/// what is made inside, for the ones named and for Windows itself; nothing
/// for anybody else; and nothing taken over from the folder above.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum OpenTo {
    /// The app as it ships, which runs with administrator rights: programs
    /// the person runs without those rights are kept out.
    Administrators,
    /// A trial, which has no such rights: the account it runs under.  That
    /// keeps out what the window's own helper programs run as, as the other
    /// does, so a trial shows whether WebView2 works in a closed folder.
    ThisAccount,
}

/// The account this program runs under, the way Windows writes it (S-1-5-...).
fn own_account() -> Result<String, String> {
    // (pointer-aligned: Windows writes a pointer and the account behind it)
    let (mut token, mut found, mut length) = (std::ptr::null_mut(), [0u64; 32], 0u32);
    // SAFETY: this process's own token, read into a buffer of the size
    // given; the text Windows allocates is copied and freed.
    unsafe {
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err(format!("Windows error {}", GetLastError()));
        }
        let read = GetTokenInformation(token, TOKEN_USER, found.as_mut_ptr() as *mut c_void, std::mem::size_of_val(&found) as u32, &mut length);
        let error = GetLastError();
        CloseHandle(token);
        if read == 0 {
            return Err(format!("Windows error {error}"));
        }
        let account = *(found.as_ptr() as *const *mut c_void);
        let mut text: *mut u16 = std::ptr::null_mut();
        if ConvertSidToStringSidW(account, &mut text) == 0 {
            return Err(format!("Windows error {}", GetLastError()));
        }
        let length = (0..).take_while(|at| *text.add(*at) != 0).count();
        let written = String::from_utf16_lossy(std::slice::from_raw_parts(text, length));
        LocalFree(text as *mut c_void);
        Ok(written)
    }
}

/// Who that is, the way Windows writes it in an access list.
fn written(open_to: OpenTo) -> Result<String, String> {
    match open_to {
        OpenTo::Administrators => Ok("BA".to_string()),
        OpenTo::ThisAccount => own_account().map_err(|why| format!("the account this runs under could not be read ({why})")),
    }
}

/// Make a new folder for the window in `parent`, marked as in use.  It did
/// not exist before: a folder that is already there is never taken.
pub fn private_folder(parent: &str, open_to: OpenTo) -> Result<OwnFolder, String> {
    private_folder_named(parent, WINDOW_FOLDER_PREFIX, open_to)
}

/// The same for any of one run's folders: its name begins with `prefix`.
pub fn private_folder_named(parent: &str, prefix: &str, open_to: OpenTo) -> Result<OwnFolder, String> {
    std::fs::create_dir_all(parent).map_err(|e| format!("{parent}: {e}"))?;
    let who = written(open_to)?;
    let mut why = String::new();
    // More than once only if the folder was taken away between being made
    // and being marked.  A start in another session does not do that (see
    // left_behind); if anything ever does, the folder was still empty, and
    // another one is made.
    for _ in 0..3 {
        let mut random = [0u8; 16];
        Windows.random(&mut random)?;
        let name: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = format!("{parent}\\{prefix}{name}");
        // Theirs, too: whoever owns a folder may change who gets into it.
        // So the owner is said here, and not left to what Windows would
        // pick.  (A program without administrator rights cannot make a
        // folder that is the administrators' own: Windows refuses.)
        closed_folder(&path, &format!("O:{who}{}", open_only_to(&who)))?;
        match hold_mark(&path) {
            Ok(in_use) => return Ok(OwnFolder { path, in_use: Some(in_use) }),
            Err(e) => why = format!("{path}: it could not be marked as in use ({e})"),
        }
    }
    Err(why)
}

/// Make the mark in a folder that was just made, and keep it open so that
/// nobody else can open it.  (Not for the programs this one starts, either:
/// Rust opens files so that they are not passed on.)
fn hold_mark(folder: &str) -> std::io::Result<std::fs::File> {
    use std::os::windows::fs::OpenOptionsExt as _;
    let nobody_else = 0;
    std::fs::OpenOptions::new().read(true).write(true).create_new(true).share_mode(nobody_else).open(format!("{folder}\\{IN_USE_MARK}"))
}

/// Who gets into a folder that is open to `who` alone, the way Windows
/// writes such a list: `who` and Windows itself, everything, also in what
/// is made inside; nobody else; nothing taken over from the folder above.
fn open_only_to(who: &str) -> String {
    format!("D:P(A;OICI;FA;;;{who})(A;OICI;FA;;;SY)")
}

/// Make the folder `path`, which must not be there yet, with `described` as
/// what Windows keeps about whose it is and who gets in.
fn closed_folder(path: &str, described: &str) -> Result<(), String> {
    let (path_w, described) = (wide(path), wide(described));
    // SAFETY: strings that end in a NUL; a descriptor Windows allocates and
    // that is freed here.
    unsafe {
        let mut descriptor = std::ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(described.as_ptr(), 1, &mut descriptor, std::ptr::null_mut()) == 0 {
            return Err(format!("the folder's access list could not be made (Windows error {})", GetLastError()));
        }
        let attributes = SecurityAttributes { length: std::mem::size_of::<SecurityAttributes>() as u32, descriptor, inherit: 0 };
        let made = CreateDirectoryW(path_w.as_ptr(), &attributes);
        let error = GetLastError();
        LocalFree(descriptor);
        if made == 0 {
            return Err(format!("{path}: Windows error {error}"));
        }
    }
    Ok(())
}

/// Make the folder `path`, which must not be there yet: owned by, and
/// open to, `open_to` and Windows itself, and nobody else.
pub fn make_closed_folder(path: &str, open_to: OpenTo) -> Result<(), String> {
    let who = written(open_to)?;
    closed_folder(path, &format!("O:{who}{}", open_only_to(&who)))
}

const SE_FILE_OBJECT: u32 = 1;
const OWNER_AND_ACCESS_LIST: u32 = 1 | 4;

/// Whose `path` is and who gets into it, the way Windows writes it down.
pub fn described(path: &str) -> Result<String, String> {
    let path_w = wide(path);
    // SAFETY: a string that ends in a NUL; a descriptor Windows allocates,
    // which written_down frees.
    unsafe {
        let mut descriptor = std::ptr::null_mut();
        let status = GetNamedSecurityInfoW(path_w.as_ptr(), SE_FILE_OBJECT, OWNER_AND_ACCESS_LIST, std::ptr::null_mut(), std::ptr::null_mut(),
                                           std::ptr::null_mut(), std::ptr::null_mut(), &mut descriptor);
        if status != 0 {
            return Err(format!("{path}: Windows error {status}"));
        }
        written_down(descriptor, path)
    }
}

/// The same for what was opened as `held`: asked of the open folder itself,
/// not of whatever is under its name by now.
fn described_held(held: &std::fs::File, path: &str) -> Result<String, String> {
    use std::os::windows::io::AsRawHandle as _;
    // SAFETY: a handle that is open for as long as `held` is borrowed; a
    // descriptor Windows allocates, which written_down frees.
    unsafe {
        let mut descriptor = std::ptr::null_mut();
        let status = GetSecurityInfo(held.as_raw_handle() as *mut c_void, SE_FILE_OBJECT, OWNER_AND_ACCESS_LIST, std::ptr::null_mut(),
                                     std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut(), &mut descriptor);
        if status != 0 {
            return Err(format!("{path}: Windows error {status}"));
        }
        written_down(descriptor, path)
    }
}

/// What Windows keeps in `descriptor`, as text.  The descriptor is freed.
///
/// # Safety
/// `descriptor` is one that Windows allocated and that nothing else frees.
unsafe fn written_down(descriptor: *mut c_void, path: &str) -> Result<String, String> {
    let mut text: *mut u16 = std::ptr::null_mut();
    let turned = ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, OWNER_AND_ACCESS_LIST, &mut text, std::ptr::null_mut());
    let error = GetLastError();
    LocalFree(descriptor);
    if turned == 0 {
        return Err(format!("{path}: Windows error {error}"));
    }
    let length = (0..).take_while(|at| *text.add(*at) != 0).count();
    let written = String::from_utf16_lossy(std::slice::from_raw_parts(text, length));
    LocalFree(text as *mut c_void);
    Ok(written)
}

/// Is `is` what Windows keeps about a folder of make_closed_folder's making
/// for `open_to`: theirs, and its access list exactly that one?
fn is_of_this_programs_making(is: &str, open_to: OpenTo, path: &str) -> Result<(), String> {
    let who = written(open_to)?;
    // (Windows adds "AI" where entries were handed down automatically at
    // some time; the entries themselves are what is compared)
    let wanted = format!("O:{who}{}", open_only_to(&who));
    if is == wanted || is == wanted.replacen("D:P(", "D:PAI(", 1) {
        Ok(())
    } else {
        Err(format!("{path} is not this program's folder: it is \"{is}\", and one of this program's making is \"{wanted}\""))
    }
}

/// Is the folder `path` as make_closed_folder makes one: owned by `open_to`,
/// and its access list exactly that one?  Why not, if not.  A folder that
/// is somebody else's, or lets somebody else in, is not one of this
/// program's making, whatever its name.
///
/// This says what is under the name at this moment.  A folder that is to
/// be used afterwards is held instead (hold_own_folder).
pub fn is_closed_to_all_but(path: &str, open_to: OpenTo) -> Result<(), String> {
    use std::os::windows::fs::MetadataExt as _;
    let found = std::fs::symlink_metadata(path).map_err(|e| format!("{path}: {e}"))?;
    if !found.is_dir() || found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(format!("{path} is not a folder of its own (a link, or a file)"));
    }
    is_of_this_programs_making(&described(path)?, open_to, path)
}

const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
const FILE_READ_ATTRIBUTES: u32 = 0x80;
const DELETE: u32 = 0x0001_0000;
const READ_CONTROL: u32 = 0x0002_0000;
const ERROR_SHARING_VIOLATION: i32 = 32;
/// What is under the name itself is opened, a folder too: a link is opened
/// as the link it is, not as what it points to.
const AS_IT_IS: u32 = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT;

/// Is what was opened as `held` a folder of this program's making for
/// `open_to` (and no link, and no file)?  Asked of the open folder.
fn own_folder_behind(held: &std::fs::File, path: &str, open_to: OpenTo) -> Result<(), String> {
    use std::os::windows::fs::MetadataExt as _;
    let found = held.metadata().map_err(|e| format!("{path}: {e}"))?;
    if !found.is_dir() || found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(format!("{path} is not a folder of its own (a link, or a file)"));
    }
    is_of_this_programs_making(&described_held(held, path)?, open_to, path)
}

/// Hold the folder `path` under its name, if it is one of this program's
/// making (as is_closed_to_all_but asks, but asked of the folder that was
/// opened).  For as long as what is given back is kept, the name is that
/// folder's: Windows lets nobody take it away, give it another name, or
/// put something else in its place.  So what is found in it afterwards is
/// found in the folder that was checked, and a program started from it is
/// started from there.
///
/// Two copies of the app (two people signed in) hold the same folder side
/// by side.  What is in the folder can still be read and written by whoever
/// its access list lets in: nobody but its makers.
pub fn hold_own_folder(path: &str, open_to: OpenTo) -> Result<std::fs::File, String> {
    use std::os::windows::fs::OpenOptionsExt as _;
    let read_and_write_but_not_remove = 1 | 2;
    let held = std::fs::OpenOptions::new().read(true).share_mode(read_and_write_but_not_remove).custom_flags(AS_IT_IS)
        .open(path).map_err(|e| format!("{path}: {e}"))?;
    own_folder_behind(&held, path, open_to)?;
    Ok(held)
}

/// What became of asking for a folder in order to take it away.
#[derive(Debug)]
pub enum ToRemove {
    /// One of this program's making that nobody holds.  While this is kept,
    /// nobody can come to hold it either.
    Ours(std::fs::File),
    /// Some program holds the folder open (as a copy of the app does that
    /// runs from it: hold_own_folder).  Which program, Windows does not say.
    InUse,
    /// Not of this program's making (or a link, or a file): never touched.
    NotOurs,
}

/// Open the folder `path` to take it away.
pub fn open_to_remove(path: &str, open_to: OpenTo) -> ToRemove {
    use std::os::windows::fs::OpenOptionsExt as _;
    let anything = 1 | 2 | 4;
    let opened = std::fs::OpenOptions::new().access_mode(DELETE | READ_CONTROL | FILE_READ_ATTRIBUTES).share_mode(anything)
        .custom_flags(AS_IT_IS).open(path);
    match opened {
        Ok(held) if own_folder_behind(&held, path, open_to).is_ok() => ToRemove::Ours(held),
        Ok(_) => ToRemove::NotOurs,
        // Asking to remove what somebody holds against removing is what
        // Windows refuses with this, and with nothing else.
        Err(e) if e.raw_os_error() == Some(ERROR_SHARING_VIOLATION) => ToRemove::InUse,
        Err(_) => ToRemove::NotOurs,
    }
}

/// Give the folder `from` the name `to`.  Never onto something that is
/// there already.
pub fn move_without_replacing(from: &str, to: &str) -> Result<(), String> {
    let (from_w, to_w) = (wide(from), wide(to));
    let replacing_nothing = 0;
    // SAFETY: two strings that end in a NUL.
    if unsafe { MoveFileExW(from_w.as_ptr(), to_w.as_ptr(), replacing_nothing) } == 0 {
        return Err(format!("{to}: Windows error {}", unsafe { GetLastError() }));
    }
    Ok(())
}

// ═════════════════════════════════════════════════════════════════════
// Squeezing, and the mark of what a file holds: Windows' own
// ═════════════════════════════════════════════════════════════════════
// The backend's files are carried squeezed (pack.rs).  Windows squeezes
// and brings back itself (cabinet.dll, part of Windows since Windows 8), so
// no code for it is written here and no crate brought in.  The library is
// taken from Windows' own folder and from nowhere else: by name alone,
// Windows would look beside the exe first, where anybody may have put one.

const LOAD_LIBRARY_SEARCH_SYSTEM32: u32 = 0x800;
const ERROR_INSUFFICIENT_BUFFER: u32 = 122;

type MakeSqueezer = unsafe extern "system" fn(u32, *mut c_void, *mut *mut c_void) -> i32;
type Squeeze = unsafe extern "system" fn(*mut c_void, *const u8, usize, *mut u8, usize, *mut usize) -> i32;
type EndSqueezer = unsafe extern "system" fn(*mut c_void) -> i32;

struct Cabinet {
    make_squeezer: MakeSqueezer,
    squeeze: Squeeze,
    end_squeezer: EndSqueezer,
    make_unsqueezer: MakeSqueezer,
    unsqueeze: Squeeze,
    end_unsqueezer: EndSqueezer,
}

fn cabinet() -> Result<&'static Cabinet, String> {
    static LOADED: std::sync::OnceLock<Result<Cabinet, String>> = std::sync::OnceLock::new();
    LOADED
        .get_or_init(|| {
            let name = wide("cabinet.dll");
            // SAFETY: a string that ends in a NUL; the names asked for are
            // the library's own, with the shapes Windows documents.
            unsafe {
                let library = LoadLibraryExW(name.as_ptr(), std::ptr::null_mut(), LOAD_LIBRARY_SEARCH_SYSTEM32);
                if library.is_null() {
                    return Err(format!("Windows' own cabinet.dll could not be loaded (Windows error {})", GetLastError()));
                }
                let find = |name: &[u8]| -> Result<*mut c_void, String> {
                    let found = GetProcAddress(library, name.as_ptr());
                    if found.is_null() {
                        return Err(format!("cabinet.dll has no {}", String::from_utf8_lossy(&name[..name.len() - 1])));
                    }
                    Ok(found)
                };
                Ok(Cabinet {
                    make_squeezer: std::mem::transmute::<*mut c_void, MakeSqueezer>(find(b"CreateCompressor\0")?),
                    squeeze: std::mem::transmute::<*mut c_void, Squeeze>(find(b"Compress\0")?),
                    end_squeezer: std::mem::transmute::<*mut c_void, EndSqueezer>(find(b"CloseCompressor\0")?),
                    make_unsqueezer: std::mem::transmute::<*mut c_void, MakeSqueezer>(find(b"CreateDecompressor\0")?),
                    unsqueeze: std::mem::transmute::<*mut c_void, Squeeze>(find(b"Decompress\0")?),
                    end_unsqueezer: std::mem::transmute::<*mut c_void, EndSqueezer>(find(b"CloseDecompressor\0")?),
                })
            }
        })
        .as_ref()
        .map_err(String::clone)
}

/// Squeeze `data` the way Windows calls number `way` (pack.rs has the
/// names).  Not for nothing at all: Windows refuses to squeeze nothing.
pub fn squeeze(way: u32, data: &[u8]) -> Result<Vec<u8>, String> {
    let cabinet = cabinet()?;
    let mut out = vec![0u8; data.len() + data.len() / 2 + 4096];
    // SAFETY: buffers and their lengths; a squeezer made and ended here.
    unsafe {
        let mut squeezer = std::ptr::null_mut();
        if (cabinet.make_squeezer)(way, std::ptr::null_mut(), &mut squeezer) == 0 {
            return Err(format!("Windows cannot squeeze that way (way {way}, Windows error {})", GetLastError()));
        }
        let mut length = 0usize;
        let mut done = (cabinet.squeeze)(squeezer, data.as_ptr(), data.len(), out.as_mut_ptr(), out.len(), &mut length);
        if done == 0 && GetLastError() == ERROR_INSUFFICIENT_BUFFER {
            out = vec![0u8; length];
            done = (cabinet.squeeze)(squeezer, data.as_ptr(), data.len(), out.as_mut_ptr(), out.len(), &mut length);
        }
        let error = GetLastError();
        (cabinet.end_squeezer)(squeezer);
        if done == 0 {
            return Err(format!("Windows could not squeeze {} bytes (Windows error {error})", data.len()));
        }
        out.truncate(length);
    }
    Ok(out)
}

/// Bring back what `squeeze` made: exactly `size` bytes, or an error.
pub fn unsqueeze(way: u32, squeezed: &[u8], size: usize) -> Result<Vec<u8>, String> {
    let cabinet = cabinet()?;
    let mut out = vec![0u8; size];
    // SAFETY: buffers and their lengths; an unsqueezer made and ended here.
    unsafe {
        let mut unsqueezer = std::ptr::null_mut();
        if (cabinet.make_unsqueezer)(way, std::ptr::null_mut(), &mut unsqueezer) == 0 {
            return Err(format!("Windows cannot bring that back (way {way}, Windows error {})", GetLastError()));
        }
        let mut length = 0usize;
        let done = (cabinet.unsqueeze)(unsqueezer, squeezed.as_ptr(), squeezed.len(), out.as_mut_ptr(), out.len(), &mut length);
        let error = GetLastError();
        (cabinet.end_unsqueezer)(unsqueezer);
        if done == 0 || length != size {
            return Err(format!("what was squeezed did not come back as {size} bytes (Windows error {error}, {length} bytes)"));
        }
    }
    Ok(out)
}

/// The SHA-256 of `data`, in sixty-four letters.  Windows' own where it has
/// it at hand (it uses what the processor offers for it); this
/// repository's own otherwise, which says the same, more slowly.
pub fn sha256_hex(data: &[u8]) -> String {
    let sha256_of_windows = 0x41 as *mut c_void;
    let mut out = [0u8; 32];
    // (in pieces: the call takes lengths up to four thousand million)
    if data.len() <= u32::MAX as usize {
        // SAFETY: a buffer and its length in, thirty-two bytes out.
        let status = unsafe { BCryptHash(sha256_of_windows, std::ptr::null_mut(), 0, data.as_ptr(), data.len() as u32, out.as_mut_ptr(), 32) };
        if status == 0 {
            return out.iter().map(|byte| format!("{byte:02x}")).collect();
        }
    }
    vispora_updater_core::sha256::hex_of(data)
}

/// Remove the window's folder of a run that is over.  WebView2's own
/// programs let go of their files a moment after the window is gone, so
/// this tries for a few seconds.
fn remove_private_folder(path: &str) -> bool {
    for _ in 0..30 {
        match std::fs::remove_dir_all(path) {
            Ok(()) => return true,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return true,
            Err(_) => std::thread::sleep(std::time::Duration::from_millis(100)),
        }
    }
    false
}

/// Take away the programs at the top of `folder`, which has to be one of
/// this program's own making: that is where a run's own program is (a
/// backend's folder has one, a window's has none).  False if Windows will
/// not let one of them go.
///
/// A program that is running cannot be deleted, whoever started it and
/// whether or not anybody still holds the folder: a backend whose shell
/// was ended by force goes on for a moment, putting the system back.  Its
/// folder is then to be left exactly as it is.  The programs come first
/// because taking a folder away stops at the first thing Windows will not
/// let go, and by then whatever came before it in the folder is gone.
///
/// Only for a folder of this program's making, held by the caller
/// (open_to_remove): what is deleted here is named by its path, and in a
/// folder that is somebody else's the path can be made to lead elsewhere.
pub fn programs_let_go(folder: &std::path::Path) -> bool {
    use std::os::windows::fs::MetadataExt as _;
    let Ok(entries) = std::fs::read_dir(folder) else { return true };
    for entry in entries.flatten() {
        if !entry.file_name().to_string_lossy().to_lowercase().ends_with(".exe") {
            continue;
        }
        let Ok(found) = entry.metadata() else { continue };
        if !found.is_file() || found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            continue;
        }
        match std::fs::remove_file(entry.path()) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return false,
        }
    }
    true
}

/// Remove what earlier runs left behind in `parent` (a run that was ended
/// from outside does not get to clean up).  Only folders of this program's
/// own making (`open_to` says whose those are) that begin as one run's
/// folders begin; never one that is in use; never one whose program is
/// still running.
///
/// A folder with such a name that somebody else made is not touched, and
/// nothing in it is opened, its mark included: the place lets programs
/// without rights make folders of their own, and what is in those, and
/// where their names lead by now, is theirs to decide.  Whose a folder is,
/// is asked of the folder itself once it is open (open_to_remove), and it
/// stays open while it is taken away.
///
/// `parent` can hold thousands of files that are nothing of the app's
/// (Windows' own folder for temporary files does).  So the name is looked
/// at first, and nothing more is asked about an entry that does not begin
/// as one run's folders begin; what is then asked first is what the
/// listing itself already said, at no further cost.
pub fn remove_private_folders(parent: &str, open_to: OpenTo) -> Tidied {
    use std::os::windows::fs::MetadataExt as _;
    let mut tidied = Tidied::default();
    let Ok(entries) = std::fs::read_dir(parent) else { return tidied };
    for entry in entries.flatten() {
        tidied.entries += 1;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !ONE_RUNS_FOLDERS.iter().any(|prefix| name.starts_with(prefix)) {
            continue;
        }
        let Ok(found) = entry.metadata() else { continue };
        if !found.is_dir() || found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            continue;
        }
        let path = entry.path();
        match open_to_remove(&path.to_string_lossy(), open_to) {
            ToRemove::NotOurs => tidied.not_ours += 1,
            ToRemove::InUse => tidied.left += 1,
            ToRemove::Ours(going) => {
                if left_behind(&path, &found) && programs_let_go(&path) && std::fs::remove_dir_all(&path).is_ok() {
                    tidied.removed += 1;
                } else {
                    tidied.left += 1;
                }
                drop(going);
            }
        }
    }
    tidied
}

/// Open what is under the mark's name in `folder`, for this program alone.
///
/// Only a folder of this program's own making is asked for its mark
/// (remove_private_folders).  All the same, a link there is not followed
/// (what is opened is the link itself), and whoever is at the other end of
/// what is opened is not handed this program's rights (a pipe's owner
/// could otherwise act as this program): what is opened here is opened
/// with administrator rights, and is not taken on trust.
fn open_mark(folder: &std::path::Path) -> std::io::Result<std::fs::File> {
    use std::os::windows::fs::OpenOptionsExt as _;
    let nobody_else = 0;
    std::fs::OpenOptions::new()
        .read(true)
        .share_mode(nobody_else)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .security_qos_flags(SECURITY_IDENTIFICATION)
        .open(folder.join(IN_USE_MARK))
}

/// Only a plain file is a mark: not a link, not a folder, not a pipe.
fn is_a_plain_file(opened: &std::fs::File) -> bool {
    use std::os::windows::fs::MetadataExt as _;
    opened.metadata().is_ok_and(|found| found.is_file() && found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0)
}

/// Is the window's folder `path` one that no run is using any more?
fn left_behind(path: &std::path::Path, found: &std::fs::Metadata) -> bool {
    match open_mark(path) {
        // The mark is there and nobody holds it: its run is over.  Anything
        // else under the mark's name is not a mark, and the folder is not
        // this start's to remove.
        Ok(mark) => is_a_plain_file(&mark),
        // No mark: being made right now, or left in that moment.  Old
        // enough, it is the second.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            found.created().ok().and_then(|made| made.elapsed().ok()).is_some_and(|age| age >= MARK_EXPECTED_WITHIN)
        }
        // The mark is held (a copy in another session runs in this folder),
        // or cannot be opened at all: not this start's to remove.
        Err(_) => false,
    }
}

/// Take every setting for WebView2 out of this program's environment, and
/// say which were there.
///
/// WebView2 reads them when it starts: one says where to take the browser's
/// programs from, another what to start the browser with (a way in for a
/// debugger among it), another where its profile is.  A program without
/// administrator rights can set them for the person and wait for the next
/// start of an app that has those rights.  The first thing the shell does.
pub fn clear_webview2_variables() -> Vec<String> {
    let found: Vec<String> = std::env::vars_os()
        .map(|(name, _)| name.to_string_lossy().into_owned())
        .filter(|name| name.to_ascii_uppercase().starts_with("WEBVIEW2_"))
        .collect();
    for name in &found {
        std::env::remove_var(name);
    }
    found
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

    fn a_place(name: &str) -> String {
        let place = std::env::temp_dir().join(format!("vispora-shell-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&place);
        std::fs::create_dir_all(&place).unwrap();
        place.to_string_lossy().into_owned()
    }

    /// Take a test's folder away again, whoever it was closed to.  (The
    /// test made it, so it is the test's own, and its owner may open it.)
    fn tidy(path: &str) {
        let _ = std::process::Command::new("icacls.exe").args([path, "/reset", "/t", "/c", "/q"]).output();
        let _ = std::fs::remove_dir_all(path);
        assert!(!std::path::Path::new(path).exists(), "a test left {path} behind");
    }

    #[test]
    fn the_windows_folder_is_new_at_every_run_and_gone_after_it() {
        let parent = a_place("folders");
        let (first, second) = (private_folder(&parent, OpenTo::ThisAccount).unwrap(), private_folder(&parent, OpenTo::ThisAccount).unwrap());
        assert_ne!(first.path, second.path, "a folder is never used twice");
        for folder in [&first, &second] {
            let name = std::path::Path::new(&folder.path).file_name().unwrap().to_string_lossy().into_owned();
            assert!(name.starts_with(WINDOW_FOLDER_PREFIX) && name.len() == WINDOW_FOLDER_PREFIX.len() + 32, "{name}");
            let inside: Vec<String> = std::fs::read_dir(&folder.path).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
            assert_eq!(inside, [IN_USE_MARK], "it starts with its mark and nothing else");
        }
        // What a run wrote goes with the folder.
        let was = first.path.clone();
        std::fs::create_dir_all(format!("{was}\\EBWebView\\Default")).unwrap();
        std::fs::write(format!("{was}\\EBWebView\\Default\\Session Storage"), b"the secret").unwrap();
        assert!(first.remove() && !std::path::Path::new(&was).exists());
        assert!(remove_private_folder(&was), "one that is gone already is no error");
        drop(second);
        tidy(&parent);
    }

    /// A folder as a run that was ended from outside leaves it: made and
    /// marked like any other, with something in it, and the mark let go of.
    /// Make the file `path` one that Windows passes on to somebody else
    /// when it is opened (a reparse point, as a link is one), of a kind
    /// that no program on the PC answers for.  Unlike a link to a file,
    /// any account may make one of these.
    fn make_it_lead_elsewhere(path: &str) -> bool {
        use std::os::windows::fs::OpenOptionsExt as _;
        use std::os::windows::io::AsRawHandle as _;
        #[link(name = "kernel32")]
        extern "system" {
            #[allow(clippy::too_many_arguments)]
            fn DeviceIoControl(device: *mut c_void, code: u32, input: *const u8, input_length: u32, output: *mut u8, output_length: u32,
                               returned: *mut u32, overlapped: *mut c_void) -> i32;
        }
        const FSCTL_SET_REPARSE_POINT: u32 = 0x0009_00A4;
        // Whose kind it is (nobody's: not one of Microsoft's), how much
        // follows the name, the name of whoever made it, and what follows.
        let mut said = Vec::new();
        said.extend_from_slice(&0x0000_7C57u32.to_le_bytes());
        said.extend_from_slice(&4u16.to_le_bytes());
        said.extend_from_slice(&0u16.to_le_bytes());
        said.extend_from_slice(b"a-test-of-shell.");
        said.extend_from_slice(b"mark");
        let file = std::fs::OpenOptions::new().write(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(path).unwrap();
        let mut returned = 0u32;
        // SAFETY: an open file, a buffer and its length, nothing asked back.
        unsafe {
            DeviceIoControl(file.as_raw_handle() as *mut c_void, FSCTL_SET_REPARSE_POINT, said.as_ptr(), said.len() as u32, std::ptr::null_mut(), 0,
                            &mut returned, std::ptr::null_mut()) != 0
        }
    }

    fn one_left_behind(parent: &str) -> String {
        let folder = private_folder(parent, OpenTo::ThisAccount).unwrap();
        std::fs::create_dir_all(format!("{}\\EBWebView\\Default", folder.path)).unwrap();
        std::fs::write(format!("{}\\EBWebView\\Local State", folder.path), b"{}").unwrap();
        let OwnFolder { path, in_use } = folder;
        drop(in_use);
        path
    }

    fn there(path: &str) -> bool {
        std::path::Path::new(path).exists()
    }

    #[test]
    fn a_start_removes_what_was_left_behind_and_never_a_folder_that_is_in_use() {
        use std::os::windows::fs::{FileTimesExt as _, OpenOptionsExt as _};
        let parent = a_place("tidying");
        // Two people signed in at one PC: the other one's copy is running.
        // Its window's folder is in the same place, with WebView2's files
        // in it, opened the way WebView2 opens most of them: so that they
        // could be deleted under it.
        let theirs = private_folder(&parent, OpenTo::ThisAccount).unwrap();
        std::fs::create_dir_all(format!("{}\\EBWebView\\Default", theirs.path)).unwrap();
        let cookies = format!("{}\\EBWebView\\Default\\Cookies", theirs.path);
        std::fs::write(&cookies, b"in use").unwrap();
        let read_write_delete = 7;
        let _open = std::fs::OpenOptions::new().read(true).share_mode(read_write_delete).open(&cookies).unwrap();
        // What a run of this account left behind.
        let left = one_left_behind(&parent);
        // A folder without a mark: one that is being made at this moment
        // looks like this, and so does one whose run ended in that moment.
        // New, it is left alone; old, it is removed.
        let (being_made, never_marked) = (format!("{parent}\\{WINDOW_FOLDER_PREFIX}being-made"), format!("{parent}\\{WINDOW_FOLDER_PREFIX}never-marked"));
        for folder in [&being_made, &never_marked] {
            make_closed_folder(folder, OpenTo::ThisAccount).unwrap();
            std::fs::write(format!("{folder}\\planted.js"), b"x").unwrap();
        }
        let long_ago = std::time::SystemTime::now() - MARK_EXPECTED_WITHIN - std::time::Duration::from_secs(60);
        let backup_semantics = 0x0200_0000;         // (what opening a folder takes)
        let folder = std::fs::OpenOptions::new().write(true).custom_flags(backup_semantics).open(&never_marked).unwrap();
        folder.set_times(std::fs::FileTimes::new().set_created(long_ago)).unwrap();
        drop(folder);
        // Not the window's, although it is in the same place; and a link
        // that begins as the window's folders begin.
        let (other, elsewhere) = (format!("{parent}\\Something-else"), a_place("elsewhere"));
        std::fs::create_dir(&other).unwrap();
        std::fs::write(format!("{parent}\\{WINDOW_FOLDER_PREFIX}a-file"), b"not a folder").unwrap();
        std::fs::write(format!("{elsewhere}\\precious.txt"), b"keep me").unwrap();
        let link = format!("{parent}\\{WINDOW_FOLDER_PREFIX}link");
        let linked = std::process::Command::new("cmd.exe").args(["/d", "/c", "mklink", "/J", &link, &elsewhere]).output().unwrap();
        assert!(linked.status.success(), "{}", String::from_utf8_lossy(&linked.stderr));

        // What is under the mark's name is not a mark: a folder, a link
        // to a folder, a file that Windows passes on to somebody else
        // when it is opened, and (where this account may make one) a link
        // to a file somewhere else.  None of these folders is this start's
        // to remove, and nothing is opened through a link.
        let odd = |name: &str| {
            let folder = format!("{parent}\\{WINDOW_FOLDER_PREFIX}{name}");
            make_closed_folder(&folder, OpenTo::ThisAccount).unwrap();
            std::fs::write(format!("{folder}\\kept.txt"), b"x").unwrap();
            folder
        };
        let (a_folder, a_link_to_a_folder, a_link_to_a_file) = (odd("mark-is-a-folder"), odd("mark-is-a-link-to-a-folder"), odd("mark-is-a-link-to-a-file"));
        // (the one kind that can be opened as a file and is no plain file,
        // and that any account may make: the rule "only a plain file is a
        // mark" is what keeps this folder)
        let leads_elsewhere = odd("mark-leads-elsewhere");
        std::fs::write(format!("{leads_elsewhere}\\{IN_USE_MARK}"), b"").unwrap();
        assert!(make_it_lead_elsewhere(&format!("{leads_elsewhere}\\{IN_USE_MARK}")), "Windows error {}", unsafe { GetLastError() });
        let opened = open_mark(std::path::Path::new(&leads_elsewhere)).expect("opened as what it is, it can be opened");
        assert!(!is_a_plain_file(&opened), "a file that leads elsewhere was taken for a mark");
        drop(opened);
        std::fs::create_dir(format!("{a_folder}\\{IN_USE_MARK}")).unwrap();
        let joined = std::process::Command::new("cmd.exe").args(["/d", "/c", "mklink", "/J", &format!("{a_link_to_a_folder}\\{IN_USE_MARK}"), &elsewhere]).output().unwrap();
        assert!(joined.status.success());
        // (a link to a file takes a right most accounts do not have; where
        // it cannot be made, the folder simply has no mark and is too new)
        let linked_a_file = std::os::windows::fs::symlink_file(format!("{elsewhere}\\precious.txt"), format!("{a_link_to_a_file}\\{IN_USE_MARK}")).is_ok();
        if linked_a_file {
            let opened = open_mark(std::path::Path::new(&a_link_to_a_file)).expect("the link itself can be opened");
            assert!(!is_a_plain_file(&opened), "a link was taken for a mark");
        }
        assert!(is_a_plain_file(&open_mark(std::path::Path::new(&left)).unwrap()), "a real mark is a plain file");

        // And one with the window's name that somebody else made, with
        // everything about it that makes one of this program's own go: a
        // mark that nobody holds, and old enough.  It is not this start's.
        let somebody_elses = format!("{parent}\\{WINDOW_FOLDER_PREFIX}somebody-elses");
        std::fs::create_dir(&somebody_elses).unwrap();
        std::fs::write(format!("{somebody_elses}\\{IN_USE_MARK}"), b"").unwrap();
        std::fs::write(format!("{somebody_elses}\\theirs.txt"), b"theirs").unwrap();
        let folder = std::fs::OpenOptions::new().write(true).custom_flags(backup_semantics).open(&somebody_elses).unwrap();
        folder.set_times(std::fs::FileTimes::new().set_created(long_ago)).unwrap();
        drop(folder);

        let tidied = remove_private_folders(&parent, OpenTo::ThisAccount);
        assert_eq!(tidied, Tidied { entries: 12, removed: 2, left: 6, not_ours: 1 }, "the two that no run is using, and only those");
        assert!(!there(&left) && !there(&never_marked));
        assert!(there(&format!("{somebody_elses}\\theirs.txt")) && there(&format!("{somebody_elses}\\{IN_USE_MARK}")), "somebody else's folder was touched");
        // (asked for somebody else's folders, this account's are not it)
        assert_eq!(remove_private_folders(&parent, OpenTo::Administrators), Tidied { entries: 10, removed: 0, left: 0, not_ours: 7 });
        for folder in [&a_folder, &a_link_to_a_folder, &a_link_to_a_file, &leads_elsewhere] {
            assert!(there(&format!("{folder}\\kept.txt")), "{folder} was taken away, with something that is no mark under the mark's name");
        }
        assert!(there(&cookies) && there(&format!("{}\\{IN_USE_MARK}", theirs.path)), "a folder that is in use was emptied");
        assert!(there(&format!("{being_made}\\planted.js")), "a folder that may be being made was removed");
        assert!(there(&other) && there(&format!("{elsewhere}\\precious.txt")), "a link was followed and what it points to removed");
        // The other copy quits: its folder goes with it.  The one that was
        // too new to tell stays until it is old enough.
        let was = theirs.path.clone();
        drop(_open);
        assert!(theirs.remove() && !there(&was));
        assert_eq!(remove_private_folders(&parent, OpenTo::ThisAccount), Tidied { entries: 9, removed: 0, left: 5, not_ours: 1 });
        let _ = std::fs::remove_file(format!("{leads_elsewhere}\\{IN_USE_MARK}"));
        let _ = std::fs::remove_dir(format!("{a_link_to_a_folder}\\{IN_USE_MARK}"));
        let _ = std::fs::remove_file(format!("{a_link_to_a_file}\\{IN_USE_MARK}"));
        let _ = std::fs::remove_dir(&link);
        tidy(&parent);
        tidy(&elsewhere);
    }

    #[test]
    fn a_backend_that_outlived_its_shell_keeps_its_folder_whole() {
        use std::os::windows::fs::OpenOptionsExt as _;
        // The shell was ended by force.  Nobody holds the folder's mark any
        // more, and the backend is still running in it for a moment, putting
        // the system back.  The next start must not take the folder apart
        // under it: not the mark, and not what comes before the program
        // when the folder is read (taking a folder away stops at the first
        // thing Windows will not let go, and not before).
        let parent = a_place("outlived");
        let folder = private_folder_named(&parent, BACKEND_FOLDER_PREFIX, OpenTo::ThisAccount).unwrap();
        let OwnFolder { path, in_use } = folder;
        std::fs::create_dir(format!("{path}\\_internal")).unwrap();
        let (first, program, library) = (format!("{path}\\0-comes-first.txt"), format!("{path}\\VisporaBackend.exe"), format!("{path}\\_internal\\python312.dll"));
        for (file, holds) in [(&first, &b"still needed"[..]), (&program, b"MZ"), (&library, b"a library")] {
            std::fs::write(file, holds).unwrap();
        }
        drop(in_use);
        // (as Windows holds the program of a process that is running: it
        // can be read, and not deleted)
        let running = std::fs::OpenOptions::new().read(true).share_mode(1).open(&program).unwrap();
        assert!(!programs_let_go(std::path::Path::new(&path)));
        assert_eq!(remove_private_folders(&parent, OpenTo::ThisAccount), Tidied { entries: 1, removed: 0, left: 1, not_ours: 0 });
        for file in [&first, &program, &library, &format!("{path}\\{IN_USE_MARK}")] {
            assert!(there(file), "{file} was taken away under a backend that is still running");
        }
        // It has ended: the next start takes the folder away.
        drop(running);
        assert_eq!(remove_private_folders(&parent, OpenTo::ThisAccount), Tidied { entries: 1, removed: 1, left: 0, not_ours: 0 });
        assert!(!there(&path));
        // What is no program of the run's is not gone at first: a folder
        // and a link under a program's name are left to the taking away.
        let other = format!("{parent}\\elsewhere");
        std::fs::create_dir_all(format!("{other}\\a-folder.exe")).unwrap();
        std::fs::write(format!("{other}\\not-a-program.txt"), b"x").unwrap();
        assert!(programs_let_go(std::path::Path::new(&other)));
        assert!(there(&format!("{other}\\a-folder.exe")) && there(&format!("{other}\\not-a-program.txt")));
        tidy(&parent);
    }

    #[test]
    fn the_mark_is_held_so_that_nobody_else_can_open_it() {
        use std::os::windows::fs::OpenOptionsExt as _;
        let parent = a_place("marked");
        let folder = private_folder(&parent, OpenTo::ThisAccount).unwrap();
        let mark = format!("{}\\{IN_USE_MARK}", folder.path);
        for (reading, writing) in [(true, false), (true, true)] {
            let refused = std::fs::OpenOptions::new().read(reading).write(writing).share_mode(7).open(&mark).unwrap_err();
            assert_eq!(refused.raw_os_error(), Some(32), "the mark could be opened beside the run that holds it");
        }
        assert!(std::fs::remove_file(&mark).is_err(), "the mark could be taken away under the run that holds it");
        // Let go of, it is an ordinary file again.
        let OwnFolder { path, in_use } = folder;
        drop(in_use);
        assert!(std::fs::OpenOptions::new().read(true).share_mode(0).open(&mark).is_ok());
        assert!(remove_private_folder(&path));
        tidy(&parent);
    }

    #[test]
    fn a_program_without_administrator_rights_cannot_get_into_the_windows_folder() {
        let parent = a_place("protected");
        // This test is such a program, unless it is run from an elevated
        // terminal, in which case it has nothing to show.
        if !session(Names::for_a_trial("x")).has_administrator_rights() {
            // It cannot make a folder that is the administrators' own
            // (1307: "may not be assigned as the owner of this object") ...
            let refused = private_folder(&parent, OpenTo::Administrators).unwrap_err();
            assert!(refused.ends_with("Windows error 1307"), "{refused}");
            assert!(std::fs::read_dir(&parent).unwrap().next().is_none(), "one was made all the same");
            // ... and it cannot get into one that lets in whom the app's
            // lets in.  (Made here as this test's own, to have one to try:
            // it is the list of who gets in that keeps the test out.)
            let folder = format!("{parent}\\who-gets-in");
            closed_folder(&folder, &open_only_to("BA")).unwrap();
            let planted = std::fs::write(format!("{folder}\\planted.js"), b"x");
            assert_eq!(planted.unwrap_err().kind(), std::io::ErrorKind::PermissionDenied, "something could be planted in it");
            assert!(std::fs::read_dir(&folder).is_err(), "it could be read");
            assert!(std::fs::create_dir(format!("{folder}\\EBWebView")).is_err());
            tidy(&folder);
            // A folder that is there already is never taken.
            std::fs::create_dir(&folder).unwrap();
            assert!(closed_folder(&folder, &open_only_to("BA")).unwrap_err().ends_with("Windows error 183"));
        }
        // The trial's kind is open to the account it runs under, and says so
        // in what Windows keeps about it.
        let account = own_account().unwrap();
        assert!(account.starts_with("S-1-5-") && account.len() > 12, "{account}");
        let mine = private_folder(&parent, OpenTo::ThisAccount).unwrap().path;
        std::fs::write(format!("{mine}\\profile"), b"x").unwrap();
        // (icacls writes the list down the way Windows writes it, which is
        // the same in every language; the file is UTF-16)
        let saved = format!("{parent}\\who-gets-in.txt");
        let _ = std::process::Command::new("icacls.exe").args([&mine, "/save", &saved, "/q"]).output().unwrap();
        let bytes = std::fs::read(&saved).unwrap();
        let listed = String::from_utf16_lossy(&bytes.chunks_exact(2).map(|pair| u16::from_le_bytes([pair[0], pair[1]])).collect::<Vec<_>>());
        let wanted = format!("D:P(A;OICI;FA;;;{account})(A;OICI;FA;;;SY)");
        assert!(listed.lines().any(|line| line.trim() == wanted), "this account and Windows itself, and nobody else; found: {listed}");
        tidy(&parent);
    }

    #[test]
    fn what_windows_squeezes_comes_back_as_it_was() {
        // Something that squeezes well, something that does not, one byte.
        let words: Vec<u8> = b"the quick brown fox jumps over the lazy dog. ".iter().cycle().take(200_000).copied().collect();
        let mut noise = vec![0u8; 70_000];
        Windows.random(&mut noise).unwrap();
        for way in [2u32, 4, 5] {
            for data in [&words[..], &noise[..], &b"x"[..]] {
                let squeezed = squeeze(way, data).unwrap();
                assert_eq!(unsqueeze(way, &squeezed, data.len()).unwrap(), data, "way {way}, {} bytes", data.len());
            }
            assert!(squeeze(way, &words).unwrap().len() < words.len() / 10, "way {way} did not squeeze");
            // Squeezed bytes that are not what they should be: an error,
            // never other bytes, and never more than was asked for.
            let squeezed = squeeze(way, &words).unwrap();
            assert!(unsqueeze(way, &squeezed, words.len() - 1).is_err(), "way {way}: a wrong length was taken");
            assert!(unsqueeze(way, &squeezed, words.len() + 1).is_err(), "way {way}: less came back than was said, and it was taken");
            assert!(unsqueeze(way, &squeezed[..squeezed.len() / 2], words.len()).is_err(), "way {way}: half of it was taken");
            assert!(unsqueeze(way, b"not squeezed at all", 19).is_err());
        }
        assert!(squeeze(99, &words).is_err() && unsqueeze(99, &words, 1).is_err(), "a way Windows does not have");
    }

    #[test]
    fn windows_and_this_repository_put_the_same_mark_on_the_same_bytes() {
        for data in [&b""[..], b"abc", &vec![7u8; 1_000_000]] {
            assert_eq!(sha256_hex(data), vispora_updater_core::sha256::hex_of(data));
        }
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }

    #[test]
    fn a_folder_of_this_programs_making_is_told_from_any_other() {
        let parent = a_place("whose");
        let (mine, plain) = (format!("{parent}\\mine"), format!("{parent}\\plain"));
        make_closed_folder(&mine, OpenTo::ThisAccount).unwrap();
        std::fs::create_dir(&plain).unwrap();
        let account = own_account().unwrap();
        assert_eq!(described(&mine).unwrap(), format!("O:{account}D:P(A;OICI;FA;;;{account})(A;OICI;FA;;;SY)"));
        assert_eq!(is_closed_to_all_but(&mine, OpenTo::ThisAccount), Ok(()));
        // An ordinary folder lets in whoever the folder above lets in.
        let ordinary = is_closed_to_all_but(&plain, OpenTo::ThisAccount).unwrap_err();
        assert!(ordinary.contains("is not this program's folder"), "{ordinary}");
        // One that is this account's is not the administrators'.
        assert!(is_closed_to_all_but(&mine, OpenTo::Administrators).is_err());
        // One of this program's making that somebody was let into as well:
        // whose it is, is as it should be; who gets in, is not.
        let widened = format!("{parent}\\widened");
        make_closed_folder(&widened, OpenTo::ThisAccount).unwrap();
        let everybody = "*S-1-1-0:(OI)(CI)F";
        assert!(std::process::Command::new("icacls.exe").args([&widened, "/grant", everybody]).output().unwrap().status.success());
        let refused = is_closed_to_all_but(&widened, OpenTo::ThisAccount).unwrap_err();
        assert!(refused.contains("is not this program's folder") && refused.contains(";;;WD)") && refused.contains(&format!("\"O:{account}D:")), "{refused}");
        assert!(hold_own_folder(&widened, OpenTo::ThisAccount).unwrap_err().contains("is not this program's folder"));
        assert!(matches!(open_to_remove(&widened, OpenTo::ThisAccount), ToRemove::NotOurs));
        // A file, a link and nothing at all are no folder of its own.
        std::fs::write(format!("{parent}\\a-file"), b"x").unwrap();
        let link = format!("{parent}\\a-link");
        let linked = std::process::Command::new("cmd.exe").args(["/d", "/c", "mklink", "/J", &link, &mine]).output().unwrap();
        assert!(linked.status.success());
        for not_one in [format!("{parent}\\a-file"), link.clone(), format!("{parent}\\nothing")] {
            assert!(is_closed_to_all_but(&not_one, OpenTo::ThisAccount).is_err(), "{not_one}");
        }
        // A folder is never given the name of something that is there.
        std::fs::write(format!("{mine}\\inside"), b"x").unwrap();
        let moved = format!("{parent}\\moved");
        assert!(move_without_replacing(&mine, &plain).unwrap_err().ends_with("Windows error 183"));
        move_without_replacing(&mine, &moved).unwrap();
        assert!(there(&format!("{moved}\\inside")) && !there(&mine));
        assert_eq!(is_closed_to_all_but(&moved, OpenTo::ThisAccount), Ok(()), "it keeps whose it is under its new name");
        let _ = std::fs::remove_dir(&link);
        tidy(&parent);
    }

    #[test]
    fn a_folder_that_is_held_stays_under_its_name_and_one_that_is_going_cannot_be_held() {
        let parent = a_place("held");
        let (mine, plain, elsewhere) = (format!("{parent}\\mine"), format!("{parent}\\plain"), format!("{parent}\\elsewhere"));
        make_closed_folder(&mine, OpenTo::ThisAccount).unwrap();
        std::fs::create_dir(&plain).unwrap();
        std::fs::write(format!("{mine}\\inside"), b"x").unwrap();

        // Held: nobody takes it away, gives it another name, or puts
        // something else under its name.  What is in it is used as before.
        let held = hold_own_folder(&mine, OpenTo::ThisAccount).unwrap();
        assert!(std::fs::rename(&mine, &elsewhere).is_err(), "it was given another name while it was held");
        assert!(std::fs::remove_dir(&mine).is_err() && there(&mine), "it was taken away while it was held");
        assert!(matches!(open_to_remove(&mine, OpenTo::ThisAccount), ToRemove::InUse));
        assert_eq!(std::fs::read(format!("{mine}\\inside")).unwrap(), b"x");
        std::fs::write(format!("{mine}\\more"), b"y").unwrap();
        std::fs::remove_file(format!("{mine}\\more")).unwrap();
        // A second copy holds it beside the first, and it stays until the
        // last of them has let go.
        let second = hold_own_folder(&mine, OpenTo::ThisAccount).unwrap();
        drop(held);
        assert!(matches!(open_to_remove(&mine, OpenTo::ThisAccount), ToRemove::InUse));
        drop(second);

        // Nobody holds it: it can be asked for to be taken away, and while
        // it is, nobody comes to hold it.
        let ToRemove::Ours(going) = open_to_remove(&mine, OpenTo::ThisAccount) else { panic!("it could not be asked for") };
        let refused = hold_own_folder(&mine, OpenTo::ThisAccount).unwrap_err();
        assert!(refused.contains("os error 32"), "{refused}");
        assert!(std::fs::remove_dir_all(&mine).is_ok());
        drop(going);
        assert!(!there(&mine));

        // What is not this program's making is neither held nor taken away:
        // an ordinary folder, the administrators' kind asked for by somebody
        // else's, a link to a folder that would pass, a file, nothing.
        make_closed_folder(&mine, OpenTo::ThisAccount).unwrap();
        let link = format!("{parent}\\a-link");
        assert!(std::process::Command::new("cmd.exe").args(["/d", "/c", "mklink", "/J", &link, &mine]).output().unwrap().status.success());
        std::fs::write(format!("{parent}\\a-file"), b"x").unwrap();
        for (not_one, why) in [(plain.clone(), "is not this program's folder"), (link.clone(), "is not a folder of its own"),
                               (format!("{parent}\\a-file"), "is not a folder of its own"), (format!("{parent}\\nothing"), "os error 2")] {
            let refused = hold_own_folder(&not_one, OpenTo::ThisAccount).unwrap_err();
            assert!(refused.contains(why), "{not_one}: {refused}");
            assert!(matches!(open_to_remove(&not_one, OpenTo::ThisAccount), ToRemove::NotOurs), "{not_one}");
        }
        assert!(hold_own_folder(&mine, OpenTo::Administrators).is_err());
        assert!(matches!(open_to_remove(&mine, OpenTo::Administrators), ToRemove::NotOurs));
        // (the link was looked at as the link it is: what it points to was
        // neither held nor asked for)
        assert!(matches!(open_to_remove(&mine, OpenTo::ThisAccount), ToRemove::Ours(_)));
        let _ = std::fs::remove_dir(&link);
        tidy(&parent);
    }

    #[test]
    fn webview2s_settings_are_taken_out_of_the_environment() {
        std::env::set_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "C:\\somewhere\\planted");
        std::env::set_var("webview2_additional_browser_arguments", "--remote-debugging-port=9222");
        std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", "C:\\somewhere\\else");
        std::env::set_var("NOT_WEBVIEW2_AT_ALL", "stays");
        let mut cleared = clear_webview2_variables();
        cleared.sort_by_key(|name| name.to_ascii_uppercase());
        assert_eq!(cleared.iter().map(|n| n.to_ascii_uppercase()).collect::<Vec<_>>(),
                   ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_USER_DATA_FOLDER"]);
        assert!(std::env::vars_os().all(|(name, _)| !name.to_string_lossy().to_ascii_uppercase().starts_with("WEBVIEW2_")));
        assert_eq!(std::env::var("NOT_WEBVIEW2_AT_ALL").as_deref(), Ok("stays"));
        assert!(clear_webview2_variables().is_empty());
        std::env::remove_var("NOT_WEBVIEW2_AT_ALL");
    }

    #[test]
    fn only_a_plain_web_address_is_handed_to_explorer() {
        // (nothing is opened here, and nothing could be: only what would be
        // handed to Explorer is looked at, never the handing over itself)
        for not_a_link in ["", "calc.exe", "C:\\Windows\\System32\\calc.exe", "file:///C:/Windows/win.ini", "ms-settings:", "javascript:alert(1)",
                           "https://example.org/a b", "https://example.org/\" & calc.exe", "http://example.org/\r\nx", "vispora://localhost/",
                           "HTTPS://EXAMPLE.ORG/", "https://example.org/x\\", "https://example.org/\u{e9}",
                           // (a quotation mark by itself, with no blank beside it)
                           "https://example.org/\"x", "https://example.org/\",x", "https://example.org/\"", "\"https://example.org/\""] {
            assert_eq!(line_for_a_link("C:\\Windows\\explorer.exe", not_a_link), None, "{not_a_link}");
        }
        // What Explorer is started with: itself, and the link in quotation marks.
        assert_eq!(line_for_a_link("C:\\Windows\\explorer.exe", "https://example.org/a,b?c=d&e=f#g").as_deref(),
                   Some("C:\\Windows\\explorer.exe \"https://example.org/a,b?c=d&e=f#g\""));
        let windows = windows_folder().unwrap();
        assert!(std::path::Path::new(&format!("{windows}\\explorer.exe")).is_file(), "{windows}");
        assert_eq!(administrators_folder().unwrap(), format!("{windows}\\Temp"));
        assert_eq!(all_users_folder(), Some(format!("{}ProgramData", &windows[..3])));
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
