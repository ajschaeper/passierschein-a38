"""WF-5: Cashflow Dashboard"""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.table import Table

from ..adapters.sheets import SheetsRepository
from ..domain.enums import ClaimStatus, InboundPaymentStatus, PaymentStatus

console = Console()
ZERO = Decimal("0")


def run(repo: SheetsRepository | None = None) -> None:
    repo     = repo or SheetsRepository()
    invoices = repo.invoices.get_all()
    persons  = {p.person_id: p for p in repo.persons.get_all()}
    console.rule("[bold blue]WF-5 Cashflow Dashboard")

    total_billed     = sum((inv.employee_net_expected for inv in invoices), ZERO)
    total_paid_out   = sum((inv.employee_net_expected for inv in invoices if inv.payment_status == PaymentStatus.PAID), ZERO)
    payments_due     = total_billed - total_paid_out
    total_reimbursed = sum((inv.total_reimbursed for inv in invoices), ZERO)
    net_exposure     = total_paid_out - total_reimbursed

    pkv_pending = sum(
        (inv.pkv_expected - inv.pkv_reimbursed)
        for inv in invoices
        if inv.pkv_claim_status == ClaimStatus.CLAIMED
    )
    bh_pending = sum(
        (inv.beihilfe_expected - inv.beihilfe_reimbursed)
        for inv in invoices
        if inv.beihilfe_claim_status == ClaimStatus.CLAIMED
    )

    summary = Table(title="Cashflow Summary", show_lines=True, min_width=55)
    summary.add_column("Metric", style="bold")
    summary.add_column("Amount (€)", justify="right")
    summary.add_row("Total invoiced",               f"{total_billed:>12,.2f}")
    summary.add_row("Paid out by employee",          f"{total_paid_out:>12,.2f}")
    due_color = "red" if payments_due > ZERO else "green"
    summary.add_row("Payments due (by employee)",    f"[{due_color}]{payments_due:>12,.2f}[/{due_color}]")
    summary.add_row("Total reimbursed",              f"{total_reimbursed:>12,.2f}")
    exp_color = "red" if net_exposure > Decimal("5000") else "green"
    summary.add_row("Net current exposure",          f"[{exp_color}]{net_exposure:>12,.2f}[/{exp_color}]")
    summary.add_row("")
    summary.add_row("PKV inflows pending",           f"{pkv_pending:>12,.2f}")
    summary.add_row("Beihilfe inflows pending",      f"{bh_pending:>12,.2f}")
    summary.add_row("Exposure after pending inflows",f"{net_exposure - pkv_pending - bh_pending:>12,.2f}")
    console.print(summary)

    # Open invoices detail — sorted by due date ascending (nulls last)
    open_inv = [inv for inv in invoices if inv.payment_status != PaymentStatus.PAID or inv.net_cost > ZERO]
    open_inv.sort(key=lambda x: (x.due_date is None, x.due_date))
    if open_inv:
        t = Table(title="Open Invoices", show_lines=True)
        t.add_column("ID")
        t.add_column("Person")
        t.add_column("Provider")
        t.add_column("Service date")
        t.add_column("Due")
        t.add_column("Split", justify="center")
        t.add_column("Employee net (€)", justify="right")
        t.add_column("Reimbursed (€)",   justify="right")
        t.add_column("Net cost (€)",     justify="right")
        t.add_column("Pay")
        t.add_column("PKV")
        t.add_column("BH")
        for inv in open_inv[:25]:
            p    = persons.get(inv.person_id)
            name = p.first_name if p else inv.person_id[:8]
            pkv_pct = int(inv.pkv_share_pct * 100)
            bh_pct  = int(inv.beihilfe_share_pct * 100)
            t.add_row(
                inv.id[:8] + "…",
                name,
                inv.provider[:20],
                str(inv.date_of_service),
                str(inv.due_date or "—"),
                f"{pkv_pct}/{bh_pct}",
                f"{inv.employee_net_expected:,.2f}",
                f"{inv.total_reimbursed:,.2f}",
                f"{inv.net_cost:,.2f}",
                str(inv.payment_status),
                str(inv.pkv_claim_status),
                str(inv.beihilfe_claim_status),
            )
        console.print(t)
