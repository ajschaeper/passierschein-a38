"""
Manual entry adapter — temporary replacement for OCR.

Provides interactive terminal prompts for every entity with:
  - Plausible, realistic German medical defaults for strings and amounts
  - Today / recent dates as defaults
  - Random enum suggestions the user can simply accept
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Type, TypeVar

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()
E = TypeVar("E", bound=Enum)


# ---------------------------------------------------------------------------
# Realistic suggestion pools
# ---------------------------------------------------------------------------

_PROVIDERS = [
    "Praxis Dr. Müller, Allgemeinmedizin",
    "Praxis Dr. Weber, Innere Medizin",
    "Praxis Dr. Schmidt, Gynäkologie",
    "Praxis Dr. Bauer, Pädiatrie",
    "Praxis Dr. Fischer, Orthopädie",
    "Städtisches Klinikum München",
    "Universitätsklinikum Heidelberg",
    "Helios Klinikum Berlin-Buch",
    "Zahnarztpraxis Schmidt & Partner",
    "Kieferorthopädie Zentrum Berlin",
    "Physiotherapie am Markt",
    "Apotheke am Rathaus",
    "Neurologie Praxis Dr. Klein",
    "Psychotherapeutische Praxis Dr. Hoffmann",
    "MVZ Augenheilkunde Mitte",
]

_AMOUNT_RANGES: dict[str, tuple[float, float]] = {
    "ambulant":      (45.0,   350.0),
    "stationaer":    (800.0,  80000.0),
    "dental_basic":  (80.0,   600.0),
    "zahnersatz":    (400.0,  4000.0),
    "kfo":           (200.0,  3000.0),
    "psychotherapy": (100.0,  250.0),
    "hilfsmittel":   (30.0,   500.0),
    "arzneimittel":  (8.0,    200.0),
    "heilmittel":    (40.0,   300.0),
    "other":         (50.0,   1000.0),
}

_BANK_REFS = [
    "BEIH-2026-001234",
    "PKV-ERSTATTUNG-2026-05",
    "BHV-BESCHEID-0042",
    "TRANSFER-REF-20260515",
]


# ---------------------------------------------------------------------------
# Generic prompt helpers
# ---------------------------------------------------------------------------

def prompt_str(label: str, default: Optional[str] = None) -> str:
    return Prompt.ask(label, default=default or "")


def prompt_date(label: str, default: Optional[date] = None) -> date:
    d = default or date.today()
    raw = Prompt.ask(label, default=d.isoformat())
    return date.fromisoformat(raw)


def prompt_amount(label: str, default: Optional[Decimal] = None) -> Decimal:
    val = Prompt.ask(label, default=str(default or Decimal("0.00")))
    return Decimal(val.replace(",", "."))


def prompt_enum(label: str, enum_cls: Type[E], default: Optional[E] = None) -> E:
    """Show all enum values with a random (or given) default."""
    members = list(enum_cls)
    suggestion = default or random.choice(members)
    console.print(f"  [dim]Options: {', '.join(m.value for m in members)}[/dim]")
    raw = Prompt.ask(label, default=suggestion.value)
    return enum_cls(raw)


def prompt_bool(label: str, default: bool = False) -> bool:
    return Confirm.ask(label, default=default)


def prompt_optional_date(label: str, default: Optional[date] = None) -> Optional[date]:
    raw = Prompt.ask(label + " (leave blank to skip)", default=default.isoformat() if default else "")
    return date.fromisoformat(raw) if raw else None


# ---------------------------------------------------------------------------
# Entity-level entry forms
# ---------------------------------------------------------------------------

def enter_person() -> dict:
    console.rule("[bold cyan]New Person")
    first_name  = prompt_str("First name", default="Anna")
    family_name = prompt_str("Family name", default="Mustermann")
    birth_date  = prompt_date("Birth date", default=date(1985, 6, 15))
    role        = prompt_enum("Role", __import__(
        "passierschein.domain.enums", fromlist=["PatientRole"]
    ).PatientRole, default=__import__(
        "passierschein.domain.enums", fromlist=["PatientRole"]
    ).PatientRole.EMPLOYEE)
    return dict(first_name=first_name, family_name=family_name,
                birth_date=birth_date, role=role)


def _parse_ocr_date(raw: Optional[str]) -> Optional[date]:
    """Parse YYYY-MM-DD or YYYY-MM from OCR output."""
    if not raw:
        return None
    try:
        if len(raw) == 7:          # YYYY-MM → use first of month
            return date.fromisoformat(raw + "-01")
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _match_person_by_name(patient_name: str, persons: list) -> Optional[int]:
    """Return 0-based index of the best-matching person, or None."""
    name_lower = patient_name.lower()
    for i, p in enumerate(persons):
        if p.first_name.lower() in name_lower or p.family_name.lower() in name_lower:
            return i
    return None


def _conf(ocr: dict, field: str) -> str:
    """
    Return OCR confidence for a field: 'high', 'medium', or 'low'.

    Convention: Claude only populates _confidence for fields that are NOT high,
    so absence of a field inside _confidence means high confidence.

    However, if _confidence is entirely missing from the OCR result (truncated
    response, old model), we fall back to 'medium' rather than 'high' so nothing
    is silently auto-accepted.
    """
    confidence_map = ocr.get("_confidence")
    if confidence_map is None:
        # _confidence block absent entirely → treat all fields as medium
        return "medium"
    return confidence_map.get(field, "high")


def _auto(ocr: dict, field: str, value: object) -> bool:
    """True when the field has a value and OCR confidence is high → auto-accept."""
    return value is not None and value != "" and _conf(ocr, field) == "high"


def enter_invoice(persons: list, two_plus_children: bool, ocr_hints: dict | None = None) -> dict:
    """Interactive form for a new invoice. Returns a dict of field values.

    High-confidence OCR fields are auto-accepted and shown but not prompted.
    Medium/low-confidence fields are prompted with the OCR value as default.
    """
    from passierschein.domain.enums import SplitType, PatientRole
    from passierschein.domain.split_matrix import matrix

    ocr = ocr_hints or {}
    ocr_tag = " [dim](OCR)[/dim]" if ocr else ""

    console.rule("[bold cyan]New Invoice")

    # Person selection — try to auto-match from OCR patient name
    if not persons:
        raise ValueError("No persons configured. Run setup first.")

    ocr_person_idx = None
    if ocr.get("patient_name"):
        ocr_person_idx = _match_person_by_name(ocr["patient_name"], persons)

    # Auto-select when OCR patient_name confidence is high and a match was found
    if ocr_person_idx is not None and _auto(ocr, "patient_name", ocr.get("patient_name")):
        person = persons[ocr_person_idx]
        console.print(
            f"  Person [dim](OCR ✓)[/dim]: [bold]{person.first_name} {person.family_name}[/bold]"
            f"  [dim]({person.role})[/dim]"
        )
    else:
        t = Table(show_lines=False, box=None)
        t.add_column("#")
        t.add_column("Name")
        t.add_column("Role")
        for i, p in enumerate(persons, 1):
            marker = " ← OCR match" if ocr_person_idx is not None and i - 1 == ocr_person_idx else ""
            t.add_row(str(i), f"{p.first_name} {p.family_name}{marker}", str(p.role))
        console.print(t)
        default_person = str((ocr_person_idx or 0) + 1)
        idx    = int(Prompt.ask("Select person", default=default_person)) - 1
        person = persons[idx]

    # Split type — from OCR hint
    # direct_billing is a deprecated alias → normalise to beihilfe_only
    split_type_map = {
        "classic":        SplitType.CLASSIC,
        "beihilfe_only":  SplitType.BEIHILFE_ONLY,
        "direct_billing": SplitType.BEIHILFE_ONLY,  # legacy OCR hint
    }
    default_split = split_type_map.get(ocr.get("split_type_hint", ""), SplitType.CLASSIC)
    if _auto(ocr, "split_type_hint", ocr.get("split_type_hint")):
        split_type = default_split
        console.print(f"  Split type [dim](OCR ✓)[/dim]: {split_type.value}")
    else:
        # Only show the two active options — direct_billing is deprecated
        _ACTIVE_SPLIT_TYPES = [SplitType.CLASSIC, SplitType.BEIHILFE_ONLY]
        console.print(f"  [dim]Options: {', '.join(t.value for t in _ACTIVE_SPLIT_TYPES)}[/dim]")
        from rich.prompt import Prompt as _Prompt
        raw = _Prompt.ask(f"Split type{ocr_tag}", default=default_split.value)
        split_type = SplitType(raw)

    # Provider
    default_provider = ocr.get("provider") or random.choice(_PROVIDERS)
    if _auto(ocr, "provider", ocr.get("provider")):
        provider = default_provider
        console.print(f"  Provider [dim](OCR ✓)[/dim]: {provider}")
    else:
        provider = Prompt.ask(f"Provider{ocr_tag if ocr.get('provider') else ''}", default=default_provider)

    # Dates
    ocr_service_date  = _parse_ocr_date(ocr.get("date_of_service"))
    ocr_received_date = _parse_ocr_date(ocr.get("date_of_invoice"))
    ocr_due_date      = _parse_ocr_date(ocr.get("due_date"))

    if _auto(ocr, "date_of_service", ocr_service_date):
        date_of_service = ocr_service_date
        console.print(f"  Date of service [dim](OCR ✓)[/dim]: {date_of_service}")
    else:
        date_of_service = prompt_date(
            f"Date of service{ocr_tag if ocr_service_date else ''}",
            default=ocr_service_date or (date.today() - timedelta(days=random.randint(3, 30))),
        )

    if _auto(ocr, "date_of_invoice", ocr_received_date):
        date_received = ocr_received_date
        console.print(f"  Date received [dim](OCR ✓)[/dim]: {date_received}")
    else:
        date_received = prompt_date(
            f"Date received{ocr_tag if ocr_received_date else ''}",
            default=ocr_received_date or (date_of_service + timedelta(days=random.randint(1, 10))),
        )

    if _auto(ocr, "due_date", ocr_due_date):
        due_date = ocr_due_date
        console.print(f"  Due date [dim](OCR ✓)[/dim]: {due_date}")
    else:
        due_date = prompt_optional_date(
            f"Due date (payment deadline){ocr_tag if ocr_due_date else ''}",
            default=ocr_due_date or (date_received + timedelta(days=30)),
        )

    # Amount
    from passierschein.adapters.claude_ocr import parse_german_amount
    ocr_amount = parse_german_amount(str(ocr.get("total_amount", "") or "")) if ocr.get("total_amount") else None
    default_amount = ocr_amount or Decimal(str(round(random.uniform(50.0, 500.0), 2)))
    if _auto(ocr, "total_amount", ocr_amount):
        total_amount = ocr_amount
        console.print(f"  Total amount [dim](OCR ✓)[/dim]: €{total_amount}")
    else:
        total_amount = prompt_amount(
            f"Total amount (EUR){ocr_tag if ocr_amount else ''}",
            default=default_amount,
        )

    # Split resolution
    role  = PatientRole(person.role)
    split = matrix.resolve(role, split_type, two_plus_children=two_plus_children)

    pkv_expected      = cents(total_amount * split.pkv_pct)
    beihilfe_expected = cents(total_amount * split.beihilfe_pct)

    if split_type == SplitType.DIRECT_BILLING:
        # Invoice is already the Beihilfe portion; PKV billed separately
        employee_net_expected = total_amount
        console.print(
            f"\n[bold]Direct billing[/bold] — 100% Beihilfe: €{beihilfe_expected}  "
            f"[dim](PKV billed separately)[/dim]"
        )
    else:
        employee_net_expected = total_amount
        console.print(
            f"\n[bold]Split:[/bold] PKV {split.pkv_pct:.0%} = €{pkv_expected}  |  "
            f"Beihilfe {split.beihilfe_pct:.0%} = €{beihilfe_expected}"
        )

    return dict(
        person_id             = person.person_id,
        split_type            = split_type,
        provider              = provider,
        date_of_service       = date_of_service,
        date_received         = date_received,
        due_date              = due_date,
        total_amount          = total_amount,
        employee_net_expected = employee_net_expected,
        pkv_share_pct         = split.pkv_pct,
        pkv_expected          = pkv_expected,
        beihilfe_share_pct    = split.beihilfe_pct,
        beihilfe_expected     = beihilfe_expected,
    )


def enter_settlement_report(
    document_id: Optional[str] = None,
    ocr_hints: Optional[dict] = None,
) -> dict:
    from passierschein.domain.enums import PaymentStatus, SettlementReportLineItemsStatus
    from passierschein.adapters.claude_ocr import parse_german_amount

    ocr = ocr_hints or {}
    ocr_tag = " [dim](OCR)[/dim]" if ocr else ""

    console.rule("[bold cyan]New Settlement Report")

    # Show OCR line items summary if available
    if ocr.get("line_items"):
        items = ocr["line_items"]
        t = Table(title=f"OCR extracted {len(items)} line item(s)", show_lines=False, box=None)
        t.add_column("Beleg"); t.add_column("Person"); t.add_column("Description")
        t.add_column("Total", justify="right"); t.add_column("%", justify="right")
        t.add_column("Granted", justify="right")
        for li in items:
            t.add_row(
                str(li.get("beleg_nr", "")),
                str(li.get("person", "")),
                (li.get("description") or "")[:35],
                f"€{li.get('total_amount', '?')}",
                f"{li.get('beihilfe_pct', '?')}%",
                f"€{li.get('amount_granted', '?')}",
            )
        console.print(t)

    default_type = (ocr.get("report_type") or random.choice(["pkv", "beihilfe"]))
    rtype = Prompt.ask(f"Type (pkv/beihilfe){ocr_tag if ocr.get('report_type') else ''}", default=default_type)

    default_ref = ocr.get("report_reference") or f"REF-{random.randint(10000,99999)}"
    ref = Prompt.ask(f"Report reference{ocr_tag if ocr.get('report_reference') else ''}", default=default_ref)

    ocr_date = _parse_ocr_date(ocr.get("date"))
    received = prompt_date(f"Received at{ocr_tag if ocr_date else ''}", default=ocr_date or date.today())

    ocr_total = parse_german_amount(str(ocr.get("total_granted") or "")) if ocr.get("total_granted") else None
    total = prompt_amount(
        f"Total reimbursed (EUR){ocr_tag if ocr_total else ''}",
        default=ocr_total or Decimal("0.00"),
    )

    return dict(
        type=rtype, report_reference=ref, received_at=received,
        total_reimbursed=total, document_id=document_id,
    )


def enter_settlement_line_item(
    settlement_report_id: str,
    invoice_id: str,
    item_type: str,
    billed_amount: Decimal,
) -> dict:
    from passierschein.domain.enums import SettlementLineItemStatus
    console.rule(f"[bold cyan]Settlement Line Item — {item_type.upper()}")
    console.print(f"Invoice: [bold]{invoice_id[:8]}…[/bold]  |  Billed: €{billed_amount}")

    eligible   = prompt_amount("Eligible amount", default=billed_amount)
    reimbursed = prompt_amount("Reimbursed amount", default=eligible)
    not_covered   = prompt_amount("Not covered amount", default=Decimal("0.00"))
    rate_cap      = prompt_amount("Rate cap reduction", default=Decimal("0.00"))
    deductible    = prompt_amount("Deductible applied (Beihilfe only)", default=Decimal("0.00"))
    reasons       = Prompt.ask("Rejection reasons (leave blank if none)", default="")
    status        = prompt_enum("Status", SettlementLineItemStatus,
                                default=SettlementLineItemStatus.COVERED)

    return dict(
        settlement_report_id = settlement_report_id,
        invoice_id           = invoice_id,
        type                 = item_type,
        billed_amount        = billed_amount,
        eligible_amount      = eligible,
        reimbursed_amount    = reimbursed,
        not_covered_amount   = not_covered,
        rate_cap_reduction   = rate_cap,
        deductible_applied   = deductible,
        rejection_reasons    = reasons or None,
        status               = status,
    )


def enter_payment(
    direction: str,
    suggested_amount: Decimal,
    linked_report_id: Optional[str] = None,
) -> dict:
    """Prompt for a payment that is already matched to a known entity."""
    from passierschein.domain.enums import PaymentMatchStatus
    console.rule(f"[bold cyan]Payment — {direction.upper()}")
    pay_date     = prompt_date("Date", default=date.today())
    amount       = prompt_amount("Amount (EUR)", default=suggested_amount)
    bank_ref     = Prompt.ask("Bank reference", default=random.choice(_BANK_REFS))
    counterparty = Prompt.ask("Counterparty (bank description, leave blank to skip)", default="")
    return dict(
        direction            = direction,
        date                 = pay_date,
        amount               = amount,
        bank_reference       = bank_ref,
        counterparty         = counterparty or None,
        match_status         = PaymentMatchStatus.MATCHED,
        settlement_report_id = linked_report_id,
    )


def enter_bank_transaction() -> dict:
    """Prompt for an unmatched bank transaction (no entity linked yet)."""
    from passierschein.domain.enums import PaymentDirection, PaymentMatchStatus
    console.rule("[bold cyan]New Bank Transaction")
    direction    = prompt_enum("Direction", PaymentDirection)
    pay_date     = prompt_date("Date", default=date.today())
    amount       = prompt_amount("Amount (EUR)", default=Decimal("0.00"))
    counterparty = Prompt.ask("Counterparty (bank description)")
    bank_ref     = Prompt.ask("Bank reference (leave blank to skip)", default="")
    return dict(
        direction    = direction,
        date         = pay_date,
        amount       = amount,
        counterparty = counterparty or None,
        bank_reference = bank_ref or None,
        match_status   = PaymentMatchStatus.UNMATCHED,
    )


def enter_document(drive_files: list[dict] | None = None) -> dict:
    """Prompt for a new document record. Shows a Drive file picker if files are provided.

    When a Drive file is selected:
    - captured_at is set automatically from the file's createdTime (no prompt)
    - document_type is auto-accepted when inferred from a recognised subfolder name
    """
    from passierschein.domain.enums import DocumentType
    console.rule("[bold cyan]New Document")

    file_path:       str              = ""
    default_type:    DocumentType     = DocumentType.UNKNOWN
    captured:        date | None      = None
    type_confident:  bool             = False

    if drive_files:
        t = Table(title="Files in Google Drive Inbox", show_lines=True)
        t.add_column("#", style="bold", justify="right")
        t.add_column("File name")
        t.add_column("Folder")
        t.add_column("Date", justify="right")
        for i, f in enumerate(drive_files, 1):
            t.add_row(str(i), f["name"], f.get("folder_path") or "", (f.get("createdTime") or "")[:10])
        t.add_row("[dim]0[/dim]", "[dim]Enter local path manually[/dim]", "", "")
        console.print(t)

        raw = Prompt.ask("Pick a file # (or 0 for manual)", default="1")
        try:
            idx = int(raw)
            if 1 <= idx <= len(drive_files):
                chosen      = drive_files[idx - 1]
                file_path   = chosen["id"]

                # captured_at from Drive createdTime
                created_str = chosen.get("createdTime") or ""
                if created_str:
                    captured = date.fromisoformat(created_str[:10])
                    console.print(f"  Captured at [dim](Drive ✓)[/dim]: {captured}")

                # Type inference from subfolder — confident only for known folder names
                folder_path = (chosen.get("folder_path") or "").lower()
                name_lower  = chosen["name"].lower()
                if "rechnungen" in folder_path:
                    default_type, type_confident = DocumentType.INVOICE, True
                elif any(kw in folder_path for kw in ("bescheid", "pkv-abrechnung", "pkv_abrechnung")):
                    default_type, type_confident = DocumentType.SETTLEMENT_REPORT, True
                elif any(kw in name_lower for kw in ("bescheid", "abrechnung", "erstattung", "leistung")):
                    default_type = DocumentType.SETTLEMENT_REPORT
                else:
                    default_type = DocumentType.INVOICE
        except (ValueError, IndexError):
            pass

    if not file_path:
        file_path = Prompt.ask("File path")

    # doc_type: auto-accept if folder was unambiguous, otherwise prompt
    if type_confident:
        console.print(f"  Document type [dim](folder ✓)[/dim]: [bold]{default_type.value}[/bold]")
        doc_type = default_type
    else:
        doc_type = prompt_enum("Document type", DocumentType, default=default_type)

    # captured_at: already set from Drive, otherwise prompt
    if captured is None:
        captured = prompt_date("Captured at", default=date.today())

    return dict(
        file_path     = file_path,
        document_type = doc_type,
        captured_at   = captured,
    )


def cents(value: Decimal) -> Decimal:
    from decimal import ROUND_HALF_UP
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
