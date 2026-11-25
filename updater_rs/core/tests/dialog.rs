//! The "pick a folder" dialog, for real.
//!
//! Every other test in this folder runs without a screen.  These two put
//! Windows' folder dialog on it for about a second each, press one of its
//! buttons and look at what win::pick_folder hands back.  They are ignored by
//! default, so that `cargo test` never takes the keyboard from whoever is at
//! the PC.  To run them:
//!
//!     cargo test --release --offline --manifest-path updater_rs/core/Cargo.toml --test dialog -- --ignored --test-threads 1
//!
//! The dialog is driven through Windows' own interfaces by hand-numbered
//! positions (see win.rs), which is exactly the kind of code that has to be
//! run once to be believed.

#![cfg(windows)]
#![allow(non_snake_case)]

use std::time::{Duration, Instant};

use vispora_updater_core::win;

#[link(name = "user32")]
extern "system" {
    fn EnumWindows(each: unsafe extern "system" fn(isize, isize) -> i32, param: isize) -> i32;
    fn GetWindowThreadProcessId(window: isize, process: *mut u32) -> u32;
    fn GetClassNameW(window: isize, name: *mut u16, capacity: i32) -> i32;
    fn IsWindowVisible(window: isize) -> i32;
    fn PostMessageW(window: isize, message: u32, wparam: usize, lparam: isize) -> i32;
}

const WM_COMMAND: u32 = 0x0111;
const OK_BUTTON: usize = 1;
const CANCEL_BUTTON: usize = 2;

/// A dialog box (#32770) of this process that is on the screen.
unsafe extern "system" fn each(window: isize, found: isize) -> i32 {
    let mut process = 0u32;
    GetWindowThreadProcessId(window, &mut process);
    let mut class = [0u16; 32];
    let length = GetClassNameW(window, class.as_mut_ptr(), class.len() as i32).max(0) as usize;
    if process == std::process::id() && IsWindowVisible(window) != 0 && String::from_utf16_lossy(&class[..length]) == "#32770" {
        *(found as *mut isize) = window;
        return 0;
    }
    1
}

/// Wait for the dialog, give it a moment to open its folder, press `button`.
fn press_when_it_is_up(button: usize) -> std::thread::JoinHandle<bool> {
    std::thread::spawn(move || {
        let gives_up = Instant::now() + Duration::from_secs(20);
        while Instant::now() < gives_up {
            let mut dialog = 0isize;
            // SAFETY: `each` writes one isize through the pointer it is
            // handed, which lives until EnumWindows returns.
            unsafe { EnumWindows(each, &mut dialog as *mut isize as isize) };
            if dialog != 0 {
                std::thread::sleep(Duration::from_millis(900));
                // SAFETY: a window handle and a plain message.
                return unsafe { PostMessageW(dialog, WM_COMMAND, button, 0) } != 0;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        false
    })
}

fn a_folder(name: &str) -> String {
    let folder = std::env::temp_dir().join(format!("vispora-updater-dialog-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&folder).unwrap();
    folder.canonicalize().unwrap().to_string_lossy().trim_start_matches(r"\\?\").to_string()
}

#[test]
#[ignore = "puts a real dialog on the screen for a second"]
fn the_folder_it_was_opened_in_is_what_its_button_picks() {
    let folder = a_folder("picked");
    let pressed = press_when_it_is_up(OK_BUTTON);
    let picked = win::pick_folder(0, &folder);
    assert!(pressed.join().unwrap(), "the dialog never came up");
    let picked = picked.expect("the dialog failed").expect("nothing was picked");
    assert_eq!(picked.to_lowercase(), folder.to_lowercase());
    let _ = std::fs::remove_dir(&folder);
}

#[test]
#[ignore = "puts a real dialog on the screen for a second"]
fn a_cancelled_dialog_picks_nothing() {
    let folder = a_folder("cancelled");
    let pressed = press_when_it_is_up(CANCEL_BUTTON);
    let picked = win::pick_folder(0, &folder);
    assert!(pressed.join().unwrap(), "the dialog never came up");
    assert_eq!(picked, Ok(None));
    let _ = std::fs::remove_dir(&folder);
}
