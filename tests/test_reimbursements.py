"""Employee reimbursements: schema migration, lifecycle, no-self-approval,
idempotent stipends, needs-info on a missing receipt, and the Justworks manual
payout export. All on tmp SQLite (never Postgres)."""
import pytest

from cfo_agent.config import load_client
from cfo_agent.engine import ledger, reimburse_flow

CLIENT = "august"


@pytest.fixture(autouse=True)
def _never_postgres(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # No LLM in tests — interpret_reimbursement returns {} without a key, which is
    # exactly the "couldn't parse -> needs info" path we want to exercise.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _reimb(conn, employee="Keara Mascareñas", cents=4250, status="submitted",
           ext=None, month="2026-07", **extra):
    r = {"external_id": ext or f"reimb-test-{employee.split()[0]}-{cents}",
         "client": CLIENT, "entity": "August Public Inc", "employee": employee,
         "kind": "one_off", "expense_date": f"{month}-14", "close_month": month,
         "amount_cents": cents, "business_purpose": "client lunch",
         "proposed_coa_line": "Groceries & Meals", "receipt_status": "stored",
         "status": status, **extra}
    return ledger.create_reimbursement(conn, r)


def test_schema_creates_reimbursement_tables(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    # Both new tables must exist and be writable.
    rid = _reimb(conn)
    assert rid
    row = ledger.reimbursement_by_id(conn, rid)
    assert row["employee"] == "Keara Mascareñas" and row["status"] == "submitted"
    ledger.record_reimbursement_event(conn, CLIENT, row["external_id"], "submitted",
                                      {"x": 1}, actor="test", source="test")
    assert ledger.reimbursement_events(conn, CLIENT, row["external_id"])


def test_create_is_idempotent_on_external_id(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    a = _reimb(conn, ext="reimb-dup")
    b = _reimb(conn, ext="reimb-dup", cents=999)   # same ext -> same row, no dup
    assert a == b
    assert len(ledger.reimbursements_for(conn, CLIENT, "2026-07")) == 1


def test_status_transitions_and_filter(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    rid = _reimb(conn)
    ledger.set_reimbursement_status(conn, rid, "approved", approver="Purvi Patel",
                                    approved_at=ledger.now())
    row = ledger.reimbursement_by_id(conn, rid)
    assert row["status"] == "approved" and row["approver"] == "Purvi Patel"
    assert len(ledger.reimbursements_for(conn, CLIENT, "2026-07", status="approved")) == 1
    assert ledger.reimbursements_for(conn, CLIENT, "2026-07", status="paid") == []


def test_cant_approve_own(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    # Purvi is the primary approver -> her own reimbursement needs the secondary.
    assert reimburse_flow.approver_for(cfg, "Purvi Patel") == "Erica Seldin"
    assert reimburse_flow.approver_for(cfg, "Keara Mascareñas") == "Purvi Patel"

    pid = _reimb(conn, employee="Purvi Patel", ext="reimb-purvi")
    with pytest.raises(PermissionError):
        reimburse_flow.approve(conn, cfg, pid, "Purvi Patel")
    fresh = reimburse_flow.approve(conn, cfg, pid, "Erica Seldin")
    assert fresh["status"] == "approved" and fresh["approver"] == "Erica Seldin"

    kid = _reimb(conn, employee="Keara Mascareñas", ext="reimb-keara")
    fresh = reimburse_flow.approve(conn, cfg, kid, "Purvi Patel")
    assert fresh["status"] == "approved"


def test_needs_info_when_receipt_missing(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    # No API key -> parser returns {} -> amount/purpose/receipt all missing.
    res = reimburse_flow.process_intake(conn, cfg, "Keara Mascareñas", "UPDU3SRN0",
                                        "reimburse something", "2026-07",
                                        slack=None, file_ids=None,
                                        source_dm_ts="1720000000.0001")
    assert res["status"] == "needs_info"
    cb = res["confirm_back"].lower()
    assert "receipt" in cb and "category" in cb   # both are required, both asked for
    row = ledger.reimbursement_by_external_id(conn, CLIENT, "reimb-1720000000.0001")
    assert row and row["status"] == "needs_info"


def test_idempotent_stipends(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    cfg.raw["reimbursements"]["stipends"] = [
        {"name": "WiFi / Internet", "employee": "Keara Mascareñas",
         "amount_cents": 7500, "coa_line": "Telephone & Internet",
         "receipt_required": False}]
    a = reimburse_flow.generate_stipends(conn, cfg, "2026-07")
    b = reimburse_flow.generate_stipends(conn, cfg, "2026-07")   # re-run, no dup
    assert len(a) == 1 and len(b) == 1
    all_rows = ledger.reimbursements_for(conn, CLIENT, "2026-07")
    assert len(all_rows) == 1
    assert all_rows[0]["kind"] == "stipend"
    assert all_rows[0]["external_id"] == "stipend-keara-mascare-as-wifi-internet-2026-07"


def test_export_payout_writes_justworks_template_csv(tmp_path):
    import csv as _csv
    from pathlib import Path
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    cfg.raw["data_root"] = str(tmp_path)   # keep the CSV out of the real Drive folder
    from cfo_agent.adapters.payment import build_rail
    from cfo_agent.adapters.payment.justworks_manual import JW_HEADER
    a = _reimb(conn, employee="Keara Mascareñas", cents=4250,
               status="approved", ext="reimb-a")
    b = _reimb(conn, employee="Erica Seldin", cents=12000,
               status="approved", ext="reimb-b")
    res = reimburse_flow.export_payout(conn, cfg, [a, b], build_rail(cfg),
                                       pay_date="07/31/2026")
    assert res["result"]["status"] == "exported"
    assert all(r["status"] == "exported" for r in res["reimbursements"])
    # CSV must be Justworks' exact bulk-upload template.
    artifact = res["result"]["artifact"]
    rows = list(_csv.reader(Path(artifact).open()))
    assert rows[0] == JW_HEADER
    body = {r[0]: r for r in rows[1:]}   # keyed by First Name
    # Notes column is generic (no purpose/category on the paystub).
    assert body["Keara"] == ["Keara", "Mascareñas", "keara@aug.co", "07/31/2026",
                             "42.50", "Expense Reimbursement"]
    assert body["Erica"][2] == "erica@aug.co" and body["Erica"][4] == "120.00"
    assert body["Erica"][5] == "Expense Reimbursement"
    # paste text (Purvi's reference, NOT the paystub) keeps purpose + category + total
    pt = res["result"]["paste_text"]
    assert "keara@aug.co" in pt and "client lunch [Groceries & Meals]" in pt
    assert "$162.50" in pt


def _needs_info_row(conn, **over):
    """A reimbursement already in needs_info (receipt on file, category missing)."""
    r = {"external_id": over.pop("ext", "reimb-THREAD1"), "client": CLIENT,
         "entity": "August Public Inc", "employee": "Keara Mascareñas",
         "kind": "one_off", "expense_date": "2026-07-15", "close_month": "2026-07",
         "amount_cents": 10000, "business_purpose": "Monthly cell phone",
         "proposed_coa_line": None, "receipt_status": "stored",
         "receipt_link": "drive://r", "status": "needs_info", **over}
    return ledger.create_reimbursement(conn, r)


def test_thread_followup_fills_missing_category_no_resend(tmp_path, monkeypatch):
    """The adoption fix: reply in the thread with just the category — Penny keeps
    the amount/purpose/receipt it already has and completes the reimbursement."""
    import cfo_agent.engine.reimburse_parse as rp
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    rid = _needs_info_row(conn, ext="reimb-THREAD1")
    # Simulate the LLM reading "Telephone & Internet" out of the reply.
    monkeypatch.setattr(rp, "interpret_reimbursement", lambda *a, **k: {
        "amount_cents": None, "business_purpose": None,
        "proposed_coa_line": "Telephone & Internet", "expense_date": "2026-07-15"})
    res = reimburse_flow.process_followup(
        conn, cfg, "Keara Mascareñas", "UPDU3SRN0", "Telephone & Internet",
        "2026-07", slack=None, file_ids=None, thread_ts="THREAD1")
    assert res["status"] == "submitted"
    row = ledger.reimbursement_by_id(conn, rid)
    assert row["proposed_coa_line"] == "Telephone & Internet"
    assert row["amount_cents"] == 10000          # kept
    assert row["business_purpose"] == "Monthly cell phone"   # kept
    assert row["receipt_status"] == "stored"     # NOT re-uploaded
    assert "queue" in res["confirm_back"].lower()


def test_thread_followup_still_missing_uses_thread_wording(tmp_path, monkeypatch):
    import cfo_agent.engine.reimburse_parse as rp
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    # missing BOTH amount and category (amount 0), receipt on file
    _needs_info_row(conn, ext="reimb-THREAD2", amount_cents=0)
    monkeypatch.setattr(rp, "interpret_reimbursement", lambda *a, **k: {
        "amount_cents": None, "business_purpose": None,
        "proposed_coa_line": "Telephone & Internet", "expense_date": "2026-07-15"})
    res = reimburse_flow.process_followup(
        conn, cfg, "Keara Mascareñas", "UPDU3SRN0", "Telephone & Internet",
        "2026-07", slack=None, file_ids=None, thread_ts="THREAD2")
    assert res["status"] == "needs_info"          # amount still missing
    cb = res["confirm_back"].lower()
    assert "amount" in cb
    assert "reply here in this thread" in cb      # follow-up wording
    assert "starts with" not in cb                # not the re-send instruction


def test_followup_unknown_thread_returns_none(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    assert reimburse_flow.process_followup(
        conn, cfg, "Keara Mascareñas", "UPDU3SRN0", "General Travel",
        "2026-07", thread_ts="does-not-exist") is None


def test_followup_on_approved_is_readonly(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    _needs_info_row(conn, ext="reimb-THREAD3",
                    proposed_coa_line="Groceries & Meals", status="approved")
    res = reimburse_flow.process_followup(
        conn, cfg, "Keara Mascareñas", "UPDU3SRN0", "actually make it Travel",
        "2026-07", thread_ts="THREAD3")
    assert res["status"] == "approved"
    assert "already" in res["confirm_back"].lower()


def test_policy_flags_over_cell_and_internet():
    from cfo_agent.engine import reimburse_policy as pol
    cfg = load_client(CLIENT)
    # over the $100 cell-phone cap
    v = pol.check(cfg, 12000, "monthly cell phone bill", "Telephone & Internet")
    assert v and v[0]["kind"] == "limit" and v[0]["limit_cents"] == 10000
    # over the $50 internet cap
    v = pol.check(cfg, 6000, "home internet - Comcast", "Telephone & Internet")
    assert v and v[0]["limit_cents"] == 5000
    # within caps -> no violation
    assert pol.check(cfg, 9000, "monthly cell phone bill", "Telephone & Internet") == []
    assert pol.check(cfg, 4000, "home internet", "Telephone & Internet") == []


def test_policy_stale_submission():
    from cfo_agent.engine import reimburse_policy as pol
    cfg = load_client(CLIENT)
    v = pol.check(cfg, 4000, "home internet", "Telephone & Internet",
                  expense_date="2026-03-01", today="2026-07-23")
    assert any(x["kind"] == "stale" for x in v)


def test_followup_over_policy_warns_and_records(tmp_path, monkeypatch):
    import cfo_agent.engine.reimburse_parse as rp
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    _needs_info_row(conn, ext="reimb-POL", amount_cents=12000,
                    business_purpose="monthly cell phone")   # $120, over $100
    monkeypatch.setattr(rp, "interpret_reimbursement", lambda *a, **k: {
        "amount_cents": None, "business_purpose": None,
        "proposed_coa_line": "Telephone & Internet", "expense_date": "2026-07-15"})
    res = reimburse_flow.process_followup(
        conn, cfg, "Keara Mascareñas", "UPDU3SRN0", "Telephone & Internet",
        "2026-07", thread_ts="POL")
    assert res["status"] == "submitted"          # still logged (not blocked)
    assert "policy" in res["confirm_back"].lower()
    evs = ledger.reimbursement_events(conn, CLIENT, "reimb-POL")
    assert any(e["event"] == "policy_flag" for e in evs)


def test_qbo_bill_body_shape():
    from cfo_agent.adapters.books_out import qbo_writer
    b = qbo_writer.bill_body("128", "175", 5000, "2026-07-23",
                             memo="Reimbursement: Home Internet", ap_account_id="225")
    assert b["VendorRef"]["value"] == "128"
    assert b["APAccountRef"]["value"] == "225"          # credits the clearing account
    assert b["TxnDate"] == "2026-07-23"
    line = b["Line"][0]
    assert line["Amount"] == 50.0
    assert line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "175"
    assert b["PrivateNote"].startswith("Reimbursement")


def test_billpayment_body_creditcard():
    from cfo_agent.adapters.books_out import qbo_writer
    b = qbo_writer.billpayment_body("42332", "128", 5000, "2026-07-24",
                                    "231", ap_account_id="59")
    assert b["PayType"] == "CreditCard"                       # 1345 is a Credit Card acct
    assert b["CreditCardPayment"]["CCAccountRef"]["value"] == "231"
    assert b["APAccountRef"]["value"] == "59"
    assert b["Line"][0]["LinkedTxn"][0] == {"TxnId": "42332", "TxnType": "Bill"}
    assert b["TotalAmt"] == 50.0


def test_bill_reimbursement_flags_missing_category(tmp_path):
    # No category -> flagged before any QBO call (safe, offline).
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    rid = _needs_info_row(conn, ext="reimb-NOBILL")   # proposed_coa_line=None
    res = reimburse_flow.bill_reimbursement(conn, cfg, rid, post=False)
    assert res["ok"] is False
    assert any("category" in p for p in res["problems"])


def test_ddl_includes_reimbursement_tables():
    """The Postgres shim must emit valid CREATE statements for the new tables —
    and the open_db sentinel is re-pointed at `reimbursements`, so it must exist."""
    from cfo_agent.engine import db
    stmts = db.pg_statements(ledger.DDL.replace("{PK}", "BIGSERIAL PRIMARY KEY"))
    joined = " ".join(stmts)
    assert "reimbursements" in joined and "reimbursement_events" in joined
    for s in stmts:
        assert s.upper().startswith("CREATE "), f"garbage statement: {s[:60]!r}"
