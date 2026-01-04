import os
import time
import math
import requests
from statistics import median
from datetime import datetime, timedelta, timezone
from supabase import create_client

ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")

TERMS = [
    "cadeira ergonômica",
    "suporte notebook",
    "luminária led",
    "aspirador portátil"
]

def sb_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def get_state(sb, key: str) -> str:
    r = sb.table("app_state").select("value").eq("key", key).limit(1).execute()
    if not r.data:
        raise Exception(f"Chave '{key}' não encontrada na tabela app_state.")
    return r.data[0]["value"]

def set_state(sb, key: str, value: str):
    sb.table("app_state").upsert({"key": key, "value": value}).execute()

def cleanup_old_snapshots(sb, days=60):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sb.table("snapshots").delete().lt("run_at", cutoff.isoformat()).execute()
    print(f"🧹 Limpeza feita: registros mais antigos que {days} dias removidos")

def refresh_access_token(sb):
    refresh_token = get_state(sb, "ML_REFRESH_TOKEN")

    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    payload = r.json()

    access_token = payload["access_token"]

    # ✅ rotação: se vier refresh_token novo, salva no Supabase automaticamente
    new_refresh = payload.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        set_state(sb, "ML_REFRESH_TOKEN", new_refresh)
        print("🔁 Mercado Livre rotacionou o refresh_token. Atualizado no Supabase automaticamente.")

    return access_token

def ml_search(term, access_token, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    r = requests.get(url, params=params, headers=headers, timeout=20)

    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(url, params=params, headers=headers, timeout=20)

    r.raise_for_status()
    return r.json()

def analyze_term(term, access_token, pages=2):
    first = ml_search(term, access_token)
    total_results = first.get("paging", {}).get("total", 0)

    sellers = set()
    prices = []
    sample_items = 0

    for p in range(pages):
        data = first if p == 0 else ml_search(term, access_token, offset=p * 50)
        for item in data.get("results", []):
            sid = item.get("seller", {}).get("id")
            price = item.get("price")
            if sid:
                sellers.add(sid)
            if price:
                prices.append(price)
            sample_items += 1
        time.sleep(0.3)

    price_median = median(prices) if prices else None
    unique_sellers = len(sellers)

    score = None
    if total_results > 0:
        score = (1 / math.log(1 + total_results)) * (1 / (1 + unique_sellers))

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "term": term,
        "total_results": total_results,
        "unique_sellers_sample": unique_sellers,
        "sample_items": sample_items,
        "price_median": price_median,
        "score": score
    }

def main():
    sb = sb_client()

    cleanup_old_snapshots(sb, days=60)

    access_token = refresh_access_token(sb)

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
