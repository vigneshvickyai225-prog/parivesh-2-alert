import os
import re
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from pypdf import PdfReader
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


BASE = "https://parivesh.nic.in"
EC_URL = "https://parivesh.nic.in/#/ec"

STATES = {
    "Tamil Nadu": ["tamil nadu", "tamilnadu", "tn"],
    "Karnataka": ["karnataka", "ka"],
    "Telangana": ["telangana", "tg", "ts"],
}

DB = Path("data/parivesh.db")
HEADERS = {"User-Agent": "Mozilla/5.0 PARIVESH-2-monitor"}


def db():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute(
        """
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
        """
    )
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


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def state_in(text, state):
    t = clean(text).lower()
    return any(
        re.search(r"\b" + re.escape(alias) + r"\b", t)
        for alias in STATES[state]
    )


def classify(text, href):
    s = (text + " " + href).lower()

    if "minutes of meeting" in s or re.search(r"\bmom\b", s) or "minutes" in s:
        return "Minutes / MoM"

    if re.search(r"\bagenda\b", s):
        return "Agenda"

    return None


def authority(text, href):
    s = (text + " " + href).upper()

    for a in ("SEIAA", "SEAC", "EAC"):
        if a in s:
            return a

    return "EC"


def parse_pdf(url):
    try:
        rr = requests.get(
            url,
            headers=HEADERS,
            timeout=45,
        )
        rr.raise_for_status()

        content_type = rr.headers.get("content-type", "").lower()

        if "pdf" not in content_type and not url.lower().split("?")[0].endswith(".pdf"):
            return None

        tmp = Path("/tmp/parivesh.pdf")
        tmp.write_bytes(rr.content)

        reader = PdfReader(str(tmp))
        txt = ""

        for p in reader.pages[:4]:
            try:
                txt += "\n" + (p.extract_text() or "")
            except Exception:
                pass

        txt = clean(txt)

        if len(txt) < 100:
            return None

        return txt[:12000]

    except Exception as e:
        print("PDF_READ_ERROR", url, repr(e))
        return None


def extract_ids(text):
    text = text or ""

    # PARIVESH Agenda IDs, e.g. EC/AGENDA/SEIAA/533775/5/2026
    agenda = re.findall(
        r"\b[A-Z]{2,8}/AGENDA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )

    # PARIVESH MoM IDs, e.g. EC/MOM/SEIAA/533775/5/2026
    mom = re.findall(
        r"\b[A-Z]{2,8}/MOM/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )

    # Environmental Clearance proposal IDs, e.g. SIA/TN/INFRA2/...
    proposal = re.findall(
        r"\bSIA/[A-Z]{2,4}/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",
        text,
        re.I,
    )

    # Some documents use "Proposal No." / "Proposal Number".
    if not proposal:
        m = re.search(
            r"proposal\s*(?:no\.?|number)\s*[:\-]?\s*([A-Za-z0-9/_-]{5,60})",
            text,
            re.I,
        )
        proposal = [m.group(1)] if m else []

    return (
        agenda[0] if agenda else "",
        mom[0] if mom else "",
        proposal[0] if proposal else "",
    )


def meeting_date(text):
    text = text or ""

    patterns = [
        r"(?:Date\s*&\s*Time|Meeting Date|Date of Meeting|held from)\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
        r"held from\s*(\d{1,2}/\d{1,2}/\d{4})",
        r"Date\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1)

    return ""


def title_from_pdf(text, typ):
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    for x in lines[:25]:
        low = x.lower()

        if typ == "Minutes / MoM" and ("minutes" in low or re.search(r"\bmom\b", low)):
            return x[:450]

        if typ == "Agenda" and "agenda" in low:
            return x[:450]

    return lines[0][:450] if lines else "PARIVESH 2.0 document"


def collect_links(page):
    links = page.locator("a").evaluate_all(
        """els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href || ''
        }))"""
    )

    out = []

    for x in links:
        h = x["href"]
        t = clean(x["text"])

        if not h:
            continue

        if h.startswith("/"):
            h = urljoin(BASE, h)

        low = (h + " " + t).lower()

        # Reject normal navigation/menu links unless they clearly
        # point to a document or an Agenda/MoM-related resource.
        is_doc = (
            ".pdf" in low
            or "/utildoc/" in low
            or "/cms/agenda" in low
            or "ref_type=agenda" in low
            or "ref_type=mom" in low
        )

        if not is_doc:
            continue

        typ = classify(t, h)

        if not typ:
            continue

        out.append((t, h, typ))

    # Remove duplicate URLs.
    seen = set()
    ans = []

    for x in out:
        if x[1] not in seen:
            seen.add(x[1])
            ans.append(x)

    return ans


def scrape_state(page, state):
    page.goto(
        EC_URL,
        wait_until="domcontentloaded",
        timeout=120000,
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=30000,
        )
    except PWTimeout:
        pass

    page.wait_for_timeout(5000)

    # Select the requested state where a state dropdown is exposed.
    for i in range(min(page.locator("select").count(), 30)):
        try:
            el = page.locator("select").nth(i)
            opts = (el.inner_text(timeout=1000) or "").lower()

            if (
                "tamil" in opts
                or "karnataka" in opts
                or "telangana" in opts
            ):
                try:
                    el.select_option(label=state)
                    page.wait_for_timeout(2500)
                except Exception:
                    pass

                break

        except Exception:
            pass

    # Scroll so lazy-loaded cards/tables are rendered.
    for _ in range(4):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(700)

    page_text = clean(page.locator("body").inner_text())
    links = collect_links(page)
    results = []

    for label, href, typ in links:
        # For direct PDFs, validate the PDF itself.
        # For CMS pages, validate the page text.
        doc_text = ""

        if (
            href.lower().split("?")[0].endswith(".pdf")
            or "/utildoc/" in href
        ):
            doc_text = parse_pdf(href) or ""

        else:
            p2 = None

            try:
                p2 = page.context.new_page()

                p2.goto(
                    href,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                try:
                    p2.wait_for_load_state(
                        "networkidle",
                        timeout=15000,
                    )
                except PWTimeout:
                    pass

                p2.wait_for_timeout(2000)
                doc_text = clean(p2.locator("body").inner_text())

            except Exception as e:
                print(
                    "PAGE_READ_ERROR",
                    href,
                    repr(e),
                )

            finally:
                if p2 is not None:
                    try:
                        p2.close()
                    except Exception:
                        pass

        combined = clean(label + " " + doc_text)

        # Keep the item only if the state is identifiable.
        if not state_in(combined, state):
            # If state selection is known and the link itself is under
            # a state-filtered page, retain only when the page body
            # explicitly contains the state.
            if not state_in(page_text, state):
                continue

        # Require actual document semantics, not a generic "MOM" menu label.
        if not re.search(
            r"\bminutes?\b|\bmom\b|\bagenda\b",
            combined,
            re.I,
        ):
            continue

        aid, mid, prop = extract_ids(doc_text or combined)

        # Do not alert on generic portal links.
        if not (aid or mid):
            continue

        actual_typ = (
            "Minutes / MoM"
            if mid or re.search(
                r"\bmom\b|minutes",
                doc_text,
                re.I,
            )
            else "Agenda"
        )

        results.append(
            {
                "state": state,
                "authority": authority(combined, href),
                "kind": actual_typ,
                "title": title_from_pdf(
                    doc_text or combined,
                    actual_typ,
                ),
                "date": meeting_date(
                    doc_text or combined
                ),
                "agenda_id": aid,
                "mom_id": mid,
                "proposal": prop,
                "href": href,
            }
        )

    return results


def notify(item):
    lines = [
        "🔔 PARIVESH 2.0 – NEW DOCUMENT",
        "",
        f"State: {item['state']}",
        f"Authority: {item['authority']}",
        f"Type: {item['kind']}",
        f"Title: {item['title']}",
    ]

    if item["date"]:
        lines.append(
            f"Meeting Date: {item['date']}"
        )

    if item["agenda_id"]:
        lines.append(
            f"Agenda ID: {item['agenda_id']}"
        )

    if item["mom_id"]:
        lines.append(
            f"MoM ID: {item['mom_id']}"
        )

    if item["proposal"]:
        lines.append(
            f"Proposal: {item['proposal']}"
        )

    lines += [
        "",
        f"📄 Open: {item['href']}",
    ]

    telegram("\n".join(lines))


def main():
    c = db()
    all_items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1000,
            }
        )

        for state in STATES:
            try:
                print("CHECKING", state)

                items = scrape_state(
                    page,
                    state,
                )

                print(
                    "VALID_DOCUMENTS",
                    state,
                    len(items),
                )

                all_items += items

            except Exception as e:
                print(
                    "STATE_ERROR",
                    state,
                    repr(e),
                )

        browser.close()

    for x in all_items:
        key = (
            x["mom_id"]
            or x["agenda_id"]
            or hashlib.sha256(
                x["href"].encode()
            ).hexdigest()
        )

        if c.execute(
            "SELECT 1 FROM seen WHERE k=?",
            (key,),
        ).fetchone():
            continue

        c.execute(
            """
            INSERT INTO seen
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                x["state"],
                x["authority"],
                x["kind"],
                x["title"],
                x["date"],
                x["agenda_id"],
                x["mom_id"],
                x["proposal"],
                x["href"],
                datetime.now(
                    timezone.utc
                ).isoformat(),
            ),
        )

        c.commit()

        # Only send genuinely identified Agenda/MoM records.
        notify(x)

    c.close()


if __name__ == "__main__":
    main()
