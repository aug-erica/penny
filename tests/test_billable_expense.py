"""Natalie's rule (Aug 2026): a charge billable to a client must land in the
*Billable Expense* GL account (5040, qbo_id 219) — not its natural category
(General Travel, Friday Lunch, …) — so the T&E invoice reads cleanly. Enforced in
reply_flow._rewrite_qbo_billable at the QBO write."""
from cfo_agent.adapters.books_out import qbo_writer
from cfo_agent.engine import reply_flow

CLIENT = "august"
BILLABLE_EXPENSE_ID = "219"          # qbo_id for "Billable Expense" in qbo_accounts.yaml


class _FakeCfg:
    client = CLIENT


class _FakeQ:
    """Stands in for a QBOFeed — returns a single-line Purchase currently coded to
    some ordinary category ('334' = Friday Lunch)."""
    def query(self, _sql):
        return [{
            "Id": "42283", "SyncToken": "0", "PaymentType": "CreditCard",
            "AccountRef": {"value": "305"},
            "Line": [{"Id": "1", "Amount": 54.45, "DetailType": "AccountBasedExpenseLineDetail",
                      "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "334"}}}],
        }]


def test_billable_to_client_moves_to_billable_expense(monkeypatch):
    captured = {}
    monkeypatch.setattr(qbo_writer, "commit_one", lambda q, op: captured.update(op))
    cust = reply_flow._rewrite_qbo_billable(
        _FakeCfg(), "qbo-42283", "Waymo People Development", customer="611", q=_FakeQ())
    assert cust == "611"
    assert captured["gl_id"] == BILLABLE_EXPENSE_ID          # flipped off Friday Lunch (334)
    assert captured["billable_customer"] == "611"


def test_no_matched_customer_keeps_category(monkeypatch):
    captured = {}
    monkeypatch.setattr(qbo_writer, "commit_one", lambda q, op: captured.update(op))
    monkeypatch.setattr(qbo_writer, "resolve_customer", lambda *a, **k: None)
    cust = reply_flow._rewrite_qbo_billable(
        _FakeCfg(), "qbo-42283", "Unknown Project", q=_FakeQ())
    assert cust is None
    assert captured["gl_id"] == "334"                        # unchanged — no client, no flip
    assert "billable_customer" not in captured


def test_not_billable_clears_billable_flag_in_qbo(monkeypatch):
    # The mirror rule: "not billable" must clear the stale Billable flag + customer
    # in QBO (the drift Natalie flagged), not just the ledger.
    captured = {}
    monkeypatch.setattr(qbo_writer, "commit_one", lambda q, op: captured.update(op))
    reply_flow._unbill_qbo(_FakeCfg(), "qbo-42289", q=_FakeQ())
    assert captured["unbill"] is True
    assert "billable_customer" not in captured


def test_commit_one_unbill_sets_notbillable_and_drops_customer():
    # commit_one honors the unbill op: NotBillable + no CustomerRef on the line.
    purchase = {
        "Id": "42289", "SyncToken": "1", "PaymentType": "CreditCard",
        "AccountRef": {"value": "305"},
        "Line": [{"Id": "1", "Amount": 284.82, "DetailType": "AccountBasedExpenseLineDetail",
                  "AccountBasedExpenseLineDetail": {
                      "AccountRef": {"value": "184"}, "BillableStatus": "Billable",
                      "CustomerRef": {"value": "611"}}}]}
    sent = {}

    class _CaptureQ:
        _access_token = "t"
        base = "https://x"; realm_id = "1"
        def _refresh_access_token(self): pass

    import cfo_agent.adapters.books_out.qbo_writer as w

    class _Resp:
        status_code = 200
        def json(self): return {}
    orig = w.httpx.post
    w.httpx.post = lambda url, headers=None, json=None, timeout=None: (
        sent.update(json) or _Resp())
    try:
        w.commit_one(_CaptureQ(), {"purchase": purchase, "unbill": True})
    finally:
        w.httpx.post = orig
    det = sent["Line"][0]["AccountBasedExpenseLineDetail"]
    assert det["BillableStatus"] == "NotBillable"
    assert "CustomerRef" not in det
