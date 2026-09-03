"""One-time 'your month is complete' notices: per-pal DM once their prior month
is clear, one #finance all-clear once everyone is, never twice, never for the
month in progress."""
from datetime import date, datetime

import pytest

from cfo_agent.config import load_client
from cfo_agent.engine import ledger, month_complete as mc

CLIENT = "august"


class _FakePenny:
    def __init__(self):
        self.dms, self.posts = [], []
    def send_dm(self, uid, text, thread_ts=None):
        self.dms.append((uid, text))
    def _post(self, method, **payload):
        self.posts.append((method, payload))
        return {"ok": True}


def _seed(conn, ext, who, cents, when="2026-08-10", coa="Groceries & Meals", **extra):
    ledger.upsert_line(conn, {"external_id": ext, "client": CLIENT, "entity": "e",
                              "close_month": "2026-08", "txn_date": when,
                              "merchant_raw": ext.upper(), "merchant_norm": ext.upper(),
                              "amount_cents": cents, "source": "card_feed", "cardholder": who,
                              "status": extra.pop("status", "draft")})
    l = ledger.line_by_external_id(conn, CLIENT, ext)
    if coa:
        ledger.set_proposal(conn, l["id"], coa, "rule", "high", "seed")
    return l


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg = load_client(CLIENT)
    # Shrink enrolment so "everyone" is two people.
    cfg.raw["continuous_close"]["cardholders"] = ["Alexis Black", "Erica Seldin"]
    return conn, cfg, _FakePenny()


def test_eligible_month_is_previous_month_after_grace():
    assert mc.eligible_month(datetime(2026, 9, 3), 3) is None
    assert mc.eligible_month(datetime(2026, 9, 4), 3) == "2026-08"
    assert mc.eligible_month(datetime(2026, 1, 15), 3) == "2025-12"


def test_open_items_counts_notes_and_ignores_credits_and_excluded():
    charges = [
        {"status": "draft", "amount_cents": 15000, "proposed_coa_line": "General Travel",
         "billable": 1, "project": "P", "receipt_status": "stored", "billable_note": None},
        {"status": "excluded", "amount_cents": 40000, "proposed_coa_line": None},
        {"status": "flagged", "amount_cents": -4000, "proposed_coa_line": None},
    ]
    st = mc.open_items(charges)
    assert st["n"] == 1 and st["open"] == 1 and st["need_note"] == 1


def test_pal_notified_once_then_team_once(env):
    conn, cfg, penny = env
    _seed(conn, "a1", "Alexis Black", 15000, coa="General Travel")   # $150 travel: receipt + project
    _seed(conn, "e1", "Erica Seldin", 2223)                          # small meal: nothing owed
    res = mc.run_once(conn, cfg, penny, "2026-08")
    assert res["pals_notified"] == ["Erica Seldin"] and res["still_open"] == {"Alexis Black": 2}
    assert not res["team_posted"] and len(penny.dms) == 1
    assert "August card is all wrapped up" in penny.dms[0][1] and "Hi Erica" in penny.dms[0][1]
    # Alexis answers: not billable + receipt
    a = ledger.line_by_external_id(conn, CLIENT, "a1")
    ledger.set_billable_project(conn, a["id"], 0, None)
    ledger.set_receipt_status(conn, a["id"], "stored", "x")
    res = mc.run_once(conn, cfg, penny, "2026-08")
    assert res["pals_notified"] == ["Alexis Black"] and res["already"] == ["Erica Seldin"]
    assert res["team_posted"] and len(penny.dms) == 2 and len(penny.posts) == 1
    post = penny.posts[0][1]
    assert post["channel"] == cfg.raw["bot"]["digest_channel"]
    assert "August 2026 card close — all clear" in post["text"] and "2/2 cardholders" in post["text"]
    assert "receipts in" in penny.dms[1][1]
    # Third tick: nothing repeats.
    res = mc.run_once(conn, cfg, penny, "2026-08")
    assert res["pals_notified"] == [] and res["team_already"] and not res["team_posted"]
    assert len(penny.dms) == 2 and len(penny.posts) == 1


def test_refunded_pair_does_not_block_or_count(env):
    conn, cfg, penny = env
    _seed(conn, "a1", "Alexis Black", 41296, coa="General Travel", status="excluded")
    _seed(conn, "a2", "Alexis Black", -41296, coa="General Travel", status="excluded")
    _seed(conn, "a3", "Alexis Black", 1200)
    _seed(conn, "e1", "Erica Seldin", 2223)
    res = mc.run_once(conn, cfg, penny, "2026-08")
    assert sorted(res["pals_notified"]) == ["Alexis Black", "Erica Seldin"] and res["team_posted"]
    assert "1 charge categorized" in [t for u, t in penny.dms if u == "UDFF86K8V"][0]
    assert "2 charges" in penny.posts[0][1]["text"]


def test_pal_with_only_excluded_lines_is_skipped(env):
    conn, cfg, penny = env
    _seed(conn, "a1", "Alexis Black", 41296, status="excluded")
    _seed(conn, "e1", "Erica Seldin", 2223)
    res = mc.run_once(conn, cfg, penny, "2026-08")
    assert res["pals_notified"] == ["Erica Seldin"] and res["n_pals"] == 1


def test_dry_run_sends_nothing(env):
    conn, cfg, penny = env
    _seed(conn, "e1", "Erica Seldin", 2223)
    res = mc.run_once(conn, cfg, penny, "2026-08", post=False)
    assert res["pals_notified"] == ["Erica Seldin"] and penny.dms == [] and penny.posts == []
    assert not ledger.notice_sent(conn, CLIENT, "2026-08", "pal_complete", "Erica Seldin")
