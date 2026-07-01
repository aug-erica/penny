"""Card-feed adapter (production): the native Chase -> QuickBooks Online daily
feed. Chase pushes card transactions into QBO every day for free, so this reads
posted transactions as they land — the enabler for categorizing spend BEFORE
the team files, instead of waiting for a month-end statement PDF.

STATUS: scaffold. Conforms to CardFeedAdapter so `card_feed.type: qbo_feed` in
client.yaml swaps it in for chase_statement_pdf with no engine changes. The
fetch is stubbed pending QBO API access (see _REQUIREMENTS).

What's needed to finish (ask for Skyfin — see Phase1-Status.md):
  1. Confirm August's QuickBooks is ONLINE (not Desktop) — the native feed is QBO-only.
  2. QBO API access: OAuth2 client (client_id/secret), a refresh token, and the
     company realmId. Ideally reuse Skyfin's existing QBO app authorization.
  3. The account id / name of the 8303 credit-card account in QBO (the feed
     lands as bank-feed items on that account).
Then this reads the CreditCard txns for the account via the Reports/Query API,
maps to CardTxn, and the rest of the pipeline is unchanged.
"""
from __future__ import annotations

from datetime import date
from typing import List

from ...models import CardTxn, StatementCheck

_REQUIREMENTS = (
    "QBO feed adapter not yet wired. Needs: (1) QuickBooks Online (confirm not "
    "Desktop), (2) OAuth2 client_id/secret + refresh_token + realmId in .env, "
    "(3) the 8303 card account id in QBO. Until then use card_feed.type: "
    "chase_statement_pdf. See Phase1-Status.md."
)


class QBOFeed:
    def __init__(self, account_ref: str, realm_id: str = "", **_):
        self.account_ref = account_ref
        self.realm_id = realm_id

    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        raise NotImplementedError(_REQUIREMENTS)

    def verify(self) -> List[StatementCheck]:
        # The daily feed has no printed control total to tie to; completeness is
        # instead cross-checked against the QBO rec report at close (books_out).
        return []
