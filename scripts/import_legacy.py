"""
Import legacy Abrechnung CSV into Passierschein A38.

Column mapping (as specified):
  A (0)  date of service
  B (1)  provider
  C (2)  treatment  (used for invoice_type inference only)
  D (3)  person first name  ("beide" → split Maja + Leo 50/50; "?" → skip)
  E (4)  total amount
  F (5)  due date (may be empty)
  G (6)  payment status   bezahlt=paid | offen=open | else=open
  H (7)  date paid (may be empty)
  I (8)  split type       klassisch=classic | Beihilferechnung=beihilfe_only

  M (12) PKV amount reimbursed  (excl. employee eigenanteil)
  P (15) PKV claim status       erstattet=settled | eingereicht=claimed | offen=open | else=open
  Q (16) PKV eigenanteil        employee's share on PKV side  →  pkv_expected = M + Q

  R (17) Beihilfe amount reimbursed  (excl. eigenanteil)
  U (20) Beihilfe claim status       same mapping as PKV
  V (21) Beihilfe eigenanteil        →  beihilfe_expected = R + V
  W (22) Beihilfe settlement reference  (informational, not stored)

Usage:
    python scripts/import_legacy.py /path/to/Abrechnung.csv
    python scripts/import_legacy.py /path/to/Abrechnung.csv --dry-run
"""
from __future__ import annotations

import csv
import sys
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import typer
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm

from passierschein.adapters.sheets import SheetsRepository
from passierschein.domain.enums import (
    ClaimStatus,
    InboundPaymentStatus,
    InvoiceType,
    PaymentStatus,
    SplitType,
)
from passierschein.domain.models import Invoice

# ---------------------------------------------------------------------------
# Column indices  (A=0, B=1, …)
# ---------------------------------------------------------------------------
C_DATE_SVC   = 0
C_PROVIDER   = 1
C_TREATMENT  = 2
C_PERSON     = 3
C_AMOUNT     = 4
C_DUE        = 5
C_PAY_ST     = 6
C_DATE_PAID  = 7
C_SPLIT      = 8
C_PKV_AMT    = 12   # M
C_PKV_ST     = 15   # P
C_PKV_EIG    = 16   # Q
C_BH_AMT     = 17   # R
C_BH_ST      = 20   # U
C_BH_EIG     = 21   # V
C_BH_REF     = 22   # W  (logged only)

app     = typer.Typer()
console = Console()
ZERO    = Decimal("0")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _get(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def parse_amount(s: str) -> Decimal:
    s = s.strip().lstrip("€").strip()
    if not s or s in ("n/a", "-"):
        return ZERO
    s = s.replace(",", "")          # remove thousand separators
    try:
        return Decimal(s)
    except InvalidOperation:
        return ZERO


def parse_date(s: str) -> Optional[date]:
    """Accept DD.MM.YYYY; if multiple dates are present take the first valid one."""
    for part in s.split():
        part = part.strip()
        if len(part) == 10 and part.count(".") == 2:
            try:
                return datetime.strptime(part, "%d.%m.%Y").date()
            except ValueError:
                continue
    return None


def map_payment_status(s: str) -> PaymentStatus:
    return PaymentStatus.PAID if s.lower() == "bezahlt" else PaymentStatus.OPEN


def map_split_type(s: str) -> SplitType:
    return SplitType.BEIHILFE_ONLY if "beihilfe" in s.lower() else SplitType.CLASSIC


def map_claim_status(s: str) -> ClaimStatus:
    v = s.lower()
    if v == "erstattet":
        return ClaimStatus.SETTLED
    if v in ("eingereicht", "abzustimmen"):
        return ClaimStatus.CLAIMED
    if v == "offen":
        return ClaimStatus.OPEN
    return ClaimStatus.OPEN   # all others → open (n/a handled per split_type below)


# ---------------------------------------------------------------------------
# Invoice type inference from treatment description
# ---------------------------------------------------------------------------

_ITYPE_RULES: list[tuple[InvoiceType, list[str]]] = [
    (InvoiceType.STATIONAER,   ["geburt", "aufenthalt", "op ", "shunt", "kh ", "bronchitis"]),
    (InvoiceType.HEILMITTEL,   ["physio", "logop", "osteo", "heilprak", "therapie",
                                 "nachsorge", "reha"]),
    (InvoiceType.ARZNEIMITTEL, ["rezept", "impf", "medi", "hct", "salbe", "creme",
                                 "bigaia", "flutide", "infantrini", "sonde", "pumpe",
                                 "spiro", "pflaster", "oppi", "betahcg"]),
    (InvoiceType.HILFSMITTEL,  ["pflege", "monitor", "sauerstoff", "compass"]),
    (InvoiceType.AMBULANT,     ["labor", "untersuchung", "arzt", "hno", "kardiol",
                                 "chirurg", "screening", "konsiliar", "spz", "kontroll"]),
    (InvoiceType.OTHER,        ["betreuung"]),
]


def infer_invoice_type(treatment: str) -> InvoiceType:
    t = treatment.lower()
    for itype, keywords in _ITYPE_RULES:
        if any(kw in t for kw in keywords):
            return itype
    return InvoiceType.OTHER


# ---------------------------------------------------------------------------
# Row → Invoice(s)
# ---------------------------------------------------------------------------

def row_to_invoices(
    raw: list[str],
    person_map: dict[str, str],     # first_name.lower() → person_id
) -> tuple[list[Invoice], Optional[str]]:
    """
    Parse one data row.
    Returns (invoices, skip_reason). skip_reason is set when the row is skipped.
    """
    row = raw + [""] * max(0, C_BH_REF + 2 - len(raw))   # pad

    # --- Detect swapped Treatment / Person columns (data-entry error in some rows) ---
    person_raw = _get(row, C_PERSON)
    treatment  = _get(row, C_TREATMENT)
    known = set(person_map.keys()) | {"beide"}
    if person_raw.lower() not in known and treatment.lower() in known:
        person_raw, treatment = treatment, person_raw

    if person_raw.lower() in ("?", "unbekannt", ""):
        return [], f"unknown person '{person_raw}'"

    # --- Core invoice fields ---
    date_of_service = parse_date(_get(row, C_DATE_SVC))
    if not date_of_service:
        return [], "no valid date"

    total_amount = parse_amount(_get(row, C_AMOUNT))
    if total_amount <= ZERO:
        return [], "zero or missing amount"

    provider       = _get(row, C_PROVIDER) or "Unbekannt"
    due_date       = parse_date(_get(row, C_DUE))
    payment_status = map_payment_status(_get(row, C_PAY_ST))
    date_paid      = parse_date(_get(row, C_DATE_PAID))
    split_type     = map_split_type(_get(row, C_SPLIT))
    invoice_type   = infer_invoice_type(treatment)

    # --- PKV ---
    pkv_m  = parse_amount(_get(row, C_PKV_AMT))    # reimbursed by PKV
    pkv_q  = parse_amount(_get(row, C_PKV_EIG))    # employee's own share (eigenanteil)
    pkv_st = map_claim_status(_get(row, C_PKV_ST))

    if split_type == SplitType.BEIHILFE_ONLY:
        pkv_expected   = ZERO
        pkv_reimbursed = ZERO
        pkv_claim_st   = ClaimStatus.NOT_APPLICABLE
        pkv_share_pct  = ZERO
    else:
        pkv_expected   = pkv_m + pkv_q           # total PKV share = reimbursed + eigenanteil
        pkv_reimbursed = pkv_m if pkv_st == ClaimStatus.SETTLED else ZERO
        pkv_claim_st   = pkv_st
        pkv_share_pct  = (
            (pkv_expected / total_amount * 100).quantize(Decimal("0.1"))
            if total_amount else ZERO
        )

    pkv_pay_st = (
        InboundPaymentStatus.RECEIVED if pkv_claim_st == ClaimStatus.SETTLED
        else InboundPaymentStatus.OPEN
    )

    # --- Beihilfe ---
    bh_r  = parse_amount(_get(row, C_BH_AMT))     # reimbursed by BH
    bh_v  = parse_amount(_get(row, C_BH_EIG))     # employee's own share
    bh_st = map_claim_status(_get(row, C_BH_ST))

    bh_expected   = bh_r + bh_v                   # total BH share
    bh_reimbursed = bh_r if bh_st == ClaimStatus.SETTLED else ZERO
    bh_share_pct  = (
        (bh_expected / total_amount * 100).quantize(Decimal("0.1"))
        if total_amount else ZERO
    )
    bh_pay_st = (
        InboundPaymentStatus.RECEIVED if bh_st == ClaimStatus.SETTLED
        else InboundPaymentStatus.OPEN
    )

    # --- Build Invoice ---
    def make(
        person_id: str,
        amt: Decimal,
        p_exp: Decimal, p_reimb: Decimal, p_pct: Decimal,
        b_exp: Decimal, b_reimb: Decimal, b_pct: Decimal,
    ) -> Invoice:
        return Invoice(
            person_id             = person_id,
            invoice_type          = invoice_type,
            split_type            = split_type,
            provider              = provider,
            date_of_service       = date_of_service,
            date_received         = date_of_service,   # not in CSV → fall back to date_of_service
            due_date              = due_date,
            total_amount          = amt,
            employee_net_expected = amt,               # STANDARD: employee pays full invoice
            payment_status        = payment_status,
            date_paid             = date_paid,
            pkv_share_pct         = p_pct,
            pkv_expected          = p_exp,
            pkv_submitted_at      = None,
            pkv_reimbursed        = p_reimb,
            pkv_claim_status      = pkv_claim_st,
            pkv_payment_status    = pkv_pay_st,
            beihilfe_share_pct    = b_pct,
            beihilfe_expected     = b_exp,
            beihilfe_submitted_at = None,
            beihilfe_reimbursed   = b_reimb,
            beihilfe_claim_status = bh_st,
            beihilfe_payment_status = bh_pay_st,
        )

    # --- Resolve person ---
    if person_raw.lower() == "beide":
        maja_id = person_map.get("maja")
        leo_id  = person_map.get("leo")
        if not maja_id or not leo_id:
            return [], "beide: Maja or Leo not found in persons sheet"
        half      = (total_amount  / 2).quantize(Decimal("0.01"))
        p_exp_h   = (pkv_expected  / 2).quantize(Decimal("0.01"))
        p_reimb_h = (pkv_reimbursed / 2).quantize(Decimal("0.01"))
        b_exp_h   = (bh_expected   / 2).quantize(Decimal("0.01"))
        b_reimb_h = (bh_reimbursed / 2).quantize(Decimal("0.01"))
        return [
            make(maja_id, half, p_exp_h, p_reimb_h, pkv_share_pct, b_exp_h, b_reimb_h, bh_share_pct),
            make(leo_id,  half, p_exp_h, p_reimb_h, pkv_share_pct, b_exp_h, b_reimb_h, bh_share_pct),
        ], None

    person_id = person_map.get(person_raw.lower())
    if not person_id:
        return [], f"person '{person_raw}' not in persons sheet"

    return [make(
        person_id,
        total_amount,
        pkv_expected, pkv_reimbursed, pkv_share_pct,
        bh_expected,  bh_reimbursed,  bh_share_pct,
    )], None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def main(
    csv_path:      Path = typer.Argument(..., exists=True, help="Path to legacy Abrechnung CSV"),
    dry_run:       bool = typer.Option(False, "--dry-run", help="Parse and preview without writing"),
    clear_existing: bool = typer.Option(False, "--clear-existing", help="Delete all rows in the invoices sheet before importing"),
) -> None:
    """Import legacy Abrechnung CSV into Passierschein A38."""
    console.rule("[bold blue]Legacy CSV Import")

    # Load persons from Sheets
    console.print("Loading persons from Google Sheets…")
    repo     = SheetsRepository()
    persons  = repo.persons.get_all()
    pid_name = {p.person_id: p.first_name for p in persons}
    person_map: dict[str, str] = {p.first_name.lower(): p.person_id for p in persons}
    console.print(f"  Known persons: {', '.join(p.first_name for p in persons)}")

    # Parse CSV — skip the two header rows
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        all_rows = list(csv.reader(fh))

    data_rows = all_rows[2:]   # row 0 = group headers, row 1 = column headers

    invoices:   list[Invoice]             = []
    skipped:    list[tuple[int, str]]     = []

    for i, raw in enumerate(data_rows, start=3):
        # Skip completely blank rows
        if not any(c.strip() for c in raw[:9]):
            continue
        # Need at least a date and an amount
        if not raw[C_DATE_SVC].strip() or not raw[C_AMOUNT].strip():
            continue

        result, reason = row_to_invoices(raw, person_map)
        if reason:
            skipped.append((i, reason))
        else:
            invoices.extend(result)

    # --- Summary ---
    console.print(f"\n[bold]Parsed:[/bold] {len(invoices)} invoices  |  {len(skipped)} rows skipped\n")

    if skipped:
        skip_tbl = Table(title="Skipped rows", show_lines=False)
        skip_tbl.add_column("Row", justify="right", style="dim")
        skip_tbl.add_column("Reason")
        skip_tbl.add_column("Provider / raw")
        for row_num, reason in skipped:
            raw = data_rows[row_num - 3]
            skip_tbl.add_row(str(row_num), reason, f"{_get(raw, C_PROVIDER)} | {_get(raw, C_PERSON)}")
        console.print(skip_tbl)

    if not invoices:
        console.print("[red]Nothing to import.[/red]")
        raise typer.Exit(1)

    # --- Preview table ---
    t = Table(title=f"Invoices to import ({len(invoices)} total — showing first 30)", show_lines=True)
    t.add_column("#",           justify="right", style="dim")
    t.add_column("Date")
    t.add_column("Provider")
    t.add_column("Person")
    t.add_column("Type")
    t.add_column("Split")
    t.add_column("Total (€)",   justify="right")
    t.add_column("PKV exp (€)", justify="right")
    t.add_column("BH exp (€)",  justify="right")
    t.add_column("Pay")
    t.add_column("PKV")
    t.add_column("BH")

    for n, inv in enumerate(invoices[:30], 1):
        t.add_row(
            str(n),
            str(inv.date_of_service),
            inv.provider[:28],
            pid_name.get(inv.person_id, "?"),
            str(inv.invoice_type),
            str(inv.split_type),
            f"{inv.total_amount:,.2f}",
            f"{inv.pkv_expected:,.2f}",
            f"{inv.beihilfe_expected:,.2f}",
            str(inv.payment_status),
            str(inv.pkv_claim_status),
            str(inv.beihilfe_claim_status),
        )
    if len(invoices) > 30:
        t.add_row("…", f"({len(invoices) - 30} more)", *[""] * 10)
    console.print(t)

    total_eur       = sum(inv.total_amount        for inv in invoices)
    total_reimb_eur = sum(inv.total_reimbursed    for inv in invoices)
    total_net_eur   = sum(inv.net_cost            for inv in invoices)
    console.print(f"\nTotal invoiced:    [bold]€{total_eur:>12,.2f}[/bold]")
    console.print(f"Total reimbursed:  [bold]€{total_reimb_eur:>12,.2f}[/bold]")
    console.print(f"Residual net cost: [bold]€{total_net_eur:>12,.2f}[/bold]")

    if dry_run:
        console.print("\n[yellow]Dry run — nothing written to Sheets.[/yellow]")
        return

    if not Confirm.ask(f"\nWrite {len(invoices)} invoices to Google Sheets?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        return

    if clear_existing:
        ws = repo.raw_worksheet("invoices")
        last_row = len(ws.get_all_values())
        if last_row > 1:                          # keep header row 1
            ws.delete_rows(2, last_row)
            console.print(f"[dim]Cleared {last_row - 1} existing rows from invoices sheet.[/dim]")

    console.print(f"Writing {len(invoices)} rows in one batch…")
    try:
        repo.invoices.append_many(invoices)
        console.print(f"[bold green]✓ {len(invoices)} invoices imported.[/bold green]")
    except Exception as exc:
        console.print(f"[red]Batch write failed: {exc}[/red]")
        raise typer.Exit(1)
    console.rule()


if __name__ == "__main__":
    app()
