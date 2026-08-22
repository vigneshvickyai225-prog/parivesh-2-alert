import os,sqlite3,hashlib,re,requests
from pathlib import Path
from datetime import datetime,timezone
from playwright.sync_api import sync_playwright,TimeoutError as PWTimeout

URL="https://parivesh.nic.in/#/ec"; STATES=["Tamil Nadu","Karnataka","Telangana"]
DB=Path("data/parivesh.db")

def init():
    DB.parent.mkdir(exist_ok=True)
    c=sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY,state TEXT,title TEXT,href TEXT,kind TEXT,proposal TEXT,seen_at TEXT)")
    c.commit(); return c

def tg(msg):
    t=os.environ["TELEGRAM_BOT_TOKEN"]; cid=os.environ["TELEGRAM_CHAT_ID"]
    r=requests.post(f"https://api.telegram.org/bot{t}/sendMessage",json={"chat_id":cid,"text":msg[:4000]},timeout=30)
    r.raise_for_status()

def kind(s):
    x=s.lower()
    if "minutes of meeting" in x or re.search(r"\bmom\b",x): return "Minutes / MoM"
    if "agenda" in x: return "Agenda"
    if "meeting" in x: return "Meeting"
    return "EC Proposal"

def props(s):
    out=[]
    for p in [r"\bSIA/(?:TN|KA|TS|TG)/[A-Z0-9_./-]+\b",r"\bSIA/[A-Z]{2,4}/[A-Z0-9_./-]+\b"]:
        out += re.findall(p,s,re.I)
    return list(dict.fromkeys(out))

def scrape(page,state):
    page.goto(URL,wait_until="domcontentloaded",timeout=120000)
    try: page.wait_for_load_state("networkidle",timeout=30000)
    except PWTimeout: pass
    page.wait_for_timeout(6000)
    # Try a state select if PARIVESH exposes one.
    for i in range(min(page.locator("select").count(),20)):
        try:
            el=page.locator("select").nth(i); txt=(el.inner_text(timeout=1000) or "").lower()
            if any(x in txt for x in ["tamil","karnataka","telangana"]):
                try: el.select_option(label=state); page.wait_for_timeout(2000); break
                except: pass
        except: pass
    rows=page.locator("a").evaluate_all("""els=>els.map(a=>({title:(a.innerText||a.textContent||'').trim(),href:a.href||'',text:(a.parentElement?a.parentElement.innerText:a.innerText||'').trim()}))""")
    rows += page.locator("button,[role=button],.card,.mat-card").evaluate_all("""els=>els.map(e=>({title:(e.innerText||e.textContent||'').trim(),href:'',text:(e.innerText||e.textContent||'').trim()}))""")
    out=[]
    for r in rows:
        s=re.sub(r"\s+"," ",r["title"]+" "+r["text"]).strip(); low=s.lower()
        if not any(x in low for x in ["agenda","minutes","mom","meeting","proposal","environmental clearance","ec "]): continue
        title=r["title"][:500]; href=r["href"] or URL; ps=", ".join(props(s)[:10])
        k=hashlib.sha256((state+"|"+href+"|"+title).encode()).hexdigest()
        out.append((k,state,title,href,kind(s),ps))
    return list(dict.fromkeys(out))

def main():
    c=init()
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True); page=b.new_page(viewport={"width":1440,"height":1000})
        items=[]
        for s in STATES:
            try: items += scrape(page,s)
            except Exception as e: print("ERROR",s,e)
        b.close()
    kws=[x.strip().lower() for x in os.getenv("WATCH_KEYWORDS","agenda,minutes,mom,environmental clearance").split(",") if x.strip()]
    all_new=os.getenv("SEND_ALL_NEW","false").lower()=="true"
    for k,s,title,href,typ,pr in items:
        if c.execute("SELECT 1 FROM seen WHERE k=?",(k,)).fetchone(): continue
        c.execute("INSERT INTO seen VALUES(?,?,?,?,?,?,?)",(k,s,title,href,typ,pr,datetime.now(timezone.utc).isoformat())); c.commit()
        if all_new or any(x in (title+" "+typ+" "+pr).lower() for x in kws):
            tg(f"🔔 PARIVESH 2.0 ALERT\n\nState: {s}\nType: {typ}\nTitle: {title}\nProposal: {pr or 'Not identified'}\n\nOpen: {href}")
    c.close()
if __name__=="__main__": main()
