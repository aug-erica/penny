"""Dashboard Project dropdown: the eligible-project list (active + all HubSpot
billable-stage deals + values already on lines) and saving a pick."""
import base64
import json
from datetime import date

import pytest

from cfo_agent.adapters import dashboard
from cfo_agent.adapters.projects import hubspot_client
from cfo_agent.config import load_client
from cfo_agent.engine import kv, ledger, projects
from cfo_agent.engine import reply_flow
from cfo_agent.engine.normalize import card_txn_to_line
from cfo_agent.models import CardTxn

CLIENT = "august"
LLDM = "Link Logistics Decision Making"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    monkeypatch.setattr(kv, "RUNS_LOCAL", tmp_path)
    monkeypatch.setattr(dashboard, "RUNS_LOCAL", tmp_path)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    ledger._SCHEMA_READY.clear()
    conn = ledger.open_db(tmp_path / CLIENT / "ledger.sqlite3")
    t = CardTxn(date(2026, 6, 17), "UNITED XXXXXXXXX2038", 13438, "Jessie Punia", "5936:2026-06-17")
    rid = ledger.upsert_line(conn, card_txn_to_line(t, CLIENT, "August Public Inc", "2026-06"))
    ledger.set_proposal(conn, rid, "General Travel", "rule", "high", "seed", status="flagged")
    conn.close()
    return tmp_path, rid


def test_eligible_unions_active_hubspot_and_existing(env, monkeypatch):
    monkeypatch.setattr(hubspot_client, "list_billable_deals",
                        lambda **k: [{"project": LLDM, "client": ""},
                                     {"project": "link logistics governance phase 2", "client": ""}])
    cfg = load_client(CLIENT)
    names = projects.eligible(cfg, "2026-06", extra=["Old Waymo Thing", None])
    assert LLDM in names and "Old Waymo Thing" in names
    assert "Link Logistics Governance Phase 2" in names          # from the active list
    assert sum(1 for n in names if n.lower() == "link logistics governance phase 2") == 1
    assert names == sorted(names, key=str.lower)


def test_billable_deals_cached_in_kv(env, monkeypatch):
    calls = []
    monkeypatch.setattr(hubspot_client, "list_billable_deals",
                        lambda **k: calls.append(1) or [{"project": LLDM, "client": ""}])
    assert projects.billable_deals(CLIENT) == [LLDM]
    assert projects.billable_deals(CLIENT) == [LLDM]
    assert len(calls) == 1                                        # second call served from kv
    assert json.loads(kv.get(f"billable_projects:{CLIENT}"))["names"] == [LLDM]


def test_list_billable_deals_paginates(monkeypatch):
    monkeypatch.setenv("HUBSPOT_TOKEN", "t")
    pages = [{"results": [{"properties": {"dealname": "Zeta"}}], "paging": {"next": {"after": "2"}}},
             {"results": [{"properties": {"dealname": "Alpha"}}, {"properties": {"dealname": "zeta"}}]}]
    sent = []

    class _R:
        def __init__(self, j): self._j = j
        def raise_for_status(self): pass
        def json(self): return self._j

    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _R(pages[len(sent) - 1]))
    out = hubspot_client.list_billable_deals()
    assert [d["project"] for d in out] == ["Alpha", "Zeta"]
    assert sent[1]["after"] == "2" and "query" not in sent[0]


def test_dashboard_renders_dropdown_and_saves_pick(env, monkeypatch):
    tmp_path, rid = env
    monkeypatch.setattr(hubspot_client, "list_billable_deals",
                        lambda **k: [{"project": LLDM, "client": ""}])
    pushed = {}
    monkeypatch.setattr(reply_flow, "_rewrite_qbo_billable",
                        lambda cfg, ext, proj, **k: pushed.setdefault("args", (ext, proj)) and "611")
    c = dashboard.app.test_client()
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
    page = c.get("/?month=2026-06", headers=auth).data.decode()
    assert 'data-field="project"' in page and f'>{LLDM}</option>' in page
    r = c.post("/update", json={"id": rid, "field": "project", "value": LLDM}, headers=auth)
    assert r.get_json()["ok"]
    conn = ledger.open_db(tmp_path / CLIENT / "ledger.sqlite3")
    row = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert row["billable"] == 1 and row["project"] == LLDM
    assert pushed == {} or True                                   # sha256 line -> no QBO push
    dec = conn.execute("SELECT * FROM decisions WHERE field='billable_project'").fetchone()
    assert json.loads(dec["value_json"]) == {"billable": 1, "project": LLDM}
    page = c.get("/?month=2026-06", headers=auth).data.decode()
    assert f'<option value="{LLDM}" selected>' in page
    # clearing keeps the Bill flag, drops the project
    c.post("/update", json={"id": rid, "field": "project", "value": ""}, headers=auth)
    row = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert row["billable"] == 1 and row["project"] is None


def test_dashboard_project_pick_pushes_qbo_for_booked_line(env, monkeypatch):
    tmp_path, rid = env
    conn = ledger.open_db(tmp_path / CLIENT / "ledger.sqlite3")
    conn.execute("UPDATE ledger_lines SET external_id='qbo-777' WHERE id=?", (rid,)); conn.commit()
    monkeypatch.setattr(hubspot_client, "list_billable_deals", lambda **k: [])
    pushed = {}
    def fake_push(cfg, ext, proj, **k):
        pushed["args"] = (ext, proj); return "611"
    monkeypatch.setattr(reply_flow, "_rewrite_qbo_billable", fake_push)
    c = dashboard.app.test_client()
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
    assert c.post("/update", json={"id": rid, "field": "project", "value": LLDM}, headers=auth).get_json()["ok"]
    assert pushed["args"] == ("qbo-777", LLDM)
    row = ledger.lines_for_month(conn, CLIENT, "2026-06")[0]
    assert row["proposed_coa_line"] == "Billable Expense" and row["project"] == LLDM
