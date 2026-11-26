//! vispora-native-ask.exe: ask the helper a question from a terminal.
//!
//!     vispora-native-ask windowed_processes
//!     vispora-native-ask gpu_live
//!     vispora-native-ask ping target=1.1.1.1 timeout_ms=500
//!     vispora-native-ask wmi namespace=root\cimv2 "query=SELECT Name FROM Win32_Processor"
//!
//!     vispora-native-ask service_state name=EventLog
//!     vispora-native-ask power_setting sub=54533251-82be-4824-96c1-47b60b740d00 setting=bc5038f7-23e0-4960-96da-33abaf5935ec
//!
//! Prints the same JSON the app receives.  The app itself does not use this
//! program (it loads the DLL); it is for checking an answer by hand and for
//! comparing it with what PowerShell, nvidia-smi and ping say on the same PC.
//!
//! Questions only.  The library's two actions (they free memory) are not
//! offered here: this tool cannot change anything.

use vispora_native::json::Json;

fn main() {
    let mut args = std::env::args().skip(1);
    let Some(question) = args.next() else {
        eprintln!(
            "usage: vispora-native-ask <question> [name=value ...]\n\
             questions: abi, windowed_processes, process_io, gpu_live, gpu_identity, gpu_release, cpu_clock, \
             cpu_times, memory, dpc_rate, network_cards, ping, wmi, service_state, power_scheme, power_setting"
        );
        std::process::exit(2);
    };
    let mut request = vec![("q".to_string(), Json::from(question))];
    for arg in args {
        let Some((name, value)) = arg.split_once('=') else {
            eprintln!("not name=value: {arg}");
            std::process::exit(2);
        };
        // A whole number is sent as a number, anything else as text.
        let value = value.parse::<u64>().map(Json::from).unwrap_or_else(|_| Json::from(value));
        request.push((name.to_string(), value));
    }
    let reply = vispora_native::answer(&Json::Object(request).to_string());
    println!("{reply}");
    let ok = Json::parse(&reply).map(|v| v["ok"] == Json::Bool(true)).unwrap_or(false);
    std::process::exit(if ok { 0 } else { 1 });
}
