"""Score proposals against receipt-vault truth categories. Honesty guard:
merchant_history must have been rebuilt with through_month = the month under
test, so precedent never includes the answers being scored."""
from __future__ import annotations

from collections import defaultdict

from . import ledger

BANDS = ("high", "medium", "low")


def validate_month(conn, client: str, close_month: str) -> dict:
    lines = ledger.lines_for_month(conn, client, close_month)
    # Vault-sourced proposals are the employee's own coding — scoring them
    # against themselves would be circular. Only the agent's independent
    # judgment (rule/history/llm) is measured.
    scored = [l for l in lines
              if l.get("proposed_coa_line") and l.get("truth_category")
              and l.get("proposed_by") != "vault"]
    by_band = defaultdict(lambda: {"n": 0, "correct": 0})
    by_proposer = defaultdict(lambda: {"n": 0, "correct": 0})
    misses = []
    billable_n = billable_correct = 0

    for l in scored:
        ok = l["proposed_coa_line"] == l["truth_category"]
        by_band[l["confidence"]]["n"] += 1
        by_proposer[l["proposed_by"]]["n"] += 1
        if ok:
            by_band[l["confidence"]]["correct"] += 1
            by_proposer[l["proposed_by"]]["correct"] += 1
        else:
            misses.append(l)
        if l.get("billable") is not None and l.get("truth_billable") is not None:
            billable_n += 1
            billable_correct += int(bool(l["billable"]) == bool(l["truth_billable"]))

    card = [l for l in lines if l["source"] == "card_feed" and l["status"] != "excluded"]
    unmatched = [l for l in card if not l.get("matched_line_id")]
    n = len(scored)
    correct = sum(b["correct"] for b in by_band.values())
    return {
        "close_month": close_month,
        "scored": n,
        "overall_hit_rate": correct / n if n else None,
        "by_band": {b: dict(by_band[b]) for b in BANDS if by_band[b]["n"]},
        "by_proposer": {k: dict(v) for k, v in by_proposer.items()},
        "billable_accuracy": billable_correct / billable_n if billable_n else None,
        "card_lines": len(card),
        "unmatched_card_lines": len(unmatched),
        "misses": misses,
    }


def render_report(v: dict) -> str:
    def pct(x):
        return f"{x * 100:.1f}%" if x is not None else "n/a"
    lines = [
        f"# Validation report — {v['close_month']}",
        "",
        f"- Scored lines (proposal + truth): **{v['scored']}**",
        f"- Overall hit rate: **{pct(v['overall_hit_rate'])}**",
        f"- Billable-flag accuracy: **{pct(v['billable_accuracy'])}**",
        f"- Card lines: {v['card_lines']}, unmatched: {v['unmatched_card_lines']}",
        "",
        "| confidence band | n | correct | precision |",
        "|---|---|---|---|",
    ]
    for band, s in v["by_band"].items():
        lines.append(f"| {band} | {s['n']} | {s['correct']} | {pct(s['correct'] / s['n'])} |")
    lines += ["", "| proposer | n | correct | precision |", "|---|---|---|---|"]
    for prop, s in v["by_proposer"].items():
        lines.append(f"| {prop} | {s['n']} | {s['correct']} | {pct(s['correct'] / s['n'])} |")
    if v["misses"]:
        lines += ["", "## Misses (proposed vs actual)", "",
                  "| date | merchant | amount | proposed | actual | proposer | confidence |",
                  "|---|---|---|---|---|---|---|"]
        for m in v["misses"]:
            lines.append(
                f"| {m['txn_date']} | {m['merchant_raw'][:40]} | "
                f"{m['amount_cents'] / 100:.2f} | {m['proposed_coa_line']} | "
                f"{m['truth_category']} | {m['proposed_by']} | {m['confidence']} |")
    return "\n".join(lines) + "\n"
