//! vispora-native.exe: ask the helper a question from a terminal.
//!
//!     vispora-native windowed_processes
//!     vispora-native gpu_live
//!     vispora-native ping target=1.1.1.1 timeout_ms=500
//!     vispora-native wmi namespace=root\cimv2 "query=SELECT Name FROM Win32_Processor"
//!
//! Prints the same JSON the app receives.  The app itself does not use this
//! program (it loads the DLL); it is for checking an answer by hand and for
//! native/check_parity.py, which compares each answer with what PowerShell,
//! nvidia-smi and ping say on the same machine.

use serde_json::{Map, Value};

fn main() {
    let mut args = std::env::args().skip(1);
    let Some(question) = args.next() else {
        eprintln!(
            "usage: vispora-native <question> [name=value ...]\n\
             questions: abi, windowed_processes, process_io, gpu_live, gpu_release, cpu_clock, cpu_times, ping, wmi"
        );
        std::process::exit(2);
    };
    let mut request = Map::new();
    for arg in args {
        let Some((name, value)) = arg.split_once('=') else {
            eprintln!("not name=value: {arg}");
            std::process::exit(2);
        };
        // A whole number is sent as a number, anything else as text.
        let value = value.parse::<u64>().map(Value::from).unwrap_or_else(|_| Value::from(value));
        request.insert(name.to_string(), value);
    }
    request.insert("q".to_string(), Value::from(question));
    let reply = vispora_native::answer(&Value::Object(request).to_string());
    println!("{reply}");
    let ok = serde_json::from_str::<Value>(&reply).map(|v| v["ok"] == true).unwrap_or(false);
    std::process::exit(if ok { 0 } else { 1 });
}
