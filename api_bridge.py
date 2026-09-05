import asyncio
import json
import sqlite3
import os
import sys
import time
from aiohttp import web
import aiohttp_cors

# Import from ScreenerNantiAja engine
sys.path.append("/home/kaiden/projects/ScreenerNantiAja")
from wallet_manager import wallet_manager
from compact_evaluator import _compute_timeframe_evaluation_sync

DB_PATH = "/home/kaiden/projects/ScreenerNantiAja/screener.db"

# IN-MEMORY RAM CACHE FOR SUB-MILLISECOND (1MS) DISPATCH
in_memory_state = {
    "signals": [],
    "active_positions": [],
    "closed_positions": [],
    "stats": {},
    "last_signal_rowid": 0,
    "last_updated_ts": 0
}

def get_db_connection(read_only=True):
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(f"file:{DB_PATH}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def sync_ram_state_from_engine_db():
    """Reads engine SQLite WAL into RAM in ~1ms without touching external APIs"""
    global in_memory_state
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
        if signals:
            in_memory_state["last_signal_rowid"] = signals[0]["rowid"]

    except Exception as e:
        pass

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
    sync_ram_state_from_engine_db()
    app['broadcaster'] = asyncio.create_task(live_broadcaster(app))

async def cleanup_background_tasks(app):
    app['broadcaster'].cancel()
    await app['broadcaster']
    for ws in list(app['websockets']):
        await ws.close()

async def index_handler(request):
    public_index = os.path.join(os.path.dirname(__file__), 'public', 'index.html')
    return web.FileResponse(public_index)

def create_app():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/ws/live', websocket_handler)

    # API Endpoints
    app.router.add_get('/api/signals', api_signals)
    app.router.add_get('/api/positions', api_positions)
    app.router.add_get('/api/stats', api_stats)
    app.router.add_get('/api/recap', api_recap)
    app.router.add_get('/api/wallet', api_get_wallet)
    app.router.add_get('/api/wallet/export', api_export_wallet)
    app.router.add_post('/api/wallet/settings', api_update_wallet_settings)

    public_dir = os.path.join(os.path.dirname(__file__), 'public')
    app.router.add_static('/static', path=public_dir)

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
