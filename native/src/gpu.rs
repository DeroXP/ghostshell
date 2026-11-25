//! The NVIDIA card's live readings, from the driver's own NVML library.
//!
//! nvidia-smi is a small program around nvml.dll: every run loads the library,
//! opens the driver, reads, prints and exits.  The app ran it once a second
//! for the temperature ceiling and five times a second during a game.  Here
//! the library is loaded once and each reading is a few direct calls.
//!
//! Only reads.  Nothing in this file can change a clock, a limit or a fan.
//!
//! When the driver is replaced while the app runs (a driver update), the
//! loaded library no longer matches it and every call fails; the library is
//! then let go and opened again later, and the app's nvidia-smi fallback
//! answers in between.

use std::ffi::c_void;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::win32;

type Device = *mut c_void;
type Status = u32;

const NVML_SUCCESS: Status = 0;
const NVML_TEMPERATURE_GPU: u32 = 0;
const NVML_CLOCK_GRAPHICS: u32 = 0;
const NVML_CLOCK_MEM: u32 = 2;

/// After a failure to open the library, how long before it is tried again
/// (a PC without an NVIDIA card must not load-and-fail five times a second).
const RETRY_AFTER: Duration = Duration::from_secs(30);

#[repr(C)]
#[derive(Default)]
struct Utilization {
    gpu: u32,
    memory: u32,
}

#[repr(C)]
#[derive(Default)]
struct MemoryV1 {
    total: u64,
    free: u64,
    used: u64,
}

#[repr(C)]
#[derive(Default)]
struct MemoryV2 {
    version: u32,
    total: u64,
    reserved: u64,
    free: u64,
    used: u64,
}

/// nvmlMemory_v2: the structure's size with the version number in the top byte.
const MEMORY_V2_VERSION: u32 = (std::mem::size_of::<MemoryV2>() as u32) | (2 << 24);

type FnVoid = unsafe extern "C" fn() -> Status;
type FnHandleByIndex = unsafe extern "C" fn(u32, *mut Device) -> Status;
type FnU32 = unsafe extern "C" fn(Device, *mut u32) -> Status;
type FnKindU32 = unsafe extern "C" fn(Device, u32, *mut u32) -> Status;
type FnUtilization = unsafe extern "C" fn(Device, *mut Utilization) -> Status;
type FnMemoryV1 = unsafe extern "C" fn(Device, *mut MemoryV1) -> Status;
type FnMemoryV2 = unsafe extern "C" fn(Device, *mut MemoryV2) -> Status;

struct Nvml {
    module: win32::Handle,
    device: Device,
    shutdown: FnVoid,
    temperature: Option<FnKindU32>,
    utilization: Option<FnUtilization>,
    clock: Option<FnKindU32>,
    max_clock: Option<FnKindU32>,
    app_clock: Option<FnKindU32>,
    power_usage: Option<FnU32>,
    power_limit: Option<FnU32>,
    fan_speed: Option<FnU32>,
    memory_v2: Option<FnMemoryV2>,
    memory_v1: Option<FnMemoryV1>,
}

// SAFETY: NVML is documented thread-safe once initialised, the device handle
// is an opaque token, and every use goes through the mutex below anyway.
unsafe impl Send for Nvml {}

impl Drop for Nvml {
    fn drop(&mut self) {
        // SAFETY: `shutdown` was resolved from `module`, which is still loaded.
        unsafe {
            (self.shutdown)();
            win32::FreeLibrary(self.module);
        }
    }
}

#[derive(Default)]
struct State {
    nvml: Option<Nvml>,
    failed_at: Option<Instant>,
    failure: String,
    hold_until: Option<Instant>,
}

static STATE: Mutex<State> = Mutex::new(State { nvml: None, failed_at: None, failure: String::new(), hold_until: None });

/// Resolve an exported function, or None when this driver's library lacks it.
///
/// # Safety
/// `T` must be the function pointer type the export really has.
unsafe fn export<T: Copy>(module: win32::Handle, name: &'static str) -> Option<T> {
    debug_assert!(name.ends_with('\0'));
    debug_assert_eq!(std::mem::size_of::<T>(), std::mem::size_of::<*const c_void>());
    let p = win32::GetProcAddress(module, name.as_ptr());
    if p.is_null() {
        None
    } else {
        Some(std::mem::transmute_copy::<*const c_void, T>(&p))
    }
}

fn load_library() -> Result<win32::Handle, String> {
    // The driver installs nvml.dll into System32; only that folder is searched.
    let name = win32::wide("nvml.dll");
    // SAFETY: `name` is NUL-terminated UTF-16.
    let module = unsafe { win32::LoadLibraryExW(name.as_ptr(), 0, win32::LOAD_LIBRARY_SEARCH_SYSTEM32) };
    if module != 0 {
        return Ok(module);
    }
    // Drivers from before 2018 kept it beside nvidia-smi (a folder only
    // administrators can write to).
    if let Ok(program_files) = std::env::var("ProgramW6432") {
        let old = win32::wide(&format!("{program_files}\\NVIDIA Corporation\\NVSMI\\nvml.dll"));
        // SAFETY: as above; a full path, searched nowhere else.
        let module = unsafe { win32::LoadLibraryExW(old.as_ptr(), 0, win32::LOAD_WITH_ALTERED_SEARCH_PATH) };
        if module != 0 {
            return Ok(module);
        }
    }
    Err("no NVIDIA driver library (nvml.dll) on this PC".into())
}

fn open() -> Result<Nvml, String> {
    let module = load_library()?;
    // SAFETY: each type below is the documented signature of that NVML export.
    unsafe {
        let init: Option<FnVoid> = export(module, "nvmlInit_v2\0");
        let shutdown: Option<FnVoid> = export(module, "nvmlShutdown\0");
        let by_index: Option<FnHandleByIndex> = export(module, "nvmlDeviceGetHandleByIndex_v2\0");
        let (Some(init), Some(shutdown), Some(by_index)) = (init, shutdown, by_index) else {
            win32::FreeLibrary(module);
            return Err("nvml.dll lacks the functions every version has".into());
        };
        let status = init();
        if status != NVML_SUCCESS {
            win32::FreeLibrary(module);
            return Err(format!("the NVIDIA driver did not start NVML (status {status})"));
        }
        let mut device: Device = std::ptr::null_mut();
        let status = by_index(0, &mut device);
        if status != NVML_SUCCESS || device.is_null() {
            shutdown();
            win32::FreeLibrary(module);
            return Err(format!("no NVIDIA card found (status {status})"));
        }
        Ok(Nvml {
            module,
            device,
            shutdown,
            temperature: export(module, "nvmlDeviceGetTemperature\0"),
            utilization: export(module, "nvmlDeviceGetUtilizationRates\0"),
            clock: export(module, "nvmlDeviceGetClockInfo\0"),
            max_clock: export(module, "nvmlDeviceGetMaxClockInfo\0"),
            app_clock: export(module, "nvmlDeviceGetApplicationsClock\0"),
            power_usage: export(module, "nvmlDeviceGetPowerUsage\0"),
            power_limit: export(module, "nvmlDeviceGetPowerManagementLimit\0"),
            fan_speed: export(module, "nvmlDeviceGetFanSpeed\0"),
            memory_v2: export(module, "nvmlDeviceGetMemoryInfo_v2\0"),
            memory_v1: export(module, "nvmlDeviceGetMemoryInfo\0"),
        })
    }
}

impl Nvml {
    fn u32_of(&self, f: Option<FnU32>) -> Option<u32> {
        let mut v = 0u32;
        // SAFETY: `f` came from this library with this signature; `v` is a valid out pointer.
        (unsafe { f?(self.device, &mut v) } == NVML_SUCCESS).then_some(v)
    }

    fn kind_u32_of(&self, f: Option<FnKindU32>, kind: u32) -> Option<u32> {
        let mut v = 0u32;
        // SAFETY: as above.
        (unsafe { f?(self.device, kind, &mut v) } == NVML_SUCCESS).then_some(v)
    }

    fn utilization(&self) -> Option<Utilization> {
        let mut u = Utilization::default();
        // SAFETY: as above.
        (unsafe { self.utilization?(self.device, &mut u) } == NVML_SUCCESS).then_some(u)
    }

    /// (used, total) in bytes, counted the way nvidia-smi's memory.used and
    /// memory.total are: the newer call, which leaves out what the driver
    /// reserves for itself, where the driver has it.
    fn memory(&self) -> Option<(u64, u64)> {
        if let Some(f) = self.memory_v2 {
            let mut m = MemoryV2 { version: MEMORY_V2_VERSION, ..Default::default() };
            // SAFETY: as above; the structure carries its own version tag.
            if unsafe { f(self.device, &mut m) } == NVML_SUCCESS {
                return Some((m.used, m.total));
            }
        }
        let mut m = MemoryV1::default();
        // SAFETY: as above.
        (unsafe { self.memory_v1?(self.device, &mut m) } == NVML_SUCCESS).then_some((m.used, m.total))
    }

    fn read(&self) -> Reading {
        let util = self.utilization();
        let memory = self.memory();
        Reading {
            temp_c: self.kind_u32_of(self.temperature, NVML_TEMPERATURE_GPU),
            gpu_util_pct: util.as_ref().map(|u| u.gpu),
            mem_util_pct: util.as_ref().map(|u| u.memory),
            core_mhz: self.kind_u32_of(self.clock, NVML_CLOCK_GRAPHICS),
            mem_mhz: self.kind_u32_of(self.clock, NVML_CLOCK_MEM),
            core_max_mhz: self.kind_u32_of(self.max_clock, NVML_CLOCK_GRAPHICS),
            mem_max_mhz: self.kind_u32_of(self.max_clock, NVML_CLOCK_MEM),
            core_app_mhz: self.kind_u32_of(self.app_clock, NVML_CLOCK_GRAPHICS),
            mem_app_mhz: self.kind_u32_of(self.app_clock, NVML_CLOCK_MEM),
            power_mw: self.u32_of(self.power_usage),
            power_limit_mw: self.u32_of(self.power_limit),
            fan_pct: self.u32_of(self.fan_speed),
            mem_used_bytes: memory.map(|m| m.0),
            mem_total_bytes: memory.map(|m| m.1),
        }
    }
}

/// One reading.  A value the card does not report is None (JSON null).
#[derive(Debug, Default, Clone, PartialEq)]
pub struct Reading {
    pub temp_c: Option<u32>,
    pub gpu_util_pct: Option<u32>,
    pub mem_util_pct: Option<u32>,
    pub core_mhz: Option<u32>,
    pub mem_mhz: Option<u32>,
    pub core_max_mhz: Option<u32>,
    pub mem_max_mhz: Option<u32>,
    pub core_app_mhz: Option<u32>,
    pub mem_app_mhz: Option<u32>,
    pub power_mw: Option<u32>,
    pub power_limit_mw: Option<u32>,
    pub fan_pct: Option<u32>,
    pub mem_used_bytes: Option<u64>,
    pub mem_total_bytes: Option<u64>,
}

impl Reading {
    /// Nothing answered at all: the library no longer talks to the driver.
    fn is_empty(&self) -> bool {
        *self == Reading::default()
    }

    pub fn to_json(&self) -> Value {
        json!({
            "temp_c": self.temp_c,
            "gpu_util_pct": self.gpu_util_pct,
            "mem_util_pct": self.mem_util_pct,
            "core_mhz": self.core_mhz,
            "mem_mhz": self.mem_mhz,
            "core_max_mhz": self.core_max_mhz,
            "mem_max_mhz": self.mem_max_mhz,
            "core_app_mhz": self.core_app_mhz,
            "mem_app_mhz": self.mem_app_mhz,
            "power_mw": self.power_mw,
            "power_limit_mw": self.power_limit_mw,
            "fan_pct": self.fan_pct,
            "mem_used_bytes": self.mem_used_bytes,
            "mem_total_bytes": self.mem_total_bytes,
        })
    }
}

/// `q: gpu_live` - the first NVIDIA card's readings.
pub fn live() -> Result<Value, String> {
    let mut state = STATE.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let now = Instant::now();
    if let Some(until) = state.hold_until {
        if now < until {
            return Err("the NVIDIA library is released for a driver install".into());
        }
        state.hold_until = None;
    }
    if state.nvml.is_none() {
        if let Some(at) = state.failed_at {
            if now.duration_since(at) < RETRY_AFTER {
                return Err(state.failure.clone());
            }
        }
        match open() {
            Ok(nvml) => {
                state.nvml = Some(nvml);
                state.failed_at = None;
            }
            Err(why) => {
                state.failed_at = Some(now);
                state.failure = why.clone();
                return Err(why);
            }
        }
    }
    let reading = state.nvml.as_ref().map(Nvml::read).unwrap_or_default();
    if reading.is_empty() {
        // The driver was replaced or reset under the loaded library.  Let go
        // of it; it is opened afresh after the retry pause.
        state.nvml = None;
        state.failed_at = Some(now);
        state.failure = "the NVIDIA driver stopped answering (replaced or reset?)".into();
        return Err(state.failure.clone());
    }
    Ok(reading.to_json())
}

/// `q: gpu_release` - let go of the NVIDIA library now and do not open it
/// again for `hold_s` seconds (a driver installer is about to replace it).
pub fn release(hold_s: u64) -> Value {
    let mut state = STATE.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let was_open = state.nvml.take().is_some();
    state.failed_at = None;
    state.hold_until = (hold_s > 0).then(|| Instant::now() + Duration::from_secs(hold_s.min(3600)));
    json!({ "released": was_open, "hold_s": hold_s.min(3600) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_v2_memory_structure_is_tagged_as_nvml_expects() {
        assert_eq!(std::mem::size_of::<MemoryV2>(), 40);
        assert_eq!(MEMORY_V2_VERSION, 0x0200_0028);
        assert_eq!(std::mem::size_of::<MemoryV1>(), 24);
        assert_eq!(std::mem::size_of::<Utilization>(), 8);
    }

    #[test]
    fn a_reading_keeps_missing_values_as_null() {
        let r = Reading { temp_c: Some(55), power_mw: Some(147_560), ..Default::default() };
        let j = r.to_json();
        assert_eq!(j["temp_c"], 55);
        assert_eq!(j["power_mw"], 147_560);
        assert!(j["fan_pct"].is_null() && j["mem_used_bytes"].is_null());
        assert_eq!(j.as_object().unwrap().len(), 14);
        assert!(!r.is_empty() && Reading::default().is_empty());
    }

    #[test]
    fn live_answers_or_says_why_not_and_release_holds_it_off() {
        // With an NVIDIA card: a reading with a temperature.  Without one: an
        // error that says so.  Either way it must not crash, twice in a row.
        for _ in 0..2 {
            match live() {
                Ok(v) => assert!(v["temp_c"].as_u64().unwrap() < 130, "{v}"),
                Err(why) => assert!(why.len() > 5),
            }
        }
        let released = release(2);
        assert_eq!(released["hold_s"], 2);
        assert!(live().unwrap_err().contains("released"));
        release(0); // lift the hold again
    }
}
