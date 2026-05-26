"""
WF-1: Invoice Intake

Manual entry flow (OCR deactivated).
Prompts for all invoice fields with plausible defaults,
resolves the split, and persists to Google Sheets.
"""
from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from ..adapters.manual_entry import enter_invoice
from ..adapters.sheets import SheetsRepository
from typing import Optional

from ..domain.models import Invoice

console = Console()


def run(
    repo: SheetsRepository | None = None,
    dry_run: bool = False,
    document_id: str | None = None,
    ocr_hints: dict | None = None,
) -> Optional[Invoice]:
    repo = repo or SheetsRepository()
    two_plus = repo.two_plus_children()
    persons  = repo.persons.get_all()
    persons_map = {p.person_id: f"{p.first_name} {p.family_name}" for p in persons}

    fields = enter_invoice(persons, two_plus_children=two_plus, ocr_hints=ocr_hints or {})
    if document_id:
        fields["document_id"] = document_id
    invoice = Invoice(**fields)

    # Confirmation summary
    t = Table(title="Invoice Summary — Please Confirm", show_lines=True)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("ID",                   invoice.id)
    t.add_row("Provider",             invoice.provider)
    t.add_row("Person",               persons_map.get(invoice.person_id, invoice.person_id))
    t.add_row("Date of service",      str(invoice.date_of_service))
    t.add_row("Split type",           str(invoice.split_type))
    t.add_row("Total amount",         f"€ {invoice.total_amount:,.2f}")
    t.add_row("Employee net expected",f"€ {invoice.employee_net_expected:,.2f}")
    t.add_row("PKV expected",         f"€ {invoice.pkv_expected:,.2f}  ({invoice.pkv_share_pct:.0%})")
    t.add_row("Beihilfe expected",    f"€ {invoice.beihilfe_expected:,.2f}  ({invoice.beihilfe_share_pct:.0%})")
    console.print(t)

    if dry_run:
        console.print("[yellow]Dry run — nothing written.[/yellow]")
        return invoice

    if Confirm.ask("Save to Sheets?", default=True):
        repo.invoices.append(invoice)
        console.print(f"[bold green]✓ Invoice {invoice.id} saved.[/bold green]")
        return invoice

    console.print("[yellow]Aborted.[/yellow]")
    return None
