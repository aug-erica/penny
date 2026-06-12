"""Precedent-based proposals: how was this merchant coded in prior months?
The merchant_history table is rebuilt per run strictly from months BEFORE the
close month, so validation never sees its own answers."""
from __future__ import annotations

from typing import Optional

from ...models import Proposal
from .. import ledger


def propose(conn, client: str, line: dict) -> Optional[Proposal]:
    hist = ledger.history_for_merchant(conn, client, line["merchant_norm"])
    if not hist:
        return None
    top = hist[0]
    total = sum(h["n"] for h in hist)
    consistent = top["n"] == total
    if consistent and top["months"] >= 2:
        conf = "high"
    elif consistent or top["n"] / total >= 0.8:
        conf = "medium"
    else:
        conf = "low"
    return Proposal(
        coa_line=top["coa_line"], proposed_by="history", confidence=conf,
        rationale=(f"matches prior coding: {top['coa_line']} "
                   f"({top['n']}/{total} past txns, {top['months']} month(s))"),
    )
