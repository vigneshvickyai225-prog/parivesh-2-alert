import os, re, json, hashlib, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

EC_URL = "https://parivesh.nic.in/#/ec"
BASE = "https://parivesh.nic.in"
STATES = {
    "Tamil Nadu": ["tamil nadu", "tamilnadu"],
    "Karnataka": ["karnataka"],
    "Telangana": ["telangana"],
}
DB = Path("data/parivesh.db")
DIAG = Path("data/parivesh_v5_diagnostics.json")
HEADERS = {"User-Agent": "Mozilla/5.0 PARIVESH-2-monitor-v5"}

ID_PATTERNS = [
    re.compile(r"\b(?:EC/)?AGENDA/SEIAA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b", re.I),
    re.compile(r"\b(?:EC/)?MOM/SEIAA/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b", re.I),
]
PROP_RE = re.compile(r"\bSIA/[A-Z]{2,4}/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b", re.I)

def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()

def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS seen(
      k TEXT PRIMARY KEY,state TEXT,authority TEXT,kind TEXT,title TEXT,
      meeting_date TEXT,agenda_id TEXT,mom_id TEXT,proposal TEXT,href TEXT,discovered_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS endpoints(
      url TEXT PRIMARY KEY,method TEXT,status INTEGER,content_type TEXT,seen_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT)""")
    c.commit(); return c

def telegram(text):
    token=os.environ["TELEGRAM_BOT_TOKEN"]; chat=os.environ["TELEGRAM_CHAT_ID"]
    r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id":chat,"text":text[:4000],"disable_web_page_preview":False},timeout=30)
    r.raise_for_status()

def state_in(text,state):
    t=clean(text).lower()
    return any(a in t for a in STATES[state])

def classify(text):
    t=clean(text); u=t.upper()
    aid=mid=""
    for m in ID_PATTERNS[0].findall(t):
        aid=m.upper(); break
    for m in ID_PATTERNS[1].findall(t):
        mid=m.upper(); break
    if not (aid or mid) or "SEIAA" not in u: return None
    p=PROP_RE.search(t)
    kind="Minutes / MoM" if mid else "Agenda"
    return aid,mid,(p.group(0) if p else ""),kind

def meeting_date(text):
    for p in [
        r"(?:Meeting Date|Date of Meeting|Date\s*&\s*Time|held from)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"\bDate\s*[:\-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})"]:
        m=re.search(p,text,re.I)
        if m:return m.group(1)
    return ""

def title(text,kind):
    lines=[clean(x) for x in str(text).splitlines() if clean(x)]
    for x in lines[:60]:
        lo=x.lower()
        if (kind=="Agenda" and "agenda" in lo) or (kind!="Agenda" and ("minutes" in lo or "mom" in lo)):
            return x[:450]
    return lines[0][:450] if lines else "PARIVESH 2.0 document"

def endpoint_from_js(js):
    # Extract strings that look like backend routes/URLs from the current bundle.
    pats=[
        r'https?://[^"\'`\\\s]+',
        r'["\'`]((?:/)?(?:ua|parivesh_api|api|rest|service|services|graphql)/[^"\'`\\\s]+)["\'`]',
        r'["\'`]([^"\'`\\\s]{0,80}(?:Get|List|Search|Agenda|MOM|Minutes|SEIAA|StatePortal)[^"\'`\\\s]{0,160})["\'`]'
    ]
    out=set()
    for p in pats:
        for x in re.findall(p,js,re.I):
            x=x.replace("\\/","/")
            if "http" in x: out.add(x)
            elif x.startswith("/"): out.add(urljoin(BASE,x))
    return sorted(out)

def fetch_js(url):
    try:
        r=requests.get(url,headers=HEADERS,timeout=60)
        if r.ok and len(r.content)>1000:
            return r.text
    except Exception as e: print("JS_FETCH_ERROR",url,repr(e))
    return ""

def api_json(session, url):
    try:
        r=session.get(url,headers=HEADERS,timeout=45)
        ct=r.headers.get("content-type","").lower()
        if r.ok and ("json" in ct or r.text.lstrip().startswith(("{","["))):
            return r.json(),r.status_code,ct
    except Exception: pass
    return None,0,""

def walk(obj):
    yield obj
    if isinstance(obj,dict):
        for v in obj.values():
            if isinstance(v,(dict,list)): yield from walk(v)
    elif isinstance(obj,list):
        for v in obj[:500]:
            if isinstance(v,(dict,list)): yield from walk(v)

def records_from_payload(payload,source):
    out=[]
    for obj in walk(payload):
        try: blob=json.dumps(obj,ensure_ascii=False)
        except Exception: continue
        if len(blob)>60000: blob=blob[:60000]
        rec=classify(blob)
        if not rec: continue
        aid,mid,prop,kind=rec
        urls=re.findall(r'https?://[^"\'\s<>]+',blob)
        href=next((u.rstrip("\\,") for u in urls if ".pdf" in u.lower() or "/utildoc/" in u.lower()),"")
        out.append({"source":source,"state_blob":blob[:12000],"agenda_id":aid,"mom_id":mid,
                    "proposal":prop,"kind":kind,"href":href})
    return out

def main():
    c=db(); session=requests.Session()
    discovered=[]; endpoints={}

    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={"width":1440,"height":1100},locale="en-IN")
        page=context.new_page()

        def on_response(resp):
            u=resp.url; endpoints[u]={
                "method":resp.request.method,"status":resp.status,
                "content_type":resp.headers.get("content-type",""),
                "time":datetime.now(timezone.utc).isoformat()}
            try:
                c.execute("INSERT OR REPLACE INTO endpoints VALUES(?,?,?,?,?)",
                    (u,resp.request.method,resp.status,resp.headers.get("content-type",""),
                     datetime.now(timezone.utc).isoformat())); c.commit()
            except: pass
            if "json" in resp.headers.get("content-type","").lower():
                try:
                    recs=records_from_payload(resp.json(),u)
                    if recs:
                        print("LIVE_API_DOCUMENTS",u,len(recs)); discovered.extend(recs)
                except: pass

        page.on("response",on_response)
        page.goto(EC_URL,wait_until="domcontentloaded",timeout=120000)
        try: page.wait_for_load_state("networkidle",timeout=30000)
        except PWTimeout: pass
        page.wait_for_timeout(8000)

        # Read the exact current JS bundle URLs loaded by #/ec.
        scripts=page.locator("script[src]").evaluate_all("els=>els.map(x=>x.src)")
        js_candidates=[]
        for s in scripts:
            if s.endswith(".js") or ".js?" in s: js_candidates.append(s)
        print("JS_BUNDLES",len(js_candidates))

        # Extract API candidates from the live bundle(s).
        api_candidates=set()
        for s in js_candidates:
            js=fetch_js(s)
            if js:
                print("BUNDLE",s,len(js))
                api_candidates.update(endpoint_from_js(js))

        # The diagnostics showed this current working endpoint; retain it as a seed.
        api_candidates.add(f"{BASE}/ua/parivesh/StatePortal/GetStatePortalData/ALL/1018")

        # Exercise the EC page controls without leaving #/ec.
        for rx in [r"agenda",r"minutes",r"\bmom\b"]:
            try:
                loc=page.get_by_text(re.compile(rx,re.I))
                for i in range(min(loc.count(),15)):
                    try:
                        el=loc.nth(i)
                        if el.is_visible() and len(clean(el.inner_text(timeout=800)))<150:
                            el.click(timeout=2500); page.wait_for_timeout(1800)
                    except: pass
            except: pass

        for _ in range(5):
            page.mouse.wheel(0,2500); page.wait_for_timeout(600)

        # Probe only candidates that look like API/backend endpoints.
        for u in sorted(api_candidates):
            if not u.startswith(BASE): continue
            if any(x in u.lower() for x in (".js",".css","favicon","/static/")): continue
            payload,status,ct=api_json(session,u)
            if payload is not None:
                print("API_PROBE",status,u)
                recs=records_from_payload(payload,u)
                if recs: discovered.extend(recs)

        # After interactions, process any late JSON response records.
        page.wait_for_timeout(2500)
        context.close(); browser.close()

    # Deduplicate records and state-filter.
    items=[]
    for r in discovered:
        for state in STATES:
            if not state_in(r["state_blob"],state): continue
            items.append({
                "state":state,"authority":"SEIAA","kind":r["kind"],
                "title":title(r["state_blob"],r["kind"]),
                "date":meeting_date(r["state_blob"]),
                "agenda_id":r["agenda_id"],"mom_id":r["mom_id"],
                "proposal":r["proposal"],"href":r["href"] or EC_URL,
            })

    uniq={}
    for x in items:
        k=x["mom_id"] or x["agenda_id"] or hashlib.sha256(
            (x["href"]+x["title"]).encode()).hexdigest()
        uniq[(x["state"],k)]=x

    new=0
    for x in uniq.values():
        key=x["mom_id"] or x["agenda_id"] or hashlib.sha256(x["href"].encode()).hexdigest()
        if c.execute("SELECT 1 FROM seen WHERE k=?",(key,)).fetchone(): continue
        c.execute("INSERT INTO seen VALUES(?,?,?,?,?,?,?,?,?,?,?)",(
            key,x["state"],x["authority"],x["kind"],x["title"],x["date"],
            x["agenda_id"],x["mom_id"],x["proposal"],x["href"],
            datetime.now(timezone.utc).isoformat()))
        c.commit()
        telegram("🔔 PARIVESH 2.0 – NEW SEIAA DOCUMENT\n\n"+
                 f"State: {x['state']}\nAuthority: SEIAA\nType: {x['kind']}\nTitle: {x['title']}"+
                 (f"\nDate: {x['date']}" if x["date"] else "")+
                 (f"\nAgenda ID: {x['agenda_id']}" if x["agenda_id"] else "")+
                 (f"\nMoM ID: {x['mom_id']}" if x["mom_id"] else "")+
                 (f"\nProposal: {x['proposal']}" if x["proposal"] else "")+
                 f"\n\n📄 Open: {x['href']}")
        new+=1

    DIAG.parent.mkdir(exist_ok=True)
    DIAG.write_text(json.dumps({
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "target":EC_URL,
        "js_bundles":js_candidates,
        "api_candidates":sorted(api_candidates),
        "network_endpoints":endpoints,
        "validated_records":list(uniq.values()),
        "new_records":new
    },ensure_ascii=False,indent=2),encoding="utf-8")
    print("NEW_DOCUMENTS",new)
    c.close()

if __name__=="__main__": main()
