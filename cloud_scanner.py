import os
import time
import math
from curl_cffi import requests
from statistics import median
from datetime import datetime, timedelta, timezone
from supabase import create_client

# Configurações do Mercado Livre
ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

# Configurações de Ambiente
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CLIENT_ID = os.environ.get("ML_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")

# Headers Ultra Realistas
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}

def main():
    # Inicializa Supabase
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    
    # Busca Token
    res_token = sb.table("app_state").select("value").eq("key", "ML_REFRESH_TOKEN").single().execute()
    refresh_token = res_token.data['value']['token']
    
    # Renova Access Token usando curl_cffi para evitar 403
    auth_data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    
    r_auth = requests.post(f"{ML_BASE}/oauth/token", data=auth_data, impersonate="chrome110")
    r_auth.raise_for_status()
    access_token = r_auth.json()['access_token']
    
    # Lista de Termos
    termos = ["cadeira ergonômica", "suporte notebook", "luminária led"]
    
    for termo in termos:
        print(f"🔎 Analisando: {termo}")
        try:
            # Busca com Impersonate
            search_url = f"{ML_BASE}/sites/{SITE_ID}/search?q={termo}&limit=50"
            headers = {**DEFAULT_HEADERS, "Authorization": f"Bearer {access_token}"}
            
            r = requests.get(search_url, headers=headers, impersonate="chrome110")
            r.raise_for_status()
            
            data = r.json()
            total = data['paging']['total']
            results = data.get('results', [])
            
            # Lógica simples de Score
            prices = [item['price'] for item in results if item.get('price')]
            sellers = len(set(item['seller']['id'] for item in results if item.get('seller')))
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
            print(f"✅ Salvo no Supabase: {termo}")
            time.sleep(5)
            
        except Exception as e:
            print(f"❌ Erro no termo {termo}: {e}")

if __name__ == "__main__":
    main()
