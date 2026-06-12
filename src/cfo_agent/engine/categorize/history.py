"""Precedent-based proposals: how was this merchant coded in prior months?

Recency-weighted: the most recent month's coding wins, because categories
drift (e.g. AI tools recoded from Web Services to R&D AI Everyday when that
category was introduced). The merchant_history table is rebuilt per run
strictly from months BEFORE the close month, so validation never sees its
own answers.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ...models import Proposal
from .. import ledger


def propose(conn, client: str, line: dict) -> Optional[Proposal]:
    rows = ledger.history_detail(conn, client, line["merchant_norm"])
    if not rows:
        return None

    months = sorted({r["close_month"] for r in rows}, reverse=True)
    by_month = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_month[r["close_month"]][r["coa_line"]] += r["n"]

    last = by_month[months[0]]
    candidate = max(last, key=last.get)
    unanimous_last = len(last) == 1

    if unanimous_last and len(months) >= 2:
        prev = by_month[months[1]]
        if len(prev) == 1 and candidate in prev:
            conf = "high"      # unanimous across the two most recent months seen
        else:
            conf = "medium"    # recent coding changed — follow it, but flag
    elif unanimous_last:
        conf = "medium"        # single month of precedent
    else:
        share = last[candidate] / sum(last.values())
        conf = "medium" if share >= 0.8 else "low"

    total = sum(sum(m.values()) for m in by_month.values())
    return Proposal(
        coa_line=candidate, proposed_by="history", confidence=conf,
        rationale=(f"matches {months[0]} coding: {candidate} "
                   f"({total} past txns over {len(months)} month(s))"),
    )
