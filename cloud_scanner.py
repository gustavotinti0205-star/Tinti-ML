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

# Puxa as chaves das configurações do seu GitHub Secrets
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")

# Configurações de tempo e limpeza
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "60"))
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "24"))

# Cabeçalhos que fingem ser um navegador comum
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Connection": "keep-alive",
}

# =========================
# Funções do Supabase
# =========================
def sb_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Configurações do Supabase ausentes nos Secrets.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def app_state_get(sb, key: str):
    r = sb.table("app_state").select("value").eq("key", key).limit(1).execute()
    return r.data[0].get("value") if r.data else None

def app_state_set(sb, key: str, value_json: dict):
    sb.table("app_state").upsert({"key": key, "value": value_json}).execute()

def get_refresh_token_from_supabase(sb) -> str:
    v = app_state_get(sb, "ML_REFRESH_TOKEN")
    if not v or not v.get("token"):
        raise RuntimeError("Refresh Token não encontrado no banco app_state.")
    return v["token"]

# =========================
# Mercado Livre com Impersonate (Bypass 403)
# =========================
def refresh_access_token(refresh_token: str) -> dict:
    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    # O segredo para não dar 403 está aqui: impersonate="chrome120"
    r = requests.post(url, data=data, headers=DEFAULT_HEADERS, impersonate="chrome120", timeout=30)
    r.raise_for_status()
    return r.json()

def ml_search_auth(term, access_token, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"
    
    r = requests.get(url, params=params, headers=headers, impersonate="chrome120", timeout=25)
    r.raise_for_status()
    return r.json()

# =========================
# Lógica de Análise de Mercado
# =========================
def analyze_term(term, access_token):
    # Faz a busca usando o token renovado
    data = ml_search_auth(term, access_token, limit=50)
    total_results = data.get("paging", {}).get("total", 0)
    
    prices = []
    sellers = set()
    
    for item in data.get("results", []):
        if item.get("price"):
            prices.append(item.get("price"))
        if item.get("seller"):
            sellers.add(item.get("seller").get("id"))

    # Cálculo do Índice de Oportunidade (Demanda Reprimida)
    # Quanto mais buscas e menos vendedores, maior o score
    unique_sellers = len(sellers)
    price_median = median(prices) if prices else 0
    score = (1 / math.log(1 + total_results)) * (1 / (1 + unique_sellers)) if total_results > 0 else 0

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "term": term,
        "total_results": total_results,
        "unique_sellers_sample": unique_sellers,
        "price_median": price_median,
        "score": score,
    }

# =========================
# Execução Principal
# =========================
def main():
    sb = sb_client()
    
    # 1. Renova o Token
    refresh_token = get_refresh_token_from_supabase(sb)
    token_data = refresh_access_token(refresh_token)
    access_token = token_data.get("access_token")
    
    # 2. Se o ML mandar um novo refresh_token, salva no banco
    if token_data.get("refresh_token"):
        app_state_set(sb, "ML_REFRESH_TOKEN", {"token": token_data["refresh_token"]})

    # 3. Define o que pesquisar (pode vir de uma tabela ou lista fixa)
    termos = ["cadeira ergonômica", "suporte notebook", "luminária led"]
    
    for termo in termos:
        print(f"🔎 Analisando brechas para: {termo}")
        try:
            resultado = analyze_term(termo, access_token)
            # Salva o resultado na sua tabela de snapshots
            sb.table("snapshots").insert(resultado).execute()
            print(f"✅ Dados salvos para {termo}")
            time.sleep(5) # Pausa para não ser bloqueado por velocidade
        except Exception as e:
            print(f"❌ Erro no termo {termo}: {e}")

if __name__ == "__main__":
    main()
