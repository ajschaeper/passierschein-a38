"""
Bank statement CSV import adapters.

Currently supported: C24 Bank

Actual C24 CSV format (comma-separated, UTF-8):
  Transaktionstyp,Buchungsdatum,Karteneinsatz,Betrag,Zahlungsempfänger,
  IBAN,BIC,Verwendungszweck,Beschreibung,Kontonummer,Kontoname,
  Kategorie,Unterkategorie,Bargeldabhebung

Amount format: "-99,49 €"  (quoted, includes € sign, German decimal)
Negative Betrag = outbound (you paid); positive = inbound (reimbursement).

No preamble — the header row is line 1.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List

from ..domain.enums import PaymentDirection

log = logging.getLogger(__name__)

_C24_DATE_FMT = "%d.%m.%Y"

# canonical key → list of normalised column-name variants
_FIELD_VARIANTS: dict[str, list[str]] = {
    "buchungsdatum":      ["buchungsdatum"],
    "betrag":             ["betrag"],
    "zahlungsempfaenger": ["zahlungsempfaenger", "zahlungsempfanger"],
    "verwendungszweck":   ["verwendungszweck"],
    "kategorie":          ["kategorie"],
    "unterkategorie":     ["unterkategorie"],
}

# C24 Kategorie values that indicate a health-related payment worth matching.
_HEALTH_CATEGORIES = {"gesundheit"}

# C24 Unterkategorie values that also indicate health, regardless of Kategorie.
# "Beihilfe" catches inbound reimbursements from Landeshauptkasse NRW which
# C24 classifies as "Einkommen / Beihilfe" rather than "Gesundheit".
_HEALTH_SUBCATEGORIES = {"beihilfe"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(s: str) -> str:
    """Lowercase + strip + replace German umlauts for robust column matching."""
    return (
        s.strip().lower()
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("ß", "ss")
    )


def _detect_encoding(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            path.read_text(encoding=enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _detect_delimiter(header_line: str) -> str:
    """Pick the delimiter that produces more columns on the header row."""
    if header_line.count(";") > header_line.count(","):
        return ";"
    return ","


def _parse_decimal(s: str) -> Decimal:
    """
    Convert C24 amount string to Decimal.
    Handles: '"-99,49 €"' → -99.49, '"621,89 €"' → 621.89
    Also handles plain formats: '-142,50' and '142.50'
    """
    # Strip currency symbol, non-breaking space, regular space
    s = s.strip().replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "")
    if not s:
        raise ValueError("empty amount")
    if "," in s:
        # German format: dots are thousands separators, comma is decimal
        s = s.replace(".", "").replace(",", ".")
    return Decimal(s)


def _fingerprint(row_date: date, amount: Decimal, verwendungszweck: str) -> str:
    """
    16-char SHA-1 prefix — stable deduplication key for re-importing the same CSV.
    Keyed on (date, abs_amount, Verwendungszweck).
    """
    key = f"{row_date.isoformat()}|{str(abs(amount))}|{verwendungszweck.strip()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_c24(file_path: str | Path) -> List[dict]:
    """
    Parse a C24 Bank CSV export.

    Returns a list of transaction dicts, each containing:
      date              – datetime.date
      amount            – Decimal, always positive
      direction         – PaymentDirection (outbound/inbound)
      counterparty      – str | None  (Zahlungsempfänger)
      bank_reference    – str | None  (Verwendungszweck)
      import_fingerprint– str  (16-char dedup key)
      kategorie         – str | None  (C24 category, e.g. 'Gesundheit')
      is_medical_hint   – bool  (True if C24 tagged this as health)

    Raises ValueError if the file doesn't look like a C24 CSV.
    """
    path = Path(file_path)
    enc  = _detect_encoding(path)
    text = path.read_text(encoding=enc)
    lines = text.splitlines()

    # Locate the header row (scan for 'Buchungsdatum'; handles optional preamble)
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if "buchungsdatum" in line.lower():
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            f"Cannot parse {path.name}: no 'Buchungsdatum' column found. "
            "Is this a C24 Bank CSV export?"
        )

    header_line = lines[header_idx]
    delimiter   = _detect_delimiter(header_line)
    data_text   = "\n".join(lines[header_idx:])
    reader      = csv.DictReader(io.StringIO(data_text), delimiter=delimiter)

    # Build normalised-name → original-fieldname lookup
    col_map: dict[str, str] = {}
    if reader.fieldnames:
        for fn in reader.fieldnames:
            col_map[_normalise(fn)] = fn

    def _get(row: dict, canonical: str) -> str:
        for variant in _FIELD_VARIANTS.get(canonical, [canonical]):
            orig = col_map.get(variant)
            if orig and orig in row:
                return (row[orig] or "").strip()
        return ""

    results: List[dict] = []
    for row in reader:
        date_str = _get(row, "buchungsdatum")
        if not date_str:
            continue
        try:
            row_date = datetime.strptime(date_str, _C24_DATE_FMT).date()
        except ValueError:
            log.warning("Skipping row with unparseable date: %r", date_str)
            continue

        betrag_str = _get(row, "betrag")
        try:
            betrag = _parse_decimal(betrag_str)
        except (InvalidOperation, ValueError):
            log.warning("Skipping row with unparseable amount: %r", betrag_str)
            continue

        direction    = PaymentDirection.OUTBOUND if betrag < 0 else PaymentDirection.INBOUND
        amount       = abs(betrag)
        counterparty = _get(row, "zahlungsempfaenger") or None
        verwendung   = _get(row, "verwendungszweck")
        kategorie      = _get(row, "kategorie") or None
        unterkategorie = _get(row, "unterkategorie") or None
        fp             = _fingerprint(row_date, amount, verwendung)
        is_medical     = (
            _normalise(kategorie      or "") in _HEALTH_CATEGORIES
            or _normalise(unterkategorie or "") in _HEALTH_SUBCATEGORIES
        )

        results.append({
            "date":               row_date,
            "amount":             amount,
            "direction":          direction,
            "counterparty":       counterparty,
            "bank_reference":     verwendung or None,
            "import_fingerprint": fp,
            "kategorie":          kategorie,
            "unterkategorie":     unterkategorie,
            "is_medical_hint":    is_medical,
        })

    log.info(
        "Parsed %d transactions from %s (delimiter=%r, enc=%s)",
        len(results), path.name, delimiter, enc,
    )
    return results
