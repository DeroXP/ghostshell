//! One WMI query, answered as JSON rows.
//!
//! The app reads the processor temperature from the sensors that
//! LibreHardwareMonitor publishes through WMI (and three older sources when
//! that one is not there).  Through PowerShell that was up to four processes
//! at every pass of the hardware monitor.  Later pieces of the port use the
//! same question for the system information page (Win32_Processor and the
//! like), which was seven PowerShell starts before the first screen had data.
//!
//! Only SELECT queries: this runs ExecQuery, which cannot change anything.
//!
//! A namespace or class that does not exist, or access denied, answers an
//! empty list with a note - exactly what the PowerShell pipelines printed
//! (nothing) under `-ErrorAction SilentlyContinue`.
//!
//! WMI is COM.  The few interfaces needed are called through their method
//! tables directly; the positions used below are those of wbemcli.idl, which
//! has not changed since Windows 2000.

use std::ffi::c_void;
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::time::Duration;

use crate::json::Json;
use crate::Reply;

type Ptr = *mut c_void;
type Hresult = i32;

#[repr(C)]
struct Guid {
    data1: u32,
    data2: u16,
    data3: u16,
    data4: [u8; 8],
}

/// CLSID_WbemLocator {4590F811-1D3A-11D0-891F-00AA004B2E24}
const CLSID_WBEM_LOCATOR: Guid =
    Guid { data1: 0x4590_F811, data2: 0x1D3A, data3: 0x11D0, data4: [0x89, 0x1F, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24] };
/// IID_IWbemLocator {DC12A687-737F-11CF-884D-00AA004B2E24}
const IID_IWBEM_LOCATOR: Guid =
    Guid { data1: 0xDC12_A687, data2: 0x737F, data3: 0x11CF, data4: [0x88, 0x4D, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24] };

const COINIT_MULTITHREADED: u32 = 0;
const CLSCTX_INPROC_SERVER: u32 = 1;
const RPC_C_AUTHN_WINNT: u32 = 10;
const RPC_C_AUTHZ_NONE: u32 = 0;
const RPC_C_AUTHN_LEVEL_CALL: u32 = 3;
const RPC_C_IMP_LEVEL_IMPERSONATE: u32 = 3;
const EOAC_NONE: u32 = 0;

const WBEM_FLAG_CONNECT_USE_MAX_WAIT: i32 = 0x80;
const WBEM_FLAG_RETURN_IMMEDIATELY: i32 = 0x10;
const WBEM_FLAG_FORWARD_ONLY: i32 = 0x20;
const WBEM_FLAG_NONSYSTEM_ONLY: i32 = 0x40;
const WBEM_S_FALSE: Hresult = 1;
const WBEM_S_TIMEDOUT: Hresult = 0x4_0004;
const WBEM_S_NO_MORE_DATA: Hresult = 0x4_0005;

const CIM_SINT64: i32 = 20;
const CIM_UINT64: i32 = 21;

const VT_ARRAY: u16 = 0x2000;

/// How long one row may take to arrive, and how long the whole query may take
/// before the caller stops waiting (the PowerShell calls had 4 to 5 seconds).
const ROW_TIMEOUT_MS: i32 = 4000;
const QUERY_TIMEOUT: Duration = Duration::from_secs(6);
const MAX_ROWS: usize = 10_000;
/// Queries still running after their caller gave up.  More than this and WMI
/// is evidently stuck: further queries are refused at once rather than
/// stacking up threads behind it.
const MAX_STUCK: usize = 4;
static RUNNING: AtomicUsize = AtomicUsize::new(0);

/// VARIANT on 64-bit Windows: a type tag and 16 bytes of value.
#[repr(C)]
struct Variant {
    vt: u16,
    reserved: [u16; 3],
    value: [u64; 2],
}

#[link(name = "ole32")]
extern "system" {
    fn CoInitializeEx(reserved: Ptr, model: u32) -> Hresult;
    fn CoUninitialize();
    fn CoCreateInstance(class: *const Guid, outer: Ptr, context: u32, interface: *const Guid, out: *mut Ptr) -> Hresult;
    fn CoSetProxyBlanket(
        proxy: Ptr,
        authn_service: u32,
        authz_service: u32,
        server_principal: *const u16,
        authn_level: u32,
        impersonation_level: u32,
        auth_info: Ptr,
        capabilities: u32,
    ) -> Hresult;
}

#[link(name = "oleaut32")]
extern "system" {
    fn SysAllocStringLen(text: *const u16, length: u32) -> *mut u16;
    fn SysFreeString(text: *mut u16);
    fn SysStringLen(text: *const u16) -> u32;
    fn VariantClear(value: *mut Variant) -> Hresult;
    fn SafeArrayGetLBound(array: Ptr, dimension: u32, bound: *mut i32) -> Hresult;
    fn SafeArrayGetUBound(array: Ptr, dimension: u32, bound: *mut i32) -> Hresult;
    fn SafeArrayGetElement(array: Ptr, index: *const i32, out: Ptr) -> Hresult;
}

// Positions in the method tables (after IUnknown's QueryInterface, AddRef, Release).
const RELEASE: usize = 2;
const LOCATOR_CONNECT_SERVER: usize = 3;
const SERVICES_EXEC_QUERY: usize = 20;
const ENUM_NEXT: usize = 4;
const OBJECT_BEGIN_ENUMERATION: usize = 8;
const OBJECT_NEXT: usize = 9;
const OBJECT_END_ENUMERATION: usize = 10;

type ReleaseFn = unsafe extern "system" fn(Ptr) -> u32;
type ConnectServerFn = unsafe extern "system" fn(
    Ptr,
    *const u16,
    *const u16,
    *const u16,
    *const u16,
    i32,
    *const u16,
    Ptr,
    *mut Ptr,
) -> Hresult;
type ExecQueryFn = unsafe extern "system" fn(Ptr, *const u16, *const u16, i32, Ptr, *mut Ptr) -> Hresult;
type EnumNextFn = unsafe extern "system" fn(Ptr, i32, u32, *mut Ptr, *mut u32) -> Hresult;
type BeginEnumerationFn = unsafe extern "system" fn(Ptr, i32) -> Hresult;
type PropertyNextFn = unsafe extern "system" fn(Ptr, i32, *mut *mut u16, *mut Variant, *mut i32, *mut i32) -> Hresult;
type EndEnumerationFn = unsafe extern "system" fn(Ptr) -> Hresult;

/// The function at `index` of a COM object's method table.
///
/// # Safety
/// `object` must be a live COM interface pointer whose table has that entry,
/// and `T` the function pointer type of that entry.
unsafe fn method<T: Copy>(object: Ptr, index: usize) -> T {
    debug_assert_eq!(std::mem::size_of::<T>(), std::mem::size_of::<Ptr>());
    let table = *(object as *const *const Ptr);
    std::mem::transmute_copy::<Ptr, T>(&*table.add(index))
}

/// A COM interface pointer, released when dropped.
struct Com(Ptr);

impl Drop for Com {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: the pointer is a live interface this wrapper owns one reference to.
            unsafe { method::<ReleaseFn>(self.0, RELEASE)(self.0) };
        }
    }
}

/// COM on this thread, shut down when dropped.  Declared before every `Com`
/// in `run`, so it is dropped after all of them.
struct ComOnThisThread;

impl Drop for ComOnThisThread {
    fn drop(&mut self) {
        // SAFETY: pairs with the successful CoInitializeEx in `run`.
        unsafe { CoUninitialize() };
    }
}

/// A BSTR, freed when dropped.
struct Bstr(*mut u16);

impl Bstr {
    fn new(text: &str) -> Result<Bstr, String> {
        let wide: Vec<u16> = text.encode_utf16().collect();
        // SAFETY: `wide` holds `wide.len()` UTF-16 units; the call copies them.
        let p = unsafe { SysAllocStringLen(wide.as_ptr(), wide.len() as u32) };
        if p.is_null() {
            Err("out of memory".into())
        } else {
            Ok(Bstr(p))
        }
    }
}

impl Drop for Bstr {
    fn drop(&mut self) {
        // SAFETY: allocated by SysAllocStringLen above.
        unsafe { SysFreeString(self.0) };
    }
}

/// # Safety
/// `text` must be null or a valid BSTR.
unsafe fn bstr_to_string(text: *const u16) -> String {
    if text.is_null() {
        return String::new();
    }
    let len = SysStringLen(text) as usize;
    String::from_utf16_lossy(std::slice::from_raw_parts(text, len))
}

/// `q: wmi` - the rows of `query` in `namespace`, each a JSON object.
pub fn query(namespace: &str, query: &str) -> Reply {
    let failed = |why: String| Reply { data: Json::Array(vec![]), note: Some(why) };
    if !is_select(query) {
        return failed("only SELECT queries are run".into());
    }
    if RUNNING.load(Ordering::SeqCst) >= MAX_STUCK {
        return failed("WMI is not answering: earlier queries are still waiting for it".into());
    }
    let (namespace, query) = (namespace.replace('/', "\\"), query.to_string());
    let (send, receive) = mpsc::channel();
    // COM is set up per thread, and the calling thread belongs to the app
    // (Python, a window toolkit), which may have set it up differently.  A
    // thread of our own keeps the two apart, and lets the caller stop waiting
    // for a WMI provider that hangs.
    RUNNING.fetch_add(1, Ordering::SeqCst);
    let worker = std::thread::Builder::new().name("vispora-native-wmi".into()).spawn(move || {
        let outcome = std::panic::catch_unwind(|| run(&namespace, &query));
        RUNNING.fetch_sub(1, Ordering::SeqCst);
        let _ = send.send(outcome.unwrap_or_else(|_| Err("the WMI query hit an internal error".into())));
    });
    if let Err(e) = worker {
        RUNNING.fetch_sub(1, Ordering::SeqCst);
        return failed(format!("could not start a thread for the WMI query: {e}"));
    }
    match receive.recv_timeout(QUERY_TIMEOUT) {
        Ok(Ok(rows)) => Reply { data: Json::Array(rows), note: None },
        Ok(Err(why)) => failed(why),
        Err(_) => failed(format!("WMI did not answer within {} seconds", QUERY_TIMEOUT.as_secs())),
    }
}

fn is_select(query: &str) -> bool {
    query.trim_start().get(..7).is_some_and(|head| head.eq_ignore_ascii_case("select "))
}

fn failure(what: &str, hr: Hresult) -> String {
    format!("{what} failed (0x{:08X})", hr as u32)
}

fn run(namespace: &str, query: &str) -> Result<Vec<Json>, String> {
    // SAFETY: every pointer handed to COM below is either null where the
    // interface allows it or points at a live local; every interface pointer
    // received is wrapped in `Com` at once, so it is released on every path.
    unsafe {
        let hr = CoInitializeEx(null_mut(), COINIT_MULTITHREADED);
        if hr < 0 {
            return Err(failure("starting COM", hr));
        }
        let _com = ComOnThisThread;

        let mut raw: Ptr = null_mut();
        let hr = CoCreateInstance(&CLSID_WBEM_LOCATOR, null_mut(), CLSCTX_INPROC_SERVER, &IID_IWBEM_LOCATOR, &mut raw);
        if hr < 0 || raw.is_null() {
            return Err(failure("creating the WMI locator", hr));
        }
        let locator = Com(raw);

        let resource = Bstr::new(namespace)?;
        let mut raw: Ptr = null_mut();
        let hr = method::<ConnectServerFn>(locator.0, LOCATOR_CONNECT_SERVER)(
            locator.0,
            resource.0,
            null(),
            null(),
            null(),
            WBEM_FLAG_CONNECT_USE_MAX_WAIT,
            null(),
            null_mut(),
            &mut raw,
        );
        if hr < 0 || raw.is_null() {
            return Err(failure(&format!("connecting to {namespace}"), hr));
        }
        let services = Com(raw);

        // Let the WMI service act as the caller when it runs the query.
        let hr = CoSetProxyBlanket(
            services.0,
            RPC_C_AUTHN_WINNT,
            RPC_C_AUTHZ_NONE,
            null(),
            RPC_C_AUTHN_LEVEL_CALL,
            RPC_C_IMP_LEVEL_IMPERSONATE,
            null_mut(),
            EOAC_NONE,
        );
        if hr < 0 {
            return Err(failure("setting the WMI connection's security", hr));
        }

        let (language, text) = (Bstr::new("WQL")?, Bstr::new(query)?);
        let mut raw: Ptr = null_mut();
        let hr = method::<ExecQueryFn>(services.0, SERVICES_EXEC_QUERY)(
            services.0,
            language.0,
            text.0,
            WBEM_FLAG_FORWARD_ONLY | WBEM_FLAG_RETURN_IMMEDIATELY,
            null_mut(),
            &mut raw,
        );
        if hr < 0 || raw.is_null() {
            return Err(failure(query, hr));
        }
        let rows_source = Com(raw);

        let mut rows = Vec::new();
        while rows.len() < MAX_ROWS {
            let mut raw: Ptr = null_mut();
            let mut returned: u32 = 0;
            let hr = method::<EnumNextFn>(rows_source.0, ENUM_NEXT)(rows_source.0, ROW_TIMEOUT_MS, 1, &mut raw, &mut returned);
            if hr < 0 {
                return Err(failure(query, hr));
            }
            if hr == WBEM_S_TIMEDOUT {
                return Err(format!("{query}: WMI took longer than {ROW_TIMEOUT_MS} ms for a row"));
            }
            if hr == WBEM_S_FALSE || returned == 0 || raw.is_null() {
                break;
            }
            let object = Com(raw);
            rows.push(row_to_json(object.0));
        }
        Ok(rows)
    }
}

/// Every property of one WMI object that is its own (not the `__CLASS` kind).
///
/// # Safety
/// `object` must be a live IWbemClassObject.
unsafe fn row_to_json(object: Ptr) -> Json {
    let mut pairs = Vec::new();
    if method::<BeginEnumerationFn>(object, OBJECT_BEGIN_ENUMERATION)(object, WBEM_FLAG_NONSYSTEM_ONLY) < 0 {
        return Json::Object(pairs);
    }
    loop {
        let mut name: *mut u16 = null_mut();
        let mut value = Variant { vt: 0, reserved: [0; 3], value: [0; 2] };
        let mut cim_type: i32 = 0;
        let hr = method::<PropertyNextFn>(object, OBJECT_NEXT)(object, 0, &mut name, &mut value, &mut cim_type, null_mut());
        if hr != 0 {
            // WBEM_S_NO_MORE_DATA at the end; anything else ends the row too.
            debug_assert!(hr == WBEM_S_NO_MORE_DATA || hr < 0);
            break;
        }
        pairs.push((bstr_to_string(name), variant_to_json(&value, cim_type)));
        SysFreeString(name);
        VariantClear(&mut value);
    }
    method::<EndEnumerationFn>(object, OBJECT_END_ENUMERATION)(object);
    Json::Object(pairs)
}

/// A WMI value as JSON: numbers as numbers (a NaN, which a sensor without a
/// reading reports, as null), text as text, lists as lists, anything else
/// (dates stay text; embedded objects) as null.
///
/// # Safety
/// `v` must be a VARIANT as WMI returned it (its pointers, if any, valid).
unsafe fn variant_to_json(v: &Variant, cim_type: i32) -> Json {
    let low = v.value[0];
    if v.vt & VT_ARRAY != 0 {
        return array_to_json(low as Ptr, v.vt & 0x0FFF);
    }
    match v.vt {
        2 => (low as u16 as i16).into(),
        3 => (low as u32 as i32).into(),
        4 => single(f32::from_bits(low as u32)),
        5 => Json::from(f64::from_bits(low)),
        8 => {
            let text = bstr_to_string(low as *const u16);
            // WMI hands 64-bit integers over as text.
            match cim_type {
                CIM_UINT64 => text.trim().parse::<u64>().map(Json::from).unwrap_or(Json::Str(text)),
                CIM_SINT64 => text.trim().parse::<i64>().map(Json::from).unwrap_or(Json::Str(text)),
                _ => Json::Str(text),
            }
        }
        11 => Json::Bool(low as u16 != 0),
        16 => (low as u8 as i8).into(),
        17 => (low as u8).into(),
        18 => (low as u16).into(),
        19 => (low as u32).into(),
        20 => (low as i64).into(),
        21 => low.into(),
        _ => Json::Null, // VT_EMPTY, VT_NULL, and what a sensor table never holds
    }
}

/// A 32-bit float as the number its shortest decimal text names (45.1, not
/// 45.099998474121094), which is also how PowerShell printed it.
fn single(f: f32) -> Json {
    if !f.is_finite() {
        return Json::Null;
    }
    f.to_string().parse::<f64>().map(Json::from).unwrap_or(Json::Null)
}

/// # Safety
/// `array` must be null or a one-dimensional SAFEARRAY of `element` values.
unsafe fn array_to_json(array: Ptr, element: u16) -> Json {
    if array.is_null() {
        return Json::Null;
    }
    let (mut first, mut last) = (0i32, -1i32);
    if SafeArrayGetLBound(array, 1, &mut first) < 0 || SafeArrayGetUBound(array, 1, &mut last) < 0 {
        return Json::Null;
    }
    let mut items = Vec::new();
    for index in first..=last.min(first.saturating_add(4095)) {
        let item = match element {
            8 => {
                let mut text: *mut u16 = null_mut();
                if SafeArrayGetElement(array, &index, &mut text as *mut *mut u16 as Ptr) >= 0 {
                    let s = bstr_to_string(text);
                    SysFreeString(text); // (the call handed out a copy)
                    Json::Str(s)
                } else {
                    Json::Null
                }
            }
            2..=5 | 11 | 16..=21 => {
                let mut raw = [0u64; 2];
                if SafeArrayGetElement(array, &index, raw.as_mut_ptr() as Ptr) >= 0 {
                    variant_to_json(&Variant { vt: element, reserved: [0; 3], value: raw }, 0)
                } else {
                    Json::Null
                }
            }
            _ => Json::Null,
        };
        items.push(item);
    }
    Json::Array(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(vt: u16, low: u64) -> Json {
        // SAFETY: none of the types used in these tests holds a pointer.
        unsafe { variant_to_json(&Variant { vt, reserved: [0; 3], value: [low, 0] }, 0) }
    }

    #[test]
    fn the_variant_is_24_bytes_as_windows_lays_it_out() {
        assert_eq!(std::mem::size_of::<Variant>(), 24);
        assert_eq!(std::mem::size_of::<Guid>(), 16);
    }

    #[test]
    fn values_become_plain_json() {
        assert_eq!(plain(4, 52.375f32.to_bits() as u64), Json::Float(52.375));
        assert_eq!(plain(4, 45.1f32.to_bits() as u64), Json::Float(45.1), "not 45.099998474121094");
        assert_eq!(plain(4, f32::NAN.to_bits() as u64), Json::Null);
        assert_eq!(plain(5, f64::INFINITY.to_bits()), Json::Null);
        assert_eq!(plain(5, 61.5f64.to_bits()), Json::Float(61.5));
        assert_eq!(plain(3, (-7i32) as u32 as u64), Json::Int(-7));
        assert_eq!(plain(19, 3_000_000_000), Json::UInt(3_000_000_000));
        assert_eq!(plain(2, (-2i16) as u16 as u64), Json::Int(-2));
        assert_eq!(plain(11, 0xFFFF), Json::Bool(true));
        assert_eq!(plain(11, 0), Json::Bool(false));
        assert_eq!(plain(17, 200), Json::UInt(200));
        assert_eq!(plain(21, u64::MAX), Json::UInt(u64::MAX));
        assert_eq!(plain(0, 5), Json::Null);
        assert_eq!(plain(1, 5), Json::Null);
        assert_eq!(plain(13, 5), Json::Null, "an embedded object is not followed");
    }

    #[test]
    fn only_select_is_run() {
        assert!(is_select("SELECT Name FROM Sensor"));
        assert!(is_select("  select * from Win32_Processor"));
        for other in ["", "DELETE FROM X", "selectx", "ASSOCIATORS OF {Win32_Service.Name='x'}"] {
            assert!(!is_select(other), "{other}");
        }
        let refused = query("root\\cimv2", "DELETE FROM Win32_Process");
        assert_eq!(refused.data, Json::Array(vec![]));
        assert!(refused.note.unwrap().contains("SELECT"));
    }

    #[test]
    fn windows_answers_a_query() {
        let os = query("root\\cimv2", "SELECT Caption, BuildNumber, TotalVisibleMemorySize FROM Win32_OperatingSystem");
        let rows = os.data.as_array().unwrap();
        assert_eq!(rows.len(), 1, "{:?}", os.note);
        assert!(rows[0]["Caption"].as_str().unwrap().contains("Windows"));
        assert!(rows[0]["BuildNumber"].as_str().unwrap().parse::<u32>().unwrap() > 7000);
        // A 64-bit integer, which WMI passes as text: a number here.
        assert!(rows[0]["TotalVisibleMemorySize"].as_u64().unwrap() > 1_000_000);
        // Only what was asked for (and none of WMI's own __ properties).
        assert!(matches!(&rows[0], Json::Object(pairs) if pairs.len() == 3 && pairs.iter().all(|(k, _)| !k.starts_with("__"))));
    }

    #[test]
    fn forward_slashes_numbers_and_lists_come_through() {
        // Forward slashes, as the app's PowerShell commands wrote the namespace.
        let cpu = query("root/cimv2", "SELECT Name, NumberOfLogicalProcessors, CurrentClockSpeed FROM Win32_Processor");
        let rows = cpu.data.as_array().unwrap();
        assert!(!rows.is_empty(), "{:?}", cpu.note);
        assert!(rows[0]["NumberOfLogicalProcessors"].as_u64().unwrap() >= 1);
        assert!(rows[0]["CurrentClockSpeed"].as_u64().unwrap() > 100);
        assert!(!rows[0]["Name"].as_str().unwrap().is_empty());
        // A property that is a list of strings (or null on an adapter without an address).
        let net = query("root\\cimv2", "SELECT IPAddress FROM Win32_NetworkAdapterConfiguration");
        let rows = net.data.as_array().unwrap();
        assert!(!rows.is_empty(), "{:?}", net.note);
        for row in rows {
            match &row["IPAddress"] {
                Json::Null => {}
                Json::Array(items) => assert!(items.iter().all(|i| i.as_str().is_some()), "{row}"),
                other => panic!("neither a list nor null: {other}"),
            }
        }
    }

    #[test]
    fn what_wmi_refuses_is_an_empty_list_with_the_reason() {
        let missing = query("root\\NoSuchNamespaceHere", "SELECT * FROM Sensor");
        assert_eq!(missing.data, Json::Array(vec![]));
        assert!(missing.note.unwrap().contains("NoSuchNamespaceHere"));
        let no_class = query("root\\cimv2", "SELECT * FROM NoSuchClassHere");
        assert_eq!(no_class.data, Json::Array(vec![]));
        assert!(no_class.note.is_some());
        let none = query("root\\cimv2", "SELECT Name FROM Win32_Process WHERE ProcessId = 4294967295");
        assert_eq!(none.data, Json::Array(vec![]));
        assert!(none.note.is_none(), "a query that simply matches nothing is not a failure");
        assert_eq!(RUNNING.load(Ordering::SeqCst), 0, "no query is left running");
    }
}
