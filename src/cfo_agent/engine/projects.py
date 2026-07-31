"""Resolve a pal's billable client/project.

Prefers the cached active-month list (fast, current) and falls back to a live
HubSpot Closed-Won search, so a project outside this close month — a long-closed
Waymo deal, an out-of-window Gilead deal — still resolves. Shared by the
reimbursement flow (`resolve`) and the card-reply flow (`candidates`).
"""
from __future__ import annotations

from . import disambiguate


def active_names(cfg, month) -> list:
    from . import reply_flow
    return reply_flow._projects(cfg, month)


def candidates(cfg, month, text) -> list:
    """Active-month projects PLUS live HubSpot Closed-Won matches for `text`,
    deduped — the candidate universe to resolve or pick from. Without a HubSpot
    token this is exactly the active list (today's behavior)."""
    names = list(active_names(cfg, month))
    seen = {n.lower() for n in names}
    from ..adapters.projects import hubspot_client
    for d in hubspot_client.search_closed_won(text):
        p = d.get("project")
        if p and p.lower() not in seen:
            names.append(p)
            seen.add(p.lower())
    return names


def resolve(cfg, month, text):
    """(project, options): a confident single match -> (name, None); several close
    (e.g. one client, multiple projects) -> (None, shortlist); nothing -> (None,
    None). Searches the active list + live HubSpot."""
    low = (text or "").strip().lower()
    if not low:
        return None, None
    pool = candidates(cfg, month, text)
    contains = [p for p in pool if low in p.lower() or p.lower() in low]
    if len(contains) == 1:
        return contains[0], None
    if len(contains) > 1:
        return None, contains[:5]
    one = disambiguate.fuzzy_one(text, pool)
    if one:
        return one, None
    return None, (disambiguate.shortlist(text, pool, limit=3) or None)
