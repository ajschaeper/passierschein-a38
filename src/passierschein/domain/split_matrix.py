"""
Split matrix: (PatientRole × SplitType) → (pkv_pct, beihilfe_pct)

Two active split types:
  - classic:       role-based split (employee 50/50, spouse 30/70, child 20/80)
  - beihilfe_only: 100% Beihilfe regardless of role; PKV not applicable.
                   Covers both: service not covered by PKV (e.g. Heilpraktiker),
                   and provider-direct-billed-PKV scenarios where only the
                   Beihilfe portion is invoiced to the employee.

  direct_billing is a deprecated alias for beihilfe_only (kept for backward
  compatibility with existing sheet data).

Special rule (§47 BBhV): employee Beihilfe share rises from 50% → 70%
when the household has 2+ children. Applies to classic only.
Detected at runtime by counting persons with role=child — not stored as a flag.

The matrix is seeded into Google Sheets on setup and can be overridden there.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Tuple

from .enums import PatientRole, SplitType


@dataclass(frozen=True)
class SplitEntry:
    pkv_pct:       Decimal
    beihilfe_pct:  Decimal

    def __post_init__(self) -> None:
        total = self.pkv_pct + self.beihilfe_pct
        if abs(total - Decimal("1.0")) > Decimal("0.001"):
            raise ValueError(f"pkv_pct + beihilfe_pct must equal 1.0, got {total}")

    @classmethod
    def from_beihilfe(cls, pct: str) -> SplitEntry:
        b = Decimal(pct)
        return cls(pkv_pct=Decimal("1") - b, beihilfe_pct=b)


def _e(b: str) -> SplitEntry:
    return SplitEntry.from_beihilfe(b)


# ---------------------------------------------------------------------------
# Default matrix  (4 effective rows)
# ---------------------------------------------------------------------------

_DEFAULTS: Dict[Tuple[PatientRole, SplitType], SplitEntry] = {
    (PatientRole.EMPLOYEE, SplitType.CLASSIC):       _e("0.50"),
    (PatientRole.SPOUSE,   SplitType.CLASSIC):       _e("0.70"),
    (PatientRole.CHILD,    SplitType.CLASSIC):       _e("0.80"),
    # beihilfe_only: 100% Beihilfe for all roles (direct_billing is a deprecated alias)
    (PatientRole.EMPLOYEE, SplitType.BEIHILFE_ONLY): _e("1.00"),
    (PatientRole.SPOUSE,   SplitType.BEIHILFE_ONLY): _e("1.00"),
    (PatientRole.CHILD,    SplitType.BEIHILFE_ONLY): _e("1.00"),
}

# Employee override when household has 2+ children (§47 BBhV)
_EMPLOYEE_2PLUS_CHILDREN: SplitEntry = _e("0.70")


class SplitMatrix:
    def __init__(
        self,
        overrides: Dict[Tuple[PatientRole, SplitType], SplitEntry] | None = None,
    ) -> None:
        self._overrides = overrides or {}

    def resolve(
        self,
        role: PatientRole,
        split_type: SplitType,
        two_plus_children: bool = False,
    ) -> SplitEntry:
        # direct_billing is a deprecated alias — normalise to beihilfe_only
        if split_type == SplitType.DIRECT_BILLING:
            split_type = SplitType.BEIHILFE_ONLY
        key = (role, split_type)
        if key in self._overrides:
            return self._overrides[key]
        # §47 BBhV: employee 50% → 70% when household has 2+ children (classic only)
        if (role == PatientRole.EMPLOYEE
                and split_type == SplitType.CLASSIC
                and two_plus_children):
            return _EMPLOYEE_2PLUS_CHILDREN
        return _DEFAULTS[key]

    def load_from_records(self, records: list[dict]) -> None:
        self._overrides = {}
        for row in records:
            key = (PatientRole(row["role"]), SplitType(row["split_type"]))
            self._overrides[key] = SplitEntry(
                pkv_pct=Decimal(str(row["pkv_pct"])),
                beihilfe_pct=Decimal(str(row["beihilfe_pct"])),
            )

    def to_records(self) -> list[dict]:
        rows = []
        for (role, split_type), entry in _DEFAULTS.items():
            rows.append({
                "role":         role.value,
                "split_type":   split_type.value,
                "pkv_pct":      str(entry.pkv_pct),
                "beihilfe_pct": str(entry.beihilfe_pct),
            })
        return rows


# Module-level singleton
matrix = SplitMatrix()
