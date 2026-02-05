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

# --- Tuning
PAGE_SIZE = 50
MAX_PAGES = 10            # discovery: keep it small/fast
DAYS_BACK = 365           # discovery: wide window

# --- Match rules (after we identify procurement dataset)
KEYWORDS = [
    "photograph", "photography", "photo shoot", "photographic",
    "video production", "videography", "filming", "film production",
    "audiovisual", "audio-visual",
    "communication campaign", "awareness campaign",
    "social media content", "content creation",
    "graphic design", "visual identity", "creative services",
    "post-production", "editing",
]

CPV_PREFIXES = [
    "7996",   # photographic services
    "921",    # motion picture and video services
    "7934",   # advertising and marketing
    "79416",  # public relations
]

STATE_FILE = Path("sent_ids.json")

# --- Modes (set as GitHub Actions variables / secrets)
DISCOVER = os.environ.get("DISCOVER", "0") == "1"          # print dataset/url stats and exit
IGNORE_SEEN = os.environ.get("IGNORE_SEEN", "0") == "1"    # treat everything as new
FORCE_EMAIL_ALL = os.environ.get("FORCE_EMAIL_ALL", "0") == "1"  # email even if already seen

# --- After discovery, set ONE of these to narrow to procurement dataset
# Example (after you see them in logs): "Calls for tenders", "Procurement", "TED", etc.
ONLY_DATABASE_LABELS = [
    # "Calls for tenders",
    # "Procurement",
]

# Optional: keep only results whose URL contains any of these (leave empty until discovery proves it)
URL_MUST_CONTAIN_ANY = [
    # "/portal/screen/opportunities/calls-for-tenders/",
]


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
        cpv_txt = ", ".join(it["cpv"]) if it["cpv"] else "None"
        lines.append(f"{it['title']}\nCPV: {cpv_txt}\n{it['url']}\n")

    msg.set_content("\n".join(lines))

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=45) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)

    print(f"Email sent to {to_addr} ({len(items)} items).")


def url_allowed(url: str) -> bool:
    if not url:
        return False
    if URL_MUST_CONTAIN_ANY:
        return any(fragment in url for fragment in URL_MUST_CONTAIN_ANY)
    return True


def db_label(item: dict) -> str:
    # observed keys earlier: databaseLabel / database in top-level
    return norm(item.get("databaseLabel") or item.get("database") or "").strip()


def main() -> None:
    seen = set() if IGNORE_SEEN else load_seen()
    sess = make_session()

    # Discovery stats
    db_counts = {}
    url_prefix_counts = {}

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
            title = best_title(item)
            dbl = db_label(item)

            if DISCOVER:
                if dbl:
                    db_counts[dbl] = db_counts.get(dbl, 0) + 1
                if url:
                    # bucket by first ~60 chars
                    pref = url[:60]
                    url_prefix_counts[pref] = url_prefix_counts.get(pref, 0) + 1
                continue

            # After discovery: keep only desired datasets (if set)
            if ONLY_DATABASE_LABELS:
                if dbl not in ONLY_DATABASE_LABELS:
                    continue

            if not url_allowed(url):
                continue

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
                "title": title,
                "url": url,
                "cpv": extract_cpv(item),
                "db": dbl,
                "kw": is_kw,
                "cpv_hit": is_cpv,
            }
            all_matches.append(row)

            if IGNORE_SEEN or (item_id not in seen):
                new_matches.append(row)

        time.sleep(0.25)

    if DISCOVER:
        print("\n=== DISCOVER: databaseLabel counts (top) ===")
        for k, v in sorted(db_counts.items(), key=lambda kv: kv[1], reverse=True)[:30]:
            print(f"{v:>5}  {k}")

        print("\n=== DISCOVER: url prefix buckets (top) ===")
        for k, v in sorted(url_prefix_counts.items(), key=lambda kv: kv[1], reverse=True)[:30]:
            print(f"{v:>5}  {k}")
        print("\nDISCOVER done. Now set ONLY_DATABASE_LABELS and/or URL_MUST_CONTAIN_ANY based on this output.")
        return

    if not all_matches:
        print("TOTAL MATCHES: 0 (after dataset/url filters + keyword/CPV matching)")
    else:
        print(f"TOTAL MATCHES (last {DAYS_BACK} days): {len(all_matches)}")
        for m in all_matches[:100]:
            cpv_txt = ", ".join(m["cpv"]) if m["cpv"] else "None"
            print(f"- {m['title']}")
            print(f"  db={m['db']} | CPV: {cpv_txt} | kw={m['kw']} cpv_hit={m['cpv_hit']}")
            print(f"  {m['url']}")
        if len(all_matches) > 100:
            print(f"... printed first 100 of {len(all_matches)} matches")

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
