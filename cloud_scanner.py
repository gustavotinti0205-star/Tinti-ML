import os
import time
import math
import requests
from statistics import median
from datetime import datetime, timedelta, timezone
from supabase import create_client

# Puxa as chaves de segurança que você configurou no GitHub
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

def main():
    if not ZENROWS_API_KEY:
        print("❌ Erro: Chave ZENROWS_API_KEY não encontrada.")
        return

    # Conecta ao seu banco de dados
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    
    # Lista de produtos para minerar
    termos = ["cadeira ergonômica", "suporte notebook", "luminária led"]
    
    for termo in termos:
        print(f"🔎 Minerando via ZenRows: {termo}")
        
        # O endereço do "túnel" que pula o bloqueio
        proxy_url = "https://api.zenrows.com/v1/"
        
        # O link do Mercado Livre que queremos acessar
        ml_url = f"https://api.mercadolibre.com/sites/MLB/search?q={termo}&limit=50"
        
        # Configurações para a ZenRows agir como um humano no Brasil
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": ml_url,
            "premium_proxy": "true",
            "proxy_country": "br"
        }

        try:
            # Faz a busca através do túnel
            r = requests.get(proxy_url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            
            results = data.get('results', [])
            total = data.get('paging', {}).get('total', 0)
            
            # Analisa os preços e vendedores
            prices = [item['price'] for item in results if item.get('price')]
            sellers = len(set(item['seller']['id'] for item in results if item.get('seller')))
            
            # Calcula o Score de Oportunidade
            score = (1 / math.log(1 + total)) * (1 / (1 + sellers)) if total > 0 else 0
            
            snapshot = {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "term": termo,
                "total_results": total,
                "unique_sellers_sample": sellers,
                "price_median": median(prices) if prices else 0,
                "score": score
            }
            
            # Salva no Supabase
            sb.table("snapshots").insert(snapshot).execute()
            print(f"✅ Sucesso: {termo} salvo no banco!")
            
        except Exception as e:
            print(f"❌ Falha no termo {termo}: {e}")
        
        time.sleep(2) # Pausa para segurança

if __name__ == "__main__":
    main()
