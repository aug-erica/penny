"""Continuous close: instead of a month-end burst, Penny catches each card
charge as it posts and handles it on the spot.

On each poll it looks at the QBO 'Credit Card Pending' charges for the enrolled
cardholders (prototype: Erica + Purvi), finds the ones it hasn't seen, then for
each: adds it to the ledger, categorizes it (flywheel/cascade), writes the
category back to QBO, and DMs the cardholder to confirm the category and send a
receipt. Idempotent — a charge already in the ledger as `qbo-<id>` is skipped, so
polling repeatedly never double-pings or double-writes.
"""
from __future__ import annotations

from collections import defaultdict

from . import ledger
from .normalize import normalize_merchant
from .categorize import rules as rules_mod, history as history_mod
from .dm_assemble import BILLABLE_CANDIDATE, _merchant, _money
from ..models import Proposal

RECEIPT_THRESHOLD_CENTS = 10000


def _who_of(card_name: str, names: list) -> str:
    """QBO card name -> Penny cardholder (hyphen-safe: 'K. Ward' -> 'Karina
    Mangu-Ward'). Parent card (no ' - ') is Erica's."""
    if " - " not in card_name:
        return "Erica Seldin"
    surname = card_name.rsplit(" - ", 1)[1].split(" (")[0].strip().split()[-1].lower()
    for n in names:
        if surname in n.lower().replace("-", " ").split():
            return n
    return card_name


def _uid_for(cfg, cardholder: str):
    for uid, name in cfg.raw.get("bot", {}).get("slack_users", {}).items():
        if name == cardholder:
            return uid
    return None


def _categorize(conn, cfg, line, learned):
    coa = learned.get(line["merchant_norm"])
    prop = (Proposal(coa_line=coa, proposed_by="rule", confidence="high",
                     rationale="learned rule (reviewer correction)")
            if coa else rules_mod.propose(cfg.rules, line))
    if prop is None:
        prop = history_mod.propose(conn, cfg.client, line)
    if prop is None:
        from .categorize import llm as llm_mod
        pairs = llm_mod.propose_batch(conn, cfg, [line])
        prop = pairs[0][1] if pairs else None
    return prop


def run_once(conn, cfg, penny, month: str, post: bool = True, log=print) -> dict:
    """Process newly-posted charges for the enrolled cardholders. Returns a
    summary dict."""
    from ..adapters.card_feed.qbo_feed import QBOFeed
    from ..adapters.books_out import qbo_writer

    cc = cfg.section("continuous_close")
    enrolled = set(cc.get("cardholders", []))
    if not enrolled:
        return {"skipped": "no cardholders enrolled"}
    names = list(cfg.raw.get("cardholders", {}).values())
    entity = cfg.raw.get("entity") or "August"

    q = QBOFeed(realm_id="")
    q._refresh_access_token()
    mapping = qbo_writer.load_mapping(cfg.client)
    tr = None
    try:
        hit = q.query("SELECT Id FROM Customer WHERE DisplayName LIKE '%Transcarent%'")
        tr = hit[0]["Id"] if hit else None
    except Exception:
        pass

    seen = {l["external_id"] for l in ledger.lines_for_month(conn, cfg.client, month)}
    learned = ledger.learned_rules(conn, cfg.client)
    new_by_pal = defaultdict(list)

    for p in qbo_writer.fetch_pending(q, month):
        ext = f"qbo-{p['Id']}"
        if ext in seen:
            continue
        ln = next((l for l in p["Line"]
                   if (l.get("AccountBasedExpenseLineDetail") or {}).get("AccountRef", {})
                   .get("value") == qbo_writer.PENDING_ID), None)
        if not ln:
            continue
        who = _who_of(p.get("AccountRef", {}).get("name", ""), names)
        if who not in enrolled:
            continue
        merch = (ln.get("Description") or "").strip()
        line = {"external_id": ext, "client": cfg.client, "entity": entity,
                "close_month": month, "txn_date": p["TxnDate"], "merchant_raw": merch,
                "merchant_norm": normalize_merchant(merch),
                "amount_cents": round(float(ln["Amount"]) * 100), "currency": "USD",
                "source": "card_feed", "cardholder": who, "status": "draft"}
        # Categorize in-memory (rules/history/LLM read the line dict, not the DB row).
        prop = _categorize(conn, cfg, line, learned)
        line["proposed_coa_line"] = prop.coa_line if prop else None
        line["billable"] = prop.billable if prop else None
        if not post:
            new_by_pal[who].append(line)   # preview only — no writes
            continue
        ledger.upsert_line(conn, line)
        fresh = ledger.line_by_external_id(conn, cfg.client, ext)
        if prop:
            ledger.set_proposal(conn, fresh["id"], prop.coa_line, prop.proposed_by,
                                prop.confidence, prop.rationale, billable=prop.billable)
        gl = mapping.get(prop.coa_line) if prop else None
        if gl:
            try:
                qbo_writer.commit_one(q, {"purchase": p, "gl_id": gl,
                                          "billable_customer": None})
            except Exception:
                log(f"[continuous] QBO write failed for {ext}: keep for reviewer")
        new_by_pal[who].append(ledger.line_by_external_id(conn, cfg.client, ext) or line)

    sent = 0
    if post:
        for who, items in new_by_pal.items():
            uid = _uid_for(cfg, who)
            if uid:
                penny.send_dm(uid, _compose(who, items, cfg))
                sent += 1
    return {"new": sum(len(v) for v in new_by_pal.values()),
            "pals": {w: len(v) for w, v in new_by_pal.items()}, "dms_sent": sent}


def _compose(cardholder: str, items: list, cfg) -> str:
    first = cardholder.split()[0]
    out = [f"🧾 Hi {first} — {len(items)} new charge(s) just posted on your August card. "
           "Here's how I've booked them:"]
    need_receipt = []
    for l in items:
        coa = l.get("proposed_coa_line") or "—"
        tag = ""
        if l.get("proposed_coa_line") in BILLABLE_CANDIDATE:
            tag = "  ← which project? (or \"not billable\")"
        out.append(f"   • {l['txn_date'][5:]} {_merchant(l['merchant_raw'])} "
                   f"{_money(l['amount_cents'])} → *{coa}*{tag}")
        if l["amount_cents"] >= RECEIPT_THRESHOLD_CENTS:
            need_receipt.append(l)
    if need_receipt:
        out.append("\n📎 Please send a receipt (photo or PDF) for:")
        for l in need_receipt:
            out.append(f"   • {_merchant(l['merchant_raw'])} {_money(l['amount_cents'])}")
    out.append("\nReply here if a category's off — otherwise you're all set, these are "
               "already in the books. (This is the new rolling close, so you'll get these "
               "as charges happen instead of a pile at month-end.)")
    return "\n".join(out)
