import os,re,json,sqlite3,hashlib
from pathlib import Path
from datetime import datetime,timezone
import requests
from playwright.sync_api import sync_playwright,TimeoutError as PWTimeout

EC_URL="https://parivesh.nic.in/#/ec"; BASE="https://parivesh.nic.in"
STATES={"Tamil Nadu":["tamil nadu","tamilnadu"],"Karnataka":["karnataka"],"Telangana":["telangana"]}
DB=Path("data/parivesh.db"); DIAG=Path("data/parivesh_v6_diagnostics.json")
UA="Mozilla/5.0 PARIVESH-2-monitor-v6"

def clean(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def db():
    DB.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY,state TEXT,authority TEXT,kind TEXT,title TEXT,meeting_date TEXT,agenda_id TEXT,mom_id TEXT,proposal TEXT,href TEXT,discovered_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS endpoints(url TEXT PRIMARY KEY,method TEXT,status INTEGER,content_type TEXT,request_body TEXT,seen_at TEXT)""")
    c.commit(); return c
def telegram(msg):
    r=requests.post("https://api.telegram.org/bot"+os.environ["TELEGRAM_BOT_TOKEN"]+"/sendMessage",
      json={"chat_id":os.environ["TELEGRAM_CHAT_ID"],"text":msg[:4000],"disable_web_page_preview":False},timeout=30)
    r.raise_for_status()
def state_in(text,state): return any(x in clean(text).lower() for x in STATES[state])
def extract(text):
    t=clean(text)
    a=re.search(r"\b(?:EC/)?[A-Z0-9_-]{2,20}/AGENDA/[A-Z0-9_/-]{3,100}\b",t,re.I)
    m=re.search(r"\b(?:EC/)?[A-Z0-9_-]{2,20}/MOM/[A-Z0-9_/-]{3,100}\b",t,re.I)
    p=re.search(r"\bSIA/[A-Z]{2,4}/[A-Z0-9_-]+/[0-9]+/[0-9]{4}\b",t,re.I)
    kind="Minutes / MoM" if m or re.search(r"\bminutes?\b|\bmom\b",t,re.I) else ("Agenda" if a or re.search(r"\bagenda\b",t,re.I) else "")
    return (a.group(0) if a else "",m.group(0) if m else "",p.group(0) if p else "",kind) if kind else None
def date_of(t):
    for p in [r"(?:Meeting Date|Date of Meeting|Date\s*&\s*Time|held from|Published(?: on)?)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",r"\bDate\s*[:\-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})"]:
        q=re.search(p,t,re.I)
        if q:return q.group(1)
    return ""
def title(t,k):
    ls=[clean(x) for x in str(t).splitlines() if clean(x)]
    for x in ls[:80]:
        if (k=="Agenda" and "agenda" in x.lower()) or (k!="Agenda" and ("minutes" in x.lower() or "mom" in x.lower())): return x[:450]
    return ls[0][:450] if ls else "PARIVESH 2.0 document"
def save_endpoint(c,r):
    try:
        c.execute("INSERT OR REPLACE INTO endpoints VALUES(?,?,?,?,?,?)",(r.url,r.request.method,r.status,r.headers.get("content-type",""),(r.request.post_data or "")[:10000],datetime.now(timezone.utc).isoformat())); c.commit()
    except: pass
def records(payload,url):
    out=[]
    def walk(o):
        yield o
        if isinstance(o,dict):
            for v in o.values():
                if isinstance(v,(dict,list)): yield from walk(v)
        elif isinstance(o,list):
            for v in o[:1000]:
                if isinstance(v,(dict,list)): yield from walk(v)
    for o in walk(payload):
        try:s=json.dumps(o,ensure_ascii=False)
        except:continue
        r=extract(s)
        if not r:continue
        a,m,p,k=r; urls=re.findall(r'https?://[^"\'\s<>]+',s)
        h=next((u.rstrip("\\,") for u in urls if ".pdf" in u.lower() or "/utildoc/" in u.lower()),"")
        out.append({"blob":s[:25000],"agenda_id":a,"mom_id":m,"proposal":p,"kind":k,"href":h,"source":url})
    return out

def main():
    c=db(); rec=[]; net={}; routes=[]; bundles=[]
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True); ctx=b.new_context(viewport={"width":1440,"height":1100},locale="en-IN",user_agent=UA); page=ctx.new_page()
        def onresp(r):
            save_endpoint(c,r); net[r.url]={"method":r.request.method,"status":r.status,"content_type":r.headers.get("content-type","")}
            if any(x in r.url.lower() for x in ("agenda","mom","minutes")): routes.append(r.url)
            if "json" in r.headers.get("content-type","").lower():
                try: rec.extend(records(r.json(),r.url))
                except: pass
        page.on("response",onresp)
        page.goto(EC_URL,wait_until="domcontentloaded",timeout=120000)
        try: page.wait_for_load_state("networkidle",timeout=30000)
        except PWTimeout: pass
        page.wait_for_timeout(7000)
        try:
            for a in page.locator("a").evaluate_all("""els=>els.map(a=>({href:a.href||'',text:(a.innerText||a.textContent||'').trim()}))"""):
                z=(a["href"]+" "+a["text"]).lower()
                if "agenda" in z or "mom" in z or "minutes" in z: routes.append(a["href"])
        except: pass
        for s in page.locator("script[src]").evaluate_all("els=>els.map(x=>x.src)"):
            if ".js" not in s: continue
            try:
                q=requests.get(s,headers={"User-Agent":UA},timeout=60)
                if q.ok:
                    bundles.append({"url":s,"bytes":len(q.content)})
                    for m in re.finditer(r'[^"\'`]{0,180}(?:ec-agenda-list|ec-mom-list|ec-agenda|ec-mom)[^"\'`]{0,220}',q.text,re.I): routes.append(clean(m.group(0)))
            except Exception as e: print("BUNDLE_ERROR",s,repr(e))
        for rx in [r"agenda",r"minutes",r"\bmom\b"]:
            try:
                loc=page.get_by_text(re.compile(rx,re.I))
                for i in range(min(loc.count(),25)):
                    try:
                        el=loc.nth(i)
                        if el.is_visible() and len(clean(el.inner_text(timeout=800)))<180:
                            el.click(timeout=3000); page.wait_for_timeout(2500)
                    except: pass
            except: pass
        for _ in range(8): page.mouse.wheel(0,2500); page.wait_for_timeout(600)
        page.wait_for_timeout(3000); ctx.close(); b.close()
    items={}
    for r in rec:
        for st in STATES:
            if state_in(r["blob"],st):
                key=r["mom_id"] or r["agenda_id"] or hashlib.sha256((r.get("href","")+r["blob"][:2000]).encode()).hexdigest()
                items[(st,key)]={"state":st,"authority":"SEIAA","kind":r["kind"],"title":title(r["blob"],r["kind"]),"date":date_of(r["blob"]),"agenda_id":r["agenda_id"],"mom_id":r["mom_id"],"proposal":r["proposal"],"href":r.get("href") or EC_URL}
    new=0
    for x in items.values():
        base=x["mom_id"] or x["agenda_id"] or hashlib.sha256(x["href"].encode()).hexdigest(); key=x["state"]+"|"+base
        if c.execute("SELECT 1 FROM seen WHERE k=?",(key,)).fetchone(): continue
        c.execute("INSERT INTO seen VALUES(?,?,?,?,?,?,?,?,?,?,?)",(key,x["state"],x["authority"],x["kind"],x["title"],x["date"],x["agenda_id"],x["mom_id"],x["proposal"],x["href"],datetime.now(timezone.utc).isoformat())); c.commit()
        msg="🔔 PARIVESH 2.0 – NEW SEIAA DOCUMENT\n\n"+f"State: {x['state']}\nAuthority: SEIAA\nType: {x['kind']}\nTitle: {x['title']}"
        if x["date"]: msg+=f"\nDate: {x['date']}"
        if x["agenda_id"]: msg+=f"\nAgenda ID: {x['agenda_id']}"
        if x["mom_id"]: msg+=f"\nMoM ID: {x['mom_id']}"
        if x["proposal"]: msg+=f"\nProposal: {x['proposal']}"
        telegram(msg+f"\n\n📄 Open: {x['href']}"); new+=1
    DIAG.parent.mkdir(exist_ok=True)
    DIAG.write_text(json.dumps({"generated_at":datetime.now(timezone.utc).isoformat(),"target":EC_URL,"js_bundles":bundles,"route_hits":routes[-1000:],"network_endpoints":net,"validated_records":list(items.values()),"new_records":new},ensure_ascii=False,indent=2),encoding="utf-8")
    print("VALIDATED",len(items),"NEW",new,"ROUTE_HITS",len(routes)); c.close()
if __name__=="__main__": main()
