//! Settings → General → "Open at login": HKCU\…\Run on Windows, an XDG autostart entry on Linux. A login launch
//! carries `--autostart` and starts in the tray.

use serde_json::{json, Value};

type R<T> = Result<T, String>;

/// The argument a login launch carries (the Run value / the autostart entry).
pub const AUTOSTART_ARG: &str = "--autostart";

/// A launch from the login entry: Bloom starts in the tray.
pub fn launched_at_login() -> bool {
    std::env::args().any(|a| a == AUTOSTART_ARG)
}

/// The command a login runs: this exe, quoted, then the autostart flag (the Run value's data).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn run_value(exe: &str) -> String {
    format!("\"{exe}\" {AUTOSTART_ARG}")
}

/// One argument of a desktop entry's Exec (the spec's quoting: in double quotes, `"` `` ` `` `$` `\` escaped).
pub fn exec_quote(arg: &str) -> String {
    if !arg.is_empty() && arg.chars().all(|c| c.is_ascii_alphanumeric() || "/._-+=:,@".contains(c)) {
        return arg.to_string();
    }
    let mut s = String::from("\"");
    for c in arg.chars() {
        if matches!(c, '"' | '`' | '$' | '\\') {
            s.push('\\');
        }
        s.push(c);
    }
    s.push('"');
    s
}

/// The XDG autostart entry (Linux): the AppImage when running from one, else this binary.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn desktop_entry(exe: &str) -> String {
    format!(
        "[Desktop Entry]\nType=Application\nName=Blurt\nComment=Dictation, snaps and recordings that become work\nExec={} {AUTOSTART_ARG}\nIcon=blurt\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
        exec_quote(exe)
    )
}

fn exe_path() -> R<String> {
    if let Some(a) = std::env::var_os("APPIMAGE").filter(|_| cfg!(target_os = "linux")) {
        return Ok(a.to_string_lossy().into_owned());
    }
    std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).map_err(|e| e.to_string())
}

#[cfg(target_os = "linux")]
fn autostart_file() -> std::path::PathBuf {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".config"));
    base.join("autostart").join("blurt.desktop")
}

/// (supported, on)
fn login_state() -> (bool, bool) {
    #[cfg(windows)]
    {
        (true, win::login_enabled())
    }
    #[cfg(target_os = "linux")]
    {
        let on = std::fs::read_to_string(autostart_file())
            .is_ok_and(|s| !s.lines().any(|l| l.trim() == "Hidden=true" || l.trim() == "X-GNOME-Autostart-enabled=false"));
        (true, on)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        (false, false)
    }
}

fn set_login(on: bool) -> R<()> {
    // test runs never touch the real login items (the Mac's SystemAccess.testMode)
    if std::env::var_os("BLURT_TEST_EVAL").is_some() || std::env::var_os("BLURT_TEST_NO_LOGIN").is_some() {
        eprintln!("blurt: login item → {on} (test run: not registered)");
        return Ok(());
    }
    #[cfg(windows)]
    {
        win::set_login(on, &run_value(&exe_path()?))
    }
    #[cfg(target_os = "linux")]
    {
        let f = autostart_file();
        if on {
            if let Some(d) = f.parent() {
                std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
            }
            std::fs::write(&f, desktop_entry(&exe_path()?)).map_err(|e| e.to_string())
        } else {
            match std::fs::remove_file(&f) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.to_string()),
                _ => Ok(()),
            }
        }
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (on, exe_path);
        Err("not on this platform".into())
    }
}

fn login_json() -> Value {
    let (supported, enabled) = login_state();
    json!({"supported": supported, "enabled": enabled})
}

/// {supported, enabled}
#[tauri::command]
pub fn login_item_get() -> Value {
    login_json()
}

#[tauri::command]
pub fn login_item_set(on: bool) -> R<Value> {
    set_login(on)?;
    Ok(login_json())
}

#[cfg(windows)]
mod win {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
    use windows::Win32::System::Registry::{
        RegDeleteKeyValueW, RegGetValueW, RegSetKeyValueW, HKEY, HKEY_CURRENT_USER, REG_SZ, RRF_RT_REG_BINARY,
        RRF_RT_REG_SZ,
    };

    const RUN: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    /// Task Manager's Startup apps switch: a value whose first byte is odd (3) is turned off there
    const APPROVED: &str = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
    const NAME: &str = "Blurt";

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    pub fn get_string(root: HKEY, key: &str, name: &str) -> Option<String> {
        let (k, n) = (wide(key), wide(name));
        let mut size: u32 = 0;
        let r = unsafe {
            RegGetValueW(root, PCWSTR(k.as_ptr()), PCWSTR(n.as_ptr()), RRF_RT_REG_SZ, None, None, Some(&mut size as *mut u32))
        };
        if r != ERROR_SUCCESS || size == 0 {
            return None;
        }
        let mut buf = vec![0u16; (size as usize).div_ceil(2) + 1];
        let mut size = (buf.len() * 2) as u32;
        let r = unsafe {
            RegGetValueW(
                root,
                PCWSTR(k.as_ptr()),
                PCWSTR(n.as_ptr()),
                RRF_RT_REG_SZ,
                None,
                Some(buf.as_mut_ptr() as *mut core::ffi::c_void),
                Some(&mut size as *mut u32),
            )
        };
        if r != ERROR_SUCCESS {
            return None;
        }
        let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        Some(String::from_utf16_lossy(&buf[..end]))
    }

    fn first_byte(root: HKEY, key: &str, name: &str) -> Option<u8> {
        let (k, n) = (wide(key), wide(name));
        let mut buf = [0u8; 64];
        let mut size = buf.len() as u32;
        let r = unsafe {
            RegGetValueW(
                root,
                PCWSTR(k.as_ptr()),
                PCWSTR(n.as_ptr()),
                RRF_RT_REG_BINARY,
                None,
                Some(buf.as_mut_ptr() as *mut core::ffi::c_void),
                Some(&mut size as *mut u32),
            )
        };
        (r == ERROR_SUCCESS && size > 0).then_some(buf[0])
    }

    fn delete(key: &str, name: &str) -> Result<(), String> {
        let (k, n) = (wide(key), wide(name));
        let r = unsafe { RegDeleteKeyValueW(HKEY_CURRENT_USER, PCWSTR(k.as_ptr()), PCWSTR(n.as_ptr())) };
        if r == ERROR_SUCCESS || r == ERROR_FILE_NOT_FOUND {
            Ok(())
        } else {
            Err(format!("registry: error {}", r.0))
        }
    }

    pub fn login_enabled() -> bool {
        get_string(HKEY_CURRENT_USER, RUN, NAME).is_some()
            && !first_byte(HKEY_CURRENT_USER, APPROVED, NAME).is_some_and(|b| b & 1 == 1)
    }

    pub fn set_login(on: bool, command: &str) -> Result<(), String> {
        if !on {
            return delete(RUN, NAME);
        }
        let (k, n, data) = (wide(RUN), wide(NAME), wide(command));
        let r = unsafe {
            RegSetKeyValueW(
                HKEY_CURRENT_USER,
                PCWSTR(k.as_ptr()),
                PCWSTR(n.as_ptr()),
                REG_SZ.0,
                Some(data.as_ptr() as *const core::ffi::c_void),
                (data.len() * 2) as u32,
            )
        };
        if r != ERROR_SUCCESS {
            return Err(format!("registry: error {}", r.0));
        }
        // turned off in Task Manager before: on again
        delete(APPROVED, NAME)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn login_commands() {
        assert_eq!(run_value(r"C:\Users\Maya Li\AppData\Local\Blurt\blurt.exe"), r#""C:\Users\Maya Li\AppData\Local\Blurt\blurt.exe" --autostart"#);
        assert_eq!(exec_quote("/opt/blurt/blurt"), "/opt/blurt/blurt");
        assert_eq!(exec_quote("/home/maya/Apps/Blurt 0.7.AppImage"), "\"/home/maya/Apps/Blurt 0.7.AppImage\"");
        assert_eq!(exec_quote(r#"/a/b"c$d"#), r#""/a/b\"c\$d""#);
        let e = desktop_entry("/home/maya/Apps/Blurt.AppImage");
        assert!(e.starts_with("[Desktop Entry]\n"));
        assert!(e.contains("\nExec=/home/maya/Apps/Blurt.AppImage --autostart\n"));
    }
}
