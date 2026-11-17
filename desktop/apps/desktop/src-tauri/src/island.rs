//! The island (contract K7): a top-centre floating pill, the same component as the macOS notch island. In Bloom it is
//! the meeting's: at rest its idle panel (the paper, 开会, 复制链接), during a meeting the take's pill (散会 under the
//! pointer), after it the post-meeting bar — and only while one of Bloom's windows is in front.
//!
//! The window and its click-through live in `blurt_capture::overlay::island`; this module feeds it (the `state` op)
//! and acts on what the user does there (web/apps/overlays/src/island.ts).

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use blurt_capture::overlay::{self, Island};
use serde_json::{json, Value};
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::DialogExt;

use crate::{capture, l10n, AppState};

#[derive(Default)]
pub struct Hub {
    island: Mutex<Option<Arc<Island<Wry>>>>,
}

fn island(app: &AppHandle) -> Option<Arc<Island<Wry>>> {
    app.state::<Hub>().island.lock().unwrap().clone().filter(|i| i.alive())
}

/// Opens the island (from a worker thread: overlay windows must not be created on the main thread).
pub fn init(app: &AppHandle) {
    if std::env::var_os("BLURT_NO_ISLAND").is_some() {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || match overlay::island(&app, &capture::overlay_config()) {
        Ok(i) => {
            let a2 = app.clone();
            i.on_action(move |v| on_action(&a2, &v));
            *app.state::<Hub>().island.lock().unwrap() = Some(i);
            refresh(&app);
            // the island's own box (CSS px, its corner radius), as the page reports where it takes the mouse
            overlay::listen_from(&app, overlay::ev::HIT_RECTS, overlay::Kind::Island.label(), |v| {
                let b = &v["island"];
                *ISLAND_BOX.lock().unwrap() = b["w"].as_f64().map(|w| {
                    (b["x"].as_f64().unwrap_or(0.0), b["y"].as_f64().unwrap_or(0.0), w, b["h"].as_f64().unwrap_or(0.0), b["r"].as_f64().unwrap_or(0.0))
                });
            });
            watch_front(&app);
        }
        Err(e) => eprintln!("blurt: no island window: {e}"),
    });
}

/// Bloom: a window of ours (the main window, the island, any overlay) is the foreground window. Bloom's own island
/// (its idle panel, a meeting's pill, the post-meeting bar) shows only then (island.ts `frontOnly`).
static FRONT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);

#[cfg(windows)]
fn ours_in_front() -> Option<bool> {
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId};
    unsafe {
        let fg = GetForegroundWindow();
        if fg.0.is_null() {
            return None; // between two windows (Alt+Tab, a closing one): no change
        }
        let mut pid = 0u32;
        GetWindowThreadProcessId(fg, Some(&mut pid));
        Some(pid == std::process::id())
    }
}

/// Every 250 ms: which process's window is in front; when that flips between ours and another app's, the island hears
/// it at once (`front`). Clicking the island or another overlay of ours keeps it "ours" — no flicker. Every 125 ms:
/// the meeting backend asking for the island's picture (island_shot).
fn watch_front(app: &AppHandle) {
    #[cfg(windows)]
    {
        let app = app.clone();
        std::thread::spawn(move || {
            let mut tick = 0u32;
            loop {
                if tick % 2 == 0 {
                    if let Some(on) = ours_in_front() {
                        if FRONT.swap(on, std::sync::atomic::Ordering::Relaxed) != on {
                            send(&app, json!({"op": "front", "on": on}));
                        }
                    }
                }
                tick = tick.wrapping_add(1);
                if let Some(out) = shot_request() {
                    let app = app.clone();
                    std::thread::spawn(move || island_shot(&app, &out));
                }
                std::thread::sleep(Duration::from_millis(125));
            }
        });
    }
    #[cfg(not(windows))]
    let _ = app;
}

/// The island's box as the page last reported it: CSS px x, y, w, h and its corner radius (None: nothing showing).
static ISLAND_BOX: Mutex<Option<(f64, f64, f64, f64, f64)>> = Mutex::new(None);

/// Bloom's meeting backend asks for the island's picture (ears.screenshot: "截图" puts the island in the meeting
/// window's picture, where it is on screen): `~/.zuhui/run/island-shot.req` = {"out": "<…>.png"}. Taken: the request.
fn shot_request() -> Option<PathBuf> {
    let home = std::env::var_os("ZUHUI_HOME").map(PathBuf::from).or_else(|| {
        std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(|h| PathBuf::from(h).join(".zuhui"))
    })?;
    let req = home.join("run").join("island-shot.req");
    let raw = std::fs::read(&req).ok()?;
    let _ = std::fs::remove_file(&req);
    let v: Value = serde_json::from_slice(&raw).ok()?;
    let out = PathBuf::from(v["out"].as_str()?);
    (out.extension().is_some_and(|e| e.eq_ignore_ascii_case("png")) && out.parent().is_some_and(|d| d.is_dir())).then_some(out)
}

/// The island as it looks now, into `out` (PNG, its rounded box only — the corners outside it transparent), then
/// `out`.json {shown, x, y, w, h} in desktop px (written last: the backend waits for it). The island is kept out of
/// screen captures (WDA_EXCLUDEFROMCAPTURE); for this one grab the exclusion is lifted for ~0.1 s — a meeting records
/// only Bloom's own window, which the island never was part of.
#[cfg(windows)]
fn island_shot(app: &AppHandle, out: &std::path::Path) {
    let answer = |v: Value| {
        let j = out.with_extension("png.json");
        let tmp = out.with_extension("png.json.tmp");
        if std::fs::write(&tmp, v.to_string()).is_ok() {
            let _ = std::fs::rename(&tmp, &j);
        }
    };
    let (Some(i), Some((x, y, w, h, r))) = (island(app), *ISLAND_BOX.lock().unwrap()) else {
        return answer(json!({"shown": false}));
    };
    let win = i.window().clone();
    if !win.is_visible().unwrap_or(false) || w < 2.0 || h < 2.0 {
        return answer(json!({"shown": false}));
    }
    let s = win.scale_factor().unwrap_or(1.0);
    let o = win.inner_position().map(|p| (p.x, p.y)).unwrap_or((i.rect().x, i.rect().y));
    let (px, py) = (o.0 + (x * s).round() as i32, o.1 + (y * s).round() as i32);
    let (pw, ph) = ((w * s).ceil() as u32, (h * s).ceil() as u32);
    let Some(hwnd) = win.hwnd().ok().map(|h| h.0 as usize) else { return answer(json!({"shown": false})) };
    let affinity = |a: u32| {
        let (tx, rx) = std::sync::mpsc::channel();
        let ok = app
            .run_on_main_thread(move || {
                use windows::Win32::Foundation::HWND;
                use windows::Win32::UI::WindowsAndMessaging::{SetWindowDisplayAffinity, WINDOW_DISPLAY_AFFINITY};
                let r = unsafe { SetWindowDisplayAffinity(HWND(hwnd as *mut _), WINDOW_DISPLAY_AFFINITY(a)) };
                let _ = tx.send(r.is_ok());
            })
            .is_ok();
        ok && rx.recv_timeout(Duration::from_secs(2)).unwrap_or(false)
    };
    const WDA_NONE: u32 = 0;
    const WDA_EXCLUDEFROMCAPTURE: u32 = 0x11;
    if !affinity(WDA_NONE) {
        return answer(json!({"shown": false, "error": "affinity"}));
    }
    std::thread::sleep(Duration::from_millis(90)); // the compositor's next frame shows it to captures
    let g = grab(px, py, pw, ph);
    affinity(WDA_EXCLUDEFROMCAPTURE);
    let Ok(g) = g else { return answer(json!({"shown": false, "error": "grab"})) };
    // BGRA → RGBA, the corners outside the rounded box transparent (what is behind them isn't the island)
    let rad = (r * s).min(g.h as f64 / 2.0).max(0.0);
    let mut rgba = Vec::with_capacity(g.bgra.len());
    for (k, px4) in g.bgra.chunks_exact(4).enumerate() {
        let (cx, cy) = ((k as u32 % g.w) as f64 + 0.5, (k as u32 / g.w) as f64 + 0.5);
        let dx = if cx < rad { rad - cx } else if cx > g.w as f64 - rad { cx - (g.w as f64 - rad) } else { 0.0 };
        let dy = if cy < rad { rad - cy } else if cy > g.h as f64 - rad { cy - (g.h as f64 - rad) } else { 0.0 };
        let a = if dx > 0.0 && dy > 0.0 { (rad + 0.5 - (dx * dx + dy * dy).sqrt()).clamp(0.0, 1.0) } else { 1.0 };
        rgba.extend_from_slice(&[px4[2], px4[1], px4[0], (a * 255.0) as u8]);
    }
    let written = (|| -> Result<(), String> {
        let f = std::fs::File::create(out).map_err(|e| e.to_string())?;
        let mut e = png::Encoder::new(std::io::BufWriter::new(f), g.w, g.h);
        e.set_color(png::ColorType::Rgba);
        e.set_depth(png::BitDepth::Eight);
        e.write_header().map_err(|e| e.to_string())?.write_image_data(&rgba).map_err(|e| e.to_string())
    })();
    match written {
        Ok(()) => answer(json!({"shown": true, "x": px, "y": py, "w": g.w, "h": g.h})),
        Err(e) => answer(json!({"shown": false, "error": e})),
    }
}

/// Bloom's meeting backend (`python -m zuhui serve`, 127.0.0.1:47330): one request, its status and JSON body. Plain
/// HTTP/1.1 with `Connection: close` over a TcpStream — the island's two meeting buttons need nothing more.
fn backend(method: &str, path: &str) -> Result<(u16, Value), String> {
    backend_at(47330, method, path)
}

fn backend_at(port: u16, method: &str, path: &str) -> Result<(u16, Value), String> {
    use std::io::{Read, Write};
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(2)).map_err(|e| e.to_string())?;
    let _ = s.set_read_timeout(Some(Duration::from_secs(15)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(5)));
    let body = if method == "POST" { "{}" } else { "" };
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:47330\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or("no response")?;
    let head = String::from_utf8_lossy(&raw[..split]).to_ascii_lowercase();
    let code = head.split_whitespace().nth(1).and_then(|c| c.parse::<u16>().ok()).ok_or("no status")?;
    let mut body = raw[split + 4..].to_vec();
    if head.contains("transfer-encoding: chunked") {
        body = dechunk(&body);
    }
    Ok((code, serde_json::from_slice(&body).unwrap_or(Value::Null)))
}

/// A chunked body as one piece.
fn dechunk(mut b: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    while let Some(eol) = b.windows(2).position(|w| w == b"\r\n") {
        let n = usize::from_str_radix(String::from_utf8_lossy(&b[..eol]).split(';').next().unwrap_or("").trim(), 16).unwrap_or(0);
        if n == 0 || b.len() < eol + 2 + n {
            break;
        }
        out.extend_from_slice(&b[eol + 2..eol + 2 + n]);
        b = &b[(eol + 4 + n).min(b.len())..];
    }
    out
}

/// The meeting's link alone (GET /api/invite → url): pasted into a browser's address bar it must just open — the
/// several-line invitation didn't.
fn invite_link() -> Option<String> {
    match backend("GET", "/api/invite") {
        Ok((200, v)) => v["url"].as_str().filter(|u| !u.is_empty()).map(str::to_owned),
        _ => None,
    }
}

/// A grabbed area of the screen (GDI, physical pixels): BGRA, top-down rows.
#[cfg(windows)]
struct Grab {
    w: u32,
    h: u32,
    bgra: Vec<u8>,
}

#[cfg(windows)]
fn grab(x: i32, y: i32, w: u32, h: u32) -> Result<Grab, String> {
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject, BITMAPINFO,
        BITMAPINFOHEADER, BI_RGB, CAPTUREBLT, DIB_RGB_COLORS, SRCCOPY,
    };
    unsafe {
        let screen = GetDC(None);
        let mem = CreateCompatibleDC(Some(screen));
        let mut bi = BITMAPINFO::default();
        bi.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w as i32,
            biHeight: -(h as i32), // top-down
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB.0,
            ..Default::default()
        };
        let mut bits: *mut std::ffi::c_void = std::ptr::null_mut();
        let bmp = CreateDIBSection(Some(mem), &bi, DIB_RGB_COLORS, &mut bits, None, 0)
            .map_err(|e| e.to_string())?;
        let old = SelectObject(mem, bmp.into());
        let ok = BitBlt(
            mem,
            0,
            0,
            w as i32,
            h as i32,
            Some(screen),
            x,
            y,
            SRCCOPY | CAPTUREBLT,
        )
        .is_ok();
        let n = (w * h * 4) as usize;
        let data = if ok && !bits.is_null() {
            std::slice::from_raw_parts(bits as *const u8, n).to_vec()
        } else {
            vec![]
        };
        SelectObject(mem, old);
        let _ = DeleteObject(bmp.into());
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
        if data.is_empty() {
            return Err("couldn't grab the screen".into());
        }
        Ok(Grab { w, h, bgra: data })
    }
}

pub fn send(app: &AppHandle, op: Value) {
    if let Some(i) = island(app) {
        i.send(op);
    }
}

/// A take starting: the island steps back (dimmed, no new lines over it; the ones that wait come after).
pub fn busy(app: &AppHandle, on: bool) {
    send(app, json!({"op": "busy", "on": on}));
}

/// The island's whole state (web/apps/overlays/src/island.ts `IslandState`).
pub fn state(app: &AppHandle) -> Value {
    let st = app.state::<AppState>();
    let cap = st.capture.state();
    let now = chrono::Utc::now().timestamp_millis() as f64 / 1000.0;
    // a meeting's post-processing bar; while its take is still being saved the bar replaces the recording pill
    let (post, finishing) = st.capture.post_state();
    json!({"op": "state", "state": {
        "recording": if cap["recording"] == true && !finishing {
            json!({"elapsed": cap["elapsed"], "paused": false, "shots": 0, "pen": false, "at": now, "window": cap["window"]})
        } else { Value::Null },
        "postMeeting": post,
        // one of our windows is in front (watch_front) — Bloom's own island shows only then
        "front": FRONT.load(std::sync::atomic::Ordering::Relaxed),
        // the paper being discussed, for the island's header (the meeting backend's current_paper.json)
        "paper": current_paper(),
        "lang": l10n::lang(),
        // the rest of island.ts's IslandState, empty: Bloom has no projects, captures, voice notes, dictation, jobs or keys
        "project": null, "projects": [], "toReview": 0, "voice": null, "dictation": null, "hatching": 0, "jobs": [],
        "hotkeys": {}, "cardPrefs": {},
    }})
}

/// Bloom: {name} of the paper the meeting backend has open (~/.zuhui/current_paper.json), or null.
fn current_paper() -> Value {
    let home = std::env::var_os("ZUHUI_HOME").map(std::path::PathBuf::from).or_else(|| {
        std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME")).map(|h| std::path::PathBuf::from(h).join(".zuhui"))
    });
    let v = home
        .and_then(|h| std::fs::read(h.join("current_paper.json")).ok())
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok());
    match v.as_ref().and_then(|v| v["name"].as_str()).map(str::trim).filter(|n| !n.is_empty()) {
        Some(n) => json!({"name": n}),
        None => Value::Null,
    }
}

pub fn refresh(app: &AppHandle) {
    if island(app).is_some() {
        send(app, state(app));
    }
    update_tray(app);
}

pub(crate) fn update_tray(app: &AppHandle) {
    // the tray just says "Bloom" (a take's time while it rolls: tray.rs)
    if let Some(t) = app.tray_by_id("main") {
        let _ = t.set_tooltip(Some(l10n::t("tray.tooltip")));
    }
}

/// What the user did in the island.
pub fn on_action(app: &AppHandle, v: &Value) {
    match v["action"].as_str().unwrap_or("") {
        // 开会 as the meeting page's button (only Bloom's window is recorded)
        "meeting.start" => capture::meeting(app),
        // 散会 as the meeting page does it: the backend ends the meeting at once, then the take stops (its file is
        // finished behind the post-meeting bar)
        "meeting.end" => {
            let app = app.clone();
            std::thread::spawn(move || {
                let ended = matches!(backend("POST", "/api/meeting/end"), Ok((200, _)));
                let _ = app.state::<AppState>().capture.stop();
                if !ended {
                    send(&app, json!({"op": "toast", "kind": "line", "msg": "island.meeting.end_failed", "mark": "warning"}));
                }
            });
        }
        // 复制链接: the meeting's link alone on the clipboard; "已复制链接" for 2 s (a click takes it away sooner)
        "invite.copy" => {
            let app = app.clone();
            std::thread::spawn(move || match invite_link() {
                Some(t) => {
                    let ok = app.clipboard().write_text(t).is_ok();
                    let (msg, mark, ms) = if ok { ("island.invite.copied", "check", 2000) } else { ("island.invite.failed", "warning", 5000) };
                    send(&app, json!({"op": "toast", "kind": "line", "msg": msg, "mark": mark, "ms": ms}));
                }
                None => send(&app, json!({"op": "toast", "kind": "line", "msg": "island.invite.failed", "mark": "warning"})),
            });
        }
        "stop" => {
            let _ = app.state::<AppState>().capture.stop();
        }
        "open.main" => crate::show_main(app),
        _ => {}
    }
}

/// The file picker, over the main window when it shows (centred on it, owned by it) — else on its own, where the
/// system puts it.
pub fn folder_dialog(app: &AppHandle) -> tauri_plugin_dialog::FileDialogBuilder<tauri::Wry> {
    let d = app.dialog().file();
    match app.get_webview_window("main").filter(|w| w.is_visible().unwrap_or(false) && !w.is_minimized().unwrap_or(false)) {
        Some(w) => d.set_parent(&w),
        None => d,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    /// One canned answer on a local port: what the request was, and what `backend_at` made of the answer.
    fn serve(answer: &'static [u8]) -> (u16, std::thread::JoinHandle<String>) {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let h = std::thread::spawn(move || {
            let (mut c, _) = l.accept().unwrap();
            let mut buf = [0u8; 2048];
            let n = c.read(&mut buf).unwrap();
            c.write_all(answer).unwrap();
            String::from_utf8_lossy(&buf[..n]).into_owned()
        });
        (port, h)
    }

    #[test]
    fn talks_to_the_meeting_backend() {
        // the backend's own shape: HTTP/1.0, Content-Length, the connection closed after the body
        let (port, h) = serve(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 30\r\n\r\n{\"ok\": true, \"text\": \"a\\nb c\"}");
        let (code, v) = backend_at(port, "GET", "/api/invite/text").unwrap();
        assert_eq!((code, v["text"].as_str()), (200, Some("a\nb c")));
        assert!(h.join().unwrap().starts_with("GET /api/invite/text HTTP/1.1\r\n"));
        let (port, h) = serve(b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nnot found");
        assert_eq!(backend_at(port, "POST", "/api/meeting/end").unwrap(), (404, Value::Null));
        let req = h.join().unwrap();
        assert!(req.starts_with("POST /api/meeting/end HTTP/1.1") && req.ends_with("\r\n\r\n{}"), "{req}");
        let (port, _h) = serve(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\n{\"ok\": \r\n5\r\ntrue}\r\n0\r\n\r\n");
        assert_eq!(backend_at(port, "GET", "/x").unwrap().1["ok"], true);
        assert!(backend_at(1, "GET", "/x").is_err(), "nothing listening");
    }
}
