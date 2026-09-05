# -*- coding: utf-8 -*-
import io

# ---------- index.html ----------
path = 'public/index.html'
src = io.open(path, encoding='utf-8').read()

old = """        <!-- Automation status chip -->
        <button id="autoBuyChip" class="auto-chip is-off" onclick="openDrawer('wallet')" title="Status automasi engine — klik untuk atur">
          <svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4m0 12v4M4.9 4.9l2.8 2.8m8.6 8.6 2.8 2.8M2 12h4m12 0h4M4.9 19.1l2.8-2.8m8.6-8.6 2.8-2.8"/></svg>
          AUTO <span id="autoBuyChipState">OFF</span>
        </button>

"""
assert old in src, 'auto chip not found'
src = src.replace(old, "")

old = """        Recap
      </a>
    </nav>"""
new = """        Recap
      </a>
      <a href="/logs" data-nav="logs">
        <svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
        Logs
      </a>
    </nav>"""
assert src.count(old) == 1, 'nav anchor not found'
src = src.replace(old, new)

old = """    <!-- ==================== STATUS BAR (FOOTER) ==================== -->"""
new = """    <!-- ==================== PAGE: LOGS ==================== -->
    <main class="page hidden" id="pageLogs">
      <div class="page-inner" style="max-width:none">
        <div class="page-head">
          <div>
            <h1 class="page-title">System Logs</h1>
            <p class="page-sub">Watchdog · Alerts · Engine · Trade — dikumpulkan bridge secara live, buffer 800 event.</p>
          </div>
          <div class="log-toolbar">
            <div class="segmented" id="logFilter">
              <button data-cat="ALL" class="active">Semua</button>
              <button data-cat="ENGINE">Engine</button>
              <button data-cat="TRADE">Trade</button>
              <button data-cat="WATCHDOG">Watchdog</button>
              <button data-cat="ALERTS">Alerts</button>
              <button data-cat="SYS">Sys</button>
            </div>
            <input id="logSearch" class="input" placeholder="Cari pesan..." style="width:170px">
            <button class="btn" id="logAutoBtn">Auto-scroll: ON</button>
          </div>
        </div>

        <section class="page-card" style="padding:0">
          <div id="logConsole" class="log-console mono"></div>
        </section>

        <section class="page-card hidden" id="telemetryCard">
          <div class="drawer-section-title"><span>[📡] Engine Telemetry</span><span class="sub">system_telemetry_history — 40 baris terakhir</span></div>
          <div class="table-wrap">
            <table class="data-table" id="telemetryTable">
              <thead><tr><th>Waktu (UTC)</th><th class="num">RPC Slot</th><th class="num">RPC ms</th><th class="num">Jupiter ms</th><th class="num">DexScreener ms</th><th class="num">Rugcheck ms</th><th class="num">Jito ms</th><th class="num">Posisi</th><th class="num">Sinyal</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </section>

        <section class="page-card hidden" id="engineFileCard">
          <div class="drawer-section-title"><span>[📄] Engine Log File</span><span class="sub mono">ENGINE_LOG_FILE</span></div>
          <div id="engineFileBox" class="recap-box" style="max-height:320px;overflow-y:auto"></div>
        </section>
      </div>
    </main>

    <!-- ==================== STATUS BAR (FOOTER) ==================== -->"""
assert src.count(old) == 1, 'footer anchor not found'
src = src.replace(old, new)
io.open(path, 'w', encoding='utf-8', newline='\n').write(src)
print('index.html ok')

# ---------- app.js ----------
path = 'public/app.js'
src = io.open(path, encoding='utf-8').read()

old = "const ROUTE_PAGE = { '/': 'terminal', '/portofolio': 'portofolio', '/evaluasi': 'evaluasi', '/recap': 'recap' };"
new = "const ROUTE_PAGE = { '/': 'terminal', '/portofolio': 'portofolio', '/evaluasi': 'evaluasi', '/recap': 'recap', '/logs': 'logs' };"
assert src.count(old) == 1, 'route not found'
src = src.replace(old, new)

old = "  document.getElementById('pageRecap').classList.toggle('hidden', currentPage !== 'recap');"
new = ("  document.getElementById('pageRecap').classList.toggle('hidden', currentPage !== 'recap');\n"
       "  document.getElementById('pageLogs').classList.toggle('hidden', currentPage !== 'logs');")
assert src.count(old) == 1, 'activatePage not found'
src = src.replace(old, new)

old = """function updateAutoChip() {
  const chip = document.getElementById('autoBuyChip');"""
new = """function updateAutoChip() {
  const chip = document.getElementById('autoBuyChip');
  if (!chip) return;"""
assert src.count(old) == 1, 'updateAutoChip not found'
src = src.replace(old, new)

old = """activatePage();
if (currentPage === 'portofolio') renderPortfolio(true);
if (currentPage === 'evaluasi') renderRecap(true);
if (currentPage === 'recap') { fetchRecapSignals(); renderEnginePerformance(true); renderMilestones(); }"""
new = """/* ---------------- Logs page (/logs) ---------------- */

let logLastId = 0;
let logCat = 'ALL';
let logQuery = '';
let logAuto = true;
const logStore = [];

function logLineEl(e) {
  const d = new Date(e.ts * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const el = document.createElement('div');
  el.className = 'log-line cat-' + e.cat + ' sev-' + e.sev;
  el.innerHTML = '<span class="lt">' + hh + ':' + mi + ':' + ss + '</span> <span class="lc">[' + e.cat + ']</span> <span class="lm">' + esc(e.msg) + '</span>';
  return el;
}

function logMatches(e) {
  if (logCat !== 'ALL' && e.cat !== logCat) return false;
  if (logQuery && !e.msg.toLowerCase().includes(logQuery)) return false;
  return true;
}

function appendLogDom(e) {
  const con = document.getElementById('logConsole');
  if (!con) return;
  const nearBottom = con.scrollHeight - con.scrollTop - con.clientHeight < 60;
  if (logMatches(e)) {
    con.appendChild(logLineEl(e));
    while (con.children.length > 800) con.firstChild.remove();
    if (logAuto && nearBottom) con.scrollTop = con.scrollHeight;
  }
}

function refilterLogs() {
  const con = document.getElementById('logConsole');
  if (!con) return;
  con.innerHTML = '';
  for (const e of logStore.slice(-500)) {
    if (logMatches(e)) con.appendChild(logLineEl(e));
  }
  if (logAuto) con.scrollTop = con.scrollHeight;
}

async function pollLogs() {
  if (currentPage !== 'logs') return;
  try {
    const res = await fetch('/api/logs?since_id=' + logLastId);
    const data = await res.json();
    if (!data.success) return;
    logLastId = data.last_id || logLastId;
    let added = false;
    for (const e of data.entries) {
      logStore.push(e);
      if (logStore.length > 800) logStore.shift();
      appendLogDom(e);
      added = true;
    }
    if (added || !document.getElementById('telemetryCard').dataset.done) {
      renderTelemetry(data.telemetry || []);
      renderEngineFile(data);
    }
  } catch (err) { /* logs are non-critical */ }
}

function fmtMs(v) {
  if (v === null || v === undefined || v === '') return '\u2014';
  const n = parseFloat(v);
  if (!isFinite(n)) return '\u2014';
  return n.toFixed(n < 10 ? 1 : 0);
}

function renderTelemetry(rows) {
  const card = document.getElementById('telemetryCard');
  if (!card) return;
  card.dataset.done = '1';
  if (!rows.length) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const tb = document.querySelector('#telemetryTable tbody');
  tb.innerHTML = rows.map(r => (
    '<tr>' +
    '<td class="mono" style="font-size:10.5px">' + esc(String(r.timestamp || '').replace('T', ' ').slice(0, 19)) + '</td>' +
    '<td class="num">' + (parseInt(r.rpc_slot) || '\u2014') + '</td>' +
    '<td class="num">' + fmtMs(r.rpc_latency_ms) + '</td>' +
    '<td class="num">' + fmtMs(r.jupiter_latency_ms) + '</td>' +
    '<td class="num">' + fmtMs(r.dexscreener_latency_ms) + '</td>' +
    '<td class="num">' + fmtMs(r.rugcheck_latency_ms) + '</td>' +
    '<td class="num">' + fmtMs(r.jito_latency_ms) + '</td>' +
    '<td class="num">' + (r.active_positions ?? '\u2014') + '</td>' +
    '<td class="num">' + (r.total_signals ?? '\u2014') + '</td>' +
    '</tr>'
  )).join('');
}

function renderEngineFile(data) {
  const card = document.getElementById('engineFileCard');
  if (!card) return;
  if (!data.engine_file_available || !Array.isArray(data.engine_log)) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const box = document.getElementById('engineFileBox');
  const text = data.engine_log.join('\n');
  const hash = String(data.engine_log.length) + text.slice(-40);
  if (box.dataset.hash !== hash) {
    box.dataset.hash = hash;
    box.textContent = text;
    box.scrollTop = box.scrollHeight;
  }
}

document.getElementById('logFilter').addEventListener('click', e => {
  const btn = e.target.closest('button[data-cat]');
  if (!btn) return;
  logCat = btn.dataset.cat;
  document.querySelectorAll('#logFilter button').forEach(b => b.classList.toggle('active', b === btn));
  refilterLogs();
});

document.getElementById('logSearch').addEventListener('input', e => {
  logQuery = e.target.value.trim().toLowerCase();
  refilterLogs();
});

document.getElementById('logAutoBtn').addEventListener('click', e => {
  logAuto = !logAuto;
  e.currentTarget.textContent = 'Auto-scroll: ' + (logAuto ? 'ON' : 'OFF');
  if (logAuto) {
    const con = document.getElementById('logConsole');
    if (con) con.scrollTop = con.scrollHeight;
  }
});

setInterval(pollLogs, 3000);

activatePage();
if (currentPage === 'portofolio') renderPortfolio(true);
if (currentPage === 'evaluasi') renderRecap(true);
if (currentPage === 'recap') { fetchRecapSignals(); renderEnginePerformance(true); renderMilestones(); }
if (currentPage === 'logs') pollLogs();"""
assert src.count(old) == 1, 'boot not found'
src = src.replace(old, new)

io.open(path, 'w', encoding='utf-8', newline='\n').write(src)
print('app.js ok')
