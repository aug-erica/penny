"""The August-owned expense ledger — the system of record for review.
SQLite for Phase 1; schema is deliberately portable to Postgres."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  client      TEXT NOT NULL,
  close_month TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  params_json TEXT
);

CREATE TABLE IF NOT EXISTS ledger_lines (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  external_id        TEXT NOT NULL UNIQUE,
  client             TEXT NOT NULL,
  entity             TEXT NOT NULL,
  close_month        TEXT NOT NULL,
  txn_date           TEXT NOT NULL,
  merchant_raw       TEXT NOT NULL,
  merchant_norm      TEXT NOT NULL,
  amount_cents       INTEGER NOT NULL,
  currency           TEXT NOT NULL DEFAULT 'USD',
  source             TEXT NOT NULL CHECK (source IN ('card_feed','receipt_vault')),
  statement_ref      TEXT,
  cardholder         TEXT,
  employee           TEXT,
  report_state       TEXT,
  reimbursable       INTEGER,
  matched_line_id    INTEGER REFERENCES ledger_lines(id),
  match_score        REAL,
  proposed_coa_line  TEXT,
  proposed_by        TEXT CHECK (proposed_by IN ('vault','rule','history','llm') OR proposed_by IS NULL),
  confidence         TEXT CHECK (confidence IN ('high','medium','low') OR confidence IS NULL),
  billable           INTEGER,
  receipt_link       TEXT,
  status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','flagged','approved','posted','excluded')),
  rationale          TEXT,
  truth_category     TEXT,
  truth_billable     INTEGER,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lines_month ON ledger_lines(client, close_month, source);
CREATE INDEX IF NOT EXISTS idx_lines_merch ON ledger_lines(merchant_norm);

CREATE TABLE IF NOT EXISTS merchant_history (
  client        TEXT NOT NULL,
  merchant_norm TEXT NOT NULL,
  coa_line      TEXT NOT NULL,
  close_month   TEXT NOT NULL,
  n             INTEGER NOT NULL,
  PRIMARY KEY (client, merchant_norm, coa_line, close_month)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def start_run(conn, client: str, close_month: str, params_json: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs (client, close_month, started_at, params_json) VALUES (?,?,?,?)",
        (client, close_month, now(), params_json),
    )
    conn.commit()
    return cur.lastrowid


def upsert_line(conn, line: dict) -> int:
    """Idempotent insert keyed on external_id. Returns the row id.
    Re-runs refresh source-derived fields but never clobber proposals or status."""
    ts = now()
    existing = conn.execute(
        "SELECT id FROM ledger_lines WHERE external_id = ?", (line["external_id"],)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE ledger_lines SET txn_date=?, merchant_raw=?, merchant_norm=?,
               amount_cents=?, currency=?, statement_ref=?, cardholder=?, employee=?,
               report_state=?, reimbursable=COALESCE(?, reimbursable),
               receipt_link=COALESCE(?, receipt_link),
               truth_category=COALESCE(?, truth_category),
               truth_billable=COALESCE(?, truth_billable), updated_at=?
               WHERE id=?""",
            (line["txn_date"], line["merchant_raw"], line["merchant_norm"],
             line["amount_cents"], line.get("currency", "USD"),
             line.get("statement_ref"), line.get("cardholder"), line.get("employee"),
             line.get("report_state"), line.get("reimbursable"), line.get("receipt_link"),
             line.get("truth_category"), line.get("truth_billable"), ts, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO ledger_lines
           (external_id, client, entity, close_month, txn_date, merchant_raw,
            merchant_norm, amount_cents, currency, source, statement_ref, cardholder,
            employee, report_state, reimbursable, receipt_link, truth_category,
            truth_billable, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (line["external_id"], line["client"], line["entity"], line["close_month"],
         line["txn_date"], line["merchant_raw"], line["merchant_norm"],
         line["amount_cents"], line.get("currency", "USD"), line["source"],
         line.get("statement_ref"), line.get("cardholder"), line.get("employee"),
         line.get("report_state"), line.get("reimbursable"), line.get("receipt_link"),
         line.get("truth_category"), line.get("truth_billable"),
         line.get("status", "draft"), ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def lines_for_month(conn, client: str, close_month: str, source: str = None) -> list:
    q = "SELECT * FROM ledger_lines WHERE client=? AND close_month=?"
    args = [client, close_month]
    if source:
        q += " AND source=?"
        args.append(source)
    return [dict(r) for r in conn.execute(q + " ORDER BY txn_date, id", args)]


def set_match(conn, line_id: int, matched_line_id: int, score: float,
              receipt_link: str = None, truth_category: str = None,
              truth_billable=None, employee: str = None):
    conn.execute(
        """UPDATE ledger_lines SET matched_line_id=?, match_score=?,
           receipt_link=COALESCE(?, receipt_link),
           truth_category=COALESCE(?, truth_category),
           truth_billable=COALESCE(?, truth_billable),
           employee=COALESCE(?, employee), updated_at=?
           WHERE id=?""",
        (matched_line_id, score, receipt_link, truth_category, truth_billable,
         employee, now(), line_id),
    )
    conn.commit()


def set_proposal(conn, line_id: int, coa_line: str, proposed_by: str,
                 confidence: str, rationale: str, billable=None, status: str = None):
    conn.execute(
        """UPDATE ledger_lines SET proposed_coa_line=?, proposed_by=?, confidence=?,
           rationale=?, billable=COALESCE(?, billable),
           status=COALESCE(?, status), updated_at=? WHERE id=?""",
        (coa_line, proposed_by, confidence, rationale, billable, status, now(), line_id),
    )
    conn.commit()


def set_status(conn, line_id: int, status: str):
    conn.execute("UPDATE ledger_lines SET status=?, updated_at=? WHERE id=?",
                 (status, now(), line_id))
    conn.commit()


def rebuild_merchant_history(conn, client: str, through_month: str):
    """Precedent index, strictly for months BEFORE through_month (keeps
    validation honest — no ground-truth leakage).

    Sources, in order of authority:
    1. Reviewer-approved ledger decisions (status approved/posted) — the
       agent's own correction loop. As closes run agent-first, this becomes
       the only source.
    2. Historical employee Expensify coding — bootstrap only, for months that
       predate the agent. The dependency on employees coding expenses decays
       as approved months accumulate; it must never grow.
    Months with approved decisions contribute ONLY those (source 2 is skipped
    for them), so the bootstrap can't dilute the reviewer's word."""
    conn.execute("DELETE FROM merchant_history WHERE client=?", (client,))
    conn.execute(
        """INSERT INTO merchant_history (client, merchant_norm, coa_line, close_month, n)
           SELECT client, merchant_norm, proposed_coa_line, close_month, COUNT(*)
           FROM ledger_lines
           WHERE client=? AND status IN ('approved','posted')
                 AND proposed_coa_line IS NOT NULL AND close_month < ?
           GROUP BY merchant_norm, proposed_coa_line, close_month""",
        (client, through_month),
    )
    conn.execute(
        """INSERT INTO merchant_history (client, merchant_norm, coa_line, close_month, n)
           SELECT client, merchant_norm, truth_category, close_month, COUNT(*)
           FROM ledger_lines
           WHERE client=? AND source='receipt_vault' AND truth_category IS NOT NULL
                 AND truth_category NOT IN ('', 'Uncategorized') AND close_month < ?
                 AND close_month NOT IN (
                     SELECT DISTINCT close_month FROM ledger_lines
                     WHERE client=? AND status IN ('approved','posted'))
           GROUP BY merchant_norm, truth_category, close_month""",
        (client, through_month, client),
    )
    conn.commit()


def history_for_merchant(conn, client: str, merchant_norm: str) -> list:
    return [dict(r) for r in conn.execute(
        """SELECT coa_line, SUM(n) AS n, COUNT(DISTINCT close_month) AS months
           FROM merchant_history WHERE client=? AND merchant_norm=?
           GROUP BY coa_line ORDER BY n DESC""",
        (client, merchant_norm),
    )]


def history_detail(conn, client: str, merchant_norm: str) -> list:
    """Per-(coa_line, close_month) counts, newest month first."""
    return [dict(r) for r in conn.execute(
        """SELECT coa_line, close_month, n FROM merchant_history
           WHERE client=? AND merchant_norm=?
           ORDER BY close_month DESC, n DESC""",
        (client, merchant_norm),
    )]
