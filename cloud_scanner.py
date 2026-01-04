import os
import time
import math
import requests
from statistics import median
from datetime import datetime, timedelta, timezone

from supabase import create_client

ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

# Supabase env
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Mercado Livre env
CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN")

TERMS = [
    "cadeira ergonômica",
    "suporte notebook",
    "luminária led",
    "aspirador portátil"
]

# --- validação básica de env ---
def require_env(name: str):
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Variável de ambiente ausente: {name}")
    return val

def sb_client():
    require_env("SUPABASE_URL")
    require_env("SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def cleanup_old_snapshots(sb, days=60):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sb.table("snapshots").delete().lt("run_at", cutoff.isoformat()).execute()
    print(f"🧹 Limpeza feita: registros mais antigos que {days} dias removidos")

# --- token cache (evita ficar pedindo token) ---
_token_cache = {"access_token": None, "fetched_at": None}

def refresh_access_token(force=False):
    """
    Troca refresh_token por access_token.
    Cache: reaproveita por ~45 min para evitar excesso de requests.
    """
    require_env("ML_CLIENT_ID")
    require_env("ML_CLIENT_SECRET")
    require_env("ML_REFRESH_TOKEN")

    if not force and _token_cache["access_token"] and _token_cache["fetched_at"]:
        age = (datetime.now(timezone.utc) - _token_cache["fetched_at"]).total_seconds()
        if age < 45 * 60:  # 45 minutos
            return _token_cache["access_token"]

    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }

    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    token = r.json()["access_token"]

    _token_cache["access_token"] = token
    _token_cache["fetched_at"] = datetime.now(timezone.utc)

    return token

def ml_search(term, access_token, limit=50, offset=0, retry=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "TintiMLScanner/1.0",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }

    r = requests.get(url, params=params, headers=headers, timeout=25)

    # Rate limit
    if r.status_code == 429:
        wait = min(5 * (retry + 1), 20)
        print(f"⏳ 429 (rate limit). Aguardando {wait}s e tentando de novo...")
        time.sleep(wait)
        if retry < 3:
            return ml_search(term, access_token, limit, offset, retry=retry + 1)
        r.raise_for_status()

    # Token inválido/expirado
    if r.status_code in (401, 403):
        # 403 pode acontecer se token não está válido para aquela requisição.
        if retry < 1:
            print("🔁 Token inválido/expirado (401/403). Renovando token e tentando novamente...")
            new_token = refresh_access_token(force=True)
            return ml_search(term, new_token, limit, offset, retry=retry + 1)
        r.raise_for_status()

    r.raise_for_status()
    return r.json()

def analyze_term(term, access_token, pages=2):
    first = ml_search(term, access_token, limit=50, offset=0)
    total_results = first.get("paging", {}).get("total", 0)

    sellers = set()
    prices = []
    sample_items = 0

    for p in range(pages):
        data = first if p == 0 else ml_search(term, access_token, limit=50, offset=p * 50)

        for item in data.get("results", []):
            sid = item.get("seller", {}).get("id")
            price = item.get("price")

            if sid:
                sellers.add(sid)
            if price is not None:
                prices.append(price)

            sample_items += 1

        time.sleep(0.25)

    price_median = float(median(prices)) if prices else None
    unique_sellers = len(sellers)

    # Score (quanto maior, melhor):
    # - demanda por vendedor (total_results / sellers)
    # - favorece faixa de preço boa de margem
    score = None
    if total_results and unique_sellers:
        demand_per_seller = total_results / max(unique_sellers, 1)
        price_factor = 1.0
        if price_median is not None:
            if 80 <= price_median <= 400:
                price_factor = 1.3
            elif price_median >= 50:
                price_factor = 1.15
            else:
                price_factor = 0.9

        # log para não explodir com termos gigantes
        score = (math.log10(1 + demand_per_seller)) * price_factor

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),   # ✅ SUA COLUNA
        "term": term,
        "total_results": int(total_results),
        "unique_sellers_sample": int(unique_sellers),
        "sample_items": int(sample_items),
        "price_median": price_median,
        "score": score,
    }

def main():
    sb = sb_client()

    # limpa histórico antigo (60 dias)
    cleanup_old_snapshots(sb, days=60)

    # pega token uma vez (cache + refresh se precisar)
    access_token = refresh_access_token()

    for term in TERMS:
        print(f"🔎 Analisando: {term}")
        try:
            data = analyze_term(term, access_token)
            sb.table("snapshots").insert(data).execute()
            print("✅ Salvou no Supabase")
        except Exception as e:
            print(f"⚠️ Erro no termo '{term}': {e}")

if __name__ == "__main__":
    main()

