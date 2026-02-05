import json
import time
from pathlib import Path

import requests

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

PAGE_SIZE = 50
PAGE_NUMBER = 1

SAMPLE_FILE = Path("sample_response.json")


def extract_items(data: dict):
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("hits"), list):
        return data["hits"]
    rl = data.get("resultList")
    if isinstance(rl, dict) and isinstance(rl.get("result"), list):
        return rl["result"]
    return []


def fetch_page(page_number: int) -> dict:
    params = {
        "apiKey": API_KEY,
        "text": "*",
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }

    body = {
        "query": {
            "bool": {
                "must": [
                    {"terms": {"type": ["2"]}},
                    {"terms": {"status": ["31094501", "31094502"]}},
                ]
            }
        }
    }

    r = requests.post(API_URL, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def pick_first(it: dict, keys: list[str]) -> str:
    for k in keys:
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def main() -> None:
    data = fetch_page(PAGE_NUMBER)
    SAMPLE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Saved raw API response to: {SAMPLE_FILE}")

    items = extract_items(data)
    print(f"PAGE {PAGE_NUMBER}: items={len(items)}")

    if not items:
        return

    first = items[0]
    print("\nFIRST ITEM KEYS:")
    print(sorted(list(first.keys())))

    print("\nFIRST 5 ITEMS (best-guess fields):")
    for it in items[:5]:
        title = pick_first(it, ["title", "name", "topic", "callTitle", "metadataTitle"])
        url = pick_first(it, ["url", "link", "webLink", "detailUrl", "detailsUrl"])
        cpv = it.get("cpvCode") or it.get("cpv") or it.get("cpvs")
        print(f"- title: {title or '[empty]'}")
        print(f"  url:   {url or '[empty]'}")
        print(f"  cpv:   {cpv}")
        print("")

    time.sleep(0.1)


if __name__ == "__main__":
    main()
