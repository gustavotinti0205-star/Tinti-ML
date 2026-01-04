import os
import math
import time
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client
from statistics import median

# ===============================
# CONFIGURAÇÕES
# ===============================
SITE_ID = "MLB"
INTERVAL_HOURS = 72
STATE_KEY = "ml_scanner"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ML_BASE = "https://api.mercadolibre.com"


# ===============================
# SUPABASE
# ===============================
def sb_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def should_run(sb):
    res = sb.table("app_state").select("value").eq("key", STATE_KEY).execute()

    if not res.data:
        sb.table("app_state").insert(
            {"key": STATE_KEY, "value": {"last_run_at": None}}
        ).execute()
        last_run = None
    else:
        last_run = parse_ts(res.data[0]["value"].get("last_run_at"))

    now = utcnow()

    if last_run is None:
        sb.table("app_state").update(
            {"value": {"last_run_at": now.isoformat()}}
        ).eq("key", STATE_KEY).execute()
        return True

    if now - last_run >= timedelta(hours=INTERVAL_HOURS):
        sb.table("app_state").update(
            {"value": {"last_run_at": now.isoformat()}}
        ).eq("key", STATE_KEY).execute()
        return True

    return False


# ===============================
# MERCADO LIVRE
# ===============================
def ml_search(term, limit=50, offset=0):
    url = f"{ML_BASE}/sites/{SITE_ID}/search"
    params = {
        "q": term,
        "limit": limit,
        "offset": offset
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ===============================
# ANALISADOR
# ===============================
def analyze_term(term, pages=4, limit=50):
    first = ml_search(term, limit=limit, offset=0)
    total_results = first.get("paging", {}).get("total", 0)

    sellers = set()
    prices = []
    sample_items = 0

    for p in range(pages):
        data = first if p == 0 else ml_search(term, limit=limit, offset=p * limit)

        for item in data.get("results", []):
            seller = item.get("seller", {}).get("id")
            price = item.get("price")

            if seller:
                sellers.add(seller)
            if price:
                prices.append(price)

            sample_items += 1

        time.sleep(0.25)

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
        "score": score
    }


# ===============================
# MAIN
# ===============================
def main():
    sb = sb_client()

    if not should_run(sb):
        print("⏭️ Skip: ainda não passaram 72h")
        return

    keywords = sb.table("keywords").select("term").eq("is_active", True).execute()

    for row in keywords.data:
        term = row["term"]
        print(f"🔎 Analisando: {term}")

        data = analyze_term(term)

        sb.table("snapshots").insert({
            "term": data["term"],
            "total_results": data["total_results"],
            "unique_sellers_sample": data["unique_sellers_sample"],
            "sample_items": data["sample_items"],
            "price_median": data["price_median"],
            "score": data["score"]
        }).execute()


if __name__ == "__main__":
    main()
