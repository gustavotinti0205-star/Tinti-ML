import os
import time
import math
from curl_cffi import requests
from statistics import median
from datetime import datetime, timedelta, timezone
from supabase import create_client

# =========================
# Configurações Iniciais
# =========================
ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")

# Cabeçalhos para parecer um navegador real
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Connection": "keep-alive",
}

# =========================
# Funções do Supabase
# =========================
def sb_client():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def app_state_get(sb, key: str):
    r = sb.table("app_state").select("value").eq("key", key).limit(1).execute()
    return r.data[0].get("value") if r.data else None

def app_state_set(sb, key: str, value_json: dict):
    sb.table("app_state").upsert({"key": key, "value": value_json}).execute()

def get_refresh_token_from_supabase(sb) -> str:
    v = app_state_get(sb, "ML_REFRESH_TOKEN")
    if not v: raise RuntimeError("Token não encontrado.")
    return v["token"]

# =========================
# Busca com Bypass de Bloqueio (Impersonate)
# =========================
def refresh_access_token(refresh_token: str) -> dict:
    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    # Aqui usamos o impersonate para fingir ser o Chrome
    r = requests.post(url, data=data, headers=DEFAULT_HEADERS, impersonate="chrome120", timeout=30)
    r.raise_for_status()
    return r.json()

def ml_search_auth(term, access_token):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": 50}
    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    
    # Busca fingindo ser navegador real
    r = requests.get(url, params=params, headers=headers, impersonate="chrome120", timeout=25)
    r.raise_for_status()
    return r.json()

# =========================
# Lógica de Mineração
# =========================
def main():
    sb = sb_client()
    refresh_token = get_refresh_token_from_supabase(sb)
    
    # Renova o Token
    token_data = refresh_access_token(refresh_token)
    access_token = token_data.get("access_token")
    
    # Atualiza o refresh_token no banco se ele mudou
    if token_data.get("refresh_token"):
        app_state_set(sb, "ML_REFRESH_TOKEN", {"token": token_data["refresh_token"]})

    termos = ["cadeira ergonômica", "suporte notebook", "luminária led"]
    
    for termo in termos:
        print(f"🔎 Analisando brechas para: {termo}")
        try:
            data = ml_search_auth(termo, access_token)
            results = data.get("results", [])
            total = data.get("paging", {}).get("total", 0)
            
            prices = [item["price"] for item in results if item.get("price")]
            sellers = len(set(item["seller"]["id"] for item in results if item.get("seller")))
            
            # Cálculo de Oportunidade
            score = (1 / math.log(1 + total)) * (1 / (1 + sellers)) if total > 0 else 0

            snapshot = {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "term": termo,
                "total_results": total,
                "unique_sellers_sample": sellers,
                "price_median": median(prices) if prices else 0,
                "score": score
            }
            
            sb.table("snapshots").insert(snapshot).execute()
            print(f"✅ Sucesso para {termo}")
            time.sleep(5)
            
        except Exception as e:
            print(f"❌ Erro no termo {termo}: {e}")

if __name__ == "__main__":
    main()
