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

    out = [f"👋 Hey {pal_first} — it's {bot_name}, August's expense bot. "
           f"Good news: *no expense report to file for June.* I've already pulled and "
           f"categorized your {len(charges)} August-card charges ({_money(total)}). "
           f"Just need a couple of quick things from you."]

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
