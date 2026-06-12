"""Gap detection: charges without receipts, vault expenses without charges,
and books-side completeness deltas. Gaps are surfaced, never guessed around."""
from __future__ import annotations

from . import ledger


def gaps_for_month(conn, client: str, close_month: str, rec_report=None) -> dict:
    lines = ledger.lines_for_month(conn, client, close_month)
    card = [l for l in lines if l["source"] == "card_feed" and l["status"] != "excluded"]
    vault = [l for l in lines if l["source"] == "receipt_vault"]

    no_receipt = [l for l in card if not l.get("matched_line_id") and not l.get("receipt_link")]
    orphan_vault = [l for l in vault
                    if not l.get("matched_line_id") and not _reimbursable(l)]
    # Same target set as the categorizer: card charges + reimbursable vault
    # expenses (a matched vault line is the same expense as its card twin).
    uncategorized = [l for l in lines
                     if l["status"] == "draft" and l["amount_cents"] > 0
                     and (l["source"] == "card_feed" or l.get("reimbursable"))
                     and not l.get("proposed_coa_line")]

    out = {
        "card_without_receipt": no_receipt,
        "vault_without_charge": orphan_vault,
        "uncategorized": uncategorized,
        "rec_report_gaps": [],
        "rec_report_note": "no rec report available",
    }
    if rec_report is not None:
        stmt_charges = [(l["txn_date"], l["amount_cents"]) for l in card]
        out["rec_report_gaps"] = rec_report.containment_gaps(stmt_charges)
        out["rec_report_note"] = (
            f"rec report: {len(rec_report.charges)} cleared charges "
            f"(declared {rec_report.declared_charge_count}), summary "
            f"{'ties' if rec_report.summary_ties() else 'DOES NOT TIE'}; "
            f"reconciled by {rec_report.reconciled_by} on {rec_report.reconciled_on}"
        )
    return out


def _reimbursable(line: dict) -> bool:
    return bool(line.get("reimbursable"))
