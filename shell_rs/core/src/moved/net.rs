//! The network monitor: how many bytes a second the network cards send and
//! receive, and which programs move the most, for the Network page; and a
//! warning when a program other than the game moves more than 5 MB a second
//! for three seconds in a row while a game runs.
//!
//! This is core/net_monitor.py, moved: its sampler and the three calls that
//! read it (`/api/network/live`, `/history`, `/top`).  The Python module
//! stays, and stays what answers when the app is started as it always was.
//! Its other two calls stay the backend's behind the shell too: pausing and
//! resuming a program (`/api/network/pause-process`, `/resume-process`)
//! change the PC.  So do the other calls under `/api/network/` (the network
//! optimizer's), which are no part of this module.
//!
//! **It runs while something reads it**, as Python's: the first call that
//! reads it starts the sampler, which takes a sample once a second, and
//! stops by itself once nobody has read it for `IDLE_STOP_S` (counted on the
//! clock that does not jump).  A start forgets what the last run measured
//! but for the minute of totals and the time of the last sample, which
//! Python keeps too.
//!
//! **Where each reading comes from: where Python reads it.**
//! * The network cards' totals: the helper's `network_cards`, which makes
//!   the calls psutil's `net_io_counters()` makes; what psutil then does in
//!   Python (the last of two cards of one name counts, and a count that
//!   goes back is added on to: `Wrap`) is done here the same way.
//! * Every program's bytes: the helper's `process_io`, as Python asks it.
//!   Where it has no answer, Python reads them with psutil, one program at a
//!   time; the shell asks its backend for that part of the pass
//!   (`THE_BACKENDS_PART`, `?name=processes`), which takes it with the
//!   module's own function.
//! * Whether a game runs, and which: the backend's game monitor knows
//!   (until it moves into the shell, step 4's piece 7).  The shell asks for
//!   it once a pass, as Python reads it once a pass (`?name=game`).  Where
//!   there is no backend to ask, no game is known for that pass: no program
//!   is taken for the game, and the page is told of none.
//!
//! It only reads, and starts no program.  Nothing of a call's text reaches
//! a question of the helper's: both questions take no words.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;

use super::{py_round, query_int, AskBackend, Readings, Say};
use crate::http::{Request, Response};
use crate::json::Json;

/// A sample every this many seconds.
pub const SAMPLE_INTERVAL: f64 = 1.0;
/// How many samples of the totals are kept: a minute of them.
pub const HISTORY_LEN: usize = 60;
/// Nobody has read it for this long: the sampler stops.
pub const IDLE_STOP_S: f64 = 30.0;
/// A program other than the game that moves this much a second ...
pub const BURST_BYTES_PER_SEC: i128 = 5 * 1024 * 1024;
/// ... for this many samples in a row is a burst.
pub const BURST_CONSECUTIVE: i128 = 3;
/// How many programs the page is shown.
const TOP: usize = 15;
/// The backend's call for one part of a pass (app.py, api_network_part).
/// Behind the shell only.
pub const THE_BACKENDS_PART: &str = "/api/network/part";
/// Programs that are never taken for a burst (core/net_monitor.py,
/// NEVER_FLAG: the app's own names from config.OWN_PROCESS_NAMES, and the
/// rest as written there).
pub const NEVER_FLAG: [&str; 15] = ["vispora.exe", "visporaupdater.exe", "ghostshell.exe", "ghostshellupdater.exe", "python.exe", "pythonw.exe", "system",
                                    "system idle process", "svchost.exe", "discord.exe", "discordcanary.exe", "discordptb.exe", "spotify.exe", "tidal.exe",
                                    "apple music.exe"];

/// What psutil's `net_io_counters(nowrap=True)` does with the cards' counts
/// in Python (psutil/_common.py, _WrapNumbers): a count that is lower than
/// it was is taken to have started again from nought, and what it was is
/// added on to it from then on, so that a total never goes back.  Kept for
/// the life of the process, as psutil keeps it for the backend's.
#[derive(Default)]
struct Wrap {
    /// The counts of the last call, by card: (sent, received).
    cache: Option<Vec<(String, [i128; 2])>>,
    /// What is added on, by card and count.
    added: HashMap<(String, usize), i128>,
}

impl Wrap {
    fn run(&mut self, cards: Vec<(String, [i128; 2])>) -> Vec<(String, [i128; 2])> {
        let Some(old) = self.cache.replace(cards.clone()) else { return cards };
        // (a card that is gone takes what was added on to it along)
        self.added.retain(|(name, _), _| cards.iter().any(|(known, _)| known == name));
        cards
            .into_iter()
            .map(|(name, counts)| {
                let Some((_, was)) = old.iter().find(|(known, _)| *known == name) else { return (name, counts) };
                let mut now = counts;
                for field in 0..2 {
                    let added = self.added.entry((name.clone(), field)).or_insert(0);
                    if counts[field] < was[field] {
                        *added += was[field];
                    }
                    now[field] = counts[field] + *added;
                }
                (name, now)
            })
            .collect()
    }
}

/// One program the sampler has seen.
struct Seen {
    pid: i128,
    name: String,
    last_other: i128,
    bps: i128,
    high: i128,
}

/// What a pass compares with, kept from pass to pass (Python's module
/// state, held by one pass at a time).
#[derive(Default)]
struct Sampling {
    /// In the order they were first seen, as a Python dict keeps them.
    seen: Vec<Seen>,
    last_nic_rx: i128,
    last_nic_tx: i128,
    /// When the last sample was taken, on the PC's own clock (0: none yet).
    last_sample_t: f64,
    wrap: Wrap,
}

/// The latest sample, as the page is given it.
struct Latest {
    rx: i128,
    tx: i128,
    top: Json,
    burst: Json,
    last_sampled_at: Json,
    /// (not there at all until the first sample, as in Python)
    active_game: Option<Json>,
}

/// How many passes were taken, and what became of them.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Passes {
    pub kept: u64,
    pub dropped: u64,
    pub asked_the_backend: u64,
}

struct State {
    /// Sampling, and not told to stop (Python: `is_running()`).
    running: bool,
    /// Which start the loop that samples belongs to.
    thread: Option<u64>,
    starts: u64,
    /// When something last read it, on the clock that does not jump.
    last_wanted: f64,
    history: VecDeque<(f64, i128, i128)>,
    latest: Latest,
    passes: Passes,
    backend: Option<AskBackend>,
    say: Option<Say>,
    closed: bool,
}

/// Why a pass is not taken: the programs could not be read (the backend
/// was asked for them, and has no answer or its function raised).
struct NoPrograms;

/// The network monitor.
pub struct NetMonitor {
    door: Arc<dyn Readings>,
    /// Does a start run a thread?  (Not where a test takes every round by
    /// hand: `round`.)
    threads: bool,
    /// Starting and stopping, one at a time (Python's `_run_lock`).
    run: Mutex<()>,
    sampling: Mutex<Sampling>,
    state: Mutex<State>,
    told: Condvar,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// What Python's `_int(value)` takes (core/native.py): a whole number, or a
/// number with a fraction cut off.  None where it raises.
fn whole(value: Option<&Json>) -> Option<i128> {
    match value {
        Some(Json::Int(whole)) => Some(*whole),
        Some(Json::Float(number)) if number.is_finite() => Some(number.trunc() as i128),
        _ => None,
    }
}

/// Bytes a second, as Python works them out: `max(0, int(delta / dt))`.
fn per_second(delta: i128, dt: f64) -> i128 {
    let rate = (delta as f64 / dt).trunc();
    if rate > 0.0 {
        rate as i128
    } else {
        0
    }
}

impl NetMonitor {
    pub fn new(door: Arc<dyn Readings>, threads: bool) -> Arc<NetMonitor> {
        let latest = Latest { rx: 0, tx: 0, top: Json::Array(Vec::new()), burst: Json::Null, last_sampled_at: Json::Int(0), active_game: None };
        let state = State { running: false, thread: None, starts: 0, last_wanted: 0.0, history: VecDeque::with_capacity(HISTORY_LEN), latest,
                            passes: Passes::default(), backend: None, say: None, closed: false };
        Arc::new(NetMonitor { door, threads, run: Mutex::new(()), sampling: Mutex::new(Sampling::default()), state: Mutex::new(state), told: Condvar::new() })
    }

    fn state(&self) -> MutexGuard<'_, State> {
        lock(&self.state)
    }

    /// How the backend is asked for a part of a pass.
    pub fn asks_the_backend_with(&self, ask: AskBackend) {
        self.state().backend = Some(ask);
    }

    /// Where to say what the sampler does.
    pub fn says_with(&self, say: Say) {
        self.state().say = Some(say);
    }

    fn say(&self, line: &str) {
        let say = self.state().say.clone();
        if let Some(say) = say {
            say(line);
        }
    }

    /// How many passes were taken so far, and what became of them.
    pub fn passes(&self) -> Passes {
        self.state().passes
    }

    /// Is the sampler running?
    pub fn is_running(&self) -> bool {
        self.state().running
    }

    /// The shell's answer to one of the module's calls, or None: the call
    /// is the backend's.  `path` is the call's path as the backend's web
    /// framework reads it.
    pub fn answer(self: &Arc<Self>, request: &Request, path: &str) -> Option<Response> {
        // (the web framework answers HEAD by running the GET's route, which
        // would start the backend's own sampler: the shell takes it too)
        let (method, target) = (request.method.as_str(), request.target.as_str());
        if method != "GET" && method != "HEAD" {
            return None;
        }
        let refused = |name: &str| {
            let words = format!("The number given for {name} is not one this program reads the way Python does.");
            Some(Response::json(400, &Json::object([("ok", false.into()), ("err", words.into())])))
        };
        let answer = match path {
            "/api/network/live" => self.live(),
            "/api/network/history" => match query_int(target, "seconds", 60) {
                Some(seconds) => Json::object([("history", self.history(seconds))]),
                // Not passed on: the backend would start its own sampler
                // to answer it, beside the shell's.
                None => return refused("seconds"),
            },
            "/api/network/top" => match query_int(target, "limit", 15) {
                Some(limit) => Json::object([("top", self.top(limit))]),
                None => return refused("limit"),
            },
            _ => return None,
        };
        let mut response = Response::json(200, &answer);
        if method == "HEAD" {
            response.body.clear();
        }
        Some(response)
    }

    /// Start the sampler if it is not running (Python's `start()`).
    pub fn start(self: &Arc<Self>) -> Json {
        let _one_at_a_time = lock(&self.run);
        let this = {
            let mut st = self.state();
            st.last_wanted = self.door.steady();
            if st.running {
                return Json::object([("ok", true.into()), ("msg", "already running".into())]);
            }
            st.starts += 1;
            st.starts
        };
        self.forget_readings();
        {
            let mut st = self.state();
            st.running = true;
            st.thread = Some(this);
        }
        // (a loop started after the quit ends before its first sample)
        if self.threads {
            let monitor = self.clone();
            if let Err(why) = std::thread::Builder::new().name("net-monitor".to_string()).spawn(move || monitor.run(this)) {
                let mut st = self.state();
                if st.thread == Some(this) {
                    st.running = false;
                }
                drop(st);
                self.say(&format!("the network monitor's sampler could not be started: no thread could be made for it ({why})"));
            }
        }
        Json::object([("ok", true.into())])
    }

    /// Stop it (Python's `stop()`).
    pub fn stop(&self) {
        let _one_at_a_time = lock(&self.run);
        self.state().running = false;
        self.told.notify_all();
    }

    /// The shell quits: the sampler stops, and none is started any more.
    pub fn close(&self) {
        {
            let mut st = self.state();
            st.closed = true;
            st.running = false;
        }
        self.told.notify_all();
    }

    /// Drop what the last run measured (Python's `_forget_readings`): a run
    /// that starts later must not work a rate out from counts read minutes
    /// ago, nor show the old figures as if they were live.
    fn forget_readings(&self) {
        let mut sampling = lock(&self.sampling);
        sampling.seen.clear();
        (sampling.last_nic_rx, sampling.last_nic_tx, sampling.last_sample_t) = (0, 0, 0.0);
        let mut st = self.state();
        (st.latest.rx, st.latest.tx, st.latest.top, st.latest.burst) = (0, 0, Json::Array(Vec::new()), Json::Null);
    }

    /// Something reads it: noted, and the sampler started if it is not
    /// running.
    fn wanted(self: &Arc<Self>) {
        let running = {
            let mut st = self.state();
            st.last_wanted = self.door.steady();
            st.running
        };
        if !running {
            self.start();
        }
    }

    /// The latest sample, as `/api/network/live` gives it.
    pub fn live(self: &Arc<Self>) -> Json {
        self.wanted();
        let st = self.state();
        let mut pairs = vec![
            ("total_rx_per_sec".to_string(), Json::Int(st.latest.rx)),
            ("total_tx_per_sec".to_string(), Json::Int(st.latest.tx)),
            ("top".to_string(), st.latest.top.clone()),
            ("burst_alert".to_string(), st.latest.burst.clone()),
            ("last_sampled_at".to_string(), st.latest.last_sampled_at.clone()),
        ];
        if let Some(game) = &st.latest.active_game {
            pairs.push(("active_game".to_string(), game.clone()));
        }
        pairs.push(("history_len".to_string(), Json::Int(st.history.len() as i128)));
        pairs.push(("running".to_string(), st.running.into()));
        Json::Object(pairs)
    }

    /// The totals of the last `seconds`, on the PC's own clock.
    pub fn history(self: &Arc<Self>, seconds: i128) -> Json {
        self.wanted();
        let cutoff = self.door.now() - seconds as f64;
        let st = self.state();
        Json::Array(
            st.history
                .iter()
                .filter(|(ts, _, _)| *ts >= cutoff)
                .map(|(ts, rx, tx)| Json::object([("ts", Json::Float(*ts)), ("rx", Json::Int(*rx)), ("tx", Json::Int(*tx))]))
                .collect(),
        )
    }

    /// The programs that move the most, `limit` of them as Python slices a
    /// list (a number below nought leaves that many off the end).
    pub fn top(self: &Arc<Self>, limit: i128) -> Json {
        let live = self.live();
        let top = live.get("top").and_then(Json::as_array).map(<[Json]>::to_vec).unwrap_or_default();
        let length = top.len() as i128;
        let end = if limit >= 0 { limit.min(length) } else { (length + limit).max(0) };
        Json::Array(top.into_iter().take(end as usize).collect())
    }

    /// What the loop of the start that counts does in one round: a sample,
    /// then whether anybody still reads it.  Did it take a sample?  (For a
    /// test that takes every round by hand.)
    pub fn round(&self) -> bool {
        let this = {
            let st = self.state();
            match st.thread {
                Some(this) if st.running && !st.closed => this,
                _ => return false,
            }
        };
        self.round_of(this, None)
    }

    /// A round of the loop of start `this`, waiting `wait` between the
    /// sample and the look at who reads it (no wait: a test's round).
    fn round_of(&self, this: u64, wait: Option<Duration>) -> bool {
        {
            let sampling = lock(&self.sampling);
            {
                let st = self.state();
                if !st.running || st.thread != Some(this) || st.closed {
                    return false;
                }
            }
            self.step(sampling);
        }
        if let Some(wait) = wait {
            let st = self.state();
            let (st, _) = self.told.wait_timeout_while(st, wait, |st| st.running && st.thread == Some(this) && !st.closed).unwrap_or_else(|p| p.into_inner());
            if !st.running || st.thread != Some(this) || st.closed {
                return true;
            }
        }
        let _one_at_a_time = lock(&self.run);
        let nobody = {
            let mut st = self.state();
            let nobody = st.running && st.thread == Some(this) && self.door.steady() - st.last_wanted > IDLE_STOP_S;
            if nobody {
                // (decided with the lock held: a reader that asks now either
                // got in first, and it goes on, or starts a new sampler)
                st.running = false;
            }
            nobody
        };
        if nobody {
            self.say(&format!("the network monitor's sampler stopped: nobody has read it for {IDLE_STOP_S:.0} s"));
        }
        true
    }

    /// The sampler's thread.
    fn run(&self, this: u64) {
        self.say("the network monitor's sampler was started");
        while self.round_of(this, Some(Duration::from_secs_f64(SAMPLE_INTERVAL))) {}
    }

    /// The backend's answer for a part of a pass: its reading, or None
    /// (there is no backend, it did not answer, or its function raised).
    fn the_backends(&self, name: &str) -> Option<Json> {
        let ask = {
            let mut st = self.state();
            if st.backend.is_some() {
                st.passes.asked_the_backend += 1;
            }
            st.backend.clone()
        }?;
        let said = ask(&format!("{THE_BACKENDS_PART}?name={name}"))?;
        if !said.get("ok").is_some_and(Json::truthy) {
            return None;
        }
        said.get("reading").cloned()
    }

    /// The network cards' totals, (received, sent), as psutil gives them to
    /// Python; (0, 0) where it would raise or say nothing.
    fn totals(&self, sampling: &mut Sampling) -> (i128, i128) {
        let Some(Json::Array(rows)) = self.door.ask("network_cards") else { return (0, 0) };
        let mut cards: Vec<(String, [i128; 2])> = Vec::with_capacity(rows.len());
        for row in &rows {
            let (Some(Json::Str(name)), Some(sent), Some(recv)) = (row.get("name"), whole(row.get("sent")), whole(row.get("recv"))) else { return (0, 0) };
            // (a dict by name: the last of two of one name, in the place of the first)
            match cards.iter_mut().find(|(known, _)| known == name) {
                Some(card) => card.1 = [sent, recv],
                None => cards.push((name.clone(), [sent, recv])),
            }
        }
        // (no cards: noughts, as psutil's None gives Python; what psutil keeps
        // of such a call shows in no rate, the first after it being nought)
        let cards = sampling.wrap.run(cards);
        (cards.iter().map(|(_, counts)| counts[1]).sum(), cards.iter().map(|(_, counts)| counts[0]).sum())
    }

    /// Every program: (pid, name in small letters, bytes of "other" I/O so
    /// far), as Python's `_process_io_rows` gives them.
    fn programs(&self) -> Result<Vec<(i128, String, i128)>, NoPrograms> {
        if let Some(Json::Array(rows)) = self.door.ask("process_io") {
            let read: Option<Vec<(i128, String, i128)>> = rows
                .iter()
                .map(|row| match (whole(row.get("pid")), row.get("name"), whole(row.get("other_bytes"))) {
                    (Some(pid), Some(Json::Str(name)), Some(other)) => Some((pid, name.to_lowercase(), other)),
                    _ => None,
                })
                .collect();
            if let Some(read) = read {
                return Ok(read);
            }
        }
        // The helper has no answer (or one Python would read in a way of its
        // own): Python reads every program with psutil.  The backend's part.
        let Some(Json::Array(rows)) = self.the_backends("processes").and_then(|reading| reading.get("rows").cloned()) else { return Err(NoPrograms) };
        rows.iter()
            .map(|row| match row.as_array() {
                Some([pid, Json::Str(name), other]) => Some((whole(Some(pid))?, name.clone(), whole(Some(other))?)),
                _ => None,
            })
            .collect::<Option<Vec<_>>>()
            .ok_or(NoPrograms)
    }

    /// The game that runs, as the backend's game monitor says it (its exe,
    /// in small letters), or None.
    fn the_game(&self) -> Option<String> {
        match self.the_backends("game")?.get("exe") {
            Some(Json::Str(exe)) if !exe.is_empty() => Some(exe.clone()),
            _ => None,
        }
    }

    /// One sampling pass (Python's `_sample_once`).
    fn step(&self, mut sampling: MutexGuard<'_, Sampling>) {
        let now = self.door.now();
        let dt = if sampling.last_sample_t != 0.0 { (now - sampling.last_sample_t).max(0.001) } else { SAMPLE_INTERVAL };

        let (rx_total, tx_total) = self.totals(&mut sampling);
        let rx = if sampling.last_nic_rx != 0 { per_second(rx_total - sampling.last_nic_rx, dt) } else { 0 };
        let tx = if sampling.last_nic_tx != 0 { per_second(tx_total - sampling.last_nic_tx, dt) } else { 0 };
        (sampling.last_nic_rx, sampling.last_nic_tx) = (rx_total, tx_total);

        let game = self.the_game();
        let Ok(rows) = self.programs() else {
            // (as a pass of Python's whose list of programs raised: the cards'
            // counts were read, and nothing else is kept)
            self.state().passes.dropped += 1;
            return;
        };

        let mut seen_now: HashSet<i128> = HashSet::with_capacity(rows.len());
        let mut at: HashMap<i128, usize> = sampling.seen.iter().enumerate().map(|(index, seen)| (seen.pid, index)).collect();
        let mut candidates: Vec<(i128, String, i128, i128)> = Vec::new();
        for (pid, name, other) in rows {
            seen_now.insert(pid);
            let Some(&index) = at.get(&pid) else {
                at.insert(pid, sampling.seen.len());
                sampling.seen.push(Seen { pid, name, last_other: other, bps: 0, high: 0 });
                continue;
            };
            let seen = &mut sampling.seen[index];
            let bps = per_second(other - seen.last_other, dt);
            (seen.last_other, seen.bps) = (other, bps);
            seen.name = name;
            let is_the_game = game.as_deref() == Some(seen.name.as_str());
            if !is_the_game && !NEVER_FLAG.contains(&seen.name.as_str()) && bps >= BURST_BYTES_PER_SEC {
                seen.high += 1;
                if seen.high >= BURST_CONSECUTIVE {
                    candidates.push((pid, seen.name.clone(), bps, seen.high));
                }
            } else {
                seen.high = 0;
            }
        }
        sampling.seen.retain(|seen| seen_now.contains(&seen.pid));

        let game_json = game.as_deref().map_or(Json::Null, Into::into);
        // (the loudest; Python's sort keeps the first of equals)
        candidates.sort_by(|a, b| b.2.cmp(&a.2));
        let burst = candidates.first().map_or(Json::Null, |(pid, name, bps, high)| {
            Json::object([
                ("pid", Json::Int(*pid)),
                ("name", name.as_str().into()),
                ("bytes_per_sec", Json::Int(*bps)),
                ("mb_per_sec", Json::Float(py_round(*bps as f64 / 1_048_576.0, 2))),
                ("duration_sec", Json::Int(*high)),
                ("active_game", game_json.clone()),
            ])
        });
        let mut moving: Vec<&Seen> = sampling.seen.iter().filter(|seen| seen.bps > 0).collect();
        moving.sort_by(|a, b| b.bps.cmp(&a.bps));
        let top = moving
            .into_iter()
            .take(TOP)
            .map(|seen| {
                Json::object([
                    ("pid", Json::Int(seen.pid)),
                    ("name", seen.name.as_str().into()),
                    ("bytes_per_sec", Json::Int(seen.bps)),
                    ("mb_per_sec", Json::Float(py_round(seen.bps as f64 / 1_048_576.0, 3))),
                    ("is_game", (game.as_deref() == Some(seen.name.as_str())).into()),
                ])
            })
            .collect();

        {
            let mut st = self.state();
            if st.history.len() == HISTORY_LEN {
                st.history.pop_front();
            }
            st.history.push_back((now, rx, tx));
            st.latest = Latest { rx, tx, top: Json::Array(top), burst, last_sampled_at: Json::Float(now), active_game: Some(game_json) };
            st.passes.kept += 1;
        }
        sampling.last_sample_t = now;
    }
}

#[cfg(test)]
mod tests {
    use super::super::{Fake, FakeBackend, Moved};
    use super::*;

    fn j(text: &str) -> Json {
        Json::parse(text).unwrap()
    }

    struct Rig {
        machine: Arc<Fake>,
        backend: Arc<FakeBackend>,
        moved: Arc<Moved>,
    }

    fn cards(rows: &[(&str, i128, i128)]) -> Option<Json> {
        Some(Json::Array(rows.iter().map(|(name, sent, recv)| Json::object([("name", (*name).into()), ("sent", Json::Int(*sent)), ("recv", Json::Int(*recv))])).collect()))
    }

    fn programs(rows: &[(i128, &str, i128)]) -> Option<Json> {
        Some(Json::Array(rows.iter().map(|(pid, name, other)| Json::object([("pid", Json::Int(*pid)), ("name", (*name).into()), ("other_bytes", Json::Int(*other))])).collect()))
    }

    const MB: i128 = 1_048_576;

    impl Rig {
        fn new() -> Rig {
            Rig::with_threads(false)
        }

        fn with_threads(threads: bool) -> Rig {
            let (machine, backend) = (Fake::new(), FakeBackend::new());
            machine.at(1000.0);
            machine.answers("network_cards", cards(&[("Ethernet", 1_000, 2_000), ("Wi-Fi", 0, 0)]));
            machine.answers("process_io", programs(&[(4, "System", 0), (100, "Steam.exe", 10_000), (200, "cs2.exe", 0)]));
            backend.says_to(&format!("{THE_BACKENDS_PART}?name=game"), Some(j(r#"{"ok": true, "reading": {"exe": "cs2.exe"}}"#)));
            let moved = Moved::new(machine.clone(), threads);
            moved.asks_the_backend_with(backend.asker());
            Rig { machine, backend, moved }
        }

        fn call(&self, method: &str, target: &str) -> Option<(u16, Json)> {
            self.moved.answer(&Request::new(method, target)).map(|answer| (answer.status, answer.to_json()))
        }

        fn live(&self) -> Json {
            self.call("GET", "/api/network/live").unwrap().1
        }

        /// A second later: the cards and the programs have moved this much.
        fn later(&self, at: f64, sent: i128, recv: i128, steam: i128, game: i128) {
            self.machine.at(at);
            self.machine.answers("network_cards", cards(&[("Ethernet", sent, recv), ("Wi-Fi", 0, 0)]));
            self.machine.answers("process_io", programs(&[(4, "System", 0), (100, "Steam.exe", steam), (200, "cs2.exe", game)]));
            assert!(self.moved.net.round());
        }
    }

    #[test]
    fn the_first_read_starts_it_and_it_answers_noughts_until_the_second_sample() {
        let rig = Rig::new();
        let first = rig.live();
        assert_eq!(first, j(r#"{"total_rx_per_sec": 0, "total_tx_per_sec": 0, "top": [], "burst_alert": null, "last_sampled_at": 0, "history_len": 0, "running": true}"#));
        assert!(rig.moved.net.round());
        let second = rig.live();
        assert_eq!((&second["total_rx_per_sec"], &second["active_game"], &second["history_len"], &second["last_sampled_at"]),
                   (&Json::Int(0), &j("\"cs2.exe\""), &Json::Int(1), &Json::Float(1000.0)));
        rig.later(1001.0, 1_000 + 3 * MB, 2_000 + 7 * MB, 10_000 + 6 * MB, 2 * MB);
        let third = rig.live();
        assert_eq!((&third["total_rx_per_sec"], &third["total_tx_per_sec"]), (&Json::Int(7 * MB), &Json::Int(3 * MB)));
        assert_eq!(third["top"], j(&format!(r#"[{{"pid": 100, "name": "steam.exe", "bytes_per_sec": {}, "mb_per_sec": 6.0, "is_game": false}},
                                                {{"pid": 200, "name": "cs2.exe", "bytes_per_sec": {}, "mb_per_sec": 2.0, "is_game": true}}]"#, 6 * MB, 2 * MB)));
        assert_eq!(third["burst_alert"], Json::Null, "one second is no burst");
        // Three seconds in a row above 5 MB a second: a burst, the game itself never.
        rig.later(1002.0, 1_000 + 6 * MB, 2_000 + 14 * MB, 10_000 + 12 * MB, 20 * MB);
        rig.later(1003.0, 1_000 + 9 * MB, 2_000 + 21 * MB, 10_000 + 18 * MB, 40 * MB);
        let burst = rig.live()["burst_alert"].clone();
        assert_eq!(burst, j(&format!(r#"{{"pid": 100, "name": "steam.exe", "bytes_per_sec": {}, "mb_per_sec": 6.0, "duration_sec": 3, "active_game": "cs2.exe"}}"#, 6 * MB)));
        assert_eq!(rig.call("GET", "/api/network/top?limit=1").unwrap().1["top"].as_array().unwrap().len(), 1);
        assert_eq!(rig.call("GET", "/api/network/top?limit=-1").unwrap().1["top"].as_array().unwrap().len(), 1, "as Python slices a list");
        assert_eq!(rig.call("GET", "/api/network/top?limit=-5").unwrap().1["top"], j("[]"));
        assert_eq!(rig.call("GET", "/api/network/history?seconds=2").unwrap().1["history"].as_array().unwrap().len(), 3, "the samples at 1001, 1002 and 1003");
    }

    #[test]
    fn nobody_reading_for_thirty_seconds_stops_it_and_a_start_forgets_but_the_minute_of_totals() {
        let rig = Rig::new();
        rig.live();
        rig.moved.net.round();
        rig.later(1001.0, 1_000 + MB, 2_000 + MB, 10_000 + MB, 0);
        // (the PC's own clock set a long way forward while it is read: it goes on)
        rig.machine.the_pcs_clock_is_set_to(1_000_000.0);
        assert!(rig.moved.net.round() && rig.moved.net.is_running());
        rig.later(1030.0, 1_000 + 2 * MB, 2_000 + 2 * MB, 10_000 + 2 * MB, 0);
        assert!(rig.moved.net.is_running(), "29 seconds after the last read");
        rig.later(1031.0, 1_000 + 3 * MB, 2_000 + 3 * MB, 10_000 + 3 * MB, 0);
        assert!(!rig.moved.net.is_running(), "31 seconds after it");
        assert!(!rig.moved.net.round(), "a loop that was told to stop takes no sample");
        // (on the clock that does not jump: the PC's own was set back meanwhile)
        rig.machine.the_pcs_clock_is_set_to(500.0);
        let again = rig.live();
        assert_eq!((&again["running"], &again["total_rx_per_sec"], &again["top"], &again["history_len"], &again["last_sampled_at"], &again["active_game"]),
                   (&Json::Bool(true), &Json::Int(0), &j("[]"), &Json::Int(5), &Json::Float(1031.0), &j("\"cs2.exe\"")));
        // The first sample of a new run has nothing to compare with, though
        // the programs and the cards moved since the last run.
        rig.later(1040.0, 1_000 + 9 * MB, 2_000 + 9 * MB, 10_000 + 9 * MB, 5 * MB);
        assert_eq!((rig.live()["total_rx_per_sec"].clone(), rig.live()["top"].clone()), (Json::Int(0), j("[]")));
    }

    #[test]
    fn the_cards_are_counted_as_psutil_counts_them() {
        let mut wrap = Wrap::default();
        let run = |wrap: &mut Wrap, cards: &[(&str, i128, i128)]| wrap.run(cards.iter().map(|(name, sent, recv)| (name.to_string(), [*sent, *recv])).collect());
        assert_eq!(run(&mut wrap, &[("a", 100, 200)]), vec![("a".to_string(), [100, 200])]);
        assert_eq!(run(&mut wrap, &[("a", 150, 250)]), vec![("a".to_string(), [150, 250])]);
        // A count that went back: started again from nought; what it was is added on.
        assert_eq!(run(&mut wrap, &[("a", 10, 260)]), vec![("a".to_string(), [160, 260])]);
        assert_eq!(run(&mut wrap, &[("a", 20, 270), ("b", 5, 5)]), vec![("a".to_string(), [170, 270]), ("b".to_string(), [5, 5])]);
        // A card that is gone takes what was added on to it along.
        assert_eq!(run(&mut wrap, &[("b", 6, 6)]), vec![("b".to_string(), [6, 6])]);
        assert_eq!(run(&mut wrap, &[("a", 30, 280), ("b", 7, 7)]), vec![("a".to_string(), [30, 280]), ("b".to_string(), [7, 7])]);
        // Two cards of one name: the last one counts, where the first was.
        let rig = Rig::new();
        rig.machine.answers("network_cards", cards(&[("Ethernet", 10, 20), ("Wi-Fi", 1, 1), ("Ethernet", 30, 40)]));
        let mut sampling = Sampling::default();
        assert_eq!(rig.moved.net.totals(&mut sampling), (41, 31));
        // No answer, an empty list, or one Python would not have: noughts.
        for not_one in [None, Some(j("[]")), Some(j(r#"[{"name": "a", "sent": "1", "recv": 1}]"#)), Some(j("{}"))] {
            rig.machine.answers("network_cards", not_one.clone());
            assert_eq!(rig.moved.net.totals(&mut sampling), (0, 0), "{not_one:?}");
        }
    }

    #[test]
    fn where_the_helper_has_no_programs_the_backend_is_asked_and_where_it_has_none_the_pass_is_dropped() {
        let rig = Rig::new();
        rig.live();
        rig.machine.answers("process_io", None);
        rig.backend.says_to(&format!("{THE_BACKENDS_PART}?name=processes"), Some(j(r#"{"ok": true, "reading": {"rows": [[100, "steam.exe", 5], [7, "ÄPFEL.EXE", 1]]}}"#)));
        assert!(rig.moved.net.round());
        rig.machine.at(1001.0);
        rig.backend.says_to(&format!("{THE_BACKENDS_PART}?name=processes"), Some(j(r#"{"ok": true, "reading": {"rows": [[100, "steam.exe", 1005], [7, "ÄPFEL.EXE", 1]]}}"#)));
        assert!(rig.moved.net.round());
        assert_eq!(rig.live()["top"], j(r#"[{"pid": 100, "name": "steam.exe", "bytes_per_sec": 1000, "mb_per_sec": 0.001, "is_game": false}]"#),
                   "the backend's rows are taken as Python yields them (already in small letters)");
        // The part raised, or there is no backend: the pass is dropped, the
        // cards' counts were read.
        rig.backend.says_to(&format!("{THE_BACKENDS_PART}?name=processes"), Some(j(r#"{"ok": false, "raised": true}"#)));
        rig.machine.at(1002.0);
        rig.machine.answers("network_cards", cards(&[("Ethernet", 5_000, 9_000)]));
        assert!(rig.moved.net.round());
        assert_eq!((rig.moved.net.passes().kept, rig.moved.net.passes().dropped), (2, 1));
        assert_eq!(rig.live()["history_len"], Json::Int(2));
        // (the next pass works its rate out from the counts the dropped one read, over the three seconds since the last kept one)
        rig.machine.at(1004.0);
        rig.machine.answers("network_cards", cards(&[("Ethernet", 5_000 + 400, 9_000 + 800)]));
        rig.machine.answers("process_io", programs(&[(100, "Steam.exe", 2005)]));
        rig.moved.net.round();
        assert_eq!((rig.live()["total_tx_per_sec"].clone(), rig.live()["total_rx_per_sec"].clone()), (Json::Int(400 / 3), Json::Int(800 / 3)));
        // No backend at all: no game, and with the helper's programs a pass is taken.
        let rig = Rig::new();
        rig.backend.says(None);
        rig.backend.says_to(&format!("{THE_BACKENDS_PART}?name=game"), None);
        rig.live();
        rig.moved.net.round();
        assert_eq!(rig.live()["active_game"], Json::Null);
    }

    #[test]
    fn the_calls_are_taken_as_python_takes_them() {
        let rig = Rig::new();
        for (method, target) in [("GET", "/api/network/live"), ("HEAD", "/api/network/live"), ("GET", "/api/network/history"), ("GET", "/api/network/top?limit=3"),
                                 ("GET", "/api/network/%6Cive"), ("HEAD", "/api/network/top")] {
            assert_eq!(rig.call(method, target).map(|answer| answer.0), Some(200), "{method} {target}");
        }
        let head = rig.moved.answer(&Request::new("HEAD", "/api/network/history")).unwrap();
        assert!(head.body.is_empty());
        // What Python may read and this program cannot: refused, not passed on.
        for target in ["/api/network/history?seconds=%D9%A1%D9%A2", &format!("/api/network/top?limit={}", "9".repeat(60))] {
            assert_eq!(rig.call("GET", target).map(|answer| answer.0), Some(400), "{target}");
        }
        // The rest is the backend's: other methods, pausing and resuming a
        // program, the network optimizer's calls, the backend's own part.
        for (method, target) in [("POST", "/api/network/live"), ("PUT", "/api/network/history"), ("OPTIONS", "/api/network/live"), ("POST", "/api/network/pause-process"),
                                 ("POST", "/api/network/resume-process"), ("GET", "/api/network/info"), ("POST", "/api/network/optimize"), ("GET", "/api/network/ping"),
                                 ("GET", "/api/network/part?name=game"), ("GET", "/api/network/live/"), ("GET", "/api/network/Live"), ("GET", "/api/network")] {
            assert_eq!(rig.call(method, target), None, "{method} {target}");
        }
    }

    #[test]
    fn started_for_real_it_samples_once_a_second_and_stops_when_nobody_reads_it() {
        let rig = Rig::with_threads(true);
        rig.live();
        let began = std::time::Instant::now();
        while rig.moved.net.passes().kept < 2 && began.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(20));
        }
        let took = began.elapsed();
        assert!(rig.moved.net.passes().kept >= 2 && took > Duration::from_millis(900) && took < Duration::from_secs(5), "{took:?}");
        // Nobody reads it for longer than its limit (on the made-up clock): it stops by itself.
        rig.machine.at(1100.0);
        let waited = std::time::Instant::now();
        while rig.moved.net.is_running() && waited.elapsed() < Duration::from_secs(5) {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!rig.moved.net.is_running());
        let kept = rig.moved.net.passes().kept;
        std::thread::sleep(Duration::from_millis(1300));
        assert_eq!(rig.moved.net.passes().kept, kept, "a stopped sampler samples no more");
        // Read again: it starts again.  Closed, as at a quit: it stops, and no read starts it.
        rig.live();
        assert!(rig.moved.net.is_running());
        rig.moved.close();
        std::thread::sleep(Duration::from_millis(300));
        rig.live();
        let kept = rig.moved.net.passes().kept;
        std::thread::sleep(Duration::from_millis(1300));
        assert_eq!(rig.moved.net.passes().kept, kept);
    }
}
