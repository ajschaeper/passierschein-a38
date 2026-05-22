"""WF-6: Alerts & Follow-ups"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from rich.console import Console
from rich.table import Table

from ..adapters.sheets import SheetsRepository
from ..config import config
from ..domain.enums import ClaimStatus, PaymentStatus

console = Console()
ZERO = Decimal("0")


def run(repo: SheetsRepository | None = None) -> None:
    repo     = repo or SheetsRepository()
    today    = date.today()
    invoices = repo.invoices.get_all()
    console.rule("[bold red]WF-6 Alerts & Follow-ups")
    fired    = False

    # 1. Unpaid invoices with approaching / past due date
    unpaid = [
        inv for inv in invoices
        if inv.payment_status == PaymentStatus.OPEN
    ]
    if unpaid:
        fired = True
        t = Table(title="[yellow]⚠ Unpaid Invoices[/yellow]", show_lines=True)
        t.add_column("Invoice ID"); t.add_column("Provider")
        t.add_column("Due"); t.add_column("Amount (€)", justify="right")
        t.add_column("Days to due", justify="right")
        for inv in unpaid:
            days = (inv.due_date - today).days if inv.due_date else None
            days_str = str(days) if days is not None else "—"
            if days is not None and days < 0:
                days_str = f"[red]{days}[/red]"
            t.add_row(inv.id[:8]+"…", inv.provider, str(inv.due_date or "—"),
                      f"{inv.employee_net_expected:,.2f}", days_str)
        console.print(t)

    # 2. PKV claims overdue
    pkv_threshold = timedelta(weeks=config.PKV_ALERT_WEEKS)
    pkv_overdue = [
        inv for inv in invoices
        if inv.pkv_claim_status == ClaimStatus.CLAIMED
        and inv.pkv_submitted_at
        and (today - inv.pkv_submitted_at) > pkv_threshold
    ]
    if pkv_overdue:
        fired = True
        t = Table(title=f"[red]⚠ PKV Claims Overdue (>{config.PKV_ALERT_WEEKS}w)[/red]", show_lines=True)
        t.add_column("Invoice ID"); t.add_column("Provider")
        t.add_column("Submitted"); t.add_column("Expected (€)", justify="right")
        t.add_column("Weeks", justify="right")
        for inv in pkv_overdue:
            weeks = (today - inv.pkv_submitted_at).days // 7
            t.add_row(inv.id[:8]+"…", inv.provider, str(inv.pkv_submitted_at),
                      f"{inv.pkv_expected:,.2f}", f"[red]{weeks}[/red]")
        console.print(t)

    # 3. Beihilfe claims overdue
    bh_threshold = timedelta(weeks=config.BEIHILFE_ALERT_WEEKS)
    bh_overdue = [
        inv for inv in invoices
        if inv.beihilfe_claim_status == ClaimStatus.CLAIMED
        and inv.beihilfe_submitted_at
        and (today - inv.beihilfe_submitted_at) > bh_threshold
    ]
    if bh_overdue:
        fired = True
        t = Table(title=f"[red]⚠ Beihilfe Claims Overdue (>{config.BEIHILFE_ALERT_WEEKS}w)[/red]", show_lines=True)
        t.add_column("Invoice ID"); t.add_column("Provider")
        t.add_column("Submitted"); t.add_column("Expected (€)", justify="right")
        t.add_column("Weeks", justify="right")
        for inv in bh_overdue:
            weeks = (today - inv.beihilfe_submitted_at).days // 7
            t.add_row(inv.id[:8]+"…", inv.provider, str(inv.beihilfe_submitted_at),
                      f"{inv.beihilfe_expected:,.2f}", f"[red]{weeks}[/red]")
        console.print(t)

    # 4. PKV settled but Beihilfe still open
    pkv_done_bh_open = [
        inv for inv in invoices
        if inv.pkv_claim_status == ClaimStatus.SETTLED
        and inv.beihilfe_claim_status == ClaimStatus.CLAIMED
    ]
    if pkv_done_bh_open:
        fired = True
        t = Table(title="[yellow]ℹ PKV Settled — Beihilfe Still Open[/yellow]", show_lines=True)
        t.add_column("Invoice ID"); t.add_column("Provider")
        t.add_column("Beihilfe expected (€)", justify="right")
        for inv in pkv_done_bh_open:
            t.add_row(inv.id[:8]+"…", inv.provider, f"{inv.beihilfe_expected:,.2f}")
        console.print(t)

    # 5. High net exposure
    total_paid = sum((inv.employee_net_expected for inv in invoices if inv.payment_status == PaymentStatus.PAID), ZERO)
    total_reimbursed = sum((inv.total_reimbursed for inv in invoices), ZERO)
    net_exposure = total_paid - total_reimbursed
    if net_exposure > Decimal(str(config.CASHFLOW_ALERT_EUR)):
        fired = True
        console.print(
            f"\n[bold red]⚠ Net exposure €{net_exposure:,.2f} exceeds threshold €{config.CASHFLOW_ALERT_EUR:,.0f}[/bold red]"
        )

    if not fired:
        console.print("[bold green]✓ No alerts.[/bold green]")
