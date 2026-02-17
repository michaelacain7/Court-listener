"""
Court Filing Monitor for Public Companies
==========================================
Two monitors in one:
  1. Federal court filings involving public companies (all federal courts)
  2. Supreme Court opinions/orders mentioning public companies

Alerts via Discord webhooks with DeepSeek AI summaries.
Runs 9 AM - 4 PM EST on market open days only.

RATE LIMITS:
  CourtListener: 5,000 req/hour (authenticated)
  At 10s intervals with staggered checks: ~360 req/hr core + follow-ups = ~600/hr (very safe)
  
Deploy on Railway.
"""

import os
import json
import time
import hashlib
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Optional
import requests
import pytz

# Discord Webhooks
DISCORD_WEBHOOK_FEDERAL = os.getenv(
    "DISCORD_WEBHOOK_FEDERAL",
    "https://discordapp.com/api/webhooks/1473448730416906250/fFtqjcW2uXr0T0SxwOFPmKC4gCsQQZRV5mDqgBSYGTZGXJ7XEkR7RVAjBaxFkxXi5bkN"
)
DISCORD_WEBHOOK_SCOTUS = os.getenv(
    "DISCORD_WEBHOOK_SCOTUS",
    "https://discordapp.com/api/webhooks/919672540237017138/Zga2QHBVwPUKXbCMNQ6hRXSsJaW8d136pOZNheRz1SK0YS5GIRnpjsGdN7trPul-zeXo"
)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-089099fee3e94c1c97189694645d6f92")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

COURTLISTENER_API_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "")
CL_BASE = "https://www.courtlistener.com"
CL_SEARCH_URL = f"{CL_BASE}/api/rest/v4/search/"
CL_DOCKET_URL = f"{CL_BASE}/api/rest/v4/dockets/"

# 10 seconds = fastest safe interval (staggered across 6 check types = each type runs every 60s)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
EST = pytz.timezone("US/Eastern")

# Rate limit tracker
class RateLimiter:
    def __init__(self, max_per_hour=4500):
        self.max_per_hour = max_per_hour
        self.requests = []
        self.lock = threading.Lock()
    
    def can_request(self):
        with self.lock:
            now = time.time()
            self.requests = [t for t in self.requests if now - t < 3600]
            return len(self.requests) < self.max_per_hour
    
    def record(self):
        with self.lock:
            self.requests.append(time.time())
    
    def remaining(self):
        with self.lock:
            now = time.time()
            self.requests = [t for t in self.requests if now - t < 3600]
            return self.max_per_hour - len(self.requests)
    
    def per_minute(self):
        with self.lock:
            now = time.time()
            return len([t for t in self.requests if now - t < 60])

rate_limiter = RateLimiter()

MARKET_HOLIDAYS = {
    "2025-01-01","2025-01-20","2025-02-17","2025-04-18","2025-05-26","2025-06-19",
    "2025-07-04","2025-09-01","2025-11-27","2025-12-25",
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03","2026-05-25","2026-06-19",
    "2026-07-03","2026-09-07","2026-11-26","2026-12-25",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("CourtMonitor")

PUBLIC_COMPANIES = {
    "Apple":"AAPL","Microsoft":"MSFT","Google":"GOOGL","Alphabet":"GOOGL",
    "Amazon":"AMZN","Meta Platforms":"META","Meta":"META","Facebook":"META",
    "Tesla":"TSLA","Nvidia":"NVDA","NVIDIA":"NVDA",
    "Intel":"INTC","AMD":"AMD","Advanced Micro Devices":"AMD",
    "Broadcom":"AVGO","Oracle":"ORCL","Salesforce":"CRM",
    "Adobe":"ADBE","Netflix":"NFLX","Uber":"UBER",
    "Airbnb":"ABNB","Snap":"SNAP","Palantir":"PLTR",
    "CrowdStrike":"CRWD","Snowflake":"SNOW","Datadog":"DDOG",
    "Shopify":"SHOP","Spotify":"SPOT","Pinterest":"PINS",
    "Roblox":"RBLX","Zoom":"ZM","DocuSign":"DOCU","Twilio":"TWLO",
    "Block":"SQ","Square":"SQ","PayPal":"PYPL","Coinbase":"COIN",
    "Robinhood":"HOOD","MicroStrategy":"MSTR","Qualcomm":"QCOM",
    "Texas Instruments":"TXN","Applied Materials":"AMAT","Lam Research":"LRCX",
    "Marvell":"MRVL","Micron":"MU","Arm Holdings":"ARM","Dell":"DELL",
    "IBM":"IBM","Cisco":"CSCO","Palo Alto Networks":"PANW","Fortinet":"FTNT",
    "ServiceNow":"NOW","Workday":"WDAY","Intuit":"INTU",
    "Autodesk":"ADSK","Synopsys":"SNPS","Cadence":"CDNS",
    "Arista Networks":"ANET","Cloudflare":"NET",
    "MongoDB":"MDB","Zscaler":"ZS","Okta":"OKTA",
    "Pfizer":"PFE","Johnson & Johnson":"JNJ","Johnson and Johnson":"JNJ",
    "Eli Lilly":"LLY","Merck":"MRK","AbbVie":"ABBV",
    "Bristol-Myers Squibb":"BMY","Bristol Myers":"BMY",
    "Amgen":"AMGN","Gilead":"GILD","Regeneron":"REGN",
    "Moderna":"MRNA","BioNTech":"BNTX","Vertex":"VRTX",
    "Biogen":"BIIB","Illumina":"ILMN","Intuitive Surgical":"ISRG",
    "Dexcom":"DXCM","Novo Nordisk":"NVO","AstraZeneca":"AZN",
    "Novartis":"NVS","Sanofi":"SNY","GlaxoSmithKline":"GSK","GSK":"GSK",
    "United Therapeutics":"UTHR","Liquidia":"LQDA",
    "Teva":"TEVA","Viatris":"VTRS","Jazz Pharmaceuticals":"JAZZ",
    "BioMarin":"BMRN","Alnylam":"ALNY","Sarepta":"SRPT",
    "Incyte":"INCY","Argenx":"ARGX","Blueprint Medicines":"BPMC",
    "JPMorgan":"JPM","JP Morgan":"JPM","Goldman Sachs":"GS",
    "Morgan Stanley":"MS","Bank of America":"BAC",
    "Wells Fargo":"WFC","Citigroup":"C","Citibank":"C",
    "Charles Schwab":"SCHW","BlackRock":"BLK","Blackstone":"BX",
    "KKR":"KKR","Apollo":"APO","Visa":"V","Mastercard":"MA",
    "American Express":"AXP","Capital One":"COF",
    "Progressive":"PGR","Allstate":"ALL","MetLife":"MET",
    "Prudential":"PRU","AIG":"AIG","Berkshire Hathaway":"BRK.B",
    "Intercontinental Exchange":"ICE","CME Group":"CME",
    "Nasdaq":"NDAQ","S&P Global":"SPGI","Moody's":"MCO",
    "SoFi":"SOFI","Affirm":"AFRM",
    "ExxonMobil":"XOM","Exxon":"XOM","Chevron":"CVX",
    "ConocoPhillips":"COP","EOG Resources":"EOG",
    "Devon Energy":"DVN","Marathon Petroleum":"MPC",
    "Valero":"VLO","Phillips 66":"PSX","Occidental":"OXY",
    "Schlumberger":"SLB","Halliburton":"HAL","Baker Hughes":"BKR",
    "NextEra Energy":"NEE","Duke Energy":"DUK",
    "Southern Company":"SO","Dominion Energy":"D",
    "Constellation Energy":"CEG","Cheniere Energy":"LNG",
    "Walmart":"WMT","Costco":"COST","Target":"TGT",
    "Home Depot":"HD","Lowe's":"LOW","Lowes":"LOW",
    "Kroger":"KR","Albertsons":"ACI",
    "Coca-Cola":"KO","PepsiCo":"PEP","Pepsi":"PEP",
    "Procter & Gamble":"PG","Nike":"NKE","Lululemon":"LULU",
    "Starbucks":"SBUX","McDonald's":"MCD","McDonalds":"MCD",
    "Chipotle":"CMG","Marriott":"MAR","Hilton":"HLT",
    "Disney":"DIS","Walt Disney":"DIS","Comcast":"CMCSA",
    "Warner Bros":"WBD","Paramount":"PARA","Fox":"FOX",
    "Booking Holdings":"BKNG","Expedia":"EXPE",
    "Boeing":"BA","Lockheed Martin":"LMT","Raytheon":"RTX",
    "RTX":"RTX","Northrop Grumman":"NOC",
    "General Dynamics":"GD","L3Harris":"LHX",
    "Honeywell":"HON","3M":"MMM","General Electric":"GE",
    "Caterpillar":"CAT","Deere":"DE","John Deere":"DE",
    "Danaher":"DHR","TransDigm":"TDG","Palantir Technologies":"PLTR",
    "AT&T":"T","Verizon":"VZ","T-Mobile":"TMUS",
    "Charter Communications":"CHTR",
    "Prologis":"PLD","American Tower":"AMT","Equinix":"EQIX",
    "Simon Property":"SPG","Digital Realty":"DLR",
    "Ford":"F","General Motors":"GM","Rivian":"RIVN",
    "Lucid":"LCID","NIO":"NIO",
    "UnitedHealth":"UNH","Elevance Health":"ELV","Cigna":"CI",
    "Humana":"HUM","CVS Health":"CVS","CVS":"CVS",
    "Walgreens":"WBA","McKesson":"MCK",
    "HCA Healthcare":"HCA","Thermo Fisher":"TMO",
    "Stryker":"SYK","Medtronic":"MDT","Abbott":"ABT",
    "Boston Scientific":"BSX","Accenture":"ACN",
    "Waste Management":"WM","Cintas":"CTAS",
    "FedEx":"FDX","UPS":"UPS","United Parcel":"UPS",
    "CSX":"CSX","Union Pacific":"UNP","Norfolk Southern":"NSC",
    "Delta Air Lines":"DAL","United Airlines":"UAL",
    "American Airlines":"AAL","Southwest Airlines":"LUV",
    "Carnival":"CCL","Royal Caribbean":"RCL",
}

SEEN_FILE = "seen_filings.json"

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
            return {k:v for k,v in data.items() if v > cutoff}
    except:
        return {}

def save_seen(seen):
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with open(SEEN_FILE, "w") as f:
        json.dump({k:v for k,v in seen.items() if v > cutoff}, f)

def mark_seen(seen, fid):
    seen[fid] = datetime.utcnow().isoformat()

def is_market_hours():
    now = datetime.now(EST)
    if now.weekday() >= 5: return False
    if now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS: return False
    if now.hour < 9 or now.hour >= 16: return False
    return True

def get_cl_headers():
    h = {"Accept": "application/json"}
    if COURTLISTENER_API_TOKEN: h["Authorization"] = f"Token {COURTLISTENER_API_TOKEN}"
    return h

def cl_request(url, params=None, timeout=20):
    if not rate_limiter.can_request():
        log.warning(f"Rate limit nearing ({rate_limiter.remaining()} left). Pausing 30s...")
        time.sleep(30)
        if not rate_limiter.can_request():
            log.error("Rate limit exhausted. Skipping.")
            return None
    rate_limiter.record()
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=get_cl_headers(), timeout=timeout)
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 60))
                log.warning(f"CL 429 - waiting {retry}s (attempt {attempt+1})")
                time.sleep(min(retry, 120))
                continue
            if resp.status_code == 401:
                log.error("CL auth failed. Set COURTLISTENER_API_TOKEN!")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            time.sleep(5)
        except requests.exceptions.RequestException as e:
            log.error(f"CL request failed: {e}")
            time.sleep(5)
    return None

def match_public_company(case_name):
    if not case_name: return []
    cu = case_name.upper()
    seen_t = set()
    matches = []
    for co, tk in PUBLIC_COMPANIES.items():
        if tk in seen_t: continue
        c = co.upper()
        if c in cu and re.search(r'\b' + re.escape(c) + r'\b', cu):
            seen_t.add(tk)
            matches.append((co, tk))
    return matches

def summarize_with_deepseek(text, context="court filing"):
    if not text or len(text.strip()) < 50:
        return "No substantial text available for summarization."
    if len(text) > 12000: text = text[:12000] + "..."
    try:
        resp = requests.post(DEEPSEEK_API_URL, headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }, json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": (
                    "You are a financial analyst specializing in legal matters affecting publicly traded companies. "
                    "Summarize court documents concisely: 1) What the case is about, 2) Key parties & tickers, "
                    "3) Potential market impact, 4) Key legal issues, 5) Next steps. Under 250 words. "
                    "Bullet points. Direct and actionable for traders."
                )},
                {"role": "user", "content": f"Summarize this {context}:\n\n{text}"}
            ],
            "max_tokens": 500, "temperature": 0.3
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"DeepSeek failed: {e}")
        return "AI summary unavailable - see original filing."

def send_discord(webhook_url, embeds):
    for _ in range(3):
        try:
            resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=15)
            if resp.status_code == 429:
                time.sleep(resp.json().get("retry_after", 5) + 0.5)
                continue
            return resp.status_code in (200, 204)
        except:
            time.sleep(2)
    return False

def make_id(item, prefix=""):
    raw = f"{prefix}_{item.get('docket_id','')}_{item.get('dateFiled','')}_{item.get('caseName',item.get('case_name',''))}"
    return hashlib.md5(raw.encode()).hexdigest()

# ── Check Functions ──

def check_federal_filings(seen):
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={
        "type":"r","order_by":"dateFiled desc","filed_after":yesterday,
        "court":"dcd nyed cacd cand ilnd txsd txnd fled flsd mad nysd paed njd ded vaed wawd coed gand mdd ctd ca1 ca2 ca3 ca4 ca5 ca6 ca7 ca8 ca9 ca10 ca11 cadc cafc"
    })
    if not data: return 0
    alerts = 0
    for item in data.get("results", []):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "fed")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        if not matches: continue
        log.info(f"FEDERAL: {cn[:80]}")
        details = []
        for k,l in [("court","Court"),("dateFiled","Filed"),("suitNature","Nature"),("cause","Cause"),("assignedTo","Judge")]:
            if item.get(k): details.append(f"**{l}:** {item[k]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        docket_id = item.get("docket_id","")
        dtext = snippet
        if docket_id and rate_limiter.remaining() > 500:
            dd = cl_request(f"{CL_DOCKET_URL}{docket_id}/", params={"fields":"id,case_name,nature_of_suit,cause"}, timeout=10)
            if dd:
                for k in ["nature_of_suit","cause"]:
                    if dd.get(k): dtext += f"\n{k}: {dd[k]}"
        stxt = f"Case: {cn}\n{dtext}"
        summary = summarize_with_deepseek(stxt) if len(stxt) > 80 else "Limited details."
        tstr = " | ".join([f"**${t}** ({c})" for c,t in matches])
        url = f"{CL_BASE}{item.get('absolute_url','')}" if item.get("absolute_url") else f"{CL_BASE}/docket/{docket_id}/"
        embed = {"title":f"⚖️ Federal Filing: {cn[:200]}","url":url,"color":0xFF6B35,
            "fields":[{"name":"📊 Tickers","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} API calls left"},"timestamp":datetime.utcnow().isoformat()}
        if details: embed["fields"].append({"name":"📋 Details","value":"\n".join(details[:5]),"inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_federal_company(seen, idx):
    companies = ["Apple","Google","Microsoft","Amazon","Meta","Tesla","Nvidia",
        "Pfizer","Johnson Johnson","Eli Lilly","Merck","AbbVie",
        "JPMorgan","Goldman Sachs","Bank of America","Wells Fargo",
        "ExxonMobil","Chevron","Boeing","Disney","Walmart","Visa","Mastercard",
        "UnitedHealth","Moderna","Gilead","Regeneron","Amgen","AT&T","Verizon",
        "Comcast","Netflix","Qualcomm","Intel","AMD","Broadcom","Costco","Target",
        "Home Depot","Nike","Lockheed Martin","Raytheon","Berkshire Hathaway","BlackRock","Coinbase"]
    co = companies[idx % len(companies)]
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={"q":co,"type":"r","order_by":"dateFiled desc","filed_after":yesterday})
    if not data: return 0
    alerts = 0
    for item in data.get("results",[]):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "comp")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        if not matches: continue
        log.info(f"COMPANY [{co}]: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"Case: {cn}\n{snippet}")
        tstr = " | ".join([f"**${t}** ({c})" for c,t in matches])
        url = f"{CL_BASE}{item.get('absolute_url','')}" if item.get("absolute_url") else ""
        embed = {"title":f"⚖️ Filing: {cn[:200]}","url":url,"color":0xFF6B35,
            "fields":[{"name":"📊 Tickers","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.utcnow().isoformat()}
        if item.get("court"): embed["fields"].append({"name":"🏛️ Court","value":item["court"],"inline":True})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_scotus_opinions(seen):
    fa = (datetime.now(EST) - timedelta(days=3)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={"type":"o","court":"scotus","order_by":"dateFiled desc","filed_after":fa})
    if not data: return 0
    alerts = 0
    for item in data.get("results",[]):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "scotus_op")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        otxt = snippet
        cid = item.get("cluster_id","")
        if cid and rate_limiter.remaining() > 500:
            cd = cl_request(f"{CL_BASE}/api/rest/v4/clusters/{cid}/",
                params={"fields":"id,case_name,syllabus,judges,date_filed"}, timeout=10)
            if cd and cd.get("syllabus"): otxt = cd["syllabus"]
        summary = summarize_with_deepseek(f"Supreme Court: {cn}\n\n{otxt}", "Supreme Court opinion")
        color = 0xDC143C if matches else 0x4169E1
        url = f"{CL_BASE}{item.get('absolute_url','')}" if item.get("absolute_url") else ""
        embed = {"title":f"🏛️ SCOTUS: {cn[:200]}","url":url,"color":color,"fields":[],
            "footer":{"text":f"SCOTUS Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.utcnow().isoformat()}
        if matches:
            tstr = " | ".join([f"**${t}** ({c})" for c,t in matches])
            embed["fields"].append({"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        if item.get("status"): embed["fields"].append({"name":"📋 Status","value":item["status"],"inline":True})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_scotus_docket(seen):
    fa = (datetime.now(EST) - timedelta(days=3)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={"type":"r","court":"scotus","order_by":"dateFiled desc","filed_after":fa})
    if not data: return 0
    alerts = 0
    for item in data.get("results",[]):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "scotus_dkt")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        if not matches: continue
        log.info(f"SCOTUS DOCKET: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"SCOTUS Docket: {cn}\n\n{snippet}", "Supreme Court docket entry")
        tstr = " | ".join([f"**${t}** ({c})" for c,t in matches])
        url = f"{CL_BASE}{item.get('absolute_url','')}" if item.get("absolute_url") else ""
        embed = {"title":f"🏛️ SCOTUS Docket: {cn[:200]}","url":url,"color":0xDC143C,
            "fields":[{"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False},
                {"name":"🤖 AI Summary","value":summary[:1000],"inline":False}],
            "footer":{"text":f"SCOTUS Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.utcnow().isoformat()}
        send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_scotus_website(seen):
    alerts = 0
    try:
        resp = requests.get("https://www.supremecourt.gov/opinions/slipopinion/24",
            timeout=15, headers={"User-Agent":"CourtFilingMonitor/1.0"})
        if not resp.ok: return 0
        from html.parser import HTMLParser
        class P(HTMLParser):
            def __init__(self):
                super().__init__(); self.in_row=False; self.cur=[]; self.rows=[]
            def handle_starttag(self,t,a):
                if t=="tr": self.in_row=True; self.cur=[]
            def handle_endtag(self,t):
                if t=="tr" and self.in_row: self.in_row=False; self.rows.append(self.cur) if self.cur else None
            def handle_data(self,d):
                if self.in_row and d.strip(): self.cur.append(d.strip())
        p = P(); p.feed(resp.text)
        for row in p.rows[-15:]:
            rt = " ".join(row)
            fid = f"scotusgov_{hashlib.md5(rt.encode()).hexdigest()}"
            if fid in seen: continue
            mark_seen(seen, fid)
            matches = match_public_company(rt)
            if not matches: continue
            log.info(f"SCOTUS.GOV: {rt[:80]}")
            summary = summarize_with_deepseek(f"Supreme Court from supremecourt.gov:\n{rt}", "Supreme Court opinion")
            tstr = " | ".join([f"**${t}** ({c})" for c,t in matches])
            embed = {"title":f"🏛️ SCOTUS: {rt[:200]}",
                "url":"https://www.supremecourt.gov/opinions/slipopinion/24","color":0xDC143C,
                "fields":[{"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False},
                    {"name":"🤖 AI Summary","value":summary[:1000],"inline":False}],
                "footer":{"text":"SCOTUS Monitor | supremecourt.gov"},"timestamp":datetime.utcnow().isoformat()}
            send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
            alerts += 1; time.sleep(0.5)
    except Exception as e:
        log.error(f"SCOTUS.gov failed: {e}")
    return alerts

# ── Main Loop ──

def main():
    log.info("="*60)
    log.info("Court Filing Monitor Starting")
    log.info(f"  Check interval: {CHECK_INTERVAL}s (each source every {CHECK_INTERVAL*6}s)")
    log.info(f"  CL Token: {'SET' if COURTLISTENER_API_TOKEN else 'NOT SET - get one at courtlistener.com!'}")
    log.info(f"  Hours: 9AM-4PM EST, market days only")
    log.info("="*60)

    if not COURTLISTENER_API_TOKEN:
        log.warning("COURTLISTENER_API_TOKEN not set! Get free token: https://www.courtlistener.com/sign-in/")

    seen = load_seen()
    cycle = 0; cidx = 0; last_save = time.time()

    startup = {"title":"🟢 Court Filing Monitor Online","description":(
        f"• Federal courts: every **{CHECK_INTERVAL*3}s** (broad + company)\n"
        f"• Supreme Court: every **{CHECK_INTERVAL*3}s** (opinions + docket + website)\n"
        f"• Companies: **{len(PUBLIC_COMPANIES)}** tracked\n"
        f"• Hours: 9AM-4PM EST\n• CL Auth: {'✅' if COURTLISTENER_API_TOKEN else '⚠️ Not set'}\n"
        f"• AI: DeepSeek"),
        "color":0x00FF00,"timestamp":datetime.utcnow().isoformat()}
    send_discord(DISCORD_WEBHOOK_FEDERAL, [startup])
    send_discord(DISCORD_WEBHOOK_SCOTUS, [startup])

    while True:
        try:
            if not is_market_hours():
                now = datetime.now(EST)
                if now.minute == 0 and now.second < 35:
                    log.info(f"Outside market hours ({now.strftime('%I:%M %p %Z')})")
                time.sleep(30)
                continue

            # Staggered 6-phase cycle: each source checked every 60s at 10s intervals
            # Phase 0: Federal broad     Phase 1: SCOTUS opinions
            # Phase 2: Federal company   Phase 3: SCOTUS docket
            # Phase 4: Federal broad     Phase 5: SCOTUS website (no API)
            phase = cycle % 6

            if phase in (0, 4):
                n = check_federal_filings(seen)
                if n: log.info(f"Sent {n} federal alerts")
            elif phase == 1:
                n = check_scotus_opinions(seen)
                if n: log.info(f"Sent {n} SCOTUS opinion alerts")
            elif phase == 2:
                n = check_federal_company(seen, cidx); cidx += 1
                if n: log.info(f"Sent {n} company alerts")
            elif phase == 3:
                n = check_scotus_docket(seen)
                if n: log.info(f"Sent {n} SCOTUS docket alerts")
            elif phase == 5:
                n = check_scotus_website(seen)
                if n: log.info(f"Sent {n} SCOTUS.gov alerts")

            cycle += 1

            if time.time() - last_save > 120:
                save_seen(seen); last_save = time.time()

            if cycle % 50 == 0:
                log.info(f"Status: {rate_limiter.remaining()} CL API left | {rate_limiter.per_minute():.0f} req/min | {len(seen)} tracked")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Shutting down..."); save_seen(seen); break
        except Exception as e:
            log.error(f"Loop error: {e}"); time.sleep(15)

if __name__ == "__main__":
    main()
