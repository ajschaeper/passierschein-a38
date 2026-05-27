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

from ..adapters.drive import list_files as _list_drive_files, move_to_archive as _move_to_archive
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

    Priority: OCR result (with confidence) > picker folder inference > explicit user prompt.
    High confidence  → auto-accept, just print.
    Medium confidence → show detected type, ask Y/n to confirm.
    Low / absent OCR  → fall through to folder inference or explicit prompt.
    """
    from rich.prompt import Confirm

    ocr_type = ocr_hints.get("document_type")
    if ocr_type in ("invoice", "settlement_report"):
        confidence_map = ocr_hints.get("_confidence")
        # absent field inside the map = high; absent map entirely = medium (safe fallback)
        confidence = confidence_map.get("document_type", "high") if confidence_map is not None else "medium"
        if confidence == "high":
            console.print(f"  Document type [dim](OCR ✓)[/dim]: [bold]{ocr_type}[/bold]")
            return ocr_type
        if confidence == "medium":
            console.print(f"  Document type [dim](OCR ~)[/dim]: [bold]{ocr_type}[/bold]")
            if Confirm.ask(f"Classify as {ocr_type}?", default=True):
                return ocr_type
        # low → fall through

    # Folder inference (from Drive picker subfolder name — already high-confidence)
    doc_type = str(document.document_type)
    if doc_type in ("invoice", "settlement_report"):
        console.print(f"  Document type [dim](folder ✓)[/dim]: [bold]{doc_type}[/bold]")
        return doc_type

    # Explicit prompt as last resort
    return Prompt.ask("Document type", choices=["invoice", "settlement_report"], default="invoice")


def run(
    repo: SheetsRepository | None = None,
    local_file: str | None = None,
    dry_run: bool = False,
    _session_invoices: list | None = None,
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
            _archive(document.file_path, f"settlement_{report.type}", ocr_hints, report.received_at)
    else:
        from . import wf1_intake
        invoice = wf1_intake.run(
            repo=repo, dry_run=dry_run, document_id=document.id,
            ocr_hints=ocr_hints, _session_invoices=_session_invoices,
        )
        if not dry_run and invoice is not None:
            document.linked_entity_id = invoice.id
            document.status = DocumentStatus.PROCESSED
            repo.documents.update(document)
            _archive(document.file_path, "invoice", ocr_hints, invoice.date_of_service)

    return document


def run_batch(repo: SheetsRepository | None = None) -> list[Document]:
    """Process every file currently in the Drive Inbox in sequence."""
    repo = repo or SheetsRepository()
    try:
        drive_files = _list_drive_files()
    except Exception as exc:
        console.print(f"[red]Could not list Drive Inbox: {exc}[/red]")
        return []

    if not drive_files:
        console.print("[yellow]Inbox is empty — nothing to import.[/yellow]")
        return []

    console.rule(f"[bold blue]Batch import — {len(drive_files)} file(s) in Inbox")
    results: list[Document] = []
    session_invoices: list = []  # invoices created this session — feeds dedup across files
    for i, f in enumerate(drive_files, 1):
        console.rule(f"[dim]{i}/{len(drive_files)}: {f['name']}")
        try:
            doc = run(repo=repo, local_file=f["id"], _session_invoices=session_invoices)
            results.append(doc)
        except Exception as exc:
            console.print(f"[red]Failed: {exc}[/red]")

    console.rule(f"[bold green]Batch complete — {len(results)}/{len(drive_files)} processed")
    return results


def _archive(file_path: str, entity_type: str, ocr_hints: dict, service_date: object) -> None:
    """Move file from Drive Inbox to the correct year/type subfolder."""
    from ..adapters.drive import is_drive_id
    if not is_drive_id(file_path):
        return  # local file, nothing to move

    # Determine year from service date, fall back to OCR hints, fall back to today
    year: int | None = None
    if hasattr(service_date, "year"):
        year = service_date.year
    if not year:
        for key in ("date_of_service", "invoice_date", "date"):
            raw = ocr_hints.get(key)
            if raw and len(str(raw)) >= 4:
                try:
                    year = int(str(raw)[:4])
                    break
                except ValueError:
                    pass
    if not year:
        from datetime import date
        year = date.today().year

    ok = _move_to_archive(file_path, year, entity_type)
    if ok:
        console.print(f"[dim]Moved to archive: {year}/{entity_type}[/dim]")
    else:
        console.print(f"[dim]Could not move to archive (folder not found for {year}/{entity_type}).[/dim]")
