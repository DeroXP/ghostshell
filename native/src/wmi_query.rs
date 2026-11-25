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

use std::collections::HashMap;

use serde_json::{Map, Number, Value};
use wmi::{COMLibrary, Variant, WMIConnection};

use crate::Reply;

/// `q: wmi` - the rows of `query` in `namespace`, each a JSON object.
pub fn query(namespace: &str, query: &str) -> Reply {
    if !is_select(query) {
        return Reply { data: Value::Array(vec![]), note: Some("only SELECT queries are run".into()) };
    }
    let (namespace, query) = (namespace.replace('/', "\\"), query.to_string());
    // COM is set up per thread, and the calling thread belongs to the app
    // (Python, a window toolkit), which may have set it up differently.  A
    // thread of our own keeps the two apart.
    let worker = std::thread::Builder::new()
        .name("vispora-native-wmi".into())
        .spawn(move || run(&namespace, &query));
    let outcome = match worker {
        Ok(handle) => handle.join().unwrap_or_else(|_| Err("the WMI query panicked".into())),
        Err(e) => Err(format!("could not start a thread for the WMI query: {e}")),
    };
    match outcome {
        Ok(rows) => Reply { data: Value::Array(rows), note: None },
        Err(why) => Reply { data: Value::Array(vec![]), note: Some(why) },
    }
}

fn is_select(query: &str) -> bool {
    query.trim_start().get(..7).is_some_and(|head| head.eq_ignore_ascii_case("select "))
}

fn run(namespace: &str, query: &str) -> Result<Vec<Value>, String> {
    let com = COMLibrary::new().map_err(|e| format!("COM: {e}"))?;
    let connection = WMIConnection::with_namespace_path(namespace, com).map_err(|e| format!("{namespace}: {e}"))?;
    let rows: Vec<HashMap<String, Variant>> = connection.raw_query(query).map_err(|e| format!("{query}: {e}"))?;
    Ok(rows.into_iter().map(row_to_json).collect())
}

fn row_to_json(row: HashMap<String, Variant>) -> Value {
    let mut object = Map::with_capacity(row.len());
    for (name, value) in row {
        object.insert(name, variant_to_json(value));
    }
    Value::Object(object)
}

/// A WMI value as JSON: numbers as numbers (a NaN, which a sensor without a
/// reading reports, as null), text as text, lists as lists, anything else
/// (embedded objects) as null.
fn variant_to_json(v: Variant) -> Value {
    match v {
        Variant::Empty | Variant::Null => Value::Null,
        Variant::String(s) => Value::String(s),
        Variant::Bool(b) => Value::Bool(b),
        Variant::I1(n) => Value::from(n),
        Variant::I2(n) => Value::from(n),
        Variant::I4(n) => Value::from(n),
        Variant::I8(n) => Value::from(n),
        Variant::UI1(n) => Value::from(n),
        Variant::UI2(n) => Value::from(n),
        Variant::UI4(n) => Value::from(n),
        Variant::UI8(n) => Value::from(n),
        Variant::R4(f) => float(f as f64),
        Variant::R8(f) => float(f),
        Variant::Array(items) => Value::Array(items.into_iter().map(variant_to_json).collect()),
        _ => Value::Null,
    }
}

fn float(f: f64) -> Value {
    Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_become_plain_json() {
        assert_eq!(variant_to_json(Variant::R4(52.375)), serde_json::json!(52.375));
        assert_eq!(variant_to_json(Variant::R4(f32::NAN)), Value::Null);
        assert_eq!(variant_to_json(Variant::R8(f64::INFINITY)), Value::Null);
        assert_eq!(variant_to_json(Variant::UI8(17_171_480_576)), serde_json::json!(17_171_480_576u64));
        assert_eq!(variant_to_json(Variant::String("CPU Package".into())), serde_json::json!("CPU Package"));
        assert_eq!(variant_to_json(Variant::Null), Value::Null);
        assert_eq!(
            variant_to_json(Variant::Array(vec![Variant::String("1.1.1.1".into()), Variant::String("8.8.8.8".into())])),
            serde_json::json!(["1.1.1.1", "8.8.8.8"])
        );
    }

    #[test]
    fn only_select_is_run() {
        assert!(is_select("SELECT Name FROM Sensor"));
        assert!(is_select("  select * from Win32_Processor"));
        for other in ["", "DELETE FROM X", "selectx", "ASSOCIATORS OF {Win32_Service.Name='x'}"] {
            assert!(!is_select(other), "{other}");
        }
        let refused = query("root\\cimv2", "DELETE FROM Win32_Process");
        assert_eq!(refused.data, Value::Array(vec![]));
        assert!(refused.note.unwrap().contains("SELECT"));
    }

    #[test]
    fn windows_answers_a_query_and_a_missing_namespace_is_an_empty_list() {
        let os = query("root\\cimv2", "SELECT Caption, BuildNumber FROM Win32_OperatingSystem");
        let rows = os.data.as_array().unwrap();
        assert_eq!(rows.len(), 1, "{:?}", os.note);
        assert!(rows[0]["Caption"].as_str().unwrap().contains("Windows"));
        // Forward slashes, as the app's PowerShell commands wrote the namespace.
        let again = query("root/cimv2", "SELECT Name FROM Win32_Processor");
        assert!(!again.data.as_array().unwrap().is_empty(), "{:?}", again.note);
        let missing = query("root\\NoSuchNamespaceHere", "SELECT * FROM Sensor");
        assert_eq!(missing.data, Value::Array(vec![]));
        assert!(missing.note.is_some());
    }
}
