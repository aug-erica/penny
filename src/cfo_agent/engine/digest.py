"""Daily close digest for #finance — public accountability during the first
week of the month. Summarizes where each pal stands: responded or not, and what
they still owe (a project call, a receipt, or a reviewer decision)."""
from __future__ import annotations

from .dm_assemble import BILLABLE_CANDIDATE, _receipt_needed
from . import ledger

_HAVE_RECEIPT = ("stored", "referenced", "received")


def _pal_status(charges: list) -> dict:
    charges = [c for c in charges if c["status"] != "excluded" and c["amount_cents"] > 0]
    # Needs a project call: travel/billable-category, no project yet, and not
    # already declared not-billable by the pal.
    untagged = [c for c in charges if c.get("proposed_coa_line") in BILLABLE_CANDIDATE
                and not c.get("project") and c.get("billable") != 0]
    needed = _receipt_needed(charges)
    missing_receipts = [c for c in needed if c.get("receipt_status") not in _HAVE_RECEIPT]
    needs_reviewer = [c for c in charges if not c.get("proposed_coa_line")]
    # "Responded" = signals ONLY a human reply produces: a recategorization, a
    # receipt, or an assigned project. (Not billable flags — the LLM sets a
    # billable_guess of 0/1 during categorization, so those aren't reply signals.)
    responded = any(c.get("proposed_by") == "reviewer" or c.get("receipt_status")
                    or c.get("project") for c in charges)
    open_items = len(untagged) + len(missing_receipts) + len(needs_reviewer)
    return {"n": len(charges), "responded": responded, "open": open_items,
            "untagged": len(untagged), "missing_receipts": len(missing_receipts),
            "needs_reviewer": len(needs_reviewer)}


def build_digest(conn, cfg, month: str, day: int = None) -> str:
    lines = ledger.lines_for_month(conn, cfg.client, month)
    by_pal = {}
    for l in lines:
        if l["source"] != "card_feed" and not l.get("reimbursable"):
            continue
        by_pal.setdefault(l.get("cardholder") or "(unknown)", []).append(l)

    stats = {pal: _pal_status(ch) for pal, ch in by_pal.items()}
    total_n = sum(s["n"] for s in stats.values())
    responded = [p for p, s in stats.items() if s["responded"]]
    # Done = nothing left open (whether they replied or simply had nothing to do).
    all_clear = [p for p, s in stats.items() if s["open"] == 0]
    open_pals = {p: s for p, s in stats.items() if s["open"] > 0}

    hdr = f"📊 *{month} expense close — Penny digest*" + (f" (day {day})" if day else "")
    out = [hdr,
           f"{total_n} card charges across {len(stats)} people · "
           f"*{len(all_clear)}/{len(stats)} fully done*"]

    if open_pals:
        out.append("\n*Still open:*")
        for pal, s in sorted(open_pals.items(), key=lambda x: -x[1]["open"]):
            bits = []
            if s["untagged"]: bits.append(f"{s['untagged']} need a project")
            if s["missing_receipts"]: bits.append(f"{s['missing_receipts']} receipts")
            if s["needs_reviewer"]: bits.append(f"{s['needs_reviewer']} to categorize")
            tag = "" if s["responded"] else " _(no reply yet)_"
            out.append(f"   • *{pal}* — {', '.join(bits)}{tag}")

    if all_clear:
        out.append(f"\n✅ *All done:* {', '.join(sorted(all_clear))}")
    out.append("\n_Reply to Penny in your DM to confirm categories or send receipts._")
    return "\n".join(out)
