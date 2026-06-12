"""cfo — the Expense Close Agent CLI.

  cfo verify-feed   --client august                 # parser self-checks, all statements
  cfo close run     --client august --month 2026-04 [--no-llm] [--no-vault] [--shadow]
  cfo close validate --client august --month 2026-04
  cfo vault smoke   --client august                 # Expensify credentials smoke test

Adapters are constructed HERE from client config — the engine never knows
which card feed or vault a client uses.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import ClientConfig, env, load_client
from .engine import gaps as gaps_mod
from .engine import ledger
from .engine import matching
from .engine import review
from .engine import validate as validate_mod
from .engine.categorize import pipeline as cat_pipeline
from .engine.normalize import card_txn_to_line, vault_expense_to_line

RUNS_LOCAL = Path(__file__).resolve().parents[2] / "runs"


def _month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _card_feed(cfg: ClientConfig):
    c = cfg.section("card_feed")
    if c.get("type") == "chase_statement_pdf":
        from .adapters.card_feed.chase_statement_pdf import ChaseStatementPDF
        return ChaseStatementPDF(
            close_data_dir=cfg.close_data_dir,
            month_folder_format=c["month_folder_format"],
            statement_glob=c["statement_glob"],
            account_last4=c["account_last4"],
        )
    raise SystemExit(f"Unknown card_feed type: {c.get('type')}")


def _vault(cfg: ClientConfig):
    v = cfg.section("receipt_vault")
    if v.get("type") == "expensify":
        from .adapters.receipt_vault.expensify_exporter import ExpensifyExporter
        return ExpensifyExporter(
            partner_user_id=env("EXPENSIFY_PARTNER_USER_ID"),
            partner_user_secret=env("EXPENSIFY_PARTNER_USER_SECRET"),
            template_path=Path(__file__).parent / "adapters/receipt_vault/templates/expenses_csv.ftl",
            cache_dir=RUNS_LOCAL / cfg.client / "raw",
        )
    raise SystemExit(f"Unknown receipt_vault type: {v.get('type')}")


def _db(cfg: ClientConfig):
    return ledger.open_db(RUNS_LOCAL / cfg.client / "ledger.sqlite3")


def cmd_verify_feed(args):
    cfg = load_client(args.client)
    feed = _card_feed(cfg)
    checks = feed.verify()
    bad = 0
    for ch in checks:
        flag = "OK " if ch.ok else "FAIL"
        print(f"[{flag}] {ch.statement_ref}")
        print(f"      purchases printed {ch.printed_purchases_cents/100:>12,.2f} "
              f"parsed {ch.parsed_purchases_cents/100:>12,.2f}")
        print(f"      payments  printed {ch.printed_payments_cents/100:>12,.2f} "
              f"parsed {ch.parsed_payments_cents/100:>12,.2f}")
        for name, card, printed, parsed in ch.cardholder_ties:
            tie = "tie" if printed == parsed else f"MISMATCH ({printed/100:,.2f} vs {parsed/100:,.2f})"
            print(f"      card {card} {name:30s} {tie}")
        bad += 0 if ch.ok else 1
    print(f"\n{len(checks)} statements, {len(checks) - bad} clean, {bad} failed")
    return 1 if bad else 0


def cmd_close_run(args):
    cfg = load_client(args.client)
    conn = _db(cfg)
    month = args.month
    m_start, m_end = _month_bounds(month)
    entity = cfg.raw.get("entity", cfg.client)
    params = json.dumps({"month": month, "shadow": args.shadow, "no_llm": args.no_llm,
                         "no_vault": args.no_vault})
    run_id = ledger.start_run(conn, cfg.client, month, params)
    print(f"run {run_id} — {cfg.client} {month}"
          + (" [SHADOW — frozen draft, blind to current truth]" if args.shadow else ""))

    # 1. card feed (verify first; refuse to run on parse mismatch)
    feed = _card_feed(cfg)
    bad = [c for c in feed.verify() if not c.ok]
    if bad:
        for c in bad:
            print(f"REFUSING TO RUN — statement parse does not tie: {c.statement_ref}")
        return 1
    txns = feed.fetch_transactions(m_start, m_end)
    for t in txns:
        ledger.upsert_line(conn, card_txn_to_line(t, cfg.client, entity, month))
    n_charges = sum(1 for t in txns if t.amount_cents > 0)
    print(f"card feed: {len(txns)} txns ({n_charges} charges) "
          f"{sum(t.amount_cents for t in txns if t.amount_cents > 0)/100:,.2f}")

    # 2. receipt vault
    if not args.no_vault:
        vcfg = cfg.section("receipt_vault")
        states = vcfg["shadow_report_states"] if args.shadow else vcfg["report_states"]
        window_start = m_start - timedelta(days=int(vcfg.get("window_pad_days_before", 45)))
        window_end = date.today()
        expenses = _vault(cfg).fetch_expenses(window_start, window_end, list(states))
        in_month = [e for e in expenses if m_start <= e.expense_date <= m_end]
        for e in in_month:
            ledger.upsert_line(conn, vault_expense_to_line(e, cfg.client, entity, month))
        print(f"receipt vault: {len(in_month)} expenses in {month} "
              f"(of {len(expenses)} in window; "
              f"{sum(1 for e in in_month if e.reimbursable)} reimbursable)")
        mcfg = cfg.section("matching")
        stats = matching.match_month(conn, cfg.client, month,
                                     int(mcfg.get("date_window_days", 3)),
                                     int(mcfg.get("merchant_fuzz_threshold", 80)))
        print(f"matching: {stats}")
    else:
        print("receipt vault: skipped (--no-vault)")

    # 3. categorize (history strictly from months before this one; the agent
    # proposes independently — employee coding is cross-check only)
    ledger.rebuild_merchant_history(conn, cfg.client, through_month=month)
    stats = cat_pipeline.categorize_month(conn, cfg, month, use_llm=not args.no_llm)
    print(f"categorization: {stats}")

    # 4. artifacts
    lines = ledger.lines_for_month(conn, cfg.client, month)
    rec = _find_rec_report(cfg, m_end)
    g = gaps_mod.gaps_for_month(conn, cfg.client, month, rec)
    # One row per real expense: card charges + reimbursable vault expenses.
    # (A vault line matched to a card line is the same expense seen twice.)
    active = [l for l in lines if l["status"] != "excluded"
              and (l["source"] == "card_feed" or l.get("reimbursable"))]
    summary = {
        "close month": month,
        "transactions": len(active),
        "total $": round(sum(l["amount_cents"] for l in active if l["amount_cents"] > 0) / 100, 2),
        "high confidence": sum(1 for l in active if l.get("confidence") == "high"),
        "flagged for review": sum(1 for l in active if l["status"] == "flagged"),
        "card charges without receipt": len(g["card_without_receipt"]),
        "vault expenses without charge": len(g["vault_without_charge"]),
        "needs reviewer (no proposal)": len(g["uncategorized"]),
        "agent disagrees with Expensify coding": stats.get("disagrees_with_expensify", 0),
        "statement charges missing from books rec": len(g["rec_report_gaps"]),
        "awaiting next reconciliation": len(g["rec_awaiting"]),
        "books cross-check": g["rec_report_note"],
        "reviewer": cfg.section("reviewer").get("primary", ""),
        "run id / drafted at": f"{run_id} / {ledger.now()}",
    }
    out_dir = cfg.runs_dir / month
    wb = review.write_review_workbook(lines, g, summary, out_dir / f"review_{month}.xlsx")
    print(f"review workbook: {wb}")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


def cmd_close_validate(args):
    cfg = load_client(args.client)
    conn = _db(cfg)
    v = validate_mod.validate_month(conn, cfg.client, args.month)
    report = validate_mod.render_report(v)
    out = cfg.runs_dir / args.month / f"validation_{args.month}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"written to {out}")
    return 0


def cmd_vault_smoke(args):
    cfg = load_client(args.client)
    vault = _vault(cfg)
    today = date.today()
    expenses = vault.fetch_expenses(today - timedelta(days=60), today,
                                    ["APPROVED", "REIMBURSED"])
    print(f"OK — {len(expenses)} expenses in the last 60 days")
    submitters = sorted({e.employee for e in expenses})
    cats = sorted({e.category for e in expenses if e.category})
    print(f"submitters visible ({len(submitters)}): {', '.join(submitters)}")
    print(f"categories seen ({len(cats)}):")
    for c in cats:
        print(f"  - {c}")
    n_receipt = sum(1 for e in expenses if e.receipt_url)
    print(f"receipt URLs present: {n_receipt}/{len(expenses)}")
    print("\nSpot-check 5 lines against the Expensify UI:")
    for e in expenses[:5]:
        print(f"  {e.expense_date} {e.merchant[:40]:40s} {e.amount_cents/100:>9,.2f} "
              f"{e.category[:30]:30s} {e.employee}")
    return 0


def _find_rec_report(cfg: ClientConfig, month_end: date):
    """A close month's charges clear across two reconciliations (cycle ~8th-7th);
    merge every available rec report into one containment pool."""
    from .adapters.books_out.qbo_rec_report import MergedRecReports, QBORecReport
    glob = cfg.section("card_feed").get("rec_report_glob")
    if not glob:
        return None
    hits = sorted(cfg.close_data_dir.glob(f"*/{glob}"))
    if not hits:
        return None
    return MergedRecReports([QBORecReport(p) for p in hits])


def main(argv=None):
    p = argparse.ArgumentParser(prog="cfo")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify-feed")
    v.add_argument("--client", required=True)
    v.set_defaults(fn=cmd_verify_feed)

    close = sub.add_parser("close")
    csub = close.add_subparsers(dest="subcmd", required=True)
    r = csub.add_parser("run")
    r.add_argument("--client", required=True)
    r.add_argument("--month", required=True, help="YYYY-MM")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-vault", action="store_true")
    r.add_argument("--shadow", action="store_true",
                   help="shadow close: include SUBMITTED reports, freeze the draft")
    r.set_defaults(fn=cmd_close_run)
    val = csub.add_parser("validate")
    val.add_argument("--client", required=True)
    val.add_argument("--month", required=True)
    val.set_defaults(fn=cmd_close_validate)

    vault = sub.add_parser("vault")
    vsub = vault.add_subparsers(dest="subcmd", required=True)
    s = vsub.add_parser("smoke")
    s.add_argument("--client", required=True)
    s.set_defaults(fn=cmd_vault_smoke)

    args = p.parse_args(argv)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
