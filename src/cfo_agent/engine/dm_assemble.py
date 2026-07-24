"""Assemble a per-pal Slack DM from their month's ledger lines.

The agent already categorized everything; the DM asks the pal only for what the
card feed can't know: which travel/billable charges tie to which project, and
the receipts they owe. Everything else is stated as handled.
"""
from __future__ import annotations

import html
from datetime import date

from .receipts import RECEIPT_THRESHOLD_CENTS, receipt_needed

# Categories that might be re-billed to a project — the only ones we ask about.
BILLABLE_CANDIDATE = {"Billable Expense", "General Travel"}
TRIP_GAP_DAYS = 4          # a >4-day gap starts a new "trip"


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
    receipts_needed = receipt_needed(charges)
    month_name = (date.fromisoformat(charges[0]["close_month"] + "-01").strftime("%B")
                  if charges else "this month")

    out = [f"👋 Hey {pal_first} — it's {bot_name}, August's expense bot. I've pulled and "
           f"categorized your {len(charges)} August-card charges for {month_name} ({_money(total)}). "
           f"Here's what I've got, grouped by category so it's easy to skim — then a "
           f"couple quick things below."]

    # Grouped by category with subtotals (reviewer feedback: easier to skim/verify).
    from collections import defaultdict
    groups = defaultdict(list)
    for l in charges:
        groups[l.get("proposed_coa_line") or "(uncategorized)"].append(l)
    for cat in sorted(groups):
        rows = sorted(groups[cat], key=lambda l: l["txn_date"])
        sub = sum(l["amount_cents"] for l in rows)
        out.append(f"\n*{cat}* — {len(rows)} charge(s), {_money(sub)}")
        out.append("| Date | Merchant | Amount |")
        out.append("|---|---|---:|")
        for l in rows:
            merch = _merchant(l["merchant_raw"])
            merch = merch[:30] + "…" if len(merch) > 31 else merch
            out.append(f"| {l['txn_date'][5:]} | {merch} | {_money(l['amount_cents'])} |")

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
        out.append(f"   _(Billable items need a receipt, plus anything over "
                   f"${RECEIPT_THRESHOLD_CENTS // 100}.)_")

    if not billable_candidates and not receipts_needed:
        out.append("\nNothing else needed from you this month — all set. 🎉")
    else:
        out.append("\nEverything else is handled — meals, software, subscriptions all categorized.")

    out.append("\n_Reimbursables (personal card, WiFi, cell) still go in Expensify for now — "
               "this is just your August card._")
    return "\n".join(out)


def confirm_back(pal_first: str, charges: list, bot_name: str, touched=None) -> str:
    """After interpreting a reply, echo exactly what was recorded so the pal can
    catch a miss (the conservative interpreter under-tags rather than guess).
    `touched` (external_ids changed by this reply) scopes the "recorded" echoes so
    we don't re-list earlier decisions when charges span months; the "still need"
    prompts stay full-state."""
    charges = [l for l in charges if l["status"] != "excluded" and l["amount_cents"] > 0]
    _this = lambda l: touched is None or l["external_id"] in touched
    candidates = [l for l in charges if l.get("proposed_coa_line") in BILLABLE_CANDIDATE]
    # Any charge tagged billable+project counts (a pal can bill a non-travel item).
    billable = [l for l in charges if l.get("billable") and l.get("project")]
    not_billable = [l for l in candidates if l.get("billable") == 0 and _this(l)]
    untagged = [l for l in candidates
                if l.get("billable") is None or (l.get("billable") and not l.get("project"))]
    needed = receipt_needed(charges)
    # A pal who replied with a file counts as "in hand" (stored OR referenced),
    # even if we haven't retained the file to a vault yet — don't nag them.
    have = ("stored", "referenced", "received")
    filed = [l for l in needed if l.get("receipt_status") == "stored" and _this(l)]
    missing_receipts = [l for l in needed if l.get("receipt_status") not in have]

    # "Recorded" echoes are scoped to this reply's changes; "still need" is full state.
    recategorized = [l for l in charges if l.get("proposed_by") == "reviewer" and _this(l)]
    # Billable expenses need a one-line invoice description for Natalie (what
    # Expensify captured). Ask for any that don't have one yet.
    need_note = [l for l in billable if not l.get("billable_note")]

    out = [f"Thanks {pal_first}! Here's what I recorded — reply if any of it's off:"]
    if recategorized:
        out.append("\n✏️ *Recategorized:*")
        for l in recategorized:
            out.append(f"   • {_merchant(l['merchant_raw'])} → {l['proposed_coa_line']}")
    if [l for l in billable if _this(l)]:
        out.append("\n✅ *Billable:*")
        for l in [l for l in billable if _this(l)]:
            note = f" — _{l['billable_note']}_" if l.get("billable_note") else ""
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])} → {l['project']}{note}")
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
    if need_note:
        out.append("\n📝 *One line for the invoice, please* — since these are billable, "
                   "what was each one for? (Natalie puts this on the client invoice.)")
        for l in need_note:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])} ({l['project']})")
    if not untagged and not missing_receipts and not need_note:
        out.append("\nYou're all set. 🎉 Nothing else needed.")
    return "\n".join(out)
