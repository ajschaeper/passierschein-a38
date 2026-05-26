"""
WF-4: Settlement Report Processing

Step A — Capture report + match line items to invoices
Step B — Record the inbound payment against the report
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..adapters.manual_entry import enter_payment, enter_settlement_line_item, enter_settlement_report
from ..adapters.sheets import SheetsRepository
from ..domain.enums import (
    ClaimStatus,
    InboundPaymentStatus,
    PaymentStatus,
    SettlementLineItemStatus,
    SettlementReportLineItemsStatus,
)
from ..domain.models import Payment, SettlementLineItem, SettlementReport

console = Console()
ZERO = Decimal("0")


def _update_invoice_from_line_item(invoice_id: str, item: SettlementLineItem, repo: SheetsRepository) -> None:
    """Roll settlement line item result up to the invoice."""
    invoice = repo.invoices.get_by_id(invoice_id)
    if not invoice:
        return

    if item.type == "pkv":
        invoice.pkv_reimbursed   = item.reimbursed_amount
        invoice.pkv_claim_status = (
            ClaimStatus.SETTLED if item.status == SettlementLineItemStatus.COVERED
            else ClaimStatus.PARTIALLY_SETTLED
        )
    else:
        invoice.beihilfe_reimbursed   = item.reimbursed_amount
        invoice.beihilfe_claim_status = (
            ClaimStatus.SETTLED if item.status == SettlementLineItemStatus.COVERED
            else ClaimStatus.PARTIALLY_SETTLED
        )

    # Recompute totals (model_validator handles this on re-instantiation;
    # here we update manually since we're mutating in place)
    from decimal import ROUND_HALF_UP
    invoice.total_reimbursed = (invoice.pkv_reimbursed + invoice.beihilfe_reimbursed).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    invoice.net_cost = (invoice.employee_net_expected - invoice.total_reimbursed).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    repo.invoices.update(invoice)


def _update_beihilfe_deductible(deductible_applied: Decimal, year: int, repo: SheetsRepository) -> None:
    if deductible_applied <= ZERO:
        return
    ded = repo.beihilfe_deductible.get_by_id(str(year))
    if not ded:
        console.print(f"[yellow]No Beihilfe deductible configured for {year}. Skipping tracker update.[/yellow]")
        return
    ded.consumed_ytd += deductible_applied
    repo.beihilfe_deductible.update(ded)
    console.print(f"[dim]Deductible tracker: consumed YTD → €{ded.consumed_ytd:,.2f}  |  remaining → €{ded.remaining:,.2f}[/dim]")


# ---------------------------------------------------------------------------
# Step A: Capture report and enter line items
# ---------------------------------------------------------------------------

def process_report(
    repo: SheetsRepository | None = None,
    document_id: str | None = None,
    ocr_hints: dict | None = None,
) -> SettlementReport:
    repo = repo or SheetsRepository()
    console.rule("[bold blue]WF-4 Step A — Settlement Report")

    # 1. Enter report metadata (pre-filled from OCR if available)
    fields = enter_settlement_report(document_id=document_id, ocr_hints=ocr_hints or {})
    report = SettlementReport(**fields)

    # 2. Show open invoices of this type to match against
    invoices = repo.invoices.get_all()
    open_invoices = [
        inv for inv in invoices
        if (report.type == "pkv"      and str(inv.pkv_claim_status)       == "claimed")
        or (report.type == "beihilfe" and str(inv.beihilfe_claim_status)  == "claimed")
    ]

    if not open_invoices:
        console.print(f"[yellow]No open {report.type.upper()} claims found to match.[/yellow]")
        repo.settlement_reports.append(report)
        return report

    t = Table(title=f"Open {report.type.upper()} claims", show_lines=True)
    t.add_column("#")
    t.add_column("Invoice ID")
    t.add_column("Provider")
    t.add_column("Date of service")
    t.add_column("Expected (€)", justify="right")
    for i, inv in enumerate(open_invoices, 1):
        expected = inv.pkv_expected if report.type == "pkv" else inv.beihilfe_expected
        t.add_row(str(i), inv.id[:8] + "…", inv.provider, str(inv.date_of_service), f"{expected:,.2f}")
    console.print(t)

    # 3. Enter line items
    line_items: List[SettlementLineItem] = []
    total_reimbursed = ZERO

    while True:
        raw = Prompt.ask("Enter invoice # to add line item (or 'done')", default="done")
        if raw.strip().lower() == "done":
            break
        try:
            idx = int(raw) - 1
            inv = open_invoices[idx]
        except (ValueError, IndexError):
            console.print("[red]Invalid selection.[/red]")
            continue

        expected = inv.pkv_expected if report.type == "pkv" else inv.beihilfe_expected
        li_fields = enter_settlement_line_item(report.id, inv.id, report.type, expected)
        item = SettlementLineItem(**li_fields)
        line_items.append(item)
        total_reimbursed += item.reimbursed_amount

        # Update Beihilfe deductible tracker
        if report.type == "beihilfe":
            _update_beihilfe_deductible(item.deductible_applied, inv.date_of_service.year, repo)

        # Roll up to invoice
        _update_invoice_from_line_item(inv.id, item, repo)

    # 4. Finalise report
    report.total_reimbursed = total_reimbursed
    unmatched = len(open_invoices) - len(line_items)
    report.line_items_status = (
        SettlementReportLineItemsStatus.FULLY_MATCHED if unmatched == 0
        else SettlementReportLineItemsStatus.ITEMS_UNMATCHED
    )

    repo.settlement_reports.append(report)
    for item in line_items:
        repo.settlement_line_items.append(item)

    console.print(
        f"[bold green]✓ Report {report.id[:8]}… saved. "
        f"{len(line_items)} line item(s) matched. "
        f"Total reimbursed: €{total_reimbursed:,.2f}[/bold green]"
    )
    if unmatched:
        console.print(f"[yellow]{unmatched} invoice(s) not yet matched — revisit later.[/yellow]")

    return report


# ---------------------------------------------------------------------------
# Step B: Record the inbound payment
# ---------------------------------------------------------------------------

def record_inbound_payment(
    report_id: str,
    repo: SheetsRepository | None = None,
) -> None:
    repo   = repo or SheetsRepository()
    # Accept either the internal UUID or the human-readable report_reference
    report = repo.settlement_reports.get_by_id(report_id)
    if report is None:
        matches = repo.settlement_reports.get_by_field("report_reference", report_id)
        report  = matches[0] if matches else None

    if not report:
        console.print(f"[red]Settlement report '{report_id}' not found (tried id and report_reference).[/red]")
        return

    console.rule(f"[bold blue]WF-4 Step B — Inbound Payment for report {report_id[:8]}…")
    console.print(f"Expected payment: [bold]€{report.total_reimbursed:,.2f}[/bold]")

    fields  = enter_payment("inbound", report.total_reimbursed, linked_report_id=report.id)
    payment = Payment(**fields)
    payment.discrepancy = payment.amount - report.total_reimbursed

    if abs(payment.discrepancy) > Decimal("0.02"):
        console.print(
            f"[bold red]⚠ Discrepancy: €{payment.discrepancy:,.2f} "
            f"(payment {payment.amount} ≠ report {report.total_reimbursed})[/bold red]"
        )
        if not Confirm.ask("Accept discrepancy and proceed?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return

    if Confirm.ask("Confirm inbound payment?", default=True):
        report.payment_status = PaymentStatus.PAID
        repo.settlement_reports.update(report)
        repo.payments.append(payment)

        # Mark invoice payment statuses
        line_items = repo.settlement_line_items.get_by_field("settlement_report_id", report_id)
        for item in line_items:
            inv = repo.invoices.get_by_id(item.invoice_id)
            if not inv:
                continue
            if item.type == "pkv":
                inv.pkv_payment_status = InboundPaymentStatus.RECEIVED
            else:
                inv.beihilfe_payment_status = InboundPaymentStatus.RECEIVED
            repo.invoices.update(inv)

        console.print(
            f"[bold green]✓ Inbound payment €{payment.amount:,.2f} recorded. "
            f"{len(line_items)} invoice(s) updated.[/bold green]"
        )
