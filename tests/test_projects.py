"""Project resolution: active list first, live HubSpot as the breadth fallback
so out-of-window / long-closed projects (Waymo, older Gilead) still resolve."""
from cfo_agent.config import load_client
from cfo_agent.engine import projects
from cfo_agent.adapters.projects import hubspot_client

CLIENT = "august"


def test_resolves_from_active_list(monkeypatch):
    cfg = load_client(CLIENT)
    monkeypatch.setattr(projects, "active_names",
                        lambda c, m: ["Colgate Skin Health – Reorg Support"])
    monkeypatch.setattr(hubspot_client, "search_closed_won", lambda t, limit=10: [])
    proj, opts = projects.resolve(cfg, "2026-07", "Colgate")
    assert proj == "Colgate Skin Health – Reorg Support"


def test_hubspot_fallback_finds_out_of_window(monkeypatch):
    cfg = load_client(CLIENT)
    # Active list has NO Waymo; HubSpot returns it -> resolves anyway.
    monkeypatch.setattr(projects, "active_names",
                        lambda c, m: ["Colgate Skin Health – Reorg Support"])
    monkeypatch.setattr(hubspot_client, "search_closed_won",
                        lambda t, limit=10: [{"project": "Waymo People Development", "client": ""}]
                        if "waymo" in t.lower() else [])
    proj, opts = projects.resolve(cfg, "2026-07", "Waymo People Development")
    assert proj == "Waymo People Development"


def test_ambiguous_returns_options(monkeypatch):
    cfg = load_client(CLIENT)
    monkeypatch.setattr(projects, "active_names", lambda c, m: [])
    monkeypatch.setattr(hubspot_client, "search_closed_won", lambda t, limit=10: [
        {"project": "Genentech L&SD Sprint", "client": ""},
        {"project": "Genentech External Affairs Leadership Forum", "client": ""}])
    proj, opts = projects.resolve(cfg, "2026-07", "Genentech")
    assert proj is None and opts and len(opts) == 2


def test_no_match(monkeypatch):
    cfg = load_client(CLIENT)
    monkeypatch.setattr(projects, "active_names", lambda c, m: ["Colgate Skin Health"])
    monkeypatch.setattr(hubspot_client, "search_closed_won", lambda t, limit=10: [])
    assert projects.resolve(cfg, "2026-07", "Nonexistent Corp") == (None, None)


def test_search_closed_won_no_token(monkeypatch):
    # No HUBSPOT_TOKEN -> empty (degrade to active-only), never raises.
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    assert hubspot_client.search_closed_won("anything") == []
