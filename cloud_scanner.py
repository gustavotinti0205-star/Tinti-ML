import os
import time
import math
import requests
from statistics import median
from datetime import datetime, timedelta, timezone

from supabase import create_client

ML_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLB"

# GitHub Secrets / Env
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

CLIENT_ID = os.getenv("ML_CLIENT_ID")
CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")

# Configs
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MIN_HOURS_BETWEEN_RUNS", "23"))  # evita duplicar no mesmo dia
PAGES = int(os.getenv("PAGES", "2"))  # quantas páginas de 50 itens para coletar amostra (2 = 100 itens)

# Fallback se não tiver keywords no banco
FALLBACK_TERMS = [
    "cadeira ergonômica",
    "suporte notebook",
    "luminária led",
    "aspirador portátil",
]


def sb_client():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY não configurados.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------
# Supabase app_state helpers
# ---------------------------
def get_app_state(sb, key: str):
    """
    Retorna dict do campo value (jsonb) ou None
    """
    res = sb.table("app_state").select("value").eq("key", key).limit(1).execute()
    if res.data and len(res.data) > 0:
        return res.data[0].get("value")
    return None


def set_app_state(sb, key: str, value: dict):
    """
    Upsert em app_state: key + value(jsonb)
    """
    payload = {
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sb.table("app_state").upsert(payload).execute()


def get_refresh_token_from_db(sb) -> str:
    data = get_app_state(sb, "ML_REFRESH_TOKEN")
    if not data or "token" not in data or not data["token"]:
        raise RuntimeError(
            "Não achei o refresh token no Supabase.\n"
            "Crie em app_state:\n"
            "key = ML_REFRESH_TOKEN\n"
            'value = {"token":"SEU_REFRESH_TOKEN_AQUI"}'
        )
    return data["token"]


def save_refresh_token_to_db(sb, new_token: str):
    set_app_state(sb, "ML_REFRESH_TOKEN", {"token": new_token})


# ---------------------------
# Run control
# ---------------------------
def should_run(sb) -> bool:
    state = get_app_state(sb, "ml_scanner") or {}
    last = state.get("last_run_at")
    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return True

    hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return hours >= MIN_HOURS_BETWEEN_RUNS


def mark_ran(sb):
    set_app_state(sb, "ml_scanner", {"last_run_at": datetime.now(timezone.utc).isoformat()})


# ---------------------------
# Cleanup snapshots
# ---------------------------
def cleanup_old_snapshots(sb, days=60):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sb.table("snapshots").delete().lt("run_at", cutoff.isoformat()).execute()
    print(f"🧹 Limpeza feita: registros mais antigos que {days} dias removidos")


# ---------------------------
# Mercado Livre OAuth
# ---------------------------
def refresh_access_token(refresh_token: str):
    url = f"{ML_BASE}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    j = r.json()

    access_token = j.get("access_token")
    new_refresh = j.get("refresh_token")  # pode vir em rotação
    if not access_token:
        raise RuntimeError(f"Resposta sem access_token: {j}")

    return access_token, new_refresh


# ---------------------------
# Mercado Livre Search
# ---------------------------
def ml_search(term, access_token, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {"q": term, "limit": limit, "offset": offset}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; TintiMLScanner/1.0; +https://github.com)",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Connection": "keep-alive",
    }

    # Retry simples (429 / 5xx)
    for attempt in range(1, 6):
        r = requests.get(url, params=params, headers=headers, timeout=30)

        if r.status_code == 429:
            wait = 2 * attempt
            print(f"⏳ 429 (rate limit). Aguardando {wait}s e tentando de novo...")
            time.sleep(wait)
            continue

        if 500 <= r.status_code < 600:
            wait = 2 * attempt
            print(f"⏳ {r.status_code} (server). Aguardando {wait}s e tentando de novo...")
            time.sleep(wait)
            continue

        # 401/403 geralmente token inválido/escopo/conta
        if r.status_code in (401, 403):
            # deixa o caller tratar
            r.raise_for_status()

        r.raise_for_status()
        return r.json()

    # se nunca retornou
    r.raise_for_status()


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
            if price is not None:
                prices.append(price)
            sample_items += 1

        time.sleep(0.25)

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
        "score": score,
    }


# ---------------------------
# Terms
# ---------------------------
def load_terms(sb):
    """
    1) Tenta carregar da tabela keywords (term, is_active)
    2) Se falhar/vazio, usa fallback
    """
    try:
        res = sb.table("keywords").select("term").eq("is_active", True).execute()
        terms = [row["term"] for row in (res.data or []) if row.get("term")]
        if terms:
            return terms
    except Exception:
        pass

    return FALLBACK_TERMS


# ---------------------------
# Main
# ---------------------------
def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("ML_CLIENT_ID / ML_CLIENT_SECRET não configurados nos secrets do GitHub.")

    sb = sb_client()

    # Controle para não rodar duplicado no mesmo dia
    if not should_run(sb):
        print(f"⏭️ Skip: ainda não passaram {MIN_HOURS_BETWEEN_RUNS}h desde o último run.")
        return

    # Limpa histórico antigo
    cleanup_old_snapshots(sb, days=RETENTION_DAYS)

    # Pega refresh token do Supabase
    refresh_token = get_refresh_token_from_db(sb)

    # Gera access token
    access_token, new_refresh = refresh_access_token(refresh_token)

    # Se rotacionou refresh_token, salva no Supabase automaticamente
    if new_refresh and new_refresh != refresh_token:
        print("🔁 Mercado Livre devolveu um NOVO refresh_token (rotação). Salvando no Supabase...")
        save_refresh_token_to_db(sb, new_refresh)

    terms = load_terms(sb)
    print(f"🧾 Termos ativos: {len(terms)}")

    # Processa termos
    for term in terms:
        print(f"🔎 Analisando: {term}")
        try:
            data = analyze_term(term, access_token, pages=PAGES)
            sb.table("snapshots").insert(data).execute()
            print("✅ Salvou no Supabase")
        except requests.HTTPError as e:
            # Se deu 401/403 pode ser que o access token tenha expirado/inválido.
            # Tenta 1 vez renovar o token e repetir o termo.
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                print(f"🔐 Token inválido/expirado ({status}). Renovando token e tentando novamente...")
                try:
                    # recarrega refresh token atual do DB (pode ter rotacionado)
                    refresh_token = get_refresh_token_from_db(sb)
                    access_token, new_refresh = refresh_access_token(refresh_token)

                    if new_refresh and new_refresh != refresh_token:
                        print("🔁 Rotação detectada. Salvando novo refresh_token no Supabase...")
                        save_refresh_token_to_db(sb, new_refresh)

                    # tenta de novo o mesmo termo
                    data = analyze_term(term, access_token, pages=PAGES)
                    sb.table("snapshots").insert(data).execute()
                    print("✅ Salvou no Supabase (retry)")
                except Exception as e2:
                    print(f"⚠️ Erro no termo '{term}' após retry: {e2}")
            else:
                print(f"⚠️ HTTP erro no termo '{term}': {e}")
        except Exception as e:
            print(f"⚠️ Erro no termo '{term}': {e}")

    # marca que rodou
    mark_ran(sb)
    print("🏁 Finalizado.")


if __name__ == "__main__":
    main()
