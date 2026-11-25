//! Power plans, read from Windows' own power API (powrprof.dll).
//!
//! The app asked `powercfg.exe` for the active plan and for single settings
//! of it, a program start each time: two before the window opens, more in the
//! start-up tasks, and a dozen every time a game starts.  These are the calls
//! powercfg itself makes.
//!
//! Only reads.  Nothing in this file can switch a plan or change a setting.

use std::ffi::c_void;

use crate::json::Json;

/// A GUID as Windows lays it out.
#[repr(C)]
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub struct Guid {
    a: u32,
    b: u16,
    c: u16,
    d: [u8; 8],
}

impl Guid {
    /// "381b4222-f694-41f0-9685-ff5bb260df2e", in either case, braces allowed.
    pub fn parse(text: &str) -> Option<Guid> {
        let text = text.trim().trim_start_matches('{').trim_end_matches('}');
        let bytes = text.as_bytes();
        if bytes.len() != 36 || [8, 13, 18, 23].iter().any(|&i| bytes[i] != b'-') {
            return None;
        }
        let hex: Vec<u8> = bytes.iter().copied().filter(|&b| b != b'-').collect();
        if hex.len() != 32 || !hex.iter().all(u8::is_ascii_hexdigit) {
            return None;
        }
        let text = std::str::from_utf8(&hex).ok()?;
        let mut d = [0u8; 8];
        for (i, slot) in d.iter_mut().enumerate() {
            *slot = u8::from_str_radix(&text[16 + i * 2..18 + i * 2], 16).ok()?;
        }
        Some(Guid {
            a: u32::from_str_radix(&text[0..8], 16).ok()?,
            b: u16::from_str_radix(&text[8..12], 16).ok()?,
            c: u16::from_str_radix(&text[12..16], 16).ok()?,
            d,
        })
    }

    /// Lower case with dashes, as powercfg prints it.
    pub fn text(&self) -> String {
        format!(
            "{:08x}-{:04x}-{:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
            self.a, self.b, self.c, self.d[0], self.d[1], self.d[2], self.d[3], self.d[4], self.d[5], self.d[6], self.d[7]
        )
    }
}

const ERROR_SUCCESS: u32 = 0;
const ACCESS_SCHEME: u32 = 16;
const MAX_ITEMS: u32 = 4096;
/// HKEY_LOCAL_MACHINE: the constant 0x80000002, sign-extended to a pointer.
const HKEY_LOCAL_MACHINE: isize = 0x8000_0002_u32 as i32 as isize;
const KEY_QUERY_VALUE: u32 = 0x0001;
/// Where Windows keeps the definition of every power setting, by subgroup.
const SETTINGS_KEY: &str = "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerSettings";

#[link(name = "powrprof")]
extern "system" {
    fn PowerGetActiveScheme(user_root: isize, active: *mut *mut Guid) -> u32;
    fn PowerReadACValueIndex(root: isize, scheme: *const Guid, sub: *const Guid, setting: *const Guid, value: *mut u32) -> u32;
    fn PowerReadDCValueIndex(root: isize, scheme: *const Guid, sub: *const Guid, setting: *const Guid, value: *mut u32) -> u32;
    fn PowerReadValueMin(root: isize, sub: *const Guid, setting: *const Guid, value: *mut u32) -> u32;
    fn PowerReadValueMax(root: isize, sub: *const Guid, setting: *const Guid, value: *mut u32) -> u32;
    fn PowerReadValueIncrement(root: isize, sub: *const Guid, setting: *const Guid, value: *mut u32) -> u32;
    fn PowerEnumerate(
        root: isize,
        scheme: *const Guid,
        sub: *const Guid,
        access_flags: u32,
        index: u32,
        buffer: *mut u8,
        buffer_size: *mut u32,
    ) -> u32;
}

#[link(name = "kernel32")]
extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[link(name = "advapi32")]
extern "system" {
    fn RegOpenKeyExW(key: isize, sub_key: *const u16, options: u32, access: u32, result: *mut isize) -> i32;
    fn RegCloseKey(key: isize) -> i32;
}

/// Does Windows know a setting of this name in this subgroup?  Its
/// definition is a registry key; opening it to read is the whole check.
/// (Listing the subgroup's settings through the power API says the same and
/// takes thirty milliseconds for the processor's hundred or so.)
fn setting_exists(sub: &Guid, setting: &Guid) -> bool {
    let path = crate::win32::wide(&format!("{SETTINGS_KEY}\\{}\\{}", sub.text(), setting.text()));
    let mut key: isize = 0;
    // SAFETY: `path` is NUL-terminated UTF-16 and `key` a valid out pointer.
    let status = unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, path.as_ptr(), 0, KEY_QUERY_VALUE, &mut key) };
    if status != 0 {
        return false;
    }
    // SAFETY: opened above, closed once.
    unsafe { RegCloseKey(key) };
    true
}

/// The plan that is active now.
fn active_scheme() -> Result<Guid, String> {
    let mut active: *mut Guid = std::ptr::null_mut();
    // SAFETY: `active` is a valid out pointer; on success Windows hands back
    // memory it allocated, which is copied and freed below.
    let status = unsafe { PowerGetActiveScheme(0, &mut active) };
    if status != ERROR_SUCCESS || active.is_null() {
        return Err(format!("Windows did not name the active power plan (error {status})"));
    }
    // SAFETY: `active` points to one GUID, as documented.
    let guid = unsafe { *active };
    // SAFETY: allocated by PowerGetActiveScheme with LocalAlloc.
    unsafe { LocalFree(active.cast()) };
    Ok(guid)
}

/// The GUIDs Windows lists for `what` (the power plans, for ACCESS_SCHEME).
fn enumerate(scheme: Option<&Guid>, sub: Option<&Guid>, what: u32) -> Vec<Guid> {
    let mut out = Vec::new();
    for index in 0..MAX_ITEMS {
        let mut guid = Guid::default();
        let mut size = std::mem::size_of::<Guid>() as u32;
        // SAFETY: the buffer is one GUID, `size` says so; null means "not given".
        let status = unsafe {
            PowerEnumerate(
                0,
                scheme.map_or(std::ptr::null(), |g| g as *const Guid),
                sub.map_or(std::ptr::null(), |g| g as *const Guid),
                what,
                index,
                (&mut guid as *mut Guid).cast(),
                &mut size,
            )
        };
        if status != ERROR_SUCCESS {
            break; // ERROR_NO_MORE_ITEMS at the end
        }
        out.push(guid);
    }
    out
}

/// `q: power_scheme` - the active power plan and every plan there is:
/// `{"guid": "...", "schemes": ["...", ...]}`.  What `powercfg
/// /getactivescheme` and `powercfg /list` print.
pub fn scheme() -> Result<Json, String> {
    let active = active_scheme()?;
    let all = enumerate(None, None, ACCESS_SCHEME);
    Ok(Json::object([
        ("guid", active.text().into()),
        ("schemes", Json::Array(all.iter().map(|g| g.text().into()).collect())),
    ]))
}

/// `q: power_setting` - one setting of one plan: `{"scheme", "ac", "dc",
/// "min", "max", "increment"}`.  `ac` and `dc` are the two "Current ... Power
/// Setting Index" values `powercfg /qh` prints; `min`, `max` and `increment`
/// are the range it prints above them, null for a setting that is a list of
/// choices and not a range.
///
/// `scheme` is a plan's GUID, or "current" (or nothing) for the active plan.
/// An unknown plan, subgroup or setting is an error, as it is for powercfg.
pub fn setting(scheme: &str, sub: &str, setting: &str) -> Result<Json, String> {
    let scheme = match scheme.trim().to_ascii_lowercase().as_str() {
        "" | "current" | "scheme_current" => active_scheme()?,
        other => Guid::parse(other).ok_or("the power plan is not a GUID")?,
    };
    let sub = Guid::parse(sub).ok_or("the power subgroup is not a GUID")?;
    let setting = Guid::parse(setting).ok_or("the power setting is not a GUID")?;
    if !enumerate(None, None, ACCESS_SCHEME).contains(&scheme) {
        return Err(format!("there is no power plan {}", scheme.text()));
    }
    if !setting_exists(&sub, &setting) {
        return Err(format!("the power subgroup {} has no setting {}", sub.text(), setting.text()));
    }
    let (mut ac, mut dc) = (0u32, 0u32);
    // SAFETY: the three GUIDs are valid for the call, the out pointers too.
    let status = unsafe { PowerReadACValueIndex(0, &scheme, &sub, &setting, &mut ac) };
    if status != ERROR_SUCCESS {
        return Err(format!("Windows did not give the setting's AC value (error {status})"));
    }
    // SAFETY: as above.
    let status = unsafe { PowerReadDCValueIndex(0, &scheme, &sub, &setting, &mut dc) };
    if status != ERROR_SUCCESS {
        return Err(format!("Windows did not give the setting's DC value (error {status})"));
    }
    type RangeFn = unsafe extern "system" fn(isize, *const Guid, *const Guid, *mut u32) -> u32;
    let range = |f: RangeFn| -> Option<u32> {
        let mut v = 0u32;
        // SAFETY: as above.
        (unsafe { f(0, &sub, &setting, &mut v) } == ERROR_SUCCESS).then_some(v)
    };
    let (min, max, increment) =
        (range(PowerReadValueMin), range(PowerReadValueMax), range(PowerReadValueIncrement));
    // A range is all three or nothing: powercfg prints them together.
    let is_range = min.is_some() && max.is_some() && increment.is_some();
    Ok(Json::object([
        ("scheme", scheme.text().into()),
        ("ac", ac.into()),
        ("dc", dc.into()),
        ("min", if is_range { min.into() } else { Json::Null }),
        ("max", if is_range { max.into() } else { Json::Null }),
        ("increment", if is_range { increment.into() } else { Json::Null }),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    const PROCESSOR: &str = "54533251-82be-4824-96c1-47b60b740d00";
    const PROCESSOR_MAX: &str = "bc5038f7-23e0-4960-96da-33abaf5935ec";

    #[test]
    fn a_guid_reads_and_prints_the_same() {
        let text = "381b4222-f694-41f0-9685-ff5bb260df2e";
        let g = Guid::parse(text).unwrap();
        assert_eq!(g.text(), text);
        assert_eq!(Guid::parse(&format!("{{{}}}", text.to_uppercase())), Some(g));
        assert_eq!(std::mem::size_of::<Guid>(), 16);
        assert_eq!((g.a, g.b, g.c, g.d[0], g.d[7]), (0x381b_4222, 0xf694, 0x41f0, 0x96, 0x2e));
        for bad in ["", "current", "381b4222f69441f09685ff5bb260df2e", "381b4222-f694-41f0-9685-ff5bb260df2", "381b4222-f694-41f0-9685-ff5bb260dfzz", "381b4222_f694_41f0_9685_ff5bb260df2e"] {
            assert_eq!(Guid::parse(bad), None, "{bad}");
        }
    }

    #[test]
    fn the_active_plan_is_one_of_the_plans() {
        let s = scheme().unwrap();
        let active = s["guid"].as_str().unwrap().to_string();
        assert!(Guid::parse(&active).is_some(), "{active}");
        let all: Vec<&str> = s["schemes"].as_array().unwrap().iter().filter_map(Json::as_str).collect();
        assert!(all.contains(&active.as_str()), "{active} in {all:?}");
    }

    #[test]
    fn a_range_setting_has_its_range_and_both_values() {
        let v = setting("current", PROCESSOR, PROCESSOR_MAX).unwrap();
        assert_eq!(v["min"].as_u64(), Some(0), "{v}");
        assert!(v["max"].as_u64().unwrap() <= 100, "{v}");
        assert!(v["ac"].as_u64().unwrap() <= 100 && v["dc"].as_u64().unwrap() <= 100, "{v}");
        assert_eq!(v["scheme"], scheme().unwrap()["guid"]);
        // The same plan asked for by its GUID answers the same.
        let by_guid = setting(v["scheme"].as_str().unwrap(), PROCESSOR, PROCESSOR_MAX).unwrap();
        assert_eq!(by_guid, v);
    }

    #[test]
    fn a_setting_is_known_by_its_definition() {
        let sub = Guid::parse(PROCESSOR).unwrap();
        assert!(setting_exists(&sub, &Guid::parse(PROCESSOR_MAX).unwrap()));
        // A hidden one (processor idle disable) is known just the same.
        assert!(setting_exists(&sub, &Guid::parse("5d76a2ca-e8c0-402f-a133-2158492d58ad").unwrap()));
        assert!(!setting_exists(&sub, &Guid::parse("12345678-1234-1234-1234-123456789abc").unwrap()));
        assert!(!setting_exists(&Guid::parse("12345678-1234-1234-1234-123456789abc").unwrap(), &sub));
        assert_eq!(HKEY_LOCAL_MACHINE as usize, 0xFFFF_FFFF_8000_0002);
    }

    #[test]
    fn what_does_not_exist_is_an_error_not_a_made_up_value() {
        let nowhere = "12345678-1234-1234-1234-123456789abc";
        assert!(setting(nowhere, PROCESSOR, PROCESSOR_MAX).unwrap_err().contains("no power plan"));
        assert!(setting("current", PROCESSOR, nowhere).unwrap_err().contains("no setting"));
        assert!(setting("current", nowhere, PROCESSOR_MAX).is_err());
        assert!(setting("current", "SUB_PROCESSOR", PROCESSOR_MAX).unwrap_err().contains("not a GUID"));
    }
}
