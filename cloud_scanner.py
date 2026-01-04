import os
import time
import math
# Substitua import requests por:
from curl_cffi import requests
from statistics import median
# ... restante igual

# =========================
# Config
# =========================
ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")

# Chaves do app_state
APPSTATE_REFRESH_KEY = "ML_REFRESH_TOKEN"
APPSTATE_META_KEY = "ml_scanner"

# Limpeza e frequência
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "60"))          # mantém snapshots por X dias
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "24"))  # 24 = 1x por dia

# Fallback se não tiver tabela keywords
FALLBACK_TERMS = [
    "cadeira ergonômica",
    "suporte notebook",
    "luminária led",
    "aspirador portátil",
]

# Requests session (melhor performance)
SESSION = requests.Session()
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (GitHubActions; TinTi-ML; +https://github.com)",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# =========================
# Supabase helpers
# =========================
def sb_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não configurados.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def app_state_get(sb, key: str):
    r = sb.table("app_state").select("value").eq("key", key).limit(1).execute()
    if r.data and len(r.data) > 0:
        return r.data[0].get("value")
    return None

def app_state_set(sb, key: str, value_json: dict):
    # app_state.key deve ser PK/unique para funcionar bem com upsert
    sb.table("app_state").upsert({"key": key, "value": value_json}).execute()

def get_refresh_token_from_supabase(sb) -> str:
    v = app_state_get(sb, APPSTATE_REFRESH_KEY)
    if not v or not isinstance(v, dict) or not v.get("token"):
        raise RuntimeError(
            "Não encontrei ML_REFRESH_TOKEN no Supabase (app_state). "
            "Crie uma linha com key='ML_REFRESH_TOKEN' e value={'token': 'SEU_TOKEN'}."
        )
    return v["token"]

def set_refresh_token_in_supabase(sb, new_token: str):
    app_state_set(sb, APPSTATE_REFRESH_KEY, {"token": new_token})
    print("🔁 Refresh token atualizado no Supabase (app_state).")

def should_run(sb) -> bool:
    meta = app_state_get(sb, APPSTATE_META_KEY)
    if not meta or not isinstance(meta, dict) or not meta.get("last_run_at"):
        return True

    try:
        last = datetime.fromisoformat(meta["last_run_at"].replace("Z", "+00:00"))
    except Exception:
        return True

    now = datetime.now(timezone.utc)
    diff = now - last
    return diff >= timedelta(hours=MIN_HOURS_BETWEEN_RUNS)

def mark_run(sb):
    now = datetime.now(timezone.utc).isoformat()
    app_state_set(sb, APPSTATE_META_KEY, {"last_run_at": now})

def cleanup_old_snapshots(sb, days=KEEP_DAYS):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sb.table("snapshots").delete().lt("run_at", cutoff.isoformat()).execute()
    print(f"🧹 Limpeza feita: snapshots mais antigos que {days} dias removidos.")

def fetch_terms(sb):
    # Se você tiver tabela keywords (term, is_active), usa ela.
    # Caso contrário, cai no fallback.
    try:
        r = sb.table("keywords").select("term").eq("is_active", True).execute()
        terms = [row["term"] for row in (r.data or []) if row.get("term")]
        if terms:
            return terms
    except Exception:
        pass
    return FALLBACK_TERMS

# =========================
# Mercado Livre OAuth
# =========================
def refresh_access_token(refresh_token: str) -> dict:
    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    r = SESSION.post(url, data=data, headers=DEFAULT_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()  # geralmente: access_token, token_type, expires_in, scope, user_id, refresh_token (às vezes)

def ml_search_public(term, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    r = SESSION.get(url, params=params, headers=DEFAULT_HEADERS, timeout=25)
    # 429 = rate limit
    if r.status_code == 429:
        time.sleep(5)
        r = SESSION.get(url, params=params, headers=DEFAULT_HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()

def ml_search_auth(term, access_token, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}
    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {access_token}"

    r = SESSION.get(url, params=params, headers=headers, timeout=25)
    if r.status_code == 429:
        time.sleep(5)
        r = SESSION.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

# =========================
# Analyzer
# =========================
def analyze_term(term, fetch_fn, pages=2):
    first = fetch_fn(term, limit=50, offset=0)
    total_results = first.get("paging", {}).get("total", 0)

    sellers = set()
    prices = []
    sample_items = 0

    for p in range(pages):
        data = first if p == 0 else fetch_fn(term, limit=50, offset=p * 50)
        for item in data.get("results", []):
            sid = (item.get("seller") or {}).get("id")
            price = item.get("price")
            if sid:
                sellers.add(sid)
            if price is not None:
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
        "total_results": int(total_results),
        "unique_sellers_sample": int(unique_sellers),
        "sample_items": int(sample_items),
        "price_median": price_median,
        "score": score,
    }

# =========================
# Main
# =========================
def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("ML_CLIENT_ID ou ML_CLIENT_SECRET não configurados no GitHub Secrets.")

    sb = sb_client()

    # evita rodar toda hora (cron + manual)
    if not should_run(sb):
        print(f"⏭️ Skip: ainda não passaram {MIN_HOURS_BETWEEN_RUNS}h desde o último run.")
        return

    cleanup_old_snapshots(sb, KEEP_DAYS)

    # 1) Pega refresh_token do Supabase (NUNCA do GitHub)
    refresh_token = get_refresh_token_from_supabase(sb)

    # 2) Renova access token
    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as e:
        print(f"❌ Falha ao renovar token do Mercado Livre: {e}")
        return

    access_token = token_data.get("access_token")
    new_refresh = token_data.get("refresh_token")

    # 3) Se o ML devolveu refresh_token novo (rotação), salva no Supabase
    if new_refresh and new_refresh != refresh_token:
        print("⚠️ Mercado Livre devolveu um NOVO refresh_token (rotação). Salvando no Supabase...")
        set_refresh_token_in_supabase(sb, new_refresh)

    # 4) Busca termos e roda
    terms = fetch_terms(sb)
    print(f"📌 Termos ativos: {len(terms)}")

    # Função padrão: tenta auth; se der 401/403, cai pro público
    def fetch(term, limit=50, offset=0):
        try:
            return ml_search_auth(term, access_token, limit=limit, offset=offset)
        except requests.HTTPError as he:
            status = getattr(he.response, "status_code", None)
            if status in (401, 403):
                print(f"🪪 Token inválido/sem permissão ({status}). Tentando busca pública sem token...")
                return ml_search_public(term, limit=limit, offset=offset)
            raise

    ok = 0
    for term in terms:
        print(f"🔎 Analisando: {term}")
        try:
            data = analyze_term(term, fetch_fn=fetch, pages=2)
            sb.table("snapshots").insert(data).execute()
            ok += 1
            print("✅ Salvou no Supabase")
        except Exception as e:
            print(f"⚠️ Erro no termo '{term}': {e}")

    # marca run no app_state
    mark_run(sb)
    print(f"🏁 Finalizado. Termos OK: {ok}/{len(terms)}")

if __name__ == "__main__":
    main()

