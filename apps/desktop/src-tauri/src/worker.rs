//! Starting, watching and stopping the ClipForge worker from the desktop shell.
//!
//! ## Why this belongs in the desktop app and nowhere else
//!
//! ClipForge has no server-side executor. On the Spark free tier there are no
//! Cloud Functions, so the claim loop and the reaper both live inside the worker
//! process (docs/adr/0006-lease-based-job-claiming.md) and a job sits at QUEUED
//! until something on *this* machine picks it up. A phone can watch that happen;
//! it cannot make it happen. The desktop shell can, because it is already the
//! app that runs on the machine that renders — which is the entire reason it
//! exists.
//!
//! So this is the one capability that is genuinely desktop-only, and the PWA is
//! expected to degrade to read-only monitoring rather than pretend otherwise.
//!
//! ## Two sources of truth, deliberately
//!
//! The panel shows both the Firestore heartbeat and this process state, because
//! they answer different questions and each is blind where the other sees:
//!
//! - The **heartbeat** answers "is a worker serving my queue?" from anywhere,
//!   including a phone. It is up to `heartbeat_seconds` stale, and a worker that
//!   has lost its network connection looks exactly like one that is not running.
//! - **This** answers "is the process I started still alive, and what is it
//!   saying?" instantly and offline — but only for this machine.
//!
//! ## Stopping is a request, not a kill
//!
//! `worker_stop` asks the worker's loopback control API to shut down cleanly and
//! only kills the process if that fails or takes too long. The difference
//! matters more than it looks: a clean stop returns the in-flight job to the
//! queue immediately, while a kill leaves it RUNNING behind a lease nobody
//! renews — and since the reaper lives *in the worker*, on a single-worker
//! deployment nothing recovers that job until a worker is started again.
//! See `clipforge/scheduler/control.py`.

use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State};

/// The event the frontend listens on. Carries a whole [`WorkerStatus`], so a
/// listener never has to reconcile a partial update against what it already had.
const UPDATE_EVENT: &str = "clipforge://worker";

/// How many log lines to keep. Enough to cover a start-up and the stage that
/// just failed, small enough that shipping the whole ring on every new line is
/// cheaper than the bookkeeping that would avoid it.
const LOG_LINES: usize = 300;

/// Matches `Settings.local_api_port`. Overridden by `.env` when it says so —
/// see [`control_port`] for why this is read rather than assumed.
const DEFAULT_CONTROL_PORT: u16 = 8767;

/// Loopback is fast or broken; there is no slow-but-fine case worth waiting for.
const CONTROL_TIMEOUT: Duration = Duration::from_secs(3);

/// How long a clean shutdown may take before the kill. The worker gives its
/// stages 2s to checkpoint and then writes an OFFLINE heartbeat, so this is
/// generous on purpose — killing a worker that was one second from finishing
/// tidily is the exact outcome this whole path exists to avoid.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(15);

// ─────────────────────────────────────────────────────────────────────────────
// The shape the frontend sees
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Serialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum RunState {
    /// No worker of ours, and nothing answering the control port.
    Stopped,
    /// Ours, and alive.
    Running,
    /// Something is serving the control port, but we did not start it — a
    /// worker launched from a terminal. Reported rather than hidden: "there is
    /// already a worker" is the answer to why Start is unavailable.
    Foreign,
    /// Ours, and it has exited. Kept visible with its exit code, because a
    /// worker that died on startup is the case most worth reading the log for.
    Exited,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct WorkerStatus {
    pub state: RunState,
    pub pid: Option<u32>,
    pub started_at_ms: Option<u64>,
    pub exit_code: Option<i32>,
    /// Why we are not running, in a sentence the UI can show as-is.
    pub note: Option<String>,
    /// Where the worker would be started from, once resolved.
    pub repo: Option<String>,
    /// Whether this build can start a worker at all. False in a browser — the
    /// frontend never gets this far there — and false here when `uv` or the
    /// repository cannot be found.
    pub can_start: bool,
    pub control_port: u16,
    pub active_job_ids: Vec<String>,
    pub log: Vec<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Default)]
struct Inner {
    child: Option<Child>,
    pid: Option<u32>,
    started_at_ms: Option<u64>,
    exit_code: Option<i32>,
    note: Option<String>,
    /// A path the user typed, when discovery could not find the checkout.
    repo: Option<PathBuf>,
    /// Where the pieces were found last time, so they are not looked up again.
    /// See [`Inner::resolve`].
    resolved: Option<Resolved>,
    log: VecDeque<String>,
}

/// The parts of the environment that do not change while the app is open.
#[derive(Clone)]
struct Resolved {
    repo: Option<PathBuf>,
    uv: Option<PathBuf>,
    control_port: u16,
}

impl Inner {
    /// Find the checkout, `uv` and the control port — once.
    ///
    /// [`snapshot`] runs on **every log line**, and without this each of those
    /// walked the PATH, read a package directory and re-read `.env` twice. A
    /// worker mid-render emits lines faster than that is free, and the cost
    /// lands on the thread draining the pipe — the one place where falling
    /// behind blocks the worker itself.
    ///
    /// Only a *successful* resolution is cached. A failed one is retried, so
    /// installing `uv` and coming back to the panel works without a restart.
    fn resolve(&mut self, app: &AppHandle) -> Resolved {
        if let Some(resolved) = &self.resolved {
            if resolved.repo.is_some() && resolved.uv.is_some() {
                return resolved.clone();
            }
        }

        let repo = discover_repo(app, self.repo.as_deref());
        let resolved = Resolved {
            control_port: control_port(repo.as_deref()),
            uv: discover_uv(),
            repo,
        };
        self.resolved = Some(resolved.clone());
        resolved
    }

    fn push_log(&mut self, line: String) {
        if self.log.len() >= LOG_LINES {
            self.log.pop_front();
        }
        self.log.push_back(line);
    }
}

#[derive(Default)]
pub struct WorkerState {
    inner: Arc<Mutex<Inner>>,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ─────────────────────────────────────────────────────────────────────────────
// Finding the pieces
// ─────────────────────────────────────────────────────────────────────────────

fn is_repo(path: &Path) -> bool {
    path.join("apps/worker/pyproject.toml").is_file()
}

fn walk_up(from: &Path) -> Option<PathBuf> {
    let mut current = Some(from);
    while let Some(dir) = current {
        if is_repo(dir) {
            return Some(dir.to_path_buf());
        }
        current = dir.parent();
    }
    None
}

fn repo_override_file(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("worker-repo.txt"))
}

/// Where the ClipForge checkout lives.
///
/// Four sources, in descending order of "someone said so explicitly". The
/// installed app cannot infer this — an NSIS build in Program Files has no
/// relationship to a checkout on P: — so the last resort is a path the user
/// typed, remembered in the app's config directory. A development build almost
/// always resolves on step 2 and never asks.
fn discover_repo(app: &AppHandle, remembered: Option<&Path>) -> Option<PathBuf> {
    if let Some(path) = remembered {
        if is_repo(path) {
            return Some(path.to_path_buf());
        }
    }

    if let Ok(raw) = std::env::var("CLIPFORGE_REPO") {
        let path = PathBuf::from(raw);
        if is_repo(&path) {
            return Some(path);
        }
    }

    // `tauri dev` runs from src-tauri/; a bundled app runs from wherever it was
    // launched. Both are worth trying before giving up.
    if let Ok(cwd) = std::env::current_dir() {
        if let Some(found) = walk_up(&cwd) {
            return Some(found);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            if let Some(found) = walk_up(dir) {
                return Some(found);
            }
        }
    }

    let stored = repo_override_file(app)
        .and_then(|path| fs::read_to_string(path).ok())
        .map(|raw| PathBuf::from(raw.trim().to_string()))
        .filter(|path| is_repo(path));

    stored
}

const UV_EXE: &str = if cfg!(windows) { "uv.exe" } else { "uv" };

/// Locate `uv`, which is what actually runs the worker.
///
/// PATH first, then the places installers put it. The fallbacks are not
/// paranoia: a GUI process on Windows inherits the PATH that existed when the
/// shell that launched it started, so a `uv` installed after login is on the
/// user's PATH and *not* on this process's. That produces "works in my terminal,
/// not in the app", which is a miserable thing to debug from a UI.
fn discover_uv() -> Option<PathBuf> {
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            let candidate = dir.join(UV_EXE);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }

    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(home) = home_dir() {
        candidates.push(home.join(".cargo").join("bin").join(UV_EXE));
        candidates.push(home.join(".local").join("bin").join(UV_EXE));
    }

    // winget installs uv into a versioned package directory and links it from a
    // separate Links folder; the app may have neither on PATH.
    if let Some(local) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        candidates.push(local.join("Microsoft/WinGet/Links").join(UV_EXE));
        let packages = local.join("Microsoft/WinGet/Packages");
        if let Ok(entries) = fs::read_dir(&packages) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                if name.to_string_lossy().starts_with("astral-sh.uv") {
                    candidates.push(entry.path().join(UV_EXE));
                }
            }
        }
    }

    candidates.into_iter().find(|path| path.is_file())
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

/// One key out of `.env`, without pulling in a parser.
///
/// Firebase identifiers and ports are configuration, never source — the rule
/// that let ClipForge change Firebase projects in one line
/// (docs/adr/0004-dedicated-firebase-project.md). Hardcoding the control port
/// here would quietly break Stop for anyone who moved it.
fn env_value(repo: &Path, key: &str) -> Option<String> {
    let text = fs::read_to_string(repo.join(".env")).ok()?;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if let Some((name, value)) = trimmed.split_once('=') {
            if name.trim() == key {
                let value = value.trim();
                if !value.is_empty() {
                    return Some(value.to_string());
                }
            }
        }
    }
    None
}

fn control_port(repo: Option<&Path>) -> u16 {
    repo.and_then(|repo| env_value(repo, "CLIPFORGE_LOCAL_API_PORT"))
        .and_then(|raw| raw.parse().ok())
        .unwrap_or(DEFAULT_CONTROL_PORT)
}

/// The token the worker writes on first start.
///
/// In the home directory, matching `Settings.local_api_token_file`. That
/// location is the whole point: the worker starts from the repository and this
/// binary from its own build output, so a relative path would resolve to two
/// different files and the pairing would silently never match. Resolved through
/// `.env` first so that moving it moves it for both halves at once.
fn token_file(repo: Option<&Path>) -> Option<PathBuf> {
    let configured = repo.and_then(|repo| env_value(repo, "CLIPFORGE_LOCAL_API_TOKEN_FILE"));

    match configured {
        Some(raw) => match raw.strip_prefix("~/") {
            Some(rest) => Some(home_dir()?.join(rest)),
            None => Some(PathBuf::from(raw)),
        },
        None => Some(home_dir()?.join(".clipforge").join("local-api-token")),
    }
}

fn read_token(repo: Option<&Path>) -> Option<String> {
    fs::read_to_string(token_file(repo)?)
        .ok()
        .map(|raw| raw.trim().to_string())
        .filter(|token| !token.is_empty())
}

// ─────────────────────────────────────────────────────────────────────────────
// Talking to the worker's control API
// ─────────────────────────────────────────────────────────────────────────────

/// A minimal HTTP/1.1 request over loopback.
///
/// Hand-rolled rather than pulling in an HTTP client for four requests to
/// 127.0.0.1. The worker's control API refuses anything that is not loopback and
/// demands a bearer token (docs/adr/0011-local-control-api.md); sending no
/// `Origin` is correct here and is the non-browser path it already allows.
fn control_request(
    port: u16,
    token: &str,
    method: &str,
    path: &str,
) -> Result<serde_json::Value, String> {
    let address = format!("127.0.0.1:{port}");
    let socket = address
        .parse()
        .map_err(|_| format!("bad control address {address}"))?;

    let mut stream = TcpStream::connect_timeout(&socket, CONTROL_TIMEOUT)
        .map_err(|err| format!("no worker answering on {address}: {err}"))?;
    stream.set_read_timeout(Some(CONTROL_TIMEOUT)).ok();
    stream.set_write_timeout(Some(CONTROL_TIMEOUT)).ok();

    let body = "{}";
    let request = format!(
        "{method} {path} HTTP/1.1\r\n\
         Host: 127.0.0.1:{port}\r\n\
         Authorization: Bearer {token}\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {len}\r\n\
         Connection: close\r\n\
         \r\n\
         {body}",
        len = body.len(),
    );

    stream
        .write_all(request.as_bytes())
        .map_err(|err| format!("control request failed: {err}"))?;

    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|err| format!("control response failed: {err}"))?;

    let text = String::from_utf8_lossy(&raw);
    let (head, body) = text
        .split_once("\r\n\r\n")
        .ok_or_else(|| "control response had no body".to_string())?;

    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("");
    if status != "200" {
        return Err(format!("worker refused the request (HTTP {status})"));
    }

    serde_json::from_str(body).map_err(|err| format!("control response was not JSON: {err}"))
}

/// Whether *something* is serving the control port, and what it says it is.
fn probe_control(repo: Option<&Path>) -> Option<serde_json::Value> {
    let token = read_token(repo)?;
    control_request(control_port(repo), &token, "GET", "/worker").ok()
}

// ─────────────────────────────────────────────────────────────────────────────
// Status
// ─────────────────────────────────────────────────────────────────────────────

/// Build a status snapshot, reaping the child first if it has exited.
///
/// Reaping here rather than in a background thread means the state can never be
/// stale in the direction that matters: the UI is told a worker is running only
/// if it was running at the moment it asked.
fn snapshot(inner: &mut Inner, app: &AppHandle) -> WorkerStatus {
    if let Some(child) = inner.child.as_mut() {
        match child.try_wait() {
            Ok(Some(status)) => {
                inner.exit_code = status.code();
                inner.child = None;
                if inner.note.is_none() {
                    inner.note = Some(match status.code() {
                        Some(0) => "The worker stopped cleanly.".to_string(),
                        Some(code) => format!("The worker exited with code {code}."),
                        None => "The worker was terminated.".to_string(),
                    });
                }
            }
            Ok(None) => {}
            Err(err) => {
                inner.note = Some(format!("Lost track of the worker process: {err}"));
                inner.child = None;
            }
        }
    }

    let Resolved {
        repo,
        uv,
        control_port,
    } = inner.resolve(app);
    let ours = inner.child.is_some();

    // Only probe when we have no child of our own: the answer is only
    // interesting for spotting a worker someone started from a terminal, and a
    // loopback round trip on every poll is not free.
    let foreign = if ours {
        None
    } else {
        probe_control(repo.as_deref())
    };

    let active_job_ids = foreign
        .as_ref()
        .and_then(|value| value.get("activeJobIds"))
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    let state = if ours {
        RunState::Running
    } else if foreign.is_some() {
        RunState::Foreign
    } else if inner.exit_code.is_some() || inner.pid.is_some() {
        RunState::Exited
    } else {
        RunState::Stopped
    };

    let uv_found = uv.is_some();
    let note = match (&state, &repo, uv_found) {
        (RunState::Stopped, None, _) => Some(
            "Cannot find the ClipForge repository from here. Enter its path to start a worker."
                .to_string(),
        ),
        (RunState::Stopped, Some(_), false) => Some(
            "Cannot find `uv`, which runs the worker. Install it, or start the worker from a terminal."
                .to_string(),
        ),
        (RunState::Foreign, _, _) => Some(
            "A worker is already running on this machine — started outside this app.".to_string(),
        ),
        _ => inner.note.clone(),
    };

    let foreign_pid = foreign
        .as_ref()
        .and_then(|value| value.get("pid"))
        .and_then(|value| value.as_u64())
        .map(|pid| pid as u32);

    WorkerStatus {
        state,
        pid: if ours { inner.pid } else { foreign_pid },
        started_at_ms: if ours { inner.started_at_ms } else { None },
        exit_code: inner.exit_code,
        note,
        repo: repo.as_ref().map(|p| p.display().to_string()),
        can_start: repo.is_some() && uv_found,
        control_port,
        active_job_ids,
        log: inner.log.iter().cloned().collect(),
    }
}

fn emit(app: &AppHandle, status: &WorkerStatus) {
    let _ = app.emit(UPDATE_EVENT, status);
}

// ─────────────────────────────────────────────────────────────────────────────
// Commands
// ─────────────────────────────────────────────────────────────────────────────

#[tauri::command]
pub fn worker_status(app: AppHandle, state: State<'_, WorkerState>) -> WorkerStatus {
    let mut inner = state.inner.lock().unwrap();
    snapshot(&mut inner, &app)
}

/// Remember where the checkout is, for an installed build that cannot infer it.
#[tauri::command]
pub fn worker_set_repo(
    app: AppHandle,
    state: State<'_, WorkerState>,
    path: String,
) -> Result<WorkerStatus, String> {
    let candidate = PathBuf::from(path.trim());
    if !is_repo(&candidate) {
        return Err(format!(
            "{} does not look like a ClipForge checkout (no apps/worker/pyproject.toml).",
            candidate.display()
        ));
    }

    if let Some(file) = repo_override_file(&app) {
        if let Some(parent) = file.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(&file, candidate.display().to_string());
    }

    let mut inner = state.inner.lock().unwrap();
    inner.repo = Some(candidate);
    // The cache is keyed on nothing, so it has to be told the answer changed.
    inner.resolved = None;
    let status = snapshot(&mut inner, &app);
    drop(inner);
    emit(&app, &status);
    Ok(status)
}

/// Start a worker pointed at the same backend this app is.
///
/// `emulators` is passed by the frontend from its own `window.__clipforge`
/// rather than read from `.env` here, and that is the point: `.env` deliberately
/// pins `CLIPFORGE_USE_EMULATORS=true` so routine development cannot touch the
/// real project, which means a worker started without an override would poll an
/// emulator while the app watched production. Taking the flag from the app makes
/// a mismatch structurally impossible.
#[tauri::command]
pub fn worker_start(
    app: AppHandle,
    state: State<'_, WorkerState>,
    emulators: bool,
) -> Result<WorkerStatus, String> {
    let mut inner = state.inner.lock().unwrap();

    {
        let current = snapshot(&mut inner, &app);
        if current.state == RunState::Running || current.state == RunState::Foreign {
            return Err("A worker is already running on this machine.".to_string());
        }
    }

    let resolved = inner.resolve(&app);
    let repo = resolved
        .repo
        .ok_or_else(|| "Cannot find the ClipForge repository.".to_string())?;
    let uv = resolved.uv.ok_or_else(|| {
        "Cannot find `uv`, which runs the worker. Install it and try again.".to_string()
    })?;

    // Uploading clips is part of going live, not a separate switch. `.env` pins
    // CLIPFORGE_BLOB_STORE=local so routine development cannot be billed for a
    // real bucket, exactly as it pins the emulator flag — so a live worker has
    // to be told, and one pointed at the emulator must not be.
    let bucket = (!emulators)
        .then(|| repo.as_path())
        .and_then(|repo| env_value(repo, "CLIPFORGE_FIREBASE_STORAGE_BUCKET"));

    let mut command = Command::new(&uv);
    command
        .arg("run")
        .arg("--directory")
        .arg(repo.join("apps/worker"))
        .arg("clipforge-worker")
        .arg("run")
        .current_dir(&repo)
        // A real environment variable beats `.env` in pydantic-settings, which is
        // exactly how tools/worker.ps1 -Live does it too.
        .env(
            "CLIPFORGE_USE_EMULATORS",
            if emulators { "true" } else { "false" },
        )
        .env("CLIPFORGE_LOCAL_API_ENABLED", "true")
        // Without this Python buffers stdout when it is a pipe, and the log
        // pane stays empty for minutes at a time — which reads as a hung worker.
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if bucket.is_some() {
        command.env("CLIPFORGE_BLOB_STORE", "firebase");
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: without it every start flashes a console window.
        command.creation_flags(0x0800_0000);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("Could not start the worker: {err}"))?;

    let pid = child.id();
    inner.log.clear();
    inner.exit_code = None;
    inner.note = None;
    inner.pid = Some(pid);
    inner.started_at_ms = Some(now_ms());
    inner.push_log(format!(
        "starting: {} run --directory {} clipforge-worker run",
        uv.display(),
        repo.join("apps/worker").display()
    ));
    inner.push_log(format!(
        "target: {}",
        if emulators {
            "local Emulator Suite"
        } else {
            "the live Firebase project"
        }
    ));
    inner.push_log(match &bucket {
        Some(name) => format!("clips: uploaded to {name} for review"),
        None => "clips: stay on this machine".to_string(),
    });

    for pipe in [
        child.stdout.take().map(PipeKind::Out),
        child.stderr.take().map(PipeKind::Err),
    ]
    .into_iter()
    .flatten()
    {
        spawn_reader(app.clone(), state.inner.clone(), pipe);
    }

    inner.child = Some(child);
    let status = snapshot(&mut inner, &app);
    drop(inner);
    emit(&app, &status);
    Ok(status)
}

/// Ask the worker to stop, and kill it only if asking does not work.
#[tauri::command]
pub fn worker_stop(app: AppHandle, state: State<'_, WorkerState>) -> Result<WorkerStatus, String> {
    let resolved = {
        let mut inner = state.inner.lock().unwrap();
        inner.resolve(&app)
    };

    // Released before the request: a clean shutdown takes seconds, and holding
    // the lock across it would freeze every status poll the UI makes meanwhile.
    let asked = match read_token(resolved.repo.as_deref()) {
        Some(token) => {
            control_request(resolved.control_port, &token, "POST", "/worker/shutdown").is_ok()
        }
        None => false,
    };

    {
        let mut inner = state.inner.lock().unwrap();
        inner.push_log(if asked {
            "stop requested — waiting for stages to checkpoint".to_string()
        } else {
            "could not reach the worker's control API; terminating".to_string()
        });
    }

    let deadline = Instant::now() + SHUTDOWN_GRACE;
    loop {
        {
            let mut inner = state.inner.lock().unwrap();
            match inner.child.as_mut() {
                None => break,
                Some(child) => {
                    if matches!(child.try_wait(), Ok(Some(_))) {
                        break;
                    }
                }
            }

            if !asked || Instant::now() >= deadline {
                if let Some(child) = inner.child.as_mut() {
                    let _ = child.kill();
                    let _ = child.wait();
                    inner.push_log("worker terminated".to_string());
                }
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(150));
    }

    let mut inner = state.inner.lock().unwrap();
    inner.note = Some("The worker was stopped from this app.".to_string());
    let status = snapshot(&mut inner, &app);
    drop(inner);
    emit(&app, &status);
    Ok(status)
}

// ─────────────────────────────────────────────────────────────────────────────
// Log capture
// ─────────────────────────────────────────────────────────────────────────────

enum PipeKind {
    Out(std::process::ChildStdout),
    Err(std::process::ChildStderr),
}

/// Drain one pipe into the ring buffer, emitting as it goes.
///
/// The worker logs to stderr through structlog, so both pipes have to be read —
/// and read *concurrently*, because a pipe nobody drains fills up and blocks the
/// process writing to it. A worker wedged on a full stderr buffer is the classic
/// version of this bug and it looks exactly like a hang.
fn spawn_reader(app: AppHandle, inner: Arc<Mutex<Inner>>, pipe: PipeKind) {
    std::thread::spawn(move || {
        let reader: Box<dyn BufRead + Send> = match pipe {
            PipeKind::Out(out) => Box::new(BufReader::new(out)),
            PipeKind::Err(err) => Box::new(BufReader::new(err)),
        };

        for line in reader.lines() {
            let Ok(line) = line else { break };
            let status = {
                let mut guard = inner.lock().unwrap();
                guard.push_log(line);
                snapshot(&mut guard, &app)
            };
            emit(&app, &status);
        }

        // The pipe closing means the process is on its way out. Publishing one
        // last snapshot is what turns the button back into "Start" without the
        // UI having to poll for it.
        let status = {
            let mut guard = inner.lock().unwrap();
            snapshot(&mut guard, &app)
        };
        emit(&app, &status);
    });
}
