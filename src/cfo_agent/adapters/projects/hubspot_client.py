"""Live HubSpot lookup for Closed-Won deals by name.

Lets Penny bill to a project that isn't in the current close month's active list —
a long-closed Waymo project, or a Gilead deal outside this month's service window.
Read-only, best-effort: no HUBSPOT_TOKEN or any error -> [] and the caller falls
back to the cached active-project list (today's behavior).
"""
from __future__ import annotations

from ...config import env
from .hubspot_projects import BILLABLE_STAGE_IDS

_SEARCH = "https://api.hubapi.com/crm/v3/objects/deals/search"


def search_closed_won(text: str, limit: int = 10) -> list:
    """Billable-stage deals (Closed Won OR Gain-Approval) whose name matches
    `text`, as [{project, client}]. Empty when there's no token, no query, or on
    any API error (never raises)."""
    token = env("HUBSPOT_TOKEN")
    if not token or not (text or "").strip():
        return []
    body = {
        "query": text.strip()[:100],
        "limit": max(1, min(limit, 50)),
        "properties": ["dealname", "dealstage"],
        "filterGroups": [{"filters": [
            {"propertyName": "dealstage", "operator": "IN",
             "values": list(BILLABLE_STAGE_IDS)}]}],
    }
    try:
        import httpx
        r = httpx.post(_SEARCH, headers={"Authorization": f"Bearer {token}"},
                       json=body, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except Exception:
        return []
    out, seen = [], set()
    for d in results:
        name = ((d.get("properties") or {}).get("dealname") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"project": name, "client": ""})
    return out


def list_billable_deals(page_size: int = 100, max_pages: int = 20) -> list:
    """EVERY deal in a billable stage (Closed Won or Gain-Approval), as
    [{project, client}] sorted by name — the universe a reviewer may tag a billable
    charge to. Paginates the search endpoint with only the stage filter (no text
    query). Empty when there's no token or on any API error (never raises)."""
    token = env("HUBSPOT_TOKEN")
    if not token:
        return []
    out, seen, after = [], set(), None
    try:
        import httpx
        for _ in range(max_pages):
            body = {"limit": max(1, min(page_size, 100)),
                    "properties": ["dealname", "dealstage"],
                    "sorts": [{"propertyName": "dealname", "direction": "ASCENDING"}],
                    "filterGroups": [{"filters": [
                        {"propertyName": "dealstage", "operator": "IN",
                         "values": list(BILLABLE_STAGE_IDS)}]}]}
            if after:
                body["after"] = after
            r = httpx.post(_SEARCH, headers={"Authorization": f"Bearer {token}"},
                           json=body, timeout=30)
            r.raise_for_status()
            j = r.json()
            for d in j.get("results", []) or []:
                name = ((d.get("properties") or {}).get("dealname") or "").strip()
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    out.append({"project": name, "client": ""})
            after = ((j.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
    except Exception:
        return out            # whatever we managed to fetch is still useful
    return sorted(out, key=lambda d: d["project"].lower())
