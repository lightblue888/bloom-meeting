//! A meeting's take (contract K7): Bloom's own main window recorded through `blurt_capture` (`record.py start --engine
//! ffmpeg --window-hwnd N --status-jsonl`; a fixed region of the desktop when the window itself can't be). The island
//! shows it as the meeting's pill; once it stops, the post-meeting bar (postmeeting.rs) follows the backend's 转写 /
//! 纪要 until they are ready.
//!
//! Where it goes (K2): the inbox store's sessions (`~/Blurt/inbox/sessions/<stamp>`), which the meeting backend reads.
//!
//! Everything here runs off the main thread (blurt_capture: creating windows from the main thread deadlocks on Windows).

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use blurt_capture::overlay::OverlayConfig;
use blurt_capture::{CaptureHandle, CaptureOptions, CaptureState, Launcher, Mic, Region, StatusEvent};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};

use crate::jobs::JobWriter;
use crate::{island, jobs, l10n, paths, scripts, settings, AppState};

/// what the PC plays, as the take's second audio track (record.system_audio, off unless set in config.json)
#[cfg(windows)]
mod sysaudio;
/// a meeting's post-processing after its take (save → 转写 → 纪要), the island's one progress bar
mod postmeeting;

/// the longest a meeting's take runs before record.py stops it (see start_inner)
const MEETING_MAX_MINUTES: f64 = 720.0;

struct Take {
    session: PathBuf,
    job: Arc<JobWriter>,
    /// when the take started rolling (record.py's "started")
    since: Option<Instant>,
    #[cfg(windows)]
    system: Option<sysaudio::Loopback>,
}

#[derive(Default)]
pub struct Capture {
    handle: Mutex<Option<Arc<Mutex<CaptureHandle>>>>,
    take: Mutex<Option<Take>>,
    /// a take is starting: a second press must not start another
    busy: Mutex<bool>,
    /// the last meeting's post-processing, from its stop until a moment after its 纪要 is ready
    post: Mutex<Option<postmeeting::Post>>,
    /// for what Stop starts off the thread that sent it: the post-processing's watcher
    app: Mutex<Option<AppHandle>>,
}

/// The island's overlay window: in the UI language.
pub fn overlay_config() -> OverlayConfig {
    OverlayConfig { lang: Some(l10n::lang().into()), platform: Some(if cfg!(windows) { "win" } else { "gnome" }.into()), ..Default::default() }
}

fn mic_from_config() -> Mic {
    let v = settings::read_config();
    match v.pointer("/record/mic") {
        Some(Value::Bool(false)) => Mic::None,
        Some(Value::String(s)) if s == "off" || s == "none" => Mic::None,
        Some(Value::String(s)) if !s.is_empty() && s != "default" => Mic::Named(s.clone()),
        _ => Mic::Default,
    }
}

fn toast_error(app: &AppHandle, code: &str) {
    island::send(app, json!({"op": "toast", "kind": "error", "code": code}));
    let _ = app.emit("capture://status", json!({"ev": "error", "code": code, "msg": l10n::t(&format!("error.{code}"))}));
}

/// `<inbox store>/sessions/<stamp>` (`-2`, `-3` … when taken).
fn new_session() -> PathBuf {
    let p = paths::inbox_store().join("sessions").join(jobs::timestamp());
    if !p.exists() {
        return p;
    }
    (2..).map(|n| PathBuf::from(format!("{}-{n}", p.display()))).find(|c| !c.exists()).unwrap_or(p)
}

impl Capture {
    /// For the island, the tray and the main window: {recording, elapsed, session, starting, window}.
    pub fn state(&self) -> Value {
        let h = self.handle.lock().unwrap().clone();
        let st = h.as_ref().map(|h| h.lock().unwrap().state());
        let take = self.take.lock().unwrap();
        json!({
            "recording": matches!(st, Some(CaptureState::Starting | CaptureState::Recording | CaptureState::Paused)),
            "elapsed": take.as_ref().map(|t| t.since.map_or(0.0, |s| s.elapsed().as_secs_f64())),
            "session": take.as_ref().map(|t| scripts::s(&t.session)),
            "starting": *self.busy.lock().unwrap(),
            // every take is a meeting's: the island keeps it a pill, no caption
            "window": take.is_some(),
        })
    }

    /// The island's `postMeeting` (null when none), and whether it is about the take still finishing — the island
    /// then shows the bar instead of the recording pill.
    pub fn post_state(&self) -> (Value, bool) {
        let post = self.post.lock().unwrap();
        let Some(p) = post.as_ref() else { return (Value::Null, false) };
        let current = self.take.lock().unwrap().as_ref().is_some_and(|t| p.is_for(&t.session));
        (p.view(), current)
    }

    pub fn recording(&self) -> bool {
        self.state()["recording"] == true
    }

    /// 散会: the take stops; its post-processing bar starts now (保存录屏 while record.py finishes the file).
    pub fn stop(&self) -> Result<(), String> {
        let h = self.handle.lock().unwrap().clone().ok_or("not recording")?;
        let mut session = None;
        if let Some(t) = self.take.lock().unwrap().as_mut() {
            // what the PC played is complete before record.py reads it
            #[cfg(windows)]
            if let Some(l) = t.system.take() {
                l.stop();
            }
            session = Some(t.session.clone());
        }
        let app = self.app.lock().unwrap().clone();
        if let (Some(s), Some(app)) = (session, app) {
            begin_post(&app, &s);
        }
        let r = h.lock().unwrap().send("stop");
        r.map_err(|e| e.to_string())
    }

    fn claim(&self) -> bool {
        let mut b = self.busy.lock().unwrap();
        if *b {
            return false;
        }
        *b = true;
        true
    }

    /// A meeting: record only this window (Bloom's own), straight away — no picker, no 3-2-1, no frame. Whatever
    /// covers it, and everything around it, stays out of the video. Blocks until the take has started or failed.
    pub fn start_window(&self, app: &AppHandle, hwnd: u64) -> Result<String, String> {
        self.start(app, None, Some(hwnd))
    }

    /// A meeting where the window itself can't be recorded: this area of the desktop.
    pub fn start_region(&self, app: &AppHandle, region: Region) -> Result<String, String> {
        self.start(app, Some(region), None)
    }

    fn start(&self, app: &AppHandle, region: Option<Region>, window: Option<u64>) -> Result<String, String> {
        if self.recording() {
            return Err("already recording".into());
        }
        if !self.claim() {
            return Err("busy".into());
        }
        island::busy(app, true);
        let res = self.start_inner(app, region, window);
        *self.busy.lock().unwrap() = false;
        island::busy(app, false);
        island::refresh(app);
        res
    }

    fn start_inner(&self, app: &AppHandle, region: Option<Region>, window: Option<u64>) -> Result<String, String> {
        *self.app.lock().unwrap() = Some(app.clone());
        let runner = app.state::<AppState>().runner();
        let record_py = runner.scripts.as_ref().map(|d| d.join("record.py")).filter(|p| p.is_file()).ok_or("scripts")?;
        let launcher = match &runner.uv {
            Some(uv) => Launcher::uv(uv, &record_py),
            None => Launcher::detect(&record_py).ok_or("uv")?,
        };
        let session = new_session();
        std::fs::create_dir_all(&session).map_err(|e| format!("{}: {e}", session.display()))?;
        let _ = app.asset_protocol_scope().allow_directory(&session, true);
        let job = Arc::new(JobWriter::new("record", Some(&session), &["capture", "finalize"]));
        job.step("capture", "running", None);
        let mut opts = CaptureOptions::new(launcher, &session);
        opts.region = region;
        opts.window = window;
        opts.mic = mic_from_config();
        opts.env = scripts::agent_environment().iter().map(|(k, v)| (k.into(), v.into())).collect();
        opts.env.push(("BLURT_JOB".into(), job.id.clone().into()));
        // a meeting starts when 开会 is clicked: no 3-2-1
        opts.countdown = Some(0);
        // record.py stops by itself after 120 min, which ended a 2-hour 组会 (2026-11-21). A meeting runs until 散会;
        // 12 h only guards a meeting left on overnight (≈650 MB/h on disk, and audio.wav stays under the 4 GB WAV limit)
        opts.max_minutes = Some(MEETING_MAX_MINUTES);
        let system_audio = settings::read_config().pointer("/record/system_audio").and_then(Value::as_bool).unwrap_or(false);
        // started before record.py, so it covers the first frame (record.py cuts it to each segment)
        #[cfg(windows)]
        let system = system_audio.then(|| sysaudio::Loopback::start(&session).map_err(|e| eprintln!("blurt: system audio: {e}")).ok()).flatten();
        #[cfg(windows)]
        {
            opts.system_audio = system.is_some();
        }
        #[cfg(not(windows))]
        let _ = system_audio;
        let sdir = scripts::s(&session);
        let (app_ev, app_done, sdir2) = (app.clone(), app.clone(), sdir.clone());
        // the take exists before the recorder does: its first status line ("started") must find it
        *self.take.lock().unwrap() = Some(Take {
            session: session.clone(),
            job: job.clone(),
            since: None,
            #[cfg(windows)]
            system,
        });
        let handle = blurt_capture::record(&opts, move |e| on_status(&app_ev, e), move |res| finished(&app_done, &sdir2, res)).map_err(|e| {
            *self.take.lock().unwrap() = None;
            job.finish("failed", Some(("io", &e.to_string())));
            e.to_string()
        })?;
        *self.handle.lock().unwrap() = Some(handle);
        crate::on_recording(app);
        let _ = app.emit("capture://status", json!({"ev": "started", "session": sdir}));
        Ok(sdir)
    }
}

/// One status line of the take: its start for the timer, the mic's level and the low-space line for the island.
fn on_status(app: &AppHandle, e: &StatusEvent) {
    let cap = &app.state::<AppState>().capture;
    if let StatusEvent::Started { .. } = e {
        if let Some(t) = cap.take.lock().unwrap().as_mut() {
            t.since = Some(Instant::now());
        }
    }
    if let StatusEvent::Other(v) = e {
        if v["ev"] == "low_space" {
            let gb = v["free"].as_f64().unwrap_or(0.0) / 1e9;
            let free = if gb >= 1.0 { format!("{gb:.1} GB") } else { format!("{:.0} MB", gb * 1000.0) };
            island::send(app, json!({"op": "toast", "kind": "capture", "msg": "rec.low_space", "vars": {"free": free},
                "key": "record-low-space", "ms": 6000, "mark": "warning"}));
        }
    }
    island::send(app, json!({"op": "status", "line": e.to_json()}));
    if !matches!(e, StatusEvent::Level { .. }) {
        island::refresh(app);
    }
}

/// A saved take that wasn't plain sailing, said once after its own line (recording.md §6.6): it ended before Finish
/// ("The recorded display was disconnected — the first 3:12 is kept"), or the grabber was started again and a few
/// seconds are missing. record.py put both in meta.json.
fn say_recovered(app: &AppHandle, session: &Path, duration: f64) {
    let Some(m) = std::fs::read(session.join("meta.json")).ok().and_then(|b| serde_json::from_slice::<Value>(&b).ok()) else { return };
    let (msg, vars) = if let Some(r) = m.pointer("/ended_early/reason").and_then(Value::as_str) {
        let r = if ["display_gone", "interrupted", "disk_full"].contains(&r) { r } else { "stopped" };
        (format!("rec.partial.{r}"), json!({"length": clock(duration)}))
    } else if m["recovered"].as_array().is_some_and(|a| !a.is_empty()) {
        let lost = m["lost_seconds"].as_f64().unwrap_or(0.0).ceil().max(1.0) as u64;
        ("rec.recovered_lost".to_string(), json!({"n": lost.to_string()}))
    } else {
        return;
    };
    island::send(app, json!({"op": "toast", "kind": "capture", "msg": msg, "vars": vars, "key": "record-recovered", "ms": 8000, "mark": "warning"}));
}

/// `0:42` / `1:02:05`, as the island writes a take's length.
fn clock(secs: f64) -> String {
    let s = secs.max(0.0).round() as u64;
    if s >= 3600 {
        format!("{}:{:02}:{:02}", s / 3600, s / 60 % 60, s % 60)
    } else {
        format!("{}:{:02}", s / 60, s % 60)
    }
}

/// The take's saved cue (sounds/rec-done.wav). Never while the take records.
fn sound(app: &AppHandle, name: &str) {
    #[cfg(windows)]
    {
        let Ok(res) = app.path().resource_dir() else { return };
        #[cfg_attr(not(debug_assertions), allow(unused_mut))]
        let mut p = res.join("sounds").join(format!("{name}.wav"));
        #[cfg(debug_assertions)] // the repo's copy: dev builds only (a release build carries no build-machine path)
        if !p.is_file() {
            p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("sounds").join(format!("{name}.wav"));
        }
        use windows::Win32::Media::Audio::{PlaySoundW, SND_ASYNC, SND_FILENAME, SND_NODEFAULT};
        let w: Vec<u16> = p.to_string_lossy().encode_utf16().chain(std::iter::once(0)).collect();
        unsafe {
            let _ = PlaySoundW(windows::core::PCWSTR(w.as_ptr()), None, SND_ASYNC | SND_FILENAME | SND_NODEFAULT);
        }
    }
    #[cfg(not(windows))]
    let _ = (app, name);
}

fn finished(app: &AppHandle, session: &str, res: Result<StatusEvent, blurt_capture::CaptureError>) {
    let st = app.state::<AppState>();
    *st.capture.handle.lock().unwrap() = None;
    let take = st.capture.take.lock().unwrap().take();
    #[cfg(windows)]
    let take = take.map(|mut t| {
        if let Some(l) = t.system.take() {
            l.stop();
        }
        t
    });
    crate::on_recording(app);
    let mut v = match &res {
        Ok(e) => e.to_json(),
        Err(e) => json!({"ev": "error", "msg": e.to_string()}),
    };
    v["session"] = json!(session);
    let _ = app.emit("capture://status", &v);
    if let Some(t) = &take {
        t.job.step("capture", "done", None);
        match v["ev"].as_str() {
            Some("stopped") => {
                t.job.step("finalize", "done", None);
                t.job.finish("done", None);
            }
            Some("discarded") => t.job.finish("cancelled", None),
            _ => t.job.finish("failed", Some(("io", v["msg"].as_str().unwrap_or("recording failed")))),
        }
    }
    let duration = v["duration"].as_f64().unwrap_or(0.0);
    island::refresh(app);
    match v["ev"].as_str() {
        Some("stopped") => {
            // the take's end in the island (✓ and its length)
            island::send(app, json!({"op": "rec_done", "length": clock(duration)}));
            sound(app, "rec-done");
            say_recovered(app, Path::new(session), duration);
            // Bloom writes its own 纪要 from the transcript: the island's bar goes on with the backend's 转写 and 纪要
            post_saved(app, Path::new(session), true);
        }
        Some("discarded") => {}
        _ => post_saved(app, Path::new(session), false),
    }
}

/// A meeting's take was asked to stop: the island's post-processing bar for it (save → 转写 → 纪要), and its watcher —
/// every 0.5 s, ~/.zuhui/postmeeting.json — until a moment after the 纪要 is ready.
fn begin_post(app: &AppHandle, session: &Path) {
    let cap = &app.state::<AppState>().capture;
    {
        let mut g = cap.post.lock().unwrap();
        if g.as_ref().is_some_and(|p| p.is_for(session)) {
            return;
        }
        *g = Some(postmeeting::Post::new(session));
    }
    island::refresh(app);
    let (app, session) = (app.clone(), session.to_path_buf());
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(500));
        let cap = &app.state::<AppState>().capture;
        let poll = match cap.post.lock().unwrap().as_mut() {
            Some(p) if p.is_for(&session) => p.poll(),
            _ => return, // a newer meeting's bar replaced it
        };
        match poll {
            postmeeting::Poll::Same => {}
            postmeeting::Poll::Changed => island::refresh(&app),
            postmeeting::Poll::Drop => {
                {
                    let mut g = cap.post.lock().unwrap();
                    if g.as_ref().is_some_and(|p| p.is_for(&session)) {
                        *g = None;
                    }
                }
                island::refresh(&app);
                return;
            }
        }
    });
}

/// record.py is done with a meeting's take: the bar's first step is done (or failed).
fn post_saved(app: &AppHandle, session: &Path, ok: bool) {
    begin_post(app, session); // stopped some other way than Stop (the window closed, an error): the bar starts here
    if let Some(p) = app.state::<AppState>().capture.post.lock().unwrap().as_mut().filter(|p| p.is_for(session)) {
        p.saved(ok);
    }
    island::refresh(app);
}

/// 开会 (blurt://meeting from the meeting backend, the island's button): record Bloom's own main window only.
/// Already recording: nothing to do (a meeting picked up again after the backend restarted). Spawns: never blocks.
pub fn meeting(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let st = app.state::<AppState>();
        if st.capture.recording() {
            return;
        }
        #[cfg(windows)]
        let hwnd = app.get_webview_window("main").and_then(|w| w.hwnd().ok()).map(|h| h.0 as usize as u64);
        #[cfg(not(windows))]
        let hwnd: Option<u64> = None;
        let res = match hwnd {
            Some(h) => st.capture.start_window(&app, h),
            None => Err("no window to record".to_string()),
        };
        if let Err(e) = res {
            if e != "already recording" {
                toast_error(&app, if ["scripts", "uv", "busy"].contains(&e.as_str()) { &e } else { "unknown" });
                eprintln!("blurt: meeting record: {e}");
            }
        }
    });
}

/// Wait for a take to end (quit).
pub fn stop_and_wait(app: &AppHandle, timeout: Duration) {
    let st = app.state::<AppState>();
    let h = st.capture.handle.lock().unwrap().clone();
    if let Some(h) = h {
        let _ = h.lock().unwrap().stop_and_wait(timeout);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn take_length() {
        assert_eq!(super::clock(42.4), "0:42");
        assert_eq!(super::clock(3725.0), "1:02:05");
        assert_eq!(super::clock(-3.0), "0:00");
    }
}
