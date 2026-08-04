"""v1 reimbursement payout rail: Justworks, manual (bulk-upload CSV).

Justworks is the tax-correct home for a reimbursement (it pays as a non-taxable
payroll line), but exposes no write/expense API — so Penny can't move the money.
Instead this rail produces a CSV in **Justworks' exact Expense Reimbursement
bulk-upload template** (First Name, Last Name, Work Email, Pay Date, Amount,
Notes) that Purvi uploads in Justworks, plus a human-readable paste summary. It
moves no money; status is always 'exported'.
"""
from __future__ import annotations

import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .base import PaymentRail
from ...engine.dm_assemble import _money

# The exact header Justworks' bulk-upload template expects — do not reorder/rename.
JW_HEADER = ["First Name", "Last Name", "Work Email", "Pay Date", "Amount", "Notes"]


class JustworksManual(PaymentRail):
    name = "justworks_manual"

    def __init__(self, cfg):
        self.cfg = cfg
        self.members = cfg.section("reimbursements").get("justworks_members", {}) or {}

    def _member(self, employee: str) -> dict:
        m = self.members.get(employee)
        return m if isinstance(m, dict) else {}

    @staticmethod
    def _split_name(employee: str):
        parts = (employee or "").split()
        if not parts:
            return "", ""
        return parts[0], " ".join(parts[1:])

    # Paystub-facing note: kept generic on purpose — the business purpose and COA
    # category stay in Penny (for Skyfin/QBO), NOT on the employee's paystub.
    PAYSTUB_NOTE = "Expense Reimbursement"

    def _row(self, r: dict, pay_date: str):
        first, last = self._split_name(r.get("employee") or "")
        email = self._member(r.get("employee")).get("work_email", "")
        return [first, last, email, pay_date, f"{r['amount_cents'] / 100:.2f}",
                self.PAYSTUB_NOTE]

    def _paste_text(self, reimbursements: list, pay_date: str) -> str:
        # Purvi's human summary keeps the purpose + category for her reference (this
        # is NOT the paystub note — that's generic; see _row).
        lines = [f"Justworks Expense Reimbursement — pay date {pay_date}:", ""]
        total = 0
        for r in reimbursements:
            total += r["amount_cents"]
            email = self._member(r.get("employee")).get("work_email", "")
            purpose = (r.get("business_purpose") or "").strip() or "reimbursement"
            cat = r.get("proposed_coa_line")
            detail = f"{purpose} [{cat}]" if cat else purpose
            lines.append(f"• {r.get('employee')} <{email}> — {_money(r['amount_cents'])} "
                         f"— {detail}")
        lines += ["", f"Total: {_money(total)} across {len(reimbursements)} reimbursement(s). "
                  "Use the exported CSV with Justworks' bulk upload."]
        return "\n".join(lines)

    def csv_text(self, reimbursements: list, pay_date: str) -> str:
        """The Justworks bulk-upload CSV as a string (header + one row each). This
        is what the dashboard streams as a real download, and what re-download
        regenerates — never depends on a file landing on disk."""
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(JW_HEADER)
        for r in reimbursements:
            w.writerow(self._row(r, pay_date))
        return buf.getvalue()

    def _write_csv(self, reimbursements: list, ref: str, month: str, pay_date: str):
        """Write the Justworks bulk-upload CSV; upload to Drive if configured, else
        local. Returns the artifact link/path, or None on failure."""
        from ...engine import gdrive
        try:
            tmp = Path(tempfile.mktemp(suffix=".csv"))
            with tmp.open("w", newline="") as fh:
                fh.write(self.csv_text(reimbursements, pay_date))
            name = f"justworks-reimbursements-{ref}.csv"
            if gdrive.configured():
                link = gdrive.upload(tmp, name, month)
                tmp.unlink(missing_ok=True)
                return link
            dest = Path(self.cfg.data_root) / "reimbursements" / "exports"
            dest.mkdir(parents=True, exist_ok=True)
            final = dest / name
            tmp.replace(final)
            return str(final)
        except Exception:
            return None

    def pay_batch(self, reimbursements: list, pay_date: str = None) -> dict:
        if not reimbursements:
            return {"rail": self.name, "ref": None, "status": "failed",
                    "paste_text": "", "artifact": None}
        # Pay Date: Purvi picks it; default to today (MM/DD/YYYY, US format). She can
        # adjust in Justworks if the payroll calendar differs.
        pay_date = pay_date or datetime.now(timezone.utc).strftime("%m/%d/%Y")
        ref = "JW-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        month = (reimbursements[0].get("close_month")
                 or (reimbursements[0].get("expense_date") or "")[:7] or "unknown")
        artifact = self._write_csv(reimbursements, ref, month, pay_date)
        return {"rail": self.name, "ref": ref, "status": "exported",
                "paste_text": self._paste_text(reimbursements, pay_date),
                "artifact": artifact,
                "csv_text": self.csv_text(reimbursements, pay_date)}
