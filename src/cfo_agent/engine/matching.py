"""Card line <-> receipt-vault line matching.

Amount leads (exact cents), date window second, merchant fuzz only breaks ties —
Chase descriptors ("SQ *COFFEE NYC") and SmartScan merchants ("Coffee") diverge
too much for merchant-first matching.
"""
from __future__ import annotations

from datetime import date, timedelta

from rapidfuzz import fuzz

from . import ledger


def match_month(conn, client: str, close_month: str,
                date_window_days: int = 3, fuzz_threshold: int = 80) -> dict:
    card = [l for l in ledger.lines_for_month(conn, client, close_month, "card_feed")
            if l["status"] != "excluded"]
    vault = [l for l in ledger.lines_for_month(conn, client, close_month, "receipt_vault")
             if not _is_reimbursable_only(l)]
    by_amount: dict = {}
    for v in vault:
        by_amount.setdefault(v["amount_cents"], []).append(v)
    taken = set()
    stats = {"matched": 0, "unmatched_card": 0, "unmatched_vault": 0}

    for c in card:
        candidates = [v for v in by_amount.get(c["amount_cents"], [])
                      if v["id"] not in taken]
        best, best_score = None, -1.0
        c_date = date.fromisoformat(c["txn_date"])
        for v in candidates:
            delta = abs((date.fromisoformat(v["txn_date"]) - c_date).days)
            if delta > date_window_days:
                continue
            name_score = fuzz.token_set_ratio(c["merchant_norm"], v["merchant_norm"])
            if len(candidates) > 1 and delta > 0 and name_score < fuzz_threshold:
                continue
            score = name_score - delta * 5
            if score > best_score:
                best, best_score = v, score
        if best:
            taken.add(best["id"])
            ledger.set_match(
                conn, c["id"], best["id"], best_score,
                receipt_link=best.get("receipt_link"),
                truth_category=best.get("truth_category"),
                truth_billable=best.get("truth_billable"),
                employee=best.get("employee"),
            )
            ledger.set_match(conn, best["id"], c["id"], best_score)
            stats["matched"] += 1
        else:
            stats["unmatched_card"] += 1
    stats["unmatched_vault"] = sum(1 for v in vault if v["id"] not in taken)
    return stats


def _is_reimbursable_only(line: dict) -> bool:
    """Reimbursable expenses (personal card) have no corporate-card counterpart."""
    return bool(line.get("reimbursable"))
