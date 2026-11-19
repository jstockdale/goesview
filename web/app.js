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

// -------------------------------------------------------------------- pop-up

/** Shows one item over the current page, with a way through to its own page. */
const popup = (() => {
  const dlg = h('dialog', { class: 'popup' });
  document.body.append(dlg);
  let list = [], at = 0, token = 0;
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });  // a click on the backdrop
  document.addEventListener('keydown', e => {
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
    dlg.replaceChildren(
      h('header', {},
        h('div', { class: 'what' },
          isImage ? h('b', { text: type ? type.label : '' })
            : [h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }), h('b', { text: item.nm || '' })],
          h('span', { class: 'grp', text: isImage ? (type ? type.grp : '') : (desc || '') }),
          h('span', { class: 'dim', text: utc(item.t, true) }), agoEl(item.r),
          item.n && h('span', { class: 'note', text: item.n })),
        h('div', { class: 'acts' },
          many && h('button', { title: 'Previous (←)', text: '◀', onclick: () => step(-1) }),
          many && h('span', { class: 'dim', text: `${at + 1} / ${list.length}` }),
          many && h('button', { title: 'Next (→)', text: '▶', onclick: () => step(1) }),
          h('a', { class: 'btn go', href: isImage ? typeHref(item.ty, item.t) : textHref(item.ty, item.id),
            text: isImage ? 'Open in timeline →' : `See it among ${code || 'similar'} bulletins →` }),
          h('a', { class: 'btn', href: file(item.p), target: '_blank', rel: 'noopener', text: isImage ? 'Full resolution ↗' : 'Raw ↗' }),
          h('button', { title: 'Close (Esc)', text: '✕', onclick: () => dlg.close() }))),
      body);
  }

  return {
    open(items, index) { list = items; at = Math.max(0, index); draw(); if (!dlg.open) dlg.showModal(); },
    close() { if (dlg.open) dlg.close(); },
  };
})();

// ---------------------------------------------------------------- live view

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
  const tele = h('div', { class: 'panel' });
  const rows = h('div', { class: 'rows' });
  root.append(h('div', { class: 'live' }, hero, wall,
    h('aside', { class: 'side' }, tele, h('div', { class: 'panel ticker' }, h('h3', { text: 'Latest bulletins' }), rows))));

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

  function drawWall(freshType) {
    const lead = heroType();
    const latest = [...state.types.values()]
      .filter(t => t.kind === 'i' && t.last && t !== lead)
      .sort((a, b) => b.last.r - a.last.r).slice(0, 12);
    const items = latest.map(t => t.last);
    wall.replaceChildren(...latest.map((t, i) => {
      const el = tile(t.last, '(max-width: 1100px) 33vw, 20vw');
      el.addEventListener('click', e => { if (plainClick(e)) { e.preventDefault(); popup.open(items, i); } });
      el.append(h('button', { class: 'pin', title: 'Show this type large', text: '★',
        onclick: e => { e.preventDefault(); e.stopPropagation(); store.set('hero', t.id); delete hero.dataset.p; drawHero(); drawWall(); } }));
      if (t.id === freshType) flash(el);
      return el;
    }));
  }

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
      const href = sec === 'text' ? '#/text' : `#/${sec}/feed`;
      return h('a', { class: 'kpi', href, title: `Show ${label.toLowerCase()} items as received` }, h('label', { text: label }),
        h('b', { text: rate.reduce((a, b) => a + b, 0) }), h('small', { text: '/ hr' }),
        spark(rate.length ? rate : new Array(60).fill(0)),
        s.last[sec] ? agoEl(s.last[sec]) : h('span', { class: 'dim', text: 'nothing yet' }));
    };
    const parts = [h('h3', { text: 'Reception, last hour' }),
      h('div', { class: 'kpis' }, kpi('Imagery', 'imagery'), kpi('Weather', 'weather'), kpi('Text', 'text'))];
    for (const scan of s.scans) {
      const all = [...new Set(scan.slots.flatMap(x => x.b))].sort((a, b) => a - b);
      if (!all.length) continue;
      const grid = h('div', { class: 'bands', style: `grid-template-columns: 22px repeat(${scan.slots.length}, minmax(0, 1fr))` });
      const odd = scan.slots.map(x => x.b.length * 2 < all.length);  // a band filed apart from its scan
      grid.append(h('span'));
      scan.slots.forEach((x, i) => grid.append(h('span', { class: 'hd' + (odd[i] ? ' odd' : ''), title: utc(x.t, true),
        text: odd[i] ? '·' : (new Date(x.t * 1000).getUTCMinutes() ? '' : pad(new Date(x.t * 1000).getUTCHours())) })));
      for (const band of all) {
        grid.append(h('span', { text: band }));
        scan.slots.forEach((x, i) => grid.append(h('i', { class: x.b.includes(band) ? (odd[i] ? 'odd' : 'on') : '',
          title: `band ${band}, ${utc(x.t, true)}` })));
      }
      parts.push(h('h3', { text: `${scan.g}: bands per scan` }), grid,
        h('div', { class: 'legend' }, 'UTC hour', h('i', { style: 'background:var(--good)' }), 'received',
          h('i', { style: 'background:var(--warn)' }), 'filed under an earlier scan'));
    }
    if (s.storage) {
      const { free, total, policy, last } = s.storage, gb = n => (n / 2 ** 30).toFixed(n < 100 * 2 ** 30 ? 1 : 0);
      const used = 1 - free / total;
      parts.push(h('h3', { text: 'Storage' }),
        h('div', { class: 'meter', title: `${Math.round(used * 100)}% of the disk used` },
          h('i', { class: used > 0.9 ? 'full' : '', style: `width:${(used * 100).toFixed(1)}%` })),
        h('div', { class: 'legend' }, `${gb(free)} GB free of ${gb(total)} GB · `,
          policy || 'no retention limit set',
          last && h('div', {}, `${last.dry ? 'Would remove' : 'Last removed'} ${last.what} · `, agoEl(last.t))));
    }
    if (s.satdump) {
      const keys = Object.keys(s.satdump);
      const wanted = keys.filter(k => /snr|ber|lock|freq|sync|deframer|viterbi|rs_avg/i.test(k));
      const kv = h('div', { class: 'kv' });
      for (const k of (wanted.length ? wanted : keys).slice(0, 12)) kv.append(h('span', { text: k, title: k }), h('span', { text: s.satdump[k] }));
      parts.push(h('h3', { text: 'SatDump' }), kv);
    }
    tele.replaceChildren(...parts);
  }

  const ticker = [];  // the bulletins on screen, newest first
  function bulletin(item, body) {
    const type = state.types.get(item.ty);
    const code = type ? type.label.slice(0, 3) : '';
    return h('a', { class: 'row', href: textHref(item.ty, item.id),
      onclick: e => { if (plainClick(e)) { e.preventDefault(); popup.open(ticker.slice(), ticker.indexOf(item)); } } },
      h('time', { text: utc(item.t) }),
      h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }),
      h('span', { class: 'nm', text: item.nm || (type && type.label) || '' }),
      h('span', { class: 'line', text: headline(body) }));
  }

  getJSON('/api/items?sec=text&kind=t&limit=40&body=1').then(d => {
    ticker.splice(0, ticker.length, ...d.items);
    rows.replaceChildren(...d.items.map(i => bulletin(i, i.b)));
  });
  drawHero(); drawWall(); drawStats(state.stats);

  return {
    nav: 'live',
    onItem(item, type) {
      if (item.k === 'i') { drawHero(); drawWall(type.id); return; }
      fetch(file(item.p), { headers: { Range: 'bytes=0-900' } }).then(r => r.text()).then(body => {
        const el = bulletin(item, body);
        ticker.unshift(item); rows.prepend(el); flash(el);
        while (rows.children.length > 60) { rows.lastChild.remove(); ticker.pop(); }
      }).catch(() => {});
    },
    onTypes() { drawHero(); drawWall(); },
    onStats: drawStats,
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
      h('a', { href: `#/${section}`, class: mode === 'latest' ? 'on' : '', text: 'Latest of each type' }),
      h('a', { href: `#/${section}/feed`, class: mode === 'feed' ? 'on' : '', text: 'Everything as received' }),
      mixed() && h('span', { class: 'spacer' }),
      mixed() && chip('Processed and raw', 'all'), mixed() && chip('Processed', 'processed'), mixed() && chip('Raw', 'raw'),
    ].filter(Boolean));
  }

  function redraw() {
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

  /** As received, but each group keeps its own block rather than one mixed stream. */
  function drawDigest() {
    const mine = run;
    for (const g of groups()) {
      if (!kinds().some(t => t.grp === g && passes(t))) continue;
      const box = h('div', { class: 'grid', 'data-grp': g });
      pane.append(heading(g, 'all received'), box);
      getJSON(`/api/items?sec=${section}&kind=i&limit=${DIGEST}&group=${encodeURIComponent(g)}${rawQuery()}`)
        .then(d => { if (mine === run) box.replaceChildren(...d.items.map(i => tile(i))); });
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
      } else if (overview()) {
        const box = [...pane.querySelectorAll('.grid')].find(e => e.dataset.grp === type.grp);
        if (!box) return redraw();
        const el = tile(item); box.prepend(el); flash(el);
        while (box.children.length > DIGEST) box.lastChild.remove();
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
    onPruned(cutoff) {
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
  const pane = h('div', { class: 'pane scroll' }, feed);
  const sentinel = h('div', { class: 'empty' });
  const side = h('aside', { class: 'types scroll' });
  const typeFilter = h('input', { type: 'search', placeholder: 'Find a product type…', oninput: () => drawSide() });
  const search = h('input', { type: 'search', style: 'max-width:340px',
    placeholder: 'Filter by product or office, e.g. AFDSEW or KSEW',
    oninput: debounce(() => { query = search.value.trim(); reset(); }, 250) });
  let query = '', before = focusId ? focusId + 1 : 0, loading = false, done = false, run = 0;
  const active = state.types.get(typeId);

  root.append(h('div', { class: 'section' }, side,
    h('div', { class: 'content' },
      h('div', { class: 'tabs' },
        h('b', { text: active ? active.label : 'All text products' }),
        focusId ? h('a', { class: 'btn', href: textHref(typeId), text: 'Jump to newest ↑' })
          : h('span', { class: 'dim', text: 'newest first, live' }),
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
    const el = h('article', { class: 'bulletin' },
      h('header', {},
        h('span', { class: 'code' + (WARN_CODES.has(code) ? ' warn' : ''), text: code }),
        h('b', { text: item.nm || (type && type.label) || '' }),
        h('span', { class: 'lbl', text: desc || '' }),
        h('time', { text: utc(item.t, true) }), agoEl(item.r),
        h('a', { href: file(item.p), target: '_blank', rel: 'noopener', text: 'raw' })),
      pre);
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
    if (d.items.length) before = d.items[d.items.length - 1].id;
    done = !d.more;
    sentinel.textContent = done ? (feed.children.length ? 'That is everything.' : 'Nothing matches.') : 'Loading…';
    loading = false;
  }
  function reset() { run++; before = 0; done = loading = false; feed.replaceChildren(); more(); }

  drawSide(); watcher.observe(sentinel); more();

  return {
    nav: 'text',
    onItem(item, type) {
      if (item.k !== 't') return;
      refreshSide();
      if (typeId && item.ty !== typeId) return;
      if (query && !(item.nm || type.label).toLowerCase().includes(query.toLowerCase())) return;
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
  if (page === 'imagery' || page === 'weather') state.view = SectionView(root, page, arg === 'feed' ? 'feed' : 'latest');
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
    if (!type) return reloadTypes();  // first item of a new type
    type.n++;
    if (!type.last || item.t >= type.last.t) type.last = item;
    if (state.view && state.view.onItem) state.view.onItem(item, type);
  });
  es.addEventListener('pruned', e => {  // old data was deleted on the server
    const { cutoff } = JSON.parse(e.data);
    if (state.view && state.view.onPruned) state.view.onPruned(cutoff);
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
document.addEventListener('keydown', e => { if (e.key === 'f' && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName) && !e.metaKey && !e.ctrlKey) fullscreen(); });

loadTypes().then(() => { route(); connect(); addEventListener('hashchange', route); })
  .catch(err => root.append(h('div', { class: 'empty', text: `Cannot reach the server: ${err.message}` })));
