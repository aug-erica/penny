"""Daily close digest for #finance — public accountability during the first
week of the month. Summarizes where each pal stands: responded or not, and what
they still owe (a project call, a receipt, or a reviewer decision)."""
from __future__ import annotations

from datetime import datetime, timezone

from .dm_assemble import BILLABLE_CANDIDATE
from .receipts import receipt_needed
from . import ledger

_HAVE_RECEIPT = ("stored", "referenced", "received")


def post_if_due(conn, cfg, month: str, penny) -> str:
    """Post the daily digest to #finance if it's the first week and we haven't
    already posted today. Idempotent — safe to call on a loop (the listener's
    scheduler does). Returns a short status string."""
    bot = cfg.raw.get("bot", {})
    today = datetime.now(timezone.utc).date().isoformat()
    if int(today[8:10]) > int(bot.get("digest_days", 7)):
        return "past first week"
    if ledger.digest_posted(conn, cfg.client, month, today):
        return "already posted today"
    channel = bot.get("digest_channel")
    if not channel:
        return "no digest channel configured"
    text = build_digest(conn, cfg, month, day=int(today[8:10]))
    penny._post("chat.postMessage", channel=channel, text=text)
    ledger.mark_digest_posted(conn, cfg.client, month, today)
    return "posted"


def _pal_status(charges: list) -> dict:
    """Per-pal open items — the shared predicate lives in month_complete."""
    from .month_complete import open_items
    return open_items(charges)


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
            if s.get("need_note"): bits.append(f"{s['need_note']} invoice note(s)")
            tag = "" if s["responded"] else " _(no reply yet)_"
            out.append(f"   • *{pal}* — {', '.join(bits)}{tag}")

    if all_clear:
        out.append(f"\n✅ *All done:* {', '.join(sorted(all_clear))}")
    out.append("\n_Reply to Penny in your DM to confirm categories or send receipts._")
    return "\n".join(out)
