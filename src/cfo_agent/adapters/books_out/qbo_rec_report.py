"""Reader for QuickBooks Online reconciliation-report PDFs (read-only cross-check).

The rec report is NOT a categorization source (payee is mostly "Credit Card
Misc.", no GL column) and its gross activity exceeds the statement's (QBO
carries journals and offsetting entries that net out). The honest completeness
check is containment: every statement charge should appear among cleared
charges by (date, amount); we also verify the summary balance equation ties.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber

from ...models import RecLine

LINE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(.*?)\s*(-?[\d,]+\.\d{2})$")
SECTION_CHARGES = re.compile(r"^Charges and cash advances cleared \((\d+)\)")
SECTION_PAYMENTS = re.compile(r"^Payments and credits cleared \((\d+)\)")
SECTION_OTHER = re.compile(r"^(Uncleared|Additional Information|Cleared transactions after)")
PERIOD_END = re.compile(r"Period Ending (\d{2}/\d{2}/\d{4})")
SUMMARY = {
    "begin": re.compile(r"^Statement beginning balance (-?[\d,]+\.\d{2})$"),
    "charges": re.compile(r"^Charges and cash advances cleared \(\d+\) (-?[\d,]+\.\d{2})$"),
    "payments": re.compile(r"^Payments and credits cleared \(\d+\) (-?[\d,]+\.\d{2})$"),
    "end": re.compile(r"^Statement ending balance (-?[\d,]+\.\d{2})$"),
}


def _cents(s: str) -> int:
    neg = s.startswith("-")
    return (-1 if neg else 1) * round(float(s.lstrip("-").replace(",", "")) * 100)


class QBORecReport:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.summary: dict = {}
        self.charges: List[RecLine] = []
        self.payments: List[RecLine] = []
        self.declared_charge_count: Optional[int] = None
        self.declared_payment_count: Optional[int] = None
        self.reconciled_by: Optional[str] = None
        self.reconciled_on: Optional[str] = None
        self.period_end = None   # date — end of the reconciled statement period
        self._parse()

    def _parse(self):
        lines: List[str] = []
        with pdfplumber.open(self.path) as pdf:
            for page in pdf.pages:
                lines.extend((page.extract_text() or "").splitlines())

        section = None
        for raw in lines:
            ln = raw.strip()
            if self.period_end is None:
                m = PERIOD_END.search(ln)
                if m:
                    self.period_end = datetime.strptime(m.group(1), "%m/%d/%Y").date()
            if ln.startswith("Reconciled by:"):
                self.reconciled_by = ln.split(":", 1)[1].strip()
            if ln.startswith("Reconciled on:"):
                self.reconciled_on = ln.split(":", 1)[1].strip()
            for key, rx in SUMMARY.items():
                m = rx.match(ln)
                if m and key not in self.summary:
                    self.summary[key] = _cents(m.group(1))
            m = SECTION_CHARGES.match(ln)
            if m:
                # The summary block repeats this header with a trailing amount;
                # only the Details occurrence (bare) starts the section.
                if not re.search(r"-?[\d,]+\.\d{2}$", ln[m.end():].strip() or ""):
                    section = "charges"
                    self.declared_charge_count = int(m.group(1))
                continue
            m = SECTION_PAYMENTS.match(ln)
            if m:
                if not re.search(r"-?[\d,]+\.\d{2}$", ln[m.end():].strip() or ""):
                    section = "payments"
                    self.declared_payment_count = int(m.group(1))
                continue
            if SECTION_OTHER.match(ln):
                section = None
                continue
            if section and ln.startswith("Total"):
                section = None
                continue
            if section:
                m = LINE.match(ln)
                if not m:
                    continue
                middle = m.group(2).strip()
                parts = middle.split(None, 2)
                txn_type, ref_no, payee = "", "", ""
                if parts:
                    # TYPE may be multi-word ("Credit Card Credit"); REF NO. is a
                    # QB id (R00...NR) or journal number; whatever remains = payee.
                    mm = re.match(
                        r"^(Expense|Credit Card Credit|Credit Card Payment|Journal|"
                        r"Bill Payment.*?|Check|Deposit|Transfer)\s*(\S*)\s*(.*)$",
                        middle)
                    if mm:
                        txn_type, ref_no, payee = mm.group(1), mm.group(2), mm.group(3)
                    else:
                        payee = middle
                rec = RecLine(
                    txn_date=datetime.strptime(m.group(1), "%m/%d/%Y").date(),
                    txn_type=txn_type, ref_no=ref_no, payee=payee.strip(),
                    amount_cents=_cents(m.group(3)),
                )
                (self.charges if section == "charges" else self.payments).append(rec)

    # -- checks --------------------------------------------------------------
    def summary_ties(self) -> bool:
        s = self.summary
        if not all(k in s for k in ("begin", "charges", "payments", "end")):
            return False
        return s["begin"] + s["charges"] + s["payments"] == s["end"]

    def detail_ties(self) -> Tuple[bool, bool]:
        charges_ok = (self.declared_charge_count == len(self.charges)
                      and sum(c.amount_cents for c in self.charges) == self.summary.get("charges"))
        payments_ok = (self.declared_payment_count == len(self.payments)
                       and sum(p.amount_cents for p in self.payments) == self.summary.get("payments"))
        return charges_ok, payments_ok

    def containment_gaps(self, statement_charges: List[Tuple[str, int]]) -> dict:
        """Statement charges (date-iso, cents) with no matching cleared charge on
        the same amount within the report. Consumes rec lines greedily so
        duplicates are respected."""
        return MergedRecReports([self]).containment_gaps(statement_charges)


class MergedRecReports:
    """A close month's statement charges span two reconciliations (cycle runs
    ~8th-7th), so containment checks run against the union of cleared charges."""

    def __init__(self, reports: List["QBORecReport"]):
        self.reports = reports
        self.charges = [c for r in reports for c in r.charges]
        self.declared_charge_count = sum(r.declared_charge_count or 0 for r in reports)
        self.reconciled_by = ", ".join(sorted({r.reconciled_by or "?" for r in reports}))
        self.reconciled_on = ", ".join(r.reconciled_on or "?" for r in reports)
        ends = [r.period_end for r in reports if r.period_end]
        self.coverage_end = max(ends) if ends else None

    def summary_ties(self) -> bool:
        return all(r.summary_ties() for r in self.reports)

    def containment_gaps(self, statement_charges: List[Tuple[str, int, str]]) -> dict:
        """Each charge is (txn-date-iso, cents, statement-end-iso). Split:
        'missing' = posted on an already-reconciled statement but absent from the
        books (a real discrepancy); 'awaiting' = posted on a statement the
        bookkeeper hasn't reconciled yet (resolves with the next rec report)."""
        pool: dict = {}
        for c in self.charges:
            pool.setdefault(c.amount_cents, []).append(c.txn_date)
        missing, awaiting = [], []
        cov = self.coverage_end.isoformat() if self.coverage_end else ""
        for d, cents, stmt_end in statement_charges:
            cands = pool.get(cents)
            if cands:
                cands.pop(0)
            elif stmt_end > cov:
                awaiting.append((d, cents))
            else:
                missing.append((d, cents))
        return {"missing": missing, "awaiting": awaiting,
                "coverage_end": self.coverage_end}
