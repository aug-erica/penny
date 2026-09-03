"""Process a pal's reply end-to-end — shared by the manual CLI and Penny's live
listener. Applies billable/project decisions and category corrections to the
ledger, stores any attached receipt files, and returns a confirm-back message.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from . import dm_assemble, ledger, receipt_read, receipt_store, receipts, reply_parse
from .normalize import normalize_merchant

# Amounts with or without cents: $1,350 / $1,350.00 / 266.83
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")

# A bare "not billable" statement (no charge named). "billable" alone is NOT here
# — a positive billable needs a project, so we let the interpreter handle it.
# Letter-only boundaries so Slack's italic markup ("_not billable_") still matches
# but "cannot billable" doesn't.
_NOT_BILLABLE = re.compile(r"(?<![a-z])no[nt][\s\-_]*billable(?![a-z])", re.I)

# "That Southwest charge was refunded" — a refund/reversal the pal knows about
# (often before the credit posts). The named charge is excluded so its receipt /
# category asks stop; when the credit lands, refunds.find_original still links it.
_REFUNDED = re.compile(
    r"(?<![a-z])(refund(?:ed|s)?|revers(?:ed|al)|charge[\s-]?back|charged back|"
    r"credited back)(?![a-z])", re.I)
_NOT_REFUNDED = re.compile(
    r"(?<![a-z])(not|no|never|wasn'?t|isn'?t|hasn'?t|haven'?t|didn'?t|won'?t)\s+"
    r"(?:been\s+|get\s+|got\s+|a\s+)?refund", re.I)


_COMMON_WORDS = {"every", "other", "about", "there", "their", "which", "these", "those",
                 "would", "should", "thank", "thanks", "please", "charge", "charges",
                 "trial", "again", "after", "before", "still", "think", "right"}


def _to_cents(s: str) -> int:
    return round(float(s.replace(",", "")) * 100)


def _amounts_in_file(path: Path) -> set:
    """Dollar amounts found in a receipt PDF's text, as cents. Lets Penny link a
    receipt to its charge even when the pal didn't type the amount in the message
    (a hotel folio / e-ticket carries its own total). Non-PDF/unreadable -> {}."""
    try:
        import pdfplumber
        cents = set()
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:4]:
                for a in _AMOUNT.findall(page.extract_text() or ""):
                    cents.add(_to_cents(a))
        return cents
    except Exception:
        return set()


def _recent_months(month):
    """The active month + the previous one, so a reply about a charge near the
    month boundary (e.g. a June charge while the active close is July) resolves."""
    y, m = int(month[:4]), int(month[5:7])
    prev = (y, m - 1) if m > 1 else (y - 1, 12)
    return [month, f"{prev[0]:04d}-{prev[1]:02d}"]


def _pal_charges(conn, client, month, cardholder):
    """The pal's live card lines: positive charges plus any unmatched credits
    (negative, informational — never generate asks, but can be recategorized)."""
    out = []
    for mo in _recent_months(month):
        out += [l for l in ledger.lines_for_month(conn, client, mo)
                if (l.get("cardholder") or "").lower().find(cardholder.lower()) >= 0
                and l["status"] != "excluded" and l["amount_cents"] != 0]
    return out


def _refund_targets(text: str, charges: list):
    """Which charge(s) a 'refunded' reply points at: by an amount in the text,
    else by a merchant token (5+ chars) appearing in the text. Returns
    (targets, options) — options non-empty when several merchants could fit."""
    pos = [c for c in charges if c["amount_cents"] > 0]
    amounts = {_to_cents(a) for a in _AMOUNT.findall(text)}
    if amounts:
        hits = [c for c in pos if c["amount_cents"] in amounts]
        if hits:
            return hits, []
    words = {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 5}
    hits = []
    for c in pos:
        # Merchant tokens that are also everyday words ("EVERY EVER* TRIAL OVER")
        # would match almost any sentence — skip them.
        toks = {t for t in re.split(r"[^a-z0-9]+", (c.get("merchant_norm") or "").lower())
                if len(t) >= 5 and t not in _COMMON_WORDS}
        if any(t in w or w in t for t in toks for w in words):
            hits.append(c)
    if len(hits) == 1:
        return hits, []
    return [], hits            # several could fit -> ask which; none -> nothing


def process_pal_reply(conn, cfg, cardholder, text, month,
                      slack=None, file_ids=None) -> dict:
    """Apply a reply to the ledger. Returns {decisions, recats, rules,
    receipts, confirm_back}. `slack` is a PennySlack for downloading files."""
    charges = _pal_charges(conn, cfg.client, month, cardholder)
    by_ext = {c["external_id"]: c for c in charges}
    # Candidate projects = this month's active list PLUS live HubSpot Closed-Won
    # matches for what the pal wrote — so a long-closed or out-of-window project
    # (e.g. Waymo) can still be tagged, not just the current month's deals.
    from . import projects as project_lookup
    projects = project_lookup.candidates(cfg, month, text)
    result = {"decisions": 0, "recats": 0, "rules": 0, "receipts": 0}
    touched = set()   # external_ids changed by THIS reply (scopes the confirm-back)
    # Charges the pal NAMED by amount ("UNITED …$134.38 / UNITED …$457.78 … billable
    # to X"): a billing call in that message applies to THESE, not to everything
    # still awaiting a project.
    amounts_in_text = {_to_cents(a) for a in _AMOUNT.findall(text)}
    named = [c for c in charges if c["amount_cents"] > 0 and c["amount_cents"] in amounts_in_text]

    # 1. billable / project
    billable_push = []   # (line, project) marked billable this reply on a qbo-* line
    unbill_push = []     # qbo-* external_ids set NOT billable this reply -> clear in QBO
    decided_billing = set()   # external_ids the interpreter made a billing call on
    for d in reply_parse.interpret_reply(cfg.client, text, charges, projects):
        line = by_ext.get(d["external_id"])
        if line:
            b = 1 if d["billable"] else 0
            ledger.set_billable_project(conn, line["id"], b, d["project"])
            ledger.record_decision(conn, cfg.client, month, d["external_id"],
                                   "billable_project",
                                   {"billable": b, "project": d["project"]},
                                   decided_by=cardholder, source="slack")
            touched.add(d["external_id"])
            decided_billing.add(d["external_id"])
            result["decisions"] += 1
            if b == 1 and d.get("project") and (line.get("external_id") or "").startswith("qbo-"):
                billable_push.append((line, d["project"]))
            elif b == 0 and (line.get("external_id") or "").startswith("qbo-"):
                unbill_push.append(line["external_id"])

    # Fallback: a bare "not billable" that names no charge applies to the charges
    # Penny is awaiting a billing call on — the billable-candidate charges still
    # undecided. Without this, "not billable" (with ~30 charges, no merchant/amount)
    # can't be tied to a charge, so the interpreter returns nothing and the pal gets
    # "I didn't catch a change" on repeat. Only fires when the interpreter caught
    # nothing specific (so "the hotel is not billable" stays precise).
    if _NOT_BILLABLE.search(text) and not decided_billing:
        awaiting = named or [c for c in charges
                             if c["amount_cents"] > 0 and c.get("billable") is None
                             and c.get("proposed_coa_line") in dm_assemble.BILLABLE_CANDIDATE]
        for c in awaiting:
            ledger.set_billable_project(conn, c["id"], 0, None)
            ledger.record_decision(conn, cfg.client, month, c["external_id"],
                                   "billable_project", {"billable": 0, "project": None},
                                   decided_by=cardholder, source="slack")
            touched.add(c["external_id"])
            result["decisions"] += 1
            if (c.get("external_id") or "").startswith("qbo-"):
                unbill_push.append(c["external_id"])

    # Positive collective fallback: the pal named a PROJECT but tied it to no
    # specific charge ("both of these are to PPFA…"), answering Penny's "which
    # project?" about the charges it just listed. Apply it to the ones awaiting a
    # project when the interpreter caught nothing specific — mirrors the
    # not-billable fallback above. Ambiguous project -> ask (project_unresolved).
    result["project_unresolved"] = []
    if not decided_billing and not _NOT_BILLABLE.search(text):
        awaiting_proj = named or [
            c for c in charges if c["amount_cents"] > 0
            if (c.get("billable") == 1 and not c.get("project"))
            or (c.get("billable") is None
                and c.get("proposed_coa_line") in dm_assemble.BILLABLE_CANDIDATE)]
        if awaiting_proj:
            proj, proj_opts = project_lookup.resolve(cfg, month, text)
            if proj:
                for c in awaiting_proj:
                    ledger.set_billable_project(conn, c["id"], 1, proj)
                    ledger.record_decision(conn, cfg.client, month, c["external_id"],
                                           "billable_project",
                                           {"billable": 1, "project": proj},
                                           decided_by=cardholder, source="slack")
                    touched.add(c["external_id"])
                    decided_billing.add(c["external_id"])
                    result["decisions"] += 1
                    if (c.get("external_id") or "").startswith("qbo-"):
                        billable_push.append((c, proj))
            elif proj_opts:
                result["project_unresolved"].append(
                    {"charges": [c["external_id"] for c in awaiting_proj],
                     "options": proj_opts})

    # Enforce "billable ⇒ customer" (Natalie/Skyfin): push BillableStatus+Customer
    # to QBO now (not only when an invoice note arrives). If we can't confidently
    # match a client, DON'T write a customer-less billable — collect it so Penny
    # asks which client in the confirm-back.
    result["customer_unresolved"] = []
    if billable_push:
        from ..adapters.books_out import qbo_writer
        from ..adapters.card_feed.qbo_feed import QBOFeed
        q, custs = None, []
        try:
            q = QBOFeed(realm_id=""); q._refresh_access_token()
            custs = qbo_writer.all_customers(q)
        except Exception:
            q = None
        for line, project in billable_push:
            cust = qbo_writer.resolve_customer(q, project, customers=custs) if q else None
            if cust:
                try:
                    _rewrite_qbo_billable(cfg, line["external_id"], project, customer=cust, q=q)
                except Exception:
                    pass
                # Keep the ledger/dashboard in step with QBO: a charge billable to a
                # client moves to Billable Expense (Natalie's rule). Deliberately NOT
                # a flywheel rule — billability is per-charge, not a merchant fact.
                ledger.set_proposal(conn, line["id"], "Billable Expense", "reviewer",
                                    "high", "billable to client -> Billable Expense")
            else:
                cands = qbo_writer.customer_candidates(q, project, 3, customers=custs) if q else []
                result["customer_unresolved"].append(
                    {"line": line, "project": project, "candidates": [nm for _, nm, _ in cands]})

    # 1b. "that was refunded": exclude the named charge (stops its receipt/category
    #     asks); the credit, when it posts, links to it via refunds.find_original.
    result["refunded"] = []
    result["refund_unresolved"] = []
    if _REFUNDED.search(text) and not _NOT_REFUNDED.search(text):
        targets, options = _refund_targets(text, charges)
        for c in targets:
            ledger.set_status(conn, c["id"], "excluded")
            base = (c.get("rationale") or "").strip()
            note = f"refunded per {cardholder.split()[0]}"
            ledger.set_rationale(conn, c["id"], f"{base} — {note}" if base else note)
            ledger.record_decision(conn, cfg.client, c["close_month"], c["external_id"],
                                   "status", {"status": "excluded", "reason": "refunded"},
                                   decided_by=cardholder, source="slack")
            touched.add(c["external_id"])
            result["refunded"].append(c)
        if not targets and options:
            result["refund_unresolved"] = options

    # 2. category corrections -> ledger + rules; non-COA names -> ask for clarity
    new_rules = {}
    clarifications = []
    recatted = []   # (line, new_category) applied — may need a QBO re-write
    for r in reply_parse.interpret_recategorizations(cfg.client, text, charges, cfg.coa_lines):
        line = by_ext.get(r["external_id"])
        if not line:
            continue
        if r["valid"]:
            rationale = f"recategorized by {cardholder.split()[0]}: {r['why']}".strip()
            ledger.set_proposal(conn, line["id"], r["new_category"], "reviewer", "high",
                                rationale, status="flagged")
            ledger.record_decision(conn, cfg.client, month, r["external_id"], "category",
                                   {"coa_line": r["new_category"],
                                    "rationale": rationale, "status": "flagged"},
                                   decided_by=cardholder, source="slack")
            new_rules[normalize_merchant(line["merchant_raw"])] = r["new_category"]
            recatted.append((line, r["new_category"]))
            touched.add(r["external_id"])
            result["recats"] += 1
        else:
            clarifications.append(r)   # attempted category isn't in the COA
    # Flywheel: persist each correction as a durable rule in Postgres.
    for mn, coa in new_rules.items():
        ledger.upsert_learned_rule(conn, cfg.client, mn, coa, "reviewer")
    result["rules"] = len(new_rules)
    result["clarify"] = len(clarifications)
    # Continuous close: if a corrected charge is already booked in QBO (qbo-<id>),
    # push the new category to the books too. Best-effort — never break the reply.
    for line, coa in recatted:
        if (line.get("external_id") or "").startswith("qbo-"):
            try:
                _rewrite_qbo(cfg, line["external_id"], coa)
            except Exception:
                pass
    # Not-billable this reply: clear the stale Billable flag + customer in QBO so the
    # charge drops off the client/T&E report (best-effort — never break the reply).
    for ext in dict.fromkeys(unbill_push):
        try:
            _unbill_qbo(cfg, ext)
        except Exception:
            pass

    # 3. receipts — download + store; link to a charge by amount when possible
    result["filed"] = []          # (merchant, amount_cents) linked this reply
    result["unlinked"] = 0
    if slack and file_ids:
        for fid in file_ids:
            tmp = Path(tempfile.mktemp())
            try:
                slack.download_file(fid, tmp)
            except Exception:
                tmp.unlink(missing_ok=True)
                continue
            fresh = _pal_charges(conn, cfg.client, month, cardholder)
            needed = [c for c in receipts.receipt_needed(fresh)
                      if c.get("receipt_status") != "stored"]
            # Match a receipt to its charge, best signal first:
            #  1) amount typed in the message,
            #  2) amount read from the file (Claude for any image/PDF, else text),
            #  3) merchant read from the file (unique match among still-needed),
            #  4) sole outstanding need.
            info = receipt_read.read_receipt(tmp)
            amounts = amounts_in_text | _amounts_in_file(tmp)
            if info.get("amount_cents"):
                amounts.add(info["amount_cents"])
            cand = [c for c in needed if c["amount_cents"] in amounts]
            # One file, several charges NAMED by amount in the message (an airline
            # e-ticket covering two tickets, "receipt for $134.38 and $457.78"):
            # the file documents all of them — link every one, not just the first.
            multi = ([c for c in needed if c["amount_cents"] in amounts_in_text]
                     if len(file_ids) == 1 else [])
            link_all = len(multi) > 1
            if link_all:
                cand = multi
            if not cand and info.get("merchant"):
                from rapidfuzz import fuzz
                m = info["merchant"].lower()
                # Token match too: an airline receipt reads as "United Airlines"
                # but the card descriptor is "UNITED XXXX5570" — a fuzzy ratio on
                # the whole strings misses it, but the shared token "united" doesn't.
                mtoks = [t for t in re.split(r"[^a-z0-9]+", m) if len(t) >= 4]

                def _merch_match(c):
                    cm = dm_assemble._merchant(c["merchant_raw"]).lower()
                    return (fuzz.partial_ratio(m, cm) >= 80
                            or any(t in cm for t in mtoks))

                mm = [c for c in needed if _merch_match(c)]
                if len(mm) == 1:
                    cand = mm
                elif len(mm) > 1:
                    # Airline/hotel receipts often show a grand total spanning
                    # several card charges (fare + seat/bag fees). Prefer an exact
                    # amount hit; otherwise the largest same-merchant charge (the
                    # primary one the receipt documents).
                    exact = [c for c in mm if c["amount_cents"] in amounts]
                    cand = exact or [max(mm, key=lambda c: c["amount_cents"])]
            targets = (cand if link_all
                       else [cand[0]] if cand else [needed[0]] if len(needed) == 1 else [])
            for target in targets:
                dest = receipt_store.store_file(cfg, month, cardholder, target, tmp)
                ledger.set_receipt_status(conn, target["id"], "stored", str(dest))
                ledger.record_decision(conn, cfg.client, month, target["external_id"],
                                       "receipt", {"status": "stored", "link": str(dest)},
                                       decided_by=cardholder, source="slack")
                result["filed"].append((target["merchant_raw"], target["amount_cents"]))
                touched.add(target["external_id"])
                # Also attach the receipt to the QBO expense itself, so it shows on
                # the transaction (Natalie's ask — no more digging in Drive).
                if (target.get("external_id") or "").startswith("qbo-"):
                    try:
                        _attach_receipt_qbo(cfg, target["external_id"], tmp, target)
                    except Exception:
                        pass
            if not targets:
                fake = {"merchant_raw": "receipt-unmatched", "amount_cents": 0, "txn_date": month}
                receipt_store.store_file(cfg, month, cardholder, fake, tmp)
                result["unlinked"] += 1
            tmp.unlink(missing_ok=True)
            result["receipts"] += 1

    # 4. billable invoice notes: attach any one-line description the pal gave for
    #    billable charges awaiting one (Penny asks for these in the confirm-back).
    result["notes"] = 0
    awaiting = [c for c in _pal_charges(conn, cfg.client, month, cardholder)
                if c["amount_cents"] > 0 and c.get("billable") == 1 and c.get("project")
                and not c.get("billable_note")]
    if awaiting:
        awaiting_by_ext = {c["external_id"]: c for c in awaiting}
        for n in reply_parse.interpret_billable_notes(cfg.client, text, awaiting):
            c = awaiting_by_ext.get(n["external_id"])
            if not c:
                continue
            ledger.set_billable_note(conn, c["id"], n["note"])
            touched.add(c["external_id"])
            result["notes"] += 1
            if (c.get("external_id") or "").startswith("qbo-"):
                try:
                    _rewrite_qbo_billable(cfg, c["external_id"], c.get("project"), note=n["note"])
                except Exception:
                    pass

    fresh = _pal_charges(conn, cfg.client, month, cardholder)
    bot = cfg.raw.get("bot", {}).get("name", "the expense bot")
    cb = dm_assemble.confirm_back(cardholder.split()[0], fresh, bot, touched=touched,
                                  refunded=result["refunded"])
    if result["refund_unresolved"]:
        qs = ["\n↩️ *Which one was refunded?* A few could fit — reply with the amount:"]
        qs += [f"   • {dm_assemble._merchant(c['merchant_raw'])} "
               f"{dm_assemble._money(c['amount_cents'])} ({c['txn_date'][5:]})"
               for c in result["refund_unresolved"][:6]]
        cb = cb + "\n" + "\n".join(qs)
    if clarifications:
        qs = ["\n❓ *A couple didn't match our chart of accounts — which should these be?*"]
        for c in clarifications:
            opts = " / ".join(c.get("suggestions", [])) or "tell me the category"
            qs.append(f"   • {dm_assemble._merchant(c['merchant'])}: you said "
                      f"\"{c['new_category']}\" — did you mean {opts}?")
        cb = cb + "\n" + "\n".join(qs)
    # Billable but no client matched — ask, rather than book a customer-less billable.
    if result.get("customer_unresolved"):
        qs = ["\n🧾 *Which client should I bill these to?* I couldn't match one "
              "automatically, so I've held off marking them billable:"]
        for u in result["customer_unresolved"]:
            merch = dm_assemble._merchant(u["line"]["merchant_raw"])
            guess = (" — closest matches: " + ", ".join(u["candidates"][:3])) if u["candidates"] else ""
            qs.append(f"   • {merch} (you said \"{u['project']}\"){guess}")
        cb = cb + "\n" + "\n".join(qs)
    # Named a project we couldn't pin to one deal (several close) — ask which.
    if result.get("project_unresolved"):
        opts = result["project_unresolved"][0].get("options", [])
        qs = ["\n🧾 *Which project should I bill these to?* A few could fit — "
              "reply with the exact name:"]
        qs += [f"   • {o}" for o in opts[:5]]
        cb = cb + "\n" + "\n".join(qs)
    # Pal asked for the full review workbook -> reply with the Drive link (single
    # source of truth; no file is sent, so no new Slack scope needed).
    result["workbook_link"] = 0
    if reply_parse.wants_workbook(text):
        urls = cfg.raw.get("bot", {}).get("review_workbook_url", {}) or {}
        url = urls.get(month) if isinstance(urls, dict) else urls
        if url:
            cb = cb + (
                "\n\n📗 *Here's the full review workbook* — it's our single source of "
                "truth. Edit any category in the *Proposed COA* dropdown (Ledger tab) "
                f"and save; I re-import your edits and each becomes a rule for next "
                f"month:\n{url}")
            result["workbook_link"] = 1

    made = (result["decisions"] + result["recats"] + result["receipts"]
            + result["clarify"] + result["workbook_link"] + result["notes"]
            + len(result.get("project_unresolved", []))
            + len(result["refunded"]) + len(result["refund_unresolved"]))
    if made == 0:
        # Be explicit when nothing was applied (reviewer feedback: "is she correcting?").
        cb = ("_(I didn't catch a specific change in that message, so I haven't recorded "
              "anything yet. Tell me a category, \"not billable\", \"refunded\", or attach a receipt for "
              "a charge and I'll update it. For a spreadsheet of corrections, it's easier "
              "to use the review workbook.)_\n\n") + cb
    result["confirm_back"] = cb
    return result


def _rewrite_qbo(cfg, external_id: str, coa: str):
    """Push a corrected category to the already-booked QBO charge (qbo-<id>)."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    gl = qbo_writer.load_mapping(cfg.client).get(coa)
    if not gl:
        return
    pid = external_id.split("qbo-", 1)[1]
    q = QBOFeed(realm_id=""); q._refresh_access_token()
    p = q.query(f"SELECT * FROM Purchase WHERE Id = '{pid}'")
    if p:
        qbo_writer.commit_one(q, {"purchase": p[0], "gl_id": gl, "billable_customer": None})


def _rewrite_qbo_billable(cfg, external_id: str, project: str, note: str = None,
                          customer=None, q=None):
    """Push a billable expense's client + optional invoice note to the booked QBO
    charge. When a customer is known, the charge is billable to a client, so it
    moves to the *Billable Expense* GL account (Natalie's rule: every client-billable
    expense must sit in Billable Expense so the T&E invoice reads cleanly — not its
    natural category like General Travel or Friday Lunch). Only marks the line
    Billable when a customer is known (commit_one skips BillableStatus when
    billable_customer is falsy), so we never write a customer-less billable, and a
    charge with no matched customer keeps its existing category untouched. Returns
    the resolved customer id (or None)."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    pid = external_id.split("qbo-", 1)[1]
    if q is None:
        q = QBOFeed(realm_id=""); q._refresh_access_token()
    p = q.query(f"SELECT * FROM Purchase WHERE Id = '{pid}'")
    if not p:
        return None
    p = p[0]
    cur_gl = next((ln["AccountBasedExpenseLineDetail"]["AccountRef"]["value"]
                   for ln in p["Line"] if ln.get("AccountBasedExpenseLineDetail")), None)
    if customer is None:                       # robust, paginated, fuzzy resolver
        customer = qbo_writer.resolve_customer(q, project)
    # Billable to a client -> Billable Expense GL; otherwise keep the category.
    target_gl = cur_gl
    if customer:
        target_gl = qbo_writer.load_mapping(cfg.client).get("Billable Expense") or cur_gl
    op = {"purchase": p, "gl_id": target_gl, "note": note}
    if customer:
        op["billable_customer"] = customer
    qbo_writer.commit_one(q, op)
    return customer


def _unbill_qbo(cfg, external_id: str, coa: str = None, q=None):
    """Clear a charge's Billable flag + customer on the booked QBO charge (qbo-<id>)
    when the pal says it's not billable. Without this the 'not billable' reply
    updates the ledger + category but leaves a STALE BillableStatus/CustomerRef in
    QBO, so the charge keeps showing on the client/T&E report (the drift Natalie
    flagged). Optionally also sets the natural category `coa`; otherwise keeps it."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    pid = external_id.split("qbo-", 1)[1]
    if q is None:
        q = QBOFeed(realm_id=""); q._refresh_access_token()
    p = q.query(f"SELECT * FROM Purchase WHERE Id = '{pid}'")
    if not p:
        return
    gl = qbo_writer.load_mapping(cfg.client).get(coa) if coa else None
    qbo_writer.commit_one(q, {"purchase": p[0], "gl_id": gl, "unbill": True})


def _attach_receipt_qbo(cfg, external_id: str, file_path, line: dict):
    """Attach a stored receipt file to its QBO expense (qbo-<id>)."""
    from ..adapters.books_out import qbo_writer
    from ..adapters.card_feed.qbo_feed import QBOFeed
    pid = external_id.split("qbo-", 1)[1]
    q = QBOFeed(realm_id=""); q._refresh_access_token()
    fname = (f"{dm_assemble._merchant(line['merchant_raw'])}_"
             f"{line['amount_cents']/100:.2f}_{line['txn_date']}")
    qbo_writer.attach_receipt(q, pid, file_path, fname)


def _projects(cfg, month):
    import yaml
    from ..config import CLIENTS_DIR
    f = CLIENTS_DIR / cfg.client / cfg.section("billable").get(
        "billable_projects_file", "active_projects.yaml")
    data = yaml.safe_load(f.read_text()) if f.exists() else {}
    return [p["project"] for p in (data.get(month, {}) or {}).get("projects", [])]
