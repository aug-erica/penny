"""Card-reply flow: a collective billable-project reply ("both of these are to X")
applies to the charges Penny just asked about; an ambiguous project asks which."""
from datetime import date

import pytest

from cfo_agent.config import load_client
from cfo_agent.engine import ledger, reply_flow, reply_parse
from cfo_agent.engine import projects as pj
from cfo_agent.engine.normalize import card_txn_to_line
from cfo_agent.adapters.projects import hubspot_client
from cfo_agent.models import CardTxn

CLIENT = "august"
PPFA = "PPFA OOP FY27 Advisory, Design and Facilitation"


@pytest.fixture(autouse=True)
def _no_net(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    monkeypatch.setattr(hubspot_client, "search_closed_won", lambda t, limit=10: [])
    # The interpreter catches nothing specific -> the collective fallback runs.
    monkeypatch.setattr(reply_parse, "interpret_reply", lambda *a, **k: [])


def _seed(conn, n=2):
    """n billable-candidate card charges for Karina, awaiting a project."""
    cfg = load_client(CLIENT)
    ids = []
    for i in range(n):
        t = CardTxn(date(2026, 7, 20 + i), f"DELTA AIR {i}", 108679 + i,
                    "Karina Mangu-Ward", f"9867:2026-07-{20 + i}")
        rid = ledger.upsert_line(conn, card_txn_to_line(t, CLIENT, "August Public Inc", "2026-07"))
        ledger.set_proposal(conn, rid, "General Travel", "rule", "high", "seed", status="flagged")
        ids.append(rid)
    return cfg, ids


def test_collective_project_applies_to_awaiting(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _seed(conn, 2)
    monkeypatch.setattr(pj, "resolve", lambda c, m, text: (PPFA, None))
    res = reply_flow.process_pal_reply(
        conn, cfg, "Karina Mangu-Ward",
        f"both of these are to {PPFA}", "2026-07")
    assert res["decisions"] == 2
    lines = {l["id"]: l for l in ledger.lines_for_month(conn, CLIENT, "2026-07")}
    for rid in ids:
        assert lines[rid]["billable"] == 1
        assert lines[rid]["project"] == PPFA
    assert "didn't catch" not in res["confirm_back"].lower()


def test_ambiguous_project_asks_which(tmp_path, monkeypatch):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _seed(conn, 1)
    monkeypatch.setattr(pj, "resolve", lambda c, m, text: (None, ["PPFA Alpha", "PPFA Beta"]))
    res = reply_flow.process_pal_reply(conn, cfg, "Karina Mangu-Ward",
                                       "these are PPFA", "2026-07")
    assert res["project_unresolved"]
    cb = res["confirm_back"].lower()
    assert "which project" in cb and "ppfa alpha" in cb
    # nothing assigned while ambiguous
    lines = ledger.lines_for_month(conn, CLIENT, "2026-07")
    assert all(l["billable"] is None for l in lines)


def test_specific_charge_decision_skips_collective_fallback(tmp_path, monkeypatch):
    # If the interpreter DID make a per-charge call, the collective fallback must
    # not also blanket-apply — resolve() raising if called proves it doesn't run.
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    cfg, ids = _seed(conn, 2)
    ext0 = ledger.lines_for_month(conn, CLIENT, "2026-07")[0]["external_id"]
    monkeypatch.setattr(reply_parse, "interpret_reply", lambda *a, **k: [
        {"external_id": ext0, "merchant": "DELTA AIR 0", "billable": True,
         "project": PPFA, "why": "x"}])

    def _boom(*a, **k):
        raise AssertionError("collective fallback ran despite a specific decision")

    monkeypatch.setattr(pj, "resolve", _boom)
    res = reply_flow.process_pal_reply(conn, cfg, "Karina Mangu-Ward",
                                       f"the delta flight is {PPFA}", "2026-07")
    assert res["decisions"] == 1
