//! Jobs (contract K4): a meeting's take is a `record` job. `$BLURT_HOME/jobs/<id>.json` is written only by the job's
//! owner; anyone else (record.py, through `BLURT_JOB`) appends to `<id>.events.jsonl`, and every reader folds those
//! events over the json the same way, so a job's state is current even while its owner isn't running. The meeting
//! backend reads them: a running `record` job is a meeting being recorded (zuhui ears.py / view.py).

use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::paths;

/// `20261111-213000`: a session folder's name, a job id's time.
pub fn timestamp() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}

/// ISO 8601 with the local offset (contract: every time on disk).
pub fn iso_now() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

/// `j-YYYYMMDD-HHMMSS-xxxx` (4 lowercase base32 characters).
pub fn new_id() -> String {
    const B32: &[u8] = b"abcdefghijklmnopqrstuvwxyz234567";
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0) as u64;
    // splitmix64 over the clock and the pid: distinct across the app's threads and processes
    let mut z = nanos ^ (u64::from(std::process::id()) << 32);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^= z >> 31;
    let tail: String = (0..4).map(|i| B32[((z >> (i * 5)) & 31) as usize] as char).collect();
    format!("j-{}-{tail}", timestamp())
}

/// `<jobs>/<id>.json` is a job; `<id>.claude-settings.json`, `.events.jsonl`, … are its attachments.
pub fn is_job_file(p: &Path) -> bool {
    let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    name.starts_with("j-") && name.ends_with(".json") && name.matches('.').count() == 1
}

/// The job id a path in `jobs/` belongs to (`j-…-ab12.events.jsonl` → `j-…-ab12`).
pub fn id_of(p: &Path) -> Option<String> {
    let name = p.file_name()?.to_string_lossy().into_owned();
    name.starts_with("j-").then(|| name.split('.').next().unwrap_or("").to_string()).filter(|s| !s.is_empty())
}

/// K4's fold: events in order; a step replaces the one with its id, `state` and `progress` replace, `items` are added
/// to the set.
pub fn fold(mut job: Value, events: &str) -> Value {
    for line in events.lines() {
        let Ok(e) = serde_json::from_str::<Value>(line.trim()) else { continue };
        let t = e.get("t").cloned().unwrap_or(Value::Null);
        if let Some(step) = e.get("step").filter(|s| s.get("id").is_some()) {
            let steps = job.get_mut("steps").and_then(Value::as_array_mut);
            match steps {
                Some(list) => match list.iter_mut().find(|s| s.get("id") == step.get("id")) {
                    Some(cur) => {
                        if let (Some(c), Some(n)) = (cur.as_object_mut(), step.as_object()) {
                            for (k, v) in n {
                                c.insert(k.clone(), v.clone());
                            }
                        }
                    }
                    None => list.push(step.clone()),
                },
                None => job["steps"] = json!([step]),
            }
        }
        if let Some(s) = e.get("state") {
            job["state"] = s.clone();
        }
        if let Some(p) = e.get("progress") {
            job["progress"] = p.clone();
        }
        if let Some(items) = e.get("items").and_then(Value::as_array) {
            let mut have: Vec<Value> = job.get("items").and_then(Value::as_array).cloned().unwrap_or_default();
            for i in items {
                if !have.contains(i) {
                    have.push(i.clone());
                }
            }
            job["items"] = Value::Array(have);
        }
        if t.is_string() {
            job["updated"] = t;
        }
    }
    job
}

fn read_events(id: &str, offset: u64) -> String {
    let p = paths::jobs_dir().join(format!("{id}.events.jsonl"));
    let Ok(mut f) = std::fs::File::open(&p) else { return String::new() };
    let mut s = String::new();
    if f.seek(SeekFrom::Start(offset)).is_ok() {
        let _ = f.read_to_string(&mut s);
    }
    s
}

/// One job's effective state.
pub fn load(id: &str) -> Option<Value> {
    let raw = std::fs::read(paths::jobs_dir().join(format!("{id}.json"))).ok()?;
    let job: Value = serde_json::from_slice(&raw).ok().filter(Value::is_object)?;
    let offset = job.get("events_offset").and_then(Value::as_u64).unwrap_or(0);
    Some(fold(job, &read_events(id, offset)))
}

pub fn is_active(j: &Value) -> bool {
    matches!(j.get("state").and_then(Value::as_str), Some("queued" | "running"))
}

/// Effective jobs, newest first (ids embed their start time).
pub fn list(active_only: bool, limit: usize) -> Vec<Value> {
    let mut ids: Vec<String> = std::fs::read_dir(paths::jobs_dir())
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| is_job_file(p))
        .filter_map(|p| id_of(&p))
        .collect();
    ids.sort_unstable_by(|a, b| b.cmp(a));
    ids.iter()
        .filter_map(|id| load(id))
        .filter(|j| j["type"] != "agent" && (!active_only || is_active(j)))
        .take(limit)
        .collect()
}

/// A job the app owns (a meeting's take). Every change rewrites `<id>.json` atomically, after
/// folding what others appended, so the file always carries the effective state.
pub struct JobWriter {
    pub id: String,
    job: Mutex<Value>,
}

impl JobWriter {
    pub fn new(kind: &str, session: Option<&Path>, steps: &[&str]) -> Self {
        let now = iso_now();
        let id = new_id();
        let job = json!({
            "schema": 1, "id": id, "type": kind, "state": "running", "owner": "app",
            "project": null, "session": session.map(|s| s.to_string_lossy().into_owned()),
            "parent": null, "title": null, "items": [], "captures": [],
            "steps": steps.iter().map(|s| json!({"id": s, "state": "pending", "progress": null, "started": null, "ended": null})).collect::<Vec<_>>(),
            "progress": null, "created": now, "started": now, "ended": null, "updated": now,
            "events_offset": 0, "error": null,
        });
        let w = JobWriter { id, job: Mutex::new(job) };
        w.save();
        w
    }

    fn save(&self) {
        let dir = paths::jobs_dir();
        let _ = std::fs::create_dir_all(&dir);
        let mut job = self.job.lock().unwrap();
        // fold what others appended since the last save, and remember how far we got
        let offset = job.get("events_offset").and_then(Value::as_u64).unwrap_or(0);
        let ev = read_events(&self.id, offset);
        if !ev.is_empty() {
            let folded = fold(job.clone(), &ev);
            *job = folded;
            job["events_offset"] = json!(offset + ev.len() as u64);
        }
        job["updated"] = json!(iso_now());
        let p = dir.join(format!("{}.json", self.id));
        let tmp = dir.join(format!(".{}.{}.tmp", self.id, std::process::id()));
        if std::fs::write(&tmp, serde_json::to_vec_pretty(&*job).unwrap_or_default()).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }

    pub fn step(&self, id: &str, state: &str, progress: Option<f64>) {
        {
            let mut job = self.job.lock().unwrap();
            let now = iso_now();
            if let Some(list) = job["steps"].as_array_mut() {
                if let Some(s) = list.iter_mut().find(|s| s["id"] == id) {
                    if state == "running" && s["started"].is_null() {
                        s["started"] = json!(now);
                    }
                    if matches!(state, "done" | "failed" | "skipped") {
                        s["ended"] = json!(now);
                    }
                    s["state"] = json!(state);
                    s["progress"] = json!(progress);
                }
            }
        }
        self.save();
    }

    /// done | failed | cancelled; open steps end as skipped (done) or failed.
    pub fn finish(&self, state: &str, error: Option<(&str, &str)>) {
        {
            let mut job = self.job.lock().unwrap();
            let now = iso_now();
            if let Some(list) = job["steps"].as_array_mut() {
                for s in list.iter_mut().filter(|s| matches!(s["state"].as_str(), Some("pending" | "running"))) {
                    s["state"] = json!(match (state, s["state"].as_str()) {
                        ("done", Some("running")) => "done",
                        ("done", _) => "skipped",
                        ("cancelled", _) => "skipped", // stopped (Stop, quitting): nothing failed
                        _ => "failed",
                    });
                    s["ended"] = json!(now);
                }
            }
            job["state"] = json!(state);
            job["ended"] = json!(now);
            if let Some((code, message)) = error {
                job["error"] = json!({"code": code, "message": message, "hint_key": null});
            }
        }
        self.save();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_and_files() {
        let id = new_id();
        assert_eq!(id.len(), "j-20261022-101500-ab12".len(), "{id}");
        assert!(id[18..].chars().all(|c| "abcdefghijklmnopqrstuvwxyz234567".contains(c)));
        assert!(is_job_file(Path::new("/j/j-20261022-101500-ab12.json")));
        assert!(!is_job_file(Path::new("/j/j-20261022-101500-ab12.claude-settings.json")));
        assert_eq!(id_of(Path::new("/j/j-20261022-101500-ab12.events.jsonl")).as_deref(), Some("j-20261022-101500-ab12"));
    }

    #[test]
    fn fold_follows_k4() {
        let job = json!({"id": "j", "type": "process", "state": "running", "items": ["u1"],
            "steps": [{"id": "prepare", "state": "done"}, {"id": "work", "state": "pending"}]});
        let ev = [
            r#"{"t":"2026-10-22T10:00:02+08:00","source":"items","event":"step","step":{"id":"work","state":"running","progress":0.5},"items":["u2","u1"]}"#,
            "not json",
            r#"{"t":"2026-10-22T10:00:03+08:00","source":"items","event":"done","state":"done","progress":1}"#,
        ]
        .join("\n");
        let f = fold(job, &ev);
        assert_eq!(f["state"], "done");
        assert_eq!(f["progress"], 1);
        assert_eq!(f["items"], json!(["u1", "u2"]));
        assert_eq!(f["steps"][1], json!({"id": "work", "state": "running", "progress": 0.5}));
        assert_eq!(f["updated"], "2026-10-22T10:00:03+08:00");
    }

    #[test]
    fn writer_folds_others_events() {
        let d = tempfile::tempdir().unwrap();
        std::env::set_var("BLURT_HOME", d.path());
        let w = JobWriter::new("record", None, &["prepare", "write"]);
        w.step("prepare", "running", None);
        let ev = paths::jobs_dir().join(format!("{}.events.jsonl", w.id));
        std::fs::write(&ev, "{\"t\":\"2026-10-22T10:00:00+08:00\",\"source\":\"items\",\"event\":\"write\",\"step\":{\"id\":\"write\",\"state\":\"done\"},\"items\":[\"u9\"]}\n").unwrap();
        w.step("prepare", "done", None);
        w.finish("done", None);
        let j = load(&w.id).unwrap();
        assert_eq!(j["state"], "done");
        assert_eq!(j["items"], json!(["u9"]));
        assert_eq!(j["steps"][1]["state"], "done");
        assert!(j["events_offset"].as_u64().unwrap() > 0);
        assert_eq!(list(false, 10).len(), 1);
        assert!(list(true, 10).is_empty());
    }
}
