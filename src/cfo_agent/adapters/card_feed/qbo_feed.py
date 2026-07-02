"""Card-feed adapter (production): read the Chase card charges from QuickBooks
Online via API, replacing the manual CSV.

MECHANISM (verified July 2026 — see QuickBooks-API-Setup-Plan.md): the QBO API
does NOT expose "For Review" bank-feed items. It only reads POSTED transactions.
August already posts Chase charges into the CC register (they show as "Credit
Card Misc."), so we read those as `Purchase` entities:
    GET /v3/company/<realmId>/query?query=
        SELECT * FROM Purchase WHERE AccountRef = '<cc_account_id>'
        AND TxnDate >= '<start>' AND TxnDate <= '<end>'
Each Purchase → CardTxn (TxnDate, payee/EntityRef or line desc → merchant,
TotalAmt → amount_cents). Charge = positive; credits carry Credit=true.

STATUS: scaffold. Conforms to CardFeedAdapter so `card_feed.type: qbo_feed`
swaps it in for the CSV with no engine changes. Fetch is stubbed pending creds.

Needs (see the setup plan): OAuth2 client_id/secret + refresh_token + realmId in
.env, and the 8303 CC Account Id. Token refresh: access token ~1h, refresh
token ~100d and ROTATES — persist the newest refresh token after each use.
"""
from __future__ import annotations

from datetime import date
from typing import List

from ...models import CardTxn, StatementCheck

_REQUIREMENTS = (
    "QBO feed adapter not yet wired. Needs QBO_CLIENT_ID / QBO_CLIENT_SECRET / "
    "QBO_REFRESH_TOKEN / QBO_REALM_ID in .env + the 8303 CC Account Id. See "
    "QuickBooks-API-Setup-Plan.md. Until then use card_feed.type: chase_activity_csv."
)
PROD_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
MINOR_VERSION = 73


class QBOFeed:
    def __init__(self, account_ref: str, realm_id: str = "", **_):
        self.account_ref = account_ref   # the CC Account Id in QBO
        self.realm_id = realm_id

    def fetch_transactions(self, month_start: date, month_end: date) -> List[CardTxn]:
        # TODO: refresh access token from QBO_REFRESH_TOKEN (persist the rotated
        # one), then query posted Purchases for the CC account in the date range
        # and map each to CardTxn. See module docstring for the query.
        raise NotImplementedError(_REQUIREMENTS)

    def verify(self) -> List[StatementCheck]:
        # No printed control total on a live read; completeness is cross-checked
        # against the QBO rec report at close (books_out).
        return []
