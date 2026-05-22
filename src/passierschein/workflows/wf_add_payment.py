"""
WF-ADD-PAYMENT: Import a bank transaction as an unmatched Payment.

Captures the raw bank statement entry (direction, date, amount, counterparty)
without linking it to any invoice or settlement report yet.
Use match-payments afterwards to assign the match.
"""
from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from ..adapters.manual_entry import enter_bank_transaction
from ..adapters.sheets import SheetsRepository
from ..domain.models import Payment

console = Console()


def run(repo: SheetsRepository | None = None) -> Payment:
    repo = repo or SheetsRepository()

    fields  = enter_bank_transaction()
    payment = Payment(**fields)

    t = Table(title="Payment Summary", show_lines=True)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("ID",           payment.id[:8] + "…")
    t.add_row("Direction",    str(payment.direction))
    t.add_row("Date",         str(payment.date))
    t.add_row("Amount",       f"€ {payment.amount:,.2f}")
    t.add_row("Counterparty", payment.counterparty or "—")
    t.add_row("Bank ref",     payment.bank_reference or "—")
    t.add_row("Match status", str(payment.match_status))
    console.print(t)

    if Confirm.ask("Save to Sheets?", default=True):
        repo.payments.append(payment)
        console.print(f"[bold green]✓ Payment {payment.id[:8]}… saved (unmatched).[/bold green]")
    else:
        console.print("[yellow]Aborted.[/yellow]")

    return payment
