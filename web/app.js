'use strict';
/* goesview front end: no build step, no dependencies. */

// ------------------------------------------------------------------ helpers

const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

const state = { types: new Map(), stats: null, view: null, skew: 0, lut: null };
const enc = p => p.split('/').map(encodeURIComponent).join('/');
const preview = (p, size) => `/p/${size}/${enc(p)}`;
const file = p => `/f/${enc(p)}`;
const typeHref = (id, time) => `#/type/${encodeURIComponent(id)}${time ? `/${time}` : ''}`;
const textHref = (id, item) => `#/text/${encodeURIComponent(id)}${item ? `/${item}` : ''}`;
const plainClick = e => !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button);
const getJSON = url => fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pad = n => String(n).padStart(2, '0');
const now = () => Date.now() / 1000 + state.skew;
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

function utc(t, withDate) {
  const d = new Date(t * 1000);
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`;
  return withDate ? `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hm}` : hm;
}
function ago(t) {
  const s = Math.max(0, now() - t);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const agoEl = t => h('span', { class: 'ago', 'data-t': t, text: ago(t) });
const bytes = n => !n ? '' : n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} kB`;
const clean = s => s.replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '').replace(/^\n+/, '');
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function flash(el) { el.classList.remove('fresh'); void el.offsetWidth; el.classList.add('fresh'); }

setInterval(() => {
  for (const el of document.querySelectorAll('.ago')) el.textContent = ago(+el.dataset.t);
  const d = new Date(now() * 1000);
  $('#clock').textContent = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}, 1000);

/** A tile showing one image item; clicking opens that type's timeline. */
function tile(item, sizes) {
  const type = state.types.get(item.ty);
  return h('a', { class: 'tile', href: typeHref(item.ty, item.t), 'data-ty': item.ty, 'data-id': item.id },
    h('img', { loading: 'lazy', alt: '', sizes: sizes || '260px', onerror: e => e.target.classList.add('broken'),
      srcset: `${preview(item.p, 320)} 320w, ${preview(item.p, 960)} 960w`, src: preview(item.p, 320) }),
    h('div', { class: 'cap' },
      h('b', { text: type.label }),
      h('span', { class: 'grp', text: type.grp }),
      h('span', { class: 'when' }, utc(item.t), ' · ', agoEl(item.r)),
      item.n && h('span', { class: 'note', text: item.n })));
}

/** Sidebar listing the types of one section, grouped. */
function sidebar(section, activeId) {
  const el = h('aside', { class: 'types scroll' });
  const fill = () => {
    el.replaceChildren();
    let group = null;
    for (const t of state.types.values()) {
      if (t.sec !== section || t.kind !== 'i') continue;
      if (t.grp !== group) el.append(h('h3', { text: group = t.grp }));
      el.append(h('a', { href: typeHref(t.id), class: t.id === activeId ? 'on' : '' },
        h('span', { text: t.label }), h('small', { text: t.n })));
    }
  };
  fill();
  el.refresh = debounce(fill, 500);
  return el;
}

// --------------------------------------------------------------- live switch

/** Live or paused, for a feed that scrolls. Live means sitting at the top and taking arrivals as
 *  they come. Scrolling down, or clicking the badge, pauses it: arrivals are only counted, so nothing
 *  moves under the reader. Going back to live reloads the feed if anything was missed. */
function liveSwitch(scroller, refresh) {
  let live = true, missed = 0, byScroll = false;
  const badge = h('button', { class: 'livebadge on' });
  function draw() {
    badge.classList.toggle('on', live);
    scroller.classList.toggle('paused', !live);
    badge.textContent = live ? '● Live' : `❚❚ Paused${missed ? ` · ${missed} new` : ''} · back to live ↑`;
    badge.title = live ? 'New arrivals appear as they come. Click to pause.'
      : 'Not updating while you look back. Click to return to the newest.';
  }
  function set(on, scrolled) {
    if (on === live) return;
    live = on;
    byScroll = !on && !!scrolled;
    if (on) { const stale = missed; missed = 0; scroller.scrollTop = 0; if (stale) refresh(); }
    draw();
  }
  badge.addEventListener('click', () => set(!live));
  scroller.addEventListener('scroll', () => {
    if (live && scroller.scrollTop > 80) set(false, true);
    else if (!live && byScroll && scroller.scrollTop < 4) set(true);  // scrolled back up to the top
  }, { passive: true });
  draw();
  return {
    badge,
    get live() { return live; },
    missed() { missed++; draw(); },
    reset() { live = true; missed = 0; byScroll = false; draw(); },
  };
}

// -------------------------------------------------------------------- pop-up

/** Shows one item over the current page, with a way through to its own page. */
const popup = (() => {
  const dlg = h('dialog', { class: 'popup' });
  document.body.append(dlg);
  let list = [], at = 0, token = 0, onShow = null, onClose = null;
  const zoom = { s: 1, x: 0, y: 0 };  // kept while stepping, so one spot can be compared across frames
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });  // a click on the backdrop
  // The wheel (or a finger) belongs to the pop-up while it is open: it zooms an image or scrolls
  // a bulletin, and never reaches the page underneath. A modal dialog does not stop that by
  // itself: the wheel over its backdrop, or over content with nothing left to scroll, moves the
  // page. The page is otherwise left alone, so it is where it was when the pop-up closes.
  function guard(e) {
    const inside = dlg.contains(e.target);
    if (!inside && !dlg.matches(':modal')) return;  // docked: the rest of the page works as usual
    const text = inside && e.target.closest('.text .body');
    if (text) {
      const room = e.type !== 'wheel' ? text.scrollHeight > text.clientHeight + 1
        : e.deltaY > 0 ? text.scrollTop + text.clientHeight < text.scrollHeight - 1 : text.scrollTop > 0;
      if (room) return;  // this one scrolls the bulletin
    }
    e.preventDefault();
  }
  const guarded = on => {  // only while open: a blocking wheel listener costs every scroll a little
    for (const kind of ['wheel', 'touchmove']) {
      document[on ? 'addEventListener' : 'removeEventListener'](kind, guard, { passive: false, capture: true });
    }
  };
  dlg.addEventListener('close', () => {
    if (dlg.open) return;  // it was only switching between docked and modal
    guarded(false);
    if (onClose) onClose();
  });
  addEventListener('resize', () => { if (dlg.open) place(); });
  document.addEventListener('keydown', e => {
    if (dlg.open && e.key === 'Escape' && !dlg.matches(':modal')) return dlg.close();  // modal dialogs do this themselves
    if (!dlg.open || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    e.preventDefault();
    step(e.key === 'ArrowLeft' ? -1 : 1);
  });
  function step(d) { if (list.length > 1) { at = (at + d + list.length) % list.length; draw(); } }

  function draw() {
    const item = list[at], type = state.types.get(item.ty), mine = ++token;
    const isImage = item.k === 'i', many = list.length > 1;
    const [code, desc] = !isImage && type ? type.label.split(' · ') : ['', ''];
    const body = h('div', { class: 'body' });
    if (isImage) {
      const img = h('img', { alt: '', draggable: 'false', src: preview(item.p, 960) });  // already cached; sharpen when ready
      const level = h('button', { class: 'zoomlevel', title: 'Reset zoom (double-click the image)', hidden: true });
      body.append(img, level);
      body.title = 'Scroll to zoom, drag to move, double-click to reset';
      // Load the smallest version that is sharp at the current zoom: the big preview, or the original.
      const sharpen = () => {
        const px = Math.min(body.clientWidth, body.clientHeight) * zoom.s * (window.devicePixelRatio || 1);
        const url = px <= 2300 ? preview(item.p, 1920) : file(item.p);
        if (img.dataset.url === url) return;
        const next = new Image();
        next.onload = () => { if (mine === token) { img.src = url; img.dataset.url = url; } };
        next.src = url;
      };
      const sharpenSoon = debounce(sharpen, 250);
      const place = () => {
        const w = body.clientWidth, ht = body.clientHeight;
        zoom.x = clamp(zoom.x, w - w * zoom.s, 0); zoom.y = clamp(zoom.y, ht - ht * zoom.s, 0);
        img.style.transform = zoom.s === 1 ? '' : `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
        body.classList.toggle('zoomed', zoom.s > 1);
        level.hidden = zoom.s === 1;
        level.textContent = `${zoom.s.toFixed(1)}× · reset`;
      };
      const reset = () => { zoom.s = 1; zoom.x = zoom.y = 0; place(); sharpenSoon(); };
      level.addEventListener('click', reset);
      body.addEventListener('dblclick', reset);
      body.addEventListener('wheel', e => {
        const r = body.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
        const to = clamp(zoom.s * Math.exp(-e.deltaY * 0.0016), 1, 16);
        zoom.x = cx - (cx - zoom.x) * to / zoom.s; zoom.y = cy - (cy - zoom.y) * to / zoom.s; zoom.s = to;
        place(); sharpenSoon();
      }, { passive: true });
      let grab = null;
      body.addEventListener('pointerdown', e => {
        if (zoom.s === 1 || e.target === level) return;
        grab = { x: e.clientX - zoom.x, y: e.clientY - zoom.y }; body.setPointerCapture(e.pointerId);
      });
      body.addEventListener('pointermove', e => { if (grab) { zoom.x = e.clientX - grab.x; zoom.y = e.clientY - grab.y; place(); } });
      body.addEventListener('pointerup', () => { grab = null; });
      requestAnimationFrame(() => { place(); sharpen(); });  // once it is in the page and has a size
    } else {
      const pre = h('pre', { text: 'Loading…' });
      fetch(file(item.p)).then(r => r.text()).then(t => { if (mine === token) pre.textContent = clean(t); })
        .catch(() => { pre.textContent = 'Could not load this bulletin.'; });
      body.append(pre);
    }
    dlg.className = `popup ${isImage ? 'image' : 'text'}`;
    // Three fixed columns, so the buttons stay put while stepping: nav | what (clipped) | actions.
    dlg.replaceChildren(
      h('header', {},
        h('div', { class: 'nav' },
          many && h('button', { title: 'Previous (←)', text: '◀', onclick: () => step(-1) }),
          many && h('span', { class: 'count', text: `${at + 1} / ${list.length}` }),
          many && h('button', { title: 'Next (→)', text: '▶', onclick: () => step(1) })),
        h('div', { class: 'what' },
          h('div', { class: 'line' },
            isImage ? h('b', { text: type ? type.label : '' })
              : [h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }), h('b', { text: item.nm || '' })],
            h('span', { class: 'grp', text: isImage ? (type ? type.grp : '') : (desc || '') })),
          h('div', { class: 'line sub' },
            h('span', { text: utc(item.t, true) }), agoEl(item.r),
            item.n && h('span', { class: 'note', text: item.n }))),
        h('div', { class: 'acts' },
          h('a', { class: 'btn go', href: isImage ? typeHref(item.ty, item.t) : textHref(item.ty, item.id) },
            isImage ? 'Open in timeline →' : ['See it among ', h('span', { class: 'code', text: code || '···' }), ' bulletins →']),
          h('a', { class: 'btn', href: file(item.p), target: '_blank', rel: 'noopener', text: isImage ? 'Full resolution ↗' : 'Raw ↗' }),
          h('button', { class: 'shut', title: 'Close (Esc)', text: 'Close', onclick: () => dlg.close() }))),
      body);
    if (onShow) onShow(item);
  }

  /** On the Live page the pop-up docks over the image area and leaves the right-hand bar in
   *  reach, so other boxes and bulletins can be clicked one after another. Anywhere else, and
   *  on narrow screens where the page is one column, it is an ordinary modal dialog. */
  function place() {
    const live = $('.live'), side = live && $('.side', live);
    const dock = !!side && innerWidth > 1100;
    if (dlg.open && dock === dlg.matches(':modal')) dlg.close();  // switching between the two modes
    if (dock) {
      const a = live.getBoundingClientRect(), b = side.getBoundingClientRect();
      Object.assign(dlg.style, { left: `${a.left + 10}px`, top: `${a.top + 10}px`,
        width: `${b.left - a.left - 20}px`, height: `${a.height - 20}px` });
      if (!dlg.open) dlg.show();
    } else {
      dlg.removeAttribute('style');
      if (!dlg.open) dlg.showModal();
    }
    dlg.classList.toggle('docked', dock);
  }

  return {
    /** opts.onShow(item) runs for each item shown; opts.onClose() when the pop-up goes away. */
    open(items, index, opts = {}) {
      const closing = onClose;
      onShow = onClose = null;        // the previous opener's selection is cleared by its own callback
      if (closing) closing();
      list = items; at = Math.max(0, index);
      zoom.s = 1; zoom.x = zoom.y = 0;
      onShow = opts.onShow || null;
      draw();
      onClose = opts.onClose || null;
      place();
      guarded(true);
    },
    close() { if (dlg.open) dlg.close(); },
  };
})();

// ---------------------------------------------------------------- live view

const TICKER_LOAD = 200;  // bulletins fetched when the Live page opens: roughly the last 15 minutes
const TICKER_KEEP = 300;  // and how many it holds as new ones arrive
const WALL_PAGE = 36;     // images fetched per page on the wall
const WALL_KEEP = 300;    // tiles kept while live, before the oldest are dropped
const WARN_CODES = new Set(['TOR', 'SVR', 'FFW', 'SMW', 'TSU', 'FLW', 'RFW', 'WSW', 'NPW', 'HLS', 'TCP', 'SVS']);

function headline(body) {
  for (const line of clean(body || '').split('\n').slice(2)) {
    const s = line.trim();
    if (s.length > 12 && !/^\w{4}\d\d \w{4} \d{6}/.test(s) && !/^[A-Z0-9]{6,9}$/.test(s)) return s;
  }
  return '';
}

function LiveView(root) {
  const hero = h('a', { class: 'hero' });
  const wall = h('div', { class: 'wall' });
  const wallEnd = h('div', { class: 'feedend' });
  const wallScroll = h('div', { class: 'wallscroll' }, wall, wallEnd);
  const tele = h('div', { class: 'panel' });
  const rows = h('div', { class: 'rows' });
  const wallLive = liveSwitch(wallScroll, () => loadWall());
  const tickLive = liveSwitch(rows, () => loadTicker());
  root.append(h('div', { class: 'live' }, hero,
    h('div', { class: 'wallbox' },
      h('div', { class: 'boxhead' }, h('h3', { text: 'Images as received' }), wallLive.badge), wallScroll),
    h('aside', { class: 'side' }, tele,
      h('div', { class: 'panel ticker' },
        h('div', { class: 'boxhead' }, h('h3', { text: 'Bulletins as received' }), tickLive.badge), rows))));

  function heroType() {
    const saved = state.types.get(store.get('hero'));
    if (saved && saved.last && saved.kind === 'i') return saved;
    const images = [...state.types.values()].filter(t => t.kind === 'i' && t.last && t.sec === 'imagery');
    return images.find(t => /Full Disk/.test(t.grp) && /^False Color · /.test(t.label))
      || images.find(t => /Full Disk/.test(t.grp) && /False Color/.test(t.label))
      || images.find(t => /Full Disk/.test(t.grp)) || images[0];
  }

  function drawHero() {
    const type = heroType();
    if (!type) { hero.replaceChildren(h('div', { class: 'empty', text: 'Waiting for the first image…' })); return; }
    const item = type.last;
    if (hero.dataset.p === item.p) return;
    hero.dataset.p = item.p;
    hero.href = typeHref(type.id);
    const img = new Image();
    img.alt = '';
    img.onload = () => {
      if (hero.dataset.p !== item.p) return;
      hero.replaceChildren(img, h('div', { class: 'cap' },
        h('b', { text: type.label }), h('span', { class: 'grp', text: type.grp }),
        h('span', { text: utc(item.t, true) }), agoEl(item.r),
        item.n && h('span', { class: 'note', text: item.n })));
    };
    img.src = preview(item.p, 1920);
  }

  // The wall: every image as it arrives, newest first; scrolling down loads older ones.
  let shown = [], wallBefore = 0, wallBusy = false, wallDone = false, wallRun = 0;
  function wallTile(item) {
    const el = tile(item, '(max-width: 1100px) 33vw, 20vw');
    el.addEventListener('click', e => { if (plainClick(e)) { e.preventDefault(); popup.open(shown.slice(), shown.indexOf(item)); } });
    el.append(h('button', { class: 'pin', title: 'Show this type large', text: '★',
      onclick: e => { e.preventDefault(); e.stopPropagation(); store.set('hero', item.ty); delete hero.dataset.p; drawHero(); loadWall(); } }));
    return el;
  }
  async function moreWall() {
    if (wallBusy || wallDone) return;
    wallBusy = true;
    const mine = wallRun, lead = heroType();
    const d = await getJSON(`/api/items?kind=i&limit=${WALL_PAGE}${wallBefore ? `&before=${wallBefore}` : ''}`).catch(() => null);
    if (mine !== wallRun) return;
    wallBusy = false;
    if (!d) return;
    for (const item of d.items) {
      if (lead && item.ty === lead.id) continue;  // the large image already shows this type
      shown.push(item);
      wall.append(wallTile(item));
    }
    if (d.items.length) wallBefore = d.items[d.items.length - 1].id;
    wallDone = !d.more;
    wallEnd.textContent = wallDone ? 'That is everything received.' : '';
    // A page may not fill a tall screen, and the observer only fires when visibility changes.
    if (!wallDone && wallEnd.getBoundingClientRect().top < wallScroll.getBoundingClientRect().bottom + 300) moreWall();
  }
  function loadWall() {
    wallRun++; shown = []; wallBefore = 0; wallBusy = wallDone = false;
    wall.replaceChildren(); wallScroll.scrollTop = 0;
    moreWall();
  }
  const wallWatch = new IntersectionObserver(es => es[0].isIntersecting && moreWall(), { root: wallScroll, rootMargin: '300px' });
  wallWatch.observe(wallEnd);

  function spark(values) {
    const max = Math.max(1, ...values);
    const pts = values.map((v, i) => `${i},${(15 - 14 * v / max).toFixed(1)}`).join(' ');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 59 16');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = `<polyline points="${pts}"/>`;
    return svg;
  }

  function drawStats(s) {
    if (!s) return;
    const kpi = (label, sec) => {
      const rate = s.rate[sec] || [];
      const href = `#/${sec}`;
      return h('a', { class: 'kpi', href, title: `Show ${label.toLowerCase()} items as received` }, h('label', { text: label }),
        h('b', { text: rate.reduce((a, b) => a + b, 0) }), h('small', { text: '/ hr' }),
        spark(rate.length ? rate : new Array(60).fill(0)),
        s.last[sec] ? agoEl(s.last[sec]) : h('span', { class: 'dim', text: 'nothing yet' }));
    };
    // Each section folds away; the choice is remembered. `brief` is what shows while it is folded.
    // `pinned`, if any, sits under the heading and stays visible folded or not.
    const fold = (key, title, brief, open, ...content) => {
      const pinned = content[0] && content[0].classList && content[0].classList.contains('pinned') ? content.shift() : null;
      const el = h('details', { class: 'fold', open: store.get(`fold:${key}`, open ? '1' : '0') === '1' },
        h('summary', {}, h('span', { text: title }), h('span', { class: 'brief' }, brief), pinned), ...content);
      el.addEventListener('toggle', () => store.set(`fold:${key}`, el.open ? '1' : '0'));
      return el;
    };
    const hourly = Object.values(s.rate).flat().reduce((a, b) => a + b, 0);
    const parts = [fold('reception', 'Reception, last hour', `${hourly} items`, true,
      h('div', { class: 'kpis' }, kpi('Imagery', 'imagery'), kpi('Weather', 'weather'), kpi('Text', 'text')))];
    const settle = [];  // run once the strips are on the page
    for (const scan of s.scans) {
      const all = [...new Set(scan.slots.flatMap(x => x.b))].sort((a, b) => a - b);
      if (!all.length) continue;
      const slots = scan.slots, last = slots.length - 1, COL = 13;  // 11px column + 2px gap
      const grid = h('div', { class: 'bands', style: `grid-template-columns: 20px repeat(${slots.length}, 11px)` });
      // A band filed apart from its scan: a folder with few bands sitting within 11 minutes of a
      // fuller one. A scan that is merely still arriving has no such neighbour and is not flagged.
      const fuller = (i, j) => j >= 0 && j <= last && Math.abs(slots[i].t - slots[j].t) <= 660 && slots[j].b.length > slots[i].b.length;
      const odd = slots.map((x, i) => x.b.length * 2 < all.length && (fuller(i, i - 1) || fuller(i, i + 1)));
      grid.append(h('span', { class: 'lab' }));
      slots.forEach((x, i) => {
        const d = new Date(x.t * 1000), onHour = !odd[i] && d.getUTCMinutes() === 0;
        const label = odd[i] ? '·' : onHour ? pad(d.getUTCHours()) : '';
        grid.append(h('span', { title: utc(x.t, true), text: i === last ? label || '▾' : label,
          class: 'hd' + (odd[i] ? ' odd' : '') + (onHour && d.getUTCHours() === 0 ? ' day' : '') + (i === last ? ' now' : '') }));
      });
      for (const band of all) {
        grid.append(h('span', { class: 'lab', text: band }));
        slots.forEach((x, i) => {
          const got = x.b.includes(band);
          grid.append(h('i', { 'data-band': band, 'data-time': x.t,
            title: got ? `Band ${band}, ${utc(x.t, true)}${odd[i] ? ', filed under an earlier scan' : ''}. Click to view.`
              : `Band ${band} not received for ${utc(x.t, true)}`,
            class: (got ? (odd[i] ? 'odd' : 'on') : '') + (i === last ? ' now' : '') }));
        });
      }
      // Where am I: the newest scan is ringed; scrolled back, say what is in view and offer the way home.
      const scroller = h('div', { class: 'bandscroll', 'data-g': scan.g }, grid);
      const place = h('span');
      const home = h('button', { class: 'link', text: 'Back to newest →', hidden: true,
        onclick: () => scroller.scrollTo({ left: scroller.scrollWidth, behavior: 'smooth' }) });
      const where = () => {
        const atEnd = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 6;
        home.hidden = atEnd;
        if (atEnd) return place.replaceChildren(`Newest scan ${utc(slots[last].t)}, ringed · `, agoEl(slots[last].t));
        const from = clamp(Math.floor(scroller.scrollLeft / COL), 0, last);
        const to = clamp(Math.floor((scroller.scrollLeft + scroller.clientWidth - 22) / COL) - 1, from, last);
        place.replaceChildren(`Viewing ${utc(slots[from].t)} to ${utc(slots[to].t)} · newest is ${utc(slots[last].t)}`);
      };
      scroller.addEventListener('scroll', where, { passive: true });
      settle.push(where);
      parts.push(fold(`scan:${scan.g}`, `${scan.g}: bands per scan, last 24 h`, `newest ${utc(slots[last].t)}`, true, scroller,
        h('div', { class: 'where' }, place, h('span', { class: 'spacer' }), home),
        h('div', { class: 'legend' }, 'UTC hour', h('i', { style: 'background:var(--good)' }), 'received',
          h('i', { style: 'background:var(--warn)' }), 'filed under an earlier scan')));
    }
    if (s.storage) {
      const { free, total, cache, satdump, dry, last, scope, archive } = s.storage;
      const dcsKeep = s.storage.dcs && s.storage.dcs.limits, dcsZip = s.storage.dcs && s.storage.dcs.archive;
      const gb = n => (n / 2 ** 30).toFixed(n < 100 * 2 ** 30 ? 1 : 0);
      const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
      // Exactly which folders SatDump retention may delete from, and what it leaves alone.
      const reach = satdump && scope && h('div', { class: 'reach' },
        `Applies to ${plural(scope.runs.length, 'GOES HRIT run folder')}:`,
        scope.runs.map(name => h('div', { class: 'run', text: name })),
        scope.protected.length ? h('div', {}, `Protected: ${scope.protected.join(', ')}`) : null,
        scope.others.length ? h('div', { title: scope.others.join('\n') }, `Never touches the ${plural(scope.others.length, 'other item')} in this folder.`) : null,
        scope.skipped ? h('div', {}, `Leaves ${plural(scope.skipped, 'unrecognised item')} inside the runs alone.`) : null);
      const used = 1 - free / total, trial = dry ? ' (dry run)' : '';
      const sweep = info => info && h('div', {}, `${info.dry ? 'Would remove' : 'Last removed'} ${info.what} · `, agoEl(info.t));
      // Two kinds of files, shown apart: our own cache, and what SatDump received.
      // Folded by default: the one line says how full the disk is and whether SatDump data can be deleted.
      // Amber only when SatDump's data is really being deleted; green when it is safe.
      const deleting = !!satdump && !dry;
      const verdict = !satdump ? h('span', { class: 'safe', text: 'never deleted' })
        : dry ? h('span', { class: 'safe', text: 'dry run: nothing will be deleted' })
          : h('span', { class: 'note', text: 'oldest data will be deleted as needed' });
      // Folded, the heading carries the SatDump setting, which is one of two things: delete oldest, or never delete.
      // A dry run is a third, testing-only state: limits are set but not in force.
      // DCS files are SatDump's too, under a policy of their own: either one deleting makes the heading amber.
      const brief = (satdump || dcsKeep) && !dry ? h('span', { class: 'note', text: 'SatDump pruning: delete oldest' })
        : h('span', { class: 'safe', text: `SatDump pruning: ${satdump || dcsKeep ? 'dry run' : 'never delete'}` });
      parts.push(fold('storage', 'Storage', brief, false,
        h('div', { class: 'pinned' },
          h('div', { class: 'meter', title: `${Math.round(used * 100)}% of the disk used` },
            h('i', { class: used > 0.9 ? 'full' : '', style: `width:${(used * 100).toFixed(1)}%` })),
          h('div', { class: 'legend' }, `${gb(free)} GB free of ${gb(total)} GB`)),
        h('div', { class: 'keeps' },
          h('b', { text: 'GOESview cache' }),
          h('span', {}, cache ? cache + trial : 'kept forever', sweep(last && last.cache)),
          archive && h('b', { text: 'Bulletins' }),
          archive && h('span', {}, `today's as files, past days zipped${archive.dry ? ' (dry run)' : ''}`,
            h('div', {}, `${plural(archive.days, 'day')} zipped so far`),
            archive.last && h('div', {}, `Last: ${archive.last.day}, ${archive.last.n.toLocaleString()} bulletins, ${gb(archive.last.freed)} GB of files into ${(archive.last.size / 2 ** 20).toFixed(0)} MB · `, agoEl(archive.last.t))),
          h('b', { class: deleting ? 'deletes' : '', text: 'SatDump data' }),
          h('span', {}, satdump ? `${satdump}; ` : '', verdict, reach, sweep(last && last.satdump)),
          (s.dcs || dcsKeep || dcsZip) && h('b', { class: dcsKeep && !dry ? 'deletes' : '', text: 'DCS files' }),
          (s.dcs || dcsKeep || dcsZip) && h('span', {}, dcsKeep ? `${dcsKeep}; ` : '',
            !dcsKeep ? h('span', { class: 'safe', text: 'never deleted' })
              : dry ? h('span', { class: 'safe', text: 'dry run: nothing will be deleted' })
                : h('span', { class: 'note', text: 'oldest DCS files will be deleted as needed' }),
            dcsZip && h('div', {}, `today's as files, past days zipped${dcsZip.dry ? ' (dry run)' : ''}; ${plural(dcsZip.days, 'day')} zipped so far`),
            dcsZip && dcsZip.last && h('div', {}, `Last: ${dcsZip.last.day}, ${dcsZip.last.n.toLocaleString()} files, ${gb(dcsZip.last.freed)} GB into ${(dcsZip.last.size / 2 ** 20).toFixed(0)} MB · `, agoEl(dcsZip.last.t)),
            sweep(last && last.dcs)))));
    }
    if (s.health) {
      const tone = { ok: 'safe', warn: 'note', bad: 'alarm' }[s.health.state];
      parts.push(fold('system', 'System', h('span', { class: tone, text: s.health.state === 'ok' ? 'everything is working' : `${s.health.n} to look at` }), false,
        h('a', { class: `syslink ${tone}`, href: '#/system', title: 'Open the System page' }, s.health.text, h('span', { class: 'dim', text: '  Open the System page →' }))));
    }
    if (s.dcs) {
      parts.push(fold('dcs', 'DCS', `${s.dcs.rate.toLocaleString()} messages / min`, false,
        h('div', { class: 'kpis two' },
          h('a', { class: 'kpi', href: '#/dcs', title: 'Open the DCS page' }, h('label', { text: 'Messages' }),
            h('b', { text: s.dcs.rate.toLocaleString() }), h('small', { text: '/ min' }),
            s.dcs.last ? h('div', {}, agoEl(s.dcs.last)) : null),
          h('a', { class: 'kpi', href: '#/dcs', title: 'Open the DCS page' }, h('label', { text: 'Platforms heard' }),
            h('b', { text: s.dcs.heard.toLocaleString() }), h('small', { text: 'this hour' })))));
    }
    if (s.satdump) {
      const keys = Object.keys(s.satdump);
      const wanted = keys.filter(k => /snr|ber|lock|freq|sync|deframer|viterbi|rs_avg/i.test(k));
      const kv = h('div', { class: 'kv' });
      for (const k of (wanted.length ? wanted : keys).slice(0, 12)) kv.append(h('span', { text: k, title: k }), h('span', { text: s.satdump[k] }));
      parts.push(fold('satdump', 'SatDump', '', true, kv));
    }
    // The panel is redrawn every few seconds: keep each strip where the reader left it,
    // and pinned to the newest scan unless they scrolled back.
    const held = new Map([...tele.querySelectorAll('.bandscroll')].map(e =>
      [e.dataset.g, { left: e.scrollLeft, atEnd: e.scrollLeft + e.clientWidth >= e.scrollWidth - 6 }]));
    tele.replaceChildren(...parts);
    for (const e of tele.querySelectorAll('.bandscroll')) {
      const was = held.get(e.dataset.g);
      e.scrollLeft = !was || was.atEnd ? e.scrollWidth : was.left;
    }
    settle.forEach(run => run());
    mark();
  }

  // What the pop-up is showing, so the right-hand bar can mark it: a band box, or a bulletin row.
  let picked = null;
  function mark() {
    for (const el of root.querySelectorAll('.sel')) el.classList.remove('sel');
    if (!picked) return;
    if (picked.row) { picked.row.classList.add('sel'); return; }
    for (const strip of tele.querySelectorAll('.bandscroll')) {
      if (strip.dataset.g !== picked.g) continue;
      const cell = strip.querySelector(`i[data-band="${picked.band}"][data-time="${picked.t}"]`);
      if (cell) {
        cell.classList.add('sel');
        if (picked.reveal) {  // once per selection: bring it into the strip's view without moving the page
          const box = strip.getBoundingClientRect(), c = cell.getBoundingClientRect();
          if (c.left < box.left + 26) strip.scrollLeft -= box.left + 26 - c.left;
          else if (c.right > box.right - 4) strip.scrollLeft += c.right - box.right + 4;
          picked.reveal = false;
        }
      }
    }
  }
  const unpick = () => { picked = null; mark(); };

  // A filled box in a bands-per-scan strip opens that band's image for that scan;
  // the arrows then step through the same band over time, as the strip does.
  tele.addEventListener('click', async e => {
    const cell = e.target.closest('.bands i.on, .bands i.odd');
    if (!cell) return;
    const group = cell.closest('.bandscroll').dataset.g, band = cell.dataset.band, time = +cell.dataset.time;
    const type = [...state.types.values()].find(t => t.kind === 'i' && t.grp === group && (t.label === `Band ${band}` || t.label.startsWith(`Band ${band} ·`)));
    if (!type) return;
    const d = await getJSON(`/api/timeline?type=${encodeURIComponent(type.id)}`).catch(() => null);
    if (!d) return;
    const frames = d.frames.map(f => ({ k: 'i', ty: type.id, t: f[0], r: f[0], p: f[1], m: f[2] || undefined, n: f[3] || undefined, s: f[4] }));
    const at = frames.findIndex(f => f.t === time);
    if (at >= 0) popup.open(frames, at, { onShow: f => { picked = { g: group, band, t: f.t, reveal: true }; mark(); }, onClose: unpick });
  });

  const ticker = [];  // the bulletins on screen, newest first
  function bulletin(item, body) {
    const type = state.types.get(item.ty);
    const code = type ? type.label.slice(0, 3) : '';
    return h('a', { class: 'row', href: textHref(item.ty, item.id), 'data-id': item.id,
      onclick: e => {
        if (!plainClick(e)) return;
        e.preventDefault();
        popup.open(ticker.slice(), ticker.indexOf(item), { onClose: unpick, onShow: shown => {
          const row = rows.querySelector(`.row[data-id="${shown.id}"]`);
          picked = row ? { row } : null;
          mark();
          if (row) {  // keep it in view within the list, without moving the page itself
            const box = rows.getBoundingClientRect(), r = row.getBoundingClientRect();
            if (r.top < box.top) rows.scrollTop -= box.top - r.top;
            else if (r.bottom > box.bottom) rows.scrollTop += r.bottom - box.bottom;
          }
        } });
      } },
      h('time', { text: utc(item.t) }),
      h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }),
      h('span', { class: 'nm', text: item.nm || (type && type.label) || '' }),
      h('span', { class: 'line', text: headline(body) }));
  }

  function loadTicker() {
    getJSON(`/api/items?sec=text&kind=t&limit=${TICKER_LOAD}&body=1`).then(d => {
      ticker.splice(0, ticker.length, ...d.items);
      rows.replaceChildren(...d.items.map(i => bulletin(i, i.b)));
      rows.scrollTop = 0;
    });
  }

  drawHero(); loadWall(); loadTicker(); drawStats(state.stats);

  return {
    nav: 'live',
    onItem(item) {
      if (item.k === 'i') {
        drawHero();
        const lead = heroType();
        if (lead && item.ty === lead.id) return;
        if (!wallLive.live) return wallLive.missed();  // paused: count it, move nothing
        shown.unshift(item);
        const el = wallTile(item);
        wall.prepend(el); flash(el);
        while (wall.children.length > WALL_KEEP) {
          wall.lastChild.remove(); shown.pop();
          wallDone = false; wallBefore = shown[shown.length - 1].id;
        }
        return;
      }
      if (!tickLive.live) return tickLive.missed();
      fetch(file(item.p), { headers: { Range: 'bytes=0-900' } }).then(r => r.text()).then(body => {
        const el = bulletin(item, body);
        ticker.unshift(item); rows.prepend(el); flash(el);
        while (rows.children.length > TICKER_KEEP) { rows.lastChild.remove(); ticker.pop(); }
      }).catch(() => {});
    },
    onTypes() { drawHero(); },
    onStats: drawStats,
    destroy() { wallWatch.disconnect(); picked = null; },
  };
}

// ------------------------------------------- section: groups, latest and feed


// How many tiles an as-received list holds. A busy feed sends about 2,400 images a day to the Imagery
// overview, so this is more than a day of it; a page left open for a week stays this size.
const SECTION_KEEP = 3000;

function SectionView(root, section, mode) {
  const side = sidebar(section);
  const tabs = h('div', { class: 'tabs' });
  const filters = h('div', { class: 'filters' });
  const pane = h('div', { class: 'pane scroll' });
  root.append(h('div', { class: 'section' }, side, h('div', { class: 'content' }, tabs, filters, pane)));

  let onlyGroup = store.get(`group:${section}`, ''), show = store.get(`show:${section}`, 'all');
  let before = 0, loading = false, done = false, run = 0, allowance = SECTION_KEEP;
  const grid = h('div', { class: 'grid' });
  const sentinel = h('div', { class: 'empty' });
  const watcher = new IntersectionObserver(es => es[0].isIntersecting && more(), { root: pane, rootMargin: '600px' });
  const waiting = [];  // arrivals held back while paused, oldest first
  const live = mode === 'feed' ? liveSwitch(pane, () => catchUp())
    : { badge: null, live: true, missed() {}, reset() {} };  // "latest of each" is always current

  const kinds = () => [...state.types.values()].filter(t => t.sec === section && t.kind === 'i');
  const groups = () => [...new Set(kinds().map(t => t.grp))];
  const inGroup = t => !onlyGroup || t.grp === onlyGroup;
  // Processed composites versus raw channels / data fields; only a choice where a group has both.
  const mixed = () => { const k = kinds().filter(inGroup); return k.some(t => t.raw) && k.some(t => !t.raw); };
  const rawChoice = () => mixed() ? show : 'all';
  const passes = t => inGroup(t) && (rawChoice() === 'all' || (rawChoice() === 'raw') === !!t.raw);
  const rawQuery = () => rawChoice() === 'all' ? '' : `&raw=${rawChoice() === 'raw' ? 1 : 0}`;
  const overview = () => !onlyGroup && groups().length > 1;
  const pickGroup = g => { onlyGroup = g; store.set(`group:${section}`, g); redraw(); };

  function drawBars() {
    const all = groups();
    if (onlyGroup && !all.includes(onlyGroup)) onlyGroup = '';
    const tab = (label, on, g) => h('button', { class: on ? 'on' : '', text: label, onclick: () => pickGroup(g) });
    tabs.replaceChildren(...(all.length > 1 ? [tab('Overview', !onlyGroup, ''), ...all.map(g => tab(g, onlyGroup === g, g))]
      : [h('b', { text: all[0] || '' })]));
    const chip = (label, value) => h('button', { class: show === value ? 'on' : '', text: label,
      onclick: () => { show = value; store.set(`show:${section}`, value); redraw(); } });
    filters.replaceChildren(...[
      h('a', { href: `#/${section}`, class: mode === 'feed' ? 'on' : '', text: 'Everything as received' }),
      h('a', { href: `#/${section}/latest`, class: mode === 'latest' ? 'on' : '', text: 'Latest of each type' }),
      h('span', { class: 'spacer' }),
      mixed() && chip('Processed and raw', 'all'), mixed() && chip('Processed', 'processed'), mixed() && chip('Raw', 'raw'),
      live.badge,
    ].filter(Boolean));
  }

  /** A new arrival goes on top. Past SECTION_KEEP the oldest tiles are dropped from the far end, which
   *  only happens while live, with the reader at the top; scrolling down fetches them again. */
  function place(item) {
    const el = tile(item); grid.prepend(el); flash(el);
    if (grid.children.length <= SECTION_KEEP) return;
    while (grid.children.length > SECTION_KEEP) grid.lastChild.remove();
    before = +grid.lastChild.dataset.id;  // continue from the oldest tile still shown
    run++; loading = done = false; allowance = SECTION_KEEP;  // a page being fetched now would leave a gap
    sentinel.textContent = 'Loading…';
  }

  /** Back to live: add what arrived meanwhile, and leave what is already on the page
   *  (older items scrolled into included) as it is. */
  function catchUp() {
    const items = waiting.splice(0);
    if (items.length >= SECTION_KEEP) return redraw();  // paused so long the oldest arrivals were let go
    for (const item of items) place(item);
  }

  function redraw() {
    live.reset();
    waiting.length = 0;
    drawBars();
    run++; before = 0; done = loading = false; allowance = SECTION_KEEP;
    watcher.unobserve(sentinel);
    pane.replaceChildren();
    if (mode === 'latest') drawLatest();
    else { grid.replaceChildren(); pane.append(grid, sentinel); watcher.observe(sentinel); more(); }
  }

  /** One tile per type, under a heading per group. */
  function drawLatest() {
    pane.replaceChildren();
    let group = null, box = null;
    for (const t of state.types.values()) {
      if (t.sec !== section || t.kind !== 'i' || !t.last || !passes(t)) continue;
      if (t.grp !== group) pane.append(heading(group = t.grp), box = h('div', { class: 'grid' }));
      box.append(tile(t.last));
    }
    if (!pane.children.length) pane.append(h('div', { class: 'empty', text: 'Nothing received yet.' }));
  }

  function heading(group) {
    return h('h3', {}, group, overview() && h('button', { class: 'link', text: 'open', onclick: () => pickGroup(group) }));
  }

  async function more() {
    if (loading || done) return;
    if (grid.children.length >= allowance) {  // a lot is loaded: going further back is the reader's call
      sentinel.replaceChildren(`${grid.children.length.toLocaleString()} shown.`,
        h('button', { class: 'more', text: 'Show older', onclick: () => {
          allowance = grid.children.length + SECTION_KEEP; sentinel.textContent = 'Loading…'; more();
        } }));
      return;
    }
    loading = true;
    const mine = run;
    const d = await getJSON(`/api/items?sec=${section}&kind=i&limit=48${before ? `&before=${before}` : ''}`
      + (onlyGroup ? `&group=${encodeURIComponent(onlyGroup)}` : '') + rawQuery());
    if (mine !== run) return;
    for (const item of d.items) grid.append(tile(item));
    if (d.items.length) before = d.items[d.items.length - 1].id;
    done = !d.more;
    sentinel.textContent = done ? (grid.children.length ? 'That is everything.' : 'Nothing received yet.') : 'Loading…';
    loading = false;
    // A page may not fill a tall screen, and the observer only fires when visibility changes.
    if (!done && sentinel.getBoundingClientRect().top < Math.min(pane.getBoundingClientRect().bottom, innerHeight) + 600) more();
  }

  redraw();

  return {
    nav: section,
    onItem(item, type) {
      side.refresh();
      if (item.k !== 'i' || type.sec !== section || !passes(type)) return;
      if (mode === 'latest') {
        const old = [...pane.querySelectorAll('.tile')].find(e => e.dataset.ty === item.ty);
        if (!old) return drawLatest();
        const el = tile(type.last); old.replaceWith(el); flash(el);
      } else if (!live.live) {
        waiting.push(item);  // paused: count it, move nothing
        if (waiting.length > SECTION_KEEP) waiting.shift();
        live.missed();
      } else place(item);
    },
    onTypes() { side.refresh(); drawBars(); if (mode === 'latest') drawLatest(); },
    destroy() { watcher.disconnect(); },
  };
}

// ---------------------------------------------------- image timeline viewer

function Viewer(root, typeId, atTime) {
  const type = state.types.get(typeId);
  if (!type) { root.append(h('div', { class: 'empty', text: 'No such type (yet).' })); return { nav: '' }; }

  let frames = [], lo = 0, idx = -1, playing = false, timer = null, settle = null, token = 0, dead = false;
  let live = true, showMap = store.get('map') === '1';
  let hours = +store.get('range', 24), fps = +store.get('fps', 6);
  const zoom = { s: 1, x: 0, y: 0 };
  const cache = new Map();

  const img = h('img', { alt: '', draggable: 'false' });
  const stage = h('div', { class: 'stage' }, img, h('span', { class: 'busy', text: 'loading' }));
  const canvas = h('canvas', { class: 'timeline' });
  const info = h('div', { class: 'info' });
  const playBtn = h('button', { title: 'Play / pause (space)', text: '▶ Play', onclick: () => playing ? pause() : play() });
  const liveBtn = h('button', { title: 'Jump to new frames as they arrive (L)', text: '● Live', onclick: () => { setLive(!live); if (live) show(frames.length - 1); } });
  const mapBtn = h('button', { title: 'Map overlay (M)', text: 'Map', onclick: () => { showMap = !showMap; store.set('map', showMap ? '1' : '0'); cache.clear(); sync(); show(idx); } });
  const original = h('a', { target: '_blank', rel: 'noopener' }, h('button', { text: 'Full resolution ↗' }));
  const select = (value, options, onchange) => {
    const el = h('select', { onchange: e => onchange(+e.target.value) }, options.map(([v, label]) => h('option', { value: v, text: label })));
    el.value = String(value);
    return el;
  };
  const rangeSel = select(hours, [[6, 'Last 6 h'], [12, 'Last 12 h'], [24, 'Last 24 h'], [48, 'Last 48 h'], [0, 'Everything']],
    v => { hours = v; store.set('range', v); setRange(); show(idx); });
  const side = sidebar(type.sec, typeId);

  root.append(h('div', { class: 'section' }, side,
    h('div', { class: 'content' }, h('div', { class: 'viewer' },
      h('div', { class: 'toolbar' },
        h('span', { class: 'title' }, h('small', { text: type.grp }), type.label),
        h('span', { class: 'spacer' }),
        h('button', { title: 'Previous frame (←)', text: '◀', onclick: () => step(-1) }),
        playBtn,
        h('button', { title: 'Next frame (→)', text: '▶', onclick: () => step(1) }),
        select(fps, [[2, '2 fps'], [4, '4 fps'], [6, '6 fps'], [10, '10 fps'], [16, '16 fps']], v => { fps = v; store.set('fps', v); }),
        rangeSel,
        liveBtn, mapBtn, original),
      stage, info, canvas))));

  const path = i => (showMap && frames[i][2]) || frames[i][1];
  const sync = () => {
    liveBtn.classList.toggle('on', live);
    mapBtn.classList.toggle('on', showMap);
    mapBtn.hidden = !frames.some(f => f[2]);
    playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play';
  };
  function setLive(on) { live = on; sync(); }
  function setRange() {
    lo = 0;
    if (hours && frames.length) {
      const from = frames[frames.length - 1][0] - hours * 3600;
      while (lo < frames.length - 1 && frames[lo][0] < from) lo++;
    }
  }

  /** Preview size that matches what is on screen; 0 means the original file. */
  function wanted() {
    const px = Math.min(stage.clientWidth, stage.clientHeight) * zoom.s * (window.devicePixelRatio || 1);
    return px <= 1000 ? 960 : px <= 2300 ? 1920 : 0;
  }

  function show(i, quick) {
    if (!frames.length) return;
    idx = clamp(i, lo, frames.length - 1);
    const frame = frames[idx], p = path(idx), mine = ++token;
    const size = quick ? 960 : wanted();
    const url = size ? preview(p, size) : file(p);
    const loader = new Image();
    stage.classList.add('loading');
    loader.onload = loader.onerror = () => { if (mine === token && !dead) { img.src = url; stage.classList.remove('loading'); } };
    loader.src = url;
    original.href = file(p);
    info.replaceChildren(...[h('b', { text: utc(frame[0], true) }),
      h('span', { class: 'dim', text: new Date(frame[0] * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) + ' local' }),
      h('span', { class: 'dim', text: `frame ${idx - lo + 1} of ${frames.length - lo}` }),
      frame[4] && h('span', { class: 'dim', text: bytes(frame[4]) }),
      frame[3] && h('span', { class: 'note', text: `paired: ${frame[3]}` })].filter(Boolean));
    draw();
    clearTimeout(settle);
    if (quick && !playing) settle = setTimeout(() => show(idx), 280);  // sharpen once scrubbing stops
  }

  function step(d) { pause(); setLive(false); show(idx + d); }

  function warm(i) {
    const p = path(i);
    let im = cache.get(p);
    if (!im) { im = new Image(); im.onload = draw; im.src = preview(p, 960); cache.set(p, im); }
    return im;
  }
  function play() {
    if (frames.length - lo < 2) return;
    playing = true; setLive(false); sync();
    for (let i = lo, n = 0; i < frames.length && n < 240; i++, n++) warm(i);
    advance();
  }
  function advance() {
    if (!playing || dead) return;
    const next = idx >= frames.length - 1 ? lo : idx + 1;
    const im = warm(next);
    const go = () => {
      if (!playing || dead) return;
      show(next, true);
      timer = setTimeout(advance, next === frames.length - 1 ? 1400 : 1000 / fps);
    };
    if (im.complete) go(); else { im.addEventListener('load', go, { once: true }); im.addEventListener('error', go, { once: true }); }
  }
  function pause() { if (!playing) return; playing = false; clearTimeout(timer); sync(); show(idx); }

  // -- timeline strip

  const PAD = 16;
  function scale() {
    const t0 = frames[lo][0], t1 = frames[frames.length - 1][0];
    const w = canvas.clientWidth - 2 * PAD, span = Math.max(1, t1 - t0);
    return { t0, t1, x: t => PAD + (t - t0) / span * w, t: x => t0 + clamp((x - PAD) / w, 0, 1) * span };
  }
  function draw() {
    const w = canvas.clientWidth, ht = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w || !frames.length) return;
    canvas.width = w * dpr; canvas.height = ht * dpr;
    const c = canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement), col = n => css.getPropertyValue(n).trim();
    const s = scale();
    const stepH = [1, 2, 3, 6, 12, 24].find(n => n * 3600 / Math.max(1, s.t1 - s.t0) * (w - 2 * PAD) > 62) || 48;
    c.font = '10px system-ui'; c.textBaseline = 'top';
    for (let t = Math.ceil(s.t0 / (stepH * 3600)) * stepH * 3600; t <= s.t1; t += stepH * 3600) {
      const d = new Date(t * 1000), x = Math.round(s.x(t)) + 0.5, midnight = d.getUTCHours() === 0;
      c.strokeStyle = midnight ? col('--faint') : col('--line');
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ht); c.stroke();
      c.fillStyle = col('--dim');
      c.fillText(midnight ? `${d.getUTCMonth() + 1}/${d.getUTCDate()}` : `${pad(d.getUTCHours())}Z`, x + 4, 3);
    }
    for (let i = lo; i < frames.length; i++) {
      const im = cache.get(path(i));
      c.fillStyle = frames[i][3] ? col('--warn') : im && im.complete ? col('--text') : col('--faint');
      c.fillRect(Math.round(s.x(frames[i][0])) - 1, 20, 2, ht - 28);
    }
    const x = Math.round(s.x(frames[idx][0]));
    c.fillStyle = col('--accent');
    c.fillRect(x - 1.5, 14, 3, ht - 16);
    c.beginPath(); c.moveTo(x - 6, 8); c.lineTo(x + 6, 8); c.lineTo(x, 16); c.fill();
  }
  function seek(e) {
    const t = scale().t(e.clientX - canvas.getBoundingClientRect().left);
    let best = lo;
    for (let i = lo; i < frames.length; i++) if (Math.abs(frames[i][0] - t) < Math.abs(frames[best][0] - t)) best = i;
    pause(); setLive(best === frames.length - 1);
    if (best !== idx) show(best, true);
  }
  canvas.addEventListener('pointerdown', e => { canvas.setPointerCapture(e.pointerId); seek(e); });
  canvas.addEventListener('pointermove', e => { if (e.buttons & 1) seek(e); });

  // -- zoom and pan

  function place() {
    const w = stage.clientWidth, ht = stage.clientHeight;
    zoom.x = clamp(zoom.x, w - w * zoom.s, 0); zoom.y = clamp(zoom.y, ht - ht * zoom.s, 0);
    img.style.transform = zoom.s === 1 ? '' : `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  }
  const sharpen = debounce(() => { if (!playing && !dead) show(idx); }, 250);
  stage.addEventListener('wheel', e => {
    e.preventDefault();
    const r = stage.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
    const s = clamp(zoom.s * Math.exp(-e.deltaY * 0.0016), 1, 16);
    zoom.x = cx - (cx - zoom.x) * s / zoom.s; zoom.y = cy - (cy - zoom.y) * s / zoom.s; zoom.s = s;
    place(); sharpen();
  }, { passive: false });
  let grab = null;
  stage.addEventListener('pointerdown', e => { grab = { x: e.clientX - zoom.x, y: e.clientY - zoom.y }; stage.setPointerCapture(e.pointerId); stage.classList.add('drag'); });
  stage.addEventListener('pointermove', e => { if (grab) { zoom.x = e.clientX - grab.x; zoom.y = e.clientY - grab.y; place(); } });
  stage.addEventListener('pointerup', () => { grab = null; stage.classList.remove('drag'); });
  stage.addEventListener('dblclick', () => { zoom.s = 1; zoom.x = zoom.y = 0; place(); sharpen(); });

  const resized = new ResizeObserver(() => { place(); draw(); });
  resized.observe(stage); resized.observe(canvas);

  function onKey(e) {
    if (/INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
    if (e.key === ' ' && (e.repeat || e.target.tagName === 'BUTTON')) return;  // a focused button handles space itself
    if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === ' ') { e.preventDefault(); playing ? pause() : play(); }
    else if (e.key === 'Home') { pause(); show(lo); }
    else if (e.key === 'End') { pause(); setLive(true); show(frames.length - 1); }
    else if (e.key === 'l') liveBtn.click();
    else if (e.key === 'm' && !mapBtn.hidden) mapBtn.click();
  }
  document.addEventListener('keydown', onKey);

  getJSON(`/api/timeline?type=${encodeURIComponent(typeId)}`).then(d => {
    if (dead) return;
    frames = d.frames;
    let start = frames.length - 1;
    if (atTime && frames.length) {  // arrived from a link to one frame
      for (let i = 0; i < frames.length; i++) if (Math.abs(frames[i][0] - atTime) < Math.abs(frames[start][0] - atTime)) start = i;
      live = start === frames.length - 1;
    }
    setRange();
    if (start < lo) { hours = 0; rangeSel.value = '0'; setRange(); }  // older than the saved range: show everything this once
    sync(); show(start);
  });

  return {
    nav: type.sec,
    onItem(item) {
      side.refresh();
      if (item.ty !== typeId) return;
      const frame = [item.t, item.p, item.m || 0, item.n || 0, item.s || 0];
      let at = frames.length;
      while (at > 0 && frames[at - 1][0] > item.t) at--;
      frames.splice(at, 0, frame);
      if (at <= idx) idx++;
      setRange();
      if (live && !playing) show(frames.length - 1); else draw();
    },
    onTypes() { side.refresh(); },
    onPruned(cutoff, what) {
      if (what === 'cache' && !(frames[0] && frames[0][1].startsWith('d/'))) return;  // only our own colour frames went
      const current = idx >= 0 && frames[idx];
      frames = frames.filter(f => f[0] > cutoff);
      if (!frames.length) { pause(); idx = -1; return; }
      setRange();
      idx = clamp(frames.indexOf(current), lo, frames.length - 1);
      if (frames[idx] !== current) show(idx); else draw();
    },
    destroy() { dead = true; clearTimeout(timer); clearTimeout(settle); resized.disconnect(); document.removeEventListener('keydown', onKey); },
  };
}

// ---------------------------------------------------------------- text feed

function TextView(root, typeId, focusId) {
  const feed = h('div', { class: 'textfeed' });
  const pane = h('div', { class: 'pane scroll textpane' }, feed);
  const sentinel = h('div', { class: 'empty' });
  const side = h('aside', { class: 'types scroll' });
  const typeFilter = h('input', { type: 'search', placeholder: 'Find a product type…', oninput: () => drawSide() });
  const search = h('input', { type: 'search', style: 'max-width:380px',
    placeholder: 'Search the text, product or office: flood warning, AFDSEW, "winter storm"',
    title: 'Every word must appear. Put a phrase in "quotes" to match it exactly. Product and office codes work too.',
    oninput: debounce(() => { query = search.value.trim(); reset(); }, 300) });
  const status = h('span', { class: 'dim', text: 'newest first, live' });
  let query = '', before = focusId ? focusId + 1 : 0, loading = false, done = false, run = 0;
  const active = state.types.get(typeId);

  root.append(h('div', { class: 'section' }, side,
    h('div', { class: 'content' },
      h('div', { class: 'tabs' },
        h('b', { text: active ? active.label : 'All text products' }),
        focusId ? h('a', { class: 'btn', href: textHref(typeId), text: 'Jump to newest ↑' }) : status,
        h('span', { class: 'spacer' }), search),
      pane)));
  pane.append(sentinel);

  function drawSide() {
    const needle = typeFilter.value.trim().toLowerCase();
    const kinds = [...state.types.values()].filter(t => t.kind === 't' && (!needle || t.label.toLowerCase().includes(needle)))
      .sort((a, b) => b.n - a.n);
    const links = kinds.map(t => h('a', { href: `#/text/${encodeURIComponent(t.id)}`, class: t.id === typeId ? 'on' : '' },
      h('span', { text: t.label, title: t.label }), h('small', { text: t.n })));
    const keepFocus = document.activeElement === typeFilter;
    side.replaceChildren(typeFilter, h('a', { href: '#/text', class: typeId ? '' : 'on' }, h('span', { text: 'All text products' })),
      h('h3', { text: 'By type' }), ...links);
    if (keepFocus) typeFilter.focus();
  }
  const refreshSide = debounce(drawSide, 1500);

  function card(item) {
    const type = state.types.get(item.ty);
    const [code, desc] = type ? type.label.split(' · ') : ['', ''];
    const pre = h('pre', { text: item.b ? clean(item.b) : '' });
    // A search hit arrives with the passage that matched, the matching words marked off.
    const marked = item.hit && item.hit.includes('\x02');
    const excerpt = marked && h('div', { class: 'excerpt' },
      item.hit.split(/\x02(.*?)\x03/s).map((part, i) => i % 2 ? h('mark', { text: part }) : part.replace(/\s+/g, ' ')));
    const el = h('article', { class: 'bulletin', 'data-id': item.id },
      h('header', {},
        h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }),
        h('b', { text: item.nm || (type && type.label) || '' }),
        h('span', { class: 'lbl', text: desc || '' }),
        h('time', { text: utc(item.t, true) }), agoEl(item.r),
        h('a', { href: file(item.p), target: '_blank', rel: 'noopener', text: 'raw' })),
      excerpt, pre);
    if (focusId && item.id === focusId) {  // the bulletin a link pointed at: show it whole
      el.classList.add('focus', 'open');
      fetch(file(item.p)).then(r => r.text()).then(t => { pre.textContent = clean(t); });
      return el;
    }
    const expand = h('button', { class: 'more', text: 'Show all', onclick: async () => {
      pre.textContent = clean(await (await fetch(file(item.p))).text());
      el.classList.add('open'); expand.remove();
    } });
    if (item.b == null) {
      fetch(file(item.p), { headers: { Range: 'bytes=0-1399' } }).then(r => r.text()).then(t => { pre.textContent = clean(t); if (t.length >= 1400) el.append(expand); });
    } else if (item.more || item.b.split('\n').length > 18) el.append(expand);
    return el;
  }

  const watcher = new IntersectionObserver(es => es[0].isIntersecting && more(), { root: pane, rootMargin: '800px' });
  async function more() {
    if (loading || done) return;
    loading = true;
    const mine = run;
    const url = `/api/items?kind=t&body=1&limit=30${typeId ? `&type=${encodeURIComponent(typeId)}` : '&sec=text'}`
      + (before ? `&before=${before}` : '') + (query ? `&q=${encodeURIComponent(query)}` : '');
    const d = await getJSON(url);
    if (mine !== run) return;
    for (const item of d.items) feed.append(card(item));
    if (d.cursor != null) before = d.cursor;  // a search hands back where it got to
    else if (d.items.length) before = d.items[d.items.length - 1].id;
    done = !d.more;
    const building = d.index && !d.index.ready
      ? ` The search index is still being built (${d.index.total ? Math.round(100 * d.index.done / d.index.total) : 0}%), so older bulletins may be missing for now.` : '';
    sentinel.textContent = (done ? (feed.children.length ? 'That is everything.' : 'Nothing matches.') : 'Loading…') + building;
    loading = false;
    if (!done && sentinel.getBoundingClientRect().top < Math.min(pane.getBoundingClientRect().bottom, innerHeight) + 800) more();
  }
  function reset() {
    run++; before = 0; done = loading = false;
    status.textContent = query ? 'matches, newest first' : 'newest first, live';
    feed.replaceChildren(); pane.scrollTop = 0;
    more();
  }

  drawSide(); watcher.observe(sentinel); more();

  return {
    nav: 'text',
    onItem(item, type) {
      if (item.k !== 't') return;
      refreshSide();
      if (typeId && item.ty !== typeId) return;
      if (query) return;  // a search is a snapshot; run it again to include what has arrived since
      const el = card(item), reading = pane.scrollTop > 60;
      feed.prepend(el);
      if (reading) pane.scrollTop += el.offsetHeight + 10; else flash(el);  // do not shove text someone is reading
      if (!reading && !loading && feed.children.length > 400) {
        while (feed.children.length > 400) feed.lastChild.remove();
        before = +feed.lastChild.dataset.id; done = false;  // scrolling down continues from the oldest one still shown
        sentinel.textContent = 'Loading…';
      }
    },
    onTypes: refreshSide,
    destroy() { watcher.disconnect(); },
  };
}

// -------------------------------------------------------------- app plumbing

const root = $('#view');

// ------------------------------------------------------------------ DCS
// Data Collection System: messages relayed from river gauges, weather stations, buoys and the like.
// SatDump writes them only with "Parse DCS" on; without DCS files there is no DCS page.

const DCS_SATS = { 1: 'GOES-East', 2: 'GOES-West' };
const dcsType = ty => ty === 'Unknown' ? 'binary' : ty;

/** A line through [x, y] points, drawn to fill its box; the newest point is marked. */
function trend(points) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = x => x1 > x0 ? (x - x0) / (x1 - x0) * 296 + 2 : 150;
  const sy = y => y1 > y0 ? 41 - (y - y0) / (y1 - y0) * 38 : 22;
  svg.setAttribute('viewBox', '0 0 300 44');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.innerHTML = `<polyline points="${points.map(p => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ')}"/>`;
  return svg;
}
function minuteLine(values) {
  return trend(values.map((v, i) => [i, v]).concat([[values.length, values[values.length - 1] || 0]]));
}

/** Each sensor's readings over time, from a platform's messages (newest first).
 *  A message carries several readings per sensor: the newest was taken `age` minutes before it
 *  was sent, and each one before that `interval` minutes earlier. */
function dcsSeries(messages) {
  const sensors = new Map();
  for (const m of messages) {
    for (const [name, interval, age, values] of m.v || []) {
      let points = sensors.get(name);
      if (!points) sensors.set(name, points = new Map());
      values.forEach((raw, i) => {
        const value = parseFloat(raw), t = Math.round((m.t - (age + i * interval) * 60) / 60) * 60;
        if (isFinite(value) && !points.has(t)) points.set(t, value);  // the newest report of a reading wins
      });
    }
  }
  return [...sensors].map(([name, points]) => [name, [...points].sort((a, b) => a[0] - b[0])]);
}

function DcsView(root, initial) {
  let q = '', kind = store.get('dcs:kind', ''), sort = store.get('dcs:sort', 'last');
  let offset = 0, loading = false, done = false, run = 0, selected = null, ticks = 0, shownAll = false;
  const head = h('div', { class: 'dcshead' });
  const search = h('input', { type: 'search', placeholder: 'Find a platform: its address, or text in its latest message',
    oninput: debounce(() => { q = search.value.trim(); reload(); }, 300) });
  const sorter = h('select', { title: 'Order', onchange: () => { sort = sorter.value; store.set('dcs:sort', sort); reload(); } },
    [['last', 'Latest heard'], ['count', 'Most messages'], ['strong', 'Strongest signal'], ['weak', 'Weakest signal'],
      ['missed', 'Most missed slots'], ['address', 'Address']].map(([v, label]) => h('option', { value: v, text: label, selected: v === sort })));
  const chips = h('div', { class: 'filters' });
  const rows = h('div', { class: 'drows' });
  const sentinel = h('div', { class: 'empty' });
  const list = h('div', { class: 'pane scroll dlist' }, rows, sentinel);
  const detail = h('div', { class: 'dcsdetail scroll' });
  const el = h('div', { class: 'dcs' }, head, h('div', { class: 'dcsbody' },
    h('div', { class: 'dcsleft' }, h('div', { class: 'dcsbar' }, search, sorter), chips, list), detail));
  root.append(el);

  function drawChips(count) {
    const chip = (label, value) => h('button', { class: kind === value ? 'on' : '', text: label,
      onclick: () => { kind = value; store.set('dcs:kind', value); reload(); } });
    chips.replaceChildren(chip('All', ''), chip('With readings', 'values'), chip('Text', 'text'), chip('Binary', 'binary'),
      chip('Flagged', 'flagged'), chip('Missed a slot', 'missed'), h('span', { class: 'spacer' }),
      h('span', { class: 'dim', text: count == null ? '' : `${count.toLocaleString()} platform${count === 1 ? '' : 's'}` }));
  }

  const row = p => h('a', { class: 'drow' + (p.a === selected ? ' on' : ''), href: `#/dcs/${p.a}`, 'data-a': p.a },
    h('span', { class: 'addr', text: p.a.toUpperCase() }),
    h('span', { class: 'what', title: p.x || '' }, p.nm && h('b', { text: p.nm }),
      h('span', { class: p.v ? 'vals' : 'raw', text: p.v || p.x || (p.n ? '' : 'never heard, only missed slots') })),
    h('span', { class: 'ty' + (p.f & 255 ? ' warn' : ''), text: p.n ? dcsType(p.ty) : '' }),
    h('span', { class: 'chn', text: `ch ${p.ch ?? '–'} · ${(DCS_SATS[p.sat] || '').replace('GOES-', '') || '?'}` }),
    h('span', { class: 'sig', text: p.sig != null ? `${p.sig.toFixed(1)} dBm` : '' }),
    p.t ? agoEl(p.t) : h('span', { class: 'dim', text: `${p.m} missed` }));

  const query = (limit, from) => `/api/dcs/platforms?limit=${limit}&offset=${from}&sort=${sort}`
    + (kind ? `&kind=${kind}` : '') + (q ? `&q=${encodeURIComponent(q)}` : '');

  async function more() {
    if (loading || done) return;
    loading = true;
    const mine = run;
    let d;
    try { d = await getJSON(query(100, offset)); } catch { d = null; }
    if (mine !== run) return;
    loading = false;
    if (!d) { done = true; sentinel.textContent = 'DCS is not available.'; return; }
    for (const p of d.items) rows.append(row(p));
    offset += d.items.length;
    done = !d.more;
    drawChips(d.count);
    sentinel.textContent = done ? (rows.children.length ? 'That is every platform.' : 'No platform matches.') : 'Loading…';
    if (!done && sentinel.getBoundingClientRect().top < Math.min(list.getBoundingClientRect().bottom, innerHeight) + 400) more();
  }
  function reload() {
    run++; offset = 0; loading = done = false;
    rows.replaceChildren(); list.scrollTop = 0; sentinel.textContent = 'Loading…';
    more();
  }
  /** Bring the top of the list up to date without disturbing someone who has scrolled into it. */
  async function refresh() {
    if (loading || list.scrollTop > 40) return;
    const mine = run;
    let d;
    try { d = await getJSON(query(100, 0)); } catch { return; }
    if (mine !== run || loading || list.scrollTop > 40) return;
    rows.replaceChildren(...d.items.map(row));
    offset = d.items.length; done = !d.more;
    drawChips(d.count);
    sentinel.textContent = done ? (rows.children.length ? 'That is every platform.' : 'No platform matches.') : 'Loading…';
  }
  list.addEventListener('scroll', () => {
    if (sentinel.getBoundingClientRect().top < Math.min(list.getBoundingClientRect().bottom, innerHeight) + 400) more();
  }, { passive: true });

  async function drawHead() {
    let s;
    try { s = await getJSON('/api/dcs/summary'); } catch { s = null; }
    if (!s || !s.store.files) {
      head.replaceChildren(h('div', { class: 'empty', text: s ? 'No DCS files yet. They appear here once SatDump writes them.'
        : 'No DCS data. Turn on "Parse DCS" in SatDump and this page fills in by itself.' }));
      return;
    }
    const hr = s.hour, pct = (a, b) => b ? `${Math.round(100 * a / b)}%` : '–';
    const tile = (label, big, small, ...rest) => h('div', { class: 'kpi still' }, h('label', { text: label }),
      h('b', { text: big }), small && h('small', { text: small }), rest);
    const busiest = Math.max(1, ...hr.channels.map(c => c[1]));
    const behind = s.store.pending > 50;
    head.replaceChildren(
      h('div', { class: 'kpis' },
        tile('Messages', s.rate.toLocaleString(), '/ min', minuteLine(s.minutes),
          h('span', { class: 'dim', text: `${hr.n.toLocaleString()} in the last hour` })),
        tile('Platforms heard', s.heard.hour.toLocaleString(), 'this hour',
          h('div', { class: 'dim', text: `${s.heard.day.toLocaleString()} in the last day` }),
          h('div', { class: 'dim', text: `${s.heard.ever.toLocaleString()} since ${utc(s.first, true)}` })),
        tile('Relayed through', pct(hr.east, hr.n), 'East',
          h('div', { class: 'split', title: `GOES-East ${hr.east.toLocaleString()}, GOES-West ${hr.west.toLocaleString()} in the last hour` },
            h('i', { style: `flex:${hr.east}` }), h('i', { style: `flex:${hr.west}` })),
          h('div', { class: 'dim', text: `${pct(hr.west, hr.n)} West` })),
        tile('Decoded readings', pct(hr.decoded, hr.n), 'of messages',
          h('div', { class: 'dim', text: hr.types.map(([ty, n]) => `${dcsType(ty)} ${pct(n, hr.n)}`).join(' · ') })),
        tile('Problems', hr.bad.toLocaleString(), 'flagged',
          h('div', { class: hr.bad / Math.max(1, hr.n) > 0.02 ? 'note' : 'dim', text: `${pct(hr.bad, hr.n)} of messages in the last hour` }),
          h('div', { class: 'dim', text: `${hr.missed.toLocaleString()} missed slots reported` }))),
      h('div', { class: 'chanline' },
        h('span', { class: 'dim', text: `${hr.channels.length} channels active in the last hour` }),
        h('div', { class: 'chans' }, hr.channels.map(([ch, n]) =>
          h('i', { title: `Channel ${ch}: ${n.toLocaleString()} messages in the last hour`, style: `opacity:${(0.25 + 0.75 * n / busiest).toFixed(2)}` }))),
        h('span', { class: 'spacer' }),
        h('span', { class: behind ? 'note' : 'dim', title: 'GOESview keeps every message this far back in its own database, for the history and charts of each platform.',
          text: `${(s.store.history / 86400).toFixed(s.store.history % 86400 ? 1 : 0)}-day history, ${(s.store.size / 2 ** 20).toFixed(0)} MB`
            + (behind ? ` · catching up, ${s.store.pending.toLocaleString()} files to read` : '') }),
        s.last && h('span', { class: 'dim' }, ' · newest ', agoEl(s.last))));
  }

  function drawDetail(d) {
    const series = dcsSeries(d.messages), asc = [...d.messages].reverse();
    const span = d.messages.length > 1 ? (d.messages[0].t - d.messages[d.messages.length - 1].t) / 3600 : 0;
    const flagNames = f => d.flags.filter((_, bit) => f >> bit & 1).map(name => name.replace(/_/g, ' '));
    const card = (label, points, unit) => {
      const ys = points.map(p => p[1]), latest = ys[ys.length - 1], lo = Math.min(...ys), hi = Math.max(...ys);
      return h('div', { class: 'card', title: label }, h('label', { text: label }),
        h('b', { text: `${+latest.toFixed(2)}${unit || ''}` }),
        points.length > 1 && h('small', { text: lo === hi ? 'steady' : `${+lo.toFixed(2)} to ${+hi.toFixed(2)}` }),
        points.length > 1 && trend(points),
        h('div', { class: 'dim' }, `${points.length} reading${points.length === 1 ? '' : 's'}, newest `, agoEl(points[points.length - 1][0])));
    };
    const radio = (label, key, unit) => {
      const points = asc.filter(m => m[key] != null).map(m => [m.t, m[key]]);
      return points.length ? card(label, points, unit) : null;
    };
    // What SatDump knows about the station, from the tables it downloads (when it has them).
    const k = d.dcp || {}, place = k.lat != null && k.lon != null;
    const about = [
      k.description && ['Station', k.description],
      (k.agency || k.state) && ['Owner', [k.agency, k.state].filter(Boolean).join(', ')],
      place && ['Location', h('a', { href: `https://www.openstreetmap.org/?mlat=${k.lat}&mlon=${k.lon}#map=11/${k.lat}/${k.lon}`,
        target: '_blank', rel: 'noopener', title: 'Open a map at this spot',
        text: `${Math.abs(k.lat).toFixed(4)}° ${k.lat >= 0 ? 'N' : 'S'}, ${Math.abs(k.lon).toFixed(4)}° ${k.lon >= 0 ? 'E' : 'W'} ↗` })],
      k.transmit_interval && ['Transmits', `every ${k.transmit_interval} minutes`],
      k.sensors && ['Sensors', k.sensors],
      (k.manufacturer || k.model) && ['Transmitter', [k.manufacturer, k.model].filter(Boolean).join(' ')],
      k.nmc_descriptor && ['NMC descriptor', k.nmc_descriptor],
    ].filter(Boolean);
    const message = m => h('div', { class: 'msg' },
      h('header', {}, h('b', { text: utc(m.t, true) }), agoEl(m.t),
        h('span', { class: 'dim', text: `${dcsType(m.ty)} · ch ${m.ch ?? '–'} · ${DCS_SATS[m.sat] || '?'}${m.sig != null ? ` · ${m.sig.toFixed(1)} dBm` : ''}` }),
        m.f & 255 ? h('span', { class: 'note', text: flagNames(m.f & 255).join(', ') }) : null),
      m.v && h('div', { class: 'vals' }, m.v.map(([name, , , values]) => h('span', {}, h('i', { text: name }), ` ${values.join('  ')}`))),
      h('pre', { text: m.x || '(empty)' }));
    const shown = shownAll ? d.messages : d.messages.slice(0, 20);
    detail.replaceChildren(...[
      h('a', { class: 'back', href: '#/dcs', text: '← All platforms' }),
      h('h2', {}, h('span', { class: 'addr', text: d.a.toUpperCase() }), k.description ? ` ${k.description}` : d.nm ? ` ${d.nm}` : null),
      h('div', { class: 'sub' },
        d.n ? `Channel ${d.ch ?? '–'} · ${DCS_SATS[d.sat] || 'satellite unknown'}${d.rate ? ` · ${d.rate} baud` : ''} · heard ${d.n.toLocaleString()} time${d.n === 1 ? '' : 's'}, first ${utc(d.first, true)}, last `
          : 'Never heard here. ', d.n ? agoEl(d.t) : null,
        d.m ? h('div', { class: 'note' }, `${d.m.toLocaleString()} missed slot${d.m === 1 ? '' : 's'} reported, the latest `, agoEl(d.mt)) : null,
        d.f & 255 ? h('div', { class: 'note', text: `Latest message flagged: ${flagNames(d.f & 255).join(', ')}` }) : null),
      about.length ? h('div', { class: 'kv2' }, about.flatMap(([label, value]) => [h('span', { text: label }), h('span', {}, value)])) : null,
      series.length ? h('h3', { text: `Readings${span > 1 ? `, last ${span < 48 ? `${Math.round(span)} hours` : `${(span / 24).toFixed(1)} days`}` : ''}` }) : null,
      series.length ? h('div', { class: 'cards' }, series.map(([name, points]) => card(name, points))) : null,
      d.messages.length ? h('h3', { text: 'Radio' }) : null,
      d.messages.length ? h('div', { class: 'cards' }, radio('Signal strength', 'sig', ' dBm'), radio('Phase noise', 'noise', '°'), radio('Frequency offset', 'off', ' Hz')) : null,
      h('h3', { text: d.messages.length ? `Messages, newest first (${d.messages.length.toLocaleString()} in the ${(d.history / 86400).toFixed(d.history % 86400 ? 1 : 0)}-day history)` : 'No messages in the history kept' }),
      shown.map(message),
      shown.length < d.messages.length ? h('button', { class: 'more', text: `Show all ${d.messages.length.toLocaleString()}`,
        onclick: () => { shownAll = true; drawDetail(d); } }) : null,
    ].flat().filter(Boolean));
  }

  async function select(address, quiet) {
    const changed = address !== selected;
    selected = address;
    if (changed) shownAll = false;
    el.classList.toggle('has-sel', !!address);
    for (const r of rows.children) r.classList.toggle('on', r.dataset.a === address);
    if (!address) {
      detail.replaceChildren(h('div', { class: 'empty', text: 'Pick a platform to see its messages, its readings over time and how well it is being received.' }));
      return;
    }
    if (changed && !quiet) detail.replaceChildren(h('div', { class: 'empty', text: 'Loading…' }));
    let d;
    try { d = await getJSON(`/api/dcs/platform?a=${encodeURIComponent(address)}`); } catch { d = null; }
    if (selected !== address) return;
    if (!d) { detail.replaceChildren(h('a', { class: 'back', href: '#/dcs', text: '← All platforms' }), h('div', { class: 'empty', text: `No platform ${address.toUpperCase()} has been heard.` })); return; }
    const at = detail.scrollTop;
    drawDetail(d);
    if (!changed) detail.scrollTop = at;
  }

  drawChips();
  drawHead();
  more();
  select(initial || null);

  return {
    nav: 'dcs',
    select,
    onStats() {  // every ten seconds
      ticks++;
      drawHead();
      refresh();
      if (selected && ticks % 6 === 0) select(selected, true);
    },
  };
}

// --------------------------------------------------------------- System
// Is everything that feeds the viewer healthy, and how has it been: SatDump's signal and lock
// (read from its log), the machine, and GOESview itself.

const SYS_RANGES = [[0.25, '15 minutes'], [1, '1 hour'], [6, '6 hours'], [24, '24 hours'], [168, '7 days'], [720, '30 days']];
const SYS_STATE = { ok: 'Working', warn: 'Needs a look', bad: 'Problem', off: 'Not watched' };
const duration = s => s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;

/** A chart card: one or more lines over the same times. Gaps in the data stay gaps. */
function sysChart(title, times, lines, { unit = '', digits = 1, zero = false, top = null } = {}) {
  const values = lines.flatMap(l => l.values).filter(v => v != null);
  if (!values.length) return null;
  let lo = zero ? 0 : Math.min(...values), hi = top ?? Math.max(...values);
  if (hi <= lo) { hi = lo + (Math.abs(lo) || 1) * 0.1; if (!zero) lo -= (Math.abs(lo) || 1) * 0.1; }
  const W = 600, H = 120, n = times.length;
  const x = i => n > 1 ? i / (n - 1) * W : W / 2, y = v => H - 4 - (v - lo) / (hi - lo) * (H - 8);
  const path = vals => {
    let d = '', pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(clamp(v, lo, hi)).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.innerHTML = [0.25, 0.5, 0.75].map(f => `<line class="grid" x1="0" x2="${W}" y1="${H * f}" y2="${H * f}"/>`).join('')
    + lines.map(l => `<path class="${l.cls || ''}" d="${path(l.values)}"/>`).join('');
  const fmt = v => `${(+v.toFixed(digits)).toLocaleString()}${unit}`;
  const newest = vals => { for (let i = vals.length - 1; i >= 0; i--) if (vals[i] != null) return vals[i]; return null; };
  const now = newest(lines[0].values);
  const when = t => { const d = new Date(t * 1000), day = (times[n - 1] - times[0]) >= 80000 ? `${d.getUTCMonth() + 1}/${d.getUTCDate()} ` : ''; return `${day}${utc(t)}`; };
  return h('div', { class: 'card chart' },
    h('label', { text: title }),
    h('div', { class: 'now' }, now != null ? h('b', { text: fmt(now) }) : h('b', { class: 'dim', text: 'no data now' }),
      lines.length > 1 && h('span', { class: 'keys' }, lines.map(l => h('span', { class: l.cls || '', text: l.name }))),
      h('small', { text: `${fmt(Math.min(...values))} to ${fmt(Math.max(...values))}` })),
    svg,
    h('div', { class: 'axis' }, h('span', { text: when(times[0]) }), h('span', { text: when(times[n - 1]) })));
}

function SystemView(root) {
  let hours = +store.get('sys:hours', '1'), status = null, minute = null, minuteAt = 0, ticks = 0;
  const lights = h('div', { class: 'lights' });
  const ranges = h('div', { class: 'filters' });
  const charts = h('div', { class: 'cards charts' });
  const details = h('div', { class: 'sysdetails' });
  root.append(h('div', { class: 'sys scroll' }, lights, ranges, charts, details));

  function drawRanges() {
    ranges.replaceChildren(h('span', { class: 'dim', text: 'Show the last' }), ...SYS_RANGES.map(([value, label]) =>
      h('button', { class: value === hours ? 'on' : '', text: label,
        onclick: () => { hours = value; store.set('sys:hours', value); minute = null; drawRanges(); load(true); } })));
  }

  function draw() {
    if (!status) return;
    lights.replaceChildren(...status.checks.map(c => h('div', { class: `light ${c.state}`, title: SYS_STATE[c.state] },
      h('i'), h('div', {}, h('b', { text: c.label }), h('span', { text: c.text })))));

    // Up to an hour comes at ten-second steps, as far back as this server has been running;
    // anything longer, and all the counts, come by the minute.
    const recent = status.recent, want = Math.round(hours * 360);
    const cut = r => ({ t: r.t.slice(-want), series: Object.fromEntries(Object.entries(r.series).map(([k, v]) => [k, v.slice(-want)])) });
    const fine = hours <= 1 && recent.t.length >= Math.min(want, 60) * 0.8 ? cut(recent) : null, slow = minute;
    const pick = key => fine && fine.series[key] && fine.series[key].some(v => v != null) ? [fine.t, fine.series[key]]
      : slow && slow.series[key] ? [slow.t, slow.series[key]] : null;
    const chart = (title, keys, opts) => {
      const got = keys.map(([key, name, cls]) => ({ key, name, cls, data: pick(key) })).filter(l => l.data);
      if (!got.length) return null;
      const times = got[0].data[0];
      return sysChart(title, times, got.filter(l => l.data[0] === times).map(l => ({ name: l.name, cls: l.cls, values: l.data[1] })), opts);
    };
    charts.replaceChildren(...[
      chart('Signal to noise', [['snr', 'now'], ['peak', 'peak', 'dimline']], { unit: ' dB', digits: 2 }),
      chart('Viterbi error rate', [['ber', 'average'], ['ber_max', 'worst', 'dimline']], { digits: 3, zero: true }),
      chart('Time in lock', [['sync', 'locked']], { unit: '%', digits: 0, zero: true, top: 100 }),
      chart('Files written by SatDump, per minute', [['written', 'written'], ['lrit', 'received', 'dimline']], { zero: true }),
      chart('Arrivals in GOESview, per minute', [['text', 'text'], ['imagery', 'imagery', 'line2'], ['weather', 'weather', 'line3']], { zero: true }),
      chart('DCS messages per minute', [['dcs', 'messages']], { digits: 0, zero: true }),
      chart('SatDump warnings and errors, per minute', [['warn', 'warnings', 'line3'], ['err', 'errors', 'line4']], { zero: true }),
      chart('Processor', [['cpu', 'machine'], ['sd_cpu', 'SatDump', 'line2'], ['gv_cpu', 'GOESview', 'line3']], { unit: '%', digits: 0, zero: true }),
      chart('Load', [['load', 'load']], { digits: 2, zero: true }),
      chart('Temperature', [['temp', 'hottest sensor']], { unit: ' °C' }),
      chart('Memory in use', [['mem', 'used']], { unit: '%', digits: 0, zero: true, top: 100 }),
      chart('Disk in use', [['disk', 'used']], { unit: '%', digits: 1, zero: true, top: 100 }),
      chart('Battery', [['battery', 'charge']], { unit: '%', digits: 0, zero: true, top: 100 }),
    ].filter(Boolean));
    if (!charts.children.length) charts.append(h('div', { class: 'empty', text: 'Gathering the first readings…' }));

    const sd = status.satdump, host = status.host, gv = status.goesview, live = status.live, proc = host.satdump;
    const rows = pairs => h('div', { class: 'kv2' }, pairs.filter(p => p && p[1] != null && p[1] !== '').flatMap(([k, v]) => [h('span', { text: k }), h('span', {}, v)]));
    const gb = n => `${(n / 2 ** 30).toFixed(1)} GB`, mb = n => `${Math.round(n / 2 ** 20)} MB`;
    details.replaceChildren(
      h('div', { class: 'panel' }, h('h3', { text: 'Receiver' }), rows([
        ['SatDump', sd.version || (sd.log ? 'version not in the log' : 'not found on this machine')],
        ['Radio', sd.device],
        ['Frequency', sd.frequency && `${(sd.frequency / 1e6).toFixed(3)} MHz`],
        ['Sample rate', sd.samplerate && `${(sd.samplerate / 1e6).toFixed(3)} Msps`],
        ['Signal', live.snr != null && `${live.snr.toFixed(2)} dB, peak ${live.peak.toFixed(2)} dB`],
        ['Viterbi', live.ber != null && `${live.viterbi ? 'in sync' : 'NOT in sync'}, error rate ${live.ber.toFixed(4)}`],
        ['Deframer', live.deframer != null && (live.deframer ? 'in sync' : 'NOT in sync')],
        ['Last report', live.t && agoEl(live.t)],
        ['Pipeline started', sd.pipeline && agoEl(sd.pipeline)],
        ['Writing to', sd.output],
        ['Parse DCS', sd.params && sd.params.parse_dcs],
        ['Process', proc && `pid ${proc.pid}, ${proc.cpu ?? '–'}% of a core, ${mb(proc.rss)}, up ${duration(proc.up || 0)}`],
        ['Log', sd.log],
      ])),
      h('div', { class: 'panel' }, h('h3', { text: 'This machine and GOESview' }), rows([
        ['Processor', host.cpu != null && `${host.cpu.toFixed(0)}% busy, load ${host.load.toFixed(2)} on ${host.cores} cores`],
        ['Memory', host.mem != null && `${gb(host.mem_total - host.mem_free)} used of ${gb(host.mem_total)}`],
        ['Temperature', host.temp != null && `${host.temp.toFixed(1)} °C`],
        ['Disk', host.disk != null && `${gb(host.disk_total - host.disk_free)} used of ${gb(host.disk_total)} (${host.disk.toFixed(1)}%)`],
        ['Power', host.battery != null && `battery ${host.battery}%${host.power ? `, ${host.power.toLowerCase()}` : ''}`],
        ['Up', host.up && duration(host.up)],
        ['GOESview', `up ${duration(gv.up)}, ${gv.cpu ?? '–'}% of a core, ${gv.rss ? mb(gv.rss) : '–'}, Python ${gv.python}`],
        ['Catalogue', `${gv.items.toLocaleString()} items`],
        ['Viewers connected', String(gv.viewers)],
        ['DCS files waiting', gv.dcs_backlog != null && gv.dcs_backlog.toLocaleString()],
      ])),
      h('div', { class: 'panel' }, h('h3', { text: 'SatDump warnings and errors, newest first' }),
        status.messages.length ? status.messages.map(m => h('div', { class: `sysmsg ${m.level === 'W' ? 'warn' : 'bad'}` },
          h('span', { class: 'dim' }, utc(m.t, true), ' · ', agoEl(m.t)), m.n > 1 && h('span', { class: 'dim', text: ` · ${m.n} times` }),
          h('div', { text: m.text }))) : h('div', { class: 'dim', text: sd.log ? 'None since SatDump started.' : 'SatDump is not watched from this machine.' })));
  }

  async function load(now) {
    try { status = await getJSON('/api/monitor'); } catch { status = null; }
    if (!status) { lights.replaceChildren(h('div', { class: 'empty', text: 'The system monitor is switched off on this server.' })); return; }
    if (now || !minute || Date.now() - minuteAt > 55000) {
      try { minute = await getJSON(`/api/monitor/history?hours=${hours}`); minuteAt = Date.now(); } catch { /* keep the old one */ }
    }
    draw();
  }

  drawRanges();
  load(true);
  return { nav: 'system', onStats() { ticks++; load(false); } };
}

function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'dcs' && state.view && state.view.nav === 'dcs') {  // picking a platform keeps the list where it is
    popup.close();
    state.view.select(parts[1] ? parts[1].toLowerCase() : null);
    return;
  }
  if (state.view && state.view.destroy) state.view.destroy();
  root.replaceChildren();
  popup.close();
  const [page, arg, extra] = parts;
  if (page === 'type' && state.types.get(arg) && state.types.get(arg).kind === 't') { location.replace(`#/text/${encodeURIComponent(arg)}`); return; }
  if (page === 'imagery' || page === 'weather') state.view = SectionView(root, page, arg === 'latest' ? 'latest' : 'feed');
  else if (page === 'type') state.view = Viewer(root, arg, +extra || 0);
  else if (page === 'text') state.view = TextView(root, arg || null, +extra || 0);
  else if (page === 'dcs') state.view = DcsView(root, arg ? arg.toLowerCase() : null);
  else if (page === 'system') state.view = SystemView(root);
  else state.view = LiveView(root);
  root.classList.toggle('flow', state.view.nav === 'live');  // the Live page stacks and scrolls as a whole in a narrow window
  for (const a of document.querySelectorAll('#nav a')) a.classList.toggle('on', a.dataset.nav === state.view.nav);
}

async function loadTypes() {
  const d = await getJSON('/api/types');
  state.skew = d.now - Date.now() / 1000;
  state.lut = d.lut;
  state.types = new Map(d.types.map(t => [t.id, t]));
}
const reloadTypes = debounce(async () => { await loadTypes(); if (state.view && state.view.onTypes) state.view.onTypes(); }, 400);

function connect() {
  const es = new EventSource('/api/events');
  const dot = $('#conn');
  es.onopen = () => { dot.classList.add('ok'); dot.title = 'Live'; };
  es.onerror = () => { dot.classList.remove('ok'); dot.title = 'Reconnecting…'; };
  es.addEventListener('item', e => {
    const item = JSON.parse(e.data), type = state.types.get(item.ty);
    if (!type) {  // first item of a new type: fetch the type list, then deliver the item as usual
      loadTypes().then(() => {
        const fresh = state.types.get(item.ty);
        if (state.view && state.view.onTypes) state.view.onTypes();
        if (fresh && state.view && state.view.onItem) state.view.onItem(item, fresh);
      });
      return;
    }
    type.n++;
    if (!type.last || item.t >= type.last.t) type.last = item;
    if (state.view && state.view.onItem) state.view.onItem(item, type);
  });
  es.addEventListener('pruned', e => {  // old data was deleted on the server
    const { cutoff, what } = JSON.parse(e.data);
    if (state.view && state.view.onPruned) state.view.onPruned(cutoff, what);
    reloadTypes();
  });
  es.addEventListener('stats', e => applyStats(JSON.parse(e.data)));
}

function applyStats(s) {
  state.stats = s;
  const hour = Object.values(s.rate).flat().reduce((a, b) => a + b, 0);
  $('#rate').textContent = `${hour} items in the last hour`;
  $('#nav a[data-nav="dcs"]').hidden = !s.dcs;  // DCS shows only while SatDump is writing DCS files
  const sys = $('#nav a[data-nav="system"]');   // the System link carries a dot when something needs a look
  sys.hidden = !s.health;
  sys.classList.toggle('warn', !!s.health && s.health.state === 'warn');
  sys.classList.toggle('bad', !!s.health && s.health.state === 'bad');
  sys.title = s.health ? s.health.text : '';
  if (state.view && state.view.onStats) state.view.onStats(s);
}

function fullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}
document.addEventListener('fullscreenchange', () => document.body.classList.toggle('kiosk', !!document.fullscreenElement));
$('#full').addEventListener('click', fullscreen);
// Header links always clear the pop-up, as Esc does. A link to the page already showing
// (GOESview or Live while on the Live page) changes nothing in the address, so the browser
// would do nothing: treat it as "show me this page afresh", back in its starting state.
$('#bar').addEventListener('click', e => {
  const link = e.target.closest('a');
  if (!link || !plainClick(e)) return;
  popup.close();
  if (link.getAttribute('href') === (location.hash || '#/')) { e.preventDefault(); route(); }
});
document.addEventListener('keydown', e => { if (e.key === 'f' && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName) && !e.metaKey && !e.ctrlKey) fullscreen(); });

loadTypes().then(() => { route(); connect(); addEventListener('hashchange', route); getJSON('/api/stats').then(applyStats).catch(() => {}); })
  .catch(err => root.append(h('div', { class: 'empty', text: `Cannot reach the server: ${err.message}` })));
