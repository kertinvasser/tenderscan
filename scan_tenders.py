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

# Pull window (testing)
DAYS_BACK = 60
PAGE_SIZE = 50
MAX_PAGES = 20

# Networking hardening
TIMEOUT_SECONDS = 60
MAX_RETRIES = 6          # per page
BACKOFF_BASE = 1.2       # exponential backoff base
BACKOFF_JITTER = 0.4     # add randomness to avoid thundering herd

STATE_FILE = Path("sent_ids.json")

KEYWORD_PATTERNS = [
    r"\bphotograph(y|er|ic|ing)\b",
    r"\bphoto(s)?\b",
    r"\bphoto[- ]?(shoot|shooting|session|coverage|reportage|production)\b",
    r"\bphotoshoot\b",
    r"\bimage(s)?\b",
    r"\bvisual(s)?\b",
    r"\bvisual[- ]?(asset(s)?|material(s)?|content|identity|campaign)\b",
    r"\bbrand[- ]?(asset(s)?|content|campaign)\b",
    r"\bvideo(s)?\b",
    r"\bvideograph(y|er|ic|ing)\b",
    r"\bfilm(making|maker|ing)?\b",
    r"\baudio[- ]?visual\b",
    r"\bmultimedia\b",
    r"\bpost[- ]?production\b",
    r"\bediting\b",
    r"\bcolour[- ]?grading\b|\bcolor[- ]?grading\b",
    r"\bsubtitl(e|ing|es)\b|\bcaption(s|ing)?\b",
    r"\bcommunication(s)?\b",
    r"\bcommunications?\s+campaign\b",
    r"\bawareness\s+campaign\b",
    r"\bvisibility\b",
    r"\boutreach\b",
    r"\bsocial\s+media\b",
    r"\bdigital\s+campaign\b",
    r"\bcontent\s+creation\b|\bcontent\s+production\b",
    r"\bcreative\s+(services|agency|production|content)\b",
    r"\bgraphic\s+design\b",
    r"\bdesign\s+services\b",
    r"\blayout\b",
    r"\binfographic(s)?\b",
    r"\banimation\b|\bmotion\s+design\b|\bmotion\s+graphic(s)?\b",
    r"\bposter(s)?\b|\bbanner(s)?\b|\bbrochure(s)?\b|\bleaflet(s)?\b",
]

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

SESSION = requests.Session()


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


def _sleep_backoff(attempt: int):
    # attempt: 1..MAX_RETRIES
    base = BACKOFF_BASE ** (attempt - 1)
    jitter = 1 + (BACKOFF_JITTER * (0.5 - (time.time() % 1)))  # deterministic-ish jitter
    delay = min(25, base * jitter)
    time.sleep(max(0.5, delay))


def fetch_page(page_number: int) -> dict | None:
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

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.post(API_URL, params=params, json=body, timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            print(f"PAGE {page_number}: network error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt)
                continue
            print(f"PAGE {page_number}: giving up after {MAX_RETRIES} attempts.")
            return None
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            print(f"PAGE {page_number}: HTTP error {status} on attempt {attempt}/{MAX_RETRIES}: {e}")
            # Retry only on likely transient statuses
            if status in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                _sleep_backoff(attempt)
                continue
            return None
        except Exception as e:
            print(f"PAGE {page_number}: unexpected error: {e}")
            return None


def send_email(matches):
    to_addr = os.environ.get("EMAIL_TO")
    if not to_addr:
        print("EMAIL_TO not set; skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU F&T keyword matches: {len(matches)} (last {DAYS_BACK}d)"
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
    failed_pages = 0

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(page)
        if data is None:
            failed_pages += 1
            # don’t kill the whole run; continue to next page
            continue

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

    print(f"Checked {checked} items. Failed pages: {failed_pages}.")

    if not matches:
        print(f"No keyword matches found in last {DAYS_BACK} days.")
        return

    new_matches = [m for m in matches if m["new"]]

    print(f"TOTAL keyword matches (last {DAYS_BACK} days): {len(matches)}")
    print(f"NEW matches (not in sent_ids.json): {len(new_matches)}")
    print("---- MATCH LIST (all, first 200) ----")
    for m in matches[:200]:
        print(m["title"])
        print(m["url"])
        if m.get("date"):
            print(f"date: {m['date']}")
        print("")

    # mark seen + save
    for m in new_matches:
        seen.add(m["id"])
    save_seen(seen)

    # send mail only for new
    if new_matches:
        send_email(new_matches)
    else:
        print("No NEW matches to email (all were already seen).")


if __name__ == "__main__":
    main()
