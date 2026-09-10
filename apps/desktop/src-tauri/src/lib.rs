//! The ClipForge desktop shell.
//!
//! Deliberately thin: it hosts the same Angular build the PWA deploys and adds
//! one capability the browser cannot have — a way to reach the worker running on
//! this machine.

mod worker;

use std::path::PathBuf;

/// Where the worker and the shell agree the pairing token lives.
///
/// The home directory, not the working directory. The worker starts from the
/// repository root and this binary from its own build output, so a relative path
/// would resolve to two different files and the pairing would silently never
/// match. `CLIPFORGE_LOCAL_API_TOKEN_FILE` overrides it for anyone who moves it.
fn token_path() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("CLIPFORGE_LOCAL_API_TOKEN_FILE") {
        let expanded = if let Some(rest) = explicit.strip_prefix("~/") {
            home_dir()?.join(rest)
        } else {
            PathBuf::from(explicit)
        };
        return Some(expanded);
    }
    Some(home_dir()?.join(".clipforge").join("local-api-token"))
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

/// What the settings page needs to talk to the worker.
///
/// The token is handed to the webview rather than the webview being given the
/// filesystem: the app needs exactly this one secret, and a general file-read
/// capability to satisfy that would be a far larger grant than the job requires.
///
/// Absent token means the worker has never run — the file is created on its
/// first start. That is a normal state, not an error, so it is reported as one.
#[tauri::command]
fn local_api() -> serde_json::Value {
    let port = std::env::var("CLIPFORGE_LOCAL_API_PORT").unwrap_or_else(|_| "8767".to_string());
    log::info!("local_api: handshake requested, port {port}");

    let token = token_path()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty());

    log::info!(
        "local_api: token file {:?}, found = {}",
        token_path(),
        token.is_some()
    );

    match token {
        Some(token) => serde_json::json!({
            "available": true,
            "origin": format!("http://127.0.0.1:{port}"),
            "token": token,
        }),
        None => serde_json::json!({
            "available": false,
            "reason": "No pairing token yet. Start the worker once — it writes the token on \
                       first run — then reopen this page.",
        }),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;
            Ok(())
        })
        .manage(worker::WorkerState::default())
        .invoke_handler(tauri::generate_handler![
            local_api,
            worker::worker_status,
            worker::worker_start,
            worker::worker_stop,
            worker::worker_set_repo,
        ])
        // Log every page the webview loads.
        //
        // This is not decoration. Google sign-in leaves the app entirely — off
        // to the Firebase auth handler, then to Google, then back — because
        // Tauri's WebView2 blocks `window.open` and the popup flow cannot be
        // used at all. When a hop in that chain fails, the window just sits
        // there, and "nothing happened" is exactly the report that started this.
        // A log of what actually loaded turns that into a diagnosis.
        .on_page_load(|webview, payload| {
            log::info!("page {:?}: {}", payload.event(), payload.url());
            let _ = webview;
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
