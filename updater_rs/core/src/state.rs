//! What the updater knows and where it stands: seeded from the command line
//! and install_info.json, changed by the pipeline, and read by the window
//! twice a second as /api/status.  The names are the ones the Python updater
//! used, because the window's script (updater/static/app.js) reads them.

use crate::json::Json;

/// The pipeline's steps, in the order they run and are shown.  The build is
/// looked for first: with nothing to install, nothing is closed.
pub const PIPELINE_STEPS: [(&str, &str); 6] = [
    ("channel", "Looking for the newest build"),
    ("close", "Closing Vispora"),
    ("download", "Downloading new version"),
    ("verify", "Verifying integrity"),
    ("install", "Installing"),
    ("launch", "Restarting Vispora"),
];

#[derive(Debug, Clone, PartialEq)]
pub struct Step {
    pub key: &'static str,
    pub label: &'static str,
    /// pending | active | done | error | skipped
    pub status: String,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct State {
    pub updater_version: String,
    pub target_dir: String,
    pub target_exe: String,
    pub channel: String,
    pub current_version: String,
    pub target_version: String,
    /// The version the app asked for when it started the updater ("" when
    /// started by hand).
    pub requested_version: String,
    /// Where to look: "auto" (GitHub, then the update server), "github" or "server".
    pub source: String,
    /// True once another channel was picked for this run (in the window or
    /// on the command line): then a build older than the installed one is
    /// what was asked for, not a place that is behind.
    pub allow_older: bool,
    pub server_url: String,
    pub auto_confirm: bool,
    pub silent: bool,
    /// "install" or "update".
    pub mode: String,
    /// What the scan for an existing install found (null when none was made).
    pub scan_result: Json,

    /// ready | downloading | done | error | cancelled
    pub phase: String,
    pub phase_label: String,
    pub progress: i64,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub steps: Vec<Step>,
    pub error: Option<String>,
    pub cancelled: bool,
    pub started_at: f64,
    pub ended_at: f64,
    /// The build that was found: where, which version, its hash and address.
    pub channel_check: Json,
}

impl Default for State {
    fn default() -> State {
        State {
            updater_version: "?".into(),
            target_dir: String::new(),
            target_exe: String::new(),
            channel: String::new(),
            current_version: String::new(),
            target_version: String::new(),
            requested_version: String::new(),
            source: "auto".into(),
            allow_older: false,
            server_url: String::new(),
            auto_confirm: false,
            silent: false,
            mode: "update".into(),
            scan_result: Json::Null,
            phase: "ready".into(),
            phase_label: "Ready".into(),
            progress: 0,
            downloaded_bytes: 0,
            total_bytes: 0,
            steps: fresh_steps(),
            error: None,
            cancelled: false,
            started_at: 0.0,
            ended_at: 0.0,
            channel_check: Json::Null,
        }
    }
}

pub fn fresh_steps() -> Vec<Step> {
    PIPELINE_STEPS
        .iter()
        .map(|&(key, label)| Step { key, label, status: "pending".into(), detail: String::new() })
        .collect()
}

impl State {
    pub fn step(&self, key: &str) -> &Step {
        self.steps.iter().find(|s| s.key == key).expect("a step of the pipeline")
    }

    /// Everything, as the window is sent it.
    pub fn to_json(&self) -> Json {
        let steps: Vec<Json> = self
            .steps
            .iter()
            .map(|s| {
                Json::object([
                    ("key", s.key.into()),
                    ("label", s.label.into()),
                    ("status", s.status.as_str().into()),
                    ("detail", s.detail.as_str().into()),
                ])
            })
            .collect();
        Json::object([
            ("updater_version", self.updater_version.as_str().into()),
            ("target_dir", self.target_dir.as_str().into()),
            ("target_exe", self.target_exe.as_str().into()),
            ("channel", self.channel.as_str().into()),
            ("current_version", self.current_version.as_str().into()),
            ("target_version", self.target_version.as_str().into()),
            ("requested_version", self.requested_version.as_str().into()),
            ("source", self.source.as_str().into()),
            ("allow_older", self.allow_older.into()),
            ("server_url", self.server_url.as_str().into()),
            ("auto_confirm", self.auto_confirm.into()),
            ("silent", self.silent.into()),
            ("mode", self.mode.as_str().into()),
            ("scan_result", self.scan_result.clone()),
            ("phase", self.phase.as_str().into()),
            ("phase_label", self.phase_label.as_str().into()),
            ("progress", self.progress.into()),
            ("downloaded_bytes", self.downloaded_bytes.into()),
            ("total_bytes", self.total_bytes.into()),
            ("steps", Json::Array(steps)),
            ("error", self.error.clone().into()),
            ("cancelled", self.cancelled.into()),
            ("started_at", self.started_at.into()),
            ("ended_at", self.ended_at.into()),
            ("channel_check", self.channel_check.clone()),
        ])
    }
}
