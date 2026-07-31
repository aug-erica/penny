"""Convert a foreign-currency amount to USD for reimbursement.

Pals sometimes pay in another currency (a Waymo ride billed in CAD, a hotel in
EUR). Penny records what they actually paid, converts to USD at the expense-date
rate, and the caller FLAGS it so the reviewer confirms the rate — the exact rate
that hits the card/bank statement is the bank's call. Uses frankfurter.app (ECB
reference rates, free, no key). Any failure -> (None, None): the caller keeps the
original amount and asks the reviewer to convert.
"""
from __future__ import annotations

import re

_API = "https://api.frankfurter.app"
_CUR = re.compile(r"\b([A-Z]{3})\b")
# Currency words/symbols a pal might type, mapped to ISO codes.
_WORDS = {
    "CAD": "CAD", "CANADIAN": "CAD", "C$": "CAD", "CA$": "CAD",
    "USD": "USD", "US$": "USD", "DOLLARS": "USD",
    "EUR": "EUR", "EUROS": "EUR", "EURO": "EUR", "€": "EUR",
    "GBP": "GBP", "POUNDS": "GBP", "£": "GBP", "STERLING": "GBP",
    "MXN": "MXN", "PESOS": "MXN", "AUD": "AUD", "JPY": "JPY", "¥": "JPY",
    "INR": "INR", "CHF": "CHF",
}


def detect_currency(text: str):
    """A currency the pal named in their message ('in CAD', 'C$40'), or None."""
    t = (text or "").upper()
    for token, iso in _WORDS.items():
        if token.isalpha():
            if re.search(r"\b" + re.escape(token) + r"\b", t):
                return iso
        elif token in t:
            return iso
    return None


def to_usd_cents(amount_cents, currency: str, date: str):
    """(usd_cents, rate) for `amount_cents` in `currency` on `date` (YYYY-MM-DD):
    already-USD -> (amount_cents, 1.0); convertible -> (converted, rate); rate
    unavailable -> (None, None). `rate` is USD per 1 unit of `currency`."""
    if amount_cents is None or not currency:
        return None, None
    cur = currency.strip().upper()
    if cur == "USD":
        return amount_cents, 1.0
    try:
        import httpx
        r = httpx.get(f"{_API}/{date}", params={"from": cur, "to": "USD"}, timeout=15)
        r.raise_for_status()
        rate = (r.json().get("rates") or {}).get("USD")
    except Exception:
        return None, None
    if not rate:
        return None, None
    return round(amount_cents * float(rate)), float(rate)
