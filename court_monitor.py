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

COURTLISTENER_API_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "b4863b70263a553e98685a87ea755a8ad5b0bfaa")
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

# ── Dynamic SEC Company Loader ──
# Downloads ALL public companies from SEC on startup (~10,000 tickers)
# Falls back to curated aliases if SEC is unreachable

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_CACHE = "sec_tickers_cache.json"

# Suffixes to strip from SEC names for matching
_STRIP_SUFFIXES = re.compile(
    r'\s*[,/]?\s*\b('
    r'Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?|Company|'
    r'Ltd\.?|Limited|LLC|L\.?L\.?C\.?|LP|L\.?P\.?|'
    r'PLC|P\.?L\.?C\.?|NV|N\.?V\.?|SA|S\.?A\.?|SE|S\.?E\.?|AG|'
    r'Class [A-Z]|Cl [A-Z]|THE'
    r')\s*\.?\s*$|/[A-Z]{2,3}\s*$', re.IGNORECASE)

# Names too short or generic to match safely (would cause false positives)
_BLOCKED_NAMES = {
    "A","AN","THE","AT","ON","TO","OF","IN","IT","UP","GO","DO","SO","OR","BY",
    "ALL","ONE","TWO","NEW","BIG","NET","NOW","RED","TEN","OUT","OLD",
    "US","AI","IO","IV","CAN","MAN","SUN","ACE","KEY","SEA","SKY","AIR",
    "USA","PLUS","CORE","STAR","GOLD","BLUE","OPEN","TRUE","REAL","PURE",
    "FIRST","SOUTH","NORTH","EAST","WEST","CLEAR","PRIME","QUEST","TRUST",
    "GLOBAL","SUMMIT","UNITED","SELECT","IMPACT","VISION","ENERGY",
}

# Curated aliases (common names not in SEC format, or important short-name overrides)
ALIAS_COMPANIES = {
    "Apple":"AAPL","Google":"GOOGL","Alphabet":"GOOGL","Meta":"META","Facebook":"META",
    "Meta Platforms":"META","NVIDIA":"NVDA","Nvidia":"NVDA","AMD":"AMD","Tesla":"TSLA",
    "Johnson & Johnson":"JNJ","Johnson and Johnson":"JNJ","JP Morgan":"JPM",
    "JPMorgan":"JPM","Exxon":"XOM","ExxonMobil":"XOM","Bristol Myers":"BMY",
    "Bristol-Myers Squibb":"BMY","Lowe's":"LOW","Lowes":"LOW","McDonald's":"MCD",
    "McDonalds":"MCD","Coca-Cola":"KO","Pepsi":"PEP","AT&T":"T","T-Mobile":"TMUS",
    "Procter & Gamble":"PG","S&P Global":"SPGI","Moody's":"MCO","3M":"MMM",
    "Walt Disney":"DIS","Disney":"DIS","John Deere":"DE","Square":"SQ","Block":"SQ",
    "Citibank":"C","United Parcel":"UPS","GlaxoSmithKline":"GSK","GSK":"GSK",
    "RTX":"RTX","CVS Health":"CVS","Warner Bros":"WBD","Fox":"FOX",
    "Berkshire Hathaway":"BRK.B","United Therapeutics":"UTHR","Liquidia":"LQDA",
    "CrowdStrike":"CRWD","Palantir":"PLTR","Snowflake":"SNOW",
    "PayPal":"PYPL","Coinbase":"COIN","Robinhood":"HOOD",
    "Boeing":"BA","Lockheed Martin":"LMT","Raytheon":"RTX",
    "Northrop Grumman":"NOC","General Dynamics":"GD",
    "Ford":"F","General Motors":"GM","Rivian":"RIVN",
    "Home Depot":"HD","Costco":"COST","Target":"TGT","Walmart":"WMT",
    "Goldman Sachs":"GS","Morgan Stanley":"MS","Bank of America":"BAC",
    "Wells Fargo":"WFC","Citigroup":"C","BlackRock":"BLK",
    "Visa":"V","Mastercard":"MA","American Express":"AXP",
    "UnitedHealth":"UNH","Humana":"HUM","Cigna":"CI",
    "Pfizer":"PFE","Merck":"MRK","AbbVie":"ABBV","Moderna":"MRNA",
    "Amgen":"AMGN","Gilead":"GILD","Regeneron":"REGN",
    "Starbucks":"SBUX","Nike":"NKE","Chipotle":"CMG",
    "Netflix":"NFLX","Uber":"UBER","Airbnb":"ABNB","Spotify":"SPOT",
    "FedEx":"FDX","Delta Air Lines":"DAL","United Airlines":"UAL",
    "American Airlines":"AAL","Southwest Airlines":"LUV",
    "Verizon":"VZ","Comcast":"CMCSA","Charter Communications":"CHTR",
    "Intel":"INTC","Broadcom":"AVGO","Qualcomm":"QCOM","Micron":"MU",
    "Oracle":"ORCL","Salesforce":"CRM","Adobe":"ADBE","IBM":"IBM",
    "Cisco":"CSCO","ServiceNow":"NOW","Intuit":"INTU",
    "Caterpillar":"CAT","Honeywell":"HON","General Electric":"GE",
    "Chevron":"CVX","ConocoPhillips":"COP","Schlumberger":"SLB","Halliburton":"HAL",
    "Abbott":"ABT","Medtronic":"MDT","Stryker":"SYK","Boston Scientific":"BSX",
    "Thermo Fisher":"TMO","Danaher":"DHR","Eli Lilly":"LLY",
    "Corcept":"CORT","Corcept Therapeutics":"CORT","Teva":"TEVA","Teva Pharmaceuticals":"TEVA",
    "Samsung":"SSNLF","Samsung Electronics":"SSNLF",
    "Hulu":"DIS","Jazz Pharmaceuticals":"JAZZ","BioMarin":"BMRN",
    "Alnylam":"ALNY","Sarepta":"SRPT","Biogen":"BIIB","Illumina":"ILMN",
    "Intuitive Surgical":"ISRG","Dexcom":"DXCM","Vertex":"VRTX",
    "AstraZeneca":"AZN","Novartis":"NVS","Sanofi":"SNY","Novo Nordisk":"NVO",
    "Arm Holdings":"ARM","Dell":"DELL","Arista Networks":"ANET",
    "Palo Alto Networks":"PANW","Fortinet":"FTNT","Workday":"WDAY",
    "Snap":"SNAP","Pinterest":"PINS","Reddit":"RDDT","DoorDash":"DASH",
    "Lucid":"LCID","Roblox":"RBLX","Unity":"U","Shopify":"SHOP",
    "MicroStrategy":"MSTR","Affirm":"AFRM","SoFi":"SOFI",
    "DraftKings":"DKNG","FanDuel":"FLUT","Flutter":"FLUT",
    "Sable Offshore":"SOL","Sable":"SOL","Pacific Pipeline":"SOL",
    "Databricks":"🔥PRIVATE","ByteDance":"🔥PRIVATE","TikTok":"🔥PRIVATE",
}

# Buzzy private companies that move markets despite not being publicly traded
# These get a special "PRIVATE" tag instead of a ticker
BUZZY_COMPANIES = {
    # AI companies
    "OpenAI": "🔥PRIVATE", "xAI": "🔥PRIVATE", "X.AI": "🔥PRIVATE",
    "Anthropic": "🔥PRIVATE", "Perplexity": "🔥PRIVATE",
    "Mistral": "🔥PRIVATE", "Cohere": "🔥PRIVATE",
    "DeepSeek": "🔥PRIVATE", "Stability AI": "🔥PRIVATE",
    # Tech unicorns
    "SpaceX": "🔥PRIVATE", "Space Exploration Technologies": "🔥PRIVATE",
    "Stripe": "🔥PRIVATE", "Databricks": "🔥PRIVATE",
    "ByteDance": "🔥PRIVATE", "TikTok": "🔥PRIVATE",
    "Discord": "🔥PRIVATE", "Canva": "🔥PRIVATE",
    "Epic Games": "🔥PRIVATE", "Valve": "🔥PRIVATE",
    # Fintech / prediction markets
    "Kalshi": "🔥PRIVATE", "KalshiEX": "🔥PRIVATE",
    "Polymarket": "🔥PRIVATE", "Ripple": "🔥PRIVATE",
    "Kraken": "🔥PRIVATE", "Circle": "🔥PRIVATE",
    "Tether": "🔥PRIVATE", "Binance": "🔥PRIVATE",
    "FTX": "🔥PRIVATE",
    # Other notable privates
    "Anduril": "🔥PRIVATE", "Scale AI": "🔥PRIVATE",
    "Flexport": "🔥PRIVATE", "Plaid": "🔥PRIVATE",
    "Figma": "🔥PRIVATE", "Notion": "🔥PRIVATE",
    "CoreWeave": "🔥PRIVATE", "Lambda": "🔥PRIVATE",
    "Cerebras": "🔥PRIVATE", "Groq": "🔥PRIVATE",
    "Character AI": "🔥PRIVATE",
    # These influence public company stocks heavily
    "CFTC": "🏛️GOV", "SEC": "🏛️GOV", "FTC": "🏛️GOV", "DOJ": "🏛️GOV",
}

def _clean_sec_name(raw):
    """Clean SEC company name for matching: strip suffixes, normalize."""
    name = raw.strip()
    # Strip trailing suffixes repeatedly
    for _ in range(3):
        cleaned = _STRIP_SUFFIXES.sub('', name).strip().rstrip(',').strip()
        if cleaned == name: break
        name = cleaned
    # Strip leading "The "
    if name.upper().startswith("THE "):
        name = name[4:]
    return name.strip()

def load_sec_companies():
    """Download ALL SEC-registered companies. Returns dict of {name: ticker}."""
    companies = {}

    # Try cached file first (avoid hammering SEC on restarts)
    try:
        with open(SEC_TICKERS_CACHE) as f:
            cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            if age < 86400:  # Cache valid for 24 hours
                companies = {k:v for k,v in cached.items() if k != "_ts"}
                if len(companies) > 5000:
                    log.info(f"Loaded {len(companies)} companies from cache")
                    return companies
    except:
        pass

    # Download fresh from SEC
    log.info("Downloading SEC company tickers...")
    try:
        resp = requests.get(SEC_TICKERS_URL, headers={
            "User-Agent": "CourtFilingMonitor/1.0 (court-monitor@example.com)",
            "Accept": "application/json"
        }, timeout=30)
        resp.raise_for_status()
        raw = resp.json()

        seen_tickers = set()
        for entry in raw.values():
            ticker = entry.get("ticker", "").strip().upper()
            title = entry.get("title", "").strip()
            if not ticker or not title: continue
            if len(ticker) > 6: continue  # Skip warrants, units, etc.

            cleaned = _clean_sec_name(title)
            if not cleaned or len(cleaned) < 3: continue
            if cleaned.upper() in _BLOCKED_NAMES: continue

            # Use title-case version for nicer display
            display = cleaned.title() if cleaned.isupper() else cleaned
            if ticker not in seen_tickers:
                companies[display] = ticker
                seen_tickers.add(ticker)

                # Also add the original SEC name (cleaned) if different
                orig_clean = _clean_sec_name(title)
                if orig_clean != display and orig_clean.upper() not in _BLOCKED_NAMES:
                    companies[orig_clean] = ticker

        # Cache to disk
        cache_data = dict(companies)
        cache_data["_ts"] = time.time()
        with open(SEC_TICKERS_CACHE, "w") as f:
            json.dump(cache_data, f)

        log.info(f"Loaded {len(companies)} companies from SEC ({len(seen_tickers)} unique tickers)")

    except Exception as e:
        log.warning(f"SEC download failed: {e}. Using aliases only.")

    return companies

def build_company_db():
    """Build the full company database: SEC companies + curated aliases + buzzy privates."""
    db = load_sec_companies()
    # Merge aliases (these override SEC names for common short-form names)
    for name, ticker in ALIAS_COMPANIES.items():
        db[name] = ticker
    # Merge buzzy private companies
    for name, tag in BUZZY_COMPANIES.items():
        db[name] = tag
    public_count = len(set(v for v in db.values() if not v.startswith("🔥") and not v.startswith("🏛️")))
    private_count = len(set(k for k, v in db.items() if v.startswith("🔥")))
    log.info(f"Company database: {len(db)} matchable names, {public_count} public tickers, {private_count} private companies")
    return db

# Will be populated at startup in main()
PUBLIC_COMPANIES = {}

def _get_seen_path():
    """Find persistent storage path. Railway volumes survive deploys; /app does not."""
    # Check env var first (user can set VOLUME_PATH in Railway)
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("VOLUME_PATH", "")
    if vol and os.path.isdir(vol):
        return os.path.join(vol, "seen_filings.json")
    # Check common Railway volume mount points
    for path in ["/data", "/vol", "/persistent", "/mnt/data"]:
        if os.path.isdir(path):
            try:
                test_file = os.path.join(path, ".write_test")
                with open(test_file, "w") as f: f.write("test")
                os.remove(test_file)
                return os.path.join(path, "seen_filings.json")
            except: continue
    # Fallback to local (won't persist across deploys)
    return "seen_filings.json"

SEEN_FILE = _get_seen_path()

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            cutoff = (datetime.now(tz=pytz.utc) - timedelta(days=7)).isoformat()
            return {k:v for k,v in data.items() if v > cutoff}
    except:
        return {}

def save_seen(seen):
    cutoff = (datetime.now(tz=pytz.utc) - timedelta(days=7)).isoformat()
    with open(SEEN_FILE, "w") as f:
        json.dump({k:v for k,v in seen.items() if v > cutoff}, f)

def mark_seen(seen, fid):
    seen[fid] = datetime.now(tz=pytz.utc).isoformat()

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
    # Sort by name length descending so "Goldman Sachs" matches before "Gold"
    for co, tk in sorted(PUBLIC_COMPANIES.items(), key=lambda x: len(x[0]), reverse=True):
        if tk in seen_t: continue
        c = co.upper()
        if len(c) < 3: continue
        # Fast substring check first, then word boundary regex
        if c in cu:
            try:
                if re.search(r'\b' + re.escape(c) + r'\b', cu):
                    seen_t.add(tk)
                    matches.append((co, tk))
            except re.error:
                pass
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
                    "You are a financial analyst summarizing court filings for stock traders. "
                    "CRITICAL RULES:\n"
                    "1. DETERMINE THE OUTCOME FIRST: Who won? Who lost? Was a motion granted or denied? "
                    "Was a lower court affirmed or reversed? Read the text carefully for words like "
                    "'affirm', 'reverse', 'remand', 'grant', 'deny', 'dismiss', 'no infringement', "
                    "'infringement found', 'invalidated', 'upheld'.\n"
                    "2. NEVER GUESS the outcome. If the text doesn't contain the actual ruling, say "
                    "'Outcome unclear from available text - read full filing.'\n"
                    "3. 'We affirm' = the appeals court AGREES with the lower court's ruling.\n"
                    "4. For patent cases: 'affirm finding of no infringement' = PATENT HOLDER LOSES, "
                    "defendant/generic maker WINS. 'Reverse finding of no infringement' = PATENT HOLDER WINS.\n"
                    "5. Market impact must match the ACTUAL outcome, not assumptions.\n\n"
                    "Format (bullet points, under 250 words):\n"
                    "• **Case Overview:** What happened\n"
                    "• **Key Parties & Tickers:** Who vs who\n"
                    "• **RULING:** Explicitly who won and who lost\n"
                    "• **Market Impact:** Based ONLY on the actual ruling\n"
                    "• **Next Steps:** What comes next"
                )},
                {"role": "user", "content": f"Summarize this {context}:\n\n{text}"}
            ],
            "max_tokens": 500, "temperature": 0.2
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"DeepSeek failed: {e}")
        return "AI summary unavailable - see original filing."

def check_materiality(case_name, court="", snippet="", filing_type="new lawsuit"):
    """Quick DeepSeek call to classify if a new case filing is material to stock price.
    Returns (is_material: bool, reason: str). Errs on side of alerting for ambiguous cases."""
    DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY","")
    if not DEEPSEEK_KEY:
        return True, "No AI key - defaulting to alert"
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"
        }, json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": (
                    "You are a financial materiality classifier. Given a court case filing, "
                    "determine if it is MATERIAL to the stock price of the companies involved.\n\n"
                    "MATERIAL cases (alert YES):\n"
                    "- Class action lawsuits\n"
                    "- Government enforcement (SEC, DOJ, FTC, CFTC, state AG, CFPB)\n"
                    "- Antitrust / monopoly suits\n"
                    "- Major patent/IP disputes between competitors\n"
                    "- Securities fraud allegations\n"
                    "- Multidistrict litigation (MDL)\n"
                    "- Environmental/regulatory with potential large fines\n"
                    "- Whistleblower / qui tam suits\n"
                    "- Major contract disputes ($100M+)\n"
                    "- Cases filed BY the company (offensive litigation)\n"
                    "- Cases involving data breaches, product liability recalls\n"
                    "- Any case likely to generate significant media coverage\n\n"
                    "NOT MATERIAL cases (alert NO):\n"
                    "- Individual plaintiff personal injury\n"
                    "- Individual employment disputes (wrongful termination, discrimination by one person)\n"
                    "- Small claims or minor contract disputes\n"
                    "- Routine slip-and-fall / premises liability\n"
                    "- Individual consumer complaints\n"
                    "- Traffic/vehicle accidents with individual plaintiffs\n"
                    "- Workers' comp claims\n"
                    "- Individual insurance disputes\n\n"
                    "When uncertain, lean toward YES (better to alert than miss).\n"
                    "Respond with EXACTLY one line: YES or NO followed by a brief reason.\n"
                    "Example: YES - Class action securities fraud with potential $500M exposure\n"
                    "Example: NO - Individual plaintiff personal injury lawsuit"
                )},
                {"role": "user", "content": (
                    f"Case: {case_name}\n"
                    f"Court: {court}\n"
                    f"Type: {filing_type}\n"
                    f"Details: {snippet[:500]}"
                )}
            ],
            "max_tokens": 80, "temperature": 0.1
        }, timeout=15)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        is_material = answer.upper().startswith("YES")
        log.info(f"  Materiality check: {answer[:120]}")
        return is_material, answer
    except Exception as e:
        log.debug(f"Materiality check failed: {e}")
        return True, "Check failed - defaulting to alert"

# Global flag: when True, send_discord silently succeeds without actually posting
SEED_MODE = False

def send_discord(webhook_url, embeds):
    if SEED_MODE:
        return True  # Silently succeed during seed phase
    for attempt in range(3):
        try:
            resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=15)
            if resp.status_code == 429:
                wait = resp.json().get("retry_after", 5) + 0.5
                log.warning(f"Discord 429 rate limited, waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            if resp.status_code in (200, 204):
                return True
            else:
                log.error(f"Discord send failed: HTTP {resp.status_code} - {resp.text[:300]}")
                return False
        except Exception as e:
            log.error(f"Discord send error (attempt {attempt+1}): {e}")
            time.sleep(2)
    log.error(f"Discord send failed after 3 attempts")
    return False

def make_id(item, prefix=""):
    raw = f"{prefix}_{item.get('docket_id','')}_{item.get('dateFiled','')}_{item.get('caseName',item.get('case_name',''))}"
    return hashlib.md5(raw.encode()).hexdigest()

def format_tickers(matches):
    """Format ticker display string, handling both public and private companies."""
    parts = []
    for name, ticker in matches:
        if ticker.startswith("🔥"):
            parts.append(f"**{name}** (Private)")
        elif ticker.startswith("🏛️"):
            parts.append(f"**{name}** (Govt)")
        else:
            parts.append(f"**${ticker}** ({name})")
    return " | ".join(parts) if parts else ""

def get_filing_url(item, search_type="o"):
    """Build the best available URL for a CL search result.
    Tries absolute_url first, then constructs from IDs, then falls back to Google search."""
    # 1. Direct absolute_url from CL
    if item.get("absolute_url"):
        url = item["absolute_url"]
        if not url.startswith("http"):
            url = f"{CL_BASE}{url}"
        return url
    # 2. Construct from docket_id (for docket searches)
    if search_type == "d" and item.get("docket_id"):
        slug = re.sub(r'[^a-z0-9]+', '-', (item.get("caseName","") or item.get("case_name","")).lower()).strip('-')[:60]
        return f"{CL_BASE}/docket/{item['docket_id']}/{slug}/"
    # 3. Construct from cluster_id or id (for opinion searches)
    if item.get("cluster_id"):
        return f"{CL_BASE}/opinion/{item['cluster_id']}/"
    if item.get("id") and search_type == "o":
        return f"{CL_BASE}/opinion/{item['id']}/"
    # 4. Fallback: Google search for the case name
    cn = item.get("caseName","") or item.get("case_name","")
    if cn:
        q = requests.utils.quote(f'{cn} court filing')
        return f"https://www.google.com/search?q={q}"
    return ""

def extract_pdf_text(pdf_url, max_pages=10, max_chars=12000):
    """Download a PDF and extract text. Tries pypdf, pdfplumber, then raw bytes.
    Returns extracted text or empty string on failure."""
    try:
        resp = requests.get(pdf_url, timeout=25, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)"
        })
        if resp.status_code != 200 or len(resp.content) < 500:
            log.debug(f"PDF download failed: {resp.status_code} ({len(resp.content)} bytes)")
            return ""
        pdf_bytes = resp.content
    except Exception as e:
        log.debug(f"PDF fetch error: {e}")
        return ""

    import io
    text = ""

    # Method 1: pypdf (most commonly available)
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for i, page in enumerate(reader.pages[:max_pages]):
            pt = page.extract_text() or ""
            pages.append(pt)
        text = "\n".join(pages).strip()
        if len(text) > 100:
            log.debug(f"PDF extracted via pypdf: {len(text)} chars from {len(reader.pages)} pages")
            return text[:max_chars]
    except Exception as e:
        log.debug(f"pypdf extraction failed: {e}")

    # Method 2: pdfplumber (better for complex layouts)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages[:max_pages]]
            text = "\n".join(pages).strip()
        if len(text) > 100:
            log.debug(f"PDF extracted via pdfplumber: {len(text)} chars")
            return text[:max_chars]
    except Exception as e:
        log.debug(f"pdfplumber extraction failed: {e}")

    # Method 3: Raw text extraction from PDF bytes (last resort)
    try:
        raw = pdf_bytes.decode('latin-1', errors='ignore')
        # Extract text between BT/ET markers (PDF text objects)
        fragments = re.findall(r'\(([^)]{3,})\)', raw)
        text = " ".join(f for f in fragments if any(c.isalpha() for c in f))[:max_chars]
        if len(text) > 200:
            log.debug(f"PDF extracted via raw bytes: {len(text)} chars")
            return text
    except Exception as e:
        log.debug(f"Raw PDF extraction failed: {e}")

    return ""

# ── Check Functions ──

def fetch_cl_opinion_text(item):
    """Fetch full opinion text from CourtListener API.
    Search results only return snippets. This fetches the actual opinion text
    by hitting the opinions or clusters endpoint."""
    # Try snippet first as fallback
    snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()

    # Method 1: Fetch the opinion directly by ID
    opinion_id = item.get("id","") or item.get("opinion_id","")
    if opinion_id and rate_limiter.remaining() > 200:
        od = cl_request(f"{CL_BASE}/api/rest/v4/opinions/{opinion_id}/",
            params={"fields":"id,plain_text,html,download_url"}, timeout=15)
        if od:
            if od.get("plain_text") and len(od["plain_text"]) > 200:
                log.info(f"  Fetched opinion text: {len(od['plain_text'])} chars")
                return od["plain_text"][:12000]
            if od.get("html") and len(od["html"]) > 200:
                text = re.sub(r'<[^>]+>','',od["html"])
                log.info(f"  Fetched opinion HTML: {len(text)} chars")
                return text[:12000]
            # If opinion has a download URL (PDF), note it but don't download
            # (CL PDFs require PACER access usually)

    # Method 2: Fetch the cluster for syllabus/summary
    cid = item.get("cluster_id","")
    if cid and rate_limiter.remaining() > 200:
        cd = cl_request(f"{CL_BASE}/api/rest/v4/clusters/{cid}/",
            params={"fields":"id,case_name,syllabus,judges,date_filed,sub_opinions"}, timeout=15)
        if cd:
            if cd.get("syllabus") and len(cd["syllabus"]) > 100:
                log.info(f"  Fetched cluster syllabus: {len(cd['syllabus'])} chars")
                return cd["syllabus"][:12000]
            # Try to get sub_opinions and fetch their text
            sub_ops = cd.get("sub_opinions", [])
            if sub_ops and rate_limiter.remaining() > 200:
                for sub_url in sub_ops[:2]:  # Try first 2 sub-opinions
                    if isinstance(sub_url, str) and "/opinions/" in sub_url:
                        sod = cl_request(sub_url,
                            params={"fields":"id,plain_text,html"}, timeout=15)
                        if sod:
                            if sod.get("plain_text") and len(sod["plain_text"]) > 200:
                                log.info(f"  Fetched sub-opinion text: {len(sod['plain_text'])} chars")
                                return sod["plain_text"][:12000]
                            if sod.get("html") and len(sod["html"]) > 200:
                                text = re.sub(r'<[^>]+>','',sod["html"])
                                return text[:12000]

    # Fallback: just the snippet
    return snippet

def check_federal_filings(seen):
    """Search for recent federal court OPINIONS (rulings/decisions) involving public companies.
    Uses type=o to get opinions only, not all filings."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={
        "type":"o","order_by":"dateFiled desc","filed_after":yesterday,
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
        log.info(f"FEDERAL OPINION: {cn[:80]}")
        details = []
        for k,l in [("court","Court"),("dateFiled","Filed"),("citation","Citation"),("suitNature","Nature")]:
            if item.get(k): details.append(f"**{l}:** {item[k]}")
        # Fetch full opinion text from CL API
        opinion_text = fetch_cl_opinion_text(item)
        stxt = f"Court Opinion in: {cn}\n\n{opinion_text}"
        summary = summarize_with_deepseek(stxt, "federal court opinion") if len(opinion_text) > 50 else "Limited details available."
        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "o")
        embed = {"title":f"🚨 Federal Opinion: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"📊 Tickers","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} API calls left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if details: embed["fields"].append({"name":"📋 Details","value":"\n".join(details[:5]),"inline":False})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Full Opinion]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_federal_company(seen, idx):
    """Search for opinions mentioning specific companies. Prioritizes major companies,
    then rotates through the broader SEC database, skipping ETFs/funds/trusts."""
    # Priority list: companies most likely to appear in market-moving litigation
    PRIORITY_COMPANIES = [
        "Apple","Google","Microsoft","Amazon","Meta","Tesla","Nvidia","Intel","AMD","Broadcom",
        "Pfizer","Johnson Johnson","Eli Lilly","Merck","AbbVie","Amgen","Gilead","Regeneron","Moderna","Vertex",
        "JPMorgan","Goldman Sachs","Bank of America","Wells Fargo","Morgan Stanley","Citigroup","BlackRock",
        "ExxonMobil","Chevron","ConocoPhillips","Boeing","Lockheed Martin","Raytheon","Northrop Grumman",
        "Disney","Comcast","Netflix","Warner Bros","Paramount",
        "Walmart","Costco","Target","Home Depot","Kroger",
        "UnitedHealth","CVS Health","Humana","Cigna","Anthem",
        "Visa","Mastercard","American Express","PayPal","Coinbase","Robinhood",
        "AT&T","Verizon","T-Mobile","Qualcomm","Texas Instruments","Micron",
        "IBM","Cisco","Oracle","Salesforce","Adobe","ServiceNow",
        "Caterpillar","Deere","Honeywell","General Electric","3M",
        "FedEx","UPS","Union Pacific","Ford","General Motors",
        "Procter Gamble","Coca-Cola","PepsiCo","Nike","Starbucks","McDonald's",
        "CrowdStrike","Palantir","Snowflake","Shopify","Spotify","Uber","Airbnb",
        "Berkshire Hathaway","MicroStrategy","Affirm","SoFi","Block","Square",
        "Corcept","Teva","Samsung","Biogen","AstraZeneca","Novartis","Sanofi","Novo Nordisk",
        "Arm Holdings","Arista Networks","Palo Alto Networks","Fortinet","Workday",
        "Jazz Pharmaceuticals","BioMarin","Alnylam","Sarepta","Illumina","Dexcom",
        "Delta Air Lines","United Airlines","American Airlines","Southwest Airlines",
        "Snap","Pinterest","Reddit","DoorDash","Rivian","Lucid",
    ]

    # Filter out garbage from SEC database: ETFs, funds, trusts, series, warrants
    SKIP_PATTERNS = re.compile(
        r'(trust|fund|etf|series|warrant|preferred|right|note|bond|debenture|'
        r'municipal|income\s+trust|capital\s+trust|floating|variable|'
        r'strats|eaton\s+vance|calamos|nuveen|blackrock\s+muni|pimco|'
        r'rivernorth|doubleline|closed.end|convertible|perpetual)',
        re.IGNORECASE)

    # First cycle through priority companies, then the broader SEC list
    if idx < len(PRIORITY_COMPANIES):
        co = PRIORITY_COMPANIES[idx % len(PRIORITY_COMPANIES)]
    else:
        # Get clean SEC names, skip funds/trusts/ETFs
        sec_names = sorted(set(PUBLIC_COMPANIES.keys()), key=len, reverse=False)
        searchable = [n for n in sec_names
            if len(n) >= 5 and len(n) <= 40  # Skip super long fund names
            and not SKIP_PATTERNS.search(n)
            and '/' not in n and '&' not in n  # Skip names with special chars that break CL
        ]
        if not searchable:
            return 0
        adjusted = (idx - len(PRIORITY_COMPANIES))
        co = searchable[adjusted % len(searchable)]

    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")

    # Search for OPINIONS (type=o) mentioning this company
    data = cl_request(CL_SEARCH_URL, params={"q":co,"type":"o","order_by":"dateFiled desc","filed_after":yesterday})
    if not data: return 0
    alerts = 0
    for item in data.get("results",[]):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "comp_op")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        if not matches: continue
        # Verify the company we searched for is actually IN the case name,
        # not just mentioned somewhere in the opinion text
        if co.lower() not in cn.lower():
            # Check if any ticker from our search maps to one in the case
            search_ticker = PUBLIC_COMPANIES.get(co, "")
            case_tickers = [t for _, t in matches]
            if search_ticker not in case_tickers:
                continue
        log.info(f"OPINION [{co}]: {cn[:80]}")
        # Fetch full opinion text from CL API
        opinion_text = fetch_cl_opinion_text(item)
        summary = summarize_with_deepseek(f"Court Opinion: {cn}\n\n{opinion_text}", "court opinion")
        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "o")
        embed = {"title":f"🚨 Opinion: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"📊 Tickers","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if item.get("court"): embed["fields"].append({"name":"🏛️ Court","value":item["court"],"inline":True})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Full Opinion]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_high_impact_filings(seen):
    """Search for high-impact NEW filings: SEC/DOJ/FTC enforcement, class action certs, major settlements.
    Uses type=r but filtered by keywords that indicate truly market-moving events.
    Filters out individual personal lawsuits (SMITH v. BIGCORP pattern)."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    # Rotate through high-impact search terms — these are institutional/govt actions, not personal suits
    queries = [
        '"Securities and Exchange Commission" enforcement',
        '"Department of Justice" antitrust',
        '"Federal Trade Commission" complaint',
        '"class action" "class certification"',
        "securities fraud settlement approval",
        '"preliminary injunction" patent infringement',
        '"consent decree" antitrust',
        "qui tam whistleblower fraud",
    ]
    cycle_idx = int(time.time() / 70) % len(queries)
    q = queries[cycle_idx]

    data = cl_request(CL_SEARCH_URL, params={
        "q": q, "type": "r", "order_by": "dateFiled desc", "filed_after": yesterday
    })
    if not data: return 0
    alerts = 0
    for item in data.get("results", [])[:10]:
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "himp")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        if not matches: continue
        # Skip individual-vs-company pattern unless it's a govt agency or VIP plaintiff
        # "SMITH v. PFIZER" = personal suit (skip)
        # "SEC v. COINBASE" = enforcement action (keep)
        # "TRUMP v. JPMORGAN" = VIP plaintiff, market-moving (keep)
        # "In re PFIZER Securities Litigation" = class action (keep)
        plaintiff = cn.split(" v. ")[0].strip() if " v. " in cn else cn.split(" v ")[0].strip() if " v " in cn else ""
        govt_agencies = ["SEC", "Securities and Exchange", "United States", "Department of Justice",
                         "DOJ", "FTC", "Federal Trade", "CFPB", "Consumer Financial", "EPA",
                         "NLRB", "EEOC", "State of", "Commonwealth", "People of", "Attorney General"]
        class_indicators = ["In re ", "In Re ", "IN RE ", "MDL", "Consolidated", "Securities Litigation",
                            "Class Action", "Antitrust Litigation", "Products Liability"]
        # High-profile individuals whose lawsuits move markets
        # NOTE: Only names unlikely to be random plaintiffs. Skip common surnames
        # like "Warren", "James", "Cook", "Gates", "Khan" — too many false positives.
        vip_plaintiffs = [
            "trump", "musk", "bezos", "zuckerberg", "buffett", "dimon", "icahn", "ackman",
            "soros", "dalio", "thiel", "ellison", "altman", "chesky", "kalanick", "neumann",
            "bankman-fried", "bankman fried",
            "pelosi", "desantis", "newsom", "paxton", "gensler",
            "elon musk", "carl icahn", "bill ackman", "george soros",
            "donald trump", "nancy pelosi", "ron desantis", "greg abbott",
            "letitia james", "elizabeth warren", "ken paxton", "gary gensler", "lina khan",
        ]
        is_govt = any(ga.lower() in plaintiff.lower() for ga in govt_agencies)
        is_class = any(ci.lower() in cn.lower() for ci in class_indicators)
        is_vip = any(vip.lower() in plaintiff.lower() for vip in vip_plaintiffs)
        if not is_govt and not is_class and not is_vip:
            # Individual lawsuit — skip unless plaintiff name looks institutional
            # Simple heuristic: personal names are short (< 20 chars), no commas
            if len(plaintiff) < 25 and "," not in plaintiff and "Inc" not in plaintiff:
                continue

        log.info(f"HIGH-IMPACT [{q[:25]}]: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"High-impact filing ({q}):\nCase: {cn}\n{snippet}", "high-impact court filing")
        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "r")
        # Clean query label for display
        q_label = q.replace('"', '').title()[:50]
        embed = {"title":f"🚨 High-Impact Filing: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[
                {"name":"📊 Tickers","value":tstr,"inline":False},
                {"name":"📌 Category","value":q_label,"inline":True},
            ],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if item.get("court"): embed["fields"].append({"name":"🏛️ Court","value":item["court"],"inline":True})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Filing]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_new_dockets(seen, idx):
    """Search for NEW lawsuits/cases filed involving major companies.
    Uses type=d (dockets) to catch new complaints, not just opinions.
    This catches: Kalshi suing Utah, new SEC enforcement, new patent suits, etc."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")

    # Combine priority public companies + buzzy private companies for docket searches
    DOCKET_WATCH_NAMES = [
        # Top public companies most likely to be in market-moving lawsuits
        "Apple","Google","Meta","Microsoft","Amazon","Tesla","Nvidia","Intel","AMD",
        "Pfizer","Eli Lilly","Merck","Johnson Johnson","AbbVie","Moderna",
        "JPMorgan","Goldman Sachs","Bank of America","Wells Fargo","Citigroup",
        "Boeing","Lockheed Martin","Disney","Comcast","Netflix",
        "Walmart","Costco","Target","Home Depot",
        "Visa","Mastercard","PayPal","Coinbase","Robinhood",
        "Uber","Airbnb","DoorDash","Snap","Reddit",
        "CrowdStrike","Palantir","Snowflake","Shopify","Salesforce",
        "Exxon","Chevron","Ford","General Motors",
        "UnitedHealth","CVS Health",
        # Buzzy private companies that move markets
        "OpenAI","xAI","Anthropic","SpaceX","Stripe","Databricks",
        "ByteDance","TikTok","Kalshi","Polymarket",
        "Binance","Ripple","Kraken","Tether",
        "Anduril","CoreWeave","Cerebras","Perplexity",
        "Epic Games","Discord","Figma",
    ]

    co = DOCKET_WATCH_NAMES[idx % len(DOCKET_WATCH_NAMES)]
    data = cl_request(CL_SEARCH_URL, params={
        "q": co, "type": "d", "order_by": "dateFiled desc", "filed_after": yesterday
    })
    if not data: return 0
    alerts = 0
    for item in data.get("results", [])[:5]:
        cn = item.get("caseName","") or item.get("case_name","")
        fid = f"dkt_{hashlib.md5((cn + str(item.get('dateFiled',''))).encode()).hexdigest()}"
        if fid in seen: continue
        mark_seen(seen, fid)
        # Must match our company database (public or private)
        matches = match_public_company(cn)
        if not matches: continue
        # Verify the searched company is actually in the case name
        if co.lower() not in cn.lower():
            search_ticker = PUBLIC_COMPANIES.get(co, "")
            case_tickers = [t for _, t in matches]
            if search_ticker not in case_tickers:
                continue
        log.info(f"NEW DOCKET [{co}]: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()

        # Materiality filter: skip individual/minor lawsuits
        is_material, reason = check_materiality(
            cn, court=item.get("court",""), snippet=snippet, filing_type="new lawsuit"
        )
        if not is_material:
            log.info(f"  SKIPPED (not material): {reason[:80]}")
            continue

        summary = summarize_with_deepseek(
            f"New lawsuit/case filed:\nCase: {cn}\nCourt: {item.get('court','')}\nFiled: {item.get('dateFiled','')}\n\n{snippet}",
            "new lawsuit filing")
        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "d")
        embed = {"title":f"⚡ New Case Filed: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"📊 Companies","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if item.get("court"): embed["fields"].append({"name":"🏛️ Court","value":item["court"],"inline":True})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Case]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_buzzy_filings(seen, idx):
    """Search for opinions AND significant filings involving buzzy private companies.
    These companies aren't public but their cases move markets (xAI v OpenAI, etc.)."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    buzzy_names = [n for n in BUZZY_COMPANIES.keys() if len(n) >= 3 and n not in ("SEC","FTC","DOJ","CFTC")]
    if not buzzy_names: return 0
    co = buzzy_names[idx % len(buzzy_names)]

    # Search opinions first (rulings/dismissals)
    data = cl_request(CL_SEARCH_URL, params={
        "q": co, "type": "o", "order_by": "dateFiled desc", "filed_after": yesterday
    })
    if not data: return 0
    alerts = 0
    for item in data.get("results", [])[:5]:
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "buzzy_op")
        if fid in seen: continue
        mark_seen(seen, fid)
        if co.lower() not in cn.lower(): continue  # Must be in case name
        log.info(f"BUZZY OPINION [{co}]: {cn[:80]}")
        opinion_text = fetch_cl_opinion_text(item)
        summary = summarize_with_deepseek(f"Court Opinion: {cn}\n\n{opinion_text}", "court opinion involving notable company")
        matches = match_public_company(cn)
        tstr = format_tickers(matches) if matches else f"**{co}** (Private)"
        doc_url = get_filing_url(item, "o")
        embed = {"title":f"🚨 Opinion: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"📊 Companies","value":tstr,"inline":False}],
            "footer":{"text":f"Court Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if item.get("court"): embed["fields"].append({"name":"🏛️ Court","value":item["court"],"inline":True})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Full Opinion]({doc_url})","inline":False})
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
        # Fetch full opinion text
        otxt = fetch_cl_opinion_text(item)
        summary = summarize_with_deepseek(f"Supreme Court: {cn}\n\n{otxt}", "Supreme Court opinion")
        doc_url = get_filing_url(item, "o")
        embed = {"title":f"🏛️ SCOTUS: {cn[:200]}","url":doc_url,"color":0xFF0000,"fields":[],
            "footer":{"text":f"SCOTUS Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if matches:
            tstr = format_tickers(matches)
            embed["fields"].append({"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False})
        if item.get("dateFiled"): embed["fields"].append({"name":"📅 Filed","value":item["dateFiled"],"inline":True})
        if item.get("status"): embed["fields"].append({"name":"📋 Status","value":item["status"],"inline":True})
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Full Opinion]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_scotus_docket(seen):
    """Check SCOTUS docket entries for company-related activity."""
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
        if not matches: continue  # Docket is noisy — only alert for company matches
        log.info(f"SCOTUS DOCKET: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"SCOTUS Docket: {cn}\n\n{snippet}", "Supreme Court docket entry")
        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "d")
        embed = {"title":f"🏛️ SCOTUS Docket: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False}],
            "footer":{"text":f"SCOTUS Monitor | {rate_limiter.remaining()} left"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
        if doc_url: embed["fields"].append({"name":"📄 Document","value":f"[View Docket Entry]({doc_url})","inline":False})
        embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
        send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
        alerts += 1; time.sleep(0.5)
    return alerts

def check_scotus_website(seen):
    """Scrape supremecourt.gov for new opinions. Sends ALL opinions — SCOTUS only issues
    ~70 per term and they're all significant. Company matches get highlighted."""
    alerts = 0
    try:
        # SCOTUS terms run Oct-June. Term number = year the term started
        # e.g. Oct 2025 - Jun 2026 = term "25"
        now = datetime.now(EST)
        term_year = now.year if now.month >= 10 else now.year - 1
        term_str = str(term_year)[-2:]  # "25" for 2025-2026 term
        scotus_url = f"https://www.supremecourt.gov/opinions/slipopinion/{term_str}"

        resp = requests.get(scotus_url,
            timeout=15, headers={"User-Agent":"CourtFilingMonitor/1.0"})
        if not resp.ok:
            log.warning(f"SCOTUS.gov returned {resp.status_code} for term {term_str}")
            return 0
        from html.parser import HTMLParser

        # Also extract PDF links from the page
        import re as _re
        pdf_links = _re.findall(r'href="(/opinions/\d+pdf/[^"]+\.pdf)"', resp.text, _re.IGNORECASE)

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

        # Filter to valid opinion rows only — must contain a docket number like "24-1287"
        docket_pattern = re.compile(r'\d{2,4}-\d{1,5}')
        valid_rows = [(i, row) for i, row in enumerate(p.rows) if any(docket_pattern.search(cell) for cell in row)]

        # On first run (no scotusgov_ entries in seen), mark all existing as seen without alerting
        scotus_seen_count = sum(1 for k in seen if k.startswith("scotusgov_"))
        is_first_run = scotus_seen_count == 0

        for i, row in valid_rows[-20:]:  # Check last 20 valid rows
            rt = " ".join(row)
            fid = f"scotusgov_{hashlib.md5(rt.encode()).hexdigest()}"
            if fid in seen: continue
            mark_seen(seen, fid)

            # On first run, just seed the seen dict — don't spam all historical opinions
            if is_first_run:
                log.info(f"SCOTUS.GOV [seed]: {rt[:80]}")
                continue

            log.info(f"SCOTUS.GOV: {rt[:80]}")
            matches = match_public_company(rt)

            # Try to find the PDF link for this opinion by matching docket number
            pdf_url = ""
            docket_match = docket_pattern.search(rt)
            if docket_match and pdf_links:
                docket_num = docket_match.group()
                for pl in pdf_links:
                    if docket_num.replace("-", "") in pl.replace("-", "") or docket_num in pl:
                        pdf_url = f"https://www.supremecourt.gov{pl}"
                        break

            # Download PDF for AI summary if available
            summary_text = f"Supreme Court opinion from supremecourt.gov:\n{rt}"
            if pdf_url:
                try:
                    pdf_resp = requests.get(pdf_url, timeout=25,
                        headers={"User-Agent": "CourtFilingMonitor/1.0"})
                    if pdf_resp.ok and len(pdf_resp.content) < 5000000:
                        import io
                        try:
                            from pypdf import PdfReader
                        except ImportError:
                            from PyPDF2 import PdfReader
                        reader = PdfReader(io.BytesIO(pdf_resp.content))
                        pages_text = []
                        for page in reader.pages[:12]:
                            pt = page.extract_text()
                            if pt:
                                pages_text.append(pt)
                        full_text = "\n".join(pages_text)
                        if len(full_text) > 300:
                            summary_text = f"Supreme Court Opinion:\n{rt}\n\nFull text:\n{full_text[:12000]}"
                            log.info(f"  SCOTUS PDF extracted: {len(full_text)} chars from {len(pages_text)} pages")
                except Exception as e:
                    log.debug(f"  SCOTUS PDF extraction failed: {e}")

            summary = summarize_with_deepseek(summary_text, "Supreme Court opinion")

            embed = {"title":f"🏛️ SCOTUS Opinion: {rt[:200]}",
                "url": pdf_url or scotus_url, "color":0xFF0000,
                "fields":[],
                "footer":{"text":"SCOTUS Monitor | supremecourt.gov"},"timestamp":datetime.now(tz=pytz.utc).isoformat()}
            if matches:
                tstr = format_tickers(matches)
                embed["fields"].append({"name":"🚨 PUBLIC COMPANY","value":tstr,"inline":False})
            doc_link = pdf_url or scotus_url
            embed["fields"].append({"name":"📄 Document","value":f"[View Opinion]({doc_link})","inline":False})
            embed["fields"].append({"name":"🤖 AI Summary","value":summary[:1000],"inline":False})
            send_discord(DISCORD_WEBHOOK_SCOTUS, [embed])
            alerts += 1; time.sleep(0.5)
    except Exception as e:
        log.error(f"SCOTUS.gov failed: {e}")
    return alerts

def check_cafc_website(seen):
    """Scrape CAFC opinions table for new filings involving public companies."""
    alerts = 0
    # The opinions TABLE page has direct PDF links; the blog page only has HTML post links
    CAFC_URL = "https://www.cafc.uscourts.gov/home/case-information/opinions-orders/"
    CAFC_BASE = "https://www.cafc.uscourts.gov"
    try:
        resp = requests.get(CAFC_URL, timeout=20,
            headers={"User-Agent": "CourtFilingMonitor/1.0"})
        if not resp.ok:
            log.warning(f"CAFC table page returned {resp.status_code}")
            return 0

        import re as _re

        # PDF links from the opinions table (primary source)
        # Match: <a href="/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf">CASE NAME [TYPE]</a>
        pdf_pattern = _re.compile(
            r'<a\s+href="(/opinions-orders/[^"]+\.pdf)"[^>]*>\s*([^<]+?)\s*</a>', _re.IGNORECASE)

        entries = []
        seen_case_nums = set()

        for match in pdf_pattern.finditer(resp.text):
            path, title = match.group(1), match.group(2).strip()
            pdf_url = f"{CAFC_BASE}{path}"
            if title and len(title) > 5:
                cn_match = _re.search(r'(\d{2}-\d{3,5})', path)
                case_num = cn_match.group(1) if cn_match else None
                if case_num and case_num not in seen_case_nums:
                    seen_case_nums.add(case_num)
                    entries.append((pdf_url, title, pdf_url))
                elif not case_num:
                    entries.append((pdf_url, title, pdf_url))

        if entries:
            log.debug(f"CAFC table: found {len(entries)} PDF entries")
        else:
            # Fallback: try blog page for HTML post links
            log.debug("CAFC table had no PDF entries, trying blog page")
            try:
                blog_resp = requests.get("https://www.cafc.uscourts.gov/category/opinion-order/",
                    timeout=15, headers={"User-Agent": "CourtFilingMonitor/1.0"})
                if blog_resp.ok:
                    post_pattern = _re.compile(
                        r'<a\s+href="(https?://www\.cafc\.uscourts\.gov/\d{2}-\d{2}-\d{4}[^"]*)"[^>]*>\s*'
                        r'([^<]+?)\s*</a>', _re.IGNORECASE)
                    for match in post_pattern.finditer(blog_resp.text):
                        url, title = match.group(1), match.group(2).strip()
                        if title and len(title) > 5:
                            entries.append((url, title, None))
            except:
                pass

        if not entries:
            return 0

        for url, title, pdf_url in entries[:20]:  # Check latest 20
            fid = f"cafc_{hashlib.md5((url + title).encode()).hexdigest()}"
            if fid in seen:
                continue
            mark_seen(seen, fid)

            # Extract case name (strip [OPINION], [ORDER], [RULE 36 JUDGMENT] suffix)
            case_name = _re.sub(r'\s*\[(OPINION|ORDER|RULE\s*36\s*JUDGMENT|ERRATA)\].*$', '', title, flags=_re.IGNORECASE).strip()

            matches = match_public_company(case_name)
            if not matches:
                continue

            # Determine doc type for display
            doc_type = "Opinion"
            if "[ORDER]" in title.upper():
                doc_type = "Order"
            elif "RULE 36" in title.upper():
                doc_type = "Rule 36 Judgment"

            log.info(f"CAFC: {case_name[:80]}")

            # Step 1: Find the PDF URL if we don't have one
            if not pdf_url and not url.endswith('.pdf'):
                # Fetch the blog post page and look for the PDF link inside
                try:
                    page_resp = requests.get(url, timeout=15,
                        headers={"User-Agent": "CourtFilingMonitor/1.0"})
                    if page_resp.ok:
                        pdf_match = _re.search(
                            r'href="((?:https?://www\.cafc\.uscourts\.gov)?/opinions-orders/[^"]+\.pdf)"',
                            page_resp.text, _re.IGNORECASE)
                        if pdf_match:
                            found = pdf_match.group(1)
                            pdf_url = found if found.startswith('http') else f"{CAFC_BASE}{found}"
                            log.info(f"  Found PDF link: {pdf_url.split('/')[-1]}")
                except Exception as e:
                    log.debug(f"  Blog page fetch failed: {e}")
            elif url.endswith('.pdf'):
                pdf_url = url

            # Step 2: Download PDF and extract text
            summary_text = f"Federal Circuit (CAFC) {doc_type}:\n{case_name}\nURL: {url}\n\nWARNING: Full text unavailable. Do NOT guess the outcome."
            if pdf_url:
                try:
                    pdf_resp = requests.get(pdf_url, timeout=25,
                        headers={"User-Agent": "CourtFilingMonitor/1.0"})
                    if pdf_resp.ok and len(pdf_resp.content) < 2000000:  # <2MB
                        import io
                        try:
                            from pypdf import PdfReader
                        except ImportError:
                            from PyPDF2 import PdfReader
                        reader = PdfReader(io.BytesIO(pdf_resp.content))
                        pages_text = []
                        # Extract first 10 pages (enough for background + ruling + discussion)
                        for page in reader.pages[:10]:
                            pt = page.extract_text()
                            if pt:
                                pages_text.append(pt)
                        full_text = "\n".join(pages_text)
                        if len(full_text) > 300:
                            summary_text = f"CAFC {doc_type}:\n{case_name}\n\nFull text (first pages):\n{full_text[:10000]}"
                            log.info(f"  PDF extracted: {len(full_text)} chars from {len(pages_text)} pages")
                        else:
                            log.warning(f"  PDF extraction got only {len(full_text)} chars")
                    else:
                        log.warning(f"  PDF download failed: HTTP {pdf_resp.status_code}, size {len(pdf_resp.content)}")
                except Exception as e:
                    log.warning(f"  PDF extraction failed: {e}")
            else:
                log.warning(f"  No PDF URL found for {case_name[:50]}")

            summary = summarize_with_deepseek(summary_text, "Federal Circuit opinion")
            tstr = format_tickers(matches)

            embed = {
                "title": f"🚨 CAFC {doc_type}: {case_name[:200]}",
                "url": url,
                "color": 0xFF0000,
                "fields": [
                    {"name": "📊 Tickers", "value": tstr, "inline": False},
                    {"name": "🏛️ Court", "value": "U.S. Court of Appeals for the Federal Circuit", "inline": False},
                    {"name": "📄 Document", "value": f"[View Full {doc_type}]({url})", "inline": False},
                    {"name": "🤖 AI Summary", "value": summary[:1000], "inline": False},
                ],
                "footer": {"text": "CAFC Monitor | cafc.uscourts.gov"},
                "timestamp": datetime.now(tz=pytz.utc).isoformat()
            }
            send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
            alerts += 1
            time.sleep(0.5)

    except Exception as e:
        log.error(f"CAFC scraper failed: {e}")
    return alerts

# ── State Court Ruling Scrapers ──
# These only fetch RULINGS (opinions/orders), not new filings.
# State court opinions pages naturally filter for this.

def check_delaware_courts(seen):
    """Scrape Delaware Court of Chancery and Supreme Court opinions.
    Delaware Chancery is THE most important state court for corporate litigation:
    M&A disputes, shareholder suits, corporate governance, fiduciary duty cases.
    ~300 Chancery + ~100 Supreme Court opinions per year."""
    alerts = 0
    for court_slug, court_label in [("court+of+chancery", "Chancery"), ("supreme+court", "Supreme")]:
        url = f"https://courts.delaware.gov/opinions/index.aspx?ag={court_slug}"
        try:
            resp = requests.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)"
            })
            if resp.status_code != 200:
                log.debug(f"Delaware {court_label} returned {resp.status_code}")
                continue
            html = resp.text
        except Exception as e:
            log.debug(f"Delaware {court_label} fetch failed: {e}")
            continue

        # Parse the opinions table - rows contain: Caption, Date, File Number, Court, Type, Judge, Description
        # Pattern: <a href="/Opinions/Download.aspx?id=XXXXX">CASE NAME</a> ... date ... description
        rows = re.findall(
            r'<a\s+href="(/Opinions/Download\.aspx\?id=\d+)"[^>]*>([^<]+)</a>.*?'
            r'(\d{2}/\d{2}/\d{4})',
            html, re.DOTALL
        )

        today = datetime.now(EST).date()
        for pdf_path, case_name, date_str in rows[:20]:
            try:
                filed = datetime.strptime(date_str, "%m/%d/%Y").date()
                if (today - filed).days > 2:  # Only last 2 days
                    continue
            except:
                continue

            fid = f"de_{hashlib.md5((case_name + date_str).encode()).hexdigest()}"
            if fid in seen: continue
            mark_seen(seen, fid)

            matches = match_public_company(case_name)
            if not matches: continue

            log.info(f"DE {court_label}: {case_name[:80]}")
            pdf_url = f"https://courts.delaware.gov{pdf_path}"

            # Fetch opinion PDF text for AI summary
            opinion_text = extract_pdf_text(pdf_url)
            if opinion_text:
                log.info(f"  DE PDF text: {len(opinion_text)} chars extracted")
            else:
                opinion_text = f"Delaware {court_label} opinion in case: {case_name}. Filed {date_str}. No PDF text could be extracted."

            summary = summarize_with_deepseek(
                f"Delaware {court_label} Court Opinion\nCase: {case_name}\nFiled: {date_str}\n\n{opinion_text}",
                f"Delaware {court_label} opinion")
            tstr = format_tickers(matches)

            embed = {"title": f"🏛️ DE {court_label}: {case_name[:200]}", "url": pdf_url, "color": 0xFF4500,
                "fields": [{"name": "📊 Companies", "value": tstr, "inline": False}],
                "footer": {"text": f"Court Monitor | Delaware State Courts"},
                "timestamp": datetime.now(tz=pytz.utc).isoformat()}
            embed["fields"].append({"name": "🏛️ Court", "value": f"Delaware {court_label}", "inline": True})
            embed["fields"].append({"name": "📅 Filed", "value": date_str, "inline": True})
            embed["fields"].append({"name": "📄 Document", "value": f"[View Opinion (PDF)]({pdf_url})", "inline": False})
            embed["fields"].append({"name": "🤖 AI Summary", "value": summary[:1000], "inline": False})
            send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
            alerts += 1; time.sleep(0.5)

    return alerts

def check_california_courts(seen):
    """Scrape California Courts of Appeal and Supreme Court published opinions.
    courts.ca.gov/opinionsearch.htm provides recent published opinions.
    Covers big CA company rulings (tech, energy, environmental)."""
    url = "https://www.courts.ca.gov/cms/opinionsearch.htm"
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)"
        })
        if resp.status_code != 200:
            log.debug(f"CA Courts returned {resp.status_code}")
            return 0
        html = resp.text
    except Exception as e:
        log.debug(f"CA Courts fetch failed: {e}")
        return 0

    alerts = 0
    # CA opinions page has links to PDFs with case names
    # Pattern varies but typically: case name, date, court, link
    # Try to find opinion entries - the page structure uses tables or divs
    entries = re.findall(
        r'href="([^"]*\.(?:pdf|PDF|htm|aspx)[^"]*)"[^>]*>\s*([^<]{10,200})',
        html
    )

    today = datetime.now(EST).date()
    for link, case_name in entries[:30]:
        case_name = re.sub(r'<[^>]+>', '', case_name).strip()
        if not case_name or len(case_name) < 10: continue
        # Skip navigation links
        if any(x in case_name.lower() for x in ["search", "home", "help", "about", "menu", "login"]): continue

        fid = f"ca_{hashlib.md5(case_name.encode()).hexdigest()}"
        if fid in seen: continue
        mark_seen(seen, fid)

        matches = match_public_company(case_name)
        if not matches: continue

        if not link.startswith("http"):
            link = f"https://www.courts.ca.gov{link}" if link.startswith("/") else f"https://www.courts.ca.gov/{link}"

        log.info(f"CA COURT: {case_name[:80]}")
        summary = summarize_with_deepseek(
            f"California state court opinion: {case_name}",
            "California court opinion")
        tstr = format_tickers(matches)

        embed = {"title": f"🏛️ CA Court: {case_name[:200]}", "url": link, "color": 0xFF4500,
            "fields": [{"name": "📊 Companies", "value": tstr, "inline": False}],
            "footer": {"text": "Court Monitor | California State Courts"},
            "timestamp": datetime.now(tz=pytz.utc).isoformat()}
        embed["fields"].append({"name": "🏛️ Court", "value": "California Appellate", "inline": True})
        embed["fields"].append({"name": "📄 Document", "value": f"[View Opinion]({link})", "inline": False})
        embed["fields"].append({"name": "🤖 AI Summary", "value": summary[:1000], "inline": False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)

    return alerts

def check_ny_courts(seen):
    """Scrape New York Court of Appeals and Appellate Division opinions.
    NY courts publish recent slip opinions online.
    Covers financial sector litigation, securities cases."""
    url = "https://www.nycourts.gov/ctapps/Decisions/New_Decisions.shtml"
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CourtMonitor/1.0)"
        })
        if resp.status_code != 200:
            log.debug(f"NY Courts returned {resp.status_code}")
            return 0
        html = resp.text
    except Exception as e:
        log.debug(f"NY Courts fetch failed: {e}")
        return 0

    alerts = 0
    # Parse opinion links from the page
    entries = re.findall(
        r'href="([^"]*\.(?:pdf|PDF|htm|html)[^"]*)"[^>]*>\s*([^<]{10,200})',
        html
    )

    for link, case_name in entries[:20]:
        case_name = re.sub(r'<[^>]+>', '', case_name).strip()
        if not case_name or len(case_name) < 10: continue
        if any(x in case_name.lower() for x in ["search", "home", "help", "archive"]): continue

        fid = f"ny_{hashlib.md5(case_name.encode()).hexdigest()}"
        if fid in seen: continue
        mark_seen(seen, fid)

        matches = match_public_company(case_name)
        if not matches: continue

        if not link.startswith("http"):
            link = f"https://www.nycourts.gov{link}" if link.startswith("/") else f"https://www.nycourts.gov/ctapps/Decisions/{link}"

        log.info(f"NY COURT: {case_name[:80]}")
        summary = summarize_with_deepseek(
            f"New York state court opinion: {case_name}",
            "New York court opinion")
        tstr = format_tickers(matches)

        embed = {"title": f"🏛️ NY Court: {case_name[:200]}", "url": link, "color": 0xFF4500,
            "fields": [{"name": "📊 Companies", "value": tstr, "inline": False}],
            "footer": {"text": "Court Monitor | New York State Courts"},
            "timestamp": datetime.now(tz=pytz.utc).isoformat()}
        embed["fields"].append({"name": "🏛️ Court", "value": "NY Court of Appeals", "inline": True})
        embed["fields"].append({"name": "📄 Document", "value": f"[View Opinion]({link})", "inline": False})
        embed["fields"].append({"name": "🤖 AI Summary", "value": summary[:1000], "inline": False})
        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1; time.sleep(0.5)

    return alerts

def check_state_courts(seen, idx):
    """Rotate through state court scrapers. Each call checks one system.
    Phase rotation: 0=Delaware, 1=California, 2=New York, then repeat."""
    sources = [check_delaware_courts, check_california_courts, check_ny_courts]
    fn = sources[idx % len(sources)]
    try:
        return fn(seen)
    except Exception as e:
        log.error(f"State court check failed: {e}")
        return 0

# ── Government Enforcement Actions (Court Filings) ──

# Search CourtListener for NEW dockets filed BY government agencies.
# Case names on PACER: "Securities and Exchange Commission v. Tesla Inc",
# "United States v. Google LLC", "Federal Trade Commission v. Meta Platforms", etc.
# These are the actual court filings — faster than waiting for press releases.

GOV_AGENCY_SEARCHES = [
    # (search_query, agency_label) — rotated one per cycle
    # Federal regulators
    ("Securities and Exchange Commission", "SEC"),
    ("Federal Trade Commission", "FTC"),
    ("Consumer Financial Protection Bureau", "CFPB"),
    ("Commodity Futures Trading Commission", "CFTC"),
    ("Environmental Protection Agency", "EPA"),
    ("Department of Justice", "DOJ"),
    ("National Labor Relations Board", "NLRB"),
    ("Department of Labor", "DOL"),
    # DOJ as "United States v." — most criminal/civil enforcement
    ("United States of America", "DOJ/USA"),
    # State attorneys general (file in federal court too)
    ("State of New York", "NY AG"),
    ("State of California", "CA AG"),
    ("State of Texas", "TX AG"),
    ("Commonwealth of Massachusetts", "MA AG"),
    ("People of the State of Illinois", "IL AG"),
    # Additional federal agencies
    ("Food and Drug Administration", "FDA"),
    ("Federal Energy Regulatory Commission", "FERC"),
    ("Office of the Comptroller", "OCC"),
    ("Federal Communications Commission", "FCC"),
    ("Federal Reserve", "Fed"),
]


def check_gov_enforcement(seen, idx):
    """Search CourtListener for NEW court filings by government agencies.
    Catches actual PACER filings — much faster than waiting for press releases.
    Example: 'SEC v. Coinbase' complaint filed in SDNY appears here before sec.gov."""

    agency_query, agency_label = GOV_AGENCY_SEARCHES[idx % len(GOV_AGENCY_SEARCHES)]
    yesterday = (datetime.now(EST) - timedelta(days=2)).strftime("%m/%d/%Y")

    # Search for new dockets with the agency name
    data = cl_request(CL_SEARCH_URL, params={
        "q": f'"{agency_query}"',
        "type": "d",
        "order_by": "dateFiled desc",
        "filed_after": yesterday,
    })
    if not data:
        return 0

    alerts = 0
    for item in data.get("results", [])[:8]:
        cn = item.get("caseName", "") or item.get("case_name", "")
        if not cn:
            continue

        fid = f"gov_{hashlib.md5((agency_label + cn[:100] + str(item.get('dateFiled', ''))).encode()).hexdigest()}"
        if fid in seen:
            continue
        mark_seen(seen, fid)

        # Verify the agency name is actually in the case name (not just snippet)
        if agency_query.lower() not in cn.lower():
            # For "United States of America", also accept "United States v."
            if agency_label == "DOJ/USA" and "united states" not in cn.lower():
                continue
            elif agency_label != "DOJ/USA":
                continue

        # Match the OTHER party (defendant) against our company database
        matches = match_public_company(cn)

        # Filter out false positives where match is from the agency name, not the defendant
        # e.g. "Exchange" matching in "Securities and Exchange Commission"
        agency_upper = agency_query.upper()
        matches = [(co, tk) for co, tk in matches if co.upper() not in agency_upper]

        if not matches:
            # Even without a direct ticker match, if a known company name is in the case,
            # check against buzzy companies
            buzzy_match = None
            for bname, btag in BUZZY_COMPANIES.items():
                if len(bname) >= 4 and bname.lower() in cn.lower():
                    buzzy_match = (bname, btag)
                    break
            if not buzzy_match:
                continue
            matches = [buzzy_match]

        log.info(f"GOV ENFORCEMENT [{agency_label}]: {cn[:100]}")

        # Get the filing snippet for context
        snippet = re.sub(r'<[^>]+>', '', item.get("snippet", "") or "").strip()

        # Build summary with available context
        summary_text = (
            f"Government Enforcement Action\n"
            f"Agency: {agency_label} ({agency_query})\n"
            f"Case: {cn}\n"
            f"Court: {item.get('court', 'Unknown')}\n"
            f"Filed: {item.get('dateFiled', 'Unknown')}\n\n"
            f"{snippet}"
        )
        summary = summarize_with_deepseek(summary_text, f"{agency_label} government enforcement filing")

        tstr = format_tickers(matches)
        doc_url = get_filing_url(item, "d")

        # Color coding by agency type
        color_map = {
            "SEC": 0x0A3161, "FTC": 0x003366, "DOJ": 0x002868, "DOJ/USA": 0x002868,
            "CFPB": 0x20AA3F, "CFTC": 0xB8860B, "EPA": 0x0071BC, "FDA": 0x0071BC,
            "NLRB": 0x8B0000, "DOL": 0x003366, "FERC": 0x003366, "OCC": 0x003366,
            "FCC": 0x003366, "Fed": 0x003366,
        }
        color = color_map.get(agency_label, 0xFF4500)

        embed = {
            "title": f"⚖️ {agency_label} Enforcement: {cn[:200]}",
            "url": doc_url,
            "color": color,
            "fields": [
                {"name": "📊 Companies", "value": tstr, "inline": False},
                {"name": "⚖️ Agency", "value": f"{agency_label} ({agency_query})", "inline": True},
            ],
            "footer": {"text": f"Court Monitor | Gov Enforcement | {rate_limiter.remaining()} left"},
            "timestamp": datetime.now(tz=pytz.utc).isoformat()
        }
        if item.get("court"):
            embed["fields"].append({"name": "🏛️ Court", "value": item["court"], "inline": True})
        if item.get("dateFiled"):
            embed["fields"].append({"name": "📅 Filed", "value": item["dateFiled"], "inline": True})
        if doc_url:
            embed["fields"].append({"name": "📄 Document", "value": f"[View Filing]({doc_url})", "inline": False})
        embed["fields"].append({"name": "🤖 AI Summary", "value": summary[:1000], "inline": False})

        send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
        alerts += 1
        time.sleep(0.5)

    return alerts

# ── Main Loop ──

def main():
    global PUBLIC_COMPANIES
    log.info("="*60)
    log.info("Court Filing Monitor Starting")
    log.info(f"  Check interval: {CHECK_INTERVAL}s (12 sources, each every {CHECK_INTERVAL*12}s)")
    log.info(f"  CL Token: {'SET' if COURTLISTENER_API_TOKEN else 'NOT SET - get one at courtlistener.com!'}")
    log.info(f"  Hours: 9AM-4PM EST, market days only")
    log.info("="*60)

    if not COURTLISTENER_API_TOKEN:
        log.warning("COURTLISTENER_API_TOKEN not set! Get free token: https://www.courtlistener.com/sign-in/")

    # Load ALL SEC-registered public companies
    PUBLIC_COMPANIES = build_company_db()
    unique_tickers = len(set(PUBLIC_COMPANIES.values()))

    seen = load_seen()
    log.info(f"  Seen file: {SEEN_FILE} ({len(seen)} entries)")
    cycle = 0; cidx = 0; didx = 0; bidx = 0; sidx = 0; gidx = 0; last_save = time.time()

    public_count = len(set(v for v in PUBLIC_COMPANIES.values() if not v.startswith("🔥") and not v.startswith("🏛️")))
    private_count = len([k for k, v in BUZZY_COMPANIES.items() if v.startswith("🔥")])
    startup = {"title":"🟢 Court Filing Monitor Online","description":(
        f"• Polling: every **{CHECK_INTERVAL}s** (staggered across 12 sources)\n"
        f"• Federal courts: **opinions + new dockets** (rulings AND lawsuits)\n"
        f"• 🚨 High-impact: SEC enforcement, DOJ antitrust, class actions\n"
        f"• ⚡ Docket watch: new lawsuits (materiality filtered)\n"
        f"• 🔥 Buzzy privates: OpenAI, xAI, Anthropic, SpaceX, Kalshi, etc.\n"
        f"• 🏛️ State courts: DE Chancery, CA Appellate, NY Court of Appeals\n"
        f"• ⚖️ Gov enforcement: SEC/DOJ/FTC/CFPB/CFTC/EPA/FDA/state AGs (court filings)\n"
        f"• Supreme Court: opinions + dockets + supremecourt.gov\n"
        f"• CAFC: direct PDF scraping (patent rulings)\n"
        f"• Companies: **{public_count}** public tickers + **{private_count}** private\n"
        f"• Hours: 9AM-4PM EST\n• CL Auth: {'✅' if COURTLISTENER_API_TOKEN else '⚠️ Not set'}\n"
        f"• AI: DeepSeek"),
        "color":0x00FF00,"timestamp":datetime.now(tz=pytz.utc).isoformat()}

    # Test Discord connectivity on startup
    r1 = send_discord(DISCORD_WEBHOOK_FEDERAL, [startup])
    r2 = send_discord(DISCORD_WEBHOOK_SCOTUS, [startup])
    log.info(f"  Discord Federal webhook: {'✅ CONNECTED' if r1 else '❌ FAILED'}")
    log.info(f"  Discord SCOTUS webhook: {'✅ CONNECTED' if r2 else '❌ FAILED'}")
    if not r1: log.error(f"  Federal webhook URL: {DISCORD_WEBHOOK_FEDERAL[:60]}...")
    if not r2: log.error(f"  SCOTUS webhook URL: {DISCORD_WEBHOOK_SCOTUS[:60]}...")

    # If seen file was empty (fresh deploy or first time with volume), do a silent seed cycle
    # to populate seen dict without spamming Discord
    global SEED_MODE
    if len(seen) == 0:
        SEED_MODE = True
        log.info("🌱 Seed mode: first run with empty seen file — scanning all sources silently first")

    while True:
        try:
            if not is_market_hours():
                now = datetime.now(EST)
                if now.minute == 0 and now.second < 35:
                    log.info(f"Outside market hours ({now.strftime('%I:%M %p %Z')})")
                time.sleep(30)
                continue

            # Staggered 12-phase cycle at 10s intervals (each source every 120s)
            # Phase 0: Federal opinions (broad)     Phase 1: SCOTUS opinions (CL API)
            # Phase 2: Company opinions (CL API)    Phase 3: SCOTUS docket (CL API)
            # Phase 4: Federal opinions (broad)     Phase 5: SCOTUS website scrape
            # Phase 6: CAFC website scrape           Phase 7: High-impact filings
            # Phase 8: New docket watch (lawsuits)  Phase 9: Buzzy private companies
            # Phase 10: State court rulings (DE/CA/NY rotation)
            # Phase 11: Gov enforcement (SEC/DOJ/FTC/CFPB/FDA/EPA/AG rotation)
            phase = cycle % 12

            if phase in (0, 4):
                n = check_federal_filings(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} federal opinion alerts")
            elif phase == 1:
                n = check_scotus_opinions(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} SCOTUS opinion alerts")
            elif phase == 2:
                n = check_federal_company(seen, cidx); cidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} company opinion alerts")
            elif phase == 3:
                n = check_scotus_docket(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} SCOTUS docket alerts")
            elif phase == 5:
                n = check_scotus_website(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} SCOTUS.gov alerts")
            elif phase == 6:
                n = check_cafc_website(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} CAFC alerts")
            elif phase == 7:
                n = check_high_impact_filings(seen)
                if n and not SEED_MODE: log.info(f"Sent {n} high-impact filing alerts")
            elif phase == 8:
                n = check_new_dockets(seen, didx); didx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} new docket alerts")
            elif phase == 9:
                n = check_buzzy_filings(seen, bidx); bidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} buzzy company alerts")
            elif phase == 10:
                n = check_state_courts(seen, sidx); sidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} state court ruling alerts")
            elif phase == 11:
                n = check_gov_enforcement(seen, gidx); gidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} gov enforcement alerts")

            # After completing first full 12-phase cycle, exit seed mode
            if SEED_MODE and phase == 11:
                SEED_MODE = False
                save_seen(seen)
                log.info(f"✅ Seed complete — {len(seen)} entries tracked. Now alerting normally.")

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
