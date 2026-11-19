// Focus rings on check boxes only while someone moves around with Tab (styles.css html.zh-kbnav). The browser's own
// rule (:focus-visible) also turns the ring on for any key pressed after a click: tick a box, press Shift or the
// screenshot keys, and a blue frame appeared round the box ("那个周围一圈蓝框 很丑", 2026-11-19).
export function trackKeyboardNav(): void {
  const root = document.documentElement;
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') root.classList.add('zh-kbnav');
  }, true);
  window.addEventListener('pointerdown', () => root.classList.remove('zh-kbnav'), true);
}
