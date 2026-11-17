//! The tray icon and its menu — a way back into Bloom: 打开 Bloom · 设置 · 退出, a speech model's download while one
//! comes down, and while a meeting is recorded its time (greyed) and 打开 Bloom only (no Quit: it would end the take).
//! Windows conventions: a left click opens the window, the right click opens this menu, above the taskbar.
//!
//! The popup: Windows' tray code opens it at the pointer, bottom-aligned, so it sat over the taskbar and hid the
//! icon it belongs to (and the clock). Bloom opens it itself (popup_above_taskbar): above the taskbar's edge, centred
//! on the icon, the icon's rectangle excluded — where Windows 11's own tray flyouts stand.
//!
//! The menu is built once and kept. A change of words or the download's "x / y" is made in place on the live menu
//! (muda edits the HMENU Windows shows, which is safe); only a change of rows (idle ↔ recording, a download coming or
//! going) builds a new one, and never while the menu is on screen: Windows would go on showing a destroyed HMENU, an
//! empty 6-px popup. That change waits until the menu closes. Every refresh is worked out off the main thread and
//! applied on it, so a listener can never re-enter a lock its emitter holds.
//!
//! The icon: Bloom's mark in black or white for the taskbar's own theme (Windows' SystemUsesLightTheme, not the apps'
//! theme), followed live. While a take rolls it is the red record dot, and the time is in the tooltip.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Listener, Manager, Wry};

use crate::{l10n, show_main, AppState};

/// One row of the menu as it should be.
#[derive(Clone, Debug, PartialEq)]
enum Node {
    Item { id: String, text: String, enabled: bool },
    Sep,
    Sub { id: String, text: String, kids: Vec<Node> },
}

/// The rows' shape: what can't be changed in place.
fn shape(nodes: &[Node]) -> String {
    nodes
        .iter()
        .map(|n| match n {
            Node::Item { id, .. } => format!("i:{id}"),
            Node::Sep => "-".into(),
            Node::Sub { id, kids, .. } => format!("s:{id}[{}]", shape(kids)),
        })
        .collect::<Vec<_>>()
        .join(",")
}

enum Handle {
    Item(MenuItem<Wry>),
    Sub(Submenu<Wry>),
}

struct Live {
    shape: String,
    rows: Vec<Node>,
    handles: HashMap<String, Handle>,
    /// the menu on the icon (its HMENU is what popup_above_taskbar shows)
    #[cfg_attr(not(windows), allow(dead_code))]
    menu: Menu<Wry>,
}

#[derive(Default)]
pub struct TrayState {
    /// a speech model coming down: (done MB, total MB) from `model-download` events
    download: Mutex<Option<(f64, f64)>>,
    live: Mutex<Option<Live>>,
    /// a refresh is on its way (it reads the newest state when it runs)
    queued: AtomicBool,
    /// plans in the order they were worked out: an older one never lands over a newer one
    planned: AtomicU64,
    applied: AtomicU64,
    /// the icon now on the taskbar: (black for a light taskbar, the take's state, its size in px)
    look: Mutex<Option<(bool, Take, u32)>>,
}

/// Make the icon (once, at launch). Err: the desktop has no tray (GNOME without the AppIndicator extension).
pub fn build(app: &AppHandle) -> tauri::Result<()> {
    app.manage(TrayState::default());
    let rows = plan(app);
    let (menu, handles) = make(app, &rows)?;
    let black = icon_black(app);
    *app.state::<TrayState>().look.lock().unwrap() = Some((black, Take::Idle, tray_px()));
    let mut b = TrayIconBuilder::with_id("main").menu(&menu).tooltip(l10n::t("tray.tooltip")).show_menu_on_left_click(false);
    match tauri::image::Image::from_bytes(icon_png(black, Take::Idle)) {
        Ok(icon) => b = b.icon(icon).icon_as_template(cfg!(target_os = "macos")),
        Err(_) => {
            if let Some(icon) = app.default_window_icon() {
                b = b.icon(icon.clone());
            }
        }
    }
    b.on_menu_event(|app, ev| chose(app, ev.id().as_ref()))
        .on_tray_icon_event(|tray, ev| match ev {
            TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } => show_main(tray.app_handle()),
            #[cfg(windows)]
            TrayIconEvent::Click { button: MouseButton::Right, button_state: MouseButtonState::Up, rect, .. } => popup_above_taskbar(tray.app_handle(), rect),
            // the pointer arriving: counts and keys brought up to date (in place; a new shape waits for a closed menu)
            TrayIconEvent::Enter { .. } => refresh_soon(tray.app_handle()),
            _ => {}
        })
        .build(app)?;
    // the right click's menu is opened here, above the taskbar (tray-icon's own opens it over the taskbar)
    #[cfg(windows)]
    let _ = app.run_on_main_thread(win_popup::take_right_click);
    *app.state::<TrayState>().live.lock().unwrap() = Some(Live { shape: shape(&rows), rows, handles, menu });
    let h = app.clone();
    app.listen_any("model-download", move |e| {
        let v: Value = serde_json::from_str(e.payload()).unwrap_or_default();
        let st = h.state::<TrayState>();
        *st.download.lock().unwrap() = (v["state"] == "running").then(|| (v["done_mb"].as_f64().unwrap_or(0.0), v["total_mb"].as_f64().unwrap_or(0.0)));
        refresh_soon(&h);
    });
    watch_taskbar(app);
    Ok(())
}

/// Bring the menu up to date: soon, from another thread, so it is safe from any listener or under any lock.
pub fn refresh(app: &AppHandle) {
    schedule(app, Duration::from_millis(30));
}

/// The same, gathering a burst of events (index rows, download lines) into one update.
fn refresh_soon(app: &AppHandle) {
    schedule(app, Duration::from_millis(150));
}

/// Trailing edge: while one is queued, later calls ride on it, since it reads the state when it runs.
fn schedule(app: &AppHandle, delay: Duration) {
    let Some(st) = app.try_state::<TrayState>() else { return };
    if st.queued.swap(true, Ordering::SeqCst) {
        return;
    }
    let h = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(delay);
        let st = h.state::<TrayState>();
        st.queued.store(false, Ordering::SeqCst);
        let n = st.planned.fetch_add(1, Ordering::SeqCst) + 1;
        let rows = plan(&h);
        let h2 = h.clone();
        let _ = h.run_on_main_thread(move || apply(&h2, n, rows));
    });
}

/// On the main thread: the plan onto the live menu.
fn apply(app: &AppHandle, n: u64, rows: Vec<Node>) {
    let st = app.state::<TrayState>();
    if st.applied.load(Ordering::SeqCst) > n {
        return; // a newer plan already landed
    }
    let Some(tray) = app.tray_by_id("main") else { return };
    let mut live = st.live.lock().unwrap();
    let same = live.as_ref().is_some_and(|l| l.shape == shape(&rows));
    if same {
        let l = live.as_mut().unwrap();
        if l.rows != rows {
            update(&l.handles, &l.rows, &rows);
            l.rows = rows;
        }
    } else if menu_on_screen() {
        // a new shape can't replace the menu Windows is showing: again once it has closed
        drop(live);
        schedule(app, Duration::from_millis(500));
        return;
    } else if let Ok((menu, handles)) = make(app, &rows) {
        let _ = tray.set_menu(Some(menu.clone()));
        *live = Some(Live { shape: shape(&rows), rows, handles, menu });
    }
    st.applied.fetch_max(n, Ordering::SeqCst);
    drop(live);
    retint(app); // a take started or ended (on_recording refreshes): the icon too
}

/// Words, keys, ticks and enabled states changed in place (same shape).
fn update(handles: &HashMap<String, Handle>, old: &[Node], new: &[Node]) {
    for (o, n) in old.iter().zip(new) {
        if o == n {
            continue;
        }
        match (n, o) {
            (Node::Item { id, text, enabled }, Node::Item { text: t0, enabled: e0, .. }) => {
                if let Some(Handle::Item(m)) = handles.get(id) {
                    if text != t0 {
                        let _ = m.set_text(text);
                    }
                    if enabled != e0 {
                        let _ = m.set_enabled(*enabled);
                    }
                }
            }
            (Node::Sub { id, text, kids }, Node::Sub { text: t0, kids: k0, .. }) => {
                if let Some(Handle::Sub(m)) = handles.get(id) {
                    if text != t0 {
                        let _ = m.set_text(text);
                    }
                }
                update(handles, k0, kids);
            }
            _ => {}
        }
    }
}

fn make(app: &AppHandle, rows: &[Node]) -> tauri::Result<(Menu<Wry>, HashMap<String, Handle>)> {
    let mut handles = HashMap::new();
    let items = make_rows(app, rows, &mut handles)?;
    let refs: Vec<&dyn IsMenuItem<Wry>> = items.iter().map(|b| b.as_ref()).collect();
    Ok((Menu::with_items(app, &refs)?, handles))
}

fn make_rows(app: &AppHandle, rows: &[Node], handles: &mut HashMap<String, Handle>) -> tauri::Result<Vec<Box<dyn IsMenuItem<Wry>>>> {
    let mut out: Vec<Box<dyn IsMenuItem<Wry>>> = Vec::new();
    for n in rows {
        match n {
            Node::Item { id, text, enabled } => {
                let m = MenuItem::with_id(app, id, text, *enabled, None::<&str>)?;
                handles.insert(id.clone(), Handle::Item(m.clone()));
                out.push(Box::new(m));
            }
            Node::Sep => out.push(Box::new(PredefinedMenuItem::separator(app)?)),
            Node::Sub { id, text, kids } => {
                let k = make_rows(app, kids, handles)?;
                let refs: Vec<&dyn IsMenuItem<Wry>> = k.iter().map(|b| b.as_ref()).collect();
                let m = Submenu::with_id_and_items(app, id, text, true, &refs)?;
                handles.insert(id.clone(), Handle::Sub(m.clone()));
                out.push(Box::new(m));
            }
        }
    }
    Ok(out)
}

/// Whether a menu of this (the main) thread is on screen now.
#[cfg(windows)]
fn menu_on_screen() -> bool {
    use windows::Win32::System::Threading::GetCurrentThreadId;
    use windows::Win32::UI::WindowsAndMessaging::{GetGUIThreadInfo, GUITHREADINFO, GUI_INMENUMODE, GUI_POPUPMENUMODE};
    let mut info = GUITHREADINFO { cbSize: std::mem::size_of::<GUITHREADINFO>() as u32, ..Default::default() };
    unsafe { GetGUIThreadInfo(GetCurrentThreadId(), &mut info) }.is_ok() && (info.flags.0 & (GUI_INMENUMODE.0 | GUI_POPUPMENUMODE.0)) != 0
}

#[cfg(not(windows))]
fn menu_on_screen() -> bool {
    false
}

// ------------------------------------------------------------------ the popup

/// The right click's menu, opened by Blurt above the taskbar: its bottom on the taskbar's top edge (its top edge for a
/// taskbar at the top, its side for one at the left / right), centred on the icon, with the icon's rectangle excluded
/// so it never covers it — where Windows 11's tray flyouts stand. tray-icon's own popup (at the pointer, bottom-
/// aligned) stood on the taskbar and hid Blurt's own icon and the clock (round-1 gate, tray.menu-recording).
#[cfg(windows)]
fn popup_above_taskbar(app: &AppHandle, rect: tauri::Rect) {
    let Some(st) = app.try_state::<TrayState>() else { return };
    let Some(menu) = st.live.lock().unwrap().as_ref().map(|l| l.menu.clone()) else { return };
    // counts and keys up to date before it shows (the Enter refresh may still be on its way)
    let Ok(hmenu) = tauri::menu::ContextMenu::hpopupmenu(&menu) else { return };
    let p = rect.position.to_physical::<i32>(1.0);
    let z = rect.size.to_physical::<i32>(1.0);
    win_popup::show(hmenu, (p.x, p.y, p.x + z.width, p.y + z.height));
}

#[cfg(windows)]
mod win_popup {
    use windows::core::BOOL;
    use windows::Win32::Foundation::{HWND, LPARAM, POINT, RECT, WPARAM};
    use windows::Win32::Graphics::Gdi::{GetMonitorInfoW, MonitorFromPoint, MONITORINFO, MONITOR_DEFAULTTONEAREST};
    use windows::Win32::System::Threading::GetCurrentThreadId;
    use windows::Win32::UI::WindowsAndMessaging::{
        EnumThreadWindows, GetClassNameW, PostMessageW, SendMessageW, SetForegroundWindow, SetMenuInfo, TrackPopupMenuEx,
        HMENU, MENUINFO, MIM_STYLE, MNS_NOCHECK, TPMPARAMS, TPM_BOTTOMALIGN, TPM_CENTERALIGN, TPM_LEFTALIGN, TPM_RIGHTALIGN,
        TPM_RIGHTBUTTON, TPM_TOPALIGN, TPM_VERTICAL, TRACK_POPUP_MENU_FLAGS, WM_NULL,
    };

    /// tray-icon's window (class "tray_icon_app") on this thread: it owns the menu (muda's subclass on it turns a pick
    /// into the menu event), so the popup is opened for it.
    fn tray_hwnd() -> Option<HWND> {
        unsafe extern "system" fn each(h: HWND, l: LPARAM) -> BOOL {
            let mut buf = [0u16; 64];
            let n = unsafe { GetClassNameW(h, &mut buf) } as usize;
            if String::from_utf16_lossy(&buf[..n]) == "tray_icon_app" {
                unsafe { *(l.0 as *mut isize) = h.0 as isize };
                return false.into();
            }
            true.into()
        }
        let mut found: isize = 0;
        unsafe {
            let _ = EnumThreadWindows(GetCurrentThreadId(), Some(each), LPARAM(&mut found as *mut isize as isize));
        }
        (found != 0).then(|| HWND(found as *mut _))
    }

    /// tray-icon opens the menu on a right click unless told not to (its WM_USER_SHOW_MENU_ON_RIGHT_CLICK = 6009):
    /// Blurt opens it itself.
    pub fn take_right_click() {
        if let Some(h) = tray_hwnd() {
            unsafe { SendMessageW(h, 6009, Some(WPARAM(0)), Some(LPARAM(0))) };
        }
    }

    /// No row carries a check: the menu drops the check column (MNS_NOCHECK), so its labels start at the normal inset
    /// rather than after an empty gutter.
    pub fn show(hmenu: isize, icon: (i32, i32, i32, i32)) {
        let Some(owner) = tray_hwnd() else { return };
        let h = HMENU(hmenu as *mut _);
        unsafe {
            let info = MENUINFO { cbSize: std::mem::size_of::<MENUINFO>() as u32, fMask: MIM_STYLE, dwStyle: MNS_NOCHECK, ..Default::default() };
            let _ = SetMenuInfo(h, &info);
        }
        let (l, t, r, b) = icon;
        let centre = POINT { x: (l + r) / 2, y: (t + b) / 2 };
        let mut mi = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
        let mon = unsafe { MonitorFromPoint(centre, MONITOR_DEFAULTTONEAREST) };
        let (work, full) = if unsafe { GetMonitorInfoW(mon, &mut mi) }.as_bool() { (mi.rcWork, mi.rcMonitor) } else { (RECT::default(), RECT::default()) };
        const GAP: i32 = 8;
        // which edge the taskbar is on: where the work area stops short of the monitor
        let (x, y, flags): (i32, i32, TRACK_POPUP_MENU_FLAGS) = if work.top > full.top {
            (centre.x, work.top.max(b) + GAP, TPM_TOPALIGN | TPM_CENTERALIGN)
        } else if work.left > full.left {
            (work.left.max(r) + GAP, centre.y, TPM_LEFTALIGN | TPM_BOTTOMALIGN)
        } else if work.right < full.right {
            (work.right.min(l) - GAP, centre.y, TPM_RIGHTALIGN | TPM_BOTTOMALIGN)
        } else {
            // bottom (or an auto-hidden taskbar: the icon's own top edge)
            let edge = if work.bottom < full.bottom { work.bottom.min(t) } else { t };
            (centre.x, edge - GAP, TPM_BOTTOMALIGN | TPM_CENTERALIGN)
        };
        let params = TPMPARAMS { cbSize: std::mem::size_of::<TPMPARAMS>() as u32, rcExclude: RECT { left: l, top: t, right: r, bottom: b } };
        unsafe {
            // the shell's rule for a notification-area menu: its owner in front, so a click elsewhere closes it, and a
            // benign message after it so the task switch completes
            let _ = SetForegroundWindow(owner);
            let _ = TrackPopupMenuEx(h, (flags | TPM_RIGHTBUTTON | TPM_VERTICAL).0, x, y, owner, Some(&params));
            let _ = PostMessageW(Some(owner), WM_NULL, WPARAM(0), LPARAM(0));
        }
    }
}

// ------------------------------------------------------------------ the icon

/// What the icon shows besides the mark: a take rolling.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Take {
    Idle,
    Rolling,
}

/// The tray pictures (the mark fitted to each real size; the rec glyph in the token colours for a light or a dark
/// taskbar). Windows: the one drawn for the
/// notification area's size now (16 / 20 / 24 / 32 px at 100 / 125 / 150 / 200 %), never a 64 px picture it shrinks;
/// elsewhere the 64 px ones.
fn icon_png(black: bool, take: Take) -> &'static [u8] {
    macro_rules! sized {
        ($stem:literal) => {
            match tray_px() {
                16 => &include_bytes!(concat!("../icons/", $stem, "-16.png"))[..],
                20 => &include_bytes!(concat!("../icons/", $stem, "-20.png"))[..],
                24 => &include_bytes!(concat!("../icons/", $stem, "-24.png"))[..],
                32 => &include_bytes!(concat!("../icons/", $stem, "-32.png"))[..],
                _ => &include_bytes!(concat!("../icons/", $stem, ".png"))[..],
            }
        };
    }
    match (take, black) {
        (Take::Idle, true) => sized!("tray-black"),
        (Take::Idle, false) => sized!("tray-white"),
        (Take::Rolling, true) => sized!("tray-rec-light"),
        (Take::Rolling, false) => sized!("tray-rec-dark"),
    }
}

/// The notification area's icon size now (SM_CXSMICON at the system DPI), as one of the sizes drawn for it.
#[cfg(windows)]
fn tray_px() -> u32 {
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSMICON};
    // a DPI-aware process gets the system DPI's size: what the notification area draws at
    let px = unsafe { GetSystemMetrics(SM_CXSMICON) }.max(16) as u32;
    match px {
        0..=17 => 16,
        18..=21 => 20,
        22..=27 => 24,
        _ => 32,
    }
}

#[cfg(not(windows))]
fn tray_px() -> u32 {
    64
}

/// The take's state and its time so far (s).
fn take(app: &AppHandle) -> (Take, f64) {
    let Some(st) = app.try_state::<AppState>() else { return (Take::Idle, 0.0) };
    let cap = st.capture.state();
    let t = if cap["recording"] == true { Take::Rolling } else { Take::Idle };
    (t, cap["elapsed"].as_f64().unwrap_or(0.0))
}

/// Black Peep for a light taskbar, white for a dark one. The Mac's is a template image (black, tinted by the system);
/// Linux panels are dark by default.
fn icon_black(app: &AppHandle) -> bool {
    if cfg!(target_os = "macos") {
        return true;
    }
    if cfg!(target_os = "linux") {
        return false;
    }
    taskbar_light().unwrap_or_else(|| app.get_webview_window("main").and_then(|w| w.theme().ok()) == Some(tauri::Theme::Light))
}

/// HKCU\…\Themes\Personalize\SystemUsesLightTheme: the taskbar's theme (AppsUseLightTheme is the apps').
#[cfg(windows)]
fn taskbar_light() -> Option<bool> {
    use windows::core::w;
    use windows::Win32::System::Registry::{RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_DWORD};
    let mut v: u32 = 0;
    let mut n = std::mem::size_of::<u32>() as u32;
    let r = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            w!("Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"),
            w!("SystemUsesLightTheme"),
            RRF_RT_REG_DWORD,
            None,
            Some(&mut v as *mut u32 as *mut _),
            Some(&mut n),
        )
    };
    r.is_ok().then_some(v != 0)
}

#[cfg(not(windows))]
fn taskbar_light() -> Option<bool> {
    None
}

/// The icon and tooltip brought up to date: the taskbar's theme (the main window's ThemeChanged, the poll), the take
/// (on_recording's refresh; once a second while it rolls, for the time) and the scale (a DPI change: its own size).
pub fn retint(app: &AppHandle) {
    let Some(st) = app.try_state::<TrayState>() else { return };
    let Some(tray) = app.tray_by_id("main") else { return };
    let (t, secs) = take(app);
    let look = (icon_black(app), t, tray_px());
    let was = st.look.lock().unwrap().replace(look);
    if was != Some(look) {
        if let Ok(icon) = tauri::image::Image::from_bytes(icon_png(look.0, t)) {
            let _ = tray.set_icon(Some(icon));
        }
    }
    match t {
        Take::Idle if was.is_some_and(|w| w.1 != Take::Idle) => crate::island::update_tray(app), // "Bloom" again
        Take::Idle => {}
        _ => {
            let s = secs.max(0.0) as u64;
            let time = format!("{}:{:02}", s / 60, s % 60);
            let _ = tray.set_tooltip(Some(l10n::tf("tray.tooltip.recording", &[("time", time)])));
        }
    }
}

/// A taskbar-only switch (Personalisation → Colours → Custom) sends the app no theme change: one registry read every
/// few seconds catches it. While a take rolls: every second (its time in the tooltip).
fn watch_taskbar(app: &AppHandle) {
    let h = app.clone();
    std::thread::spawn(move || loop {
        let rolling = h.try_state::<TrayState>().and_then(|s| *s.look.lock().unwrap()).is_some_and(|l| l.1 != Take::Idle);
        std::thread::sleep(Duration::from_secs(if rolling { 1 } else { 3 }));
        let look = (icon_black(&h), take(&h).0, tray_px());
        let was = h.try_state::<TrayState>().and_then(|s| *s.look.lock().unwrap());
        if was != Some(look) || look.1 != Take::Idle {
            // a take started or ended; while it rolls, its status row's time
            if was.is_some_and(|w| w.1 != look.1) || look.1 != Take::Idle {
                refresh_soon(&h);
            }
            let h2 = h.clone();
            let _ = h.run_on_main_thread(move || retint(&h2));
        }
    });
}

// ------------------------------------------------------------------ the rows

/// "164 / 716 MB", "0.4 / 1.6 GB": both in the total's unit (the done figure never flips MB → GB mid-way)
fn size_pair(done: f64, total: f64) -> (String, String) {
    if total >= 1000.0 {
        (format!("{:.1}", done / 1024.0), format!("{:.1} GB", total / 1024.0))
    } else {
        (format!("{}", done.round() as i64), format!("{} MB", total.round() as i64))
    }
}

/// The menu as it should be now (worked out off the main thread).
fn plan(app: &AppHandle) -> Vec<Node> {
    let cap = app.state::<AppState>().capture.state();
    let item = |id: &str, text: String| Node::Item { id: id.into(), text, enabled: true };
    let mut rows: Vec<Node> = Vec::new();
    if cap["recording"] == true {
        // a meeting is recorded: a greyed line with its time (the tooltip that carries it can't show while the menu
        // is open), updated in place each second; the take ends from the meeting page or the island (散会)
        let s = cap["elapsed"].as_f64().unwrap_or(0.0).max(0.0) as u64;
        let time = format!("{}:{:02}", s / 60, s % 60);
        rows.push(Node::Item { id: "take_status".into(), text: l10n::tf("tray.tooltip.recording", &[("time", time)]), enabled: false });
        rows.push(Node::Sep);
        rows.push(item("open", l10n::t("tray.open")));
        return rows;
    }
    // a speech model coming down: a short title that keeps the menu its idle width ("Speech model · 164 / 716 MB"),
    // both figures in the total's unit so the row doesn't change width as the bytes tick; the submenu says what is
    // going on and names what Show and Stop act on
    if let Some((done, total)) = *app.state::<TrayState>().download.lock().unwrap() {
        let title = if total > 0.0 {
            let (d, t) = size_pair(done, total);
            l10n::tf("tray.models.row", &[("done", d), ("total", t)])
        } else {
            l10n::t("tray.models.downloading_start")
        };
        let pct = if total > 0.0 { ((done / total) * 100.0).floor().min(99.0) as i64 } else { 0 };
        let kids = vec![
            Node::Item { id: "models_status".into(), text: l10n::tf("tray.models.status", &[("pct", pct.to_string())]), enabled: false },
            Node::Sep,
            item("models", l10n::t("tray.models.show_download")),
            item("models_stop", l10n::t("tray.models.stop_download")),
        ];
        rows.push(Node::Sub { id: "sub:download".into(), text: title, kids });
        rows.push(Node::Sep);
    }
    rows.push(item("open", l10n::t("tray.open")));
    rows.push(Node::Sep);
    rows.push(item("settings", l10n::t("tray.settings")));
    rows.push(item("quit", l10n::t("tray.quit")));
    rows
}

/// The main window at a place: {"view": "settings", "pane": "speech"}
fn open_at(app: &AppHandle, nav: Value) {
    show_main(app);
    let _ = app.emit("nav", nav);
}

fn chose(app: &AppHandle, id: &str) {
    match id {
        "quit" => app.exit(0),
        "open" => show_main(app),
        "settings" => open_at(app, json!({"view": "settings"})),
        "models" => open_at(app, json!({"view": "settings", "pane": "speech"})),
        "models_stop" => {
            crate::models::model_download_cancel();
        }
        _ => {}
    }
}
