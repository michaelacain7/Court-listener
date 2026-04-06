"""
Court Filing Monitor for Public Companies
==========================================
Monitors:
  1. Federal court filings involving public companies (all federal courts)
  2. Supreme Court opinions/orders mentioning public companies
  3. SEC EDGAR 6-K filings for Chinese/international court rulings
  4. Financial news for Chinese court rulings affecting US-traded stocks

Alerts via Discord webhooks with DeepSeek AI summaries.
Runs 9 AM - 4 PM EST on market open days only.

RATE LIMITS:
  CourtListener: 5,000 req/hour (authenticated)
  Adaptive polling: 3s during peak, 5s normal, circuit breaker at 80% usage

Deploy on Railway.
"""

import os
import json
import time
import hashlib
import logging
import re
import threading
from collections import deque
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
# Mirror webhook — receives ALL alerts (federal + SCOTUS + state + gov enforcement)
DISCORD_WEBHOOK_MIRROR = os.getenv(
    "DISCORD_WEBHOOK_MIRROR",
    "https://discordapp.com/api/webhooks/1479265927051608086/OXjKNirFTDZDGwgqIiq6zcU8a-mcAEgHmsAA2y71uLK2XYlPJ8dC21C8NKRQH6ZhHQR1"
)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-089099fee3e94c1c97189694645d6f92")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

COURTLISTENER_API_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "b4863b70263a553e98685a87ea755a8ad5b0bfaa")
CL_BASE = "https://www.courtlistener.com"
CL_SEARCH_URL = f"{CL_BASE}/api/rest/v4/search/"
CL_DOCKET_URL = f"{CL_BASE}/api/rest/v4/dockets/"

# Base interval — adaptive polling adjusts this dynamically
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
EST = pytz.timezone("US/Eastern")

# ── Adaptive Rate Limiter with Circuit Breaker ──

class RateLimiter:
    """Thread-safe rate limiter with circuit breaker and adaptive backoff.
    Uses a deque for O(1) append/popleft instead of list filtering."""

    def __init__(self, max_per_hour=4500):
        self.max_per_hour = max_per_hour
        self.requests = deque()
        self.lock = threading.Lock()
        self._circuit_open = False
        self._circuit_open_until = 0
        self._consecutive_429s = 0

    def _prune(self):
        """Remove requests older than 1 hour. Must hold self.lock."""
        cutoff = time.time() - 3600
        while self.requests and self.requests[0] < cutoff:
            self.requests.popleft()

    def can_request(self):
        with self.lock:
            if self._circuit_open and time.time() < self._circuit_open_until:
                return False
            if self._circuit_open and time.time() >= self._circuit_open_until:
                self._circuit_open = False
            self._prune()
            return len(self.requests) < self.max_per_hour

    def record(self):
        with self.lock:
            self.requests.append(time.time())

    def remaining(self):
        with self.lock:
            self._prune()
            return self.max_per_hour - len(self.requests)

    def per_minute(self):
        with self.lock:
            cutoff = time.time() - 60
            return sum(1 for t in self.requests if t > cutoff)

    def usage_pct(self):
        """Return current usage as a percentage of max_per_hour."""
        with self.lock:
            self._prune()
            return len(self.requests) / self.max_per_hour * 100

    def record_429(self):
        """Record a 429 response — triggers circuit breaker after repeated hits."""
        with self.lock:
            self._consecutive_429s += 1
            if self._consecutive_429s >= 2:
                # Open circuit for exponential backoff: 60s, 120s, 240s...
                wait = min(60 * (2 ** (self._consecutive_429s - 2)), 600)
                self._circuit_open = True
                self._circuit_open_until = time.time() + wait
                logging.getLogger("CourtMonitor").warning(
                    f"Circuit breaker OPEN for {wait}s after {self._consecutive_429s} consecutive 429s")

    def record_success(self):
        """Record a successful request — resets 429 counter."""
        with self.lock:
            self._consecutive_429s = 0

    def should_throttle(self):
        """Returns True if usage is above 80% — caller should slow down."""
        return self.usage_pct() >= 80

rate_limiter = RateLimiter()

# ── Market Holidays ──
# US stock market holidays (NYSE/NASDAQ) for 2025-2027
MARKET_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
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
    # Chinese litigation watchlist companies (public tickers)
    "Xiao-I":"AIXI","Xiao-I Corporation":"AIXI","XiaoI":"AIXI",
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

# ── Chinese Litigation Watchlist ──
# Cases in Chinese courts affecting US-traded stocks.
# Used by SEC EDGAR 6-K monitor and news monitor.
CHINA_LITIGATION_WATCHLIST = {
    "AIXI": {
        "company": "Xiao-I Corporation",
        "ticker": "AIXI",
        "opponent": "Apple Inc.",
        "opponent_ticker": "AAPL",
        "court": "Shanghai High People's Court",
        "case_type": "Patent infringement",
        "status": "Damages phase — patents declared permanently valid by Supreme People's Court (Mar 2026)",
        "damages_claimed": "$1.4 billion",
        "keywords": ["xiao-i", "xiaoai", "AIXI", "shanghai court", "shanghai high people"],
        "sec_cik": "0001866757",  # Xiao-I CIK for EDGAR searches
    },
}

# SEC EDGAR 6-K search — companies to monitor for Chinese/international court filings
EDGAR_6K_WATCHLIST = {
    "AIXI": {
        "cik": "0001866757",
        "search_terms": ["xiao-i", "court", "litigation", "patent", "infringement", "damages", "ruling"],
    },
}

# News keywords for Chinese court monitoring
CHINA_NEWS_KEYWORDS = [
    '"Shanghai court" Apple',
    '"Xiao-I" court ruling',
    '"AIXI" court',
    '"Supreme People\'s Court" Apple patent',
    '"Shanghai High People\'s Court"',
    '"China court ruling" stock',
]

# ── Macro-Market Keywords ──
# Court rulings on these topics move entire sectors even when no specific company is named.
MACRO_KEYWORDS = {
    # Trade / Tariffs (CAFC, Court of International Trade)
    "tariff": "📊 TARIFFS/TRADE", "tariffs": "📊 TARIFFS/TRADE",
    "duties": "📊 TARIFFS/TRADE", "import duty": "📊 TARIFFS/TRADE",
    "antidumping": "📊 TARIFFS/TRADE", "countervailing": "📊 TARIFFS/TRADE",
    "customs": "📊 TARIFFS/TRADE", "trade representative": "📊 TARIFFS/TRADE",
    "section 301": "📊 TARIFFS/TRADE", "section 232": "📊 TARIFFS/TRADE",
    "section 201": "📊 TARIFFS/TRADE",
    "IEEPA": "📊 TARIFFS/TRADE", "international emergency economic powers": "📊 TARIFFS/TRADE",
    "harmonized tariff": "📊 TARIFFS/TRADE", "USTR": "📊 TARIFFS/TRADE",
    "trade act": "📊 TARIFFS/TRADE", "trade war": "📊 TARIFFS/TRADE",
    "border protection": "📊 TARIFFS/TRADE",
    "commerce department": "📊 TARIFFS/TRADE",
    # Executive power / regulatory
    "executive order": "📊 EXEC POWER", "presidential authority": "📊 EXEC POWER",
    "emergency powers": "📊 EXEC POWER", "national emergency": "📊 EXEC POWER",
    "major questions doctrine": "📊 REGULATORY",
    "chevron deference": "📊 REGULATORY", "chevron": "📊 REGULATORY",
    "administrative procedure act": "📊 REGULATORY",
    # Healthcare / Drug pricing
    "affordable care act": "📊 HEALTHCARE", "ACA": "📊 HEALTHCARE",
    "drug pricing": "📊 HEALTHCARE", "medicare": "📊 HEALTHCARE",
    "medicaid": "📊 HEALTHCARE", "FDA approval": "📊 HEALTHCARE",
    "prescription drug": "📊 HEALTHCARE",
    # Energy / Environment
    "clean air act": "📊 ENERGY/ENV", "clean water act": "📊 ENERGY/ENV",
    "emissions": "📊 ENERGY/ENV", "climate": "📊 ENERGY/ENV",
    "renewable fuel": "📊 ENERGY/ENV", "pipeline": "📊 ENERGY/ENV",
    "LNG export": "📊 ENERGY/ENV", "drilling": "📊 ENERGY/ENV",
    # Tech / Telecom regulation
    "net neutrality": "📊 TECH/TELECOM", "broadband": "📊 TECH/TELECOM",
    "section 230": "📊 TECH/TELECOM", "TikTok ban": "📊 TECH/TELECOM",
    "data privacy": "📊 TECH/TELECOM",
    # Financial regulation
    "dodd-frank": "📊 FINANCIAL REG", "banking regulation": "📊 FINANCIAL REG",
    "securities regulation": "📊 FINANCIAL REG", "fiduciary rule": "📊 FINANCIAL REG",
    "cryptocurrency": "📊 CRYPTO REG", "digital asset": "📊 CRYPTO REG",
    "stablecoin": "📊 CRYPTO REG",
    # Antitrust / competition (structural)
    "monopoly": "📊 ANTITRUST", "antitrust": "📊 ANTITRUST",
    "anticompetitive": "📊 ANTITRUST", "market dominance": "📊 ANTITRUST",
    # Patent system / IP (affects patent-heavy sectors)
    "patent eligibility": "📊 PATENT LAW", "section 101": "📊 PATENT LAW",
    "inter partes review": "📊 PATENT LAW",
    # Labor / Employment (broad)
    "minimum wage": "📊 LABOR", "overtime rule": "📊 LABOR",
    "independent contractor": "📊 LABOR", "gig economy": "📊 LABOR",
    "non-compete": "📊 LABOR",
    # Sanctions
    "sanctions": "📊 SANCTIONS", "OFAC": "📊 SANCTIONS",
    "export control": "📊 SANCTIONS", "entity list": "📊 SANCTIONS",
}

def match_macro_keywords(text):
    """Check text for macro-market keywords that move sectors.
    Returns list of (keyword, sector_tag) matches, or empty list."""
    if not text:
        return []
    text_lower = text.lower()
    seen_sectors = set()
    matches = []
    for kw, sector in MACRO_KEYWORDS.items():
        if sector in seen_sectors:
            continue
        if kw.lower() in text_lower:
            matches.append((kw, sector))
            seen_sectors.add(sector)
    return matches

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

    # Determine cache path — prefer persistent volume
    cache_path = SEC_TICKERS_CACHE
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("VOLUME_PATH", "")
    if vol and os.path.isdir(vol):
        cache_path = os.path.join(vol, "sec_tickers_cache.json")

    # Try cached file first (avoid hammering SEC on restarts)
    try:
        with open(cache_path) as f:
            cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            if age < 86400:  # Cache valid for 24 hours
                companies = {k: v for k, v in cached.items() if k != "_ts"}
                if len(companies) > 5000:
                    log.info(f"Loaded {len(companies)} companies from cache")
                    return companies
    except (OSError, json.JSONDecodeError, KeyError) as e:
        log.debug(f"SEC cache load failed: {e}")

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
        try:
            with open(cache_path, "w") as f:
                json.dump(cache_data, f)
        except OSError as e:
            log.warning(f"Failed to write SEC cache: {e}")

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
            except OSError:
                continue
    # Fallback to local (won't persist across deploys)
    return "seen_filings.json"

SEEN_FILE = _get_seen_path()

def load_seen():
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            cutoff = (datetime.now(tz=pytz.utc) - timedelta(days=7)).isoformat()
            return {k: v for k, v in data.items() if v > cutoff}
    except (OSError, json.JSONDecodeError) as e:
        log.debug(f"Seen file load: {e}")
        return {}

def save_seen(seen):
    cutoff = (datetime.now(tz=pytz.utc) - timedelta(days=7)).isoformat()
    try:
        tmp_path = SEEN_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump({k: v for k, v in seen.items() if v > cutoff}, f)
        os.replace(tmp_path, SEEN_FILE)
    except OSError as e:
        log.error(f"Failed to save seen file: {e}")

def mark_seen(seen, fid):
    seen[fid] = datetime.now(tz=pytz.utc).isoformat()

def is_market_hours():
    now = datetime.now(EST)
    if now.weekday() >= 5: return False
    if now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS: return False
    if now.hour < 9 or now.hour >= 16: return False
    return True

def get_adaptive_interval():
    """Return polling interval based on time of day and rate limit usage.
    Faster during market open/close (high-activity windows), slower if approaching limits."""
    if rate_limiter.should_throttle():
        log.warning(f"Rate limit at {rate_limiter.usage_pct():.0f}% — throttling to 15s")
        return 15

    now = datetime.now(EST)
    hour, minute = now.hour, now.minute

    # Peak periods: market open (9:30-10:00) and close (3:30-4:00)
    if (hour == 9 and minute >= 30) or (hour == 10 and minute < 0):
        return max(3, CHECK_INTERVAL - 2)
    if (hour == 15 and minute >= 30) or hour == 16:
        return max(3, CHECK_INTERVAL - 2)

    # Normal market hours
    return CHECK_INTERVAL

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
                rate_limiter.record_429()
                # Exponential backoff: use Retry-After header or 60 * 2^attempt
                retry_after = 60
                try:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                except (ValueError, TypeError):
                    pass
                wait = min(retry_after * (2 ** attempt), 300)
                log.warning(f"CL 429 - exponential backoff {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                log.error("CL auth failed. Set COURTLISTENER_API_TOKEN!")
                return None
            resp.raise_for_status()
            rate_limiter.record_success()
            return resp.json()
        except requests.exceptions.Timeout:
            log.warning(f"CL timeout (attempt {attempt+1})")
            time.sleep(5 * (attempt + 1))
        except requests.exceptions.RequestException as e:
            log.error(f"CL request failed: {e}")
            time.sleep(5 * (attempt + 1))
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

def check_materiality(case_name, court="", snippet="", filing_type="new lawsuit", docket_id=None):
    """Multi-layer materiality filter for new case filings.
    Layer 1: Fast local pattern match (no API call needed)
    Layer 2: Fetch CL docket details for nature-of-suit code
    Layer 3: DeepSeek classification with actual context
    Returns (is_material: bool, reason: str)."""

    cn = case_name.strip()
    cn_lower = cn.lower()

    # ── Layer 1: Fast local pre-filter ──
    # Pattern: "SingleName v. BigCompany" = almost always individual plaintiff
    # Split on " v. " or " v " or " vs. " or " vs "
    parts = re.split(r'\s+v\.?\s+', cn, maxsplit=1, flags=re.IGNORECASE)
    is_class = False  # Initialize before use in Layer 2

    if len(parts) == 2:
        plaintiff = parts[0].strip()
        defendant = parts[1].strip()

        # Check if plaintiff looks like a government/institutional entity (KEEP)
        govt_patterns = [
            "united states", "securities and exchange", "federal trade",
            "department of", "state of", "commonwealth of", "people of",
            "attorney general", "consumer financial", "commodity futures",
            "national labor", "environmental protection", "food and drug",
            "in re ", "in the matter of",
        ]
        is_govt_plaintiff = any(gp in plaintiff.lower() for gp in govt_patterns)

        # Check if plaintiff looks like a company (KEEP - company v. company)
        company_indicators = ["inc", "llc", "corp", "ltd", "l.p.", "co.", "company",
                              "group", "holdings", "partners", "fund", "bank",
                              "insurance", "financial", "technologies", "pharma",
                              "systems", "enterprises", "international", "association"]
        is_company_plaintiff = any(ci in plaintiff.lower() for ci in company_indicators)

        # Check for class action / MDL indicators (KEEP)
        class_indicators = ["in re", "mdl", "consolidated", "class action",
                            "securities litigation", "antitrust litigation",
                            "products liability", "on behalf of"]
        is_class = any(ci in cn_lower for ci in class_indicators)

        # If plaintiff is a short single name (no company suffix, not govt) → individual suit
        if not is_govt_plaintiff and not is_company_plaintiff and not is_class:
            # Count words in plaintiff name
            plaintiff_words = [w for w in plaintiff.split() if len(w) > 1]
            # Individual names: 1-3 words, no commas (unless "Doe, John" style)
            if len(plaintiff_words) <= 3 and "," not in plaintiff:
                log.info(f"  PRE-FILTER: Individual plaintiff '{plaintiff}' → SKIP")
                return False, f"NO - Individual plaintiff ({plaintiff}) v. company - not material"
            # Also catch "Beasley" single-word plaintiff
            if len(plaintiff_words) == 1:
                log.info(f"  PRE-FILTER: Single-name plaintiff '{plaintiff}' → SKIP")
                return False, f"NO - Single-name individual plaintiff - not material"

    # ── Layer 2: Fetch CL docket details for nature-of-suit ──
    docket_context = ""
    nature_of_suit = ""
    if docket_id and rate_limiter.remaining() > 300:
        try:
            docket_data = cl_request(
                f"{CL_BASE}/api/rest/v4/dockets/{docket_id}/",
                params={"fields": "id,nature_of_suit,cause,case_name,referred_to,assigned_to"},
                timeout=10
            )
            if docket_data:
                nature_of_suit = docket_data.get("nature_of_suit", "") or ""
                cause = docket_data.get("cause", "") or ""
                docket_context = f"Nature of suit: {nature_of_suit}\nCause: {cause}"
                log.debug(f"  Docket details: NOS={nature_of_suit}, cause={cause}")

                # Fast reject based on nature-of-suit codes (individual matters)
                nos_lower = nature_of_suit.lower()
                immaterial_nos = [
                    "motor vehicle", "personal injury", "product liability",
                    "assault", "slander", "libel", "marine",
                    "airplane", "medical malpractice", "personal property",
                    "insurance", "negotiable instrument", "student loan",
                    "other personal property", "prop. damage",
                ]
                if any(nos in nos_lower for nos in immaterial_nos):
                    # But keep if it's a class action or MDL even with these NOS codes
                    if not is_class:
                        log.info(f"  NOS-FILTER: '{nature_of_suit}' → SKIP")
                        return False, f"NO - Nature of suit: {nature_of_suit}"
        except Exception as e:
            log.debug(f"  Docket detail fetch failed: {e}")

    # ── Layer 3: DeepSeek classification with enriched context ──
    DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    if not DEEPSEEK_KEY:
        return True, "No AI key - defaulting to alert"
    try:
        context_block = f"Case: {case_name}\nCourt: {court}\nType: {filing_type}"
        if docket_context:
            context_block += f"\n{docket_context}"
        if snippet:
            context_block += f"\nSnippet: {snippet[:500]}"

        resp = requests.post(DEEPSEEK_API_URL, headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"
        }, json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": (
                    "You are a financial materiality classifier for stock traders. "
                    "Determine if this court case filing would move the stock price.\n\n"
                    "MATERIAL — alert YES:\n"
                    "- Class actions, MDL, securities litigation\n"
                    "- Government enforcement (SEC, DOJ, FTC, CFTC, state AG, CFPB)\n"
                    "- Antitrust / monopoly suits\n"
                    "- Major patent/IP disputes between companies\n"
                    "- Securities fraud, accounting fraud\n"
                    "- Environmental/regulatory with potential large fines\n"
                    "- Whistleblower / qui tam\n"
                    "- Major contract disputes ($100M+)\n"
                    "- Company-filed offensive litigation\n"
                    "- Data breaches, product liability class actions\n"
                    "- Any case likely to generate financial news coverage\n\n"
                    "NOT MATERIAL — alert NO:\n"
                    "- Individual plaintiff vs company (personal injury, employment, slip-and-fall)\n"
                    "- Single-person wrongful termination or discrimination\n"
                    "- Individual consumer complaints\n"
                    "- Traffic/vehicle accidents\n"
                    "- Workers' comp, individual insurance disputes\n"
                    "- Small claims, routine commercial disputes\n"
                    "- Any case where one individual person is suing a company\n\n"
                    "KEY RULE: If the plaintiff is a single individual person's name "
                    "(not a company, government entity, or class), default to NO.\n"
                    "Large companies get sued by individuals THOUSANDS of times per year. "
                    "These are NEVER material to stock price.\n\n"
                    "Respond with EXACTLY one line: YES or NO followed by brief reason.\n"
                    "Example: YES - SEC enforcement action, potential large fine\n"
                    "Example: NO - Individual personal injury plaintiff"
                )},
                {"role": "user", "content": context_block}
            ],
            "max_tokens": 80, "temperature": 0.1
        }, timeout=15)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        is_material = answer.upper().startswith("YES")
        log.info(f"  Materiality: {answer[:120]}")
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
                # Safely parse retry_after — may not be valid JSON
                wait = 5.0
                try:
                    wait = float(resp.json().get("retry_after", 5)) + 0.5
                except (ValueError, json.JSONDecodeError, AttributeError):
                    wait = 5.0 * (attempt + 1)
                log.warning(f"Discord 429 rate limited, waiting {wait:.1f}s")
                time.sleep(wait)
                continue
            if resp.status_code in (200, 204):
                # Mirror to the combined webhook (fire-and-forget with timeout)
                if DISCORD_WEBHOOK_MIRROR and webhook_url != DISCORD_WEBHOOK_MIRROR:
                    try:
                        requests.post(DISCORD_WEBHOOK_MIRROR, json={"embeds": embeds}, timeout=10)
                    except Exception:
                        pass  # Don't let mirror failure block primary
                return True
            else:
                log.error(f"Discord send failed: HTTP {resp.status_code} - {resp.text[:300]}")
                return False
        except Exception as e:
            log.error(f"Discord send error (attempt {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
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
    """Build the best available URL for a CL search result."""
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
    except ImportError:
        log.debug("pdfplumber not installed, skipping")
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
    """Search for recent federal court OPINIONS (rulings/decisions) involving public companies
    or macro-market topics (tariffs, trade, regulatory policy)."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    data = cl_request(CL_SEARCH_URL, params={
        "type":"o","order_by":"dateFiled desc","filed_after":yesterday,
        "court":"dcd nyed cacd cand ilnd txsd txnd fled flsd mad nysd paed njd ded vaed wawd coed gand mdd ctd cit ca1 ca2 ca3 ca4 ca5 ca6 ca7 ca8 ca9 ca10 ca11 cadc cafc"
    })
    if not data: return 0
    alerts = 0
    for item in data.get("results", []):
        cn = item.get("caseName","") or item.get("case_name","")
        fid = make_id(item, "fed")
        if fid in seen: continue
        mark_seen(seen, fid)
        matches = match_public_company(cn)
        macro_matches = []
        is_macro = False

        if not matches:
            # Check case name + snippet for macro keywords
            snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
            macro_matches = match_macro_keywords(cn + " " + snippet)
            if not macro_matches:
                continue
            is_macro = True

        log.info(f"FEDERAL {'MACRO ' if is_macro else ''}OPINION: {cn[:80]}")
        details = []
        for k,l in [("court","Court"),("dateFiled","Filed"),("citation","Citation"),("suitNature","Nature")]:
            if item.get(k): details.append(f"**{l}:** {item[k]}")
        # Fetch full opinion text from CL API
        opinion_text = fetch_cl_opinion_text(item)
        stxt = f"Court Opinion in: {cn}\n\n{opinion_text}"
        summary = summarize_with_deepseek(stxt, "federal court opinion") if len(opinion_text) > 50 else "Limited details available."

        if matches:
            tstr = format_tickers(matches)
        else:
            tstr = " | ".join(sector for _, sector in macro_matches[:3])

        doc_url = get_filing_url(item, "o")
        embed = {"title":f"{'🔴' if is_macro else '🚨'} Federal Opinion: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[{"name":"📊 Impact" if is_macro else "📊 Tickers","value":tstr,"inline":False}],
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
    """Search for high-impact NEW filings: SEC/DOJ/FTC enforcement, class action certs,
    major settlements, AND macro-market rulings (tariffs, trade, regulatory policy).
    Filters out individual personal lawsuits (SMITH v. BIGCORP pattern)."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")
    # Rotate through high-impact search terms — institutional/govt actions + macro topics
    queries = [
        '"Securities and Exchange Commission" enforcement',
        '"Department of Justice" antitrust',
        '"Federal Trade Commission" complaint',
        '"class action" "class certification"',
        "securities fraud settlement approval",
        '"preliminary injunction" patent infringement',
        '"consent decree" antitrust',
        "qui tam whistleblower fraud",
        # Macro-market keyword searches (tariffs, trade, regulatory)
        "tariff duties customs",
        "IEEPA emergency powers trade",
        '"executive order" regulation',
        '"antidumping" OR "countervailing"',
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
        macro_matches = []
        is_macro = False

        if not matches:
            # Check for macro keywords in case name + snippet
            snippet_raw = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
            macro_matches = match_macro_keywords(cn + " " + snippet_raw + " " + q)
            if not macro_matches:
                continue
            is_macro = True
        else:
            # Skip individual-vs-company pattern unless it's a govt agency or VIP plaintiff
            plaintiff = cn.split(" v. ")[0].strip() if " v. " in cn else cn.split(" v ")[0].strip() if " v " in cn else ""
            govt_agencies = ["SEC", "Securities and Exchange", "United States", "Department of Justice",
                             "DOJ", "FTC", "Federal Trade", "CFPB", "Consumer Financial", "EPA",
                             "NLRB", "EEOC", "State of", "Commonwealth", "People of", "Attorney General"]
            class_indicators = ["In re ", "In Re ", "IN RE ", "MDL", "Consolidated", "Securities Litigation",
                                "Class Action", "Antitrust Litigation", "Products Liability"]
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
                if len(plaintiff) < 25 and "," not in plaintiff and "Inc" not in plaintiff:
                    continue

        log.info(f"HIGH-IMPACT {'MACRO ' if is_macro else ''}[{q[:25]}]: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"High-impact filing ({q}):\nCase: {cn}\n{snippet}", "high-impact court filing")

        if matches:
            tstr = format_tickers(matches)
        else:
            tstr = " | ".join(sector for _, sector in macro_matches[:3])

        doc_url = get_filing_url(item, "r")
        # Clean query label for display
        q_label = q.replace('"', '').title()[:50]
        embed = {"title":f"{'🔴' if is_macro else '🚨'} High-Impact Filing: {cn[:200]}","url":doc_url,"color":0xFF0000,
            "fields":[
                {"name":"📊 Impact" if is_macro else "📊 Tickers","value":tstr,"inline":False},
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
        # Chinese litigation watchlist
        "Xiao-I",
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
            cn, court=item.get("court",""), snippet=snippet,
            filing_type="new lawsuit", docket_id=item.get("docket_id")
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
        pdf_links = re.findall(r'href="(/opinions/\d+pdf/[^"]+\.pdf)"', resp.text, re.IGNORECASE)

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
    CAFC_URL = "https://www.cafc.uscourts.gov/home/case-information/opinions-orders/"
    CAFC_BASE = "https://www.cafc.uscourts.gov"
    try:
        resp = requests.get(CAFC_URL, timeout=20,
            headers={"User-Agent": "CourtFilingMonitor/1.0"})
        if not resp.ok:
            log.warning(f"CAFC table page returned {resp.status_code}")
            return 0

        # PDF links from the opinions table (primary source)
        pdf_pattern = re.compile(
            r'<a\s+href="(/opinions-orders/[^"]+\.pdf)"[^>]*>\s*([^<]+?)\s*</a>', re.IGNORECASE)

        entries = []
        seen_case_nums = set()

        for match in pdf_pattern.finditer(resp.text):
            path, title = match.group(1), match.group(2).strip()
            pdf_url = f"{CAFC_BASE}{path}"
            if title and len(title) > 5:
                cn_match = re.search(r'(\d{2}-\d{3,5})', path)
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
                    post_pattern = re.compile(
                        r'<a\s+href="(https?://www\.cafc\.uscourts\.gov/\d{2}-\d{2}-\d{4}[^"]*)"[^>]*>\s*'
                        r'([^<]+?)\s*</a>', re.IGNORECASE)
                    for match in post_pattern.finditer(blog_resp.text):
                        url, title = match.group(1), match.group(2).strip()
                        if title and len(title) > 5:
                            entries.append((url, title, None))
            except Exception as e:
                log.debug(f"CAFC blog page fetch failed: {e}")

        if not entries:
            return 0

        for url, title, pdf_url in entries[:20]:  # Check latest 20
            fid = f"cafc_{hashlib.md5((url + title).encode()).hexdigest()}"
            if fid in seen:
                continue
            mark_seen(seen, fid)

            # Extract case name (strip [OPINION], [ORDER], [RULE 36 JUDGMENT] suffix)
            case_name = re.sub(r'\s*\[(OPINION|ORDER|RULE\s*36\s*JUDGMENT|ERRATA)\].*$', '', title, flags=re.IGNORECASE).strip()

            # Determine doc type for display
            doc_type = "Opinion"
            if "[ORDER]" in title.upper():
                doc_type = "Order"
            elif "RULE 36" in title.upper():
                doc_type = "Rule 36 Judgment"

            # Step 1: Check for company match
            matches = match_public_company(case_name)
            macro_matches = []
            is_macro = False

            # Step 2: If no company match, check for macro keywords in case name
            if not matches:
                macro_matches = match_macro_keywords(case_name + " " + title)

            # Step 3: Extract PDF text (needed for both summary AND macro detection)
            full_text = ""
            if pdf_url or url.endswith('.pdf'):
                actual_pdf = pdf_url or url
                try:
                    pdf_resp = requests.get(actual_pdf, timeout=25,
                        headers={"User-Agent": "CourtFilingMonitor/1.0"})
                    if pdf_resp.ok and len(pdf_resp.content) < 2000000:
                        import io
                        try:
                            from pypdf import PdfReader
                        except ImportError:
                            from PyPDF2 import PdfReader
                        reader = PdfReader(io.BytesIO(pdf_resp.content))
                        pages_text = []
                        for page in reader.pages[:10]:
                            pt = page.extract_text()
                            if pt:
                                pages_text.append(pt)
                        full_text = "\n".join(pages_text)
                        if len(full_text) > 300:
                            log.debug(f"  PDF extracted: {len(full_text)} chars from {len(pages_text)} pages")
                except Exception as e:
                    log.debug(f"  PDF extraction failed: {e}")
            elif not pdf_url and not url.endswith('.pdf'):
                try:
                    page_resp = requests.get(url, timeout=15,
                        headers={"User-Agent": "CourtFilingMonitor/1.0"})
                    if page_resp.ok:
                        pdf_match = re.search(
                            r'href="((?:https?://www\.cafc\.uscourts\.gov)?/opinions-orders/[^"]+\.pdf)"',
                            page_resp.text, re.IGNORECASE)
                        if pdf_match:
                            found = pdf_match.group(1)
                            pdf_url = found if found.startswith('http') else f"{CAFC_BASE}{found}"
                            full_text = extract_pdf_text(pdf_url)
                except Exception as e:
                    log.debug(f"  Blog page fetch failed: {e}")

            # Step 4: If still no match, check macro keywords in the PDF text
            if not matches and not macro_matches and full_text:
                macro_matches = match_macro_keywords(full_text[:3000])

            # Skip if neither company nor macro keyword match
            if not matches and not macro_matches:
                continue

            is_macro = bool(macro_matches) and not matches
            log.info(f"CAFC {'MACRO' if is_macro else ''}: {case_name[:80]}")

            # Build summary
            if full_text and len(full_text) > 300:
                summary_text = f"CAFC {doc_type}:\n{case_name}\n\nFull text (first pages):\n{full_text[:10000]}"
            else:
                summary_text = f"Federal Circuit (CAFC) {doc_type}:\n{case_name}\nURL: {url}\n\nWARNING: Full text unavailable. Do NOT guess the outcome."

            summary = summarize_with_deepseek(summary_text, "Federal Circuit opinion")

            # Format tickers/sectors
            if matches:
                tstr = format_tickers(matches)
            else:
                tstr = " | ".join(sector for _, sector in macro_matches[:3])

            embed = {
                "title": f"{'🔴' if is_macro else '🚨'} CAFC {doc_type}: {case_name[:200]}",
                "url": url,
                "color": 0xFF0000,
                "fields": [
                    {"name": "📊 Impact" if is_macro else "📊 Tickers", "value": tstr, "inline": False},
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

def check_delaware_courts(seen):
    """Scrape Delaware Court of Chancery and Supreme Court opinions."""
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

        rows = re.findall(
            r'<a\s+href="(/Opinions/Download\.aspx\?id=\d+)"[^>]*>([^<]+)</a>.*?'
            r'(\d{2}/\d{2}/\d{4})',
            html, re.DOTALL
        )

        today = datetime.now(EST).date()
        for pdf_path, case_name, date_str in rows[:20]:
            try:
                filed = datetime.strptime(date_str, "%m/%d/%Y").date()
                if (today - filed).days > 2:
                    continue
            except ValueError:
                continue

            fid = f"de_{hashlib.md5((case_name + date_str).encode()).hexdigest()}"
            if fid in seen: continue
            mark_seen(seen, fid)

            matches = match_public_company(case_name)
            if not matches: continue

            log.info(f"DE {court_label}: {case_name[:80]}")
            pdf_url = f"https://courts.delaware.gov{pdf_path}"

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
    """Scrape California Courts of Appeal and Supreme Court published opinions."""
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
    entries = re.findall(
        r'href="([^"]*\.(?:pdf|PDF|htm|aspx)[^"]*)"[^>]*>\s*([^<]{10,200})',
        html
    )

    today = datetime.now(EST).date()
    for link, case_name in entries[:30]:
        case_name = re.sub(r'<[^>]+>', '', case_name).strip()
        if not case_name or len(case_name) < 10: continue
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
    """Scrape New York Court of Appeals and Appellate Division opinions."""
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
    """Rotate through state court scrapers. Each call checks one system."""
    sources = [check_delaware_courts, check_california_courts, check_ny_courts]
    fn = sources[idx % len(sources)]
    try:
        return fn(seen)
    except Exception as e:
        log.error(f"State court check failed: {e}")
        return 0

# ── Government Enforcement Actions (Court Filings) ──

GOV_AGENCY_SEARCHES = [
    ("Securities and Exchange Commission", "SEC"),
    ("Federal Trade Commission", "FTC"),
    ("Consumer Financial Protection Bureau", "CFPB"),
    ("Commodity Futures Trading Commission", "CFTC"),
    ("Environmental Protection Agency", "EPA"),
    ("Department of Justice", "DOJ"),
    ("National Labor Relations Board", "NLRB"),
    ("Department of Labor", "DOL"),
    ("United States of America", "DOJ/USA"),
    ("State of New York", "NY AG"),
    ("State of California", "CA AG"),
    ("State of Texas", "TX AG"),
    ("Commonwealth of Massachusetts", "MA AG"),
    ("People of the State of Illinois", "IL AG"),
    ("Food and Drug Administration", "FDA"),
    ("Federal Energy Regulatory Commission", "FERC"),
    ("Office of the Comptroller", "OCC"),
    ("Federal Communications Commission", "FCC"),
    ("Federal Reserve", "Fed"),
]


def check_gov_enforcement(seen, idx):
    """Search CourtListener for NEW court filings by government agencies."""
    agency_query, agency_label = GOV_AGENCY_SEARCHES[idx % len(GOV_AGENCY_SEARCHES)]
    yesterday = (datetime.now(EST) - timedelta(days=2)).strftime("%m/%d/%Y")

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

        # Verify the agency name is actually in the case name
        if agency_query.lower() not in cn.lower():
            if agency_label == "DOJ/USA" and "united states" not in cn.lower():
                continue
            elif agency_label != "DOJ/USA":
                continue

        # Match the OTHER party (defendant) against our company database
        matches = match_public_company(cn)

        # Filter out false positives where match is from the agency name, not the defendant
        agency_upper = agency_query.upper()
        matches = [(co, tk) for co, tk in matches if co.upper() not in agency_upper]

        if not matches:
            buzzy_match = None
            for bname, btag in BUZZY_COMPANIES.items():
                if len(bname) >= 4 and bname.lower() in cn.lower():
                    buzzy_match = (bname, btag)
                    break
            if not buzzy_match:
                continue
            matches = [buzzy_match]

        log.info(f"GOV ENFORCEMENT [{agency_label}]: {cn[:100]}")

        snippet = re.sub(r'<[^>]+>', '', item.get("snippet", "") or "").strip()

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


# ── Chinese Court Monitoring (Phase 12-13) ──

def check_sec_edgar_6k(seen, idx):
    """Monitor SEC EDGAR for 6-K filings from Chinese litigation watchlist companies.
    Foreign private issuers (like AIXI) must file 6-K forms for material events
    including court rulings, patent decisions, and litigation updates."""

    watchlist_keys = list(EDGAR_6K_WATCHLIST.keys())
    if not watchlist_keys:
        return 0

    ticker = watchlist_keys[idx % len(watchlist_keys)]
    config = EDGAR_6K_WATCHLIST[ticker]
    cik = config["cik"]
    search_terms = config["search_terms"]

    alerts = 0

    # Method 1: EDGAR EFTS full-text search API for 6-K filings
    # This searches the full text of filings, not just metadata
    try:
        efts_url = "https://efts.sec.gov/LATEST/search-index"
        # Search for company-specific 6-K filings from the last 30 days
        thirty_days_ago = (datetime.now(tz=pytz.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        params = {
            "q": " OR ".join(f'"{t}"' for t in search_terms[:3]),
            "dateRange": "custom",
            "startdt": thirty_days_ago,
            "forms": "6-K",
        }

        resp = requests.get(efts_url, params=params, timeout=20, headers={
            "User-Agent": "CourtFilingMonitor/1.0 (court-monitor@example.com)",
            "Accept": "application/json"
        })

        if resp.ok:
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])

            for hit in hits[:5]:
                source = hit.get("_source", {})
                filing_id = hit.get("_id", "")
                file_date = source.get("file_date", "")
                display_name = source.get("display_names", [""])[0] if source.get("display_names") else ""
                form_type = source.get("form_type", "6-K")

                fid = f"edgar6k_{hashlib.md5(filing_id.encode()).hexdigest()}"
                if fid in seen:
                    continue
                mark_seen(seen, fid)

                # Build EDGAR filing URL
                filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=6-K&dateb=&owner=include&count=10"
                if filing_id:
                    # Try to construct direct link
                    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{filing_id}"

                case_info = CHINA_LITIGATION_WATCHLIST.get(ticker, {})
                log.info(f"🇨🇳 EDGAR 6-K [{ticker}]: {display_name[:80]} filed {file_date}")

                summary_text = (
                    f"SEC EDGAR 6-K Filing\n"
                    f"Company: {case_info.get('company', ticker)} (${ticker})\n"
                    f"Form: {form_type}\n"
                    f"Filed: {file_date}\n"
                    f"Context: {case_info.get('case_type', 'Litigation')} - "
                    f"{case_info.get('company', '')} v. {case_info.get('opponent', '')}\n"
                    f"Court: {case_info.get('court', 'Unknown')}\n"
                    f"Status: {case_info.get('status', 'Unknown')}\n"
                    f"Damages: {case_info.get('damages_claimed', 'Unknown')}"
                )
                summary = summarize_with_deepseek(summary_text, "SEC 6-K filing for Chinese court litigation")

                tickers_display = f"**${ticker}** ({case_info.get('company', '')}) | **${case_info.get('opponent_ticker', '')}** ({case_info.get('opponent', '')})"

                embed = {
                    "title": f"🇨🇳 China Court Filing (6-K): {case_info.get('company', ticker)} — {form_type}",
                    "url": filing_url,
                    "color": 0xDE2910,  # Chinese red
                    "fields": [
                        {"name": "📊 Tickers", "value": tickers_display, "inline": False},
                        {"name": "🏛️ Court", "value": case_info.get("court", "Chinese Court"), "inline": True},
                        {"name": "📅 Filed", "value": file_date, "inline": True},
                        {"name": "⚖️ Case", "value": f"{case_info.get('case_type', '')} — {case_info.get('status', '')}", "inline": False},
                        {"name": "💰 Damages", "value": case_info.get("damages_claimed", "Unknown"), "inline": True},
                        {"name": "📄 Document", "value": f"[View SEC Filing]({filing_url})", "inline": False},
                        {"name": "🤖 AI Summary", "value": summary[:1000], "inline": False},
                    ],
                    "footer": {"text": "China Court Monitor | SEC EDGAR 6-K"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
                alerts += 1
                time.sleep(0.5)
        else:
            log.debug(f"EDGAR EFTS returned {resp.status_code}")
    except Exception as e:
        log.debug(f"EDGAR 6-K search failed: {e}")

    # Method 2: EDGAR company filings RSS feed
    try:
        rss_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=6-K&dateb=&owner=include&count=5&search_text=&action=getcompany&output=atom"
        resp = requests.get(rss_url, timeout=15, headers={
            "User-Agent": "CourtFilingMonitor/1.0 (court-monitor@example.com)"
        })
        if resp.ok:
            # Simple XML parsing for Atom feed entries
            entry_pattern = re.compile(r'<entry>(.*?)</entry>', re.DOTALL)
            title_pattern = re.compile(r'<title[^>]*>([^<]+)</title>')
            link_pattern = re.compile(r'<link[^>]*href="([^"]+)"')
            updated_pattern = re.compile(r'<updated>([^<]+)</updated>')

            for entry_match in entry_pattern.finditer(resp.text):
                entry_xml = entry_match.group(1)
                title_m = title_pattern.search(entry_xml)
                link_m = link_pattern.search(entry_xml)
                updated_m = updated_pattern.search(entry_xml)

                if not title_m or not link_m:
                    continue

                entry_title = title_m.group(1).strip()
                entry_link = link_m.group(1).strip()
                entry_date = updated_m.group(1).strip() if updated_m else ""

                fid = f"edgar6k_rss_{hashlib.md5(entry_link.encode()).hexdigest()}"
                if fid in seen:
                    continue
                mark_seen(seen, fid)

                # Only alert on 6-K filings
                if "6-K" not in entry_title.upper():
                    continue

                case_info = CHINA_LITIGATION_WATCHLIST.get(ticker, {})
                log.info(f"🇨🇳 EDGAR 6-K RSS [{ticker}]: {entry_title[:80]}")

                tickers_display = f"**${ticker}** ({case_info.get('company', '')}) | **${case_info.get('opponent_ticker', '')}** ({case_info.get('opponent', '')})"

                embed = {
                    "title": f"🇨🇳 China Court (6-K): {case_info.get('company', ticker)}",
                    "url": entry_link,
                    "color": 0xDE2910,
                    "fields": [
                        {"name": "📊 Tickers", "value": tickers_display, "inline": False},
                        {"name": "📋 Filing", "value": entry_title[:200], "inline": False},
                        {"name": "🏛️ Court", "value": case_info.get("court", "Chinese Court"), "inline": True},
                        {"name": "📅 Date", "value": entry_date[:10], "inline": True},
                        {"name": "📄 Document", "value": f"[View SEC Filing]({entry_link})", "inline": False},
                    ],
                    "footer": {"text": "China Court Monitor | SEC EDGAR RSS"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
                alerts += 1
                time.sleep(0.5)
    except Exception as e:
        log.debug(f"EDGAR RSS check failed: {e}")

    return alerts


def check_china_news(seen, idx):
    """Monitor financial news for Chinese court rulings affecting US-traded stocks.
    Uses Google News RSS to catch news faster than SEC filings."""

    if not CHINA_NEWS_KEYWORDS:
        return 0

    # Rotate through search keywords
    query = CHINA_NEWS_KEYWORDS[idx % len(CHINA_NEWS_KEYWORDS)]
    alerts = 0

    try:
        # Google News RSS feed
        encoded_q = requests.utils.quote(query)
        news_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=US&ceid=US:en"

        resp = requests.get(news_url, timeout=15, headers={
            "User-Agent": "CourtFilingMonitor/1.0"
        })
        if not resp.ok:
            log.debug(f"Google News RSS returned {resp.status_code} for query: {query[:40]}")
            return 0

        # Parse RSS items
        item_pattern = re.compile(r'<item>(.*?)</item>', re.DOTALL)
        title_pattern = re.compile(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>')
        link_pattern = re.compile(r'<link>(.*?)</link>')
        pubdate_pattern = re.compile(r'<pubDate>(.*?)</pubDate>')

        for item_match in item_pattern.finditer(resp.text):
            item_xml = item_match.group(1)
            title_m = title_pattern.search(item_xml)
            link_m = link_pattern.search(item_xml)
            pubdate_m = pubdate_pattern.search(item_xml)

            if not title_m or not link_m:
                continue

            title = title_m.group(1).strip()
            link = link_m.group(1).strip()
            pub_date = pubdate_m.group(1).strip() if pubdate_m else ""

            # Skip old news (more than 3 days)
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    news_dt = parsedate_to_datetime(pub_date)
                    if (datetime.now(tz=pytz.utc) - news_dt).days > 3:
                        continue
                except Exception:
                    pass

            fid = f"china_news_{hashlib.md5((title + link).encode()).hexdigest()}"
            if fid in seen:
                continue
            mark_seen(seen, fid)

            # Check if news matches any watchlist company
            title_lower = title.lower()
            matched_case = None
            for wticker, winfo in CHINA_LITIGATION_WATCHLIST.items():
                for kw in winfo["keywords"]:
                    if kw.lower() in title_lower:
                        matched_case = (wticker, winfo)
                        break
                if matched_case:
                    break

            if not matched_case:
                # Also check for general company matches
                matches = match_public_company(title)
                if not matches:
                    continue
                # If it mentions a China-related keyword, alert
                china_keywords = ["china", "chinese", "shanghai", "beijing", "court", "patent", "ruling", "supreme people"]
                if not any(ck in title_lower for ck in china_keywords):
                    continue

            log.info(f"🇨🇳 CHINA NEWS [{query[:25]}]: {title[:80]}")

            if matched_case:
                wticker, winfo = matched_case
                tickers_display = f"**${wticker}** ({winfo['company']}) | **${winfo.get('opponent_ticker', '')}** ({winfo.get('opponent', '')})"
                court_info = winfo.get("court", "Chinese Court")
            else:
                tickers_display = format_tickers(matches)
                court_info = "Chinese Court (from news)"

            summary = summarize_with_deepseek(
                f"Financial news about Chinese court ruling:\nHeadline: {title}\nQuery: {query}\n"
                f"Published: {pub_date}\n\nNote: This is from a news source. Verify details from official filings.",
                "Chinese court ruling news article")

            embed = {
                "title": f"🇨🇳 China Court News: {title[:200]}",
                "url": link,
                "color": 0xDE2910,  # Chinese red
                "fields": [
                    {"name": "📊 Tickers", "value": tickers_display, "inline": False},
                    {"name": "🏛️ Court", "value": court_info, "inline": True},
                    {"name": "📅 Published", "value": pub_date[:25] if pub_date else "Recent", "inline": True},
                    {"name": "🔍 Source", "value": "Financial News (Google News)", "inline": True},
                    {"name": "📄 Article", "value": f"[Read Full Article]({link})", "inline": False},
                    {"name": "🤖 AI Summary", "value": summary[:1000], "inline": False},
                ],
                "footer": {"text": "China Court Monitor | News Watch"},
                "timestamp": datetime.now(tz=pytz.utc).isoformat()
            }
            send_discord(DISCORD_WEBHOOK_FEDERAL, [embed])
            alerts += 1
            time.sleep(0.5)

    except Exception as e:
        log.debug(f"China news check failed: {e}")

    return alerts


# ── Main Loop ──

def main():
    global PUBLIC_COMPANIES
    log.info("=" * 60)
    log.info("Court Filing Monitor Starting")
    log.info(f"  Base interval: {CHECK_INTERVAL}s (adaptive: 3-15s)")
    log.info(f"  14 sources, each every ~{CHECK_INTERVAL*14}s")
    log.info(f"  CL Token: {'SET' if COURTLISTENER_API_TOKEN else 'NOT SET - get one at courtlistener.com!'}")
    log.info(f"  Hours: 9AM-4PM EST, market days only")
    log.info(f"  Chinese court monitoring: {len(CHINA_LITIGATION_WATCHLIST)} cases tracked")
    log.info("=" * 60)

    if not COURTLISTENER_API_TOKEN:
        log.warning("COURTLISTENER_API_TOKEN not set! Get free token: https://www.courtlistener.com/sign-in/")

    # Load ALL SEC-registered public companies
    PUBLIC_COMPANIES = build_company_db()

    seen = load_seen()
    log.info(f"  Seen file: {SEEN_FILE} ({len(seen)} entries)")
    cycle = 0; cidx = 0; didx = 0; bidx = 0; sidx = 0; gidx = 0; eidx = 0; nidx = 0
    last_save = time.time()

    public_count = len(set(v for v in PUBLIC_COMPANIES.values() if not v.startswith("🔥") and not v.startswith("🏛️")))
    private_count = len([k for k, v in BUZZY_COMPANIES.items() if v.startswith("🔥")])
    startup = {"title": "🟢 Court Filing Monitor Online", "description": (
        f"• Polling: **adaptive** ({CHECK_INTERVAL}s base, 3s peak, 15s throttled)\n"
        f"• Federal courts: **opinions + new dockets** (rulings AND lawsuits)\n"
        f"• 🔴 Macro-market: tariffs, trade, IEEPA, regulatory, antitrust\n"
        f"• 🚨 High-impact: SEC enforcement, DOJ antitrust, class actions\n"
        f"• ⚡ Docket watch: new lawsuits (materiality filtered)\n"
        f"• 🔥 Buzzy privates: OpenAI, xAI, Anthropic, SpaceX, Kalshi, etc.\n"
        f"• 🏛️ State courts: DE Chancery, CA Appellate, NY Court of Appeals\n"
        f"• ⚖️ Gov enforcement: SEC/DOJ/FTC/CFPB/CFTC/EPA/FDA/state AGs\n"
        f"• Supreme Court: opinions + dockets + supremecourt.gov\n"
        f"• CAFC + CIT: patent rulings + trade court (PDF extraction)\n"
        f"• 🇨🇳 China courts: SEC EDGAR 6-K + financial news monitoring\n"
        f"• Companies: **{public_count}** public + **{private_count}** private + **{len(CHINA_LITIGATION_WATCHLIST)}** China cases\n"
        f"• Hours: 9AM-4PM EST\n• CL Auth: {'✅' if COURTLISTENER_API_TOKEN else '⚠️ Not set'}\n"
        f"• AI: DeepSeek | Circuit breaker: enabled"),
        "color": 0x00FF00, "timestamp": datetime.now(tz=pytz.utc).isoformat()}

    # Test Discord connectivity on startup
    r1 = send_discord(DISCORD_WEBHOOK_FEDERAL, [startup])
    r2 = send_discord(DISCORD_WEBHOOK_SCOTUS, [startup])
    log.info(f"  Discord Federal webhook: {'✅ CONNECTED' if r1 else '❌ FAILED'}")
    log.info(f"  Discord SCOTUS webhook: {'✅ CONNECTED' if r2 else '❌ FAILED'}")
    log.info(f"  Discord Mirror webhook: {'✅ SET' if DISCORD_WEBHOOK_MIRROR else '⚠️ Not set'}")
    if not r1: log.error(f"  Federal webhook URL: {DISCORD_WEBHOOK_FEDERAL[:60]}...")
    if not r2: log.error(f"  SCOTUS webhook URL: {DISCORD_WEBHOOK_SCOTUS[:60]}...")

    # If seen file was empty (fresh deploy or first time with volume), do a silent seed cycle
    global SEED_MODE
    if len(seen) == 0:
        SEED_MODE = True
        log.info("🌱 Seed mode: first run with empty seen file — scanning all sources silently first")

    # Total phases including Chinese court monitoring
    TOTAL_PHASES = 14

    while True:
        try:
            if not is_market_hours():
                now = datetime.now(EST)
                if now.minute == 0 and now.second < 35:
                    log.info(f"Outside market hours ({now.strftime('%I:%M %p %Z')})")
                time.sleep(30)
                continue

            # Staggered 14-phase cycle with adaptive intervals
            # Phase 0: Federal opinions (broad)     Phase 1: SCOTUS opinions (CL API)
            # Phase 2: Company opinions (CL API)    Phase 3: SCOTUS docket (CL API)
            # Phase 4: Federal opinions (broad)     Phase 5: SCOTUS website scrape
            # Phase 6: CAFC website scrape           Phase 7: High-impact filings
            # Phase 8: New docket watch (lawsuits)  Phase 9: Buzzy private companies
            # Phase 10: State court rulings (DE/CA/NY rotation)
            # Phase 11: Gov enforcement (SEC/DOJ/FTC/CFPB/FDA/EPA/AG rotation)
            # Phase 12: SEC EDGAR 6-K (Chinese court filings)
            # Phase 13: China news monitor (Google News RSS)
            phase = cycle % TOTAL_PHASES

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
            elif phase == 12:
                n = check_sec_edgar_6k(seen, eidx); eidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} 🇨🇳 EDGAR 6-K alerts")
            elif phase == 13:
                n = check_china_news(seen, nidx); nidx += 1
                if n and not SEED_MODE: log.info(f"Sent {n} 🇨🇳 China news alerts")

            # After completing first full cycle, exit seed mode
            if SEED_MODE and cycle >= TOTAL_PHASES - 1:
                SEED_MODE = False
                save_seen(seen)
                log.info(f"✅ Seed complete — {len(seen)} entries tracked. Now alerting normally.")

            cycle += 1

            if time.time() - last_save > 120:
                save_seen(seen); last_save = time.time()

            if cycle % 50 == 0:
                log.info(f"Status: {rate_limiter.remaining()} CL API left | {rate_limiter.usage_pct():.0f}% used | {rate_limiter.per_minute():.0f} req/min | {len(seen)} tracked")

            # Adaptive polling interval
            interval = get_adaptive_interval()
            time.sleep(interval)

        except KeyboardInterrupt:
            log.info("Shutting down..."); save_seen(seen); break
        except Exception as e:
            log.error(f"Loop error: {e}"); time.sleep(15)

if __name__ == "__main__":
    main()
