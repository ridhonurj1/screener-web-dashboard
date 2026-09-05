# ScreenerNantiAja — Web Dashboard & Real-Time Quant Terminal

Modern, decoupled real-time Web Dashboard for **ScreenerNantiAja** (Solana Quant Screener & Autonomous Execution Suite).

## 🚀 Fitur Utama

- **Jupiter (jup.ag) Design System**:
  - Dark theme signature `#0E141E`, `#151D2C`, dan aksen Electric Lime `#C7F284` & Mint Cyan `#28D7B5`.
  - Minimalist top bar dengan slide-over drawer menu (Garis 3 / `☰`).
  - Fluid 100vw x 100vh layout dengan card hover elevation.
- **Sub-Detik (500ms) Real-Time WebSocket**:
  - Membaca live on-chain token signals, active open positions, dan realized PnL langsung dari database engine SQLite WAL.
  - Zero Cloudflare CDN cache delay dengan update harga live dan visual blink animations.
- **In-App Real-Time Candlestick Chart**:
  - Klik kartu koin mana saja untuk langsung membuka chart live PumpSwap / Raydium di dalam aplikasi tanpa perlu pindah tab.
- **Fitur Engine Telegram Terintegrasi**:
  - 🔍 **Quick CA Checker**: Input bar pencarian CA token langsung di header atas.
  - 🧠 **Top Smart Money Radar**: Peringkat 231 wallet whale dengan win rate tinggi.
  - 📊 **Laporan Kinerja & Rekapitulasi (Recap Menu)**: Evaluasi performa 24 Jam, 7 Hari, 30 Hari, dan All-Time.
  - 💳 **Manajemen Wallet Trading**: Public key deposit address, pengaturan modal default buy SOL, slippage, auto-buy on-chain, dan fitur **Export Private Key (Base58)**.

## 🛠️ Tech Stack & Arsitektur

- **Backend Bridge**: Python 3.11 (`aiohttp`, `aiohttp-cors`, SQLite WAL).
- **Frontend**: Single Page Application (HTML5, Tailwind CSS, Lucide Icons, WebSocket Client).
- **Arsitektur**: Decoupled (Terpisah mandiri dari engine `ScreenerNantiAja`).

## 📦 Cara Menjalankan

```bash
# Install dependencies
pip install aiohttp aiohttp-cors

# Jalankan server API & Web
python3 api_bridge.py

# Atau jalankan via PM2 di background
pm2 start "python3 api_bridge.py" --name "screener-web"
```

Buka di browser: `http://localhost:8000`
