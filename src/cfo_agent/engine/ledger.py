"""The August-owned expense ledger — the system of record for review.
Runs on SQLite locally (and in tests) or Postgres in the cloud, via engine.db;
set DATABASE_URL to select Postgres. Schema is written portably: `{PK}` is the
autoincrement primary key, filled per dialect at open_db()."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import db

DDL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      {PK},
  client      TEXT NOT NULL,
  close_month TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  params_json TEXT
);

CREATE TABLE IF NOT EXISTS ledger_lines (
  id                 {PK},
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
  matched_line_id    BIGINT REFERENCES ledger_lines(id),
  match_score        REAL,
  proposed_coa_line  TEXT,
  proposed_by        TEXT CHECK (proposed_by IN ('vault','rule','history','llm','event','reviewer') OR proposed_by IS NULL),
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
CREATE TABLE IF NOT EXISTS processed_dm (
  ts            TEXT PRIMARY KEY,
  cardholder    TEXT,
  processed_at  TEXT NOT NULL
);
-- The correction flywheel, cloud-native: every reviewer correction (dashboard
-- or Slack) becomes a durable merchant->COA rule here, so next month's close
-- auto-proposes it. Replaces the old rules.yaml file writes (ephemeral on
-- Railway). Curated seed rules still live in rules.yaml.
CREATE TABLE IF NOT EXISTS merchant_rules (
  client        TEXT NOT NULL,
  merchant_norm TEXT NOT NULL,
  coa_line      TEXT NOT NULL,
  source        TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (client, merchant_norm)
);
-- Append-only journal of HUMAN decisions (pal replies, dashboard edits,
-- approvals). Agent proposals are rebuildable and live on ledger_lines, while
-- human decisions are durable and REPLAY on top after any close re-run
-- (apply_decisions). Never updated, never deleted — it is also the audit trail.
CREATE TABLE IF NOT EXISTS decisions (
  id               {PK},
  client           TEXT NOT NULL,
  close_month      TEXT NOT NULL,
  line_external_id TEXT NOT NULL,
  field            TEXT NOT NULL CHECK (field IN ('category','billable_project','receipt','status')),
  value_json       TEXT NOT NULL,
  decided_by       TEXT,
  source           TEXT,
  decided_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_month ON decisions(client, close_month);
-- One row per day the #finance digest was posted — the idempotency guard so the
-- listener's daily scheduler posts at most once per day, even across restarts.
CREATE TABLE IF NOT EXISTS digest_posts (
  client       TEXT NOT NULL,
  close_month  TEXT NOT NULL,
  post_date    TEXT NOT NULL,
  posted_at    TEXT NOT NULL,
  PRIMARY KEY (client, close_month, post_date)
);
"""

# Reimbursement tables kept in their OWN DDL block so open_db can create just these
# on an already-migrated Postgres WITHOUT re-touching ledger_lines — an
# ALTER/ADD COLUMN on the live ledger_lines takes an ACCESS EXCLUSIVE lock that
# can't be acquired while the listener/dashboard are querying it (it times out and
# rolls back the whole migration). These are all brand-new objects → no contention.
REIMB_DDL = """
-- Employee out-of-pocket REIMBURSEMENTS (the last thing leaving Expensify).
-- Kept in its own table, NOT ledger_lines: reimbursements are employee-initiated,
-- need human approval, and get PAID to a person — a different lifecycle
-- (submitted -> approved/rejected -> exported -> paid) and its own payee identity.
-- Overloading ledger_lines would force reimbursement branches through every card
-- path and require relaxing its source/status CHECKs.
CREATE TABLE IF NOT EXISTS reimbursements (
  id                {PK},
  external_id       TEXT NOT NULL UNIQUE,
  client            TEXT NOT NULL,
  entity            TEXT NOT NULL,
  employee          TEXT NOT NULL,
  submitter_uid     TEXT,
  kind              TEXT NOT NULL DEFAULT 'one_off'
                      CHECK (kind IN ('one_off','stipend')),
  expense_date      TEXT NOT NULL,
  close_month       TEXT NOT NULL,
  submitted_at      TEXT NOT NULL,
  amount_cents      INTEGER NOT NULL,
  currency          TEXT NOT NULL DEFAULT 'USD',
  business_purpose  TEXT,
  proposed_coa_line TEXT,
  billable          INTEGER,
  project           TEXT,
  orig_currency     TEXT,
  orig_amount_cents INTEGER,
  group_id          TEXT,
  receipt_status    TEXT,
  receipt_link      TEXT,
  status            TEXT NOT NULL DEFAULT 'submitted'
                      CHECK (status IN ('submitted','needs_info','approved','rejected','exported','paid')),
  approver          TEXT,
  approved_at       TEXT,
  rejected_reason   TEXT,
  payment_rail      TEXT,
  payout_ref        TEXT,
  exported_at       TEXT,
  paid_at           TEXT,
  qbo_vendor_id     TEXT,
  qbo_bill_id       TEXT,
  source_dm_ts      TEXT,
  rationale         TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reimb_month ON reimbursements(client, close_month, status);
CREATE INDEX IF NOT EXISTS idx_reimb_emp ON reimbursements(client, employee);
-- Append-only audit journal for reimbursements (parallel to `decisions`, kept
-- separate so apply_decisions never tries to replay these onto ledger_lines).
CREATE TABLE IF NOT EXISTS reimbursement_events (
  id                {PK},
  client            TEXT NOT NULL,
  reimb_external_id TEXT NOT NULL,
  event             TEXT NOT NULL,
  detail_json       TEXT,
  actor             TEXT,
  source            TEXT,
  at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reimb_events ON reimbursement_events(client, reimb_external_id);
"""

# Full schema = base tables + reimbursement tables (fresh DBs run all of it).
DDL = DDL + REIMB_DDL


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark_dm_processed(conn, ts: str, cardholder: str = None):
    if conn.is_pg:
        conn.execute("INSERT INTO processed_dm(ts, cardholder, processed_at) "
                     "VALUES (?,?,?) ON CONFLICT (ts) DO NOTHING", (ts, cardholder, now()))
    else:
        conn.execute("INSERT OR IGNORE INTO processed_dm(ts, cardholder, processed_at) "
                     "VALUES (?,?,?)", (ts, cardholder, now()))
    conn.commit()


def processed_dm_ts(conn) -> set:
    return {r["ts"] for r in conn.execute("SELECT ts FROM processed_dm")}


def claim_dm(conn, ts: str, cardholder: str = None) -> bool:
    """Atomically claim a message for processing. Returns True if THIS caller
    claimed it (go process), False if it was already claimed (skip). Prevents
    double-processing when a rolling deploy briefly runs two listeners that both
    receive the same Slack event."""
    if conn.is_pg:
        cur = conn.execute("INSERT INTO processed_dm(ts, cardholder, processed_at) "
                           "VALUES (?,?,?) ON CONFLICT (ts) DO NOTHING", (ts, cardholder, now()))
    else:
        cur = conn.execute("INSERT OR IGNORE INTO processed_dm(ts, cardholder, processed_at) "
                           "VALUES (?,?,?)", (ts, cardholder, now()))
    conn.commit()
    return cur.rowcount == 1


def unclaim_dm(conn, ts: str):
    """Release a claim so the message can be retried (call if processing threw)."""
    conn.execute("DELETE FROM processed_dm WHERE ts=?", (ts,))
    conn.commit()


def upsert_learned_rule(conn, client: str, merchant_norm: str, coa_line: str,
                        source: str = "reviewer"):
    """Record/refresh a learned merchant->COA rule (the flywheel). Latest
    correction for a merchant wins. Idempotent."""
    if not merchant_norm or not coa_line:
        return
    conn.execute(
        "INSERT INTO merchant_rules(client, merchant_norm, coa_line, source, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(client, merchant_norm) DO UPDATE SET "
        "coa_line=excluded.coa_line, source=excluded.source, updated_at=excluded.updated_at",
        (client, merchant_norm, coa_line, source, now()))
    conn.commit()


def learned_rules(conn, client: str) -> dict:
    """{merchant_norm: coa_line} from reviewer corrections, for the categorizer."""
    return {r["merchant_norm"]: r["coa_line"] for r in conn.execute(
        "SELECT merchant_norm, coa_line FROM merchant_rules WHERE client=?", (client,))}


def digest_posted(conn, client: str, close_month: str, post_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM digest_posts WHERE client=? AND close_month=? AND post_date=?",
        (client, close_month, post_date)).fetchone() is not None


def mark_digest_posted(conn, client: str, close_month: str, post_date: str):
    conn.execute(
        "INSERT INTO digest_posts(client, close_month, post_date, posted_at) "
        "VALUES(?,?,?,?) ON CONFLICT(client, close_month, post_date) DO NOTHING",
        (client, close_month, post_date, now()))
    conn.commit()


_SCHEMA_READY = set()   # backends whose schema this process has already ensured


def _ensure_reimb_columns(conn):
    """Additively add the reimbursement billable/project columns if missing. Runs
    even on the 'fully migrated' fast path (reimbursements already exists) because
    those columns post-date the table. Safe on the live DB: reimbursements is tiny,
    so the add is a fast metadata-only change under lock_timeout; any contention
    just no-ops and retries on the next start."""
    for col, decl in (("billable", "INTEGER"), ("project", "TEXT"),
                      ("orig_currency", "TEXT"), ("orig_amount_cents", "INTEGER"),
                      ("group_id", "TEXT")):
        try:
            if conn.is_pg:
                conn.execute(
                    f"ALTER TABLE reimbursements ADD COLUMN IF NOT EXISTS {col} {decl}")
            else:
                try:
                    conn.execute(f"ALTER TABLE reimbursements ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass  # already present
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def open_db(path) -> db.Conn:
    conn = db.connect(path)
    # Ensure schema ONCE per process (not per call): a web service opens a fresh
    # connection per request, and re-running CREATE/ALTER DDL each time serializes
    # on schema locks and times out workers. First connection sets it up.
    key = "pg" if conn.is_pg else str(path)
    if key in _SCHEMA_READY:
        return conn
    pk = "BIGSERIAL PRIMARY KEY" if conn.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    if conn.is_pg:
        try:
            conn.execute("SET lock_timeout='5s'")   # never hang a worker on DDL
        except Exception:
            pass
        # Fully migrated? `reimbursements` is the newest object — its presence means
        # skip ALL DDL (a CREATE/ALTER takes locks that fight the live listener).
        try:
            if conn.execute("SELECT 1 FROM information_schema.tables WHERE "
                            "table_name='reimbursements'").fetchone():
                _ensure_reimb_columns(conn)   # additive, idempotent (tiny table)
                _SCHEMA_READY.add(key)
                return conn
        except Exception:
            pass
        # Base schema present (billable_note exists) but reimbursements missing:
        # create ONLY the new tables. CRUCIAL — do NOT re-run the ledger_lines
        # ADD COLUMN loop here: those columns already exist and the ALTER needs an
        # ACCESS EXCLUSIVE lock that can't be taken while the table is being read,
        # so it times out and rolls back the whole migration (the reimbursements
        # CREATE included). The new tables are brand-new objects → no contention.
        try:
            has_base = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE "
                "table_name='ledger_lines' AND column_name='billable_note'").fetchone()
        except Exception:
            has_base = None
        if has_base:
            conn.executescript(REIMB_DDL.replace("{PK}", pk))
            conn.commit()
            _SCHEMA_READY.add(key)
            return conn
    # Fresh DB (or any SQLite): full schema + additive column migrations.
    conn.executescript(DDL.replace("{PK}", pk))
    for col, decl in (("project", "TEXT"), ("receipt_status", "TEXT"),
                      ("billable_note", "TEXT")):
        if conn.is_pg:
            conn.execute(f"ALTER TABLE ledger_lines ADD COLUMN IF NOT EXISTS {col} {decl}")
        else:
            try:
                conn.execute(f"ALTER TABLE ledger_lines ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # already present
    conn.commit()
    _SCHEMA_READY.add(key)
    return conn


def start_run(conn, client: str, close_month: str, params_json: str = "") -> int:
    rid = conn.insert_returning_id(
        "INSERT INTO runs (client, close_month, started_at, params_json) VALUES (?,?,?,?)",
        (client, close_month, now(), params_json), "run_id",
    )
    conn.commit()
    return rid


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
    new_id = conn.insert_returning_id(
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
         line.get("status", "draft"), ts, ts), "id",
    )
    conn.commit()
    return new_id


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
    # billable is an INTEGER column; a rule may hand us a Python bool, which
    # Postgres refuses to COALESCE against an integer (SQLite is lenient). Coerce.
    if isinstance(billable, bool):
        billable = int(billable)
    conn.execute(
        """UPDATE ledger_lines SET proposed_coa_line=?, proposed_by=?, confidence=?,
           rationale=?, billable=COALESCE(?, billable),
           status=COALESCE(?, status), updated_at=? WHERE id=?""",
        (coa_line, proposed_by, confidence, rationale, billable, status, now(), line_id),
    )
    conn.commit()


def reset_proposals_for_month(conn, client: str, close_month: str):
    """Clear agent proposals so a re-run re-proposes from scratch. Reviewer
    decisions (approved/posted) and excluded lines are left untouched — only
    draft/flagged proposals are wiped back to draft."""
    conn.execute(
        """UPDATE ledger_lines SET proposed_coa_line=NULL, proposed_by=NULL,
           confidence=NULL, rationale=NULL, billable=NULL, status='draft',
           updated_at=? WHERE client=? AND close_month=?
           AND status IN ('draft','flagged')""",
        (now(), client, close_month),
    )
    conn.commit()


def line_by_external_id(conn, client: str, external_id: str):
    r = conn.execute(
        "SELECT * FROM ledger_lines WHERE client=? AND external_id=?",
        (client, external_id)).fetchone()
    return dict(r) if r else None


def set_status(conn, line_id: int, status: str):
    conn.execute("UPDATE ledger_lines SET status=?, updated_at=? WHERE id=?",
                 (status, now(), line_id))
    conn.commit()


def set_billable_project(conn, line_id: int, billable, project=None):
    # Set both directly (not COALESCE): a reply is a definite decision, and a
    # "not billable" reply must clear any previously-tagged project.
    conn.execute(
        "UPDATE ledger_lines SET billable=?, project=?, updated_at=? WHERE id=?",
        (billable, project, now(), line_id))
    conn.commit()


def set_billable_note(conn, line_id: int, note: str):
    """One-line description of a billable expense, for Natalie to put on invoices
    (what Expensify captured natively)."""
    conn.execute("UPDATE ledger_lines SET billable_note=?, updated_at=? WHERE id=?",
                 (note, now(), line_id))
    conn.commit()


def set_receipt_status(conn, line_id: int, status: str, link: str = None):
    conn.execute(
        """UPDATE ledger_lines SET receipt_status=?, receipt_link=COALESCE(?, receipt_link),
           updated_at=? WHERE id=?""", (status, link, now(), line_id))
    conn.commit()


def record_decision(conn, client: str, close_month: str, external_id: str,
                    field: str, value: dict, decided_by: str = None,
                    source: str = None):
    """Append one human decision to the journal. Replayed by apply_decisions
    after a close re-run wipes agent proposals — so a re-run can never lose a
    pal's or reviewer's word."""
    conn.execute(
        "INSERT INTO decisions (client, close_month, line_external_id, field, "
        "value_json, decided_by, source, decided_at) VALUES (?,?,?,?,?,?,?,?)",
        (client, close_month, external_id, field, json.dumps(value),
         decided_by, source, now()))
    conn.commit()


def apply_decisions(conn, client: str, close_month: str) -> dict:
    """Replay the month's human decisions oldest-first on top of fresh agent
    proposals (a later decision on the same line/field wins by overwriting).
    Unknown external_ids — e.g. card lines re-keyed by a feed-adapter switch —
    are skipped and counted, never fatal."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions WHERE client=? AND close_month=? ORDER BY id",
        (client, close_month))]
    applied = skipped = 0
    for d in rows:
        line = line_by_external_id(conn, client, d["line_external_id"])
        if not line:
            skipped += 1
            continue
        v = json.loads(d["value_json"])
        f = d["field"]
        if f == "category":
            set_proposal(conn, line["id"], v["coa_line"], "reviewer", "high",
                         v.get("rationale", ""), status=v.get("status", "flagged"))
        elif f == "billable_project":
            set_billable_project(conn, line["id"], v.get("billable"), v.get("project"))
        elif f == "receipt":
            set_receipt_status(conn, line["id"], v.get("status"), v.get("link"))
        elif f == "status":
            set_status(conn, line["id"], v["status"])
        applied += 1
    return {"applied": applied, "skipped": skipped}


def approve_month(conn, client: str, close_month: str, decided_by: str = None,
                  force: bool = False) -> dict:
    """Reviewer sign-off over the month: mark every categorized target line
    (same population the categorizer works) 'approved', making it next month's
    top precedent source. Lines with no category are blockers: with force they
    are left untouched and reported; without force nothing is approved."""
    target = [l for l in lines_for_month(conn, client, close_month)
              if l["status"] != "excluded" and l["amount_cents"] > 0
              and (l["source"] == "card_feed" or l.get("reimbursable"))]
    blocked = [l for l in target if not l.get("proposed_coa_line")]
    to_approve = [l for l in target if l.get("proposed_coa_line")
                  and l["status"] in ("draft", "flagged")]
    already = sum(1 for l in target if l["status"] in ("approved", "posted"))
    if blocked and not force:
        return {"approved": 0, "already": already, "blocked": blocked,
                "refused": True}
    ts = now()
    conn.executemany(
        "UPDATE ledger_lines SET status='approved', updated_at=? WHERE id=?",
        [(ts, l["id"]) for l in to_approve])
    conn.executemany(
        "INSERT INTO decisions (client, close_month, line_external_id, field, "
        "value_json, decided_by, source, decided_at) VALUES (?,?,?,?,?,?,?,?)",
        [(client, close_month, l["external_id"], "status",
          json.dumps({"status": "approved"}), decided_by, "approve", ts)
         for l in to_approve])
    conn.commit()
    return {"approved": len(to_approve), "already": already, "blocked": blocked,
            "refused": False}


def backfill_decisions(conn, client: str, close_month: str) -> int:
    """One-time: synthesize decision rows from the current ledger state, so
    human edits made BEFORE the journal existed survive future re-runs.
    Refuses if the month already has decision rows (idempotency guard).
    Rows are marked source='backfill' — provenance is explicit, since a
    billable flag on a pre-journal line may be an LLM guess, not a human call;
    later real decisions win on replay regardless (latest-wins ordering)."""
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE client=? AND close_month=?",
        (client, close_month)).fetchone()["n"]
    if n:
        raise RuntimeError(
            f"{close_month} already has {n} decision row(s) — backfill is one-time only.")
    made = 0
    for l in lines_for_month(conn, client, close_month):
        if l.get("proposed_by") == "reviewer" and l.get("proposed_coa_line"):
            record_decision(conn, client, close_month, l["external_id"], "category",
                            {"coa_line": l["proposed_coa_line"],
                             "rationale": l.get("rationale") or "backfilled reviewer decision",
                             "status": l["status"] if l["status"] in ("flagged", "approved")
                             else "flagged"},
                            decided_by="backfill", source="backfill")
            made += 1
        if l.get("billable") is not None or l.get("project"):
            record_decision(conn, client, close_month, l["external_id"],
                            "billable_project",
                            {"billable": l.get("billable"), "project": l.get("project")},
                            decided_by="backfill", source="backfill")
            made += 1
        if l.get("receipt_status"):
            record_decision(conn, client, close_month, l["external_id"], "receipt",
                            {"status": l["receipt_status"], "link": l.get("receipt_link")},
                            decided_by="backfill", source="backfill")
            made += 1
    return made


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


# --- Employee reimbursements ------------------------------------------------

# Columns a caller may set on create_reimbursement (besides the always-required
# ones), so the insert stays explicit and dialect-portable.
_REIMB_COLS = (
    "external_id", "client", "entity", "employee", "submitter_uid", "kind",
    "expense_date", "close_month", "submitted_at", "amount_cents", "currency",
    "business_purpose", "proposed_coa_line", "billable", "project",
    "orig_currency", "orig_amount_cents", "group_id",
    "receipt_status", "receipt_link", "status", "source_dm_ts", "rationale",
)
# Fields set_reimbursement_status may update (whitelist — never interpolate keys
# from callers without this guard).
_REIMB_STATUS_FIELDS = frozenset({
    "approver", "approved_at", "rejected_reason", "payment_rail", "payout_ref",
    "exported_at", "paid_at", "qbo_vendor_id", "qbo_bill_id", "proposed_coa_line",
    "rationale",
})


def create_reimbursement(conn, r: dict) -> int:
    """Idempotent insert keyed on external_id. Returns the row id (existing or new)."""
    existing = conn.execute(
        "SELECT id FROM reimbursements WHERE external_id=?", (r["external_id"],)
    ).fetchone()
    if existing:
        return existing["id"]
    ts = now()
    vals = {
        "currency": "USD", "kind": "one_off", "status": "submitted",
        "submitted_at": ts, **{k: r.get(k) for k in _REIMB_COLS if k in r},
    }
    vals["submitted_at"] = vals.get("submitted_at") or ts
    cols = list(vals.keys()) + ["created_at", "updated_at"]
    placeholders = ",".join(["?"] * len(cols))
    args = [vals[c] for c in vals] + [ts, ts]
    new_id = conn.insert_returning_id(
        f"INSERT INTO reimbursements ({','.join(cols)}) VALUES ({placeholders})",
        tuple(args), "id")
    conn.commit()
    return new_id


def reimbursement_by_external_id(conn, client: str, external_id: str):
    r = conn.execute(
        "SELECT * FROM reimbursements WHERE client=? AND external_id=?",
        (client, external_id)).fetchone()
    return dict(r) if r else None


def reimbursement_by_id(conn, reimb_id: int):
    r = conn.execute("SELECT * FROM reimbursements WHERE id=?", (reimb_id,)).fetchone()
    return dict(r) if r else None


def reimbursements_in_group(conn, client: str, group_id: str) -> list:
    """All reimbursements from one multi-receipt submission (they share a group_id
    = the intake message ts). Empty group_id -> just that row is its own group."""
    if not group_id:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM reimbursements WHERE client=? AND group_id=? ORDER BY id",
        (client, group_id))]


def reimbursements_for(conn, client: str, month: str = None,
                       status=None) -> list:
    """List reimbursements for a client, optionally filtered by close_month and
    status (a single status string or an iterable of statuses)."""
    q = "SELECT * FROM reimbursements WHERE client=?"
    args = [client]
    if month:
        q += " AND close_month=?"
        args.append(month)
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        q += f" AND status IN ({','.join(['?'] * len(statuses))})"
        args.extend(statuses)
    return [dict(r) for r in conn.execute(q + " ORDER BY submitted_at, id", args)]


def set_reimbursement_status(conn, reimb_id: int, status: str, **fields):
    """Set status plus any whitelisted lifecycle fields (approver, approved_at,
    payout_ref, exported_at, paid_at, …). Ignores unknown keys defensively."""
    sets = ["status=?", "updated_at=?"]
    args = [status, now()]
    for k, v in fields.items():
        if k in _REIMB_STATUS_FIELDS:
            sets.append(f"{k}=?")
            args.append(v)
    args.append(reimb_id)
    conn.execute(f"UPDATE reimbursements SET {','.join(sets)} WHERE id=?", tuple(args))
    conn.commit()


_REIMB_EDIT_FIELDS = frozenset({
    "amount_cents", "business_purpose", "proposed_coa_line", "expense_date",
    "billable", "project", "orig_currency", "orig_amount_cents", "close_month",
})


def update_reimbursement_fields(conn, reimb_id: int, **fields):
    """Fill/correct intake data fields on an in-flight reimbursement (thread
    follow-up). Whitelisted; ignores unknown keys and None values (never nulls out
    an existing value)."""
    sets, args = [], []
    for k, v in fields.items():
        if k in _REIMB_EDIT_FIELDS and v is not None:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(now())
    args.append(reimb_id)
    conn.execute(f"UPDATE reimbursements SET {','.join(sets)} WHERE id=?", tuple(args))
    conn.commit()


def set_reimbursement_receipt(conn, reimb_id: int, status: str, link: str = None):
    conn.execute(
        """UPDATE reimbursements SET receipt_status=?,
           receipt_link=COALESCE(?, receipt_link), updated_at=? WHERE id=?""",
        (status, link, now(), reimb_id))
    conn.commit()


def set_reimbursement_coa(conn, reimb_id: int, coa_line: str):
    """Reviewer edits the proposed category on a reimbursement (dashboard)."""
    conn.execute(
        "UPDATE reimbursements SET proposed_coa_line=?, updated_at=? WHERE id=?",
        (coa_line, now(), reimb_id))
    conn.commit()


def record_reimbursement_event(conn, client: str, reimb_external_id: str,
                               event: str, detail: dict = None,
                               actor: str = None, source: str = None):
    """Append one entry to the reimbursement audit journal (never updated/deleted)."""
    conn.execute(
        "INSERT INTO reimbursement_events (client, reimb_external_id, event, "
        "detail_json, actor, source, at) VALUES (?,?,?,?,?,?,?)",
        (client, reimb_external_id, event,
         json.dumps(detail or {}), actor, source, now()))
    conn.commit()


def reimbursement_events(conn, client: str, reimb_external_id: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM reimbursement_events WHERE client=? AND reimb_external_id=? "
        "ORDER BY id", (client, reimb_external_id))]
