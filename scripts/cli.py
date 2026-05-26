"""
Passierschein A38 — CLI

    add-document                         Drive picker → OCR → Invoice or Settlement Report
    submit <id> [<id>...] --side pkv|beihilfe|both
    set-paid-out <invoice-id>            you paid the doctor
    set-paid-in <report-id>              reimbursement arrived
    add-payment                          import raw bank transaction (unmatched)
    match-payments                       match unmatched payments
    dashboard / alerts
    list-persons / add-person
    ocr <file>                           inspect OCR output (dev tool)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from typing import List, Optional

import typer

from passierschein.adapters.sheets import SheetsRepository
from passierschein.adapters.manual_entry import prompt_str, prompt_date, prompt_enum
from passierschein.domain.models import Person
from passierschein.domain.enums import PatientRole
from passierschein.workflows import (
    wf2_payment,
    wf3_submission,
    wf4_matching,
    wf5_dashboard,
    wf6_alerts,
    wf_add_document,
    wf_add_payment,
    wf_match_payment,
)

app = typer.Typer(name="a38", help="Passierschein A38 — Beihilfe & PKV tracker", no_args_is_help=True)


@app.command("add-person")
def add_person() -> None:
    """Add a household member (employee, spouse, or child)."""
    from rich.console import Console
    from rich.prompt import Confirm
    from rich.table import Table
    console = Console()
    console.rule("[bold cyan]Add Person")

    first_name  = prompt_str("First name")
    family_name = prompt_str("Family name")
    birth_date  = prompt_date("Birth date (YYYY-MM-DD)")
    role        = prompt_enum("Role", PatientRole)

    person = Person(
        first_name=first_name,
        family_name=family_name,
        birth_date=birth_date,
        role=role,
    )

    t = Table(show_lines=True)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("ID",          person.person_id)
    t.add_row("Name",        f"{person.first_name} {person.family_name}")
    t.add_row("Birth date",  str(person.birth_date))
    t.add_row("Role",        str(person.role))
    console.print(t)

    if Confirm.ask("Save to Sheets?", default=True):
        repo = SheetsRepository()
        repo.persons.append(person)
        console.print(f"[bold green]✓ {person.first_name} {person.family_name} saved (ID: {person.person_id})[/bold green]")
    else:
        console.print("[yellow]Aborted.[/yellow]")


@app.command("list-persons")
def list_persons() -> None:
    """List all household members."""
    from rich.console import Console
    from rich.table import Table
    console = Console()
    repo    = SheetsRepository()
    persons = repo.persons.get_all()
    two_plus = repo.two_plus_children()

    t = Table(title=f"Household members  (2+ children: {two_plus} → employee rate: {'70%' if two_plus else '50%'})", show_lines=True)
    t.add_column("ID");         t.add_column("First name"); t.add_column("Family name")
    t.add_column("Birth date"); t.add_column("Role")
    for p in persons:
        t.add_row(p.person_id[:8]+"…", p.first_name, p.family_name, str(p.birth_date), str(p.role))
    console.print(t)


@app.command("add-document")
def add_document(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Local file path (skips Drive picker)"),
    all_inbox: bool = typer.Option(False, "--all", help="Process every file in the Drive Inbox"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Drive picker → OCR → auto-classify → Invoice or Settlement Report.

    Use --all to process every file currently in the Inbox in one batch.
    """
    repo = SheetsRepository()
    if all_inbox:
        wf_add_document.run_batch(repo=repo)
    else:
        wf_add_document.run(repo=repo, local_file=file, dry_run=dry_run)


@app.command("set-paid-out")
def set_paid_out(invoice_id: str = typer.Argument(...)) -> None:
    """Record that you paid the doctor for an invoice."""
    wf2_payment.run(invoice_id)


@app.command()
def submit(
    invoice_ids: List[str] = typer.Argument(...),
    side: str = typer.Option("both", "--side", "-s", help="pkv | beihilfe | both"),
) -> None:
    """Record claim submission for one or more invoices."""
    wf3_submission.run(invoice_ids, side=side)  # type: ignore[arg-type]


@app.command("set-paid-in")
def set_paid_in(report_id: str = typer.Argument(...)) -> None:
    """Record the inbound reimbursement payment for a settlement report."""
    wf4_matching.record_inbound_payment(report_id)


@app.command("add-payment")
def add_payment() -> None:
    """Import a bank transaction as an unmatched payment."""
    wf_add_payment.run()


@app.command("match-payments")
def match_payments() -> None:
    """Match unmatched bank payments to invoices or settlement reports."""
    wf_match_payment.run()


@app.command()
def dashboard() -> None:
    """Show cashflow dashboard."""
    wf5_dashboard.run()


@app.command()
def alerts() -> None:
    """Show overdue claims and alerts."""
    wf6_alerts.run()


@app.command("ocr")
def ocr_file(
    file: str = typer.Argument(..., help="PDF or image file to inspect"),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSON instead of formatted table"),
) -> None:
    """Inspect OCR extraction output for a file (developer tool)."""
    import json as _json
    from rich.console import Console
    from rich.table import Table
    from passierschein.adapters.claude_ocr import extract_document_data
    console = Console()

    console.print(f"[dim]Extracting from [bold]{file}[/bold]…[/dim]")
    try:
        data = extract_document_data(file)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]OCR failed: {exc}[/red]")
        raise typer.Exit(1)

    if raw:
        console.print_json(_json.dumps(data, indent=2, default=str))
        return

    doc_type = data.get("document_type", "invoice")

    if doc_type == "settlement_report":
        h = Table(title=f"OCR — {Path(file).name}  [settlement report]", show_lines=True)
        h.add_column("Field", style="bold"); h.add_column("Value")
        for key, label in [
            ("report_type",      "Type"),
            ("report_reference", "Reference"),
            ("beihilfe_number",  "Beihilfe-Nr."),
            ("date",             "Date"),
            ("recipient_name",   "Recipient"),
            ("total_granted",    "Total granted (€)"),
        ]:
            val = data.get(key)
            if val is not None:
                h.add_row(label, str(val))
        console.print(h)

        items = data.get("line_items") or []
        if items:
            lt = Table(title=f"{len(items)} line item(s)", show_lines=True)
            lt.add_column("Beleg", style="bold")
            lt.add_column("Person")
            lt.add_column("Date")
            lt.add_column("Description")
            lt.add_column("Invoice €", justify="right")
            lt.add_column("BH %", justify="right")
            lt.add_column("BH due €", justify="right")
            lt.add_column("Eligible €", justify="right")
            lt.add_column("Granted €", justify="right")
            lt.add_column("Deductible €", justify="right")
            lt.add_column("Notes")
            for li in items:
                lt.add_row(
                    str(li.get("beleg_nr", "")),
                    str(li.get("person", "")),
                    str(li.get("invoice_date") or ""),
                    (li.get("description") or "")[:40],
                    str(li.get("total_amount") or ""),
                    f"{li.get('beihilfe_pct') or ''}%",
                    str(li.get("beihilfe_due") or ""),
                    str(li.get("amount_eligible") or ""),
                    str(li.get("amount_granted") or ""),
                    str(li.get("deductible") or ""),
                    (li.get("notes") or "")[:60],
                )
            console.print(lt)

    else:
        t = Table(title=f"OCR — {Path(file).name}  [invoice]", show_lines=True)
        t.add_column("Field", style="bold"); t.add_column("Extracted value")
        for key, label in [
            ("provider",          "Provider"),
            ("patient_name",      "Patient"),
            ("date_of_service",   "Date of service"),
            ("date_of_invoice",   "Date of invoice"),
            ("due_date",          "Due date"),
            ("total_amount",      "Total amount (€)"),
            ("split_type_hint",   "Split type"),
            ("invoice_type_hint", "Invoice type"),
            ("invoice_number",    "Invoice number"),
            ("notes",             "Notes"),
        ]:
            val = data.get(key)
            if val is not None and val != "":
                t.add_row(label, str(val))
        console.print(t)


if __name__ == "__main__":
    app()
