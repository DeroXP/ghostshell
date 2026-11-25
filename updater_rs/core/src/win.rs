//! The real doors to the outside (see platform.rs): Windows' own HTTP client
//! for the network, tasklist and taskkill for the app's processes, the file
//! copy the swap is built on, the clock, and the "pick a folder" dialog.
//!
//! Windows functions are declared by hand, as in native/: few enough that a
//! bindings crate would only add outside code to a program that runs as
//! administrator and replaces an exe.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::io::Read as _;
use std::os::windows::process::CommandExt as _;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::platform::{Body, Clock, Disk, Net, Platform, Processes};

type Handle = *mut c_void;

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// The real platform.
pub fn platform() -> Platform {
    Platform { net: Box::new(WinHttp), procs: Box::new(RealProcesses), disk: Box::new(RealDisk), clock: Box::new(RealClock) }
}

// ═════════════════════════════════════════════════════════════════════
// The network: WinHTTP
// ═════════════════════════════════════════════════════════════════════
// Windows' own client: its TLS, its certificate store, the system's proxy
// settings.  Redirects are followed (a GitHub release file is served from
// another host), but never from https to plain http: that is WinHTTP's
// default and it is left alone.

const WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY: u32 = 4;
const WINHTTP_FLAG_SECURE: u32 = 0x0080_0000;
const WINHTTP_QUERY_STATUS_CODE: u32 = 19;
const WINHTTP_QUERY_CONTENT_LENGTH: u32 = 5;
const WINHTTP_QUERY_FLAG_NUMBER: u32 = 0x2000_0000;
const WINHTTP_QUERY_FLAG_NUMBER64: u32 = 0x0800_0000;
const WINHTTP_OPTION_RECEIVE_RESPONSE_TIMEOUT: u32 = 7;

#[link(name = "winhttp")]
extern "system" {
    fn WinHttpOpen(agent: *const u16, access_type: u32, proxy: *const u16, bypass: *const u16, flags: u32) -> Handle;
    fn WinHttpSetTimeouts(session: Handle, resolve: i32, connect: i32, send: i32, receive: i32) -> i32;
    fn WinHttpSetOption(handle: Handle, option: u32, value: *const c_void, length: u32) -> i32;
    fn WinHttpConnect(session: Handle, server: *const u16, port: u16, reserved: u32) -> Handle;
    fn WinHttpOpenRequest(
        connection: Handle,
        verb: *const u16,
        object: *const u16,
        version: *const u16,
        referrer: *const u16,
        accept_types: *const *const u16,
        flags: u32,
    ) -> Handle;
    fn WinHttpSendRequest(
        request: Handle,
        headers: *const u16,
        headers_length: u32,
        optional: *const c_void,
        optional_length: u32,
        total_length: u32,
        context: usize,
    ) -> i32;
    fn WinHttpReceiveResponse(request: Handle, reserved: *mut c_void) -> i32;
    fn WinHttpQueryHeaders(
        request: Handle,
        info_level: u32,
        name: *const u16,
        buffer: *mut c_void,
        buffer_length: *mut u32,
        index: *mut u32,
    ) -> i32;
    fn WinHttpReadData(request: Handle, buffer: *mut c_void, to_read: u32, read: *mut u32) -> i32;
    fn WinHttpCloseHandle(handle: Handle) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn GetLastError() -> u32;
    fn GetLocalTime(time: *mut SystemTimeParts);
}

fn last_error(what: &str) -> String {
    // SAFETY: reads the calling thread's last error.
    let code = unsafe { GetLastError() };
    let hint = match code {
        12002 => " (timed out)",
        12007 => " (the name could not be resolved)",
        12029 => " (could not connect)",
        12030 => " (the connection was closed)",
        12175 => " (the secure connection failed)",
        _ => "",
    };
    format!("{what}: Windows error {code}{hint}")
}

/// An http or https address, taken apart.
#[derive(Debug, PartialEq)]
struct Url {
    secure: bool,
    host: String,
    port: u16,
    /// The path with its query, "/" at least.
    object: String,
}

fn parse_url(url: &str) -> Result<Url, String> {
    let bad = || format!("not an http(s) address: {url}");
    if url.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(bad());
    }
    let (scheme, rest) = url.split_once("://").ok_or_else(bad)?;
    let secure = match scheme.to_ascii_lowercase().as_str() {
        "https" => true,
        "http" => false,
        _ => return Err(bad()),
    };
    let rest = rest.split('#').next().unwrap_or("");
    let end = rest.find(['/', '?']).unwrap_or(rest.len());
    let (authority, object) = rest.split_at(end);
    if authority.is_empty() || authority.contains('@') {
        return Err(bad()); // (no user names in addresses: "github.com@elsewhere" is elsewhere)
    }
    if authority.starts_with('[') {
        return Err(bad()); // (no IPv6 literals: neither place is reached by one)
    }
    let (host, port) = match authority.split_once(':') {
        Some((host, port)) => (host, port.parse::<u16>().map_err(|_| bad())?),
        None => (authority, if secure { 443 } else { 80 }),
    };
    if host.is_empty() {
        return Err(bad());
    }
    let object = match object {
        "" => "/".to_string(),
        o if o.starts_with('?') => format!("/{o}"),
        o => o.to_string(),
    };
    Ok(Url { secure, host: host.to_string(), port, object })
}

struct WinHttp;

struct WinHttpBody {
    session: Handle,
    connection: Handle,
    request: Handle,
    length: Option<u64>,
    received: u64,
}

// SAFETY: the handles are only ever used by the one thread that owns the
// body; WinHTTP handles are not tied to the thread that made them.
unsafe impl Send for WinHttpBody {}

impl Drop for WinHttpBody {
    fn drop(&mut self) {
        // SAFETY: each handle was opened by this body (or is null) and is closed once.
        unsafe {
            for handle in [self.request, self.connection, self.session] {
                if !handle.is_null() {
                    WinHttpCloseHandle(handle);
                }
            }
        }
    }
}

impl Body for WinHttpBody {
    fn content_length(&self) -> Option<u64> {
        self.length
    }

    fn read(&mut self, buf: &mut [u8]) -> Result<usize, String> {
        let mut read: u32 = 0;
        let want = buf.len().min(u32::MAX as usize) as u32;
        // SAFETY: `buf` is writable for `want` bytes; `read` is a valid out pointer.
        if unsafe { WinHttpReadData(self.request, buf.as_mut_ptr().cast(), want, &mut read) } == 0 {
            return Err(last_error("the download was interrupted"));
        }
        self.received += read as u64;
        if read == 0 {
            // The end.  A connection that closes early ends the same way, so
            // the length the server announced is what tells the two apart.
            if let Some(expected) = self.length.filter(|&n| self.received < n) {
                return Err(format!("the download was interrupted: {} of {expected} bytes arrived", self.received));
            }
        }
        Ok(read as usize)
    }
}

impl Net for WinHttp {
    fn open(&self, url: &str, headers: &[(&str, &str)], timeout_s: u32) -> Result<Box<dyn Body>, String> {
        let target = parse_url(url)?;
        let agent = headers.iter().find(|(name, _)| name.eq_ignore_ascii_case("User-Agent")).map_or("VisporaUpdater", |h| h.1);
        let mut extra = String::new();
        for (name, value) in headers {
            if name.eq_ignore_ascii_case("User-Agent") {
                continue;
            }
            if name.chars().chain(value.chars()).any(|c| c == '\r' || c == '\n') {
                return Err("a header with a line break in it".into());
            }
            extra += &format!("{name}: {value}\r\n");
        }
        let ms = (timeout_s.clamp(1, 600) * 1000) as i32;
        // SAFETY: every string passed is NUL-terminated UTF-16 that outlives
        // the call; every handle is checked before use and owned by `body`,
        // which closes them when it is dropped (also on the early returns).
        unsafe {
            let mut body = WinHttpBody {
                session: std::ptr::null_mut(),
                connection: std::ptr::null_mut(),
                request: std::ptr::null_mut(),
                length: None,
                received: 0,
            };
            body.session = WinHttpOpen(wide(agent).as_ptr(), WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, std::ptr::null(), std::ptr::null(), 0);
            if body.session.is_null() {
                return Err(last_error("the network could not be opened"));
            }
            if WinHttpSetTimeouts(body.session, ms, ms, ms, ms) == 0 {
                return Err(last_error("the time limit could not be set"));
            }
            body.connection = WinHttpConnect(body.session, wide(&target.host).as_ptr(), target.port, 0);
            if body.connection.is_null() {
                return Err(last_error(&format!("could not connect to {}", target.host)));
            }
            body.request = WinHttpOpenRequest(
                body.connection,
                wide("GET").as_ptr(),
                wide(&target.object).as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                if target.secure { WINHTTP_FLAG_SECURE } else { 0 },
            );
            if body.request.is_null() {
                return Err(last_error("the request could not be made"));
            }
            // How long the answer's first line and headers may take is a
            // setting of its own (90 seconds unless told otherwise).
            let wait = ms as u32;
            if WinHttpSetOption(body.request, WINHTTP_OPTION_RECEIVE_RESPONSE_TIMEOUT, (&wait as *const u32).cast(), 4) == 0 {
                return Err(last_error("the time limit could not be set"));
            }
            let extra_wide = wide(&extra);
            let (extra_ptr, extra_len) =
                if extra.is_empty() { (std::ptr::null(), 0) } else { (extra_wide.as_ptr(), (extra_wide.len() - 1) as u32) };
            if WinHttpSendRequest(body.request, extra_ptr, extra_len, std::ptr::null(), 0, 0, 0) == 0
                || WinHttpReceiveResponse(body.request, std::ptr::null_mut()) == 0
            {
                return Err(last_error(&format!("no answer from {}", target.host)));
            }
            let mut status: u32 = 0;
            let mut size = 4u32;
            if WinHttpQueryHeaders(
                body.request,
                WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                std::ptr::null(),
                (&mut status as *mut u32).cast(),
                &mut size,
                std::ptr::null_mut(),
            ) == 0
            {
                return Err(last_error("the answer has no status"));
            }
            if !(200..300).contains(&status) {
                return Err(format!("HTTP Error {status}"));
            }
            let mut length: u64 = 0;
            let mut size = 8u32;
            if WinHttpQueryHeaders(
                body.request,
                WINHTTP_QUERY_CONTENT_LENGTH | WINHTTP_QUERY_FLAG_NUMBER64,
                std::ptr::null(),
                (&mut length as *mut u64).cast(),
                &mut size,
                std::ptr::null_mut(),
            ) != 0
            {
                body.length = Some(length);
            }
            Ok(Box::new(body))
        }
    }
}

// ═════════════════════════════════════════════════════════════════════
// Processes: tasklist, taskkill, and starting the app
// ═════════════════════════════════════════════════════════════════════
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED_PROCESS: u32 = 0x0000_0008;

struct RealProcesses;

impl Processes for RealProcesses {
    fn run(&self, args: &[String], timeout_s: u32) -> Result<String, String> {
        let (program, rest) = args.split_first().ok_or("no command")?;
        let mut child = Command::new(program)
            .args(rest)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| e.to_string())?;
        let mut stdout = child.stdout.take().ok_or("no output")?;
        let reader = std::thread::spawn(move || {
            let mut out = Vec::new();
            let _ = stdout.read_to_end(&mut out);
            out
        });
        let deadline = Instant::now() + Duration::from_secs(timeout_s.max(1) as u64);
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = reader.join();
                    return Err(format!("{program} did not finish within {timeout_s} seconds"));
                }
                Err(e) => return Err(e.to_string()),
            }
        }
        let out = reader.join().map_err(|_| "the output could not be read".to_string())?;
        Ok(String::from_utf8_lossy(&out).into_owned())
    }

    fn spawn(&self, args: &[String]) -> Result<(), String> {
        let (program, rest) = args.split_first().ok_or("no command")?;
        Command::new(program)
            .args(rest)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(DETACHED_PROCESS)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}

// ═════════════════════════════════════════════════════════════════════
// The disk and the clock
// ═════════════════════════════════════════════════════════════════════
struct RealDisk;

impl Disk for RealDisk {
    fn copy(&self, from: &str, to: &str) -> std::io::Result<()> {
        // (Windows' CopyFile: the data with its timestamps, replacing `to`.)
        std::fs::copy(from, to).map(|_| ())
    }
}

#[repr(C)]
#[derive(Default)]
struct SystemTimeParts {
    year: u16,
    month: u16,
    day_of_week: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    milliseconds: u16,
}

struct RealClock;

impl Clock for RealClock {
    fn now(&self) -> f64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
    }

    fn sleep(&self, seconds: f64) {
        if seconds > 0.0 {
            std::thread::sleep(Duration::from_secs_f64(seconds));
        }
    }

    fn stamp(&self) -> String {
        let mut t = SystemTimeParts::default();
        // SAFETY: `t` is a valid SYSTEMTIME to fill in.
        unsafe { GetLocalTime(&mut t) };
        format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", t.year, t.month, t.day, t.hour, t.minute, t.second)
    }
}

// ═════════════════════════════════════════════════════════════════════
// The "pick a folder" dialog
// ═════════════════════════════════════════════════════════════════════
// Windows' own file dialog in its folder mode (IFileOpenDialog), through COM
// directly.  Method numbers are positions in the interfaces' tables, from
// shobjidl_core.h.

#[repr(C)]
struct Guid(u32, u16, u16, [u8; 8]);

const CLSID_FILE_OPEN_DIALOG: Guid = Guid(0xDC1C5A9C, 0xE88A, 0x4DDE, [0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7]);
const IID_IFILE_OPEN_DIALOG: Guid = Guid(0xD57C7288, 0xD4AD, 0x4768, [0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60]);
const IID_ISHELL_ITEM: Guid = Guid(0x43826D1E, 0xE718, 0x42EE, [0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE]);
const CLSCTX_INPROC_SERVER: u32 = 1;
const COINIT_APARTMENTTHREADED: u32 = 2;
const FOS_PICKFOLDERS: u32 = 0x20;
const FOS_FORCEFILESYSTEM: u32 = 0x40;
const SIGDN_FILESYSPATH: u32 = 0x8005_8000;
const CANCELLED: i32 = 0x8007_04C7_u32 as i32;
const RELEASE: usize = 2;
const DIALOG_SHOW: usize = 3;
const DIALOG_SET_OPTIONS: usize = 9;
const DIALOG_GET_OPTIONS: usize = 10;
const DIALOG_SET_FOLDER: usize = 12;
const DIALOG_GET_RESULT: usize = 20;
const ITEM_GET_DISPLAY_NAME: usize = 5;

#[link(name = "ole32")]
extern "system" {
    fn CoInitializeEx(reserved: *mut c_void, model: u32) -> i32;
    fn CoUninitialize();
    fn CoCreateInstance(class: *const Guid, outer: *mut c_void, context: u32, interface: *const Guid, out: *mut Handle) -> i32;
    fn CoTaskMemFree(memory: *mut c_void);
}

#[link(name = "shell32")]
extern "system" {
    fn SHCreateItemFromParsingName(path: *const u16, bind: *mut c_void, interface: *const Guid, out: *mut Handle) -> i32;
}

/// The method at `index` of a COM object's table.
///
/// # Safety
/// `object` must be a live COM interface pointer and `T` the method's type.
unsafe fn method<T: Copy>(object: Handle, index: usize) -> T {
    let table = *(object as *const *const *const c_void);
    std::mem::transmute_copy::<*const c_void, T>(&*table.add(index))
}

struct Com(Handle);

impl Drop for Com {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: a live interface pointer, released once.
            unsafe { method::<unsafe extern "system" fn(Handle) -> u32>(self.0, RELEASE)(self.0) };
        }
    }
}

/// Show the folder dialog over the window `owner` (0 for none), opened at
/// `start_in` where that exists.  The folder that was picked, or None when
/// the dialog was cancelled.
pub fn pick_folder(owner: isize, start_in: &str) -> Result<Option<String>, String> {
    type SetOptions = unsafe extern "system" fn(Handle, u32) -> i32;
    type GetOptions = unsafe extern "system" fn(Handle, *mut u32) -> i32;
    type SetFolder = unsafe extern "system" fn(Handle, Handle) -> i32;
    type Show = unsafe extern "system" fn(Handle, isize) -> i32;
    type GetResult = unsafe extern "system" fn(Handle, *mut Handle) -> i32;
    type GetDisplayName = unsafe extern "system" fn(Handle, u32, *mut *mut u16) -> i32;
    // SAFETY: COM is initialised for this thread for the length of the call
    // (and only uninitialised if this call initialised it); every interface
    // pointer is checked, used with its documented method types, and released
    // by its `Com` wrapper; the name Windows allocates is copied and freed.
    unsafe {
        let initialised = CoInitializeEx(std::ptr::null_mut(), COINIT_APARTMENTTHREADED) >= 0;
        let result = (|| {
            let mut dialog = Com(std::ptr::null_mut());
            let hr = CoCreateInstance(&CLSID_FILE_OPEN_DIALOG, std::ptr::null_mut(), CLSCTX_INPROC_SERVER, &IID_IFILE_OPEN_DIALOG, &mut dialog.0);
            if hr < 0 || dialog.0.is_null() {
                return Err(format!("the folder dialog could not be opened (0x{:08X})", hr as u32));
            }
            let mut options = 0u32;
            method::<GetOptions>(dialog.0, DIALOG_GET_OPTIONS)(dialog.0, &mut options);
            method::<SetOptions>(dialog.0, DIALOG_SET_OPTIONS)(dialog.0, options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);
            if !start_in.is_empty() && std::path::Path::new(start_in).is_dir() {
                let mut folder = Com(std::ptr::null_mut());
                if SHCreateItemFromParsingName(wide(start_in).as_ptr(), std::ptr::null_mut(), &IID_ISHELL_ITEM, &mut folder.0) >= 0
                    && !folder.0.is_null()
                {
                    method::<SetFolder>(dialog.0, DIALOG_SET_FOLDER)(dialog.0, folder.0);
                }
            }
            let hr = method::<Show>(dialog.0, DIALOG_SHOW)(dialog.0, owner);
            if hr == CANCELLED {
                return Ok(None);
            }
            if hr < 0 {
                return Err(format!("the folder dialog failed (0x{:08X})", hr as u32));
            }
            let mut item = Com(std::ptr::null_mut());
            if method::<GetResult>(dialog.0, DIALOG_GET_RESULT)(dialog.0, &mut item.0) < 0 || item.0.is_null() {
                return Err("the folder dialog gave no folder".into());
            }
            let mut name: *mut u16 = std::ptr::null_mut();
            if method::<GetDisplayName>(item.0, ITEM_GET_DISPLAY_NAME)(item.0, SIGDN_FILESYSPATH, &mut name) < 0 || name.is_null() {
                return Err("the folder that was picked has no path".into());
            }
            let mut length = 0;
            while *name.add(length) != 0 {
                length += 1;
            }
            let path = String::from_utf16_lossy(std::slice::from_raw_parts(name, length));
            CoTaskMemFree(name.cast());
            Ok(Some(path))
        })();
        if initialised {
            CoUninitialize();
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_address_is_taken_apart() {
        let url = |secure, host: &str, port, object: &str| Url { secure, host: host.into(), port, object: object.into() };
        assert_eq!(parse_url("https://api.github.com/repos/DeroXP/ghostshell/releases?per_page=30").unwrap(),
                   url(true, "api.github.com", 443, "/repos/DeroXP/ghostshell/releases?per_page=30"));
        assert_eq!(parse_url("https://vispora.up.railway.app").unwrap(), url(true, "vispora.up.railway.app", 443, "/"));
        assert_eq!(parse_url("http://127.0.0.1:8080/version/stable#top").unwrap(), url(false, "127.0.0.1", 8080, "/version/stable"));
        assert_eq!(parse_url("HTTPS://Example.test?x=1").unwrap(), url(true, "Example.test", 443, "/?x=1"));
        for bad in ["", "vispora.up.railway.app/version", "ftp://example.test/x", "file:///C:/Windows/x.exe",
                    "https://", "https:///x", "https://github.com@example.test/x", "https://example.test:99999/",
                    "https://example.test/a b", "https://example.test/a\r\nHost: x", "12345", "{'x': 1}"] {
            assert!(parse_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn the_stamp_is_the_local_time_to_the_second() {
        let stamp = RealClock.stamp();
        assert_eq!(stamp.len(), 19, "{stamp}");
        assert_eq!((&stamp[4..5], &stamp[10..11], &stamp[13..14]), ("-", "T", ":"));
        assert!(stamp[..4].parse::<u32>().unwrap() >= 2026);
        assert!(RealClock.now() > 1_700_000_000.0);
    }

    #[test]
    fn a_command_is_run_hidden_and_its_output_read() {
        let out = RealProcesses.run(&["cmd".into(), "/c".into(), "echo hello".into()], 10).unwrap();
        assert_eq!(out.trim(), "hello");
        assert!(RealProcesses.run(&["no-such-program-7f3a.exe".into()], 5).is_err());
        let err = RealProcesses.run(&["ping".into(), "-n".into(), "30".into(), "127.0.0.1".into()], 1).unwrap_err();
        assert!(err.contains("did not finish"), "{err}");
    }

    #[test]
    fn a_copy_replaces_the_target_and_keeps_the_time() {
        let dir = std::env::temp_dir().join(format!("vispora_updater_win_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (from, to) = (dir.join("a.bin"), dir.join("b.bin"));
        std::fs::write(&from, b"new").unwrap();
        std::fs::write(&to, b"old old old").unwrap();
        RealDisk.copy(from.to_str().unwrap(), to.to_str().unwrap()).unwrap();
        assert_eq!(std::fs::read(&to).unwrap(), b"new");
        assert_eq!(std::fs::metadata(&from).unwrap().modified().unwrap(), std::fs::metadata(&to).unwrap().modified().unwrap());
        assert!(RealDisk.copy(dir.join("missing").to_str().unwrap(), to.to_str().unwrap()).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
