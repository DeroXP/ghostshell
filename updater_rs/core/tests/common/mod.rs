//! The test rig: a sandboxed install and an updater whose doors to the
//! outside are fakes.
//!
//! Everything the updater would touch is faked or redirected, as in the
//! Python tests this is ported from (tests/test_updater.py): the network is a
//! table of answers, tasklist / taskkill / the app launch are recorded, the
//! clock does not wait, and the "install" is a folder inside a temporary
//! directory.  Program Files and the user's folders point into it too, so the
//! scan for an install never looks at the real ones.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use vispora_updater_core::cli;
use vispora_updater_core::json::Json;
use vispora_updater_core::paths::{self, Env};
use vispora_updater_core::platform::{Body, Clock, Disk, Net, Platform, Processes};
use vispora_updater_core::server::{self, Response};
use vispora_updater_core::sha256;
use vispora_updater_core::sources;
use vispora_updater_core::state::State;
use vispora_updater_core::{Updater, UPDATER_VERSION};

pub const SERVER: &str = sources::SERVER_URL_DEFAULT;
pub const GITHUB: &str = sources::GITHUB_RELEASES_URL;
pub const OWN_SERVER: &str = "https://updates.example.test";
pub const NEWER: &str = "9.0.0";
pub const NEWEST: &str = "9.1.0";

pub fn repeated(prefix: &[u8], piece: &[u8], times: usize) -> Vec<u8> {
    let mut out = prefix.to_vec();
    for _ in 0..times {
        out.extend_from_slice(piece);
    }
    out
}

/// The exe that is installed, and builds to replace it with.  (Anything under
/// 100 KB is taken for an error page; GitHub's files must be over 1 MB.)
pub fn old() -> Vec<u8> {
    repeated(b"", b"old build ", 20000)
}
pub fn new() -> Vec<u8> {
    repeated(b"", b"new build ", 20000)
}
pub fn app_new() -> Vec<u8> {
    repeated(b"MZ", b"new build ", 120000)
}
pub fn app_other() -> Vec<u8> {
    repeated(b"MZ", b"another build ", 90000)
}

pub fn j(text: &str) -> Json {
    Json::parse(text).unwrap_or_else(|e| panic!("not JSON ({e}): {text}"))
}

pub fn quoted(text: &str) -> String {
    Json::from(text).to_string()
}

// ── the network ───────────────────────────────────────────────────────
struct Bytes {
    data: Vec<u8>,
    at: usize,
}

impl Body for Bytes {
    fn content_length(&self) -> Option<u64> {
        Some(self.data.len() as u64)
    }
    fn read(&mut self, buf: &mut [u8]) -> Result<usize, String> {
        let n = buf.len().min(self.data.len() - self.at);
        buf[..n].copy_from_slice(&self.data[self.at..self.at + n]);
        self.at += n;
        Ok(n)
    }
}

type Opener = Box<dyn Fn(&str) -> Option<Result<Box<dyn Body>, String>> + Send + Sync>;

#[derive(Default)]
pub struct FakeNet {
    pub routes: Mutex<HashMap<String, Vec<u8>>>,
    pub calls: Mutex<Vec<String>>,
    pub headers: Mutex<HashMap<String, HashMap<String, String>>>,
    /// Asked before the table: a test's own answer for an address.
    pub opener: Mutex<Option<Opener>>,
}

impl FakeNet {
    pub fn route(&self, url: &str, body: impl Into<Vec<u8>>) {
        self.routes.lock().unwrap().insert(url.to_string(), body.into());
    }

    pub fn unroute(&self, url: &str) {
        self.routes.lock().unwrap().remove(url);
    }

    pub fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }

    pub fn header(&self, url: &str, name: &str) -> Option<String> {
        self.headers.lock().unwrap().get(url)?.get(&name.to_lowercase()).cloned()
    }

    /// The update server publishes a build on a channel.  `sha`: what it says
    /// the hash is (the true one when None).
    pub fn publish(&self, channel: &str, version: &str, body: &[u8], sha: Option<&str>, base: &str) {
        let hash = sha.map_or_else(|| sha256::hex_of(body), str::to_string);
        let meta = format!(
            "{{\"available\": true, \"version\": {}, \"size_bytes\": {}, \"notes\": {}, \"sha256\": {}, \"download_url\": {}}}",
            quoted(version),
            body.len(),
            quoted(&format!("Server notes for {version}.")),
            quoted(&hash),
            quoted(&format!("{base}/download/{channel}"))
        );
        self.route(&format!("{base}/version/{channel}"), meta);
        self.route(&format!("{base}/download/{channel}"), body);
    }

    /// GitHub answers with these releases and serves their files.
    pub fn github(&self, releases: &[Release]) {
        let mut listed = Vec::new();
        for r in releases {
            for (url, body) in &r.bodies {
                self.route(url, body.clone());
            }
            listed.push(r.json.clone());
        }
        self.route(GITHUB, Json::Array(listed).to_string());
    }

    pub fn github_says(&self, anything: &str) {
        self.route(GITHUB, anything);
    }
}

struct NetDoor(Arc<FakeNet>);

impl Net for NetDoor {
    fn open(&self, url: &str, headers: &[(&str, &str)], _timeout_s: u32) -> Result<Box<dyn Body>, String> {
        self.0.calls.lock().unwrap().push(url.to_string());
        let sent = headers.iter().map(|(k, v)| (k.to_lowercase(), v.to_string())).collect();
        self.0.headers.lock().unwrap().insert(url.to_string(), sent);
        if let Some(opener) = self.0.opener.lock().unwrap().as_ref() {
            if let Some(answer) = opener(url) {
                return answer;
            }
        }
        match self.0.routes.lock().unwrap().get(url) {
            Some(body) => Ok(Box::new(Bytes { data: body.clone(), at: 0 })),
            None => Err(format!("no route to {url}")),
        }
    }
}

/// A release as GitHub lists it, and the files it serves.
#[derive(Clone)]
pub struct Release {
    pub json: Json,
    pub bodies: Vec<(String, Vec<u8>)>,
}

pub fn url_of(version: &str, name: &str) -> String {
    format!("https://github.com/{}/releases/download/v{version}/{name}", sources::GITHUB_REPO)
}

pub fn asset(name: &str, body: &[u8], tag: &str) -> Json {
    j(&format!(
        "{{\"name\": {}, \"state\": \"uploaded\", \"size\": {}, \"digest\": \"sha256:{}\", \"browser_download_url\": {}}}",
        quoted(name),
        body.len(),
        sha256::hex_of(body),
        quoted(&format!("https://github.com/{}/releases/download/{tag}/{name}", sources::GITHUB_REPO))
    ))
}

/// A release tagged "v<version>" carrying `files`.
pub fn release_with(version: &str, files: &[(&str, &[u8])], prerelease: bool) -> Release {
    let tag = format!("v{version}");
    let assets: Vec<Json> = files.iter().map(|(name, body)| asset(name, body, &tag)).collect();
    let json = Json::object([
        ("tag_name", tag.as_str().into()),
        ("name", tag.as_str().into()),
        ("draft", false.into()),
        ("prerelease", prerelease.into()),
        ("body", format!("What is new in {version}.").into()),
        ("published_at", "2026-11-24T10:00:00Z".into()),
        ("assets", Json::Array(assets.clone())),
    ]);
    let bodies = assets
        .iter()
        .zip(files)
        .map(|(a, (_, body))| (a["browser_download_url"].as_str().unwrap().to_string(), body.to_vec()))
        .collect();
    Release { json, bodies }
}

/// A final release with one Vispora.exe (APP_NEW).
pub fn release(version: &str) -> Release {
    release_with(version, &[("Vispora.exe", &app_new())], false)
}

impl Release {
    /// The same release with one of its fields changed.
    pub fn with(mut self, key: &str, value: Json) -> Release {
        self.json.set(key, value);
        self
    }
}

// ── tasklist / taskkill / launching the app ───────────────────────────
type Spawner = Box<dyn Fn(&[String]) -> Result<(), String> + Send + Sync>;

pub struct FakeProcesses {
    /// image name (lower case) -> process ids
    pub running: Mutex<HashMap<String, Vec<u32>>>,
    pub commands: Mutex<Vec<Vec<String>>>,
    pub launched: Mutex<Vec<Vec<String>>>,
    /// Does the app close when asked politely?
    pub polite: Mutex<bool>,
    /// True: not even taskkill /F ends it.
    pub immortal: Mutex<bool>,
    /// A test's own way of starting a program (the default records and succeeds).
    pub spawner: Mutex<Option<Spawner>>,
}

impl FakeProcesses {
    pub fn commands(&self) -> Vec<Vec<String>> {
        self.commands.lock().unwrap().clone()
    }
    pub fn launched(&self) -> Vec<Vec<String>> {
        self.launched.lock().unwrap().clone()
    }
    pub fn ran(&self, program: &str) -> Vec<Vec<String>> {
        self.commands().into_iter().filter(|c| c[0] == program).collect()
    }
    pub fn pids(&self, image: &str) -> Vec<u32> {
        self.running.lock().unwrap().get(&image.to_lowercase()).cloned().unwrap_or_default()
    }
}

struct ProcDoor(Arc<FakeProcesses>);

impl Processes for ProcDoor {
    fn run(&self, args: &[String], _timeout_s: u32) -> Result<String, String> {
        self.0.commands.lock().unwrap().push(args.to_vec());
        let mut out = String::new();
        if args[0] == "tasklist" {
            let image = args[2].split(" eq ").nth(1).unwrap_or("");
            let pids = self.0.pids(image);
            out = pids.iter().map(|pid| format!("\"{image}\",\"{pid}\",\"Console\",\"1\",\"120,000 K\"")).collect::<Vec<_>>().join("\n");
            if pids.is_empty() {
                out = "INFO: No tasks are running which match the specified criteria.".into();
            }
        } else if args[0] == "taskkill"
            && !*self.0.immortal.lock().unwrap()
            && (*self.0.polite.lock().unwrap() || args.contains(&"/F".to_string()))
        {
            let pid: u32 = args[args.iter().position(|a| a == "/PID").unwrap() + 1].parse().unwrap();
            for pids in self.0.running.lock().unwrap().values_mut() {
                pids.retain(|p| *p != pid);
            }
        }
        Ok(out)
    }

    fn spawn(&self, args: &[String]) -> Result<(), String> {
        if let Some(spawner) = self.0.spawner.lock().unwrap().as_ref() {
            return spawner(args);
        }
        self.0.launched.lock().unwrap().push(args.to_vec());
        Ok(())
    }
}

// ── the disk and the clock ────────────────────────────────────────────
/// A test's own copy: Some(result) to answer for the disk, None to let the
/// real copy happen.
type Copier = Box<dyn Fn(&str, &str) -> Option<std::io::Result<()>> + Send + Sync>;

#[derive(Default)]
pub struct FakeDisk {
    pub copier: Mutex<Option<Copier>>,
}

struct DiskDoor(Arc<FakeDisk>);

impl Disk for DiskDoor {
    fn copy(&self, from: &str, to: &str) -> std::io::Result<()> {
        if let Some(copier) = self.0.copier.lock().unwrap().as_ref() {
            if let Some(answer) = copier(from, to) {
                return answer;
            }
        }
        std::fs::copy(from, to).map(|_| ())
    }
}

pub fn io_error(code: i32, text: &str) -> std::io::Error {
    let _ = code;
    std::io::Error::new(std::io::ErrorKind::Other, text.to_string())
}

/// A clock that does not wait: a sleep moves its time forward at once.
pub struct FakeClock {
    micros: AtomicU64,
}

impl Clock for FakeClock {
    fn now(&self) -> f64 {
        self.micros.load(Ordering::SeqCst) as f64 / 1e6
    }
    fn sleep(&self, seconds: f64) {
        self.micros.fetch_add((seconds.max(0.0) * 1e6) as u64, Ordering::SeqCst);
        std::thread::yield_now();
    }
    fn stamp(&self) -> String {
        "2026-11-24T12:00:00".into()
    }
}

// ── the rig ───────────────────────────────────────────────────────────
static NEXT: AtomicU64 = AtomicU64::new(1);

pub struct Rig {
    pub root: String,
    pub updater: Arc<Updater>,
    pub net: Arc<FakeNet>,
    pub procs: Arc<FakeProcesses>,
    pub disk: Arc<FakeDisk>,
    /// The app exe of the sandboxed install.
    pub target: String,
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.updater.stop_install();
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

impl Rig {
    /// An updater in a sandbox, with nothing set up in it yet.
    pub fn bare() -> Rig {
        Rig::with_net(None)
    }

    /// The same, with a network of the test's own choosing in place of the
    /// table of answers (the real client talking to a listener on this PC).
    pub fn with_net(real: Option<Box<dyn Net>>) -> Rig {
        let n = NEXT.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!("vispora_updater_tests_{}_{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let root = root.to_string_lossy().into_owned();
        let env = Env {
            appdata: paths::join(&root, "Roaming"),
            localappdata: paths::join(&root, "Local"),
            home: paths::join(&root, "Profile"),
            program_files: paths::join(&root, "pf"),
            program_files_x86: paths::join(&root, "pf86"),
        };
        for folder in [&env.appdata, &env.localappdata, &env.home, &env.program_files, &env.program_files_x86] {
            std::fs::create_dir_all(folder).unwrap();
        }
        let net = Arc::new(FakeNet::default());
        let procs = Arc::new(FakeProcesses {
            running: Mutex::default(),
            commands: Mutex::default(),
            launched: Mutex::default(),
            polite: Mutex::new(true),
            immortal: Mutex::new(false),
            spawner: Mutex::new(None),
        });
        let disk = Arc::new(FakeDisk::default());
        let platform = Platform {
            net: real.unwrap_or_else(|| Box::new(NetDoor(net.clone()))),
            procs: Box::new(ProcDoor(procs.clone())),
            disk: Box::new(DiskDoor(disk.clone())),
            clock: Box::new(FakeClock { micros: AtomicU64::new(1_764_000_000_000_000) }),
        };
        let updater = Updater::new(env, platform);
        Rig { root, updater, net, procs, disk, target: String::new() }
    }

    /// A fresh install folder and updater state (tests/test_updater.py's setup).
    pub fn setup(mode: &str, exe_name: &str, channel: &str, running: &[u32]) -> Rig {
        let mut rig = Rig::bare();
        let folder = paths::join(&paths::join(&rig.root, "installs"), "1");
        rig.target = paths::join(&folder, exe_name);
        if mode == "update" {
            std::fs::create_dir_all(&folder).unwrap();
            std::fs::write(&rig.target, old()).unwrap();
            rig.procs.running.lock().unwrap().insert(exe_name.to_lowercase(), running.to_vec());
        }
        rig.updater.init_state(State {
            target_dir: folder,
            target_exe: rig.target.clone(),
            channel: channel.to_string(),
            current_version: "3.6.0".into(),
            server_url: SERVER.into(),
            silent: true,
            updater_version: UPDATER_VERSION.into(),
            mode: mode.to_string(),
            ..State::default()
        });
        rig
    }

    /// An installed GhostShell.exe on the stable channel, not running.
    pub fn update() -> Rig {
        Rig::setup("update", "GhostShell.exe", "stable", &[])
    }

    pub fn folder(&self) -> String {
        paths::dirname(&self.target)
    }

    pub fn state(&self) -> State {
        self.updater.snapshot()
    }

    pub fn error(&self) -> String {
        self.state().error.unwrap_or_default()
    }

    pub fn run(&self) -> i32 {
        self.updater.run_install_pipeline()
    }

    /// step key -> status
    pub fn steps(&self) -> HashMap<&'static str, String> {
        self.state().steps.into_iter().map(|s| (s.key, s.status)).collect()
    }

    pub fn all_steps_are(&self, status: &str) -> bool {
        self.steps().values().all(|s| s == status)
    }

    pub fn step_detail(&self, key: &str) -> String {
        self.state().step(key).detail.clone()
    }

    /// The backups next to the target, by name.
    pub fn backups(&self) -> Vec<String> {
        let name = paths::basename(&self.target).to_string();
        let mut found: Vec<String> = std::fs::read_dir(self.folder())
            .map(|entries| entries.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect())
            .unwrap_or_default();
        found.retain(|f| {
            f.strip_prefix(&format!("{name}.bak.")).is_some_and(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()))
        });
        found.sort();
        found
    }

    pub fn installed(&self) -> Vec<u8> {
        std::fs::read(&self.target).unwrap()
    }

    pub fn part_files(&self) -> Vec<String> {
        std::fs::read_dir(&self.updater.paths.updater_dir)
            .map(|entries| entries.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect::<Vec<_>>())
            .unwrap_or_default()
            .into_iter()
            .filter(|f| f.ends_with(".part"))
            .collect()
    }

    pub fn install_info(&self) -> Json {
        Json::parse_bytes(&std::fs::read(&self.updater.paths.install_info).unwrap()).unwrap()
    }

    pub fn write_install_info(&self, text: &str) {
        std::fs::write(&self.updater.paths.install_info, text).unwrap();
    }

    /// The app's record of this install, with more fields from `extra` ("" for none).
    pub fn record(&self, channel: &str, extra: &str) {
        self.write_install_info(&format!(
            "{{\"install_path\": {}, \"install_dir\": {}, \"channel\": {}, \"version\": \"3.6.0\"{}{extra}}}",
            quoted(&self.target),
            quoted(&self.folder()),
            quoted(channel),
            if extra.is_empty() { "" } else { ", " }
        ));
    }

    /// Start the updater as its command line would: the arguments, the app's
    /// record, and for --silent the whole run.  The exit code of a silent
    /// run or of a refused command line; None when a window would open.
    pub fn main(&self, argv: &[&str]) -> Option<i32> {
        let argv: Vec<String> = argv.iter().map(|a| a.to_string()).collect();
        let args = match cli::parse_args(&argv) {
            Ok(args) => args,
            Err(e) => return Some(e.exit_code),
        };
        cli::prepare(&self.updater, &args, UPDATER_VERSION);
        args.silent.then(|| cli::run_silent(&self.updater))
    }

    pub fn get(&self, path: &str) -> Response {
        server::handle(&self.updater, "GET", path, b"")
    }

    pub fn post(&self, path: &str, body: &str) -> Json {
        server::handle(&self.updater, "POST", path, body.as_bytes()).to_json()
    }
}

/// Wait (for real) until `condition` holds; false if it never does.
pub fn wait(condition: impl Fn() -> bool, seconds: f64) -> bool {
    let end = std::time::Instant::now() + std::time::Duration::from_secs_f64(seconds);
    while std::time::Instant::now() < end {
        if condition() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    condition()
}
