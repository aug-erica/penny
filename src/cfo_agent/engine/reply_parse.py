"""Interpret a pal's free-text Slack reply against their own charge list.

Pals answer naturally ("the SF trip was McCain, O'Hare was Genentech, rest not
billable"). This maps that to per-charge decisions — billable yes/no + which
project — constrained to the pal's actual charges and the active project list.
Falls back to no-ops if the LLM is unavailable or the reply is unparseable, so
a garbled reply never mis-tags silently.
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..config import env

MODEL = "claude-haiku-4-5-20251001"

PROMPT = """A team member at {client} replied to a message asking which of their card charges are billable and to which project.

Their charges (index, date, merchant, amount, current category):
{charges}

Active projects they can bill to (use the name VERBATIM, or null if not billable):
{projects}

Their reply:
\"\"\"{reply}\"\"\"

For each charge the reply clearly addresses, output a decision. Leave out charges the reply doesn't speak to. If they say something is "not billable", set billable=false, project=null. Only use a project name from the list above.

Reply with a JSON array only:
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
