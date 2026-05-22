"""
One-time setup: create all Google Sheets tabs, write headers, seed reference data.
Safe to re-run — existing sheets are not overwritten.

    python scripts/setup_sheets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gspread
from google.oauth2.service_account import Credentials
from rich.console import Console
from rich.prompt import Confirm, Prompt

from passierschein.adapters.sheets import SHEET_HEADERS, SCOPES
from passierschein.config import config
from passierschein.domain.enums import PatientRole
from passierschein.domain.models import BeihilfeDeductible, Person
from passierschein.domain.split_matrix import SplitMatrix

console = Console()


def get_or_create(spreadsheet: gspread.Spreadsheet, name: str) -> tuple[gspread.Worksheet, bool]:
    try:
        ws = spreadsheet.worksheet(name)
        console.print(f"  [dim]{name} — already exists, skipping.[/dim]")
        return ws, False
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=2000, cols=30)
        console.print(f"  [green]{name} — created.[/green]")
        return ws, True


def write_headers(ws: gspread.Worksheet, headers: list[str], created: bool) -> None:
    if not created:
        return
    ws.update(range_name="A1", values=[headers], value_input_option="USER_ENTERED")
    ws.format("1:1", {"textFormat": {"bold": True}})


def seed_split_matrix(ws: gspread.Worksheet, created: bool) -> None:
    if not created:
        return
    rows = SplitMatrix().to_records()
    ws.append_rows([list(r.values()) for r in rows], value_input_option="USER_ENTERED")
    console.print(f"  [green]split_matrix — seeded {len(rows)} rows.[/green]")


def seed_persons(ws: gspread.Worksheet, created: bool) -> None:
    if not created:
        return
    console.print("\n[bold]Configure household members:[/bold]")
    persons = []

    emp_first  = Prompt.ask("Employee first name", default="Max")
    emp_family = Prompt.ask("Employee family name", default="Mustermann")
    emp_birth  = Prompt.ask("Employee birth date (YYYY-MM-DD)", default="1982-03-10")
    persons.append(Person(first_name=emp_first, family_name=emp_family,
                          birth_date=emp_birth, role=PatientRole.EMPLOYEE))

    if Confirm.ask("Add spouse?", default=True):
        sp_first  = Prompt.ask("Spouse first name", default="Anna")
        sp_family = Prompt.ask("Spouse family name", default=emp_family)
        sp_birth  = Prompt.ask("Spouse birth date (YYYY-MM-DD)", default="1985-07-22")
        persons.append(Person(first_name=sp_first, family_name=sp_family,
                              birth_date=sp_birth, role=PatientRole.SPOUSE))

    n = int(Prompt.ask("Number of children", default="0"))
    for i in range(n):
        c_first  = Prompt.ask(f"Child {i+1} first name")
        c_birth  = Prompt.ask(f"Child {i+1} birth date (YYYY-MM-DD)")
        persons.append(Person(first_name=c_first, family_name=emp_family,
                              birth_date=c_birth, role=PatientRole.CHILD))

    headers = SHEET_HEADERS["persons"]
    rows = [[str(p.model_dump().get(h, "")) for h in headers] for p in persons]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    console.print(f"\n  [green]{len(persons)} person(s) saved.[/green]")
    for p in persons:
        console.print(f"    {p.first_name} {p.family_name} ({p.role}) — ID: [cyan]{p.person_id}[/cyan]")


def seed_deductible(ws: gspread.Worksheet, created: bool) -> None:
    if not created:
        return
    from datetime import date
    year = date.today().year
    amount = Decimal_input = Prompt.ask(
        f"Beihilfe annual deductible for {year} (EUR, leave blank to skip)", default=""
    )
    if amount:
        from decimal import Decimal
        ded = BeihilfeDeductible(year=year, configured_amount=Decimal(amount))
        headers = SHEET_HEADERS["beihilfe_deductible"]
        row = [str(ded.model_dump().get(h, "")) for h in headers]
        ws.append_row(row, value_input_option="USER_ENTERED")
        console.print(f"  [green]Beihilfe deductible for {year}: €{amount}[/green]")


def main() -> None:
    console.rule("[bold blue]Passierschein A38 — Google Sheets Setup")
    console.print(f"Spreadsheet: [cyan]{config.SPREADSHEET_ID}[/cyan]")

    creds = Credentials.from_service_account_file(str(config.GOOGLE_CREDENTIALS_FILE), scopes=SCOPES)
    gc    = gspread.authorize(creds)
    ss    = gc.open_by_key(config.SPREADSHEET_ID)

    console.print("\n[bold]Worksheets:[/bold]")
    sheets: dict[str, tuple[gspread.Worksheet, bool]] = {}
    for name in SHEET_HEADERS:
        ws, created = get_or_create(ss, name)
        write_headers(ws, SHEET_HEADERS[name], created)
        sheets[name] = (ws, created)

    console.print("\n[bold]Seeding reference data:[/bold]")
    seed_split_matrix(*sheets["split_matrix"])
    seed_persons(*sheets["persons"])
    seed_deductible(*sheets["beihilfe_deductible"])

    console.rule("[bold green]Setup complete")
    console.print(f"https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}")


if __name__ == "__main__":
    main()
