"""Shared dataclasses passed between adapters and the engine."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class CardTxn:
    """One transaction parsed from a card feed."""
    txn_date: date
    merchant_raw: str
    amount_cents: int            # signed: charges > 0, payments/credits < 0
    cardholder: Optional[str]
    statement_ref: str           # e.g. "8303:2026-05-07"
    currency: str = "USD"

    def external_id(self) -> str:
        key = f"card|{self.statement_ref}|{self.txn_date}|{self.amount_cents}|{self.merchant_raw}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]


@dataclass
class VaultExpense:
    """One expense pulled from the receipt vault (Expensify)."""
    expense_date: date
    merchant: str
    amount_cents: int
    currency: str
    employee: str                # submitter email
    category: str                # the vault's approved category (ground truth)
    tag: str
    billable: bool
    reimbursable: bool
    receipt_url: str
    report_id: str
    report_state: str
    vault_id: str                # vault's own transaction id, if present

    def external_id(self) -> str:
        key = f"vault|{self.report_id}|{self.vault_id}|{self.expense_date}|{self.amount_cents}|{self.merchant}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]


@dataclass
class RecLine:
    """One cleared line from a books-side reconciliation report (cross-check only)."""
    txn_date: date
    txn_type: str
    ref_no: str
    payee: str
    amount_cents: int


@dataclass
class Proposal:
    coa_line: str
    proposed_by: str             # rule | history | llm
    confidence: str              # high | medium | low
    rationale: str
    billable: Optional[bool] = None


@dataclass
class StatementCheck:
    """Per-statement parse verification against printed totals."""
    statement_ref: str
    printed_purchases_cents: int
    parsed_purchases_cents: int
    printed_payments_cents: int
    parsed_payments_cents: int
    printed_fees_cents: int = 0
    parsed_fees_cents: int = 0
    cardholder_ties: list = field(default_factory=list)  # (name, card, printed, parsed)

    @property
    def ok(self) -> bool:
        return (
            self.printed_purchases_cents == self.parsed_purchases_cents
            and self.printed_payments_cents == self.parsed_payments_cents
            and self.printed_fees_cents == self.parsed_fees_cents
            and all(p == q for _, _, p, q in self.cardholder_ties)
        )
