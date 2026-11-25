//! The tray icon: Tauri's, showing what shell_rs/core/src/tray.rs decides.
//!
//! Today's tray (app.py, _create_tray_icon) is the measure.  The icon is
//! there from the first moment, before the backend is up.  A click or a
//! double click with the left button brings the window back; the right
//! button opens the menu.  "Open Vispora" and "Quit" are the shell's own:
//! the window is the shell's, and quitting from here goes the way every
//! quit goes.  Every other entry is carried out by the backend, as it
//! always was (core: tray::call_for).  What the icon shows is asked of the
//! backend every three seconds, as today's tray asks.

use std::sync::{Arc, Condvar, Mutex};

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, ContextMenu, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::{App, AppHandle};
use vispora_shell_core::platform::Window;
use vispora_shell_core::shell::Phase;
use vispora_shell_core::tray::{self, Kind, Look, State};
use vispora_shell_core::Shell;

/// What a click on the icon or in its menu acts on.
struct Hands {
    shell: Arc<Shell>,
    window: Arc<dyn Window>,
    /// Set when the tray is to be looked at again now, and not at the next
    /// three seconds: after a menu entry was carried out.
    look_again: Mutex<bool>,
    wake: Condvar,
}

impl Hands {
    /// Bring the window back.
    fn open(&self, asked_by: &str) {
        self.shell.say(&format!("{asked_by} asked for the window"));
        self.window.show();
    }

    /// A menu entry was chosen.  Never waits: this runs where the window's
    /// loop runs, and the backend takes the time it takes.
    fn chosen(self: &Arc<Self>, id: &str) {
        match id {
            tray::OPEN => self.open("the tray's menu"),
            tray::QUIT => self.shell.quit("Quit in the tray's menu"),
            other => {
                let Some(call) = tray::call_for(other) else { return };
                let (hands, entry) = (self.clone(), other.to_string());
                std::thread::spawn(move || {
                    let answered = hands.shell.ask_backend("POST", call).is_some();
                    hands.shell.say(&format!("the tray's menu: {entry}, {}", if answered { "carried out by the backend" } else { "not answered by the backend" }));
                    hands.again();
                });
            }
        }
    }

    /// The icon itself was clicked.
    fn clicked(&self, event: &TrayIconEvent) {
        match event {
            TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } => self.open("a click on the tray icon"),
            TrayIconEvent::DoubleClick { button: MouseButton::Left, .. } => self.open("a double click on the tray icon"),
            _ => {}
        }
    }

    fn again(&self) {
        *self.look_again.lock().unwrap_or_else(|p| p.into_inner()) = true;
        self.wake.notify_all();
    }
}

/// The menu, as Windows shows it.
fn menu(app: &AppHandle, look: &Look) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    for entry in &look.menu {
        match entry.kind {
            Kind::Line => menu.append(&PredefinedMenuItem::separator(app)?)?,
            Kind::Entry => menu.append(&MenuItem::with_id(app, entry.id, &entry.text, entry.enabled, None::<&str>)?)?,
            Kind::Tick(on) => menu.append(&CheckMenuItem::with_id(app, entry.id, &entry.text, entry.enabled, on, None::<&str>)?)?,
        }
    }
    Ok(menu)
}

/// Make the icon show `look`.  The menu's own number in Windows, for a
/// trial that wants to read the menu as Windows has it.
fn show(icon: &TrayIcon, look: &Look) -> tauri::Result<isize> {
    let side = tray::SIZE as u32;
    icon.set_icon(Some(Image::new(tray::picture(look.icon), side, side)))?;
    icon.set_tooltip(Some(&look.tooltip))?;
    let menu = menu(icon.app_handle(), look)?;
    let number = menu.hpopupmenu()?;
    icon.set_menu(Some(menu))?;
    Ok(number)
}

/// Put the icon in the tray, and keep it up to date for as long as the
/// shell runs.  `for_a_trial`: also write down what a trial reads.
pub fn make(app: &App, shell: Arc<Shell>, window: Arc<dyn Window>, for_a_trial: bool) -> tauri::Result<()> {
    let hands = Arc::new(Hands { shell: shell.clone(), window, look_again: Mutex::new(false), wake: Condvar::new() });
    // Until the backend has said anything: the mark in grey, and the two
    // entries that are the shell's own.
    let first = tray::look(None);
    let side = tray::SIZE as u32;
    let (for_menu, for_clicks) = (hands.clone(), hands.clone());
    let icon = TrayIconBuilder::with_id("vispora")
        .icon(Image::new(tray::picture(first.icon), side, side))
        .tooltip(&first.tooltip)
        // The left button brings the window back, as today; it does not
        // open the menu.
        .show_menu_on_left_click(false)
        .on_menu_event(move |_app, event| for_menu.chosen(event.id().as_ref()))
        .on_tray_icon_event(move |_icon, event| for_clicks.clicked(&event))
        .build(app)?;
    let said = move |shell: &Shell, look: &Look, number: isize| {
        shell.say(&format!("the tray shows: {}, \"{}\"", look.icon.name(), look.tooltip));
        if for_a_trial {
            shell.say(&format!("tray look {number} {}", look.to_json()));
        }
    };
    said(&shell, &first, show(&icon, &first)?);

    let watching = hands.clone();
    std::thread::spawn(move || {
        let mut shown = first;
        let mut complaints = tray::Complaints::default();
        loop {
            if matches!(shell.phase(), Phase::Quitting | Phase::Gone) {
                return;
            }
            let asked_for = std::mem::replace(&mut *watching.look_again.lock().unwrap_or_else(|p| p.into_inner()), false);
            let state = shell.ask_backend("GET", tray::STATE_CALL).as_ref().and_then(State::from_json);
            let look = tray::look(state.as_ref());
            // (after a menu entry the menu is made again whether or not
            // anything changed: Windows ticks an entry by itself when it is
            // clicked, and the tick has to say what is so)
            if asked_for || look != shown {
                match show(&icon, &look) {
                    Ok(number) => {
                        if let Some(line) = complaints.worked() {
                            shell.say(&line);
                        }
                        said(&shell, &look, number);
                        shown = look;
                    }
                    // (tried again at every round, and said once)
                    Err(why) => {
                        if let Some(line) = complaints.failed(&why.to_string()) {
                            shell.say(&line);
                        }
                    }
                }
            }
            // (soon only while the backend is not up: see tray::look_again_in)
            let pause = tray::look_again_in(shell.phase() == Phase::Ready);
            let waiting = watching.look_again.lock().unwrap_or_else(|p| p.into_inner());
            let _ = watching.wake.wait_timeout_while(waiting, pause, |again| !*again);
        }
    });
    Ok(())
}
