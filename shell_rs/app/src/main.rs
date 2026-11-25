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
//! The tray icon is in tray.rs.  The backend is carried in the exe itself
//! and unpacked before it is started (core/src/backend.rs).  Nothing in a
//! release uses this program yet.

#![windows_subsystem = "windows"]

mod tray;

use std::io::Write as _;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use tauri::http::Response as Answer;
use tauri::utils::config::Color;
use tauri::webview::NewWindowResponse;
use tauri::{RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use vispora_shell_core::http::Request;
use vispora_shell_core::instance::{self, Begin, Held};
use vispora_shell_core::json::Json;
use vispora_shell_core::moved;
use vispora_shell_core::platform::{Command, Window};
use vispora_shell_core::pack::Pack;
use vispora_shell_core::trouble::Trouble;
use vispora_shell_core::{backend, page, paths, win, Settings, Shell, APP_NAME};

/// The build that is meant to ship asks for administrator rights; a trial
/// build never does (Cargo.toml).
const ASKS_FOR_RIGHTS: bool = cfg!(feature = "elevated");
/// What the exe carries of its backend: the app, as one piece of bytes
/// (core/src/pack.rs).  Put in when the exe is built (build.rs); nothing in
/// a build that was made without one.
static CARRIED: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/backend.pack"));
/// Which of the two ways the backend is kept on the PC (Cargo.toml).
const KEEPS_ITS_BACKEND: bool = cfg!(feature = "keep-backend");
/// Started with this, the shell answers nothing of what has moved from the
/// backend itself (core/src/moved/): every call is the backend's, as it
/// was before anything moved.
const ALL_TO_BACKEND: &str = "--all-to-backend";
/// "vispora" is a name of this program's own: WebView2 shows the page as
/// http://vispora.localhost/ and asks nobody outside for it.
const PAGE: &str = "vispora://localhost/";

/// How a trial build is told what to run with: a stand-in backend, names of
/// its own, where its window goes, and a folder it takes scripts from to
/// run in its page (how a trial looks at the page from inside).  The build
/// that ships does not read this.
struct Trial {
    tag: String,
    backend: Command,
    window: String,
    scripts: Option<String>,
    /// Where the window's folder is made, in place of the trial's own data
    /// folder: how a trial sees what is said when it cannot be made.
    folder_in: Option<String>,
    /// A file that takes what would otherwise be shown in a message box (a
    /// box would sit on the screen of whoever is at the PC, and wait).
    messages: Option<String>,
    /// A pack to unpack before the stand-in backend is started, in place
    /// of what the exe carries (how a trial sees the unpacking for real,
    /// the real backend's files included, without anything real running).
    pack: Option<String>,
    /// Which of the two ways the backend is kept, in place of the build's.
    keep: Option<bool>,
    /// False: every call is the backend's, as with ALL_TO_BACKEND.
    moved: Option<bool>,
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
        scripts: told["scripts"].as_str().map(str::to_string),
        folder_in: told["folder_in"].as_str().map(str::to_string),
        messages: told["messages"].as_str().map(str::to_string),
        pack: told["pack"].as_str().map(str::to_string),
        keep: match &told["keep"] {
            Json::Bool(keep) => Some(*keep),
            _ => None,
        },
        moved: match &told["moved"] {
            Json::Bool(moved) => Some(*moved),
            _ => None,
        },
    })
}

/// Tell the person something in a plain Windows message.  (A trial may ask
/// for it in a file instead.)
fn tell(trial: Option<&Trial>, text: &str) {
    match trial.and_then(|trial| trial.messages.as_ref()) {
        Some(file) => {
            if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(file) {
                let _ = writeln!(file, "{text}");
            }
        }
        None => win::tell(APP_NAME, text),
    }
}

/// (trial builds only) Run in the page whatever script turns up in `folder`.
fn run_scripts_from(folder: String, window: WebviewWindow) {
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(100));
        let Ok(entries) = std::fs::read_dir(&folder) else { continue };
        let mut scripts: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).filter(|path| path.extension().is_some_and(|kind| kind == "js")).collect();
        scripts.sort();
        for script in scripts {
            if let Ok(text) = std::fs::read_to_string(&script) {
                let _ = std::fs::remove_file(&script);
                let _ = window.eval(text);
            }
        }
    });
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
    // First of all, before anything of WebView2 is touched: its settings
    // are not taken from the environment (see win::clear_webview2_variables).
    // The shell sets none of them itself; what it wants of WebView2 it says
    // in the window's own options.
    let ignored = win::clear_webview2_variables();
    // (lossily: an argument that is not valid Unicode is not a reason to stop)
    let arguments: Vec<String> = std::env::args_os().skip(1).map(|word| word.to_string_lossy().into_owned()).collect();
    let from_autostart = arguments.iter().any(|argument| argument == "--from-autostart");
    // Every call to the backend, as it was before anything moved into the
    // shell (core/src/moved/): for telling apart, on a PC, whether something
    // is the shell's doing or the backend's.
    let all_to_backend = arguments.iter().any(|argument| argument == ALL_TO_BACKEND);
    let trial = trial();
    if !ASKS_FOR_RIGHTS && trial.is_none() {
        win::tell(APP_NAME, "This build of the shell is for trials only and has no backend of its own.\n\nSee shell_rs/app/try_it.py.");
        std::process::exit(2);
    }

    // Is a copy running already?  Before anything asks for administrator
    // rights.  (A copy that was started to get them carries a mark for it,
    // instance::ASKED_FOR_RIGHTS, which goes no further than this.)
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
    // The backend: what the exe carries.  (A trial may name a pack of its
    // own, and starts its stand-in whatever was unpacked.)
    let carried: &'static [u8] = match trial.as_ref().and_then(|trial| trial.pack.as_ref()) {
        Some(named) => match std::fs::read(named) {
            Ok(bytes) => Box::leak(bytes.into_boxed_slice()),
            Err(why) => {
                tell(trial.as_ref(), &format!("The trial's pack {named} cannot be read: {why}"));
                std::process::exit(2);
            }
        },
        None => CARRIED,
    };
    if trial.is_none() && carried.is_empty() {
        // A build that was made without its backend has nothing to start.
        win::tell(APP_NAME, "Vispora's files are incomplete: its background program is missing.\n\nDownload Vispora again.");
        std::process::exit(1);
    }
    // (the program is put in once it is unpacked: see the_backend_made_ready)
    let mut backend = trial.as_ref().map_or_else(|| Command { program: String::new(), args: Vec::new(), cwd: None }, |trial| trial.backend.clone());
    backend.args.push("--backend".to_string());
    if from_autostart {
        backend.args.push("--from-autostart".to_string());
    }

    let mut settings = Settings::new(backend, &folders.appdata_dir);
    // What a person starts is this program: the backend is told where it
    // is (it needs it where it names the app's exe).
    settings.shell_exe = std::env::current_exe().ok().map(|exe| exe.to_string_lossy().into_owned());
    let all_to_backend = all_to_backend || trial.as_ref().is_some_and(|trial| trial.moved == Some(false));
    let code = match run(settings, &folders, held, from_autostart, trial.as_ref(), &ignored, carried, all_to_backend) {
        Ok(code) => code,
        // Each kind of trouble in words of its own (core/src/trouble.rs).
        Err(trouble) => {
            tell(trial.as_ref(), &trouble.words());
            1
        }
    };
    std::process::exit(code);
}

/// The window could not be opened, or WebView2 is not there.
fn no_window(why: impl std::fmt::Display) -> Trouble {
    Trouble::Window(why.to_string())
}

/// What became of unpacking the backend.
#[derive(Default)]
struct Unpacking {
    /// The folder it was unpacked into for this run, to be taken away at
    /// the quit.  (None for a folder that stays.)
    for_this_run: Mutex<Option<win::OwnFolder>>,
    /// The folder that stays, held under its name until the app ends: the
    /// folder that was checked is the folder every start of the backend
    /// runs from.  (None for a folder of this run's own.)
    kept_held: Mutex<Option<std::fs::File>>,
    /// It could not be made ready: the app then ends as one that did not start.
    failed: std::sync::atomic::AtomicBool,
}

/// Have the shell unpack what the exe carries before it starts its backend
/// for the first time: on the thread that starts the backend, so that the
/// window is there meanwhile.  `places` says where; `keeps` which of the
/// two ways.  In a trial the stand-in is started whatever was unpacked.
fn the_backend_made_ready(shell: &Arc<Shell>, carried: &'static [u8], places: backend::Places, keeps: bool, stand_in: bool,
                          messages: Option<String>) -> Arc<Unpacking> {
    let unpacked: Arc<Unpacking> = Arc::default();
    let (for_the_shell, slot, start_as) = (shell.clone(), unpacked.clone(), shell.settings.backend.clone());
    let made_ready = move || -> Result<Command, String> {
        let shell = &for_the_shell;
        let began = std::time::Instant::now();
        let pack = Pack::read(carried)?;
        let way = if keeps { backend::Way::OncePerVersion } else { backend::Way::EveryStart };
        let ready = backend::make_ready(&pack, way, &places)?;
        for line in &ready.said {
            shell.say(line);
        }
        shell.say(&format!("the background program was ready to be started {} ms after that began", began.elapsed().as_millis()));
        // Folders that stay and are not this pack's are taken away (all of
        // them, in a build that keeps none): never in the start's way.  This
        // pack's own is never among them, whether it was used or not: one
        // that did not pass is left exactly as it is.
        if let Some(kept_in) = places.kept.clone() {
            let (shell, but, open_to) = (shell.clone(), keeps.then(|| backend::kept_name(&pack)), places.open_to);
            std::thread::spawn(move || {
                let others = backend::remove_other_kept(&kept_in, but.as_deref(), open_to);
                if others != backend::Others::default() {
                    shell.say(&format!("folders of the background program from other versions: {} taken away, {} left because they could not be taken away now \
                                        (held open, or something in them is in use), {} left because they are not this program's making",
                                       others.removed, others.left, others.not_ours));
                }
            });
        }
        *slot.for_this_run.lock().unwrap_or_else(|p| p.into_inner()) = ready.own;
        *slot.kept_held.lock().unwrap_or_else(|p| p.into_inner()) = ready.held;
        Ok(if stand_in { start_as.clone() } else { Command { program: ready.program, ..start_as.clone() } })
    };
    let (for_trouble, gone_wrong) = (shell.clone(), unpacked.clone());
    *shell.prepare_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move || {
        made_ready().map_err(|why| {
            gone_wrong.failed.store(true, std::sync::atomic::Ordering::SeqCst);
            // Said to the person, and then the app is quit: there is
            // nothing for the window to show without its backend.
            let (shell, words, messages) = (for_trouble.clone(), Trouble::Backend(why.clone()).words(), messages.clone());
            std::thread::spawn(move || {
                match &messages {
                    Some(file) => {
                        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(file) {
                            let _ = writeln!(file, "{words}");
                        }
                    }
                    None => win::tell(APP_NAME, &words),
                }
                shell.quit("the background program could not be made ready");
            });
            why
        })
    }));
    unpacked
}

/// Open the window, start the backend, and stay until the app is quit.
#[allow(clippy::too_many_arguments)]
fn run(settings: Settings, folders: &paths::Paths, held: Option<Box<dyn Held>>, from_autostart: bool, trial: Option<&Trial>, ignored: &[String],
       carried: &'static [u8], all_to_backend: bool) -> Result<i32, Trouble> {
    // Asked before anything is opened: where WebView2 is missing, creating
    // the window does not fail, it waits for ever.
    let webview2 = tauri::webview_version().map_err(no_window)?;
    let shell = Shell::new(settings, win::platform()).map_err(Trouble::Shell)?;

    let log = PathBuf::from(paths::join(&folders.appdata_dir, "shell.log"));
    *shell.log_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move |line: &str| {
        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(&log) {
            let _ = writeln!(file, "[{}] {line}", vispora_shell_core::stamp());
        }
    }));
    shell.say(&format!("shell {} starting (WebView2 {webview2}{})", shell.settings.version,
                       if from_autostart { ", at sign-in" } else { "" }));
    if !ignored.is_empty() {
        shell.say(&format!("settings for WebView2 were in the environment and were not used: {}", ignored.join(", ")));
    }
    // What has moved from the backend is answered by the shell itself
    // (core/src/moved/: it only reads, and the graphics card's reading is
    // asked of the backend).  The backend's own routes are as they were,
    // and answer whatever the shell does not take.
    if all_to_backend {
        shell.say("every call is the backend's: the shell answers nothing of what has moved itself");
    } else {
        shell.reads_with(Arc::new(moved::Real), true);
    }

    // The window's own folder, new at every start and gone after it (see
    // win::private_folder).  The app as it ships keeps it where nobody
    // without administrator rights can rename it or take it away (see
    // win::administrators_folder); a trial keeps it with its own data.
    let (folder_in, open_to) = if ASKS_FOR_RIGHTS {
        let place = win::administrators_folder().map_err(|why| Trouble::Folder { place: "Windows' folder for temporary files".to_string(), why })?;
        (place, win::OpenTo::Administrators)
    } else {
        (trial.and_then(|trial| trial.folder_in.clone()).unwrap_or_else(|| folders.appdata_dir.clone()), win::OpenTo::ThisAccount)
    };
    let own_folder = win::private_folder(&folder_in, open_to).map_err(|why| Trouble::Folder { place: folder_in.clone(), why })?;
    shell.say(&format!("the window's folder for this run: {}", own_folder.path));

    // The backend is unpacked where the window's folder is made (for one
    // run), or where folders stay: in the app as it ships, places that only
    // administrators can change; in a trial, the trial's own data folder.
    let unpacked = (!carried.is_empty()).then(|| {
        let places = backend::Places {
            for_one_run: folder_in.clone(),
            kept: if ASKS_FOR_RIGHTS { win::all_users_folder() } else { Some(folder_in.clone()) },
            open_to,
        };
        let keeps = trial.and_then(|trial| trial.keep).unwrap_or(KEEPS_ITS_BACKEND);
        the_backend_made_ready(&shell, carried, places, keeps, trial.is_some(), trial.and_then(|trial| trial.messages.clone()))
    });

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
        .build(tauri::generate_context!())
        .map_err(no_window)?;

    // The page's address ends in the run's secret, as it always did: the
    // page takes it from there and out of its address bar.
    let address = format!("{PAGE}{}", shell.fragment()).parse().map_err(no_window)?;
    let (own_page, for_links) = (shell.settings.origin.clone(), shell.clone());
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
        // Nothing of a run is written down for a later one (the page keeps
        // the run's secret in its session storage), and what WebView2 does
        // write goes into the folder that is this run's alone.
        .incognito(true)
        .data_directory(PathBuf::from(&own_folder.path))
        // The window stays on the app's own page: the whole of where a
        // page is from is compared, not its host name alone ...
        .on_navigation(move |url| page::is_the_apps_page(url.as_str(), &own_page))
        // ... and a link the page opens is handed to Explorer, which opens
        // it in the person's browser as it opens anything clicked on the
        // desktop: without this program's rights and without its handles
        // (see win::open_link).
        .on_new_window(move |url, _features| {
            if let Err(why) = win::open_link(url.as_str()) {
                for_links.say(&format!("a link was not opened: {why}"));
            }
            NewWindowResponse::Deny
        })
        // (the script looks where it runs: see page::shim)
        .initialization_script(page::shim(shell.token(), &shell.settings.origin));
    let window = match trial.map(|trial| trial.window.as_str()) {
        // (only in a trial build: out of the way on a PC somebody is using)
        // (hidden, and beyond the edge of the desktop for when it is shown)
        Some("hidden") => window.position(-30000.0, -30000.0).focused(false).skip_taskbar(true).visible(false),
        Some("offscreen") => window.position(-30000.0, -30000.0).focused(false).skip_taskbar(true),
        // At sign-in the app starts out of sight, as it always has.
        _ if from_autostart => window.visible(false),
        _ => window.center(),
    }
    .build()
    .map_err(no_window)?;

    // What earlier runs left behind is taken away now that the window is
    // there, on a thread of its own: never in the window's way.  (A folder
    // that is in use, this run's own or a copy's in another session, is
    // not touched, and neither is one that somebody else made: see
    // win::remove_private_folders.)
    let (tidying, tidy_in) = (shell.clone(), folder_in.clone());
    std::thread::spawn(move || {
        let began = std::time::Instant::now();
        let tidied = win::remove_private_folders(&tidy_in, open_to);
        tidying.say(&format!(
            "the window's old folders: {} entries looked at in {} ms, {} left behind by earlier runs and removed, {} in use and left alone, \
             {} not this program's making and not touched",
            tidied.entries, began.elapsed().as_millis(), tidied.removed, tidied.left, tidied.not_ours
        ));
    });

    let out_of_the_way = trial.is_some_and(|trial| trial.window != "shown");
    let pane = Arc::new(Pane(window.clone(), out_of_the_way));
    *shell.window.lock().unwrap_or_else(|p| p.into_inner()) = Some(pane.clone());
    // The tray icon, there from the first moment.  Without one the app
    // still runs (a window that was hidden is then brought back by starting
    // the app again), as today's app runs without its tray.
    if let Err(why) = tray::make(&app, shell.clone(), pane.clone(), trial.is_some()) {
        shell.say(&format!("no tray icon: {why}"));
    }
    if let Some(folder) = trial.and_then(|trial| trial.scripts.clone()) {
        run_scripts_from(folder, window);
    }
    // Another start of the app asks this copy for its window.
    if let Some(held) = &held {
        let (asked_for, shell) = (pane.clone(), shell.clone());
        held.when_asked_to_show(Box::new(move || {
            shell.say("another start of the app asked for the window");
            asked_for.show();
        }));
    }
    // The shell is gone when the backend's process has ended: then, and
    // only then, the window is taken away, and with its last window gone
    // the window's loop ends.
    //
    // The window is taken away; the loop is not told to end.  WebView2 lets
    // go of the window's folder only when its window is gone, and the folder
    // is removed after the loop: with the loop told to end, the window was
    // still there, the folder stayed, and the quit waited three seconds for
    // nothing.  And told to end a loop that is over already, Tauri ends the
    // process on the spot, before anything after the loop is done; taking
    // away a window that is gone does nothing.
    let last_window = pane.clone();
    *shell.exit_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move |_code| {
        let _ = last_window.0.destroy();
    }));

    shell.start();
    let at_the_end = shell.clone();
    let code = app.run_return(move |_, event| {
        // The last thing the window's loop says, however it ends: the quit
        // is over, the window was closed from outside (Alt+F4, the taskbar),
        // or Windows is signing the person out, shutting down or replacing
        // the app's files.  In that last case the process is ended as soon
        // as this returns, and nothing after the loop is reached.  So here:
        // the process does not end before the backend has put the system
        // back and ended.
        if let RunEvent::Exit = event {
            at_the_end.quit_and_wait("the window is gone");
        }
    });
    let quit = shell.quit_and_wait("the window was closed");
    // WebView2's own programs end with the window, and let go of the folder.
    *shell.window.lock().unwrap_or_else(|p| p.into_inner()) = None;
    drop(pane);
    if !own_folder.remove() {
        shell.say("the window's folder is still in use and stays; the next start removes it");
    }
    // The backend has ended: the folder it was unpacked into for this run goes.
    let mut never_started = false;
    if let Some(unpacked) = unpacked {
        never_started = unpacked.failed.load(std::sync::atomic::Ordering::SeqCst);
        if let Some(folder) = unpacked.for_this_run.lock().unwrap_or_else(|p| p.into_inner()).take() {
            if !folder.remove() {
                shell.say("the background program's folder is still in use and stays; the next start removes it");
            }
        }
    }
    drop(held);
    Ok(if never_started { 1 } else if code != 0 { code } else { quit })
}
