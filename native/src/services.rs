//! Is a Windows service installed, and is it running?
//!
//! What `sc query <name>` answers, from the service manager directly.  The
//! start-up dependency check asked sc.exe once per name it knows a service by.
//!
//! Only reads: the service manager is opened to connect, the service to query
//! its status, and neither handle can start, stop or change anything.

use crate::json::Json;
use crate::win32::{self, wide};

const SC_MANAGER_CONNECT: u32 = 0x0001;
const SERVICE_QUERY_STATUS: u32 = 0x0004;
const ERROR_INVALID_NAME: u32 = 123;
const ERROR_SERVICE_DOES_NOT_EXIST: u32 = 1060;

#[repr(C)]
#[derive(Default)]
struct ServiceStatus {
    service_type: u32,
    current_state: u32,
    controls_accepted: u32,
    win32_exit_code: u32,
    service_specific_exit_code: u32,
    check_point: u32,
    wait_hint: u32,
}

#[link(name = "advapi32")]
extern "system" {
    fn OpenSCManagerW(machine: *const u16, database: *const u16, access: u32) -> isize;
    fn OpenServiceW(manager: isize, name: *const u16, access: u32) -> isize;
    fn QueryServiceStatus(service: isize, status: *mut ServiceStatus) -> i32;
    fn CloseServiceHandle(handle: isize) -> i32;
}

struct ScHandle(isize);

impl Drop for ScHandle {
    fn drop(&mut self) {
        // SAFETY: the handle came from the service manager and is closed once.
        unsafe { CloseServiceHandle(self.0) };
    }
}

/// `q: service_state` - `{"installed": bool, "state": n}`.  `state` is
/// Windows' number for it (1 stopped, 2 starting, 3 stopping, 4 running, 5 to
/// 7 the paused ones), null when the service is not installed.
pub fn state(name: &str) -> Result<Json, String> {
    if name.is_empty() || name.len() > 256 || name.contains('\0') {
        return Err("not a service name".into());
    }
    // SAFETY: null machine and database mean this PC's active database.
    let manager = unsafe { OpenSCManagerW(std::ptr::null(), std::ptr::null(), SC_MANAGER_CONNECT) };
    if manager == 0 {
        // SAFETY: reads the calling thread's last error.
        return Err(format!("the service manager could not be opened (error {})", unsafe { win32::GetLastError() }));
    }
    let manager = ScHandle(manager);
    let wide_name = wide(name);
    // SAFETY: `wide_name` is NUL-terminated UTF-16; the manager handle is open.
    let service = unsafe { OpenServiceW(manager.0, wide_name.as_ptr(), SERVICE_QUERY_STATUS) };
    if service == 0 {
        // SAFETY: as above.
        let error = unsafe { win32::GetLastError() };
        if error == ERROR_SERVICE_DOES_NOT_EXIST || error == ERROR_INVALID_NAME {
            return Ok(Json::object([("installed", false.into()), ("state", Json::Null)]));
        }
        return Err(format!("the service could not be opened (error {error})"));
    }
    let service = ScHandle(service);
    let mut status = ServiceStatus::default();
    // SAFETY: `status` is a valid SERVICE_STATUS to fill in.
    if unsafe { QueryServiceStatus(service.0, &mut status) } == 0 {
        // SAFETY: as above.
        return Err(format!("the service did not give its status (error {})", unsafe { win32::GetLastError() }));
    }
    Ok(Json::object([("installed", true.into()), ("state", status.current_state.into())]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_service_every_windows_has_is_found_and_has_a_state() {
        // The event log service: installed on every Windows, and running.
        let v = state("EventLog").unwrap();
        assert_eq!(v["installed"], Json::Bool(true), "{v}");
        assert_eq!(v["state"].as_u64(), Some(4), "{v}");
        assert_eq!(std::mem::size_of::<ServiceStatus>(), 28);
    }

    #[test]
    fn a_service_that_is_not_there_is_not_installed() {
        let v = state("VisporaNoSuchService_7f3a").unwrap();
        assert_eq!(v["installed"], Json::Bool(false));
        assert!(v["state"].is_null());
        assert!(state("").is_err());
        assert!(state("a\0b").is_err());
    }
}
