"""Adapter interfaces. Card feed in, receipt vault, books out — today's choices
(Chase PDF, Expensify, QuickBooks) are implementations, never assumptions."""
from __future__ import annotations

from datetime import date
from typing import List, Protocol

from ..models import CardTxn, StatementCheck, VaultExpense


class CardFeedAdapter(Protocol):
    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        """All transactions with txn_date inside [month_start, month_end]."""
        ...

    def verify(self) -> List[StatementCheck]:
        """Self-checks against the feed's own totals; run() must refuse on failure."""
        ...


class ReceiptVaultAdapter(Protocol):
    def fetch_expenses(self, window_start: date, window_end: date,
                       states: List[str]) -> List[VaultExpense]:
        ...


class BooksOutAdapter(Protocol):
    def export(self, lines: List[dict], dest: str) -> str:
        """Write approved lines somewhere a human can take them onward.
        Phase 1: CSV only. Nothing ever posts without explicit approval."""
        ...
