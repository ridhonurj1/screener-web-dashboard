# ScreenerNantiAja — Solana Alpha Terminal

Real-time Web Dashboard untuk **ScreenerNantiAja** (Solana Quant Screener & Autonomous Execution Suite). Didesain sebagai terminal trading profesional: identitas *alpha radar*, aksen electric-lime di atas deep navy, numerik tabular monospace, dan animasi mikro 200ms dari engine.

![brand](public/logo.svg)

## 🚀 Fitur

### Market Radar & Data Live
- **Alpha Screener Radar** — sinyal token live via WebSocket (tick 200ms), dengan logo token (Jupiter Tokens API), tier/strategy chip, Alpha Score meter, dan sparkline harga real-time.
- **KPI Strip** — saldo sandbox, realized PnL (% modal), win rate meter, dan aktivitas engine.
- **Filter & Sort** — sinyal Aktif/Semua/Tertutup, urut terbaru/multiplikasi/skor.
- **Status Bar** — harga SOL live (DexScreener), gas (base + priority fee), kecepatan engine (sync ms, tick age), klien WebSocket & uptime.
- **Connection Pill** — LIVE / MENGHUBUNGKAN / OFFLINE (klik untuk reconnect), auto-reconnect berjenjang.

### Trading Manual & Otomatis
- **Trade Ticket** — beli/jual manual dari kartu token, chart modal, atau kartu posisi.
  - Mode **Paper** (sandbox) & **Live** (swap on-chain riil via Jupiter `SwapRouter`).
  - Preview estimasi token + price impact live sebelum konfirmasi (Jupiter quote).
  - Jual parsial (25/50/75/100%) dengan akuntansi saldo & PnL otomatis.
- **Kontrol Automasi** — status radar scanner & paper exit loop (selalu aktif), toggle **Auto-Buy On-Chain** untuk mode riil otomatis, plus parameter default buy & slippage.
- **Posisi & Riwayat** — panel posisi aktif dengan PnL live, riwayat trade lengkap (R-multiple, hold duration, hasil TP/SL).

### Halaman Dedicated
- `/` **Terminal** — radar sinyal live + panel posisi/riwayat.
- `/portofolio` — **kurva PnL kumulatif** (anchor 0 SOL) dengan ruler Y-axis, sumbu waktu, crosshair + tooltip per trade, baseline modal, titik terakhir berdenyut, plus ledger semua transaksi.
- `/evaluasi` — evaluasi kinerja paper trading: kartu statistik (net PnL, profit factor, best/worst), tabel per token, insight periode.
- `/recap` — **Performance Recap & Winrate Tracking** format identik Topic #6 engine: tree `[📊] Ponyin Recap` → `[📈] Stats & Performance` (Total/Win/Lose/In Play/Winrate/Top ATH), benchmark **Hedge Fund Grade** (Realized Avg R vs ≥ +0.40R, Profit Factor vs ≥ 1.75), jam UTC dual-precision milidetik, dan tabel `[📋] Recent Signals` dengan link GMGN.

### Wallet & Smart Money
- **Tab Portofolio** — saldo besar, **kurva pergerakan saldo** (ledger replay semua transaksi), 6 statistik portofolio (total transaksi, win rate, posisi terbuka, nilai posisi, volume, realized PnL), dan **riwayat transaksi** lengkap (BUY/SELL/TP/SL) dengan saldo setelah tiap event.
- **Evaluasi Kinerja Terstruktur** — kartu statistik (net PnL, profit factor, trade terbaik/terburuk), **tabel kinerja per token**, insight naratif, plus laporan teks resmi engine dalam collapsible.
- **Import Wallet Sendiri** — masuk dengan private key Base58/hex (terenkripsi oleh engine `wallet_manager`).
- **Export Private Key**, deposit address, dan saldo on-chain.
- **Top Smart Money Radar** — leaderboard wallet whale (win rate 7d, PnL, kategori, tags).
- **Quick CA Checker** — cek token Solana mana pun langsung dari header (shortcut `/`).

## 🎨 Desain (Rebranding v2)

- Logo baru **"N" candlestick**: dua candle ber-wick sebagai batang huruf N, dihubungkan garis harga dengan node lime — orisinal, favicon, header, dan status bar.
- Design system CSS murni (zero CDN): token warna berlapis, radii, motion, dan state (`public/app.css`).
- Semua ikon inline SVG — tetap berfungsi di jaringan Tailscale tanpa internet.
- Skeleton loading, empty state, toast bertipe, keyboard (`/` fokus pencarian, `Esc` menutup overlay), responsif penuh hingga layar 390px.

## 🛠️ Arsitektur

```
┌────────────────────┐  SQLite WAL   ┌──────────────────┐   WebSocket/REST   ┌──────────────┐
│ ScreenerNantiAja   │ ────────────► │  api_bridge.py   │ ◄────────────────► │  public/     │
│ (engine + server)  │   baca ~1ms   │  aiohttp :8000   │    tick 200ms      │ index.html   │
│ Tailscale network  │               │  + Jupiter/Dex   │                    │ app.css/js   │
└────────────────────┘               └──────────────────┘                    └──────────────┘
```

- **Bridge** (`api_bridge.py`): membaca SQLite WAL engine ke RAM (<1ms), broadcast WebSocket 200ms, REST API, proxy Jupiter/DexScreener dengan cache, dan eksekusi trade manual (paper & live via `SwapRouter`).
- **Frontend**: SPA statis tanpa build step — HTML/CSS/JS murni.
- **Path engine dapat dikonfigurasi** via env: `ENGINE_DIR` dan `DB_PATH`. Tanpa modul engine (mis. di mesin dev), bridge tetap jalan read-only dengan degradasi elegan.

## 📦 Menjalankan

```bash
pip install aiohttp aiohttp-cors

# di server engine (path default /home/kaiden/projects/ScreenerNantiAja)
python3 api_bridge.py

# atau dengan path kustom / mesin dev
ENGINE_DIR=/path/to/ScreenerNantiAja DB_PATH=/path/to/screener.db python3 api_bridge.py

# via PM2
pm2 start "python3 api_bridge.py" --name "screener-web"
```

Buka `http://localhost:8000` (atau IP Tailscale server dari perangkat mana pun).

## ⚠️ Catatan Keamanan

- Mode **Live** mengirim transaksi on-chain riil dari wallet engine — selalu konfirmasi dua langkah di UI.
- Auto-Buy On-Chain hanya aktif setelah diaktifkan eksplisit di menu Akun & Wallet.
- Import wallet menggantikan wallet engine — pindahkan saldo lama terlebih dahulu.
- Export private key menampilkan kredensial sensitif; jangan pernah dibagikan.
