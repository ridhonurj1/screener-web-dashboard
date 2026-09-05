import asyncio
import json
import sqlite3
import os
import sys
import time
from aiohttp import web, ClientSession, ClientTimeout
import aiohttp_cors
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

def get_db_connection(read_only=True):
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(f"file:{DB_PATH}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
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

        # Enrich stats
        stats["active_positions_count"] = len(active_positions)
        c.execute("SELECT count(*) FROM signals")
        stats["total_signals_count"] = c.fetchone()[0]

        conn.close()

        in_memory_state["signals"] = signals
        in_memory_state["active_positions"] = active_positions
        in_memory_state["closed_positions"] = closed_positions
        in_memory_state["stats"] = stats
        in_memory_state["last_updated_ts"] = time.time()
        in_memory_state["perf"]["sync_ok"] = True
        if signals:
            in_memory_state["last_signal_rowid"] = signals[0]["rowid"]

    except Exception:
        in_memory_state["perf"]["sync_ok"] = False
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        perf = in_memory_state["perf"]
        perf["last_sync_ms"] = round(dt_ms, 2)
        perf["avg_sync_ms"] = round((perf["avg_sync_ms"] * 0.9 + dt_ms * 0.1) if perf["avg_sync_ms"] else dt_ms, 2)

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
    return web.json_response({"success": True, "count": len(in_memory_state["signals"]), "data": in_memory_state["signals"]})

async def api_positions(request):
    return web.json_response({
        "success": True,
        "active": in_memory_state["active_positions"],
        "closed": in_memory_state["closed_positions"]
    })

async def api_stats(request):
    return web.json_response({"success": True, "data": in_memory_state["stats"]})

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

async def api_get_wallet(request):
    user_id = request.query.get("user_id", "6166029678")
    try:
        conn = get_db_connection(True)
        c = conn.cursor()
        c.execute("SELECT user_id, public_key, default_buy_sol, auto_buy_enabled, slippage_pct, created_at FROM user_trading_wallets WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return web.json_response({"success": False, "error": "Wallet not found"})

        wallet_data = dict(row)
        return web.json_response({"success": True, "wallet": wallet_data})
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
        decrypted_b58 = wallet_manager.decrypt_private_key(enc_pk)

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
        buy_sol = float(body.get("default_buy_sol", 0.1))
        slippage = float(body.get("slippage_pct", 15.0))
        auto_buy = 1 if body.get("auto_buy_enabled") else 0

        conn = get_db_connection(False)
        c = conn.cursor()
        c.execute("""
            UPDATE user_trading_wallets
            SET default_buy_sol=?, slippage_pct=?, auto_buy_enabled=?
            WHERE user_id=?
        """, (buy_sol, slippage, auto_buy, user_id))
        conn.commit()
        conn.close()

        return web.json_response({"success": True, "message": "Pengaturan wallet tersimpan"})
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
    percent = min(max(float(body.get("percent", 100) or 100), 1.0), 100.0)
    amount_sol = float(body.get("amount_sol", 0) or 0)

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

        slippage_pct = float(body.get("slippage_pct", 15) or 15)
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
        c = conn.cursor()
        c.execute("SELECT virtual_balance_sol FROM paper_account_stats WHERE id=1")
        bal_row = c.fetchone()
        balance = float(bal_row["virtual_balance_sol"]) if bal_row else 0.1
        if amount_sol > balance:
            conn.close()
            return web.json_response({"success": False, "error": f"Saldo sandbox tidak cukup ({balance:.4f} SOL)"}, status=400)
        c.execute("""
            INSERT INTO paper_trading_positions
            (token_ca, symbol, sol_spent, tokens_bought, tokens_remaining, entry_price_usd, entry_mcap,
             current_price_usd, current_mcap, peak_mcap, peak_multiplier, realized_sol, tp1_hit, status,
             score, strategy, liquidity_usd, sm_count, execution_route, token_decimals, entry_quote_ts, entry_data_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 0.0, 0, 'OPEN', 0, 'Manual Dashboard', ?, 0, 'Manual Buy (Paper)', ?, ?, 'Manual Dashboard Buy — DexScreener + Jupiter quote')
        """, (ca, symbol, amount_sol, tokens_bought, tokens_bought, price_usd, mcap, price_usd, mcap, mcap, liq, token_decimals, now_ts))
        pos_id = c.lastrowid
        c.execute("UPDATE paper_account_stats SET virtual_balance_sol = virtual_balance_sol - ?, total_trades = total_trades + 1 WHERE id = 1", (amount_sol,))
        conn.commit()
        conn.close()
        sync_ram_state_from_engine_db()
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
        sol_price = 200.0  # conservative fallback for SOL->USD conversion
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
        c.execute("""
            UPDATE paper_trading_positions SET tokens_remaining=0, realized_sol=?, exit_price_usd=?, current_price_usd=?,
                current_mcap=?, status='CLOSED', exit_reason='MANUAL DASHBOARD SELL', closed_at=datetime('now','localtime'),
                hold_duration_sec=? WHERE id=?
        """, (new_realized, price_usd, price_usd, float(pair.get("marketCap") or pos["current_mcap"] or 0) if pair else pos["current_mcap"], hold_sec, position_id))
        is_win = 1 if new_realized >= float(pos["sol_spent"]) else 0
        c.execute("UPDATE paper_account_stats SET win_trades = win_trades + ?, lose_trades = lose_trades + ? WHERE id=1", (is_win, 1 - is_win))
    else:
        c.execute("UPDATE paper_trading_positions SET tokens_remaining=?, realized_sol=?, exit_price_usd=? WHERE id=?",
                  (new_remaining, new_realized, price_usd, position_id))
    c.execute("UPDATE paper_account_stats SET virtual_balance_sol = virtual_balance_sol + ?, realized_pnl_sol = realized_pnl_sol + ? WHERE id=1",
              (realized_sol, pnl_sol))
    conn.commit()
    conn.close()
    sync_ram_state_from_engine_db()
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
    """Token logos via Jupiter Tokens API, merged into a TTL cache."""
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
                    if tid:
                        TOKEN_LOGO_CACHE[tid] = {"url": t.get("icon"), "ts": now}
        except Exception:
            pass
        for ca in missing:
            if ca not in TOKEN_LOGO_CACHE:
                TOKEN_LOGO_CACHE[ca] = {"url": None, "ts": now}
    return web.json_response({"success": True, "logos": {ca: TOKEN_LOGO_CACHE[ca]["url"] for ca in cas if ca in TOKEN_LOGO_CACHE}})

# --- WEBSOCKET ENGINE EVENT BUS (ZERO EXTERNAL API DELAY) ---

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app['websockets'].add(ws)

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

            # Refresh RAM cache from engine WAL
            sync_ram_state_from_engine_db()

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
                    pass
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)

async def start_background_tasks(app):
    app['websockets'] = set()
    app['http_session'] = ClientSession()
    sync_ram_state_from_engine_db()
    app['broadcaster'] = asyncio.create_task(live_broadcaster(app))

async def cleanup_background_tasks(app):
    app['broadcaster'].cancel()
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
    return web.FileResponse(os.path.join(PUBLIC_DIR, name), headers={'Cache-Control': 'no-cache'})

def create_app():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/portofolio', index_handler)
    app.router.add_get('/evaluasi', index_handler)
    app.router.add_get('/recap', index_handler)
    app.router.add_get('/ws/live', websocket_handler)
    app.router.add_get('/{filename}', static_file_handler)

    # API Endpoints
    app.router.add_get('/api/signals', api_signals)
    app.router.add_get('/api/positions', api_positions)
    app.router.add_get('/api/stats', api_stats)
    app.router.add_get('/api/recap', api_recap)
    app.router.add_get('/api/smart_wallets', api_smart_wallets)
    app.router.add_get('/api/check_ca', api_check_ca)
    app.router.add_get('/api/network', api_network)
    app.router.add_get('/api/token_meta', api_token_meta)
    app.router.add_get('/api/wallet', api_get_wallet)
    app.router.add_get('/api/wallet/export', api_export_wallet)
    app.router.add_post('/api/wallet/settings', api_update_wallet_settings)
    app.router.add_post('/api/wallet/import', api_wallet_import)
    app.router.add_get('/api/trade/preview', api_trade_preview)
    app.router.add_post('/api/trade', api_trade)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app

if __name__ == '__main__':
    app = create_app()
    web.run_app(app, host='0.0.0.0', port=8000)
