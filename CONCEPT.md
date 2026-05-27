# Passierschein A38

> Named after the legendary bureaucratic labyrinth in "Asterix Conquers Rome" — because that's what dealing with Beihilfe feels like.

## Problem

German "Beamte" (tenured state employees, ~2 million people as of 2026) face a uniquely complex healthcare reimbursement situation that no existing tool addresses well.

### The Dual-Insurance Setup

Every medical invoice triggers two parallel reimbursement processes:

1. **PKV (Private Krankenversicherung)** — the employee's private health insurer, covering a fixed percentage of eligible costs
2. **Beihilfe** — the employer (federal/state government), covering the remaining percentage directly

The Beihilfe share is determined by a `(person role × split type)` matrix. There are two split types:

- **Classic** — the standard role-based split; employee paid the full invoice upfront and claims both PKV and Beihilfe shares
- **Beihilfe-only** — 100% Beihilfe regardless of role; PKV not applicable. Covers two scenarios: (a) service not covered by PKV at all (e.g. Heilpraktiker); (b) provider already billed PKV directly and issued only the Beihilfe portion to the employee — the outcome is identical in both cases

| Role | Classic | Beihilfe-only |
|---|---|---|
| Employee | 50% Beihilfe / 50% PKV | 100% Beihilfe |
| Employee (2+ children) | 70% Beihilfe / 30% PKV | 100% Beihilfe |
| Spouse | 70% Beihilfe / 30% PKV | 100% Beihilfe |
| Child | 80% Beihilfe / 20% PKV | 100% Beihilfe |

The `split_type` is set at intake and drives the split matrix lookup. For `beihilfe_only`, `pkv_claim_status` is automatically set to `not_applicable`.

This means every invoice must be analysed, split, and submitted to two different institutions with different forms, deadlines, and processes.

### Beihilfe is Slow

Beihilfe offices are chronically slow — processing times of 6–12 weeks are common. This creates significant **cashflow exposure**: the employee fronts the full invoice amount and waits for reimbursement from two different sources on two different timelines.

---

## The Scale Problem (Personal Context / Motivating Use Case)

- Pregnant spouse + two care-intensive children
- ~20 invoices per month
- ~2,000–4,000 EUR in claims per month in normal months
- Individual outlier invoices up to ~400,000 EUR (e.g. complex hospital stays, NICU, specialist treatments) — not a single occurrence
- Without a system: constant risk of missing submissions, losing track of outstanding reimbursements, and misjudging available cash

---

## Goals

### 1. Save Time — Automate the Paperwork
- Capture invoices (scan/upload)
- Extract key data automatically (AI/OCR): date, provider, patient, amount, type
- Determine the correct PKV/Beihilfe split per patient
- Prepare claim submissions

### 2. Track Status — Know Where Every Euro Is
For each invoice, track the full lifecycle:

Each invoice has four independent status tracks. They can progress in any order.

```
                    ┌─ payment_status ──────────────────────────────────┐
                    │  open → paid                                       │
                    │                                                    │
Invoice captured ───┼─ pkv_claim_status ────────────────────────────────┤→ fully settled
                    │  open → claimed → settled / partially_settled      │   (all applicable
                    │  (n/a for beihilfe_only / direct_billing)          │    tracks complete)
                    │                                                    │
                    ├─ pkv_payment_status                                │
                    │  open → received (via settlement report payment)   │
                    │                                                    │
                    ├─ beihilfe_claim_status ───────────────────────────┤
                    │  open → claimed → settled / partially_settled      │
                    │                                                    │
                    └─ beihilfe_payment_status                           │
                       open → received (via settlement report payment)  ─┘
```

> **Cashflow note:** reimbursements (claim settled + payment received) can arrive before `payment_status = paid`. This is the preferred scenario — the employee uses incoming reimbursements to fund the payment to the provider.

At any point, the system should answer:
- *Which invoices are outstanding?*
- *What am I still owed, and from whom?*
- *What's the reason for any shortfall — not covered, rate-capped, or deductible?*
- *What's stuck in Beihilfe purgatory?*

### 3. Cashflow Management — Anticipate Cash Needs
- Know which invoices are due for payment and when
- Know when reimbursements are expected (based on submission date + typical processing time)
- Flag outlier invoices that create significant short-term cash exposure
- Provide a running net position: total paid out minus total reimbursed

---

## Core Entities

### Document
A captured source file (invoice PDF, settlement report PDF). Acts as an intake queue — `pending` until linked to an Invoice or SettlementReport, then `processed`.

| Field | Description |
|---|---|
| `id` | Unique identifier |
| `file_path` | Local path or Drive URL |
| `document_type` | `invoice` / `settlement_report` / `unknown` |
| `captured_at` | Date the file was captured |
| `status` | `pending` → `processed` |
| `linked_entity_id` | FK → Invoice.id or SettlementReport.id (set when processed) |

### Invoice
The invoice is the **leading entity** of the data model. All claim, settlement, and payment information rolls up to the invoice level so the full financial picture is visible in one place.

**General**
| Field | Description |
|---|---|
| `id` | Unique identifier |
| `person_id` | FK → Person |
| `invoice_type` | Optional categorisation: `ambulant` / `stationaer` / `dental_basic` / `zahnersatz` / `kfo` / `psychotherapy` / `hilfsmittel` / `arzneimittel` / `heilmittel` / `other` (default) |
| `split_type` | `classic` / `beihilfe_only` — drives the split matrix lookup |
| `provider` | Doctor, hospital, pharmacy, etc. |
| `date_of_service` | When the medical service was rendered |
| `date_received` | When the invoice arrived |
| `due_date` | Payment deadline (if stated on invoice) |
| `total_amount` | Full invoice amount as billed |
| `employee_net_expected` | What the employee is liable to pay the provider: `total_amount` for classic/beihilfe_only; Beihilfe share only for direct_billing |
| `payment_status` | `open` / `paid` |
| `date_paid` | Date the employee paid the provider (null if not yet paid) |
| `total_reimbursed` | `pkv_reimbursed + beihilfe_reimbursed` — rolled up from settlement line items |
| `net_cost` | `employee_net_expected − total_reimbursed` — employee's true out-of-pocket |
| `document_id` | FK → Document (the source invoice file) |

**PKV**
| Field | Description |
|---|---|
| `pkv_share_pct` | PKV reimbursement rate, resolved from `split_matrix[person.role][split_type]` at intake |
| `pkv_expected` | `total_amount × pkv_share_pct` |
| `pkv_submitted_at` | Date PKV claim was submitted (null = not yet submitted) |
| `pkv_reimbursed` | Actual amount reimbursed by PKV (from settlement line items) |
| `pkv_claim_status` | `not_applicable` / `open` / `claimed` / `settled` / `partially_settled` |
| `pkv_payment_status` | `open` / `received` — whether the PKV bank transfer arrived |

**Beihilfe**
| Field | Description |
|---|---|
| `beihilfe_share_pct` | Beihilfe reimbursement rate, resolved from `split_matrix[person.role][split_type]` at intake |
| `beihilfe_expected` | `total_amount × beihilfe_share_pct` |
| `beihilfe_submitted_at` | Date Beihilfe claim was submitted (null = not yet submitted) |
| `beihilfe_reimbursed` | Actual amount reimbursed by Beihilfe (from settlement line items, after deductible) |
| `beihilfe_claim_status` | `not_applicable` / `open` / `claimed` / `settled` / `partially_settled` |
| `beihilfe_payment_status` | `open` / `received` — whether the Beihilfe bank transfer arrived |

> The invoice is **fully settled** when: `payment_status = paid` AND all applicable claim statuses are `settled` AND all applicable payment statuses are `received`.


### Settlement Report
The document received back from PKV or Beihilfe. Needs to be captured (PDF) and its line items recorded. The insurer determines the grouping — it may cover any set of open claims regardless of when they were submitted.

| Field | Description |
|---|---|
| `id` | Unique identifier |
| `type` | `pkv` or `beihilfe` |
| `received_at` | Date the report arrived |
| `report_reference` | The insurer's reference number on the document |
| `total_reimbursed` | Sum of all line items — this is what the payment should equal |
| `document_id` | FK → Document (the source settlement report PDF) |
| `line_items_status` | `unprocessed` / `fully_matched` / `items_unmatched` |
| `payment_status` | `open` / `paid` |

### Payment
A cash transaction — either inbound (reimbursement from PKV or Beihilfe) or outbound (employee paying the provider). Payments can be bulk-imported from a bank CSV export or entered manually; they are matched to their entity separately.

| Field | Description |
|---|---|
| `id` | Unique identifier |
| `direction` | `inbound` (from insurer to employee) / `outbound` (from employee to provider) |
| `date` | Transaction date |
| `amount` | Transaction amount |
| `settlement_report_id` | FK → Settlement Report (inbound only; null until matched) |
| `discrepancy` | `amount − settlement_report.total_reimbursed` (computed on match) |
| `bank_reference` | Verwendungszweck from the bank statement |
| `match_status` | `unmatched` / `matched` / `out_of_scope` |
| `counterparty` | Bank statement sender/recipient name |
| `import_fingerprint` | SHA-1 dedup key for CSV imports — prevents re-importing the same transaction |

> **Reconciliation note (inbound):** the payment amount should equal `settlement_report.total_reimbursed`. Any discrepancy is flagged before the report is marked as paid.
>
> **Out-of-scope:** payments irrelevant to healthcare tracking (groceries, rent, …) are marked `out_of_scope` — either at import time or interactively during `match-payments`. They are stored for audit completeness but never surfaced in matching workflows.

### Settlement Line Item
One row per invoice resolved within a settlement report. The line item is the audit trail for every euro gap between what was claimed and what was paid. Links directly to an invoice — there is no intermediate claim entity.

| Field | Description |
|---|---|
| `id` | Unique identifier |
| `settlement_report_id` | Parent settlement report |
| `type` | `pkv` or `beihilfe` |
| `invoice_id` | The invoice this line item resolves |
| `billed_amount` | Amount that was claimed |
| `eligible_amount` | Amount deemed eligible after insurer review (before deductible) |
| `reimbursed_amount` | Amount actually paid out (after deductible, if Beihilfe) |
| `not_covered_amount` | Portion rejected as not covered by policy / Beihilfe rules |
| `rate_cap_reduction` | Portion reduced due to rate caps (e.g. GOÄ factor limits, Beihilfe treatment caps) |
| `deductible_applied` | Annual deductible amount consumed by this line item (Beihilfe only) |
| `rejection_reasons` | Free text / codes explaining any reductions (from the report) |
| `status` | `covered` / `partial` / `rejected` |

> **Invariant:** `billed_amount = reimbursed_amount + not_covered_amount + rate_cap_reduction + deductible_applied`
>
> **Cardinality:** Invoice → Settlement Line Item is `1:{1,2}`. A `beihilfe_only` or `direct_billing` invoice produces exactly **1** line item (Beihilfe only; PKV is `not_applicable`). A `classic` invoice produces exactly **2** line items (one PKV, one Beihilfe), each belonging to its own settlement report.

### Person
Every household member who can appear on a medical invoice.

| Field | Description |
|---|---|
| `person_id` | Unique identifier |
| `first_name` | First name |
| `family_name` | Family name |
| `birth_date` | Date of birth (used to determine eligibility rules, e.g. KFO age limits) |
| `role` | `employee` / `spouse` / `child` — links to split matrix |

> `role` is one of the two keys into the split matrix (`role × split_type`). The 2+ children rule, which raises the employee's Beihilfe rate from 50% to 70%, is derived at runtime by counting persons with `role = child` — not stored as a separate flag.

---

### Beihilfe Annual Deductible (Kostendämpfungspauschale)
Beihilfe imposes an annual flat deductible. Its amount depends on Besoldungsgruppe (salary grade) and household size. It is consumed from the top of the first settlement(s) each calendar year.

| Field | Description |
|---|---|
| `year` | Calendar year |
| `configured_amount` | The applicable deductible for this year (from Beihilfeverordnung) |
| `consumed_ytd` | Amount consumed so far this year (sum of `deductible_applied` across all line items) |
| `remaining` | `configured_amount − consumed_ytd` (computed property) |

The system tracks `consumed_ytd` automatically as Beihilfe settlement line items are recorded. Once `remaining = 0`, subsequent Beihilfe line items are no longer reduced by the deductible.

---

## Process Triggers

| Trigger | CLI command | What it creates |
|---|---|---|
| **Document captured** | `add-document` | `Document` → OCR auto-classifies → routes to Invoice or SettlementReport |
| **Bank statement imported** | `import-payments <csv>` | Bulk `Payment` rows (unmatched); non-health rows pre-labelled `out_of_scope` |
| **Single payment entered** | `add-payment` | One `Payment` (unmatched) |
| **Claim submitted** | `submit` | Updates `*_submitted_at` and `*_claim_status = claimed` on Invoice |

Payments are matched to their entities via `match-payments`, which cascades status updates to the linked Invoice or SettlementReport.

---

## Workflows

### add-document
Captures a source file (PDF or image), extracts structured data via Claude OCR, and routes to the appropriate creation workflow.

1. User provides a local file path (or `--all` to process every file in the Drive Inbox)
2. Claude API extracts document data: detects document type (`invoice` / `settlement_report`), provider, dates, amounts, and patient name
3. If confidence is insufficient, user is prompted to confirm or correct the classification
4. **Invoice path:** extracted fields pre-fill the add-invoice flow; user reviews and confirms
5. **Settlement report path:** extracted fields pre-fill the add-settlement-report flow; line items are presented for review
6. On confirmation, `Document.linked_entity_id` is set and `status → processed`; file is archived to Google Drive

### add-invoice
1. User selects person, split type, provider, dates, and total amount
2. System resolves the split via `split_matrix[person.role][split_type]`
3. For `beihilfe_only`: `employee_net_expected` = full invoice amount (already the Beihilfe portion); `pkv_claim_status` set to `not_applicable`
4. User confirms; invoice saved with `payment_status = open`, claims `open`

### set-paid-out
1. User selects an invoice
2. System suggests `employee_net_expected` as the payment amount
3. User confirms date, amount, bank reference, counterparty
4. Payment saved with `match_status = matched`; invoice `payment_status → paid`

### submit
The workflow is the same for PKV and Beihilfe. Submission is recorded directly on the invoice — there is no separate claim entity.

1. User selects one or more invoices and indicates which side is being submitted (`pkv`, `beihilfe`, or `both`)
2. User provides the submission date
3. System sets `pkv_submitted_at` and/or `beihilfe_submitted_at`; corresponding claim status → `claimed`

> What we track is simply: *did we send this, and when?* How the insurer groups or processes submissions on their end is their concern, reflected in the settlement report when it arrives.

### add-settlement-report
When a settlement report (Leistungsabrechnung / Beihilfebescheid) arrives from PKV or Beihilfe:

1. User enters report metadata (type, reference, received date, total)
2. System shows open claims of the matching type
3. User enters line items one by one, matching each to an invoice
4. For Beihilfe line items: `deductible_applied` is recorded and `consumed_ytd` updated
5. Each matched invoice is updated: reimbursed amounts, claim status → `settled` / `partially_settled`
6. Report saved; unmatched invoices flagged

**Step B — Record the payment**

The actual bank transfer arrives separately (typically days after the report). Use `set-paid-in` or `add-payment` + `match-payments`.

> **Separation of concerns:** the report tells you *why* each euro was or wasn't reimbursed; the payment tells you the cash actually arrived. Both must be present before an invoice can be considered fully settled.

### set-paid-in
1. User identifies the settlement report (by ID or reference number)
2. User confirms payment date, amount, bank reference
3. System calculates `discrepancy = payment_amount − report.total_reimbursed`; flags if non-trivial
4. Payment saved with `match_status = matched`; report `payment_status → paid`; invoice `*_payment_status → received` cascaded to all line-item invoices

### import-payments
Bulk-imports a C24 bank statement CSV export and optionally runs `match-payments` immediately.

1. CSV is parsed and deduplicated against existing payments via `import_fingerprint` (SHA-1 of date + abs(amount) + Verwendungszweck) — re-importing the same file is safe
2. C24 category tags are used to pre-classify rows: `Kategorie = Gesundheit` or `Unterkategorie = Beihilfe` → health; everything else → `out_of_scope`
3. User sees a numbered preview table (non-health rows shown dimmed); can toggle any row's classification
4. On confirmation, all rows are saved in a single API call: health rows as `unmatched`, the rest as `out_of_scope`
5. Option to run `match-payments` immediately after import

### add-payment
Captures a single raw bank transaction without linking it to any entity yet.

1. User provides direction, date, amount, counterparty, optional bank reference
2. Payment saved with `match_status = unmatched`

### match-payments
Processes all unmatched payments interactively. `out_of_scope` payments are never shown here.

1. For each unmatched payment, system suggests candidates:
   - Outbound → invoices where `employee_net_expected ≈ payment.amount` and `payment_status ≠ paid`
   - Inbound → settlement reports where `total_reimbursed ≈ payment.amount` and `payment_status ≠ paid`
2. User confirms, provides a manual ID, or marks the payment as `out_of_scope` (`x`) to permanently dismiss it from future matching
3. On confirmation: payment linked, `match_status → matched`, status updates cascaded

### dashboard
- Cashflow summary: total invoiced, paid out, payments due, total reimbursed, net exposure
- PKV and Beihilfe inflows pending (submitted but not yet reimbursed)
- Open invoices table sorted by due date, with person name, split ratios, and all status tracks

### alerts
- Invoices received but not yet paid (approaching due date)
- Claims submitted but no reimbursement after N weeks
- PKV settled but Beihilfe still pending (common scenario)

---

## Tech Stack

### Now — Local Python + Google Sheets

| Layer | Technology | Role |
|---|---|---|
| Runtime | Local Python scripts | Orchestration, business logic |
| Storage / UI | Google Sheets (via `gspread`) | Database, manual input, dashboards |
| OCR / AI | Claude API (multimodal) | Invoice extraction from PDF/image |
| Document storage | Local filesystem / Google Drive | Raw invoice files |

**Google Sheets structure:**

| Sheet | Purpose |
|---|---|
| `documents` | Captured files — intake queue before linking to an invoice or report |
| `invoices` | Master invoice registry — the leading entity |
| `settlement_reports` | One row per settlement report received |
| `settlement_line_items` | One row per line item within a settlement report |
| `payments` | One row per payment (inbound or outbound); unmatched until linked |
| `beihilfe_deductible` | Annual deductible config + consumed YTD (one row per year) |
| `persons` | Household members with role, used to resolve split matrix |
| `split_matrix` | `(role × split_type) → PKV% / Beihilfe%` |

Google Sheets doubles as the UI for now — formatted views, conditional formatting for status, and manual overrides without needing a frontend.

### Later — AWS

| Layer | Technology |
|---|---|
| Compute | AWS Lambda (Python) |
| Document storage | S3 |
| Database | DynamoDB or RDS (PostgreSQL) |
| Scheduling | EventBridge |
| Secrets | AWS Secrets Manager |

The Python business logic is written to be runtime-agnostic (no Sheets-specific coupling in the core domain layer) so migration is a matter of swapping the persistence adapter.

---

## Out of Scope (for now)

- Direct API integration with PKV insurers or Beihilfe portals (these don't offer open APIs)
- Tax optimization / Steuererklärung assistance
- Multi-user / shared household access (could be added later)
- Document archiving / DMS features beyond what's needed for claim tracking
