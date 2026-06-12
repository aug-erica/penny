"""Card-feed adapter v1: Chase credit-card statement PDFs.

Layout facts (verified against Jan–May 2026 statements):
- ACCOUNT ACTIVITY lines: "MM/DD  MERCHANT DESCRIPTION  amount" (amount signed).
- Cardholder attribution is a TRAILER: each cardholder's transactions are
  followed by "<NAME>" then "TRANSACTIONS THIS CYCLE (CARD nnnn) $total[-]".
  A trailing "-" on the total means negative (payments land on the main card).
- Page 1 carries printed control totals: "Purchases +$x", "Payment, Credits -$x"
  and "Opening/Closing Date MM/DD/YY - MM/DD/YY" for year resolution.
- Airline fare continuation lines ("040926 1 C PWM LGA") don't match the
  transaction regex and are skipped.

verify() reconciles parsed sums against every printed total; the pipeline
refuses to run on any mismatch.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pdfplumber

from ...models import CardTxn, StatementCheck

TXN = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]*\.\d{2})$")
CYCLE = re.compile(r"TRANSACTIONS THIS CYCLE \(CARD (\d{4})\)\s+\$([\d,]*\.\d{2})(-?)")
PERIOD = re.compile(r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})")
PURCHASES = re.compile(r"^Purchases \+\$([\d,]*\.\d{2})$")
PAYMENTS = re.compile(r"^Payment, Credits -\$([\d,]*\.\d{2})$")
FEES = re.compile(r"^Fees Charged \+?\$([\d,]*\.\d{2})$")
# Account-level fees appear as transaction lines but are excluded from the
# printed "Purchases" summary total — exact descriptions only, so genuine
# merchants like "DELTA AIR Baggage Fee" stay purchases.
ACCOUNT_FEES = {"ANNUAL MEMBERSHIP FEE", "LATE FEE", "RETURNED PAYMENT FEE",
                "FOREIGN TRANSACTION FEE", "CASH ADVANCE FEE", "BALANCE TRANSFER FEE"}


def _cents(s: str) -> int:
    return round(float(s.replace(",", "")) * 100)


class ChaseStatementPDF:
    """Parses every statement matching the configured glob, exposes transactions
    by date range. Statement cycle (~8th to 7th) means a close month spans two
    statements; callers pass the calendar month and we filter by txn date."""

    def __init__(self, close_data_dir: Path, month_folder_format: str,
                 statement_glob: str, account_last4: str):
        self.close_data_dir = Path(close_data_dir)
        self.month_folder_format = month_folder_format
        self.statement_glob = statement_glob
        self.account_last4 = account_last4
        self._parsed: dict = {}   # path -> (txns, check)

    # -- discovery ---------------------------------------------------------
    def statement_paths(self) -> List[Path]:
        return sorted(self.close_data_dir.glob(f"*/{self.statement_glob}"))

    # -- parsing -----------------------------------------------------------
    def _parse(self, path: Path):
        if path in self._parsed:
            return self._parsed[path]
        lines: List[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                lines.extend((page.extract_text() or "").splitlines())

        period_start = period_end = None
        printed_purchases = printed_payments = printed_fees = None
        for ln in lines:
            m = PERIOD.search(ln)
            if m and period_start is None:
                period_start = datetime.strptime(m.group(1), "%m/%d/%y").date()
                period_end = datetime.strptime(m.group(2), "%m/%d/%y").date()
            m = PURCHASES.match(ln.strip())
            if m and printed_purchases is None:
                printed_purchases = _cents(m.group(1))
            m = PAYMENTS.match(ln.strip())
            if m and printed_payments is None:
                printed_payments = _cents(m.group(1))
            m = FEES.match(ln.strip())
            if m and printed_fees is None:
                printed_fees = _cents(m.group(1))
        if not period_end:
            raise ValueError(f"Could not find statement period in {path.name}")
        statement_ref = f"{self.account_last4}:{period_end.isoformat()}"

        # Collect transaction blocks; cardholder trailers close each block.
        txns: List[CardTxn] = []
        block: List[CardTxn] = []
        ties = []
        prev_line = ""
        for ln in lines:
            stripped = ln.strip()
            m = CYCLE.search(stripped)
            if m:
                name = prev_line.strip().title()
                printed = _cents(m.group(2)) * (-1 if m.group(3) else 1)
                parsed = sum(t.amount_cents for t in block)
                for t in block:
                    t.cardholder = name
                ties.append((name, m.group(1), printed, parsed))
                txns.extend(block)
                block = []
                prev_line = stripped
                continue
            t = TXN.match(stripped)
            if t:
                amt = t.group(3)
                block.append(CardTxn(
                    txn_date=self._resolve_date(t.group(1), period_start, period_end),
                    merchant_raw=t.group(2).strip(),
                    amount_cents=_cents(amt) if not amt.startswith("-")
                                 else -_cents(amt[1:]),
                    cardholder=None,
                    statement_ref=statement_ref,
                ))
            prev_line = stripped
        if block:
            # Transactions after the last trailer would mean an unattributed
            # block — surface loudly rather than guess.
            ties.append(("UNATTRIBUTED-TAIL", "????", 0,
                         sum(t.amount_cents for t in block)))
            txns.extend(block)

        is_fee = lambda t: t.merchant_raw.upper() in ACCOUNT_FEES
        parsed_purchases = sum(t.amount_cents for t in txns
                               if t.amount_cents > 0 and not is_fee(t))
        parsed_payments = sum(-t.amount_cents for t in txns if t.amount_cents < 0)
        parsed_fees = sum(t.amount_cents for t in txns if is_fee(t))
        check = StatementCheck(
            statement_ref=f"{path.name} [{statement_ref}]",
            printed_purchases_cents=printed_purchases or 0,
            parsed_purchases_cents=parsed_purchases,
            printed_payments_cents=printed_payments or 0,
            parsed_payments_cents=parsed_payments,
            printed_fees_cents=printed_fees or 0,
            parsed_fees_cents=parsed_fees,
            cardholder_ties=ties,
        )
        self._parsed[path] = (txns, check)
        return txns, check

    @staticmethod
    def _resolve_date(mmdd: str, period_start: date, period_end: date) -> date:
        """MM/DD has no year; pick the year that puts the date nearest the
        statement period (handles Dec/Jan wrap)."""
        month, day = int(mmdd[:2]), int(mmdd[3:])
        candidates = []
        for year in {period_start.year, period_end.year, period_start.year - 1}:
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue
        mid = period_start + (period_end - period_start) / 2
        return min(candidates, key=lambda d: abs(d - mid))

    # -- adapter interface ---------------------------------------------------
    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        out = []
        for path in self.statement_paths():
            txns, _ = self._parse(path)
            if not txns:
                continue
            lo = min(t.txn_date for t in txns)
            hi = max(t.txn_date for t in txns)
            if hi < month_start or lo > month_end:
                continue
            out.extend(t for t in txns if month_start <= t.txn_date <= month_end)
        # A txn appearing on two overlapping statements (posted near cycle edge)
        # dedupes downstream on external_id, which includes statement_ref —
        # so dedupe here on (date, amount, merchant) keeping first occurrence.
        seen, deduped = set(), []
        for t in sorted(out, key=lambda t: (t.txn_date, t.statement_ref)):
            key = (t.txn_date, t.amount_cents, t.merchant_raw)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(t)
        return deduped

    def verify(self) -> List[StatementCheck]:
        return [self._parse(p)[1] for p in self.statement_paths()]
