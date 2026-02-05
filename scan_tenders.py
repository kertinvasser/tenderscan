import json
import os
import re
import time
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

# Discovery run settings
PAGE_SIZE = 50
MAX_PAGES = 80
DAYS_BACK = 365

# Write discovery output to a file too (handy if logs truncate)
OUT_FILE = Path("discover_output.jsonl")


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("POST",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def norm(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, (int, float, bool)):
        return str(x)
    if isinstance(x, list):
        return " ".join(norm(v) for v in x)
    if isinstance(x, dict):
        return " ".join(norm(v) for v in x.values())
    return str(x)


def best_url(item: dict) -> str:
    for k in ("url", "link"):
        v = norm(item.get(k)).strip()
        if v:
            return v
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("url", "link"):
            v = norm(md.get(k)).strip()
            if v:
                return v
    return ""


def db_label(item: dict) -> str:
    return norm(item.get("databaseLabel") or item.get("database") or "").strip()


def extract_items(data: dict) -> list[dict]:
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("hits"), list):
        return data["hits"]
    rl = data.get("resultList")
    if isinstance(rl, dict) and isinstance(rl.get("result"), list):
        return rl["result"]
    return []


def fetch_page(sess: requests.Session, page_number: int) -> dict:
    params = {
        "apiKey": API_KEY,
        "text": "*",
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }

    body = {
        "query": {
            "bool": {
                "should": [
                    {"range": {"startDate": {"gte": f"now-{DAYS_BACK}d/d", "lte": "now"}}},
                    {"range": {"publicationDate": {"gte": f"now-{DAYS_BACK}d/d", "lte": "now"}}},
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"field": "startDate", "order": "DESC"}],
    }

    r = sess.post(API_URL, params=params, json=body, timeout=45)
    r.raise_for_status()
    return r.json()


def main() -> None:
    sess = make_session()

    db_counts: dict[str, int] = {}
    url_prefix_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}

    OUT_FILE.write_text("", encoding="utf-8")

    total = 0

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(sess, page)
        items = extract_items(data)
        print(f"PAGE {page}: items={len(items)}")
        if not items:
            break

        for item in items:
            total += 1
            dbl = db_label(item) or "(empty)"
            db_counts[dbl] = db_counts.get(dbl, 0) + 1

            url = best_url(item)
            pref = (url[:80] if url else "(no url)")
            url_prefix_counts[pref] = url_prefix_counts.get(pref, 0) + 1

            t = norm(item.get("type") or "(no type)")
            type_counts[t] = type_counts.get(t, 0) + 1

            st = norm(item.get("status") or "(no status)")
            status_counts[st] = status_counts.get(st, 0) + 1

            # Save a thin record for later inspection
            rec = {
                "db": dbl,
                "type": t,
                "status": st,
                "url": url,
                "title": norm(item.get("title") or item.get("summary") or ""),
            }
            OUT_FILE.write_text(OUT_FILE.read_text(encoding="utf-8") + json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

        time.sleep(0.25)

    print(f"\nTOTAL ITEMS SCANNED: {total}")

    print("\n=== DISCOVER: databaseLabel counts (top) ===")
    for k, v in sorted(db_counts.items(), key=lambda kv: kv[1], reverse=True)[:50]:
        print(f"{v:>6}  {k}")

    print("\n=== DISCOVER: type counts (top) ===")
    for k, v in sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)[:50]:
        print(f"{v:>6}  {k}")

    print("\n=== DISCOVER: status counts (top) ===")
    for k, v in sorted(status_counts.items(), key=lambda kv: kv[1], reverse=True)[:50]:
        print(f"{v:>6}  {k}")

    print("\n=== DISCOVER: url prefix buckets (top) ===")
    for k, v in sorted(url_prefix_counts.items(), key=lambda kv: kv[1], reverse=True)[:50]:
        print(f"{v:>6}  {k}")

    print(f"\nSaved thin records to {OUT_FILE} (commit it if you want to inspect in repo).")


if __name__ == "__main__":
    main()
