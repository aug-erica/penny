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


def _pal_charges(conn, client, month, cardholder):
    return [l for l in ledger.lines_for_month(conn, client, month)
            if (l.get("cardholder") or "").lower().find(cardholder.lower()) >= 0
            and l["status"] != "excluded" and l["amount_cents"] > 0]


def process_pal_reply(conn, cfg, cardholder, text, month,
                      slack=None, file_ids=None) -> dict:
    """Apply a reply to the ledger. Returns {decisions, recats, rules,
    receipts, confirm_back}. `slack` is a PennySlack for downloading files."""
    charges = _pal_charges(conn, cfg.client, month, cardholder)
    by_ext = {c["external_id"]: c for c in charges}
    projects = _projects(cfg, month)
    result = {"decisions": 0, "recats": 0, "rules": 0, "receipts": 0}

    # 1. billable / project
    for d in reply_parse.interpret_reply(cfg.client, text, charges, projects):
        line = by_ext.get(d["external_id"])
        if line:
            b = 1 if d["billable"] else 0
            ledger.set_billable_project(conn, line["id"], b, d["project"])
            ledger.record_decision(conn, cfg.client, month, d["external_id"],
                                   "billable_project",
                                   {"billable": b, "project": d["project"]},
                                   decided_by=cardholder, source="slack")
            result["decisions"] += 1

    # 2. category corrections -> ledger + rules; non-COA names -> ask for clarity
    new_rules = {}
    clarifications = []
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
            result["recats"] += 1
        else:
            clarifications.append(r)   # attempted category isn't in the COA
    # Flywheel: persist each correction as a durable rule in Postgres.
    for mn, coa in new_rules.items():
        ledger.upsert_learned_rule(conn, cfg.client, mn, coa, "reviewer")
    result["rules"] = len(new_rules)
    result["clarify"] = len(clarifications)

    # 3. receipts — download + store; link to a charge by amount when possible
    result["filed"] = []          # (merchant, amount_cents) linked this reply
    result["unlinked"] = 0
    if slack and file_ids:
        amounts_in_text = {_to_cents(a) for a in _AMOUNT.findall(text)}
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
            if not cand and info.get("merchant"):
                from rapidfuzz import fuzz
                m = info["merchant"].lower()
                mm = [c for c in needed
                      if fuzz.partial_ratio(m, dm_assemble._merchant(c["merchant_raw"]).lower()) >= 80]
                if len(mm) == 1:
                    cand = mm
            target = cand[0] if cand else (needed[0] if len(needed) == 1 else None)
            if target:
                dest = receipt_store.store_file(cfg, month, cardholder, target, tmp)
                ledger.set_receipt_status(conn, target["id"], "stored", str(dest))
                ledger.record_decision(conn, cfg.client, month, target["external_id"],
                                       "receipt", {"status": "stored", "link": str(dest)},
                                       decided_by=cardholder, source="slack")
                result["filed"].append((target["merchant_raw"], target["amount_cents"]))
            else:
                fake = {"merchant_raw": "receipt-unmatched", "amount_cents": 0, "txn_date": month}
                receipt_store.store_file(cfg, month, cardholder, fake, tmp)
                result["unlinked"] += 1
            tmp.unlink(missing_ok=True)
            result["receipts"] += 1

    fresh = _pal_charges(conn, cfg.client, month, cardholder)
    bot = cfg.raw.get("bot", {}).get("name", "the expense bot")
    cb = dm_assemble.confirm_back(cardholder.split()[0], fresh, bot)
    if clarifications:
        qs = ["\n❓ *A couple didn't match our chart of accounts — which should these be?*"]
        for c in clarifications:
            opts = " / ".join(c.get("suggestions", [])) or "tell me the category"
            qs.append(f"   • {dm_assemble._merchant(c['merchant'])}: you said "
                      f"\"{c['new_category']}\" — did you mean {opts}?")
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
            + result["clarify"] + result["workbook_link"])
    if made == 0:
        # Be explicit when nothing was applied (reviewer feedback: "is she correcting?").
        cb = ("_(I didn't catch a specific change in that message, so I haven't recorded "
              "anything yet. Tell me a category, \"not billable\", or attach a receipt for "
              "a charge and I'll update it. For a spreadsheet of corrections, it's easier "
              "to use the review workbook.)_\n\n") + cb
    result["confirm_back"] = cb
    return result


def _projects(cfg, month):
    import yaml
    from ..config import CLIENTS_DIR
    f = CLIENTS_DIR / cfg.client / cfg.section("billable").get(
        "billable_projects_file", "active_projects.yaml")
    data = yaml.safe_load(f.read_text()) if f.exists() else {}
    return [p["project"] for p in (data.get(month, {}) or {}).get("projects", [])]
