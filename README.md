# Passierschein A38

CLI tool for tracking German Beihilfe + PKV healthcare reimbursements.

> See [CONCEPT.md](CONCEPT.md) for the full domain model, data design, and architectural rationale.

---

## Setup

**Prerequisites:** Python 3.11+, a Google Cloud service account with Sheets + Drive access.

```bash
pip install -r requirements.txt
```

**Environment — copy and fill in:**

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `SPREADSHEET_ID` | Google Sheets ID (required) |
| `GOOGLE_CREDENTIALS_FILE` | Path to service account JSON (default: `credentials.json`) |
| `ANTHROPIC_API_KEY` | Claude API key (required for `add-document` OCR) |
| `DRIVE_FOLDER_ID` | Root Drive folder for archived documents |
| `DRIVE_INBOX_ID` | Drive Inbox folder scanned by `add-document --all` |

**One-time sheet setup:**

```bash
python scripts/setup_sheets.py
```

---

## Commands

```
python scripts/cli.py <command> [options]
```

| Command | What it does |
|---|---|
| `add-person` | Add a household member (employee, spouse, or child) |
| `list-persons` | List all household members |
| `add-document [--file F] [--all]` | OCR a PDF/image → route to invoice or settlement report. `--all` processes every file in the Drive Inbox |
| `submit <id…> [--side pkv\|beihilfe\|both]` | Record claim submission for one or more invoices |
| `set-paid-out <invoice-id>` | Record that you paid the doctor |
| `import-payments <file.csv>` | Bulk-import a C24 bank statement CSV; non-health rows pre-labelled out-of-scope |
| `add-payment` | Manually enter a single bank transaction |
| `match-payments` | Interactively match unmatched payments to invoices or settlement reports |
| `dashboard` | Cashflow summary and open invoices |
| `alerts` | Overdue claims and pending reimbursements |
| `migrate-sheet` | Sync all sheet header rows to the current schema (run after code updates) |
| `ocr <file>` | Inspect raw OCR output for a file (dev tool) |

---

## Typical flow

```
add-document --all          # process Drive Inbox → invoices created
submit <id> --side both     # record submission to PKV + Beihilfe
import-payments statement.csv  # import bank CSV, pre-screen OOS rows
match-payments              # link payments to reports/invoices
dashboard                   # review net position
alerts                      # check for overdue items
```
