"""
Migrate existing Google Sheets to the new schema.

Changes applied:
  invoices            — append column: document_id (col AC, was 28 cols → now 29? Let me check)
  settlement_reports  — rename column source_file → document_id  (same position)
  payments            — append columns: match_status, counterparty
  documents           — create new worksheet with full header row

Safe to re-run: existing data is not modified, only header/structure changes.

Usage:
    python scripts/migrate_sheets.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import argparse

import gspread
from google.oauth2.service_account import Credentials

from passierschein.adapters.sheets import SHEET_HEADERS, SCOPES
from passierschein.config import config


def get_spreadsheet() -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_file(str(config.GOOGLE_CREDENTIALS_FILE), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(config.SPREADSHEET_ID)


def _get_header_row(ws: gspread.Worksheet) -> list[str]:
    return ws.row_values(1)


def _ensure_columns(ws: gspread.Worksheet, expected: list[str], dry_run: bool) -> None:
    """Append any missing header columns to the right of the sheet."""
    current = _get_header_row(ws)
    missing  = [h for h in expected if h not in current]
    if not missing:
        print(f"  [{ws.title}] headers already up-to-date.")
        return
    new_headers = current + missing
    if dry_run:
        print(f"  [{ws.title}] DRY-RUN: would add columns {missing}")
        return
    ws.update("A1", [new_headers], value_input_option="USER_ENTERED")
    print(f"  [{ws.title}] Added columns: {missing}")


def _rename_column(ws: gspread.Worksheet, old_name: str, new_name: str, dry_run: bool) -> None:
    current = _get_header_row(ws)
    if new_name in current:
        print(f"  [{ws.title}] Column '{new_name}' already exists.")
        return
    if old_name not in current:
        print(f"  [{ws.title}] Column '{old_name}' not found — skipping rename.")
        return
    idx = current.index(old_name)
    current[idx] = new_name
    if dry_run:
        print(f"  [{ws.title}] DRY-RUN: would rename '{old_name}' → '{new_name}' at col {idx + 1}")
        return
    ws.update("A1", [current], value_input_option="USER_ENTERED")
    print(f"  [{ws.title}] Renamed '{old_name}' → '{new_name}'")


def _ensure_worksheet(ss: gspread.Spreadsheet, name: str, headers: list[str], dry_run: bool) -> None:
    existing_titles = [ws.title for ws in ss.worksheets()]
    if name in existing_titles:
        ws = ss.worksheet(name)
        _ensure_columns(ws, headers, dry_run)
        return
    if dry_run:
        print(f"  DRY-RUN: would create worksheet '{name}' with headers {headers}")
        return
    ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers) + 5)
    ws.update("A1", [headers], value_input_option="USER_ENTERED")
    print(f"  Created worksheet '{name}' with {len(headers)} columns.")


def main(dry_run: bool = False) -> None:
    print("Connecting to Google Sheets…")
    ss = get_spreadsheet()
    print(f"Spreadsheet: {ss.title}\n")

    # settlement_reports: rename source_file → document_id
    print("settlement_reports:")
    ws_sr = ss.worksheet("settlement_reports")
    _rename_column(ws_sr, "source_file", "document_id", dry_run)
    _ensure_columns(ws_sr, SHEET_HEADERS["settlement_reports"], dry_run)

    # invoices: add document_id column
    print("invoices:")
    ws_inv = ss.worksheet("invoices")
    _ensure_columns(ws_inv, SHEET_HEADERS["invoices"], dry_run)

    # payments: add match_status, counterparty
    print("payments:")
    ws_pay = ss.worksheet("payments")
    _ensure_columns(ws_pay, SHEET_HEADERS["payments"], dry_run)

    # documents: create if missing
    print("documents:")
    _ensure_worksheet(ss, "documents", SHEET_HEADERS["documents"], dry_run)

    print("\nDone." if not dry_run else "\nDry run complete — no changes made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Google Sheets schema")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
