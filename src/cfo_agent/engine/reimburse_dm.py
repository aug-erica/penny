"""Slack copy for the reimbursement flow — intake ack, needs-info prompts, the
approver's review request, and approved/paid/rejected confirmations. Kept in the
style of dm_assemble.py (short, plain, reuses _money/_merchant)."""
from __future__ import annotations

from .dm_assemble import _money


def _orig(r: dict) -> str:
    """'(CAD 14.79)' when the reimbursement was converted from a foreign currency."""
    if r.get("orig_currency") and r.get("orig_amount_cents"):
        return f"({r['orig_currency']} {r['orig_amount_cents'] / 100:.2f})"
    return ""


def _one_line(r: dict) -> str:
    parts = [_money(r["amount_cents"]), _orig(r), r.get("expense_date") or ""]
    if r.get("business_purpose"):
        parts.append(f"— {r['business_purpose']}")
    if r.get("proposed_coa_line"):
        parts.append(f"[{r['proposed_coa_line']}]")
    if r.get("project"):
        parts.append(f"→ billable to {r['project']}")
    elif r.get("billable"):
        parts.append("→ billable (client TBD)")
    return " ".join(p for p in parts if p)


def _close_label(cm: str) -> str:
    """'2026-07' -> 'July 2026' for the confirm-back."""
    import calendar
    try:
        y, m = cm.split("-")
        return f"{calendar.month_name[int(m)]} {y}"
    except Exception:
        return cm or ""


def ack_intake(first: str, r: dict, bot_name: str = "Penny") -> str:
    rc = ", receipt ✓" if r.get("receipt_status") == "stored" else ""
    cat = r.get("proposed_coa_line")
    # Penny proposed the category — say so and invite a one-word correction, so the
    # employee never has to recall or type a category name up front.
    cat_note = (f" I filed it under *{cat}* — just reply if that's off. " if cat
                else " ")
    mo = _close_label(r.get("close_month"))
    where = f"in the *{mo}* close queue" if mo else "in the approval queue"
    return (
        f"Got it, {first} — logged a reimbursement for {_one_line(r)}{rc}.{cat_note}"
        f"It's {where} now; I'll let you know once it's approved and set for "
        f"payout. 🧾 _(Reply with the date if this should be a different month.)_")


def category_options_block(options: list) -> str:
    """A short numbered pick-list appended when Penny isn't sure of the category.
    The employee replies with a number instead of retyping the name."""
    if not options:
        return ""
    lines = ["\n_Not 100% sure on the category — if it's off, reply with the number:_"]
    for i, o in enumerate(options, 1):
        lines.append(f"   {i}) {o}")
    return "\n".join(lines)


def bad_choice(first: str, n: int) -> str:
    rng = "1" if n == 1 else f"1–{n}"
    return f"Sorry {first} — I only listed {n} option{'s' if n != 1 else ''}. Reply with {rng}."


def project_ask(first: str, r: dict, bot_name: str = "Penny") -> str:
    """Billable reimbursement, no client yet — ask which client to bill. Open
    question (the pal names the client; Penny fuzzy-matches active projects)."""
    return (f"Got it, {first} — since this one's *billable*, which client/project "
            f"should I bill it to? Just name the client (e.g. \"McCain\") and I'll "
            f"match it to the right project.")


def project_options_block(options: list) -> str:
    """A numbered client/project pick-list — used when the client the pal named
    matches several active projects (e.g. one account, multiple contracts)."""
    if not options:
        return ""
    lines = ["A few could fit — which client/project should I bill this to? "
             "Reply with the number:"]
    for i, o in enumerate(options, 1):
        lines.append(f"   {i}) {o}")
    return "\n".join(lines)


def cancelled(first: str, n: int = 1, bot_name: str = "Penny") -> str:
    what = "that reimbursement" if n <= 1 else f"those {n} reimbursements"
    return (f"Done, {first} — I've cancelled {what}; nothing will be paid out. "
            f"Just start a new message if you want to re-file it. 👍")


def clarify_intent(first: str) -> str:
    """Asked only when Penny can't tell if a pal-initiated DM is a new out-of-pocket
    reimbursement or an answer about a card charge. The pal just replies with a word;
    Penny resolves it against what they already sent (no resending / re-uploading)."""
    return (f"Quick check, {first} — did you pay for this out of pocket (a "
            f"*reimbursement*), or is it about a *company-card charge* I flagged? "
            f"Just reply *reimbursement* or *card* and I'll take it from there — no "
            f"need to resend anything.")


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
                     bot_name: str = "Penny", low_confidence: bool = False) -> str:
    emp = r.get("employee", "someone")
    line = f"*{emp}* — {_one_line(r)}"
    flag = ("\n   ⚠️ *category is my best guess — worth a double-check*"
            if low_confidence else "")
    if r.get("orig_currency"):
        flag += (f"\n   💱 *converted from {r['orig_currency']} — confirm the rate "
                 f"before payout*")
    tail = (f"\nApprove or reject in the queue: {dashboard_url}/reimbursements"
            if dashboard_url else "\nApprove or reject it in the reimbursements queue.")
    return f"🧾 New reimbursement to review:\n   • {line}{flag}{tail}"


def approval_request_group(rows: list, dashboard_url: str = None) -> str:
    emp = rows[0].get("employee", "someone")
    total = sum(r.get("amount_cents") or 0 for r in rows)
    proj = rows[0].get("project")
    head = (f"🧾 New reimbursements to review — *{emp}*, {len(rows)} receipts, "
            f"{_money(total)}")
    if proj:
        head += f" → billable to {proj}"
    if any(r.get("orig_currency") for r in rows):
        head += "\n   💱 *some were converted to USD — confirm the rate before payout*"
    tail = (f"\nApprove in the queue: {dashboard_url}/reimbursements"
            if dashboard_url else "\nApprove them in the reimbursements queue.")
    return head + tail


def group_ack(first: str, rows: list, unreadable: int, need_client: bool,
              bot_name: str = "Penny") -> str:
    """Ack for a multi-receipt submission: one line per receipt + a running total,
    a note on any Penny couldn't read, and (if billable) the single client ask."""
    n = len(rows)
    total = sum(r.get("amount_cents") or 0 for r in rows)
    coa = rows[0].get("proposed_coa_line")
    head = (f"Got it, {first} — logged *{n} reimbursements* from your receipts, "
            f"totaling {_money(total)}" + (f" [{coa}]" if coa else "") + ":")
    lines = [head]
    for r in rows:
        bit = f"   • {_money(r['amount_cents'])} {_orig(r)}".rstrip()
        lines.append(bit)
    if unreadable:
        lines.append(f"⚠️ I couldn't read an amount on {unreadable} of them — "
                     f"reply with the amount(s) and I'll finish those.")
    body = "\n".join(lines)
    if need_client:
        body += "\n\n" + project_ask(first, rows[0])
    elif not unreadable:
        mo = _close_label(rows[0].get("close_month"))
        where = f"in the *{mo}* close queue" if mo else "in the approval queue"
        body += f"\n\nThey're {where} now; I'll confirm once approved. 🧾"
    return body


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
