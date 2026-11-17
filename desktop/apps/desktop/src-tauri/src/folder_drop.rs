//! A paper dropped from Explorer onto 会议 (components/MeetingCurrent.tsx). The main window keeps
//! `dragDropEnabled: false` (Tauri's own drop target would take every drag over the webview), so a page never learns a
//! dropped file's path. The page hands the dropped `File` objects over with
//! `chrome.webview.postMessageWithAdditionalObjects("zuhui-drop-paper", files)`, here WebView2 gives each one's path
//! (ICoreWebView2File), and the paths go back as `mainwin://paper-dropped` {paths}; the page posts the first one to
//! the 组会 backend.

#[cfg(windows)]
pub fn install(app: &tauri::AppHandle) {
    use tauri::{Emitter, Manager};
    use webview2_com::Microsoft::Web::WebView2::Win32::{ICoreWebView2File, ICoreWebView2WebMessageReceivedEventArgs2};
    use webview2_com::{take_pwstr, WebMessageReceivedEventHandler};
    use windows::core::{Interface, PWSTR};

    let Some(w) = app.get_webview_window("main") else { return };
    let app = app.clone();
    let _ = w.with_webview(move |wv| unsafe {
        let Ok(core) = wv.controller().CoreWebView2() else { return };
        let handler = WebMessageReceivedEventHandler::create(Box::new(move |_, args| {
            let Some(args) = args else { return Ok(()) };
            let mut msg = PWSTR::null();
            if args.TryGetWebMessageAsString(&mut msg).is_err() {
                return Ok(()); // the app's own IPC: wry's handler has it
            }
            if take_pwstr(msg) != "zuhui-drop-paper" {
                return Ok(());
            }
            let Ok(args2) = args.cast::<ICoreWebView2WebMessageReceivedEventArgs2>() else { return Ok(()) };
            let Ok(objs) = args2.AdditionalObjects() else { return Ok(()) };
            let mut n = 0u32;
            let _ = objs.Count(&mut n);
            let mut paths = Vec::new();
            for i in 0..n {
                let Ok(o) = objs.GetValueAtIndex(i) else { continue };
                let Ok(f) = o.cast::<ICoreWebView2File>() else { continue };
                let mut p = PWSTR::null();
                if f.Path(&mut p).is_ok() {
                    let p = take_pwstr(p);
                    if std::path::Path::new(&p).is_file() {
                        paths.push(p);
                    }
                }
            }
            let _ = app.emit_to("main", "mainwin://paper-dropped", serde_json::json!({"paths": paths}));
            Ok(())
        }));
        let mut token = Default::default();
        if let Err(e) = core.add_WebMessageReceived(&handler, &mut token) {
            eprintln!("blurt: paper drops unavailable: {e}");
        }
    });
}

#[cfg(not(windows))]
pub fn install(_app: &tauri::AppHandle) {}
