"""
WF-MATCH-PAYMENTS: Match unmatched payments to invoices or settlement reports.

Outbound payments: user selects one or more unpaid invoices covered by the
payment (N invoices → 1 payment). invoice.payment_id is set on each.

Inbound payments: matched 1:1 to a settlement report by amount proximity.
Status updates cascade to all line-item invoices.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..adapters.sheets import SheetsRepository
from ..domain.enums import (
    InboundPaymentStatus,
    PaymentMatchStatus,
    PaymentStatus,
)
from ..domain.models import Invoice, Payment

console = Console()
ZERO      = Decimal("0")
TOLERANCE = Decimal("0.50")


# ---------------------------------------------------------------------------
# Inbound: 1 payment → 1 settlement report
# ---------------------------------------------------------------------------

def _suggest_inbound(payment: Payment, repo: SheetsRepository) -> list:
    return [
        r for r in repo.settlement_reports.get_all()
        if str(r.payment_status) != "paid"
        and abs(r.total_reimbursed - payment.amount) <= TOLERANCE
    ]


def _match_inbound(payment: Payment, report_id: str, repo: SheetsRepository) -> None:
    report = repo.settlement_reports.get_by_id(report_id)
    if not report:
        console.print(f"[red]Settlement report {report_id} not found.[/red]")
        return

    payment.settlement_report_id = report_id
    payment.match_status         = PaymentMatchStatus.MATCHED
    payment.discrepancy          = payment.amount - report.total_reimbursed

    report.payment_status = PaymentStatus.PAID
    repo.settlement_reports.update(report)

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

    repo.payments.update(payment)
    console.print(
        f"[bold green]✓ Matched to report {report_id[:8]}… "
        f"(discrepancy: €{payment.discrepancy:,.2f}, "
        f"{len(line_items)} invoice(s) updated)[/bold green]"
    )


# ---------------------------------------------------------------------------
# Outbound: 1 payment → N invoices
# ---------------------------------------------------------------------------

def _match_outbound(payment: Payment, invoices: List[Invoice], repo: SheetsRepository) -> None:
    total_covered = sum(inv.employee_net_expected for inv in invoices)
    payment.match_status = PaymentMatchStatus.MATCHED
    payment.discrepancy  = payment.amount - total_covered
    repo.payments.update(payment)

    for inv in invoices:
        inv.payment_status = PaymentStatus.PAID
        inv.date_paid      = payment.date
        inv.payment_id     = payment.id
        repo.invoices.update(inv)

    console.print(
        f"[bold green]✓ Payment matched to {len(invoices)} invoice(s). "
        f"Total covered: €{total_covered:,.2f}  |  "
        f"Discrepancy: €{payment.discrepancy:,.2f}[/bold green]"
    )


def _select_invoices_for_outbound(payment: Payment, repo: SheetsRepository) -> List[Invoice]:
    """Interactive multi-select of unpaid invoices for one outbound payment."""
    all_invoices = repo.invoices.get_all()
    unpaid = [inv for inv in all_invoices if str(inv.payment_status) != "paid"]

    if not unpaid:
        console.print("[yellow]No unpaid invoices.[/yellow]")
        return []

    persons = {p.person_id: p.first_name for p in repo.persons.get_all()}

    t = Table(title=f"Unpaid invoices  (payment: €{payment.amount:,.2f})", show_lines=True)
    t.add_column("#")
    t.add_column("Person")
    t.add_column("Provider")
    t.add_column("Service date")
    t.add_column("Amount (€)", justify="right")
    for i, inv in enumerate(unpaid, 1):
        t.add_row(
            str(i),
            persons.get(inv.person_id, inv.person_id[:8]),
            inv.provider[:30],
            str(inv.date_of_service),
            f"{inv.employee_net_expected:,.2f}",
        )
    console.print(t)

    selected: List[Invoice] = []
    running = ZERO

    while True:
        console.print(
            f"  Running total: €{running:,.2f} / €{payment.amount:,.2f}  "
            f"  Remaining: €{payment.amount - running:,.2f}"
        )
        raw = Prompt.ask("Invoice #(s) to add (comma-separated), or 'done'", default="done")
        if raw.strip().lower() == "done":
            break
        for part in raw.split(","):
            part = part.strip()
            try:
                idx = int(part) - 1
                inv = unpaid[idx]
                if inv in selected:
                    console.print(f"[yellow]  #{idx+1} already selected.[/yellow]")
                else:
                    selected.append(inv)
                    running += inv.employee_net_expected
                    console.print(f"  [dim]Added: {inv.provider[:30]}  €{inv.employee_net_expected:,.2f}[/dim]")
            except (ValueError, IndexError):
                console.print(f"[red]  Invalid selection: {part!r}[/red]")

    return selected


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(repo: SheetsRepository | None = None) -> None:
    repo = repo or SheetsRepository()

    all_payments = repo.payments.get_all()
    unmatched    = [p for p in all_payments if str(p.match_status) == "unmatched"]

    if not unmatched:
        console.print("[green]No unmatched payments.[/green]")
        return

    console.rule(f"[bold blue]WF-MATCH — {len(unmatched)} unmatched payment(s)")

    for payment in unmatched:
        console.print(
            f"\n[bold]{payment.id[:8]}…[/bold]  "
            f"{payment.direction}  €{payment.amount:,.2f}  "
            f"{payment.date}  [dim]{payment.counterparty or ''}[/dim]"
        )

        direction = str(payment.direction)

        if direction == "outbound":
            selected = _select_invoices_for_outbound(payment, repo)
            if not selected:
                console.print("[yellow]Skipped.[/yellow]")
                continue
            total = sum(inv.employee_net_expected for inv in selected)
            console.print(
                f"\nSelected {len(selected)} invoice(s), total €{total:,.2f}  "
                f"(payment €{payment.amount:,.2f})"
            )
            if not Confirm.ask("Confirm match?", default=True):
                console.print("[yellow]Skipped.[/yellow]")
                continue
            _match_outbound(payment, selected, repo)

        else:  # inbound
            candidates = _suggest_inbound(payment, repo)
            if candidates:
                t = Table(title="Suggested settlement reports", show_lines=True)
                t.add_column("#")
                t.add_column("Details")
                for i, r in enumerate(candidates, 1):
                    t.add_row(str(i), f"{r.id[:8]}… | {r.report_reference} | €{r.total_reimbursed:,.2f} | {r.received_at}")
                console.print(t)
                raw = Prompt.ask(
                    "Select # or enter report ID (or 'skip')",
                    default="1" if len(candidates) == 1 else "skip",
                )
            else:
                console.print("[yellow]No automatic suggestions.[/yellow]")
                raw = Prompt.ask("Enter settlement report ID (or 'skip')", default="skip")

            if raw.strip().lower() == "skip":
                continue
            try:
                report_id = candidates[int(raw) - 1].id
            except (ValueError, IndexError):
                report_id = raw.strip()

            if not Confirm.ask(f"Confirm match → {report_id[:8]}…?", default=True):
                continue
            _match_inbound(payment, report_id, repo)
