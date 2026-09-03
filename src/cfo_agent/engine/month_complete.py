"""One-time "your month is complete" notices.

Every existing "all set" line in Penny is the tail of a reply -- a pal only sees
it if they happen to message once they're clear. This module tells people
proactively: each cardholder gets ONE DM when their prior month's card charges
are fully handled (pal-side: categorized, required receipts in, billable calls
made + invoice note), and #finance gets ONE post when every enrolled cardholder
is clear. Idempotent via the `notices` table; runs from the poll loop.

Only the PREVIOUS calendar month is ever eligible, and only after a few days'
grace for late-posting charges -- a "complete" verdict on the month in progress
is meaningless (charges are still posting; this is exactly what made the old
daily digest report a false all-clear).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import ledger
from .dm_assemble import BILLABLE_CANDIDATE, _money
from .receipts import receipt_needed

_HAVE_RECEIPT = ("stored", "referenced", "received")
DEFAULT_GRACE_DAYS = 3


def open_items(charges: list) -> dict:
    """Per-pal completeness. `open` counts what a human still owes: a project
    call on a billable-candidate charge, a required receipt, an uncategorized
    charge, or an invoice note on a billable one. Excluded lines (payments,
    refund pairs) and credits never count."""
    charges = [c for c in charges if c["status"] != "excluded" and c["amount_cents"] > 0]
    untagged = [c for c in charges if c.get("proposed_coa_line") in BILLABLE_CANDIDATE
                and not c.get("project") and c.get("billable") != 0]
    needed = receipt_needed(charges)
    missing_receipts = [c for c in needed if c.get("receipt_status") not in _HAVE_RECEIPT]
    needs_reviewer = [c for c in charges if not c.get("proposed_coa_line")]
    need_note = [c for c in charges
                 if c.get("billable") == 1 and c.get("project") and not c.get("billable_note")]
    # "Responded" = signals ONLY a human reply produces (not the LLM's billable guess).
    responded = any(c.get("proposed_by") == "reviewer" or c.get("receipt_status")
                    or c.get("project") for c in charges)
    return {"n": len(charges), "responded": responded,
            "open": len(untagged) + len(missing_receipts) + len(needs_reviewer) + len(need_note),
            "untagged": len(untagged), "missing_receipts": len(missing_receipts),
            "needs_reviewer": len(needs_reviewer), "need_note": len(need_note),
            "receipts_required": len(needed),
            "total_cents": sum(c["amount_cents"] for c in charges)}


def eligible_month(now, grace_days: int = DEFAULT_GRACE_DAYS):
    """The month whose completeness may be judged right now: the PREVIOUS
    calendar month, once `grace_days` of the new month have passed. Else None."""
    if now.day <= grace_days:
        return None
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def _month_name(month: str, year: bool = False) -> str:
    return date.fromisoformat(month + "-01").strftime("%B %Y" if year else "%B")


def _first(pal: str) -> str:
    return "there" if "/" in pal else pal.split()[0]


def pal_message(pal: str, month: str, st: dict) -> str:
    mo = _month_name(month)
    n = st["n"]
    bits = f"{n} charge{'s' if n != 1 else ''} categorized"
    if st["receipts_required"]:
        bits += ", receipts in"
    return (f"🎉 Hi {_first(pal)} — your {mo} card is all wrapped up: {bits}, everything "
            f"booked. Nothing more needed from you for {mo}. Thanks!")


def team_message(month: str, n_pals: int, n_charges: int, total_cents: int) -> str:
    return (f"✅ *{_month_name(month, year=True)} card close — all clear.* "
            f"{n_pals}/{n_pals} cardholders done · {n_charges} charges · {_money(total_cents)} "
            "— categorized, receipted, and in QBO. Ready for review.")


def run_once(conn, cfg, penny, month: str, post: bool = True, log=print) -> dict:
    """DM each enrolled cardholder whose month is clear (once), then post the
    team all-clear to #finance (once) when nobody has anything open."""
    from .continuous_close import _uid_for
    client = cfg.client
    enrolled = set(cfg.section("continuous_close").get("cardholders", []))
    by_pal = defaultdict(list)
    for l in ledger.lines_for_month(conn, client, month, source="card_feed"):
        if l.get("cardholder") in enrolled:
            by_pal[l["cardholder"]].append(l)

    res = {"month": month, "pals_notified": [], "already": [], "still_open": {},
           "team_posted": False, "team_already": False, "n_pals": 0}
    n_charges, total = 0, 0
    for pal, ch in sorted(by_pal.items()):
        st = open_items(ch)
        if st["n"] == 0:
            continue                       # only excluded/credit lines -> nothing to say
        res["n_pals"] += 1
        n_charges += st["n"]
        total += st["total_cents"]
        if st["open"]:
            res["still_open"][pal] = st["open"]
            continue
        if ledger.notice_sent(conn, client, month, "pal_complete", pal):
            res["already"].append(pal)
            continue
        uid = _uid_for(cfg, pal)
        if not uid:
            log(f"[complete] {pal}: clear, but no Slack user mapped — skipped")
            continue
        if post:
            penny.send_dm(uid, pal_message(pal, month, st))
            ledger.mark_notice_sent(conn, client, month, "pal_complete", pal)
        res["pals_notified"].append(pal)

    if res["n_pals"] and not res["still_open"]:
        if ledger.notice_sent(conn, client, month, "month_complete", "team"):
            res["team_already"] = True
        else:
            channel = cfg.raw.get("bot", {}).get("digest_channel")
            if channel and post:
                penny._post("chat.postMessage", channel=channel,
                            text=team_message(month, res["n_pals"], n_charges, total))
                ledger.mark_notice_sent(conn, client, month, "month_complete", "team")
            res["team_posted"] = bool(channel)
    return res
