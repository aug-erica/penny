"""Refund recognition in the rolling close: a card credit (QBO Purchase with
Credit=true, positive line amount) is stored negative, paired with the charge it
reverses, netted in the ledger + QBO, and never asked about."""
from datetime import date

import pytest

from cfo_agent.adapters.books_out import qbo_writer
from cfo_agent.adapters.card_feed import qbo_feed
from cfo_agent.config import load_client
from cfo_agent.engine import continuous_close as cc
from cfo_agent.engine import ledger, receipts, refunds
from cfo_agent.models import Proposal

CLIENT = "august"
CARD = "Chase Business Card (8303):Chase Business Card - A. Black (9768)"
GT_GL = qbo_writer.load_mapping(CLIENT)["General Travel"]


def _purchase(pid, amount, when, desc, credit=False, gl=qbo_writer.PENDING_ID,
              billable_customer=None):
    detail = {"AccountRef": {"value": gl}}
    if billable_customer:
        detail["BillableStatus"] = "Billable"
        detail["CustomerRef"] = {"value": billable_customer}
    p = {"Id": str(pid), "SyncToken": "0", "PaymentType": "CreditCard", "TxnDate": when,
         "TotalAmt": amount, "AccountRef": {"value": "2184", "name": CARD},
         "Line": [{"Id": "1", "Amount": amount, "Description": desc,
                   "DetailType": "AccountBasedExpenseLineDetail",
                   "AccountBasedExpenseLineDetail": detail}]}
    if credit:
        p["Credit"] = True
    return p


class _FakeQ:
    def __init__(self, purchases=None):
        self.purchases = {p["Id"]: p for p in (purchases or [])}
    def _refresh_access_token(self):
        return "t"
    def query(self, sql):
        pid = sql.split("Id = '")[1].split("'")[0] if "Id = '" in sql else None
        return [self.purchases[pid]] if pid in self.purchases else []


class _FakePenny:
    def __init__(self):
        self.dms = []
    def send_dm(self, uid, text, thread_ts=None):
        self.dms.append((uid, text))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    ops = []
    monkeypatch.setattr(qbo_writer, "commit_one", lambda q, op: ops.append(op) or {})
    monkeypatch.setattr(cc, "_categorize", lambda conn, cfg, line, learned: Proposal(
        "General Travel", "rule", "high", "test"))
    state = {"pending": []}
    monkeypatch.setattr(qbo_writer, "fetch_pending", lambda q, month: list(state["pending"]))
    monkeypatch.setattr(qbo_feed, "QBOFeed", lambda realm_id="": _FakeQ(state["pending"]))
    penny = _FakePenny()
    return conn, cfg, ops, state, penny


def _line(conn, ext):
    return ledger.line_by_external_id(conn, CLIENT, ext)


def _alexis(conn, month="2026-08"):
    return [l for l in ledger.lines_for_month(conn, CLIENT, month)
            if l["cardholder"] == "Alexis Black"]


def test_same_poll_pair_nets_silently(env):
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(1, 412.96, "2026-08-14", "SOUTHWES 5262134966910 800-435-9792 TX"),
                        _purchase(2, 412.96, "2026-08-20", "SOUTHWES 5262134966910 800-435-9792 TX",
                                  credit=True)]
    res = cc.run_once(conn, cfg, penny, "2026-08")
    charge, credit = _line(conn, "qbo-1"), _line(conn, "qbo-2")
    assert credit["amount_cents"] == -41296 and charge["amount_cents"] == 41296
    assert charge["status"] == "excluded" and credit["status"] == "excluded"
    assert credit["proposed_coa_line"] == "General Travel"
    assert "refunded 08-20" in charge["rationale"]
    # both booked to the same GL in QBO -> nets to zero on the P&L
    gls = {op["purchase"]["Id"]: op["gl_id"] for op in ops}
    assert gls == {"1": GT_GL, "2": GT_GL}
    # pair recorded; nothing to ask anyone; NO DM at all
    assert ledger.refund_links(conn, CLIENT)[0]["kind"] == "full"
    assert receipts.receipt_needed(_alexis(conn)) == []
    assert penny.dms == [] and res["dms_sent"] == 0
    # a journaled status decision so a close re-run replays the exclusion
    rows = conn.execute("SELECT * FROM decisions WHERE line_external_id='qbo-1'").fetchall()
    assert rows and rows[0]["field"] == "status"


def test_later_poll_refund_cancels_charge_with_fyi(env):
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(1, 412.96, "2026-08-14", "SOUTHWES 5262134966910")]
    cc.run_once(conn, cfg, penny, "2026-08")
    assert len(penny.dms) == 1 and "Please send a receipt" in penny.dms[0][1]
    # ...six days later the refund posts
    state["pending"].append(_purchase(2, 412.96, "2026-08-20", "SOUTHWES 5262134966910",
                                      credit=True))
    res = cc.run_once(conn, cfg, penny, "2026-08")
    assert res["new"] == 0 and res["credits"] == 1
    assert _line(conn, "qbo-1")["status"] == "excluded"
    assert receipts.receipt_needed(_alexis(conn)) == []          # receipt ask is gone
    assert len(penny.dms) == 2
    note = penny.dms[1][1]
    assert "↩️" in note and "refunded 08-20" in note and "cancels your 08-14 charge" in note
    assert "Please send a receipt" not in note
    # idempotent: a third poll re-sees both and does nothing
    res = cc.run_once(conn, cfg, penny, "2026-08")
    assert res["credits"] == 0 and len(penny.dms) == 2


def test_partial_refund_keeps_charge_live(env):
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(1, 412.96, "2026-08-14", "SOUTHWES 5262134966910")]
    cc.run_once(conn, cfg, penny, "2026-08")
    state["pending"].append(_purchase(2, 50.00, "2026-08-20", "SOUTHWES 5262134966910",
                                      credit=True))
    cc.run_once(conn, cfg, penny, "2026-08")
    charge, credit = _line(conn, "qbo-1"), _line(conn, "qbo-2")
    assert charge["status"] == "draft"                            # still live
    assert credit["status"] == "excluded" and credit["amount_cents"] == -5000
    assert ledger.refund_links(conn, CLIENT)[0]["kind"] == "partial"
    assert len(receipts.receipt_needed(_alexis(conn))) == 1       # original still owes a receipt
    note = penny.dms[1][1]
    assert "partial refund" in note and "still needs its receipt" in note


def test_unmatched_credit_is_informational(env):
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(9, 88.10, "2026-08-20", "UNITED 0164394203722", credit=True)]
    res = cc.run_once(conn, cfg, penny, "2026-08")
    credit = _line(conn, "qbo-9")
    assert credit["amount_cents"] == -8810 and credit["status"] == "flagged"
    assert credit["proposed_coa_line"] == "General Travel"
    assert ops[0]["gl_id"] == GT_GL                                # booked, not left pending
    assert ledger.refund_links(conn, CLIENT) == []
    assert receipts.receipt_needed(_alexis(conn)) == []            # never an ask
    note = penny.dms[0][1]
    assert "credit posted 08-20" in note and "couldn't find the original" in note
    assert "new charge(s)" not in note                             # not presented as a charge


def test_dry_run_writes_nothing_but_previews_note(env):
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(1, 412.96, "2026-08-14", "SOUTHWES 5262134966910"),
                        _purchase(2, 412.96, "2026-08-20", "SOUTHWES 5262134966910", credit=True)]
    res = cc.run_once(conn, cfg, penny, "2026-08", post=False)
    assert ledger.lines_for_month(conn, CLIENT, "2026-08") == [] and ops == [] and penny.dms == []
    assert res["credits"] == 1 and res["refunds"][0]["preview"]


def test_link_mirrors_billable_customer(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    ops = []
    monkeypatch.setattr(qbo_writer, "commit_one", lambda q, op: ops.append(op) or {})
    # original already booked billable to customer 611 in Billable Expense (219)
    orig_p = _purchase(1, 300.00, "2026-08-10", "DELTA AIR 0062345", gl="219", billable_customer="611")
    q = _FakeQ([orig_p])
    for pid, cents in (("1", 30000), ("2", -30000)):
        ledger.upsert_line(conn, {"external_id": f"qbo-{pid}", "client": CLIENT, "entity": "e",
                                  "close_month": "2026-08", "txn_date": "2026-08-1" + pid,
                                  "merchant_raw": "DELTA AIR 0062345", "merchant_norm": "DELTA AIR",
                                  "amount_cents": cents, "source": "card_feed",
                                  "cardholder": "Alexis Black", "status": "draft"})
    orig = _line(conn, "qbo-1")
    ledger.set_proposal(conn, orig["id"], "Billable Expense", "reviewer", "high", "x", billable=1)
    ledger.set_billable_project(conn, orig["id"], 1, "Some Project")
    credit = _line(conn, "qbo-2")
    res = refunds.link(conn, cfg, q, credit, _purchase(2, 300.00, "2026-08-12", "DELTA AIR", credit=True),
                       _line(conn, "qbo-1"), "full", qbo_writer.load_mapping(CLIENT))
    assert res["gl"] == "219" and res["customer"] == "611" and res["booked"]
    assert ops[0]["billable_customer"] == "611"                    # client isn't over-billed


def test_find_original_never_pairs_a_charge_twice(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    for pid, when in (("1", "2026-08-10"), ("2", "2026-08-11")):
        ledger.upsert_line(conn, {"external_id": f"qbo-{pid}", "client": CLIENT, "entity": "e",
                                  "close_month": "2026-08", "txn_date": when,
                                  "merchant_raw": "SOUTHWES 111", "merchant_norm": "SOUTHWES",
                                  "amount_cents": 10000, "source": "card_feed",
                                  "cardholder": "Alexis Black", "status": "draft"})
    credit = {"external_id": "qbo-3", "amount_cents": -10000, "txn_date": "2026-08-20",
              "merchant_norm": "SOUTHWES", "cardholder": "Alexis Black"}
    first, kind = refunds.find_original(conn, CLIENT, credit)
    assert (first["external_id"], kind) == ("qbo-2", "full")       # most recent first
    ledger.add_refund_link(conn, CLIENT, "qbo-3", "qbo-2", "full")
    second, kind = refunds.find_original(conn, CLIENT, dict(credit, external_id="qbo-4"))
    assert (second["external_id"], kind) == ("qbo-1", "full")
    # a different merchant never matches
    assert refunds.find_original(conn, CLIENT, dict(credit, external_id="qbo-5",
                                                    merchant_norm="LYFT")) == (None, None)


def test_backfill_repairs_credit_ingested_as_charge(env):
    conn, cfg, ops, state, penny = env
    # Before the fix, the poller stored the credit POSITIVE and DM'd it as a charge.
    charge_p = _purchase(1, 412.96, "2026-08-14", "SOUTHWES 5262134966910", gl=GT_GL)
    credit_p = _purchase(2, 412.96, "2026-08-20", "SOUTHWES 5262134966910", credit=True, gl=GT_GL)
    for p in (charge_p, credit_p):
        ledger.upsert_line(conn, {"external_id": f"qbo-{p['Id']}", "client": CLIENT, "entity": "e",
                                  "close_month": "2026-08", "txn_date": p["TxnDate"],
                                  "merchant_raw": "SOUTHWES 5262134966910", "merchant_norm": "SOUTHWES",
                                  "amount_cents": 41296, "source": "card_feed",
                                  "cardholder": "Alexis Black", "status": "draft"})
        ledger.set_proposal(conn, _line(conn, f"qbo-{p['Id']}")["id"], "General Travel", "llm", "low", "x")
    assert len(receipts.receipt_needed(_alexis(conn))) == 2       # the bug: two receipt asks
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(qbo_writer, "fetch_month", lambda q, month: [charge_p, credit_p])
    try:
        dry = refunds.backfill(conn, cfg, _FakeQ([charge_p, credit_p]), "2026-08", post=False)
        assert dry["linked"][0][1] == "full" and _line(conn, "qbo-2")["amount_cents"] == 41296
        res = refunds.backfill(conn, cfg, _FakeQ([charge_p, credit_p]), "2026-08", post=True)
    finally:
        monkeypatch.undo()
    assert res["linked"][0][:3] == ("qbo-2", "full", "qbo-1")
    assert _line(conn, "qbo-2")["amount_cents"] == -41296
    assert _line(conn, "qbo-1")["status"] == "excluded" and _line(conn, "qbo-2")["status"] == "excluded"
    assert receipts.receipt_needed(_alexis(conn)) == []
    assert "Alexis Black" in res["notes"] and "cancels your 08-14 charge" in res["notes"]["Alexis Black"][0]
    assert ops[-1]["purchase"]["Id"] == "2" and ops[-1]["gl_id"] == GT_GL


def test_find_original_blank_descriptor_pairs_only_when_unique(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    for pid, cents in (("1", 41296), ("2", 9900)):
        ledger.upsert_line(conn, {"external_id": f"qbo-{pid}", "client": CLIENT, "entity": "e",
                                  "close_month": "2026-08", "txn_date": "2026-08-1" + pid,
                                  "merchant_raw": "SOUTHWES 111", "merchant_norm": "SOUTHWES",
                                  "amount_cents": cents, "source": "card_feed",
                                  "cardholder": "Alexis Black", "status": "draft"})
    blank = {"external_id": "qbo-3", "amount_cents": -41296, "txn_date": "2026-08-20",
             "merchant_norm": "", "cardholder": "Alexis Black"}
    assert refunds.find_original(conn, CLIENT, blank)[0]["external_id"] == "qbo-1"
    # two identical charges -> ambiguous -> no pairing
    ledger.upsert_line(conn, {"external_id": "qbo-4", "client": CLIENT, "entity": "e",
                              "close_month": "2026-08", "txn_date": "2026-08-13",
                              "merchant_raw": "LYFT", "merchant_norm": "LYFT", "amount_cents": 41296,
                              "source": "card_feed", "cardholder": "Alexis Black", "status": "draft"})
    assert refunds.find_original(conn, CLIENT, blank) == (None, None)


def test_two_partials_that_add_up_are_a_full_refund(env):
    # Alexis, Aug 2026: $775.80 Southwest fare refunded as $370.40 (08-13) + $405.40 (08-19).
    conn, cfg, ops, state, penny = env
    state["pending"] = [_purchase(1, 775.80, "2026-08-10", "SOUTHWES XXXXXXXXX7287")]
    cc.run_once(conn, cfg, penny, "2026-08")
    state["pending"].append(_purchase(2, 370.40, "2026-08-13", "SOUTHWES XXXXXXXXX7287", credit=True))
    cc.run_once(conn, cfg, penny, "2026-08")
    assert _line(conn, "qbo-1")["status"] == "draft"                 # half back: still live
    assert "partial refund" in penny.dms[1][1]
    state["pending"].append(_purchase(3, 405.40, "2026-08-19", "SOUTHWES XXXXXXXXX4196", credit=True))
    cc.run_once(conn, cfg, penny, "2026-08")
    charge = _line(conn, "qbo-1")
    assert charge["status"] == "excluded" and "refunded in full" in charge["rationale"]
    assert receipts.receipt_needed(_alexis(conn)) == []               # receipt ask is gone
    assert "fully refunds your 08-10 $775.80 charge" in penny.dms[2][1]
    assert {op["gl_id"] for op in ops} == {GT_GL}                      # all three coded alike
    # a later same-amount credit can't pair to the now fully-refunded charge
    assert refunds.find_original(conn, CLIENT, {"external_id": "qbo-9", "amount_cents": -77580,
                                                "txn_date": "2026-08-25", "merchant_norm": "SOUTHWES",
                                                "cardholder": "Alexis Black"}) == (None, None)


def test_backfill_dry_run_shows_summed_partials_as_full(env):
    conn, cfg, ops, state, penny = env
    ps = [_purchase(1, 775.80, "2026-08-10", "SOUTHWES XXXXXXXXX7287", gl=GT_GL),
          _purchase(2, 370.40, "2026-08-13", "SOUTHWES XXXXXXXXX7287", credit=True, gl=GT_GL),
          _purchase(3, 405.40, "2026-08-19", "SOUTHWES XXXXXXXXX4196", credit=True, gl=GT_GL)]
    for p in ps:
        ledger.upsert_line(conn, {"external_id": f"qbo-{p['Id']}", "client": CLIENT, "entity": "e",
                                  "close_month": "2026-08", "txn_date": p["TxnDate"],
                                  "merchant_raw": p["Line"][0]["Description"], "merchant_norm": "SOUTHWES",
                                  "amount_cents": round(p["TotalAmt"] * 100), "source": "card_feed",
                                  "cardholder": "Alexis Black", "status": "draft"})
    mp = pytest.MonkeyPatch(); mp.setattr(qbo_writer, "fetch_month", lambda q, m: ps)
    try:
        dry = refunds.backfill(conn, cfg, _FakeQ(ps), "2026-08", post=False)
        assert [k for _, k, _, _ in dry["linked"]] == ["partial", "full"]
        assert _line(conn, "qbo-1")["status"] == "draft"              # dry run wrote nothing
        res = refunds.backfill(conn, cfg, _FakeQ(ps), "2026-08", post=True)
    finally:
        mp.undo()
    assert _line(conn, "qbo-1")["status"] == "excluded"
    assert receipts.receipt_needed(_alexis(conn)) == []
