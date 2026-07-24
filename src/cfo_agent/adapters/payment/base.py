"""The payment-rail seam for reimbursements.

A PaymentRail takes a batch of APPROVED reimbursements and does whatever "pay"
means for that rail. Money movement stays human-gated: the v1 rail
(justworks_manual) moves no money — it produces a copy-paste payout summary a
human enters in Justworks. A future rail (a Justworks write API, Bill.com, QBO
Bill Pay) can implement the same interface without touching intake/approval.
"""
from __future__ import annotations


class PaymentRail:
    name = "base"

    def pay_batch(self, reimbursements: list, pay_date: str = None) -> dict:
        """Process a batch of approved reimbursement rows (dicts). `pay_date` is
        the payout date the human chose (rails that need one, like Justworks).
        Returns:
          {rail, ref, status, paste_text, artifact}
        where status is one of {'paid','exported','failed'}:
          - 'paid'     the rail actually moved the money
          - 'exported' the rail prepared a payout for a human to release
          - 'failed'   nothing was produced
        `paste_text` is human-ready text (may be ''); `artifact` is a link/path to
        a produced file, or None.
        """
        raise NotImplementedError
