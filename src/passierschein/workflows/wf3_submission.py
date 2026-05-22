"""
WF-3: Claim Submission

Record submission dates on invoices (PKV, Beihilfe, or both).
No separate claim entity — submission is tracked directly on the invoice.
"""
from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..adapters.sheets import SheetsRepository
from ..domain.enums import ClaimStatus, SplitType

console = Console()


def run(
    invoice_ids: List[str],
    side: Literal["pkv", "beihilfe", "both"],
    submitted_on: Optional[date] = None,
    repo: SheetsRepository | None = None,
) -> None:
    repo = repo or SheetsRepository()
    console.rule(f"[bold blue]WF-3 Claim Submission — {side.upper()}")

    submitted_on = submitted_on or date.fromisoformat(
        Prompt.ask("Submission date (YYYY-MM-DD)", default=date.today().isoformat())
    )

    invoices = [repo.invoices.get_by_id(iid) for iid in invoice_ids]
    invoices = [inv for inv in invoices if inv is not None]

    if not invoices:
        console.print("[red]No matching invoices found.[/red]")
        return

    t = Table(title="Invoices to submit", show_lines=True)
    t.add_column("Invoice ID")
    t.add_column("Provider")
    t.add_column("PKV expected (€)", justify="right")
    t.add_column("Beihilfe expected (€)", justify="right")
    for inv in invoices:
        t.add_row(
            inv.id[:8] + "…", inv.provider,
            f"{inv.pkv_expected:,.2f}",
            f"{inv.beihilfe_expected:,.2f}",
        )
    console.print(t)

    if not Confirm.ask(f"Submit {len(invoices)} invoice(s) ({side.upper()}) on {submitted_on}?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        return

    for inv in invoices:
        if side in ("pkv", "both") and inv.pkv_claim_status != ClaimStatus.NOT_APPLICABLE:
            inv.pkv_submitted_at  = submitted_on
            inv.pkv_claim_status  = ClaimStatus.CLAIMED
        if side in ("beihilfe", "both"):
            inv.beihilfe_submitted_at = submitted_on
            inv.beihilfe_claim_status = ClaimStatus.CLAIMED
        repo.invoices.update(inv)

    console.print(f"[bold green]✓ {len(invoices)} invoice(s) marked as submitted ({side.upper()}).[/bold green]")
