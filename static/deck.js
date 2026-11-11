// devicedeck preview page: every shown device side by side, view only.
// The only controls: the gear (device list: show/hide, rename, move up/down) and dragging a
// device by its label to reorder. Both save through POST /api/config. Nothing here sends input
// to a device: iPhone frames ignore pointer events (CSS) and block every event inside
// (viewer_patch.py), and the server passes only stream paths to the iPhones.
(() => {
  'use strict';
  const EDGE = 8, GAP = 16, LABEL_H = 34, BEZEL = 7, POLL_MS = 3000, STALL_MS = 8000;
  const DEFAULT_RATIO = {ios: 1170 / 2532, simulator: 1179 / 2556, android: 1080 / 2400};
  const KIND = {ios: 'iOS device', simulator: 'Simulator', android: 'Android'};

  const deck = document.getElementById('deck');
  const empty = document.getElementById('empty');
  const mark = document.getElementById('drop-mark');
  const gear = document.getElementById('gear');
  const panel = document.getElementById('panel');
  const list = document.getElementById('list');
  const status = document.getElementById('status');

  let devices = [];          // full list from /api/devices, in the user's order
  let panelsPort = null;
  let version = 0;           // bumped by every save, so an older poll does not undo it
  let loaded = false;
  const tiles = new Map();   // key -> tile
  const keyOf = (d) => d.kind + ':' + d.id;
  const shown = () => devices.filter((d) => d.connected && !d.hidden);
  const isImage = (d) => d.kind !== 'ios' || /\.mjpg$/.test(d.stream || '');

  // ----- tiles

  function makeTile(d) {
    const el = document.createElement('section');
    el.className = 'tile ' + d.kind;
    const label = document.createElement('div');
    label.className = 'label';
    label.draggable = true;
    label.title = 'Drag to move this device';
    const name = document.createElement('span');
    name.className = 'name';
    const meta = document.createElement('span');
    meta.className = 'meta';
    label.append(name, meta);
    const screen = document.createElement('div');
    screen.className = 'screen';
    let video;
    if (!isImage(d)) {
      video = document.createElement('iframe');
      video.setAttribute('tabindex', '-1');
      video.setAttribute('scrolling', 'no');
      video.title = d.name;
    } else {
      video = document.createElement('img');
      video.alt = '';
      video.draggable = false;
    }
    video.className = 'video';
    const off = document.createElement('div');
    off.className = 'off';
    off.textContent = 'connecting…';
    screen.append(video, off);
    el.append(label, screen);
    const tile = {key: keyOf(d), kind: d.kind, el, label, name, meta, screen, video, off,
                  frame: null, ratio: null, src: null, image: isImage(d)};
    if (tile.image) {
      const retry = () => {
        tile.off.hidden = false;
        setTimeout(() => reconnect(tile), 3000);
      };
      video.addEventListener('error', retry);
    }
    wireDrag(tile);
    deck.append(el);
    tiles.set(tile.key, tile);
    return tile;
  }

  function reconnect(tile) {
    if (tiles.get(tile.key) !== tile || !tile.src) return;
    tile.kicked = Date.now();
    tile.video.src = tile.src + '?t=' + Date.now();
  }

  function streamUrl(d) {
    if (!d.stream) return null;
    if (d.kind === 'ios') return d.stream;
    if (!panelsPort) return null;
    return location.protocol + '//' + location.hostname + ':' + panelsPort + d.stream;
  }

  function updateTile(tile, d, index) {
    tile.el.style.order = String(index);
    if (tile.name.textContent !== d.name) tile.name.textContent = d.name;
    if (!tile.image && tile.video.title !== d.name) tile.video.title = d.name;
    if (tile.meta.textContent !== (d.meta || '')) tile.meta.textContent = d.meta || '';
    tile.label.setAttribute('aria-label', d.name + (d.meta ? ', ' + d.meta : '') + '. Drag to move.');
    const src = streamUrl(d);
    if (src !== tile.src) {
      tile.src = src;
      if (!src) tile.video.removeAttribute('src');
      else if (!tile.image) tile.video.src = src;
      else reconnect(tile);
    } else if (src && tile.image && !d.live && Date.now() - (tile.kicked || 0) > STALL_MS) {
      reconnect(tile);
    }
    tile.off.hidden = !!d.live;
    tile.off.textContent = d.error || 'connecting…';
    if (d.frame && !tile.frame) setFrame(tile, d.frame);
    if (!tile.frame && d.size && d.size[0] && d.size[1]) tile.ratio = d.size[0] / d.size[1];
  }

  // Bezel from device_frame.py laid over the screen; the screen sits in its middle.
  function setFrame(tile, frame) {
    const [sw, sh] = frame.screen, [fw, fh] = frame.frame, [ox, oy] = frame.offset;
    const chrome = new Image();
    chrome.className = 'chrome';
    chrome.alt = '';
    chrome.draggable = false;
    chrome.src = '/frame/' + encodeURIComponent(frame.product) + '.png';
    Object.assign(chrome.style, {left: (-ox / sw * 100) + '%', top: (-oy / sh * 100) + '%',
                                 width: (fw / sw * 100) + '%', height: (fh / sh * 100) + '%'});
    // Clip the screen to the display's own shape, so square screenshot corners stay inside the bezel.
    if (frame.mask) {
      const url = 'url("/frame/' + encodeURIComponent(frame.product) + '.mask.png")';
      for (const el of [tile.video, tile.off]) {
        for (const prop of ['maskImage', 'webkitMaskImage']) el.style[prop] = url;
        for (const prop of ['maskSize', 'webkitMaskSize']) el.style[prop] = '100% 100%';
        for (const prop of ['maskRepeat', 'webkitMaskRepeat']) el.style[prop] = 'no-repeat';
      }
    }
    tile.screen.classList.add('framed');
    tile.screen.append(chrome);
    tile.frame = frame;
    tile.ratio = sw / sh;
  }

  function removeTile(tile) {
    tile.video.removeAttribute('src');
    tile.el.remove();
    tiles.delete(tile.key);
  }

  function render() {
    const visible = shown();
    const keys = new Set(visible.map(keyOf));
    for (const tile of [...tiles.values()]) if (!keys.has(tile.key)) removeTile(tile);
    for (const d of visible) {
      const tile = tiles.get(keyOf(d));
      if (tile && tile.image !== isImage(d)) removeTile(tile);
    }
    visible.forEach((d, i) => updateTile(tiles.get(keyOf(d)) || makeTile(d), d, i));
    empty.hidden = !loaded || visible.length > 0;
    layout();
  }

  // ----- sizes: every device as tall as fits, all of them across the window

  function layout() {
    const order = shown().map((d) => tiles.get(keyOf(d))).filter(Boolean);
    if (!order.length) return;
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    const availH = vh - 2 * EDGE - LABEL_H, availW = vw - 2 * EDGE - GAP * (order.length - 1);
    const parts = order.map((t) => {
      const r = t.ratio || DEFAULT_RATIO[t.kind] || 0.46;
      const g = t.frame ? [t.frame.frame[0] / t.frame.screen[0], t.frame.frame[1] / t.frame.screen[1]] : null;
      const h = Math.max(40, g ? availH / g[1] : availH - 2 * BEZEL);
      return {t, r, g, w: h * r, fixed: g ? 0 : 2 * BEZEL, grow: g ? g[0] : 1};
    });
    const flexible = parts.reduce((s, p) => s + p.w * p.grow, 0);
    const fixed = parts.reduce((s, p) => s + p.fixed, 0);
    const scale = flexible + fixed > availW ? Math.max(0.05, (availW - fixed) / flexible) : 1;
    for (const p of parts) {
      const w = Math.max(30, p.w * scale), h = w / p.r;
      p.t.screen.style.width = w + 'px';
      p.t.screen.style.height = h + 'px';
      p.t.screen.style.margin = p.g ? ((p.g[1] - 1) * h / 2) + 'px ' + ((p.g[0] - 1) * w / 2) + 'px' : '';
    }
  }
  window.addEventListener('resize', layout);

  // An iPhone frame reports its video size; used when the model has no bezel data.
  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin || !e.data || !Array.isArray(e.data.deckSize)) return;
    for (const tile of tiles.values()) {
      if (tile.video.contentWindow === e.source && !tile.frame) {
        const [w, h] = e.data.deckSize;
        if (w > 0 && h > 0) { tile.ratio = w / h; layout(); }
      }
    }
  });

  // ----- drag a device by its label to reorder

  let dragKey = null, dropBefore = undefined;

  function wireDrag(tile) {
    tile.label.addEventListener('dragstart', (e) => {
      dragKey = tile.key;
      dropBefore = undefined;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', tile.key);
      tile.el.classList.add('dragging');
    });
    tile.label.addEventListener('dragend', endDrag);
  }

  function endDrag() {
    const tile = dragKey && tiles.get(dragKey);
    if (tile) tile.el.classList.remove('dragging');
    dragKey = null;
    dropBefore = undefined;
    mark.hidden = true;
  }

  // Where the dragged device would land: before the first shown device whose middle is right
  // of the pointer (null = at the end).
  function dropTarget(x) {
    const others = shown().map(keyOf).filter((k) => k !== dragKey).map((k) => tiles.get(k)).filter(Boolean);
    for (const t of others) {
      const r = t.el.getBoundingClientRect();
      if (x < r.left + r.width / 2) return {before: t.key, tile: t, side: 'left'};
    }
    return {before: null, tile: others[others.length - 1] || null, side: 'right'};
  }

  function newOrder(before) {
    const keys = devices.map(keyOf).filter((k) => k !== dragKey);
    let at;
    if (before) {
      at = keys.indexOf(before);
    } else {
      const lastShown = shown().map(keyOf).filter((k) => k !== dragKey).pop();
      at = lastShown ? keys.indexOf(lastShown) + 1 : keys.length;
    }
    keys.splice(at < 0 ? keys.length : at, 0, dragKey);
    return keys;
  }

  document.addEventListener('dragover', (e) => {
    if (!dragKey) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = dropTarget(e.clientX);
    dropBefore = target.before;
    const unchanged = newOrder(target.before).join('\n') === devices.map(keyOf).join('\n');
    if (!target.tile || unchanged) { mark.hidden = true; return; }
    const r = target.tile.el.getBoundingClientRect();
    const rects = [...tiles.values()].map((t) => t.el.getBoundingClientRect());
    const top = Math.min(...rects.map((q) => q.top)), bottom = Math.max(...rects.map((q) => q.bottom));
    mark.style.left = (target.side === 'left' ? r.left - GAP / 2 : r.right + GAP / 2) + 'px';
    mark.style.top = top + 'px';
    mark.style.height = (bottom - top) + 'px';
    mark.hidden = false;
  });

  document.addEventListener('drop', (e) => {
    if (!dragKey) return;
    e.preventDefault();
    const before = dropBefore === undefined ? dropTarget(e.clientX).before : dropBefore;
    const keys = newOrder(before);
    endDrag();
    reorder(keys);
  });

  function reorder(keys) {
    const byKey = new Map(devices.map((d) => [keyOf(d), d]));
    const next = keys.map((k) => byKey.get(k)).filter(Boolean);
    if (next.map(keyOf).join('\n') === devices.map(keyOf).join('\n')) return;
    devices = next;
    save('Order saved');
  }

  // ----- saving

  async function save(message) {
    const v = ++version;
    devices.forEach((d, i) => { d.order = i; });
    render();
    renderList(true);
    const body = {devices: devices.map((d, i) => ({id: d.id, kind: d.kind, name: d.custom_name || null,
                                                    hidden: !!d.hidden, order: i}))};
    try {
      const r = await fetch('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify(body), cache: 'no-store'});
      const reply = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(reply.error || ('HTTP ' + r.status));
      setStatus(message || 'Saved', false);
    } catch (err) {
      setStatus('Not saved: ' + err.message, true);
    }
    if (v === version) poll();
  }

  function setStatus(text, error) {
    status.textContent = text;
    status.classList.toggle('error', !!error);
    clearTimeout(setStatus.timer);
    if (!error) setStatus.timer = setTimeout(() => { status.textContent = ''; }, 2500);
  }

  // ----- the device list panel

  let listSig = '';

  function renderList(force) {
    if (panel.hidden) return;
    if (!force && panel.contains(document.activeElement) && document.activeElement.type === 'text') return;
    const sig = JSON.stringify(devices.map((d) => [keyOf(d), d.name, d.custom_name, d.hidden, d.connected, d.meta]));
    if (!force && sig === listSig) return;
    listSig = sig;
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.focus : null;
    list.replaceChildren(...devices.map((d, i) => row(d, i)));
    if (focused) {
      const again = list.querySelector('[data-focus="' + CSS.escape(focused) + '"]');
      if (again && !again.disabled) again.focus();
    }
  }

  function current(key) {
    return devices.find((x) => keyOf(x) === key);
  }

  function row(d, i) {
    const key = keyOf(d);
    const li = document.createElement('li');
    li.classList.toggle('offline', !d.connected);
    const show = document.createElement('input');
    show.type = 'checkbox';
    show.checked = !d.hidden;
    show.title = 'Show in the preview';
    show.setAttribute('aria-label', 'Show ' + d.name);
    show.dataset.focus = key + ':show';
    show.addEventListener('change', () => {
      const cur = current(key);
      if (!cur) return;
      cur.hidden = !show.checked;
      save(show.checked ? 'Shown' : 'Hidden');
    });
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = KIND[d.kind] || d.kind;
    const name = document.createElement('input');
    name.type = 'text';
    name.value = d.custom_name || '';
    name.placeholder = d.detected_name || d.id;
    name.maxLength = 100;
    name.setAttribute('aria-label', 'Label for ' + (d.detected_name || d.id));
    name.dataset.focus = key + ':name';
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') name.blur(); if (e.key === 'Escape') { name.value = (current(key) || d).custom_name || ''; name.blur(); } });
    name.addEventListener('change', () => {
      const cur = current(key);
      if (!cur) return;
      const value = name.value.trim() || null;
      if (value === (cur.custom_name || null)) return;
      cur.custom_name = value;
      cur.name = value || cur.detected_name || cur.id;
      save('Label saved');
    });
    const up = moveButton('▲', 'Move up', key + ':up', i === 0, () => move(key, -1));
    const down = moveButton('▼', 'Move down', key + ':down', i === devices.length - 1, () => move(key, 1));
    const info = document.createElement('span');
    info.className = 'info';
    info.textContent = [d.meta, d.id, d.connected ? null : 'not connected'].filter(Boolean).join(' · ');
    info.title = info.textContent;
    li.append(show, kind, name, up, down, info);
    return li;
  }

  function moveButton(text, title, focusKey, disabled, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'move';
    b.textContent = text;
    b.title = title;
    b.setAttribute('aria-label', title);
    b.disabled = disabled;
    b.dataset.focus = focusKey;
    b.addEventListener('click', onClick);
    return b;
  }

  function move(key, step) {
    const keys = devices.map(keyOf);
    const i = keys.indexOf(key);
    const j = i + step;
    if (i < 0 || j < 0 || j >= keys.length) return;
    [keys[i], keys[j]] = [keys[j], keys[i]];
    reorder(keys);
  }

  function setPanel(open) {
    panel.hidden = !open;
    gear.setAttribute('aria-expanded', String(open));
    if (open) { renderList(true); } else { gear.focus(); }
  }
  gear.addEventListener('click', () => setPanel(panel.hidden));
  document.getElementById('panel-close').addEventListener('click', () => setPanel(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) setPanel(false); });
  document.addEventListener('pointerdown', (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !gear.contains(e.target)) setPanel(false);
  });

  // ----- polling

  async function poll() {
    const v = version;
    try {
      const r = await fetch('/api/devices', {cache: 'no-store'});
      const data = await r.json();
      if (v !== version) return;
      devices = data.devices || [];
      panelsPort = data.panels_port || null;
      loaded = true;
      render();
      renderList(false);
    } catch (e) {
      // the daemon is restarting; keep the last picture
    }
  }
  poll();
  setInterval(poll, POLL_MS);
})();
