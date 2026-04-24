import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def run() -> None:
    from scraper import discover_listing_urls, scrape_listing_details
    from scorer import score_listing
    from sheets import SheetsClient
    from discord_alert import send_alert, should_alert

    errors: list[str] = []
    sheets = SheetsClient()

    # Step 1 — discover listing URLs
    try:
        urls = await discover_listing_urls()
    except Exception as e:
        logger.error("Link discovery failed: %s", e)
        sheets.append_run_log(0, 0, 0, str(e))
        sys.exit(1)

    listings_found = len(urls)
    logger.info("Discovered %d listing URLs", listings_found)

    # Step 2 — check what's already in the sheet
    try:
        existing_ids = sheets.get_existing_ids()
    except Exception as e:
        logger.error("Could not read existing IDs from sheet: %s", e)
        sheets.append_run_log(listings_found, 0, 0, str(e))
        sys.exit(1)

    # Step 3 — scrape detail pages for new listings only
    try:
        raw_listings = await scrape_listing_details(urls, existing_ids)
    except Exception as e:
        logger.error("Detail scraping failed: %s", e)
        errors.append(str(e))
        raw_listings = []

    # Step 4 — score each listing
    scored_listings = []
    for listing in raw_listings:
        try:
            result = score_listing(listing)
            listing["score"] = result.score
            listing["score_reason"] = result.score_reason
            listing["alerted"] = "FALSE"
            scored_listings.append(listing)
        except Exception as e:
            logger.warning("Scoring failed for %s: %s", listing.get("listing_id"), e)
            errors.append(str(e))

    # Step 5 — append new listings to Sheets
    try:
        new_count = sheets.append_listings(scored_listings)
    except Exception as e:
        logger.error("Failed to write listings to sheet: %s", e)
        errors.append(str(e))
        new_count = 0

    # Step 6 — send Discord alerts for high-score listings
    alerts_sent = 0
    unalerted_ids = sheets.get_unalerted_ids()
    for listing in scored_listings:
        lid = str(listing.get("listing_id", ""))
        if lid not in unalerted_ids:
            continue
        if not should_alert(listing):
            continue
        success = send_alert(listing)
        if success:
            sheets.mark_alerted(lid)
            alerts_sent += 1

    # Step 7 — write run log
    sheets.append_run_log(
        listings_found=listings_found,
        new_listings=new_count,
        alerts_sent=alerts_sent,
        errors="; ".join(errors) if errors else "",
    )

    logger.info(
        "Run complete — found=%d new=%d alerts=%d errors=%d",
        listings_found, new_count, alerts_sent, len(errors),
    )


if __name__ == "__main__":
    asyncio.run(run())
