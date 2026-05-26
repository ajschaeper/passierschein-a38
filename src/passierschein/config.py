from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill in your values."
        )
    return val


class Config:
    # Optional — only needed when OCR is re-enabled
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    GOOGLE_CREDENTIALS_FILE: Path = Path(
        os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    )
    SPREADSHEET_ID: str = _require("SPREADSHEET_ID")
    DRIVE_FOLDER_ID: str = os.getenv("DRIVE_FOLDER_ID", "")
    DRIVE_INBOX_ID:  str = os.getenv("DRIVE_INBOX_ID",  "")

    # Alert thresholds
    BEIHILFE_ALERT_WEEKS: int = int(os.getenv("BEIHILFE_ALERT_WEEKS", "8"))
    PKV_ALERT_WEEKS: int = int(os.getenv("PKV_ALERT_WEEKS", "4"))
    CASHFLOW_ALERT_EUR: float = float(os.getenv("CASHFLOW_ALERT_EUR", "5000"))


config = Config()
