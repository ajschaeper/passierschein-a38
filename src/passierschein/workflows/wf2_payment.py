"""
WF-2: Payment Recording

Mark one or more invoices as paid by a single outbound payment.
Records the Payment, sets invoice.payment_id, and updates payment_status.
"""
from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.prompt import Confirm

from ..adapters.manual_entry import enter_payment
from ..adapters.sheets import SheetsRepository
from ..domain.enums import PaymentStatus
from ..domain.models import Payment

console = Console()


def run(
    invoice_id: str,
    repo: SheetsRepository | None = None,
) -> None:
    repo    = repo or SheetsRepository()
    invoice = repo.invoices.get_by_id(invoice_id)

    if not invoice:
        console.print(f"[red]Invoice {invoice_id} not found.[/red]")
        return

    if invoice.payment_status == PaymentStatus.PAID:
        console.print(f"[yellow]Invoice {invoice_id} is already marked as paid.[/yellow]")
        return

    console.rule(f"[bold blue]WF-2 Payment — {invoice.provider}  €{invoice.employee_net_expected:,.2f}")

    fields  = enter_payment("outbound", invoice.employee_net_expected)
    payment = Payment(**fields)

    if Confirm.ask("Confirm outbound payment?", default=True):
        repo.payments.append(payment)
        invoice.payment_status = PaymentStatus.PAID
        invoice.date_paid      = payment.date
        invoice.payment_id     = payment.id
        repo.invoices.update(invoice)
        console.print(f"[bold green]✓ Payment recorded. Invoice marked as paid.[/bold green]")
    else:
        console.print("[yellow]Aborted.[/yellow]")
