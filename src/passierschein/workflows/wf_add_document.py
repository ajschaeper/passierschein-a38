"""
WF-ADD-DOC: Capture a document.

Creates a Document record (status=pending), then optionally routes to:
  - add-invoice  (document_type == invoice)
  - add-settlement-report  (document_type == settlement_report)

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


def run(repo: SheetsRepository | None = None) -> Document:
    repo = repo or SheetsRepository()

    fields   = enter_document()
    document = Document(**fields)

    repo.documents.append(document)
    console.print(f"[bold green]✓ Document {document.id[:8]}… captured (status: pending).[/bold green]")

    if document.document_type == DocumentType.INVOICE:
        if Confirm.ask("Proceed to add-invoice now?", default=True):
            from . import wf1_intake
            invoice = wf1_intake.run(repo=repo, document_id=document.id)
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
