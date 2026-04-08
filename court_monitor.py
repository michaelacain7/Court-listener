"""
Court Filing Monitor for Public Companies
==========================================
Monitors:
  1. Federal court filings involving public companies (all federal courts)
  2. Supreme Court opinions/orders mentioning public companies
  3. Health monitoring with Discord alerts

Alerts via Discord webhooks with DeepSeek AI summaries.
Runs 9 AM - 5 PM Eastern (EST/EDT, DST-aware) on market open days only.

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
from collections import deque, OrderedDict
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
# Health alerts webhook — falls back to mirror if not set
DISCORD_HEALTH_WEBHOOK = os.getenv("DISCORD_HEALTH_WEBHOOK", "") or DISCORD_WEBHOOK_MIRROR

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-089099fee3e94c1c97189694645d6f92")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

COURTLISTENER_API_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "b4863b70263a553e98685a87ea755a8ad5b0bfaa")
CL_BASE = "https://www.courtlistener.com"
CL_SEARCH_URL = f"{CL_BASE}/api/rest/v4/search/"
CL_DOCKET_URL = f"{CL_BASE}/api/rest/v4/dockets/"

# Base interval — adaptive polling adjusts this dynamically
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
EST = pytz.timezone("US/Eastern")

# ── DeepSeek Response Cache (LRU) ──

class LRUCache:
    """Simple LRU cache using OrderedDict. Thread-safe."""
    def __init__(self, max_size=300):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _make_key(self, content):
        """Hash first 2000 chars of content for cache key."""
        return hashlib.md5(content[:2000].encode('utf-8', errors='ignore')).hexdigest()

    def get(self, content):
        key = self._make_key(content)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                self.hits += 1
                return self.cache[key]
            self.misses += 1
            return None

    def put(self, content, result):
        key = self._make_key(content)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = result
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

deepseek_cache = LRUCache(max_size=300)

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
                try:
                    health_monitor.alert_circuit_breaker(False)
                except NameError:
                    pass
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
                try:
                    health_monitor.alert_circuit_breaker(True, f"{self._consecutive_429s} consecutive 429s, backoff {wait}s")
                except NameError:
                    pass

    def record_success(self):
        """Record a successful request — resets 429 counter."""
        with self.lock:
            self._consecutive_429s = 0

    def should_throttle(self):
        """Returns True if usage is above 80% — caller should slow down."""
        return self.usage_pct() >= 80

rate_limiter = RateLimiter()

# ── Health Monitor ──

class HealthMonitor:
    """Tracks system health metrics and sends Discord alerts for issues.
    Alerts are color-coded: green=info, yellow=warning, red=error.
    Deduplicates alerts with cooldown periods to avoid spam."""

    def __init__(self):
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.total_api_calls = 0
        self.phase_success = {}   # {phase_name: count}
        self.phase_failure = {}   # {phase_name: count}
        self.consecutive_errors = {}  # {phase_name: count}
        self.api_response_times = deque(maxlen=100)  # last 100 response times
        self.hourly_api_calls = deque()  # timestamps of API calls this hour
        self.last_alert_times = {}  # {alert_key: timestamp} for cooldown
        self.last_summary_time = 0
        self.errors_since_last_summary = []  # [(time, phase, error_msg)]
        self._circuit_breaker_opened_at = 0

    def _should_alert(self, alert_key, cooldown_seconds=900):
        """Check if we should send this alert (15-min cooldown by default)."""
        now = time.time()
        last = self.last_alert_times.get(alert_key, 0)
        if now - last < cooldown_seconds:
            return False
        self.last_alert_times[alert_key] = now
        return True

    def _send_health_alert(self, embed):
        """Send a health alert to the health webhook. Does not mirror."""
        if SEED_MODE:
            return
        if not DISCORD_HEALTH_WEBHOOK:
            return
        try:
            requests.post(DISCORD_HEALTH_WEBHOOK, json={"embeds": [embed]}, timeout=10)
        except Exception as e:
            log.debug(f"Health alert send failed: {e}")

    def record_api_call(self, response_time=None):
        """Record an API call and optionally its response time."""
        with self.lock:
            self.total_api_calls += 1
            now = time.time()
            self.hourly_api_calls.append(now)
            # Prune entries older than 1 hour
            cutoff = now - 3600
            while self.hourly_api_calls and self.hourly_api_calls[0] < cutoff:
                self.hourly_api_calls.popleft()
            if response_time is not None:
                self.api_response_times.append(response_time)

    def record_phase_success(self, phase_name):
        """Record a successful phase execution."""
        with self.lock:
            self.phase_success[phase_name] = self.phase_success.get(phase_name, 0) + 1
            self.consecutive_errors[phase_name] = 0

    def record_phase_failure(self, phase_name, error_msg=""):
        """Record a failed phase execution. Alerts on 3+ consecutive failures."""
        with self.lock:
            self.phase_failure[phase_name] = self.phase_failure.get(phase_name, 0) + 1
            self.consecutive_errors[phase_name] = self.consecutive_errors.get(phase_name, 0) + 1
            consec = self.consecutive_errors[phase_name]
            self.errors_since_last_summary.append((time.time(), phase_name, error_msg[:200]))

        if consec >= 3:
            alert_key = f"consec_err_{phase_name}"
            if self._should_alert(alert_key, cooldown_seconds=900):
                embed = {
                    "title": "🔴 Consecutive Phase Failures",
                    "color": 0xFF0000,
                    "fields": [
                        {"name": "Phase", "value": phase_name, "inline": True},
                        {"name": "Consecutive Failures", "value": str(consec), "inline": True},
                        {"name": "Error", "value": error_msg[:500] or "Unknown", "inline": False},
                    ],
                    "footer": {"text": "Health Monitor"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                self._send_health_alert(embed)

    def check_rate_limit_warning(self):
        """Alert when CourtListener API usage exceeds 70% of hourly limit."""
        pct = rate_limiter.usage_pct()
        remaining = rate_limiter.remaining()
        if pct >= 70:
            alert_key = "rate_limit_warning"
            if self._should_alert(alert_key, cooldown_seconds=900):
                # Project usage: current rate * 60 minutes
                per_min = rate_limiter.per_minute()
                projected = int(per_min * 60)
                color = 0xFF0000 if pct >= 90 else 0xFFFF00
                embed = {
                    "title": "🟡 Rate Limit Warning" if pct < 90 else "🔴 Rate Limit Critical",
                    "color": color,
                    "fields": [
                        {"name": "Current Usage", "value": f"{pct:.0f}% ({rate_limiter.max_per_hour - remaining}/{rate_limiter.max_per_hour})", "inline": True},
                        {"name": "Remaining", "value": str(remaining), "inline": True},
                        {"name": "Projected (hour)", "value": f"~{projected} calls", "inline": True},
                    ],
                    "footer": {"text": "Health Monitor"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                self._send_health_alert(embed)

    def alert_429(self, api_name, endpoint, retry_after=None):
        """Immediately alert on 429 rate limit response."""
        alert_key = f"429_{api_name}"
        if self._should_alert(alert_key, cooldown_seconds=300):
            embed = {
                "title": "🔴 429 Rate Limited",
                "color": 0xFF0000,
                "fields": [
                    {"name": "API", "value": api_name, "inline": True},
                    {"name": "Endpoint", "value": endpoint[:200], "inline": True},
                    {"name": "Retry-After", "value": f"{retry_after}s" if retry_after else "Unknown", "inline": True},
                ],
                "footer": {"text": "Health Monitor"},
                "timestamp": datetime.now(tz=pytz.utc).isoformat()
            }
            self._send_health_alert(embed)

    def alert_circuit_breaker(self, opened, reason=""):
        """Alert when circuit breaker opens or closes."""
        if opened:
            self._circuit_breaker_opened_at = time.time()
            alert_key = "circuit_breaker_open"
            if self._should_alert(alert_key, cooldown_seconds=300):
                embed = {
                    "title": "🔴 Circuit Breaker OPENED",
                    "color": 0xFF0000,
                    "fields": [
                        {"name": "Reason", "value": reason[:500] or "Rate limit threshold exceeded", "inline": False},
                    ],
                    "footer": {"text": "Health Monitor"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                self._send_health_alert(embed)
        else:
            duration = time.time() - self._circuit_breaker_opened_at if self._circuit_breaker_opened_at else 0
            alert_key = "circuit_breaker_close"
            if self._should_alert(alert_key, cooldown_seconds=60):
                embed = {
                    "title": "🟢 Circuit Breaker Closed",
                    "color": 0x00FF00,
                    "fields": [
                        {"name": "Duration", "value": f"{duration:.0f}s" if duration else "Unknown", "inline": True},
                    ],
                    "footer": {"text": "Health Monitor"},
                    "timestamp": datetime.now(tz=pytz.utc).isoformat()
                }
                self._send_health_alert(embed)

    def alert_connectivity(self, service, error_type, error_msg):
        """Alert on connection timeouts, DNS failures, or SSL errors."""
        alert_key = f"connectivity_{service}"
        if self._should_alert(alert_key, cooldown_seconds=900):
            embed = {
                "title": "🔴 API Connectivity Issue",
                "color": 0xFF0000,
                "fields": [
                    {"name": "Service", "value": service, "inline": True},
                    {"name": "Error Type", "value": error_type, "inline": True},
                    {"name": "Details", "value": error_msg[:500], "inline": False},
                ],
                "footer": {"text": "Health Monitor"},
                "timestamp": datetime.now(tz=pytz.utc).isoformat()
            }
            self._send_health_alert(embed)

    def maybe_send_summary(self):
        """Send a 2-hour health summary during trading hours."""
        now = time.time()
        if now - self.last_summary_time < 7200:  # 2 hours
            return
        if not is_market_hours():
            return
        self.last_summary_time = now

        uptime_s = now - self.start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)

        with self.lock:
            hourly_calls = len(self.hourly_api_calls)
            avg_response = 0.0
            if self.api_response_times:
                avg_response = sum(self.api_response_times) / len(self.api_response_times)
            total_successes = sum(self.phase_success.values())
            total_failures = sum(self.phase_failure.values())
            recent_errors = [(t, p, e) for t, p, e in self.errors_since_last_summary if now - t < 7200]
            self.errors_since_last_summary = []

        remaining = rate_limiter.remaining()
        pct = rate_limiter.usage_pct()

        error_summary = "None"
        if recent_errors:
            error_lines = []
            for _, phase, msg in recent_errors[-5:]:
                error_lines.append(f"• {phase}: {msg[:80]}")
            error_summary = "\n".join(error_lines)

        slow_flag = ""
        if avg_response > 5.0:
            slow_flag = " ⚠️ SLOW"

        embed = {
            "title": "🟢 System Health Summary",
            "color": 0x00FF00 if total_failures == 0 else 0xFFFF00,
            "fields": [
                {"name": "Uptime", "value": f"{hours}h {minutes}m", "inline": True},
                {"name": "API Calls (hour)", "value": str(hourly_calls), "inline": True},
                {"name": "Rate Limit", "value": f"{remaining} left ({pct:.0f}% used)", "inline": True},
                {"name": "Phases Completed", "value": str(total_successes), "inline": True},
                {"name": "Phase Failures", "value": str(total_failures), "inline": True},
                {"name": "Avg Response", "value": f"{avg_response:.1f}s{slow_flag}", "inline": True},
                {"name": "DeepSeek Cache", "value": f"Hits: {deepseek_cache.hits} | Misses: {deepseek_cache.misses}", "inline": True},
                {"name": "Errors (2h window)", "value": error_summary[:500], "inline": False},
            ],
            "footer": {"text": "Health Monitor | 2-hour summary"},
            "timestamp": datetime.now(tz=pytz.utc).isoformat()
        }
        self._send_health_alert(embed)

health_monitor = HealthMonitor()

# ── Market Holidays (Dynamic Calculation) ──
# Computes US market holidays for any year using observed-date rules.
# Includes: New Year's Day, MLK Day, Presidents' Day, Good Friday,
# Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.

from datetime import date

def _observed_date(d):
    """Apply NYSE observed-date rules: if a holiday falls on Saturday, the
    preceding Friday is observed; if on Sunday, the following Monday."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    elif d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d

def _nth_weekday(year, month, weekday, n):
    """Return the n-th occurrence of a weekday in a given month (1-indexed)."""
    first = date(year, month, 1)
    # Days until first target weekday
    delta = (weekday - first.weekday()) % 7
    first_occurrence = first + timedelta(days=delta)
    return first_occurrence + timedelta(weeks=n - 1)

def _last_weekday(year, month, weekday):
    """Return the last occurrence of a weekday in a given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=delta)

def _easter(year):
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)

def _good_friday(year):
    """Return Good Friday (2 days before Easter Sunday)."""
    return _easter(year) - timedelta(days=2)

def get_market_holidays(year):
    """Return a set of 'YYYY-MM-DD' strings for all US market holidays in a year."""
    holidays = set()

    # New Year's Day — Jan 1
    holidays.add(_observed_date(date(year, 1, 1)))
    # MLK Day — 3rd Monday in January
    holidays.add(_nth_weekday(year, 1, 0, 3))  # Monday=0
    # Presidents' Day — 3rd Monday in February
    holidays.add(_nth_weekday(year, 2, 0, 3))
    # Good Friday
    holidays.add(_good_friday(year))
    # Memorial Day — last Monday in May
    holidays.add(_last_weekday(year, 5, 0))
    # Juneteenth — June 19
    holidays.add(_observed_date(date(year, 6, 19)))
    # Independence Day — July 4
    holidays.add(_observed_date(date(year, 7, 4)))
    # Labor Day — 1st Monday in September
    holidays.add(_nth_weekday(year, 9, 0, 1))
    # Thanksgiving — 4th Thursday in November
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thursday=3
    # Christmas — Dec 25
    holidays.add(_observed_date(date(year, 12, 25)))

    return {d.strftime("%Y-%m-%d") for d in holidays}

def _build_holiday_cache():
    """Pre-build holiday sets for current year +/- 1 to cover year boundaries."""
    now = datetime.now(EST)
    cache = set()
    for y in range(now.year - 1, now.year + 2):
        cache.update(get_market_holidays(y))
    return cache

MARKET_HOLIDAYS = _build_holiday_cache()

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

def is_trading_hours():
    """Check if current Eastern time is within trading hours.
    Active: Monday-Friday, 9:00 AM - 5:00 PM ET (handles EST/EDT via pytz).
    Excludes weekends and US market holidays (dynamically computed)."""
    now = datetime.now(EST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:
        return False
    if now.hour < 9 or now.hour >= 17:
        return False
    return True

# Backward-compatible alias used by health monitor
is_market_hours = is_trading_hours

def get_adaptive_interval():
    """Return polling interval based on time of day and rate limit usage.
    Faster during market open/close (high-activity windows), slower if approaching limits."""
    if rate_limiter.should_throttle():
        log.warning(f"Rate limit at {rate_limiter.usage_pct():.0f}% — throttling to 15s")
        return 15

    now = datetime.now(EST)
    hour, minute = now.hour, now.minute

    # Peak periods: market open (9:30-10:00) and close (4:30-5:00)
    if (hour == 9 and minute >= 30) or (hour == 10 and minute < 0):
        return max(3, CHECK_INTERVAL - 2)
    if (hour == 16 and minute >= 30) or hour == 17:
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
        health_monitor.check_rate_limit_warning()
        time.sleep(30)
        if not rate_limiter.can_request():
            log.error("Rate limit exhausted. Skipping.")
            return None
    rate_limiter.record()
    health_monitor.record_api_call()
    for attempt in range(3):
        try:
            t0 = time.time()
            resp = requests.get(url, params=params, headers=get_cl_headers(), timeout=timeout)
            elapsed = time.time() - t0
            health_monitor.record_api_call(response_time=elapsed)
            if resp.status_code == 429:
                rate_limiter.record_429()
                retry_after = 60
                try:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                except (ValueError, TypeError):
                    pass
                health_monitor.alert_429("CourtListener", url[:100], retry_after)
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
            health_monitor.alert_connectivity("CourtListener", "Timeout", f"Request timed out after {timeout}s: {url[:100]}")
            time.sleep(5 * (attempt + 1))
        except requests.exceptions.ConnectionError as e:
            log.error(f"CL connection error: {e}")
            health_monitor.alert_connectivity("CourtListener", "ConnectionError", str(e)[:300])
            time.sleep(5 * (attempt + 1))
        except requests.exceptions.RequestException as e:
            log.error(f"CL request failed: {e}")
            err_type = type(e).__name__
            if "SSL" in str(e) or "ssl" in str(e):
                err_type = "SSLError"
            health_monitor.alert_connectivity("CourtListener", err_type, str(e)[:300])
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

# ── DeepSeek API Calls (Optimized with prefix caching, truncation, LRU cache) ──

# Static system prompts — extracted so DeepSeek can cache them (90-95% discount on cached input tokens)
_SYSTEM_PROMPT_SUMMARIZE = (
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
)

_SYSTEM_PROMPT_MATERIALITY = (
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
)


def summarize_with_deepseek(text, context="court filing"):
    """Summarize court text using DeepSeek with optimizations:
    - LRU cache to avoid duplicate API calls
    - Input truncation to 4000 chars max
    - Prefix caching via static system prompt
    - Reduced max_tokens (500 -> 400)
    """
    if not text or len(text.strip()) < 50:
        return "No substantial text available for summarization."

    # Truncate input to 4000 chars to reduce token usage
    if len(text) > 4000:
        text = text[:4000] + "..."

    # Check LRU cache first
    cache_key_content = f"summarize:{context}:{text}"
    cached = deepseek_cache.get(cache_key_content)
    if cached:
        return cached

    try:
        resp = requests.post(DEEPSEEK_API_URL, headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }, json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT_SUMMARIZE},
                {"role": "user", "content": f"Summarize this {context}:\n\n{text}"}
            ],
            "max_tokens": 400, "temperature": 0.2
        }, timeout=30)
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"]
        deepseek_cache.put(cache_key_content, result)
        return result
    except Exception as e:
        log.error(f"DeepSeek failed: {e}")
        return "AI summary unavailable - see original filing."

def check_materiality(case_name, court="", snippet="", filing_type="new lawsuit", docket_id=None):
    """Multi-layer materiality filter for new case filings.
    Layer 1: Fast local pattern match (no API call needed)
    Layer 2: Fetch CL docket details for nature-of-suit code
    Layer 3: DeepSeek classification with actual context
    Returns (is_material: bool, reason: str).

    Optimized: LRU cache, input truncation, prefix caching, reduced max_tokens.
    """

    cn = case_name.strip()
    cn_lower = cn.lower()

    # ── Layer 1: Fast local pre-filter ──
    parts = re.split(r'\s+v\.?\s+', cn, maxsplit=1, flags=re.IGNORECASE)
    is_class = False

    if len(parts) == 2:
        plaintiff = parts[0].strip()
        defendant = parts[1].strip()

        govt_patterns = [
            "united states", "securities and exchange", "federal trade",
            "department of", "state of", "commonwealth of", "people of",
            "attorney general", "consumer financial", "commodity futures",
            "national labor", "environmental protection", "food and drug",
            "in re ", "in the matter of",
        ]
        is_govt_plaintiff = any(gp in plaintiff.lower() for gp in govt_patterns)

        company_indicators = ["inc", "llc", "corp", "ltd", "l.p.", "co.", "company",
                              "group", "holdings", "partners", "fund", "bank",
                              "insurance", "financial", "technologies", "pharma",
                              "systems", "enterprises", "international", "association"]
        is_company_plaintiff = any(ci in plaintiff.lower() for ci in company_indicators)

        class_indicators = ["in re", "mdl", "consolidated", "class action",
                            "securities litigation", "antitrust litigation",
                            "products liability", "on behalf of"]
        is_class = any(ci in cn_lower for ci in class_indicators)

        if not is_govt_plaintiff and not is_company_plaintiff and not is_class:
            plaintiff_words = [w for w in plaintiff.split() if len(w) > 1]
            if len(plaintiff_words) <= 3 and "," not in plaintiff:
                log.info(f"  PRE-FILTER: Individual plaintiff '{plaintiff}' → SKIP")
                return False, f"NO - Individual plaintiff ({plaintiff}) v. company - not material"
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

                nos_lower = nature_of_suit.lower()
                immaterial_nos = [
                    "motor vehicle", "personal injury", "product liability",
                    "assault", "slander", "libel", "marine",
                    "airplane", "medical malpractice", "personal property",
                    "insurance", "negotiable instrument", "student loan",
                    "other personal property", "prop. damage",
                ]
                if any(nos in nos_lower for nos in immaterial_nos):
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

        # Check LRU cache for materiality checks
        cache_key_content = f"materiality:{context_block}"
        cached = deepseek_cache.get(cache_key_content)
        if cached:
            is_material = cached.upper().startswith("YES")
            return is_material, cached

        resp = requests.post(DEEPSEEK_API_URL, headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"
        }, json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT_MATERIALITY},
                {"role": "user", "content": context_block}
            ],
            "max_tokens": 60, "temperature": 0.1
        }, timeout=15)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        deepseek_cache.put(cache_key_content, answer)
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
                        pass
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
    if item.get("absolute_url"):
        url = item["absolute_url"]
        if not url.startswith("http"):
            url = f"{CL_BASE}{url}"
        return url
    if search_type == "d" and item.get("docket_id"):
        slug = re.sub(r'[^a-z0-9]+', '-', (item.get("caseName","") or item.get("case_name","")).lower()).strip('-')[:60]
        return f"{CL_BASE}/docket/{item['docket_id']}/{slug}/"
    if item.get("cluster_id"):
        return f"{CL_BASE}/opinion/{item['cluster_id']}/"
    if item.get("id") and search_type == "o":
        return f"{CL_BASE}/opinion/{item['id']}/"
    cn = item.get("caseName","") or item.get("case_name","")
    if cn:
        q = requests.utils.quote(f'{cn} court filing')
        return f"https://www.google.com/search?q={q}"
    return ""

def extract_pdf_text(pdf_url, max_pages=10, max_chars=4000):
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
    snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()

    opinion_id = item.get("id","") or item.get("opinion_id","")
    if opinion_id and rate_limiter.remaining() > 200:
        od = cl_request(f"{CL_BASE}/api/rest/v4/opinions/{opinion_id}/",
            params={"fields":"id,plain_text,html,download_url"}, timeout=15)
        if od:
            if od.get("plain_text") and len(od["plain_text"]) > 200:
                log.info(f"  Fetched opinion text: {len(od['plain_text'])} chars")
                return od["plain_text"][:4000]
            if od.get("html") and len(od["html"]) > 200:
                text = re.sub(r'<[^>]+>','',od["html"])
                log.info(f"  Fetched opinion HTML: {len(text)} chars")
                return text[:4000]

    cid = item.get("cluster_id","")
    if cid and rate_limiter.remaining() > 200:
        cd = cl_request(f"{CL_BASE}/api/rest/v4/clusters/{cid}/",
            params={"fields":"id,case_name,syllabus,judges,date_filed,sub_opinions"}, timeout=15)
        if cd:
            if cd.get("syllabus") and len(cd["syllabus"]) > 100:
                log.info(f"  Fetched cluster syllabus: {len(cd['syllabus'])} chars")
                return cd["syllabus"][:4000]
            sub_ops = cd.get("sub_opinions", [])
            if sub_ops and rate_limiter.remaining() > 200:
                for sub_url in sub_ops[:2]:
                    if isinstance(sub_url, str) and "/opinions/" in sub_url:
                        sod = cl_request(sub_url,
                            params={"fields":"id,plain_text,html"}, timeout=15)
                        if sod:
                            if sod.get("plain_text") and len(sod["plain_text"]) > 200:
                                log.info(f"  Fetched sub-opinion text: {len(sod['plain_text'])} chars")
                                return sod["plain_text"][:4000]
                            if sod.get("html") and len(sod["html"]) > 200:
                                text = re.sub(r'<[^>]+>','',sod["html"])
                                return text[:4000]

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
            snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
            macro_matches = match_macro_keywords(cn + " " + snippet)
            if not macro_matches:
                continue
            is_macro = True

        log.info(f"FEDERAL {'MACRO ' if is_macro else ''}OPINION: {cn[:80]}")
        details = []
        for k,l in [("court","Court"),("dateFiled","Filed"),("citation","Citation"),("suitNature","Nature")]:
            if item.get(k): details.append(f"**{l}:** {item[k]}")
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

    SKIP_PATTERNS = re.compile(
        r'(trust|fund|etf|series|warrant|preferred|right|note|bond|debenture|'
        r'municipal|income\s+trust|capital\s+trust|floating|variable|'
        r'strats|eaton\s+vance|calamos|nuveen|blackrock\s+muni|pimco|'
        r'rivernorth|doubleline|closed.end|convertible|perpetual)',
        re.IGNORECASE)

    if idx < len(PRIORITY_COMPANIES):
        co = PRIORITY_COMPANIES[idx % len(PRIORITY_COMPANIES)]
    else:
        sec_names = sorted(set(PUBLIC_COMPANIES.keys()), key=len, reverse=False)
        searchable = [n for n in sec_names
            if len(n) >= 5 and len(n) <= 40
            and not SKIP_PATTERNS.search(n)
            and '/' not in n and '&' not in n
        ]
        if not searchable:
            return 0
        adjusted = (idx - len(PRIORITY_COMPANIES))
        co = searchable[adjusted % len(searchable)]

    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")

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
        if co.lower() not in cn.lower():
            search_ticker = PUBLIC_COMPANIES.get(co, "")
            case_tickers = [t for _, t in matches]
            if search_ticker not in case_tickers:
                continue
        log.info(f"OPINION [{co}]: {cn[:80]}")
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
    queries = [
        '"Securities and Exchange Commission" enforcement',
        '"Department of Justice" antitrust',
        '"Federal Trade Commission" complaint',
        '"class action" "class certification"',
        "securities fraud settlement approval",
        '"preliminary injunction" patent infringement',
        '"consent decree" antitrust',
        "qui tam whistleblower fraud",
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
            snippet_raw = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
            macro_matches = match_macro_keywords(cn + " " + snippet_raw + " " + q)
            if not macro_matches:
                continue
            is_macro = True
        else:
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
        summary = summarize_with_deepseek(f"High-impact filing ({q}):\nCase: {cn}\n{snippet[:4000]}", "high-impact court filing")

        if matches:
            tstr = format_tickers(matches)
        else:
            tstr = " | ".join(sector for _, sector in macro_matches[:3])

        doc_url = get_filing_url(item, "r")
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
    Uses type=d (dockets) to catch new complaints, not just opinions."""
    yesterday = (datetime.now(EST) - timedelta(days=1)).strftime("%m/%d/%Y")

    DOCKET_WATCH_NAMES = [
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
        matches = match_public_company(cn)
        if not matches: continue
        if co.lower() not in cn.lower():
            search_ticker = PUBLIC_COMPANIES.get(co, "")
            case_tickers = [t for _, t in matches]
            if search_ticker not in case_tickers:
                continue
        log.info(f"NEW DOCKET [{co}]: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()

        is_material, reason = check_materiality(
            cn, court=item.get("court",""), snippet=snippet,
            filing_type="new lawsuit", docket_id=item.get("docket_id")
        )
        if not is_material:
            log.info(f"  SKIPPED (not material): {reason[:80]}")
            continue

        summary = summarize_with_deepseek(
            f"New lawsuit/case filed:\nCase: {cn}\nCourt: {item.get('court','')}\nFiled: {item.get('dateFiled','')}\n\n{snippet[:4000]}",
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
        if co.lower() not in cn.lower(): continue
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
        if not matches: continue
        log.info(f"SCOTUS DOCKET: {cn[:80]}")
        snippet = re.sub(r'<[^>]+>','',item.get("snippet","") or "").strip()
        summary = summarize_with_deepseek(f"SCOTUS Docket: {cn}\n\n{snippet[:4000]}", "Supreme Court docket entry")
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
        now = datetime.now(EST)
        term_year = now.year if now.month >= 10 else now.year - 1
        term_str = str(term_year)[-2:]
        scotus_url = f"https://www.supremecourt.gov/opinions/slipopinion/{term_str}"

        resp = requests.get(scotus_url,
            timeout=15, headers={"User-Agent":"CourtFilingMonitor/1.0"})
        if not resp.ok:
            log.warning(f"SCOTUS.gov returned {resp.status_code} for term {term_str}")
            return 0
        from html.parser import HTMLParser

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

        docket_pattern = re.compile(r'\d{2,4}-\d{1,5}')
        valid_rows = [(i, row) for i, row in enumerate(p.rows) if any(docket_pattern.search(cell) for cell in row)]

        scotus_seen_count = sum(1 for k in seen if k.startswith("scotusgov_"))
        is_first_run = scotus_seen_count == 0

        for i, row in valid_rows[-20:]:
            rt = " ".join(row)
            fid = f"scotusgov_{hashlib.md5(rt.encode()).hexdigest()}"
            if fid in seen: continue
            mark_seen(seen, fid)

            if is_first_run:
                log.info(f"SCOTUS.GOV [seed]: {rt[:80]}")
                continue

            log.info(f"SCOTUS.GOV: {rt[:80]}")
            matches = match_public_company(rt)

            pdf_url = ""
            docket_match = docket_pattern.search(rt)
            if docket_match and pdf_links:
                docket_num = docket_match.group()
                for pl in pdf_links:
                    if docket_num.replace("-", "") in pl.replace("-", "") or docket_num in pl:
                        pdf_url = f"https://www.supremecourt.gov{pl}"
                        break

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
                            summary_text = f"Supreme Court Opinion:\n{rt}\n\nFull text:\n{full_text[:4000]}"
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

        for url, title, pdf_url in entries[:20]:
            fid = f"cafc_{hashlib.md5((url + title).encode()).hexdigest()}"
            if fid in seen:
                continue
            mark_seen(seen, fid)

            case_name = re.sub(r'\s*\[(OPINION|ORDER|RULE\s*36\s*JUDGMENT|ERRATA)\].*$', '', title, flags=re.IGNORECASE).strip()

            doc_type = "Opinion"
            if "[ORDER]" in title.upper():
                doc_type = "Order"
            elif "RULE 36" in title.upper():
                doc_type = "Rule 36 Judgment"

            matches = match_public_company(case_name)
            macro_matches = []
            is_macro = False

            if not matches:
                macro_matches = match_macro_keywords(case_name + " " + title)

            full_text = ""
            if pdf_url or url.endswith('.pdf'):
                actual_pdf = pdf_url or url
                full_text = extract_pdf_text(actual_pdf)
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

            if not matches and not macro_matches and full_text:
                macro_matches = match_macro_keywords(full_text[:3000])

            if not matches and not macro_matches:
                continue

            is_macro = bool(macro_matches) and not matches
            log.info(f"CAFC {'MACRO' if is_macro else ''}: {case_name[:80]}")

            if full_text and len(full_text) > 300:
                summary_text = f"CAFC {doc_type}:\n{case_name}\n\nFull text (first pages):\n{full_text[:4000]}"
            else:
                summary_text = f"Federal Circuit (CAFC) {doc_type}:\n{case_name}\nURL: {url}\n\nWARNING: Full text unavailable. Do NOT guess the outcome."

            summary = summarize_with_deepseek(summary_text, "Federal Circuit opinion")

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
                f"Delaware {court_label} Court Opinion\nCase: {case_name}\nFiled: {date_str}\n\n{opinion_text[:4000]}",
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

        if agency_query.lower() not in cn.lower():
            if agency_label == "DOJ/USA" and "united states" not in cn.lower():
                continue
            elif agency_label != "DOJ/USA":
                continue

        matches = match_public_company(cn)

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
            f"{snippet[:4000]}"
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


# ── Main Loop ──

def main():
    global PUBLIC_COMPANIES
    TOTAL_PHASES = 12
    log.info("=" * 60)
    log.info("Court Filing Monitor Starting")
    log.info(f"  Base interval: {CHECK_INTERVAL}s (adaptive: 3-15s)")
    log.info(f"  {TOTAL_PHASES} sources, each every ~{CHECK_INTERVAL*TOTAL_PHASES}s")
    log.info(f"  CL Token: {'SET' if COURTLISTENER_API_TOKEN else 'NOT SET - get one at courtlistener.com!'}")
    log.info(f"  Hours: 9AM-5PM ET (DST-aware), market days only")
    log.info(f"  Health webhook: {'SET' if DISCORD_HEALTH_WEBHOOK else 'NOT SET'}")
    log.info(f"  DeepSeek cache: LRU (max 300 entries)")
    log.info("=" * 60)

    if not COURTLISTENER_API_TOKEN:
        log.warning("COURTLISTENER_API_TOKEN not set! Get free token: https://www.courtlistener.com/sign-in/")

    # Load ALL SEC-registered public companies
    PUBLIC_COMPANIES = build_company_db()

    seen = load_seen()
    log.info(f"  Seen file: {SEEN_FILE} ({len(seen)} entries)")
    cycle = 0; cidx = 0; didx = 0; bidx = 0; sidx = 0; gidx = 0
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
        f"• 🩺 Health monitor: rate limits, errors, 2h summaries\n"
        f"• 💾 DeepSeek cache: LRU (300 entries) for cost reduction\n"
        f"• Companies: **{public_count}** public + **{private_count}** private\n"
        f"• Hours: 9AM-5PM ET\n• CL Auth: {'✅' if COURTLISTENER_API_TOKEN else '⚠️ Not set'}\n"
        f"• AI: DeepSeek (optimized: prefix caching + LRU + truncation)"),
        "color": 0x00FF00, "timestamp": datetime.now(tz=pytz.utc).isoformat()}

    r1 = send_discord(DISCORD_WEBHOOK_FEDERAL, [startup])
    r2 = send_discord(DISCORD_WEBHOOK_SCOTUS, [startup])
    log.info(f"  Discord Federal webhook: {'✅ CONNECTED' if r1 else '❌ FAILED'}")
    log.info(f"  Discord SCOTUS webhook: {'✅ CONNECTED' if r2 else '❌ FAILED'}")
    log.info(f"  Discord Mirror webhook: {'✅ SET' if DISCORD_WEBHOOK_MIRROR else '⚠️ Not set'}")
    if not r1: log.error(f"  Federal webhook URL: {DISCORD_WEBHOOK_FEDERAL[:60]}...")
    if not r2: log.error(f"  SCOTUS webhook URL: {DISCORD_WEBHOOK_SCOTUS[:60]}...")

    global SEED_MODE
    if len(seen) == 0:
        SEED_MODE = True
        log.info("🌱 Seed mode: first run with empty seen file — scanning all sources silently first")

    PHASE_NAMES = {
        0: "Federal Opinions", 1: "SCOTUS Opinions", 2: "Company Opinions",
        3: "SCOTUS Docket", 4: "Federal Opinions", 5: "SCOTUS Website",
        6: "CAFC Website", 7: "High-Impact", 8: "New Dockets",
        9: "Buzzy Privates", 10: "State Courts", 11: "Gov Enforcement",
    }

    while True:
        try:
            if not is_trading_hours():
                now = datetime.now(EST)
                if now.minute == 0 and now.second < 35:
                    log.info(f"Outside trading hours ({now.strftime('%I:%M %p %Z')}) — skipping cycle. Active: Mon-Fri 9AM-5PM ET, excl. holidays.")
                time.sleep(30)
                continue

            phase = cycle % TOTAL_PHASES
            phase_name = PHASE_NAMES.get(phase, f"Phase {phase}")
            n = 0

            try:
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

                health_monitor.record_phase_success(phase_name)
            except Exception as phase_err:
                log.error(f"Phase {phase} ({phase_name}) error: {phase_err}")
                health_monitor.record_phase_failure(phase_name, str(phase_err))

            health_monitor.check_rate_limit_warning()
            health_monitor.maybe_send_summary()

            if SEED_MODE and cycle >= TOTAL_PHASES - 1:
                SEED_MODE = False
                save_seen(seen)
                log.info(f"✅ Seed complete — {len(seen)} entries tracked. Now alerting normally.")

            cycle += 1

            if time.time() - last_save > 120:
                save_seen(seen); last_save = time.time()

            if cycle % 50 == 0:
                log.info(f"Status: {rate_limiter.remaining()} CL API left | {rate_limiter.usage_pct():.0f}% used | {rate_limiter.per_minute():.0f} req/min | {len(seen)} tracked | Cache hits: {deepseek_cache.hits}")

            interval = get_adaptive_interval()
            time.sleep(interval)

        except KeyboardInterrupt:
            log.info("Shutting down..."); save_seen(seen); break
        except Exception as e:
            log.error(f"Loop error: {e}"); time.sleep(15)

if __name__ == "__main__":
    main()
