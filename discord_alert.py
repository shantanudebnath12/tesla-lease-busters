import logging
import os

import httpx

logger = logging.getLogger(__name__)

ALERT_SCORE_THRESHOLD = 7.5


def _build_message(listing: dict) -> str:
    score = listing.get("score", "?")
    title = listing.get("title", "Unknown")
    effective = listing.get("effective_payment")
    with_tax = listing.get("monthly_payment_with_tax")
    base = listing.get("monthly_payment")
    months = listing.get("months_remaining")
    location = listing.get("location", "Unknown")
    cash = listing.get("takeover_cash") or 0
    url = listing.get("url", "")
    reason = listing.get("score_reason", "")

    # Primary: effective payment (incentive baked in); secondary: with-tax; tertiary: base
    if effective:
        payment_line = f"**${effective:,.0f}/mo effective**"
        if with_tax:
            payment_line += f" · ${with_tax:,.0f}/mo with taxes"
        elif base:
            payment_line += f" · ${base:,.0f}/mo before taxes"
    elif with_tax:
        payment_line = f"**${with_tax:,.0f}/mo** (incl. taxes)"
    elif base:
        payment_line = f"${base:,.0f}/mo (before taxes)"
    else:
        payment_line = "Payment unknown"

    months_str = f"{months} months remaining" if months else "Unknown term"
    cash_line = f"\n\U0001f381 ${cash:,.0f} cash incentive" if cash else ""

    return (
        f"\U0001f697 **New Tesla Lease Deal** — Score: {score}/10\n\n"
        f"**{title}**\n"
        f"\U0001f4b0 {payment_line} | {months_str}\n"
        f"\U0001f4cd {location}"
        f"{cash_line}\n"
        f"\U0001f517 {url}\n\n"
        f"Reason: {reason}"
    )


def send_alert(listing: dict) -> bool:
    """Send a Discord webhook alert. Returns True on success."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL is not set — skipping alert")
        return False

    message = _build_message(listing)
    try:
        resp = httpx.post(
            webhook_url,
            json={"content": message},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Alert sent for listing_id=%s", listing.get("listing_id"))
        return True
    except Exception as e:
        logger.error("Discord alert failed for listing_id=%s: %s", listing.get("listing_id"), e)
        return False


def should_alert(listing: dict) -> bool:
    score = listing.get("score")
    if score is None:
        return False
    return float(score) >= ALERT_SCORE_THRESHOLD
