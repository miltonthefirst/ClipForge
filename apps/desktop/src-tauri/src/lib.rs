//! The ClipForge desktop shell.
//!
//! Deliberately thin: it hosts the same Angular build the PWA deploys and adds
//! no commands of its own. The reason it exists is environmental rather than
//! functional — it runs on the machine that rendered the clips, so the app's
//! local-file-server playback branch resolves and the video plays.

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
