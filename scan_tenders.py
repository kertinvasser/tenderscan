import json
import os
import re
import time
from email.message import EmailMessage
from pathlib import Path
import smtplib
import requests

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

KEYWORDS = [
    "photo", "photography", "video", "videography", "audiovisual",
    "content", "communication", "social media", "campaign"
]

CPV_PREFIXES = ["7996", "921"]

PAGE_SIZE = 50
MAX_PAGES = 20

STATE_FILE = Path("sent_ids.json")


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def kw_match(text):
    t = text.lower()
    return any(k in t for k in KEYWORDS)


def cpv_match(cpv):
    if not cpv:
        return False
    cpv = re.sub(r"\D", "", str(cpv))
    return any(cpv.startswith(p) for p in CPV_PREFIXES)


def fetch_page(page_number):
    params = {
        "apiKey": API_KEY,
        "text": "***",
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
        },
        "sort": [{"field": "startDate", "order": "DESC"}],
    }

    r = requests.post(API_URL, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def send_email(matches):
    to_addr = os.environ.get("EMAIL_TO")
    if not to_addr:
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU tenders: {len(matches)}"
    msg["From"] = os.environ.get("EMAIL_FROM", "")
    msg["To"] = to_addr

    lines = []
    for m in matches:
        lines.append(f"{m['title']}\n{m['url']}\n")

    msg.set_content("\n".join(lines))

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def main():
    seen = load_seen()
    matches = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        items = data.get("results") or data.get("hits") or []
        if not items:
            break

        for it in items:
            item_id = str(it.get("id"))
            if not item_id or item_id in seen:
                continue

            title = it.get("title", "")
            desc = it.get("description", "")
            cpv = it.get("cpvCode")
            url = it.get("url", "")

            if kw_match(f"{title}\n{desc}") or cpv_match(cpv):
                matches.append({"id": item_id, "title": title, "url": url})

        time.sleep(0.2)

    if not matches:
        print("No new matches.")
        return

    for m in matches:
        seen.add(m["id"])
        print(m["title"], m["url"])

    save_seen(seen)
    send_email(matches)


if __name__ == "__main__":
    main()
