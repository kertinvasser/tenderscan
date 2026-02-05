import json
import os
import re
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
import smtplib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY = "SEDIA"

# ---- MODE ----
# Procurement in the EU Funding & Tenders Search API is typically type "1".
# (You were using type "2" earlier, which returns funding/opportunity "topicDetails/*.json" items.)
SEARCH_TYPES = ["1"]  # PROCUREMENT
SEARCH_STATUSES = []  # leave empty to not over-filter; procurement status codes vary

# ---- MATCHING ----
# Keywords used for client-side matching against title+summary+content.
KEYWORDS = [
    # photo / video production
    "photo", "photography", "photographer", "photoshoot", "shooting",
    "video", "videography", "videographer", "filming", "film production",
    "video production", "post-production", "post production", "editing",
    "motion graphics", "animation", "subtitling", "voice-over", "voice over",
    # comms / campaign creative
    "audiovisual", "audio-visual", "audio visual", "visual content",
    "content creation", "creative services", "creative agency", "creative support",
    "visual identity", "branding", "brand assets", "graphic design", "design services",
    "communication campaign", "campaign", "awareness campaign",
    "social media content", "social media assets", "media content",
    "promotional video", "explainer video",
    # procurement-ish wording
    "framework contract", "service contract", "communication services", "media services",
]

# CPV codes: we match by prefix, so "7996" matches 79961000 etc.
CPV_PREFIXES = [
    "7996",   # photographic services (79961000)
    "921",    # motion picture and video services (various)
    "923",    # entertainment services (sometimes used broadly)
    "798",    # printing and related services (often bundled with design assets)
]

# ---- SCAN WINDOW / PAGES ----
PAGE_SIZE = 50
MAX_PAGES = 40
DAYS_BACK = 90  # set longer for testing; change to 14 later if you want

# ---- STATE ----
STATE_FILE = Path("sent_ids.json")

# If 1, email everything that matches (not just new vs sent_ids.json)
FORCE_EMAIL_ALL = os.environ.get("FORCE_EMAIL_ALL", "0") == "1"
# If 1, ignore sent_ids.json for "newness" check (useful for testing prints)
IGNORE_SEEN = os.environ.get("IGNORE_SEEN", "0") == "1"


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("POST",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def load_seen() -> set[str]:
    if STATE_FILE.exists():
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


def normalize_text(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, (int, float, bool)):
        return str(x)
    if isinstance(x, list):
        return " ".join(normalize_text(v) for v in x)
    if isinstance(x, dict):
        # try common payload shapes, otherwise stringify a bit
        for k in ("text", "value", "label", "title", "summary"):
            if k in x and isinstance(x[k], str):
                return x[k]
        return " ".join(normalize_text(v) for v in x.values())
    return str(x)


def kw_match(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KEYWORDS)


def extract_cpv(item: dict) -> list[str]:
    # Try a bunch of likely places
    candidates = []

    for k in ("cpv", "cpvCode", "cpvCodes", "mainCpv", "cpvMain"):
        if k in item:
            candidates.append(item.get(k))

    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("cpv", "cpvCode", "cpvCodes", "mainCpv", "cpvMain"):
            if k in md:
                candidates.append(md.get(k))

    # Flatten to list[str]
    out = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, list):
            for v in c:
                out.append(normalize_text(v))
        else:
            out.append(normalize_text(c))
    # keep only digits
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
    # Your logs showed title sometimes empty; also present as metadata / summary / reference.
    for path in (
        ("title",),
        ("summary",),
        ("reference",),
        ("metadata", "callTitle"),
        ("metadata", "title"),
        ("metadata", "identifier"),
    ):
        cur = item
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok:
            s = normalize_text(cur).strip()
            if s:
                return s
    return "(no title)"


def best_url(item: dict) -> str:
    for k in ("url", "link"):
        if k in item and item.get(k):
            return normalize_text(item.get(k)).strip()
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for k in ("url", "link"):
            if k in md and md.get(k):
                return normalize_text(md.get(k)).strip()
    return ""


def best_blob(item: dict) -> str:
    parts = []
    parts.append(best_title(item))
    parts.append(normalize_text(item.get("summary")))
    parts.append(normalize_text(item.get("content")))
    parts.append(normalize_text(item.get("metadata")))
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

    # Some records use startDate, others publicationDate.
    # Use should+minimum_should_match so we accept either.
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
    # If the API sometimes returns non-200 with JSON body, surface it.
    if r.status_code >= 400:
        raise RuntimeError(f"API HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def send_email(items: list[dict]) -> None:
    to_addr = os.environ.get("EMAIL_TO", "").strip()
    from_addr = os.environ.get("EMAIL_FROM", "").strip()
    if not to_addr:
        print("EMAIL_TO not set -> skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"EU procurement matches: {len(items)} ({datetime.utcnow().strftime('%Y-%m-%d')})"
    msg["From"] = from_addr
    msg["To"] = to_addr

    lines = []
    for it in items:
        title = it.get("title", "(no title)")
        url = it.get("url", "")
        cpvs = it.get("cpv", [])
        lines.append(f"{title}\nCPV: {', '.join(cpvs) if cpvs else 'None'}\n{url}\n")

    msg.set_content("\n".join(lines))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]

    with smtplib.SMTP(host, port, timeout=45) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)

    print(f"Email sent to {to_addr} ({len(items)} items).")


def main() -> None:
    seen = set() if IGNORE_SEEN else load_seen()
    sess = _session()

    all_matches: list[dict] = []
    new_matches: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_page(sess, page)
        items = data.get("results") or data.get("hits") or []
        print(f"PAGE {page}: items={len(items)}")

        if not items:
            break

        for item in items:
            item_id = normalize_text(item.get("id") or item.get("reference") or item.get("url")).strip()
            if not item_id:
                continue

            blob = best_blob(item)
            is_kw = kw_match(blob)
            is_cpv = cpv_match(item)

            if not (is_kw or is_cpv):
                continue

            title = best_title(item)
            url = best_url(item)
            cpvs = extract_cpv(item)

            row = {
                "id": item_id,
                "title": title,
                "url": url,
                "cpv": cpvs,
                "kw": is_kw,
                "cpv_hit": is_cpv,
            }
            all_matches.append(row)

            if IGNORE_SEEN or (item_id not in seen):
                new_matches.append(row)

        time.sleep(0.25)

    # Always print what matched (so you can verify in Actions logs)
    if not all_matches:
        print("TOTAL MATCHES: 0 (no keyword/CPV hits in the scanned window)")
    else:
        print(f"TOTAL MATCHES (last {DAYS_BACK} days): {len(all_matches)}")
        for m in all_matches[:200]:
            print(f"- {m['title']}")
            print(f"  CPV: {', '.join(m['cpv']) if m['cpv'] else 'None'} | kw={m['kw']} cpv_hit={m['cpv_hit']}")
            print(f"  {m['url']}")
        if len(all_matches) > 200:
            print(f"... printed first 200 of {len(all_matches)} matches")

    # Email logic
    if FORCE_EMAIL_ALL:
        if all_matches:
            send_email(all_matches)
        else:
            print("No matches -> no email.")
    else:
        if not new_matches:
            print("No new matches (everything already in sent_ids.json).")
        else:
            send_email(new_matches)
            for m in new_matches:
                seen.add(m["id"])
            save_seen(seen)


if __name__ == "__main__":
    main()
