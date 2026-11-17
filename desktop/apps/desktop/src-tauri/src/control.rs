//! `$BLURT_HOME/run/control.json` while Bloom runs: the meeting backend (zuhui/view.py `readiness`) takes the file's
//! presence as "Bloom is running" (the meeting page's 语音识别 已连接 / 未连接). Blurt wrote it for its agent-setup
//! endpoint; Bloom has no endpoint, only the marker: {pid, app}.

use serde_json::json;

use crate::{paths, settings};

pub fn run_file() -> std::path::PathBuf {
    paths::blurt_home().join("run").join("control.json")
}

/// Written once at launch (atomically: a reader never sees half a file).
pub fn start() {
    let f = run_file();
    if let Some(d) = f.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let body = json!({"pid": std::process::id(), "app": "Bloom"}).to_string();
    let tmp = f.with_extension(format!("json.{}.tmp", std::process::id()));
    if std::fs::write(&tmp, body).is_ok() {
        let _ = std::fs::rename(&tmp, &f);
    }
}

/// The file goes with the app — only ours (another instance's stays).
pub fn stop() {
    let f = run_file();
    if settings::read_json(&f).is_some_and(|v| v["pid"] == json!(std::process::id())) {
        let _ = std::fs::remove_file(f);
    }
}
