"""
format_sheets.py — Apply visual formatting to all Google Sheets tabs.

Per tab:
  - Freeze row 1, bold + light-blue header background
  - Coloured tab
  - Column widths
  - Currency number format on amount columns
  - Conditional formatting on every status column

Idempotent: existing conditional-format rules are cleared before adding new
ones, so re-running is safe.

    python scripts/format_sheets.py
    # or:
    python scripts/cli.py format-sheet
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from rich.console import Console
from rich.table import Table

from passierschein.adapters.sheets import SCOPES, SHEET_HEADERS
from passierschein.config import config

console = Console()


# ---------------------------------------------------------------------------
# Primitive builders
# ---------------------------------------------------------------------------

def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {
        "red":   int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue":  int(h[4:6], 16) / 255,
    }


def _freeze(sheet_id: int) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def _tab_color(sheet_id: int, hex_color: str) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "tabColorStyle": {"rgbColor": _rgb(hex_color)},
            },
            "fields": "tabColorStyle",
        }
    }


def _header_row(sheet_id: int, num_cols: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": _rgb("#CFE2F3"),
                    "textFormat": {"bold": True},
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }
    }


def _col_width(sheet_id: int, col_idx: int, pixels: int) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_idx,
                "endIndex": col_idx + 1,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _currency_col(sheet_id: int, col_idx: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col_idx,
                "endColumnIndex": col_idx + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "numberFormat": {
                        "type": "NUMBER",
                        "pattern": '#,##0.00\\ "€"',
                    }
                }
            },
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _cf(sheet_id: int, col_idx: int, text: str, bg_hex: str) -> dict:
    """Conditional format rule: TEXT_EQ → solid background colour."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": text}],
                    },
                    "format": {"backgroundColor": _rgb(bg_hex)},
                },
            },
            "index": 0,
        }
    }


def _clear_cf(sheet_id: int, count: int) -> list[dict]:
    """Delete all existing conditional format rules (highest index first)."""
    return [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
        for i in range(count - 1, -1, -1)
    ]


# ---------------------------------------------------------------------------
# Status colour constants
# ---------------------------------------------------------------------------
#  Green  — terminal good state:  settled, paid, matched, received, covered, processed
#  Yellow — needs action:         open, unmatched, unprocessed, pending
#  Blue   — in flight:            claimed
#  Orange — partial / mismatch:   partially_settled, items_unmatched
#  Grey   — not applicable / dismissed: not_applicable, out_of_scope
#  Red    — bad outcome:          rejected

G = "#C6EFCE"
Y = "#FFEB9C"
B = "#9FC5E8"
O = "#FFCC99"
N = "#D9D9D9"
R = "#FFC7CE"


# ---------------------------------------------------------------------------
# Per-sheet formatters
# ---------------------------------------------------------------------------

def _fmt_invoices(sid: int) -> list[dict]:
    h = SHEET_HEADERS["invoices"]
    reqs: list[dict] = [_freeze(sid), _tab_color(sid, "#4472C4"), _header_row(sid, len(h))]

    widths = {
        "id": 80, "person_id": 80, "invoice_type": 110, "split_type": 105,
        "billing_variant": 110, "provider": 180,
        "date_of_service": 110, "date_received": 110, "due_date": 100,
        "total_amount": 100, "employee_net_expected": 145,
        "payment_status": 110, "date_paid": 100, "payment_id": 80,
        "total_reimbursed": 125, "net_cost": 100,
        "pkv_share_pct": 80, "pkv_expected": 105, "pkv_submitted_at": 120,
        "pkv_reimbursed": 110, "pkv_claim_status": 125, "pkv_payment_status": 135,
        "beihilfe_share_pct": 95, "beihilfe_expected": 115,
        "beihilfe_submitted_at": 140, "beihilfe_reimbursed": 125,
        "beihilfe_claim_status": 140, "beihilfe_payment_status": 150,
        "document_id": 80,
    }
    for field, px in widths.items():
        if field in h:
            reqs.append(_col_width(sid, h.index(field), px))

    for field in ("total_amount", "employee_net_expected", "pkv_expected",
                  "pkv_reimbursed", "beihilfe_expected", "beihilfe_reimbursed",
                  "total_reimbursed", "net_cost"):
        if field in h:
            reqs.append(_currency_col(sid, h.index(field)))

    # payment_status
    c = h.index("payment_status")
    reqs += [_cf(sid, c, "paid", G), _cf(sid, c, "open", Y)]

    # pkv_claim_status + beihilfe_claim_status
    for field in ("pkv_claim_status", "beihilfe_claim_status"):
        c = h.index(field)
        reqs += [
            _cf(sid, c, "settled",           G),
            _cf(sid, c, "partially_settled",  O),
            _cf(sid, c, "claimed",            B),
            _cf(sid, c, "open",               Y),
            _cf(sid, c, "not_applicable",     N),
        ]

    # pkv_payment_status + beihilfe_payment_status
    for field in ("pkv_payment_status", "beihilfe_payment_status"):
        c = h.index(field)
        reqs += [_cf(sid, c, "received", G), _cf(sid, c, "open", Y)]

    return reqs


def _fmt_payments(sid: int) -> list[dict]:
    h = SHEET_HEADERS["payments"]
    reqs: list[dict] = [_freeze(sid), _tab_color(sid, "#4BACC6"), _header_row(sid, len(h))]

    widths = {
        "id": 80, "direction": 85, "date": 100, "amount": 105,
        "settlement_report_id": 80, "discrepancy": 100,
        "bank_reference": 260, "match_status": 110,
        "counterparty": 180, "import_fingerprint": 125,
    }
    for field, px in widths.items():
        if field in h:
            reqs.append(_col_width(sid, h.index(field), px))

    for field in ("amount", "discrepancy"):
        if field in h:
            reqs.append(_currency_col(sid, h.index(field)))

    c = h.index("direction")
    reqs += [_cf(sid, c, "inbound", G), _cf(sid, c, "outbound", Y)]

    c = h.index("match_status")
    reqs += [
        _cf(sid, c, "matched",      G),
        _cf(sid, c, "unmatched",    Y),
        _cf(sid, c, "out_of_scope", N),
    ]

    return reqs


def _fmt_settlement_reports(sid: int) -> list[dict]:
    h = SHEET_HEADERS["settlement_reports"]
    reqs: list[dict] = [_freeze(sid), _tab_color(sid, "#548235"), _header_row(sid, len(h))]

    widths = {
        "id": 80, "type": 75, "report_reference": 160, "received_at": 110,
        "total_reimbursed": 135, "document_id": 80,
        "line_items_status": 145, "payment_status": 110,
    }
    for field, px in widths.items():
        if field in h:
            reqs.append(_col_width(sid, h.index(field), px))

    if "total_reimbursed" in h:
        reqs.append(_currency_col(sid, h.index("total_reimbursed")))

    c = h.index("payment_status")
    reqs += [_cf(sid, c, "paid", G), _cf(sid, c, "open", Y)]

    c = h.index("line_items_status")
    reqs += [
        _cf(sid, c, "fully_matched",   G),
        _cf(sid, c, "items_unmatched", O),
        _cf(sid, c, "unprocessed",     Y),
    ]

    return reqs


def _fmt_line_items(sid: int) -> list[dict]:
    h = SHEET_HEADERS["settlement_line_items"]
    reqs: list[dict] = [_freeze(sid), _tab_color(sid, "#70AD47"), _header_row(sid, len(h))]

    for field in ("billed_amount", "eligible_amount", "reimbursed_amount",
                  "not_covered_amount", "rate_cap_reduction", "deductible_applied"):
        if field in h:
            reqs.append(_currency_col(sid, h.index(field)))

    c = h.index("status")
    reqs += [
        _cf(sid, c, "covered",  G),
        _cf(sid, c, "partial",  O),
        _cf(sid, c, "rejected", R),
    ]

    return reqs


def _fmt_documents(sid: int) -> list[dict]:
    h = SHEET_HEADERS["documents"]
    reqs: list[dict] = [_freeze(sid), _tab_color(sid, "#7030A0"), _header_row(sid, len(h))]

    widths = {
        "id": 80, "file_path": 260, "document_type": 125,
        "captured_at": 110, "status": 100, "linked_entity_id": 80,
    }
    for field, px in widths.items():
        if field in h:
            reqs.append(_col_width(sid, h.index(field), px))

    c = h.index("status")
    reqs += [_cf(sid, c, "processed", G), _cf(sid, c, "pending", Y)]

    return reqs


def _fmt_reference(sid: int, tab: str) -> list[dict]:
    h = SHEET_HEADERS[tab]
    return [_freeze(sid), _tab_color(sid, "#808080"), _header_row(sid, len(h))]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FORMATTERS = {
    "invoices":              _fmt_invoices,
    "payments":              _fmt_payments,
    "settlement_reports":    _fmt_settlement_reports,
    "settlement_line_items": _fmt_line_items,
    "documents":             _fmt_documents,
    "persons":               lambda sid: _fmt_reference(sid, "persons"),
    "split_matrix":          lambda sid: _fmt_reference(sid, "split_matrix"),
    "beihilfe_deductible":   lambda sid: _fmt_reference(sid, "beihilfe_deductible"),
}


def run() -> None:
    creds = Credentials.from_service_account_file(
        str(config.GOOGLE_CREDENTIALS_FILE), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(config.SPREADSHEET_ID)

    # Fetch sheet metadata to get sheetId + existing CF rule counts
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = service.spreadsheets().get(
        spreadsheetId=config.SPREADSHEET_ID,
        fields="sheets(properties(sheetId,title),conditionalFormats)",
    ).execute()

    ws_meta: dict[str, dict] = {}
    for s in meta.get("sheets", []):
        props = s["properties"]
        ws_meta[props["title"]] = {
            "sheetId":  props["sheetId"],
            "cf_count": len(s.get("conditionalFormats", [])),
        }

    all_requests: list[dict] = []
    results: list[tuple[str, str]] = []

    for tab, fmt_fn in FORMATTERS.items():
        if tab not in ws_meta:
            results.append((tab, "[yellow]not found — skipped[/yellow]"))
            continue
        sid      = ws_meta[tab]["sheetId"]
        cf_count = ws_meta[tab]["cf_count"]
        all_requests.extend(_clear_cf(sid, cf_count))
        all_requests.extend(fmt_fn(sid))
        results.append((tab, "[green]ok[/green]"))

    ss.batch_update({"requests": all_requests})

    t = Table(title="Sheet formatting", show_lines=True)
    t.add_column("Tab"); t.add_column("Status")
    for tab, status in results:
        t.add_row(tab, status)
    console.print(t)


if __name__ == "__main__":
    run()
