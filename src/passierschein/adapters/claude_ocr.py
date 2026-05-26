"""
Invoice extraction via Claude (multimodal).

Sends a PDF or image to Claude and extracts structured invoice data.
Returns a dict ready to hydrate an Invoice model (minus split percentages,
which are resolved separately by the split matrix).
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic

from ..config import config

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
You are a precise data extraction assistant. Extract the following fields from this German medical invoice (Rechnung/Honorarrechnung).

Return ONLY a JSON object with these keys — no prose, no markdown, no explanation:

{
  "date_of_service": "YYYY-MM-DD or YYYY-MM (if only month known) or null",
  "date_of_invoice": "YYYY-MM-DD or null",
  "provider": "name of the doctor, hospital, or pharmacy",
  "patient_name": "name of the patient as written on the invoice",
  "total_amount": "decimal number as string, e.g. '142.50'",
  "invoice_type_hint": "one of: ambulant | stationaer | dental_basic | zahnersatz | kfo | psychotherapy | hilfsmittel | arzneimittel | heilmittel | other",
  "split_type_hint": "classic (employee paid full invoice, claims PKV+Beihilfe) | beihilfe_only (no PKV) | direct_billing (PKV billed to provider, invoice is Beihilfe portion only)",
  "invoice_number": "invoice/Rechnungsnummer if present, else null",
  "line_items": [
    {"description": "...", "amount": "decimal string"}
  ],
  "notes": "any relevant details (e.g. Beleg-Nr, GOÄ codes, partial invoice indicator) — keep brief"
}

Rules:
- Amounts in German format (1.234,56) must be converted to standard decimal (1234.56)
- If the invoice is labelled as a partial invoice for Beihilfe (e.g. 'Anteil für Beihilfe', 'Beihilfeanteil'), set split_type_hint to 'direct_billing'
- For date_of_service: use the date of treatment, not the invoice date. If multiple service dates, use the earliest.
- invoice_type_hint: use 'stationaer' for hospital stays (Krankenhaus, Klinik), 'dental_basic' for dental (Zahnarzt) unless clearly prosthetics/implants (zahnersatz), 'arzneimittel' for pharmacy (Apotheke)
- Return null for any field you cannot determine with confidence
- line_items: include at most 10 items; if there are more, summarise the remainder as a single "Weitere Positionen" entry
- Be concise — the entire JSON response must fit within 2000 tokens
"""


def _encode_file(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a PDF or image file."""
    suffix = path.suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media_type = media_types.get(suffix)
    if not media_type:
        raise ValueError(f"Unsupported file type: {suffix}")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, media_type


def extract_invoice_data(file_path: str | Path) -> Dict[str, Any]:
    """
    Send an invoice file to Claude and return extracted fields as a dict.

    Returns a dict with keys matching the JSON schema above.
    Raises on API error or unparseable response.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Invoice file not found: {path}")

    data, media_type = _encode_file(path)
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    log.info("Extracting invoice data from %s (%s)", path.name, media_type)

    if media_type == "application/pdf":
        # Use the document source type for PDFs
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            },
            {
                "type": "text",
                "text": EXTRACTION_PROMPT,
            },
        ]
    else:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            },
            {
                "type": "text",
                "text": EXTRACTION_PROMPT,
            },
        ]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    log.debug("Claude raw response: %s", raw)

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    # If the response was truncated (stop_reason == "max_tokens"), attempt
    # to salvage it by closing any open string/array/object so json.loads
    # has a chance of succeeding on the fields that did arrive.
    if response.stop_reason == "max_tokens":
        log.warning("Response was truncated (max_tokens hit) — attempting recovery")
        raw = _close_truncated_json(raw)

    extracted: Dict[str, Any] = json.loads(raw)
    log.info(
        "Extracted: provider=%s, amount=%s, type=%s",
        extracted.get("provider"),
        extracted.get("total_amount"),
        extracted.get("invoice_type_hint"),
    )
    return extracted


def _close_truncated_json(raw: str) -> str:
    """
    Best-effort repair of a JSON string that was cut off mid-stream.

    Strategy:
    1. Drop the last (incomplete) line — it's almost always the broken one.
    2. Strip trailing commas left by the dropped line.
    3. Close any open arrays and objects in reverse order.
    """
    lines = raw.rstrip().splitlines()

    # Drop the last line if it doesn't end with a complete JSON value token
    if lines and not lines[-1].rstrip().endswith((",", "}", "]", '"', "null", "true", "false")):
        lines = lines[:-1]

    partial = "\n".join(lines).rstrip().rstrip(",")

    # Count open brackets/braces to figure out what needs closing
    depth_curly  = partial.count("{") - partial.count("}")
    depth_square = partial.count("[") - partial.count("]")

    # Close open arrays first (they're nested inside the object)
    partial += "]" * max(depth_square, 0)
    partial += "}" * max(depth_curly, 0)

    return partial


def parse_german_amount(amount_str: str) -> Optional[Decimal]:
    """
    Convert a German-formatted amount string to Decimal.
    Handles: '1.234,56' → 1234.56, '142,50' → 142.50, '142.50' → 142.50
    """
    if not amount_str:
        return None
    s = str(amount_str).strip().replace(" ", "").replace("€", "").replace("EUR", "")
    # German format: dot as thousands separator, comma as decimal
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        log.warning("Could not parse amount: %r", amount_str)
        return None
