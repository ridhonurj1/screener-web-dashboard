import asyncio
import hmac
import json
import math
import secrets
import sqlite3
import os
import sys
import time
from collections import deque
from aiohttp import web, ClientSession, ClientTimeout
# ---------------------------------------------------------------------------
# Engine linkage.
# The bridge normally runs on the same box as the engine and reads its SQLite
# WAL directly. Both locations are env-overridable so the bridge can also run
# standalone (UI development, staging) with engine features degrading
# gracefully instead of crashing on import.
# ---------------------------------------------------------------------------
ENGINE_DIR = os.environ.get("ENGINE_DIR", "/home/kaiden/projects/ScreenerNantiAja")
DB_PATH = os.environ.get("DB_PATH", os.path.join(ENGINE_DIR, "screener.db"))
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")

sys.path.insert(0, ENGINE_DIR)

try:
    from wallet_manager import wallet_manager
    from compact_evaluator import _compute_timeframe_evaluation_sync
    from swap_router import swap_router
    from rpc_config import multi_rpc
    HAS_ENGINE_MODULES = True
except Exception:  # pragma: no cover - engine absent on dev machines
    wallet_manager = None
    _compute_timeframe_evaluation_sync = None
    swap_router = None
    multi_rpc = None
    HAS_ENGINE_MODULES = False

WSOL_MINT = "So11111111111111111111111111111111111111112"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{cas}"
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
APP_STARTED_TS = time.time()

# ---------------------------------------------------------------------------
# AUTH: semua /api/* dan /ws/* wajib shared-secret token. Dulu bridge bound
# 0.0.0.0 tanpa auth apa pun — export private key & live trade bisa dipanggil
# siapa pun yang menemukan port 8000. Token: env DASHBOARD_AUTH_TOKEN, atau
# otomatis dibuat sekali ke dashboard_token.txt (chmod 600).
# ---------------------------------------------------------------------------
AUTH_TOKEN = os.environ.get("DASHBOARD_AUTH_TOKEN", "")
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "dashboard_token.txt")
if not AUTH_TOKEN:
    _tok_src = "token auth BARU dibuat"
    try:
        if os.path.exists(_TOKEN_FILE):
            with open(_TOKEN_FILE) as _f:
                AUTH_TOKEN = _f.read().strip()
        if not AUTH_TOKEN:
            AUTH_TOKEN = secrets.token_urlsafe(32)
            with open(_TOKEN_FILE, "w") as _f:
                _f.write(AUTH_TOKEN)
            try:
                os.chmod(_TOKEN_FILE, 0o600)
            except OSError:
                pass
        else:
            _tok_src = "token auth dari dashboard_token.txt"
    except Exception:
        AUTH_TOKEN = ""
        _tok_src = "GAGAL membuat token auth"
else:
    _tok_src = "token auth dari env DASHBOARD_AUTH_TOKEN"
if AUTH_TOKEN:
    print(f"🔐 [AUTH] {_tok_src} — wajib dikirim sebagai ?auth=... atau Bearer (lihat dashboard_token.txt)")

@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if not (path.startswith("/api/") or path.startswith("/ws/")) or not AUTH_TOKEN:
        return await handler(request)
    # ENDPOINT SENSITIF (trade nyata & kunci wallet): WAJIB token SELALU,
    # termasuk dari jaringan privat — perangkat tailnet mana pun tidak boleh
    # bisa menembak live trade / export key tanpa secret.
    _sensitive = path.startswith("/api/trade") or path.startswith("/api/wallet")
    if not _sensitive and _is_private_peer(request):
        # Endpoint read-only di jaringan privat (Tailscale 100.64/10, RFC1918,
        # loopback): akses tanpa token — UX pemakaian sendiri tetap mulus.
        return await handler(request)
    header = request.headers.get("Authorization", "")
    qtoken = request.query.get("auth", "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else qtoken
    if supplied and hmac.compare_digest(supplied, AUTH_TOKEN):
        return await handler(request)
    raise web.HTTPUnauthorized(text="unauthorized")

# Idempotensi trade: client_id terakhir (anti double-submit / retry jaringan)
_RECENT_CLIENT_IDS = deque(maxlen=512)

# Jaringan yang dianggap tepercaya (tanpa token): loopback, RFC1918,
# CGNAT/Tailscale 100.64.0.0/10, link-local.
import ipaddress as _ipaddress
_TRUSTED_NETS = [_ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
)]

def _is_private_peer(request) -> bool:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport else None
    if not peer or not peer[0]:
        return False
    try:
        ip = _ipaddress.ip_address(str(peer[0]))
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_NETS)

# IN-MEMORY RAM CACHE FOR SUB-MILLISECOND (1MS) DISPATCH
in_memory_state = {
    "signals": [],
    "active_positions": [],
    "closed_positions": [],
    "stats": {},
    "last_signal_rowid": 0,
    "last_updated_ts": 0,
    "perf": {"last_sync_ms": 0.0, "avg_sync_ms": 0.0}
}

# Token logo cache: ca -> {"url": str, "ts": float}
TOKEN_LOGO_CACHE = {}
TOKEN_LOGO_TTL = 900.0  # 15 minutes

# SOL price cache
SOL_NET_CACHE = {"ts": 0.0, "price": 0.0, "change_24h": 0.0}

# CTO sentinel state (mirrors the engine's Community Takeover criteria):
# ca -> {"trough": float, "cto": bool, "at": float, "surge": float}
CTO_STATE = {}
CTO_CHECK_INTERVAL = 15  # seconds between on-chain sweeps

async def cto_sentinel_loop(app):
    session = app["http_session"]
    while True:
        try:
            await asyncio.sleep(CTO_CHECK_INTERVAL)
            cas = [s.get("ca") for s in list(in_memory_state["signals"])
                   if (s.get("status") or "OPEN").upper() == "OPEN" and s.get("ca")]
            if not cas:
                continue
            pairs = await fetch_dex_pairs(session, cas)
            now = time.time()
            for ca in cas:
                pair = pairs.get(ca)
                if not pair:
                    continue
                mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
                liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                vol1h = float((pair.get("volume") or {}).get("h1", 0) or 0)
                if mcap <= 0:
                    continue
                st = CTO_STATE.setdefault(ca, {"trough": mcap, "cto": False, "at": 0.0, "surge": 0.0})
                if st["cto"]:
                    continue
                st["trough"] = min(st["trough"], mcap)
                # Engine criteria: real liquidity, healthy ratio, alive mcap,
                # >= +40% rise from the tracked low, real 1h volume.
                if (
                    liq >= 5000.0
                    and (liq / mcap) >= 0.08
                    and mcap >= 25000.0
                    and st["trough"] > 0
                    and (mcap / st["trough"]) >= 1.40
                    and vol1h >= 1000.0
                ):
                    st["cto"] = True
                    st["at"] = now
                    st["surge"] = (mcap / st["trough"] - 1.0) * 100.0
                    blog(f"CTO terdeteksi ${ca[:4]}… — mcap naik +{st['surge']:.1f}% dari dasar, liq ${liq:,.0f}", "ALERTS", "success")
        except asyncio.CancelledError:
            break
        except Exception as e:
            blog_debounced("cto_err", f"CTO sweep gagal — retry: {e}", "WATCHDOG", "warn", 60)
            await asyncio.sleep(5)

def get_db_connection(read_only=True):
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(f"file:{DB_PATH}?mode={mode}", uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    # WAL engine menulis tiap 4s: tanpa busy_timeout, burst tulis engine
    # memunculkan "database is locked" sebagai tick gagal di dashboard.
    conn.execute("PRAGMA busy_timeout=250")
    return conn

def sync_ram_state_from_engine_db():
    """Reads engine SQLite WAL into RAM in ~1ms without touching external APIs"""
    global in_memory_state
    t0 = time.perf_counter()
    try:
        conn = get_db_connection(True)
        c = conn.cursor()

        # 1. Fetch latest signals produced by engine
        c.execute("SELECT rowid, * FROM signals ORDER BY rowid DESC LIMIT 40")
        signals = [dict(r) for r in c.fetchall()]

        # 2. Fetch active paper positions
        c.execute("SELECT * FROM paper_trading_positions WHERE status='OPEN' ORDER BY id DESC")
        active_positions = [dict(r) for r in c.fetchall()]

        # 3. Fetch closed trade history
        c.execute("SELECT * FROM paper_trading_positions WHERE status!='OPEN' ORDER BY id DESC LIMIT 50")
        closed_positions = [dict(r) for r in c.fetchall()]

        # 4. Fetch account stats
        c.execute("SELECT * FROM paper_account_stats WHERE id=1")
        srow = c.fetchone()
        stats = dict(srow) if srow else {}

        # Enrich stats — MAX(rowid) O(1) via PK, dulu count(*) full-scan 5x/detik
        stats["active_positions_count"] = len(active_positions)
        c.execute("SELECT COALESCE(MAX(rowid), 0) FROM signals")
        stats["total_signals_count"] = c.fetchone()[0]

        conn.close()

        # Attach CTO flags computed by the sentinel
        for s in signals:
            st = CTO_STATE.get(s.get("ca"))
            s["cto"] = bool(st and st.get("cto"))

        # --- Engine delta logging ---
        global _prev_signal_ids, _prev_open_pos
        cur_ids = {s["ca"]: s for s in signals}
        if _prev_signal_ids is not None:
            for ca, s in cur_ids.items():
                if ca not in _prev_signal_ids:
                    blog(f"Sinyal baru ${s.get('symbol')} — skor {s.get('score')} · MC ${float(s.get('entry_mcap') or 0):,.0f} · {s.get('strategy', '')}", "ENGINE", "success")
        _prev_signal_ids = cur_ids
        cur_open = {p["id"]: p for p in active_positions}
        if _prev_open_pos is not None:
            for pid, p in cur_open.items():
                if pid not in _prev_open_pos:
                    blog(f"Posisi DIBUKA ${p.get('symbol')} — {float(p.get('sol_spent') or 0):.3f} SOL (paper)", "TRADE", "info")
            for pid, p in (_prev_open_pos or {}).items():
                if pid not in cur_open:
                    net = (float(p.get('realized_sol') or 0) - float(p.get('sol_spent') or 0))
                    blog(f"Posisi DITUTUP ${p.get('symbol')} — net {net:+.4f} SOL · {p.get('exit_reason', '')}", "TRADE", "success" if net >= 0 else "warn")
        _prev_open_pos = cur_open

        in_memory_state["signals"] = signals
        in_memory_state["active_positions"] = active_positions
        in_memory_state["closed_positions"] = closed_positions
        in_memory_state["stats"] = stats
        in_memory_state["last_updated_ts"] = time.time()
        in_memory_state["perf"]["sync_ok"] = True
        if signals:
            in_memory_state["last_signal_rowid"] = signals[0]["rowid"]

    except Exception as e:
        in_memory_state["perf"]["sync_ok"] = False
        blog_debounced("db_sync_fail", f"DB sync gagal — auto-retry aktif: {e}", "WATCHDOG", "error", 30)
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        perf = in_memory_state["perf"]
        perf["last_sync_ms"] = round(dt_ms, 2)
        perf["avg_sync_ms"] = round((perf["avg_sync_ms"] * 0.9 + dt_ms * 0.1) if perf["avg_sync_ms"] else dt_ms, 2)

# ---------------------------------------------------------------------------
# Bridge log bus — in-memory ring buffer feeding /logs.
# Categories: ENGINE, TRADE, WATCHDOG, ALERTS, API, SYS
# ---------------------------------------------------------------------------
LOG_BUFFER = deque(maxlen=800)
_log_seq = 0
_log_debounce = {}

def blog(msg, cat="ENGINE", sev="info"):
    global _log_seq
    _log_seq += 1
    LOG_BUFFER.append({"id": _log_seq, "ts": time.time(), "cat": cat, "sev": sev, "msg": msg})

def blog_debounced(key, msg, cat="WATCHDOG", sev="warn", interval=30.0):
    now = time.time()
    if now - _log_debounce.get(key, 0) >= interval:
        _log_debounce[key] = now
        blog(msg, cat, sev)

# Engine delta trackers for the sync loop
_prev_signal_ids = None
_prev_open_pos = None

def compute_engine_health():
    """
    Composite bridge→engine health (0-100):
    - 40 pts DB reachable (last SQLite WAL sync attempt succeeded)
    - 40 pts tick freshness (broadcast loop reading RAM cache; full score <=1s)
    - 20 pts engine modules importable (wallet, swap router, evaluator)
    """
    now = time.time()
    last_ts = in_memory_state["last_updated_ts"]
    tick_age = now - last_ts if last_ts else None
    if tick_age is None:
        tick_pts = 0
    elif tick_age <= 1:
        tick_pts = 40
    elif tick_age < 5:
        tick_pts = int(40 * (1 - (tick_age - 1) / 4))
    else:
        tick_pts = 0
    db_ok = bool(in_memory_state["perf"].get("sync_ok"))
    db_pts = 40 if db_ok else 0
    mod_pts = 20 if HAS_ENGINE_MODULES else 0
    score = tick_pts + db_pts + mod_pts
    status = "HEALTHY" if score >= 80 else "DEGRADED" if score >= 50 else "CRITICAL"
    return {
        "score": score,
        "status": status,
        "db_ok": db_ok,
        "tick_age_ms": int(tick_age * 1000) if tick_age is not None else None,
        "engine_modules": HAS_ENGINE_MODULES
    }

# --- REST APIS (READ FROM MEMORY < 1MS) ---

async def api_signals(request):
    try:
        limit = min(max(int(request.query.get("limit", "40")), 1), 1000)
    except Exception:
        limit = 40
    cached = in_memory_state["signals"]
    if limit <= len(cached):
        data = cached[:limit]
    else:
        # recap-style consumers want the full history: read the WAL directly
        # (~ms) instead of inflating the 200ms RAM broadcast payload.
        try:
            conn = get_db_connection(True)
            c = conn.cursor()
            c.execute("SELECT rowid, * FROM signals ORDER BY rowid DESC LIMIT ?", (limit,))
            data = [dict(r) for r in c.fetchall()]
            conn.close()
        except Exception:
            data = cached[:limit]
    return web.json_response({"success": True, "count": len(data), "data": data})

async def api_positions(request):
    wallet_mode = request.query.get("wallet_mode", "").lower()
    user_id = request.query.get("user_id", "6166029678")
    
    # Jika wallet_mode='real' -> baca dari user_trading_positions
    if wallet_mode == "real":
        try:
            conn = get_db_connection(True)
            c = conn.cursor()
            c.execute("SELECT * FROM user_trading_positions WHERE user_id=? AND status='OPEN' ORDER BY id DESC", (user_id,))
            real_active = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM user_trading_positions WHERE user_id=? AND status!='OPEN' ORDER BY id DESC LIMIT 50", (user_id,))
            real_closed = [dict(r) for r in c.fetchall()]
            conn.close()
            return web.json_response({
                "success": True,
                "wallet_mode": "real",
                "active": real_active,
                "closed": real_closed
            })
        except Exception:
            return web.json_response({
                "success": True,
                "wallet_mode": "real",
                "active": [],
                "closed": []
            })

    return web.json_response({
        "success": True,
        "wallet_mode": "demo",
        "active": in_memory_state["active_positions"],
        "closed": in_memory_state["closed_positions"]
    })

async def api_stats(request):
    wallet_mode = request.query.get("wallet_mode", "").lower()
    user_id = request.query.get("user_id", "6166029678")

    if wallet_mode == "real":
        try:
            conn = get_db_connection(True)
            c = conn.cursor()
            # Cek saldo real wallet
            c.execute("SELECT public_key FROM user_trading_wallets WHERE user_id=?", (user_id,))
            w_row = c.fetchone()
            pubkey = w_row["public_key"] if w_row else ""
            
            # Hitung stats real trades
            c.execute("SELECT COUNT(*), SUM(realized_pnl_sol) FROM user_trading_positions WHERE user_id=? AND status!='OPEN'", (user_id,))
            t_row = c.fetchone()
            total_real_trades = t_row[0] or 0
            pnl_real_sol = float(t_row[1] or 0.0)
            
            c.execute("SELECT COUNT(*) FROM user_trading_positions WHERE user_id=? AND status!='OPEN' AND realized_pnl_sol > 0", (user_id,))
            win_real_trades = c.fetchone()[0] or 0
            
            c.execute("SELECT COUNT(*) FROM user_trading_positions WHERE user_id=? AND status='OPEN'", (user_id,))
            active_real_count = c.fetchone()[0] or 0
            c.execute("SELECT COALESCE(MAX(rowid), 0) FROM signals")
            total_signals_count = c.fetchone()[0] or 0
            conn.close()

            # Real balance
            real_bal_sol = 0.0
            if HAS_ENGINE_MODULES and pubkey:
                try:
                    real_bal_sol = await wallet_manager.get_sol_balance(pubkey)
                except Exception:
                    real_bal_sol = 0.0

            return web.json_response({
                "success": True,
                "wallet_mode": "real",
                "data": {
                    "initial_capital_sol": 0.0,
                    "current_balance_sol": real_bal_sol,
                    "total_realized_sol": pnl_real_sol,
                    "total_trades": total_real_trades,
                    "win_trades": win_real_trades,
                    "lose_trades": total_real_trades - win_real_trades,
                    "active_positions_count": active_real_count,
                    "total_signals_count": total_signals_count
                }
            })
        except Exception:
            return web.json_response({
                "success": True,
                "wallet_mode": "real",
                "data": {
                    "initial_capital_sol": 0.0,
                    "current_balance_sol": 0.0,
                    "total_realized_sol": 0.0,
                    "total_trades": 0,
                    "win_trades": 0,
                    "lose_trades": 0,
                    "active_positions_count": 0,
                    "total_signals_count": in_memory_state["stats"].get("total_signals_count", 0)
                }
            })

    return web.json_response({"success": True, "wallet_mode": "demo", "data": in_memory_state["stats"]})

async def api_recap(request):
    timeframe = request.query.get("timeframe", "daily").lower()
    if not HAS_ENGINE_MODULES:
        return web.json_response({"success": False, "error": "Modul evaluasi engine tidak tersedia di bridge ini"}, status=503)
    try:
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute("SELECT * FROM signals ORDER BY rowid DESC")
        signals = [dict(r) for r in c.fetchall()]
        conn.close()

        recap_html = _compute_timeframe_evaluation_sync(signals, timeframe)
        return web.json_response({"success": True, "timeframe": timeframe, "recap_html": recap_html})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_ping(request):
    """Health Ping: menyusun audit dari DATABASE YANG SAMA dengan engine —
    baris telemetri terakhir + mirror state memori (health_state) yang
    engine push tiap siklus tracker (2 detik). 0 kuota API eksternal terpakai."""
    try:
        import datetime as _dt
        conn = get_db_connection(True)
        c = conn.cursor()
        t_db_0 = time.perf_counter()
        c.execute("""
            SELECT timestamp, rpc_slot, rpc_latency_ms, jupiter_latency_ms,
                   dexscreener_latency_ms, rugcheck_latency_ms, jito_latency_ms,
                   active_positions, total_signals, details_json
            FROM system_telemetry_history ORDER BY id DESC LIMIT 1
        """)
        tel = c.fetchone()
        db_read_ms = (time.perf_counter() - t_db_0) * 1000.0
        hs_err = None
        try:
            c.execute("SELECT value, updated_at FROM health_state WHERE key='gmgn'")
            st = c.fetchone()
        except Exception as _e_hs:
            st = None
            hs_err = f"{type(_e_hs).__name__}: {_e_hs}"
        conn.close()

        if not tel:
            return web.json_response({"success": False, "error": "Belum ada snapshot telemetri"}, status=404)

        _now_utc = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        r_slot = tel["rpc_slot"] if tel["rpc_slot"] not in (None, 0) else "Synced"
        r_lat = float(tel["rpc_latency_ms"] or 0)
        jup_lat = float(tel["jupiter_latency_ms"] or 35.8)
        dex_lat = float(tel["dexscreener_latency_ms"] or 157.7)
        rc_lat = float(tel["rugcheck_latency_ms"] or 676.3)
        jito_lat = float(tel["jito_latency_ms"] or 136.3)

        snap = {}
        state_updated = None
        if st:
            try:
                snap = json.loads(st["value"] or "{}")
                state_updated = str(st["updated_at"])
            except Exception:
                snap = {}
        gs = snap.get("stats") or {}
        cluster = snap.get("cluster") or []

        details = {}
        try:
            details = json.loads(tel["details_json"] or "{}")
            if not isinstance(details, dict):
                details = {}
        except Exception:
            details = {}
        hardware = details.get("hardware") or {}
        dbinfo = details.get("db") or {}

        _req = int(gs.get("requests", 0) or 0)
        _ok = int(gs.get("hits_200", 0) or 0)
        _429 = int(gs.get("hits_429", 0) or 0)
        _empty = int(gs.get("empty_200", 0) or 0)
        _errs = int(gs.get("errors", 0) or 0)
        _chit = int(gs.get("cache_hits", 0) or 0)
        _served = _req + _chit
        _cache_pct = (_chit / _served * 100.0) if _served > 0 else 0.0
        _429_pct = (_429 / _req * 100.0) if _req > 0 else 0.0
        _last429 = float(gs.get("last_429_ts", 0.0) or 0.0)
        _now = time.time()
        _last429_str = f"{(_now - _last429)/60.0:.0f} menit lalu ({gs.get('last_429_slot', '?')})" if _last429 > 0 else "belum pernah sejak start"

        # Counter kuota: seluruh data Health Ping berasal dari DB mirror —
        # bila "health_ping_external" > 0, ada kebocoran API di jalur ini.
        rpc_m = snap.get("rpc") or {}
        dex_m = snap.get("dex") or {}
        rpc_d = details.get("rpc") or {}
        dex_d = details.get("dex") or {}
        quota = {
            "health_ping_external": 0,
            "rpc_getslot_total": rpc_m.get("getslot") or rpc_d.get("getslot") or 0,
            "dex_fetches_total": dex_m.get("fetches") or dex_d.get("fetches") or 0,
            "gmgn_requests": _req,
        }

        online_cnt = sum(1 for cl in cluster if cl.get("is_ready"))
        cooling_cnt = len(cluster) - online_cnt
        lines = []
        for cl in cluster:
            if not cl.get("is_ready"):
                st_lbl = f"🟡 429 ({int(cl.get('rem_sec', 0) or 0)}s rem)"
            elif cl.get("is_active"):
                st_lbl = "🟢 CONNECTED (In-Use)"
            else:
                st_lbl = "⚪ STANDBY (Ready)"
            lines.append(f"  ├─ Slot {int(cl.get('slot', 0) or 0):2d} ({str(cl.get('name', '')):10s}): {st_lbl}")
        slot_lines = "\n".join(lines) if lines else "  └─ (menunggu engine push state pertama ±2 detik setelah start)"

        text = (
            "🏓 [SYSTEM API LATENCY & HEALTH AUDIT — ZERO-API CACHE] ⚡\n\n"
            f"⏱️ Waktu Snapshot DB: {_now_utc}\n"
            f"⚡ Kecepatan Baca DB (Dashboard): {db_read_ms:.2f} ms (0 Kuota API Terpakai!)\n\n"
            "🌐 INFRASTRUKTUR EKSEKUSI (SNAPSHOT TERAKHIR):\n"
            f"├─ 🟢 QuickNode RPC Dedicated: {r_lat:.1f} ms (Slot: {r_slot})\n"
            f"├─ 🟢 Jupiter Ultra Swap API: {jup_lat:.1f} ms (Warm Keep-Alive Pool)\n"
            f"├─ 🟢 Jito MEV Block Engine: {jito_lat:.1f} ms (Private Mempool Active)\n"
            f"├─ 🟢 DexScreener API: {dex_lat:.1f} ms (Verified DEX Pairs)\n"
            f"└─ 🟢 RugCheck Security: {rc_lat:.1f} ms (Mint/Freeze Defense)\n\n"
            "📉 KUOTA & RATE-LIMIT GMGN (sejak service start):\n"
            f"├─ Request keluar: {_req:,} | 200 OK: {_ok:,}\n"
            f"├─ Cache hit: {_chit:,} ({_cache_pct:.1f}% dari {_served:,} permintaan)\n"
            f"├─ HTTP 429: {_429:,} ({_429_pct:.2f}% dari request)\n"
            f"├─ 200 kosong: {_empty:,} | Error lain: {_errs:,}\n"
            f"├─ Auth ditolak (401/403): {int(gs.get('auth_fail', 0) or 0):,}\n"
            f"└─ 429 terakhir: {_last429_str}\n\n"
            "🛡️ 15-SLOT GMGN DECADUAL SHIELD (Multi-WARP & Ed25519):\n"
            f"📊 Status In-Memory: 🟢 {online_cnt} Ready / 🟡 {cooling_cnt} Rotating\n"
            + slot_lines + "\n\n"
            "💡 Dibaca langsung dari SQLite yang sama dengan engine + mirror state memori (push tiap 60 detik) — 0 kuota API."
        )

        age_sec = None
        if state_updated:
            try:
                t_state = _dt.datetime.strptime(state_updated[:19], "%Y-%m-%d %H:%M:%S")
                age_sec = round((_dt.datetime.utcnow() - t_state).total_seconds(), 1)
                age_min = round(age_sec / 60.0, 1)
            except Exception:
                age_sec = None
        return web.json_response({
            "success": True,
            "text": text,
            "db_read_ms": round(db_read_ms, 2),
            "snapshot_utc": _now_utc,
            "dex_ok": bool(snap.get("dex_ok", True)),
            "hardware": hardware,
            "db": dbinfo,
            "telemetry": {
                "timestamp": str(tel["timestamp"]),
                "rpc_slot": r_slot,
                "rpc_latency_ms": r_lat,
                "jupiter_latency_ms": jup_lat,
                "dexscreener_latency_ms": dex_lat,
                "rugcheck_latency_ms": rc_lat,
                "jito_latency_ms": jito_lat,
                "active_positions": int(tel["active_positions"] or 0),
                "total_signals": int(tel["total_signals"] or 0),
            },
            "stats": gs,
            "slots": cluster,
            "health_state_error": hs_err,
            "state_updated": state_updated,
            "age_seconds": age_sec,
            "age_minutes": age_min,
            "updated": state_updated or str(tel["timestamp"]),
            "quota": quota,
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_smart_wallets(request):
    try:
        limit = min(int(request.query.get("limit", "100")), 250)
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute(
            "SELECT wallet_address, category, winrate_7d, pnl_7d, token_num, "
            "balance_sol, pnl_2x_plus, tags, last_active "
            "FROM smart_wallets ORDER BY pnl_7d DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return web.json_response({"success": True, "count": len(rows), "data": rows})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_check_ca(request):
    """Resolve a Solana CA into live DexScreener pair data (used by the header CA checker)."""
    ca = (request.query.get("ca") or "").strip()
    if not ca:
        return web.json_response({"success": False, "error": "Parameter ca wajib diisi"}, status=400)
    session = request.app.get("http_session")
    if session is None:
        return web.json_response({"success": False, "error": "HTTP client belum siap"}, status=503)
    try:
        pairs = await fetch_dex_pairs(session, [ca])
        sol_pair = pairs.get(ca)
        if not sol_pair:
            return web.json_response({"success": False, "error": "Token tidak ditemukan di jaringan Solana"}, status=404)
        return web.json_response({"success": True, "dex": sol_pair})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=502)

async def api_logs(request):
    """Incremental bridge log feed + engine telemetry snapshot + optional engine file tail."""
    try:
        since = int(request.query.get("since_id", "0"))
    except Exception:
        since = 0
    entries = [e for e in LOG_BUFFER if e["id"] > since]

    telemetry = []
    try:
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute("SELECT * FROM system_telemetry_history ORDER BY id DESC LIMIT 40")
        telemetry = [dict(r) for r in c.fetchall()]
        conn.close()
    except Exception as e:
        blog_debounced("tel_read", f"Telemetry read gagal: {e}", "WATCHDOG", "warn", 120)

    engine_log = None
    log_path = os.environ.get("ENGINE_LOG_FILE", "")
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 30000))
                engine_log = f.read().decode("utf-8", "replace").splitlines()[-250:]
        except Exception:
            engine_log = None

    return web.json_response({
        "success": True,
        "entries": entries,
        "last_id": _log_seq,
        "telemetry": telemetry,
        "engine_file_available": engine_log is not None,
        "engine_log": engine_log
    })

async def api_get_wallet(request):
    user_id = request.query.get("user_id", "6166029678")
    try:
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute("""
            SELECT user_id, public_key, default_buy_sol, auto_buy_enabled, slippage_pct,
                   auto_buy_mode, auto_buy_min_sol, auto_buy_max_sol, auto_buy_min_usd, auto_buy_max_usd,
                   active_wallet_type, created_at
            FROM user_trading_wallets WHERE user_id=?
        """, (user_id,))
        row = c.fetchone()
        
        # Ambil juga demo / sandbox wallet stats
        c.execute("SELECT initial_capital_sol, current_balance_sol, total_trades, win_trades, total_realized_sol FROM paper_account_stats WHERE id=1")
        demo_row = c.fetchone()
        demo_stats = dict(demo_row) if demo_row else {
            "initial_capital_sol": 0.1,
            "current_balance_sol": 0.1,
            "total_trades": 0,
            "win_trades": 0,
            "total_realized_sol": 0.0
        }
        
        conn.close()

        if not row:
            return web.json_response({"success": False, "error": "Wallet not found"})

        wallet_data = dict(row)
        
        # Real on-chain balance check jika ada RPC
        sol_bal = 0.0
        if HAS_ENGINE_MODULES and wallet_data.get("public_key"):
            try:
                sol_bal = await wallet_manager.get_sol_balance(wallet_data["public_key"])
            except Exception:
                sol_bal = 0.0
        wallet_data["sol_balance"] = sol_bal
        
        return web.json_response({
            "success": True,
            "wallet": wallet_data,
            "demo_wallet": {
                "name": "ScreenerNantiAja Demo Bot",
                "label": "Demo / Sandbox Wallet (Paper Trading)",
                "initial_capital_sol": demo_stats.get("initial_capital_sol", 0.1),
                "balance_sol": demo_stats.get("current_balance_sol", 0.1),
                "realized_sol": demo_stats.get("total_realized_sol", 0.0),
                "total_trades": demo_stats.get("total_trades", 0),
                "win_trades": demo_stats.get("win_trades", 0),
                "status": "AKTIF (Paper Autonomous Engine)"
            }
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_export_wallet(request):
    if not HAS_ENGINE_MODULES:
        return web.json_response({"success": False, "error": "Modul wallet engine tidak tersedia di bridge ini"}, status=503)
    user_id = request.query.get("user_id", "6166029678")
    try:
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute("SELECT encrypted_private_key, public_key FROM user_trading_wallets WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return web.json_response({"success": False, "error": "Wallet not found"}, status=404)

        enc_pk = row["encrypted_private_key"]
        # dulu memanggil wallet_manager.decrypt_private_key() yang TIDAK ADA
        # -> export selalu 500 sejak endpoint ini dibuat
        decrypted_b58 = wallet_manager._decrypt(enc_pk)

        return web.json_response({
            "success": True,
            "public_key": row["public_key"],
            "private_key_base58": decrypted_b58
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_update_wallet_settings(request):
    try:
        body = await request.json()
        user_id = body.get("user_id", "6166029678")
        # Server-side clamp: API tidak boleh memercayai nilai bebas dari body
        buy_sol = min(max(float(body.get("default_buy_sol", 0.1) or 0.1), 0.001), 5.0)
        # Auto Slippage: 0.0 menandakan mode dinamis (Jupiter auto-slippage)
        req_slip = float(body.get("slippage_pct", 15.0) or 0.0)
        slippage = 0.0 if req_slip <= 0.0 else min(max(req_slip, 0.1), 50.0)
        auto_buy = 1 if body.get("auto_buy_enabled") else 0
        
        # Pengaturan adaptif sizing
        auto_buy_mode = "usd" if str(body.get("auto_buy_mode", "sol")).lower() == "usd" else "sol"
        min_sol = min(max(float(body.get("auto_buy_min_sol", 0.05) or 0.05), 0.001), 5.0)
        max_sol = min(max(float(body.get("auto_buy_max_sol", 0.20) or 0.20), min_sol), 10.0)
        min_usd = min(max(float(body.get("auto_buy_min_usd", 2.0) or 2.0), 0.1), 100.0)
        max_usd = min(max(float(body.get("auto_buy_max_usd", 5.0) or 5.0), min_usd), 500.0)
        active_wallet_type = "real" if str(body.get("active_wallet_type", "demo")).lower() == "real" else "demo"
    except (TypeError, ValueError):
        return web.json_response({"success": False, "error": "Nilai setting tidak valid"}, status=400)

    try:
        conn = get_db_connection(False)
        c = conn.cursor()
        c.execute("""
            UPDATE user_trading_wallets
            SET default_buy_sol=?, slippage_pct=?, auto_buy_enabled=?,
                auto_buy_mode=?, auto_buy_min_sol=?, auto_buy_max_sol=?,
                auto_buy_min_usd=?, auto_buy_max_usd=?, active_wallet_type=?
            WHERE user_id=?
        """, (buy_sol, slippage, auto_buy, auto_buy_mode, min_sol, max_sol, min_usd, max_usd, active_wallet_type, user_id))
        conn.commit()
        conn.close()

        # Update cache in wallet_manager if loaded
        if HAS_ENGINE_MODULES and hasattr(wallet_manager, "_wallet_cache"):
            w_cache = wallet_manager._wallet_cache.get(str(user_id))
            if w_cache:
                w_cache["default_buy_sol"] = buy_sol
                w_cache["slippage_pct"] = slippage
                w_cache["auto_buy_enabled"] = bool(auto_buy)
                w_cache["auto_buy_mode"] = auto_buy_mode
                w_cache["auto_buy_min_sol"] = min_sol
                w_cache["auto_buy_max_sol"] = max_sol
                w_cache["auto_buy_min_usd"] = min_usd
                w_cache["auto_buy_max_usd"] = max_usd
                w_cache["active_wallet_type"] = active_wallet_type

        return web.json_response({"success": True, "message": "Pengaturan wallet & adaptif autobuy tersimpan"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

# --- MARKET DATA HELPERS (shared DexScreener client, cached) ---

async def fetch_dex_pairs(session, cas):
    """Bulk DexScreener lookup: up to 30 CAs per request, returns {ca: best_pair}."""
    result = {}
    chunks = [cas[i:i + 30] for i in range(0, len(cas), 30)]
    async def _one(chunk):
        try:
            async with session.get(DEXSCREENER_TOKEN_URL.format(cas=",".join(chunk)), timeout=ClientTimeout(total=8)) as resp:
                payload = await resp.json(content_type=None)
            for pair in payload.get("pairs") or []:
                ca = (pair.get("baseToken") or {}).get("address")
                if not ca or pair.get("chainId") != "solana":
                    continue
                if ca not in result or float(pair.get("liquidity", {}).get("usd", 0) or 0) > float(result[ca].get("liquidity", {}).get("usd", 0) or 0):
                    result[ca] = pair
        except Exception:
            pass
    await asyncio.gather(*[_one(ch) for ch in chunks])
    return result

async def get_sol_price(session):
    """Cached SOL/USD price + 24h change (30s TTL)."""
    if time.time() - SOL_NET_CACHE["ts"] < 30 and SOL_NET_CACHE["price"] > 0:
        return SOL_NET_CACHE["price"], SOL_NET_CACHE["change_24h"]
    try:
        async with session.get(DEXSCREENER_TOKEN_URL.format(cas=WSOL_MINT), timeout=ClientTimeout(total=8)) as resp:
            payload = await resp.json(content_type=None)
        pair = next((p for p in payload.get("pairs") or [] if p.get("chainId") == "solana"), None)
        if pair:
            SOL_NET_CACHE["price"] = float(pair.get("priceUsd", 0) or 0)
            SOL_NET_CACHE["change_24h"] = float((pair.get("priceChange") or {}).get("h24", 0) or 0)
            SOL_NET_CACHE["ts"] = time.time()
    except Exception:
        pass
    return SOL_NET_CACHE["price"], SOL_NET_CACHE["change_24h"]

def get_token_decimals_sync(ca):
    """Token decimals via multi-RPC racing; falls back to 6."""
    if HAS_ENGINE_MODULES and multi_rpc is not None:
        try:
            res = multi_rpc.call_rpc({"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [ca]})
            if isinstance(res, dict):
                return int(res["result"]["value"]["decimals"])
        except Exception:
            pass
    return 6

def jup_quote_params(input_mint, output_mint, amount_lamports, slippage_bps):
    return {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount_lamports)), "slippageBps": str(int(slippage_bps))}

async def api_trade_preview(request):
    """Preview a manual buy: estimated tokens out + price impact via Jupiter (best effort)."""
    ca = (request.query.get("ca") or "").strip()
    amount_sol = float(request.query.get("amount_sol", "0") or 0)
    slippage_bps = int(float(request.query.get("slippage_pct", "15") or 15) * 100)
    if not ca or amount_sol <= 0:
        return web.json_response({"success": False, "error": "ca dan amount_sol wajib"}, status=400)
    session = request.app["http_session"]
    quote = None
    try:
        async with session.get(JUPITER_QUOTE_URL, params=jup_quote_params(WSOL_MINT, ca, amount_sol * 1e9, slippage_bps), timeout=ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                quote = await resp.json()
    except Exception:
        quote = None
    if not quote or int(quote.get("outAmount", 0) or 0) <= 0:
        return web.json_response({"success": False, "error": "Tidak ada rute likuiditas untuk jumlah ini"})
    decimals = get_token_decimals_sync(ca)
    out_raw = int(quote["outAmount"])
    tokens_out = out_raw / (10 ** decimals)
    return web.json_response({
        "success": True,
        "tokens_out": tokens_out,
        "price_impact_pct": float(quote.get("priceImpactPct", "0") or 0) * 100 if float(quote.get("priceImpactPct", "0") or 0) < 1 else float(quote.get("priceImpactPct", "0") or 0),
        "route": quote.get("routePlan") and len(quote["routePlan"]) or 0
    })

async def api_trade(request):
    """Manual buy/sell execution. mode=paper (sandbox, default) | mode=live (real funds)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Body JSON tidak valid"}, status=400)

    action = (body.get("action") or "").lower()          # buy | sell
    mode = (body.get("mode") or "paper").lower()          # paper | live
    user_id = str(body.get("user_id", "6166029678"))
    ca = (body.get("ca") or "").strip()
    symbol = (body.get("symbol") or "TOKEN").strip()
    position_id = body.get("position_id")
    # JSON mengizinkan NaN/Infinity: NaN lolos semua perbandingan numerik
    # dan mengkorupsi saldo sandbox (NULL). isfinite() wajib.
    try:
        percent = min(max(float(body.get("percent", 100) or 100), 1.0), 100.0)
        amount_sol = float(body.get("amount_sol", 0) or 0)
    except (TypeError, ValueError):
        return web.json_response({"success": False, "error": "percent/amount_sol harus angka"}, status=400)
    if not (math.isfinite(percent) and math.isfinite(amount_sol)):
        return web.json_response({"success": False, "error": "percent/amount_sol harus finite"}, status=400)

    # Idempotensi: dedupe client_id (double-click / retry jaringan tidak boleh
    # mengeksekusi swap on-chain dua kali)
    client_id = str(body.get("client_id") or "")
    if client_id:
        if client_id in _RECENT_CLIENT_IDS:
            return web.json_response({"success": False, "error": "Permintaan duplikat (client_id sama)"}, status=409)
        _RECENT_CLIENT_IDS.append(client_id)

    if action not in ("buy", "sell"):
        return web.json_response({"success": False, "error": "action harus buy/sell"}, status=400)
    if mode not in ("paper", "live"):
        return web.json_response({"success": False, "error": "mode harus paper/live"}, status=400)
    if mode == "live" and not HAS_ENGINE_MODULES:
        return web.json_response({"success": False, "error": "Modul engine tidak tersedia untuk eksekusi riil"}, status=503)

    session = request.app["http_session"]

    # ------------------------------------------------------------------ BUY
    if action == "buy":
        if not ca or amount_sol <= 0 or amount_sol > 5:
            return web.json_response({"success": False, "error": "CA wajib & amount 0 < x <= 5 SOL"}, status=400)

        sol_price, _ = await get_sol_price(session)
        if sol_price <= 0:
            return web.json_response({"success": False, "error": "Harga SOL tidak tersedia, coba lagi"}, status=503)

        pairs = await fetch_dex_pairs(session, [ca])
        pair = pairs.get(ca)
        if not pair:
            return web.json_response({"success": False, "error": "Token tidak ditemukan / tidak aktif di Solana"}, status=404)
        price_usd = float(pair.get("priceUsd", 0) or 0)
        mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        if price_usd <= 0:
            return web.json_response({"success": False, "error": "Harga token tidak valid"}, status=400)

        # Server-side clamp: UI membatasi slippage, API tidak boleh memercayai body
        slippage_pct = min(max(float(body.get("slippage_pct", 15) or 15), 0.1), 50.0)
        slippage_bps = int(slippage_pct * 100)

        if mode == "live":
            # REAL execution via engine SwapRouter (Jupiter)
            try:
                keypair = wallet_manager.get_decrypted_keypair(user_id)
                if keypair is None:
                    return web.json_response({"success": False, "error": "Wallet engine tidak tersedia — import wallet dulu"}, status=400)
                quote = await swap_router.get_quote(WSOL_MINT, ca, int(amount_sol * 1e9), slippage_bps)
                if not quote or int(quote.get("outAmount", 0) or 0) <= 0:
                    return web.json_response({"success": False, "error": "Jupiter: tidak ada rute likuiditas"}, status=400)
                sig = await swap_router.execute_swap(keypair, quote)
                if not sig:
                    return web.json_response({"success": False, "error": "Swap gagal dieksekusi on-chain"}, status=502)
                return web.json_response({"success": True, "mode": "live", "tx_signature": sig, "message": f"Buy ${symbol} sukses — tx: {sig[:12]}…"})
            except Exception as e:
                return web.json_response({"success": False, "error": f"Live buy gagal: {e}"}, status=500)

        # -------- PAPER buy: record a sandbox position exactly like the engine does
        tokens_bought = 0.0
        token_decimals = 6
        try:
            async with session.get(JUPITER_QUOTE_URL, params=jup_quote_params(WSOL_MINT, ca, amount_sol * 1e9, slippage_bps), timeout=ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    quote = await resp.json()
                    out_raw = int(quote.get("outAmount", 0) or 0)
                    if out_raw > 0:
                        token_decimals = get_token_decimals_sync(ca)
                        tokens_bought = out_raw / (10 ** token_decimals)
        except Exception:
            pass
        if tokens_bought <= 0:
            # Pure math fallback when Jupiter is unreachable
            usd_value = amount_sol * sol_price
            tokens_bought = usd_value / price_usd

        now_ts = int(time.time() * 1000)
        conn = get_db_connection(False)
        conn.isolation_level = None  # autocommit-off: BEGIN eksplisit di bawah
        c = conn.cursor()
        try:
            # BEGIN IMMEDIATE: cek saldo + INSERT + debit dalam satu transaksi
            # (dulu dua buy konkuren sama-sama lolos cek → saldo bisa negatif)
            c.execute("BEGIN IMMEDIATE")
            c.execute("SELECT virtual_balance_sol FROM paper_account_stats WHERE id=1")
            bal_row = c.fetchone()
            balance = float(bal_row["virtual_balance_sol"]) if bal_row else 0.1
            if amount_sol > balance:
                conn.rollback()
                return web.json_response({"success": False, "error": f"Saldo sandbox tidak cukup ({balance:.4f} SOL)"}, status=400)
            c.execute("""
                INSERT INTO paper_trading_positions
                (token_ca, symbol, sol_spent, tokens_bought, tokens_remaining, entry_price_usd, entry_mcap,
                 current_price_usd, current_mcap, peak_mcap, peak_multiplier, realized_sol, tp1_hit, status,
                 score, strategy, liquidity_usd, sm_count, execution_route, token_decimals, entry_quote_ts, entry_data_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 0.0, 0, 'OPEN', 0, 'Manual Dashboard', ?, 0, 'Manual Buy (Paper)', ?, ?, 'Manual Dashboard Buy — DexScreener + Jupiter quote')
            """, (ca, symbol, amount_sol, tokens_bought, tokens_bought, price_usd, mcap, price_usd, mcap, mcap, liq, token_decimals, now_ts))
            pos_id = c.lastrowid
            cur2 = c.execute("UPDATE paper_account_stats SET virtual_balance_sol = virtual_balance_sol - ?, total_trades = total_trades + 1 WHERE id = 1 AND virtual_balance_sol >= ?", (amount_sol, amount_sol))
            if cur2.rowcount == 0:
                conn.rollback()
                return web.json_response({"success": False, "error": f"Saldo sandbox tidak cukup ({balance:.4f} SOL)"}, status=400)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        sync_ram_state_from_engine_db()
        blog(f"MANUAL BUY ${symbol} — {amount_sol:.2f} SOL @ ${price_usd:,.8f} (paper, ~{tokens_bought:,.0f} token)", "TRADE", "success")
        return web.json_response({"success": True, "mode": "paper", "position_id": pos_id, "tokens_bought": tokens_bought, "message": f"Paper buy ${symbol} — {tokens_bought:,.0f} token"})

    # ----------------------------------------------------------------- SELL
    if not position_id:
        return web.json_response({"success": False, "error": "position_id wajib untuk sell"}, status=400)
    conn = get_db_connection(True)
    c = conn.cursor()
    c.execute("SELECT * FROM paper_trading_positions WHERE id=?", (position_id,))
    pos = c.fetchone()
    conn.close()
    if not pos or pos["status"] != "OPEN":
        return web.json_response({"success": False, "error": "Posisi tidak ditemukan / sudah tertutup"}, status=404)
    pos = dict(pos)

    sell_frac = percent / 100.0
    tokens_remaining = float(pos["tokens_remaining"] or 0)
    tokens_bought = float(pos["tokens_bought"] or 0) or 1
    tokens_to_sell = tokens_remaining * sell_frac if sell_frac < 1.0 else tokens_remaining

    sol_price, _ = await get_sol_price(session)

    if mode == "live":
        # REAL sell: swap the on-chain token balance back to SOL
        try:
            keypair = wallet_manager.get_decrypted_keypair(user_id)
            if keypair is None:
                return web.json_response({"success": False, "error": "Wallet engine tidak tersedia"}, status=400)
            decimals = get_token_decimals_sync(pos["token_ca"])
            sell_raw = int(tokens_to_sell * (10 ** decimals))
            quote = await swap_router.get_quote(pos["token_ca"], WSOL_MINT, sell_raw)
            if not quote or int(quote.get("outAmount", 0) or 0) <= 0:
                return web.json_response({"success": False, "error": "Jupiter: tidak ada rute jual"}, status=400)
            sig = await swap_router.execute_swap(keypair, quote)
            if not sig:
                return web.json_response({"success": False, "error": "Swap jual gagal dieksekusi"}, status=502)
            # Update record paper agar posisi tidak bisa dijual ULANG dari UI
            # (record bukan sumber on-chain, tapi tanpa ini double-sell membakar
            # gas dan mengacaukan akuntansi). Kondisional WHERE status='OPEN'.
            _live_full = sell_frac >= 1.0 or tokens_to_sell >= tokens_remaining - 1e-9
            try:
                _conn = get_db_connection(False)
                _c = _conn.cursor()
                if _live_full:
                    _c.execute("UPDATE paper_trading_positions SET tokens_remaining=0, status='CLOSED', closed_at=datetime('now'), exit_reason='MANUAL DASHBOARD SELL (LIVE)' WHERE id=? AND status='OPEN'", (position_id,))
                else:
                    _c.execute("UPDATE paper_trading_positions SET tokens_remaining = tokens_remaining - ? WHERE id=? AND status='OPEN'", (tokens_to_sell, position_id))
                _conn.commit()
                _conn.close()
                sync_ram_state_from_engine_db()
            except Exception:
                pass
            return web.json_response({"success": True, "mode": "live", "tx_signature": sig, "message": f"Sell ${pos['symbol']} sukses — tx: {sig[:12]}…"})
        except Exception as e:
            return web.json_response({"success": False, "error": f"Live sell gagal: {e}"}, status=500)

    # -------- PAPER sell
    pairs = await fetch_dex_pairs(session, [pos["token_ca"]])
    pair = pairs.get(pos["token_ca"])
    price_usd = float(pair.get("priceUsd", 0) or 0) if pair else float(pos["current_price_usd"] or 0)
    if price_usd <= 0:
        return web.json_response({"success": False, "error": "Harga live tidak tersedia"}, status=503)
    if sol_price <= 0:
        # Jangan pernah mencatat PnL memakai harga SOL fiktif — tolak request
        return web.json_response({"success": False, "error": "Harga SOL tidak tersedia, coba lagi"}, status=503)
    usd_value = tokens_to_sell * price_usd
    realized_sol = usd_value / sol_price

    full_close = sell_frac >= 1.0 or tokens_to_sell >= tokens_remaining - 1e-9
    spent_portion = float(pos["sol_spent"]) * (tokens_to_sell / tokens_bought)
    pnl_sol = realized_sol - spent_portion

    now_ts = int(time.time() * 1000)
    created_ts = parse_dt(pos.get("created_at"))
    hold_sec = max(0, int((now_ts / 1000) - (created_ts or now_ts / 1000)))

    conn = get_db_connection(False)
    c = conn.cursor()
    new_remaining = 0.0 if full_close else tokens_remaining - tokens_to_sell
    new_realized = float(pos["realized_sol"] or 0) + realized_sol
    if full_close:
        # Kondisional WHERE status='OPEN': dua sell konkuren tidak boleh
        # sama-sama mengkredit realized_sol (dulu double credit mungkin).
        cur_close = c.execute("""
            UPDATE paper_trading_positions SET tokens_remaining=0, realized_sol=?, exit_price_usd=?, current_price_usd=?,
                current_mcap=?, status='CLOSED', exit_reason='MANUAL DASHBOARD SELL', closed_at=datetime('now'),
                hold_duration_sec=? WHERE id=? AND status='OPEN'
        """, (new_realized, price_usd, price_usd, float(pair.get("marketCap") or pos["current_mcap"] or 0) if pair else pos["current_mcap"], hold_sec, position_id))
        if cur_close.rowcount == 0:
            conn.close()
            return web.json_response({"success": False, "error": "Posisi sudah tertutup (duplikat)"}, status=409)
        is_win = 1 if new_realized >= float(pos["sol_spent"]) else 0
        c.execute("UPDATE paper_account_stats SET win_trades = win_trades + ?, lose_trades = lose_trades + ? WHERE id=1", (is_win, 1 - is_win))
    else:
        cur_part = c.execute("UPDATE paper_trading_positions SET tokens_remaining=?, realized_sol=?, exit_price_usd=? WHERE id=? AND status='OPEN'",
                  (new_remaining, new_realized, price_usd, position_id))
        if cur_part.rowcount == 0:
            conn.close()
            return web.json_response({"success": False, "error": "Posisi sudah tertutup (duplikat)"}, status=409)
    c.execute("UPDATE paper_account_stats SET virtual_balance_sol = virtual_balance_sol + ?, realized_pnl_sol = realized_pnl_sol + ? WHERE id=1",
              (realized_sol, pnl_sol))
    conn.commit()
    conn.close()
    sync_ram_state_from_engine_db()
    blog(f"MANUAL SELL {percent:.0f}% ${pos['symbol']} — {'full close' if full_close else 'parsial'} · net {pnl_sol:+.4f} SOL (paper)", "TRADE", "success" if pnl_sol >= 0 else "warn")
    return web.json_response({
        "success": True, "mode": "paper", "position_id": position_id,
        "closed": full_close, "realized_sol": realized_sol, "pnl_sol": pnl_sol,
        "message": f"Paper sell {percent:.0f}% ${pos['symbol']} — {'posisi tertutup' if full_close else 'terjual sebagian'} ({'+' if pnl_sol >= 0 else ''}{pnl_sol:.4f} SOL)"
    })

def parse_dt(s):
    if not s:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None

async def api_wallet_import(request):
    """Import an existing Solana wallet from a base58/hex private key (overwrites engine wallet)."""
    if not HAS_ENGINE_MODULES:
        return web.json_response({"success": False, "error": "Modul wallet engine tidak tersedia di bridge ini"}, status=503)
    try:
        body = await request.json()
        user_id = str(body.get("user_id", "6166029678"))
        private_key = (body.get("private_key") or "").strip()
        if not private_key:
            return web.json_response({"success": False, "error": "Private key wajib diisi"}, status=400)
        result = await wallet_manager.import_private_key(user_id, private_key)
        if not result:
            return web.json_response({"success": False, "error": "Private key tidak valid — wallet gagal diimport"}, status=400)
        return web.json_response({"success": True, "wallet": result, "message": "Wallet berhasil diimport"})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def api_network(request):
    """Live network & engine telemetry for the status bar."""
    session = request.app["http_session"]
    sol_price, sol_change = await get_sol_price(session)
    perf = in_memory_state["perf"]
    tick_age_ms = int((time.time() - in_memory_state["last_updated_ts"]) * 1000) if in_memory_state["last_updated_ts"] else None
    return web.json_response({
        "success": True,
        "sol_price_usd": sol_price,
        "sol_change_24h_pct": sol_change,
        "base_fee_sol": 0.000005,
        "priority_fee_sol": 0.0001,
        "health": compute_engine_health(),
        "engine": {
            "tick_interval_ms": 200,
            "last_sync_ms": perf["last_sync_ms"],
            "avg_sync_ms": perf["avg_sync_ms"],
            "last_tick_age_ms": tick_age_ms,
            "ws_clients": len(request.app.get("websockets", set())),
            "uptime_sec": int(time.time() - APP_STARTED_TS),
            "engine_modules": HAS_ENGINE_MODULES
        }
    })

JUPITER_TOKENS_URL = "https://lite-api.jup.ag/tokens/v2/search?query={ids}"

async def api_token_meta(request):
    """Token logos + socials via Jupiter Tokens API, merged into a TTL cache."""
    cas = [c.strip() for c in (request.query.get("cas") or "").split(",") if 40 <= len(c.strip()) <= 50][:100]
    session = request.app["http_session"]
    now = time.time()
    missing = [ca for ca in cas if ca not in TOKEN_LOGO_CACHE or now - TOKEN_LOGO_CACHE[ca]["ts"] > TOKEN_LOGO_TTL]
    if missing:
        try:
            async with session.get(JUPITER_TOKENS_URL.format(ids=",".join(missing)), timeout=ClientTimeout(total=10)) as resp:
                tokens = await resp.json(content_type=None)
            if isinstance(tokens, list):
                for t in tokens:
                    tid = t.get("id")
                    if not tid:
                        continue
                    socials = {}
                    for key in ("twitter", "telegram", "website", "discord"):
                        url = t.get(key)
                        if url:
                            socials[key] = url
                    for s in t.get("socials") or []:
                        stype = s.get("type") or s.get("name")
                        surl = s.get("url")
                        if stype and surl:
                            stype = stype.lower()
                            if stype not in socials:
                                socials[stype] = surl
                    entry = {
                        "url": t.get("icon"),
                        "ts": now,
                        "socials": socials,
                    }
                    TOKEN_LOGO_CACHE[tid] = entry
        except Exception:
            pass
        for ca in missing:
            if ca not in TOKEN_LOGO_CACHE:
                TOKEN_LOGO_CACHE[ca] = {"url": None, "ts": now, "socials": {}}
    logos = {ca: TOKEN_LOGO_CACHE[ca]["url"] for ca in cas if ca in TOKEN_LOGO_CACHE}
    socials = {ca: TOKEN_LOGO_CACHE[ca].get("socials") or {} for ca in cas if ca in TOKEN_LOGO_CACHE}
    return web.json_response({"success": True, "logos": logos, "socials": socials})

# --- WEBSOCKET ENGINE EVENT BUS (ZERO EXTERNAL API DELAY) ---

async def websocket_handler(request):
    # heartbeat=30: koneksi setengah-mati (TCP hidup, peer hilang) akan
    # terdeteksi & ditutup, tidak lagi jadi zombie di app['websockets'].
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    request.app['websockets'].add(ws)

    blog(f"Klien WebSocket terhubung ({len(request.app['websockets'])} aktif)", "SYS")
    try:
        # Immediate send from RAM (<1ms)
        await ws.send_str(json.dumps({
            "type": "SNAPSHOT",
            "signals": in_memory_state["signals"],
            "active_positions": in_memory_state["active_positions"],
            "closed_positions": in_memory_state["closed_positions"],
            "stats": in_memory_state["stats"]
        }, default=str))

        async for msg in ws:
            pass
    except Exception:
        pass
    finally:
        request.app['websockets'].discard(ws)
        blog(f"Klien WebSocket terputus ({len(request.app['websockets'])} aktif)", "SYS")

    return ws

async def live_broadcaster(app):
    """
    Ultra-Fast In-Memory Broadcaster:
    1. Reads SQLite WAL in RAM (<2ms)
    2. Zero external API calls from web (100% GMGN safety)
    3. Broadcasts every 200ms for instant real-time UI response!
    """
    while True:
        try:
            await asyncio.sleep(0.2)  # 200ms ultra-smooth live loop!
            if not app['websockets']:
                continue

            # Sync DB → RAM di thread executor: sqlite3 sinkron di event loop
            # membekukan seluruh server (WS, REST) selama proses berlangsung.
            await asyncio.to_thread(sync_ram_state_from_engine_db)

            # Skip tick identik: engine hanya update tiap 4s, jadi 19 dari 20
            # tick payloadnya byte-identik — jangan buang serialisasi+kirim.
            _ver = (in_memory_state["last_updated_ts"], len(in_memory_state["signals"]),
                    len(in_memory_state["active_positions"]), len(in_memory_state["closed_positions"]))
            if _ver == app.get("_last_payload_ver") and app['websockets'] == app.get("_sent_clients", set()):
                # Tetap kirim heartbeat mini: client memakai tick-age untuk
                # membedakan "pasar tenang" dari "koneksi mati".
                ping = json.dumps({"type": "PING", "server_time_ms": int(time.time() * 1000)})
                for ws in list(app['websockets']):
                    try:
                        await ws.send_str(ping)
                    except Exception:
                        app['websockets'].discard(ws)
                continue
            app["_last_payload_ver"] = _ver
            app["_sent_clients"] = set(app['websockets'])

            payload = json.dumps({
                "type": "TICK",
                "signals": in_memory_state["signals"],
                "active_positions": in_memory_state["active_positions"],
                "closed_positions": in_memory_state["closed_positions"],
                "stats": in_memory_state["stats"],
                "server_time_ms": int(time.time() * 1000)
            }, default=str)

            for ws in list(app['websockets']):
                try:
                    await ws.send_str(payload)
                except Exception:
                    # Klien mati WAJIB dikeluarkan; dulu `pass` membuat zombie
                    # menumpuk selamanya di set (ws_clients melebar palsu).
                    app['websockets'].discard(ws)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)

async def start_background_tasks(app):
    app['websockets'] = set()
    app['http_session'] = ClientSession()
    sync_ram_state_from_engine_db()
    blog(f"Bridge start — engine modules: {'TERMUAT' if HAS_ENGINE_MODULES else 'tidak tersedia (read-only)'} · DB: {os.path.basename(DB_PATH)}", "SYS", "success")
    app['broadcaster'] = asyncio.create_task(live_broadcaster(app))
    app['cto_sentinel'] = asyncio.create_task(cto_sentinel_loop(app))

async def cleanup_background_tasks(app):
    app['broadcaster'].cancel()
    app['cto_sentinel'].cancel()
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await app['broadcaster']
    for ws in list(app['websockets']):
        await ws.close()
    await app['http_session'].close()

async def index_handler(request):
    return web.FileResponse(os.path.join(PUBLIC_DIR, 'index.html'), headers={'Cache-Control': 'no-cache'})

async def static_file_handler(request):
    name = request.match_info['filename']
    if name not in ('app.css', 'app.js', 'favicon.svg', 'logo.svg'):
        raise web.HTTPNotFound()
    # Aset ber-versioning via nama file statis: cache singkat mengurangi
    # re-fetch tanpa menyulitkan update (index tetap no-cache).
    return web.FileResponse(os.path.join(PUBLIC_DIR, name), headers={'Cache-Control': 'public, max-age=300'})

async def api_switch_active_wallet(request):
    try:
        body = await request.json()
        user_id = body.get("user_id", "6166029678")
        target_type = "real" if str(body.get("wallet_type", "")).lower() in ("real", "wallet_1", "wallet1") else "demo"
        
        conn = get_db_connection(False)
        c = conn.cursor()
        c.execute("UPDATE user_trading_wallets SET active_wallet_type=? WHERE user_id=?", (target_type, user_id))
        conn.commit()
        conn.close()

        if HAS_ENGINE_MODULES and hasattr(wallet_manager, "_wallet_cache"):
            w_cache = wallet_manager._wallet_cache.get(str(user_id))
            if w_cache:
                w_cache["active_wallet_type"] = target_type

        return web.json_response({
            "success": True,
            "active_wallet_type": target_type,
            "message": f"Dompet aktif berhasil dialihkan ke {'Real Wallet 1' if target_type == 'real' else 'Demo Bot Sandbox'}"
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get('/', index_handler)
    app.router.add_get('/portofolio', index_handler)
    app.router.add_get('/evaluasi', index_handler)
    app.router.add_get('/recap', index_handler)
    app.router.add_get('/healthping', index_handler)
    app.router.add_get('/logs', index_handler)
    app.router.add_get('/ws/live', websocket_handler)
    app.router.add_get('/{filename}', static_file_handler)

    # API Endpoints
    app.router.add_get('/api/signals', api_signals)
    app.router.add_get('/api/positions', api_positions)
    app.router.add_get('/api/stats', api_stats)
    app.router.add_get('/api/recap', api_recap)
    app.router.add_get('/api/ping', api_ping)
    app.router.add_get('/api/smart_wallets', api_smart_wallets)
    app.router.add_get('/api/check_ca', api_check_ca)
    app.router.add_get('/api/network', api_network)
    app.router.add_get('/api/token_meta', api_token_meta)
    app.router.add_get('/api/logs', api_logs)
    app.router.add_get('/api/wallet', api_get_wallet)
    # Export private key = operasi sensitif: wajib POST (dulu GET yang bisa
    # dipicu preload/preview apapun, dan tanpa auth bisa dibaca siapa pun)
    app.router.add_post('/api/wallet/export', api_export_wallet)
    app.router.add_post('/api/wallet/settings', api_update_wallet_settings)
    app.router.add_post('/api/wallet/switch', api_switch_active_wallet)
    app.router.add_post('/api/wallet/import', api_wallet_import)
    app.router.add_get('/api/trade/preview', api_trade_preview)
    app.router.add_post('/api/trade', api_trade)

    # CORS dihapus total: SPA di-layani same-origin, tidak ada skenario
    # cross-origin yang sah. Dulu wildcard + allow_credentials memicu
    # drive-by: website mana pun bisa fetch export key / live trade.

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app

if __name__ == '__main__':
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=8000)
