"""
Newsletter Monitor Bot
======================
Monitors newsletter/trading alert websites for changes using persistent browser sessions.
Sends alerts via Discord webhooks when new content is detected.

Railway deployment:
  - Reads secrets from env vars ($VAR_NAME in config.json)
  - Health check HTTP server on $PORT
  - AI self-diagnostics via Claude API every 30min
  - Active only 9:00 AM – 4:00 PM EST (configurable)

Usage:
    python monitor.py                  # Run (default config.json)
    python monitor.py --config x.json  # Custom config
    python monitor.py --login-only     # Login and save session
    python monitor.py --test-webhook   # Test Discord delivery
    python monitor.py --visible        # Show browser window (local dev)
"""

import asyncio
import hashlib
import json
import logging
import argparse
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page

try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("newsletter-monitor")

# ─── In-memory log ring buffer for AI health checks ───────────────────────────
LOG_BUFFER = deque(maxlen=200)

class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))

_bh = BufferHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_bh)


# ─── Config Helpers ────────────────────────────────────────────────────────────
def resolve_env_vars(obj):
    """Recursively resolve $ENV_VAR references in config values."""
    if isinstance(obj, str):
        if obj.startswith("$"):
            var_name = obj[1:]
            val = os.environ.get(var_name, "")
            if not val:
                log.warning(f"Env var {var_name} not set")
            return val
        # Also handle inline $VAR in strings
        def _sub(m):
            return os.environ.get(m.group(1), m.group(0))
        return re.sub(r'\$([A-Z_][A-Z0-9_]*)', _sub, obj)
    elif isinstance(obj, dict):
        return {k: resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_env_vars(v) for v in obj]
    return obj


def load_config(path: str) -> dict:
    with open(path) as f:
        raw = json.load(f)
    return resolve_env_vars(raw)


# ─── Timezone Helpers ──────────────────────────────────────────────────────────
def get_tz(tz_name: str):
    """Get timezone object from name."""
    if HAS_ZONEINFO:
        return ZoneInfo(tz_name)
    if HAS_PYTZ:
        return pytz.timezone(tz_name)
    # Fallback: assume US/Eastern = UTC-5 (ignores DST)
    if "eastern" in tz_name.lower():
        return timezone(timedelta(hours=-5))
    return timezone.utc


def now_in_tz(tz_name: str) -> datetime:
    tz = get_tz(tz_name)
    return datetime.now(tz)


# ─── Health Check HTTP Server (Railway needs a port listener) ──────────────────
class HealthStatus:
    """Shared health state between monitor and HTTP server."""
    def __init__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_cycle = None
        self.last_cycle_ok = False
        self.total_cycles = 0
        self.total_changes = 0
        self.errors = deque(maxlen=20)
        self.is_sleeping = False
        self.ai_last_check = None
        self.ai_last_status = "pending"
        self.sites_status = {}

    def to_dict(self):
        return {
            "status": "healthy" if self.last_cycle_ok or self.is_sleeping else "degraded",
            "mode": "sleeping" if self.is_sleeping else "active",
            "started_at": self.started_at,
            "last_cycle": self.last_cycle,
            "total_cycles": self.total_cycles,
            "total_changes_detected": self.total_changes,
            "recent_errors": list(self.errors)[-5:],
            "ai_health": {
                "last_check": self.ai_last_check,
                "status": self.ai_last_status,
            },
            "sites": self.sites_status,
        }


HEALTH = HealthStatus()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(HEALTH.to_dict(), indent=2).encode())

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def start_health_server(port: int):
    """Start health check HTTP server in a background thread."""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Health check server running on port {port}")


# ─── Discord Webhook ──────────────────────────────────────────────────────────
async def send_discord_alert(
    webhook_url: str,
    title: str,
    description: str,
    url: str = "",
    color: int = 0x00FF00,
    fields: list[dict] = None,
):
    """Send an embed message to a Discord webhook."""
    if not webhook_url:
        return
    embed = {
        "title": (title or "Alert")[:256],
        "description": (description or "Change detected")[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url and url.startswith("http"):
        embed["url"] = url
    if fields:
        # Discord requires non-empty name and value for each field
        valid_fields = []
        for f in fields[:25]:
            fname = str(f.get("name", "") or "").strip()
            fvalue = str(f.get("value", "") or "").strip()
            if fname and fvalue:
                valid_fields.append({
                    "name": fname[:256],
                    "value": fvalue[:1024],
                    "inline": bool(f.get("inline", False)),
                })
        if valid_fields:
            embed["fields"] = valid_fields

    payload = {"embeds": [embed]}

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status == 204:
                        log.info(f"Discord alert sent: {title}")
                        return True
                    elif resp.status == 429:
                        retry_after = (await resp.json()).get("retry_after", 5)
                        log.warning(f"Rate limited, retrying in {retry_after}s")
                        await asyncio.sleep(retry_after)
                    elif resp.status == 400:
                        body = await resp.text()
                        log.error(f"Discord webhook 400: {body}")
                        log.error(f"Discord payload debug: {json.dumps(payload, default=str)[:2000]}")
                        return False
                    else:
                        body = await resp.text()
                        log.error(f"Discord webhook {resp.status}: {body}")
                        return False
        except Exception as e:
            log.error(f"Discord send error (attempt {attempt+1}): {e}")
            await asyncio.sleep(2)
    return False


# ─── AI Health Monitor ─────────────────────────────────────────────────────────
class AIHealthMonitor:
    """
    Uses DeepSeek API to analyze logs and verify the monitor is working correctly.
    Runs every N minutes during active hours. Reports issues to Discord.
    """

    def __init__(self, config: dict, webhooks: dict):
        self.enabled = config.get("enabled", False)
        self.interval = config.get("interval_minutes", 30) * 60
        self.api_key = config.get("api_key", "")
        self.api_url = config.get("api_url", "https://api.deepseek.com/v1/chat/completions")
        self.model = config.get("model", "deepseek-chat")
        self.webhooks = webhooks

        if self.enabled and not self.api_key:
            log.warning("AI health check enabled but DEEPSEEK_API_KEY not set — disabling")
            self.enabled = False

    async def run_check(self) -> dict:
        """Run an AI-powered health check by analyzing recent logs."""
        if not self.enabled:
            return {"status": "disabled"}

        # Gather context
        recent_logs = "\n".join(LOG_BUFFER)
        health_data = json.dumps(HEALTH.to_dict(), indent=2)

        prompt = f"""You are a DevOps monitoring assistant. Analyze the following system state and recent logs from a Newsletter Monitor Bot. This bot:
- Monitors newsletter websites (Paradigm Press, Banyan Hill, InvestorPlace) for trading alert changes
- Uses Playwright browser automation to stay logged in
- Sends Discord alerts when changes are detected, with DeepSeek AI analysis of stock recommendations
- Runs 9 AM – 4 PM EST weekdays only (market hours)
- Monitors ~20 pages across 3 sites

IMPORTANT - these are NORMAL operations, do NOT flag them:
- Discord rate limits with automatic retry (bot handles these gracefully)
- "CHANGE DETECTED" followed by successful or failed Discord sends
- Occasional single-page errors that self-resolve next cycle
- First 1-3 cycles after deploy showing many changes (hash recalibration)
- A few 400 errors from Discord during burst alerts

Only flag as "warning" if:
- A site consistently fails to log in for 5+ consecutive cycles
- Multiple pages show errors for 5+ consecutive cycles
- Zero cycles completing for several minutes

Only flag as "critical" if:
- The bot has completely stopped cycling
- All sites are failing simultaneously
- Repeated crash/restart loops visible in logs

Respond with JSON only:
- "status": "healthy" | "warning" | "critical"
- "issues": [list of REAL issues only, empty if healthy]
- "recommendations": [only if warning/critical]
- "summary": one-sentence assessment

HEALTH STATE:
{health_data}

RECENT LOGS (last ~200 entries):
{recent_logs[-8000:]}

Respond with ONLY the JSON object, no markdown fences."""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 1000,
                        "temperature": 0.3,
                        "messages": [
                            {"role": "system", "content": "You are a DevOps monitoring assistant. Respond only with valid JSON."},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        log.error(f"AI health check API error {resp.status}: {err}")
                        return {"status": "error", "detail": f"API {resp.status}"}

                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"].strip()

                    # Strip markdown fences if present
                    if text.startswith("```"):
                        text = re.sub(r'^```(?:json)?\s*', '', text)
                        text = re.sub(r'\s*```$', '', text)

                    result = json.loads(text)
                    log.info(f"AI Health Check: {result.get('status', 'unknown')} — {result.get('summary', '')}")

                    # Update global health
                    HEALTH.ai_last_check = datetime.now(timezone.utc).isoformat()
                    HEALTH.ai_last_status = result.get("status", "unknown")

                    # Only alert Discord on warning/critical — stay silent when healthy
                    if result.get("status") in ("warning", "critical"):
                        color = 0xFFA500 if result["status"] == "warning" else 0xFF0000
                        emoji = "⚠️" if result["status"] == "warning" else "🚨"
                        issues_text = "\n".join(f"• {i}" for i in result.get("issues", []))
                        recs_text = "\n".join(f"• {r}" for r in result.get("recommendations", []))

                        for wh_url in self.webhooks.values():
                            await send_discord_alert(
                                wh_url,
                                f"{emoji} AI Health Check: {result['status'].upper()}",
                                result.get("summary", ""),
                                color=color,
                                fields=[
                                    {"name": "Issues", "value": issues_text[:1000] or "None", "inline": False},
                                    {"name": "Recommendations", "value": recs_text[:1000] or "None", "inline": False},
                                ],
                            )

                    return result

        except json.JSONDecodeError as e:
            log.error(f"AI health check: could not parse response: {e}")
            return {"status": "error", "detail": "parse_error"}
        except Exception as e:
            log.error(f"AI health check error: {e}")
            return {"status": "error", "detail": str(e)}


# ─── Site Handlers ─────────────────────────────────────────────────────────────

class SiteHandler:
    """Base class for site-specific login and content extraction."""

    def __init__(self, site_config: dict):
        self.config = site_config
        self.name = site_config["name"]
        self.login_config = site_config.get("login", {})

    async def login(self, page: Page) -> bool:
        raise NotImplementedError

    async def check_if_logged_in(self, page: Page) -> bool:
        raise NotImplementedError

    async def extract_content(self, page: Page, monitor: dict) -> str:
        selector = monitor.get("selector", "body")
        try:
            await page.wait_for_selector(selector, timeout=15000)
            element = await page.query_selector(selector)
            if element:
                return await element.inner_text()
        except Exception as e:
            log.warning(f"Selector '{selector}' failed, falling back to body: {e}")
        return await page.inner_text("body")

    async def extract_alerts(self, page: Page, monitor: dict) -> list[dict]:
        """
        Extract structured alert items (title + link) from the page.
        Returns list of {"title": ..., "url": ...} dicts.
        Override in subclass for site-specific selectors.
        """
        alerts = []
        link_selectors = [
            "article a[href]",
            ".entry-title a[href]",
            "h2 a[href]", "h3 a[href]", "h4 a[href]",
            ".post-title a[href]",
            ".article-title a[href]",
            ".content-list a[href]",
            "a.alert-link[href]",
        ]
        for sel in link_selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                try:
                    title = (await el.inner_text()).strip()
                    href = await el.get_attribute("href")
                    if title and href and len(title) > 3:
                        if not self._is_junk_link(title, href):
                            alerts.append({"title": title, "url": href})
                except:
                    continue
            if alerts:
                break

        # Deduplicate by URL
        seen = set()
        unique = []
        for a in alerts:
            if a["url"] not in seen:
                seen.add(a["url"])
                unique.append(a)
        return unique[:20]

    def _is_junk_link(self, title: str, url: str) -> bool:
        """Filter out nav, login, footer, and other non-content links."""
        t = title.lower().strip()
        u = url.lower()

        # Skip login/account related
        junk_titles = [
            "log in", "login", "log out", "logout", "sign in", "sign out",
            "forgot", "password", "reset", "subscribe", "my account",
            "contact", "privacy", "terms", "cookie", "disclaimer",
            "about us", "customer service", "support", "help",
            "home", "menu", "search", "close", "skip to content",
            "not yet a subscriber", "get started", "subscribe now",
        ]
        for jt in junk_titles:
            if jt in t:
                return True

        # Skip exact-match nav section titles (common page navigation)
        nav_exact = {
            "alerts", "updates", "reports", "portfolio", "trades",
            "trade alerts", "archives", "resources", "videos",
            "dashboard", "settings", "profile", "notifications",
            "members", "member area", "member's area",
            "read more", "view all", "see all", "show more",
            "next", "previous", "back", "forward",
        }
        if t in nav_exact:
            return True

        # Skip titles that are just the subscription/service name (nav headers)
        # Only block if the title is SHORT (< 40 chars) and matches a service name
        # Long titles like "Strategic Fortunes: Buy XYZ Stock Now" should pass through
        nav_contains = [
            "mastermind group", "paradigm press", "banyan hill",
            "investorplace", "investor place",
            "strategic fortunes", "extreme fortunes", "trademonster",
            "accelerated profits", "growth investor", "power portfolio",
            "platinum growth", "breakthrough stocks", "ai revolution",
        ]
        if len(t) < 40:
            for nc in nav_contains:
                if nc in t:
                    return True

        # Skip non-content URLs
        junk_url_parts = [
            "login", "logout", "sign-in", "sign-out", "wp-login",
            "password", "reset", "subscribe", "my-account", "ipa-login",
            "contact", "privacy", "terms", "cookie", "disclaimer",
            "#", "javascript:", "mailto:", "tel:",
            "facebook.com", "twitter.com", "linkedin.com", "youtube.com",
        ]
        for jp in junk_url_parts:
            if jp in u:
                return True

        # Skip subscription section nav links (relative paths like /subscription/pmg/alerts)
        if "/subscription/" in u and u.count("/") <= 4:
            # These are nav links within the subscription area, not actual articles
            return True

        # Skip very short titles (likely nav items)
        if len(t) < 5:
            return True

        # Skip single-word ALL-CAPS titles under 15 chars (nav buttons)
        if " " not in t.strip() and title.isupper() and len(t) < 15:
            return True

        return False


class ParadigmHandler(SiteHandler):
    """Handler for Paradigm Press Group."""

    async def login(self, page: Page) -> bool:
        try:
            login_url = self.login_config.get("url", "https://my.paradigmpressgroup.com/")
            email = self.login_config["email"]
            password = self.login_config["password"]

            # ─── Step 1: Force clean state for re-login ───
            # With stale cookies, the site redirects to dashboard instead of login form.
            # Try navigating to logout first, then back to login.
            log.info(f"[{self.name}] Forcing clean state for re-login...")

            # Try explicit logout paths
            logout_urls = [
                "https://my.paradigmpressgroup.com/logout",
                "https://my.paradigmpressgroup.com/sign-out",
                "https://my.paradigmpressgroup.com/api/auth/logout",
            ]
            for logout_url in logout_urls:
                try:
                    resp = await page.goto(logout_url, wait_until="domcontentloaded", timeout=8000)
                    if resp and resp.status < 400:
                        log.info(f"[{self.name}] Logout via {logout_url} (status {resp.status})")
                        await asyncio.sleep(2)
                        break
                except:
                    continue

            # ─── Step 2: Try multiple login URLs ───
            login_urls = [
                login_url,
                "https://my.paradigmpressgroup.com/login",
                "https://my.paradigmpressgroup.com/auth",
                "https://my.paradigmpressgroup.com/sign-in",
                "https://my.paradigmpressgroup.com/",
            ]
            # Deduplicate while preserving order
            seen = set()
            unique_urls = []
            for u in login_urls:
                if u not in seen:
                    seen.add(u)
                    unique_urls.append(u)

            email_input = None
            pass_input = None

            email_selectors = [
                'input[type="email"]', 'input[name="email"]', 'input[name="username"]',
                'input[name="login"]', 'input[placeholder*="email" i]',
                'input[placeholder*="username" i]', '#email', '#username',
                'input[name="user_login"]', 'input[id*="email" i]', 'input[id*="login" i]',
            ]
            pass_selectors = [
                'input[type="password"]', 'input[name="password"]', '#password',
                'input[name="user_pass"]', 'input[id*="password" i]',
            ]

            for try_url in unique_urls:
                log.info(f"[{self.name}] Trying login URL: {try_url}")
                try:
                    await page.goto(try_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                except Exception as e:
                    log.debug(f"[{self.name}] URL {try_url} navigation error: {e}")
                    continue

                # Check if we're already logged in (session recovered after page load)
                try:
                    page_text = (await page.inner_text("body")).lower()
                    if any(kw in page_text for kw in ["dashboard", "portfolio", "logout", "sign out", "my account"]):
                        if not any(kw in page_text for kw in ["sign in", "log in", "forgot password"]):
                            log.info(f"[{self.name}] Already logged in at {try_url}")
                            return True
                except:
                    pass

                # Look for login form
                for sel in email_selectors:
                    email_input = await page.query_selector(sel)
                    if email_input and await email_input.is_visible():
                        break
                    email_input = None

                for sel in pass_selectors:
                    pass_input = await page.query_selector(sel)
                    if pass_input and await pass_input.is_visible():
                        break
                    pass_input = None

                if email_input and pass_input:
                    log.info(f"[{self.name}] Found login form at {try_url}")
                    break

                # Try clicking a login trigger button/link
                login_triggers = [
                    'a:has-text("Log In")', 'a:has-text("Sign In")',
                    'button:has-text("Log In")', 'button:has-text("Sign In")',
                    '[data-action="login"]', '.login-link', '#login-btn',
                    'a[href*="login"]', 'a[href*="sign-in"]',
                ]
                for sel in login_triggers:
                    try:
                        trigger = await page.query_selector(sel)
                        if trigger and await trigger.is_visible():
                            log.info(f"[{self.name}] Clicking login trigger: {sel}")
                            await trigger.click()
                            await asyncio.sleep(3)
                            break
                    except:
                        continue

                # Re-check for form after clicking trigger
                for sel in email_selectors:
                    email_input = await page.query_selector(sel)
                    if email_input and await email_input.is_visible():
                        break
                    email_input = None
                for sel in pass_selectors:
                    pass_input = await page.query_selector(sel)
                    if pass_input and await pass_input.is_visible():
                        break
                    pass_input = None

                if email_input and pass_input:
                    log.info(f"[{self.name}] Found login form after trigger click at {try_url}")
                    break

                # Check for login form inside iframes
                try:
                    for frame in page.frames:
                        if frame == page.main_frame:
                            continue
                        for sel in email_selectors:
                            email_input = await frame.query_selector(sel)
                            if email_input:
                                for psel in pass_selectors:
                                    pass_input = await frame.query_selector(psel)
                                    if pass_input:
                                        log.info(f"[{self.name}] Found login form in iframe at {try_url}")
                                        break
                                if pass_input:
                                    break
                        if email_input and pass_input:
                            break
                except:
                    pass

                if email_input and pass_input:
                    break

                log.debug(f"[{self.name}] No login form at {try_url}, trying next...")
                email_input = None
                pass_input = None

            if not email_input or not pass_input:
                log.error(f"[{self.name}] Could not find login form on any URL")
                try:
                    await page.screenshot(path="debug_paradigm_login.png")
                    log.info(f"[{self.name}] Current URL: {page.url}")
                    page_text = (await page.inner_text("body"))[:500]
                    log.info(f"[{self.name}] Page text preview: {page_text[:200]}")
                except:
                    pass
                return False

            # ─── Step 3: Fill and submit ───
            await email_input.fill(email)
            await asyncio.sleep(0.5)
            await pass_input.fill(password)
            await asyncio.sleep(0.5)

            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Log In")', 'button:has-text("Sign In")',
                'button:has-text("Submit")', '.login-button', '#login-submit',
            ]
            clicked = False
            for sel in submit_selectors:
                submit = await page.query_selector(sel)
                if submit:
                    await submit.click(force=True)
                    clicked = True
                    break
            if not clicked:
                await pass_input.press("Enter")

            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except:
                pass

            page_text = (await page.inner_text("body")).lower()
            if any(kw in page_text for kw in ["dashboard", "welcome", "portfolio", "logout", "sign out", "my account"]):
                log.info(f"[{self.name}] Login successful")
                return True
            elif any(kw in page_text for kw in ["invalid", "incorrect", "error", "try again"]):
                log.error(f"[{self.name}] Login failed — bad credentials")
                return False
            else:
                log.info(f"[{self.name}] Login appears OK (URL: {page.url})")
                return True

        except Exception as e:
            log.error(f"[{self.name}] Login error: {e}")
            return False

    async def check_if_logged_in(self, page: Page) -> bool:
        try:
            url = page.url.lower()
            text = (await page.inner_text("body")).lower()

            # If page body is very short, it's still loading — assume not logged in
            if len(text.strip()) < 50:
                return False

            # URL-based detection
            if "login" in url or "sign-in" in url or "signin" in url:
                return False

            # Content-based detection
            login_signs = ["sign in", "log in", "forgot password", "forgot your password",
                           "enter your email", "enter your password", "create an account"]
            logged_in_signs = ["dashboard", "portfolio", "logout", "sign out", "my account",
                               "welcome", "subscription", "alerts", "reports"]

            has_login = any(kw in text for kw in login_signs)
            has_logged_in = any(kw in text for kw in logged_in_signs)

            if has_login and not has_logged_in:
                return False
            if has_logged_in:
                return True
            # Neither login nor logged-in signs found — ambiguous, assume not logged in
            return False
        except:
            return False


class GenericHandler(SiteHandler):
    """Generic handler for sites with standard email/password forms."""

    async def login(self, page: Page) -> bool:
        try:
            login_url = self.login_config.get("url", self.config["base_url"])
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            email = self.login_config.get("email", "")
            password = self.login_config.get("password", "")
            if not email or not password:
                log.warning(f"[{self.name}] No credentials, skipping login")
                return True

            email_input = await page.query_selector(
                'input[type="email"], input[name="email"], input[name="username"], '
                'input[placeholder*="email" i], input[placeholder*="username" i]'
            )
            pass_input = await page.query_selector(
                'input[type="password"], input[name="password"]'
            )

            if email_input and pass_input:
                await email_input.fill(email)
                await asyncio.sleep(0.3)
                await pass_input.fill(password)
                await asyncio.sleep(0.3)

                submit = await page.query_selector(
                    'button[type="submit"], input[type="submit"], '
                    'button:has-text("Log In"), button:has-text("Sign In")'
                )
                if submit:
                    await submit.click()
                else:
                    await pass_input.press("Enter")

                await asyncio.sleep(5)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except:
                    pass
                log.info(f"[{self.name}] Login attempted")
                return True
            else:
                log.warning(f"[{self.name}] Could not find login form")
                return False
        except Exception as e:
            log.error(f"[{self.name}] Login error: {e}")
            return False

    async def check_if_logged_in(self, page: Page) -> bool:
        try:
            text = (await page.inner_text("body")).lower()
            return "sign in" not in text and "log in" not in text
        except:
            return False


class BanyanHillHandler(SiteHandler):
    """Handler for Banyan Hill (banyanhill.com) — WordPress/WooCommerce login."""

    async def login(self, page: Page) -> bool:
        try:
            login_url = self.login_config.get("url", "https://banyanhill.com/customer-login/")
            log.info(f"[{self.name}] Navigating to login: {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(4)

            email = self.login_config["email"]
            password = self.login_config["password"]

            # Banyan Hill has a two-step login: email first, then password
            # Step 1: Enter email
            email_selectors = [
                'input[type="email"]', 'input[name="email"]',
                'input[placeholder*="email" i]', 'input[name="username"]',
                'input[name="log"]', '#username', '#user_login',
                'input[type="text"]',
            ]
            email_input = None
            for sel in email_selectors:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible():
                            email_input = el
                            log.info(f"[{self.name}] Found email input: {sel}")
                            break
                    except:
                        continue

            if not email_input:
                log.error(f"[{self.name}] Could not find email input")
                await page.screenshot(path="debug_banyanhill_login.png")
                return False

            # Use fill() directly — don't click first, as labels overlay the inputs
            await email_input.fill(email)
            await asyncio.sleep(1)

            # Click "Next" button if present (two-step flow)
            next_selectors = [
                'button:has-text("Next")', 'input[value="Next"]',
                'button:has-text("Continue")', 'a:has-text("Next")',
                'button[type="submit"]',
            ]
            for sel in next_selectors:
                btn = await page.query_selector(sel)
                if btn:
                    try:
                        if await btn.is_visible():
                            log.info(f"[{self.name}] Clicking Next: {sel}")
                            await btn.click(force=True)
                            await asyncio.sleep(3)
                            break
                    except:
                        continue

            # Step 2: Enter password
            pass_selectors = [
                'input[type="password"]', 'input[name="password"]',
                'input[name="pwd"]', '#user_pass', '#password',
            ]
            pass_input = None
            for sel in pass_selectors:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible():
                            pass_input = el
                            log.info(f"[{self.name}] Found password input: {sel}")
                            break
                    except:
                        continue

            if not pass_input:
                # Maybe it's a single-step form after all — try pressing Enter
                log.warning(f"[{self.name}] No password field visible, pressing Enter")
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
                for sel in pass_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        try:
                            if await el.is_visible():
                                pass_input = el
                                break
                        except:
                            continue

            if not pass_input:
                log.error(f"[{self.name}] Could not find password input")
                await page.screenshot(path="debug_banyanhill_password.png")
                return False

            # Fill password directly without clicking
            await pass_input.fill(password)
            await asyncio.sleep(0.5)

            # Submit
            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Log In")', 'button:has-text("Log in")',
                'button:has-text("Sign In")', 'button:has-text("Sign in")',
                'button:has-text("Submit")',
            ]
            clicked = False
            for sel in submit_selectors:
                btn = await page.query_selector(sel)
                if btn:
                    try:
                        if await btn.is_visible():
                            await btn.click(force=True)
                            clicked = True
                            break
                    except:
                        continue
            if not clicked:
                await page.keyboard.press("Enter")

            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except:
                pass

            page_text = (await page.inner_text("body")).lower()
            if any(kw in page_text for kw in ["dashboard", "my account", "logout", "log out", "welcome", "hi ", "trade alert", "portfolio"]):
                log.info(f"[{self.name}] Login successful")
                return True
            elif any(kw in page_text for kw in ["invalid", "incorrect", "error", "unknown email"]):
                log.error(f"[{self.name}] Login failed — bad credentials")
                await page.screenshot(path="debug_banyanhill_fail.png")
                return False
            else:
                log.info(f"[{self.name}] Login status unclear (URL: {page.url})")
                await page.screenshot(path="debug_banyanhill_post_login.png")
                return True

        except Exception as e:
            log.error(f"[{self.name}] Login error: {e}")
            return False

    async def check_if_logged_in(self, page: Page) -> bool:
        try:
            url = page.url.lower()
            text = (await page.inner_text("body")).lower()

            # If redirected to login page, definitely not logged in
            if "customer-login" in url or "login/?redirect" in url:
                return False

            # Login form indicators
            has_login_form = any(kw in text for kw in [
                "username or email", "lost your password", "remember me",
                "account log in", "customer login", "log in to access",
                "welcome! log in", "forgot your password",
            ])
            has_logged_in = any(kw in text for kw in [
                "logout", "log out", "my account", "welcome back",
                "trade alert", "portfolio", "position",
            ])

            if has_login_form and not has_logged_in:
                return False
            return True
        except:
            return False


class InvestorPlaceHandler(SiteHandler):
    """Handler for InvestorPlace — custom /ipa-login/ form, not wp-login."""

    async def login(self, page: Page) -> bool:
        try:
            login_url = "https://investorplace.com/ipa-login/"
            log.info(f"[{self.name}] Navigating to login: {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(4)

            email = self.login_config["email"]
            password = self.login_config["password"]

            # InvestorPlace has two login forms on the page:
            # 1. Main page form under "Log in to your Account"
            # 2. Modal "Subscriber Sign in" triggered by header
            # Both have unlabeled <input> fields. Try multiple strategies.

            email_input = None
            pass_input = None

            # Strategy 1: Find inputs by surrounding label text
            all_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input:not([type])')
            for inp in all_inputs:
                try:
                    placeholder = await inp.get_attribute("placeholder") or ""
                    aria = await inp.get_attribute("aria-label") or ""
                    name = await inp.get_attribute("name") or ""
                    combined = (placeholder + aria + name).lower()
                    if any(kw in combined for kw in ["email", "username", "user"]):
                        email_input = inp
                        break
                except:
                    continue

            all_pass = await page.query_selector_all('input[type="password"]')
            if all_pass:
                pass_input = all_pass[0]

            # Strategy 2: If no labeled inputs, use position — first text-like input + first password
            if not email_input:
                # Get all visible text inputs
                for inp in all_inputs:
                    try:
                        visible = await inp.is_visible()
                        if visible:
                            email_input = inp
                            break
                    except:
                        continue

            if not pass_input and all_pass:
                for inp in all_pass:
                    try:
                        visible = await inp.is_visible()
                        if visible:
                            pass_input = inp
                            break
                    except:
                        continue

            # Strategy 3: Try broader selectors
            if not email_input:
                broad_email = [
                    'form input[type="text"]', 'form input[type="email"]',
                    '.ipa-login input', '#ipa-login input',
                    'input[autocomplete="username"]', 'input[autocomplete="email"]',
                ]
                for sel in broad_email:
                    email_input = await page.query_selector(sel)
                    if email_input:
                        break

            if not pass_input:
                pass_input = await page.query_selector('form input[type="password"]')

            if not email_input or not pass_input:
                log.error(f"[{self.name}] Could not find login form inputs")
                await page.screenshot(path="debug_investorplace_login.png")
                # Log what we found for debugging
                input_count = len(all_inputs)
                pass_count = len(all_pass)
                log.error(f"[{self.name}] Found {input_count} text inputs, {pass_count} password inputs")
                return False

            log.info(f"[{self.name}] Found login form inputs, filling credentials...")
            await email_input.click()
            await email_input.fill(email)
            await asyncio.sleep(0.5)
            await pass_input.click()
            await pass_input.fill(password)
            await asyncio.sleep(0.5)

            # Find submit button near the password field
            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Sign in")', 'button:has-text("Log in")',
                'button:has-text("Log In")', 'button:has-text("Sign In")',
                'a:has-text("Sign in")', 'input[value="Sign in" i]',
                'input[value="Log in" i]', '.ipa-login button',
            ]
            clicked = False
            for sel in submit_selectors:
                submit = await page.query_selector(sel)
                if submit:
                    try:
                        visible = await submit.is_visible()
                        if visible:
                            log.info(f"[{self.name}] Clicking submit: {sel}")
                            await submit.click()
                            clicked = True
                            break
                    except:
                        continue
            if not clicked:
                log.info(f"[{self.name}] No submit button found, pressing Enter")
                await pass_input.press("Enter")

            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except:
                pass

            page_text = (await page.inner_text("body")).lower()
            current_url = page.url.lower()

            if any(kw in page_text for kw in ["my services", "dashboard", "logout", "log out", "welcome"]):
                log.info(f"[{self.name}] Login successful")
                return True
            elif any(kw in page_text for kw in ["invalid", "incorrect", "error", "not recognized"]):
                log.error(f"[{self.name}] Login failed — bad credentials")
                await page.screenshot(path="debug_investorplace_fail.png")
                return False
            elif "dashboard" in current_url or "my-account" in current_url:
                log.info(f"[{self.name}] Login successful (redirected to dashboard)")
                return True
            else:
                log.info(f"[{self.name}] Login status unclear (URL: {page.url})")
                await page.screenshot(path="debug_investorplace_post_login.png")
                return True

        except Exception as e:
            log.error(f"[{self.name}] Login error: {e}")
            return False

    async def check_if_logged_in(self, page: Page) -> bool:
        try:
            text = (await page.inner_text("body")).lower()
            url = page.url.lower()

            # Definite signs we're NOT logged in
            if any(kw in text for kw in ["you must be logged in", "please log in", "subscriber login",
                                          "enter your username", "username or email address"]):
                return False

            # If we're on wp-login page, we're not logged in
            if "wp-login" in url:
                return False

            # On subscription pages, check for restricted content indicators
            if "investorplace.com/" in url and any(sub in url for sub in [
                "acceleratedprofits", "airevolutionportfolio", "breakthroughstocks",
                "growthinvestor", "platinumgrowthclub", "powerportfolio"
            ]):
                # If page has very little content, it's probably a login wall
                if len(text) < 200:
                    return False
                # Look for actual content indicators
                if any(kw in text for kw in ["trade alert", "portfolio", "position", "buy", "sell",
                                              "recommendation", "stock", "article", "report"]):
                    return True
                # If we see subscription/paywall language
                if any(kw in text for kw in ["subscribe", "upgrade", "unlock", "premium access"]):
                    return False

            # Positive signs of being logged in
            if any(kw in text for kw in ["logout", "log out", "my account", "welcome"]):
                return True

            return True
        except:
            return False

    async def extract_alerts(self, page: Page, monitor: dict) -> list[dict]:
        """InvestorPlace-specific: extract article/report links from subscription pages."""
        alerts = []
        selectors = [
            "article a[href]",
            ".entry-title a[href]",
            "h2 a[href]", "h3 a[href]",
            ".post-listing a[href]",
            ".article-listing a[href]",
            "a.ipm-post-title[href]",
            ".ipm-article-list a[href]",
            "td a[href]",
            ".content a[href]",
        ]
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                try:
                    title = (await el.inner_text()).strip()
                    href = await el.get_attribute("href")
                    if title and href and len(title) > 5 and "investorplace.com" in href:
                        if not self._is_junk_link(title, href):
                            alerts.append({"title": title, "url": href})
                except:
                    continue
            if alerts:
                break

        seen = set()
        unique = []
        for a in alerts:
            if a["url"] not in seen:
                seen.add(a["url"])
                unique.append(a)
        return unique[:20]


class CNBCProHandler(SiteHandler):
    """Handler for CNBC Pro — paywall content with email/password login."""

    async def login(self, page: Page) -> bool:
        try:
            # CNBC login page
            login_url = "https://www.cnbc.com/application/generic"
            log.info(f"[{self.name}] Navigating to login: {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(4)

            email = self.login_config["email"]
            password = self.login_config["password"]

            # CNBC may show a "SIGN IN" button first that opens a login modal/form
            sign_in_selectors = [
                'a:has-text("Sign In")', 'a:has-text("SIGN IN")',
                'button:has-text("Sign In")', 'button:has-text("SIGN IN")',
                'a[href*="sign-in"]', 'a[href*="login"]',
                '.sign-in-link', '[data-action="sign-in"]',
            ]
            for sel in sign_in_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        log.info(f"[{self.name}] Clicking Sign In: {sel}")
                        await btn.click()
                        await asyncio.sleep(4)
                        break
                except:
                    continue

            # Look for email input — CNBC may use iframe for auth
            # First check if there's an iframe for the login form
            login_frame = None
            for frame in page.frames:
                frame_url = frame.url.lower()
                if any(kw in frame_url for kw in ["login", "auth", "sign-in", "identity", "registration"]):
                    login_frame = frame
                    log.info(f"[{self.name}] Found login iframe: {frame_url[:80]}")
                    break

            target = login_frame if login_frame else page

            # Find email input
            email_selectors = [
                'input[type="email"]', 'input[name="email"]',
                'input[placeholder*="email" i]', 'input[name="username"]',
                'input[autocomplete="email"]', 'input[autocomplete="username"]',
                'input[id*="email" i]', 'input[id*="user" i]',
                'input[type="text"]',
            ]
            email_input = None
            for sel in email_selectors:
                try:
                    el = await target.query_selector(sel)
                    if el and await el.is_visible():
                        email_input = el
                        log.info(f"[{self.name}] Found email input: {sel}")
                        break
                except:
                    continue

            if not email_input:
                # Try waiting for a form to appear
                log.info(f"[{self.name}] No email input yet, waiting for form...")
                await asyncio.sleep(3)
                for sel in email_selectors:
                    try:
                        el = await target.query_selector(sel)
                        if el and await el.is_visible():
                            email_input = el
                            break
                    except:
                        continue

            if not email_input:
                log.error(f"[{self.name}] Could not find email input")
                await page.screenshot(path="debug_cnbc_login.png")
                return False

            await email_input.fill(email)
            await asyncio.sleep(1)

            # CNBC may have a two-step flow (email first, then password)
            # Try clicking Continue/Next if present
            next_selectors = [
                'button:has-text("Continue")', 'button:has-text("Next")',
                'button:has-text("Submit")', 'input[value="Continue" i]',
                'button[type="submit"]',
            ]
            for sel in next_selectors:
                try:
                    btn = await target.query_selector(sel)
                    if btn and await btn.is_visible():
                        text = (await btn.inner_text()).strip().lower()
                        # Only click if it looks like a "next/continue" not "sign in"
                        if any(kw in text for kw in ["continue", "next", "submit"]):
                            log.info(f"[{self.name}] Clicking next step: {sel}")
                            await btn.click()
                            await asyncio.sleep(3)
                            break
                except:
                    continue

            # Find password input
            pass_selectors = [
                'input[type="password"]', 'input[name="password"]',
                'input[autocomplete="current-password"]',
                'input[id*="password" i]',
            ]
            pass_input = None
            for sel in pass_selectors:
                try:
                    el = await target.query_selector(sel)
                    if el and await el.is_visible():
                        pass_input = el
                        log.info(f"[{self.name}] Found password input: {sel}")
                        break
                except:
                    continue

            if not pass_input:
                # Password might appear after clicking next
                await asyncio.sleep(3)
                for sel in pass_selectors:
                    try:
                        el = await target.query_selector(sel)
                        if el and await el.is_visible():
                            pass_input = el
                            break
                    except:
                        continue

            if not pass_input:
                log.error(f"[{self.name}] Could not find password input")
                await page.screenshot(path="debug_cnbc_password.png")
                return False

            await pass_input.fill(password)
            await asyncio.sleep(0.5)

            # Submit login
            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Sign In")', 'button:has-text("Log In")',
                'button:has-text("Sign in")', 'button:has-text("Log in")',
                'button:has-text("Submit")', 'button:has-text("Continue")',
            ]
            clicked = False
            for sel in submit_selectors:
                try:
                    btn = await target.query_selector(sel)
                    if btn and await btn.is_visible():
                        log.info(f"[{self.name}] Clicking submit: {sel}")
                        await btn.click()
                        clicked = True
                        break
                except:
                    continue
            if not clicked:
                await pass_input.press("Enter")

            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except:
                pass

            # Verify login
            page_text = (await page.inner_text("body")).lower()
            current_url = page.url.lower()

            if any(kw in page_text for kw in ["sign out", "log out", "my account",
                                                "profile", "pro subscriber", "welcome"]):
                log.info(f"[{self.name}] Login successful")
                return True
            elif any(kw in page_text for kw in ["invalid", "incorrect", "error",
                                                  "not recognized", "try again"]):
                log.error(f"[{self.name}] Login failed — bad credentials")
                await page.screenshot(path="debug_cnbc_fail.png")
                return False
            elif "account" in current_url or "pro" in current_url:
                log.info(f"[{self.name}] Login likely successful (URL: {page.url})")
                return True
            else:
                # CNBC often just redirects back — check for paywall unlock
                log.info(f"[{self.name}] Login status unclear (URL: {page.url}), assuming success")
                await page.screenshot(path="debug_cnbc_post_login.png")
                return True

        except Exception as e:
            log.error(f"[{self.name}] Login error: {e}")
            return False

    async def check_if_logged_in(self, page: Page) -> bool:
        try:
            text = (await page.inner_text("body")).lower()
            url = page.url.lower()

            # Positive indicators of being logged in
            if any(kw in text for kw in ["sign out", "log out", "my account",
                                          "pro subscriber"]):
                return True

            # Paywall / login wall indicators
            if any(kw in text for kw in ["subscribe to read", "subscribe now",
                                          "unlock this article", "pro subscribers only",
                                          "become a pro member", "start your free trial",
                                          "sign in to read"]):
                return False

            # If "sign in" appears prominently but no logged-in indicators
            if "sign in" in text and not any(kw in text for kw in ["sign out", "log out"]):
                # Could be the nav bar sign-in link — check more carefully
                # If we can see article content, we're probably fine
                if len(text) > 1000 and any(kw in text for kw in [
                    "stock", "buy", "sell", "market", "investment", "portfolio",
                    "analyst", "price target", "earnings"
                ]):
                    return True
                return False

            # Default: assume logged in if page has substantial content
            return len(text) > 500

        except:
            return False

    async def extract_alerts(self, page: Page, monitor: dict) -> list[dict]:
        """Extract article links from CNBC Pro author profile pages."""
        alerts = []

        # CNBC article selectors — profile pages list articles with links
        selectors = [
            "div.Card-titleContainer a[href]",
            "a.Card-title[href]",
            ".Card-title a[href]",
            "article a[href]",
            ".RiverHeadline a[href]",
            "h2 a[href]", "h3 a[href]",
            ".ArticleList a[href]",
            ".FeaturedCard a[href]",
            "a[href*='/pro/']",
            "a[href*='/2026/']", "a[href*='/2025/']",
        ]

        for sel in selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                try:
                    title = (await el.inner_text()).strip()
                    href = await el.get_attribute("href")
                    if not title or not href or len(title) < 10:
                        continue
                    # Only keep CNBC article URLs
                    if "cnbc.com" in href and not self._is_junk_link(title, href):
                        # Normalize relative URLs
                        if href.startswith("/"):
                            href = f"https://www.cnbc.com{href}"
                        alerts.append({"title": title, "url": href})
                except:
                    continue
            if alerts:
                break

        seen = set()
        unique = []
        for a in alerts:
            if a["url"] not in seen:
                seen.add(a["url"])
                unique.append(a)
        return unique[:20]


HANDLER_MAP = {
    "paradigm press": ParadigmHandler,
    "paradigmpressgroup.com": ParadigmHandler,
    "banyan hill": BanyanHillHandler,
    "banyanhill.com": BanyanHillHandler,
    "investorplace": InvestorPlaceHandler,
    "investorplace.com": InvestorPlaceHandler,
    "cnbc pro": CNBCProHandler,
    "cnbc.com": CNBCProHandler,
}


def get_handler(site_config: dict) -> SiteHandler:
    name_lower = site_config["name"].lower()
    base_url = site_config.get("base_url", "").lower()
    for key, cls in HANDLER_MAP.items():
        if key in name_lower or key in base_url:
            return cls(site_config)
    return GenericHandler(site_config)


# ─── Change Detection ──────────────────────────────────────────────────────────

class ChangeDetector:
    def __init__(self, state_file: str = "monitor_state.json"):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def _hash(self, content: str) -> str:
        # Normalize: strip whitespace, remove common dynamic elements
        normalized = " ".join(content.split())
        # Remove timestamps like "12:34 PM", "2026-02-25", "Feb 25, 2026"
        normalized = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM|am|pm)?\b', '', normalized)
        normalized = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', normalized)
        normalized = re.sub(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s*\d{4}\b', '', normalized)
        normalized = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '', normalized)
        # Remove "X minutes ago", "X hours ago" style strings
        normalized = re.sub(r'\b\d+\s+(seconds?|minutes?|hours?|days?)\s+ago\b', '', normalized)
        # Remove common session/cache-busting tokens (long hex/alphanumeric strings)
        normalized = re.sub(r'\b[a-f0-9]{20,}\b', '', normalized)
        # Remove stock price-like numbers that change constantly (e.g., "$1,234.56")
        # Only remove if they look like live prices, not trade alerts
        normalized = re.sub(r'(?<!\w)\$[\d,]+\.\d{2,4}(?!\w)', '', normalized)
        # Remove nonce/token values common in WordPress sites
        normalized = re.sub(r'nonce["\s:=]+["\']?[a-f0-9]+["\']?', '', normalized)
        # Remove "Log in" / "Log out" state text that changes with session
        normalized = re.sub(r'\b(log\s*in|log\s*out|sign\s*in|sign\s*out|logged\s*in\s*as)\b', '', normalized, flags=re.IGNORECASE)
        # Remove "Welcome, Username" type greetings
        normalized = re.sub(r'welcome,?\s+\S+', '', normalized, flags=re.IGNORECASE)

        # Remove common page boilerplate / chrome that appears intermittently
        boilerplate_patterns = [
            r'skip to (?:main )?content',
            r'enable accessibility (?:for low vision)?',
            r'open the accessibility menu',
            r'accept all cookies?',
            r'reject all',
            r'cookies? settings',
            r'cookie (?:policy|preferences?)',
            r'by clicking .{0,200}?(?:cookies?|marketing efforts)',
            r'privacy (?:policy|notice)',
            r'terms (?:of (?:use|service))',
            r'(?:we|this site) (?:uses?|utilizes?) cookies?',
            r'storing of cookies on your device',
            r'third[- ]party partners?',
            r'site navigation,? analyze site usage',
            r'close (?:menu|modal|banner|popup|dialog)',
            r'primary menu',
            r'search symbol.{0,50}?keywords?',
            r'copyright ©?\s*\d{4}',
            r'all rights reserved',
            r'(?:dow|nasdaq|s&p)\s*/?\s*',
            r'subscribe now!?\s*not yet a subscriber\??',
            r'not yet a premium subscriber\??',
            r'expand/collapse doe',
            r'close doe',
            r'be_ixf.*?(?=\s{2}|\n)',
            r'php_sdk.*?(?=\s{2}|\n)',
            r'ct_\d+',
            r'ym_\d+\s*d_\d+',
        ]
        for bp in boilerplate_patterns:
            normalized = re.sub(bp, '', normalized, flags=re.IGNORECASE)

        # Re-normalize whitespace after removals
        normalized = " ".join(normalized.split())
        return hashlib.sha256(normalized.encode()).hexdigest()

    def check(self, key: str, content: str) -> tuple[bool, str, str]:
        """Returns (changed, old_hash, new_hash). First-time returns changed=False."""
        new_hash = self._hash(content)
        entry = self.state.get(key, {})
        old_hash = entry.get("hash", "")
        now = datetime.now(timezone.utc).isoformat()

        if not old_hash:
            self.state[key] = {"hash": new_hash, "first_seen": now, "last_seen": now}
            self._save()
            return False, "", new_hash

        if old_hash != new_hash:
            self.state[key] = {
                "hash": new_hash,
                "first_seen": entry.get("first_seen", now),
                "last_seen": now,
                "previous_hash": old_hash,
            }
            self._save()
            return True, old_hash, new_hash

        self.state[key]["last_seen"] = now
        self._save()
        return False, old_hash, new_hash

    def get_content(self, key: str) -> Optional[str]:
        return self.state.get(key, {}).get("last_content")

    def store_content(self, key: str, content: str):
        if key not in self.state:
            self.state[key] = {}
        self.state[key]["last_content"] = content[:5000]
        self._save()

    def get_alerts(self, key: str) -> list[dict]:
        return self.state.get(key, {}).get("last_alerts", [])

    def store_alerts(self, key: str, alerts: list[dict]):
        if key not in self.state:
            self.state[key] = {}
        self.state[key]["last_alerts"] = alerts[:30]
        self._save()


def _strip_boilerplate(text: str) -> str:
    """Remove common page chrome from display text."""
    lines = text.splitlines()
    junk_patterns = [
        r'skip to (?:main )?content',
        r'enable accessibility',
        r'open the accessibility menu',
        r'accept all cookies?',
        r'reject all',
        r'cookies? settings',
        r'cookie (?:policy|preferences?)',
        r'by clicking .{0,200}?(?:cookies?|marketing efforts)',
        r'privacy (?:policy|notice)',
        r'terms (?:of (?:use|service))',
        r'(?:we|this site) (?:uses?|utilizes?) cookies?',
        r'storing of cookies',
        r'third[- ]party partners?',
        r'site navigation,? analyze',
        r'close (?:menu|modal|banner|popup|dialog)',
        r'primary menu',
        r'search symbol',
        r'copyright ©?\s*\d{4}',
        r'all rights reserved',
        r'subscribe now',
        r'not yet a (?:premium )?subscriber',
        r'expand/collapse doe',
        r'close doe',
        r'forgot (?:your )?password',
        r'^\s*(?:log\s*in|log\s*out|sign\s*in|sign\s*out)\s*$',
        r'^\s*(?:home|menu|search|close)\s*$',
        r'^\s*(?:dow|nasdaq|s&p)\s*/?',
        r'^\s*[/|•·]\s*$',
        r'be_ixf',
        r'php_sdk',
        r'customer login',
        r'account log in',
        r'log in to access',
        r'welcome!?\s*log in',
        r'did you forget your password',
        r'footer information',
        r'newsletter sign up',
        r'get the best of banyan',
        r'share (?:this|on)',
        r'enter your mastodon',
        r'select page',
    ]
    compiled = [re.compile(p, re.IGNORECASE) for p in junk_patterns]
    clean = []
    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) < 3:
            continue
        if any(p.search(stripped) for p in compiled):
            continue
        clean.append(stripped)
    return "\n".join(clean)


def page_has_today_date(content: str) -> bool:
    """Check if the page content contains today's date in any common format.
    
    This filters out false positives where page structure changed but no
    new dated content was actually added. Real trade alerts always have today's date.
    """
    if HAS_PYTZ:
        now = datetime.now(pytz.timezone("US/Eastern"))
    elif HAS_ZONEINFO:
        now = datetime.now(ZoneInfo("US/Eastern"))
    else:
        now = datetime.now(timezone(timedelta(hours=-5)))

    text = content.lower()

    # Generate today's date in many formats
    month_full = now.strftime("%B").lower()      # "february"
    month_short = now.strftime("%b").lower()      # "feb"
    day = now.day                                  # 25
    year = now.year                                # 2026
    year_short = now.strftime("%y")                # "26"
    month_num = now.month                          # 2

    # Check various date formats:
    # "February 25, 2026", "February 25 2026", "Feb 25, 2026"
    # "2/25/2026", "02/25/2026", "2/25/26", "02/25/26"
    # "2-25-2026", "02-25-2026"
    # "2026-02-25" (ISO)
    # "25 February 2026", "25 Feb 2026"
    patterns = [
        f"{month_full} {day}",              # "february 25"
        f"{month_full}  {day}",             # "february  25" (double space)
        f"{month_short} {day}",             # "feb 25"
        f"{month_short}. {day}",            # "feb. 25"
        f"{month_num}/{day}/{year}",         # "2/25/2026"
        f"{month_num}/{day}/{year_short}",   # "2/25/26"
        f"{month_num:02d}/{day:02d}/{year}", # "02/25/2026"
        f"{month_num:02d}/{day:02d}/{year_short}",  # "02/25/26"
        f"{month_num}-{day}-{year}",         # "2-25-2026"
        f"{month_num:02d}-{day:02d}-{year}", # "02-25-2026"
        f"{year}-{month_num:02d}-{day:02d}", # "2026-02-25"
        f"{day} {month_full}",              # "25 february"
        f"{day} {month_short}",             # "25 feb"
    ]

    for pat in patterns:
        if pat in text:
            return True

    return False


def _extract_fingerprint_sentences(text: str) -> set:
    """Extract normalized key sentences from text for content-level dedup.

    Returns a set of fingerprints (first 80 chars of meaningful lines, lowercased).
    Two pieces of content with 50%+ overlap in fingerprints are considered duplicates.
    """
    fingerprints = set()
    for line in text.split("\n"):
        line_s = line.strip()
        # Skip short lines, dates, nav items, boilerplate
        if len(line_s) < 30:
            continue
        # Normalize: lowercase, collapse whitespace
        norm = " ".join(line_s.lower().split())
        # Take first 80 chars as fingerprint (catches article snippets even if endings shift)
        fp = norm[:80]
        fingerprints.add(fp)
    return fingerprints


def content_diff(old: str, new: str, max_lines: int = 30) -> str:
    if not old or not new:
        result = _strip_boilerplate(new or "")
        return result[:1500] or "Content changed"
    old_clean = _strip_boilerplate(old)
    new_clean = _strip_boilerplate(new)
    old_set = set(line.strip() for line in old_clean.splitlines() if line.strip())
    additions = [l.strip() for l in new_clean.splitlines() if l.strip() and l.strip() not in old_set]
    if not additions:
        return "Content structure changed (no specific new lines detected)."
    result = "\n".join(additions[:max_lines])
    if len(additions) > max_lines:
        result += f"\n... +{len(additions) - max_lines} more lines"
    return result.strip() or "Content changed"


# ─── Main Monitor ──────────────────────────────────────────────────────────────

class NewsletterMonitor:
    def __init__(self, config_path: str = "config.json"):
        self.config = load_config(config_path)
        self.check_interval = self.config.get("check_interval_seconds", 60)
        self.headless = self.config.get("browser_headless", True)
        self.user_data_dir = self.config.get("user_data_dir", "./browser_data")
        self.webhooks = self.config.get("discord_webhooks", {})
        self.sites = self.config.get("sites", [])
        self.hours = self.config.get("trading_hours", {})
        self.detector = ChangeDetector()
        self.context: Optional[BrowserContext] = None
        self.site_pages: dict[str, Page] = {}
        self.handlers: dict[str, SiteHandler] = {}
        self.login_locks: dict[str, asyncio.Lock] = {}  # Per-site login locks
        self.last_login_attempt: dict[str, float] = {}  # Per-site timestamp of last login attempt
        self.alerted_diff_hashes: dict[str, set] = {}  # Per-monitor set of diff hashes already alerted
        self.alerted_titles: dict[str, set] = {}  # Per-monitor set of alert titles already sent
        self.alerted_content_fingerprints: dict[str, set] = {}  # Per-monitor set of content sentence fingerprints

        # API discovery & fast polling
        self.discovered_apis: dict[str, dict] = {}  # url -> {site, monitor, has_trade, last_body_hash, cookies}
        self.api_discovery_mode = True  # Log all APIs for first N cycles
        self.api_discovery_cycles = 0
        self.api_discovery_max = 20  # Discover for 20 cycles (more data) then start fast polling
        self.api_poll_interval = 2  # Poll APIs every 2 seconds (was 3)
        self.api_poll_running = False
        self.api_alerted_hashes: set = set()  # Dedup for API alerts
        self.api_alerted_titles: set = set()  # Dedup for API alert titles
        self._api_last_bodies: dict[str, str] = {}  # Last body per API endpoint (for content diff)

        # ─── Timing comparison logger ───
        # Tracks API vs page detection times to prove speed advantage
        # Key: normalized content fingerprint, Value: {api_at, page_at, monitor, detail}
        self.timing_log: list[dict] = []  # Chronological list of all detections
        self.api_detect_times: dict[str, dict] = {}  # content_hash -> {timestamp, monitor, source_url, preview}
        self.page_detect_times: dict[str, dict] = {}  # content_hash -> {timestamp, monitor, preview}
        self.timing_report_sent = False

        # AI health monitor
        ai_cfg = self.config.get("ai_health_check", {})
        self.ai_monitor = AIHealthMonitor(ai_cfg, self.webhooks)

    def _log_detection(self, source: str, monitor_name: str, content_preview: str,
                       source_url: str = "", content_hash: str = ""):
        """Log a detection event for timing comparison.
        source: 'api_fast', 'api_passive', or 'page'
        """
        tz_name = self.hours.get("timezone", "US/Eastern")
        now = now_in_tz(tz_name)
        ts = now.isoformat()
        ts_display = now.strftime("%I:%M:%S.%f %p")[:-3]  # Include milliseconds

        entry = {
            "source": source,
            "monitor": monitor_name,
            "timestamp": ts,
            "display_time": ts_display,
            "epoch": time.time(),
            "content_hash": content_hash,
            "preview": content_preview[:200],
            "url": source_url[:200],
        }
        self.timing_log.append(entry)

        # Keep timing log bounded
        if len(self.timing_log) > 500:
            self.timing_log = self.timing_log[-250:]

        # Cross-reference: check if opposite source already detected similar content
        if source in ("api_fast", "api_passive") and content_hash:
            self.api_detect_times[content_hash] = entry
            # Check if page already detected this
            if content_hash in self.page_detect_times:
                page_entry = self.page_detect_times[content_hash]
                delta = entry["epoch"] - page_entry["epoch"]
                log.info(f"⏱️ TIMING: API detected AFTER page by {delta:.1f}s for {monitor_name}")
        elif source == "page" and content_hash:
            self.page_detect_times[content_hash] = entry
            # Check if API already detected this
            if content_hash in self.api_detect_times:
                api_entry = self.api_detect_times[content_hash]
                delta = entry["epoch"] - api_entry["epoch"]
                log.info(f"⏱️ TIMING: Page detected AFTER API by {delta:.1f}s for {monitor_name} — API was {delta:.1f}s FASTER!")

        log.info(f"📊 TIMING LOG [{ts_display}] {source.upper()} | {monitor_name} | {content_preview[:80]}")
        return entry

    async def _send_timing_report(self):
        """Send a timing comparison report to Discord at end of day."""
        if not self.timing_log:
            return

        tz_name = self.hours.get("timezone", "US/Eastern")
        now = now_in_tz(tz_name)

        # Group by monitor
        by_monitor: dict[str, list] = {}
        for entry in self.timing_log:
            mon = entry["monitor"]
            if mon not in by_monitor:
                by_monitor[mon] = []
            by_monitor[mon].append(entry)

        lines = [f"**Date:** {now.strftime('%Y-%m-%d')}",
                 f"**Total detections:** {len(self.timing_log)}",
                 ""]

        # Find timing pairs (API vs page for same content)
        api_wins = 0
        page_wins = 0
        deltas = []

        for content_hash, api_entry in self.api_detect_times.items():
            if content_hash in self.page_detect_times:
                page_entry = self.page_detect_times[content_hash]
                delta = page_entry["epoch"] - api_entry["epoch"]
                deltas.append(delta)
                if delta > 0:
                    api_wins += 1
                else:
                    page_wins += 1

        if deltas:
            avg_delta = sum(deltas) / len(deltas)
            lines.append(f"**⚡ Timing Pairs Found:** {len(deltas)}")
            lines.append(f"**API faster:** {api_wins} times")
            lines.append(f"**Page faster:** {page_wins} times")
            lines.append(f"**Avg delta:** {avg_delta:+.1f}s (positive = API was faster)")
            lines.append("")

        # Summary by source
        api_fast = [e for e in self.timing_log if e["source"] == "api_fast"]
        api_passive = [e for e in self.timing_log if e["source"] == "api_passive"]
        page = [e for e in self.timing_log if e["source"] == "page"]
        lines.append(f"**⚡ Fast API detections:** {len(api_fast)}")
        lines.append(f"**🔍 Passive API detections:** {len(api_passive)}")
        lines.append(f"**📄 Page detections:** {len(page)}")
        lines.append("")

        # Show all detections chronologically
        lines.append("**📊 Detection Timeline:**")
        for entry in self.timing_log[-30:]:  # Last 30
            icon = {"api_fast": "⚡", "api_passive": "🔍", "page": "📄"}.get(entry["source"], "?")
            lines.append(f"{icon} `{entry['display_time']}` **{entry['monitor']}** — {entry['preview'][:60]}")

        # Show discovered API endpoints
        if self.discovered_apis:
            trade_apis = {u: i for u, i in self.discovered_apis.items() if i["has_trade"]}
            lines.append(f"\n**📡 Discovered Endpoints:** {len(self.discovered_apis)} total, {len(trade_apis)} trade-relevant")
            for url, info in list(trade_apis.items())[:10]:
                lines.append(f"  `{url[:120]}` (seen {info['times_seen']}x)")

        first_wh = next(iter(self.webhooks.values()), "")
        if first_wh:
            await send_discord_alert(
                first_wh,
                "📊 API Timing Report",
                "\n".join(lines)[:4000],
                color=0x3498DB,
            )
        self.timing_report_sent = True

    # US market holidays (NYSE/NASDAQ closed days)
    MARKET_HOLIDAYS_2025 = {
        (1, 1),   # New Year's Day
        (1, 20),  # MLK Jr Day
        (2, 17),  # Presidents Day
        (4, 18),  # Good Friday
        (5, 26),  # Memorial Day
        (6, 19),  # Juneteenth
        (7, 4),   # Independence Day
        (9, 1),   # Labor Day
        (11, 27), # Thanksgiving
        (12, 25), # Christmas
    }
    MARKET_HOLIDAYS_2026 = {
        (1, 1),   # New Year's Day
        (1, 19),  # MLK Jr Day
        (2, 16),  # Presidents Day
        (4, 3),   # Good Friday
        (5, 25),  # Memorial Day
        (6, 19),  # Juneteenth
        (7, 3),   # Independence Day (observed)
        (9, 7),   # Labor Day
        (11, 26), # Thanksgiving
        (12, 25), # Christmas
    }
    MARKET_HOLIDAYS_2027 = {
        (1, 1),   # New Year's Day
        (1, 18),  # MLK Jr Day
        (2, 15),  # Presidents Day
        (3, 26),  # Good Friday
        (5, 31),  # Memorial Day
        (6, 18),  # Juneteenth (observed)
        (7, 5),   # Independence Day (observed)
        (9, 6),   # Labor Day
        (11, 25), # Thanksgiving
        (12, 24), # Christmas (observed)
    }
    MARKET_HOLIDAYS = {
        2025: MARKET_HOLIDAYS_2025,
        2026: MARKET_HOLIDAYS_2026,
        2027: MARKET_HOLIDAYS_2027,
    }

    def _is_market_holiday(self, dt) -> bool:
        """Check if a given date is a US market holiday."""
        year_holidays = self.MARKET_HOLIDAYS.get(dt.year, set())
        return (dt.month, dt.day) in year_holidays

    def is_active_hours(self) -> bool:
        """Check if current time is within configured trading hours (weekdays, non-holidays)."""
        tz_name = self.hours.get("timezone", "US/Eastern")
        now = now_in_tz(tz_name)
        start = now.replace(
            hour=self.hours.get("start_hour", 9),
            minute=self.hours.get("start_minute", 0),
            second=0, microsecond=0,
        )
        end = now.replace(
            hour=self.hours.get("end_hour", 16),
            minute=self.hours.get("end_minute", 0),
            second=0, microsecond=0,
        )
        # Only active on weekdays (Mon=0 ... Fri=4)
        is_weekday = now.weekday() < 5
        is_in_window = start <= now <= end
        is_holiday = self._is_market_holiday(now)
        return is_weekday and is_in_window and not is_holiday

    def time_until_active(self) -> float:
        """Seconds until next active window. Returns 0 if already active."""
        if self.is_active_hours():
            return 0

        tz_name = self.hours.get("timezone", "US/Eastern")
        now = now_in_tz(tz_name)

        # Find next weekday at start_hour
        target = now.replace(
            hour=self.hours.get("start_hour", 9),
            minute=self.hours.get("start_minute", 0),
            second=0, microsecond=0,
        )

        if now >= target or now.weekday() >= 5 or self._is_market_holiday(now):
            # Move to next day
            target += timedelta(days=1)

        # Skip weekends and holidays
        while target.weekday() >= 5 or self._is_market_holiday(target):
            target += timedelta(days=1)

        delta = (target - now).total_seconds()
        return max(delta, 0)

    async def start(self, login_only: bool = False):
        """Start the monitor."""
        log.info("=" * 60)
        log.info("Newsletter Monitor Starting")
        log.info(f"Sites: {len(self.sites)} | Interval: {self.check_interval}s")
        log.info(f"Trading hours: {self.hours.get('start_hour',9)}:{self.hours.get('start_minute',0):02d}"
                 f" – {self.hours.get('end_hour',16)}:{self.hours.get('end_minute',0):02d} "
                 f"{self.hours.get('timezone','US/Eastern')}")
        log.info(f"AI health check: {'ON' if self.ai_monitor.enabled else 'OFF'}")
        log.info("=" * 60)

        # Health server already started in main() before init
        log.info("Health server already running (started pre-init)")

        pw = await async_playwright().start()

        os.makedirs(self.user_data_dir, exist_ok=True)
        self.context = await pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_https_errors=True,
        )

        # Anti-detection
        for p in self.context.pages:
            await p.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")

        # Login to all enabled sites
        for site in self.sites:
            if not site.get("enabled", True):
                continue
            handler = get_handler(site)
            self.handlers[site["name"]] = handler
            self.login_locks[site["name"]] = asyncio.Lock()

            page = await self.context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")
            self.site_pages[site["name"]] = page

            try:
                await page.goto(site["base_url"], wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3)

                if await handler.check_if_logged_in(page):
                    log.info(f"[{site['name']}] Already logged in (saved session)")
                else:
                    log.info(f"[{site['name']}] Logging in...")
                    success = await handler.login(page)
                    if success:
                        log.info(f"[{site['name']}] ✓ Login OK")
                    else:
                        log.error(f"[{site['name']}] ✗ Login FAILED")

                HEALTH.sites_status[site["name"]] = "logged_in"
            except Exception as e:
                log.error(f"[{site['name']}] Login error: {e}")
                HEALTH.sites_status[site["name"]] = f"error: {e}"

        if login_only:
            log.info("Login-only mode — sessions saved. Exiting.")
            await self.context.close()
            return

        # ─── One-time API probe on startup ───
        # Discover all API endpoints used by Paradigm (runs once, results logged)
        await self._probe_site_apis()

        # Start fast poller immediately if probe found content APIs
        pollable = {url: info for url, info in self.discovered_apis.items()
                     if info.get("has_trade") or info.get("is_content_api")}
        if pollable and not self.api_poll_running:
            self.api_poll_running = True
            asyncio.create_task(self._fast_api_poll_loop())
            log.info(f"Fast API poller started from probe: {len(pollable)} endpoints")

        # Startup notification
        first_wh = next(iter(self.webhooks.values()), "")
        content_api_count = sum(1 for i in self.discovered_apis.values() if i.get("is_content_api"))
        await send_discord_alert(
            first_wh,
            "📡 Newsletter Monitor Started",
            (f"**Sites:** {len(self.sites)}\n"
             f"**Interval:** {self.check_interval}s\n"
             f"**Content APIs found:** {content_api_count} (fast-polling every {self.api_poll_interval}s)\n"
             f"**Hours:** {self.hours.get('start_hour',9)}:{self.hours.get('start_minute',0):02d}"
             f" – {self.hours.get('end_hour',16)}:{self.hours.get('end_minute',0):02d} EST\n"
             f"**AI Health:** {'Enabled' if self.ai_monitor.enabled else 'Disabled'}"),
            color=0x3498DB,
        )

        # Run main loop + AI health loop concurrently
        await asyncio.gather(
            self._monitor_loop(),
            self._ai_health_loop(),
        )

    async def _probe_site_apis(self):
        """One-time startup probe to discover ALL API endpoints used by sites.

        Visits a few pages and logs every HTTP response, then probes common
        API patterns via JavaScript fetch(). Results are logged so we can
        identify the fastest content delivery APIs.
        """
        log.info("="*60)
        log.info("🔬 API PROBE: Discovering content delivery endpoints...")
        log.info("="*60)

        for site in self.sites:
            if not site.get("enabled", True):
                continue
            site_name = site["name"]
            monitors = site.get("monitors", [])
            if not monitors:
                continue

            log.info(f"\n🌐 Probing {site_name}...")

            # Pick 2-3 representative monitors to visit
            probe_monitors = monitors[:3]

            all_api_urls = {}  # url_pattern -> {url, size, ct, preview}

            for monitor in probe_monitors:
                monitor_name = monitor.get("name", "")
                monitor_url = monitor.get("url", "")
                if not monitor_url:
                    continue

                captured = []

                async def capture_all(response, _captured=captured):
                    try:
                        url = response.url
                        url_lower = url.lower()
                        ct = response.headers.get("content-type", "")

                        # Skip obvious non-API
                        skip = [".css", ".js", ".png", ".jpg", ".gif", ".svg", ".woff",
                                ".ttf", ".ico", "fonts.", "google", "facebook", "analytics",
                                "tracking", "doubleclick", "cloudflare", "recaptcha"]
                        if any(s in url_lower for s in skip):
                            return

                        if response.status != 200:
                            return
                        if not any(t in ct for t in ["json", "text/html", "text/plain"]):
                            return

                        try:
                            body = await response.text()
                        except:
                            return

                        if len(body) < 30:
                            return

                        _captured.append({
                            "url": url,
                            "ct": ct,
                            "size": len(body),
                            "preview": body[:300],
                        })
                    except:
                        pass

                try:
                    probe_page = await self.context.new_page()
                    probe_page.on("response", capture_all)
                    await probe_page.goto(monitor_url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(4)  # Wait for async JS
                    await probe_page.close()
                except Exception as e:
                    log.debug(f"  Probe page error for {monitor_name}: {e}")
                    try:
                        await probe_page.close()
                    except:
                        pass

                for c in captured:
                    parsed = urlparse(c["url"])
                    pattern = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if pattern not in all_api_urls:
                        all_api_urls[pattern] = c

            # Log all discovered responses
            if all_api_urls:
                log.info(f"  📡 {len(all_api_urls)} unique response URLs captured:")
                # Sort: prioritize /api/ and json responses
                sorted_apis = sorted(all_api_urls.items(),
                    key=lambda x: (
                        -10 if "/api/" in x[0].lower() else 0,
                        -5 if "json" in x[1]["ct"] else 0,
                        -x[1]["size"],
                    ))
                for pattern, info in sorted_apis[:30]:
                    preview = info["preview"][:120].replace("\n", " ")
                    log.info(f"    [{info['size']:>6}b] [{info['ct'][:20]:>20}] {pattern[:100]}")
                    log.info(f"             Preview: {preview}")

            # ─── JavaScript fetch() probe for common API patterns ───
            base_url = site.get("base_url", "")
            if not base_url:
                continue

            log.info(f"\n  🔬 Probing API patterns via fetch()...")

            # Build probe paths based on site
            probe_paths = [
                "/api/content", "/api/articles", "/api/alerts", "/api/trades",
                "/api/publications", "/api/messages", "/api/notifications",
                "/api/feed", "/api/recent", "/api/dashboard", "/api/user",
                "/api/user/subscriptions", "/api/user/alerts", "/api/user/messages",
                "/api/user/notifications", "/api/auth/session", "/api/me",
                "/api/v1/content", "/api/v1/alerts", "/api/v1/trades",
                "/api/cms/get/entity?content_type=article",
                "/api/cms/get/entity?content_type=alert",
                "/api/cms/get/entity?content_type=trade",
                "/api/cms/get/entity?content_type=trade_alert",
                "/api/cms/get/entity?content_type=message",
                "/api/cms/get/entity?content_type=notification",
                "/api/cms/get/entity?content_type=report",
                "/api/cms/get/entity?content_type=flash",
                "/api/cms/get/entity?content_type=flash_alert",
                "/api/cms/get/entity?content_type=publication",
                "/api/cms/get/entity?content_type=content",
                "/api/cms/get/entity?content_type=post",
                "/graphql", "/api/graphql",
            ]

            try:
                main_page = self.site_pages.get(site_name)
                if main_page:
                    api_hits = []
                    for path in probe_paths:
                        try:
                            result = await main_page.evaluate(f"""
                                async () => {{
                                    try {{
                                        const r = await fetch('{path}', {{credentials: 'include'}});
                                        const text = await r.text();
                                        return {{status: r.status, size: text.length,
                                                ct: r.headers.get('content-type'),
                                                body: text.substring(0, 500)}};
                                    }} catch(e) {{
                                        return {{error: e.message}};
                                    }}
                                }}
                            """)
                            if result.get("error"):
                                continue
                            status = result["status"]
                            size = result["size"]
                            body = result.get("body", "")
                            ct = result.get("ct", "") or ""

                            if status == 200 and size > 30:
                                preview = body[:150].replace("\n", " ")
                                log.info(f"    ✅ {status} [{size:>6}b] {path}")
                                log.info(f"       Type: {ct} | Preview: {preview}")
                                api_hits.append({
                                    "url": f"{base_url.rstrip('/')}{path}",
                                    "path": path,
                                    "size": size,
                                    "ct": ct,
                                    "preview": body[:500],
                                })
                            elif status not in (404, 405):
                                log.debug(f"    ⬜ {status} [{size:>6}b] {path}")
                        except:
                            pass

                    # Store hits for the fast poller to use
                    if api_hits:
                        log.info(f"\n  🎯 {len(api_hits)} API endpoints found for {site_name}!")
                        for hit in api_hits:
                            norm_key = hit["url"].split("?")[0] if "?" not in hit.get("path", "") else hit["url"]
                            parsed = urlparse(hit["url"])
                            norm_key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                            if "?" in hit["url"]:
                                norm_key = hit["url"]  # Keep query params for CMS endpoints

                            if norm_key not in self.discovered_apis:
                                self.discovered_apis[norm_key] = {
                                    "full_url": hit["url"],
                                    "site": site_name,
                                    "monitor": "API Probe",
                                    "monitors_seen": {"API Probe"},
                                    "has_trade": False,  # Will be set True if content matches
                                    "is_content_api": True,  # Flag for content APIs
                                    "size": hit["size"],
                                    "content_type": hit["ct"],
                                    "preview": hit["preview"][:150],
                                    "first_seen_cycle": 0,
                                    "times_seen": 1,
                                    "notified": False,
                                }
                                log.info(f"    📡 Added to fast poller: {norm_key[:100]}")
                    else:
                        log.info(f"  ⚠️ No API endpoints found via fetch() for {site_name}")
            except Exception as e:
                log.warning(f"  API probe error for {site_name}: {e}")

        log.info("="*60)
        log.info("🔬 API PROBE COMPLETE")
        log.info("="*60)

    async def _monitor_loop(self):
        """Main monitoring loop with market-hours gating and concurrent page checks."""
        cycle = 0
        while True:
            # Check if we're in active hours
            if not self.is_active_hours():
                if not HEALTH.is_sleeping:
                    HEALTH.is_sleeping = True
                    sleep_secs = self.time_until_active()
                    tz_name = self.hours.get("timezone", "US/Eastern")
                    now = now_in_tz(tz_name)
                    reason = "market holiday" if self._is_market_holiday(now) else "outside trading hours"
                    log.info(f"{reason.capitalize()} ({now.strftime('%I:%M %p %Z')}). "
                             f"Sleeping {sleep_secs/3600:.1f}h until next active window.")

                    first_wh = next(iter(self.webhooks.values()), "")
                    await send_discord_alert(
                        first_wh,
                        "💤 Monitor Sleeping",
                        f"{reason.capitalize()} ({now.strftime('%A, %b %d %I:%M %p %Z')}).\n"
                        f"Will resume in ~{sleep_secs/3600:.1f} hours.",
                        color=0x95A5A6,
                    )

                await asyncio.sleep(60)
                continue

            if HEALTH.is_sleeping:
                HEALTH.is_sleeping = False
                tz_name = self.hours.get("timezone", "US/Eastern")
                now = now_in_tz(tz_name)
                log.info(f"Trading hours active ({now.strftime('%I:%M %p %Z')}). Resuming monitoring.")

                first_wh = next(iter(self.webhooks.values()), "")
                await send_discord_alert(
                    first_wh,
                    "☀️ Monitor Active",
                    f"Trading hours started ({now.strftime('%I:%M %p %Z')}). Monitoring resumed.",
                    color=0x2ECC71,
                )

                # Re-login to all sites in case sessions expired overnight
                for site in self.sites:
                    if not site.get("enabled", True):
                        continue
                    page = self.site_pages.get(site["name"])
                    handler = self.handlers.get(site["name"])
                    if page and handler:
                        try:
                            await page.goto(site["base_url"], wait_until="domcontentloaded", timeout=45000)
                            await asyncio.sleep(2)
                            if not await handler.check_if_logged_in(page):
                                log.info(f"[{site['name']}] Session expired overnight, re-logging in...")
                                await handler.login(page)
                        except Exception as e:
                            log.error(f"[{site['name']}] Wake-up re-login error: {e}")

                # Restart fast API poller if we have discovered APIs
                pollable_apis = {url: info for url, info in self.discovered_apis.items()
                                 if info.get("has_trade") or info.get("is_content_api")}
                if pollable_apis and not self.api_poll_running:
                    self.api_poll_running = True
                    asyncio.create_task(self._fast_api_poll_loop())
                    log.info(f"Fast API poller restarted: {len(pollable_apis)} endpoints")

            cycle += 1
            HEALTH.last_cycle = datetime.now(timezone.utc).isoformat()
            HEALTH.total_cycles = cycle
            HEALTH.last_cycle_ok = True
            log.info(f"--- Cycle #{cycle} ---")

            # Run all sites concurrently, but stagger monitors within each site
            # to avoid hammering one domain with simultaneous requests
            site_tasks = []
            for site in self.sites:
                if not site.get("enabled", True):
                    continue
                site_tasks.append(self._check_site(site))

            await asyncio.gather(*site_tasks)

            # ─── Proactive session keep-alive ───
            # Paradigm sessions expire ~40min. Navigate main page every ~20min to refresh.
            SESSION_REFRESH_CYCLES = 80  # 80 × 15s = 20 minutes
            if cycle > 0 and cycle % SESSION_REFRESH_CYCLES == 0:
                for site in self.sites:
                    if not site.get("enabled", True):
                        continue
                    main_page = self.site_pages.get(site["name"])
                    handler = self.handlers.get(site["name"])
                    if main_page and handler:
                        try:
                            await main_page.goto(site["base_url"], wait_until="domcontentloaded", timeout=15000)
                            await asyncio.sleep(1)
                            if await handler.check_if_logged_in(main_page):
                                log.debug(f"[{site['name']}] Session keep-alive OK")
                            else:
                                log.info(f"[{site['name']}] Session expired during keep-alive, re-logging in...")
                                success = await handler.login(main_page)
                                if success:
                                    log.info(f"[{site['name']}] Keep-alive re-login successful")
                                else:
                                    log.warning(f"[{site['name']}] Keep-alive re-login failed")
                        except Exception as e:
                            log.debug(f"[{site['name']}] Session keep-alive error: {e}")

            # ─── API Discovery tracking ───
            if self.api_discovery_mode:
                self.api_discovery_cycles += 1
                if self.api_discovery_cycles >= self.api_discovery_max and not self.api_poll_running:
                    # Don't disable discovery — keep finding new endpoints all day
                    # But now start fast polling with what we have
                    trade_apis = {url: info for url, info in self.discovered_apis.items() if info.get("has_trade")}
                    content_apis = {url: info for url, info in self.discovered_apis.items() if info.get("is_content_api")}
                    all_apis = self.discovered_apis

                    log.info(f"API Discovery phase complete: {len(all_apis)} total, {len(trade_apis)} trade, {len(content_apis)} content")
                    log.info("Discovery will continue running to find new endpoints")

                    # Send discovery report to Discord
                    first_wh = next(iter(self.webhooks.values()), "")
                    if first_wh:
                        report_lines = [
                            f"**Total API endpoints discovered:** {len(all_apis)}",
                            f"**Trade keyword APIs:** {len(trade_apis)}",
                            f"**Content APIs (from probe):** {len(content_apis)}",
                            "",
                        ]
                        pollable = {**trade_apis, **content_apis}
                        if pollable:
                            report_lines.append("**🎯 APIs being fast-polled:**")
                            for url, info in list(pollable.items())[:15]:
                                api_type = "Trade" if info.get("has_trade") else "Content"
                                monitors = info.get("monitors_seen", {info.get("monitor", "?")})
                                report_lines.append(
                                    f"⚡ `{url[:120]}`\n"
                                    f"   Type: {api_type} | Site: {info['site']} | Size: {info['size']}b"
                                )
                        if len(all_apis) > len(pollable):
                            report_lines.append(f"\n**Other APIs ({len(all_apis) - len(pollable)}):**")
                            for url, info in list(all_apis.items())[:20]:
                                if not info.get("has_trade") and not info.get("is_content_api"):
                                    report_lines.append(f"📡 `{url[:120]}` ({info['size']}b)")

                        await send_discord_alert(
                            first_wh,
                            "🔍 API Discovery Report",
                            "\n".join(report_lines)[:4000],
                            color=0x9B59B6,
                        )

                    # Start fast API poller if we found pollable endpoints
                    pollable = {url: info for url, info in self.discovered_apis.items()
                                if info.get("has_trade") or info.get("is_content_api")}
                    if pollable and not self.api_poll_running:
                        self.api_poll_running = True
                        asyncio.create_task(self._fast_api_poll_loop())
                        log.info(f"Fast API poller started: {len(pollable)} endpoints every {self.api_poll_interval}s")

            interval = self.check_interval
            log.info(f"Next check in {interval}s")

            # ─── Hourly timing status report ───
            if cycle > 0 and cycle % 240 == 0:  # Every ~240 cycles at 15s = ~1 hour
                if self.timing_log:
                    first_wh = next(iter(self.webhooks.values()), "")
                    if first_wh:
                        tz_name = self.hours.get("timezone", "US/Eastern")
                        now = now_in_tz(tz_name)
                        api_count = len([e for e in self.timing_log if "api" in e["source"]])
                        page_count = len([e for e in self.timing_log if e["source"] == "page"])
                        disc_total = len(self.discovered_apis)
                        disc_trade = len({u for u, i in self.discovered_apis.items() if i["has_trade"]})
                        await send_discord_alert(
                            first_wh,
                            f"📊 Hourly API Timing Status ({now.strftime('%I:%M %p')})",
                            f"**Detections so far:** {len(self.timing_log)}\n"
                            f"⚡ API detections: {api_count} | 📄 Page detections: {page_count}\n"
                            f"📡 Discovered endpoints: {disc_total} total, {disc_trade} trade-relevant\n"
                            f"🔄 Fast poller: {'ACTIVE' if self.api_poll_running else 'INACTIVE'}\n"
                            f"🔍 Discovery mode: {'ON (cycle ' + str(self.api_discovery_cycles) + '/' + str(self.api_discovery_max) + ')' if self.api_discovery_mode else 'COMPLETE'}",
                            color=0x3498DB,
                        )

            # ─── End of day timing report ───
            tz_name = self.hours.get("timezone", "US/Eastern")
            now_check = now_in_tz(tz_name)
            end_hour = self.hours.get("end_hour", 16)
            end_minute = self.hours.get("end_minute", 0)
            # Send report in the last cycle before market close
            minutes_to_close = (end_hour * 60 + end_minute) - (now_check.hour * 60 + now_check.minute)
            if 0 <= minutes_to_close <= 1 and not self.timing_report_sent:
                await self._send_timing_report()

            await asyncio.sleep(interval)

    async def _check_site(self, site: dict):
        """
        Check all monitors for a single site.
        Uses a dedicated page per monitor for true concurrency.
        Staggers requests by 1-2s to be polite to the server.
        """
        site_name = site["name"]
        handler = self.handlers.get(site_name)
        if not handler:
            return

        monitors = site.get("monitors", [])
        if not monitors:
            return

        # Create temporary pages for concurrent checks (reuse main page for first monitor)
        tasks = []
        for i, monitor in enumerate(monitors):
            # Small stagger between requests to same domain (0.5s apart)
            delay = i * 0.5
            tasks.append(self._check_monitor_concurrent(site, handler, monitor, delay))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                err_msg = f"[{monitors[i].get('name', '?')}] {result}"
                log.error(err_msg)
                HEALTH.errors.append(err_msg)
                HEALTH.last_cycle_ok = False

    async def _extract_article_content(self, url: str, site_name: str) -> str:
        """Follow a link and extract the full article text content.

        Opens the URL in a new page (using existing authenticated session),
        extracts the main article body text, and returns it.
        Uses site-specific selectors to avoid grabbing sidebar/nav content.
        """
        if not url or not url.startswith("http"):
            return ""

        try:
            page = await self.context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")

            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)

            # Site-specific selectors (ordered most-specific first)
            url_lower = url.lower()

            if "cnbc.com" in url_lower:
                # CNBC articles: target the article body, NOT the sidebar
                content_selectors = [
                    '[data-module="ArticleBody"]',
                    ".ArticleBody-articleBody",
                    ".group .ArticleBody",
                    ".article-body",
                    ".ArticleBody",
                    '[class*="ArticleBody"]',
                    '[class*="articleBody"]',
                    "article .group",
                    "article [class*='content']",
                ]
            elif "paradigmpressgroup.com" in url_lower:
                content_selectors = [
                    ".article-content",
                    ".post-content",
                    ".entry-content",
                    "[class*='article']",
                    "main",
                ]
            elif "banyanhill.com" in url_lower:
                content_selectors = [
                    "article .entry-content",
                    ".entry-content",
                    ".post-content",
                    ".single-post-content",
                    "article",
                ]
            elif "investorplace.com" in url_lower:
                content_selectors = [
                    "article .entry-content",
                    ".entry-content",
                    ".post-content",
                    "#content article",
                    "article",
                ]
            else:
                content_selectors = [
                    "article .entry-content",
                    "article .post-content",
                    ".article-body",
                    ".entry-content",
                    ".post-content",
                    ".content-area article",
                    ".single-post-content",
                    "#content article",
                    "article",
                    ".report-content",
                    ".alert-content",
                    "main",
                    "#main-content",
                    ".page-content",
                ]

            content = ""
            for sel in content_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        text = await el.inner_text()
                        if text and len(text.strip()) > 100:
                            content = text.strip()
                            break
                except:
                    continue

            # Smart fallback: use JavaScript to find the largest block of <p> text
            # This avoids grabbing sidebar/nav text that pollutes ticker extraction
            if not content or len(content) < 200:
                content = await page.evaluate("""() => {
                    // Find all containers that hold multiple <p> tags
                    const candidates = document.querySelectorAll(
                        'article, [role="article"], main, .content, [class*="article"], [class*="content"], [class*="body"]'
                    );
                    let best = '';
                    let bestLen = 0;
                    for (const el of candidates) {
                        const paragraphs = el.querySelectorAll('p');
                        let pText = '';
                        for (const p of paragraphs) {
                            const t = p.innerText.trim();
                            if (t.length > 20) pText += t + '\\n';
                        }
                        if (pText.length > bestLen) {
                            bestLen = pText.length;
                            best = pText;
                        }
                    }
                    // If we found paragraphs, use those; otherwise fallback to largest text block
                    if (best.length > 200) return best;

                    // Fallback: just get all <p> from the page
                    const allP = document.querySelectorAll('p');
                    let allText = '';
                    for (const p of allP) {
                        const t = p.innerText.trim();
                        if (t.length > 30) allText += t + '\\n';
                    }
                    return allText || document.body.innerText;
                }""")

            # For CNBC specifically: strip common sidebar noise patterns
            if "cnbc.com" in url_lower and content:
                content = self._clean_cnbc_content(content)

            # Also try clicking "Read More" / "Continue Reading" if the content seems truncated
            if len(content) < 500:
                read_more_selectors = [
                    'a:has-text("Read More")', 'a:has-text("READ MORE")',
                    'a:has-text("Continue Reading")', 'a:has-text("Read Full")',
                    'button:has-text("Read More")', 'button:has-text("Show More")',
                    '.read-more a', '.readmore a',
                ]
                for sel in read_more_selectors:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(3)
                            # Re-extract after expanding
                            for csel in content_selectors:
                                try:
                                    el = await page.query_selector(csel)
                                    if el:
                                        text = await el.inner_text()
                                        if text and len(text.strip()) > len(content):
                                            content = text.strip()
                                            break
                                except:
                                    continue
                            break
                    except:
                        continue

            await page.close()

            # Clean up: limit length for API call
            if len(content) > 8000:
                content = content[:8000] + "..."

            log.info(f"[{site_name}] Extracted {len(content)} chars from {url[:60]}")
            return content

        except Exception as e:
            log.warning(f"[{site_name}] Article extraction failed for {url[:60]}: {e}")
            try:
                await page.close()
            except:
                pass
            return ""

    @staticmethod
    def _clean_cnbc_content(text: str) -> str:
        """Strip common CNBC sidebar/nav noise from extracted text."""
        lines = text.split("\n")
        clean_lines = []
        # Patterns that indicate sidebar/nav content to skip
        skip_patterns = [
            "sign in", "create free account", "subscribe",
            "related video", "watch now", "live tv",
            "trending now", "most popular", "latest news",
            "pro subscriber", "investing club",
            "share this article", "share article",
            "privacy policy", "terms of service",
            "cookie policy", "copyright",
            "get this delivered", "newsletter",
            "data is a real-time snapshot",
            "market data provided by",
            "powered by", "ad feedback",
        ]
        for line in lines:
            line_s = line.strip()
            if not line_s or len(line_s) < 10:
                continue
            line_lower = line_s.lower()
            # Skip nav/sidebar lines
            if any(p in line_lower for p in skip_patterns):
                continue
            # Skip lines that are just ticker symbols and prices (sidebar widgets)
            # e.g. "AAPL +1.2%" or "$NVDA 850.23"
            if re.match(r'^[\$A-Z]{1,5}\s+[\+\-\d\.\%]+$', line_s):
                continue
            # Skip very short lines that look like nav items
            if len(line_s) < 20 and not any(c.islower() for c in line_s):
                continue
            clean_lines.append(line_s)
        return "\n".join(clean_lines)

    async def _analyze_with_deepseek(self, title: str, article_content: str, site_name: str, monitor_name: str) -> dict:
        """Use DeepSeek API to extract stock recommendations from article content.

        Returns dict with:
            headline: str  — Bloomberg-style headline
            tickers: list[str] — Stock tickers mentioned
            action: str — BUY/SELL/HOLD/CLOSE
            summary: str — 1-2 sentence summary
        """
        api_key = self.ai_monitor.api_key if self.ai_monitor.enabled else os.environ.get("DEEPSEEK_API_KEY", "")
        api_url = self.ai_monitor.api_url if self.ai_monitor.enabled else "https://api.deepseek.com/v1/chat/completions"
        model = self.ai_monitor.model if self.ai_monitor.enabled else "deepseek-chat"

        if not api_key:
            log.warning("DeepSeek API key not available for article analysis")
            return {"headline": title, "tickers": [], "action": "", "summary": ""}

        # Truncate content for API
        content_for_api = article_content[:6000] if article_content else ""

        prompt = f"""You are a financial news wire analyst. Analyze this newsletter article and extract the key trading information.

SOURCE: {site_name} — {monitor_name}
TITLE: {title}

ARTICLE CONTENT:
{content_for_api}

Respond in EXACTLY this JSON format, nothing else:
{{
    "headline": "Write a Bloomberg/Reuters-style headline (max 120 chars). Format: TICKER(S): Action — Key reason. Example: 'NVDA: BUY — Newsletter recommends ahead of GTC catalyst' or 'AEIS, CIEN, COHR: BUY — Added to High-Growth Buy List for data center exposure'",
    "tickers": ["TICKER1", "TICKER2"],
    "action": "BUY or SELL or HOLD or CLOSE or ADD or TRIM or INFO",
    "summary": "1-2 sentence summary of the recommendation and reasoning"
}}

Rules:
- CRITICAL: Only include tickers that the AUTHOR is specifically recommending, analyzing, or discussing as investment ideas in the article body
- Do NOT include tickers from sidebar widgets, trending lists, "related articles", or page navigation
- Do NOT include tickers that are only briefly mentioned in passing or as comparisons (e.g. "like NVDA" when article is about something else)
- Tickers must be valid uppercase US stock/ETF symbols (1-5 letters). Never include random words as tickers
- For trade alerts with entry/exit prices, include those in the summary
- headline must read like a Bloomberg terminal alert — lead with the actual tickers being recommended
- If the article is marketing/promotional with no actionable trade info, use action "INFO"
- If no specific stock recommendation, use action "INFO" and list only tickers that are the main subject of the article"""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 500,
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error(f"DeepSeek API error {resp.status}: {body[:200]}")
                        return {"headline": title, "tickers": [], "action": "", "summary": ""}

                    data = await resp.json()
                    content_text = data["choices"][0]["message"]["content"].strip()

                    # Parse JSON from response (handle markdown code blocks)
                    content_text = content_text.replace("```json", "").replace("```", "").strip()
                    result = json.loads(content_text)

                    log.info(f"[{monitor_name}] DeepSeek analysis: {result.get('headline', '')[:80]}")
                    return {
                        "headline": result.get("headline", title)[:200],
                        "tickers": result.get("tickers", [])[:10],
                        "action": result.get("action", "INFO"),
                        "summary": result.get("summary", "")[:500],
                    }

        except json.JSONDecodeError as e:
            log.warning(f"DeepSeek returned non-JSON: {e}")
            return {"headline": title, "tickers": [], "action": "", "summary": ""}
        except Exception as e:
            log.warning(f"DeepSeek analysis error: {e}")
            return {"headline": title, "tickers": [], "action": "", "summary": ""}

    async def _check_monitor_concurrent(self, site: dict, handler: SiteHandler, monitor: dict, delay: float):
        """Check a single monitor using a fresh page (enables concurrency)."""
        if delay > 0:
            await asyncio.sleep(delay)

        monitor_name = monitor.get("name", monitor["url"])
        monitor_url = monitor["url"]
        monitor_key = f"{site['name']}:{monitor_name}"

        # Create a temporary page for this check
        page = await self.context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")

        # ─── Network interception: capture background API responses ───
        api_responses = []
        discovered_urls = []  # For API discovery

        async def capture_response(response):
            """Capture XHR/fetch responses that might contain trade alert data."""
            try:
                url = response.url
                url_lower = url.lower()
                content_type = response.headers.get("content-type", "")

                # Only capture JSON/text API responses, skip images/css/js/fonts
                if not any(ct in content_type for ct in ["json", "text/html", "text/plain"]):
                    return
                # Skip static assets and marketing/analytics services
                SKIP_DOMAINS = [
                    ".css", ".js", ".png", ".jpg", ".gif", ".svg", ".woff", ".ttf",
                    "fonts.", "analytics", "tracking", "google", "facebook", "cdn.",
                    "cloudflare", "recaptcha", "gtm.", "doubleclick",
                    # Marketing/analytics/personalization platforms (NOT trade data)
                    "lytics.io", "segment.io", "segment.com", "mixpanel.com",
                    "hubspot", "marketo", "pardot", "mailchimp", "klaviyo",
                    "optimizely", "hotjar", "heap.", "amplitude.",
                    "intercom", "drift.", "zendesk", "crisp.",
                    "tealium", "braze.", "iterable.", "sailthru",
                    # Ad/tracking
                    "adsrvr.", "adroll.", "criteo.", "taboola.", "outbrain.",
                    "bidswitch", "pubmatic", "rubiconproject",
                ]
                if any(skip in url_lower for skip in SKIP_DOMAINS):
                    return

                # Skip CMS promo/marketing content endpoints
                if "content_type=promo" in url_lower:
                    return

                status = response.status
                if status == 200:
                    try:
                        body = await response.text()
                        if len(body) < 50 or len(body) > 100000:
                            return

                        body_lower = body.lower()
                        # Trade-specific keywords (avoid generic marketing like "buy now", "recommendation")
                        trade_keywords = [
                            "trade alert", "new alert", "flash alert",
                            "entry price", "stop loss", "profit target",
                            "limit order", "market order",
                            "action to take", "trade notification",
                            "buy to open", "sell to close", "buy to close", "sell to open",
                            "strike price", "expiration date", "options contract",
                            "share price", "price target", "cost basis",
                        ]
                        has_trade = any(kw in body_lower for kw in trade_keywords)

                        if has_trade:
                            api_responses.append({
                                "url": url,
                                "body": body[:5000],
                                "content_type": content_type,
                            })

                        # API discovery: log ALL json/api URLs during discovery mode
                        if self.api_discovery_mode:
                            is_api = ("json" in content_type
                                      or "/api" in url_lower
                                      or "wp-json" in url_lower
                                      or "ajax" in url_lower
                                      or "graphql" in url_lower
                                      or "fetch" in url_lower)
                            if is_api or has_trade:
                                discovered_urls.append({
                                    "url": url,
                                    "has_trade": has_trade,
                                    "size": len(body),
                                    "content_type": content_type,
                                    "body_preview": body[:300],
                                })
                    except:
                        pass
            except:
                pass

        page.on("response", capture_response)

        try:
            await page.goto(monitor_url, wait_until="domcontentloaded", timeout=20000)
            # Wait for dynamic content to render (trade alerts are usually JS-rendered)
            await asyncio.sleep(3)

            # Check session
            if not await handler.check_if_logged_in(page):
                log.warning(f"[{monitor_name}] Session expired, attempting re-login...")

                # Use a lock so only one monitor per site re-logins at a time
                lock = self.login_locks.get(site["name"])
                async with lock:
                    # Check if we already tried re-login recently (within 60s)
                    last_attempt = self.last_login_attempt.get(site["name"], 0)
                    if time.time() - last_attempt < 60:
                        log.info(f"[{monitor_name}] Re-login attempted recently, skipping")
                        return

                    # Use the main page for re-login
                    main_page = self.site_pages.get(site["name"])
                    if main_page:
                        log.info(f"[{monitor_name}] Performing re-login for {site['name']}...")
                        self.last_login_attempt[site["name"]] = time.time()

                        # Try up to 2 times
                        success = False
                        for attempt in range(2):
                            success = await handler.login(main_page)
                            if success:
                                break
                            if attempt == 0:
                                log.warning(f"[{monitor_name}] Re-login attempt 1 failed, retrying in 5s...")
                                await asyncio.sleep(5)

                        if not success:
                            log.error(f"[{monitor_name}] Re-login FAILED")
                            for wh_key in monitor.get("alert_webhooks", ["primary"]):
                                wh_url = self.webhooks.get(wh_key, "")
                                await send_discord_alert(
                                    wh_url,
                                    f"⚠️ Session Lost: {site['name']}",
                                    f"Could not re-login to {site['name']}. Check credentials.",
                                    color=0xFF0000,
                                )
                            HEALTH.sites_status[site["name"]] = "session_lost"
                            return

                        # Verify login worked by checking a protected page
                        await main_page.goto(monitor_url, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.sleep(2)
                        if not await handler.check_if_logged_in(main_page):
                            log.error(f"[{monitor_name}] Re-login appeared successful but protected page still requires auth")
                            HEALTH.sites_status[site["name"]] = "login_failed"
                            return

                        log.info(f"[{monitor_name}] Re-login verified successfully")

                # Retry the page after re-login
                # Close current temp page and open fresh one so it picks up new cookies
                await page.close()
                page = await self.context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => false});")
                await asyncio.sleep(1)  # Let cookies propagate in context
                await page.goto(monitor_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)

                # Final check — if still not logged in, skip this cycle (will retry next)
                if not await handler.check_if_logged_in(page):
                    log.warning(f"[{monitor_name}] Still not logged in after re-login, will retry next cycle")
                    await page.close()
                    return

            # Extract content
            content = await handler.extract_content(page, monitor)
            if not content or len(content.strip()) < 10:
                log.warning(f"[{monitor_name}] Empty content")
                return

            # Guard: if page looks like a login page, trigger re-login
            content_lower = content.lower()
            login_indicators = ["customer login", "account log in", "log in to access",
                                "enter your username", "forgot your password", "welcome! log in",
                                "enter your email", "sign in to your account"]
            if any(ind in content_lower for ind in login_indicators):
                log.warning(f"[{monitor_name}] Page shows login form — triggering re-login for {site['name']}")

                lock = self.login_locks.get(site["name"])
                async with lock:
                    last_attempt = self.last_login_attempt.get(site["name"], 0)
                    if time.time() - last_attempt < 120:
                        log.info(f"[{monitor_name}] Re-login attempted recently, skipping this cycle")
                        return

                    main_page = self.site_pages.get(site["name"])
                    if main_page:
                        log.info(f"[{monitor_name}] Performing re-login for {site['name']}...")
                        self.last_login_attempt[site["name"]] = time.time()
                        success = await handler.login(main_page)
                        if success:
                            log.info(f"[{monitor_name}] Re-login successful, will verify next cycle")
                        else:
                            log.error(f"[{monitor_name}] Re-login FAILED")
                            for wh_key in monitor.get("alert_webhooks", ["primary"]):
                                wh_url = self.webhooks.get(wh_key, "")
                                await send_discord_alert(
                                    wh_url,
                                    f"⚠️ Session Lost: {site['name']}",
                                    f"Could not re-login to {site['name']}. Check credentials.",
                                    color=0xFF0000,
                                )
                return

            # Extract structured alerts (titles + links)
            alerts = []
            try:
                alerts = await handler.extract_alerts(page, monitor)
            except Exception as e:
                log.debug(f"[{monitor_name}] Alert extraction failed: {e}")

            # Detect changes
            old_content = self.detector.get_content(monitor_key)
            changed, _, _ = self.detector.check(monitor_key, content)

            if changed:
                log.info(f"[{monitor_name}] *** CHANGE DETECTED ***")
                HEALTH.total_changes += 1

                diff_text = content_diff(old_content or "", content) if old_content else content[:1500]

                # Skip alerts where diff has no meaningful content
                if diff_text in ("Content structure changed (no specific new lines detected).",
                                 "Content changed"):
                    log.info(f"[{monitor_name}] Change detected but no meaningful diff — suppressing alert")
                    self.detector.store_content(monitor_key, content)
                    if alerts:
                        self.detector.store_alerts(monitor_key, alerts)
                    HEALTH.sites_status[site["name"]] = "ok"
                    return

                # Date gate: check if today's date appears in the DIFF (not whole page)
                # Real new trade alerts will have today's date in the changed content
                if not page_has_today_date(diff_text):
                    log.info(f"[{monitor_name}] Change detected but no today's date in diff — suppressing alert")
                    self.detector.store_content(monitor_key, content)
                    if alerts:
                        self.detector.store_alerts(monitor_key, alerts)
                    HEALTH.sites_status[site["name"]] = "ok"
                    return

                # Content-based dedup: hash the diff and skip if we already alerted on this exact diff
                diff_hash = hashlib.sha256(diff_text.strip().encode()).hexdigest()[:16]
                if monitor_key not in self.alerted_diff_hashes:
                    self.alerted_diff_hashes[monitor_key] = set()
                if diff_hash in self.alerted_diff_hashes[monitor_key]:
                    log.info(f"[{monitor_name}] Change detected but diff already alerted — suppressing duplicate")
                    self.detector.store_content(monitor_key, content)
                    if alerts:
                        self.detector.store_alerts(monitor_key, alerts)
                    HEALTH.sites_status[site["name"]] = "ok"
                    return

                # ─── Content fingerprint dedup ───
                # Even if the diff hash changes (surrounding context shifts), check if
                # the core content sentences have already been alerted.
                # Extract meaningful sentences from the diff and check overlap.
                if monitor_key not in self.alerted_content_fingerprints:
                    self.alerted_content_fingerprints[monitor_key] = set()

                diff_sentences = _extract_fingerprint_sentences(diff_text)
                if diff_sentences:
                    # Check how many of the diff's key sentences we've already sent
                    already_sent = diff_sentences & self.alerted_content_fingerprints[monitor_key]
                    overlap_ratio = len(already_sent) / len(diff_sentences) if diff_sentences else 0

                    if overlap_ratio >= 0.5:  # 50%+ of sentences already alerted
                        log.info(f"[{monitor_name}] Change detected but {overlap_ratio:.0%} of content already alerted — suppressing repeat")
                        self.detector.store_content(monitor_key, content)
                        if alerts:
                            self.detector.store_alerts(monitor_key, alerts)
                        HEALTH.sites_status[site["name"]] = "ok"
                        return

                # Find new alerts by comparing to stored alerts AND already-sent titles
                old_alerts_raw = self.detector.get_alerts(monitor_key)
                if monitor_key not in self.alerted_titles:
                    self.alerted_titles[monitor_key] = set()

                new_alerts = []
                if alerts:
                    old_urls = set(a.get("url", "") for a in old_alerts_raw)
                    for a in alerts:
                        title_norm = a.get("title", "").strip().lower()
                        url = a.get("url", "")
                        # Skip if URL already seen OR title already alerted
                        if url in old_urls:
                            continue
                        if title_norm in self.alerted_titles[monitor_key]:
                            continue
                        new_alerts.append(a)
                    self.detector.store_alerts(monitor_key, alerts)

                # If we have structured alerts but none are new, suppress
                if alerts and not new_alerts:
                    log.info(f"[{monitor_name}] Change detected but all alerts already sent — suppressing")
                    self.detector.store_content(monitor_key, content)
                    HEALTH.sites_status[site["name"]] = "ok"
                    return

                for wh_key in monitor.get("alert_webhooks", ["primary"]):
                    wh_url = self.webhooks.get(wh_key, "")
                    if not wh_url:
                        continue

                    # ⏱️ Log timing for comparison (only log once per diff, not per webhook)
                    if wh_key == monitor.get("alert_webhooks", ["primary"])[0]:
                        preview = ""
                        if new_alerts:
                            preview = new_alerts[0].get("title", "")
                        elif diff_text:
                            preview = diff_text[:100]
                        self._log_detection(
                            source="page",
                            monitor_name=monitor_name,
                            content_preview=preview,
                            source_url=monitor_url,
                            content_hash=diff_hash,
                        )

                    # ─── Extract article content and analyze with DeepSeek ───
                    if new_alerts:
                        for alert_item in new_alerts[:3]:  # Limit to 3 to avoid slowdowns
                            alert_title = (alert_item.get("title", "") or "New Alert")[:256]
                            alert_url = alert_item.get("url", monitor_url)

                            # Extract full article content
                            article_content = ""
                            if alert_url and alert_url.startswith("http"):
                                article_content = await self._extract_article_content(alert_url, site["name"])

                            # If no article content from link, use the diff text
                            if not article_content or len(article_content) < 50:
                                article_content = diff_text or ""

                            # Analyze with DeepSeek
                            analysis = await self._analyze_with_deepseek(
                                alert_title, article_content, site["name"], monitor_name
                            )

                            headline = analysis.get("headline", alert_title)
                            tickers = analysis.get("tickers", [])
                            action = analysis.get("action", "")
                            summary = analysis.get("summary", "")

                            # Format Bloomberg-style alert
                            # Color based on action
                            action_colors = {
                                "BUY": 0x00C853, "ADD": 0x00C853,
                                "SELL": 0xFF1744, "CLOSE": 0xFF1744, "TRIM": 0xFF6D00,
                                "HOLD": 0xFFAB00,
                                "INFO": 0x2196F3,
                            }
                            alert_color = action_colors.get(action.upper(), 0xFF9900)

                            # Build ticker string
                            ticker_str = " ".join(f"${t}" for t in tickers) if tickers else ""

                            # Build fields
                            fields = []
                            if headline and headline != alert_title:
                                fields.append({
                                    "name": "📰 Headline",
                                    "value": headline,
                                    "inline": False,
                                })
                            if tickers:
                                fields.append({
                                    "name": "📊 Tickers",
                                    "value": ticker_str,
                                    "inline": True,
                                })
                            if action:
                                action_emoji = {"BUY": "🟢", "ADD": "🟢", "SELL": "🔴", "CLOSE": "🔴",
                                                "TRIM": "🟠", "HOLD": "🟡", "INFO": "🔵"}.get(action.upper(), "⚪")
                                fields.append({
                                    "name": "⚡ Action",
                                    "value": f"{action_emoji} **{action.upper()}**",
                                    "inline": True,
                                })
                            if summary:
                                fields.append({
                                    "name": "📋 Summary",
                                    "value": summary[:1000],
                                    "inline": False,
                                })
                            if alert_url:
                                fields.append({
                                    "name": "🔗 Source",
                                    "value": f"[Read Full Report]({alert_url})",
                                    "inline": False,
                                })

                            # Build title
                            if tickers and action:
                                embed_title = f"🚨 {ticker_str} — {action.upper()} | {monitor_name}"
                            else:
                                embed_title = f"🚨 {monitor_name}: {alert_title[:80]}"

                            await send_discord_alert(
                                wh_url,
                                embed_title[:256],
                                f"**{site['name']}** | {monitor_name}",
                                url=alert_url if alert_url else monitor_url,
                                color=alert_color,
                                fields=fields,
                            )
                            await asyncio.sleep(0.5)

                    elif diff_text and diff_text.strip():
                        # No structured alerts — analyze the diff text directly
                        analysis = await self._analyze_with_deepseek(
                            f"New content on {monitor_name}", diff_text, site["name"], monitor_name
                        )

                        headline = analysis.get("headline", "")
                        tickers = analysis.get("tickers", [])
                        action = analysis.get("action", "INFO")
                        summary = analysis.get("summary", "")

                        action_colors = {
                            "BUY": 0x00C853, "ADD": 0x00C853,
                            "SELL": 0xFF1744, "CLOSE": 0xFF1744, "TRIM": 0xFF6D00,
                            "HOLD": 0xFFAB00, "INFO": 0x2196F3,
                        }
                        alert_color = action_colors.get(action.upper(), 0xFF9900)
                        ticker_str = " ".join(f"${t}" for t in tickers) if tickers else ""

                        fields = []
                        if headline:
                            fields.append({"name": "📰 Headline", "value": headline, "inline": False})
                        if tickers:
                            fields.append({"name": "📊 Tickers", "value": ticker_str, "inline": True})
                        if action:
                            action_emoji = {"BUY": "🟢", "ADD": "🟢", "SELL": "🔴", "CLOSE": "🔴",
                                            "TRIM": "🟠", "HOLD": "🟡", "INFO": "🔵"}.get(action.upper(), "⚪")
                            fields.append({"name": "⚡ Action", "value": f"{action_emoji} **{action.upper()}**", "inline": True})
                        if summary:
                            fields.append({"name": "📋 Summary", "value": summary[:1000], "inline": False})
                        if not fields:
                            fields.append({"name": "New Content", "value": diff_text[:1000], "inline": False})
                        fields.append({"name": "🔗 Source", "value": f"[View Page]({monitor_url})", "inline": False})

                        if tickers and action:
                            embed_title = f"🚨 {ticker_str} — {action.upper()} | {monitor_name}"
                        else:
                            embed_title = f"🚨 Alert: {monitor_name}"

                        await send_discord_alert(
                            wh_url,
                            embed_title[:256],
                            f"**{site['name']}** | {monitor_name}",
                            url=monitor_url,
                            color=alert_color,
                            fields=fields,
                        )
                        await asyncio.sleep(0.3)

                    else:
                        # Fallback: minimal alert
                        await send_discord_alert(
                            wh_url,
                            f"🚨 Alert: {monitor_name}"[:256],
                            f"Change detected on **{site['name']}**\n[View Page]({monitor_url})",
                            url=monitor_url,
                            color=0xFF9900,
                            fields=[{"name": "Change Detected", "value": f"Content updated on [{monitor_name}]({monitor_url})", "inline": False}],
                        )
                        await asyncio.sleep(0.3)

                # Track what we just alerted on so we never send it again
                self.alerted_diff_hashes[monitor_key].add(diff_hash)
                # Keep set bounded (max 100 hashes per monitor)
                if len(self.alerted_diff_hashes[monitor_key]) > 100:
                    # Remove oldest entries by converting to list and keeping last 50
                    self.alerted_diff_hashes[monitor_key] = set(list(self.alerted_diff_hashes[monitor_key])[-50:])

                # Track content fingerprints so same articles don't re-alert even if diff shifts
                if diff_sentences:
                    self.alerted_content_fingerprints[monitor_key].update(diff_sentences)
                    # Keep bounded
                    if len(self.alerted_content_fingerprints[monitor_key]) > 500:
                        self.alerted_content_fingerprints[monitor_key] = set(
                            list(self.alerted_content_fingerprints[monitor_key])[-250:]
                        )

                # Track sent alert titles
                for a in new_alerts:
                    title_norm = a.get("title", "").strip().lower()
                    if title_norm:
                        self.alerted_titles[monitor_key].add(title_norm)
            else:
                log.info(f"[{monitor_name}] No change")

            self.detector.store_content(monitor_key, content)
            if alerts:
                self.detector.store_alerts(monitor_key, alerts)

            # ─── Background API monitoring ───
            # Check captured XHR/fetch responses for trade alerts not visible on page
            if api_responses:
                api_key = f"{monitor_key}:api"
                if api_key not in self.alerted_diff_hashes:
                    self.alerted_diff_hashes[api_key] = set()
                if api_key not in self.alerted_titles:
                    self.alerted_titles[api_key] = set()

                for resp_data in api_responses:
                    body = resp_data["body"]
                    resp_url = resp_data["url"]

                    # Only process if body contains today's date
                    if not page_has_today_date(body):
                        continue

                    # Hash this response to dedup
                    resp_hash = hashlib.sha256(body.strip().encode()).hexdigest()[:16]
                    if resp_hash in self.alerted_diff_hashes[api_key]:
                        continue

                    # Extract trade-relevant lines from the response
                    trade_lines = []
                    try:
                        # Try parsing as JSON
                        data = json.loads(body)
                        body_text = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)
                    except (json.JSONDecodeError, ValueError):
                        body_text = body

                    for line in body_text.split("\n"):
                        line_s = line.strip()
                        if not line_s or len(line_s) < 10:
                            continue
                        line_lower = line_s.lower()
                        if any(kw in line_lower for kw in ["trade alert", "flash alert", "entry price", "stop loss", "profit target", "action to take", "buy to open", "sell to close", "limit order", "price target", "strike price"]):
                            # Skip if already alerted on this exact line
                            line_norm = line_s.lower().strip()
                            if line_norm not in self.alerted_titles[api_key]:
                                trade_lines.append(line_s)

                    if not trade_lines:
                        continue

                    # ⏱️ Log timing for comparison
                    self._log_detection(
                        source="api_passive",
                        monitor_name=monitor_name,
                        content_preview=trade_lines[0] if trade_lines else "",
                        source_url=resp_url,
                        content_hash=resp_hash,
                    )

                    log.info(f"[{monitor_name}] 🔍 BACKGROUND API ALERT from {resp_url[:80]}")
                    log.info(f"[{monitor_name}] Trade content: {trade_lines[0][:100]}...")

                    # Build Discord alert
                    alert_body = "\n".join(trade_lines[:10])
                    if len(alert_body) > 900:
                        alert_body = alert_body[:900] + "..."

                    for wh_key in monitor.get("alert_webhooks", ["primary"]):
                        wh_url = self.webhooks.get(wh_key, "")
                        if not wh_url:
                            continue
                        await send_discord_alert(
                            wh_url,
                            f"🔍 {monitor_name}: Background API Alert",
                            f"Trade data found in background request on **{site['name']}**\n[View Page]({monitor_url})",
                            url=monitor_url,
                            color=0x9B59B6,
                            fields=[{
                                "name": "📡 API Trade Data",
                                "value": alert_body[:1000],
                                "inline": False,
                            }, {
                                "name": "Source",
                                "value": resp_url[:200],
                                "inline": False,
                            }],
                        )
                        await asyncio.sleep(0.3)

                    # Track as sent
                    self.alerted_diff_hashes[api_key].add(resp_hash)
                    for line in trade_lines[:10]:
                        self.alerted_titles[api_key].add(line.lower().strip())

            # ─── API Discovery: track endpoints for fast polling ───
            if self.api_discovery_mode and discovered_urls:
                for disc in discovered_urls:
                    disc_url = disc["url"]
                    # Normalize URL for dedup: strip query params with user-specific data
                    # but keep content-type/filter params
                    parsed_url = urlparse(disc_url)
                    # Use scheme + netloc + path as the dedup key
                    norm_key = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

                    is_new = norm_key not in self.discovered_apis
                    if is_new:
                        self.discovered_apis[norm_key] = {
                            "full_url": disc_url,  # Keep full URL for actual polling
                            "site": site["name"],
                            "monitor": monitor_name,
                            "monitors_seen": {monitor_name},
                            "has_trade": disc["has_trade"],
                            "size": disc["size"],
                            "content_type": disc["content_type"],
                            "preview": disc["body_preview"][:150],
                            "first_seen_cycle": self.api_discovery_cycles,
                            "times_seen": 0,
                            "notified": False,
                        }
                    else:
                        # Track which monitors also see this endpoint
                        self.discovered_apis[norm_key]["monitors_seen"].add(monitor_name)

                    self.discovered_apis[norm_key]["times_seen"] += 1
                    if disc["has_trade"]:
                        self.discovered_apis[norm_key]["has_trade"] = True

                    # Send ONE Discord notification per unique endpoint, only first time
                    if is_new and disc["has_trade"] and not self.discovered_apis[norm_key]["notified"]:
                        self.discovered_apis[norm_key]["notified"] = True
                        log.info(f"🎯 NEW TRADE API: {norm_key[:100]} (from {monitor_name})")
                        first_wh = next(iter(self.webhooks.values()), "")
                        if first_wh:
                            await send_discord_alert(
                                first_wh,
                                f"🎯 New Trade API Endpoint Found",
                                f"**First seen on:** {monitor_name}\n**Site:** {site['name']}\n"
                                f"**URL:** `{norm_key[:200]}`\n"
                                f"**Size:** {disc['size']}b | **Type:** {disc['content_type']}\n"
                                f"**Preview:** ```{disc['body_preview'][:300]}```",
                                color=0x00FF00,
                            )

            HEALTH.sites_status[site["name"]] = "ok"

        except Exception as e:
            raise e
        finally:
            try:
                if not page.is_closed():
                    await page.close()
            except:
                pass

    async def _fast_api_poll_loop(self):
        """Fast-poll discovered trade API endpoints every few seconds.

        Uses lightweight HTTP requests (no browser) to check for changes
        in API responses much faster than full page loads.

        Polls both trade-keyword APIs and content listing APIs found by probe.
        """
        # Select endpoints to poll: trade APIs + content APIs
        poll_apis = {url: info for url, info in self.discovered_apis.items()
                     if info.get("has_trade") or info.get("is_content_api")}
        if not poll_apis:
            log.info("Fast API poller: no APIs to poll yet, waiting...")
            await asyncio.sleep(30)
            poll_apis = {url: info for url, info in self.discovered_apis.items()
                         if info.get("has_trade") or info.get("is_content_api")}
            if not poll_apis:
                log.info("Fast API poller: still no APIs found, exiting")
                self.api_poll_running = False
                return

        trade_count = sum(1 for i in poll_apis.values() if i.get("has_trade"))
        content_count = sum(1 for i in poll_apis.values() if i.get("is_content_api"))
        log.info(f"Fast API poller: monitoring {len(poll_apis)} endpoints ({trade_count} trade, {content_count} content) every {self.api_poll_interval}s")

        # Get cookies from browser context for authenticated requests
        cookie_headers = await self._build_cookie_headers()

        # Store last response hashes for change detection
        last_hashes: dict[str, str] = {}
        last_cookie_refresh = time.time()
        last_endpoint_refresh = time.time()
        COOKIE_REFRESH_INTERVAL = 300  # Refresh cookies every 5 minutes
        ENDPOINT_REFRESH_INTERVAL = 120  # Check for new endpoints every 2 minutes
        poll_count = 0

        headers_base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with aiohttp.ClientSession() as session:
            while self.is_active_hours():
                poll_start = time.time()
                poll_count += 1

                # Refresh cookies periodically
                if time.time() - last_cookie_refresh > COOKIE_REFRESH_INTERVAL:
                    try:
                        cookie_headers = await self._build_cookie_headers()
                        last_cookie_refresh = time.time()
                        log.debug("Fast poller: refreshed cookies")
                    except Exception as e:
                        log.debug(f"Fast poller: cookie refresh failed: {e}")

                # Refresh endpoint list periodically (pick up newly discovered APIs)
                if time.time() - last_endpoint_refresh > ENDPOINT_REFRESH_INTERVAL:
                    new_poll_apis = {url: info for url, info in self.discovered_apis.items()
                                     if info.get("has_trade") or info.get("is_content_api")}
                    if len(new_poll_apis) > len(poll_apis):
                        added = len(new_poll_apis) - len(poll_apis)
                        log.info(f"Fast poller: picked up {added} new endpoint(s), now monitoring {len(new_poll_apis)}")
                        poll_apis = new_poll_apis
                    last_endpoint_refresh = time.time()

                # Log poll count every 100 polls
                if poll_count % 100 == 0:
                    log.info(f"Fast API poller: {poll_count} polls completed, monitoring {len(poll_apis)} endpoints")

                for api_url, info in poll_apis.items():
                    try:
                        # Use full URL (with query params) for actual HTTP request
                        poll_url = info.get("full_url", api_url)
                        # Match cookies to domain
                        parsed = urlparse(poll_url)
                        domain = parsed.netloc

                        headers = dict(headers_base)
                        # Find matching cookies
                        for cookie_domain, cookie_str in cookie_headers.items():
                            if cookie_domain in domain or domain in cookie_domain:
                                headers["Cookie"] = cookie_str
                                break

                        async with session.get(poll_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8), ssl=False) as resp:
                            if resp.status != 200:
                                continue

                            body = await resp.text()
                            if len(body) < 50:
                                continue

                            # Hash for change detection
                            body_hash = hashlib.sha256(body.encode()).hexdigest()[:16]
                            old_hash = last_hashes.get(api_url)
                            last_hashes[api_url] = body_hash

                            if old_hash is None:
                                # First poll — baseline
                                continue

                            if body_hash == old_hash:
                                # No change
                                continue

                            # Change detected! Check if it has today's date
                            if not page_has_today_date(body):
                                continue

                            # Check if already alerted on this content
                            if body_hash in self.api_alerted_hashes:
                                continue

                            is_content_api = info.get("is_content_api", False)

                            # For content APIs: any change with today's date = new content
                            # For trade APIs: require trade keywords
                            body_lower = body.lower()
                            trade_keywords = [
                                "trade alert", "new alert", "flash alert",
                                "entry price", "stop loss", "profit target",
                                "action to take", "limit order", "market order",
                                "buy to open", "sell to close", "buy to close", "sell to open",
                                "strike price", "expiration date", "price target",
                            ]
                            has_trade_kw = any(kw in body_lower for kw in trade_keywords)

                            if not is_content_api and not has_trade_kw:
                                # Trade API without trade keywords = not relevant
                                continue

                            # Extract what changed
                            trade_lines = []
                            try:
                                data = json.loads(body)
                                body_text = json.dumps(data, indent=2)
                            except (json.JSONDecodeError, ValueError):
                                body_text = body

                            if has_trade_kw:
                                # Extract trade-specific lines
                                for line in body_text.split("\n"):
                                    line_s = line.strip()
                                    if len(line_s) < 10:
                                        continue
                                    if any(kw in line_s.lower() for kw in trade_keywords):
                                        line_norm = line_s.lower().strip()
                                        if line_norm not in self.api_alerted_titles:
                                            trade_lines.append(line_s)
                            elif is_content_api:
                                # For content APIs, extract meaningful new lines
                                # Compare old vs new body to find additions
                                old_body = self._api_last_bodies.get(api_url, "")
                                old_lines = set(l.strip().lower() for l in old_body.split("\n") if l.strip())
                                for line in body_text.split("\n"):
                                    line_s = line.strip()
                                    if len(line_s) < 20:
                                        continue
                                    if line_s.lower() not in old_lines:
                                        line_norm = line_s.lower().strip()
                                        if line_norm not in self.api_alerted_titles:
                                            trade_lines.append(line_s)

                            # Store current body for next comparison
                            self._api_last_bodies[api_url] = body_text

                            if not trade_lines:
                                continue

                            # Calculate timing
                            detect_time = now_in_tz(self.hours.get("timezone", "US/Eastern"))
                            time_str = detect_time.strftime("%I:%M:%S %p EST")

                            # ⏱️ Log timing for comparison
                            self._log_detection(
                                source="api_fast",
                                monitor_name=info["monitor"],
                                content_preview=trade_lines[0] if trade_lines else "",
                                source_url=api_url,
                                content_hash=body_hash,
                            )

                            log.info(f"⚡ FAST API ALERT [{time_str}]: {info['site']} / {info['monitor']}")
                            log.info(f"  Source: {api_url[:100]}")
                            log.info(f"  Content: {trade_lines[0][:100]}")

                            alert_body = "\n".join(trade_lines[:8])
                            if len(alert_body) > 800:
                                alert_body = alert_body[:800] + "..."

                            # Analyze with DeepSeek
                            analysis = await self._analyze_with_deepseek(
                                f"API Alert: {info['monitor']}", alert_body,
                                info["site"], info["monitor"]
                            )

                            headline = analysis.get("headline", "")
                            tickers = analysis.get("tickers", [])
                            action = analysis.get("action", "INFO")
                            summary = analysis.get("summary", "")
                            ticker_str = " ".join(f"${t}" for t in tickers) if tickers else ""

                            action_colors = {
                                "BUY": 0x00C853, "ADD": 0x00C853,
                                "SELL": 0xFF1744, "CLOSE": 0xFF1744, "TRIM": 0xFF6D00,
                                "HOLD": 0xFFAB00, "INFO": 0x2196F3,
                            }
                            alert_color = action_colors.get(action.upper(), 0xE91E63)

                            # Send to all webhooks
                            for wh_url in self.webhooks.values():
                                if not wh_url:
                                    continue

                                fields = []
                                if headline:
                                    fields.append({"name": "📰 Headline", "value": headline, "inline": False})
                                if tickers:
                                    fields.append({"name": "📊 Tickers", "value": ticker_str, "inline": True})
                                if action:
                                    action_emoji = {"BUY": "🟢", "ADD": "🟢", "SELL": "🔴", "CLOSE": "🔴",
                                                    "TRIM": "🟠", "HOLD": "🟡", "INFO": "🔵"}.get(action.upper(), "⚪")
                                    fields.append({"name": "⚡ Action", "value": f"{action_emoji} **{action.upper()}**", "inline": True})
                                if summary:
                                    fields.append({"name": "📋 Summary", "value": summary[:1000], "inline": False})
                                fields.append({
                                    "name": "⏱️ Detection",
                                    "value": f"Fast API at {time_str}\n(Page check every {self.check_interval}s)",
                                    "inline": False,
                                })

                                if tickers and action:
                                    embed_title = f"⚡ {ticker_str} — {action.upper()} | {info['monitor']} (API)"
                                else:
                                    embed_title = f"⚡ FAST API: {info['monitor']}"

                                await send_discord_alert(
                                    wh_url,
                                    embed_title[:256],
                                    f"**{info['site']}** | Detected via API",
                                    color=alert_color,
                                    fields=fields,
                                )
                                await asyncio.sleep(0.3)

                            # Track as sent
                            self.api_alerted_hashes.add(body_hash)
                            for line in trade_lines[:8]:
                                self.api_alerted_titles.add(line.lower().strip())

                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        log.debug(f"Fast API poll error for {api_url[:60]}: {e}")
                        continue

                # Sleep for the poll interval, accounting for time spent polling
                elapsed = time.time() - poll_start
                sleep_time = max(0.5, self.api_poll_interval - elapsed)
                await asyncio.sleep(sleep_time)

        log.info("Fast API poller stopped (outside active hours)")
        self.api_poll_running = False

    async def _build_cookie_headers(self) -> dict:
        """Build cookie header strings per domain from browser context."""
        cookies = await self.context.cookies()
        cookie_jars = {}
        for c in cookies:
            domain = c.get("domain", "").lstrip(".")
            if domain not in cookie_jars:
                cookie_jars[domain] = []
            cookie_jars[domain].append(f"{c['name']}={c['value']}")
        result = {}
        for domain, parts in cookie_jars.items():
            result[domain] = "; ".join(parts)
        return result

    async def _refresh_api_cookies(self):
        """Refresh cookies from browser context for API polling."""
        cookies = await self.context.cookies()
        cookie_jars = {}
        for c in cookies:
            domain = c.get("domain", "").lstrip(".")
            if domain not in cookie_jars:
                cookie_jars[domain] = []
            cookie_jars[domain].append(f"{c['name']}={c['value']}")
        result = {}
        for domain, parts in cookie_jars.items():
            result[domain] = "; ".join(parts)
        return result

    async def _ai_health_loop(self):
        """Periodic AI health checks during active hours."""
        if not self.ai_monitor.enabled:
            log.info("AI health monitor disabled")
            return

        # Wait a bit before first check to gather some data
        await asyncio.sleep(120)

        while True:
            if self.is_active_hours():
                log.info("Running AI health check...")
                try:
                    await self.ai_monitor.run_check()
                except Exception as e:
                    log.error(f"AI health check failed: {e}")

            await asyncio.sleep(self.ai_monitor.interval)


# ─── CLI ───────────────────────────────────────────────────────────────────────

async def test_webhooks(config_path: str):
    config = load_config(config_path)
    for name, url in config.get("discord_webhooks", {}).items():
        log.info(f"Testing webhook: {name}")
        await send_discord_alert(
            url, "🧪 Test Alert",
            "Newsletter Monitor webhook test successful!",
            color=0x2ECC71,
            fields=[{"name": "Webhook", "value": name, "inline": True}],
        )
    log.info("Done!")


async def recon_all_sites(config_path: str):
    """Recon all newsletter sites to find hidden/unmonitored content pages."""
    config = load_config(config_path)
    primary_wh = list(config.get("discord_webhooks", {}).values())[0] if config.get("discord_webhooks") else ""

    # Build set of already-monitored URLs
    monitored_urls = set()
    for site in config.get("sites", []):
        for mon in site.get("monitors", []):
            monitored_urls.add(mon.get("url", "").lower().rstrip("/"))

    # Site-specific probe paths
    SITE_PROBES = {
        "Paradigm Press": {
            "base": "https://my.paradigmpressgroup.com",
            "probe_paths": [
                # Ray Blanco services
                "/subscription/ray-blanco", "/subscription/rayblanco",
                "/subscription/ray-blanco/alerts", "/subscription/ray-blanco/trades",
                "/subscription/tpc", "/subscription/tpc/alerts", "/subscription/tpc/trades", "/subscription/tpc/portfolio",
                "/subscription/technology-profits-confidential", "/subscription/technology-profits-confidential/alerts",
                "/subscription/tpd", "/subscription/technology-profits-daily",
                "/subscription/cpe", "/subscription/crypto-profits-extreme", "/subscription/crypto-profits-extreme/alerts",
                "/subscription/bba", "/subscription/biotech-breakout-alert", "/subscription/bba/alerts",
                "/subscription/psf", "/subscription/penny-stock-fortunes",
                "/subscription/fia", "/subscription/fda-insider-alert",
                # Other editors/authors
                "/editor/ray-blanco", "/editor/chris-campbell", "/editor/zach-scheidt",
                "/editor/greg-guenthner", "/editor/chris-cimorelli", "/editor/dan-amoss",
                "/editor/byron-king", "/editor/sean-ring", "/editor/jody-chudley",
                # More subscription slugs
                "/subscription/altucher-confidential", "/subscription/altucher-confidential/alerts",
                "/subscription/strategic-intelligence", "/subscription/strategic-intelligence/alerts",
                "/subscription/rickards-gold-speculator", "/subscription/rickards-gold-speculator/alerts",
                "/subscription/pro-momentum-trader", "/subscription/pro-momentum-trader/alerts",
                "/subscription/extreme-alpha", "/subscription/extreme-alpha/alerts",
                "/subscription/microcap-millionaire", "/subscription/microcap-millionaire/alerts",
                "/subscription/buy-the-dip", "/subscription/buy-the-dip/alerts",
                "/subscription/sector-alpha", "/subscription/sector-alpha/alerts",
                "/subscription/fast-lane-fortunes", "/subscription/fast-lane-fortunes/alerts",
                "/subscription/income-alliance", "/subscription/income-alliance/alerts",
                # API / config endpoints
                "/wp-json/wp/v2/posts?per_page=5&orderby=date",
                "/wp-json/wp/v2/posts?search=ray+blanco&per_page=5",
                "/api/alerts", "/api/v1/alerts", "/api/v1/publications",
                "/my-subscriptions", "/subscriptions", "/dashboard",
                "/message-center",
            ],
            "search_keywords": ["ray", "blanco", "technology profits", "tpc", "biotech", "fda", "crypto profits", "cpe", "bba"],
        },
        "Banyan Hill": {
            "base": "https://banyanhill.com",
            "probe_paths": [
                # Services we might not have
                "/strategic-fortunes-pro/", "/strategic-fortunes-pro/portfolio/",
                "/extreme-fortunes/", "/extreme-fortunes/portfolio/", "/extreme-fortunes/quarterly-reports/",
                "/tim-sykes-weekend-trader/", "/tim-sykes-weekend-trader/portfolio/",
                "/tim-sykes-xgpt-trader/", "/tim-sykes-xgpt-trader/portfolio/",
                "/trademonster-ai-alerts/", "/trademonster-ai-alerts/portfolio/",
                # Other Banyan Hill services
                "/profit-amplifier/", "/profit-amplifier/trade-alerts/",
                "/alpha-investor/", "/alpha-investor/trade-alerts/", "/alpha-investor/portfolio/",
                "/total-wealth/", "/total-wealth/trade-alerts/",
                "/true-momentum/", "/true-momentum/trade-alerts/",
                "/rapid-profit-trader/", "/rapid-profit-trader/trade-alerts/",
                "/bauman-daily/", "/bauman-daily/trade-alerts/",
                "/smart-profits-daily/", "/smart-profits-daily/trade-alerts/",
                "/american-investor-today/", "/american-investor-today/trade-alerts/",
                "/venture-society/", "/venture-society/trade-alerts/",
                "/crypto-flash-trader/", "/crypto-flash-trader/trade-alerts/",
                "/micro-cap-confidential/", "/micro-cap-confidential/trade-alerts/",
                "/stock-power-daily/", "/stock-power-daily/trade-alerts/",
                "/money-flow-trader/", "/money-flow-trader/trade-alerts/",
                "/options-power-trader/", "/options-power-trader/trade-alerts/",
                "/10x-profits/", "/10x-profits/trade-alerts/",
                "/profit-surge-trader/", "/profit-surge-trader/trade-alerts/",
                # Account / general
                "/my-account/", "/my-subscriptions/",
                "/wp-json/wp/v2/posts?per_page=5&orderby=date",
            ],
            "search_keywords": ["trade alert", "buy", "sell", "new alert", "portfolio", "entry price", "stop loss"],
        },
        "InvestorPlace": {
            "base": "https://investorplace.com",
            "probe_paths": [
                # Sub-pages of current monitors
                "/acceleratedprofits/my-portfolio/", "/acceleratedprofits/trade-alerts/",
                "/airevolutionportfolio/my-portfolio/", "/airevolutionportfolio/trade-alerts/",
                "/breakthroughstocks/my-portfolio/", "/breakthroughstocks/trade-alerts/",
                "/growthinvestor/my-portfolio/", "/growthinvestor/trade-alerts/",
                "/platinumgrowthclub/my-portfolio/", "/platinumgrowthclub/trade-alerts/",
                "/powerportfolio/my-portfolio/", "/powerportfolio/trade-alerts/",
                # Other InvestorPlace services
                "/innovationinvestor/", "/innovationinvestor/trade-alerts/",
                "/earlystagetrade/", "/earlystagetrade/trade-alerts/",
                "/tradingwithluke/", "/tradingwithluke/trade-alerts/",
                "/rajeventures/", "/rajeventures/trade-alerts/",
                "/ultimatecrypto/", "/ultimatecrypto/trade-alerts/",
                "/rajearningsinvestor/", "/rajearningsinvestor/trade-alerts/",
                "/elitetradesmithtravel/", "/elitetradesmithtravel/trade-alerts/",
                "/tradesmith/", "/tradesmith/trade-alerts/",
                "/technologicalmillionaire/", "/technologicalmillionaire/trade-alerts/",
                "/buyingblitz/", "/buyingblitz/trade-alerts/",
                "/speculationreport/", "/speculationreport/trade-alerts/",
                "/crypto-investor-network/", "/crypto-investor-network/trade-alerts/",
                # Account
                "/my-account/", "/my-subscriptions/",
            ],
            "search_keywords": ["trade alert", "buy", "sell", "flash alert", "new recommendation", "entry price"],
        },
    }

    log.info("=" * 60)
    log.info("FULL SITE RECON — Finding all hidden/unmonitored content")
    log.info("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        all_results = []  # For Discord summary

        for site_cfg in config.get("sites", []):
            site_name = site_cfg["name"]
            site_probes = SITE_PROBES.get(site_name)
            if not site_probes:
                log.info(f"No recon config for {site_name}, skipping")
                continue

            base = site_probes["base"]
            email = site_cfg.get("login", {}).get("email", "")
            password = site_cfg.get("login", {}).get("password", "")

            log.info(f"\n{'=' * 60}")
            log.info(f"RECON: {site_name}")
            log.info(f"{'=' * 60}")

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            api_requests = []
            page = await context.new_page()

            def make_request_handler(reqs):
                def on_request(request):
                    url = request.url
                    if any(kw in url.lower() for kw in ["api", "json", "ajax", "wp-json", "graphql", "alert", "trade", "content", "subscription"]):
                        reqs.append({"method": request.method, "url": url})
                return on_request

            page.on("request", make_request_handler(api_requests))

            # ─── Login using the SAME handlers as the main bot ───
            log.info(f"Logging in to {site_name} using production handler...")
            handler = get_handler(site_cfg)

            try:
                login_success = await handler.login(page)
                if not login_success:
                    log.error(f"  ❌ Login FAILED for {site_name} — recon will hit paywalls")
                    # Try to continue anyway — some public pages might still be useful
                else:
                    log.info(f"  Login handler returned success")

                # Verify login by navigating to a known protected page
                verify_urls = {
                    "Paradigm Press": "https://my.paradigmpressgroup.com/subscription/x10",
                    "Banyan Hill": "https://banyanhill.com/strategic-fortunes-pro/trade-alerts/",
                    "InvestorPlace": "https://investorplace.com/acceleratedprofits/",
                }
                verify_url = verify_urls.get(site_name, base)
                await page.goto(verify_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)

                is_logged_in = await handler.check_if_logged_in(page)
                if is_logged_in:
                    log.info(f"  ✅ Login VERIFIED — authenticated session active for {site_name}")
                    page_text = (await page.inner_text("body"))[:200]
                    log.info(f"  Verified page content: {page_text[:100]}...")
                else:
                    log.error(f"  ❌ Login VERIFICATION FAILED — {site_name} session not authenticated")
                    log.error(f"  Current URL: {page.url}")
                    body_preview = (await page.inner_text("body"))[:200]
                    log.error(f"  Page shows: {body_preview[:100]}...")
                    # Send warning to Discord
                    if primary_wh:
                        await send_discord_alert(
                            primary_wh,
                            f"⚠️ Recon: {site_name} Login Failed",
                            f"Could not authenticate to {site_name}. Recon results may be incomplete (hitting paywalls).\nURL: {page.url}",
                            color=0xFF0000,
                        )

            except Exception as e:
                log.error(f"  Login error: {e}")
                await context.close()
                continue

            # ─── Scan landing page for all links ───
            log.info(f"Scanning {site_name} for subscription/content links...")

            try:
                await page.goto(base, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
            except:
                pass

            all_page_links = await page.query_selector_all("a[href]")
            subscription_links = set()
            highlight_links = set()
            search_kw = site_probes["search_keywords"]

            for link in all_page_links:
                try:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if not href:
                        continue
                    href_lower = href.lower()
                    text_lower = text.lower()

                    if any(kw in href_lower for kw in ["/subscription/", "/editor/", "trade-alerts", "/portfolio", "fortunes", "trader", "investor"]):
                        subscription_links.add((text[:80], href))
                    if site_name == "InvestorPlace" and "investorplace.com/" in href_lower:
                        if not any(skip in href_lower for skip in ["wp-", "login", "cdn", ".js", ".css", "author", "category", "tag", "ipa-"]):
                            path = href_lower.replace("https://investorplace.com/", "").strip("/")
                            if path and "/" not in path and 3 < len(path) < 40:
                                subscription_links.add((text[:80], href))

                    if any(kw in text_lower or kw in href_lower for kw in search_kw):
                        highlight_links.add((text[:80], href))
                except:
                    continue

            log.info(f"  Found {len(subscription_links)} content links on {site_name}")
            for text, href in sorted(subscription_links, key=lambda x: x[1]):
                is_monitored = href.lower().rstrip("/") in monitored_urls
                marker = "  MONITORED" if is_monitored else "  NOT MONITORED"
                log.info(f"    [{text}] -> {href} [{marker}]")

            if highlight_links:
                log.info(f"  Keyword matches ({len(highlight_links)}):")
                for text, href in highlight_links:
                    log.info(f"    [{text}] -> {href}")

            # ─── Scan page source for API endpoints ───
            try:
                page_source = await page.content()
                api_matches = re.findall(r'["\'](/(?:api|wp-json|graphql)[^"\']{5,100})["\']', page_source)
                if api_matches:
                    log.info(f"  API endpoints in source:")
                    for url in set(api_matches):
                        log.info(f"    {url}")

                slug_matches = re.findall(r'"slug"\s*:\s*"([^"]+)"', page_source)
                if slug_matches:
                    log.info(f"  Slugs found: {set(slug_matches)}")
            except:
                pass

            # ─── Add dashboard-discovered links to probe list ───
            probe_paths = list(site_probes["probe_paths"])
            for text, href in subscription_links:
                href_path = href.replace(base, "")
                if href_path.startswith("/") and href_path not in probe_paths:
                    probe_paths.append(href_path)
                    for suffix in ["/alerts", "/trades", "/trade-alerts", "/portfolio", "/reports", "/quarterly-reports"]:
                        candidate = href_path.rstrip("/") + suffix
                        if candidate not in probe_paths:
                            probe_paths.append(candidate)

            probe_paths = list(dict.fromkeys(probe_paths))  # Dedupe preserving order

            # ─── Probe candidate URLs ───
            log.info(f"\n  Probing {len(probe_paths)} candidate URLs for {site_name}...")
            found_pages = []

            for path in probe_paths:
                url = f"{base}{path}" if path.startswith("/") else path
                probe_page = await context.new_page()
                try:
                    resp = await probe_page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(2)
                    status = resp.status if resp else "?"
                    final_url = probe_page.url
                    text = await probe_page.inner_text("body")
                    text_preview = text[:300].replace("\n", " ").strip()
                    has_content = (len(text.strip()) > 100
                                  and "not found" not in text.lower()[:200]
                                  and "404" not in text[:50]
                                  and "page not found" not in text.lower()[:200])
                    is_login = (any(kw in final_url.lower() for kw in ["login", "log-in", "signin"])
                                or any(kw in text.lower()[:300] for kw in ["log in", "enter your email", "sign in to", "customer login"]))

                    if has_content and not is_login and status != 404:
                        is_monitored = final_url.lower().rstrip("/") in monitored_urls
                        text_lower = text.lower()

                        # Detect paywall/subscription walls
                        paywall_indicators = [
                            "subscribe to access", "subscription required", "premium content",
                            "become a member", "join now to access", "unlock this content",
                            "you need to be a subscriber", "not yet a subscriber",
                            "upgrade your account", "purchase a subscription",
                            "this content is for subscribers only", "members only",
                            "sign up to read", "start your subscription",
                        ]
                        is_paywalled = any(pw in text_lower[:500] for pw in paywall_indicators)

                        # Detect if we're seeing actual trade content (behind the paywall)
                        has_trade = any(kw in text_lower for kw in [
                            "buy", "sell", "trade alert", "new alert", "recommendation",
                            "entry price", "stop loss", "profit target", "flash alert",
                            "position size", "limit order", "market order",
                        ])
                        has_highlight = any(kw in text_lower for kw in search_kw)

                        if is_paywalled:
                            marker = "PAYWALLED"
                            log.info(f"    [🔒 {marker}] [{status}] {path}")
                            log.info(f"         -> {final_url}")
                            log.info(f"         Content: {text_preview[:150]}...")
                            found_pages.append({
                                "path": path, "url": final_url, "status": status,
                                "has_trade": False, "has_highlight": False,
                                "is_monitored": is_monitored, "is_paywalled": True,
                                "preview": text_preview[:200],
                            })
                        else:
                            if has_highlight:
                                marker = "TARGET"
                            elif has_trade:
                                marker = "TRADE"
                            elif is_monitored:
                                marker = "MONITORED"
                            else:
                                marker = "PAGE"

                            mon_status = " [MONITORED]" if is_monitored else " [NOT MONITORED]"
                            log.info(f"    [{marker}] [{status}] {path}{mon_status}")
                            log.info(f"         -> {final_url}")
                            log.info(f"         Content: {text_preview[:150]}...")

                            found_pages.append({
                                "path": path, "url": final_url, "status": status,
                                "has_trade": has_trade, "has_highlight": has_highlight,
                                "is_monitored": is_monitored, "is_paywalled": False,
                                "preview": text_preview[:200],
                            })

                        # Look for sub-links with trade keywords
                        sub_links = await probe_page.query_selector_all("a[href]")
                        for sl in sub_links:
                            try:
                                sh = await sl.get_attribute("href")
                                st = (await sl.inner_text()).strip()
                                if sh and any(kw in st.lower() for kw in ["buy", "sell", "alert", "trade"]) and len(st) > 5:
                                    log.info(f"         Sub-link: [{st[:60]}] -> {sh}")
                            except:
                                continue
                except Exception:
                    pass  # Silently skip errors during mass probing
                finally:
                    await probe_page.close()

            # ─── Report API requests ───
            if api_requests:
                seen = set()
                unique_apis = []
                for req in api_requests:
                    if req["url"] not in seen:
                        seen.add(req["url"])
                        unique_apis.append(req)
                if unique_apis:
                    log.info(f"\n  Captured {len(unique_apis)} API/XHR requests:")
                    for req in unique_apis[:30]:
                        log.info(f"    [{req['method']}] {req['url'][:150]}")

            # ─── Build site summary for Discord ───
            unmonitored = [fp for fp in found_pages if not fp["is_monitored"] and not fp.get("is_paywalled")]
            trade_pages = [fp for fp in found_pages if fp["has_trade"]]
            paywalled = [fp for fp in found_pages if fp.get("is_paywalled")]
            authenticated = [fp for fp in found_pages if not fp.get("is_paywalled")]

            site_summary = [
                f"\n**{site_name}**",
                f"Links on landing: {len(subscription_links)} | Authenticated pages: {len(authenticated)} | Paywalled: {len(paywalled)} | Trade content: {len(trade_pages)} | APIs: {len(api_requests)}",
            ]

            if paywalled:
                site_summary.append(f"🔒 **Paywalled ({len(paywalled)}) — login may have failed:**")
                for fp in paywalled[:5]:
                    site_summary.append(f"🔒 {fp['url']}")

            if unmonitored:
                site_summary.append(f"⚠️ **Unmonitored with content ({len(unmonitored)}):**")
                for fp in unmonitored:
                    marker = "🎯" if fp["has_highlight"] else ("⚡" if fp["has_trade"] else "📄")
                    site_summary.append(f"{marker} {fp['url']}")

            all_results.extend(site_summary)
            log.info(f"\n  {site_name} recon complete: {len(found_pages)} pages, {len(unmonitored)} unmonitored, {len(paywalled)} paywalled")

            await context.close()

        # ─── Send combined summary to Discord ───
        log.info("\n" + "=" * 60)
        log.info("FULL RECON COMPLETE")
        log.info("=" * 60)

        if primary_wh and all_results:
            summary_text = "\n".join(all_results)
            if len(summary_text) > 3900:
                chunks = []
                current = ""
                for line in all_results:
                    if len(current) + len(line) + 1 > 3900:
                        chunks.append(current)
                        current = line
                    else:
                        current += "\n" + line if current else line
                if current:
                    chunks.append(current)

                for i, chunk in enumerate(chunks):
                    await send_discord_alert(
                        primary_wh,
                        f"🔍 Site Recon ({i+1}/{len(chunks)})",
                        chunk,
                        color=0x3498DB,
                    )
                    await asyncio.sleep(1)
            else:
                await send_discord_alert(
                    primary_wh,
                    "🔍 Full Site Recon Complete",
                    summary_text,
                    color=0x3498DB,
                )

        await browser.close()
        log.info("Recon finished!")


def main():
    parser = argparse.ArgumentParser(description="Newsletter Monitor Bot")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--test-webhook", action="store_true")
    parser.add_argument("--recon", action="store_true", help="Recon all sites for hidden/unmonitored content")
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()

    # Start health server IMMEDIATELY so Railway healthcheck passes
    # even if the rest of startup takes a while
    port = int(os.environ.get("PORT", "8080"))
    start_health_server(port)
    log.info(f"Health server started on port {port} (pre-init)")

    if args.test_webhook:
        asyncio.run(test_webhooks(args.config))
        return

    if args.recon:
        asyncio.run(recon_all_sites(args.config))
        return

    monitor = NewsletterMonitor(args.config)
    if args.visible:
        monitor.headless = False

    # Retry loop: if the bot crashes, restart it (health server stays alive)
    max_retries = 5
    for attempt in range(max_retries):
        try:
            asyncio.run(monitor.start(login_only=args.login_only))
            break  # Clean exit
        except KeyboardInterrupt:
            log.info("Monitor stopped by user")
            break
        except Exception as e:
            log.error(f"Monitor crashed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                log.info(f"Restarting in 30s...")
                import time as _time
                _time.sleep(30)
                monitor = NewsletterMonitor(args.config)  # Fresh instance
            else:
                log.error("Max retries exceeded, exiting")
                # Keep process alive for health checks so Railway doesn't redeploy endlessly
                import time as _time
                while True:
                    _time.sleep(60)


if __name__ == "__main__":
    main()
