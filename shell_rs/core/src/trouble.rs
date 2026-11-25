//! What the person is told when the app cannot start: plain words, and
//! words of its own for each kind of trouble.  A failure that is not
//! WebView2's is not blamed on WebView2.

/// Why the app could not start.
#[derive(Debug, Clone, PartialEq)]
pub enum Trouble {
    /// The shell's own part could not be set up (its socket, its secret).
    Shell(String),
    /// The folder the window keeps its files in could not be made: where
    /// it was to be made, and what went wrong.
    Folder { place: String, why: String },
    /// WebView2 is missing, or the window could not be opened.
    Window(String),
    /// The backend, which the exe carries, could not be unpacked.
    Backend(String),
}

impl Trouble {
    /// The whole of what is said, in a plain Windows message.
    pub fn words(&self) -> String {
        let (what, details) = match self {
            Trouble::Shell(why) => (
                "Vispora could not start.\n\n\
                 It could not set up the connection between its window and its background program. \
                 That connection stays inside this PC. Starting Vispora again usually helps; if it does not, \
                 the details below say what Windows answered."
                    .to_string(),
                why,
            ),
            Trouble::Folder { place, why } => (
                format!(
                    "Vispora could not make the folder its window keeps its files in while the app runs.\n\n\
                     The folder is made in {place}. That folder has to be there, and Vispora has to be allowed to make a folder in it. \
                     This has nothing to do with Microsoft Edge WebView2: installing or repairing that does not help here."
                ),
                why,
            ),
            Trouble::Backend(why) => (
                "Vispora could not get its background program ready.\n\n\
                 The program is inside Vispora.exe and is unpacked when the app starts, into a folder that only administrators can change. \
                 That takes free room on the drive Windows is on. If Vispora.exe was damaged on its way to this PC, downloading it again helps. \
                 This has nothing to do with Microsoft Edge WebView2."
                    .to_string(),
                why,
            ),
            Trouble::Window(why) => (
                "Vispora's window could not be opened.\n\n\
                 It needs Microsoft Edge WebView2, which is part of Windows 11 and of most Windows 10 PCs. \
                 Installing or repairing \"Microsoft Edge WebView2 Runtime\" usually fixes this."
                    .to_string(),
                why,
            ),
        };
        format!("{what}\n\nNothing was changed on this PC.\n\nDetails: {details}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_kind_of_trouble_has_words_of_its_own() {
        let window = Trouble::Window("WebView2 is not installed".into()).words();
        let folder = Trouble::Folder { place: "C:\\Windows\\Temp".into(), why: "C:\\Windows\\Temp is missing".into() }.words();
        let shell = Trouble::Shell("Windows error 10106".into()).words();
        assert!(window.starts_with("Vispora's window could not be opened.") && window.contains("Installing or repairing \"Microsoft Edge WebView2 Runtime\" usually fixes this."));
        // A folder that cannot be made is not WebView2's doing, and the
        // person is not sent to install it.
        assert!(folder.starts_with("Vispora could not make the folder its window keeps its files in"));
        assert!(folder.contains("The folder is made in C:\\Windows\\Temp.") && folder.contains("nothing to do with Microsoft Edge WebView2"));
        assert!(!folder.contains("usually fixes this") && !shell.contains("WebView2"));
        assert!(shell.starts_with("Vispora could not start."));
        let backend = Trouble::Backend("there is no room left on the drive".into()).words();
        assert!(backend.starts_with("Vispora could not get its background program ready.") && backend.contains("is unpacked when the app starts"));
        assert!(!backend.contains("usually fixes this") && backend.contains("nothing to do with Microsoft Edge WebView2"));
        for (said, why) in [(&window, "WebView2 is not installed"), (&folder, "C:\\Windows\\Temp is missing"), (&shell, "Windows error 10106"),
                            (&backend, "there is no room left on the drive")] {
            assert!(said.ends_with(&format!("Nothing was changed on this PC.\n\nDetails: {why}")), "{said}");
            assert!(!said.contains('\u{2014}') && !said.contains("  "), "{said}");
        }
    }
}
