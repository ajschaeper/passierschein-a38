"""
WF-IMPORT-PAYMENTS: Bulk-import bank statement (C24 CSV) → Payment rows.

Flow:
  1. Parse CSV — C24 format, auto-detects encoding + delimiter + preamble
  2. Deduplicate against already-imported payments (import_fingerprint)
  3. Show numbered preview table.
     C24 already categorises transactions; non-health rows are pre-suggested
     as out-of-scope (marked with [dim] in the table).
  4. User can adjust the out-of-scope selection (add/remove row numbers).
  5. Confirm and bulk-save — out-of-scope rows saved with match_status=out_of_scope,
     the rest as unmatched.
  6. Offer to run match-payments immediately.
"""
from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..adapters.sheets import SheetsRepository
from ..domain.enums import PaymentMatchStatus
from ..domain.models import Payment

console = Console()


def _parse_row_numbers(raw: str, max_idx: int) -> set[int]:
    """Parse '1,3,5-8,10' into a set of 0-based indices. Out-of-range numbers ignored."""
    result: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                for n in range(int(lo), int(hi) + 1):
                    if 1 <= n <= max_idx:
                        result.add(n - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if 1 <= n <= max_idx:
                    result.add(n - 1)
            except ValueError:
                pass
    return result


def run(repo: SheetsRepository | None = None, csv_file: str | None = None) -> list[Payment]:
    """
    Import payments from a C24 CSV file.

    Returns the list of newly created Payment objects.
    """
    from ..adapters.bank_csv import parse_c24

    repo = repo or SheetsRepository()

    if not csv_file:
        csv_file = Prompt.ask("Path to C24 CSV export")

    console.print(f"[dim]Parsing [bold]{csv_file}[/bold]…[/dim]")
    try:
        rows = parse_c24(csv_file)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return []

    if not rows:
        console.print("[yellow]No transactions found in file.[/yellow]")
        return []

    # ── Deduplicate ───────────────────────────────────────────────────────────
    existing     = repo.payments.get_all()
    existing_fps = {p.import_fingerprint for p in existing if p.import_fingerprint}

    new_rows = [r for r in rows if r["import_fingerprint"] not in existing_fps]
    skipped  = len(rows) - len(new_rows)

    console.print(
        f"  Found [bold]{len(rows)}[/bold] transactions — "
        f"[bold green]{len(new_rows)}[/bold green] new, "
        f"[dim]{skipped} already imported (skipped)[/dim]"
    )

    if not new_rows:
        console.print("[green]Nothing new to import.[/green]")
        return []

    # ── Pre-select out-of-scope based on C24 category ─────────────────────────
    # Rows NOT flagged as health-related are pre-suggested as out-of-scope.
    suggested_oos = {i for i, r in enumerate(new_rows) if not r["is_medical_hint"]}

    # ── Preview table ─────────────────────────────────────────────────────────
    t = Table(title=f"New transactions ({len(new_rows)})  — [dim]greyed = pre-suggested out-of-scope[/dim]", show_lines=True)
    t.add_column("#",            style="bold", justify="right")
    t.add_column("Date")
    t.add_column("Dir",          justify="center")
    t.add_column("Amount (€)",   justify="right")
    t.add_column("Counterparty")
    t.add_column("Kategorie")
    t.add_column("Verwendungszweck")

    for i, r in enumerate(new_rows):
        oos = i in suggested_oos
        dim = "[dim]" if oos else ""
        end = "[/dim]" if oos else ""
        dir_label = (
            f"{dim}[red]OUT[/red]{end}" if str(r["direction"]) == "outbound"
            else f"{dim}[green]IN[/green]{end}"
        )
        t.add_row(
            f"{dim}{i + 1}{end}",
            f"{dim}{r['date']}{end}",
            dir_label,
            f"{dim}{r['amount']:,.2f}{end}",
            f"{dim}{(r['counterparty'] or '')[:30]}{end}",
            f"{dim}{(r['kategorie'] or '')[:20]}{end}",
            f"{dim}{(r['bank_reference'] or '')[:40]}{end}",
        )
    console.print(t)

    n_health = len(new_rows) - len(suggested_oos)
    console.print(
        f"  [dim]{n_health} health transaction(s) queued for matching, "
        f"{len(suggested_oos)} pre-marked out-of-scope based on C24 category.[/dim]"
    )

    # ── Let user adjust ───────────────────────────────────────────────────────
    console.print("[dim]Adjust if needed — enter row numbers to TOGGLE (add to or remove from out-of-scope).[/dim]")
    raw_toggle = Prompt.ask(
        "Toggle row #s (e.g. 3,7,10-12 — or Enter to accept as shown)",
        default="",
    )
    oos_indices = set(suggested_oos)
    if raw_toggle.strip():
        to_toggle = _parse_row_numbers(raw_toggle, len(new_rows))
        for idx in to_toggle:
            if idx in oos_indices:
                oos_indices.discard(idx)   # was OOS, remove → will be matched
            else:
                oos_indices.add(idx)       # was health, add → dismiss
        console.print(
            f"  [dim]→ {len(new_rows) - len(oos_indices)} to match, "
            f"{len(oos_indices)} out-of-scope[/dim]"
        )

    # ── Confirm ───────────────────────────────────────────────────────────────
    to_match = len(new_rows) - len(oos_indices)
    if not Confirm.ask(
        f"Import {len(new_rows)} transaction(s) "
        f"({to_match} to match, {len(oos_indices)} out-of-scope)?",
        default=True,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return []

    # ── Bulk save — single API call to avoid Sheets write-quota (429) ─────────
    created: list[Payment] = [
        Payment(
            direction          = r["direction"],
            date               = r["date"],
            amount             = r["amount"],
            counterparty       = r["counterparty"],
            bank_reference     = r["bank_reference"],
            import_fingerprint = r["import_fingerprint"],
            match_status       = PaymentMatchStatus.OUT_OF_SCOPE if i in oos_indices
                                 else PaymentMatchStatus.UNMATCHED,
        )
        for i, r in enumerate(new_rows)
    ]
    repo.payments.append_many(created)

    oos_saved  = sum(1 for p in created if str(p.match_status) == "out_of_scope")
    need_match = len(created) - oos_saved
    console.print(
        f"[bold green]✓ Imported {len(created)} payment(s)[/bold green]  "
        f"[dim]({need_match} to match, {oos_saved} out-of-scope)[/dim]"
    )

    # ── Offer to match ────────────────────────────────────────────────────────
    if need_match and Confirm.ask("Run match-payments now?", default=True):
        from . import wf_match_payment
        wf_match_payment.run(repo=repo)

    return created
