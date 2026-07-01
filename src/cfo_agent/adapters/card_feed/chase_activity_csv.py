"""Card-feed adapter: Chase 'activity' CSV export (manually downloaded).

A bridge between the month-end statement PDF and the eventual daily QBO feed —
a flat transaction list closer in shape to the feed. Columns:
  Card | Transaction Date | Post Date | Description | Category | Type | Amount | Memo
- `Card` is the last-4; mapped to cardholder via client config (`cardholders`),
  since the CSV carries no name.
- Amount sign is INVERTED vs. our model: a Sale is negative (-20.99). We flip so
  charges are positive and payments/returns negative, matching CardTxn.
- No printed control total, so verify() has nothing to tie to; completeness is
  cross-checked against the QBO rec report at close (same as the daily feed).
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List

from ...models import CardTxn, StatementCheck


def _cents(s: str) -> int:
    return round(float(s.replace(",", "")) * 100)


class ChaseActivityCSV:
    def __init__(self, csv_path: Path, cardholders: Dict[str, str],
                 account_last4: str, period_tag: str = None):
        self.csv_path = Path(csv_path)
        self.cardholders = {str(k): v for k, v in (cardholders or {}).items()}
        self.account_last4 = account_last4
        self.period_tag = period_tag or self.csv_path.stem
        self._txns = None

    def _parse(self) -> List[CardTxn]:
        if self._txns is not None:
            return self._txns
        out = []
        statement_ref = f"{self.account_last4}:{self.period_tag}"
        with self.csv_path.open(newline="") as f:
            for row in csv.DictReader(f):
                # Invert sign: CSV Sale is negative; our charges are positive.
                amount_cents = -_cents(row["Amount"])
                card = (row.get("Card") or "").strip()
                out.append(CardTxn(
                    txn_date=datetime.strptime(row["Transaction Date"], "%m/%d/%Y").date(),
                    merchant_raw=row["Description"].strip(),
                    amount_cents=amount_cents,
                    cardholder=self.cardholders.get(card, f"Card …{card}"),
                    statement_ref=statement_ref,
                ))
        self._txns = out
        return out

    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        return [t for t in self._parse() if month_start <= t.txn_date <= month_end]

    def verify(self) -> List[StatementCheck]:
        # No printed control total in an activity export; nothing to reconcile
        # against here. Completeness is verified vs the QBO rec report at close.
        return []
