"""Receipt repository: where receipt files actually live.

Receipts land in a per-month folder in the Brain, named so a human (and the
audit trail) can find them: <month>/<pal>__<merchant>__<amount>__<date>.<ext>.
The ledger's receipt_link points at the stored file; receipt_status is:
  - 'stored'      the file is in the repository (real, retained)
  - 'referenced'  we only have a Slack file id, not the bytes (NOT retained —
                  needs the @Flo bot token with files:read to fetch, or the pal
                  to drop the file where we can read it)
Only 'stored' counts as a real, audit-safe receipt.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path


def receipts_dir(cfg, month: str) -> Path:
    d = cfg.data_root / "receipts" / month
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40]


def store_file(cfg, month: str, pal: str, line: dict, src_path: Path) -> Path:
    """Copy a receipt file into the repository; return its stored path."""
    ext = Path(src_path).suffix or ".pdf"
    name = (f"{_slug(pal)}__{_slug(line['merchant_raw'])}__"
            f"{line['amount_cents']/100:.2f}__{line['txn_date']}{ext}")
    dest = receipts_dir(cfg, month) / name
    shutil.copyfile(src_path, dest)
    return dest
