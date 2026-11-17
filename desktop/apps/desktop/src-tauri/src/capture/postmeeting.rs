//! Bloom: a meeting's post-processing after its take stopped — the island's one progress bar (island.ts
//! `postMeeting`). The steps are the backend's, as many as it lists, in its order (today: save 保存录屏 → transcript
//! 转写 → polish 整理 → summary 纪要), from ~/.zuhui/postmeeting.json ({meeting, session, started, title, steps:
//! [{key, label, state, started, ended, expected_s, frac}], progress, done, error}, rewritten at each step). The
//! "save" step is ours: running from the stop until record.py's last line (the backend can't see that). Polled every
//! 0.5 s while it is pending; the island drops it a moment after the last step is done (or after a failure, or when no
//! backend ever answers). The island draws the bar smoothly itself from each step's `started` and `expected_s`.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// How long the full bar stays after the last step is done, and a failure's line.
const HOLD_DONE: Duration = Duration::from_secs(4);
const HOLD_FAILED: Duration = Duration::from_secs(12);
/// Saved, but no postmeeting.json for this meeting after this long: no backend is doing the rest.
const NO_BACKEND: Duration = Duration::from_secs(90);
/// Never more than this, whatever the file says.
const MAX: Duration = Duration::from_secs(40 * 60);
/// The steps before the backend's file says which (its own four), and how long each usually takes (s).
const DEFAULT_STEPS: [(&str, &str, f64); 4] = [("save", "保存录屏", 6.0), ("transcript", "转写", 60.0), ("polish", "整理", 40.0), ("summary", "纪要", 40.0)];

pub struct Post {
    session: PathBuf,
    /// ours: "running" from the stop until record.py's last line, then "done" or "failed"
    save: &'static str,
    since: Instant,
    /// the stop and the save's end, wall clock (s): the save step's `started` / `ended`
    stop_wall: f64,
    saved_wall: Option<f64>,
    saved_at: Option<Instant>,
    /// the backend's postmeeting.json for this meeting, as last read
    file: Option<Value>,
    end: Option<Instant>,
}

pub enum Poll {
    Same,
    Changed,
    Drop,
}

fn wall() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

fn state_file() -> Option<PathBuf> {
    std::env::var_os("ZUHUI_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(|h| PathBuf::from(h).join(".zuhui")))
        .map(|d| d.join("postmeeting.json"))
}

/// "20261111-135912" (a meeting id / take folder) → "11月11日 13:59"; anything else as it is.
fn when(id: &str) -> String {
    let b = id.as_bytes();
    if id.len() >= 13 && b[8] == b'-' && id[..8].bytes().chain(id[9..13].bytes()).all(|c| c.is_ascii_digit()) {
        let n = |r: std::ops::Range<usize>| id[r].parse::<u32>().unwrap_or(0);
        return format!("{}月{}日 {}:{}", n(4..6), n(6..8), &id[9..11], &id[11..13]);
    }
    id.to_owned()
}

/// The folder's own name: the backend may write the path without the `\\?\` prefix the app's paths carry.
fn leaf(p: &str) -> Option<String> {
    Path::new(p.trim_start_matches(r"\\?\")).file_name().map(|n| n.to_string_lossy().to_lowercase())
}

fn state_of(s: &Value) -> &'static str {
    match s["state"].as_str() {
        Some("running") => "running",
        Some("done") => "done",
        Some("failed") => "failed",
        _ => "pending",
    }
}

impl Post {
    pub fn new(session: &Path) -> Self {
        Post {
            session: session.to_path_buf(),
            save: "running",
            since: Instant::now(),
            stop_wall: wall(),
            saved_wall: None,
            saved_at: None,
            file: None,
            end: None,
        }
    }

    pub fn is_for(&self, session: &Path) -> bool {
        leaf(&session.to_string_lossy()) == leaf(&self.session.to_string_lossy())
    }

    pub fn saved(&mut self, ok: bool) {
        self.save = if ok { "done" } else { "failed" };
        self.saved_at = Some(Instant::now());
        self.saved_wall = Some(wall());
    }

    /// The backend's file, if it is about this meeting (its session folder, or the meeting id = the folder's name).
    fn read(&self) -> Option<Value> {
        let v: Value = serde_json::from_slice(&std::fs::read(state_file()?).ok()?).ok()?;
        let me = leaf(&self.session.to_string_lossy())?;
        let theirs = v["session"].as_str().and_then(leaf).or_else(|| v["meeting"].as_str().map(str::to_lowercase));
        (theirs.as_deref() == Some(me.as_str())).then_some(v)
    }

    pub fn poll(&mut self) -> Poll {
        let mut changed = false;
        if let Some(v) = self.read() {
            if self.file.as_ref() != Some(&v) {
                self.file = Some(v);
                changed = true;
            }
        }
        let view = self.view();
        if self.end.is_none() && (view["done"] == true || view["failed"] == true) {
            self.end = Some(Instant::now());
            changed = true;
        }
        let hold = if view["done"] == true { HOLD_DONE } else { HOLD_FAILED };
        let gone = self.end.is_some_and(|e| e.elapsed() > hold)
            || (self.file.is_none() && self.saved_at.is_some_and(|t| t.elapsed() > NO_BACKEND))
            || self.since.elapsed() > MAX;
        if gone {
            Poll::Drop
        } else if changed {
            Poll::Changed
        } else {
            Poll::Same
        }
    }

    /// The steps as the island draws them: the backend's (any number, its order and labels; each with started /
    /// ended / expected_s / frac when it gives them), the save step's state and times ours. Before its file comes:
    /// the save step and Bloom's usual three after it.
    fn steps(&self) -> Vec<Value> {
        let listed: Vec<Value> = self.file.as_ref().and_then(|v| v["steps"].as_array().cloned()).filter(|s| !s.is_empty()).unwrap_or_else(|| {
            DEFAULT_STEPS.iter().map(|(k, l, e)| json!({"key": k, "label": l, "state": "pending", "expected_s": e})).collect()
        });
        let mut out: Vec<Value> = Vec::with_capacity(listed.len() + 1);
        if !listed.iter().any(|s| s["key"] == "save") {
            out.push(json!({"key": "save", "label": "保存录屏", "state": "pending", "expected_s": DEFAULT_STEPS[0].2}));
        }
        out.extend(listed);
        for s in &mut out {
            let k = s["key"].as_str().unwrap_or("").to_owned();
            let default_e = DEFAULT_STEPS.iter().find(|d| d.0 == k).map_or(30.0, |d| d.2);
            let e = s["expected_s"].as_f64().filter(|e| e.is_finite() && *e > 0.0).unwrap_or(default_e);
            let label = s["label"].as_str().filter(|l| !l.is_empty()).map(str::to_owned).unwrap_or_else(|| k.clone());
            let mut o = json!({"key": k, "label": label, "state": state_of(s), "expected_s": e,
                               "started": s["started"], "ended": s["ended"], "frac": s["frac"]});
            if k == "save" {
                o["state"] = json!(self.save);
                o["started"] = json!(self.stop_wall);
                o["ended"] = json!(self.saved_wall);
                o["frac"] = Value::Null;
            }
            *s = o;
        }
        out
    }

    /// The island's `postMeeting`: {meeting, steps: [{key, label, state, started, ended, expected_s, frac}], progress
    /// 0–1, done, failed, error, started}. `progress` here is a coarse floor (the steps done, by their expected
    /// lengths); the island eases the running step on from its `started` (island.ts postSmooth).
    pub fn view(&self) -> Value {
        let f = self.file.as_ref();
        let steps = self.steps();
        let total: f64 = steps.iter().map(|s| s["expected_s"].as_f64().unwrap_or(1.0).max(1.0)).sum();
        let done_w: f64 = steps
            .iter()
            .filter(|s| s["state"] == "done")
            .map(|s| s["expected_s"].as_f64().unwrap_or(1.0).max(1.0))
            .sum();
        let failed = steps.iter().any(|s| s["state"] == "failed");
        let all_done = steps.iter().all(|s| s["state"] == "done");
        let done = !failed && self.save == "done" && (all_done || (f.is_some_and(|v| v["done"] == true) && steps.iter().all(|s| s["state"] != "running")));
        let theirs = f.and_then(|v| v["progress"].as_f64()).filter(|p| p.is_finite()).unwrap_or(0.0);
        let progress = if done { 1.0 } else { (if total > 0.0 { done_w / total } else { 0.0 }).max(theirs).clamp(0.0, 0.99) };
        let error = f
            .and_then(|v| v["error"].as_str().map(str::to_owned))
            .filter(|e| !e.is_empty())
            .or_else(|| (self.save == "failed").then(|| "录屏没能保存".to_owned()));
        let failed = failed || (error.is_some() && !done);
        let id = f.and_then(|v| v["meeting"].as_str().map(str::to_owned)).or_else(|| leaf(&self.session.to_string_lossy()));
        // what the island names it by: the backend's title, else the meeting's start ("11月11日 13:59")
        let meeting = f.and_then(|v| v["title"].as_str().map(str::to_owned)).or_else(|| id.as_deref().map(when));
        json!({
            "id": id,
            "meeting": meeting,
            "steps": steps,
            "progress": progress,
            "done": done,
            "failed": failed,
            "error": error,
            "started": self.stop_wall,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn combines_our_save_with_the_backends_steps() {
        let mut p = Post::new(Path::new(r"\\?\C:\Users\x\Blurt\inbox\sessions\20261111-135912"));
        let v = p.view();
        let keys: Vec<&str> = v["steps"].as_array().unwrap().iter().map(|s| s["key"].as_str().unwrap()).collect();
        assert_eq!(keys, ["save", "transcript", "polish", "summary"], "Bloom's four until the backend says");
        assert_eq!(v["steps"][0]["state"], "running");
        assert!(v["steps"][0]["started"].as_f64().unwrap() > 1.7e9, "the save step runs from the stop");
        assert!(v["progress"].as_f64().unwrap() < 0.1);
        p.saved(true);
        p.file = Some(json!({"meeting": "m", "session": r"C:\Users\x\Blurt\inbox\sessions\20261111-135912",
            "steps": [{"key": "save", "label": "结束会议", "state": "pending", "expected_s": 5},
                      {"key": "transcript", "label": "转写", "state": "done", "started": 1.0, "ended": 2.0, "expected_s": 60},
                      {"key": "polish", "label": "整理", "state": "running", "started": 3.0, "expected_s": 30, "frac": 0.5},
                      {"key": "summary", "label": "纪要", "state": "pending", "expected_s": 25},
                      {"key": "extra", "label": "", "state": "pending"}],
            "progress": 0.9, "done": false, "error": null}));
        let v = p.view();
        let st = v["steps"].as_array().unwrap();
        assert_eq!(st.len(), 5, "as many steps as the file lists");
        assert_eq!((st[0]["state"].as_str(), st[0]["label"].as_str()), (Some("done"), Some("结束会议")), "the save state is ours");
        assert_eq!(st[2]["frac"], 0.5);
        assert_eq!((st[4]["label"].as_str(), st[4]["expected_s"].as_f64()), (Some("extra"), Some(30.0)));
        assert_eq!(v["progress"], 0.9, "the backend's own progress when it is ahead of the steps");
        assert_eq!(v["done"], false);
        let f = p.file.as_mut().unwrap();
        for s in f["steps"].as_array_mut().unwrap() {
            s["state"] = json!("done");
        }
        f["done"] = json!(true);
        let v = p.view();
        assert_eq!((v["done"].as_bool(), v["progress"].as_f64()), (Some(true), Some(1.0)));
        let mut q = Post::new(Path::new("C:/s/20261111-150000"));
        q.saved(false);
        assert_eq!(q.view()["failed"], true);
        assert!(p.is_for(Path::new(r"C:\Users\x\Blurt\inbox\sessions\20261111-135912")));
        assert_eq!(when("20261111-135912"), "11月11日 13:59");
        assert_eq!(Post::new(Path::new("C:/s/20261111-090501")).view()["meeting"], "11月11日 09:05");
    }
}
