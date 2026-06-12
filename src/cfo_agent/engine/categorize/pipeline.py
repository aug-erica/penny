"""Categorization cascade: rules -> history -> LLM. The agent proposes
independently — employee Expensify coding is NEVER a proposal source, because
the point of the agent is to remove the need for employees to code expenses.
Where employee coding still exists it is used only as a cross-check: a
disagreement flags the line for the reviewer.

Nothing is left blank: a line with no proposal is flagged with an explanation.
"""
from __future__ import annotations

from .. import ledger
from . import history as history_mod
from . import rules as rules_mod


def categorize_month(conn, cfg, close_month: str, use_llm: bool = True) -> dict:
    """Targets: card-feed charges plus reimbursable vault expenses (personal-card
    spend). Vault lines matched to a card line are the same expense seen twice —
    the card line carries the proposal."""
    lines = [l for l in ledger.lines_for_month(conn, cfg.client, close_month)
             if l["status"] == "draft" and l["amount_cents"] > 0
             and (l["source"] == "card_feed" or l.get("reimbursable"))]
    stats = {"rule": 0, "history": 0, "llm": 0, "needs_reviewer": 0,
             "disagrees_with_expensify": 0}
    leftovers = []
    for line in lines:
        prop = rules_mod.propose(cfg.rules, line)
        if prop is None:
            prop = history_mod.propose(conn, cfg.client, line)
        if prop is None:
            leftovers.append(line)
            continue
        stats["disagrees_with_expensify"] += _apply(conn, cfg, line, prop)
        stats[prop.proposed_by] += 1

    still_open = leftovers
    if leftovers and use_llm:
        from . import llm as llm_mod
        still_open = []
        for line, prop in llm_mod.propose_batch(conn, cfg, leftovers):
            if prop:
                stats["disagrees_with_expensify"] += _apply(conn, cfg, line, prop)
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


def _why_unproposable(line: dict) -> str:
    if not line.get("matched_line_id") and line["source"] == "card_feed":
        return ("no receipt found for this charge and no rule or prior coding "
                "for this merchant; chase the receipt or pick a category")
    return ("first appearance of this merchant — no rule or prior coding; "
            "your pick becomes next month's rule")


def _apply(conn, cfg, line: dict, prop) -> int:
    """Returns 1 if the proposal disagrees with still-extant employee coding."""
    bcfg = cfg.section("billable")
    billable = prop.billable
    if billable is None and prop.coa_line in (bcfg.get("billable_categories") or []):
        billable = True
    flag_billable = bcfg.get("always_flag") and billable

    # Cross-check only (never a source): while employees still code in
    # Expensify, surface disagreements to the reviewer.
    disagrees = 0
    rationale = prop.rationale
    truth = line.get("truth_category")
    if truth and truth != "Uncategorized" and truth != prop.coa_line:
        who = (line.get("employee") or "submitter").split("@")[0]
        rationale += f" — differs from {who}'s Expensify coding ({truth})"
        disagrees = 1

    status = ("flagged" if (prop.confidence != "high" or flag_billable or disagrees)
              else "draft")
    ledger.set_proposal(conn, line["id"], prop.coa_line, prop.proposed_by,
                        prop.confidence, rationale,
                        billable=billable, status=status)
    return disagrees
