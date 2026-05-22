"""
Import C24 bank transaction CSV as unmatched Payments.

Each row becomes a Payment(match_status=unmatched) unless it already exists
in the sheet (deduplicated by date + amount + counterparty).

Internal C24 transfers (Pocket-Umbuchung, Sparen) are skipped automatically.

Usage:
    python scripts/import_bank_csv.py Transaktionen.csv [--dry-run]
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import argparse

from rich.console import Console
from rich.table import Table

from passierschein.adapters.sheets import SheetsRepository
from passierschein.domain.enums import PaymentDirection, PaymentMatchStatus
from passierschein.domain.models import Payment

console = Console()

# ---------------------------------------------------------------------------
# Column indices (0-based, matching C24 CSV header order)
# ---------------------------------------------------------------------------
COL_TYPE        = 0   # Transaktionstyp
COL_DATE        = 1   # Buchungsdatum
COL_CARD_DATE   = 2   # Karteneinsatz
COL_AMOUNT      = 3   # Betrag
COL_PAYEE       = 4   # Zahlungsempfänger
COL_IBAN        = 5   # IBAN
COL_BIC         = 6   # BIC
COL_REFERENCE   = 7   # Verwendungszweck
COL_DESCRIPTION = 8   # Beschreibung  (often the legal counterparty name)
COL_ACCOUNT_NR  = 9   # Kontonummer
COL_ACCOUNT     = 10  # Kontoname
COL_CATEGORY    = 11  # Kategorie
COL_SUBCATEGORY = 12  # Unterkategorie

# Transaction types that are C24-internal and never represent real payments
_SKIP_TYPES = {"Pocket-Umbuchung", "Sparen"}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> date:
    """Parse DD.MM.YYYY → date."""
    d, m, y = raw.strip().split(".")
    return date(int(y), int(m), int(d))


def _parse_amount(raw: str) -> tuple[Decimal, PaymentDirection]:
    """
    Parse '"-99,49 €"' or '"621,89 €"' into (abs_amount, direction).
    Negative → outbound (employee pays provider).
    Positive → inbound (reimbursement received).
    """
    cleaned = raw.strip().replace("€", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Cannot parse amount: {raw!r}")
    direction = PaymentDirection.OUTBOUND if value < 0 else PaymentDirection.INBOUND
    return abs(value), direction


def _get(row: list[str], idx: int) -> str:
    try:
        return row[idx].strip()
    except IndexError:
        return ""


def _parse_row(row: list[str]) -> Payment | None:
    tx_type = _get(row, COL_TYPE)
    if tx_type in _SKIP_TYPES:
        return None

    raw_date   = _get(row, COL_DATE)
    raw_amount = _get(row, COL_AMOUNT)
    if not raw_date or not raw_amount:
        return None

    tx_date            = _parse_date(raw_date)
    amount, direction  = _parse_amount(raw_amount)

    # Prefer Beschreibung (legal name) over Zahlungsempfänger when available
    description = _get(row, COL_DESCRIPTION)
    payee       = _get(row, COL_PAYEE)
    counterparty = description if description else payee

    reference = _get(row, COL_REFERENCE)
    card_ts   = _get(row, COL_CARD_DATE)
    # For card transactions Verwendungszweck is usually empty; use the card
    # timestamp as a synthetic reference so identical same-day card payments
    # (e.g. two kids' prescriptions at the same pharmacy) don't dedup as one.
    if not reference and card_ts:
        reference = f"card:{card_ts}"

    return Payment(
        direction      = direction,
        date           = tx_date,
        amount         = amount,
        counterparty   = counterparty or None,
        bank_reference = reference,
        match_status   = PaymentMatchStatus.UNMATCHED,
    )


# ---------------------------------------------------------------------------
# Deduplication key
# ---------------------------------------------------------------------------

def _key(p: Payment) -> tuple:
    return (str(p.date), str(p.amount), p.counterparty or "", p.bank_reference or "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(csv_path: Path, dry_run: bool = False) -> None:
    console.rule(f"[bold blue]C24 CSV Import — {csv_path.name}")

    # Parse CSV
    with csv_path.open(encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows   = list(reader)

    console.print(f"[dim]Read {len(rows)} data rows (+ 1 header)[/dim]")

    parsed: list[Payment] = []
    skipped_internal = 0
    skipped_parse_error = 0

    for i, row in enumerate(rows, 2):  # 2 = first data row
        tx_type = _get(row, COL_TYPE)
        if tx_type in _SKIP_TYPES:
            skipped_internal += 1
            continue
        try:
            p = _parse_row(row)
            if p:
                parsed.append(p)
        except Exception as exc:
            console.print(f"[yellow]Row {i}: parse error — {exc}[/yellow]")
            skipped_parse_error += 1

    console.print(
        f"Parsed: [bold]{len(parsed)}[/bold]  |  "
        f"Skipped internal: {skipped_internal}  |  "
        f"Parse errors: {skipped_parse_error}"
    )

    # Deduplicate against existing payments in the sheet
    if not dry_run:
        console.print("Loading existing payments for deduplication…")
        repo     = SheetsRepository()
        existing = {_key(p) for p in repo.payments.get_all()}
    else:
        existing = set()

    new_payments = [p for p in parsed if _key(p) not in existing]
    duplicates   = len(parsed) - len(new_payments)

    console.print(
        f"New: [bold green]{len(new_payments)}[/bold green]  |  "
        f"Already in sheet: [dim]{duplicates}[/dim]"
    )

    if not new_payments:
        console.print("[green]Nothing to import.[/green]")
        return

    # Preview table
    t = Table(title=f"{'DRY RUN — ' if dry_run else ''}Payments to import", show_lines=True)
    t.add_column("Date")
    t.add_column("Dir")
    t.add_column("Amount (€)", justify="right")
    t.add_column("Counterparty")
    t.add_column("Reference")
    for p in new_payments[:30]:
        color = "green" if str(p.direction) == "inbound" else "red"
        t.add_row(
            str(p.date),
            f"[{color}]{p.direction}[/{color}]",
            f"{p.amount:,.2f}",
            (p.counterparty or "")[:35],
            (p.bank_reference or "")[:40],
        )
    if len(new_payments) > 30:
        t.add_row(f"… and {len(new_payments) - 30} more", "", "", "", "")
    console.print(t)

    if dry_run:
        console.print("[yellow]Dry run — nothing written.[/yellow]")
        return

    console.print(f"Writing {len(new_payments)} payments…")
    repo.payments.append_many(new_payments)
    console.print(f"[bold green]✓ Done. {len(new_payments)} payment(s) imported.[/bold green]")
    console.print("[dim]Run 'match-payments' to link them to invoices or settlement reports.[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import C24 bank CSV as unmatched Payments")
    parser.add_argument("csv_file", type=Path, help="Path to Transaktionen.csv")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if not args.csv_file.exists():
        console.print(f"[red]File not found: {args.csv_file}[/red]")
        sys.exit(1)

    main(args.csv_file, dry_run=args.dry_run)
