"""Decide what a pal's DM to Penny is about — so pals don't need a magic word.

Penny ALWAYS initiates card-charge conversations: the continuous-close poller
reads new charges from the QBO bank feed and DMs each pal to confirm project /
send a receipt. So when a pal *starts* a conversation, it's almost always an
out-of-pocket REIMBURSEMENT — something Penny doesn't already know about.

This module routes a wordless, pal-initiated top-level DM to the right flow. It
defaults to reimbursement (the pal-initiated assumption), short-circuits on clear
card-answer signals, uses a light LLM tiebreaker, and returns 'ambiguous' only
when it genuinely can't tell — the listener then asks a one-line clarifier rather
than silently filing the wrong thing. Thread replies are routed by the listener
via the thread's subject and never reach here; an explicit "reimburse" also skips
this. Pure/cheap: the cheap signals need no LLM (so they're trivially testable).
"""
from __future__ import annotations

import re

from ..config import env
from . import disambiguate

MODEL = "claude-haiku-4-5-20251001"

# Same amount shapes as the reimbursement/card parsers ($1,350 / 42.50 / $42).
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")
# A bare "not billable" (mirrors reply_flow._NOT_BILLABLE) — a card-charge answer.
_NOT_BILLABLE = re.compile(r"(?<![a-z])no[nt][\s\-_]*billable(?![a-z])", re.I)
# Words that only make sense as an answer to Penny's card-charge question.
_CARD_CUES = re.compile(
    r"(?<![a-z])(billable|bill it|bill to|the charge|that charge|this charge|"
    r"my card|the card|company card|receipt for the)(?![a-z])", re.I)


# "Where do I stand?" — a pal asking for their status, not filing or answering
# anything. Answered with a summary instead of the reimbursement/card clarifier.
_STATUS_Q = re.compile(
    r"(what(?:'s| is| do)\s+(?:still\s+)?(?:outstanding|open|left|missing|needed|"
    r"(?:you|i)\s+(?:still\s+)?(?:need|owe)))|"
    r"(anything\s+(?:else\s+)?(?:outstanding|open|missing|left|you need|i owe|needed))|"
    r"(am\s+i\s+(?:all\s+)?(?:set|good|done|caught up|up to date|current))|"
    r"(is\s+everything\s+(?:up to date|in|done|set|current))|"
    r"(do\s+i\s+(?:have|owe)\s+any(?:thing)?\s*(?:outstanding|open|else|missing)?)|"
    r"(where\s+(?:do\s+)?(?:i|things)\s+stand)|"
    r"(^\s*status\??\s*$)", re.I)


def is_status_question(text: str, has_receipt: bool = False) -> bool:
    """A pal asking what's outstanding / whether they're up to date. Never when a
    file is attached (that's a submission), and only for short-ish messages."""
    t = (text or "").strip()
    if not t or has_receipt or len(t.split()) > 25:
        return False
    return bool(_STATUS_Q.search(t))


def has_money(text: str) -> bool:
    return bool(_AMOUNT.search(text or ""))


def _amounts_cents(text: str) -> set:
    return {round(float(a.replace(",", "")) * 100) for a in _AMOUNT.findall(text or "")}


def mentions_project(text: str, projects: list) -> bool:
    """The reply names an active project (exact or a confident fuzzy match) — i.e.
    it's answering Penny's 'which project?' about a card charge."""
    if not (text and projects):
        return False
    low = text.lower()
    if any(p.lower() in low for p in projects):
        return True
    return disambiguate.fuzzy_one(text, projects) is not None


def looks_like_card_answer(text: str, projects: list) -> bool:
    t = text or ""
    return bool(_NOT_BILLABLE.search(t) or _CARD_CUES.search(t)
                or mentions_project(t, projects))


def classify(client: str, text: str, has_receipt: bool, awaiting_receipts: bool,
             projects: list, charge_amounts=None) -> str:
    """Return 'reimbursement' | 'card' | 'ambiguous' for a wordless, pal-initiated
    top-level DM. `awaiting_receipts` = the pal has card charges still missing a
    receipt (so a bare receipt is probably answering that, not a new expense).
    `charge_amounts` = cents of the pal's existing card charges — a reply whose
    amount matches one is about THAT charge (a card answer), not a new expense."""
    t = (text or "").strip()

    # References an existing card charge by amount ("$38.39 is billable") -> it's
    # answering Penny's question about that charge, not a new out-of-pocket expense.
    if charge_amounts and (_amounts_cents(t) & set(charge_amounts)):
        return "card"
    # A clear card-charge answer with no new dollar amount -> card flow.
    if looks_like_card_answer(t, projects) and not has_money(t):
        return "card"
    # A dollar amount the pal typed themselves (not matching a known charge) ->
    # they're telling us about a new out-of-pocket expense.
    if has_money(t):
        return "reimbursement"
    # A bare receipt with little/no description (no amount, no card cue): if Penny
    # is waiting on receipts for this pal's card charges it's genuinely ambiguous
    # (could be answering that) -> ask; otherwise it's a new expense's receipt.
    if has_receipt and len(t.split()) <= 5:
        return "ambiguous" if awaiting_receipts else "reimbursement"

    llm = _llm_classify(client, t, has_receipt, projects)
    return llm or "ambiguous"


# Answers to the clarifier ("reimbursement or card?").
_ANS_REIMB = re.compile(
    r"(?<![a-z])(reimburse\w*|out[\s\-]?of[\s\-]?pocket|i paid|my own|expense|mine|yes|yep|yeah)(?![a-z])",
    re.I)
_ANS_CARD = re.compile(
    r"(?<![a-z])(card|charge|company|no|nope|nah)(?![a-z])", re.I)


def interpret_answer(text: str):
    """Map a clarifier reply to 'reimbursement' | 'card' | None (couldn't tell)."""
    t = text or ""
    r, c = bool(_ANS_REIMB.search(t)), bool(_ANS_CARD.search(t))
    if r and not c:
        return "reimbursement"
    if c and not r:
        return "card"
    return None


def _llm_classify(client: str, text: str, has_receipt: bool, projects: list):
    """Haiku tiebreaker. Returns 'reimbursement' | 'card' | None (unsure/unavailable)."""
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key or not text:
        return None
    import json
    import anthropic
    prompt = f"""A team member at {client.title()} sent this message to Penny, the expense bot:
\"\"\"{text}\"\"\"
{"(They also attached a receipt file.)" if has_receipt else ""}

Penny handles two things:
- REIMBURSEMENT: the person paid out of pocket and wants money back (a new expense Penny didn't know about).
- CARD: the person is answering Penny's question about a company-card charge Penny already flagged (which project it's for, whether it's billable, or sending that charge's receipt).

Penny ALWAYS starts card-charge conversations itself, so a message the person started is usually a reimbursement — but not always.

Classify this message. Reply with JSON only:
{{"intent": "reimbursement" | "card" | "unsure", "confidence": "high" | "low"}}"""
    try:
        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=MODEL, max_tokens=60,
            messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text
        d = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, IndexError, json.JSONDecodeError, Exception):
        return None
    intent = d.get("intent")
    if intent in ("reimbursement", "card") and d.get("confidence") == "high":
        return intent
    return None
