"""Interpret an employee's reimbursement-intake DM.

An employee DMs Penny something like "reimburse $42.50 client lunch with the
McCain team on 6/14" (the message starts with the trigger word). This maps that
free text to the fields an accountable-plan reimbursement needs: amount, a
one-line business purpose, the expense date, and a PROPOSED COA category.

Categorization philosophy (July 2026): Penny *proposes* the category the way it
already does for card spend — the employee never has to recall or type an exact
category name. The parser always returns its best-fit category plus a confidence
and a short ranked shortlist; the flow decides whether that's confident enough to
just file, or worth offering the employee a one-tap numbered pick. Still Haiku,
still returns {} on any failure so a garbled message never books a bogus row.
"""
from __future__ import annotations

import json
import re

from ..config import env

MODEL = "claude-haiku-4-5-20251001"

# Amounts with or without cents: $1,350 / $1,350.00 / 266.83 (same as reply_flow).
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")

_PROMPT = """A team member at {client} is submitting a business expense to be REIMBURSED (they paid out of pocket and want the money back). Today is {today}.

Their message:
\"\"\"{text}\"\"\"
{merchant}
Valid expense categories (use these VERBATIM — never invent one):
{coa}

Extract:
- amount_usd: the dollar amount they paid (a number), or null if not stated.
- business_purpose: a concise one-line business purpose (what it was for / who it was with). null if they gave none.
- expense_date: the date of the expense as YYYY-MM-DD. If they gave a date, use it (assume the current year if only month/day). If they gave none, use {today}.
- category_options: the 1-3 MOST likely categories from the list, VERBATIM, best guess first. Always give at least one — infer from the purpose and merchant the way a bookkeeper would (e.g. a restaurant -> Groceries & Meals, a flight/hotel -> General Travel). Order by likelihood.
- category_confidence: "high" only if one category is clearly correct; "medium" if it's a reasonable lead but a couple could fit; "low" if you're mostly guessing.

Reply with a JSON object only:
{{"amount_usd": <number or null>, "business_purpose": "<text or null>", "expense_date": "YYYY-MM-DD", "category_options": ["<exact from list>", ...], "category_confidence": "high|medium|low"}}"""


def _grounded(cat: str, text: str) -> bool:
    """Is category `cat` actually named in the text? A distinctive word of the
    category appears, or a high partial match. Used to *raise confidence* when the
    employee themselves named the category (not to null a proposal out)."""
    r = (text or "").lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", cat.lower())
            if len(t) >= 4 and t not in {"expense", "expenses"}]
    if any(t in r for t in toks):
        return True
    from rapidfuzz import fuzz
    return fuzz.partial_ratio(cat.lower(), r) >= 90


def interpret_reimbursement(client: str, text: str, coa_lines: list,
                            today: str, merchant: str = None) -> dict:
    """Parse a reimbursement-intake message. Returns
    {amount_cents, business_purpose, expense_date, proposed_coa_line,
     category_confidence, category_options} — amount/purpose may be None — or {}
    if the LLM is unavailable / unparseable. `merchant` (read off the receipt, when
    available) sharpens the category guess. proposed_coa_line is the best guess and
    is never nulled just because the employee didn't name it — that's the point."""
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key or not (text or "").strip():
        return {}
    import anthropic
    coa = "\n".join(f"- {c}" for c in coa_lines)
    mline = f"\nMerchant on the receipt: {merchant}\n" if merchant else ""
    msg = anthropic.Anthropic(api_key=api_key).messages.create(
        model=MODEL, max_tokens=500,
        messages=[{"role": "user", "content": _PROMPT.format(
            client=client.title(), text=text, coa=coa, today=today, merchant=mline)}])
    try:
        raw = msg.content[0].text
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return {}

    # Amount: trust the LLM only if it's an amount actually present in the text
    # (guards against a hallucinated total); else fall back to the first typed
    # amount; else None -> caller treats as needs_info.
    text_cents = {round(float(a.replace(",", "")) * 100) for a in _AMOUNT.findall(text)}
    amt = d.get("amount_usd")
    amount_cents = round(amt * 100) if isinstance(amt, (int, float)) and amt else None
    if amount_cents is None or (text_cents and amount_cents not in text_cents):
        amount_cents = min(text_cents) if len(text_cents) == 1 else (
            amount_cents if amount_cents else (next(iter(text_cents)) if text_cents else None))

    purpose = (d.get("business_purpose") or "").strip() or None

    exp = (d.get("expense_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
        exp = today

    # Keep only real COA lines, in the model's ranked order, de-duped.
    valid = set(coa_lines)
    options, seen = [], set()
    for c in (d.get("category_options") or []):
        if c in valid and c not in seen:
            options.append(c)
            seen.add(c)
    proposed = options[0] if options else None

    conf = (d.get("category_confidence") or "").strip().lower()
    if conf not in {"high", "medium", "low"}:
        conf = "medium" if proposed else "low"
    # If the employee actually named the category, trust it fully.
    if proposed and _grounded(proposed, text):
        conf = "high"

    return {"amount_cents": amount_cents, "business_purpose": purpose,
            "expense_date": exp, "proposed_coa_line": proposed,
            "category_confidence": conf, "category_options": options}
