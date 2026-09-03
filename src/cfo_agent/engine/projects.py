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


# Words allowed INSIDE a proper-noun run without breaking it ("Planned Parenthood
# Federation of America", "Design and Facilitation").
_JOINERS = r"(?:of|and|for|the|de|&)"
# A card descriptor / masked number / dollar amount is never a project name.
_NOISE = re.compile(r"\d|X{3,}|^\$", re.I)


def _phrases(text: str) -> list:
    """Candidate project phrases in a message, most specific first: whatever
    follows a billing cue ("billable to <X>", "bill to <X>", "for the <X> project"),
    then every run of Capitalized words. Mike's "receipt for UNITED …$134.38 …
    billable to Link Logistics Decision Making, flights for Beaver Creek" must
    yield "Link Logistics Decision Making" even though it sits mid-message."""
    t = text or ""
    out = []
    for m in re.finditer(r"(?:billable|bill(?:ed)?|charge(?:d)?)\s+to\s+(?:the\s+)?([^,.\n;]+)",
                         t, re.I):
        out.append(m.group(1).strip())
    for m in re.finditer(r"(?:for|on)\s+(?:the\s+)?([A-Z][\w&']*(?:\s+(?:[A-Z][\w&']*|"
                         + _JOINERS + r"))*)\s+(?:project|engagement|deal|work)", t):
        out.append(m.group(1).strip())
    for m in re.finditer(r"\b[A-Z][A-Za-z&']+(?:\s+(?:[A-Z][A-Za-z0-9&']+|" + _JOINERS
                         + r"))+", t):
        out.append(m.group(0).strip())
    clean, seen = [], set()
    for ph in out:
        words = [w for w in re.findall(r"[A-Za-z0-9&']+", ph)
                 if w.lower() not in _STOP and not _NOISE.search(w)]
        if not words:
            continue
        q = " ".join(words)
        if q.lower() not in seen:
            seen.add(q.lower())
            clean.append(q)
    return clean


def _hubspot_matches(text) -> list:
    """Live HubSpot deal matches for `text`, robust to loose phrasing. HubSpot's
    full-text search ANDs tokens, so an extra/wrong word ('...Advisory Retainer'
    when the deal is '...Advisory, Design...') zeroes it out. We try the project-
    looking PHRASES in the message first (a billing cue, then Capitalized runs),
    each backing off to shorter prefixes, then the leading distinctive words."""
    from ..adapters.projects import hubspot_client
    lead = [w for w in re.findall(r"[A-Za-z0-9&']+", text or "")
            if w.lower() not in _STOP]
    if not lead:
        return []
    tried = set()

    def _try(words):
        for k in (len(words), 4, 3, 2):
            if k > len(words) or k < 1:
                continue
            q = " ".join(words[:k])
            if q.lower() in tried:
                continue
            tried.add(q.lower())
            hits = hubspot_client.search_closed_won(q)
            if hits:
                return hits
        return []

    for ph in _phrases(text):
        hits = _try(ph.split())
        if hits:
            return hits
    hits = _try(lead)
    if hits:
        return hits
    # last resort: a single distinctive leading word
    q = lead[0]
    if q.lower() not in tried:
        return hubspot_client.search_closed_won(q)
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


_BILLABLE_TTL_S = 6 * 3600


def billable_deals(client: str, force: bool = False) -> list:
    """All HubSpot billable-stage deal names, cached in the kv store for a few
    hours so a dashboard load doesn't hit HubSpot every time. Empty without a
    token (the active list still applies)."""
    import json
    from datetime import datetime, timezone
    from . import kv
    from ..adapters.projects import hubspot_client
    key = f"billable_projects:{client}"
    if not force:
        try:
            raw = json.loads(kv.get(key) or "null")
            if raw and (datetime.now(timezone.utc)
                        - datetime.fromisoformat(raw["at"])).total_seconds() < _BILLABLE_TTL_S:
                return list(raw["names"])
        except Exception:
            pass
    names = [d["project"] for d in hubspot_client.list_billable_deals() if d.get("project")]
    if names:
        try:
            kv.set(key, json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                    "names": names}))
        except Exception:
            pass
    return names


def eligible(cfg, month, extra=()) -> list:
    """Every project a billable charge may be tagged to, for a pick-list: this
    month's active list, the previous month's (a late charge for a deal that just
    ended), every HubSpot billable-stage deal, plus `extra` (values already on
    ledger lines, so nothing on screen is un-selectable). Deduped, sorted."""
    y, m = int(month[:4]), int(month[5:7])
    prev = f"{y if m > 1 else y - 1:04d}-{m - 1 if m > 1 else 12:02d}"
    pool = list(active_names(cfg, month)) + list(active_names(cfg, prev))
    pool += billable_deals(cfg.client)
    pool += [e for e in extra if e]
    seen, out = set(), []
    for pname in pool:
        k = pname.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(pname.strip())
    return sorted(out, key=str.lower)
