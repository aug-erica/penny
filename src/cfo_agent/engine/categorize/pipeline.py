"""Categorization cascade: rules -> history -> LLM. Every proposal carries
who proposed it, a confidence band, and a one-line rationale."""
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
    stats = {"rule": 0, "history": 0, "llm": 0, "uncategorized": 0}
    leftovers = []
    for line in lines:
        prop = rules_mod.propose(cfg.rules, line)
        if prop is None:
            prop = history_mod.propose(conn, cfg.client, line)
        if prop is None:
            leftovers.append(line)
            continue
        _apply(conn, cfg, line, prop)
        stats[prop.proposed_by] += 1

    if leftovers and use_llm:
        from . import llm as llm_mod
        for line, prop in llm_mod.propose_batch(conn, cfg, leftovers):
            if prop:
                _apply(conn, cfg, line, prop)
                stats["llm"] += 1
            else:
                stats["uncategorized"] += 1
    else:
        stats["uncategorized"] += len(leftovers)
    return stats


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
