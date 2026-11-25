//! The tray icon, as far as it needs no tray: which of its three pictures
//! it shows, what its tooltip says, and what is in its menu, all decided
//! from what the backend says about itself.
//!
//! Today's tray (app.py, _create_tray_icon) is the measure: the same three
//! pictures, the same tooltip, the same menu in the same order with the
//! same words.  tests/test_tray_calls.py builds today's tray and this one
//! from the same state and holds them against each other.
//!
//! What a menu entry does is the backend's to carry out, as it always was
//! (`call_for`), except "Open Vispora" and "Quit": the window and quitting
//! are the shell's.

use crate::json::Json;

/// What the backend says the tray shows (GET /api/tray/state).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct State {
    /// Game Mode is on: the app is watching for games.
    pub monitoring: bool,
    /// A game is running.
    pub playing: bool,
    /// Its name, where the app knows one.
    pub active_game: Option<String>,
    pub crosshair: bool,
}

impl State {
    /// None for an answer that is not the backend's answer to that call.
    pub fn from_json(said: &Json) -> Option<State> {
        if !said["ok"].truthy() {
            return None;
        }
        Some(State {
            monitoring: said["monitoring"].truthy(),
            playing: said["playing"].truthy(),
            active_game: said["active_game"].as_str().filter(|name| !name.is_empty()).map(str::to_string),
            crosshair: said["crosshair"].truthy(),
        })
    }
}

/// Which of the three pictures.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Icon {
    /// Game Mode is off: the mark in grey.
    Idle,
    /// Game Mode is on and no game is running.
    Armed,
    /// A game is running: brighter, with a dot in the corner.
    Playing,
}

impl Icon {
    pub fn name(self) -> &'static str {
        match self {
            Icon::Idle => "idle",
            Icon::Armed => "armed",
            Icon::Playing => "playing",
        }
    }

    pub fn named(name: &str) -> Option<Icon> {
        [Icon::Idle, Icon::Armed, Icon::Playing].into_iter().find(|icon| icon.name() == name)
    }
}

/// What kind of line a menu has.
#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    /// Words to click (or, switched off, only to read).
    Entry,
    /// Words with a tick in front when the thing is on.
    Tick(bool),
    /// A line between groups.
    Line,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub id: &'static str,
    pub text: String,
    pub kind: Kind,
    /// False: shown, and not to be clicked.
    pub enabled: bool,
}

/// Everything the tray shows.
#[derive(Debug, Clone, PartialEq)]
pub struct Look {
    pub icon: Icon,
    pub tooltip: String,
    pub menu: Vec<Entry>,
}

pub const OPEN: &str = "open";
pub const STATUS: &str = "status";
pub const GAME_MODE: &str = "game-mode";
pub const FORCE_NORMAL: &str = "force-normal";
pub const CROSSHAIR: &str = "crosshair";
pub const REFRESH_PC: &str = "refresh-pc";
pub const CLEAN_RAM: &str = "clean-ram";
pub const QUIT: &str = "quit";
pub const LINE: &str = "line";

/// Where the backend says what the tray shows.
pub const STATE_CALL: &str = "/api/tray/state";

/// The backend's call for a menu entry that is the backend's to carry out.
pub fn call_for(id: &str) -> Option<&'static str> {
    match id {
        GAME_MODE => Some("/api/tray/game-mode"),
        FORCE_NORMAL => Some("/api/tray/force-normal"),
        CROSSHAIR => Some("/api/tray/crosshair"),
        REFRESH_PC => Some("/api/tray/refresh-pc"),
        CLEAN_RAM => Some("/api/tray/clean-ram"),
        _ => None,
    }
}

/// The first `most` letters of `text`.
fn first(text: &str, most: usize) -> String {
    text.chars().take(most).collect()
}

/// What the tray shows for `state`.  None: the backend has not said yet (it
/// is still starting, or it is gone).  The tray is there all the same, with
/// the two entries that are the shell's own; the others can be read and not
/// clicked until there is a backend to carry them out.
pub fn look(state: Option<&State>) -> Look {
    let nothing_yet = State::default();
    let (said, state) = (state.is_some(), state.unwrap_or(&nothing_yet));
    let icon = if state.playing {
        Icon::Playing
    } else if state.monitoring {
        Icon::Armed
    } else {
        Icon::Idle
    };
    let game = state.active_game.as_deref();
    let tooltip = match game {
        Some(game) => format!("Vispora: playing {}", first(game, 48)),
        None if icon == Icon::Armed => "Vispora: game mode armed".to_string(),
        None => "Vispora".to_string(),
    };
    let status = match game {
        Some(game) => format!("\u{25b6}  {}", first(game, 38)),
        None if state.monitoring => "\u{25cf}  Game Mode: armed".to_string(),
        None => "\u{25cb}  Game Mode: off".to_string(),
    };
    let entry = |id, text: &str, kind, enabled| Entry { id, text: text.to_string(), kind, enabled };
    let line = || entry(LINE, "", Kind::Line, false);
    let mut menu = vec![
        entry(OPEN, "Open Vispora", Kind::Entry, true),
        line(),
        entry(STATUS, &status, Kind::Entry, false),
        entry(GAME_MODE, "Game Mode", Kind::Tick(state.monitoring), said),
    ];
    // (only while a game is running, as in today's menu)
    if game.is_some() {
        menu.push(entry(FORCE_NORMAL, "Force Normal Mode", Kind::Entry, said));
    }
    menu.extend([
        entry(CROSSHAIR, "Crosshair", Kind::Tick(state.crosshair), said),
        line(),
        entry(REFRESH_PC, "Refresh PC now", Kind::Entry, said),
        entry(CLEAN_RAM, "Clean RAM now", Kind::Entry, said),
        line(),
        entry(QUIT, "Quit", Kind::Entry, true),
    ]);
    Look { icon, tooltip, menu }
}

impl Look {
    /// The look as JSON: for the log, and for the tests that compare it
    /// with today's tray.
    pub fn to_json(&self) -> Json {
        let menu: Vec<Json> = self
            .menu
            .iter()
            .map(|entry| {
                let (kind, ticked) = match entry.kind {
                    Kind::Entry => ("entry", false),
                    Kind::Tick(on) => ("tick", on),
                    Kind::Line => ("line", false),
                };
                Json::object([
                    ("id", entry.id.into()),
                    ("text", entry.text.as_str().into()),
                    ("kind", kind.into()),
                    ("ticked", ticked.into()),
                    ("enabled", entry.enabled.into()),
                ])
            })
            .collect();
        Json::object([("icon", self.icon.name().into()), ("tooltip", self.tooltip.as_str().into()), ("menu", Json::Array(menu))])
    }
}

// ═════════════════════════════════════════════════════════════════════
// The three pictures
// ═════════════════════════════════════════════════════════════════════
// The app's mark, two blades of a V: in grey, in colour, and in brighter
// colour with a dot in the corner.  They are the pictures today's app draws
// for its tray icon when it starts (app.py, _render_tray_image), written
// down once by pictures/make.py and carried here as they are, so that the
// tray shows the same dots.  tests/test_tray_calls.py has the app draw them
// again at every run and compares.

/// A picture is this many dots wide and high.
pub const SIZE: usize = 64;

/// One of the three pictures: SIZE dots by SIZE dots, four bytes a dot
/// (red, green, blue, and how solid it is), row after row from the top.
pub fn picture(icon: Icon) -> &'static [u8] {
    match icon {
        Icon::Idle => include_bytes!("../pictures/tray-idle.rgba"),
        Icon::Armed => include_bytes!("../pictures/tray-armed.rgba"),
        Icon::Playing => include_bytes!("../pictures/tray-playing.rgba"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn texts(look: &Look) -> Vec<&str> {
        look.menu.iter().map(|entry| entry.text.as_str()).collect()
    }

    fn entry<'a>(look: &'a Look, id: &str) -> &'a Entry {
        look.menu.iter().find(|entry| entry.id == id).unwrap_or_else(|| panic!("no entry {id}"))
    }

    #[test]
    fn game_mode_off_armed_and_playing() {
        let off = look(Some(&State::default()));
        assert_eq!((off.icon, off.tooltip.as_str()), (Icon::Idle, "Vispora"));
        assert_eq!(texts(&off), ["Open Vispora", "", "\u{25cb}  Game Mode: off", "Game Mode", "Crosshair", "", "Refresh PC now", "Clean RAM now", "", "Quit"]);
        assert_eq!(entry(&off, GAME_MODE).kind, Kind::Tick(false));

        let armed = look(Some(&State { monitoring: true, crosshair: true, ..State::default() }));
        assert_eq!((armed.icon, armed.tooltip.as_str()), (Icon::Armed, "Vispora: game mode armed"));
        assert_eq!(entry(&armed, STATUS).text, "\u{25cf}  Game Mode: armed");
        assert_eq!((&entry(&armed, GAME_MODE).kind, &entry(&armed, CROSSHAIR).kind), (&Kind::Tick(true), &Kind::Tick(true)));
        assert!(armed.menu.iter().all(|entry| entry.id != FORCE_NORMAL), "nothing to force back while no game runs");

        // The two ticks are each their own.
        let ticks = |state: State| {
            let shown = look(Some(&state));
            (entry(&shown, GAME_MODE).kind.clone(), entry(&shown, CROSSHAIR).kind.clone())
        };
        assert_eq!(ticks(State { crosshair: true, ..State::default() }), (Kind::Tick(false), Kind::Tick(true)));
        assert_eq!(ticks(State { monitoring: true, ..State::default() }), (Kind::Tick(true), Kind::Tick(false)));

        let playing = look(Some(&State { monitoring: true, playing: true, active_game: Some("Some Game".into()), crosshair: false }));
        assert_eq!((playing.icon, playing.tooltip.as_str()), (Icon::Playing, "Vispora: playing Some Game"));
        assert_eq!(texts(&playing), ["Open Vispora", "", "\u{25b6}  Some Game", "Game Mode", "Force Normal Mode", "Crosshair", "", "Refresh PC now", "Clean RAM now", "", "Quit"]);
    }

    #[test]
    fn the_status_line_can_only_be_read_and_long_names_are_cut() {
        let name = "A".repeat(30) + &"\u{e9}".repeat(30);
        let playing = look(Some(&State { playing: true, active_game: Some(name.clone()), ..State::default() }));
        let status = entry(&playing, STATUS);
        assert!(!status.enabled && status.kind == Kind::Entry);
        // 38 letters in the menu, 48 in the tooltip: letters, not bytes.
        assert_eq!(status.text, format!("\u{25b6}  {}{}", "A".repeat(30), "\u{e9}".repeat(8)));
        assert_eq!(playing.tooltip, format!("Vispora: playing {}{}", "A".repeat(30), "\u{e9}".repeat(18)));
        // A game without a name the app knows: the picture says playing,
        // the words say what Game Mode is.
        let nameless = look(Some(&State { playing: true, ..State::default() }));
        assert_eq!((nameless.icon, nameless.tooltip.as_str(), entry(&nameless, STATUS).text.as_str()), (Icon::Playing, "Vispora", "\u{25cb}  Game Mode: off"));
    }

    #[test]
    fn before_the_backend_has_said_anything_only_the_shells_own_entries_can_be_clicked() {
        let early = look(None);
        assert_eq!((early.icon, early.tooltip.as_str()), (Icon::Idle, "Vispora"));
        let to_click: Vec<&str> = early.menu.iter().filter(|entry| entry.enabled).map(|entry| entry.id).collect();
        assert_eq!(to_click, [OPEN, QUIT]);
        // Once it has, everything but the status line and the lines.
        let later = look(Some(&State::default()));
        let to_click: Vec<&str> = later.menu.iter().filter(|entry| entry.enabled).map(|entry| entry.id).collect();
        assert_eq!(to_click, [OPEN, GAME_MODE, CROSSHAIR, REFRESH_PC, CLEAN_RAM, QUIT]);
    }

    #[test]
    fn what_the_backend_says_is_read_and_anything_else_is_not_taken_for_it() {
        let said = Json::parse(r#"{"ok": true, "monitoring": true, "playing": true, "active_game": "Some Game", "crosshair": true}"#).unwrap();
        assert_eq!(State::from_json(&said), Some(State { monitoring: true, playing: true, active_game: Some("Some Game".into()), crosshair: true }));
        let quiet = Json::parse(r#"{"ok": true, "monitoring": false, "playing": false, "active_game": null, "crosshair": false}"#).unwrap();
        assert_eq!(State::from_json(&quiet), Some(State::default()));
        for not_it in [r#"{"ok": false, "err": "missing or wrong session token"}"#, "{}", "[]", "null", r#""ok""#] {
            assert_eq!(State::from_json(&Json::parse(not_it).unwrap()), None, "{not_it}");
        }
        // An empty name is no name.
        let blank = Json::parse(r#"{"ok": true, "active_game": ""}"#).unwrap();
        assert_eq!(State::from_json(&blank).unwrap().active_game, None);
    }

    #[test]
    fn each_entry_that_is_the_backends_has_its_call_and_the_shells_own_have_none() {
        let playing = look(Some(&State { playing: true, active_game: Some("x".into()), ..State::default() }));
        let mut calls = Vec::new();
        for entry in &playing.menu {
            match call_for(entry.id) {
                Some(call) => calls.push(call),
                None => assert!([OPEN, STATUS, QUIT, LINE].contains(&entry.id), "{} has no call", entry.id),
            }
        }
        assert_eq!(calls, ["/api/tray/game-mode", "/api/tray/force-normal", "/api/tray/crosshair", "/api/tray/refresh-pc", "/api/tray/clean-ram"]);
        assert!(calls.iter().all(|call| call.starts_with("/api/tray/")) && STATE_CALL == "/api/tray/state");
    }

    #[test]
    fn the_pictures_are_the_mark_in_three_looks() {
        let dot = |picture: &[u8], column: usize, row: usize| -> [u8; 4] {
            let at = (row * SIZE + column) * 4;
            [picture[at], picture[at + 1], picture[at + 2], picture[at + 3]]
        };
        let (idle, armed, playing) = (picture(Icon::Idle), picture(Icon::Armed), picture(Icon::Playing));
        for picture in [idle, armed, playing] {
            assert_eq!(picture.len(), SIZE * SIZE * 4, "a picture is not {SIZE} by {SIZE} dots: run pictures/make.py again");
            // Nothing in the corners at the bottom and the top left.
            assert_eq!(dot(picture, 0, 0)[3], 0);
            assert_eq!(dot(picture, 1, SIZE - 1)[3], 0);
            assert_eq!(dot(picture, SIZE - 1, SIZE - 1)[3], 0);
            // The edges are smooth: some dots are neither empty nor solid.
            assert!(picture.chunks_exact(4).any(|d| d[3] > 0 && d[3] < 255));
        }
        // In the middle of each blade: its colour, solid.
        assert_eq!(dot(idle, 25, 26), [140, 145, 152, 255]);
        assert_eq!(dot(idle, 39, 26), [95, 100, 108, 255]);
        assert_eq!(dot(armed, 25, 26), [184, 200, 255, 255]);
        assert_eq!(dot(armed, 39, 26), [159, 147, 230, 255]);
        assert_eq!(dot(playing, 25, 26), [167, 240, 228, 255]);
        assert_eq!(dot(playing, 39, 26), [217, 184, 255, 255]);
        // The dot in the corner is there while a game runs, and only then.
        assert_eq!(dot(playing, 53, 11), [255, 200, 80, 255]);
        assert_eq!(dot(armed, 53, 11)[3], 0);
        assert_eq!(dot(idle, 53, 11)[3], 0);
        // Three different pictures.
        assert!(idle != armed && armed != playing && idle != playing);
        assert_eq!(Icon::named("armed"), Some(Icon::Armed));
        assert_eq!(Icon::named("other"), None);
    }
}
