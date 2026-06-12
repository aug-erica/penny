# cfo-agent — Expense Close Agent (Phase 1)

Agent drafts, human approves. The first agent in the August × Skyfin month-close
automation experiment. Spec: `August Brain/06_Operations/CFO Agent/Expensify-Agent-Spec.md`.

## What it does (Phase 1, read-only)

Ingests corporate-card spend (Chase statement PDFs) and reimbursable spend
(Expensify Report Exporter), matches receipts, proposes a chart-of-accounts line
per transaction with confidence + rationale, cross-checks completeness against
the QBO reconciliation reports, and emits a review workbook + gap list to the
Drive `CFO Agent/runs/<month>/` folder. **Nothing is ever sent or posted.**

## Layout

- `src/cfo_agent/engine/` — client-agnostic: ledger (SQLite), matching,
  categorization cascade (rules → history → LLM), gaps, validation, review xlsx.
- `src/cfo_agent/adapters/` — the three swappable slots: card feed in
  (`chase_statement_pdf`), receipt vault (`expensify_exporter`), books out
  (`csv_stub`; QBO rec-report reader for cross-checks).
- `clients/august/` — ALL client specifics: paths, COA, rules, reviewer.
  A second client is a new folder here, not new engine code.

## Commands

```bash
.venv/bin/cfo verify-feed --client august          # statement parser self-checks
.venv/bin/cfo vault smoke --client august          # Expensify credentials test
.venv/bin/cfo close run --client august --month 2026-04 [--no-llm] [--no-vault] [--shadow]
.venv/bin/cfo close validate --client august --month 2026-04
```

`close run` refuses to proceed if any statement parse doesn't tie to the
printed totals. `--shadow` includes SUBMITTED Expensify reports (for drafting a
close in parallel with the bookkeeper) and stamps the run for later blind scoring.

## Credentials

Copy `.env.example` to `.env` (local only, gitignored). Expensify
`partnerUserID`/`partnerUserSecret` from an approver account; optional
`ANTHROPIC_API_KEY` for the LLM categorization fallback.
