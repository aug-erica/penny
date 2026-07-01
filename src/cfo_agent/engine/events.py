"""Event-window overrides — calendar context the card feed can't carry.

Runs AFTER the categorization cascade. A window overrides a line's proposed
category only when that proposal is in the window's `absorbs` list, so
merchant-rule categories (software, subscriptions, book-print vendors) that
happen to fall inside the window are left alone. Windows are client config,
supplied prospectively by the reviewer — the agent is given the calendar, it
doesn't divine it.
"""
from __future__ import annotations

from datetime import date

from . import ledger


def apply_event_windows(conn, cfg, close_month: str) -> dict:
    windows = cfg.raw.get("event_windows") or []
    if not windows:
        return {"overridden": 0}
    lines = [l for l in ledger.lines_for_month(conn, cfg.client, close_month)
             if l["status"] != "excluded" and l["amount_cents"] > 0
             and (l["source"] == "card_feed" or l.get("reimbursable"))]
    n = 0
    for l in lines:
        d = date.fromisoformat(l["txn_date"])
        current = l.get("proposed_coa_line")
        for w in windows:
            if not _in_window(w, d):
                continue
            if current not in (w.get("absorbs") or []):
                continue
            if current == w["coa_line"]:
                continue
            # An event window is a PRIOR, not a certainty — not every meal during
            # Summit week is Summit spend. Cap at medium so it always lands in the
            # reviewer's flagged set and never pollutes the trusted high band.
            conf = w.get("confidence", "medium")
            if conf == "high":
                conf = "medium"
            rationale = f"within {w['name']}" + (
                f" ({w['start']}–{w['end']})" if w.get("start") else " (recurring)")
            billable = 1 if w["coa_line"] in (
                cfg.section("billable").get("billable_categories") or []) else None
            flag_billable = cfg.section("billable").get("always_flag") and billable
            status = "flagged" if (conf != "high" or flag_billable) else "draft"
            ledger.set_proposal(conn, l["id"], w["coa_line"], "event", conf,
                                rationale, billable=billable, status=status)
            n += 1
            break
    return {"overridden": n}


def _in_window(w: dict, d: date) -> bool:
    if w.get("recur") == "fridays":
        return d.weekday() == 4
    if w.get("start") and w.get("end"):
        return date.fromisoformat(w["start"]) <= d <= date.fromisoformat(w["end"])
    return False
