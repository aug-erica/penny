"""Slack copy for the reimbursement flow — intake ack, needs-info prompts, the
approver's review request, and approved/paid/rejected confirmations. Kept in the
style of dm_assemble.py (short, plain, reuses _money/_merchant)."""
from __future__ import annotations

from .dm_assemble import _money


def _one_line(r: dict) -> str:
    parts = [_money(r["amount_cents"]), r.get("expense_date") or ""]
    if r.get("business_purpose"):
        parts.append(f"— {r['business_purpose']}")
    if r.get("proposed_coa_line"):
        parts.append(f"[{r['proposed_coa_line']}]")
    return " ".join(p for p in parts if p)


def ack_intake(first: str, r: dict, bot_name: str = "Penny") -> str:
    rc = ", receipt ✓" if r.get("receipt_status") == "stored" else ""
    return (
        f"Got it, {first} — logged a reimbursement for {_one_line(r)}{rc}. "
        f"It's in the approval queue now; I'll let you know once it's approved "
        f"and set for payout. 🧾")


def needs_info(first: str, r: dict, missing: list, trigger: str = "reimburse",
               followup: bool = False, bot_name: str = "Penny") -> str:
    """`missing` is a subset of {'amount','business_purpose','category','receipt'}.
    `followup=True` when the person is already in a thread about this charge — then
    Penny remembers everything they've sent and only asks for what's left, in-thread
    (no re-sending, no re-uploading the receipt)."""
    labels = {
        "receipt": "the *receipt* (photo or PDF) — required so this stays a "
                   "non-taxable reimbursement",
        "amount": "the *amount* you paid",
        "business_purpose": "a one-line *business purpose* (what it was for)",
        "category": "the *expense category* (e.g. Groceries & Meals, General "
                    "Travel, Office Supplies, Professional Development) — so we book "
                    "it to the right account",
    }
    have = _one_line(r) if r.get("amount_cents") else None
    lead = (f"Thanks {first} — I've got {have}, but I still need "
            if have else f"Thanks {first} — to reimburse you I need ")
    need = " and ".join(labels[m] for m in missing if m in labels)
    if followup:
        # Everything already sent is kept — just add what's left, right here.
        extra = " (attach the receipt)" if "receipt" in missing else ""
        tail = (f" — just reply here in this thread{extra} and I'll add it. "
                "No need to resend anything you've already sent.")
    else:
        tail = f'. Send it to me in one message that starts with "{trigger}"'
        tail += " and attach the receipt." if "receipt" in missing else "."
    return f"{lead}{need}{tail}"


def policy_warning(violations: list) -> str:
    """A block appended to the confirm-back when a submission is over a policy cap
    or stale. Penny still logs it and flags it for the reviewer."""
    if not violations:
        return ""
    label = "a policy flag" if len(violations) == 1 else "a couple of policy flags"
    lines = [f"\n⚠️ *Heads up — {label}:*"]
    for v in violations:
        lines.append(f"   • {v['message']}.")
    lines.append("I've still logged it and flagged it for Purvi — she'll decide "
                 "whether it can go through as submitted.")
    return "\n".join(lines)


def already(first: str, r: dict, bot_name: str = "Penny") -> str:
    return (f"Thanks {first} — this reimbursement ({_one_line(r)}) is already "
            f"*{r.get('status')}*, so I can't change it from here. Ping Purvi or "
            f"Erica if it needs a correction.")


def approval_request(r: dict, dashboard_url: str = None,
                     bot_name: str = "Penny") -> str:
    emp = r.get("employee", "someone")
    line = f"*{emp}* — {_one_line(r)}"
    tail = (f"\nApprove or reject in the queue: {dashboard_url}/reimbursements"
            if dashboard_url else "\nApprove or reject it in the reimbursements queue.")
    return f"🧾 New reimbursement to review:\n   • {line}{tail}"


def approved(first: str, r: dict, bot_name: str = "Penny") -> str:
    by = f" by {r['approver']}" if r.get("approver") else ""
    return (f"✅ {first}, your reimbursement for {_one_line(r)} was approved{by}. "
            f"It's set for the next Justworks payout — I'll confirm when it's paid.")


def paid(first: str, r: dict, bot_name: str = "Penny") -> str:
    return (f"💸 {first}, your reimbursement for {_one_line(r)} has been paid out "
            f"through Justworks. All set!")


def rejected(first: str, r: dict, reason: str = None,
             bot_name: str = "Penny") -> str:
    why = f" Reason: {reason}" if reason else ""
    return (f"I wasn't able to approve your reimbursement for {_one_line(r)}.{why} "
            f"Reply if you'd like to resubmit with more detail.")
