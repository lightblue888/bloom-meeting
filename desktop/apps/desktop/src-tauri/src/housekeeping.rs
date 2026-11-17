//! At launch, quietly, and the backends behind Settings → Storage:
//!
//!   housekeeping      jobs the last run left "running" marked failed (the meeting backend takes a running `record`
//!                     job for a meeting being recorded); organized recordings' videos older than
//!                     record.delete_videos_after_days to the Recycle Bin (and daily); `transcribe.py auto --apply`
//!                     (the speech models for this machine, as Automatic picks them).
//!   storage           sizes of what Bloom keeps; Move to Recycle Bin; the videos of organized recordings by age.

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager};

use crate::{jobs, paths, scripts, settings, AppState};

type R<T> = Result<T, String>;

fn set_config(pointer: &[&str], v: Option<Value>) {
    let _ = settings::update(|cfg| set_at(cfg, pointer, v));
}

fn set_at(cfg: &mut Value, pointer: &[&str], v: Option<Value>) {
    let mut cur = cfg;
    for k in &pointer[..pointer.len() - 1] {
        if cur.get(*k).and_then(Value::as_object).is_none() {
            cur[*k] = json!({});
        }
        cur = &mut cur[*k];
    }
    let last = pointer[pointer.len() - 1];
    match v {
        Some(v) => cur[last] = v,
        None => {
            if let Some(o) = cur.as_object_mut() {
                o.remove(last);
            }
        }
    }
}

// ------------------------------------------------------------------ at launch

/// Called once from setup.
pub fn start(app: &AppHandle) {
    if std::env::var_os("BLURT_NO_HOUSEKEEPING").is_some() {
        return;
    }
    let app = app.clone();
    let launched = chrono::Local::now();
    std::thread::spawn(move || {
        finish_interrupted_jobs(launched);
        delete_old_videos(&app);
        speech_models(&app);
        // daily while the app runs
        loop {
            std::thread::sleep(Duration::from_secs(24 * 3600));
            delete_old_videos(&app);
        }
    });
}

/// Jobs the app owned that were still "running" when it quit (or crashed): failed, the running step failed and the
/// rest skipped, so the job page explains it (Housekeeping.finishInterruptedJobs).
fn finish_interrupted_jobs(launched: chrono::DateTime<chrono::Local>) {
    for j in jobs::list(true, 200) {
        if j["owner"] != "app" {
            continue;
        }
        let created = j["created"].as_str().and_then(|c| chrono::DateTime::parse_from_rfc3339(c).ok());
        if !created.is_some_and(|c| c < launched - chrono::Duration::seconds(5)) {
            continue;
        }
        let Some(id) = j["id"].as_str() else { continue };
        let p = paths::jobs_dir().join(format!("{id}.json"));
        let Some(mut doc) = settings::read_json(&p) else { continue };
        if !matches!(doc["state"].as_str(), Some("running" | "queued")) {
            // its file is older than the folded events: write the folded state
            doc = j.clone();
        }
        let now = jobs::iso_now();
        doc["state"] = json!("failed");
        doc["ended"] = json!(now);
        doc["error"] = json!({"code": "unknown", "message": "Bloom quit before this job finished", "hint_key": "main.job.hint.interrupted"});
        if let Some(steps) = doc["steps"].as_array_mut() {
            for s in steps {
                match s["state"].as_str() {
                    Some("running") => {
                        s["state"] = json!("failed");
                        s["ended"] = json!(now);
                    }
                    Some("pending" | "queued") => s["state"] = json!("skipped"),
                    _ => {}
                }
            }
        }
        // keep what was folded in: the events were read into `j`
        doc["events_offset"] = j.get("events_offset").cloned().unwrap_or(doc["events_offset"].clone());
        let tmp = p.with_extension(format!("json.{}.tmp", std::process::id()));
        if std::fs::write(&tmp, serde_json::to_vec_pretty(&doc).unwrap_or_default()).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

/// K9: `transcribe.py auto --apply` picks the speech models for this machine (a new GPU, more memory …) while the choice
/// is Automatic. Never before the first run's model download was decided (setup.rs writes
/// `record.dictation.first_download`; Blurt's setup assistant wrote `app.onboarded`).
fn speech_models(app: &AppHandle) {
    let cfg = settings::read_config();
    let set_up = cfg.pointer("/app/onboarded").and_then(Value::as_bool) == Some(true) || cfg.pointer("/record/dictation/first_download").is_some();
    if !set_up {
        return;
    }
    let runner = app.state::<AppState>().runner();
    if !runner.has("transcribe.py") || runner.uv.is_none() {
        return;
    }
    let _ = runner.run("transcribe.py", &["auto".into(), "--apply".into()], Duration::from_secs(120));
}

// ------------------------------------------------------------------ storage

/// Where the takes are: the inbox store's sessions/, plus v0.6's ~/Blurt/recordings when it is still there.
pub fn session_roots() -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = [paths::inbox_store().join("sessions")].into_iter().filter(|p| p.is_dir()).collect();
    let legacy = paths::legacy_recordings();
    if legacy.is_dir() && !v.contains(&legacy) {
        v.push(legacy);
    }
    v
}

pub fn disk_size(p: &Path) -> u64 {
    let Ok(m) = std::fs::symlink_metadata(p) else { return 0 };
    if !m.is_dir() {
        return m.len();
    }
    std::fs::read_dir(p).into_iter().flatten().filter_map(Result::ok).map(|e| disk_size(&e.path())).sum()
}

/// The files that are only the video: the mp4 and the motion scan made from it.
const VIDEO_FILES: [&str; 2] = ["recording.mp4", "recording.scan.npz"];

#[derive(Debug, Clone)]
pub struct Video {
    pub session: PathBuf,
    pub files: Vec<PathBuf>,
    pub bytes: u64,
    pub age_days: f64,
}

/// The recording became items: its transcript is there and its batch isn't being made.
pub fn organized(session: &Path) -> bool {
    session.join("transcript.json").is_file()
        && settings::read_json(&session.join("batch.json")).is_some_and(|b| b.is_object() && b["processing"] != true)
}

/// Sessions whose video can go: organized, recorded at least `days` ago (None = any age), not `excluding`.
pub fn videos(roots: &[PathBuf], days: Option<u32>, excluding: Option<&Path>) -> Vec<Video> {
    let now = std::time::SystemTime::now();
    let mut out = Vec::new();
    for r in roots {
        for s in std::fs::read_dir(r).into_iter().flatten().filter_map(Result::ok).map(|e| e.path()) {
            if excluding.is_some_and(|x| x == s) {
                continue;
            }
            let Ok(m) = std::fs::metadata(s.join("recording.mp4")) else { continue };
            if !organized(&s) {
                continue;
            }
            let age = m.modified().ok().and_then(|t| now.duration_since(t).ok()).map_or(0.0, |d| d.as_secs_f64() / 86400.0);
            if days.is_some_and(|d| age < d as f64) {
                continue;
            }
            let files: Vec<PathBuf> = VIDEO_FILES.iter().map(|f| s.join(f)).filter(|f| f.exists()).collect();
            let bytes = files.iter().map(|f| disk_size(f)).sum();
            out.push(Video { session: s, files, bytes, age_days: age });
        }
    }
    out.sort_by(|a, b| b.age_days.total_cmp(&a.age_days));
    out
}

/// {"mode": "video"} — and once the video is gone {"video_removed": iso} — merged into the session's meta.json, so a
/// recording without its video never reads as a snap batch (VideoRetention.stamp).
pub fn stamp(session: &Path, removed: bool) {
    let f = session.join("meta.json");
    let mut m = settings::read_json(&f).filter(Value::is_object).unwrap_or_else(|| json!({}));
    m["mode"] = json!("video");
    if removed {
        m["video_removed"] = json!(jobs::iso_now());
    }
    let tmp = f.with_extension(format!("json.{}.tmp", std::process::id()));
    if std::fs::write(&tmp, serde_json::to_vec_pretty(&m).unwrap_or_default()).is_ok() {
        let _ = std::fs::rename(&tmp, &f);
    }
}

/// To the Recycle Bin (Windows: the shell's own delete-to-recycle-bin; Linux: `gio trash`). → what went.
pub fn recycle(files: &[PathBuf]) -> Vec<PathBuf> {
    if files.is_empty() {
        return vec![];
    }
    #[cfg(windows)]
    {
        // Microsoft.VisualBasic's FileSystem sends to the Recycle Bin without any dialog (one PowerShell for all)
        let quote = |p: &Path| format!("'{}'", p.to_string_lossy().replace('\'', "''"));
        let mut script = String::from("Add-Type -AssemblyName Microsoft.VisualBasic;");
        for f in files {
            let call = if f.is_dir() { "DeleteDirectory" } else { "DeleteFile" };
            script.push_str(&format!(
                "try {{ [Microsoft.VisualBasic.FileIO.FileSystem]::{call}({}, 'OnlyErrorDialogs', 'SendToRecycleBin') }} catch {{}};",
                quote(f)
            ));
        }
        let _ = scripts::command(&["powershell".into(), "-NoProfile".into(), "-NonInteractive".into(), "-Command".into(), script]).status();
    }
    #[cfg(not(windows))]
    {
        let mut argv = vec!["gio".to_string(), "trash".into()];
        argv.extend(files.iter().map(|f| f.to_string_lossy().into_owned()));
        let _ = scripts::command(&argv).status();
    }
    files.iter().filter(|f| !f.exists()).cloned().collect()
}

fn current_session(app: &AppHandle) -> Option<PathBuf> {
    app.state::<AppState>().capture.state()["session"].as_str().map(PathBuf::from)
}

/// The videos to the Recycle Bin, each session stamped a recording first and `video_removed` once its video went.
pub fn trash_videos(c: &[Video]) -> (usize, u64) {
    for v in c {
        stamp(&v.session, false);
    }
    let all: Vec<PathBuf> = c.iter().flat_map(|v| v.files.clone()).collect();
    let gone = recycle(&all);
    let mut n = 0;
    let mut bytes = 0;
    for v in c {
        if gone.contains(&v.session.join("recording.mp4")) {
            stamp(&v.session, true);
            n += 1;
            bytes += v.bytes;
        }
    }
    (n, bytes)
}

pub fn auto_days() -> Option<u32> {
    settings::read_config().pointer("/record/delete_videos_after_days").and_then(Value::as_u64).filter(|d| *d > 0).map(|d| d as u32)
}

/// Settings → Storage's "Delete videos automatically" (off unless the user chose an age). The recording in progress
/// is never touched.
pub fn delete_old_videos(app: &AppHandle) {
    let Some(days) = auto_days() else { return };
    let c = videos(&session_roots(), Some(days), current_session(app).as_deref());
    if !c.is_empty() {
        let _ = trash_videos(&c);
    }
}

#[tauri::command(async)]
pub fn storage_scan(app: AppHandle) -> Value {
    let roots = session_roots();
    let folders: Vec<Value> = roots
        .iter()
        .map(|r| {
            let n = std::fs::read_dir(r).into_iter().flatten().filter_map(Result::ok).filter(|e| !e.file_name().to_string_lossy().starts_with('.')).count();
            json!({"path": scripts::s(r), "bytes": disk_size(r), "sessions": n})
        })
        .collect();
    let vids = videos(&roots, None, current_session(&app).as_deref());
    let video_rows: Vec<Value> = vids.iter().map(|v| json!({"session": scripts::s(&v.session), "bytes": v.bytes, "age_days": v.age_days})).collect();
    let h = paths::blurt_home();
    let mut models = Vec::new();
    let mdir = h.join("models");
    for e in std::fs::read_dir(&mdir).into_iter().flatten().filter_map(Result::ok).map(|e| e.path()) {
        let name = e.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        if name.starts_with('.') {
            continue;
        }
        if name == "asr" || name == "polish" {
            for m in std::fs::read_dir(&e).into_iter().flatten().filter_map(Result::ok).map(|x| x.path()) {
                if !m.file_name().is_some_and(|n| n.to_string_lossy().starts_with('.')) {
                    models.push(json!({"path": scripts::s(&m), "folder": m.file_name().map(|n| n.to_string_lossy().into_owned()), "bytes": disk_size(&m)}));
                }
            }
        } else {
            models.push(json!({"path": scripts::s(&e), "folder": name, "bytes": disk_size(&e)}));
        }
    }
    // App data, as the Mac's Storage lists it (key → its row's words; removable ones may go to the Recycle Bin)
    let files: Vec<Value> = [
        ("dictation_history", "dictation/history.jsonl", false),
        ("dictation_audio", "dictation/audio", false),
        ("index", "index.db", false),
        ("build", "build", true),
        ("runs", "runs", false),
        ("jobs", "jobs", false),
        ("handoff", "handoff", false),
        ("prompts", "prompts", true),
        ("bench", "bench", true),
        ("tmp", "tmp", true),
        ("logs", "logs", false),
    ]
    .iter()
    .filter_map(|(k, rel, rm)| {
        let p = h.join(rel);
        let mut b = disk_size(&p);
        if *rel == "index.db" {
            b += disk_size(&h.join("index.db-wal")) + disk_size(&h.join("index.db-shm"));
        }
        (b > 0).then(|| json!({"key": k, "path": scripts::s(&p), "bytes": b, "removable": rm}))
    })
    .collect();
    json!({"folders": folders, "videos": video_rows, "models": models, "files": files, "auto_days": auto_days(), "available": free_bytes(&h)})
}

/// Free space on the disk that holds `p` (Storage's lead line, the Mac's "%@ available"); None when it can't be read.
fn free_bytes(p: &Path) -> Option<u64> {
    let p = std::iter::successors(Some(p), |x| x.parent()).find(|x| x.exists())?;
    #[cfg(windows)]
    {
        // .NET's DriveInfo: no extra Win32 feature for one number
        let q = scripts::s(p).replace('\'', "''");
        let script = format!("[System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot('{q}')).AvailableFreeSpace");
        let out = scripts::command(&["powershell".into(), "-NoProfile".into(), "-NonInteractive".into(), "-Command".into(), script]).output().ok()?;
        return String::from_utf8_lossy(&out.stdout).trim().parse().ok();
    }
    #[allow(unreachable_code)]
    {
        let out = std::process::Command::new("df").args(["-Pk"]).arg(p).output().ok()?;
        let text = String::from_utf8_lossy(&out.stdout).into_owned();
        let kb: u64 = text.lines().nth(1)?.split_whitespace().nth(3)?.parse().ok()?;
        Some(kb * 1024)
    }
}

/// Move to Recycle Bin, from Storage (only paths under Blurt's own folder or a session's video).
#[tauri::command(async)]
pub fn storage_trash(files: Vec<String>) -> R<Vec<String>> {
    let home = paths::blurt_home();
    let n = files.len();
    let files: Vec<PathBuf> = files.iter().map(PathBuf::from).filter(|p| p.starts_with(&home) && p != &home).collect();
    if files.len() != n {
        return Err("not Blurt's own files".into());
    }
    Ok(recycle(&files).iter().map(|p| scripts::s(p)).collect())
}

/// The videos of organized recordings older than `days` (None: all) to the Recycle Bin → {count, bytes}.
#[tauri::command(async)]
pub fn videos_trash(app: AppHandle, days: Option<u32>) -> Value {
    let c = videos(&session_roots(), days, current_session(&app).as_deref());
    let (n, bytes) = trash_videos(&c);
    json!({"count": n, "bytes": bytes})
}

/// "Delete videos automatically": None = never; an age deletes what is already older at once.
#[tauri::command(async)]
pub fn videos_auto_set(app: AppHandle, days: Option<u32>) -> Value {
    set_config(&["record", "delete_videos_after_days"], days.filter(|d| *d > 0).map(|d| json!(d)));
    delete_old_videos(&app);
    json!({"auto_days": auto_days()})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn old_organized_videos_only() {
        let d = tempfile::tempdir().unwrap();
        let s = d.path().join("20260101-100000");
        std::fs::create_dir_all(&s).unwrap();
        std::fs::write(s.join("recording.mp4"), vec![0u8; 1000]).unwrap();
        assert!(videos(&[d.path().to_path_buf()], None, None).is_empty()); // not organized yet
        std::fs::write(s.join("transcript.json"), "{}").unwrap();
        std::fs::write(s.join("batch.json"), r#"{"processing": true}"#).unwrap();
        assert!(videos(&[d.path().to_path_buf()], None, None).is_empty()); // being made
        std::fs::write(s.join("batch.json"), r#"{"processing": false}"#).unwrap();
        let v = videos(&[d.path().to_path_buf()], None, None);
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].bytes, 1000);
        assert!(videos(&[d.path().to_path_buf()], Some(30), None).is_empty()); // made today
        assert!(videos(&[d.path().to_path_buf()], None, Some(&s)).is_empty()); // the take in progress
        stamp(&s, true);
        let m = settings::read_json(&s.join("meta.json")).unwrap();
        assert_eq!(m["mode"], "video");
        assert!(m["video_removed"].is_string());
    }
}
