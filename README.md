# Court Filing Monitor for Public Companies

Two monitors running in one process:
1. **Federal Court Filings** — Scans all major federal courts for new filings involving public companies
2. **Supreme Court Monitor** — Checks SCOTUS opinions, orders & docket entries, alerts on any public company involvement

## Features
- 400+ public companies tracked (S&P 500 + notable mid-caps)
- Smart word-boundary matching (no false positives like "Stanford" → "Ford")
- DeepSeek AI summaries of every filing
- Market hours only: 9 AM – 4 PM EST, weekdays, skips holidays
- Rotating company-specific searches for comprehensive coverage
- Deduplication with 7-day rolling state file
- Built-in rate limiter tracking (stays under 5,000 req/hr)

## Speed / Rate Limits
- **10-second intervals** (fastest safe setting) — staggered across 6 check types
- Each individual source checked every ~60 seconds
- Uses ~468 req/hr of 5,000 allowed (91% safety margin)
- Built-in 429 retry with exponential backoff

## Discord Webhooks
- **Federal filings** → orange embeds with ticker, court, judge, AI summary
- **SCOTUS filings** → red embeds (public company) / blue embeds (general)

## Railway Deployment

1. Create a new Railway project
2. Connect your GitHub repo (or upload files directly)
3. Set these environment variables in Railway:

```
DISCORD_WEBHOOK_FEDERAL=https://discordapp.com/api/webhooks/...
DISCORD_WEBHOOK_SCOTUS=https://discordapp.com/api/webhooks/...
DEEPSEEK_API_KEY=sk-...
COURTLISTENER_API_TOKEN=your-token    # REQUIRED for fast polling
CHECK_INTERVAL=10                      # seconds (10 = fastest safe)
```

4. Deploy — the Procfile tells Railway to run `python court_monitor.py`

## CourtListener API Token (REQUIRED)
Free accounts get **5,000 requests/hour**. Get a free token at:
https://www.courtlistener.com/sign-in/

Without a token you'll hit very low anonymous limits almost immediately.

## Data Sources
- CourtListener REST API v4 (RECAP docket search + opinions search)
- supremecourt.gov slip opinions page (scraped)
- DeepSeek API for AI summarization
