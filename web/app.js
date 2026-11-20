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
  return h('a', { class: 'tile', href: typeHref(item.ty, item.t), 'data-ty': item.ty },
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
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });  // a click on the backdrop
  dlg.addEventListener('close', () => { if (onClose) onClose(); });
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
      const img = h('img', { alt: '', src: preview(item.p, 960) });  // already cached; sharpen when ready
      const sharp = new Image();
      sharp.onload = () => { if (mine === token) img.src = sharp.src; };
      sharp.src = preview(item.p, 1920);
      body.append(img);
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
          h('button', { title: 'Close (Esc)', text: '✕', onclick: () => dlg.close() }))),
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
      onShow = opts.onShow || null;
      draw();
      onClose = opts.onClose || null;
      place();
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
      const { free, total, cache, satdump, dry, last, scope, archive } = s.storage, gb = n => (n / 2 ** 30).toFixed(n < 100 * 2 ** 30 ? 1 : 0);
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
        : dry ? h('span', { class: 'safe', text: 'dry run: nothing is deleted' })
          : h('span', { class: 'note', text: 'older data is deleted' });
      // Folded, the heading carries the SatDump status; the bar and free space stay below it either way.
      const brief = [deleting && h('span', { class: 'note', text: 'SatDump data is pruned' }),
        satdump && dry && h('span', { class: 'safe', text: 'SatDump pruning: dry run' })];
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
          h('span', {}, satdump ? `${satdump}; ` : '', verdict, reach, sweep(last && last.satdump)))));
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
          if (row) row.scrollIntoView({ block: 'nearest' });
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

const DIGEST = 8;  // items per group on the as-received overview

function SectionView(root, section, mode) {
  const side = sidebar(section);
  const tabs = h('div', { class: 'tabs' });
  const filters = h('div', { class: 'filters' });
  const pane = h('div', { class: 'pane scroll' });
  root.append(h('div', { class: 'section' }, side, h('div', { class: 'content' }, tabs, filters, pane)));

  let onlyGroup = store.get(`group:${section}`, ''), show = store.get(`show:${section}`, 'all');
  let before = 0, loading = false, done = false, run = 0;
  const grid = h('div', { class: 'grid' });
  const sentinel = h('div', { class: 'empty' });
  const watcher = new IntersectionObserver(es => es[0].isIntersecting && more(), { root: pane, rootMargin: '600px' });
  const live = mode === 'feed' ? liveSwitch(pane, () => redraw())
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

  function redraw() {
    live.reset();
    drawBars();
    run++; before = 0; done = loading = false;
    watcher.unobserve(sentinel);
    pane.replaceChildren();
    if (mode === 'latest') drawLatest();
    else if (overview()) drawDigest();
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

  /** As received, but each group keeps its own block rather than one mixed stream.
   *  Each block starts with its newest few and can be extended downwards. */
  function drawDigest() {
    const mine = run;
    for (const g of groups()) {
      if (!kinds().some(t => t.grp === g && passes(t))) continue;
      const box = h('div', { class: 'grid', 'data-grp': g });
      const older = h('button', { class: 'more', text: 'Show older', hidden: true });
      let cursor = 0;
      const load = async count => {
        older.disabled = true;
        const d = await getJSON(`/api/items?sec=${section}&kind=i&limit=${count}&group=${encodeURIComponent(g)}${rawQuery()}`
          + (cursor ? `&before=${cursor}` : ''));
        if (mine !== run) return;
        for (const item of d.items) box.append(tile(item));
        if (d.items.length) cursor = d.items[d.items.length - 1].id;
        older.hidden = !d.more;
        older.disabled = false;
      };
      older.addEventListener('click', () => load(24));
      pane.append(heading(g, 'open this group'), box, older);
      load(DIGEST);
    }
  }

  function heading(group, more) {
    return h('h3', {}, group, overview() && h('button', { class: 'link', text: more || 'open', onclick: () => pickGroup(group) }));
  }

  async function more() {
    if (loading || done) return;
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
    if (!done && sentinel.getBoundingClientRect().top < pane.getBoundingClientRect().bottom + 600) more();
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
        live.missed();  // paused: count it, move nothing
      } else if (overview()) {
        const box = [...pane.querySelectorAll('.grid')].find(e => e.dataset.grp === type.grp);
        if (!box) return redraw();
        const el = tile(item); box.prepend(el); flash(el);
      } else { const el = tile(item); grid.prepend(el); flash(el); }
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
    const el = h('article', { class: 'bulletin' },
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
    if (!done && sentinel.getBoundingClientRect().top < pane.getBoundingClientRect().bottom + 800) more();
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
      while (!reading && feed.children.length > 400) feed.lastChild.remove();
    },
    onTypes: refreshSide,
    destroy() { watcher.disconnect(); },
  };
}

// -------------------------------------------------------------- app plumbing

const root = $('#view');

function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (state.view && state.view.destroy) state.view.destroy();
  root.replaceChildren();
  popup.close();
  const [page, arg, extra] = parts;
  if (page === 'type' && state.types.get(arg) && state.types.get(arg).kind === 't') { location.replace(`#/text/${encodeURIComponent(arg)}`); return; }
  if (page === 'imagery' || page === 'weather') state.view = SectionView(root, page, arg === 'latest' ? 'latest' : 'feed');
  else if (page === 'type') state.view = Viewer(root, arg, +extra || 0);
  else if (page === 'text') state.view = TextView(root, arg || null, +extra || 0);
  else state.view = LiveView(root);
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
  es.addEventListener('stats', e => {
    const s = state.stats = JSON.parse(e.data);
    const hour = Object.values(s.rate).flat().reduce((a, b) => a + b, 0);
    $('#rate').textContent = `${hour} items in the last hour`;
    if (state.view && state.view.onStats) state.view.onStats(s);
  });
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

loadTypes().then(() => { route(); connect(); addEventListener('hashchange', route); })
  .catch(err => root.append(h('div', { class: 'empty', text: `Cannot reach the server: ${err.message}` })));
