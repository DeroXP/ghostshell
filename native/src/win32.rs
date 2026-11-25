//! The handful of Windows functions the helper calls, declared by hand.
//!
//! There are few enough of them that a bindings crate would mostly add build
//! time, and their C signatures have not changed since Windows XP.  Handles
//! are `isize` (a pointer-sized integer), BOOL is `i32`, NTSTATUS is `i32`.

#![allow(non_snake_case)]

use std::ffi::c_void;

pub type Handle = isize;
pub type Hwnd = isize;

pub const GW_OWNER: u32 = 4;

// NtQuerySystemInformation classes
pub const SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION: u32 = 8;
pub const SYSTEM_PROCESS_INFORMATION: u32 = 5;
pub const STATUS_INFO_LENGTH_MISMATCH: i32 = 0xC000_0004_u32 as i32;

// CallNtPowerInformation level
pub const PROCESSOR_INFORMATION: i32 = 11;

// LoadLibraryExW flags
pub const LOAD_LIBRARY_SEARCH_SYSTEM32: u32 = 0x0000_0800;
pub const LOAD_WITH_ALTERED_SEARCH_PATH: u32 = 0x0000_0008;

pub const INVALID_HANDLE_VALUE: Handle = -1;

#[link(name = "user32")]
extern "system" {
    pub fn EnumWindows(callback: unsafe extern "system" fn(Hwnd, isize) -> i32, lparam: isize) -> i32;
    pub fn GetWindow(hwnd: Hwnd, cmd: u32) -> Hwnd;
    pub fn IsWindowVisible(hwnd: Hwnd) -> i32;
    pub fn GetWindowThreadProcessId(hwnd: Hwnd, process_id: *mut u32) -> u32;
}

#[link(name = "ntdll")]
extern "system" {
    pub fn NtQuerySystemInformation(class: u32, info: *mut c_void, length: u32, return_length: *mut u32) -> i32;
}

#[link(name = "powrprof")]
extern "system" {
    pub fn CallNtPowerInformation(
        level: i32,
        input: *const c_void,
        input_length: u32,
        output: *mut c_void,
        output_length: u32,
    ) -> i32;
}

#[link(name = "iphlpapi")]
extern "system" {
    pub fn IcmpCreateFile() -> Handle;
    pub fn IcmpCloseHandle(handle: Handle) -> i32;
    pub fn IcmpSendEcho(
        handle: Handle,
        destination: u32,
        request_data: *const c_void,
        request_size: u16,
        request_options: *const c_void,
        reply_buffer: *mut c_void,
        reply_size: u32,
        timeout_ms: u32,
    ) -> u32;
}

#[link(name = "kernel32")]
extern "system" {
    pub fn LoadLibraryExW(file_name: *const u16, file: Handle, flags: u32) -> Handle;
    pub fn GetProcAddress(module: Handle, name: *const u8) -> *const c_void;
    pub fn FreeLibrary(module: Handle) -> i32;
    pub fn GetLastError() -> u32;
}

/// A Rust string as the NUL-terminated UTF-16 Windows wants.
pub fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Read a little-endian value at `offset` of a byte buffer, or None when the
/// buffer is too short.  The system structures here are read by offset, so
/// every read is bounds-checked.
pub fn read<T: Copy>(buf: &[u8], offset: usize) -> Option<T> {
    let size = std::mem::size_of::<T>();
    if offset.checked_add(size)? > buf.len() {
        return None;
    }
    // SAFETY: the range was checked above; read_unaligned has no alignment
    // requirement, and T is a plain integer type at every call site.
    Some(unsafe { std::ptr::read_unaligned(buf.as_ptr().add(offset) as *const T) })
}
