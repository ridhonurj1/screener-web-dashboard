/* ==========================================================================
   ScreenerNantiAja — Terminal Client
   Keyed DOM renderer (smooth 200ms ticks), per-token price history
   sparklines, resilient WebSocket connection, zero external deps.
   ========================================================================== */

'use strict';

/* ---------------- State ---------------- */

const state = {
  signals: [],
  activePositions: [],
  closedPositions: [],
  stats: {},
  filter: 'OPEN',
  sort: 'time',
  sideTab: 'open',
  lastTickAt: 0,
  priceHistory: new Map(),   // ca -> number[] (ring buffer)
  cardRefs: new Map(),       // ca -> {root, refs}
  domOrder: '',
  closedSig: '',             // change-detection for history list
  activePosSig: ''
};

let currentModalCa = '';
const lastSeenPrices = {};
const TOKEN_LOGOS = new Map();      // ca -> icon url
const LOGOS_FETCHED = new Set();    // ca batches already requested
let walletAutoBuy = false;          // real-money auto-buy state (from wallet settings)

/* ---------------- Icons (inline SVG) ---------------- */

const I = {
  bolt: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 3 14h7v8l10-12h-7l0-8z"/></svg>',
  copy: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
  chart: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m7 14 4-4 4 3 5-6"/></svg>',
  coins: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/></svg>',
  drop: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/></svg>',
  brain: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/></svg>',
  gauge: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
  check: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  alert: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4m0 4h.01"/></svg>',
  info: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></svg>',
  radar: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 1 0-8.01 8.91"/><circle cx="12" cy="12" r="10"/></svg>',
  wallet: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" height="14" x="2" y="5" rx="2"/><path d="M2 10h20"/></svg>',
  flame: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>'
};

/* ---------------- Formatters ---------------- */

function fmtPrice(p) {
  p = parseFloat(p);
  if (!p || !isFinite(p) || p <= 0) return '0';
  if (p >= 1000) return p.toFixed(2);
  if (p >= 1) return p.toFixed(4);
  if (p >= 0.001) return p.toFixed(6);
  if (p >= 0.00001) return p.toFixed(8);
  return p.toExponential(3);
}

function fmtUSD(v) {
  v = parseFloat(v) || 0;
  if (v <= 0) return '$0';
  if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
  return '$' + v.toFixed(v < 10 ? 1 : 0);
}

function fmtSol(v, dp = 4) {
  v = parseFloat(v) || 0;
  return v.toFixed(dp);
}

function parseTs(s) {
  if (!s) return null;
  const d = new Date(String(s).replace(' ', 'T'));
  return isNaN(d.getTime()) ? null : d.getTime();
}

function relTime(ts) {
  if (!ts) return '—';
  const diff = Math.max(0, Date.now() - ts) / 1000;
  if (diff < 60) return 'baru saja';
  if (diff < 3600) return Math.floor(diff / 60) + 'm lalu';
  if (diff < 86400) return Math.floor(diff / 3600) + 'j lalu';
  return Math.floor(diff / 86400) + 'h lalu';
}

function fmtHold(sec) {
  sec = parseInt(sec) || 0;
  if (sec < 60) return sec + 'dtk';
  if (sec < 3600) return Math.floor(sec / 60) + 'mnt';
  return (sec / 3600).toFixed(1) + ' jam';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const AVATAR_TONES = ['tone-lime', 'tone-cyan', 'tone-blue', 'tone-violet', 'tone-amber'];
function avatarTone(ca) {
  let h = 0;
  for (let i = 0; i < ca.length; i++) h = (h * 31 + ca.charCodeAt(i)) | 0;
  return AVATAR_TONES[Math.abs(h) % AVATAR_TONES.length];
}

function avatarHTML(ca, symbol, style = '') {
  const logo = TOKEN_LOGOS.get(ca);
  const initial = esc((symbol || '?').slice(0, 3).toUpperCase());
  if (logo) {
    return `<div class="token-avatar" data-ca="${esc(ca)}" style="${style}"><img src="${esc(logo)}" alt="" loading="lazy" onerror="this.parentElement.classList.add('${avatarTone(ca)}');this.remove()"></div>`;
  }
  return `<div class="token-avatar ${avatarTone(ca)}" data-ca="${esc(ca)}" style="${style}">${initial}</div>`;
}

/* ---------------- Token logos (Jupiter, batched via bridge) ---------------- */

let logosFetchTimer = null;
function queueLogoFetch() {
  clearTimeout(logosFetchTimer);
  logosFetchTimer = setTimeout(fetchLogos, 400);
}

async function fetchLogos() {
  const cas = [...state.cardRefs.keys()]
    .filter(ca => !LOGOS_FETCHED.has(ca));
  if (cas.length === 0) return;
  cas.forEach(ca => LOGOS_FETCHED.add(ca));

  // batch max 60 per request (URL safety)
  for (let i = 0; i < cas.length; i += 60) {
    const batch = cas.slice(i, i + 60);
    try {
      const res = await fetch(`/api/token_meta?cas=${batch.join(',')}`);
      const data = await res.json();
      if (data.success && data.logos) {
        for (const [ca, url] of Object.entries(data.logos)) {
          if (url) TOKEN_LOGOS.set(ca, url);
        }
      }
    } catch (e) { /* logos are cosmetic — stay silent */ }
  }
  applyLogos();
}

function applyLogos() {
  for (const { root } of state.cardRefs.values()) {
    const holder = root.querySelector('.token-avatar[data-ca]');
    if (!holder || holder.querySelector('img')) continue;
    const logo = TOKEN_LOGOS.get(holder.dataset.ca);
    if (!logo) continue;
    holder.classList.remove(...AVATAR_TONES);
    const img = document.createElement('img');
    img.src = logo;
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = () => { img.remove(); holder.classList.add(avatarTone(holder.dataset.ca)); };
    holder.appendChild(img);
  }
}

/* ---------------- Toast ---------------- */

function toast(text, type = 'success') {
  const zone = document.getElementById('toastZone');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = type === 'success' ? I.check : type === 'error' ? I.alert : I.info;
  el.innerHTML = `${icon}<span>${esc(text)}</span>`;
  zone.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 260);
  }, 2400);
}

function copyCA(ca) {
  if (!ca) return;
  navigator.clipboard.writeText(ca).then(
    () => toast(`CA ${ca.slice(0, 5)}…${ca.slice(-4)} dicopy!`),
    () => toast('Gagal mengcopy CA', 'error')
  );
}

/* ---------------- Price history / sparkline ---------------- */

function pushPrice(ca, price) {
  if (!price || !isFinite(price) || price <= 0) return;
  let buf = state.priceHistory.get(ca);
  if (!buf) { buf = []; state.priceHistory.set(ca, buf); }
  const last = buf[buf.length - 1];
  if (last === price) return;          // skip flat duplicates
  buf.push(price);
  if (buf.length > 90) buf.shift();
}

function sparklineSVG(ca) {
  const buf = state.priceHistory.get(ca) || [];
  const W = 100, H = 34, PAD = 3;
  if (buf.length < 2) {
    return `<div class="sig-spark"><div class="sig-spark-empty">MENUNGGU TICK HARGA…</div></div>`;
  }
  const min = Math.min(...buf), max = Math.max(...buf);
  const range = (max - min) || (max * 0.0001) || 1;
  const pts = buf.map((p, i) => {
    const x = PAD + (i / (buf.length - 1)) * (W - 2 * PAD);
    const y = H - PAD - ((p - min) / range) * (H - 2 * PAD);
    return x.toFixed(1) + ',' + y.toFixed(1);
  });
  const up = buf[buf.length - 1] >= buf[0];
  const color = up ? '#2fd77b' : '#ff5470'; // literal hex: SVG attributes cannot resolve CSS vars
  const line = `M${pts.join('L')}`;
  const area = `${line}L${W - PAD},${H}L${PAD},${H}Z`;
  const gid = 'sg' + Math.abs(hashStr(ca)).toString(36);
  return `<div class="sig-spark"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}"/><stop offset="1" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <path class="area" d="${area}" fill="url(#${gid})"/>
    <path class="line" d="${line}" stroke="${color}"/>
  </svg></div>`;
}

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h;
}

/* ---------------- Signal cards (keyed renderer) ---------------- */

function visibleSignals() {
  let list = state.signals.slice();

  // dedupe by CA (engine can emit OPEN + CLOSED rows for the same token)
  const seen = new Set();
  list = list.filter(s => {
    if (!s || !s.ca || seen.has(s.ca)) return false;
    seen.add(s.ca);
    return true;
  });

  if (state.filter === 'OPEN') list = list.filter(s => (s.status || 'OPEN').toUpperCase() === 'OPEN');
  if (state.filter === 'CLOSED') list = list.filter(s => (s.status || '').toUpperCase() === 'CLOSED');

  if (state.sort === 'multiplier') list.sort((a, b) => multOf(b) - multOf(a));
  else if (state.sort === 'score') list.sort((a, b) => (parseFloat(b.score) || 0) - (parseFloat(a.score) || 0));
  else list.sort((a, b) => (parseTs(b.created_at) || 0) - (parseTs(a.created_at) || 0));

  return list;
}

function multOf(sig) {
  const entry = parseFloat(sig.entry_price);
  const live = parseFloat(sig.current_price);
  if (entry > 0 && live > 0) return live / entry;
  return parseFloat(sig.current_multiplier) || 1;
}

function scoreColor(score) {
  score = parseFloat(score) || 0;
  if (score >= 75) return 'var(--lime)';
  if (score >= 55) return 'var(--amber)';
  return 'var(--red)';
}

function buildSignalCard(sig) {
  const root = document.createElement('article');
  root.className = 'sig-card';
  root.dataset.ca = sig.ca;
  const closed = (sig.status || '').toUpperCase() === 'CLOSED';
  if (closed) root.classList.add('is-closed');

  root.innerHTML = `
    <div class="sig-row1">
      <div class="sig-id">
        ${avatarHTML(sig.ca, sig.symbol)}
        <div class="sig-name-block">
          <div class="sig-symbol-row">
            <span class="sig-symbol">$${esc(sig.symbol || '???')}</span>
            <span class="name">${esc(sig.name || '')}</span>
          </div>
          <div class="sig-meta-row">
            <span class="chip" data-ref="strategy">${esc(sig.strategy || 'Ponyin Quant')}</span>
            ${sig.tier_label ? `<span class="chip chip-cyan" data-ref="tier">${esc(sig.tier_label)}</span>` : ''}
            <span class="dot-sep">•</span>
            <span class="sig-age" data-ref="age" title="${esc(sig.created_at || '')}"></span>
          </div>
        </div>
      </div>
      <div class="sig-price-block">
        <span class="sig-price" data-ref="price">$${fmtPrice(sig.current_price)}</span>
        <div><span class="sig-mult mult-flat" data-ref="mult"></span></div>
      </div>
    </div>
    <div data-ref="spark"></div>
    <div class="sig-metrics">
      <div class="metric"><div class="k">${I.coins} Live MC</div><div class="v" data-ref="mcap">—</div></div>
      <div class="metric"><div class="k">${I.gauge} Entry MC</div><div class="v" data-ref="entry">—</div></div>
      <div class="metric"><div class="k">${I.drop} Likuiditas</div><div class="v c-cyan" data-ref="liq">—</div></div>
      <div class="metric"><div class="k">${I.brain} Smart Money</div><div class="v c-lime" data-ref="sm">0 SM</div></div>
    </div>
    <div class="score-meter">
      <span class="kpi-label" style="font-size:9px">${I.flame} Alpha Score</span>
      <div class="track"><i data-ref="scoreBar"></i></div>
      <span class="num" data-ref="scoreNum">0</span>
    </div>
    <div class="sig-foot">
      <button class="ca-chip" data-act="copy" title="Copy contract address">
        <span>${esc(sig.ca.slice(0, 6))}…${esc(sig.ca.slice(-4))}</span>
        ${I.copy}
      </button>
      <div class="actions">
        <button class="btn btn-lime" data-act="chart">${I.chart} Chart</button>
        <button class="btn btn-solid" data-act="buy">${I.bolt} Beli</button>
      </div>
    </div>`;

  const refs = {};
  root.querySelectorAll('[data-ref]').forEach(el => { refs[el.dataset.ref] = el; });
  refs.sparkHost = refs.spark;

  root.addEventListener('click', e => {
    if (e.target.closest('[data-act="copy"]')) { e.stopPropagation(); copyCA(sig.ca); return; }
    if (e.target.closest('[data-act="buy"]')) {
      e.stopPropagation();
      openTradeModal({ action: 'buy', ca: sig.ca, symbol: sig.symbol || 'TOKEN', price: sig.current_price });
      return;
    }
    openChartModal(sig.ca, sig.symbol, sig.name, sig.current_price);
  });

  state.cardRefs.set(sig.ca, { root, refs, sig });
  return root;
}

function updateSignalCard(sig) {
  const entry = state.cardRefs.get(sig.ca);
  if (!entry) return;
  const { refs } = entry;
  entry.sig = sig;

  const liveNum = parseFloat(sig.current_price) || 0;
  const mult = multOf(sig);
  const isUp = mult >= 1;

  // price + tick flash
  refs.price.textContent = '$' + fmtPrice(sig.current_price);
  if (lastSeenPrices[sig.ca] !== undefined && liveNum !== lastSeenPrices[sig.ca]) {
    refs.price.classList.remove('tick-up', 'tick-down');
    void refs.price.offsetWidth; // restart animation
    refs.price.classList.add(liveNum > lastSeenPrices[sig.ca] ? 'tick-up' : 'tick-down');
  }
  lastSeenPrices[sig.ca] = liveNum;

  // multiplier
  refs.mult.textContent = `${isUp ? '▲' : '▼'} ${mult.toFixed(2)}x`;
  refs.mult.className = `sig-mult ${mult > 1.001 ? 'mult-up' : mult < 0.999 ? 'mult-down' : 'mult-flat'}`;

  // metrics
  refs.mcap.textContent = fmtUSD(sig.current_mcap || sig.entry_mcap);
  refs.entry.textContent = fmtUSD(sig.entry_mcap);
  refs.liq.textContent = fmtUSD(sig.liq);
  refs.sm.textContent = `${parseInt(sig.sm_count) || 0} SM`;

  // score meter
  const score = parseFloat(sig.score) || 0;
  refs.scoreBar.style.width = Math.min(100, score) + '%';
  refs.scoreBar.style.background = scoreColor(score);
  refs.scoreNum.textContent = score.toFixed(0);
  refs.scoreNum.style.color = scoreColor(score);

  // sparkline
  refs.spark.innerHTML = sparklineSVG(sig.ca);
}

function renderSignals() {
  const listEl = document.getElementById('signalsList');
  listEl.querySelectorAll(':scope > .skeleton').forEach(el => el.remove());
  const list = visibleSignals();

  document.getElementById('tokenBadge').textContent = `${list.length} token`;

  if (list.length === 0) {
    state.cardRefs.clear();
    listEl.innerHTML = `
      <div class="empty-state">
        <div class="orb">${I.radar}</div>
        <div class="title">Belum ada sinyal di filter ini</div>
        <div class="hint">Engine terus memindai token Solana baru. Kartu akan muncul otomatis begitu ada sinyal lolos screening Ponyin v5.</div>
      </div>`;
    state.domOrder = '';
    return;
  }

  // create missing cards
  const frag = document.createDocumentFragment();
  let created = false;
  for (const sig of list) {
    if (!state.cardRefs.has(sig.ca)) { frag.appendChild(buildSignalCard(sig)); created = true; }
  }
  if (created) { listEl.appendChild(frag); queueLogoFetch(); }

  // remove stale cards
  const wanted = new Set(list.map(s => s.ca));
  for (const [ca, entry] of state.cardRefs) {
    if (!wanted.has(ca)) { entry.root.remove(); state.cardRefs.delete(ca); }
  }

  // keep desired order (skip churn when unchanged)
  const orderKey = list.map(s => s.ca).join('|');
  if (orderKey !== state.domOrder) {
    state.domOrder = orderKey;
    for (const sig of list) listEl.appendChild(state.cardRefs.get(sig.ca).root);
  }

  // update all
  for (const sig of list) updateSignalCard(sig);
}

function renderSignalAges() {
  for (const { refs, sig } of state.cardRefs.values()) {
    refs.age.textContent = relTime(parseTs(sig.created_at));
  }
}
setInterval(renderSignalAges, 30000);

/* ---------------- Positions & history ---------------- */

function pnlOf(pos) {
  const entry = parseFloat(pos.entry_price_usd);
  const live = parseFloat(pos.current_price_usd);
  if (entry > 0 && live > 0) return ((live - entry) / entry) * 100;
  return 0;
}

function renderActivePositions() {
  const el = document.getElementById('sideTabContentOpen');
  const list = state.activePositions;
  document.getElementById('posBadge').textContent = list.length;

  const sig = list.map(p => p.id).join('|');
  if (sig === state.activePosSig && list.length > 0) { updateActivePositions(); return; }
  state.activePosSig = sig;

  if (list.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="orb">${I.chart}</div>
        <div class="title">Tidak ada posisi aktif</div>
        <div class="hint">Modal sandbox siap 100%. Entry baru akan tampil di sini secara real-time dengan PnL live.</div>
      </div>`;
    return;
  }

  el.innerHTML = list.map(pos => {
    const pnl = pnlOf(pos);
    const win = pnl >= 0;
    const live = parseFloat(pos.current_price_usd) || 0;
    const peak = parseFloat(pos.peak_multiplier) || 1;
    return `
    <div class="pos-card" onclick="openChartModal('${esc(pos.token_ca)}', '${esc(pos.symbol)}', '', '${live}')">
      <div class="pos-row1">
        <div class="pos-token">
          <div class="token-avatar ${avatarTone(pos.token_ca || '')}" style="width:28px;height:28px;font-size:10px;border-radius:8px">${esc((pos.symbol || '?').slice(0, 3).toUpperCase())}</div>
          <div>
            <span class="pos-symbol">$${esc(pos.symbol)}</span>
            <span class="pos-sol">· ${fmtSol(pos.sol_spent, 3)} SOL</span>
          </div>
        </div>
        <div class="pos-pnl" style="color:${win ? 'var(--green)' : 'var(--red)'}">
          <span class="arrow">${win ? '▲' : '▼'}</span>${win ? '+' : ''}${pnl.toFixed(2)}%
        </div>
      </div>
      <div class="pos-grid">
        <div class="cell"><div class="k">Live</div><div class="v">$${fmtPrice(pos.current_price_usd)}</div></div>
        <div class="cell"><div class="k">MCap</div><div class="v">${fmtUSD(pos.current_mcap)}</div></div>
        <div class="cell"><div class="k">Peak</div><div class="v" style="color:var(--cyan)">${peak.toFixed(2)}x</div></div>
        <div class="cell"><div class="k">Score</div><div class="v" style="color:${scoreColor(pos.score)}">${parseInt(pos.score) || 0}</div></div>
      </div>
      <div class="pos-foot">
        <span class="chip" style="font-size:9.5px">${esc(pos.strategy || 'Ponyin')}</span>
        <div style="display:flex;align-items:center;gap:9px">
          <span class="sig-age">${relTime(parseTs(pos.created_at))}</span>
          <button class="btn btn-danger" onclick="event.stopPropagation();openTradeModal({action:'sell', positionId:${pos.id}, symbol:'${esc(pos.symbol)}', tokenCa:'${esc(pos.token_ca)}', tokensRemaining:${parseFloat(pos.tokens_remaining) || 0}, entryPrice:${parseFloat(pos.entry_price_usd) || 0}, currentPrice:${parseFloat(pos.current_price_usd) || 0}, solSpent:${parseFloat(pos.sol_spent) || 0}})">Jual</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

function updateActivePositions() {
  // lightweight refresh of price-dependent cells
  renderActivePositionsForce();
}

function renderActivePositionsForce() {
  const keep = state.activePosSig;
  state.activePosSig = '';
  renderActivePositions();
  state.activePosSig = keep;
}

function renderHistory() {
  const el = document.getElementById('sideTabContentHistory');
  const list = state.closedPositions;
  document.getElementById('histBadge').textContent = list.length;

  const sig = list.map(p => p.id).join('|');
  if (sig === state.closedSig) return;
  state.closedSig = sig;

  if (list.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="orb">${I.wallet}</div>
        <div class="title">Belum ada riwayat trade</div>
        <div class="hint">Setiap posisi yang close (TP/SL/manual) tercatat permanen di sini dari SQLite engine.</div>
      </div>`;
    return;
  }

  el.innerHTML = list.map(pos => {
    const exit = (pos.exit_reason || '').toUpperCase();
    const isWin = exit.includes('TP') || (parseFloat(pos.realized_sol) || 0) >= (parseFloat(pos.sol_spent) || 0);
    const peak = parseFloat(pos.peak_multiplier) || 1;
    const r = pos.r_result !== null && pos.r_result !== undefined && pos.r_result !== ''
      ? `${parseFloat(pos.r_result) >= 0 ? '+' : ''}${parseFloat(pos.r_result).toFixed(2)}R` : '';
    const cls = isWin ? 'win' : 'loss';
    const label = exit.includes('TP') ? 'TAKE PROFIT' : exit.includes('SL') ? 'STOP LOSS' : 'CLOSED';
    const pnlSol = (parseFloat(pos.realized_sol) || 0) - (parseFloat(pos.sol_spent) || 0);
    return `
    <div class="hist-row" onclick="openChartModal('${esc(pos.token_ca)}', '${esc(pos.symbol)}', '', '')">
      <div class="hist-left">
        <div class="hist-symbol-row">
          <span class="hist-symbol">$${esc(pos.symbol)}</span>
          <span class="hist-result ${cls}">${label}</span>
        </div>
        <div class="hist-sub">${fmtSol(pos.sol_spent, 3)} SOL → ${fmtSol(pos.realized_sol, 3)} SOL (${pnlSol >= 0 ? '+' : ''}${pnlSol.toFixed(4)}) · hold ${fmtHold(pos.hold_duration_sec)}</div>
      </div>
      <div class="hist-right">
        <div class="hist-mult" style="color:${isWin ? 'var(--green)' : 'var(--red)'}">${peak.toFixed(2)}x ${r}</div>
        <div class="hist-mcap">${fmtUSD(pos.entry_mcap)} → ${fmtUSD(pos.exit_price_usd ? pos.current_mcap : pos.entry_mcap)}</div>
      </div>
    </div>`;
  }).join('');
}

/* ---------------- Stats / KPIs ---------------- */

function renderStats() {
  const s = state.stats || {};
  const balance = parseFloat(s.virtual_balance_sol) || 0;
  const pnl = parseFloat(s.realized_pnl_sol) || 0;
  const wins = parseInt(s.win_trades) || 0;
  const loses = parseInt(s.lose_trades) || 0;
  const total = wins + loses;
  const wr = total > 0 ? (wins / total) * 100 : 0;
  // Engine sandbox starts at 0.1 SOL; guard against divide-by-zero when funds are held in open positions
  const initial = Math.max(balance - pnl, 0.1);

  // header strip
  document.getElementById('statBalance').textContent = `${fmtSol(balance)} SOL`;
  const pnlEl = document.getElementById('statPnl');
  pnlEl.textContent = `${pnl >= 0 ? '+' : ''}${fmtSol(pnl)} SOL`;
  pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('statWinRate').textContent = total > 0 ? `${wr.toFixed(1)}%` : '—';

  // KPI cards
  document.getElementById('kpiBalance').textContent = fmtSol(balance);
  document.getElementById('kpiBalanceSub').textContent = `Modal awal ${fmtSol(Math.max(initial, 0.1))} SOL`;

  const kpiPnl = document.getElementById('kpiPnl');
  kpiPnl.textContent = `${pnl >= 0 ? '+' : ''}${fmtSol(pnl)}`;
  kpiPnl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  const pnlPct = initial > 0 ? (pnl / initial) * 100 : 0;
  document.getElementById('kpiPnlSub').textContent = total > 0
    ? `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}% modal · ${total} trade`
    : 'Belum ada trade selesai';

  document.getElementById('kpiWinRate').textContent = total > 0 ? `${wr.toFixed(1)}%` : '—';
  document.getElementById('kpiWinRate').style.color = total === 0 ? 'var(--text-1)' : (wr >= 50 ? 'var(--green)' : 'var(--red)');
  document.getElementById('kpiWinBar').style.width = `${Math.min(100, wr)}%`;

  const active = parseInt(s.active_positions_count ?? state.activePositions.length) || 0;
  document.getElementById('kpiActivity').textContent = active;
  document.getElementById('kpiActivitySub').textContent = `posisi terbuka · ${parseInt(s.total_signals_count) || state.signals.length} sinyal dipantau`;
}

/* ---------------- WebSocket ---------------- */

const connPill = document.getElementById('connPill');
const connLabel = document.getElementById('connLabel');

function setConn(mode) {
  connPill.classList.remove('is-live', 'is-connecting', 'is-offline');
  connPill.classList.add(`is-${mode}`);
  connLabel.textContent = mode === 'live' ? 'LIVE' : mode === 'connecting' ? 'MENGHUBUNGKAN' : 'OFFLINE — KLIK UNTUK SAMBUNG ULANG';
  connPill.title = mode === 'live' ? 'Terhubung ke engine (tick 200ms)' : mode === 'connecting' ? 'Menyambungkan ke engine...' : 'Koneksi engine terputus. Klik untuk mencoba lagi.';
}

function applyPayload(data) {
  state.lastTickAt = Date.now();

  if (data.stats) state.stats = data.stats;
  if (Array.isArray(data.signals)) {
    state.signals = data.signals;
    for (const s of data.signals) {
      const p = parseFloat(s.current_price);
      if (p > 0) pushPrice(s.ca, p);
    }
  }
  if (data.active_positions) state.activePositions = data.active_positions;
  if (data.closed_positions) state.closedPositions = data.closed_positions;

  renderSignals();
  renderActivePositions();
  renderHistory();
  renderPortfolio(false);
  renderRecap();
  renderStats();
}

let ws = null;
let reconnectDelay = 1000;

function connectWS() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  setConn('connecting');

  const loc = window.location;
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    ws = new WebSocket(`${proto}//${loc.host}/ws/live`);
  } catch (e) {
    setConn('offline');
    scheduleReconnect();
    return;
  }

  ws.onopen = () => { reconnectDelay = 1000; };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      applyPayload(data);
      setConn('live');
    } catch (e) {
      console.error('WS parse error:', e);
    }
  };

  ws.onclose = () => {
    setConn('offline');
    scheduleReconnect();
  };

  ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
}

function scheduleReconnect() {
  setTimeout(connectWS, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, 8000);
}

connPill.addEventListener('click', () => {
  if (connPill.classList.contains('is-offline')) {
    reconnectDelay = 1000;
    connectWS();
    toast('Menyambungkan ulang ke engine...', 'info');
  }
});

// HTTP fallback bootstrap so first paint is instant even before WS opens
async function httpBootstrap() {
  try {
    const [rSig, rPos, rStats] = await Promise.all([
      fetch('/api/signals').then(r => r.json()),
      fetch('/api/positions').then(r => r.json()),
      fetch('/api/stats').then(r => r.json())
    ]);
    if (rStats?.data && !state.lastTickAt) state.stats = rStats.data;
    if (rSig?.data && !state.lastTickAt) state.signals = rSig.data;
    if (rPos && !state.lastTickAt) {
      state.activePositions = rPos.active || [];
      state.closedPositions = rPos.closed || [];
    }
    if (!state.lastTickAt) {
      renderSignals();
      renderActivePositions();
      renderHistory();
      renderStats();
    }
  } catch (e) {
    console.error('HTTP bootstrap failed:', e);
  }
}

/* ---------------- Drawer ---------------- */

const drawer = document.getElementById('sideDrawer');
const backdrop = document.getElementById('drawerBackdrop');
const DRAWER_TABS = ['wallets', 'wallet'];

function openDrawer(tab) {
  backdrop.classList.remove('hidden');
  requestAnimationFrame(() => backdrop.classList.add('open'));
  drawer.classList.add('open');
  switchDrawerTab(tab || 'wallets');
}

function closeDrawer() {
  backdrop.classList.remove('open');
  drawer.classList.remove('open');
  setTimeout(() => backdrop.classList.add('hidden'), 300);
}

function toggleDrawer() {
  drawer.classList.contains('open') ? closeDrawer() : openDrawer('wallets');
}

function switchDrawerTab(tab) {
  DRAWER_TABS.forEach(t => {
    document.getElementById(`btnTab${t.charAt(0).toUpperCase() + t.slice(1)}`).classList.toggle('active', t === tab);
    document.getElementById(`drawerTab${t.charAt(0).toUpperCase() + t.slice(1)}`).classList.toggle('hidden', t !== tab);
  });
  if (tab === 'wallets') loadSmartWallets();
  if (tab === 'wallet') loadWalletData();
}

/* ---------------- Smart money ---------------- */

let smLoaded = false;

async function loadSmartWallets() {
  if (smLoaded) return;
  try {
    const res = await fetch('/api/smart_wallets?limit=80');
    const data = await res.json();
    const el = document.getElementById('drawerSmartWalletsList');
    if (data.success && data.data?.length) {
      smLoaded = true;
      document.getElementById('smCountBadge').textContent = `${data.count} wallet teratas`;
      const catTone = { insider: 'chip-lime', smart_degen: 'chip-cyan', snipe: 'chip-blue', whale: 'chip-amber' };
      el.innerHTML = data.data.map((w, i) => {
        const wr = parseFloat(w.winrate_7d) || 0;
        const pnl = parseFloat(w.pnl_7d) || 0;
        const tags = (() => { try { return JSON.parse(w.tags || '[]'); } catch (e) { return []; } })();
        return `
        <div class="sw-row">
          <span class="sw-rank ${i < 3 ? 'top' : ''}">#${i + 1}</span>
          <div class="sw-main">
            <div class="sw-addr">${esc(w.wallet_address.slice(0, 6))}…${esc(w.wallet_address.slice(-4))}
              <span class="chip ${catTone[w.category] || ''}" style="font-size:9px">${esc(w.category || 'smart')}</span>
            </div>
            <div class="sw-tags">${tags.slice(0, 3).map(t => `<span class="chip" style="font-size:9px">${esc(t)}</span>`).join('')}</div>
          </div>
          <div class="sw-stats">
            <div class="sw-stat"><div class="v" style="color:${wr >= 50 ? 'var(--green)' : 'var(--text-2)'}">${wr.toFixed(0)}%</div><div class="k">Win 7d</div></div>
            <div class="sw-stat"><div class="v" style="color:${pnl >= 0 ? 'var(--green)' : 'var(--red)'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}</div><div class="k">PnL SOL</div></div>
            <div class="sw-stat"><div class="v">${parseInt(w.token_num) || 0}</div><div class="k">Token</div></div>
          </div>
        </div>`;
      }).join('');
    } else {
      el.innerHTML = emptyStateHTML(I.brain, 'Data smart money belum tersedia', 'Engine belum mengumpulkan wallet whale. Fitur ini terisi otomatis saat engine berjalan.');
    }
  } catch (e) {
    console.error('loadSmartWallets:', e);
  }
}

function emptyStateHTML(icon, title, hint) {
  return `
    <div class="empty-state">
      <div class="orb">${icon}</div>
      <div class="title">${esc(title)}</div>
      <div class="hint">${esc(hint)}</div>
    </div>`;
}

/* ---------------- Recap (structured performance report) ---------------- */

let recapPeriod = 'daily';
let engineRecapCache = {};

function closedInPeriod(days) {
  if (!days) return state.closedPositions.slice();
  const cutoff = Date.now() - days * 86400e3;
  return state.closedPositions.filter(c => (parseTs(c.closed_at) || 0) >= cutoff);
}

function netOf(c) {
  return (parseFloat(c.realized_sol) || 0) - (parseFloat(c.sol_spent) || 0);
}

function isWinTrade(c) {
  return (parseFloat(c.realized_sol) || 0) >= (parseFloat(c.sol_spent) || 0);
}

let recapSig = '';

function renderRecap(force) {
  const sig = recapPeriod + '|' + state.closedPositions.map(c => c.id).join(',');
  if (!force && sig === recapSig) return;
  recapSig = sig;
  const days = { daily: 1, weekly: 7, monthly: 30, all: 0 }[recapPeriod] || 0;
  const list = closedInPeriod(days);
  document.querySelectorAll('#recapTabs button').forEach(b => b.classList.toggle('active', b.dataset.tf === recapPeriod));

  const nets = list.map(netOf);
  const totalNet = nets.reduce((a, b) => a + b, 0);
  const wins = list.filter(isWinTrade).length;
  const wr = list.length ? (wins / list.length) * 100 : 0;
  const grossWin = nets.filter(n => n > 0).reduce((a, b) => a + b, 0);
  const grossLoss = Math.abs(nets.filter(n => n < 0).reduce((a, b) => a + b, 0));
  const pf = grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? Infinity : 0);
  const best = list.length ? list.reduce((a, b) => netOf(b) > netOf(a) ? b : a) : null;
  const worst = list.length ? list.reduce((a, b) => netOf(b) < netOf(a) ? b : a) : null;

  const set = (id, html, cls) => {
    const el = document.getElementById(id);
    el.innerHTML = html;
    el.className = 'v ' + (cls || '');
  };
  set('rcTrades', String(list.length));
  set('rcWinrate', list.length ? `${wr.toFixed(0)}%` : '—', list.length ? (wr >= 50 ? 'up' : 'down') : '');
  set('rcNet', `${totalNet >= 0 ? '+' : ''}${totalNet.toFixed(4)}`, totalNet >= 0 ? 'up' : 'down');
  set('rcPF', list.length ? (pf === Infinity ? '∞' : pf.toFixed(2)) : '—', pf >= 1.5 ? 'up' : pf >= 1 ? 'dim' : 'down');
  set('rcBest', best ? `<span style="font-size:9px;color:var(--text-4)">$${esc(best.symbol)}</span> +${netOf(best).toFixed(4)}` : '—', best ? 'up' : '');
  set('rcWorst', worst ? `<span style="font-size:9px;color:var(--text-4)">$${esc(worst.symbol)}</span> ${netOf(worst).toFixed(4)}` : '—', worst ? 'down' : '');

  // per-symbol table
  const tbody = document.querySelector('#recapSymbolTable tbody');
  const bySym = {};
  for (const c of list) {
    const s = bySym[c.symbol] || (bySym[c.symbol] = { trades: 0, wins: 0, net: 0, peak: 0 });
    s.trades++;
    if (isWinTrade(c)) s.wins++;
    s.net += netOf(c);
    s.peak = Math.max(s.peak, parseFloat(c.peak_multiplier) || 1);
  }
  const rows = Object.entries(bySym).sort((a, b) => b[1].net - a[1].net);
  document.getElementById('recapEmpty').classList.toggle('hidden', list.length > 0);
  document.getElementById('recapSymbolTable').classList.toggle('hidden', list.length === 0);
  tbody.innerHTML = rows.map(([sym, s]) => `
    <tr>
      <td class="sym-cell">$${esc(sym)}</td>
      <td class="num">${s.trades}</td>
      <td class="num" style="color:${s.wins / s.trades >= 0.5 ? 'var(--green)' : 'var(--red)'}">${((s.wins / s.trades) * 100).toFixed(0)}%</td>
      <td class="num" style="color:${s.net >= 0 ? 'var(--green)' : 'var(--red)'}">${s.net >= 0 ? '+' : ''}${s.net.toFixed(4)}</td>
      <td class="num" style="color:var(--cyan)">${s.peak.toFixed(2)}x</td>
    </tr>`).join('');

  // insight line
  const holds = list.map(c => parseInt(c.hold_duration_sec) || 0).filter(h => h > 0);
  const avgHold = holds.length ? holds.reduce((a, b) => a + b, 0) / holds.length : 0;
  const volume = list.reduce((a, c) => a + (parseFloat(c.sol_spent) || 0), 0);
  document.getElementById('recapInsight').innerHTML = list.length
    ? `Periode ini engine mengeksekusi <b>${list.length} trade</b> (volume ${volume.toFixed(2)} SOL) dengan durasi rata-rata <b>${fmtHold(avgHold)}</b>. ` +
      `${wins} menang / ${list.length - wins} kalah. ${totalNet >= 0 ? 'Net profit' : 'Net loss'} <b style="color:${totalNet >= 0 ? 'var(--green)' : 'var(--red)'}">${Math.abs(totalNet).toFixed(4)} SOL</b>.`
    : 'Belum ada trade tertutup pada periode ini. Statistik diisi otomatis dari riwayat engine.';
}

function setRecapPeriod(tf) {
  recapPeriod = tf;
  renderRecap(true);
}

document.getElementById('recapTabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tf]');
  if (btn) setRecapPeriod(btn.dataset.tf);
});

async function loadEngineRecap(tf) {
  const box = document.getElementById('recapContentBox');
  if (engineRecapCache[tf]) { box.textContent = engineRecapCache[tf]; return; }
  box.textContent = 'Memuat laporan engine...';
  try {
    const res = await fetch(`/api/recap?timeframe=${tf}`);
    const data = await res.json();
    if (data.success && data.recap_html) {
      engineRecapCache[tf] = data.recap_html;
      box.textContent = data.recap_html;
    } else {
      box.textContent = data.error ? `Laporan engine tidak tersedia: ${data.error}` : 'Belum ada laporan engine untuk periode ini.';
    }
  } catch (e) {
    box.textContent = `Gagal memuat: ${e.message}`;
  }
}

/* ---------------- Portfolio tab ---------------- */

let pfSig = '';

function buildPortfolioData() {
  const open = state.activePositions;
  const closed = state.closedPositions;
  const B = parseFloat(state.stats.virtual_balance_sol) || 0;

  const events = [];
  for (const p of open) {
    const t = parseTs(p.created_at) || Date.now();
    events.push({ t, type: 'BUY', sym: p.symbol, sol: -(parseFloat(p.sol_spent) || 0), pos: p });
    const realized = parseFloat(p.realized_sol) || 0;
    if (realized > 0) events.push({ t: t + 1000, type: 'SELL', sym: p.symbol, sol: realized, pos: p });
  }
  for (const c of closed) {
    events.push({ t: parseTs(c.created_at) || 0, type: 'BUY', sym: c.symbol, sol: -(parseFloat(c.sol_spent) || 0), pos: c });
    events.push({ t: parseTs(c.closed_at) || (parseTs(c.created_at) || 0) + 1, type: 'CLOSE', sym: c.symbol, sol: parseFloat(c.realized_sol) || 0, pos: c });
  }

  // implied starting capital: current balance minus every cashflow so far
  const sumNet = events.reduce((a, e) => a + e.sol, 0);
  let bal = B - sumNet;
  events.sort((a, b) => a.t - b.t);

  const points = [];
  if (events.length) points.push({ t: events[0].t, v: bal });
  const ledger = [];
  for (const e of events) {
    bal += e.sol;
    points.push({ t: e.t, v: bal });
    ledger.unshift({ ...e, balAfter: bal });
  }

  // Cumulative realized PnL curve — starts at exactly 0 SOL, one point
  // per closed trade, chronological. This is the honest growth curve
  // (no balance-reconstruction artifacts).
  const closedSorted = state.closedPositions
    .filter(c => parseTs(c.closed_at))
    .sort((a, b) => parseTs(a.closed_at) - parseTs(b.closed_at));
  const pnlSeries = [];
  if (closedSorted.length) {
    pnlSeries.push({ t: parseTs(closedSorted[0].created_at) || parseTs(closedSorted[0].closed_at), v: 0 });
    let cum = 0;
    for (const c of closedSorted) {
      cum += netOf(c);
      pnlSeries.push({ t: parseTs(c.closed_at), v: cum, sym: c.symbol, trade: netOf(c) });
    }
  }

  return { balance: B, initial: events.length ? B - sumNet : B, points, ledger, pnlSeries };
}

function niceTicks(min, max, count = 4) {
  const range = (max - min) || 0.001;
  const rawStep = range / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + 1e-9; v += step) ticks.push(parseFloat(v.toFixed(10)));
  return { ticks, step };
}

function tickDecimals(step) {
  if (step < 0.001) return 4;
  if (step < 0.01) return 3;
  if (step < 0.1) return 3;
  return 2;
}

function fmtAxisTime(ts) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return `${hh}:${mm}`;
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${hh}:${mm}`;
}

/* Interactive equity chart: Y-axis ruler, time axis, hover crosshair +
   tooltip. Line lives in a stretched SVG, all text/markers are HTML
   overlays so nothing distorts. Domain always includes 0 SOL. */
function mountEquityChart(host, points) {
  if (!host) return;
  if (points.length < 2) {
    host.innerHTML = '<div class="eq-empty">KURVA PROFIT MUNCUL SETELAH TRADE PERTAMA DITUTUP</div>';
    return;
  }

  const vs = points.map(p => p.v);
  let min = Math.min(0, ...vs);           // curve is anchored to 0 SOL
  let max = Math.max(0, ...vs);
  const pad = (max - min) * 0.1 || Math.abs(max) * 0.05 || 0.001;
  min -= pad; max += pad;

  const TOP = 7, BOT = 7;                 // headroom (% of plot height)
  const n = points.length;
  const yPct = v => TOP + (1 - (v - min) / (max - min)) * (100 - TOP - BOT);
  const xPct = i => (i / (n - 1)) * 100;

  const { ticks, step } = niceTicks(min, max, 4);
  const dec = tickDecimals(step);

  // paths in a 1000x1000 stretched viewBox
  const px = i => (xPct(i) / 100) * 1000;
  const py = v => (yPct(v) / 100) * 1000;
  const pts = points.map((p, i) => `${px(i).toFixed(1)},${py(p.v).toFixed(1)}`);
  const line = `M${pts.join('L')}`;
  const area = `${line}L1000,1000L0,1000Z`;
  const up = vs[n - 1] >= 0;
  const color = up ? '#2fd77b' : '#ff5470';

  // x labels: 5 evenly spaced indices
  const idxs = [0, 0.25, 0.5, 0.75, 1].map(f => Math.round(f * (n - 1)));

  host.innerHTML = `
    <div class="eq-legend">
      <span class="eq-legend-name">PNL KUMULATIF</span>
      <b class="${up ? 'up' : 'down'}">${vs[n - 1] >= 0 ? '+' : ''}${vs[n - 1].toFixed(4)} SOL</b>
      <span class="eq-legend-dim">· mulai dari 0 SOL · ${n - 1} trade tertutup</span>
    </div>
    <svg class="eq-svg" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
      ${ticks.map(v => `<line class="eq-grid" vector-effect="non-scaling-stroke" x1="0" x2="1000" y1="${py(v).toFixed(1)}" y2="${py(v).toFixed(1)}"/>`).join('')}
      <defs><linearGradient id="pfGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${color}" stop-opacity="0.32"/>
        <stop offset="1" stop-color="${color}" stop-opacity="0"/>
      </linearGradient></defs>
      <path class="eq-area" d="${area}" fill="url(#pfGrad)"/>
      <path class="eq-glow" vector-effect="non-scaling-stroke" d="${line}" stroke="${color}"/>
      <path class="eq-line" vector-effect="non-scaling-stroke" d="${line}" stroke="${color}"/>
    </svg>
    <div class="eq-plot">
      <div class="eq-cross" hidden></div>
      <div class="eq-dotc" hidden></div>
      <div class="eq-last" style="left:${xPct(n - 1)}%;top:${yPct(vs[n - 1])}%"></div>
    </div>
    <div class="eq-yaxis">
      ${ticks.map(v => `<span style="top:${yPct(v)}%">${v >= 0 ? '+' : ''}${v.toFixed(dec)}</span>`).join('')}
    </div>
    <div class="eq-xaxis">${idxs.map(i => `<span>${fmtAxisTime(points[i].t)}</span>`).join('')}</div>
    <div class="eq-tip" hidden></div>`;

  // hover crosshair + tooltip
  const svg = host.querySelector('.eq-svg');
  const cross = host.querySelector('.eq-cross');
  const dot = host.querySelector('.eq-dotc');
  const tip = host.querySelector('.eq-tip');

  const move = e => {
    const rect = svg.getBoundingClientRect();
    const fx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const i = Math.round(fx * (n - 1));
    const p = points[i];
    const prev = points[Math.max(0, i - 1)];
    const d = p.v - prev.v;
    cross.hidden = false; dot.hidden = false; tip.hidden = false;
    cross.style.left = (fx * 100) + '%';
    dot.style.left = (fx * 100) + '%';
    dot.style.top = yPct(p.v) + '%';
    const px2 = Math.min(rect.width, Math.max(0, fx * rect.width));
    tip.style.left = Math.min(rect.width - 74, Math.max(74, px2)) + 'px';
    tip.style.top = `calc(${yPct(p.v)}% - 12px)`;
    const pnlTxt = `${p.v >= 0 ? '+' : ''}${p.v.toFixed(4)} SOL`;
    tip.innerHTML = p.sym
      ? `<b style="color:${color}">$${esc(p.sym)} ${pnlTxt}</b>
         <span>${fmtAxisTime(p.t)} · trade <i style="font-style:normal;color:${(p.trade || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${(p.trade || 0) >= 0 ? '+' : ''}${(p.trade || 0).toFixed(4)}</i></span>`
      : `<b style="color:${color}">${pnlTxt}</b>
         <span>${fmtAxisTime(p.t)} · titik awal</span>`;
  };
  const leave = () => { cross.hidden = true; dot.hidden = true; tip.hidden = true; };
  svg.addEventListener('mousemove', move);
  svg.addEventListener('mouseleave', leave);
}

function renderPortfolio(force) {
  const el = document.getElementById('portfolioView');
  if (!el) return;
  const stats = state.stats || {};
  const sig = `${stats.virtual_balance_sol}|${state.activePositions.map(p => p.id + ':' + p.realized_sol).join(',')}|${state.closedPositions.map(p => p.id).join(',')}`;
  if (!force && sig === pfSig) return;
  pfSig = sig;

  const data = buildPortfolioData();
  const B = data.balance;
  const pnl = B - data.initial;
  const pnlPct = data.initial > 0 ? (pnl / data.initial) * 100 : 0;

  const open = state.activePositions;
  const closed = state.closedPositions;
  const wins = closed.filter(isWinTrade).length;
  const wr = closed.length ? (wins / closed.length) * 100 : 0;
  const solPrice = networkState.sol_price_usd || 0;
  const openValueSol = open.reduce((a, p) => a + ((parseFloat(p.tokens_remaining) || 0) * (parseFloat(p.current_price_usd) || 0)) / (solPrice || 1), 0);
  const volume = closed.reduce((a, c) => a + (parseFloat(c.sol_spent) || 0), 0);

  el.innerHTML = `
    <div class="pf-head">
      <div class="kpi-label" style="justify-content:center">${I.wallet} Saldo Portofolio</div>
      <div class="pf-balance">${fmtSol(B)}<span class="unit">SOL</span></div>
      <div class="pf-balance-sub">
        <b class="${pnl >= 0 ? 'up' : 'down'}">${pnl >= 0 ? '+' : ''}${fmtSol(pnl)} SOL (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%)</b>
        · modal awal ${fmtSol(data.initial)} SOL
      </div>
    </div>
    <div class="pf-chart">
      <div class="eqchart" id="eqChart"></div>
      <div class="pf-chart-labels"><span>kurva profit (PnL kumulatif) · arahkan kursor untuk detail tiap trade</span><span>${data.pnlSeries.length} titik</span></div>
    </div>
    <div class="pf-stats">
      <div class="pf-stat"><div class="v">${closed.length + open.length}</div><div class="k">Total Transaksi</div></div>
      <div class="pf-stat"><div class="v" style="color:${wr >= 50 ? 'var(--green)' : 'var(--red)'}">${closed.length ? wr.toFixed(0) + '%' : '—'}</div><div class="k">Win Rate</div></div>
      <div class="pf-stat"><div class="v" style="color:var(--cyan)">${open.length}</div><div class="k">Posisi Terbuka</div></div>
      <div class="pf-stat"><div class="v">${openValueSol > 0 ? fmtSol(openValueSol) : '—'}</div><div class="k">Nilai Posisi (SOL)</div></div>
      <div class="pf-stat"><div class="v">${volume.toFixed(2)}</div><div class="k">Volume (SOL)</div></div>
      <div class="pf-stat"><div class="v" style="color:${pnl >= 0 ? 'var(--green)' : 'var(--red)'}">${pnl >= 0 ? '+' : ''}${fmtSol(pnl)}</div><div class="k">Realized PnL</div></div>
    </div>
    <div class="pf-section-title"><span>Riwayat Transaksi</span><span class="kpi-foot" style="text-transform:none;letter-spacing:0">${data.ledger.length} event</span></div>
    <div class="ledger">
      ${data.ledger.length === 0 ? `
        <div class="empty-state">
          <div class="orb">${I.wallet}</div>
          <div class="title">Belum ada transaksi</div>
          <div class="hint">Setiap buy, sell parsial, dan penutupan posisi tercatat di sini bersama saldo setelahnya.</div>
        </div>` :
      data.ledger.map(e => {
        const isBuy = e.type === 'BUY';
        const exit = ((e.pos && e.pos.exit_reason) || '').toUpperCase();
        const badge = isBuy ? ['buy', 'BUY'] : e.type === 'SELL' ? ['sell', 'SELL'] : exit.includes('TP') ? ['tp', 'TP'] : exit.includes('SL') ? ['sl', 'SL'] : ['sell', 'CLOSE'];
        const amt = e.sol;
        return `
        <div class="ledger-row">
          <span class="ledger-type ${badge[0]}">${badge[1]}</span>
          <div class="ledger-main">
            <div class="ledger-sym">$${esc(e.sym)}</div>
            <div class="ledger-sub">${relTime(e.t)}${e.type === 'CLOSE' && e.pos ? ' · ' + esc(e.pos.exit_reason || '') : e.type === 'SELL' ? ' · parsial (TP1)' : ''}</div>
          </div>
          <div class="ledger-right">
            <div class="ledger-amt" style="color:${amt >= 0 ? 'var(--green)' : 'var(--red)'}">${amt >= 0 ? '+' : '−'}${Math.abs(amt).toFixed(4)}</div>
            <div class="ledger-bal">saldo ${e.balAfter.toFixed(4)}</div>
          </div>
        </div>`;
      }).join('')}
    </div>`;

  mountEquityChart(document.getElementById('eqChart'), data.pnlSeries);
}

/* ---------------- Wallet ---------------- */

let walletLoaded = false;

async function loadWalletData() {
  try {
    const res = await fetch('/api/wallet');
    const data = await res.json();
    if (data.success && data.wallet) {
      walletLoaded = true;
      const w = data.wallet;
      document.getElementById('drawerWalletPubkey').textContent = w.public_key;
      document.getElementById('headerWalletPreview').textContent = `${w.public_key.slice(0, 4)}…${w.public_key.slice(-4)}`;
      document.getElementById('drawerWalletSol').textContent = `${fmtSol(w.sol_balance ?? 0)} SOL`;
      document.getElementById('settingBuySol').value = w.default_buy_sol ?? 0.1;
      document.getElementById('settingSlippage').value = w.slippage_pct ?? 15;
      walletAutoBuy = !!w.auto_buy_enabled;
      document.getElementById('autoBuySwitch').checked = walletAutoBuy;
      updateAutoChip();
      syncAutoBuyWarning();
    }
  } catch (e) {
    console.error('loadWalletData:', e);
  }
}

function syncAutoBuyWarning() {
  const on = document.getElementById('autoBuySwitch').checked;
  document.getElementById('autoBuyWarning').classList.toggle('hidden', !on);
  document.getElementById('autoDotReal').classList.toggle('off', !on);
  document.getElementById('autoStateReal').textContent = on ? 'AKTIF' : 'MATI';
}

function updateAutoChip() {
  const chip = document.getElementById('autoBuyChip');
  chip.classList.toggle('is-on', walletAutoBuy);
  chip.classList.toggle('is-off', !walletAutoBuy);
  document.getElementById('autoBuyChipState').textContent = walletAutoBuy ? 'ON' : 'OFF';
}

document.getElementById('autoBuySwitch').addEventListener('change', syncAutoBuyWarning);

async function saveWalletSettings() {
  const payload = {
    default_buy_sol: parseFloat(document.getElementById('settingBuySol').value) || 0.1,
    slippage_pct: parseFloat(document.getElementById('settingSlippage').value) || 15,
    auto_buy_enabled: document.getElementById('autoBuySwitch').checked
  };
  try {
    const res = await fetch('/api/wallet/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      walletAutoBuy = payload.auto_buy_enabled;
      updateAutoChip();
      syncAutoBuyWarning();
      toast('Pengaturan & automasi tersimpan!');
      if (payload.auto_buy_enabled) toast('Auto-buy RIIL aktif — engine akan eksekusi saldo asli.', 'error');
    } else {
      toast('Gagal: ' + (data.error || 'unknown'), 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

async function importWallet() {
  const input = document.getElementById('importPkInput');
  const pk = input.value.trim();
  if (!pk) { toast('Tempel private key dulu', 'info'); return; }
  if (!confirm('Ganti wallet engine dengan wallet dari private key ini?\n\nPastikan saldo wallet LAMA sudah dipindahkan — setelah diganti, dashboard tidak bisa memulihkan wallet lama.')) return;
  try {
    const res = await fetch('/api/wallet/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ private_key: pk })
    });
    const data = await res.json();
    if (data.success) {
      input.value = '';
      toast('Wallet berhasil diimport!');
      loadWalletData();
    } else {
      toast('Gagal: ' + (data.error || 'private key tidak valid'), 'error');
    }
  } catch (e) {
    toast('Gagal import: ' + e.message, 'error');
  }
}

async function revealPrivateKey() {
  if (!confirm('Tampilkan private key wallet trading ini? Pastikan tidak ada yang melihat layar kamu.')) return;
  try {
    const res = await fetch('/api/wallet/export');
    const data = await res.json();
    if (data.success && data.private_key_base58) {
      document.getElementById('pkValue').value = data.private_key_base58;
      document.getElementById('pkBox').classList.remove('hidden');
    } else {
      toast('Gagal: ' + (data.error || 'unknown'), 'error');
    }
  } catch (e) {
    toast('Gagal mengekspor: ' + e.message, 'error');
  }
}

function togglePkVisibility() {
  const el = document.getElementById('pkValue');
  const showing = el.type === 'text';
  el.type = showing ? 'password' : 'text';
  document.getElementById('pkToggle').textContent = showing ? 'Lihat' : 'Sembunyi';
}

/* ---------------- Chart modal ---------------- */

const chartModal = document.getElementById('chartModal');

function openChartModal(ca, symbol, name, priceStr) {
  if (!ca) return;
  currentModalCa = ca;
  document.getElementById('modalTokenSymbol').textContent = '$' + (symbol || 'TOKEN');
  document.getElementById('modalTokenName').textContent = name || '';
  document.getElementById('modalTokenPrice').textContent = priceStr ? '$' + fmtPrice(priceStr) : '';
  document.getElementById('modalExternalLink').href = `https://dexscreener.com/solana/${ca}`;
  document.getElementById('chartIframe').src = `https://dexscreener.com/solana/${ca}?embed=1&theme=dark&trades=0&info=0`;
  chartModal.classList.remove('hidden');
  requestAnimationFrame(() => chartModal.classList.add('open'));
}

function closeChartModal() {
  chartModal.classList.remove('open');
  setTimeout(() => {
    chartModal.classList.add('hidden');
    document.getElementById('chartIframe').src = '';
  }, 240);
}

/* ---------------- Quick CA checker ---------------- */

async function quickCheckCa() {
  const input = document.getElementById('headerCaInput');
  const ca = input.value.trim();
  if (!ca) { toast('Masukkan contract address dulu', 'info'); return; }

  toast(`Memeriksa CA ${ca.slice(0, 6)}…`, 'info');
  try {
    const res = await fetch(`/api/check_ca?ca=${encodeURIComponent(ca)}`);
    const data = await res.json();
    if (data.success && data.dex) {
      openChartModal(ca, data.dex.baseToken?.symbol || 'TOKEN', data.dex.baseToken?.name || '', data.dex.priceUsd);
      input.value = '';
    } else {
      toast(data.error || 'Token tidak ditemukan di Solana', 'error');
    }
  } catch (e) {
    toast('Gagal memeriksa CA: ' + e.message, 'error');
  }
}

document.getElementById('headerCaInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') quickCheckCa();
});

/* ---------------- Radar toolbar ---------------- */

document.getElementById('signalFilter').addEventListener('click', e => {
  const btn = e.target.closest('button[data-filter]');
  if (!btn) return;
  state.filter = btn.dataset.filter;
  document.querySelectorAll('#signalFilter button').forEach(b => b.classList.toggle('active', b === btn));
  state.domOrder = '';
  renderSignals();
});

document.getElementById('signalSort').addEventListener('change', e => {
  state.sort = e.target.value;
  state.domOrder = '';
  renderSignals();
});

/* ---------------- Side tabs ---------------- */

document.querySelectorAll('.side-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    state.sideTab = btn.dataset.tab;
    document.querySelectorAll('.side-tab').forEach(b => b.classList.toggle('active', b === btn));
    document.getElementById('sideTabContentOpen').classList.toggle('hidden', state.sideTab !== 'open');
    document.getElementById('sideTabContentHistory').classList.toggle('hidden', state.sideTab !== 'history');
  });
});

/* ---------------- Keyboard ---------------- */

document.addEventListener('keydown', e => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName);
  if (e.key === '/' && !typing) {
    e.preventDefault();
    document.getElementById('headerCaInput').focus();
  }
  if (e.key === 'Escape') {
    if (!chartModal.classList.contains('hidden')) closeChartModal();
    else if (!tradeModal.classList.contains('hidden')) closeTradeModal();
    else if (drawer.classList.contains('open')) closeDrawer();
    else {
      const pk = document.getElementById('pkBox');
      if (!pk.classList.contains('hidden')) { pk.classList.add('hidden'); document.getElementById('pkValue').value = ''; }
    }
  }
});

/* ---------------- Trade ticket (manual buy/sell) ---------------- */

const tradeModal = document.getElementById('tradeModal');
let trade = null;
let previewTimer = null;

function openTradeModal(opts) {
  trade = {
    action: opts.action || 'buy',
    ca: opts.ca || '',
    symbol: opts.symbol || 'TOKEN',
    price: opts.price || '',
    positionId: opts.positionId || null,
    tokensRemaining: opts.tokensRemaining || 0,
    solSpent: opts.solSpent || 0,
    mode: 'paper',
    percent: 100
  };

  const isBuy = trade.action === 'buy';
  document.getElementById('tradeTitle').textContent = isBuy ? `Beli $${trade.symbol}` : `Jual $${trade.symbol}`;
  document.getElementById('tradeSubtitle').textContent = isBuy
    ? (trade.price ? `Harga live $${fmtPrice(trade.price)}` : 'Market buy via engine')
    : `Posisi #${trade.positionId} · ${fmtSol(trade.tokensRemaining, 0)} token tersisa`;

  const icon = document.getElementById('tradeActionIcon');
  icon.className = `trade-action-icon ${isBuy ? 'buy' : 'sell'}`;

  document.getElementById('tradeModeWrap').classList.toggle('hidden', !isBuy);
  document.getElementById('tradeAmountWrap').classList.toggle('hidden', !isBuy);
  document.getElementById('tradePreview').classList.toggle('hidden', !isBuy);
  document.getElementById('tradePercentWrap').classList.toggle('hidden', isBuy);
  document.getElementById('tradeModeLiveBtn').disabled = !HAS_ENGINE_HINT;

  if (isBuy) {
    setTradeMode('paper');
    document.getElementById('tradeAmountInput').value = document.getElementById('settingBuySol')?.value || '0.1';
    scheduleTradePreview();
  } else {
    setTradePercent(100);
    updateSellInfo();
  }

  tradeModal.classList.remove('hidden');
  requestAnimationFrame(() => tradeModal.classList.add('open'));
}

let HAS_ENGINE_HINT = null;   // resolved from /api/network engine_modules
async function resolveEngineHint() {
  if (HAS_ENGINE_HINT !== null) return;
  try {
    const r = await fetch('/api/network');
    const d = await r.json();
    HAS_ENGINE_HINT = !!(d.engine && d.engine.engine_modules);
  } catch (e) { HAS_ENGINE_HINT = false; }
}

function closeTradeModal() {
  tradeModal.classList.remove('open');
  setTimeout(() => tradeModal.classList.add('hidden'), 240);
  trade = null;
}

function setTradeMode(mode) {
  trade.mode = mode;
  document.querySelectorAll('#tradeModeSeg button').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  const live = mode === 'live';
  document.getElementById('tradeModeHint').textContent = live
    ? 'Swap on-chain riil via Jupiter dari wallet engine. Review sebelum konfirmasi.'
    : 'Order virtual memakai saldo sandbox. Tidak ada dana riil yang tersentuh.';
  document.getElementById('tradeLiveWarn').classList.toggle('hidden', !live);
  updateTradeConfirm();
}

function updateTradeConfirm() {
  const btn = document.getElementById('tradeConfirmBtn');
  if (!trade) return;
  const isBuy = trade.action === 'buy';
  if (isBuy) {
    const amt = parseFloat(document.getElementById('tradeAmountInput').value) || 0;
    btn.textContent = trade.mode === 'live' ? `KONFIRMASI BELI RIIL — ${amt.toFixed(2)} SOL` : `Konfirmasi Beli — ${amt.toFixed(2)} SOL`;
    btn.classList.toggle('is-live', trade.mode === 'live');
  } else {
    btn.textContent = `Konfirmasi Jual — ${trade.percent}% $${trade.symbol}`;
    btn.classList.remove('is-live');
  }
}

function scheduleTradePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(fetchTradePreview, 450);
  updateTradeConfirm();
}

async function fetchTradePreview() {
  if (!trade || trade.action !== 'buy') return;
  const amount = parseFloat(document.getElementById('tradeAmountInput').value) || 0;
  const slippage = parseFloat(document.getElementById('settingSlippage')?.value || 15);
  document.getElementById('tpSlippage').textContent = `${slippage.toFixed(0)}%`;
  document.getElementById('tpTokens').textContent = 'menghitung…';
  document.getElementById('tpImpact').textContent = '—';
  if (amount <= 0) { document.getElementById('tpTokens').textContent = '—'; return; }
  try {
    const res = await fetch(`/api/trade/preview?ca=${encodeURIComponent(trade.ca)}&amount_sol=${amount}&slippage_pct=${slippage}`);
    const data = await res.json();
    if (data.success) {
      document.getElementById('tpTokens').textContent = data.tokens_out >= 1000
        ? data.tokens_out.toLocaleString('en-US', { maximumFractionDigits: 0 })
        : data.tokens_out.toLocaleString('en-US', { maximumFractionDigits: 4 });
      const impact = data.price_impact_pct;
      const impEl = document.getElementById('tpImpact');
      impEl.textContent = `${impact.toFixed(2)}%`;
      impEl.className = 'mono ' + (impact < 2 ? 'good' : impact < 5 ? '' : 'bad');
    } else {
      document.getElementById('tpTokens').textContent = 'rute tidak tersedia';
      document.getElementById('tpImpact').textContent = '—';
    }
  } catch (e) {
    document.getElementById('tpTokens').textContent = '—';
  }
}

function setTradePercent(pct) {
  trade.percent = pct;
  document.querySelectorAll('#tradePercentChips button').forEach(b => b.classList.toggle('active', +b.dataset.pct === pct));
  updateTradeConfirm();
}

function updateSellInfo() {
  if (!trade || trade.action !== 'sell') return;
  const solPrice = networkState.sol_price_usd || 0;
  const usdVal = trade.tokensRemaining * (trade.currentPrice || 0);
  const solVal = solPrice > 0 ? usdVal / solPrice : 0;
  document.getElementById('tradeSellInfo').innerHTML =
    `Modal masuk ${fmtSol(trade.solSpent, 3)} SOL · nilai kini ± <b class="mono" style="color:var(--lime)">${solVal > 0 ? fmtSol(solVal, 4) + ' SOL</b>' : '—'}`;
}

document.getElementById('tradeModeSeg').addEventListener('click', e => {
  const btn = e.target.closest('button[data-mode]');
  if (btn && !btn.disabled) setTradeMode(btn.dataset.mode);
});

document.getElementById('tradeAmountChips').addEventListener('click', e => {
  const btn = e.target.closest('button[data-amt]');
  if (!btn) return;
  document.getElementById('tradeAmountInput').value = btn.dataset.amt;
  document.querySelectorAll('#tradeAmountChips button').forEach(b => b.classList.toggle('active', b === btn));
  scheduleTradePreview();
});

document.getElementById('tradeAmountInput').addEventListener('input', () => {
  document.querySelectorAll('#tradeAmountChips button').forEach(b => b.classList.remove('active'));
  scheduleTradePreview();
});

document.getElementById('tradePercentChips').addEventListener('click', e => {
  const btn = e.target.closest('button[data-pct]');
  if (btn) setTradePercent(+btn.dataset.pct);
});

async function submitTrade() {
  if (!trade) return;
  const btn = document.getElementById('tradeConfirmBtn');
  const isBuy = trade.action === 'buy';
  const payload = {
    action: trade.action,
    mode: isBuy ? trade.mode : 'paper',
    ca: trade.ca,
    symbol: trade.symbol,
    user_id: '6166029678'
  };
  if (isBuy) {
    payload.amount_sol = parseFloat(document.getElementById('tradeAmountInput').value) || 0;
    payload.slippage_pct = parseFloat(document.getElementById('settingSlippage')?.value || 15);
    if (payload.amount_sol <= 0) { toast('Jumlah beli tidak valid', 'error'); return; }
    if (trade.mode === 'live' && !confirm('Kirim swap RIIL on-chain dengan dana asli dari wallet engine?')) return;
  } else {
    payload.position_id = trade.positionId;
    payload.percent = trade.percent;
  }

  btn.disabled = true;
  try {
    const res = await fetch('/api/trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success) {
      toast(data.message || 'Order dieksekusi!');
      closeTradeModal();
    } else {
      toast(data.error || 'Order gagal', 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- Network & engine status bar ---------------- */

let networkState = { sol_price_usd: 0, sol_change_24h_pct: 0, base_fee_sol: 0, priority_fee_sol: 0, engine: {} };

function fmtUptime(sec) {
  sec = parseInt(sec) || 0;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}j ${m}m` : `${m}m`;
}

async function pollNetwork() {
  try {
    const res = await fetch('/api/network');
    const d = await res.json();
    if (!d.success) return;
    networkState = d;
    HAS_ENGINE_HINT = !!(d.engine && d.engine.engine_modules);
    document.getElementById('tradeModeLiveBtn').disabled = !HAS_ENGINE_HINT;

    const ch = d.sol_change_24h_pct || 0;
    document.getElementById('sbSol').innerHTML =
      `<span class="sb-k">SOL</span> <b class="mono">$${fmtPrice(d.sol_price_usd)}</b> <span class="${ch >= 0 ? 'up' : 'down'}">${ch >= 0 ? '+' : ''}${ch.toFixed(1)}%</span>`;

    document.getElementById('sbGas').innerHTML =
      `<span class="sb-k">GAS</span> <b class="mono">${(d.base_fee_sol * 1e6).toFixed(0)}μ + ${(d.priority_fee_sol * 1e6).toFixed(0)}μ SOL</b>`;

    const e = d.engine || {};
    const ageS = e.last_tick_age_ms != null ? (e.last_tick_age_ms / 1000).toFixed(1) : '—';
    document.getElementById('sbEngine').innerHTML =
      `<span class="sb-k">ENGINE</span> <b class="mono">${(e.avg_sync_ms ?? 0).toFixed(1)}ms sync · tick ${ageS}s lalu</b>`;

    document.getElementById('sbWs').innerHTML =
      `<span class="sb-k">WS</span> <b class="mono">${e.ws_clients ?? 0} klien · uptime ${fmtUptime(e.uptime_sec)}</b>`;

    if (d.health) {
      const h = d.health;
      const el = document.getElementById('sbHealth');
      const cls = h.score >= 80 ? 'up' : h.score >= 50 ? 'warn' : 'down';
      el.innerHTML = `<span class="sb-k">HEALTH</span> <b class="mono ${cls}">${h.score}% ${h.status}</b>`;
      el.title = `Health engine ${h.score}/100 — DB: ${h.db_ok ? 'terhubung' : 'gagal dibaca'}, usia tick: ${h.tick_age_ms ?? '—'}ms, modul engine: ${h.engine_modules ? 'termuat' : 'tidak tersedia'}`;
    }

    if (trade && trade.action === 'sell') updateSellInfo();
  } catch (e) { /* status bar is non-critical */ }
}

/* ---------------- Page routing ---------------- */

const ROUTE_PAGE = { '/': 'terminal', '/portofolio': 'portofolio', '/evaluasi': 'evaluasi', '/recap': 'recap' };
const currentPage = ROUTE_PAGE[window.location.pathname.replace(/\/+$/, '') || '/'] || 'terminal';

function activatePage() {
  document.body.dataset.page = currentPage;
  document.getElementById('mainTerminal').classList.toggle('hidden', currentPage !== 'terminal');
  document.getElementById('pagePortofolio').classList.toggle('hidden', currentPage !== 'portofolio');
  document.getElementById('pageEvaluasi').classList.toggle('hidden', currentPage !== 'evaluasi');
  document.getElementById('pageRecap').classList.toggle('hidden', currentPage !== 'recap');
  document.querySelectorAll('.pagenav a').forEach(a => a.classList.toggle('active', a.dataset.nav === currentPage));
}

let engineTf = 'daily';
document.getElementById('engineTabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tf]');
  if (!btn) return;
  engineTf = btn.dataset.tf;
  document.querySelectorAll('#engineTabs button').forEach(b => b.classList.toggle('active', b === btn));
  loadEngineRecap(engineTf);
});

/* ---------------- Boot ---------------- */

activatePage();
if (currentPage === 'portofolio') renderPortfolio(true);
if (currentPage === 'evaluasi') renderRecap(true);
if (currentPage === 'recap') loadEngineRecap(engineTf);

httpBootstrap();
connectWS();
loadWalletData();          // header wallet badge + auto-buy state
resolveEngineHint();
pollNetwork();
setInterval(pollNetwork, 10000);
setTimeout(() => renderSignalAges(), 500);
