//! The command line, and what the updater makes of it and of the app's
//! install record before anything is shown.  A port of updater/main.py up to
//! the point where the window opens.
//!
//!     VisporaUpdater.exe
//!         [--target-dir <path>]         Vispora install directory
//!         [--target-exe <path>]         explicit path to the app exe
//!         [--channel stable|beta]
//!         [--current-version <v>]
//!         [--target-version <v>]        version to install (the newest if omitted)
//!         [--auto-confirm]              skip the "click to start" gate
//!         [--server <url>]              the update server's address
//!         [--source auto|github|server] where to look for the build: GitHub
//!                                       first and the update server second
//!                                       (auto), or one of them only
//!         [--silent]                    no window: install and exit
//!
//! The same arguments the Python updater took, so that the app
//! (core/updater.py) can start either.

use std::sync::Arc;

use crate::installer::Updater;
use crate::paths;
use crate::sources::{self, SERVER_URL_DEFAULT};
use crate::state::State;

/// The release server's previous host name, dead since the service was
/// renamed on 2026-07-01.  Kept only so that a stale --server from an old
/// install is recognised and pointed at the live server.
pub const DEAD_SERVER_HOST: &str = "ghostshell-site.up.railway.app";

#[derive(Debug, Clone, PartialEq)]
pub struct Args {
    pub target_dir: String,
    pub target_exe: String,
    pub channel: String,
    pub current_version: String,
    pub target_version: String,
    pub auto_confirm: bool,
    pub server: String,
    pub source: String,
    pub silent: bool,
}

impl Default for Args {
    fn default() -> Args {
        Args {
            target_dir: String::new(),
            target_exe: String::new(),
            channel: String::new(),
            current_version: String::new(),
            target_version: String::new(),
            auto_confirm: false,
            server: SERVER_URL_DEFAULT.to_string(),
            source: String::new(),
            silent: false,
        }
    }
}

/// Why the arguments were not taken, and the exit code that goes with it
/// (2 for a mistake, 0 for --help), as Python's argparse had it.
#[derive(Debug, Clone, PartialEq)]
pub struct ArgsError {
    pub exit_code: i32,
    pub message: String,
}

const VALUE_OPTIONS: [&str; 7] =
    ["--target-dir", "--target-exe", "--channel", "--current-version", "--target-version", "--server", "--source"];
const FLAG_OPTIONS: [&str; 2] = ["--auto-confirm", "--silent"];
const USAGE: &str = "usage: VisporaUpdater [-h] [--target-dir TARGET_DIR] [--target-exe TARGET_EXE] \
                     [--channel {,stable,beta}] [--current-version CURRENT_VERSION] \
                     [--target-version TARGET_VERSION] [--auto-confirm] [--server SERVER] \
                     [--source {,auto,github,server}] [--silent]";

fn mistake(message: String) -> ArgsError {
    ArgsError { exit_code: 2, message: format!("{USAGE}\nVisporaUpdater: error: {message}") }
}

/// The option `given` names: itself, or the one option it is the beginning
/// of (argparse takes "--chan" for "--channel").
fn option_named(given: &str) -> Result<&'static str, ArgsError> {
    let all = VALUE_OPTIONS.iter().chain(FLAG_OPTIONS.iter()).copied();
    if let Some(exact) = all.clone().find(|o| *o == given) {
        return Ok(exact);
    }
    let starting: Vec<&'static str> = all.filter(|o| o.starts_with(given)).collect();
    match starting.as_slice() {
        [one] => Ok(one),
        [] => Err(mistake(format!("unrecognized arguments: {given}"))),
        several => Err(mistake(format!("ambiguous option: {given} could match {}", several.join(", ")))),
    }
}

/// Read the command line (without the program's own name).
pub fn parse_args(argv: &[String]) -> Result<Args, ArgsError> {
    let mut args = Args::default();
    let mut i = 0;
    while i < argv.len() {
        let token = argv[i].as_str();
        i += 1;
        if token == "-h" || token == "--help" {
            return Err(ArgsError { exit_code: 0, message: USAGE.to_string() });
        }
        if !token.starts_with("--") || token == "--" {
            return Err(mistake(format!("unrecognized arguments: {token}")));
        }
        let (name, attached) = match token.split_once('=') {
            Some((name, value)) => (name, Some(value.to_string())),
            None => (token, None),
        };
        let option = option_named(name)?;
        if FLAG_OPTIONS.contains(&option) {
            if attached.is_some() {
                return Err(mistake(format!("argument {option}: ignored explicit argument")));
            }
            match option {
                "--auto-confirm" => args.auto_confirm = true,
                _ => args.silent = true,
            }
            continue;
        }
        let value = match attached {
            Some(value) => value,
            // (the next word, unless it is an option itself)
            None if i < argv.len() && !(argv[i].starts_with('-') && argv[i].len() > 1) => {
                i += 1;
                argv[i - 1].clone()
            }
            None => return Err(mistake(format!("argument {option}: expected one argument"))),
        };
        let choices: &[&str] = match option {
            "--channel" => &["", "stable", "beta"],
            "--source" => &["", "auto", "github", "server"],
            _ => &[],
        };
        if !choices.is_empty() && !choices.contains(&value.as_str()) {
            let listed: Vec<String> = choices.iter().map(|c| format!("'{c}'")).collect();
            return Err(mistake(format!("argument {option}: invalid choice: '{value}' (choose from {})", listed.join(", "))));
        }
        match option {
            "--target-dir" => args.target_dir = value,
            "--target-exe" => args.target_exe = value,
            "--channel" => args.channel = value,
            "--current-version" => args.current_version = value,
            "--target-version" => args.target_version = value,
            "--server" => args.server = value,
            _ => args.source = value,
        }
    }
    Ok(args)
}

/// Put the command line and the app's install record together and seed the
/// updater's state with the result.  The command line wins where both say
/// something.  Without a target from either, the PC is looked over for an
/// install: found, it is updated; not found, this is a fresh install into a
/// suggested folder (which the window lets the user change).
pub fn prepare(updater: &Arc<Updater>, args: &Args, updater_version: &str) {
    updater.log("───────────────────────────────");
    updater.log(&format!("VisporaUpdater v{updater_version} invoked"));
    updater.log(&format!("  args: {args:?}"));
    let info = updater.load_install_info();
    updater.log(&format!("  install_info: {info}"));
    let recorded = |key: &str| info[key].as_str().unwrap_or("").to_string();
    let or = |given: &str, key: &str| if given.is_empty() { recorded(key) } else { given.to_string() };

    let mut target_dir = or(&args.target_dir, "install_dir");
    let mut target_exe = or(&args.target_exe, "install_path");
    let mut channel = or(&args.channel, "channel");
    let mut current_version = or(&args.current_version, "version");
    let given_server = if args.server.is_empty() { SERVER_URL_DEFAULT } else { &args.server };
    let mut server_url = given_server.trim_end_matches('/').to_string();
    // Where to look: what the app said on the command line, else what it
    // wrote down for an updater that is started by hand, else GitHub first.
    let source = sources::source_setting(&or(&args.source, "update_source"));
    // A channel named on the command line that is not the install's own was
    // picked for this run: then an older build than the installed one is
    // what was asked for (see Updater::find_release).
    let allow_older = !args.channel.is_empty() && info["channel"].truthy() && info["channel"].as_str() != Some(&args.channel);
    if server_url.contains(DEAD_SERVER_HOST) {
        updater.log(&format!("  migrating dead server URL {server_url:?} -> {SERVER_URL_DEFAULT:?}"));
        server_url = SERVER_URL_DEFAULT.to_string();
    }

    if !target_exe.is_empty() && target_dir.is_empty() {
        target_dir = paths::dirname(&target_exe);
    }
    // A folder without an exe named: whichever app exe is already in it.
    if !target_dir.is_empty() && target_exe.is_empty() {
        target_exe = updater.pick_app_exe(&target_dir);
    }

    let mut mode = "update";
    let mut scan_result = crate::json::Json::Null;
    if target_exe.is_empty() {
        let found = updater.find_existing_install();
        if found["found"].truthy() {
            target_exe = found["path"].py_str();
            target_dir = found["dir"].py_str();
            if channel.is_empty() {
                channel = found["channel"].py_str_or_empty();
            }
            if current_version.is_empty() {
                current_version = found["version"].py_str_or_empty();
            }
            updater.log(&format!("auto-scan: found existing install at {target_exe}"));
        } else {
            mode = "install";
            target_dir = updater.default_install_dir();
            target_exe = updater.pick_app_exe(&target_dir);
            updater.log(&format!("auto-scan: no existing install; install mode, default dir {target_dir}"));
        }
        scan_result = found;
    } else {
        updater.log(&format!("using explicit target from args: {target_exe}"));
    }

    updater.init_state(State {
        target_dir,
        target_exe,
        channel,
        current_version,
        target_version: args.target_version.clone(),
        // What the app asked for.  target_version is replaced by what is
        // found; this stays, so that a place that is behind is not installed
        // from.
        requested_version: args.target_version.clone(),
        source: source.to_string(),
        allow_older,
        server_url,
        auto_confirm: args.auto_confirm,
        silent: args.silent,
        updater_version: updater_version.to_string(),
        mode: mode.to_string(),
        scan_result,
        ..State::default()
    });
}

/// --silent: no window, install now, and the pipeline's exit code.
pub fn run_silent(updater: &Arc<Updater>) -> i32 {
    updater.log("running in --silent mode");
    let code = updater.run_install_pipeline();
    updater.log(&format!("silent install exit: {code}"));
    code
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(words: &[&str]) -> Result<Args, ArgsError> {
        parse_args(&words.iter().map(|w| w.to_string()).collect::<Vec<_>>())
    }

    #[test]
    fn command_line_defaults() {
        let args = parse(&[]).unwrap();
        assert_eq!(args, Args::default());
        assert_eq!(args.server, SERVER_URL_DEFAULT);
        assert!(args.channel.is_empty() && !args.silent && !args.auto_confirm);
        assert_eq!(args.source, "", "not said: the app's record, or GitHub first");
        let args = parse(&["--auto-confirm", "--channel", "beta"]).unwrap();
        assert!(args.auto_confirm && args.channel == "beta");
    }

    #[test]
    fn everything_the_app_passes_is_taken() {
        // core/updater.py: the hand-off at install time.
        let args = parse(&["--target-exe", "C:\\Program Files\\Vispora\\Vispora.exe", "--target-dir",
                           "C:\\Program Files\\Vispora", "--channel", "stable", "--current-version", "3.8.1",
                           "--target-version", "3.9.0", "--server", "https://vispora.up.railway.app", "--auto-confirm",
                           "--source", "github"])
        .unwrap();
        assert_eq!(args.target_exe, "C:\\Program Files\\Vispora\\Vispora.exe");
        assert_eq!(args.target_dir, "C:\\Program Files\\Vispora");
        assert_eq!((args.channel.as_str(), args.current_version.as_str(), args.target_version.as_str()),
                   ("stable", "3.8.1", "3.9.0"));
        assert!(args.auto_confirm && !args.silent && args.source == "github");
    }

    #[test]
    fn it_reads_the_forms_argparse_reads() {
        let args = parse(&["--channel=beta", "--target-dir=D:\\Games\\Vispora", "--silent"]).unwrap();
        assert_eq!((args.channel.as_str(), args.target_dir.as_str(), args.silent), ("beta", "D:\\Games\\Vispora", true));
        // The beginning of a name is enough when only one option starts so.
        let args = parse(&["--chan", "stable", "--sil", "--so", "server", "--auto"]).unwrap();
        assert_eq!((args.channel.as_str(), args.silent, args.source.as_str(), args.auto_confirm), ("stable", true, "server", true));
        assert_eq!(parse(&["--channel", ""]).unwrap().channel, "");
        assert_eq!(parse(&["--target-version", "3.9.0-beta.1", "--source", ""]).unwrap().target_version, "3.9.0-beta.1");
    }

    #[test]
    fn a_mistake_on_the_command_line_ends_with_code_2() {
        for bad in [
            vec!["--source", "railway"],
            vec!["--channel", "nightly"],
            vec!["--no-such-option"],
            vec!["stray"],
            vec!["--channel"],
            vec!["--channel", "--silent"],
            vec!["--silent=yes"],
            vec!["--target", "x"],          // --target-dir, --target-exe or --target-version?
            vec!["--s"],                    // --server, --source or --silent?
            vec!["-x"],
            vec!["--"],
        ] {
            let err = parse(&bad).unwrap_err();
            assert_eq!(err.exit_code, 2, "{bad:?}");
            assert!(err.message.contains("VisporaUpdater: error:"), "{bad:?}: {}", err.message);
        }
        assert!(parse(&["--source", "railway"]).unwrap_err().message.contains("invalid choice: 'railway'"));
        assert_eq!(parse(&["--help"]).unwrap_err().exit_code, 0);
        assert_eq!(parse(&["-h"]).unwrap_err().exit_code, 0);
    }
}
