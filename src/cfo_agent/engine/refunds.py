"""Refund recognition for card charges.

In QBO a card refund is a `Purchase` with `Credit: true` and a POSITIVE line
amount. Penny stores it as a NEGATIVE ledger line and pairs it with the charge it
reverses (same cardholder, same merchant, same amount, within a window). A paired
pair is netted: both lines are `excluded` (nothing to categorize, no receipt to
chase), and the credit is coded in QBO exactly like the original charge (same GL,
same billable customer) so the P&L and any client invoice net to zero.

An unmatched credit (partial refund, original predates Penny, different card) is
kept as an informational negative line -- never generates an ask, can be
recategorized -- and the pal gets a one-line FYI.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from rapidfuzz import fuzz

from . import ledger
from .dm_assemble import _merchant, _money
from .receipts import receipt_needed

FULL_WINDOW_DAYS = 90       # a full refund can lag the charge by months (airlines)
PARTIAL_WINDOW_DAYS = 60    # a partial credit (seat fee, bag) lands sooner
MERCHANT_MIN = 80

_HAVE_RECEIPT = ("stored", "referenced", "received")


def _tokens(norm: str) -> set:
    return {t for t in re.split(r"[^A-Z0-9]+", (norm or "").upper()) if len(t) >= 4}


def merchant_match(a_norm: str, b_norm: str) -> bool:
    """Same merchant? Fuzzy on the normalized descriptors, or a shared token of 4+
    chars (an airline refund descriptor drops the ticket number but keeps the
    'SOUTHWES' / 'UNITED' token)."""
    if not a_norm or not b_norm:
        return False
    if fuzz.token_set_ratio(a_norm.upper(), b_norm.upper()) >= MERCHANT_MIN:
        return True
    return bool(_tokens(a_norm) & _tokens(b_norm))


def find_original(conn, client: str, credit: dict, exclude=frozenset()):
    """The charge a credit reverses. `credit` is a ledger-line dict (negative
    amount_cents). Returns (line, 'full') on an exact-amount match (most recent
    first), (line, 'partial') when exactly one larger same-merchant charge is in
    the shorter window, else (None, None). Charges already fully refunded are
    never matched twice."""
    amt = abs(int(credit["amount_cents"]))
    if not amt or not credit.get("cardholder"):
        return None, None
    d = date.fromisoformat(credit["txn_date"])
    since = (d - timedelta(days=FULL_WINDOW_DAYS)).isoformat()
    cands = ledger.refund_candidates(conn, client, credit["cardholder"], since, credit["txn_date"])
    taken = ledger.charges_refunded(conn, client) | set(exclude)
    others = [c for c in cands if c["external_id"] != credit.get("external_id")]
    if not (credit.get("merchant_norm") or "").strip():
        # No descriptor on the credit (it happens on bank-fed reversals): pair on
        # amount alone, but only when exactly one charge fits -- never guess.
        full = [c for c in others if c["amount_cents"] == amt and c["external_id"] not in taken]
        return (full[0], "full") if len(full) == 1 else (None, None)
    same = [c for c in others
            if merchant_match(credit.get("merchant_norm"), c.get("merchant_norm"))]
    full = [c for c in same if c["amount_cents"] == amt and c["external_id"] not in taken]
    if full:
        return full[0], "full"                 # ORDER BY txn_date DESC -> most recent
    psince = (d - timedelta(days=PARTIAL_WINDOW_DAYS)).isoformat()
    partial = [c for c in same if c["amount_cents"] > amt and c["txn_date"] >= psince
               and c["status"] != "excluded"]
    if len(partial) == 1:
        return partial[0], "partial"
    return None, None


def _original_coding(q, original: dict, mapping: dict):
    """(gl_id, customer_id) the original charge is booked under in QBO -- read from
    the live Purchase when it's a qbo-* line, else from Penny's category map."""
    from ..adapters.books_out import qbo_writer
    gl, cust = None, None
    ext = original.get("external_id") or ""
    if q is not None and ext.startswith("qbo-"):
        try:
            rows = q.query(f"SELECT * FROM Purchase WHERE Id = '{ext[4:]}'")
        except Exception:
            rows = []
        for ln in (rows[0].get("Line", []) if rows else []):
            d = ln.get("AccountBasedExpenseLineDetail")
            if not d:
                continue
            gl = (d.get("AccountRef") or {}).get("value")
            if d.get("BillableStatus") == "Billable":
                cust = (d.get("CustomerRef") or {}).get("value")
            break
        if gl == qbo_writer.PENDING_ID:        # original itself still uncoded
            gl = None
    if not gl and original.get("proposed_coa_line"):
        gl = (mapping or {}).get(original["proposed_coa_line"])
    return gl, cust


def link(conn, cfg, q, credit: dict, credit_purchase, original: dict, kind: str,
         mapping: dict = None, source: str = "poller", log=print) -> dict:
    """Record the pair and net it: credit line excluded + coded like the original
    (ledger AND QBO); on a full refund the original is excluded too, with a
    journaled status decision so a close re-run replays it."""
    from ..adapters.books_out import qbo_writer
    client = cfg.client
    ledger.add_refund_link(conn, client, credit["external_id"], original["external_id"],
                           kind, source=source)
    coa = original.get("proposed_coa_line")
    why = (f"{kind} refund of {original['external_id']} "
           f"({_merchant(original['merchant_raw'])} {original['txn_date']})")
    ledger.set_proposal(conn, credit["id"], coa, "rule", "high", why,
                        billable=original.get("billable"), status="excluded")
    if kind == "full" and original.get("status") != "excluded":
        ledger.set_status(conn, original["id"], "excluded")
        base = (original.get("rationale") or "").strip()
        suffix = f"refunded {credit['txn_date'][5:]} (credit {credit['external_id']})"
        ledger.set_rationale(conn, original["id"], f"{base} — {suffix}" if base else suffix)
        ledger.record_decision(conn, client, original["close_month"], original["external_id"],
                               "status", {"status": "excluded", "reason": "refunded",
                                          "credit": credit["external_id"]},
                               decided_by="penny", source="refund")
    gl, cust = _original_coding(q, original, mapping)
    booked = False
    if gl and q is not None and credit_purchase is not None:
        try:
            qbo_writer.commit_one(q, {"purchase": credit_purchase, "gl_id": gl,
                                      "billable_customer": cust})
            booked = True
        except Exception as exc:
            log(f"[refund] QBO write failed for {credit['external_id']}: {exc}")
    if not gl:
        # Can't mirror -> leave the credit visible to the reviewer rather than hide it.
        ledger.set_status(conn, credit["id"], "flagged")
        log(f"[refund] no GL to mirror for {credit['external_id']} (original "
            f"{original['external_id']} has no category) — flagged for reviewer")
    return {"kind": kind, "gl": gl, "customer": cust, "booked": booked,
            "original": original["external_id"]}


def note_for(credit: dict, original: dict = None, kind: str = None,
             coa: str = None) -> str:
    """The one-line FYI for the pal's drip DM."""
    merch = _merchant(credit["merchant_raw"])
    when = credit["txn_date"][5:]
    amt = _money(abs(credit["amount_cents"]))
    if original is None:
        if coa:
            return (f"↩️ {merch} -{amt} credit posted {when} — booked against *{coa}*; "
                    "I couldn't find the original charge. Reply if it belongs elsewhere.")
        return (f"↩️ {merch} -{amt} credit posted {when} — I couldn't find the original "
                "charge or a category, so I've flagged it for the reviewer.")
    od = original["txn_date"][5:]
    if kind == "full":
        return f"↩️ {merch} {amt} refunded {when} — cancels your {od} charge, nothing needed."
    still = ""
    if (original in receipt_needed([original])
            and original.get("receipt_status") not in _HAVE_RECEIPT):
        still = " (that one still needs its receipt)"
    return (f"↩️ {merch} -{amt} partial refund {when} — netted against your {od} "
            f"{_money(original['amount_cents'])} charge{still}.")


# ---- backfill ---------------------------------------------------------------
def backfill(conn, cfg, q, month: str, post: bool = False, log=print) -> dict:
    """Repair credits the poller ingested as positive charges before it knew about
    refunds: re-read the month's Purchases from QBO, and for each Credit=true one
    that Penny holds as a positive line, flip the sign, pair it, and mirror the
    coding. Dry-run unless `post`. Returns per-pal FYI notes so a human can decide
    whether to send them."""
    from collections import defaultdict

    from ..adapters.books_out import qbo_writer
    mapping = qbo_writer.load_mapping(cfg.client)
    out = {"month": month, "credits": 0, "linked": [], "unmatched": [], "skipped": [],
           "notes": defaultdict(list)}
    for p in qbo_writer.fetch_month(q, month):
        if not p.get("Credit"):
            continue
        out["credits"] += 1
        ext = f"qbo-{p['Id']}"
        line = ledger.line_by_external_id(conn, cfg.client, ext)
        if not line:
            out["skipped"].append((ext, "not in ledger"))
            continue
        if any(r["credit_external_id"] == ext for r in ledger.refund_links(conn, cfg.client)):
            out["skipped"].append((ext, "already linked"))
            continue
        cents = -abs(int(line["amount_cents"]))
        probe = dict(line, amount_cents=cents)
        orig, kind = find_original(conn, cfg.client, probe)
        who = line.get("cardholder") or "?"
        desc = f"{line['txn_date']} {_merchant(line['merchant_raw'])} {_money(cents)} ({who})"
        if not post:
            if orig:
                out["linked"].append((ext, kind, orig["external_id"], desc))
            else:
                out["unmatched"].append((ext, desc))
            out["notes"][who].append(note_for(probe, orig, kind, line.get("proposed_coa_line")))
            continue
        ledger.set_amount(conn, line["id"], cents)
        fresh = ledger.line_by_external_id(conn, cfg.client, ext)
        if orig:
            res = link(conn, cfg, q, fresh, p, orig, kind, mapping, source="backfill", log=log)
            out["linked"].append((ext, kind, orig["external_id"], desc))
            out["notes"][who].append(note_for(fresh, orig, kind))
        else:
            if fresh["status"] not in ("excluded", "approved", "posted"):
                ledger.set_status(conn, fresh["id"], "flagged")
            out["unmatched"].append((ext, desc))
            out["notes"][who].append(note_for(fresh, None, None, fresh.get("proposed_coa_line")))
    out["notes"] = dict(out["notes"])
    return out
