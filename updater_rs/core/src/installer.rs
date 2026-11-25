//! The install pipeline: does the actual work.  A port of updater/installer.py
//! step for step, message for message and exit code for exit code; the Python
//! tests for that file are this one's specification (tests/pipeline.rs).
//!
//! Two modes:
//!   * update mode: Vispora exists somewhere on disk.  The build is looked
//!     for, any running instance closed, the build downloaded and checked,
//!     the exe swapped, and the app started again.
//!   * install mode: Vispora is not on disk.  The same, minus the close.
//!
//! Steps, with a look at "cancelled" between each:
//!   1. channel   ask GitHub's releases for the newest build, then the update
//!                server's /version/<channel> if GitHub has none
//!   2. close     find and close any running Vispora
//!   3. download  to a .part file, with progress
//!   4. verify    SHA-256 against the hash that place gave
//!   5. install   back up the old exe, copy the new one into its place
//!   6. launch    start the new Vispora
//!
//! Worth keeping in mind:
//!   * The close step asks politely first (taskkill without /F sends
//!     WM_CLOSE), then falls back to taskkill /F.
//!   * The download stays a .part file until its hash has passed: half a
//!     download can never end up at the target path.
//!   * The backup is <exe>.bak.<time>.  Once an install succeeds only the
//!     backup it has just made is kept.
//!   * If the copy fails, or the new exe will not start, the backup is put
//!     back.
//!   * install_info.json is rewritten after a successful install, so that a
//!     later run finds the right state.

use std::io::Write as _;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};

use crate::json::Json;
use crate::paths::{self, Env, Paths, APP_EXE_NAMES, APP_NAME, LEGACY_APP_NAME};
use crate::platform::Platform;
use crate::sha256::Sha256;
use crate::sources::{self, parse_version, source_name};
use crate::state::{fresh_steps, State};

type ExitHook = Arc<dyn Fn(i32) + Send + Sync>;
type FolderHook = Arc<dyn Fn(&str) -> Result<Option<String>, String> + Send + Sync>;

/// The updater: where things are, its doors to the outside, and its state.
pub struct Updater {
    pub env: Env,
    pub paths: Paths,
    pub platform: Platform,
    pub state: Mutex<State>,
    /// Whether an install is running beside the window, and whether the
    /// updater is closing (see start_install, stop_install, shut_down).
    pub(crate) run: Mutex<Run>,
    /// Rung when a running install has ended, for everybody who waits for it.
    pub(crate) run_ended: Condvar,
    /// Ends the process (the window's Close button, once the pipeline has stopped).
    pub exit_hook: Mutex<Option<ExitHook>>,
    /// The "pick a folder" dialog: the folder, or None when it was cancelled.
    pub folder_hook: Mutex<Option<FolderHook>>,
}

/// The install that runs beside the window.
#[derive(Default)]
pub(crate) struct Run {
    /// The pipeline is on its thread and has not come back yet.
    alive: bool,
    /// The updater is on its way out: nothing new is begun.
    closing: bool,
}

/// A build one of the places has.
#[derive(Debug, Clone, PartialEq)]
pub struct Release {
    pub source: &'static str,
    pub version: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub download_url: String,
    pub notes: Json,
    pub released_at: Json,
}

impl Release {
    fn to_json(&self) -> Json {
        Json::object([
            ("ok", true.into()),
            ("source", self.source.into()),
            ("version", self.version.as_str().into()),
            ("sha256", self.sha256.as_str().into()),
            ("size_bytes", self.size_bytes.into()),
            ("download_url", self.download_url.as_str().into()),
            ("notes", self.notes.clone()),
            ("released_at", self.released_at.clone()),
        ])
    }
}

/// A finished download: still a .part file, with its hash.
#[derive(Debug, Clone, PartialEq)]
pub struct Downloaded {
    pub sha256: String,
    pub size_bytes: u64,
    pub path: String,
}

/// How the close step ended.
#[derive(Debug, Clone, PartialEq)]
pub struct Closed {
    pub ok: bool,
    pub msg: String,
    pub skipped: bool,
}

/// How the swap ended.
#[derive(Debug, Clone, PartialEq)]
pub struct Installed {
    pub ok: bool,
    pub msg: String,
    pub backup_path: String,
}

/// A size as the server gave it, 0 when it is not a number.
/// (Python: `max(0, int(value or 0))`, 0 when that fails.)
fn whole_number(value: &Json) -> u64 {
    let n: i128 = match value {
        Json::Bool(true) => 1,
        Json::Int(n) => *n,
        Json::Float(f) if f.is_finite() => f.trunc() as i128,
        Json::Str(s) => sources::py_int(s).map_or(0, |n| n as i128),
        _ => 0,
    };
    n.clamp(0, u64::MAX as i128) as u64
}

/// A version as part of a file name.  A version is digits, letters, dots,
/// plus and minus; anything else in what a server sent (a slash, say) must
/// not get to choose where the download is written.
fn file_safe(version: &str) -> String {
    version.chars().map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '-') { c } else { '_' }).collect()
}

/// The first `n` characters of `text`.
fn head(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

impl Updater {
    pub fn new(env: Env, platform: Platform) -> Arc<Updater> {
        let paths = Paths::resolve(&env);
        Arc::new(Updater {
            env,
            paths,
            platform,
            state: Mutex::new(State::default()),
            run: Mutex::new(Run::default()),
            run_ended: Condvar::new(),
            exit_hook: Mutex::new(None),
            folder_hook: Mutex::new(None),
        })
    }

    /// Append a line to updater.log, with the time.  Best effort.
    pub fn log(&self, line: &str) {
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&self.paths.log_path) {
            let _ = writeln!(f, "[{}] {line}", self.platform.clock.stamp());
        }
    }

    // ─── State ───────────────────────────────────────────────────────

    pub fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn snapshot(&self) -> State {
        self.lock().clone()
    }

    /// Seed the install context (called once, before the window opens).
    pub fn init_state(&self, seed: State) {
        let mut st = self.lock();
        *st = seed;
        st.steps = fresh_steps();
    }

    /// Mark a step as pending / active / done / error / skipped.  An empty
    /// detail leaves the one that is there.
    pub fn update_step(&self, key: &str, status: &str, detail: &str) {
        let mut st = self.lock();
        if let Some(s) = st.steps.iter_mut().find(|s| s.key == key) {
            s.status = status.to_string();
            if !detail.is_empty() {
                s.detail = detail.to_string();
            }
        }
    }

    pub fn update_progress(&self, progress: i64, downloaded: u64, total: u64, phase: &str, phase_label: &str) {
        let mut st = self.lock();
        st.progress = progress.clamp(0, 100);
        if downloaded != 0 {
            st.downloaded_bytes = downloaded;
        }
        if total != 0 {
            st.total_bytes = total;
        }
        if !phase.is_empty() {
            st.phase = phase.to_string();
        }
        if !phase_label.is_empty() {
            st.phase_label = phase_label.to_string();
        }
    }

    pub fn set_error(&self, msg: &str) {
        let now = self.platform.clock.now();
        let mut st = self.lock();
        st.phase = "error".into();
        st.phase_label = "Error".into();
        st.error = Some(msg.to_string());
        st.ended_at = now;
    }

    pub fn is_cancelled(&self) -> bool {
        self.lock().cancelled
    }

    pub fn set_done(&self) {
        let now = self.platform.clock.now();
        let mut st = self.lock();
        st.phase = "done".into();
        st.phase_label = "Update complete".into();
        st.progress = 100;
        st.ended_at = now;
    }

    fn check_cancelled(&self) -> bool {
        if self.is_cancelled() {
            self.log("install cancelled by user");
            return true;
        }
        false
    }

    // ─── Finding the install ─────────────────────────────────────────

    /// The app-exe path inside `target_dir`.  An install that has been
    /// updated in place since before the rename still has its exe under the
    /// old file name (shortcuts, taskbar pins and the autostart task all
    /// point at it), so an existing exe under either name is reused as it
    /// is; a fresh install gets Vispora.exe.
    pub fn pick_app_exe(&self, target_dir: &str) -> String {
        for name in APP_EXE_NAMES {
            let p = paths::join(target_dir, name);
            if paths::is_file(&p) {
                return p;
            }
        }
        paths::join(target_dir, APP_EXE_NAMES[0])
    }

    /// What the app wrote about its own install (path, channel, version), or
    /// an empty object when there is no such record or it cannot be read.
    pub fn load_install_info(&self) -> Json {
        let empty = Json::Object(vec![]);
        let Ok(bytes) = std::fs::read(&self.paths.install_info) else {
            return empty;
        };
        match Json::parse_bytes(&bytes) {
            Ok(info) if info.is_object() => info,
            Ok(_) => empty,
            Err(e) => {
                self.log(&format!("install_info.json load failed: {e}"));
                empty
            }
        }
    }

    /// Locate an installed copy of the app:
    /// `{found, path, dir, source: "install_info" | "standard" | "", channel,
    /// version, candidates: [what was looked at]}`.
    ///
    /// Fastest first: install_info.json in the app's data folder (written by
    /// the app at every start, and it also gives the channel and version),
    /// then the standard install folders under both the current and the
    /// pre-rename name.  No scan of the whole disk: for an install in an odd
    /// place the window's folder picker is the way.
    pub fn find_existing_install(&self) -> Json {
        let answer = |found: bool, path: &str, source: &str, channel: Json, version: Json, candidates: &[String]| {
            Json::object([
                ("found", found.into()),
                ("path", path.into()),
                ("dir", if found { paths::dirname(path).into() } else { "".into() }),
                ("source", source.into()),
                ("channel", channel),
                ("version", version),
                ("candidates", candidates.to_vec().into()),
            ])
        };
        let info_path = &self.paths.install_info;
        let mut candidates = vec![format!("install_info.json @ {info_path}")];
        if paths::is_file(info_path) {
            match std::fs::read(info_path).map_err(|e| e.to_string()).and_then(|b| Json::parse_bytes(&b)) {
                Ok(info) => {
                    // (a record that is not an object reads as an empty one)
                    let recorded = info["install_path"].as_str().unwrap_or("");
                    if paths::is_file(recorded) {
                        self.log(&format!("found via install_info.json: {recorded}"));
                        // (what the record says, or "" where it does not say)
                        let said = |key: &str| info.get(key).cloned().unwrap_or_else(|| "".into());
                        return answer(true, recorded, "install_info", said("channel"), said("version"), &candidates);
                    }
                }
                Err(e) => self.log(&format!("install_info.json parse failed: {e}")),
            }
        }

        let env = &self.env;
        let mut folders = Vec::new();
        for name in [APP_NAME, LEGACY_APP_NAME] {
            folders.push(paths::join(&env.program_files, name));
            folders.push(paths::join(&env.program_files_x86, name));
            folders.push(paths::join(&paths::join(&env.localappdata, "Programs"), name));
            folders.push(paths::join(&env.localappdata, name));
            folders.push(paths::join(&paths::join(&env.home, "Documents"), name));
        }
        folders.push(paths::join(&env.home, "Desktop"));
        folders.push(paths::join(&env.home, "Downloads"));
        for folder in &folders {
            for name in APP_EXE_NAMES {
                let p = paths::join(folder, name);
                candidates.push(p.clone());
                if paths::is_file(&p) {
                    self.log(&format!("found via standard path: {p}"));
                    return answer(true, &p, "standard", "".into(), "".into(), &candidates);
                }
            }
        }
        self.log(&format!("no existing install found across {} candidates", candidates.len()));
        answer(false, "", "", "".into(), "".into(), &candidates)
    }

    /// The folder suggested for a fresh install: Program Files (the updater
    /// runs as administrator), or the user's own Programs folder where there
    /// is no Program Files.  Only a suggestion: nothing is created here.
    pub fn default_install_dir(&self) -> String {
        if paths::is_dir(&self.env.program_files) {
            return paths::join(&self.env.program_files, APP_NAME);
        }
        paths::join(&paths::join(&self.env.localappdata, "Programs"), APP_NAME)
    }

    // ─── Step: close the running app ─────────────────────────────────

    /// The process ids of every running process named `image_name`.
    ///
    /// The caller passes the name of the exe being replaced, not a fixed
    /// one: an install updated in place since before the rename still runs
    /// under its old file name, and a process this cannot find is a locked
    /// exe the swap would then fail on.
    pub fn find_app_pids(&self, image_name: &str) -> Vec<u32> {
        let args: Vec<String> =
            ["tasklist", "/FI", &format!("IMAGENAME eq {image_name}"), "/FO", "CSV", "/NH"].map(String::from).to_vec();
        let out = match self.platform.procs.run(&args, 4) {
            Ok(out) => out,
            Err(e) => {
                self.log(&format!("tasklist failed: {e}"));
                return Vec::new();
            }
        };
        let mut pids = Vec::new();
        for line in out.lines() {
            // "Vispora.exe","1234","Console","1","12 K"
            let line = line.trim().trim_matches('"');
            if line.is_empty() {
                continue;
            }
            let parts: Vec<&str> = line.split("\",\"").map(|p| p.trim_matches('"')).collect();
            if parts.len() >= 2 {
                if let Ok(pid) = parts[1].trim().parse::<u32>() {
                    pids.push(pid);
                }
            }
        }
        pids
    }

    fn taskkill(&self, pid: u32, force: bool) {
        let mut args = vec!["taskkill".to_string()];
        if force {
            args.push("/F".into());
        }
        args.extend(["/PID".to_string(), pid.to_string()]);
        if let Err(e) = self.platform.procs.run(&args, 4) {
            self.log(&format!("{} taskkill on pid={pid} failed: {e}", if force { "force" } else { "graceful" }));
        }
    }

    /// Ask the app to close, and fall back to taskkill /F.
    pub fn close_app(&self, target_exe: &str, timeout_s: f64) -> Closed {
        let done = |ok: bool, msg: String| Closed { ok, msg, skipped: false };
        let clock = &self.platform.clock;
        let image = match paths::basename(target_exe) {
            "" => APP_EXE_NAMES[0],
            name => name,
        };
        let pids = self.find_app_pids(image);
        if pids.is_empty() {
            self.log(&format!("no running {image}, skipping close step"));
            return Closed { ok: true, msg: "not running".into(), skipped: true };
        }
        self.log(&format!("{image} running, pids={pids:?}"));
        self.update_step("close", "active", &format!("Closing {} {APP_NAME} process(es)\u{2026}", pids.len()));

        // Politely first: taskkill without /F sends WM_CLOSE, which the app's
        // window takes as "close".
        for &pid in &pids {
            self.taskkill(pid, false);
        }
        let deadline = clock.now() + timeout_s / 2.0;
        while clock.now() < deadline {
            if self.check_cancelled() {
                return done(false, "cancelled".into());
            }
            if self.find_app_pids(image).is_empty() {
                self.log(&format!("{image} exited gracefully"));
                return done(true, format!("closed {} process(es) gracefully", pids.len()));
            }
            clock.sleep(0.5);
        }

        self.log("graceful close timed out, escalating to taskkill /F");
        self.update_step("close", "active", "Force-closing stuck process\u{2026}");
        for pid in self.find_app_pids(image) {
            self.taskkill(pid, true);
        }
        let deadline = clock.now() + timeout_s / 2.0;
        while clock.now() < deadline {
            if self.check_cancelled() {
                return done(false, "cancelled".into());
            }
            if self.find_app_pids(image).is_empty() {
                return done(true, "force-closed".into());
            }
            clock.sleep(0.5);
        }
        let remaining = self.find_app_pids(image);
        done(false, format!("could not close {APP_NAME} (pids still alive: {remaining:?})"))
    }

    // ─── Step: find the build ────────────────────────────────────────

    fn user_agent(&self) -> String {
        format!("{APP_NAME}Updater/{}", self.lock().updater_version)
    }

    fn http_get_json(&self, url: &str, headers: &[(&str, &str)]) -> Option<Json> {
        let agent = self.user_agent();
        let mut all = vec![("User-Agent", agent.as_str())];
        all.extend_from_slice(headers);
        let answer = self.platform.net.get(url, &all, 15).and_then(|body| Json::parse_bytes(&body));
        match answer {
            Ok(value) => Some(value),
            Err(e) => {
                self.log(&format!("GET {url} failed: {e}"));
                None
            }
        }
    }

    /// Ask the update server's /version/<channel> for its newest build.
    pub fn check_channel(&self, server_url: &str, channel: &str) -> Result<Release, String> {
        let base = server_url.trim_end_matches('/');
        let endpoint = format!("{base}/version/{channel}");
        let data = match self.http_get_json(&endpoint, &[]) {
            Some(data) if data.is_object() && data.truthy() => data,
            _ => return Err(format!("could not reach {endpoint}")),
        };
        if !data["available"].truthy() || !data["version"].truthy() {
            return Err(format!("no release published on '{channel}'"));
        }
        let or_empty = |v: &Json| if v.truthy() { v.clone() } else { "".into() };
        Ok(Release {
            source: "server",
            version: data["version"].py_str(),
            sha256: data["sha256"].py_str_or_empty(),
            size_bytes: whole_number(&data["size_bytes"]),
            download_url: if data["download_url"].truthy() {
                data["download_url"].py_str()
            } else {
                format!("{base}/download/{channel}")
            },
            notes: or_empty(&data["notes"]),
            released_at: or_empty(&data["released_at"]),
        })
    }

    /// What GitHub's releases have for `channel` (see sources::github_app_release
    /// for what counts as a release there).
    pub fn check_github(&self, channel: &str) -> Result<Release, String> {
        let data = match self.http_get_json(sources::GITHUB_RELEASES_URL, &sources::GITHUB_HEADERS) {
            Some(data) if data.as_array().is_some() => data,
            _ => return Err("could not reach GitHub".into()),
        };
        let Some(offer) = sources::github_app_release(&data, channel) else {
            return Err(format!("no release published on '{channel}' on GitHub"));
        };
        Ok(Release {
            source: "github",
            version: offer.version,
            sha256: offer.sha256,
            size_bytes: offer.size_bytes.clamp(0, u64::MAX as i128) as u64,
            download_url: offer.download_url,
            notes: offer.notes.into(),
            released_at: offer.released_at.into(),
        })
    }

    /// Ask the places releases are published, in their order (GitHub first
    /// and the update server second unless told otherwise, see
    /// sources::source_order), and take the first that has a build for the
    /// channel.  `skip`: places already tried (their file could not be
    /// downloaded or failed its check).
    ///
    /// A place that only has an older build than the floor is passed over,
    /// and if no place reaches the floor nothing is installed.  The floor is
    /// the version the app asked for (--target-version): the app found its
    /// update in one place, and the other may be behind.  Started by hand,
    /// it is the version that is installed, unless another channel was
    /// picked for this run: going back from beta to stable is asked for, a
    /// downgrade because one place is behind is not.
    pub fn find_release(&self, st: &State, skip: &[&str]) -> Result<Release, String> {
        let (mut floor, mut floor_is) = (st.requested_version.as_str(), "that was asked for");
        if floor.is_empty() && st.mode == "update" && !st.allow_older {
            (floor, floor_is) = (st.current_version.as_str(), "that is installed");
        }
        let mut errors: Vec<String> = Vec::new();
        for source in sources::source_order(&st.source, &st.server_url) {
            if skip.contains(&source) {
                continue;
            }
            let found = if source == "github" {
                self.check_github(&st.channel)
            } else {
                self.check_channel(&st.server_url, &st.channel)
            };
            let info = match found {
                Ok(info) => info,
                Err(e) => {
                    errors.push(e);
                    continue;
                }
            };
            if !floor.is_empty() && parse_version(&info.version) < parse_version(floor) {
                errors.push(format!(
                    "{} has v{}, older than the v{floor} {floor_is}",
                    source_name(source),
                    info.version
                ));
                continue;
            }
            return Ok(info);
        }
        Err(if errors.is_empty() { "nowhere else to look for a release".to_string() } else { errors.join("; ") })
    }

    /// Show and record the build that will be installed; returns its version.
    fn note_found(&self, st: &State, info: &Release, instead_of: &str) -> String {
        let mut place = source_name(info.source).to_string();
        if !instead_of.is_empty() {
            place += &format!("; the file from {instead_of} could not be used");
        }
        self.update_step("channel", "done", &format!("Latest on '{}': v{} (from {place})", st.channel, info.version));
        let mut live = self.lock();
        live.target_version = info.version.clone();
        live.channel_check = info.to_json();
        info.version.clone()
    }

    // ─── Steps: download and verify ──────────────────────────────────

    /// Download `url` to `dest`.part, reporting progress.  The answer says
    /// where the .part file is, how large it is and what its SHA-256 is.
    pub fn download(&self, url: &str, dest: &str, total_hint: u64) -> Result<Downloaded, String> {
        let tmp = format!("{dest}.part");
        let remove_tmp = || {
            let _ = std::fs::remove_file(&tmp);
        };
        std::fs::create_dir_all(paths::dirname(dest)).map_err(|e| e.to_string())?;
        let agent = self.user_agent();
        let result: Result<Downloaded, String> = (|| {
            let mut body = self.platform.net.open(url, &[("User-Agent", agent.as_str())], 60)?;
            let total = body.content_length().unwrap_or(total_hint);
            self.update_progress(0, 0, total, "downloading", "Downloading");
            let mut file = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
            let mut hash = Sha256::new();
            let mut downloaded: u64 = 0;
            let mut buf = vec![0u8; 65536];
            loop {
                if self.check_cancelled() {
                    drop(file); // Windows will not delete a file that is still open
                    return Err("cancelled".into());
                }
                let n = body.read(&mut buf)?;
                if n == 0 {
                    break;
                }
                file.write_all(&buf[..n]).map_err(|e| e.to_string())?;
                hash.update(&buf[..n]);
                downloaded += n as u64;
                let pct = if total != 0 { (downloaded as f64 / total as f64 * 100.0) as i64 } else { 0 };
                self.update_progress(pct, downloaded, total, "", "");
            }
            file.flush().map_err(|e| e.to_string())?;
            drop(file);
            let size = std::fs::metadata(&tmp).map_err(|e| e.to_string())?.len();
            if size < 1024 * 100 {
                // under 100 KB: an error page, not a build
                return Err(format!("downloaded file is {size} B, likely a server error page"));
            }
            Ok(Downloaded { sha256: hash.hex(), size_bytes: size, path: tmp.clone() })
        })();
        if result.is_err() {
            remove_tmp();
        }
        result
    }

    // ─── Step: install (with backup and rollback) ────────────────────

    /// Delete the rollback backups earlier updates left next to
    /// `target_exe`, keeping only `keep` (the one this run has just made).
    /// Without this every update leaves a full copy of the previous exe
    /// behind for good.
    fn prune_old_backups(&self, target_exe: &str, keep: &str) {
        let folder = match paths::dirname(target_exe) {
            f if f.is_empty() => ".".to_string(),
            f => f,
        };
        let prefix = format!("{}.bak.", paths::basename(target_exe)).to_lowercase();
        let Ok(entries) = std::fs::read_dir(&folder) else {
            return;
        };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let is_backup = name
                .to_lowercase()
                .strip_prefix(&prefix)
                .is_some_and(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()));
            let path = paths::join(&folder, &name);
            if !is_backup || paths::same_path(&path, keep) {
                continue;
            }
            match std::fs::remove_file(&path) {
                Ok(()) => self.log(&format!("removed old backup {path}")),
                Err(e) => self.log(&format!("could not remove old backup {path}: {e}")),
            }
        }
    }

    /// Put the downloaded file in at `target_exe`, with a backup to roll back
    /// to.  In install mode the target does not exist yet: the folder is
    /// made and the file copied in.  In update mode the existing exe is
    /// backed up first and put back if the copy fails.
    ///
    /// Err: something that should not happen (the folder cannot be made).
    pub fn install_exe(&self, staged_path: &str, target_exe: &str) -> Result<Installed, String> {
        let disk = &self.platform.disk;
        let target_dir = match paths::dirname(target_exe) {
            d if d.is_empty() => ".".to_string(),
            d => d,
        };
        std::fs::create_dir_all(&target_dir).map_err(|e| e.to_string())?;

        let mut backup = String::new();
        let pre_existed = std::path::Path::new(target_exe).exists();
        if pre_existed {
            backup = format!("{target_exe}.bak.{}", self.platform.clock.now() as i64);
            match disk.copy(target_exe, &backup) {
                Ok(()) => self.log(&format!("backed up old exe to {backup}")),
                Err(e) => {
                    self.log(&format!("backup failed: {e}"));
                    // Without a backup, a failed copy below would leave no
                    // exe at all, so the old one stays where it is.  (A full
                    // disk leaves a partial backup behind.)
                    let _ = std::fs::remove_file(&backup);
                    return Ok(Installed {
                        ok: false,
                        msg: format!("could not back up the old exe: {e}"),
                        backup_path: String::new(),
                    });
                }
            }
            // Out of the way, so that the copy is unambiguous.
            if let Err(e) = std::fs::remove_file(target_exe) {
                return Ok(Installed { ok: false, msg: format!("could not delete old exe: {e}"), backup_path: backup });
            }
        }

        // With retries: antivirus often holds a new exe for a second after it
        // is written.
        let mut last_err = String::new();
        for attempt in 1..=20 {
            match disk.copy(staged_path, target_exe) {
                Ok(()) => {
                    let _ = std::fs::remove_file(staged_path);
                    self.log(&format!("installed new exe at {target_exe}"));
                    // Only once a fresh backup exists: if this run's backup
                    // had failed, the previous one would be the only way back.
                    if !backup.is_empty() {
                        self.prune_old_backups(target_exe, &backup);
                    }
                    return Ok(Installed { ok: true, msg: format!("installed (attempt {attempt})"), backup_path: backup });
                }
                Err(e) => {
                    last_err = e.to_string();
                    self.log(&format!("install copy attempt {attempt}/20 failed: {e}"));
                    self.platform.clock.sleep(1.0);
                }
            }
        }

        // Every attempt failed: put the backup back, if there is one.
        let mut rolled_back = false;
        if !backup.is_empty() && std::path::Path::new(&backup).exists() {
            match disk.copy(&backup, target_exe) {
                Ok(()) => {
                    rolled_back = true;
                    self.log("rolled back to backup");
                }
                Err(e) => self.log(&format!("rollback failed: {e}")),
            }
        }
        Ok(Installed {
            ok: false,
            msg: format!("install copy failed: {last_err}; rolled_back={}", if rolled_back { "True" } else { "False" }),
            backup_path: backup,
        })
    }

    // ─── Step: launch, and the record for next time ──────────────────

    pub fn launch_new(&self, target_exe: &str) -> Result<(), String> {
        self.platform.procs.spawn(&[target_exe.to_string()])
    }

    /// Rewrite install_info.json with the new state, so that a later run of
    /// the updater finds it.
    pub fn update_install_info(&self, target_exe: &str, channel: &str, version: &str) {
        let mut d = Json::object([
            ("install_path", target_exe.into()),
            ("install_dir", paths::dirname(target_exe).into()),
            ("channel", channel.into()),
            ("version", version.into()),
            ("updated_at", self.platform.clock.now().into()),
        ]);
        // Where this install gets its updates is the app's to say: keep what
        // it wrote.
        let old = self.load_install_info();
        if old["update_source"].truthy() {
            d.set("update_source", old["update_source"].clone());
        }
        let written = std::fs::create_dir_all(paths::dirname(&self.paths.install_info))
            .and_then(|()| std::fs::write(&self.paths.install_info, d.to_pretty()));
        match written {
            Ok(()) => self.log(&format!("updated install_info.json: {d}")),
            Err(e) => self.log(&format!("install_info.json write failed: {e}")),
        }
    }

    // ─── The whole sequence ──────────────────────────────────────────

    /// Run the full sequence.  0 when it worked; otherwise which step it
    /// stopped at: 1 cancelled, 2 nothing to go on, 3 close, 4 no build
    /// found, 5 download, 6 verify, 7 install, 8 launch, 9 the unexpected.
    ///
    /// Whatever goes wrong inside is shown in the window as an error: it
    /// must not leave a step spinning with the app closed and nothing said.
    pub fn run_install_pipeline(&self) -> i32 {
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.pipeline()));
        let why = match outcome {
            Ok(Ok(code)) => return code,
            Ok(Err(why)) => why,
            Err(panic) => panic
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| panic.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "an internal error".to_string()),
        };
        self.log(&format!("pipeline stopped on an unexpected error: {why}"));
        let active: Vec<&'static str> =
            self.lock().steps.iter().filter(|s| s.status == "active").map(|s| s.key).collect();
        for key in active {
            self.update_step(key, "error", "stopped unexpectedly");
        }
        self.set_error(&format!("The update stopped on an unexpected error: {why}"));
        9
    }

    fn pipeline(&self) -> Result<i32, String> {
        let st = self.snapshot();
        let mode = st.mode.as_str();
        self.log(&format!("pipeline start: mode={mode} target={} channel={}", st.target_exe, st.channel));

        // Both are needed before anything can be installed.
        if st.target_exe.is_empty() {
            self.set_error(&format!("No target install path: please pick where {APP_NAME} lives."));
            return Ok(2);
        }
        if st.channel.is_empty() {
            self.set_error("No release channel: please pick stable or beta.");
            return Ok(2);
        }

        // ── Find the build (GitHub, then the update server) ──
        // Before anything is closed: when no place has a build to install,
        // an app that is running keeps running.
        if self.check_cancelled() {
            return Ok(1);
        }
        self.update_step("channel", "active", &format!("Looking for the newest {} build\u{2026}", st.channel));
        let mut info = match self.find_release(&st, &[]) {
            Ok(info) => info,
            Err(e) => {
                self.update_step("channel", "error", &e);
                self.set_error(&e);
                return Ok(4);
            }
        };
        let mut target_version = self.note_found(&st, &info, "");

        // ── Close the app (not in install mode) ──
        if self.check_cancelled() {
            return Ok(1);
        }
        if mode == "install" {
            // Nothing to close.  The folder may not exist yet (the user may
            // have typed one): make it now.
            let folder = match paths::dirname(&st.target_exe) {
                d if d.is_empty() => ".".to_string(),
                d => d,
            };
            if let Err(e) = std::fs::create_dir_all(&folder) {
                self.update_step("close", "error", &format!("can't create dir: {e}"));
                self.set_error(&format!("Couldn't create install directory: {e}"));
                return Ok(3);
            }
            self.update_step("close", "skipped", "fresh install, nothing to close");
        } else {
            let r = self.close_app(&st.target_exe, 30.0);
            if !r.ok {
                self.update_step("close", "error", &r.msg);
                self.set_error(&format!("{}. Please close {APP_NAME} manually and click Retry.", r.msg));
                return Ok(3);
            }
            self.update_step("close", "done", &r.msg);
        }

        // ── Download and verify ──
        // When the file from one place cannot be downloaded or fails its
        // check, the next place is asked for the build before giving up.
        let mut tried: Vec<&'static str> = Vec::new();
        let dl = loop {
            if self.check_cancelled() {
                return Ok(1);
            }
            let staged = paths::join(&self.paths.updater_dir, &format!("{APP_NAME}-{}.exe", file_safe(&target_version)));
            self.update_step("download", "active", &format!("Downloading v{target_version}\u{2026}"));
            let dl = self.download(&info.download_url, &staged, info.size_bytes);
            // (step, exit code, what the step shows, what the window shows)
            let failed: Option<(&str, i32, String, String)> = match &dl {
                Err(e) => Some(("download", 5, e.clone(), format!("Download failed: {e}"))),
                Ok(d) => {
                    self.update_step("download", "done", &format!("Downloaded {} MB", d.size_bytes / (1024 * 1024)));
                    if self.check_cancelled() {
                        return Ok(1);
                    }
                    self.update_step("verify", "active", "Computing SHA-256\u{2026}");
                    let expected = info.sha256.to_lowercase();
                    let actual = d.sha256.to_lowercase();
                    let bad = if expected.is_empty() {
                        // Closed, not open: a missing hash looks the same as
                        // one that was stripped on the way, and installing an
                        // unchecked program is what this check is there to
                        // prevent.
                        Some("Server did not provide a SHA-256 hash for this build; \
                              refusing to install an unverified binary."
                            .to_string())
                    } else if actual != expected {
                        Some(format!(
                            "SHA-256 mismatch: expected {}\u{2026}, got {}\u{2026}.  Aborting to avoid installing \
                             a corrupted or tampered build.",
                            head(&expected, 16),
                            head(&actual, 16)
                        ))
                    } else {
                        None
                    };
                    if bad.is_some() {
                        let _ = std::fs::remove_file(&d.path);
                    }
                    bad.map(|err| ("verify", 6, err.clone(), err))
                }
            };
            let Some((step, code, detail, err)) = failed else {
                break dl.expect("a download that did not fail");
            };
            tried.push(info.source);
            let cancelled = matches!(&dl, Err(e) if e == "cancelled");
            let other = if cancelled { None } else { self.find_release(&st, &tried).ok() };
            match other {
                // (never the same place twice: no endless retry)
                Some(other) if !tried.contains(&other.source) => {
                    self.log(&format!("{err}  Trying {} instead.", source_name(other.source)));
                    let gave_up_on = source_name(info.source);
                    info = other;
                    target_version = self.note_found(&st, &info, gave_up_on);
                    self.update_step("verify", "pending", "");
                }
                _ => {
                    self.update_step(step, "error", &detail);
                    self.set_error(&err);
                    return Ok(code);
                }
            }
        };
        self.update_step("verify", "done", "SHA-256 OK");

        // ── Install ──
        if self.check_cancelled() {
            return Ok(1);
        }
        self.update_step("install", "active", "Swapping exe\u{2026}");
        let inst = self.install_exe(&dl.path, &st.target_exe)?;
        if !inst.ok {
            self.update_step("install", "error", &inst.msg);
            self.set_error(&inst.msg);
            return Ok(7);
        }
        self.update_step("install", "done", &inst.msg);

        // ── The record, and the launch ──
        self.update_install_info(&st.target_exe, &st.channel, &target_version);
        if self.check_cancelled() {
            self.update_step("launch", "skipped", "user cancelled before restart");
            self.set_done();
            return Ok(0);
        }
        self.update_step("launch", "active", &format!("Starting new {APP_NAME}\u{2026}"));
        let Err(why) = self.launch_new(&st.target_exe) else {
            self.update_step("launch", "done", "Launched");
            self.set_done();
            self.log("pipeline complete (exit 0)");
            return Ok(0);
        };
        if !inst.backup_path.is_empty() && std::path::Path::new(&inst.backup_path).exists() {
            // The new exe would not start (antivirus took it away after the
            // copy, say): put the previous one back and start that.
            if let Err(e) = self.platform.disk.copy(&inst.backup_path, &st.target_exe) {
                self.log(&format!("rollback after a failed start failed: {e}"));
                self.update_step("launch", "error", &why);
                self.set_error(&format!(
                    "Couldn't start the new {APP_NAME} ({why}) or put the previous version back ({e}).  \
                     It is saved as {}.",
                    inst.backup_path
                ));
                return Ok(8);
            }
            self.log(&format!("new exe would not start ({why}); rolled back to backup"));
            let old = self.launch_new(&st.target_exe);
            self.update_step("install", "error", "rolled back: the new version would not start");
            self.update_step("launch", "error", &why);
            self.set_error(&format!(
                "Couldn't start the new {APP_NAME} ({why}), so the previous version was put back{}",
                if old.is_ok() { " and started.".to_string() } else { format!(".  Launch it from {}.", st.target_exe) }
            ));
            return Ok(8);
        }
        self.update_step("launch", "error", &why);
        self.set_error(&format!(
            "Install OK but couldn't start the new {APP_NAME}: {why}.  Launch it manually from {}.",
            st.target_exe
        ));
        Ok(8)
    }

    // ─── Running it beside the window ────────────────────────────────

    // One install at a time, on a thread of its own.  Whoever wants it
    // stopped (the Close button, the window going away, both, either of
    // them twice) waits until it has really ended: the process must never
    // end while the exe is being swapped.  So nobody holds the thread
    // itself, which only one of them could wait on.  `run` says whether an
    // install is under way, and `run_ended` is rung when it is over, for as
    // many as are waiting.

    fn run(&self) -> MutexGuard<'_, Run> {
        self.run.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Start the pipeline on a thread of its own.  Refused, with the reason,
    /// while one is running (also one that was asked to stop and has not
    /// stopped yet), and once the updater is closing.
    pub fn start_install(self: &Arc<Self>) -> Result<(), &'static str> {
        let mut run = self.run();
        if run.alive {
            return Err("install already running");
        }
        if run.closing {
            return Err("the updater is closing");
        }
        {
            let now = self.platform.clock.now();
            let mut st = self.lock();
            st.phase = "ready".into();
            st.phase_label = "Starting".into();
            st.error = None;
            st.cancelled = false;
            st.progress = 0;
            st.started_at = now;
            st.ended_at = 0.0;
            st.steps = fresh_steps();
        }
        /// Says that the install is over when the pipeline's thread is done
        /// with it, however it ends.
        struct Over(Arc<Updater>);
        impl Drop for Over {
            fn drop(&mut self) {
                self.0.run().alive = false;
                self.0.run_ended.notify_all();
            }
        }
        run.alive = true;
        // (let go of before the thread is made: should that fail, `Over` is
        // dropped right here and takes this lock itself)
        drop(run);
        let over = Over(Arc::clone(self));
        let started = std::thread::Builder::new().name("updater-pipeline".into()).spawn(move || {
            over.0.run_install_pipeline();
        });
        match started {
            Ok(_) => Ok(()),
            Err(e) => {
                self.log(&format!("the install's thread could not be started: {e}"));
                Err("the install could not be started")
            }
        }
    }

    pub fn install_running(&self) -> bool {
        self.run().alive
    }

    /// Cancel the install, if one is running, and wait until it has ended.
    /// A cancel does not interrupt the exe swap, and exiting in the middle of
    /// it can leave neither the old exe nor the new one in place.  Everybody
    /// who calls this waits, however many they are and whoever came first.
    pub fn stop_install(&self) {
        let mut run = self.run();
        if run.alive {
            self.lock().cancelled = true;
        }
        while run.alive {
            run = self.run_ended.wait(run).unwrap_or_else(|poisoned| poisoned.into_inner());
        }
    }

    /// The updater is closing: from here on no install is started.  Without
    /// this, one could begin in the moment between the end of the install
    /// that was waited for and the end of the process.
    pub fn begin_closing(&self) {
        self.run().closing = true;
    }

    /// The last thing before the process ends: no install is started any
    /// more, and a running one is cancelled and waited for.
    pub fn shut_down(&self) {
        self.begin_closing();
        self.stop_install();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_size_from_the_server_is_only_a_hint() {
        let n = |text: &str| whole_number(&Json::parse(text).unwrap());
        assert_eq!((n("27000000"), n("\"44\""), n("3.9"), n("true")), (27_000_000, 44, 3, 1));
        for odd in ["\"44 MB\"", "[\"x\"]", "{\"a\":1}", "1e400", "null", "-5", "NaN", "false", "\"\"", "-Infinity"] {
            assert_eq!(n(odd), 0, "{odd}");
        }
        assert_eq!(n("123456789012345678901234567890"), u64::MAX);
    }

    #[test]
    fn a_version_cannot_choose_where_the_download_is_written() {
        assert_eq!(file_safe("3.8.1"), "3.8.1");
        assert_eq!(file_safe("3.9.0-beta.2+b5"), "3.9.0-beta.2+b5");
        assert_eq!(file_safe("..\\..\\Windows\\x"), ".._.._Windows_x");
        assert_eq!(file_safe("1/2:3 4"), "1_2_3_4");
    }
}
