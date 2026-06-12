"""Merchant normalization and CardTxn/VaultExpense -> ledger-line dicts."""
from __future__ import annotations

import re

from ..models import CardTxn, VaultExpense

# Processor prefixes that obscure the actual merchant.
_PREFIXES = re.compile(
    r"^(SQ \*|TST\*\s?|TST\* |DD \*|PP\*|PAYPAL \*|SP |TL\* |D J\*|WWW\.)", re.I
)
# Trailing US city/state pairs, phone numbers, and store numbers.
_STATE = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    "NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
# Strip one trailing city token + state ("BROOKLYN NY"). Multi-word cities
# leave a residue ("NEW YORK NY" -> "NEW") — acceptable: normalization feeds
# fuzzy token matching, and both sides of every comparison use this transform.
_TRAIL_LOC = re.compile(r"\s+[A-Za-z.'&/-]{2,20}\s+(" + _STATE + r")$")
_PHONE = re.compile(r"\s*\d{3}-\d{3}-\d{4}")
_NUMS = re.compile(r"\s+[#*]?(?:[A-Za-z]{1,3})?\d{2,}\S*")  # store/code tokens: '2090', 'US0109', '#14400'
_MULTISPACE = re.compile(r"\s{2,}")


def normalize_merchant(raw: str) -> str:
    s = raw.strip()
    s = _PREFIXES.sub("", s)
    s = _PHONE.sub("", s)
    s = _TRAIL_LOC.sub("", s)
    s = _NUMS.sub("", s)
    s = _MULTISPACE.sub(" ", s)
    return s.strip(" *-").upper()


def card_txn_to_line(t: CardTxn, client: str, entity: str, close_month: str) -> dict:
    return {
        "external_id": t.external_id(),
        "client": client,
        "entity": entity,
        "close_month": close_month,
        "txn_date": t.txn_date.isoformat(),
        "merchant_raw": t.merchant_raw,
        "merchant_norm": normalize_merchant(t.merchant_raw),
        "amount_cents": t.amount_cents,
        "currency": t.currency,
        "source": "card_feed",
        "statement_ref": t.statement_ref,
        "cardholder": t.cardholder,
        # Payments/credits to the account are not expenses to categorize.
        "status": "excluded" if t.amount_cents < 0 else "draft",
    }


def vault_expense_to_line(e: VaultExpense, client: str, entity: str, close_month: str) -> dict:
    return {
        "external_id": e.external_id(),
        "client": client,
        "entity": entity,
        "close_month": close_month,
        "txn_date": e.expense_date.isoformat(),
        "merchant_raw": e.merchant,
        "merchant_norm": normalize_merchant(e.merchant),
        "amount_cents": e.amount_cents,
        "currency": e.currency,
        "source": "receipt_vault",
        "employee": e.employee,
        "report_state": e.report_state,
        "reimbursable": 1 if e.reimbursable else 0,
        "receipt_link": e.receipt_url or None,
        "truth_category": e.category or None,
        "truth_billable": 1 if e.billable else 0,
        "status": "draft",
    }
