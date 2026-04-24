import asyncio
import logging
import os
import random
import re
from datetime import datetime, timezone
from math import ceil
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_SEARCH_URL = (
    "https://www.leasebusters.com/vehicle-search-result"
    "?gallery=1"
    "&categories=SUVs%20/%20Crossovers-7"
    "&makes=Tesla-46"
    "&postalcode=H3G%202A6"
    "&page="
)
BASE_URL = "https://www.leasebusters.com"

# Explicit path used when Playwright's auto-detected browser path doesn't exist.
# Override with CHROMIUM_PATH env var on Railway.
_CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")


def _browser_kwargs() -> dict:
    kwargs: dict = {
        "headless": True,
        "args": [
            "--disable-quic",
            "--disable-features=EncryptedClientHello",
            "--ignore-certificate-errors",
        ],
    }
    if os.path.exists(_CHROMIUM_PATH):
        kwargs["executable_path"] = _CHROMIUM_PATH
    return kwargs


def _extract_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/details/" in href:
            links.add(urljoin(BASE_URL, href))
    return links


def _parse_total_count(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    # Matches "Results X–Y of Z" or "Results X-Y of Z"
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Results\s+\d+[–\-]\d+\s+of\s+(\d+)", text)
    if match:
        return int(match.group(1))
    logger.warning("Could not parse total listing count from search results page")
    return 0


async def discover_listing_urls() -> list[str]:
    """Phase 1: discover all /details/ URLs using Playwright to bypass bot protection."""
    all_links: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**_browser_kwargs())
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Fetch page 1 to get total count
        await page.goto(BASE_SEARCH_URL + "1", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        first_html = await page.content()
        total = _parse_total_count(first_html)
        all_links |= _extract_links(first_html)

        if total == 0:
            logger.warning("No listings found — returning whatever links were on page 1")
            await browser.close()
            return list(all_links)

        total_pages = ceil(total / 10)
        logger.info("Found %d listings across %d pages", total, total_pages)

        for page_num in range(2, total_pages + 1):
            try:
                await page.goto(BASE_SEARCH_URL + str(page_num), wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)
                html = await page.content()
                all_links |= _extract_links(html)
            except Exception as e:
                logger.warning("Failed to fetch search page %d: %s", page_num, e)
            await asyncio.sleep(random.uniform(0.5, 1.5))

        await browser.close()

    logger.info("Discovered %d unique listing URLs", len(all_links))
    return list(all_links)


def _extract_listing_id(url: str) -> str:
    match = re.search(r"/details/([^/?#]+)", url)
    return match.group(1) if match else url.split("/")[-1]


def _parse_float(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return int(cleaned)
    except ValueError:
        return None


def _detect_model(title: str) -> str:
    if "Model X" in title:
        return "Model X"
    if "Model Y" in title:
        return "Model Y"
    return "Unknown"


async def _scrape_listing(page, url: str) -> dict | None:
    """Scrape a single listing detail page using an existing Playwright page object."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        logger.warning("Failed to load %s: %s", url, e)
        return None

    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")

    # Check for expired/taken listings
    page_text = soup.get_text(" ", strip=True).lower()
    if any(phrase in page_text for phrase in ["this listing has been taken", "listing expired", "no longer available"]):
        logger.info("Skipping expired/taken listing: %s", url)
        return None

    listing_id = _extract_listing_id(url)

    # Title — typically an <h1> or prominent heading
    title = None
    for selector in ["h1", ".listing-title", ".vehicle-title"]:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            break
    if not title:
        title = "Unknown"

    year_match = re.search(r"\b(20\d{2})\b", title)
    year = int(year_match.group(1)) if year_match else None
    model = _detect_model(title)

    def find_field_value(labels: list[str]) -> str | None:
        for label in labels:
            # Search in dt/dd pairs
            for dt in soup.find_all("dt"):
                if label.lower() in dt.get_text(strip=True).lower():
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        return dd.get_text(strip=True)
            # Search in label/value row patterns
            for row in soup.find_all(["tr", "li", "div"]):
                row_text = row.get_text(" ", strip=True)
                if label.lower() in row_text.lower():
                    # grab numbers from the same element
                    nums = re.findall(r"[\$]?\s*[\d,]+(?:\.\d+)?", row_text)
                    if nums:
                        return nums[0]
        return None

    monthly_payment = _parse_float(find_field_value(["monthly payment", "monthly", "payment/month", "$/month"]))
    months_remaining = _parse_int(find_field_value(["months remaining", "months left", "remaining months"]))
    km_allowance = _parse_int(find_field_value(["km allowance", "km/year", "annual km", "km per year", "kilometres/year"]))
    km_used = _parse_int(find_field_value(["km used", "kilometres used", "km driven", "odometer"]))
    msrp = _parse_float(find_field_value(["msrp", "original msrp", "original price"]))
    takeover_cash = _parse_float(find_field_value(["takeover cash", "cash incentive", "incentive cash", "cash bonus"])) or 0.0

    # Location
    location = None
    for selector in [".listing-location", ".location", "[class*='location']"]:
        el = soup.select_one(selector)
        if el:
            location = el.get_text(strip=True)
            break
    if not location:
        loc_match = re.search(r"Location[:\s]+([A-Za-z\s,]+)", soup.get_text(" ", strip=True))
        location = loc_match.group(1).strip() if loc_match else None

    return {
        "listing_id": listing_id,
        "title": title,
        "model": model,
        "year": year,
        "monthly_payment": monthly_payment,
        "months_remaining": months_remaining,
        "km_allowance": km_allowance,
        "km_used": km_used,
        "msrp": msrp,
        "takeover_cash": takeover_cash,
        "location": location,
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


async def scrape_listing_details(urls: list[str], existing_ids: set[str]) -> list[dict]:
    """Phase 2: scrape detail pages for URLs not already in the sheet."""
    new_urls = [u for u in urls if _extract_listing_id(u) not in existing_ids]
    logger.info("%d URLs to scrape (skipping %d already in sheet)", len(new_urls), len(urls) - len(new_urls))

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**_browser_kwargs())
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for url in new_urls:
            listing = await _scrape_listing(page, url)
            if listing:
                results.append(listing)
            delay = random.uniform(1.0, 2.0)
            await asyncio.sleep(delay)

        await browser.close()

    logger.info("Scraped %d valid listings", len(results))
    return results
