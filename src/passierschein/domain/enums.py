from __future__ import annotations

from enum import Enum


class PatientRole(str, Enum):
    EMPLOYEE = "employee"
    SPOUSE   = "spouse"
    CHILD    = "child"


class SplitType(str, Enum):
    CLASSIC        = "classic"         # role-based PKV+Beihilfe split; employee paid full invoice
    BEIHILFE_ONLY  = "beihilfe_only"   # 100% Beihilfe, PKV not applicable
    DIRECT_BILLING = "direct_billing"  # PKV billed directly to provider; employee claims Beihilfe only


class InvoiceType(str, Enum):
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


class PaymentStatus(str, Enum):
    OPEN = "open"
    PAID = "paid"


class ClaimStatus(str, Enum):
    NOT_APPLICABLE    = "not_applicable"   # e.g. PKV on a beihilfe_only invoice
    OPEN              = "open"
    CLAIMED           = "claimed"
    SETTLED           = "settled"
    PARTIALLY_SETTLED = "partially_settled"


class InboundPaymentStatus(str, Enum):
    OPEN     = "open"
    RECEIVED = "received"


class PaymentDirection(str, Enum):
    INBOUND  = "inbound"   # from insurer → employee (linked to settlement report)
    OUTBOUND = "outbound"  # from employee → provider   (linked to invoice)


class SettlementLineItemStatus(str, Enum):
    COVERED  = "covered"
    PARTIAL  = "partial"
    REJECTED = "rejected"


class SettlementReportLineItemsStatus(str, Enum):
    UNPROCESSED     = "unprocessed"
    FULLY_MATCHED   = "fully_matched"
    ITEMS_UNMATCHED = "items_unmatched"


class DocumentType(str, Enum):
    INVOICE           = "invoice"
    SETTLEMENT_REPORT = "settlement_report"
    UNKNOWN           = "unknown"


class DocumentStatus(str, Enum):
    PENDING   = "pending"
    PROCESSED = "processed"


class PaymentMatchStatus(str, Enum):
    UNMATCHED    = "unmatched"
    MATCHED      = "matched"
    OUT_OF_SCOPE = "out_of_scope"   # irrelevant transaction (groceries, rent, …)
