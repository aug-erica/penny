"""Books-out adapter, Phase 1: a CSV a human carries into QuickBooks.
Nothing is ever posted by the agent. This stub exists so the interface is real
from day one; the QBO API adapter replaces it in Phase 3 behind the same call."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List

FIELDS = ["txn_date", "merchant_raw", "amount_cents", "currency", "cardholder",
          "employee", "proposed_coa_line", "billable", "receipt_link", "external_id"]


class CSVStub:
    def export(self, lines: List[dict], dest: str) -> str:
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for line in lines:
                if line.get("status") == "approved":
                    w.writerow(line)
        return str(path)
