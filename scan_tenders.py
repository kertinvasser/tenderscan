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

# Pull window
DAYS_BACK = 60  # bump this while testing
PAGE_SIZE = 50
MAX_PAGES = 20

STATE_FILE = Path("sent_ids.json")

# Keyword matching: regex patterns (case-insensitive)
# Goal: catch photo/video/content/campaign production WITHOUT matching "photonics"/"photovoltaic" etc.
KEYWORD_PATTERNS = [
    # photo / photography
    r"\bphotograph(y|er|ic|ing)\b",
    r"\bphoto(s)?\b",
    r"\bphoto[- ]?(shoot|shooting|session|coverage|reportage|production)\b",
    r"\bphotoshoot\b",
    r"\bimage(s)?\b",
    r"\bvisual(s)?\b",
    r"\bvisual[- ]?(asset(s)?|material(s)?|content|identity|campaign)\b",
    r"\bbrand[- ]?(asset(s)?|content|campaign)\b",

    # video / audiovisual
    r"\bvideo(s)?\b",
    r"\bvideograph(y|er|ic|ing)\b",
    r"\bfilm(making|maker|ing)?\b",
    r"\baudio[- ]?visual\b",
    r"\bmultimedia\b",
    r"\bpost[- ]?production\b",
    r"\bediting\b",
    r"\bcolour[- ]?grading\b|\bcolor[- ]?grading\b",
    r"\bsubtitl(e|ing|es)\b|\bcaption(s|ing)?\b",

    # comms / campaigns (where photo/video is usually embedded)
    r"\bcommunication(s)?\b",
    r"\bcommunications?\s+campaign\b",
    r"\bawareness\s+campaign\b",
    r"\bvisibility\b",
    r"\boutreach\b",
    r"\bsocial\s+media\b",
    r"\bdigital\s+campaign\b",
    r"\bcontent\s+creation\b|\bcontent\s+production\b",
    r"\bcreative\s+(services|agency|production|content)\b",

    # design / print deliverables that often include visuals
    r"\bgraphic\s+design\b",
    r"\bdesign\s+services\b",
    r"\blayout\b",
    r"\binfographic(s)?\b",
    r"\banimation\b|\bmotion\s+design\b|\bmotion\s+graphic(s)?\b",
    r"\bposter(s)?\b|\bbanner(s)?\b|\bbrochure(s)?\b|\bleaflet(s)?\b",
]

# Exclude common scientific false positives that contain "photo" as a substring
NEGATIVE_PATTERNS = [
    r"\bphotonics?\b",
    r"\bphotonic\b",
    r"\bphotovolta(ic|ics)?\b",
    r"\bphotoelectr(ic|on|ons|onic|onics)?\b",
    r"\bphotosynth(esis|etic)\b",
    r"\bphotocatal(ysis|yst|ytic)\b",
    r"\bphotometr(y|ic)\b",
    r"\bphotochem(istry|ical)\b",
]

KW_RE = re.compile("|".join(KEYWORD_PATTERNS), re.IGNORECASE)
NEG_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)


def load_seen():
    if STATE_FILE.exists():
        txt = STATE_FILE.read_text().strip()
        if not txt:
            return set()
        try:
            return set(json.loads(txt))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def kw_match(text: str) -> bool:
    if not text:
        return False
    if NEG_RE.search(text):
        return False
    return KW_RE.search(text) is not None


def fetch_page(page_number: int) -> dict:
    params = {
        "apiKey": API_KEY,
        "text": "***",  # keep broad; we filter locally with KW_RE
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

    r = requests.post(API_URL, params=params, json=body, timeout=45)
    r.raise_for_status()
    return r.json()


def send_email(matches):
    to_addr = os.environ.get("EMAIL_TO")
    if not to_addr:
        print("EMAIL_TO not set; skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU Funding & Tenders keyword matches: {len(matches)} (last {DAYS_BACK}d)"
    msg["From"] = os.environ.get("EMAIL_FROM", "")
    msg["To"] = to_addr

    lines = []
    for m in matches:
        lines.append(m["title"])
        lines.append(m["url"])
        if m.get("date"):
            lines.append(f"date: {m['date']}")
        lines.append("")

    msg.set_content("\n".join(lines).strip() + "\n")

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)

    print(f"Email sent to {to_addr} with {len(matches)} matches.")


def main():
    seen = load_seen()
    matches = []
    checked = 0

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)

        items = data.get("results") or data.get("hits") or []
        print(f"PAGE {page}: items={len(items)}")
        if not items:
            break

        for it in items:
            checked += 1
            item_id = str(it.get("id") or "").strip()
            if not item_id:
                continue

            title = (it.get("title") or "").strip()
            url = (it.get("url") or "").strip()
            # fields vary; use several candidates
            desc = (it.get("description") or it.get("summary") or "").strip()
            start_date = it.get("startDate") or it.get("publicationDate") or ""

            blob = f"{title}\n{desc}".strip()

            if kw_match(blob):
                is_new = item_id not in seen
                matches.append(
                    {
                        "id": item_id,
                        "title": title or "[no title field]",
                        "url": url or "[no url field]",
                        "date": start_date,
                        "new": is_new,
                    }
                )

        time.sleep(0.15)

    # Show matches in logs (so you can see it even if email fails)
    if not matches:
        print(f"No keyword matches found in last {DAYS_BACK} days. Checked {checked} items.")
        return

    # Email only NEW ones (prevents spam)
    new_matches = [m for m in matches if m["new"]]

    print(f"TOTAL keyword matches (last {DAYS_BACK} days): {len(matches)}")
    print(f"NEW matches (not in sent_ids.json): {len(new_matches)}")
    print("---- MATCH LIST (all) ----")
    for m in matches[:200]:  # keep logs readable
        print(m["title"])
        print(m["url"])
        if m.get("date"):
            print(f"date: {m['date']}")
        print("")

    # Mark as seen (only new ones)
    for m in new_matches:
        seen.add(m["id"])
    save_seen(seen)

    if new_matches:
        send_email(new_matches)
    else:
        print("No NEW matches to email (all matches were already seen).")


if __name__ == "__main__":
    main()
