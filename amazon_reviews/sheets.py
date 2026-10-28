"""Optional: append new reviews to a Google Sheet (off by default).

Needs `pip install gspread` and a Google service-account JSON key whose
client_email has edit access to the sheet.
"""

from __future__ import annotations

import logging

from .config import GoogleSheetsSettings
from .parser import COLUMNS, Review

log = logging.getLogger("amazon_reviews.sheets")


def push_new_reviews(reviews: list[Review], cfg: GoogleSheetsSettings) -> int:
    if not reviews:
        return 0
    try:
        import gspread  # optional dependency
    except ImportError as e:
        raise RuntimeError("Google Sheets push needs: pip install gspread") from e
    if not cfg.spreadsheet_id:
        raise RuntimeError("google_sheets.spreadsheet_id is empty in config.yaml")

    client = gspread.service_account(filename=cfg.service_account_file)
    book = client.open_by_key(cfg.spreadsheet_id)
    try:
        sheet = book.worksheet(cfg.worksheet)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(cfg.worksheet, rows=1000, cols=len(COLUMNS))
    if not sheet.row_values(1):
        sheet.append_row(COLUMNS)
    rows = []
    for r in reviews:
        d = r.to_dict()
        rows.append(["" if d[c] is None else d[c] for c in COLUMNS])
    sheet.append_rows(rows, value_input_option="RAW")
    log.info("event=sheets_push rows=%d worksheet=%s", len(rows), cfg.worksheet)
    return len(rows)
