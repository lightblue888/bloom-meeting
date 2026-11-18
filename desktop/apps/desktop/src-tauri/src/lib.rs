//! Bloom (组会) for Windows — a Tauri 2 app derived from Blurt (MIT). It holds the meeting window (web/apps/desktop:
//! 会议 / 回顾 / 设置, the meeting backend's pages), records a meeting (Bloom's own window only, through
//! `blurt_capture` and the skill's `record.py`), and shows the top-centre island (web/apps/overlays) with the meeting's
//! pill and its post-processing bar. The meeting backend itself is the separate `zuhui` Python package (zuhui.rs).
//!
//! Rust side: the take (capture.rs), the island (island.rs), the tray, single instance, `blurt://` deep links (the
//! backend's `blurt://meeting` / `blurt://stop`), the speech model list and download (models.rs), Storage
//! (housekeeping.rs), Open at login (login.rs), uv / ffmpeg setup (setup.rs).

mod capture;
mod control;
mod folder_drop;
mod housekeeping;
mod island;
mod jobs;
mod l10n;
mod login;
mod mainwin;
mod models;
mod paths;
mod scripts;
mod settings;
mod setup;
mod tray;
mod zuhui; // the 组会 backend's start / probe

use std::sync::RwLock;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_opener::OpenerExt;

use crate::scripts::Runner;

pub struct AppState {
    runner: RwLock<Runner>,
    pub capture: capture::Capture,
}

impl AppState {
    pub fn runner(&self) -> Runner {
        self.runner.read().unwrap().clone()
    }
    /// uv was just installed (setup.rs): look for it again
    pub fn refresh_runner(&self, app: &AppHandle) {
        *self.runner.write().unwrap() = Runner::new(app);
    }
}

pub fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        // a window hidden behind tao's back (another process's ShowWindow) still counts as shown to it, and show()
        // would do nothing: hide first so the show really happens
        if !w.is_visible().unwrap_or(true) {
            let _ = w.hide();
        }
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// The main window's title in the UI language.
pub fn retitle(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_title(&l10n::t("tray.tooltip"));
    }
}

/// One place for what a deep link does (`blurt://meeting` and `blurt://stop` come from the meeting backend).
pub fn dispatch(app: &AppHandle, action: &str) {
    let st = app.state::<AppState>();
    let res = match action {
        // 开会: only Bloom's own window is recorded (zuhui's /api/meeting/start)
        "meeting" => {
            capture::meeting(app);
            Ok(())
        }
        "menu" | "open" => {
            show_main(app);
            Ok(())
        }
        "stop" if st.capture.recording() => st.capture.stop(),
        _ => Ok(()),
    };
    if let Err(e) = res {
        let _ = app.emit("capture://status", json!({"ev": "error", "msg": e}));
    }
}

/// A take started / ended: the tray's menu and icon follow.
pub fn on_recording(app: &AppHandle) {
    tray::refresh(app);
}

/// blurt://meeting · blurt://stop · blurt://open · blurt://settings[/<pane>]
pub fn handle_url(app: &AppHandle, raw: &str) {
    let Ok(url) = tauri::Url::parse(raw) else { return };
    if url.scheme() != "blurt" {
        return;
    }
    let host = url.host_str().unwrap_or("").to_string();
    match host.as_str() {
        "settings" => {
            show_main(app);
            let pane = url.path().trim_start_matches('/').to_string();
            let _ = app.emit("nav", json!({"view": "settings", "pane": pane}));
        }
        "" => show_main(app),
        other => dispatch(app, other),
    }
}

// ------------------------------------------------------------------ commands

type R<T> = Result<T, String>;

fn platform() -> &'static str {
    if cfg!(windows) {
        "win"
    } else if cfg!(target_os = "macos") {
        "mac"
    } else {
        "gnome"
    }
}

#[tauri::command]
fn app_status(app: AppHandle, state: tauri::State<'_, AppState>) -> Value {
    let r = state.runner();
    json!({
        "version": app.package_info().version.to_string(),
        "os": std::env::consts::OS,
        "platform": platform(),
        "lang": l10n::lang(),
        "blurt_home": scripts::s(&paths::blurt_home()),
        "scripts": r.scripts.as_deref().map(scripts::s),
        "uv": r.uv.as_deref().map(scripts::s),
        "capture": state.capture.state(),
    })
}

#[tauri::command]
fn settings_get() -> Value {
    json!({
        "config": settings::read_config(),
        "config_path": scripts::s(&paths::config_file()),
    })
}

#[tauri::command]
fn settings_set(app: AppHandle, patch: Value) -> R<Value> {
    let cfg = settings::merge(patch.clone())?;
    if patch.get("lang").is_some() {
        retitle(&app);
        island::refresh(&app);
        tray::refresh(&app); // the menu's words follow at once
    }
    Ok(cfg)
}

#[tauri::command(async)]
fn asr_models(state: tauri::State<'_, AppState>) -> R<Value> {
    state.runner().run("transcribe.py", &["models".into()], Duration::from_secs(60)).map_err(String::from)
}

#[tauri::command]
fn reveal(app: AppHandle, path: String) -> R<()> {
    app.opener().reveal_item_in_dir(&path).map_err(|e| e.to_string())
}

#[tauri::command]
fn open_external(app: AppHandle, url: String) -> R<()> {
    let ok = ["http://", "https://", "file://"].iter().any(|p| url.starts_with(p));
    if !ok {
        return Err(format!("not opening {url}"));
    }
    app.opener().open_url(&url, None::<&str>).map_err(|e| e.to_string())
}

#[tauri::command]
fn open_path(app: AppHandle, path: String) -> R<()> {
    // Windows: a file no app is registered for opens in Notepad, not the "Open with" chooser
    #[cfg(windows)]
    if !has_handler(&path) {
        return std::process::Command::new("notepad.exe").arg(&path).spawn().map(|_| ()).map_err(|e| e.to_string());
    }
    app.opener().open_path(&path, None::<&str>).map_err(|e| e.to_string())
}

/// Windows: does an app open this kind of file? Folders always open (Explorer).
#[cfg(windows)]
fn has_handler(path: &str) -> bool {
    use windows::core::{HSTRING, PCWSTR};
    use windows::Win32::UI::Shell::{AssocQueryStringW, ASSOCF_INIT_IGNOREUNKNOWN, ASSOCSTR_EXECUTABLE};
    let p = std::path::Path::new(path);
    if p.is_dir() {
        return true;
    }
    let Some(ext) = p.extension().and_then(|e| e.to_str()) else { return false };
    let ext = HSTRING::from(format!(".{ext}"));
    let mut len = 0u32;
    // only the length is asked for: S_FALSE with it when an app is registered, an error when none is
    let hr = unsafe { AssocQueryStringW(ASSOCF_INIT_IGNOREUNKNOWN, ASSOCSTR_EXECUTABLE, &ext, PCWSTR::null(), None, &mut len) };
    hr.is_ok() && len > 0
}

#[tauri::command]
fn clipboard_write(app: AppHandle, text: String) -> R<()> {
    app.clipboard().write_text(text).map_err(|e| e.to_string())
}

#[derive(serde::Deserialize, Default)]
#[serde(default)]
struct StartOpts {
    /// [x, y, w, h] physical px of the desktop: what is recorded when the window itself can't be
    region: Option<[i64; 4]>,
    /// 开会: record only the main window itself (it moves with the window, covered or not); `region` is the fallback
    window: Option<bool>,
}

#[tauri::command]
async fn capture_start(app: AppHandle, opts: Option<StartOpts>) -> R<String> {
    let o = opts.unwrap_or_default();
    // overlay windows must not be created on the main thread (blurt_capture)
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(windows)]
        if o.window == Some(true) {
            if let Some(h) = app.get_webview_window("main").and_then(|w| w.hwnd().ok()).map(|h| h.0 as usize as u64) {
                return app.state::<AppState>().capture.start_window(&app, h);
            }
        }
        let region = o.region.map(|[x, y, w, h]| blurt_capture::Region::new(x as i32, y as i32, w as u32, h as u32)).ok_or("no window to record")?;
        app.state::<AppState>().capture.start_region(&app, region)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command(async)]
fn capture_control(app: AppHandle, cmd: String) -> R<()> {
    match cmd.as_str() {
        "stop" => app.state::<AppState>().capture.stop(),
        _ => Err(format!("unknown control {cmd}")),
    }
}

#[tauri::command]
fn capture_state(state: tauri::State<'_, AppState>) -> Value {
    state.capture.state()
}

// ------------------------------------------------------------------ app

/// A web page that should feel like the app: WebView2's browser keys (F5 / Ctrl+R reload, Ctrl+P print, Ctrl+F find,
/// Alt+← back) do nothing — the page still gets the keydown, so the app's own Ctrl+F keeps working — and on the
/// island the browser's Back / Refresh menu never opens. The main window keeps its menu for text fields (main.tsx
/// turns it off everywhere else).
#[cfg(windows)]
fn browser_ui_off(wv: &tauri::Webview) {
    let overlay = wv.label() != "main";
    let _ = wv.with_webview(move |p| unsafe {
        use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Settings3;
        use windows::core::Interface;
        let Ok(core) = p.controller().CoreWebView2() else { return };
        let Ok(settings) = core.Settings() else { return };
        if overlay {
            let _ = settings.SetAreDefaultContextMenusEnabled(false);
        }
        if let Ok(s3) = settings.cast::<ICoreWebView2Settings3>() {
            let _ = s3.SetAreBrowserAcceleratorKeysEnabled(false);
        }
    });
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // the deep-link feature forwards blurt:// URLs in argv to on_open_url; a plain second launch shows the window
            if !argv.iter().any(|a| a.starts_with("blurt:")) {
                show_main(app);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .on_page_load(|_wv, _| {
            #[cfg(windows)]
            browser_ui_off(_wv);
        })
        .manage(island::Hub::default())
        .setup(|app| {
            let handle = app.handle().clone();
            retitle(&handle);
            app.manage(AppState { runner: RwLock::new(Runner::new(&handle)), capture: capture::Capture::default() });
            if std::env::var_os("BLURT_NO_TRAY").is_none() {
                if let Err(e) = tray::build(&handle) {
                    eprintln!("blurt: no tray: {e}");
                }
            }
            mainwin::restore(&handle); // the main window's last frame (mainwin.rs)
            folder_drop::install(&handle); // a paper dropped from Explorer (folder_drop.rs)
            // a login launch (Settings → General → Open at login) starts in the tray
            if login::launched_at_login() && handle.tray_by_id("main").is_some() {
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            // blurt:// must always open the installed app. A copy run from a build tree (target\debug, target\quick …)
            // never registers itself: a debug build that did so once sent every later link to a window that loads the
            // dev server (localhost:1420). The installed copy registers on every start, taking the link back.
            #[cfg(any(windows, target_os = "linux"))]
            {
                let in_build_tree = std::env::current_exe()
                    .map(|p| p.components().any(|c| c.as_os_str().eq_ignore_ascii_case("target")))
                    .unwrap_or(true);
                if !in_build_tree || std::env::var_os("APPIMAGE").is_some() {
                    let _ = app.deep_link().register_all();
                }
            }
            {
                let h = handle.clone();
                app.deep_link().on_open_url(move |ev| {
                    for u in ev.urls() {
                        handle_url(&h, u.as_str());
                    }
                });
            }
            if let Ok(Some(urls)) = app.deep_link().get_current() {
                for u in urls {
                    handle_url(&handle, u.as_str());
                }
            }
            zuhui::start_watchdog();
            island::init(&handle);
            if std::env::var_os("BLURT_NO_SETUP").is_none() {
                setup::start(&handle);
            }
            housekeeping::start(&handle); // interrupted jobs, old videos, the speech models' auto pick
            control::start(); // run/control.json: the meeting backend's "Bloom is running"
            Ok(())
        })
        .on_window_event(|w, ev| {
            if matches!(ev, WindowEvent::Moved(_) | WindowEvent::Resized(_) | WindowEvent::ScaleFactorChanged { .. }) {
                mainwin::changed(w);
            }
            if matches!(ev, WindowEvent::ThemeChanged(_)) {
                tray::retint(w.app_handle());
            }
            // closing the window keeps Bloom in the tray; Quit is in the tray menu
            if let WindowEvent::CloseRequested { api, .. } = ev {
                if w.label() == "main" && std::env::var_os("BLURT_QUIT_ON_CLOSE").is_none() && w.app_handle().tray_by_id("main").is_some() {
                    api.prevent_close();
                    let _ = w.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            app_status,
            settings_get,
            settings_set,
            asr_models,
            reveal,
            open_external,
            open_path,
            clipboard_write,
            capture_start,
            capture_control,
            capture_state,
            models::dictation_models,
            models::model_download,
            models::model_download_cancel,
            housekeeping::storage_scan,
            housekeeping::storage_trash,
            housekeeping::videos_trash,
            housekeeping::videos_auto_set,
            login::login_item_get,
            login::login_item_set,
            // 组会: the backend behind 会议 / 回顾 (zuhui.rs)
            zuhui::zuhui_backend_start,
            zuhui::zuhui_backend_running,
            zuhui::zuhui_pick_paper,
            zuhui::zuhui_save_text,
            zuhui::zuhui_pick_recording,
            zuhui::zuhui_pick_save,
            zuhui::zuhui_pick_folder,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Bloom");
    app.run(|app, ev| {
        if let RunEvent::Exit = ev {
            if app.state::<AppState>().capture.recording() {
                capture::stop_and_wait(app, Duration::from_secs(10)); // never lose a take on quit
            }
            control::stop();
        }
    });
}
