//! Capture for the Bloom Tauri app (derived from Blurt; contract K7).
//!
//! * [`engine`] — runs `record.py start --engine ffmpeg --status-jsonl` and controls it through the session's
//!   `.cmd` file: [`start`] → [`CaptureHandle`] (`pause` / `resume` / `snap` / `stop` / `discard` / `wait`), and
//!   [`record`], a take watched to its end. Later a native engine (Windows Graphics Capture, PipeWire) can replace
//!   it behind the same API.
//! * [`overlay`] (feature `overlay`, Tauri 2) — the transparent, always-on-top, content-protected island window drawn
//!   by `web/apps/overlays`: [`overlay::island`].
//!
//! Coordinates: a [`Region`] is physical pixels of the virtual desktop (what gdigrab / x11grab take).

pub mod engine;
pub mod region;
pub mod status;

#[cfg(feature = "overlay")]
pub mod overlay;

pub use engine::{record, start, CaptureError, CaptureHandle, CaptureOptions, CaptureState, Launcher, Mic};
pub use region::{CssRect, Region};
pub use status::StatusEvent;
