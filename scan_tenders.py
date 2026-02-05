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

# Match if ANY of these prefixes match (OR), e.g. 7934xxxx or 7996xxxx etc.
CPV_PREFIXES = [
    "793",   # market research/advertising/marketing (broad)
    "7934",  # advertising/marketing services (narrower)
    "794",   # business/management/PR (broad)
    "7941",  # PR/communications (narrower)
    "799",   # miscellaneous business services (broad)
    "7996",  # photographic services (narrower)
    "921",   # film/video services
    "922",   # radio/television services
    "923",   # entertainment-related services (sometimes used)
    "798",   # publishing/printing/related (sometimes used for content)
]

# Optional keyword match (kept on; CPV match alone is enough)
KEYWORDS = [
    "photo", "photography", "photographer",
    "video", "videography", "filming", "film",
    "audiovisual", "audio-visual",
    "content", "campaign", "communication", "communications",
    "social media", "digital", "storytelling",
]

PAGE_SIZE = 50
MAX_PAGES = 40
DAYS_BACK = 30

STATE_FILE = Path("sent_ids.json")


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "[]")
        return set(map(str, data))
    except json.JSONDecodeError:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def kw_match(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)


def cpv_match(cpv) -> bool:
    if not cpv:
        return False
    cpv_digits = re.sub(r"\D", "", str(cpv))
    return any(cpv_digits.startswith(p) for p in CPV_PREFIXES)


def fetch_page(page_number: int) -> dict:
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
                    {
                        "range": {
                            "startDate": {
                                "gte": f"now-{DAYS_BACK}d/d",
                                "lte": "now",
                            }
                        }
                    },
                ]
            }
        },
        "sort": [{"field": "startDate", "order": "DESC"}],
    }

    r = requests.post(API_URL, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_items(data: dict) -> list[dict]:
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("hits"), list):
        return data["hits"]
    rl = data.get("resultList")
    if isinstance(rl, dict) and isinstance(rl.get("result"), list):
        return rl["result"]
    return []


def send_email(matches: list[dict]) -> bool:
    email_to = os.environ.get("EMAIL_TO", "").strip()
    if not email_to:
        return False

    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = os.environ.get("SMTP_PORT", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    email_from = os.environ.get("EMAIL_FROM", smtp_user).strip()

    if not (smtp_host and smtp_port and smtp_user and smtp_pass and email_from):
        print("Email not sent: missing SMTP/EMAIL env vars.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"EU tenders (last {DAYS_BACK} days): {len(matches)}"
    msg["From"] = email_from
    msg["To"] = email_to

    lines = []
    for m in matches:
        lines.append(m["title"])
        if m.get("cpv"):
            lines.append(f"CPV: {m['cpv']}")
        lines.append(m["url"])
        lines.append("")

    msg.set_content("\n".join(lines).strip() + "\n")

    with smtplib.SMTP(smtp_host, int(smtp_port)) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)

    return True


def main() -> None:
    seen = load_seen()
    matches: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        items = extract_items(data)
        if not items:
            break

        for it in items:
            item_id = str(it.get("id") or "").strip()
            if not item_id or item_id in seen:
                continue

            title = (it.get("title") or "").strip()
            desc = (it.get("description") or "").strip()
            cpv = it.get("cpvCode")
            url = (it.get("url") or it.get("link") or "").strip()

            # include if CPV matches OR keywords match
            if cpv_match(cpv) or kw_match(f"{title}\n{desc}"):
                matches.append({"id": item_id, "title": title, "url": url, "cpv": cpv})

        time.sleep(0.15)

    if not matches:
        print("No new matches.")
        return

    for m in matches:
        print(f"- {m['title']} | CPV: {m.get('cpv')} | {m['url']}")

    emailed = send_email(matches)
    print(f"TOTAL MATCHES (last {DAYS_BACK} days): {len(matches)} | emailed={emailed}")

    for m in matches:
        seen.add(m["id"])
    save_seen(seen)


if __name__ == "__main__":
    main()
