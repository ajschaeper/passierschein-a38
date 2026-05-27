from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import (
    ClaimStatus,
    DocumentStatus,
    DocumentType,
    InboundPaymentStatus,
    InvoiceType,
    PatientRole,
    PaymentDirection,
    PaymentMatchStatus,
    PaymentStatus,
    SettlementLineItemStatus,
    SettlementReportLineItemsStatus,
    SplitType,
)


def new_id() -> str:
    return str(uuid.uuid4())


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Document  (captured file, classified and linked to Invoice or SettlementReport)
# ---------------------------------------------------------------------------

class Document(BaseModel):
    id:               str  = Field(default_factory=new_id)
    file_path:        str
    document_type:    DocumentType   = DocumentType.UNKNOWN
    captured_at:      date
    status:           DocumentStatus = DocumentStatus.PENDING
    linked_entity_id: Optional[str]  = None   # Invoice.id or SettlementReport.id

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------

class Person(BaseModel):
    person_id:   str  = Field(default_factory=new_id)
    first_name:  str
    family_name: str
    birth_date:  date
    role:        PatientRole

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Invoice  (leading entity)
# ---------------------------------------------------------------------------

class Invoice(BaseModel):
    id: str = Field(default_factory=new_id)

    # --- General ---
    person_id:           str
    invoice_type:        InvoiceType    = InvoiceType.OTHER
    split_type:          SplitType
    provider:            str
    date_of_service:     date
    date_received:       date
    due_date:            Optional[date] = None
    total_amount:        Decimal
    employee_net_expected: Decimal      # total_amount for standard; Beihilfe share for split
    payment_status:      PaymentStatus  = PaymentStatus.OPEN
    date_paid:           Optional[date] = None
    payment_id:          Optional[str]  = None   # FK → Payment (outbound)
    total_reimbursed:    Decimal        = Decimal("0.00")
    net_cost:            Decimal        = Decimal("0.00")  # recomputed on every update

    # --- PKV ---
    pkv_share_pct:       Decimal
    pkv_expected:        Decimal
    pkv_submitted_at:    Optional[date]          = None
    pkv_reimbursed:      Decimal                 = Decimal("0.00")
    pkv_claim_status:    ClaimStatus             = ClaimStatus.OPEN
    pkv_payment_status:  InboundPaymentStatus    = InboundPaymentStatus.OPEN

    # --- Beihilfe ---
    beihilfe_share_pct:     Decimal
    beihilfe_expected:      Decimal
    beihilfe_submitted_at:  Optional[date]       = None
    beihilfe_reimbursed:    Decimal              = Decimal("0.00")
    beihilfe_claim_status:  ClaimStatus          = ClaimStatus.OPEN
    beihilfe_payment_status: InboundPaymentStatus = InboundPaymentStatus.OPEN

    document_id:     Optional[str] = None   # FK → Document

    class Config:
        use_enum_values = True

    @model_validator(mode="after")
    def apply_split_type_defaults(self) -> Invoice:
        """PKV is not applicable for beihilfe_only and direct_billing."""
        if self.split_type in (SplitType.BEIHILFE_ONLY, SplitType.DIRECT_BILLING):
            self.pkv_claim_status   = ClaimStatus.NOT_APPLICABLE
            self.pkv_payment_status = InboundPaymentStatus.OPEN
        return self

    @model_validator(mode="after")
    def recompute_totals(self) -> Invoice:
        self.total_reimbursed = cents(self.pkv_reimbursed + self.beihilfe_reimbursed)
        self.net_cost         = cents(self.employee_net_expected - self.total_reimbursed)
        return self

    @field_validator(
        "total_amount", "employee_net_expected", "pkv_share_pct", "pkv_expected",
        "pkv_reimbursed", "beihilfe_share_pct", "beihilfe_expected",
        "beihilfe_reimbursed", "total_reimbursed", "net_cost",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v)) if v is not None else Decimal("0")


# ---------------------------------------------------------------------------
# Settlement Report
# ---------------------------------------------------------------------------

class SettlementReport(BaseModel):
    id:               str  = Field(default_factory=new_id)
    type:             str  # "pkv" or "beihilfe"
    report_reference: str
    received_at:      date
    total_reimbursed: Decimal = Decimal("0.00")
    document_id:      Optional[str] = None   # FK → Document
    line_items_status: SettlementReportLineItemsStatus = SettlementReportLineItemsStatus.UNPROCESSED
    payment_status:   PaymentStatus = PaymentStatus.OPEN

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Settlement Line Item
# ---------------------------------------------------------------------------

class SettlementLineItem(BaseModel):
    id:                    str  = Field(default_factory=new_id)
    settlement_report_id:  str
    invoice_id:            str
    type:                  str       # "pkv" or "beihilfe"
    billed_amount:         Decimal
    eligible_amount:       Decimal
    reimbursed_amount:     Decimal
    not_covered_amount:    Decimal   = Decimal("0.00")
    rate_cap_reduction:    Decimal   = Decimal("0.00")
    deductible_applied:    Decimal   = Decimal("0.00")
    rejection_reasons:     Optional[str] = None
    status:                SettlementLineItemStatus = SettlementLineItemStatus.COVERED

    class Config:
        use_enum_values = True

    @model_validator(mode="after")
    def check_invariant(self) -> SettlementLineItem:
        reconstructed = cents(
            self.reimbursed_amount
            + self.not_covered_amount
            + self.rate_cap_reduction
            + self.deductible_applied
        )
        if abs(reconstructed - cents(self.billed_amount)) > Decimal("0.02"):
            raise ValueError(
                f"Invariant violated: billed={self.billed_amount} != "
                f"reimbursed+not_covered+rate_cap+deductible={reconstructed}"
            )
        return self


# ---------------------------------------------------------------------------
# Payment  (inbound from insurer OR outbound to provider)
# ---------------------------------------------------------------------------

class Payment(BaseModel):
    id:                    str  = Field(default_factory=new_id)
    direction:             PaymentDirection
    date:                  date
    amount:                Decimal
    settlement_report_id:  Optional[str] = None   # inbound only
    discrepancy:           Decimal        = Decimal("0.00")  # inbound only
    bank_reference:        Optional[str]  = None
    match_status:          PaymentMatchStatus = PaymentMatchStatus.UNMATCHED
    counterparty:          Optional[str]  = None  # bank transaction description
    import_fingerprint:    Optional[str]  = None  # sha1 dedup key for CSV imports

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Beihilfe Annual Deductible  (Kostendämpfungspauschale)
# ---------------------------------------------------------------------------

class BeihilfeDeductible(BaseModel):
    year:               int
    configured_amount:  Decimal
    consumed_ytd:       Decimal  = Decimal("0.00")

    @property
    def remaining(self) -> Decimal:
        return cents(max(Decimal("0.00"), self.configured_amount - self.consumed_ytd))
