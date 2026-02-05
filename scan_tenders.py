import json
import os
import re
import time
from email.message import EmailMessage
from pathlib import Path
import smtplib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

# ---- What we want: actual tenders on the portal (human pages), NOT topicDetails/*.json
PORTAL_URL_MUST_CONTAIN = "/portal/screen/opportunities/calls-for-tenders/"

# ---- Procurement vs funding:
# In this API, values vary, but earlier you got topicDetails with type "2".
# We keep type filter loose and use the URL filter as the real guardrail.
SEARCH_TYPES = []  # keep empty, rely on URL filter (safer)
SEARCH_STATUSES = []  # optional; leave empty

# ---- Matching (client-side): keyword OR CPV prefix
KEYWORDS = [
    "photo", "photography", "photographer",
    "video", "videography", "videographer",
    "filming", "film production", "video production",
    "audiovisual", "audio-visual", "visual",
    "content creation", "creative services",
    "communication campaign", "campaign",
    "social media content", "media content",
    "graphic design", "design services",
]

CPV_PREFIXES = [
    "7996",    # photographic services
    "921",     # motion picture and video services
    "7934",    # advertising and marketing
    "79416",   # PR services
    "798",     # printing/design-related
]

PAGE_SIZE = 50
MAX_PAGES = 40
DAYS_BACK = 180  # longer test window

STATE_FILE = Path("sent_ids.json")

FORCE_EMAIL_ALL = os.environ.get("FORCE_EMAIL_ALL", "0") == "1"
IGNORE_SEEN = os.environ.get("IGNORE_SEEN", "0") == "1"


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


def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return set()
        data = json.loads(raw)
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception:
        return set()
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


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


def kw_match(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KEYWORDS)


def extract_cpv(item: dict) -> list[str]:
    candidates = []
    for k in ("cpv", "cpvCode", "cpvCodes", "mainCpv", "cpvMain"):
        if k in item:
            candidates.append(item.get(k))
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("cpv", "cpvCode", "cpvCodes", "mainCpv", "cpvMain"):
            if k in md:
                candidates.append(md.get(k))

    out = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, list):
            out.extend([norm(v) for v in c])
        else:
            out.append(norm(c))

    cleaned = []
    for s in out:
        digits = re.sub(r"\D", "", s)
        if digits:
            cleaned.append(digits)
    return cleaned


def cpv_match(item: dict) -> bool:
    cpvs = extract_cpv(item)
    if not cpvs:
        return False
    return any(any(code.startswith(pref) for pref in CPV_PREFIXES) for code in cpvs)


def best_title(item: dict) -> str:
    for k in ("title", "summary"):
        v = norm(item.get(k)).strip()
        if v:
            return v
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("title", "callTitle", "identifier", "reference"):
            v = norm(md.get(k)).strip()
            if v:
                return v
    return "(no title)"


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


def best_blob(item: dict) -> str:
    parts = [
        best_title(item),
        norm(item.get("summary")),
        norm(item.get("content")),
        norm(item.get("metadata")),
    ]
    return "\n".join(p for p in parts if p)


def fetch_page(sess: requests.Session, page_number: int) -> dict:
    params = {
        "apiKey": API_KEY,
        "text": "***",
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }

    must = []
    if SEARCH_TYPES:
        must.append({"terms": {"type": SEARCH_TYPES}})
    if SEARCH_STATUSES:
        must.append({"terms": {"status": SEARCH_STATUSES}})

    # Accept either startDate or publicationDate
    date_should = [
        {"range": {"startDate": {"gte": f"now-{DAYS_BACK}d/d", "lte": "now"}}},
        {"range": {"publicationDate": {"gte": f"now-{DAYS_BACK}d/d", "lte": "now"}}},
    ]

    body = {
        "query": {
            "bool": {
                "must": must,
                "should": date_should,
                "minimum_should_match": 1,
            }
        },
        "sort": [{"field": "startDate", "order": "DESC"}],
    }

    r = sess.post(API_URL, params=params, json=body, timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"API HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def send_email(items: list[dict]) -> None:
    to_addr = os.environ.get("EMAIL_TO", "").strip()
    if not to_addr:
        print("EMAIL_TO not set -> skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU tenders: {len(items)}"
    msg["From"] = os.environ.get("EMAIL_FROM", "").strip()
    msg["To"] = to_addr

    lines = []
    for it in items:
        lines.append(f"{it['title']}\nCPV: {', '.join(it['cpv']) if it['cpv'] else 'None'}\n{it['url']}\n")

    msg.set_content("\n".join(lines))

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=45) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)

    print(f"Email sent to {to_addr} ({len(items)} items).")


def main() -> None:
    seen = set() if IGNORE_SEEN else load_seen()
    sess = make_session()

    all_matches = []
    new_matches = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(sess, page)
        items = data.get("results") or data.get("hits") or []
        print(f"PAGE {page}: items={len(items)}")
        if not items:
            break

        for item in items:
            url = best_url(item)
            if PORTAL_URL_MUST_CONTAIN not in url:
                continue  # hard block topicDetails/*.json etc.

            item_id = norm(item.get("id") or item.get("reference") or url).strip()
            if not item_id:
                continue

            blob = best_blob(item)
            is_kw = kw_match(blob)
            is_cpv = cpv_match(item)

            if not (is_kw or is_cpv):
                continue

            row = {
                "id": item_id,
                "title": best_title(item),
                "url": url,
                "cpv": extract_cpv(item),
                "kw": is_kw,
                "cpv_hit": is_cpv,
            }
            all_matches.append(row)

            if IGNORE_SEEN or (item_id not in seen):
                new_matches.append(row)

        time.sleep(0.25)

    if not all_matches:
        print("TOTAL MATCHES: 0 (portal tenders only; none matched keywords/CPV in window)")
    else:
        print(f"TOTAL MATCHES (last {DAYS_BACK} days): {len(all_matches)}")
        for m in all_matches[:200]:
            print(f"- {m['title']}")
            print(f"  CPV: {', '.join(m['cpv']) if m['cpv'] else 'None'} | kw={m['kw']} cpv_hit={m['cpv_hit']}")
            print(f"  {m['url']}")
        if len(all_matches) > 200:
            print(f"... printed first 200 of {len(all_matches)} matches")

    if FORCE_EMAIL_ALL:
        if all_matches:
            send_email(all_matches)
        else:
            print("No matches -> no email.")
        return

    if not new_matches:
        print("No new matches (already in sent_ids.json).")
        return

    send_email(new_matches)
    for m in new_matches:
        seen.add(m["id"])
    save_seen(seen)


if __name__ == "__main__":
    main()
