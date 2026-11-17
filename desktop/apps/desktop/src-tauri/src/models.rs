//! The speech models Settings → Models lists and downloads (the meeting's live transcript runs on one of them, e.g.
//! Fun-ASR-Nano): `transcribe.py dictation-models` for the list, one download at a time through `transcribe.py setup
//! --model-id` — its stderr's "N/M MB" lines become `model-download` events {id, state, done_mb, total_mb, message}.

use std::io::{BufRead, BufReader};
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

use crate::{scripts, AppState};

type R<T> = Result<T, String>;

static DOWNLOADING: Mutex<Option<String>> = Mutex::new(None);

/// `dictation-models`' last answer: the size Download… promised.
static MODELS: Mutex<Option<Value>> = Mutex::new(None);

/// The download's process while it runs (Stop kills it and everything it started).
static DL_PROC: Mutex<Option<std::process::Child>> = Mutex::new(None);

/// Stop the download (the tray's and Settings' Stop): its process tree ends and what it left half-written goes
/// (`*.part`, Hugging Face's `*.incomplete`, a model's `.<dir>.tar.bz2`), so nothing half-there passes for a model.
/// → whether one was running.
#[tauri::command]
pub fn model_download_cancel() -> bool {
    let Some(mut c) = DL_PROC.lock().unwrap().take() else { return false };
    scripts::kill_tree(&mut c);
    clean_partial(&crate::paths::blurt_home().join("models"));
    true
}

fn clean_partial(dir: &std::path::Path) {
    for e in std::fs::read_dir(dir).into_iter().flatten().filter_map(Result::ok) {
        let p = e.path();
        let name = e.file_name().to_string_lossy().into_owned();
        if p.is_dir() {
            clean_partial(&p);
        } else if name.ends_with(".part") || name.ends_with(".incomplete") || (name.starts_with('.') && name.ends_with(".tar.bz2")) {
            let _ = std::fs::remove_file(&p);
        }
    }
}

/// The models this PC can download and has: {models: [{id, name, size_mb, installed, recommended…}], recommended,
/// selected, downloading}.
pub fn list(runner: &scripts::Runner, timeout: Duration) -> R<Value> {
    let v = runner.run("transcribe.py", &["dictation-models".into()], timeout).map_err(String::from)?;
    *MODELS.lock().unwrap() = Some(v.clone());
    Ok(v)
}

#[tauri::command(async)]
pub fn dictation_models(state: tauri::State<'_, AppState>) -> R<Value> {
    let mut v = list(&state.runner(), Duration::from_secs(90))?;
    v["downloading"] = json!(DOWNLOADING.lock().unwrap().clone());
    Ok(v)
}

/// Start downloading a speech model; progress arrives as `model-download` events {id, done_mb, total_mb, state}.
#[tauri::command]
pub fn model_download(app: AppHandle, id: String) -> R<()> {
    {
        let mut d = DOWNLOADING.lock().unwrap();
        if d.is_some() {
            return Err("a download is already running".into());
        }
        *d = Some(id.clone());
    }
    let runner = app.state::<AppState>().runner();
    let argv = match runner.argv("transcribe.py", &["setup".into(), "--model-id".into(), id.clone(), "--parent".into(), std::process::id().to_string()]) {
        Ok(a) => a,
        Err(e) => {
            *DOWNLOADING.lock().unwrap() = None;
            return Err(e.message);
        }
    };
    // the size Download… promised (the catalogue's size_mb): the bar counts up to it even when the archive itself is
    // smaller, so "x / y" never names a second size
    let promised = MODELS
        .lock()
        .unwrap()
        .as_ref()
        .and_then(|v| v["models"].as_array().cloned())
        .and_then(|ms| ms.iter().find(|m| m["id"] == json!(id)).and_then(|m| m["size_mb"].as_f64()))
        .unwrap_or(0.0);
    std::thread::spawn(move || {
        // its own process group (Unix) / a tree taskkill ends (Windows): Stop ends uv and the Python under it
        let mut c = scripts::daemon_command(&argv);
        c.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        let emit = |state: &str, done: f64, total: f64, msg: Option<String>| {
            let total = if total > 0.0 { total.max(promised) } else { total };
            let _ = app.emit("model-download", json!({"id": id, "state": state, "done_mb": done, "total_mb": total, "message": msg}));
        };
        // the download's last words on stderr (what failed: no uv, offline …): the log and the failed event say them
        let mut last_err = String::new();
        emit("running", 0.0, 0.0, None);
        let ok = match c.spawn() {
            Ok(mut ch) => {
                let err_pipe = ch.stderr.take();
                let out_pipe = ch.stdout.take();
                *DL_PROC.lock().unwrap() = Some(ch);
                if let Some(err) = err_pipe {
                    for line in BufReader::new(err).lines().map_while(Result::ok) {
                        if !line.trim().is_empty() && !line.trim().ends_with("MB") {
                            last_err = line.trim().chars().take(300).collect();
                        }
                        // "  412/950 MB" (transcribe.progress_line)
                        if let Some((a, b)) = line.trim().strip_suffix("MB").and_then(|x| x.trim().split_once('/')) {
                            if let (Ok(d), Ok(t)) = (a.trim().parse::<f64>(), b.trim().parse::<f64>()) {
                                emit("running", d, t, None);
                            }
                        }
                    }
                }
                let mut out = String::new();
                if let Some(mut o) = out_pipe {
                    use std::io::Read;
                    let _ = o.read_to_string(&mut out);
                }
                // Stop took the process (and waited for it): a stopped download isn't a failed one
                let Some(mut ch) = DL_PROC.lock().unwrap().take() else {
                    *DOWNLOADING.lock().unwrap() = None;
                    emit("stopped", 0.0, 0.0, None);
                    return;
                };
                let st = ch.wait().map(|s| s.success()).unwrap_or(false);
                st && out.lines().last().and_then(|l| serde_json::from_str::<Value>(l).ok()).is_some_and(|v| v["ok"] == json!(true))
            }
            Err(e) => {
                last_err = e.to_string();
                false
            }
        };
        *DOWNLOADING.lock().unwrap() = None;
        if !ok {
            eprintln!("blurt: speech model download failed: {last_err}");
        }
        emit(if ok { "done" } else { "failed" }, 0.0, 0.0, (!ok && !last_err.is_empty()).then(|| last_err.clone()));
    });
    Ok(())
}
