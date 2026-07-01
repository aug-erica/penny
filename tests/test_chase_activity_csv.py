from datetime import date
from pathlib import Path

from cfo_agent.adapters.card_feed.chase_activity_csv import ChaseActivityCSV

CSV = """Card,Transaction Date,Post Date,Description,Category,Type,Amount,Memo
6369,06/30/2026,06/30/2026,RDU WRAL5 TRAVEL Shop,Merchandise,Sale,-20.99,
8303,06/15/2026,06/16/2026,AUTOMATIC PAYMENT - THANK,,Payment,24481.39,
9768,05/31/2026,06/01/2026,SOME MAY CHARGE,Food,Sale,-10.00,
6761,06/29/2026,06/29/2026,Hubspot Inc.,Merchandise,Sale,-2376.20,
"""


def _adapter(tmp_path):
    p = tmp_path / "june.csv"
    p.write_text(CSV)
    return ChaseActivityCSV(p, {"6369": "Tirzah Enumah", "6761": "Purvi Patel"},
                            account_last4="8303", period_tag="2026-06")


def test_sign_flip_and_cardholder(tmp_path):
    a = _adapter(tmp_path)
    june = a.fetch_transactions(date(2026, 6, 1), date(2026, 6, 30))
    # May 31 charge filtered out; 3 June rows remain
    assert len(june) == 3
    by_merch = {t.merchant_raw: t for t in june}
    # Sale -20.99 -> positive charge; cardholder mapped from last-4
    assert by_merch["RDU WRAL5 TRAVEL Shop"].amount_cents == 2099
    assert by_merch["RDU WRAL5 TRAVEL Shop"].cardholder == "Tirzah Enumah"
    # Payment +24481.39 -> negative (credit)
    assert by_merch["AUTOMATIC PAYMENT - THANK"].amount_cents == -2448139
    # Unknown card falls back to a labeled placeholder, never crashes
    assert by_merch["Hubspot Inc."].cardholder == "Purvi Patel"


def test_month_filter_excludes_prior_month(tmp_path):
    a = _adapter(tmp_path)
    june = a.fetch_transactions(date(2026, 6, 1), date(2026, 6, 30))
    assert all(t.txn_date.month == 6 for t in june)
