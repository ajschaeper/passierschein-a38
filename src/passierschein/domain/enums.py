from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """Shared base for our string enums.

    Subclassing `str, Enum` gives equality with raw strings and JSON-serialises
    cleanly, but `str(member)` in Python <3.11 still returns 'ClassName.MEMBER'
    instead of the value. We override __str__ so `str(PaymentStatus.OPEN) == 'open'`
    — keeping display, sheet writes, and dedup comparisons consistent.
    """
    def __str__(self) -> str:
        return self.value


class PatientRole(_StrEnum):
    EMPLOYEE = "employee"
    SPOUSE   = "spouse"
    CHILD    = "child"


class SplitType(_StrEnum):
    CLASSIC        = "classic"         # role-based PKV+Beihilfe split; employee paid full invoice
    BEIHILFE_ONLY  = "beihilfe_only"   # 100% Beihilfe, PKV not applicable
    DIRECT_BILLING = "direct_billing"  # deprecated alias for beihilfe_only — kept for backward compat


class InvoiceType(_StrEnum):
    """Categorisation only — does not drive the split lookup."""
    AMBULANT      = "ambulant"
    STATIONAER    = "stationaer"
    DENTAL_BASIC  = "dental_basic"
    ZAHNERSATZ    = "zahnersatz"
    KFO           = "kfo"
    PSYCHOTHERAPY = "psychotherapy"
    HILFSMITTEL   = "hilfsmittel"
    ARZNEIMITTEL  = "arzneimittel"
    HEILMITTEL    = "heilmittel"
    OTHER         = "other"


class PaymentStatus(_StrEnum):
    OPEN = "open"
    PAID = "paid"


class ClaimStatus(_StrEnum):
    NOT_APPLICABLE    = "not_applicable"   # e.g. PKV on a beihilfe_only invoice
    OPEN              = "open"
    CLAIMED           = "claimed"
    SETTLED           = "settled"
    PARTIALLY_SETTLED = "partially_settled"


class InboundPaymentStatus(_StrEnum):
    OPEN     = "open"
    RECEIVED = "received"


class PaymentDirection(_StrEnum):
    INBOUND  = "inbound"   # from insurer → employee (linked to settlement report)
    OUTBOUND = "outbound"  # from employee → provider   (linked to invoice)


class SettlementLineItemStatus(_StrEnum):
    COVERED  = "covered"
    PARTIAL  = "partial"
    REJECTED = "rejected"


class SettlementReportLineItemsStatus(_StrEnum):
    UNPROCESSED     = "unprocessed"
    FULLY_MATCHED   = "fully_matched"
    ITEMS_UNMATCHED = "items_unmatched"


class DocumentType(_StrEnum):
    INVOICE           = "invoice"
    SETTLEMENT_REPORT = "settlement_report"
    UNKNOWN           = "unknown"


class DocumentStatus(_StrEnum):
    PENDING   = "pending"
    PROCESSED = "processed"


class PaymentMatchStatus(_StrEnum):
    UNMATCHED    = "unmatched"
    MATCHED      = "matched"
    OUT_OF_SCOPE = "out_of_scope"   # irrelevant transaction (groceries, rent, …)
