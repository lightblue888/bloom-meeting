//! Where things live (contract K1 / K4), the same rules as `_common.py`: `$BLURT_HOME` (default `~/.blurt`) holds
//! `config.json`, `jobs/`, `models/` and `run/`; the takes live in the inbox store, `$BLURT_WORKSPACE/inbox/` (default
//! `~/Blurt/inbox/`).

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

pub fn home() -> PathBuf {
    // $HOME first so tests (and the Linux e2e) can relocate everything; dirs::home_dir is the platform answer.
    std::env::var_os("HOME")
        .filter(|h| !h.is_empty())
        .map(PathBuf::from)
        .or_else(dirs::home_dir)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn blurt_home() -> PathBuf {
    std::env::var_os("BLURT_HOME").filter(|h| !h.is_empty()).map(PathBuf::from).unwrap_or_else(|| home().join(".blurt"))
}

pub fn config_file() -> PathBuf {
    blurt_home().join("config.json")
}

pub fn jobs_dir() -> PathBuf {
    blurt_home().join("jobs")
}

pub fn default_workspace() -> PathBuf {
    std::env::var_os("BLURT_WORKSPACE")
        .filter(|h| !h.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join("Blurt"))
}

/// The inbox store (K1), where the takes go: `$BLURT_WORKSPACE/inbox`.
pub fn inbox_store() -> PathBuf {
    default_workspace().join("inbox")
}

/// Sessions recorded before v0.7 (Settings → Storage still counts them).
pub fn legacy_recordings() -> PathBuf {
    default_workspace().join("recordings")
}

fn first_existing(cands: impl IntoIterator<Item = PathBuf>) -> Option<PathBuf> {
    cands.into_iter().find(|p| p.exists())
}

/// The recording and speech scripts (record.py, live.py, transcribe.py): `$BLURT_SCRIPTS` → the copy bundled with the
/// app → the repo checkout (dev builds). Never an installed Blurt skill's copy: its record.py isn't Bloom's.
pub fn scripts_dir(app: &AppHandle) -> Option<PathBuf> {
    let mut c: Vec<PathBuf> = Vec::new();
    if let Some(p) = std::env::var_os("BLURT_SCRIPTS").filter(|p| !p.is_empty()) {
        c.push(PathBuf::from(p));
    }
    if let Ok(res) = app.path().resource_dir() {
        c.push(res.join("scripts"));
    }
    #[cfg(debug_assertions)] // the repo checkout: dev builds only (a release build carries no build-machine path)
    c.push(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../skills/blurt/scripts"));
    first_existing(c.into_iter().map(|p| p.join("record.py"))).and_then(|p| p.parent().map(normalize))
}

/// `uv` from PATH, else the copy setup.rs installs (`~/.blurt/bin/uv`), else the usual installer locations.
pub fn uv() -> Option<PathBuf> {
    let exe = if cfg!(windows) { "uv.exe" } else { "uv" };
    if let Some(path) = std::env::var_os("PATH") {
        for d in std::env::split_paths(&path) {
            let p = d.join(exe);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    let h = home();
    first_existing([
        blurt_home().join("bin").join(exe),
        h.join(".blurt/bin").join(exe),
        h.join(".local/bin").join(exe),
        h.join(".cargo/bin").join(exe),
    ])
}

/// Lexically clean `a/b/../c` (no symlink resolution: Windows `canonicalize` gives `\\?\` paths Python dislikes).
pub fn normalize(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in p.components() {
        match comp {
            std::path::Component::ParentDir => {
                if !out.pop() {
                    out.push("..");
                }
            }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_dotdot() {
        assert_eq!(normalize(Path::new("/a/b/../c/./d")), PathBuf::from("/a/c/d"));
    }
}
