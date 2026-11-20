//! The 组会 backend (`python -m zuhui serve`, docs/meeting-ui-spec.md): the main window's 当前会议 page reads it over
//! local HTTP on 127.0.0.1:47330. These two commands are the page's "启动" button and its probe. Nothing else of the
//! app knows about it.

use std::net::{SocketAddr, TcpStream};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::settings;

/// where `python -m zuhui serve` listens
pub const PORT: u16 = 47330;

/// The 组会 project folder: config.json's `zuhui.project_dir` (install.ps1 writes it), else where install.ps1 puts it
/// by default (%LOCALAPPDATA%\bloom-meeting\app), else an `app` folder beside this exe.
fn project_dir() -> std::path::PathBuf {
    let cfg = settings::read_config();
    if let Some(d) = cfg.pointer("/zuhui/project_dir").and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()) {
        return std::path::PathBuf::from(d);
    }
    if let Some(d) = std::env::var_os("LOCALAPPDATA").map(|l| std::path::PathBuf::from(l).join("bloom-meeting").join("app")).filter(|d| d.is_dir()) {
        return d;
    }
    std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.join("app"))).unwrap_or_else(|| std::path::PathBuf::from("app"))
}

/// The python that runs the backend: config.json's `zuhui.python`, else the project's own environment
/// (`.venv`, made by install.ps1 with uv), else `python` on PATH.
fn python_for(dir: &std::path::Path) -> String {
    if let Some(p) = settings::read_config().pointer("/zuhui/python").and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()) {
        return p.to_string();
    }
    let venv = dir.join(".venv").join("Scripts").join("python.exe");
    if venv.is_file() {
        return venv.to_string_lossy().into_owned();
    }
    "python".to_string()
}

/// 换论文: the system file picker over the main window, papers only. The chosen path (None when cancelled) goes back to
/// the page, which imports it exactly like a dropped file (POST /api/paper).
#[tauri::command(async)]
pub fn zuhui_pick_paper(app: tauri::AppHandle) -> Option<String> {
    let f = crate::island::folder_dialog(&app)
        .set_title("选择文档")
        .add_filter("文档", &["pdf", "md", "markdown", "tex", "docx", "pptx", "txt"])
        .blocking_pick_file()?;
    f.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}

/// 回顾 → 分享: the system save dialog over the main window, then the exported transcript written there (UTF-8; a TXT
/// gets a BOM so Notepad shows the Chinese right). The saved path back, None when cancelled.
#[tauri::command(async)]
pub fn zuhui_save_text(app: tauri::AppHandle, name: String, ext: String, text: String) -> Result<Option<String>, String> {
    let md = ext == "md";
    let Some(f) = crate::island::folder_dialog(&app)
        .set_title("导出逐字稿")
        .set_file_name(&name)
        .add_filter(if md { "Markdown" } else { "文本" }, if md { &["md"] } else { &["txt"] })
        .blocking_save_file()
    else {
        return Ok(None);
    };
    let path = f.into_path().map_err(|e| e.to_string())?;
    let mut bytes: Vec<u8> = Vec::with_capacity(text.len() + 3);
    if !md {
        bytes.extend_from_slice(&[0xEF, 0xBB, 0xBF]);
    }
    bytes.extend_from_slice(text.as_bytes());
    std::fs::write(&path, bytes).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(Some(path.to_string_lossy().into_owned()))
}

/// 回顾 → 保存 → 存录屏: the system save dialog over the main window, video files only. The chosen path back (None when
/// cancelled); the page then has the backend copy the recording there (POST /api/recording/save) — hundreds of MB never
/// pass through the page.
#[tauri::command(async)]
pub fn zuhui_pick_recording(app: tauri::AppHandle, name: String, ext: String) -> Option<String> {
    let ext = if ext.trim().is_empty() { "mp4".to_string() } else { ext.trim().to_string() };
    let f = crate::island::folder_dialog(&app)
        .set_title("保存录屏")
        .set_file_name(&name)
        .add_filter("视频", &[ext.as_str()])
        .blocking_save_file()?;
    f.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}

/// 回顾 → 保存 → 存成 Word / 存成 PDF / 导出会议包: the system save dialog over the main window (the page passes the
/// title in the app's language). The chosen path back (None when cancelled); the backend then writes the file there
/// (POST /api/meetings/<id>/save). Only these three kinds of file.
#[tauri::command(async)]
pub fn zuhui_pick_save(app: tauri::AppHandle, name: String, ext: String, title: Option<String>) -> Option<String> {
    let ext = ext.trim().trim_start_matches('.').to_ascii_lowercase();
    let filter = match ext.as_str() {
        "docx" => "Word",
        "pdf" => "PDF",
        "html" => "HTML",
        _ => return None,
    };
    let title = title.map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).unwrap_or_else(|| "保存".to_string());
    let f = crate::island::folder_dialog(&app)
        .set_title(&title)
        .set_file_name(&name)
        .add_filter(filter, &[ext.as_str()])
        .blocking_save_file()?;
    f.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}

/// Settings → 散会后自动保存: the system folder picker over the main window. The chosen folder back (None when
/// cancelled); the backend checks it (POST /api/autosave).
#[tauri::command(async)]
pub fn zuhui_pick_folder(app: tauri::AppHandle, title: Option<String>) -> Option<String> {
    let title = title.map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).unwrap_or_else(|| "选择文件夹".to_string());
    let f = crate::island::folder_dialog(&app).set_title(&title).blocking_pick_folder()?;
    f.into_path().ok().map(|p| p.to_string_lossy().into_owned())
}

/// `python -X utf8 -m zuhui serve` in the project folder, detached: no console window on Windows, nothing awaited.
/// Refuses when the folder isn't there (the page shows the reason), or when the port already answers.
/// When this app last spawned a backend. The page's auto-start and the watchdog both ask while a fresh backend is still
/// loading (it takes 6-9 s to open its port); without this the second ask spawned another python that only exited on
/// backend.lock.
static LAST_SPAWN: Mutex<Option<Instant>> = Mutex::new(None);

#[tauri::command(async)]
pub fn zuhui_backend_start() -> Result<(), String> {
    if zuhui_backend_running() {
        return Ok(());
    }
    let mut last = LAST_SPAWN.lock().unwrap_or_else(|e| e.into_inner());
    if last.is_some_and(|t| t.elapsed() < Duration::from_secs(15)) {
        return Ok(());
    }
    let dir = project_dir();
    if !dir.is_dir() {
        return Err(format!("zuhui project folder not found: {}", dir.display()));
    }
    let python = python_for(&dir);
    let mut cmd = Command::new(python);
    cmd.args(["-X", "utf8", "-m", "zuhui", "serve"]).current_dir(&dir).stdin(Stdio::null());
    // its output goes to ~/.zuhui/backend.log (appended), so a crash can be read afterwards
    match backend_log() {
        Some(f) => {
            let e = f.try_clone().map(Stdio::from).unwrap_or_else(|_| Stdio::null());
            cmd.stdout(Stdio::from(f)).stderr(e);
        }
        None => {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let r = cmd.spawn().map(|_| ()).map_err(|e| format!("python -m zuhui serve: {e}"));
    if r.is_ok() {
        *last = Some(Instant::now());
    }
    r
}

fn backend_log() -> Option<std::fs::File> {
    let home = std::env::var_os("ZUHUI_HOME").map(std::path::PathBuf::from).or_else(|| {
        std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(|h| std::path::PathBuf::from(h).join(".zuhui"))
    })?;
    std::fs::create_dir_all(&home).ok()?;
    std::fs::OpenOptions::new().create(true).append(true).open(home.join("backend.log")).ok()
}

/// Keeps the backend up whatever page the window shows (the 当前会议 page used to be the only one that started it,
/// so a backend that died while 回顾 or 设置 was open stayed dead): every 5 s, when nothing listens on the port, start
/// it — at most once every 20 s, once a minute after 5 starts that never came up. config.json `zuhui.watchdog: false`
/// turns it off.
pub fn start_watchdog() {
    if settings::read_config().pointer("/zuhui/watchdog").and_then(Value::as_bool) == Some(false) {
        return;
    }
    let _ = std::thread::Builder::new().name("zuhui-watchdog".into()).spawn(|| {
        let mut last: Option<std::time::Instant> = None;
        let mut tries = 0u32;
        loop {
            std::thread::sleep(Duration::from_secs(5));
            if zuhui_backend_running() {
                tries = 0;
                continue;
            }
            let gap = Duration::from_secs(if tries >= 5 { 60 } else { 20 });
            if last.map_or(true, |t| t.elapsed() >= gap) {
                last = Some(std::time::Instant::now());
                tries += 1;
                if let Err(e) = zuhui_backend_start() {
                    eprintln!("zuhui watchdog: {e}");
                }
            }
        }
    });
}

/// Is something listening on 127.0.0.1:47330? Only a connect that succeeds counts. (Windows takes about 2 s to report
/// a refused connect on localhost, so a timeout can't be read as "busy".) A busy backend still accepts at the TCP level
/// (its accept queue is 128 deep), and a second backend started by mistake exits at once on backend.lock.
#[tauri::command]
pub fn zuhui_backend_running() -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], PORT));
    TcpStream::connect_timeout(&addr, Duration::from_millis(1000)).is_ok()
}
