import json
import os
import re
import time
from email.message import EmailMessage
from pathlib import Path

import requests
import smtplib

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

# what you actually want
KEYWORDS = [
    "photo", "photography", "video", "videography",
    "audiovisual", "film", "content", "communication",
    "social media", "campaign", "media production"
]

# ANY of these is enough
CPV_PREFIXES = [
    "7996",  # photographic services
    "921",   # motion picture / video
    "7934",  # advertising / marketing
    "7982",  # printing + design
]

PAGE_SIZE = 50
MAX_PAGES = 10
DAYS_BACK = 90  # widen window so you SEE results

STATE_FILE = Path("sent_ids.json")


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def kw_match(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in KEYWORDS)


def cpv_match(cpv) -> bool:
    if not cpv:
        return False
    cpv = re.sub(r"\D", "", str(cpv))
    return any(cpv.startswith(p) for p in CPV_PREFIXES)


def extract_items(data: dict) -> list[dict]:
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("hits"), list):
        return data["hits"]
    rl = data.get("resultList")
    if isinstance(rl, dict) and isinstance(rl.get("result"), list):
        return rl["result"]
    return []


def fetch_page(page: int) -> dict:
    params = {
        "apiKey": API_KEY,
        "text": "*",
        "pageSize": PAGE_SIZE,
        "pageNumber": page,
    }

    body = {
        "query": {
            "bool": {
                "must": [
                    # PROCUREMENT NOTICES
                    {"terms": {"contentType": ["PROCUREMENT"]}},
                    # open + ongoing
                    {"terms": {"status": ["OPEN", "PUBLISHED"]}},
                    {
                        "range": {
                            "publicationDate": {
                                "gte": f"now-{DAYS_BACK}d/d",
                                "lte": "now"
                            }
                        }
                    },
                ]
            }
        },
        "sort": [{"field": "publicationDate", "order": "DESC"}],
    }

    r = requests.post(API_URL, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def send_email(matches: list[dict]) -> None:
    to_addr = os.environ.get("EMAIL_TO")
    if not to_addr:
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU tenders matched: {len(matches)}"
    msg["From"] = os.environ.get("EMAIL_FROM", "")
    msg["To"] = to_addr

    lines = []
    for m in matches:
        lines.append(
            f"{m['title']}\n"
            f"CPV: {m.get('cpv')}\n"
            f"{m['url']}\n"
        )

    msg.set_content("\n".join(lines))

    with smtplib.SMTP(
        os.environ["SMTP_HOST"],
        int(os.environ["SMTP_PORT"]),
    ) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def main() -> None:
    seen = load_seen()
    matches: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        items = extract_items(data)

        print(f"PAGE {page}: items={len(items)}")

        if not items:
            break

        for it in items:
            item_id = str(it.get("reference") or it.get("id"))
            if not item_id or item_id in seen:
                continue

            title = it.get("title", "") or it.get("summary", "")
            desc = it.get("content", "")
            cpv = it.get("cpvCode") or it.get("cpv")
            url = it.get("url") or it.get("reference")

            if cpv_match(cpv) or kw_match(f"{title}\n{desc}"):
                matches.append({
                    "id": item_id,
                    "title": title.strip(),
                    "cpv": cpv,
                    "url": url,
                })

        time.sleep(0.2)

    if not matches:
        print("No new matches.")
        return

    for m in matches:
        print(m["title"])
        print("CPV:", m.get("cpv"))
        print(m["url"])
        print("---")
        seen.add(m["id"])

    save_seen(seen)
    send_email(matches)
    print(f"TOTAL MATCHES: {len(matches)}")


if __name__ == "__main__":
    main()
