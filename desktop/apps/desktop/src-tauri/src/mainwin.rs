//! The main window's frame (size, position, maximized), kept in `$BLURT_HOME/ui/main-window.json` under `frame` and
//! put back at launch when it still lands on a screen; its title bar in the shell's own dark colour.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, PhysicalPosition, PhysicalSize, WebviewWindow};

use crate::paths;

fn state_file() -> PathBuf {
    paths::blurt_home().join("ui").join("main-window.json")
}

/// The saved frame, if any: {x, y, w, h, maximized}.
fn saved_frame() -> Option<Value> {
    frame_at(&state_file())
}

fn frame_at(p: &std::path::Path) -> Option<Value> {
    let v: Value = serde_json::from_str(&std::fs::read_to_string(p).ok()?).ok()?;
    v.get("frame").filter(|f| f["w"].as_f64().unwrap_or(0.0) >= 400.0 && f["h"].as_f64().unwrap_or(0.0) >= 300.0).cloned()
}

fn write_frame(frame: Value) {
    write_frame_at(&state_file(), frame);
}

fn write_frame_at(p: &std::path::Path, frame: Value) {
    let p = p.to_path_buf();
    let mut v: Value = std::fs::read_to_string(&p).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_else(|| json!({}));
    if !v.is_object() {
        v = json!({});
    }
    v["frame"] = frame;
    if let Some(d) = p.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let tmp = p.with_extension("json.tmp");
    if std::fs::write(&tmp, v.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, &p);
    }
}

/// The smallest the window may be (tauri.conf.json minWidth / minHeight, logical px): a programmatic set_size isn't
/// held to it.
const MIN: (f64, f64) = (960.0, 520.0);

/// A screen in physical px: (x, y, w, h) of its work area and its scale.
type Screen = ((i32, i32, u32, u32), f64);

fn screens(w: &WebviewWindow) -> Vec<Screen> {
    w.available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| {
            let a = m.work_area();
            ((a.position.x, a.position.y, a.size.width, a.size.height), m.scale_factor())
        })
        .collect()
}

/// Where a saved frame goes back: on the screen that holds its title bar (else the first screen, centred), its size
/// in that screen's pixels, at least the window's minimum, at most the work area, and all of it inside the work area.
/// Frames saved before 2026-11 hold physical px (no `scale`): read at the target screen's scale.
/// `chrome`: the window's frame around its page (title bar, borders; logical px) — the size saved and set is the page's,
/// the frame must fit on the screen too. Returns (x, y) of the frame and (w, h) of the page, physical, and whether it
/// fits (else the window is maximized).
fn place(f: &Value, screens: &[Screen], chrome: (f64, f64)) -> Option<((i32, i32, u32, u32), bool)> {
    let (x, y) = (f["x"].as_i64().unwrap_or(0) as i32, f["y"].as_i64().unwrap_or(0) as i32);
    let (fw, fh) = (f["w"].as_f64().unwrap_or(1280.0), f["h"].as_f64().unwrap_or(800.0));
    let logical = f["scale"].as_f64().is_some();
    let hit = |&&((sx, sy, sw, sh), s): &&Screen| {
        let width = if logical { fw * s } else { fw };
        let (cx, cy) = (x + (width / 2.0) as i32, y + (16.0 * s) as i32);
        cx >= sx && cx < sx + sw as i32 && cy >= sy && cy < sy + sh as i32
    };
    let (found, screen) = match screens.iter().find(hit) {
        Some(s) => (true, *s),
        None => (false, *screens.first()?),
    };
    let ((sx, sy, sw, sh), s) = screen;
    let (cw, ch) = ((chrome.0 * s).round(), (chrome.1 * s).round());
    let (room_w, room_h) = ((sw as f64 - cw).max(0.0), (sh as f64 - ch).max(0.0));
    let (mut w, mut h) = if logical { (fw * s, fh * s) } else { (fw, fh) };
    let (min_w, min_h) = (MIN.0 * s, MIN.1 * s);
    let fits = min_w <= room_w && min_h <= room_h;
    w = w.max(min_w).min(room_w);
    h = h.max(min_h).min(room_h);
    let (w, h) = (w.round() as u32, h.round() as u32);
    let (ow, oh) = (w + cw as u32, h + ch as u32);
    let (x, y) = if found {
        (x.clamp(sx, sx + sw.saturating_sub(ow) as i32), y.clamp(sy, sy + sh.saturating_sub(oh) as i32))
    } else {
        (sx + (sw.saturating_sub(ow) / 2) as i32, sy + (sh.saturating_sub(oh) / 2) as i32)
    };
    Some(((x, y, w, h), fits))
}

/// The window's frame around its page (logical px). Windows 11's frame includes the invisible resize borders, so a
/// window flush with the work area keeps its edges on it.
fn chrome(w: &WebviewWindow) -> (f64, f64) {
    let k = w.scale_factor().unwrap_or(1.0);
    match (w.outer_size(), w.inner_size()) {
        (Ok(o), Ok(i)) => (o.width.saturating_sub(i.width) as f64 / k, o.height.saturating_sub(i.height) as f64 / k),
        _ => (0.0, 0.0),
    }
}

/// Windows 11: the native title bar takes the shell's own dark surface (#1f1e1d, the rail and toolbar under it) instead
/// of the system's caption colour, so the bar and the window read as one piece. Older Windows ignores the attributes.
#[cfg(windows)]
fn tint_caption(w: &WebviewWindow) {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_USE_IMMERSIVE_DARK_MODE};
    let Ok(h) = w.hwnd() else { return };
    let hwnd = HWND(h.0 as _);
    let dark: u32 = 1;
    let caption: u32 = 0x001D_1E1F; // COLORREF is 0x00BBGGRR: #1F1E1D
    let text: u32 = 0x00E9_F1F3; // #F3F1E9
    let p = |v: &u32| v as *const u32 as *const core::ffi::c_void;
    unsafe {
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, p(&dark), 4);
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, p(&caption), 4);
        let _ = DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR, p(&text), 4);
    }
}
#[cfg(not(windows))]
fn tint_caption(_: &WebviewWindow) {}

/// At launch: the last frame back on the main window.
pub fn restore(app: &AppHandle) {
    let Some(w) = app.get_webview_window("main") else { return };
    tint_caption(&w);
    let Some(f) = saved_frame() else {
        clear_of_island(&w); // the first size, centred (tauri.conf.json): on a short screen its caption reaches the top
        return;
    };
    let Some(((x, y, width, height), fits)) = place(&f, &screens(&w), chrome(&w)) else { return };
    // position first: on another screen's scale Windows resizes the window as it arrives, then the size lands
    let _ = w.set_position(PhysicalPosition::new(x, y));
    let _ = w.set_size(PhysicalSize::new(width, height));
    if f["maximized"].as_bool() == Some(true) || !fits {
        let _ = w.maximize();
    } else {
        clear_of_island(&w);
    }
}

/// A frame saved with its caption under the island (the top edge of the screen, across its centre — round-2 gate:
/// island.launch, main.window, installer.after) comes back nudged down below the island's strip while it fits; one
/// that doesn't reach the screen's centre stays where it was.
fn clear_of_island(w: &WebviewWindow) {
    let (Ok(Some(m)), Ok(pos), Ok(size)) = (w.current_monitor(), w.outer_position(), w.outer_size()) else { return };
    let (a, k) = (m.work_area(), m.scale_factor());
    let mid = m.position().x + m.size().width as i32 / 2;
    let half = (160.0 * k) as i32; // the island's widest resting capsule and its shadow, about the centre
    let across = pos.x < mid + half && pos.x + size.width as i32 > mid - half;
    let top = a.position.y + (ISLAND_CLEAR * k) as i32;
    let room = a.position.y + a.size.height as i32 - size.height as i32;
    if across && pos.y < top && room > pos.y {
        let _ = w.set_position(PhysicalPosition::new(pos.x, top.min(room)));
    }
}

static GEN: AtomicU64 = AtomicU64::new(0);

/// The island's strip at the top centre of the screen (a 30 px capsule 6 px down, its bubble under it) in logical px:
/// a window keeps its title bar below it when the screen has the room, so the island never sits on its caption.
const ISLAND_CLEAR: f64 = 48.0;

/// The window moved or was resized: its frame saved a moment later (once a drag of its edge settles). A minimized
/// window keeps the last frame it had; a maximized one keeps its normal frame and says it was maximized.
pub fn changed(w: &tauri::Window) {
    if w.label() != "main" {
        return;
    }
    let g = GEN.fetch_add(1, Ordering::SeqCst) + 1;
    let app = w.app_handle().clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(600));
        if GEN.load(Ordering::SeqCst) != g {
            return;
        }
        let Some(w) = app.get_webview_window("main") else { return };
        if w.is_minimized().unwrap_or(false) || !w.is_visible().unwrap_or(false) {
            return;
        }
        let maximized = w.is_maximized().unwrap_or(false);
        let prev = saved_frame();
        let (pos, size) = (w.outer_position().ok(), w.inner_size().ok());
        let frame = match (maximized, prev, pos, size) {
            (true, Some(mut p), ..) => {
                p["maximized"] = json!(true);
                p
            }
            // the size in logical px with the scale it was saved at, so another scale (150 %, another screen) gets the
            // same window back rather than the same pixels
            (_, _, Some(p), Some(s)) => {
                let k = w.scale_factor().unwrap_or(1.0);
                let (lw, lh) = (s.width as f64 / k, s.height as f64 / k);
                json!({"x": p.x, "y": p.y, "w": lw.round(), "h": lh.round(), "scale": k, "maximized": maximized})
            }
            _ => return,
        };
        write_frame(frame);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_round_trip_keeps_other_keys() {
        let d = tempfile::tempdir().unwrap();
        let f = d.path().join("ui/main-window.json");
        std::fs::create_dir_all(d.path().join("ui")).unwrap();
        std::fs::write(&f, r#"{"scope":"inbox"}"#).unwrap();
        write_frame_at(&f, json!({"x": 10, "y": 20, "w": 1300, "h": 820, "maximized": false}));
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        assert_eq!(v["scope"], "inbox");
        assert_eq!(frame_at(&f).unwrap()["w"], 1300);
        write_frame_at(&f, json!({"x": 0, "y": 0, "w": 100, "h": 100}));
        assert!(frame_at(&f).is_none(), "a frame too small to use is ignored");
    }

    #[test]
    fn frames_come_back_inside_the_screen_and_above_the_minimum() {
        // 1920×1080 at 150 %: work area 1920×1032
        let s150 = [((0, 0, 1920, 1032), 1.5)];
        // saved at 100 % as 937×558 logical: back as 1440×837 physical (960×558 logical, the minimum width)
        let f = json!({"x": 100, "y": 50, "w": 937, "h": 558, "scale": 1.0});
        assert_eq!(place(&f, &s150, (0.0, 0.0)), Some(((100, 50, 1440, 837), true)));
        // a big frame from a 2560×1600 screen: no larger than the work area, pulled inside it
        let f = json!({"x": 300, "y": 200, "w": 2400, "h": 1500, "scale": 1.0});
        assert_eq!(place(&f, &s150, (0.0, 0.0)), Some(((0, 0, 1920, 1032), true)));
        // an old physical frame (no scale) larger than the screen
        let f = json!({"x": 0, "y": 0, "w": 1942, "h": 1106});
        assert_eq!(place(&f, &s150, (0.0, 0.0)), Some(((0, 0, 1920, 1032), true)));
        // off every screen (a monitor unplugged): centred on the first
        let f = json!({"x": 5000, "y": 0, "w": 1000, "h": 600, "scale": 1.0});
        assert_eq!(place(&f, &[((0, 0, 1920, 1040), 1.0)], (0.0, 0.0)), Some(((460, 220, 1000, 600), true)));
        // a screen smaller than the minimum: the window fills it and is maximized
        let f = json!({"x": 0, "y": 0, "w": 1280, "h": 800, "scale": 1.0});
        assert_eq!(place(&f, &[((0, 0, 1024, 728), 1.5)], (0.0, 0.0)), Some(((0, 0, 1024, 728), false)));
        // the frame counts: a page as big as it can be leaves room for the title bar and borders (Windows 11: 14 + 37
        // logical) — 1942×1064 on a 1920×1032 work area was the dogfood's
        let f = json!({"x": 150, "y": 120, "w": 1384, "h": 781, "scale": 1.0});
        assert_eq!(place(&f, &s150, (14.0, 37.0)), Some(((0, 0, 1899, 976), true)));
    }
}
