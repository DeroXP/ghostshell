//! The shell's window: what is to become Vispora.exe.
//!
//! Everything the shell decides is in shell_rs/core.  This file is the part
//! that needs a window: it finds out whether it is the one copy, opens the
//! window (Tauri on WebView2) on the app's own page, hands every request of
//! the page to the core, and stays until the app is quit.
//!
//! The page is the app's own and is not changed.  Its requests never leave
//! the process on their way to the shell: there is no server and no port
//! for the page.  Nothing in the page can reach Tauri's own commands: none
//! are registered, and the page is given no permission for the built-in
//! ones.
//!
//! Not here yet: the tray icon (a hidden window is brought back by starting
//! the app again), and the backend packed into the exe.  Nothing in a
//! release uses this program yet.

#![windows_subsystem = "windows"]

use std::io::Write as _;
use std::path::PathBuf;
use std::sync::Arc;

use tauri::http::Response as Answer;
use tauri::utils::config::Color;
use tauri::webview::NewWindowResponse;
use tauri::{WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use vispora_shell_core::http::Request;
use vispora_shell_core::instance::{self, Begin, Held};
use vispora_shell_core::json::Json;
use vispora_shell_core::platform::{Command, Window};
use vispora_shell_core::{page, paths, win, Settings, Shell, APP_NAME};

/// The build that is meant to ship asks for administrator rights; a trial
/// build never does (Cargo.toml).
const ASKS_FOR_RIGHTS: bool = cfg!(feature = "elevated");
/// "vispora" is a name of this program's own: WebView2 shows the page as
/// http://vispora.localhost/ and asks nobody outside for it.
const PAGE: &str = "vispora://localhost/";

/// How a trial build is told what to run with: a stand-in backend, names of
/// its own, and where its window goes.  The build that ships does not read
/// this.
struct Trial {
    tag: String,
    backend: Command,
    window: String,
}

fn trial() -> Option<Trial> {
    if ASKS_FOR_RIGHTS {
        return None;
    }
    let told = Json::parse(&std::env::var("VISPORA_SHELL_TRY").ok()?).ok()?;
    let backend = &told["backend"];
    Some(Trial {
        tag: told["tag"].as_str()?.to_string(),
        backend: Command {
            program: backend["program"].as_str()?.to_string(),
            args: backend["args"].as_array()?.iter().map(Json::py_str).collect(),
            cwd: backend["cwd"].as_str().map(str::to_string),
        },
        window: told["window"].as_str().unwrap_or("hidden").to_string(),
    })
}

/// The window, as the core asks for it.  The second part says that this
/// is a trial whose window stays out of the way: then showing it does not
/// take the keyboard from whoever is at the PC.
struct Pane(WebviewWindow, bool);

impl Window for Pane {
    fn minimize(&self) -> bool {
        self.0.minimize().is_ok()
    }

    fn hide(&self) -> bool {
        // (minimized first, as the Python app does it: WebView2 is left in a
        // state from which a later show works)
        let _ = self.0.minimize();
        self.0.hide().is_ok()
    }

    fn show(&self) -> bool {
        let shown = self.0.show().is_ok();
        let _ = self.0.unminimize();
        if !self.1 {
            let _ = self.0.set_focus();
        }
        shown
    }
}

fn main() {
    // (lossily: an argument that is not valid Unicode is not a reason to stop)
    let arguments: Vec<String> = std::env::args_os().skip(1).map(|word| word.to_string_lossy().into_owned()).collect();
    let from_autostart = arguments.iter().any(|argument| argument == "--from-autostart");
    let trial = trial();
    if !ASKS_FOR_RIGHTS && trial.is_none() {
        win::tell(APP_NAME, "This build of the shell is for trials only and has no backend of its own.\n\nSee shell_rs/app/try_it.py.");
        std::process::exit(2);
    }

    // First of all: is a copy running already?  Before anything asks for
    // administrator rights.
    let names = trial.as_ref().map_or_else(win::Names::of_the_app, |trial| win::Names::for_a_trial(&trial.tag));
    let held = match instance::begin(&win::session(names), ASKS_FOR_RIGHTS, &arguments) {
        Begin::Run(held) => held,
        Begin::Shown | Begin::Running | Begin::Elevating => return,
        Begin::NotElevated(why) => {
            win::tell(APP_NAME, &format!("Vispora needs administrator rights to change Windows' settings, and did not get them.\n\n({why}.)"));
            std::process::exit(1);
        }
    };

    let folders = paths::Paths::resolve(&paths::Env::from_process());
    let backend = match &trial {
        Some(trial) => trial.backend.clone(),
        None => {
            // Where the one-file piece will put it.  Until then the build
            // that ships has nothing to start, and says so.
            let beside = std::env::current_exe().ok().and_then(|exe| exe.parent().map(|folder| folder.join("backend")));
            let program = beside.map(|folder| folder.join("VisporaBackend.exe")).unwrap_or_default();
            if !program.is_file() {
                win::tell(APP_NAME, "Vispora's files are incomplete: its background program is missing.\n\nDownload Vispora again.");
                std::process::exit(1);
            }
            Command { program: program.to_string_lossy().into_owned(), args: Vec::new(), cwd: None }
        }
    };
    let mut backend = backend;
    backend.args.push("--backend".to_string());
    if from_autostart {
        backend.args.push("--from-autostart".to_string());
    }

    let code = match run(Settings::new(backend, &folders.appdata_dir), &folders, held, from_autostart, trial.as_ref()) {
        Ok(code) => code,
        Err(why) => {
            win::tell(APP_NAME, &format!(
                "Vispora's window could not be opened.\n\n\
                 It needs Microsoft Edge WebView2, which is part of Windows 11 and of most Windows 10 PCs. \
                 Installing or repairing \"Microsoft Edge WebView2 Runtime\" usually fixes this.\n\n\
                 Nothing was changed on this PC.\n\nDetails: {why}"
            ));
            1
        }
    };
    std::process::exit(code);
}

/// Open the window, start the backend, and stay until the app is quit.
fn run(settings: Settings, folders: &paths::Paths, held: Option<Box<dyn Held>>, from_autostart: bool, trial: Option<&Trial>)
       -> Result<i32, Box<dyn std::error::Error>> {
    // Asked before anything is opened: where WebView2 is missing, creating
    // the window does not fail, it waits for ever.
    let webview2 = tauri::webview_version()?;
    let shell = Shell::new(settings, win::platform())?;

    let log = PathBuf::from(paths::join(&folders.appdata_dir, "shell.log"));
    *shell.log_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move |line: &str| {
        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(&log) {
            let _ = writeln!(file, "[{}] {line}", vispora_shell_core::stamp());
        }
    }));
    shell.say(&format!("shell {} starting (WebView2 {webview2}{})", shell.settings.version,
                       if from_autostart { ", at sign-in" } else { "" }));

    let for_requests = shell.clone();
    let app = tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("vispora", move |_context, asked, responder| {
            // Answered on a thread of its own: a call that is passed on
            // takes as long as the backend takes, and the window has to
            // keep drawing meanwhile.
            let shell = for_requests.clone();
            std::thread::spawn(move || {
                let target = asked.uri().path_and_query().map_or("/", |target| target.as_str()).to_string();
                let mut request = Request::new(asked.method().as_str(), &target).with_body(asked.body().clone());
                for (name, value) in asked.headers() {
                    request = request.with_header(name.as_str(), &String::from_utf8_lossy(value.as_bytes()));
                }
                let answer = shell.handle(&request);
                let mut reply = Answer::builder().status(answer.status);
                for (name, value) in &answer.headers {
                    reply = reply.header(name.as_str(), value.as_str());
                }
                let reply = reply.body(answer.body).unwrap_or_else(|_| {
                    Answer::builder().status(502).body(b"{\"ok\": false, \"err\": \"the answer could not be handed to the page\"}".to_vec())
                        .expect("a status and a body always make a response")
                });
                responder.respond(reply);
            });
        })
        .build(tauri::generate_context!())?;

    // The page's address ends in the run's secret, as it always did: the
    // page takes it from there and out of its address bar.
    let address = format!("{PAGE}{}", shell.fragment()).parse()?;
    let window = WebviewWindowBuilder::new(&app, "main", WebviewUrl::CustomProtocol(address))
        .title(APP_NAME)
        // The same window as the Python app's: no frame of Windows' own (the
        // page draws its title bar), the same sizes, the app's background
        // while the page loads.
        .inner_size(1320.0, 860.0)
        .min_inner_size(1024.0, 600.0)
        .decorations(false)
        .maximizable(false)
        .shadow(false)
        .background_color(Color(10, 10, 15, 255))
        // WebView2 keeps a profile folder; it goes into the app's data folder.
        .data_directory(PathBuf::from(paths::join(&folders.appdata_dir, "webview")))
        // The window stays on the app's own page ...
        .on_navigation(|url| url.scheme() == "vispora" || url.host_str() == Some("vispora.localhost"))
        // ... and a link the page opens goes to the browser, started the
        // way Explorer would start it (it gets none of this program's
        // handles: see win::start_other).
        .on_new_window(|url, _features| {
            if matches!(url.scheme(), "http" | "https") {
                let _ = win::start_other(url.as_str(), "");
            }
            NewWindowResponse::Deny
        })
        .initialization_script(page::shim(shell.token()));
    let window = match trial.map(|trial| trial.window.as_str()) {
        // (only in a trial build: out of the way on a PC somebody is using)
        // (hidden, and beyond the edge of the desktop for when it is shown)
        Some("hidden") => window.position(-30000.0, -30000.0).focused(false).skip_taskbar(true).visible(false),
        Some("offscreen") => window.position(-30000.0, -30000.0).focused(false).skip_taskbar(true),
        // At sign-in the app starts out of sight, as it always has.
        _ if from_autostart => window.visible(false),
        _ => window.center(),
    }
    .build()?;

    let out_of_the_way = trial.is_some_and(|trial| trial.window != "shown");
    let pane = Arc::new(Pane(window, out_of_the_way));
    *shell.window.lock().unwrap_or_else(|p| p.into_inner()) = Some(pane.clone());
    // Another start of the app asks this copy for its window.
    if let Some(held) = &held {
        let (asked_for, shell) = (pane.clone(), shell.clone());
        held.when_asked_to_show(Box::new(move || {
            shell.say("another start of the app asked for the window");
            asked_for.show();
        }));
    }
    // The shell is gone when the backend's process has ended: then, and
    // only then, the window's loop is told to end.
    let handle = app.handle().clone();
    *shell.exit_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move |code| handle.exit(code)));

    shell.start();
    let code = app.run_return(|_, _| {});
    // The window is gone: closed from outside (Alt+F4, the taskbar, signing
    // out), or the quit is over.  Either way the process does not end
    // before the backend has put the system back and ended.
    let quit = shell.quit_and_wait("the window was closed");
    drop(held);
    Ok(if code != 0 { code } else { quit })
}
