"""Assemble a per-pal Slack DM from their month's ledger lines.

The agent already categorized everything; the DM asks the pal only for what the
card feed can't know: which travel/billable charges tie to which project, and
the receipts they owe. Everything else is stated as handled.
"""
from __future__ import annotations

import html
from datetime import date

# Categories that might be re-billed to a project — the only ones we ask about.
BILLABLE_CANDIDATE = {"Billable Expense", "General Travel"}
TRIP_GAP_DAYS = 4          # a >4-day gap starts a new "trip"
RECEIPT_THRESHOLD_CENTS = 7500   # non-billable receipts required above this


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _merchant(raw: str) -> str:
    # Chase CSV descriptors carry HTML entities (AT&amp;T) and padded spacing.
    return " ".join(html.unescape(raw).split())


def _trips(travel_lines: list) -> list:
    """Cluster travel charges into trips by date proximity."""
    if not travel_lines:
        return []
    lines = sorted(travel_lines, key=lambda l: l["txn_date"])
    trips, cur = [], [lines[0]]
    for l in lines[1:]:
        gap = (date.fromisoformat(l["txn_date"]) - date.fromisoformat(cur[-1]["txn_date"])).days
        if gap > TRIP_GAP_DAYS:
            trips.append(cur)
            cur = [l]
        else:
            cur.append(l)
    trips.append(cur)
    return trips


def assemble_dm(pal_first: str, lines: list, projects: list, bot_name: str) -> str:
    charges = [l for l in lines if l["status"] != "excluded" and l["amount_cents"] > 0]
    total = sum(l["amount_cents"] for l in charges)
    billable_candidates = [l for l in charges
                           if l.get("proposed_coa_line") in BILLABLE_CANDIDATE]
    receipts_needed = [l for l in charges
                       if l.get("billable") or l["amount_cents"] >= RECEIPT_THRESHOLD_CENTS]

    out = [f"👋 Hey {pal_first} — it's {bot_name}, August's expense bot. I've pulled and "
           f"categorized your {len(charges)} August-card charges for June ({_money(total)}). "
           f"Here's what I've got — give it a look, then a couple quick things below."]

    # The full categorized list, so pals can see (and trust) every call Penny made.
    out.append("\n*Your June expenses, as I categorized them:*")
    out.append("| Date | Merchant | Amount | Category |")
    out.append("|---|---|---:|---|")
    for l in sorted(charges, key=lambda l: l["txn_date"]):
        merch = _merchant(l["merchant_raw"])
        merch = merch[:28] + "…" if len(merch) > 29 else merch
        out.append(f"| {l['txn_date'][5:]} | {merch} | {_money(l['amount_cents'])} "
                   f"| {l.get('proposed_coa_line') or '—'} |")

    marks = ["1️⃣", "2️⃣", "3️⃣"]
    step = iter(marks)

    if billable_candidates:
        trips = _trips(billable_candidates)
        out.append(f"\n*{next(step)} Which project is each of these billable to?* "
                   "(or reply \"not billable\")")
        for t in trips:
            span = (f"{t[0]['txn_date'][5:]}"
                    + (f"–{t[-1]['txn_date'][5:]}" if len(t) > 1 else ""))
            tot = _money(sum(l["amount_cents"] for l in t))
            merchants = ", ".join(sorted({_merchant(l["merchant_raw"]).split()[0].title() for l in t}))
            out.append(f"   • {span} — {merchants} ({tot})")
        proj_names = " · ".join(p["project"] for p in projects[:8])
        out.append(f"   Pick from your active projects: _{proj_names} … (full list of "
                   f"{len(projects)})_")

    if receipts_needed:
        out.append(f"\n*{next(step)} Receipts, please* (reply here with a photo or PDF — I'll file them):")
        for l in receipts_needed:
            out.append(f"   • {_merchant(l['merchant_raw'])} — {_money(l['amount_cents'])} ({l['txn_date']})")
        out.append("   _(Billable items need a receipt, plus anything over $75.)_")

    if not billable_candidates and not receipts_needed:
        out.append("\nNothing else needed from you this month — all set. 🎉")
    else:
        out.append("\nEverything else is handled — meals, software, subscriptions all categorized.")

    out.append("\n_Reimbursables (personal card, WiFi, cell) still go in Expensify for now — "
               "this is just your August card._")
    return "\n".join(out)


def confirm_back(pal_first: str, charges: list, bot_name: str) -> str:
    """After interpreting a reply, echo exactly what was recorded so the pal can
    catch a miss (the conservative interpreter under-tags rather than guess)."""
    charges = [l for l in charges if l["status"] != "excluded" and l["amount_cents"] > 0]
    candidates = [l for l in charges if l.get("proposed_coa_line") in BILLABLE_CANDIDATE]
    # Any charge tagged billable+project counts (a pal can bill a non-travel item).
    billable = [l for l in charges if l.get("billable") and l.get("project")]
    not_billable = [l for l in candidates if l.get("billable") == 0]
    untagged = [l for l in candidates
                if l.get("billable") is None or (l.get("billable") and not l.get("project"))]
    needed = [l for l in charges if l.get("billable") or l["amount_cents"] >= RECEIPT_THRESHOLD_CENTS]
    # A pal who replied with a file counts as "in hand" (stored OR referenced),
    # even if we haven't retained the file to a vault yet — don't nag them.
    have = ("stored", "referenced", "received")
    filed = [l for l in needed if l.get("receipt_status") == "stored"]
    missing_receipts = [l for l in needed if l.get("receipt_status") not in have]

    recategorized = [l for l in charges if l.get("proposed_by") == "reviewer"]

    out = [f"Thanks {pal_first}! Here's what I recorded — reply if any of it's off:"]
    if recategorized:
        out.append("\n✏️ *Recategorized:*")
        for l in recategorized:
            out.append(f"   • {_merchant(l['merchant_raw'])} → {l['proposed_coa_line']}")
    if billable:
        out.append("\n✅ *Billable:*")
        for l in billable:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])} → {l['project']}")
    if not_billable:
        out.append(f"\n🚫 *Not billable:* {len(not_billable)} charge(s) — got it.")
    if untagged:
        out.append("\n❓ *Still need a call on these* — billable to which project, or not?")
        for l in untagged:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])} ({l['txn_date'][5:]})")
    if filed:
        out.append("\n📎 *Receipts filed:*")
        for l in filed:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])} ✓ (saved to the repository)")
    if missing_receipts:
        out.append("\n📎 *Still need receipts for:*")
        for l in missing_receipts:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])}")
    elif needed and not filed:
        out.append("\n📎 Receipts: all in — thank you!")
    if not untagged and not missing_receipts:
        out.append("\nYou're all set. 🎉 Nothing else needed.")
    return "\n".join(out)
