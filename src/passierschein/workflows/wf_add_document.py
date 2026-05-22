"""
WF-ADD-DOC: Capture a document.

Creates a Document record (status=pending), runs OCR if the document is an
invoice, then optionally routes to add-invoice or add-settlement-report.
The document_id is threaded into the subsequent workflow so the invoice or
report keeps a reference to the source file.
"""
from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm

from ..adapters.manual_entry import enter_document
from ..adapters.sheets import SheetsRepository
from ..domain.enums import DocumentStatus, DocumentType
from ..domain.models import Document

console = Console()


def _try_ocr(file_path: str) -> dict:
    """Run Claude OCR on the file; return extracted hints or {} on failure."""
    try:
        from ..adapters.claude_ocr import extract_invoice_data
        console.print("[dim]Running OCR…[/dim]")
        hints = extract_invoice_data(file_path)
        console.print(
            f"[dim]OCR extracted: "
            f"provider=[bold]{hints.get('provider', '?')}[/bold]  "
            f"amount=€{hints.get('total_amount', '?')}  "
            f"date={hints.get('date_of_service', '?')}[/dim]"
        )
        return hints
    except FileNotFoundError as exc:
        console.print(f"[red]File not found: {exc}[/red]")
        return {}
    except Exception as exc:
        console.print(f"[yellow]OCR failed ({exc}) — continuing with manual entry.[/yellow]")
        return {}


def run(repo: SheetsRepository | None = None) -> Document:
    repo = repo or SheetsRepository()

    fields   = enter_document()
    document = Document(**fields)

    repo.documents.append(document)
    console.print(f"[bold green]✓ Document {document.id[:8]}… captured (status: pending).[/bold green]")

    if document.document_type == DocumentType.INVOICE:
        ocr_hints = _try_ocr(document.file_path)

        if Confirm.ask("Proceed to add-invoice now?", default=True):
            from . import wf1_intake
            invoice = wf1_intake.run(repo=repo, document_id=document.id, ocr_hints=ocr_hints)
            document.linked_entity_id = invoice.id
            document.status           = DocumentStatus.PROCESSED
            repo.documents.update(document)
            console.print(f"[dim]Document linked to invoice {invoice.id[:8]}…[/dim]")

    elif document.document_type == DocumentType.SETTLEMENT_REPORT:
        if Confirm.ask("Proceed to add-settlement-report now?", default=True):
            from . import wf4_matching
            report = wf4_matching.process_report(repo=repo, document_id=document.id)
            document.linked_entity_id = report.id
            document.status           = DocumentStatus.PROCESSED
            repo.documents.update(document)
            console.print(f"[dim]Document linked to report {report.id[:8]}…[/dim]")

    return document
