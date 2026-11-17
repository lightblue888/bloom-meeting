// 导航：上一处/下一处（J/K）、目录跳转与高亮、按条目筛选、展开未改动、右侧缩略刻度、三种视图。
// 依赖页面里先定义的 HD = {key, unfold, fold, rail}
(function () {
  var root = document.documentElement, bar = document.querySelector('.bar');
  function setBarH() { root.style.setProperty('--barh', bar.getBoundingClientRect().height + 'px'); }
  setBarH();
  if (window.ResizeObserver) new ResizeObserver(setBarH).observe(bar);

  // 视图：修订标记 / 修订后 / 原文
  var vbtn = document.querySelectorAll('.views button');
  function setView(v) {
    document.body.setAttribute('data-view', v);
    vbtn.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-v') === v)); });
    try { localStorage.setItem(HD.key, v); } catch (e) {}
    layoutRail();
  }
  vbtn.forEach(function (b) { b.addEventListener('click', function () { setView(b.getAttribute('data-v')); }); });

  var rows = Array.prototype.slice.call(document.querySelectorAll('.chg'));
  var links = {};
  document.querySelectorAll('.nvi').forEach(function (a) { links[a.dataset.target] = a; });
  var pos = document.getElementById('pos'), side = document.getElementById('side'), sb = document.getElementById('sidebtn');
  var cur = -1;

  function visible() { return rows.filter(function (r) { return !r.classList.contains('out'); }); }
  function mark(r) {
    rows.forEach(function (x) { x.classList.toggle('current', x === r); });
    Object.keys(links).forEach(function (k) { links[k].classList.toggle('on', !!r && k === r.id); });
    var vis = visible(), i = vis.indexOf(r);
    pos.innerHTML = '<b>' + (i >= 0 ? i + 1 : '–') + '</b> / ' + vis.length;
    if (r && links[r.id]) {
      var l = links[r.id], lt = l.offsetTop, st = side.scrollTop;
      if (lt < st + 30 || lt > st + side.clientHeight - 30) side.scrollTop = lt - side.clientHeight / 3;
    }
  }
  // 平滑滚动期间不让滚动跟踪改写当前位置，连点「下一处」才不会少跳
  var lockUntil = 0;
  function go(r) {
    if (!r) return;
    lockUntil = Date.now() + 800;
    r.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    cur = rows.indexOf(r); mark(r);
    side.classList.remove('open'); sb.setAttribute('aria-expanded', 'false');
  }
  function step(d) {
    var vis = visible(); if (!vis.length) return;
    var i = cur >= 0 ? vis.indexOf(rows[cur]) : -1;
    var j = i < 0 ? (d > 0 ? 0 : vis.length - 1) : Math.min(vis.length - 1, Math.max(0, i + d));
    go(vis[j]);
  }
  document.getElementById('next').addEventListener('click', function () { step(1); });
  document.getElementById('prev').addEventListener('click', function () { step(-1); });
  document.addEventListener('keydown', function (e) {
    if (e.target.closest('select, input, textarea, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'j' || e.key === 'J' || e.key === 'n') { e.preventDefault(); step(1); }
    if (e.key === 'k' || e.key === 'K' || e.key === 'p') { e.preventDefault(); step(-1); }
  });
  document.querySelectorAll('.nvi').forEach(function (a) {
    a.addEventListener('click', function (e) { e.preventDefault(); go(document.getElementById(a.dataset.target)); });
  });

  // 滚动时跟踪当前位置
  var ticking = false;
  function track() {
    ticking = false;
    drawView();
    if (Date.now() < lockUntil) return;
    var top = (parseFloat(getComputedStyle(root).getPropertyValue('--barh')) || 64) + 40, best = null;
    visible().forEach(function (r) { if (r.getBoundingClientRect().top <= top) best = r; });
    var k = best ? rows.indexOf(best) : -1;
    if (k !== cur) { cur = k; mark(best); }
  }
  window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(track); } }, { passive: true });

  // 按条目筛选
  var flt = document.getElementById('flt');
  if (flt) flt.addEventListener('change', function () {
    var v = flt.value;
    document.body.classList.toggle('filtering', !!v);
    rows.forEach(function (r) {
      var hit = !v || (' ' + r.dataset.notes + ' ').indexOf(' ' + v + ' ') >= 0;
      r.classList.toggle('out', !hit);
      if (links[r.id]) links[r.id].hidden = !hit;
    });
    layoutRail(); track();
    var vis = visible(); if (v && vis.length) go(vis[0]);
  });

  // 展开或收起全部未改动段落
  var folds = document.querySelectorAll('.fold'), unfold = document.getElementById('unfold'), allOpen = false;
  if (unfold) unfold.addEventListener('click', function () {
    allOpen = !allOpen;
    folds.forEach(function (f) { f.open = allOpen; });
    unfold.textContent = allOpen ? HD.fold : HD.unfold;
    layoutRail();
  });
  folds.forEach(function (f) { f.addEventListener('toggle', layoutRail); });

  // 目录抽屉（窄屏）
  sb.addEventListener('click', function () { var o = side.classList.toggle('open'); sb.setAttribute('aria-expanded', String(o)); });

  // 右侧缩略刻度
  var rail = document.getElementById('rail'), viewBox = document.createElement('div');
  viewBox.className = 'view';
  function layoutRail() {
    rail.innerHTML = ''; rail.appendChild(viewBox);
    var h = document.documentElement.scrollHeight, rh = rail.clientHeight;
    rows.forEach(function (r) {
      var a = document.createElement('a');
      a.className = 'k-' + r.dataset.kind + (r.classList.contains('out') ? ' out' : '');
      a.style.top = Math.min(rh - 3, (r.getBoundingClientRect().top + window.scrollY) / h * rh) + 'px';
      a.title = HD.rail.replace('{n}', r.dataset.no);
      a.addEventListener('click', function () { go(r); });
      rail.appendChild(a);
    });
    drawView();
  }
  function drawView() {
    var h = document.documentElement.scrollHeight, rh = rail.clientHeight;
    viewBox.style.top = (window.scrollY / h * rh) + 'px';
    viewBox.style.height = Math.max(8, window.innerHeight / h * rh) + 'px';
  }
  window.addEventListener('resize', function () { setBarH(); layoutRail(); });

  var saved = 'mark';
  try { saved = localStorage.getItem(HD.key) || 'mark'; } catch (e) {}
  setView(saved);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layoutRail);
  track();
})();
