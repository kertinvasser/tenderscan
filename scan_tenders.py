import time
import requests

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

PAGE_SIZE = 50
MAX_PAGES = 2  # keep small for debugging


def extract_items(data: dict) -> list[dict]:
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
        "text": "*",  # wildcard
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }

    # No date filter here. This is purely to prove we get items back.
    body = {
        "query": {
            "bool": {
                "must": [
                    {"terms": {"type": ["2"]}},
                    {"terms": {"status": ["31094501", "31094502"]}},
                ]
            }
        },
        "sort": [{"field": "startDate", "order": "DESC"}],
    }

    r = requests.post(API_URL, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def pick_url(it: dict) -> str:
    return (it.get("url") or it.get("link") or "").strip()


def main() -> None:
    total_items = 0
    printed = 0

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        items = extract_items(data)
        print(f"PAGE {page}: items={len(items)}")

        if not items:
            break

        total_items += len(items)

        for it in items:
            title = (it.get("title") or "").strip()
            cpv = it.get("cpvCode")
            url = pick_url(it)
            start_date = it.get("startDate")
            pub_date = it.get("publicationDate")

            # print first 20 items only
            if printed < 20:
                print(f"- {title}")
                print(f"  CPV: {cpv} | startDate: {start_date} | publicationDate: {pub_date}")
                print(f"  {url}")
                printed += 1

        time.sleep(0.15)

    print(f"TOTAL ITEMS RETURNED: {total_items}")


if __name__ == "__main__":
    main()
