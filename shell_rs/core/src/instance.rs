//! The first thing a start does: find out whether it is the one copy.
//!
//! In this order, which is the app's own (app.py, main()):
//!
//! 1. Is a copy running already?  Then it is asked to show its window, and
//!    this start leaves.  Before anything asks for administrator rights: a
//!    second start must not put Windows' prompt on the screen only to say
//!    that the app is running.
//! 2. Does this copy have administrator rights?  The app needs them.  If
//!    not, it starts itself again asking for them, and leaves.  (The build
//!    for trials never asks.)
//! 3. Then it becomes the one copy, and keeps what makes it so for as long
//!    as it runs.
//!
//! "Is a copy running" is a name in Windows that only one program can hold
//! (a mutex), the same name the Python app has always used, so that an old
//! copy and a new one see each other.  "Show your window" is a second name
//! (an event) that the running copy listens on and anybody in the same
//! session may ring.  Windows' own calls for both (win.rs); nothing is sent
//! over a port.

/// What keeps this copy the one copy.  Let go of when the shell ends.
pub trait Held: Send {
    /// From now on, call `show` whenever another start asks for the window.
    fn when_asked_to_show(&self, show: Box<dyn Fn() + Send>);
}

/// Windows, as far as this file asks it anything.
pub trait Session {
    /// Ask a running copy to show its window.  False when there is none to ask.
    fn ask_other_to_show(&self) -> bool;
    /// Is a copy running that could not be asked (an older build of the app)?
    fn another_is_running(&self) -> bool;
    fn has_administrator_rights(&self) -> bool;
    /// Start this program again, asking for administrator rights.
    fn start_with_administrator_rights(&self, arguments: &[String]) -> Result<(), String>;
    /// Take the name.  None when another start took it a moment earlier.
    fn become_the_one(&self) -> Result<Option<Box<dyn Held>>, String>;
}

/// What a copy is started with when it was started to get administrator
/// rights.  It is not passed on to anything else.  A copy that carries it
/// and still has no rights does not ask again: where Windows starts the
/// new copy without them all the same (its prompt switched off on an account
/// that is no administrator's, say), each copy would start the next and
/// leave, without an end and without a word.
pub const ASKED_FOR_RIGHTS: &str = "--asked-for-rights";

/// How a start goes on.
pub enum Begin {
    /// This is the one copy.
    Run(Option<Box<dyn Held>>),
    /// Another copy is running and has been asked to show its window.
    Shown,
    /// Another copy is running and could not be asked.
    Running,
    /// This copy has started one with administrator rights and leaves.
    Elevating,
    /// Administrator rights are needed and were not given: why.
    NotElevated(String),
}

/// See the top of the file.  `needs_administrator` is false in a trial build.
pub fn begin(session: &dyn Session, needs_administrator: bool, arguments: &[String]) -> Begin {
    if session.ask_other_to_show() {
        return Begin::Shown;
    }
    if session.another_is_running() {
        return Begin::Running;
    }
    if needs_administrator && !session.has_administrator_rights() {
        if arguments.iter().any(|argument| argument == ASKED_FOR_RIGHTS) {
            return Begin::NotElevated("Windows started it again as asked, but still without administrator rights".to_string());
        }
        let mut marked = arguments.to_vec();
        marked.push(ASKED_FOR_RIGHTS.to_string());
        return match session.start_with_administrator_rights(&marked) {
            Ok(()) => Begin::Elevating,
            Err(why) => Begin::NotElevated(why),
        };
    }
    match session.become_the_one() {
        Ok(Some(held)) => Begin::Run(Some(held)),
        // Two starts at the same moment: the other one was first.
        Ok(None) if session.ask_other_to_show() => Begin::Shown,
        Ok(None) => Begin::Running,
        // The name could not be taken at all.  As the app has always done:
        // a second copy is a nuisance, an app that does not start is worse.
        Err(_) => Begin::Run(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    /// A session that answers as it is told and writes down what it was asked.
    #[derive(Default)]
    struct Told {
        shows_other: Vec<bool>,
        another: bool,
        administrator: bool,
        elevation: Option<Result<(), String>>,
        becomes: Option<Result<bool, String>>,
        asked: RefCell<Vec<&'static str>>,
        shows: RefCell<usize>,
    }

    struct Nothing;
    impl Held for Nothing {
        fn when_asked_to_show(&self, _show: Box<dyn Fn() + Send>) {}
    }

    impl Session for Told {
        fn ask_other_to_show(&self) -> bool {
            self.asked.borrow_mut().push("show");
            let n = *self.shows.borrow();
            *self.shows.borrow_mut() += 1;
            self.shows_other.get(n).copied().unwrap_or(false)
        }
        fn another_is_running(&self) -> bool {
            self.asked.borrow_mut().push("running");
            self.another
        }
        fn has_administrator_rights(&self) -> bool {
            self.asked.borrow_mut().push("rights");
            self.administrator
        }
        fn start_with_administrator_rights(&self, arguments: &[String]) -> Result<(), String> {
            assert_eq!(arguments, ["--from-autostart".to_string(), ASKED_FOR_RIGHTS.to_string()], "marked, once");
            self.asked.borrow_mut().push("elevate");
            self.elevation.clone().expect("not expected to ask for rights")
        }
        fn become_the_one(&self) -> Result<Option<Box<dyn Held>>, String> {
            self.asked.borrow_mut().push("become");
            match self.becomes.clone().expect("not expected to take the name") {
                Ok(true) => Ok(Some(Box::new(Nothing))),
                Ok(false) => Ok(None),
                Err(why) => Err(why),
            }
        }
    }

    fn start(session: &Told, needs_administrator: bool) -> (&'static str, Vec<&'static str>) {
        let how = match begin(session, needs_administrator, &["--from-autostart".to_string()]) {
            Begin::Run(Some(_)) => "run",
            Begin::Run(None) => "run without the name",
            Begin::Shown => "shown",
            Begin::Running => "running",
            Begin::Elevating => "elevating",
            Begin::NotElevated(_) => "not elevated",
        };
        (how, session.asked.borrow().clone())
    }

    #[test]
    fn a_second_start_shows_the_first_and_never_asks_for_rights() {
        let second = Told { shows_other: vec![true], ..Told::default() };
        assert_eq!(start(&second, true), ("shown", vec!["show"]), "the prompt would have come for nothing");
        // An older build is running, which cannot be asked: leave all the same.
        let beside_old = Told { another: true, ..Told::default() };
        assert_eq!(start(&beside_old, true), ("running", vec!["show", "running"]));
    }

    #[test]
    fn the_first_start_asks_for_rights_and_only_then_takes_the_name() {
        // Without rights: start again with them, and do not hold the name
        // meanwhile (the copy with rights has to be able to take it).
        let plain = Told { elevation: Some(Ok(())), ..Told::default() };
        assert_eq!(start(&plain, true), ("elevating", vec!["show", "running", "rights", "elevate"]));
        // With rights (the copy just started, or a start at sign-in): run.
        let elevated = Told { administrator: true, becomes: Some(Ok(true)), ..Told::default() };
        assert_eq!(start(&elevated, true), ("run", vec!["show", "running", "rights", "become"]));
        // The prompt was answered with no.
        let refused = Told { elevation: Some(Err("The operation was canceled by the user.".into())), ..Told::default() };
        assert_eq!(start(&refused, true), ("not elevated", vec!["show", "running", "rights", "elevate"]));
    }

    #[test]
    fn asking_for_rights_does_not_go_round_in_a_circle() {
        // The copy that was started to get rights, and still has none: it
        // says so and stops.  It does not start a third.
        let again = Told::default();
        let how = begin(&again, true, &["--from-autostart".to_string(), ASKED_FOR_RIGHTS.to_string()]);
        let Begin::NotElevated(why) = how else { panic!("it went on") };
        assert!(why.contains("still without administrator rights"), "{why}");
        assert_eq!(*again.asked.borrow(), ["show", "running", "rights"], "nothing was started");
        // With rights, the mark changes nothing.
        let fine = Told { administrator: true, becomes: Some(Ok(true)), ..Told::default() };
        assert!(matches!(begin(&fine, true, &[ASKED_FOR_RIGHTS.to_string()]), Begin::Run(Some(_))));
    }

    #[test]
    fn a_trial_build_never_asks_for_rights() {
        let trial = Told { becomes: Some(Ok(true)), ..Told::default() };
        assert_eq!(start(&trial, false), ("run", vec!["show", "running", "become"]));
    }

    #[test]
    fn two_starts_at_the_same_moment_end_as_one_copy() {
        // The other one took the name first, and by now can be asked.
        let late = Told { administrator: true, becomes: Some(Ok(false)), shows_other: vec![false, true], ..Told::default() };
        assert_eq!(start(&late, true), ("shown", vec!["show", "running", "rights", "become", "show"]));
        // ... or cannot be asked yet: leave anyway, there is a copy.
        let later = Told { administrator: true, becomes: Some(Ok(false)), ..Told::default() };
        assert_eq!(start(&later, true).0, "running");
    }

    #[test]
    fn a_name_that_cannot_be_taken_does_not_keep_the_app_from_starting() {
        let odd = Told { administrator: true, becomes: Some(Err("Windows error 8".into())), ..Told::default() };
        assert_eq!(start(&odd, true).0, "run without the name");
    }
}
