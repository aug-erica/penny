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
import tempfile
from datetime import date, timedelta
from pathlib import Path

from .config import ClientConfig, env, load_client
from .engine import events
from .engine import gaps as gaps_mod
from .engine import ledger
from .engine import matching
from .engine import review
from .engine import validate as validate_mod
from .engine.categorize import pipeline as cat_pipeline
from .engine.normalize import card_txn_to_line, vault_expense_to_line

from .config import RUNS_LOCAL


def _month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _card_feed(cfg: ClientConfig, card_csv: str = None):
    # Explicit CSV export overrides the configured feed for this run (bridge
    # source until the daily QBO feed is wired). Doesn't touch config, so the
    # PDF-based historical months still rebuild cleanly.
    if card_csv:
        from .adapters.card_feed.chase_activity_csv import ChaseActivityCSV
        return ChaseActivityCSV(
            csv_path=Path(card_csv),
            cardholders=cfg.raw.get("cardholders", {}),
            account_last4=cfg.section("card_feed").get("account_last4", ""),
        )
    c = cfg.section("card_feed")
    if c.get("type") == "chase_statement_pdf":
        from .adapters.card_feed.chase_statement_pdf import ChaseStatementPDF
        return ChaseStatementPDF(
            close_data_dir=cfg.close_data_dir,
            month_folder_format=c["month_folder_format"],
            statement_glob=c["statement_glob"],
            account_last4=c["account_last4"],
        )
    if c.get("type") == "qbo_feed":
        from .adapters.card_feed.qbo_feed import QBOFeed
        return QBOFeed(account_ref=c.get("account_ref", ""),
                       realm_id=env("QBO_REALM_ID"))
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
    conn = ledger.open_db(RUNS_LOCAL / cfg.client / "ledger.sqlite3")
    if not conn.is_pg:
        # Guard against the split-brain trap: the live system of record is the
        # cloud Postgres. A command run without DATABASE_URL silently hits a
        # stale local file instead — warn loudly.
        print("⚠  LEDGER = local SQLite, NOT the live cloud Postgres. "
              "Set DATABASE_URL (Penny's DATABASE_PUBLIC_URL) to act on the real "
              "ledger; otherwise you're editing a stale local copy.", file=sys.stderr)
    return conn


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
    feed = _card_feed(cfg, card_csv=args.card_csv)
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
    ledger.reset_proposals_for_month(conn, cfg.client, month)  # idempotent re-runs
    ledger.rebuild_merchant_history(conn, cfg.client, through_month=month)
    stats = cat_pipeline.categorize_month(conn, cfg, month, use_llm=not args.no_llm)
    print(f"categorization: {stats}")

    # 3b. event-window overrides (calendar context: Summit weeks, launches)
    ev = events.apply_event_windows(conn, cfg, month)
    if ev["overridden"]:
        print(f"event windows: {ev['overridden']} lines recoded to event categories")

    # 3c. replay human decisions ON TOP of the fresh proposals — a re-run can
    # never lose a pal's or reviewer's word (they're journaled, not just rows).
    dec = ledger.apply_decisions(conn, cfg.client, month)
    if dec["applied"] or dec["skipped"]:
        print(f"decisions replayed: {dec['applied']} applied"
              + (f", {dec['skipped']} skipped (line no longer present)"
                 if dec["skipped"] else ""))

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
    wb = review.write_review_workbook(lines, g, summary, out_dir / f"review_{month}.xlsx",
                                      coa_lines=cfg.coa_lines)
    print(f"review workbook: {wb}")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


def cmd_close_approve(args):
    """Reviewer sign-off over the month: mark every categorized line approved.
    Approved lines are next month's top precedent source (the flywheel's real
    fuel) and the population Phase 3 will post to QuickBooks. Refuses if any
    line still has no category, unless --force."""
    cfg = load_client(args.client)
    conn = _db(cfg)
    reviewer = cfg.section("reviewer").get("primary", "") or "cli"
    res = ledger.approve_month(conn, cfg.client, args.month,
                               decided_by=reviewer, force=args.force)
    for l in res["blocked"]:
        print(f"  [no category] {l['txn_date']} {l['merchant_raw'][:40]:40s} "
              f"{l['amount_cents']/100:>9,.2f}  {l.get('cardholder') or ''}")
    if res["refused"]:
        print(f"REFUSED — {len(res['blocked'])} line(s) above have no category. "
              "Categorize them (dashboard or Slack), or --force to approve the rest.")
        return 1
    print(f"approved {res['approved']} line(s) for {args.month}"
          + (f" ({res['already']} already approved/posted)" if res["already"] else "")
          + (f"; {len(res['blocked'])} left open (no category)" if res["blocked"] else ""))
    print("These decisions are now the top precedent source for next month's close.")
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


# Legacy workbook re-import (cmd_close_ingest_review) and the rules.yaml
# file-writer (_merge_rules) were removed July 6, 2026. Reviewer corrections now
# happen live — in the editable dashboard or via Slack — and persist to Postgres
# (ledger.upsert_learned_rule), which the categorizer reads as the flywheel.
# rules.yaml remains only for curated seed rules.


def cmd_notify_build(args):
    """Assemble each pal's Slack DM from the month's ledger. Prints them (or one,
    with --only <cardholder>); sending is done separately so nothing goes out by
    accident."""
    import yaml
    from .config import CLIENTS_DIR
    from .engine import dm_assemble

    cfg = load_client(args.client)
    conn = _db(cfg)
    bot = cfg.raw.get("bot", {}).get("name", "the expense bot")
    proj_data = yaml.safe_load((CLIENTS_DIR / cfg.client / cfg.section("billable")
                                .get("billable_projects_file", "active_projects.yaml")).read_text())
    projects = (proj_data.get(args.month, {}) or {}).get("projects", [])

    lines = ledger.lines_for_month(conn, cfg.client, args.month)
    by_pal = {}
    for l in lines:
        if l["source"] != "card_feed" and not l.get("reimbursable"):
            continue
        by_pal.setdefault(l.get("cardholder") or "(unknown)", []).append(l)

    for pal, pal_lines in sorted(by_pal.items()):
        if args.only and args.only.lower() not in pal.lower():
            continue
        first = pal.split()[0]
        dm = dm_assemble.assemble_dm(first, pal_lines, projects, bot)
        print(f"\n===== DM → {pal} =====\n{dm}\n")
    return 0


def cmd_notify_send(args):
    """Send each open pal their expense DM AS Penny. Default targets pals who
    haven't responded and still have open items; --only NAME targets one.
    Dry-run unless --send."""
    import yaml
    from .config import CLIENTS_DIR
    from .engine import dm_assemble, digest as digest_mod
    from .adapters.slack_client import PennySlack

    cfg = load_client(args.client)
    conn = _db(cfg)
    bot = cfg.raw.get("bot", {})
    id_by_pal = {v: k for k, v in bot.get("slack_users", {}).items()}
    proj_data = yaml.safe_load((CLIENTS_DIR / cfg.client / "active_projects.yaml").read_text())
    projects = (proj_data.get(args.month, {}) or {}).get("projects", [])

    lines = ledger.lines_for_month(conn, cfg.client, args.month)
    by_pal = {}
    for l in lines:
        if l["source"] != "card_feed" and not l.get("reimbursable"):
            continue
        by_pal.setdefault(l.get("cardholder") or "(unknown)", []).append(l)

    penny = PennySlack() if args.send else None
    sent = 0
    for pal, pal_lines in sorted(by_pal.items()):
        if args.only and args.only.lower() not in pal.lower():
            continue
        st = digest_mod._pal_status(pal_lines)
        if not args.only and not (st["open"] > 0 and not st["responded"]):
            continue   # default: only un-responded pals with open items
        uid = id_by_pal.get(pal)
        if not uid:
            print(f"  [skip] no Slack id for {pal}")
            continue
        dm = dm_assemble.assemble_dm(pal.split()[0], pal_lines, projects,
                                     bot.get("name", "Penny"))
        if args.send:
            penny.send_dm(uid, dm)
            print(f"  sent → {pal}")
        else:
            print(f"  would send → {pal} ({st['open']} open)")
        sent += 1
    print(f"\n{'sent' if args.send else 'would send'} {sent} DM(s)"
          + ("" if args.send else " — add --send to actually send"))
    return 0


def cmd_notify_collect(args):
    """Apply a pal's reply (text) to the ledger via the shared processor —
    billable/project + category corrections + rules — and print the confirm-back.
    Same code path Penny's live listener uses."""
    from .engine import reply_flow
    cfg = load_client(args.client)
    conn = _db(cfg)
    reply = args.reply or (Path(args.reply_file).read_text() if args.reply_file else "")
    if not reply.strip():
        print("no reply text provided (--reply or --reply-file)")
        return 1
    res = reply_flow.process_pal_reply(conn, cfg, args.pal, reply, args.month)
    if not (res["decisions"] or res["recats"]):
        print("couldn't interpret the reply into changes (nothing changed).")
    else:
        print(f"applied {res['decisions']} billable/project + {res['recats']} "
              f"recategorization(s)" + (f"; wrote {res['rules']} rule(s)" if res['rules'] else ""))
    print("\n----- confirm-back message -----")
    print(res["confirm_back"])
    return 0


def cmd_notify_receipt(args):
    """Record a receipt a pal sent, matched to its charge by amount.
    --file <path> actually STORES the file in the repository ('stored').
    --link <ref> only records a pointer ('referenced' — not retained)."""
    from .engine import receipts, receipt_store
    cfg = load_client(args.client)
    conn = _db(cfg)
    charges = [l for l in ledger.lines_for_month(conn, cfg.client, args.month)
               if (l.get("cardholder") or "").lower().find(args.pal.lower()) >= 0]
    cents = round(float(args.amount) * 100)
    matches = receipts.match_by_amount(charges, cents)
    if not matches:
        print(f"no un-receipted charge needing a receipt at ${args.amount} for {args.pal}")
        return 1
    if len(matches) > 1:
        print(f"ambiguous — {len(matches)} charges at ${args.amount}; using the first:")
    m = matches[0]
    if args.slack_file:
        import tempfile
        from .adapters.slack_client import PennySlack
        tmp = Path(tempfile.mktemp())
        PennySlack().download_file(args.slack_file, tmp)
        dest = receipt_store.store_file(cfg, args.month, args.pal, m, tmp)
        tmp.unlink(missing_ok=True)
        ledger.set_receipt_status(conn, m["id"], "stored", str(dest))
        print(f"  receipt STORED (fetched from Slack via Penny): {m['merchant_raw'][:30]} "
              f"${cents/100:.2f} → {dest}")
    elif args.file:
        dest = receipt_store.store_file(cfg, args.month, args.pal, m, Path(args.file))
        ledger.set_receipt_status(conn, m["id"], "stored", str(dest))
        print(f"  receipt STORED: {m['merchant_raw'][:30]} ${cents/100:.2f} → {dest}")
    else:
        ledger.set_receipt_status(conn, m["id"], "referenced", args.link)
        print(f"  receipt REFERENCED only (file NOT retained): {m['merchant_raw'][:30]} "
              f"${cents/100:.2f} — needs Penny to be in the conversation to fetch {args.link}")
    return 0


def cmd_notify_digest(args):
    """Post the close status to #finance. First week of the month only unless
    --force. --post actually sends via Penny; otherwise it just prints."""
    from datetime import date
    from .engine import digest as digest_mod
    cfg = load_client(args.client)
    conn = _db(cfg)
    bot = cfg.raw.get("bot", {})
    today = date.today()
    day = today.day
    text = digest_mod.build_digest(conn, cfg, args.month, day=day)
    print(text)
    if not args.post:
        print("\n(dry run — add --post to send to #finance)")
        return 0
    if day > int(bot.get("digest_days", 7)) and not args.force:
        print(f"\nday {day} is past the first week — skipping post (use --force to override).")
        return 0
    from .adapters.slack_client import PennySlack, SlackError
    try:
        PennySlack()._post("chat.postMessage", channel=bot["digest_channel"], text=text)
        from datetime import datetime, timezone
        ledger.mark_digest_posted(conn, cfg.client, args.month,
                                  datetime.now(timezone.utc).date().isoformat())
        print(f"\nposted to #finance ({bot['digest_channel']})")
    except SlackError as e:
        print(f"\npost failed: {e}"
              + (" — invite @Penny to #finance first" if "not_in_channel" in str(e) else ""))
        return 1
    return 0


def cmd_penny_smoke(args):
    """Confirm Penny's bot works; optionally send a test DM to an email."""
    from .adapters.slack_client import PennySlack
    p = PennySlack()
    who = p.auth_test()
    print(f"OK — Penny is '{who['user']}' (id {who['user_id']}) in {who['team']}")
    if args.dm:
        uid = p.user_id_by_email(args.dm)
        p.send_dm(uid, args.message or "👋 Hi — it's Penny, August's expense bot, "
                  "now sending from my own account. (Test message.)")
        print(f"  sent test DM to {args.dm}")
    return 0


def cmd_penny_listen(args):
    """Start Penny's live Socket Mode listener — processes pal DMs autonomously."""
    from .adapters import penny_listener
    penny_listener.run(args.client, args.month)
    return 0


def cmd_penny_catchup(args):
    """Process any DMs Penny missed while offline (safe to re-run)."""
    from .adapters.slack_client import PennySlack
    from .engine import penny_catchup
    cfg = load_client(args.client)
    db_path = RUNS_LOCAL / cfg.client / "ledger.sqlite3"
    done = penny_catchup.catch_up(cfg, args.month, PennySlack(), db_path,
                                  post=args.post)
    if not done:
        print("no missed messages")
    for pal, n in done:
        print(f"  {pal}: {n} message(s) processed{'' if args.post else ' (dry-run, not sent)'}")
    return 0


def cmd_qbo_smoke(args):
    """Prove the QuickBooks connection: refresh the token and list credit-card
    accounts. Needs QBO_CLIENT_ID/SECRET/REFRESH_TOKEN/REALM_ID in .env."""
    from .adapters.card_feed.qbo_feed import QBOFeed, QBOError
    feed = QBOFeed()
    try:
        feed._refresh_access_token()
        print(f"OK — token refreshed ({env('QBO_ENV') or 'sandbox'}), realm {feed.realm_id}")
        accts = feed.credit_card_accounts()
        print(f"credit-card accounts visible ({len(accts)}):")
        for a in accts:
            print(f"  id {a['id']:>6}  {a['name']}")
    except QBOError as e:
        print(f"QBO smoke failed: {e}")
        return 1
    return 0


def cmd_db_smoke(args):
    """Prove the ledger backend: connect, ensure schema, round-trip a write.
    On Railway (DATABASE_URL set) this confirms Postgres; locally, SQLite."""
    from .engine import db
    conn = ledger.open_db(RUNS_LOCAL / args.client / "ledger.sqlite3")
    kind = "Postgres" if conn.is_pg else "SQLite"
    ledger.mark_dm_processed(conn, "smoke-test-ts", "smoke")
    n = len(ledger.processed_dm_ts(conn))
    lines = len(ledger.lines_for_month(conn, args.client, args.month)) if args.month else 0
    print(f"OK — ledger backend is {kind}. processed_dm rows: {n}"
          + (f"; {lines} lines for {args.month}" if args.month else ""))
    print(f"  DATABASE_URL {'set' if db.database_url() else 'not set (local SQLite)'}")
    return 0


def cmd_db_import(args):
    """One-time cutover: load the local SQLite ledger INTO Postgres. Run with
    DATABASE_URL pointing at the target Postgres. Safe to re-run (ON CONFLICT
    DO NOTHING)."""
    import sqlite3 as sq
    from .engine import db
    if not db.database_url():
        print("Set DATABASE_URL to the target Postgres first (this loads INTO it).")
        return 1
    src = sq.connect(args.from_path)
    src.row_factory = sq.Row
    dst = ledger.open_db(RUNS_LOCAL / args.client / "ledger.sqlite3")  # -> Postgres
    if not dst.is_pg:
        print("DATABASE_URL did not resolve to Postgres — aborting.")
        return 1
    conflict = {"runs": "(run_id)", "merchant_history":
                "(client, merchant_norm, coa_line, close_month)",
                "processed_dm": "(ts)", "ledger_lines": "(id)"}
    counts = {}
    # ledger_lines last, and in two passes, to satisfy its self-FK matched_line_id.
    for t in ["runs", "merchant_history", "processed_dm", "ledger_lines"]:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if t == "ledger_lines":
            ins = [c for c in cols if c != "matched_line_id"]
            ph = ",".join(["?"] * len(ins))
            dst.executemany(f"INSERT INTO ledger_lines ({','.join(ins)}) VALUES ({ph}) "
                            "ON CONFLICT (id) DO NOTHING",
                            [tuple(r[c] for c in ins) for r in rows])
            upd = [(r["matched_line_id"], r["id"]) for r in rows
                   if r["matched_line_id"] is not None]
            dst.executemany("UPDATE ledger_lines SET matched_line_id=? WHERE id=?", upd)
        else:
            ph = ",".join(["?"] * len(cols))
            dst.executemany(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph}) "
                            f"ON CONFLICT {conflict[t]} DO NOTHING",
                            [tuple(r[c] for c in cols) for r in rows])
        dst.commit()
        counts[t] = len(rows)
    for t, idc in [("runs", "run_id"), ("ledger_lines", "id")]:
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{t}','{idc}'), "
                    f"COALESCE((SELECT MAX({idc}) FROM {t}), 1))")
    dst.commit()
    print("imported into Postgres:", counts)
    return 0


def cmd_db_backfill(args):
    """One-time: journal the current month's human edits as decision rows, so
    edits made BEFORE the decisions journal existed survive future re-runs.
    Run once per pre-journal month (refuses if rows already exist)."""
    cfg = load_client(args.client)
    conn = _db(cfg)
    try:
        made = ledger.backfill_decisions(conn, cfg.client, args.month)
    except RuntimeError as exc:
        print(f"refused: {exc}")
        return 1
    print(f"backfilled {made} decision row(s) for {args.month}")
    return 0


def cmd_drive_smoke(args):
    """Prove Drive receipt storage: upload a tiny test file and print its link.
    Needs GOOGLE_SERVICE_ACCOUNT_JSON + DRIVE_RECEIPTS_FOLDER_ID."""
    from .engine import gdrive
    if not gdrive.configured():
        print("Drive not configured — set GOOGLE_SERVICE_ACCOUNT_JSON and "
              "DRIVE_RECEIPTS_FOLDER_ID (falls back to local filesystem).")
        return 1
    tmp = Path(tempfile.mktemp(suffix=".txt"))
    tmp.write_text("Penny Drive smoke test — safe to delete.")
    try:
        link = gdrive.upload(tmp, "penny-drive-smoke-test.txt", args.month or "smoke")
        print(f"OK — uploaded to Drive: {link}")
    finally:
        tmp.unlink(missing_ok=True)
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
    r.add_argument("--card-csv", default=None,
                   help="use a Chase activity CSV export as the card feed for "
                        "this run (overrides the configured feed)")
    r.add_argument("--shadow", action="store_true",
                   help="shadow close: include SUBMITTED reports, freeze the draft")
    r.set_defaults(fn=cmd_close_run)
    val = csub.add_parser("validate")
    val.add_argument("--client", required=True)
    val.add_argument("--month", required=True)
    val.set_defaults(fn=cmd_close_validate)
    ap = csub.add_parser("approve", help="reviewer sign-off: mark categorized lines approved")
    ap.add_argument("--client", required=True)
    ap.add_argument("--month", required=True)
    ap.add_argument("--force", action="store_true",
                    help="approve categorized lines even if some have no category yet")
    ap.set_defaults(fn=cmd_close_approve)

    notify = sub.add_parser("notify")
    nsub = notify.add_subparsers(dest="subcmd", required=True)
    nb = nsub.add_parser("build")
    nb.add_argument("--client", required=True)
    nb.add_argument("--month", required=True)
    nb.add_argument("--only", default=None, help="only this cardholder (substring match)")
    nb.set_defaults(fn=cmd_notify_build)
    ns = nsub.add_parser("send")
    ns.add_argument("--client", required=True)
    ns.add_argument("--month", required=True)
    ns.add_argument("--only", default=None, help="one cardholder (substring)")
    ns.add_argument("--send", action="store_true", help="actually send via Penny (else dry-run)")
    ns.set_defaults(fn=cmd_notify_send)
    nc = nsub.add_parser("collect")
    nc.add_argument("--client", required=True)
    nc.add_argument("--month", required=True)
    nc.add_argument("--pal", required=True, help="cardholder name (substring match)")
    nc.add_argument("--reply", default=None, help="the pal's reply text")
    nc.add_argument("--reply-file", default=None)
    nc.set_defaults(fn=cmd_notify_collect)
    nr = nsub.add_parser("receipt")
    nr.add_argument("--client", required=True)
    nr.add_argument("--month", required=True)
    nr.add_argument("--pal", required=True)
    nr.add_argument("--amount", required=True, help="receipt amount in dollars")
    nr.add_argument("--file", default=None, help="local path to the receipt file (stored in repo)")
    nr.add_argument("--slack-file", default=None, help="Slack file id — Penny downloads + stores it")
    nr.add_argument("--link", default=None, help="Slack file ref (referenced only, not retained)")
    nr.set_defaults(fn=cmd_notify_receipt)
    nd = nsub.add_parser("digest")
    nd.add_argument("--client", required=True)
    nd.add_argument("--month", required=True)
    nd.add_argument("--post", action="store_true", help="post to #finance (else dry-run)")
    nd.add_argument("--force", action="store_true", help="post even past the first week")
    nd.set_defaults(fn=cmd_notify_digest)

    vault = sub.add_parser("vault")
    vsub = vault.add_subparsers(dest="subcmd", required=True)
    s = vsub.add_parser("smoke")
    s.add_argument("--client", required=True)
    s.set_defaults(fn=cmd_vault_smoke)

    qbo = sub.add_parser("qbo")
    qsub = qbo.add_subparsers(dest="subcmd", required=True)
    qs = qsub.add_parser("smoke")
    qs.set_defaults(fn=cmd_qbo_smoke)

    penny = sub.add_parser("penny")
    psub = penny.add_subparsers(dest="subcmd", required=True)
    ps = psub.add_parser("smoke")
    ps.add_argument("--dm", default=None, help="send a test DM to this email")
    ps.add_argument("--message", default=None)
    ps.set_defaults(fn=cmd_penny_smoke)
    pl = psub.add_parser("listen")
    pl.add_argument("--client", required=True)
    pl.add_argument("--month", required=True, help="the close month being worked, YYYY-MM")
    pl.set_defaults(fn=cmd_penny_listen)
    pc = psub.add_parser("catchup")
    pc.add_argument("--client", required=True)
    pc.add_argument("--month", required=True, help="the close month being worked, YYYY-MM")
    pc.add_argument("--post", action="store_true", help="send confirm-backs (else dry-run)")
    pc.set_defaults(fn=cmd_penny_catchup)

    db_p = sub.add_parser("db")
    dbsub = db_p.add_subparsers(dest="subcmd", required=True)
    dbs = dbsub.add_parser("smoke", help="prove the ledger backend (Postgres in cloud)")
    dbs.add_argument("--client", default="august")
    dbs.add_argument("--month", default=None)
    dbs.set_defaults(fn=cmd_db_smoke)
    dbi = dbsub.add_parser("import-sqlite", help="one-time cutover load: SQLite -> Postgres")
    dbi.add_argument("--client", default="august")
    dbi.add_argument("--from", dest="from_path", required=True, help="path to local ledger.sqlite3")
    dbi.set_defaults(fn=cmd_db_import)
    dbb = dbsub.add_parser("backfill-decisions",
                           help="one-time: journal pre-existing human edits as decisions")
    dbb.add_argument("--client", default="august")
    dbb.add_argument("--month", required=True, help="YYYY-MM")
    dbb.set_defaults(fn=cmd_db_backfill)

    drive_p = sub.add_parser("drive")
    drsub = drive_p.add_subparsers(dest="subcmd", required=True)
    drs = drsub.add_parser("smoke", help="prove Drive receipt storage (upload a test file)")
    drs.add_argument("--month", default=None)
    drs.set_defaults(fn=cmd_drive_smoke)

    args = p.parse_args(argv)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
