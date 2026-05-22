"""
Google Sheets adapter.

Each domain entity maps to one worksheet tab.
Row 1 is always the header row (written by setup_sheets.py).
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from enum import Enum as _Enum
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

import gspread
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

from ..config import config
from ..domain.models import (
    BeihilfeDeductible,
    Document,
    Invoice,
    Payment,
    Person,
    SettlementLineItem,
    SettlementReport,
)

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# Sheet → header column definitions
# ---------------------------------------------------------------------------

SHEET_HEADERS: Dict[str, List[str]] = {
    "persons": [
        "person_id", "first_name", "family_name", "birth_date", "role",
    ],
    "invoices": [
        # general
        "id", "person_id", "invoice_type", "split_type", "billing_variant",
        "provider", "date_of_service", "date_received", "due_date",
        "total_amount", "employee_net_expected",
        "payment_status", "date_paid", "payment_id", "total_reimbursed", "net_cost",
        # pkv
        "pkv_share_pct", "pkv_expected", "pkv_submitted_at",
        "pkv_reimbursed", "pkv_claim_status", "pkv_payment_status",
        # beihilfe
        "beihilfe_share_pct", "beihilfe_expected", "beihilfe_submitted_at",
        "beihilfe_reimbursed", "beihilfe_claim_status", "beihilfe_payment_status",
        # document link
        "document_id",
    ],
    "settlement_reports": [
        "id", "type", "report_reference", "received_at",
        "total_reimbursed", "document_id", "line_items_status", "payment_status",
    ],
    "settlement_line_items": [
        "id", "settlement_report_id", "invoice_id", "type",
        "billed_amount", "eligible_amount", "reimbursed_amount",
        "not_covered_amount", "rate_cap_reduction", "deductible_applied",
        "rejection_reasons", "status",
    ],
    "payments": [
        "id", "direction", "date", "amount",
        "settlement_report_id", "discrepancy", "bank_reference",
        "match_status", "counterparty",
    ],
    "beihilfe_deductible": [
        "year", "configured_amount", "consumed_ytd", "remaining",
    ],
    "split_matrix": [
        "role", "split_type", "pkv_pct", "beihilfe_pct",
    ],
    "documents": [
        "id", "file_path", "document_type", "captured_at", "status", "linked_entity_id",
    ],
}

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col_letter(n: int) -> str:
    """Convert a 1-based column number to an A1-notation letter (A, B, … Z, AA, AB, …)."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, _Enum):
        return str(v.value)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def _model_to_row(model: BaseModel, headers: List[str]) -> List[str]:
    data = model.model_dump()
    return [_to_str(data.get(h)) for h in headers]


# Fields whose values Google Sheets may store as date serial numbers (int)
_DATE_FIELDS = {
    "birth_date", "date_of_service", "date_received", "due_date", "date_paid",
    "pkv_submitted_at", "beihilfe_submitted_at",
    "submitted_at", "received_at", "reimbursed_at", "date",
}


def _fix_date_serials(record: dict) -> dict:
    """
    Google Sheets stores dates as integer serial numbers when read with
    UNFORMATTED_VALUE (days since 1899-12-30). Convert them back to ISO strings.
    """
    from datetime import timedelta
    result = {}
    for k, v in record.items():
        if k in _DATE_FIELDS and isinstance(v, int):
            result[k] = (date(1899, 12, 30) + timedelta(days=v)).isoformat()
        else:
            result[k] = v
    return result


def _fix_enum_reprs(record: dict) -> dict:
    """
    Normalise enum values that were incorrectly written as 'ClassName.VALUE'
    (e.g. 'PaymentStatus.OPEN') back to the raw enum value string (e.g. 'open').
    This handles rows written before the _to_str() Enum fix was applied.
    """
    result = {}
    for k, v in record.items():
        if isinstance(v, str) and "." in v:
            parts = v.split(".", 1)
            # Heuristic: class name starts with an uppercase letter
            if parts[0] and parts[0][0].isupper():
                result[k] = parts[1].lower()
                continue
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Generic sheet table
# ---------------------------------------------------------------------------

class SheetTable(Generic[T]):
    def __init__(self, ws: gspread.Worksheet, model_cls: Type[T], headers: List[str]) -> None:
        self._ws       = ws
        self._cls      = model_cls
        self._headers  = headers

    def _id_col(self) -> int:
        """1-based column index for the 'id' or first PK field."""
        for i, h in enumerate(self._headers, 1):
            if h in ("id", "person_id", "year"):
                return i
        return 1

    def _find_row(self, key: str) -> Optional[int]:
        try:
            cell = self._ws.find(key, in_column=self._id_col())
            return cell.row if cell else None
        except gspread.exceptions.CellNotFound:
            return None

    def append(self, model: T) -> None:
        row = _model_to_row(model, self._headers)
        self._ws.append_row(row, value_input_option="USER_ENTERED")
        log.debug("Appended %s", self._cls.__name__)

    def append_many(self, models: List[T]) -> None:
        """Write all models in a single API call (avoids rate limits)."""
        if not models:
            return
        rows = [_model_to_row(m, self._headers) for m in models]
        self._ws.append_rows(rows, value_input_option="USER_ENTERED")
        log.debug("Batch-appended %d × %s", len(rows), self._cls.__name__)

    def update(self, model: T) -> None:
        key = str(getattr(model, "id", None) or getattr(model, "person_id", None) or getattr(model, "year", None))
        row_idx = self._find_row(key)
        if row_idx is None:
            raise KeyError(f"Row not found for key={key}")
        row     = _model_to_row(model, self._headers)
        col_end = _col_letter(len(self._headers))
        self._ws.update(f"A{row_idx}:{col_end}{row_idx}", [row], value_input_option="USER_ENTERED")
        log.debug("Updated %s key=%s", self._cls.__name__, key)

    def upsert(self, model: T) -> None:
        key = str(getattr(model, "id", None) or getattr(model, "person_id", None) or getattr(model, "year", None))
        if key and self._find_row(key) is not None:
            self.update(model)
        else:
            self.append(model)

    def get_all(self) -> List[T]:
        records = self._ws.get_all_records(
            value_render_option="UNFORMATTED_VALUE",
        )
        result = []
        for rec in records:
            cleaned = {k: (v if v != "" else None) for k, v in rec.items()}
            cleaned = _fix_date_serials(cleaned)
            cleaned = _fix_enum_reprs(cleaned)
            try:
                result.append(self._cls(**cleaned))
            except Exception as exc:
                log.warning("Skipping malformed row %s: %s", rec, exc)
        return result

    def get_by_id(self, key: str) -> Optional[T]:
        for m in self.get_all():
            pk = str(getattr(m, "id", None) or getattr(m, "person_id", None) or getattr(m, "year", None))
            if pk == key:
                return m
        return None

    def get_by_field(self, field: str, value: str) -> List[T]:
        return [m for m in self.get_all() if str(getattr(m, field, None)) == value]


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class SheetsRepository:
    def __init__(self) -> None:
        creds = Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS_FILE), scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        self._spreadsheet = gc.open_by_key(config.SPREADSHEET_ID)
        self._cache: Dict[str, gspread.Worksheet] = {}

    def _ws(self, name: str) -> gspread.Worksheet:
        if name not in self._cache:
            self._cache[name] = self._spreadsheet.worksheet(name)
        return self._cache[name]

    @property
    def persons(self) -> SheetTable[Person]:
        return SheetTable(self._ws("persons"), Person, SHEET_HEADERS["persons"])

    @property
    def invoices(self) -> SheetTable[Invoice]:
        return SheetTable(self._ws("invoices"), Invoice, SHEET_HEADERS["invoices"])

    @property
    def settlement_reports(self) -> SheetTable[SettlementReport]:
        return SheetTable(self._ws("settlement_reports"), SettlementReport, SHEET_HEADERS["settlement_reports"])

    @property
    def settlement_line_items(self) -> SheetTable[SettlementLineItem]:
        return SheetTable(self._ws("settlement_line_items"), SettlementLineItem, SHEET_HEADERS["settlement_line_items"])

    @property
    def payments(self) -> SheetTable[Payment]:
        return SheetTable(self._ws("payments"), Payment, SHEET_HEADERS["payments"])

    @property
    def beihilfe_deductible(self) -> SheetTable[BeihilfeDeductible]:
        return SheetTable(self._ws("beihilfe_deductible"), BeihilfeDeductible, SHEET_HEADERS["beihilfe_deductible"])

    @property
    def documents(self) -> SheetTable[Document]:
        return SheetTable(self._ws("documents"), Document, SHEET_HEADERS["documents"])

    def two_plus_children(self) -> bool:
        """Derive the §47 household flag from the persons sheet."""
        children = [p for p in self.persons.get_all() if str(p.role) == "child"]
        return len(children) >= 2

    def raw_worksheet(self, name: str) -> gspread.Worksheet:
        return self._ws(name)
