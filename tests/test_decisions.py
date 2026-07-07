"""The decisions journal: human decisions must survive a close re-run
(reset_proposals_for_month wipes agent proposals; apply_decisions replays the
humans' word on top). Plus the approve step and the KV store."""
from datetime import date

import pytest

from cfo_agent.engine import kv, ledger
from cfo_agent.engine.normalize import card_txn_to_line, vault_expense_to_line
from cfo_agent.models import CardTxn, VaultExpense

CLIENT = "august"


@pytest.fixture(autouse=True)
def _never_postgres(monkeypatch):
    # Tests must always run on tmp SQLite, even in a shell with DATABASE_URL set.
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _card_line(conn, month="2026-06", merchant="UNITED 0162393308095 UNITED.COM TX",
               cents=48867, day=13, card="Erica Seldin"):
    t = CardTxn(date(int(month[:4]), int(month[5:7]), day), merchant, cents, card,
                "8303:2026-07-07")
    line = card_txn_to_line(t, CLIENT, "August Public Inc", month)
    lid = ledger.upsert_line(conn, line)
    return lid, line["external_id"]


def test_decisions_survive_rerun(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    lid, ext = _card_line(conn)
    # agent proposes; then the pal decides (as reply_flow would record it)
    ledger.set_proposal(conn, lid, "Billable Expense", "llm", "medium", "LLM guess")
    ledger.set_billable_project(conn, lid, 1, "McCain PACE")
    ledger.record_decision(conn, CLIENT, "2026-06", ext, "billable_project",
                           {"billable": 1, "project": "McCain PACE"},
                           decided_by="Erica Seldin", source="slack")
    ledger.set_proposal(conn, lid, "General Travel", "reviewer", "high",
                        "recategorized by Erica", status="flagged")
    ledger.record_decision(conn, CLIENT, "2026-06", ext, "category",
                           {"coa_line": "General Travel",
                            "rationale": "recategorized by Erica", "status": "flagged"},
                           decided_by="Erica Seldin", source="slack")
    ledger.set_receipt_status(conn, lid, "stored", "drive://receipt")
    ledger.record_decision(conn, CLIENT, "2026-06", ext, "receipt",
                           {"status": "stored", "link": "drive://receipt"},
                           decided_by="Erica Seldin", source="slack")

    # the close re-run wipe...
    ledger.reset_proposals_for_month(conn, CLIENT, "2026-06")
    wiped = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert wiped["billable"] is None and wiped["proposed_coa_line"] is None

    # ...and the replay restores every human decision
    res = ledger.apply_decisions(conn, CLIENT, "2026-06")
    assert res == {"applied": 3, "skipped": 0}
    l = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert l["billable"] == 1 and l["project"] == "McCain PACE"
    assert l["proposed_coa_line"] == "General Travel"
    assert l["proposed_by"] == "reviewer" and l["status"] == "flagged"
    assert l["receipt_status"] == "stored" and l["receipt_link"] == "drive://receipt"


def test_replay_latest_decision_wins(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    lid, ext = _card_line(conn)
    for coa in ("General Travel", "Billable Expense"):
        ledger.record_decision(conn, CLIENT, "2026-06", ext, "category",
                               {"coa_line": coa, "rationale": "r"}, source="slack")
    ledger.apply_decisions(conn, CLIENT, "2026-06")
    assert ledger.lines_for_month(conn, CLIENT, "2026-06")[0]["proposed_coa_line"] \
        == "Billable Expense"


def test_unknown_external_id_is_skipped_not_fatal(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    _card_line(conn)
    ledger.record_decision(conn, CLIENT, "2026-06", "no-such-line", "category",
                           {"coa_line": "General Travel"}, source="slack")
    res = ledger.apply_decisions(conn, CLIENT, "2026-06")
    assert res == {"applied": 0, "skipped": 1}


def test_approve_month_blocks_then_forces(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    lid_a, _ = _card_line(conn, merchant="ANTHROPIC ANTHROPIC.COM CA", cents=4899, day=2)
    lid_b, _ = _card_line(conn, merchant="MYSTERY MERCHANT", cents=1200, day=3)
    ledger.set_proposal(conn, lid_a, "R&D AI Everyday (Internal Tools & Training)",
                        "rule", "high", "seed rule")

    res = ledger.approve_month(conn, CLIENT, "2026-06", decided_by="Purvi Patel")
    assert res["refused"] and res["approved"] == 0
    assert [l["id"] for l in res["blocked"]] == [lid_b]
    assert ledger.lines_for_month(conn, CLIENT, "2026-06")[0]["status"] != "approved"

    res = ledger.approve_month(conn, CLIENT, "2026-06", decided_by="Purvi Patel",
                               force=True)
    assert not res["refused"] and res["approved"] == 1
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-06")}
    assert lines[lid_a]["status"] == "approved"
    assert lines[lid_b]["status"] == "draft"          # blocker left open, untouched
    # approval is journaled for the audit trail
    n = conn.execute("SELECT COUNT(*) AS n FROM decisions WHERE field='status'").fetchone()["n"]
    assert n == 1
    # and approved lines survive the re-run wipe natively
    ledger.reset_proposals_for_month(conn, CLIENT, "2026-06")
    assert ledger.lines_for_month(conn, CLIENT, "2026-06")[0]["status"] == "approved"


def test_history_feeds_on_approved_and_skips_bootstrap(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    lid, _ = _card_line(conn, month="2026-05", merchant="ANTHROPIC ANTHROPIC.COM CA",
                        cents=4899, day=20)
    ledger.set_proposal(conn, lid, "R&D AI Everyday (Internal Tools & Training)",
                        "reviewer", "high", "corrected")
    # same month also has employee Expensify coding (the bootstrap source)
    e = VaultExpense(date(2026, 5, 21), "Granola", 42000, "USD", "x@aug.co",
                     "Web Services & Subscriptions", "", False, False, "u", "r1",
                     "APPROVED", "t1")
    ledger.upsert_line(conn, vault_expense_to_line(e, CLIENT, "August Public Inc",
                                                   "2026-05"))
    ledger.approve_month(conn, CLIENT, "2026-05", force=True)
    ledger.rebuild_merchant_history(conn, CLIENT, through_month="2026-06")
    assert ledger.history_for_merchant(conn, CLIENT, "ANTHROPIC")[0]["coa_line"] \
        == "R&D AI Everyday (Internal Tools & Training)"
    # an approved month contributes ONLY approved decisions — bootstrap skipped
    assert ledger.history_for_merchant(conn, CLIENT, "GRANOLA") == []


def test_backfill_journals_existing_edits_once(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    lid, ext = _card_line(conn)
    ledger.set_proposal(conn, lid, "General Travel", "reviewer", "high",
                        "reviewer edit (dashboard)", status="flagged")
    ledger.set_billable_project(conn, lid, 0, None)
    ledger.set_receipt_status(conn, lid, "stored", "drive://r")

    assert ledger.backfill_decisions(conn, CLIENT, "2026-06") == 3
    with pytest.raises(RuntimeError):
        ledger.backfill_decisions(conn, CLIENT, "2026-06")   # one-time only

    ledger.reset_proposals_for_month(conn, CLIENT, "2026-06")
    ledger.apply_decisions(conn, CLIENT, "2026-06")
    l = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert l["proposed_coa_line"] == "General Travel"
    assert l["billable"] == 0 and l["project"] is None
    assert l["receipt_status"] == "stored"


def test_admin_month_switch_parsing():
    from cfo_agent.adapters.penny_listener import _MONTH_SWITCH, _parse_month
    assert _parse_month("2026-07") == "2026-07"
    assert _parse_month("July 2026") == "2026-07"
    assert _parse_month("2026-13") is None
    assert _parse_month("banana") is None
    m = _MONTH_SWITCH.match("Penny, start the 2026-07 close")
    assert m and m.group(1) == "2026-07"
    m = _MONTH_SWITCH.match("switch to July close!")
    assert m and m.group(1) == "July"
    # normal expense replies must never be hijacked
    assert _MONTH_SWITCH.match("the SF trip was McCain, rest not billable") is None
    assert _MONTH_SWITCH.match("start chasing those receipts please") is None


def test_ddl_splits_cleanly_for_postgres():
    """Every statement the Postgres shim will execute must be real SQL — a
    semicolon inside a DDL comment once split mid-comment and shipped garbage
    to production (July 6). SQLite tests can't catch that; this does."""
    from cfo_agent.engine import db
    stmts = db.pg_statements(ledger.DDL.replace("{PK}", "BIGSERIAL PRIMARY KEY"))
    assert stmts, "DDL produced no statements"
    for s in stmts:
        assert s.upper().startswith("CREATE "), f"garbage statement: {s[:60]!r}"


def test_kv_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kv, "RUNS_LOCAL", tmp_path)
    assert kv.get("close_month:august") is None
    assert kv.active_month("august", "2026-06") == "2026-06"   # fallback
    kv.set("close_month:august", "2026-07")
    assert kv.active_month("august", "2026-06") == "2026-07"   # DB wins
    kv.set("close_month:august", "2026-08")                    # overwrite
    assert kv.get("close_month:august") == "2026-08"
