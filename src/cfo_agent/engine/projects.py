"""Resolve a pal's billable client/project.

Prefers the cached active-month list (fast, current) and falls back to a live
HubSpot Closed-Won search, so a project outside this close month — a long-closed
Waymo deal, an out-of-window Gilead deal — still resolves. Shared by the
reimbursement flow (`resolve`) and the card-reply flow (`candidates`).
"""
from __future__ import annotations

import re

from . import disambiguate

# Filler words that shouldn't drive a HubSpot deal search.
_STOP = {"both", "of", "these", "this", "that", "are", "is", "to", "the", "a",
         "an", "for", "it", "and", "on", "in", "all", "them", "they", "re",
         "billable", "project", "client", "clients", "expense", "expenses",
         "charge", "charges", "bill", "was", "were", "my", "our", "with"}


def active_names(cfg, month) -> list:
    from . import reply_flow
    return reply_flow._projects(cfg, month)


def _hubspot_matches(text) -> list:
    """Live HubSpot deal matches for `text`, robust to loose phrasing. HubSpot's
    full-text search ANDs tokens, so an extra/wrong word ('...Advisory Retainer'
    when the deal is '...Advisory, Design...') zeroes it out. We back off from the
    full distinctive phrase to shorter prefixes until something matches."""
    from ..adapters.projects import hubspot_client
    words = [w for w in re.findall(r"[A-Za-z0-9&']+", text or "")
             if w.lower() not in _STOP]
    if not words:
        return []
    tried = set()
    for k in (len(words), 3, 2, 1):
        if k > len(words):
            continue
        q = " ".join(words[:k])
        if q in tried:
            continue
        tried.add(q)
        hits = hubspot_client.search_closed_won(q)
        if hits:
            return hits
    return []


def candidates(cfg, month, text) -> list:
    """Active-month projects PLUS live HubSpot matches for `text`, deduped — the
    candidate universe to resolve or pick from. Without a HubSpot token this is
    exactly the active list (today's behavior)."""
    names = list(active_names(cfg, month))
    seen = {n.lower() for n in names}
    for d in _hubspot_matches(text):
        p = d.get("project")
        if p and p.lower() not in seen:
            names.append(p)
            seen.add(p.lower())
    return names


def resolve(cfg, month, text):
    """(project, options): a confident single match -> (name, None); several close
    (e.g. one client, multiple projects) -> (None, shortlist); nothing -> (None,
    None). Searches the active list + live HubSpot, and is robust to loose phrasing
    (a full sentence, or an extra word the deal name doesn't have)."""
    low = (text or "").strip().lower()
    words = [w for w in re.findall(r"[A-Za-z0-9&']+", text or "")
             if w.lower() not in _STOP]
    if not low or not words:
        return None, None
    active = active_names(cfg, month)
    hs = [d["project"] for d in _hubspot_matches(text) if d.get("project")]
    pool, seen = [], set()
    for p in list(active) + hs:
        if p.lower() not in seen:
            pool.append(p)
            seen.add(p.lower())

    # 1. Exact substring either way — handles short replies ("Gilead Manufacturing")
    #    and full deal names pasted verbatim.
    contains = [p for p in pool if p.lower() in low or low in p.lower()]
    if len(contains) == 1:
        return contains[0], None
    if len(contains) > 1:
        return None, contains[:5]
    # 2. HubSpot's token-backoff search already narrowed it: one hit is confident,
    #    a handful is a pick-list. (This is what rescues loose phrasing like
    #    "PPFA OOP Advisory Retainer" -> "PPFA OOP FY27 Advisory, Design…".)
    if len(hs) == 1:
        return hs[0], None
    if 1 < len(hs) <= 6:
        return None, hs[:5]
    # 3. Fuzzy over the pool using the distinctive words only (not the full sentence).
    q = " ".join(words)
    one = disambiguate.fuzzy_one(q, pool)
    if one:
        return one, None
    return None, (disambiguate.shortlist(q, pool, limit=3) or None)
