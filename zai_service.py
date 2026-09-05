import os
import aiohttp
import json

CONFIG_FILE = "/home/kaiden/projects/screener-web-dashboard/ai_config.json"

def get_ai_config():
    default_config = {
        "provider": "z.ai",
        "api_key": "",
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4-flash",
        "enabled": False
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return {**default_config, **json.load(f)}
        except Exception:
            pass
    return default_config

def save_ai_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

class ZAIAgent:
    @staticmethod
    async def analyze_token_alpha(token_symbol, token_name, mcap, liq, vol_5m, top10_pct, sm_count, ca):
        cfg = get_ai_config()
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return {
                "verdict": "SAFE_NEUTRAL",
                "score": 75,
                "summary": "AI Analisis standby. Masukkan API Key ZCode di menu pengaturan."
            }

        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }

        prompt = f"""Kamu adalah On-Chain Quant Meme Auditor. Analisis token Solana berikut secara cepat dan tajam:
- Simbol: ${token_symbol} ({token_name})
- CA: {ca}
- Market Cap: ${round(mcap):,}
- Liquidity: ${round(liq):,}
- Volume 5m: ${round(vol_5m):,}
- Top 10 Holders: {top10_pct:.1f}% (Batas aman <= 20%)
- Smart Money Count: {sm_count} (Wajib >= 1)

Berikan analisis singkat 1-2 kalimat dalam Bahasa Indonesia dan tentukan Verdict: 'STRONG_BUY', 'CAUTION', atau 'HIGH_RISK'.
Jawab strictly format JSON:
{{
  "verdict": "STRONG_BUY" | "CAUTION" | "HIGH_RISK",
  "score": number (0-100),
  "summary": "Analisis ringkas 1-2 kalimat"
}}"""

        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data["choices"][0]["message"]["content"]
                        # Extract JSON
                        start = content.find("{")
                        end = content.rfind("}")
                        if start != -1 and end != -1:
                            return json.loads(content[start:end+1])
                    else:
                        err_text = await resp.text()
                        return {"verdict": "API_ERROR", "score": 0, "summary": f"HTTP Error {resp.status}: {err_text[:80]}"}
        except Exception as e:
            return {"verdict": "ERROR", "score": 0, "summary": f"Koneksi gagal: {str(e)}"}
        
        return {"verdict": "CAUTION", "score": 60, "summary": "Analisis tidak dapat diselesaikan."}
