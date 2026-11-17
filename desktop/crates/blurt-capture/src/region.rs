//! The recorded area. On Windows and Linux (the platforms this crate is for) a [`Region`] is in physical pixels of
//! the virtual desktop / X screen — what gdigrab and x11grab take — and that is what `record.py --region` receives.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Region {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
}

/// A rectangle in a window's CSS pixels (what an overlay page reports).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CssRect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl Region {
    pub const fn new(x: i32, y: i32, w: u32, h: u32) -> Self {
        Self { x, y, w, h }
    }

    /// Even width and height (yuv420p / H.264 refuse odd sizes); record.py does the same.
    pub fn even(self) -> Self {
        Self { w: self.w & !1, h: self.h & !1, ..self }
    }

    fn right(&self) -> i32 {
        self.x + self.w as i32
    }

    fn bottom(&self) -> i32 {
        self.y + self.h as i32
    }

    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.x && x < self.right() && y >= self.y && y < self.bottom()
    }
}

impl fmt::Display for Region {
    /// `x,y,w,h` — the `record.py --region` form.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{},{},{},{}", self.x, self.y, self.w, self.h)
    }
}

impl FromStr for Region {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let v: Vec<f64> = s
            .split(',')
            .map(|p| p.trim().parse::<f64>().map_err(|e| format!("{p:?}: {e}")))
            .collect::<Result<_, _>>()?;
        match v[..] {
            [x, y, w, h] if w >= 0.0 && h >= 0.0 => Ok(Region::new(x as i32, y as i32, w as u32, h as u32)),
            _ => Err(format!("region needs x,y,w,h (got {s:?})")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn even_sizes() {
        assert_eq!(Region::new(-1905, 30, 151, 75).even(), Region::new(-1905, 30, 150, 74));
        assert!(Region::new(0, 0, 100, 100).contains(99, 0) && !Region::new(0, 0, 100, 100).contains(100, 0));
    }

    #[test]
    fn parse_and_print() {
        let r: Region = "1, 2,321,241".parse().unwrap();
        assert_eq!(r.to_string(), "1,2,321,241");
        assert!("1,2,3".parse::<Region>().is_err());
        assert!("a,2,3,4".parse::<Region>().is_err());
    }
}
