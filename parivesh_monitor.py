import os
import re
import sqlite3
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from pypdf import PdfReader
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -----------------------------------------------------------------------------
# PARIVESH 2.0 SEIAA publication monitor
# Monitors actual SEIAA Agenda / MoM documents for:
#   Tamil Nadu, Karnataka, Telangana
# Telegram credentials are supplied through GitHub Actions secrets.
# -----------------------------------------------------------------------------

BASE = "https://parivesh.nic.in"
EC_URL = "https://parivesh.nic.in/#/ec"
LEGACY_URL = "https://cpc.parivesh.nic.in/Login.aspx?_Login=Moefcc"

STATES = {
    "Tamil Nadu": ["tamil nadu", "tamilnadu"],
    "Karnataka": ["karnataka"],
    "Telangana": ["telangana", "telengana"],
}

# These are the official publication menu labels exposed by the PARIVESH portal.
PUBLICATION_LABELS = {
    "Agenda": "SEIAA Agenda for PARIVESH 2.0",
    "Minutes / MoM": "SEIAA MoM for PARIVESH 2.0",
}

DB = Path("data/parivesh.db")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PARIVESH-2-SEIAA-monitor/2.0"
}


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS seen(
            k TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            authority TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT,
            meeting_date TEXT,
            agenda_id TEXT,
            mom_id TEXT,
            proposal TEXT,
            href TEXT,
            discovered_at TEXT
        )
        """
    )
    c.commit()
    return c


def telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat,
            "text": text[:4000],
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def state_in(text, state):
    text = clean(text).lower()
    return any(alias in text for alias in STATES[state])


def parse_pdf(url, session=None):
    """Download and extract enough of a PDF to identify an actual Agenda/MoM."""
    try:
        s = session or requests.Session()
        response = s.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        final_url = response.url
        is_pdf = (
            "pdf" in content_type
            or final_url.lower().split("?")[0].endswith(".pdf")
            or url.lower().split("?")[0].endswith(".pdf")
            or "/utildoc/" in final_url.lower()
        )
        if not is_pdf:
            return ""

        tmp = Path("/tmp/parivesh_seiaa.pdf")
        tmp.write_bytes(response.content)

        reader = PdfReader(str(tmp))
        parts = []
        # First 8 pages are normally enough to capture the official header,
        # IDs, meeting date and first proposal.
        for page in reader.pages[:8]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass

        text = clean("\n".join(parts))
        return text[:30000] if len(text) >= 80 else ""
    except Exception as exc:
        print("PDF_READ_ERROR", url, repr(exc))
        return ""


def extract_ids(text):
    text = text or ""

    # Actual PARIVESH 2.0 SEIAA identifiers.
    agenda = re.findall(
        r"\bEC/AGENDA/SEIAA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )
    mom = re.findall(
        r"\bEC/MOM/SEIAA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )

    # Proposal numbers commonly appearing inside SEIAA documents.
    proposal = re.findall(
        r"\bSIA/[A-Z]{2,4}/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )

    if not proposal:
        m = re.search(
            r"proposal\s*(?:no\.?|number)\s*[:\-]?\s*([A-Za-z0-9/_-]{5,80})",
            text,
            re.I,
        )
        if m:
            proposal = [m.group(1)]

    return (
        agenda[0] if agenda else "",
        mom[0] if mom else "",
        proposal[0] if proposal else "",
    )


def meeting_date(text):
    text = text or ""
    patterns = [
        r"(?:Date\s*&\s*Time|Meeting Date|Date of Meeting|Meeting held|held from|held on)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?:Date)\s*[:\-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


def extract_title(text, kind):
    text = text or ""
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    for line in lines[:40]:
        low = line.lower()
        if kind == "Minutes / MoM" and ("minutes of" in low or re.search(r"\bmom\b", low)):
            return line[:500]
        if kind == "Agenda" and "agenda" in low:
            return line[:500]

    return lines[0][:500] if lines else "PARIVESH 2.0 SEIAA document"


def identify_document(text, requested_kind, state):
    """Return metadata only when the document itself proves it is a SEIAA Agenda/MoM."""
    if not text:
        return None

    aid, mid, proposal = extract_ids(text)
    if not aid and not mid:
        return None

    # Never infer the type from a navigation label. The actual document ID wins.
    if requested_kind == "Minutes / MoM" and not mid:
        return None
    if requested_kind == "Agenda" and not aid:
        return None

    # Require SEIAA and the requested state in the actual document.
    if not re.search(r"\bSEIAA\b", text, re.I):
        return None
    if not state_in(text, state):
        return None

    actual_kind = "Minutes / MoM" if mid else "Agenda"
    title = extract_title(text, actual_kind)

    # Avoid accidentally accepting a SEIAA document from another state.
    if not state_in(text, state):
        return None

    return {
        "state": state,
        "authority": "SEIAA",
        "kind": actual_kind,
        "title": title,
        "date": meeting_date(text),
        "agenda_id": aid,
        "mom_id": mid,
        "proposal": proposal,
    }


def absolute_href(href):
    if href.startswith("/"):
        return urljoin(BASE, href)
    return href


def anchors(page):
    try:
        return page.locator("a").evaluate_all(
            """
            els => els.map(a => ({
                text: (a.innerText || a.textContent || '').trim(),
                href: a.href || '',
                title: a.title || ''
            }))
            """
        )
    except Exception:
        return []


def discover_publication_pages(page):
    """Find the official PARIVESH publication pages by their menu labels.

    This avoids hard-coding fragile legacy .aspx URLs. PARIVESH's public portal
    exposes separate SEIAA Agenda and SEIAA MoM for PARIVESH 2.0 entries.
    """
    found = {}
    page.goto(LEGACY_URL, wait_until="domcontentloaded", timeout=120000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)

    for kind, wanted in PUBLICATION_LABELS.items():
        candidates = []
        for a in anchors(page):
            label = clean(f"{a.get('text','')} {a.get('title','')}")
            if wanted.lower() in label.lower():
                candidates.append(a)

        if not candidates:
            print("PUBLICATION_LINK_NOT_FOUND", kind, wanted)
            continue

        a = candidates[0]
        href = a.get("href", "")
        if href and not href.lower().startswith("javascript:") and href != "#":
            found[kind] = urljoin(LEGACY_URL, href)
            print("PUBLICATION_PAGE", kind, found[kind])
            continue

        # Some ASP.NET menu items use postback/javascript links. Click it and
        # capture the resulting URL/content, then return to the home page.
        try:
            locator = page.get_by_text(wanted, exact=False).first
            if locator.count() and locator.is_visible():
                before = page.url
                locator.click(timeout=10000)
                page.wait_for_timeout(2000)
                if page.url != before:
                    found[kind] = page.url
                    print("PUBLICATION_PAGE_CLICK", kind, page.url)
                page.goto(LEGACY_URL, wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(1500)
        except Exception as exc:
            print("PUBLICATION_CLICK_ERROR", kind, repr(exc))

    return found


def candidate_links(page):
    """Collect links that could actually lead to a publication document."""
    out = []
    seen = set()
    for a in anchors(page):
        text = clean(a.get("text", ""))
        href = absolute_href(a.get("href", ""))
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            continue

        low = f"{text} {href}".lower()
        looks_like_document = (
            ".pdf" in low
            or "/utildoc/" in low
            or "download" in low
            or "viewdocument" in low
            or "agenda" in low
            or "mom" in low
            or "minutes" in low
        )
        if not looks_like_document:
            continue

        if href in seen:
            continue
        seen.add(href)
        out.append((text, href))
    return out


def click_next_publication_page(page):
    """Advance a normal ASP.NET/DataTables-style listing when a Next control exists."""
    selectors = [
        "a:has-text('Next')",
        "a:has-text('Next >')",
        "button:has-text('Next')",
        "a:has-text('›')",
        "a:has-text('»')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            for i in range(count - 1, -1, -1):
                item = loc.nth(i)
                if not item.is_visible():
                    continue
                cls = (item.get_attribute("class") or "").lower()
                aria = (item.get_attribute("aria-disabled") or "").lower()
                if "disabled" in cls or aria == "true":
                    continue
                before = clean(page.locator("body").inner_text())[:3000]
                before_url = page.url
                item.click(timeout=10000)
                page.wait_for_timeout(1200)
                after = clean(page.locator("body").inner_text())[:3000]
                if after != before or page.url != before_url:
                    return True
        except Exception:
            pass
    return False


def scan_publication_page(page, publication_url, requested_kind, state, session):
    """Scan the official SEIAA 2.0 publication listing and validate PDFs."""
    results = []
    visited_page_signatures = set()

    page.goto(publication_url, wait_until="domcontentloaded", timeout=120000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1800)

    for page_no in range(1, 21):
        body = clean(page.locator("body").inner_text())
        signature = hashlib.sha256(body[:10000].encode("utf-8", "ignore")).hexdigest()
        if signature in visited_page_signatures:
            break
        visited_page_signatures.add(signature)

        print("SCAN", requested_kind, state, "page", page_no, page.url)

        links = candidate_links(page)
        for label, href in links:
            # If the anchor itself is not a PDF, open it and inspect the page.
            doc_text = ""
            final_href = href
            if href.lower().split("?")[0].endswith(".pdf") or "/utildoc/" in href.lower():
                doc_text = parse_pdf(href, session)
            else:
                child = None
                try:
                    child = page.context.new_page()
                    child.goto(href, wait_until="domcontentloaded", timeout=60000)
                    try:
                        child.wait_for_load_state("networkidle", timeout=12000)
                    except PWTimeout:
                        pass
                    child.wait_for_timeout(1200)
                    final_href = child.url
                    # Direct PDF navigation may not expose HTML body text.
                    if final_href.lower().split("?")[0].endswith(".pdf") or "/utildoc/" in final_href.lower():
                        doc_text = parse_pdf(final_href, session)
                    else:
                        page_body = clean(child.locator("body").inner_text())
                        aid, mid, _ = extract_ids(page_body)
                        if aid or mid:
                            doc_text = page_body[:30000]
                except Exception as exc:
                    print("DOCUMENT_PAGE_ERROR", href, repr(exc))
                finally:
                    if child is not None:
                        try:
                            child.close()
                        except Exception:
                            pass

            metadata = identify_document(doc_text, requested_kind, state)
            if metadata:
                metadata["href"] = final_href
                results.append(metadata)
                print("VALID_DOCUMENT", state, requested_kind, metadata.get("mom_id") or metadata.get("agenda_id"), final_href)

        if not click_next_publication_page(page):
            break

    # Deduplicate within the current run.
    unique = {}
    for item in results:
        key = item["mom_id"] or item["agenda_id"] or item["href"]
        unique[key] = item
    return list(unique.values())


def scan_new_ec_page(page, state, session):
    """Fallback: inspect the PARIVESH 2.0 EC SPA itself, but never trust menu labels.

    A record is accepted only when its actual document/page contains an official
    EC/AGENDA/SEIAA or EC/MOM/SEIAA identifier and the requested state.
    """
    results = []
    page.goto(EC_URL, wait_until="domcontentloaded", timeout=120000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(4000)

    # Give Angular lazy content a chance to render.
    for _ in range(8):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(500)

    # Try visible state selectors, but do not rely on them for document identity.
    for i in range(min(page.locator("select").count(), 30)):
        try:
            el = page.locator("select").nth(i)
            opts = (el.inner_text(timeout=1000) or "").lower()
            if state.lower() in opts:
                try:
                    el.select_option(label=state)
                    page.wait_for_timeout(1800)
                except Exception:
                    pass
                break
        except Exception:
            pass

    for label, href in candidate_links(page):
        if not ("agenda" in (label + href).lower() or "mom" in (label + href).lower() or "minutes" in (label + href).lower()):
            continue

        doc_text = ""
        final_href = href
        if href.lower().split("?")[0].endswith(".pdf") or "/utildoc/" in href.lower():
            doc_text = parse_pdf(href, session)
        else:
            child = None
            try:
                child = page.context.new_page()
                child.goto(href, wait_until="domcontentloaded", timeout=60000)
                try:
                    child.wait_for_load_state("networkidle", timeout=12000)
                except PWTimeout:
                    pass
                child.wait_for_timeout(1200)
                final_href = child.url
                if final_href.lower().split("?")[0].endswith(".pdf") or "/utildoc/" in final_href.lower():
                    doc_text = parse_pdf(final_href, session)
                else:
                    doc_text = clean(child.locator("body").inner_text())[:30000]
            except Exception as exc:
                print("SPA_DOCUMENT_ERROR", href, repr(exc))
            finally:
                if child is not None:
                    try:
                        child.close()
                    except Exception:
                        pass

        for requested_kind in ("Agenda", "Minutes / MoM"):
            metadata = identify_document(doc_text, requested_kind, state)
            if metadata:
                metadata["href"] = final_href
                results.append(metadata)

    return results


def notify(item):
    lines = [
        "🔔 PARIVESH 2.0 – NEW SEIAA DOCUMENT",
        "",
        f"State: {item['state']}",
        "Authority: SEIAA",
        f"Type: {item['kind']}",
        f"Title: {item['title']}",
    ]
    if item["date"]:
        lines.append(f"Meeting Date: {item['date']}")
    if item["agenda_id"]:
        lines.append(f"Agenda ID: {item['agenda_id']}")
    if item["mom_id"]:
        lines.append(f"MoM ID: {item['mom_id']}")
    if item["proposal"]:
        lines.append(f"Proposal: {item['proposal']}")
    lines.extend(["", f"📄 Open: {item['href']}"])
    telegram("\n".join(lines))


def send_heartbeat(stats):
    """Daily operational proof that the monitor is actually running."""
    today = datetime.now().astimezone().strftime("%d-%b-%Y %H:%M IST")
    lines = [
        "🟢 PARIVESH MONITOR ACTIVE",
        "",
        f"Last check: {today}",
        f"Tamil Nadu: {stats.get('Tamil Nadu', 0)} valid documents found",
        f"Karnataka: {stats.get('Karnataka', 0)} valid documents found",
        f"Telangana: {stats.get('Telangana', 0)} valid documents found",
        "",
        "Source: PARIVESH 2.0 SEIAA Agenda / MoM publications",
    ]
    telegram("\n".join(lines))


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    conn = db()
    all_items = []
    stats = {state: 0 for state in STATES}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()

        # Primary source: official public menu pages specifically labelled
        # SEIAA Agenda/MoM for PARIVESH 2.0.
        publication_pages = {}
        try:
            publication_pages = discover_publication_pages(page)
        except Exception as exc:
            print("PUBLICATION_DISCOVERY_ERROR", repr(exc))

        for state in STATES:
            for kind, publication_url in publication_pages.items():
                try:
                    items = scan_publication_page(
                        page,
                        publication_url,
                        kind,
                        state,
                        session,
                    )
                    stats[state] += len(items)
                    all_items.extend(items)
                except Exception as exc:
                    print("PUBLICATION_SCAN_ERROR", state, kind, repr(exc))

            # Secondary source: new PARIVESH 2.0 EC application, with strict
            # document-ID validation. It cannot create alerts from generic menu pages.
            try:
                items = scan_new_ec_page(page, state, session)
                stats[state] += len(items)
                all_items.extend(items)
            except Exception as exc:
                print("SPA_SCAN_ERROR", state, repr(exc))

        context.close()
        browser.close()

    # Run-level deduplication.
    unique = {}
    for item in all_items:
        key = item["mom_id"] or item["agenda_id"] or hashlib.sha256(item["href"].encode()).hexdigest()
        unique[key] = item

    new_count = 0
    for key, item in unique.items():
        if conn.execute("SELECT 1 FROM seen WHERE k=?", (key,)).fetchone():
            continue

        conn.execute(
            """
            INSERT INTO seen
            (k,state,authority,kind,title,meeting_date,agenda_id,mom_id,proposal,href,discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
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
        conn.commit()
        notify(item)
        new_count += 1

    print("RUN_COMPLETE", "unique_valid_documents=", len(unique), "new_alerts=", new_count)
    print("STATE_STATS", stats)

    # Heartbeat is optional and off by default so the bot is not spammed daily.
    # Set HEARTBEAT=1 in GitHub Actions variables if you want it.
    if os.environ.get("HEARTBEAT", "0") == "1":
        send_heartbeat(stats)

    conn.close()


if __name__ == "__main__":
    main()
