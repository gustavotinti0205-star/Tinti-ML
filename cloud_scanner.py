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
REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN")

TERMS = [
    "cadeira ergonômica",
    "suporte notebook",
    "luminária led",
    "aspirador portátil"
]


def sb_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não configurados.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def cleanup_old_snapshots(sb, days=60):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sb.table("snapshots").delete().lt("run_at", cutoff.isoformat()).execute()
    print(f"🧹 Limpeza feita: registros mais antigos que {days} dias removidos")


def refresh_access_token():
    if not CLIENT_ID or not CLIENT_SECRET or not REFRESH_TOKEN:
        raise RuntimeError("ML_CLIENT_ID / ML_CLIENT_SECRET / ML_REFRESH_TOKEN não configurados.")

    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }

    r = requests.post(url, data=data, timeout=30)

    # Se falhar aqui, quase sempre é refresh token inválido/rotacionado
    if r.status_code in (400, 401, 403):
        try:
            details = r.json()
        except Exception:
            details = {"raw": r.text}
        raise RuntimeError(f"Falha ao renovar token (status {r.status_code}). Detalhes: {details}")

    r.raise_for_status()
    j = r.json()

    access = j.get("access_token")
    if not access:
        raise RuntimeError(f"Resposta do refresh sem access_token: {j}")

    # IMPORTANTE:
    # Se o Mercado Livre devolver refresh_token novo (rotação),
    # você precisa atualizar o secret ML_REFRESH_TOKEN manualmente no GitHub.
    new_refresh = j.get("refresh_token")
    if new_refresh and new_refresh != REFRESH_TOKEN:
        print("⚠️ O Mercado Livre devolveu um NOVO refresh_token (rotação).")
        print("⚠️ Atualize o secret ML_REFRESH_TOKEN no GitHub com o novo valor.")
        # Não imprimimos o token aqui por segurança.

    return access


def ml_search_public(term, limit=50, offset=0):
    """Busca pública SEM token (muitas vezes evita 403 quando o token está ruim)."""
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    headers = {
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


def ml_search_auth(term, access_token, limit=50, offset=0):
    """Busca com token. Se der 401/403, tentamos fallback público."""
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

    # Se token estiver ruim, pode vir 401/403.
    if r.status_code in (401, 403):
        print(f"🔁 Token inválido/expirado ({r.status_code}). Tentando busca pública sem token...")
        return ml_search_public(term, limit=limit, offset=offset)

    r.raise_for_status()
    return r.json()


def analyze_term(term, access_token, pages=2):
    first = ml_search_auth(term, access_token)
    total_results = first.get("paging", {}).get("total", 0)

    sellers = set()
    prices = []
    sample_items = 0

    for p in range(pages):
        data = first if p == 0 else ml_search_auth(term, access_token, offset=p * 50)
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
        "term": term,
        "total_results": total_results,
        "unique_sellers_sample": unique_sellers,
        "sample_items": sample_items,
        "price_median": price_median,
        "score": score,
        "run_at": datetime.now(timezone.utc).isoformat()
    }


def main():
    sb = sb_client()

    # 1) Limpa registros antigos
    cleanup_old_snapshots(sb, days=60)

    # 2) Tenta renovar token
    access_token = refresh_access_token()

    # 3) Roda termos e salva
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
