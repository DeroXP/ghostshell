//! VisporaUpdater.exe: the updater's window, and the exe that is shipped.
//!
//! Everything the updater does is in updater_rs/core.  This file opens a
//! window (Tauri on WebView2), shows the updater's page in it and hands
//! every request of the page to the core, the way the Python updater's
//! window handed them to its Flask server.  The page is the Python updater's
//! own, unchanged: it asks `/api/status`, `/api/start` and so on, and here
//! those never leave the process.  There is no server and no open port.
//!
//! Nothing in the page can reach Tauri's own commands: none are registered,
//! and the page is given no permission for the built-in ones.
//!
//! With `--silent` there is no window at all and Tauri is never started.

#![windows_subsystem = "windows"]

use std::path::PathBuf;
use std::sync::Arc;

use tauri::http::Response;
use tauri::utils::config::Color;
use tauri::{WebviewUrl, WebviewWindowBuilder};
use vispora_updater_core::{cli, paths, server, win, Updater, UPDATER_VERSION};

/// The page's address.  "updater" is a name of this program's own: WebView2
/// shows it as http://updater.localhost/ and asks nobody outside for it.
const PAGE: &str = "updater://localhost/";
const TITLE: &str = "Vispora Updater";
/// What the pipeline answers when something it did not foresee happens.
const EXIT_UNEXPECTED: i32 = 9;

fn main() {
    // (lossily: an argument that is not valid Unicode is a wrong argument,
    // not a reason to stop before the first line of the log)
    let argv: Vec<String> = std::env::args_os().skip(1).map(|word| word.to_string_lossy().into_owned()).collect();
    let args = match cli::parse_args(&argv) {
        Ok(args) => args,
        Err(e) => {
            eprintln!("{}", e.message);
            std::process::exit(e.exit_code);
        }
    };
    let updater = Updater::new(paths::Env::from_process(), win::platform());
    cli::prepare(&updater, &args, UPDATER_VERSION);
    let code = if args.silent { cli::run_silent(&updater) } else { with_window(&updater) };
    std::process::exit(code);
}

/// Show the window and stay until it is closed.  The exit code.
fn with_window(updater: &Arc<Updater>) -> i32 {
    let app = match build(updater) {
        Ok(app) => app,
        Err(e) => return without_window(updater, &e.to_string()),
    };
    let code = app.run_return(|_, _| {});
    // The window is gone (Alt+F4, say), but an install may still be running:
    // returning now would cut an exe swap off half-way.  This waits for it,
    // whoever else is waiting for it already.
    updater.shut_down();
    code
}

fn build(updater: &Arc<Updater>) -> Result<tauri::App, Box<dyn std::error::Error>> {
    // Asked before anything is opened: where WebView2 is missing, creating
    // the window does not fail, it waits for ever.
    let webview2 = tauri::webview_version()?;
    updater.log(&format!("opening the window (WebView2 {webview2})"));
    let for_requests = updater.clone();
    let app = tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("updater", move |_context, request, responder| {
            // Answered on a thread of its own: the folder dialog stays open
            // for as long as the user likes, and the window has to keep
            // drawing meanwhile.
            let updater = for_requests.clone();
            std::thread::spawn(move || {
                let answer = server::handle(&updater, request.method().as_str(), request.uri().path(), request.body());
                let response = Response::builder()
                    .status(answer.status)
                    .header("Content-Type", answer.content_type)
                    .header("Cache-Control", "no-store")
                    .body(answer.body)
                    .expect("a status and two fixed headers always make a response");
                responder.respond(response);
            });
        })
        .build(tauri::generate_context!())?;

    let page = PAGE.parse().expect("the page's address is a constant");
    let window = WebviewWindowBuilder::new(&app, "main", WebviewUrl::CustomProtocol(page))
        .title(TITLE)
        // The same pane as the Python updater's: no frame of Windows' own
        // (the page draws its title bar, and that bar is what moves the
        // window), a fixed size, the app's background while the page loads.
        .inner_size(720.0, 580.0)
        .min_inner_size(640.0, 540.0)
        .resizable(false)
        .maximizable(false)
        .decorations(false)
        .shadow(false)
        .background_color(Color(2, 2, 3, 255))
        // WebView2 keeps a profile folder; it goes beside the updater's log,
        // not into a folder named after this program in the user's AppData.
        .data_directory(PathBuf::from(paths::join(&updater.paths.updater_dir, "webview")))
        // The page has no links.  Should anything ever try to take the
        // window elsewhere, it stays where it is.
        .on_navigation(|url| url.scheme() == "updater" || url.host_str() == Some("updater.localhost"))
        // No "Back / Reload / Print" menu on a right click.
        .initialization_script("document.addEventListener('contextmenu', function (e) { e.preventDefault(); });");
    let window = match trial_wish().as_deref() {
        // (only in a trial build: see trial_wish)
        Some("hidden") => window.visible(false),
        Some("offscreen") => window.position(-30000.0, -30000.0).focused(false).skip_taskbar(true),
        _ => window.center(),
    }
    .build()?;

    // "Browse..." in the page: Windows' folder dialog, over this window.
    let owner = window.hwnd()?.0 as isize;
    *updater.folder_hook.lock().unwrap_or_else(|p| p.into_inner()) =
        Some(Arc::new(move |start_in: &str| win::pick_folder(owner, start_in)));
    // "Close", and the end of a finished update: the core has already waited
    // for the install to stop when it calls this.
    let handle = app.handle().clone();
    *updater.exit_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move |code| handle.exit(code)));
    Ok(app)
}

/// The window could not be opened: in practice, Microsoft Edge WebView2 is
/// missing or broken on this PC.  An update the app asked for is installed
/// all the same; anything else needs somebody to look at it, so say why
/// there is nothing to look at.
fn without_window(updater: &Arc<Updater>, why: &str) -> i32 {
    updater.log(&format!("the window could not be opened: {why}"));
    let asked_by_the_app = {
        let state = updater.lock();
        state.auto_confirm && state.mode == "update"
    };
    if asked_by_the_app {
        updater.log("installing without a window");
        let code = updater.run_install_pipeline();
        updater.log(&format!("install without a window, exit: {code}"));
        return code;
    }
    win::tell(
        TITLE,
        &format!(
            "The updater's window could not be opened.\n\n\
             It needs Microsoft Edge WebView2, which is part of Windows 11 and of most Windows 10 PCs. \
             Installing or repairing \"Microsoft Edge WebView2 Runtime\" usually fixes this.\n\n\
             Nothing was changed on this PC.\n\nDetails: {why}"
        ),
    );
    EXIT_UNEXPECTED
}

/// A build without the `elevated` feature (never the shipped one) can keep
/// its window out of the way, so that try_it.py can run it on a PC somebody
/// is using: "hidden" never shows it, "offscreen" shows it beyond the edge of
/// the desktop without taking the keyboard.  The shipped exe has no such
/// switch.
fn trial_wish() -> Option<String> {
    if cfg!(feature = "elevated") {
        return None;
    }
    std::env::var("VISPORA_UPDATER_TRY").ok()
}
