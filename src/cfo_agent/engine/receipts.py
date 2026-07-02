"""Receipt handling: match a receipt a pal sends to the charge it belongs to and
record it on the ledger line.

A pal replies with a photo/PDF (and usually says or shows the amount). We match
on amount within that pal's receipt-needed set — the safest key, since a Slack
image carries no structured merchant/date. Ambiguous ($X matches two charges)
is surfaced, not guessed. Routing the file to the retention vault is a separate
step (see Phase 2 receipt-vault decision); here we record status + link.
"""
from __future__ import annotations

from . import ledger

RECEIPT_THRESHOLD_CENTS = 10000   # policy: receipts required over $100 (Purvi, July 2)


def receipt_needed(charges: list) -> list:
    # Over the threshold, OR confirmed-billable-to-a-project (client requirement).
    # NOT the LLM's billable guess alone — that over-asked for small receipts.
    return [c for c in charges if c["status"] != "excluded" and c["amount_cents"] > 0
            and (c["amount_cents"] >= RECEIPT_THRESHOLD_CENTS
                 or (c.get("billable") and c.get("project")))]


def match_by_amount(charges: list, amount_cents: int) -> list:
    """Charges in the receipt-needed set matching this amount and not yet
    receipted. Returns all matches so the caller can flag ambiguity."""
    return [c for c in receipt_needed(charges)
            if c["amount_cents"] == amount_cents and c.get("receipt_status") != "received"]


def record(conn, line_id: int, link: str = None):
    ledger.set_receipt_status(conn, line_id, "received", link)
