# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the tracker

```bash
# Install dependencies (run once)
pip install -r requirements.txt
playwright install chromium

# Run a full scrape cycle
python main.py
```

Requires a `.env` file (or environment variables) with:
```
GOOGLE_CREDENTIALS_JSON=./credentials.json
GOOGLE_SHEET_ID=<sheet id from the URL>
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

## Architecture

The pipeline runs top-to-bottom in `main.py` as a single async `run()` call:

1. **`scraper.discover_listing_urls()`** — async httpx fetches page 1, parses total count, then `asyncio.gather`s all remaining pages in parallel. Returns deduplicated `list[str]` of `/details/` URLs.

2. **`sheets.SheetsClient.get_existing_ids()`** — fetches all `listing_id` values already in the sheet so the next step can skip them.

3. **`scraper.scrape_listing_details(urls, existing_ids)`** — single shared Playwright browser visits each new URL sequentially with 1-2s random delays. Parses fields using a `find_field_value()` helper that tries `<dt>/<dd>` pairs first, then falls back to scanning any `<tr>/<li>/<div>` that contains the label text.

4. **`scorer.score_listing(listing)`** — pure function, no I/O. Returns `ScoreResult(score, score_reason)`. Weights: payment 35%, takeover cash 20%, months remaining 20%, km allowance 15%, km used ratio 10%.

5. **`sheets.append_listings()`** — append-only; re-checks existing IDs to prevent duplicates even within a single batch.

6. **`discord_alert.send_alert()`** — fires for listings where `score >= 7.5` and `alerted != TRUE`. `mark_alerted()` writes `TRUE` back to the sheet immediately after a successful send.

7. **`sheets.append_run_log()`** — always writes a summary row, even when earlier steps fail.

## Key invariants

- The `listings` tab is **append-only** — never update or delete rows.
- Deduplication happens in two places: `existing_ids` check before scraping, and again inside `append_listings()` before writing. Both are needed.
- `listing_id` is the URL slug extracted from `/details/<slug>` — it's the primary key throughout.
- Missing fields are `None`, never absent — callers must handle `None` for every numeric field.
- Scorer benchmarks (payment floor/ceiling, km thresholds, etc.) are module-level constants at the top of `scorer.py` — adjust them there.
- `BASE_SEARCH_URL` in `scraper.py` is the single config point for changing the vehicle filter (make, category, postal code).

## Deployment

Railway runs `python main.py` on cron `0 */3 * * *`. No long-running process — each run is a short-lived script. Logs go to stdout (Railway captures them). The `credentials.json` path and other secrets are set as Railway environment variables.
