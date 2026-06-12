"""Categorization cascade: employee's own vault coding -> rules -> history -> LLM.

Production runs inherit the matched Expensify expense's category — the team
member already coded it, the approver reviews it. Blind runs (accuracy
measurement) skip that step so the agent's independent judgment is scored.
Nothing is left blank: a line with no proposal is flagged with an explanation.
"""
from __future__ import annotations

from ...models import Proposal
from .. import ledger
from . import history as history_mod
from . import rules as rules_mod


def categorize_month(conn, cfg, close_month: str, use_llm: bool = True,
                     blind: bool = False) -> dict:
    """Targets: card-feed charges plus reimbursable vault expenses (personal-card
    spend). Vault lines matched to a card line are the same expense seen twice —
    the card line carries the proposal."""
    lines = [l for l in ledger.lines_for_month(conn, cfg.client, close_month)
             if l["status"] == "draft" and l["amount_cents"] > 0
             and (l["source"] == "card_feed" or l.get("reimbursable"))]
    stats = {"vault": 0, "rule": 0, "history": 0, "llm": 0, "needs_reviewer": 0}
    leftovers = []
    for line in lines:
        prop = None if blind else _from_vault_coding(line)
        if prop is None:
            prop = rules_mod.propose(cfg.rules, line)
        if prop is None:
            prop = history_mod.propose(conn, cfg.client, line)
        if prop is None:
            leftovers.append(line)
            continue
        _apply(conn, cfg, line, prop)
        stats[prop.proposed_by] += 1

    still_open = leftovers
    if leftovers and use_llm:
        from . import llm as llm_mod
        still_open = []
        for line, prop in llm_mod.propose_batch(conn, cfg, leftovers):
            if prop:
                _apply(conn, cfg, line, prop)
                stats["llm"] += 1
            else:
                still_open.append(line)

    for line in still_open:
        # Never leave a blank: flag it with the reason and what fixes it.
        why = _why_unproposable(line)
        ledger.set_proposal(conn, line["id"], None, None, "low",
                            f"needs reviewer — {why}", status="flagged")
        stats["needs_reviewer"] += 1
    return stats


def _from_vault_coding(line: dict):
    """The matched Expensify expense carries the submitter's own category —
    in production that IS the draft proposal, pending approver review."""
    if not line.get("truth_category") or line["truth_category"] == "Uncategorized":
        return None
    who = (line.get("employee") or "the submitter").split("@")[0]
    state = (line.get("report_state") or "").lower() or "submitted"
    return Proposal(
        coa_line=line["truth_category"], proposed_by="vault", confidence="high",
        rationale=f"as coded by {who} in Expensify ({state} report)",
        billable=bool(line["truth_billable"]) if line.get("truth_billable") is not None else None,
    )


def _why_unproposable(line: dict) -> str:
    if line.get("matched_line_id") and line.get("truth_category") == "Uncategorized":
        who = (line.get("employee") or "submitter").split("@")[0]
        return (f"{who}'s Expensify expense is itself Uncategorized; "
                "pick a category and it becomes a rule next month")
    if not line.get("matched_line_id") and line["source"] == "card_feed":
        return ("no Expensify report filed for this charge and no prior coding "
                "for this merchant; chase the receipt or pick a category")
    return ("first appearance of this merchant — no rule or prior coding; "
            "your pick becomes next month's rule")


def _apply(conn, cfg, line: dict, prop):
    bcfg = cfg.section("billable")
    billable = prop.billable
    if billable is None and prop.coa_line in (bcfg.get("billable_categories") or []):
        billable = True
    flag_billable = bcfg.get("always_flag") and billable
    status = "flagged" if (prop.confidence != "high" or flag_billable) else "draft"
    ledger.set_proposal(conn, line["id"], prop.coa_line, prop.proposed_by,
                        prop.confidence, prop.rationale,
                        billable=billable, status=status)
