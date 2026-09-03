"""Fixes from Mike's Aug-2026 United thread: one receipt covering two named charges
links to BOTH; a project named mid-message is found; "what's outstanding?" gets a
status answer instead of the reimbursement/card clarifier."""
from datetime import date
from pathlib import Path

import pytest

from cfo_agent.config import load_client
from cfo_agent.engine import (dm_assemble, intent, ledger, projects, receipt_read,
                              receipt_store, reply_flow, reply_parse)
from cfo_agent.engine import projects as pj
from cfo_agent.engine.normalize import card_txn_to_line
from cfo_agent.adapters.projects import hubspot_client
from cfo_agent.models import CardTxn

CLIENT = "august"
LLDM = "Link Logistics Decision Making"
_real_interpret = reply_parse.interpret_reply    # the autouse fixture stubs the module attr


@pytest.fixture(autouse=True)
def _no_net(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    monkeypatch.setattr(hubspot_client, "search_closed_won", lambda t, limit=10: [])
    monkeypatch.setattr(reply_parse, "interpret_reply", lambda *a, **k: [])


def _united(conn):
    """Mike's two United tickets, both over the receipt threshold."""
    cfg = load_client(CLIENT)
    ids = []
    for i, (last4, cents) in enumerate((("2038", 13438), ("8761", 45778))):
        t = CardTxn(date(2026, 8, 17 + i), f"UNITED XXXXXXXXX{last4}", cents,
                    "Mike Arauz", f"8311:2026-08-{17 + i}")
        rid = ledger.upsert_line(conn, card_txn_to_line(t, CLIENT, "August Public Inc", "2026-08"))
        ledger.set_proposal(conn, rid, "General Travel", "rule", "high", "seed", status="flagged")
        ids.append(rid)
    return cfg, ids


class _Slack:
    def download_file(self, fid, dest):
        Path(dest).write_bytes(b"%PDF-1.4 fake eticket")
        return dest


def test_one_receipt_two_named_charges_links_both(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _united(conn)
    monkeypatch.setattr(receipt_read, "read_receipt", lambda p: {})
    monkeypatch.setattr(reply_flow, "_amounts_in_file", lambda p: set())
    monkeypatch.setattr(receipt_store, "store_file",
                        lambda cfg, month, who, target, tmp: tmp_path / f"r{target['id']}.pdf")
    res = reply_flow.process_pal_reply(
        conn, cfg, "Mike Arauz",
        "receipt for\nUNITED XXXXXXXXX2038 $134.38\nUNITED XXXXXXXXX8761 $457.78\n\n"
        "billable to Link Logistics Decision Making, flights for Beaver Creek workshop",
        "2026-08", slack=_Slack(), file_ids=["F1"])
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-08")}
    assert lines[ids[0]]["receipt_status"] == "stored"
    assert lines[ids[1]]["receipt_status"] == "stored"       # the second one used to stay "needed"
    assert len(res["filed"]) == 2
    assert "Still need receipts" not in res["confirm_back"]


def test_one_receipt_one_amount_links_one(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _united(conn)
    monkeypatch.setattr(receipt_read, "read_receipt", lambda p: {})
    monkeypatch.setattr(reply_flow, "_amounts_in_file", lambda p: set())
    monkeypatch.setattr(receipt_store, "store_file",
                        lambda cfg, month, who, target, tmp: tmp_path / f"r{target['id']}.pdf")
    res = reply_flow.process_pal_reply(conn, cfg, "Mike Arauz", "receipt for $457.78",
                                       "2026-08", slack=_Slack(), file_ids=["F1"])
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-08")}
    assert lines[ids[0]]["receipt_status"] is None and lines[ids[1]]["receipt_status"] == "stored"


def test_named_charges_get_the_project_not_everything(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _united(conn)
    # a third travel charge the pal did NOT mention must stay untouched
    t = CardTxn(date(2026, 8, 24), "PARK HYATT BEAVER CREEK", 8049, "Mike Arauz", "8311:2026-08-24")
    other = ledger.upsert_line(conn, card_txn_to_line(t, CLIENT, "August Public Inc", "2026-08"))
    ledger.set_proposal(conn, other, "General Travel", "rule", "high", "seed", status="flagged")
    monkeypatch.setattr(pj, "resolve", lambda c, m, text: (LLDM, None))
    monkeypatch.setattr(reply_flow, "_rewrite_qbo_billable", lambda *a, **k: None)
    res = reply_flow.process_pal_reply(
        conn, cfg, "Mike Arauz",
        "UNITED XXXXXXXXX2038 $134.38 (08-17)\n\nthis is billable to Link Logistics Decision "
        "Making, flights for Beaver Creek", "2026-08")
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-08")}
    assert lines[ids[0]]["billable"] == 1 and lines[ids[0]]["project"] == LLDM
    assert lines[ids[1]]["billable"] is None                  # not named -> not touched
    assert lines[other]["billable"] is None
    assert res["decisions"] == 1


def test_not_billable_with_named_amount_is_scoped(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _united(conn)
    reply_flow.process_pal_reply(conn, cfg, "Mike Arauz", "the $134.38 one is not billable", "2026-08")
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-08")}
    assert lines[ids[0]]["billable"] == 0 and lines[ids[1]]["billable"] is None


def test_phrases_find_project_mid_message():
    text = ("receipt for\nUNITED XXXXXXXXX2038 $134.38\nUNITED XXXXXXXXX8761 $457.78\n\n"
            "billable to Link Logistics Decision Making, flights for Beaver Creek workshop")
    ph = projects._phrases(text)
    assert ph[0] == "Link Logistics Decision Making"
    assert "UNITED XXXXXXXXX2038" not in ph and not any("134" in x for x in ph)
    assert projects._phrases("this is billable, for the Gilead Manufacturing project") \
        [0].startswith("Gilead Manufacturing")


def test_hubspot_search_tries_project_phrase_first(monkeypatch):
    asked = []
    def fake(q, limit=10):
        asked.append(q)
        return [{"project": LLDM, "client": ""}] if q == LLDM else []
    monkeypatch.setattr(hubspot_client, "search_closed_won", fake)
    hits = projects._hubspot_matches(
        "receipt for UNITED XXXXXXXXX2038 $134.38 billable to Link Logistics Decision Making, "
        "flights for Beaver Creek workshop")
    assert hits and hits[0]["project"] == LLDM
    assert asked[0] == LLDM                                   # not "receipt UNITED ..."


def test_llm_project_outside_active_list_is_verified_via_hubspot(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    class _Msg:
        content = [type("C", (), {"text": '[{"i": 0, "billable": true, "project": "%s", "why": "x"}]' % LLDM})()]
    class _Client:
        def __init__(self, api_key): pass
        class messages:
            @staticmethod
            def create(**kw): return _Msg()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr(hubspot_client, "search_closed_won",
                        lambda t, limit=10: [{"project": LLDM, "client": ""}] if "Decision" in t else [])
    charges = [{"external_id": "qbo-1", "txn_date": "2026-08-17", "merchant_raw": "UNITED",
                "amount_cents": 13438, "proposed_coa_line": "General Travel"}]
    out = _real_interpret(CLIENT, "billable to " + LLDM, charges,
                          ["Link Logistics Governance Phase 2"])
    assert out and out[0]["billable"] is True and out[0]["project"] == LLDM


@pytest.mark.parametrize("text,expected", [
    ("what is outstanding? is everything up to date?", True),
    ("do i have any outstanding charges? or anything you need?", True),
    ("am I all set?", True),
    ("status", True),
    ("reimburse $42 client lunch", False),
    ("the $134.38 one is not billable", False),
    ("receipt for UNITED $457.78", False),
])
def test_status_question_detection(text, expected):
    assert intent.is_status_question(text) is expected
    assert intent.is_status_question(text, has_receipt=True) is False


def test_status_reply_lists_open_items_or_all_set(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _united(conn)
    charges = reply_flow._pal_charges(conn, CLIENT, "2026-08", "Mike Arauz")
    txt = dm_assemble.status_reply("Mike", charges, "2026-08", open_reimbursements=1)
    assert "Still need a call" in txt and "Still need receipts" in txt and "1 reimbursement" in txt
    for rid in ids:
        ledger.set_billable_project(conn, rid, 0, None)
        ledger.set_receipt_status(conn, rid, "stored", "x")
    charges = reply_flow._pal_charges(conn, CLIENT, "2026-08", "Mike Arauz")
    assert "all set" in dm_assemble.status_reply("Mike", charges, "2026-08")
