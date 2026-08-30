import os
import re
import json
import time
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from pypdf import PdfReader
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


EC_URL = "https://parivesh.nic.in/#/ec"
BASE = "https://parivesh.nic.in"
STATES = {
    "Tamil Nadu": ["tamil nadu", "tamilnadu"],
    "Karnataka": ["karnataka"],
    "Telangana": ["telangana"],
}

DB = Path("data/parivesh.db")
DIAG = Path("data/network_endpoints.json")
HEADERS = {"User-Agent": "Mozilla/5.0 PARIVESH-2-monitor-v4"}

ID_RE = re.compile(
    r"\b(?:EC/)?(?:AGENDA|MOM)/SEIAA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
    re.I,
)
PROPOSAL_RE = re.compile(
    r"\bSIA/[A-Z]{2,4}/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
    re.I,
)


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen(
            k TEXT PRIMARY KEY,
            state TEXT,
            authority TEXT,
            kind TEXT,
            title TEXT,
            meeting_date TEXT,
            agenda_id TEXT,
            mom_id TEXT,
            proposal TEXT,
            href TEXT,
            discovered_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS network_endpoints(
            url TEXT PRIMARY KEY,
            method TEXT,
            status INTEGER,
            content_type TEXT,
            seen_at TEXT
        )
    """)
    c.commit()
    return c


def telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat,
            "text": text[:4000],
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    r.raise_for_status()


def state_match(text, state):
    t = clean(text).lower()
    return any(alias in t for alias in STATES[state])


def extract_ids(text):
    text = clean(text)
    ids = ID_RE.findall(text)
    agenda = ""
    mom = ""
    for raw in ids:
        x = raw.upper()
        if "/AGENDA/" in x and not agenda:
            agenda = x
        if "/MOM/" in x and not mom:
            mom = x

    p = PROPOSAL_RE.search(text)
    return agenda, mom, p.group(0) if p else ""


def meeting_date(text):
    patterns = [
        r"(?:Meeting Date|Date of Meeting|Date\s*&\s*Time|held from)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"\bDate\s*[:\-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return ""


def authority(text):
    u = text.upper()
    for x in ("SEIAA", "SEAC", "EAC"):
        if x in u:
            return x
    return ""


def title(text, kind):
    lines = [clean(x) for x in str(text).splitlines() if clean(x)]
    for x in lines[:40]:
        lo = x.lower()
        if kind == "Minutes / MoM" and ("minutes" in lo or re.search(r"\bmom\b", lo)):
            return x[:450]
        if kind == "Agenda" and "agenda" in lo:
            return x[:450]
    return lines[0][:450] if lines else "PARIVESH 2.0 document"


def parse_pdf(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        r.raise_for_status()
        if "pdf" not in r.headers.get("content-type", "").lower() and \
           not url.lower().split("?")[0].endswith(".pdf"):
            return ""
        p = Path("/tmp/parivesh_v4.pdf")
        p.write_bytes(r.content)
        reader = PdfReader(str(p))
        text = ""
        for pg in reader.pages[:8]:
            try:
                text += "\n" + (pg.extract_text() or "")
            except Exception:
                pass
        return clean(text)[:20000]
    except Exception as e:
        print("PDF_ERROR", url, repr(e))
        return ""


def classify_record(text):
    t = clean(text)
    aid, mid, prop = extract_ids(t)
    if mid:
        kind = "Minutes / MoM"
    elif aid:
        kind = "Agenda"
    else:
        return None
    if "SEIAA" not in t.upper():
        return None
    return aid, mid, prop, kind


def walk_json(obj, path="root"):
    """Yield text-rich JSON objects/lists for generic API response discovery."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from walk_json(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            if isinstance(v, (dict, list)):
                yield from walk_json(v, f"{path}[{i}]")


def extract_from_json(payload, source_url):
    results = []
    for path, obj in walk_json(payload):
        try:
            blob = json.dumps(obj, ensure_ascii=False)
        except Exception:
            continue
        if len(blob) > 50000:
            blob = blob[:50000]

        rec = classify_record(blob)
        if not rec:
            continue

        aid, mid, prop, kind = rec

        # Discover direct PDF/document URLs embedded in the API object.
        urls = re.findall(r'https?://[^"\'\s<>]+', blob)
        href = ""
        for u in urls:
            if ".pdf" in u.lower() or "/utildoc/" in u.lower():
                href = u.rstrip("\\,")
                break

        results.append({
            "source": source_url,
            "json_path": path,
            "state_text": blob[:5000],
            "agenda_id": aid,
            "mom_id": mid,
            "proposal": prop,
            "kind": kind,
            "href": href,
            "raw": blob[:20000],
        })
    return results


def save_endpoint(c, url, method, status, content_type):
    try:
        c.execute(
            "INSERT OR REPLACE INTO network_endpoints VALUES(?,?,?,?,?)",
            (url, method, status or 0, content_type or "",
             datetime.now(timezone.utc).isoformat())
        )
        c.commit()
    except Exception:
        pass


def discover_network(page, c):
    discovered = []
    seen = set()

    def response_handler(resp):
        u = resp.url
        if u in seen:
            return
        seen.add(u)

        req = resp.request
        ct = resp.headers.get("content-type", "")
        save_endpoint(c, u, req.method, resp.status, ct)

        # API-like calls are especially useful diagnostics.
        low = u.lower()
        if any(x in low for x in ("/api/", "/rest/", "/service/", "/graphql", ".json")):
            print("API_CANDIDATE", req.method, resp.status, u)

        if "application/json" not in ct.lower() and not any(
            x in low for x in ("/api/", "/rest/", "/service/", "/graphql")
        ):
            return

        try:
            payload = resp.json()
        except Exception:
            return

        found = extract_from_json(payload, u)
        if found:
            print("API_DOCUMENTS", u, len(found))
            discovered.extend(found)

    page.on("response", response_handler)
    return discovered, seen


def click_ec_controls(page):
    """
    Stay strictly inside /#/ec. We do not navigate to legacy CPC pages.
    Click visible controls whose accessible text clearly indicates Agenda/MoM.
    """
    candidates = [
        r"agenda",
        r"minutes",
        r"\bmom\b",
    ]

    for pattern in candidates:
        try:
            loc = page.get_by_text(re.compile(pattern, re.I))
            count = min(loc.count(), 20)
            for i in range(count):
                try:
                    item = loc.nth(i)
                    if not item.is_visible():
                        continue
                    txt = clean(item.inner_text(timeout=1000))
                    # Avoid generic menu labels unless they are clearly actionable.
                    if not txt or len(txt) > 150:
                        continue
                    print("EC_CONTROL", txt)
                    item.click(timeout=3000)
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
        except Exception:
            pass


def collect_rendered_links(page):
    out = []
    try:
        links = page.locator("a").evaluate_all(
            """els => els.map(a => ({
                text:(a.innerText||a.textContent||'').trim(),
                href:a.href||''
            }))"""
        )
    except Exception:
        return out

    for x in links:
        h = x.get("href", "")
        t = clean(x.get("text", ""))
        low = (h + " " + t).lower()
        if not h:
            continue
        if ".pdf" in low or "/utildoc/" in low:
            out.append((t, h))
    return out


def validate_pdf_candidate(href, state):
    text = parse_pdf(href)
    if not text:
        return None

    rec = classify_record(text)
    if not rec:
        return None

    aid, mid, prop, kind = rec
    if not state_match(text, state):
        return None

    return {
        "state": state,
        "authority": authority(text),
        "kind": kind,
        "title": title(text, kind),
        "date": meeting_date(text),
        "agenda_id": aid,
        "mom_id": mid,
        "proposal": prop,
        "href": href,
    }


def scan_state(page, state, api_records):
    results = []

    # API records discovered from the EC application.
    for r in api_records:
        blob = r.get("raw", "")
        if not state_match(blob, state):
            continue
        results.append({
            "state": state,
            "authority": "SEIAA",
            "kind": r["kind"],
            "title": title(blob, r["kind"]),
            "date": meeting_date(blob),
            "agenda_id": r["agenda_id"],
            "mom_id": r["mom_id"],
            "proposal": r["proposal"],
            "href": r.get("href") or EC_URL,
        })

    # Also validate PDF links rendered by the EC application itself.
    for _, href in collect_rendered_links(page):
        item = validate_pdf_candidate(href, state)
        if item:
            results.append(item)

    # Deduplicate.
    unique = {}
    for x in results:
        k = x["mom_id"] or x["agenda_id"] or hashlib.sha256(
            x["href"].encode()
        ).hexdigest()
        unique[k] = x
    return list(unique.values())


def write_diagnostics(c, discovered, seen_urls):
    DIAG.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for url in sorted(seen_urls):
        row = c.execute(
            "SELECT url,method,status,content_type,seen_at FROM network_endpoints WHERE url=?",
            (url,),
        ).fetchone()
        if row:
            rows.append({
                "url": row[0],
                "method": row[1],
                "status": row[2],
                "content_type": row[3],
                "seen_at": row[4],
            })

    DIAG.write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "target": EC_URL,
            "api_document_records": discovered,
            "network_endpoints": rows,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def notify(item):
    lines = [
        "🔔 PARIVESH 2.0 – NEW SEIAA DOCUMENT",
        "",
        f"State: {item['state']}",
        f"Authority: {item['authority'] or 'SEIAA'}",
        f"Type: {item['kind']}",
        f"Title: {item['title']}",
    ]
    if item["date"]:
        lines.append(f"Meeting/Document Date: {item['date']}")
    if item["agenda_id"]:
        lines.append(f"Agenda ID: {item['agenda_id']}")
    if item["mom_id"]:
        lines.append(f"MoM ID: {item['mom_id']}")
    if item["proposal"]:
        lines.append(f"Proposal: {item['proposal']}")
    lines += ["", f"📄 Open: {item['href']}"]
    telegram("\n".join(lines))


def heartbeat(stats):
    msg = [
        "🟢 PARIVESH V4 MONITOR ACTIVE",
        "",
        f"Last check: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')} IST",
    ]
    for state, value in stats.items():
        msg.append(f"{state}: {value} validated document(s)")
    telegram("\n".join(msg))


def main():
    c = db()
    discovered = []
    seen_urls = set()
    stats = {s: 0 for s in STATES}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="en-IN",
        )
        page = context.new_page()

        api_records, seen_urls = discover_network(page, c)

        print("OPEN", EC_URL)
        page.goto(EC_URL, wait_until="domcontentloaded", timeout=120000)

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass

        page.wait_for_timeout(8000)

        # Stay on the requested /#/ec page only.
        if "/#/ec" not in page.url and "#/ec" not in page.url:
            print("WARNING_UNEXPECTED_URL", page.url)

        click_ec_controls(page)

        for _ in range(5):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)

        page_text = clean(page.locator("body").inner_text())
        print("EC_PAGE_TEXT_LENGTH", len(page_text))

        # Give late API calls time to finish.
        page.wait_for_timeout(3000)

        all_items = []
        for state in STATES:
            print("CHECKING", state)
            items = scan_state(page, state, api_records)
            stats[state] = len(items)
            print("VALID_DOCUMENTS", state, len(items))
            all_items.extend(items)

        context.close()
        browser.close()

    # Re-read endpoint records after browser closes.
    try:
        rows = c.execute(
            "SELECT url FROM network_endpoints"
        ).fetchall()
        seen_urls.update(r[0] for r in rows)
    except Exception:
        pass

    write_diagnostics(c, discovered, seen_urls)

    new_count = 0
    for item in all_items:
        key = item["mom_id"] or item["agenda_id"] or hashlib.sha256(
            item["href"].encode()
        ).hexdigest()

        if c.execute(
            "SELECT 1 FROM seen WHERE k=?",
            (key,),
        ).fetchone():
            continue

        c.execute(
            """INSERT INTO seen VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                item["state"],
                item["authority"],
                item["kind"],
                item["title"],
                item["date"],
                item["agenda_id"],
                item["mom_id"],
                item["proposal"],
                item["href"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        c.commit()

        notify(item)
        new_count += 1

    print("NEW_DOCUMENTS", new_count)
    print("NETWORK_DIAGNOSTICS", DIAG)

    # Heartbeat once every 24 hours, stored in the same DB.
    c.execute("""
        CREATE TABLE IF NOT EXISTS meta(
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)
    row = c.execute(
        "SELECT v FROM meta WHERE k='last_heartbeat'"
    ).fetchone()
    now = datetime.now(timezone.utc)
    send_hb = True
    if row:
        try:
            send_hb = (
                now - datetime.fromisoformat(row[0])
            ) >= timedelta(hours=24)
        except Exception:
            pass

    if send_hb:
        heartbeat(stats)
        c.execute(
            "INSERT OR REPLACE INTO meta VALUES(?,?)",
            ("last_heartbeat", now.isoformat()),
        )
        c.commit()

    c.close()


if __name__ == "__main__":
    main()
