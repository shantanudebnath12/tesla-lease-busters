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

# Inline stealth patches — replaces playwright-stealth to avoid pkg_resources dependency
_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params);
}
"""


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


async def _wait_past_cloudflare(page, timeout: int = 45000) -> None:
    """Wait until Cloudflare's interstitial is gone and real content is loaded."""
    try:
        await page.wait_for_function(
            "() => !document.title.includes('Just a moment')",
            timeout=timeout,
        )
        # After the title changes the post-challenge redirect/reload is still in flight;
        # give it a moment before we read content.
        await asyncio.sleep(3)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass


async def discover_listing_urls() -> list[str]:
    """Phase 1: discover all /details/ URLs using Playwright to bypass bot protection."""
    all_links: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**_browser_kwargs())
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        # Fetch page 1 to get total count
        await page.goto(BASE_SEARCH_URL + "1", wait_until="domcontentloaded", timeout=60000)
        await _wait_past_cloudflare(page)
        first_html = await page.content()
        total = _parse_total_count(first_html)
        all_links |= _extract_links(first_html)

        if total == 0:
            soup = BeautifulSoup(first_html, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            logger.warning("Page title: %s", soup.title.string if soup.title else "none")
            logger.warning("Page text (first 500 chars): %s", page_text[:500])
            logger.warning("No listings found — returning whatever links were on page 1")
            await browser.close()
            return list(all_links)

        total_pages = ceil(total / 10)
        logger.info("Found %d listings across %d pages", total, total_pages)

        for page_num in range(2, total_pages + 1):
            try:
                await page.goto(BASE_SEARCH_URL + str(page_num), wait_until="domcontentloaded", timeout=60000)
                await _wait_past_cloudflare(page)
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


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _months_until(year_str: str, month_str: str, day_str: str) -> int | None:
    month_num = _MONTH_NAMES.get(month_str[:3].lower())
    if not month_num:
        return None
    try:
        expiry = datetime(int(year_str), month_num, int(day_str), tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        months = (expiry.year - now.year) * 12 + (expiry.month - now.month)
        return max(0, months)
    except ValueError:
        return None


async def _scrape_listing(page, url: str) -> dict | None:
    """Scrape a single listing detail page using an existing Playwright page object."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await _wait_past_cloudflare(page)
    except Exception as e:
        logger.warning("Failed to load %s: %s", url, e)
        return None

    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    # Skip expired/taken listings
    if any(p in page_text.lower() for p in ["this listing has been taken", "listing expired", "no longer available"]):
        logger.info("Skipping expired/taken listing: %s", url)
        return None

    listing_id = _extract_listing_id(url)

    # Title: try heading elements first, fall back to URL slug
    # (leasebusters uses h2 or custom classes, not h1)
    title = None
    for selector in ["h1", "h2", ".listing-title", ".vehicle-title"]:
        el = soup.select_one(selector)
        if el:
            t = el.get_text(strip=True)
            if len(t) > 5:
                title = t
                break
    if not title:
        # /details/568628/2024-Tesla-Model Y → "2024 Tesla Model Y"
        slug = url.split("/details/")[-1]
        parts = slug.split("/", 1)
        if len(parts) > 1:
            title = parts[1].replace("-", " ").replace("+", " ").replace("%20", " ").strip()
    if not title:
        title = "Unknown"

    year_match = re.search(r"\b(20\d{2})\b", title)
    year = int(year_match.group(1)) if year_match else None
    model = _detect_model(title)

    def rx(pattern: str) -> re.Match | None:
        return re.search(pattern, page_text, re.IGNORECASE)

    # Monthly payment before taxes (Leasebusters shows both before and after)
    m = rx(r"Monthly\s+Payment\s*\(before\s+taxes\)\s*\$?\s*([\d,]+(?:\.\d{2})?)")
    if not m:
        m = rx(r"Monthly\s+Payment[^$\d\n]{0,30}\$?\s*([\d,]+(?:\.\d{2})?)")
    monthly_payment = _parse_float(m.group(1)) if m else None

    # Months remaining: calculated from "Lease Expiry Date 2027-May-07"
    months_remaining = None
    m = rx(r"Lease\s+Expiry\s+Date\s+(\d{4})-(\w{3,9})-(\d{1,2})")
    if m:
        months_remaining = _months_until(m.group(1), m.group(2), m.group(3))

    # km allowance: "Total km Allowance 60,000" annualized using lease term
    km_allowance = None
    m = rx(r"Total\s+km\s+Allowance\s+([\d,]+)")
    if m:
        total_km = _parse_int(m.group(1))
        tm = rx(r"Original\s+Lease\s+Term\s+(\d+)\s+Months")
        lease_term = _parse_int(tm.group(1)) if tm else None
        if total_km and lease_term and lease_term > 0:
            km_allowance = round(total_km / lease_term * 12)
        else:
            km_allowance = total_km

    # km used: "Odometer (kms) 82,000"
    m = rx(r"Odometer\s*\(kms?\)\s*([\d,]+)")
    km_used = _parse_int(m.group(1)) if m else None

    # MSRP (not always present)
    m = rx(r"(?:MSRP|Original\s+MSRP|Original\s+Price)\s*\$?\s*([\d,]+(?:\.\d{2})?)")
    msrp = _parse_float(m.group(1)) if m else None

    # Takeover cash / incentive
    m = rx(r"(?:Takeover\s+Cash|Cash\s+Incentive|Incentive\s+Cash|Cash\s+Bonus)\s*\$?\s*([\d,]+(?:\.\d{2})?)")
    takeover_cash = _parse_float(m.group(1)) if m else 0.0

    # Location: "Vehicle Location: Whitby, ON"
    m = rx(r"Vehicle\s+Location:\s*([A-Za-z][^$\n]{2,40}?)(?=\s+(?:Effective|Listing|Seller|Ask|Year|Odometer))")
    if not m:
        m = rx(r"Location[:\s]+([A-Za-z][A-Za-z\s,]{1,30})")
    location = m.group(1).strip() if m else None

    logger.info(
        "Parsed %s | payment=%s | months=%s | km_allow=%s | km_used=%s | location=%s",
        listing_id, monthly_payment, months_remaining, km_allowance, km_used, location,
    )

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
        await context.add_init_script(_STEALTH_JS)
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
