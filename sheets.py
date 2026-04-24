import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SHEET_NAME = "Tesla Lease Tracker"
TAB_LISTINGS = "listings"
TAB_RUN_LOG = "run_log"

LISTINGS_COLUMNS = [
    "listing_id", "title", "model", "year", "monthly_payment",
    "months_remaining", "km_allowance", "km_used", "msrp", "takeover_cash",
    "location", "url", "score", "score_reason", "scraped_at", "alerted",
]

RUN_LOG_COLUMNS = ["run_at", "listings_found", "new_listings", "alerts_sent", "errors"]

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _get_client() -> gspread.Client:
    raw = os.environ["GOOGLE_CREDENTIALS_JSON"]
    try:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    except json.JSONDecodeError:
        creds = Credentials.from_service_account_file(raw, scopes=_SCOPES)
    return gspread.authorize(creds)


def _ensure_tab(spreadsheet: gspread.Spreadsheet, title: str, headers: list[str]) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        logger.info("Created tab '%s'", title)
    return ws


class SheetsClient:
    def __init__(self):
        client = _get_client()
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self._spreadsheet = client.open_by_key(sheet_id)
        self._listings_ws = _ensure_tab(self._spreadsheet, TAB_LISTINGS, LISTINGS_COLUMNS)
        self._run_log_ws = _ensure_tab(self._spreadsheet, TAB_RUN_LOG, RUN_LOG_COLUMNS)

    def get_existing_ids(self) -> set[str]:
        """Return all listing_ids already in the sheet."""
        try:
            records = self._listings_ws.get_all_records()
            return {str(r["listing_id"]) for r in records if r.get("listing_id")}
        except Exception as e:
            logger.warning("Could not fetch existing IDs: %s", e)
            return set()

    def get_unalerted_ids(self) -> set[str]:
        """Return listing_ids that have NOT been alerted yet."""
        try:
            records = self._listings_ws.get_all_records()
            return {
                str(r["listing_id"])
                for r in records
                if str(r.get("alerted", "")).upper() not in ("TRUE", "1", "YES")
            }
        except Exception as e:
            logger.warning("Could not fetch unalerted IDs: %s", e)
            return set()

    def append_listings(self, listings: list[dict]) -> int:
        """Append new listings (with score fields) to the sheet. Returns count appended."""
        if not listings:
            return 0
        existing_ids = self.get_existing_ids()
        rows = []
        for listing in listings:
            lid = str(listing.get("listing_id", ""))
            if lid in existing_ids:
                logger.debug("Skipping duplicate listing_id=%s", lid)
                continue
            row = [str(listing.get(col, "") or "") for col in LISTINGS_COLUMNS]
            rows.append(row)
            existing_ids.add(lid)  # prevent duplicates within the same batch

        if rows:
            self._listings_ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info("Appended %d new listing(s) to sheet", len(rows))
        return len(rows)

    def mark_alerted(self, listing_id: str) -> None:
        """Set alerted=TRUE for the row matching listing_id."""
        try:
            cell = self._listings_ws.find(listing_id, in_column=1)
            if cell:
                alerted_col = LISTINGS_COLUMNS.index("alerted") + 1
                self._listings_ws.update_cell(cell.row, alerted_col, "TRUE")
        except Exception as e:
            logger.warning("Could not mark listing %s as alerted: %s", listing_id, e)

    def append_run_log(self, listings_found: int, new_listings: int, alerts_sent: int, errors: str) -> None:
        row = [
            datetime.now(timezone.utc).isoformat(),
            listings_found,
            new_listings,
            alerts_sent,
            errors,
        ]
        try:
            self._run_log_ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning("Could not write run log: %s", e)
