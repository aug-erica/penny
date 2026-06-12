from datetime import date
from pathlib import Path

import pytest

from cfo_agent.engine import ledger
from cfo_agent.engine.normalize import card_txn_to_line, normalize_merchant
from cfo_agent.engine.categorize import rules as rules_mod
from cfo_agent.models import CardTxn
from cfo_agent.adapters.card_feed.chase_statement_pdf import ChaseStatementPDF


def test_normalize_merchant_strips_processor_noise():
    assert normalize_merchant("SQ *COMPILATION COFFEE Brooklyn NY") == "COMPILATION COFFEE"
    assert normalize_merchant("TST*BABA COOL - FORT GRE Brooklyn NY") == "BABA COOL - FORT GRE"
    assert normalize_merchant("PRET A MANGER US0109 BROOKLYN NY") == "PRET A MANGER"
    assert normalize_merchant("ADOBE *800-833-6687 ADOBE.LY/ENUS CA") == "ADOBE"
    assert normalize_merchant("CHIPOTLE 2090 BROOKLYN NY") == "CHIPOTLE"
    # same-transform-both-sides matters more than perfection:
    assert normalize_merchant("VILLAGE GOURMET GROCERY NEW YORK NY") == "VILLAGE GOURMET GROCERY NEW"


def test_ledger_upsert_is_idempotent(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    t = CardTxn(date(2026, 4, 9), "CHIPOTLE 2090 BROOKLYN NY", 2635, "Erica", "8303:2026-05-07")
    line = card_txn_to_line(t, "august", "August Public Inc", "2026-04")
    id1 = ledger.upsert_line(conn, line)
    ledger.set_proposal(conn, id1, "8000-Meals", "rule", "high", "test", status="flagged")
    id2 = ledger.upsert_line(conn, line)
    assert id1 == id2
    row = ledger.lines_for_month(conn, "august", "2026-04")[0]
    # re-upsert must not clobber proposals or status
    assert row["proposed_coa_line"] == "8000-Meals"
    assert row["status"] == "flagged"


def test_payments_are_excluded():
    t = CardTxn(date(2026, 4, 13), "Payment Thank You - Web", -1500000, None, "8303:2026-05-07")
    line = card_txn_to_line(t, "august", "e", "2026-04")
    assert line["status"] == "excluded"


def test_rules_precedence():
    rules = {
        "merchant_rules": [{"match": "ANTHROPIC", "kind": "substring", "coa_line": "A"}],
        "keyword_rules": [{"match": "DOORDASH", "coa_line": "B"}],
        "cardholder_defaults": {"Erica A Seldin": "C"},
    }
    line = {"merchant_norm": "ANTHROPIC", "merchant_raw": "ANTHROPIC ANTHROPIC.COM CA",
            "cardholder": "Erica A Seldin"}
    p = rules_mod.propose(rules, line)
    assert p.coa_line == "A" and p.proposed_by == "rule"
    line2 = {"merchant_norm": "DD *DOORDASH X", "merchant_raw": "DD *DOORDASH X CA",
             "cardholder": "Erica A Seldin"}
    assert rules_mod.propose(rules, line2).coa_line == "B"
    line3 = {"merchant_norm": "MYSTERY", "merchant_raw": "MYSTERY", "cardholder": "Erica A Seldin"}
    p3 = rules_mod.propose(rules, line3)
    assert p3.coa_line == "C" and p3.confidence == "low"


def test_resolve_date_handles_year_wrap():
    r = ChaseStatementPDF._resolve_date
    start, end = date(2025, 12, 15), date(2026, 1, 14)
    assert r("12/20", start, end) == date(2025, 12, 20)
    assert r("01/05", start, end) == date(2026, 1, 5)


def test_merchant_history_excludes_month_under_test(tmp_path):
    conn = ledger.open_db(tmp_path / "l.sqlite3")
    from cfo_agent.models import VaultExpense
    from cfo_agent.engine.normalize import vault_expense_to_line
    for month, day in (("2026-03", 10), ("2026-04", 12)):
        e = VaultExpense(date(2026, int(month[5:]), day), "Anthropic", 4899, "USD",
                         "x@aug.co", "8200-Software", "", False, False, "u", f"r{month}",
                         "APPROVED", f"t{month}")
        ledger.upsert_line(conn, vault_expense_to_line(e, "august", "e", month))
    ledger.rebuild_merchant_history(conn, "august", through_month="2026-04")
    hist = ledger.history_for_merchant(conn, "august", "ANTHROPIC")
    assert len(hist) == 1 and hist[0]["n"] == 1  # March only — April's truth never leaks
