"""Interpret a pal's free-text Slack reply against their own charge list.

Pals answer naturally ("the SF trip was McCain, O'Hare was Genentech, rest not
billable"). This maps that to per-charge decisions — billable yes/no + which
project — constrained to the pal's actual charges and the active project list.
Falls back to no-ops if the LLM is unavailable or the reply is unparseable, so
a garbled reply never mis-tags silently.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from ..config import env

# A pal asking to see/edit the full review workbook (Option A: reviewers and any
# pal who wants line-by-line control use the workbook, the single source of truth).
# Kept tight — a false negative just means they ask again; a false positive would
# dump a link unprompted.
_WORKBOOK_REQUEST = re.compile(
    r"\b(workbook|spread ?sheet|excel|the (review )?sheet|line[- ]by[- ]line|"
    r"full (list|ledger)|see (all|everything|the whole))\b", re.I)


def wants_workbook(reply: str) -> bool:
    """True when the reply asks for the review workbook / to edit line-by-line."""
    return bool(_WORKBOOK_REQUEST.search(reply or ""))

MODEL = "claude-haiku-4-5-20251001"

PROMPT = """A team member at {client} replied to a message asking which of their card charges are billable and to which project.

Their charges (index, date, merchant, amount, current category):
{charges}

Active projects they can bill to (use the name VERBATIM, or null if not billable):
{projects}

Their reply:
\"\"\"{reply}\"\"\"

Output a decision ONLY for a charge the reply EXPLICITLY speaks to about billing — i.e. it says the charge is billable/not billable, or names a client/project for it. Do NOT infer billability from anything else (a category change, a receipt, a general comment). If the reply is only about categories or receipts and says nothing about billing, return an empty array []. Never invent a project.

Reply with a JSON array only (empty [] if nothing about billing):
[{{"i": <index>, "billable": true|false, "project": "<exact name or null>", "why": "<6 words>"}}]"""


def interpret_reply(client: str, reply: str, charges: List[dict],
                    projects: List[str]) -> List[dict]:
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key or not reply.strip():
        return []
    import anthropic
    ch = "\n".join(
        f"{i}. {c['txn_date']} {c['merchant_raw']} ${c['amount_cents']/100:.2f} "
        f"[{c.get('proposed_coa_line') or '—'}]" for i, c in enumerate(charges))
    pj = "\n".join(f"- {p}" for p in projects)
    msg = anthropic.Anthropic(api_key=api_key).messages.create(
        model=MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": PROMPT.format(
            client=client.title(), charges=ch, projects=pj, reply=reply)}])
    try:
        text = msg.content[0].text
        text = text[text.index("["):text.rindex("]") + 1]
        raw = json.loads(text)
    except (ValueError, IndexError, json.JSONDecodeError):
        return []

    valid_projects = set(projects)
    out = []
    for d in raw:
        i = d.get("i")
        if not isinstance(i, int) or not (0 <= i < len(charges)):
            continue
        proj = d.get("project")
        if proj in (None, "null", ""):
            proj = None
        elif proj not in valid_projects:
            continue  # never invent a project
        out.append({
            "external_id": charges[i]["external_id"],
            "merchant": charges[i]["merchant_raw"],
            "billable": bool(d.get("billable")),
            "project": proj,
            "why": d.get("why", ""),
        })
    return out


RECAT_PROMPT = """A team member at {client} replied about their card charges and may have asked to RECATEGORIZE one or more.

Their charges (index, date, merchant, amount, current category):
{charges}

Valid categories (use VERBATIM):
{coa}

Their reply:
\"\"\"{reply}\"\"\"

Only output a change when the reply NAMES a target category (verbatim or an obvious synonym of one in the list). The category you output must be something the reply actually refers to — never infer one.

Return an empty array [] when the reply names no category — e.g. it's about receipts ("here's the receipt", "read it correctly"), amounts ("this is the $485 one"), billability ("not billable"), or is just emphasis/frustration. Those are NOT recategorizations.

Reply with a JSON array only:
[{{"i": <index>, "new_category": "<exact from list>", "why": "<6 words>"}}]"""


def _grounded(cat: str, reply: str) -> bool:
    """Is category `cat` actually supported by the reply text? Guards against the
    LLM inventing a category from a message that names none (e.g. a bare receipt
    upload or 'READ IT CORRECTLY' becoming 'Uber Accrued Expenses'). True only if a
    distinctive word of the category appears in the reply, or the category name is
    clearly present (high partial match)."""
    r = reply.lower()
    # 'expense(s)' is too generic to anchor on; everything else len>=4 counts.
    toks = [t for t in re.split(r"[^a-z0-9]+", cat.lower())
            if len(t) >= 4 and t not in {"expense", "expenses"}]
    if any(t in r for t in toks):
        return True
    from rapidfuzz import fuzz
    return fuzz.partial_ratio(cat.lower(), r) >= 90


def interpret_recategorizations(client: str, reply: str, charges: list,
                                coa_lines: list) -> list:
    """Detect 'recategorize X as Y' style corrections in a pal's reply."""
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key or not reply.strip():
        return []
    import anthropic
    ch = "\n".join(
        f"{i}. {c['txn_date']} {c['merchant_raw']} ${c['amount_cents']/100:.2f} "
        f"[{c.get('proposed_coa_line') or '—'}]" for i, c in enumerate(charges))
    coa = "\n".join(f"- {c}" for c in coa_lines)
    msg = anthropic.Anthropic(api_key=api_key).messages.create(
        model=MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": RECAT_PROMPT.format(
            client=client.title(), charges=ch, coa=coa, reply=reply)}])
    try:
        text = msg.content[0].text
        text = text[text.index("["):text.rindex("]") + 1]
        raw = json.loads(text)
    except (ValueError, IndexError, json.JSONDecodeError):
        return []
    valid = set(coa_lines)
    out = []
    for d in raw:
        i = d.get("i")
        cat = d.get("new_category")
        if not isinstance(i, int) or not (0 <= i < len(charges)) or not cat:
            continue
        if cat == charges[i].get("proposed_coa_line"):
            continue
        if not _grounded(cat, reply):   # drop categories the reply doesn't support
            continue
        item = {"external_id": charges[i]["external_id"],
                "merchant": charges[i]["merchant_raw"],
                "old": charges[i].get("proposed_coa_line"),
                "new_category": cat, "why": d.get("why", ""),
                "valid": cat in valid}
        if not item["valid"]:
            # Not an exact COA line — suggest the closest real ones for Penny to ask.
            from rapidfuzz import process, fuzz
            item["suggestions"] = [m for m, _s, _ in
                                   process.extract(cat, coa_lines, scorer=fuzz.WRatio, limit=3)]
        out.append(item)
    return out


NOTE_PROMPT = """A team member at {client} was asked to describe — for a client invoice — what their BILLABLE charge(s) were for.

Their billable charges still needing an invoice description (index, date, merchant, amount, project):
{charges}

Their reply:
\"\"\"{reply}\"\"\"

Give a concise one-sentence invoice description for each charge the reply explains. If the reply states one overall purpose, apply it to every listed charge. If the reply is NOT describing these expenses (it's about categories, billability, receipts, or something unrelated), return an empty array [].

Reply with a JSON array only:
[{{"i": <index>, "note": "<one-sentence description>"}}]"""


def interpret_billable_notes(client: str, reply: str, charges: list) -> list:
    """Map a pal's free-text description to invoice notes for the billable
    charges awaiting one. `charges` = billable, project set, no note yet."""
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key or not reply.strip() or not charges:
        return []
    import anthropic
    ch = "\n".join(
        f"{i}. {c['txn_date']} {c['merchant_raw']} ${c['amount_cents']/100:.2f} "
        f"[{c.get('project') or '—'}]" for i, c in enumerate(charges))
    msg = anthropic.Anthropic(api_key=api_key).messages.create(
        model=MODEL, max_tokens=800,
        messages=[{"role": "user", "content": NOTE_PROMPT.format(
            client=client.title(), charges=ch, reply=reply)}])
    try:
        text = msg.content[0].text
        raw = json.loads(text[text.index("["):text.rindex("]") + 1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return []
    out = []
    for d in raw:
        i, note = d.get("i"), (d.get("note") or "").strip()
        if isinstance(i, int) and 0 <= i < len(charges) and note:
            out.append({"external_id": charges[i]["external_id"], "note": note})
    return out
