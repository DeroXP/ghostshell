//! Names and on-disk locations.
//!
//! The updater is built and shipped separately from the app, so the few
//! names both sides must agree on are repeated here (the app's are in
//! config.py; tests/test_updater_rs.py checks that they still match).
//!
//! Paths are kept as text, the way the app writes them into
//! install_info.json and the command line hands them over, and joined and
//! split the way Windows paths are.

use std::path::Path;

pub const APP_NAME: &str = "Vispora";
/// The app's name until 3.5.0.  Installs from that era still have a
/// GhostShell.exe and a %APPDATA%\GhostShell folder on disk, and the updater
/// has to be able to find and update them.
pub const LEGACY_APP_NAME: &str = "GhostShell";
/// New name first, so an on-disk Vispora.exe wins over a leftover GhostShell.exe.
pub const APP_EXE_NAMES: [&str; 2] = ["Vispora.exe", "GhostShell.exe"];

fn is_sep(c: char) -> bool {
    c == '\\' || c == '/'
}

/// `a\b`, as Python's os.path.join gives it for a folder and a name.
pub fn join(folder: &str, name: &str) -> String {
    if folder.is_empty() || folder.ends_with(is_sep) {
        format!("{folder}{name}")
    } else {
        format!("{folder}\\{name}")
    }
}

/// Where the part after the drive ("C:") or the share ("\\server\share") starts.
fn after_drive(path: &str) -> usize {
    let b = path.as_bytes();
    if b.len() >= 2 && b[1] == b':' {
        return 2;
    }
    if b.len() > 2 && is_sep(b[0] as char) && is_sep(b[1] as char) && !is_sep(b[2] as char) {
        // \\server\share
        let rest = &path[2..];
        if let Some(i) = rest.find(is_sep) {
            let share = &rest[i + 1..];
            if !share.is_empty() {
                return 2 + i + 1 + share.find(is_sep).unwrap_or(share.len());
            }
        }
    }
    0
}

/// The folder a path is in, as os.path.dirname gives it: "C:\x\y.exe" is in
/// "C:\x", "C:\y.exe" in "C:\", and a bare name in "".
pub fn dirname(path: &str) -> String {
    let drive = after_drive(path);
    let tail = &path[drive..];
    let cut = tail.rfind(is_sep).map_or(0, |i| i + 1);
    let head = &tail[..cut];
    let trimmed = head.trim_end_matches(is_sep);
    format!("{}{}", &path[..drive], if trimmed.is_empty() { head } else { trimmed })
}

/// The last part of a path.
pub fn basename(path: &str) -> &str {
    let tail = &path[after_drive(path)..];
    &tail[tail.rfind(is_sep).map_or(0, |i| i + 1)..]
}

/// Two paths that name the same file to Windows (case and slashes aside).
pub fn same_path(a: &str, b: &str) -> bool {
    let norm = |p: &str| p.replace('/', "\\").to_lowercase();
    norm(a) == norm(b)
}

pub fn is_file(path: &str) -> bool {
    !path.is_empty() && Path::new(path).is_file()
}

pub fn is_dir(path: &str) -> bool {
    !path.is_empty() && Path::new(path).is_dir()
}

/// The folders the updater looks in, as the environment names them.  Tests
/// hand in folders of their own, so the scan for an install never looks at
/// the real Program Files.
#[derive(Clone, Debug)]
pub struct Env {
    /// %APPDATA% (the user's folder where that is not set).
    pub appdata: String,
    /// %LOCALAPPDATA%.
    pub localappdata: String,
    /// The user's folder.
    pub home: String,
    pub program_files: String,
    pub program_files_x86: String,
}

impl Env {
    /// From the process's environment, with the fallbacks the Python updater had.
    pub fn from_process() -> Env {
        let var = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
        let home = var("USERPROFILE")
            .or_else(|| Some(format!("{}{}", var("HOMEDRIVE")?, var("HOMEPATH")?)))
            .unwrap_or_else(|| "C:\\".to_string());
        Env {
            appdata: var("APPDATA").unwrap_or_else(|| home.clone()),
            localappdata: var("LOCALAPPDATA").unwrap_or_else(|| join(&join(&home, "AppData"), "Local")),
            program_files: var("ProgramFiles").unwrap_or_else(|| "C:\\Program Files".to_string()),
            program_files_x86: var("ProgramFiles(x86)").unwrap_or_else(|| "C:\\Program Files (x86)".to_string()),
            home,
        }
    }
}

/// Where the app keeps its data, and the updater its own files.
#[derive(Clone, Debug)]
pub struct Paths {
    pub appdata_dir: String,
    /// `<data folder>\updater`: downloads are staged here, and the log is here.
    pub updater_dir: String,
    pub log_path: String,
    /// install_info.json, written by the app at every start.
    pub install_info: String,
}

impl Paths {
    /// The folder the app keeps its data in: %APPDATA%\Vispora, or the
    /// pre-rename %APPDATA%\GhostShell while that is still the only one on
    /// disk.  The app moves the old folder across on its own first launch;
    /// the updater, which normally runs from inside that folder, only follows
    /// where it is.  It must never create the new folder next to a populated
    /// old one, or the app would start using the empty one and the user's
    /// data would look gone.
    pub fn resolve(env: &Env) -> Paths {
        let current = join(&env.appdata, APP_NAME);
        let legacy = join(&env.appdata, LEGACY_APP_NAME);
        let appdata_dir = if is_dir(&legacy) && !is_dir(&current) { legacy } else { current };
        let updater_dir = join(&appdata_dir, "updater");
        let _ = std::fs::create_dir_all(&updater_dir);
        Paths {
            log_path: join(&updater_dir, "updater.log"),
            install_info: join(&appdata_dir, "install_info.json"),
            updater_dir,
            appdata_dir,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_are_joined_and_split_the_windows_way() {
        assert_eq!(join("C:\\Program Files", "Vispora"), "C:\\Program Files\\Vispora");
        assert_eq!(join("C:\\", "Vispora.exe"), "C:\\Vispora.exe");
        assert_eq!(join("D:/Games/", "x"), "D:/Games/x");
        // (ntpath.dirname / basename)
        for (path, folder, name) in [
            ("C:\\Program Files\\Vispora\\Vispora.exe", "C:\\Program Files\\Vispora", "Vispora.exe"),
            ("C:\\Vispora.exe", "C:\\", "Vispora.exe"),
            ("C:\\", "C:\\", ""),
            ("C:Vispora.exe", "C:", "Vispora.exe"),
            ("Vispora.exe", "", "Vispora.exe"),
            ("D:/Games/Vispora", "D:/Games", "Vispora"),
            ("D:\\Games\\Vispora\\", "D:\\Games\\Vispora", ""),
            ("\\\\server\\share\\app\\Vispora.exe", "\\\\server\\share\\app", "Vispora.exe"),
            ("\\\\server\\share\\Vispora.exe", "\\\\server\\share\\", "Vispora.exe"),
            ("\\Games\\x", "\\Games", "x"),
            ("\\x", "\\", "x"),
            ("", "", ""),
        ] {
            assert_eq!(dirname(path), folder, "dirname of {path}");
            assert_eq!(basename(path), name, "basename of {path}");
        }
        assert!(same_path("C:/Games/Vispora.EXE.bak.5", "c:\\games\\vispora.exe.bak.5"));
        assert!(!same_path("C:\\a", "C:\\b"));
    }
}
