"""
WF-ADD-DOC: Capture a document, run OCR, route to invoice or settlement report.

Flow:
  1. Drive file picker (or local --file)
  2. OCR — auto-detects type
  3. Route based on detected type (no user prompt needed):
       invoice          → wf1_intake  → Document + Invoice
       settlement_report → wf4_matching → Document + Report + LineItems
  4. Mark Document as processed and link to created entity
"""
from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.prompt import Prompt

from ..adapters.drive import list_files as _list_drive_files
from ..adapters.manual_entry import enter_document
from ..adapters.sheets import SheetsRepository
from ..domain.enums import DocumentStatus, DocumentType
from ..domain.models import Document

console = Console()


def _try_ocr(file_path: str) -> dict:
    """Auto-detect and extract. Returns {} on failure."""
    try:
        from ..adapters.claude_ocr import extract_document_data
        console.print("[dim]Running OCR…[/dim]")
        hints = extract_document_data(file_path)
        if hints.get("document_type") == "settlement_report":
            n = len(hints.get("line_items") or [])
            console.print(
                f"[dim]→ Settlement report: "
                f"ref=[bold]{hints.get('report_reference', '?')}[/bold]  "
                f"total=€{hints.get('total_granted', '?')}  "
                f"{n} line item(s)[/dim]"
            )
        else:
            console.print(
                f"[dim]→ Invoice: "
                f"provider=[bold]{hints.get('provider', '?')}[/bold]  "
                f"amount=€{hints.get('total_amount', '?')}  "
                f"date={hints.get('date_of_service', '?')}[/dim]"
            )
        return hints
    except FileNotFoundError as exc:
        console.print(f"[red]File not found: {exc}[/red]")
        return {}
    except Exception as exc:
        console.print(f"[yellow]OCR failed ({exc}) — will ask for type manually.[/yellow]")
        return {}


def _resolve_type(document: Document, ocr_hints: dict) -> str:
    """
    Return 'invoice' or 'settlement_report'.

    Priority: OCR result > picker folder inference > explicit user prompt.
    """
    # 1. OCR told us
    ocr_type = ocr_hints.get("document_type")
    if ocr_type in ("invoice", "settlement_report"):
        return ocr_type

    # 2. Picker inferred from Drive folder name
    doc_type = str(document.document_type)
    if doc_type in ("invoice", "settlement_report"):
        return doc_type

    # 3. Ask
    raw = Prompt.ask("Document type", choices=["invoice", "settlement_report"], default="invoice")
    return raw


def run(
    repo: SheetsRepository | None = None,
    local_file: str | None = None,
    dry_run: bool = False,
) -> Document:
    """
    Full add-document flow. Call from the CLI add-document command.
    """
    repo = repo or SheetsRepository()

    # ── 1. Pick file ──────────────────────────────────────────────────────────
    if local_file:
        fields = dict(
            file_path=local_file,
            document_type=DocumentType.UNKNOWN,
            captured_at=date.today(),
        )
    else:
        try:
            drive_files = _list_drive_files()
        except Exception as exc:
            console.print(f"[dim]Drive listing unavailable ({exc}) — manual path entry.[/dim]")
            drive_files = []
        fields = enter_document(drive_files=drive_files or None)

    document = Document(**fields)
    repo.documents.append(document)
    console.print(f"[bold green]✓ Document {document.id[:8]}… captured.[/bold green]")

    # ── 2. OCR ────────────────────────────────────────────────────────────────
    ocr_hints = _try_ocr(document.file_path) if document.file_path else {}

    # ── 3. Route ──────────────────────────────────────────────────────────────
    doc_type = _resolve_type(document, ocr_hints)

    if doc_type == "settlement_report":
        from . import wf4_matching
        report = wf4_matching.process_report(repo=repo, document_id=document.id, ocr_hints=ocr_hints)
        if not dry_run:
            document.linked_entity_id = report.id
            document.status = DocumentStatus.PROCESSED
            repo.documents.update(document)
    else:
        from . import wf1_intake
        invoice = wf1_intake.run(repo=repo, dry_run=dry_run, document_id=document.id, ocr_hints=ocr_hints)
        if not dry_run:
            document.linked_entity_id = invoice.id
            document.status = DocumentStatus.PROCESSED
            repo.documents.update(document)

    return document
