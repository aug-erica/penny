"""Process a pal's reply end-to-end — shared by the manual CLI and Penny's live
listener. Applies billable/project decisions and category corrections to the
ledger, stores any attached receipt files, and returns a confirm-back message.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from . import dm_assemble, ledger, receipt_store, receipts, reply_parse
from .normalize import normalize_merchant
from .rules_io import merge_rules

# Amounts with or without cents: $1,350 / $1,350.00 / 266.83
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")


def _to_cents(s: str) -> int:
    return round(float(s.replace(",", "")) * 100)


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
            ledger.set_billable_project(conn, line["id"], 1 if d["billable"] else 0, d["project"])
            result["decisions"] += 1

    # 2. category corrections -> ledger + rules
    new_rules = {}
    for r in reply_parse.interpret_recategorizations(cfg.client, text, charges, cfg.coa_lines):
        line = by_ext.get(r["external_id"])
        if line:
            ledger.set_proposal(conn, line["id"], r["new_category"], "reviewer", "high",
                                f"recategorized by {cardholder.split()[0]}: {r['why']}".strip(),
                                status="flagged")
            new_rules[normalize_merchant(line["merchant_raw"])] = r["new_category"]
            result["recats"] += 1
    result["rules"] = merge_rules(cfg.client, new_rules)

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
            # 1) amount named in the message; 2) sole outstanding need.
            cand = [c for c in needed if c["amount_cents"] in amounts_in_text]
            target = cand[0] if cand else (needed[0] if len(needed) == 1 else None)
            if target:
                dest = receipt_store.store_file(cfg, month, cardholder, target, tmp)
                ledger.set_receipt_status(conn, target["id"], "stored", str(dest))
                result["filed"].append((target["merchant_raw"], target["amount_cents"]))
            else:
                fake = {"merchant_raw": "receipt-unmatched", "amount_cents": 0, "txn_date": month}
                receipt_store.store_file(cfg, month, cardholder, fake, tmp)
                result["unlinked"] += 1
            tmp.unlink(missing_ok=True)
            result["receipts"] += 1

    fresh = _pal_charges(conn, cfg.client, month, cardholder)
    bot = cfg.raw.get("bot", {}).get("name", "the expense bot")
    result["confirm_back"] = dm_assemble.confirm_back(cardholder.split()[0], fresh, bot)
    return result


def _projects(cfg, month):
    import yaml
    from ..config import CLIENTS_DIR
    f = CLIENTS_DIR / cfg.client / cfg.section("billable").get(
        "billable_projects_file", "active_projects.yaml")
    data = yaml.safe_load(f.read_text()) if f.exists() else {}
    return [p["project"] for p in (data.get(month, {}) or {}).get("projects", [])]
