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

RECEIPT_THRESHOLD_CENTS = 7500


def receipt_needed(charges: list) -> list:
    return [c for c in charges if c["status"] != "excluded" and c["amount_cents"] > 0
            and (c.get("billable") or c["amount_cents"] >= RECEIPT_THRESHOLD_CENTS)]


def match_by_amount(charges: list, amount_cents: int) -> list:
    """Charges in the receipt-needed set matching this amount and not yet
    receipted. Returns all matches so the caller can flag ambiguity."""
    return [c for c in receipt_needed(charges)
            if c["amount_cents"] == amount_cents and c.get("receipt_status") != "received"]


def record(conn, line_id: int, link: str = None):
    ledger.set_receipt_status(conn, line_id, "received", link)
