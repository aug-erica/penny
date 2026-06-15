"""LLM fallback for merchants no rule or precedent covers. Output is
constrained to the client's COA list; anything else is rejected. Optional —
the pipeline runs fine with --no-llm."""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

from ...config import env
from ...models import Proposal

BATCH = 25
MODEL = "claude-haiku-4-5-20251001"   # cheap classification; bump if accuracy demands

PROMPT = """You are categorizing corporate-card transactions for {client}, a consulting firm, into its chart of accounts.

{guidance}

Valid COA lines (you MUST pick from this list verbatim):
{coa}

Recent examples of how this firm actually coded merchants:
{examples}

Transactions to categorize (JSON):
{txns}

For each, pick the most likely category. When the merchant name alone can't
determine intent (e.g. a meal that could be team/client/conference), choose the
safe default from the guidance and set confident=false.

Reply with a JSON array, one object per transaction, same order:
{{"i": <index>, "coa_line": "<verbatim from list>", "billable_guess": true|false, "confident": true|false, "why": "<8 words max>"}}
Reply with the JSON array only."""


def propose_batch(conn, cfg, lines: List[dict]) -> List[Tuple[dict, Optional[Proposal]]]:
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        return [(l, None) for l in lines]
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    coa = cfg.coa_lines
    examples = _examples(conn, cfg.client)
    guidance = _guidance(cfg)
    out: List[Tuple[dict, Optional[Proposal]]] = []
    for i in range(0, len(lines), BATCH):
        chunk = lines[i:i + BATCH]
        txns = [{"i": j, "merchant": l["merchant_raw"], "amount": l["amount_cents"] / 100,
                 "date": l["txn_date"], "cardholder": l.get("cardholder") or l.get("employee")}
                for j, l in enumerate(chunk)]
        msg = client.messages.create(
            model=MODEL, max_tokens=2048,
            messages=[{"role": "user", "content": PROMPT.format(
                client=cfg.client.title(), guidance=guidance,
                coa="\n".join(f"- {c}" for c in coa),
                examples=examples, txns=json.dumps(txns))}],
        )
        try:
            text = msg.content[0].text.strip()
            text = text[text.index("["):text.rindex("]") + 1]
            answers = {a["i"]: a for a in json.loads(text)}
        except (ValueError, KeyError, json.JSONDecodeError):
            answers = {}
        for j, line in enumerate(chunk):
            a = answers.get(j)
            if a and a.get("coa_line") in coa:
                # LLM proposals always land in the flagged-for-review band; the
                # confident flag only sharpens the rationale, never auto-fills.
                conf = "medium" if a.get("confident") else "low"
                out.append((line, Proposal(
                    coa_line=a["coa_line"], proposed_by="llm", confidence=conf,
                    rationale=f"LLM: {a.get('why', '')}".strip(),
                    billable=a.get("billable_guess"),
                )))
            else:
                out.append((line, None))
    return out


def _guidance(cfg) -> str:
    from ...config import CLIENTS_DIR
    path = CLIENTS_DIR / cfg.client / "category_guidance.md"
    return path.read_text() if path.exists() else ""


def _examples(conn, client: str, n: int = 30) -> str:
    rows = conn.execute(
        """SELECT merchant_norm, coa_line, SUM(n) AS cnt FROM merchant_history
           WHERE client=? GROUP BY merchant_norm, coa_line
           ORDER BY cnt DESC LIMIT ?""", (client, n)).fetchall()
    return "\n".join(f"- {r['merchant_norm']} -> {r['coa_line']}" for r in rows) or "(none yet)"
