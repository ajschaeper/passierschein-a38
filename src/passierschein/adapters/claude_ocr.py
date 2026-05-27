"""
Document extraction via Claude (multimodal).

Auto-detects document type and applies the appropriate schema:
  - invoice      → Rechnung / Honorarrechnung from doctor / hospital / pharmacy
  - settlement_report → Beihilfebescheid / PKV Leistungsabrechnung

Entry points:
  extract_document_data(file_path)   — auto-detect, returns {"document_type": ..., ...}
  extract_invoice_data(file_path)    — backward-compatible alias (asserts invoice type)
"""
from __future__ import annotations

import base64
import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic

from ..config import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

INVOICE_PROMPT = """\
You are a precise data extraction assistant. Extract the following fields from this \
German medical invoice (Rechnung / Honorarrechnung).

Return ONLY a JSON object — no prose, no markdown, no explanation:

{
  "document_type": "invoice",
  "date_of_service": "YYYY-MM-DD or YYYY-MM (if only month known) or null",
  "date_of_invoice": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "provider": "name of the doctor, hospital, or pharmacy",
  "patient_name": "name of the PATIENT who received treatment — only from labels indicating patient. Null if no such label is visible. See rules below.",
  "patient_birth_date": "YYYY-MM-DD — patient's date of birth, from the SAME block as patient_name. Null if patient_name is null.",
  "policy_holder_name": "name from the 'Versicherter:' / 'Versicherte Person:' label — the insurance policy holder, usually a parent on a child's invoice. Null if not present. Informational only — never the patient.",
  "policy_holder_birth_date": "YYYY-MM-DD — birth date next to the Versicherter, or null.",
  "total_amount": "decimal string, e.g. '142.50'",
  "invoice_type_hint": "ambulant | stationaer | dental_basic | zahnersatz | kfo | psychotherapy | hilfsmittel | arzneimittel | heilmittel | other",
  "split_type_hint": "classic | beihilfe_only",
  "invoice_number": "Rechnungsnummer or null",
  "notes": "brief note (GOÄ codes, partial invoice, etc.) or null"
}

Rules:
- Amounts in German format (1.234,56) → convert to standard decimal (1234.56)
- due_date: look for 'Bitte zahlen Sie bis', 'Zahlungsziel', 'zahlbar bis', 'fällig am'. \
If only payment terms like '30 Tage' are stated, compute due_date = date_of_invoice + those days. \
A date computed from explicit payment terms is deterministic — treat it as high confidence.
- patient_name / patient_birth_date: STRICTLY from a label that names the patient — \
'Name:', 'Patient:', 'Behandelter:', 'Pat.:'. If you cannot find such a label, return null \
for BOTH. Do NOT use the Rechnungsempfänger (billing address) or Versicherter (policy holder) \
as a substitute — those go into policy_holder_name/birth_date instead. A wrong patient \
silently corrupts data; null is the correct answer when uncertain — the user will pick \
manually. The patient on children's invoices is almost always different from the \
policy holder/recipient (who is the parent). \
Example matching this hospital-invoice layout: \
  'Versicherter: Schmidt, Anna, Geb. 08.03.1987' \
  'Name: Schmidt, Maja, Geb. 27.12.2024' \
→ patient_name='Schmidt, Maja', patient_birth_date='2024-12-27', \
  policy_holder_name='Schmidt, Anna', policy_holder_birth_date='1987-03-08'.
- split_type_hint: use 'beihilfe_only' when ANY of these signals is present — \
(a) labels 'Beihilfeanteil', 'Anteil für Beihilfe', 'gemäß Beihilfesatz' (provider billed PKV directly; this is the Beihilfe-portion invoice); \
(b) line items show a Beihilfe percentage factor e.g. '80 bis 1.847,56'; \
(c) 'Faktor: 0.8' or '80%' column across most items; \
(d) service is from a Heilpraktiker or other provider not covered by PKV. \
Use 'classic' for any other full invoice billed entirely to the employee.
- date_of_service: date of treatment (not invoice date); if multiple dates, use earliest.
- invoice_type_hint: 'stationaer' for hospital stays, 'dental_basic' for Zahnarzt unless \
prosthetics/implants (zahnersatz), 'arzneimittel' for pharmacy.
- Return null for any field you cannot determine with confidence.
- Do NOT include line_items — they are not needed for invoices.
- Keep notes brief. Total response must fit within 1000 tokens.
- Add a "_confidence" key at the end: an object mapping field names to \
"high" (clearly printed, unambiguous), "medium" (likely but inferred), or \
"low" (guessed or unclear). Only include fields where confidence is not high.
"""

SETTLEMENT_REPORT_PROMPT = """\
You are a precise data extraction assistant. Extract all data from this German \
Beihilfebescheid or PKV Leistungsabrechnung (settlement report).

The document has two parts:
  1. Cover letter pages — narrative text explaining decisions per Beleg-Nr., \
including rejection reasons, partial eligibility, legal references.
  2. Berechnung / Abrechnung table — one row per Beleg-Nr. with amounts.

Return ONLY a JSON object — no prose, no markdown, no explanation:

{
  "document_type": "settlement_report",
  "report_type": "beihilfe | pkv",
  "report_reference": "Antragsnummer or report number as string",
  "beihilfe_number": "Beihilfenummer / Versicherungsnummer or null",
  "date": "YYYY-MM-DD (Bescheiddatum / Abrechnungsdatum)",
  "recipient_name": "name of the Beihilfeberechtigter / policyholder",
  "total_granted": "decimal string — Überweisungsbetrag / total reimbursed",
  "line_items": [
    {
      "beleg_nr": "document/invoice reference number as string",
      "person": "A (employee) | K1 | K2 | K3 (children) | S (spouse) or as labelled",
      "invoice_date": "YYYY-MM-DD or null",
      "description": "Leistungsart / service description",
      "total_amount": "decimal string — Rechnungsbetrag (column 'Rechnungsbetrag': full invoice amount)",
      "beihilfe_pct": "integer — v.H. percentage from the 'v.H.' column (e.g. 70 or 80)",
      "amount_eligible": "decimal string — beihilfefähiger Abrechnungsbetrag \
(column 'Beihilfefähige Abrechnungsbetrag': the rate-cap-adjusted eligible amount, \
INDEPENDENT of PKV; may be less than total_amount when a rate cap applies, \
or 0.00 when the claim is rejected entirely)",
      "amount_granted": "decimal string — anteilige Beihilfe \
(column 'Anteilige Beihilfe': the amount Beihilfe actually pays = amount_eligible × beihilfe_pct%)",
      "notes": "any explanation from the cover letter for this Beleg-Nr. — \
e.g. rejection reason, legal reference, partial eligibility. null if none."
    }
  ]
}

Rules:
- Amounts in German format (1.234,56) → standard decimal (1234.56)
- Extract EVERY row from the Berechnung table — do not truncate or summarise.
- For each line item, scan the cover letter pages for references to that Beleg-Nr. \
and include the relevant explanation in 'notes' (1–2 sentences max).
- If a Beleg-Nr. appears in the cover letter only (rejected entirely, not in table), \
still include it as a line item with amount_granted='0.00' and the rejection reason in notes.
- person codes: 'A' = Antragsteller/employee, 'K1'/'K2'/etc. = children (Kind), \
'S' or 'E' = Ehegatte/spouse. Use whatever code appears in the document.
- report_type: 'beihilfe' for Beihilfebescheid from Bezirksregierung/Beihilfestelle; \
'pkv' for PKV Leistungsabrechnung/Erstattungsbescheid from a private insurer.
- Return null for any field you cannot determine.
- Add a "_confidence" key: object mapping field names to "high", "medium", or "low". \
Only include fields where confidence is not high.
"""

DETECT_PROMPT = """\
Look at this document. Is it:
(A) An INVOICE (Rechnung, Honorarrechnung) — issued by a doctor, hospital, pharmacy, or therapist, \
requesting payment from the patient.
(B) A SETTLEMENT REPORT (Beihilfebescheid, Leistungsabrechnung, Erstattungsbescheid) — issued by \
a Beihilfestelle (Bezirksregierung) or private insurer (PKV), confirming what they will reimburse.

Reply with exactly one word: invoice  OR  settlement_report
"""


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _resolve_to_local(file_path: str | Path) -> tuple[Path, bool]:
    """
    If file_path is a Drive file ID, download it to a temp file.
    Returns (local_path, is_temp) — caller must unlink if is_temp=True.
    """
    from .drive import is_drive_id, download_to_temp
    s = str(file_path)
    if is_drive_id(s):
        return download_to_temp(s), True
    return Path(s), False


def _encode_file(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a PDF or image file."""
    suffix = path.suffix.lower()
    media_types = {
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media_type = media_types.get(suffix)
    if not media_type:
        raise ValueError(f"Unsupported file type: {suffix}")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, media_type


def _build_content(data: str, media_type: str, prompt: str) -> list:
    if media_type == "application/pdf":
        return [
            {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": prompt},
        ]
    return [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
        {"type": "text", "text": prompt},
    ]


def _call_claude(content: list, max_tokens: int) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    log.debug("Claude raw response: %s", raw)

    if response.stop_reason == "max_tokens":
        log.warning("Response truncated (max_tokens=%d) — attempting JSON recovery", max_tokens)
        raw = _close_truncated_json(raw)

    return raw


def _parse_json(raw: str) -> Dict[str, Any]:
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def _close_truncated_json(raw: str) -> str:
    """Best-effort repair of a JSON string cut off mid-stream."""
    lines = raw.rstrip().splitlines()
    if lines and not lines[-1].rstrip().endswith((",", "}", "]", '"', "null", "true", "false")):
        lines = lines[:-1]
    partial = "\n".join(lines).rstrip().rstrip(",")
    depth_curly  = partial.count("{") - partial.count("}")
    depth_square = partial.count("[") - partial.count("]")
    partial += "]" * max(depth_square, 0)
    partial += "}" * max(depth_curly, 0)
    return partial


# ---------------------------------------------------------------------------
# Settlement report post-processing
# ---------------------------------------------------------------------------

def _compute_settlement_fields(report: Dict[str, Any]) -> None:
    """
    Add computed fields to each settlement report line item (mutates in place):

      beihilfe_due  = total_amount × beihilfe_pct%
                      (what Beihilfe should pay if no rate caps or rejections)

      deductible    = beihilfe_due − amount_granted
                      (employee's shortfall on the Beihilfe side)

    These are computed in Python rather than relying on Claude arithmetic.
    The table column 'amount_eligible' (beihilfefähiger Abrechnungsbetrag)
    is what the insurer actually used for the calculation — it can be lower
    than total_amount due to rate caps (Heilpraktiker, GOÄ multiplier limits)
    or 0.00 for a fully rejected claim.
    """
    from decimal import ROUND_HALF_UP
    q = Decimal("0.01")
    for item in report.get("line_items") or []:
        try:
            total   = Decimal(str(item.get("total_amount")  or "0"))
            pct     = Decimal(str(item.get("beihilfe_pct")  or "0")) / 100
            granted = Decimal(str(item.get("amount_granted") or "0"))
            due     = (total * pct).quantize(q, rounding=ROUND_HALF_UP)
            item["beihilfe_due"] = str(due)
            item["deductible"]   = str((due - granted).quantize(q, rounding=ROUND_HALF_UP))
        except Exception as exc:
            log.warning("Could not compute settlement fields for beleg %s: %s", item.get("beleg_nr"), exc)
            item.setdefault("beihilfe_due", None)
            item.setdefault("deductible",   None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_document_type(file_path: str | Path) -> str:
    """Return 'invoice' or 'settlement_report' by asking Claude."""
    path, is_temp = _resolve_to_local(file_path)
    try:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        data, media_type = _encode_file(path)
        content = _build_content(data, media_type, DETECT_PROMPT)
        raw = _call_claude(content, max_tokens=10)
        doc_type = raw.strip().lower()
        return "settlement_report" if "settlement" in doc_type else "invoice"
    finally:
        if is_temp:
            path.unlink(missing_ok=True)


def extract_document_data(file_path: str | Path) -> Dict[str, Any]:
    """
    Auto-detect document type and extract structured data.

    Returns a dict with 'document_type' as the first key, then either
    invoice fields or settlement report fields.
    Accepts a local file path or a Google Drive file ID.
    """
    path, is_temp = _resolve_to_local(file_path)
    try:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        data, media_type = _encode_file(path)
        log.info("Extracting from %s (%s)", path.name, media_type)

        # Step 1: detect type
        detect_content = _build_content(data, media_type, DETECT_PROMPT)
        type_raw = _call_claude(detect_content, max_tokens=10).strip().lower()
        is_settlement = "settlement" in type_raw
        log.info("Detected document type: %s", "settlement_report" if is_settlement else "invoice")

        # Step 2: extract with the right prompt and token budget
        prompt     = SETTLEMENT_REPORT_PROMPT if is_settlement else INVOICE_PROMPT
        max_tokens = 4096 if is_settlement else 1024

        extract_content = _build_content(data, media_type, prompt)
        raw = _call_claude(extract_content, max_tokens=max_tokens)
        extracted = _parse_json(raw)

        if is_settlement:
            _compute_settlement_fields(extracted)
            log.info(
                "Settlement report: ref=%s, total=%s, %d line items",
                extracted.get("report_reference"),
                extracted.get("total_granted"),
                len(extracted.get("line_items") or []),
            )
        else:
            log.info(
                "Invoice: provider=%s, amount=%s, type=%s",
                extracted.get("provider"),
                extracted.get("total_amount"),
                extracted.get("invoice_type_hint"),
            )
        return extracted
    finally:
        if is_temp:
            path.unlink(missing_ok=True)


def extract_invoice_data(file_path: str | Path) -> Dict[str, Any]:
    """
    Backward-compatible entry point: extracts invoice data directly
    (skips auto-detection, uses the invoice prompt).
    Accepts a local file path or a Google Drive file ID.
    """
    path, is_temp = _resolve_to_local(file_path)
    try:
        if not path.exists():
            raise FileNotFoundError(f"Invoice file not found: {path}")

        data, media_type = _encode_file(path)
        log.info("Extracting invoice from %s (%s)", path.name, media_type)

        content = _build_content(data, media_type, INVOICE_PROMPT)
        raw = _call_claude(content, max_tokens=1024)
        extracted = _parse_json(raw)
        log.info(
            "Extracted: provider=%s, amount=%s, type=%s",
            extracted.get("provider"),
            extracted.get("total_amount"),
            extracted.get("invoice_type_hint"),
        )
        return extracted
    finally:
        if is_temp:
            path.unlink(missing_ok=True)


def parse_german_amount(amount_str: str) -> Optional[Decimal]:
    """
    Convert a German-formatted amount string to Decimal.
    Handles: '1.234,56' → 1234.56, '142,50' → 142.50, '142.50' → 142.50
    """
    if not amount_str:
        return None
    s = str(amount_str).strip().replace(" ", "").replace("€", "").replace("EUR", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        log.warning("Could not parse amount: %r", amount_str)
        return None
