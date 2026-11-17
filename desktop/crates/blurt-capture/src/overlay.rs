//! The island's overlay window (Tauri 2), drawn by `web/apps/overlays`: the top-centre pill of contract K7.
//!
//! It is transparent, undecorated, always on top, on all workspaces, out of the taskbar and content-protected —
//! Windows: `WDA_EXCLUDEFROMCAPTURE` (tao), so the recording never sees it. It never takes focus (Windows
//! `WS_EX_NOACTIVATE`, GTK `accept-focus = false`) and is click-through except over what it draws.
//!
//! # What the host app provides
//! * the overlay pages at `<frontendDist>/overlays/*.html` (build `web/apps/overlays`; [`OverlayConfig::base`]);
//! * a capability that lets windows `blurt-overlay-*` use `core:default` (events).
//!
//! Call [`island`] from another thread, never the main thread: creating windows from a synchronous command deadlocks
//! on Windows.
//!
//! # Events (the overlay host protocol; `web/apps/overlays/src/host.ts` has the same names)
//! | event | direction | payload |
//! |---|---|---|
//! | `blurt://island` | Rust → island | an island op (`state`, `toast`, `status`, `hover`, `peek`, …) |
//! | `blurt://action` | island → Rust | `{label, action, …}` (`meeting.start`, `meeting.end`, `invite.copy`, …) |
//! | `blurt://hit-rects` | island → Rust | `{label, rects: [{x,y,w,h}]}` where it takes the mouse |
//! | `blurt://ready` | island → Rust | `{label, kind}` once it listens |
//!
//! Initial parameters (`lang`, `platform`) reach the page through an initialization script
//! (`window.__BLURT_OVERLAY_PARAMS__`), the same keys as the browser preview's query string.

use crate::region::{CssRect, Region};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{
    AppHandle, Emitter, EventId, Listener, Manager, Monitor, PhysicalPosition, PhysicalSize, Runtime, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder, WindowEvent,
};

/// WebView2's browser arguments: the same as the main window's `additionalBrowserArgs` (tauri.conf.json). Webviews that
/// share a data folder must be started with the same arguments, or the second one fails to open. Tauri's three defaults,
/// plus no media keys / media session (no "tauri.localhost" media flyout while the 回顾 recording plays).
pub const BROWSER_ARGS: &str =
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection,HardwareMediaKeyHandling,MediaSessionService";

pub mod ev {
    pub const ISLAND: &str = "blurt://island";
    pub const ACTION: &str = "blurt://action";
    pub const HIT_RECTS: &str = "blurt://hit-rects";
    pub const READY: &str = "blurt://ready";
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Island,
}

impl Kind {
    pub fn page(self) -> &'static str {
        match self {
            Kind::Island => "island.html",
        }
    }

    /// Window label (`blurt-overlay-*`: what the app's capability lists).
    pub fn label(self) -> &'static str {
        match self {
            Kind::Island => "blurt-overlay-island",
        }
    }
}

#[derive(Debug, Clone)]
pub struct OverlayConfig {
    /// Path of the overlay pages inside the app's frontendDist (default `overlays/`).
    pub base: String,
    /// `en` / `zh-Hans`; default: the page follows the system language.
    pub lang: Option<String>,
    /// `win` / `gnome` token set; default: from the user agent.
    pub platform: Option<String>,
    /// Let screenshots see the overlays (tests only: content protection hides them from every capture on Windows
    /// and macOS). Default: `BLURT_OVERLAY_CAPTURABLE=1` in the environment.
    pub capturable: bool,
}

impl Default for OverlayConfig {
    fn default() -> Self {
        Self {
            base: "overlays/".into(),
            lang: None,
            platform: None,
            capturable: std::env::var("BLURT_OVERLAY_CAPTURABLE").as_deref() == Ok("1"),
        }
    }
}

fn monitor_region(m: &Monitor) -> Region {
    Region::new(m.position().x, m.position().y, m.size().width, m.size().height)
}

/// The monitor under the pointer (else the primary one).
fn monitor_at_cursor<R: Runtime>(app: &AppHandle<R>) -> Option<Monitor> {
    let monitors = app.available_monitors().ok()?;
    if let Ok(p) = app.cursor_position() {
        if let Some(m) = monitors.iter().find(|m| monitor_region(m).contains(p.x as i32, p.y as i32)) {
            return Some(m.clone());
        }
    }
    app.primary_monitor().ok().flatten().or_else(|| monitors.into_iter().next())
}

fn monitor_containing<R: Runtime>(app: &AppHandle<R>, r: &Region) -> Option<Monitor> {
    let (cx, cy) = (r.x + r.w as i32 / 2, r.y + r.h as i32 / 2);
    app.available_monitors()
        .ok()?
        .into_iter()
        .find(|m| monitor_region(m).contains(cx, cy))
        .or_else(|| monitor_at_cursor(app))
}

/// Opens (or replaces) one overlay window covering `at` (physical px), hidden until placed, then shown.
fn open<R: Runtime>(
    app: &AppHandle<R>,
    kind: Kind,
    cfg: &OverlayConfig,
    at: Region,
    params: BTreeMap<&str, String>,
) -> tauri::Result<WebviewWindow<R>> {
    if let Some(old) = app.get_webview_window(kind.label()) {
        let _ = old.destroy();
    }
    let scale = monitor_containing(app, &at).map(|m| m.scale_factor()).unwrap_or(1.0);
    let mut p: BTreeMap<&str, String> = params;
    if let Some(l) = &cfg.lang {
        p.entry("lang").or_insert_with(|| l.clone());
    }
    if let Some(pl) = &cfg.platform {
        p.entry("platform").or_insert_with(|| pl.clone());
    }
    let init = format!("window.__BLURT_OVERLAY_PARAMS__ = {};", serde_json::to_string(&p).unwrap_or_default());
    let url = WebviewUrl::App(format!("{}{}", cfg.base, kind.page()).into());
    let w = WebviewWindowBuilder::new(app, kind.label(), url)
        .title("Blurt")
        .additional_browser_args(BROWSER_ARGS)
        .initialization_script(init)
        .transparent(true)
        .background_color(tauri::webview::Color(0, 0, 0, 0))
        .decorations(false)
        .shadow(false)
        .resizable(false)
        .skip_taskbar(true)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .content_protected(!cfg.capturable)
        .focusable(false)
        .focused(false)
        .position(at.x as f64 / scale, at.y as f64 / scale)
        .inner_size(at.w as f64 / scale, at.h as f64 / scale)
        .visible(false)
        .build()?;
    w.set_position(PhysicalPosition::new(at.x, at.y))?;
    w.set_size(PhysicalSize::new(at.w, at.h))?;
    w.show()?;
    Ok(w)
}

fn label_of(payload: &Value) -> &str {
    payload.get("label").and_then(Value::as_str).unwrap_or("")
}

/// Listens to `event` from the overlay window `label` only.
pub fn listen_from<R: Runtime>(
    app: &AppHandle<R>,
    event: &'static str,
    label: &'static str,
    f: impl Fn(Value) + Send + 'static,
) -> EventId {
    app.listen_any(event, move |e| {
        if let Ok(v) = serde_json::from_str::<Value>(e.payload()) {
            if label_of(&v) == label {
                f(v);
            }
        }
    })
}

// ------------------------------------------------------------------------------------------------ island

/// The island window: the monitor's whole work area. Click-through except over what is drawn. At least this tall
/// (logical px) on a very small screen.
pub const ISLAND_H: f64 = 600.0;
/// K7: on a screen without a notch the pill hides when idle; the pointer resting 250 ms in the top-middle 320×8 zone
/// brings it out.
const PEEK: (f64, f64, Duration) = (320.0, 8.0, Duration::from_millis(250));
/// Ops the page needs again after a reload (sent on `ready`).
const STICKY: [&str; 1] = ["state"];

pub struct Island<R: Runtime> {
    app: AppHandle<R>,
    window: WebviewWindow<R>,
    sticky: Arc<Mutex<BTreeMap<String, Value>>>,
    listeners: Mutex<Vec<EventId>>,
    alive: Arc<AtomicBool>,
    at: Arc<Mutex<(Region, f64)>>,
}

fn island_rect(m: Option<&Monitor>) -> (Region, f64) {
    m.map(|m| {
        let a = m.work_area();
        let s = m.scale_factor();
        (Region::new(a.position.x, a.position.y, a.size.width, a.size.height.max((ISLAND_H * s).round() as u32)), s)
    })
    .unwrap_or((Region::new(0, 0, 1920, ISLAND_H as u32), 1.0))
}

pub fn island<R: Runtime>(app: &AppHandle<R>, cfg: &OverlayConfig) -> tauri::Result<Arc<Island<R>>> {
    let (at, scale) = island_rect(monitor_at_cursor(app).as_ref());
    let rects: Arc<Mutex<Vec<CssRect>>> = Arc::default();
    let sticky: Arc<Mutex<BTreeMap<String, Value>>> = Arc::default();
    let label = Kind::Island.label();
    let mut listeners = Vec::new();
    let r2 = rects.clone();
    // nothing on the island (no open body, toast, take, voice note, dictation, menu or panel): it may move screens
    let idle = Arc::new(AtomicBool::new(true));
    let i2 = idle.clone();
    listeners.push(listen_from(app, ev::HIT_RECTS, label, move |v| {
        if let Ok(r) = serde_json::from_value::<Vec<CssRect>>(v["rects"].clone()) {
            *r2.lock().unwrap() = r;
        }
        i2.store(v["idle"].as_bool().unwrap_or(false), Ordering::SeqCst);
    }));
    let (app2, s2) = (app.clone(), sticky.clone());
    listeners.push(listen_from(app, ev::READY, label, move |_| {
        for op in s2.lock().unwrap().values() {
            let _ = app2.emit_to(label, ev::ISLAND, op.clone());
        }
    }));
    let window = open(app, Kind::Island, cfg, at, BTreeMap::new())?;
    // Linux: tao's click-through is an input shape too, applied later on its own loop — it would undo ours
    #[cfg(not(target_os = "linux"))]
    let _ = window.set_ignore_cursor_events(true);
    #[cfg(target_os = "linux")]
    input_shape(&window, &[]);
    let alive = Arc::new(AtomicBool::new(true));
    let a2 = alive.clone();
    window.on_window_event(move |e| {
        if matches!(e, WindowEvent::Destroyed) {
            a2.store(false, Ordering::SeqCst);
        }
    });
    let at = Arc::new(Mutex::new((at, scale)));
    spawn_hit_tester(app.clone(), window.clone(), rects, alive.clone(), at.clone(), idle);
    Ok(Arc::new(Island { app: app.clone(), window, sticky, listeners: Mutex::new(listeners), alive, at }))
}

/// The island's input region: its drawn rects (CSS px = GDK's logical px), the rest of the window passes clicks on.
/// An X input shape on the toplevel, which mutter honours for XWayland windows.
#[cfg(target_os = "linux")]
fn input_shape<R: Runtime>(w: &WebviewWindow<R>, rects: &[CssRect]) {
    let (w2, rects) = (w.clone(), rects.to_vec());
    let _ = w.run_on_main_thread(move || {
        use gtk::prelude::*;
        let Ok(gw) = w2.gtk_window() else { return };
        let region = gtk::cairo::Region::create();
        for r in &rects {
            let rect = gtk::cairo::RectangleInt::new(r.x.floor() as i32, r.y.floor() as i32, r.w.ceil() as i32 + 1, r.h.ceil() as i32 + 1);
            let _ = region.union_rectangle(&rect);
        }
        gw.input_shape_combine_region(Some(&region));
    });
}

/// Where the island window should be now: on its screen as that screen is now (a resolution, scaling or work-area
/// change, an RDP reconnect at another size), or — while nothing shows on it — on the screen with the pointer (K7:
/// the island shows where you are; Island.swift poll).
fn island_target<R: Runtime>(app: &AppHandle<R>, cur: &(Region, f64), idle: bool) -> (Region, f64) {
    if idle {
        island_rect(monitor_at_cursor(app).as_ref())
    } else {
        island_rect(monitor_containing(app, &cur.0).as_ref())
    }
}

/// ~30 Hz: take the mouse only over the drawn rects; a pointer resting in the top-middle zone reveals the island.
/// Once a second: the window follows its screen (island_target).
fn spawn_hit_tester<R: Runtime>(
    app: AppHandle<R>,
    w: WebviewWindow<R>,
    rects: Arc<Mutex<Vec<CssRect>>>,
    alive: Arc<AtomicBool>,
    at: Arc<Mutex<(Region, f64)>>,
    idle: Arc<AtomicBool>,
) {
    thread::spawn(move || {
        let mut ignoring = true;
        let mut peek_since: Option<Instant> = None;
        let mut tick: u32 = 0;
        #[cfg(target_os = "linux")]
        let mut shaped: Vec<CssRect> = vec![];
        while alive.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(33));
            tick = tick.wrapping_add(1);
            // Linux (XWayland): the pointer's position reaches an X client only while it is over one of its windows, so
            // a click-through island never sees it coming; the window takes clicks where its rects are instead
            #[cfg(target_os = "linux")]
            {
                let now = rects.lock().unwrap().clone();
                if now != shaped {
                    input_shape(&w, &now);
                    shaped = now;
                }
            }
            if tick % 30 == 0 {
                let cur = *at.lock().unwrap();
                let want = island_target(&app, &cur, idle.load(Ordering::SeqCst));
                if want != cur {
                    *at.lock().unwrap() = want;
                    // position first: arriving on a screen of another scale resizes the window, then the size lands
                    let _ = w.set_position(PhysicalPosition::new(want.0.x, want.0.y));
                    let _ = w.set_size(PhysicalSize::new(want.0.w, want.0.h));
                }
            }
            if cfg!(target_os = "linux") {
                continue;
            }
            let Ok(p) = app.cursor_position() else { continue };
            let (region, scale) = *at.lock().unwrap();
            let origin = w.inner_position().map(|o| (o.x, o.y)).unwrap_or((region.x, region.y));
            let s = w.scale_factor().unwrap_or(scale);
            let (cx, cy) = ((p.x - origin.0 as f64) / s, (p.y - origin.1 as f64) / s);
            let over = rects.lock().unwrap().iter().any(|r| cx >= r.x && cx < r.x + r.w && cy >= r.y && cy < r.y + r.h);
            if over == ignoring {
                ignoring = !over;
                let _ = w.set_ignore_cursor_events(ignoring);
                // the page's mouseenter / mouseleave don't come when the window turns click-through (or back) under a
                // pointer that has stopped moving: say it from here too
                let _ = app.emit_to(Kind::Island.label(), ev::ISLAND, json!({"op": "hover", "x": cx, "y": cy, "out": !over}));
            }
            let mid = region.w as f64 / s / 2.0;
            let in_peek = (cx - mid).abs() < PEEK.0 / 2.0 && (0.0..PEEK.1).contains(&cy);
            match (in_peek, peek_since) {
                (true, None) => peek_since = Some(Instant::now()),
                (true, Some(t)) if t.elapsed() >= PEEK.2 => {
                    let _ = app.emit_to(Kind::Island.label(), ev::ISLAND, json!({"op": "peek"}));
                    peek_since = Some(Instant::now() + Duration::from_secs(3600)); // once per visit
                }
                (false, _) => peek_since = None,
                _ => {}
            }
        }
    });
}

impl<R: Runtime> Island<R> {
    pub fn window(&self) -> &WebviewWindow<R> {
        &self.window
    }

    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    /// Sends one op to the page; `state` is also kept for a page that (re)loads later.
    pub fn send(&self, op: Value) {
        if let Some(name) = op.get("op").and_then(Value::as_str).filter(|n| STICKY.contains(n)) {
            self.sticky.lock().unwrap().insert(name.to_string(), op.clone());
        }
        let _ = self.app.emit_to(Kind::Island.label(), ev::ISLAND, op);
    }

    /// Island actions: `{action, …}` as the page sent them (the label is checked already).
    pub fn on_action(&self, f: impl Fn(Value) + Send + 'static) {
        let id = listen_from(&self.app, ev::ACTION, Kind::Island.label(), f);
        self.listeners.lock().unwrap().push(id);
    }

    /// The island's area on the desktop (physical px).
    pub fn rect(&self) -> Region {
        self.at.lock().unwrap().0
    }
}

impl<R: Runtime> Drop for Island<R> {
    fn drop(&mut self) {
        for id in self.listeners.lock().unwrap().drain(..) {
            self.app.unlisten(id);
        }
        self.alive.store(false, Ordering::SeqCst);
        let _ = self.window.destroy();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_are_blurt_overlay_star() {
        assert!(Kind::Island.label().starts_with("blurt-overlay-"));
        assert_eq!(Kind::Island.page(), "island.html");
    }
}
