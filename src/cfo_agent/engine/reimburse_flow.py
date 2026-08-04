"""The reimbursement brain — intake, approval, payout export, mark-paid, and
stipend generation. Parallel to reply_flow.process_pal_reply (which handles card
charges); this handles employee-initiated out-of-pocket reimbursements.

Lifecycle: submitted -> approved/rejected -> exported (Justworks payout prepared)
-> paid. Receipts are mandatory (accountable-plan: receipt + business purpose +
timeliness keeps the reimbursement non-taxable).
"""
from __future__ import annotations

import re
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import (db, disambiguate, fx, ledger, projects, receipt_read, receipt_store,
               reimburse_dm, reimburse_parse, reimburse_policy)
from .continuous_close import _uid_for
from .normalize import normalize_merchant


def _qbo_enabled(cfg) -> bool:
    """Auto-write to QBO only in the live cloud env (DATABASE_URL set → Postgres kv
    holds the QBO token). This also keeps tests/local runs from ever posting to
    production QBO by accident."""
    return bool(db.database_url()) and bool(cfg.section("reimbursements").get("qbo"))


def _month_end(month: str) -> str:
    """Last calendar day of 'YYYY-MM' as 'YYYY-MM-DD' (the grouped Bill's date)."""
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    return f"{month}-{calendar.monthrange(y, m)[1]:02d}"
from ..config import env


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _close_month_for(expense_date: str, fallback: str) -> str:
    """A reimbursement belongs to the close of the month it was INCURRED, not the
    month it was submitted — so a July expense filed on Aug 3 lands in the July
    close (Levi/Purvi's 'why is Penny putting this in August?'). Falls back to the
    active month when the date is missing/garbled."""
    m = re.match(r"(\d{4}-\d{2})-\d{2}$", expense_date or "")
    return m.group(1) if m else fallback


# Withdraw/cancel intent in a reimbursement thread.
_CANCEL = re.compile(
    r"\b(cancel|withdraw|nevermind|never mind|nvm|scratch that|disregard this|"
    r"delete this|remove this|forget (this|it))\b", re.I)


def _wants_cancel(text: str) -> bool:
    t = (text or "").lower()
    if "don't cancel" in t or "do not cancel" in t or "not cancel" in t:
        return False
    return bool(_CANCEL.search(t))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def approver_for(cfg, employee: str) -> str:
    """Who approves a reimbursement from `employee`. Primary reviewer, unless the
    submitter IS the primary — then the secondary (no self-approval)."""
    rc = cfg.section("reimbursements").get("approver", {}) or {}
    primary = rc.get("primary") or cfg.raw.get("reviewer", {}).get("primary")
    secondary = rc.get("secondary") or cfg.raw.get("reviewer", {}).get("secondary")
    return secondary if employee == primary else primary


def _dashboard_url() -> str:
    return (env("DASHBOARD_URL") or "").rstrip("/") or None


def _ingest_receipt(cfg, month, employee, expense_date, slack, file_id):
    """Download + store ONE receipt file and read its amount, merchant, and
    currency (Claude). Returns {receipt_status, receipt_link, amount_cents,
    merchant, currency} — all None-ish if there's no file or it can't be read."""
    out = {"receipt_status": None, "receipt_link": None, "amount_cents": None,
           "merchant": None, "currency": None}
    if not (slack and file_id):
        return out
    tmp = Path(tempfile.mktemp())
    try:
        slack.download_file(file_id, tmp)
        info = receipt_read.read_receipt(tmp)
        out["amount_cents"] = info.get("amount_cents")
        out["merchant"] = info.get("merchant") or None
        out["currency"] = info.get("currency")
        pseudo = {"merchant_raw": (out["merchant"] or "reimbursement"),
                  "amount_cents": out["amount_cents"] or 0, "txn_date": expense_date}
        out["receipt_status"] = "stored"
        out["receipt_link"] = str(receipt_store.store_file(cfg, month, employee, pseudo, tmp))
    except Exception:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return out


def _to_usd(amount_cents, currency, expense_date):
    """Convert an original amount to what Penny should reimburse (USD). Returns
    (amount_cents_to_pay, orig_currency, orig_amount_cents, rate):
      - USD or unknown currency -> pay as-is, no orig marker.
      - non-USD, converted -> pay the USD amount; keep the original for the record.
      - non-USD, rate unavailable -> pay the original (flagged), keep the marker so
        the reviewer converts.
    """
    cur = (currency or "USD").upper()
    if amount_cents is None or cur == "USD":
        return amount_cents, None, None, None
    usd, rate = fx.to_usd_cents(amount_cents, cur, expense_date)
    if usd is not None:
        return usd, cur, amount_cents, rate
    return amount_cents, cur, amount_cents, None   # couldn't convert -> flag as-is


# Events that carry a pending numbered pick-list Penny offered the employee, per
# dimension. A matching *_chosen/*_set resolves it. `category` = which COA line;
# `project` = which client/contract to bill (for a billable reimbursement).
_CATEGORY_OPTS = "category_options"
_PROJECT_OPTS = "project_options"
_OPT_DIM = {_CATEGORY_OPTS: "category", _PROJECT_OPTS: "project"}
_RESOLVES = {"category_chosen": "category", "category_set": "category",
             "project_chosen": "project", "project_set": "project"}


def _pending_options(conn, cfg, external_id: str):
    """The still-open numbered pick-list for this reimbursement as
    (dimension, options), or (None, None). Reads the append-only journal: an
    options event with no matching resolve event after it is the open one."""
    import json
    dim, opts = None, None
    for ev in ledger.reimbursement_events(conn, cfg.client, external_id):
        e = ev["event"]
        if e in _OPT_DIM:
            try:
                o = (json.loads(ev.get("detail_json") or "{}") or {}).get("options") or []
            except (ValueError, TypeError):
                o = []
            dim, opts = (_OPT_DIM[e], o) if o else (None, None)
        elif _RESOLVES.get(e) == dim:
            dim, opts = None, None
    return dim, opts


def _is_billable(cfg, coa) -> bool:
    """True when this category means 'bill it to a client' (so Penny must capture
    which project) — mirrors the card side's billable_categories."""
    return bool(coa) and coa in (cfg.section("billable").get("billable_categories") or [])


def _propagate_group(conn, cfg, anchor):
    """Apply the anchor's shared fields (category, billable, client) to its
    multi-receipt siblings and re-finalize each one's status by its OWN basics
    (a sibling still missing an amount stays needs_info)."""
    gid = anchor.get("group_id")
    if not gid:
        return
    rc = cfg.section("reimbursements")
    for m in ledger.reimbursements_in_group(conn, cfg.client, gid):
        if m["id"] == anchor["id"] or m["status"] in ("approved", "exported", "paid"):
            continue
        ledger.update_reimbursement_fields(
            conn, m["id"], proposed_coa_line=anchor.get("proposed_coa_line"),
            billable=anchor.get("billable"), project=anchor.get("project"))
        mm = _missing_fields(rc, m.get("amount_cents"), m.get("business_purpose"),
                             anchor.get("proposed_coa_line"), m.get("receipt_status"))
        ledger.set_reimbursement_status(conn, m["id"], "needs_info" if mm else "submitted")


def _ack_or_ask_client(conn, cfg, ext, rid, first, slack, cat_conf="high",
                       cat_options=None):
    """Tail shared by intake/follow-up/pick once the submitter-required basics are
    in. Persists the billable flag; if the category is billable but no client is
    set yet, asks which client to bill (numbered pick-list when one was offered,
    else an open ask) instead of acking; otherwise acks + notifies the approver.
    Propagates shared fields to any multi-receipt siblings. Returns (status,
    fresh_row, confirm_back)."""
    row = ledger.reimbursement_by_id(conn, rid)
    billable = _is_billable(cfg, row.get("proposed_coa_line"))
    if bool(row.get("billable")) != billable:
        ledger.update_reimbursement_fields(conn, rid, billable=1 if billable else 0)
        row = ledger.reimbursement_by_id(conn, rid)
    if billable and not row.get("project"):
        ledger.set_reimbursement_status(conn, rid, "needs_info")
        ledger.record_reimbursement_event(conn, cfg.client, ext, "project_prompt", {},
                                          actor="system", source="penny")
        row = ledger.reimbursement_by_id(conn, rid)
        dim, opts = _pending_options(conn, cfg, ext)
        cb = (reimburse_dm.project_options_block(opts)
              if dim == "project" and opts else reimburse_dm.project_ask(first, row))
        return "needs_info", row, cb
    ledger.set_reimbursement_status(conn, rid, "submitted")
    row = ledger.reimbursement_by_id(conn, rid)
    _propagate_group(conn, cfg, row)
    if row.get("group_id"):
        grp = ledger.reimbursements_in_group(conn, cfg.client, row["group_id"])
        cb = reimburse_dm.group_ack(first, grp, 0, need_client=False)
        _notify_approver_group(cfg, grp, slack)
    else:
        cb = reimburse_dm.ack_intake(first, row)
        if cat_options:
            cb += reimburse_dm.category_options_block(cat_options)
        _notify_approver(cfg, row, slack, low_confidence=(cat_conf == "low"))
    return "submitted", row, cb


def _propose_category(conn, cfg, merchant, parsed):
    """Best category for a reimbursement. A known merchant on the receipt hits the
    same deterministic flywheel the card side uses (learned reviewer corrections,
    then seed rules) for a high-confidence pick; otherwise fall back to the LLM's
    ranked guess from the parse. Returns (coa_line, confidence, options)."""
    if merchant:
        from .categorize import rules as rules_mod
        pseudo = {"merchant_norm": normalize_merchant(merchant),
                  "merchant_raw": merchant, "cardholder": None}
        coa = ledger.learned_rules(conn, cfg.client).get(pseudo["merchant_norm"])
        prop = None
        if coa:
            return coa, "high", [coa]
        prop = rules_mod.propose(cfg.rules, pseudo)
        if prop is not None and prop.coa_line in set(cfg.coa_lines):
            return prop.coa_line, prop.confidence, [prop.coa_line]
    return (parsed.get("proposed_coa_line"),
            parsed.get("category_confidence") or ("medium" if parsed.get("proposed_coa_line") else "low"),
            parsed.get("category_options") or ([parsed["proposed_coa_line"]]
                                               if parsed.get("proposed_coa_line") else []))


def _missing_fields(rc, amount_cents, purpose, coa, receipt_status) -> list:
    """What the SUBMITTER must still provide for a complete reimbursement. Category
    is deliberately NOT here — Penny proposes it (the employee never has to recall a
    category name), and a low-confidence guess is flagged for the reviewer, not
    bounced back to the submitter. A receipt is only required at/above the threshold
    (default $100). When the amount isn't known yet we don't ask for a receipt (we
    may not need it once the amount comes in)."""
    missing = []
    if not amount_cents:
        missing.append("amount")
    if not purpose:
        missing.append("business_purpose")
    threshold = rc.get("receipt_threshold_cents", 10000)
    if (rc.get("receipt_required", True) and amount_cents and amount_cents >= threshold
            and receipt_status != "stored"):
        missing.append("receipt")
    return missing


def _fx_rationale(cat_conf, coa, orig_cur, orig_amt_cents, rate):
    rat = f"auto-proposed ({cat_conf} confidence)" if coa else None
    if orig_cur:
        conv = f"{orig_cur} {(orig_amt_cents or 0) / 100:.2f}"
        conv += f" @ {rate:.4f} → USD" if rate else " (rate TBD — reviewer confirms)"
        rat = (rat + "; " if rat else "") + f"converted from {conv}"
    return rat


def process_intake(conn, cfg, employee, uid, text, month,
                   slack=None, file_ids=None, source_dm_ts=None) -> dict:
    """Handle a reimbursement-intake message. One receipt → one reimbursement;
    SEVERAL receipts in one message → one reimbursement per receipt (a group), all
    sharing the purpose/category/client the pal gave once. Reads each receipt's
    amount + currency, converts non-USD to USD (flagged for the reviewer), proposes
    the category, and resolves the billable client from the message when it can.
    Returns {reimbursement (anchor), status, confirm_back}."""
    rc = cfg.section("reimbursements")
    first = employee.split()[0]
    files = list(file_ids or [])
    msg_currency = fx.detect_currency(text)

    # Read each attached receipt (amount / merchant / currency). No files -> one slot.
    ings = [_ingest_receipt(cfg, month, employee, _today(), slack, fid) for fid in files]
    first_merchant = next((g["merchant"] for g in ings if g.get("merchant")), None)

    parsed = reimburse_parse.interpret_reimbursement(
        cfg.client, text, cfg.coa_lines, _today(), merchant=first_merchant)
    purpose = parsed.get("business_purpose")
    expense_date = parsed.get("expense_date") or _today()
    coa, cat_conf, cat_options = _propose_category(conn, cfg, first_merchant, parsed)
    offer_shortlist = cat_conf in ("low", "medium") and len(cat_options) >= 2
    billable = _is_billable(cfg, coa)
    # If billable, try to resolve the client straight from the message ("all
    # billable to the Gilead project") — active list + live HubSpot search.
    project = projects.resolve(cfg, month, text)[0] if billable else None

    slots = ings or [{"receipt_status": None, "receipt_link": None,
                      "amount_cents": parsed.get("amount_cents"), "currency": None}]
    ext_base = source_dm_ts or uuid.uuid4().hex[:16]
    group_id = ext_base if len(slots) > 1 else None
    # File to the close of the month the expense was incurred, not the submit month.
    close_month = _close_month_for(expense_date, month)
    # A negative/offset reimbursement (Levi reducing his payout): the receipt shows
    # a positive magnitude, but the message carries the sign — apply it to each slot.
    negative = ((parsed.get("amount_cents") or 0) < 0) or reimburse_parse.looks_negative(text)

    created, last_violations = [], []
    for i, g in enumerate(slots):
        cur = msg_currency or g.get("currency") or "USD"
        slot_amt = g.get("amount_cents")
        if negative and slot_amt:
            slot_amt = -abs(slot_amt)
        pay_cents, orig_cur, orig_amt_cents, rate = _to_usd(slot_amt, cur, expense_date)
        ext = f"reimb-{ext_base}" if i == 0 else f"reimb-{ext_base}-{i + 1}"
        r = {
            "external_id": ext, "client": cfg.client,
            "entity": cfg.raw.get("entity") or "August", "employee": employee,
            "submitter_uid": uid, "kind": "one_off", "expense_date": expense_date,
            "close_month": close_month, "amount_cents": pay_cents or 0, "currency": "USD",
            "business_purpose": purpose, "proposed_coa_line": coa,
            "billable": 1 if billable else 0, "project": project,
            "orig_currency": orig_cur, "orig_amount_cents": orig_amt_cents,
            "group_id": group_id,
            "rationale": _fx_rationale(cat_conf, coa, orig_cur, orig_amt_cents, rate),
            "receipt_status": g.get("receipt_status"), "receipt_link": g.get("receipt_link"),
            "status": "submitted", "source_dm_ts": source_dm_ts,
        }
        rid = ledger.create_reimbursement(conn, r)
        row = ledger.reimbursement_by_id(conn, rid)
        ledger.record_reimbursement_event(
            conn, cfg.client, ext, "intake",
            {"amount_cents": pay_cents, "proposed_coa_line": coa,
             "category_confidence": cat_conf, "orig_currency": orig_cur,
             "orig_amount_cents": orig_amt_cents, "fx_rate": rate, "group_id": group_id},
            actor=employee, source="slack" if slack else "cli")
        if offer_shortlist and i == 0:
            ledger.record_reimbursement_event(conn, cfg.client, ext, _CATEGORY_OPTS,
                                              {"options": cat_options}, actor="system",
                                              source="penny")
        v = reimburse_policy.check(cfg, pay_cents, purpose, coa, expense_date, _today())
        if v:
            ledger.record_reimbursement_event(conn, cfg.client, ext, "policy_flag",
                                              {"violations": v}, actor="system", source="policy")
            last_violations = v
        created.append(row)

    anchor = created[0]
    ext, rid = anchor["external_id"], anchor["id"]

    # --- Single receipt: unchanged tail (missing basics -> ask; else ack/ask-client).
    if len(created) == 1:
        row = anchor
        missing = _missing_fields(rc, row.get("amount_cents"), purpose, coa,
                                  row.get("receipt_status"))
        if missing:
            ledger.set_reimbursement_status(conn, rid, "needs_info")
            row = ledger.reimbursement_by_id(conn, rid)
            cb = reimburse_dm.needs_info(first, row, missing,
                                         trigger=rc.get("trigger", "reimburse"),
                                         followup=True)
            status = "needs_info"
        else:
            status, row, cb = _ack_or_ask_client(
                conn, cfg, ext, rid, first, slack, cat_conf=cat_conf,
                cat_options=cat_options if offer_shortlist else None)
        cb += reimburse_dm.policy_warning(last_violations)
        return {"reimbursement": row, "status": status, "confirm_back": cb}

    # --- Group (several receipts): shared client, per-receipt amounts.
    unreadable = [r for r in created if not r.get("amount_cents")]
    for r in unreadable:                      # a receipt Penny couldn't read an amount from
        ledger.set_reimbursement_status(conn, r["id"], "needs_info")
    need_client = billable and not project
    if need_client:
        for r in created:
            ledger.set_reimbursement_status(conn, r["id"], "needs_info")
        ledger.record_reimbursement_event(conn, cfg.client, ext, "project_prompt", {},
                                          actor="system", source="penny")
    status = "needs_info" if (unreadable or need_client) else "submitted"
    fresh = ledger.reimbursements_in_group(conn, cfg.client, group_id)
    cb = reimburse_dm.group_ack(first, fresh, len(unreadable), need_client)
    if not (unreadable or need_client):
        _notify_approver_group(cfg, fresh, slack)
    cb += reimburse_dm.policy_warning(last_violations)
    return {"reimbursement": anchor, "status": status, "confirm_back": cb}


def process_followup(conn, cfg, employee, uid, text, month,
                     slack=None, file_ids=None, thread_ts=None) -> dict:
    """A reply in an existing reimbursement's thread — NO trigger word needed.
    Penny remembers the charge and fills in whatever new info this message adds
    (category, amount, purpose, and/or a newly-attached receipt), never re-asking
    for what it already has and never making the employee re-upload the receipt.
    Returns {reimbursement, status, confirm_back}, or None if the thread isn't a
    reimbursement (caller falls through to the card-charge path)."""
    ext = f"reimb-{thread_ts}"
    row = ledger.reimbursement_by_external_id(conn, cfg.client, ext)
    if not row:
        return None
    first = employee.split()[0]
    # Only an in-flight reimbursement can be edited by a reply. Once it's approved/
    # exported/paid, don't mutate — just say where it stands.
    if row["status"] not in ("submitted", "needs_info"):
        return {"reimbursement": row, "status": row["status"],
                "confirm_back": reimburse_dm.already(first, row)}

    # Cancel / withdraw: a reply like "cancel" / "nevermind" / "scratch that" in the
    # thread pulls the reimbursement (and its multi-receipt group) so it's never
    # paid — Michael's "I tried to cancel… she doesn't get it".
    if _wants_cancel(text) and not file_ids:
        gid = row.get("group_id")
        rows = (ledger.reimbursements_in_group(conn, cfg.client, gid) if gid else [row])
        n = 0
        for r in rows:
            if r["status"] in ("submitted", "needs_info"):
                ledger.set_reimbursement_status(conn, r["id"], "rejected",
                                                rejected_reason="withdrawn by submitter")
                ledger.record_reimbursement_event(conn, cfg.client, r["external_id"],
                                                  "cancelled", {}, actor=employee,
                                                  source="slack")
                n += 1
        fresh = ledger.reimbursement_by_id(conn, row["id"])
        return {"reimbursement": fresh, "status": "rejected",
                "confirm_back": reimburse_dm.cancelled(first, n)}

    rc = cfg.section("reimbursements")

    # A bare-number reply ("2", "#2") picks from the numbered category shortlist
    # Penny last offered — no re-typing the category name. Only when a pick-list is
    # actually pending and no receipt is attached (a receipt is new info, not a pick).
    choice = disambiguate.parse_choice(text)
    dim, opts = _pending_options(conn, cfg, ext) if choice else (None, None)
    if choice and opts and not file_ids:
        if 1 <= choice <= len(opts):
            picked = opts[choice - 1]
            if dim == "project":
                ledger.update_reimbursement_fields(conn, row["id"], project=picked)
                ledger.record_reimbursement_event(
                    conn, cfg.client, ext, "project_chosen",
                    {"project": picked, "choice": choice}, actor=employee, source="slack")
            else:
                ledger.update_reimbursement_fields(conn, row["id"], proposed_coa_line=picked)
                ledger.record_reimbursement_event(
                    conn, cfg.client, ext, "category_chosen",
                    {"category": picked, "choice": choice}, actor=employee, source="slack")
            fresh = ledger.reimbursement_by_id(conn, row["id"])
            miss = _missing_fields(rc, fresh.get("amount_cents"),
                                   fresh.get("business_purpose"),
                                   fresh.get("proposed_coa_line"),
                                   fresh.get("receipt_status"))
            if miss:
                ledger.set_reimbursement_status(conn, row["id"], "needs_info")
                fresh = ledger.reimbursement_by_id(conn, row["id"])
                cb = reimburse_dm.needs_info(first, fresh, miss,
                                             trigger=rc.get("trigger", "reimburse"),
                                             followup=True)
                return {"reimbursement": fresh, "status": "needs_info", "confirm_back": cb}
            # Basics in — ack, or (if the pick made it billable) ask which client.
            st, fresh, cb = _ack_or_ask_client(conn, cfg, ext, row["id"], first, slack)
            return {"reimbursement": fresh, "status": st, "confirm_back": cb}
        cb = reimburse_dm.bad_choice(first, len(opts))
        return {"reimbursement": row, "status": row["status"], "confirm_back": cb}

    # A receipt sent in the follow-up gets stored now (so it's never lost), its
    # merchant sharpens the category, and a foreign-currency total is converted.
    receipt_status = row.get("receipt_status")
    receipt_link = row.get("receipt_link")
    merchant = None
    receipt_amount = None
    fu_orig_cur = fu_orig_amt = None
    if slack and file_ids:
        g = _ingest_receipt(cfg, month, employee,
                            row.get("expense_date") or _today(), slack, file_ids[0])
        merchant = g.get("merchant")
        if g.get("receipt_status") == "stored":
            receipt_status, receipt_link = g["receipt_status"], g["receipt_link"]
        cur = fx.detect_currency(text) or g.get("currency") or "USD"
        receipt_amount, fu_orig_cur, fu_orig_amt, _rate = _to_usd(
            g.get("amount_cents"), cur, row.get("expense_date") or _today())

    parsed = reimburse_parse.interpret_reimbursement(
        cfg.client, text, cfg.coa_lines, row.get("expense_date") or _today(),
        merchant=merchant)

    # Merge: new value wins if present, else keep what we already had (never null out).
    amount_cents = parsed.get("amount_cents") or receipt_amount or (row.get("amount_cents") or None)
    purpose = parsed.get("business_purpose") or row.get("business_purpose")
    # Honor a corrected date in the reply ("actually it was July 15") and move the
    # reimbursement to that month's close if it changed.
    expense_date = parsed.get("expense_date") or row.get("expense_date") or _today()
    new_close_month = _close_month_for(expense_date, row.get("close_month"))

    # Category: a follow-up only changes it when it adds real signal — the row had
    # none yet, or the reply clearly names/confirms one (high confidence). This stops
    # a bare receipt upload from re-guessing over a category already settled.
    new_coa, new_conf, new_options = _propose_category(conn, cfg, merchant, parsed)
    existing = row.get("proposed_coa_line")
    if existing and new_conf != "high":
        coa, cat_conf, cat_options = existing, "high", []
    else:
        coa, cat_conf, cat_options = (new_coa or existing), new_conf, new_options
    offer_shortlist = (not existing) and cat_conf in ("low", "medium") and len(cat_options) >= 2

    ledger.update_reimbursement_fields(
        conn, row["id"], amount_cents=amount_cents, business_purpose=purpose,
        proposed_coa_line=coa, expense_date=expense_date, close_month=new_close_month,
        orig_currency=fu_orig_cur, orig_amount_cents=fu_orig_amt)
    if receipt_status == "stored" and row.get("receipt_status") != "stored":
        ledger.set_reimbursement_receipt(conn, row["id"], "stored", receipt_link)

    if offer_shortlist:
        ledger.record_reimbursement_event(conn, cfg.client, ext, _CATEGORY_OPTS,
                                          {"options": cat_options}, actor="system",
                                          source="penny")

    # Billable + no client yet -> read a client from this reply (a name resolves it,
    # searching the active list + live HubSpot; an ambiguous client — e.g. several
    # projects for one account — offers a numbered pick).
    if _is_billable(cfg, coa) and not row.get("project"):
        proj, proj_opts = projects.resolve(cfg, month, text)
        if proj:
            ledger.update_reimbursement_fields(conn, row["id"], project=proj)
            ledger.record_reimbursement_event(conn, cfg.client, ext, "project_set",
                                              {"project": proj}, actor=employee,
                                              source="slack")
        elif proj_opts:
            ledger.record_reimbursement_event(conn, cfg.client, ext, _PROJECT_OPTS,
                                              {"options": proj_opts}, actor="system",
                                              source="penny")

    violations = reimburse_policy.check(cfg, amount_cents, purpose, coa,
                                        expense_date, _today())
    if violations:
        ledger.record_reimbursement_event(conn, cfg.client, ext, "policy_flag",
                                          {"violations": violations, "followup": True},
                                          actor="system", source="policy")

    missing = _missing_fields(rc, amount_cents, purpose, coa, receipt_status)
    if missing:
        ledger.set_reimbursement_status(conn, row["id"], "needs_info")
        fresh = ledger.reimbursement_by_id(conn, row["id"])
        cb = reimburse_dm.needs_info(first, fresh, missing,
                                     trigger=rc.get("trigger", "reimburse"),
                                     followup=True)
        status = "needs_info"
    else:
        status, fresh, cb = _ack_or_ask_client(
            conn, cfg, ext, row["id"], first, slack, cat_conf=cat_conf,
            cat_options=cat_options if offer_shortlist else None)
    ledger.record_reimbursement_event(conn, cfg.client, ext, status,
                                      {"followup": True, "proposed_coa_line": coa,
                                       "category_confidence": cat_conf},
                                      actor=employee, source="slack")
    cb += reimburse_dm.policy_warning(violations)
    return {"reimbursement": fresh, "status": status, "confirm_back": cb}


def _notify_approver(cfg, row: dict, slack, low_confidence: bool = False):
    """DM the approver that a reimbursement is waiting (best-effort). When Penny's
    category is a low-confidence guess, flag that so the reviewer double-checks it —
    the uncertainty goes to the approver, never back to the submitter."""
    if not slack:
        return
    approver = approver_for(cfg, row["employee"])
    uid = _uid_for(cfg, approver)
    if not uid:
        return
    try:
        slack.send_dm(uid, reimburse_dm.approval_request(
            row, _dashboard_url(), low_confidence=low_confidence))
    except Exception:
        pass


def _notify_approver_group(cfg, rows: list, slack):
    """One DM to the approver for a multi-receipt submission (best-effort)."""
    if not slack or not rows:
        return
    approver = approver_for(cfg, rows[0]["employee"])
    uid = _uid_for(cfg, approver)
    if not uid:
        return
    try:
        slack.send_dm(uid, reimburse_dm.approval_request_group(rows, _dashboard_url()))
    except Exception:
        pass


def approve(conn, cfg, reimb_id: int, approver: str, slack=None) -> dict:
    """Approve a reimbursement. Enforces no self-approval. Returns the fresh row."""
    row = ledger.reimbursement_by_id(conn, reimb_id)
    if not row:
        raise ValueError(f"reimbursement {reimb_id} not found")
    if approver == row["employee"]:
        raise PermissionError(
            f"{approver} cannot approve their own reimbursement — "
            f"needs {approver_for(cfg, row['employee'])}")
    ledger.set_reimbursement_status(conn, reimb_id, "approved",
                                    approver=approver, approved_at=ledger.now())
    ledger.record_reimbursement_event(conn, cfg.client, row["external_id"],
                                      "approved", {"approver": approver},
                                      actor=approver, source="dashboard")
    fresh = ledger.reimbursement_by_id(conn, reimb_id)
    # NOTE: QBO booking is NOT per-approval — reimbursements are grouped into one
    # Bill per employee per month (dated month-end) at the monthly book step
    # (book_month, run at payout/export). See book_month().
    if slack:
        uid = fresh.get("submitter_uid") or _uid_for(cfg, fresh["employee"])
        if uid:
            try:
                slack.send_dm(uid, reimburse_dm.approved(fresh["employee"].split()[0], fresh))
            except Exception:
                pass
    return fresh


def reject(conn, cfg, reimb_id: int, reason: str, actor: str, slack=None) -> dict:
    row = ledger.reimbursement_by_id(conn, reimb_id)
    if not row:
        raise ValueError(f"reimbursement {reimb_id} not found")
    ledger.set_reimbursement_status(conn, reimb_id, "rejected",
                                    rejected_reason=reason)
    ledger.record_reimbursement_event(conn, cfg.client, row["external_id"],
                                      "rejected", {"reason": reason},
                                      actor=actor, source="dashboard")
    fresh = ledger.reimbursement_by_id(conn, reimb_id)
    if slack:
        uid = fresh.get("submitter_uid") or _uid_for(cfg, fresh["employee"])
        if uid:
            try:
                slack.send_dm(uid, reimburse_dm.rejected(
                    fresh["employee"].split()[0], fresh, reason))
            except Exception:
                pass
    return fresh


def export_payout(conn, cfg, reimb_ids, rail, pay_date: str = None) -> dict:
    """Run the payment rail over the given APPROVED reimbursements and mark them
    exported. `pay_date` is the payout date for rails that need one. Returns
    {result, reimbursements}."""
    rows = [ledger.reimbursement_by_id(conn, i) for i in reimb_ids]
    rows = [r for r in rows if r and r["status"] == "approved"]
    if not rows:
        return {"result": {"status": "failed", "paste_text": "", "artifact": None,
                           "ref": None, "rail": getattr(rail, "name", "")},
                "reimbursements": []}
    # Book the month's grouped QBO Bills now (one per employee, dated month-end) —
    # best-effort, cloud-only. Done before marking exported so the rows are still
    # eligible; idempotent so a re-export never double-bills.
    if _qbo_enabled(cfg):
        try:
            book_month(conn, cfg, rows[0]["close_month"], post=True)
        except Exception:
            pass
    result = rail.pay_batch(rows, pay_date=pay_date)
    if result.get("status") in ("exported", "paid"):
        new_status = "paid" if result["status"] == "paid" else "exported"
        for r in rows:
            ledger.set_reimbursement_status(
                conn, r["id"], new_status, payment_rail=result.get("rail"),
                payout_ref=result.get("ref"), exported_at=ledger.now())
            ledger.record_reimbursement_event(
                conn, cfg.client, r["external_id"], new_status,
                {"payout_ref": result.get("ref"), "artifact": result.get("artifact")},
                actor="system", source="rail")
    return {"result": result,
            "reimbursements": [ledger.reimbursement_by_id(conn, r["id"]) for r in rows]}


def mark_paid(conn, cfg, reimb_ids, actor: str = None, slack=None) -> list:
    """Flag exported reimbursements as paid once the human completed the Justworks
    payout. Returns the fresh rows."""
    out, bill_ids = [], set()
    for i in reimb_ids:
        row = ledger.reimbursement_by_id(conn, i)
        if not row:
            continue
        if row["status"] == "paid":       # idempotent — don't double-process
            out.append(row)
            continue
        ledger.set_reimbursement_status(conn, i, "paid", paid_at=ledger.now())
        ledger.record_reimbursement_event(conn, cfg.client, row["external_id"],
                                          "paid", {}, actor=actor, source="dashboard")
        if row.get("qbo_bill_id"):
            bill_ids.add(row["qbo_bill_id"])
        fresh = ledger.reimbursement_by_id(conn, i)
        out.append(fresh)
        if slack:
            uid = fresh.get("submitter_uid") or _uid_for(cfg, fresh["employee"])
            if uid:
                try:
                    slack.send_dm(uid, reimburse_dm.paid(
                        fresh["employee"].split()[0], fresh))
                except Exception:
                    pass
    # Clear each grouped Bill ONCE (one BillPayment per Bill, DR A/P / CR 1345 — the
    # Justworks payout also posts to 1345, netting it). Best-effort, cloud-only,
    # guarded against double-payment (skips a Bill already at $0).
    if _qbo_enabled(cfg):
        for bid in bill_ids:
            try:
                _pay_bill(conn, cfg, bid, post=True)
            except Exception:
                pass
    return out


def bill_reimbursement(conn, cfg, reimb_id: int, post: bool = False) -> dict:
    """Create (or preview) the QBO Bill for a reimbursement: expense line → the
    category GL, payable → the clearing account (config qbo.clearing_account_id),
    vendor → the employee's mapped QBO vendor. Returns resolved ids + the payload
    (+ bill_id when post=True), or {ok: False, problems:[...]} on config gaps.
    Best-effort — the caller decides whether to surface/raise."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    r = ledger.reimbursement_by_id(conn, reimb_id)
    if not r:
        return {"ok": False, "problems": [f"no reimbursement #{reimb_id}"]}
    qc = cfg.section("reimbursements").get("qbo", {}) or {}
    ap_id = qc.get("ap_account_id")        # normal A/P (payable side); line uses the category GL
    vend_name = (qc.get("vendors", {}) or {}).get(r["employee"])
    gl = qbo_writer.load_mapping(cfg.client).get(r.get("proposed_coa_line"))
    problems = []
    if not r.get("proposed_coa_line"):
        problems.append("reimbursement has no category yet")
    elif not gl:
        problems.append(f"no GL mapping for category '{r['proposed_coa_line']}'")
    if not vend_name:
        problems.append(f"no QBO vendor mapped for '{r['employee']}'")
    if not ap_id:
        problems.append("no qbo.ap_account_id configured")
    memo = (f"Reimbursement: {r.get('business_purpose')}"
            if r.get("business_purpose") else "Reimbursement")
    vendor_id = None
    if not problems:
        q = QBOFeed(realm_id=""); q._refresh_access_token()
        vendor_id = qbo_writer.resolve_vendor_by_name(q, vend_name)
        if not vendor_id:
            problems.append(f"QBO vendor '{vend_name}' not found")
    out = {"ok": not problems, "problems": problems, "reimb": r,
           "vendor_name": vend_name, "vendor_id": vendor_id, "gl_id": gl,
           "ap_account_id": ap_id, "payload": None}
    if problems:
        return out
    out["payload"] = qbo_writer.bill_body(vendor_id, gl, r["amount_cents"],
                                          r["expense_date"], memo=memo, ap_account_id=ap_id)
    if not post:
        return out
    bill = qbo_writer.create_bill(q, vendor_id, gl, r["amount_cents"],
                                  r["expense_date"], memo=memo, ap_account_id=ap_id)
    bill_id = bill.get("Id")
    ledger.set_reimbursement_status(conn, reimb_id, r["status"],
                                    qbo_vendor_id=vendor_id, qbo_bill_id=bill_id)
    ledger.record_reimbursement_event(conn, cfg.client, r["external_id"], "qbo_bill",
                                      {"bill_id": bill_id, "gl": gl, "ap": ap_id},
                                      actor="system", source="qbo")
    out["bill_id"] = bill_id
    return out


def pay_bill_reimbursement(conn, cfg, reimb_id: int, post: bool = False) -> dict:
    """Mark a reimbursement's Bill paid, crediting the 1345 reimbursement clearing
    account (Other Current Asset) instead of a bank — the Justworks payroll posting
    to 1345 then offsets it. NOTE: QBO may reject a BillPayment whose funding account
    isn't Bank/Credit Card type; this is the step that verifies that."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    r = ledger.reimbursement_by_id(conn, reimb_id)
    if not r:
        return {"ok": False, "problems": [f"no reimbursement #{reimb_id}"]}
    qc = cfg.section("reimbursements").get("qbo", {}) or {}
    clearing = qc.get("reimbursement_clearing_account_id")
    ap_id = qc.get("ap_account_id")
    problems = []
    if not r.get("qbo_bill_id"):
        problems.append("no qbo_bill_id on this reimbursement — create the Bill first")
    if not clearing:
        problems.append("no qbo.reimbursement_clearing_account_id configured")
    if problems:
        return {"ok": False, "problems": problems, "reimb": r}
    payload = qbo_writer.billpayment_body(r["qbo_bill_id"], r.get("qbo_vendor_id"),
                                          r["amount_cents"], r["expense_date"], clearing, ap_id)
    out = {"ok": True, "reimb": r, "clearing_account_id": clearing, "payload": payload}
    if not post:
        return out
    q = QBOFeed(realm_id=""); q._refresh_access_token()
    # Guard against a duplicate payment: if the Bill is already fully paid (Balance 0),
    # don't create another BillPayment.
    try:
        b = q.query(f"SELECT Id, Balance FROM Bill WHERE Id = '{r['qbo_bill_id']}'")
        if b and float(b[0].get("Balance") or 0) <= 0:
            out["already_paid"] = True
            return out
    except Exception:
        pass
    bp = qbo_writer.create_bill_payment(q, r["qbo_bill_id"], r.get("qbo_vendor_id"),
                                        r["amount_cents"], r["expense_date"], clearing, ap_id)
    out["billpayment_id"] = bp.get("Id")
    ledger.record_reimbursement_event(conn, cfg.client, r["external_id"],
                                      "qbo_billpayment",
                                      {"billpayment_id": bp.get("Id"), "clearing": clearing},
                                      actor="system", source="qbo")
    return out


def book_month(conn, cfg, month: str, post: bool = False, bill_date: str = None) -> dict:
    """Group a month's reimbursements into ONE QBO Bill per employee (Natalie's
    structure): a multi-line Bill (one line per reimbursement on its category GL),
    payable to normal A/P, dated the last day of the month (override via bill_date).
    Only bills reimbursements that are approved/exported and not yet on a Bill, so
    re-running never duplicates. Dry-run (post=False) returns the plan without any
    QBO calls; post=True creates the Bills and stamps qbo_bill_id on each line's row."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    qc = cfg.section("reimbursements").get("qbo", {}) or {}
    ap_id = qc.get("ap_account_id")
    vendors = qc.get("vendors", {}) or {}
    mapping = qbo_writer.load_mapping(cfg.client)
    txn_date = bill_date or _month_end(month)

    eligible = [r for r in ledger.reimbursements_for(conn, cfg.client, month)
                if r["status"] in ("approved", "exported") and not r.get("qbo_bill_id")]
    by_emp = defaultdict(list)
    for r in eligible:
        by_emp[r["employee"]].append(r)

    q = None
    bills = []
    for emp, rows in sorted(by_emp.items()):
        vend_name = vendors.get(emp)
        lines, problems = [], []
        for r in rows:
            gl = mapping.get(r.get("proposed_coa_line"))
            if not r.get("proposed_coa_line") or not gl:
                problems.append(f"#{r['id']} has no mapped category — skipped")
                continue
            lines.append({"gl_id": gl, "amount_cents": r["amount_cents"],
                          "description": r.get("business_purpose") or "Reimbursement",
                          "reimb_id": r["id"], "ext": r["external_id"]})
        if not vend_name:
            problems.append(f"no QBO vendor mapped for {emp}")
        item = {"employee": emp, "vendor_name": vend_name, "txn_date": txn_date,
                "n_lines": len(lines), "total_cents": sum(l["amount_cents"] for l in lines),
                "problems": problems}
        if post and lines and vend_name:
            if q is None:
                q = QBOFeed(realm_id=""); q._refresh_access_token()
            vendor_id = qbo_writer.resolve_vendor_by_name(q, vend_name)
            if not vendor_id:
                item["problems"].append(f"QBO vendor '{vend_name}' not found")
            else:
                bill = qbo_writer.create_bill_lines(
                    q, vendor_id, lines, txn_date,
                    memo=f"{emp} reimbursements — {month}", ap_account_id=ap_id)
                bid = bill.get("Id")
                item["bill_id"] = bid
                for l in lines:
                    cur = ledger.reimbursement_by_id(conn, l["reimb_id"])
                    ledger.set_reimbursement_status(conn, l["reimb_id"], cur["status"],
                                                    qbo_vendor_id=vendor_id, qbo_bill_id=bid)
                    ledger.record_reimbursement_event(
                        conn, cfg.client, l["ext"], "qbo_bill",
                        {"bill_id": bid, "grouped": True, "txn_date": txn_date},
                        actor="system", source="qbo")
        bills.append(item)
    return {"month": month, "txn_date": txn_date, "bills": bills,
            "n_employees": len(bills)}


def _pay_bill(conn, cfg, bill_id: str, post: bool = True) -> dict:
    """Pay a (possibly grouped) Bill in full, crediting 1345 — one BillPayment per
    Bill. Skips if the Bill is already at $0 balance (no double-payment)."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    qc = cfg.section("reimbursements").get("qbo", {}) or {}
    clearing = qc.get("reimbursement_clearing_account_id")
    ap_id = qc.get("ap_account_id")
    if not (bill_id and clearing):
        return {"ok": False}
    q = QBOFeed(realm_id=""); q._refresh_access_token()
    b = q.query(f"SELECT Id, Balance, VendorRef FROM Bill WHERE Id = '{bill_id}'")
    if not b:
        return {"ok": False}
    bal = float(b[0].get("Balance") or 0)
    if bal <= 0:
        return {"ok": True, "already_paid": True}
    vendor_id = (b[0].get("VendorRef") or {}).get("value")
    if not post:
        return {"ok": True, "would_pay_cents": round(bal * 100)}
    bp = qbo_writer.create_bill_payment(q, bill_id, vendor_id, round(bal * 100),
                                        ledger.now()[:10], clearing, ap_id)
    return {"ok": True, "billpayment_id": bp.get("Id")}


def generate_stipends(conn, cfg, month: str) -> list:
    """Create this month's recurring stipend reimbursements (idempotent per
    employee+stipend+month). Returns the rows created/existing. Stipends go
    through the same approval queue. NOTE the accountable-plan tax caveat: a fixed
    allowance without substantiation may be taxable — receipt_required per config."""
    rc = cfg.section("reimbursements")
    entity = cfg.raw.get("entity") or "August"
    out = []
    for st in rc.get("stipends", []) or []:
        emp = st.get("employee")
        name = st.get("name", "Stipend")
        ext = f"stipend-{_slug(emp)}-{_slug(name)}-{month}"
        needs_receipt = st.get("receipt_required", False)
        r = {
            "external_id": ext, "client": cfg.client, "entity": entity,
            "employee": emp, "kind": "stipend", "expense_date": f"{month}-01",
            "close_month": month, "amount_cents": st.get("amount_cents", 0),
            "currency": "USD", "business_purpose": name,
            "proposed_coa_line": st.get("coa_line"),
            "receipt_status": None if needs_receipt else "not_required",
            "status": "needs_info" if needs_receipt else "submitted",
        }
        rid = ledger.create_reimbursement(conn, r)
        row = ledger.reimbursement_by_id(conn, rid)
        ledger.record_reimbursement_event(conn, cfg.client, ext, row["status"],
                                          {"stipend": name}, actor="system",
                                          source="stipend")
        out.append(row)
    return out
